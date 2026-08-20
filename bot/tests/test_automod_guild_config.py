"""Per-guild configuration integration tests for the automod engine (Phase 5).

The engine must consume configuration through the configuration service:
a ``/config`` change (service update) takes effect on the next message, a
guild can disable automod for itself, exemptions come from the guild config,
and the per-guild detector-set cache stays bounded. No real Discord server
is contacted.
"""

from __future__ import annotations

from bot.automod.engine import MAX_DETECTOR_SETS, AutomodEngine
from bot.configuration.automod import AutomodSettings
from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.repository import MemoryCaseRepository
from bot.moderation.cases import STATUS_SUCCESS
from bot.permissions.checks import PermissionChecker
from bot.services.cases import CaseService
from bot.services.guild_config import GuildConfigService
from bot.services.moderation import ModerationService
from bot.tests.fakes import (
    FakeBot,
    FakeChannel,
    FakeGuild,
    FakeMember,
    FakeMessage,
    FakePermissions,
    FakeRole,
)


def _make_world(automod: AutomodSettings | None = None):
    settings = Settings(token="test-token", automod=automod or AutomodSettings(enabled=True))
    bot = FakeBot()
    case_service = CaseService(MemoryCaseRepository())
    config_service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    permissions = PermissionChecker(bot, settings, config_service=config_service)
    moderation_service = ModerationService(
        bot, case_service, settings=settings, permissions=permissions, config_service=config_service
    )
    engine = AutomodEngine(settings, case_service, moderation_service, permissions, config_service)
    config_service.add_invalidation_listener(engine.invalidate_guild)
    return engine, case_service, config_service


def _guild(guild_id: int = 10) -> FakeGuild:
    bot_member = FakeMember(
        100_000,
        "riyxoen",
        roles=[FakeRole(900, "bot", 9)],
        guild_permissions=FakePermissions(
            moderate_members=True, manage_messages=True, read_message_history=True
        ),
        bot=True,
    )
    channel = FakeChannel(20, "general")
    guild = FakeGuild(guild_id, owner_id=1, me=bot_member, members=[bot_member], channels=[channel])
    channel.guild = guild
    return guild


def _member(guild: FakeGuild, user_id: int = 3) -> FakeMember:
    return FakeMember(user_id, "user", roles=[FakeRole(101, "user", 3)], guild=guild)


def _message(guild: FakeGuild, member: FakeMember, content: str, message_id: int) -> FakeMessage:
    return FakeMessage(message_id, content, guild=guild, author=member, channel=guild.channels[0])


async def test_per_guild_automod_toggle_gates_enforcement() -> None:
    engine, case_service, config_service = _make_world()
    guild = _guild()
    member = _member(guild)
    config_service.update(guild.id, actor_user_id=1, changes={"automod_enabled": False})

    for i in range(5):
        await engine.handle_message(_message(guild, member, f"msg{i}", i))
    assert case_service.list_for_guild(guild.id).total == 0

    config_service.update(guild.id, actor_user_id=1, changes={"automod_enabled": True})
    messages = [_message(guild, member, f"go{i}", 100 + i) for i in range(5)]
    for message in messages:
        await engine.handle_message(message)

    page = case_service.list_for_guild(guild.id)
    assert page.total == 1
    assert page.items[0].status == STATUS_SUCCESS
    assert messages[-1].deleted is True


async def test_config_change_takes_effect_without_restart() -> None:
    """Lowering the spam threshold via the config service changes behavior."""
    engine, case_service, config_service = _make_world(
        AutomodSettings(enabled=True, spam_threshold=100, spam_window_seconds=5)
    )
    guild = _guild()
    member = _member(guild)

    for i in range(5):
        await engine.handle_message(_message(guild, member, f"quiet{i}", i))
    assert case_service.list_for_guild(guild.id).total == 0  # threshold 100 not reached

    config_service.update(guild.id, actor_user_id=1, changes={"spam_threshold": 3})
    messages = [_message(guild, member, f"loud{i}", 100 + i) for i in range(3)]
    for message in messages:
        await engine.handle_message(message)

    assert case_service.list_for_guild(guild.id).total == 1
    assert messages[-1].deleted is True


async def test_per_guild_exemptions_from_config() -> None:
    engine, case_service, config_service = _make_world()
    guild = _guild()
    member = _member(guild)
    config_service.add_exempt(guild.id, actor_user_id=1, kind="user", entity_id=member.id)

    for i in range(5):
        await engine.handle_message(_message(guild, member, "hi", i))
    assert case_service.list_for_guild(guild.id).total == 0


async def test_per_guild_exempt_channel_from_config() -> None:
    engine, case_service, config_service = _make_world()
    guild = _guild()
    member = _member(guild)
    channel = FakeChannel(42, "bot-spam", guild=guild)
    guild.channels.append(channel)
    config_service.add_exempt(guild.id, actor_user_id=1, kind="channel", entity_id=42)

    for i in range(5):
        await engine.handle_message(
            FakeMessage(i, "hi", guild=guild, author=member, channel=channel)
        )
    assert case_service.list_for_guild(guild.id).total == 0


async def test_invalidation_rebuilds_detectors_with_new_action() -> None:
    engine, case_service, config_service = _make_world(
        AutomodSettings(enabled=True, link_action="allow")
    )
    guild = _guild()
    member = _member(guild)
    message = _message(guild, member, "see https://evil.example.org/x", 1)
    await engine.handle_message(message)
    assert message.deleted is False

    config_service.update(guild.id, actor_user_id=1, changes={"link_action": "delete"})
    message2 = _message(guild, member, "see https://evil.example.org/y", 2)
    await engine.handle_message(message2)
    assert message2.deleted is True


async def test_detector_set_cache_is_bounded() -> None:
    engine, _case_service, _config_service = _make_world()
    guilds = [_guild(guild_id=100 + i) for i in range(MAX_DETECTOR_SETS + 5)]
    for guild in guilds:
        member = _member(guild)
        await engine.handle_message(_message(guild, member, "hello", 1))
    assert len(engine._detector_sets) <= MAX_DETECTOR_SETS


async def test_invalidate_guild_drops_cached_detectors() -> None:
    engine, _case_service, config_service = _make_world()
    guild = _guild()
    engine._get_detectors(guild.id, config_service.get(guild.id))
    assert guild.id in engine._detector_sets

    engine.invalidate_guild(guild.id)
    assert guild.id not in engine._detector_sets


async def test_other_guild_configs_are_independent() -> None:
    engine, case_service, config_service = _make_world(
        AutomodSettings(enabled=True, spam_threshold=100)
    )
    guild_a = _guild(10)
    guild_b = _guild(20)
    member_a = _member(guild_a)
    member_b = _member(guild_b)
    config_service.update(guild_a.id, actor_user_id=1, changes={"spam_threshold": 3})

    # Guild A's low threshold fires; guild B's high threshold does not.
    for i in range(3):
        await engine.handle_message(_message(guild_a, member_a, f"a{i}", i))
    for i in range(3):
        await engine.handle_message(_message(guild_b, member_b, f"b{i}", 100 + i))

    assert case_service.list_for_guild(10).total == 1
    assert case_service.list_for_guild(20).total == 0
