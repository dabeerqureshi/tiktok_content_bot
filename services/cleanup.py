"""Disk-space safety and retention cleanup.

Laptop storage is finite: originals are kept only until every clip has been
posted (plus a retention window), published clips are pruned after
``clips_keep_days``, and downloads are refused when free space drops below
``disk_critical_mb``.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from config import load_settings
from database.db import db

log = logging.getLogger(__name__)


def free_disk_mb(path: Path) -> float | None:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except Exception:  # pragma: no cover - unusual mounts
        return None


def disk_status() -> dict:
    """Snapshot used by health checks and the daily report."""
    settings = load_settings()
    mb = free_disk_mb(settings.data_dir)
    if mb is None:
        return {"free_mb": None, "state": "unknown"}
    if mb < settings.disk_critical_mb:
        state = "critical"
    elif mb < settings.disk_high_water_mb:
        state = "low"
    else:
        state = "ok"
    return {"free_mb": round(mb, 1), "state": state}


def enforce_disk_safety() -> str | None:
    """Run cleanup when space is low; return an alert message when critical."""
    settings = load_settings()
    status = disk_status()
    if status["free_mb"] is None:
        return None

    if status["state"] == "low":
        removed = run_cleanup()
        log.info("Disk low (%.0f MB free): cleaned %d file(s)",
                 status["free_mb"], removed)
        after = disk_status()
        if after["state"] == "ok":
            return None

    if status["state"] == "critical":
        run_cleanup()
        msg = (f"CRITICAL disk space: {status['free_mb']:.0f} MB free - "
               "downloads paused")
        log.warning(msg)
        return msg
    return None


def run_cleanup() -> int:
    """Delete expired artifacts. Returns the number of files removed."""
    settings = load_settings()
    now = time.time()
    removed = 0

    # 1. Temp dir: anything older than temp_keep_hours.
    cutoff = now - settings.temp_keep_hours * 3600
    for f in settings.temp_dir.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            removed += _delete(f)

    # 2. Published clips older than clips_keep_days.
    cutoff_days = now - settings.clips_keep_days * 86_400
    for row in db.query(
        "SELECT id, file_path FROM clips WHERE status='PUBLISHED' AND file_path IS NOT NULL"
    ):
        p = Path(row["file_path"])
        if p.exists() and p.stat().st_mtime < cutoff_days:
            removed += _delete(p)

    # 3. Originals for fully-posted videos older than originals_keep_days.
    cutoff_orig = now - settings.originals_keep_days * 86_400
    for row in db.query(
        "SELECT v.id, v.file_path FROM videos v "
        "WHERE v.status='COMPLETED' AND v.file_path IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.video_id=v.id "
        "                AND c.status NOT IN ('PUBLISHED','FAILED'))"
    ):
        p = Path(row["file_path"])
        if p.exists() and p.stat().st_mtime < cutoff_orig:
            removed += _delete(p)
            with db.transaction() as conn:
                conn.execute("UPDATE videos SET file_path=NULL WHERE id=?", (row["id"],))

    if removed:
        log.info("Cleanup removed %d file(s)", removed)
    return removed


def delete_original_if_all_posted(video_id: int) -> bool:
    """Free the original as soon as every one of its clips is PUBLISHED."""
    settings = load_settings()
    row = db.query(
        "SELECT file_path FROM videos WHERE id=? AND file_path IS NOT NULL",
        (video_id,),
    )
    if not row:
        return False
    pending = db.query(
        "SELECT COUNT(*) c FROM clips WHERE video_id=? AND status != 'PUBLISHED'",
        (video_id,),
    )[0]["c"]
    if pending:
        return False
    p = Path(row[0]["file_path"])
    if p.exists():
        _delete(p)
        log.info("Deleted original for video %d (all clips posted)", video_id)
    with db.transaction() as conn:
        conn.execute("UPDATE videos SET file_path=NULL WHERE id=?", (video_id,))
    return True


def _delete(path: Path) -> int:
    try:
        path.unlink()
        return 1
    except OSError as exc:  # pragma: no cover
        log.warning("Could not delete %s: %s", path, exc)
        return 0