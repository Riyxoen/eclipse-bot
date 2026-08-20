"""Tests for the case repository layer (SQLite + memory) and migrations.

The repository is tested directly and separately from the case service and
commands. No Discord connection is involved.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from bot.database.migrations import LATEST_SCHEMA_VERSION, current_schema_version, migrate
from bot.database.repository import (
    MemoryCaseRepository,
    SQLiteCaseRepository,
    build_case_repository,
)
from bot.moderation.cases import STATUS_FAILED, STATUS_SUCCESS, CaseRecord, utc_now


def _record(guild_id: int = 10, target: int = 20, moderator: int = 30, **overrides) -> CaseRecord:
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


def _timeout_record(guild_id: int = 10, target: int = 20, moderator: int = 30) -> CaseRecord:
    created = utc_now()
    return CaseRecord(
        guild_id=guild_id,
        target_user_id=target,
        moderator_user_id=moderator,
        action="timeout",
        reason="spam",
        created_at=created,
        status=STATUS_SUCCESS,
        duration_seconds=300,
        expires_at=created + timedelta(seconds=300),
    )


# ------------------------------------------------------------- initialization


def test_repository_initializes_schema(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        connection = sqlite3.connect(repo.path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "cases" in tables
        finally:
            connection.close()
    finally:
        repo.close()


def test_migrations_record_schema_version(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        connection = sqlite3.connect(repo.path)
        try:
            assert current_schema_version(connection) == LATEST_SCHEMA_VERSION
        finally:
            connection.close()
    finally:
        repo.close()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "cases.db"
    SQLiteCaseRepository(path).close()
    SQLiteCaseRepository(path).close()  # reopening must not fail or re-apply
    connection = sqlite3.connect(path)
    try:
        assert current_schema_version(connection) == LATEST_SCHEMA_VERSION
        # A second migrate() call is a no-op.
        assert migrate(connection) == LATEST_SCHEMA_VERSION
    finally:
        connection.close()


def test_build_case_repository_none_returns_memory() -> None:
    assert isinstance(build_case_repository(None), MemoryCaseRepository)


def test_build_case_repository_path_returns_sqlite(tmp_path: Path) -> None:
    repo = build_case_repository(tmp_path / "cases.db")
    try:
        assert isinstance(repo, SQLiteCaseRepository)
    finally:
        repo.close()


# ------------------------------------------------------------------ creation


def test_create_assigns_unique_incrementing_ids(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        first = repo.create(_record())
        second = repo.create(_record(action="kick"))
        assert first.case_id == 1
        assert second.case_id == first.case_id + 1
        assert first.case_id != second.case_id
    finally:
        repo.close()


def test_create_round_trips_all_fields(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        created = utc_now()
        stored = repo.create(
            CaseRecord(
                guild_id=10,
                target_user_id=20,
                moderator_user_id=30,
                action="timeout",
                reason="spam",
                created_at=created,
                status=STATUS_SUCCESS,
                duration_seconds=300,
                expires_at=created + timedelta(seconds=300),
            )
        )
        fetched = repo.get(10, stored.case_id)
        assert fetched is not None
        assert fetched.guild_id == 10
        assert fetched.target_user_id == 20
        assert fetched.moderator_user_id == 30
        assert fetched.action == "timeout"
        assert fetched.reason == "spam"
        assert fetched.status == STATUS_SUCCESS
        assert fetched.duration_seconds == 300
        assert fetched.expires_at is not None
        assert fetched.expires_at.tzinfo is not None
        assert fetched.created_at.tzinfo is not None
    finally:
        repo.close()


def test_create_round_trips_automated_fields(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        stored = repo.create(_record(automated=True, detector="spam", action="delete"))
        fetched = repo.get(10, stored.case_id)
        assert fetched is not None
        assert fetched.automated is True
        assert fetched.detector == "spam"
    finally:
        repo.close()


def test_create_persists_failed_cases_with_error(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        stored = repo.create(_record(status=STATUS_FAILED, error="Safe error message."))
        fetched = repo.get(10, stored.case_id)
        assert fetched is not None
        assert fetched.status == STATUS_FAILED
        assert fetched.error == "Safe error message."
    finally:
        repo.close()


# --------------------------------------------------------------- retrieval


def test_get_missing_case_returns_none(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        assert repo.get(10, 999) is None
    finally:
        repo.close()


def test_get_enforces_guild_isolation(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        stored = repo.create(_record(guild_id=10))
        # Same case ID through a different guild must be invisible.
        assert repo.get(99, stored.case_id) is None
        assert repo.get(10, stored.case_id) is not None
    finally:
        repo.close()


def test_created_at_round_trips_as_utc(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        created = utc_now()
        stored = repo.create(_record(created_at=created))
        fetched = repo.get(10, stored.case_id)
        assert fetched is not None
        assert fetched.created_at.tzinfo is not None
        assert fetched.created_at == created
    finally:
        repo.close()


# ------------------------------------------------------------------- listing


def test_list_for_guild_returns_only_that_guild(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        repo.create(_record(guild_id=10, target=1))
        repo.create(_record(guild_id=20, target=2))
        repo.create(_record(guild_id=10, target=3))

        cases, total = repo.list_for_guild(10, limit=10, offset=0)
        assert total == 2
        assert {case.target_user_id for case in cases} == {1, 3}
    finally:
        repo.close()


def test_list_for_member_filters_by_target(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        repo.create(_record(target=100))
        repo.create(_record(target=200))
        repo.create(_record(target=100))

        cases, total = repo.list_for_member(10, 100, limit=10, offset=0)
        assert total == 2
        assert all(case.target_user_id == 100 for case in cases)
    finally:
        repo.close()


def test_listing_is_newest_first(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        earlier = repo.create(_record(target=100, created_at=utc_now()))
        later = repo.create(_record(target=100, created_at=utc_now()))
        cases, _ = repo.list_for_member(10, 100, limit=10, offset=0)
        assert cases[0].case_id == later.case_id
        assert cases[1].case_id == earlier.case_id
    finally:
        repo.close()


def test_listing_pagination(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        for _ in range(25):
            repo.create(_record(target=100))

        page1, total = repo.list_for_member(10, 100, limit=10, offset=0)
        page2, _ = repo.list_for_member(10, 100, limit=10, offset=10)
        page3, _ = repo.list_for_member(10, 100, limit=10, offset=20)

        assert total == 25
        assert len(page1) == 10
        assert len(page2) == 10
        assert len(page3) == 5
        ids = {c.case_id for c in page1 + page2 + page3}
        assert len(ids) == 25  # no duplicates across pages
    finally:
        repo.close()


# ------------------------------------------------------------------- updates


def test_update_status_returns_updated_case(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        stored = repo.create(_record())
        updated = repo.update_status(10, stored.case_id, STATUS_FAILED)
        assert updated is not None
        assert updated.status == STATUS_FAILED
        assert repo.get(10, stored.case_id).status == STATUS_FAILED
    finally:
        repo.close()


def test_update_status_wrong_guild_returns_none(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        stored = repo.create(_record(guild_id=10))
        assert repo.update_status(99, stored.case_id, STATUS_FAILED) is None
        # The original record is untouched.
        assert repo.get(10, stored.case_id).status == STATUS_SUCCESS
    finally:
        repo.close()


def test_update_status_missing_case_returns_none(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        assert repo.update_status(10, 999, STATUS_FAILED) is None
    finally:
        repo.close()


def test_update_metadata_round_trips(tmp_path: Path) -> None:
    for repo in (SQLiteCaseRepository(tmp_path / "cases.db"), MemoryCaseRepository()):
        try:
            stored = repo.create(_record())
            updated = repo.update_metadata(
                10, stored.case_id, {"dm_delivered": False, "muted": True}
            )
            assert updated is not None
            assert updated.metadata == {"dm_delivered": False, "muted": True}
            assert repo.get(10, stored.case_id).metadata == {
                "dm_delivered": False,
                "muted": True,
            }
            # Clearing metadata works too.
            cleared = repo.update_metadata(10, stored.case_id, None)
            assert cleared is not None and cleared.metadata is None
        finally:
            repo.close()


def test_update_metadata_wrong_guild_or_missing_returns_none(tmp_path: Path) -> None:
    for repo in (SQLiteCaseRepository(tmp_path / "cases.db"), MemoryCaseRepository()):
        try:
            stored = repo.create(_record(guild_id=10))
            assert repo.update_metadata(99, stored.case_id, {"x": 1}) is None
            assert repo.update_metadata(10, 999, {"x": 1}) is None
        finally:
            repo.close()


# --------------------------------------------------------- memory repository


def test_memory_repository_matches_sqlite_behavior() -> None:
    repo = MemoryCaseRepository()
    first = repo.create(_record(guild_id=10, target=100))
    repo.create(_record(guild_id=20, target=100))
    second = repo.create(_record(guild_id=10, target=100))

    assert first.case_id != second.case_id
    assert repo.get(10, first.case_id) is not None
    assert repo.get(20, first.case_id) is None  # guild isolation
    cases, total = repo.list_for_member(10, 100, limit=10, offset=0)
    assert total == 2
    assert repo.update_status(99, first.case_id, STATUS_FAILED) is None
    assert repo.update_status(10, first.case_id, STATUS_FAILED).status == STATUS_FAILED


# -------------------------------------------------------------- migrations


def test_migration_v2_adds_automated_columns_to_v1_database(tmp_path: Path) -> None:
    """A Phase 3 (v1) database upgrades in place with its data intact."""
    path = tmp_path / "cases.db"
    connection = sqlite3.connect(str(path))
    connection.executescript(
        """
        CREATE TABLE cases (
            case_id            INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id           INTEGER NOT NULL,
            target_user_id     INTEGER NOT NULL,
            moderator_user_id  INTEGER NOT NULL,
            action             TEXT    NOT NULL,
            reason             TEXT    NOT NULL,
            duration_seconds   INTEGER,
            expires_at         TEXT,
            status             TEXT    NOT NULL CHECK (status IN ('success', 'failed')),
            error              TEXT,
            created_at         TEXT    NOT NULL
        );
        """
    )
    connection.execute("PRAGMA user_version = 1")
    connection.execute(
        "INSERT INTO cases (guild_id, target_user_id, moderator_user_id, action,"
        " reason, created_at, status) VALUES (10, 20, 30, 'warn', 'spam',"
        " '2024-01-01T00:00:00+00:00', 'success')"
    )
    connection.commit()
    connection.close()

    repo = SQLiteCaseRepository(path)  # runs migration v2 on open
    try:
        check = sqlite3.connect(str(path))
        try:
            assert current_schema_version(check) == LATEST_SCHEMA_VERSION
        finally:
            check.close()
        # Pre-existing data survived and reads back with v2 defaults.
        record = repo.get(10, 1)
        assert record is not None
        assert record.reason == "spam"
        assert record.automated is False
        assert record.detector is None
    finally:
        repo.close()


# ---------------------------------------------------------------- warnings


def test_count_warnings_counts_only_successful_warns_for_member(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        repo.create(_record(guild_id=10, target=100, action="warn"))
        repo.create(_record(guild_id=10, target=100, action="warn", status=STATUS_FAILED))
        repo.create(_record(guild_id=10, target=100, action="kick"))
        repo.create(_record(guild_id=10, target=200, action="warn"))
        repo.create(_record(guild_id=20, target=100, action="warn"))
        assert repo.count_warnings(10, 100, None) == 1
    finally:
        repo.close()


def test_count_warnings_honors_since_cutoff(tmp_path: Path) -> None:
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        old = utc_now() - timedelta(days=10)
        recent = utc_now()
        repo.create(_record(guild_id=10, target=100, action="warn", created_at=old))
        repo.create(_record(guild_id=10, target=100, action="warn", created_at=recent))
        assert repo.count_warnings(10, 100, utc_now() - timedelta(days=5)) == 1
        assert repo.count_warnings(10, 100, None) == 2
    finally:
        repo.close()


def test_count_warnings_memory_repository_matches_sqlite() -> None:
    repo = MemoryCaseRepository()
    old = utc_now() - timedelta(days=10)
    repo.create(_record(guild_id=10, target=100, action="warn", created_at=old))
    repo.create(_record(guild_id=10, target=100, action="warn"))
    repo.create(_record(guild_id=10, target=100, action="warn", status=STATUS_FAILED))
    repo.create(_record(guild_id=20, target=100, action="warn"))
    assert repo.count_warnings(10, 100, utc_now() - timedelta(days=5)) == 1
    assert repo.count_warnings(10, 100, None) == 2


# ------------------------------------------------------- parameterized SQL


def test_guild_ids_are_never_interpolated(tmp_path: Path) -> None:
    """A hostile-looking ID is treated as data, never as SQL."""
    repo = SQLiteCaseRepository(tmp_path / "cases.db")
    try:
        repo.create(_record(guild_id=10))
        # These must not raise or return rows from another guild.
        assert repo.get(-1, 1) is None
        assert repo.list_for_guild(9999, limit=10, offset=0) == ([], 0)
        assert repo.update_status(9999, 1, STATUS_FAILED) is None
    finally:
        repo.close()
