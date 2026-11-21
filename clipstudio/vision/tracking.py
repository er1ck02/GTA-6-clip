"""Action tracking for the gameplay crop: where is the action, and a smooth path for the crop to follow.

Method: frame differencing on small sampled frames. The column profile of
changed pixels is scanned with a window as wide as the crop; the window with
the most motion is the action. How far the crop leaves its default centre
depends on how concentrated the motion is: when the camera pans (first-person
shooters, driving) the whole frame changes, the motion is uniform and the crop
stays put, which is what a viewer wants.

The raw path is then smoothed offline (median, zero-phase Gaussian, deadzone,
speed limit) and simplified to a few keyframes, so the renderer never jitters.
"""
from __future__ import annotations

import numpy as np

from clipstudio.core.models import ActionTrack, NormRect, TrackPoint

DIFF_THR = 14.0


def _gray_small(frames: np.ndarray, max_w: int = 192) -> np.ndarray:
    g = (0.299 * frames[..., 0] + 0.587 * frames[..., 1] + 0.114 * frames[..., 2]).astype(np.float32)
    n, h, w = g.shape
    k = max(1, w // max_w)
    if k > 1:
        h2, w2 = h // k, w // k
        g = g[:, : h2 * k, : w2 * k].reshape(n, h2, k, w2, k).mean((2, 4))
    return g


def raw_action_path(frames: np.ndarray, times: np.ndarray, crop_w: float, default_cx: float,
                    exclude: list[NormRect] | None = None) -> list[TrackPoint]:
    """Per-sample action centre (normalised x) and confidence. ``crop_w``: crop width / source width."""
    g = _gray_small(frames)
    n, h, w = g.shape
    mask = np.ones((h, w), np.float32)
    for r in exclude or []:
        mask[int(r.y * h): int(np.ceil((r.y + r.h) * h)), int(r.x * w): int(np.ceil((r.x + r.w) * w))] = 0
    win = max(1, int(round(crop_w * w)))
    kernel = np.ones(win, np.float32)
    pts: list[TrackPoint] = []
    for i in range(n):
        if i == 0:
            continue
        d = (np.abs(g[i] - g[i - 1]) > DIFF_THR).astype(np.float32) * mask
        changed = d.sum() / max(1.0, mask.sum())
        col = d.sum(0)
        total = col.sum()
        if total < 0.002 * h * w:  # nothing moved
            pts.append(TrackPoint(t=float(times[i]), cx=default_cx, confidence=0.0))
            continue
        ws = np.convolve(col, kernel, mode="valid")  # ws[j] = motion in columns j..j+win-1
        j = int(np.argmax(ws))
        cx = (j + win / 2) / w
        conc = (ws[j] / total - crop_w) / max(1e-6, 1 - crop_w)
        if changed > 0.45:  # camera motion, not an on-screen subject
            conc *= 0.3
        conf = float(np.clip(conc, 0, 1))
        pull = float(np.clip((conf - 0.1) / 0.3, 0, 1))
        pts.append(TrackPoint(t=float(times[i]), cx=round(default_cx + (cx - default_cx) * pull, 4),
                              confidence=round(conf, 3)))
    if pts:
        pts.insert(0, TrackPoint(t=float(times[0]), cx=pts[0].cx, confidence=pts[0].confidence))
    return pts


def _median(x: np.ndarray, k: int) -> np.ndarray:
    if len(x) < 3 or k < 3:
        return x
    p = k // 2
    xp = np.pad(x, p, mode="edge")
    return np.array([np.median(xp[i:i + k]) for i in range(len(x))])


def _gauss(x: np.ndarray, sigma: float) -> np.ndarray:
    if sigma <= 0.3 or len(x) < 3:
        return x
    r = int(3 * sigma)
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    return np.convolve(np.pad(x, r, mode="edge"), k, mode="valid")


def _rdp(pts: np.ndarray, eps: float) -> list[int]:
    """Ramer-Douglas-Peucker on (t, x) points; returns kept indices."""
    keep = {0, len(pts) - 1}
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        t0, x0 = pts[a]
        t1, x1 = pts[b]
        seg = pts[a + 1:b]
        interp = x0 + (x1 - x0) * (seg[:, 0] - t0) / max(1e-9, t1 - t0)
        err = np.abs(seg[:, 1] - interp)
        k = int(np.argmax(err))
        if err[k] > eps:
            m = a + 1 + k
            keep.add(m)
            stack += [(a, m), (m, b)]
    return sorted(keep)


def smooth_path(raw: list[TrackPoint], fps: float, crop_w: float, deadzone: float, max_speed: float,
                smoothing_s: float, max_keyframes: int = 48) -> list[tuple[float, float]]:
    """Smoothed, speed-limited, simplified (t, cx) keyframes. Times are as given (clip-relative)."""
    if not raw:
        return []
    t = np.array([p.t for p in raw])
    x = np.array([p.cx for p in raw])
    x = _median(x, 5)
    x = _gauss(x, smoothing_s * fps)
    # deadzone: the crop only moves when the target leaves a band around it, then trails it
    out = np.empty_like(x)
    cur = x[0]
    for i, v in enumerate(x):
        if abs(v - cur) > deadzone:
            cur = v - np.sign(v - cur) * deadzone
        out[i] = cur
    # speed limit, applied forwards and backwards so it does not lag the action
    step = max_speed / max(fps, 1e-6)
    for rng in (range(1, len(out)), range(len(out) - 2, -1, -1)):
        for i in rng:
            j = i - 1 if rng.step == 1 else i + 1
            out[i] = np.clip(out[i], out[j] - step, out[j] + step)
    lo, hi = crop_w / 2, 1 - crop_w / 2
    out = np.clip(out, lo, hi) if hi > lo else np.full_like(out, 0.5)
    pts = np.stack([t, out], 1)
    eps = 0.003
    idx = _rdp(pts, eps)
    while len(idx) > max_keyframes:
        eps *= 1.5
        idx = _rdp(pts, eps)
    return [(round(float(pts[i, 0]), 3), round(float(pts[i, 1]), 4)) for i in idx]


def track_action(frames: np.ndarray, times: np.ndarray, sample_fps: float, crop_w: float, default_cx: float,
                 exclude: list[NormRect] | None, deadzone: float, max_speed: float, smoothing_s: float) -> ActionTrack:
    raw = raw_action_path(frames, times, crop_w, default_cx, exclude)
    sm = smooth_path(raw, sample_fps, crop_w, deadzone, max_speed, smoothing_s)
    conf = np.mean([p.confidence for p in raw]) if raw else 0.0
    notes = [f"{len(raw)} samples at {sample_fps:g} fps, mean motion concentration {conf:.2f}, {len(sm)} keyframes"]
    if conf < 0.1:
        notes.append("motion is spread over the whole frame (camera movement); crop held near the default centre")
    return ActionTrack(method="motion_window", sample_fps=sample_fps, raw=raw, smoothed=sm, notes=notes)
