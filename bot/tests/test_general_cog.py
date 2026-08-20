"""Tests for the general cog: ``/ping`` and ``/help``."""

from __future__ import annotations

from bot.cogs.general import HELP_TEXT, build_ping_response, help_command, ping
from discord import app_commands


def test_build_ping_response_formats_latency() -> None:
    assert build_ping_response(42.8) == "Pong! Gateway latency is 43 ms."


def test_ping_is_top_level_command() -> None:
    assert isinstance(ping, app_commands.Command)
    assert ping.name == "ping"
    assert ping.parent is None


def test_help_is_top_level_command() -> None:
    assert isinstance(help_command, app_commands.Command)
    assert help_command.name == "help"
    assert help_command.parent is None


def test_help_text_lists_all_commands() -> None:
    for command_name in (
        "/ping",
        "/help",
        "/warn",
        "/timeout",
        "/kick",
        "/ban",
        "/unban",
        "/purge",
        "/slowmode",
        "/lock",
        "/unlock",
        "/case",
        "/cases",
        "/config",
    ):
        assert command_name in HELP_TEXT
