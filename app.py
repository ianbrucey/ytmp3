#!/usr/bin/env python3
"""
YT Audio Library — paste a link, get a searchable local music library.

Run:  python app.py   ->   http://127.0.0.1:8765
Needs: pip install flask yt-dlp   and   ffmpeg on PATH (brew install ffmpeg)
"""

import queue
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import yt_dlp
from flask import Flask, Response, jsonify, request, send_from_directory

# --- config ---------------------------------------------------------------
DOWNLOAD_DIR = Path.home() / "Music_Downloads"      # change this if you like
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DOWNLOAD_DIR / "library.db"
HOST, PORT = "127.0.0.1", 8765                      # 8765 avoids macOS AirPlay on 5000

app = Flask(__name__)

# --- database ---------------------------------------------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS songs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id    TEXT UNIQUE,
        title       TEXT,
        artist      TEXT,
        description TEXT DEFAULT '',
        tags        TEXT DEFAULT '',
        duration    INTEGER DEFAULT 0,
        filename    TEXT,
        url         TEXT,
        uploader    TEXT DEFAULT '',
        added_at    TEXT)""")
    con.commit()
    con.close()

init_db()

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

# --- helpers ---------------------------------------------------------------
def guess_artist(info):
    for key in ("artist", "creator", "album_artist"):   # present on music uploads
        val = (info.get(key) or "").strip()
        if val:
            return val
    parts = re.split(r"\s+[-–—]\s+", (info.get("title") or ""), maxsplit=1)
    if len(parts) == 2 and 0 < len(parts[0].strip()) <= 60:
        return parts[0].strip()                          # "Artist - Track" title
    return (info.get("uploader") or "Unknown artist").strip()

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|\x00-\x1F]', "_", name).strip().rstrip(".")[:150] or "untitled"

def unique_path(stem):
    candidate, n = DOWNLOAD_DIR / f"{stem}.mp3", 2
    while candidate.exists():
        candidate = DOWNLOAD_DIR / f"{stem} ({n}).mp3"
        n += 1
    return candidate

# --- download worker (one at a time, in the background) ---------------------
JOBS = {}
_jobs_lock = threading.Lock()
_queue = queue.Queue()

def on_progress(job, d):
    if d.get("status") == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        done = d.get("downloaded_bytes") or 0
        job["percent"] = round(100 * done / total, 1) if total else None
        job["status"] = "downloading"
    elif d.get("status") == "finished":
        job["percent"], job["status"] = 100.0, "processing"

def finalize(info):
    """Rename <id>.mp3 -> 'Artist - Title.mp3', remove stray thumbnail files."""
    raw = DOWNLOAD_DIR / f"{info['id']}.mp3"
    if not raw.exists():
        raise RuntimeError("Expected audio file not found after download")
    dest = unique_path(sanitize(f"{guess_artist(info)} - {info.get('title') or info['id']}"))
    raw.rename(dest)
    for stray in DOWNLOAD_DIR.glob(f"{info['id']}.*"):
        stray.unlink()
    return dest.name

def add_song(info, filename):
    row = (
        info.get("id"),
        (info.get("title") or "").strip(),
        guess_artist(info),
        (info.get("description") or "").strip(),
        ", ".join(t for t in (info.get("tags") or []) if t),
        int(info.get("duration") or 0),
        filename,
        info.get("webpage_url") or info.get("original_url") or "",
        (info.get("uploader") or "").strip(),
        time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    con = db()
    try:
        old = con.execute("SELECT filename FROM songs WHERE video_id=?", (row[0],)).fetchone()
        con.execute("""INSERT INTO songs (video_id, title, artist, description, tags,
                       duration, filename, url, uploader, added_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(video_id) DO UPDATE SET
                         title=excluded.title, artist=excluded.artist,
                         description=excluded.description, tags=excluded.tags,
                         duration=excluded.duration, filename=excluded.filename,
                         url=excluded.url, uploader=excluded.uploader,
                         added_at=excluded.added_at""", row)
        con.commit()
    finally:
        con.close()
    if old and old["filename"] != filename:              # re-download: drop stale file
        stale = DOWNLOAD_DIR / old["filename"]
        if stale.exists():
            stale.unlink()

def run_download(job):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "quiet": True, "no_warnings": True,
        "noplaylist": True, "playlist_items": "1",
        "extractor_args": {"youtube": {"player_client": ["android"]}},
        "default_search": "ytsearch",          # lets you paste a search phrase too
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": True},
        ],
        "progress_hooks": [lambda d: on_progress(job, d)],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(job["url"], download=True)
        if info is None:
            raise RuntimeError("No result found")
        entries = info.get("entries")
        if entries:
            info = next(e for e in entries if e)
        filename = finalize(info)
        add_song(info, filename)
        job.update(status="done", title=info.get("title") or filename)
    except Exception as exc:
        job.update(status="error", error=str(exc)[:400])

def worker():
    while True:
        job = _queue.get()
        try:
            run_download(job)
        finally:
            _queue.task_done()

threading.Thread(target=worker, daemon=True).start()

# --- api --------------------------------------------------------------------
@app.post("/api/download")
def submit():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    with _jobs_lock:
        for job in JOBS.values():
            if job["url"] == url and job["status"] in ("queued", "downloading", "processing"):
                return jsonify({"job": job, "duplicate": True})
        job = {"id": uuid.uuid4().hex[:8], "url": url, "status": "queued",
               "percent": None, "title": None, "error": None, "created": time.time()}
        JOBS[job["id"]] = job
        finished = sorted((j for j in JOBS.values() if j["status"] in ("done", "error")),
                          key=lambda j: j["created"])
        for j in finished[:-30]:
            JOBS.pop(j["id"], None)
    _queue.put(job)
    return jsonify({"job": job})

@app.get("/api/jobs")
def jobs():
    with _jobs_lock:
        active = [j for j in JOBS.values() if j["status"] in ("queued", "downloading", "processing")]
        recent = sorted((j for j in JOBS.values() if j["status"] in ("done", "error")),
                        key=lambda j: j["created"])[-8:][::-1]
    return jsonify({"active": active, "recent": recent})

@app.get("/api/songs")
def songs():
    q = (request.args.get("q") or "").strip()
    con = db()
    try:
        if q:
            like = f"%{q}%"
            rows = con.execute("""SELECT * FROM songs WHERE title LIKE ? OR artist LIKE ?
                                  OR description LIKE ? OR tags LIKE ?
                                  ORDER BY added_at DESC LIMIT 500""",
                               (like, like, like, like)).fetchall()
        else:
            rows = con.execute("SELECT * FROM songs ORDER BY added_at DESC LIMIT 500").fetchall()
    finally:
        con.close()
    return jsonify({"songs": [dict(r) for r in rows]})

@app.post("/api/songs/<int:song_id>/delete")
def delete_song(song_id):
    con = db()
    try:
        row = con.execute("SELECT filename FROM songs WHERE id=?", (song_id,)).fetchone()
        if row is None:
            return jsonify({"error": "Not found"}), 404
        con.execute("DELETE FROM songs WHERE id=?", (song_id,))
        con.commit()
    finally:
        con.close()
    if row["filename"]:
        target = (DOWNLOAD_DIR / row["filename"]).resolve()
        if target.is_relative_to(DOWNLOAD_DIR) and target.exists():
            target.unlink()
    return jsonify({"ok": True})

@app.get("/audio/<path:filename>")
def audio(filename):
    return send_from_directory(DOWNLOAD_DIR, filename)

# --- ui ---------------------------------------------------------------------
PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>YT Audio Library</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#262b36; --text:#e8eaf0;
          --muted:#8b93a7; --accent:#4f8cff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }
  .wrap { max-width:860px; margin:0 auto; padding:28px 20px 80px; }
  h1 { font-size:20px; margin:0 0 18px; }
  h1 span { color:var(--muted); font-weight:400; font-size:13px; margin-left:8px; }
  .dl { display:flex; gap:10px; }
  input[type=text] { flex:1; background:var(--panel); border:1px solid var(--line);
    color:var(--text); border-radius:8px; padding:11px 14px; font-size:15px; outline:none; }
  input[type=text]:focus { border-color:var(--accent); }
  button { background:var(--accent); border:0; color:#fff; border-radius:8px;
    padding:11px 18px; font-size:15px; cursor:pointer; }
  button:hover { filter:brightness(1.1); }
  button.ghost { background:transparent; border:1px solid var(--line);
    color:var(--muted); padding:6px 12px; font-size:13px; }
  button.ghost:hover { color:var(--text); border-color:var(--accent); }
  .jobs { margin-top:14px; display:flex; flex-direction:column; gap:8px; }
  .job { background:var(--panel); border:1px solid var(--line); border-radius:8px;
    padding:10px 14px; font-size:14px; }
  .job .bar { height:4px; background:var(--line); border-radius:2px; margin-top:6px; overflow:hidden; }
  .job .bar i { display:block; height:100%; background:var(--accent); transition:width .3s; }
  .job.error { color:#ff8a8a; }  .job.done { color:#7bd88f; }
  .search { margin:26px 0 14px; width:100%; }
  .muted { color:var(--muted); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; margin-bottom:10px; }
  .row { display:flex; align-items:baseline; gap:10px; }
  .row .title { font-weight:600; }  .row .artist { color:var(--accent); }
  .row .dur { margin-left:auto; color:var(--muted); font-size:13px; }
  .chips { margin-top:6px; display:flex; flex-wrap:wrap; gap:6px; }
  .chip { background:#1f2430; border:1px solid var(--line); color:var(--muted);
    border-radius:20px; padding:1px 10px; font-size:12px; }
  details { margin-top:6px; font-size:13px; color:var(--muted); }
  summary { cursor:pointer; }
  .actions { margin-top:10px; display:flex; gap:8px; align-items:center; }
  audio { flex:1; height:32px; }
  .empty { text-align:center; color:var(--muted); padding:40px 0; }
</style></head><body>
<div class="wrap">
  <h1>YT Audio Library <span>&rarr; ~/Music_Downloads</span></h1>
  <form id="dlform" class="dl">
    <input id="url" type="text" autocomplete="off"
      placeholder="Paste a YouTube link (or just type a song name to search for)...">
    <button type="submit">Download</button>
  </form>
  <div id="jobs" class="jobs"></div>
  <input id="q" class="search" type="text" placeholder="Search title, artist, description, tags...">
  <div id="list"></div>
</div>
<script>
const $ = (s) => document.querySelector(s);
const esc = (s) => (s ?? "").toString().replace(/[&<>"']/g,
  (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" }[c]));
const fmt = (s) => s ? Math.floor(s/60) + ":" + String(Math.floor(s%60)).padStart(2,"0") : "-:--";

let player = new Audio(), lastBtn = null, pollTimer = null, prevActive = 0;

$("#dlform").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#url").value.trim();
  if (!url) return;
  $("#url").value = "";
  const res = await fetch("/api/download", { method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({ url }) });
  const data = await res.json();
  if (!res.ok) alert(data.error || "Something went wrong");
  pollJobs();
});

async function pollJobs() {
  const { active, recent } = await (await fetch("/api/jobs")).json();
  $("#jobs").innerHTML = [...active, ...recent].map(jobHtml).join("");
  if (prevActive > 0 && active.length === 0) loadSongs($("#q").value.trim());
  prevActive = active.length;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollJobs, active.length ? 1200 : 10000);
}

function jobHtml(j) {
  const label = esc(j.title || j.url);
  if (j.status === "error") return `<div class="job error">Failed: ${label} - ${esc(j.error || "unknown error")}</div>`;
  if (j.status === "done")  return `<div class="job done">Added: ${label}</div>`;
  const sub = j.status === "processing" ? "converting..."
            : (j.percent != null ? j.percent + "%" : "starting...");
  const w = j.percent ?? (j.status === "processing" ? 100 : 4);
  return `<div class="job">Downloading: ${label} <span class="muted">${sub}</span>
          <div class="bar"><i style="width:${w}%"></i></div></div>`;
}

async function loadSongs(q = "") {
  const { songs } = await (await fetch("/api/songs?q=" + encodeURIComponent(q))).json();
  const list = $("#list");
  list.innerHTML = songs.length ? songs.map(songHtml).join("")
    : `<div class="empty">${q ? "No matches." : "Nothing yet - paste a link above."}</div>`;
}

function songHtml(s) {
  const chips = (s.tags || "").split(",").map(t => t.trim()).filter(Boolean).slice(0, 8)
    .map(t => `<span class="chip">${esc(t)}</span>`).join("");
  const desc = s.description
    ? `<details><summary>Description</summary>${esc(s.description.slice(0, 600))}</details>` : "";
  return `<div class="card">
    <div class="row">
      <span class="title">${esc(s.title)}</span>
      <span class="artist">${esc(s.artist)}</span>
      <span class="dur">${fmt(s.duration)}</span>
    </div>
    ${chips ? `<div class="chips">${chips}</div>` : ""}${desc}
    <div class="actions">
      <button class="ghost" onclick="play(this)" data-fn="${esc(s.filename)}">Play</button>
      <button class="ghost" onclick="removeSong(${s.id})">Delete</button>
      <span class="muted" style="font-size:12px">added ${esc(s.added_at)}</span>
    </div></div>`;
}

function play(btn) {
  const fn = btn.dataset.fn;
  if (lastBtn && lastBtn !== btn) lastBtn.textContent = "Play";
  if (player.dataset.fn === fn && !player.paused) {
    player.pause(); btn.textContent = "Play"; return;
  }
  player.pause();
  player = new Audio("/audio/" + encodeURIComponent(fn));
  player.dataset.fn = fn;
  player.play();
  btn.textContent = "Pause";
  player.onended = () => btn.textContent = "Play";
  lastBtn = btn;
}

async function removeSong(id) {
  if (!confirm("Remove from library and delete the audio file?")) return;
  await fetch(`/api/songs/${id}/delete`, { method: "POST" });
  loadSongs($("#q").value.trim());
}

let debounce = null;
$("#q").addEventListener("input", (e) => {
  clearTimeout(debounce);
  debounce = setTimeout(() => loadSongs(e.target.value.trim()), 250);
});

loadSongs();
pollJobs();
</script></body></html>"""

@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")

if __name__ == "__main__":
    print(f"Library folder : {DOWNLOAD_DIR}")
    print(f"Open in browser: http://{HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
