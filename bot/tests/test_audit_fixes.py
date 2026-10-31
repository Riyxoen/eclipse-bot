"""Regression tests for the medium/low severity audit fixes.

Covers:

- one canonical command-prefix rule shared by ``RIYXOEN_COMMAND_PREFIX`` and
  ``/config prefix`` (the loader used to accept 4 characters while the guild
  validator rejected anything above 3);
- a well-formed ``.env.example``;
- ``/config view`` no longer rendering "nevers" for a zero warning window;
- ``·warnings`` honouring the configured warning window and listing every
  warning instead of only the first page;
- the ``/ping`` formatter and the admin-command helpers having exactly one
  definition.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from bot.configuration.display import format_config_view
from bot.configuration.errors import GuildConfigError
from bot.configuration.guild import default_guild_config, validate_setting
from bot.configuration.loader import load_settings
from bot.configuration.prefix import validate_command_prefix
from bot.configuration.settings import Settings
from bot.core.errors import ConfigError
from bot.moderation.cases import STATUS_CLEARED, STATUS_SUCCESS, CaseRecord, utc_now
from bot.tests.fakes import FakeMember, FakePermissions, FakeRole
from bot.tests.test_new_features import _dispatch, _message, _replies, _target, _world

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GUILD_ID = 10


# --------------------------------------------------------------- prefix rule


@pytest.mark.parametrize("prefix", ["!", "++", "·", "??"])
def test_prefix_rule_accepts_valid_prefixes(prefix: str) -> None:
    """Both entry points accept the same valid prefixes."""
    assert validate_command_prefix(prefix) == prefix
    assert validate_setting("command_prefix", prefix) == prefix


@pytest.mark.parametrize("prefix", ["!!!!", "a b", "@", "#", "/", "<", ">", ":", ""])
def test_prefix_rule_rejects_invalid_prefixes(prefix: str) -> None:
    """Both entry points reject the same prefixes, with the same limits."""
    with pytest.raises(ValueError):
        validate_command_prefix(prefix)
    with pytest.raises(GuildConfigError):
        validate_setting("command_prefix", prefix)


def test_loader_rejects_four_character_prefix(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment loader enforces the same 3-character limit as /config."""
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_COMMAND_PREFIX", "....")

    with pytest.raises(ConfigError, match="RIYXOEN_COMMAND_PREFIX"):
        load_settings()


def test_loader_rejects_discord_syntax_prefix(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prefix that collides with Discord syntax (/ for slash commands) fails fast."""
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_COMMAND_PREFIX", "/")

    with pytest.raises(ConfigError, match="RIYXOEN_COMMAND_PREFIX"):
        load_settings()


def test_loader_accepts_multi_character_prefix(
    fake_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid multi-character prefix still works end to end."""
    monkeypatch.setenv("DISCORD_TOKEN", fake_token)
    monkeypatch.setenv("RIYXOEN_COMMAND_PREFIX", "++")

    assert load_settings().command_prefix == "++"


# --------------------------------------------------------------- .env.example


def test_env_example_is_well_formed() -> None:
    """The template has no stray markers and documents the real prefix rule."""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "[TEMPLATE]" not in text
    assert "1-4 non-space characters" not in text
    assert "1-3 non-space characters" in text


# ------------------------------------------------------------ config display


def _all_config_pages(config) -> str:
    return "\n".join(format_config_view(config, None, page=page) for page in range(1, 5))


def test_config_view_reports_warning_expiry() -> None:
    """A zero warning window renders "never expires", never "nevers"."""
    config = default_guild_config(Settings(token="test-token"), 1)

    text = _all_config_pages(config)
    assert "nevers" not in text
    assert "Warning expiry: 604800s" in text

    never_expires = config.replace(warning_window_seconds=0)
    text = _all_config_pages(never_expires)
    assert "nevers" not in text
    assert "Warning expiry: never expires" in text


# ------------------------------------------------------------------- warnings


def _warn_case(target_id: int, created_at, status: str = STATUS_SUCCESS) -> CaseRecord:
    return CaseRecord(
        guild_id=GUILD_ID,
        target_user_id=target_id,
        moderator_user_id=3,
        action="warn",
        reason="spam",
        created_at=created_at,
        status=status,
    )


async def test_warnings_uses_window_and_lists_every_case() -> None:
    """`·warnings` counts within the window and lists all warnings, not page 1."""
    world = _world()
    guild = world["guild"]
    target = _target(guild, user_id=21)
    moderator = FakeMember(
        3,
        "mod",
        roles=[FakeRole(101, "mod", 7)],
        guild=guild,
        guild_permissions=FakePermissions(moderate_members=True),
    )
    now = utc_now()
    case_service = world["case_service"]
    case_service.create(_warn_case(target.id, now))
    case_service.create(_warn_case(target.id, now - timedelta(days=30)))
    case_service.create(_warn_case(target.id, now, status=STATUS_CLEARED))

    await _dispatch(world, _message(world, moderator, f"·warnings {target.mention}"))
    replies = " ".join(_replies(world))

    assert "Warnings for" in replies
    # Only the fresh, successful warning counts as active; the 30-day-old one
    # has expired and the cleared one never counts again.
    assert "1 active warning(s)" in replies
    assert "window 604800s" in replies
    assert "Active" in replies
    assert "Expired" in replies
    assert "Cleared" in replies


# ------------------------------------------------------------- shared helpers


def test_ping_helper_has_a_single_definition() -> None:
    """`/ping` and `/health ping` share one formatter."""
    from bot.cogs.general import build_ping_response as general_ping
    from bot.cogs.health import build_ping_response as health_ping

    assert general_ping is health_ping


def test_admin_command_helpers_are_shared() -> None:
    """`/config` and `/automod` use the shared admin mixin, not copies."""
    from bot.cogs.admin import AdminCommandMixin
    from bot.cogs.automod import AutomodCog
    from bot.cogs.config import ConfigCog

    assert ConfigCog._apply is AdminCommandMixin._apply
    assert AutomodCog._apply is AdminCommandMixin._apply
    assert ConfigCog._require_admin is AdminCommandMixin._require_admin
    assert AutomodCog._require_admin is AdminCommandMixin._require_admin
