"""Automated-moderation commands: ``/automod`` (Phase 8).

The engine itself is configured per guild through the existing
:class:`bot.services.guild_config.GuildConfigService` (the same source the
``/config`` commands and the engine consume) — this cog is a thin, readable
interface on top of it. No configuration logic is duplicated here; every
command verifies the caller is an administrator through the shared
:class:`bot.permissions.checks.PermissionChecker` (never trusting the
command's visible permission gate), then delegates.

Command surface (kept small and understandable per the spec):

    /automod enable | /automod disable | /automod status
    /automod invites <action> [code]      (allow/delete/warn/timeout)
    /automod invites allowed add|remove|list <code>
    /automod raid <threshold> <window> <action>   (alert/timeout)

Everything else (spam, duplicates, mentions, links, word filter, escalation,
cooldowns, exemptions) is already configurable via ``/config moderation …``;
``/automod status`` summarizes every detector's state so administrators can
see the whole picture in one place.
"""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord import app_commands

from bot.configuration.errors import GuildConfigError
from bot.moderation.errors import ModerationError
from bot.services.guild_config import GuildConfigService

logger = logging.getLogger("riyxoen.automod_cog")

_INVITE_ACTION_DESCRIPTION = "Invite handling (allow, delete, warn, or timeout)."
_RAID_ACTION_DESCRIPTION = "Raid action (alert posts a log notice; timeout is opt-in)."


class AutomodCog(app_commands.Group):
    """Automated-moderation commands (administrators only)."""

    invites = app_commands.Group(name="invites", description="Invite filtering settings.")
    raid = app_commands.Group(name="raid", description="Raid protection settings.")

    def __init__(self, bot: discord.Client, config_service: GuildConfigService) -> None:
        super().__init__(name="automod", description="Automated moderation (administrators).")
        self.bot = bot
        self.config_service = config_service

    # -------------------------------------------------------------- helpers

    def _require_admin(self, interaction: discord.Interaction) -> None:
        """Gate automod configuration behind the shared admin permission layer."""
        permissions = interaction.client.permissions
        permissions.require_guild(interaction.guild)
        permissions.require_administrator(interaction.user, interaction.guild)

    async def _deny(
        self, interaction: discord.Interaction, exc: ModerationError | GuildConfigError
    ) -> None:
        logger.info("automod command denied: %s (user=%s)", exc.user_message, interaction.user.id)
        await interaction.response.send_message(exc.user_message, ephemeral=True)

    async def _apply(
        self, interaction: discord.Interaction, changes: dict, *, summary: str
    ) -> None:
        """Validate via the service and respond with old -> new values."""
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

    # ------------------------------------------------------- master switch

    @app_commands.command(name="enable", description="Enable automated moderation in this server.")
    @app_commands.guild_only()
    async def enable(self, interaction: discord.Interaction) -> None:
        """Turn the per-guild automod master switch on."""
        await self._apply(interaction, {"automod_enabled": True}, summary="automated moderation")

    @app_commands.command(
        name="disable", description="Disable automated moderation in this server."
    )
    @app_commands.guild_only()
    async def disable(self, interaction: discord.Interaction) -> None:
        """Turn the per-guild automod master switch off."""
        await self._apply(interaction, {"automod_enabled": False}, summary="automated moderation")

    @app_commands.command(name="status", description="Show this server's automod status.")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        """Summarize the per-guild automod configuration (admin only)."""
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        config = self.config_service.get(interaction.guild.id)
        lines = [
            f"**Automod: {'enabled' if config.automod_enabled else 'disabled'}**",
            f"- Spam: {config.spam_threshold} msgs / {config.spam_window_seconds}s → `{config.spam_action}`",
            f"- Duplicates: {config.duplicate_threshold} identical / {config.duplicate_window_seconds}s → `{config.duplicate_action}`",
            f"- Mentions: `{config.mention_action}` (user ≥{config.mention_user_threshold}, role ≥{config.mention_role_threshold}, total ≥{config.mention_total_threshold})",
            f"- Links: `{config.link_action}`",
            f"- Invites: `{config.invite_action}`",
            f"- Word filter: `{config.word_filter_action}` (terms: {len(config.blocked_terms)})",
            f"- Raid: `{config.raid_action}` (≥{config.raid_join_threshold} joins / {config.raid_window_seconds}s)",
            f"- Enforcement cooldown: {config.enforcement_cooldown_seconds}s",
            "Change settings with `/automod …` or `/config moderation …`.",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------- invites

    @invites.command(name="action", description="Set the invite-filtering action.")
    @app_commands.describe(action=_INVITE_ACTION_DESCRIPTION)
    async def invites_action(
        self,
        interaction: discord.Interaction,
        action: Literal["allow", "delete", "warn", "timeout"],
    ) -> None:
        """Set what happens when a non-allowlisted invite link is posted."""
        await self._apply(interaction, {"invite_action": action}, summary="invite filtering")

    @invites.command(name="allowed", description="Manage allowlisted invite codes.")
    @app_commands.describe(
        sub="add, remove, or list allowed invite codes.",
        code="The invite code to add/remove (e.g. the part after discord.gg/).",
    )
    async def invites_allowed(
        self,
        interaction: discord.Interaction,
        sub: Literal["add", "remove", "list"],
        code: str | None = None,
    ) -> None:
        """Add, remove, or list the guild's allowed invite codes."""
        try:
            self._require_admin(interaction)
        except ModerationError as exc:
            await self._deny(interaction, exc)
            return
        if sub == "list":
            config = self.config_service.get(interaction.guild.id)
            codes = (
                ", ".join(config.invite_allowed_codes) if config.invite_allowed_codes else "none"
            )
            await interaction.response.send_message(
                f"Allowed invite codes: `{codes}`", ephemeral=True
            )
            return
        if code is None or not code.strip():
            await self._deny(
                interaction, GuildConfigError("An invite code is required for that action.")
            )
            return
        cleaned = code.strip().lower()
        try:
            if sub == "add":
                updated = self.config_service.add_allowed_invite_code(
                    interaction.guild.id, actor_user_id=interaction.user.id, code=cleaned
                )
            else:
                updated = self.config_service.remove_allowed_invite_code(
                    interaction.guild.id, actor_user_id=interaction.user.id, code=cleaned
                )
        except GuildConfigError as exc:
            await self._deny(interaction, exc)
            return
        await interaction.response.send_message(
            f"{'Added' if sub == 'add' else 'Removed'} allowed invite code `{cleaned}` "
            f"({len(updated.invite_allowed_codes)} configured).",
            ephemeral=True,
        )

    # ---------------------------------------------------------------- raid

    @raid.command(name="configure", description="Configure raid protection.")
    @app_commands.describe(
        threshold="Joins within the window that trigger a raid (2-1000).",
        window_seconds="Time window in seconds (1-86400).",
        action=_RAID_ACTION_DESCRIPTION,
    )
    async def raid_configure(
        self,
        interaction: discord.Interaction,
        threshold: int,
        window_seconds: int,
        action: Literal["alert", "timeout"],
    ) -> None:
        """Set the join-burst threshold, window, and action (conservative by default)."""
        await self._apply(
            interaction,
            {
                "raid_join_threshold": threshold,
                "raid_window_seconds": window_seconds,
                "raid_action": action,
            },
            summary="raid protection",
        )
