"""Consistent Discord embed formatting for Eclipse moderation messages.

Provides color-coded embeds for success, error, warning, info, AFK,
and jail notifications. All embeds are short, readable, and never
expose sensitive information.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import discord

from bot.moderation.validation import humanize_duration

# Color palette
COLOR_SUCCESS = 0x2ECC71   # green
COLOR_ERROR = 0xE74C3C     # red
COLOR_WARNING = 0xF39C12   # orange
COLOR_INFO = 0x3498DB      # blue
COLOR_AFK = 0x95A5A6       # gray
COLOR_JAIL = 0x8B0000      # dark red
COLOR_CUSTOM_ROLE = 0x5865F2  # blurple


def _footer_text() -> str:
    return "Eclipse"


def success_embed(title: str, description: str = "", **kwargs: Any) -> discord.Embed:
    """Green success embed for completed moderation actions."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_SUCCESS,
        timestamp=kwargs.get("timestamp") or datetime.now(UTC),
    )
    embed.set_footer(text=_footer_text())
    for name, value in kwargs.get("fields", []):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def error_embed(title: str, description: str = "", **kwargs: Any) -> discord.Embed:
    """Red error embed for permission/config/disabled-feature errors."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_ERROR,
        timestamp=kwargs.get("timestamp") or datetime.now(UTC),
    )
    embed.set_footer(text=_footer_text())
    for name, value in kwargs.get("fields", []):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def warning_embed(title: str, description: str = "", **kwargs: Any) -> discord.Embed:
    """Orange warning embed for non-critical issues."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_WARNING,
        timestamp=kwargs.get("timestamp") or datetime.now(UTC),
    )
    embed.set_footer(text=_footer_text())
    for name, value in kwargs.get("fields", []):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def info_embed(title: str, description: str = "", **kwargs: Any) -> discord.Embed:
    """Blue info embed for general information."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_INFO,
        timestamp=kwargs.get("timestamp") or datetime.now(UTC),
    )
    embed.set_footer(text=_footer_text())
    for name, value in kwargs.get("fields", []):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def afk_embed(title: str, description: str = "", **kwargs: Any) -> discord.Embed:
    """Gray AFK notification embed."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_AFK,
        timestamp=kwargs.get("timestamp") or datetime.now(UTC),
    )
    embed.set_footer(text=_footer_text())
    for name, value in kwargs.get("fields", []):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def jail_embed(title: str, description: str = "", **kwargs: Any) -> discord.Embed:
    """Dark red jail notification embed."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_JAIL,
        timestamp=kwargs.get("timestamp") or datetime.now(UTC),
    )
    embed.set_footer(text=_footer_text())
    for name, value in kwargs.get("fields", []):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def custom_role_embed(title: str, description: str = "", **kwargs: Any) -> discord.Embed:
    """Blurple custom-role embed."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=COLOR_CUSTOM_ROLE,
        timestamp=kwargs.get("timestamp") or datetime.now(UTC),
    )
    embed.set_footer(text=_footer_text())
    for name, value in kwargs.get("fields", []):
        embed.add_field(name=name, value=value, inline=False)
    return embed


def moderation_action_embed(
    *,
    case_id: int,
    action: str,
    target: str,
    moderator: str,
    reason: str,
    status: str = "Success",
    duration: str | None = None,
    extra_fields: list[tuple[str, str]] | None = None,
) -> discord.Embed:
    """Standard moderation action embed with case information."""
    color = COLOR_SUCCESS if status.lower() == "success" else COLOR_ERROR
    embed = discord.Embed(
        title=f"Case #{case_id} — {action.title()}",
        color=color,
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name="Target", value=target, inline=True)
    embed.add_field(name="Moderator", value=moderator, inline=True)
    embed.add_field(name="Reason", value=reason or "—", inline=False)
    if duration:
        # Humanize the duration if it's a raw seconds string
        try:
            secs = int(duration.rstrip('s'))
            duration = humanize_duration(secs)
        except (ValueError, TypeError):
            pass
        embed.add_field(name="Duration", value=duration, inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    if extra_fields:
        for name, value in extra_fields:
            embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=_footer_text())
    return embed
