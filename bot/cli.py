"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import replace

from bot import __version__
from bot.configuration.loader import load_settings
from bot.core.bot import run
from bot.core.errors import ConfigError
from bot.core.startup import check_environment
from bot.logging.configure import configure_logging

logger = logging.getLogger("riyxoen.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riyxoen",
        description="Riyxoen Moderation Bot",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="run startup validation and exit without connecting to Discord",
    )
    parser.add_argument(
        "--log-level",
        help="override the configured log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entrypoint; returns a process exit code."""
    args = build_parser().parse_args(argv)

    if args.check:
        return check_environment()

    try:
        settings = load_settings()
    except ConfigError as exc:
        # Logging is not configured yet; print a safe message to stderr.
        print(f"riyxoen: configuration error:\n{exc}", file=sys.stderr)
        return 2

    if args.log_level:
        level = getattr(logging, args.log_level.upper(), None)
        if not isinstance(level, int):
            print(f"riyxoen: invalid --log-level {args.log_level!r}", file=sys.stderr)
            return 2
        settings = replace(settings, log_level=level)

    configure_logging(
        level=settings.log_level,
        log_file=settings.log_file,
        secrets=(settings.token,),
    )
    logger.info("bot starting (riyxoen v%s)", __version__)
    logger.info("configuration loaded")
    return asyncio.run(run(settings))
