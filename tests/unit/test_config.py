import pytest

from clipstudio.config import Config, load_config, parse_override


def test_defaults_are_valid():
    c = Config()
    assert c.video.width == 1080 and c.video.height == 1920
    assert c.captions.resolved_style().max_words >= 1


def test_profiles_change_model():
    assert load_config(profile="fast").transcription.model == "base"
    assert load_config(profile="quality").transcription.model == "large-v3-turbo"


def test_override_parsing_and_types():
    assert parse_override("highlights.minimum_score=0.7") == ("highlights.minimum_score", 0.7)
    c = load_config(overrides=["captions.preset=minimal", "video.fps=30"])
    assert c.captions.preset == "minimal" and c.video.fps == 30


@pytest.mark.parametrize("bad", [
    "highlights.minimum_duration=50",           # min > target
    "video.width=1081",                         # odd
    "captions.preset=nope",                     # unknown preset
    "reframing.facecam_region={x: 0.9, y: 0, w: 0.3, h: 0.2}",  # outside frame
    "highlights.weights={audio_intensity: 0, audio_spike: 0, reaction_language: 0, speech_energy: 0, opening_hook: 0, completeness: 0}",
    "processing.nonsense=1",                    # unknown key
])
def test_invalid_values_rejected(bad):
    with pytest.raises(ValueError):
        load_config(overrides=[bad])


def test_section_hash_changes_only_for_that_section():
    a, b = Config(), load_config(overrides=["captions.preset=minimal"])
    assert a.section_hash("transcription") == b.section_hash("transcription")
    assert a.section_hash("captions") != b.section_hash("captions")
