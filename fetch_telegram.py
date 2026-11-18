#!/usr/bin/env python3
"""Download audio from the @muzrec1 Telegram channel (two modes).

Mode "page" (no token needed) - voice previews only:
  The public channel page exposes VOICE previews as direct CDN URLs:
      <audio class="tgme_widget_message_voice" src="<cdn ogg URL>">
  Fetch the page with a SHORT User-Agent ('Mozilla/5.0'; a full Chrome
  UA gets the JS shell with no content), then download each voice file
  with curl + browser headers (Chrome UA + Referer). urllib gets
  HTTP 500 from the Telegram CDN; curl works.

  Full songs are NOT downloadable from the public page: in the channel
  page they are DOCUMENT blocks whose only link is the message URL
  (https://t.me/muzrec1/<id>), which returns HTML, not audio.

Mode "botapi" (token needed, server-side only):
  Full songs ARE downloadable through the Bot API, but only with a
  file_id. The bot captures file_ids when songs are sent to it. This
  mode reads the token from the TELEGRAM_BOT_TOKEN env var (or the
  .env file) at runtime and NEVER prints it, and downloads each
  file_id via getFile -> api.telegram.org/file/...

Usage:
  # voice previews from the public page (no token)
  python3 fetch_telegram.py --mode page --channel muzrec1 --before 274 --out ./music --max 11
  # full songs via Bot API (needs a JSON list of {post_id, file_id})
  python3 fetch_telegram.py --mode botapi --file-ids ./file_ids.json --out ./music
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request

SHORT_UA = "Mozilla/5.0"
CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
ENV_PATH = os.path.expanduser("~/workspace/telegram-voice-bot/.env")


def load_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("TELEGRAM_BOT_TOKEN not found (env var or .env)")


def fetch_page(channel, before):
    url = f"https://t.me/s/{channel}"
    if before:
        url += f"?before={before}"
    out = subprocess.run(
        ["curl", "-s", "-A", SHORT_UA, url],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"failed to fetch channel page {url}")
    return out.stdout


def parse_voices(html):
    """Return [(post_id, ogg_url)] for voice previews.

    Splits the page by data-post markers so each URL is paired with its
    own message id.
    """
    chunks = re.split(r'data-post="[^"/]+/(\d+)"', html)
    voices = []
    for i in range(1, len(chunks) - 1, 2):
        post_id = chunks[i]
        chunk = chunks[i + 1]
        m = re.search(r'<audio[^>]*src="([^"]+)"', chunk)
        if m:
            voices.append((post_id, m.group(1)))
    return voices


def curl_download(url, dest, referer):
    out = subprocess.run(
        [
            "curl", "-s", "-L",
            "-A", CHROME_UA,
            "-H", f"Referer: {referer}",
            "-o", dest, url,
        ],
        capture_output=True, text=True, timeout=600,
    )
    if out.returncode != 0:
        raise RuntimeError(f"curl failed for {dest}")
    return os.path.getsize(dest)


def mode_page(args):
    os.makedirs(args.out, exist_ok=True)
    before = args.before
    downloaded = 0
    seen = set()
    for _ in range(args.pages):
        if downloaded >= args.max:
            break
        html = fetch_page(args.channel, before)
        voices = parse_voices(html)
        if not voices:
            print("no more voices on this page")
            break
        before = min(int(pid) for pid, _ in voices)
        for post_id, url in voices:
            if downloaded >= args.max:
                break
            if post_id in seen:
                continue
            seen.add(post_id)
            dest = os.path.join(args.out, f"voice-{post_id}.ogg")
            if os.path.exists(dest) and os.path.getsize(dest) > 0:
                print(f"post {post_id}: already have {os.path.basename(dest)}")
                downloaded += 1
                continue
            size = curl_download(url, dest, f"https://t.me/s/{args.channel}")
            print(f"post {post_id}: downloaded {size} bytes -> {os.path.basename(dest)}")
            downloaded += 1
    print(f"done: {downloaded} voice preview(s) in {args.out}")


def bot_api(method, token, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode() if params else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def mode_botapi(args):
    with open(args.file_ids) as f:
        entries = json.load(f)
    token = load_token()
    os.makedirs(args.out, exist_ok=True)
    downloaded = 0
    for entry in entries:
        post_id = entry.get("post_id", "?")
        file_id = entry["file_id"]
        res = bot_api("getFile", token, {"file_id": file_id})
        if not res.get("ok"):
            print(f"post {post_id}: getFile failed")
            continue
        file_path = res["result"]["file_path"]
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "bin"
        dest = os.path.join(args.out, f"{post_id}.{ext}")
        file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        req = urllib.request.Request(file_url, headers={"User-Agent": CHROME_UA})
        with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as out:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                out.write(chunk)
        size = os.path.getsize(dest)
        print(f"post {post_id}: downloaded {size} bytes -> {os.path.basename(dest)}")
        downloaded += 1
    print(f"done: {downloaded} file(s) in {args.out}")


def main():
    ap = argparse.ArgumentParser(description="Download audio from a Telegram channel")
    ap.add_argument("--mode", choices=["page", "botapi"], default="page")
    ap.add_argument("--channel", default="muzrec1")
    ap.add_argument("--before", type=int, default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max", type=int, default=11)
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--file-ids", default=None, help="JSON list of {post_id, file_id} (botapi mode)")
    args = ap.parse_args()
    if args.mode == "page":
        mode_page(args)
    else:
        if not args.file_ids:
            ap.error("--mode botapi requires --file-ids")
        mode_botapi(args)


if __name__ == "__main__":
    main()
