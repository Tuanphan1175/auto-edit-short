"""Grab: local video downloader (YouTube, TikTok, Facebook, Instagram, ...) built on yt-dlp.

Run:  .venv\\Scripts\\python.exe tools\\grab\\server.py   ->  http://127.0.0.1:8765
Files land in input/ so `python -m autoedit` can pick them up directly.
Binds to localhost only.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import unicodedata
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yt_dlp
from yt_dlp.utils import download_range_func

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "input"
HOST, PORT = "127.0.0.1", 8765
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".wav"}

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def slug(text: str, limit: int = 40) -> str:
    """ASCII, lowercase, '_' separated: safe as an auto-edit project name."""
    text = text.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text[:limit].strip("_") or "video"


def unique_name(base: str, ext: str) -> str:
    name, k = base, 2
    while (OUT / f"{name}.{ext}").exists():
        name, k = f"{base}_{k}", k + 1
    return name


def parse_time(value) -> float | None:
    """'90', '1:30', '00:01:30' -> seconds. Empty -> None."""
    if value in (None, ""):
        return None
    parts = str(value).strip().split(":")
    if not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts) or len(parts) > 3:
        raise ValueError(f"thời gian không hợp lệ: {value}")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def get_info(url: str) -> dict:
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    heights = sorted({f["height"] for f in info.get("formats") or []
                      if f.get("height") and f.get("vcodec") not in (None, "none")}, reverse=True)
    w, h = info.get("width"), info.get("height")
    return {
        "title": info.get("title") or "video",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "platform": info.get("extractor_key") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
        "heights": heights[:8],
        "vertical": bool(w and h and h > w),
        "suggested_name": slug(info.get("title") or "video"),
    }


def run_job(job_id: str, req: dict) -> None:
    job = JOBS[job_id]

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            with LOCK:
                job["progress"] = round(100 * d.get("downloaded_bytes", 0) / total, 1) if total else job["progress"]
                job["speed"] = d.get("_speed_str", "").strip()
                job["eta"] = d.get("_eta_str", "").strip()
        elif d["status"] == "finished":
            with LOCK:
                job["stage"] = "Đang ghép / xử lý…"

    audio = req.get("kind") == "audio"
    ext = "mp3" if audio else "mp4"
    name = unique_name(slug(req.get("name") or "video"), ext)
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": str(OUT / f"{name}.%(ext)s"),
        "progress_hooks": [hook],
    }
    if audio:
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    else:
        hmax = int(req.get("height") or 1080)
        # Prefer H.264/AAC (plays everywhere, ffmpeg-friendly), fall back to anything at that height.
        opts["format"] = (f"bv*[height<={hmax}][vcodec^=avc1]+ba[ext=m4a]/"
                          f"bv*[height<={hmax}]+ba/b[height<={hmax}]/b")
        opts["merge_output_format"] = "mp4"
    start, end = parse_time(req.get("start")), parse_time(req.get("end"))
    if start is not None or end is not None:
        opts["download_ranges"] = download_range_func(None, [(start or 0, end or float("inf"))])
        opts["force_keyframes_at_cuts"] = True
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([req["url"]])
        with LOCK:
            job.update(status="done", progress=100, stage="Xong", file=f"{name}.{ext}")
    except Exception as exc:  # report any yt-dlp / ffmpeg failure to the UI
        with LOCK:
            job.update(status="error", stage="Lỗi", error=re.sub(r"\x1b\[[0-9;]*m", "", str(exc))[:400])


def list_files() -> list[dict]:
    files = [p for p in OUT.iterdir() if p.suffix.lower() in MEDIA_EXT]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "size": p.stat().st_size, "stem": p.stem} for p in files]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the console quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/files":
            return self._json(list_files())
        if path.startswith("/api/job/"):
            with LOCK:
                job = dict(JOBS.get(path.rsplit("/", 1)[-1]) or {})
            return self._json(job or {"error": "not found"}, 200 if job else 404)
        if path.startswith("/file/"):
            target = (OUT / unquote(path[6:])).resolve()
            if target.parent != OUT.resolve() or not target.is_file():
                return self._json({"error": "not found"}, 404)
            ctype = "audio/mpeg" if target.suffix == ".mp3" else "video/mp4"
            return self._send(200, target.read_bytes(), ctype)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            req = self._body()
            url = str(req.get("url", "")).strip()
            if not re.match(r"^https?://", url):
                return self._json({"error": "Link phải bắt đầu bằng http:// hoặc https://"}, 400)
            if path == "/api/info":
                return self._json(get_info(url))
            if path == "/api/download":
                parse_time(req.get("start")), parse_time(req.get("end"))  # validate early
                job_id = uuid.uuid4().hex[:10]
                with LOCK:
                    JOBS[job_id] = {"id": job_id, "status": "running", "progress": 0, "stage": "Đang tải…",
                                    "title": req.get("title") or url, "speed": "", "eta": ""}
                threading.Thread(target=run_job, args=(job_id, {**req, "url": url}), daemon=True).start()
                return self._json({"id": job_id})
        except Exception as exc:
            return self._json({"error": re.sub(r"\x1b\[[0-9;]*m", "", str(exc))[:400]}, 400)
        self._json({"error": "not found"}, 404)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252
    OUT.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Grab chạy tại {url}  (Ctrl+C để tắt) — file lưu vào {OUT}")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
