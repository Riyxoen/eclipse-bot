"""AFK system service.

Manages per-user AFK state across guilds. When a user goes AFK:
- Their nickname is changed to "AFK | <original name>"
- The original name is preserved for restoration
- When they send another message, AFK is automatically removed
- When someone mentions an AFK user, a notification is sent

State persists through bot restarts via the afk_state database table.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import discord

from bot.database.migrations import migrate

logger = logging.getLogger("riyxoen.afk")


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
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        migrate(self._connection)

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
            set_at=datetime.fromisoformat(row["set_at"]),
        )

    # ------------------------------------------------------------------- set

    def set_afk(
        self,
        guild_id: int,
        user_id: int,
        original_name: str,
        afk_message: str = "",
    ) -> AfkState:
        """Set a user as AFK. Persists to the database."""
        state = AfkState(
            guild_id=guild_id,
            user_id=user_id,
            original_name=original_name,
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

    # -------------------------------------------------------------- helpers

    async def apply_nickname(self, member: discord.Member, afk_name: str) -> bool:
        """Try to change the member's nickname to the AFK format.

        Returns True if the nickname was changed, False if it failed
        (e.g. role hierarchy prevents the bot from changing it).
        """
        try:
            await member.edit(nick=afk_name)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.info(
                "could not set AFK nickname: guild=%s user=%s",
                getattr(member, "guild", None) and member.guild.id,
                member.id,
            )
            return False

    async def restore_nickname(self, member: discord.Member, original_name: str) -> bool:
        """Try to restore the member's original nickname.

        Returns True if restored, False if it failed.
        """
        try:
            await member.edit(nick=original_name if original_name else None)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.info(
                "could not restore nickname: guild=%s user=%s",
                getattr(member, "guild", None) and member.guild.id,
                member.id,
            )
            return False

    def close(self) -> None:
        """Release database resources."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
