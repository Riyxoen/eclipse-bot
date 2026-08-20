"""Best-effort DM notifications for punished users.

A failed DM must never fail the moderation action itself: every failure is
logged (with safe, non-private detail) and swallowed.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from bot.moderation.validation import humanize_duration

logger = logging.getLogger("riyxoen.notifications")

#: Actions that warrant a DM to the affected user.
_NOTIFIABLE_ACTIONS = ("warn", "timeout", "kick", "ban")


def build_punishment_dm(
    action: str,
    reason: str,
    guild_name: str,
    *,
    case_id: int | None = None,
    duration_seconds: int | None = None,
) -> str:
    """Build the DM text for a punishment. Contains no sensitive information."""
    lines = [f"You were **{action}** in **{guild_name}**."]
    if duration_seconds is not None:
        lines.append(f"Duration: {humanize_duration(duration_seconds)}")
    if reason:
        lines.append(f"**Reason:** {reason}")
    if case_id is not None:
        lines.append(f"Case #{case_id}")
    return "\n".join(lines)


class NotificationService:
    """Sends best-effort DM notifications; never raises into the caller."""

    def __init__(self, bot: discord.Client, *, enabled: bool = True) -> None:
        self.bot = bot
        self.enabled = enabled

    async def notify_punishment(
        self,
        target: Any,
        action: str,
        reason: str,
        guild_name: str,
        *,
        case_id: int | None = None,
        duration_seconds: int | None = None,
    ) -> bool:
        """Attempt to DM ``target`` about a punishment.

        Returns ``True`` when the DM was sent. All failures (DMs closed,
        user blocking the bot, network errors, ...) are logged and swallowed;
        the moderation action itself is unaffected.
        """
        if not self.enabled:
            return False
        if target is None or getattr(target, "bot", False):
            return False
        try:
            await target.send(
                build_punishment_dm(
                    action,
                    reason,
                    guild_name,
                    case_id=case_id,
                    duration_seconds=duration_seconds,
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001 - every failure mode is non-fatal
            logger.info(
                "dm notification failed: case=%s action=%s guild=%s target=%s error=%s",
                case_id,
                action,
                getattr(getattr(target, "guild", None), "id", None),
                getattr(target, "id", None),
                type(exc).__name__,
            )
            return False
