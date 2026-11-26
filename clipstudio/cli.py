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

    p = sub.add_parser("supervise", help="supervisor review of rendered clips: list pending, approve or reject")
    _common(p)
    p.add_argument("--project", help="project id (with --clip)")
    p.add_argument("--clip", help="clip id to give a verdict on")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--approve", action="store_true")
    g.add_argument("--reject", action="store_true")
    p.add_argument("--reason", default="", help="why (required for a verdict)")
    p.add_argument("--title", help="burn this title into the clip before approving (re-renders it)")
    p.add_argument("--post-title", help="caption to post with")
    p.add_argument("--reviewer", default="supervisor")

    p = sub.add_parser("retention", help="free disk: list (or with --apply delete) local footage that can no "
                       "longer become a post; `retention posted FILE --platform X` records a post")
    _common(p)
    p.add_argument("action", nargs="?", choices=["sweep", "posted"], default="sweep")
    p.add_argument("file", nargs="?", help="(posted) the approved clip's file name")
    p.add_argument("--platform", dest="posted_platform", help="(posted) youtube, tiktok, instagram, ...")
    p.add_argument("--url", default="", help="(posted) link to the post")
    p.add_argument("--apply", action="store_true", help="(sweep) delete; without it nothing is touched")
    p.add_argument("--manifest", type=Path, help="(sweep) write every path that would go to this file")

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

    p = sub.add_parser("production", help="one-command start / safe stop of the whole studio (streams, edits, "
                       "uploads); remembers the lineup for next time")
    psub = p.add_subparsers(dest="action", required=True)
    for name, hlp in (("start", "open the server and browser, resume the last channels (stand-ins for offline "
                                "ones) and finish what the last stop left"),
                      ("stop", "stop recording, finish or save every clip in flight, say how long, then close"),
                      ("status", "what production is doing and what the next start will resume"),
                      ("toggle", "one button: safe stop if production is running, else start it"),
                      ("supervise", "run the server and restart it if it crashes (used by start)"),
                      ("shortcuts", "put Start / Stop / Status shortcuts on the Windows desktop")):
        q = psub.add_parser(name, help=hlp)
        _common(q)
        q.add_argument("--port", type=int, help="server port (default: the remembered one, else 8765)")
        if name == "start":
            q.add_argument("--channel", action="append", metavar="URL",
                           help="watch these instead of the saved lineup (repeatable; becomes the new lineup)")
            q.add_argument("--no-browser", action="store_true", help="don't open the workstation")
        if name == "stop":
            q.add_argument("--now", action="store_true", help="finish only the clip being edited; save the rest "
                           "for the next start")
            q.add_argument("--no-wait", action="store_true", help="ask it to stop and return the estimate at once")
            q.add_argument("--keep-server", action="store_true", help="leave the server open after stopping")

    p = sub.add_parser("live", help="watch a live stream; keep and edit only the moments that pop off")
    _common(p)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--url", action="append", help="live stream URL (Twitch, YouTube, Kick...); repeat to watch "
                                                    "several streams at once")
    src.add_argument("--all", action="store_true", help="watch every stream URL listed in live.channels")
    src.add_argument("--input", type=Path, help="a recorded file played back in real time (dry run / test)")
    p.add_argument("--chat", help="auto (from the URL, default), none, twitch:<channel>, youtube, or a chat export "
                                  "file (played back with --input)")
    p.add_argument("--channel", help="streamer's channel name (for the upload allowlist); found from the URL if omitted")
    p.add_argument("--out", type=Path, help="where moments and clips go (default <data_dir>/live/<channel>)")
    p.add_argument("--duration", type=float, default=0, help="stop after this many seconds (0 = until the stream ends)")
    p.add_argument("--max-moments", type=int, help="stop after this many moments")
    p.add_argument("--no-process", action="store_true", help="only save the moments; don't edit them")
    p.add_argument("--fast", action="store_true", help="with --input: read the file as fast as possible, not in real time")

    p = sub.add_parser("formats", help="original formats: weekly top 5 (narrated long-form + Shorts), news Shorts")
    fsub = p.add_subparsers(dest="action", required=True)
    q = fsub.add_parser("top5", help="build a weekly top 5 episode from a spec (JSON)")
    q.add_argument("spec", type=Path)
    q.add_argument("--out", type=Path, required=True)
    q.add_argument("--no-shorts", action="store_true", help="skip the vertical Short cut-downs")
    q.add_argument("--voice", help="force a voice engine: edge, kokoro or silent (dry run)")
    q = fsub.add_parser("news", help="build a GTA 6 news / easter-egg Short from a spec (JSON)")
    q.add_argument("spec", type=Path)
    q.add_argument("--out", type=Path, required=True)
    q.add_argument("--voice", help="force a voice engine: edge, kokoro or silent (dry run)")
    q = fsub.add_parser("draft", help="top 5 spec skeleton from the newest approved clips (script left as TODO)")
    q.add_argument("--approved", type=Path, required=True, help="the approved clips folder (has MAP.txt)")
    q.add_argument("--out", type=Path, required=True)
    q = fsub.add_parser("voice", help="speak a line with the narrator voice (to audition voices)")
    q.add_argument("text")
    q.add_argument("--out", type=Path, required=True, help="output .wav")
    q.add_argument("--engine", default="edge")
    q.add_argument("--edge-voice")
    for q in fsub.choices.values():
        q.add_argument("--json", action="store_true")
        q.add_argument("-v", "--verbose", action="store_true")
    return ap


def cmd_formats(a) -> int:
    import shutil as _sh

    from clipstudio.formats import news, top5
    from clipstudio.formats.voice import VoiceCfg, speak

    vo = {"engines": [a.voice]} if getattr(a, "voice", None) else None
    if a.action == "top5":
        res = top5.build(a.spec, a.out, shorts=not a.no_shorts, voice_override=vo)
    elif a.action == "news":
        res = news.build(a.spec, a.out, voice_override=vo)
    elif a.action == "draft":
        res = {"spec": str(top5.draft(a.approved, a.out))}
    else:
        d = {"engines": [a.engine]}
        if a.edge_voice:
            d["edge_voice"] = a.edge_voice
        wav = speak(a.text, VoiceCfg.from_dict(d, cache_dir=a.out.parent / "voice_cache"))
        a.out.parent.mkdir(parents=True, exist_ok=True)
        _sh.copy2(wav, a.out)
        res = {"wav": str(a.out)}
    print(json.dumps(res, indent=2, default=str, ensure_ascii=False))
    return 0


def cmd_live(a) -> int:
    cfg = _cfg(a)
    if a.max_moments is not None:
        cfg.live.max_moments = a.max_moments
    urls = list(cfg.live.channels) if a.all else (a.url or [])
    if a.all and not urls:
        log.error("live --all: no stream URLs in live.channels")
        return 2
    if len(urls) > 1:
        return _live_many(a, cfg, urls)
    return _live_one(a, cfg, urls[0] if urls else None)


def _live_session(a, cfg, url: str | None, out: Path | None = None):
    import time as _time

    from clipstudio.live.buffer import Recorder
    from clipstudio.live.chat import channel_from_url, make_chat_source, youtube_channel
    from clipstudio.live.session import LiveSession

    channel = a.channel if len(a.url or []) <= 1 and not a.all else None
    if channel is None and url:
        channel = channel_from_url(url) or (youtube_channel(url) if "youtu" in url else None)
    channel = channel.lower().lstrip("@") if channel else None
    base = Path(cfg.paths.data_dir).expanduser().resolve() / "live"
    out = out or base / f"{channel or 'stream'}_{_time.strftime('%Y%m%d_%H%M%S')}"
    rec = Recorder(out / "buffer", cfg.live.segment_seconds, file=None if url else a.input, url=url,
                   realtime=not a.fast)
    chat = make_chat_source(a.chat if not a.all and len(a.url or []) <= 1 else None, url, out)
    allowed = channel in {c.lower().lstrip("@") for c in cfg.upload.allow_channels} if channel else False
    log.info("live: %s | channel %s (%s) | chat %s | buffer %gs | moments %gs before + %gs after",
             url or a.input, channel or "unknown", "on the upload allowlist" if allowed else "review only",
             chat.name, cfg.live.buffer_seconds, cfg.live.pre_seconds, cfg.live.post_seconds)
    return LiveSession(cfg, rec, chat, out, channel=channel, process=False if a.no_process else None,
                       max_seconds=a.duration)


def _print_moments(moments, out) -> None:
    for m in moments:
        print(f"{m.id}  {m.status:10s}  score {m.trigger['score']:.2f}  {', '.join(m.trigger['reasons'])}  "
              f"{m.clip or m.path or ''}  [{m.upload}]")
    print(f"queue: {out / 'queue.json'}")


def _live_one(a, cfg, url: str | None) -> int:
    sess = _live_session(a, cfg, url, a.out)
    moments = sess.run()
    doc = {"out": str(sess.out), "channel": sess.channel, "stopped": sess.stopped_reason,
           "segments_discarded": sess.ring.deleted, "peak_buffer_mb": round(sess.peak_disk / 1e6, 1),
           "moments": [m.__dict__ for m in moments]}
    if a.json:
        _emit(a, doc)
    else:
        _print_moments(moments, sess.out)
    return 0 if all(m.status in ("done", "saved") for m in moments) else 1


def _live_many(a, cfg, urls: list[str]) -> int:
    """Several streams at once: one recorder + trigger per stream, editors sharing live.max_parallel_edits."""
    import threading

    if len(urls) > cfg.live.max_streams:
        log.error("%d streams asked for; live.max_streams is %d", len(urls), cfg.live.max_streams)
        return 2
    sessions, threads, errors = [], [], {}
    for u in urls:
        try:
            sessions.append(_live_session(a, cfg, u))
        except Exception as e:  # one bad URL does not stop the others
            errors[u] = str(e)
            log.error("could not start %s: %s", u, e)

    def run(s):
        try:
            s.run()
        except Exception as e:
            errors[str(s.rec.url)] = str(e)
            log.error("%s stopped: %s", s.rec.url, e)

    for s in sessions:
        t = threading.Thread(target=run, args=(s,), daemon=True, name=f"live-{s.channel}")
        t.start()
        threads.append(t)
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
    except KeyboardInterrupt:
        for s in sessions:
            s.stop()
        for t in threads:
            t.join()
    docs = [{"out": str(s.out), "channel": s.channel, "stopped": s.stopped_reason,
             "moments": [m.__dict__ for m in s.moments]} for s in sessions]
    if a.json:
        _emit(a, {"streams": docs, "errors": errors})
    else:
        for s in sessions:
            print(f"== {s.channel or s.rec.url}: {s.stopped_reason}")
            _print_moments(s.moments, s.out)
        for u, e in errors.items():
            print(f"!! {u}: {e}")
    ok = all(m.status in ("done", "saved") for s in sessions for m in s.moments)
    return 0 if ok and not errors else 1


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


def cmd_supervise(a) -> int:
    """List the clips waiting for review, or record a verdict on one."""
    from clipstudio.pipeline.studio import Studio
    from clipstudio.supervise import pending

    st = Studio(_cfg(a))
    if not a.clip:
        rows = pending(st.data_dir)
        if a.json:
            _emit(a, {"pending": rows})
            return 0
        for d in rows:
            print(f"{d.get('project', ''):<32} {d.get('clip_id', ''):<10} {d.get('channel') or '':<14} "
                  f"score {d.get('score', 0):.2f}  QC {d.get('qc', '')}  sheet {d.get('sheet', '')}")
            print(f"    {d.get('picker', '')[-160:]}")
        print(f"{len(rows)} clip(s) waiting for review")
        return 0
    if not (a.project and (a.approve or a.reject) and a.reason.strip()):
        raise SystemExit("a verdict needs --project, --clip, --approve or --reject, and --reason")
    p = st.load(a.project)
    doc = st.supervise(p, a.clip, bool(a.approve), a.reason.strip(), a.reviewer, a.title, a.post_title)
    _emit(a, doc)
    if not a.json:
        print(f"{doc['status']}: {doc['clip']}")
    return 0


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


def cmd_production(a) -> int:
    from clipstudio.production import agent

    res = {"start": agent.start, "stop": agent.stop, "status": agent.status, "supervise": agent.supervise,
           "toggle": agent.toggle, "shortcuts": agent.shortcuts}[a.action](a)
    if isinstance(res, int):
        return res
    _emit(a, res)
    return 0


def cmd_ui(a) -> int:
    from clipstudio.server.app import serve

    serve(_cfg(a), a.host, a.port)
    return 0


def cmd_retention(a) -> int:
    """Show (or delete) what the retention rules would remove; or record that a clip was posted."""
    from clipstudio import retention

    cfg = _cfg(a)
    data_dir = Path(cfg.paths.data_dir).expanduser().resolve()
    if a.action == "posted":
        if not a.file or not a.posted_platform:
            raise ClipStudioError("retention posted needs FILE and --platform")
        retention.mark_posted(data_dir, a.file, a.posted_platform, a.url)
        _emit(a, {"posted": a.file, "platform": a.posted_platform})
        return 0
    pl = retention.plan(cfg, data_dir)
    if a.manifest:
        a.manifest.write_text("".join(f"{x.size}\t{x.reason}\t{x.path}\n" for x in pl.actions), encoding="utf-8")
    doc = {"plan": pl.summary()}
    if a.apply:
        doc["result"] = retention.apply(pl)
    if a.json:
        _emit(a, doc)
    else:
        for why, s in doc["plan"]["by_reason"].items():
            print(f"{s['gb']:>8.2f} GB {s['files']:>6} files  {why}")
        print(f"{doc['plan']['gb']:>8.2f} GB total" + ("" if a.apply else "  (dry run: add --apply to delete)"))
        if a.apply:
            print(f"freed {doc['result']['freed_gb']} GB, {doc['result']['failed_count']} file(s) could not be deleted")
    return 0


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    setup_logging(getattr(a, "verbose", False))
    install_interrupt_handlers()
    register_cuda_dlls()
    handlers = {"doctor": cmd_doctor, "process": cmd_process, "review": cmd_review, "export": cmd_export,
                "resume": cmd_resume, "status": cmd_status, "ui": cmd_ui, "production": cmd_production,
                "live": cmd_live, "supervise": cmd_supervise, "formats": cmd_formats,
                "retention": cmd_retention}
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
