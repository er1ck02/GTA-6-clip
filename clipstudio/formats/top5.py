"""Weekly "top 5 GTA 6 streamer moments": a ranked countdown with our own narration between credited clips.

Episode spec (JSON, paths relative to the spec file)::

    {
      "series": "Top 5 GTA 6 streamer moments",
      "week": "Week of Nov 24, 2026",
      "title": "top 5 gta 6 streamer moments this week (ludwig lost by 0.001s)",
      "hook": "one line for the top of the description",
      "intro": "narration over the title card",
      "outro": "narration over the credits",
      "voice": {"engines": ["edge", "kokoro"], "edge_voice": "en-US-BrianMultilingualNeural"},
      "encoder": "libx264",
      "moments": [
        {"rank": 5, "clip": "approved/ludwig_race.mp4", "in": 0, "out": null, "layout": "auto",
         "streamer": "Ludwig", "link": "youtube.com/@ludwig", "label": "the closest race in Leonida",
         "setup": "narration before the clip", "react": "narration after the clip",
         "short_title": "ludwig lost by 0.001 seconds 😭", "thumb_t": 12.5}
      ]
    }

Output (``out_dir``): ``episode.mp4`` (16:9), ``thumbnail.jpg``, ``upload.json``, and ``shorts/`` with one
vertical cut-down per moment (clip with a rank badge, then our narrated reaction) and its ``*.upload.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

from clipstudio.core.logging import get_logger
from clipstudio.formats import compose as C
from clipstudio.formats import meta, style
from clipstudio.formats.voice import VoiceCfg, duration, speak
from clipstudio.media.ffmpeg import probe

log = get_logger("top5")
WIDE = (1920, 1080)
TALL = (1080, 1920)


def load(spec_path: Path) -> dict:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    base = Path(spec_path).parent
    for m in spec.get("moments", []):
        for k in ("clip", "short_clip"):
            if m.get(k):
                p = Path(m[k])
                m[k] = str(p if p.is_absolute() else (base / p))
    return spec


def validate(spec: dict) -> list[dict]:
    ms = spec.get("moments") or []
    if not 3 <= len(ms) <= 10:
        raise meta.RuleError("an episode needs 3-10 moments")
    ranks = sorted(m.get("rank") for m in ms)
    if ranks != list(range(1, len(ms) + 1)):
        raise meta.RuleError(f"ranks must be 1..{len(ms)} once each, got {ranks}")
    minimal = spec.get("style") == "minimal"
    meta.check_todo(spec.get("title"), *(() if minimal else (spec.get("intro"), spec.get("outro"))), where="episode")
    meta.check_text(spec["title"], spec.get("hook", ""), spec.get("intro", ""), spec.get("outro", ""), where="episode")
    for m in ms:
        w = f"moment #{m['rank']}"
        meta.check_todo(m.get("streamer"), m.get("link"), *(() if minimal else (m.get("setup"), m.get("react"))), where=w)
        meta.check_text(m.get("label", ""), m.get("setup", ""), m.get("react", ""), m.get("short_title", ""), where=w)
        for k in ("clip", "short_clip"):
            if m.get(k) and not Path(m[k]).exists():
                raise meta.RuleError(f"{w}: {k} not found: {m[k]}")
    return sorted(ms, key=lambda m: -m["rank"])      # countdown order: 5 -> 1


def _window(m: dict) -> tuple[float, float]:
    d = probe(Path(m["clip"])).duration
    t_in = float(m.get("in") or 0)
    t_out = float(m["out"]) if m.get("out") else d
    return t_in, min(t_out, d)


def _link(m: dict) -> str:
    l = m["link"].strip()
    return l if l.startswith("http") else "https://" + l


def build(spec_path: Path, out_dir: Path, *, shorts: bool = True, voice_override: dict | None = None) -> dict:
    spec = load(spec_path)
    order = validate(spec)
    out_dir = Path(out_dir)
    seg_dir, gfx = out_dir / "segments", out_dir / "gfx"
    seg_dir.mkdir(parents=True, exist_ok=True)
    vcfg = VoiceCfg.from_dict({**(spec.get("voice") or {}), **(voice_override or {})},
                              cache_dir=out_dir.parent / "voice_cache")
    enc = C.Enc(encoder=spec.get("encoder", "libx264"))
    series = spec.get("series", "Top 5 GTA 6 streamer moments")
    week = spec.get("week", "")
    strip = f"{series} · {week}" if week else series

    segs: list[tuple[str, Path]] = []
    minimal = spec.get("style") == "minimal"
    first = order[0]
    lay0 = C.Layout.for_source(Path(first["clip"]), first.get("layout")).with_mask(first.get("mask"))
    if minimal:
        # owner's format: no voice. Short title card, then each clip with "#N STREAMER" on screen for the first
        # seconds and the credit after it, straight into the next clip, short credits card at the end.
        segs.append(("Intro", C.card(seg_dir / "00_intro.mp4", bg_clip=Path(first["clip"]), bg_t=_window(first)[0],
                                     layout=lay0, overlays=[style.title_card(gfx / "title.png", series, week)],
                                     narration=None, dur=float(spec.get("intro_seconds", 2.5)), size=WIDE, enc=enc,
                                     blur=24)))
        hold = float(spec.get("rank_seconds", 2.5))
        for m in order:
            clip, r = Path(m["clip"]), m["rank"]
            lay = C.Layout.for_source(clip, m.get("layout")).with_mask(m.get("mask"))
            t_in, t_out = _window(m)
            pos = m.get("credit_pos", "tl")
            rk = style.rank_card(gfx / f"rank{r}.png", r, m["streamer"], m.get("label", ""), strip, dim=60)
            lt = style.lower_third(gfx / f"credit{r}.png", r, m["streamer"], m["link"].replace("https://", ""), pos=pos)
            segs.append((f"#{r} {m['streamer']}" + (f": {m['label']}" if m.get("label") else ""),
                         C.wide_clip(seg_dir / f"{r:02d}_clip.mp4", clip=clip, t_in=t_in, t_out=t_out, layout=lay,
                                     overlays=[(rk, 0, hold), (lt, hold, None)], enc=enc, mute=m.get("mute"))))
    else:
        # ---- intro: title card over the first moment's gameplay (blurred, so nothing is spoiled)
        narr = speak(spec["intro"], vcfg)
        segs.append(("Intro", C.card(seg_dir / "00_intro.mp4", bg_clip=Path(first["clip"]), bg_t=_window(first)[0],
                                     layout=lay0, overlays=[style.title_card(gfx / "title.png", series, week)],
                                     narration=narr, dur=max(5.0, duration(narr) + 0.8), size=WIDE, enc=enc, blur=24)))
        for m in order:
            clip, r = Path(m["clip"]), m["rank"]
            lay = C.Layout.for_source(clip, m.get("layout")).with_mask(m.get("mask"))
            t_in, t_out = _window(m)
            narr = speak(m["setup"], vcfg)
            segs.append((f"#{r} {m['streamer']}" + (f": {m['label']}" if m.get("label") else ""),
                         C.card(seg_dir / f"{r:02d}a_setup.mp4", bg_clip=clip, bg_t=t_in, layout=lay,
                                overlays=[style.rank_card(gfx / f"rank{r}.png", r, m["streamer"], m.get("label", ""), strip)],
                                narration=narr, dur=duration(narr) + 0.7, size=WIDE, enc=enc)))
            pos = m.get("credit_pos", "tl")
            lt = style.lower_third(gfx / f"credit{r}.png", r, m["streamer"], m["link"].replace("https://", ""), pos=pos)
            segs.append(("", C.wide_clip(seg_dir / f"{r:02d}b_clip.mp4", clip=clip, t_in=t_in, t_out=t_out, layout=lay,
                                         overlays=[lt], enc=enc, mute=m.get("mute"))))
            last = C.frame_png(clip, t_out - 0.15, gfx / f"last{r}.png", lay, WIDE)
            narr = speak(m["react"], vcfg)
            segs.append(("", C.still_card(seg_dir / f"{r:02d}c_react.mp4", image=last,
                                          overlays=[style.corner_badge(gfx / f"badge{r}.png", r, pos=pos)], narration=narr,
                                          dur=duration(narr) + 0.5, enc=enc, size=WIDE)))
    # ---- outro: credits
    top = order[-1]
    credits, seen = [], set()
    for m in order:
        if m["streamer"] in seen:
            continue
        seen.add(m["streamer"])
        ranks = ", ".join(f"#{x['rank']}" for x in order if x["streamer"] == m["streamer"])
        label = m["streamer"] if len(ranks) > 12 else f"{ranks}  {m['streamer']}"
        credits.append((label, m["link"].replace("https://", "")))
    narr = None if minimal or not spec.get("outro") else speak(spec["outro"], vcfg)
    segs.append(("Credits", C.card(seg_dir / "99_outro.mp4", bg_clip=Path(top["clip"]), bg_t=_window(top)[0],
                                   layout=C.Layout.for_source(Path(top["clip"]), top.get("layout")).with_mask(top.get("mask")),
                                   overlays=[style.end_card(gfx / "end.png", credits,
                                                            spec.get("cta", "subscribe for next week's top 5"))],
                                   narration=narr, dur=(float(spec.get("outro_seconds", 5.0)) if narr is None
                                                        else max(8.0, duration(narr) + 1.5)),
                                   size=WIDE, enc=enc, blur=26)))

    episode = C.concat([p for _, p in segs], out_dir / "episode.mp4")
    chapters, t = [], 0.0
    for name, p in segs:
        if name:
            chapters.append((t, name))
        t += C.seg_duration(p)
    total = t
    if minimal:     # YouTube needs chapters >= 10 s: the first clip starts at 0:00, no Intro/Credits chapters
        chapters = [(a, b) for a, b in chapters if b not in ("Intro", "Credits")]
        if chapters:
            chapters[0] = (0.0, chapters[0][1])

    # ---- thumbnail from the #1 moment
    t_in, t_out = _window(top)
    tt = float(top.get("thumb_t") or (t_in + (t_out - t_in) * 0.6))
    frame = C.frame_png(Path(top["clip"]), tt, gfx / "thumb_src.png",
                        C.Layout.for_source(Path(top["clip"]), top.get("layout")).with_mask(top.get("mask")), WIDE,
                        avoid_mask=True)
    thumb = style.thumbnail(frame, out_dir / "thumbnail.jpg", spec.get("thumb_top", "top 5 this week"),
                            spec.get("thumb_text", f"{top['streamer']} #1?"))

    desc = _description(spec, order, chapters)
    names = list(dict.fromkeys(m["streamer"].lower() for m in order))
    group = meta.TOP5_TAGS if len(order) == 5 else [f"top {len(order)} gta 6 moments", "gta 6 best moments",
                                                    "gta 6 compilation", "gta 6 funniest moments"]
    tg = meta.tags(spec.get("extra_tags", []), group, [f"{n} gta 6" for n in names], names, meta.BASE_TAGS)
    meta.write_upload(out_dir, file=episode, title=spec["title"], description=desc, tags_=tg, short=False,
                      thumbnail=thumb, visibility=spec.get("visibility", "unlisted"))
    result = {"episode": str(episode), "duration": round(total, 1), "thumbnail": str(thumb),
              "chapters": [(meta.stamp(a), b) for a, b in chapters], "shorts": []}
    if shorts:
        result["shorts"] = [str(p) for p in build_shorts(spec, order, out_dir / "shorts", vcfg, enc, strip)]
    (out_dir / "build.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("top 5 built: %s (%.0f s, %d shorts)", episode, total, len(result["shorts"]))
    return result


def _description(spec: dict, order: list[dict], chapters: list[tuple[float, str]]) -> str:
    lines = []
    if spec.get("hook"):
        lines += [spec["hook"], ""]
    lines += [spec.get("about", "The best GTA 6 streamer moments, ranked."), ""]
    lines += [f"{meta.stamp(t)} {name}" for t, name in chapters] + [""]
    lines += ["Clips from (go support them):"]
    seen = set()
    for m in order:
        if m["streamer"] not in seen:
            seen.add(m["streamer"])
            lines.append(f"{m['streamer']}: {_link(m)}")
    ours = "Ranking and edit are ours." if spec.get("style") == "minimal" else "Ranking, script and narration are ours."
    lines += ["", f"All clips belong to the streamers and are credited on screen. {ours} "
                  "Streamers: want a clip removed? Comment or message and it's gone.", "",
              "#gta6 #gtavi #gta6moments"]
    return "\n".join(lines)


def build_shorts(spec: dict, order: list[dict], out_dir: Path, vcfg: VoiceCfg, enc: C.Enc, strip: str) -> list[Path]:
    """One vertical Short per moment: payoff-first clip with a rank badge, then our narrated reaction."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_parts"
    made = []
    for m in order:
        r = m["rank"]
        if m.get("short_clip"):          # e.g. the approved 9:16 render of the moment
            clip = Path(m["short_clip"])
            t_in = float(m.get("short_in") or 0)
            t_out = float(m.get("short_out") or probe(clip).duration)
        else:
            clip = Path(m["clip"])
            t_in, t_out = _window(m)
            t_in = float(m.get("short_in", t_in))
        name = f"top5_{r}_{''.join(c for c in m['streamer'].lower() if c.isalnum())}"
        badge = style.short_badge(tmp / f"{name}_badge.png", r, spec.get("short_series", "this week's top 5 gta 6 moments"),
                                  f"clip: {m['streamer']}", y=int(spec.get("short_badge_y", 1600)))
        a = C.vertical_clip(tmp / f"{name}_a.mp4", clip=clip, t_in=t_in, t_out=t_out, overlays=[badge], enc=enc)
        last = C.frame_png(clip, t_out - 0.15, tmp / f"{name}_last.png", size=TALL)
        narr = speak(m["react"] + " " + spec.get("short_outro", "Full top five is on the channel."), vcfg)
        end = style.short_end(tmp / f"{name}_end.png", f"#{r} this week", "full top 5 on the channel")
        b = C.still_card(tmp / f"{name}_b.mp4", image=last, overlays=[end], narration=narr,
                         dur=duration(narr) + 0.5, enc=enc, size=TALL)
        out = C.concat([a, b], out_dir / f"{name}.mp4")
        title = m.get("short_title") or f"#{r} gta 6 moment this week: {m['streamer'].lower()} 😭"
        desc = (f"#{r} in this week's top 5 GTA 6 streamer moments.\n\nClip: {m['streamer']}, go follow: {_link(m)}\n"
                f"Commentary and ranking by us. Full countdown on the channel.\n\n#gta6 "
                f"#{''.join(c for c in m['streamer'].lower() if c.isalnum())} #shorts")
        tg = meta.tags([m["streamer"].lower(), f"{m['streamer'].lower()} gta 6"], meta.BASE_TAGS, meta.TOP5_TAGS[:3])
        meta.write_upload(out_dir, file=out, title=title, description=desc, tags_=tg, short=True,
                          visibility=spec.get("visibility", "unlisted"))
        made.append(out)
    return made


def draft(approved_dir: Path, out: Path, n: int = 5) -> Path:
    """Spec skeleton from the newest approved clips (MAP.txt). The script lines are TODO: a writer fills them."""
    approved_dir = Path(approved_dir)
    rows = []
    mp = approved_dir / "MAP.txt"
    if mp.exists():
        for line in mp.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = [p.strip() for p in line.split("|")]
            f = approved_dir / parts[0]
            if parts and f.exists():
                kv = dict(p.split(":", 1) for p in parts[1:] if ":" in p)
                rows.append({"file": f, "title": kv.get("title", "").strip(), "channel": kv.get("channel", "").strip()})
    seen, uniq = set(), []
    for r in reversed(rows):
        if r["file"].name not in seen:
            seen.add(r["file"].name)
            uniq.append(r)
    pick = uniq[:n]
    spec = {"series": "Top 5 GTA 6 streamer moments", "week": "TODO week of ...", "title": "TODO",
            "hook": "TODO", "intro": "TODO", "outro": "TODO",
            "voice": {"engines": ["edge", "kokoro"]}, "encoder": "libx264",
            "candidates": [str(r["file"]) for r in uniq[n:n + 10]],
            "moments": [{"rank": n - i, "clip": str(r["file"]), "in": 0, "out": None, "layout": "auto",
                         "streamer": r["channel"] or "TODO", "link": f"youtube.com/@{r['channel'] or 'TODO'}",
                         "label": "TODO", "setup": "TODO", "react": "TODO", "short_title": r["title"]}
                        for i, r in enumerate(pick)]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    return out
