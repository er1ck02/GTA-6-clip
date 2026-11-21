"""Chunked, resumable transcription: chunk planning and per-chunk caching (with a fake model)."""
import wave

import numpy as np
import pytest

from clipstudio.config import load_config
from clipstudio.core.models import Segment, Word
from clipstudio.transcription.faster_whisper_backend import FasterWhisperBackend, plan_chunks

SR = 16000


def _audio(seconds, quiet_at=()):
    rng = np.random.default_rng(0)
    a = (rng.standard_normal(seconds * SR) * 0.2).astype(np.float32)
    for t in quiet_at:
        a[int(t * SR):int((t + 1) * SR)] *= 0.001
    return a


def test_short_audio_is_one_chunk():
    assert plan_chunks(_audio(30), SR, 600) == [(0, 30 * SR)]
    assert plan_chunks(_audio(30), SR, 0) == [(0, 30 * SR)]


def test_chunks_cut_at_quiet_moments_and_cover_everything():
    a = _audio(100, quiet_at=(23.0, 52.0, 77.0))
    ch = plan_chunks(a, SR, 25, search_s=6)
    assert ch[0][0] == 0 and ch[-1][1] == len(a)
    assert all(x[1] == y[0] for x, y in zip(ch, ch[1:]))
    cuts = [c[0] / SR for c in ch[1:]]
    assert any(abs(c - 23.5) < 0.6 for c in cuts)


class _Info:
    language, language_probability = "en", 0.99


def _write_wav(path, a):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR)
        w.writeframes((a * 32767).astype(np.int16).tobytes())


def test_interrupted_run_resumes_from_cached_chunks(tmp_path, monkeypatch):
    cfg = load_config(overrides=["transcription.chunk_seconds=20"]).transcription
    wav = tmp_path / "a.wav"
    _write_wav(wav, _audio(70, quiet_at=(19.0, 41.0)))
    calls = []

    def fake_run(self, model, audio, offset, language):
        calls.append(round(offset))
        if len(calls) == 3 and fail[0]:
            raise KeyboardInterrupt  # user hits Ctrl+C during the third chunk
        w = Word(text=f"w{round(offset)}", start=offset + 1, end=offset + 1.4, probability=0.9)
        yield _Info(), Segment(id=0, start=w.start, end=w.end, text=w.text, words=[w])

    monkeypatch.setattr(FasterWhisperBackend, "_load", lambda self: object())
    monkeypatch.setattr(FasterWhisperBackend, "_run", fake_run)
    b = FasterWhisperBackend(cfg, "cpu", tmp_path)
    fail = [True]
    with pytest.raises(KeyboardInterrupt):
        b.transcribe(wav, 70.0, chunk_dir=tmp_path / "chunks")
    assert len(list((tmp_path / "chunks").glob("chunk_*.json"))) == 2
    fail[0] = False
    calls.clear()
    tr = b.transcribe(wav, 70.0, chunk_dir=tmp_path / "chunks")
    assert len(calls) == 1  # only the unfinished chunk ran again
    assert [s.id for s in tr.segments] == [0, 1, 2] and tr.language == "en"
    assert tr.segments[0].start < tr.segments[1].start < tr.segments[2].start
