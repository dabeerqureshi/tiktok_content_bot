"""Scheduling + publishing worker.

Owns the posts lifecycle:
    PENDING -> UPLOADING -> PROCESSING -> PUBLISHED
                 (retry)          \-> FAILED / RETRY
- SCHEDULED clips are enqueued as PENDING posts at the next time slot
  (interval = 24h / posts_per_day).
- Due posts (scheduled_at <= now) are published. publish_id is persisted so
  a crash between TikTok's init and completion can be recovered and the same
  clip is never uploaded twice blindly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path

from config import load_settings
from database.db import db
from services import email
from services.tiktok import TikTokError, TikTokService
from .base import Worker

log = logging.getLogger(__name__)

# Status values that mean "the post really landed on TikTok".
SUCCESS_STATUSES = {"PUBLISH_SUCCEED", "SEND_TO_USER_INBOX", "SUCCESS"}
TERMINAL_FAILURES = {"PUBLISH_FAILED", "FAILED"}


class PublishWorker(Worker):
    name = "publish_worker"

    def __init__(self) -> None:
        self.tiktok = TikTokService()

    def run_once(self) -> None:
        if not self.tiktok.health_check():
            log.warning("TikTok not configured; publish worker idle")
            return
        self._schedule_ready_clips()
        self._publish_due()

    def _schedule_ready_clips(self) -> None:
        settings = load_settings()
        with db.transaction() as conn:
            ready = conn.execute(
                "SELECT id FROM clips WHERE status='READY' "
                "AND id NOT IN (SELECT clip_id FROM posts)"
            ).fetchall()
            for row in ready:
                slot = _next_slot(conn, settings.posts_per_day)
                conn.execute(
                    "INSERT INTO posts (clip_id, scheduled_at, status) VALUES (?, ?, 'PENDING')",
                    (row["id"], slot),
                )
                conn.execute(
                    "UPDATE clips SET status='SCHEDULED' WHERE id=?", (row["id"],)
                )
                log.info("Scheduled clip %s -> %s", row["id"], slot)

    def _publish_due(self) -> None:
        settings = load_settings()

        # Posts stuck in PROCESSING for >24h mean TikTok never resolved the
        # status; requeue them (bounded by max_attempts) instead of blocking
        # the pending-upload cap forever.
        with db.transaction() as conn:
            conn.execute(
                "UPDATE posts SET status='RETRY', next_retry_at=datetime('now'), "
                "attempts=attempts+1 WHERE status='PROCESSING' "
                "AND attempts < ? "
                "AND created_at < datetime('now', '-1 day')",
                (settings.tiktok_max_attempts,),
            )
            conn.execute(
                "UPDATE posts SET status='FAILED', last_error='stuck in PROCESSING >24h' "
                "WHERE status='PROCESSING' AND attempts >= ?",
                (settings.tiktok_max_attempts,),
            )

        # Respect TikTok's ~5 pending API uploads per 24h window.
        in_flight = db.query(
            "SELECT COUNT(*) c FROM posts WHERE status IN ('UPLOADING','PROCESSING')"
        )[0]["c"]
        if in_flight >= settings.tiktok_pending_cap:
            log.info("Pending-upload cap reached (%d); holding new posts",
                     in_flight)
            return

        rows = db.query(
            "SELECT * FROM posts WHERE status IN ('PENDING','RETRY') "
            "AND scheduled_at IS NOT NULL AND scheduled_at <= datetime('now') "
            "AND (next_retry_at IS NULL OR next_retry_at <= datetime('now')) "
            "ORDER BY scheduled_at"
        )
        for post in rows:
            if in_flight >= settings.tiktok_pending_cap:
                break
            self._publish_one(post, settings)
            in_flight += 1

    def _publish_one(self, post, settings) -> None:
        post_id = post["id"]
        clip = db.query("SELECT * FROM clips WHERE id=?", (post["clip_id"],))
        clip = clip[0] if clip else None
        if clip is None:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE posts SET status='FAILED', last_error='missing clip' WHERE id=?",
                    (post_id,),
                )
            return

        with db.transaction() as conn:
            conn.execute(
                "UPDATE posts SET status='UPLOADING', attempts=attempts+1 WHERE id=?",
                (post_id,),
            )

        # Fail fast when the clip file is gone (e.g. pruned by retention
        # cleanup) instead of burning retries on a guaranteed FileNotFoundError.
        clip_file = Path(clip["file_path"]) if clip["file_path"] else None
        if not (clip_file and clip_file.exists()):
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE posts SET status='FAILED', last_error='clip file missing: '"
                    "|| COALESCE(?, 'null') WHERE id=?",
                    (clip["file_path"], post_id),
                )
                conn.execute("UPDATE clips SET status='FAILED' WHERE id=?", (clip["id"],))
            log.error("Clip file missing for post %s (%s); marked FAILED", post_id, clip["file_path"])
            return

        publish_id = post["tiktok_publish_id"]
        caption = (clip["caption"] or "") + " " + (clip["hashtags"] or "")
        title = clip["title"] or (clip["caption"] or "")[:150] or "Clip"
        try:
            if not publish_id:
                init = self.tiktok.init_video_upload(title)
                publish_id = init["publish_id"]
                # Persist the token immediately for crash recovery.
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE posts SET tiktok_publish_id=?, status='UPLOADING' WHERE id=?",
                        (publish_id, post_id),
                    )
                self.tiktok.upload_file(init["upload_url"], Path(clip["file_path"]))
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE posts SET status='PROCESSING' WHERE id=?", (post_id,)
                    )

            status = self.tiktok.fetch_status(publish_id)
            if status in SUCCESS_STATUSES:
                self._mark_published(post_id, clip)
            elif status in TERMINAL_FAILURES:
                raise TikTokError(f"TikTok reported {status}")
            else:
                # Still processing (or awaiting user inbox confirmation).
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE posts SET status='PROCESSING', last_error=? WHERE id=?",
                        (f"processing ({status})", post_id),
                    )
        except Exception as exc:  # noqa: BLE001
            log.exception("Publish failed for post %s", post_id)
            attempts = post["attempts"] + 1
            if attempts < settings.tiktok_max_attempts:
                new_status = "RETRY"
                backoff = settings.retry_backoff_base_seconds * (2 ** (attempts - 1))
                next_retry = _fmt_time(datetime.now().timestamp() + backoff)
            else:
                new_status, next_retry = "FAILED", None
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE posts SET status=?, last_error=?, attempts=?, next_retry_at=? "
                    "WHERE id=?",
                    (new_status, str(exc), attempts, next_retry, post_id),
                )
                if new_status == "FAILED":
                    conn.execute(
                        "UPDATE clips SET status='FAILED' WHERE id=?",
                        (clip["id"],),
                    )
            email.alert(
                "TikTok upload failed",
                f"Post #{post_id} / clip #{clip['id']}\nError: {exc}\n"
                f"Attempt: {attempts}/{settings.tiktok_max_attempts}"
                + (f"\nNext retry: {next_retry}" if next_retry else ""),
            )

    def _mark_published(self, post_id: int, clip) -> None:
        from services import cleanup  # local import avoids a cycle

        with db.transaction() as conn:
            conn.execute(
                "UPDATE posts SET status='PUBLISHED', posted_at=datetime('now'), "
                "last_error=NULL WHERE id=?",
                (post_id,),
            )
            conn.execute("UPDATE clips SET status='PUBLISHED' WHERE id=?", (clip["id"],))
        log.info("Post %s published", post_id)
        # Free the source original as soon as nothing is left to publish.
        try:
            cleanup.delete_original_if_all_posted(clip["video_id"])
        except Exception:  # noqa: BLE001 - cleanup must never break publishing
            log.exception("Post-publish cleanup failed for video %s", clip["video_id"])
def _fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _next_slot(conn, posts_per_day: int) -> str:
    """Return the next publishing time slot as 'YYYY-MM-DD HH:MM:SS'.

    Slots are evenly spaced across the day (interval = 24h / posts_per_day),
    continuing from the latest scheduled post, or the next boundary if none
    exist yet.
    """
    interval_s = 86_400.0 / max(1, posts_per_day)
    # Only active posts extend the schedule; FAILED/RETRY rows must not
    # push future slots further away.
    row = conn.execute(
        "SELECT MAX(scheduled_at) AS m FROM posts "
        "WHERE status IN ('PENDING','UPLOADING','PROCESSING')"
    ).fetchone()
    latest = row["m"] if row and row["m"] else None
    if latest:
        base = datetime.fromisoformat(latest)
        return (base + timedelta(seconds=interval_s)).strftime("%Y-%m-%d %H:%M:%S")
    now_epoch = datetime.now().timestamp()
    next_epoch = (int(now_epoch / interval_s) + 1) * interval_s
    return datetime.fromtimestamp(next_epoch).strftime("%Y-%m-%d %H:%M:%S")