"""Unit tests for the custom-role service (Phase 6).

Tests the service in isolation (no message layer): enable/idempotency, role
creation without duplicates, rename/color with validation, the disabled-state
guard, permission and hierarchy failures, missing roles, and Discord API
error mapping. Uses fakes only — no real Discord.
"""

from __future__ import annotations

import pytest
from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.moderation.errors import (
    CustomRoleDisabledError,
    InvalidHexColorError,
    InvalidRoleNameError,
    MissingBotPermissionError,
    MissingModeratorPermissionError,
)
from bot.moderation.validation import validate_hex_color, validate_role_name
from bot.permissions.checks import PermissionChecker
from bot.services.custom_roles import CustomRoleService
from bot.services.guild_config import GuildConfigService
from bot.tests.fakes import (
    FakeBot,
    FakeGuild,
    FakeMember,
    FakePermissions,
    FakeRole,
    forbidden,
)


def _make_world():
    settings = Settings(token="test-token")
    bot = FakeBot()
    guild = FakeGuild(
        10,
        owner_id=1,
        me=FakeMember(
            100_000,
            "riyxoen",
            roles=[FakeRole(900, "bot", 9)],
            guild_permissions=FakePermissions(manage_roles=True),
            bot=True,
        ),
    )
    guild.me.guild = guild
    config_service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    permissions = PermissionChecker(bot, settings, config_service=config_service)
    service = CustomRoleService(settings, permissions, config_service)
    return {
        "settings": settings,
        "bot": bot,
        "guild": guild,
        "config_service": config_service,
        "permissions": permissions,
        "service": service,
    }


def _actor(guild: FakeGuild, *, manage_roles: bool = True, position: int = 8) -> FakeMember:
    return FakeMember(
        2,
        "actor",
        roles=[FakeRole(100, "actor", position)],
        guild=guild,
        guild_permissions=FakePermissions(manage_roles=manage_roles),
    )


# ------------------------------------------------------------ validation


def test_validate_hex_color_accepts_and_normalizes() -> None:
    assert validate_hex_color("#ff0000") == "ff0000"
    assert validate_hex_color("#FF0000") == "ff0000"
    assert validate_hex_color("00ff00") == "00ff00"
    assert validate_hex_color("  #5865F2  ") == "5865f2"


def test_validate_hex_color_rejects_malformed() -> None:
    for bad in ("", "red", "#ff00", "#ff00000", "ff000", "notacolor", "#gg0000"):
        with pytest.raises(InvalidHexColorError):
            validate_hex_color(bad)


def test_validate_role_name() -> None:
    assert validate_role_name("  Cool Role  ") == "Cool Role"
    assert validate_role_name("Riyxoen") == "Riyxoen"
    with pytest.raises(InvalidRoleNameError):
        validate_role_name("")
    with pytest.raises(InvalidRoleNameError):
        validate_role_name("   ")
    with pytest.raises(InvalidRoleNameError):
        validate_role_name("x" * 101)
    with pytest.raises(InvalidRoleNameError):
        validate_role_name("bad@name")
    with pytest.raises(InvalidRoleNameError):
        validate_role_name("bad#name")
    with pytest.raises(InvalidRoleNameError):
        validate_role_name("bad:name")


# ---------------------------------------------------------------- enable


async def test_enable_creates_role_and_persists_state() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)

    result = await world["service"].enable(guild, actor)

    assert result is True
    config = world["config_service"].get(guild.id)
    assert config.custom_roles_enabled is True
    assert config.custom_role_id is not None
    role = guild.get_role(config.custom_role_id)
    assert role is not None
    assert role.name == "Eclipse Custom"


async def test_enable_is_idempotent_and_never_duplicates() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)

    await world["service"].enable(guild, actor)
    roles_before = len(guild.roles)
    result = await world["service"].enable(guild, actor)

    assert result is False  # already enabled
    assert len(guild.roles) == roles_before


async def test_enable_requires_manage_roles() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild, manage_roles=False)

    with pytest.raises(MissingModeratorPermissionError):
        await world["service"].enable(guild, actor)
    assert world["config_service"].get(guild.id).custom_roles_enabled is False


async def test_enable_requires_bot_manage_roles() -> None:
    world = _make_world()
    guild = world["guild"]
    guild.me.guild_permissions = FakePermissions()  # bot has no permissions
    actor = _actor(guild)

    with pytest.raises(MissingBotPermissionError):
        await world["service"].enable(guild, actor)


async def test_enable_recreates_deleted_role_without_duplicate() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)
    role = world["service"].managed_role(guild)
    guild.roles.remove(role)  # role deleted behind the bot's back

    result = await world["service"].enable(guild, actor)

    assert result is True
    new_role = world["service"].managed_role(guild)
    assert new_role is not None and new_role.id != role.id


# ---------------------------------------------------------------- rename


async def test_rename_requires_enabled_first() -> None:
    world = _make_world()
    guild = world["guild"]
    with pytest.raises(CustomRoleDisabledError):
        await world["service"].rename(guild, _actor(guild), "Cool")


async def test_rename_updates_role_name() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)

    await world["service"].rename(guild, actor, "Super Role")

    assert world["service"].managed_role(guild).name == "Super Role"


async def test_rename_validates_name() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)

    with pytest.raises(InvalidRoleNameError):
        await world["service"].rename(guild, actor, "bad#name")


async def test_rename_missing_role_reports_clearly() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)
    guild.roles.remove(world["service"].managed_role(guild))

    with pytest.raises(CustomRoleDisabledError):
        await world["service"].rename(guild, actor, "Cool")


async def test_rename_never_edits_role_above_bot() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)
    # The managed role is moved above the bot's highest role.
    role = world["service"].managed_role(guild)
    role.position = 99

    with pytest.raises(MissingBotPermissionError):
        await world["service"].rename(guild, actor, "Too High")


# ---------------------------------------------------------------- color


async def test_color_updates_and_normalizes() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)

    await world["service"].color(guild, actor, "#FF0000")

    assert world["service"].managed_role(guild).colour == 0xFF0000


async def test_color_rejects_malformed_hex() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)

    with pytest.raises(InvalidHexColorError):
        await world["service"].color(guild, actor, "blurple")


async def test_color_requires_enabled_first() -> None:
    world = _make_world()
    guild = world["guild"]
    with pytest.raises(CustomRoleDisabledError):
        await world["service"].color(guild, _actor(guild), "#00ff00")


# --------------------------------------------------------- discord failures


async def test_discord_edit_failure_maps_to_safe_error() -> None:
    world = _make_world()
    guild = world["guild"]
    actor = _actor(guild)
    await world["service"].enable(guild, actor)
    world["service"].managed_role(guild).fail_edit(forbidden("permission race"))

    with pytest.raises(MissingBotPermissionError):
        await world["service"].rename(guild, actor, "Cool")
