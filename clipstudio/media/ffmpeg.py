"""Thin, explicit wrappers around the ffmpeg / ffprobe executables.

FFmpeg is the single media engine in this project: decoding, analysis filters,
compositing, subtitle burn-in and encoding all go through here.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import numpy as np

from clipstudio.core.errors import DependencyMissing, MediaError
from clipstudio.core.logging import get_logger
from clipstudio.core.models import MediaInfo

log = get_logger("ffmpeg")


def ffmpeg_bin() -> str:
    p = shutil.which("ffmpeg")
    if not p:
        raise DependencyMissing("ffmpeg was not found on PATH. See README > Installing FFmpeg.")
    return p


def ffprobe_bin() -> str:
    p = shutil.which("ffprobe")
    if not p:
        raise DependencyMissing("ffprobe was not found on PATH. It ships with FFmpeg.")
    return p


def run(args: list[str], *, check: bool = True, capture: bool = True, timeout: float | None = None,
        cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a command. On Ctrl+C the child is killed before the interrupt propagates."""
    log.debug("exec %s", " ".join(_q(a) for a in args))
    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        cwd=str(cwd) if cwd else None,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    cp = subprocess.CompletedProcess(args, proc.returncode, out, err)
    if check and proc.returncode != 0:
        tail = (err or b"").decode(errors="replace").strip().splitlines()[-15:]
        raise MediaError(f"{Path(args[0]).name} exited with {proc.returncode}:\n" + "\n".join(tail))
    return cp


def _q(a: str) -> str:
    return a if re.fullmatch(r"[\w./:=+,-]+", a) else repr(a)


@lru_cache(maxsize=1)
def ffmpeg_capabilities() -> dict:
    out = run([ffmpeg_bin(), "-hide_banner", "-filters"]).stdout.decode(errors="replace")
    filters = {m.group(1) for m in re.finditer(r"^\s*[.TSC]{3}\s+(\w+)\s", out, re.M)}
    enc = run([ffmpeg_bin(), "-hide_banner", "-encoders"]).stdout.decode(errors="replace")
    encoders = {m.group(1) for m in re.finditer(r"^\s*[VAS][.FSXBD]{5}\s+(\S+)\s", enc, re.M)}
    ver = run([ffmpeg_bin(), "-version"]).stdout.decode(errors="replace").splitlines()[0]
    return {"version": ver, "filters": filters, "encoders": encoders}


def probe(path: Path) -> MediaInfo:
    if not path.exists():
        raise MediaError(f"file not found: {path}")
    cp = run(
        [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=False,
    )
    if cp.returncode != 0:
        raise MediaError(f"ffprobe could not read {path.name}: {cp.stderr.decode(errors='replace').strip()[:400]}")
    data = json.loads(cp.stdout)
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise MediaError(f"{path.name} has no video stream")
    fmt = data.get("format", {})
    duration = float(fmt.get("duration") or v.get("duration") or 0)
    if duration <= 0:
        raise MediaError(f"{path.name} reports no duration; the file may be truncated")
    return MediaInfo(
        path=str(path.resolve()),
        duration=duration,
        width=int(v["width"]),
        height=int(v["height"]),
        fps=_rate(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"),
        video_codec=v.get("codec_name", "unknown"),
        has_audio=a is not None,
        audio_codec=a.get("codec_name") if a else None,
        sample_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        channels=int(a["channels"]) if a and a.get("channels") else None,
        size_bytes=int(fmt.get("size") or path.stat().st_size),
        container=fmt.get("format_name", "unknown"),
        title=(fmt.get("tags") or {}).get("title"),
    )


def _rate(r: str) -> float:
    try:
        n, d = r.split("/")
        return float(n) / float(d) if float(d) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def extract_audio_wav(src: Path, dst: Path, sample_rate: int = 16000) -> Path:
    """Decode the source's first audio stream once to mono 16 kHz WAV.

    Transcription and loudness analysis both read this file, so the long
    source video is only decoded for audio a single time.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.wav")
    run([ffmpeg_bin(), "-v", "error", "-y", "-i", str(src), "-vn", "-map", "0:a:0", "-ac", "1",
         "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(tmp)])
    tmp.replace(dst)
    return dst


def read_wav_float(path: Path) -> tuple[np.ndarray, int]:
    """Read a 16-bit PCM mono WAV written by :func:`extract_audio_wav`."""
    import wave

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise MediaError(f"{path} is not mono 16-bit PCM")
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, sr
