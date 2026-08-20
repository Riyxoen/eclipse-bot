"""Centralized exemption checks for automated moderation.

Exemptions are evaluated once, before any detector runs, so detectors never
duplicate this logic. A message from an exempt author/channel is never
analyzed and never enforced.

Exemptions (in evaluation order):

1. Bot users (and anything without an author).
2. Outside a guild (never analyzed; the engine also early-returns).
3. The server owner.
4. Configured exempt user IDs (per-guild configuration).
5. Configured exempt channel IDs (per-guild configuration).
6. Members holding a configured exempt role (per-guild configuration).
7. Moderators (required Discord permission **or** a configured moderator
   role, via the shared :class:`bot.permissions.checks.PermissionChecker` —
   the same source of truth the commands use; the checker itself consults
   the per-guild configuration for moderator roles).

The engine passes the guild's configuration snapshot in, so this checker
stays pure (no storage access, no Discord I/O) and a ``/config`` exemption
change takes effect immediately.
"""

from __future__ import annotations

from typing import Any

from bot.configuration.guild import GuildConfig
from bot.moderation.actions import ACTION_VIEW_CASES
from bot.permissions.checks import PermissionChecker


class ExemptionChecker:
    """Answers \\"is this message exempt from automated moderation?\\".

    Pure and synchronous: it only reads cached guild/member data (``member.roles``,
    ``guild.owner_id``) and the guild's configuration snapshot.
    """

    def __init__(self, permissions: PermissionChecker) -> None:
        self.permissions = permissions

    def is_exempt(self, message: Any, config: GuildConfig) -> bool:
        """Whether ``message`` is exempt from automated moderation."""
        author = message.author
        if author is None:
            return True
        if getattr(author, "bot", False):
            return True

        guild = message.guild
        if guild is None:
            return True
        if getattr(guild, "owner_id", None) is not None and author.id == guild.owner_id:
            return True

        if author.id in config.exempt_user_ids:
            return True

        channel = getattr(message, "channel", None)
        if channel is not None and getattr(channel, "id", None) in config.exempt_channel_ids:
            return True

        if config.exempt_role_ids and any(
            role.id in config.exempt_role_ids for role in getattr(author, "roles", ())
        ):
            return True

        if self.permissions.is_moderator(author, ACTION_VIEW_CASES):
            return True

        return False
