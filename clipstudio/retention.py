"""Disk retention: keep only the footage that can still turn into a post.

Live mode saves a ~65 MB recording for every trigger, and most triggers are judged weak and skipped. Before this
module those recordings, the leftover ring-buffer segments, every rejected render and every analysis WAV stayed on
disk forever (150 GB in four days). The rules, all tunable under ``retention:`` in the config:

1. Raw recordings (``live/<session>/moments/*.mp4``) go once the moment is settled: a skipped or failed moment
   after ``skipped_keep_hours``; an edited one as soon as none of its clips is waiting for review or kept as an
   approved clip. The small ``.chat.json`` next to it stays.
2. Ring-buffer segments left in ``live/<session>/buffer`` by a session that is no longer recording.
3. Renders: a rejected clip's videos, and renders that never got a review packet once they are
   ``unreviewed_max_age_hours`` old.
4. Intermediates of settled projects: ``analysis/*.wav``, ``clips/*/*.mov`` (chat overlay), ``source/*.part``.
5. Approved clips. Posted ones (``approved/posted.jsonl``, or ``instagram/posted.log``) stay ``posted_keep_days``
   after their first post (cross-posting and the weekly top 5 use them), then the approved copy and the render
   go. Unposted ones are capped at ``backlog_days * clips_per_day``, best first (review score, plus
   ``priority_bonus`` for production's priority channels such as IShowSpeed); the rest are retired.

Never touched: anything a ``formats/`` spec points at, anything under ``formats/``, files outside the data folder,
and the buffer of a session that is still recording. ``plan()`` only lists what would go; ``apply()`` deletes.
Only local files are deleted: nothing here talks to YouTube, TikTok or Instagram.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Iterable

from clipstudio.core.logging import get_logger

log = get_logger("retention")

VIDEO = {".mp4", ".mov", ".mkv", ".ts", ".webm", ".flv", ".m4v"}
SETTLED_MOMENT = ("skipped", "failed")
LEDGER = "posted.jsonl"


@dataclasses.dataclass
class Action:
    path: Path
    size: int
    reason: str


@dataclasses.dataclass
class Plan:
    actions: list[Action] = dataclasses.field(default_factory=list)
    retire: list[tuple[Path, str]] = dataclasses.field(default_factory=list)   # (review.json, reason)
    kept_backlog: list[str] = dataclasses.field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(a.size for a in self.actions)

    def summary(self) -> dict:
        by: dict[str, list[int]] = {}
        for a in self.actions:
            s = by.setdefault(a.reason, [0, 0])
            s[0] += 1
            s[1] += a.size
        return {"files": len(self.actions), "bytes": self.bytes, "gb": round(self.bytes / 1e9, 2),
                "by_reason": {k: {"files": v[0], "gb": round(v[1] / 1e9, 2)} for k, v in by.items()},
                "retired": len(self.retire), "backlog_kept": self.kept_backlog}


# ------------------------------------------------------------------ posted ledger
def mark_posted(data_dir: Path, file: str, platform: str, url: str = "", t: float | None = None) -> None:
    """Record that an approved clip went up on a platform (the uploader or whoever posts calls this)."""
    out = Path(data_dir) / "approved"
    out.mkdir(parents=True, exist_ok=True)
    row = {"file": Path(file).name, "platform": platform, "url": url, "t": t or time.time()}
    with open(out / LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def first_posted(data_dir: Path) -> dict[str, float]:
    """File name -> time of its first post anywhere, from the ledger and the Instagram log."""
    seen: dict[str, float] = {}

    def add(name: str, t: float) -> None:
        if name and (name not in seen or t < seen[name]):
            seen[name] = t

    led = Path(data_dir) / "approved" / LEDGER
    if led.exists():
        for line in led.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
                add(Path(row["file"]).name, float(row.get("t") or 0))
            except (ValueError, KeyError, TypeError):
                continue
    ig = Path(data_dir) / "instagram" / "posted.log"
    if ig.exists():   # "2026-11-24T17:45Z | file.mp4 | caption | url"
        for line in ig.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 2 and parts[1].lower().endswith(".mp4"):
                add(parts[1], _iso(parts[0]) or time.time())   # unreadable time: count it as posted just now
    return seen


def _iso(s: str) -> float:
    import datetime as dt
    for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            pass
    return 0.0


# ------------------------------------------------------------------ planning
def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _age_h(p: Path, now: float) -> float:
    try:
        return (now - p.stat().st_mtime) / 3600
    except OSError:
        return 0.0


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def protected_paths(data_dir: Path) -> set[Path]:
    """Every media file a formats spec (top 5, news, ...) points at: those are rebuilt from the originals."""
    out: set[Path] = set()

    def walk(v) -> None:
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and Path(v).suffix.lower() in VIDEO:
            out.add(_norm(Path(v)))

    for spec in (Path(data_dir) / "formats").rglob("*.json"):
        doc = _read(spec)
        if doc is not None:
            walk(doc)
    return out


def _norm(p: Path) -> Path:
    try:
        return p.expanduser().resolve()
    except OSError:
        return p


def plan(cfg, data_dir: Path, now: float | None = None, active_sessions: Iterable[Path] = ()) -> Plan:
    rc = cfg.retention
    now = now or time.time()
    data_dir = _norm(Path(data_dir))
    keep = protected_paths(data_dir)
    active = {_norm(Path(p)) for p in active_sessions}
    pl = Plan()
    seen: set[Path] = set()

    def drop(p: Path, reason: str) -> None:
        p = _norm(p)
        if p in seen or p in keep or not p.is_file():
            return
        if data_dir not in p.parents or (data_dir / "formats") in p.parents:
            return
        seen.add(p)
        pl.actions.append(Action(p, _size(p), reason))

    posted = first_posted(data_dir)
    approved_dir = data_dir / "approved"

    # --- clips: what each review packet says
    settled: dict[str, bool] = {}          # project id -> nothing in it can still be posted or reviewed
    backlog: list[tuple[float, float, Path, dict]] = []
    unblock: dict[str, int] = {}           # project id -> unposted approved clips (decided by the backlog cap)
    blocked: set[str] = set()              # projects held by something other than the backlog
    for proj in sorted((data_dir / "projects").glob("*")):
        if not proj.is_dir():
            continue
        done, in_backlog = True, False
        for cd in sorted((proj / "clips").glob("*")):
            if not cd.is_dir():
                continue
            vids = [f for f in cd.iterdir() if f.suffix.lower() in VIDEO]
            meta = cd / "review.json"
            doc = _read(meta) if meta.exists() else None
            if doc is None:
                if vids and all(_age_h(v, now) >= rc.unreviewed_max_age_hours for v in vids):
                    for v in vids:
                        drop(v, "render never reviewed")
                elif vids:
                    done = False
                continue
            status = doc.get("status")
            render = Path(doc.get("clip") or "")
            if status == "pending":
                done = False
            elif status in ("rejected", "retired") or doc.get("retention_deleted"):
                for v in vids:
                    drop(v, "rejected render" if status == "rejected" else "retired render")
            elif status == "approved":
                t_post = posted.get(render.name)
                if t_post is not None:
                    if (now - t_post) / 86400 >= rc.posted_keep_days:
                        for v in vids:
                            drop(v, "posted clip (render)")
                        drop(approved_dir / render.name, "posted clip (approved copy)")
                    else:
                        done = False
                else:
                    bonus = rc.priority_bonus if str(doc.get("channel") or "").lower() in \
                        {c.lower() for c in cfg.production.priority_channels} else 0.0
                    backlog.append((float(doc.get("score") or 0) + bonus, float(doc.get("reviewed") or 0), meta, doc))
                    unblock[proj.name] = unblock.get(proj.name, 0) + 1
                    in_backlog = True
            # chat overlay intermediates are never needed once the clip is rendered
            for v in vids:
                if v.suffix.lower() == ".mov":
                    drop(v, "render intermediate")
        settled[proj.name] = done and not in_backlog
        if not done:
            blocked.add(proj.name)

    # --- unposted approved backlog: best first, at most N
    cap = int(round(rc.backlog_days * rc.clips_per_day))
    backlog.sort(key=lambda r: (r[0], r[1]), reverse=True)
    for i, (score, _, meta, doc) in enumerate(backlog):
        render = Path(doc.get("clip") or "")
        if i < cap:
            pl.kept_backlog.append(render.name)
            continue
        why = f"backlog over {cap} clips ({rc.backlog_days:g} days), score {score:.2f}"
        pl.retire.append((meta, why))
        for v in (f for f in meta.parent.iterdir() if f.suffix.lower() in VIDEO):
            drop(v, "approved backlog trim")
        drop(approved_dir / render.name, "approved backlog trim")
        unblock[meta.parent.parent.parent.name] -= 1
    for name, n in unblock.items():
        if n == 0 and name not in blocked:
            settled[name] = True

    # --- loose files in approved/ that a ledger says were posted long ago (hand-placed copies)
    if approved_dir.is_dir():
        for f in approved_dir.iterdir():
            t_post = posted.get(f.name)
            if f.suffix.lower() in VIDEO and t_post is not None and (now - t_post) / 86400 >= rc.posted_keep_days:
                drop(f, "posted clip (approved copy)")

    # --- intermediates of settled projects
    for name, done in settled.items():
        if not done:
            continue
        proj = data_dir / "projects" / name
        for pat, why in (("analysis/*.wav", "analysis audio"), ("source/*.part", "partial download")):
            for f in proj.glob(pat):
                drop(f, why)

    # --- raw recordings and leftover buffer segments
    for q in sorted((data_dir / "live").glob("*/queue.json")):
        sess = _norm(q.parent)
        doc = _read(q) or {}
        for m in doc.get("moments", []):
            p = Path(m.get("path") or "")
            if not p.suffix or not p.exists():
                continue
            st = m.get("status")
            if st in SETTLED_MOMENT:
                if _age_h(p, now) >= rc.skipped_keep_hours:
                    drop(p, f"raw recording ({st})")
            elif st == "done":
                proj = m.get("project")
                if not proj or settled.get(proj, not (data_dir / "projects" / proj).exists()):
                    drop(p, "raw recording (edited, nothing left to post)")
        if sess not in active:
            for f in (q.parent / "buffer").glob("*"):
                if f.is_file() and _age_h(f, now) * 60 >= rc.buffer_min_age_minutes:
                    drop(f, "leftover buffer segment")
    return pl


def apply(pl: Plan) -> dict:
    """Delete what the plan lists and mark retired clips in their review packets."""
    freed, failed = 0, []
    for a in pl.actions:
        try:
            a.path.unlink()
            freed += a.size
        except FileNotFoundError:
            pass
        except OSError as e:     # in use (Windows) or permissions: try again next run
            failed.append(f"{a.path}: {e}")
    for meta, why in pl.retire:
        doc = _read(meta)
        if doc is None:
            continue
        doc.update(status="retired", reason=f"retention: {why}", retention_deleted=time.time())
        meta.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("retention: freed %.2f GB (%d files, %d failed, %d clips retired)",
             freed / 1e9, len(pl.actions) - len(failed), len(failed), len(pl.retire))
    return {"freed_bytes": freed, "freed_gb": round(freed / 1e9, 2), "failed": failed[:20],
            "failed_count": len(failed), "retired": len(pl.retire)}


def run(cfg, data_dir: Path, active_sessions: Iterable[Path] = (), dry_run: bool = False) -> dict:
    pl = plan(cfg, data_dir, active_sessions=active_sessions)
    out = {"plan": pl.summary()}
    if not dry_run:
        out["result"] = apply(pl)
    return out
