"""Tests for the ``/automod`` command group (Phase 8).

The cog is a thin administrator-gated interface over the configuration
service: tests verify the permission gate (owner / administrator permission /
administrator role), the master switch (enable/disable), status output, and
the invites/raid configuration subcommands. No real Discord is contacted.
"""

from __future__ import annotations

from bot.cogs.automod import AutomodCog
from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.permissions.checks import PermissionChecker
from bot.services.guild_config import GuildConfigService
from bot.tests.fakes import (
    FakeBot,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    FakePermissions,
    FakeRole,
)


def _make_world():
    settings = Settings(token="test-token")
    bot = FakeBot()
    config_service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    permissions = PermissionChecker(bot, settings, config_service=config_service)
    cog = AutomodCog(bot, config_service)
    client = type("Client", (), {"permissions": permissions})()
    return {"settings": settings, "config_service": config_service, "cog": cog, "client": client}


def _guild() -> FakeGuild:
    bot_member = FakeMember(
        100_000,
        "riyxoen",
        roles=[FakeRole(900, "bot", 9)],
        bot=True,
        guild_permissions=FakePermissions(administrator=False),
    )
    return FakeGuild(10, owner_id=1, me=bot_member, members=[bot_member])


def _admin(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        2,
        "admin",
        roles=[FakeRole(100, "admin", 8)],
        guild=guild,
        guild_permissions=FakePermissions(administrator=True),
    )


def _regular(guild: FakeGuild) -> FakeMember:
    return FakeMember(4, "member", roles=[FakeRole(50, "user", 1)], guild=guild)


def _interaction(world, guild: FakeGuild, user: FakeMember) -> FakeInteraction:
    return FakeInteraction(guild=guild, user=user, client=world["client"])


async def _invoke(world, command, interaction, *args, **kwargs) -> None:
    # Command callbacks are unbound methods on the cog instance.
    await command.callback(world["cog"], interaction, *args, **kwargs)


# ---------------------------------------------------------------- gating


async def test_regular_member_denied() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _regular(guild))
    await _invoke(world, world["cog"].enable, interaction)
    assert "permission" in interaction.response.messages[0]
    assert world["config_service"].get(guild.id).automod_enabled is False


async def test_owner_can_enable() -> None:
    world = _make_world()
    guild = _guild()
    owner = FakeMember(1, "owner", roles=[FakeRole(100, "owner", 9)], guild=guild)
    interaction = _interaction(world, guild, owner)
    await _invoke(world, world["cog"].enable, interaction)
    assert world["config_service"].get(guild.id).automod_enabled is True
    assert "Updated" in interaction.response.messages[0]


async def test_administrator_permission_can_enable_and_disable() -> None:
    world = _make_world()
    guild = _guild()
    admin = FakeMember(
        2,
        "admin",
        roles=[FakeRole(100, "admin", 8)],
        guild=guild,
        guild_permissions=FakePermissions(administrator=True),
    )
    interaction = _interaction(world, guild, admin)
    await _invoke(world, world["cog"].enable, interaction)
    assert world["config_service"].get(guild.id).automod_enabled is True
    await _invoke(world, world["cog"].disable, interaction)
    assert world["config_service"].get(guild.id).automod_enabled is False


# ----------------------------------------------------------------- status


async def test_status_shows_detector_summary() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].status, interaction)
    text = interaction.response.messages[0]
    assert "Automod: disabled" in text
    assert "Spam" in text
    assert "Invites" in text
    assert "Raid" in text


async def test_status_denied_for_regular_member() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _regular(guild))
    await _invoke(world, world["cog"].status, interaction)
    assert "permission" in interaction.response.messages[0]


# ---------------------------------------------------------------- invites


async def test_invites_action_configured() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].invites_action, interaction, "delete")
    assert world["config_service"].get(guild.id).invite_action == "delete"
    assert "invite_action" in interaction.response.messages[0]


async def test_invites_allowed_add_and_list() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].invites_allowed, interaction, "add", "Welcome")
    config = world["config_service"].get(guild.id)
    assert config.invite_allowed_codes == ("welcome",)  # normalized lowercase
    await _invoke(world, world["cog"].invites_allowed, interaction, "list", None)
    assert "welcome" in interaction.response.messages[-1]


async def test_invites_allowed_duplicate_rejected() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].invites_allowed, interaction, "add", "welcome")
    await _invoke(world, world["cog"].invites_allowed, interaction, "add", "welcome")
    assert "already configured" in interaction.response.messages[-1]


async def test_invites_allowed_remove() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].invites_allowed, interaction, "add", "welcome")
    await _invoke(world, world["cog"].invites_allowed, interaction, "remove", "welcome")
    assert world["config_service"].get(guild.id).invite_allowed_codes == ()


async def test_invites_allowed_missing_code_rejected() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].invites_allowed, interaction, "add", None)
    assert "invite code is required" in interaction.response.messages[0]


# -------------------------------------------------------------------- raid


async def test_raid_configure_validated() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].raid_configure, interaction, 8, 15, "alert")
    config = world["config_service"].get(guild.id)
    assert config.raid_join_threshold == 8
    assert config.raid_window_seconds == 15
    assert config.raid_action == "alert"


async def test_raid_configure_rejects_bad_threshold() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin(guild))
    await _invoke(world, world["cog"].raid_configure, interaction, 1, 10, "timeout")
    assert "must be between" in interaction.response.messages[0]
    # Defaults unchanged.
    config = world["config_service"].get(guild.id)
    assert config.raid_join_threshold == 10


async def test_raid_configure_denied_for_regular_member() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _regular(guild))
    await _invoke(world, world["cog"].raid_configure, interaction, 8, 15, "alert")
    assert "permission" in interaction.response.messages[0]
