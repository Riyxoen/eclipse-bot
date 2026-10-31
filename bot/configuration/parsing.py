"""Shared environment-value parsers.

``bot.configuration.loader`` already imports ``bot.configuration.automod``, so
``automod`` cannot import the loader's helpers without creating a cycle. The
two rules both modules need live here instead, defined exactly once.
"""

from __future__ import annotations

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def parse_bool(name: str, raw: str | None, default: bool, errors: list[str]) -> bool:
    """Parse a boolean environment value, recording an error when invalid."""
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    errors.append(f"{name} must be a boolean (1/0, true/false, yes/no, on/off); got {raw!r}")
    return default


def parse_snowflake_list(name: str, raw: str | None, errors: list[str]) -> tuple[int, ...]:
    """Parse a comma-separated list of snowflake IDs (empty/blank means ``()``)."""
    if raw is None or not raw.strip():
        return ()
    ids: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit():
            errors.append(f"{name} must be a comma-separated list of IDs; got {token!r}.")
            continue
        value = int(token)
        if value <= 0:
            errors.append(f"{name} IDs must be positive; got {value!r}.")
            continue
        ids.append(value)
    return tuple(ids)
