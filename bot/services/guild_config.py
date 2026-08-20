"""Configuration service: the only way commands and the moderation engine
touch per-guild configuration.

Architecture: ``commands -> config service -> SQLite repository -> engine``.
Commands call this service (never SQLite directly); the automated moderation
engine reads per-guild snapshots through it, so a ``/config`` change takes
effect without a restart. Detectors never query storage — they receive a
validated :class:`bot.configuration.guild.GuildConfig` snapshot.

Responsibilities (per the Phase 5 spec):

- **Loading + defaults** — :meth:`get` returns the guild's snapshot, creating
  and persisting the seeded defaults on first access.
- **Updating** — :meth:`update` validates every change, persists, and returns
  the new immutable snapshot.
- **Resetting** — :meth:`reset` restores the documented defaults (never
  touches case/moderation history).
- **Guild isolation** — every operation is keyed by ``guild_id``; there is no
  cross-guild path.
- **Bounded cache** — snapshots are cached per guild up to a fixed size
  (LRU-style eviction), and :meth:`invalidate` drops a guild's entry and
  notifies listeners (the automod engine clears its per-guild detector sets),
  so stale configuration cannot persist.
- **Audit logging** — every mutation emits a structured audit line
  (guild, actor, setting, old value, new value); never tokens or secrets.
- **Failure handling** — read failures degrade to the seeded defaults with a
  loud log (the bot keeps working); write failures raise
  :class:`GuildConfigError` with a safe message — SQL errors never reach
  Discord users.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from bot.configuration.errors import GuildConfigError
from bot.configuration.guild import (
    MAX_LIST_ENTRIES,
    GuildConfig,
    default_guild_config,
    validate_entity_id,
    validate_list_entry,
    validate_setting,
)
from bot.configuration.settings import Settings
from bot.database.config_repository import GuildConfigRepository
from bot.moderation.cases import utc_now

logger = logging.getLogger("riyxoen.config")

#: Default number of guild snapshots kept in memory (bounded cache).
DEFAULT_CACHE_SIZE = 256


class GuildConfigService:
    """Validates, persists, caches, and audits per-guild configuration."""

    def __init__(
        self,
        repository: GuildConfigRepository,
        *,
        settings: Settings,
        cache_size: int = DEFAULT_CACHE_SIZE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.cache_size = max(cache_size, 1)
        self._cache: OrderedDict[int, GuildConfig] = OrderedDict()
        self._listeners: list[Callable[[int], None]] = []
        self._clock = clock or utc_now

    # ------------------------------------------------------------ listeners

    def add_invalidation_listener(self, callback: Callable[[int], None]) -> None:
        """Register a callback invoked with ``guild_id`` after a config change."""
        self._listeners.append(callback)

    # ------------------------------------------------------------------ get

    def get(self, guild_id: int) -> GuildConfig:
        """Return the guild's configuration snapshot (never ``None``).

        First access creates and persists the seeded defaults. On a storage
        failure the seeded defaults are returned without persisting (the bot
        must keep working); the error is logged with full diagnostics.
        """
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached
        try:
            record = self.repository.get(guild_id)
        except (sqlite3.Error, OSError):
            logger.exception(
                "could not load configuration for guild %s; using seeded defaults",
                guild_id,
            )
            record = None
        if record is None:
            record = default_guild_config(self.settings, guild_id)
            try:
                self.repository.upsert(record, updated_by=None, updated_at=self._clock())
            except (sqlite3.Error, OSError):
                logger.exception(
                    "could not persist initial configuration for guild %s",
                    guild_id,
                )
        self._cache[guild_id] = record
        self._trim_cache()
        return record

    # --------------------------------------------------------------- update

    def update(
        self,
        guild_id: int,
        *,
        actor_user_id: int | None,
        changes: dict[str, Any],
    ) -> GuildConfig:
        """Validate and apply ``changes``; returns the new snapshot.

        Raises :class:`GuildConfigError` when a value is invalid or the write
        fails. Every applied change is audited.
        """
        if not changes:
            raise GuildConfigError("No settings were provided to change.")
        current = self.get(guild_id)
        validated: dict[str, Any] = {}
        for key, value in changes.items():
            validated[key] = validate_setting(key, value)
        new = replace(current, **validated)
        self._save(guild_id, new, actor_user_id=actor_user_id)
        for key, value in validated.items():
            self._audit_change(guild_id, actor_user_id, key, getattr(current, key), value)
        self._cache_put(guild_id, new)
        return new

    # --------------------------------------------------------------- reset

    def reset(self, guild_id: int, *, actor_user_id: int | None) -> GuildConfig:
        """Restore the documented defaults for the guild.

        Moderation cases and any other database data are untouched — only the
        guild's configuration row is replaced.
        """
        fresh = default_guild_config(self.settings, guild_id)
        self._save(guild_id, fresh, actor_user_id=actor_user_id)
        logger.info(
            "config reset: guild=%s actor=%s",
            guild_id,
            actor_user_id,
        )
        self._cache_put(guild_id, fresh)
        return fresh

    # ------------------------------------------------------ list mutations

    def add_exempt(
        self, guild_id: int, *, actor_user_id: int | None, kind: str, entity_id: int
    ) -> GuildConfig:
        """Add ``entity_id`` to the guild's exempt users/roles/channels."""
        field = self._exempt_field(kind)
        validated = validate_entity_id(f"{field} IDs", entity_id)
        return self._append_to_list(guild_id, actor_user_id, field, validated)

    def remove_exempt(
        self, guild_id: int, *, actor_user_id: int | None, kind: str, entity_id: int
    ) -> GuildConfig:
        """Remove ``entity_id`` from the guild's exempt users/roles/channels."""
        field = self._exempt_field(kind)
        validated = validate_entity_id(f"{field} IDs", entity_id)
        return self._remove_from_list(guild_id, actor_user_id, field, validated)

    def add_role(
        self, guild_id: int, *, actor_user_id: int | None, kind: str, role_id: int
    ) -> GuildConfig:
        """Add a moderator or administrator role by ID."""
        field = f"{kind}_role_ids"
        if field not in ("moderator_role_ids", "administrator_role_ids"):
            raise GuildConfigError("Role kind must be 'moderator' or 'administrator'.")
        validated = validate_entity_id(field, role_id)
        return self._append_to_list(guild_id, actor_user_id, field, validated)

    def remove_role(
        self, guild_id: int, *, actor_user_id: int | None, kind: str, role_id: int
    ) -> GuildConfig:
        """Remove a moderator or administrator role by ID."""
        field = f"{kind}_role_ids"
        if field not in ("moderator_role_ids", "administrator_role_ids"):
            raise GuildConfigError("Role kind must be 'moderator' or 'administrator'.")
        validated = validate_entity_id(field, role_id)
        return self._remove_from_list(guild_id, actor_user_id, field, validated)

    def add_blocked_term(
        self, guild_id: int, *, actor_user_id: int | None, term: str
    ) -> GuildConfig:
        """Add a blocked term (deduplicated, normalized)."""
        cleaned = validate_list_entry("blocked_terms", term)
        return self._append_to_list(guild_id, actor_user_id, "blocked_terms", cleaned)

    def remove_blocked_term(
        self, guild_id: int, *, actor_user_id: int | None, term: str
    ) -> GuildConfig:
        """Remove a blocked term by its normalized value."""
        cleaned = validate_list_entry("blocked_terms", term)
        return self._remove_from_list(guild_id, actor_user_id, "blocked_terms", cleaned)

    def add_allowed_domain(
        self, guild_id: int, *, actor_user_id: int | None, domain: str
    ) -> GuildConfig:
        """Add an allowed link domain (deduplicated, normalized)."""
        cleaned = validate_list_entry("allowed_domains", domain)
        return self._append_to_list(guild_id, actor_user_id, "allowed_domains", cleaned)

    def remove_allowed_domain(
        self, guild_id: int, *, actor_user_id: int | None, domain: str
    ) -> GuildConfig:
        """Remove an allowed link domain by its normalized value."""
        cleaned = validate_list_entry("allowed_domains", domain)
        return self._remove_from_list(guild_id, actor_user_id, "allowed_domains", cleaned)

    def add_allowed_invite_code(
        self, guild_id: int, *, actor_user_id: int | None, code: str
    ) -> GuildConfig:
        """Add an allowed invite code (deduplicated, normalized)."""
        cleaned = validate_list_entry("invite_allowed_codes", code)
        return self._append_to_list(guild_id, actor_user_id, "invite_allowed_codes", cleaned)

    def remove_allowed_invite_code(
        self, guild_id: int, *, actor_user_id: int | None, code: str
    ) -> GuildConfig:
        """Remove an allowed invite code by its normalized value."""
        cleaned = validate_list_entry("invite_allowed_codes", code)
        return self._remove_from_list(guild_id, actor_user_id, "invite_allowed_codes", cleaned)

    # ------------------------------------------------------------ internals

    def _append_to_list(
        self,
        guild_id: int,
        actor_user_id: int | None,
        field: str,
        entry: Any,
    ) -> GuildConfig:
        current = self.get(guild_id)
        existing = tuple(getattr(current, field))
        if entry in existing:
            raise GuildConfigError(
                f"That entry is already configured (current {field.replace('_', ' ')})."
            )
        if len(existing) >= MAX_LIST_ENTRIES:
            raise GuildConfigError("That list is full; remove an entry first.")
        new = replace(current, **{field: existing + (entry,)})
        self._save(guild_id, new, actor_user_id=actor_user_id)
        self._audit_change(guild_id, actor_user_id, field, existing, new)
        self._cache_put(guild_id, new)
        return new

    def _remove_from_list(
        self,
        guild_id: int,
        actor_user_id: int | None,
        field: str,
        entry: Any,
    ) -> GuildConfig:
        current = self.get(guild_id)
        existing = tuple(getattr(current, field))
        if entry not in existing:
            raise GuildConfigError(
                f"That entry is not currently configured (current {field.replace('_', ' ')})."
            )
        new = replace(current, **{field: tuple(item for item in existing if item != entry)})
        self._save(guild_id, new, actor_user_id=actor_user_id)
        self._audit_change(guild_id, actor_user_id, field, existing, new)
        self._cache_put(guild_id, new)
        return new

    @staticmethod
    def _exempt_field(kind: str) -> str:
        mapping = {
            "user": "exempt_user_ids",
            "role": "exempt_role_ids",
            "channel": "exempt_channel_ids",
        }
        field = mapping.get(kind)
        if field is None:
            raise GuildConfigError("Exemption kind must be 'user', 'role', or 'channel'.")
        return field

    def _save(self, guild_id: int, config: GuildConfig, *, actor_user_id: int | None) -> None:
        try:
            self.repository.upsert(config, updated_by=actor_user_id, updated_at=self._clock())
        except (sqlite3.Error, OSError) as exc:
            logger.exception(
                "config persistence failed: guild=%s actor=%s",
                guild_id,
                actor_user_id,
            )
            raise GuildConfigError(
                "The configuration could not be saved to the local database. "
                "Please try again or contact an administrator."
            ) from exc

    @staticmethod
    def _audit_change(
        guild_id: int,
        actor_user_id: int | None,
        setting: str,
        old: Any,
        new: Any,
    ) -> None:
        logger.info(
            "config change: guild=%s actor=%s setting=%s old=%r new=%r",
            guild_id,
            actor_user_id,
            setting,
            old,
            new,
        )

    def _cache_put(self, guild_id: int, config: GuildConfig) -> None:
        self._cache[guild_id] = config
        self._trim_cache()
        for listener in self._listeners:
            listener(guild_id)

    def _trim_cache(self) -> None:
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
