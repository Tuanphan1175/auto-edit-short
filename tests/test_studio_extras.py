from pathlib import Path

from autoedit import broll, settings, snapgen


def test_deep_merge_overrides_nested_without_mutating_base():
    base = {"colors": {"accent": "#FFD60A", "text": "#FFF"}, "split_shift": 320}
    out = settings.deep_merge(base, {"colors": {"accent": "#FF0000"}})
    assert out == {"colors": {"accent": "#FF0000", "text": "#FFF"}, "split_shift": 320}
    assert base["colors"]["accent"] == "#FFD60A"


def test_slugify_vietnamese():
    assert settings.slugify("Bác sĩ — Vàng đậm!") == "bac_si_vang_dam"
    assert settings.slugify("   ") == "phong_cach"


def test_broll_usable_skips_disabled_missing_and_clamps_to_duration(tmp_path: Path):
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"x")
    items = [
        {"id": "b1", "t": 5, "dur": 3, "file": str(clip), "enabled": True},
        {"id": "b2", "t": 1, "dur": 2, "file": str(clip), "enabled": False},
        {"id": "b3", "t": 2, "dur": 2, "file": str(tmp_path / "missing.mp4"), "enabled": True},
        {"id": "b4", "t": 9.5, "dur": 3, "file": str(clip), "enabled": True},  # only 0.5 s left -> dropped
    ]
    out = broll.usable(items, 10.0)
    assert [x["id"] for x in out] == ["b1"]
    assert out[0]["dur"] == 3


def test_snapgen_prompt_rules_appended_once():
    assert "No text" in snapgen.full_prompt("A bowl of soup")
    p = "Kitchen, no text, no logos"
    assert snapgen.full_prompt(p) == p


def test_snapgen_redact(monkeypatch):
    monkeypatch.setattr(snapgen, "api_key", lambda: "sk-secret-123")
    assert snapgen.redact("bad key sk-secret-123 here") == "bad key <redacted> here"
