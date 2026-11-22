"""Ducking, voice chain and punch zoom verified on real encodes (measured, not just built)."""
import subprocess
from pathlib import Path

import numpy as np
import pytest

from clipstudio.config import load_config
from clipstudio.media.ffmpeg import probe
from clipstudio.reframing.layout import plan_layout
from clipstudio.rendering.render import render_clip

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def two_track(tmp_path_factory):
    """6 s source: game track = steady 220 Hz tone, mic track (titled 'Mic') = 1 kHz tone only from 2 s to 4 s."""
    out = tmp_path_factory.mktemp("enh") / "two_track.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=6",
        "-filter_complex", "[1:a]volume=0.4[g];[2:a]volume='between(t,2,4)':eval=frame[m]",
        "-map", "0:v", "-map", "[g]", "-map", "[m]", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "pcm_s16le",
        "-metadata:s:a:0", "title=Game", "-metadata:s:a:1", "title=Mic", str(out)], check=True)
    return out


def _cfg(*ov):
    # full-frame gameplay: the zoom check below compares against the centre of the whole frame
    return load_config(overrides=["reframing.mode=gameplay", "reframing.gameplay_fill=full", "reframing.tracking=false", "video.fps=30",
                                  "export.preset=ultrafast", *ov])


def _render(src, out, cfg, voice=None, punches=None):
    media = probe(src)
    plan = plan_layout(media, cfg.reframing, cfg.video, cfg.captions)
    return render_clip(media, plan, 0.0, 6.0, None, out, cfg.video, cfg.audio, cfg.export, "cpu",
                       voice_track=voice, enhance=cfg.enhance, punches=punches)


def _tone_level(path, freq, t0, t1, sr=48000):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         check=True, capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(np.float64)[int(t0 * sr):int(t1 * sr)]
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(x), 1 / sr)
    return spec[(f > freq - 15) & (f < freq + 15)].max()


def _frame(path, t):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=270:480", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(480, 270).astype(np.float64)


def test_game_audio_ducks_under_voice(tmp_path, two_track):
    on = _render(two_track, tmp_path / "duck.mp4", _cfg(), voice=1)
    off = _render(two_track, tmp_path / "noduck.mp4", _cfg("audio.duck_game=false"), voice=1)
    ratio_on = _tone_level(on, 220, 2.6, 3.6) / _tone_level(on, 220, 0.4, 1.4)
    ratio_off = _tone_level(off, 220, 2.6, 3.6) / _tone_level(off, 220, 0.4, 1.4)
    assert ratio_off > 0.85, ratio_off            # without ducking the game tone is steady
    assert ratio_on < 0.6 * ratio_off, (ratio_on, ratio_off)
    assert _tone_level(on, 1000, 2.6, 3.6) > 5 * _tone_level(on, 1000, 0.4, 1.4)  # voice survives


def test_punch_zoom_changes_only_the_punch_window(tmp_path, two_track):
    plain = _render(two_track, tmp_path / "plain.mp4", _cfg("enhance.punch_zoom=1.0"))
    punch = _render(two_track, tmp_path / "punch.mp4", _cfg("enhance.punch_zoom=1.3"), punches=[3.0])
    before = np.abs(_frame(plain, 1.0) - _frame(punch, 1.0)).mean()
    during = np.abs(_frame(plain, 3.2) - _frame(punch, 3.2)).mean()
    after = np.abs(_frame(plain, 5.5) - _frame(punch, 5.5)).mean()
    assert before < 4 and after < 4, (before, after)
    assert during > 3 * max(before, 1.0), (before, during)
    # zoomed frame == centre of the plain frame scaled up by 1.3
    a, b = _frame(plain, 3.2), _frame(punch, 3.2)
    h, w = a.shape
    ch, cw = int(h / 1.3), int(w / 1.3)
    from PIL import Image
    centre = np.asarray(Image.fromarray(a[(h - ch) // 2:(h - ch) // 2 + ch, (w - cw) // 2:(w - cw) // 2 + cw]
                                        .astype(np.uint8)).resize((w, h), Image.BICUBIC), np.float64)
    assert np.abs(centre - b).mean() < 0.5 * during, (np.abs(centre - b).mean(), during)


def test_cut_removes_a_section(tmp_path, two_track):
    media = probe(two_track)
    cfg = _cfg()
    plan = plan_layout(media, cfg.reframing, cfg.video, cfg.captions)
    out = render_clip(media, plan, 0.0, 6.0, None, tmp_path / "cut.mp4", cfg.video, cfg.audio, cfg.export, "cpu",
                      voice_track=1, enhance=cfg.enhance, cuts=[(1.9, 4.1)])
    info = probe(out)
    assert abs(info.duration - 3.8) < 0.15, info.duration
    # the mic tone (2-4 s in the source) is gone, the game tone continues on both sides of the cut
    full = render_clip(media, plan, 0.0, 6.0, None, tmp_path / "full.mp4", cfg.video, cfg.audio, cfg.export, "cpu",
                       voice_track=1, enhance=cfg.enhance)
    assert _tone_level(out, 1000, 0.2, 3.6) < 0.1 * _tone_level(full, 1000, 2.2, 3.8)
    assert _tone_level(out, 220, 2.2, 3.6) > 0.5 * _tone_level(out, 220, 0.2, 1.6)


def test_platform_copies(tmp_path, two_track):
    from clipstudio.rendering.platforms import export_platforms

    master = _render(two_track, tmp_path / "master.mp4", _cfg())
    ecfg = load_config(overrides=["export.preset=ultrafast",
                                  "export.platform_presets.short={label: Short, max_duration: 3, max_fps: 24}"]).export
    outs = {o.platform: o for o in export_platforms(master, tmp_path / "pl", ecfg, "Big moment", "desc",
                                                    ["tiktok", "short"])}
    assert outs["tiktok"].method == "copy" and not outs["tiktok"].trimmed
    assert abs(probe(Path(outs["tiktok"].path)).duration - probe(master).duration) < 0.1
    s = outs["short"]
    assert s.method == "reencode" and s.trimmed and len(s.reasons) == 2
    info = probe(Path(s.path))
    assert abs(info.duration - 3.0) < 0.1 and abs(info.fps - 24) < 0.01 and info.has_audio
    import json
    side = json.loads(open(s.sidecar).read())
    assert side["title"] == "Big moment" and side["file"].endswith(".short.mp4")


def test_black_edges_detected_on_a_real_encode(tmp_path):
    from clipstudio.media.ffmpeg import black_edge_trim, black_segments

    src = tmp_path / "fade.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
                    "-vf", "drawbox=c=black:t=fill:enable='lt(t,0.6)+gt(t,7.5)'", "-c:v", "libx264",
                    "-preset", "ultrafast", str(src)], check=True)
    segs = black_segments(src, 0.0, 8.0)
    lead, trail = black_edge_trim(segs, 8.0)
    assert 0.55 <= lead <= 0.75 and 0.45 <= trail <= 0.65, segs
    # a window that starts later sees no leading black
    assert black_edge_trim(black_segments(src, 2.0, 6.0), 4.0) == (0.0, 0.0)


def test_late_video_start_stays_in_sync(tmp_path):
    """Live/HLS recordings: the video stream starts after the audio. A flash and a beep that coincide in the
    source must coincide in the clip, and both streams must be the full clip length."""
    src = tmp_path / "late.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-itsoffset", "0.6", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30:duration=20,drawbox=c=white:t=fill:enable='between(t,9.4,9.6)'",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=21",
                    "-filter_complex", "[1:a]volume='if(between(t,10,10.2),0.9,0.0)':eval=frame[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                    "-c:a", "aac", str(src)], check=True)
    cfg = _cfg("audio.normalization=false")
    media = probe(src)
    plan = plan_layout(media, cfg.reframing, cfg.video, cfg.captions)
    for start in (0.0, 4.0):
        out = tmp_path / f"o{start}.mp4"
        render_clip(media, plan, start, start + 12.0, None, out, cfg.video, cfg.audio, cfg.export, "cpu",
                    enhance=cfg.enhance)
        durs = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0",
                               str(out)], check=True, capture_output=True, text=True).stdout.split()
        d = {k: float(v) for k, v in (x.split(",") for x in durs)}
        assert abs(d["video"] - d["audio"]) < 0.1 and abs(d["video"] - 12.0) < 0.1, d
        # flash time from luma, beep time from audio level
        lum = [_frame(out, t / 30).mean() for t in range(int((10 - start - 0.5) * 30), int((10 - start + 0.5) * 30))]
        flash = (int((10 - start - 0.5) * 30) + int(np.argmax(lum))) / 30
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                             check=True, capture_output=True).stdout
        x = np.abs(np.frombuffer(raw, np.int16).astype(float))
        beep = float(np.argmax(x > 0.3 * x.max())) / 8000
        assert abs(flash - beep) < 0.1, (start, flash, beep)
