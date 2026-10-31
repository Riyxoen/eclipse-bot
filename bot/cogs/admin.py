"""Shared plumbing for administrator-only slash-command groups.

``/config`` and ``/automod`` need exactly the same three things: a
server-side administrator gate, a safe denial responder, and a
"validate → persist → report old → new" helper. Those lived as identical
copies in both cogs; this mixin is the single implementation.

Subclasses override :meth:`_announce_change` when they want the change
mirrored to the guild's log channel (``/config`` does; ``/automod`` does not).
"""

from __future__ import annotations

import logging

import discord

from bot.configuration.errors import GuildConfigError
from bot.moderation.errors import ModerationError
from bot.services.guild_config import GuildConfigService

logger = logging.getLogger("riyxoen.admin_commands")


class AdminCommandMixin:
    """Administrator gate plus shared configuration-change helpers."""

    #: Provided by the concrete cog that mixes this in.
    config_service: GuildConfigService

    # ------------------------------------------------------------ permission

    def _require_admin(self, interaction: discord.Interaction) -> None:
        """Gate a command behind the shared admin permission layer (server-side)."""
        permissions = interaction.client.permissions
        permissions.require_guild(interaction.guild)
        permissions.require_administrator(interaction.user, interaction.guild)

    async def _deny(
        self, interaction: discord.Interaction, exc: ModerationError | GuildConfigError
    ) -> None:
        """Show a safe denial/validation message; detail is in the logs."""
        logger.info("admin command denied: %s (user=%s)", exc.user_message, interaction.user.id)
        await interaction.response.send_message(exc.user_message, ephemeral=True)

    # --------------------------------------------------------------- changes

    async def _announce_change(
        self, interaction: discord.Interaction, summary: str, keys: list[str]
    ) -> None:
        """Hook for a log-channel summary of a change. Default: nothing."""
        return None

    async def _apply(
        self,
        interaction: discord.Interaction,
        changes: dict,
        *,
        summary: str,
    ) -> None:
        """Validate via the service, respond with old -> new, then announce."""
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        try:
            previous = self.config_service.get(interaction.guild.id)
            self.config_service.update(
                interaction.guild.id,
                actor_user_id=interaction.user.id,
                changes=changes,
            )
        except GuildConfigError as exc:
            await self._deny(interaction, exc)
            return
        lines = [f"Updated **{summary}**."]
        for key, value in changes.items():
            lines.append(f"- `{key}`: `{getattr(previous, key)}` → `{value}`")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        await self._announce_change(interaction, summary, list(changes))
