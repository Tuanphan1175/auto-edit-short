"""SnapGen.ai (Veo) video client for illustration B-roll.

The API key is read from .env (SNAPGENAI_API_KEY) on every call. It is never returned, logged or written
anywhere else: every error message passes through redact().
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from .common import ROOT, run

BASE = "https://api.snapgen.ai"
ENV_FILE = ROOT / ".env"
KEY_NAMES = ("SNAPGENAI_API_KEY", "SNAPGEN_API_KEY")
FAMILY, MODEL, ASPECT, RESOLUTION = "veo", "veo-3.1-fast", "9:16", "720p"
CREDITS_PER_CLIP = 4
POLL_S, MAX_POLLS = 10, 90
# Rules learned from earlier runs: the model draws fake text on paper/screens even when told not to.
PROMPT_RULES = ("Vertical 9:16, slow cinematic push-in camera. No text, no numbers, no logos, no labels, "
                "no packaging, no screens, no faces, no people visible (hands only if needed).")


def api_key() -> str:
    for k in KEY_NAMES:
        if os.environ.get(k):
            return os.environ[k].strip()
    try:
        lines = ENV_FILE.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return ""
    for line in lines:
        k, _, v = line.partition("=")
        if k.strip() in KEY_NAMES and v.strip():
            return v.strip().strip("'\"")
    return ""


def has_key() -> bool:
    return bool(api_key())


def redact(text: str) -> str:
    key = api_key()
    return str(text).replace(key, "<redacted>") if key else str(text)


def _multipart(fields: dict) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items()]
    return ("".join(parts) + f"--{b}--\r\n").encode("utf-8"), f"multipart/form-data; boundary={b}"


def _call(path: str, fields: dict | None = None) -> dict:
    key = api_key()
    if not key:
        raise RuntimeError("chưa có SNAPGENAI_API_KEY trong file .env")
    headers = {"x-api-key": key, "User-Agent": "auto-edit-studio"}
    data = None
    if fields is not None:
        data, headers["Content-Type"] = _multipart(fields)
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise RuntimeError(redact(f"SnapGen {e.code}: {body}")) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise RuntimeError(redact(f"SnapGen lỗi kết nối: {e}")) from None


def full_prompt(prompt: str) -> str:
    p = prompt.strip()
    return p if "no text" in p.lower() else f"{p} {PROMPT_RULES}"


def submit(prompt: str) -> str:
    body = _call(f"/uapi/v1/video-gen/{FAMILY}", {"prompt": full_prompt(prompt), "model": MODEL,
                                                   "aspect_ratio": ASPECT, "resolution": RESOLUTION})
    if not body.get("uuid"):
        raise RuntimeError(redact(f"SnapGen không trả uuid: {str(body)[:300]}"))
    return body["uuid"]


def wait(job: str, out: Path, log=print) -> Path:
    """Poll until done, download, strip audio -> out (mp4)."""
    last = None
    for _ in range(MAX_POLLS):
        item = _call(f"/uapi/v1/history/{urllib.parse.quote(job)}")
        st = item.get("status")
        pct = item.get("status_percentage")
        if (st, pct) != last:
            log(f"  {job[:8]}… trạng thái {st} {pct if pct is not None else ''}")
            last = (st, pct)
        if st == 3:
            raise RuntimeError(redact(f"SnapGen tạo lỗi: {item.get('error_code')} {item.get('error_message')}"))
        if st == 2:
            urls = [(item.get("generated_video") or [{}])[0].get("video_url"), item.get("generate_result"),
                    item.get("media_url")]
            url = next((u for u in urls if isinstance(u, str) and u.startswith("http")), None)
            if not url:
                raise RuntimeError("SnapGen xong nhưng không có link video")
            raw = out.with_suffix(".raw.mp4")
            out.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(url, timeout=120) as r, raw.open("wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-an", "-c:v", "copy", str(out)])
            raw.unlink(missing_ok=True)
            return out
        time.sleep(POLL_S)
    raise RuntimeError("SnapGen quá lâu chưa xong (15 phút)")
