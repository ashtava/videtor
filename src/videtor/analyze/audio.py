"""What a clip sounds like: loudness, silence, and whether anyone is speaking.

Everything decodes through ffmpeg so any container the camera or recorder
wrote works. Speech presence uses WebRTC's voice activity detector on 16 kHz
mono; it is a yes/no per 30 ms frame, cheap enough to run on every clip, and it
decides which clips are worth transcribing later.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

import numpy as np

try:  # the wheel is tiny but C-compiled; keep the module importable without it
    import webrtcvad
except ImportError:  # pragma: no cover
    webrtcvad = None

SAMPLE_RATE = 16000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000


@dataclass
class Span:
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


def decode_mono(path: str, sample_rate: int = SAMPLE_RATE, timeout: int = 600) -> np.ndarray | None:
    """The clip's audio as int16 mono at `sample_rate`; None if it has no audio stream."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"]
    try:
        run = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0 or not run.stdout:
        return None
    return np.frombuffer(run.stdout, dtype=np.int16)


def loudness(path: str, timeout: int = 600) -> dict | None:
    """EBU R128 summary from ffmpeg's ebur128 filter: integrated LUFS, range, true peak."""
    cmd = ["ffmpeg", "-v", "info", "-nostats", "-i", path, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
    try:  # ffmpeg echoes file metadata, which is not always UTF-8 (a Latin-1 © has killed a run)
        run = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    summary = run.stderr.rsplit("Summary:", 1)
    if len(summary) < 2:
        return None
    text = summary[1]

    def grab(label: str) -> float | None:
        m = re.search(rf"{label}:\s*(-?[\d.]+|-inf)", text)
        return None if not m or m.group(1) == "-inf" else float(m.group(1))

    integrated = grab("I")
    if integrated is None:
        return None
    return {"integrated_lufs": integrated, "range_lu": grab("LRA"), "true_peak_dbtp": grab("Peak")}


def silences(path: str, noise_db: float = -40.0, min_s: float = 0.5, timeout: int = 600) -> list[Span]:
    """Spans quieter than `noise_db` for at least `min_s`, from ffmpeg's silencedetect."""
    cmd = ["ffmpeg", "-v", "info", "-nostats", "-i", path, "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return []
    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+)", run.stderr)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[\d.]+)", run.stderr)]
    spans = [Span(max(s, 0.0), e) for s, e in zip(starts, ends)]
    if len(starts) > len(ends):  # silence running to the end of the clip
        spans.append(Span(max(starts[-1], 0.0), float("inf")))
    return spans


def speech_spans(
    pcm: np.ndarray, aggressiveness: int = 3, min_speech_s: float = 0.3, merge_gap_s: float = 0.3, min_modulation: float = 0.25
) -> list[Span]:
    """Where WebRTC VAD hears speech: 30 ms decisions merged into spans, short blips dropped.

    The VAD also says yes to steady tones and some music, so each span must
    show a speech-like energy envelope: the frame RMS inside it varies
    (coefficient of variation ≥ `min_modulation`) the way syllables do, where a
    held note or a drone stays flat.
    """
    if webrtcvad is None or pcm.size < FRAME_SAMPLES:
        return []
    vad = webrtcvad.Vad(aggressiveness)
    n = pcm.size // FRAME_SAMPLES
    frames = pcm[: n * FRAME_SAMPLES].reshape(n, FRAME_SAMPLES)
    flags = [vad.is_speech(f.tobytes(), SAMPLE_RATE) for f in frames]
    rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1))
    step = FRAME_MS / 1000
    spans: list[Span] = []
    for i, on in enumerate(flags):
        if not on:
            continue
        t = i * step
        if spans and t - spans[-1].end <= merge_gap_s:
            spans[-1].end = t + step
        else:
            spans.append(Span(t, t + step))

    def modulated(span: Span) -> bool:
        env = rms[int(span.start / step) : int(span.end / step)]
        return env.size > 3 and env.mean() > 0 and float(env.std() / env.mean()) >= min_modulation

    return [s for s in spans if s.length >= min_speech_s and modulated(s)]


def audio_features(path: str, duration_s: float | None = None) -> dict:
    """One record per clip: loudness, silence share, speech share and spans, or {"audio": False}."""
    pcm = decode_mono(path)
    if pcm is None:
        return {"audio": False}
    length = duration_s or pcm.size / SAMPLE_RATE
    rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if pcm.size else 0.0
    speech = speech_spans(pcm)
    quiet = silences(path)
    quiet_s = sum(min(s.end, length) - s.start for s in quiet if s.start < length)
    return {
        "audio": True,
        "rms_dbfs": round(20 * np.log10(rms / 32768) if rms > 0 else -120.0, 2),
        "loudness": loudness(path),
        "silence_share": round(min(quiet_s / length, 1.0), 3) if length else None,
        "speech_share": round(min(sum(s.length for s in speech) / length, 1.0), 3) if length else None,
        "speech_s": round(sum(s.length for s in speech), 2),
        "speech_spans": [[round(s.start, 2), round(s.end, 2)] for s in speech[:50]],
        "speech": bool(speech) and sum(s.length for s in speech) >= 1.0,
    }
