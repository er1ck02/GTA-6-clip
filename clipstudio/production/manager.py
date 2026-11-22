"""The production agent inside the server: resumes the lineup, stands in for offline channels, keeps the PC
awake, and on stop drains (or shelves) every in-flight edit, VOD analysis and upload before saying it is safe.

State lives in ``<data_dir>/production/state.json`` so the next start knows what was being watched and what was
left unfinished, even after a crash.
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from clipstudio.config import Config
from clipstudio.core import events
from clipstudio.core.compat import write_text_atomic
from clipstudio.core.errors import ClipStudioError
from clipstudio.core.logging import get_logger
from clipstudio.production import lineup as L

log = get_logger("production")

EDITABLE = ("saved", "queued", "processing")   # moment states that still need an edit after a stop or crash
MAX_ATTEMPTS = 2                               # a failed edit is retried once (e.g. after a fix lands)


class KeepAwake:
    """Ask Windows not to sleep while production runs. The request belongs to the thread that made it, so the
    manager's own loop thread calls ``want()`` every tick. Elsewhere this does nothing."""
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

    def __init__(self) -> None:
        self.active = False

    def want(self, on: bool) -> None:
        if on == self.active:
            return
        if sys.platform == "win32":
            try:
                import ctypes

                flags = self.ES_CONTINUOUS | (self.ES_SYSTEM_REQUIRED if on else 0)
                ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                log.warning("could not change the sleep setting: %s", e)
                return
        self.active = on


class ProductionManager:
    def __init__(self, cfg: Config, live, jobs=None, uploads=None,
                 analyze: Callable[[str], Any] | None = None, check: L.Probe | None = None,
                 tick: float = 5.0, autostart_loop: bool = True):
        self.cfg, self.pc = cfg, cfg.production
        self.live, self.jobs, self.uploads, self.analyze = live, jobs, uploads, analyze
        self.check = check or L.probe
        self.data_dir = Path(cfg.paths.data_dir).expanduser().resolve()
        self.dir = self.data_dir / "production"
        self.path = self.dir / "state.json"
        self.tick = tick
        self.lock = threading.RLock()
        self.state = self._load()
        self.slots: list[dict] = []          # what each lineup entry is doing right now
        self.ours: set[str] = set()          # watch keys production started (vs. added by hand)
        self.message = ""
        self.stop_mode: str | None = None
        self.stop_started = 0.0
        self.stop_estimate = 0.0
        self.backlog_thread: threading.Thread | None = None
        self.backlog_left = 0
        self.backlog_abort = threading.Event()
        self.edit_times: deque[float] = deque(maxlen=30)
        self._edit_t0: dict[tuple, float] = {}
        self._next_recheck = 0.0
        self._planning = False
        self.awake = KeepAwake()
        for ch in self.state.get("posted_stand_ins", []) if self.pc.post_stand_ins else []:
            self._allow(ch)
        self._unsub = events.subscribe(self._on_event)
        if self.state.get("mode") in ("running", "draining"):
            # the server stopped without a clean stop (crash, window closed): remember that, start nothing
            self.state["mode"] = "interrupted"
            self.state["interrupted_at"] = time.time()
            self.save()
        self._quit = threading.Event()
        if autostart_loop:
            threading.Thread(target=self._loop, daemon=True, name="clipstudio-production").start()

    # ------------------------------------------------------------ state file
    def _load(self) -> dict:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                return doc
        except (OSError, ValueError):
            pass
        return {"mode": "idle", "lineup": [], "known": {}, "backlog_vods": []}

    def save(self) -> None:
        with self.lock:
            self.state["slots"] = self.slots
            self.state["saved_at"] = time.time()
            write_text_atomic(self.path, json.dumps(self.state, indent=2, default=str))

    @property
    def mode(self) -> str:
        return self.state.get("mode", "idle")

    def _set_mode(self, mode: str, msg: str = "") -> None:
        self.state["mode"], self.state["mode_since"] = mode, time.time()
        if msg:
            self.message = msg
        events.publish("production", mode=mode, message=msg)
        self.save()

    # ------------------------------------------------------------ edit timing (for the countdown)
    def _on_event(self, ev: dict) -> None:
        if ev.get("kind") != "live_moment":
            return
        key = (ev.get("channel"), ev.get("moment"))
        st = ev.get("status")
        if st == "processing":
            self._edit_t0.setdefault(key, ev.get("ts") or time.time())
        elif st in ("done", "failed", "skipped") and key in self._edit_t0:
            self.edit_times.append(max(1.0, (ev.get("ts") or time.time()) - self._edit_t0.pop(key)))

    def avg_edit_seconds(self) -> float:
        if self.edit_times:
            return sum(self.edit_times) / len(self.edit_times)
        return float(self.state.get("avg_edit_seconds") or self.pc.edit_seconds_estimate)

    # ------------------------------------------------------------ what is being watched
    def _watches(self) -> list:
        return list(getattr(self.live, "watches", {}).values())

    def _remember(self, channel: str | None, url: str | None) -> None:
        if not channel:
            return
        u = L.resume_url(url, channel)
        k = self.state.setdefault("known", {})
        rec = k.setdefault(channel, {})
        if u:
            rec["url"] = u
        rec["last_seen"] = time.time()

    def _allow(self, channel: str) -> None:
        """production.post_stand_ins: a stand-in's clips post like the lineup's (credited), so add it to the
        upload allowlist for this run and remember it for the next one."""
        ch = channel.lower().lstrip("@")
        if ch not in {c.lower().lstrip("@") for c in self.cfg.upload.allow_channels}:
            self.cfg.upload.allow_channels.append(ch)
        posted = self.state.setdefault("posted_stand_ins", [])
        if ch not in posted:
            posted.append(ch)

    def _running(self) -> dict[str, Any]:
        """channel -> watch, for streams recording right now."""
        out = {}
        for w in self._watches():
            if w.running and w.session.channel and not w.session.stopped_reason:  # recording, not just editing
                out[w.session.channel] = w
        return out

    def history(self) -> list[L.PastChannel]:
        return L.rank(L.scan_history(self.data_dir / "live", self.state.get("known")), self.cfg.upload.allow_channels)

    # ------------------------------------------------------------ start
    def start(self, channels: list[str] | None = None, resume_backlog: bool = True) -> dict:
        """Re-engage production: resume the last lineup (or ``channels``), stand in for offline channels, edit
        whatever the last stop left unedited. Safe to call again while running (it just re-plans)."""
        with self.lock:
            if self.mode == "draining":
                raise ClipStudioError("production is still stopping; wait until it says it's safe, then start again")
            if channels:
                self.state["lineup"] = [{"channel": L.channel_of(u) or u, "url": u} for u in channels]
            running = self._running()
            for ch, w in running.items():  # streams already being watched join the lineup as they are
                self._remember(ch, w.session.rec.url)
                if not any(e.get("channel") == ch for e in self.state["lineup"]):
                    self.state["lineup"].append({"channel": ch, "url": L.resume_url(w.session.rec.url, ch)})
            if not self.state["lineup"]:
                self.state["lineup"] = self._first_lineup()
            self.stop_mode = None
            self.backlog_abort.clear()
            self._set_mode("running", "starting production")
        if self.uploads is not None and hasattr(self.uploads, "start"):
            try:
                self.uploads.start()
            except Exception as e:  # noqa: BLE001 - already running, or not signed in yet
                log.debug("uploads.start: %s", e)
        plans = self._apply_plan()
        if resume_backlog:
            self._resume_backlog()
        self._next_recheck = time.time() + self.pc.recheck_minutes * 60
        live_n = sum(p["action"] in ("watch", "substitute", "keep", "fill") for p in plans)
        msg = f"production running: {live_n} stream(s) recording"
        subs = [p for p in plans if p["action"] in ("substitute", "fill")]
        if subs:
            msg += f", {len(subs)} stand-in(s)"
        if self.backlog_left:
            msg += f", finishing {self.backlog_left} clip(s) saved from last time"
        self.message = msg
        self.save()
        return {**self.status(), "plan": plans}

    def _first_lineup(self) -> list[dict]:
        """No saved lineup yet: live.channels from the config, else your top past streamers."""
        if self.cfg.live.channels:
            return [{"channel": L.channel_of(u) or u, "url": u} for u in self.cfg.live.channels]
        top = self.history()[: self.pc.default_streams]
        return [{"channel": c.channel, "url": c.urls[0] if c.urls else None} for c in top]

    def _idle(self, r: L.LiveCheck) -> str:
        return L.idle_reason(r, self.pc.idle_keywords, self.pc.substitute_keywords)

    def _discover(self) -> list[L.LiveCheck]:
        return L.discover_live(self.pc.discover_channels, self.check, self.pc.discover_keywords, idle=self._idle)

    def _drop_idle(self) -> bool:
        """Streams recording right now that turned into a sleep/AFK/Just Chatting (or non-GTA) stream are stopped;
        their slot waits for the original to be back for real, and a stand-in takes over meanwhile."""
        slots = [s for s in self._recording_slots() if s.get("url")]
        if not slots:
            return False
        with ThreadPoolExecutor(max_workers=8) as ex:
            res = list(ex.map(lambda s: self.check(s["url"]), slots))
        changed = False
        for s, r in zip(slots, res):
            why = self._idle(r) if r.live else ""
            if not why:
                continue
            log.info("%s is on a %s; swapping it out", s["channel"], why)
            key = s["key"]
            with self.lock:
                if s["action"] == "fill":
                    self.slots.remove(s)
                else:
                    s["action"], s["key"] = "wait", None
                    s["note"] = f"{s['channel']} is {'on a ' + why if why.endswith('stream') else why}; swapped out"
            try:
                self.live.stop(key)
            except ClipStudioError:
                pass
            changed = True
        return changed

    def _recording_slots(self) -> list[dict]:
        running = set(self._running().values())
        return [s for s in self.slots if s.get("key") and self.live.watches.get(s["key"]) in running]

    def _apply_plan(self) -> list[dict]:
        """Check every lineup channel, start the live ones, stand someone in for the rest, and keep
        ``target_streams`` recording. Stand-ins that are still recording keep their slot."""
        if self._planning:
            return self.slots
        self._planning = True
        try:
            running = self._running()
            held = [s for s in self._recording_slots() if s["action"] in ("substitute", "fill")]
            held_wanted = {s["wanted"] for s in held if s["action"] == "substitute"}
            lineup = [e for e in self.state["lineup"]
                      if (e.get("channel") or L.channel_of(e.get("url") or "") or "").lower() not in held_wanted]
            target = self.pc.target_streams
            plans = L.plan_lineup(lineup, self.history(), self.check,
                                  substitute=self.pc.substitute, keywords=self.pc.substitute_keywords,
                                  max_candidates=self.pc.max_candidates, busy=set(running),
                                  target=max(0, target - len(held)) if target else 0,
                                  discover=self._discover if target else None, idle=self._idle)
            if self.mode != "running":  # a stop arrived while we were checking
                return [p.to_dict() for p in plans]
            if target:  # an original came back: make room by stopping extra streams, newest first
                over = len(held) + sum(p.action in ("watch", "keep", "substitute", "fill") for p in plans) - target
                for s in sorted(held, key=lambda s: (s["action"] != "fill", -s.get("started_at", 0))):
                    if over <= 0:
                        break
                    log.info("stopping stand-in %s to keep %d streams", s["channel"], target)
                    try:
                        self.live.stop(s["key"])
                    except ClipStudioError:
                        pass
                    held.remove(s)
                    if s["action"] == "substitute":  # its lineup channel was skipped above; list it as waiting
                        plans.append(L.SlotPlan(s["wanted"], s.get("wanted_url"), "wait",
                                                note=f"{s['wanted']} is offline; {target} streams are already recording"))
                    over -= 1
            slots = []
            for p in plans:
                d = p.to_dict()
                if p.action in ("watch", "substitute", "fill"):
                    try:
                        key = self.live.start(p.url, None, p.channel, None, 0)["id"]
                        self.ours.add(key)
                        self._remember(p.channel, p.url)
                        d["key"], d["started_at"] = key, time.time()
                        if p.action in ("substitute", "fill") and self.pc.post_stand_ins:
                            self._allow(p.channel)
                    except ClipStudioError as e:
                        if p.action == "fill":
                            continue
                        d["action"], d["note"] = "wait", f"could not start {p.channel}: {e}"
                elif p.action == "keep":
                    w = running.get(p.channel)
                    d["key"] = w.key if w else None
                slots.append(d)
                log.info("lineup %s: %s %s", p.wanted, d["action"], d["note"])
            order = {e.get("channel"): i for i, e in enumerate(self.state["lineup"])}
            slots = sorted(held + slots, key=lambda d: (d["action"] == "fill", order.get(d["wanted"], 1 << 20)))
            with self.lock:
                self.slots = slots
                self.save()
            return slots
        finally:
            self._planning = False

    # ------------------------------------------------------------ loop: follow the streams
    def _loop(self) -> None:
        while not self._quit.wait(self.tick):
            try:
                self.awake.want(self.pc.keep_awake and self.mode in ("running", "draining"))
                if self.mode == "running":
                    self._follow()
                elif self.mode == "draining":
                    self._check_drained()
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                log.error("production loop: %s", e)
        self.awake.want(False)

    def _follow(self) -> None:
        """Keep the lineup in step with reality: streams added or stopped by hand, streams that went offline,
        originals that came back. Re-plans when something changed, and every ``recheck_minutes``."""
        changed = False
        with self.lock:
            lineup = self.state["lineup"]
            names = {e["channel"] for e in lineup}
            slot_by_key = {s.get("key"): s for s in self.slots if s.get("key")}
            for w in self._watches():
                ch = w.session.channel
                if w.key in self.ours or w.key in slot_by_key or not ch or ch in names:
                    continue
                if w.running and not w.session.stopped_reason:
                    lineup.append({"channel": ch, "url": L.resume_url(w.session.rec.url, ch)})  # added by hand
                    self.slots.append({"wanted": ch, "wanted_url": w.session.rec.url, "action": "keep",
                                       "channel": ch, "url": w.session.rec.url, "key": w.key,
                                       "note": "added from the workstation"})
                    self._remember(ch, w.session.rec.url)
                    names.add(ch)
                    changed = True
            for s in list(self.slots):
                w = self.live.watches.get(s.get("key")) if s.get("key") else None
                if w is None or (w.running and not w.session.stopped_reason):
                    continue  # still recording
                why = w.session.stopped_reason or w.error or "stopped"
                if "workstation" in why:  # you stopped it by hand: drop it from the lineup
                    self.state["lineup"] = [e for e in self.state["lineup"] if e["channel"] != s["wanted"]]
                    self.slots.remove(s)
                    changed = True
                elif s["action"] == "fill":  # an extra stream ended: a fresh pick takes its place
                    self.slots.remove(s)
                    changed = True
                else:
                    s["action"], s["key"], s["note"] = "wait", None, f"{s['channel']} went offline ({why[:80]})"
                    changed = True
            if changed:
                self.save()
        due = time.time() >= self._next_recheck
        if due and self.pc.idle_keywords and self._drop_idle():
            changed = True
        needs = (any(s["action"] in ("wait", "substitute", "fill") for s in self.slots)
                 or (self.pc.target_streams and len(self._recording_slots()) < self.pc.target_streams))
        if (changed or due) and needs:
            self._next_recheck = time.time() + self.pc.recheck_minutes * 60
            self._swap_back()
            self._apply_plan()
        elif due:
            self._next_recheck = time.time() + self.pc.recheck_minutes * 60
            self.save()

    def _swap_back(self) -> None:
        """An original that is live again takes its slot back from its stand-in."""
        for s in self.slots:
            if s["action"] != "substitute" or not s.get("key"):
                continue
            r = L.first_live([u for u in [L.resume_url(s.get("wanted_url"), s["wanted"])] if u]
                             + L.guess_urls(s["wanted"]), self.check)
            if r.live and self._idle(r):
                continue  # live, but still asleep/AFK: the stand-in stays
            if r.live and self.pc.target_streams:
                # the original gets its slot back; the stand-in keeps recording as an extra stream unless that
                # would go over target_streams (then _apply_plan stops it)
                log.info("%s is live again; %s becomes an extra stream", s["wanted"], s["channel"])
                s.update(action="fill", wanted=s["channel"], wanted_url=s.get("url"),
                         note=f"keeping {self.pc.target_streams} streams going: {s['channel']} stays on")
            elif r.live:
                log.info("%s is live again; stopping stand-in %s", s["wanted"], s["channel"])
                try:
                    self.live.stop(s["key"])
                except ClipStudioError:
                    pass
                s["action"], s["key"] = "wait", None  # _apply_plan starts the original

    # ------------------------------------------------------------ backlog: what the last stop left unedited
    def backlog(self) -> list[tuple[Path, dict]]:
        """(session folder, moment) for every saved moment that was never edited, newest sessions first."""
        active = {Path(w.session.out).resolve() for w in self._watches() if w.running}
        cutoff = time.time() - self.pc.backlog_max_age_hours * 3600 if self.pc.backlog_max_age_hours else 0
        out = []
        live_dir = self.data_dir / "live"
        if not live_dir.is_dir():
            return out
        for d in sorted(live_dir.iterdir(), key=lambda p: p.name, reverse=True):
            if not d.is_dir() or d.resolve() in active:
                continue
            try:
                doc = json.loads((d / "queue.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for m in doc.get("moments") or []:
                retry = m.get("status") == "failed" and int(m.get("attempts") or 0) < MAX_ATTEMPTS
                if ((m.get("status") in EDITABLE or retry) and not m.get("clip") and m.get("path")
                        and Path(m["path"]).is_file() and float(m.get("created") or 0) >= cutoff):
                    out.append((d, m))
        return out

    def _resume_backlog(self) -> None:
        todo = self.backlog()
        vods = list(self.state.get("backlog_vods") or [])
        if vods and self.jobs is not None and self.analyze is not None:
            for pid in vods:
                self.jobs.submit("analyze", pid, None, lambda p=pid: self.analyze(p))
            log.info("resumed %d VOD analysis job(s) from the last stop", len(vods))
            self.state["backlog_vods"] = []
        if not todo or (self.backlog_thread and self.backlog_thread.is_alive()):
            return
        self.backlog_left = len(todo)
        self.backlog_thread = threading.Thread(target=self._edit_backlog, args=(todo,), daemon=True,
                                               name="clipstudio-backlog")
        self.backlog_thread.start()

    def _edit_backlog(self, todo: list[tuple[Path, dict]]) -> None:
        from clipstudio.live.session import LiveSession, Moment, edit_gate

        by_dir: dict[Path, list[str]] = {}
        for d, m in todo:
            by_dir.setdefault(d, []).append(m["id"])
        for d, ids in by_dir.items():
            try:
                doc = json.loads((d / "queue.json").read_text(encoding="utf-8"))
                s = LiveSession(self.cfg, None, None, d, channel=doc.get("channel"))  # type: ignore[arg-type]
                fields = set(Moment.__dataclass_fields__)
                s.moments = [Moment(**{k: v for k, v in x.items() if k in fields}) for x in doc.get("moments") or []]
            except Exception as e:  # noqa: BLE001
                log.error("could not reopen %s: %s", d.name, e)
                self.backlog_left -= len(ids)
                continue
            for m in s.moments:
                if m.id not in ids:
                    continue
                if self.backlog_abort.is_set():
                    return
                with edit_gate(self.cfg.live.max_parallel_edits):
                    if self.backlog_abort.is_set():
                        return
                    m.saved = tuple(m.saved)  # type: ignore[assignment]
                    m.status, m.error = "processing", None
                    s.save_queue()
                    events.publish("live_moment", channel=s.channel, moment=m.id, status="processing")
                    s._edit(m)
                    s.save_queue()
                    events.publish("live_moment", channel=s.channel, moment=m.id, status=m.status, clip=m.clip,
                                   upload=m.upload)
                self.backlog_left = max(0, self.backlog_left - 1)
                log.info("backlog %s/%s: %s", d.name, m.id, m.status)

    # ------------------------------------------------------------ stop
    def in_flight(self) -> dict:
        rec = edit = wait = 0
        streams = 0
        for w in self._watches():
            if not w.running:
                continue
            streams += 1
            rec += int(bool(w.session.running))
            for m in list(w.session.moments):
                if m.status == "processing":
                    edit += 1
                elif m.status in ("pending", "saved", "queued") and not (
                        m.status == "saved" and w.session._abandon.is_set()):
                    wait += 1
        vod_run = vod_wait = 0
        if self.jobs is not None:
            for j in self.jobs.list():
                if j["kind"] == "analyze":
                    vod_run += j["status"] == "running"
                    vod_wait += j["status"] == "queued"
        uploading = upload_wait = 0
        if self.uploads is not None:
            for j in list(getattr(self.uploads, "jobs", [])):
                uploading += getattr(j, "status", "") == "uploading"
        backlog = self.backlog_left if self.backlog_thread and self.backlog_thread.is_alive() else 0
        return {"streams": streams, "recording": rec, "editing": edit, "waiting": wait, "backlog": backlog,
                "vod_running": vod_run, "vod_queued": vod_wait, "uploading": uploading,
                "upload_queued": upload_wait}

    def estimate(self, f: dict | None = None) -> float:
        """Seconds until everything in flight is finished (or shelved, for a quick stop)."""
        f = f or self.in_flight()
        avg = self.avg_edit_seconds()
        slots = max(1, self.cfg.live.max_parallel_edits)
        edits = f["editing"] * 0.5 + f["waiting"] + f["backlog"]
        if self.stop_mode == "now":
            edits = f["editing"] * 0.5
        secs = math.ceil(edits / slots) * avg if edits else 0.0
        secs += f["vod_running"] * self.pc.vod_seconds_estimate * 0.5
        if self.stop_mode != "now":
            secs += f["vod_queued"] * self.pc.vod_seconds_estimate
        secs += f["uploading"] * 60
        secs += 5 if f["recording"] else 0
        return round(secs)

    def busy(self, f: dict | None = None) -> bool:
        f = f or self.in_flight()
        if self.stop_mode == "now":
            return bool(f["streams"] or f["vod_running"] or f["uploading"] or f["backlog"])
        return any(f[k] for k in ("streams", "editing", "waiting", "backlog", "vod_running", "vod_queued",
                                  "uploading"))

    def stop(self, mode: str = "safe") -> dict:
        """``safe``: stop recording, finish every saved moment, VOD and upload, then say it's safe.
        ``now``: stop recording, finish only what is mid-edit; everything else is saved for the next start."""
        if mode not in ("safe", "now"):
            raise ClipStudioError("stop mode is 'safe' or 'now'")
        with self.lock:
            if self.mode in ("idle", "stopped", "interrupted") and not self.busy():
                self._snapshot_lineup()
                self._set_mode("stopped", "nothing was running; safe to use your PC")
                return self.status()
            if self.stop_mode == "now" and mode == "safe":
                mode = "now"  # can't un-shelve
            self.stop_mode = mode
            if self.mode != "draining":
                self.stop_started = time.time()
                self._snapshot_lineup()
            self._set_mode("draining")
        for w in self._watches():
            if w.running:
                (w.session.abandon if mode == "now" else w.session.stop)()
        if mode == "now":
            self.backlog_abort.set()
            self._shelve_vod_queue()
        if self.uploads is not None and hasattr(self.uploads, "stop"):
            try:
                self.uploads.stop()  # no new uploads start; one already sending finishes (uploads resume anyway)
            except Exception as e:  # noqa: BLE001
                log.warning("could not pause uploads: %s", e)
        f = self.in_flight()
        self.stop_estimate = self.estimate(f)
        self.message = self._wait_message(f)
        self.save()
        self._check_drained()
        return self.status()

    def _snapshot_lineup(self) -> None:
        """Remember the channels that were wanted (originals, not stand-ins) for the next start."""
        for w in self._watches():
            if w.running and w.session.channel:
                self._remember(w.session.channel, w.session.rec.url)
        self.state["last_lineup"] = list(self.state.get("lineup") or [])

    def _shelve_vod_queue(self) -> None:
        """Pull VOD analyses that haven't started out of the job queue; the next start runs them."""
        if self.jobs is None:
            return
        import queue as _q

        kept, shelved = [], []
        while True:
            try:
                item = self.jobs.q.get_nowait()
            except _q.Empty:
                break
            job, _fn = item
            if job["kind"] == "analyze" and job["project"]:
                job["status"], job["error"] = "failed", "saved for the next production start"
                shelved.append(job["project"])
            else:
                kept.append(item)
        for item in kept:
            self.jobs.q.put(item)
        if shelved:
            self.state["backlog_vods"] = sorted(set(self.state.get("backlog_vods") or []) | set(shelved))

    def _wait_message(self, f: dict) -> str:
        if not self.busy(f):
            return "safe to use your PC: everything is finished and saved"
        mins = max(1, math.ceil(self.estimate(f) / 60))
        parts = []
        if f["editing"]:
            parts.append(f"{f['editing']} clip(s) editing")
        if self.stop_mode != "now" and f["waiting"]:
            parts.append(f"{f['waiting']} waiting to edit")
        if self.stop_mode != "now" and f["backlog"]:
            parts.append(f"{f['backlog']} from last time")
        if f["vod_running"] or (self.stop_mode != "now" and f["vod_queued"]):
            parts.append(f"{f['vod_running'] + (f['vod_queued'] if self.stop_mode != 'now' else 0)} VOD(s)")
        if f["uploading"]:
            parts.append(f"{f['uploading']} upload(s) sending")
        if f["recording"]:
            parts.append("streams closing")
        return f"please wait about {mins} min: " + ", ".join(parts or ["finishing up"])

    def _check_drained(self) -> None:
        if self.mode != "draining":
            return
        f = self.in_flight()
        if self.busy(f):
            self.message = self._wait_message(f)
            return
        took = time.time() - self.stop_started
        shelved = len(self.backlog()) + len(self.state.get("backlog_vods") or [])
        if self.edit_times:
            self.state["avg_edit_seconds"] = round(self.avg_edit_seconds(), 1)
        self.state["last_stop"] = {"at": time.time(), "mode": self.stop_mode, "seconds": round(took),
                                   "saved_for_next_start": shelved}
        msg = "safe to use your PC: everything is finished and saved"
        if shelved:
            msg += f" ({shelved} item(s) will be finished on the next start)"
        self.slots = [{**s, "action": "stopped", "key": None} for s in self.slots]
        self._set_mode("stopped", msg)
        self.awake.want(False)
        log.info("production stopped after %.0fs; %s", took, msg)

    # ------------------------------------------------------------ status
    def status(self) -> dict:
        f = self.in_flight()
        eta = self.estimate(f) if self.mode == "draining" else 0
        if self.mode == "draining":
            self.message = self._wait_message(f)
        hist = [{"channel": c.channel, "clips": c.clips, "moments": c.moments, "sessions": c.sessions,
                 "urls": c.urls[:1]} for c in self.history()[:10]]
        bl = self.backlog() if self.mode != "running" else []
        return {"mode": self.mode, "since": self.state.get("mode_since"), "message": self.message,
                "safe": self.mode in ("idle", "stopped", "interrupted") and not self.busy(f),
                "stop_mode": self.stop_mode, "eta_seconds": eta,
                "stop_elapsed": round(time.time() - self.stop_started) if self.mode == "draining" else None,
                "in_flight": f, "lineup": self.state.get("lineup", []), "slots": self.slots,
                "avg_edit_seconds": round(self.avg_edit_seconds(), 1), "keep_awake": self.awake.active,
                "backlog_saved": len(bl) + len(self.state.get("backlog_vods") or []),
                "last_stop": self.state.get("last_stop"), "interrupted_at": self.state.get("interrupted_at"),
                "top_streamers": hist, "state_file": str(self.path)}

    def close(self) -> None:
        self._quit.set()
        self._unsub()
