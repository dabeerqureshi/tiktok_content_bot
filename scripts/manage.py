"""Management CLI.

Examples:
    python scripts/manage.py add-source "My Channel" https://www.youtube.com/@example
    python scripts/manage.py sources
    python scripts/manage.py stats
    python scripts/manage.py health
    python scripts/manage.py reset-failed
    python scripts/manage.py cleanup
    python scripts/manage.py run-once          # single worker cycle (no loop)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402
import logging  # noqa: E402

from config import load_settings  # noqa: E402
from database import db, migrate  # noqa: E402


def _setup():
    settings = load_settings()
    settings.ensure_dirs()
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    version = migrate(db)
    return settings, version


def cmd_add_source(args) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sources (name, channel_url) VALUES (?, ?)",
            (args.name, args.url),
        )
    print(f"Source added: {args.name} -> {args.url}")


def cmd_sources(_args) -> None:
    rows = db.query("SELECT * FROM sources ORDER BY id")
    if not rows:
        print("No sources configured. Use add-source.")
        return
    for r in rows:
        state = "enabled" if r["enabled"] else "disabled"
        print(f"[{r['id']}] {r['name']} ({state})\n    {r['channel_url']}")


def cmd_stats(_args) -> None:
    from services.maintenance import collect_stats

    for key, value in collect_stats(load_settings()).items():
        print(f"{key}: {value}")
    print("\nBy status:")
    for table in ("videos", "clips", "posts"):
        rows = db.query(
            f"SELECT status, COUNT(*) c FROM {table} GROUP BY status ORDER BY status"
        )
        summary = ", ".join(f"{r['status']}={r['c']}" for r in rows) or "empty"
        print(f"  {table}: {summary}")


def cmd_health(_args) -> None:
    from services.maintenance import Maintenance

    problems = Maintenance().check_health()
    print("\n".join(problems) if problems else "All health checks OK.")


def cmd_reset_failed(_args) -> None:
    notes = db.reset_abandoned_jobs()
    with db.transaction() as conn:
        for sql in (
            "UPDATE videos SET status='DOWNLOADED', attempts=0 WHERE status='FAILED' AND file_path IS NOT NULL",
            "UPDATE clips SET status='READY', attempts=0 WHERE status='FAILED'",
            "UPDATE posts SET status='PENDING', attempts=0, next_retry_at=NULL "
            "WHERE status IN ('FAILED','RETRY')",
        ):
            conn.execute(sql)
    print("Reset complete:", "; ".join(notes) or "nothing in flight")


def cmd_cleanup(_args) -> None:
    from services import cleanup

    removed = cleanup.run_cleanup()
    disk = cleanup.disk_status()
    print(f"Removed {removed} file(s). Disk: {disk['free_mb']} MB free ({disk['state']}).")


def cmd_run_once(args) -> None:
    from services.scheduler import Scheduler
    from workers import ClipWorker, PublishWorker, VideoWorker

    scheduler = Scheduler([VideoWorker(), ClipWorker(), PublishWorker()])
    for i in range(args.cycles):
        print(f"--- cycle {i + 1}/{args.cycles} ---")
        scheduler.run_once()
    print("Done.")


def main() -> None:
    parser = argparse.ArgumentParser(description="TikTok Content Bot management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add-source", help="register an approved YouTube channel")
    p.add_argument("name")
    p.add_argument("url")
    p.set_defaults(func=cmd_add_source)

    for name, fn, help_text in (
        ("sources", cmd_sources, "list configured sources"),
        ("stats", cmd_stats, "queue/publishing statistics"),
        ("health", cmd_health, "run dependency health checks"),
        ("reset-failed", cmd_reset_failed, "requeue failed videos/clips/posts"),
        ("cleanup", cmd_cleanup, "run retention cleanup now"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(func=fn)

    p = sub.add_parser("run-once", help="run N worker cycles and exit")
    p.add_argument("--cycles", type=int, default=1)
    p.set_defaults(func=cmd_run_once)

    args = parser.parse_args()
    _setup()
    args.func(args)


if __name__ == "__main__":
    main()