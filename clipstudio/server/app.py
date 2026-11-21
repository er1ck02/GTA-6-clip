"""Local HTTP API + static workstation UI.  Start with ``python -m app.cli ui``.

Everything the workstation shows comes from here: real source frames, the
real crop plan, the real transcript/captions, real render + QC results, and a
live event stream of pipeline stages (the "clipping desk").

The server binds to 127.0.0.1 by default and only reads source files the user
explicitly imports.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import queue
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from clipstudio import __version__
from clipstudio.config import BUILTIN_CAPTION_PRESETS, Config
from clipstudio.core import events
from clipstudio.core.errors import ClipStudioError
from clipstudio.core.logging import get_logger
from clipstudio.core.models import CaptionTrack, LoudnessTrack, MediaInfo, QCReport, Transcript
from clipstudio.media.ffmpeg import ffmpeg_bin, run
from clipstudio.pipeline.studio import Studio

try:  # module level so FastAPI can resolve string annotations (from __future__ import annotations)
    from fastapi import Request
except ImportError:  # UI extras not installed; create_app() reports it clearly
    Request = Any  # type: ignore[misc,assignment]

log = get_logger("server")
STATIC = Path(__file__).parent / "static"

# What the clipping desk shows. 'stage' ties a tool to real pipeline events;
# tools without a stage are listed honestly as not integrated yet.
TOOLS: list[dict[str, Any]] = [
    {"id": "ytdlp", "name": "yt-dlp", "role": "URL ingest", "stage": "ingest", "status": "optional"},
    {"id": "ffprobe", "name": "FFprobe", "role": "metadata", "stage": "metadata", "status": "active"},
    {"id": "ffmpeg_audio", "name": "FFmpeg", "role": "audio extract", "stage": "extract_audio", "status": "active"},
    {"id": "faster_whisper", "name": "faster-whisper", "role": "word timestamps", "stage": "transcribe", "status": "active"},
    {"id": "loudness", "name": "NumPy RMS", "role": "audio energy", "stage": "loudness", "status": "active"},
    {"id": "visual", "name": "Visual signals", "role": "cuts · flashes · death screens", "stage": "visual", "status": "active"},
    {"id": "chat", "name": "Chat replay", "role": "chat spikes", "stage": "chat", "status": "active"},
    {"id": "scorer", "name": "Highlight scorer", "role": "rank moments", "stage": "candidates", "status": "active"},
    {"id": "llm", "name": "LLM re-rank", "role": "context judge", "stage": "rank", "status": "active"},
    {"id": "mediapipe", "name": "MediaPipe BlazeFace", "role": "facecam detect", "stage": "facecam", "status": "active"},
    {"id": "tracker", "name": "Motion tracker", "role": "follow action", "stage": "track", "status": "active"},
    {"id": "reframe", "name": "Reframe planner", "role": "9:16 layout", "stage": "plan", "status": "active"},
    {"id": "libass", "name": "libass captions", "role": "word captions", "stage": "captions", "status": "active"},
    {"id": "x264", "name": "FFmpeg x264", "role": "render 9:16", "stage": "render", "status": "active"},
    {"id": "qc", "name": "QC suite", "role": "verify output", "stage": "qc", "status": "active"},
    {"id": "streamlink", "name": "Streamlink", "role": "live capture", "stage": None, "status": "planned"},
    {"id": "tesseract", "name": "Tesseract OCR", "role": "HUD text", "stage": None, "status": "optional"},
    {"id": "yolo", "name": "Ultralytics YOLO", "role": "object tracking", "stage": None, "status": "optional"},
    {"id": "sam2", "name": "SAM 2", "role": "segmentation", "stage": None, "status": "optional"},
    {"id": "demucs", "name": "Demucs", "role": "voice/music split", "stage": None, "status": "optional"},
    {"id": "rife", "name": "Practical-RIFE", "role": "slow motion", "stage": None, "status": "optional"},
    {"id": "video2x", "name": "Video2X", "role": "upscale", "stage": None, "status": "optional"},
]


class JobRunner:
    """One background worker: heavy jobs (transcribe/render) run one at a time, never on the request thread."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, daemon=True, name="clipstudio-jobs").start()

    def submit(self, kind: str, project: str | None, clip: str | None, fn) -> dict:
        jid = uuid.uuid4().hex[:10]
        job = {"id": jid, "kind": kind, "project": project, "clip": clip, "status": "queued",
               "created": time.time(), "error": None, "result": None}
        with self.lock:
            self.jobs[jid] = job
        events.publish("job", job=jid, status="queued", job_kind=kind, project=project, clip=clip)
        self.q.put((job, fn))
        return job

    def _loop(self) -> None:
        while True:
            job, fn = self.q.get()
            token = events.context.set({"job": job["id"], "project": job["project"], "clip": job["clip"]})
            job["status"], job["started"] = "running", time.time()
            events.publish("job", status="running", job_kind=job["kind"])
            try:
                job["result"] = fn()
                job["status"] = "done"
            except Exception as e:
                job["status"], job["error"] = "failed", str(e)
                log.error("job %s failed: %s", job["id"], e)
                log.debug(traceback.format_exc())
            job["finished"] = time.time()
            events.publish("job", status=job["status"], job_kind=job["kind"], error=job["error"])
            events.context.reset(token)

    def list(self) -> list[dict]:
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: -j["created"])[:50]


def create_app(cfg: Config):
    try:
        from fastapi import Body, FastAPI, HTTPException
        from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as e:
        raise ClipStudioError("the UI needs FastAPI + uvicorn: pip install -r requirements/ui.txt") from e

    studio = Studio(cfg)
    jobs = JobRunner()
    app = FastAPI(title="GTA 6 Clip Studio", version=__version__)
    thumbs = studio.data_dir / "cache" / "thumbs"

    @app.exception_handler(ClipStudioError)
    async def _cs_error(_req: Request, exc: ClipStudioError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    def proj(pid: str):
        try:
            return studio.load(pid)
        except ClipStudioError as e:
            raise HTTPException(404, str(e))

    def has(p, name: str) -> bool:
        return p.p(name).exists()

    # ------------------------------------------------------------ system
    @app.get("/api/system")
    def system():
        from clipstudio.doctor import run_doctor

        r = run_doctor(cfg)
        return {"doctor": r.to_dict(), "device": studio.device, "profile": cfg.processing.profile,
                "whisper_model": cfg.transcription.model, "tools": TOOLS}

    @app.get("/api/config")
    def config():
        return {"config": cfg.model_dump(mode="json"),
                "caption_presets": sorted({**BUILTIN_CAPTION_PRESETS, **cfg.captions.presets})}

    # ------------------------------------------------------------ projects
    @app.get("/api/projects")
    def projects():
        return studio.list_projects()

    @app.post("/api/projects")
    def create_project(body: dict = Body(...)):
        path, url = body.get("input_path"), body.get("url")
        p = studio.open_or_create(input_path=Path(path) if path else None, url=url)
        job = jobs.submit("analyze", p.id, None, lambda: analyze_and_preview(p.id))
        return {"project": p.id, "job": job}

    def analyze_and_preview(pid: str) -> None:
        pr = studio.load(pid)
        studio.analyze(pr)
        studio.ensure_preview(pr)

    @app.post("/api/projects/{pid}/analyze")
    def analyze(pid: str):
        p = proj(pid)
        return jobs.submit("analyze", p.id, None, lambda: analyze_and_preview(p.id))

    @app.get("/api/projects/{pid}")
    def project(pid: str):
        p = proj(pid)
        media = MediaInfo.load(p.p("media")).model_dump() if has(p, "media") else None
        clips = {}
        for st in (p.root / "clips").glob("*/state.json"):
            d = json.loads(st.read_text())
            plj = st.parent / "platforms" / "platforms.json"
            clips[d.get("clip_id")] = {"qc_passed": d.get("qc_passed"), "output": bool(d.get("output")),
                                       "stages": {k: v.get("status") for k, v in d.get("stages", {}).items()},
                                       "platforms": [{k: x[k] for k in ("platform", "method", "duration", "trimmed",
                                                                         "reasons")}
                                                     for x in (json.loads(plj.read_text()) if plj.exists() else [])]}
        return {"id": p.id, "source": p.state.data.get("source"), "media": media,
                "stages": p.state.data.get("stages", {}), "clips": clips, "preview": preview_state(p),
                "facecam": fc.model_dump() if (fc := studio.facecam(p)) else None,
                "sample": "sample" in Path(p.state.data["source"]["path"]).name.lower()}

    @app.get("/api/projects/{pid}/candidates")
    def candidates(pid: str):
        p = proj(pid)
        if not has(p, "candidates"):
            return {"weights": {}, "candidates": []}
        return studio.candidates(p).model_dump()

    @app.get("/api/projects/{pid}/transcript")
    def transcript(pid: str):
        p = proj(pid)
        if not has(p, "transcript"):
            raise HTTPException(404, "not transcribed yet")
        return Transcript.load(p.p("transcript")).model_dump()

    @app.get("/api/projects/{pid}/waveform")
    def waveform(pid: str, start: float = 0.0, end: float | None = None, bins: int = 400):
        p = proj(pid)
        if not has(p, "loudness"):
            raise HTTPException(404, "audio not analysed yet")
        lt = LoudnessTrack.load(p.p("loudness"))
        end = end if end is not None else len(lt.rms_db) * lt.window
        i0, i1 = int(start / lt.window), max(int(start / lt.window) + 1, int(end / lt.window))
        vals = lt.rms_db[i0:i1] or [-90.0]
        bins = max(1, min(bins, 4000))
        out = []
        for b in range(bins):
            seg = vals[int(b * len(vals) / bins): max(int(b * len(vals) / bins) + 1, int((b + 1) * len(vals) / bins))]
            peak = max(seg) if seg else -90.0
            out.append(round(max(0.0, min(1.0, (peak + 60) / 60)), 3))  # -60..0 dBFS -> 0..1
        return {"start": start, "end": end, "bins": out, "median_db": lt.median_db}

    # ------------------------------------------------------------ media
    preview_jobs: dict[str, str] = {}

    def preview_state(p) -> dict:
        if not has(p, "media") or not studio.preview_needed(p):
            return {"needed": False, "ready": True}
        return {"needed": True, "ready": has(p, "preview")}

    @app.get("/api/projects/{pid}/source")
    def source(pid: str):
        """The video the browser plays: the original, or an H.264 proxy when the codec isn't playable (HEVC)."""
        p = proj(pid)
        st = preview_state(p)
        if not st["needed"]:
            return FileResponse(p.source_path())  # Starlette FileResponse supports HTTP Range (seeking)
        if st["ready"]:
            return FileResponse(p.p("preview"), media_type="video/mp4")
        running = preview_jobs.get(p.id)
        if not running or next((j for j in jobs.list() if j["id"] == running), {}).get("status") in (None, "failed", "done"):
            preview_jobs[p.id] = jobs.submit("preview", p.id, None, lambda: studio.ensure_preview(studio.load(p.id)) and None)["id"]
        return JSONResponse({"error": "building a browser preview of this HEVC source; retry shortly"},
                            status_code=503, headers={"Retry-After": "5"})

    @app.get("/api/projects/{pid}/frame")
    def frame(pid: str, t: float = 0.0, w: int = 640):
        p = proj(pid)
        w = max(64, min(w, 1920))
        key = hashlib.sha1(f"{p.id}:{t:.2f}:{w}".encode()).hexdigest()[:16]
        f = thumbs / f"{key}.jpg"
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            run([ffmpeg_bin(), "-v", "error", "-y", "-ss", f"{max(0.0, t):.2f}", "-i", str(p.source_path()),
                 "-frames:v", "1", "-vf", f"scale={w}:-2", "-q:v", "4", str(f)])
        return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    # ------------------------------------------------------------ clips
    @app.patch("/api/projects/{pid}/clips/{cid}")
    def edit_clip(pid: str, cid: str, body: dict = Body(...)):
        p = proj(pid)
        allowed = {"status", "start", "end", "layout", "crop_center_x", "facecam_region", "caption_preset", "cuts"}
        bad = set(body) - allowed
        if bad:
            raise HTTPException(400, f"unknown fields: {sorted(bad)}")
        # an explicit null resets that field to automatic
        clear = tuple(k for k, v in body.items() if v is None)
        return studio.edit_clip(p, cid, clear=clear, **{k: v for k, v in body.items() if v is not None}).model_dump()

    @app.post("/api/projects/{pid}/clips/{cid}/split")
    def split_clip(pid: str, cid: str, body: dict = Body(...)):
        if not isinstance(body.get("at"), (int, float)):
            raise HTTPException(400, "body needs a numeric 'at' (source seconds)")
        a, b = studio.split_clip(proj(pid), cid, float(body["at"]))
        return {"first": a.model_dump(), "second": b.model_dump()}

    @app.get("/api/projects/{pid}/clips/{cid}/preview")
    def preview(pid: str, cid: str):
        p = proj(pid)
        c, plan, track = studio.preview(p, cid)
        edit = p.edits().clips.get(c.id)
        cfg_c = studio._clip_config(edit)
        f_tr = p.clip_dir(c.id) / "track.json"
        fc = studio.facecam(p)
        return {"candidate": c.model_dump(), "plan": plan.model_dump(), "captions": track.model_dump() if track else None,
                "facecam": fc.model_dump() if fc else None,
                "track": json.loads(f_tr.read_text()) if f_tr.exists() else None,
                "caption_style": cfg_c.captions.resolved_style().model_dump(),
                "effects": {"punches": studio.punches(p, c, cfg_c), "punch_zoom": cfg_c.enhance.punch_zoom,
                            "hook": c.title if cfg_c.enhance.hook_title else None,
                            "hook_seconds": cfg_c.enhance.hook_title_seconds,
                            "voice_track": studio.voice_track(MediaInfo.load(p.p("media")), cfg_c),
                            "duck_game": cfg_c.audio.duck_game, "voice_enhancement": cfg_c.audio.voice_enhancement},
                "edit": edit.model_dump(exclude_none=True) if edit else {}}

    @app.put("/api/projects/{pid}/clips/{cid}/captions")
    def put_captions(pid: str, cid: str, body: dict = Body(...)):
        p = proj(pid)
        return studio.save_captions(p, cid, CaptionTrack.model_validate(body)).model_dump()

    @app.delete("/api/projects/{pid}/clips/{cid}/captions")
    def reset_captions(pid: str, cid: str):
        p = proj(pid)
        c = studio.candidates(p).get(cid)
        f = p.clip_dir(c.id) / "captions.json"
        if f.exists():
            f.unlink()
        return {"reset": True}

    def _platform_list(body: dict | None) -> list[str] | None:
        names = (body or {}).get("platforms")
        if names is None:
            return None
        unknown = [n for n in names if n not in cfg.export.platform_presets]
        if unknown:
            raise HTTPException(400, f"unknown platforms {unknown}; choose from {sorted(cfg.export.platform_presets)}")
        return list(names)

    def _render_job(p, cid: str, platforms: list[str] | None) -> dict:
        return jobs.submit("render", p.id, cid,
                           lambda: studio.render(studio.load(p.id), [cid], platforms=platforms)[0].qc.passed)

    @app.post("/api/projects/{pid}/clips/{cid}/render")
    def render(pid: str, cid: str, body: dict | None = Body(None)):
        p = proj(pid)
        c = studio.candidates(p).get(cid)
        return _render_job(p, c.id, _platform_list(body))

    @app.post("/api/projects/{pid}/render")
    def render_batch(pid: str, body: dict | None = Body(None)):
        """Batch export: the given clips, or every accepted clip. One queued job per clip."""
        p = proj(pid)
        cs = studio.candidates(p)
        ids = (body or {}).get("clips")
        ids = [cs.get(c).id for c in ids] if ids else [c.id for c in cs.candidates if c.status == "accepted"]
        if not ids:
            raise HTTPException(400, "nothing to export: accept some moments first")
        plats = _platform_list(body)
        return {"jobs": [_render_job(p, cid, plats) for cid in ids]}

    @app.get("/api/projects/{pid}/clips/{cid}/platforms/{name}")
    def clip_platform(pid: str, cid: str, name: str, meta: bool = False):
        p = proj(pid)
        c = studio.candidates(p).get(cid)
        if name not in cfg.export.platform_presets:
            raise HTTPException(404, f"unknown platform {name!r}")
        f = p.clip_dir(c.id) / "platforms" / f"{p.id}_{c.id}.{name}.{'json' if meta else 'mp4'}"
        if not f.exists():
            raise HTTPException(404, f"no {name} copy yet: export with that platform selected")
        return FileResponse(f, media_type="application/json" if meta else "video/mp4", filename=f.name)

    @app.get("/api/projects/{pid}/clips/{cid}/qc")
    def qc(pid: str, cid: str):
        p = proj(pid)
        f = p.clip_dir(studio.candidates(p).get(cid).id) / "qc.json"
        if not f.exists():
            raise HTTPException(404, "not rendered yet")
        return QCReport.load(f).model_dump()

    @app.get("/api/projects/{pid}/clips/{cid}/video")
    def clip_video(pid: str, cid: str):
        p = proj(pid)
        c = studio.candidates(p).get(cid)
        f = p.clip_dir(c.id) / f"{p.id}_{c.id}.mp4"
        if not f.exists():
            raise HTTPException(404, "not rendered yet")
        return FileResponse(f, media_type="video/mp4", filename=f.name)

    @app.get("/api/projects/{pid}/clips/{cid}/captions.srt")
    def clip_srt(pid: str, cid: str):
        p = proj(pid)
        f = p.clip_dir(studio.candidates(p).get(cid).id) / "captions.srt"
        if not f.exists():
            raise HTTPException(404, "not rendered yet")
        return FileResponse(f, media_type="application/x-subrip", filename=f.name)

    # ------------------------------------------------------------ jobs + live events
    @app.get("/api/jobs")
    def list_jobs():
        return jobs.list()

    @app.get("/api/events")
    async def stream(request: Request, after: int = 0):
        loop = asyncio.get_running_loop()
        aq: asyncio.Queue = asyncio.Queue()
        unsub = events.subscribe(lambda ev: loop.call_soon_threadsafe(aq.put_nowait, ev))

        async def gen():
            try:
                for ev in events.history(after):
                    yield f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(aq.get(), timeout=15)
                        yield f"id: {ev['seq']}\ndata: {json.dumps(ev, default=str)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                unsub()

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    if STATIC.exists():
        app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="ui")
    return app


def serve(cfg: Config, host: str = "127.0.0.1", port: int = 8765) -> None:
    try:
        import uvicorn
    except ImportError as e:
        raise ClipStudioError("the UI needs FastAPI + uvicorn: pip install -r requirements/ui.txt") from e
    app = create_app(cfg)
    print(f"GTA 6 Clip Studio running at http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
