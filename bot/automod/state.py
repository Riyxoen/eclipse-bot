"""Bounded in-memory state for automated moderation.

Memory safety contract (documented per the Phase 4 spec):

- **Bounded per user** — each ``(guild, user)`` key holds at most
  ``per_user`` entries (a fixed ``deque(maxlen=...)``).
- **Bounded globally** — at most ``max_users`` keys are tracked. When a new
  key arrives at capacity, the oldest-inserted key is evicted.
- **Stale cleanup** — entries older than the configured window are pruned on
  access, and empty keys are dropped. Detectors expose a ``prune(now)`` sweep
  that the engine runs periodically, so even idle keys cannot accumulate.

No unbounded maps, no infinite histories: total memory is
``O(max_users * per_user)`` regardless of how long the bot runs or how many
users it sees.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import datetime
from typing import Any


class BoundedUserHistory:
    """Per-``(guild_id, user_id)`` bounded history of ``(item, timestamp)``."""

    def __init__(self, *, max_users: int, per_user: int) -> None:
        self.max_users = max(max_users, 1)
        self.per_user = max(per_user, 1)
        self._data: dict[tuple[int, int], deque[tuple[Any, datetime]]] = {}

    def __len__(self) -> int:
        return len(self._data)

    def add(self, key: tuple[int, int], item: Any, timestamp: datetime) -> None:
        """Record ``item`` at ``timestamp`` under ``key`` (bounded)."""
        entries = self._data.get(key)
        if entries is None:
            if len(self._data) >= self.max_users:
                self._evict_oldest()
            entries = deque(maxlen=self.per_user)
            self._data[key] = entries
        entries.append((item, timestamp))

    def snapshot(self, key: tuple[int, int]) -> list[tuple[Any, datetime]]:
        """Return a copy of the entries for ``key`` (newest last)."""
        entries = self._data.get(key)
        if entries is None:
            return []
        return list(entries)

    def prune(self, key: tuple[int, int], cutoff: datetime) -> None:
        """Drop entries for ``key`` older than ``cutoff``; drop empty keys."""
        entries = self._data.get(key)
        if entries is None:
            return
        while entries and entries[0][1] < cutoff:
            entries.popleft()
        if not entries:
            del self._data[key]

    def prune_all(self, cutoff: datetime) -> int:
        """Sweep every key, dropping entries older than ``cutoff``.

        Returns the number of keys removed. Called periodically by the engine
        so stale keys are reclaimed even when never accessed again.
        """
        removed = 0
        for key in list(self._data):
            self.prune(key, cutoff)
            if key not in self._data:
                removed += 1
        return removed

    def _evict_oldest(self) -> None:
        # dict preserves insertion order: pop the oldest-inserted key.
        oldest = next(iter(self._data))
        del self._data[oldest]


def flatten_entries(
    snapshot: Iterable[tuple[Any, datetime]],
) -> tuple[list[Any], list[datetime]]:
    """Split a ``BoundedUserHistory`` snapshot into items and timestamps."""
    items: list[Any] = []
    timestamps: list[datetime] = []
    for item, timestamp in snapshot:
        items.append(item)
        timestamps.append(timestamp)
    return items, timestamps
