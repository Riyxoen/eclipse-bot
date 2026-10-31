"""AFK system: status service and message-driven behaviour.

The flow (matching the familiar moderation-bot behaviour):

1. **Activation** — ``/afk [reason]`` or ``·afk [reason]`` marks the member
   away and stores an optional reason.
2. **Nickname tag** — the member is renamed to ``[AFK] <name>`` so others can
   see the status before they even message them; the original name is stored so
   it can be restored exactly.
3. **Auto-response** — when another member mentions an AFK user, the bot
   answers in the channel with that user's reason and how long ago they left.
4. **Automatic removal** — the AFK member's next message in that guild clears
   the status, restores the original nickname, and posts a short welcome back.

State persists through restarts via the ``afk_state`` table (migration v5).

Failure handling: every Discord call is best-effort. A nickname the bot cannot
change, a closed DM, a missing channel — all are logged and swallowed, because
an AFK status must never break message handling.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import discord

from bot.database.migrations import migrate
from bot.moderation.cases import utc_now
from bot.utilities.embeds import afk_embed, success_embed

logger = logging.getLogger("riyxoen.afk")

#: Tag applied to an AFK member's nickname (``[AFK] Name``).
AFK_NICK_PREFIX = "[AFK] "
#: Discord's maximum nickname length (characters).
MAX_NICKNAME_LENGTH = 32


# ------------------------------------------------------------------- naming


def build_afk_nickname(display_name: str) -> str:
    """The tagged nickname for an AFK member.

    Any previous tag is stripped first (so tags never nest) and the result is
    truncated to Discord's nickname limit.
    """
    base = strip_afk_nickname(display_name or "")
    return (AFK_NICK_PREFIX + base)[:MAX_NICKNAME_LENGTH]


def is_afk_nickname(nickname: str | None) -> bool:
    """Whether ``nickname`` carries this bot's AFK tag."""
    return bool(nickname) and str(nickname).startswith(AFK_NICK_PREFIX)


def strip_afk_nickname(display_name: str) -> str:
    """Remove every AFK tag from ``display_name`` (idempotent)."""
    text = display_name or ""
    while is_afk_nickname(text):
        text = text[len(AFK_NICK_PREFIX) :]
    return text.strip()


def format_elapsed(delta: timedelta) -> str:
    """Render how long ago something happened ("a moment", "5 minutes", ...)."""
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 15:
        return "a moment"
    if seconds < 60:
        return f"{seconds} seconds"
    minutes = seconds // 60
    if minutes < 60:
        return _plural(minutes, "minute")
    hours = minutes // 60
    if hours < 24:
        return _plural(hours, "hour")
    return _plural(hours // 24, "day")


def _plural(value: int, unit: str) -> str:
    return f"{value} {unit}" if value == 1 else f"{value} {unit}s"


# --------------------------------------------------------------------- state


@dataclass
class AfkState:
    """AFK state for a user in a guild."""

    guild_id: int
    user_id: int
    original_name: str
    afk_message: str
    set_at: datetime


class AfkService:
    """Manages AFK state with database persistence."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path
        self._connection: sqlite3.Connection | None = None
        self._memory: dict[tuple[int, int], AfkState] = {}  # in-memory fallback
        if db_path is not None:
            try:
                self._connect(db_path)
            except (sqlite3.Error, OSError):
                logger.exception("could not open AFK database %s", db_path)
                self._connection = None

    def _connect(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path))
        try:
            connection.row_factory = sqlite3.Row
            migrate(connection)
        except Exception:
            connection.close()
            raise
        self._connection = connection

    # ------------------------------------------------------------------ get

    def get(self, guild_id: int, user_id: int) -> AfkState | None:
        """Return the AFK state for a user, or None if not AFK."""
        if self._connection is None:
            return self._memory.get((guild_id, user_id))
        try:
            row = self._connection.execute(
                "SELECT original_name, afk_message, set_at FROM afk_state"
                " WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
        except sqlite3.Error:
            logger.exception("failed to read AFK state: guild=%s user=%s", guild_id, user_id)
            return None
        if row is None:
            return None
        return AfkState(
            guild_id=guild_id,
            user_id=user_id,
            original_name=row["original_name"],
            afk_message=row["afk_message"],
            set_at=_parse_timestamp(row["set_at"]),
        )

    # ------------------------------------------------------------------- set

    def set_afk(
        self,
        guild_id: int,
        user_id: int,
        original_name: str,
        afk_message: str = "",
    ) -> AfkState:
        """Set (or update) a user's AFK state and persist it.

        ``original_name`` is the member's untagged display name; any AFK tag is
        stripped so re-running the command never nests tags or stores a tagged
        name for the later restore.
        """
        state = AfkState(
            guild_id=guild_id,
            user_id=user_id,
            original_name=strip_afk_nickname(original_name),
            afk_message=afk_message,
            set_at=datetime.now(UTC),
        )
        if self._connection is None:
            self._memory[(guild_id, user_id)] = state
            return state
        try:
            self._connection.execute(
                "INSERT OR REPLACE INTO afk_state"
                " (guild_id, user_id, original_name, afk_message, set_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (guild_id, user_id, original_name, afk_message, state.set_at.isoformat()),
            )
            self._connection.commit()
        except sqlite3.Error:
            logger.exception("failed to persist AFK state: guild=%s user=%s", guild_id, user_id)
        return state

    # ---------------------------------------------------------------- remove

    def remove(self, guild_id: int, user_id: int) -> AfkState | None:
        """Remove a user's AFK state. Returns the removed state, or None."""
        if self._connection is None:
            return self._memory.pop((guild_id, user_id), None)
        state = self.get(guild_id, user_id)
        if state is None:
            return None
        try:
            self._connection.execute(
                "DELETE FROM afk_state WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            self._connection.commit()
        except sqlite3.Error:
            logger.exception("failed to remove AFK state: guild=%s user=%s", guild_id, user_id)
        return state

    # -------------------------------------------------------------- nicknames

    async def apply_nickname(self, member: discord.Member, afk_name: str) -> bool:
        """Try to set the member's AFK nickname.

        Returns True if the nickname was changed, False if it failed (e.g. the
        bot lacks Manage Nicknames or is below the member in the hierarchy).
        """
        try:
            await member.edit(nick=afk_name)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.info(
                "could not set AFK nickname: guild=%s user=%s",
                getattr(getattr(member, "guild", None), "id", None),
                getattr(member, "id", None),
            )
            return False

    async def restore_nickname(self, member: discord.Member, original_name: str) -> bool:
        """Restore the member's original nickname (or clear it).

        A member who had no nickname before going AFK (their display name was
        their username) gets the nickname cleared instead of pinned to their
        username.
        """
        nickname = None
        if original_name and original_name != getattr(member, "name", None):
            nickname = original_name
        try:
            await member.edit(nick=nickname)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.info(
                "could not restore nickname: guild=%s user=%s",
                getattr(getattr(member, "guild", None), "id", None),
                getattr(member, "id", None),
            )
            return False

    def close(self) -> None:
        """Release database resources."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None


# ------------------------------------------------------------------ events


class AfkEventHandler:
    """Message-driven AFK behaviour: activity detection and mention replies.

    Runs on every guild message *before* prefix commands, so sending a command
    also counts as activity. The AFK command itself is exempt (via
    ``status_command``), otherwise re-running ``·afk`` would clear and re-set
    the status in the same message.

    Never raises: AFK handling must not break message processing.
    """

    def __init__(
        self,
        service: AfkService,
        *,
        status_command: Callable[[Any], bool] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.service = service
        self._status_command = status_command
        self._clock = clock or utc_now

    async def handle_message(self, message: Any) -> None:
        """Process one message (activity + mentions); never raises."""
        try:
            await self._handle(message)
        except Exception:  # noqa: BLE001 - must never break message handling
            logger.exception(
                "afk handling failed: guild=%s author=%s",
                getattr(getattr(message, "guild", None), "id", None),
                getattr(getattr(message, "author", None), "id", None),
            )

    async def _handle(self, message: Any) -> None:
        guild = getattr(message, "guild", None)
        author = getattr(message, "author", None)
        if guild is None or author is None or getattr(author, "bot", False):
            return
        if not self._is_status_command(message):
            await self._clear_activity(guild, author, message)
        await self._answer_mentions(guild, author, message)

    def _is_status_command(self, message: Any) -> bool:
        """Whether this message is the AFK command itself."""
        if self._status_command is None:
            return False
        try:
            return bool(self._status_command(message))
        except Exception:  # noqa: BLE001 - classification is best-effort
            logger.debug("could not classify the afk command", exc_info=True)
            return False

    async def _clear_activity(self, guild: Any, author: Any, message: Any) -> None:
        """The author spoke: drop AFK, restore the nickname, say welcome back."""
        state = self.service.remove(guild.id, author.id)
        if state is None:
            return
        if is_afk_nickname(getattr(author, "nick", None)):
            await self.service.restore_nickname(author, state.original_name)
        await self._send(
            message,
            success_embed(
                "Welcome Back!",
                f"Welcome back, {author.mention}! Your AFK status has been removed.",
            ),
        )

    async def _answer_mentions(self, guild: Any, author: Any, message: Any) -> None:
        """Answer a mention of AFK members with their reason and elapsed time."""
        lines: list[str] = []
        now = self._clock()
        for user in getattr(message, "mentions", None) or ():
            if user is None or getattr(user, "bot", False):
                continue
            if getattr(user, "id", None) == getattr(author, "id", None):
                continue
            state = self.service.get(guild.id, user.id)
            if state is None:
                continue
            name = (
                getattr(user, "display_name", None) or getattr(user, "name", None) or str(user.id)
            )
            line = f"**{name}** is AFK"
            if state.afk_message:
                line += f": {state.afk_message}"
            set_at = state.set_at
            if set_at.tzinfo is None:
                set_at = set_at.replace(tzinfo=UTC)
            line += f" — away for {format_elapsed(now - set_at)}."
            lines.append(line)
        if not lines:
            return
        await self._send(message, afk_embed("AFK", "\n".join(lines)))

    @staticmethod
    async def _send(message: Any, embed: discord.Embed) -> None:
        """Best-effort embed to the message's channel."""
        channel = getattr(message, "channel", None)
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except Exception:  # noqa: BLE001 - a notice must never fail the message
            logger.info(
                "could not post an AFK notice: guild=%s",
                getattr(getattr(message, "guild", None), "id", None),
            )


def _parse_timestamp(value: str) -> datetime:
    """Parse a stored ISO timestamp, defaulting to UTC when it is naive."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
