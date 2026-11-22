import pytest

from clipstudio.config import load_config
from clipstudio.core.models import MediaInfo
from clipstudio.reframing.layout import plan_layout


def _m(w, h):
    return MediaInfo(path="/x", duration=10, width=w, height=h, fps=30, video_codec="h264", has_audio=True,
                     size_bytes=1, container="mp4")


@pytest.mark.parametrize("w,h", [(1920, 1080), (1280, 720), (2560, 1440), (1440, 1080), (3840, 2160), (1080, 1920)])
@pytest.mark.parametrize("mode", ["gameplay", "fit_blur", "facecam_split", "facecam_pip"])
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


def _det(conf, region=(0.02, 0.04, 0.25, 0.28)):
    from clipstudio.core.models import FacecamDetection, NormRect
    x, y, w, h = region
    return FacecamDetection(region=NormRect(x=x, y=y, w=w, h=h), confidence=conf, method="face+border")


def test_auto_layout_uses_confident_facecam_only():
    cfg = load_config(overrides=["reframing.mode=auto"])
    hi = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    lo = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.4), None, 0.6)
    none = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, None, None, 0.6)
    assert hi.mode == "facecam_split" and hi.source == "auto"
    assert lo.mode == "gameplay" and none.mode == "gameplay"


def test_manual_facecam_region_beats_detection():
    cfg = load_config(overrides=["reframing.mode=auto", "reframing.facecam_region={x: 0.7, y: 0.6, w: 0.3, h: 0.4}"])
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    assert plan.source == "manual" and plan.keyframes[0].facecam.x >= int(0.7 * 1920)


def test_split_gameplay_crop_steps_off_the_facecam():
    cfg = load_config(overrides=["reframing.mode=facecam_split", "reframing.gameplay_center_x=0.45"])
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    g = plan.keyframes[0].gameplay
    assert g.x >= int((0.02 + 0.25) * 1920)  # starts right of the facecam box


def test_tracking_path_becomes_constant_size_keyframes():
    from clipstudio.core.models import ActionTrack
    cfg = load_config(overrides=["reframing.mode=gameplay", "reframing.tracking=true"])
    tr = ActionTrack(method="test", sample_fps=4, smoothed=[(0.0, 0.3), (2.0, 0.3), (4.0, 0.7), (6.0, 0.7)])
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, None, tr)
    xs = [k.gameplay.x for k in plan.keyframes]
    assert len(plan.keyframes) == 4 and xs[0] < xs[-1]
    assert len({(k.gameplay.w, k.gameplay.h) for k in plan.keyframes}) == 1
    # tracking off -> static
    cfg2 = load_config(overrides=["reframing.mode=gameplay", "reframing.tracking=false"])
    assert len(plan_layout(_m(1920, 1080), cfg2.reframing, cfg2.video, cfg2.captions, None, tr).keyframes) == 1


def test_pip_box_is_protected_from_captions():
    cfg = load_config(overrides=["reframing.mode=facecam_pip"])
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    pip = next(p for p in plan.panels if p.name == "facecam").dest
    assert plan.protected_regions == [pip] and plan.caption_band[0] > pip.y + pip.h


def test_crop_is_locked_by_default_and_split_is_a_top_third():
    from clipstudio.core.models import ActionTrack
    cfg = load_config(overrides=["reframing.mode=auto"])
    tr = ActionTrack(method="test", sample_fps=4, smoothed=[(0.0, 0.3), (2.0, 0.3), (4.0, 0.7), (6.0, 0.7)])
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), tr, 0.6)
    assert plan.mode == "facecam_split" and len(plan.keyframes) == 1  # no panning unless tracking is asked for
    face = next(p.dest for p in plan.panels if p.name == "facecam")
    game = next(p.dest for p in plan.panels if p.name == "gameplay")
    assert (face.h, game.h) == (640, 1280)
    # the gameplay crop is enlarged far less than a full-screen 9:16 crop (608 px wide) would be
    assert plan.keyframes[0].gameplay.w >= 900


def test_framing_moves_and_zooms_the_locked_crop():
    cfg = load_config(overrides=["reframing.mode=auto"])
    fr = {"cx": 0.56, "cy": 0.4, "zoom": 1.25, "cover_before": 0.09, "cover_after": 0.0}
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6, fr)
    g = plan.keyframes[0].gameplay
    assert len(plan.keyframes) == 1 and abs(g.h - 1080 / 1.25) <= 2 and g.y == 0
    assert abs(g.w / g.h - 1080 / 1280) < 0.01 and any("overlays" in n for n in plan.notes)
