"""Local media libraries set in Studio: B-roll, background music, sound effects.

config/studio.json  {"library": {"broll": "E:\\SOURCE\\KHO DATA", "music": "...", "sfx": "...\\sfx.zip"},
                     "defaults": {"captions": true, "audio_fx": false}}

Each library path is a folder or a .zip file. A .zip is extracted once to .tmp/libcache/<hash>/ (re-extracted when
the zip changes) and used like a folder. Empty path -> the project's default library (media/broll, media/music,
media/sfx).
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import zipfile
from pathlib import Path

from .common import ROOT

STUDIO_CFG = ROOT / "config" / "studio.json"
CACHE = ROOT / ".tmp" / "libcache"
KINDS = ("broll", "music", "sfx")
DEFAULT_DIRS = {"broll": ROOT / "media" / "broll", "music": ROOT / "media" / "music", "sfx": ROOT / "media" / "sfx"}
EXT = {
    "broll": {".mp4", ".mov", ".m4v", ".webm", ".mkv"},
    "music": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
    "sfx": {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"},
}
TRANSITION_SFX = re.compile(r"whoosh|swoosh|swish|swipe|transition|chuyen|chuyển|vut|vụt|woosh|wind|gio|gió", re.I)
MUSIC_GAIN_DB = -21.0   # music bed under the voice (before ducking)
SFX_GAIN_DB = -9.0
SFX_MIN_GAP = 2.0       # seconds between two sound effects
MAX_SFX = 14


def read_cfg() -> dict:
    try:
        return json.loads(STUDIO_CFG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_cfg(cfg: dict) -> None:
    STUDIO_CFG.parent.mkdir(parents=True, exist_ok=True)
    tmp = STUDIO_CFG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STUDIO_CFG)


def clean_path(raw: str) -> str:
    return str(raw or "").strip().strip('"').strip("'").strip()


def check_path(raw: str) -> Path | None:
    """Validated library path (folder or .zip). Empty -> None. Raises ValueError with a user-facing message."""
    s = clean_path(raw)
    if not s:
        return None
    p = Path(s)
    if not p.is_absolute():
        raise ValueError(f"cần đường dẫn đầy đủ, vd. E:\\TISA\\NHAC NEN — '{s}'")
    if p.is_dir():
        return p
    if p.is_file() and p.suffix.lower() == ".zip":
        if not zipfile.is_zipfile(p):
            raise ValueError(f"file zip hỏng: {p}")
        return p
    raise ValueError(f"không thấy thư mục hoặc file .zip: {p}")


def _extract(zip_path: Path) -> Path:
    st = zip_path.stat()
    key = hashlib.sha1(f"{zip_path.resolve()}|{st.st_size}|{int(st.st_mtime)}".lower().encode()).hexdigest()[:16]
    out = CACHE / key
    if (out / ".done").exists():
        return out
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            target = (out / info.filename).resolve()
            if not target.is_relative_to(root):  # zip-slip guard
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as dst:
                while chunk := src.read(4 * 2**20):
                    dst.write(chunk)
    (out / ".done").write_text("ok", encoding="utf-8")
    return out


def folder(kind: str) -> Path:
    """Folder to scan for this library (user path, extracted zip, or the project default)."""
    raw = read_cfg().get("library", {}).get(kind, "")
    try:
        p = check_path(raw)
    except ValueError:
        p = None
    if p is None:
        return DEFAULT_DIRS[kind]
    return _extract(p) if p.is_file() else p


def files(kind: str, limit: int = 2000) -> list[Path]:
    d = folder(kind)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*")):
        if p.suffix.lower() in EXT[kind] and p.is_file() and ".raw." not in p.name:
            out.append(p)
            if len(out) >= limit:
                break
    return out


def get_library() -> dict:
    cfg = read_cfg()
    lib = {k: cfg.get("library", {}).get(k, "") for k in KINDS}
    defaults = {"captions": True, "audio_fx": False, **cfg.get("defaults", {})}
    return {**lib, "defaults": defaults}


def set_library(paths: dict | None = None, defaults: dict | None = None) -> dict:
    """Save library paths / option defaults. Returns {"library", "errors"}; invalid paths are not saved."""
    cfg = read_cfg()
    lib, errors = dict(cfg.get("library", {})), {}
    for k in KINDS:
        if paths and k in paths:
            try:
                check_path(paths[k])
                lib[k] = clean_path(paths[k])
            except ValueError as exc:
                errors[k] = str(exc)
    cfg["library"] = lib
    if defaults:
        cfg["defaults"] = {**cfg.get("defaults", {}),
                           **{k: bool(v) for k, v in defaults.items() if k in ("captions", "audio_fx")}}
    write_cfg(cfg)
    return {"library": get_library(), "errors": errors}


def counts() -> dict:
    return {k: len(files(k)) for k in KINDS}


# ---------------------------------------------------------------- music + sfx choice
def pick_music(name: str, seed: int = 0) -> Path | None:
    pool = files("music")
    return random.Random(f"{name}|music|{seed}").choice(pool) if pool else None


def sfx_times(plan: dict, broll: list[dict], duration: float) -> list[float]:
    """Whoosh points: scene changes and b-roll entries, at least SFX_MIN_GAP apart."""
    ts = [float(e["t"]) for e in plan.get("events", []) if e.get("type") == "scene" and float(e.get("t", 0)) > 0.2]
    ts += [float(b["t"]) for b in broll if b.get("enabled", True) and b.get("file")]
    out: list[float] = []
    for t in sorted(ts):
        if t < duration - 0.3 and (not out or t - out[-1] >= SFX_MIN_GAP):
            out.append(round(t, 3))
    return out[:MAX_SFX]


def pick_sfx(name: str, n: int, seed: int = 0) -> list[Path]:
    pool = files("sfx")
    if not pool or n <= 0:
        return []
    pref = [p for p in pool if TRANSITION_SFX.search(p.stem)] or pool
    rnd = random.Random(f"{name}|sfx|{seed}")
    return [rnd.choice(pref) for _ in range(n)]


def mix_graph(n_sfx: int, sfx_times_s: list[float], duration: float, has_music: bool) -> tuple[str, str]:
    """ffmpeg filter graph. Inputs: 0 = voice, 1 = music (if any, looped), then one input per sfx.
    Music is ducked by the voice (sidechain) and faded in/out. Returns (graph, output label)."""
    parts, mix = ["[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit=2[v][vkey]"], ["[v]"]
    first_sfx = 1
    if has_music:
        fade_out = max(0.0, duration - 1.5)
        parts.append(f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{duration:.3f},"
                     f"volume={MUSIC_GAIN_DB}dB,afade=t=in:d=1,afade=t=out:st={fade_out:.3f}:d=1.5[mraw]")
        parts.append("[mraw][vkey]sidechaincompress=threshold=0.03:ratio=6:attack=15:release=350[m]")
        mix.append("[m]")
        first_sfx = 2
    else:
        parts.append("[vkey]anullsink")
    for i, t in enumerate(sfx_times_s[:n_sfx]):
        ms = int(t * 1000)
        parts.append(f"[{first_sfx + i}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:1.6,"
                     f"afade=t=out:st=1.2:d=0.4,volume={SFX_GAIN_DB}dB,adelay={ms}|{ms}[s{i}]")
        mix.append(f"[s{i}]")
    parts.append(f"{''.join(mix)}amix=inputs={len(mix)}:normalize=0:duration=first[out]")
    return ";".join(parts), "[out]"
