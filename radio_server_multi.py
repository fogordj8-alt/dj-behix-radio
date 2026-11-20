#!/usr/bin/env python3
"""DJ BEHIX Radio - multi-station endless live stream server.

Four shared live broadcasts (one per station), each from its own folder:
  * Tracks play back-to-back in shuffled order, reshuffled every cycle.
  * A cycle never repeats the previous order. The stream never ends.
  * A listener who joins late joins mid-song, like a radio.
  * Every track is transcoded live to MP3 128k so mixed sources form
    one seamless byte stream.

Stations (name -> music dir, from STATIONS env or defaults):
  afro       /app/music-afro
  nostalgia  /app/music-nostalgia
  pop        /app/music-pop
  gym        /app/music-gym

Endpoints:
  GET /stream/<station>       the endless MP3 stream for that station
  GET /nowplaying/<station>   JSON: current track, elapsed, duration, listeners
  GET /status                 JSON: per-station + overall download progress
  GET /stream                 alias for /stream/afro (back-compat)
  GET /nowplaying             alias for /nowplaying/afro (back-compat)
  GET /health                 "ok"

Tracks are downloaded at RUNTIME by a background thread per station
(yt-dlp from the same track lists), NOT at build time: Railway kills
builds after ~20 min, far too short for 2,247 tracks. The build only
installs tools and copies code, so it finishes in ~1-2 min. Newly
downloaded tracks join the rotation automatically because each
broadcaster re-scans its music dir on every pick.

Config (env vars, all optional):
  MUSIC_DIR_AFRO, MUSIC_DIR_NOSTALGIA, MUSIC_DIR_POP, MUSIC_DIR_GYM
  PORT, HOST, BITRATE
"""
import json
import os
import queue
import random
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".opus", ".m4a", ".flac", ".wav", ".aac"}
CHUNK = 8192
QUEUE_MAX = 128

STATIONS = {
    "afro": os.environ.get("MUSIC_DIR_AFRO", "/app/music-afro"),
    "nostalgia": os.environ.get("MUSIC_DIR_NOSTALGIA", "/app/music-nostalgia"),
    "pop": os.environ.get("MUSIC_DIR_POP", "/app/music-pop"),
    "gym": os.environ.get("MUSIC_DIR_GYM", "/app/music-gym"),
}

# Optional filter: STATIONS=afro runs only the Afro station (one Zerops
# service per station). Comma-separated; default is all stations.
_wanted = os.environ.get("STATIONS")
if _wanted:
    _keep = {s.strip() for s in _wanted.split(",") if s.strip() in STATIONS}
    if _keep:
        STATIONS = {name: d for name, d in STATIONS.items() if name in _keep}

# Hyphenated station ids some clients use -> canonical single-word id.
ALIASES = {
    "afro-house": "afro",
    "pop-irani": "pop",
    "nostalgia-irani": "nostalgia",
    "nostalgia-ir": "nostalgia",
    "gym-archive": "gym",
}

# Track lists (copied into the image) -> (list file, search suffix).
TRACKS_DIR = os.environ.get("TRACKS_DIR", "/app")
TRACK_LISTS = {
    "afro": ("afro-tracks.txt", "Afro House"),
    "nostalgia": ("nostalgia-tracks.txt", ""),
    "pop": ("pop-tracks.txt", ""),
    "gym": ("gym-tracks.txt", ""),
}

# Optional per-station list override, e.g. LIST_FILE_NOSTALGIA=nostalgia-b-tracks.txt
# (lets one station's list be split across two services).
for _name in list(TRACK_LISTS):
    _override = os.environ.get("LIST_FILE_" + _name.upper())
    if _override:
        TRACK_LISTS[_name] = (_override, TRACK_LISTS[_name][1])

DOWNLOAD_PROGRESS = {
    name: {"total": 0, "downloaded": 0, "failed": 0, "current": None}
    for name in STATIONS
}


def download_station(name, music_dir, list_file, suffix, initial_delay=0):
    """Background thread: fetch every missing track of one station.

    Skips files that already exist (so restarts and volume persistence
    resume instead of re-downloading). One bad track never blocks the
    queue: it is logged as FAILED and the loop moves on. After a full
    pass, the thread waits and retries the missing tracks: YouTube
    rate-limits bursts of searches, so failed tracks usually succeed
    on a later pass once the block lifts. Downloads are paced
    (DOWNLOAD_DELAY_SECONDS between tracks, default 8s) and the four
    stations start staggered so the first burst does not trip the
    rate limit again.
    """
    if initial_delay:
        time.sleep(initial_delay)
    prog = DOWNLOAD_PROGRESS[name]
    path = os.path.join(TRACKS_DIR, list_file)
    if not os.path.isfile(path):
        log(f"[{name}] downloader: list file {path} not found, skipping")
        return
    os.makedirs(music_dir, exist_ok=True)
    entries = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or "|" not in line:
                continue
            nn, artist, title = line.split("|", 2)
            entries.append((nn.strip(), artist.strip(), title.strip()))
    prog["total"] = len(entries)
    delay = float(os.environ.get("DOWNLOAD_DELAY_SECONDS", "8"))
    retry_wait = float(os.environ.get("DOWNLOAD_RETRY_SECONDS", "600"))
    while True:
        prog["downloaded"] = 0
        prog["failed"] = 0
        for nn, artist, title in entries:
            dest = os.path.join(music_dir, f"{nn} - {artist} - {title}.mp3")
            if os.path.isfile(dest) and os.path.getsize(dest) > 0:
                prog["downloaded"] += 1
                continue
            query = f"{artist} {title} {suffix}".strip()
            prog["current"] = f"{artist} - {title}"
            ok = False
            err = ""
            # Source chain: YouTube via the android player client
            # first (it bypasses the "Sign in to confirm you're not
            # a bot" block that hits datacenter IPs), then YouTube
            # web client, then SoundCloud as a last resort (some
            # tracks are DRM-blocked or missing there).
            # Nostalgia must be ORIGINAL versions only (no remixes) — user rule.
            # Pop may include remixes. Afro/Gym are DJ music (remixes normal).
            allow_remix = name != "nostalgia"
            if allow_remix:
                sources = [
                    (["--extractor-args", "youtube:player_client=android"],
                     f"ytsearch1:{query}"),
                    ([], f"ytsearch1:{query}"),
                    ([], f"scsearch1:{query}"),
                ]
            else:
                # No-remix: search returns 5 candidates and yt-dlp skips
                # any whose title contains "remix", so the first
                # non-remix (original) result is downloaded instead.
                sources = [
                    (["--extractor-args", "youtube:player_client=android",
                      "--reject-title", "(?i)remix"],
                     f"ytsearch5:{query}"),
                    (["--reject-title", "(?i)remix"],
                     f"ytsearch5:{query}"),
                    (["--reject-title", "(?i)remix"],
                     f"scsearch5:{query}"),
                ]
            for extra_args, search in sources:
                if ok:
                    break
                for _attempt in (1, 2):
                    try:
                        r = subprocess.run(
                            ["yt-dlp", "-x", "--audio-format", "mp3",
                             "--audio-quality", "128K", "-o", dest,
                             "--match-filter", "duration < 720", *extra_args, search],
                            capture_output=True, text=True, timeout=300,
                        )
                        if r.returncode == 0 and os.path.isfile(dest) and os.path.getsize(dest) > 0:
                            dur = probe_duration(dest); ok = False if dur is not None and dur > 720 else True; os.remove(dest) if not ok else None
                            break
                        if r.stderr:
                            lines = [l for l in r.stderr.strip().splitlines() if l.strip()]
                            err = lines[-1][:200] if lines else ""
                    except Exception as e:
                        err = str(e)[:200]
                    try:
                        os.remove(dest)
                    except OSError:
                        pass
                    time.sleep(2)
            if ok:
                prog["downloaded"] += 1
                log(f"[{name}] downloaded {nn}: {artist} - {title} "
                    f"({prog['downloaded']}/{prog['total']})")
            else:
                prog["failed"] += 1
                log(f"[{name}] FAILED {nn}: {artist} - {title} {err}")
            prog["current"] = None
            time.sleep(delay)
        log(f"[{name}] downloader: pass done {prog['downloaded']}/{prog['total']} "
            f"({prog['failed']} failed)")
        if prog["downloaded"] >= prog["total"]:
            # List complete; re-check hourly in case the list grows.
            time.sleep(3600)
        else:
            time.sleep(retry_wait)


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def list_tracks(music_dirs):
    tracks = []
    for d in music_dirs:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if os.path.isfile(path) and os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                tracks.append(path)
    return tracks


class Broadcaster:
    def __init__(self, name, music_dirs, bitrate="128k", seed=None):
        self.name = name
        self.music_dirs = music_dirs
        self.bitrate = bitrate
        self.rng = random.Random(seed)
        self.clients = set()
        self.lock = threading.Lock()
        self.current = {"track": None, "started_at": None, "duration": None}
        self.track_count = 0
        self._stop = threading.Event()

    def register(self):
        q = queue.Queue(maxsize=QUEUE_MAX)
        with self.lock:
            self.clients.add(q)
        return q

    def unregister(self, q):
        with self.lock:
            self.clients.discard(q)

    def listener_count(self):
        with self.lock:
            return len(self.clients)

    def _broadcast(self, data):
        dead = []
        with self.lock:
            targets = list(self.clients)
        for q in targets:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.append(q)
        for q in dead:
            self.unregister(q)

    def _play_track(self, path):
        name = os.path.basename(path)
        duration = probe_duration(path)
        self.current = {
            "track": name,
            "started_at": time.time(),
            "duration": duration,
        }
        dur = f"{duration:.1f}s" if duration else "?"
        log(f"[{self.name}] NOW PLAYING: {name} ({dur})")
        cmd = [
            "ffmpeg", "-re", "-v", "error", "-i", path,
            "-vn", "-ac", "2", "-ar", "44100", "-b:a", self.bitrate,
            "-f", "mp3", "-",
        ]
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        try:
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                self._broadcast(chunk)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    def run(self):
        log(f"[{self.name}] broadcaster started (dir: {', '.join(self.music_dirs)})")
        prev_path = None
        while not self._stop.is_set():
            tracks = list_tracks(self.music_dirs)
            if not tracks:
                log(f"[{self.name}] WARN: no audio files, retrying in 5s...")
                time.sleep(5)
                continue
            self.track_count = len(tracks)
            # Pick one random track at a time (never the same twice in a row),
            # so the archive plays in a truly random order, never sequentially.
            choices = [t for t in tracks if t != prev_path] or tracks
            path = self.rng.choice(choices)
            prev_path = path
            log(f"[{self.name}] random pick: {os.path.basename(path)} "
                f"(of {len(tracks)} tracks)")
            try:
                self._play_track(path)
            except Exception as e:
                log(f"[{self.name}] ERROR playing {os.path.basename(path)}: {e}")
        log(f"[{self.name}] broadcaster stopped")


def make_handler(broadcasters):
    class Handler(BaseHTTPRequestHandler):
       protocol_version = "HTTP/1.1"
        def log_message(self, fmt, *args):
            pass

        def send_response(self, code, message=None):
            super().send_response(code, message)
            self.send_header("Access-Control-Allow-Origin", "*")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _send_json(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_jsonp(self, payload, callback):
            # JSONP: wraps the payload in a JS function call so a page can
            # load it with a plain <script> tag (no fetch/XHR, no CORS).
            safe = "".join(c for c in callback if c.isalnum() or c in "._")
            if not safe:
                return self._send_json(payload)
            body = f"{safe}({json.dumps(payload)});".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _nowplaying_payload(self, b):
            cur = b.current
            elapsed = time.time() - cur["started_at"] if cur["started_at"] else 0
            return {
                "track": cur["track"],
                "elapsed_seconds": round(elapsed, 1),
                "duration_seconds": cur["duration"],
                "listeners": b.listener_count(),
                "tracks_in_playlist": b.track_count,
            }

        def do_GET(self):
            raw = self.path
            path = raw.split("?", 1)[0].rstrip("/")
            query = raw.split("?", 1)[1] if "?" in raw else ""
            callback = None
            for part in query.split("&"):
                if part.startswith("callback="):
                    callback = part[len("callback="):]
                    break
            if path == "/health":
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/status":
                stations = {}
                total_all = 0
                done_all = 0
                for sname, b in broadcasters.items():
                    p = DOWNLOAD_PROGRESS[sname]
                    stations[sname] = {
                        "total": p["total"],
                        "downloaded": p["downloaded"],
                        "failed": p["failed"],
                        "current": p["current"],
                        "tracks_on_disk": b.track_count,
                    }
                    total_all += p["total"]
                    done_all += p["downloaded"]
                payload = {
                    "stations": stations,
                    "total_tracks": total_all,
                    "downloaded_tracks": done_all,
                    "percent": round(100.0 * done_all / total_all, 1) if total_all else 0,
                }
                if callback:
                    self._send_jsonp(payload, callback)
                else:
                    self._send_json(payload)
                return
            if path in ("/stream", "/nowplaying"):
                # back-compat aliases for Afro House
                path = path + "/afro"
            if path.startswith("/stream/"):
                station = ALIASES.get(path[len("/stream/"):], path[len("/stream/"):])
                b = broadcasters.get(station)
                if b is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self._handle_stream(b)
                return
            if path.startswith("/nowplaying/"):
                station = ALIASES.get(path[len("/nowplaying/"):], path[len("/nowplaying/"):])
                b = broadcasters.get(station)
                if b is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                payload = self._nowplaying_payload(b)
                if callback:
                    self._send_jsonp(payload, callback)
                else:
                    self._send_json(payload)
                return
            if path in ("", "/index.html"):
                body = b"DJ BEHIX Radio - multi-station stream server"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(404)
            self.end_headers()

        def _handle_stream(self, b):
            q = b.register()
            log(f"[{b.name}] client connected ({b.listener_count()} listeners)")
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache")
         self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Now-Playing", str(b.current["track"] or ""))
            self.end_headers()
            try:
                while True:
                    try:
                        chunk = q.get(timeout=1)
                    except queue.Empty:
                        continue
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            finally:
                b.unregister(q)
                log(f"[{b.name}] client disconnected ({b.listener_count()} listeners)")

    return Handler


def main():
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    bitrate = os.environ.get("BITRATE", "128k")
    broadcasters = {}
    for name, music_dir in STATIONS.items():
        b = Broadcaster(name, [music_dir], bitrate=bitrate)
        broadcasters[name] = b
        t = threading.Thread(target=b.run, daemon=True)
        t.start()
    # Runtime downloads: one background thread per station fetches the
    # missing tracks from the track lists. The stream starts at once
    # with whatever is on disk and grows as downloads land. Threads
    # start staggered (20s apart) so the first search burst does not
    # trip YouTube's rate limit.
    for i, (name, music_dir) in enumerate(STATIONS.items()):
        list_file, suffix = TRACK_LISTS[name]
        t = threading.Thread(
            target=download_station,
            args=(name, music_dir, list_file, suffix, i * 20),
            daemon=True,
        )
        t.start()
    server = ThreadingHTTPServer((host, port), make_handler(broadcasters))
    log(f"listening on http://{host}:{port} stations: {', '.join(broadcasters)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for b in broadcasters.values():
            b._stop.set()
        server.shutdown()


if __name__ == "__main__":
    main()
