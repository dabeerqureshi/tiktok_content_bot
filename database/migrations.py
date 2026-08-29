"""Idempotent schema migrations for the folder-uploader bot.

``migrate()`` creates every table with ``IF NOT EXISTS`` and records the applied
version in ``schema_version``. For this single-writer app, running all DDL up
front is simpler and safer than an incremental migration framework.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database

SCHEMA_VERSION = 1

DDL_STATEMENTS: list[str] = [
    # --- schema bookkeeping --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version   INTEGER NOT NULL
    )
    """,
    # --- local videos waiting to be posted -----------------------------
    # content_hash is UNIQUE so a file can never be ingested (or uploaded) twice,
    # even if it is renamed or copied into the folder again.
    """
    CREATE TABLE IF NOT EXISTS videos (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        file_name     TEXT NOT NULL,
        file_path     TEXT NOT NULL,
        content_hash  TEXT NOT NULL UNIQUE,
        size_bytes    INTEGER NOT NULL DEFAULT 0,
        title         TEXT,
        status        TEXT NOT NULL DEFAULT 'PENDING',
        attempts      INTEGER NOT NULL DEFAULT 0,
        publish_id    TEXT,
        last_error    TEXT,
        discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
        uploaded_at   TEXT,
        next_retry_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_videos_status
        ON videos(status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_videos_uploaded
        ON videos(uploaded_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_videos_next_retry
        ON videos(next_retry_at)
    """,
    # --- key/value state (completion-email de-dup, etc.) ---------------
    """
    CREATE TABLE IF NOT EXISTS state (
        key         TEXT PRIMARY KEY,
        value       TEXT,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # --- structured event/error log ------------------------------------
    """
    CREATE TABLE IF NOT EXISTS system_events (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        level      TEXT NOT NULL,
        component  TEXT,
        message    TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
]


def _current_version(conn) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return int(row["v"]) if row and row["v"] else 0


def migrate(db: "Database") -> int:
    """Bring the database up to :data:`SCHEMA_VERSION`. Returns the version."""
    with db.transaction() as conn:
        for ddl in DDL_STATEMENTS:
            conn.execute(ddl)
        current = _current_version(conn)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            _run_migration(conn, version)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    return SCHEMA_VERSION


def _run_migration(conn, version: int) -> None:
    """Per-version hooks (none yet; placeholder for future schema changes)."""
    if version == 1:
        # v1 is the base schema defined by DDL_STATEMENTS above.
        return
