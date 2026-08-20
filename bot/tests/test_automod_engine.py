"""Tests for the automated moderation engine pipeline.

The engine is tested end to end against the real moderation service, case
service (memory repository), and permission checker, using lightweight
Discord fakes — no real Discord server is ever contacted. Focus: detection ->
enforcement -> case creation, cooldowns, escalation, exemptions, failure
handling, and memory bounds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.automod.engine import AutomodEngine
from bot.configuration.automod import AutomodSettings
from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.repository import MemoryCaseRepository
from bot.moderation.cases import STATUS_FAILED, STATUS_SUCCESS, CaseRecord
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
    forbidden,
    not_found,
)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _enabled(**overrides) -> AutomodSettings:
    values: dict = {"enabled": True}
    values.update(overrides)
    return AutomodSettings(**values)


def _make_guild() -> FakeGuild:
    bot_member = FakeMember(
        100_000,
        "riyxoen",
        roles=[FakeRole(900, "bot", 9)],
        guild_permissions=FakePermissions(
            moderate_members=True,
            kick_members=True,
            ban_members=True,
            manage_messages=True,
            read_message_history=True,
        ),
        bot=True,
    )
    channel = FakeChannel(20, "general")
    guild = FakeGuild(10, owner_id=1, me=bot_member, members=[bot_member], channels=[channel])
    channel.guild = guild
    return guild


def _member(guild: FakeGuild, user_id: int = 3) -> FakeMember:
    return FakeMember(user_id, "user", roles=[FakeRole(101, "user", 3)], guild=guild)


def _make_engine_full(automod: AutomodSettings, *, clock: FakeClock | None = None):
    bot = FakeBot()
    settings = Settings(token="test-token", automod=automod)
    case_service = CaseService(MemoryCaseRepository())
    # Per-guild config is seeded from ``settings.automod``, so the existing
    # test semantics are preserved: the engine reads each guild's snapshot
    # through the config service instead of the frozen env config.
    config_service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    moderation_service = ModerationService(
        bot, case_service, settings=settings, config_service=config_service
    )
    permissions = PermissionChecker(bot, settings, config_service=config_service)
    engine = AutomodEngine(
        settings, case_service, moderation_service, permissions, config_service, clock=clock
    )
    return engine, case_service


def _message(
    guild: FakeGuild,
    member: FakeMember,
    content: str,
    *,
    message_id: int = 1,
    channel: FakeChannel | None = None,
    mentions: list | None = None,
    role_mentions: list | None = None,
    everyone: bool = False,
) -> FakeMessage:
    channel = channel or guild.channels[0]
    return FakeMessage(
        message_id,
        content,
        guild=guild,
        author=member,
        channel=channel,
        mentions=mentions or [],
        role_mentions=role_mentions or [],
        mention_everyone=everyone,
    )


# ------------------------------------------------------------------ gating


async def test_disabled_engine_does_nothing() -> None:
    engine, case_service = _make_engine_full(AutomodSettings())  # enabled=False
    guild = _make_guild()
    member = _member(guild)
    for _ in range(10):
        await engine.handle_message(_message(guild, member, "spam spam spam"))
    assert case_service.list_for_guild(guild.id).total == 0


async def test_normal_message_no_detection() -> None:
    engine, case_service = _make_engine_full(_enabled())
    guild = _make_guild()
    member = _member(guild)
    await engine.handle_message(_message(guild, member, "how is everyone today?"))
    assert case_service.list_for_guild(guild.id).total == 0


async def test_bot_authors_never_analyzed() -> None:
    engine, case_service = _make_engine_full(_enabled())
    guild = _make_guild()
    bot_member = guild.me
    for _ in range(10):
        await engine.handle_message(_message(guild, bot_member, "beep boop"))
    assert case_service.list_for_guild(guild.id).total == 0


async def test_empty_content_never_analyzed() -> None:
    engine, case_service = _make_engine_full(_enabled())
    guild = _make_guild()
    member = _member(guild)
    await engine.handle_message(_message(guild, member, ""))
    assert case_service.list_for_guild(guild.id).total == 0


# ------------------------------------------------------------ enforcement


async def test_spam_burst_deletes_message_and_creates_automated_case() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(spam_threshold=5, spam_window_seconds=5, spam_action="delete"), clock=clock
    )
    guild = _make_guild()
    member = _member(guild)
    # Distinct content per message so only the frequency detector fires.
    messages = [_message(guild, member, f"msg{i}", message_id=i) for i in range(5)]
    for message in messages:
        await engine.handle_message(message)
        clock.advance(1)

    assert messages[-1].deleted is True
    assert all(not m.deleted for m in messages[:-1])
    page = case_service.list_for_guild(guild.id)
    assert page.total == 1
    record = page.items[0]
    assert record.action == "delete"
    assert record.automated is True
    assert record.detector == "spam"
    assert record.status == STATUS_SUCCESS
    assert "Automated moderation" in record.reason
    assert record.target_user_id == member.id
    assert record.moderator_user_id == guild.me.id


async def test_spam_action_warn_creates_warn_case_and_dms() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(spam_threshold=3, spam_window_seconds=5, spam_action="warn"), clock=clock
    )
    guild = _make_guild()
    member = _member(guild)
    for i in range(3):
        await engine.handle_message(_message(guild, member, "hi", message_id=i))
        clock.advance(1)

    page = case_service.list_for_guild(guild.id)
    assert page.total == 1
    record = page.items[0]
    assert record.action == "warn"
    assert record.automated is True
    assert record.detector == "spam"
    assert any("warn" in message and "spam" in message for message in member.sent)


async def test_spam_action_timeout_applies_default_duration() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            spam_threshold=3,
            spam_window_seconds=5,
            spam_action="timeout",
            timeout_duration_seconds=1800,
        ),
        clock=clock,
    )
    guild = _make_guild()
    member = _member(guild)
    for i in range(3):
        await engine.handle_message(_message(guild, member, "hi", message_id=i))
        clock.advance(1)

    record = case_service.list_for_guild(guild.id).items[0]
    assert record.action == "timeout"
    assert record.duration_seconds == 1800
    assert record.automated is True
    assert member.timed_out_until is not None


# --------------------------------------------------------------- cooldowns


async def test_cooldown_prevents_repeat_enforcement_for_same_burst() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            spam_threshold=5,
            spam_window_seconds=5,
            spam_action="delete",
            enforcement_cooldown_seconds=60,
        ),
        clock=clock,
    )
    guild = _make_guild()
    member = _member(guild)
    burst = [_message(guild, member, f"msg{i}", message_id=i) for i in range(8)]
    for message in burst:
        await engine.handle_message(message)
        clock.advance(1)

    # Only the first burst produced one case; the rest hit the cooldown.
    assert case_service.list_for_guild(guild.id).total == 1
    assert burst[4].deleted is True
    assert all(not message.deleted for message in burst[5:])


async def test_cooldown_expires_and_allows_new_enforcement() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            spam_threshold=3,
            spam_window_seconds=5,
            spam_action="delete",
            enforcement_cooldown_seconds=10,
        ),
        clock=clock,
    )
    guild = _make_guild()
    member = _member(guild)
    first_burst = [_message(guild, member, "go", message_id=i) for i in range(3)]
    for message in first_burst:
        await engine.handle_message(message)
        clock.advance(1)

    clock.advance(11)  # cooldown expires

    second_burst = [_message(guild, member, "again", message_id=i + 100) for i in range(3)]
    for message in second_burst:
        await engine.handle_message(message)
        clock.advance(1)

    assert case_service.list_for_guild(guild.id).total == 2
    assert second_burst[-1].deleted is True


# -------------------------------------------------------------- exemptions


async def test_exempt_channel_skips_enforcement() -> None:
    engine, case_service = _make_engine_full(_enabled(exempt_channel_ids=(20,)))
    guild = _make_guild()
    member = _member(guild)
    for i in range(5):
        await engine.handle_message(_message(guild, member, "hi", message_id=i))
    assert case_service.list_for_guild(guild.id).total == 0


async def test_moderator_messages_never_enforced() -> None:
    engine, case_service = _make_engine_full(_enabled())
    guild = _make_guild()
    moderator = FakeMember(
        4,
        "mod",
        roles=[FakeRole(102, "mod", 7)],
        guild=guild,
        guild_permissions=FakePermissions(moderate_members=True),
    )
    for i in range(5):
        await engine.handle_message(_message(guild, moderator, "hi", message_id=i))
    assert case_service.list_for_guild(guild.id).total == 0


# -------------------------------------------------------------- escalation


def _create_warn_cases(
    case_service: CaseService, guild_id: int, target_id: int, count: int, clock: FakeClock
) -> None:
    for _ in range(count):
        case_service.create(
            CaseRecord(
                guild_id=guild_id,
                target_user_id=target_id,
                moderator_user_id=100_000,
                action="warn",
                reason="earlier warning",
                created_at=clock(),
                status=STATUS_SUCCESS,
            )
        )


async def test_escalation_converts_warn_to_timeout() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            spam_threshold=3,
            spam_window_seconds=5,
            spam_action="warn",
            escalation=((3, 3600), (5, 43200)),
            warning_window_seconds=604800,
        ),
        clock=clock,
    )
    guild = _make_guild()
    member = _member(guild)
    _create_warn_cases(case_service, guild.id, member.id, 2, clock)  # 2 existing warnings

    for i in range(3):
        await engine.handle_message(_message(guild, member, "hi", message_id=i))
        clock.advance(1)

    page = case_service.list_for_guild(guild.id)
    assert page.total == 3  # 2 pre-existing + 1 escalated action
    record = page.items[0]  # newest first
    assert record.action == "timeout"
    assert record.duration_seconds == 3600
    assert record.automated is True
    assert record.detector == "spam"


async def test_escalation_uses_highest_reached_threshold() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            spam_threshold=3,
            spam_window_seconds=5,
            spam_action="warn",
            escalation=((3, 3600), (5, 43200)),
        ),
        clock=clock,
    )
    guild = _make_guild()
    member = _member(guild)
    _create_warn_cases(case_service, guild.id, member.id, 4, clock)  # 4 -> 5th warning

    for i in range(3):
        await engine.handle_message(_message(guild, member, "hi", message_id=i))
        clock.advance(1)

    record = case_service.list_for_guild(guild.id).items[0]
    assert record.action == "timeout"
    assert record.duration_seconds == 43200


async def test_escalation_not_reached_stays_a_warn() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            spam_threshold=3,
            spam_window_seconds=5,
            spam_action="warn",
            escalation=((5, 43200),),
        ),
        clock=clock,
    )
    guild = _make_guild()
    member = _member(guild)
    _create_warn_cases(case_service, guild.id, member.id, 1, clock)

    for i in range(3):
        await engine.handle_message(_message(guild, member, "hi", message_id=i))
        clock.advance(1)

    record = case_service.list_for_guild(guild.id).items[0]
    assert record.action == "warn"
    assert record.duration_seconds is None


# ------------------------------------------------------------ link/mentions


async def test_link_action_allow_is_a_no_op() -> None:
    engine, case_service = _make_engine_full(_enabled(link_action="allow"))
    guild = _make_guild()
    member = _member(guild)
    await engine.handle_message(_message(guild, member, "see https://example.org/x"))
    assert case_service.list_for_guild(guild.id).total == 0


async def test_link_action_delete_enforces_unlisted_urls() -> None:
    engine, case_service = _make_engine_full(
        _enabled(link_action="delete", allowed_domains=("youtube.com",))
    )
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "see https://evil.example.org/x")
    await engine.handle_message(message)
    assert message.deleted is True
    record = case_service.list_for_guild(guild.id).items[0]
    assert record.action == "delete"
    assert record.detector == "links"


async def test_link_allowlisted_url_not_enforced() -> None:
    engine, case_service = _make_engine_full(
        _enabled(link_action="delete", allowed_domains=("youtube.com",))
    )
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "watch https://youtube.com/watch?v=abc")
    await engine.handle_message(message)
    assert message.deleted is False
    assert case_service.list_for_guild(guild.id).total == 0


async def test_mention_threshold_enforced() -> None:
    engine, case_service = _make_engine_full(
        _enabled(mention_user_threshold=5, mention_action="delete")
    )
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "hey", mentions=[object() for _ in range(6)])
    await engine.handle_message(message)
    assert message.deleted is True
    assert case_service.list_for_guild(guild.id).items[0].detector == "mentions"


async def test_large_mention_count_still_bounded() -> None:
    engine, case_service = _make_engine_full(
        _enabled(mention_user_threshold=10, mention_action="delete")
    )
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "big", mentions=[object() for _ in range(10_000)])
    await engine.handle_message(message)
    assert message.deleted is True


# ---------------------------------------------------------------- invites


async def test_invite_detector_deletes_non_allowed_invites() -> None:
    engine, case_service = _make_engine_full(
        _enabled(invite_action="delete", invite_allowed_codes=("welcome",))
    )
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "join https://discord.gg/other123")
    await engine.handle_message(message)
    assert message.deleted is True
    record = case_service.list_for_guild(guild.id).items[0]
    assert record.detector == "invites"
    assert record.action == "delete"


async def test_invite_allowlist_code_not_enforced() -> None:
    engine, case_service = _make_engine_full(
        _enabled(invite_action="delete", invite_allowed_codes=("welcome",))
    )
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "join https://discord.gg/welcome")
    await engine.handle_message(message)
    assert message.deleted is False
    assert case_service.list_for_guild(guild.id).total == 0


async def test_invite_action_allow_is_a_no_op() -> None:
    engine, case_service = _make_engine_full(_enabled(invite_action="allow"))
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "join https://discord.gg/other123")
    await engine.handle_message(message)
    assert message.deleted is False
    assert case_service.list_for_guild(guild.id).total == 0


async def test_invite_warn_creates_warn_case() -> None:
    engine, case_service = _make_engine_full(_enabled(invite_action="warn"))
    guild = _make_guild()
    member = _member(guild)
    await engine.handle_message(_message(guild, member, "https://discord.gg/xyz"))
    record = case_service.list_for_guild(guild.id).items[0]
    assert record.action == "warn"
    assert record.detector == "invites"


# -------------------------------------------------------------------- raid


async def test_raid_join_burst_alerts_to_log_channel() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            raid_join_threshold=3,
            raid_window_seconds=10,
            raid_action="alert",
            enforcement_cooldown_seconds=60,
        ),
        clock=clock,
    )
    guild = _make_guild()
    log_channel = FakeChannel(21, "logs", guild=guild)
    guild.channels.append(log_channel)
    engine.config_service.update(
        guild.id, actor_user_id=1, changes={"log_channel_id": 21, "mod_log_enabled": True}
    )

    for i in range(3):
        member = _member(guild, user_id=50 + i)
        guild.members.append(member)
        await engine.handle_member_join(guild, member)
        clock.advance(1)

    # No case is created for an alert (nothing was punished), but the log
    # channel received the raid notice.
    assert case_service.list_for_guild(guild.id).total == 0
    assert any(
        getattr(embed, "title", "") == "Raid protection alert" for embed in log_channel.sent_embeds
    )


async def test_raid_cooldown_prevents_repeat_alerts() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            raid_join_threshold=3,
            raid_window_seconds=10,
            raid_action="alert",
            enforcement_cooldown_seconds=60,
        ),
        clock=clock,
    )
    guild = _make_guild()
    log_channel = FakeChannel(21, "logs", guild=guild)
    guild.channels.append(log_channel)
    engine.config_service.update(
        guild.id, actor_user_id=1, changes={"log_channel_id": 21, "mod_log_enabled": True}
    )

    # First burst triggers one alert.
    for i in range(3):
        member = _member(guild, user_id=50 + i)
        guild.members.append(member)
        await engine.handle_member_join(guild, member)
        clock.advance(1)
    alerts_after_first = sum(
        getattr(embed, "title", "") == "Raid protection alert" for embed in log_channel.sent_embeds
    )
    assert alerts_after_first == 1

    # A second burst inside the cooldown does not alert again.
    for i in range(3):
        member = _member(guild, user_id=100 + i)
        guild.members.append(member)
        await engine.handle_member_join(guild, member)
        clock.advance(1)
    alerts_total = sum(
        getattr(embed, "title", "") == "Raid protection alert" for embed in log_channel.sent_embeds
    )
    assert alerts_total == 1


async def test_raid_timeout_action_applies_default_duration() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(
            raid_join_threshold=3,
            raid_window_seconds=10,
            raid_action="timeout",
            timeout_duration_seconds=1800,
        ),
        clock=clock,
    )
    guild = _make_guild()
    joiners = []
    for i in range(3):
        member = _member(guild, user_id=50 + i)
        guild.members.append(member)
        joiners.append(member)
        await engine.handle_member_join(guild, member)
        clock.advance(1)

    # Each of the three recent joiners received an automated timeout case.
    page = case_service.list_for_guild(guild.id)
    assert page.total == 3
    assert all(record.action == "timeout" for record in page.items)
    assert all(record.detector == "raid" for record in page.items)
    assert all(record.duration_seconds == 1800 for record in page.items)
    assert all(member.timed_out_until is not None for member in joiners)


async def test_raid_below_threshold_no_action() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(raid_join_threshold=5, raid_window_seconds=10, raid_action="alert")
    )
    guild = _make_guild()
    for i in range(4):
        member = _member(guild, user_id=50 + i)
        guild.members.append(member)
        await engine.handle_member_join(guild, member)
        clock.advance(1)
    assert case_service.list_for_guild(guild.id).total == 0


async def test_raid_disabled_when_automod_off() -> None:
    engine, case_service = _make_engine_full(AutomodSettings())  # master switch off
    guild = _make_guild()
    for i in range(5):
        member = _member(guild, user_id=50 + i)
        guild.members.append(member)
        await engine.handle_member_join(guild, member)
    assert case_service.list_for_guild(guild.id).total == 0


async def test_raid_bot_joins_ignored() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(raid_join_threshold=2, raid_window_seconds=10, raid_action="alert"), clock=clock
    )
    guild = _make_guild()
    for i in range(3):
        bot_join = FakeMember(900 + i, f"bot{i}", guild=guild, bot=True)
        await engine.handle_member_join(guild, bot_join)
        clock.advance(1)
    assert case_service.list_for_guild(guild.id).total == 0


# ------------------------------------------------------------ word filter


async def test_word_filter_delete() -> None:
    engine, case_service = _make_engine_full(
        _enabled(blocked_terms=("badword",), word_filter_action="delete")
    )
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "say badword and see")
    await engine.handle_message(message)
    assert message.deleted is True
    record = case_service.list_for_guild(guild.id).items[0]
    assert record.detector == "word_filter"


# ------------------------------------------------------------- failures


async def test_missing_bot_permission_records_failed_case_without_crash() -> None:
    engine, case_service = _make_engine_full(_enabled(spam_threshold=3, spam_window_seconds=5))
    guild = _make_guild()
    member = _member(guild)
    restricted = FakeChannel(
        21,
        "restricted",
        guild=guild,
        permissions_for_bot=FakePermissions(manage_messages=False, read_message_history=True),
    )
    guild.channels.append(restricted)
    for i in range(3):
        message = _message(guild, member, "hi", message_id=i, channel=restricted)
        await engine.handle_message(message)
        assert message.deleted is False  # never reached Discord

    page = case_service.list_for_guild(guild.id)
    assert page.total == 1
    assert page.items[0].status == STATUS_FAILED
    assert page.items[0].action == "delete"


async def test_already_deleted_message_creates_no_duplicate_case() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(spam_threshold=3, spam_window_seconds=5, spam_action="delete"), clock=clock
    )
    guild = _make_guild()
    member = _member(guild)
    messages = [_message(guild, member, "hi", message_id=i) for i in range(3)]
    messages[-1].fail_delete(not_found("already gone"))
    for message in messages:
        await engine.handle_message(message)
        clock.advance(1)
    # The message was already deleted by something else: no phantom case.
    assert case_service.list_for_guild(guild.id).total == 0


async def test_discord_rejection_records_failed_case_and_swallows() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(spam_threshold=3, spam_window_seconds=5, spam_action="delete"), clock=clock
    )
    guild = _make_guild()
    member = _member(guild)
    messages = [_message(guild, member, "hi", message_id=i) for i in range(3)]
    messages[-1].fail_delete(forbidden("rate limited"))
    for message in messages:
        await engine.handle_message(message)
        clock.advance(1)
    page = case_service.list_for_guild(guild.id)
    assert page.total == 1
    assert page.items[0].status == STATUS_FAILED


async def test_hierarchy_blocked_automated_warn_records_failed_case() -> None:
    clock = FakeClock()
    engine, case_service = _make_engine_full(
        _enabled(spam_threshold=3, spam_window_seconds=5, spam_action="warn"), clock=clock
    )
    guild = _make_guild()
    admin = FakeMember(9, "admin", roles=[FakeRole(950, "admin", 99)], guild=guild)
    for i in range(3):
        await engine.handle_message(_message(guild, admin, "hi", message_id=i))
        clock.advance(1)
    page = case_service.list_for_guild(guild.id)
    assert page.total == 1
    assert page.items[0].status == STATUS_FAILED


async def test_detector_exception_never_escapes_handle_message() -> None:
    engine, _case_service = _make_engine_full(_enabled())
    guild = _make_guild()
    member = _member(guild)
    message = _message(guild, member, "hi")

    def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("detector exploded")

    engine._detect = _boom
    # Must not raise into the event loop.
    await engine.handle_message(message)
