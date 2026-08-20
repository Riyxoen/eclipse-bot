"""Configuration loading and validation (fail fast, report all problems)."""

from __future__ import annotations

import logging
from pathlib import Path

from bot.configuration.automod import load_automod_settings
from bot.configuration.env import TOKEN_ENV_VAR, getenv, load_env_file
from bot.configuration.settings import Settings
from bot.core.errors import ConfigError

#: Sentinel used by ``.env.example``; treated as "not configured".
_PLACEHOLDER_TOKEN = "your_discord_bot_token_here"

#: Environment variable names for optional settings.
ENV_LOG_LEVEL = "RIYXOEN_LOG_LEVEL"
ENV_LOG_FILE = "RIYXOEN_LOG_FILE"
ENV_SHUTDOWN_TIMEOUT = "RIYXOEN_SHUTDOWN_TIMEOUT_SECONDS"
ENV_ENABLE_MESSAGE_CONTENT = "RIYXOEN_ENABLE_MESSAGE_CONTENT_INTENT"
ENV_MODERATOR_ROLE_IDS = "RIYXOEN_MODERATOR_ROLE_IDS"
ENV_LOG_CHANNEL_ID = "RIYXOEN_LOG_CHANNEL_ID"
ENV_NOTIFY_USERS = "RIYXOEN_NOTIFY_USERS"
ENV_MAX_PURGE_AMOUNT = "RIYXOEN_MAX_PURGE_AMOUNT"
ENV_DATABASE_PATH = "RIYXOEN_DATABASE_PATH"
ENV_COMMAND_PREFIX = "RIYXOEN_COMMAND_PREFIX"

#: Upper bound accepted for RIYXOEN_MAX_PURGE_AMOUNT (sanity ceiling; the
#: per-command maximum is whatever the operator configures below this).
MAX_PURGE_AMOUNT_CEILING = 1000


def _parse_snowflake_list(name: str, raw: str | None, errors: list[str]) -> tuple[int, ...]:
    """Parse a comma-separated list of snowflake IDs (roles), or ``()``."""
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


def _parse_optional_snowflake(name: str, raw: str | None, errors: list[str]) -> int | None:
    """Parse an optional single snowflake ID (empty value means unset)."""
    if raw is None or not raw.strip():
        return None
    token = raw.strip()
    if not token.isdigit():
        errors.append(f"{name} must be an ID; got {token!r}.")
        return None
    value = int(token)
    if value <= 0:
        errors.append(f"{name} must be a positive ID; got {token!r}.")
        return None
    return value


def _parse_positive_int(name: str, raw: str | None, default: int, errors: list[str]) -> int:
    """Parse a positive integer setting, recording an error when invalid."""
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        errors.append(f"{name} must be a whole number; got {raw!r}.")
        return default
    if value < 1:
        errors.append(f"{name} must be at least 1; got {raw!r}.")
        return default
    return value


_LOG_LEVELS: dict[str, int] = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _parse_bool(name: str, raw: str | None, default: bool, errors: list[str]) -> bool:
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


def load_settings(env_file: Path | None = None) -> Settings:
    """Load and validate settings from the environment.

    Raises :class:`bot.core.errors.ConfigError` with *all* discovered problems
    when the configuration is invalid. Never returns a partial ``Settings``.
    """
    load_env_file(env_file)
    errors: list[str] = []

    token = getenv(TOKEN_ENV_VAR)
    if not token:
        errors.append(f"{TOKEN_ENV_VAR} is not configured.")
    elif token == _PLACEHOLDER_TOKEN:
        errors.append(f"{TOKEN_ENV_VAR} is still set to the placeholder value.")

    raw_level = getenv(ENV_LOG_LEVEL)
    log_level = logging.INFO
    if raw_level is not None:
        log_level = _LOG_LEVELS.get(raw_level.strip().upper())
        if log_level is None:
            errors.append(
                f"{ENV_LOG_LEVEL} must be one of {', '.join(_LOG_LEVELS)}; got {raw_level!r}."
            )

    raw_log_file = getenv(ENV_LOG_FILE)
    log_file = Path(raw_log_file).expanduser() if raw_log_file else None

    raw_timeout = getenv(ENV_SHUTDOWN_TIMEOUT)
    shutdown_timeout = 10.0
    if raw_timeout is not None:
        try:
            shutdown_timeout = float(raw_timeout)
            if shutdown_timeout <= 0:
                errors.append(
                    f"{ENV_SHUTDOWN_TIMEOUT} must be greater than 0; got {raw_timeout!r}."
                )
        except ValueError:
            errors.append(f"{ENV_SHUTDOWN_TIMEOUT} must be a number; got {raw_timeout!r}.")

    enable_message_content = _parse_bool(
        ENV_ENABLE_MESSAGE_CONTENT, getenv(ENV_ENABLE_MESSAGE_CONTENT), True, errors
    )

    raw_prefix = getenv(ENV_COMMAND_PREFIX)
    command_prefix = "."
    if raw_prefix is not None and raw_prefix.strip():
        command_prefix = raw_prefix.strip()
        if len(command_prefix) > 4 or command_prefix.isspace():
            errors.append(
                f"{ENV_COMMAND_PREFIX} must be 1-4 non-space characters; got {raw_prefix!r}."
            )

    moderator_role_ids = _parse_snowflake_list(
        ENV_MODERATOR_ROLE_IDS, getenv(ENV_MODERATOR_ROLE_IDS), errors
    )
    log_channel_id = _parse_optional_snowflake(
        ENV_LOG_CHANNEL_ID, getenv(ENV_LOG_CHANNEL_ID), errors
    )
    notify_users = _parse_bool(ENV_NOTIFY_USERS, getenv(ENV_NOTIFY_USERS), True, errors)

    max_purge_amount = _parse_positive_int(
        ENV_MAX_PURGE_AMOUNT, getenv(ENV_MAX_PURGE_AMOUNT), 100, errors
    )
    if max_purge_amount > MAX_PURGE_AMOUNT_CEILING:
        errors.append(
            f"{ENV_MAX_PURGE_AMOUNT} can't exceed {MAX_PURGE_AMOUNT_CEILING}; got "
            f"{max_purge_amount!r}."
        )

    raw_database_path = getenv(ENV_DATABASE_PATH)
    database_path = (
        Path(raw_database_path).expanduser() if raw_database_path else Path("data/cases.db")
    )

    # Phase 4: automated moderation. Parse after the core settings so the
    # token error is reported even when the automod env is also broken.
    automod = load_automod_settings(errors)

    if errors:
        raise ConfigError("configuration is invalid:\n- " + "\n- ".join(errors))

    return Settings(
        token=token or "",
        log_level=log_level,
        log_file=log_file,
        shutdown_timeout_seconds=shutdown_timeout,
        enable_message_content_intent=enable_message_content,
        command_prefix=command_prefix,
        moderator_role_ids=moderator_role_ids,
        log_channel_id=log_channel_id,
        notify_users=notify_users,
        max_purge_amount=max_purge_amount,
        database_path=database_path,
        automod=automod,
    )
