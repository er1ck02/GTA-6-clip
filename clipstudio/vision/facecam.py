"""Find the streamer's facecam box in gaming footage.

Signals, computed on frames sampled across the whole source (so gameplay
changes between samples while overlays stay put):

* **faces**: face boxes from ``faces.py``, clustered by position. A facecam is
  a small face that sits in the same place in most samples.
* **persistent edges**: per-pixel fraction of samples with a strong luminance
  edge. The border of a webcam overlay is a straight line that is present in
  every sample; gameplay edges move.
* **interior motion**: a facecam interior changes a little between samples
  (the person moves); a static HUD panel does not.

The face is the decisive signal. A bordered panel without a face (a minimap,
a sponsor box) is reported with low confidence and is never auto-applied.
"""
from __future__ import annotations

import numpy as np

from clipstudio.core.logging import get_logger
from clipstudio.core.models import FacecamDetection, NormRect
from clipstudio.vision.faces import FaceBox, FaceDetector

log = get_logger("vision")

EDGE_THR = 18.0        # grey-level step that counts as an edge
LINE_MIN = 0.7         # persistence a border line needs (texture/noise scores about 0.5)
FULLFRAME_FACE = 0.06  # face area (fraction of frame) above which this is a face-cam-only stream, not an overlay


def _gray(frames: np.ndarray) -> np.ndarray:
    f = frames.astype(np.float32)
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


def edge_persistence(frames: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(Pv, Ph, std): vertical-edge and horizontal-edge persistence maps (H, W) and temporal std."""
    g = _gray(frames)
    n, H, W = g.shape
    pv = np.zeros((H, W), np.float32)
    ph = np.zeros((H, W), np.float32)
    # Count an edge only when it has the same polarity as in the other samples: an overlay border is the
    # same light/dark step every time, while game texture (foliage, noise, grids) flips sign at random.
    dx = np.diff(g, axis=2)
    dy = np.diff(g, axis=1)
    pv[:, 1:] = np.maximum((dx > EDGE_THR).mean(0), (dx < -EDGE_THR).mean(0))
    ph[1:, :] = np.maximum((dy > EDGE_THR).mean(0), (dy < -EDGE_THR).mean(0))
    # a 1-px line may land on either side of the boundary after scaling: take the max of neighbours
    pv = np.maximum(pv, np.roll(pv, 1, axis=1))
    ph = np.maximum(ph, np.roll(ph, 1, axis=0))
    return pv, ph, g.std(0)


def _cluster_faces(per_frame: list[list[FaceBox]]) -> tuple[list[FaceBox], float]:
    """Largest group of faces at the same place across frames, and its hit rate."""
    allf = [(i, b) for i, bs in enumerate(per_frame) for b in bs]
    if not allf:
        return [], 0.0
    best: list[tuple[int, FaceBox]] = []
    for _, ref in allf:
        tol = max(0.04, 0.6 * max(ref.w, ref.h))
        grp = [(i, b) for i, b in allf if abs(b.cx - ref.cx) < tol and abs(b.cy - ref.cy) < tol]
        if len({i for i, _ in grp}) > len({i for i, _ in best}):
            best = grp
    frames_hit = len({i for i, _ in best})
    return [b for _, b in best], frames_hit / max(1, len(per_frame))


def _median_box(boxes: list[FaceBox]) -> FaceBox:
    a = np.array([[b.x, b.y, b.w, b.h, b.score] for b in boxes])
    m = np.median(a, axis=0)
    return FaceBox(*map(float, m))


def _best_line(profile: np.ndarray, lo: int, hi: int, prefer: int) -> tuple[int, float]:
    """Strongest line position in profile[lo:hi]; among near-equal scores, the one nearest ``prefer``."""
    lo, hi = max(0, lo), min(len(profile), hi)
    if hi <= lo:
        return -1, 0.0
    seg = profile[lo:hi]
    top = seg.max()
    if top < LINE_MIN:
        return -1, float(top)
    cands = np.nonzero(seg >= top - 0.08)[0] + lo
    pos = int(cands[np.argmin(np.abs(cands - prefer))])
    return pos, float(profile[pos])


def _box_around_face(fb: FaceBox, pv: np.ndarray, ph: np.ndarray) -> tuple[tuple[int, int, int, int], float, int]:
    """Search outward from the face for the four border lines. Returns (x0,y0,x1,y1), border score, sides found."""
    H, W = pv.shape
    fx0, fy0 = int(fb.x * W), int(fb.y * H)
    fx1, fy1 = int((fb.x + fb.w) * W), int((fb.y + fb.h) * H)
    fw, fh = max(4, fx1 - fx0), max(4, fy1 - fy0)
    # rows / columns spanning the plausible box, for the line profiles
    ya, yb = max(0, fy0 - fh), min(H, fy1 + fh)
    xa, xb = max(0, fx0 - fw), min(W, fx1 + fw)
    col = pv[ya:yb].mean(0)
    row = ph[:, xa:xb].mean(1)
    mx, my = max(2, fw // 16), max(2, fh // 16)  # face boxes are loose; a border can sit right next to one
    x0, s0 = _best_line(col, fx0 - 4 * fw, fx0 - mx, fx0)
    x1, s1 = _best_line(col, fx1 + mx, fx1 + 4 * fw, fx1)
    y0, s2 = _best_line(row, fy0 - 3 * fh, fy0 - my, fy0)
    y1, s3 = _best_line(row, fy1 + my, fy1 + 4 * fh, fy1)
    found = [s for p, s in ((x0, s0), (x1, s1), (y0, s2), (y1, s3)) if p >= 0]
    # default extents for sides without a line: a typical webcam framing around a face,
    # or the frame edge when the face is close to it
    if x0 < 0:
        x0 = max(0, fx0 - int(1.3 * fw))
        x0 = 0 if x0 < 0.6 * fw else x0
    if x1 < 0:
        x1 = min(W, fx1 + int(1.3 * fw))
        x1 = W if W - x1 < 0.6 * fw else x1
    if y0 < 0:
        y0 = max(0, fy0 - int(0.9 * fh))
        y0 = 0 if y0 < 0.6 * fh else y0
    if y1 < 0:
        y1 = min(H, fy1 + int(1.4 * fh))
        y1 = H if H - y1 < 0.6 * fh else y1
    score = float(np.mean(found)) if found else 0.0
    return (x0, y0, x1, y1), score, len(found)


def _corner_panels(pv: np.ndarray, ph: np.ndarray, std: np.ndarray) -> list[tuple[tuple[int, int, int, int], float]]:
    """Bordered rectangles near the frame corners, found from persistent edges alone."""
    H, W = pv.shape
    cv = ch = np.zeros(0)  # column / row cumulative sums, set per corner below

    def vseg(x: np.ndarray, y0: int, y1: np.ndarray) -> np.ndarray:
        return (cv[y1, x] - cv[y0, x]) / np.maximum(1, y1 - y0)

    def hseg(y: np.ndarray, x0: int, x1: np.ndarray) -> np.ndarray:
        return (ch[y, x1] - ch[y, x0]) / np.maximum(1, x1 - x0)

    out = []
    xs = np.arange(int(0.08 * W), int(0.5 * W))
    ys = np.arange(int(0.08 * H), int(0.55 * H))
    for flip_x in (False, True):
        for flip_y in (False, True):
            P_v = pv[:, ::-1] if flip_x else pv
            P_h = ph[:, ::-1] if flip_x else ph
            P_v = P_v[::-1] if flip_y else P_v
            P_h = P_h[::-1] if flip_y else P_h
            cv = np.vstack([np.zeros((1, W)), np.cumsum(P_v, axis=0)])
            ch = np.hstack([np.zeros((H, 1)), np.cumsum(P_h, axis=1)])
            best = None
            # near edges: the frame edge itself or a persistent line within 10 %
            x0s = [0] + [int(x) for x in np.argsort(P_v[: H // 2, : int(0.1 * W)].mean(0))[-2:] if x > 2]
            y0s = [0] + [int(y) for y in np.argsort(P_h[: int(0.1 * H), : W // 2].mean(1))[-2:] if y > 2]
            X1, Y1 = np.meshgrid(xs, ys)
            for x0 in x0s:
                for y0 in y0s:
                    right = vseg(X1, y0, Y1)
                    bottom = hseg(Y1, x0, X1)
                    sides = [right, bottom]
                    if x0 > 0:
                        sides.append(vseg(np.full_like(X1, x0), y0, Y1))
                    if y0 > 0:
                        sides.append(hseg(np.full_like(Y1, y0), x0, X1))
                    s = np.minimum.reduce(sides[:2]) * 0.6 + np.mean(sides, axis=0) * 0.4
                    area = (X1 - x0) * (Y1 - y0) / (W * H)
                    s = np.where((area > 0.015) & (area < 0.25), s, 0)
                    i = np.unravel_index(np.argmax(s), s.shape)
                    if best is None or s[i] > best[1]:
                        best = ((x0, y0, int(X1[i]), int(Y1[i])), float(s[i]))
            if best and best[1] >= LINE_MIN:
                (x0, y0, x1, y1), sc = best
                if flip_x:
                    x0, x1 = W - x1, W - x0
                if flip_y:
                    y0, y1 = H - y1, H - y0
                interior = std[y0 + 2:y1 - 2, x0 + 2:x1 - 2]
                if interior.size and interior.mean() > 4.0:  # not a static HUD panel
                    out.append(((x0, y0, x1, y1), sc))
    return sorted(out, key=lambda r: -r[1])


def detect_facecam(frames: np.ndarray, detector: FaceDetector | None) -> FacecamDetection:
    n, H, W = frames.shape[:3]
    pv, ph, std = edge_persistence(frames)
    notes: list[str] = []
    per_frame: list[list[FaceBox]] = []
    if detector is not None:
        per_frame = [detector.detect(f) for f in frames]
    else:
        notes.append("no face detector available (install requirements/vision.txt); border-only search")
    faces, hit = _cluster_faces(per_frame)
    face_hits = sum(1 for bs in per_frame if bs)

    def norm(b: tuple[int, int, int, int]) -> NormRect:
        x0, y0, x1, y1 = b
        return NormRect(x=round(x0 / W, 4), y=round(y0 / H, 4), w=round((x1 - x0) / W, 4), h=round((y1 - y0) / H, 4))

    if faces and hit >= 0.3:
        fb = _median_box(faces)
        face_rect = NormRect(x=round(fb.x, 4), y=round(fb.y, 4), w=round(fb.w, 4), h=round(fb.h, 4))
        if fb.w * fb.h > FULLFRAME_FACE:
            notes.append(f"face fills {fb.w * fb.h:.0%} of the frame: a camera-only shot, not a facecam overlay")
            return FacecamDetection(region=None, face=face_rect, confidence=0.0, method="fullframe_face",
                                    detector=detector.name if detector else "none", samples=n, face_hit_rate=round(hit, 3),
                                    notes=notes)
        box, bscore, sides = _box_around_face(fb, pv, ph)
        # a corner panel that contains the face is a better border than the per-side search
        for (pb, ps) in _corner_panels(pv, ph, std):
            x0, y0, x1, y1 = pb
            if x0 <= fb.cx * W <= x1 and y0 <= fb.cy * H <= y1 and ps >= bscore - 0.05 and (x1 - x0) < 6 * fb.w * W:
                box, bscore, sides = pb, ps, 4
                break
        if sides >= 2:
            conf = 0.45 + 0.35 * min(1.0, hit / 0.6) + 0.2 * bscore
        else:
            # no border: first-person view models (a gloved hand) and cutscene faces also look like this,
            # so only a face that is there almost all the time can reach the auto-apply threshold
            conf = 0.30 + 0.35 * min(1.0, hit / 0.8)
        method = "face+border" if sides >= 2 else "face"
        notes.append(f"face in {hit:.0%} of {n} samples; {sides}/4 border lines found (score {bscore:.2f})")
        return FacecamDetection(region=norm(box), face=face_rect, confidence=round(min(1.0, conf), 3), method=method,
                                detector=detector.name if detector else "none", samples=n,
                                face_hit_rate=round(hit, 3), border_score=round(bscore, 3), notes=notes)

    if face_hits:
        notes.append(f"faces in {face_hits}/{n} samples but never in a stable place")
    panels = _corner_panels(pv, ph, std)
    if panels:
        box, sc = panels[0]
        notes.append("bordered corner panel without a confirmed face (could be a minimap or overlay); not auto-applied")
        return FacecamDetection(region=norm(box), confidence=round(0.4 * sc, 3), method="border_only",
                                detector=detector.name if detector else "none", samples=n,
                                face_hit_rate=round(hit, 3), border_score=round(sc, 3), notes=notes)
    notes.append("no facecam found")
    return FacecamDetection(region=None, confidence=0.0, method="none", detector=detector.name if detector else "none",
                            samples=n, face_hit_rate=round(hit, 3), notes=notes)
