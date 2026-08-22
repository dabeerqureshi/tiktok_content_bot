"""TikTok Content Bot - application entrypoint.

Responsibilities:
- Config + directories + logging + schema migration.
- Crash recovery (reset abandoned in-flight jobs).
- Startup health checks (ffmpeg / yt-dlp / Ollama / TikTok / disk space).
- Boot workers and run the scheduler until Ctrl+C, then shut down gracefully.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from config import load_settings
from database import db, migrate
from services import email
from services.maintenance import Maintenance
from services.scheduler import Scheduler
from workers import ClipWorker, PublishWorker, VideoWorker


def _setup_logging(settings) -> None:
    settings.ensure_dirs()
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        settings.log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Windows consoles default to cp1252 and raise UnicodeEncodeError on
    # non-ASCII log output; force UTF-8 with replacement so logging never
    # takes down a worker thread.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-standard stdout
        pass

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    # Quieten noisy third-party loggers.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _main() -> None:
    settings = load_settings()
    settings.ensure_dirs()
    _setup_logging(settings)
    log = logging.getLogger("app")

    problems = settings.validate()
    if problems:
        for p in problems:
            log.error("[config] %s", p)
        sys.exit(1)

    try:
        version = migrate(db)
    except Exception as exc:  # pragma: no cover
        log.exception("Schema migration failed")
        sys.exit(1)
    log.info("Schema v%d applied (db=%s)", version, db.db_path)

    notes = db.reset_abandoned_jobs()
    for note in notes:
        log.info("[recovery] %s", note)
    if notes:
        db.insert_event("INFO", "recovery", "; ".join(notes))

    maintenance = Maintenance()
    maintenance.check_health(settings)

    workers: list = [VideoWorker(), ClipWorker(), PublishWorker()]
    scheduler = Scheduler(workers, maintenance=maintenance.run_cycle)
    scheduler.start()

    stop = threading.Event()

    def _shutdown(signum, _frame) -> None:  # pragma: no cover
        log.info("Received signal %s - shutting down", signum)
        stop.set()
        scheduler.stop()
        email.send("TikTok Bot stopped", "Graceful shutdown completed.")

    try:
        signal.signal(signal.SIGINT, _shutdown)
    except (ValueError, OSError):  # not the main thread / non-POSIX
        pass
    try:
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, OSError):
        pass

    log.info("TikTok Bot running. Press Ctrl+C to stop.")
    try:
        stop.wait()
    except KeyboardInterrupt:
        _shutdown("KeyboardInterrupt", None)
    log.info("Exiting.")


if __name__ == "__main__":
    _main()