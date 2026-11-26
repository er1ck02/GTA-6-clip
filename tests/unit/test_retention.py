"""Retention: which local files go, which stay (temp folders only, no media decoding)."""
import json
import os
import time
from pathlib import Path

from clipstudio import retention
from clipstudio.config import load_config

DAY = 86400


def _cfg(**rc):
    return load_config(overrides=[f"retention.{k}={v}" for k, v in rc.items()])


def _file(p: Path, size=10, age_h=10.0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    t = time.time() - age_h * 3600
    os.utime(p, (t, t))
    return p


def _clip(data: Path, proj: str, cid: str, status: str | None, score=0.5, channel="lacy", age_h=100.0) -> Path:
    cd = data / "projects" / proj / "clips" / cid
    render = _file(cd / f"{proj}_{cid}.mp4", age_h=age_h)
    _file(cd / "chat.mov", age_h=age_h)
    if status:
        (cd / "review.json").write_text(json.dumps({"status": status, "clip": str(render), "score": score,
                                                    "channel": channel, "reviewed": time.time()}), encoding="utf-8")
    _file(data / "projects" / proj / "analysis" / "audio.wav", age_h=age_h)
    return render


def _session(data: Path, name: str, moments: list[tuple[str, str, str | None]]) -> Path:
    s = data / "live" / name
    rows = []
    for mid, status, proj in moments:
        p = _file(s / "moments" / f"{mid}.mp4", age_h=24)
        rows.append({"id": mid, "path": str(p), "status": status, "project": proj})
    (s / "queue.json").parent.mkdir(parents=True, exist_ok=True)
    (s / "queue.json").write_text(json.dumps({"moments": rows}), encoding="utf-8")
    _file(s / "buffer" / "seg_000001.ts", age_h=5)
    return s


def _gone(pl, p: Path) -> bool:
    return retention._norm(p) in {a.path for a in pl.actions}


def test_settled_footage_goes_and_live_work_stays(tmp_path):
    data = tmp_path / "data"
    rej = _clip(data, "p-rej", "c1", "rejected")
    pend = _clip(data, "p-pend", "c1", "pending")
    young = _clip(data, "p-young", "c1", None, age_h=1)
    old = _clip(data, "p-old", "c1", None, age_h=100)
    s = _session(data, "lacy_1", [("m1", "skipped", None), ("m2", "done", "p-rej"), ("m3", "done", "p-pend"),
                                  ("m4", "queued", None)])
    active = _session(data, "xqc_2", [])
    pl = retention.plan(_cfg(), data, active_sessions=[active])

    assert _gone(pl, rej) and _gone(pl, old)
    assert not _gone(pl, pend) and not _gone(pl, young)
    assert _gone(pl, s / "moments" / "m1.mp4") and _gone(pl, s / "moments" / "m2.mp4")
    assert not _gone(pl, s / "moments" / "m3.mp4")          # its clip is still waiting for review
    assert not _gone(pl, s / "moments" / "m4.mp4")          # not edited yet
    assert _gone(pl, s / "buffer" / "seg_000001.ts")
    assert not _gone(pl, active / "buffer" / "seg_000001.ts")
    assert _gone(pl, data / "projects" / "p-rej" / "analysis" / "audio.wav")
    assert not _gone(pl, data / "projects" / "p-pend" / "analysis" / "audio.wav")
    assert _gone(pl, pend.parent / "chat.mov")              # intermediates go even while a clip is pending

    res = retention.apply(pl)
    assert res["freed_bytes"] == pl.bytes and not rej.exists() and pend.exists()
    assert (s / "moments" / "m4.mp4").exists()


def test_posted_clips_kept_for_a_week_then_deleted(tmp_path):
    data = tmp_path / "data"
    fresh = _clip(data, "p-a", "c1", "approved")
    stale = _clip(data, "p-b", "c1", "approved")
    for r in (fresh, stale):
        _file(data / "approved" / r.name)
    retention.mark_posted(data, fresh.name, "youtube", t=time.time() - 2 * DAY)
    retention.mark_posted(data, stale.name, "youtube", t=time.time() - 8 * DAY)
    retention.mark_posted(data, stale.name, "tiktok", t=time.time() - 1 * DAY)   # first post counts
    s = _session(data, "s", [("m1", "done", "p-a"), ("m2", "done", "p-b")])
    pl = retention.plan(_cfg(), data)
    assert _gone(pl, stale) and _gone(pl, data / "approved" / stale.name) and _gone(pl, s / "moments" / "m2.mp4")
    assert not _gone(pl, fresh) and not _gone(pl, data / "approved" / fresh.name)
    assert not _gone(pl, s / "moments" / "m1.mp4")
    assert pl.kept_backlog == []


def test_instagram_log_counts_as_posted(tmp_path):
    data = tmp_path / "data"
    r = _clip(data, "p-a", "c1", "approved")
    _file(data / "approved" / r.name)
    (data / "instagram").mkdir(parents=True)
    (data / "instagram" / "posted.log").write_text(f"2020-01-01T10:00Z | {r.name} | title | https://x\n",
                                                   encoding="utf-8")
    pl = retention.plan(_cfg(), data)
    assert _gone(pl, data / "approved" / r.name)


def test_unposted_backlog_keeps_the_best_and_favours_priority_channels(tmp_path):
    data = tmp_path / "data"
    renders = {}
    for i, (score, ch) in enumerate([(0.9, "lacy"), (0.5, "ishowspeed"), (0.6, "xqc"), (0.4, "kreek"), (0.3, "lacy")]):
        renders[i] = _clip(data, f"p-{i}", "c1", "approved", score=score, channel=ch)
        _file(data / "approved" / renders[i].name)
    s = _session(data, "s", [("m3", "done", "p-3")])
    pl = retention.plan(_cfg(backlog_days=1, clips_per_day=3), data)   # keep 3
    # 0.5 + 0.15 for IShowSpeed beats xqc's 0.6
    assert pl.kept_backlog == [renders[0].name, renders[1].name, renders[2].name]
    for i in (3, 4):
        assert _gone(pl, renders[i]) and _gone(pl, data / "approved" / renders[i].name)
    assert _gone(pl, s / "moments" / "m3.mp4")
    retention.apply(pl)
    doc = json.loads((renders[3].parent / "review.json").read_text(encoding="utf-8"))
    assert doc["status"] == "retired"
    # next sweep: retired clips stay retired, kept ones stay
    pl2 = retention.plan(_cfg(backlog_days=1, clips_per_day=3), data)
    assert pl2.kept_backlog == [renders[0].name, renders[1].name, renders[2].name] and not pl2.actions


def test_formats_specs_and_outside_paths_are_protected(tmp_path):
    data = tmp_path / "data"
    s = _session(data, "s", [("m1", "skipped", None)])
    outside = _file(tmp_path / "elsewhere" / "m9.mp4", age_h=24)
    q = json.loads((s / "queue.json").read_text(encoding="utf-8"))
    q["moments"].append({"id": "m9", "path": str(outside), "status": "skipped"})
    (s / "queue.json").write_text(json.dumps(q), encoding="utf-8")
    (data / "formats").mkdir(parents=True)
    (data / "formats" / "top5.json").write_text(json.dumps({"moments": [{"clip": str(s / "moments" / "m1.mp4")}]}),
                                               encoding="utf-8")
    pl = retention.plan(_cfg(), data)
    assert not _gone(pl, s / "moments" / "m1.mp4") and not _gone(pl, outside)


def test_edits_in_flight_and_stale_unedited_moments(tmp_path):
    data = tmp_path / "data"
    busy = data / "projects" / "p-busy"                       # analysing: no clips yet, log just written
    wav = _file(busy / "analysis" / "audio.wav", age_h=5)
    _file(busy / "project.log.jsonl", age_h=0)
    idle = data / "projects" / "p-idle"                       # finished long ago, nothing rendered
    old_log = _file(idle / "project.log.jsonl", age_h=24 * 8)
    idle_wav = _file(idle / "analysis" / "audio.wav", age_h=24 * 8)
    s = _session(data, "s", [("m1", "queued", None)])
    stale = s / "moments" / "m1.mp4"
    fresh_skip = _file(s / "moments" / "m2.mp4", age_h=0.5)
    pl = retention.plan(_cfg(), data)
    assert not _gone(pl, wav) and _gone(pl, idle_wav) and _gone(pl, old_log)
    assert not _gone(pl, stale) and not _gone(pl, fresh_skip)     # 24 h old: production may still resume it
    os.utime(stale, (time.time() - 50 * 3600,) * 2)
    assert _gone(retention.plan(_cfg(), data), stale)


def test_cli_dry_run_touches_nothing(tmp_path, capsys):
    from clipstudio.cli import main
    data = tmp_path / "data"
    s = _session(data, "s", [("m1", "skipped", None)])
    man = tmp_path / "manifest.tsv"
    assert main(["retention", "--set", f"paths.data_dir={data}", "--manifest", str(man)]) == 0
    assert (s / "moments" / "m1.mp4").exists() and "m1.mp4" in man.read_text(encoding="utf-8")
    assert main(["retention", "posted", "x.mp4", "--platform", "youtube", "--set", f"paths.data_dir={data}"]) == 0
    assert "x.mp4" in retention.first_posted(data)
    assert main(["retention", "--apply", "--set", f"paths.data_dir={data}"]) == 0
    assert not (s / "moments" / "m1.mp4").exists()
