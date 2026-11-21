from clipstudio.core.models import AudioStream, MediaInfo
from clipstudio.media.ffmpeg import pick_speech_track


def _m(titles):
    return MediaInfo(path="/x", duration=10, width=1920, height=1080, fps=60, video_codec="h264", has_audio=bool(titles),
                     size_bytes=1, container="mp4",
                     audio_streams=[AudioStream(order=i, codec="aac", title=t) for i, t in enumerate(titles)])


def test_auto_prefers_named_mic_track():
    assert pick_speech_track(_m(["Game", "Mic"]), "auto") == [1]


def test_auto_mixes_unnamed_tracks():
    assert pick_speech_track(_m(["SoundHandler", "SoundHandler"]), "auto") == [0, 1]


def test_explicit_and_mix():
    assert pick_speech_track(_m(["Game", "Mic"]), "0") == [0]
    assert pick_speech_track(_m(["Game", "Mic"]), "mix") == [0, 1]
    assert pick_speech_track(_m([]), "auto") == []


def test_wav_memmap_and_window_rms_match_full_read(tmp_path):
    import wave

    import numpy as np

    from clipstudio.media.ffmpeg import read_wav_float, wav_memmap, window_rms

    rng = np.random.default_rng(0)
    x = (rng.standard_normal(16000 * 3 + 123) * 3000).astype(np.int16)
    f = tmp_path / "a.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(x.tobytes())
    mm, sr = wav_memmap(f)
    full, sr2 = read_wav_float(f)
    assert sr == sr2 == 16000 and len(mm) == len(full)
    assert np.allclose(np.asarray(mm, dtype=np.float32) / 32768.0, full)
    win = 800
    naive = np.sqrt(np.mean(full[: len(full) // win * win].reshape(-1, win).astype(np.float64) ** 2, axis=1))
    assert np.allclose(window_rms(mm, win, block_windows=7), naive)
    assert np.allclose(window_rms(full, win), naive)
