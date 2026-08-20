"""Secret redaction for log output.

Defense in depth: secrets are scrubbed twice. :class:`SecretRedactionFilter`
runs at the handler boundary and cleans the record's message and arguments
(including degenerate cases where message formatting fails).
:class:`RedactingFormatter` then redacts the fully formatted output, which
catches tracebacks rendered from ``exc_info`` — those are produced by the
formatter *after* filters have already run.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

#: Matches Discord bot tokens and similar JWT-style values
#: (three dot-separated base64url-ish segments).
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}")

#: Matches secret assignments such as ``Authorization: Bearer xyz`` or
#: ``token=abc``. The key is preserved and the rest of the line is treated as
#: the value (headers may contain spaces, e.g. ``Bearer <token>``).
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(token|secret|password|passwd|authorization|api[_-]?key|client[_-]?secret)\b"
    r"\s*[:=]\s*.*$"
)

_REDACTED = "[REDACTED]"


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Return ``text`` with token-shaped and configured secrets scrubbed."""
    scrubbed = _TOKEN_PATTERN.sub(_REDACTED, text)
    scrubbed = _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}={_REDACTED}", scrubbed
    )
    for secret in secrets:
        if secret:
            scrubbed = scrubbed.replace(secret, _REDACTED)
    return scrubbed


def _redact_args(args: Any, secrets: tuple[str, ...]) -> Any:
    """Recursively scrub string values inside logging arguments.

    The container type is preserved: ``logging``'s ``%``-style formatting
    unpacks *tuples* into positional arguments but treats a *list* as a
    single value, so converting tuples to lists would corrupt every
    formatted message (e.g. ``v['0.1.0']`` instead of ``v0.1.0``).
    """
    if isinstance(args, Mapping):
        return {key: _redact_args(value, secrets) for key, value in args.items()}
    if isinstance(args, tuple):
        return tuple(_redact_args(value, secrets) for value in args)
    if isinstance(args, list):
        return [_redact_args(value, secrets) for value in args]
    if isinstance(args, str):
        return redact(args, secrets)
    return args


class SecretRedactionFilter(logging.Filter):
    """Scrub secrets from a log record's message and arguments."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg), self._secrets)
        if record.args:
            record.args = _redact_args(record.args, self._secrets)
        return True


class RedactingFormatter(logging.Formatter):
    """Formatter that scrubs secrets from the fully formatted output."""

    def __init__(
        self,
        secrets: tuple[str, ...] = (),
        *,
        fmt: str | None = None,
        datefmt: str | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self._secrets)
