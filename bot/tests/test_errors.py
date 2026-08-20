"""Tests for the domain error hierarchy and centralized error classification."""

from __future__ import annotations

from bot.core.error_handler import classify_error
from bot.core.errors import BotError, ConfigError
from discord import app_commands


def test_hierarchy() -> None:
    assert issubclass(ConfigError, BotError)
    assert issubclass(BotError, Exception)


def test_classify_missing_permissions() -> None:
    error = app_commands.MissingPermissions([])
    message = classify_error(error)
    assert message is not None
    assert "permission" in message


def test_classify_bot_missing_permissions() -> None:
    error = app_commands.BotMissingPermissions([])
    message = classify_error(error)
    assert message is not None
    assert "permission" in message


def test_classify_no_private_message() -> None:
    message = classify_error(app_commands.NoPrivateMessage())
    assert message is not None
    assert "server" in message


def test_classify_generic_check_failure() -> None:
    message = classify_error(app_commands.CheckFailure("denied"))
    assert message is not None
    assert "allowed" in message


def test_classify_transformer_error_target_not_found() -> None:
    from discord import AppCommandOptionType

    class _FakeTransformer:
        _error_display_name = "member"

    error = app_commands.TransformerError("123", AppCommandOptionType.user, _FakeTransformer())
    message = classify_error(error)
    assert message is not None
    assert "couldn't be found" in message


def test_classify_cooldown_includes_retry() -> None:
    error = app_commands.CommandOnCooldown(cooldown=None, retry_after=7.4)
    message = classify_error(error)
    assert message is not None
    assert "7" in message


def test_classify_command_not_found_returns_none() -> None:
    assert classify_error(app_commands.CommandNotFound("a b", [])) is None


def test_classify_unknown_error_returns_none() -> None:
    class _UnexpectedError(app_commands.AppCommandError):
        pass

    assert classify_error(_UnexpectedError("boom")) is None
