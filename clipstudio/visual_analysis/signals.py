"""One low-resolution pass over the whole video produces per-sample visual signals.

Per sample (default 4 per second, 128 px wide): brightness, saturation, motion
(mean absolute frame difference) and a colour-histogram change score. From
these:

* **scene cuts**: histogram change far above its local level. Used to snap
  clip edges to cuts.
* **flash**: a sudden brightness jump (explosions, muzzle flashes, white-outs).
* **desaturation**: colour drains out of the frame for over a second. GTA's
  WASTED / BUSTED screens, kill-cams and many games' death screens do this.
* **HUD text** (optional, needs Tesseract): OCR of the centre band at
  desaturation/flash moments for configurable words such as WASTED, BUSTED,
  MISSION PASSED, ELIMINATED, VICTORY.

Everything here is a generic image statistic; nothing is game-specific except
the configurable OCR word list.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import numpy as np

from clipstudio.config import VisualCfg
from clipstudio.core.logging import get_logger
from clipstudio.core.models import MediaInfo, VisualEvent, VisualTrack
from clipstudio.media.ffmpeg import ffmpeg_bin, run, stream_raw

log = get_logger("visual")


def _rolling_median(x: np.ndarray, win: int) -> np.ndarray:
    if len(x) == 0:
        return x
    win = max(1, win)
    # block medians (fast enough for hours of 4 fps samples), interpolated back
    step = max(1, win // 4)
    centers = np.arange(0, len(x), step)
    vals = np.array([np.median(x[max(0, c - win // 2): c + win // 2 + 1]) for c in centers])
    return np.interp(np.arange(len(x)), centers, vals)


def measure(path: Path, media: MediaInfo, cfg: VisualCfg, progress=None) -> dict[str, np.ndarray]:
    """Decode once and return per-sample arrays."""
    w = cfg.width
    h = max(2, int(round(w * media.height / media.width / 2)) * 2)
    fb = w * h * 3
    args = [ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-i", str(path), "-an", "-sn",
            "-vf", f"fps={cfg.sample_fps:g},scale={w}:{h}:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    bright, sat, motion, hist = [], [], [], []
    prev_g = prev_h = None
    n = 0
    for buf in stream_raw(args, fb):
        frames = np.frombuffer(buf, np.uint8).reshape(-1, h, w, 3).astype(np.float32)
        mx, mn = frames.max(-1), frames.min(-1)
        g = 0.299 * frames[..., 0] + 0.587 * frames[..., 1] + 0.114 * frames[..., 2]
        s = (mx - mn) / np.maximum(mx, 1.0)
        for i in range(len(frames)):
            bright.append(float(g[i].mean()))
            sat.append(float(s[i].mean()))
            hh = np.concatenate([np.histogram(frames[i, ..., c], bins=16, range=(0, 256))[0] for c in range(3)])
            hh = hh / max(1, hh.sum())
            motion.append(float(np.abs(g[i] - prev_g).mean()) if prev_g is not None else 0.0)
            hist.append(float(0.5 * np.abs(hh - prev_h).sum()) if prev_h is not None else 0.0)
            prev_g, prev_h = g[i], hh
        n += len(frames)
        if progress and media.duration > 0:
            progress(min(1.0, n / cfg.sample_fps / media.duration))
    return {k: np.asarray(v, np.float32) for k, v in
            (("brightness", bright), ("saturation", sat), ("motion", motion), ("hist_change", hist))}


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            out.append((i, j))
            i = j
        else:
            i += 1
    return out


def detect_events(m: dict[str, np.ndarray], fps: float, cfg: VisualCfg) -> tuple[list[float], list[VisualEvent]]:
    """Scene cuts (times) and visual events from the per-sample arrays."""
    hc, br, sa = m["hist_change"], m["brightness"], m["saturation"]
    n = len(hc)
    cuts: list[float] = []
    events: list[VisualEvent] = []
    if n < 3:
        return cuts, events
    base = _rolling_median(hc, int(10 * fps))
    for i in range(1, n):
        if hc[i] >= cfg.cut_threshold and hc[i] > 3 * base[i] and (not cuts or i / fps - cuts[-1] >= 0.5):
            cuts.append(round(i / fps, 3))

    long_win = int(60 * fps)
    b_med = _rolling_median(br, long_win)
    s_med = _rolling_median(sa, long_win)
    # flash: brightness well above the minute's median, starting from a normal level
    flash = (br > b_med * (1 + cfg.flash_rise)) & (br - b_med > 25)
    for a, b in _runs(flash):
        if b - a <= 4 * fps:  # a white-out, not a lighting change
            events.append(VisualEvent(kind="flash", start=round(a / fps, 3), end=round(b / fps, 3),
                                      strength=round(float(min(1.0, (br[a:b].max() - b_med[a]) / 80)), 3)))
    # desaturation: colour drains out for at least min_desat_seconds
    desat = (sa < s_med * cfg.desat_ratio) & (s_med > 0.12)
    for a, b in _runs(desat):
        if (b - a) / fps >= cfg.min_desat_seconds:
            events.append(VisualEvent(kind="desaturation", start=round(a / fps, 3), end=round(b / fps, 3),
                                      strength=round(float(min(1.0, 1 - sa[a:b].mean() / max(1e-6, s_med[a]))), 3)))
    return cuts, sorted(events, key=lambda e: e.start)


def ocr_available() -> bool:
    try:
        import pytesseract  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return shutil.which("tesseract") is not None


def ocr_words(path: Path, media: MediaInfo, times: list[float], words: list[str]) -> list[VisualEvent]:
    """OCR the centre band of full-resolution frames at the given times; report configured words found."""
    import pytesseract
    from PIL import Image, ImageOps

    want = [w.upper() for w in words]
    out = []
    W = min(1280, media.width)
    H = int(W * media.height / media.width) // 2 * 2
    for t in times:
        cp = run([ffmpeg_bin(), "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(path), "-frames:v", "1",
                  "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=False)
        if len(cp.stdout) < W * H * 3:
            continue
        fr = np.frombuffer(cp.stdout[: W * H * 3], np.uint8).reshape(H, W, 3)
        band = fr[int(H * 0.25): int(H * 0.75), int(W * 0.1): int(W * 0.9)]
        img = ImageOps.autocontrast(Image.fromarray(band).convert("L"))
        # coloured HUD text on a desaturated frame: the saturation map isolates the letters
        bf = band.astype(np.float32)
        satmap = (bf.max(-1) - bf.min(-1)) / np.maximum(bf.max(-1), 1.0)
        sat_img = Image.fromarray(np.where(satmap > 0.45, 0, 255).astype(np.uint8))
        found = set()
        for im in (sat_img, img, ImageOps.invert(img)):
            txt = pytesseract.image_to_string(im, config="--psm 6").upper()
            txt = re.sub(r"[^A-Z ]+", " ", txt)
            found |= {w for w in want if w in txt}
        for w in sorted(found):
            out.append(VisualEvent(kind="hud_text", start=round(t, 3), end=round(t + 1.0, 3), strength=1.0, label=w))
    return out


def analyze_visual(path: Path, media: MediaInfo, cfg: VisualCfg, progress=None) -> VisualTrack:
    m = measure(path, media, cfg, progress)
    cuts, events = detect_events(m, cfg.sample_fps, cfg)
    notes = [f"{len(m['motion'])} samples at {cfg.sample_fps:g} fps; {len(cuts)} scene cuts; "
             f"{sum(e.kind == 'flash' for e in events)} flashes; {sum(e.kind == 'desaturation' for e in events)} "
             "desaturation events"]
    if cfg.ocr != "off":
        if ocr_available():
            probe_t = sorted({round(t, 2) for e in events for t in (e.start + 0.6, min(e.end - 0.2, e.start + 1.5))})
            probe_t = probe_t[: cfg.max_ocr_frames]
            hud = ocr_words(path, media, probe_t, cfg.ocr_words)
            events = sorted(events + hud, key=lambda e: e.start)
            notes.append(f"OCR checked {len(probe_t)} moments, found {len(hud)} HUD words")
        elif cfg.ocr == "on":
            notes.append("OCR requested but Tesseract/pytesseract is not installed")
    mo = m["motion"]
    med = float(np.median(mo)) if mo.size else 0.0
    return VisualTrack(sample_fps=cfg.sample_fps, motion=[round(float(x), 2) for x in mo], motion_median=round(med, 3),
                       brightness=[round(float(x), 1) for x in m["brightness"]],
                       saturation=[round(float(x), 3) for x in m["saturation"]],
                       scene_cuts=cuts, events=events, notes=notes)
