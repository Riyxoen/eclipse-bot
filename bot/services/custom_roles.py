"""Custom-role system service (Phase 6: ``.el`` prefix commands).

Commands stay thin and delegate here. The service owns the full pipeline for
the bot-managed custom role:

    guild configuration -> permission checks -> role lookup/create -> Discord edit -> config save

Behavior contract:

- ``.el enable`` — enables the system for the guild, creating the managed
  role if it does not exist yet (never duplicates: an existing stored role
  ID is reused; a deleted stored role is recreated once). Idempotent: a
  second ``.el enable`` replies clearly instead of creating a duplicate.
- ``.el rename`` / ``.el color`` — require the system to be enabled first
  (``CustomRoleDisabledError`` with a clear message otherwise), locate the
  managed role, and edit it.
- Permission safety — every command requires the actor to hold the Discord
  ``manage_roles`` permission (or Administrator / a configured
  administrator role; ``PermissionChecker.require_manage_roles``), the bot
  to hold ``manage_roles``, and never attempts to modify a role above the
  bot's highest role. All of this is validated server-side, never trusted
  from the message.
- Failure handling — Discord API errors are logged with safe, non-private
  detail and surfaced as safe user messages; nothing internal reaches the
  user.

The enabled state and the managed role ID are stored per guild in the
existing configuration system (SQLite), so the state survives restarts.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from bot.configuration.guild import GuildConfig
from bot.configuration.settings import Settings
from bot.moderation.errors import (
    CustomRoleDisabledError,
    MissingBotPermissionError,
    ModerationError,
)
from bot.moderation.validation import validate_hex_color, validate_role_name
from bot.permissions.checks import PermissionChecker
from bot.services.guild_config import GuildConfigService

logger = logging.getLogger("riyxoen.custom_roles")

#: Default name for the bot-managed custom role (created by ``.el enable``).
DEFAULT_ROLE_NAME = "Eclipse Custom"

#: Default color for the bot-managed custom role (Blurple; created by
#: ``.el enable`` when the role does not exist yet).
DEFAULT_ROLE_COLOR = 0x5865F2


class CustomRoleService:
    """Enables, locates, renames, and recolors the bot-managed custom role."""

    def __init__(
        self,
        settings: Settings,
        permissions: PermissionChecker,
        config_service: GuildConfigService,
    ) -> None:
        self.settings = settings
        self.permissions = permissions
        self.config_service = config_service

    # ------------------------------------------------------------------ get

    def is_enabled(self, guild_id: int) -> bool:
        """Whether the custom-role system is enabled for ``guild_id``."""
        return self.config_service.get(guild_id).custom_roles_enabled

    def managed_role(self, guild: discord.Guild) -> discord.Role | None:
        """The bot-managed role for ``guild``, or ``None`` when unset/deleted."""
        config = self.config_service.get(guild.id)
        role_id = config.custom_role_id
        if role_id is None:
            return None
        return guild.get_role(role_id)

    def _config(self, guild: discord.Guild) -> GuildConfig:
        return self.config_service.get(guild.id)

    # --------------------------------------------------------------- enable

    async def enable(self, guild: discord.Guild, actor: discord.Member) -> bool:
        """Enable the custom-role system for ``guild`` (idempotent).

        Returns ``True`` when this call enabled the system (creating the
        managed role if needed — never a duplicate), ``False`` when it was
        already enabled (the caller reports that clearly). The actor needs
        Manage Roles (or Administrator); the bot needs Manage Roles too.
        """
        self.permissions.require_guild(guild)
        self.permissions.require_manage_roles(actor, guild)
        await self.permissions.require_bot_manage_roles(guild)

        config = self._config(guild)
        role = self._locate_role(guild)
        if role is None and config.custom_roles_enabled:
            # The managed role was deleted behind the bot's back: recreate it
            # (``.el enable`` is the documented recovery path).
            role = await self._create_role(guild, config)
            self.config_service.update(
                guild.id,
                actor_user_id=actor.id,
                changes={"custom_role_id": role.id},
            )
            logger.info(
                "custom role recreated after deletion: guild=%s actor=%s role=%s",
                guild.id,
                actor.id,
                role.id,
            )
            return True
        if config.custom_roles_enabled:
            return False  # already enabled — caller reports the clear message

        if role is None:
            role = await self._create_role(guild, config)

        self.config_service.update(
            guild.id,
            actor_user_id=actor.id,
            changes={"custom_roles_enabled": True, "custom_role_id": role.id},
        )
        logger.info(
            "custom roles enabled: guild=%s actor=%s role=%s",
            guild.id,
            actor.id,
            role.id,
        )
        return True

    # -------------------------------------------------------------- rename

    async def rename(self, guild: discord.Guild, actor: discord.Member, name: str) -> GuildConfig:
        """Rename the managed role (requires the system to be enabled)."""
        self.permissions.require_guild(guild)
        self.permissions.require_manage_roles(actor, guild)
        cleaned = validate_role_name(name)
        config = self._require_enabled(guild)
        role = self._require_role(guild)
        await self._edit(guild, role, actor, name=cleaned)
        return config

    # --------------------------------------------------------------- color

    async def color(
        self, guild: discord.Guild, actor: discord.Member, hex_color: str
    ) -> GuildConfig:
        """Change the managed role's color (requires the system to be enabled)."""
        self.permissions.require_guild(guild)
        self.permissions.require_manage_roles(actor, guild)
        normalized = validate_hex_color(hex_color)
        config = self._require_enabled(guild)
        role = self._require_role(guild)
        await self._edit(guild, role, actor, color=int(normalized, 16))
        return config

    # ------------------------------------------------------------ internals

    def _require_enabled(self, guild: discord.Guild) -> GuildConfig:
        """Raise :class:`CustomRoleDisabledError` when the system is disabled."""
        config = self._config(guild)
        if not config.custom_roles_enabled:
            raise CustomRoleDisabledError()
        return config

    def _require_role(self, guild: discord.Guild) -> discord.Role:
        """The managed role, raising a safe error when it is missing/deleted.

        The stored role ID is authoritative; a deleted role is reported
        clearly (re-enable recreates it) rather than guessed at.
        """
        role = self.managed_role(guild)
        if role is None:
            raise CustomRoleDisabledError(
                "The managed custom role is missing (it may have been deleted). "
                "Run `.el enable` again to recreate it."
            )
        return role

    def _locate_role(self, guild: discord.Guild) -> discord.Role | None:
        """The existing managed role (stored ID), or ``None`` when unset/deleted."""
        role = self.managed_role(guild)
        if role is None:
            return None
        return role

    async def _create_role(self, guild: discord.Guild, config: GuildConfig) -> discord.Role:
        """Create the managed role with the configured (or default) styling.

        The role is created below the bot's highest role automatically
        (Discord places new roles below the creator's highest role), so the
        bot can always edit it afterwards.
        """
        try:
            return await guild.create_role(
                name=DEFAULT_ROLE_NAME,
                colour=DEFAULT_ROLE_COLOR,
                reason="Custom-role system enabled",
            )
        except discord.Forbidden:
            raise MissingBotPermissionError(
                "I need the Manage Roles permission to create the custom role."
            ) from None
        except discord.HTTPException:
            logger.info(
                "custom role creation rejected by discord: guild=%s",
                guild.id,
            )
            raise ModerationError(
                "The custom role could not be created by Discord. Please try again."
            ) from None

    async def _edit(
        self,
        guild: discord.Guild,
        role: discord.Role,
        actor: discord.Member,
        *,
        name: str | None = None,
        color: int | None = None,
    ) -> None:
        """Apply a rename/color edit with permission + hierarchy safety."""
        await self.permissions.require_bot_manage_roles(guild)
        bot_highest = await self._bot_highest_position(guild)
        if role.position >= bot_highest:
            raise MissingBotPermissionError(
                "I cannot edit that role — it is at or above my highest role."
            )
        changes: dict[str, Any] = {}
        if name is not None:
            changes["name"] = name
        if color is not None:
            changes["colour"] = color
        try:
            await role.edit(**changes)
        except discord.Forbidden:
            raise MissingBotPermissionError(
                "I need the Manage Roles permission to edit the custom role."
            ) from None
        except discord.HTTPException:
            logger.info(
                "custom role edit rejected by discord: guild=%s role=%s",
                guild.id,
                role.id,
            )
            raise ModerationError(
                "The custom role could not be edited by Discord. Please try again."
            ) from None
        logger.info(
            "custom role edited: guild=%s actor=%s role=%s changes=%s",
            guild.id,
            actor.id,
            role.id,
            sorted(changes),
        )

    async def _bot_highest_position(self, guild: discord.Guild) -> int:
        """The bot's highest role position in ``guild`` (0 when unknown)."""
        bot_member = await self.permissions._get_bot_member(guild)  # noqa: SLF001 - shared helper
        return getattr(getattr(bot_member, "top_role", None), "position", 0)
