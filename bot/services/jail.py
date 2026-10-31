"""Jail system service.

Manages the per-guild jail: applying a jail role, stripping previous roles,
and restoring them on release. The jail configuration (role ID, channel ID)
lives in GuildConfig; previous roles are stored in the case metadata for
auditability.

Pipeline (owned by :class:`bot.services.moderation.ModerationService`)::

    permission checks -> plan (read-only) -> Discord mutation -> case record

This module deliberately exposes the jail work as two steps:

- :meth:`JailService.plan_jail` / :meth:`JailService.plan_unjail` are
  **read-only**: they resolve the jail role and compute which roles would be
  removed/restored. Nothing is mutated, so the moderation service can call
  them before (or instead of) enforcing.
- :meth:`JailService.apply_jail` / :meth:`JailService.apply_unjail` perform
  the Discord mutations and are only called once every permission, target,
  hierarchy, and state check has passed.

There is intentionally **no** public ``jail()``/``unjail()`` entry point on
this service: callers must go through
:meth:`bot.services.moderation.ModerationService.jail` so the checks can
never be skipped and every attempt produces a case record.

Safety:

- Previous roles are stored on the case before removal, so ``·unjail`` can
  restore them.
- Deleted roles are skipped on restore (never crash).
- Roles at or above the bot's highest role are never removed.
- The bot's own top role and ``@everyone`` are never removed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class JailPlan:
    """A read-only plan for jailing one member (no Discord mutation).

    ``roles_to_remove`` are the member's roles that the bot may strip (below
    the bot's highest role, excluding ``@everyone``); ``previous_role_ids`` is
    what gets stored on the case so ``·unjail`` can restore them.
    """

    jail_role: discord.Role
    roles_to_remove: tuple[Any, ...]

    @property
    def previous_role_ids(self) -> tuple[int, ...]:
        """IDs of the roles that will be removed (stored on the case)."""
        return tuple(role.id for role in self.roles_to_remove)


def _actor_name(actor: Any) -> str:
    """Safe display name for a moderator (never raises)."""
    name = getattr(actor, "display_name", None) or getattr(actor, "name", None)
    return str(name) if name else str(getattr(actor, "id", "moderator"))


class JailService:
    """Manages the jail system: setup, plan/apply, and release."""

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
                        logger.info(
                            "could not reposition the jail role: guild=%s role=%s",
                            guild.id,
                            jail_role.id,
                        )
            except discord.Forbidden:
                raise MissingBotPermissionError(
                    "I need the Manage Roles permission to create the jail role."
                ) from None
            except discord.HTTPException:
                raise ModerationError(
                    "The jail role could not be created by Discord. Please try again."
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

    # ------------------------------------------------------------ validation

    def check_configured(self, guild: discord.Guild) -> discord.Role:
        """Return the guild's jail role, raising a safe error when unusable.

        Raises :class:`JailNotConfiguredError` when the jail system was never
        set up or when the configured role has been deleted since.
        """
        config = self._config(guild.id)
        if config.jail_role_id is None:
            raise JailNotConfiguredError()
        role = self.get_jail_role(guild)
        if role is None:
            raise JailNotConfiguredError(
                "The jail role has been deleted. An administrator must run `·jail setup` again."
            )
        return role

    def assert_not_jailed(self, guild: discord.Guild, target: discord.Member) -> None:
        """Raise :class:`AlreadyJailedError` when ``target`` is already jailed."""
        jail_role = self.check_configured(guild)
        if jail_role in target.roles:
            raise AlreadyJailedError()

    def assert_jailed(self, guild: discord.Guild, target: discord.Member) -> None:
        """Raise :class:`NotJailedError` when ``target`` is not jailed."""
        jail_role = self.check_configured(guild)
        if jail_role not in target.roles:
            raise NotJailedError()

    # ------------------------------------------------------------------ jail

    async def plan_jail(self, guild: discord.Guild, target: discord.Member) -> JailPlan:
        """Compute what jailing ``target`` would change (read-only).

        Never mutates Discord state, so it is safe to call before the
        permission checks: nothing happens if they then reject the action.
        """
        jail_role = self.check_configured(guild)
        bot_member = await self.permissions._get_bot_member(guild)
        bot_top_role = getattr(bot_member, "top_role", None)
        bot_highest_pos = getattr(bot_top_role, "position", 0)

        roles_to_remove = tuple(
            role
            for role in target.roles
            # Never remove @everyone.
            if role.id != guild.default_role.id
            # Never remove roles at or above the bot's highest role.
            and getattr(role, "position", 0) < bot_highest_pos
            # Never remove the bot's own role.
            and role != bot_top_role
        )
        return JailPlan(jail_role=jail_role, roles_to_remove=roles_to_remove)

    async def apply_jail(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        plan: JailPlan,
        reason: str,
    ) -> None:
        """Strip the planned roles and apply the jail role.

        Only called after every permission, target, hierarchy, and state
        check has passed (see :meth:`bot.services.moderation.ModerationService.jail`).
        """
        audit = f"Jailed by {_actor_name(moderator)}: {reason}"

        if plan.roles_to_remove:
            try:
                await target.remove_roles(*plan.roles_to_remove, reason=audit)
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise ModerationError("Failed to remove roles from the jailed user.") from exc

        try:
            await target.add_roles(plan.jail_role, reason=audit)
        except (discord.Forbidden, discord.HTTPException) as exc:
            # Try to restore roles if adding jail role fails
            if plan.roles_to_remove:
                try:
                    await target.add_roles(
                        *plan.roles_to_remove, reason="Jail failed, restoring roles"
                    )
                except (discord.Forbidden, discord.HTTPException):
                    logger.info(
                        "could not restore roles after a failed jail: guild=%s user=%s",
                        guild.id,
                        getattr(target, "id", None),
                    )
            raise ModerationError("Failed to apply the jail role.") from exc

        await self._open_jail_channel(guild, target, reason)

    async def _open_jail_channel(
        self, guild: discord.Guild, target: discord.Member, reason: str
    ) -> None:
        """Best-effort: let the jailed member see the jail channel."""
        config = self._config(guild.id)
        if config.jail_channel_id is None:
            return
        jail_channel = self.get_jail_channel(guild)
        if jail_channel is None:
            return
        try:
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
                getattr(target, "id", None),
            )

    # ---------------------------------------------------------------- unjail

    async def plan_unjail(
        self,
        guild: discord.Guild,
        target: discord.Member,
        previous_role_ids: list[int],
    ) -> tuple[Any, ...]:
        """Compute which previous roles can be restored (read-only).

        Roles that no longer exist, are at or above the bot's highest role, or
        are the jail role itself are skipped — never an error.
        """
        self.check_configured(guild)
        bot_member = await self.permissions._get_bot_member(guild)
        bot_highest_pos = getattr(getattr(bot_member, "top_role", None), "position", 0)
        jail_role_id = self._config(guild.id).jail_role_id

        restorable: list[Any] = []
        for role_id in previous_role_ids:
            if role_id == jail_role_id:
                continue
            role = guild.get_role(role_id)
            if role is None:
                # Role was deleted — skip silently.
                continue
            if getattr(role, "position", 0) >= bot_highest_pos:
                # Role is at or above the bot's highest — skip.
                continue
            restorable.append(role)
        return tuple(restorable)

    async def apply_unjail(
        self,
        guild: discord.Guild,
        moderator: discord.Member,
        target: discord.Member,
        previous_role_ids: list[int],
    ) -> int:
        """Remove the jail role and restore the member's previous roles.

        Returns the number of roles actually restored. Every step is
        best-effort for the restore (logged, never raised): the member must
        always be released from jail.
        """
        jail_role = self.check_configured(guild)
        audit = f"Unjailed by {_actor_name(moderator)}"

        try:
            await target.remove_roles(jail_role, reason=audit)
        except (discord.Forbidden, discord.HTTPException) as exc:
            raise ModerationError("Failed to remove the jail role.") from exc

        roles_to_restore = await self.plan_unjail(guild, target, previous_role_ids)
        if roles_to_restore:
            try:
                await target.add_roles(*roles_to_restore, reason=audit)
            except (discord.Forbidden, discord.HTTPException):
                roles_to_restore = ()
                logger.info(
                    "could not restore roles during unjail: guild=%s user=%s",
                    guild.id,
                    getattr(target, "id", None),
                )

        config = self._config(guild.id)
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
        return len(roles_to_restore)

    # ----------------------------------------------------------- helpers

    def find_jail_case_metadata(self, guild_id: int, user_id: int, case_service: Any) -> dict | None:
        """Find the most recent jail case for a user to retrieve previous roles."""
        page = case_service.list_for_member(guild_id, user_id, page_size=20)
        for record in page.items:
            if record.action == "jail" and record.status == "success":
                return record.metadata
        return None
