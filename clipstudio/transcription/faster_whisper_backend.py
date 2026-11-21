"""faster-whisper (CTranslate2) backend: word timestamps, VAD, CPU int8 or CUDA fp16."""
from __future__ import annotations

from pathlib import Path

from clipstudio.config import TranscriptionCfg
from clipstudio.core.errors import DependencyMissing, StageFailed
from clipstudio.core.logging import get_logger
from clipstudio.core.models import Segment, Transcript, Word
from clipstudio.media.ffmpeg import read_wav_float
from clipstudio.transcription.base import ProgressFn

log = get_logger("transcription")


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

    def transcribe(self, wav: Path, duration: float, progress: ProgressFn | None = None) -> Transcript:
        model = self._load()
        audio, sr = read_wav_float(wav)
        assert sr == 16000, "extract_audio_wav must produce 16 kHz"
        segments_iter, info = model.transcribe(
            audio,
            language=self.cfg.language,
            beam_size=self.cfg.beam_size,
            vad_filter=self.cfg.vad_filter,
            word_timestamps=self.cfg.word_timestamps,
            condition_on_previous_text=False,  # reduces hallucination loops on long gaming audio
        )
        segments: list[Segment] = []
        for i, s in enumerate(segments_iter):
            words = [
                Word(text=w.word.strip(), start=round(float(w.start), 3), end=round(float(w.end), 3),
                     probability=round(float(w.probability), 4))
                for w in (s.words or []) if w.word.strip()
            ]
            segments.append(Segment(
                id=i, start=round(float(s.start), 3), end=round(float(s.end), 3), text=s.text.strip(),
                words=words, avg_logprob=float(s.avg_logprob), no_speech_prob=float(s.no_speech_prob),
            ))
            if progress and duration > 0:
                progress(min(1.0, float(s.end) / duration))
        del model
        return Transcript(
            backend=self.name, model=self.cfg.model, language=info.language,
            language_probability=float(info.language_probability), duration=duration, segments=segments,
        )
