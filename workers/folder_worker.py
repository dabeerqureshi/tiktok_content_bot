"""Folder-aware publish worker.

Discovers videos in a local folder, deduplicates them by content hash, and
publishes them on a fixed daily schedule using the TikTok Content Posting API
(Direct Post, FILE_UPLOAD flow).

Lifecycle of a video row:

    PENDING  ->  UPLOADING  ->  UPLOADED   (terminal success)
                            ->  FAILED     (after tiktok_max_attempts)
              (on failure) ->  PENDING    (retry with backoff)

Slot rules (enforced each cycle):
  * elapsed slots today = count(upload_times whose time <= now)
  * uploads_today       = videos.status='UPLOADED' and uploaded_at is today
  * in_flight           = videos.status='UPLOADING'
  * capacity            = elapsed - uploads_today - in_flight  (>= 0)
  * if capacity > 0 and a PENDING video is eligible -> upload ONE.

This yields exactly N uploads/day aligned to the configured times, catches up
after downtime (one per 60s cycle), and never exceeds the daily quota even when
retries happen (a successful retry consumes the same slot a new post would have).
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from config import load_settings
from database.db import db
from services import email
from services.tiktok import TikTokError, TikTokService

log = logging.getLogger(__name__)

# Statuses that mean "the post really landed on TikTok."
SUCCESS_STATUSES = {"PUBLISH_SUCCEED", "SEND_TO_USER_INBOX", "SUCCESS"}
TERMINAL_FAILURES = {"PUBLISH_FAILED", "FAILED"}

_HASH_CHUNK = 8 * 1024 * 1024  # 8 MB read blocks


def _now_local() -> str:
    """Local timestamp 'YYYY-MM-DD HH:MM:SS' so slot math is timezone-local."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _now_local_offset(seconds: float) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_HASH_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


class FolderWorker:
    name = "folder_worker"

    def __init__(self) -> None:
        self.tiktok = TikTokService()

    # ------------------------------------------------------------------
    def run_once(self) -> None:
        s = load_settings()
        self._register_files()
        # Publish gate: simulate mode never touches TikTok.
        if not (s.simulate or self.tiktok.health_check()):
            if not s.simulate:
                log.warning("TikTok not configured; publish worker idle")
            return
        self._recover_inflight()
        self._publish_due()
        self._check_completion()

    # ------------------------------------------------------------------
    def _register_files(self) -> None:
        """Scan content_dir; hash new files; insert (dedup by content_hash)."""
        s = load_settings()
        if not s.content_dir.exists():
            return
        for p in s.content_dir.iterdir():
            if not p.is_file():
                continue  # skip subdirs (e.g. content/uploaded/)
            if p.suffix.lower() not in s.video_exts:
                continue
            try:
                size = p.stat().st_size
            except OSError as exc:
                log.warning("Could not stat %s: %s", p, exc)
                continue
            if size == 0:
                self._register_invalid(p, "zero-byte file (skipped)")
                continue
            content_hash = _hash_file(p)
            with db.transaction() as conn:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO videos "
                    "(file_name, file_path, content_hash, size_bytes, title) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (p.name, str(p), content_hash, size, s.tiktok_title),
                )
                if cur.rowcount:
                    log.info("Registered %s (%d bytes, sha256=%s)",
                             p.name, size, content_hash[:12])
        # Hashing runs in the scheduler thread; a few hundred large files may
        # take a few seconds on the first scan. Acceptable for this workload.

    def _register_invalid(self, p: Path, reason: str) -> None:
        try:
            content_hash = _hash_file(p)
        except OSError:
            content_hash = ""
        with db.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO videos "
                "(file_name, file_path, content_hash, size_bytes, "
                "status, last_error) VALUES (?, ?, ?, ?, 'FAILED', ?)",
                (p.name, str(p), content_hash, p.stat().st_size, reason),
            )

    # ------------------------------------------------------------------
    def _recover_inflight(self) -> None:
        """Resolve uploads that were mid-flight when the bot restarted."""
        s = load_settings()
        rows = db.query("SELECT * FROM videos WHERE status='UPLOADING'")
        for v in rows:
            if not v["publish_id"]:
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE videos SET status='PENDING' WHERE id=?", (v["id"],)
                    )
                continue
            try:
                if s.simulate:
                    status = "PUBLISH_SUCCEED"
                else:
                    status = self.tiktok.fetch_status(v["publish_id"])
            except TikTokError as exc:
                log.warning("Could not verify publish_id %s: %s", v["publish_id"], exc)
                db.insert_event("WARNING", self.name, f"verify {v['file_name']}: {exc}")
                continue
            self._resolve(v["id"], v["publish_id"], status, v)

    # ------------------------------------------------------------------
    def _publish_due(self) -> None:
        s = load_settings()
        times = sorted(dtime(h, m) for h, m in s.upload_schedule())
        now = datetime.now()
        elapsed = sum(1 for t in times if now.time() >= t)
        if elapsed == 0:
            return

        today = now.strftime("%Y-%m-%d")
        done_today = db.query(
            "SELECT COUNT(*) AS c FROM videos "
            "WHERE status='UPLOADED' AND uploaded_at LIKE ?",
            (f"{today}%",),
        )[0]["c"]
        in_flight = db.query(
            "SELECT COUNT(*) AS c FROM videos WHERE status='UPLOADING'"
        )[0]["c"]

        capacity = elapsed - done_today - in_flight
        if capacity <= 0:
            return
        if in_flight >= s.tiktok_pending_cap:
            log.info("Pending-upload cap reached (%d); holding new posts", in_flight)
            return

        # One upload per cycle keeps the API call cadence gentle while still
        # catching up promptly (each upload blocks the cycle until it returns).
        capacity = min(capacity, 1)
        video = self._pick_next(s, capacity)
        if video is None:
            return
        self._upload(video)

    def _pick_next(self, s, capacity: int):
        if s.pick_order == "random":
            order = "ORDER BY RANDOM()"
        elif s.pick_order == "oldest":
            order = "ORDER BY attempts DESC, discovered_at ASC, id ASC"
        elif s.pick_order == "newest":
            order = "ORDER BY attempts DESC, discovered_at DESC, id DESC"
        else:  # name
            order = "ORDER BY attempts DESC, file_name COLLATE NOCASE ASC"
        rows = db.query(
            f"SELECT * FROM videos WHERE status='PENDING' "
            f"AND (next_retry_at IS NULL OR next_retry_at <= ?) {order} LIMIT ?",
            (_now_local(), capacity),
        )
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    def _upload(self, video) -> None:
        s = load_settings()
        vid = video["id"]
        path = Path(video["file_path"])

        with db.transaction() as conn:
            conn.execute(
                "UPDATE videos SET status='UPLOADING', attempts=attempts+1, "
                "last_error=NULL WHERE id=?",
                (vid,),
            )

        if not path.exists():
            self._fail(vid, video, "file missing from disk")
            return

        try:
            if s.simulate:
                publish_id = f"dryrun-{video['file_name']}#{time.time_ns()}"
                status = "PUBLISH_SUCCEED"
            else:
                init = self.tiktok.init_video_upload(s.tiktok_title)
                publish_id = init["publish_id"]
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE videos SET publish_id=? WHERE id=?", (publish_id, vid)
                    )
                self.tiktok.upload_file(init["upload_url"], path)
                status = self.tiktok.fetch_status(publish_id)
            self._resolve(vid, publish_id, status, video)
        except (TikTokError, Exception) as exc:  # noqa: BLE001
            self._handle_failure(vid, video, exc, s)

    def _resolve(self, vid: int, publish_id: str, status: str, video) -> None:
        s = load_settings()
        if status in SUCCESS_STATUSES:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET status='UPLOADED', publish_id=?, "
                    "uploaded_at=?, last_error=NULL WHERE id=?",
                    (publish_id, _now_local(), vid),
                )
            log.info("Uploaded %s (publish_id=%s)", video["file_name"], publish_id)
            db.insert_event("INFO", "publish", f"{video['file_name']} -> uploaded")
            if s.move_uploaded:
                dest = s.content_dir / "uploaded" / video["file_name"]
                try:
                    Path(video["file_path"]).rename(dest)
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE videos SET file_path=? WHERE id=?", (str(dest), vid)
                        )
                except Exception:  # noqa: BLE001
                    log.exception("Could not move %s to uploaded/", video["file_name"])
        elif status in TERMINAL_FAILURES:
            self._fail(vid, video, f"TikTok rejected after upload: {status}")
        else:
            # Still processing on TikTok's side (or queued for their inbox).
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET status='UPLOADED', publish_id=?, "
                    "uploaded_at=? WHERE id=?",
                    (publish_id, _now_local(), vid),
                )
            log.info("Uploaded %s -> queued for processing (status=%s)",
                     video["file_name"], status)

    def _handle_failure(self, vid: int, video, exc: BaseException, s) -> None:
        fresh = db.query("SELECT attempts FROM videos WHERE id=?", (vid,))
        attempts = fresh[0]["attempts"] if fresh else 0
        if attempts < s.tiktok_max_attempts:
            backoff = s.retry_backoff_base_seconds * (2 ** (attempts - 1))
            next_retry = _now_local_offset(backoff)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET status='PENDING', attempts=?, "
                    "next_retry_at=?, last_error=? WHERE id=?",
                    (attempts, next_retry, str(exc)[:400], vid),
                )
            log.warning("Upload %s failed (%d/%d); retry @ %s",
                        video["file_name"], attempts, s.tiktok_max_attempts, next_retry)
            db.insert_event("WARNING", "publish", str(exc))
        else:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET status='FAILED', last_error=? WHERE id=?",
                    (str(exc)[:400], vid),
                )
            log.error("Upload %s permanently failed after %d attempts",
                      video["file_name"], attempts)
            db.insert_event("ERROR", "publish", str(exc))
            if not s.simulate:
                email.alert(
                    "TikTok upload failed",
                    f"Video: {video['file_name']}\nError: {exc}\n"
                    f"Attempts: {attempts}/{s.tiktok_max_attempts}",
                )

    def _fail(self, vid: int, video, reason: str) -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE videos SET status='FAILED', last_error=? WHERE id=?",
                (reason, vid),
            )
        log.error("Upload %s aborted: %s", video["file_name"], reason)
        db.insert_event("ERROR", "publish", f"{video['file_name']}: {reason}")

    # ------------------------------------------------------------------
    def _check_completion(self) -> None:
        """Email the user when the folder is exhausted; warn when it's low."""
        s = load_settings()
        if s.simulate:
            return
        counts = {
            r["status"]: r["c"]
            for r in db.query("SELECT status, COUNT(*) AS c FROM videos GROUP BY status")
        }
        pending = counts.get("PENDING", 0)
        uploading = counts.get("UPLOADING", 0)
        uploaded = counts.get("UPLOADED", 0)
        failed = counts.get("FAILED", 0)
        total = pending + uploading + uploaded + failed
        if total == 0:
            return  # nothing has ever been added

        busy = pending + uploading
        if busy == 0:
            marker = db.get_state("completion_total")
            if marker != str(total):
                failed_txt = (
                    f" ({failed} failed - see logs / `manage.py list`)"
                    if failed else ""
                )
                email.send(
                    "All videos uploaded ✅",
                    f"All {uploaded} of {total} video(s) in {s.content_dir} have been "
                    f"uploaded on TikTok{failed_txt}.\n\n"
                    "Add more videos to the folder to keep the schedule going.",
                )
                db.set_state("completion_total", str(total))
                db.set_state("low_stock_sent", "")
        elif busy <= 2:
            if db.get_state("low_stock_sent") is None:
                email.send(
                    "Stock running low ⚠️",
                    f"Only {busy} video(s) left to upload in {s.content_dir}. "
                    "Add more videos to keep the schedule going.",
                )
                db.set_state("low_stock_sent", "1")



