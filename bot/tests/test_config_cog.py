"""Tests for the ``/config`` command group (Phase 5).

The command layer is tested separately from the service: it is driven
through the real command callbacks with lightweight Discord fakes, covering
the administrator permission gate (owner / administrator permission /
administrator role), validation errors, channel verification, exemption
management, reset confirmation, and log-channel announcements. No real
Discord server is contacted.
"""

from __future__ import annotations

import pytest
from bot.cogs.config import ConfigCog
from bot.configuration.settings import Settings
from bot.database.config_repository import MemoryGuildConfigRepository
from bot.database.repository import MemoryCaseRepository
from bot.moderation.errors import MissingAdministratorPermissionError
from bot.permissions.checks import PermissionChecker
from bot.services.cases import CaseService
from bot.services.guild_config import GuildConfigService
from bot.services.moderation import ModerationService
from bot.tests.fakes import (
    FakeBot,
    FakeChannel,
    FakeClient,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    FakePermissions,
    FakeRole,
)


def _command(group, name):
    for child in group.commands:
        if child.name == name:
            return child
    raise AssertionError(f"command {name!r} not found in group")


def _settings() -> Settings:
    return Settings(token="test-token")


def _make_world(*, guild_id: int = 10):
    settings = _settings()
    bot = FakeBot()
    config_service = GuildConfigService(MemoryGuildConfigRepository(), settings=settings)
    case_service = CaseService(MemoryCaseRepository())
    permissions = PermissionChecker(bot, settings, config_service=config_service)
    moderation_service = ModerationService(
        bot, case_service, settings=settings, permissions=permissions, config_service=config_service
    )
    cog = ConfigCog(bot, config_service, moderation_service)
    return {
        "settings": settings,
        "bot": bot,
        "config_service": config_service,
        "permissions": permissions,
        "moderation_service": moderation_service,
        "cog": cog,
    }


def _guild(guild_id: int = 10, *, owner_id: int = 1) -> FakeGuild:
    bot_member = FakeMember(
        100_000,
        "riyxoen",
        roles=[FakeRole(900, "bot", 9)],
        guild_permissions=FakePermissions(administrator=True),
        bot=True,
    )
    guild = FakeGuild(guild_id, owner_id=owner_id, me=bot_member, members=[bot_member])
    return guild


def _interaction(world, guild: FakeGuild, user: FakeMember) -> FakeInteraction:
    client = FakeClient(permissions=world["permissions"])
    return FakeInteraction(guild=guild, user=user, client=client)


def _owner(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        guild.owner_id or 1,
        "owner",
        roles=[FakeRole(1, "@everyone", 0)],
        guild=guild,
        guild_permissions=FakePermissions(),
    )


def _admin_by_permission(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        2,
        "admin",
        roles=[FakeRole(100, "admin", 7)],
        guild=guild,
        guild_permissions=FakePermissions(administrator=True),
    )


def _moderator_only(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        3,
        "mod",
        roles=[FakeRole(101, "mod", 6)],
        guild=guild,
        guild_permissions=FakePermissions(moderate_members=True),
    )


def _regular(guild: FakeGuild) -> FakeMember:
    return FakeMember(4, "member", roles=[FakeRole(50, "user", 1)], guild=guild)


# ---------------------------------------------------------- registration


def test_config_group_structure() -> None:
    world = _make_world()
    cog = world["cog"]
    top = sorted(child.name for child in cog.commands)
    assert top == ["exemptions", "logs", "moderation", "prefix", "reset", "roles", "view"]

    moderation = sorted(child.name for child in cog.moderation.commands)
    assert "spam" in moderation and "duplicate" in moderation and "mentions" in moderation
    assert "links" in moderation and "words" in moderation and "domains" in moderation
    assert "escalation" in moderation and "cooldown" in moderation
    assert "timeout-duration" in moderation and "purge-max" in moderation and "dm" in moderation
    assert "automod" in moderation

    roles = sorted(child.name for child in cog.roles.commands)
    assert roles == [
        "administrator-add",
        "administrator-remove",
        "moderator-add",
        "moderator-remove",
    ]

    exemptions = sorted(child.name for child in cog.exemptions.commands)
    assert exemptions == [
        "channel-add",
        "channel-remove",
        "role-add",
        "role-remove",
        "user-add",
        "user-remove",
    ]


# --------------------------------------------------------- permission gate


async def test_regular_member_denied_everywhere() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _regular(guild))

    await _command(world["cog"], "view").callback(world["cog"], interaction)
    assert "Administrator" in interaction.response.messages[0]

    interaction.response.messages.clear()
    await _command(world["cog"].moderation, "spam").callback(
        world["cog"], interaction, threshold=5, window_seconds=5
    )
    assert "Administrator" in interaction.response.messages[0]


async def test_moderator_without_administrator_denied() -> None:
    """A regular moderator must not receive sensitive configuration."""
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator_only(guild))

    await _command(world["cog"], "reset").callback(world["cog"], interaction, confirm=True)

    assert "Administrator" in interaction.response.messages[0]


async def test_owner_allowed() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "view").callback(world["cog"], interaction)

    assert "Server configuration" in interaction.response.messages[0]


async def test_administrator_permission_allowed() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _admin_by_permission(guild))

    await _command(world["cog"], "view").callback(world["cog"], interaction)

    assert "Server configuration" in interaction.response.messages[0]


async def test_configured_administrator_role_allowed() -> None:
    world = _make_world()
    guild = _guild()
    world["config_service"].add_role(guild.id, actor_user_id=1, kind="administrator", role_id=777)
    admin = FakeMember(5, "role-admin", roles=[FakeRole(777, "admins", 8)], guild=guild)
    interaction = _interaction(world, guild, admin)

    await _command(world["cog"], "view").callback(world["cog"], interaction)

    assert "Server configuration" in interaction.response.messages[0]


def test_require_administrator_raises_for_regular_member() -> None:
    world = _make_world()
    guild = _guild()
    with pytest.raises(MissingAdministratorPermissionError):
        world["permissions"].require_administrator(_regular(guild), guild)


# ------------------------------------------------------------------- view


async def test_view_shows_settings() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "view").callback(world["cog"], interaction)

    text = interaction.response.messages[0]
    assert "Server configuration" in text
    assert "Automated moderation" in text
    assert "Maximum purge" in text
    assert "Moderator roles" in text
    assert "Link action" in text
    assert "Blocked terms" in text

    # Escalation renders on the later page (pagination).
    interaction.response.messages.clear()
    await _command(world["cog"], "view").callback(world["cog"], interaction, page=2)
    assert "Warning escalation" in interaction.response.messages[0]


async def test_view_resolves_role_and_channel_labels() -> None:
    world = _make_world()
    guild = _guild()
    guild.roles = [FakeRole(555, "staff", 5)]
    guild.channels = [FakeChannel(222, "logs", guild=guild)]
    world["config_service"].add_role(guild.id, actor_user_id=1, kind="moderator", role_id=555)
    world["config_service"].update(
        guild.id,
        actor_user_id=1,
        changes={"log_channel_id": 222, "mod_log_enabled": True},
    )
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "view").callback(world["cog"], interaction)

    text = interaction.response.messages[0]
    assert "<@&555>" in text  # resolved role mention
    assert "<#222>" in text  # resolved channel mention


async def test_view_handles_deleted_roles_safely() -> None:
    world = _make_world()
    guild = _guild()
    world["config_service"].add_role(guild.id, actor_user_id=1, kind="moderator", role_id=999_999)
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "view").callback(world["cog"], interaction)

    text = interaction.response.messages[0]
    assert "deleted role" in text
    assert "999999" in text


async def test_view_paginates_large_config() -> None:
    world = _make_world()
    guild = _guild()
    for i in range(20):
        world["config_service"].add_blocked_term(guild.id, actor_user_id=1, term=f"term{i}")
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "view").callback(world["cog"], interaction, page=2)

    text = interaction.response.messages[0]
    assert "page 2/" in text


# -------------------------------------------------------------- moderation


async def test_moderation_spam_updates_settings() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "spam").callback(
        world["cog"], interaction, threshold=7, window_seconds=10, action="warn"
    )

    assert "Updated **spam detection**" in interaction.response.messages[0]
    assert "spam_threshold" in interaction.response.messages[0]
    assert world["config_service"].get(guild.id).spam_threshold == 7
    assert world["config_service"].get(guild.id).spam_action == "warn"


async def test_moderation_spam_rejects_invalid_threshold() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "spam").callback(
        world["cog"], interaction, threshold=1, window_seconds=5
    )

    assert "between 2 and" in interaction.response.messages[0]
    assert world["config_service"].get(guild.id).spam_threshold == 5  # unchanged


async def test_moderation_mentions_updates_only_provided_dimensions() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "mentions").callback(
        world["cog"], interaction, user_threshold=25
    )

    config = world["config_service"].get(guild.id)
    assert config.mention_user_threshold == 25
    assert config.mention_role_threshold == 6  # untouched


async def test_moderation_mentions_requires_something() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "mentions").callback(world["cog"], interaction)

    assert "at least one" in interaction.response.messages[0]


async def test_moderation_escalation_parses_and_validates() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "escalation").callback(
        world["cog"], interaction, spec="3:3600,5:43200"
    )
    assert world["config_service"].get(guild.id).escalation == ((3, 3600), (5, 43200))

    interaction.response.messages.clear()
    await _command(world["cog"].moderation, "escalation").callback(
        world["cog"], interaction, spec="3:999999999"
    )
    assert "28 days" in interaction.response.messages[0]


async def test_moderation_purge_max_validates_upper_limit() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "purge-max").callback(
        world["cog"], interaction, amount=5000
    )

    assert "between 1 and 1000" in interaction.response.messages[0]
    assert world["config_service"].get(guild.id).max_purge_amount == 100


async def test_moderation_words_add_remove_list() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "words").callback(
        world["cog"], interaction, sub="add", term="BadWord"
    )
    assert world["config_service"].get(guild.id).blocked_terms == ("badword",)

    interaction.response.messages.clear()
    await _command(world["cog"].moderation, "words").callback(world["cog"], interaction, sub="list")
    assert "badword" in interaction.response.messages[0]

    interaction.response.messages.clear()
    await _command(world["cog"].moderation, "words").callback(
        world["cog"], interaction, sub="remove", term="BADWORD"
    )
    assert world["config_service"].get(guild.id).blocked_terms == ()


async def test_moderation_words_requires_term_for_add() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "words").callback(
        world["cog"], interaction, sub="add", term=None
    )

    assert "value is required" in interaction.response.messages[0]


async def test_moderation_domains_allow() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "domains").callback(
        world["cog"], interaction, sub="add", domain="GitHub.com"
    )

    assert world["config_service"].get(guild.id).allowed_domains == ("github.com",)

    interaction.response.messages.clear()
    await _command(world["cog"].moderation, "domains").callback(
        world["cog"], interaction, sub="list"
    )
    assert "github.com" in interaction.response.messages[0]


# ------------------------------------------------------------------- logs


async def test_logs_sets_channel_and_enables() -> None:
    world = _make_world()
    guild = _guild()
    channel = FakeChannel(222, "logs", guild=guild)
    guild.channels = [channel]
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "logs").callback(world["cog"], interaction, channel=channel)

    config = world["config_service"].get(guild.id)
    assert config.log_channel_id == 222
    assert config.mod_log_enabled is True  # bot has send/embed perms on the channel


async def test_logs_warns_when_bot_cannot_post() -> None:
    world = _make_world()
    guild = _guild()
    channel = FakeChannel(
        222,
        "logs",
        guild=guild,
        permissions_for_bot=FakePermissions(send_messages=False, embed_links=False),
    )
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "logs").callback(world["cog"], interaction, channel=channel)

    text = interaction.response.messages[0]
    assert "can't send embeds" in text
    # The channel is still saved; the problem is reported clearly.
    assert world["config_service"].get(guild.id).log_channel_id == 222


async def test_logs_rejects_channel_from_another_guild() -> None:
    world = _make_world()
    guild = _guild()
    foreign = FakeChannel(999, "elsewhere", guild=FakeGuild(99))
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "logs").callback(world["cog"], interaction, channel=foreign)

    assert "must be in this server" in interaction.response.messages[0]


async def test_logs_requires_channel_or_enabled() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "logs").callback(
        world["cog"], interaction, channel=None, enabled=None
    )

    assert "channel and/or an enabled value" in interaction.response.messages[0]


# ------------------------------------------------------------------ roles


async def test_roles_moderator_add_and_remove() -> None:
    world = _make_world()
    guild = _guild()
    role = FakeRole(555, "staff", 5)
    guild.roles = [role]
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].roles, "moderator-add").callback(
        world["cog"], interaction, role=role
    )
    assert world["config_service"].get(guild.id).moderator_role_ids == (555,)
    assert "Added moderator role" in interaction.response.messages[0]

    interaction.response.messages.clear()
    await _command(world["cog"].roles, "moderator-remove").callback(
        world["cog"], interaction, role_id="555"
    )
    assert world["config_service"].get(guild.id).moderator_role_ids == ()


async def test_roles_administrator_remove_by_id_after_deletion() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))
    world["config_service"].add_role(
        guild.id, actor_user_id=1, kind="administrator", role_id=999_999
    )

    await _command(world["cog"].roles, "administrator-remove").callback(
        world["cog"], interaction, role_id="999999"
    )

    assert world["config_service"].get(guild.id).administrator_role_ids == ()
    assert "Removed administrator role" in interaction.response.messages[0]


async def test_roles_rejects_invalid_id() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].roles, "moderator-remove").callback(
        world["cog"], interaction, role_id="abc"
    )

    assert "positive ID" in interaction.response.messages[0]


# -------------------------------------------------------------- exemptions


async def test_exemptions_user_add_and_remove() -> None:
    world = _make_world()
    guild = _guild()
    member = FakeMember(3, "target", roles=[FakeRole(50, "user", 1)], guild=guild)
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].exemptions, "user-add").callback(
        world["cog"], interaction, member=member
    )
    assert world["config_service"].get(guild.id).exempt_user_ids == (3,)

    interaction.response.messages.clear()
    await _command(world["cog"].exemptions, "user-remove").callback(
        world["cog"], interaction, user_id="3"
    )
    assert world["config_service"].get(guild.id).exempt_user_ids == ()


async def test_exemptions_role_add_and_channel_add() -> None:
    world = _make_world()
    guild = _guild()
    role = FakeRole(777, "trusted", 5)
    channel = FakeChannel(42, "bot-spam", guild=guild)
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].exemptions, "role-add").callback(
        world["cog"], interaction, role=role
    )
    await _command(world["cog"].exemptions, "channel-add").callback(
        world["cog"], interaction, channel=channel
    )

    config = world["config_service"].get(guild.id)
    assert config.exempt_role_ids == (777,)
    assert config.exempt_channel_ids == (42,)


async def test_exemptions_rejects_foreign_member() -> None:
    world = _make_world()
    guild = _guild()
    foreign = FakeMember(9, "outsider", guild=FakeGuild(99))
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].exemptions, "user-add").callback(
        world["cog"], interaction, member=foreign
    )

    assert "must be in this server" in interaction.response.messages[0]


async def test_exemptions_duplicate_rejected() -> None:
    world = _make_world()
    guild = _guild()
    member = FakeMember(3, "target", guild=guild)
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].exemptions, "user-add").callback(
        world["cog"], interaction, member=member
    )
    interaction.response.messages.clear()
    await _command(world["cog"].exemptions, "user-add").callback(
        world["cog"], interaction, member=member
    )

    assert "already configured" in interaction.response.messages[0]


# ------------------------------------------------------------------ reset


async def test_reset_requires_confirmation() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))
    world["config_service"].update(
        guild.id, actor_user_id=1, changes={"spam_threshold": 9, "notify_users": False}
    )

    await _command(world["cog"], "reset").callback(world["cog"], interaction, confirm=False)

    text = interaction.response.messages[0]
    assert "reset **all** server configuration" in text
    assert "Moderation cases are NOT affected" in text
    # Nothing changed yet.
    assert world["config_service"].get(guild.id).spam_threshold == 9


async def test_reset_with_confirmation_restores_defaults() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))
    world["config_service"].update(
        guild.id, actor_user_id=1, changes={"spam_threshold": 9, "notify_users": False}
    )

    await _command(world["cog"], "reset").callback(world["cog"], interaction, confirm=True)

    config = world["config_service"].get(guild.id)
    assert config.spam_threshold == 5
    assert config.notify_users is True
    assert "reset to defaults" in interaction.response.messages[0]


async def test_reset_does_not_touch_cases() -> None:
    world = _make_world()
    guild = _guild()
    from bot.moderation.cases import STATUS_SUCCESS, CaseRecord, utc_now

    record = world["moderation_service"].case_service.create(
        CaseRecord(
            guild_id=guild.id,
            target_user_id=3,
            moderator_user_id=2,
            action="warn",
            reason="spam",
            created_at=utc_now(),
            status=STATUS_SUCCESS,
        )
    )
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "reset").callback(world["cog"], interaction, confirm=True)

    assert world["moderation_service"].case_service.get(guild.id, record.case_id) is not None


# ------------------------------------------------------------------ prefix


async def test_prefix_updates_command_prefix() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"], "prefix").callback(world["cog"], interaction, prefix="!")

    assert world["config_service"].get(guild.id).command_prefix == "!"
    assert "Updated **command prefix**" in interaction.response.messages[0]


async def test_prefix_rejects_invalid_values() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _owner(guild))

    prefix = _command(world["cog"], "prefix")
    await prefix.callback(world["cog"], interaction, prefix="!!!!")
    assert "can't be longer than 3" in interaction.response.messages[0]
    assert world["config_service"].get(guild.id).command_prefix == "·"  # unchanged

    interaction.response.messages.clear()
    await prefix.callback(world["cog"], interaction, prefix="a b")
    assert "can't contain spaces" in interaction.response.messages[0]

    interaction.response.messages.clear()
    await prefix.callback(world["cog"], interaction, prefix="@")
    assert "can't contain" in interaction.response.messages[0]


async def test_prefix_requires_administrator() -> None:
    world = _make_world()
    guild = _guild()
    interaction = _interaction(world, guild, _moderator_only(guild))

    await _command(world["cog"], "prefix").callback(world["cog"], interaction, prefix="!")

    assert "Administrator" in interaction.response.messages[0]
    assert world["config_service"].get(guild.id).command_prefix == "·"


# -------------------------------------------------------- log channel announce


async def test_config_change_posts_summary_to_log_channel() -> None:
    world = _make_world()
    guild = _guild()
    channel = FakeChannel(222, "logs", guild=guild)
    guild.channels = [channel]
    world["config_service"].update(
        guild.id,
        actor_user_id=1,
        changes={"log_channel_id": 222, "mod_log_enabled": True},
    )
    interaction = _interaction(world, guild, _owner(guild))

    await _command(world["cog"].moderation, "cooldown").callback(
        world["cog"], interaction, seconds=30
    )

    assert channel.sent_embeds  # an embed was posted to the log channel
    embed = channel.sent_embeds[0]
    assert embed.title == "Configuration change"
