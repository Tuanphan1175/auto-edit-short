import numpy as np

from autoedit.transcribe import PHO_MAX_WINDOW_S, PHO_SR, _pho_windows


def test_short_audio_is_one_window():
    a = np.ones(PHO_SR * 10, dtype="float32")
    assert _pho_windows(a) == [(0, len(a))]


def test_windows_cover_audio_contiguously_and_fit_whisper():
    a = np.random.default_rng(0).standard_normal(PHO_SR * 95).astype("float32")
    w = _pho_windows(a)
    assert w[0][0] == 0 and w[-1][1] == len(a)
    assert all(e == s for (_, e), (s, _) in zip(w, w[1:]))
    assert all(0 < e - s <= PHO_MAX_WINDOW_S * PHO_SR for s, e in w)


def test_cut_lands_in_silence():
    a = np.ones(PHO_SR * 40, dtype="float32")
    gap = slice(int(PHO_SR * 26), int(PHO_SR * 26.5))
    a[gap] = 0
    (_, cut), _ = _pho_windows(a)
    assert gap.start <= cut < gap.stop
