"""Face detection backends.

Order of preference (``vision.face_detector: auto``):
1. MediaPipe Tasks FaceDetector (BlazeFace short range, Apache-2.0, ~230 KB model).
   The model is fetched once into ``<models_dir>/vision/`` if missing.
2. OpenCV Haar cascade (frontal face, bundled with opencv-python-headless < 5).
3. None: callers fall back to non-face heuristics and say so.

BlazeFace short range expects faces that fill a good part of its 128 px input,
while a facecam face in a 1080p stream is small. ``detect`` therefore also runs
on overlapping tiles and maps the boxes back to frame coordinates.
"""
from __future__ import annotations

import atexit
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from clipstudio.core.logging import get_logger

log = get_logger("vision")

# keep MediaPipe/TFLite C++ logging quiet unless the user asked for it
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

BLAZE_URL = ("https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/"
             "float16/latest/blaze_face_short_range.tflite")
BLAZE_NAME = "blaze_face_short_range.tflite"


@dataclass
class FaceBox:
    """Normalised (0..1) face box in frame coordinates."""
    x: float
    y: float
    w: float
    h: float
    score: float

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


def _iou(a: FaceBox, b: FaceBox) -> float:
    x1, y1 = max(a.x, b.x), max(a.y, b.y)
    x2, y2 = min(a.x + a.w, b.x + b.w), min(a.y + a.h, b.y + b.h)
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    return inter / max(1e-9, a.w * a.h + b.w * b.h - inter)


def _nms(boxes: list[FaceBox], thr: float = 0.3) -> list[FaceBox]:
    out: list[FaceBox] = []
    for b in sorted(boxes, key=lambda b: -b.score):
        if all(_iou(b, o) < thr for o in out):
            out.append(b)
    return out


def _tiles(h: int, w: int) -> list[tuple[int, int, int, int]]:
    """Full frame, a 2x2 grid and a 3x3 grid of overlapping tiles (x, y, tw, th)."""
    out = [(0, 0, w, h)]
    for n in (2, 3):
        tw, th = int(w / n * 1.25), int(h / n * 1.25)
        for i in range(n):
            for j in range(n):
                x = min(w - tw, int(i * w / n - (tw - w / n) / 2)) if n > 1 else 0
                y = min(h - th, int(j * h / n - (th - h / n) / 2)) if n > 1 else 0
                out.append((max(0, x), max(0, y), tw, th))
    return out


class FaceDetector:
    name = "none"

    def _detect_one(self, rgb: np.ndarray) -> list[FaceBox]:  # pragma: no cover - interface
        return []

    def detect(self, rgb: np.ndarray, tiled: bool = True) -> list[FaceBox]:
        h, w = rgb.shape[:2]
        found: list[FaceBox] = []
        for (x, y, tw, th) in (_tiles(h, w) if tiled else [(0, 0, w, h)]):
            tile = np.ascontiguousarray(rgb[y:y + th, x:x + tw])
            for b in self._detect_one(tile):
                found.append(FaceBox((x + b.x * tw) / w, (y + b.y * th) / h, b.w * tw / w, b.h * th / h, b.score))
        return _nms(found)

    def close(self) -> None:
        pass


class MediaPipeFaces(FaceDetector):
    name = "mediapipe_blazeface"

    def __init__(self, model: Path, min_conf: float):
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        self._mp = mp
        self._det = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model)), min_detection_confidence=min_conf))
        # close before interpreter teardown; MediaPipe's own __del__ fails noisily at exit otherwise
        atexit.register(self.close)

    def _detect_one(self, rgb: np.ndarray) -> list[FaceBox]:
        h, w = rgb.shape[:2]
        res = self._det.detect(self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb))
        out = []
        for d in res.detections:
            bb = d.bounding_box
            out.append(FaceBox(bb.origin_x / w, bb.origin_y / h, bb.width / w, bb.height / h,
                               float(d.categories[0].score) if d.categories else 0.5))
        return out

    def close(self) -> None:
        det, self._det = getattr(self, "_det", None), None
        if det is not None:
            try:
                det.close()
            except Exception:  # noqa: BLE001
                pass


class HaarFaces(FaceDetector):
    name = "opencv_haar"

    def __init__(self):
        import cv2

        path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        self._cv2 = cv2
        self._c = cv2.CascadeClassifier(str(path))
        if self._c.empty():
            raise RuntimeError(f"could not load {path}")

    def _detect_one(self, rgb: np.ndarray) -> list[FaceBox]:
        h, w = rgb.shape[:2]
        g = self._cv2.cvtColor(rgb, self._cv2.COLOR_RGB2GRAY)
        rects = self._c.detectMultiScale(g, scaleFactor=1.1, minNeighbors=6, minSize=(24, 24))
        return [FaceBox(x / w, y / h, fw / w, fh / h, 0.6) for (x, y, fw, fh) in rects]

    def detect(self, rgb: np.ndarray, tiled: bool = False) -> list[FaceBox]:
        # Haar is scale-invariant already; tiling only adds false positives
        return super().detect(rgb, tiled=False)


def ensure_blazeface(models_dir: Path, download: bool = True) -> Path | None:
    p = models_dir / "vision" / BLAZE_NAME
    if p.exists() and p.stat().st_size > 100_000:
        return p
    if not download:
        return None
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        log.info("downloading BlazeFace model (~230 KB) to %s", p)
        with urllib.request.urlopen(BLAZE_URL, timeout=30) as r, open(tmp, "wb") as f:
            f.write(r.read())
        if tmp.stat().st_size < 100_000:
            raise RuntimeError("download too small")
        tmp.replace(p)
        return p
    except Exception as e:  # noqa: BLE001
        log.warning("BlazeFace model unavailable (%s)", e)
        return None


def available_backends() -> dict[str, bool]:
    out = {}
    for mod in ("mediapipe", "cv2"):
        try:
            __import__(mod)
            out[mod] = True
        except Exception:  # noqa: BLE001
            out[mod] = False
    return out


def get_face_detector(kind: str, models_dir: Path, min_conf: float = 0.5, download: bool = True) -> FaceDetector | None:
    """Return the best available detector for ``kind`` (auto|mediapipe|opencv|none), or None."""
    if kind == "none":
        return None
    if kind in ("auto", "mediapipe"):
        try:
            model = ensure_blazeface(models_dir, download)
            if model:
                return MediaPipeFaces(model, min_conf)
        except Exception as e:  # noqa: BLE001
            log.warning("MediaPipe face detector unavailable: %s", e)
        if kind == "mediapipe":
            return None
    try:
        return HaarFaces()
    except Exception as e:  # noqa: BLE001
        log.warning("OpenCV face detector unavailable: %s", e)
        return None
