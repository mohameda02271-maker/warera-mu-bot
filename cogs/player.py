import discord
from discord import app_commands
from discord.ext import commands
import warera


class PlayerCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _resolve_country_name(self, player: object) -> str:
        direct_country = (
            getattr(player, "country_name", None)
            or getattr(player, "country", None)
        )
        if isinstance(direct_country, str) and direct_country.strip():
            return direct_country.strip()

        country_id = getattr(player, "country_id", None)
        if country_id:
            try:
                country_obj = await warera.country.get_by_id(str(country_id))
                resolved = getattr(country_obj, "name", None)
                if resolved:
                    return f"{resolved} (`{country_id}`)"
            except Exception:
                pass
            return f"معرّف دولة: `{country_id}`"

        return "غير محددة"

    @app_commands.command(
        name="player",
        description="عرض بيانات لاعب ومعرفة الدولة التابع لها",
    )
    @app_commands.describe(user_id="معرّف اللاعب (User ID) في WarEra")
    async def player_command(
        self, interaction: discord.Interaction, user_id: str
    ) -> None:
        await interaction.response.defer(thinking=True)
        try:
            player = await warera.user.get_by_id(user_id.strip())
            username = (
                getattr(player, "username", None)
                or getattr(player, "name", None)
                or f"Player #{user_id}"
            )
            country_display = await self._resolve_country_name(player)

            embed = discord.Embed(
                title=f"🎮 {username}",
                description=f"المعرّف: `{user_id.strip()}`",
                color=discord.Color.green(),
            )
            embed.add_field(name="🌍 الدولة", value=country_display, inline=False)

            for attr, label in [
                ("level", "المستوى"),
                ("mu_id", "الوحدة العسكرية"),
                ("role", "الدور/الرتبة"),
            ]:
                val = getattr(player, attr, None)
                if val is not None:
                    embed.add_field(name=label, value=str(val), inline=True)

            await interaction.followup.send(embed=embed)
        except Exception as exc:
            await interaction.followup.send(
                f"تعذر جلب بيانات اللاعب `{user_id}`: `{exc}`",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlayerCog(bot))
