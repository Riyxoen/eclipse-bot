"""Domain exception hierarchy for the bot."""

from __future__ import annotations


class BotError(Exception):
    """Base class for all bot-domain errors."""


class ConfigError(BotError):
    """Raised when configuration is missing or invalid."""
