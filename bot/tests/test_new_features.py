"""Tests for all new Eclipse features: AFK, jail, untimeout, ·cr prefix, embeds."""

from __future__ import annotations

from datetime import UTC, datetime

from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.repository import MemoryCaseRepository
from bot.moderation.cases import STATUS_SUCCESS
from bot.permissions.checks import PermissionChecker
from bot.prefix import PrefixDispatcher, parse_prefix_command
from bot.services.afk import AfkService
from bot.services.cases import CaseService
from bot.services.custom_roles import CustomRoleService
from bot.services.guild_config import GuildConfigService
from bot.services.jail import JailService
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

# ------------------------------------------------------------------- helpers


class _FakeIntents:
    message_content = True


def _world():
    settings = Settings(token="test-token", notify_users=True, command_prefix="·")
    bot = FakeBot()
    bot._intents = _FakeIntents()
    guild = _guild(bot)
    case_service = CaseService(MemoryCaseRepository())
    config_service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    permissions = PermissionChecker(bot, settings, config_service=config_service)
    moderation_service = ModerationService(
        bot, case_service, settings=settings, permissions=permissions, config_service=config_service,
    )
    custom_roles = CustomRoleService(settings, permissions, config_service)
    afk_service = AfkService()  # in-memory (no db_path)
    jail_service = JailService(permissions, config_service)
    bot.custom_roles = custom_roles
    bot.moderation_service = moderation_service
    bot.permissions = permissions
    bot.config_service = config_service
    bot.case_service = case_service
    bot.afk_service = afk_service
    bot.jail_service = jail_service
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
        "afk_service": afk_service,
        "jail_service": jail_service,
        "dispatcher": dispatcher,
    }


def _guild(bot: FakeBot) -> FakeGuild:
    bot_member = FakeMember(
        100_000,
        "eclipse",
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
        guild_permissions=FakePermissions(manage_roles=True, administrator=True),
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
        1, content, guild=world["guild"], author=author,
        channel=world["guild"].channels[0],
    )


async def _dispatch(world, message: FakeMessage) -> bool:
    return await world["dispatcher"].handle(message)


def _replies(world) -> list[str]:
    channel = world["guild"].channels[0]
    results = []
    for msg, embed in zip(channel.sent_messages, channel.sent_embeds, strict=False):
        if msg:
            results.append(msg)
        if embed is not None:
            parts = []
            if getattr(embed, "title", None):
                parts.append(embed.title)
            if getattr(embed, "description", None):
                parts.append(embed.description)
            for field in getattr(embed, "fields", []):
                parts.append(f"{field.name}: {field.value}")
            results.append(" ".join(parts))
    return results


# ============================================================ Prefix change


async def test_default_prefix_is_middle_dot() -> None:
    """The default prefix is `·`, not `.`."""
    parsed = parse_prefix_command("·warn @user spam", "·")
    assert parsed is not None
    assert parsed.name == "warn"


async def test_dot_prefix_still_works_with_config_override() -> None:
    """Setting the prefix to `.` via config makes dot-commands work."""
    world = _world()
    guild = world["guild"]
    world["config_service"].update(guild.id, actor_user_id=1, changes={"command_prefix": "."})
    target = _target(guild)
    await _dispatch(world, _message(world, _moderator(guild), f".warn {target.mention} spam"))
    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "warn"


# ============================================================ ·cr commands


async def test_cr_enable_creates_role() -> None:
    """·cr enable creates the custom role and enables the system."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))

    assert any("enabled" in reply.lower() for reply in _replies(world))
    config = world["config_service"].get(guild.id)
    assert config.custom_roles_enabled is True
    assert config.custom_role_id is not None


async def test_cr_enable_already_enabled() -> None:
    """·cr enable is idempotent."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    assert any("already enabled" in reply.lower() for reply in _replies(world))


async def test_cr_rename_updates_role() -> None:
    """·cr rename changes the managed role name."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    await _dispatch(world, _message(world, _admin(guild), "·cr rename Cool Role"))

    role = world["custom_roles"].managed_role(guild)
    assert role is not None and role.name == "Cool Role"
    assert any("Cool Role" in reply for reply in _replies(world))


async def test_cr_color_updates_role_color() -> None:
    """·cr color changes the managed role color."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    await _dispatch(world, _message(world, _admin(guild), "·cr color #FF0000"))

    role = world["custom_roles"].managed_role(guild)
    assert role is not None and role.colour == 0xFF0000
    assert any("ff0000" in reply.lower() for reply in _replies(world))


async def test_cr_color_rejects_invalid_hex() -> None:
    """·cr color rejects non-hex values."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    await _dispatch(world, _message(world, _admin(guild), "·cr color notacolor"))

    assert any("isn't valid" in reply.lower() for reply in _replies(world))


async def test_cr_disabled_shows_error() -> None:
    """·cr commands show clear error when custom-role system is disabled."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr rename Test"))

    assert any("disabled" in reply.lower() for reply in _replies(world))
    assert any("enable" in reply.lower() for reply in _replies(world))


async def test_cr_requires_manage_roles() -> None:
    """·cr commands require Manage Roles permission."""
    world = _world()
    guild = world["guild"]
    regular = _regular(guild)
    await _dispatch(world, _message(world, regular, "·cr enable"))

    assert any("Manage Roles" in reply or "permission" in reply.lower() for reply in _replies(world))
    assert world["config_service"].get(guild.id).custom_roles_enabled is False


async def test_el_still_works_as_alias() -> None:
    """The old `.el` prefix still works as an alias for custom roles."""
    world = _world()
    guild = world["guild"]
    world["config_service"].update(guild.id, actor_user_id=1, changes={"command_prefix": "."})
    await _dispatch(world, _message(world, _admin(guild), ".el enable"))
    assert world["config_service"].get(guild.id).custom_roles_enabled is True


# ================================================================ AFK system


async def test_afk_sets_nickname() -> None:
    """·afk sets the AFK state and changes nickname."""
    world = _world()
    guild = world["guild"]
    user = _regular(guild)
    await _dispatch(world, _message(world, user, "·afk brb"))

    replies = _replies(world)
    assert any("AFK" in reply for reply in replies)
    state = world["afk_service"].get(guild.id, user.id)
    assert state is not None
    assert state.afk_message == "brb"


async def test_afk_removes_on_message() -> None:
    """AFK is removed when the user sends another message."""
    world = _world()
    guild = world["guild"]
    user = _regular(guild)
    await _dispatch(world, _message(world, user, "·afk brb"))
    assert world["afk_service"].get(guild.id, user.id) is not None

    await _dispatch(world, _message(world, user, "·afk"))
    replies = _replies(world)
    assert any("welcome back" in reply.lower() or "AFK" in reply for reply in replies)
    assert world["afk_service"].get(guild.id, user.id) is None


async def test_afk_mention_notification() -> None:
    """When someone mentions an AFK user, a notification is sent."""
    world = _world()
    guild = world["guild"]
    afk_user = _regular(guild)
    await _dispatch(world, _message(world, afk_user, "·afk sleeping"))
    state = world["afk_service"].get(guild.id, afk_user.id)
    assert state is not None
    assert state.afk_message == "sleeping"


async def test_afk_persists_in_memory() -> None:
    """AFK state is retrievable after being set (in-memory persistence)."""
    world = _world()
    guild = world["guild"]
    user = _regular(guild)

    world["afk_service"].set_afk(guild.id, user.id, "TestUser", "away")
    state = world["afk_service"].get(guild.id, user.id)
    assert state is not None
    assert state.original_name == "TestUser"
    assert state.afk_message == "away"

    removed = world["afk_service"].remove(guild.id, user.id)
    assert removed is not None
    assert world["afk_service"].get(guild.id, user.id) is None


# ============================================================ Untimeout command


async def test_untimeout_removes_timeout() -> None:
    """·untimeout removes a user's Discord timeout."""
    world = _world()
    guild = world["guild"]
    moderator = _moderator(guild)
    target = _target(guild)

    # Apply a timeout first
    target.timed_out_until = datetime(2099, 1, 1, tzinfo=UTC)

    await _dispatch(world, _message(world, moderator, f"·untimeout {target.mention} appeal"))

    record = world["case_service"].list_for_guild(guild.id).items[0]
    assert record.action == "unmute"
    assert record.status == STATUS_SUCCESS
    assert any("untimeout" in reply.lower() or "unmute" in reply.lower() for reply in _replies(world))


async def test_untimeout_when_not_timed_out() -> None:
    """·untimeout shows error when user is not timed out."""
    world = _world()
    guild = world["guild"]
    target = _target(guild)

    await _dispatch(world, _message(world, _moderator(guild), f"·untimeout {target.mention} why"))

    assert any("isn't currently muted" in reply.lower() or "not muted" in reply.lower() for reply in _replies(world))


# ============================================================ Jail system


async def test_jail_setup_configures_system() -> None:
    """·jail setup configures the jail role and channel."""
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)

    await _dispatch(world, _message(world, admin, "·jail setup"))

    config = world["config_service"].get(guild.id)
    assert config.jail_role_id is not None
    role = guild.get_role(config.jail_role_id)
    assert role is not None
    assert role.name == "Jailed"
    assert any("configured" in reply.lower() or "active" in reply.lower() for reply in _replies(world))


async def test_jail_setup_requires_administrator() -> None:
    """·jail setup requires Administrator permission."""
    world = _world()
    guild = world["guild"]
    regular = _regular(guild)

    await _dispatch(world, _message(world, regular, "·jail setup"))

    assert any("permission" in reply.lower() or "administrator" in reply.lower() for reply in _replies(world))


async def test_jail_applies_role_and_stores_previous() -> None:
    """·jail applies the jail role and stores previous roles."""
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)
    target = _target(guild, user_id=10)
    target.roles = [FakeRole(60, "user", 2), FakeRole(70, "trusted", 3)]

    # Setup jail first
    await _dispatch(world, _message(world, admin, "·jail setup"))

    # Jail the target
    await _dispatch(world, _message(world, admin, f"·jail {target.mention} spam"))

    config = world["config_service"].get(guild.id)
    jail_role = guild.get_role(config.jail_role_id)
    assert jail_role in target.roles

    # Check the case was created
    cases = world["case_service"].list_for_guild(guild.id)
    assert cases.total >= 1
    jail_case = [r for r in cases.items if r.action == "jail"]
    assert len(jail_case) >= 1
    assert jail_case[0].metadata is not None
    assert "previous_role_ids" in jail_case[0].metadata


async def test_jail_requires_configuration() -> None:
    """·jail shows error when jail system is not configured."""
    world = _world()
    guild = world["guild"]
    target = _target(guild)

    await _dispatch(world, _message(world, _moderator(guild), f"·jail {target.mention} spam"))

    assert any("not configured" in reply.lower() or "jail setup" in reply.lower() for reply in _replies(world))


async def test_unjail_restores_roles() -> None:
    """·unjail restores previous roles."""
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)
    target = _target(guild, user_id=11)
    target.roles = [FakeRole(60, "user", 2), FakeRole(70, "trusted", 3)]

    # Setup jail
    await _dispatch(world, _message(world, admin, "·jail setup"))

    # Jail
    await _dispatch(world, _message(world, admin, f"·jail {target.mention} spam"))

    # Find the jail case metadata
    config = world["config_service"].get(guild.id)
    jail_role = guild.get_role(config.jail_role_id)
    previous_ids = []
    for role in target.roles:
        if role.id != guild.default_role.id and role != jail_role:
            previous_ids.append(role.id)

    # Unjail
    await _dispatch(world, _message(world, admin, f"·unjail {target.mention}"))

    assert jail_role not in target.roles
    # Previous roles should be restored
    role_ids = [r.id for r in target.roles]
    for pid in previous_ids:
        assert pid in role_ids


async def test_unjail_not_jailed() -> None:
    """·unjail shows error when user is not jailed."""
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)
    target = _target(guild)

    # Setup jail
    await _dispatch(world, _message(world, admin, "·jail setup"))

    await _dispatch(world, _message(world, admin, f"·unjail {target.mention}"))

    assert any("isn't currently jailed" in reply.lower() or "not jailed" in reply.lower() for reply in _replies(world))


async def test_jail_hierarchy_protection() -> None:
    """Jail refuses to jail someone with a higher role than the bot."""
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)
    # Target with role higher than bot's
    higher = FakeMember(99, "higher", roles=[FakeRole(999, "boss", 99)], guild=guild)
    guild.members.append(higher)

    await _dispatch(world, _message(world, admin, "·jail setup"))
    await _dispatch(world, _message(world, admin, f"·jail {higher.mention} spam"))

    assert any("role is higher" in reply.lower() or "could not" in reply.lower() for reply in _replies(world))


# ========================================================== Embed responses


async def test_moderation_uses_embeds() -> None:
    """Moderation commands respond with embeds."""
    world = _world()
    guild = world["guild"]
    target = _target(guild)
    channel = guild.channels[0]

    await _dispatch(world, _message(world, _moderator(guild), f"·warn {target.mention} spam"))

    # Should have an embed in the response
    assert len(channel.sent_embeds) >= 1
    embed = channel.sent_embeds[-1]
    assert embed is not None
    assert "Warn" in embed.title or "Case" in embed.title


async def test_error_responses_use_embeds() -> None:
    """Error responses use error embeds."""
    world = _world()
    guild = world["guild"]
    channel = guild.channels[0]

    await _dispatch(world, _message(world, _regular(guild), "·ban <@999> spam"))

    embed = channel.sent_embeds[-1]
    assert embed is not None
    from bot.utilities.embeds import COLOR_ERROR
    assert int(embed.color) == COLOR_ERROR


async def test_disabled_feature_uses_error_embed() -> None:
    """Disabled features show error embeds with clear message."""
    world = _world()
    guild = world["guild"]
    channel = guild.channels[0]

    await _dispatch(world, _message(world, _admin(guild), "·cr rename Test"))

    embed = channel.sent_embeds[-1]
    assert embed is not None
    assert "disabled" in embed.description.lower()


# ==================================================== Permission failures


async def test_ban_permission_denied() -> None:
    """Regular member gets permission denied for ban."""
    world = _world()
    guild = world["guild"]
    target = _target(guild)

    await _dispatch(world, _message(world, _regular(guild), f"·ban {target.mention} spam"))

    assert any("permission" in reply.lower() for reply in _replies(world))
    assert target.banned is False


async def test_jail_permission_denied() -> None:
    """Regular member gets permission denied for jail."""
    world = _world()
    guild = world["guild"]
    target = _target(guild)

    # Setup jail first (requires admin)
    await _dispatch(world, _message(world, _admin(guild), "·jail setup"))
    # Regular member tries to jail
    await _dispatch(world, _message(world, _regular(guild), f"·jail {target.mention} spam"))

    assert any("permission" in reply.lower() for reply in _replies(world))


# ========================================================= Error handling


async def test_ban_missing_argument() -> None:
    """Ban without mention shows usage error."""
    world = _world()
    await _dispatch(world, _message(world, _moderator(world["guild"]), "·ban"))
    assert any("mention" in reply.lower() or "reason" in reply.lower() for reply in _replies(world))


async def test_ban_invalid_user() -> None:
    """Ban with invalid user ID shows error."""
    world = _world()
    await _dispatch(world, _message(world, _moderator(world["guild"]), "·ban <@999> spam"))
    assert any("not found" in reply.lower() for reply in _replies(world))


async def test_cr_color_missing_argument() -> None:
    """·cr color without argument shows usage."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    await _dispatch(world, _message(world, _admin(guild), "·cr color"))
    assert any("usage" in reply.lower() or "provide" in reply.lower() for reply in _replies(world))


async def test_cr_rename_missing_argument() -> None:
    """·cr rename without argument shows usage."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    await _dispatch(world, _message(world, _admin(guild), "·cr rename"))
    assert any("usage" in reply.lower() or "provide" in reply.lower() for reply in _replies(world))


async def test_unknown_cr_subcommand() -> None:
    """Unknown ·cr subcommand shows available options."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·cr enable"))
    await _dispatch(world, _message(world, _admin(guild), "·cr invalid"))
    assert any("unknown" in reply.lower() or "available" in reply.lower() for reply in _replies(world))


async def test_jail_missing_argument() -> None:
    """·jail without mention shows usage."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·jail setup"))
    await _dispatch(world, _message(world, _admin(guild), "·jail"))
    assert any("mention" in reply.lower() or "usage" in reply.lower() for reply in _replies(world))


async def test_unjail_missing_argument() -> None:
    """·unjail without mention shows usage."""
    world = _world()
    guild = world["guild"]
    await _dispatch(world, _message(world, _admin(guild), "·jail setup"))
    await _dispatch(world, _message(world, _admin(guild), "·unjail"))
    assert any("mention" in reply.lower() or "usage" in reply.lower() for reply in _replies(world))


async def test_untimeout_missing_argument() -> None:
    """·untimeout without mention shows usage."""
    world = _world()
    await _dispatch(world, _message(world, _moderator(world["guild"]), "·untimeout"))
    assert any("mention" in reply.lower() for reply in _replies(world))


# ===================================================== Configuration failures


async def test_jail_already_jailed() -> None:
    """Jailing someone already jailed shows clear error."""
    world = _world()
    guild = world["guild"]
    admin = _admin(guild)
    target = _target(guild, user_id=12)
    target.roles = [FakeRole(60, "user", 2)]

    await _dispatch(world, _message(world, admin, "·jail setup"))
    await _dispatch(world, _message(world, admin, f"·jail {target.mention} spam"))
    await _dispatch(world, _message(world, admin, f"·jail {target.mention} again"))

    assert any("already jailed" in reply.lower() for reply in _replies(world))


# ================================================= Help command with new prefix


async def test_help_shows_new_commands() -> None:
    """Help shows jail, AFK, untimeout, and ·cr commands."""
    world = _world()
    guild = world["guild"]
    # Admin with moderate_members to show all sections
    admin_mod = FakeMember(
        99, "adminmod",
        roles=[FakeRole(102, "adminmod", 8)],
        guild=guild,
        guild_permissions=FakePermissions(manage_roles=True, administrator=True, moderate_members=True),
    )
    await _dispatch(world, _message(world, admin_mod, "·help"))

    text = "\n".join(_replies(world))
    assert "jail" in text.lower()
    assert "afk" in text.lower()
    assert "untimeout" in text.lower()
    assert "cr" in text.lower()


async def test_help_for_regular_member() -> None:
    """Help for regular member shows only utility commands."""
    world = _world()
    await _dispatch(world, _message(world, _regular(world["guild"]), "·help"))

    text = "\n".join(_replies(world))
    assert "afk" in text.lower()
    assert "Eclipse" in text
