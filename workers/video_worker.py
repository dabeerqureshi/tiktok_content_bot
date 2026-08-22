"""Source discovery + download worker.

- Discovers the latest eligible video per enabled source (deduped by the
  UNIQUE youtube_id constraint).
- Downloads any video stuck in DISCOVERED / DOWNLOADING.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import load_settings
from database.db import db
from services import cleanup, downloader, email
from services.youtube import YouTubeService
from .base import Worker

log = logging.getLogger(__name__)

MAX_DOWNLOAD_ATTEMPTS = 3


class VideoWorker(Worker):
    name = "video_worker"

    def __init__(self) -> None:
        self.youtube = YouTubeService()

    def run_once(self) -> None:
        self._discover()
        self._download_pending()

    def _discover(self) -> None:
        sources = db.query("SELECT * FROM sources WHERE enabled=1")
        for src in sources:
            try:
                meta = self.youtube.get_metadata(src["channel_url"])
                if not meta.youtube_id:
                    continue
                # INSERT OR IGNORE: youtube_id is UNIQUE, so duplicates roll off.
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO videos "
                        "(source_id, youtube_id, url, title, duration, thumbnail, status) "
                        "VALUES (?, ?, ?, ?, ?, ?, 'DISCOVERED')",
                        (
                            src["id"],
                            meta.youtube_id,
                            meta.webpage_url,
                            meta.title,
                            meta.duration,
                            meta.thumbnail,
                        ),
                    )
                    conn.execute(
                        "UPDATE sources SET last_checked_at=datetime('now') WHERE id=?",
                        (src["id"],),
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("Discovery failed for %s: %s", src["name"], exc)
                db.insert_event("WARNING", self.name, f"source {src['name']}: {exc}")

    def _download_pending(self) -> None:
        # Disk safety first: refuse to fill the drive.
        alert_msg = cleanup.enforce_disk_safety()
        if alert_msg:
            db.insert_event("ERROR", self.name, alert_msg)
            email.alert("Disk space critical", alert_msg)
            return
        rows = db.query(
            "SELECT * FROM videos WHERE status IN ('DISCOVERED','DOWNLOADING') ORDER BY id LIMIT 1"
        )
        for row in rows:
            self._download(row)

    def _download(self, row) -> None:
        # Idempotent resume: an already-downloaded file is not re-fetched.
        existing = Path(row["file_path"]) if row["file_path"] else None
        if existing and existing.exists():
            with db.transaction() as conn:
                conn.execute("UPDATE videos SET status='DOWNLOADED' WHERE id=?", (row["id"],))
            return

        with db.transaction() as conn:
            conn.execute(
                "UPDATE videos SET status='DOWNLOADING', last_error=NULL WHERE id=?",
                (row["id"],),
            )
        try:
            path = downloader.download_video(row["youtube_id"], row["url"])
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET status='DOWNLOADED', file_path=?, downloaded_at=datetime('now') "
                    "WHERE id=?",
                    (str(path), row["id"]),
                )
            log.info("Downloaded video %s", row["youtube_id"])
        except Exception as exc:  # noqa: BLE001
            log.exception("Download failed for %s", row["youtube_id"])
            attempts = row["attempts"] + 1
            status = "FAILED" if attempts >= MAX_DOWNLOAD_ATTEMPTS else "DISCOVERED"
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET status=?, last_error=?, attempts=? WHERE id=?",
                    (status, str(exc), attempts, row["id"]),
                )
            db.insert_event("ERROR", self.name, f"{row['youtube_id']}: {exc}")