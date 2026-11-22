"""Command line interface. Run ``python -m app.cli --help``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clipstudio.config import load_config
from clipstudio.core.compat import install_interrupt_handlers, register_cuda_dlls
from clipstudio.core.errors import Cancelled, ClipStudioError
from clipstudio.core.logging import get_logger, setup_logging

log = get_logger("cli")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", action="append", default=[], help="YAML config file (repeatable, merged in order)")
    p.add_argument("--profile", choices=["fast", "balanced", "quality"], help="processing profile")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="override any config value, e.g. --set captions.preset=minimal")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true", help="print one machine-readable JSON document on stdout "
                   "(logs go to stderr), for scripts and agents")


def _clip_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--layout", choices=["auto", "gameplay", "fit_blur", "facecam_split", "facecam_pip"])
    p.add_argument("--caption-preset")
    p.add_argument("--title", help="clip title, also burned in as the on-screen hook")
    p.add_argument("--facecam", metavar="X,Y,W,H", help="facecam region in normalised coords, e.g. 0.02,0.05,0.25,0.3")


def _platform_opt(p: argparse.ArgumentParser) -> None:
    p.add_argument("--platform", nargs="+", metavar="NAME",
                   help="also write per-platform copies: tiktok youtube_shorts instagram_reels (or your presets)")


VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts", ".m4v", ".avi"}


def expand_inputs(paths: list[Path]) -> list[Path]:
    """Files as given; folders contribute their video files (not recursive), sorted by name."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(f for f in p.iterdir() if f.suffix.lower() in VIDEO_EXT and ".partial" not in f.name)
        else:
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        if p.resolve() not in seen:
            seen.add(p.resolve())
            uniq.append(p)
    return uniq


def _emit(a: argparse.Namespace, doc) -> None:
    if a.json:
        print(json.dumps(doc, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="clipstudio", description="GTA 6 Clip Studio")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="check system compatibility")
    _common(p)

    p = sub.add_parser("process", help="analyse a VOD, rank highlights, render the best clips")
    _common(p)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path, nargs="+",
                     help="video file(s) or folder(s); several = batch, each becomes its own project")
    src.add_argument("--url")
    p.add_argument("--chat", type=Path, help="chat replay file (TwitchDownloader / chat-downloader JSON, "
                   "YouTube .live_chat.json, CSV). A <video>.chat.json next to the video is found automatically")
    p.add_argument("--min-duration", type=float)
    p.add_argument("--max-duration", type=float)
    p.add_argument("--target-duration", type=float)
    p.add_argument("--top", type=int, help="render the top N candidates (0 = analyse only)")
    p.add_argument("--force", action="store_true", help="ignore caches and rerun every stage")
    _platform_opt(p)
    _clip_opts(p)

    p = sub.add_parser("review", help="list candidates; accept/reject/trim/reframe them")
    _common(p)
    p.add_argument("--project", required=True)
    p.add_argument("--accept", nargs="+", default=[], metavar="CLIP")
    p.add_argument("--reject", nargs="+", default=[], metavar="CLIP")
    p.add_argument("--clip", help="clip id or rank to adjust")
    p.add_argument("--start", type=float)
    p.add_argument("--end", type=float)
    p.add_argument("--crop-x", type=float, help="gameplay crop centre, 0..1 of source width")
    p.add_argument("--cut", action="append", default=[], metavar="FROM-TO",
                   help="remove a section from inside --clip, in source seconds (repeatable), e.g. --cut 63.5-71")
    p.add_argument("--clear-cuts", action="store_true", help="remove all cuts from --clip")
    _clip_opts(p)

    p = sub.add_parser("export", help="render clips (accepted ones, given ids, or top N)")
    _common(p)
    p.add_argument("--project", required=True)
    p.add_argument("--clips", nargs="+", metavar="CLIP")
    p.add_argument("--top", type=int)
    p.add_argument("--all-accepted", action="store_true", help="every accepted clip (batch export)")
    p.add_argument("--force", action="store_true")
    _platform_opt(p)

    p = sub.add_parser("resume", help="continue an interrupted project, reusing completed stages")
    _common(p)
    p.add_argument("--project", required=True)

    p = sub.add_parser("status", help="show projects, or one project's stage status")
    _common(p)
    p.add_argument("--project")

    p = sub.add_parser("ui", help="start the Clip Studio workstation (local web UI)")
    _common(p)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("live", help="watch a live stream; keep and edit only the moments that pop off")
    _common(p)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", help="live stream URL (Twitch, YouTube, Kick...)")
    src.add_argument("--input", type=Path, help="a recorded file played back in real time (dry run / test)")
    p.add_argument("--chat", help="auto (from the URL, default), none, twitch:<channel>, youtube, or a chat export "
                                  "file (played back with --input)")
    p.add_argument("--channel", help="streamer's channel name (for the upload allowlist); found from the URL if omitted")
    p.add_argument("--out", type=Path, help="where moments and clips go (default <data_dir>/live/<channel>)")
    p.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = until the stream ends)")
    p.add_argument("--max-moments", type=int, help="stop after this many moments")
    p.add_argument("--no-process", action="store_true", help="only save the moments; don't edit them")
    p.add_argument("--fast", action="store_true", help="with --input: read the file as fast as possible, not in real time")
    return ap


def cmd_live(a) -> int:
    import time as _time

    from clipstudio.live.buffer import Recorder
    from clipstudio.live.chat import channel_from_url, make_chat_source, youtube_channel
    from clipstudio.live.session import LiveSession

    cfg = _cfg(a)
    if a.max_moments is not None:
        cfg.live.max_moments = a.max_moments
    channel = a.channel
    if channel is None and a.url:
        channel = channel_from_url(a.url) or (youtube_channel(a.url) if "youtu" in a.url else None)
    channel = channel.lower().lstrip("@") if channel else None
    base = Path(cfg.paths.data_dir).expanduser().resolve() / "live"
    out = a.out or base / f"{channel or 'stream'}_{_time.strftime('%Y%m%d_%H%M%S')}"
    rec = Recorder(out / "buffer", cfg.live.segment_seconds, file=a.input, url=a.url, realtime=not a.fast)
    chat = make_chat_source(a.chat, a.url, out)
    allowed = channel in {c.lower().lstrip("@") for c in cfg.upload.allow_channels} if channel else False
    log.info("live: %s | channel %s (%s) | chat %s | buffer %gs | moments %gs before + %gs after",
             a.url or a.input, channel or "unknown", "on the upload allowlist" if allowed else "review only",
             chat.name, cfg.live.buffer_seconds, cfg.live.pre_seconds, cfg.live.post_seconds)
    sess = LiveSession(cfg, rec, chat, out, channel=channel, process=False if a.no_process else None,
                       max_seconds=a.duration)
    moments = sess.run()
    doc = {"out": str(out), "channel": channel, "stopped": sess.stopped_reason,
           "segments_discarded": sess.ring.deleted, "peak_buffer_mb": round(sess.peak_disk / 1e6, 1),
           "moments": [m.__dict__ for m in moments]}
    if a.json:
        _emit(a, doc)
    else:
        for m in moments:
            print(f"{m.id}  {m.status:10s}  score {m.trigger['score']:.2f}  {', '.join(m.trigger['reasons'])}  "
                  f"{m.clip or m.path or ''}  [{m.upload}]")
        print(f"queue: {out / 'queue.json'}")
    return 0 if all(m.status in ("done", "saved") for m in moments) else 1


def _cfg(a: argparse.Namespace):
    ov = list(a.overrides)
    for flag, key in (("min_duration", "highlights.minimum_duration"), ("max_duration", "highlights.maximum_duration"),
                      ("target_duration", "highlights.target_duration"), ("layout", "reframing.mode"),
                      ("caption_preset", "captions.preset")):
        val = getattr(a, flag, None)
        if val is not None:
            ov.append(f"{key}={val}")
    if getattr(a, "platform", None):
        ov.append(f"export.platforms=[{', '.join(a.platform)}]")
    if getattr(a, "facecam", None):
        x, y, w, h = (float(v) for v in a.facecam.split(","))
        ov.append(f"reframing.facecam_region={{x: {x}, y: {y}, w: {w}, h: {h}}}")
    # keep target inside an explicitly narrowed min/max range
    lo_f, hi_f = getattr(a, "min_duration", None), getattr(a, "max_duration", None)
    if getattr(a, "target_duration", None) is None and (lo_f is not None or hi_f is not None):
        # validate against the base config first: the narrowed range alone may exclude the default target
        base = [o for o in ov if not o.startswith(("highlights.minimum_duration=", "highlights.maximum_duration="))]
        h = load_config(a.config, base, a.profile).highlights
        lo = lo_f if lo_f is not None else h.minimum_duration
        hi = hi_f if hi_f is not None else h.maximum_duration
        if not lo <= h.target_duration <= hi:
            ov.append(f"highlights.target_duration={round((lo + hi) / 2, 2)}")
    return load_config(a.config, ov, a.profile)


def _print_candidates(cs, limit: int = 50) -> None:
    print(f"\n{'#':>3}  {'id':<9} {'start':>8} {'end':>8} {'dur':>5} {'score':>5} {'conf':>4}  {'status':<8} {'cat':<10} title")
    for c in cs.candidates[:limit]:
        flag = "" if c.passes_threshold else " (below threshold)"
        cut = f" [-{c.cut_seconds:.1f}s cut]" if c.cuts else ""
        print(f"{c.rank:>3}  {c.id:<9} {c.start:>8.2f} {c.end:>8.2f} {c.output_duration:>5.1f} {c.score:>5.2f} "
              f"{c.confidence:>4.2f}  {c.status:<8} {c.category:<10} {c.title}{flag}{cut}")
    print()


def _cand_doc(cs, limit: int = 50) -> list[dict]:
    return [{**c.model_dump(exclude={"features", "contributions"}), "output_duration": c.output_duration}
            for c in cs.candidates[:limit]]


def _print_results(results) -> int:
    bad = 0
    print("\nRendered clips:")
    for r in results:
        mark = "PASS" if r.qc.passed else "FAIL"
        bad += 0 if r.qc.passed else 1
        print(f"  [{mark}] {r.clip_id}  {r.output}  ({r.qc.summary()}; {r.seconds:.1f}s)")
        for pl in r.platforms:
            how = "re-encoded: " + "; ".join(pl["reasons"]) if pl["reasons"] else "stream copy"
            print(f"         {pl['platform']:<16} {pl['path']}  ({how})")
    return bad


def _results(a, results, extra: dict | None = None) -> int:
    bad = sum(0 if r.qc.passed else 1 for r in results)
    if a.json:
        _emit(a, {**(extra or {}), "results": [r.to_dict() for r in results], "failed": bad})
    else:
        _print_results(results)
    return 1 if bad else 0


def cmd_doctor(a) -> int:
    from clipstudio.doctor import format_report, run_doctor

    cfg = _cfg(a)
    r = run_doctor(cfg)
    if a.json:
        print(json.dumps(r.to_dict(), indent=2))
    else:
        print(format_report(r))
    logs = Path(cfg.paths.logs_dir)
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "doctor.json").write_text(json.dumps(r.to_dict(), indent=2))
    return 0 if r.ok else 1


def _process_one(a, st, cfg, input_path, url, chat) -> dict:
    p = st.open_or_create(input_path=input_path, url=url, chat=chat)
    cs = st.analyze(p, force=a.force)
    if not a.json:
        _print_candidates(cs)
        print(f"project: {p.id}")
    top = cfg.export.auto_render_top if a.top is None else a.top
    doc = {"project": p.id, "source": str(input_path or url), "candidates": _cand_doc(cs), "results": []}
    if top <= 0:
        if not a.json:
            print(f"next: python -m app.cli review --project {p.id}")
        return doc
    results = st.render(p, st.default_selection(p, top), force=a.force)
    if not a.json:
        _print_results(results)
    doc["results"] = [r.to_dict() for r in results]
    return doc


def cmd_process(a) -> int:
    from clipstudio.pipeline.studio import Studio

    cfg = _cfg(a)
    st = Studio(cfg)
    log.info("device: %s | profile: %s | whisper: %s", st.device, cfg.processing.profile, cfg.transcription.model)
    inputs = expand_inputs(a.input) if a.input else [None]
    if a.input and not inputs:
        log.error("no video files found in %s", ", ".join(map(str, a.input)))
        return 1
    if a.chat and len(inputs) > 1:
        log.error("--chat works with a single input; for a batch put <video>.chat.json next to each video")
        return 1
    docs, errors = [], []
    for i, src in enumerate(inputs):
        if len(inputs) > 1:
            log.info("[%d/%d] %s", i + 1, len(inputs), src)
        try:
            docs.append(_process_one(a, st, cfg, src, a.url, a.chat))
        except ClipStudioError as e:
            if len(inputs) == 1:
                raise
            # a batch keeps going: one bad file must not cost the rest of the night's work
            log.error("%s failed: %s", src, e)
            errors.append({"source": str(src), "error": str(e)})
    failed = sum(1 for d in docs for r in d["results"] if not r["qc_passed"])
    if a.json:
        _emit(a, {"projects": docs, "errors": errors, "failed_clips": failed})
    elif len(inputs) > 1:
        print(f"batch: {len(docs)} of {len(inputs)} sources processed, {failed} clip(s) failed QC, "
              f"{len(errors)} source(s) failed")
    return 1 if failed or errors else 0


def cmd_review(a) -> int:
    from clipstudio.pipeline.studio import Studio

    st = Studio(_cfg(a))
    p = st.load(a.project)
    for cid in a.accept:
        st.edit_clip(p, cid, status="accepted")
    for cid in a.reject:
        st.edit_clip(p, cid, status="rejected")
    if a.clip:
        fr = None
        if a.facecam:
            x, y, w, h = (float(v) for v in a.facecam.split(","))
            fr = {"x": x, "y": y, "w": w, "h": h}
        cuts = None
        if a.cut:
            cuts = list(st.candidates(p).get(a.clip).cuts) if not a.clear_cuts else []
            for spec in a.cut:
                lo, _, hi = spec.partition("-")
                cuts.append((float(lo), float(hi)))
        c = st.edit_clip(p, a.clip, clear=("cuts",) if a.clear_cuts and not a.cut else (), start=a.start, end=a.end,
                         crop_center_x=a.crop_x, layout=a.layout, caption_preset=a.caption_preset,
                         facecam_region=fr, cuts=cuts, title=getattr(a, "title", None))
        log.info("updated %s: %.2f-%.2fs%s", c.id, c.start, c.end, f", {len(c.cuts)} cut(s)" if c.cuts else "")
    cs = st.candidates(p)
    if a.json:
        _emit(a, {"project": p.id, "candidates": _cand_doc(cs)})
    else:
        _print_candidates(cs)
        for c in cs.candidates[:3]:
            print(f"#{c.rank} {c.id}: {c.reason}\n    \"{c.transcript[:200]}\"\n")
        print(f"next: python -m app.cli export --project {p.id}")
    return 0


def cmd_export(a) -> int:
    from clipstudio.pipeline.studio import Studio

    cfg = _cfg(a)
    st = Studio(cfg)
    p = st.load(a.project)
    if a.all_accepted:
        ids = [c.id for c in st.candidates(p).candidates if c.status == "accepted"]
    else:
        ids = a.clips or st.default_selection(p, a.top if a.top is not None else cfg.export.auto_render_top)
    if not ids:
        log.error("nothing to export: accept clips with `review --accept` or pass --clips/--top")
        if a.json:
            _emit(a, {"project": p.id, "results": [], "failed": 0, "error": "nothing to export"})
        return 1
    return _results(a, st.render(p, ids, force=a.force), {"project": p.id})


def cmd_resume(a) -> int:
    from clipstudio.pipeline.studio import Studio

    cfg = _cfg(a)
    st = Studio(cfg)
    p = st.load(a.project)
    st.analyze(p)
    ids = p.state.data.get("requested_clips") or st.default_selection(p, cfg.export.auto_render_top)
    if not ids:
        _emit(a, {"project": p.id, "results": [], "failed": 0})
        return 0
    return _results(a, st.render(p, ids), {"project": p.id})


def cmd_status(a) -> int:
    from clipstudio.pipeline.studio import Studio

    st = Studio(_cfg(a))
    if not a.project:
        prs = st.list_projects()
        if a.json:
            _emit(a, {"projects": prs})
            return 0
        for pr in prs:
            stages = " ".join(f"{k}:{v}" for k, v in pr["stages"].items())
            print(f"{pr['id']:<36} {Path(pr['source'].get('path', '')).name:<30} {stages}")
        return 0
    p = st.load(a.project)
    clips = [json.loads(d.read_text()) for d in sorted((p.root / "clips").glob("*/state.json"))]
    if a.json:
        _emit(a, {"project": p.id, "source": str(p.source_path()), "stages": p.state.data["stages"],
                  "clips": [{"clip_id": s.get("clip_id"), "qc_passed": s.get("qc_passed"), "output": s.get("output"),
                             "stages": s.get("stages", {})} for s in clips]})
        return 0
    print(f"project {p.id}\nsource  {p.source_path()}")
    for name, rec in p.state.data["stages"].items():
        print(f"  {name:<14} {rec['status']:<11} {rec.get('seconds') or 0:>7.1f}s  {rec.get('error') or ''}")
    for s in clips:
        print(f"  clip {s.get('clip_id')}: QC {'passed' if s.get('qc_passed') else 'not passed'}  {s.get('output', '')}")
    return 0


def cmd_ui(a) -> int:
    from clipstudio.server.app import serve

    serve(_cfg(a), a.host, a.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    setup_logging(getattr(a, "verbose", False))
    install_interrupt_handlers()
    register_cuda_dlls()
    handlers = {"doctor": cmd_doctor, "process": cmd_process, "review": cmd_review, "export": cmd_export,
                "resume": cmd_resume, "status": cmd_status, "ui": cmd_ui,
                "live": cmd_live}
    def fail(code: int, msg: str) -> int:
        log.error(msg)
        if getattr(a, "json", False):
            print(json.dumps({"error": msg, "exit_code": code}))
        return code

    try:
        return handlers[a.cmd](a)
    except Cancelled as e:
        return fail(130, str(e))
    except KeyboardInterrupt:
        return fail(130, "cancelled")
    except ClipStudioError as e:
        return fail(2, str(e))
    except ValueError as e:  # config validation
        return fail(2, f"invalid configuration: {e}")


if __name__ == "__main__":
    sys.exit(main())
