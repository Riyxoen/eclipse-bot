"""Tests for the health cog."""

from __future__ import annotations

from bot.cogs.health import HealthCog, build_ping_response
from discord import app_commands


def test_build_ping_response_formats_latency() -> None:
    assert build_ping_response(123.456) == "Pong! Gateway latency is 123 ms."


def test_health_cog_exposes_ping_command_with_cooldown() -> None:
    cog = HealthCog(bot=None)  # type: ignore[arg-type]
    command = cog.ping
    assert isinstance(command, app_commands.Command)
    assert command.name == "ping"
    assert command.checks  # cooldown check is registered
