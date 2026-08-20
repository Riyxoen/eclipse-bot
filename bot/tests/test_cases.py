"""Tests for the CaseRecord domain model."""

from __future__ import annotations

from datetime import UTC, timedelta

from bot.moderation.cases import (
    STATUS_CLEARED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    VALID_STATUSES,
    CaseRecord,
    utc_now,
)


def _record(**overrides) -> CaseRecord:
    values: dict = {
        "guild_id": 10,
        "target_user_id": 20,
        "moderator_user_id": 30,
        "action": "warn",
        "reason": "spam",
        "created_at": utc_now(),
        "status": STATUS_SUCCESS,
    }
    values.update(overrides)
    return CaseRecord(**values)


def test_case_record_defaults() -> None:
    record = _record()
    assert record.case_id is None
    assert record.duration_seconds is None
    assert record.expires_at is None
    assert record.error is None
    assert record.status == STATUS_SUCCESS
    assert record.automated is False
    assert record.detector is None


def test_success_property_reflects_status() -> None:
    assert _record(status=STATUS_SUCCESS).success is True
    assert _record(status=STATUS_FAILED).success is False


def test_identity_uses_user_ids_not_usernames() -> None:
    record = _record(target_user_id=123, moderator_user_id=456)
    assert record.target_user_id == 123
    assert record.moderator_user_id == 456
    assert not hasattr(record, "target_username")


def test_expires_at_stored_for_timeouts() -> None:
    created = utc_now()
    record = _record(
        action="timeout", duration_seconds=300, expires_at=created + timedelta(seconds=300)
    )
    assert record.expires_at == created + timedelta(seconds=300)


def test_created_at_is_utc_aware() -> None:
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(None)


def test_valid_statuses_contains_all_states() -> None:
    assert set(VALID_STATUSES) == {STATUS_SUCCESS, STATUS_FAILED, STATUS_CLEARED}


def test_failed_case_carries_safe_error() -> None:
    record = _record(status=STATUS_FAILED, error="You can't moderate the bot itself.")
    assert record.success is False
    assert record.error == "You can't moderate the bot itself."


def test_automated_case_carries_detector() -> None:
    record = _record(automated=True, detector="spam")
    assert record.automated is True
    assert record.detector == "spam"


def test_manual_case_has_no_detector() -> None:
    record = _record()
    assert record.automated is False
    assert record.detector is None
