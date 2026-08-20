"""Centralized permission and hierarchy checks for moderation actions.

Every moderation action passes through :class:`PermissionChecker` before any
Discord mutation happens. The checks run server-side inside the application —
the bot never relies only on a command's visible Discord permission gate.

Checks performed (in order) for member-targeting actions:

1. The action runs inside a guild (``require_guild``).
2. The moderator has the required Discord permission **or** a configured
   moderator role (``require_moderator``).
3. The bot has the required Discord permission(s) (``require_bot_permissions``).
4. The target exists and is not the bot itself or the server owner
   (``require_target``).
5. The moderator's highest role outranks the target's, and the bot's highest
   role outranks the target's (``require_hierarchy``).
6. The action is valid for the target's current state (``require_state``).

Unban targets a user ID rather than a guild member, so hierarchy/target
checks that only make sense for members are skipped; ban-state validity is
enforced by Discord and mapped to a safe error by the moderation service.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from bot.configuration.settings import Settings
from bot.moderation.actions import (
    ACTION_REQUIRED_PERMISSIONS,
    CHANNEL_SCOPED_PERMISSIONS,
)
from bot.moderation.errors import (
    HierarchyError,
    InvalidTargetError,
    MissingAdministratorPermissionError,
    MissingBotPermissionError,
    MissingModeratorPermissionError,
    NotInGuildError,
)

logger = logging.getLogger("riyxoen.permissions")


def _is_member_like(target: Any) -> bool:
    """True for real ``discord.Member`` instances or member-shaped fakes."""
    return isinstance(target, discord.Member) or (
        hasattr(target, "top_role") and hasattr(target, "guild")
    )


class PermissionChecker:
    """Server-side permission, target, and hierarchy validation.

    ``bot`` is only used to identify the bot's own user (for the
    "target is not the bot" rule) and to look the bot up inside a guild.
    """

    def __init__(
        self,
        bot: discord.Client,
        settings: Settings,
        *,
        config_service: Any = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        #: Phase 5: when present, per-guild configuration (moderator/admin
        #: roles, max purge) is authoritative over the environment defaults.
        self.config_service = config_service

    # ------------------------------------------------------------------ guild

    def require_guild(self, guild: discord.Guild | None) -> None:
        """Raise :class:`NotInGuildError` unless the action runs inside a guild."""
        if guild is None:
            raise NotInGuildError()

    # -------------------------------------------------------------- moderator

    def is_moderator(self, member: discord.Member | None, action: str) -> bool:
        """Whether ``member`` may perform ``action`` (permission or role).

        Non-raising form of :meth:`require_moderator`; used by the automated
        moderation engine's exemption checks so the exemption logic and the
        command permission checks share one source of truth. Moderator roles
        come from the guild's configuration when available, falling back to
        the environment defaults.
        """
        if member is None:
            return False
        required = ACTION_REQUIRED_PERMISSIONS[action]
        granted = member.guild_permissions
        if any(getattr(granted, permission, False) for permission in required):
            return True
        role_ids = self._moderator_role_ids(getattr(member, "guild", None))
        if role_ids and any(role.id in role_ids for role in member.roles):
            return True
        return False

    # ---------------------------------------------------------- administrator

    def is_administrator(self, member: discord.Member | None) -> bool:
        """Whether ``member`` may change server configuration.

        The server owner and members with the Discord ``administrator``
        permission always qualify; members holding a configured administrator
        role qualify too. This is the Phase 5 admin gate — a regular
        moderator never inherits it automatically.
        """
        if member is None:
            return False
        guild = getattr(member, "guild", None)
        if guild is not None:
            owner_id = getattr(guild, "owner_id", None)
            if owner_id is not None and member.id == owner_id:
                return True
        if getattr(member.guild_permissions, "administrator", False):
            return True
        role_ids = self._administrator_role_ids(guild)
        return bool(role_ids and any(role.id in role_ids for role in member.roles))

    def require_administrator(self, member: discord.Member | None, guild: discord.Guild) -> None:
        """Verify ``member`` may change server configuration (server-side).

        Raises :class:`MissingAdministratorPermissionError` with a safe
        message when the member is not the owner, lacks the Discord
        ``administrator`` permission, and holds no configured admin role.
        """
        self.require_guild(guild)
        if not self.is_administrator(member):
            raise MissingAdministratorPermissionError()

    # ------------------------------------------------------------ role lookup

    def _moderator_role_ids(self, guild: Any) -> tuple[int, ...]:
        """Per-guild moderator roles, falling back to the environment."""
        if self.config_service is not None and guild is not None:
            try:
                return self.config_service.get(guild.id).moderator_role_ids
            except Exception:  # noqa: BLE001 - config must never break permissions
                logger.exception(
                    "could not load per-guild moderator roles for guild %s; using env defaults",
                    getattr(guild, "id", None),
                )
        return self.settings.moderator_role_ids

    def _administrator_role_ids(self, guild: Any) -> tuple[int, ...]:
        """Per-guild administrator roles (environment has no admin roles)."""
        if self.config_service is not None and guild is not None:
            try:
                return self.config_service.get(guild.id).administrator_role_ids
            except Exception:  # noqa: BLE001 - config must never break permissions
                logger.exception(
                    "could not load per-guild administrator roles for guild %s",
                    getattr(guild, "id", None),
                )
        return ()

    def require_moderator(self, member: discord.Member | None, action: str) -> None:
        """Verify the moderator holds the required permission or a moderator role.

        Discord permissions are checked at the guild level
        (``member.guild_permissions``); a member with any configured moderator
        role is allowed regardless of permissions. The denial message is the
        short, standardized Phase 6 wording.
        """
        if member is None:
            raise NotInGuildError()
        if self.is_moderator(member, action):
            return
        raise MissingModeratorPermissionError()

    # ------------------------------------------------------- custom roles

    def require_manage_roles(self, member: discord.Member | None, guild: discord.Guild) -> None:
        """Verify ``member`` may manage the custom-role system (Phase 6).

        Custom-role commands (``.el enable/rename/color``) modify roles, so
        the actor needs the Discord ``manage_roles`` permission, or
        Administrator / a configured administrator role (which Discord
        treats as having every permission). A regular moderator never gains
        role-management powers automatically. Server-side only.
        """
        self.require_guild(guild)
        if member is None:
            raise MissingModeratorPermissionError()
        if getattr(member.guild_permissions, "manage_roles", False):
            return
        if getattr(member.guild_permissions, "administrator", False):
            return
        if member.id == getattr(guild, "owner_id", None):
            return
        role_ids = self._administrator_role_ids(guild)
        if role_ids and any(role.id in role_ids for role in member.roles):
            return
        raise MissingModeratorPermissionError(
            "You need the Manage Roles permission (or Administrator) to manage custom roles."
        )

    # ------------------------------------------------------------------- bot

    async def require_bot_permissions(
        self,
        guild: discord.Guild,
        action: str,
        channel: discord.abc.GuildChannel | None = None,
    ) -> None:
        """Verify the bot has the Discord permissions required for ``action``.

        Purge permissions are channel-scoped (``manage_messages`` +
        ``read_message_history`` on the channel); every other action is
        guild-scoped.
        """
        bot_member = await self._get_bot_member(guild)
        if channel is not None and action in CHANNEL_SCOPED_PERMISSIONS:
            granted = channel.permissions_for(bot_member)
            required = CHANNEL_SCOPED_PERMISSIONS[action]
        else:
            granted = bot_member.guild_permissions
            required = ACTION_REQUIRED_PERMISSIONS[action]
        missing = [permission for permission in required if not getattr(granted, permission, False)]
        if missing:
            raise MissingBotPermissionError()

    async def require_bot_manage_roles(self, guild: discord.Guild) -> None:
        """Verify the bot holds the Discord ``manage_roles`` permission.

        Used by the custom-role system (Phase 6), which edits roles rather
        than performing a named moderation action. Raises
        :class:`MissingBotPermissionError` when the bot cannot manage roles.
        """
        bot_member = await self._get_bot_member(guild)
        if not getattr(bot_member.guild_permissions, "manage_roles", False):
            raise MissingBotPermissionError(
                "I need the Manage Roles permission to manage custom roles."
            )

    async def _get_bot_member(self, guild: discord.Guild) -> discord.Member:
        """Return the bot's member object in ``guild``, raising a safe error if absent."""
        bot_member = None
        try:
            bot_member = guild.me
        except AttributeError:
            bot_member = None
        if bot_member is None and self.bot.user is not None:
            bot_member = guild.get_member(self.bot.user.id)
        if bot_member is None:
            bot_member = await guild.fetch_member(self.bot.user.id)
        if bot_member is None:
            raise MissingBotPermissionError("The bot could not be found in this server.")
        return bot_member

    # ----------------------------------------------------------------- target

    def require_target(
        self,
        target: Any,
        guild: discord.Guild,
        moderator: discord.Member,
        *,
        member_required: bool,
    ) -> None:
        """Validate the target exists and is touchable by moderation.

        ``member_required`` selects the stricter rules for actions that need a
        guild member (warn/timeout/kick/ban): the target must be a member, not
        the bot, not the server owner, and not the moderator themself.
        """
        if target is None:
            raise InvalidTargetError()
        if member_required:
            if not _is_member_like(target):
                raise InvalidTargetError("That user isn't a member of this server.")
            if self.bot.user is not None and target.id == self.bot.user.id:
                raise InvalidTargetError("You can't moderate the bot itself.")
            if target.id == guild.owner_id:
                raise InvalidTargetError("The server owner can't be moderated.")
            if moderator is not None and target.id == moderator.id:
                raise InvalidTargetError("You can't moderate yourself.")

    # ------------------------------------------------------------- hierarchy

    async def require_hierarchy(
        self,
        moderator: discord.Member,
        target: discord.Member,
        guild: discord.Guild,
    ) -> None:
        """Verify role hierarchy for member-targeting actions.

        Both the moderator's highest role and the bot's highest role must be
        strictly higher than the target's highest role (checks 7 and 8 of the
        permission model). Discord would refuse these anyway; we validate
        server-side so users get a clear message instead of a generic error.
        """
        bot_member = await self._get_bot_member(guild)
        if moderator.top_role.position <= target.top_role.position:
            raise HierarchyError(
                "I cannot moderate this user because their role is higher than or equal to mine."
            )
        if bot_member.top_role.position <= target.top_role.position:
            raise HierarchyError(
                "I cannot moderate this user because their role is higher than or equal "
                "to the bot's role."
            )

    # ---------------------------------------------------------------- state

    def require_state(
        self,
        action: str,
        *,
        duration_seconds: int | None = None,
        purge_amount: int | None = None,
        guild: discord.Guild | None = None,
    ) -> None:
        """Validate that the action is applicable given the target's state.

        Purge amounts are validated against the configured maximum here
        (defense in depth; the command layer validates first) — the guild's
        per-guild maximum when a config service is available, otherwise the
        environment default. Timeout durations were validated by the command
        layer as well.
        """
        if action == "purge" and purge_amount is not None:
            from bot.moderation.validation import validate_purge_amount

            validate_purge_amount(purge_amount, self._max_purge_amount(guild))
        if action == "timeout" and duration_seconds is not None and duration_seconds < 1:
            from bot.moderation.errors import InvalidDurationError

            raise InvalidDurationError()

    def _max_purge_amount(self, guild: discord.Guild | None) -> int:
        """Per-guild maximum purge amount, falling back to the environment."""
        if self.config_service is not None and guild is not None:
            try:
                return self.config_service.get(guild.id).max_purge_amount
            except Exception:  # noqa: BLE001 - config must never break moderation
                logger.exception(
                    "could not load per-guild max purge amount for guild %s; using env default",
                    guild.id,
                )
        return self.settings.max_purge_amount
