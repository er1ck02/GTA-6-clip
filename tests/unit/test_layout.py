import pytest

from clipstudio.config import load_config
from clipstudio.core.models import MediaInfo
from clipstudio.reframing.layout import plan_layout


def _m(w, h):
    return MediaInfo(path="/x", duration=10, width=w, height=h, fps=30, video_codec="h264", has_audio=True,
                     size_bytes=1, container="mp4")


@pytest.mark.parametrize("w,h", [(1920, 1080), (1280, 720), (2560, 1440), (1440, 1080), (3840, 2160), (1080, 1920)])
@pytest.mark.parametrize("mode", ["gameplay", "fit_blur", "facecam_split"])
@pytest.mark.parametrize("cx", [0.0, 0.5, 1.0])
def test_crops_inside_frame_and_9x16(w, h, mode, cx):
    cfg = load_config(overrides=[f"reframing.mode={mode}", f"reframing.gameplay_center_x={cx}",
                                 "reframing.facecam_region={x: 0.75, y: 0.7, w: 0.25, h: 0.3}"])
    plan = plan_layout(_m(w, h), cfg.reframing, cfg.video, cfg.captions)
    kf = plan.keyframes[0]
    for r in [kf.gameplay] + ([kf.facecam] if kf.facecam else []):
        assert r.x >= 0 and r.y >= 0 and r.x + r.w <= w and r.y + r.h <= h
        assert r.w % 2 == 0 and r.h % 2 == 0
    if mode == "gameplay":
        assert abs(kf.gameplay.w / kf.gameplay.h - 9 / 16) < 0.01
    if mode == "facecam_split":
        face = next(p for p in plan.panels if p.name == "facecam").dest
        assert abs(kf.facecam.w / kf.facecam.h - face.w / face.h) < 0.02
        assert plan.caption_band[0] > face.h


def test_split_without_region_falls_back_with_note():
    cfg = load_config(overrides=["reframing.mode=facecam_split"])
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions)
    assert plan.mode == "gameplay" and plan.notes
