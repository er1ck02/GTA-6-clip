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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipstudio.config import Config
from clipstudio.core import events
from clipstudio.core.logging import get_logger
from clipstudio.live.buffer import Recorder, Ring, Segment, concat_segments
from clipstudio.live.chat import ChatSource
from clipstudio.live.triggers import Trigger, TriggerDetector, segment_loudness

log = get_logger("live")


@dataclass
class Moment:
    id: str
    trigger: dict
    window: tuple[float, float]           # stream seconds asked for
    saved: tuple[float, float] = (0, 0)  # stream seconds actually saved (segment boundaries)
    path: str = ""
    chat_path: str | None = None
    channel: str | None = None
    status: str = "pending"               # pending | saved | processing | done | failed
    clip: str | None = None
    project: str | None = None
    qc_passed: bool | None = None
    qc_summary: str | None = None
    upload: str = "review"                # review | allowed | not_allowed
    upload_note: str = ""
    error: str | None = None
    created: float = field(default_factory=time.time)


def upload_decision(cfg: Config, channel: str | None, qc_passed: bool | None) -> tuple[str, str]:
    allowed = {c.lower().lstrip("@") for c in cfg.upload.allow_channels}
    if not channel or channel.lower() not in allowed:
        return "review", (f"{channel or 'this channel'} is not on upload.allow_channels: "
                          "review it and only post with the streamer's permission")
    if cfg.upload.require_qc_pass and not qc_passed:
        return "review", "QC did not pass: review before posting"
    return "allowed", f"{channel} allows clipping; ready to upload (connect accounts to publish)"


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
        self.jobs: queue.Queue[Moment | None] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.peak_disk = 0
        self.stopped_reason = ""
        self._qlock = threading.Lock()

    # ------------------------------------------------------------ queue file
    def save_queue(self) -> None:
        with self._qlock:  # the recorder loop and the editing worker both update it
            self.out.mkdir(parents=True, exist_ok=True)
            doc = {"channel": self.channel, "moments": [asdict(m) for m in self.moments]}
            tmp = self.out / "queue.json.tmp"
            tmp.write_text(json.dumps(doc, indent=2))
            tmp.replace(self.out / "queue.json")

    # ------------------------------------------------------------ capture
    def _on_trigger(self, tr: Trigger) -> None:
        if self.lc.max_moments and len(self.moments) >= self.lc.max_moments:
            return
        mid = f"m{len(self.moments) + 1:03d}_{int(tr.t)}s"
        a, b = max(0.0, tr.t - self.lc.pre_seconds), tr.t + self.lc.post_seconds
        m = Moment(mid, asdict(tr), (a, b), channel=self.channel)
        self.ring.pin(mid, a, b)
        self.moments.append(m)
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
            events.publish("live_moment", moment=m.id, status=m.status)
            self.save_queue()

    def _write_chat(self, m: Moment, video: Path) -> str | None:
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
        from clipstudio.pipeline.studio import Studio

        while True:
            m = self.jobs.get()
            if m is None:
                return
            m.status = "processing"
            self.save_queue()
            events.publish("live_moment", moment=m.id, status="processing")
            try:
                dur = m.saved[1] - m.saved[0]
                cfg = self.cfg.model_copy(deep=True)
                h = cfg.highlights
                h.minimum_score = 0.0  # the trigger already decided this moment is worth a clip
                h.maximum_duration = max(6.0, min(h.maximum_duration, dur - 1))
                h.minimum_duration = min(h.minimum_duration, h.maximum_duration * 0.6)
                h.target_duration = min(max(h.target_duration, h.minimum_duration), h.maximum_duration)
                st = Studio(cfg)
                p = st.open_or_create(input_path=Path(m.path), chat=Path(m.chat_path) if m.chat_path else None)
                st.analyze(p)
                ids = st.default_selection(p, 1)
                res = st.render(p, ids)[0] if ids else None
                m.project = p.id
                if res is None:
                    raise RuntimeError("no clip could be cut from this moment")
                m.clip, m.qc_passed, m.qc_summary = str(res.output), res.qc.passed, res.qc.summary()
                m.upload, m.upload_note = upload_decision(self.cfg, self.channel, m.qc_passed)
                m.status = "done"
                log.info("clip ready: %s (%s; %s)", Path(m.clip).name, m.qc_summary, m.upload)
            except Exception as e:
                m.status, m.error = "failed", f"{type(e).__name__}: {e}"
                log.error("editing %s failed: %s", m.id, m.error)
            self.save_queue()
            events.publish("live_moment", moment=m.id, status=m.status, clip=m.clip, upload=m.upload)

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
        self.rec.start()
        self.chat.start()
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
                for tr in self.det.evaluate(head):
                    self._on_trigger(tr)
                self._finish_pending()
                self.peak_disk = max(self.peak_disk, self.ring.disk_bytes())
                self.ring.prune()
                keep_from = head - max(self.lc.buffer_seconds, self.lc.baseline_seconds) - self.lc.pre_seconds
                self.det.forget_before(int(keep_from))
                self.chat_log = [c for c in self.chat_log if c[0] >= keep_from]
                if not self.rec.running:
                    # drain what ffmpeg finished writing, then judge the tail
                    for seg in self.rec.poll():
                        self._ingest(seg)
                    for tr in self.det.evaluate(self.ring.head + 10) + self.det.flush():
                        self._on_trigger(tr)
                    err = self.rec.error_text()
                    self.stopped_reason = "stream ended" + (f": {err[-300:]}" if err else "")
                    break
                if self.max_seconds and time.time() - t0 >= self.max_seconds:
                    self.stopped_reason = f"time limit ({self.max_seconds:g}s)"
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
            log.info("live session ended (%s): %d moment(s), %d segment(s) discarded, peak buffer %.0f MB",
                     self.stopped_reason, len(self.moments), self.ring.deleted, self.peak_disk / 1e6)
        return self.moments
