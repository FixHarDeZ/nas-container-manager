#!/usr/bin/env python3
"""One-time YouTube consent: prints the refresh token for the vault.

Run on your workstation (it opens a browser); stdlib only, no pip install.

    python3 scripts/youtube_auth.py <client_id> <client_secret>

Before running, in https://console.cloud.google.com:
  1. Create a project, then enable "YouTube Data API v3".
  2. OAuth consent screen: External, and set publishing status to
     **In production**. Left in "Testing", refresh tokens expire after 7 days
     and the bot dies silently a week later. Unverified is fine for personal
     use — you click through the warning once as the app's own owner.
  3. Credentials -> Create credentials -> OAuth client ID -> **Desktop app**.
     Pass the client id and secret to this script.

Paste the printed token into the vault, under the path for the channel you
just authorised — one channel per Locale (docs/adr/0008), and writing the
English channel's token over the Thai path silently redirects every Thai
upload:

    make edit-vault
    #  Thai channel:    stacks.shorts_factory.youtube.{client_id,client_secret,refresh_token}
    #  English channel: stacks.shorts_factory.youtube_en.{client_id,client_secret,refresh_token}

Then map the new keys in shorts-factory/secrets.manifest.yaml (the English
channel's env names are YOUTUBE_EN_CLIENT_ID / _CLIENT_SECRET / _REFRESH_TOKEN;
they are deliberately absent until the vault has the values, because
render_env.py refuses to build any .env while a manifest points at a missing
vault path), and deploy:

    make secrets && ./scripts/deploy.sh -s shorts-factory -y

Sign in as the account that owns the channel you are authorising: Google grants
the token to whichever channel the consent screen was completed for.
"""
from __future__ import annotations

import http.server
import json
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
# upload: publish the clip. force-ssl: attach a caption track.
# yt-analytics.readonly: read how past clips performed.
SCOPE = " ".join([
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
])
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"

received: dict[str, str] = {}
answered = threading.Event()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        received.update(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
        answered.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = "code" in received
        self.wfile.write(
            ("<h2>กลับไปที่เทอร์มินัลได้เลย</h2>" if ok else "<h2>ไม่ได้รับ code</h2>").encode()
        )

    def log_message(self, *args):
        pass


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    client_id, client_secret = sys.argv[1], sys.argv[2]
    state = secrets.token_urlsafe(16)

    server = http.server.HTTPServer(("localhost", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    params = {
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        # offline + consent is what actually returns a refresh token; without
        # prompt=consent Google omits it on every authorisation after the first.
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    print("เปิดเบราว์เซอร์ให้แล้ว ถ้าไม่ขึ้น เปิดลิงก์นี้เอง:\n", url, "\n")
    webbrowser.open(url)

    print("รออนุญาตในเบราว์เซอร์...", flush=True)
    if not answered.wait(timeout=600):
        print("รอเกิน 10 นาที — ยกเลิก")
        return 1
    server.shutdown()

    if "error" in received:
        print("ไม่สำเร็จ:", received["error"])
        return 1
    if received.get("state") != state:
        print("state ไม่ตรง — ยกเลิกเพื่อความปลอดภัย")
        return 1

    body = urllib.parse.urlencode({
        "code": received["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=body)) as reply:
        tokens = json.load(reply)

    if "refresh_token" not in tokens:
        print("ไม่ได้ refresh_token กลับมา — เพิกถอนสิทธิ์แอปที่ "
              "https://myaccount.google.com/permissions แล้วรันใหม่")
        return 1

    print("\nrefresh_token:\n" + tokens["refresh_token"])
    print("\nเอาไปใส่ vault ที่ stacks.shorts_factory.youtube.refresh_token")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
