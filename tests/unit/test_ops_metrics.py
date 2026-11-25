"""Stats logger: parsing, append-only ledger, 48 h readings, weekly summary (no network)."""
import json
from datetime import datetime, timezone

from clipstudio.ops import metrics as M

NOW = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)


def fake_fetch(pages):
    def fetch(args):
        url = args[-1]
        if "--flat-playlist" in args:
            if url in pages:
                return 0, json.dumps({"entries": [{"url": u} for u in pages[url]], "channel_follower_count": 42}), ""
            return 1, "", "ERROR: [TikTok] Unable to extract"
        vid = url.rstrip("/").rsplit("/", 1)[-1]
        if "instagram" in url:
            return 1, "", "ERROR: login required"
        return 0, json.dumps({"id": vid, "title": "tg shot through the door 😳", "view_count": 4300,
                              "like_count": 200, "comment_count": 9, "duration": 22,
                              "timestamp": NOW.timestamp() - 48 * 3600, "webpage_url": url}), ""
    return fetch


def test_snapshot_logs_videos_errors_and_followers(tmp_path):
    (tmp_path / "instagram").mkdir()
    (tmp_path / "instagram" / "posted.log").write_text(
        "2026-11-22T16:27Z | a.mp4 | t | https://www.instagram.com/gta6clipsnn/reel/DgDOeQJBIGF/\n", encoding="utf-8")
    pages = {"https://www.youtube.com/@gta6clipsnn/shorts": ["abc123"], "https://www.youtube.com/@gta6clipsnn/videos": []}
    rows = M.snapshot(tmp_path, fake_fetch(pages), NOW)
    yt = next(r for r in rows if r.platform == "youtube" and r.video_id == "abc123")
    assert yt.views == "4300" and yt.streamer == "typicalgamer" and yt.age_h == "48.0"
    assert yt.url == "https://www.youtube.com/shorts/abc123"
    assert any(r.platform == "youtube" and r.video_id == "_channel" and r.followers == "42" for r in rows)
    assert any(r.platform == "tiktok" and r.error for r in rows)                  # logged, not guessed
    ig = next(r for r in rows if r.platform == "instagram" and r.video_id == "DgDOeQJBIGF")
    assert ig.error and not ig.views
    M.snapshot(tmp_path, fake_fetch(pages), NOW)                                    # append-only
    assert len(M.read_rows(tmp_path / "ops" / "metrics.csv")) == 2 * len(rows)


def test_views_at_picks_closest_reading_in_window():
    snaps = [{"age_h": "20", "views": "5"}, {"age_h": "44", "views": "900"}, {"age_h": "70", "views": "1500"}]
    assert M.views_at(snaps, 48, 36, 72) == 900
    assert M.views_at([{"age_h": "10", "views": "5"}], 48, 36, 72) is None


def test_weekly_summary_ranks_winners_and_reports_unreadable(tmp_path):
    rows = []
    for i, (title, v) in enumerate([("tg shot through the door 😳", 4300), ("lacy lost it 😭", 281),
                                    ("ludwig too realistic 😭", 2400)]):
        rows.append({"snapshot_utc": "2026-11-26T12:00Z", "platform": "youtube", "video_id": f"v{i}", "url": "",
                     "title": title, "streamer": M.guess_streamer(title), "posted_utc": "2026-11-24T12:00Z",
                     "age_h": "48", "views": str(v), "followers": "", "error": ""})
    rows.append({"snapshot_utc": "2026-11-26T12:00Z", "platform": "tiktok", "video_id": "_page", "error": "blocked",
                 "followers": ""})
    text = M.summarize(rows, NOW)
    assert "Hit rate at 48 h: 2/3" in text
    assert text.index("tg shot through the door") < text.index("ludwig too realistic")
    assert "tiktok: blocked" in text


def test_personal_channel_never_read(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "channels.json").write_text(json.dumps(
        {"youtube": {"urls": ["https://www.youtube.com/@wettoefungus/shorts"]}, "tiktok": {"urls": []},
         "instagram": {"urls": []}}), encoding="utf-8")
    called = []
    M.snapshot(tmp_path, lambda a: called.append(a) or (1, "", "x"), NOW)
    assert not called
