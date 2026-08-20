"""Case service: the only way moderation logic and commands touch cases.

Commands call this service (never SQLite directly). The service depends on
the :class:`bot.database.repository.CaseRepository` abstraction, so a future
database backend can be swapped in without touching commands or the
moderation service.

Guards enforced here:

- **Guild isolation** — every read/update requires ``guild_id``; a case can
  never be fetched, listed, or mutated through another guild's ID. A
  mismatched or missing case surfaces as ``None`` ("Case not found."), never
  as an error that leaks the case's existence.
- **Validated identifiers** — ``case_id`` < 1 and ``page``/``limit`` misuse
  are rejected before touching storage.
- **Bounded reads** — list limits are clamped so no caller can request an
  unbounded result set (pagination).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from bot.database.repository import CaseRepository
from bot.moderation.cases import VALID_STATUSES, CaseRecord

#: Upper bound for any single list request (defense against unbounded reads).
MAX_LIST_LIMIT = 50


@dataclass(frozen=True, slots=True)
class CasePage:
    """A page of case records plus pagination metadata."""

    items: list[CaseRecord]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        return max(1, (self.total + self.page_size - 1) // self.page_size)

    @property
    def has_more(self) -> bool:
        return self.page * self.page_size < self.total


class CaseService:
    """Validates, paginates, and enforces ownership for case records."""

    def __init__(self, repository: CaseRepository) -> None:
        self.repository = repository

    # ---------------------------------------------------------------- create

    def create(self, record: CaseRecord) -> CaseRecord:
        """Persist ``record`` and return it with its assigned ``case_id``.

        ``status`` must be a known value (misuse is a programming error).
        """
        self._validate_status(record.status)
        return self.repository.create(record)

    # ------------------------------------------------------------------- get

    def get(self, guild_id: int, case_id: int) -> CaseRecord | None:
        """Return the case for ``guild_id``/``case_id``, or ``None``.

        The case must belong to ``guild_id`` (strict isolation); a case that
        exists in another guild is indistinguishable from a missing case.
        """
        if case_id < 1:
            return None
        return self.repository.get(guild_id, case_id)

    # ----------------------------------------------------------------- lists

    def list_for_guild(self, guild_id: int, *, page: int = 1, page_size: int = 10) -> CasePage:
        """Return a page of the guild's cases, newest first."""
        limit, offset, page_number = self._page_bounds(page, page_size)
        items, total = self.repository.list_for_guild(guild_id, limit=limit, offset=offset)
        return CasePage(items=items, total=total, page=page_number, page_size=limit)

    def list_for_member(
        self, guild_id: int, target_user_id: int, *, page: int = 1, page_size: int = 10
    ) -> CasePage:
        """Return a page of a member's cases in the guild, newest first."""
        limit, offset, page_number = self._page_bounds(page, page_size)
        items, total = self.repository.list_for_member(
            guild_id, target_user_id, limit=limit, offset=offset
        )
        return CasePage(items=items, total=total, page=page_number, page_size=limit)

    # ------------------------------------------------------------- warnings

    def count_active_warnings(
        self, guild_id: int, target_user_id: int, since: datetime | None
    ) -> int:
        """Count a member's successful warnings in a guild (escalation input).

                ``since`` is the warning-expiration cutoff: only warnings created at or
        after ``since`` count as active. ``None`` means no expiration (every stored
        warning counts). The automated-moderation escalation policy uses this to
        decide whether a warning should become a timeout.
        """
        if since is not None and since.tzinfo is None:
            raise ValueError("since must be timezone-aware (UTC)")
        return self.repository.count_warnings(guild_id, target_user_id, since)

    # --------------------------------------------------------------- updates

    def update_status(self, guild_id: int, case_id: int, status: str) -> CaseRecord | None:
        """Set a case's status (guild-scoped); returns the updated case or ``None``.

        ``None`` means the case does not exist in this guild. Reserved for
        future features (e.g. appeals/resolutions); no command uses it yet.
        """
        self._validate_status(status)
        if case_id < 1:
            return None
        return self.repository.update_status(guild_id, case_id, status)

    def update_metadata(
        self, guild_id: int, case_id: int, metadata: dict | None
    ) -> CaseRecord | None:
        """Set a case's optional metadata (guild-scoped); returns the updated case.

        ``None`` means the case does not exist in this guild. Used to record
        delivery status and other action-specific state on an existing case
        (e.g. ``dm_delivered`` after a punishment's DM attempt).
        """
        if case_id < 1:
            return None
        return self.repository.update_metadata(guild_id, case_id, metadata)

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(f"invalid case status: {status!r}")

    @staticmethod
    def _page_bounds(page: int, page_size: int) -> tuple[int, int, int]:
        page_number = max(int(page), 1)
        size = min(max(int(page_size), 1), MAX_LIST_LIMIT)
        return size, (page_number - 1) * size, page_number
