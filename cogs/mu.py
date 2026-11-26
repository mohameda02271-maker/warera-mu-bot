import os
import discord
from discord import app_commands
from discord.ext import commands
import warera


class MilitaryUnitCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.mu_id = os.getenv("MU_ID", "YOUR_MU_ID_HERE")

    @app_commands.command(name="mu", description="عرض بيانات الوحدة العسكرية")
    async def mu_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            mu = await warera.mu.get_by_id(self.mu_id)

            name = getattr(mu, "name", None) or f"MU #{self.mu_id}"
            embed = discord.Embed(
                title=f"🛡️ {name}",
                description=f"معرّف الوحدة: `{self.mu_id}`",
                color=discord.Color.gold(),
            )

            for attr, label in [
                ("leader_id", "القائد"),
                ("members_count", "عدد الأعضاء"),
                ("country_id", "الدولة"),
            ]:
                val = getattr(mu, attr, None)
                if val is not None:
                    embed.add_field(name=label, value=str(val), inline=True)

            await interaction.followup.send(embed=embed)
        except Exception as exc:
            await interaction.followup.send(
                f"حصل خطأ أثناء جلب بيانات الوحدة: `{exc}`",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MilitaryUnitCog(bot))
