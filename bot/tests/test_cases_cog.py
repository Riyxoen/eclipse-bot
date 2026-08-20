"""Tests for the case-management commands (``/case``, ``/cases``,
``/moderation-history``): command registration, permission gating, guild
isolation, and not-found handling. Uses fakes only — no Discord connection."""

from __future__ import annotations

import pytest
from bot.cogs.cases import case_command, cases_command, moderation_history_command
from bot.configuration.settings import Settings
from bot.database.repository import MemoryCaseRepository
from bot.moderation.cases import STATUS_SUCCESS, CaseRecord, utc_now
from bot.moderation.errors import MissingModeratorPermissionError
from bot.permissions.checks import PermissionChecker
from bot.services.cases import CaseService
from bot.tests.fakes import (
    FakeBot,
    FakeClient,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    FakePermissions,
    FakeRole,
)
from discord import app_commands

# ------------------------------------------------------------------ helpers


def _case_service() -> CaseService:
    return CaseService(MemoryCaseRepository())


def _settings() -> Settings:
    return Settings(token="test-token")


def _permissions(bot: FakeBot) -> PermissionChecker:
    return PermissionChecker(bot, _settings())


def _moderator(guild: FakeGuild) -> FakeMember:
    return FakeMember(
        2,
        "mod",
        roles=[FakeRole(100, "mod", 7)],
        guild=guild,
        guild_permissions=FakePermissions(moderate_members=True),
    )


def _member(guild: FakeGuild, user_id: int = 3) -> FakeMember:
    return FakeMember(user_id, "target", roles=[FakeRole(50, "user", 1)], guild=guild)


def _make_context(
    *, service: CaseService, guild_id: int = 10
) -> tuple[FakeInteraction, FakeGuild, FakeMember]:
    """Build an interaction where a moderator asks about a target member."""
    guild = FakeGuild(guild_id, owner_id=1)
    moderator = _moderator(guild)
    bot = FakeBot()
    client = FakeClient(case_service=service, permissions=_permissions(bot))
    interaction = FakeInteraction(guild=guild, user=moderator, client=client)
    return interaction, guild, moderator


def _seed_case(service: CaseService, guild_id: int, target: int, **overrides) -> CaseRecord:
    values: dict = {
        "guild_id": guild_id,
        "target_user_id": target,
        "moderator_user_id": 2,
        "action": "warn",
        "reason": "spam",
        "created_at": utc_now(),
        "status": STATUS_SUCCESS,
    }
    values.update(overrides)
    return service.create(CaseRecord(**values))


# --------------------------------------------------------------- registration


def test_case_commands_are_top_level() -> None:
    for command in (case_command, cases_command, moderation_history_command):
        assert isinstance(command, app_commands.Command)
        assert command.parent is None
        assert command.guild_only is True


async def test_cases_and_moderation_history_share_implementation() -> None:
    """Both commands produce identical output for identical input."""
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    _seed_case(service, guild.id, target=3)
    target = _member(guild, user_id=3)

    await cases_command.callback(interaction, target)
    cases_text = interaction.response.messages[0]

    other, _, _ = _make_context(service=service)
    await moderation_history_command.callback(other, target)

    assert other.response.messages[0] == cases_text


def test_case_command_has_case_id_option() -> None:
    parameters = {p.name: p for p in case_command.parameters}
    assert "case_id" in parameters
    assert parameters["case_id"].required is True


def test_cases_command_has_member_option() -> None:
    parameters = {p.name: p for p in cases_command.parameters}
    assert "member" in parameters
    assert parameters["member"].required is True


# -------------------------------------------------------------- /case flows


async def test_case_command_shows_full_detail() -> None:
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    record = _seed_case(service, guild.id, target=3, action="timeout")

    await case_command.callback(interaction, record.case_id)

    text = interaction.response.messages[0]
    assert f"Case #{record.case_id}" in text
    assert "Action: Timeout" in text
    assert "Status: Success" in text


async def test_case_command_missing_case_returns_not_found() -> None:
    service = _case_service()
    interaction, _, _ = _make_context(service=service)

    await case_command.callback(interaction, 999)

    assert interaction.response.messages[0] == "Case not found."


async def test_case_command_invalid_id_returns_not_found() -> None:
    service = _case_service()
    interaction, _, _ = _make_context(service=service)

    await case_command.callback(interaction, -5)

    assert interaction.response.messages[0] == "Case not found."


async def test_case_command_enforces_guild_isolation() -> None:
    service = _case_service()
    record = _seed_case(service, guild_id=10, target=3)
    # Same case ID, but asked from a different guild.
    interaction, _, _ = _make_context(service=service, guild_id=20)

    await case_command.callback(interaction, record.case_id)

    assert interaction.response.messages[0] == "Case not found."


async def test_case_command_denies_regular_member() -> None:
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    regular = _member(guild)
    interaction.user = regular  # a non-moderator asks

    await case_command.callback(interaction, 1)

    assert "permission" in interaction.response.messages[0].lower()
    assert "Case" not in interaction.response.messages[0]


async def test_case_command_never_leaks_internal_errors() -> None:
    service = _case_service()
    interaction, _, _ = _make_context(service=service)

    await case_command.callback(interaction, 0)  # invalid IDs resolve to "not found"

    assert interaction.response.messages[0] == "Case not found."


# ------------------------------------------------------------- /cases flows


async def test_cases_command_lists_member_cases() -> None:
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    _seed_case(service, guild.id, target=3, action="warn")
    _seed_case(service, guild.id, target=3, action="kick")
    target = _member(guild, user_id=3)

    await cases_command.callback(interaction, target)

    text = interaction.response.messages[0]
    assert "Moderation cases for" in text
    assert "Warn" in text
    assert "Kick" in text


async def test_cases_command_empty_member() -> None:
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    target = _member(guild, user_id=3)

    await cases_command.callback(interaction, target)

    assert "No moderation cases found" in interaction.response.messages[0]


async def test_cases_command_isolates_guilds() -> None:
    service = _case_service()
    _seed_case(service, guild_id=10, target=3)
    interaction, guild, _ = _make_context(service=service, guild_id=20)
    target = _member(guild, user_id=3)

    await cases_command.callback(interaction, target)

    assert "No moderation cases found" in interaction.response.messages[0]


async def test_cases_command_respects_page_parameter() -> None:
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    for _ in range(25):
        _seed_case(service, guild.id, target=3)
    target = _member(guild, user_id=3)

    await cases_command.callback(interaction, target, page=3)

    text = interaction.response.messages[0]
    assert "page 3 of 3" in text
    assert "(25 total)" in text


async def test_cases_command_negative_page_is_clamped() -> None:
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    _seed_case(service, guild.id, target=3)
    target = _member(guild, user_id=3)

    await cases_command.callback(interaction, target, page=-2)

    assert "page 1 of 1" in interaction.response.messages[0]


async def test_moderation_history_aliases_cases() -> None:
    service = _case_service()
    interaction, guild, _ = _make_context(service=service)
    _seed_case(service, guild.id, target=3)
    target = _member(guild, user_id=3)

    await moderation_history_command.callback(interaction, target)

    assert "Moderation cases for" in interaction.response.messages[0]


# ------------------------------------------------------------- permission gate


async def test_permission_checker_requires_moderation_for_view_cases() -> None:
    guild = FakeGuild(10, owner_id=1)
    bot = FakeBot()
    checker = _permissions(bot)
    regular = FakeMember(3, guild=guild, guild_permissions=FakePermissions())
    with pytest.raises(MissingModeratorPermissionError):
        checker.require_moderator(regular, "view_cases")


async def test_permission_checker_allows_moderator_role_for_view_cases() -> None:
    guild = FakeGuild(10, owner_id=1)
    bot = FakeBot()
    settings = Settings(token="test-token", moderator_role_ids=(555,))
    checker = PermissionChecker(bot, settings)
    staff = FakeMember(3, roles=[FakeRole(555, "staff", 5)], guild=guild)
    checker.require_moderator(staff, "view_cases")  # must not raise
