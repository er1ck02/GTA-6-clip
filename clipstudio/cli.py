"""Command line interface. Run ``python -m app.cli --help``."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clipstudio.config import load_config
from clipstudio.core.errors import Cancelled, ClipStudioError
from clipstudio.core.logging import get_logger, setup_logging

log = get_logger("cli")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", action="append", default=[], help="YAML config file (repeatable, merged in order)")
    p.add_argument("--profile", choices=["fast", "balanced", "quality"], help="processing profile")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="override any config value, e.g. --set captions.preset=minimal")
    p.add_argument("-v", "--verbose", action="store_true")


def _clip_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("--layout", choices=["gameplay", "fit_blur", "facecam_split"])
    p.add_argument("--caption-preset")
    p.add_argument("--facecam", metavar="X,Y,W,H", help="facecam region in normalised coords, e.g. 0.02,0.05,0.25,0.3")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="clipstudio", description="GTA 6 Clip Studio")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="check system compatibility")
    _common(p)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("process", help="analyse a VOD, rank highlights, render the best clips")
    _common(p)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path)
    src.add_argument("--url")
    p.add_argument("--min-duration", type=float)
    p.add_argument("--max-duration", type=float)
    p.add_argument("--target-duration", type=float)
    p.add_argument("--top", type=int, help="render the top N candidates (0 = analyse only)")
    p.add_argument("--force", action="store_true", help="ignore caches and rerun every stage")
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
    p.add_argument("--json", action="store_true")
    _clip_opts(p)

    p = sub.add_parser("export", help="render clips (accepted ones, given ids, or top N)")
    _common(p)
    p.add_argument("--project", required=True)
    p.add_argument("--clips", nargs="+", metavar="CLIP")
    p.add_argument("--top", type=int)
    p.add_argument("--force", action="store_true")

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
    return ap


def _cfg(a: argparse.Namespace):
    ov = list(a.overrides)
    for flag, key in (("min_duration", "highlights.minimum_duration"), ("max_duration", "highlights.maximum_duration"),
                      ("target_duration", "highlights.target_duration"), ("layout", "reframing.mode"),
                      ("caption_preset", "captions.preset")):
        val = getattr(a, flag, None)
        if val is not None:
            ov.append(f"{key}={val}")
    if getattr(a, "facecam", None):
        x, y, w, h = (float(v) for v in a.facecam.split(","))
        ov.append(f"reframing.facecam_region={{x: {x}, y: {y}, w: {w}, h: {h}}}")
    # keep target inside an explicitly narrowed min/max range
    if getattr(a, "target_duration", None) is None and (getattr(a, "min_duration", None) or getattr(a, "max_duration", None)):
        cfg0 = load_config(a.config, ov, a.profile)
        h = cfg0.highlights
        lo, hi = h.minimum_duration, h.maximum_duration
        if not lo <= h.target_duration <= hi:
            ov.append(f"highlights.target_duration={round((lo + hi) / 2, 2)}")
    return load_config(a.config, ov, a.profile)


def _print_candidates(cs, limit: int = 50) -> None:
    print(f"\n{'#':>3}  {'id':<9} {'start':>8} {'end':>8} {'dur':>5} {'score':>5} {'conf':>4}  {'status':<8} {'cat':<10} title")
    for c in cs.candidates[:limit]:
        flag = "" if c.passes_threshold else " (below threshold)"
        print(f"{c.rank:>3}  {c.id:<9} {c.start:>8.2f} {c.end:>8.2f} {c.duration:>5.1f} {c.score:>5.2f} "
              f"{c.confidence:>4.2f}  {c.status:<8} {c.category:<10} {c.title}{flag}")
    print()


def _print_results(results) -> int:
    bad = 0
    print("\nRendered clips:")
    for r in results:
        mark = "PASS" if r.qc.passed else "FAIL"
        bad += 0 if r.qc.passed else 1
        print(f"  [{mark}] {r.clip_id}  {r.output}  ({r.qc.summary()}; {r.seconds:.1f}s)")
    return bad


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


def cmd_process(a) -> int:
    from clipstudio.pipeline.studio import Studio

    cfg = _cfg(a)
    st = Studio(cfg)
    log.info("device: %s | profile: %s | whisper: %s", st.device, cfg.processing.profile, cfg.transcription.model)
    p = st.open_or_create(input_path=a.input, url=a.url)
    cs = st.analyze(p, force=a.force)
    _print_candidates(cs)
    top = cfg.export.auto_render_top if a.top is None else a.top
    print(f"project: {p.id}")
    if top <= 0:
        print(f"next: python -m app.cli review --project {p.id}")
        return 0
    ids = st.default_selection(p, top)
    results = st.render(p, ids, force=a.force)
    return 1 if _print_results(results) else 0


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
        c = st.edit_clip(p, a.clip, start=a.start, end=a.end, crop_center_x=a.crop_x, layout=a.layout,
                         caption_preset=a.caption_preset, facecam_region=fr)
        log.info("updated %s: %.2f-%.2fs", c.id, c.start, c.end)
    cs = st.candidates(p)
    if a.json:
        print(cs.model_dump_json(indent=2))
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
    ids = a.clips or st.default_selection(p, a.top if a.top is not None else cfg.export.auto_render_top)
    if not ids:
        log.error("nothing to export: accept clips with `review --accept` or pass --clips/--top")
        return 1
    return 1 if _print_results(st.render(p, ids, force=a.force)) else 0


def cmd_resume(a) -> int:
    from clipstudio.pipeline.studio import Studio

    cfg = _cfg(a)
    st = Studio(cfg)
    p = st.load(a.project)
    st.analyze(p)
    ids = p.state.data.get("requested_clips") or st.default_selection(p, cfg.export.auto_render_top)
    if not ids:
        return 0
    return 1 if _print_results(st.render(p, ids)) else 0


def cmd_status(a) -> int:
    from clipstudio.pipeline.studio import Studio

    st = Studio(_cfg(a))
    if not a.project:
        for pr in st.list_projects():
            stages = " ".join(f"{k}:{v}" for k, v in pr["stages"].items())
            print(f"{pr['id']:<36} {Path(pr['source'].get('path', '')).name:<30} {stages}")
        return 0
    p = st.load(a.project)
    print(f"project {p.id}\nsource  {p.source_path()}")
    for name, rec in p.state.data["stages"].items():
        print(f"  {name:<14} {rec['status']:<11} {rec.get('seconds') or 0:>7.1f}s  {rec.get('error') or ''}")
    for d in sorted((p.root / "clips").glob("*/state.json")):
        s = json.loads(d.read_text())
        print(f"  clip {s.get('clip_id')}: QC {'passed' if s.get('qc_passed') else 'not passed'}  {s.get('output', '')}")
    return 0


def cmd_ui(a) -> int:
    from clipstudio.server.app import serve

    serve(_cfg(a), a.host, a.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    setup_logging(getattr(a, "verbose", False))
    handlers = {"doctor": cmd_doctor, "process": cmd_process, "review": cmd_review, "export": cmd_export,
                "resume": cmd_resume, "status": cmd_status, "ui": cmd_ui}
    try:
        return handlers[a.cmd](a)
    except Cancelled as e:
        log.error(str(e))
        return 130
    except KeyboardInterrupt:
        log.error("cancelled")
        return 130
    except ClipStudioError as e:
        log.error(str(e))
        return 2
    except ValueError as e:  # config validation
        log.error("invalid configuration: %s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
