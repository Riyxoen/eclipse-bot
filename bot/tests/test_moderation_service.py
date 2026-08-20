"""Tests for the central moderation service.

Every test drives the service against lightweight fakes — no real Discord
server is contacted. Cases are persisted to a per-test SQLite database in a
temporary directory (or a failing repository where noted).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from bot.configuration.settings import Settings
from bot.database.repository import MemoryCaseRepository, build_case_repository
from bot.moderation.cases import STATUS_FAILED, STATUS_SUCCESS, CaseRecord, utc_now
from bot.moderation.errors import (
    CasePersistenceError,
    HierarchyError,
    InvalidDurationError,
    InvalidPurgeAmountError,
    InvalidReasonError,
    InvalidTargetError,
    ModerationExecutionError,
)
from bot.moderation.response import format_case_response
from bot.services.cases import CaseService
from bot.services.moderation import ModerationService
from bot.tests.fakes import (
    FakeBot,
    FakeChannel,
    FakeGuild,
    FakeMember,
    FakePermissions,
    FakeRole,
    FakeUser,
    forbidden,
    not_found,
)

# ------------------------------------------------------------------ helpers


class FailingRepository(MemoryCaseRepository):
    """In-memory repository whose writes fail (simulates DB outages)."""

    def create(self, record: CaseRecord) -> CaseRecord:
        raise sqlite3.OperationalError("simulated disk I/O error")


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "token": "test-token",
        "notify_users": True,
        "max_purge_amount": 100,
        "database_path": tmp_path / "cases.db",
    }
    values.update(overrides)
    return Settings(**values)


def _make_service(
    tmp_path: Path,
    bot: FakeBot | None = None,
    settings: Settings | None = None,
    repository=None,
    config_service=None,
) -> ModerationService:
    bot = bot or FakeBot()
    settings = settings or _settings(tmp_path)
    repository = repository or build_case_repository(settings.database_path)
    permissions = None
    if config_service is not None:
        from bot.permissions.checks import PermissionChecker

        permissions = PermissionChecker(bot, settings, config_service=config_service)
    return ModerationService(
        bot,
        CaseService(repository),
        settings=settings,
        permissions=permissions,
        config_service=config_service,
    )


def _make_guild_config_service(settings: Settings):
    from bot.database.config_repository import MemoryGuildConfigRepository
    from bot.services.guild_config import GuildConfigService

    return GuildConfigService(MemoryGuildConfigRepository(), settings=settings)


def _get_case(service: ModerationService, guild_id: int, case_id: int) -> CaseRecord | None:
    return service.case_service.get(guild_id, case_id)


def _make_guild() -> FakeGuild:
    bot_member = FakeMember(
        100_000,
        "riyxoen",
        roles=[FakeRole(900, "bot", 9)],
        guild_permissions=FakePermissions(
            moderate_members=True, kick_members=True, ban_members=True, manage_messages=True
        ),
        bot=True,
    )
    return FakeGuild(10, owner_id=1, me=bot_member, members=[bot_member])


def _moderator(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        2,
        "mod",
        roles=[FakeRole(100, "mod", 7)],
        guild=guild,
        guild_permissions=FakePermissions(
            moderate_members=True, kick_members=True, ban_members=True, manage_messages=True
        ),
    )


def _target(guild: FakeGuild, user_id: int = 3) -> FakeMember:
    return FakeMember(user_id, "target", roles=[FakeRole(101, "user", 3)], guild=guild)


# ------------------------------------------------------------------ success


async def test_warn_creates_success_case_and_dms(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    record = await service.warn(guild, moderator, target, "  spam  ")

    assert record.status == STATUS_SUCCESS
    assert record.action == "warn"
    assert record.reason == "spam"
    assert record.guild_id == guild.id
    assert record.moderator_user_id == moderator.id
    assert record.target_user_id == target.id
    assert record.case_id is not None
    # DM notification sent (notifications enabled by default)
    assert any("warn" in message and "spam" in message for message in target.sent)
    # Case is persisted and retrievable
    fetched = _get_case(service, guild.id, record.case_id)
    assert fetched is not None and fetched.status == STATUS_SUCCESS


async def test_timeout_applies_timeout_records_duration_and_expiry(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    record = await service.timeout(guild, moderator, target, 300, "spam")

    assert record.status == STATUS_SUCCESS
    assert record.duration_seconds == 300
    assert target.timed_out_until is not None
    assert target.timeout_reason == "spam"
    # expires_at = created_at + duration, stored on the case
    assert record.expires_at is not None
    assert record.expires_at - record.created_at == timedelta(seconds=300)


async def test_kick_kicks_member(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    record = await service.kick(guild, moderator, target, "rule breaking")

    assert record.status == STATUS_SUCCESS
    assert target.kicked is True


async def test_ban_bans_member(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    record = await service.ban(guild, moderator, target, "raid")

    assert record.status == STATUS_SUCCESS
    assert target.banned is True


async def test_unban_unbans_user(tmp_path: Path) -> None:
    bot = FakeBot()
    bot.add_user(FakeUser(50, "former member"))
    service = _make_service(tmp_path, bot=bot)
    guild = _make_guild()
    moderator = _moderator(guild)

    record = await service.unban(guild, moderator, 50, "appeal accepted")

    assert record.status == STATUS_SUCCESS
    assert record.action == "unban"
    assert record.target_user_id == 50


async def test_purge_deletes_messages_and_records_count(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    channel = FakeChannel(20, guild=guild)

    record = await service.purge(channel, moderator, 10)

    assert record.status == STATUS_SUCCESS
    assert record.action == "purge"
    assert record.reason == "Purged 10 messages"
    assert channel.purged == [10]


async def test_purge_amount_is_validated_before_execution(tmp_path: Path) -> None:
    service = _make_service(tmp_path, settings=_settings(tmp_path, max_purge_amount=5))
    guild = _make_guild()
    moderator = _moderator(guild)
    channel = FakeChannel(20, guild=guild)

    with pytest.raises(InvalidPurgeAmountError):
        await service.purge(channel, moderator, 6)
    assert channel.purged == []


# ------------------------------------------------------------ denied checks


async def test_missing_moderator_permission_records_failed_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = FakeMember(2, roles=[FakeRole(100, "user", 7)], guild=guild)  # no perms
    target = _target(guild)

    from bot.moderation.errors import MissingModeratorPermissionError

    with pytest.raises(MissingModeratorPermissionError):
        await service.kick(guild, moderator, target, "spam")

    record = _get_case(service, guild.id, 1)
    assert record is not None
    assert record.status == STATUS_FAILED
    assert record.success is False
    assert "permission" in (record.error or "")


async def test_hierarchy_blocked_records_failed_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)
    target.roles = [FakeRole(101, "admin", 10)]  # higher than the moderator

    with pytest.raises(HierarchyError):
        await service.kick(guild, moderator, target, "spam")

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


async def test_target_is_bot_blocked(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    bot_member = guild.me

    with pytest.raises(InvalidTargetError, match="bot"):
        await service.ban(guild, moderator, bot_member, "no")

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


async def test_target_is_owner_blocked(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    owner = _target(guild, user_id=1)

    with pytest.raises(InvalidTargetError, match="owner"):
        await service.kick(guild, moderator, owner, "no")

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


async def test_invalid_reason_records_failed_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    with pytest.raises(InvalidReasonError):
        await service.warn(guild, moderator, target, "   ")

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


async def test_invalid_duration_records_failed_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    with pytest.raises(InvalidDurationError):
        await service.timeout(guild, moderator, target, 0, "spam")

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


async def test_not_in_guild_blocked_without_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    with pytest.raises(Exception) as excinfo:
        await service.warn(None, _moderator(_make_guild()), _target(_make_guild()), "spam")  # type: ignore[arg-type]
    from bot.moderation.errors import NotInGuildError

    assert isinstance(excinfo.value, NotInGuildError)


# ------------------------------------------------------------ failed action


async def test_discord_rejection_records_failed_case_and_raises_safe_error(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)
    guild.fail_action(forbidden("missing kick permission"))

    with pytest.raises(ModerationExecutionError) as excinfo:
        await service.kick(guild, moderator, target, "spam")

    # Safe message; nothing internal leaks.
    assert "Discord" in str(excinfo.value)
    assert "missing kick permission" not in str(excinfo.value)
    record = _get_case(service, guild.id, 1)
    assert record is not None
    assert record.status == STATUS_FAILED
    assert "completed" in (record.error or "")


async def test_failed_action_never_creates_success_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)
    guild.fail_action(forbidden("nope"))

    with pytest.raises(ModerationExecutionError):
        await service.ban(guild, moderator, target, "spam")

    record = _get_case(service, guild.id, 1)
    assert record is not None
    assert record.status == STATUS_FAILED
    assert record.success is False


async def test_unban_unknown_user_records_failed_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)  # bot knows no users
    guild = _make_guild()
    moderator = _moderator(guild)

    with pytest.raises(InvalidTargetError, match="could not be found"):
        await service.unban(guild, moderator, 999_999, "appeal")

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


async def test_unban_not_banned_records_failed_case(tmp_path: Path) -> None:
    bot = FakeBot()
    bot.add_user(FakeUser(50, "former member"))
    service = _make_service(tmp_path, bot=bot)
    guild = _make_guild()
    moderator = _moderator(guild)
    guild.fail_unban(not_found("404 unknown ban"))

    with pytest.raises(InvalidTargetError, match="isn't banned"):
        await service.unban(guild, moderator, 50, "appeal")

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


# ------------------------------------------------------ persistence failures


async def test_persistence_failure_after_success_raises_safe_error_and_keeps_action(
    tmp_path: Path,
) -> None:
    """Contract: action -> persist -> respond. If persistence fails after the
    action succeeded, the user is told the action happened but the record is
    missing — never that the action failed."""
    service = _make_service(tmp_path, repository=FailingRepository())
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    with pytest.raises(CasePersistenceError) as excinfo:
        await service.kick(guild, moderator, target, "spam")

    assert target.kicked is True  # the Discord action went through
    assert "completed" in str(excinfo.value)
    assert "sqlite" not in str(excinfo.value).lower()
    assert "OperationalError" not in str(excinfo.value)


async def test_persistence_failure_for_failed_action_keeps_original_error(tmp_path: Path) -> None:
    service = _make_service(tmp_path, repository=FailingRepository())
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)
    target.roles = [FakeRole(101, "admin", 10)]  # hierarchy blocks before Discord

    with pytest.raises(HierarchyError):
        await service.kick(guild, moderator, target, "spam")

    # No case could be written, and the safe error still reaches the caller.
    assert _get_case(service, guild.id, 1) is None


# ---------------------------------------------------------------- DM failure


async def test_dm_failure_does_not_fail_action(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)
    target.fail_send(forbidden("cannot send messages to this user"))

    record = await service.timeout(guild, moderator, target, 300, "spam")

    assert record.status == STATUS_SUCCESS
    assert target.timed_out_until is not None  # the action itself went through


async def test_dm_failure_logged_but_case_successful(tmp_path: Path, caplog) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)
    target.fail_send(forbidden("blocked"))

    with caplog.at_level(logging.INFO, logger="riyxoen.notifications"):
        record = await service.warn(guild, moderator, target, "spam")

    assert record.status == STATUS_SUCCESS
    assert "dm notification failed" in caplog.text


async def test_notify_disabled_skips_dm(tmp_path: Path) -> None:
    service = _make_service(tmp_path, settings=_settings(tmp_path, notify_users=False))
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    await service.warn(guild, moderator, target, "spam")

    assert target.sent == []


# ------------------------------------------------------------------- audit


async def test_every_attempt_emits_structured_audit_log(tmp_path: Path, caplog) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    with caplog.at_level(logging.INFO, logger="riyxoen.moderation"):
        record = await service.kick(guild, moderator, target, "spam")

    assert f"case={record.case_id}" in caplog.text
    assert "action=kick" in caplog.text
    assert "guild=10" in caplog.text
    assert "moderator=2" in caplog.text
    assert "target=3" in caplog.text
    assert "result=success" in caplog.text


async def test_failed_attempt_audit_log_marks_result_failed(tmp_path: Path, caplog) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)
    target.roles = [FakeRole(101, "admin", 10)]

    with caplog.at_level(logging.INFO, logger="riyxoen.moderation"):
        with pytest.raises(HierarchyError):
            await service.kick(guild, moderator, target, "spam")

    assert "result=failed" in caplog.text


async def test_audit_log_includes_reason(tmp_path: Path, caplog) -> None:
    """Phase 6: the audit event includes the (truncated) moderation reason.

    Reasons are moderation metadata, not message contents; they are truncated
    to keep log lines bounded and the log redaction filter still scrubs any
    token-shaped strings defensively.
    """
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    with caplog.at_level(logging.INFO, logger="riyxoen.moderation"):
        await service.warn(guild, moderator, target, "spam from the general chat")

    assert "reason=spam from the general chat" in caplog.text


# ---------------------------------------------------------------- responses


def test_format_case_response_contains_expected_fields() -> None:
    created = utc_now()
    record = CaseRecord(
        case_id=42,
        guild_id=10,
        target_user_id=3,
        moderator_user_id=2,
        action="timeout",
        reason="spam",
        created_at=created,
        status=STATUS_SUCCESS,
        duration_seconds=300,
        expires_at=created + timedelta(seconds=300),
    )
    text = format_case_response(record, target_label="<@3>", moderator_label="<@2>")
    assert "Case #42" in text
    assert "Action: Timeout" in text
    assert "Target: <@3>" in text
    assert "Moderator: <@2>" in text
    assert "Reason: spam" in text
    assert "Status: Success" in text
    assert "5m" in text  # humanized duration


def test_format_case_response_shows_failed_status() -> None:
    created = utc_now()
    record = CaseRecord(
        case_id=7,
        guild_id=10,
        target_user_id=3,
        moderator_user_id=2,
        action="kick",
        reason="spam",
        created_at=created,
        status=STATUS_FAILED,
        error="The action could not be completed.",
    )
    text = format_case_response(record, target_label="x", moderator_label="y")
    assert "Status: Failed" in text
    # The internal error string never reaches the response.
    assert "could not be completed" not in text


# --------------------------------------------------- automated moderation


async def test_automated_warn_marks_case_automated_with_detector(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = guild.me  # the bot is the actor for automated actions
    target = _target(guild)

    record = await service.warn(
        guild,
        moderator,
        target,
        "Automated moderation: spam detected",
        automated=True,
        detector="spam",
    )

    assert record.status == STATUS_SUCCESS
    assert record.automated is True
    assert record.detector == "spam"
    fetched = _get_case(service, guild.id, record.case_id)
    assert fetched is not None and fetched.automated is True and fetched.detector == "spam"


async def test_automated_timeout_marks_case_automated_with_detector(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    target = _target(guild)

    record = await service.timeout(
        guild,
        guild.me,
        target,
        300,
        "Automated moderation: spam detected",
        automated=True,
        detector="spam",
    )

    assert record.automated is True
    assert record.detector == "spam"
    assert record.duration_seconds == 300
    assert target.timed_out_until is not None


async def test_manual_warn_stays_manual(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    moderator = _moderator(guild)
    target = _target(guild)

    record = await service.warn(guild, moderator, target, "manual warning")

    assert record.automated is False
    assert record.detector is None


async def test_delete_message_creates_automated_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    target = _target(guild)
    channel = FakeChannel(20, guild=guild)
    from bot.tests.fakes import FakeMessage

    message = FakeMessage(1, "bad", guild=guild, author=target, channel=channel)

    record = await service.delete_message(
        guild,
        guild.me,
        message,
        "Automated moderation: blocked term detected",
        automated=True,
        detector="word_filter",
    )

    assert record is not None
    assert record.action == "delete"
    assert record.status == STATUS_SUCCESS
    assert record.automated is True
    assert record.detector == "word_filter"
    assert record.target_user_id == target.id
    assert message.deleted is True


async def test_delete_message_already_deleted_returns_none_without_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    target = _target(guild)
    channel = FakeChannel(20, guild=guild)
    from bot.tests.fakes import FakeMessage

    message = FakeMessage(1, "bad", guild=guild, author=target, channel=channel)
    message.fail_delete(not_found("already gone"))

    record = await service.delete_message(
        guild, guild.me, message, "Automated moderation: spam detected", detector="spam"
    )

    # Nothing to enforce, no phantom case: the message was already deleted.
    assert record is None
    assert _get_case(service, guild.id, 1) is None


async def test_delete_message_missing_bot_permission_records_failed_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    target = _target(guild)
    channel = FakeChannel(
        20,
        guild=guild,
        permissions_for_bot=FakePermissions(manage_messages=False, read_message_history=True),
    )
    from bot.moderation.errors import MissingBotPermissionError
    from bot.tests.fakes import FakeMessage

    message = FakeMessage(1, "bad", guild=guild, author=target, channel=channel)

    with pytest.raises(MissingBotPermissionError):
        await service.delete_message(
            guild, guild.me, message, "Automated moderation: spam detected", detector="spam"
        )

    assert message.deleted is False
    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


async def test_delete_message_discord_rejection_records_failed_case(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    guild = _make_guild()
    target = _target(guild)
    channel = FakeChannel(20, guild=guild)
    from bot.tests.fakes import FakeMessage

    message = FakeMessage(1, "bad", guild=guild, author=target, channel=channel)
    message.fail_delete(forbidden("rate limited"))

    with pytest.raises(ModerationExecutionError):
        await service.delete_message(
            guild, guild.me, message, "Automated moderation: spam detected", detector="spam"
        )

    record = _get_case(service, guild.id, 1)
    assert record is not None and record.status == STATUS_FAILED


# ------------------------------------------------ per-guild configuration


async def test_per_guild_log_channel_receives_case_summary(tmp_path: Path) -> None:
    settings = _settings(tmp_path, log_channel_id=None)  # env: no log channel
    config_service = _make_guild_config_service(settings)
    service = _make_service(tmp_path, settings=settings, config_service=config_service)
    guild = _make_guild()
    channel = FakeChannel(222, "logs", guild=guild)
    guild.channels = [channel]
    config_service.update(
        guild.id,
        actor_user_id=1,
        changes={"log_channel_id": 222, "mod_log_enabled": True},
    )

    await service.warn(guild, _moderator(guild), _target(guild), "spam")

    assert channel.sent_embeds
    assert channel.sent_embeds[0].title.startswith("Case #")


async def test_per_guild_mod_log_disabled_skips_posting(tmp_path: Path) -> None:
    settings = _settings(tmp_path, log_channel_id=None)
    config_service = _make_guild_config_service(settings)
    service = _make_service(tmp_path, settings=settings, config_service=config_service)
    guild = _make_guild()
    channel = FakeChannel(222, "logs", guild=guild)
    guild.channels = [channel]
    config_service.update(
        guild.id,
        actor_user_id=1,
        changes={"log_channel_id": 222, "mod_log_enabled": False},
    )

    await service.warn(guild, _moderator(guild), _target(guild), "spam")

    assert channel.sent_embeds == []


async def test_per_guild_dm_notifications_respected(tmp_path: Path) -> None:
    settings = _settings(tmp_path, notify_users=True)  # env: DMs on
    config_service = _make_guild_config_service(settings)
    service = _make_service(tmp_path, settings=settings, config_service=config_service)
    guild = _make_guild()
    config_service.update(guild.id, actor_user_id=1, changes={"notify_users": False})
    target = _target(guild)

    await service.warn(guild, _moderator(guild), target, "spam")

    assert target.sent == []  # per-guild config disabled DMs


async def test_per_guild_max_purge_validated_in_service(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_purge_amount=100)
    config_service = _make_guild_config_service(settings)
    service = _make_service(tmp_path, settings=settings, config_service=config_service)
    guild = _make_guild()
    config_service.update(guild.id, actor_user_id=1, changes={"max_purge_amount": 5})
    moderator = _moderator(guild)
    channel = FakeChannel(20, guild=guild)

    with pytest.raises(InvalidPurgeAmountError):
        await service.purge(channel, moderator, 6)
    assert channel.purged == []

    record = await service.purge(channel, moderator, 5)
    assert record.status == STATUS_SUCCESS


async def test_max_purge_amount_for_uses_per_guild_config(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_purge_amount=100)
    config_service = _make_guild_config_service(settings)
    service = _make_service(tmp_path, settings=settings, config_service=config_service)
    guild = _make_guild()
    assert service.max_purge_amount_for(guild) == 100

    config_service.update(guild.id, actor_user_id=1, changes={"max_purge_amount": 7})
    assert service.max_purge_amount_for(guild) == 7
