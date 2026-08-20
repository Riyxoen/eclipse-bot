"""Health commands.

The only cog in Phase 1. ``/health ping`` proves the full slash-command
pipeline: tree registration, grouping, cooldowns, and centralized error
handling. A ``discord.app_commands.Group`` is the native "cog" equivalent for
a slash-only ``discord.Client``.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

logger = logging.getLogger("riyxoen.health")


def build_ping_response(latency_ms: float) -> str:
    """Format the ping response; kept pure for unit testing."""
    return f"Pong! Gateway latency is {latency_ms:.0f} ms."


class HealthCog(app_commands.Group):
    """Health and diagnostics commands, grouped under ``/health``."""

    def __init__(self, bot: discord.Client) -> None:
        super().__init__(name="health", description="Health and diagnostics commands.")
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's gateway latency.")
    @app_commands.checks.cooldown(1, 5)  # once per 5 seconds per user
    async def ping(self, interaction: discord.Interaction) -> None:
        """Reply with the current gateway latency."""
        latency_ms = self.bot.latency * 1000
        logger.debug("ping from %s (id=%s)", interaction.user, interaction.user.id)
        await interaction.response.send_message(build_ping_response(latency_ms), ephemeral=True)
