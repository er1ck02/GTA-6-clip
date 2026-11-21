"""Unified transcription interface. Backends turn a 16 kHz mono WAV into a Transcript."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from clipstudio.config import TranscriptionCfg
from clipstudio.core.models import Transcript

ProgressFn = Callable[[float], None]


class TranscriptionBackend(Protocol):
    name: str

    def transcribe(self, wav: Path, duration: float, progress: ProgressFn | None = None,
                   chunk_dir: Path | None = None) -> Transcript: ...


def resolve_device(requested: str) -> str:
    if requested in ("cpu", "cuda"):
        return requested
    from clipstudio.core.compat import cuda_usable

    ok, detail = cuda_usable()
    if not ok and "device(s)" in detail:  # a GPU exists but cannot be used: say why instead of failing later
        from clipstudio.core.logging import get_logger

        get_logger("transcription").warning("GPU not used for transcription: %s", detail)
    return "cuda" if ok else "cpu"


def get_backend(cfg: TranscriptionCfg, device: str, models_dir: Path) -> TranscriptionBackend:
    if cfg.backend == "faster_whisper":
        from clipstudio.transcription.faster_whisper_backend import FasterWhisperBackend

        return FasterWhisperBackend(cfg, resolve_device(device), models_dir)
    raise ValueError(f"unknown transcription backend {cfg.backend!r}")
