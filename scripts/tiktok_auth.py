"""One-time TikTok OAuth helper.

Runs a tiny local web server, opens the TikTok authorize URL in your browser,
catches the redirect (via the ngrok tunnel -> localhost), exchanges the code
for tokens, and prints the values to paste into .env.

Usage:
    python scripts/tiktok_auth.py --redirect-uri https://xxxx.ngrok-free.app/auth/tiktok/callback

Steps:
    1. Start ngrok:  ngrok http 8080
    2. Add <ngrok-url>/auth/tiktok/callback as a Redirect URI in the TikTok portal.
    3. Fill TIKTOK_CLIENT_KEY / TIKTOK_CLIENT_SECRET in .env.
    4. Run this script with --redirect-uri pointing at the ngrok callback URL.
    5. Log in as your sandbox target user in the browser window that opens.
"""

from __future__ import annotations

import sys
import threading
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import argparse  # noqa: E402

import requests  # noqa: E402
from dotenv import set_key  # noqa: E402

from config import BASE_DIR, load_settings  # noqa: E402

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
ENV_FILE = BASE_DIR / ".env"

result: dict = {"code": None, "state": None}


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path.rstrip("/") == "/auth/tiktok/callback":
            result["code"] = (params.get("code") or [None])[0]
            result["state"] = (params.get("state") or [None])[0]
            err = (params.get("error") or [None])[0]
            msg = err or "Authorization received. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<h1>{msg}</h1>".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args) -> None:  # silence default logging
        pass


def exchange_code(code: str, redirect_uri: str) -> dict:
    s = load_settings()
    resp = requests.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_key": s.tiktok_client_key,
            "client_secret": s.tiktok_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        timeout=30,
    )
    resp.raise_for_status()
    payload = resp.json()
    if isinstance(payload.get("error"), str) and payload.get("error"):
        raise SystemExit(f"Token exchange failed: {payload}")
    data = payload.get("data") or payload  # TikTok returns flat or nested
    if not data.get("access_token"):
        raise SystemExit(
            f"Unexpected token response:\n{payload}\n\n"
            "Common causes:\n"
            "- The auth code was already consumed (re-run the script for a fresh one)\n"
            "- Wrong client_key/client_secret in .env\n"
            "- redirect_uri does not EXACTLY match what's registered in the portal\n"
            "- Sandbox app: you must log in as the added target user"
        )
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="TikTok OAuth token helper")
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Local port to listen on - must match the port ngrok forwards to (default: 8080)",
    )
    parser.add_argument(
        "--redirect-uri",
        required=True,
        help="Must exactly match a Redirect URI registered in the TikTok portal",
    )
    args = parser.parse_args()

    s = load_settings()
    if not s.tiktok_client_key or not s.tiktok_client_secret:
        raise SystemExit("Set TIKTOK_CLIENT_KEY and TIKTOK_CLIENT_SECRET in .env first.")

    state = uuid.uuid4().hex
    qs = urllib.parse.urlencode({
        "client_key": s.tiktok_client_key,
        "scope": "user.info.basic,video.publish,video.upload",
        "response_type": "code",
        "redirect_uri": args.redirect_uri,
        "state": state,
    })
    authorize_url = f"{AUTH_URL}?{qs}"

    server = HTTPServer(("127.0.0.1", args.port), CallbackHandler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("Opening browser for TikTok authorization...")
    print(authorize_url)
    webbrowser.open(authorize_url)

    print(f"Waiting for callback on http://127.0.0.1:{args.port} ...")
    for _ in range(120):  # give up after ~2 min of no request
        if result["code"]:
            break
        server.timeout = 1
        server.handle_request()
    server.server_close()

    if not result["code"]:
        raise SystemExit("No authorization code received (timeout).")
    if result["state"] != state:
        raise SystemExit("State mismatch - possible CSRF; aborting.")

    data = exchange_code(result["code"], args.redirect_uri)

    print(f"\n=== Paste these into {ENV_FILE} ===\n")
    print(f"TIKTOK_ACCESS_TOKEN={data.get('access_token')}")
    print(f"TIKTOK_REFRESH_TOKEN={data.get('refresh_token')}")
    print(f"TIKTOK_OPEN_ID={data.get('open_id')}")
    print(f"\nAccess token expires_in: {data.get('expires_in')}s, "
          f"refresh expires_in: {data.get('refresh_expires_in')}s")

    try:
        for k, v in (
            ("TIKTOK_ACCESS_TOKEN", data.get("access_token")),
            ("TIKTOK_REFRESH_TOKEN", data.get("refresh_token")),
            ("TIKTOK_OPEN_ID", data.get("open_id")),
        ):
            if v:
                set_key(str(ENV_FILE), k, v)
        print(f"\nAlso written directly to {ENV_FILE}")
    except FileNotFoundError:
        print(f"\nNote: {ENV_FILE} not found - copy .env.example to .env and paste manually.")


if __name__ == "__main__":
    main()
