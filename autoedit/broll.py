"""Illustration B-roll layer: work/<name>/broll.json -> full-frame clips in the HyperFrames project.

Item: {"id", "t", "dur", "file", "src_start", "enabled", "line", "prompt"}.
Each clip is pre-cut and scaled to 1080x1920 30 fps without audio (hf/assets/broll/<id>.mp4) so the renderer only
plays a plain file. It sits above the person / motion graphics (z 5) and below captions (z 6).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .common import FPS, H, ROOT, W, ffprobe_duration, run
from .settings import load_broll

FADE = 0.25
MIN_DUR = 0.8


def resolve(file: str) -> Path:
    p = Path(file)
    return p if p.is_absolute() else ROOT / p


def usable(items: list[dict], D: float) -> list[dict]:
    out = []
    for it in items:
        if not it.get("enabled", True) or not it.get("file"):
            continue
        src = resolve(it["file"])
        if not src.is_file():
            continue
        t = max(0.0, float(it.get("t", 0)))
        dur = min(float(it.get("dur", 2.5)), D - t)
        if dur >= MIN_DUR:
            out.append({**it, "t": round(t, 3), "dur": round(dur, 3), "src": src})
    return sorted(out, key=lambda x: x["t"])


def _prepare(src: Path, start: float, dur: float, dst: Path) -> None:
    key = hashlib.sha1(f"{src}|{src.stat().st_mtime}|{start}|{dur}".encode()).hexdigest()[:12]
    stamp = dst.with_suffix(".key")
    if dst.exists() and stamp.exists() and stamp.read_text() == key:
        return
    src_dur = ffprobe_duration(src)
    start = max(0.0, min(start, max(0.0, src_dur - dur)))
    loop = ["-stream_loop", "-1"] if src_dur < start + dur else []  # short stock clip: loop it
    run(["ffmpeg", "-v", "error", "-y", *loop, "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur + 0.1:.3f}", "-an",
         "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1,format=yuv420p",
         "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-g", str(FPS), str(dst)])
    stamp.write_text(key)


def build_layer(name: str, hf: Path, D: float) -> tuple[str, list[str], list[dict]]:
    """Returns (html, gsap lines, layout entries)."""
    items = usable(load_broll(name), D)
    adir = hf / "assets" / "broll"
    if adir.exists():
        for old in adir.iterdir():
            if old.stem not in {f"b{k}" for k in range(len(items))}:
                old.unlink(missing_ok=True)
    if not items:
        return "", [], []
    adir.mkdir(parents=True, exist_ok=True)
    html, js, layout = [], [], []
    for k, it in enumerate(items):
        bid = f"b{k}"
        _prepare(it["src"], float(it.get("src_start", 0)), it["dur"], adir / f"{bid}.mp4")
        t, d = it["t"], it["dur"]
        html.append(f'<div id="{bid}w" class="broll"><video id="{bid}" class="clip" data-start="{t}" data-duration="{d}" '
                    f'data-track-index="{10 + k}" src="assets/broll/{bid}.mp4" muted playsinline '
                    f'data-layout-allow-occlusion></video></div>')
        js += [f'tl.fromTo("#{bid}w",{{opacity:0,scale:1.04}},{{opacity:1,scale:1.0,duration:{FADE},ease:"power2.out"}},{t});',
               f'tl.to("#{bid}w",{{scale:1.06,duration:{max(0.1, d - 2 * FADE)},ease:"none"}},{round(t + FADE, 3)});',
               f'tl.to("#{bid}w",{{opacity:0,duration:{FADE},ease:"power2.in"}},{round(t + d - FADE, 3)});']
        layout.append({"id": bid, "t": t, "end": round(t + d, 3), "file": it["file"]})
    return "\n    ".join(html), js, layout
