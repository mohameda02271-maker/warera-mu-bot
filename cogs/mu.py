import asyncio
import json
import os
import re
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

import warera


def _fmt_num(val: Any) -> str:
    if val is None:
        return "0"
    try:
        num = float(val)
        if num.is_integer():
            return f"{int(num):,}"
        return f"{num:,.2f}"
    except (TypeError, ValueError):
        return str(val)


def _get_attr_or_key(obj: Any, *keys: str) -> Any:
    for k in keys:
        if obj is None:
            return None
        if isinstance(obj, dict) and k in obj:
            return obj[k]
        val = getattr(obj, k, None)
        if val is not None:
            return val
    return None


async def _search_mu_ids(query: str) -> list[str]:
    q = query.strip()
    if re.fullmatch(r"[0-9a-fA-F]{24}", q):
        return [q]

    headers = warera._headers() if hasattr(warera, "_headers") else {"Accept": "application/json"}
    url = f"{warera.BASE_URL}/trpc/search.searchAnything"
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            params={"input": json.dumps({"searchText": q})},
            headers=headers,
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"Search failed ({resp.status}): {text[:200]}")
            raw = await resp.json(content_type=None)

    data = raw.get("result", {}).get("data", {}) if isinstance(raw, dict) else {}
    if isinstance(data, dict) and "json" in data and isinstance(data["json"], dict):
        data = data["json"]

    mu_ids = data.get("muIds") if isinstance(data, dict) else []
    if isinstance(mu_ids, list):
        return [str(x) for x in mu_ids if x]
    return []


async def _pick_best_mu(mu_ids: list[str]) -> tuple[Any, list[tuple[str, Any]]]:
    mus = await asyncio.gather(*(warera.mu.get_by_id(mid) for mid in mu_ids), return_exceptions=True)
    valid: list[tuple[float, str, Any]] = []
    for mid, item in zip(mu_ids, mus):
        if isinstance(item, Exception) or item is None:
            continue
        rankings = _get_attr_or_key(item, "rankings")
        total_dmg = _get_attr_or_key(_get_attr_or_key(rankings, "muDamages"), "value") or 0
        try:
            score = float(total_dmg)
        except (TypeError, ValueError):
            score = 0.0
        valid.append((score, mid, item))

    if not valid:
        first_mu = await warera.mu.get_by_id(mu_ids[0])
        return first_mu, []

    valid.sort(key=lambda t: t[0], reverse=True)
    _, best_id, best_mu = valid[0]
    others = [(mid, item) for _, mid, item in valid[1:] if mid != best_id]
    return best_mu, others


async def _resolve_member_names(member_ids: list[str], limit: int = 20) -> list[str]:
    targets = member_ids[:limit]

    async def fetch_one(uid: str) -> str:
        try:
            u = await warera.user.get_by_id(uid)
            uname = _get_attr_or_key(u, "username", "name")
            return str(uname) if uname else uid
        except Exception:
            return uid

    names = await asyncio.gather(*(fetch_one(uid) for uid in targets))
    if len(member_ids) > limit:
        names.append(f"... (+{len(member_ids) - limit})")
    return names


class MilitaryUnitCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.default_query = os.getenv("MU_ID") or "KEMET-CORE"

    @app_commands.command(name="mu", description="عرض بيانات الوحدة العسكرية والأعضاء والدامج")
    @app_commands.describe(query="اسم/يوزر نيم الوحدة (مثل KEMET-CORE) أو معرّف الوحدة")
    async def mu_command(self, interaction: discord.Interaction, query: str | None = None) -> None:
        await interaction.response.defer(thinking=True)
        search_query = (query or self.default_query).strip()

        try:
            mu_ids = await _search_mu_ids(search_query)
            if not mu_ids:
                await interaction.followup.send(
                    f"لم يتم العثور على وحدة عسكرية مطابقة لـ: `{search_query}`",
                    ephemeral=True,
                )
                return

            mu, other_mus = await _pick_best_mu(mu_ids)
            mu_id = str(_get_attr_or_key(mu, "_id", "id") or mu_ids[0])
            name = str(_get_attr_or_key(mu, "name") or f"MU #{mu_id}")
            avatar_url = _get_attr_or_key(mu, "avatarUrl", "avatar")

            members_raw = _get_attr_or_key(mu, "members") or []
            member_ids = [str(m) for m in members_raw if m] if isinstance(members_raw, list) else []
            member_names = await _resolve_member_names(member_ids)

            rankings = _get_attr_or_key(mu, "rankings")
            weekly_obj = _get_attr_or_key(rankings, "muWeeklyDamages")
            total_obj = _get_attr_or_key(rankings, "muDamages")

            weekly_dmg = _get_attr_or_key(weekly_obj, "value") or 0
            weekly_rank = _get_attr_or_key(weekly_obj, "rank")
            weekly_tier = _get_attr_or_key(weekly_obj, "tier")

            total_dmg = _get_attr_or_key(total_obj, "value") or 0
            total_rank = _get_attr_or_key(total_obj, "rank")
            total_tier = _get_attr_or_key(total_obj, "tier")

            weekly_text = f"**{_fmt_num(weekly_dmg)}**"
            if weekly_rank is not None:
                weekly_text += f"\nRank: `#{int(weekly_rank):,}`" + (f" ({weekly_tier})" if weekly_tier else "")

            total_text = f"**{_fmt_num(total_dmg)}**"
            if total_rank is not None:
                total_text += f"\nRank: `#{int(total_rank):,}`" + (f" ({total_tier})" if total_tier else "")

            embed = discord.Embed(
                title=f"🛡️ {name}",
                description=f"الوحدة: **{name}**",
                color=discord.Color.gold(),
            )
            if isinstance(avatar_url, str) and avatar_url.startswith(("http://", "https://")):
                embed.set_thumbnail(url=avatar_url)

            embed.add_field(name="💥 Weekly Damage", value=weekly_text, inline=True)
            embed.add_field(name="⚔️ Total Damage", value=total_text, inline=True)
            embed.add_field(name="👥 Members Count", value=f"**{len(member_ids):,}**", inline=True)

            members_value = "، ".join(member_names) if member_names else "لا يوجد أعضاء"
            embed.add_field(name="📋 Members", value=members_value[:1024], inline=False)

            if other_mus:
                other_names = []
                for oid, omu in other_mus[:5]:
                    oname = str(_get_attr_or_key(omu, "name") or f"MU #{oid}")
                    other_names.append(f"**{oname}**")
                embed.set_footer(text=f"نتائج إضافية مطابقة لـ {search_query}: {len(other_mus)}")
                embed.add_field(name="🔎 وحدات أخرى مطابقة", value="، ".join(other_names), inline=False)

            await interaction.followup.send(embed=embed)
        except Exception as exc:
            await interaction.followup.send(
                f"حصل خطأ أثناء جلب بيانات الوحدة: `{exc}`",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MilitaryUnitCog(bot))


def _load_dotenv_values(path: str = ".env") -> dict[str, str]:
    vals: dict[str, str] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                s = line.strip()
                if s and not s.startswith("#") and "=" in s:
                    k, v = s.split("=", 1)
                    vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def run_bot() -> None:
    env_vals = _load_dotenv_values()
    token = (os.getenv("DISCORD_TOKEN") or env_vals.get("DISCORD_TOKEN") or "").strip()
    api_key = (os.getenv("WARERA_API_KEY") or env_vals.get("WARERA_API_KEY") or "").strip()

    if api_key and hasattr(warera, "set_api_key"):
        warera.set_api_key(api_key)

    if not token:
        raise RuntimeError("DISCORD_TOKEN غير موجود في البيئة أو ملف .env")

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)

    @bot.event
    async def setup_hook() -> None:
        await bot.add_cog(MilitaryUnitCog(bot))
        await bot.tree.sync()

    @bot.event
    async def on_ready() -> None:
        print(f"Bot logged in as {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")

    bot.run(token)


if __name__ == "__main__":
    run_bot()
