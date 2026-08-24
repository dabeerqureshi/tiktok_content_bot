from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent

# A single module-level cascade of Paths so every component shares them.
_STORAGE = BASE_DIR / "storage"


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
    storage_dir: Path = _STORAGE
    originals_dir: Path = _STORAGE / "originals"
    clips_dir: Path = _STORAGE / "clips"
    failed_dir: Path = _STORAGE / "failed"
    temp_dir: Path = _STORAGE / "temp"
    db_path: Path = BASE_DIR / "data" / "content.db"
    log_path: Path = BASE_DIR / "logs" / "app.log"

    # --- Scheduling ----------------------------------------------------
    posts_per_day: int = 3
    scheduler_poll_seconds: int = 60
    daily_report_hour: int = 8          # local hour (0-23) for the daily email
    health_check_interval_minutes: int = 60

    # --- Retry / backoff -------------------------------------------------
    tiktok_max_attempts: int = 3
    retry_backoff_base_seconds: int = 300   # attempts>=1 -> base * 2^(n-1)
    tiktok_pending_cap: int = 5             # TikTok: <=5 pending API uploads/24h

    # --- Clip generation ----------------------------------------------
    clip_min_seconds: float = 45.0
    clip_target_seconds: float = 70.0
    clip_max_seconds: float = 90.0
    clip_width: int = 1080
    clip_height: int = 1920
    clip_fps: int = 30

    # --- TikTok --------------------------------------------------------
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = None
    tiktok_access_token: str | None = None
    tiktok_open_id: str | None = None
    tiktok_privacy: str = "SELF_ONLY"
    tiktok_chunk_mb: int = 50
    tiktok_refresh_token: str | None = None

    # --- Ollama ---------------------------------------------------------
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    # --- faster-whisper ------------------------------------------------
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # --- SMTP -----------------------------------------------------------
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_starttls: bool = True
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_from_name: str = "TikTok Bot"
    smtp_to: str | None = None

    # --- Disk safety -----------------------------------------------------
    disk_high_water_mb: int = 30_000  # < this => run cleanup
    disk_critical_mb: int = 10_000    # < this => stop downloading + alert
    originals_keep_days: int = 7      # delete completed originals after N days
    clips_keep_days: int = 30         # delete published clip files after N days
    temp_keep_hours: int = 24

    # --- Ollama ---------------------------------------------------------
    ollama_max_retries: int = 2       # re-ask on invalid/unbounded JSON

    # --- FFmpeg ----------------------------------------------------------
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # --- yt-dlp hardening -------------------------------------------------
    youtube_cookiefile: str | None = None  # exported cookies.txt for age/bot walls

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.log_dir,
            self.originals_dir,
            self.clips_dir,
            self.failed_dir,
            self.temp_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def tiktok_configured(self) -> bool:
        return bool(self.tiktok_access_token and self.tiktok_open_id)

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from and self.smtp_to)

    @property
    def clip_duration_bounds(self) -> tuple[float, float]:
        return self.clip_min_seconds, self.clip_max_seconds

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty list = valid)."""
        problems: list[str] = []
        if self.posts_per_day < 1:
            problems.append("posts_per_day must be >= 1")
        if not (0 < self.clip_min_seconds <= self.clip_target_seconds <= self.clip_max_seconds):
            problems.append("clip durations must satisfy min <= target <= max")
        if not (5 <= self.tiktok_chunk_mb <= 64):
            problems.append("tiktok_chunk_mb must be within TikTok's 5-64 MB range")
        if self.tiktok_privacy not in ("SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "EVERYONE"):
            problems.append("tiktok_privacy must be SELF_ONLY, MUTUAL_FOLLOW_FRIENDS, or EVERYONE")
        if self.youtube_cookiefile and not Path(self.youtube_cookiefile).is_file():
            problems.append(f"youtube_cookiefile not found: {self.youtube_cookiefile}")
        if not (0 <= self.daily_report_hour <= 23):
            problems.append("daily_report_hour must be 0-23")
        if self.disk_critical_mb >= self.disk_high_water_mb:
            problems.append("disk_critical_mb must be below disk_high_water_mb")
        return problems


def load_settings() -> Settings:
    return Settings()