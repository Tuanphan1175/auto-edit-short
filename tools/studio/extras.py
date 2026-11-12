"""Studio add-ons: background library, saved styles, B-roll library (local / Google Drive folders) and SnapGen.

The SnapGen key stays in .env; only `has_key` (a bool) ever reaches the browser.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from autoedit import library  # noqa: E402
from autoedit import settings as st  # noqa: E402
from autoedit import snapgen  # noqa: E402

BGS = ROOT / "media" / "backgrounds"
BROLL_HOME = ROOT / "media" / "broll"
SNAP_DIR = BROLL_HOME / "snapgen"
STUDIO_CFG = ROOT / "config" / "studio.json"
THUMBS = ROOT / ".tmp" / "thumbs"
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VID_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
MAX_LIBRARY = 400
BG_W, BG_H = 1188, 2112  # compose draws #bg at this size (Ken Burns head-room)


def rel_or_abs(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p.resolve())


# ---------------------------------------------------------------- backgrounds
def list_backgrounds() -> list[dict]:
    if not BGS.exists():
        return []
    files = sorted((p for p in BGS.iterdir() if p.suffix.lower() in IMG_EXT), key=lambda p: -p.stat().st_mtime)
    return [{"file": rel_or_abs(p), "name": p.stem} for p in files]


def add_background(src: Path, label: str, blur: bool) -> str:
    """Any photo -> 9:16 cover crop at the size compose uses (+ optional soft blur so the speaker stands out)."""
    BGS.mkdir(parents=True, exist_ok=True)
    stem = st.slugify(label or "nen")
    dst, n = BGS / f"{stem}.jpg", 2
    while dst.exists():
        dst, n = BGS / f"{stem}_{n}.jpg", n + 1
    vf = f"scale={BG_W}:{BG_H}:force_original_aspect_ratio=increase,crop={BG_W}:{BG_H}" + (",gblur=sigma=7" if blur else "")
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1", "-vf", vf, "-q:v", "3", str(dst)],
                       capture_output=True, text=True)
    src.unlink(missing_ok=True)
    if r.returncode or not dst.exists():
        raise RuntimeError("không đọc được ảnh — dùng JPG/PNG/WebP")
    with (BGS / "SOURCES.md").open("a", encoding="utf-8") as f:
        f.write(f"\n- {dst.name} — ảnh người dùng tải lên qua Studio ({time.strftime('%Y-%m-%d')})"
                f"{', blur' if blur else ''}\n")
    return rel_or_abs(dst)


def valid_background(file: str) -> bool:
    p = (ROOT / file).resolve()
    return p.is_file() and p.parent == BGS.resolve() and p.suffix.lower() in IMG_EXT


# ---------------------------------------------------------------- b-roll library
def _cfg() -> dict:
    try:
        return json.loads(STUDIO_CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def broll_dirs() -> list[Path]:
    dirs = [BROLL_HOME, library.folder("broll")] + [Path(d) for d in _cfg().get("broll_dirs", [])]
    seen, out = set(), []
    for d in dirs:
        key = str(d.resolve()).lower()
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def set_broll_dirs(dirs: list[str]) -> list[str]:
    clean = []
    for d in dirs:
        p = Path(str(d).strip().strip('"'))
        if not str(p) or not p.is_absolute():
            raise ValueError(f"cần đường dẫn đầy đủ, vd. G:\\My Drive\\B-roll — '{d}'")
        if not p.is_dir():
            raise ValueError(f"không thấy thư mục: {p}")
        clean.append(str(p))
    cfg = {**_cfg(), "broll_dirs": clean}
    STUDIO_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return clean


def _id(p: Path) -> str:
    return hashlib.sha1(str(p.resolve()).lower().encode()).hexdigest()[:14]


def scan_library() -> list[dict]:
    BROLL_HOME.mkdir(parents=True, exist_ok=True)
    out = []
    for d in broll_dirs():
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*")):
            if p.suffix.lower() in VID_EXT and p.is_file() and ".raw." not in p.name:
                out.append({"id": _id(p), "file": rel_or_abs(p), "name": p.stem,
                            "folder": rel_or_abs(d), "size": p.stat().st_size,
                            "mtime": p.stat().st_mtime, "_path": p})
                if len(out) >= MAX_LIBRARY:
                    return out
    return out


def allowed_broll(path: Path) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    return rp.is_file() and rp.suffix.lower() in VID_EXT and any(rp.is_relative_to(d.resolve()) for d in broll_dirs())


def path_for(file_or_id: str) -> Path | None:
    p = Path(file_or_id)
    p = p if p.is_absolute() else ROOT / p
    if allowed_broll(p):
        return p
    hit = next((x["_path"] for x in scan_library() if x["id"] == file_or_id), None)
    return hit


def thumb(path: Path) -> Path:
    THUMBS.mkdir(parents=True, exist_ok=True)
    out = THUMBS / f"{_id(path)}_{int(path.stat().st_mtime)}.jpg"
    if not out.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=240:-2", "-q:v", "5", str(out)], capture_output=True)
        if not out.exists():  # clip shorter than 1 s
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-frames:v", "1", "-vf", "scale=240:-2",
                            str(out)], capture_output=True)
    return out


def library_for_director(name: str) -> Path:
    """Compact list the director reads to pick matching clips (no absolute home paths beyond what the user set)."""
    items = [{"file": x["file"], "name": x["name"], "folder": x["folder"]} for x in scan_library()]
    p = ROOT / "work" / name / "broll_library.json"
    p.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def clean_items(items: list) -> list[dict]:
    out = []
    for k, it in enumerate(items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        f = str(it.get("file") or "")
        if f and not path_for(f):
            f = ""
        out.append({"id": str(it.get("id") or f"b{k + 1}")[:20],
                    "t": round(max(0.0, float(it.get("t") or 0)), 2),
                    "dur": round(min(6.0, max(0.8, float(it.get("dur") or 2.5))), 2),
                    "src_start": round(max(0.0, float(it.get("src_start") or 0)), 2),
                    "line": str(it.get("line") or "")[:300], "prompt": str(it.get("prompt") or "")[:1500],
                    "file": f or None, "enabled": bool(it.get("enabled", True))})
    return sorted(out, key=lambda x: x["t"])


# ---------------------------------------------------------------- snapgen job
def run_snapgen(job: dict, lock: threading.Lock, name: str, ids: list[str]) -> None:
    def log(line: str) -> None:
        with lock:
            job["log"].append(snapgen.redact(line))

    try:
        items = st.load_broll(name)
        todo = [it for it in items if it["id"] in ids and it.get("prompt")]
        if not todo:
            raise RuntimeError("không có đoạn nào có prompt để tạo")
        log(f"Gửi {len(todo)} prompt lên SnapGen ({snapgen.MODEL}, {snapgen.CREDITS_PER_CLIP} credit/clip)…")
        jobs = []
        for it in todo:
            jobs.append((it, snapgen.submit(it["prompt"])))
            log(f"  ✓ đã gửi {it['id']} ({it.get('line', '')[:50]})")
        for k, (it, uid) in enumerate(jobs):
            with lock:
                job.update(index=k, step_label=f"Chờ SnapGen {k + 1}/{len(jobs)}")
            out = SNAP_DIR / f"{name}_{it['id']}_{time.strftime('%m%d_%H%M%S')}.mp4"
            snapgen.wait(uid, out, log)
            items = st.load_broll(name)  # re-read: user may have edited meanwhile
            for x in items:
                if x["id"] == it["id"]:
                    x.update(file=rel_or_abs(out), src_start=x.get("src_start") or 0.5, enabled=True)
            st.save_broll(name, items)
            log(f"  ✓ {it['id']} → {rel_or_abs(out)}")
        log("Xong. Xem lại từng clip (loại clip có chữ giả / mặt người), rồi bấm Dựng lại.")
        with lock:
            job.update(status="done", step_label="Xong")
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI log
        log(f"✗ {exc}")
        with lock:
            job.update(status="error", error=snapgen.redact(str(exc)))
