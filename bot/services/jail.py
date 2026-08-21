"""Jail system service.

Manages the per-guild jail: applying a jail role, stripping previous
roles, and restoring them on release. The jail configuration (role ID,
channel ID) lives in GuildConfig; previous roles are stored in the
case metadata for auditability.

Pipeline:
    guild config -> permission checks -> role manipulation -> case record

Safety:
- Previous roles are always stored before removal.
- Deleted roles are skipped on restore (never crash).
- Roles higher than the bot's highest role are never removed.
- The bot's own role is never removed.
- Jail/unjail produce moderation cases for full audit trail.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from bot.configuration.guild import GuildConfig
from bot.moderation.errors import (
    AlreadyJailedError,
    JailNotConfiguredError,
    MissingBotPermissionError,
    ModerationError,
    NotJailedError,
)
from bot.permissions.checks import PermissionChecker
from bot.services.guild_config import GuildConfigService

logger = logging.getLogger("riyxoen.jail")


class JailService:
    """Manages the jail system: setup, jail, and unjail operations."""

    def __init__(
        self,
        permissions: PermissionChecker,
        config_service: GuildConfigService,
    ) -> None:
        self.permissions = permissions
        self.config_service = config_service

    # ---------------------------------------------------------------- config

    def _config(self, guild_id: int) -> GuildConfig:
        return self.config_service.get(guild_id)

    def is_configured(self, guild_id: int) -> bool:
        """Whether the jail system is configured for this guild."""
        config = self._config(guild_id)
        return config.jail_role_id is not None

    def get_jail_role(self, guild: discord.Guild) -> discord.Role | None:
        """The configured jail role, or None if not set or deleted."""
        config = self._config(guild.id)
        if config.jail_role_id is None:
            return None
        return guild.get_role(config.jail_role_id)

    def get_jail_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """The configured jail channel, or None if not set or deleted."""
        config = self._config(guild.id)
        if config.jail_channel_id is None:
            return None
        channel = guild.get_channel(config.jail_channel_id)
        return channel if isinstance(channel, discord.TextChannel) else None

    # ----------------------------------------------------------------- setup

    async def setup(
        self,
        guild: discord.Guild,
        actor: discord.Member,
        jail_role: discord.Role | None = None,
        jail_channel: discord.TextChannel | None = None,
    ) -> GuildConfig:
        """Configure the jail system for this guild.

        If jail_role/jail_channel are not provided, creates/uses defaults.
        Requires Administrator permission.
        """
        self.permissions.require_guild(guild)
        self.permissions.require_administrator(actor, guild)
        await self.permissions.require_bot_manage_roles(guild)

        changes: dict[str, Any] = {}

        # Jail role
        if jail_role is None:
            # Create a new jail role
            try:
                jail_role = await guild.create_role(
                    name="Jailed",
                    color=0x8B0000,
                    reason="Jail system setup",
                )
                # Move it below the bot's highest role
                bot_member = await self.permissions._get_bot_member(guild)
                if bot_member.top_role.position > 1:
                    try:
                        await jail_role.edit(position=bot_member.top_role.position - 1)
                    except (discord.Forbidden, discord.HTTPException):
                        pass
            except discord.Forbidden:
                raise MissingBotPermissionError(
                    "I need the Manage Roles permission to create the jail role."
                ) from None
            except discord.HTTPException:
                raise ModerationError(
                    "The jail role could be created by Discord. Please try again."
                ) from None
        changes["jail_role_id"] = jail_role.id

        # Jail channel
        if jail_channel is not None:
            changes["jail_channel_id"] = jail_channel.id
        else:
            # Try to create a jail channel
            try:
                default_role = guild.default_role
                jail_channel = await guild.create_text_channel(
                    "jail",
                    reason="Jail system setup",
                    overwrites={
                        default_role: discord.PermissionOverwrite(read_messages=False),
                    },
                )
                # Allow the jail role to see the channel
                await jail_channel.set_permissions(
                    jail_role,
                    read_messages=True,
                    send_messages=True,
                    reason="Jail system setup",
                )
                # Allow the bot to see the channel
                bot_member = await self.permissions._get_bot_member(guild)
                await jail_channel.set_permissions(
                    bot_member,
                    read_messages=True,
                    send_messages=True,
                    reason="Jail system setup",
                )
                changes["jail_channel_id"] = jail_channel.id
            except (discord.Forbidden, discord.HTTPException):
                # Channel creation failed, but role was created — still usable
                logger.info("could not create jail channel: guild=%s", guild.id)

        return self.config_service.update(
            guild.id,
            actor_user_id=actor.id,
            changes=changes,
        )

    # ----------------------------------------------------------------- jail

    async def jail(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        reason: str,
    ) -> list[discord.Role]:
        """Jail a member: save their roles, strip them, apply jail role.

        Returns the list of previous role IDs that were removed (stored
        in the returned metadata for case records).

        Raises:
            JailNotConfiguredError: if the jail system isn't set up
            AlreadyJailedError: if the user is already jailed
        """
        self.permissions.require_guild(guild)

        config = self._config(guild.id)
        if config.jail_role_id is None:
            raise JailNotConfiguredError()

        jail_role = self.get_jail_role(guild)
        if jail_role is None:
            raise JailNotConfiguredError(
                "The jail role has been deleted. An administrator must run `·jail setup` again."
            )

        # Check if already jailed
        if jail_role in target.roles:
            raise AlreadyJailedError()

        # Save previous roles (exclude @everyone and roles higher than bot's)
        bot_member = await self.permissions._get_bot_member(guild)
        bot_highest_pos = getattr(getattr(bot_member, "top_role", None), "position", 0)

        previous_role_ids = []
        roles_to_remove = []
        for role in target.roles:
            # Never remove @everyone
            if role.id == guild.default_role.id:
                continue
            # Never remove roles at or above the bot's highest role
            if role.position >= bot_highest_pos:
                continue
            # Never remove the bot's own role
            if role == getattr(bot_member, "top_role", None):
                continue
            previous_role_ids.append(role.id)
            roles_to_remove.append(role)

        # Remove old roles
        if roles_to_remove:
            try:
                await target.remove_roles(
                    *roles_to_remove,
                    reason=f"Jailed by {moderator.display_name}: {reason}",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise ModerationError(
                    "Failed to remove roles from the jailed user."
                ) from exc

        # Add jail role
        try:
            await target.add_roles(
                jail_role,
                reason=f"Jailed by {moderator.display_name}: {reason}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            # Try to restore roles if adding jail role fails
            if roles_to_remove:
                try:
                    await target.add_roles(*roles_to_remove, reason="Jail failed, restoring roles")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            raise ModerationError(
                "Failed to apply the jail role."
            ) from exc

        # Move to jail channel if configured
        if config.jail_channel_id is not None:
            jail_channel = self.get_jail_channel(guild)
            if jail_channel is not None:
                try:
                    # Allow the jailed user to see the jail channel
                    await jail_channel.set_permissions(
                        target,
                        read_messages=True,
                        send_messages=True,
                        reason=f"Jailed: {reason}",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.info(
                        "could not set jail channel permissions: guild=%s user=%s",
                        guild.id,
                        target.id,
                    )

        return previous_role_ids

    # ---------------------------------------------------------------- unjail

    async def unjail(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        previous_role_ids: list[int],
    ) -> None:
        """Release a member from jail: remove jail role, restore previous roles.

        Roles that no longer exist or are above the bot are safely skipped.
        """
        self.permissions.require_guild(guild)

        config = self._config(guild.id)
        if config.jail_role_id is None:
            raise JailNotConfiguredError()

        jail_role = self.get_jail_role(guild)
        if jail_role is None:
            raise JailNotConfiguredError(
                "The jail role has been deleted."
            )

        # Check if actually jailed
        if jail_role not in target.roles:
            raise NotJailedError()

        # Remove jail role
        try:
            await target.remove_roles(
                jail_role,
                reason=f"Unjailed by {moderator.display_name}",
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise ModerationError(
                "Failed to remove the jail role."
            ) from exc

        # Restore previous roles (skip deleted/higher roles)
        bot_member = await self.permissions._get_bot_member(guild)
        bot_highest_pos = getattr(getattr(bot_member, "top_role", None), "position", 0)

        roles_to_restore = []
        for role_id in previous_role_ids:
            role = guild.get_role(role_id)
            if role is None:
                # Role was deleted — skip silently
                continue
            if role.position >= bot_highest_pos:
                # Role is at or above bot's highest — skip
                continue
            roles_to_restore.append(role)

        if roles_to_restore:
            try:
                await target.add_roles(
                    *roles_to_restore,
                    reason=f"Unjailed by {moderator.display_name}",
                )
            except (discord.Forbidden, discord.HTTPException):
                logger.info(
                    "could not restore all roles during unjail: guild=%s user=%s",
                    guild.id,
                    target.id,
                )

        # Remove jail channel permissions
        if config.jail_channel_id is not None:
            jail_channel = self.get_jail_channel(guild)
            if jail_channel is not None:
                try:
                    await jail_channel.set_permissions(
                        target,
                        overwrite=None,
                        reason="Unjailed: removing jail channel access",
                    )
                except (discord.Forbidden, discord.HTTPException):
                    pass

    # ----------------------------------------------------------- helpers

    def find_jail_case_metadata(self, guild_id: int, user_id: int, case_service: Any) -> dict | None:
        """Find the most recent jail case for a user to retrieve previous roles."""
        page = case_service.list_for_member(guild_id, user_id, page_size=20)
        for record in page.items:
            if record.action == "jail" and record.status == "success":
                return record.metadata
        return None
