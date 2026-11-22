"""YouTube uploader against a fake Google (no network): sign-in, resumable uploads, retries, quota, queue rules."""
import json
import urllib.parse
from pathlib import Path

import pytest

from clipstudio.config import load_config
from clipstudio.publish import youtube as yt
from clipstudio.publish.metadata import build_metadata
from clipstudio.publish.queue import UploadManager, next_pacific_midnight, pacific_day_start
from clipstudio.publish.youtube import (AuthError, QuotaExceeded, Response, SetupError, TransientError, YouTubeAuth,
                                        YouTubeUploader, video_body)

CHUNK = yt.CHUNK_ALIGN


class FakeGoogle:
    """Just enough of accounts.google.com + the YouTube upload API to exercise every path."""

    def __init__(self):
        self.calls = []
        self.sessions = {}            # url -> {"size", "got", "meta"}
        self.fail_chunks = []         # statuses (or "net") to answer the next chunk PUTs with
        self.insert_error = None      # (status, reason) for the session-start POST
        self.refresh_ok = True
        self.privacy_override = None  # simulate Google locking an unverified app's upload to private
        self.n = 0

    def __call__(self, method, url, headers=None, body=None, timeout=120):
        headers = headers or {}
        self.calls.append((method, url, dict(headers), len(body or b"")))
        if url.startswith(yt.TOKEN_URL):
            form = dict(urllib.parse.parse_qsl(body.decode()))
            if form["grant_type"] == "authorization_code":
                assert form["code"] == "good-code" and form["code_verifier"]
                return Response(200, {}, json.dumps({"access_token": "at1", "refresh_token": "rt1",
                                                     "expires_in": 3600, "scope": " ".join(yt.SCOPES)}).encode())
            if not self.refresh_ok:
                return Response(400, {}, b'{"error": "invalid_grant", "error_description": "Token has been expired"}')
            self.n += 1
            return Response(200, {}, json.dumps({"access_token": f"at-r{self.n}", "expires_in": 3600}).encode())
        if url.startswith(yt.REVOKE_URL):
            return Response(200, {}, b"")
        if url.startswith(f"{yt.API}/channels"):
            return Response(200, {}, b'{"items": [{"id": "UC1", "snippet": {"title": "Clip City"}}]}')
        if url.startswith(yt.UPLOAD_URL) and method == "POST":
            if self.insert_error:
                st, reason = self.insert_error
                return Response(st, {}, json.dumps({"error": {"code": st, "message": reason,
                                                              "errors": [{"reason": reason}]}}).encode())
            assert headers["Authorization"].startswith("Bearer ")
            loc = f"https://upload.example/session/{len(self.sessions) + 1}"
            self.sessions[loc] = {"size": int(headers["X-Upload-Content-Length"]), "got": 0, "meta": json.loads(body)}
            return Response(200, {"Location": loc}, b"")
        if url in self.sessions:
            s = self.sessions[url]
            rng = headers["Content-Range"]
            if rng.startswith("bytes */"):  # status query
                if s["got"] >= s["size"]:
                    return self._done(s)
                return Response(308, {"Range": f"bytes=0-{s['got'] - 1}"} if s["got"] else {}, b"")
            if self.fail_chunks:
                f = self.fail_chunks.pop(0)
                if f == "net":
                    raise TransientError("network error talking to Google: connection reset")
                if f == "partial":  # half the chunk arrived, then the connection dropped
                    a = int(rng.split()[1].split("-")[0])
                    s["got"] = a + len(body) // 2
                    raise TransientError("network error talking to Google: connection reset")
                return Response(f, {}, b'{"error": {"errors": [{"reason": "backendError"}]}}')
            a, rest = rng.split()[1].split("-")
            b_, total = rest.split("/")
            assert int(a) == s["got"], "client must resume exactly where the server is"
            s["got"] = int(b_) + 1
            if s["got"] >= s["size"]:
                return self._done(s)
            return Response(308, {"Range": f"bytes=0-{s['got'] - 1}"}, b"")
        raise AssertionError(f"unexpected call {method} {url}")

    def _done(self, s):
        privacy = self.privacy_override or s["meta"]["status"]["privacyStatus"]
        return Response(200, {}, json.dumps({"id": "vid123", "status": {"privacyStatus": privacy}}).encode())

    def chunk_puts(self):
        return [c for c in self.calls if c[0] == "PUT" and c[3] > 0]


@pytest.fixture
def secrets_dir(tmp_path):
    d = tmp_path / "secrets"
    d.mkdir()
    (d / "client.json").write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com",
                                                             "client_secret": "sec", "token_uri": yt.TOKEN_URL}}))
    return d


def signed_in_auth(secrets_dir, google, clock=lambda: 1000.0):
    auth = YouTubeAuth(secrets_dir / "client.json", secrets_dir / "token.json", transport=google, clock=clock)
    p = auth.begin("http://127.0.0.1:8765/api/youtube/callback")
    auth.finish(p.state, "good-code")
    return auth


def clip(tmp_path, size=CHUNK * 3 + 1234, name="clip.mp4"):
    f = tmp_path / name
    f.write_bytes(b"\0" * size)
    return f


# ------------------------------------------------------------------ sign-in
def test_sign_in_uses_pkce_loopback_and_saves_token_outside_repo(secrets_dir):
    g = FakeGoogle()
    auth = YouTubeAuth(secrets_dir / "client.json", secrets_dir / "token.json", transport=g, clock=lambda: 1000.0)
    assert not auth.signed_in()
    p = auth.begin("http://127.0.0.1:8765/api/youtube/callback")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(p.url).query))
    assert q["code_challenge_method"] == "S256" and q["access_type"] == "offline"
    assert "youtube.upload" in q["scope"] and q["redirect_uri"].startswith("http://127.0.0.1:")
    tok = auth.finish(p.state, "good-code")
    assert tok["refresh_token"] == "rt1" and tok["channel"]["title"] == "Clip City"
    assert auth.signed_in() and json.loads((secrets_dir / "token.json").read_text())["refresh_token"] == "rt1"
    with pytest.raises(AuthError):  # a state is single use
        auth.finish(p.state, "good-code")


def test_sign_in_errors_are_clear(secrets_dir, tmp_path):
    with pytest.raises(SetupError, match="not found"):
        YouTubeAuth(tmp_path / "nope.json", tmp_path / "t.json").begin("http://127.0.0.1:1/")
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": {"client_id": "x"}}))
    with pytest.raises(SetupError, match="Desktop app"):
        YouTubeAuth(web, tmp_path / "t.json").begin("http://127.0.0.1:1/")
    g = FakeGoogle()
    auth = YouTubeAuth(secrets_dir / "client.json", secrets_dir / "token.json", transport=g)
    p = auth.begin("http://127.0.0.1:1/")
    with pytest.raises(AuthError, match="not completed"):
        auth.finish(p.state, None, "access_denied")
    with pytest.raises(AuthError, match="not signed in"):
        auth.access_token()


def test_access_token_refreshes_and_reports_revoked_sign_in(secrets_dir):
    g = FakeGoogle()
    now = [1000.0]
    auth = signed_in_auth(secrets_dir, g, clock=lambda: now[0])
    assert auth.access_token() == "at1"
    now[0] += 4000  # expired
    assert auth.access_token() == "at-r1"
    g.refresh_ok = False
    now[0] += 4000
    with pytest.raises(AuthError, match="Testing"):
        auth.access_token()


# ------------------------------------------------------------------ resumable upload
def test_resumable_upload_sends_chunks_and_returns_video(secrets_dir, tmp_path):
    g = FakeGoogle()
    up = YouTubeUploader(signed_in_auth(secrets_dir, g), chunk_bytes=CHUNK, sleep=lambda s: None)
    seen = []
    res = up.upload(clip(tmp_path), video_body("T", "D", ["gta"], "20", "unlisted"),
                    on_progress=lambda a, b, s: seen.append((a, b)))
    assert res.video_id == "vid123" and res.url == "https://www.youtube.com/shorts/vid123"
    assert res.privacy == "unlisted"
    assert len(g.chunk_puts()) == 4 and seen[-1][0] == seen[-1][1]
    meta = next(iter(g.sessions.values()))["meta"]
    assert meta["status"]["selfDeclaredMadeForKids"] is False and meta["snippet"]["categoryId"] == "20"


def test_upload_resumes_after_dropped_connection_and_server_errors(secrets_dir, tmp_path):
    g = FakeGoogle()
    waits = []
    up = YouTubeUploader(signed_in_auth(secrets_dir, g), chunk_bytes=CHUNK, sleep=waits.append)
    g.fail_chunks = ["partial", 503, "net"]
    res = up.upload(clip(tmp_path), video_body("T", "D", [], "20", "unlisted"))
    assert res.video_id == "vid123"
    assert waits == [2.0, 4.0, 8.0]  # backoff grows; every resume asked the session where to continue
    assert len(g.sessions) == 1      # never started over


def test_upload_gives_up_after_chunk_retries(secrets_dir, tmp_path):
    g = FakeGoogle()
    up = YouTubeUploader(signed_in_auth(secrets_dir, g), chunk_bytes=CHUNK, sleep=lambda s: None, chunk_retries=2)
    g.fail_chunks = [500, 500, 500, 500]
    with pytest.raises(TransientError):
        up.upload(clip(tmp_path), video_body("T", "D", [], "20", "unlisted"))


def test_upload_resumes_a_saved_session(secrets_dir, tmp_path):
    g = FakeGoogle()
    up = YouTubeUploader(signed_in_auth(secrets_dir, g), chunk_bytes=CHUNK, sleep=lambda s: None)
    f = clip(tmp_path)
    sessions = []
    g.fail_chunks = [500, 500, 500, 500, 500, 500, 500]
    with pytest.raises(TransientError):
        up.upload(f, video_body("T", "D", [], "20", "unlisted"), on_progress=lambda a, b, s: sessions.append(s))
    g.fail_chunks = []
    url = next(iter(g.sessions))
    g.sessions[url]["got"] = CHUNK * 2  # the server already has two chunks
    res = up.upload(f, video_body("T", "D", [], "20", "unlisted"), session=url)
    assert res.video_id == "vid123" and len(g.sessions) == 1


def test_quota_and_permission_errors_are_classified(secrets_dir, tmp_path):
    g = FakeGoogle()
    up = YouTubeUploader(signed_in_auth(secrets_dir, g), chunk_bytes=CHUNK, sleep=lambda s: None)
    g.insert_error = (403, "quotaExceeded")
    with pytest.raises(QuotaExceeded, match="midnight Pacific"):
        up.upload(clip(tmp_path), video_body("T", "D", [], "20", "unlisted"))
    g.insert_error = (403, "youtubeSignupRequired")
    with pytest.raises(AuthError):
        up.upload(clip(tmp_path), video_body("T", "D", [], "20", "unlisted"))
    g.insert_error = (400, "invalidTitle")
    with pytest.raises(yt.YouTubeError) as e:
        up.upload(clip(tmp_path), video_body("T", "D", [], "20", "unlisted"))
    assert not e.value.retryable


# ------------------------------------------------------------------ metadata
def test_metadata_credits_streamer_and_respects_limits():
    cfg = load_config(overrides=["upload.youtube.streamers={ludwig: {name: Ludwig, url: 'https://www.youtube.com/@ludwig'}}"])
    m = build_metadata(cfg.upload.youtube, '"I\'m a good noodle" <3', "ludwig",
                       "https://www.youtube.com/watch?v=7aet0w1rrQE")
    assert m["title"] == "\"I'm a good noodle\" 3 | Ludwig #shorts"
    assert "Clipped from Ludwig's live stream: https://www.youtube.com/watch?v=7aet0w1rrQE" in m["description"]
    assert "follow Ludwig: https://www.youtube.com/@ludwig" in m["description"]
    assert "Ludwig" in m["tags"] and "<" not in m["description"]
    long = build_metadata(cfg.upload.youtube, "word " * 60, "ludwig", None)
    assert len(long["title"]) <= 100 and long["title"].endswith("| Ludwig #shorts")
    assert "Clipped from" not in long["description"]  # no source link known: that line is dropped
    tw = build_metadata(cfg.upload.youtube, "x", "xqc", "https://www.twitch.tv/xqc")
    assert "https://www.twitch.tv/xqc" in tw["description"] and "Xqc" in tw["title"]


# ------------------------------------------------------------------ queue
def manager(tmp_path, secrets_dir, google, now, **over):
    sets = [f"paths.data_dir={tmp_path / 'data'}", "upload.allow_channels=[ludwig]", "upload.youtube.enabled=true",
            f"upload.youtube.client_secrets={secrets_dir / 'client.json'}",
            f"upload.youtube.token_file={secrets_dir / 'token.json'}", "upload.youtube.min_gap_minutes=30"]
    sets += [f"upload.youtube.{k}={v}" for k, v in over.items()]
    cfg = load_config(overrides=sets)
    auth = signed_in_auth(secrets_dir, google, clock=lambda: now[0])
    up = YouTubeUploader(auth, chunk_bytes=CHUNK, sleep=lambda s: None)
    return UploadManager(cfg, auth=auth, uploader=up, clock=lambda: now[0])


def test_allowlist_decides_auto_post_vs_review(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    m = manager(tmp_path, secrets_dir, FakeGoogle(), now)
    a = m.add(clip(tmp_path, name="a.mp4"), channel="Ludwig", title="A", qc_passed=True)
    b = m.add(clip(tmp_path, name="b.mp4"), channel="xqc", title="B", qc_passed=True)
    c = m.add(clip(tmp_path, name="c.mp4"), channel="ludwig", title="C", qc_passed=False)
    assert (a.status, b.status, c.status) == ("queued", "review", "review")
    assert "allow_channels" in b.reason and "QC" in c.reason
    assert m.add(tmp_path / "a.mp4", channel="ludwig").id == a.id  # no duplicates
    assert a.privacy == "unlisted"


def test_queue_posts_spaced_out_and_survives_restart(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    g = FakeGoogle()
    m = manager(tmp_path, secrets_dir, g, now)
    j1 = m.add(clip(tmp_path, name="1.mp4"), channel="ludwig", title="One", qc_passed=True)
    j2 = m.add(clip(tmp_path, name="2.mp4"), channel="ludwig", title="Two", qc_passed=True)
    assert m.step().id == j1.id and j1.status == "done" and j1.url.endswith("vid123")
    assert m.step() is None and "spacing" in m.why_waiting()  # 30 min gap
    now[0] += 31 * 60
    m2 = UploadManager(m.cfg, auth=m.auth, uploader=m.uploader, clock=lambda: now[0])  # reload from disk
    assert m2.step().id == j2.id and m2.get(j2.id).status == "done"
    assert m2.status()["today"]["posts"] == 2


def test_disabled_or_signed_out_never_posts(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    g = FakeGoogle()
    m = manager(tmp_path, secrets_dir, g, now, enabled="false")
    m.add(clip(tmp_path), channel="ludwig", title="x", qc_passed=True)
    assert m.step() is None and "off" in m.why_waiting()
    assert not g.chunk_puts()
    m.yc.enabled = True
    m.auth.sign_out()
    assert m.step() is None and "not signed in" in m.why_waiting()


def test_review_approve_edit_reject(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    m = manager(tmp_path, secrets_dir, FakeGoogle(), now)
    j = m.add(clip(tmp_path), channel="xqc", title="x", qc_passed=True)
    assert m.step() is None  # review items never post on their own
    m.edit(j.id, {"title": "Better title", "privacy": "private"})
    with pytest.raises(Exception):
        m.edit(j.id, {"clip": "/etc/passwd"})
    m.approve(j.id)
    assert m.step().status == "done"
    sent = next(iter(m.uploader.http.sessions.values()))["meta"]
    assert sent["snippet"]["title"] == "Better title" and sent["status"]["privacyStatus"] == "private"
    k = m.add(clip(tmp_path, name="k.mp4"), channel="xqc", title="k", qc_passed=True)
    assert m.reject(k.id).status == "rejected"


def test_quota_exceeded_pauses_until_pacific_midnight(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    g = FakeGoogle()
    m = manager(tmp_path, secrets_dir, g, now)
    j = m.add(clip(tmp_path), channel="ludwig", title="x", qc_passed=True)
    g.insert_error = (403, "quotaExceeded")
    m.step()
    assert j.status == "queued" and m.paused_until == next_pacific_midnight(now[0])
    assert "quota" in m.why_waiting()
    g.insert_error = None
    now[0] = m.paused_until + 1
    assert m.step().status == "done"


def test_daily_cap_is_enforced_before_spending_quota(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    m = manager(tmp_path, secrets_dir, FakeGoogle(), now, min_gap_minutes=0, max_per_day=1)
    m.add(clip(tmp_path, name="1.mp4"), channel="ludwig", title="1", qc_passed=True)
    j2 = m.add(clip(tmp_path, name="2.mp4"), channel="ludwig", title="2", qc_passed=True)
    m.step()
    assert m.step() is None and j2.status == "queued" and "daily limit" in m.why_waiting()


def test_transient_failures_back_off_then_fail(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    g = FakeGoogle()
    m = manager(tmp_path, secrets_dir, g, now, max_attempts=2)
    j = m.add(clip(tmp_path), channel="ludwig", title="x", qc_passed=True)
    g.insert_error = (503, "backendError")
    m.step()
    assert j.status == "queued" and j.not_before == now[0] + 120 and "retry 2/2" in j.reason
    assert m.step() is None  # still backing off
    now[0] += 121
    m.step()
    assert j.status == "failed" and "gave up" in j.reason
    g.insert_error = None
    m.retry(j.id)
    assert m.step().status == "done"


def test_signed_out_mid_queue_blocks_until_sign_in(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    g = FakeGoogle()
    m = manager(tmp_path, secrets_dir, g, now)
    j = m.add(clip(tmp_path), channel="ludwig", title="x", qc_passed=True)
    g.refresh_ok = False
    now[0] += 7200  # access token expired; refresh is refused
    m.step()
    assert j.status == "queued" and "sign in" in m.why_waiting()
    g.refresh_ok = True
    m.signed_in_again()
    assert m.step().status == "done"


def test_unverified_app_private_lock_is_explained(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    g = FakeGoogle()
    g.privacy_override = "private"
    m = manager(tmp_path, secrets_dir, g, now)
    j = m.add(clip(tmp_path), channel="ludwig", title="x", qc_passed=True)
    m.step()
    assert j.status == "done" and j.result_privacy == "private" and "unverified" in j.reason


def test_missing_clip_fails_cleanly(tmp_path, secrets_dir):
    now = [1_800_000_000.0]
    m = manager(tmp_path, secrets_dir, FakeGoogle(), now)
    f = clip(tmp_path)
    j = m.add(f, channel="ludwig", title="x", qc_passed=True)
    f.unlink()
    m.step()
    assert j.status == "failed" and "not found" in j.error


def test_live_moment_hook(tmp_path, secrets_dir):
    from clipstudio.live.session import Moment

    now = [1_800_000_000.0]
    m = manager(tmp_path, secrets_dir, FakeGoogle(), now)
    mo = Moment("m001_10s", {"t": 10, "score": 1.2, "reasons": []}, (0, 20), channel="ludwig",
                clip=str(clip(tmp_path)), qc_passed=True, project="p1", title="Huge crash")
    j = m.add_live_moment(mo, source_url="https://www.youtube.com/watch?v=7aet0w1rrQE")
    assert j.status == "queued" and j.moment == "m001_10s" and j.title.startswith("Huge crash | Ludwig")


def test_pacific_day_boundaries():
    t = 1_800_000_000.0
    start = pacific_day_start(t)
    assert start <= t < start + 25 * 3600
    assert 23 * 3600 <= next_pacific_midnight(t) - start <= 25 * 3600


def test_token_and_secrets_are_not_in_repo_defaults():
    yc = load_config().upload.youtube
    assert yc.enabled is False and yc.privacy == "unlisted"
    assert yc.token_file.startswith("~") and yc.client_secrets.startswith("~")
    assert not Path("configs/default.yaml").read_text().count("client_secret\":")


def test_upload_api(tmp_path, secrets_dir):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from clipstudio.server.app import create_app

    cfg = load_config(overrides=[f"paths.data_dir={tmp_path / 'data'}", "upload.allow_channels=[ludwig]",
                                 f"upload.youtube.client_secrets={secrets_dir / 'client.json'}",
                                 f"upload.youtube.token_file={secrets_dir / 'token.json'}"])
    app = create_app(cfg)
    c = TestClient(app)
    st = c.get("/api/uploads").json()
    assert st["enabled"] is False and st["signed_in"] is False and st["has_client"] is True
    assert "off" in st["waiting"]
    url = c.post("/api/youtube/connect").json()["url"]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert q["redirect_uri"].endswith("/api/youtube/callback")
    assert c.get("/api/youtube/callback", params={"state": "bogus", "code": "x"}).status_code == 400
    j = app.state.uploads.add(clip(tmp_path), channel="xqc", title="T", qc_passed=True)
    assert c.patch(f"/api/uploads/{j.id}", json={"title": "New"}).json()["title"] == "New"
    assert c.post(f"/api/uploads/{j.id}/approve").json()["status"] == "queued"
    assert c.post(f"/api/uploads/{j.id}/approve").status_code == 400  # already queued
    assert c.post(f"/api/uploads/{j.id}/explode").status_code == 404
    assert c.get("/api/uploads").json()["counts"]["queued"] == 1
    app.state.uploads.stop()


def test_personal_config_is_merged_after_repo_config(tmp_path, monkeypatch):
    f = tmp_path / "config.yaml"
    f.write_text("upload:\n  allow_channels: [ludwig]\n  youtube: {enabled: true}\n")
    monkeypatch.setenv("CLIPSTUDIO_USER_CONFIG", str(f))
    cfg = load_config()
    assert cfg.upload.allow_channels == ["ludwig"] and cfg.upload.youtube.enabled is True
    monkeypatch.setenv("CLIPSTUDIO_USER_CONFIG", "")
    assert load_config().upload.youtube.enabled is False
