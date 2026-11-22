"""The upload queue: decides auto-post vs review, spaces posts out, respects quota, retries, and runs in the
background so Clip Studio posts by itself.

State lives in ``<data_dir>/uploads/queue.json`` (survives restarts; an interrupted upload resumes its session).

Job statuses:
  review     waiting for you: channel not on ``upload.allow_channels``, or QC did not pass
  queued     approved (automatically for allowlisted channels) and waiting for its turn
  uploading  being sent now
  done       on YouTube (``url``)
  failed     gave up (``error`` says why; RETRY puts it back in the queue)
  rejected   you said no

Nothing is posted while ``upload.youtube.enabled`` is false or nobody is signed in: queued jobs simply wait,
and the panel says why.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from clipstudio.config import Config
from clipstudio.core import events
from clipstudio.core.compat import write_text_atomic
from clipstudio.core.errors import ClipStudioError
from clipstudio.core.logging import get_logger
from clipstudio.publish.metadata import build_metadata
from clipstudio.publish.youtube import (AuthError, QuotaExceeded, SetupError, YouTubeAuth, YouTubeError,
                                        YouTubeUploader, video_body)

log = get_logger("uploads")
EDITABLE = {"title", "description", "tags", "privacy"}


@dataclass
class UploadJob:
    id: str
    clip: str                      # path of the finished 9:16 mp4
    channel: str | None = None
    project: str | None = None
    clip_id: str | None = None
    moment: str | None = None
    source_url: str | None = None
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    privacy: str = "unlisted"
    status: str = "review"
    reason: str = ""               # why it is in this state (shown in the UI)
    attempts: int = 0
    not_before: float = 0.0        # retry backoff
    progress: float = 0.0          # 0..1
    session_url: str | None = None  # resumable session, so a restart resumes instead of starting over
    video_id: str | None = None
    url: str | None = None
    result_privacy: str | None = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    uploaded_at: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "UploadJob":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def pacific_day_start(ts: float) -> float:
    """Start of the YouTube quota day (midnight America/Los_Angeles) containing ``ts``."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # Windows without the tzdata package: Pacific standard time is close enough
        tz = timezone(timedelta(hours=-8))
    d = datetime.fromtimestamp(ts, tz)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def next_pacific_midnight(ts: float) -> float:
    return pacific_day_start(pacific_day_start(ts) + 36 * 3600)


def allowed_channel(cfg: Config, channel: str | None) -> bool:
    return bool(channel) and channel.lower().lstrip("@") in {c.lower().lstrip("@") for c in cfg.upload.allow_channels}


class UploadManager:
    def __init__(self, cfg: Config, auth: YouTubeAuth | None = None, uploader: YouTubeUploader | None = None,
                 state_dir: Path | None = None, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.yc = cfg.upload.youtube
        self.clock = clock
        self.auth = auth or YouTubeAuth(self.yc.client_secrets, self.yc.token_file, clock=clock)
        self.uploader = uploader or YouTubeUploader(self.auth, chunk_bytes=self.yc.chunk_mb * 1024 * 1024)
        self.dir = state_dir or Path(cfg.paths.data_dir).expanduser().resolve() / "uploads"
        self.path = self.dir / "queue.json"
        self.lock = threading.RLock()
        self.jobs: list[UploadJob] = []
        self.history: list[dict] = []    # [{t, cost}] uploads in the last two days, for quota/day limits
        self.paused_until = 0.0
        self.pause_reason = ""
        self.blocked = ""                # sign-in / setup problem that stops the worker until fixed
        self.current: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.thread: threading.Thread | None = None
        self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            log.warning("upload queue file is corrupt; starting empty (old file kept as queue.json.bad)")
            self.path.replace(self.path.with_suffix(".json.bad"))
            return
        self.jobs = [UploadJob.from_dict(j) for j in d.get("jobs", [])]
        self.history = d.get("history", [])
        self.paused_until = float(d.get("paused_until", 0))
        self.pause_reason = d.get("pause_reason", "")
        for j in self.jobs:  # an upload cut off by a restart goes back in line and resumes its session
            if j.status == "uploading":
                j.status, j.reason = "queued", "resuming after a restart"

    def save(self) -> None:
        with self.lock:
            cutoff = self.clock() - 2 * 86400
            self.history = [h for h in self.history if h["t"] >= cutoff]
            doc = {"jobs": [asdict(j) for j in self.jobs], "history": self.history,
                   "paused_until": self.paused_until, "pause_reason": self.pause_reason}
            write_text_atomic(self.path, json.dumps(doc, indent=2))

    def _publish(self, j: UploadJob, **extra) -> None:
        stage = {"uploading": "running", "done": "done", "failed": "failed"}.get(j.status, "skipped")
        events.publish("upload", job=j.id, status=j.status, moment=j.moment, project=j.project, clip=j.clip_id,
                       progress=round(j.progress, 3), url=j.url, reason=j.reason, error=j.error, **extra)
        events.publish("stage", stage="upload", status=stage, moment=j.moment, project=j.project, upload=j.id)

    # ------------------------------------------------------------ adding clips
    def add(self, clip: str | Path, channel: str | None = None, title: str | None = None,
            source_url: str | None = None, qc_passed: bool | None = None, project: str | None = None,
            clip_id: str | None = None, moment: str | None = None, force_review: bool = False) -> UploadJob:
        """Queue a finished clip. Allowlisted channels with a passing QC go straight to the queue; everything else
        waits for review. Adding the same clip file twice returns the existing job."""
        clip = str(Path(clip).resolve())
        with self.lock:
            dup = next((j for j in self.jobs if j.clip == clip and j.status not in ("rejected", "failed")), None)
            if dup:
                return dup
            ch = channel.lower().lstrip("@") if channel else None
            meta = build_metadata(self.yc, title, ch, source_url)
            j = UploadJob(id=uuid.uuid4().hex[:10], clip=clip, channel=ch, project=project, clip_id=clip_id,
                          moment=moment, source_url=source_url, privacy=self.yc.privacy, created=self.clock(), **meta)
            if force_review:
                j.status, j.reason = "review", "added for review"
            elif not allowed_channel(self.cfg, ch):
                j.status, j.reason = "review", (f"{ch or 'this channel'} is not on upload.allow_channels: post only "
                                                "with the streamer's permission")
            elif self.cfg.upload.require_qc_pass and qc_passed is not True:
                j.status, j.reason = "review", "QC did not pass: check it before posting"
            else:
                j.status, j.reason = "queued", f"{ch} allows clipping: posts automatically"
            self.jobs.append(j)
            self.save()
        log.info("upload %s: %s (%s)", j.id, j.status, j.reason)
        self._publish(j)
        self._wake.set()
        return j

    def add_live_moment(self, m, source_url: str | None = None) -> UploadJob | None:
        """Hook for live mode: called with each finished Moment."""
        if not m.clip:
            return None
        return self.add(m.clip, channel=m.channel, title=getattr(m, "title", None), source_url=source_url,
                        qc_passed=m.qc_passed, project=m.project, moment=m.id)

    # ------------------------------------------------------------ review actions
    def get(self, jid: str) -> UploadJob:
        j = next((x for x in self.jobs if x.id == jid), None)
        if j is None:
            raise ClipStudioError(f"no upload {jid!r}")
        return j

    def _set(self, jid: str, allowed_from: tuple[str, ...], status: str, reason: str) -> UploadJob:
        with self.lock:
            j = self.get(jid)
            if j.status not in allowed_from:
                raise ClipStudioError(f"upload {jid} is {j.status}; can't change it to {status}")
            j.status, j.reason, j.error = status, reason, None
            if status == "queued":
                j.attempts, j.not_before = 0, 0.0
            self.save()
        self._publish(j)
        self._wake.set()
        return j

    def approve(self, jid: str) -> UploadJob:
        return self._set(jid, ("review",), "queued", "approved by you")

    def reject(self, jid: str) -> UploadJob:
        return self._set(jid, ("review", "queued", "failed"), "rejected", "rejected by you")

    def retry(self, jid: str) -> UploadJob:
        return self._set(jid, ("failed",), "queued", "retry requested")

    def edit(self, jid: str, changes: dict) -> UploadJob:
        bad = set(changes) - EDITABLE
        if bad:
            raise ClipStudioError(f"can't edit {sorted(bad)}; editable: {sorted(EDITABLE)}")
        if "privacy" in changes and changes["privacy"] not in ("private", "unlisted", "public"):
            raise ClipStudioError("privacy must be private, unlisted or public")
        if "title" in changes and not (0 < len(str(changes["title"]).strip()) <= 100):
            raise ClipStudioError("title must be 1-100 characters")
        with self.lock:
            j = self.get(jid)
            if j.status not in ("review", "queued", "failed"):
                raise ClipStudioError(f"upload {jid} is {j.status}; it can't be edited now")
            for k, v in changes.items():
                setattr(j, k, [str(t) for t in v] if k == "tags" else str(v).replace("<", "").replace(">", ""))
            self.save()
        return j

    # ------------------------------------------------------------ scheduling
    def _today(self, now: float) -> tuple[int, int]:
        start = pacific_day_start(now)
        today = [h for h in self.history if h["t"] >= start]
        return len(today), sum(h.get("cost", self.yc.upload_cost) for h in today)

    def next_slot(self, now: float | None = None) -> float:
        """Earliest time the spacing rule allows the next post."""
        now = self.clock() if now is None else now
        last = max((h["t"] for h in self.history), default=0.0)
        return max(now, last + self.yc.min_gap_minutes * 60)

    def why_waiting(self) -> str:
        now = self.clock()
        if not self.yc.enabled:
            return "uploader is off (set upload.youtube.enabled: true after the one-time setup)"
        if not self.auth.has_client():
            return "OAuth client file missing: see docs/YOUTUBE_SETUP.md"
        if not self.auth.signed_in():
            return "not signed in: press CONNECT YOUTUBE"
        if self.blocked:
            return self.blocked
        if self.paused_until > now:
            return f"{self.pause_reason}; resumes {time.strftime('%a %H:%M', time.localtime(self.paused_until))}"
        slot = self.next_slot(now)
        if slot > now and any(j.status == "queued" for j in self.jobs):
            return f"spacing posts {self.yc.min_gap_minutes:g} min apart; next at {time.strftime('%H:%M', time.localtime(slot))}"
        return ""

    def _next_job(self, now: float) -> UploadJob | None:
        ready = [j for j in self.jobs if j.status == "queued" and j.not_before <= now]
        return min(ready, key=lambda j: j.created) if ready else None

    def step(self) -> UploadJob | None:
        """Upload at most one job if everything allows it. Returns the job worked on (tests call this directly)."""
        now = self.clock()
        if not self.yc.enabled or not self.auth.signed_in() or self.blocked or self.paused_until > now:
            return None
        if self.next_slot(now) > now:
            return None
        with self.lock:
            j = self._next_job(now)
            if j is None:
                return None
            count, used = self._today(now)
            if count >= self.yc.max_per_day or used + self.yc.upload_cost > self.yc.daily_quota:
                self.paused_until = next_pacific_midnight(now)
                self.pause_reason = (f"daily limit reached ({count} posts, {used}/{self.yc.daily_quota} quota units)")
                self.save()
                log.info("uploads paused: %s", self.pause_reason)
                return None
            if not Path(j.clip).is_file():
                j.status, j.error, j.reason = "failed", f"clip file not found: {j.clip}", "file missing"
                self.save()
                self._publish(j)
                return j
            j.status, j.reason, j.attempts, j.error = "uploading", "sending to YouTube", j.attempts + 1, None
            self.current = j.id
            self.save()
        self._publish(j)
        self._run_upload(j, now)
        self.current = None
        return j

    def _run_upload(self, j: UploadJob, now: float) -> None:
        last_pub = [0.0]

        def progress(sent: int, total: int, session: str) -> None:
            j.progress, j.session_url = sent / total if total else 0.0, session
            if time.time() - last_pub[0] > 1 or sent == total:
                last_pub[0] = time.time()
                self.save()
                self._publish(j)

        body = video_body(j.title, j.description, j.tags, self.yc.category_id, j.privacy)
        try:
            res = self.uploader.upload(Path(j.clip), body, session=j.session_url, on_progress=progress)
        except QuotaExceeded as e:
            with self.lock:
                j.status, j.reason, j.error = "queued", "waiting for quota", str(e)
                self.paused_until, self.pause_reason = next_pacific_midnight(self.clock()), "YouTube quota used up"
                self.save()
            log.warning("upload %s: %s", j.id, e)
        except (AuthError, SetupError) as e:
            with self.lock:
                j.status, j.reason, j.error = "queued", "waiting for sign-in", str(e)
                self.blocked = str(e)
                self.save()
            log.error("upload %s: %s", j.id, e)
        except YouTubeError as e:
            with self.lock:
                if e.retryable and j.attempts < self.yc.max_attempts:
                    wait = min(3600.0, 120.0 * 2 ** (j.attempts - 1))
                    j.status, j.not_before, j.error = "queued", self.clock() + wait, str(e)
                    j.reason = f"retry {j.attempts + 1}/{self.yc.max_attempts} in {wait / 60:.0f} min"
                else:
                    j.status, j.error = "failed", str(e)
                    j.reason = "gave up after retries" if e.retryable else "YouTube refused it"
                    if "session expired" in str(e):
                        j.session_url = None
                self.save()
            log.error("upload %s: %s", j.id, e)
        except Exception as e:  # never kill the worker
            with self.lock:
                j.status, j.error, j.reason = "failed", f"{type(e).__name__}: {e}", "unexpected error"
                self.save()
            log.exception("upload %s crashed", j.id)
        else:
            with self.lock:
                j.status, j.progress, j.video_id, j.url = "done", 1.0, res.video_id, res.url
                j.result_privacy, j.uploaded_at, j.session_url, j.error = res.privacy, self.clock(), None, None
                j.reason = f"posted ({res.privacy or j.privacy})"
                if res.privacy and res.privacy != j.privacy:
                    j.reason += (f"; you asked for {j.privacy}: Google keeps uploads from unverified apps private "
                                 "until the API audit passes (docs/YOUTUBE_SETUP.md)")
                self.history.append({"t": self.clock(), "cost": self.yc.upload_cost})
                self.save()
            log.info("uploaded %s -> %s (%s)", Path(j.clip).name, j.url, res.privacy)
        self._publish(j)

    # ------------------------------------------------------------ sign-in hooks
    def signed_in_again(self) -> None:
        self.blocked = ""
        self._wake.set()

    # ------------------------------------------------------------ background worker
    def start(self, tick: float = 5.0) -> None:
        if self.thread and self.thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    while self.step() is not None and not self._stop.is_set():
                        pass
                except Exception:
                    log.exception("upload worker error")
                self._wake.wait(tick)
                self._wake.clear()

        self.thread = threading.Thread(target=loop, daemon=True, name="clipstudio-uploads")
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    # ------------------------------------------------------------ status for the UI
    def status(self) -> dict:
        now = self.clock()
        tok = self.auth.token() or {}
        count, used = self._today(now)
        with self.lock:
            jobs = sorted((asdict(j) for j in self.jobs), key=lambda j: -j["created"])[:100]
        return {"enabled": self.yc.enabled, "has_client": self.auth.has_client(), "signed_in": self.auth.signed_in(),
                "channel": tok.get("channel"), "privacy": self.yc.privacy, "waiting": self.why_waiting(),
                "current": self.current, "paused_until": self.paused_until if self.paused_until > now else None,
                "today": {"posts": count, "max_posts": self.yc.max_per_day, "quota_used": used,
                          "quota": self.yc.daily_quota},
                "next_slot": self.next_slot(now), "allow_channels": self.cfg.upload.allow_channels,
                "counts": {s: sum(1 for j in self.jobs if j.status == s)
                           for s in ("review", "queued", "uploading", "done", "failed", "rejected")},
                "jobs": jobs}
