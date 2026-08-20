"""Case-management commands: ``/case``, ``/cases``, ``/moderation-history``.

These are top-level commands (registered directly on the tree). They stay
thin: verify the moderator is authorized via the shared permission checker,
then delegate to the case service. ``/cases`` and ``/moderation-history`` are
two spellings of the same command sharing one implementation.

Moderation history is moderation data: only authorized moderators (required
Discord permission or a configured moderator role) can view it. Regular
members get the standard safe denial message.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from bot.moderation.actions import ACTION_VIEW_CASES
from bot.moderation.errors import ModerationError
from bot.moderation.response import format_case_detail, format_case_list
from bot.services.cases import CaseService

logger = logging.getLogger("riyxoen.cases_cog")

#: Number of cases shown per page in list views.
PAGE_SIZE = 10

_CASE_ID_DESCRIPTION = "The case ID to show (e.g. 42)."
_MEMBER_DESCRIPTION = "The member whose moderation history to show."
_PAGE_DESCRIPTION = f"Page number (default 1, {PAGE_SIZE} cases per page)."


def _require_view_permissions(interaction: discord.Interaction) -> None:
    """Gate case viewing behind the shared permission system."""
    permissions = interaction.client.permissions
    permissions.require_guild(interaction.guild)
    permissions.require_moderator(interaction.user, ACTION_VIEW_CASES)


def _user_mention(client: discord.Client, guild: discord.Guild, user_id: int) -> str:
    """Best-effort mention from cached data; never falls back to a username."""
    member = guild.get_member(user_id)
    if member is not None:
        return member.mention
    user = client.get_user(user_id)
    if user is not None:
        return user.mention
    return f"<@{user_id}>"


async def _respond(interaction: discord.Interaction, content: str) -> None:
    await interaction.response.send_message(content, ephemeral=True)


@app_commands.command(name="case", description="Show details for one moderation case.")
@app_commands.guild_only()
@app_commands.describe(case_id=_CASE_ID_DESCRIPTION)
async def case_command(interaction: discord.Interaction, case_id: int) -> None:
    """Show the full record for a single case (guild-scoped)."""
    try:
        _require_view_permissions(interaction)
    except ModerationError as exc:
        await _respond(interaction, exc.user_message)
        return

    service: CaseService = interaction.client.case_service
    record = service.get(interaction.guild.id, case_id)
    if record is None:
        await _respond(interaction, "Case not found.")
        return

    await _respond(
        interaction,
        format_case_detail(
            record,
            target_label=_user_mention(
                interaction.client, interaction.guild, record.target_user_id
            ),
            moderator_label=_user_mention(
                interaction.client, interaction.guild, record.moderator_user_id
            ),
        ),
    )


async def _list_cases(interaction: discord.Interaction, member: discord.Member, page: int) -> None:
    """Shared implementation for ``/cases`` and ``/moderation-history``."""
    try:
        _require_view_permissions(interaction)
    except ModerationError as exc:
        await _respond(interaction, exc.user_message)
        return

    page_number = max(int(page), 1)
    service: CaseService = interaction.client.case_service
    result = service.list_for_member(
        interaction.guild.id, member.id, page=page_number, page_size=PAGE_SIZE
    )
    if not result.items:
        await _respond(interaction, f"No moderation cases found for {member.mention}.")
        return

    def label(user_id: int) -> str:
        return _user_mention(interaction.client, interaction.guild, user_id)

    await _respond(interaction, format_case_list(result, member_label=member.mention, label=label))


@app_commands.command(name="cases", description="Show recent moderation cases for a member.")
@app_commands.guild_only()
@app_commands.describe(member=_MEMBER_DESCRIPTION, page=_PAGE_DESCRIPTION)
async def cases_command(
    interaction: discord.Interaction, member: discord.Member, page: int = 1
) -> None:
    """Show a member's recent moderation cases (paginated)."""
    await _list_cases(interaction, member, page)


@app_commands.command(
    name="moderation-history",
    description="Show a member's moderation history (same as /cases).",
)
@app_commands.guild_only()
@app_commands.describe(member=_MEMBER_DESCRIPTION, page=_PAGE_DESCRIPTION)
async def moderation_history_command(
    interaction: discord.Interaction, member: discord.Member, page: int = 1
) -> None:
    """Alias of ``/cases`` — same implementation, same permission gate."""
    await _list_cases(interaction, member, page)
