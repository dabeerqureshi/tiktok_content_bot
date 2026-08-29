"""Periodic maintenance: hourly health checks + daily email report.

Runs inside the scheduler loop. Cadence state is persisted in
``system_events`` so a restart does not re-send the daily report.
"""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime

from config import load_settings
from database.db import db
from services import email
from services.tiktok import TikTokService

log = logging.getLogger(__name__)

_HEALTH_INTERVAL = "maintenance health_check"
_REPORT_MARKER = "maintenance daily_report"


class Maintenance:
    def __init__(self) -> None:
        self._last_health: datetime | None = None

    # ------------------------------------------------------------------
    def run_cycle(self) -> None:
        s = load_settings()
        now = datetime.now()

        if (
            self._last_health is None
            or (now - self._last_health).total_seconds()
            >= s.health_check_interval_minutes * 60
        ):
            self.check_health()
            self._last_health = now

        if now.hour == s.daily_report_hour:
            self._daily_report_if_needed(now.date())

    # ------------------------------------------------------------------
    def check_health(self, settings=None) -> list[str]:
        """Run dependency checks; returns the list of problems found."""
        s = settings or load_settings()
        problems: list[str] = []

        if not TikTokService().health_check():
            problems.append("TikTok credentials missing - publish worker idle")
        if not s.smtp_configured:
            problems.append("SMTP not configured - no email alerts")
        if not s.tiktok_title:
            problems.append("tiktok_title is empty")
        if not s.content_dir.exists():
            problems.append(f"content folder missing: {s.content_dir}")
        else:
            free_mb = shutil.disk_usage(s.content_dir).free // (1024 * 1024)
            if free_mb < 1000:
                problems.append(f"low disk: {free_mb} MB free")

        if problems:
            db.insert_event("WARNING", _HEALTH_INTERVAL, "; ".join(problems))
            log.warning("[health] %s", "; ".join(problems))
        else:
            log.info("[health] all checks OK")
        return problems

    # ------------------------------------------------------------------
    def _daily_report_if_needed(self, today: date) -> None:
        row = db.query(
            "SELECT message FROM system_events "
            "WHERE component=? AND message LIKE ? ORDER BY id DESC LIMIT 1",
            (_REPORT_MARKER, f"{today.isoformat()}|%"),
        )
        if row:
            return  # already sent today

        stats = collect_stats(load_settings())
        body = "\n".join(f"{k}: {v}" for k, v in stats.items())
        sent = email.daily_report(body)
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO system_events (level, component, message) VALUES ('INFO', ?, ?)",
                (_REPORT_MARKER, f"{today.isoformat()}|sent={sent}"),
            )
        log.info("Daily report %s", "sent" if sent else "skipped (SMTP unset)")


def collect_stats(settings) -> dict:
    """Aggregate the numbers quoted in the daily report."""
    def scalar(sql: str, params: tuple = ()) -> int:
        rows = db.query(sql, params)
        return int(rows[0][list(rows[0].keys())[0]]) if rows else 0

    counts = {
        r["status"]: r["c"]
        for r in db.query("SELECT status, COUNT(*) AS c FROM videos GROUP BY status")
    }
    today = datetime.now().strftime("%Y-%m-%d")
    disk = (
        shutil.disk_usage(settings.content_dir).free // (1024 * 1024)
        if settings.content_dir.exists() else "n/a"
    )
    return {
        "total videos": sum(counts.values()),
        "uploaded (today)": scalar(
            "SELECT COUNT(*) AS c FROM videos WHERE status='UPLOADED' AND uploaded_at LIKE ?",
            (f"{today}%",),
        ),
        "uploaded (all-time)": counts.get("UPLOADED", 0),
        "pending": counts.get("PENDING", 0),
        "uploading": counts.get("UPLOADING", 0),
        "failed": counts.get("FAILED", 0),
        "disk free MB": disk,
    }
