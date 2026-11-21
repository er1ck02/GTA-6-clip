import numpy as np

from clipstudio.config import load_config
from clipstudio.core.models import LoudnessTrack
from clipstudio.highlights.candidates import generate_candidates

from tests.conftest import make_transcript

CALM = "so we are just driving around the city right now nothing much going on here."
HYPE = "oh my god no way the car just exploded did you see that! that was insane! lets go!"


def _loud(duration, loud_ranges, window=0.5):
    n = int(duration / window)
    db = np.full(n, -30.0)
    for a, b in loud_ranges:
        db[int(a / window):int(b / window)] = -12.0
    return LoudnessTrack(window=window, rms_db=db.tolist(), median_db=-30.0, p90_db=-12.0)


def _setup():
    sents = []
    t = 0.0
    for i in range(30):
        text = HYPE if 12 <= i <= 14 else CALM
        sents.append((t, text))
        t += 6.0
    tr = make_transcript(sents)
    return tr, _loud(tr.duration, [(72, 90)])


def test_hype_section_ranks_first_and_bounds_hold():
    tr, loud = _setup()
    cfg = load_config(overrides=["highlights.minimum_duration=12", "highlights.maximum_duration=30",
                                 "highlights.target_duration=20"]).highlights
    cs = generate_candidates(tr, loud, cfg, tr.duration)
    top = cs.candidates[0]
    assert 60 <= top.peak_time <= 90
    assert top.category == "reaction"
    for c in cs.candidates:
        assert cfg.minimum_duration <= c.duration <= cfg.maximum_duration + 1e-6
        assert set(c.contributions) == set(cfg.weights.model_dump())
        assert c.reason and c.transcript
    assert cs.candidates[0].score > cs.candidates[-1].score


def test_no_duplicates_of_same_event():
    tr, loud = _setup()
    cfg = load_config(overrides=["highlights.minimum_duration=12", "highlights.maximum_duration=30",
                                 "highlights.target_duration=20"]).highlights
    cs = generate_candidates(tr, loud, cfg, tr.duration)
    for i, a in enumerate(cs.candidates):
        for b in cs.candidates[i + 1:]:
            assert not (b.start <= a.peak_time <= b.end)


def test_weights_change_ranking():
    tr, loud = _setup()
    base = ["highlights.minimum_duration=12", "highlights.maximum_duration=30", "highlights.target_duration=20"]
    only_speech = load_config(overrides=base + [
        "highlights.weights={audio_intensity: 0, audio_spike: 0, reaction_language: 0, speech_energy: 1, opening_hook: 0, completeness: 0}"
    ]).highlights
    cs = generate_candidates(tr, loud, only_speech, tr.duration)
    assert all(abs(c.contributions["speech_energy"] - c.features["speech_energy"]) < 1e-3 for c in cs.candidates)


def test_deterministic_ids():
    tr, loud = _setup()
    cfg = load_config().highlights
    a = generate_candidates(tr, loud, cfg, tr.duration)
    b = generate_candidates(tr, loud, cfg, tr.duration)
    assert [c.id for c in a.candidates] == [c.id for c in b.candidates]
