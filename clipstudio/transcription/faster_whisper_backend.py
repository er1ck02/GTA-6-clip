"""faster-whisper (CTranslate2) backend: word timestamps, VAD, CPU int8 or CUDA fp16."""
from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from clipstudio.core.compat import replace_file
from clipstudio.config import TranscriptionCfg
from clipstudio.core.errors import DependencyMissing, StageFailed
from clipstudio.core.logging import get_logger
from clipstudio.core.models import Segment, Transcript, Word
from clipstudio.media.ffmpeg import wav_memmap, window_rms
from clipstudio.transcription.base import ProgressFn

log = get_logger("transcription")


_models: dict[tuple, object] = {}
_models_lock = threading.Lock()


class FasterWhisperBackend:
    name = "faster_whisper"

    def __init__(self, cfg: TranscriptionCfg, device: str, models_dir: Path):
        self.cfg = cfg
        self.device = device
        self.models_dir = models_dir
        if cfg.compute_type != "auto":
            self.compute_type = cfg.compute_type
        else:
            self.compute_type = "float16" if device == "cuda" else "int8"

    def _load(self):
        """The whisper model, loaded once per process and settings (live mode edits many moments; loading it for
        each one cost seconds and leaked CTranslate2 threads)."""
        key = (self.cfg.model, self.device, self.compute_type, str(self.models_dir))
        with _models_lock:
            if key not in _models:
                _models[key] = self._load_new()
            return _models[key]

    def _load_new(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise DependencyMissing("faster-whisper is not installed: pip install -r requirements.txt") from e
        model = self.cfg.model
        local = Path(model).expanduser()
        if local.is_dir():
            model = str(local)
        log.info("loading whisper model %s on %s (%s)", model, self.device, self.compute_type)
        try:
            return WhisperModel(model, device=self.device, compute_type=self.compute_type,
                                download_root=str(self.models_dir / "whisper"))
        except Exception as e:  # network failure on first download, bad path, CUDA libs missing
            raise StageFailed(
                "transcribe",
                f"could not load whisper model {model!r} on {self.device}: {e}. "
                "If this is a first run, the model is downloaded from Hugging Face; "
                "set transcription.model to a local CTranslate2 model directory to work offline.",
            ) from e

    def _run(self, model, audio: np.ndarray, offset: float, language: str | None):
        segments_iter, info = model.transcribe(
            audio,
            language=language,
            beam_size=self.cfg.beam_size,
            vad_filter=self.cfg.vad_filter,
            word_timestamps=self.cfg.word_timestamps,
            condition_on_previous_text=False,  # reduces hallucination loops on long gaming audio
        )
        for s in segments_iter:
            words = [
                Word(text=w.word.strip(), start=round(float(w.start) + offset, 3), end=round(float(w.end) + offset, 3),
                     probability=round(float(w.probability), 4))
                for w in (s.words or []) if w.word.strip()
            ]
            yield info, Segment(
                id=0, start=round(float(s.start) + offset, 3), end=round(float(s.end) + offset, 3), text=s.text.strip(),
                words=words, avg_logprob=float(s.avg_logprob), no_speech_prob=float(s.no_speech_prob),
            )

    def transcribe(self, wav: Path, duration: float, progress: ProgressFn | None = None,
                   chunk_dir: Path | None = None) -> Transcript:
        audio, sr = wav_memmap(wav)  # int16, memory-mapped; each chunk is converted to float on its own
        assert sr == 16000, "extract_audio_wav must produce 16 kHz"
        model = self._load()
        chunks = plan_chunks(audio, sr, self.cfg.chunk_seconds)
        language = self.cfg.language
        lang_prob = 1.0
        segments: list[Segment] = []
        for ci, (a, b) in enumerate(chunks):
            cache = chunk_dir / f"chunk_{ci:04d}_{a / sr:.2f}_{b / sr:.2f}.json" if chunk_dir and len(chunks) > 1 else None
            if cache is not None and cache.exists():
                d = json.loads(cache.read_text(encoding="utf-8"))
                segs = [Segment.model_validate(x) for x in d["segments"]]
                language, lang_prob = d["language"], d["language_probability"]
                log.info("    chunk %d/%d cached (%.0f-%.0fs)", ci + 1, len(chunks), a / sr, b / sr)
            else:
                segs = []
                for info, seg in self._run(model, np.asarray(audio[a:b], dtype=np.float32) / 32768.0, a / sr, language):
                    segs.append(seg)
                    if progress and duration > 0:
                        progress(min(1.0, seg.end / duration))
                if language is None or ci == 0:
                    # the first chunk's language is reused so a multi-hour VOD stays in one language
                    try:
                        language, lang_prob = info.language, float(info.language_probability)  # type: ignore[possibly-undefined]
                    except NameError:  # chunk without any speech
                        pass
                if cache is not None:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache.with_suffix(".part")
                    tmp.write_text(json.dumps({"language": language, "language_probability": lang_prob,
                                               "segments": [x.model_dump() for x in segs]}), encoding="utf-8")
                    replace_file(tmp, cache)
                    if len(chunks) > 1:
                        log.info("    chunk %d/%d done (%.0f-%.0fs)", ci + 1, len(chunks), a / sr, b / sr)
            segments.extend(segs)
            if progress and duration > 0:
                progress(min(1.0, (b / sr) / duration))
        del model
        for i, s in enumerate(segments):
            s.id = i
        return Transcript(
            backend=self.name, model=self.cfg.model, language=language or "unknown",
            language_probability=lang_prob, duration=duration, segments=segments,
        )


def plan_chunks(audio: np.ndarray, sr: int, chunk_seconds: int, search_s: float = 30.0) -> list[tuple[int, int]]:
    """Sample ranges of roughly ``chunk_seconds``, each cut at the quietest half second near the target."""
    n = len(audio)
    if chunk_seconds <= 0 or n <= sr * chunk_seconds * 1.25:
        return [(0, n)]
    win = sr // 2
    nwin = n // win
    rms = window_rms(audio, win) + 1e-6
    cuts = [0]
    target = chunk_seconds * sr
    while n - cuts[-1] > target * 1.25:
        want = cuts[-1] + target
        lo = max(cuts[-1] // win + 1, int((want - search_s * sr) // win))
        hi = min(nwin - 1, int((want + search_s * sr) // win))
        k = lo + int(np.argmin(rms[lo:hi + 1])) if hi > lo else want // win
        cuts.append(int(k * win + win // 2))
    cuts.append(n)
    return list(zip(cuts[:-1], cuts[1:]))
