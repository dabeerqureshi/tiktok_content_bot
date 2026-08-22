"""Downloads the source video to ``storage/originals``.

Uses yt-dlp (kept separate from :mod:`services.youtube` so the download
options can evolve independently). Output is normalized to ``<youtube_id>.mp4``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from config import load_settings

log = logging.getLogger(__name__)


def download_video(youtube_id: str, url: str, out_dir: Path | None = None) -> Path:
    import yt_dlp  # noqa: PLC0415

    settings = load_settings()
    out_dir = out_dir or settings.originals_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    opts: dict = {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(out_dir / f"{youtube_id}.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 4,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    ext = str(info.get("ext") or "mp4")
    path = out_dir / f"{youtube_id}.{ext}"
    if not path.exists():
        # Re-scan the folder in case the extension on disk differs.
        matches = list(out_dir.glob(f"{youtube_id}.*"))
        if not matches:
            raise FileNotFoundError(f"Download produced no file for {youtube_id}")
        path = matches[0]
    log.info("Downloaded %s -> %s", youtube_id, path)
    return path