"""Studio: local web UI for the whole auto-edit pipeline.

    download (yt-dlp) -> transcribe -> cut list review -> cut -> director (Claude Code) -> build -> QA

Run:  studio.bat   (or .venv\\Scripts\\python.exe tools\\studio\\server.py)  ->  http://127.0.0.1:8765
Binds to localhost only. Pipeline steps run as `python -m autoedit <step> <name>` subprocesses.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def _load_grab():
    """Downloader module (get_info, run_job, JOBS). Loaded by path: both files are named server.py."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("grab_server", HERE.parent / "grab" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


grab = _load_grab()
sys.path.insert(0, str(HERE))
import extras as ex  # noqa: E402
from autoedit import library as lib  # noqa: E402
from autoedit import settings as pst  # noqa: E402
from autoedit import snapgen  # noqa: E402

HOST, PORT = "127.0.0.1", 8765
PY = sys.executable
INPUT, WORK, OUTPUT = ROOT / "input", ROOT / "work", ROOT / "output"
SERVE_ROOTS = (INPUT, WORK, OUTPUT, ROOT / "samples", ROOT / "media")
NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,60}$")
CTYPES = {".mp4": "video/mp4", ".mp3": "audio/mpeg", ".jpg": "image/jpeg", ".png": "image/png",
          ".webm": "video/webm", ".json": "application/json; charset=utf-8", ".jpeg": "image/jpeg",
          ".webp": "image/webp", ".mov": "video/quicktime", ".m4v": "video/mp4", ".mkv": "video/x-matroska",
          ".svg": "image/svg+xml"}

JOBS: dict[str, dict] = {}
ACTIVE: dict[str, str] = {}  # project name -> running job id (one pipeline job per project)
LOCK = threading.Lock()

DIRECTOR_PROMPT = """Bạn là đạo diễn của repo auto-edit-short. Làm Bước 2 trong CLAUDE.md cho video "{name}":
1. Đọc CLAUDE.md (schema edit_plan.json và mục "Bước 2 — Đạo diễn"), work/{name}/transcript.cut.json, config/style.json, danh sách media/logos/.
2. Viết work/{name}/edit_plan.json đúng schema; chữ trên màn hình phải khớp nội dung đang nói.
3. Tự kiểm theo luật plan-check: scene phủ kín [0, duration], fullscreen 20–30%, split 20–30%, ≤ 3 s một thay đổi hình ảnh.
Chỉ ghi file work/{name}/edit_plan.json, không chạy lệnh shell. Trả lời ngắn gọn bằng tiếng Việt khi xong."""
DIRECTOR_FIX = "\n\nplan-check của lần trước báo lỗi, sửa work/{name}/edit_plan.json cho hết lỗi:\n{errors}"
DIRECTOR_RETRIES = 2
DIRECTOR_NOTE = ("\n\nNgười dùng CHƯA ƯNG bản dựng trước và góp ý (bắt buộc làm theo, vẫn giữ luật plan-check):\n{note}")
DIRECTOR_STYLE = ("\n\nPhong cách đã chọn: \"{label}\". {notes}\nTham khảo cách dựng mẫu trong config/styles/{slug}.plan.json "
                  "(nhịp, tỉ lệ layout, loại motion graphic, cách viết head text, chuyển cảnh, zoom) — chỉ học phong cách, "
                  "KHÔNG chép nội dung; chữ phải khớp lời nói của video này.")
BROLL_PROMPT = """Bạn là đạo diễn B-roll của repo auto-edit-short cho video "{name}".
Đọc work/{name}/transcript.cut.json, work/{name}/edit_plan.json và work/{name}/broll_library.json (kho clip có sẵn).
Chọn {n} khoảnh khắc nên chèn video minh hoạ toàn màn hình (B-roll), mỗi đoạn 2–3 giây, gắn với MỘT câu nói cụ thể:
- Chỉ đặt trong scene layout "normal", KHÔNG trùng thời gian head_text, không trong 1.5 giây đầu, cách nhau ≥ 4 giây.
- Nếu kho có clip khớp nội dung (xét tên file/thư mục): đặt "file" đúng chuỗi "file" trong broll_library.json.
- Luôn viết "prompt" tiếng Anh để tạo bằng SnapGen Veo nếu cần: "Vertical 9:16", chuyển động camera chậm (slow push-in),
  "No text, no numbers, no logos, no labels", không mặt người/không người (chỉ tay nếu cần), tránh vật có chữ
  (sổ, lịch, bao bì, màn hình), ánh sáng tự nhiên, hợp nội dung y tế/sức khoẻ nếu video nói về sức khoẻ.
Ghi work/{name}/broll.json dạng {{"items":[{{"id":"b1","t":12.3,"dur":2.5,"line":"câu nói","prompt":"...","file":null,"src_start":0.5,"enabled":true}}]}}.
Giữ nguyên các mục đã có "file" trong work/{name}/broll.json cũ nếu có (chỉ thêm/sửa mục khác).
Chỉ ghi file work/{name}/broll.json, không chạy lệnh shell. Trả lời ngắn gọn bằng tiếng Việt khi xong."""

CUT_PROMPT = """Bạn là biên tập viên cắt của repo auto-edit-short cho video "{name}".
Đọc CLAUDE.md (schema cut_list.json, mục "Bước 1"), work/{name}/transcript.json và work/{name}/cut_list.json.
Rà lại danh sách cắt: bật/tắt "enabled" của từng cut, thêm cut "manual" (id mới, start/end khớp ranh giới từ trong
transcript, không cắt vào giữa một từ được giữ) cho câu nói lại / nói vấp / đoạn thừa còn sót; tắt các cut làm mất ý.
{note}
Giữ nguyên các trường khác, đặt "approved": true. Chỉ ghi file work/{name}/cut_list.json, không chạy lệnh shell.
Trả lời ngắn gọn bằng tiếng Việt khi xong."""

# "Làm lại phần" chips in Studio -> pipeline steps (merged in PIPE_ORDER) + what the director should redo
REDO_PARTS = {
    "cut": {"steps": ["cut-review", "approve", "cut", "director", "plan-check", "build"], "label": "cắt"},
    "head_text": {"steps": ["director", "plan-check", "build"], "label": "chữ sau đầu (head_text)"},
    "zoom": {"steps": ["director", "plan-check", "build"], "label": "zoom"},
    "broll": {"steps": ["broll-suggest", "build"], "label": "B-roll"},
    "audio": {"steps": ["build"], "label": "âm thanh (nhạc nền + SFX)"},
    "bg": {"steps": ["build"], "label": "ảnh nền"},
}
PIPE_ORDER = ["cut-review", "approve", "cut", "director", "plan-check", "broll-suggest", "build"]

UPLOADS = ROOT / ".tmp" / "uploads"
UPLOAD_EXT = {"mp4", "mov", "m4v", "mkv", "webm", "avi", "mts", "3gp"}
TOKEN_RE = re.compile(r"^[a-f0-9]{8,32}$")

STEP_LABELS = {"assemble": "Ghép video thô", "transcribe": "Nhận dạng giọng nói", "cutlist": "Tìm đoạn thừa", "approve": "Duyệt tất cả",
               "cut": "Cắt video", "director": "Claude viết kịch bản dựng", "build": "Dựng + render + QA",
               "plan-check": "Kiểm tra kịch bản", "broll-suggest": "Claude gợi ý B-roll",
               "cut-review": "Claude rà lại đoạn cắt"}


# ---------------------------------------------------------------- project state

def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def source_of(name: str) -> Path | None:
    meta = load_json(WORK / name / "meta.json")
    if meta and (ROOT / meta["source"]).exists():
        return ROOT / meta["source"]
    p = INPUT / f"{name}.mp4"
    return p if p.exists() else None


def status_of(name: str) -> dict:
    wd = WORK / name
    cl = load_json(wd / "cut_list.json") or {}
    qa = load_json(wd / "qa" / "qa_report.json")
    final = OUTPUT / f"{name}_final.mp4"
    st = {
        "source": source_of(name) is not None,
        "transcribed": (wd / "transcript.json").exists(),
        "cutlist": bool(cl),
        "approved": bool(cl.get("approved")),
        "cut": (wd / "cut.mp4").exists() and (wd / "transcript.cut.json").exists(),
        "plan": (wd / "edit_plan.json").exists(),
        "final": final.exists(),
        "qa_ok": bool(qa and qa.get("ok")) if final.exists() else False,
    }
    order = ["source", "transcribed", "approved", "cut", "plan", "final"]
    st["step"] = next((i for i, k in enumerate(order) if not st[k]), len(order))
    return st


def list_projects() -> list[dict]:
    names = {p.stem for p in INPUT.glob("*.mp4")}
    names |= {p.name for p in WORK.iterdir() if (p / "meta.json").exists()} if WORK.exists() else set()
    with LOCK:
        names |= set(ACTIVE)  # e.g. still assembling raw clips, no input file yet
    out = []
    for n in names:
        if not NAME_RE.match(n):
            continue
        src = source_of(n)
        mtime = max([p.stat().st_mtime for p in (src, WORK / n) if p and p.exists()] or [0])
        with LOCK:
            running = ACTIVE.get(n)
        out.append({"name": n, "status": status_of(n), "mtime": mtime, "running": running})
    return sorted(out, key=lambda x: x["mtime"], reverse=True)


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def project_detail(name: str) -> dict:
    wd = WORK / name
    st = status_of(name)
    tr = load_json(wd / "transcript.cut.json") or load_json(wd / "transcript.json") or {}
    cl = load_json(wd / "cut_list.json") or {}
    plan = load_json(wd / "edit_plan.json") or {}
    qa = load_json(wd / "qa" / "qa_report.json")
    src = source_of(name)
    scenes = [e for e in plan.get("events", []) if e.get("type") == "scene"]
    share = {}
    for e in scenes:
        lay = e["params"].get("layout", "normal")
        share[lay] = round(share.get(lay, 0) + e["params"].get("dur", 0), 2)
    final = OUTPUT / f"{name}_final.mp4"
    sett = pst.load_settings(name)
    return {
        "name": name, "status": st,
        "source": rel(src) if src else None,
        "cut_video": rel(wd / "cut.mp4") if (wd / "cut.mp4").exists() else None,
        "final": rel(final) if final.exists() else None,
        "duration": tr.get("duration"),
        "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in tr.get("segments", [])],
        "cuts": [{k: c.get(k) for k in ("id", "reason", "start", "end", "text", "enabled", "similarity", "kept_text")}
                 for c in cl.get("cuts", [])],
        "cut_stats": cl.get("stats"),
        "plan": {"duration": plan.get("duration"), "share": share,
                 "events": [{"t": e.get("t"), "type": e.get("type"), "params": e.get("params")}
                            for e in plan.get("events", [])]} if plan else None,
        "qa": {**qa, "sheets": [rel(Path(s)) for s in qa.get("sheets", []) if Path(s).exists()]} if qa else None,
        "claude": shutil.which("claude") is not None,
        "director_prompt": director_prompt(name),
        "settings": {**sett, "background_effective": pst.background_for(name, plan) if plan else sett.get("background"),
                     "preset_label": (pst.get_preset(sett.get("style_preset", "")) or {}).get("label")},
        "broll": pst.load_broll(name),
        "snapgen": {"has_key": snapgen.has_key(), "credits_per_clip": snapgen.CREDITS_PER_CLIP, "model": snapgen.MODEL},
    }


# ---------------------------------------------------------------- pipeline jobs

def approve_all(name: str, enabled: dict | None = None) -> None:
    path = WORK / name / "cut_list.json"
    cl = load_json(path)
    if not cl:
        raise RuntimeError("chưa có cut_list.json")
    cuts = [{**c, "enabled": bool(enabled.get(c["id"], c.get("enabled", True)))} if enabled else c
            for c in cl["cuts"]]
    path.write_text(json.dumps({**cl, "cuts": cuts, "approved": True}, ensure_ascii=False, indent=2), encoding="utf-8")


def probe(path: Path) -> tuple[float, bool]:
    """(duration seconds, has audio stream)."""
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                        "-of", "json", str(path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"không đọc được {path.name} — có phải file video không?")
    data = json.loads(r.stdout)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    return float(data.get("format", {}).get("duration") or 0), has_audio


def assemble(name: str, files: list[Path], log) -> None:
    """Raw clips -> input/<name>.mp4. One .mp4 is moved as is; otherwise every clip is scaled/cropped to
    1080x1920 30 fps (same rule as the cut step), audio resampled to 48 kHz stereo, and concatenated in order."""
    dst = INPUT / f"{name}.mp4"
    if dst.exists():
        raise RuntimeError(f"input/{name}.mp4 đã tồn tại — đổi tên dự án")
    if len(files) == 1 and files[0].suffix.lower() == ".mp4":
        probe(files[0])
        shutil.move(str(files[0]), dst)
        log(f"✓ đã lưu input/{name}.mp4")
        return
    infos = [probe(f) for f in files]
    total = sum(d for d, _ in infos) or 1
    args, parts, labels = [], [], []
    for i, (f, (dur, has_audio)) in enumerate(zip(files, infos)):
        args += ["-i", str(f)]
        parts.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
                     f"fps=30,setsar=1,format=yuv420p[v{i}]")
        parts.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]" if has_audio else
                     f"anullsrc=r=48000:cl=stereo,atrim=duration={dur:.3f}[a{i}]")
        labels.append(f"[v{i}][a{i}]")
        log(f"  clip {i + 1}: {f.name.split('_', 1)[-1]}  {dur:.1f}s{'' if has_audio else '  (không có tiếng)'}")
    graph = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(files)}:v=1:a=1[v][a]"
    tmp = dst.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-v", "error", "-nostats", "-progress", "pipe:1", "-y", *args, "-filter_complex", graph,
           "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "fast", "-crf", "16", "-g", "30",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(tmp)]
    log(f"Ghép {len(files)} clip, tổng {total:.1f}s → 1080×1920 30fps…")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            encoding="utf-8", errors="replace")
    step, tail = 10, []
    for line in proc.stdout:
        m = re.match(r"out_time_us=(\d+)", line)
        if m and int(m[1]) / 1e6 / total * 100 >= step:
            log(f"  {step}%")
            step += 10
        elif not m and "=" not in line:
            tail.append(line.rstrip())
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("ffmpeg ghép lỗi: " + " | ".join(tail[-5:]))
    tmp.replace(dst)
    log(f"✓ đã ghép → input/{name}.mp4")


def director_prompt(name: str, note: str = "") -> str:
    prompt = DIRECTOR_PROMPT.format(name=name)
    preset = pst.get_preset(pst.load_settings(name).get("style_preset", ""))
    if preset:
        prompt += DIRECTOR_STYLE.format(label=preset.get("label", ""), notes=preset.get("notes", ""), slug=preset["slug"])
    if note:
        prompt += DIRECTOR_NOTE.format(note=note)
    return prompt


def claude_cmd(prompt: str) -> list[str]:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("không tìm thấy lệnh claude (Claude Code) trên máy")
    return [claude, "-p", prompt, "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write", "Edit", "Glob", "Grep"]


def step_cmd(name: str, step: str, quality: str, feedback: str = "", note: str = "",
             broll_n: str = "") -> list[str]:
    if step == "transcribe":
        src = source_of(name)
        if not src:
            raise RuntimeError(f"không thấy video nguồn input/{name}.mp4")
        return [PY, "-m", "autoedit", "transcribe", rel(src)]
    if step == "director":
        prompt = director_prompt(name, note)
        if feedback:
            prompt += DIRECTOR_FIX.format(name=name, errors=feedback)
        return claude_cmd(prompt)
    if step == "broll-suggest":
        ex.library_for_director(name)
        return claude_cmd(BROLL_PROMPT.format(name=name, n=broll_n or "3–5"))
    if step == "cut-review":
        extra = f"Yêu cầu của người dùng (bắt buộc làm theo): {note}" if note else ""
        return claude_cmd(CUT_PROMPT.format(name=name, note=extra))
    if step == "build":
        return [PY, "-m", "autoedit", "build", name, "--quality", quality]
    return [PY, "-m", "autoedit", step, name]


def run_pipeline(job_id: str, name: str, steps: list[str], quality: str, enabled: dict | None,
                 files: list[Path] | None = None, note: str = "", broll_n: str = "") -> None:
    job = JOBS[job_id]

    def log(line: str) -> None:
        with LOCK:
            job["log"].append(line.rstrip())
            del job["log"][:-600]

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    k, retries, feedback = 0, 0, ""
    try:
        while k < len(steps):
            step = steps[k]
            with LOCK:
                job.update(step=step, step_label=STEP_LABELS.get(step, step), index=k)
            log(f"━━ {STEP_LABELS.get(step, step)} ━━")
            if step == "approve":
                approve_all(name, enabled)
                log("✓ đã duyệt cut_list.json")
                k += 1
                continue
            if step == "assemble":
                assemble(name, files or [], log)
                shutil.rmtree(files[0].parent, ignore_errors=True)
                k += 1
                continue
            proc = subprocess.Popen(step_cmd(name, step, quality, feedback, note, broll_n), cwd=ROOT, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    encoding="utf-8", errors="replace")
            with LOCK:
                job["pid"] = proc.pid
            out = []
            for line in proc.stdout:
                out.append(line)
                log(line)
            if proc.wait() == 0:
                if step == "broll-suggest":
                    pst.save_broll(name, ex.clean_items(pst.load_broll(name)))
                k, feedback = k + 1, ""
                continue
            # plan-check failed right after Claude wrote the plan: hand the errors back and let it fix them
            if step == "plan-check" and k > 0 and steps[k - 1] == "director" and retries < DIRECTOR_RETRIES:
                retries += 1
                feedback = "".join(out)[-2000:]
                log(f"↻ plan-check chưa đạt — nhờ Claude sửa (lần {retries}/{DIRECTOR_RETRIES})")
                k -= 1
                continue
            raise RuntimeError(f"bước '{STEP_LABELS.get(step, step)}' lỗi (mã {proc.returncode}) — xem log")
        with LOCK:
            job.update(status="done", step_label="Xong")
    except Exception as exc:
        log(f"✗ {exc}")
        with LOCK:
            job.update(status="error", error=str(exc))
    finally:
        with LOCK:
            ACTIVE.pop(name, None)


def start_pipeline(name: str, steps: list[str], quality: str = "looks", enabled: dict | None = None,
                   files: list[Path] | None = None, note: str = "", broll_n: str = "") -> str:
    with LOCK:
        if name in ACTIVE:
            raise RuntimeError("dự án này đang chạy một bước khác")
        job_id = uuid.uuid4().hex[:10]
        JOBS[job_id] = {"id": job_id, "name": name, "steps": steps, "status": "running", "log": [],
                        "step": steps[0], "step_label": STEP_LABELS.get(steps[0], steps[0]), "index": 0}
        ACTIVE[name] = job_id
    threading.Thread(target=run_pipeline, args=(job_id, name, steps, quality, enabled, files, note, broll_n),
                     daemon=True).start()
    return job_id


def stop_job(job_id: str) -> None:
    with LOCK:
        pid = JOBS.get(job_id, {}).get("pid")
    if pid:  # kill the whole process tree (ffmpeg / node children included)
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)


PRESETS = {
    "transcribe": ["transcribe", "cutlist"],
    "cut": ["approve", "cut"],
    "director": ["director", "plan-check"],
    "build": ["build"],
    "auto": ["transcribe", "cutlist", "approve", "cut", "director", "plan-check", "build"],
    "redo": ["director", "plan-check", "build"],
    "broll": ["broll-suggest"],
}


def clean_options(opts) -> dict:
    """Studio toggles -> per-project settings keys."""
    opts = opts if isinstance(opts, dict) else {}
    return {k: bool(opts[k]) for k in ("captions", "audio_fx") if k in opts}


def redo_plan(name: str, parts: list[str], note: str) -> tuple[list[str], str]:
    """Selected "Làm lại phần" chips + free-text request -> (steps, director note). Parts that only need new
    inputs act here: "bg" picks another background, "audio" reshuffles the music / sfx choice."""
    parts = [p for p in parts if p in REDO_PARTS]
    wanted = {s for p in parts for s in REDO_PARTS[p]["steps"]}
    if note and not wanted & {"director", "cut-review"}:
        wanted |= {"director", "plan-check", "build"}
    if not wanted:
        wanted = {"build"}
    if "cut-review" in wanted and lib.counts()["broll"] and pst.load_broll(name):
        wanted.add("broll-suggest")  # b-roll timings shift after a new cut
    sett = pst.load_settings(name)
    if "bg" in parts:
        cur = sett.get("background")
        pool = [b["file"] for b in ex.list_backgrounds() if b["file"] != cur]
        if pool:
            pst.update_settings(name, background=random.choice(pool))
    if "audio" in parts:
        pst.update_settings(name, audio_seed=int(sett.get("audio_seed", 0)) + 1)
    redo = [REDO_PARTS[p]["label"] for p in parts if p in ("cut", "head_text", "zoom")]
    dnote = ""
    if redo:
        dnote = (f"Làm lại phần: {', '.join(redo)}. Đọc work/{name}/edit_plan.json cũ trước; các phần khác "
                 "giữ càng giống bản cũ càng tốt.")
    if note:
        dnote = f"{dnote}\n{note}" if dnote else note
    return [s for s in PIPE_ORDER if s in wanted], dnote


def unique_name(base: str) -> str:
    name, n = base, 2
    while (INPUT / f"{name}.mp4").exists() or (WORK / name).exists() or name in ACTIVE:
        name, n = f"{base[:55]}_{n}", n + 1
    return name


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _media(self, relpath: str) -> None:
        target = (ROOT / relpath).resolve()
        if not any(target.is_relative_to(r.resolve()) for r in SERVE_ROOTS) or not target.is_file():
            return self._json({"error": "not found"}, 404)
        self._file(target)

    def _file(self, target: Path) -> None:
        size = target.stat().st_size
        ctype = CTYPES.get(target.suffix.lower(), "application/octet-stream")
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if not m:
            return self._send(200, target.read_bytes(), ctype, {"Accept-Ranges": "bytes"})
        start = int(m[1]) if m[1] else max(0, size - int(m[2] or 0))
        end = min(int(m[2]) if m[1] and m[2] else size - 1, size - 1, start + 8 * 2**20)
        with target.open("rb") as f:
            f.seek(start)
            body = f.read(end - start + 1)
        self._send(206, body, ctype, {"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{end}/{size}"})

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path == "/":
            return self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/projects":
            return self._json(list_projects())
        if path.startswith("/api/project/"):
            name = path.rsplit("/", 1)[-1]
            if not NAME_RE.match(name):
                return self._json({"error": "tên không hợp lệ"}, 400)
            return self._json(project_detail(name))
        if path.startswith("/api/job/"):
            with LOCK:
                jid = path.rsplit("/", 1)[-1]
                job = JOBS.get(jid) or grab.JOBS.get(jid)
                job = json.loads(json.dumps(job)) if job else None
            return self._json(job or {"error": "not found"}, 200 if job else 404)
        if path.startswith("/media/"):
            return self._media(path[len("/media/"):])
        if path == "/api/backgrounds":
            return self._json(ex.list_backgrounds())
        if path == "/api/presets":
            return self._json(pst.list_presets())
        if path == "/api/broll/library":
            items = [{k: v for k, v in x.items() if k != "_path"} for x in ex.scan_library()]
            return self._json({"dirs": [str(d) for d in ex.broll_dirs()], "home": rel(ex.BROLL_HOME),
                               "extra": ex._cfg().get("broll_dirs", []), "items": items,
                               "has_key": snapgen.has_key()})
        if path == "/api/library":
            return self._json({**lib.get_library(), "counts": lib.counts(),
                               "claude": shutil.which("claude") is not None})
        if path in ("/api/broll/file", "/api/broll/thumb"):
            from urllib.parse import parse_qs
            f = (parse_qs(urlparse(self.path).query).get("f") or [""])[0]
            target = ex.path_for(f) if f else None
            if not target:
                return self._json({"error": "not found"}, 404)
            if path.endswith("thumb"):
                t = ex.thumb(target)
                return self._send(200, t.read_bytes(), "image/jpeg") if t.exists() else self._json({"error": "no thumb"}, 404)
            return self._file(target)
        self._json({"error": "not found"}, 404)

    def do_PUT(self):
        """PUT /api/upload?token=<hex>&i=<n>&ext=<mp4|mov|...>  raw file body, streamed to disk."""
        from urllib.parse import parse_qs
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        token, ext = q.get("token", ""), q.get("ext", "").lower()
        if u.path == "/api/upload-bg":
            return self._upload_bg(q)
        if u.path != "/api/upload" or not TOKEN_RE.match(token) or ext not in UPLOAD_EXT or not q.get("i", "").isdigit():
            return self._json({"error": "yêu cầu upload không hợp lệ"}, 400)
        folder = UPLOADS / token
        folder.mkdir(parents=True, exist_ok=True)
        remaining = int(self.headers.get("Content-Length") or 0)
        target = folder / f"{int(q['i']):03d}.{ext}"
        with target.open("wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 4 * 2**20))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        if remaining:
            target.unlink(missing_ok=True)
            return self._json({"error": "upload bị ngắt giữa chừng"}, 400)
        self._json({"ok": True, "size": target.stat().st_size})

    def _upload_bg(self, q: dict) -> None:
        ext = q.get("ext", "").lower()
        n = int(self.headers.get("Content-Length") or 0)
        if ext not in ("jpg", "jpeg", "png", "webp") or not 0 < n <= 40 * 2**20:
            return self._json({"error": "ảnh nền phải là JPG/PNG/WebP ≤ 40 MB"}, 400)
        tmp = UPLOADS / f"bg_{uuid.uuid4().hex[:8]}.{ext}"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(self.rfile.read(n))
        try:
            f = ex.add_background(tmp, q.get("label", ""), q.get("blur") == "1")
        except RuntimeError as exc:
            return self._json({"error": str(exc)}, 400)
        self._json({"file": f})

    def _post_extra(self, path: str, req: dict):
        """Background / style / b-roll endpoints. Returns None when the path is not one of them."""
        name = str(req.get("name", ""))
        needs_name = path in ("/api/settings", "/api/preset/save", "/api/broll/save", "/api/broll/generate")
        if needs_name and (not NAME_RE.match(name) or not (WORK / name).is_dir()):
            return self._json({"error": "dự án không hợp lệ"}, 400)
        if path == "/api/settings":
            ch = {}
            if "background" in req:
                bg = req["background"]
                if bg and not ex.valid_background(str(bg)):
                    return self._json({"error": "ảnh nền không hợp lệ"}, 400)
                ch["background"] = bg or None
            if "style_preset" in req:
                sp = req["style_preset"]
                if sp and not pst.get_preset(str(sp)):
                    return self._json({"error": "không thấy phong cách này"}, 400)
                ch["style_preset"] = sp or None
            return self._json(pst.update_settings(name, **ch))
        if path == "/api/preset/save":
            label = str(req.get("label", "")).strip()
            if not label:
                return self._json({"error": "đặt tên cho phong cách"}, 400)
            if not (WORK / name / "edit_plan.json").exists():
                return self._json({"error": "dự án chưa có kịch bản dựng để lưu"}, 400)
            preset = pst.save_preset(name, label, str(req.get("notes", "")))
            pst.update_settings(name, style_preset=preset["slug"])
            return self._json(preset)
        if path == "/api/preset/delete":
            pst.delete_preset(str(req.get("slug", "")))
            return self._json({"ok": True})
        if path == "/api/broll/dirs":
            dirs = req.get("dirs") if isinstance(req.get("dirs"), list) else []
            return self._json({"dirs": ex.set_broll_dirs([str(d) for d in dirs])})
        if path == "/api/broll/save":
            items = ex.clean_items(req.get("items"))
            pst.save_broll(name, items)
            return self._json({"items": items})
        if path == "/api/broll/generate":
            ids = [str(i) for i in req.get("ids", [])][:8]
            if not snapgen.has_key():
                return self._json({"error": "chưa có SNAPGENAI_API_KEY trong .env"}, 400)
            if not ids:
                return self._json({"error": "chọn ít nhất một đoạn"}, 400)
            with LOCK:
                job_id = uuid.uuid4().hex[:10]
                JOBS[job_id] = {"id": job_id, "name": name, "steps": ["snapgen"], "status": "running", "log": [],
                                "step": "snapgen", "step_label": "Tạo B-roll SnapGen", "index": 0}
            threading.Thread(target=ex.run_snapgen, args=(JOBS[job_id], LOCK, name, ids), daemon=True).start()
            return self._json({"id": job_id})
        return None

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            if path in ("/api/info", "/api/download"):
                url = str(req.get("url", "")).strip()
                if not re.match(r"^https?://", url):
                    return self._json({"error": "Link phải bắt đầu bằng http:// hoặc https://"}, 400)
                if path == "/api/info":
                    return self._json(grab.get_info(url))
                grab.parse_time(req.get("start")), grab.parse_time(req.get("end"))
                job_id = uuid.uuid4().hex[:10]
                with grab.LOCK:
                    grab.JOBS[job_id] = {"id": job_id, "status": "running", "progress": 0, "stage": "Đang tải…",
                                         "title": req.get("title") or url, "speed": "", "eta": ""}
                threading.Thread(target=grab.run_job, args=(job_id, {**req, "url": url}), daemon=True).start()
                return self._json({"id": job_id})
            if path.startswith(("/api/settings", "/api/preset/", "/api/broll/")):
                if self._post_extra(path, req) is None:
                    return self._json({"error": "not found"}, 404)
                return
            if path == "/api/run":
                name, preset = str(req.get("name", "")), str(req.get("preset", ""))
                if not NAME_RE.match(name) or preset not in PRESETS:
                    return self._json({"error": "yêu cầu không hợp lệ"}, 400)
                quality = req.get("quality") if req.get("quality") in ("draft", "looks", "delivery") else "looks"
                enabled = req.get("enabled") if isinstance(req.get("enabled"), dict) else None
                steps = PRESETS[preset]
                if preset == "auto":  # resume from the first unfinished step
                    st = status_of(name)
                    first = ("transcribe" if not st["transcribed"] else "cutlist" if not st["cutlist"]
                             else "approve" if not st["cut"] else "director" if not st["plan"] else "build")
                    steps = steps[steps.index(first):]
                note = str(req.get("note") or "").strip()[:2000]
                opts = clean_options(req.get("options"))
                if opts:
                    (WORK / name).mkdir(parents=True, exist_ok=True)
                    pst.update_settings(name, **opts)
                if preset == "redo" and not (WORK / name / "transcript.cut.json").exists():
                    return self._json({"error": "cần cắt video trước"}, 400)
                broll_n = ""
                if preset == "broll":
                    cnt = str(req.get("count") or "")
                    broll_n = cnt if cnt.isdigit() and 1 <= int(cnt) <= 10 else ""
                return self._json({"id": start_pipeline(name, steps, quality, enabled, note=note, broll_n=broll_n)})
            if path == "/api/assemble":
                token = str(req.get("token", ""))
                folder = UPLOADS / token
                if not TOKEN_RE.match(token) or not folder.is_dir():
                    return self._json({"error": "chưa có file nào được tải lên"}, 400)
                files = sorted(p for p in folder.iterdir() if p.suffix.lower().lstrip(".") in UPLOAD_EXT)
                if not files:
                    return self._json({"error": "chưa có file nào được tải lên"}, 400)
                name = grab.slug(str(req.get("name") or "video_tho"))
                if req.get("unique"):
                    name = unique_name(name)
                if (INPUT / f"{name}.mp4").exists() or (WORK / name).exists():
                    shutil.rmtree(folder, ignore_errors=True)  # browser re-uploads on retry
                    return self._json({"error": f"đã có dự án tên '{name}' — đổi tên khác"}, 400)
                steps = ["assemble"] + (PRESETS["auto"] if req.get("auto") else [])
                if req.get("auto") and shutil.which("claude") and lib.counts()["broll"]:
                    steps = steps[:-1] + ["broll-suggest", "build"]  # pick illustration clips from the library
                (WORK / name).mkdir(parents=True, exist_ok=True)
                sp = str(req.get("style_preset") or "")
                if sp and pst.get_preset(sp):
                    pst.update_settings(name, style_preset=sp)
                opts = clean_options(req.get("options"))
                if opts:
                    pst.update_settings(name, **opts)
                note = str(req.get("note") or "").strip()[:2000]
                return self._json({"id": start_pipeline(name, steps, files=files, note=note), "name": name})
            if path == "/api/library":
                paths = {k: str(req[k]) for k in lib.KINDS if k in req}
                defaults = req.get("defaults") if isinstance(req.get("defaults"), dict) else None
                return self._json({**lib.set_library(paths, defaults), "counts": lib.counts()})
            if path == "/api/redo":
                name = str(req.get("name", ""))
                if not NAME_RE.match(name) or not (WORK / name).is_dir():
                    return self._json({"error": "chọn một dự án để làm lại"}, 400)
                if not (WORK / name / "transcript.cut.json").exists() or not (WORK / name / "edit_plan.json").exists():
                    return self._json({"error": "dự án chưa edit xong lần nào — bấm Tiến hành edit trước"}, 400)
                opts = clean_options(req.get("options"))
                if opts:
                    pst.update_settings(name, **opts)
                parts = [str(x) for x in req["parts"]] if isinstance(req.get("parts"), list) else []
                steps, dnote = redo_plan(name, parts, str(req.get("note") or "").strip()[:2000])
                quality = req.get("quality") if req.get("quality") in ("draft", "looks", "delivery") else "looks"
                return self._json({"id": start_pipeline(name, steps, quality, note=dnote), "steps": steps})
            if path == "/api/stop":
                stop_job(str(req.get("id", "")))
                return self._json({"ok": True})
        except Exception as exc:
            return self._json({"error": re.sub(r"\x1b\[[0-9;]*m", "", str(exc))[:400]}, 400)
        self._json({"error": "not found"}, 404)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    INPUT.mkdir(exist_ok=True)
    WORK.mkdir(exist_ok=True)
    OUTPUT.mkdir(exist_ok=True)
    url = f"http://{HOST}:{PORT}"
    ThreadingHTTPServer.allow_reuse_address = False  # on Windows reuse would let a 2nd copy share the port
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        print(f"Studio đã chạy sẵn — mở {url}", flush=True)
        webbrowser.open(url)
        return
    print(f"Studio chạy tại {url}  (Ctrl+C để tắt)", flush=True)
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
