"""Regression tests for the bot client wiring.

These guard against the Phase 1 bug where ``discord.Client`` (unlike
``discord.ext.commands.Bot``) does not create a ``.tree`` — without this fix
``setup_hook`` crashes and no slash commands are registered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bot.configuration.settings import Settings
from bot.core.bot import RiyxoenBot, RiyxoenCommandTree
from bot.services.moderation import ModerationService


def _settings(db_path: Path) -> Settings:
    return Settings(token="test-token", database_path=db_path)


def test_bot_constructs_with_command_tree(tmp_path: Path) -> None:
    bot = RiyxoenBot(_settings(tmp_path / "cases.db"))
    assert isinstance(bot.tree, RiyxoenCommandTree)


async def test_on_ready_logs_connection_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    import logging

    from bot.tests.fakes import FakeUser

    bot = RiyxoenBot(_settings(tmp_path / "cases.db"))
    bot._connection.user = FakeUser(100_000, "riyxoen-test", bot=True)
    monkeypatch.setattr(RiyxoenBot, "guilds", property(lambda self: [object(), object()]))

    with caplog.at_level(logging.INFO, logger="riyxoen.bot"):
        await bot.on_ready()

    assert "bot connected" in caplog.text
    assert "Bot connected as riyxoen-test" in caplog.text
    assert "Guild count: 2" in caplog.text


async def test_setup_hook_registers_all_commands_and_builds_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = RiyxoenBot(_settings(tmp_path / "cases.db"))

    async def _fake_sync(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    monkeypatch.setattr(bot.tree, "sync", _fake_sync)
    await bot.setup_hook()

    names = sorted(command.name for command in bot.tree.get_commands())
    assert names == [
        "automod",
        "ban",
        "case",
        "cases",
        "config",
        "health",
        "help",
        "kick",
        "lock",
        "moderation-history",
        "ping",
        "purge",
        "slowmode",
        "timeout",
        "unban",
        "unlock",
        "warn",
    ]
    assert isinstance(bot.moderation_service, ModerationService)
    assert bot.case_repository is not None
    assert bot.case_service is not None
    assert bot.permissions is not None
    from bot.automod.engine import AutomodEngine
    from bot.services.guild_config import GuildConfigService

    assert isinstance(bot.automod, AutomodEngine)
    assert isinstance(bot.config_service, GuildConfigService)
    assert bot.config_repository is not None

    # The Phase 6 moderation commands are top-level and guild-only.
    for name in ("warn", "timeout", "kick", "ban", "unban", "purge", "slowmode", "lock", "unlock"):
        command = next(c for c in bot.tree.get_commands() if c.name == name)
        assert command.guild_only is True

    # The /automod cog exposes the Phase 8 surface (admin-gated).
    automod_group = next(c for c in bot.tree.get_commands() if c.name == "automod")
    automod_children = sorted(c.name for c in automod_group.walk_commands())
    assert "enable" in automod_children
    assert "disable" in automod_children
    assert "status" in automod_children
    assert "invites" in automod_children
    assert "raid" in automod_children

    # The config cog exposes the Phase 5 command groups.
    config_group = next(c for c in bot.tree.get_commands() if c.name == "config")
    config_children = sorted(c.name for c in config_group.walk_commands())
    assert "view" in config_children
    assert "reset" in config_children
    assert "moderation" in config_children
    assert "roles" in config_children
    assert "exemptions" in config_children
    assert "logs" in config_children


async def test_setup_hook_initializes_sqlite_database(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "nested" / "cases.db"
    bot = RiyxoenBot(_settings(db_path))

    async def _fake_sync(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    monkeypatch.setattr(bot.tree, "sync", _fake_sync)
    await bot.setup_hook()

    from bot.database.repository import SQLiteCaseRepository

    assert isinstance(bot.case_repository, SQLiteCaseRepository)
    assert db_path.exists()  # database auto-initializes at startup

    # The repository is actually usable end to end.
    from bot.moderation.cases import STATUS_SUCCESS, CaseRecord, utc_now

    record = bot.case_service.create(
        CaseRecord(
            guild_id=10,
            target_user_id=1,
            moderator_user_id=2,
            action="warn",
            reason="test",
            created_at=utc_now(),
            status=STATUS_SUCCESS,
        )
    )
    assert bot.case_service.get(10, record.case_id) is not None


async def test_setup_hook_falls_back_to_memory_repository_when_db_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file in the way of the database path makes SQLite unopenable.
    blocker = tmp_path / "cases.db"
    blocker.write_text("not a directory", encoding="utf-8")
    bot = RiyxoenBot(_settings(blocker))

    async def _fake_sync(*args, **kwargs):  # noqa: ANN002, ANN003
        return []

    monkeypatch.setattr(bot.tree, "sync", _fake_sync)
    await bot.setup_hook()

    from bot.database.repository import MemoryCaseRepository

    assert isinstance(bot.case_repository, MemoryCaseRepository)
    assert bot.case_service is not None
    assert bot.moderation_service is not None


async def test_on_message_dispatches_to_automod_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = RiyxoenBot(_settings(tmp_path / "cases.db"))
    handled: list = []

    async def _fake_handle(self, message):  # noqa: ANN001
        handled.append(message)

    bot.automod = type("FakeEngine", (), {"handle_message": _fake_handle})()
    message = object()
    await bot.on_message(message)  # type: ignore[arg-type]

    assert handled == [message]


async def test_on_message_safe_when_engine_not_built(tmp_path: Path) -> None:
    bot = RiyxoenBot(_settings(tmp_path / "cases.db"))
    bot.automod = None
    await bot.on_message(object())  # type: ignore[arg-type]


async def test_message_content_intent_enabled_when_automod_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bot.configuration.automod import AutomodSettings
    from bot.core.intents import build_intents

    settings = Settings(
        token="test-token",
        database_path=tmp_path / "cases.db",
        automod=AutomodSettings(enabled=True),
    )
    assert build_intents(settings).message_content is True

    # Phase 6: prefix commands require message content, so the intent is on
    # by default (and stays on even when automod is disabled).
    settings = Settings(token="test-token", database_path=tmp_path / "cases.db")
    assert build_intents(settings).message_content is True

    settings = Settings(
        token="test-token",
        database_path=tmp_path / "cases.db",
        enable_message_content_intent=False,
    )
    assert build_intents(settings).message_content is False
