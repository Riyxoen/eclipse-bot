"""Per-guild configuration repository: the only place that touches config persistence.

:class:`GuildConfigRepository` is the abstraction the configuration service
depends on. :class:`SQLiteGuildConfigRepository` is the local implementation
(standard library ``sqlite3``, same database file as cases); it uses the same
versioned migration runner as the case repository. :class:`MemoryGuildConfigRepository`
is an in-memory fallback used when the database cannot be opened.

Rules enforced here mirror the case repository:

- **Parameterized SQL only** — no query is ever built with string
  interpolation of user input.
- **Guild isolation** — the primary key *is* the guild ID; there is no way to
  read or write one guild's configuration through another guild's ID.
- **No secrets** — the ``settings_json`` column holds validated configuration
  (IDs, thresholds, domain names, terms) only, never tokens or credentials.
"""

from __future__ import annotations

import logging
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path

from bot.configuration.guild import GuildConfig
from bot.database.migrations import migrate

logger = logging.getLogger("riyxoen.database")


class GuildConfigRepository(ABC):
    """Storage abstraction for per-guild configuration."""

    @abstractmethod
    def get(self, guild_id: int) -> GuildConfig | None:
        """Return the config for ``guild_id``, or ``None`` when unset."""

    @abstractmethod
    def upsert(
        self,
        config: GuildConfig,
        *,
        updated_by: int | None,
        updated_at: datetime,
    ) -> None:
        """Insert or replace the config for ``config.guild_id``."""


class SQLiteGuildConfigRepository(GuildConfigRepository):
    """SQLite-backed per-guild configuration repository."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.row_factory = sqlite3.Row
        migrate(self._connection)

    def get(self, guild_id: int) -> GuildConfig | None:
        row = self._connection.execute(
            "SELECT settings_json FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row is None:
            return None
        return GuildConfig.from_json(guild_id, row["settings_json"])

    def upsert(
        self,
        config: GuildConfig,
        *,
        updated_by: int | None,
        updated_at: datetime,
    ) -> None:
        self._connection.execute(
            "INSERT INTO guild_config (guild_id, settings_json, updated_at, updated_by)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(guild_id) DO UPDATE SET settings_json = excluded.settings_json,"
            " updated_at = excluded.updated_at, updated_by = excluded.updated_by",
            (
                config.guild_id,
                config.to_json(),
                updated_at.astimezone(UTC).isoformat(),
                updated_by,
            ),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()


class MemoryGuildConfigRepository(GuildConfigRepository):
    """In-memory fallback repository (config does not survive a restart)."""

    def __init__(self) -> None:
        self._rows: dict[int, GuildConfig] = {}

    def get(self, guild_id: int) -> GuildConfig | None:
        return self._rows.get(guild_id)

    def upsert(
        self,
        config: GuildConfig,
        *,
        updated_by: int | None,
        updated_at: datetime,
    ) -> None:
        self._rows[config.guild_id] = config

    def close(self) -> None:
        self._rows.clear()


def build_guild_config_repository(path: Path | None) -> GuildConfigRepository:
    """Return the SQLite repository for ``path`` (or memory when ``path`` is ``None``).

    Raises :class:`sqlite3.Error` / :class:`OSError` when the database cannot
    be opened — callers that want graceful degradation catch these and fall
    back to :class:`MemoryGuildConfigRepository`.
    """
    if path is None:
        return MemoryGuildConfigRepository()
    return SQLiteGuildConfigRepository(path)
