"""Tests for configuration loading and validation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from bot.configuration.loader import load_settings
from bot.configuration.settings import Settings
from bot.core.errors import ConfigError


def test_valid_environment_produces_settings(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT", "0")

    settings = load_settings()

    assert isinstance(settings, Settings)
    assert settings.token == fake_token
    assert settings.log_level == logging.DEBUG
    assert settings.shutdown_timeout_seconds == 5.0
    assert settings.enable_message_content_intent is False


def test_defaults_applied(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)

    settings = load_settings()

    assert settings.log_level == logging.INFO
    assert settings.log_file is None
    assert settings.shutdown_timeout_seconds == 10.0
    assert settings.enable_message_content_intent is True  # prefix commands need it
    assert settings.command_prefix == "·"


def test_missing_token_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        load_settings()


def test_placeholder_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "your_discord_bot_token_here")
    with pytest.raises(ConfigError, match="placeholder"):
        load_settings()


def test_invalid_log_level_reported(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_LOG_LEVEL", "LOUD")
    with pytest.raises(ConfigError, match="RIYXOEN_LOG_LEVEL"):
        load_settings()


def test_invalid_timeout_reported(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS", "soon")
    with pytest.raises(ConfigError, match="RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS"):
        load_settings()


def test_negative_timeout_reported(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS", "-3")
    with pytest.raises(ConfigError, match="must be greater than 0"):
        load_settings()


def test_invalid_boolean_reported(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT", "maybe")
    with pytest.raises(ConfigError, match="RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT"):
        load_settings()


def test_multiple_errors_reported_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIYXOEN_LOG_LEVEL", "LOUD")
    monkeypatch.setenv("RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS", "nope")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    message = str(excinfo.value)
    assert "DISCORD_TOKEN" in message
    assert "RIYXOEN_LOG_LEVEL" in message
    assert "RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS" in message


def test_load_settings_reads_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DISCORD_TOKEN=from.file.token\n", encoding="utf-8")

    settings = load_settings(env_file=env_file)

    assert settings.token == "from.file.token"


# ---------------------------------------------------------------- Phase 2


def test_moderation_defaults_applied(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)

    settings = load_settings()

    assert settings.moderator_role_ids == ()
    assert settings.log_channel_id is None
    assert settings.notify_users is True
    assert settings.max_purge_amount == 100
    assert settings.database_path == Path("data/cases.db")
    assert settings.enable_message_content_intent is True


def test_message_content_intent_defaults_to_on(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    assert load_settings().enable_message_content_intent is True


def test_command_prefix_default_and_override(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    assert load_settings().command_prefix == "·"
    monkeypatch.setenv("RIYXOEN_COMMAND_PREFIX", "!")
    assert load_settings().command_prefix == "!"


def test_moderator_role_ids_parsed(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_MODERATOR_ROLE_IDS", "111, 222,333")

    settings = load_settings()

    assert settings.moderator_role_ids == (111, 222, 333)


def test_moderator_role_ids_empty_is_valid(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_MODERATOR_ROLE_IDS", "")

    assert load_settings().moderator_role_ids == ()


def test_moderator_role_ids_invalid_reported(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_MODERATOR_ROLE_IDS", "111,not-a-number")

    with pytest.raises(ConfigError, match="RIYXOEN_MODERATOR_ROLE_IDS"):
        load_settings()


def test_log_channel_id_parsed(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_LOG_CHANNEL_ID", "555")

    assert load_settings().log_channel_id == 555


def test_log_channel_id_empty_is_none(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_LOG_CHANNEL_ID", "")

    assert load_settings().log_channel_id is None


def test_log_channel_id_invalid_reported(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_LOG_CHANNEL_ID", "abc")

    with pytest.raises(ConfigError, match="RIYXOEN_LOG_CHANNEL_ID"):
        load_settings()


def test_notify_users_disabled(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_NOTIFY_USERS", "0")

    assert load_settings().notify_users is False


def test_max_purge_amount_parsed(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_MAX_PURGE_AMOUNT", "250")

    assert load_settings().max_purge_amount == 250


def test_max_purge_amount_invalid_reported(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_MAX_PURGE_AMOUNT", "many")

    with pytest.raises(ConfigError, match="RIYXOEN_MAX_PURGE_AMOUNT"):
        load_settings()


def test_max_purge_amount_ceiling_enforced(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_MAX_PURGE_AMOUNT", "5000")

    with pytest.raises(ConfigError, match="5000"):
        load_settings()


def test_database_path_parsed(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_DATABASE_PATH", "/tmp/riyxoen/cases.sqlite")

    assert load_settings().database_path == Path("/tmp/riyxoen/cases.sqlite")
