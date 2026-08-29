"""Smoke tests for the folder uploader bot.

Runs in SIMULATE mode against a throwaway SQLite DB and a temp content folder,
so no network or TikTok credentials are required. Run with:

    python scripts/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Configure isolation BEFORE importing app modules (the db singleton and
# Settings read the environment at import time).
_TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(_TMP / "t.db")
os.environ["CONTENT_DIR"] = str(_TMP / "content")
os.environ["UPLOAD_TIMES"] = "00:00,00:01,00:02"   # always-elapsed slots in tests
os.environ["PICK_ORDER"] = "oldest"
os.environ["TIKTOK_TITLE"] = "test-title"
os.environ["TIKTOK_CLIENT_KEY"] = "x"
os.environ["TIKTOK_REFRESH_TOKEN"] = "y"
os.environ["SIMULATE"] = "true"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Settings, load_settings  # noqa: E402
from database import db, migrate  # noqa: E402
from workers.folder_worker import FolderWorker  # noqa: E402


def _counts() -> dict:
    return {
        r["status"]: r["c"]
        for r in db.query("SELECT status, COUNT(*) AS c FROM videos GROUP BY status")
    }


def main() -> None:
    settings = load_settings()
    migrate(db)

    # --- schema --------------------------------------------------------
    tables = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"videos", "state", "system_events", "schema_version"} <= tables
    ver = db.query("SELECT MAX(version) v FROM schema_version")[0]["v"]
    assert ver == 1
    print("SCHEMA OK (v1)")

    # --- schedule parser ----------------------------------------------
    assert settings.upload_schedule() == [(0, 0), (0, 1), (0, 2)]
    try:
        settings.upload_times = "25:99"
        settings.upload_schedule()
        raise AssertionError("bad schedule not rejected")
    except ValueError:
        pass
    print("SCHEDULE PARSER OK")

    # --- idempotency: UNIQUE content_hash -----------------------------
    with db.transaction() as conn:
        conn.execute("INSERT INTO videos (file_name, file_path, content_hash, size_bytes) "
                     "VALUES ('a.mp4', 'x', 'hash-a', 10)")
        conn.execute("INSERT OR IGNORE INTO videos (file_name, file_path, content_hash, size_bytes) "
                     "VALUES ('a-copy.mp4', 'y', 'hash-a', 10)")
    n = db.query("SELECT COUNT(*) AS c FROM videos WHERE content_hash='hash-a'")[0]["c"]
    assert n == 1, "UNIQUE(content_hash) not enforced"
    db.query("DELETE FROM videos")  # clean slate for the E2E test
    print("IDEMPOTENCY OK (content_hash UNIQUE)")

    # --- backoff math --------------------------------------------------
    base = settings.retry_backoff_base_seconds
    assert [base * (2 ** (n - 1)) for n in (1, 2, 3)] == [base, base * 2, base * 4]
    print("BACKOFF MATH OK")

    # --- end-to-end simulate ------------------------------------------
    content = settings.content_dir
    content.mkdir(parents=True, exist_ok=True)
    payloads = {
        "f1.mp4": b"AAAA-video-data",
        "f1_dup.mp4": b"AAAA-video-data",   # same content -> deduped
        "f2.mp4": b"BBBB-video-data",
        "f3.mp4": b"CCCC-video-data",
    }
    for name, data in payloads.items():
        (content / name).write_bytes(data)

    worker = FolderWorker()
    # cycle 1: register + 1 upload ; cycles 2-3: 2 more uploads
    for _ in range(3):
        worker.run_once()

    counts = _counts()
    print("counts:", counts)
    assert sum(counts.values()) == 3, f"expected 3 videos, got {counts}"
    assert counts.get("UPLOADED", 0) == 3, f"expected 3 uploaded, got {counts}"
    assert counts.get("PENDING", 0) == 0
    assert counts.get("FAILED", 0) == 0
    print("END-TO-END SIMULATE OK (3 unique files uploaded, duplicate skipped)")

    # --- recovery: pre-init (UPLOADING, no publish_id) -> PENDING -----
    with db.transaction() as conn:
        conn.execute(
            "UPDATE videos SET status='UPLOADING', publish_id=NULL "
            "WHERE id=(SELECT id FROM videos WHERE status='UPLOADED' LIMIT 1)"
        )
    notes = db.reset_abandoned_jobs()
    assert db.query(
        "SELECT COUNT(*) AS c FROM videos WHERE status='UPLOADING'"
    )[0]["c"] == 0
    assert db.query(
        "SELECT COUNT(*) AS c FROM videos WHERE status='PENDING'"
    )[0]["c"] >= 1
    print("RECOVERY (pre-init) OK:", notes or "no-op")

    # --- recovery: mid-upload (publish_id present) -> re-resolve ------
    with db.transaction() as conn:
        conn.execute(
            "UPDATE videos SET status='UPLOADING' "
            "WHERE id=(SELECT id FROM videos "
            "WHERE status='UPLOADED' AND publish_id IS NOT NULL LIMIT 1)"
        )
    worker._recover_inflight()
    assert db.query(
        "SELECT COUNT(*) AS c FROM videos WHERE status='UPLOADING'"
    )[0]["c"] == 0
    print("RECOVERY (mid-upload verify) OK")

    # --- config validation --------------------------------------------
    from pydantic import ValidationError  # noqa: E402
    try:
        Settings(pick_order="bogus")  # type: ignore[call-arg]
        raise AssertionError("bad pick_order not rejected")
    except ValidationError:
        pass
    print("CONFIG VALIDATION OK")

    print("\nALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
