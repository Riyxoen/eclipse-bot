"""Tests for the centralized permission checker (guild, moderator, bot,
target, hierarchy, and state checks)."""

from __future__ import annotations

import pytest
from bot.configuration.settings import Settings
from bot.moderation.errors import (
    HierarchyError,
    InvalidTargetError,
    MissingAdministratorPermissionError,
    MissingBotPermissionError,
    MissingModeratorPermissionError,
    NotInGuildError,
)
from bot.permissions.checks import PermissionChecker
from bot.tests.fakes import FakeBot, FakeChannel, FakeGuild, FakeMember, FakePermissions, FakeRole


def _settings(**overrides) -> Settings:
    values = {
        "token": "test-token",
        "moderator_role_ids": (),
        "max_purge_amount": 100,
    }
    values.update(overrides)
    return Settings(**values)


def _checker(bot: FakeBot | None = None, settings: Settings | None = None) -> PermissionChecker:
    return PermissionChecker(bot or FakeBot(), settings or _settings())


def _member(
    id: int,
    *roles: FakeRole,
    perms: FakePermissions | None = None,
    guild: FakeGuild | None = None,
) -> FakeMember:
    return FakeMember(
        id, roles=list(roles), guild_permissions=perms or FakePermissions(), guild=guild
    )


def _guild_with_bot(bot_id: int = 100_000, owner_id: int = 1) -> FakeGuild:
    bot_member = _member(bot_id, FakeRole(900, "bot", 9), perms=FakePermissions(kick_members=True))
    guild = FakeGuild(10, owner_id=owner_id, me=bot_member, members=[bot_member])
    return guild


# ------------------------------------------------------------ guild context


def test_require_guild_rejects_none() -> None:
    with pytest.raises(NotInGuildError):
        _checker().require_guild(None)


def test_require_guild_accepts_guild() -> None:
    _checker().require_guild(FakeGuild(1))  # must not raise


# ------------------------------------------------------------- moderator perm


def test_moderator_with_permission_allowed() -> None:
    mod = _member(2, perms=FakePermissions(moderate_members=True))
    _checker().require_moderator(mod, "warn")


def test_moderator_without_permission_denied() -> None:
    mod = _member(2)
    with pytest.raises(MissingModeratorPermissionError):
        _checker().require_moderator(mod, "kick")


def test_moderator_with_configured_role_allowed() -> None:
    mod = _member(2, FakeRole(555, "staff", 5))
    checker = _checker(settings=_settings(moderator_role_ids=(555,)))
    checker.require_moderator(mod, "ban")  # no ban permission, but staff role


def test_moderator_with_non_configured_role_denied() -> None:
    mod = _member(2, FakeRole(999, "regular", 5))
    checker = _checker(settings=_settings(moderator_role_ids=(555,)))
    with pytest.raises(MissingModeratorPermissionError):
        checker.require_moderator(mod, "kick")


def test_moderator_none_is_guild_error() -> None:
    with pytest.raises(NotInGuildError):
        _checker().require_moderator(None, "warn")


# ------------------------------------------------------- is_moderator bool


def test_is_moderator_true_with_permission() -> None:
    mod = _member(2, perms=FakePermissions(moderate_members=True))
    assert _checker().is_moderator(mod, "warn") is True


def test_is_moderator_true_with_configured_role() -> None:
    mod = _member(2, FakeRole(555, "staff", 5))
    checker = _checker(settings=_settings(moderator_role_ids=(555,)))
    assert checker.is_moderator(mod, "ban") is True


def test_is_moderator_false_for_regular_member() -> None:
    mod = _member(2)
    assert _checker().is_moderator(mod, "warn") is False


def test_is_moderator_false_for_none() -> None:
    assert _checker().is_moderator(None, "warn") is False


# --------------------------------------------------------------- bot perms


async def test_bot_guild_permission_required() -> None:
    guild = _guild_with_bot()
    await _checker().require_bot_permissions(guild, "kick")  # bot has kick_members


async def test_bot_missing_permission_denied() -> None:
    guild = _guild_with_bot()
    guild.me.guild_permissions = FakePermissions()  # strip all permissions
    with pytest.raises(MissingBotPermissionError):
        await _checker().require_bot_permissions(guild, "ban")


async def test_bot_purge_uses_channel_permissions() -> None:
    guild = _guild_with_bot()
    guild.me.guild_permissions = FakePermissions()  # guild-level lacks manage_messages
    channel = FakeChannel(20, guild=guild)  # channel grants manage_messages + read_history
    await _checker().require_bot_permissions(guild, "purge", channel=channel)


async def test_bot_purge_denied_without_channel_permissions() -> None:
    guild = _guild_with_bot()
    channel = FakeChannel(
        20, guild=guild, permissions_for_bot=FakePermissions(manage_messages=False)
    )
    with pytest.raises(MissingBotPermissionError):
        await _checker().require_bot_permissions(guild, "purge", channel=channel)


async def test_bot_delete_uses_channel_permissions() -> None:
    guild = _guild_with_bot()
    guild.me.guild_permissions = FakePermissions()  # guild-level lacks manage_messages
    channel = FakeChannel(20, guild=guild)  # channel grants manage_messages
    await _checker().require_bot_permissions(guild, "delete", channel=channel)


async def test_bot_delete_denied_without_channel_permissions() -> None:
    guild = _guild_with_bot()
    channel = FakeChannel(
        20, guild=guild, permissions_for_bot=FakePermissions(manage_messages=False)
    )
    with pytest.raises(MissingBotPermissionError):
        await _checker().require_bot_permissions(guild, "delete", channel=channel)


# ------------------------------------------------------------------- target


def test_target_none_rejected() -> None:
    guild = _guild_with_bot()
    with pytest.raises(InvalidTargetError):
        _checker().require_target(None, guild, _member(2), member_required=True)


def test_target_not_a_member_rejected() -> None:
    guild = _guild_with_bot()
    from bot.tests.fakes import FakeUser

    with pytest.raises(InvalidTargetError, match="member"):
        _checker().require_target(FakeUser(50), guild, _member(2), member_required=True)


def test_target_bot_itself_rejected() -> None:
    guild = _guild_with_bot()
    bot_member = guild.me
    with pytest.raises(InvalidTargetError, match="bot"):
        _checker().require_target(bot_member, guild, _member(2), member_required=True)


def test_target_owner_rejected() -> None:
    guild = _guild_with_bot(owner_id=7)
    owner = _member(7)
    with pytest.raises(InvalidTargetError, match="owner"):
        _checker().require_target(owner, guild, _member(2), member_required=True)


def test_target_moderator_self_rejected() -> None:
    guild = _guild_with_bot()
    mod = _member(2)
    with pytest.raises(InvalidTargetError, match="yourself"):
        _checker().require_target(mod, guild, mod, member_required=True)


def test_valid_member_target_accepted() -> None:
    guild = _guild_with_bot()
    _checker().require_target(_member(3), guild, _member(2), member_required=True)


# --------------------------------------------------------------- hierarchy


async def test_hierarchy_rejects_equal_roles() -> None:
    guild = _guild_with_bot()
    mod = _member(2, FakeRole(100, "mod", 5))
    target = _member(3, FakeRole(101, "other", 5))  # same position as mod
    with pytest.raises(HierarchyError, match="role is higher than or equal"):
        await _checker().require_hierarchy(mod, target, guild)


async def test_hierarchy_rejects_higher_target() -> None:
    guild = _guild_with_bot()
    mod = _member(2, FakeRole(100, "mod", 5))
    target = _member(3, FakeRole(101, "admin", 8))
    with pytest.raises(HierarchyError):
        await _checker().require_hierarchy(mod, target, guild)


async def test_hierarchy_rejects_when_bot_role_too_low() -> None:
    guild = FakeGuild(10, owner_id=1, me=_member(100_000, FakeRole(900, "bot", 2)))
    mod = _member(2, FakeRole(100, "mod", 5))
    target = _member(3, FakeRole(101, "user", 4))  # above the bot's role
    with pytest.raises(HierarchyError, match="bot's role"):
        await _checker().require_hierarchy(mod, target, guild)


async def test_hierarchy_passes_when_both_above_target() -> None:
    guild = _guild_with_bot()  # bot role position 9
    mod = _member(2, FakeRole(100, "mod", 7))
    target = _member(3, FakeRole(101, "user", 3))
    await _checker().require_hierarchy(mod, target, guild)  # must not raise


# -------------------------------------------------------------------- state


def test_state_rejects_oversized_purge() -> None:
    checker = _checker(settings=_settings(max_purge_amount=50))
    from bot.moderation.errors import InvalidPurgeAmountError

    with pytest.raises(InvalidPurgeAmountError):
        checker.require_state("purge", purge_amount=51)


def test_state_accepts_valid_purge() -> None:
    checker = _checker(settings=_settings(max_purge_amount=50))
    checker.require_state("purge", purge_amount=50)  # must not raise


def test_state_rejects_invalid_timeout_duration() -> None:
    from bot.moderation.errors import InvalidDurationError

    with pytest.raises(InvalidDurationError):
        _checker().require_state("timeout", duration_seconds=0)


# ----------------------------------------------------------- administrator


def _admin_guild(owner_id: int = 1) -> FakeGuild:
    return FakeGuild(10, owner_id=owner_id)


def test_is_administrator_true_for_owner() -> None:
    guild = _admin_guild(owner_id=7)
    owner = _member(7, guild=guild)
    assert _checker().is_administrator(owner) is True


def test_is_administrator_true_with_administrator_permission() -> None:
    guild = _admin_guild()
    admin = _member(2, perms=FakePermissions(administrator=True), guild=guild)
    assert _checker().is_administrator(admin) is True


def test_is_administrator_false_for_moderator_only() -> None:
    """A regular moderator never inherits admin automatically (Phase 5)."""
    guild = _admin_guild()
    mod = _member(2, perms=FakePermissions(moderate_members=True), guild=guild)
    assert _checker().is_moderator(mod, "warn") is True
    assert _checker().is_administrator(mod) is False


def test_is_administrator_false_for_regular_member() -> None:
    guild = _admin_guild()
    assert _checker().is_administrator(_member(3, guild=guild)) is False


def test_require_administrator_raises_for_regular_member() -> None:
    guild = _admin_guild()
    with pytest.raises(MissingAdministratorPermissionError):
        _checker().require_administrator(_member(3, guild=guild), guild)


def test_require_administrator_raises_outside_guild() -> None:
    with pytest.raises(NotInGuildError):
        _checker().require_administrator(_member(2), None)


def test_is_administrator_false_for_none() -> None:
    assert _checker().is_administrator(None) is False


# ------------------------------------------------- per-guild config lookup


def _config_aware_checker(settings: Settings, guild: FakeGuild):
    """A checker with a per-guild config service seeded from ``settings``."""
    from bot.database.config_repository import MemoryGuildConfigRepository
    from bot.services.guild_config import GuildConfigService

    repository = MemoryGuildConfigRepository()
    service = GuildConfigService(repository, settings=settings)
    service.add_role(guild.id, actor_user_id=1, kind="moderator", role_id=555)
    service.add_role(guild.id, actor_user_id=1, kind="administrator", role_id=777)
    return PermissionChecker(FakeBot(), settings, config_service=service)


def test_is_moderator_uses_per_guild_roles() -> None:
    guild = _admin_guild()
    settings = _settings()  # env has NO moderator roles
    checker = _config_aware_checker(settings, guild)
    staff = _member(2, FakeRole(555, "staff", 5), guild=guild)
    assert checker.is_moderator(staff, "ban") is True


def test_is_administrator_uses_per_guild_admin_roles() -> None:
    guild = _admin_guild()
    settings = _settings()
    checker = _config_aware_checker(settings, guild)
    admin = _member(2, FakeRole(777, "admins", 8), guild=guild)
    assert checker.is_administrator(admin) is True


def test_per_guild_max_purge_applies_when_guild_provided() -> None:
    from bot.database.config_repository import MemoryGuildConfigRepository
    from bot.moderation.errors import InvalidPurgeAmountError
    from bot.services.guild_config import GuildConfigService

    settings = _settings(max_purge_amount=100)
    guild = FakeGuild(10, owner_id=1)
    service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    service.update(guild.id, actor_user_id=1, changes={"max_purge_amount": 5})
    checker = PermissionChecker(FakeBot(), settings, config_service=service)

    checker.require_state("purge", purge_amount=5, guild=guild)  # allowed
    with pytest.raises(InvalidPurgeAmountError):
        checker.require_state("purge", purge_amount=6, guild=guild)


def test_state_falls_back_to_env_max_without_guild() -> None:
    from bot.moderation.errors import InvalidPurgeAmountError

    checker = _checker(settings=_settings(max_purge_amount=50))
    checker.require_state("purge", purge_amount=50)  # env max applies
    with pytest.raises(InvalidPurgeAmountError):
        checker.require_state("purge", purge_amount=51)
