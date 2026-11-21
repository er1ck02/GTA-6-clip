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
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:  # ctranslate2 missing or broken CUDA install: CPU is always safe
        return "cpu"


def get_backend(cfg: TranscriptionCfg, device: str, models_dir: Path) -> TranscriptionBackend:
    if cfg.backend == "faster_whisper":
        from clipstudio.transcription.faster_whisper_backend import FasterWhisperBackend

        return FasterWhisperBackend(cfg, resolve_device(device), models_dir)
    raise ValueError(f"unknown transcription backend {cfg.backend!r}")
