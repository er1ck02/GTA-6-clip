"""End-to-end tests that encode and inspect real MP4 files."""
import subprocess

import pytest

from clipstudio.config import load_config
from clipstudio.core.errors import Cancelled
from clipstudio.core.models import CropPlan
from clipstudio.media.ffmpeg import probe
from clipstudio.pipeline import studio as studio_mod
from clipstudio.pipeline.studio import Studio

from tests.conftest import whisper_model

pytestmark = pytest.mark.integration
needs_whisper = pytest.mark.skipif(whisper_model() is None,
                                   reason="set CLIPSTUDIO_WHISPER_MODEL to a local faster-whisper model dir")


def _cfg(tmp_path, *extra):
    return load_config(overrides=[f"paths.data_dir={tmp_path / 'data'}", f"transcription.model={whisper_model()}",
                                  "highlights.minimum_duration=8", "highlights.maximum_duration=20",
                                  "highlights.target_duration=12", "video.fps=30", "export.preset=veryfast", *extra])


@needs_whisper
def test_end_to_end_and_resume(tmp_path, sample_short, monkeypatch):
    st = Studio(_cfg(tmp_path))
    p = st.open_or_create(input_path=sample_short)
    cs = st.analyze(p)
    assert cs.candidates, "expected at least one candidate"
    top = cs.candidates[0]
    # the synthetic sample's loud 'explosion' reaction is at ~8-20 s
    assert 6 <= top.peak_time <= 22
    words = " ".join(s.text for s in __import__("clipstudio.core.models", fromlist=["Transcript"]).Transcript.load(p.p("transcript")).segments).lower()
    assert "exploded" in words

    # interrupted render -> state says interrupted, analysis is not redone on resume
    real = studio_mod.render_clip

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(studio_mod, "render_clip", interrupted)
    with pytest.raises(Cancelled):
        st.render(p, [top.id])
    monkeypatch.setattr(studio_mod, "render_clip", real)

    st2 = Studio(_cfg(tmp_path))
    p2 = st2.load(p.id)
    before = p2.state.stage("transcribe").finished
    st2.analyze(p2)
    assert p2.state.stage("transcribe").finished == before, "transcription must be reused on resume"
    [res] = st2.render(p2, [top.id])
    assert res.qc.passed, [c for c in res.qc.checks if c.status == "fail"]
    assert "plan" in res.cached_stages and "captions" in res.cached_stages
    info = probe(res.output)
    assert (info.width, info.height) == (1080, 1920) and info.has_audio
    assert abs(info.duration - (top.end - top.start)) < 0.25

    # caption-only change re-renders without re-transcribing or re-planning
    st3 = Studio(_cfg(tmp_path, "captions.preset=minimal"))
    [res3] = st3.render(st3.load(p.id), [top.id])
    assert res3.qc.passed and "plan" in res3.cached_stages and "render" not in res3.cached_stages


@needs_whisper
@pytest.mark.parametrize("layout", ["fit_blur", "facecam_split"])
def test_layouts_render_and_pass_qc(tmp_path, sample_short, layout):
    st = Studio(_cfg(tmp_path))
    p = st.open_or_create(input_path=sample_short)
    cs = st.analyze(p)
    c = st.edit_clip(p, cs.candidates[0].id, layout=layout,
                     facecam_region={"x": 0.02, "y": 0.037, "w": 0.25, "h": 0.278})
    [res] = st.render(p, [c.id])
    assert res.qc.passed, [x for x in res.qc.checks if x.status == "fail"]
    face = next(x for x in res.qc.checks if x.name == "caption_avoids_face")
    assert face.status == "pass"


@needs_whisper
def test_auto_facecam_split_and_tracking(tmp_path, sample_face):
    pytest.importorskip("mediapipe")
    st = Studio(_cfg(tmp_path, "paths.models_dir=models"))
    p = st.open_or_create(input_path=sample_face)
    cs = st.analyze(p)
    fc = st.facecam(p)
    if fc is None or fc.detector == "none":
        pytest.skip("no face detector backend available")
    assert fc.method == "face+border" and fc.confidence >= 0.9
    # the panel is drawn at x=40, y=40, 480x300 in a 1920x1080 frame
    assert abs(fc.region.x - 40 / 1920) < 0.01 and abs(fc.region.w - 480 / 1920) < 0.02
    [res] = st.render(p, [cs.candidates[0].id])
    assert res.qc.passed, [x for x in res.qc.checks if x.status == "fail"]
    plan = CropPlan.load(res.output.parent / "cropplan.json")
    assert plan.mode == "facecam_split" and plan.source == "auto"
    assert len(plan.keyframes) > 1  # the moving 'vehicle' is followed
    checks = {c.name: c for c in res.qc.checks}
    assert checks["facecam_visible"].status == "pass"
    assert checks["crop_speed"].status == "pass" and checks["crop_jitter"].status == "pass"


def test_qc_flags_broken_output(tmp_path):
    """QC must fail a black, silent, wrong-size file instead of calling it done."""
    from clipstudio.core.models import CropKeyframe, CropPlan, Panel, PixelRect
    from clipstudio.quality_control.checks import run_qc

    bad = tmp_path / "bad.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=720x720:d=3:r=30",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "3", "-c:v", "libx264",
                    "-c:a", "aac", "-shortest", str(bad)], check=True)
    plan = CropPlan(mode="gameplay", source_width=1920, source_height=1080, out_width=1080, out_height=1920,
                    panels=[Panel(name="gameplay", dest=PixelRect(x=0, y=0, w=1080, h=1920))],
                    keyframes=[CropKeyframe(t=0, gameplay=PixelRect(x=656, y=0, w=608, h=1080))],
                    caption_band=(192, 1612))
    cfg = load_config()
    rep = run_qc("bad", bad, 10.0, plan, None, None, cfg.quality, cfg.captions, expect_audio=True)
    failed = {c.name for c in rep.checks if c.status == "fail"}
    assert not rep.passed
    assert {"resolution", "aspect_ratio", "duration", "black_frames", "frame_content", "loudness"} <= failed


def test_cli_doctor_runs(tmp_path):
    from clipstudio.cli import main

    assert main(["doctor", "--set", f"paths.logs_dir={tmp_path}"]) in (0, 1)
    assert (tmp_path / "doctor.json").exists()
