"""Domain errors for per-guild configuration (Phase 5)."""

from __future__ import annotations

from bot.core.errors import BotError


class GuildConfigError(BotError):
    """A server configuration change was invalid or could not be persisted.

    ``user_message`` is the sanitized text shown to the Discord user; it must
    never contain exception internals, secrets, or database details. Full
    diagnostics belong in the local logs (see the configuration service).
    """

    user_message = "That configuration change could not be made."

    def __init__(self, user_message: str | None = None) -> None:
        message = user_message or self.user_message
        super().__init__(message)
        self.user_message = message
