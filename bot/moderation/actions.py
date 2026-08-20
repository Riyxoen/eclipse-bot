"""Moderation action definitions: names, permission requirements, and labels.

Centralizes the mapping between a moderation action and the Discord
permissions it requires so the permission checker, the moderation service,
and the commands all agree on one source of truth.
"""

from __future__ import annotations

#: Canonical action names (also the values stored in case records).
ACTION_WARN = "warn"
ACTION_TIMEOUT = "timeout"
#: Manual timeout removal (prefix command ``.unmute``). Distinct from
#: ``timeout`` so cases and permissions distinguish the two directions.
ACTION_UNMUTE = "unmute"
ACTION_KICK = "kick"
ACTION_BAN = "ban"
ACTION_UNBAN = "unban"
ACTION_PURGE = "purge"
ACTION_SLOWMODE = "slowmode"
ACTION_LOCK = "lock"
ACTION_UNLOCK = "unlock"
#: Single-message deletion (used by the automated moderation engine).
ACTION_DELETE = "delete"

#: Read-only action: viewing moderation history (no Discord mutation).
ACTION_VIEW_CASES = "view_cases"

ALL_ACTIONS = (
    ACTION_WARN,
    ACTION_TIMEOUT,
    ACTION_UNMUTE,
    ACTION_KICK,
    ACTION_BAN,
    ACTION_UNBAN,
    ACTION_PURGE,
    ACTION_SLOWMODE,
    ACTION_LOCK,
    ACTION_UNLOCK,
    ACTION_DELETE,
)

#: Actions that result in a DM notification to the affected user.
PUNISHMENT_ACTIONS = (ACTION_WARN, ACTION_TIMEOUT, ACTION_UNMUTE, ACTION_KICK, ACTION_BAN)

#: Discord permission attribute names required for each action.
#:
#: - warn/timeout -> ``moderate_members`` (timeouts require it natively; warn
#:   is a bot-side concept with no dedicated Discord permission, so the
#:   closest moderation permission is used).
#: - kick -> ``kick_members``, ban/unban -> ``ban_members``.
#: - purge -> ``manage_messages`` (bot additionally needs
#:   ``read_message_history`` on the channel, checked separately).
ACTION_REQUIRED_PERMISSIONS: dict[str, tuple[str, ...]] = {
    ACTION_WARN: ("moderate_members",),
    ACTION_TIMEOUT: ("moderate_members",),
    ACTION_UNMUTE: ("moderate_members",),
    ACTION_KICK: ("kick_members",),
    ACTION_BAN: ("ban_members",),
    ACTION_UNBAN: ("ban_members",),
    ACTION_PURGE: ("manage_messages",),
    ACTION_DELETE: ("manage_messages",),
    # Channel-management actions (Phase 6): slowmode, lock, and unlock modify
    # a channel's settings/overwrites, so they require Manage Channels.
    ACTION_SLOWMODE: ("manage_channels",),
    ACTION_LOCK: ("manage_channels",),
    ACTION_UNLOCK: ("manage_channels",),
    # Viewing moderation history requires moderation permissions (there is no
    # dedicated Discord permission for it; ``moderate_members`` is the closest
    # fit). Configured moderator roles are also honored by the checker.
    ACTION_VIEW_CASES: ("moderate_members",),
}

#: Actions whose required permissions are evaluated against a specific
#: channel (via ``channel.permissions_for``) rather than the guild-wide set.
#: Purge and single-message deletion are channel-scoped by nature.
CHANNEL_SCOPED_PERMISSIONS: dict[str, tuple[str, ...]] = {
    ACTION_PURGE: ("manage_messages", "read_message_history"),
    ACTION_DELETE: ("manage_messages",),
    ACTION_SLOWMODE: ("manage_channels",),
    ACTION_LOCK: ("manage_channels",),
    ACTION_UNLOCK: ("manage_channels",),
}

#: Human-readable Discord permission names (discord.py 2.7 does not ship a
#: public attribute -> display-name map; keep one here for safe messages).
_PERMISSION_DISPLAY_NAMES: dict[str, str] = {
    "moderate_members": "Moderate Members",
    "kick_members": "Kick Members",
    "ban_members": "Ban Members",
    "manage_messages": "Manage Messages",
    "read_message_history": "Read Message History",
    "manage_channels": "Manage Channels",
}


def permission_display_name(attribute: str) -> str:
    """Return the human-readable name of a Discord permission attribute."""
    return _PERMISSION_DISPLAY_NAMES.get(attribute, attribute.replace("_", " ").title())
