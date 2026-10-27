# YT Audio Library

Local web app: paste a YouTube link → MP3 lands in `~/Music_Downloads`,
searchable by title / artist / description / tags.

## Run

```bash
source .venv/bin/activate
python app.py
# then open http://127.0.0.1:8765
```

- Requires `ffmpeg` on PATH (`brew install ffmpeg`) — already present on this machine.
- Library database: `~/Music_Downloads/library.db` (SQLite).
- Deps (already installed into `.venv`): `flask`, `yt-dlp`.
  Update the YouTube extractor occasionally with `pip install -U yt-dlp`
  (YouTube changes break old versions).
