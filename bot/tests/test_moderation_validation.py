"""Tests for argument validation: reasons, durations, purge amounts."""

from __future__ import annotations

import pytest
from bot.moderation.errors import InvalidDurationError, InvalidPurgeAmountError, InvalidReasonError
from bot.moderation.validation import (
    MAX_REASON_LENGTH,
    MAX_TIMEOUT_SECONDS,
    humanize_duration,
    parse_duration,
    validate_purge_amount,
    validate_reason,
)

# ---------------------------------------------------------------- reasons


def test_reason_required() -> None:
    with pytest.raises(InvalidReasonError):
        validate_reason("")


def test_reason_required_whitespace_only() -> None:
    with pytest.raises(InvalidReasonError):
        validate_reason("   \n\t ")


def test_reason_none_rejected() -> None:
    with pytest.raises(InvalidReasonError):
        validate_reason(None)  # type: ignore[arg-type]


def test_reason_stripped() -> None:
    assert validate_reason("  spam  ") == "spam"


def test_reason_too_long_rejected() -> None:
    with pytest.raises(InvalidReasonError, match="300"):
        validate_reason("x" * (MAX_REASON_LENGTH + 1))


def test_reason_at_max_length_accepted() -> None:
    assert len(validate_reason("x" * MAX_REASON_LENGTH)) == MAX_REASON_LENGTH


# --------------------------------------------------------------- durations


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("90", 90),
        ("90s", 90),
        ("5m", 300),
        ("2h", 7200),
        ("3d", 259200),
        ("  1h  ", 3600),
    ],
)
def test_parse_duration_valid(text: str, expected: int) -> None:
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "1.5h", "h", "-5m", "5x", "1h30m"])
def test_parse_duration_invalid(text: str) -> None:
    with pytest.raises(InvalidDurationError):
        parse_duration(text)


def test_parse_duration_zero_rejected() -> None:
    with pytest.raises(InvalidDurationError):
        parse_duration("0s")


def test_parse_duration_over_28_days_rejected() -> None:
    with pytest.raises(InvalidDurationError, match="28 days"):
        parse_duration("29d")


def test_parse_duration_accepts_exactly_28_days() -> None:
    assert parse_duration("28d") == MAX_TIMEOUT_SECONDS


def test_parse_duration_none_rejected() -> None:
    with pytest.raises(InvalidDurationError):
        parse_duration(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------- purge


def test_purge_amount_zero_rejected() -> None:
    with pytest.raises(InvalidPurgeAmountError):
        validate_purge_amount(0, max_amount=100)


def test_purge_amount_negative_rejected() -> None:
    with pytest.raises(InvalidPurgeAmountError):
        validate_purge_amount(-3, max_amount=100)


def test_purge_amount_over_max_rejected() -> None:
    with pytest.raises(InvalidPurgeAmountError, match="100"):
        validate_purge_amount(101, max_amount=100)


def test_purge_amount_none_rejected() -> None:
    with pytest.raises(InvalidPurgeAmountError):
        validate_purge_amount(None, max_amount=100)


def test_purge_amount_at_max_accepted() -> None:
    assert validate_purge_amount(100, max_amount=100) == 100


# ------------------------------------------------------- humanize duration


def test_humanize_duration_units() -> None:
    assert humanize_duration(90) == "90s"
    assert humanize_duration(300) == "5m"
    assert humanize_duration(7200) == "2h"
    assert humanize_duration(259200) == "3d"
