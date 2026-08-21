"""Per-guild configuration model (Phase 5).

Each guild gets an independent :class:`GuildConfig` — a validated, immutable
snapshot of every setting the moderation engine and services consume. The
defaults are seeded from the global environment ``Settings`` (so existing
deployments keep their behavior), and administrators override them per guild
through the ``/config`` commands; overrides live in the local SQLite database
via the configuration service and repository.

Guards:

- **No secrets** — this model holds configuration only (IDs, thresholds,
  domain names, terms); never tokens, credentials, or environment variables.
- **Bounded lists** — exempt users/roles/channels, blocked terms, and allowed
  domains are capped so an administrator cannot create unbounded config.
- **Validated values** — every scalar update passes through
  :func:`validate_setting` with sane lower/upper bounds (see the limits in
  this module) to prevent resource abuse.
- **UTC timestamps** — ``updated_at`` metadata is ISO-8601 UTC text.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields, replace
from typing import Any

from bot.configuration.automod import (
    _DETECTOR_ACTIONS,
    ACTION_ALLOW,
    parse_escalation_spec,
)
from bot.configuration.errors import GuildConfigError
from bot.configuration.loader import MAX_PURGE_AMOUNT_CEILING
from bot.configuration.settings import Settings
from bot.moderation.validation import MAX_TIMEOUT_SECONDS

# ---------------------------------------------------------------- limits
#: Upper bound for detector thresholds (spam/duplicate/mention counts).
MAX_THRESHOLD = 1000
#: Upper bound for time windows and the enforcement cooldown (seconds).
MAX_WINDOW_SECONDS = 86_400  # 1 day
#: Upper bound for the default timeout duration (Discord caps at 28 days).
MAX_TIMEOUT_DURATION_SECONDS = MAX_TIMEOUT_SECONDS
#: Upper bound for the number of entries in a config list (terms, domains,
#: exemptions, roles) — defense against unbounded configuration.
MAX_LIST_ENTRIES = 100
#: Upper bound for a single list entry (a term or domain string).
MAX_ENTRY_LENGTH = 100
#: Upper bound for escalation warning counts.
MAX_ESCALATION_COUNT = 1000


# ---------------------------------------------------------------- model


@dataclass(frozen=True, slots=True)
class GuildConfig:
    """Immutable per-guild configuration snapshot.

    Field defaults mirror the global environment defaults so a bare
    :class:`GuildConfig` is a valid (if unseeded) configuration; production
    always builds guild configs from :func:`default_guild_config`, which
    seeds them from the operator's environment settings.
    """

    guild_id: int
    #: Master switch for automated moderation in this guild.
    automod_enabled: bool = False
    #: Role IDs that may run every moderation command (bypass permissions).
    moderator_role_ids: tuple[int, ...] = ()
    #: Role IDs that may change server configuration (bypass Administrator).
    administrator_role_ids: tuple[int, ...] = ()
    #: Channel that receives moderation/log events (``None`` = unset).
    log_channel_id: int | None = None
    #: Whether moderation events are posted to the log channel.
    mod_log_enabled: bool = False
    #: Whether punished users receive a best-effort DM notification.
    notify_users: bool = True
    #: Maximum messages a single purge may delete in this guild.
    max_purge_amount: int = 100
    #: Spam detection: N messages within N seconds -> action.
    spam_threshold: int = 5
    spam_window_seconds: int = 5
    spam_action: str = "delete"
    #: Duplicate detection: N identical (normalized) messages within N seconds.
    duplicate_threshold: int = 4
    duplicate_window_seconds: int = 30
    duplicate_action: str = "delete"
    #: Mention detection: independent thresholds per dimension (0 disables).
    mention_user_threshold: int = 10
    mention_role_threshold: int = 6
    mention_total_threshold: int = 15
    mention_everyone_threshold: int = 0
    mention_action: str = "delete"
    #: Link detection action (``allow`` disables enforcement).
    link_action: str = ACTION_ALLOW
    allowed_domains: tuple[str, ...] = ()
    #: Word filter: blocked terms (never hardcoded in source) + mode.
    blocked_terms: tuple[str, ...] = ()
    blocked_terms_substring: bool = False
    word_filter_action: str = ACTION_ALLOW
    #: Default timeout duration used when a detector's action is "timeout".
    timeout_duration_seconds: int = 3600
    #: Exempt users/roles/channels (bypass automated moderation).
    exempt_user_ids: tuple[int, ...] = ()
    exempt_role_ids: tuple[int, ...] = ()
    exempt_channel_ids: tuple[int, ...] = ()
    #: Seconds between enforcements for the same (guild, user); 0 disables.
    enforcement_cooldown_seconds: int = 60
    #: Warnings expire after this many seconds; 0 means never expire.
    warning_window_seconds: int = 7 * 24 * 60 * 60
    #: Ordered ``(warning_count, timeout_seconds)`` escalation pairs.
    escalation: tuple[tuple[int, int], ...] = ()
    #: Custom-role system (Phase 6): whether it is enabled for this guild.
    custom_roles_enabled: bool = False
    #: The bot-managed custom role's ID (``None`` until ``.el enable``).
    custom_role_id: int | None = None
    #: Invite filtering (Phase 8): enforcement action and allowed invite codes.
    invite_action: str = ACTION_ALLOW
    invite_allowed_codes: tuple[str, ...] = ()
    #: Raid protection (Phase 8): join burst threshold, window, and action
    #: (``alert`` posts a log-channel notice; ``timeout`` is opt-in).
    raid_join_threshold: int = 10
    raid_window_seconds: int = 10
    raid_action: str = "alert"
    #: Per-guild override for the text-command prefix (Phase 10). Seeded from
    #: the global environment prefix; administrators change it via
    #: ``/config prefix`` and the prefix dispatcher honors it per guild.
    command_prefix: str = "·"
    #: Jail system: the role ID applied to jailed users (None = not configured).
    jail_role_id: int | None = None
    #: Jail system: the channel ID jailed users can access (None = unrestricted).
    jail_channel_id: int | None = None

    # ---------------------------------------------------------- persistence

    def to_json(self) -> str:
        """Serialize to JSON for the ``settings_json`` column."""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, guild_id: int, raw: str) -> GuildConfig:
        """Deserialize from JSON produced by :meth:`to_json`.

        Raises :class:`GuildConfigError` on malformed data; a corrupted row
        is surfaced as a safe error, never a crash.
        """
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise GuildConfigError("The stored server configuration is corrupted.") from exc
        known = {field.name for field in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise GuildConfigError("The stored server configuration is corrupted.")
        data["guild_id"] = guild_id
        tuple_fields = {field.name for field in fields(cls) if field.type.startswith("tuple")}
        for key, value in data.items():
            if key == "escalation" and isinstance(value, (list, tuple)):
                # Escalation is a tuple of (count, seconds) pairs; JSON makes
                # the pairs lists, so normalize both levels.
                data[key] = tuple(tuple(pair) for pair in value)
            elif key in tuple_fields and isinstance(value, list):
                data[key] = tuple(value)
        return cls(**data)

    # ------------------------------------------------------------- helpers

    def replace(self, **changes: Any) -> GuildConfig:
        """Return a new snapshot with validated ``changes`` applied."""
        return replace(self, **changes)


def default_guild_config(settings: Settings, guild_id: int) -> GuildConfig:
    """Build the default per-guild config, seeded from the environment.

    The global environment values act as the bootstrap defaults for every
    guild; administrators then override per guild via ``/config``. Keeping
    this the single source of defaults means an operator's environment setup
    still applies until a server explicitly changes it.
    """
    automod = settings.automod
    return GuildConfig(
        guild_id=guild_id,
        automod_enabled=automod.enabled,
        moderator_role_ids=settings.moderator_role_ids,
        log_channel_id=settings.log_channel_id,
        mod_log_enabled=settings.log_channel_id is not None,
        notify_users=settings.notify_users,
        max_purge_amount=settings.max_purge_amount,
        spam_threshold=automod.spam_threshold,
        spam_window_seconds=automod.spam_window_seconds,
        spam_action=automod.spam_action,
        duplicate_threshold=automod.duplicate_threshold,
        duplicate_window_seconds=automod.duplicate_window_seconds,
        duplicate_action=automod.duplicate_action,
        mention_user_threshold=automod.mention_user_threshold,
        mention_role_threshold=automod.mention_role_threshold,
        mention_total_threshold=automod.mention_total_threshold,
        mention_everyone_threshold=automod.mention_everyone_threshold,
        mention_action=automod.mention_action,
        link_action=automod.link_action,
        allowed_domains=automod.allowed_domains,
        blocked_terms=automod.blocked_terms,
        blocked_terms_substring=automod.blocked_terms_substring,
        word_filter_action=automod.word_filter_action,
        timeout_duration_seconds=automod.timeout_duration_seconds,
        exempt_user_ids=automod.exempt_user_ids,
        exempt_role_ids=automod.exempt_role_ids,
        exempt_channel_ids=automod.exempt_channel_ids,
        enforcement_cooldown_seconds=automod.enforcement_cooldown_seconds,
        warning_window_seconds=automod.warning_window_seconds,
        escalation=automod.escalation,
        custom_roles_enabled=False,
        custom_role_id=None,
        invite_action=automod.invite_action,
        invite_allowed_codes=automod.invite_allowed_codes,
        raid_join_threshold=automod.raid_join_threshold,
        raid_window_seconds=automod.raid_window_seconds,
        raid_action=automod.raid_action,
        command_prefix=settings.command_prefix,
        jail_role_id=None,
        jail_channel_id=None,
    )


# ------------------------------------------------------------- validation


def _require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise GuildConfigError(f"{name} must be true or false.")
    return value


def _require_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GuildConfigError(f"{name} must be a whole number.")
    if value < minimum or value > maximum:
        raise GuildConfigError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _require_action(name: str, value: Any, allowed: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise GuildConfigError(f"{name} must be one of: {', '.join(allowed)}.")
    return value


def _require_optional_snowflake(name: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuildConfigError(f"{name} must be a positive ID.")
    return value


def _require_snowflake(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GuildConfigError(f"{name} must be a positive ID.")
    return value


def _require_id_tuple(name: str, value: Any, *, allow_empty: bool = True) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)):
        raise GuildConfigError(f"{name} must be a list of IDs.")
    result: list[int] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int) or entry <= 0:
            raise GuildConfigError(f"{name} must be a list of positive IDs.")
        if entry in result:
            raise GuildConfigError(f"{name} cannot contain duplicate IDs.")
        result.append(entry)
    if not allow_empty and not result:
        raise GuildConfigError(f"{name} cannot be empty.")
    if len(result) > MAX_LIST_ENTRIES:
        raise GuildConfigError(f"{name} can't have more than {MAX_LIST_ENTRIES} entries.")
    return tuple(result)


def _require_prefix(name: str, value: Any) -> str:
    """Validate a text-command prefix: 1-3 non-space characters without
    characters that conflict with Discord syntax (mentions, channels, slash
    commands). Returns the cleaned prefix."""
    if not isinstance(value, str) or not value.strip():
        raise GuildConfigError(f"{name} must be 1-3 non-space characters.")
    cleaned = value.strip()
    if len(cleaned) > 3:
        raise GuildConfigError(f"{name} can't be longer than 3 characters.")
    if any(ch.isspace() for ch in cleaned):
        raise GuildConfigError(f"{name} can't contain spaces.")
    if any(ch in cleaned for ch in ("@", "#", "/", "<", ">", ":")):
        raise GuildConfigError(f"{name} can't contain @, #, /, <, >, or : characters.")
    return cleaned


def _require_str_list(name: str, value: Any, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise GuildConfigError(f"{name} must be a list of values.")
    result: list[str] = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise GuildConfigError(f"{name} entries must be non-empty text.")
        cleaned = entry.strip()
        if len(cleaned) > MAX_ENTRY_LENGTH:
            raise GuildConfigError(
                f"{name} entries can't be longer than {MAX_ENTRY_LENGTH} characters."
            )
        lowered = cleaned.lower()
        if lowered in result:
            raise GuildConfigError(f"{name} cannot contain duplicate entries.")
        result.append(lowered)
    if not allow_empty and not result:
        raise GuildConfigError(f"{name} cannot be empty.")
    if len(result) > MAX_LIST_ENTRIES:
        raise GuildConfigError(f"{name} can't have more than {MAX_LIST_ENTRIES} entries.")
    return tuple(result)


def _require_escalation(name: str, value: Any) -> tuple[tuple[int, int], ...]:
    if value == () or value == []:
        return ()
    if isinstance(value, str):
        try:
            return parse_escalation_spec(value)
        except ValueError as exc:
            raise GuildConfigError(f"{name}: {exc}") from exc
    if not isinstance(value, (list, tuple)):
        raise GuildConfigError(f"{name} must be a list of count:duration pairs.")
    result: list[tuple[int, int]] = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise GuildConfigError(f"{name} entries must look like (count, seconds).")
        count, duration = pair
        result.append(
            (
                _require_int(
                    f"{name} warning counts", count, minimum=1, maximum=MAX_ESCALATION_COUNT
                ),
                _require_int(
                    f"{name} timeout durations",
                    duration,
                    minimum=1,
                    maximum=MAX_TIMEOUT_DURATION_SECONDS,
                ),
            )
        )
    result.sort(key=lambda pair: pair[0])
    for previous, current in zip(result, result[1:], strict=False):
        if current[0] == previous[0]:
            raise GuildConfigError(f"{name} warning counts must be distinct.")
    return tuple(result)


#: Validators for every directly-updatable scalar setting. List settings
#: (terms, domains, exemptions, roles) are mutated through dedicated service
#: methods that reuse the same validation helpers.
_VALIDATORS: dict[str, Any] = {
    "automod_enabled": lambda v: _require_bool("automod_enabled", v),
    "log_channel_id": lambda v: _require_optional_snowflake("log_channel_id", v),
    "mod_log_enabled": lambda v: _require_bool("mod_log_enabled", v),
    "notify_users": lambda v: _require_bool("notify_users", v),
    "max_purge_amount": lambda v: _require_int(
        "max_purge_amount", v, minimum=1, maximum=MAX_PURGE_AMOUNT_CEILING
    ),
    "spam_threshold": lambda v: _require_int("spam_threshold", v, minimum=2, maximum=MAX_THRESHOLD),
    "spam_window_seconds": lambda v: _require_int(
        "spam_window_seconds", v, minimum=1, maximum=MAX_WINDOW_SECONDS
    ),
    "spam_action": lambda v: _require_action("spam_action", v, _DETECTOR_ACTIONS["spam"]),
    "duplicate_threshold": lambda v: _require_int(
        "duplicate_threshold", v, minimum=2, maximum=MAX_THRESHOLD
    ),
    "duplicate_window_seconds": lambda v: _require_int(
        "duplicate_window_seconds", v, minimum=1, maximum=MAX_WINDOW_SECONDS
    ),
    "duplicate_action": lambda v: _require_action(
        "duplicate_action", v, _DETECTOR_ACTIONS["duplicate"]
    ),
    "mention_user_threshold": lambda v: _require_int(
        "mention_user_threshold", v, minimum=0, maximum=MAX_THRESHOLD
    ),
    "mention_role_threshold": lambda v: _require_int(
        "mention_role_threshold", v, minimum=0, maximum=MAX_THRESHOLD
    ),
    "mention_total_threshold": lambda v: _require_int(
        "mention_total_threshold", v, minimum=0, maximum=MAX_THRESHOLD
    ),
    "mention_everyone_threshold": lambda v: _require_int(
        "mention_everyone_threshold", v, minimum=0, maximum=MAX_THRESHOLD
    ),
    "mention_action": lambda v: _require_action("mention_action", v, _DETECTOR_ACTIONS["mentions"]),
    "link_action": lambda v: _require_action("link_action", v, _DETECTOR_ACTIONS["links"]),
    "blocked_terms_substring": lambda v: _require_bool("blocked_terms_substring", v),
    "word_filter_action": lambda v: _require_action(
        "word_filter_action", v, _DETECTOR_ACTIONS["word_filter"]
    ),
    "timeout_duration_seconds": lambda v: _require_int(
        "timeout_duration_seconds", v, minimum=1, maximum=MAX_TIMEOUT_DURATION_SECONDS
    ),
    "enforcement_cooldown_seconds": lambda v: _require_int(
        "enforcement_cooldown_seconds", v, minimum=0, maximum=MAX_WINDOW_SECONDS
    ),
    "warning_window_seconds": lambda v: _require_int(
        "warning_window_seconds", v, minimum=0, maximum=MAX_WINDOW_SECONDS
    ),
    "escalation": lambda v: _require_escalation("escalation", v),
    "custom_roles_enabled": lambda v: _require_bool("custom_roles_enabled", v),
    "custom_role_id": lambda v: _require_optional_snowflake("custom_role_id", v),
    "invite_action": lambda v: _require_action("invite_action", v, _DETECTOR_ACTIONS["invites"]),
    "invite_allowed_codes": lambda v: _require_str_list("invite_allowed_codes", v),
    "raid_join_threshold": lambda v: _require_int(
        "raid_join_threshold", v, minimum=2, maximum=MAX_THRESHOLD
    ),
    "raid_window_seconds": lambda v: _require_int(
        "raid_window_seconds", v, minimum=1, maximum=MAX_WINDOW_SECONDS
    ),
    "raid_action": lambda v: _require_action("raid_action", v, _DETECTOR_ACTIONS["raid"]),
    "command_prefix": lambda v: _require_prefix("command_prefix", v),
    "jail_role_id": lambda v: _require_optional_snowflake("jail_role_id", v),
    "jail_channel_id": lambda v: _require_optional_snowflake("jail_channel_id", v),
}


def validate_setting(name: str, value: Any) -> Any:
    """Validate a single setting value; returns it unchanged or raises
    :class:`GuildConfigError` with a safe message.

    Unknown setting names are a programming error.
    """
    validator = _VALIDATORS.get(name)
    if validator is None:
        raise GuildConfigError(f"Unknown configuration setting: {name}.")
    return validator(value)


def validate_list_entry(name: str, value: Any) -> str:
    """Validate a single list entry (a blocked term or allowed domain)."""
    if not isinstance(value, str) or not value.strip():
        raise GuildConfigError(f"{name} must be non-empty text.")
    cleaned = value.strip()
    if len(cleaned) > MAX_ENTRY_LENGTH:
        raise GuildConfigError(
            f"{name} entries can't be longer than {MAX_ENTRY_LENGTH} characters."
        )
    return cleaned.lower()


def validate_entity_id(name: str, value: Any) -> int:
    """Validate a single snowflake entity ID (exemption or role)."""
    return _require_snowflake(name, value)


def validate_id_list(name: str, value: Any, *, allow_empty: bool = True) -> tuple[int, ...]:
    """Validate a full ID list (used by the repository round-trip tests)."""
    return _require_id_tuple(name, value, allow_empty=allow_empty)
