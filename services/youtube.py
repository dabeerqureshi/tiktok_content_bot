"""Isolated wrapper around yt-dlp for METADATA extraction only.

YouTube keeps changing its delivery (Proof-of-Origin tokens, 403s for some
formats). Every interaction with yt-dlp lives in this module and
:mod:`services.downloader` so that if YouTube changes approach again, you
only change one file. Adding cookies (``cookiefile``) and regularly updating
yt-dlp via pip are the two most common fixes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class VideoMetadata:
    youtube_id: str
    title: str
    duration: float
    webpage_url: str
    thumbnail: str | None = None
    channel: str | None = None


class YouTubeService:
    def __init__(self, cookiefile: str | None = None) -> None:
        self._ytdlp = None
        self._cookiefile = cookiefile

    def _ydl_opts(self) -> dict:
        opts: dict = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
        }
        if self._cookiefile:
            opts["cookiefile"] = self._cookiefile
        return opts

    def _import(self):
        """Import yt_dlp lazily and cache the module."""
        if self._ytdlp is None:
            import yt_dlp  # noqa: PLC0415

            self._ytdlp = yt_dlp
        return self._ytdlp

    def get_metadata(self, url: str) -> VideoMetadata:
        ydlp = self._import()
        with ydlp.YoutubeDL(self._ydl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
        return VideoMetadata(
            youtube_id=str(info.get("id", "")),
            title=str(info.get("title") or ""),
            duration=float(info.get("duration") or 0),
            webpage_url=str(info.get("webpage_url") or url),
            thumbnail=info.get("thumbnail"),
            channel=info.get("channel"),
        )

    def health_check(self) -> bool:
        try:
            self._import()
            return True
        except Exception as exc:  # pragma: no cover - depends on env
            log.warning("yt-dlp health check failed: %s", exc)
            return False