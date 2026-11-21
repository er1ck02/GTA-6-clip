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
                                 "opening_hook", "completeness"}
    vis = VisualTrack(sample_fps=4, motion=[1.0] * 1200, motion_median=1.0, scene_cuts=[118.0],
                      events=[VisualEvent(kind="desaturation", start=120, end=124, strength=0.9),
                              VisualEvent(kind="hud_text", start=120.6, end=121.6, strength=1, label="WASTED")])
    rate, hype = activity(_chat_track(spike_at=124), 300, load_config().chat.keywords)
    cs = generate_candidates(tr, _loud(), cfg, 300, Signals(visual=vis, chat_rate=rate, chat_hype=hype))
    assert {"game_event", "chat_activity", "visual_action"} <= set(cs.weights)
    top = cs.candidates[0]
    assert top.start <= 120.6 <= top.end and top.features["game_event"] == 1.0 and top.features["chat_activity"] > 0.5
    assert "WASTED" in top.title or "WASTED" in top.reason
    assert top.start == 118.0  # snapped to the scene cut just before the event
