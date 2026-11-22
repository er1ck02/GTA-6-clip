"""Keep stream overlays (sponsor banners, alert boxes, a second camera) out of the gameplay crop.

An overlay is drawn on top of the game, so its edges stay in exactly the same place while the game picture
behind it moves. ``overlay_mask`` marks those places from frames sampled across one clip, and ``best_framing``
picks the crop (position and a small zoom) that shows the least of them. Zooming costs sharpness and moving
off-centre costs composition, so a crop only changes when an overlay really is in the way.
"""
from __future__ import annotations

import numpy as np

EDGE_THR = 18.0        # grey-level step that counts as an edge
PERSIST = 0.6          # share of the clip's samples an edge must be present in
STATIC_SCENE = 0.35    # more of the frame than this "persistent": the game picture itself is standing still
MIN_COVER = 0.03       # overlays must cover this share of the default crop before the crop is changed
W_COVER, W_ZOOM, W_CENTRE = 2.0, 0.25, 0.15


def _box_mean(a: np.ndarray, k: int) -> np.ndarray:
    """Mean over a k x k neighbourhood (integral image, edge-padded)."""
    p = k // 2
    ap = np.pad(a.astype(np.float32), p, mode="edge")
    c = np.pad(ap.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    h, w = a.shape
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)


def overlay_mask(frames: np.ndarray) -> np.ndarray | None:
    """Boolean (h, w) mask of overlay graphics, or None when it cannot be told (few frames, a still scene)."""
    if len(frames) < 8:
        return None
    f = frames.astype(np.float32)
    g = 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]
    n, h, w = g.shape
    dx, dy = np.diff(g, axis=2), np.diff(g, axis=1)
    e = np.zeros((h, w), np.float32)
    # same-polarity persistence: an overlay's edge is the same light/dark step every time; game texture is not
    e[:, 1:] = np.maximum((dx > EDGE_THR).mean(0), (dx < -EDGE_THR).mean(0))
    e[1:, :] = np.maximum(e[1:, :], np.maximum((dy > EDGE_THR).mean(0), (dy < -EDGE_THR).mean(0)))
    k = max(5, (w // 40) | 1)
    mask = _box_mean(e >= PERSIST, k) > 0.10
    if mask.mean() > STATIC_SCENE:
        return None
    return _touching_border(mask)


def _touching_border(mask: np.ndarray) -> np.ndarray:
    """Keep only the parts of ``mask`` connected to the frame edge.

    Stream overlays are docked to an edge or a corner. A still graphic in the middle of the picture is the
    game's own screen (a results table, a menu, a dialogue box): that is content, and the crop must not run
    away from it."""
    h, w = mask.shape
    band_y, band_x = max(1, h // 30), max(1, w // 30)
    seed = np.zeros_like(mask)
    seed[:band_y], seed[-band_y:], seed[:, :band_x], seed[:, -band_x:] = True, True, True, True
    seed &= mask
    for _ in range(h + w):
        grown = seed.copy()
        grown[1:] |= seed[:-1]
        grown[:-1] |= seed[1:]
        grown[:, 1:] |= seed[:, :-1]
        grown[:, :-1] |= seed[:, 1:]
        grown &= mask
        if (grown == seed).all():
            break
        seed = grown
    return seed


def _cover(ii: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> float:
    area = max(1, (x1 - x0) * (y1 - y0))
    return float(ii[y1, x1] - ii[y0, x1] - ii[y1, x0] + ii[y0, x0]) / area


def best_framing(mask: np.ndarray | None, crop_w: float, crop_h: float = 1.0, max_zoom: float = 1.3,
                 centre_x: float = 0.5) -> dict | None:
    """Crop that shows the least overlay: ``{"cx", "cy", "zoom", "cover_before", "cover_after"}`` (normalised
    centre, zoom relative to the default crop of ``crop_w`` x ``crop_h``), or None to keep the default."""
    if mask is None or max_zoom <= 1.0:
        return None
    h, w = mask.shape
    ii = np.pad(mask.astype(np.float32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))

    def px(cx: float, cy: float, z: float) -> tuple[int, int, int, int]:
        cw, ch = crop_w / z * w, crop_h / z * h
        x0 = int(round(min(max(0.0, cx * w - cw / 2), w - cw)))
        y0 = int(round(min(max(0.0, cy * h - ch / 2), h - ch)))
        return x0, y0, min(w, int(round(x0 + cw))), min(h, int(round(y0 + ch)))

    before = _cover(ii, *px(centre_x, 0.5, 1.0))
    if before < MIN_COVER:
        return None
    best, best_cost = None, W_COVER * before
    for z in np.arange(1.0, max_zoom + 1e-6, 0.05):
        cw, ch = crop_w / z, crop_h / z
        for cx in np.arange(cw / 2, 1 - cw / 2 + 1e-6, 0.01):
            for cy in np.arange(ch / 2, 1 - ch / 2 + 1e-6, 0.02):
                cov = _cover(ii, *px(cx, cy, z))
                cost = W_COVER * cov + W_ZOOM * (z - 1) + W_CENTRE * abs(cx - centre_x)
                if cost < best_cost - 1e-6:
                    best, best_cost = (float(cx), float(cy), float(z), cov), cost
    if best is None:
        return None
    cx, cy, z, cov = best
    return {"cx": round(cx, 4), "cy": round(cy, 4), "zoom": round(z, 3), "cover_before": round(before, 4),
            "cover_after": round(cov, 4)}
