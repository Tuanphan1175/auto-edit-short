from autoedit.transcribe import build_segments, snap_to_silences


def w(text, s, e):
    return {"text": text, "start": s, "end": e}


def test_word_stretched_over_inner_silence_keeps_longer_side():
    # 'thứ' 14.86-18.74 spans silence 15.00-18.46: real speech is the 0.28 s after the pause
    out = snap_to_silences([w("thứ", 14.86, 18.74)], [[15.0, 18.46]])
    assert (out[0]["start"], out[0]["end"]) == (18.46, 18.74)


def test_silence_at_word_edges_moves_that_edge():
    out = snap_to_silences([w("a", 0.0, 3.2), w("b", 5.0, 7.0)], [[0.0, 2.95], [6.1, 7.7]])
    assert (out[0]["start"], out[0]["end"]) == (2.95, 3.2)
    assert (out[1]["start"], out[1]["end"]) == (5.0, 6.1)


def test_short_silences_and_untouched_words_are_ignored():
    words = [w("x", 1.0, 1.5), w("y", 1.5, 2.0)]
    assert snap_to_silences(words, [[1.2, 1.4]]) == words  # 0.2 s gap < SNAP_MIN_SILENCE


def test_snapping_restores_sentence_breaks_without_punctuation():
    words = [w("sức", 14.36, 14.6), w("mạnh", 14.6, 14.86), w("thứ", 14.86, 18.74), w("nhất", 18.74, 19.0)]
    assert len(build_segments(words)) == 1
    assert len(build_segments(snap_to_silences(words, [[15.0, 18.46]]))) == 2
