"""Narration voice: free neural text-to-speech, cached per line.

Engines, tried in order (``engines``):

* ``edge``   Microsoft Edge's online neural voices through the ``edge-tts`` package. Free, no account or key,
  the most natural of the free options (the "Multilingual" voices sound conversational). Needs internet.
* ``kokoro`` Kokoro-82M, an open-weight (Apache-2.0) local model through the ``kokoro`` package. Offline once
  the model is downloaded; good quality; needs PyTorch.
* ``silent`` silence sized like speech (about 2.6 words/s). Only for tests and dry runs; never the default.

Each line is rendered once to ``<cache>/<sha>.wav`` (48 kHz stereo), so re-building an episode after a script
tweak only re-speaks the lines that changed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import wave
from dataclasses import dataclass, field
from pathlib import Path

from clipstudio.core.logging import get_logger
from clipstudio.media.ffmpeg import ffmpeg_bin, run

log = get_logger("voice")

DEFAULT_EDGE_VOICE = "en-US-BrianMultilingualNeural"   # casual, upbeat male narrator
DEFAULT_KOKORO_VOICE = "am_michael"


@dataclass
class VoiceCfg:
    engines: list[str] = field(default_factory=lambda: ["edge", "kokoro"])
    edge_voice: str = DEFAULT_EDGE_VOICE
    edge_rate: str = "+6%"          # a touch faster than default reads less robotic for hype lines
    edge_pitch: str = "+0Hz"
    kokoro_voice: str = DEFAULT_KOKORO_VOICE
    kokoro_speed: float = 1.05
    cache_dir: Path = Path("formats_cache/voice")

    @classmethod
    def from_dict(cls, d: dict | None, cache_dir: Path | None = None) -> "VoiceCfg":
        d = dict(d or {})
        if cache_dir is not None:
            d.setdefault("cache_dir", cache_dir)
        if "cache_dir" in d:
            d["cache_dir"] = Path(d["cache_dir"])
        if isinstance(d.get("engines"), str):
            d["engines"] = [d["engines"]]
        return cls(**d)


class VoiceUnavailable(RuntimeError):
    pass


def _key(text: str, engine: str, cfg: VoiceCfg) -> str:
    params = {"edge": [cfg.edge_voice, cfg.edge_rate, cfg.edge_pitch], "kokoro": [cfg.kokoro_voice, cfg.kokoro_speed],
              "silent": []}.get(engine, [])
    return hashlib.sha1(json.dumps([text, engine, params]).encode()).hexdigest()[:16]


def speak(text: str, cfg: VoiceCfg) -> Path:
    """Return a 48 kHz stereo WAV of ``text`` from the first engine that works."""
    text = " ".join(text.split())
    if not text:
        raise ValueError("empty narration line")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    for engine in cfg.engines:
        out = cfg.cache_dir / f"{engine}-{_key(text, engine, cfg)}.wav"
        if out.exists() and out.stat().st_size > 1000:
            return out
        try:
            _ENGINES[engine](text, out, cfg)
            return out
        except Exception as e:  # noqa: BLE001 - try the next engine
            errors.append(f"{engine}: {e}")
            log.warning("voice engine %s failed: %s", engine, e)
            out.unlink(missing_ok=True)
    raise VoiceUnavailable("no voice engine worked: " + "; ".join(errors))


def duration(wav: Path) -> float:
    with wave.open(str(wav), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _to_wav48(src: Path, dst: Path) -> None:
    tmp = dst.with_suffix(".tmp.wav")
    run([ffmpeg_bin(), "-hide_banner", "-v", "error", "-y", "-i", str(src), "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le",
         "-af", "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
                "silenceremove=start_periods=1:start_threshold=-50dB,areverse", str(tmp)])
    tmp.replace(dst)


def _edge(text: str, out: Path, cfg: VoiceCfg) -> None:
    try:
        import edge_tts  # type: ignore
    except ImportError as e:
        raise VoiceUnavailable("pip install edge-tts") from e
    mp3 = out.with_suffix(".mp3")

    async def go():
        await edge_tts.Communicate(text, cfg.edge_voice, rate=cfg.edge_rate, pitch=cfg.edge_pitch).save(str(mp3))
    asyncio.run(go())
    if not mp3.exists() or mp3.stat().st_size < 500:
        raise VoiceUnavailable("edge-tts returned no audio")
    _to_wav48(mp3, out)
    mp3.unlink(missing_ok=True)


_KOKORO = {}


def _kokoro(text: str, out: Path, cfg: VoiceCfg) -> None:
    try:
        import numpy as np
        from kokoro import KPipeline  # type: ignore
    except ImportError as e:
        raise VoiceUnavailable("pip install kokoro soundfile") from e
    pipe = _KOKORO.get("a") or _KOKORO.setdefault("a", KPipeline(lang_code="a"))
    parts = [np.asarray(a, dtype=np.float32) for _, _, a in pipe(text, voice=cfg.kokoro_voice, speed=cfg.kokoro_speed)]
    if not parts:
        raise VoiceUnavailable("kokoro returned no audio")
    pcm = (np.clip(np.concatenate(parts), -1, 1) * 32767).astype("<i2")
    raw = out.with_suffix(".24k.wav")
    with wave.open(str(raw), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm.tobytes())
    _to_wav48(raw, out)
    raw.unlink(missing_ok=True)


def _silent(text: str, out: Path, cfg: VoiceCfg) -> None:
    secs = max(0.8, len(text.split()) / 2.6)
    import numpy as np
    n = int(secs * 48000)
    # a quiet tone, not digital silence: loudness normalisation of pure zeros yields NaN
    tone = (np.sin(np.arange(n) * 2 * np.pi * 220 / 48000) * 600).astype("<i2")
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(48000); w.writeframes(np.repeat(tone, 2).tobytes())


_ENGINES = {"edge": _edge, "kokoro": _kokoro, "silent": _silent}
