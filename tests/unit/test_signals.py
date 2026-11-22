"""Phase 3 signals: visual events, chat replay formats and scoring, and their effect on candidates."""
import json

import numpy as np

from clipstudio.chat.replay import activity, chat_peaks, chat_score, find_sidecar, load_chat
from clipstudio.config import load_config
from clipstudio.core.models import LoudnessTrack, VisualEvent, VisualTrack
from clipstudio.highlights.candidates import Signals, generate_candidates
from clipstudio.visual_analysis.signals import detect_events
from tests.conftest import make_transcript

FPS = 4.0


def _arrays(n=600):
    rng = np.random.default_rng(0)
    hist = np.abs(rng.normal(0.03, 0.01, n)).astype(np.float32)
    bright = np.full(n, 90.0, np.float32) + rng.normal(0, 2, n).astype(np.float32)
    sat = np.full(n, 0.45, np.float32) + rng.normal(0, 0.01, n).astype(np.float32)
    hist[200] = 0.8                      # hard cut at 50 s
    bright[300:302] = 230                # 0.5 s white flash at 75 s
    sat[400:416] = 0.05                  # 4 s colour drain at 100 s (death screen)
    return {"hist_change": hist, "brightness": bright, "saturation": sat, "motion": np.ones(n, np.float32)}


def test_detect_events_finds_cut_flash_and_desaturation():
    cfg = load_config().visual
    cuts, ev = detect_events(_arrays(), FPS, cfg)
    assert cuts == [50.0]
    kinds = {e.kind: e for e in ev}
    assert abs(kinds["flash"].start - 75.0) < 0.3
    assert abs(kinds["desaturation"].start - 100.0) < 0.3 and kinds["desaturation"].end - kinds["desaturation"].start >= 3.5


def test_chat_formats(tmp_path):
    td = tmp_path / "vod.chat.json"
    td.write_text(json.dumps({"comments": [
        {"content_offset_seconds": 12.5, "commenter": {"display_name": "a"}, "message": {"body": "KEKW"}},
        {"content_offset_seconds": 3.0, "commenter": {"display_name": "b"}, "message": {"body": "hi"}}]}))
    ch = load_chat(td)
    assert ch.format == "twitchdownloader" and [m.t for m in ch.messages] == [3.0, 12.5]

    cd = tmp_path / "cd.json"
    cd.write_text(json.dumps([{"time_in_seconds": 5, "message": "pog", "author": {"name": "x"}}]))
    assert load_chat(cd).format == "chat_downloader"

    yt = tmp_path / "v.live_chat.json"
    line = {"replayChatItemAction": {"videoOffsetTimeMsec": "4200", "actions": [{"addChatItemAction": {"item": {
        "liveChatTextMessageRenderer": {"message": {"runs": [{"text": "no way"}]}, "authorName": {"simpleText": "y"}}}}}]}}
    yt.write_text(json.dumps(line) + "\n" + json.dumps(line) + "\n")
    c = load_chat(yt)
    assert c.format == "youtube_live_chat" and c.messages[0].t == 4.2 and c.messages[0].text == "no way"

    csvf = tmp_path / "c.csv"
    csvf.write_text("time,user,message\n0:01:05,z,LUL\n7,z,hello\n")
    c = load_chat(csvf)
    assert c.format == "csv" and [m.t for m in c.messages] == [7.0, 65.0]

    video = tmp_path / "vod.mp4"
    video.write_bytes(b"x")
    assert find_sidecar(video) == td


def _chat_track(spike_at=60, n=300):
    from clipstudio.core.models import ChatMessage, ChatTrack
    msgs = [ChatMessage(t=float(t), text="hello") for t in range(0, n, 3)]
    msgs += [ChatMessage(t=spike_at + i * 0.1, text="KEKW no way") for i in range(60)]
    return ChatTrack(source="t", format="test", messages=sorted(msgs, key=lambda m: m.t))


def test_chat_score_and_peaks():
    kw = load_config().chat.keywords
    rate, hype = activity(_chat_track(), 300, kw)
    assert chat_score(rate, hype, 55, 70, 5) > 0.8
    assert chat_score(rate, hype, 150, 165, 5) < 0.15
    assert any(50 <= p <= 66 for p in chat_peaks(rate, 5))


def _loud(n_s=300):
    return LoudnessTrack(window=0.5, rms_db=[-30.0] * (n_s * 2), median_db=-30.0, p90_db=-30.0)


def test_signals_create_and_lift_candidates():
    cfg = load_config(overrides=["highlights.minimum_duration=10", "highlights.maximum_duration=30",
                                 "highlights.target_duration=20"]).highlights
    tr = make_transcript([(20, "just driving around the city for now nothing much"),
                          (200, "okay let me check the map real quick")])
    base = generate_candidates(tr, _loud(), cfg, 300)
    assert set(base.weights) == {"audio_intensity", "audio_spike", "reaction_language", "speech_energy",
                                 "opening_hook", "completeness", "payoff_position"}
    vis = VisualTrack(sample_fps=4, motion=[1.0] * 1200, motion_median=1.0, scene_cuts=[118.0],
                      events=[VisualEvent(kind="desaturation", start=120, end=124, strength=0.9),
                              VisualEvent(kind="hud_text", start=120.6, end=121.6, strength=1, label="WASTED")])
    rate, hype = activity(_chat_track(spike_at=124), 300, load_config().chat.keywords)
    cs = generate_candidates(tr, _loud(), cfg, 300, Signals(visual=vis, chat_rate=rate, chat_hype=hype))
    assert {"game_event", "chat_activity", "visual_action"} <= set(cs.weights)
    top = cs.candidates[0]
    assert top.start <= 120.6 <= top.end and top.features["game_event"] == 1.0 and top.features["chat_activity"] > 0.5
    assert "WASTED" in top.title or "WASTED" in top.reason
    # the clip opens well before the death screen (what led to it), not on the cut 2 s before it
    assert top.start <= 120.0 - 5.0


def test_live_anchor_frames_the_clip_on_the_trigger_moment():
    from clipstudio.highlights.candidates import position_score
    cfg = load_config(overrides=["highlights.minimum_duration=10", "highlights.maximum_duration=30",
                                 "highlights.target_duration=20"]).highlights
    # chatter at the start of the saved moment, the thing chat reacted to much later
    tr = make_transcript([(2, "okay so where are we going now let me look"), (41, "oh my god no way")])
    plain = generate_candidates(tr, _loud(60), cfg, 60)
    assert "trigger_moment" not in plain.weights
    cs = generate_candidates(tr, _loud(60), cfg, 60, Signals(anchor=40.0))
    best = cs.candidates[0]
    assert best.start < 40.0 < best.end and best.features["trigger_moment"] == 1.0
    # setup before, reaction after: the moment is neither the first nor the last thing in the clip
    assert 0.3 <= (40.0 - best.start) / best.duration <= 0.85
    assert position_score(1.0, 0, 20) < 0.2 and position_score(19.8, 0, 20) == 0.0 and position_score(12, 0, 20) == 1.0


def test_tighten_trims_quiet_edges_but_not_speech_or_the_minimum():
    from clipstudio.highlights.candidates import build_sentences, tighten
    loud = LoudnessTrack(window=0.5, rms_db=[-40.0] * 20 + [-20.0] * 20 + [-40.0] * 20, median_db=-40.0, p90_db=-20.0)
    s, e = tighten(0.0, 30.0, [], loud, keep=1.0, min_dur=5.0)
    assert (s, e) == (9.0, 21.0)                       # 1 s of quiet kept around the loud part
    s, e = tighten(0.0, 30.0, [], loud, keep=1.0, min_dur=20.0)
    assert e - s >= 20.0 - 1e-6                        # never shorter than the minimum
    sents = build_sentences(make_transcript([(3, "wait what is that")]))
    s, e = tighten(0.0, 30.0, sents, loud, keep=1.0, min_dur=5.0)
    assert s <= 3.0 - 0.25 + 1e-6                      # words are never cut off


def _loud_with(peaks, n_s=60, base=-40.0):
    db = [base] * (n_s * 2)
    for t in peaks:
        db[int(t * 2)] = base + 20.0
        db[int(t * 2) + 1] = base + 20.0
    return LoudnessTrack(window=0.5, rms_db=db, median_db=base, p90_db=base)


def test_a_clip_needs_a_cause_and_a_reaction_together():
    from clipstudio.chat.replay import is_reaction
    from clipstudio.highlights.candidates import build_sentences, cause_time, evidence_at, find_payoff, lead_in_start
    # chat that reacts vs chat that is just busy
    assert is_reaction("LMAOOO") and is_reaction("no way") and is_reaction("hahaha") and is_reaction("WHAT?!")
    assert not is_reaction("W") and not is_reaction("giveaway time") and not is_reaction("hi from brazil")

    sents = build_sentences(make_transcript([(8, "so we take the highway then left at the light"),
                                             (31, "oh my god no way")]))
    loud = _loud_with([30])
    sig = Signals()
    assert evidence_at(30.0, sents, loud, sig) == ["audio", "speech"]
    assert evidence_at(12.0, sents, loud, sig) == []                 # same clip, nothing happening there
    t, ev = find_payoff(sents, loud, sig, 5.0, 45.0)
    assert abs(t - 30.0) <= 3.0 and ev == ["audio", "speech"]
    # a loud sound at 10 s and a "wow" at 40 s are two things, not one moment
    apart = build_sentences(make_transcript([(40, "oh my god no way")]))
    assert all(len(evidence_at(float(x), apart, _loud_with([10]), sig)) < 2 for x in range(5, 50))
    # busy chat with no reactions in it is not a sign; laughing chat is
    rate = np.zeros(60, np.float32)
    rate[30:40] = 4
    quiet = Signals(chat_rate=rate, chat_hype=np.zeros(60, np.float32), chat_react=np.zeros(60, np.float32))
    assert "chat" not in evidence_at(30.0, [], _loud_with([]), quiet)
    react = np.zeros(60, np.float32)
    react[31:36] = 2
    assert "chat" in evidence_at(30.0, [], _loud_with([]), Signals(chat_rate=rate, chat_hype=react, chat_react=react))

    # the clip starts before the cause (the crash at 14 s), not on its aftermath
    assert cause_time(30.0, _loud_with([14, 30]), sig) == 14.0
    assert cause_time(30.0, _loud_with([]), sig) is None
    # lead-in runs back over the talk that leads straight into the moment, but not over unrelated talk long before
    talk = build_sentences(make_transcript([(2, "anyway thanks for the sub"), (17, "watch this watch this"),
                                            (19.5, "i am going to jump it")]))
    assert abs(lead_in_start(30.0, 20.0, talk, 8.0, 26.0) - 16.75) < 0.01

    cfg = load_config(overrides=["highlights.minimum_duration=10", "highlights.maximum_duration=40",
                                 "highlights.target_duration=20", "highlights.minimum_score=0"]).highlights
    # loud game audio alone, nobody reacting: rejected
    calm = make_transcript([(20, "just driving around the city for now nothing much")])
    cs = generate_candidates(calm, _loud_with([22]), cfg, 60)
    assert cs.candidates and not any(c.passes_threshold for c in cs.candidates)
    assert "probably not a real moment" in cs.candidates[0].reason
    # a crash at 14 s and the streamer reacting at 30 s: kept, and the clip opens before the crash
    tr = make_transcript([(31, "oh my god no way bro what was that")])
    cs = generate_candidates(tr, _loud_with([14, 30]), cfg, 60)
    best = cs.candidates[0]
    assert best.passes_threshold and best.start <= 14.0 - 4.0 and best.end >= 36.0
    off = cfg.model_copy(update={"min_evidence": 0})
    assert all(c.passes_threshold for c in generate_candidates(calm, _loud_with([22]), off, 60).candidates)


def test_start_is_pulled_back_over_the_build_up_only():
    from clipstudio.highlights.candidates import build_sentences, extend_setup

    def vis(motion_per_s, cuts=()):
        return VisualTrack(sample_fps=4, motion=[m for m in motion_per_s for _ in range(4)], motion_median=1.0,
                           scene_cuts=list(cuts), events=[])
    race = vis([8.0] * 20 + [1.0] * 30)           # 20 s of racing, then the results screen
    assert extend_setup(15.0, 40.0, race, [], 12.0, 45.0) == 3.0          # back over the race, capped at 12 s
    assert extend_setup(15.0, 40.0, race, [], 12.0, 30.0) == 10.0         # never longer than the maximum
    assert extend_setup(15.0, 40.0, vis([8.0] * 20 + [1.0] * 30, cuts=[9.0]), [], 12.0, 45.0) == 9.0   # stops at a cut
    # calm footage before the clip (talking to chat, a menu): nothing is pulled in
    talk = vis([1.0] * 14 + [8.0] * 6 + [1.0] * 30)
    assert extend_setup(15.0, 40.0, talk, [], 12.0, 45.0) == 14.0
    assert extend_setup(15.0, 40.0, vis([1.0] * 50), [], 12.0, 45.0) == 15.0
    # the new start does not land inside a sentence
    sents = build_sentences(make_transcript([(2.0, "keep it steady keep it steady come on")]))
    assert extend_setup(15.0, 40.0, race, sents, 12.0, 45.0) <= 2.0
