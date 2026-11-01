"""What is said in a take, word by word: local Whisper on the Mac's GPU (mlx-whisper), cached per take.

For interview pieces: a paper edit picks sentences from the transcripts, the
fumbles inside them (fillers, a phrase started twice, a long pause, a word
Whisper itself doubts) are cut out, and B-roll covers the joins. Brand and
product names go in as a prompt so Whisper spells them the way the brief does.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HUB_MODEL = "mlx-community/whisper-large-v3-turbo"
LOCAL_MODEL = Path("cache/models/whisper-large-v3-turbo")  # config.json + weights.safetensors, fetched once
MODEL = str(LOCAL_MODEL) if (LOCAL_MODEL / "weights.safetensors").exists() and (LOCAL_MODEL / "config.json").exists() else HUB_MODEL
CACHE_DIR = Path("cache/transcripts")
FILLERS = {"um", "umm", "uh", "uhh", "erm", "hmm", "ah", "eh", "mm"}
PAUSE_S = 0.8  # a silence this long inside a sentence reads as a fumble on screen


def _clean(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def transcribe(path: str | Path, fid: str, cache_dir: Path = CACHE_DIR, prompt: str | None = None, language: str | None = None) -> dict:
    """{fid, path, language, segments: [{start, end, text, avg_logprob, no_speech_prob, words: [{w, start, end, p}]}]}."""
    out = Path(cache_dir) / f"{fid}.json"
    if out.exists():
        return json.loads(out.read_text())
    import mlx_whisper  # loads the model on first use (downloaded once from the Hugging Face hub)

    r = mlx_whisper.transcribe(
        str(path), path_or_hf_repo=MODEL, word_timestamps=True, condition_on_previous_text=False,
        initial_prompt=prompt, language=language, verbose=None, hallucination_silence_threshold=2.0,
    )
    segments = []
    for s in r.get("segments", []):
        if s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -1.0:
            continue  # music or room tone that Whisper tried to read as words
        words = [{"w": w["word"].strip(), "start": round(w["start"], 3), "end": round(w["end"], 3), "p": round(w.get("probability", 1.0), 3)}
                 for w in s.get("words", []) if w.get("word", "").strip()]
        segments.append({"start": round(s["start"], 3), "end": round(s["end"], 3), "text": s["text"].strip(),
                         "avg_logprob": round(s.get("avg_logprob", 0), 3), "no_speech_prob": round(s.get("no_speech_prob", 0), 3), "words": words})
    data = {"fid": fid, "path": str(path), "language": r.get("language"), "model": MODEL, "segments": segments,
            "text": " ".join(s["text"] for s in segments)}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1) + "\n")
    return data


def words(data: dict) -> list[dict]:
    return [w for s in data.get("segments", []) for w in s["words"]]


def fumbles(ws: list[dict], pause_s: float = PAUSE_S) -> list[dict]:
    """Spans to cut inside a run of words: fillers, a word or phrase said twice in a row (the first try goes),
    long pauses, and words Whisper doubts. Each {start, end, kind, text}."""
    out = []
    clean = [_clean(w["w"]) for w in ws]
    for i, w in enumerate(ws):
        if clean[i] in FILLERS:
            out.append({"start": w["start"], "end": w["end"], "kind": "filler", "text": w["w"]})
        elif w.get("p", 1.0) < 0.25:
            out.append({"start": w["start"], "end": w["end"], "kind": "doubtful", "text": w["w"]})
        if i and w["start"] - ws[i - 1]["end"] > pause_s:
            out.append({"start": ws[i - 1]["end"], "end": w["start"], "kind": "pause", "text": f"{w['start'] - ws[i - 1]['end']:.1f}s"})
    # restarts: a phrase of 1-4 words repeated straight away ("the the", "we wanted to, we wanted to")
    for n in (4, 3, 2, 1):
        i = 0
        while i + 2 * n <= len(ws):
            a, b = clean[i : i + n], clean[i + n : i + 2 * n]
            if a == b and all(a) and ws[i + n]["start"] - ws[i]["start"] < 4.0:
                out.append({"start": ws[i]["start"], "end": ws[i + n]["start"], "kind": "restart", "text": " ".join(x["w"] for x in ws[i : i + n])})
                i += n
            else:
                i += 1
    return sorted(out, key=lambda f: f["start"])


def sentences(data: dict) -> list[dict]:
    """The transcript split at sentence ends: {start, end, text, words}."""
    out, cur = [], []
    for w in words(data):
        cur.append(w)
        if re.search(r"[.!?]$", w["w"]):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [{"start": s[0]["start"], "end": s[-1]["end"], "text": " ".join(w["w"] for w in s), "words": s} for s in out]


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe the takes of a footage pool (cached per take).")
    ap.add_argument("index", type=Path)
    ap.add_argument("--key", required=True)
    ap.add_argument("--prompt", default=None, help="Names Whisper should spell right, e.g. 'Mia, Lightscape collection'.")
    ap.add_argument("--min-speech", type=float, default=0.0, help="Skip takes shorter than this many seconds.")
    args = ap.parse_args()
    from videtor.analyze import footmap

    index = json.loads(args.index.read_text())
    pool = sorted(footmap.pool(index, args.key), key=lambda e: e["basename"])
    for e in pool:
        if not (e.get("media") or {}).get("audio") or (e["media"].get("duration_s") or 0) < args.min_speech:
            continue
        data = transcribe(e["path"], e["fid"], prompt=args.prompt)
        n = len(words(data))
        print(f"{e['basename']:12} {e['media']['duration_s']:6.1f}s  {n:4} words  {data['text'][:90]}", flush=True)


if __name__ == "__main__":
    main()
