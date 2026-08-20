"""Tests for user-facing case formatting (detail and paginated list views)."""

from __future__ import annotations

from datetime import timedelta

from bot.moderation.cases import STATUS_FAILED, STATUS_SUCCESS, CaseRecord, utc_now
from bot.moderation.response import format_case_detail, format_case_list, format_case_response
from bot.services.cases import CasePage


def _record(**overrides) -> CaseRecord:
    values: dict = {
        "case_id": 42,
        "guild_id": 10,
        "target_user_id": 3,
        "moderator_user_id": 2,
        "action": "timeout",
        "reason": "spam",
        "created_at": utc_now(),
        "status": STATUS_SUCCESS,
        "duration_seconds": 300,
        "expires_at": utc_now() + timedelta(seconds=300),
    }
    values.update(overrides)
    return CaseRecord(**values)


def test_format_case_response_contains_expected_fields() -> None:
    text = format_case_response(_record(), target_label="<@3>", moderator_label="<@2>")
    assert "Case #42" in text
    assert "Action: Timeout" in text
    assert "Target: <@3>" in text
    assert "Moderator: <@2>" in text
    assert "Reason: spam" in text
    assert "Status: Success" in text
    assert "5m" in text


def test_format_case_response_shows_failed_status_without_error_text() -> None:
    text = format_case_response(
        _record(status=STATUS_FAILED, error="The action could not be completed."),
        target_label="x",
        moderator_label="y",
    )
    assert "Status: Failed" in text
    assert "could not be completed" not in text


def test_format_case_detail_contains_all_spec_fields() -> None:
    text = format_case_detail(_record(), target_label="<@3>", moderator_label="<@2>")
    assert "Case #42" in text
    assert "Action: Timeout" in text
    assert "Target: <@3>" in text
    assert "Moderator: <@2>" in text
    assert "Reason: spam" in text
    assert "Created:" in text
    assert "Duration: 5m" in text
    assert "Expires:" in text
    assert "Status: Success" in text
    assert "UTC" in text


def test_format_case_detail_omits_duration_for_non_timeouts() -> None:
    text = format_case_detail(
        _record(action="kick", duration_seconds=None, expires_at=None),
        target_label="x",
        moderator_label="y",
    )
    assert "Duration" not in text
    assert "Expires" not in text
    assert "Status: Success" in text


def test_format_case_detail_shows_automated_source() -> None:
    text = format_case_detail(
        _record(automated=True, detector="spam"),
        target_label="x",
        moderator_label="y",
    )
    assert "Source: Automated (spam)" in text


def test_format_case_detail_shows_manual_source() -> None:
    text = format_case_detail(_record(), target_label="x", moderator_label="y")
    assert "Source: Manual" in text


def test_format_case_list_marks_automated_cases() -> None:
    page = CasePage(
        items=[_record(case_id=5, action="warn", automated=True, detector="spam")],
        total=1,
        page=1,
        page_size=10,
    )
    text = format_case_list(page, member_label="<@3>", label=lambda uid: f"<@{uid}>")
    assert "Warn 🤖" in text


def test_format_case_list_does_not_mark_manual_cases() -> None:
    page = CasePage(
        items=[_record(case_id=5, action="warn")],
        total=1,
        page=1,
        page_size=10,
    )
    text = format_case_list(page, member_label="<@3>", label=lambda uid: f"<@{uid}>")
    assert "🤖" not in text


def test_format_case_list_shows_page_metadata_and_rows() -> None:
    page = CasePage(
        items=[
            _record(case_id=12, action="warn", reason="spam"),
            _record(case_id=11, action="kick", reason="advertising"),
        ],
        total=12,
        page=1,
        page_size=10,
    )
    text = format_case_list(page, member_label="<@3>", label=lambda uid: f"<@{uid}>")
    assert "Moderation cases for <@3>" in text
    assert "page 1 of 2" in text
    assert "(12 total)" in text
    assert "#12" in text
    assert "Warn" in text
    assert "<@2>" in text
    assert "spam" in text
    assert "Success" in text


def test_format_case_list_truncates_long_reasons() -> None:
    page = CasePage(
        items=[_record(case_id=1, action="warn", reason="x" * 200)],
        total=1,
        page=1,
        page_size=10,
    )
    text = format_case_list(page, member_label="<@3>", label=lambda uid: f"<@{uid}>")
    assert "x" * 200 not in text
    assert "…" in text


def test_format_case_list_mentions_next_page() -> None:
    page = CasePage(items=[_record(case_id=1)], total=25, page=2, page_size=10)
    text = format_case_list(page, member_label="<@3>", label=lambda uid: f"<@{uid}>")
    assert "page 3" in text
