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
        # the crop has the shape of the panel it fills (a wide view of the game over a blurred fill)
        gp = next(p for p in plan.panels if p.name == "gameplay").dest
        assert abs(kf.gameplay.w / kf.gameplay.h - gp.w / gp.h) < 0.01 and gp.w == 1080 and gp.h <= 1280
        full = load_config(overrides=["reframing.mode=gameplay", "reframing.gameplay_fill=full"])
        fk = plan_layout(_m(w, h), full.reframing, full.video, full.captions).keyframes[0].gameplay
        assert abs(fk.w / fk.h - 9 / 16) < 0.01
    if mode == "facecam_split":
        face = next(p for p in plan.panels if p.name == "facecam").dest
        # a webcam big enough fills the panel exactly; a small one is shown whole over a blurred fill
        assert plan.facecam_fit or abs(kf.facecam.w / kf.facecam.h - face.w / face.h) < 0.02
        assert plan.caption_band[0] > face.y + face.h


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
    # a zoomed-in view (half the screen) can step around the webcam; the default wide view shows the screen as is
    cfg = load_config(overrides=["reframing.mode=facecam_split", "reframing.gameplay_center_x=0.45",
                                 "reframing.gameplay_view=0.45", "reframing.facecam_panel_ratio=0.3334"])
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
    # streamer on top, game right under it, and the game is not zoomed in: four fifths of the screen's width,
    # shrunk to fit (never enlarged), with the pair centred on a blurred fill
    # the streamer fills the top half; the whole game screen sits right under it, shrunk to the frame's width
    # and never enlarged; what is left at the bottom is a blurred band
    assert (face.y, face.h, game.y, game.h) == (0, 960, 960, 608)
    assert plan.keyframes[0].gameplay.w >= 1916 and plan.keyframes[0].gameplay.h == 1080
    assert plan.panels[0].name == "background"


def test_framing_moves_and_zooms_the_locked_crop():
    cfg = load_config(overrides=["reframing.mode=auto"])
    fr = {"cx": 0.56, "cy": 0.4, "zoom": 1.25, "cover_before": 0.09, "cover_after": 0.0}
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6, fr)
    g = plan.keyframes[0].gameplay
    assert len(plan.keyframes) == 1 and abs(g.h - 1080 / 1.25) <= 2 and g.y == 0
    assert abs(g.w / g.h - 1080 / 608) < 0.01 and any("overlays" in n for n in plan.notes)


def test_small_webcam_is_shown_whole_not_blown_up():
    from clipstudio.rendering.render import build_video_graph
    cfg = load_config(overrides=["reframing.mode=auto"])
    small = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions,
                        _det(0.9, region=(0.039, 0.413, 0.088, 0.175)), None, 0.6)
    f = small.keyframes[0].facecam
    assert small.facecam_fit and f.w >= 150 and f.h >= 170          # the whole camera picture, not a strip of it
    assert any("small webcam" in n for n in small.notes)
    g = build_video_graph(small, 60, None)
    assert "boxblur" in g and "overlay=" in g and "[base]" in g
    inner = small.facecam_inner                                      # enlarged no more than facecam_max_upscale
    assert inner is not None and inner.h <= f.h * cfg.reframing.facecam_max_upscale + 2 and inner.h <= 960
    big = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    assert not big.facecam_fit and "boxblur=24" not in build_video_graph(big, 60, None)   # no blurred webcam fill


def test_no_facecam_gameplay_is_wide_not_zoomed():
    from clipstudio.rendering.render import build_video_graph
    cfg = load_config(overrides=["reframing.mode=auto"])
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions)
    g = plan.keyframes[0].gameplay
    assert plan.mode == "gameplay" and g.w >= 900                 # 912 px of the source, not 608
    assert (plan.out_width, plan.out_height) == (1080, 1920)      # the output frame is still exactly 9:16
    graph = build_video_graph(plan, 60, None)
    assert "boxblur" in graph and "overlay=0:" in graph


def test_upscaled_webcam_comes_from_a_second_input(tmp_path):
    from clipstudio.rendering.face_upscale import find_program
    from clipstudio.rendering.render import build_video_graph
    cfg = load_config(overrides=["reframing.mode=auto"])
    small = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions,
                        _det(0.9, region=(0.039, 0.413, 0.088, 0.175)), None, 0.6)
    plain = build_video_graph(small, 60, None)
    sr = build_video_graph(small, 60, None, face_input=4)
    assert "[1:v]" not in plain and "[0:v]split=2[fsrc][gsrc]" in plain
    assert "[1:v]fps=60:start_time=0" in sr and "[fsrc]" not in sr and "[0:v]null[gsrc]" in sr
    # not installed: nothing is found, and nothing is downloaded
    assert find_program(tmp_path) is None
    (tmp_path / "realesrgan" / "models").mkdir(parents=True)
    (tmp_path / "realesrgan" / "realesrgan-ncnn-vulkan.exe").write_bytes(b"")
    assert find_program(tmp_path) is None                      # the program without its model is not usable
    (tmp_path / "realesrgan" / "models" / "realesrgan-x4plus.bin").write_bytes(b"")
    assert find_program(tmp_path) is not None


def test_title_is_a_picture_above_the_streamer_and_captions_are_off(tmp_path):
    from clipstudio.captions.title_card import render_title
    from clipstudio.rendering.render import build_video_graph
    cfg = load_config()
    assert not cfg.captions.enabled                                   # the clip carries a title only
    size = render_title("what did bro just do \U0001F62D", tmp_path / "t.png", 1080, 1920)
    assert size and size[0] <= 1080 * 0.95 and (tmp_path / "t.png").stat().st_size > 0
    long = render_title("word " * 60, tmp_path / "l.png", 1080, 1920)
    assert long[0] <= 1080 and long[1] < 1920 * 0.12                  # wrapped and cut to two lines
    assert render_title("   ", tmp_path / "n.png", 1080, 1920) is None
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    g = build_video_graph(plan, 60, None, title_input=(1, 100, 80, 0.0))
    assert "[1:v]format=rgba[titlev]" in g and "overlay=100:80" in g and "enable" not in g
    assert "enable='lte(t,3.50)'" in build_video_graph(plan, 60, None, title_input=(1, 100, 80, 3.5))


def test_long_title_shrinks_to_one_line_before_wrapping(tmp_path):
    from clipstudio.captions.title_card import render_title
    short = render_title("tg tried to be nice 😭", tmp_path / "s.png", 1080, 1920)
    longer = render_title("typical gamer shot through the door 😳", tmp_path / "l.png", 1080, 1920)
    assert longer[0] <= 1080 * 0.95 and longer[1] <= short[1]       # still one line, in smaller letters


def test_webcam_inside_the_gameplay_is_blurred_there_when_shown_on_top():
    from clipstudio.rendering.render import build_video_graph
    cfg = load_config()
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    assert plan.mode == "facecam_split"
    g = build_video_graph(plan, 60, None, cfg.enhance)
    f, gp = plan.keyframes[0].facecam, plan.keyframes[0].gameplay
    inside = gp.x <= f.x and gp.y <= f.y and f.x + f.w <= gp.x + gp.w and f.y + f.h <= gp.y + gp.h
    assert ("[gblur]" in g) == inside
    off = cfg.enhance.model_copy(update={"hide_gameplay_cam": False})
    assert "[gblur]" not in build_video_graph(plan, 60, None, off)


def test_stream_chat_box_is_smeared_before_any_crop():
    from clipstudio.core.models import PixelRect
    from clipstudio.rendering.render import build_video_graph, hide_in_source
    cfg = load_config()
    assert "supertf" in cfg.reframing.channel_chat_boxes and "ishowspeed" in cfg.reframing.channel_chat_boxes
    plan = plan_layout(_m(1920, 1080), cfg.reframing, cfg.video, cfg.captions, _det(0.9), None, 0.6)
    box = [PixelRect(x=1478, y=831, w=440, h=247)]
    g = build_video_graph(plan, 60, None, cfg.enhance, hide_boxes=box)
    assert g.startswith("[0:v]delogo=x=1478:y=831:w=440:h=247") and g.count("[0:v]") == 1
    assert "delogo" not in build_video_graph(plan, 60, None, cfg.enhance)
    two = hide_in_source("[0:v]null[a];[0:v]null[b]", box)
    assert two.count("[0:v]") == 1 and "split=2[srcclean0][srcclean1]" in two


def test_credit_line_sits_under_the_title_or_alone(tmp_path):
    from clipstudio.captions.title_card import render_title
    plain = render_title("supertf vs airport security 😭", tmp_path / "p.png", 1080, 1920)
    credited = render_title("supertf vs airport security 😭", tmp_path / "c.png", 1080, 1920, credit="twitch.tv/supertf")
    assert credited[1] > plain[1] and credited[0] <= 1080 * 0.95     # one more (smaller) line underneath
    alone = render_title("", tmp_path / "a.png", 1080, 1920, credit="twitch.tv/supertf")
    assert alone and alone[1] < plain[1] * 1.2
    assert render_title("", tmp_path / "n.png", 1080, 1920, credit="  ") is None
