"""Tests for the per-guild configuration service (Phase 5).

The service is tested separately from Discord commands and separately from
the repository: it is driven against the repository abstraction (SQLite and
memory), and a failing repository simulates database outages.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest
from bot.configuration.errors import GuildConfigError
from bot.configuration.settings import Settings
from bot.database.config_repository import (
    MemoryGuildConfigRepository,
    SQLiteGuildConfigRepository,
)
from bot.services.guild_config import GuildConfigService


class FailingConfigRepository(MemoryGuildConfigRepository):
    """Repository whose reads and writes raise (simulates DB outages)."""

    def get(self, guild_id: int):
        raise sqlite3.OperationalError("simulated disk I/O error")

    def upsert(self, config, *, updated_by, updated_at):
        raise sqlite3.OperationalError("simulated disk I/O error")


def _settings(**overrides) -> Settings:
    values: dict = {"token": "test-token"}
    values.update(overrides)
    return Settings(**values)


def _service(tmp_path=None, *, settings=None, repository=None, cache_size: int = 4):
    settings = settings or _settings()
    if repository is None:
        repository = MemoryGuildConfigRepository()
    return GuildConfigService(repository, settings=settings, cache_size=cache_size)


# ------------------------------------------------------------ defaults


def test_first_get_creates_and_persists_defaults() -> None:
    settings = _settings(moderator_role_ids=(111,))
    repository = MemoryGuildConfigRepository()
    service = _service(settings=settings, repository=repository)

    config = service.get(10)

    assert config.guild_id == 10
    assert config.moderator_role_ids == (111,)  # seeded from env
    # Persisted so a restart keeps the same defaults.
    assert repository.get(10) == config


def test_get_returns_cached_snapshot(tmp_path) -> None:
    service = _service(tmp_path, repository=SQLiteGuildConfigRepository(tmp_path / "cases.db"))
    first = service.get(10)
    second = service.get(10)
    assert first is second  # cached — no extra storage round trip


# ------------------------------------------------------------- isolation


def test_guilds_are_isolated() -> None:
    service = _service()
    service.update(10, actor_user_id=1, changes={"spam_threshold": 9})
    service.update(20, actor_user_id=1, changes={"spam_threshold": 3})

    assert service.get(10).spam_threshold == 9
    assert service.get(20).spam_threshold == 3
    # Updating guild A never touches guild B's row.
    assert service.get(10).moderator_role_ids == ()
    assert service.get(20).moderator_role_ids == ()


# --------------------------------------------------------------- updates


def test_update_validates_and_persists() -> None:
    repository = MemoryGuildConfigRepository()
    service = _service(repository=repository)
    updated = service.update(10, actor_user_id=2, changes={"spam_threshold": 8})

    assert updated.spam_threshold == 8
    assert repository.get(10).spam_threshold == 8


def test_update_rejects_invalid_value_without_changing_state() -> None:
    repository = MemoryGuildConfigRepository()
    service = _service(repository=repository)
    with pytest.raises(GuildConfigError):
        service.update(10, actor_user_id=2, changes={"spam_threshold": -5})

    # Nothing was persisted and defaults still stand.
    config = repository.get(10)
    assert config.spam_threshold == 5


def test_update_rejects_unknown_setting() -> None:
    service = _service()
    with pytest.raises(GuildConfigError, match="Unknown"):
        service.update(10, actor_user_id=2, changes={"nope": 1})


def test_update_requires_at_least_one_change() -> None:
    service = _service()
    with pytest.raises(GuildConfigError):
        service.update(10, actor_user_id=2, changes={})


def test_update_enforces_upper_limits() -> None:
    service = _service()
    with pytest.raises(GuildConfigError):
        service.update(10, actor_user_id=2, changes={"max_purge_amount": 100_000})
    with pytest.raises(GuildConfigError):
        service.update(10, actor_user_id=2, changes={"escalation": "3:999999999"})


# --------------------------------------------------------------- reset


def test_reset_restores_defaults_and_preserves_cases(tmp_path) -> None:
    service = _service(tmp_path, repository=SQLiteGuildConfigRepository(tmp_path / "cases.db"))
    service.update(10, actor_user_id=2, changes={"spam_threshold": 9, "notify_users": False})
    assert service.get(10).spam_threshold == 9

    reset = service.reset(10, actor_user_id=2)

    assert reset.spam_threshold == 5  # env default
    assert reset.notify_users is True
    # The guild_config table is reset; unrelated tables (cases) are untouched.
    connection = sqlite3.connect(str(tmp_path / "cases.db"))
    (tables,) = connection.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'cases'"
    ).fetchone()
    assert tables == 1


def test_reset_is_guild_scoped() -> None:
    service = _service()
    service.update(10, actor_user_id=2, changes={"spam_threshold": 9})
    service.update(20, actor_user_id=2, changes={"spam_threshold": 7})
    service.reset(10, actor_user_id=2)

    assert service.get(10).spam_threshold == 5
    assert service.get(20).spam_threshold == 7  # untouched


# ------------------------------------------------------- exemption/roles


def test_add_and_remove_exempt_entries() -> None:
    service = _service()
    updated = service.add_exempt(10, actor_user_id=2, kind="user", entity_id=99)
    assert updated.exempt_user_ids == (99,)

    updated = service.add_exempt(10, actor_user_id=2, kind="role", entity_id=77)
    assert updated.exempt_role_ids == (77,)

    updated = service.add_exempt(10, actor_user_id=2, kind="channel", entity_id=55)
    assert updated.exempt_channel_ids == (55,)

    updated = service.remove_exempt(10, actor_user_id=2, kind="user", entity_id=99)
    assert updated.exempt_user_ids == ()


def test_duplicate_exempt_entry_rejected() -> None:
    service = _service()
    service.add_exempt(10, actor_user_id=2, kind="user", entity_id=99)
    with pytest.raises(GuildConfigError, match="already configured"):
        service.add_exempt(10, actor_user_id=2, kind="user", entity_id=99)


def test_removing_unconfigured_entry_rejected() -> None:
    service = _service()
    with pytest.raises(GuildConfigError, match="not currently configured"):
        service.remove_exempt(10, actor_user_id=2, kind="user", entity_id=99)


def test_invalid_exemption_kind_rejected() -> None:
    service = _service()
    with pytest.raises(GuildConfigError, match="Exemption kind"):
        service.add_exempt(10, actor_user_id=2, kind="server", entity_id=99)


def test_add_and_remove_roles() -> None:
    service = _service()
    updated = service.add_role(10, actor_user_id=2, kind="moderator", role_id=500)
    assert updated.moderator_role_ids == (500,)
    updated = service.add_role(10, actor_user_id=2, kind="administrator", role_id=600)
    assert updated.administrator_role_ids == (600,)

    updated = service.remove_role(10, actor_user_id=2, kind="moderator", role_id=500)
    assert updated.moderator_role_ids == ()


def test_invalid_role_kind_rejected() -> None:
    service = _service()
    with pytest.raises(GuildConfigError):
        service.add_role(10, actor_user_id=2, kind="owner", role_id=500)


def test_add_remove_terms_and_domains() -> None:
    service = _service()
    updated = service.add_blocked_term(10, actor_user_id=2, term="  BadWord  ")
    assert updated.blocked_terms == ("badword",)  # normalized + deduplicated

    updated = service.add_allowed_domain(10, actor_user_id=2, domain="YouTube.com")
    assert updated.allowed_domains == ("youtube.com",)

    updated = service.remove_blocked_term(10, actor_user_id=2, term="BADWORD")
    assert updated.blocked_terms == ()

    updated = service.remove_allowed_domain(10, actor_user_id=2, domain="youtube.com")
    assert updated.allowed_domains == ()


def test_duplicate_term_rejected() -> None:
    service = _service()
    service.add_blocked_term(10, actor_user_id=2, term="bad")
    with pytest.raises(GuildConfigError, match="already configured"):
        service.add_blocked_term(10, actor_user_id=2, term="bad")


# --------------------------------------------------------------- caching


def test_cache_is_bounded(tmp_path) -> None:
    service = _service(tmp_path, repository=MemoryGuildConfigRepository(), cache_size=3)
    for guild_id in range(1, 6):
        service.get(guild_id)
    assert len(service._cache) <= 3
    # Oldest guild was evicted; refetching recreates it from storage.
    assert service.get(1) is not None


def test_update_invalidates_and_notifies_listeners() -> None:
    service = _service()
    notified: list[int] = []
    service.add_invalidation_listener(notified.append)

    service.update(10, actor_user_id=2, changes={"spam_threshold": 6})

    assert notified == [10]
    # The updated snapshot is cached, so the engine sees it immediately.
    assert service.get(10).spam_threshold == 6


def test_reset_notifies_listeners() -> None:
    service = _service()
    notified: list[int] = []
    service.add_invalidation_listener(notified.append)
    service.reset(10, actor_user_id=2)
    assert notified == [10]


# ------------------------------------------------------- database failures


def test_get_degrades_to_defaults_on_db_failure(caplog) -> None:
    service = _service(repository=FailingConfigRepository())
    with caplog.at_level(logging.ERROR, logger="riyxoen.config"):
        config = service.get(10)
    assert config is not None
    assert config.guild_id == 10
    assert config.spam_threshold == 5  # seeded defaults keep the bot working
    assert "could not load configuration" in caplog.text


def test_update_raises_safe_error_on_db_failure() -> None:
    service = _service(repository=FailingConfigRepository())
    with pytest.raises(GuildConfigError) as excinfo:
        service.update(10, actor_user_id=2, changes={"spam_threshold": 6})
    # No SQL details leak to the user.
    assert "sqlite" not in str(excinfo.value).lower()
    assert "OperationalError" not in str(excinfo.value)
    assert "database" in str(excinfo.value).lower()


# ------------------------------------------------------------ audit logging


def test_every_change_emits_audit_log(caplog) -> None:
    service = _service()
    with caplog.at_level(logging.INFO, logger="riyxoen.config"):
        service.update(10, actor_user_id=2, changes={"spam_threshold": 8})
        service.add_exempt(10, actor_user_id=2, kind="user", entity_id=99)
        service.reset(10, actor_user_id=2)

    assert "config change: guild=10 actor=2 setting=spam_threshold old=5 new=8" in caplog.text
    assert "setting=exempt_user_ids" in caplog.text
    assert "config reset: guild=10 actor=2" in caplog.text
    # No secrets, tokens, or message contents are ever logged.
    assert "token" not in caplog.text.lower()
