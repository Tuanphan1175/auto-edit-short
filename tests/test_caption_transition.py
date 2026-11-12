import json

from autoedit.common import ROOT
from autoedit.compose import build_captions
from autoedit.qa import FACE_H


def test_transition_caption_clears_shifted_face():
    style = json.loads((ROOT / "config/style.json").read_text(encoding="utf-8"))
    words = [{"text": "hơn", "start": 9.5, "end": 9.9}]
    scenes = [
        {"t": 0, "end": 10, "layout": "normal"},
        {"t": 10, "end": 12, "layout": "split"},
    ]
    _, captions = build_captions(
        words, scenes, lambda start, end: {"top": 597, "cx": 540}, style, 12
    )
    caption = captions[0]
    face_bottom = 597 + style["split_shift"] + 40 + FACE_H
    assert caption["top"] >= face_bottom + style["caption"]["gap_above_head"]
    assert caption["bottom"] <= 1920 * 0.9
