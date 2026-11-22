"""Live mode pieces that don't need a stream: buffer bookkeeping, chat parsing, triggers, upload policy."""
from pathlib import Path

from clipstudio.config import load_config
from clipstudio.live.buffer import Ring, Segment
from clipstudio.live.chat import ReplayChat, channel_from_url, parse_irc_line, youtube_chat_lines
from clipstudio.live.session import upload_decision
from clipstudio.live.triggers import TriggerDetector


def _seg(tmp_path: Path, i: int, dur=2.0, arrived=None) -> Segment:
    p = tmp_path / f"seg_{i:06d}.ts"
    p.write_bytes(b"x" * 10)
    return Segment(i, p, i * dur, (i + 1) * dur, arrived if arrived is not None else 1000.0 + (i + 1) * dur)


def test_ring_keeps_only_the_buffer_unless_pinned(tmp_path):
    r = Ring(buffer_seconds=10)
    for i in range(10):
        r.add(_seg(tmp_path, i))
    r.pin("m1", 1.0, 3.0)  # segments 0 and 1
    r.prune()
    kept = [s.index for s in r.segments]
    assert kept == [0, 1, 4, 5, 6, 7, 8, 9]          # head 20 s: anything ending before 10 s goes, except the pin
    assert not (tmp_path / "seg_000002.ts").exists() and (tmp_path / "seg_000000.ts").exists()
    r.unpin("m1")
    r.prune()
    assert [s.index for s in r.segments] == [4, 5, 6, 7, 8, 9]
    assert r.deleted == 4
    assert [s.index for s in r.covering(9.5, 12.5)] == [4, 5, 6]


def test_stream_time_maps_wall_clock_onto_video(tmp_path):
    r = Ring(60)
    for i in range(3):
        r.add(_seg(tmp_path, i))  # segment i ends at stream 2(i+1), arrived at 1000 + 2(i+1)
    assert r.stream_time_at(1004.0) == 4.0
    assert r.stream_time_at(1005.5) == 5.5
    assert r.stream_time_at(1001.0) == 1.0


def test_chat_parsers():
    assert parse_irc_line("@badge-info=;color=#FF0000 :viewer1!viewer1@viewer1.tmi.twitch.tv PRIVMSG #ludwig :KEKW no way\r\n") \
        == ("viewer1", "KEKW no way")
    assert parse_irc_line(":tmi.twitch.tv 001 justinfan123 :Welcome") is None
    line = ('{"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": '
            '{"message": {"runs": [{"text": "clip it "}, {"emoji": {"shortcuts": [":fire:"]}}]}, '
            '"authorName": {"simpleText": "fan"}}}}}], "videoOffsetTimeMsec": "1000"}}')
    assert youtube_chat_lines(line) == [("fan", "clip it :fire:")]
    assert youtube_chat_lines("not json") == []
    assert channel_from_url("https://www.twitch.tv/Ludwig") == "ludwig"
    assert channel_from_url("https://www.youtube.com/@ludwig/live") == "ludwig"
    assert channel_from_url("https://www.youtube.com/watch?v=abc") is None


def test_replay_chat_follows_stream_time(tmp_path):
    import json

    f = tmp_path / "c.json"
    f.write_text(json.dumps([{"time_in_seconds": t, "message": "hi", "author": {"name": "u"}} for t in (1, 5, 9)]))
    rc = ReplayChat(f)
    assert [m.t for m in rc.poll(4)] == [1.0]
    assert [m.t for m in rc.poll(10)] == [5.0, 9.0]


def _detector(**ov):
    cfg = load_config(overrides=[f"live.{k}={v}" for k, v in ov.items()])
    return TriggerDetector(cfg.live, cfg.chat), cfg


def test_audio_jump_triggers_without_chat():
    det, _ = _detector(warmup_seconds=20, cooldown_seconds=30)
    det.add_audio({s: (-30.0 if not 60 <= s < 63 else -18.0) for s in range(120)})
    trs = det.evaluate(120) + det.flush()
    assert len(trs) == 1 and 59 <= trs[0].t <= 62 and trs[0].reasons == ["audio"]


def test_steady_noise_does_not_trigger():
    import numpy as np

    det, _ = _detector()
    rng = np.random.default_rng(1)
    det.add_audio({s: float(-25 + rng.normal(0, 1.5)) for s in range(300)})
    for s in range(0, 300, 3):
        det.add_chat(s + 0.5, "hello chat")
    assert det.evaluate(300) + det.flush() == []


def test_chat_burst_and_clip_commands_trigger_at_the_peak():
    det, _ = _detector(warmup_seconds=20)
    det.add_audio({s: -25.0 for s in range(120)})
    for s in range(0, 120, 3):
        det.add_chat(s + 0.2, "just chatting")
    for i in range(30):
        det.add_chat(80 + i * 0.1, "KEKW no way")
    for i in range(4):
        det.add_chat(82 + i * 0.5, "clip it")
    trs = det.evaluate(120) + det.flush()
    assert len(trs) == 1
    t = trs[0]
    assert 77 <= t.t <= 82 and {"chat", "hype", "command"} <= set(t.reasons), t


def test_cooldown_merges_back_to_back_spikes():
    det, _ = _detector(warmup_seconds=20, cooldown_seconds=60)
    det.add_audio({s: (-18.0 if s in (60, 61, 80, 81, 160, 161) else -30.0) for s in range(220)})
    trs = det.evaluate(220) + det.flush()
    assert [round(t.t) for t in trs][:1] and len(trs) == 2  # 60 and 160; 80 is inside the cooldown


def test_upload_needs_allowlist_and_qc():
    cfg = load_config(overrides=["upload.allow_channels=[Ludwig]"])
    assert upload_decision(cfg, "ludwig", True)[0] == "allowed"
    assert upload_decision(cfg, "ludwig", False)[0] == "review"
    assert upload_decision(cfg, "someone_else", True)[0] == "review"
    assert upload_decision(cfg, None, True)[0] == "review"
    assert upload_decision(load_config(), "ludwig", True)[0] == "review"  # nobody is allowed by default


def test_edit_gate_is_shared_and_limits_parallel_edits():
    """Every stream's editor waits on the same gate, so N streams never edit more than max_parallel_edits at once."""
    import threading
    import time

    from clipstudio.live.session import edit_gate

    assert edit_gate(1) is edit_gate(1) and edit_gate(2) is not edit_gate(1)
    busy, peak, lock = [0], [0], threading.Lock()

    def edit():
        with edit_gate(2):
            with lock:
                busy[0] += 1
                peak[0] = max(peak[0], busy[0])
            time.sleep(0.05)
            with lock:
                busy[0] -= 1

    ts = [threading.Thread(target=edit) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert peak[0] == 2
