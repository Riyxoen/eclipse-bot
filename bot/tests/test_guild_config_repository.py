"""Tests for the per-guild configuration repository (Phase 5).

The repository is tested separately from the service: direct persistence,
guild isolation, migration/initialization, and the memory fallback. No
Discord objects are involved.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from bot.configuration.guild import default_guild_config
from bot.configuration.settings import Settings
from bot.database.config_repository import (
    MemoryGuildConfigRepository,
    SQLiteGuildConfigRepository,
    build_guild_config_repository,
)
from bot.database.migrations import current_schema_version


def _config(guild_id: int, **overrides):
    settings = Settings(token="test-token")
    base = default_guild_config(settings, guild_id)
    from dataclasses import replace

    return replace(base, **overrides)


def _at() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _make_sqlite(tmp_path: Path) -> SQLiteGuildConfigRepository:
    return SQLiteGuildConfigRepository(tmp_path / "data" / "cases.db")


# ------------------------------------------------------------ SQLite repo


def test_sqlite_upsert_and_get_round_trip(tmp_path: Path) -> None:
    repo = _make_sqlite(tmp_path)
    assert repo.get(10) is None  # nothing stored yet

    config = _config(10, spam_threshold=9, exempt_role_ids=(7, 8), escalation=((3, 3600),))
    repo.upsert(config, updated_by=2, updated_at=_at())

    fetched = repo.get(10)
    assert fetched is not None
    assert fetched == config
    assert fetched.spam_threshold == 9
    assert fetched.exempt_role_ids == (7, 8)
    assert fetched.escalation == ((3, 3600),)


def test_sqlite_upsert_replaces_existing_row(tmp_path: Path) -> None:
    repo = _make_sqlite(tmp_path)
    repo.upsert(_config(10, max_purge_amount=50), updated_by=2, updated_at=_at())
    repo.upsert(_config(10, max_purge_amount=200), updated_by=3, updated_at=_at())

    assert repo.get(10).max_purge_amount == 200
    # Only one row per guild.
    connection = sqlite3.connect(str(repo.path))
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM guild_config WHERE guild_id = ?", (10,)
    ).fetchone()
    assert count == 1


def test_sqlite_guild_isolation(tmp_path: Path) -> None:
    repo = _make_sqlite(tmp_path)
    repo.upsert(_config(10, spam_threshold=3), updated_by=1, updated_at=_at())
    repo.upsert(_config(20, spam_threshold=9), updated_by=1, updated_at=_at())

    assert repo.get(10).spam_threshold == 3
    assert repo.get(20).spam_threshold == 9
    assert repo.get(30) is None  # guild without a row


def test_sqlite_schema_initialized_and_migrated(tmp_path: Path) -> None:
    repo = _make_sqlite(tmp_path)
    connection = sqlite3.connect(str(repo.path))
    assert current_schema_version(connection) >= 3
    (tables,) = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'guild_config'"
    ).fetchone()
    assert tables == 1


def test_sqlite_persists_across_reopens(tmp_path: Path) -> None:
    path = tmp_path / "cases.db"
    repo = SQLiteGuildConfigRepository(path)
    repo.upsert(_config(10, notify_users=False), updated_by=2, updated_at=_at())
    repo.close()

    reopened = SQLiteGuildConfigRepository(path)
    assert reopened.get(10).notify_users is False
    reopened.close()


def test_builder_returns_memory_when_path_none() -> None:
    assert isinstance(build_guild_config_repository(None), MemoryGuildConfigRepository)


def test_builder_raises_on_unopenable_path(tmp_path: Path) -> None:
    blocker = tmp_path / "cases.db"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises((sqlite3.Error, OSError)):
        build_guild_config_repository(blocker)


# ---------------------------------------------------------- memory repo


def test_memory_repo_round_trip_and_isolation() -> None:
    repo = MemoryGuildConfigRepository()
    assert repo.get(10) is None
    repo.upsert(_config(10, spam_threshold=3), updated_by=1, updated_at=_at())
    repo.upsert(_config(20, spam_threshold=8), updated_by=1, updated_at=_at())

    assert repo.get(10).spam_threshold == 3
    assert repo.get(20).spam_threshold == 8
    assert repo.get(30) is None
    # Upsert replaces.
    repo.upsert(_config(10, spam_threshold=4), updated_by=2, updated_at=_at())
    assert repo.get(10).spam_threshold == 4


def test_memory_repo_close_clears() -> None:
    repo = MemoryGuildConfigRepository()
    repo.upsert(_config(10), updated_by=1, updated_at=_at())
    repo.close()
    assert repo.get(10) is None
