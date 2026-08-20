"""Tests for the per-guild configuration model (Phase 5).

Covers default seeding from the environment settings, JSON persistence
round-trips, and value validation (bounds, upper limits, invalid values).
"""

from __future__ import annotations

import pytest
from bot.configuration.automod import AutomodSettings
from bot.configuration.errors import GuildConfigError
from bot.configuration.guild import (
    MAX_ENTRY_LENGTH,
    MAX_LIST_ENTRIES,
    default_guild_config,
    validate_entity_id,
    validate_list_entry,
    validate_setting,
)
from bot.configuration.settings import Settings


def _settings(**overrides) -> Settings:
    values: dict = {"token": "test-token"}
    values.update(overrides)
    return Settings(**values)


# ------------------------------------------------------------- defaults


def test_defaults_seeded_from_environment() -> None:
    settings = _settings(
        moderator_role_ids=(111,),
        log_channel_id=222,
        max_purge_amount=50,
        automod=AutomodSettings(
            enabled=True,
            spam_threshold=7,
            blocked_terms=("bad",),
            exempt_role_ids=(333,),
            escalation=((3, 3600),),
        ),
    )
    config = default_guild_config(settings, guild_id=42)

    assert config.guild_id == 42
    assert config.automod_enabled is True
    assert config.moderator_role_ids == (111,)
    assert config.log_channel_id == 222
    assert config.mod_log_enabled is True  # seeded from the env log channel
    assert config.max_purge_amount == 50
    assert config.spam_threshold == 7
    assert config.blocked_terms == ("bad",)
    assert config.exempt_role_ids == (333,)
    assert config.escalation == ((3, 3600),)


def test_defaults_when_no_log_channel() -> None:
    config = default_guild_config(_settings(), guild_id=1)
    assert config.log_channel_id is None
    assert config.mod_log_enabled is False


def test_defaults_include_command_prefix() -> None:
    config = default_guild_config(_settings(command_prefix="!"), guild_id=1)
    assert config.command_prefix == "!"


# ------------------------------------------------------------ persistence


def test_json_round_trip() -> None:
    config = default_guild_config(_settings(), guild_id=7)
    restored = type(config).from_json(7, config.to_json())
    assert restored == config
    assert restored.guild_id == 7
    assert isinstance(restored.allowed_domains, tuple)
    assert isinstance(restored.escalation, tuple)


def test_from_json_rejects_unknown_fields() -> None:
    with pytest.raises(GuildConfigError):
        type(default_guild_config(_settings(), 1)).from_json(1, '{"guild_id": 1, "bogus": true}')


def test_from_json_rejects_malformed_json() -> None:
    with pytest.raises(GuildConfigError):
        type(default_guild_config(_settings(), 1)).from_json(1, "{not json")


# ------------------------------------------------------------ validation


def test_validate_setting_accepts_valid_values() -> None:
    assert validate_setting("spam_threshold", 5) == 5
    assert validate_setting("mention_user_threshold", 0) == 0
    assert validate_setting("max_purge_amount", 1000) == 1000
    assert validate_setting("timeout_duration_seconds", 28 * 24 * 60 * 60) == 28 * 24 * 60 * 60
    assert validate_setting("mod_log_enabled", True) is True
    assert validate_setting("log_channel_id", None) is None
    assert validate_setting("spam_action", "warn") == "warn"
    assert validate_setting("link_action", "allow") == "allow"


def test_validate_setting_rejects_negative_values() -> None:
    with pytest.raises(GuildConfigError):
        validate_setting("spam_threshold", -1)
    with pytest.raises(GuildConfigError):
        validate_setting("mention_user_threshold", -5)


def test_validate_setting_rejects_zero_where_invalid() -> None:
    with pytest.raises(GuildConfigError):
        validate_setting("spam_threshold", 0)  # spam needs at least 2
    with pytest.raises(GuildConfigError):
        validate_setting("max_purge_amount", 0)


def test_validate_setting_enforces_upper_limits() -> None:
    with pytest.raises(GuildConfigError):
        validate_setting("spam_threshold", 100_000)
    with pytest.raises(GuildConfigError):
        validate_setting("spam_window_seconds", 1_000_000)
    with pytest.raises(GuildConfigError):
        validate_setting("timeout_duration_seconds", 28 * 24 * 60 * 60 + 1)
    with pytest.raises(GuildConfigError):
        validate_setting("max_purge_amount", 1001)


def test_validate_setting_rejects_invalid_action() -> None:
    with pytest.raises(GuildConfigError):
        validate_setting("spam_action", "ban")  # ban is never an automated action
    with pytest.raises(GuildConfigError):
        validate_setting("link_action", "nuke")


def test_validate_setting_rejects_wrong_types() -> None:
    with pytest.raises(GuildConfigError):
        validate_setting("spam_threshold", "five")
    with pytest.raises(GuildConfigError):
        validate_setting("mod_log_enabled", "yes")
    with pytest.raises(GuildConfigError):
        validate_setting("log_channel_id", -3)


def test_validate_setting_unknown_name_rejected() -> None:
    with pytest.raises(GuildConfigError, match="Unknown"):
        validate_setting("not_a_setting", 1)


def test_validate_escalation_rejects_bad_spec() -> None:
    with pytest.raises(GuildConfigError):
        validate_setting("escalation", "3:3600,3:7200")  # duplicate count
    with pytest.raises(GuildConfigError):
        validate_setting("escalation", "3")  # missing duration
    with pytest.raises(GuildConfigError):
        validate_setting("escalation", "3:999999999")  # beyond 28 days


def test_validate_escalation_accepts_valid_spec() -> None:
    assert validate_setting("escalation", "5:43200,3:3600") == ((3, 3600), (5, 43200))
    assert validate_setting("escalation", "") == ()


def test_validate_command_prefix() -> None:
    assert validate_setting("command_prefix", "!") == "!"
    assert validate_setting("command_prefix", "++") == "++"
    with pytest.raises(GuildConfigError):
        validate_setting("command_prefix", "!!!!")  # too long
    with pytest.raises(GuildConfigError):
        validate_setting("command_prefix", "a b")  # spaces
    with pytest.raises(GuildConfigError):
        validate_setting("command_prefix", "@")  # mention syntax
    with pytest.raises(GuildConfigError):
        validate_setting("command_prefix", "")  # empty


# --------------------------------------------------------------- entities


def test_validate_entity_id_rejects_invalid() -> None:
    with pytest.raises(GuildConfigError):
        validate_entity_id("role ID", 0)
    with pytest.raises(GuildConfigError):
        validate_entity_id("role ID", -1)
    with pytest.raises(GuildConfigError):
        validate_entity_id("role ID", "abc")
    assert validate_entity_id("role ID", 555) == 555


def test_validate_list_entry_normalizes() -> None:
    assert validate_list_entry("blocked_terms", "  BadWord  ") == "badword"
    with pytest.raises(GuildConfigError):
        validate_list_entry("blocked_terms", "   ")
    with pytest.raises(GuildConfigError):
        validate_list_entry("blocked_terms", "x" * (MAX_ENTRY_LENGTH + 1))


def test_list_capacity_is_bounded() -> None:
    # The service caps lists at MAX_LIST_ENTRIES; the validator does too.
    from bot.configuration.guild import validate_id_list

    with pytest.raises(GuildConfigError):
        validate_id_list("exempt_user_ids", tuple(range(1, MAX_LIST_ENTRIES + 2)))
