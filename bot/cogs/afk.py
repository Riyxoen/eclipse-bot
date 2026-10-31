"""``/afk`` — mark yourself away (slash equivalent of ``·afk``).

Stays thin: it stores the status and applies the ``[AFK]`` nickname tag, then
confirms. Removal is automatic — the next message the member sends in this
server clears the status and restores their nickname (see
:class:`bot.services.afk.AfkEventHandler`).
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.services.afk import (
    AfkService,
    build_afk_nickname,
    strip_afk_nickname,
)
from bot.utilities.embeds import afk_embed

logger = logging.getLogger("riyxoen.afk_cog")


@app_commands.command(name="afk", description="Mark yourself as AFK with an optional reason.")
@app_commands.guild_only()
@app_commands.describe(reason="Why you are away — shown when someone mentions you.")
async def afk_command(interaction: discord.Interaction, reason: str | None = None) -> None:
    """Set or update the caller's AFK status."""
    service: AfkService | None = getattr(interaction.client, "afk_service", None)
    guild = interaction.guild
    if service is None or guild is None:
        await interaction.response.send_message(
            "The AFK system is not available right now.", ephemeral=True
        )
        return

    member = interaction.user
    display_name = getattr(member, "display_name", None) or getattr(member, "name", "member")
    base_name = strip_afk_nickname(display_name)
    requested = (reason or "").strip()
    existing = service.get(guild.id, member.id)
    if existing is not None and not requested:
        # Keep the previous reason when the new command omits one.
        requested = existing.afk_message

    service.set_afk(guild.id, member.id, base_name, requested)
    tagged = await service.apply_nickname(member, build_afk_nickname(base_name))

    description = f"{member.mention} is now AFK."
    if requested:
        description += f"\n> {requested}"
    if not tagged:
        description += (
            "\n_I couldn't change your nickname — I may be missing Manage Nicknames "
            "or be lower in the role hierarchy._"
        )
    title = "AFK Updated" if existing is not None else "AFK"
    await interaction.response.send_message(embed=afk_embed(title, description), ephemeral=True)
