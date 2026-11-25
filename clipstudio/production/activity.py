"""Is anything happening on screen? A sleep stream keeps a GTA title but shows a still room: measure how much
the picture changes between frames a fraction of a second apart, on a few recent segments of the recording."""
from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

from clipstudio.core.logging import get_logger

log = get_logger("production")

W, H = 64, 36


def frames(path: Path, n: int = 3, fps: float = 2.0, timeout: float = 30) -> np.ndarray | None:
    """``n`` tiny grayscale frames, 1/fps apart, from the start of a segment (None if ffmpeg can't read it)."""
    from clipstudio.media.ffmpeg import ffmpeg_bin

    try:
        cp = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path),
                             "-vf", f"fps={fps},scale={W}:{H},format=gray", "-frames:v", str(n),
                             "-f", "rawvideo", "-"], capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.debug("motion: %s: %s", path, e)
        return None
    k = len(cp.stdout) // (W * H)
    if cp.returncode or k < 2:
        return None
    return np.frombuffer(cp.stdout[: k * W * H], dtype=np.uint8).reshape(k, H, W).astype(np.float32)


def motion(paths: list[Path], sample: int = 8, read=frames) -> float | None:
    """Median over up to ``sample`` segments (spread across ``paths``) of the mean absolute pixel change between
    consecutive frames, 0-255. Gameplay is well above 3; a still room with someone asleep is close to 0-1."""
    if not paths:
        return None
    step = max(1, len(paths) // sample)
    scores = []
    for p in paths[::step][:sample]:
        f = read(p)
        if f is not None:
            scores.append(float(np.abs(np.diff(f, axis=0)).mean()))
    return float(np.median(scores)) if scores else None
