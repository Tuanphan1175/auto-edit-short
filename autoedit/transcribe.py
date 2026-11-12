"""Step 1a: audio extraction + word-level Vietnamese transcription + glossary fix."""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from .common import CONFIG, ROOT, detect_silences, extract_audio, ffprobe_duration, load, norm, save

MLX_MODEL = "mlx-community/whisper-large-v3-turbo"
PHO_MODEL_GPU = "vinai/PhoWhisper-large"  # ~3 GB fp16, fits a 12 GB card
PHO_MODEL_CPU = "vinai/PhoWhisper-small"


def _pho_model() -> str:
    import torch

    return PHO_MODEL_GPU if torch.cuda.is_available() else PHO_MODEL_CPU

# Fillers that are also real Vietnamese particles: only count them when isolated by a pause.
AMBIGUOUS_FILLERS = {"à", "ạ", "ơ", "ừ", "ờ"}


def _glossary() -> dict:
    p = CONFIG / "glossary.json"
    return load(p) if p.exists() else {"replace": {}, "fillers": [], "prompt_terms": []}


# --------------------------------------------------------------------------- engines
def _run_mlx(wav: Path, prompt: str) -> list[dict]:
    import mlx_whisper

    r = mlx_whisper.transcribe(
        str(wav),
        path_or_hf_repo=MLX_MODEL,
        language="vi",
        task="transcribe",
        word_timestamps=True,
        initial_prompt=prompt,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    out = []
    for seg in r["segments"]:
        for w in seg.get("words", []):
            out.append({"text": w["word"], "start": float(w["start"]), "end": float(w["end"])})
    return out


PHO_SR = 16000
PHO_MAX_WINDOW_S = 28.0  # Whisper sees 30 s; leave room so a window never gets truncated
PHO_CUT_SEARCH_S = 4.0   # look this far back from the window end for the quietest point to cut at


def _pho_windows(a, max_s: float = PHO_MAX_WINDOW_S) -> list[tuple[int, int]]:
    """Split audio into <=max_s windows, cutting at the quietest 20 ms frame so words aren't split."""
    import numpy as np

    hop, n, out, s = PHO_SR // 50, len(a), [], 0
    while s < n:
        e = min(n, s + int(max_s * PHO_SR))
        if e < n:
            lo = e - int(PHO_CUT_SEARCH_S * PHO_SR)
            frames = a[lo:e][: (e - lo) // hop * hop].reshape(-1, hop)
            e = lo + int(np.argmin((frames ** 2).mean(1))) * hop + hop // 2
        out.append((s, e))
        s = e
    return out


def _pho_window_words(model, proc, x, offset: float) -> list[dict]:
    """Transcribe one window, then time its words with a single cross-attention pass + DTW.

    The transformers pipeline's return_timestamps="word" keeps every decoding step's attentions
    (17+ GB for a 30 s window on PhoWhisper-large): it spills into shared memory, runs ~10x slower
    and crashes natively (0xC0000005). One forward pass over the finished tokens needs < 1 GB.
    """
    import numpy as np
    import torch
    from transformers.models.whisper.generation_whisper import _dynamic_time_warping, _median_filter

    tk, dev = proc.tokenizer, model.device
    feats = proc(x, sampling_rate=PHO_SR, return_tensors="pt").input_features.to(dev, model.dtype)
    with torch.no_grad():
        gen = model.generate(feats, language="vi", task="transcribe")[0].tolist()
        text = [t for t in gen if t < tk.eos_token_id]
        if not text:
            return []
        prompt = [tk.convert_tokens_to_ids(t) for t in
                  ("<|startoftranscript|>", "<|vi|>", "<|transcribe|>", "<|notimestamps|>")]
        ids = torch.tensor([prompt + text + [tk.eos_token_id]], device=dev)
        enc = model.model.encoder(feats)
        out = model(encoder_outputs=enc, decoder_input_ids=ids, output_attentions=True)
    n_frames = int(np.ceil(len(x) / PHO_SR / 0.02))
    w = torch.stack([out.cross_attentions[l][0, h] for l, h in model.generation_config.alignment_heads]).float()
    w = w[:, :, :n_frames]
    w = (w - w.mean(-1, keepdim=True)) / w.std(-1, keepdim=True)
    w = _median_filter(w, 7).mean(0)
    # Rows from <|notimestamps|> to the last text token: row k predicts text token k.
    rows = -w[len(prompt) - 1: len(prompt) + len(text)].cpu().numpy().astype(np.float64)
    ti, fi = _dynamic_time_warping(rows)
    jumps = np.pad(np.diff(ti), (1, 0), constant_values=1).astype(bool)
    times = list(fi[jumps] * 0.02) + [n_frames * 0.02]
    # Group BPE pieces into words (a word starts with a space byte) and decode whole words so
    # multi-byte Vietnamese characters split across tokens come out intact.
    words, cur = [], None
    for k, t in enumerate(text):
        if cur is None or tk.convert_ids_to_tokens(t).startswith("Ġ"):
            cur = {"ids": [], "start": times[k]}
            words.append(cur)
        cur["ids"].append(t)
        cur["end"] = times[k + 1]
    return [{"text": " " + tk.decode(wd["ids"]).strip(), "start": offset + float(wd["start"]),
             "end": offset + float(wd["end"])} for wd in words if tk.decode(wd["ids"]).strip()]


def _run_pho(wav: Path, prompt: str) -> list[dict]:
    import soundfile as sf
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    from transformers import logging as hf_logging

    # The generation_config deprecation notices are harmless and only confuse users.
    hf_logging.set_verbosity_error()
    use_cuda = torch.cuda.is_available()
    name = _pho_model()
    proc = WhisperProcessor.from_pretrained(name)
    # eager attention: sdpa cannot return the cross-attention weights needed for word timing.
    model = WhisperForConditionalGeneration.from_pretrained(
        name, dtype=torch.float16 if use_cuda else torch.float32, attn_implementation="eager")
    model = model.to("cuda" if use_cuda else "cpu").eval()
    a, sr = sf.read(str(wav), dtype="float32")
    if a.ndim > 1:
        a = a.mean(1)
    if sr != PHO_SR:
        raise ValueError(f"expected {PHO_SR} Hz audio, got {sr}: {wav}")
    raw: list[dict] = []
    for s, e in _pho_windows(a):
        raw += _pho_window_words(model, proc, a[s:e], s / PHO_SR)
    return raw


# --------------------------------------------------------------------------- post-processing
def _merge_tokens(raw: list[dict]) -> list[dict]:
    """Whisper marks a new word with a leading space; glue the rest ('90' + '%')."""
    words: list[dict] = []
    for t in raw:
        txt = unicodedata.normalize("NFC", t["text"])
        if not txt.strip():
            continue
        if words and not txt.startswith(" "):
            words[-1]["text"] += txt.strip()
            words[-1]["end"] = t["end"]
        else:
            words.append({"text": txt.strip(), "start": t["start"], "end": t["end"]})
    # strip pure-punctuation words into previous word
    out: list[dict] = []
    for w in words:
        if out and not norm(w["text"]):
            out[-1]["text"] += w["text"]
            continue
        out.append(w)
    for w in out:
        w["start"], w["end"] = round(w["start"], 3), round(max(w["end"], w["start"] + 0.04), 3)
    return out


def apply_glossary(words: list[dict], gl: dict) -> list[dict]:
    rules = sorted(((norm(k).split(), v) for k, v in gl.get("replace", {}).items()), key=lambda r: -len(r[0]))
    out, i = [], 0
    keys = [norm(w["text"]) for w in words]
    while i < len(words):
        hit = None
        for pat, rep in rules:
            n = len(pat)
            if n and keys[i:i + n] == pat:
                hit = (n, rep)
                break
        if hit:
            n, rep = hit
            last = words[i + n - 1]
            trail = re.search(r"[^\w%]+$", last["text"])
            out.append({"text": rep + (trail.group(0) if trail else ""), "start": words[i]["start"],
                        "end": last["end"], "raw": " ".join(w["text"] for w in words[i:i + n]), "fixed": True})
            i += n
        else:
            out.append({**words[i], "raw": words[i]["text"], "fixed": False})
            i += 1
    return out


def mark_fillers(words: list[dict], gl: dict) -> None:
    fillers = {norm(f) for f in gl.get("fillers", [])}
    for k, w in enumerate(words):
        key = norm(w["text"])
        is_f = key in fillers
        if is_f and key in AMBIGUOUS_FILLERS:
            gap_before = w["start"] - words[k - 1]["end"] if k else 1.0
            gap_after = words[k + 1]["start"] - w["end"] if k + 1 < len(words) else 1.0
            is_f = gap_before >= 0.12 or gap_after >= 0.12 or w["text"].endswith((",", "..."))
        w["filler"] = bool(is_f)


def build_segments(words: list[dict], gap: float = 0.35) -> list[dict]:
    segs, cur = [], []
    for k, w in enumerate(words):
        if cur and (w["start"] - words[k - 1]["end"] >= gap):
            segs.append(cur)
            cur = []
        cur.append(k)
        if re.search(r"[.?!…]$", w["text"]):
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return [{"id": i, "start": words[s[0]]["start"], "end": words[s[-1]]["end"],
             "text": " ".join(words[j]["text"] for j in s), "word_ids": s} for i, s in enumerate(segs)]


SNAP_MIN_SILENCE = 0.3  # only trust silencedetect gaps at least this long
MIN_WORD = 0.08


def snap_to_silences(words: list[dict], silences: list[list[float]]) -> list[dict]:
    """Chunked Whisper (PhoWhisper pipeline) often stretches one word across a long pause
    (e.g. 'thứ' 14.86-18.74 s over a 15.00-18.46 s silence). That hides sentence breaks and makes the
    cut step keep the pause. Trim each word off any measured silence it overlaps: a silence touching the
    word's start/end moves that edge; a silence inside the word keeps the longer speaking side."""
    gaps = [(a, b) for a, b in silences if b - a >= SNAP_MIN_SILENCE]
    out = []
    for w in words:
        s, e = w["start"], w["end"]
        for a, b in gaps:
            if b <= s or a >= e:
                continue
            if a <= s and b < e:      # silence covers the start
                s = b
            elif a > s and b >= e:    # silence covers the end
                e = a
            elif a > s and b < e:     # silence inside: word is on the longer side
                s, e = (s, a) if a - s >= e - b else (b, e)
            # silence covers the whole word: leave it, nothing better to anchor to
        if e - s < MIN_WORD:
            s, e = w["start"], w["end"]
        out.append({**w, "start": round(s, 3), "end": round(e, 3)})
    return out


def drop_tail_hallucinations(words: list[dict], duration: float) -> list[dict]:
    """Whisper repeats lines / invents words past the end of the media."""
    return [w for w in words if w["start"] < duration - 0.05]


def _rel(p: Path) -> str:
    """Store project-relative paths so shared JSON never leaks the local home directory."""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return Path(p).name


def transcribe(media: Path, out_json: Path, engine: str = "mlx") -> dict:
    media = Path(media)
    wav = out_json.parent / (out_json.stem + ".wav")
    extract_audio(media, wav)
    gl = _glossary()
    # Prompt nudges Whisper to keep disfluencies (needed to find fillers/retakes) and spell tool names.
    prompt = "Ừm, à, ờ... thì, ừm, mình nói lại nhé. " + ", ".join(gl.get("prompt_terms", [])) + "."
    raw = _run_mlx(wav, prompt) if engine == "mlx" else _run_pho(wav, prompt)
    duration = ffprobe_duration(media)
    silences = detect_silences(wav)
    words = drop_tail_hallucinations(_merge_tokens(raw), duration)
    words = snap_to_silences(words, silences)
    words = apply_glossary(words, gl)
    mark_fillers(words, gl)
    for i, w in enumerate(words):
        w["i"] = i
    words = [{"i": w["i"], "text": w["text"], "start": w["start"], "end": w["end"], "raw": w["raw"],
              "fixed": w["fixed"], "filler": w["filler"]} for w in words]
    data = {
        "source": _rel(media),
        "duration": round(duration, 3),
        "language": "vi",
        "engine": MLX_MODEL if engine == "mlx" else _pho_model(),
        "words": words,
        "segments": build_segments(words),
        "silences": silences,
    }
    save(out_json, data)
    return data
