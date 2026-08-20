"""Tests for the centralized exemption checker.

Exemptions are evaluated before any detector runs, so detectors never repeat
this logic. Moderators, the server owner, bots, and configured users/roles/
channels are exempt; regular members are not. The checker reads the guild's
configuration snapshot (per-guild exemptions) rather than global settings.
"""

from __future__ import annotations

from bot.automod.exemptions import ExemptionChecker
from bot.configuration.automod import AutomodSettings
from bot.configuration.guild import default_guild_config
from bot.configuration.settings import Settings
from bot.permissions.checks import PermissionChecker
from bot.tests.fakes import (
    FakeBot,
    FakeChannel,
    FakeGuild,
    FakeMember,
    FakeMessage,
    FakePermissions,
    FakeRole,
)


def _settings(automod: AutomodSettings | None = None, **overrides) -> Settings:
    values: dict = {"token": "test-token", "automod": automod or AutomodSettings()}
    values.update(overrides)
    return Settings(**values)


def _checker(settings: Settings | None = None) -> ExemptionChecker:
    settings = settings or _settings()
    return ExemptionChecker(PermissionChecker(FakeBot(), settings))


def _config(settings: Settings | None = None, **overrides):
    """A per-guild config snapshot seeded from ``settings`` plus overrides."""
    from dataclasses import replace

    settings = settings or _settings()
    return replace(default_guild_config(settings, guild_id=10), **overrides)


def _guild(owner_id: int | None = 1) -> FakeGuild:
    return FakeGuild(10, owner_id=owner_id)


def _member(
    guild: FakeGuild,
    user_id: int = 3,
    *,
    roles: list[FakeRole] | None = None,
    permissions: FakePermissions | None = None,
    bot: bool = False,
) -> FakeMember:
    return FakeMember(
        user_id,
        "member",
        roles=roles or [FakeRole(101, "user", 3)],
        guild=guild,
        guild_permissions=permissions or FakePermissions(),
        bot=bot,
    )


def _message(
    guild: FakeGuild, member: FakeMember, channel: FakeChannel | None = None
) -> FakeMessage:
    channel = channel or FakeChannel(20, guild=guild)
    return FakeMessage(1, "hello", guild=guild, author=member, channel=channel)


def test_bot_user_exempt() -> None:
    checker = _checker()
    guild = _guild()
    member = _member(guild, bot=True)
    assert checker.is_exempt(_message(guild, member), _config()) is True


def test_server_owner_exempt() -> None:
    checker = _checker()
    guild = _guild(owner_id=1)
    owner = _member(guild, user_id=1)
    assert checker.is_exempt(_message(guild, owner), _config()) is True


def test_exempt_user_id() -> None:
    checker = _checker()
    guild = _guild()
    member = _member(guild, user_id=3)
    config = _config(exempt_user_ids=(3,))
    assert checker.is_exempt(_message(guild, member), config) is True
    # Without the exemption the same member is not exempt.
    assert checker.is_exempt(_message(guild, member), _config()) is False


def test_exempt_role_id() -> None:
    checker = _checker()
    guild = _guild()
    member = _member(guild, roles=[FakeRole(777, "trusted", 5)])
    config = _config(exempt_role_ids=(777,))
    assert checker.is_exempt(_message(guild, member), config) is True


def test_exempt_channel_id() -> None:
    checker = _checker()
    guild = _guild()
    member = _member(guild)
    exempt_channel = FakeChannel(42, guild=guild)
    config = _config(exempt_channel_ids=(42,))
    assert checker.is_exempt(_message(guild, member, exempt_channel), config) is True
    # A non-exempt channel for the same member is NOT exempt.
    assert checker.is_exempt(_message(guild, member), config) is False


def test_moderator_by_permission_exempt() -> None:
    checker = _checker()
    guild = _guild()
    member = _member(guild, permissions=FakePermissions(moderate_members=True))
    assert checker.is_exempt(_message(guild, member), _config()) is True


def test_moderator_by_configured_role_exempt() -> None:
    settings = _settings(moderator_role_ids=(500,))
    checker = _checker(settings)
    guild = _guild()
    member = _member(guild, roles=[FakeRole(500, "mod role", 8)])
    # The per-guild config seeds moderator roles from the environment.
    assert checker.is_exempt(_message(guild, member), _config(settings)) is True


def test_per_guild_exemptions_override_env_seeds() -> None:
    """Exemptions come from the guild config, not only the env template."""
    checker = _checker()
    guild = _guild()
    member = _member(guild, user_id=3)
    # Env has no exemptions; only the guild config grants one.
    assert checker.is_exempt(_message(guild, member), _config()) is False
    assert checker.is_exempt(_message(guild, member), _config(exempt_user_ids=(3,))) is True


def test_regular_member_not_exempt() -> None:
    checker = _checker()
    guild = _guild()
    member = _member(guild, user_id=99)
    assert checker.is_exempt(_message(guild, member), _config()) is False


def test_no_author_is_exempt() -> None:
    checker = _checker()
    guild = _guild()
    message = _message(guild, _member(guild))
    message.author = None  # type: ignore[assignment]
    assert checker.is_exempt(message, _config()) is True
