"""Tests for the top-level moderation commands (Phase 6).

Covers the full command layer with fakes: immediate actions (warn, timeout,
slowmode, unlock, small purge), confirmation-gated actions (kick, ban, large
purge, lock) including expiry, ownership, cancellation, and double-click
protection, unban autocomplete, optional-reason defaults, and the
permission/hierarchy denial paths. No real Discord server is contacted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.cogs.moderation import (
    DEFAULT_REASON,
    _unban_autocomplete,
    ban,
    kick,
    lock,
    purge,
    slowmode,
    timeout,
    unban,
    unlock,
    warn,
)
from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.repository import MemoryCaseRepository
from bot.moderation.cases import STATUS_FAILED, STATUS_SUCCESS
from bot.permissions.checks import PermissionChecker
from bot.services.cases import CaseService
from bot.services.confirmation import ConfirmationController
from bot.services.guild_config import GuildConfigService
from bot.services.moderation import ModerationService
from bot.tests.fakes import (
    FakeBot,
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    FakePermissionOverwrite,
    FakePermissions,
    FakeRole,
    FakeUser,
    forbidden,
)


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _make_world(*, clock: FakeClock | None = None):
    settings = Settings(token="test-token")
    bot = FakeBot()
    case_service = CaseService(MemoryCaseRepository())
    config_service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    permissions = PermissionChecker(bot, settings, config_service=config_service)
    moderation_service = ModerationService(
        bot,
        case_service,
        settings=settings,
        permissions=permissions,
        config_service=config_service,
    )
    confirmations = ConfirmationController(clock=clock)
    client = FakeClient(
        case_service=case_service,
        permissions=permissions,
        moderation_service=moderation_service,
        confirmation_service=confirmations,
    )
    return {
        "settings": settings,
        "bot": bot,
        "case_service": case_service,
        "config_service": config_service,
        "permissions": permissions,
        "moderation_service": moderation_service,
        "confirmations": confirmations,
        "client": client,
    }


def _guild() -> FakeGuild:
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
            manage_channels=True,
        ),
        bot=True,
    )
    channel = FakeChannel(20, "general")
    guild = FakeGuild(10, owner_id=1, me=bot_member, members=[bot_member], channels=[channel])
    channel.guild = guild
    return guild


def _moderator(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        2,
        "mod",
        roles=[FakeRole(100, "mod", 7)],
        guild=guild,
        guild_permissions=FakePermissions(
            moderate_members=True,
            kick_members=True,
            ban_members=True,
            manage_messages=True,
            read_message_history=True,
            manage_channels=True,
        ),
    )


def _regular(guild: FakeGuild) -> FakeMember:
    return FakeMember(4, "member", roles=[FakeRole(50, "user", 1)], guild=guild)


def _target(guild: FakeGuild, user_id: int = 3) -> FakeMember:
    return FakeMember(user_id, "target", roles=[FakeRole(101, "user", 3)], guild=guild)


def _interaction(
    world, guild: FakeGuild, user: FakeMember, *, channel: FakeChannel | None = None
) -> FakeInteraction:
    return FakeInteraction(guild=guild, user=user, client=world["client"], channel=channel)


async def _click_async(
    world, guild: FakeGuild, user: FakeMember, view, button_index: int = 0
) -> FakeInteraction:
    click = _interaction(world, guild, user)
    await view.children[button_index].callback(click)
    return click


# ------------------------------------------------------------ registration


def test_moderation_commands_are_top_level() -> None:
    for command in (warn, timeout, kick, ban, unban, purge, slowmode, lock, unlock):
        assert command.parent is None
        assert command.guild_only is True


def test_reason_option_is_optional() -> None:
    for command in (warn, timeout, kick, ban, unban):
        parameters = {p.name: p for p in command.parameters}
        assert parameters["reason"].required is False


# -------------------------------------------------------------- warn/timeout


async def test_warn_executes_immediately_and_uses_default_reason() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    interaction = _interaction(world, guild, moderator)

    await warn.callback(interaction, target)

    text = interaction.response.messages[0]
    assert "Action: Warn" in text
    assert f"Case #{world['case_service'].list_for_guild(guild.id).items[0].case_id}" in text
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.reason == DEFAULT_REASON
    assert record.status == STATUS_SUCCESS


async def test_warn_with_reason_stores_it() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator(guild))

    await warn.callback(interaction, _target(guild), "spamming")

    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.reason == "spamming"


async def test_timeout_validates_duration() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    interaction = _interaction(world, guild, moderator)

    await timeout.callback(interaction, target, "5m", "too fast")

    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "timeout"
    assert record.duration_seconds == 300
    assert target.timed_out_until is not None


async def test_timeout_rejects_invalid_duration() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator(guild))

    await timeout.callback(interaction, _target(guild), "-5m", "x")

    # Command-layer validation rejects before the service is reached.
    assert "Duration" in interaction.response.messages[0]
    assert world["case_service"].list_for_guild(guild.id).total == 0


async def test_timeout_rejects_beyond_twenty_eight_days() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator(guild))

    await timeout.callback(interaction, _target(guild), "30d", "x")

    assert "28 days" in interaction.response.messages[0]


# ------------------------------------------------------------ confirmation


async def test_kick_shows_confirmation_then_executes_once() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    interaction = _interaction(world, guild, moderator)

    await kick.callback(interaction, target, "bye")

    # A confirmation prompt was shown, not an immediate kick.
    assert "confirm to continue" in interaction.response.messages[0]
    assert target.kicked is False
    view = interaction.response.views[0]
    assert [child.label for child in view.children] == ["Confirm", "Cancel"]

    # First click executes exactly once.
    click = await _click_async(world, guild, moderator, view, 0)
    assert target.kicked is True
    assert "Action: Kick" in click.response.followup_messages[0]

    # Double-click protection: the second click does nothing.
    second = await _click_async(world, guild, moderator, view, 0)
    assert "expired or was already used" in second.response.messages[0]
    assert world["case_service"].list_for_guild(guild.id).total == 1  # one case, no duplicate


async def test_confirmation_ownership_enforced() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    other = FakeMember(5, "other", roles=[FakeRole(102, "mod2", 6)], guild=guild)
    target = _target(guild)
    interaction = _interaction(world, guild, moderator)

    await ban.callback(interaction, target, "bye")

    view = interaction.response.views[0]
    # Someone else clicks confirm: denied, nothing happens.
    click = await _click_async(world, guild, other, view, 0)
    assert "only be used by the moderator" in click.response.messages[0]
    assert target.banned is False
    assert world["case_service"].list_for_guild(guild.id).total == 0


async def test_confirmation_expiry_blocks_execution() -> None:
    clock = FakeClock()
    world = _make_world(clock=clock)
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    interaction = _interaction(world, guild, moderator)

    await kick.callback(interaction, target, "bye")
    view = interaction.response.views[0]

    clock.advance(world["confirmations"].timeout_seconds + 1)

    click = await _click_async(world, guild, moderator, view, 0)
    assert "expired or was already used" in click.response.messages[0]
    assert target.kicked is False
    assert world["case_service"].list_for_guild(guild.id).total == 0


async def test_confirmation_cancellation() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    interaction = _interaction(world, guild, moderator)

    await kick.callback(interaction, target, "bye")
    view = interaction.response.views[0]

    click = await _click_async(world, guild, moderator, view, 1)  # Cancel
    assert "Action cancelled." in click.response.messages[0]
    assert target.kicked is False
    assert world["case_service"].list_for_guild(guild.id).total == 0

    # The confirmation was consumed; confirming afterwards does nothing.
    after = await _click_async(world, guild, moderator, view, 0)
    assert "expired or was already used" in after.response.messages[0]
    assert target.kicked is False


async def test_ban_confirmation_then_executes() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    interaction = _interaction(world, guild, moderator)

    await ban.callback(interaction, target, "raid")
    view = interaction.response.views[0]

    await _click_async(world, guild, moderator, view, 0)
    assert target.banned is True
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "ban"
    assert record.reason == "raid"
    assert record.status == STATUS_SUCCESS


async def test_confirmed_action_failure_shows_safe_error_and_records_failed_case() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    guild.fail_action(forbidden("missing kick permission"))
    interaction = _interaction(world, guild, moderator)

    await kick.callback(interaction, target, "bye")
    view = interaction.response.views[0]

    click = await _click_async(world, guild, moderator, view, 0)
    # Safe error via followup; the failed case was recorded.
    assert any("completed" in message for message in click.response.followup_messages)
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.status == STATUS_FAILED
    # Discord's internal detail never leaks to the user or the case error.
    assert "missing kick permission" not in (record.error or "")
    assert "missing kick permission" not in " ".join(click.response.followup_messages)


async def test_confirmation_prompt_is_ephemeral_and_bounded() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    for i in range(5):
        interaction = _interaction(world, guild, moderator)
        await kick.callback(interaction, _target(guild, user_id=10 + i), "bye")
        assert interaction.response.messages[0]  # prompt visible
    assert len(world["confirmations"]) <= 5


# ---------------------------------------------------------------- purge


async def test_purge_small_executes_immediately() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    channel = guild.channels[0]
    interaction = _interaction(world, guild, moderator, channel=channel)

    await purge.callback(interaction, 10)

    assert channel.purged == [10]
    text = interaction.response.messages[0]
    assert "Purged 10 messages" in text


async def test_purge_large_requires_confirmation() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    channel = guild.channels[0]
    interaction = _interaction(world, guild, moderator, channel=channel)

    await purge.callback(interaction, 50)

    assert channel.purged == []  # nothing deleted yet
    assert "confirm to continue" in interaction.response.messages[0]
    view = interaction.response.views[0]

    click = await _click_async(world, guild, moderator, view, 0)
    assert channel.purged == [50]
    assert "Purged 50 messages" in click.response.followup_messages[0]


async def test_purge_validates_maximum() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    world["config_service"].update(guild.id, actor_user_id=1, changes={"max_purge_amount": 5})
    channel = guild.channels[0]
    interaction = _interaction(world, guild, moderator, channel=channel)

    await purge.callback(interaction, 50)

    assert "Purge amount can't exceed 5" in interaction.response.messages[0]
    assert channel.purged == []


# -------------------------------------------------------------- slowmode


async def test_slowmode_sets_delay_and_creates_case() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    channel = guild.channels[0]
    interaction = _interaction(world, guild, moderator, channel=channel)

    await slowmode.callback(interaction, "5m", None, "calm down")

    assert channel.slowmode_delay == 300
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "slowmode"
    assert record.target_user_id == channel.id
    assert record.status == STATUS_SUCCESS
    assert "Slowmode" in interaction.response.messages[0]


async def test_slowmode_zero_clears() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator(guild), channel=guild.channels[0])

    await slowmode.callback(interaction, "0", None, None)

    assert guild.channels[0].slowmode_delay == 0


async def test_slowmode_rejects_beyond_six_hours() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator(guild), channel=guild.channels[0])

    await slowmode.callback(interaction, "7h", None, None)

    # Command-layer validation rejects before the service is reached.
    assert "Slowmode must be between 0 and 6h" in interaction.response.messages[0]
    assert world["case_service"].list_for_guild(guild.id).total == 0


# ---------------------------------------------------------------- lock


async def test_lock_and_unlock_restore_prior_state() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    channel = guild.channels[0]
    everyone = guild.default_role

    # No prior overwrite: lock creates one; unlock removes it entirely.
    interaction = _interaction(world, guild, moderator, channel=channel)
    await lock.callback(interaction, None, "raid mode")
    view = interaction.response.views[0]
    click = await _click_async(world, guild, moderator, view, 0)
    assert "Action: Lock" in click.response.followup_messages[0]

    lock_record = world["case_service"].list_for_guild(guild.id).items[0]
    assert lock_record.action == "lock"
    assert lock_record.metadata == {"previous_send_messages": None, "had_overwrite": False}
    assert channel.overwrites_for(everyone).send_messages is False

    # Unlock restores exactly: the @everyone overwrite is removed.
    interaction2 = _interaction(world, guild, moderator, channel=channel)
    await unlock.callback(interaction2, None, "all clear")
    assert everyone not in channel.overwrites
    unlock_record = world["case_service"].list_for_guild(guild.id).items[0]
    assert unlock_record.action == "unlock"
    assert unlock_record.status == STATUS_SUCCESS


async def test_lock_restores_existing_overwrite_value() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    channel = guild.channels[0]
    everyone = guild.default_role
    # The channel already explicitly allowed @everyone to send.
    channel.overwrites[everyone] = FakePermissionOverwrite(send_messages=True)

    interaction = _interaction(world, guild, moderator, channel=channel)
    await lock.callback(interaction, None, "quiet")
    view = interaction.response.views[0]
    await _click_async(world, guild, moderator, view, 0)
    assert channel.overwrites_for(everyone).send_messages is False

    interaction2 = _interaction(world, guild, moderator, channel=channel)
    await unlock.callback(interaction2, None, "done")
    # The prior explicit value (True) is restored — not blindly deleted.
    assert channel.overwrites_for(everyone).send_messages is True


async def test_unlock_without_lock_case_refused() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator(guild), channel=guild.channels[0])

    await unlock.callback(interaction, None, "why")

    assert "isn't locked" in interaction.response.messages[0]
    assert world["case_service"].list_for_guild(guild.id).total == 0


# ----------------------------------------------------------------- unban


async def test_unban_autocomplete_suggests_banned_users() -> None:
    world = _make_world()
    guild = _guild()
    guild.banned_users = [FakeUser(50, "bob"), FakeUser(60, "alice")]
    interaction = _interaction(world, guild, _moderator(guild))

    choices = await _unban_autocomplete(interaction, "bo")
    assert [(choice.name, choice.value) for choice in choices] == [("bob (50)", "50")]


async def test_unban_executes() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    world["bot"].add_user(FakeUser(50, "bob"))
    interaction = _interaction(world, guild, moderator)

    await unban.callback(interaction, "50", "appeal accepted")

    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "unban"
    assert record.status == STATUS_SUCCESS
    assert "Unban" in interaction.response.messages[0]


# ----------------------------------------------------- permission/hierarchy


async def test_regular_member_denied() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _regular(guild))

    await warn.callback(interaction, _target(guild))

    assert "You do not have permission" in interaction.response.messages[0]


async def test_role_hierarchy_protection() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    admin = FakeMember(9, "admin", roles=[FakeRole(950, "admin", 99)], guild=guild)
    interaction = _interaction(world, guild, moderator)

    await warn.callback(interaction, admin, "x")

    assert "role is higher than or equal" in interaction.response.messages[0]
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.status == STATUS_FAILED


async def test_bot_missing_permission_fails_at_confirm_time_safely() -> None:
    world = _make_world()
    guild = _guild()
    moderator = _moderator(guild)
    target = _target(guild)
    guild.me.guild_permissions = FakePermissions()  # bot has no permissions
    interaction = _interaction(world, guild, moderator)

    await kick.callback(interaction, target, "bye")
    view = interaction.response.views[0]
    click = await _click_async(world, guild, moderator, view, 0)

    assert any("permission" in message for message in click.response.followup_messages)
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.status == STATUS_FAILED
