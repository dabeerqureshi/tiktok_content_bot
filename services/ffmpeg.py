"""FFmpeg helpers: probe + clip rendering to TikTok-friendly output.

Standardizes clips to:
    container : MP4
    video     : H.264 (yuv420p) at 1080x1920 (9:16 portrait), 30 fps
    audio     : AAC 128 kbps, 44.1 kHz
    flags     : +faststart for progressive streaming
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from config import load_settings

log = logging.getLogger(__name__)


def ffmpeg_binary() -> str:
    s = load_settings()
    return s.ffmpeg_path or shutil.which("ffmpeg") or "ffmpeg"


def ffprobe_binary() -> str:
    s = load_settings()
    return s.ffprobe_path or shutil.which("ffprobe") or "ffprobe"


def available() -> bool:
    """True when the configured (or PATH) ffmpeg binary exists and is executable."""
    binary = ffmpeg_binary()
    if Path(binary).is_absolute():
        return Path(binary).is_file()
    return shutil.which(binary) is not None


def make_clip(src: Path, out: Path, start: float, end: float) -> Path:
    """Extract a 9:16, 1080x1920 H.264/AAC 30fps clip from ``src``.

    The filter scales to fit 1080x1920, pads any letterbox to solid black,
    forces 30 fps, and re-encodes audio to AAC. Audio is drawn into the
    center of the portrait canvas using a smart crop.
    """
    settings = load_settings()
    duration = max(0.1, end - start)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    width, height, fps = settings.clip_width, settings.clip_height, settings.clip_fps
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"crop={width}:{height},fps={fps}"
    )
    cmd = [
        ffmpeg_binary(),
        "-y",
        "-ss", f"{start:.3f}",
        "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "medium",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-movflags", "+faststart",
        str(out),
    ]
    _run(cmd)
    if not out.exists():
        raise RuntimeError(f"ffmpeg did not produce {out}")
    log.info("Rendered clip %s (%.1fs)", out.name, duration)
    return out


def probe_duration(path: Path) -> float:
    cmd = [
        ffprobe_binary(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1500:]}")