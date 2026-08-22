# tiktok_content_bot

A local, 24/7 content bot that turns authorized YouTube videos into short-form
TikTok clips and publishes them on a schedule - built as a single Python
application (no n8n).

```
YouTube (yt-dlp) -> faster-whisper -> Ollama (clip analysis) -> FFmpeg -> SQLite -> TikTok API -> SMTP alerts
```

## Architecture

- **SQLite (WAL)** coordinates everything: `sources`, `videos`, `transcripts`,
  `clips`, `posts`, `jobs`, `system_events`. State machines + crash recovery built in.
- **Workers** run on a scheduler loop: `VideoWorker` (discover + download),
  `ClipWorker` (transcribe -> analyze -> render), `PublishWorker` (schedule + publish).

```
app.py
|-- config.py
|-- database/  (db, models, migrations)
|-- services/  (youtube, downloader, ffmpeg, transcription, ollama, tiktok, email, scheduler)
|-- workers/   (video, clip, publish)
|-- storage/   (originals, clips, failed, temp)
|-- data/content.db
`-- logs/app.log
```

## Setup (Phase 0)

1. Install Python 3.11/3.12.
2. Install FFmpeg (`winget install Gyan.FFmpeg`) so `ffmpeg`/`ffprobe` are on PATH.
3. Install Ollama and pull a model: `ollama pull llama3.1:8b`.
4. Install Python deps:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   pip install -U yt-dlp
   ```
5. Configure: `copy .env.example .env`, then fill in TikTok / SMTP / Ollama values.
6. Seed an approved source:
   ```powershell
   sqlite3 data/content.db "INSERT INTO sources (name, channel_url) VALUES ('My Channel', 'https://www.youtube.com/@example');"
   ```

## Run

```powershell
python app.py
```

On startup the app migrates the schema, recovers abandoned in-flight jobs,
runs health checks (ffmpeg / yt-dlp / Ollama / TikTok / disk space), then starts
the scheduler. Ctrl+C shuts down gracefully.

## TikTok publishing status

The TikTok Content Posting API restricts **unaudited** clients to **private**
posts (`SELF_ONLY`). Complete Phase 1 on a private test account, then apply for
app audit + `video.publish` approval before expecting public automated publishing.
See `docs/RESEARCH.md` for verified API details and the rollout plan.

## Management CLI

```powershell
python scripts/manage.py add-source "My Channel" https://www.youtube.com/@example
python scripts/manage.py sources          # list configured sources
python scripts/manage.py health           # dependency health checks
python scripts/manage.py stats            # queue/publishing statistics
python scripts/manage.py reset-failed     # requeue failed work
python scripts/manage.py cleanup          # retention cleanup now
python scripts/manage.py run-once         # single worker cycle (no loop)
```

## Production safeguards

- Crash recovery on startup (in-flight jobs requeued); UNIQUE(youtube_id) prevents double ingestion; publish_id persisted before upload prevents double posting.
- Exponential backoff on failed posts (base * 2^(n-1)), bounded attempts, TikTok pending-upload cap enforced.
- Disk safety: downloads pause below DISK_CRITICAL_MB, retention cleanup at DISK_HIGH_WATER_MB; originals deleted once every clip is posted; published clips pruned after CLIPS_KEEP_DAYS.
- Hourly health checks + daily email report (only if SMTP configured).
- faster-whisper model cached across calls; Ollama invalid-JSON retried with corrective prompt.
