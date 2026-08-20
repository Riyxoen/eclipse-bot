"""Automated-moderation configuration (Phase 4).

All automated-moderation settings are local, environment-driven values with
sane, conservative defaults. Nothing here is a secret. The whole engine is
opt-in via ``RIYXOEN_AUTOMOD_ENABLED``; when disabled, no message content is
read and no detector runs.

False-positive safety by default:

- The engine is **off** unless an operator enables it.
- Thresholds are conservative and must be crossed (never a single message).
- ``0`` disables an individual mention dimension.
- Link enforcement defaults to ``allow`` (blocking URLs is opt-in).
- Blocked terms are **empty** by default — offensive words are never
  hardcoded into source; operators define them.
- Warning escalation is off until thresholds are configured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from bot.moderation.validation import MAX_TIMEOUT_SECONDS

#: Canonical automated-moderation actions.
ACTION_ALLOW = "allow"
ACTION_DELETE = "delete"
ACTION_WARN = "warn"
ACTION_TIMEOUT = "timeout"
#: Non-punitive alert: post a notice to the guild's configured log channel
#: (used by conservative raid protection; never punishes on weak evidence).
ACTION_ALERT = "alert"

#: Actions valid for each detector (link additionally supports ``allow``;
#: invites support ``allow``/``delete``/``warn``/``timeout``; raid supports
#: the conservative ``alert`` plus ``timeout`` — never ban/kick).
_DETECTOR_ACTIONS: dict[str, tuple[str, ...]] = {
    "spam": (ACTION_DELETE, ACTION_WARN, ACTION_TIMEOUT),
    "duplicate": (ACTION_DELETE, ACTION_WARN, ACTION_TIMEOUT),
    "mentions": (ACTION_DELETE, ACTION_WARN, ACTION_TIMEOUT),
    "links": (ACTION_ALLOW, ACTION_DELETE, ACTION_WARN, ACTION_TIMEOUT),
    "word_filter": (ACTION_DELETE, ACTION_WARN, ACTION_TIMEOUT),
    "invites": (ACTION_ALLOW, ACTION_DELETE, ACTION_WARN, ACTION_TIMEOUT),
    "raid": (ACTION_ALERT, ACTION_TIMEOUT),
}

#: Environment variable names.
ENV_AUTOMOD_ENABLED = "RIYXOEN_AUTOMOD_ENABLED"
ENV_SPAM_THRESHOLD = "RIYXOEN_AUTOMOD_SPAM_THRESHOLD"
ENV_SPAM_WINDOW = "RIYXOEN_AUTOMOD_SPAM_WINDOW_SECONDS"
ENV_SPAM_ACTION = "RIYXOEN_AUTOMOD_SPAM_ACTION"
ENV_DUPLICATE_THRESHOLD = "RIYXOEN_AUTOMOD_DUPLICATE_THRESHOLD"
ENV_DUPLICATE_WINDOW = "RIYXOEN_AUTOMOD_DUPLICATE_WINDOW_SECONDS"
ENV_DUPLICATE_ACTION = "RIYXOEN_AUTOMOD_DUPLICATE_ACTION"
ENV_MENTION_USER = "RIYXOEN_AUTOMOD_MENTION_USER_THRESHOLD"
ENV_MENTION_ROLE = "RIYXOEN_AUTOMOD_MENTION_ROLE_THRESHOLD"
ENV_MENTION_TOTAL = "RIYXOEN_AUTOMOD_MENTION_TOTAL_THRESHOLD"
ENV_MENTION_EVERYONE = "RIYXOEN_AUTOMOD_MENTION_EVERYONE_THRESHOLD"
ENV_MENTION_ACTION = "RIYXOEN_AUTOMOD_MENTION_ACTION"
#: Default timeout applied when a detector's action is "timeout".
ENV_TIMEOUT_DURATION = "RIYXOEN_AUTOMOD_TIMEOUT_DURATION_SECONDS"
ENV_LINK_ACTION = "RIYXOEN_AUTOMOD_LINK_ACTION"
ENV_ALLOWED_DOMAINS = "RIYXOEN_AUTOMOD_ALLOWED_DOMAINS"
ENV_BLOCKED_TERMS = "RIYXOEN_AUTOMOD_BLOCKED_TERMS"
ENV_BLOCKED_TERMS_SUBSTRING = "RIYXOEN_AUTOMOD_BLOCKED_TERMS_SUBSTRING"
ENV_WORD_FILTER_ACTION = "RIYXOEN_AUTOMOD_WORD_FILTER_ACTION"
ENV_EXEMPT_USERS = "RIYXOEN_AUTOMOD_EXEMPT_USER_IDS"
ENV_EXEMPT_ROLES = "RIYXOEN_AUTOMOD_EXEMPT_ROLE_IDS"
ENV_EXEMPT_CHANNELS = "RIYXOEN_AUTOMOD_EXEMPT_CHANNEL_IDS"
ENV_COOLDOWN = "RIYXOEN_AUTOMOD_ENFORCEMENT_COOLDOWN_SECONDS"
ENV_WARNING_WINDOW = "RIYXOEN_AUTOMOD_WARNING_WINDOW_SECONDS"
ENV_ESCALATION = "RIYXOEN_AUTOMOD_ESCALATION"
#: Phase 8 detectors: invites (discord.gg links) and raid (join bursts).
ENV_INVITE_ACTION = "RIYXOEN_AUTOMOD_INVITE_ACTION"
ENV_INVITE_ALLOWED_CODES = "RIYXOEN_AUTOMOD_INVITE_ALLOWED_CODES"
ENV_RAID_JOIN_THRESHOLD = "RIYXOEN_AUTOMOD_RAID_JOIN_THRESHOLD"
ENV_RAID_WINDOW = "RIYXOEN_AUTOMOD_RAID_WINDOW_SECONDS"
ENV_RAID_ACTION = "RIYXOEN_AUTOMOD_RAID_ACTION"


@dataclass(frozen=True, slots=True)
class AutomodSettings:
    """Validated automated-moderation configuration.

    Threshold semantics: ``0`` disables the affected dimension (mention
    dimensions and enforcement cooldown). ``warning_window_seconds`` of ``0``
    means warnings never expire. ``escalation`` is an ordered tuple of
    ``(warning_count, timeout_seconds)`` pairs (see :data:`_parse_escalation`).
    """

    enabled: bool = False
    spam_threshold: int = 5
    spam_window_seconds: int = 5
    spam_action: str = ACTION_DELETE
    duplicate_threshold: int = 4
    duplicate_window_seconds: int = 30
    duplicate_action: str = ACTION_DELETE
    mention_user_threshold: int = 10
    mention_role_threshold: int = 6
    mention_total_threshold: int = 15
    mention_everyone_threshold: int = 0  # 0 = disabled (Discord gates @everyone)
    mention_action: str = ACTION_DELETE
    #: Timeout duration (seconds) used when a detector's action is "timeout".
    timeout_duration_seconds: int = 3600
    link_action: str = ACTION_ALLOW
    allowed_domains: tuple[str, ...] = ()
    blocked_terms: tuple[str, ...] = ()
    blocked_terms_substring: bool = False
    word_filter_action: str = ACTION_DELETE
    exempt_user_ids: tuple[int, ...] = ()
    exempt_role_ids: tuple[int, ...] = ()
    exempt_channel_ids: tuple[int, ...] = ()
    enforcement_cooldown_seconds: int = 60
    warning_window_seconds: int = 7 * 24 * 60 * 60  # 7 days
    escalation: tuple[tuple[int, int], ...] = ()
    #: Invite filtering: enforcement action and allowed invite codes.
    invite_action: str = ACTION_ALLOW
    invite_allowed_codes: tuple[str, ...] = ()
    #: Raid protection: join burst threshold, window, and action.
    raid_join_threshold: int = 10
    raid_window_seconds: int = 10
    raid_action: str = ACTION_ALERT


# --------------------------------------------------------------- parsing


def _env(name: str) -> str | None:
    return os.getenv(name)


def _parse_int(name: str, raw: str | None, default: int, errors: list[str], *, minimum: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        errors.append(f"{name} must be a whole number; got {raw!r}.")
        return default
    if value < minimum:
        errors.append(f"{name} must be at least {minimum}; got {value!r}.")
        return default
    return value


def _parse_bool(name: str, raw: str | None, default: bool, errors: list[str]) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    errors.append(f"{name} must be a boolean (1/0, true/false, yes/no, on/off); got {raw!r}")
    return default


def _parse_action(
    name: str, raw: str | None, default: str, allowed: tuple[str, ...], errors: list[str]
) -> str:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value not in allowed:
        errors.append(f"{name} must be one of {', '.join(allowed)}; got {value!r}.")
        return default
    return value


def _parse_snowflake_list(name: str, raw: str | None, errors: list[str]) -> tuple[int, ...]:
    if raw is None or not raw.strip():
        return ()
    ids: list[int] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if not token.isdigit() or int(token) <= 0:
            errors.append(f"{name} must be a comma-separated list of IDs; got {token!r}.")
            continue
        ids.append(int(token))
    return tuple(ids)


def _parse_str_list(name: str, raw: str | None, errors: list[str]) -> tuple[str, ...]:
    if raw is None or not raw.strip():
        return ()
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        errors.append(f"{name} must be a comma-separated list of values.")
    return tuple(values)


def parse_escalation_spec(raw: str) -> tuple[tuple[int, int], ...]:
    """Parse ``3:3600,5:43200`` into ``((3, 3600), (5, 43200))``.

    Each pair means: once the member's active warning count (including the
    new warning) reaches ``warning_count``, apply a timeout of
    ``timeout_seconds``. Pairs must have strictly increasing warning counts
    and durations within Discord's 28-day timeout limit. An empty/blank
    string returns ``()`` (escalation disabled).

    Raises :class:`ValueError` with a human-readable message when the spec
    is malformed. Shared by the environment parser and the Phase 5
    ``/config moderation escalation`` command so the logic is not duplicated.
    """
    if raw is None or not raw.strip():
        return ()
    parsed: list[tuple[int, int]] = []
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        pieces = token.split(":")
        if len(pieces) != 2:
            raise ValueError(f"entries must look like 3:3600; got {token!r}.")
        count_raw, duration_raw = pieces
        if not count_raw.isdigit() or int(count_raw) < 1:
            raise ValueError(f"warning counts must be positive integers; got {count_raw!r}.")
        if not duration_raw.isdigit() or int(duration_raw) < 1:
            raise ValueError(f"timeout durations must be positive integers; got {duration_raw!r}.")
        duration = int(duration_raw)
        if duration > MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout durations can't exceed 28 days; got {duration!r}.")
        parsed.append((int(count_raw), duration))
    parsed.sort(key=lambda pair: pair[0])
    for previous, current in zip(parsed, parsed[1:], strict=False):
        if current[0] == previous[0]:
            raise ValueError(f"warning counts must be distinct; got {current[0]!r} twice.")
    return tuple(parsed)


def _parse_escalation(name: str, raw: str | None, errors: list[str]) -> tuple[tuple[int, int], ...]:
    """Environment wrapper over :func:`parse_escalation_spec`."""
    try:
        return parse_escalation_spec(raw or "")
    except ValueError as exc:
        errors.append(f"{name} {exc}")
        return ()


def load_automod_settings(errors: list[str]) -> AutomodSettings:
    """Load and validate automated-moderation settings from the environment.

    Invalid values are appended to ``errors`` (never raising directly) so the
    caller can report every configuration problem at once.
    """
    enabled = _parse_bool(ENV_AUTOMOD_ENABLED, _env(ENV_AUTOMOD_ENABLED), False, errors)

    spam_threshold = _parse_int(ENV_SPAM_THRESHOLD, _env(ENV_SPAM_THRESHOLD), 5, errors, minimum=2)
    spam_window = _parse_int(ENV_SPAM_WINDOW, _env(ENV_SPAM_WINDOW), 5, errors, minimum=1)
    spam_action = _parse_action(
        ENV_SPAM_ACTION, _env(ENV_SPAM_ACTION), ACTION_DELETE, _DETECTOR_ACTIONS["spam"], errors
    )

    duplicate_threshold = _parse_int(
        ENV_DUPLICATE_THRESHOLD, _env(ENV_DUPLICATE_THRESHOLD), 4, errors, minimum=2
    )
    duplicate_window = _parse_int(
        ENV_DUPLICATE_WINDOW, _env(ENV_DUPLICATE_WINDOW), 30, errors, minimum=1
    )
    duplicate_action = _parse_action(
        ENV_DUPLICATE_ACTION,
        _env(ENV_DUPLICATE_ACTION),
        ACTION_DELETE,
        _DETECTOR_ACTIONS["duplicate"],
        errors,
    )

    mention_user = _parse_int(ENV_MENTION_USER, _env(ENV_MENTION_USER), 10, errors, minimum=0)
    mention_role = _parse_int(ENV_MENTION_ROLE, _env(ENV_MENTION_ROLE), 6, errors, minimum=0)
    mention_total = _parse_int(ENV_MENTION_TOTAL, _env(ENV_MENTION_TOTAL), 15, errors, minimum=0)
    mention_everyone = _parse_int(
        ENV_MENTION_EVERYONE, _env(ENV_MENTION_EVERYONE), 0, errors, minimum=0
    )
    mention_action = _parse_action(
        ENV_MENTION_ACTION,
        _env(ENV_MENTION_ACTION),
        ACTION_DELETE,
        _DETECTOR_ACTIONS["mentions"],
        errors,
    )
    timeout_duration = _parse_int(
        ENV_TIMEOUT_DURATION, _env(ENV_TIMEOUT_DURATION), 3600, errors, minimum=1
    )
    if timeout_duration > MAX_TIMEOUT_SECONDS:
        errors.append(f"{ENV_TIMEOUT_DURATION} can't exceed 28 days; got {timeout_duration!r}.")

    link_action = _parse_action(
        ENV_LINK_ACTION, _env(ENV_LINK_ACTION), ACTION_ALLOW, _DETECTOR_ACTIONS["links"], errors
    )
    allowed_domains = _parse_str_list(ENV_ALLOWED_DOMAINS, _env(ENV_ALLOWED_DOMAINS), errors)

    blocked_terms = _parse_str_list(ENV_BLOCKED_TERMS, _env(ENV_BLOCKED_TERMS), errors)
    blocked_substring = _parse_bool(
        ENV_BLOCKED_TERMS_SUBSTRING, _env(ENV_BLOCKED_TERMS_SUBSTRING), False, errors
    )
    word_filter_action = _parse_action(
        ENV_WORD_FILTER_ACTION,
        _env(ENV_WORD_FILTER_ACTION),
        ACTION_DELETE,
        _DETECTOR_ACTIONS["word_filter"],
        errors,
    )

    exempt_users = _parse_snowflake_list(ENV_EXEMPT_USERS, _env(ENV_EXEMPT_USERS), errors)
    exempt_roles = _parse_snowflake_list(ENV_EXEMPT_ROLES, _env(ENV_EXEMPT_ROLES), errors)
    exempt_channels = _parse_snowflake_list(ENV_EXEMPT_CHANNELS, _env(ENV_EXEMPT_CHANNELS), errors)

    cooldown = _parse_int(ENV_COOLDOWN, _env(ENV_COOLDOWN), 60, errors, minimum=0)
    warning_window = _parse_int(
        ENV_WARNING_WINDOW, _env(ENV_WARNING_WINDOW), 7 * 24 * 60 * 60, errors, minimum=0
    )
    escalation = _parse_escalation(ENV_ESCALATION, _env(ENV_ESCALATION), errors)

    invite_action = _parse_action(
        ENV_INVITE_ACTION,
        _env(ENV_INVITE_ACTION),
        ACTION_ALLOW,
        _DETECTOR_ACTIONS["invites"],
        errors,
    )
    invite_allowed_codes = _parse_str_list(
        ENV_INVITE_ALLOWED_CODES, _env(ENV_INVITE_ALLOWED_CODES), errors
    )
    raid_join_threshold = _parse_int(
        ENV_RAID_JOIN_THRESHOLD, _env(ENV_RAID_JOIN_THRESHOLD), 10, errors, minimum=2
    )
    raid_window = _parse_int(ENV_RAID_WINDOW, _env(ENV_RAID_WINDOW), 10, errors, minimum=1)
    raid_action = _parse_action(
        ENV_RAID_ACTION, _env(ENV_RAID_ACTION), ACTION_ALERT, _DETECTOR_ACTIONS["raid"], errors
    )

    return AutomodSettings(
        enabled=enabled,
        spam_threshold=spam_threshold,
        spam_window_seconds=spam_window,
        spam_action=spam_action,
        duplicate_threshold=duplicate_threshold,
        duplicate_window_seconds=duplicate_window,
        duplicate_action=duplicate_action,
        mention_user_threshold=mention_user,
        mention_role_threshold=mention_role,
        mention_total_threshold=mention_total,
        mention_everyone_threshold=mention_everyone,
        mention_action=mention_action,
        timeout_duration_seconds=timeout_duration,
        link_action=link_action,
        allowed_domains=allowed_domains,
        blocked_terms=blocked_terms,
        blocked_terms_substring=blocked_substring,
        word_filter_action=word_filter_action,
        exempt_user_ids=exempt_users,
        exempt_role_ids=exempt_roles,
        exempt_channel_ids=exempt_channels,
        enforcement_cooldown_seconds=cooldown,
        warning_window_seconds=warning_window,
        escalation=escalation,
        invite_action=invite_action,
        invite_allowed_codes=invite_allowed_codes,
        raid_join_threshold=raid_join_threshold,
        raid_window_seconds=raid_window,
        raid_action=raid_action,
    )
