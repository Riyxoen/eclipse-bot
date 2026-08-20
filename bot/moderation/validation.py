"""Argument validation for moderation commands.

Validation is pure (no Discord I/O) so it is trivially unit-testable and
shared by the command layer and the moderation service. Every failure raises
a :class:`bot.moderation.errors.ModerationError` subclass carrying a safe
user-facing message.
"""

from __future__ import annotations

import re

from bot.moderation.errors import (
    InvalidDurationError,
    InvalidHexColorError,
    InvalidPurgeAmountError,
    InvalidReasonError,
    InvalidRoleNameError,
)

#: Maximum length of a moderation reason.
MAX_REASON_LENGTH = 300

#: Discord's maximum timeout duration: 28 days, in seconds.
MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60

#: Discord's hard ceiling for a single bulk-delete request; the configured
#: maximum purge amount is validated against a sane bound as well.
BULK_DELETE_LIMIT = 100

#: Discord's maximum slowmode delay: 6 hours, in seconds.
MAX_SLOWMODE_SECONDS = 6 * 60 * 60

_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION_PATTERN = re.compile(r"^\s*(\d+)\s*([smhd])?\s*$", re.IGNORECASE)


def validate_reason(reason: str) -> str:
    """Validate and normalize a moderation reason.

    Raises :class:`InvalidReasonError` when the reason is missing/blank or
    longer than :data:`MAX_REASON_LENGTH`. Returns the stripped reason.
    """
    if reason is None:
        raise InvalidReasonError()
    cleaned = reason.strip()
    if not cleaned:
        raise InvalidReasonError()
    if len(cleaned) > MAX_REASON_LENGTH:
        raise InvalidReasonError(
            f"Reason must be at most {MAX_REASON_LENGTH} characters (got {len(cleaned)})."
        )
    return cleaned


def parse_duration(text: str) -> int:
    """Parse a timeout duration into seconds.

    Accepts a bare number of seconds (``"90"``) or a number with a unit
    suffix (``"30s"``, ``"5m"``, ``"2h"``, ``"3d"``). Raises
    :class:`InvalidDurationError` when unparsable, zero/negative, or beyond
    Discord's 28-day timeout limit. Returns whole seconds.
    """
    if text is None:
        raise InvalidDurationError()
    match = _DURATION_PATTERN.match(text)
    if match is None:
        raise InvalidDurationError()
    value = int(match.group(1))
    unit = _DURATION_UNITS[(match.group(2) or "s").lower()]
    seconds = value * unit
    if seconds < 1:
        raise InvalidDurationError()
    if seconds > MAX_TIMEOUT_SECONDS:
        raise InvalidDurationError(
            "Duration can't exceed 28 days — Discord timeouts are limited to 28 days."
        )
    return seconds


def validate_purge_amount(amount: int | None, max_amount: int) -> int:
    """Validate a purge message count against the configured maximum.

    Raises :class:`InvalidPurgeAmountError` when the amount is missing,
    below 1, or above ``max_amount``. Returns the validated amount.
    """
    if amount is None or amount < 1:
        raise InvalidPurgeAmountError("Purge amount must be at least 1 message.")
    if amount > max_amount:
        raise InvalidPurgeAmountError(f"Purge amount can't exceed {max_amount} messages per use.")
    return amount


def validate_slowmode_duration(seconds: int | None) -> int:
    """Validate a channel slowmode delay in seconds (0..6 hours).

    ``0`` (or ``None``) clears slowmode. Raises
    :class:`InvalidDurationError` when negative or beyond Discord's 6-hour
    limit. Returns the validated value (``0`` when ``None``).
    """
    value = int(seconds or 0)
    if value < 0 or value > MAX_SLOWMODE_SECONDS:
        raise InvalidDurationError(
            f"Slowmode must be between 0 and {humanize_duration(MAX_SLOWMODE_SECONDS)}."
        )
    return value


#: Discord's maximum role name length (characters).
MAX_ROLE_NAME_LENGTH = 100

#: Characters Discord forbids in role names.
_ROLE_NAME_FORBIDDEN = ("@", "#", ":")

_HEX_COLOR_PATTERN = re.compile(r"^#?([0-9a-fA-F]{6})$")


def validate_hex_color(text: str) -> str:
    """Validate a hex color and return it normalized to ``rrggbb`` (lowercase).

    Accepts ``#ff0000``, ``ff0000``, ``#FF0000`` (case is normalized). Raises
    :class:`InvalidHexColorError` when the value is missing, malformed, or
    not exactly six hex digits. The returned value has no ``#`` prefix so it
    can be passed straight to Discord's ``int(color, 16)``.
    """
    if text is None:
        raise InvalidHexColorError()
    match = _HEX_COLOR_PATTERN.match(text.strip())
    if match is None:
        raise InvalidHexColorError()
    return match.group(1).lower()


def validate_role_name(name: str) -> str:
    """Validate a Discord role name; returns the stripped name.

    Raises :class:`InvalidRoleNameError` when the name is missing, too long
    (Discord caps role names at 100 characters), or contains a character
    Discord forbids (``@``, ``#``, ``:``).
    """
    if name is None:
        raise InvalidRoleNameError()
    cleaned = name.strip()
    if not cleaned:
        raise InvalidRoleNameError()
    if len(cleaned) > MAX_ROLE_NAME_LENGTH:
        raise InvalidRoleNameError(
            f"Role names can't be longer than {MAX_ROLE_NAME_LENGTH} characters "
            f"(got {len(cleaned)})."
        )
    for forbidden in _ROLE_NAME_FORBIDDEN:
        if forbidden in cleaned:
            raise InvalidRoleNameError("Role names can't contain @, #, or : characters.")
    return cleaned


def humanize_duration(seconds: int) -> str:
    """Render a duration in whole seconds as a short human string (e.g. \"2h\")."""
    if seconds % 86400 == 0 and seconds >= 86400:
        days = seconds // 86400
        return f"{days}d"
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours}h"
    if seconds % 60 == 0 and seconds >= 60:
        minutes = seconds // 60
        return f"{minutes}m"
    return f"{seconds}s"
