"""Live mode loop: record -> rolling buffer -> triggers -> save moment -> edit -> review/upload queue.

Only the moments are kept. Each saved moment is ``pre_seconds`` before the trigger and ``post_seconds`` after,
joined from the buffer without re-encoding, with the matching chat written next to it so the normal pipeline's
chat signal works. A worker thread then runs the usual analysis, 9:16 render and QC on that short file while
recording continues. The queue (``queue.json``) says for each clip whether it may be uploaded: only when the
channel is on ``upload.allow_channels`` and QC passed; everything else waits for review.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipstudio.core.compat import write_text_atomic
from clipstudio.config import Config
from clipstudio.core import events
from clipstudio.core.errors import ClipStudioError, MediaError
from clipstudio.core.logging import get_logger
from clipstudio.live.buffer import Recorder, Ring, Segment, concat_segments
from clipstudio.live.chat import ChatSource
from clipstudio.live.triggers import Trigger, TriggerDetector, segment_loudness

log = get_logger("live")
MAX_RECONNECTS = 20


class LiveError(ClipStudioError):
    """Live mode could not record at all (bad URL, unsupported stream, network)."""


@dataclass
class Moment:
    id: str
    trigger: dict
    window: tuple[float, float]           # stream seconds asked for
    saved: tuple[float, float] = (0, 0)  # stream seconds actually saved (segment boundaries)
    path: str = ""
    chat_path: str | None = None
    channel: str | None = None
    status: str = "pending"               # pending | saved | queued | processing | done | failed
    clip: str | None = None
    project: str | None = None
    qc_passed: bool | None = None
    qc_summary: str | None = None
    upload: str = "review"                # review | allowed | not_allowed
    upload_note: str = ""
    error: str | None = None
    created: float = field(default_factory=time.time)
    attempts: int = 0                     # edit attempts (a failed edit is retried once on the next production start)


def upload_decision(cfg: Config, channel: str | None, qc_passed: bool | None,
                    clip: str | None = None) -> tuple[str, str]:
    allowed = {c.lower().lstrip("@") for c in cfg.upload.allow_channels}
    if not channel or channel.lower() not in allowed:
        return "review", (f"{channel or 'this channel'} is not on upload.allow_channels: "
                          "review it and only post with the streamer's permission")
    if cfg.upload.require_qc_pass and not qc_passed:
        return "review", "QC did not pass: review before posting"
    if cfg.upload.require_review:
        from clipstudio.supervise import is_approved
        if not is_approved(clip):
            return "review", "waiting for the supervisor's review (clipstudio supervise); post only from the approved folder"
    return "allowed", f"{channel} allows clipping; ready to upload (connect accounts to publish)"


_gates: dict[int, threading.BoundedSemaphore] = {}
_gates_lock = threading.Lock()


def edit_gate(n: int) -> threading.BoundedSemaphore:
    """One process-wide gate per size: every stream's editor waits here, so watching many channels never runs
    more than ``live.max_parallel_edits`` analyses/renders at once (the GPU and CPU are shared)."""
    with _gates_lock:
        if n not in _gates:
            _gates[n] = threading.BoundedSemaphore(n)
        return _gates[n]


class LiveSession:
    def __init__(self, cfg: Config, recorder: Recorder, chat: ChatSource, out_dir: Path,
                 channel: str | None = None, process: bool | None = None, max_seconds: float = 0):
        self.cfg = cfg
        self.lc = cfg.live
        self.rec = recorder
        self.chat = chat
        self.out = out_dir
        self.channel = channel
        self.process = self.lc.auto_process if process is None else process
        self.max_seconds = max_seconds
        self.ring = Ring(self.lc.buffer_seconds)
        self.det = TriggerDetector(self.lc, cfg.chat)
        self.moments: list[Moment] = []
        self.pending: list[Moment] = []
        self.chat_log: list[tuple[float, str, str]] = []  # (stream time, user, text), trimmed with the buffer
        self.emote_ids: dict[str, str] = {}  # Twitch emote name -> id seen in this chat (saved as emotes.json)
        self._emotes_saved = 0
        self.jobs: queue.Queue[Moment | None] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.peak_disk = 0
        self.stopped_reason = ""
        self._qlock = threading.Lock()
        self._stop = threading.Event()
        self._abandon = threading.Event()
        self.running = False
        self.started_at = 0.0
        self.chat_count = 0
        self.recent_chat: deque[dict] = deque(maxlen=40)
        self.triggers: list[dict] = []

    def stop(self) -> None:
        """Ask the loop to finish (pending moments are still saved and edited)."""
        self._stop.set()

    def abandon(self) -> None:
        """Stop quickly: moments are still saved, but only the edit already running is finished. Moments waiting
        for an editing slot stay on disk as "saved" so the next production start edits them."""
        self._abandon.set()
        self._stop.set()

    def _shelve(self, m: Moment) -> None:
        m.status, m.upload_note = "saved", "saved for the next production start (stopped before editing)"
        self.save_queue()
        events.publish("live_moment", channel=self.channel, moment=m.id, status="saved")

    def snapshot(self) -> dict:
        """Everything the workstation's live panel shows, as plain data (safe to call from another thread)."""
        segs = list(self.ring.segments)
        head = self.ring.head
        hist = list(self.det.history)[-240:]
        chat = dict(list(self.det.chat.items()))
        hype = dict(list(self.det.hype.items()))
        cmds = dict(list(self.det.command.items()))
        audio = dict(list(self.det.audio.items()))
        lo = int(head) - 180
        return {
            "running": self.running, "stopped": self.stopped_reason, "channel": self.channel,
            "source": str(self.rec.url or self.rec.file), "chat_source": self.chat.name,
            "started_at": self.started_at, "head": round(head, 2), "out": str(self.out),
            "buffer_seconds": self.lc.buffer_seconds, "threshold": self.lc.threshold,
            "pre": self.lc.pre_seconds, "post": self.lc.post_seconds,
            "segments": [{"i": x.index, "start": round(x.start, 2), "end": round(x.end, 2),
                          "pinned": self.ring._pinned(x)} for x in segs],
            "disk_mb": round(sum(x.path.stat().st_size for x in segs if x.path.exists()) / 1e6, 1),
            "discarded": self.ring.deleted, "reconnects": self.rec.restarts,
            "signals": [{"t": t, "score": sc, **sig} for t, sc, sig in hist],
            "audio": {k: round(v, 1) for k, v in audio.items() if k >= lo},
            "chat_rate": {k: v for k, v in chat.items() if k >= lo},
            "hype": {k: v for k, v in hype.items() if k >= lo},
            "commands": {k: v for k, v in cmds.items() if k >= lo},
            "chat_count": self.chat_count, "recent_chat": list(self.recent_chat),
            "triggers": list(self.triggers),
            "moments": [asdict(m) for m in self.moments],
            "allowed": bool(self.channel and self.channel.lower() in
                            {c.lower().lstrip("@") for c in self.cfg.upload.allow_channels}),
        }

    # ------------------------------------------------------------ queue file
    def save_queue(self) -> None:
        with self._qlock:  # the recorder loop and the editing worker both update it
            doc = {"channel": self.channel, "moments": [asdict(m) for m in self.moments]}
            write_text_atomic(self.out / "queue.json", json.dumps(doc, indent=2))

    # ------------------------------------------------------------ capture
    def _on_trigger(self, tr: Trigger) -> None:
        if self.lc.max_moments and len(self.moments) >= self.lc.max_moments:
            return
        mid = f"m{len(self.moments) + 1:03d}_{int(tr.t)}s"
        a, b = max(0.0, tr.t - self.lc.pre_seconds), tr.t + self.lc.post_seconds
        m = Moment(mid, asdict(tr), (a, b), channel=self.channel)
        self.ring.pin(mid, a, b)
        self.moments.append(m)
        self.triggers.append({"id": mid, "t": tr.t, "score": tr.score, "reasons": tr.reasons})
        events.publish("stage", stage="live_trigger", status="done", moment=mid)
        self.pending.append(m)
        log.info("moment %s at %.0fs (score %.2f: %s); saving %.0f-%.0fs", mid, tr.t, tr.score,
                 ", ".join(tr.reasons) or "audio", a, b)
        events.publish("live_moment", moment=mid, t=tr.t, score=tr.score, reasons=tr.reasons, status="pending")
        self.save_queue()

    def _finish_pending(self, final: bool = False) -> None:
        for m in list(self.pending):
            a, b = m.window
            if self.ring.head < b and not final:
                continue
            segs = self.ring.covering(a, b)
            self.pending.remove(m)
            try:
                dst = self.out / "moments" / f"{m.id}.mp4"
                concat_segments(segs, dst)
                m.saved = (round(segs[0].start, 3), round(segs[-1].end, 3))
                m.path = str(dst)
                m.chat_path = self._write_chat(m, dst)
                m.status = "saved"
                log.info("saved %s (%.0fs, %d segments)", dst.name, m.saved[1] - m.saved[0], len(segs))
                if self.process:
                    self.jobs.put(m)
            except Exception as e:
                m.status, m.error = "failed", str(e)
                log.error("could not save %s: %s", m.id, e)
            finally:
                self.ring.unpin(m.id)
            events.publish("live_moment", channel=self.channel, moment=m.id, status=m.status)
            self.save_queue()

    def _save_emotes(self) -> None:
        """Keep the channel's emote ids next to its moments, so the chat overlay can draw subscriber emotes."""
        if len(self.emote_ids) == self._emotes_saved:
            return
        try:
            write_text_atomic(self.out / "emotes.json", json.dumps(self.emote_ids, indent=1, sort_keys=True))
            self._emotes_saved = len(self.emote_ids)
        except OSError as e:
            log.debug("could not save emote ids: %s", e)

    def _write_chat(self, m: Moment, video: Path) -> str | None:
        self._save_emotes()
        a, b = m.saved
        rows = [{"time_in_seconds": round(t - a, 3), "message": text, "author": {"name": user}}
                for t, user, text in self.chat_log if a <= t <= b]
        if not rows:
            return None
        p = video.with_suffix(".chat.json")
        p.write_text(json.dumps(rows))
        return str(p)

    # ------------------------------------------------------------ editing worker
    def _worker(self) -> None:
        while True:
            m = self.jobs.get()
            if m is None:
                return
            if self._abandon.is_set():
                self._shelve(m)
                continue
            m.status = "queued"  # waiting for a free editing slot (shared by every stream being watched)
            self.save_queue()
            events.publish("live_moment", channel=self.channel, moment=m.id, status="queued")
            with edit_gate(self.lc.max_parallel_edits):
                if self._abandon.is_set():
                    self._shelve(m)
                    continue
                m.status = "processing"
                self.save_queue()
                events.publish("live_moment", channel=self.channel, moment=m.id, status="processing")
                self._edit(m)
            self.save_queue()
            events.publish("live_moment", channel=self.channel, moment=m.id, status=m.status, clip=m.clip, upload=m.upload)

    def _edit(self, m: Moment) -> None:
        from clipstudio.pipeline.studio import Studio

        m.attempts += 1
        try:
            dur = m.saved[1] - m.saved[0]
            cfg = self.cfg.model_copy(deep=True)
            h = cfg.highlights
            h.minimum_score = 0.0  # the trigger already decided this moment is worth a clip
            h.maximum_duration = max(6.0, min(h.maximum_duration, dur - 1))
            h.minimum_duration = min(h.minimum_duration, h.maximum_duration * 0.6)
            h.target_duration = min(max(h.target_duration, h.minimum_duration), h.maximum_duration)
            st = Studio(cfg)
            # frame the clip on what chat reacted to: the trigger time, minus chat's lag when chat set it off
            lag = self.lc.chat_lag_seconds if {"chat", "hype", "command"} & set(m.trigger.get("reasons", [])) else 1.0
            anchor = min(max(0.0, float(m.trigger.get("t", m.saved[0])) - lag - m.saved[0]), dur)
            p = st.open_or_create(input_path=Path(m.path), chat=Path(m.chat_path) if m.chat_path else None,
                                  anchor=anchor)
            m.project = p.id
            self.save_queue()
            token = events.context.set({"project": p.id, "moment": m.id})
            try:
                events.publish("live_moment", channel=self.channel, moment=m.id, status="processing", project=p.id)
                cs = st.analyze(p)
                ids = st.default_selection(p, 1)
                best = cs.get(ids[0]) if ids else None
                why = None
                if best is not None and best.status != "accepted":
                    need = self.cfg.highlights.min_evidence
                    if best.score < self.lc.min_clip_score:
                        why = f"best clip scores {best.score:.2f}, under live.min_clip_score {self.lc.min_clip_score:.2f}"
                    elif best.features.get("payoff_evidence", need) < need:
                        why = best.reason.split("; payoff seen in: ")[-1].join(["no clear payoff (seen in: ", ")"])
                    elif best.features.get("llm_keep", 1.0) == 0.0:
                        why = "the LLM judged it not worth posting: " + best.reason.split("LLM ")[-1]
                if why:
                    m.status, m.upload = "skipped", "review"
                    m.upload_note = f"weak moment: {why}; the footage is kept, nothing was edited"
                    log.info("moment %s skipped: %s", m.id, m.upload_note)
                    return
                res = st.render(p, ids)[0] if ids else None
            finally:
                events.context.reset(token)
            if res is None:
                raise RuntimeError("no clip could be cut from this moment")
            m.clip, m.qc_passed, m.qc_summary = str(res.output), res.qc.passed, res.qc.summary()
            m.upload, m.upload_note = upload_decision(self.cfg, self.channel, m.qc_passed, m.clip)
            m.status = "done"
            events.publish("stage", stage="upload", status="done" if m.upload == "allowed" else "skipped",
                           moment=m.id, project=m.project, decision=m.upload, note=m.upload_note)
            log.info("clip ready: %s (%s; %s)", Path(m.clip).name, m.qc_summary, m.upload)
        except Exception as e:
            m.status, m.error = "failed", f"{type(e).__name__}: {e}"
            log.error("editing %s failed: %s", m.id, m.error)

    def _stream_over(self) -> bool:
        """Ask yt-dlp whether a dropped stream is still live (False when unsure, so we try to reconnect)."""
        from clipstudio.live.buffer import _ytdlp_cmd
        import subprocess

        try:
            cp = subprocess.run(_ytdlp_cmd() + ["--quiet", "--no-warnings", "--skip-download", "--print",
                                                "%(live_status)s", self.rec.url],
                                capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return False
        status = (cp.stdout.strip().splitlines() or [""])[0]
        return status in ("was_live", "not_live", "post_live")

    # ------------------------------------------------------------ main loop
    def _ingest(self, seg: Segment) -> None:
        self.ring.add(seg)
        try:
            self.det.add_audio(segment_loudness(seg.path, seg.start))
        except OSError as e:
            log.warning("could not read audio of %s: %s", seg.path.name, e)

    def run(self, poll: float = 0.25) -> list[Moment]:
        self.out.mkdir(parents=True, exist_ok=True)
        if self.process:
            self.worker = threading.Thread(target=self._worker, daemon=True, name="live-editor")
            self.worker.start()
        self.running, self.started_at = True, time.time()
        events.publish("stage", stage="live_record", status="running")
        self.rec.start()
        self.chat.start()
        if self.chat.name != "none":
            events.publish("stage", stage="live_chat", status="running")
        events.publish("stage", stage="live_trigger", status="running")
        t0 = time.time()
        try:
            while True:
                for seg in self.rec.poll():
                    self._ingest(seg)
                head = self.ring.head
                for msg in self.chat.poll(head):
                    t = msg.t if msg.stream_time else self.ring.stream_time_at(msg.t)
                    self.det.add_chat(t, msg.text)
                    self.chat_log.append((t, msg.user, msg.text))
                    if getattr(msg, "emotes", None):
                        self.emote_ids.update(msg.emotes)
                    self.chat_count += 1
                    self.recent_chat.append({"t": round(t, 1), "user": msg.user, "text": msg.text[:200]})
                for tr in self.det.evaluate(head):
                    self._on_trigger(tr)
                self._finish_pending()
                self.peak_disk = max(self.peak_disk, self.ring.disk_bytes())
                self.ring.prune()
                keep_from = head - max(self.lc.buffer_seconds, self.lc.baseline_seconds) - self.lc.pre_seconds
                self.det.forget_before(int(keep_from))
                self.chat_log = [c for c in self.chat_log if c[0] >= keep_from]
                if not self.rec.running:
                    # drain what ffmpeg finished writing
                    for seg in self.rec.poll():
                        self._ingest(seg)
                    err = self.rec.error_text()
                    if self.rec.segments_total == 0:
                        raise LiveError(f"recording never started: {err[-500:] or 'no video arrived'}")
                    if self.rec.url and self.rec.restarts < MAX_RECONNECTS and not self._stream_over():
                        log.warning("recorder stopped: %s", err[-300:] or "no error text")
                        try:
                            self.rec.restart()
                            continue
                        except MediaError as e:
                            err = str(e)
                    # judge the tail
                    for tr in self.det.evaluate(self.ring.head + 10) + self.det.flush():
                        self._on_trigger(tr)
                    self.stopped_reason = "stream ended" + (f": {err[-300:]}" if err else "")
                    break
                if self.max_seconds and time.time() - t0 >= self.max_seconds:
                    self.stopped_reason = f"time limit ({self.max_seconds:g}s)"
                    break
                if self._stop.is_set():
                    self.stopped_reason = "stopped from the workstation"
                    break
                if self.lc.max_moments and len(self.moments) >= self.lc.max_moments and not self.pending:
                    self.stopped_reason = f"{self.lc.max_moments} moment(s) saved"
                    break
                time.sleep(poll)
        except KeyboardInterrupt:
            self.stopped_reason = "stopped by user"
        finally:
            self.rec.stop()
            self.chat.stop()
            for seg in self.rec.poll():
                self.ring.add(seg)
            self._finish_pending(final=True)
            if self.worker is not None:
                self.jobs.put(None)
                self.worker.join()
            for s in self.ring.segments:  # nothing is kept but the moments
                s.path.unlink(missing_ok=True)
            self.save_queue()
            self.running = False
            for stage in ("live_record", "live_chat", "live_trigger"):
                events.publish("stage", stage=stage, status="done")
            log.info("live session ended (%s): %d moment(s), %d segment(s) discarded, peak buffer %.0f MB",
                     self.stopped_reason, len(self.moments), self.ring.deleted, self.peak_disk / 1e6)
        return self.moments
