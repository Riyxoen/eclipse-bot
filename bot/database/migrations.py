"""Schema migrations for the local SQLite case database.

Migrations are versioned with SQLite's ``PRAGMA user_version`` and applied in
order, once, when the repository opens the database. Every migration is a
single, idempotent script; adding a new schema version means appending a
``(version, sql)`` tuple to :data:`MIGRATIONS`. The bot never runs partial
schema state: each script executes in its own implicit transaction.
"""

from __future__ import annotations

import sqlite3

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS cases (
    case_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id           INTEGER NOT NULL,
    target_user_id     INTEGER NOT NULL,
    moderator_user_id  INTEGER NOT NULL,
    action             TEXT    NOT NULL,
    reason             TEXT    NOT NULL,
    duration_seconds   INTEGER,
    expires_at         TEXT,
    status             TEXT    NOT NULL CHECK (status IN ('success', 'failed')),
    error              TEXT,
    created_at         TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_guild ON cases (guild_id);
CREATE INDEX IF NOT EXISTS idx_cases_guild_target ON cases (guild_id, target_user_id);
"""

# Phase 4: automated-moderation cases identify themselves as automated and
# record which detector triggered the action. Both columns are additive, so
# existing Phase 3 databases migrate in place without data loss.
_SCHEMA_V2 = """
ALTER TABLE cases ADD COLUMN automated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cases ADD COLUMN detector TEXT;
"""

# Phase 5: per-guild configuration. Each guild owns exactly one row holding
# its validated settings as JSON; ``updated_at``/``updated_by`` record the
# last change for audit purposes. No secrets are ever stored here. Additive
# and independent of the ``cases`` table, so existing databases migrate in
# place without touching case data.
_SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id      INTEGER PRIMARY KEY,
    settings_json TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    updated_by    INTEGER
);
"""

# Phase 6: optional action-specific metadata on a case (JSON text). Used to
# record reversible state — e.g. ``lock`` stores the channel's pre-lock
# overwrite so ``unlock`` restores it exactly. Additive; existing rows get
# ``NULL`` (no metadata) and keep working.
_SCHEMA_V4 = """
ALTER TABLE cases ADD COLUMN metadata TEXT;
"""

#: Ordered list of migrations: (schema version, SQL script).
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
)

# Phase 11: AFK state persistence. Stores the user's AFK status, original
# nickname, and optional custom message per guild. Additive; a fresh table
# does not touch existing data.
_SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS afk_state (
    guild_id       INTEGER NOT NULL,
    user_id        INTEGER NOT NULL,
    original_name  TEXT    NOT NULL,
    afk_message    TEXT    NOT NULL DEFAULT '',
    set_at         TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);
"""

#: Ordered list of migrations: (schema version, SQL script).
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
    (5, _SCHEMA_V5),
)

#: The newest schema version the code understands.
LATEST_SCHEMA_VERSION = MIGRATIONS[-1][0]


def current_schema_version(connection: sqlite3.Connection) -> int:
    """Return the schema version recorded in ``connection``."""
    (version,) = connection.execute("PRAGMA user_version").fetchone()
    return int(version)


def migrate(connection: sqlite3.Connection) -> int:
    """Apply any pending migrations to ``connection``.

    Safe to call repeatedly: migrations that are already applied are skipped.
    Returns the schema version after migration. Raises
    :class:`sqlite3.Error` on failure (the caller decides how to surface it).
    """
    version = current_schema_version(connection)
    for target, script in MIGRATIONS:
        if target > version:
            connection.executescript(script)
            # ``version`` comes from our own MIGRATIONS list (an int), never
            # from user input — this is not string interpolation of input.
            connection.execute(f"PRAGMA user_version = {target}")
            connection.commit()
            version = target
    return version
