"""Idempotent schema migrations.

``migrate()`` creates every table with ``IF NOT EXISTS`` and records the
applied version in ``schema_version``. New migrations should bump
:data:`SCHEMA_VERSION` and append statements to :data:`DDL_STATEMENTS` (or
add a dedicated migration function). For a local single-writer app, running
all DDL up front is simpler and safer than an incremental framework.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database

SCHEMA_VERSION = 2

# Versioned, idempotent schema changes applied on top of the base DDL.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE videos ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE clips ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE posts ADD COLUMN next_retry_at TEXT",
        "CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)",
        "CREATE INDEX IF NOT EXISTS idx_clips_status ON clips(status)",
        "CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status)",
        "CREATE INDEX IF NOT EXISTS idx_posts_scheduled ON posts(scheduled_at)",
    ],
}

DDL_STATEMENTS: list[str] = [
    # --- schema bookkeeping --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version     INTEGER NOT NULL
    )
    """,
    # --- approved sources ----------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS sources (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        name             TEXT NOT NULL,
        channel_url      TEXT NOT NULL,
        enabled          INTEGER NOT NULL DEFAULT 1,
        last_checked_at  TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # --- downloaded source videos ---------------------------------------
    # youtube_id is UNIQUE so a source video can never be ingested twice.
    """
    CREATE TABLE IF NOT EXISTS videos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id       INTEGER REFERENCES sources(id),
        youtube_id      TEXT NOT NULL UNIQUE,
        url             TEXT NOT NULL,
        title           TEXT,
        duration        REAL NOT NULL DEFAULT 0,
        thumbnail       TEXT,
        status          TEXT NOT NULL DEFAULT 'DISCOVERED',
        file_path       TEXT,
        transcript_path TEXT,
        last_error      TEXT,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        downloaded_at   TEXT,
        processed_at    TEXT
    )
    """,
    # --- saved transcription --------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS transcripts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id   INTEGER NOT NULL REFERENCES videos(id),
        json_path  TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # --- generated clips ------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS clips (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id    INTEGER NOT NULL REFERENCES videos(id),
        file_path   TEXT,
        start_time  REAL,
        end_time    REAL,
        duration    REAL,
        title       TEXT,
        caption     TEXT,
        hashtags    TEXT,           -- JSON array string
        score       INTEGER,
        status      TEXT NOT NULL DEFAULT 'CREATED',
        last_error  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # --- scheduled/published posts --------------------------------------
    """
    CREATE TABLE IF NOT EXISTS posts (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        clip_id           INTEGER NOT NULL REFERENCES clips(id),
        scheduled_at      TEXT,
        tiktok_publish_id TEXT,
        status            TEXT NOT NULL DEFAULT 'PENDING',
        attempts          INTEGER NOT NULL DEFAULT 0,
        last_error        TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now')),
        posted_at         TEXT
    )
    """,
    # --- generic job ledger for observability ----------------------------
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_type    TEXT NOT NULL,
        entity_id   INTEGER,
        status      TEXT NOT NULL,
        attempts    INTEGER NOT NULL DEFAULT 0,
        started_at  TEXT,
        finished_at TEXT,
        last_error  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    # --- structured event/error log -------------------------------------
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
        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            for statement in MIGRATIONS[version]:
                try:
                    conn.execute(statement)
                except Exception:  # column/index already present - idempotent
                    pass

        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
            (SCHEMA_VERSION,),
        )
    return SCHEMA_VERSION
