# DJ BEHIX Radio — 4 stations (Railway)
# Tracks are downloaded at RUNTIME by the server (background downloader
# threads, one per station), NOT at build time: Railway kills builds
# after ~20 min (DeadlineExceeded), far too short for 2,247 tracks.
# The build only installs tools and copies code, so it finishes in ~1-2 min.
# Attach a Railway volume at /data and set MUSIC_DIR_* to /data/music-*
# so downloaded tracks survive restarts and redeploys.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp
WORKDIR /app
COPY afro-tracks.txt nostalgia-tracks.txt pop-tracks.txt gym-tracks.txt ./
ENV PORT=8000 BITRATE=128k
ENV MUSIC_DIR_AFRO=/app/music-afro MUSIC_DIR_NOSTALGIA=/app/music-nostalgia MUSIC_DIR_POP=/app/music-pop MUSIC_DIR_GYM=/app/music-gym
RUN mkdir -p /app/music-afro /app/music-nostalgia /app/music-pop /app/music-gym
COPY radio_server_multi.py ./radio_server.py
EXPOSE 8000
CMD ["python3", "radio_server.py"]
