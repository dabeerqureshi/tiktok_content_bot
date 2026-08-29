# TikTok Content Bot

A self-contained Python bot that takes **your own videos from a local folder** and
posts them to TikTok on a fixed daily schedule — 3 times a day at 04:00, 12:00 and
20:00 (configurable) — while a SQLite database guarantees each video is uploaded
**exactly once**. When the folder is exhausted it emails you to add more.

This is the **folder uploader** mode: no YouTube, no transcription, no local LLM.
You provide the videos and the constant post title; the bot handles the rest,
including auto-refreshing your TikTok access token.

## How it works

```
content/  --(new files hashed & registered; dedup by SHA-256)-->  SQLite
   |                                                        |
   `-- schedule: 3 uploads/day at 04:00 / 12:00 / 20:00         v
                                                          TikTok Content Posting API
                                                          (Direct Post, FILE_UPLOAD)
                                                          init -> chunked upload -> poll
                                                            |
                                            on failure: retry w/ exp. backoff (3x)
                                            on success: mark UPLOADED (publish_id saved)
                                            folder empty: email "add more videos"
```

### Guarantees

- **No duplicate uploads** — every file is keyed by a SHA-256 *content* hash, so
  renaming or re-copying a file can never post it twice.
- **Exactly 3 posts/day** — a per-day slot budget enforces this; a successful
  retry consumes the slot a normal post would have taken, so you never exceed it.
- **Crash-safe** — `publish_id` is persisted *before* the file is uploaded, so a
  restart after TikTok init either resolves the real outcome (via a status poll)
  or safely re-queues. Nothing is double-posted or lost.
- **Self-healing auth** — sandbox access tokens expire after ~24h; the bot
  auto-refreshes using your refresh token (~365 days) and re-saves it to `.env`.

## Quick start

1. Install **Python 3.11 or 3.12** (the code uses modern type syntax; macOS ships
   3.9 — use Homebrew: `brew install python@3.12`).
2. Create a virtualenv and install deps:
   ```bash
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/pip install -U tiktok-openapi  # (placeholder; only `requests` needed)
   ```
   > In practice `pip install -r requirements.txt` is enough; the line above is
   > not required. You can ignore it.
3. Configure everything from `.env.example`:
   ```bash
   cp .env.example .env     # then edit .env and fill in your values
   ```
4. Drop your videos into `content/` (any of `.mp4 .mov .m4v .mkv`).
5. Get your TikTok tokens (one-time):
   ```bash
   python scripts/tiktok_auth.py --redirect-uri https://<your-ngrok-url>/auth/tiktok/callback
   ```
   Paste the printed `TIKTOK_ACCESS_TOKEN` / `TIKTOK_REFRESH_TOKEN` / `TIKTOK_OPEN_ID`
   into `.env` (the script can write them for you).

   > You already connected your account to the app in the TikTok sandbox — this
   > step exchanges that connection for the API tokens the bot actually uses.
6. Run it (foreground, for testing):
   ```bash
   .venv/bin/python app.py
   ```
## .env reference

| Key | Required | Default | Description |
|---|---|---|---|
| `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` | yes | — | Your sandbox app credentials from the TikTok developer portal. |
| `TIKTOK_ACCESS_TOKEN` | yes | — | User access token (`video.publish`, `video.upload`). |
| `TIKTOK_REFRESH_TOKEN` | yes | — | Used to mint a new access token every ~24h (auto). |
| `TIKTOK_OPEN_ID` | yes | — | The authorizing user's open id. |
| `TIKTOK_TITLE` | yes | — | **Constant title** used for every post. |
| `TIKTOK_PRIVACY` | no | `SELF_ONLY` | Must stay `SELF_ONLY` until your app is audited. |
| `TIKTOK_CHUNK_MB` | no | `50` | Upload chunk size (TikTok: 5–64 MB). |
| `TIKTOK_MAX_ATTEMPTS` | no | `3` | Retries before a video is marked FAILED. |
| `RETRY_BACKOFF_BASE_SECONDS` | no | `600` | Backoff = base × 2^(attempt-1). |
| `TIKTOK_PENDING_CAP` | no | `5` | TikTok caps ~5 pending uploads/24h. |
| `UPLOAD_TIMES` | no | `04:00,12:00,20:00` | Daily post times (local, HH:MM). |
| `PICK_ORDER` | no | `random` | How to choose the next video: `random\|oldest\|newest\|name`. |
| `MOVE_UPLOADED` | no | `false` | Move files to `content/uploaded/` after success. |
| `SIMULATE` | no | `false` | Dry-run: pick + mark only, no TikTok calls. |
| `CONTENT_DIR` | no | `content` | Where to drop your videos. |
| `SCHEDULER_POLL_SECONDS` | no | `60` | How often the bot wakes up. |
| `DAILY_REPORT_HOUR` | no | `20` | Local hour for the daily email digest. |
| `SMTP_*` | yes¹ | — | SMTP credentials for alerts. Required for the "all uploaded" email. |

¹ SMTP is only required if you want email alerts and the all-uploaded report.

## Run (production)

**Foreground** (good for observing during setup):
```bash
.venv/bin/python app.py
```

**Background / autostart on macOS** (recommended so 4am uploads survive reboot):
```bash
# 1. Point the plist at your venv + repo path (edit scripts/tiktok_content_bot.plist)
# 2. Install and start the service:
sudo cp scripts/tiktok_content_bot.plist /Library/LaunchDaemons/
sudo launchctl load -w /Library/LaunchDaemons/com.tiktokbot.content.plist
# Logs: logs/daemon.log  AND  logs/app.log (rotating)
```

Stop it: `sudo launchctl unload -w /Library/LaunchDaemons/com.tiktokbot.content.plist`.


## CLI

```bash
python scripts/manage.py status            # config + counts + next scheduled slot
python scripts/manage.py list              # table of every video and its status
python scripts/manage.py requeue-failed    # retry videos that gave up (FAILED -> PENDING)
python scripts/manage.py run-once --simulate --cycles 5   # test the loop without TikTok
python scripts/smoke_test.py               # automated self-test (simulate mode)
```

Statuses a video can be in: `PENDING` → `UPLOADING` → `UPLOADED` (success) or
`FAILED` (after `TIKTOK_MAX_ATTEMPTS`). `list`/`status` show the breakdown.

## The daily schedule (exactly 3 posts/day)

At each 60-second cycle the bot computes:

```
slots_today   = count(UPLOAD_TIMES whose time <= now)          # 0..3
already_done  = videos uploaded today
in_flight     = videos currently UPLOADING
capacity      = slots_today - already_done - in_flight  (clamped to 0..1)
```

If `capacity > 0` and a `PENDING` video exists, **one** is uploaded. This means:

- At 04:00 the 1st slot opens → 1 upload; 12:00 → 2nd; 20:00 → 3rd.
- If the Mac slept through 04:00 and 12:00, the next cycle (on wake) uploads one,
  then the cycle after that uploads the next, until caught up — never more than
  3 in a day.
- A retry that succeeds counts as "already_done", so it occupies the slot a normal
  post would have taken. The daily cap is a hard ceiling.

Example: `UPLOAD_TIMES=04:00,12:00,20:00` yields 3 uploads/day.

## Duplicate prevention

Every file dropped into `content/` is SHA-256 hashed on first register and stored
with a `UNIQUE` constraint on `content_hash`. Consequences:

- Same file re-added under a different name → already in the DB, skipped.
- Two copies of the same file → registered once.
- A renamed file whose content changed → new hash → treated as a new video.

## Emails

| When | Subject |
|---|---|
| **All videos uploaded** (folder empty of pending work) | `All videos uploaded ✅` — telling you the count and to add more to the folder. Sent **once** per "batch"; re-arms automatically when you drop new files in. |
| **Stock low** (≤ 2 videos left to post) | `Stock running low ⚠️` |
| **Upload failed permanently** (after max retries) | `TikTok upload failed` (via `email.alert`) |
| **Health check failing** (bad tokens / missing folder / low disk) | `Health check failing` |
| **Daily digest** at `DAILY_REPORT_HOUR` | `Daily TikTok Bot Report` (only if SMTP configured) |

The bot silently skips email when SMTP is unset, so it never crashes on a
misconfigured mail server.

## Sandbox reality (important)

- While your app is **unaudited**, every post is **private** (`privacy_level=SELF_ONLY`)
  — only you can see them on your account. Public posts require TikTok app audit +
  an approved `video.publish` scope.
- Access tokens last ~24h in the sandbox; the bot refreshes them automatically (see
  self-healing auth above). Refresh tokens last ~365 days.
- Respect your daily quota: TikTok limits the number of pending uploads. The bot
  also enforces `TIKTOK_PENDING_CAP` (default 5) — 3/day is safely under it.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "TikTok not configured; publish worker idle" | Fill `TIKTOK_CLIENT_KEY` + `TIKTOK_REFRESH_TOKEN` (+ access token) in `.env`. |
| Uploads fail with `access_token_invalid` | Run `python scripts/tiktok_auth.py` to re-authorize; the bot will also auto-refresh, but a revoked token needs re-auth. |
| Token refresh failing | Same — re-run the auth script. The bot logs the failure and emails you. |
| No emails | Configure `SMTP_*`. Test with `run-once --simulate`; emails are skipped in simulate. |
| Stuck UPLOADING after a crash | On restart the bot polls TikTok's status endpoint with the stored `publish_id` and resolves it — no manual work needed. |
| Want to preview the loop without posting | Set `SIMULATE=true` and run `python scripts/manage.py run-once --simulate --cycles 5`. |


