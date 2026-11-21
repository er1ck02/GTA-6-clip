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
