"""YouTube Data API v3: one-time OAuth sign-in (installed-app flow) and resumable video uploads.

Standard library only (urllib), so nothing extra to install. Every network call goes through a ``Transport``
so tests replace it with a fake and never touch Google.

Sign-in: the "Desktop app" OAuth client flow with PKCE and a loopback redirect
(``http://127.0.0.1:<port>/...``). The person signs in once in their own browser; the refresh token is saved
to ``youtube.token_file`` (outside the repo, owner-only permissions) and access tokens are refreshed from it
silently afterwards.

Uploads use the resumable protocol: one POST creates an upload session (metadata), then the file is sent in
chunks with ``Content-Range``. A dropped connection or a 5xx asks the session how much arrived and resumes
from there, so a 60 MB clip never restarts from zero. The session URL is handed back to the caller after each
chunk so an upload interrupted by a restart resumes too (sessions live about a week).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from clipstudio.core.compat import write_text_atomic
from clipstudio.core.errors import ClipStudioError
from clipstudio.core.logging import get_logger

log = get_logger("youtube")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API = "https://www.googleapis.com/youtube/v3"
# upload + read (read is only used to show which channel you signed in to)
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.readonly"]
CHUNK_ALIGN = 256 * 1024  # resumable chunks must be multiples of 256 KiB (except the last)


# ---------------------------------------------------------------- errors
class YouTubeError(ClipStudioError):
    """An upload failed. ``retryable`` says whether trying again later can help."""
    retryable = False


class TransientError(YouTubeError):
    """Network trouble or a Google server error: retry with backoff."""
    retryable = True


class QuotaExceeded(YouTubeError):
    """The project's daily API quota (or the channel's upload limit) is used up; resets at midnight Pacific."""


class AuthError(YouTubeError):
    """Not signed in, or the sign-in was revoked/expired: sign in again."""


class SetupError(YouTubeError):
    """The OAuth client file is missing or wrong."""


# ---------------------------------------------------------------- transport
@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict:
        try:
            return json.loads(self.body or b"{}")
        except ValueError:
            return {}

    def header(self, name: str) -> str | None:
        name = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == name), None)


class Transport(Protocol):
    def __call__(self, method: str, url: str, headers: dict[str, str] | None = None,
                 body: bytes | None = None, timeout: float = 120) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # 308 means "resume incomplete" here, never a redirect
        return None


_opener = urllib.request.build_opener(_NoRedirect())


def urllib_transport(method: str, url: str, headers: dict[str, str] | None = None,
                     body: bytes | None = None, timeout: float = 120) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return Response(r.status, dict(r.headers.items()), r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, dict(e.headers.items()) if e.headers else {}, e.read() or b"")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise TransientError(f"network error talking to Google: {getattr(e, 'reason', e)}") from e


def _reason(r: Response) -> tuple[str, str]:
    """(reason, message) from a Google API error body."""
    d = r.json()
    err = d.get("error")
    if isinstance(err, dict):
        errs = err.get("errors") or [{}]
        return errs[0].get("reason", "") or err.get("status", ""), err.get("message", "")
    return str(err or ""), d.get("error_description", "") or r.body[:200].decode("utf-8", "replace")


def raise_for(r: Response, what: str) -> None:
    """Turn a non-2xx response into the right error class with a message a person can act on."""
    if 200 <= r.status < 300:
        return
    reason, msg = _reason(r)
    detail = f"{what}: HTTP {r.status} {reason} {msg}".strip()
    if r.status == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded",
                                      "rateLimitExceeded", "userRateLimitExceeded"):
        if reason in ("rateLimitExceeded", "userRateLimitExceeded"):
            raise TransientError(detail)
        raise QuotaExceeded(detail + " (resets at midnight Pacific time)")
    if r.status == 401 or reason in ("invalid_grant", "unauthorized_client", "authError"):
        raise AuthError(detail + ": sign in to YouTube again")
    if r.status == 403 and reason in ("forbidden", "insufficientPermissions", "youtubeSignupRequired"):
        raise AuthError(detail + " (the signed-in Google account needs a YouTube channel and upload permission)")
    if r.status == 429 or r.status >= 500:
        raise TransientError(detail)
    raise YouTubeError(detail)


# ---------------------------------------------------------------- auth
def _expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


def load_client(path: str | Path) -> dict:
    """The OAuth client (``client_id``/``client_secret``) from the JSON downloaded from Google Cloud."""
    f = _expand(path)
    if not f.is_file():
        raise SetupError(f"YouTube OAuth client file not found at {f}. Download it from Google Cloud "
                         "(APIs & Services > Credentials > your Desktop app client > Download JSON) and save it "
                         "there, or set upload.youtube.client_secrets. See docs/YOUTUBE_SETUP.md")
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except ValueError as e:
        raise SetupError(f"{f} is not valid JSON: {e}") from e
    c = d.get("installed") or d.get("web")
    if not c or not c.get("client_id"):
        raise SetupError(f"{f} is not an OAuth client file (expected an 'installed' section; create the client "
                         "as type 'Desktop app')")
    if "web" in d and "installed" not in d:
        raise SetupError(f"{f} is a 'Web application' client; create a 'Desktop app' client instead")
    return {"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
            "token_uri": c.get("token_uri", TOKEN_URL)}


@dataclass
class PendingSignIn:
    state: str
    verifier: str
    redirect_uri: str
    url: str
    created: float


class YouTubeAuth:
    """Holds the client file, the saved token and any sign-in in progress. Thread-safe enough for one server."""

    def __init__(self, client_secrets: str | Path, token_file: str | Path, transport: Transport = urllib_transport,
                 clock: Callable[[], float] = time.time):
        self.client_path = _expand(client_secrets)
        self.token_path = _expand(token_file)
        self.http = transport
        self.clock = clock
        self.pending: dict[str, PendingSignIn] = {}

    # ---- token file
    def token(self) -> dict | None:
        if not self.token_path.is_file():
            return None
        try:
            return json.loads(self.token_path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _save(self, tok: dict) -> None:
        write_text_atomic(self.token_path, json.dumps(tok, indent=2))
        try:
            os.chmod(self.token_path, 0o600)  # owner only (a no-op beyond read-only on Windows)
        except OSError:
            pass

    def signed_in(self) -> bool:
        t = self.token()
        return bool(t and t.get("refresh_token"))

    def has_client(self) -> bool:
        return self.client_path.is_file()

    # ---- sign-in
    def begin(self, redirect_uri: str) -> PendingSignIn:
        """Start a sign-in: returns the Google URL the person opens. ``redirect_uri`` must be a loopback URL."""
        client = load_client(self.client_path)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        q = {"client_id": client["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
             "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
             "code_challenge": challenge, "code_challenge_method": "S256", "include_granted_scopes": "true"}
        p = PendingSignIn(state, verifier, redirect_uri, f"{AUTH_URL}?{urllib.parse.urlencode(q)}", self.clock())
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if now - v.created < 900}
        self.pending[state] = p
        return p

    def finish(self, state: str, code: str | None, error: str | None = None) -> dict:
        """Exchange the code Google sent to the redirect for tokens, and save them."""
        p = self.pending.pop(state or "", None)
        if p is None:
            raise AuthError("this sign-in link is unknown or expired; start the YouTube sign-in again")
        if error:
            raise AuthError(f"Google sign-in was not completed: {error}")
        if not code:
            raise AuthError("Google did not return a sign-in code")
        client = load_client(self.client_path)
        r = self._form(client["token_uri"], {"code": code, "client_id": client["client_id"],
                                             "client_secret": client["client_secret"], "redirect_uri": p.redirect_uri,
                                             "grant_type": "authorization_code", "code_verifier": p.verifier})
        raise_for(r, "sign-in")
        d = r.json()
        if not d.get("refresh_token"):
            raise AuthError("Google returned no refresh token; remove Clip Studio's access at "
                            "https://myaccount.google.com/permissions and sign in again")
        tok = {"refresh_token": d["refresh_token"], "access_token": d.get("access_token"),
               "expires_at": self.clock() + float(d.get("expires_in", 3600)) - 60, "scope": d.get("scope", ""),
               "created": self.clock()}
        self._save(tok)
        try:
            tok["channel"] = self.channel_info(tok["access_token"])
            self._save(tok)
        except YouTubeError as e:
            log.warning("signed in, but could not read the channel name: %s", e)
        log.info("YouTube sign-in saved to %s", self.token_path)
        return tok

    def sign_out(self) -> None:
        t = self.token()
        if t and t.get("refresh_token"):
            try:
                self._form(REVOKE_URL, {"token": t["refresh_token"]})
            except YouTubeError:
                pass
        self.token_path.unlink(missing_ok=True)

    # ---- access tokens
    def access_token(self, force_refresh: bool = False) -> str:
        t = self.token()
        if not t or not t.get("refresh_token"):
            raise AuthError("not signed in to YouTube: open Clip Studio's UPLOADS panel and press CONNECT YOUTUBE "
                            "(or run `clipstudio youtube login`)")
        if not force_refresh and t.get("access_token") and self.clock() < float(t.get("expires_at", 0)):
            return t["access_token"]
        client = load_client(self.client_path)
        r = self._form(client["token_uri"], {"refresh_token": t["refresh_token"], "client_id": client["client_id"],
                                             "client_secret": client["client_secret"], "grant_type": "refresh_token"})
        if r.status == 400 and _reason(r)[0] == "invalid_grant":
            raise AuthError("the saved YouTube sign-in was revoked or expired (an app left in Google's 'Testing' "
                            "mode expires sign-ins after 7 days; see docs/YOUTUBE_SETUP.md): sign in again")
        raise_for(r, "token refresh")
        d = r.json()
        t.update(access_token=d["access_token"], expires_at=self.clock() + float(d.get("expires_in", 3600)) - 60)
        self._save(t)
        return t["access_token"]

    def channel_info(self, access: str | None = None) -> dict:
        r = self.http("GET", f"{API}/channels?part=snippet&mine=true",
                      {"Authorization": f"Bearer {access or self.access_token()}"}, None, 30)
        raise_for(r, "channel lookup")
        items = r.json().get("items") or []
        if not items:
            raise AuthError("this Google account has no YouTube channel; create one, then sign in again")
        return {"id": items[0]["id"], "title": items[0]["snippet"].get("title", "")}

    def _form(self, url: str, data: dict) -> Response:
        return self.http("POST", url, {"Content-Type": "application/x-www-form-urlencoded"},
                         urllib.parse.urlencode(data).encode(), 30)


# ---------------------------------------------------------------- uploads
@dataclass
class UploadResult:
    video_id: str
    privacy: str          # what YouTube actually set (unverified apps get "private" whatever was asked)
    url: str
    raw: dict


def video_body(title: str, description: str, tags: list[str], category_id: str, privacy: str,
               publish_at: str | None = None) -> dict:
    status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True}
    if publish_at:  # YouTube only schedules private videos
        status.update(privacyStatus="private", publishAt=publish_at)
    return {"snippet": {"title": title, "description": description, "tags": tags, "categoryId": category_id},
            "status": status}


class YouTubeUploader:
    def __init__(self, auth: YouTubeAuth, transport: Transport | None = None, chunk_bytes: int = 8 * 1024 * 1024,
                 sleep: Callable[[float], None] = time.sleep, chunk_retries: int = 5):
        self.auth = auth
        self.http = transport or auth.http
        self.chunk = max(CHUNK_ALIGN, chunk_bytes // CHUNK_ALIGN * CHUNK_ALIGN)
        self.sleep = sleep
        self.chunk_retries = chunk_retries

    def _call(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float = 300) -> Response:
        """One authorised request; refreshes the access token once on a 401."""
        for attempt in (0, 1):
            h = dict(headers, Authorization=f"Bearer {self.auth.access_token(force_refresh=attempt == 1)}")
            r = self.http(method, url, h, body, timeout)
            if r.status != 401 or attempt:
                return r
        return r  # pragma: no cover

    def start_session(self, path: Path, body: dict) -> str:
        size = path.stat().st_size
        r = self._call("POST", f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
                       {"Content-Type": "application/json; charset=UTF-8", "X-Upload-Content-Length": str(size),
                        "X-Upload-Content-Type": "video/mp4"}, json.dumps(body).encode(), 60)
        raise_for(r, "starting the upload")
        loc = r.header("Location")
        if not loc:
            raise TransientError("Google did not return an upload session URL")
        return loc

    def _offset(self, session: str, size: int) -> int | Response:
        """How many bytes the session has (or the final response if it already finished)."""
        r = self._call("PUT", session, {"Content-Range": f"bytes */{size}", "Content-Length": "0"}, b"", 60)
        if r.status in (200, 201):
            return r
        if r.status == 308:
            rng = r.header("Range")
            return int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
        if r.status in (404, 410):
            raise YouTubeError(f"upload session expired (HTTP {r.status}); it will start over")
        raise_for(r, "checking upload progress")
        return 0

    def upload(self, path: Path, body: dict, session: str | None = None,
               on_progress: Callable[[int, int, str], None] | None = None) -> UploadResult:
        """Upload ``path``. ``on_progress(sent, total, session_url)`` is called after every chunk."""
        path = Path(path)
        if not path.is_file():
            raise YouTubeError(f"clip file not found: {path}")
        size = path.stat().st_size
        if size == 0:
            raise YouTubeError(f"clip file is empty: {path}")
        if session:
            try:
                got = self._offset(session, size)
            except YouTubeError as e:
                if e.retryable:
                    raise
                log.info("old upload session unusable (%s); starting a new one", e)
                session, got = None, 0
        if not session:
            session, got = self.start_session(path, body), 0
        if isinstance(got, Response):
            return self._result(got)
        failures = 0
        with path.open("rb") as f:
            while True:
                f.seek(got)
                data = f.read(self.chunk)
                end = got + len(data) - 1
                try:
                    r = self._call("PUT", session, {"Content-Length": str(len(data)),
                                                    "Content-Range": f"bytes {got}-{end}/{size}",
                                                    "Content-Type": "video/mp4"}, data)
                    if r.status in (200, 201):
                        if on_progress:
                            on_progress(size, size, session)
                        return self._result(r)
                    if r.status == 308:
                        rng = r.header("Range")
                        got = int(rng.rsplit("-", 1)[1]) + 1 if rng else 0
                        failures = 0
                        if on_progress:
                            on_progress(got, size, session)
                        continue
                    raise_for(r, "uploading")
                    raise TransientError(f"unexpected HTTP {r.status} while uploading")
                except TransientError as e:
                    failures += 1
                    if failures > self.chunk_retries:
                        raise
                    wait = min(60.0, 2.0 ** failures)
                    log.warning("upload interrupted (%s); resuming in %.0fs", e, wait)
                    self.sleep(wait)
                    res = self._offset(session, size)
                    if isinstance(res, Response):
                        return self._result(res)
                    got = res

    @staticmethod
    def _result(r: Response) -> UploadResult:
        d = r.json()
        vid = d.get("id")
        if not vid:
            raise YouTubeError(f"upload finished but YouTube returned no video id: {r.body[:200]!r}")
        privacy = (d.get("status") or {}).get("privacyStatus", "")
        return UploadResult(vid, privacy, f"https://www.youtube.com/shorts/{vid}", d)
