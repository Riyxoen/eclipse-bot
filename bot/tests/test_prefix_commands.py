"""Tests for the prefix command layer (Phase 6: ``.el`` / ``.ban`` / ...).

Covers parsing (pure functions), dispatcher routing (fakes only — no real
Discord), the custom-role command behaviors, and the moderation prefix
commands including DM-failure reporting and safe error paths.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.repository import MemoryCaseRepository
from bot.moderation.cases import STATUS_CLEARED, STATUS_SUCCESS, CaseRecord, utc_now
from bot.permissions.checks import PermissionChecker
from bot.prefix import (
    PrefixDispatcher,
    PrefixRateLimiter,
    parse_member_id,
    parse_prefix_command,
    split_target_and_reason,
)
from bot.services.cases import CaseService
from bot.services.custom_roles import CustomRoleService
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
)

# ---------------------------------------------------------------- surface


def test_prefix_command_surface_is_wired() -> None:
    """Every documented quick-moderation prefix command has a handler.

    Slash-command loading is covered in ``test_bot.py`` (``setup_hook``
    registers the full tree); this guards the text-command surface the
    Phase 11 startup contract requires (moderation + custom roles +
    utility, routed by ``PrefixDispatcher``).
    """
    from bot.prefix import _MODERATION_HANDLERS

    assert set(_MODERATION_HANDLERS) == {
        "ban",
        "kick",
        "mute",
        "unmute",
        "warn",
        "warnings",
        "clearwarnings",
        "modhistory",
        "purge",
        "slowmode",
        "lockdown",
        "unlockdown",
    }
    # ``el`` (custom roles) and ``help`` (utility) are routed specially.
    from bot.prefix import PrefixDispatcher

    assert hasattr(PrefixDispatcher, "_route_el")
    assert hasattr(PrefixDispatcher, "_route_help")


# ---------------------------------------------------------------- parsing


def test_parse_prefix_command_extracts_name_subcommand_and_args() -> None:
    parsed = parse_prefix_command(".el rename Cool Role", ".")
    assert parsed is not None
    assert parsed.name == "el"
    assert parsed.subcommand == "rename"
    assert parsed.arguments == "Cool Role"


def test_parse_prefix_command_enable_has_no_arguments() -> None:
    parsed = parse_prefix_command(".el enable", ".")
    assert parsed is not None
    assert parsed.name == "el"
    assert parsed.subcommand == "enable"
    assert parsed.arguments == ""


def test_parse_prefix_command_moderation_splits_reason() -> None:
    parsed = parse_prefix_command(".ban @123 spam reason", ".")
    assert parsed is not None
    assert parsed.name == "ban"
    assert parsed.subcommand is None
    assert parsed.arguments == "@123 spam reason"


def test_parse_prefix_command_ignores_non_commands() -> None:
    assert parse_prefix_command("hello there", ".") is None
    assert parse_prefix_command("", ".") is None
    assert parse_prefix_command("..el enable", ".") is None
    assert parse_prefix_command(".", ".") is None


def test_parse_prefix_command_respects_custom_prefix() -> None:
    parsed = parse_prefix_command("!el enable", "!")
    assert parsed is not None
    assert parsed.name == "el"
    assert parse_prefix_command(".el enable", "!") is None


def test_parse_member_id_and_split() -> None:
    assert parse_member_id("<@123> rest") == 123
    assert parse_member_id("not a mention") is None
    target, reason = split_target_and_reason("<@123>  spam  here ")
    assert target == "123"
    assert reason == "spam  here"
    target, reason = split_target_and_reason("no mention at all")
    assert target is None
    assert reason == "no mention at all"


# ---------------------------------------------------------------- fixtures


class _FakeIntents:
    message_content = True


def _world():
    settings = Settings(token="test-token", notify_users=True)
    bot = FakeBot()
    bot._intents = _FakeIntents()
    guild = _guild(bot)
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
    custom_roles = CustomRoleService(settings, permissions, config_service)
    bot.custom_roles = custom_roles
    bot.moderation_service = moderation_service
    bot.permissions = permissions
    bot.config_service = config_service
    bot.case_service = case_service
    dispatcher = PrefixDispatcher(bot, settings)
    return {
        "settings": settings,
        "bot": bot,
        "guild": guild,
        "case_service": case_service,
        "config_service": config_service,
        "permissions": permissions,
        "moderation_service": moderation_service,
        "custom_roles": custom_roles,
        "dispatcher": dispatcher,
    }


def _guild(bot: FakeBot) -> FakeGuild:
    bot_member = FakeMember(
        100_000,
        "riyxoen",
        roles=[FakeRole(900, "bot", 9)],
        guild_permissions=FakePermissions(
            moderate_members=True,
            kick_members=True,
            ban_members=True,
            manage_roles=True,
        ),
        bot=True,
    )
    channel = FakeChannel(20, "general")
    guild = FakeGuild(10, owner_id=1, me=bot_member, members=[bot_member], channels=[channel])
    channel.guild = guild
    return guild


def _admin(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        2,
        "admin",
        roles=[FakeRole(100, "admin", 8)],
        guild=guild,
        guild_permissions=FakePermissions(manage_roles=True),
    )


def _moderator(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        3,
        "mod",
        roles=[FakeRole(101, "mod", 7)],
        guild=guild,
        guild_permissions=FakePermissions(
            moderate_members=True,
            kick_members=True,
            ban_members=True,
            manage_messages=True,
            manage_channels=True,
            manage_roles=True,
        ),
    )


def _regular(guild: FakeGuild) -> FakeMember:
    return FakeMember(4, "member", roles=[FakeRole(50, "user", 1)], guild=guild)


def _target(guild: FakeGuild, user_id: int = 5) -> FakeMember:
    member = FakeMember(user_id, "target", roles=[FakeRole(60, "user", 2)], guild=guild)
    guild.members.append(member)
    return member


def _message(world, author: FakeMember, content: str) -> FakeMessage:
    return FakeMessage(
        1,
        content,
        guild=world["guild"],
        author=author,
        channel=world["guild"].channels[0],
    )


async def _dispatch(world, message: FakeMessage) -> bool:
    return await world["dispatcher"].handle(message)


def _replies(world) -> list[str]:
    return world["guild"].channels[0].sent_messages


# ---------------------------------------------------------- dispatcher flow


async def test_non_command_message_is_ignored() -> None:
    world = _world()
    handled = await _dispatch(world, _message(world, _regular(world["guild"]), "hello world"))
    assert handled is False
    assert _replies(world) == []


async def test_bot_messages_and_dms_are_ignored() -> None:
    world = _world()
    guild = world["guild"]
    bot_user = FakeMember(100_000, "riyxoen", bot=True, guild=guild)
    bot_msg = _message(world, bot_user, ".el enable")
    assert await _dispatch(world, bot_msg) is False

    dm = FakeMessage(2, ".ban @5 spam", guild=None, author=_admin(guild), channel=None)
    assert await _dispatch(world, dm) is False


async def test_dispatcher_replies_to_unknown_command() -> None:
    """An unknown prefix command gets the safe 'Command unavailable.' reply
    (Phase 10) instead of being silently ignored."""
    world = _world()
    await _dispatch(world, _message(world, _admin(world["guild"]), ".unknown arg"))
    assert any("Command unavailable" in reply for reply in _replies(world))


# ------------------------------------------------------- custom role (.el)


async def test_el_enable_creates_role_and_replies() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))

    assert any("enabled" in reply.lower() for reply in _replies(world))
    config = world["config_service"].get(guild.id)
    assert config.custom_roles_enabled is True
    assert config.custom_role_id is not None
    assert guild.get_role(config.custom_role_id) is not None  # role exists


async def test_el_enable_second_call_is_idempotent() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))
    role_count_before = len(guild.roles)
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))

    assert any("already enabled" in reply.lower() for reply in _replies(world))
    assert len(guild.roles) == role_count_before  # no duplicate role


async def test_el_rename_requires_enable_first() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), ".el rename Cool"))

    assert any(".el enable` first" in reply for reply in _replies(world))
    assert _replies(world)[-1].startswith("The custom-role system is disabled")


async def test_el_rename_updates_role() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))
    await _dispatch(world, _message(world, _admin(guild), ".el rename Super Role"))

    role = world["custom_roles"].managed_role(guild)
    assert role is not None and role.name == "Super Role"
    assert any("Super Role" in reply for reply in _replies(world))


async def test_el_color_normalizes_and_updates_role() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))
    await _dispatch(world, _message(world, _admin(guild), ".el color #FF0000"))

    role = world["custom_roles"].managed_role(guild)
    assert role is not None and role.colour == 0xFF0000
    assert any("ff0000" in reply.lower() for reply in _replies(world))


async def test_el_color_rejects_malformed_hex() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))
    await _dispatch(world, _message(world, _admin(guild), ".el color notacolor"))

    assert any("isn't valid" in reply.lower() for reply in _replies(world))
    assert _replies(world)[-1].startswith("That color isn't valid")


async def test_el_commands_require_manage_roles_permission() -> None:
    world = _world()
    guild = world["guild"]
    regular = _regular(guild)
    await _dispatch(world, _message(world, regular, ".el enable"))

    assert any("Manage Roles" in reply for reply in _replies(world))
    assert world["config_service"].get(guild.id).custom_roles_enabled is False


async def test_el_missing_role_reports_clearly() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))
    # Delete the managed role behind the bot's back.
    role = world["custom_roles"].managed_role(guild)
    guild.roles.remove(role)

    await _dispatch(world, _message(world, _admin(guild), ".el color #00ff00"))

    assert any("missing" in reply.lower() for reply in _replies(world))
    assert _replies(world)[-1].startswith("The managed custom role is missing")


# ------------------------------------------------------- moderation (.ban)


async def test_ban_requires_reason() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".ban {target.mention}"))

    assert any("reason is required" in reply.lower() for reply in _replies(world))
    assert target.banned is False


async def test_ban_executes_and_creates_case() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".ban {target.mention} raid spam"))

    assert target.banned is True
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "ban"
    assert record.reason == "raid spam"
    assert record.status == STATUS_SUCCESS
    assert any("Action: Ban" in reply for reply in _replies(world))


async def test_kick_executes_and_creates_case() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".kick {target.mention} bye"))

    assert target.kicked is True
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "kick"


async def test_ban_invalid_user_reports_safely() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _moderator(guild), ".ban <@999> spam"))

    assert any("User not found" in reply for reply in _replies(world))


async def test_ban_missing_permission_denied_safely() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _regular(guild), f".ban {target.mention} spam"))

    assert any("do not have permission" in reply.lower() for reply in _replies(world))
    assert target.banned is False
    # The failed attempt is still recorded as a failed case (auditable).
    assert world["case_service"].list_for_guild(guild.id).total == 1


async def test_ban_hierarchy_protection() -> None:
    world = _world()
    guild = world["guild"]
    higher = FakeMember(9, "higher", roles=[FakeRole(950, "boss", 99)], guild=guild)
    guild.members.append(higher)
    await _dispatch(world, _message(world, _moderator(guild), f".ban {higher.mention} spam"))

    assert any("role is higher" in reply.lower() for reply in _replies(world))
    assert higher.banned is False


async def test_dm_failure_does_not_fail_action_and_is_reported() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    target.fail_send(forbidden("dm closed"))
    await _dispatch(world, _message(world, _moderator(guild), f".ban {target.mention} spam"))

    assert target.banned is True  # the ban succeeded despite the DM failure
    replies = " ".join(_replies(world))
    assert "could not be DM'd" in replies
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.status == STATUS_SUCCESS
    assert (record.metadata or {}).get("dm_delivered") is False


async def test_discord_api_failure_is_safe_and_recorded() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    guild.fail_action(forbidden("missing kick permission"))
    await _dispatch(world, _message(world, _moderator(guild), f".kick {target.mention} bye"))

    replies = " ".join(_replies(world))
    assert "could not be completed" in replies
    assert "missing kick permission" not in replies  # internal detail stays local
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.status == "failed"
    assert "missing kick permission" not in (record.error or "")


# ------------------------------------------------------------- mute family


async def test_mute_applies_timeout_and_tracks_expiry() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".mute {target.mention} 10m spam"))

    assert target.timed_out_until is not None
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "timeout"
    assert record.duration_seconds == 600
    assert record.expires_at is not None
    assert (record.metadata or {}).get("muted") is True
    assert any("Duration: 10m" in reply for reply in _replies(world))


async def test_mute_rejects_invalid_duration() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".mute {target.mention} -5m spam"))

    assert any("Duration must be" in reply for reply in _replies(world))
    assert target.timed_out_until is None
    assert world["case_service"].list_for_guild(guild.id).total == 0


async def test_mute_rejects_missing_reason() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".mute {target.mention} 10m"))

    assert any("reason is required" in reply.lower() for reply in _replies(world))
    assert target.timed_out_until is None


async def test_unmute_removes_timeout() -> None:
    world = _world()
    guild = world["guild"]
    moderator = _moderator(guild)
    target = _target(guild)
    await _dispatch(world, _message(world, moderator, f".mute {target.mention} 1h spam"))
    await _dispatch(world, _message(world, moderator, f".unmute {target.mention} appeal ok"))

    assert target.timed_out_until is None
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "unmute"
    assert record.status == STATUS_SUCCESS


async def test_unmute_when_not_muted_is_clean() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".unmute {target.mention} why"))

    assert any("isn't currently muted" in reply.lower() for reply in _replies(world))
    # The refusal is still recorded as a failed case (auditable).
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "unmute"
    assert record.status == "failed"
    assert record.error == "That user isn't currently muted."


async def test_unmute_after_discord_expiry_works_restart_safely() -> None:
    """A timeout expires Discord-side (no bot running): unmute still works
    because state comes from the member's live timeout, not memory."""
    world = _world()
    guild = world["guild"]
    moderator = _moderator(guild)
    target = _target(guild)
    # Simulate the timeout having been applied earlier and then expiring.
    target.timed_out_until = datetime(2020, 1, 1, tzinfo=UTC)  # long past

    await _dispatch(world, _message(world, moderator, f".unmute {target.mention} expired"))

    assert any("isn't currently muted" in reply.lower() for reply in _replies(world))
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.status == "failed"


def test_mute_duration_formats_parse() -> None:
    from bot.moderation.validation import parse_duration

    assert parse_duration("10m") == 600
    assert parse_duration("1h") == 3600
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400


# ------------------------------------------------------------- rate limiter


class _RateClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_rate_limiter_allows_under_limit() -> None:
    clock = _RateClock()
    limiter = PrefixRateLimiter(limit=3, window_seconds=5, clock=clock)
    assert limiter.allow(1) is True
    assert limiter.allow(1) is True
    assert limiter.allow(1) is True


def test_rate_limiter_rejects_over_limit() -> None:
    clock = _RateClock()
    limiter = PrefixRateLimiter(limit=3, window_seconds=5, clock=clock)
    for _ in range(3):
        assert limiter.allow(1) is True
    assert limiter.allow(1) is False  # 4th command within the window


def test_rate_limiter_window_expires() -> None:
    clock = _RateClock()
    limiter = PrefixRateLimiter(limit=3, window_seconds=5, clock=clock)
    for _ in range(3):
        assert limiter.allow(1) is True
    clock.advance(6)
    assert limiter.allow(1) is True  # window elapsed


def test_rate_limiter_tracks_users_independently() -> None:
    clock = _RateClock()
    limiter = PrefixRateLimiter(limit=3, window_seconds=5, clock=clock)
    for _ in range(3):
        assert limiter.allow(1) is True
    assert limiter.allow(2) is True  # a different user is not limited


def test_rate_limiter_is_bounded() -> None:
    clock = _RateClock()
    limiter = PrefixRateLimiter(limit=2, window_seconds=5, max_users=3, clock=clock)
    for user_id in range(50):
        limiter.allow(user_id)
    assert len(limiter) <= 3  # global cap enforced


def test_rate_limiter_prune_reclaims_stale_users() -> None:
    clock = _RateClock()
    limiter = PrefixRateLimiter(limit=2, window_seconds=5, clock=clock)
    limiter.allow(1)
    limiter.allow(2)
    clock.advance(10)
    limiter.prune()
    assert len(limiter) == 0


async def test_dispatcher_rate_limits_rapid_commands() -> None:
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)
    # The admin hammers the command faster than the limiter allows.
    for _ in range(15):
        await _dispatch(world, _message(world, admin, ".el enable"))
    replies = " ".join(_replies(world))
    assert "too quickly" in replies
    # Not every attempt reached Discord: at most one role was created.
    assert len([r for r in guild.roles if r.name == "Riyxoen Custom"]) <= 1


async def test_dispatcher_normal_use_not_limited() -> None:
    """Normal spaced-out usage is never rate limited (no false positives)."""
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)
    await _dispatch(world, _message(world, admin, ".el enable"))
    await _dispatch(world, _message(world, admin, ".el color #ff0000"))
    assert world["config_service"].get(guild.id).custom_roles_enabled is True
    assert world["custom_roles"].managed_role(guild).colour == 0xFF0000
    assert not any("too quickly" in reply for reply in _replies(world))


# ==================================================== Phase 10: warn system


def _make_warning(world, target_id: int, *, reason: str = "spam") -> CaseRecord:
    return world["case_service"].create(
        CaseRecord(
            guild_id=world["guild"].id,
            target_user_id=target_id,
            moderator_user_id=3,
            action="warn",
            reason=reason,
            created_at=utc_now(),
            status=STATUS_SUCCESS,
        )
    )


async def test_warn_prefix_creates_case_and_replies() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".warn {target.mention} spam"))

    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "warn"
    assert record.reason == "spam"
    assert record.status == STATUS_SUCCESS
    assert any("Action: Warn" in reply for reply in _replies(world))


async def test_warn_requires_reason() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".warn {target.mention}"))

    assert any("reason is required" in reply.lower() for reply in _replies(world))
    assert world["case_service"].list_for_guild(guild.id).total == 0


async def test_warn_denied_for_regular_member() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _regular(guild), f".warn {target.mention} spam"))

    assert any("do not have permission" in reply.lower() for reply in _replies(world))
    # The failed attempt is still recorded as a failed case (auditable).
    assert world["case_service"].list_for_guild(guild.id).total == 1


async def test_warnings_lists_warnings_for_target() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    _make_warning(world, target.id, reason="spam")
    _make_warning(world, target.id, reason="flood")
    world["case_service"].create(
        CaseRecord(
            guild_id=guild.id,
            target_user_id=target.id,
            moderator_user_id=3,
            action="kick",
            reason="bye",
            created_at=utc_now(),
            status=STATUS_SUCCESS,
        )
    )

    await _dispatch(world, _message(world, _moderator(guild), f".warnings {target.mention}"))

    replies = " ".join(_replies(world))
    assert "Warnings for" in replies
    assert "2 active" in replies
    # Only warning cases are listed, not the kick.
    assert "kick" not in replies.lower()


async def test_warnings_requires_moderator() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    _make_warning(world, target.id)
    await _dispatch(world, _message(world, _regular(guild), f".warnings {target.mention}"))

    assert any("do not have permission" in reply.lower() for reply in _replies(world))


async def test_clearwarnings_marks_warnings_cleared() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    _make_warning(world, target.id)
    _make_warning(world, target.id)

    message = _message(world, _moderator(guild), f".clearwarnings {target.mention}")
    await _dispatch(world, message)

    assert any("Cleared 2 warning" in reply for reply in _replies(world))
    page = world["case_service"].list_for_member(guild.id, target.id, page_size=10)
    assert all(record.status == STATUS_CLEARED for record in page.items)
    # Active warning count drops to zero; history and labels remain.
    active = world["case_service"].count_active_warnings(guild.id, target.id, since=None)
    assert active == 0


async def test_clearwarnings_without_warnings_is_clean() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".clearwarnings {target.mention}"))

    assert any("no warnings to clear" in reply.lower() for reply in _replies(world))


async def test_modhistory_lists_recent_cases() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    _make_warning(world, target.id)
    world["case_service"].create(
        CaseRecord(
            guild_id=guild.id,
            target_user_id=target.id,
            moderator_user_id=3,
            action="ban",
            reason="raid",
            created_at=utc_now(),
            status=STATUS_SUCCESS,
        )
    )

    await _dispatch(world, _message(world, _moderator(guild), f".modhistory {target.mention}"))

    replies = " ".join(_replies(world))
    assert "Moderation cases for" in replies
    assert "Warn" in replies and "Ban" in replies


async def test_modhistory_without_cases_is_clean() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".modhistory {target.mention}"))

    assert any("No moderation cases found" in reply for reply in _replies(world))


async def test_modhistory_requires_moderator() -> None:
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    _make_warning(world, target.id)
    await _dispatch(world, _message(world, _regular(guild), f".modhistory {target.mention}"))

    assert any("do not have permission" in reply.lower() for reply in _replies(world))


# ====================================================== Phase 10: channel


async def test_purge_deletes_and_reports_accurately() -> None:
    world = _world()
    guild = world["guild"]
    channel = guild.channels[0]
    await _dispatch(world, _message(world, _moderator(guild), ".purge 5"))

    assert channel.purged == [5]
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "purge"
    assert record.reason == "Purged 5 messages"
    assert any("Action: Purge" in reply for reply in _replies(world))


async def test_purge_reports_partial_deletion() -> None:
    """The reply never claims every message was deleted when some failed."""
    world = _world()
    guild = world["guild"]
    # The fake purge caps at 50 deleted; requesting 60 must report 50 of 60.
    await _dispatch(world, _message(world, _moderator(guild), ".purge 60"))

    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert "Purged 50 of 60 messages" in record.reason
    assert any("Purged 50 of 60" in reply for reply in _replies(world))


async def test_purge_rejects_invalid_amounts() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _moderator(guild), ".purge 0"))
    assert any("at least 1" in reply for reply in _replies(world))

    world["guild"].channels[0].sent_messages.clear()
    await _dispatch(world, _message(world, _moderator(guild), ".purge 99999"))
    assert any("can't exceed" in reply for reply in _replies(world))

    world["guild"].channels[0].sent_messages.clear()
    await _dispatch(world, _message(world, _moderator(guild), ".purge abc"))
    assert any("provide a number" in reply for reply in _replies(world))


async def test_purge_denied_for_regular_member() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _regular(guild), ".purge 5"))

    assert any("do not have permission" in reply.lower() for reply in _replies(world))


async def test_slowmode_sets_delay() -> None:
    world = _world()
    guild = world["guild"]
    channel = guild.channels[0]
    await _dispatch(world, _message(world, _moderator(guild), ".slowmode 10"))

    assert channel.slowmode_delay == 10
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "slowmode"
    assert record.duration_seconds == 10


async def test_slowmode_zero_clears() -> None:
    world = _world()
    guild = world["guild"]
    channel = guild.channels[0]
    await _dispatch(world, _message(world, _moderator(guild), ".slowmode 0"))

    assert channel.slowmode_delay == 0


async def test_slowmode_rejects_invalid_duration() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _moderator(guild), ".slowmode 999999"))
    assert any("Slowmode must be" in reply for reply in _replies(world))

    world["guild"].channels[0].sent_messages.clear()
    await _dispatch(world, _message(world, _moderator(guild), ".slowmode -5"))
    assert any("Duration must be" in reply for reply in _replies(world))


async def test_lockdown_locks_channel_and_creates_case() -> None:
    world = _world()
    guild = world["guild"]
    channel = guild.channels[0]
    await _dispatch(world, _message(world, _moderator(guild), ".lockdown"))

    overwrite = channel.overwrites.get(guild.default_role)
    assert overwrite is not None and overwrite.send_messages is False
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "lock"


async def test_lockdown_repeat_is_idempotent() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _moderator(guild), ".lockdown"))
    await _dispatch(world, _message(world, _moderator(guild), ".lockdown"))

    assert any("already locked" in reply.lower() for reply in _replies(world))
    lock_cases = [
        record
        for record in world["case_service"].list_for_guild(guild.id).items
        if record.action == "lock"
    ]
    assert len(lock_cases) == 1  # no duplicate lock case


async def test_unlockdown_restores_channel_state() -> None:
    world = _world()
    guild = world["guild"]
    channel = guild.channels[0]
    moderator = _moderator(guild)
    await _dispatch(world, _message(world, moderator, ".lockdown"))
    await _dispatch(world, _message(world, moderator, ".unlockdown"))

    # The lock created the @everyone overwrite, so unlock removes it entirely.
    assert guild.default_role not in channel.overwrites
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "unlock"


async def test_unlockdown_when_not_locked_is_clean() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _moderator(guild), ".unlockdown"))

    assert any("isn't locked" in reply.lower() for reply in _replies(world))
    assert world["case_service"].list_for_guild(guild.id).total == 0  # no phantom case


async def test_lockdown_denied_for_regular_member() -> None:
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _regular(guild), ".lockdown"))

    assert any("do not have permission" in reply.lower() for reply in _replies(world))


# ====================================================== Phase 10: help / prefix


async def test_help_shows_organized_categories_for_moderator() -> None:
    world = _world()
    await _dispatch(world, _message(world, _moderator(world["guild"]), ".help"))

    text = "\n".join(_replies(world))
    assert "__Moderation__" in text
    assert "__Custom Roles__" in text
    assert "__Utility__" in text
    assert "Configuration" not in text  # moderator is not an administrator
    assert ".clearwarnings" in text
    assert ".lockdown" in text


async def test_help_hides_restricted_sections_for_regular_member() -> None:
    world = _world()
    await _dispatch(world, _message(world, _regular(world["guild"]), ".help"))

    text = "\n".join(_replies(world))
    assert "__Moderation__" not in text
    assert "__Custom Roles__" not in text
    assert "require moderator or administrator" in text
    assert ".ban" not in text


async def test_per_guild_prefix_override() -> None:
    """A /config prefix change applies to the next command per guild."""
    world = _world()
    guild = world["guild"]
    moderator = _moderator(guild)
    target = _target(guild)
    world["config_service"].update(guild.id, actor_user_id=1, changes={"command_prefix": "!"})

    # The old prefix no longer parses.
    old = _message(world, moderator, f".warn {target.mention} spam")
    assert await _dispatch(world, old) is False
    # The new prefix works.
    await _dispatch(world, _message(world, moderator, f"!warn {target.mention} spam"))

    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "warn"
    assert record.status == STATUS_SUCCESS


async def test_help_uses_per_guild_prefix() -> None:
    world = _world()
    guild = world["guild"]
    world["config_service"].update(guild.id, actor_user_id=1, changes={"command_prefix": "!"})
    await _dispatch(world, _message(world, _moderator(guild), "!help"))

    text = "\n".join(_replies(world))
    assert "prefix `!`" in text
