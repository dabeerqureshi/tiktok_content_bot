"""Scheduler that drives the workers on a fixed poll cadence.

A single background thread runs every worker once per cycle, then runs the
maintenance cycle (hourly health checks + daily report). Worker failures are
isolated into ``system_events`` so one broken worker never stops the others.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from config import load_settings
from database.db import db

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        workers: list,
        maintenance: Callable[[], None] | None = None,
    ) -> None:
        self.workers = workers
        self.maintenance = maintenance
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()
        log.info("Scheduler started")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        log.info("Scheduler stopped")

    def run_once(self) -> None:
        """One full cycle - also used by the CLI for single-shot testing."""
        for worker in self.workers:
            try:
                worker.run_once()
            except Exception as exc:  # noqa: BLE001
                log.exception("Worker %s failed", worker.name)
                db.insert_event("ERROR", worker.name, str(exc))
        if self.maintenance:
            try:
                self.maintenance()
            except Exception as exc:  # noqa: BLE001
                log.exception("Maintenance failed")
                db.insert_event("ERROR", "maintenance", str(exc))

    def _run(self) -> None:
        settings = load_settings()
        while not self._stop.is_set():
            cycle_start = time.time()
            self.run_once()
            elapsed = time.time() - cycle_start
            wait = max(1, settings.scheduler_poll_seconds - elapsed)
            self._stop.wait(wait)