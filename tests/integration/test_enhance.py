"""Ducking, voice chain and punch zoom verified on real encodes (measured, not just built)."""
import subprocess

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
    return load_config(overrides=["reframing.mode=gameplay", "reframing.tracking=false", "video.fps=30",
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
