"""Production agent: lineup planning, stand-ins, safe/quick stop, backlog, API and launch settings (no network)."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from clipstudio.config import load_config
from clipstudio.live.session import LiveSession, Moment
from clipstudio.production import agent
from clipstudio.production import lineup as L
from clipstudio.production.manager import ProductionManager


# ------------------------------------------------------------------ fakes
class FakeSession:
    def __init__(self, channel, url, out):
        self.channel, self.rec = channel, SimpleNamespace(url=url, file=None)
        self.out, self.moments, self.stopped_reason, self.running = out, [], "", True
        self._abandon = SimpleNamespace(is_set=lambda: self.abandoned)
        self.abandoned = False

    def stop(self):
        self.running, self.stopped_reason = False, "stopped from the workstation"

    def abandon(self):
        self.abandoned = True
        self.stop()


class FakeWatch:
    def __init__(self, key, session):
        self.key, self.session, self.alive, self.error = key, session, True, None

    @property
    def running(self):
        return self.alive


class FakeLive:
    def __init__(self, tmp):
        self.watches, self.tmp, self.started = {}, tmp, []

    def start(self, url, file, channel, chat, duration, realtime=True):
        key = channel
        w = FakeWatch(key, FakeSession(channel, url, self.tmp / "live" / f"{channel}_20260101_000000"))
        self.watches[key] = w
        self.started.append(channel)
        return {"id": key}

    def stop(self, key=None):
        for k, w in self.watches.items():
            if key is None or k == key:
                w.session.stop()


def checker(live: set, titles: dict | None = None):
    titles = titles or {}

    def check(url):
        ch = L.channel_of(url)
        if url.startswith("https://www.twitch.tv/") and ch in live:
            return L.LiveCheck(True, titles.get(ch, "GTA 6 RP day 3"), url)
        return L.LiveCheck(False, url=url, error="not currently live")
    return check


def session_dir(root: Path, channel: str, stamp: str, moments: list[dict], url: str | None = None) -> Path:
    d = root / "live" / f"{channel}_{stamp}"
    (d / "moments").mkdir(parents=True)
    for m in moments:
        if m.get("path") == "make":
            p = d / "moments" / f"{m['id']}.mp4"
            p.write_bytes(b"x")
            m["path"] = str(p)
    (d / "queue.json").write_text(json.dumps({"channel": channel, "moments": moments}))
    if url:
        (d / "source.json").write_text(json.dumps({"url": url, "channel": channel}))
    return d


@pytest.fixture
def cfg(tmp_path):
    # no network in tests: no Twitch category / YouTube search
    return load_config(overrides=[f"paths.data_dir={tmp_path}", "production.recheck_minutes=1",
                                  "production.discover_twitch_category=''", "production.discover_youtube_search=''"])


def mgr(cfg, live, check, **kw):
    return ProductionManager(cfg, live, check=check, autostart_loop=False, **kw)


# ------------------------------------------------------------------ lineup
def test_resume_url_turns_broadcasts_into_channel_pages():
    assert L.resume_url("https://www.youtube.com/watch?v=7aet0w1rrQE", "ludwig") == "https://www.youtube.com/@ludwig/live"
    assert L.resume_url("https://twitch.tv/KaiCenat?x=1", None) == "https://www.twitch.tv/kaicenat"
    assert L.resume_url("https://kick.com/xqc", "xqc") == "https://kick.com/xqc"
    assert L.resume_url("https://www.youtube.com/@Lacy/live", None) == "https://www.youtube.com/@Lacy/live"
    assert L.resume_url(None, "x") is None


def test_history_ranks_allowlisted_then_most_clips(tmp_path):
    session_dir(tmp_path, "xqc", "20261121_192226", [{"id": "a", "status": "done"}, {"id": "b", "status": "done"}])
    session_dir(tmp_path, "lacy", "20261121_192240", [{"id": "a", "status": "skipped"}])
    session_dir(tmp_path, "ludwig", "20261121_164606", [{"id": "a", "status": "done"}],
                url="https://www.youtube.com/watch?v=7aet0w1rrQE")
    session_dir(tmp_path, "stream", "20261121_100000", [])
    h = L.scan_history(tmp_path / "live")
    assert set(h) == {"xqc", "lacy", "ludwig"}
    assert h["ludwig"].urls == ["https://www.youtube.com/@ludwig/live"]
    assert [c.channel for c in L.rank(h)] == ["xqc", "ludwig", "lacy"]
    assert [c.channel for c in L.rank(h, ["ludwig"])][0] == "ludwig"


def test_plan_watches_live_and_substitutes_offline_with_a_gta_streamer():
    ranked = [L.PastChannel("xqc", clips=9), L.PastChannel("lacy", clips=5), L.PastChannel("kaicenat", clips=1)]
    lineup = [{"channel": "ludwig", "url": "https://www.youtube.com/@ludwig/live"},
              {"channel": "theburntpeanut", "url": "https://www.twitch.tv/theburntpeanut"}]
    check = checker({"theburntpeanut", "xqc", "lacy"}, {"xqc": "Just Chatting", "lacy": "GTA VI first day"})
    plans = L.plan_lineup(lineup, ranked, check, keywords=["gta"])
    assert [(p.wanted, p.action, p.channel) for p in plans] == [
        ("ludwig", "substitute", "lacy"), ("theburntpeanut", "watch", "theburntpeanut")]
    assert "ludwig is offline; watching lacy instead" in plans[0].note


def test_plan_waits_when_no_past_streamer_is_live_and_keeps_busy_ones():
    plans = L.plan_lineup([{"channel": "a"}, {"channel": "b"}], [L.PastChannel("c")], checker(set()),
                          busy={"a"}, keywords=["gta"])
    assert [p.action for p in plans] == ["keep", "wait"]
    assert "checking again later" in plans[1].note


# ------------------------------------------------------------------ manager: start / follow
def test_first_start_uses_top_past_streamers_and_resumes_saved_lineup(cfg, tmp_path):
    session_dir(tmp_path, "xqc", "20261121_192226", [{"id": "a", "status": "done"}] * 3)
    session_dir(tmp_path, "lacy", "20261121_192240", [{"id": "a", "status": "done"}])
    live = FakeLive(tmp_path)
    m = mgr(cfg, live, checker({"xqc", "lacy"}))
    res = m.start()
    assert res["mode"] == "running" and sorted(live.started) == ["lacy", "xqc"]
    assert [e["channel"] for e in m.state["lineup"]] == ["xqc", "lacy"]
    saved = json.loads((tmp_path / "production" / "state.json").read_text())
    assert saved["mode"] == "running" and len(saved["slots"]) == 2


def test_offline_stream_gets_a_stand_in_and_the_original_takes_it_back(cfg, tmp_path):
    cfg.production.target_streams = 0  # just the lineup: the stand-in stops when the original returns
    session_dir(tmp_path, "lacy", "20261121_192240", [{"id": "a", "status": "done"}])
    live = FakeLive(tmp_path)
    online = {"ludwig"}
    m = mgr(cfg, live, lambda u: checker(online)(u))
    m.start(["https://www.twitch.tv/ludwig"])
    assert live.started == ["ludwig"]
    # ludwig's stream ends; lacy (clipped before, playing GTA) is live
    online.clear()
    online.add("lacy")
    live.watches["ludwig"].session.stopped_reason = "stream ended"
    live.watches["ludwig"].alive = False
    m._follow()
    assert live.started[-1] == "lacy"
    assert m.slots[0]["action"] == "substitute" and m.slots[0]["wanted"] == "ludwig"
    # ludwig is back: the stand-in is stopped and ludwig is watched again
    online.add("ludwig")
    m._next_recheck = 0
    m._follow()
    assert live.watches["lacy"].session.running is False
    assert live.started[-1] == "ludwig" and m.slots[0]["action"] == "watch"


def test_target_keeps_n_streams_with_twitch_stand_ins_and_swaps_back(cfg, tmp_path):
    cfg.production.target_streams = 3
    cfg.production.discover_channels = ["bigone", "mid", "notgta"]
    session_dir(tmp_path, "lacy", "20261121_192240", [{"id": "a", "status": "done"}])
    live = FakeLive(tmp_path)
    online = {"ludwig", "lacy", "bigone", "mid", "notgta"}
    viewers = {"bigone": 50000, "mid": 900, "notgta": 99999}
    titles = {"notgta": "Just Chatting", "lacy": "GTA 6 story"}

    def check(u):
        r = checker(online, titles)(u)
        r.viewers = viewers.get(L.channel_of(u), 0)
        return r
    m = mgr(cfg, live, check)
    m.start(["https://www.twitch.tv/ludwig", "https://www.twitch.tv/kai"])  # kai is offline
    # ludwig + lacy (past streamer) for kai + the most-watched GTA 6 stream on Twitch; not the Just Chatting one
    assert sorted(live.started) == ["bigone", "lacy", "ludwig"]
    acts = {s["channel"]: s["action"] for s in m.slots}
    assert acts == {"ludwig": "watch", "lacy": "substitute", "bigone": "fill"}
    assert "Twitch" in next(s["note"] for s in m.slots if s["channel"] == "bigone")
    assert {"lacy", "bigone"} <= set(cfg.upload.allow_channels)  # stand-ins post like the lineup (credited)
    assert mgr(cfg, FakeLive(tmp_path), check).state["posted_stand_ins"] == ["lacy", "bigone"]
    # bigone ends: the next best GTA stream takes its place, still 3 recording
    online.discard("bigone")
    live.watches["bigone"].session.stopped_reason = "stream ended"
    live.watches["bigone"].alive = False
    m._follow()
    assert live.started[-1] == "mid" and len(m._recording_slots()) == 3
    # kai comes back: kai is watched, lacy stays on as an extra, the newest extra (mid) makes room
    online.add("kai")
    m._next_recheck = 0
    m._follow()
    on = {s["channel"] for s in m._recording_slots()}
    assert on == {"ludwig", "kai", "lacy"} and live.watches["mid"].session.running is False
    # nothing changes on a quiet recheck: no stream is restarted or added twice
    n = len(live.started)
    m._next_recheck = 0
    m._follow()
    assert len(live.started) == n and len(m._recording_slots()) == 3


def test_probe_reads_twitch_titles_from_the_description():
    # yt-dlp on a Twitch live page: title "Lacy (live)", real title in description, viewers in view_count
    r = L.parse_probe('is_live|12345||"GTA 6 | day 4 \\"heists\\""|Lacy (live)', "https://www.twitch.tv/lacy")
    assert r.live and r.title == 'GTA 6 | day 4 "heists"' and r.viewers == 12345
    r = L.parse_probe('is_live|50000|Gaming|"long\\ntext"|DAY 4 GTA VI MARATHON', "https://www.youtube.com/@ludwig/live")
    assert r.title == "DAY 4 GTA VI MARATHON" and r.viewers == 50000 and r.category == ""
    assert L.parse_probe("is_live|||NA|Z (live)", "https://www.twitch.tv/z").title == "Z (live)"
    assert L.parse_probe("not_live|||NA|x").live is False


def test_youtube_live_search_feeds_stand_ins():
    r = L.parse_search_line("is_live|4321|@BigGTA|GTA 6 heists LIVE | day 3")
    assert r.live and r.url == "https://www.youtube.com/@BigGTA/live" and r.viewers == 4321
    assert L.channel_of(r.url) == "biggta" and r.title == "GTA 6 heists LIVE | day 3"
    assert L.parse_search_line("was_live|10|@x|GTA 6") is None and L.parse_search_line("is_live|10||GTA 6") is None
    yt = [L.LiveCheck(True, "GTA 6 story", "https://www.youtube.com/@yt/live", viewers=900),
          L.LiveCheck(True, "Minecraft", "https://www.youtube.com/@mc/live", viewers=99999)]
    check = lambda u: L.LiveCheck(True, "GTA 6 rp", u, viewers=500) if "tw1" in u else L.LiveCheck(False, url=u)  # noqa: E731
    got = L.discover_live(["tw1", "tw2"], check, ["gta 6"], search=lambda: yt)
    assert [L.channel_of(g.url) for g in got] == ["yt", "tw1"]  # most viewers first, non-GTA dropped


def test_youtube_search_reads_emoji_titles_as_utf8(monkeypatch):
    import subprocess as sp

    out = "is_live|98404|@IShowSpeed|\U0001f534EXPLORING VICE CITY - GTA 6 STREAM\n".encode("utf-8")

    def fake_run(cmd, **kw):  # decode the way subprocess would with the kwargs given (Windows default = cp1252)
        enc = kw.get("encoding") or "cp1252"
        return sp.CompletedProcess(cmd, 0, out.decode(enc, kw.get("errors") or "strict"), "")
    monkeypatch.setattr(L.subprocess, "run", fake_run)
    monkeypatch.setattr("clipstudio.live.buffer._ytdlp_cmd", lambda: ["yt-dlp"])
    r = L.youtube_live("GTA 6")
    assert len(r) == 1 and r[0].viewers == 98404 and r[0].title.startswith("\U0001f534EXPLORING")


def test_twitch_category_is_picked_first_and_needs_credentials(tmp_path, monkeypatch):
    from clipstudio.production import twitch

    monkeypatch.setenv("CLIPSTUDIO_HOME", str(tmp_path))
    twitch._token.clear()
    twitch._games.clear()
    assert twitch.top_streams("Grand Theft Auto VI") == []  # no ~/.clipstudio/twitch.json: skipped
    (tmp_path / "twitch.json").write_text('{"client_id": "cid", "client_secret": "sec"}')
    calls = []

    def fake(url, *, data=None, headers=None, timeout=20):
        calls.append(url)
        if "oauth2/token" in url:
            return {"access_token": "tok", "expires_in": 3600}
        if "/games?" in url:
            assert headers["Client-Id"] == "cid" and headers["Authorization"] == "Bearer tok"
            return {"data": [{"id": "999"}]}
        assert "game_id=999" in url
        return {"data": [{"user_login": "Small", "title": "chill", "viewer_count": 50},
                         {"user_login": "Big", "title": "sleep stream zzz", "viewer_count": 90000},
                         {"user_login": "Mid", "title": "heists", "viewer_count": 3000}]}
    monkeypatch.setattr(twitch, "_call", fake)
    top = twitch.top_streams("Grand Theft Auto VI")
    assert [L.channel_of(r.url) for r in top] == ["big", "mid", "small"]
    cfg = load_config()
    idle = lambda r: L.idle_reason(r, cfg.production.idle_keywords, [])  # noqa: E731
    yt = [L.LiveCheck(True, "GTA 6", "https://www.youtube.com/@yt/live", viewers=10**6)]
    got = L.discover_live([], lambda u: L.LiveCheck(False, url=u), ["gta 6"], idle=idle,
                          search=lambda: yt, category=lambda: top)
    # category first (no keyword needed, sleep stream skipped), YouTube after
    assert [L.channel_of(g.url) for g in got] == ["mid", "small", "yt"]
    twitch.top_streams("Grand Theft Auto VI")
    assert sum("oauth2" in c for c in calls) == 1 and sum("/games?" in c for c in calls) == 1  # cached


def test_sleep_and_non_gta_streams_count_as_offline(cfg, tmp_path):
    words, games = cfg.production.idle_keywords, cfg.production.substitute_keywords
    assert L.idle_reason(L.LiveCheck(True, "SLEEP STREAM zzz"), words, games) == "sleep stream"
    assert L.idle_reason(L.LiveCheck(True, "GTA 6 no sleep till 100%"), words, games) == ""
    assert L.idle_reason(L.LiveCheck(True, "hi", category="Just Chatting"), words, games) == "just chatting stream"
    assert "not GTA" in L.idle_reason(L.LiveCheck(True, "chill", category="Minecraft"), words, games)
    assert L.idle_reason(L.LiveCheck(True, "chill", category="Grand Theft Auto VI"), words, games) == ""

    cfg.production.target_streams = 2
    cfg.production.discover_channels = ["big"]
    live = FakeLive(tmp_path)
    online = {"lacy", "big"}
    titles = {"lacy": "GTA 6 day 4", "big": "GTA 6 heists"}
    m = mgr(cfg, live, lambda u: checker(online, titles)(u))
    m.start(["https://www.twitch.tv/lacy"])
    assert sorted(live.started) == ["big", "lacy"]
    # lacy falls asleep on stream: on the next check she is swapped out; the slot waits for her
    titles["lacy"] = "sleep stream zzz"
    m._next_recheck = 0
    m._follow()
    assert live.watches["lacy"].session.running is False
    lacy = next(s for s in m.slots if s["wanted"] == "lacy")
    assert lacy["action"] == "wait" and "sleep" in lacy["note"]
    assert m.state["lineup"][0]["channel"] == "lacy"  # still in the lineup, not dropped like a hand stop
    # she wakes up and plays again: recorded again
    titles["lacy"] = "GTA 6 morning"
    m._next_recheck = 0
    m._follow()
    assert live.started[-1] == "lacy"
    assert next(s for s in m.slots if s["wanted"] == "lacy")["action"] == "watch"


def test_a_still_picture_is_a_sleep_stream_even_with_a_gta_title(cfg, tmp_path, monkeypatch):
    from clipstudio.production import activity

    # the measure itself, on real ffmpeg frames is covered by activity.motion; here: fake frame reads
    still = lambda p: np.zeros((3, 36, 64), np.float32)  # noqa: E731
    moving = lambda p: np.stack([np.zeros((36, 64)), np.full((36, 64), 20.0), np.zeros((36, 64))])  # noqa: E731
    assert activity.motion([Path("a"), Path("b")], read=still) == 0.0
    assert activity.motion([Path("a")], read=moving) == 20.0
    assert activity.motion([], read=still) is None

    cfg.production.target_streams = 2
    cfg.production.discover_channels = ["big"]
    live = FakeLive(tmp_path)
    online = {"lacy", "big"}
    m = mgr(cfg, live, lambda u: checker(online, {"lacy": "GTA 6 MARATHON 24/7"})(u))
    m.start(["https://www.twitch.tv/lacy"])
    seg = tmp_path / "seg.ts"
    seg.write_bytes(b"x")
    for w in live.watches.values():
        w.session.ring = SimpleNamespace(segments=[SimpleNamespace(path=seg, end=10.0)])
    score = {"lacy": 0.2, "big": 9.0}
    monkeypatch.setattr(activity, "motion", lambda paths, **kw: score[current[0]])
    current = [""]
    orig_still = m._still

    def still_for(s):
        current[0] = s["channel"]
        return orig_still(s)
    monkeypatch.setattr(m, "_still", still_for)
    m._next_recheck = 0
    m._follow()  # 1st still reading: not yet
    assert live.watches["lacy"].session.running is True
    m._next_recheck = 0
    m._follow()  # 2nd in a row: asleep, swapped out, not retried during the cooldown
    assert live.watches["lacy"].session.running is False
    lacy = next(s for s in m.slots if s["wanted"] == "lacy")
    assert lacy["action"] == "wait" and "nothing moving" in lacy["note"]
    m._next_recheck = 0
    m._follow()
    assert live.started.count("lacy") == 1 and live.watches["big"].session.running is True


def test_streams_added_or_stopped_by_hand_change_the_lineup(cfg, tmp_path):
    live = FakeLive(tmp_path)
    m = mgr(cfg, live, checker({"a"}))
    m.start(["https://www.twitch.tv/a"])
    live.start("https://www.twitch.tv/b", None, "b", None, 0)  # pasted in the workstation
    m._follow()
    assert [e["channel"] for e in m.state["lineup"]] == ["a", "b"]
    live.stop("a")  # stopped by hand
    live.watches["a"].alive = False
    m._follow()
    assert [e["channel"] for e in m.state["lineup"]] == ["b"]


# ------------------------------------------------------------------ manager: stop
def test_safe_stop_waits_for_edits_and_counts_down(cfg, tmp_path):
    live = FakeLive(tmp_path)
    m = mgr(cfg, live, checker({"a"}))
    m.start(["https://www.twitch.tv/a"])
    s = live.watches["a"].session
    s.moments = [Moment("m1", {}, (0, 1), status="processing"), Moment("m2", {}, (0, 1), status="queued"),
                 Moment("m3", {}, (0, 1), status="done")]
    res = m.stop("safe")
    assert res["mode"] == "draining" and not res["safe"]
    assert res["eta_seconds"] == 2 * 25  # 1.5 edits rounded up to 2 slots-worth at 25 s
    assert res["message"].startswith("please wait about 1 min")
    assert s.running is False  # recording stopped at once
    for x in s.moments:
        x.status = "done"
    live.watches["a"].alive = False
    m._check_drained()
    st = m.status()
    assert st["mode"] == "stopped" and st["safe"] and st["message"].startswith("safe to use your PC")
    assert m.state["last_lineup"][0]["channel"] == "a"


def test_quick_stop_shelves_waiting_moments_and_the_next_start_edits_them(cfg, tmp_path, monkeypatch):
    s = LiveSession(cfg, None, None, tmp_path / "live" / "a_20261122_010000", channel="a")
    s.out.mkdir(parents=True)
    clip = s.out / "m1.mp4"
    clip.write_bytes(b"x")
    m1 = Moment("m1", {"t": 5}, (0, 10), saved=(0, 10), path=str(clip), channel="a", status="saved")
    s.moments = [m1]
    s.jobs.put(m1)
    s.jobs.put(None)
    s.abandon()
    s._worker()
    assert m1.status == "saved" and "next production start" in m1.upload_note
    doc = json.loads((s.out / "queue.json").read_text())
    assert doc["moments"][0]["status"] == "saved"

    edited = []

    def fake_edit(self, m):
        edited.append((self.channel, m.id))
        m.status, m.clip = "done", "clip.mp4"
    monkeypatch.setattr(LiveSession, "_edit", fake_edit)
    m = mgr(cfg, FakeLive(tmp_path), checker(set()))
    assert [x["id"] for _d, x in m.backlog()] == ["m1"]
    m.start(["https://www.twitch.tv/zzz"])
    m.backlog_thread.join(5)
    assert edited == [("a", "m1")]
    assert json.loads((s.out / "queue.json").read_text())["moments"][0]["status"] == "done"
    assert m.backlog() == []


def test_quick_stop_saves_queued_vod_jobs_for_next_start(cfg, tmp_path):
    import queue as q

    jobs = SimpleNamespace(q=q.Queue(), submitted=[])
    jobs.list = lambda: []
    jobs.submit = lambda kind, pid, clip, fn: jobs.submitted.append(pid)
    job = {"kind": "analyze", "project": "p1", "status": "queued"}
    jobs.q.put((job, lambda: None))
    live = FakeLive(tmp_path)
    m = mgr(cfg, live, checker({"a"}), jobs=jobs, analyze=lambda pid: None)
    m.start(["https://www.twitch.tv/a"])
    m.stop("now")
    assert m.state["backlog_vods"] == ["p1"] and jobs.q.empty()
    live.watches["a"].alive = False
    m._check_drained()
    assert m.mode == "stopped" and m.state["last_stop"]["saved_for_next_start"] == 1
    m.start()
    assert jobs.submitted == ["p1"] and m.state["backlog_vods"] == []


def test_crash_marks_interrupted_and_stop_when_idle_is_instant(cfg, tmp_path):
    (tmp_path / "production").mkdir()
    (tmp_path / "production" / "state.json").write_text(json.dumps({"mode": "running", "lineup": [], "known": {}}))
    m = mgr(cfg, FakeLive(tmp_path), checker(set()))
    assert m.mode == "interrupted"
    assert m.stop()["safe"] is True


def test_edit_timings_feed_the_estimate(cfg, tmp_path):
    from clipstudio.core import events

    m = mgr(cfg, FakeLive(tmp_path), checker(set()))
    t = time.time()
    m._on_event({"kind": "live_moment", "channel": "a", "moment": "m1", "status": "processing", "ts": t})
    m._on_event({"kind": "live_moment", "channel": "a", "moment": "m1", "status": "done", "ts": t + 40})
    assert m.avg_edit_seconds() == 40
    m.close()
    assert events  # imported for the subscription side effect


# ------------------------------------------------------------------ API
def test_production_api_start_status_stop(cfg, tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from clipstudio.production import lineup
    from clipstudio.server.app import create_app

    monkeypatch.setattr(lineup, "probe", checker(set()))
    c = TestClient(create_app(cfg))
    assert c.get("/api/production").json()["mode"] == "idle"
    r = c.post("/api/production/start", json={"channels": ["https://www.twitch.tv/nobody"]}).json()
    assert r["mode"] == "running" and r["plan"][0]["action"] == "wait"
    r = c.post("/api/production/stop", json={}).json()
    assert r["mode"] == "stopped" and r["safe"]
    assert c.post("/api/production/stop", json={"mode": "later"}).status_code == 400


# ------------------------------------------------------------------ launch settings
def test_launch_settings_are_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("CLIPSTUDIO_HOME", str(tmp_path / "home"))
    a = SimpleNamespace(config=[], profile="quality", overrides=[f"paths.data_dir={tmp_path / 'dd'}"], port=None)
    launch = agent.resolve_launch(a, persist=True)
    assert launch["port"] == 8765 and "--profile" in launch["server_args"]
    again = agent.resolve_launch(SimpleNamespace(config=[], profile=None, overrides=[], port=None))
    assert again == launch
    cmd = agent.server_cmd(launch)
    assert cmd[1:5] == ["-m", "app.cli", "ui", "--port"]
    cfg = agent.config_for(launch)
    assert Path(cfg.paths.data_dir) == tmp_path / "dd" and cfg.processing.profile == "quality"


def test_repair_path_adds_the_venv_and_is_idempotent(monkeypatch):
    import os
    import sys

    monkeypatch.setenv("PATH", "/usr/bin")
    added = agent.repair_path()
    assert str(Path(sys.executable).parent) in os.environ["PATH"].split(os.pathsep)
    assert agent.repair_path() == []  # a second call adds nothing
    assert all(d in os.environ["PATH"].split(os.pathsep) for d in added)


def test_failed_moments_are_retried_once(cfg, tmp_path):
    session_dir(tmp_path, "a", "20261122_050000", [
        {"id": "m1", "status": "failed", "path": "make", "created": time.time()},
        {"id": "m2", "status": "failed", "path": "make", "created": time.time(), "attempts": 2},
        {"id": "m3", "status": "done", "path": "make", "created": time.time(), "clip": "x.mp4"}])
    m = mgr(cfg, FakeLive(tmp_path), checker(set()))
    assert [x["id"] for _d, x in m.backlog()] == ["m1"]
