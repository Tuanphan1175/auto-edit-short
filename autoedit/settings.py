"""Per-project overrides chosen in Studio, and saved edit styles ("phong cách").

work/<name>/settings.json   {"background": "media/backgrounds/x.jpg", "style_preset": "<slug>"}
config/styles/<slug>.json   {"label", "notes", "style": {...style.json overrides}, "background", "source", "created"}
config/styles/<slug>.plan.json   edit_plan.json of the project the style was saved from (reference for the director)
"""
from __future__ import annotations

import copy
import json
import re
import time
import unicodedata
from pathlib import Path

from .common import ROOT

STYLES = ROOT / "config" / "styles"
SLUG_RE = re.compile(r"^[a-z0-9_\-]{1,50}$")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def slugify(text: str) -> str:
    t = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:50] or "phong_cach"


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


# ---------------------------------------------------------------- project settings
def settings_path(name: str) -> Path:
    return ROOT / "work" / name / "settings.json"


def load_settings(name: str) -> dict:
    return _read(settings_path(name))


def update_settings(name: str, **changes) -> dict:
    cur = load_settings(name)
    for k, v in changes.items():
        if v is None:
            cur.pop(k, None)
        else:
            cur[k] = v
    _write(settings_path(name), cur)
    return cur


def base_style() -> dict:
    return _read(ROOT / "config" / "style.json")


def effective_style(name: str) -> dict:
    """config/style.json, overridden by the project's chosen style preset (if any)."""
    preset = get_preset(load_settings(name).get("style_preset", ""))
    return deep_merge(base_style(), preset.get("style", {})) if preset else base_style()


def background_for(name: str, plan: dict) -> str:
    """Studio choice > style preset > what the director wrote in edit_plan.json."""
    s = load_settings(name)
    preset = get_preset(s.get("style_preset", ""))
    for cand in (s.get("background"), preset.get("background") if preset else None, plan.get("background")):
        if cand and (ROOT / cand).is_file():
            return cand
    return plan.get("background", "")


# ---------------------------------------------------------------- style presets
def get_preset(slug: str) -> dict:
    if not slug or not SLUG_RE.match(slug):
        return {}
    data = _read(STYLES / f"{slug}.json")
    return {**data, "slug": slug} if data else {}


def list_presets() -> list[dict]:
    if not STYLES.exists():
        return []
    out = [get_preset(p.stem) for p in STYLES.glob("*.json") if not p.name.endswith(".plan.json")]
    return sorted((p for p in out if p), key=lambda p: p.get("created", 0), reverse=True)


def save_preset(project: str, label: str, notes: str = "") -> dict:
    """Snapshot what made this project's edit good: effective style, background, director plan, notes."""
    wd = ROOT / "work" / project
    plan = _read(wd / "edit_plan.json")
    slug = base = slugify(label)
    n = 2
    while (STYLES / f"{slug}.json").exists() and get_preset(slug).get("source") != project:
        slug, n = f"{base}_{n}", n + 1
    data = {
        "label": label.strip()[:80] or slug,
        "notes": notes.strip()[:2000],
        "style": effective_style(project),
        "background": background_for(project, plan) or None,
        "source": project,
        "created": time.time(),
    }
    _write(STYLES / f"{slug}.json", data)
    if plan:
        _write(STYLES / f"{slug}.plan.json", plan)
    return {**data, "slug": slug}


def delete_preset(slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise ValueError("bad preset id")
    for p in (STYLES / f"{slug}.json", STYLES / f"{slug}.plan.json"):
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------- b-roll list (work/<name>/broll.json)
def broll_path(name: str) -> Path:
    return ROOT / "work" / name / "broll.json"


def load_broll(name: str) -> list[dict]:
    return _read(broll_path(name)).get("items", [])


def save_broll(name: str, items: list[dict]) -> None:
    _write(broll_path(name), {"items": items})
