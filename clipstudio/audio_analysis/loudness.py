"""Cheap whole-source audio analysis: short-window RMS loudness from the cached WAV."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from clipstudio.core.models import LoudnessTrack
from clipstudio.media.ffmpeg import wav_memmap, window_rms

FLOOR_DB = -90.0


def analyze_loudness(wav: Path, window: float) -> LoudnessTrack:
    audio, sr = wav_memmap(wav)
    n = max(1, int(round(window * sr)))
    frames = len(audio) // n
    if frames == 0:
        return LoudnessTrack(window=window, rms_db=[], median_db=FLOOR_DB, p90_db=FLOOR_DB)
    rms = window_rms(audio, n)
    db = np.maximum(20 * np.log10(np.maximum(rms, 1e-9)), FLOOR_DB)
    # tail remainder as a final window
    if len(audio) > frames * n:
        tail = np.asarray(audio[frames * n:], dtype=np.float64) / 32768.0
        db = np.append(db, max(20 * np.log10(max(np.sqrt(np.mean(tail * tail)), 1e-9)), FLOOR_DB))
    active = db[db > FLOOR_DB + 1]
    ref = active if active.size else db
    return LoudnessTrack(
        window=window,
        rms_db=[round(float(x), 2) for x in db],
        median_db=round(float(np.median(ref)), 2),
        p90_db=round(float(np.percentile(ref, 90)), 2),
    )


def slice_db(track: LoudnessTrack, start: float, end: float) -> np.ndarray:
    i0 = max(0, int(start / track.window))
    i1 = max(i0 + 1, int(np.ceil(end / track.window)))
    return np.asarray(track.rms_db[i0:i1], dtype=np.float64)
