from clipstudio.captions.engine import build_caption_track, write_ass, write_srt
from clipstudio.config import load_config
from clipstudio.core.models import MediaInfo
from clipstudio.reframing.layout import plan_layout

from tests.conftest import make_transcript


def _media(w=1920, h=1080):
    return MediaInfo(path="/x.mp4", duration=100, width=w, height=h, fps=30, video_codec="h264", has_audio=True,
                     size_bytes=1, container="mp4")


def _track(preset="bold_viral", **ov):
    cfg = load_config(overrides=[f"captions.preset={preset}"] + [f"{k}={v}" for k, v in ov.items()])
    tr = make_transcript([(10.0, "Oh my god no way the car just exploded."), (13.5, "Did you see that? That was insane!"),
                          (17.0, "We are so back chat lets go")])
    plan = plan_layout(_media(), cfg.reframing, cfg.video, cfg.captions)
    return cfg, plan, build_caption_track(tr.words(), 9.0, 20.0, cfg.captions, plan), tr


def test_chunks_cover_every_word_without_overlap():
    cfg, plan, track, tr = _track()
    words = [w for c in track.chunks for ln in c.lines for w in ln]
    assert len(words) == len(tr.words_between(9, 20)) == track.source_word_count
    for a, b in zip(track.chunks, track.chunks[1:]):
        assert a.end <= b.start + 1e-6
    assert all(0 <= c.start < c.end <= 11.0 for c in track.chunks)


def test_word_times_are_clip_relative():
    _, _, track, tr = _track()
    first = track.chunks[0].lines[0][0]
    assert abs(first.start - (tr.words()[0].start - 9.0)) < 1e-3


def test_respects_max_words_and_lines():
    cfg, _, track, _ = _track("high_energy")
    st = cfg.captions.resolved_style()
    for c in track.chunks:
        n = sum(len(ln) for ln in c.lines)
        assert n <= st.max_words + 1  # +1 allows an orphan merge
        assert len(c.lines) <= st.max_lines


def test_no_chunk_crosses_sentence_end():
    _, _, track, _ = _track("minimal")
    for c in track.chunks:
        flat = [w.text for ln in c.lines for w in ln]
        for t in flat[:-1]:
            assert not t.endswith((".", "!", "?"))


def test_profanity_filter():
    cfg = load_config(overrides=["captions.profanity_filter=true", "captions.preset=minimal"])
    tr = make_transcript([(1.0, "what the fuck was that")])
    plan = plan_layout(_media(), cfg.reframing, cfg.video, cfg.captions)
    track = build_caption_track(tr.words(), 0.0, 5.0, cfg.captions, plan)
    assert "f***" in " ".join(c.text for c in track.chunks)


def test_ass_and_srt_files(tmp_path):
    cfg, plan, track, _ = _track()
    ass = write_ass(track, cfg.captions.resolved_style(), plan, tmp_path / "c.ass").read_text()
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert ass.count("Dialogue:") >= len(track.chunks)
    assert "\\pos(540," in ass
    srt = write_srt(track, tmp_path / "c.srt").read_text()
    assert srt.startswith("1\n00:00:")
    assert "-->" in srt


def test_caption_baseline_inside_band_for_split_layout():
    cfg = load_config(overrides=["reframing.mode=facecam_split", "reframing.facecam_region={x: 0, y: 0, w: 0.25, h: 0.3}",
                                 "captions.vertical_position=0.1"])
    plan = plan_layout(_media(), cfg.reframing, cfg.video, cfg.captions)
    tr = make_transcript([(1.0, "hello there chat")])
    track = build_caption_track(tr.words(), 0, 5, cfg.captions, plan)
    top = track.layout.baseline_y - track.layout.line_height_px * cfg.captions.resolved_style().max_lines
    assert top >= plan.caption_band[0]


def test_hook_title_event(tmp_path):
    cfg, plan, track, _ = _track()
    ass = write_ass(track, cfg.captions.resolved_style(), plan, tmp_path / "h.ass", hook="Car explodes {mid} chase",
                    hook_seconds=2.0).read_text()
    assert "Style: Hook," in ass
    hook = [ln for ln in ass.splitlines() if ",Hook,," in ln]
    assert len(hook) == 1 and hook[0].startswith("Dialogue: 1,0:00:00.00,0:00:02.00")
    assert "CAR EXPLODES" in hook[0] and "{mid}" not in hook[0]
    assert ",Hook,," not in write_ass(track, cfg.captions.resolved_style(), plan, tmp_path / "n.ass").read_text()
    # hook_seconds 0: the title card stays up for the whole clip
    whole = write_ass(track, cfg.captions.resolved_style(), plan, tmp_path / "w.ass", hook="Stays up",
                      hook_seconds=0, clip_duration=12.5).read_text()
    assert [ln for ln in whole.splitlines() if ",Hook,," in ln][0].startswith("Dialogue: 1,0:00:00.00,0:00:12.50")


def test_chat_overlay_lines_roll_up_bottom_left(tmp_path):
    from clipstudio.captions.chat_overlay import clean_text, schedule, write_chat_ass
    from clipstudio.core.models import ChatMessage
    cfg, plan, track, _ = _track()
    co = cfg.chat_overlay
    msgs = [ChatMessage(t=-2.0, user="@early", text="here before the clip"),
            ChatMessage(t=1.0, user="@a", text="NO WAY :face-blue-smiling:"),
            ChatMessage(t=1.05, user="@b", text="LMAO"), ChatMessage(t=1.1, user="@c", text="clip it"),
            ChatMessage(t=4.0, user="@d", text="x" * 300), ChatMessage(t=99.0, user="@late", text="after the clip")]
    assert clean_text("NO WAY :face-blue-smiling: \U0001F602") == "NO WAY"
    sch = schedule(msgs, 10.0)
    assert [round(t, 2) for t, _ in sch] == [0.0, 1.0, 1.3, 1.6, 4.0]       # a burst is spaced out, late ones dropped
    n = write_chat_ass(msgs, plan, co, 10.0, tmp_path / "chat.ass", caption_bottom=track.layout.baseline_y)
    ass = (tmp_path / "chat.ass").read_text(encoding="utf-8")
    ev = [ln for ln in ass.splitlines() if ln.startswith("Dialogue:")]
    assert n == 5 and ev and all(",Chat,," in e for e in ev)
    assert "face-blue" not in ass and "after the clip" not in ass and "early" in ass and "…" in ass
    import re
    ys = [int(y) for y in re.findall(r"\\pos\(\d+,(\d+)\)", ass)]
    xs = [int(x) for x in re.findall(r"\\pos\((\d+),", ass)]
    # under the captions, above the bottom safe margin, on the left
    assert ys and all(track.layout.baseline_y < y <= plan.caption_band[1] for y in ys) and max(xs) < plan.out_width * 0.1
    # too few messages: no box
    assert write_chat_ass(msgs[:2], plan, co, 10.0, tmp_path / "none.ass") == 0


def test_chat_overlay_skips_bots_and_commands():
    from clipstudio.captions.chat_overlay import is_noise
    assert is_noise("@Fossabot", "JOIN THE OFFICIAL DISCORD") and is_noise("@someone", "!giveaway")
    assert not is_noise("@abbot_fan", "LMAO") and not is_noise("@viewer", "no way!")
