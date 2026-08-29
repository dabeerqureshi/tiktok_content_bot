"""Management CLI for the TikTok folder uploader.

Examples:
    python scripts/manage.py status           # show config + counts + next slot
    python scripts/manage.py list             # table of videos
    python scripts/manage.py requeue-failed   # reset FAILED -> PENDING
    python scripts/manage.py run-once --simulate --cycles 3
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

# Make the project root importable when run as `python scripts/manage.py ...`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import logging  # noqa: E402

from config import load_settings  # noqa: E402
from database import db, migrate  # noqa: E402
from services.tiktok import TikTokService  # noqa: E402
from workers import FolderWorker  # noqa: E402


def _setup():
    s = load_settings()
    s.ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    migrate(db)
    return s


def _counts():
    rows = db.query("SELECT status, COUNT(*) AS c FROM videos GROUP BY status")
    return {r["status"]: r["c"] for r in rows}


def _next_slot(s):
    times = sorted(dtime(h, m) for h, m in s.upload_schedule())
    now = datetime.now()
    today = now.date()
    for t in times:
        slot = datetime.combine(today, t)
        if slot > now:
            return slot.strftime("%Y-%m-%d %H:%M:%S")
    slot = datetime.combine(today, times[0]) + timedelta(days=1)
    return slot.strftime("%Y-%m-%d %H:%M:%S")


def cmd_status(_args):
    s = load_settings()
    print("TikTok Content Bot - status")
    print(f"  content_dir : {s.content_dir}")
    print(f"  upload_times: {s.upload_times}")
    print(f"  title       : {s.tiktok_title!r}")
    print(f"  pick_order  : {s.pick_order}")
    print(f"  simulate    : {s.simulate}")
    print(f"  next slot   : {_next_slot(s)}")
    print(f"  tiktok auth : {'ready' if TikTokService().health_check() else 'NOT configured'}")
    print(f"  smtp        : {'ready' if s.smtp_configured else 'not configured'}")

    counts = _counts()
    total = sum(counts.values())
    print("\nVideos:")
    for status in ("PENDING", "UPLOADING", "UPLOADED", "FAILED"):
        print(f"  {status:<10}: {counts.get(status, 0)}")
    print(f"  {'total':<10}: {total}")

    pending = counts.get("PENDING", 0)
    if pending:
        print("\nNext videos to upload:")
        rows = db.query(
            "SELECT id, file_name, size_bytes, attempts FROM videos "
            "WHERE status='PENDING' ORDER BY attempts DESC, discovered_at ASC, id ASC LIMIT 5"
        )
        for r in rows:
            size_mb = r["size_bytes"] / (1024 * 1024)
            print(f"  #{r['id']} {r['file_name']}  {size_mb:.1f}MB  attempts={r['attempts']}")


def cmd_list(_args):
    rows = db.query(
        "SELECT id, file_name, status, attempts, size_bytes, uploaded_at, last_error "
        "FROM videos ORDER BY id"
    )
    if not rows:
        print("No videos registered yet. Drop files into the content folder.")
        return
    print(f"{'#':>3}  {'status':<10} {'attempts':>7} {'size MB':>9}  name")
    for r in rows:
        size_mb = r["size_bytes"] / (1024 * 1024)
        print(f"{r['id']:>3}  {r['status']:<10} {r['attempts']:>7} {size_mb:>9.1f}  {r['file_name']}")
    print(f"\n{sum(1 for _ in rows)} video(s) total.")


def cmd_requeue_failed(_args):
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE videos SET status='PENDING', attempts=0, next_retry_at=NULL, "
            "last_error=NULL WHERE status='FAILED'"
        )
        count = cur.rowcount
    print(f"Requeued {count} failed video(s).")


def cmd_run_once(args):
    if args.simulate:
        os.environ["SIMULATE"] = "true"
    worker = FolderWorker()
    for i in range(args.cycles):
        print(f"--- cycle {i + 1}/{args.cycles} ---")
        worker.run_once()
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="TikTok Content Bot management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, fn, help_text in (
        ("status", cmd_status, "show config, counts, next slot"),
        ("list", cmd_list, "list all videos"),
        ("requeue-failed", cmd_requeue_failed, "reset FAILED -> PENDING"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=fn)

    p = sub.add_parser("run-once", help="run N worker cycles and exit")
    p.add_argument("--cycles", type=int, default=1)
    p.add_argument("--simulate", action="store_true",
                   help="dry-run: mark uploads without calling TikTok")
    p.set_defaults(func=cmd_run_once)

    args = parser.parse_args()
    _setup()
    args.func(args)


if __name__ == "__main__":
    main()
