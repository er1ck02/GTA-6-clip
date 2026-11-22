"""Project store and the resumable stage runner.

Layout on disk (``<data_dir>/projects/<job_id>/``)::

    project.json        source, config snapshot, stage status/keys/timings
    edits.json          user decisions (accept/reject, trims, crop/layout overrides)
    analysis/           media.json, audio.wav, transcript.json, loudness.json
    candidates.json
    clips/<clip_id>/    state.json, cropplan.json, captions.json/.ass/.srt, <clip>.mp4, qc.json
    project.log.jsonl   structured log

A stage is skipped when its recorded cache key matches and its outputs
exist. Keys hash the stage version, upstream keys and the config sections the
stage reads, so changing e.g. caption style re-renders but never re-transcribes.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from clipstudio.core.compat import write_text_atomic
from clipstudio.core import events
from clipstudio.core.errors import Cancelled, ClipStudioError, StageFailed
from clipstudio.core.logging import get_logger, log_event

log = get_logger("pipeline")


class StageRecord(BaseModel):
    status: str = "pending"  # pending | running | done | failed | interrupted | skipped
    key: str | None = None
    started: float | None = None
    finished: float | None = None
    seconds: float | None = None
    error: str | None = None
    outputs: list[str] = Field(default_factory=list)


class ClipEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    status: Literal["pending", "accepted", "rejected"] | None = None
    start: float | None = Field(None, ge=0)
    end: float | None = Field(None, gt=0)
    layout: Literal["auto", "gameplay", "fit_blur", "facecam_split", "facecam_pip"] | None = None
    crop_center_x: float | None = Field(None, ge=0, le=1)
    facecam_region: dict[str, float] | None = None
    caption_preset: str | None = None
    cuts: list[tuple[float, float]] | None = None  # removed sections, absolute source seconds


class Edits(BaseModel):
    clips: dict[str, ClipEdit] = Field(default_factory=dict)
    # clips the user created (e.g. by splitting one in two): full candidate records, edits apply on top
    added: dict[str, dict[str, Any]] = Field(default_factory=dict)


def key_of(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


class StateFile:
    """A dict of StageRecords persisted atomically to JSON."""

    def __init__(self, path: Path, extra_defaults: dict[str, Any] | None = None):
        self.path = path
        self.data: dict[str, Any] = {"stages": {}}
        if extra_defaults:
            self.data.update(extra_defaults)
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    def stage(self, name: str) -> StageRecord:
        return StageRecord(**self.data["stages"].get(name, {}))

    def set_stage(self, name: str, rec: StageRecord) -> None:
        self.data["stages"][name] = rec.model_dump()
        self.save()

    def save(self) -> None:
        write_text_atomic(self.path, json.dumps(self.data, indent=2, default=str))


@dataclass
class StageResult:
    name: str
    key: str
    cached: bool
    seconds: float


def run_stage(state: StateFile, name: str, key: str, outputs: list[Path], fn: Callable[[], None],
              *, resume: bool = True, force: bool = False, retries: int = 0) -> StageResult:
    rec = state.stage(name)
    if resume and not force and rec.status == "done" and rec.key == key and all(p.exists() for p in outputs):
        log_event(log, 20, f"  [cached] {name}", stage=name, key=key, event="stage_cached")
        events.publish("stage", stage=name, status="cached")
        return StageResult(name, key, True, 0.0)
    rec = StageRecord(status="running", key=key, started=time.time())
    state.set_stage(name, rec)
    log_event(log, 20, f"  [run]    {name} ...", stage=name, key=key, event="stage_start")
    events.publish("stage", stage=name, status="running")
    attempt = 0
    while True:
        t0 = time.perf_counter()
        try:
            fn()
            missing = [str(p) for p in outputs if not p.exists()]
            if missing:
                raise StageFailed(name, f"stage finished but did not produce: {missing}")
            break
        except KeyboardInterrupt:
            rec.status, rec.error = "interrupted", "cancelled by user"
            rec.finished = time.time()
            state.set_stage(name, rec)
            events.publish("stage", stage=name, status="interrupted")
            raise Cancelled(f"cancelled during stage '{name}'; run `resume` to continue")
        except Exception as e:  # recorded, logged with traceback, then re-raised: never swallowed
            if attempt < retries and not isinstance(e, ClipStudioError):
                attempt += 1
                log.warning("stage %s failed (%s); retry %d/%d", name, e, attempt, retries)
                continue
            rec.status, rec.error = "failed", f"{type(e).__name__}: {e}"
            rec.finished = time.time()
            rec.seconds = round(time.perf_counter() - t0, 3)
            state.set_stage(name, rec)
            events.publish("stage", stage=name, status="failed", error=rec.error)
            log.debug("stage %s failed", name, exc_info=True)
            if isinstance(e, StageFailed):
                raise
            raise StageFailed(name, str(e)) from e
    rec.status = "done"
    rec.finished = time.time()
    rec.seconds = round(time.perf_counter() - t0, 3)
    rec.outputs = [str(p) for p in outputs]
    state.set_stage(name, rec)
    log_event(log, 20, f"  [done]   {name} ({rec.seconds:.1f}s)", stage=name, seconds=rec.seconds, event="stage_done")
    events.publish("stage", stage=name, status="done", seconds=rec.seconds)
    return StageResult(name, key, False, rec.seconds)
