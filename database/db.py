"""SQLite access layer.

Uses WAL mode so readers and a writer can run concurrently. All writes go
through :meth:`Database.transaction`, which issues an explicit ``BEGIN
IMMEDIATE`` so a write lock is taken up-front and ``busy_timeout`` handles
contention cleanly.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from config import load_settings

settings = load_settings()


class Database:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else settings.db_path

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """A single-writer transaction. Commits on success, rolls back on error."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def insert_event(self, level: str, component: str, message: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO system_events (level, component, message) VALUES (?, ?, ?)",
                (level, component, message),
            )

    def reset_abandoned_jobs(self) -> list[str]:
        """Detect in-flight work left over from a crash and re-queue it.

        A video stuck in ``UPLOADING`` means the app crashed after TikTok
        init or mid-chunk-PUT. Its ``publish_id`` is preserved so the worker
        can resolve the real outcome via ``fetch_status`` instead of blindly
        re-uploading (which would double-post). Rows with no ``publish_id``
        (crashed before init) reset to ``PENDING``.
        """
        notes: list[str] = []

        def _mark(sql: str, msg: str, conn: sqlite3.Connection) -> None:
            cur = conn.execute(sql)
            if cur.rowcount:
                notes.append(f"{msg}: {cur.rowcount}")

        with self.transaction() as conn:
            _mark(
                "UPDATE videos SET status='PENDING', last_error='recovered in-flight upload' "
                "WHERE status='UPLOADING' AND publish_id IS NULL",
                "uploads re-queued (pre-init)",
                conn,
            )
        for _note in notes:
            self.insert_event("INFO", "recovery", _note)
        return notes

    def count_by_status(self, table: str, status: str) -> int:
        rows = self.query(
            f"SELECT COUNT(*) AS c FROM {table} WHERE status=?", (status,)
        )
        return int(rows[0]["c"]) if rows else 0

    def get_state(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM state WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_state(self, key: str, value: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO state (key, value, updated_at) "
                "VALUES (?, ?, datetime('now'))",
                (key, value),
            )
            conn.execute(
                "UPDATE state SET value=?, updated_at=datetime('now') WHERE key=?",
                (value, key),
            )


# Shared singleton used by services/workers: `from database.db import db`.
db = Database()
