"""Structured logging configuration for the bot."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from bot.logging.redaction import RedactingFormatter, SecretRedactionFilter

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    *,
    level: int = logging.INFO,
    log_file: Path | None = None,
    secrets: tuple[str, ...] = (),
) -> None:
    """Configure root logging with secret redaction and an optional file handler.

    The parent directory of ``log_file`` is created if needed. Existing root
    handlers are removed first so library defaults do not double-log. Every
    handler gets both the redaction filter and the redacting formatter.
    """
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    formatter = RedactingFormatter(secrets=secrets, fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(SecretRedactionFilter(secrets))
        root.addHandler(handler)
