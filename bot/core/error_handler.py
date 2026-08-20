"""Centralized, sanitized handling of application-command errors."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

logger = logging.getLogger("riyxoen.errors")

#: User-facing messages for known error categories. Shown verbatim to users;
#: they must never contain exception internals or secrets.
_MESSAGES: dict[type[app_commands.AppCommandError], str] = {
    app_commands.CommandOnCooldown: "That command is on cooldown — try again shortly.",
    app_commands.MissingPermissions: "You don't have permission to run this command.",
    app_commands.BotMissingPermissions: "The bot is missing a permission needed for this command.",
    app_commands.NoPrivateMessage: "This command can only be used in a server.",
    app_commands.TransformerError: "That user or member couldn't be found.",
    app_commands.CheckFailure: "You're not allowed to use this command here.",
}


def classify_error(error: app_commands.AppCommandError) -> str | None:
    """Return a sanitized user-facing message for a known error, else ``None``.

    ``None`` means the error should either be silently ignored
    (``CommandNotFound``) or treated as unexpected and logged in full locally.
    """
    if isinstance(error, app_commands.CommandNotFound):
        return None
    if isinstance(error, app_commands.CommandOnCooldown):
        retry_after = getattr(error, "retry_after", None)
        if retry_after:
            return f"That command is on cooldown — try again in {retry_after:.0f} seconds."
        return _MESSAGES[app_commands.CommandOnCooldown]
    for error_type, message in _MESSAGES.items():
        if isinstance(error, error_type):
            return message
    return None


async def handle_application_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    """Handle an app-command error without leaking internals to users."""
    if isinstance(error, app_commands.CommandNotFound):
        logger.debug("unknown command invoked; ignored")
        return

    message = classify_error(error)
    if message is not None:
        logger.info("command error (known): %s", error)
        await _respond(interaction, message)
        return

    command_name = getattr(interaction.command, "name", "<unknown>")
    logger.exception("unhandled error in command %r", command_name, exc_info=error)
    await _respond(interaction, "Something went wrong while running that command.")


async def _respond(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)
