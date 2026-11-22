"""Facecam detection, action tracking and the animated-crop expression, on synthetic frames."""
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from clipstudio.core.models import NormRect
from clipstudio.rendering.render import lerp_expr
from clipstudio.vision.facecam import detect_facecam
from clipstudio.vision.tracking import raw_action_path, smooth_path, track_action

FACE = Path(__file__).resolve().parents[1] / "fixtures" / "face_nasa_public_domain.jpg"
W, H = 960, 540


def _stream_frames(n=16, face=True, seed=0):
    """Changing 'gameplay' noise blocks + a static bordered webcam box in the top-left."""
    rng = np.random.default_rng(seed)
    cam = np.asarray(Image.open(FACE).convert("RGB").resize((230, 150)))
    out = []
    for i in range(n):
        f = np.kron(rng.integers(0, 255, (H // 30, W // 30, 3)), np.ones((30, 30, 1))).astype(np.uint8)
        f[18:178, 18:258] = 245  # 5 px white border around a 230x150 camera picture
        if face:
            # the person moves a little between samples (brightness/noise), the overlay itself does not
            f[23:173, 23:253] = np.clip(cam.astype(int) - 20 + (i % 4) * 4 + rng.integers(-6, 6, cam.shape), 0, 225)
        else:
            f[23:173, 23:253] = (40 + i * 5) % 255
        out.append(f)
    return np.stack(out)


class FakeDetector:
    """Stands in for MediaPipe: reports the pasted face where it is."""
    name = "fake"

    def __init__(self, box=(0.06, 0.06, 0.09, 0.16)):
        self.box = box

    def detect(self, rgb, **kw):
        from clipstudio.vision.faces import FaceBox
        return [FaceBox(*self.box, 0.9)]


def test_facecam_found_from_face_and_border():
    det = detect_facecam(_stream_frames(), FakeDetector())
    assert det.method == "face+border" and det.confidence >= 0.9
    r = det.region
    # true camera picture: x 23..253, y 23..173 of 960x540
    assert abs(r.x - 23 / W) < 0.012 and abs(r.y - 23 / H) < 0.02
    assert abs(r.w - 230 / W) < 0.02 and abs(r.h - 150 / H) < 0.03


def test_border_without_face_is_not_auto_applied():
    det = detect_facecam(_stream_frames(face=False), None)
    assert det.method == "border_only" and det.confidence < 0.6 and det.region is not None


def test_no_overlay_no_facecam():
    rng = np.random.default_rng(1)
    frames = rng.integers(0, 255, (12, H, W, 3), dtype=np.uint8)
    det = detect_facecam(frames, None)
    assert det.region is None and det.confidence == 0


def test_unstable_or_borderless_face_stays_below_threshold():
    rng = np.random.default_rng(2)
    frames = rng.integers(0, 255, (12, H, W, 3), dtype=np.uint8)

    class Sometimes(FakeDetector):
        calls = 0

        def detect(self, rgb, **kw):
            self.calls += 1
            return super().detect(rgb) if self.calls % 2 else []

    det = detect_facecam(frames, Sometimes(box=(0.6, 0.75, 0.08, 0.14)))
    assert det.confidence < 0.6  # e.g. a first-person gun hand that looks like a face half the time


def test_mediapipe_finds_real_face_if_installed():
    pytest.importorskip("mediapipe")
    from clipstudio.vision.faces import get_face_detector
    d = get_face_detector("mediapipe", Path("models"), download=True)
    if d is None:
        pytest.skip("BlazeFace model not available offline")
    try:
        det = detect_facecam(_stream_frames(8), d)
    finally:
        d.close()
    assert det.method == "face+border" and det.confidence >= 0.9


def _blob_frames(xs, w=320, h=180):
    frames = np.zeros((len(xs), h, w, 3), np.uint8) + 40
    for i, x in enumerate(xs):
        c = int(x * w)
        frames[i, 60:120, max(0, c - 15):c + 15] = 230
        # flicker so consecutive frames differ even if the blob stops
        frames[i, 60:120, max(0, c - 15):c + 15, i % 3] = 120
    return frames


def test_tracking_follows_a_moving_subject():
    xs = list(np.linspace(0.25, 0.75, 40))
    tr = track_action(_blob_frames(xs), np.arange(40) / 4, 4, 0.32, 0.5, None, 0.04, 0.6, 0.5)
    first, last = tr.smoothed[0][1], tr.smoothed[-1][1]
    assert first < 0.45 and last > 0.55
    assert len(tr.smoothed) <= 48


def test_tracking_holds_centre_when_the_whole_frame_moves():
    rng = np.random.default_rng(3)
    frames = rng.integers(0, 255, (20, 180, 320, 3), dtype=np.uint8)  # camera pan: everything changes
    tr = track_action(frames, np.arange(20) / 4, 4, 0.32, 0.5, None, 0.04, 0.3, 0.7)
    assert all(abs(cx - 0.5) < 0.05 for _, cx in tr.smoothed)


def test_tracking_ignores_the_facecam_region():
    frames = _blob_frames([0.1] * 12)  # only motion is inside the excluded box
    raw = raw_action_path(frames, np.arange(12) / 4, 0.32, 0.5, [NormRect(x=0, y=0, w=0.25, h=1)])
    assert all(p.cx == 0.5 for p in raw)


def test_smoothing_limits_speed_and_jitter():
    from clipstudio.core.models import TrackPoint
    raw = [TrackPoint(t=i / 4, cx=0.3 if (i // 2) % 2 else 0.7, confidence=1) for i in range(40)]  # violent jitter
    sm = smooth_path(raw, 4, 0.32, 0.04, 0.3, 0.7)
    for (t0, x0), (t1, x1) in zip(sm, sm[1:]):
        assert abs(x1 - x0) / (t1 - t0) <= 0.3 + 1e-6


def test_lerp_expr():
    assert lerp_expr([0, 1], [10, 10]) == "10"
    e = lerp_expr([0.0, 2.0, 4.0], [100, 300, 300])
    assert e.startswith("'2*trunc(") and "lt(t,2.000)" in e


def _fc(conf, method="face", face=(0.10, 0.44, 0.05, 0.09), region=(0.04, 0.36, 0.18, 0.28)):
    from clipstudio.core.models import FacecamDetection
    return FacecamDetection(region=NormRect(x=region[0], y=region[1], w=region[2], h=region[3]),
                            face=NormRect(x=face[0], y=face[1], w=face[2], h=face[3]), confidence=conf, method=method,
                            face_hit_rate=1.0)


def test_session_prior_confirms_a_webcam_seen_in_other_moments():
    from clipstudio.vision.facecam import apply_session_prior
    weak = _fc(0.55)
    # alone, or with a face somewhere else in the other moments (an in-game character), nothing changes
    assert apply_session_prior(weak, []).confidence == 0.55
    assert apply_session_prior(weak, [_fc(0.55, face=(0.5, 0.2, 0.05, 0.09))] * 3).confidence == 0.55
    # the same spot in two other moments: confirmed
    out = apply_session_prior(weak, [_fc(0.55), _fc(0.55)])
    assert out.confidence >= 0.6 and "other moment" in out.notes[-1]
    # a moment that found the border lends its box, so every clip of the stream gets the same framing
    good = _fc(0.95, method="face+border", region=(0.0, 0.35, 0.22, 0.29))
    out = apply_session_prior(weak, [good])
    assert out.confidence >= 0.9 and out.region == good.region
    assert apply_session_prior(good, [weak, weak]).region == good.region


def test_lerp_expr_eases():
    e = lerp_expr([0.0, 2.0], [100, 300])
    assert "(3-2*" in e  # smoothstep, not a linear ramp


def test_overlay_mask_finds_a_banner_and_the_crop_steps_off_it():
    from clipstudio.vision.overlays import best_framing, overlay_mask
    rng = np.random.default_rng(3)
    h, w, n = 180, 320, 16
    frames = np.zeros((n, h, w, 3), np.uint8)
    banner = rng.integers(0, 255, (50, 130, 3), dtype=np.uint8)
    banner = np.repeat(np.repeat(banner[::5, ::5], 5, 0), 5, 1)          # blocky art: strong, fixed edges
    for i in range(n):
        game = rng.integers(0, 255, (h // 6, w // 8, 3), dtype=np.uint8)  # the game picture changes every sample
        frames[i] = np.roll(np.repeat(np.repeat(game, 6, 0), 8, 1)[:h, :w], (i * 5, i * 7), (0, 1))  # and moves
        frames[i, h - 50:, :130] = banner                                 # bottom-left sponsor banner
    mask = overlay_mask(frames)
    assert mask is not None and mask[h - 30:, :110].mean() > 0.9 and mask[:100, 150:].mean() < 0.05
    crop_w = 912 / 1920
    fr = best_framing(mask, crop_w, 1.0, 1.3)
    assert fr is not None and fr["cover_after"] < 0.25 * fr["cover_before"] and 1.0 <= fr["zoom"] <= 1.3
    # nothing in the way: the default crop is kept
    assert best_framing(np.zeros((h, w), bool), crop_w) is None
    # a still scene (everything "persistent") is not mistaken for overlays
    assert overlay_mask(np.repeat(frames[:1], n, 0)) is None


def test_a_still_graphic_mid_screen_is_content_not_an_overlay():
    from clipstudio.vision.overlays import _touching_border
    m = np.zeros((90, 160), bool)
    m[30:60, 50:110] = True      # a results table in the middle of the game picture
    m[70:, :40] = True           # a banner docked to the bottom-left corner
    out = _touching_border(m)
    assert out[80, 10] and not out[45, 80]
