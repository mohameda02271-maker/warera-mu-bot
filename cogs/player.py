import re
from typing import Any
from pathlib import Path
from typing import Optional
import json
import os
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
import warera

REMINDERS_FILE = "reminders.json"


def _fmt_num(n: float | int) -> str:
    n = float(n or 0)
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{int(n):,}"


def _parse_iso(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _to_discord_ts(iso_str: str | None) -> str:
    if not iso_str:
        return ""
    dt = _parse_iso(iso_str)
    if dt:
        ts = int(dt.timestamp())
        return f"<t:{ts}:R> (<t:{ts}:t>)"
    return f"`{str(iso_str)[:16].replace('T', ' ')} UTC`"


def _fmt_exact_num(val) -> str:
    if isinstance(val, int):
        return f"{val:,}"
    if isinstance(val, float):
        s = f"{val:,.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return s
    return str(val)


def _extract_bar_pair(obj):
    if obj is None:
        return None, None
    if isinstance(obj, dict):
        cur = obj.get("currentBarValue", obj.get("current", obj.get("cur")))
        mx = obj.get("value", obj.get("max", obj.get("total")))
        return cur, mx
    cur = getattr(obj, "currentBarValue", None)
    if cur is None:
        cur = getattr(obj, "current", None)
    if cur is None:
        cur = getattr(obj, "cur", None)
    mx = getattr(obj, "value", None)
    if mx is None:
        mx = getattr(obj, "max", None)
    if mx is None:
        mx = getattr(obj, "total", None)
    return cur, mx


async def _resolve_name(raw_val, fetcher) -> str:
    if not raw_val:
        return "None"
    if not isinstance(raw_val, str):
        name = getattr(raw_val, "name", None) or getattr(raw_val, "title", None)
        if name:
            return str(name)
        raw_val = getattr(raw_val, "id", None) or getattr(raw_val, "_id", None) or str(raw_val)
    try:
        obj = await fetcher(raw_val)
        if obj:
            name = getattr(obj, "name", None) or getattr(obj, "title", None)
            if name:
                return str(name)
    except Exception as e:
        print(f"[_sync_member_roles error] {type(e).__name__}: {e}")
        changes.append(f"⚠️ خطأ في مزامنة الرولات: {e}")
    return str(raw_val)


def _get_skill_level(skill_obj) -> float:
    if skill_obj is None:
        return 0.0
    if isinstance(skill_obj, dict):
        val = skill_obj.get("level", 0)
        return float(val) if isinstance(val, (int, float)) else 0.0
    val = getattr(skill_obj, "level", 0)
    return float(val) if isinstance(val, (int, float)) else 0.0


def _is_eco_player(skills_obj, user_obj) -> bool:
    """Compare war skill levels vs eco skill levels to classify eco vs war player."""
    if skills_obj is None:
        return True

    def _get(name: str):
        if isinstance(skills_obj, dict):
            return skills_obj.get(name)
        return getattr(skills_obj, name, None)

    war_keys = ("attack", "armor", "dodge", "precision", "criticalChance", "criticalDamages")
    eco_keys = ("companies", "production", "management", "energy")

    war_level = sum(_get_skill_level(_get(k)) for k in war_keys)
    eco_level = sum(_get_skill_level(_get(k)) for k in eco_keys)

    # Pure eco when war skill levels are 0, or eco levels outweigh/equal war levels
    if war_level <= 0:
        return True
    return eco_level >= war_level


def _fmt_bar(skill_obj, kind: str = "health") -> str:
    cur, mx = _extract_bar_pair(skill_obj)
    icon = "💚" if kind == "health" else "🍽️"
    if isinstance(cur, (int, float)) and isinstance(mx, (int, float)) and mx > 0:
        pct = max(0, min(100, int(round((cur / mx) * 100))))
        return f"{icon} **{_fmt_exact_num(cur)} / {_fmt_exact_num(mx)}** `({pct}%)`"
    if isinstance(cur, (int, float)):
        return f"{icon} **{_fmt_exact_num(cur)}**"
    if isinstance(mx, (int, float)):
        return f"{icon} **{_fmt_exact_num(mx)}**"
    return "Hidden"

def _load_reminders() -> list[dict]:
    if not os.path.exists(REMINDERS_FILE):
        return []
    try:
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_reminders(reminders: list[dict]) -> None:
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(reminders, f, indent=2)


LINKS_FILE = Path("data/linked_players.json")
PREV_WARRIOR_FILE = Path("data/weekly_warrior_history.json")
ECO_ROLE_NAMES = ("🌱 Eco Farmer", "Eco Farmer", "eco")
WAR_ROLE_NAMES = ("⚔️ War Fighter", "War Fighter", "war")
GREAT_WARRIOR_ROLE_NAMES = ("👑 Great Warrior", "Great Warrior")
PREV_WARRIOR_ROLE_NAMES = ("🏅 Former Champion", "Former Champion")
TARGET_MU_NAME = "kemet-core"


def _load_links() -> dict[str, dict[str, Any]]:
    if not LINKS_FILE.exists():
        return {}
    try:
        data = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_links(links: dict[str, dict[str, Any]]) -> None:
    LINKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LINKS_FILE.write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")


def _find_linked_member_id(game_username: str, game_user_id: Any = None) -> int | None:
    links = _load_links()
    uname_l = (game_username or "").strip().lower()
    uid_s = str(game_user_id).strip() if game_user_id is not None else ""
    for discord_id_str, info in links.items():
        if not isinstance(info, dict):
            continue
        linked_uname = str(info.get("game_username") or "").strip().lower()
        linked_uid = str(info.get("game_user_id") or "").strip()
        if (uname_l and linked_uname == uname_l) or (uid_s and linked_uid and linked_uid == uid_s):
            try:
                return int(discord_id_str)
            except ValueError:
                return None
    return None


def _find_role(guild: discord.Guild, candidates: tuple[str, ...]) -> discord.Role | None:
    lowered = {c.strip().lower() for c in candidates}
    for role in guild.roles:
        if role.name.strip().lower() in lowered:
            return role
    return None


async def _get_or_create_role(
    guild: discord.Guild,
    candidates: tuple[str, ...],
    color: discord.Color,
    changes: list[str],
    reason: str,
) -> discord.Role | None:
    existing = _find_role(guild, candidates)
    if existing:
        return existing

    me = guild.me
    if me is None or not me.guild_permissions.manage_roles:
        changes.append(f"⚠️ البوت يحتاج صلاحية Manage Roles لإنشاء رول ({candidates[0]})")
        return None

    try:
        created = await guild.create_role(
            name=candidates[0],
            color=color,
            reason=reason,
        )
        changes.append(f"✨ تم إنشاء رول {created.name}")
        return created
    except discord.Forbidden:
        changes.append(f"⚠️ لا توجد صلاحية كافية لإنشاء رول ({candidates[0]})")
        return None
    except Exception as e:
        changes.append(f"⚠️ تعذر إنشاء رول ({candidates[0]}): {e}")
        return None


def _get_weekly_damage(user_obj) -> float:
    rankings = getattr(user_obj, "rankings", None)
    weekly = (
        getattr(rankings, "weeklyUserDamages", None)
        or getattr(rankings, "userWeeklyDamages", None)
    ) if rankings else None
    val = getattr(weekly, "value", 0) if weekly else 0
    return float(val) if isinstance(val, (int, float)) else 0.0


async def _is_top_kemet_core_weekly(user_obj, mu_name: str, mu_raw: Any) -> bool:
    is_kemet = (mu_name or "").strip().lower() == TARGET_MU_NAME
    if not is_kemet and mu_raw is not None:
        resolved = await _resolve_name(mu_raw, warera.mu.get_by_id)
        is_kemet = (resolved or "").strip().lower() == TARGET_MU_NAME
    if not is_kemet:
        return False

    target_uid = getattr(user_obj, "id", None) or getattr(user_obj, "_id", None)
    target_uname = (getattr(user_obj, "username", None) or "").strip().lower()
    target_dmg = _get_weekly_damage(user_obj)

    mu_obj = None
    for fn_name in ("get_by_id", "get_by_query", "get"):
        fn = getattr(warera.mu, fn_name, None)
        if callable(fn) and mu_raw is not None:
            try:
                mu_obj = await fn(mu_raw)
                if mu_obj:
                    break
            except Exception:
                continue

    members_raw = []
    if mu_obj is not None:
        for attr in ("members", "users", "memberIds", "userIds"):
            val = getattr(mu_obj, attr, None)
            if isinstance(val, (list, tuple)) and val:
                members_raw = list(val)
                break

    if not members_raw:
        return True

    max_dmg = target_dmg
    for m in members_raw:
        member_obj = m
        if isinstance(m, (str, int)):
            for fn_name in ("get_by_id", "get_by_query"):
                fn = getattr(warera.user, fn_name, None)
                if callable(fn):
                    try:
                        member_obj = await fn(m)
                        break
                    except Exception:
                        member_obj = None
        if member_obj is None:
            continue
        m_uid = getattr(member_obj, "id", None) or getattr(member_obj, "_id", None)
        m_uname = (getattr(member_obj, "username", None) or "").strip().lower()
        if (target_uid is not None and m_uid == target_uid) or (target_uname and m_uname == target_uname):
            continue
        m_dmg = _get_weekly_damage(member_obj)
        if m_dmg > max_dmg:
            return False

    return True


def _load_prev_warrior_history() -> dict[str, Any]:
    if not PREV_WARRIOR_FILE.exists():
        return {"previous_champions": []}
    try:
        data = json.loads(PREV_WARRIOR_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            champs = data.get("previous_champions")
            return {"previous_champions": list(champs) if isinstance(champs, list) else []}
    except Exception as e:
        print(f"[SYNC ERROR] {type(e).__name__}: {e}", flush=True)
        changes.append(f"⚠️ خطأ في الرولات: {e}")
    return {"previous_champions": []}


def _save_prev_warrior_history(history: dict[str, Any]) -> None:
    PREV_WARRIOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREV_WARRIOR_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_previous_champion(user_obj) -> None:
    uid = str(getattr(user_obj, "id", None) or getattr(user_obj, "_id", None) or "").strip()
    uname = str(getattr(user_obj, "username", None) or "").strip().lower()
    history = _load_prev_warrior_history()
    champs = history["previous_champions"]
    exists = any(
        (uid and str(c.get("id") or "").strip() == uid)
        or (uname and str(c.get("username") or "").strip().lower() == uname)
        for c in champs
        if isinstance(c, dict)
    )
    if not exists and (uid or uname):
        champs.append({"id": uid, "username": uname})
        _save_prev_warrior_history(history)


def _is_previous_champion(user_obj) -> bool:
    uid = str(getattr(user_obj, "id", None) or getattr(user_obj, "_id", None) or "").strip()
    uname = str(getattr(user_obj, "username", None) or "").strip().lower()
    history = _load_prev_warrior_history()
    for c in history["previous_champions"]:
        if not isinstance(c, dict):
            continue
        if (uid and str(c.get("id") or "").strip() == uid) or (
            uname and str(c.get("username") or "").strip().lower() == uname
        ):
            return True
    return False


async def _resolve_guild_member(guild: Optional[discord.Guild], user_id: Optional[int]) -> Optional[discord.Member]:
    if guild is None or user_id is None:
        return None
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except Exception:
        return None


def _apply_style_prefix_to_nick(current_nick: str, is_eco: bool) -> str:
    prefix = "🌱 " if is_eco else "⚔️ "
    cleaned = re.sub(r"^(?:[🌱⚔️]\s*)+", "", current_nick.strip())
    base_name = cleaned or current_nick.strip()
    new_nick = f"{prefix}{base_name}"
    return new_nick[:32]


def _format_style_nick(game_name: str | None, fallback_name: str, is_eco: bool) -> str:
    badge = "🌱" if is_eco else "⚔️"
    source_name = (game_name or "").strip() or (fallback_name or "").strip() or "Player"
    cleaned = re.sub(r"^(?:[🌱⚔️👑🏅]\s*)+", "", source_name).strip()
    core = cleaned or source_name
    return f"{badge} {core}"[:32]


async def _assign_mu_role(
    guild: Optional[discord.Guild],
    member: Optional[discord.Member],
    mu_name: Optional[str],
    mu_raw: Any,
) -> list[str]:
    notes: list[str] = []
    if guild is None or member is None:
        return notes

    clean_name = (mu_name or "").strip()
    if not clean_name and mu_raw not in (None, "", False):
        clean_name = f"MU-{mu_raw}".strip()
    if not clean_name:
        return notes

    role_label = clean_name if clean_name.lower().startswith("mu") else f"MU: {clean_name}"
    role_label = role_label[:100]

    me = guild.me
    can_manage = bool(me and getattr(getattr(me, "guild_permissions", None), "manage_roles", False))
    top_pos = getattr(getattr(me, "top_role", None), "position", 0) if me else 0

    target_role = discord.utils.find(
        lambda r: r.name.lower() == role_label.lower() or r.name.lower() == clean_name.lower(),
        guild.roles,
    )

    if target_role is None:
        if not can_manage:
            notes.append(f"⚠️ Missing Manage Roles to create `{role_label}`")
            return notes
        try:
            target_role = await guild.create_role(name=role_label, reason="Auto MU role assignment")
            notes.append(f"➕ Created role `{target_role.name}`")
        except discord.Forbidden:
            notes.append(f"⚠️ Forbidden creating role `{role_label}`")
            return notes
        except Exception as exc:
            notes.append(f"⚠️ Error creating role `{role_label}`: {exc}")
            return notes

    if not can_manage or target_role.position >= top_pos:
        notes.append(f"⚠️ Role `{target_role.name}` is above bot or Manage Roles missing")
        return notes

    # إزالة أي رتبة وحدة سابقة مختلفة عن رتبة الوحدة الحالية
    old_mu_roles = [
        r for r in member.roles
        if r.id != target_role.id
        and r != guild.default_role
        and r.position < top_pos
        and (r.name.lower().startswith("mu:") or r.name.lower().startswith("mu "))
    ]
    if old_mu_roles:
        try:
            await member.remove_roles(*old_mu_roles, reason="Update MU role")
        except discord.Forbidden:
            notes.append(f"⚠️ Forbidden removing old MU role for `{member.display_name}`")

    if target_role not in member.roles:
        try:
            await member.add_roles(target_role, reason="Assign MU role")
            notes.append(f"🛡️ Assigned `{target_role.name}`")
        except discord.Forbidden:
            notes.append(f"⚠️ Forbidden assigning `{target_role.name}`")
        except Exception as exc:
            notes.append(f"⚠️ Error assigning `{target_role.name}`: {exc}")

    return notes


async def _sync_member_roles(
    guild: Optional[discord.Guild],
    member: Optional[discord.Member],
    user_obj,
    is_eco: bool,
    mu_name: str,
    mu_raw: Any,
) -> list[str]:
    if guild is None or member is None:
        print(f"[SYNC DEBUG] missing guild or member: guild={bool(guild)} member={getattr(member, 'id', None)}", flush=True)
        return []
    me = guild.me
    print(f"[SYNC DEBUG] member={member.id} ({member}) | bot_manage_roles={getattr(getattr(me, 'guild_permissions', None), 'manage_roles', None)} | bot_top_pos={getattr(getattr(me, 'top_role', None), 'position', None)}", flush=True)

    changes: list[str] = []
    eco_role = await _get_or_create_role(
        guild, ECO_ROLE_NAMES, discord.Color.green(), changes, "Auto-create Eco role"
    )
    war_role = await _get_or_create_role(
        guild, WAR_ROLE_NAMES, discord.Color.red(), changes, "Auto-create War role"
    )
    warrior_role = await _get_or_create_role(
        guild, GREAT_WARRIOR_ROLE_NAMES, discord.Color.gold(), changes, "Auto-create Great Warrior role"
    )

    print(f"[SYNC DEBUG] roles -> eco={getattr(eco_role, 'name', None)}({getattr(eco_role, 'position', None)}) war={getattr(war_role, 'name', None)}({getattr(war_role, 'position', None)}) warrior={getattr(warrior_role, 'name', None)}({getattr(warrior_role, 'position', None)})", flush=True)
    desired_style_role = eco_role if is_eco else war_role
    print(f"[SYNC DEBUG] desired={getattr(desired_style_role, 'name', None)} in_roles={bool(desired_style_role and desired_style_role in member.roles)}", flush=True)
    other_style_role = war_role if is_eco else eco_role

    try:
        if other_style_role and other_style_role in member.roles:
            await member.remove_roles(other_style_role, reason="Updated playstyle role")
            changes.append(f"➖ {other_style_role.name}")
        if desired_style_role and desired_style_role not in member.roles:
            print(f"[SYNC DEBUG] adding style role {desired_style_role.name}", flush=True)
            await member.add_roles(desired_style_role, reason="Updated playstyle role")
            changes.append(f"➕ {desired_style_role.name}")

        game_nick = locals().get("player_name") or locals().get("game_name") or locals().get("warera_name") or locals().get("username")
        if isinstance(game_nick, dict):
            game_nick = game_nick.get("name") or game_nick.get("username")
        raw_name = member.nick or member.display_name or member.name
        new_nick = _format_style_nick(game_nick, raw_name, is_eco)
        print(f"[NICK DEBUG] raw={raw_name!r} current_nick={member.nick!r} new_nick={new_nick!r}", flush=True)
        if member.nick != new_nick:
            try:
                await member.edit(nick=new_nick, reason="Add playstyle badge to nickname")
                changes.append(f"🏷️ {new_nick}")
                print(f"[NICK DEBUG] nick updated to {new_nick!r}", flush=True)
            except discord.Forbidden as e:
                print(f"[NICK FORBIDDEN] {e}", flush=True)
                changes.append("⚠️ تعذر تغيير الاسم (تحقق من Manage Nicknames ورتبة العضو/المالك)")
        current_display = member.nick or member.display_name
        desired_nick = _apply_style_prefix_to_nick(current_display, is_eco)
        if desired_nick != member.nick:
            try:
                await member.edit(nick=desired_nick, reason="Sync playstyle badge in server nickname")
                changes.append(f"🏷️ {desired_nick}")
            except discord.Forbidden:
                pass


        prev_warrior_role = await _get_or_create_role(
            guild, PREV_WARRIOR_ROLE_NAMES, discord.Color.orange(), changes, "Auto-create Previous Champion role"
        )
        is_top_warrior = await _is_top_kemet_core_weekly(user_obj, mu_name, mu_raw)
        if is_top_warrior:
            if warrior_role and warrior_role not in member.roles:
                await member.add_roles(warrior_role, reason="Top weekly damage in kemet-core")
                changes.append(f"👑 {warrior_role.name}")
            if prev_warrior_role and prev_warrior_role in member.roles:
                await member.remove_roles(prev_warrior_role, reason="Currently #1 weekly damage in kemet-core")
                changes.append(f"➖ {prev_warrior_role.name}")
        else:
            if warrior_role and warrior_role in member.roles:
                await member.remove_roles(warrior_role, reason="No longer top weekly damage in kemet-core")
                changes.append(f"➖ {warrior_role.name}")
                _record_previous_champion(user_obj)
            if prev_warrior_role and _is_previous_champion(user_obj) and prev_warrior_role not in member.roles:
                await member.add_roles(prev_warrior_role, reason="Former top weekly damage in kemet-core")
                changes.append(f"🏅 {prev_warrior_role.name}")
    except discord.Forbidden:
        changes.append("⚠️ تأكد من صلاحية Manage Roles ورفع رتبة البوت فوق الرولات")
    except Exception as e:
        print(f"[SYNC ERROR] {type(e).__name__}: {e}", flush=True)
        changes.append(f"⚠️ {type(e).__name__}: {e}")

    mu_notes = await _assign_mu_role(guild, member, mu_name, mu_raw)
    if mu_notes:
        changes.extend(mu_notes)
    return changes


class PlayerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.check_reminders.start()

    async def cog_unload(self):
        self.check_reminders.cancel()

    @tasks.loop(seconds=30)
    async def check_reminders(self):
        reminders = _load_reminders()
        if not reminders:
            return

        now_ts = int(datetime.now(timezone.utc).timestamp())
        remaining = []
        changed = False

        for r in reminders:
            if now_ts >= r.get("end_ts", 0):
                changed = True
                channel = self.bot.get_channel(r["channel_id"])
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(r["channel_id"])
                    except Exception:
                        channel = None
                if channel:
                    embed = discord.Embed(
                        title="💊 Pill Debuff Ended!",
                        description=f"<@{r['discord_user_id']}>, the debuff for **{r['username']}** has ended. You are ready to take a new pill!",
                        color=discord.Color.green()
                    )
                    try:
                        await channel.send(
                            content=f"<@{r['discord_user_id']}>",
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions(users=True)
                        )
                    except Exception:
                        pass
            else:
                remaining.append(r)

        if changed:
            _save_reminders(remaining)

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="player", description="Show player stats & sync roles (defaults to your linked account)")
    @app_commands.describe(player="Player Username or User ID (optional if linked)")
    async def player(self, interaction: discord.Interaction, player: Optional[str] = None):
        await interaction.response.defer()
        try:
            links = _load_links()
            linked_entry = links.get(str(interaction.user.id))
            query = (player or "").strip()
            if not query:
                if linked_entry and linked_entry.get("game_username"):
                    query = str(linked_entry["game_username"])
                else:
                    await interaction.followup.send(
                        "❌ اكتب اسم اللاعب أو اربط حسابك أولاً باستخدام `/link <username>`."
                    )
                    return

            u = await warera.user.get_by_query(query)

            username = getattr(u, "username", None) or "Unknown"
            avatar = getattr(u, "avatarUrl", None)

            mu_raw = getattr(u, "mu", None) or getattr(u, "muId", None)
            country_raw = getattr(u, "country", None) or getattr(u, "countryId", None)
            mu_name = await _resolve_name(mu_raw, warera.mu.get_by_id)
            country_name = await _resolve_name(country_raw, warera.country.get_by_id)

            rankings = getattr(u, "rankings", None)
            weekly = (
                getattr(rankings, "weeklyUserDamages", None)
                or getattr(rankings, "userWeeklyDamages", None)
            ) if rankings else None
            weekly_dmg = getattr(weekly, "value", 0) or 0
            weekly_rank = getattr(weekly, "rank", None)
            weekly_tier = getattr(weekly, "tier", None)

            rank_text = f"#{int(weekly_rank):,}" if isinstance(weekly_rank, (int, float)) else "Unranked"
            if weekly_tier:
                rank_text += f" ({weekly_tier})"

            total = getattr(rankings, "userDamages", None) if rankings else None
            total_dmg = getattr(total, "value", 0) or 0

            skills = getattr(u, "skills", None)
            hp_text = _fmt_bar(getattr(skills, "health", None) if skills else None, "health")
            hg_text = _fmt_bar(getattr(skills, "hunger", None) if skills else None, "hunger")

            buffs = getattr(u, "buffs", None)
            if buffs is None:
                pill_text = "❌ No Pill"
            else:
                buff_codes = getattr(buffs, "buffCodes", None) or []
                debuff_codes = getattr(buffs, "debuffCodes", None) or []
                if buff_codes:
                    pill_text = f"✅ Active ({', '.join(map(str, buff_codes))})"
                    end_ts = _to_discord_ts(getattr(buffs, "buffEndAt", None))
                    if end_ts:
                        pill_text += f"\nEnds: {end_ts}"
                elif debuff_codes:
                    pill_text = f"⚠️ Debuff ({', '.join(map(str, debuff_codes))})"
                    end_ts = _to_discord_ts(getattr(buffs, "debuffEndAt", None))
                    if end_ts:
                        pill_text += f"\nEnds: {end_ts}"
                else:
                    pill_text = "❌ No Pill"

            is_eco = _is_eco_player(skills, u)
            status_line = (
                "مقاطع الحرب ورايح يحلب البقرة • الاخ بيلعب المزرعه السعيده"
                if is_eco
                else "عايش في ميدان المعركة وناسي الدنيا • كيمت كور فرحانه بيك"
            )

            embed = discord.Embed(
                title=f"👤 {username}",
                description=f"**{status_line}**",
                color=discord.Color.gold()
            )
            if avatar:
                embed.set_thumbnail(url=avatar)

            embed.add_field(name="🛡️ MU", value=mu_name, inline=True)
            embed.add_field(name="🌍 Country", value=country_name, inline=True)
            embed.add_field(name="⚔️ Total Damage", value=_fmt_num(total_dmg), inline=True)
            embed.add_field(name="💥 Weekly Damage", value=_fmt_num(weekly_dmg), inline=True)
            embed.add_field(name="🏆 Weekly Rank", value=rank_text, inline=True)
            embed.add_field(name="💊 Pill Status", value=pill_text, inline=False)
            embed.add_field(name="Health", value=hp_text, inline=True)
            embed.add_field(name="Hunger", value=hg_text, inline=True)

            embed.set_footer(text="design & engineer by great-ayyubi")

            target_member: Optional[discord.Member] = None
            if interaction.guild:
                if not player and isinstance(interaction.user, discord.Member):
                    target_member = interaction.user
                else:
                    game_uid = getattr(u, "id", None) or getattr(u, "_id", None)
                    linked_discord_id = _find_linked_member_id(username, game_uid)
                    if linked_discord_id:
                        target_member = await _resolve_guild_member(interaction.guild, linked_discord_id)

            role_changes = await _sync_member_roles(
                interaction.guild, target_member, u, is_eco, mu_name, mu_raw
            )
            if role_changes and target_member:
                embed.add_field(
                    name="🎭 Roles Updated",
                    value=f"{target_member.mention}: " + " • ".join(role_changes),
                    inline=False,
                )

            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`")

    @app_commands.command(name="pill_remind", description="Set a reminder when a player's pill debuff ends")
    @app_commands.describe(
        player="Player Username or User ID",
        member="Discord member to mention (defaults to you)",
        channel="Channel to send the reminder in (defaults to this channel)"
    )
    async def pill_remind(
        self,
        interaction: discord.Interaction,
        player: str,
        member: discord.Member | None = None,
        channel: discord.TextChannel | None = None
    ):
        await interaction.response.defer()
        try:
            target_member = member or interaction.user
            target_channel = channel or interaction.channel

            u = await warera.user.get_by_query(player)
            username = getattr(u, "username", None) or player
            buffs = getattr(u, "buffs", None)

            debuff_codes = (getattr(buffs, "debuffCodes", None) or []) if buffs else []
            debuff_end_raw = getattr(buffs, "debuffEndAt", None) if buffs else None
            debuff_end_dt = _parse_iso(debuff_end_raw)

            if debuff_codes and debuff_end_dt:
                end_ts = int(debuff_end_dt.timestamp())
                now_ts = int(datetime.now(timezone.utc).timestamp())

                if end_ts <= now_ts:
                    await interaction.followup.send(f"✅ **{username}**'s debuff has already ended! Ready for a pill.")
                    return

                reminders = _load_reminders()
                reminders = [
                    r for r in reminders
                    if not (r["discord_user_id"] == target_member.id and r["username"].lower() == username.lower())
                ]
                reminders.append({
                    "discord_user_id": target_member.id,
                    "channel_id": target_channel.id,
                    "username": username,
                    "end_ts": end_ts,
                })
                _save_reminders(reminders)

                embed = discord.Embed(
                    title="⏰ Pill Reminder Set",
                    description=(
                        f"I will mention {target_member.mention} in {target_channel.mention} "
                        f"when **{username}**'s debuff ends:\n<t:{end_ts}:R> (<t:{end_ts}:t>)"
                    ),
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed)
                return

            buff_codes = (getattr(buffs, "buffCodes", None) or []) if buffs else []
            buff_end_ts = _to_discord_ts(getattr(buffs, "buffEndAt", None) if buffs else None)
            if buff_codes:
                await interaction.followup.send(
                    f"💊 **{username}** currently has an active pill ({', '.join(map(str, buff_codes))}). "
                    f"It ends {buff_end_ts or 'soon'}. Run `/pill_remind` again once the debuff starts!"
                )
                return

            await interaction.followup.send(f"✅ **{username}** has no active debuff right now. You can take a pill!")
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`")



    @app_commands.command(name="link", description="Link your Discord account to your WarEra game profile")
    @app_commands.describe(
        player="Your WarEra Username or User ID",
        member="Discord member to link (optional)"
    )
    async def link(
        self,
        interaction: discord.Interaction,
        player: str,
        member: Optional[discord.Member] = None,
    ):
        await interaction.response.defer()
        try:
            target = member or interaction.user
            u = await warera.user.get_by_query(player)
            username = getattr(u, "username", None) or player
            game_uid = getattr(u, "id", None) or getattr(u, "_id", None)

            links = _load_links()
            links[str(target.id)] = {
                "game_username": username,
                "game_user_id": game_uid,
            }
            _save_links(links)

            mu_raw = getattr(u, "mu", None) or getattr(u, "muId", None)
            mu_name = await _resolve_name(mu_raw, warera.mu.get_by_id)
            skills = getattr(u, "skills", None)
            is_eco = _is_eco_player(skills, u)

            guild_member = await _resolve_guild_member(interaction.guild, target.id)
            role_changes = await _sync_member_roles(
                interaction.guild, guild_member, u, is_eco, mu_name, mu_raw
            )

            msg = f"✅ تم ربط {target.mention} بحساب اللعبة **{username}**."
            if role_changes:
                msg += "\n" + "\n".join(role_changes)
            if role_changes:
                msg += "\n🎭 **الرولات:** " + " • ".join(role_changes)
            await interaction.followup.send(msg)
        except Exception as e:
            await interaction.followup.send(f"❌ Error: `{e}`")

    @app_commands.command(name="unlink", description="Unlink a Discord account from a WarEra profile")
    @app_commands.describe(member="Discord member to unlink (defaults to you)")
    async def unlink(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        target = member or interaction.user
        links = _load_links()
        removed = links.pop(str(target.id), None)
        _save_links(links)
        if removed:
            await interaction.response.send_message(f"🔓 تم فك ربط {target.mention} بنجاح.")
        else:
            await interaction.response.send_message(f"ℹ️ {target.mention} غير مربوط بأي حساب حالياً.")


    @app_commands.command(name="help", description="Show Kemet-core commands and automatic role/nickname features")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📋 Kemet-core — Commands & Features",
            color=discord.Color.gold(),
            description=(
                "• `/link` — Link your Discord account to your game profile & sync roles\n"
                "• `/player` — View stats (`MU`, damage, rank, pill, health, hunger) & sync roles\n"
                "• `/pill_remind` — Set a reminder for pill / debuff timing\n"
                "• `/unlink` — Unlink your Discord account from a game profile\n"
                "• **Auto Roles & Nickname** — Syncs playstyle roles (`⚔️` / `🌱`) and adds `⚔️` or `🌱` prefix when permitted"
            ),
        )
        embed.set_footer(text="design & engineer by great-ayyubi")
        await interaction.response.send_message(embed=embed)

    async def _fetch_user_obj(self, query: str):
        try:
            return await warera.user.get_by_query(query)
        except Exception:
            return None

    def _extract_pill_info(self, u) -> str:
        buffs = getattr(u, "buffs", None)
        if not buffs:
            return "❌ No Pill"

        buff_codes = list(getattr(buffs, "buffCodes", None) or [])
        debuff_codes = list(getattr(buffs, "debuffCodes", None) or [])

        ends_val = None
        for attr in (
            "buffEndsAt", "buff_ends_at", "endsAt", "ends_at",
            "expiresAt", "expires_at", "pillEndsAt", "pill_ends_at",
            "debuffEndsAt", "debuff_ends_at",
        ):
            val = getattr(buffs, attr, None) or getattr(u, attr, None)
            if val:
                ends_val = val
                break

        ts = ""
        if ends_val:
            if isinstance(ends_val, datetime):
                ts = _to_discord_ts(ends_val.isoformat())
            elif isinstance(ends_val, (int, float)):
                ts = _to_discord_ts(datetime.fromtimestamp(ends_val).isoformat())
            else:
                ts = _to_discord_ts(str(ends_val))

        if buff_codes:
            codes = ", ".join(map(str, buff_codes))
            return f"✅ {codes}" + (f" ({ts})" if ts else "")
        if debuff_codes:
            codes = ", ".join(map(str, debuff_codes))
            return f"⚠️ Debuff ({codes})" + (f" ({ts})" if ts else "")
        return "❌ No Pill"

    @app_commands.command(name="help", description="Show Kemet-core commands and automatic role/nickname features")
    async def help_cmd(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="📋 Kemet-core — Commands & Features",
            color=discord.Color.gold(),
            description=(
                "• `/link` — Link your Discord account to your game profile & sync roles\n"
                "• `/player` — View stats (`MU`, damage, rank, pill, health, hunger) & sync roles\n"
                "• `/pill_remind` — Set a reminder for pill / debuff timing\n"
                "• `/unlink` — Unlink your Discord account from a game profile\n"
                "• `/mu_roster` — MU Owner readiness sheet (pill, timer, health, hunger)\n"
                "• `/mu_weekly` — MU Owner weekly & total damage leaderboard\n"
                "• `/mu_sync` — MU Owner bulk role/nickname sync & unlinked audit"
            ),
        )
        embed.set_footer(text="design & engineer by great-ayyubi")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="mu_roster", description="MU Owner: View linked members readiness (pill, health, hunger, style)")
    async def mu_roster(self, interaction: discord.Interaction):
        await interaction.response.defer()
        links = _load_links()
        if not links:
            await interaction.followup.send("❌ لا توجد حسابات مربوطة بعد.")
            return

        blocks = []
        war_cnt = 0
        eco_cnt = 0

        for disc_id, info in list(links.items())[:20]:
            key = (
                info.get("game_username")
                or info.get("username")
                or info.get("user_id")
                or info.get("game_user_id")
                or disc_id
            )
            u = await self._fetch_user_obj(str(key))
            member = await _resolve_guild_member(interaction.guild, int(disc_id)) if disc_id.isdigit() else None

            name = getattr(u, "username", None) or (member.display_name if member else str(key))
            skills = getattr(u, "skills", None) if u else None
            is_eco = _is_eco_player(skills, u) if u else False
            if is_eco:
                eco_cnt += 1
            else:
                war_cnt += 1

            badge = "🌱" if is_eco else "⚔️"
            hp_text = _fmt_bar(getattr(skills, "health", None) if skills else None, "health") if u else "—"
            hg_text = _fmt_bar(getattr(skills, "hunger", None) if skills else None, "hunger") if u else "—"
            pill_text = self._extract_pill_info(u) if u else "❌ Unknown"

            blocks.append(
                f"{badge} **{name}**\n"
                f"💊 Pill: {pill_text}\n"
                f"❤️ HP: {hp_text}  •  🍖 HG: {hg_text}"
            )

        embed = discord.Embed(
            title="🛡️ MU Readiness Roster",
            color=discord.Color.gold(),
            description="\n\n".join(blocks) if blocks else "لا توجد بيانات متاحة.",
        )
        embed.add_field(
            name="Style Breakdown",
            value=f"⚔️ War: **{war_cnt}**  |  🌱 Eco: **{eco_cnt}**",
            inline=False,
        )
        embed.set_footer(text="design & engineer by great-ayyubi")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="mu_weekly", description="MU Owner: Weekly & total damage leaderboard for linked members")
    async def mu_weekly(self, interaction: discord.Interaction):
        await interaction.response.defer()
        links = _load_links()
        if not links:
            await interaction.followup.send("❌ لا توجد حسابات مربوطة بعد.")
            return

        rows = []
        for disc_id, info in links.items():
            key = (
                info.get("game_username")
                or info.get("username")
                or info.get("user_id")
                or info.get("game_user_id")
                or disc_id
            )
            u = await self._fetch_user_obj(str(key))
            if not u:
                continue

            name = getattr(u, "username", None) or str(key)
            rankings = getattr(u, "rankings", None)
            weekly = (
                getattr(rankings, "weeklyUserDamages", None)
                or getattr(rankings, "userWeeklyDamages", None)
            ) if rankings else None
            wk_dmg = float(getattr(weekly, "value", 0) or _get_weekly_damage(u) or 0)
            wk_rank = getattr(weekly, "rank", None)
            total = getattr(rankings, "userDamages", None) if rankings else None
            tot_dmg = float(getattr(total, "value", 0) or 0)
            rank_str = f"#{int(wk_rank):,}" if isinstance(wk_rank, (int, float)) else "—"
            rows.append((wk_dmg, tot_dmg, name, rank_str))

        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)
        lines = [
            f"**{idx}.** {name}\n💥 Weekly: **{_fmt_num(wk)}** • ⚔️ Total: {_fmt_num(tot)} • 🏆 {rk}"
            for idx, (wk, tot, name, rk) in enumerate(rows[:15], start=1)
        ]

        embed = discord.Embed(
            title="📊 MU Weekly Damage Standings",
            color=discord.Color.gold(),
            description="\n\n".join(lines) if lines else "لا توجد بيانات دمج متاحة.",
        )
        embed.set_footer(text="design & engineer by great-ayyubi")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="mu_sync", description="MU Owner: Bulk sync roles/nicknames and audit linked/unlinked members")
    async def mu_sync(self, interaction: discord.Interaction):
        await interaction.response.defer()
        links = _load_links()
        synced = 0
        issues = []

        for disc_id, info in links.items():
            if not disc_id.isdigit():
                continue
            member = await _resolve_guild_member(interaction.guild, int(disc_id))
            if not member:
                issues.append(f"• ID `{disc_id}`: خارج السيرفر")
                continue

            key = (
                info.get("game_username")
                or info.get("username")
                or info.get("user_id")
                or info.get("game_user_id")
                or member.display_name
            )
            u = await self._fetch_user_obj(str(key))
            try:
                skills_obj = getattr(u, "skills", None) if u else None
                is_eco_val = _is_eco_player(skills_obj, u) if u else False
                mu_raw_val = (getattr(u, "mu", None) or getattr(u, "muId", None)) if u else info.get("mu")
                mu_name_val = await _resolve_name(mu_raw_val, warera.mu.get_by_id) if mu_raw_val else ""
                res = await _sync_member_roles(interaction.guild, member, u, is_eco_val, mu_name_val, mu_raw_val)
                synced += 1
                if isinstance(res, str) and ("⚠️" in res or "Forbidden" in res):
                    issues.append(f"• {member.display_name}: {res}")
            except discord.Forbidden:
                issues.append(f"• {member.display_name}: صلاحيات أو ترتيب رتب")
            except Exception as exc:
                issues.append(f"• {member.display_name}: {exc}")

        total_members = len([m for m in interaction.guild.members if not m.bot]) if interaction.guild else 0
        other_members = max(total_members - len(links), 0)

        embed = discord.Embed(
            title="🔄 MU Bulk Sync & MU Roles",
            color=discord.Color.gold(),
            description=(
                f"• الحسابات المربوطة: **{len(links)}**\n"
                f"• تمت مزامنة وحداتهم ورتبهم: **{synced}**\n"
                f"• أعضاء آخرون في السيرفر (غير مربوطين): **{other_members}**"
            ),
        )
        if issues:
            embed.add_field(
                name="Notes",
                value="\n".join(issues[:10]),
                inline=False,
            )
        embed.set_footer(text="design & engineer by great-ayyubi")
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(PlayerCog(bot))
