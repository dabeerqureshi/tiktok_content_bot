# R&D Notes — tiktok_content_bot

Verified against primary sources (2026) while scoping this project.

## TikTok Content Posting API (Direct Post)

- **FILE_UPLOAD is the right path for a laptop.** Flow: `POST /v2/post/publish/video/init/` → `{publish_id, upload_url}` → `PUT` the file to `upload_url` with a `Content-Range` header → poll `POST /v2/post/publish/status/fetch/`. No public server needed.
- **Audit restriction (critical).** *"All content posted by unaudited clients will be restricted to private viewing mode."* Public posting requires app audit + approved `video.publish` scope. Default `privacy_level=SELF_ONLY` while testing.
- **5-minute max video duration** per post (`max_video_post_duration_sec: 300` in Query Creator Info).
- **Supported format:** MP4 + H.264 (we render 1080x1920, 9:16, 30 fps, AAC).
- **Not re-verified during research:** exact chunk-size range (reported 5–64 MB, final chunk can be larger), 4 GB max file, and the "≤5 pending shares / 24h" limit. These live on the Upload Media Transfer Guide / FAQ pages (URLs behind bot-protection). Design never uploads ahead of schedule, so this doesn't change the architecture.
- **2026 addition:** Content Posting API now also supports photos — future headroom, not needed now.

## Ollama

- **Structured outputs are native.** Pass a JSON Schema in the `format` field of `/api/generate` and `/api/chat`; model returns schema-conforming output. JSON mode = `format: "json"`.
- **Python lib:** `client.chat(model=..., format=<schema>, ...)` works; pass a Pydantic `model_json_schema()`. We additionally validate with a `TypeAdapter`.

## faster-whisper

- Segment-level (and word-level) timestamps map directly to the video timeline → transcript-driven clip selection. `vad_filter=True` reduces hallucinated/music segments.

## yt-dlp (YouTube)

- **YouTube is actively enforcing Proof-of-Origin (PO) tokens**; some formats return HTTP 403 and there is ongoing account/IP churn. Keep every yt-dlp call in `services/youtube.py` + `services/downloader.py` and **update yt-dlp frequently via pip**. Add a `cookiefile` if needed.
- Installed version on this machine was `2023.12.30` (very stale) — update to `>=2026.8.19`.
- Only process videos you are authorized to download and republish.

## Local environment (2026 scan)

| Component | Status | Action |
|---|---|---|
| Python | 3.11.7 (default) + 3.13.5, pip 26.2.1 | use 3.11/3.12 |
| yt-dlp | 2023.12.30 (stale) | `pip install -U yt-dlp` |
| FFmpeg | not installed | `winget install Gyan.FFmpeg` |
| Ollama | not installed | install + `ollama pull llama3.1:8b` |
| faster-whisper / pydantic | not installed | `pip install -r requirements.txt` |

## Operational design decisions

- **SQLite WAL** with `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`; single writer via `BEGIN IMMEDIATE`.
- **Idempotency:** `videos.youtube_id UNIQUE`; `posts.tiktok_publish_id` persisted before/after each API stage so a crash can't double-post.
- **Crash recovery:** on startup, reset any in-flight rows (e.g. `PROCESSING → READY/DOWNLOADED/RETRY`).
- **Queue-driven scheduling:** clips are never pre-uploaded; posts are scheduled to evenly-spaced slots (24h / posts_per_day) and only published when `scheduled_at <= now`.
- **Retry/backoff:** `posts.attempts`, `RETRY → FAILED` at `tiktok_max_attempts`.
- **Monitoring:** errors/alerts via SMTP only for critical events + daily report; silent no-op when SMTP unset.
- **Disk safety:** `disk_high_water_mb` / `disk_critical_mb` thresholds to stop downloads and alert.

## Rollout

1. **Phase 0** — provision env (FFmpeg, Ollama, `pip install -r requirements.txt`), copy `.env.example` → `.env`.
2. **Phase 1** — TikTok **private** path: create TikTok developer app, get `video.publish` token, add a source + a test clip, verify upload on a private account.
3. **Phase 2** — full source → clip pipeline (yt-dlp → whisper → Ollama → FFmpeg).
4. **Phase 3** — scheduling + crash recovery + daily report.
5. **Phase 4** — TikTok app audit for public posting + Windows Task Scheduler autostart.
