"""AI upscaling of a small webcam picture with Real-ESRGAN (optional).

A streamer's webcam can be 170 px wide in the stream and has to fill a third of a 1080x1920 clip. Classic
scaling only smooths that; Real-ESRGAN redraws clean edges. It is run on the webcam crop alone, through the
authors' portable ``realesrgan-ncnn-vulkan`` program (Vulkan, any recent GPU), never on the gameplay.

Nothing here downloads anything. The program is used when it has been unpacked into
``<models_dir>/realesrgan/`` (see docs: "Sharper webcam"); otherwise the normal scaler is used.

The result is a short intermediate video of the upscaled crop, aligned to the clip's first frame, which the
renderer takes as a second input.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from clipstudio.core.errors import MediaError
from clipstudio.core.logging import get_logger
from clipstudio.core.models import PixelRect
from clipstudio.media.ffmpeg import ffmpeg_bin, run

log = get_logger("face_upscale")
EXE_NAMES = ("realesrgan-ncnn-vulkan.exe", "realesrgan-ncnn-vulkan")
MODEL = "realesrgan-x4plus"   # the general photo model; the anime models turn faces into drawings
SCALE = 4


def find_program(models_dir: Path) -> Path | None:
    """The upscaler executable under ``<models_dir>/realesrgan/`` (with its model files), or None."""
    base = Path(models_dir) / "realesrgan"
    for name in EXE_NAMES:
        exe = base / name
        if exe.is_file() and (base / "models" / f"{MODEL}.bin").is_file():
            return exe
    return None


def upscale_face(src: Path, start: float, dur: float, crop: PixelRect, fps: float, out: Path, exe: Path,
                 timeout: float = 1200.0) -> Path:
    """Write ``out``: the ``crop`` of ``src`` from ``start`` for ``dur`` seconds, enlarged 4x, at ``fps``."""
    work = out.parent / "_face_sr"
    shutil.rmtree(work, ignore_errors=True)
    fin, fout = work / "in", work / "out"
    fin.mkdir(parents=True)
    fout.mkdir()
    t0 = time.perf_counter()
    try:
        # the recording's compression blocks are smoothed first, or the model sharpens them into a grid
        run([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}",
             "-i", str(src), "-an", "-sn",
             "-vf", f"fps={fps:g}:start_time=0,crop={crop.w}:{crop.h}:{crop.x}:{crop.y},deblock=filter=weak:block=8",
             "-start_number", "0", str(fin / "%06d.png")])
        n = len(list(fin.glob("*.png")))
        if n == 0:
            raise MediaError("no webcam frames were extracted for upscaling")
        cp = subprocess.run([str(exe), "-i", str(fin), "-o", str(fout), "-n", MODEL, "-s", str(SCALE), "-f", "png"],
                            cwd=str(exe.parent), capture_output=True, timeout=timeout)
        done = len(list(fout.glob("*.png")))
        if cp.returncode != 0 or done < n:
            tail = cp.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["no error text"]
            raise MediaError(f"Real-ESRGAN produced {done} of {n} frames (exit {cp.returncode}): {tail[0]}")
        tmp = out.with_name(out.stem + ".partial.mp4")
        # near-lossless intermediate: the clip's final encode is the only lossy step that matters
        run([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-y", "-framerate", f"{fps:g}", "-start_number", "0",
             "-i", str(fout / "%06d.png"), "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
             "-pix_fmt", "yuv420p", str(tmp)])
        tmp.replace(out)
        log.info("    webcam upscaled with Real-ESRGAN: %d frames (%dx%d -> %dx%d) in %.0f s", n, crop.w, crop.h,
                 crop.w * SCALE, crop.h * SCALE, time.perf_counter() - t0)
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)
