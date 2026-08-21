"""General top-level commands: ``/ping`` and ``/help``.

These are registered directly on the command tree (not inside a group), so
they appear as ``/ping`` and ``/help``. The text they return is static and
contains no internal implementation details.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

logger = logging.getLogger("riyxoen.general")

HELP_TEXT = (
    "**Eclipse commands**\n"
    "- `/ping` — Check the bot's latency\n"
    "- `/help` — Show this help\n"
    "- `/health ping` — Health check\n"
    "- `/warn <member> [reason]` — Warn a member\n"
    "- `/timeout <member> <duration> [reason]` — Time out a member "
    "(duration like `30m`, `2h`, `3d`, max 28 days)\n"
    "- `/kick <member> [reason]` — Kick a member (confirm)\n"
    "- `/ban <member> [reason]` — Ban a member (confirm)\n"
    "- `/unban <user> [reason]` — Unban a user (pick from suggestions)\n"
    "- `/purge <amount> [reason]` — Bulk-delete messages in this channel\n"
    "- `/slowmode <duration> [channel] [reason]` — Set channel slowmode\n"
    "- `/lock [channel] [reason]` — Lock a channel (confirm)\n"
    "- `/unlock [channel] [reason]` — Unlock a channel\n"
    "- `/case <case_id>` · `/cases <member>` · `/moderation-history <member>` — Case history\n"
    "- `/config …` — Server configuration (administrators)"
)


def build_ping_response(latency_ms: float) -> str:
    """Format the ping response; kept pure for unit testing."""
    return f"Pong! Gateway latency is {latency_ms:.0f} ms."


@app_commands.command(name="ping", description="Check the bot's gateway latency.")
async def ping(interaction: discord.Interaction) -> None:
    """Reply with the current gateway latency."""
    latency_ms = interaction.client.latency * 1000
    logger.debug("ping from %s (id=%s)", interaction.user, interaction.user.id)
    await interaction.response.send_message(build_ping_response(latency_ms), ephemeral=True)


@app_commands.command(name="help", description="Show available commands.")
async def help_command(interaction: discord.Interaction) -> None:
    """Reply with the static command overview."""
    logger.debug("help requested by %s (id=%s)", interaction.user, interaction.user.id)
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)
