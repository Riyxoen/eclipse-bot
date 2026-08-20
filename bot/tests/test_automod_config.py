"""Tests for automated-moderation configuration parsing and validation."""

from __future__ import annotations

import pytest
from bot.configuration.automod import (
    AutomodSettings,
    load_automod_settings,
)
from bot.configuration.loader import load_settings
from bot.core.errors import ConfigError


def _load(monkeypatch: pytest.MonkeyPatch, **env: str) -> AutomodSettings:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    errors: list[str] = []
    settings = load_automod_settings(errors)
    assert errors == []
    return settings


# ---------------------------------------------------------------- defaults


def test_defaults_are_opt_in_and_conservative() -> None:
    errors: list[str] = []
    settings = load_automod_settings(errors)
    assert errors == []
    assert settings.enabled is False  # opt-in
    assert settings.spam_threshold == 5
    assert settings.spam_window_seconds == 5
    assert settings.spam_action == "delete"
    assert settings.duplicate_threshold == 4
    assert settings.duplicate_window_seconds == 30
    assert settings.mention_everyone_threshold == 0  # @everyone enforcement off
    assert settings.link_action == "allow"  # URLs are not blocked by default
    assert settings.blocked_terms == ()  # no hardcoded offensive words
    assert settings.blocked_terms_substring is False
    assert settings.escalation == ()
    assert settings.enforcement_cooldown_seconds == 60
    assert settings.warning_window_seconds == 7 * 24 * 60 * 60
    assert settings.timeout_duration_seconds == 3600


# ------------------------------------------------------------------- parse


def test_full_environment_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _load(
        monkeypatch,
        RIYXOEN_AUTOMOD_ENABLED="1",
        RIYXOEN_AUTOMOD_SPAM_THRESHOLD="7",
        RIYXOEN_AUTOMOD_SPAM_WINDOW_SECONDS="10",
        RIYXOEN_AUTOMOD_SPAM_ACTION="warn",
        RIYXOEN_AUTOMOD_DUPLICATE_THRESHOLD="3",
        RIYXOEN_AUTOMOD_DUPLICATE_WINDOW_SECONDS="15",
        RIYXOEN_AUTOMOD_DUPLICATE_ACTION="timeout",
        RIYXOEN_AUTOMOD_MENTION_USER_THRESHOLD="20",
        RIYXOEN_AUTOMOD_MENTION_ROLE_THRESHOLD="5",
        RIYXOEN_AUTOMOD_MENTION_TOTAL_THRESHOLD="25",
        RIYXOEN_AUTOMOD_MENTION_EVERYONE_THRESHOLD="2",
        RIYXOEN_AUTOMOD_MENTION_ACTION="warn",
        RIYXOEN_AUTOMOD_TIMEOUT_DURATION_SECONDS="900",
        RIYXOEN_AUTOMOD_LINK_ACTION="delete",
        RIYXOEN_AUTOMOD_ALLOWED_DOMAINS="youtube.com, github.com",
        RIYXOEN_AUTOMOD_BLOCKED_TERMS="alpha,beta,gamma",
        RIYXOEN_AUTOMOD_BLOCKED_TERMS_SUBSTRING="1",
        RIYXOEN_AUTOMOD_WORD_FILTER_ACTION="warn",
        RIYXOEN_AUTOMOD_EXEMPT_USER_IDS="111, 222",
        RIYXOEN_AUTOMOD_EXEMPT_ROLE_IDS="333",
        RIYXOEN_AUTOMOD_EXEMPT_CHANNEL_IDS="444",
        RIYXOEN_AUTOMOD_ENFORCEMENT_COOLDOWN_SECONDS="30",
        RIYXOEN_AUTOMOD_WARNING_WINDOW_SECONDS="86400",
        RIYXOEN_AUTOMOD_ESCALATION="3:3600,5:43200",
    )
    assert settings.enabled is True
    assert settings.spam_threshold == 7
    assert settings.spam_window_seconds == 10
    assert settings.spam_action == "warn"
    assert settings.duplicate_threshold == 3
    assert settings.duplicate_action == "timeout"
    assert settings.mention_user_threshold == 20
    assert settings.mention_everyone_threshold == 2
    assert settings.timeout_duration_seconds == 900
    assert settings.link_action == "delete"
    assert settings.allowed_domains == ("youtube.com", "github.com")
    assert settings.blocked_terms == ("alpha", "beta", "gamma")
    assert settings.blocked_terms_substring is True
    assert settings.word_filter_action == "warn"
    assert settings.exempt_user_ids == (111, 222)
    assert settings.exempt_role_ids == (333,)
    assert settings.exempt_channel_ids == (444,)
    assert settings.enforcement_cooldown_seconds == 30
    assert settings.warning_window_seconds == 86400
    assert settings.escalation == ((3, 3600), (5, 43200))


# -------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("RIYXOEN_AUTOMOD_SPAM_ACTION", "ban", "must be one of"),
        ("RIYXOEN_AUTOMOD_DUPLICATE_ACTION", "kick", "must be one of"),
        ("RIYXOEN_AUTOMOD_MENTION_ACTION", "ban", "must be one of"),
        ("RIYXOEN_AUTOMOD_LINK_ACTION", "ban", "must be one of"),
        ("RIYXOEN_AUTOMOD_WORD_FILTER_ACTION", "ban", "must be one of"),
        ("RIYXOEN_AUTOMOD_SPAM_THRESHOLD", "1", "at least 2"),
        ("RIYXOEN_AUTOMOD_DUPLICATE_THRESHOLD", "0", "at least 2"),
        ("RIYXOEN_AUTOMOD_SPAM_WINDOW_SECONDS", "0", "at least 1"),
        ("RIYXOEN_AUTOMOD_MENTION_USER_THRESHOLD", "-3", "at least 0"),
        ("RIYXOEN_AUTOMOD_BLOCKED_TERMS_SUBSTRING", "maybe", "boolean"),
        ("RIYXOEN_AUTOMOD_EXEMPT_USER_IDS", "abc", "list of IDs"),
        ("RIYXOEN_AUTOMOD_EXEMPT_ROLE_IDS", "12,xyz", "list of IDs"),
        ("RIYXOEN_AUTOMOD_TIMEOUT_DURATION_SECONDS", "0", "at least 1"),
    ],
)
def test_invalid_values_reported(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str, match: str
) -> None:
    monkeypatch.setenv(name, value)
    errors: list[str] = []
    load_automod_settings(errors)
    assert any(match in error for error in errors), errors


def test_escalation_parsed_sorted_regardless_of_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _load(monkeypatch, RIYXOEN_AUTOMOD_ESCALATION="5:43200,3:3600")
    assert settings.escalation == ((3, 3600), (5, 43200))


def test_escalation_duplicate_counts_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIYXOEN_AUTOMOD_ESCALATION", "3:3600,3:7200")
    errors: list[str] = []
    load_automod_settings(errors)
    assert any("distinct" in error for error in errors)


def test_escalation_duration_over_28_days_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIYXOEN_AUTOMOD_ESCALATION", "3:99999999")
    errors: list[str] = []
    load_automod_settings(errors)
    assert any("28 days" in error for error in errors)


def test_escalation_malformed_entry_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIYXOEN_AUTOMOD_ESCALATION", "3:3600,banana")
    errors: list[str] = []
    load_automod_settings(errors)
    assert any("3:3600" in error for error in errors)


def test_escalation_zero_threshold_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIYXOEN_AUTOMOD_ESCALATION", "0:3600")
    errors: list[str] = []
    load_automod_settings(errors)
    assert any("positive" in error for error in errors)


# --------------------------------------------------------------- integration


def test_load_settings_integrates_automod(fake_token: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_AUTOMOD_ENABLED", "1")
    monkeypatch.setenv("RIYXOEN_AUTOMOD_SPAM_THRESHOLD", "6")

    settings = load_settings()

    assert settings.automod.enabled is True
    assert settings.automod.spam_threshold == 6


def test_load_settings_reports_automod_errors(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_AUTOMOD_SPAM_ACTION", "ban")
    with pytest.raises(ConfigError, match="RIYXOEN_AUTOMOD_SPAM_ACTION"):
        load_settings()


def test_automod_disabled_by_default_in_settings(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    assert load_settings().automod.enabled is False
