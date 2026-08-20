"""Moderation commands (Phase 6): top-level, consistent, professional UX.

Command surface (all top-level, all guild-only):

    /warn <member> [reason]
    /timeout <member> <duration> [reason]
    /kick <member> [reason]            (confirmation)
    /ban <member> [reason]             (confirmation)
    /unban <user> [reason]             (autocomplete of banned users)
    /purge <amount> [reason]           (confirmation for large purges)
    /slowmode <duration> [channel] [reason]
    /lock [channel] [reason]           (confirmation)
    /unlock [channel] [reason]

Design rules:

- Commands stay thin: validate arguments, delegate to the moderation
  service, format the resulting case. No business logic here.
- Reasons are **optional** and default to "No reason provided" (the stored
  case always carries the final reason).
- Dangerous actions (ban, kick, large purge, lock) go through a
  confirmation prompt with buttons. The confirmation state machine lives in
  :class:`bot.services.confirmation.ConfirmationController` (expiry,
  ownership, atomic double-click protection); the ``discord.ui.View`` below
  only wires buttons to it.
- Every successful action produces exactly one case via the existing case
  system; moderation summaries are ephemeral and public log embeds go to
  the configured log channel (owned by the moderation service).
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ui import Button, View

from bot.moderation.errors import InvalidTargetError, ModerationError
from bot.moderation.response import format_case_response
from bot.moderation.validation import (
    MAX_REASON_LENGTH,
    parse_duration,
    validate_purge_amount,
    validate_slowmode_duration,
)

logger = logging.getLogger("riyxoen.moderation_cog")

#: Reason used when the moderator omits one (documented default).
DEFAULT_REASON = "No reason provided"

#: Purge amounts at or above this require a confirmation click.
LARGE_PURGE_THRESHOLD = 20

_DURATION_DESCRIPTION = "How long, e.g. 30m, 2h, 3d (max 28 days)."
_REASON_DESCRIPTION = f"Optional reason (max {MAX_REASON_LENGTH} characters)."
_CHANNEL_DESCRIPTION = "The channel (defaults to this channel)."
_UNBAN_DESCRIPTION = "The banned user (pick from the suggestions)."


def _reason(reason: str | None) -> str:
    """Normalize an optional reason to the documented default."""
    cleaned = (reason or "").strip()
    return cleaned or DEFAULT_REASON


def _parse_user_id(raw: str) -> int:
    """Parse a snowflake user ID string (from the unban autocomplete)."""
    cleaned = raw.strip()
    if not cleaned.isdigit() or int(cleaned) <= 0:
        raise InvalidTargetError("User not found.")
    return int(cleaned)


async def _deny(interaction: discord.Interaction, exc: ModerationError) -> None:
    """Show a safe denial; full detail is already in the logs."""
    logger.info("moderation denied: %s (user=%s)", exc.user_message, interaction.user.id)
    await _respond(interaction, exc.user_message)


async def _respond(interaction: discord.Interaction, content: str) -> None:
    """Reply ephemerally, whether or not the interaction was already deferred."""
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


def _record_response(record, *, target_label: str, moderator_label: str) -> str:
    return format_case_response(record, target_label=target_label, moderator_label=moderator_label)


# ------------------------------------------------------------ confirmation


class ConfirmationView(View):
    """Buttons for one pending confirmation. Delegates all state to the
    :class:`ConfirmationController`; a wrong user, an expired confirmation,
    or a double click gets a safe error and the action never runs twice."""

    def __init__(
        self,
        controller: Any,
        confirmation: Any,
        *,
        on_confirm: Any,
        timeout: int | None = None,
    ) -> None:
        super().__init__(timeout=timeout or getattr(controller, "timeout_seconds", 30))
        self._controller = controller
        self._confirmation = confirmation
        self._on_confirm = on_confirm

        confirm = Button(label="Confirm", style=discord.ButtonStyle.danger)
        confirm.callback = self._handle_confirm
        cancel = Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._handle_cancel
        self.add_item(confirm)
        self.add_item(cancel)

    def _guild_id(self, interaction: discord.Interaction) -> int | None:
        return getattr(interaction, "guild_id", None) or getattr(
            getattr(interaction, "guild", None), "id", None
        )

    async def _handle_confirm(self, interaction: discord.Interaction) -> None:
        error = self._controller.try_confirm(
            self._confirmation.key, self._guild_id(interaction), interaction.user.id
        )
        if error is not None:
            await _respond(interaction, error)
            return
        await interaction.response.defer()
        try:
            await self._on_confirm(interaction)
        except ModerationError as exc:
            await interaction.followup.send(exc.user_message, ephemeral=True)

    async def _handle_cancel(self, interaction: discord.Interaction) -> None:
        error = self._controller.try_cancel(
            self._confirmation.key, self._guild_id(interaction), interaction.user.id
        )
        if error is not None:
            await _respond(interaction, error)
            return
        await _respond(interaction, "Action cancelled.")

    async def on_timeout(self) -> None:
        """Disable the buttons once the confirmation has expired."""
        for child in self.children:
            child.disabled = True


async def _confirm_and_run(
    interaction: discord.Interaction,
    *,
    summary: str,
    action: Any,
    target_label: str,
    moderator_label: str,
) -> None:
    """Show a confirmation prompt; on Confirm, run ``action`` exactly once.

    ``action`` is an async callable returning the resulting ``CaseRecord``.
    """
    controller = interaction.client.confirmation_service
    confirmation = controller.create(interaction.guild.id, interaction.user.id, summary)

    async def _run(button_interaction: discord.Interaction) -> None:
        record = await action()
        await button_interaction.followup.send(
            _record_response(record, target_label=target_label, moderator_label=moderator_label),
            ephemeral=True,
        )

    view = ConfirmationView(controller, confirmation, on_confirm=_run)
    await interaction.response.send_message(
        f"**{summary}** — confirm to continue. "
        f"This confirmation expires in {controller.timeout_seconds} seconds and can only "
        "be used by the moderator who started it.",
        view=view,
        ephemeral=True,
    )


# ------------------------------------------------------------ /warn /timeout


@app_commands.command(name="warn", description="Warn a member.")
@app_commands.guild_only()
@app_commands.describe(member="The member to warn", reason=_REASON_DESCRIPTION)
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str | None = None,
) -> None:
    """Warn a member; records a case and (optionally) DMs them."""
    service = interaction.client.moderation_service
    try:
        record = await service.warn(interaction.guild, interaction.user, member, _reason(reason))
    except ModerationError as exc:
        await _deny(interaction, exc)
        return
    await _respond(
        interaction,
        _record_response(
            record,
            target_label=member.mention,
            moderator_label=interaction.user.mention,
        ),
    )


@app_commands.command(name="timeout", description="Time a member out.")
@app_commands.guild_only()
@app_commands.describe(
    member="The member to time out",
    duration=_DURATION_DESCRIPTION,
    reason=_REASON_DESCRIPTION,
)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    duration: str,
    reason: str | None = None,
) -> None:
    """Time a member out for a validated duration (1s..28d)."""
    service = interaction.client.moderation_service
    try:
        duration_seconds = parse_duration(duration)
        record = await service.timeout(
            interaction.guild, interaction.user, member, duration_seconds, _reason(reason)
        )
    except ModerationError as exc:
        await _deny(interaction, exc)
        return
    await _respond(
        interaction,
        _record_response(
            record,
            target_label=member.mention,
            moderator_label=interaction.user.mention,
        ),
    )


# ------------------------------------------------------------- /kick /ban


@app_commands.command(name="kick", description="Kick a member (requires confirmation).")
@app_commands.guild_only()
@app_commands.describe(member="The member to kick", reason=_REASON_DESCRIPTION)
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str | None = None,
) -> None:
    """Kick a member after a confirmation click."""
    service = interaction.client.moderation_service
    await _confirm_and_run(
        interaction,
        summary=f"Kick {member.mention}",
        target_label=member.mention,
        moderator_label=interaction.user.mention,
        action=lambda: service.kick(interaction.guild, interaction.user, member, _reason(reason)),
    )


@app_commands.command(name="ban", description="Ban a member (requires confirmation).")
@app_commands.guild_only()
@app_commands.describe(member="The member to ban", reason=_REASON_DESCRIPTION)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str | None = None,
) -> None:
    """Ban a member after a confirmation click."""
    service = interaction.client.moderation_service
    await _confirm_and_run(
        interaction,
        summary=f"Ban {member.mention}",
        target_label=member.mention,
        moderator_label=interaction.user.mention,
        action=lambda: service.ban(interaction.guild, interaction.user, member, _reason(reason)),
    )


# ------------------------------------------------------------------- /unban


async def _unban_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Suggest banned users; values carry their user ID (no manual IDs)."""
    guild = interaction.guild
    if guild is None:
        return []
    try:
        entries = await guild.bans().flatten()
    except Exception:  # noqa: BLE001 - autocomplete must never fail the command
        return []
    current_lower = current.strip().lower()
    choices: list[app_commands.Choice[str]] = []
    for entry in entries:
        user = getattr(entry, "user", None)
        if user is None:
            continue
        label = f"{getattr(user, 'name', 'user')} ({user.id})"
        if current_lower and current_lower not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label, value=str(user.id)))
        if len(choices) >= 25:
            break
    return choices


@app_commands.command(name="unban", description="Unban a user (pick from suggestions).")
@app_commands.guild_only()
@app_commands.autocomplete(user=_unban_autocomplete)
@app_commands.describe(user=_UNBAN_DESCRIPTION, reason=_REASON_DESCRIPTION)
async def unban(
    interaction: discord.Interaction,
    user: str,
    reason: str | None = None,
) -> None:
    """Unban a user by ID (autocomplete selects banned users for you)."""
    service = interaction.client.moderation_service
    try:
        user_id = _parse_user_id(user)
        record = await service.unban(interaction.guild, interaction.user, user_id, _reason(reason))
    except ModerationError as exc:
        await _deny(interaction, exc)
        return
    target_label = f"<@{user_id}>"
    await _respond(
        interaction,
        _record_response(
            record,
            target_label=target_label,
            moderator_label=interaction.user.mention,
        ),
    )


# ------------------------------------------------------------------- /purge


@app_commands.command(name="purge", description="Bulk-delete recent messages in this channel.")
@app_commands.guild_only()
@app_commands.describe(
    amount="How many recent messages to delete (capped by configuration).",
    reason=_REASON_DESCRIPTION,
)
async def purge(
    interaction: discord.Interaction,
    amount: int,
    reason: str | None = None,
) -> None:
    """Delete up to ``amount`` messages; large purges require confirmation."""
    service = interaction.client.moderation_service
    channel = interaction.channel
    if not hasattr(channel, "purge"):
        await _deny(interaction, InvalidTargetError("Purge only works in text channels."))
        return
    try:
        validated = validate_purge_amount(amount, service.max_purge_amount_for(interaction.guild))
    except ModerationError as exc:
        await _deny(interaction, exc)
        return

    async def _run_purge() -> Any:
        return await service.purge(channel, interaction.user, validated)

    if validated >= LARGE_PURGE_THRESHOLD:
        await _confirm_and_run(
            interaction,
            summary=f"Purge {validated} messages in {getattr(channel, 'mention', '<channel>')}",
            target_label=getattr(channel, "mention", str(validated)),
            moderator_label=interaction.user.mention,
            action=_run_purge,
        )
        return
    try:
        record = await _run_purge()
    except ModerationError as exc:
        await _deny(interaction, exc)
        return
    await _respond(
        interaction,
        _record_response(
            record,
            target_label=getattr(channel, "mention", str(validated)),
            moderator_label=interaction.user.mention,
        ),
    )


# --------------------------------------------------------------- /slowmode


def _parse_slowmode(text: str) -> int:
    """Parse a slowmode duration; ``0``/``0s`` clears slowmode (0..6 hours)."""
    cleaned = text.strip().lower()
    if cleaned in ("0", "0s"):
        return 0
    return validate_slowmode_duration(parse_duration(cleaned))


@app_commands.command(name="slowmode", description="Set a channel's slowmode delay.")
@app_commands.guild_only()
@app_commands.describe(
    duration="Slowmode delay, e.g. 10s, 5m, 1h (0 clears; max 6 hours).",
    channel=_CHANNEL_DESCRIPTION,
    reason=_REASON_DESCRIPTION,
)
async def slowmode(
    interaction: discord.Interaction,
    duration: str,
    channel: discord.TextChannel | None = None,
    reason: str | None = None,
) -> None:
    """Set the channel slowmode (0..6 hours; ``0`` clears it)."""
    service = interaction.client.moderation_service
    channel = channel or interaction.channel
    if not hasattr(channel, "edit"):
        await _deny(interaction, InvalidTargetError("Slowmode only works in text channels."))
        return
    try:
        seconds = _parse_slowmode(duration)
        record = await service.slowmode(
            interaction.guild, interaction.user, channel, seconds, _reason(reason)
        )
    except ModerationError as exc:
        await _deny(interaction, exc)
        return
    await _respond(
        interaction,
        _record_response(
            record,
            target_label=getattr(channel, "mention", "<channel>"),
            moderator_label=interaction.user.mention,
        ),
    )


# ----------------------------------------------------------------- lock/unlock


@app_commands.command(name="lock", description="Lock a channel (requires confirmation).")
@app_commands.guild_only()
@app_commands.describe(channel=_CHANNEL_DESCRIPTION, reason=_REASON_DESCRIPTION)
async def lock(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    reason: str | None = None,
) -> None:
    """Deny @everyone from sending messages in the channel (reversible)."""
    service = interaction.client.moderation_service
    channel = channel or interaction.channel
    if not hasattr(channel, "set_permissions"):
        await _deny(interaction, InvalidTargetError("Lock only works in text channels."))
        return
    await _confirm_and_run(
        interaction,
        summary=f"Lock {getattr(channel, 'mention', '<channel>')}",
        target_label=getattr(channel, "mention", "<channel>"),
        moderator_label=interaction.user.mention,
        action=lambda: service.lock_channel(
            interaction.guild, interaction.user, channel, _reason(reason)
        ),
    )


@app_commands.command(name="unlock", description="Unlock a channel, restoring its previous state.")
@app_commands.guild_only()
@app_commands.describe(channel=_CHANNEL_DESCRIPTION, reason=_REASON_DESCRIPTION)
async def unlock(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    reason: str | None = None,
) -> None:
    """Restore the channel's pre-lock permission state (from the lock case)."""
    service = interaction.client.moderation_service
    channel = channel or interaction.channel
    if not hasattr(channel, "set_permissions"):
        await _deny(interaction, InvalidTargetError("Unlock only works in text channels."))
        return
    try:
        record = await service.unlock_channel(
            interaction.guild, interaction.user, channel, _reason(reason)
        )
    except ModerationError as exc:
        await _deny(interaction, exc)
        return
    await _respond(
        interaction,
        _record_response(
            record,
            target_label=getattr(channel, "mention", "<channel>"),
            moderator_label=interaction.user.mention,
        ),
    )
