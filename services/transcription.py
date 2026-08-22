"""faster-whisper transcription.

Returns a list of segments (``start``/``end`` in seconds + text) that map
directly onto the video timeline, so Ollama can pick strong segments by time.

The model is loaded once per (model, device, compute_type) combination and
reused for every call - loading faster-whisper weights is by far the most
expensive part of a transcription, so this cache matters at 3 posts/day.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_model_lock = threading.Lock()
_model_cache: dict[tuple[str, str, str], Any] = {}


def _get_model():
    """Load and cache the WhisperModel for the configured settings."""
    from config import load_settings  # noqa: PLC0415

    settings = load_settings()
    key = (settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
    with _model_lock:
        if key not in _model_cache:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            log.info("Loading whisper model %s (%s/%s)...", *key)
            _model_cache[key] = WhisperModel(
                key[0], device=key[1], compute_type=key[2]
            )
    return _model_cache[key]


def transcribe(path: Path, word_timestamps: bool = False) -> list[dict[str, Any]]:
    model = _get_model()
    segments, info = model.transcribe(str(path), vad_filter=True,
                                      word_timestamps=word_timestamps)
    result: list[dict[str, Any]] = []
    for seg in segments:
        entry: dict[str, Any] = {
            "start": seg.start, "end": seg.end, "text": seg.text.strip()
        }
        if word_timestamps and seg.words:
            entry["words"] = [
                {"start": w.start, "end": w.end, "word": w.word} for w in seg.words
            ]
        result.append(entry)
    log.info("Transcribed %s (%.0fs audio) -> %d segments",
             path.name, info.duration or 0, len(result))
    return result


def transcript_text(segments: list[dict[str, Any]]) -> str:
    return "\n".join(f"[{s['start']:.1f}-{s['end']:.1f}] {s['text']}" for s in segments)