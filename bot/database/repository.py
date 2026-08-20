"""Case repository: the only place that touches case persistence.

:class:`CaseRepository` is the abstraction the case service depends on.
:class:`SQLiteCaseRepository` is the local implementation (standard library
``sqlite3``); :class:`MemoryCaseRepository` is an in-memory fallback used
when the database cannot be opened, so the bot stays functional without
persistence.

Rules enforced here:

- **Parameterized SQL only** — no query is ever built with string
  interpolation of user input.
- **Guild isolation** — every read/update takes ``guild_id`` and filters by
  it; a case can never be reached through another guild's ID.
- **UTC timestamps** — ``created_at``/``expires_at`` are stored as ISO-8601
  UTC text and round-tripped as tz-aware datetimes.
- **No secrets** — the database stores case metadata only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bot.database.migrations import migrate
from bot.moderation.cases import STATUS_SUCCESS, CaseRecord

logger = logging.getLogger("riyxoen.database")

_SELECT_COLUMNS = (
    "case_id, guild_id, target_user_id, moderator_user_id, action, reason,"
    " duration_seconds, expires_at, status, error, created_at, automated, detector,"
    " metadata"
)


def _parse_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_metadata(value: str | None) -> dict | None:
    """Deserialize the optional JSON metadata column (``None`` when unset)."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        logger.warning("case metadata is corrupted; ignoring")
        return None
    return parsed if isinstance(parsed, dict) else None


def _record_to_row(record: CaseRecord) -> tuple[Any, ...]:
    return (
        record.guild_id,
        record.target_user_id,
        record.moderator_user_id,
        record.action,
        record.reason,
        record.duration_seconds,
        record.expires_at.isoformat() if record.expires_at is not None else None,
        record.status,
        record.error,
        record.created_at.isoformat(),
        int(record.automated),
        record.detector,
        json.dumps(record.metadata) if record.metadata else None,
    )


def _row_to_record(row: sqlite3.Row | tuple[Any, ...]) -> CaseRecord:
    return CaseRecord(
        case_id=row[0],
        guild_id=row[1],
        target_user_id=row[2],
        moderator_user_id=row[3],
        action=row[4],
        reason=row[5],
        duration_seconds=row[6],
        expires_at=_parse_datetime(row[7]),
        status=row[8],
        error=row[9],
        created_at=_parse_datetime(row[10]),
        automated=bool(row[11]),
        detector=row[12],
        metadata=_parse_metadata(row[13]),
    )


class CaseRepository(ABC):
    """Storage abstraction for case records. All reads are guild-scoped."""

    @abstractmethod
    def create(self, record: CaseRecord) -> CaseRecord:
        """Persist ``record`` and return it with its assigned ``case_id``."""

    @abstractmethod
    def get(self, guild_id: int, case_id: int) -> CaseRecord | None:
        """Return the case for ``guild_id``/``case_id``, or ``None``."""

    @abstractmethod
    def list_for_guild(
        self, guild_id: int, *, limit: int, offset: int
    ) -> tuple[list[CaseRecord], int]:
        """Return ``(cases, total)`` for ``guild_id``, newest first."""

    @abstractmethod
    def list_for_member(
        self, guild_id: int, target_user_id: int, *, limit: int, offset: int
    ) -> tuple[list[CaseRecord], int]:
        """Return ``(cases, total)`` for a member in a guild, newest first."""

    @abstractmethod
    def update_status(self, guild_id: int, case_id: int, status: str) -> CaseRecord | None:
        """Set a case's status; returns the updated case or ``None``."""

    @abstractmethod
    def update_metadata(
        self, guild_id: int, case_id: int, metadata: dict | None
    ) -> CaseRecord | None:
        """Set a case's optional metadata (JSON); returns the updated case or ``None``."""

    @abstractmethod
    def count_warnings(self, guild_id: int, target_user_id: int, since: datetime | None) -> int:
        """Count successful warn cases for a member, optionally since a time.

        ``since is None`` means no time bound (every stored warning counts).
        Used by the automated-moderation escalation policy.
        """

    @abstractmethod
    def close(self) -> None:
        """Release resources held by the repository."""


class SQLiteCaseRepository(CaseRepository):
    """SQLite-backed case repository (standard library only)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        migrate(self._connection)

    def create(self, record: CaseRecord) -> CaseRecord:
        cursor = self._connection.execute(
            "INSERT INTO cases (guild_id, target_user_id, moderator_user_id, action,"
            " reason, duration_seconds, expires_at, status, error, created_at,"
            " automated, detector, metadata)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _record_to_row(record),
        )
        self._connection.commit()
        return CaseRecord(
            case_id=cursor.lastrowid,
            guild_id=record.guild_id,
            target_user_id=record.target_user_id,
            moderator_user_id=record.moderator_user_id,
            action=record.action,
            reason=record.reason,
            duration_seconds=record.duration_seconds,
            expires_at=record.expires_at,
            status=record.status,
            error=record.error,
            created_at=record.created_at,
            automated=record.automated,
            detector=record.detector,
            metadata=record.metadata,
        )

    def get(self, guild_id: int, case_id: int) -> CaseRecord | None:
        row = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM cases WHERE guild_id = ? AND case_id = ?",
            (guild_id, case_id),
        ).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_for_guild(
        self, guild_id: int, *, limit: int, offset: int
    ) -> tuple[list[CaseRecord], int]:
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM cases WHERE guild_id = ?"
            " ORDER BY created_at DESC, case_id DESC LIMIT ? OFFSET ?",
            (guild_id, limit, offset),
        ).fetchall()
        (total,) = self._connection.execute(
            "SELECT COUNT(*) FROM cases WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return [_row_to_record(row) for row in rows], int(total)

    def list_for_member(
        self, guild_id: int, target_user_id: int, *, limit: int, offset: int
    ) -> tuple[list[CaseRecord], int]:
        rows = self._connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM cases"
            " WHERE guild_id = ? AND target_user_id = ?"
            " ORDER BY created_at DESC, case_id DESC LIMIT ? OFFSET ?",
            (guild_id, target_user_id, limit, offset),
        ).fetchall()
        (total,) = self._connection.execute(
            "SELECT COUNT(*) FROM cases WHERE guild_id = ? AND target_user_id = ?",
            (guild_id, target_user_id),
        ).fetchone()
        return [_row_to_record(row) for row in rows], int(total)

    def update_status(self, guild_id: int, case_id: int, status: str) -> CaseRecord | None:
        cursor = self._connection.execute(
            "UPDATE cases SET status = ? WHERE guild_id = ? AND case_id = ?",
            (status, guild_id, case_id),
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get(guild_id, case_id)

    def update_metadata(
        self, guild_id: int, case_id: int, metadata: dict | None
    ) -> CaseRecord | None:
        cursor = self._connection.execute(
            "UPDATE cases SET metadata = ? WHERE guild_id = ? AND case_id = ?",
            (json.dumps(metadata) if metadata else None, guild_id, case_id),
        )
        self._connection.commit()
        if cursor.rowcount == 0:
            return None
        return self.get(guild_id, case_id)

    def count_warnings(self, guild_id: int, target_user_id: int, since: datetime | None) -> int:
        if since is None:
            (count,) = self._connection.execute(
                "SELECT COUNT(*) FROM cases WHERE guild_id = ? AND target_user_id = ?"
                " AND action = 'warn' AND status = 'success'",
                (guild_id, target_user_id),
            ).fetchone()
        else:
            (count,) = self._connection.execute(
                "SELECT COUNT(*) FROM cases WHERE guild_id = ? AND target_user_id = ?"
                " AND action = 'warn' AND status = 'success' AND created_at >= ?",
                (guild_id, target_user_id, since.isoformat()),
            ).fetchone()
        return int(count)

    def close(self) -> None:
        self._connection.close()


class MemoryCaseRepository(CaseRepository):
    """In-memory fallback repository (cases do not survive a restart)."""

    def __init__(self) -> None:
        self._records: dict[int, CaseRecord] = {}
        self._next_id = 1

    def create(self, record: CaseRecord) -> CaseRecord:
        stored = CaseRecord(
            case_id=self._next_id,
            guild_id=record.guild_id,
            target_user_id=record.target_user_id,
            moderator_user_id=record.moderator_user_id,
            action=record.action,
            reason=record.reason,
            duration_seconds=record.duration_seconds,
            expires_at=record.expires_at,
            status=record.status,
            error=record.error,
            created_at=record.created_at,
            automated=record.automated,
            detector=record.detector,
            metadata=record.metadata,
        )
        self._records[stored.case_id] = stored
        self._next_id += 1
        return stored

    @staticmethod
    def _sort_key(record: CaseRecord) -> tuple[datetime, int]:
        return record.created_at, record.case_id or 0

    def get(self, guild_id: int, case_id: int) -> CaseRecord | None:
        record = self._records.get(case_id)
        if record is None or record.guild_id != guild_id:
            return None
        return record

    def list_for_guild(
        self, guild_id: int, *, limit: int, offset: int
    ) -> tuple[list[CaseRecord], int]:
        matching = sorted(
            (record for record in self._records.values() if record.guild_id == guild_id),
            key=self._sort_key,
            reverse=True,
        )
        return matching[offset : offset + limit], len(matching)

    def list_for_member(
        self, guild_id: int, target_user_id: int, *, limit: int, offset: int
    ) -> tuple[list[CaseRecord], int]:
        matching = sorted(
            (
                record
                for record in self._records.values()
                if record.guild_id == guild_id and record.target_user_id == target_user_id
            ),
            key=self._sort_key,
            reverse=True,
        )
        return matching[offset : offset + limit], len(matching)

    def update_status(self, guild_id: int, case_id: int, status: str) -> CaseRecord | None:
        record = self._records.get(case_id)
        if record is None or record.guild_id != guild_id:
            return None
        updated = CaseRecord(
            case_id=record.case_id,
            guild_id=record.guild_id,
            target_user_id=record.target_user_id,
            moderator_user_id=record.moderator_user_id,
            action=record.action,
            reason=record.reason,
            duration_seconds=record.duration_seconds,
            expires_at=record.expires_at,
            status=status,
            error=record.error,
            created_at=record.created_at,
            automated=record.automated,
            detector=record.detector,
            metadata=record.metadata,
        )
        self._records[case_id] = updated
        return updated

    def update_metadata(
        self, guild_id: int, case_id: int, metadata: dict | None
    ) -> CaseRecord | None:
        record = self._records.get(case_id)
        if record is None or record.guild_id != guild_id:
            return None
        updated = CaseRecord(
            case_id=record.case_id,
            guild_id=record.guild_id,
            target_user_id=record.target_user_id,
            moderator_user_id=record.moderator_user_id,
            action=record.action,
            reason=record.reason,
            duration_seconds=record.duration_seconds,
            expires_at=record.expires_at,
            status=record.status,
            error=record.error,
            created_at=record.created_at,
            automated=record.automated,
            detector=record.detector,
            metadata=metadata,
        )
        self._records[case_id] = updated
        return updated

    def count_warnings(self, guild_id: int, target_user_id: int, since: datetime | None) -> int:
        count = 0
        for record in self._records.values():
            if record.guild_id != guild_id or record.target_user_id != target_user_id:
                continue
            if record.action != "warn" or record.status != STATUS_SUCCESS:
                continue
            if since is not None and record.created_at < since:
                continue
            count += 1
        return count

    def close(self) -> None:
        self._records.clear()


def build_case_repository(path: Path | None) -> CaseRepository:
    """Return the SQLite repository for ``path`` (or memory when ``path`` is ``None``).

    Raises :class:`sqlite3.Error` / :class:`OSError` when the database cannot
    be opened — callers that want graceful degradation catch these and fall
    back to :class:`MemoryCaseRepository`.
    """
    if path is None:
        return MemoryCaseRepository()
    return SQLiteCaseRepository(path)
