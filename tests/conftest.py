import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipstudio.config import load_config  # noqa: E402
from clipstudio.core.models import Segment, Transcript, Word  # noqa: E402


def whisper_model() -> str | None:
    m = os.environ.get("CLIPSTUDIO_WHISPER_MODEL")
    if m and Path(m).is_dir():
        return m
    return None


@pytest.fixture
def cfg(tmp_path):
    return load_config(files=[], overrides=[f"paths.data_dir={tmp_path / 'data'}", f"paths.logs_dir={tmp_path / 'logs'}"])


def make_transcript(sentences: list[tuple[float, str]], wps: float = 3.0) -> Transcript:
    segs = []
    for i, (t, text) in enumerate(sentences):
        words, cur = [], t
        for tok in text.split():
            d = 1.0 / wps
            words.append(Word(text=tok, start=round(cur, 3), end=round(cur + d * 0.85, 3), probability=0.95))
            cur += d
        segs.append(Segment(id=i, start=words[0].start, end=words[-1].end, text=text, words=words))
    return Transcript(backend="test", model="test", language="en", language_probability=1.0,
                      duration=max(s.end for s in segs) + 5, segments=segs)


@pytest.fixture(scope="session")
def sample_short(tmp_path_factory) -> Path:
    if not (shutil.which("espeak-ng") or shutil.which("espeak")):
        pytest.skip("espeak-ng not installed; cannot synthesise the sample video")
    out = tmp_path_factory.mktemp("media") / "sample_short.mp4"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_video.py"), str(out), "--short"], check=True)
    return out


@pytest.fixture(scope="session")
def sample_face(tmp_path_factory) -> Path:
    """Short synthetic stream whose facecam panel shows a real (public-domain) face."""
    if not (shutil.which("espeak-ng") or shutil.which("espeak")):
        pytest.skip("espeak-ng not installed; cannot synthesise the sample video")
    out = tmp_path_factory.mktemp("media") / "sample_face.mp4"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_video.py"), str(out), "--short",
                    "--face", str(ROOT / "tests" / "fixtures" / "face_nasa_public_domain.jpg")], check=True)
    return out


@pytest.fixture(scope="session")
def sample_events(tmp_path_factory) -> Path:
    """Short synthetic stream with a scene cut, a flash and a desaturated WASTED screen at the big moment."""
    if not (shutil.which("espeak-ng") or shutil.which("espeak")):
        pytest.skip("espeak-ng not installed; cannot synthesise the sample video")
    out = tmp_path_factory.mktemp("media") / "sample_events.mp4"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_sample_video.py"), str(out), "--short", "--events"],
                   check=True)
    return out
