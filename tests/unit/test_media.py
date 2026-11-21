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
