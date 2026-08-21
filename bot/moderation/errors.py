"""Moderation domain exceptions.

Every exception carries a ``user_message`` that is safe to show to Discord
users verbatim — it must never contain exception internals, secrets, or
sensitive details. Full detail belongs in local logs (see the moderation
service's audit logging).
"""

from __future__ import annotations

from bot.core.errors import BotError


class ModerationError(BotError):
    """Base class for all moderation-domain errors.

    ``user_message`` is the sanitized text shown to the user; it is the only
    part of the error that ever reaches Discord.
    """

    user_message: str = "That moderation action could not be completed."

    def __init__(self, user_message: str | None = None) -> None:
        message = user_message or self.user_message
        super().__init__(message)
        self.user_message = message


class NotInGuildError(ModerationError):
    """The command was used outside of a guild (e.g. a DM)."""

    user_message = "This command can only be used in a server."


class MissingModeratorPermissionError(ModerationError):
    """The moderator lacks the required permission and has no moderator role."""

    user_message = "You do not have permission to use this command."


class MissingBotPermissionError(ModerationError):
    """The bot lacks a Discord permission required for the action."""

    user_message = "I do not have permission to perform this action."


class MissingAdministratorPermissionError(ModerationError):
    """Server configuration changes require Administrator (or an admin role).

    Phase 5: configuration is sensitive moderation data. A regular moderator
    must never receive it automatically; only the server owner, members with
    the Discord ``administrator`` permission, or members holding a configured
    administrator role may change it (checked server-side).
    """

    user_message = (
        "You need the Administrator permission or a configured administrator "
        "role to change server configuration."
    )


class InvalidTargetError(ModerationError):
    """The target does not exist, is the bot, the server owner, or otherwise untouchable."""

    user_message = "User not found."


class HierarchyError(ModerationError):
    """The moderator's or the bot's highest role is not above the target's."""

    user_message = "I cannot moderate this user because their role is higher than or equal to mine."


class InvalidReasonError(ModerationError):
    """The reason is missing, empty, or too long."""

    user_message = "A reason is required for this action."


class InvalidDurationError(ModerationError):
    """The timeout duration is missing, unparsable, or outside Discord's limits."""

    user_message = "Duration must be between 1 second and 28 days (e.g. 30m, 2h, 3d)."


class InvalidPurgeAmountError(ModerationError):
    """The purge amount is missing, invalid, or exceeds the configured maximum."""

    user_message = "That purge amount is not allowed."


class InvalidHexColorError(ModerationError):
    """The custom-role color is malformed (not exactly six hex digits)."""

    user_message = "That color isn't valid — use a hex code like #ff0000 or #5865f2."


class InvalidRoleNameError(ModerationError):
    """The role name is missing, too long, or contains forbidden characters."""

    user_message = "That role name isn't valid."


class ModerationExecutionError(ModerationError):
    """The action was rejected by Discord itself (permission race, invalid state, ...).

    The case is still recorded as failed; the user only ever sees the safe
    message while the real reason goes to the logs.
    """

    user_message = (
        "The action could not be completed by Discord. Check the bot's permissions "
        "and that the target is still valid, then try again."
    )


class CasePersistenceError(ModerationError):
    """The Discord action completed but the case record could not be saved.

    This is an explicit, documented inconsistency: the moderation action is
    never rolled back (Discord has no rollback for kick/ban/timeout), and the
    user is told the truth — the action went through but the local record is
    missing. Full diagnostics go to the logs; no SQL error reaches Discord.
    """

    user_message = (
        "The action was completed, but its case record could not be saved to the "
        "local database. Please report this to the server administrators."
    )


class CustomRoleError(ModerationError):
    """Base class for custom-role system failures (Phase 6: prefix commands)."""

    user_message = "The custom-role system could not complete that action."


class CustomRoleDisabledError(CustomRoleError):
    """The custom-role system is not enabled for this guild."""

    user_message = (
        "The custom-role system is disabled in this server. "
        "A moderator or administrator must run `.el enable` first."
    )


class NotTimedOutError(ModerationError):
    """The target is not currently muted/timed out (unmute)."""

    user_message = "That user isn't currently muted."


class JailNotConfiguredError(ModerationError):
    """The jail system is not configured for this guild."""

    user_message = (
        "The jail system is not configured in this server. "
        "An administrator must run `·jail setup` first."
    )


class AlreadyJailedError(ModerationError):
    """The target is already jailed."""

    user_message = "That user is already jailed."


class NotJailedError(ModerationError):
    """The target is not currently jailed."""

    user_message = "That user isn't currently jailed."
