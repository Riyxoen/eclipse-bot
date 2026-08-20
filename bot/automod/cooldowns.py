"""Enforcement cooldowns for automated moderation.

Prevents repeated automated punishment for the same event (e.g. a spam burst
that keeps producing messages after the first batch is deleted): once a
``(guild_id, user_id)`` key is enforced, further detections for that key are
skipped until the cooldown expires.

Memory behavior is bounded: at most ``max_entries`` keys are tracked, expired
entries are dropped on access and on insertion overflow (oldest-inserted
first). ``cooldown_seconds`` of 0 disables cooldowns entirely (start becomes
a no-op).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from bot.moderation.cases import utc_now

#: Upper bound on tracked cooldown keys (defense against unbounded growth).
MAX_COOLDOWN_ENTRIES = 10_000


class CooldownTracker:
    """Tracks ``(guild_id, user_id) -> expiry`` pairs, bounded and self-cleaning."""

    def __init__(
        self,
        *,
        max_entries: int = MAX_COOLDOWN_ENTRIES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.max_entries = max(max_entries, 1)
        self._clock = clock or utc_now
        self._expiries: dict[tuple[int, int], datetime] = {}

    def __len__(self) -> int:
        return len(self._expiries)

    def start(self, key: tuple[int, int], cooldown_seconds: int) -> None:
        """Arm the cooldown for ``key`` (no-op when ``cooldown_seconds <= 0``)."""
        if cooldown_seconds <= 0:
            return
        self._expiries[key] = self._clock() + timedelta(seconds=cooldown_seconds)
        if len(self._expiries) > self.max_entries:
            self._evict_expired_or_oldest()

    def is_active(self, key: tuple[int, int]) -> bool:
        """Whether ``key`` is still within its enforcement cooldown."""
        expiry = self._expiries.get(key)
        if expiry is None:
            return False
        if self._clock() < expiry:
            return True
        del self._expiries[key]
        return False

    def prune(self) -> None:
        """Drop all expired entries (called periodically by the engine)."""
        now = self._clock()
        expired = [key for key, expiry in self._expiries.items() if expiry <= now]
        for key in expired:
            del self._expiries[key]

    def _evict_expired_or_oldest(self) -> None:
        self.prune()
        while len(self._expiries) > self.max_entries:
            oldest = next(iter(self._expiries))
            del self._expiries[oldest]
