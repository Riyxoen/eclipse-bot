"""Tests for the case service.

The service is tested against the repository abstraction (memory
implementation here), separate from Discord commands. Focus: guild isolation,
identifier validation, pagination bounds, and status validation.
"""

from __future__ import annotations

import pytest
from bot.database.repository import MemoryCaseRepository
from bot.moderation.cases import STATUS_FAILED, STATUS_SUCCESS, CaseRecord, utc_now
from bot.services.cases import MAX_LIST_LIMIT, CaseService


def _record(guild_id: int = 10, target: int = 100, moderator: int = 200, **overrides) -> CaseRecord:
    values: dict = {
        "guild_id": guild_id,
        "target_user_id": target,
        "moderator_user_id": moderator,
        "action": "warn",
        "reason": "spam",
        "created_at": utc_now(),
        "status": STATUS_SUCCESS,
    }
    values.update(overrides)
    return CaseRecord(**values)


@pytest.fixture
def service() -> CaseService:
    return CaseService(MemoryCaseRepository())


# ------------------------------------------------------------------ create


def test_create_assigns_case_id(service: CaseService) -> None:
    record = service.create(_record())
    assert record.case_id is not None
    assert service.get(10, record.case_id) is not None


def test_create_rejects_unknown_status(service: CaseService) -> None:
    with pytest.raises(ValueError, match="status"):
        service.create(_record(status="pending"))


def test_create_never_duplicates_on_retry(service: CaseService) -> None:
    """Two separate attempts are two cases, never a duplicated original."""
    first = service.create(_record())
    second = service.create(_record())
    assert first.case_id != second.case_id


# --------------------------------------------------------------------- get


def test_get_returns_case(service: CaseService) -> None:
    record = service.create(_record(guild_id=10, target=100))
    fetched = service.get(10, record.case_id)
    assert fetched is not None
    assert fetched.target_user_id == 100


def test_get_enforces_guild_isolation(service: CaseService) -> None:
    record = service.create(_record(guild_id=10))
    # Same case ID from another guild is indistinguishable from "not found".
    assert service.get(20, record.case_id) is None
    assert service.get(10, record.case_id) is not None


def test_get_missing_case_returns_none(service: CaseService) -> None:
    assert service.get(10, 999) is None


@pytest.mark.parametrize("case_id", [0, -1, -100])
def test_get_invalid_case_ids_return_none(service: CaseService, case_id: int) -> None:
    assert service.get(10, case_id) is None


# ------------------------------------------------------------------- lists


def test_list_for_member_isolated_by_guild(service: CaseService) -> None:
    service.create(_record(guild_id=10, target=100))
    service.create(_record(guild_id=20, target=100))
    page = service.list_for_member(10, 100)
    assert page.total == 1
    assert page.items[0].guild_id == 10


def test_list_for_member_multiple_moderators(service: CaseService) -> None:
    service.create(_record(target=100, moderator=1))
    service.create(_record(target=100, moderator=2))
    service.create(_record(target=100, moderator=3))
    page = service.list_for_member(10, 100)
    assert page.total == 3
    assert {r.moderator_user_id for r in page.items} == {1, 2, 3}


def test_list_for_member_multiple_targets_separated(service: CaseService) -> None:
    service.create(_record(target=100))
    service.create(_record(target=100))
    service.create(_record(target=200))
    assert service.list_for_member(10, 100).total == 2
    assert service.list_for_member(10, 200).total == 1


def test_list_for_guild_counts_only_that_guild(service: CaseService) -> None:
    for guild in (10, 10, 20):
        service.create(_record(guild_id=guild))
    assert service.list_for_guild(10).total == 2
    assert service.list_for_guild(20).total == 1


def test_list_pagination_bounds(service: CaseService) -> None:
    for _ in range(25):
        service.create(_record(target=100))

    page1 = service.list_for_member(10, 100, page=1, page_size=10)
    page2 = service.list_for_member(10, 100, page=2, page_size=10)
    page3 = service.list_for_member(10, 100, page=3, page_size=10)

    assert len(page1.items) == 10
    assert len(page2.items) == 10
    assert len(page3.items) == 5
    assert page1.total == 25
    assert page1.total_pages == 3
    assert page1.has_more is True
    assert page2.has_more is True
    assert page3.has_more is False
    ids = {r.case_id for r in page1.items + page2.items + page3.items}
    assert len(ids) == 25  # no overlap between pages


def test_list_clamps_oversized_page_size(service: CaseService) -> None:
    for _ in range(60):
        service.create(_record(target=100))
    page = service.list_for_member(10, 100, page_size=10_000)
    assert len(page.items) <= MAX_LIST_LIMIT
    assert page.total == 60


def test_list_negative_page_is_clamped(service: CaseService) -> None:
    service.create(_record(target=100))
    page = service.list_for_member(10, 100, page=-5)
    assert page.page == 1
    assert len(page.items) == 1


def test_list_empty_member(service: CaseService) -> None:
    page = service.list_for_member(10, 999)
    assert page.items == []
    assert page.total == 0
    assert page.total_pages == 1


# --------------------------------------------------------------- updates


def test_update_status(service: CaseService) -> None:
    record = service.create(_record())
    updated = service.update_status(10, record.case_id, STATUS_FAILED)
    assert updated is not None
    assert updated.status == STATUS_FAILED
    assert service.get(10, record.case_id).status == STATUS_FAILED


def test_update_status_wrong_guild_returns_none(service: CaseService) -> None:
    record = service.create(_record(guild_id=10))
    assert service.update_status(20, record.case_id, STATUS_FAILED) is None
    assert service.get(10, record.case_id).status == STATUS_SUCCESS


def test_update_status_missing_case_returns_none(service: CaseService) -> None:
    assert service.update_status(10, 999, STATUS_FAILED) is None


def test_update_status_invalid_id_returns_none(service: CaseService) -> None:
    assert service.update_status(10, 0, STATUS_FAILED) is None


def test_update_status_rejects_unknown_status(service: CaseService) -> None:
    record = service.create(_record())
    with pytest.raises(ValueError, match="status"):
        service.update_status(10, record.case_id, "resolved")


def test_update_metadata(service: CaseService) -> None:
    record = service.create(_record())
    updated = service.update_metadata(10, record.case_id, {"dm_delivered": False})
    assert updated is not None
    assert updated.metadata == {"dm_delivered": False}
    assert service.get(10, record.case_id).metadata == {"dm_delivered": False}


def test_update_metadata_wrong_guild_or_missing_returns_none(service: CaseService) -> None:
    record = service.create(_record(guild_id=10))
    assert service.update_metadata(20, record.case_id, {"x": 1}) is None
    assert service.update_metadata(10, 999, {"x": 1}) is None
    assert service.update_metadata(10, 0, {"x": 1}) is None


# ------------------------------------------------------ active warnings


def test_count_active_warnings_counts_successful_warns_only(service: CaseService) -> None:
    service.create(_record(action="warn"))
    service.create(_record(action="warn", status=STATUS_FAILED))
    service.create(_record(action="kick"))
    service.create(_record(guild_id=20, action="warn"))
    assert service.count_active_warnings(10, 100, None) == 1


def test_count_active_warnings_honors_expiration_cutoff(service: CaseService) -> None:
    from datetime import timedelta

    old = utc_now() - timedelta(days=30)
    service.create(_record(action="warn", created_at=old))
    service.create(_record(action="warn"))
    cutoff = utc_now() - timedelta(days=7)
    assert service.count_active_warnings(10, 100, cutoff) == 1
    assert service.count_active_warnings(10, 100, None) == 2


def test_count_active_warnings_rejects_naive_datetime(service: CaseService) -> None:
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone"):
        service.count_active_warnings(10, 100, datetime(2024, 1, 1))
