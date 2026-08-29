"""TikTok Content Posting API (Direct Post via FILE_UPLOAD).

Flow (verified against TikTok docs, Aug 2026):
  1. init_video_upload()   -> publish_id + upload_url
  2. upload_file()         -> PUT the local file in 5-64 MB chunks
  3. fetch_status()        -> poll until PUBLISH_SUCCEED / SEND_TO_USER_INBOX / FAILED

Production notes:
- Unaudited clients are restricted to PRIVATE posts (SELF_ONLY) until audit.
- Transient HTTP failures (429/5xx/timeouts) are retried with backoff.
- refresh_access_token() exchanges TIKTOK_REFRESH_TOKEN for a new access token
  when the current one expires (access tokens last 24h in the sandbox). New
  tokens are persisted to .env (never logged).
- Token refresh uses a form-encoded body (OAuth spec); the auth_retry=False
  flag prevents infinite recursion if the refresh itself is rejected.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

import requests
from dotenv import set_key

from config import BASE_DIR, load_settings

log = logging.getLogger(__name__)

ENV_FILE = BASE_DIR / ".env"
_persist_lock = threading.Lock()


def _persist_tokens(access: str, refresh: str, open_id: str | None) -> None:
    """Write renewed tokens back to .env (never logged). Thread-safe."""
    with _persist_lock:
        try:
            set_key(str(ENV_FILE), "TIKTOK_ACCESS_TOKEN", access)
            if refresh:
                set_key(str(ENV_FILE), "TIKTOK_REFRESH_TOKEN", refresh)
            if open_id:
                set_key(str(ENV_FILE), "TIKTOK_OPEN_ID", open_id)
        except OSError as exc:
            log.error("Could not persist TikTok tokens to .env: %s "
                      "(in-memory token still valid for this run)", exc)


INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
CREATOR_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class TikTokError(RuntimeError):
    pass


class TikTokService:
    def __init__(self) -> None:
        s = load_settings()
        self.access_token = s.tiktok_access_token
        self.open_id = s.tiktok_open_id
        self.privacy = s.tiktok_privacy or "SELF_ONLY"
        self.chunk_mb = min(64, max(5, s.tiktok_chunk_mb or 50))

    def _headers(self, json_body: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if json_body:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        return headers

    def health_check(self) -> bool:
        s = load_settings()
        # Access tokens expire (24h in sandbox) but are auto-refreshed from the
        # refresh token, so a refresh token alone is sufficient to be "ready".
        return bool(s.tiktok_client_key and s.tiktok_refresh_token)

    def _request(self, method: str, url: str, *, auth_retry: bool = True, **kwargs) -> requests.Response:
        """POST/PUT/GET with retry + exponential backoff on transient errors.

        ``auth_retry=False`` disables the automatic token refresh so the OAuth
        token endpoint can be called without risk of infinite recursion.
        """
        last_exc: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.request(method, url, timeout=60, **kwargs)
                if (
                    auth_retry
                    and attempt < MAX_RETRIES
                    and resp.status_code == 401
                    and self.refresh_access_token()
                ):
                    # Token renewed (and persisted); retry with fresh headers.
                    if kwargs.get("headers"):
                        kwargs["headers"]["Authorization"] = f"Bearer {self.access_token}"
                    continue
                if resp.status_code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    log.warning("TikTok %s -> %s, retrying in %ss", url, resp.status_code, wait)
                    time.sleep(wait)
                    continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF_SECONDS * attempt
                    log.warning("TikTok request error (%s), retrying in %ss", exc, wait)
                    time.sleep(wait)
        raise TikTokError(f"TikTok unreachable after {MAX_RETRIES} attempts: {last_exc}")

    def query_creator_info(self) -> dict:
        resp = self._request("POST", CREATOR_URL, headers=self._headers(), json={})
        return _check(resp)

    def refresh_access_token(self) -> dict | None:
        """Exchange the refresh token for a new access token (form-encoded body).

        New tokens are persisted to .env so restarts keep working; they are
        never written to logs.
        """
        s = load_settings()
        if not (s.tiktok_client_key and s.tiktok_refresh_token):
            log.warning("TikTok refresh token missing - cannot renew access token")
            return None
        resp = self._request(
            "POST", TOKEN_URL, auth_retry=False, data={
                "client_key": s.tiktok_client_key,
                "client_secret": s.tiktok_client_secret,
                "grant_type": "refresh_token",
                "refresh_token": s.tiktok_refresh_token,
            },
        )
        try:
            data = _check(resp)
        except TikTokError as exc:
            log.error("TikTok token refresh failed: %s "
                      "(re-run scripts/tiktok_auth.py to re-authorize)", exc)
            return None
        data = data.get("data") or data  # API returns flat or nested
        if not (data.get("access_token") and data.get("refresh_token")):
            log.error("TikTok token refresh returned no tokens - "
                      "re-run scripts/tiktok_auth.py to re-authorize")
            return None
        self.access_token = data["access_token"]
        _persist_tokens(
            access=data["access_token"],
            refresh=data.get("refresh_token"),
            open_id=data.get("open_id"),
        )
        log.info("TikTok access token renewed and persisted to .env")
        return data


    def init_video_upload(self, title: str) -> dict:
        body = {
            "post_info": {
                "title": title,
                "privacy_level": self.privacy,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {"source": "FILE_UPLOAD"},
        }
        resp = self._request("POST", INIT_URL, headers=self._headers(), json=body)
        data = _check(resp)
        return {
            "publish_id": data["data"]["publish_id"],
            "upload_url": data["data"]["upload_url"],
        }

    def upload_file(self, upload_url: str, path: Path, chunk_size: int | None = None) -> None:
        """PUT the video in chunks using Content-Range (5-64 MB per chunk)."""
        size_mb = chunk_size or self.chunk_mb
        if size_mb < 5:
            size_mb = 5
        chunk_bytes = size_mb * 1024 * 1024
        total = path.stat().st_size
        uploaded = 0
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_bytes)
                if not chunk:
                    break
                start = uploaded
                end = uploaded + len(chunk) - 1
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Range": f"bytes {start}-{end}/{total}",
                }
                resp = self._request("PUT", upload_url, data=chunk, headers=headers)
                if resp.status_code not in (200, 201, 206):
                    raise TikTokError(
                        f"Upload chunk failed ({resp.status_code}): {resp.text[:500]}"
                    )
                uploaded += len(chunk)
        log.info("Uploaded %s (%d bytes) to TikTok", path.name, total)

    def fetch_status(self, publish_id: str) -> str:
        resp = self._request(
            "POST", STATUS_URL, headers=self._headers(),
            json={"publish_id": publish_id},
        )
        data = _check(resp)
        pub = data.get("data", {})
        status = str(pub.get("status") or "")
        if status in ("PUBLISH_SUCCEED", "PUBLISH_FAILED", "SEND_TO_USER_INBOX", "FAILED"):
            return status
        # No terminal status yet. NB: post_status is a details *object* (not a
        # string) in the status/fetch response, so it must never be stringified
        # as a status. Surface the raw status or UNKNOWN.
        return status or "UNKNOWN"

    def publish(
        self,
        path: Path,
        title: str,
        wait_poll: bool = False,
        max_polls: int = 6,
        poll_interval_s: int = 10,
    ) -> tuple[str, str]:
        """Run init -> upload -> poll. Returns (publish_id, status)."""
        init = self.init_video_upload(title)
        publish_id = init["publish_id"]
        log.info("Initialized upload, publish_id=%s", publish_id)
        self.upload_file(init["upload_url"], path)

        status = "PROCESSING"
        if wait_poll:
            for _ in range(max_polls):
                status = self.fetch_status(publish_id)
                if status in ("PUBLISH_SUCCEED", "PUBLISH_FAILED", "FAILED",
                              "SEND_TO_USER_INBOX"):
                    break
                time.sleep(poll_interval_s)
        return publish_id, status


def _check(resp: requests.Response) -> dict:
    """Validate a TikTok JSON response.

    Handles both shapes the API uses:
    - nested (REST endpoints): {"error": {"code": "...", "message": "..."}}
    - flat (OAuth endpoints):   {"error": "invalid_request",
                                 "error_description": "..."}
    """
    try:
        payload = resp.json()
    except ValueError:
        raise TikTokError(f"TikTok API non-JSON response ({resp.status_code})") from None
    err = payload.get("error", {}) or {}
    if isinstance(err, str):  # flat OAuth-style error
        if err in ("", "ok", "0"):
            return payload
        raise TikTokError(
            f"TikTok API error {err}: {payload.get('error_description', '')} "
            f"({resp.status_code})"
        )
    if err.get("code") in (None, "ok", "", "0"):
        return payload
    raise TikTokError(
        f"TikTok API error {err.get('code')}: {err.get('message')} ({resp.status_code})"
    )

