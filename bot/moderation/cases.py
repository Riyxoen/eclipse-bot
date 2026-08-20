"""Moderation case model.

One :class:`CaseRecord` is produced per moderation attempt and persisted by
the database layer (:mod:`bot.database.repository`). This module defines the
domain model only — no SQL here.

Identity is always a Discord **user ID**, never a username. Timestamps are
UTC. ``status`` is the action outcome: ``"success"`` or ``"failed"``.
``expires_at`` is set for timeouts (``created_at + duration``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

#: Case status values.
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
#: Warnings that were cleared by a moderator (``.clearwarnings``, Phase 10).
#: Cleared warnings keep their history but no longer count as active toward
#: the automated-moderation escalation policy.
STATUS_CLEARED = "cleared"

#: All valid status values.
VALID_STATUSES = (STATUS_SUCCESS, STATUS_FAILED, STATUS_CLEARED)


def utc_now() -> datetime:
    """Current UTC time (overridable in tests via monkeypatch)."""
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class CaseRecord:
    """A single moderation attempt and its outcome.

    ``case_id`` is ``None`` until the record has been persisted (the
    repository assigns it). ``duration_seconds`` and ``expires_at`` are set
    for timeouts; ``error`` holds a safe, user-facing error string for failed
    attempts. ``automated``/``detector`` mark actions taken by the automated
    moderation engine (Phase 4) rather than by a human moderator.
    """

    guild_id: int
    target_user_id: int
    moderator_user_id: int
    action: str
    reason: str
    created_at: datetime
    status: str
    case_id: int | None = None
    duration_seconds: int | None = None
    expires_at: datetime | None = None
    error: str | None = None
    #: True when the case was created by the automated moderation engine.
    automated: bool = False
    #: Canonical detector name that triggered the action (``None`` for manual).
    detector: str | None = None
    #: Optional action-specific metadata (JSON in storage). Used by channel
    #: actions (Phase 6): ``lock`` records the pre-lock overwrite state so
    #: ``unlock`` can restore it exactly. Never contains secrets.
    metadata: dict | None = None

    @property
    def success(self) -> bool:
        """Whether the action completed (convenience over ``status``)."""
        return self.status == STATUS_SUCCESS
