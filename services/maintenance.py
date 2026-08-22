"""Periodic maintenance: hourly health checks + daily email report.

Runs inside the scheduler loop. Cadence state is persisted in
``system_events`` so a restart does not re-send the daily report.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from config import load_settings
from database.db import db
from services import cleanup, email, ffmpeg, ollama
from services.tiktok import TikTokService
from services.youtube import YouTubeService

log = logging.getLogger(__name__)

_HEALTH_INTERVAL = "maintenance health_check"
_REPORT_MARKER = "maintenance daily_report"


class Maintenance:
    def __init__(self) -> None:
        self._last_health: datetime | None = None

    # ------------------------------------------------------------------
    def run_cycle(self) -> None:
        settings = load_settings()
        now = datetime.now()

        if (self._last_health is None or
                (now - self._last_health).total_seconds()
                >= settings.health_check_interval_minutes * 60):
            self.check_health()
            self._last_health = now

        if now.hour == settings.daily_report_hour:
            self._daily_report_if_needed(settings, now.date())

    # ------------------------------------------------------------------
    def check_health(self, settings=None) -> list[str]:
        """Run all dependency checks; returns the list of problems found."""
        if settings is None:
            settings = load_settings()
        problems: list[str] = []

        if not ffmpeg.available():
            problems.append("ffmpeg not on PATH")
        if not YouTubeService().health_check():
            problems.append("yt-dlp import failed")
        if not ollama.available():
            problems.append(f"Ollama not reachable at {settings.ollama_host}")
        if not TikTokService().health_check():
            problems.append("TikTok credentials missing - publish worker idle")
        if not settings.smtp_configured:
            problems.append("SMTP not configured - no email alerts")

        disk = cleanup.disk_status()
        if disk["state"] == "critical":
            problems.append(f"CRITICAL disk space: {disk['free_mb']} MB free")

        if problems:
            db.insert_event("WARNING", _HEALTH_INTERVAL, "; ".join(problems))
            log.warning("[health] %s", "; ".join(problems))
            if any("Ollama" in p or "ffmpeg" in p for p in problems):
                email.alert("Health check failing", "\n".join(problems))
        else:
            log.info("[health] all checks OK")
        return problems

    # ------------------------------------------------------------------
    def _daily_report_if_needed(self, settings, today: date) -> None:
        row = db.query(
            "SELECT message FROM system_events "
            "WHERE component=? AND message LIKE ? "
            "ORDER BY id DESC LIMIT 1",
            (_REPORT_MARKER, f"{today.isoformat()}|%"),
        )
        if row:
            return  # already sent today

        stats = collect_stats(settings)
        body = "\n".join(f"{k}: {v}" for k, v in stats.items())
        sent = email.daily_report(body)
        marker = f"{today.isoformat()}|sent={sent}"
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO system_events (level, component, message) VALUES ('INFO', ?, ?)",
                (_REPORT_MARKER, marker),
            )
        log.info("Daily report %s", "sent" if sent else "skipped (SMTP unset)")


def collect_stats(settings) -> dict:
    """Aggregate the numbers quoted in the daily report."""

    def scalar(sql: str, params: tuple = ()) -> int:
        rows = db.query(sql, params)
        return int(rows[0][list(rows[0].keys())[0]]) if rows else 0

    return {
        "videos completed (24h)": scalar(
            "SELECT COUNT(*) FROM videos WHERE status='COMPLETED' "
            "AND processed_at >= datetime('now', '-1 day')"),
        "clips created (24h)": scalar(
            "SELECT COUNT(*) FROM clips WHERE created_at >= datetime('now', '-1 day')"),
        "posts published (24h)": scalar(
            "SELECT COUNT(*) FROM posts WHERE status='PUBLISHED' "
            "AND posted_at >= datetime('now', '-1 day')"),
        "failed uploads (24h)": scalar(
            "SELECT COUNT(*) FROM posts WHERE status='FAILED' "
            "AND created_at >= datetime('now', '-7 day')"),
        "queue ready": scalar("SELECT COUNT(*) FROM clips WHERE status='READY'"),
        "queue scheduled": scalar("SELECT COUNT(*) FROM clips WHERE status='SCHEDULED'"),
        "disk free MB": cleanup.disk_status()["free_mb"],
    }