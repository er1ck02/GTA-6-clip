"""API tests against a real analysed project (uses the pipeline end to end)."""
import time

import pytest

from clipstudio.config import load_config

from tests.conftest import whisper_model

pytestmark = pytest.mark.integration
fastapi = pytest.importorskip("fastapi")


@pytest.mark.skipif(whisper_model() is None, reason="set CLIPSTUDIO_WHISPER_MODEL")
def test_api_drives_the_pipeline(tmp_path, sample_short):
    from fastapi.testclient import TestClient

    from clipstudio.server.app import create_app

    cfg = load_config(overrides=[f"paths.data_dir={tmp_path / 'data'}", f"transcription.model={whisper_model()}",
                                 "highlights.minimum_duration=8", "highlights.maximum_duration=20",
                                 "highlights.target_duration=12", "video.fps=30", "export.preset=veryfast"])
    c = TestClient(create_app(cfg))

    def wait(job_id):
        for _ in range(240):
            j = next(x for x in c.get("/api/jobs").json() if x["id"] == job_id)
            if j["status"] in ("done", "failed"):
                return j
            time.sleep(0.5)
        raise AssertionError("job timed out")

    r = c.post("/api/projects", json={"input_path": str(sample_short)}).json()
    pid = r["project"]
    assert wait(r["job"]["id"])["status"] == "done"
    cands = c.get(f"/api/projects/{pid}/candidates").json()["candidates"]
    cid = cands[0]["id"]

    # crop edit changes the plan the preview returns
    before = c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()["plan"]["keyframes"][0]["gameplay"]["x"]
    c.patch(f"/api/projects/{pid}/clips/{cid}", json={"crop_center_x": 0.2})
    after = c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()
    assert after["plan"]["keyframes"][0]["gameplay"]["x"] < before

    # caption edit is saved and validated
    track = after["captions"]
    track["chunks"][0]["lines"][0][0]["text"] = "EDITED"
    assert c.put(f"/api/projects/{pid}/clips/{cid}/captions", json=track).json()["edited"] is True
    bad = dict(track, chunks=[dict(track["chunks"][0], start=-5)])
    assert c.put(f"/api/projects/{pid}/clips/{cid}/captions", json=bad).status_code == 400
    assert c.patch(f"/api/projects/{pid}/clips/{cid}", json={"bogus": 1}).status_code == 400

    # render through the job runner; the edited caption reaches the burned-in ASS
    j = c.post(f"/api/projects/{pid}/clips/{cid}/render").json()
    assert wait(j["id"])["status"] == "done"
    assert c.get(f"/api/projects/{pid}/clips/{cid}/qc").json()["passed"] is True
    v = c.get(f"/api/projects/{pid}/clips/{cid}/video")
    assert v.status_code == 200 and v.content[4:8] == b"ftyp"
    ass = (tmp_path / "data" / "projects" / pid / "clips" / cid / "captions.ass").read_text()
    assert "EDITED" in ass

    assert c.get(f"/api/projects/{pid}/frame?t=3&w=200").headers["content-type"] == "image/jpeg"
    assert len(c.get(f"/api/projects/{pid}/waveform?bins=40").json()["bins"]) == 40
    assert c.get("/api/projects/nope").status_code == 404

    # validation and explicit reset of a field
    assert c.patch(f"/api/projects/{pid}/clips/{cid}", json={"layout": "sideways"}).status_code == 400
    assert c.patch(f"/api/projects/{pid}/clips/{cid}", json={"crop_center_x": 1.5}).status_code == 400
    c.patch(f"/api/projects/{pid}/clips/{cid}", json={"crop_center_x": None})
    assert c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()["edit"].get("crop_center_x") is None

    # trimming the start keeps hand-edited captions on the same source moment
    pv = c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()
    first = pv["captions"]["chunks"][0]["start"]
    c.patch(f"/api/projects/{pid}/clips/{cid}", json={"start": pv["candidate"]["start"] - 1.0})
    pv2 = c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()
    assert abs(pv2["captions"]["chunks"][0]["start"] - (first + 1.0)) < 0.01

    # cuts: validated, reflected in the candidate, removed from the render; batch export writes platform copies
    c.delete(f"/api/projects/{pid}/clips/{cid}/captions")
    cand = c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()["candidate"]
    a = cand["start"] + 2.0
    assert c.patch(f"/api/projects/{pid}/clips/{cid}", json={"cuts": [[cand["start"] + 0.3, cand["end"] + 50]]}).status_code == 400
    r = c.patch(f"/api/projects/{pid}/clips/{cid}", json={"cuts": [[a, a + 1.5]], "status": "accepted"}).json()
    assert r["cuts"] == [[round(a, 3), round(a + 1.5, 3)]]
    pv3 = c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()
    assert "punches" in pv3["effects"]
    assert pv3["captions"]["chunks"][-1]["end"] <= cand["end"] - cand["start"] - 1.5 + 0.01
    assert c.post(f"/api/projects/{pid}/render", json={"platforms": ["myspace"]}).status_code == 400
    jobs_ = c.post(f"/api/projects/{pid}/render", json={"platforms": ["tiktok"]}).json()["jobs"]
    assert [j["clip"] for j in jobs_] == [cid]
    assert wait(jobs_[0]["id"])["status"] == "done"
    qc = c.get(f"/api/projects/{pid}/clips/{cid}/qc").json()
    dur = next(ch for ch in qc["checks"] if ch["name"] == "duration")
    assert qc["passed"] is True, qc
    proj = c.get(f"/api/projects/{pid}").json()
    assert proj["clips"][cid]["platforms"][0]["platform"] == "tiktok"
    v = c.get(f"/api/projects/{pid}/clips/{cid}/platforms/tiktok")
    assert v.status_code == 200 and v.content[4:8] == b"ftyp"
    assert c.get(f"/api/projects/{pid}/clips/{cid}/platforms/tiktok?meta=true").json()["platform"] == "TikTok"
    assert dur["status"] == "pass"
    c.patch(f"/api/projects/{pid}/clips/{cid}", json={"cuts": None})
    assert c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()["candidate"]["cuts"] == []

    # split at a point inside the clip: the clip keeps the first part, a new clip gets the rest
    cand = c.get(f"/api/projects/{pid}/clips/{cid}/preview").json()["candidate"]
    assert c.post(f"/api/projects/{pid}/clips/{cid}/split", json={"at": cand["start"] + 0.2}).status_code == 400
    mid = round((cand["start"] + cand["end"]) / 2, 2)
    sp = c.post(f"/api/projects/{pid}/clips/{cid}/split", json={"at": mid}).json()
    assert sp["first"]["end"] == mid and sp["second"]["start"] == mid and sp["second"]["end"] == cand["end"]
    ids = [x["id"] for x in c.get(f"/api/projects/{pid}/candidates").json()["candidates"]]
    assert sp["second"]["id"] in ids
    assert c.get(f"/api/projects/{pid}/clips/{sp['second']['id']}/preview").status_code == 200

    # live event stream replays stage history
    with c.stream("GET", "/api/events") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:"):
                assert '"kind"' in line
                break


def test_live_panel_api(tmp_path):
    """The workstation's live panel: start a dry run from a file, watch the buffer and the moment arrive."""
    import json
    import subprocess
    import time

    from fastapi.testclient import TestClient

    from clipstudio.server.app import create_app

    src = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=90",
                    "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=90", "-filter_complex",
                    "[1:a]volume='if(between(t,60,63),0.9,0.05)':eval=frame[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac", str(src)], check=True)
    chat = [{"time_in_seconds": float(t), "message": "hi", "author": {"name": "u"}} for t in range(0, 90, 3)]
    (tmp_path / "s.chat.json").write_text(json.dumps(chat))
    cfg = load_config(overrides=[f"paths.data_dir={tmp_path / 'data'}", "live.auto_process=false",
                                 "upload.allow_channels=[tester]"])
    c = TestClient(create_app(cfg))
    assert c.get("/api/live").json()["active"] is False
    r = c.post("/api/live/start", json={"file": str(src), "channel": "tester", "chat": str(tmp_path / "s.chat.json"),
                                        "fast": True})
    assert r.status_code == 200, r.text
    key = r.json()["id"]
    dup = c.post("/api/live/start", json={"file": str(src)})  # the same source twice is refused while it runs
    assert dup.status_code == 400 or not c.get(f"/api/live?id={key}").json()["running"]
    deadline = time.time() + 60
    while time.time() < deadline:
        s = c.get(f"/api/live?id={key}").json()
        if s["active"] and not s["running"] and s["started_at"]:
            break
        time.sleep(0.5)
    assert s["allowed"] and s["chat_source"] == "replay" and s["chat_count"] >= 25
    assert s["signals"] and s["head"] > 80
    assert len(s["moments"]) == 1 and s["moments"][0]["status"] == "saved"
    assert c.post("/api/live/start", json={}).status_code == 400
    sysinfo = c.get("/api/system").json()
    assert {"live_record", "live_chat", "live_trigger", "upload"} <= {t["stage"] for t in sysinfo["tools"]}


def test_live_many_streams(tmp_path):
    """Several streams watched at once: each gets its own session and tile, stop/remove work per stream."""
    import subprocess
    import time

    from fastapi.testclient import TestClient

    from clipstudio.server.app import create_app

    srcs = []
    for i, f in enumerate((300, 500, 700)):
        src = tmp_path / f"s{i}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=40",
                        "-f", "lavfi", "-i", f"sine=frequency={f}:sample_rate=48000:duration=40",
                        "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-c:a", "aac", str(src)], check=True)
        srcs.append(src)
    cfg = load_config(overrides=[f"paths.data_dir={tmp_path / 'data'}", "live.auto_process=false", "live.max_streams=2"])
    c = TestClient(create_app(cfg))
    a = c.post("/api/live/start", json={"file": str(srcs[0]), "channel": "alpha"}).json()
    b = c.post("/api/live/start", json={"file": str(srcs[1]), "channel": "beta"}).json()
    assert {a["id"], b["id"]} == {"alpha", "beta"}
    over = c.post("/api/live/start", json={"file": str(srcs[2]), "channel": "gamma"})
    assert over.status_code == 400 and "max_streams" in over.text
    deadline = time.time() + 30
    while time.time() < deadline:
        s = c.get("/api/live").json()
        if all(x["head"] > 6 for x in s["sessions"]):
            break
        time.sleep(0.5)
    assert {x["id"] for x in s["sessions"]} == {"alpha", "beta"} and all(x["running"] for x in s["sessions"])
    assert c.get("/api/live?id=beta").json()["channel"] == "beta"
    assert c.get("/api/live/frame?id=alpha").headers["content-type"] == "image/jpeg"
    assert c.delete("/api/live/alpha").status_code == 400  # still running
    c.post("/api/live/stop", json={"id": "alpha"})
    deadline = time.time() + 30
    while time.time() < deadline and c.get("/api/live?id=alpha").json()["running"]:
        time.sleep(0.3)
    assert c.get("/api/live?id=beta").json()["running"]  # stopping one leaves the other recording
    assert c.delete("/api/live/alpha").status_code == 200
    assert [x["id"] for x in c.get("/api/live").json()["sessions"]] == ["beta"]
    c.post("/api/live/stop", json={})
    assert c.post("/api/live/start-all", json={}).status_code == 400  # live.channels is empty
