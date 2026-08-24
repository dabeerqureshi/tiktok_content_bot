"""Transcription -> AI analysis -> FFmpeg clip worker.

Turns a DOWNLOADED video into READY clips:
    DOWNLOADED -> TRANSCRIBING -> ANALYZING -> PROCESSING -> COMPLETED
Each suggestion becomes a clips row (CREATED) and then stays CREATED until
the publish worker schedules it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from config import load_settings
from database.db import db
from services import ffmpeg, ollama
from services.ollama import analyze_transcript
from services.transcription import transcribe, transcript_text
from .base import Worker

log = logging.getLogger(__name__)


class ClipWorker(Worker):
    name = "clip_worker"

    def run_once(self) -> None:
        if not ffmpeg.available():
            log.warning("ffmpeg not available; clip worker idle")
            return
        rows = db.query(
            "SELECT * FROM videos WHERE status IN ('DOWNLOADED','TRANSCRIBING','ANALYZING','PROCESSING') "
            "ORDER BY id LIMIT 1"
        )
        for row in rows:
            try:
                self._process(row)
            except Exception as exc:  # noqa: BLE001 - never kill the scheduler
                self._fail(row, exc)

    def _fail(self, row, exc: Exception) -> None:
        settings = load_settings()
        attempts = (row["attempts"] or 0) + 1
        status = "FAILED" if attempts >= settings.tiktok_max_attempts else "DOWNLOADED"
        log.exception("Processing failed for %s (attempt %d)", row["youtube_id"], attempts)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE videos SET status=?, last_error=?, attempts=? WHERE id=?",
                (status, str(exc), attempts, row["id"]),
            )
        db.insert_event("ERROR", self.name, f"{row['youtube_id']}: {exc}")

    def _process(self, row) -> None:
        settings = load_settings()
        video_id, youtube_id = row["id"], row["youtube_id"]

        # Fail fast when the source original was pruned by cleanup.
        src_file = Path(row["file_path"]) if row["file_path"] else None
        if not (src_file and src_file.exists()):
            raise FileNotFoundError(
                f"source video missing for {youtube_id}: {row['file_path']}"
            )

        # 1. Transcribe
        with db.transaction() as conn:
            conn.execute(
                "UPDATE videos SET status='TRANSCRIBING' WHERE id=?", (video_id,)
            )
        transcript_path = settings.data_dir / f"transcript_{youtube_id}.json"
        if not Path(row["transcript_path"] or "").exists():
            segments = transcribe(Path(row["file_path"]))
            transcript_path.write_text(
                json.dumps(segments), encoding="utf-8"
            )
            with db.transaction() as conn:
                # Guard against duplicate transcript rows if a previous run
                # crashed between writing the file and updating the video row.
                already = conn.execute(
                    "SELECT 1 FROM transcripts WHERE video_id=?", (video_id,)
                ).fetchone()
                if not already:
                    conn.execute(
                        "INSERT INTO transcripts (video_id, json_path) VALUES (?, ?)",
                        (video_id, str(transcript_path)),
                    )
                conn.execute(
                    "UPDATE videos SET status='ANALYZING', transcript_path=? WHERE id=?",
                    (str(transcript_path), video_id),
                )
        else:
            transcript_path = Path(row["transcript_path"])

        # 2. Analyze + render clips
        with db.transaction() as conn:
            conn.execute("UPDATE videos SET status='ANALYZING' WHERE id=?", (video_id,))
        segments = json.loads(transcript_path.read_text(encoding="utf-8"))
        text = transcript_text(segments)
        if not ollama.available():
            raise RuntimeError(f"Ollama not reachable at {settings.ollama_host}")
        suggestions = analyze_transcript(text, row["title"] or "", row["duration"] or 0)

        if not suggestions:
            log.warning("No clip suggestions for video %s", youtube_id)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE videos SET status='COMPLETED', processed_at=datetime('now') WHERE id=?",
                    (video_id,),
                )
            return

        with db.transaction() as conn:
            conn.execute("UPDATE videos SET status='PROCESSING' WHERE id=?", (video_id,))
        self._render_all(row, segments, suggestions)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE videos SET status='COMPLETED', processed_at=datetime('now') WHERE id=?",
                (video_id,),
            )

    def _render_all(self, row, segments, suggestions) -> None:
        settings = load_settings()
        src = Path(row["file_path"])
        video_id = row["id"]
        done = 0
        for i, sug in enumerate(suggestions, start=1):
            start, end = max(0.0, sug["start"]), sug["end"]
            duration = end - start
            min_s, max_s = settings.clip_duration_bounds
            if duration < min_s or duration > max_s:
                log.info("Skipping clip %s: duration %.1f out of bounds", i, duration)
                continue
            out = settings.clips_dir / f"{row['youtube_id']}_clip_{i:03d}.mp4"
            try:
                ffmpeg.make_clip(src, out, start, end)
            except Exception as exc:  # noqa: BLE001
                log.exception("FFmpeg clip %s failed", i)
                db.insert_event("ERROR", self.name, f"{row['youtube_id']} clip {i}: {exc}")
                continue
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO clips "
                    "(video_id, file_path, start_time, end_time, duration, title, caption, "
                    " hashtags, score, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'READY')",
                    (
                        video_id,
                        str(out),
                        start,
                        end,
                        duration,
                        sug["title"],
                        sug["caption"],
                        json.dumps(sug.get("hashtags", [])),
                        sug.get("score", 5),
                    ),
                )
            done += 1
        log.info("Rendered %d clip(s) for %s", done, row["youtube_id"])