"""Cross-platform behaviour found on a Windows 11 + FFmpeg 9.1 run: filter list format, CUDA DLLs,
Ctrl+Break, fonts without fontconfig, silent mic streams and black clip edges."""
import shutil
import signal
import wave
from pathlib import Path

import numpy as np
import pytest

from clipstudio.captions import engine
from clipstudio.core import compat
from clipstudio.media.ffmpeg import black_edge_trim, parse_filter_list, wav_loudest_dbfs

FFMPEG8 = """Filters:
  T.. = Timeline support
 ... afir              N->N       Apply Finite Impulse Response filter with supplied coefficients in additional stream(s).
 TSC ass               V->V       Render ASS subtitles onto input video using the libass library.
 T.. loudnorm          A->A       EBU R128 loudness normalization
"""
FFMPEG9 = """Filters:
  T. = Timeline support
  .S = Slice threading
 .. afir              N->N       Apply Finite Impulse Response filter with supplied coefficients in additional stream(s).
 TS ass               V->V       Render ASS subtitles onto input video using the libass library.
 T. loudnorm          A->A       EBU R128 loudness normalization
"""


@pytest.mark.parametrize("text", [FFMPEG8, FFMPEG9])
def test_filter_list_parses_ffmpeg_8_and_9(text):
    assert parse_filter_list(text) == {"afir", "ass", "loudnorm"}


def test_black_edge_trim():
    assert black_edge_trim([], 30) == (0.0, 0.0)
    assert black_edge_trim([(0.0, 0.58)], 30) == (0.62, 0.0)
    assert black_edge_trim([(29.4, 30.0)], 30) == (0.0, 0.64)
    assert black_edge_trim([(10.0, 11.0)], 30) == (0.0, 0.0)  # black in the middle is QC's business


def _wav(path: Path, x: np.ndarray, sr=16000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype(np.int16).tobytes())


def test_wav_loudest_dbfs(tmp_path):
    _wav(tmp_path / "silent.wav", np.zeros(16000 * 3))
    _wav(tmp_path / "hiss.wav", np.random.default_rng(0).normal(0, 10 ** (-91 / 20), 16000 * 3))
    t = np.arange(16000 * 3) / 16000
    _wav(tmp_path / "tone.wav", 0.5 * np.sin(2 * np.pi * 440 * t) * (t > 1))
    assert wav_loudest_dbfs(tmp_path / "silent.wav") == -120.0
    assert wav_loudest_dbfs(tmp_path / "hiss.wav") < -60
    assert -10 < wav_loudest_dbfs(tmp_path / "tone.wav") < -5


def test_cuda_check_loads_cublas_not_just_counts_devices(monkeypatch):
    import ctranslate2

    compat.cuda_usable.cache_clear()
    monkeypatch.setattr(ctranslate2, "get_cuda_device_count", lambda: 1)
    monkeypatch.setattr(compat, "load_cublas", lambda: None)
    ok, detail = compat.cuda_usable()
    assert not ok and "cuBLAS" in detail
    compat.cuda_usable.cache_clear()
    monkeypatch.setattr(compat, "load_cublas", lambda: "libcublas.so.12")
    assert compat.cuda_usable()[0]
    compat.cuda_usable.cache_clear()


def test_resolve_device_falls_back_to_cpu(monkeypatch):
    from clipstudio.transcription import base

    monkeypatch.setattr(compat, "cuda_usable", lambda: (False, "1 CUDA device(s) but cuBLAS could not be loaded"))
    assert base.resolve_device("auto") == "cpu"
    monkeypatch.setattr(compat, "cuda_usable", lambda: (True, "ok"))
    assert base.resolve_device("auto") == "cuda"
    assert base.resolve_device("cpu") == "cpu"


def test_nvidia_dll_dirs_found_on_sys_path(tmp_path, monkeypatch):
    (tmp_path / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (tmp_path / "nvidia" / "cudnn" / "bin").mkdir(parents=True)
    monkeypatch.setattr("sys.path", [str(tmp_path)])
    assert compat.nvidia_dll_dirs() == [tmp_path / "nvidia" / "cublas" / "bin", tmp_path / "nvidia" / "cudnn" / "bin"]


def test_ctrl_break_maps_to_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr(signal, "SIGBREAK", signal.SIGUSR1, raising=False)
    old = signal.getsignal(signal.SIGUSR1)
    try:
        compat.install_interrupt_handlers()
        assert signal.getsignal(signal.SIGUSR1) is signal.default_int_handler
    finally:
        signal.signal(signal.SIGUSR1, old)


def test_fonts_found_without_fontconfig(tmp_path, monkeypatch):
    src = Path("/usr/share/fonts/truetype/dejavu")
    if not (src / "DejaVuSans-Bold.ttf").exists():
        pytest.skip("DejaVu fonts not installed")
    for f in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        shutil.copy(src / f, tmp_path / f)
    monkeypatch.setattr(compat, "font_dirs", lambda: [tmp_path])
    monkeypatch.setattr(engine.shutil, "which", lambda name: None)  # no fc-list, as on Windows
    engine._font_index.cache_clear()
    engine.resolve_font.cache_clear()
    try:
        fam, f = engine.resolve_font(("Montserrat", "DejaVu Sans"), True)
        assert fam == "DejaVu Sans" and f == str(tmp_path / "DejaVuSans-Bold.ttf")
        fam, f = engine.resolve_font(("DejaVu Sans",), False)
        assert f == str(tmp_path / "DejaVuSans.ttf")
    finally:
        engine._font_index.cache_clear()
        engine.resolve_font.cache_clear()


def test_source_black_seconds_respects_bounds_and_cuts():
    from clipstudio.pipeline.studio import source_black_seconds

    segs = [(99.0, 100.5), (105.0, 105.4), (108.0, 109.0)]
    assert source_black_seconds(segs, 100.0, 110.0) == 1.9
    assert source_black_seconds(segs, 100.0, 110.0, cuts=[(107.5, 108.5)]) == 1.4
    assert source_black_seconds(segs, 101.0, 104.0) == 0.0


def test_facecam_prefers_an_edge_face_over_a_frequent_mid_screen_one():
    """Seen on a GTA VI stream: the character creator kept the game character's face mid-screen in most
    samples, while the streamer's webcam sat at the left edge."""
    from clipstudio.vision.facecam import _cluster_faces, overlay_prior
    from clipstudio.vision.faces import FaceBox

    game = FaceBox(0.47, 0.30, 0.10, 0.16, 0.9)     # centre of the screen
    cam = FaceBox(0.07, 0.40, 0.05, 0.08, 0.8)      # left edge, small
    per_frame = [[game] + ([cam] if i % 2 == 0 else []) for i in range(20)]
    faces, hit = _cluster_faces(per_frame)
    assert abs(faces[0].cx - cam.cx) < 0.01 and hit == 0.5
    assert overlay_prior(game) < 0.3 and overlay_prior(cam) == 1.0


def test_replace_file_retries_windows_sharing_violation(tmp_path, monkeypatch):
    """A destination held open by a reader (WinError 5 on Windows) is retried, not fatal."""
    import os

    from clipstudio.core import compat

    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(compat.os, "replace", flaky)
    dst = tmp_path / "project.json"
    dst.write_text("old")
    compat.write_text_atomic(dst, '{"ok": true}')
    assert dst.read_text() == '{"ok": true}'
    assert calls["n"] == 3
    assert list(tmp_path.glob("*.tmp")) == []  # temp file never left behind


def test_replace_file_gives_up_eventually(tmp_path, monkeypatch):
    import pytest

    from clipstudio.core import compat

    def locked(src, dst):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(compat.os, "replace", locked)
    with pytest.raises(PermissionError):
        compat.write_text_atomic(tmp_path / "x.json", "{}")
    assert list(tmp_path.glob("*.tmp")) == []
