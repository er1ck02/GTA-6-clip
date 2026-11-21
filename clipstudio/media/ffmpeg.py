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
from clipstudio.core.models import AudioStream, MediaInfo

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


def stream_raw(args: list[str], frame_bytes: int, batch: int = 64):
    """Run ffmpeg writing raw frames to stdout and yield them in batches (bytes), without holding the whole video.

    The child is killed if the consumer stops early or on Ctrl+C.
    """
    log.debug("exec %s", " ".join(_q(a) for a in args))
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        while True:
            buf = proc.stdout.read(frame_bytes * batch)  # type: ignore[union-attr]
            if not buf:
                break
            n = len(buf) // frame_bytes
            if n:
                yield buf[: n * frame_bytes]
        proc.wait()
        if proc.returncode != 0:
            tail = proc.stderr.read().decode(errors="replace").strip().splitlines()[-10:]  # type: ignore[union-attr]
            raise MediaError(f"ffmpeg exited with {proc.returncode}:\n" + "\n".join(tail))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


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
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    a = audios[0] if audios else None
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
        audio_streams=[
            AudioStream(order=i, codec=s.get("codec_name", "unknown"), channels=s.get("channels"),
                        title=(s.get("tags") or {}).get("title") or (s.get("tags") or {}).get("handler_name"))
            for i, s in enumerate(audios)
        ],
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


def audio_mix_filter(n_tracks: int, label_out: str = "amix") -> str | None:
    """Filtergraph that sums all audio tracks (game + mic recordings) without level normalisation."""
    if n_tracks <= 1:
        return None
    ins = "".join(f"[0:a:{i}]" for i in range(n_tracks))
    return f"{ins}amix=inputs={n_tracks}:normalize=0:duration=longest[{label_out}]"


def extract_audio_wav(src: Path, dst: Path, tracks: list[int] | None = None, sample_rate: int = 16000,
                      speech_normalize: bool = False) -> Path:
    """Decode audio once to mono 16 kHz WAV.

    ``tracks`` selects audio streams by order; several are summed (e.g. Game + Mic
    tracks from OBS/SteelSeries/Medal recordings). ``speech_normalize`` evens out
    quiet microphone tracks for speech recognition only; it never touches the
    output audio.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp.wav")
    tracks = tracks or [0]
    post = ",dynaudnorm=f=250:g=15:p=0.9" if speech_normalize else ""
    if len(tracks) == 1:
        args = ["-map", f"0:a:{tracks[0]}"] + (["-af", post.lstrip(",")] if post else [])
    else:
        ins = "".join(f"[0:a:{i}]" for i in tracks)
        args = ["-filter_complex", f"{ins}amix=inputs={len(tracks)}:normalize=0:duration=longest{post}[a]", "-map", "[a]"]
    run([ffmpeg_bin(), "-v", "error", "-y", "-i", str(src), "-vn", *args, "-ac", "1",
         "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(tmp)])
    tmp.replace(dst)
    return dst


def pick_speech_track(info: MediaInfo, setting: str | int) -> list[int]:
    """Which audio streams to transcribe. 'auto' prefers a stream named like a microphone."""
    n = len(info.audio_streams)
    if n == 0:
        return []
    if isinstance(setting, int) or (isinstance(setting, str) and setting.isdigit()):
        i = int(setting)
        if not 0 <= i < n:
            raise MediaError(f"speech_track {i} does not exist; the source has {n} audio stream(s)")
        return [i]
    if setting == "auto":
        for s in info.audio_streams:
            if s.title and re.search(r"\b(mic|microphone|voice|commentary)\b", s.title, re.I):
                return [s.order]
    return list(range(n))  # 'mix' or nothing recognisable: everything


def wav_memmap(path: Path) -> tuple[np.ndarray, int]:
    """Memory-map the samples of a 16-bit PCM mono WAV (no copy: an 8-hour VOD is ~0.9 GB on disk, 0 in RAM)."""
    import struct

    with open(path, "rb") as fh:
        head = fh.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise MediaError(f"{path} is not a WAV file")
        sr = None
        while True:
            hdr = fh.read(8)
            if len(hdr) < 8:
                raise MediaError(f"{path}: no data chunk")
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = fh.read(size)
                ch, sr, bits = struct.unpack("<H", fmt[2:4])[0], struct.unpack("<I", fmt[4:8])[0], struct.unpack("<H", fmt[14:16])[0]
                if ch != 1 or bits != 16:
                    raise MediaError(f"{path} is not mono 16-bit PCM")
            elif cid == b"data":
                offset = fh.tell()
                break
            else:
                fh.seek(size + (size & 1), 1)
    if sr is None:
        raise MediaError(f"{path}: fmt chunk missing")
    n = min(size, path.stat().st_size - offset) // 2  # tolerate a data size of 0xFFFFFFFF from a streamed write
    return np.memmap(path, dtype=np.int16, mode="r", offset=offset, shape=(n,)), sr


def window_rms(samples: np.ndarray, win: int, block_windows: int = 8192) -> np.ndarray:
    """RMS of consecutive ``win``-sample windows (full-scale 1.0), computed in blocks so memory stays flat."""
    scale = 1.0 / 32768.0 if samples.dtype == np.int16 else 1.0
    nwin = len(samples) // win
    out = np.empty(nwin, dtype=np.float64)
    for i in range(0, nwin, block_windows):
        j = min(nwin, i + block_windows)
        blk = np.asarray(samples[i * win:j * win], dtype=np.float64).reshape(j - i, win) * scale
        out[i:j] = np.sqrt(np.mean(blk * blk, axis=1))
    return out


def read_wav_float(path: Path) -> tuple[np.ndarray, int]:
    """Read a 16-bit PCM mono WAV written by :func:`extract_audio_wav`."""
    import wave

    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise MediaError(f"{path} is not mono 16-bit PCM")
        sr = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return data.astype(np.float32) / 32768.0, sr
