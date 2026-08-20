"""Startup validation and diagnostics."""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from bot.configuration.loader import load_settings
from bot.configuration.settings import Settings
from bot.core.errors import ConfigError
from bot.database.repository import SQLiteCaseRepository

logger = logging.getLogger("riyxoen.startup")

#: Sentinel used by ``.env.example``; treated as "not configured".
_PLACEHOLDER_TOKEN = "your_discord_bot_token_here"


def run_preflight(settings: Settings) -> list[str]:
    """Validate that the bot can start; return problems (empty list = OK).

    Performs no network I/O, so it is safe to run before connecting to
    Discord.
    """
    problems: list[str] = []

    if not settings.token:
        problems.append("DISCORD_TOKEN is not configured.")
    elif settings.token == _PLACEHOLDER_TOKEN:
        problems.append("DISCORD_TOKEN is still set to the placeholder value.")

    if settings.log_file is not None:
        log_dir: Path = settings.log_file.parent
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"cannot create log directory {log_dir}: {exc}")
        else:
            if not os.access(log_dir, os.W_OK):
                problems.append(f"log directory is not writable: {log_dir}")

    db_dir: Path = settings.database_path.parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(f"cannot create database directory {db_dir}: {exc}")
    else:
        if not os.access(db_dir, os.W_OK):
            problems.append(f"database directory is not writable: {db_dir}")

    # Verify the case database can actually be opened and initialized
    # (schema + migrations). This creates the file on first run, which is the
    # intended local-initialization behavior.
    try:
        SQLiteCaseRepository(settings.database_path).close()
    except (sqlite3.Error, OSError) as exc:
        problems.append(f"cannot open case database {settings.database_path}: {exc}")

    return problems


def check_environment() -> int:
    """Run pre-flight checks and report; exit 0 on success, 1 on problems."""
    try:
        import discord  # noqa: F401 - verifies the dependency is installed

        settings = load_settings()
    except ConfigError as exc:
        logger.error("configuration invalid: %s", exc)
        return 1
    except ImportError as exc:
        logger.error("discord.py is not installed: %s", exc)
        return 1

    problems = run_preflight(settings)
    for problem in problems:
        logger.error("pre-flight failure: %s", problem)
    if problems:
        return 1

    logger.info("pre-flight checks passed")
    return 0
