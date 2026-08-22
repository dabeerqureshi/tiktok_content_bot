"""SQLite access layer.

Uses WAL mode so readers and a writer can run concurrently. All writes go
through the :meth:`Database.transaction` context manager, which issues an
explicit ``BEGIN IMMEDIATE`` so a write lock is taken up-front and retries
``busy_timeout`` handle contention cleanly.
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

        Any row stuck in a transient *_ING status (e.g. PROCESSING from a
        worker that never finished) is reset to its safe, replayable state so
        the workers will pick it back up on restart.
        """
        notes: list[str] = []

        def _mark(sql: str, msg: str, conn: sqlite3.Connection) -> None:
            cur = conn.execute(sql)
            if cur.rowcount:
                notes.append(f"{msg}: {cur.rowcount}")

        with self.transaction() as conn:
            _mark(
                "UPDATE videos SET status='DOWNLOADED' WHERE status IN "
                "('DOWNLOADING','TRANSCRIBING','ANALYZING','PROCESSING')",
                "videos re-queued",
                conn,
            )
            _mark(
                "UPDATE clips SET status='READY' WHERE status IN ('CREATED','UPLOADING')",
                "clips re-queued",
                conn,
            )
            _mark(
                "UPDATE posts SET status='RETRY' WHERE status IN ('UPLOADING','PROCESSING')",
                "posts re-queued",
                conn,
            )
            _mark(
                "UPDATE jobs SET status='FAILED' WHERE status='RUNNING'",
                "jobs failed",
                conn,
            )
        return notes

    def count_by_status(self, table: str, status: str) -> int:
        rows = self.query(
            f"SELECT COUNT(*) AS c FROM {table} WHERE status=?", (status,)
        )
        return int(rows[0]["c"]) if rows else 0


# Shared singleton used by services/workers: `from database.db import db`.
db = Database()
