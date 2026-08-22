"""SMTP alerts (monitoring only, never part of the control path).

Silently no-ops when SMTP is not configured, so the app never crashes on a
missing email server.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from email.utils import formataddr

from config import load_settings

log = logging.getLogger(__name__)


def send(subject: str, body: str) -> bool:
    s = load_settings()
    if not (s.smtp_host and s.smtp_from and s.smtp_to):
        log.info("SMTP not configured; skipping email '%s'", subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((s.smtp_from_name or "TikTok Bot", s.smtp_from))
    msg["To"] = s.smtp_to
    msg.set_content(body)
    try:
        with smtplib.SMTP(s.smtp_host, int(s.smtp_port or 587), timeout=30) as server:
            if s.smtp_starttls:
                server.starttls()
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password or "")
            server.send_message(msg)
        return True
    except Exception as exc:  # pragma: no cover - depends on SMTP
        log.warning("SMTP send failed: %s", exc)
        return False


def alert(title: str, body: str) -> None:
    send(f"[ALERT] TikTok Bot Error: {title}", body)


def daily_report(lines: list[str]) -> None:
    send("[REPORT] Daily TikTok Bot Report", "\n".join(lines))