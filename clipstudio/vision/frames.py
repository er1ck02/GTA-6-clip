"""Decode small RGB frames from a video with FFmpeg (no OpenCV needed)."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from clipstudio.core.errors import MediaError
from clipstudio.media.ffmpeg import ffmpeg_bin, run


def frame_size(src_w: int, src_h: int, width: int) -> tuple[int, int]:
    width = min(width, src_w - src_w % 2)
    h = max(2, int(round(width * src_h / src_w / 2.0)) * 2)
    return width, h


def sample_range(path: Path, start: float, end: float, fps: float, src_w: int, src_h: int,
                 width: int = 320) -> tuple[np.ndarray, np.ndarray]:
    """Frames at ``fps`` between start and end. Returns (times, frames[n,h,w,3] uint8).

    Times are absolute source seconds.
    """
    w, h = frame_size(src_w, src_h, width)
    dur = max(0.05, end - start)
    cp = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
              "-i", str(path), "-an", "-sn", "-vf", f"fps={fps:g},scale={w}:{h}:flags=area",
              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    buf = np.frombuffer(cp.stdout, np.uint8)
    n = buf.size // (w * h * 3)
    if n == 0:
        raise MediaError(f"no frames decoded from {path.name} between {start:.2f}s and {end:.2f}s")
    frames = buf[: n * w * h * 3].reshape(n, h, w, 3)
    times = start + np.arange(n) / fps
    return times, frames


def sample_at(path: Path, times: list[float], src_w: int, src_h: int, width: int = 960) -> tuple[np.ndarray, np.ndarray]:
    """One frame at each time (fast input seeking). Frames that fail to decode are skipped."""
    w, h = frame_size(src_w, src_h, width)
    got_t, got = [], []
    for t in times:
        cp = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                  "-an", "-sn", "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=area",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=False)
        if cp.returncode == 0 and len(cp.stdout) >= w * h * 3:
            got.append(np.frombuffer(cp.stdout[: w * h * 3], np.uint8).reshape(h, w, 3))
            got_t.append(t)
    if not got:
        raise MediaError(f"could not decode any frames from {path.name}")
    return np.array(got_t), np.stack(got)
