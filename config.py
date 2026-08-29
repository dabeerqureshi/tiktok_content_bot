from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Application configuration.

    Values are read (in order of precedence) from environment variables and
    then from a ``.env`` file at the project root. See ``.env.example`` for the
    full set of supported keys.
    """

    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Paths ---------------------------------------------------------
    data_dir: Path = BASE_DIR / "data"
    log_dir: Path = BASE_DIR / "logs"
    content_dir: Path = BASE_DIR / "content"
    db_path: Path = BASE_DIR / "data" / "content.db"
    log_path: Path = BASE_DIR / "logs" / "app.log"

    # --- Content -------------------------------------------------------
    video_extensions: str = ".mp4,.mov,.m4v,.mkv"
    tiktok_title: str = ""
    pick_order: str = "random"           # random | oldest | newest | name
    move_uploaded: bool = False         # move file to content/uploaded/ after success
    simulate: bool = False              # dry-run: pick + mark, no TikTok upload

    # --- Schedule ------------------------------------------------------
    upload_times: str = "04:00,12:00,20:00"   # comma-separated local times (HH:MM)
    scheduler_poll_seconds: int = 60
    daily_report_hour: int = 20               # local hour (0-23) for the daily email
    health_check_interval_minutes: int = 60

    # --- TikTok --------------------------------------------------------
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = None
    tiktok_access_token: str | None = None
    tiktok_open_id: str | None = None
    tiktok_refresh_token: str | None = None
    tiktok_privacy: str = "SELF_ONLY"         # SELF_ONLY until app audit passes
    tiktok_chunk_mb: int = 50                 # TikTok allows 5-64 MB
    tiktok_max_attempts: int = 3
    retry_backoff_base_seconds: int = 600     # base * 2^(attempt-1); 10m,20m,40m ...
    tiktok_pending_cap: int = 5               # TikTok: <=5 pending uploads / 24h

    # --- SMTP ---------------------------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_from_name: str = "TikTok Bot"
    smtp_to: str | None = None

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.log_dir, self.content_dir):
            d.mkdir(parents=True, exist_ok=True)
        if self.move_uploaded:
            (self.content_dir / "uploaded").mkdir(exist_ok=True)

    # --- Derived helpers ----------------------------------------------
    @property
    def tiktok_configured(self) -> bool:
        # A refresh token alone is enough; access tokens are minted from it.
        return bool(self.tiktok_client_key and self.tiktok_refresh_token)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.smtp_to)

    @property
    def video_exts(self) -> frozenset[str]:
        return frozenset(
            e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
            for e in self.video_extensions.split(",")
            if e.strip()
        )

    @field_validator("pick_order")
    @classmethod
    def _pick_order_valid(cls, v: str) -> str:
        allowed = {"random", "oldest", "newest", "name"}
        if v not in allowed:
            raise ValueError(f"pick_order must be one of {sorted(allowed)}")
        return v

    def upload_schedule(self) -> list[tuple[int, int]]:
        """Parse ``upload_times`` into a sorted list of (hour, minute) tuples."""
        out: list[tuple[int, int]] = []
        for raw in self.upload_times.split(","):
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split(":")
            if len(parts) != 2:
                raise ValueError(f"bad upload_times entry: {raw!r} (want HH:MM)")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError(f"bad upload_times entry: {raw!r}")
            out.append((h, m))
        out = sorted(set(out))
        if not out:
            raise ValueError("upload_times must contain at least one HH:MM entry")
        return out

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty list = valid)."""
        problems: list[str] = []
        if not self.tiktok_title:
            problems.append("tiktok_title is empty - set your constant post title")
        try:
            self.upload_schedule()
        except ValueError as exc:
            problems.append(str(exc))
        if not (5 <= self.tiktok_chunk_mb <= 64):
            problems.append("tiktok_chunk_mb must be within TikTok's 5-64 MB range")
        if self.tiktok_privacy not in ("SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "EVERYONE"):
            problems.append("tiktok_privacy must be SELF_ONLY, MUTUAL_FOLLOW_FRIENDS, or EVERYONE")
        if not (0 <= self.daily_report_hour <= 23):
            problems.append("daily_report_hour must be 0-23")
        return problems


def load_settings() -> Settings:
    return Settings()
