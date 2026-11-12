import zipfile
from pathlib import Path

import pytest

from autoedit import library


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(library, "STUDIO_CFG", tmp_path / "studio.json")
    monkeypatch.setattr(library, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(library, "DEFAULT_DIRS", {k: tmp_path / "default" / k for k in library.KINDS})
    return tmp_path


def test_check_path_accepts_folder_zip_and_empty(tmp_path: Path):
    z = tmp_path / "sfx.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("a.mp3", b"x")
    assert library.check_path("") is None
    assert library.check_path(f'"{tmp_path}"') == tmp_path
    assert library.check_path(str(z)) == z


def test_check_path_rejects_relative_missing_and_non_zip(tmp_path: Path):
    other = tmp_path / "note.txt"
    other.write_text("x")
    for bad in ("relative\\dir", str(tmp_path / "nope"), str(other)):
        with pytest.raises(ValueError):
            library.check_path(bad)


def test_set_library_keeps_valid_and_reports_invalid(cfg: Path):
    music = cfg / "music"
    music.mkdir()
    r = library.set_library({"music": str(music), "sfx": str(cfg / "missing")}, {"audio_fx": True})
    assert r["library"]["music"] == str(music)
    assert r["library"]["sfx"] == ""
    assert "sfx" in r["errors"]
    assert r["library"]["defaults"]["audio_fx"] is True


def test_zip_library_is_extracted_and_scanned(cfg: Path):
    z = cfg / "sfx.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("pack/whoosh_1.mp3", b"x")
        f.writestr("pack/readme.txt", b"x")
        f.writestr("../evil.mp3", b"x")  # zip-slip entry is skipped
    library.set_library({"sfx": str(z)})
    found = library.files("sfx")
    assert [p.name for p in found] == ["whoosh_1.mp3"]
    assert not (cfg / "evil.mp3").exists()


def test_empty_path_uses_default_folder(cfg: Path):
    d = library.DEFAULT_DIRS["music"]
    d.mkdir(parents=True)
    (d / "bed.mp3").write_bytes(b"x")
    assert library.counts()["music"] == 1


def test_sfx_times_respect_gap_and_skip_start():
    plan = {"events": [{"type": "scene", "t": 0}, {"type": "scene", "t": 3.0}, {"type": "scene", "t": 4.0},
                       {"type": "head_text", "t": 5.0}, {"type": "scene", "t": 9.9}]}
    broll = [{"t": 6.5, "file": "a.mp4", "enabled": True}, {"t": 7.0, "file": None}]
    assert library.sfx_times(plan, broll, 10.0) == [3.0, 6.5]


def test_pick_sfx_prefers_transition_names(cfg: Path):
    d = cfg / "sfx"
    d.mkdir()
    for n in ("ding.mp3", "whoosh_fast.mp3"):
        (d / n).write_bytes(b"x")
    library.set_library({"sfx": str(d)})
    assert {p.name for p in library.pick_sfx("demo", 4)} == {"whoosh_fast.mp3"}


def test_mix_graph_with_and_without_music():
    g, out = library.mix_graph(2, [1.5, 4.0], 12.0, True)
    assert out == "[out]" and "sidechaincompress" in g and "adelay=1500|1500" in g and "amix=inputs=4" in g
    g2, _ = library.mix_graph(1, [2.0], 12.0, False)
    assert "anullsink" in g2 and "[1:a]" in g2 and "amix=inputs=2" in g2
