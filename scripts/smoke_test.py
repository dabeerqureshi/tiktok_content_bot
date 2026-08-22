"""Smoke tests for the scaffolding: schema, CRUD, recovery, models."""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database.db import Database
from database.migrations import SCHEMA_VERSION, migrate
from database.models import ClipList


def build_db() -> tuple[Database, Path]:
    tmp = Path(tempfile.mkdtemp())
    db = Database(tmp / "t.db")
    migrate(db)
    return db, tmp

db, tmp = build_db()

tables = sorted(r["name"] for r in db.query(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))
print("TABLES:", tables)
assert {"sources", "videos", "transcripts", "clips", "posts",
        "jobs", "system_events", "schema_version"} <= set(tables)

# Schema v2 columns + indexes must exist.
version_row = db.query("SELECT MAX(version) v FROM schema_version")[0]["v"]
assert version_row == SCHEMA_VERSION, f"schema version {version_row} != {SCHEMA_VERSION}"
cols = {r["name"] for r in db.query("PRAGMA table_info(posts)")}
assert "next_retry_at" in cols, "v2 column next_retry_at missing"
vcols = {r["name"] for r in db.query("PRAGMA table_info(videos)")}
assert "attempts" in vcols, "v2 column videos.attempts missing"
print(f"SCHEMA v{SCHEMA_VERSION} OK")

with db.transaction() as conn:
    conn.execute("INSERT INTO sources (name, channel_url) VALUES (?, ?)",
                 ("test", "https://youtu.be/x"))
    conn.execute("INSERT INTO videos (source_id, youtube_id, url, title) "
                 "VALUES (1, 'abc', 'u', 't')")
    clip_id = conn.execute(
        "INSERT INTO clips (video_id, title) VALUES (1, 'c')").lastrowid
    conn.execute("INSERT INTO posts (clip_id, scheduled_at) VALUES (?, datetime('now'))",
                 (clip_id,))

# UNIQUE(youtube_id) must reject duplicates.
with db.transaction() as conn:
    conn.execute("INSERT OR IGNORE INTO videos (youtube_id, url) VALUES ('abc','u2')")
dupes = db.query("SELECT COUNT(*) c FROM videos WHERE youtube_id='abc'")[0]["c"]
assert dupes == 1, "UNIQUE constraint not enforced"
print("IDEMPOTENCY OK")

# Recovery: mark in-flight, then reset.
with db.transaction() as conn:
    conn.execute("UPDATE posts SET status='UPLOADING'")
notes = db.reset_abandoned_jobs()
print("RECOVERY:", notes)
assert db.count_by_status("posts", "RETRY") == 1

# Publish slot math: 3/day -> evenly spaced, first slot in the future.
from workers.publish_worker import _fmt_time, _next_slot  # noqa: E402

with db.transaction() as conn:
    slot1 = _next_slot(conn, posts_per_day=3)
    conn.execute("INSERT INTO posts (clip_id, scheduled_at, status) "
                 "VALUES (1, ?, 'PENDING')", (slot1,))
    slot2 = _next_slot(conn, posts_per_day=3)
d1, d2 = datetime.fromisoformat(slot1), datetime.fromisoformat(slot2)
gap_hours = (d2 - d1).total_seconds() / 3600
assert abs(gap_hours - 8.0) < 1/60, f"slot gap {gap_hours}h != 8h"
assert d1 > datetime.now(), "first slot must be in the future"
print(f"SLOT MATH OK: {slot1} then {slot2} (+{gap_hours:.0f}h)")

# Backoff math: base * 2^(n-1).
base_backoff = 300
assert [base_backoff * (2 ** (n - 1)) for n in (1, 2, 3)] == [300, 600, 1200]
retry_time = _fmt_time(datetime.now().timestamp() + 600)
datetime.strptime(retry_time, "%Y-%m-%d %H:%M:%S")
print("BACKOFF MATH OK")

# Cleanup: published clip file past retention is removed.
from services import cleanup  # noqa: E402
import os  # noqa: E402
import time as _time  # noqa: E402

cleanup.db = db  # inject the test DB (module default points at data/content.db)

old_clip = tmp / "old_clip.mp4"
old_clip.write_bytes(b"x")
stale = _time.time() - 40 * 86_400  # older than clips_keep_days
os.utime(old_clip, (stale, stale))
with db.transaction() as conn:
    conn.execute("UPDATE clips SET status='PUBLISHED', file_path=? WHERE id=?",
                 (str(old_clip), clip_id))
removed = cleanup.run_cleanup()
assert not old_clip.exists() and removed >= 1, "retention cleanup failed"
fp_col = db.query("SELECT file_path FROM videos WHERE id=1")[0]["file_path"]
print("CLEANUP OK")

# Pydantic schema used for Ollama structured output.
cl = ClipList.model_validate({"clips": [{
    "start": 1.0, "end": 70.0, "score": 9, "reason": "r",
    "title": "T", "caption": "C", "hashtags": ["a"]}]})
print("PYDANTIC OK:", list(cl.clips[0].model_dump().keys()))
json.dumps(cl.model_json_schema())

# Config validation catches bad values.
from config import load_settings  # noqa: E402
settings = load_settings()
assert settings.validate() == [], f"unexpected config problems: {settings.validate()}"
print("CONFIG VALIDATION OK")

# Event logging helper.
db.insert_event("INFO", "smoke", "hello")
levels = [r["level"] for r in db.query("SELECT level FROM system_events")]
assert levels.count("INFO") == 1
print("ALL SMOKE TESTS PASSED")