"""Confirmation controller for dangerous moderation actions (Phase 6).

Dangerous actions (ban, kick, large purge, server lock) are never executed
immediately: the command shows a confirmation prompt with buttons, and the
moderation action only runs after the moderator confirms. This module owns
the *state machine* behind that UX; the ``discord.ui.View`` lives in the
command layer.

Guarantees (per the Phase 6 spec):

- **Expiry** — a confirmation is only valid for :data:`DEFAULT_TIMEOUT_SECONDS`
  (or a per-controller value). Expired confirmations can never be confirmed.
- **Ownership** — only the moderator who initiated the action may confirm or
  cancel it; anyone else gets a safe error.
- **Double-click protection** — :meth:`try_confirm` consumes the confirmation
  **atomically**: the first caller gets ``None`` (proceed) and every later
  caller — including a second click racing the first — gets an error. The
  action can never execute twice.
- **Cancellation** — :meth:`try_cancel` consumes the confirmation too.
- **Bounded memory** — at most :data:`MAX_PENDING` confirmations are tracked;
  stale (expired) entries are dropped on access and on overflow.
- **Never trusts input** — all lookups are keyed by the random confirmation
  key plus guild/user IDs; there is no cross-guild or cross-user path.
"""

from __future__ import annotations

import logging
import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.moderation.cases import utc_now

logger = logging.getLogger("riyxoen.confirmation")

#: Seconds a confirmation stays valid before it expires.
DEFAULT_TIMEOUT_SECONDS = 30

#: Upper bound on tracked confirmations (defense against unbounded growth).
MAX_PENDING = 100


@dataclass(frozen=True, slots=True)
class Confirmation:
    """A pending confirmation for one moderation action."""

    key: str
    guild_id: int
    user_id: int
    summary: str
    expires_at: datetime


class ConfirmationController:
    """Tracks and resolves pending confirmations. Pure and synchronous."""

    def __init__(
        self,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_pending: int = MAX_PENDING,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.timeout_seconds = max(timeout_seconds, 1)
        self.max_pending = max(max_pending, 1)
        self._clock = clock or utc_now
        self._pending: OrderedDict[str, Confirmation] = OrderedDict()

    def __len__(self) -> int:
        return len(self._pending)

    # --------------------------------------------------------------- create

    def create(self, guild_id: int, user_id: int, summary: str) -> Confirmation:
        """Register a new confirmation; returns it (key = random token).

        ``summary`` is a short, safe description shown on the prompt
        (e.g. ``Ban @user``) — never secrets or message contents.
        """
        self._drop_expired()
        key = secrets.token_urlsafe(16)
        confirmation = Confirmation(
            key=key,
            guild_id=guild_id,
            user_id=user_id,
            summary=summary,
            expires_at=self._clock() + timedelta(seconds=self.timeout_seconds),
        )
        self._pending[key] = confirmation
        if len(self._pending) > self.max_pending:
            self._pending.popitem(last=False)
        return confirmation

    # -------------------------------------------------------------- resolve

    def try_confirm(self, key: str, guild_id: int, user_id: int) -> str | None:
        """Try to confirm ``key``.

        Returns ``None`` when the confirmation may proceed — and **consumes
        it**, so a second click can never execute the action again. Returns a
        safe user-facing error message otherwise (not the owner, expired, or
        already used/not found).
        """
        return self._resolve(key, guild_id, user_id, consume=True)

    def try_cancel(self, key: str, guild_id: int, user_id: int) -> str | None:
        """Try to cancel ``key`` (consumes it on success). Same semantics as
        :meth:`try_confirm`."""
        return self._resolve(key, guild_id, user_id, consume=True)

    def _resolve(self, key: str, guild_id: int, user_id: int, *, consume: bool) -> str | None:
        confirmation = self._pending.get(key)
        if confirmation is None:
            return "This confirmation has expired or was already used."
        if confirmation.guild_id != guild_id or confirmation.user_id != user_id:
            return "This confirmation can only be used by the moderator who started it."
        if self._clock() >= confirmation.expires_at:
            self._pending.pop(key, None)
            return "This confirmation has expired or was already used."
        if consume:
            self._pending.pop(key, None)
        return None

    # -------------------------------------------------------------- cleanup

    def _drop_expired(self) -> None:
        now = self._clock()
        expired = [key for key, item in self._pending.items() if now >= item.expires_at]
        for key in expired:
            self._pending.pop(key, None)
