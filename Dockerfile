# DJ BEHIX Radio — 4 stations (Railway)
# Downloads each station's tracks from YouTube at build time, then streams them.
#
# NOTE: the track-download layer runs BEFORE the server file is copied,
# so editing radio_server_multi.py alone does NOT re-download the music
# on the next deploy (Docker layer cache stays valid for the music).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir yt-dlp
WORKDIR /app
COPY afro-tracks.txt nostalgia-tracks.txt pop-tracks.txt gym-tracks.txt ./
ENV PORT=8000 BITRATE=128k
ENV MUSIC_DIR_AFRO=/app/music-afro MUSIC_DIR_NOSTALGIA=/app/music-nostalgia MUSIC_DIR_POP=/app/music-pop MUSIC_DIR_GYM=/app/music-gym
RUN mkdir -p /app/music-afro /app/music-nostalgia /app/music-pop /app/music-gym && \
  while IFS='|' read -r nn artist title; do \
    dest="/app/music-afro/$nn - $artist - $title.mp3"; \
    [ -s "$dest" ] && continue; \
    yt-dlp -x --audio-format mp3 --audio-quality 128K -o "$dest" "ytsearch1:$artist $title Afro House" || echo "FAILED afro: $nn $artist - $title"; \
  done < afro-tracks.txt; \
  while IFS='|' read -r nn artist title; do \
    dest="/app/music-nostalgia/$nn - $artist - $title.mp3"; \
    [ -s "$dest" ] && continue; \
    yt-dlp -x --audio-format mp3 --audio-quality 128K -o "$dest" "ytsearch1:$artist $title" || echo "FAILED nostalgia: $nn $artist - $title"; \
  done < nostalgia-tracks.txt; \
  while IFS='|' read -r nn artist title; do \
    dest="/app/music-pop/$nn - $artist - $title.mp3"; \
    [ -s "$dest" ] && continue; \
    yt-dlp -x --audio-format mp3 --audio-quality 128K -o "$dest" "ytsearch1:$artist $title" || echo "FAILED pop: $nn $artist - $title"; \
  done < pop-tracks.txt; \
  while IFS='|' read -r nn artist title; do \
    dest="/app/music-gym/$nn - $artist - $title.mp3"; \
    [ -s "$dest" ] && continue; \
    yt-dlp -x --audio-format mp3 --audio-quality 128K -o "$dest" "ytsearch1:$artist $title" || echo "FAILED gym: $nn $artist - $title"; \
  done < gym-tracks.txt; \
  echo "afro: $(ls /app/music-afro/*.mp3 2>/dev/null | wc -l) nostalgia: $(ls /app/music-nostalgia/*.mp3 2>/dev/null | wc -l) pop: $(ls /app/music-pop/*.mp3 2>/dev/null | wc -l) gym: $(ls /app/music-gym/*.mp3 2>/dev/null | wc -l)"
COPY radio_server_multi.py ./radio_server.py
EXPOSE 8000
CMD ["python3", "radio_server.py"]
