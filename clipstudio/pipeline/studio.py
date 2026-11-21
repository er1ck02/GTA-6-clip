"""The Studio service: the one orchestration API used by the CLI (and later the UI/HTTP API).

    studio = Studio(cfg)
    project = studio.open_or_create(input_path=...)
    studio.analyze(project)              # ingest -> metadata -> audio -> transcript -> loudness -> candidates
    studio.render(project, clip_ids)     # plan -> captions -> render -> QC, per clip
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from clipstudio import __version__
from clipstudio.audio_analysis.loudness import analyze_loudness
from clipstudio.captions.engine import build_caption_track, write_ass, write_srt
from clipstudio.config import Config, Rect
from clipstudio.core import events
from clipstudio.core.errors import ClipStudioError, MediaError
from clipstudio.core.logging import add_file_log, get_logger
from clipstudio.core.models import (Candidate, CandidateSet, CaptionTrack, CropPlan, LoudnessTrack, MediaInfo,
                                    QCReport, Transcript)
from clipstudio.highlights.candidates import generate_candidates
from clipstudio.ingestion.sources import download_url, file_fingerprint, job_id_for_file, job_id_for_url, validate_local
from clipstudio.media.ffmpeg import extract_audio_wav, probe
from clipstudio.pipeline.project import ClipEdit, Edits, StateFile, key_of, run_stage
from clipstudio.quality_control.checks import run_qc
from clipstudio.reframing.layout import plan_layout
from clipstudio.rendering.render import render_clip
from clipstudio.transcription.base import get_backend, resolve_device

log = get_logger("studio")

# bump a stage's version when its logic changes so caches invalidate
STAGE_VERSIONS = {"metadata": 1, "extract_audio": 1, "transcribe": 1, "loudness": 1, "candidates": 4,
                  "plan": 1, "captions": 3, "render": 2, "qc": 2}


@dataclass
class Project:
    id: str
    root: Path
    state: StateFile

    @property
    def analysis(self) -> Path:
        return self.root / "analysis"

    def p(self, name: str) -> Path:
        return {
            "media": self.analysis / "media.json", "audio": self.analysis / "audio.wav",
            "transcript": self.analysis / "transcript.json", "loudness": self.analysis / "loudness.json",
            "candidates": self.root / "candidates.json", "edits": self.root / "edits.json",
        }[name]

    def clip_dir(self, cid: str) -> Path:
        return self.root / "clips" / cid

    def source_path(self) -> Path:
        return Path(self.state.data["source"]["path"])

    def edits(self) -> Edits:
        f = self.p("edits")
        return Edits.model_validate_json(f.read_text()) if f.exists() else Edits()

    def save_edits(self, e: Edits) -> None:
        tmp = self.p("edits").with_suffix(".tmp")
        tmp.write_text(e.model_dump_json(indent=2, exclude_none=True))
        tmp.replace(self.p("edits"))


@dataclass
class ClipResult:
    clip_id: str
    output: Path
    qc: QCReport
    seconds: float
    cached_stages: list[str] = field(default_factory=list)


class Studio:
    def __init__(self, cfg: Config, progress: Callable[[str, float], None] | None = None):
        self.cfg = cfg
        self.data_dir = Path(cfg.paths.data_dir).expanduser().resolve()
        self.models_dir = Path(cfg.paths.models_dir).expanduser().resolve()
        self.device = resolve_device(cfg.processing.device)
        self.progress = progress or (lambda stage, frac: None)

    # ------------------------------------------------------------------ projects
    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    def list_projects(self) -> list[dict]:
        out = []
        if self.projects_dir.exists():
            for d in sorted(self.projects_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                f = d / "project.json"
                if f.exists():
                    data = json.loads(f.read_text())
                    out.append({"id": d.name, "source": data.get("source", {}), "created": data.get("created"),
                                "stages": {k: v.get("status") for k, v in data.get("stages", {}).items()}})
        return out

    def load(self, project_id: str) -> Project:
        root = self.projects_dir / project_id
        if not (root / "project.json").exists():
            matches = [d for d in self.projects_dir.glob(f"{project_id}*") if (d / "project.json").exists()] \
                if self.projects_dir.exists() else []
            if len(matches) != 1:
                raise ClipStudioError(f"project {project_id!r} not found" if not matches
                                      else f"project id {project_id!r} is ambiguous: {[m.name for m in matches]}")
            root = matches[0]
        return self._attach(Project(root.name, root, StateFile(root / "project.json")))

    def _attach(self, p: Project) -> Project:
        add_file_log(p.root / "project.log.jsonl")
        return p

    def open_or_create(self, input_path: Path | None = None, url: str | None = None) -> Project:
        if (input_path is None) == (url is None):
            raise ClipStudioError("give exactly one of --input or --url")
        if input_path is not None:
            src = validate_local(input_path)
            pid = job_id_for_file(src)
            source = {"type": "file", "path": str(src), "fingerprint": file_fingerprint(src)}
        else:
            pid = job_id_for_url(url)
            dl = download_url(url, self.projects_dir / pid / "source")
            src = validate_local(dl)
            source = {"type": "url", "url": url, "path": str(src), "fingerprint": file_fingerprint(src)}
        root = self.projects_dir / pid
        state = StateFile(root / "project.json", {"id": pid, "created": time.time(), "version": __version__})
        state.data["source"] = source
        state.data["config"] = self.cfg.model_dump(mode="json")
        state.save()
        log.info("project %s (%s)", pid, src.name)
        return self._attach(Project(pid, root, state))

    def verify_source(self, p: Project) -> None:
        src = p.source_path()
        if not src.exists():
            raise MediaError(f"source file for project {p.id} is missing: {src}")
        if file_fingerprint(src) != p.state.data["source"]["fingerprint"]:
            raise MediaError(f"source file {src} changed since project {p.id} was created; create a new project")

    # ------------------------------------------------------------------ analysis
    def analyze(self, p: Project, force: bool = False) -> CandidateSet:
        cfg, resume = self.cfg, self.cfg.processing.resume_enabled and self.cfg.processing.cache_enabled
        self.verify_source(p)
        src = p.source_path()
        fp = p.state.data["source"]["fingerprint"]
        log.info("analysing %s", p.id)

        k_meta = key_of("metadata", STAGE_VERSIONS["metadata"], fp)
        run_stage(p.state, "metadata", k_meta, [p.p("media")], lambda: probe(src).save(p.p("media")),
                  resume=resume, force=force)
        media = MediaInfo.load(p.p("media"))
        if not media.has_audio:
            raise MediaError("source has no audio; Phase 2 highlight detection needs speech")

        k_aud = key_of("extract_audio", STAGE_VERSIONS["extract_audio"], k_meta)
        run_stage(p.state, "extract_audio", k_aud, [p.p("audio")], lambda: extract_audio_wav(src, p.p("audio")),
                  resume=resume, force=force)

        k_tr = key_of("transcribe", STAGE_VERSIONS["transcribe"], k_aud, cfg.section_hash("transcription"))

        def do_transcribe() -> None:
            backend = get_backend(cfg.transcription, self.device, self.models_dir)
            last = [0.0]

            def prog(f: float) -> None:
                self.progress("transcribe", f)
                events.publish("progress", stage="transcribe", progress=round(f, 3))
                if f - last[0] >= 0.1:
                    last[0] = f
                    log.info("    transcribed %d%%", int(f * 100))

            backend.transcribe(p.p("audio"), media.duration, prog).save(p.p("transcript"))

        run_stage(p.state, "transcribe", k_tr, [p.p("transcript")], do_transcribe, resume=resume, force=force)

        k_ld = key_of("loudness", STAGE_VERSIONS["loudness"], k_aud, cfg.section_hash("audio_analysis"))
        run_stage(p.state, "loudness", k_ld, [p.p("loudness")],
                  lambda: analyze_loudness(p.p("audio"), cfg.audio_analysis.window_seconds).save(p.p("loudness")),
                  resume=resume, force=force)

        k_c = key_of("candidates", STAGE_VERSIONS["candidates"], k_tr, k_ld, cfg.section_hash("highlights"))
        run_stage(p.state, "candidates", k_c, [p.p("candidates")],
                  lambda: generate_candidates(Transcript.load(p.p("transcript")), LoudnessTrack.load(p.p("loudness")),
                                              cfg.highlights, media.duration).save(p.p("candidates")),
                  resume=resume, force=force)
        return self.candidates(p)

    def candidates(self, p: Project) -> CandidateSet:
        """Generated candidates with the user's edits applied on top."""
        cs = CandidateSet.load(p.p("candidates"))
        edits = p.edits()
        for c in cs.candidates:
            e = edits.clips.get(c.id)
            if not e:
                continue
            if e.status:
                c.status = e.status  # type: ignore[assignment]
            if e.start is not None or e.end is not None:
                c.start = e.start if e.start is not None else c.start
                c.end = e.end if e.end is not None else c.end
                c.duration = round(c.end - c.start, 3)
                c.user_adjusted = True
        return cs

    def edit_clip(self, p: Project, cid: str, **changes) -> Candidate:
        cs = self.candidates(p)
        c = cs.get(cid)
        media = MediaInfo.load(p.p("media"))
        edits = p.edits()
        e = edits.clips.get(c.id, ClipEdit())
        for k, v in changes.items():
            if v is not None:
                setattr(e, k, v)
        start = e.start if e.start is not None else c.start
        end = e.end if e.end is not None else c.end
        if not (0 <= start < end <= media.duration + 1e-3):
            raise ClipStudioError(f"invalid clip range {start:.2f}-{end:.2f}s (source is {media.duration:.2f}s)")
        if e.facecam_region is not None:
            Rect(**e.facecam_region)  # validate
        edits.clips[c.id] = e
        p.save_edits(edits)
        return self.candidates(p).get(c.id)

    # ------------------------------------------------------------------ clips
    def _clip_config(self, e: ClipEdit | None) -> Config:
        cfg = self.cfg.model_copy(deep=True)
        if e:
            if e.layout:
                cfg.reframing.mode = e.layout  # type: ignore[assignment]
            if e.crop_center_x is not None:
                cfg.reframing.gameplay_center_x = e.crop_center_x
            if e.facecam_region is not None:
                cfg.reframing.facecam_region = Rect(**e.facecam_region)
            if e.caption_preset:
                cfg.captions.preset = e.caption_preset
                cfg.captions.resolved_style()
        return cfg

    def render(self, p: Project, clip_ids: list[str], force: bool = False) -> list[ClipResult]:
        self.verify_source(p)
        results = []
        requested = set(p.state.data.get("requested_clips", []))
        cs = self.candidates(p)
        ids = [cs.get(c).id for c in clip_ids]
        p.state.data["requested_clips"] = sorted(requested | set(ids))
        p.state.save()
        for cid in ids:
            results.append(self.render_one(p, cid, force=force))
        return results

    def render_one(self, p: Project, cid: str, force: bool = False) -> ClipResult:
        t0 = time.perf_counter()
        resume = self.cfg.processing.resume_enabled and self.cfg.processing.cache_enabled
        c = self.candidates(p).get(cid)
        e = p.edits().clips.get(c.id)
        cfg = self._clip_config(e)
        d = p.clip_dir(c.id)
        d.mkdir(parents=True, exist_ok=True)
        state = StateFile(d / "state.json", {"clip_id": c.id})
        media = MediaInfo.load(p.p("media"))
        tr_key = p.state.stage("transcribe").key
        cached: list[str] = []
        log.info("clip %s  #%d  %.2f-%.2fs  \"%s\"", c.id, c.rank, c.start, c.end, c.title)

        f_plan, f_caps, f_ass, f_srt = d / "cropplan.json", d / "captions.json", d / "captions.ass", d / "captions.srt"
        f_out, f_qc = d / f"{p.id}_{c.id}.mp4", d / "qc.json"

        margins = {k: getattr(cfg.captions, k) for k in ("safe_margin_x", "safe_margin_top", "safe_margin_bottom")}
        k_plan = key_of("plan", STAGE_VERSIONS["plan"], media.model_dump(), cfg.section_hash("reframing", "video"), margins)
        r = run_stage(state, "plan", k_plan, [f_plan],
                      lambda: plan_layout(media, cfg.reframing, cfg.video, cfg.captions).save(f_plan),
                      resume=resume, force=force)
        cached += [r.name] if r.cached else []
        plan = CropPlan.load(f_plan)

        caps_on = cfg.captions.enabled
        k_cap = key_of("captions", STAGE_VERSIONS["captions"], tr_key, k_plan, c.start, c.end, cfg.section_hash("captions"))

        def do_captions() -> None:
            # a hand-corrected caption track is preserved; only its ASS/SRT are regenerated
            if f_caps.exists() and CaptionTrack.load(f_caps).edited:
                track = CaptionTrack.load(f_caps)
                log.info("    using hand-edited captions")
            else:
                words = Transcript.load(p.p("transcript")).words_between(c.start, c.end)
                track = build_caption_track(words, c.start, c.end, cfg.captions, plan)
                track.save(f_caps)
            write_ass(track, cfg.captions.resolved_style(), plan, f_ass)
            if cfg.captions.export_srt:
                write_srt(track, f_srt)

        if caps_on:
            r = run_stage(state, "captions", k_cap, [f_caps, f_ass], do_captions, resume=resume, force=force)
            cached += [r.name] if r.cached else []

        k_r = key_of("render", STAGE_VERSIONS["render"], k_plan, k_cap if caps_on else None, c.start, c.end,
                     p.state.data["source"]["fingerprint"], cfg.section_hash("video", "audio", "export"),
                     f_ass.read_text() if caps_on else None)
        r = run_stage(state, "render", k_r, [f_out],
                      lambda: render_clip(media, plan, c.start, c.end, f_ass if caps_on else None, f_out,
                                          cfg.video, cfg.audio, cfg.export, self.device),
                      resume=resume, force=force, retries=1)
        cached += [r.name] if r.cached else []

        k_q = key_of("qc", STAGE_VERSIONS["qc"], k_r, cfg.section_hash("quality"))

        def do_qc() -> None:
            track = CaptionTrack.load(f_caps) if caps_on else None
            run_qc(c.id, f_out, c.end - c.start, plan, track, f_ass if caps_on else None, cfg.quality,
                   cfg.captions, expect_audio=media.has_audio).save(f_qc)

        r = run_stage(state, "qc", k_q, [f_qc], do_qc, resume=resume, force=force)
        cached += [r.name] if r.cached else []
        qc = QCReport.load(f_qc)
        state.data["qc_passed"] = qc.passed
        state.data["output"] = str(f_out)
        state.save()
        level = log.info if qc.passed else log.error
        level("    QC %s (%s) -> %s", "PASSED" if qc.passed else "FAILED", qc.summary(), f_out)
        if not qc.passed:
            for ch in qc.checks:
                if ch.status == "fail":
                    log.error("      fail: %s: %s", ch.name, ch.detail)
        return ClipResult(c.id, f_out, qc, round(time.perf_counter() - t0, 2), cached)

    def preview(self, p: Project, cid: str) -> tuple[Candidate, CropPlan, CaptionTrack | None]:
        """Plan + caption track for the clip as it would render now (no encoding). Used by the UI."""
        c = self.candidates(p).get(cid)
        cfg = self._clip_config(p.edits().clips.get(c.id))
        media = MediaInfo.load(p.p("media"))
        plan = plan_layout(media, cfg.reframing, cfg.video, cfg.captions)
        f_caps = p.clip_dir(c.id) / "captions.json"
        track = None
        if cfg.captions.enabled:
            if f_caps.exists() and CaptionTrack.load(f_caps).edited:
                track = CaptionTrack.load(f_caps)
            else:
                words = Transcript.load(p.p("transcript")).words_between(c.start, c.end)
                track = build_caption_track(words, c.start, c.end, cfg.captions, plan)
        return c, plan, track

    def save_captions(self, p: Project, cid: str, track: CaptionTrack) -> CaptionTrack:
        """Store a hand-corrected caption track; it is preserved across re-renders."""
        c = self.candidates(p).get(cid)
        dur = c.end - c.start
        chunks = sorted(track.chunks, key=lambda ch: ch.start)
        for i, ch in enumerate(chunks):
            if not (0 <= ch.start < ch.end <= dur + 0.05):
                raise ClipStudioError(f"caption {i} timing {ch.start:.2f}-{ch.end:.2f}s is outside the clip (0-{dur:.2f}s)")
            if i and ch.start < chunks[i - 1].end - 1e-3:
                raise ClipStudioError(f"caption {i} overlaps the previous caption")
            ch.index = i
        track.chunks = chunks
        track.edited = True
        track.source_word_count = sum(len(ln) for ch in chunks for ln in ch.lines)
        d = p.clip_dir(c.id)
        d.mkdir(parents=True, exist_ok=True)
        track.save(d / "captions.json")
        # invalidate the caption stage so the next render rewrites the ASS file
        st = StateFile(d / "state.json", {"clip_id": c.id})
        if "captions" in st.data["stages"]:
            st.data["stages"]["captions"]["key"] = None
            st.save()
        return track

    def default_selection(self, p: Project, top: int) -> list[str]:
        cs = self.candidates(p)
        accepted = [c.id for c in cs.candidates if c.status == "accepted"]
        if accepted:
            return accepted
        pool = [c for c in cs.candidates if c.status != "rejected"]
        good = [c.id for c in pool if c.passes_threshold][:top]
        if not good and pool and top > 0:
            log.warning("no candidate reached minimum_score %.2f; rendering the best one anyway for review",
                        self.cfg.highlights.minimum_score)
            good = [pool[0].id]
        return good
