"""Step 4a: HyperFrames render -> loudnorm (-14 LUFS, 2 pass) -> mux H.264 + AAC -> output/<name>_final.mp4."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .common import OUTPUT, ROOT, run


def _npx_env() -> dict:
    env = dict(os.environ)
    env.setdefault("npm_config_cache", str(ROOT / ".npm-cache"))  # the user's ~/.npm is root-owned
    # HyperFrames stores ~1 GB of captured frames per render in TEMP; keep them on the project drive
    # (the system drive may be nearly full). Override with AUTOEDIT_TMP.
    tmp = Path(os.environ.get("AUTOEDIT_TMP", ROOT / ".tmp"))
    tmp.mkdir(parents=True, exist_ok=True)
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp)
    return env


def hf(*args: str, timeout: int | None = None) -> subprocess.CompletedProcess:
    npx = shutil.which("npx") or "npx"  # Windows needs the full path to npx.cmd
    return subprocess.run([npx, "hyperframes", *args], cwd=ROOT, env=_npx_env(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


LOUD_TOL = 0.5      # LU; QA accepts ±1, aim tighter
TRUE_PEAK = -1.5    # dBTP


def _measure(path: Path, target: float) -> dict:
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-vn", "-af",
                        f"loudnorm=I={target}:TP={TRUE_PEAK}:LRA=11:print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    return json.loads(re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", p.stderr, re.S).group(0))


def _encode(src: Path, dst: Path, af: str) -> None:
    run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vn", "-af", af, "-ar", "48000", "-c:a", "aac", "-b:a", "192k",
         str(dst)])


def loudnorm(src: Path, dst: Path, target: float = -14.0) -> dict:
    m = _measure(src, target)
    af = (f"loudnorm=I={target}:TP={TRUE_PEAK}:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    _encode(src, dst, af)
    got = float(_measure(dst, target)["input_i"])
    # Quiet, peaky voice (e.g. -25 LUFS input): linear loudnorm refuses gain that would break the true-peak
    # ceiling and lands short (-15.9). Fall back to plain gain + a true-peak limiter, re-measuring each pass.
    gain = target - float(m["input_i"])
    for _ in range(4):
        if abs(got - target) <= LOUD_TOL:
            break
        limit = 10 ** (TRUE_PEAK / 20)
        _encode(src, dst, f"volume={gain:.2f}dB,alimiter=limit={limit:.4f}:level=0:latency=1")
        got = float(_measure(dst, target)["input_i"])
        gain += target - got
    m["output_i"] = got
    return m


def mix_music_sfx(name: str, wd: Path) -> Path:
    """Voice track of cut.mp4, plus background music (ducked) and transition whooshes when the project's
    Studio option "Nhạc và SFX" is on and the libraries have files. Returns the file loudnorm should read."""
    from . import library
    from .settings import load_broll, load_settings

    src = wd / "cut.mp4"
    sett = load_settings(name)
    if not sett.get("audio_fx"):
        return src
    seed = int(sett.get("audio_seed", 0))
    plan = json.loads((wd / "edit_plan.json").read_text(encoding="utf-8"))
    duration = float(plan["duration"])
    music = library.pick_music(name, seed)
    times = library.sfx_times(plan, load_broll(name), duration)
    sfx = library.pick_sfx(name, len(times), seed)
    if not music and not sfx:
        print("  (Nhạc và SFX đang bật nhưng kho nhạc nền / SFX trống — giữ tiếng gốc)")
        return src
    graph, out_label = library.mix_graph(len(sfx), times, duration, music is not None)
    inputs = ["-i", str(src)]
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
    for p in sfx:
        inputs += ["-i", str(p)]
    mixed = wd / "audio_mix.wav"
    run(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", out_label,
         "-t", f"{duration:.3f}", "-ar", "48000", "-c:a", "pcm_s16le", str(mixed)])
    print(f"  ♪ nhạc nền: {music.name if music else '—'} · {len(sfx)} SFX")
    return mixed


def render(name: str, quality: str = "looks", workers: str = "3") -> Path:
    wd = ROOT / "work" / name
    raw = wd / "render.mp4"
    r = hf("render", str(wd / "hf"), "-o", str(raw), "--quality", quality, "--workers", workers, "--fps", "30",
           "--keyframe-interval", "1", "--quiet")
    if r.returncode != 0 or not raw.exists():
        raise RuntimeError(f"hyperframes render failed:\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}")
    voice = wd / "voice_norm.m4a"
    m = loudnorm(mix_music_sfx(name, wd), voice)
    OUTPUT.mkdir(exist_ok=True)
    out = OUTPUT / f"{name}_final.mp4"
    vinfo = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,pix_fmt,width,height",
                 "-of", "json", str(raw)]).stdout
    v = json.loads(vinfo)["streams"][0]
    copy_ok = v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p" and (v["width"], v["height"]) == (1080, 1920)
    vcodec = ["-c:v", "copy"] if copy_ok else ["-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
                                               "-vf", "scale=1080:1920"]
    run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-i", str(voice), "-map", "0:v:0", "-map", "1:a:0", *vcodec,
         "-c:a", "copy", "-shortest", "-movflags", "+faststart", str(out)])
    print(f"✓ render {raw.name} ({'copied' if copy_ok else 're-encoded'} video), loudnorm {m['input_i']} -> {m['output_i']:.1f} LUFS -> {out}")
    return out
