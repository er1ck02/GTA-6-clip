"""GTA 6 news / easter-egg Shorts: our own script, spoken by our narrator, with the source on screen.

Facts are never invented: every beat that states a fact (``"claim": true``, the default) must name at least one
entry of ``sources`` (publication + date + URL), and the build refuses otherwise. Hedged reports (a player
claims, "reportedly") must be said as hedged in the script; the build can't judge wording, the writer must.

Spec (JSON, paths relative to the spec file)::

    {
      "title": "gta 6's first patch fixed 7 things (one bug is still there) 😳",
      "kicker": "gta 6 news",
      "sources": {"psl": {"name": "PlayStation LifeStyle, Nov 23", "url": "https://..."}},
      "footage": [{"streamer": "Ludwig", "link": "youtube.com/@ludwig"}],
      "clip": "approved/ludwig_race.mp4",
      "beats": [
        {"say": "narration", "show": "short on-screen text", "sources": ["psl"], "clip": "...", "in": 4.0},
        {"say": "follow for more GTA 6 news", "show": "follow for more", "claim": false}
      ]
    }

Layout (1080x1920): on-screen text on top, the gameplay unzoomed in a window, the source line under it, all
over a blurred copy of the gameplay. The streamer's mic is muted; only our narration plays.
"""
from __future__ import annotations

import json
from pathlib import Path

from clipstudio.core.logging import get_logger
from clipstudio.formats import compose as C
from clipstudio.formats import meta, style
from clipstudio.formats.voice import VoiceCfg, duration, speak
from clipstudio.media.ffmpeg import probe

log = get_logger("news")
TALL = (1080, 1920)
GAME_BOX = (40, 880, 1000, 562)
MAX_SECONDS = 60.0


def load(spec_path: Path) -> dict:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    base = Path(spec_path).parent

    def fix(p):
        p = Path(p)
        return str(p if p.is_absolute() else base / p)
    if spec.get("clip"):
        spec["clip"] = fix(spec["clip"])
    for b in spec.get("beats", []):
        if b.get("clip"):
            b["clip"] = fix(b["clip"])
    return spec


def validate(spec: dict) -> None:
    beats = spec.get("beats") or []
    if not beats:
        raise meta.RuleError("a news Short needs beats")
    srcs = spec.get("sources") or {}
    meta.check_todo(spec.get("title"), where="title")
    meta.check_text(spec["title"], spec.get("kicker", ""), where="title")
    for k, s in srcs.items():
        if not str(s.get("url", "")).startswith("http") or not s.get("name"):
            raise meta.RuleError(f"source {k!r} needs a name and an http(s) url")
    if not spec.get("footage"):
        raise meta.RuleError("credit the gameplay footage: 'footage': [{streamer, link}]")
    for i, b in enumerate(beats, 1):
        w = f"beat {i}"
        meta.check_todo(b.get("say"), b.get("show"), where=w)
        meta.check_text(b["say"], b["show"], where=w)
        if b.get("claim", True):
            ids = b.get("sources") or []
            if not ids:
                raise meta.RuleError(f"{w} states a fact but cites no source (mark \"claim\": false if it doesn't)")
            missing = [x for x in ids if x not in srcs]
            if missing:
                raise meta.RuleError(f"{w} cites unknown source(s) {missing}")
        clip = b.get("clip") or spec.get("clip")
        if not clip or not Path(clip).exists():
            raise meta.RuleError(f"{w}: gameplay clip not found: {clip}")


def build(spec_path: Path, out_dir: Path, *, voice_override: dict | None = None) -> dict:
    spec = load(spec_path)
    validate(spec)
    out_dir = Path(out_dir)
    parts, gfx = out_dir / "_parts", out_dir / "gfx"
    vcfg = VoiceCfg.from_dict({**(spec.get("voice") or {}), **(voice_override or {})},
                              cache_dir=out_dir.parent / "voice_cache")
    enc = C.Enc(encoder=spec.get("encoder", "libx264"))
    srcs = spec.get("sources") or {}
    segs, cursor = [], {}
    for i, b in enumerate(spec["beats"], 1):
        clip = Path(b.get("clip") or spec["clip"])
        lay = C.Layout.for_source(clip, b.get("layout") or spec.get("layout")).with_mask(
            b.get("mask") if b.get("mask") is not None else spec.get("mask"))
        narr = speak(b["say"], vcfg)
        dur = duration(narr) + float(b.get("pause", 0.35))
        t = float(b["in"]) if b.get("in") is not None else cursor.get(str(clip), 0.0)
        clip_d = probe(clip).duration
        if t + dur > clip_d:           # loop back rather than run off the end
            t = 0.0 if dur >= clip_d else max(0.0, clip_d - dur)
        cursor[str(clip)] = t + dur
        src = ", ".join(srcs[x]["name"] for x in (b.get("sources") or []) if x in srcs)
        foot = b.get("footage") or " / ".join(f["streamer"] for f in spec["footage"])
        card = style.news_card(gfx / f"beat{i}.png", spec.get("kicker", "gta 6 news"), b["show"], src, _box(),
                               footage=foot)
        segs.append(C.card(parts / f"beat{i:02d}.mp4", bg_clip=clip, bg_t=t, layout=lay, overlays=[card],
                           narration=narr, dur=dur, size=TALL, enc=enc, game_box=GAME_BOX, blur=22, dim=-0.25,
                           lead=0.1))
    name = spec.get("slug") or "news_short"
    video = C.concat(segs, out_dir / f"{name}.mp4")
    total = C.seg_duration(video)
    if total > MAX_SECONDS + 1:
        log.warning("news Short is %.0f s (over %d s): trim the script", total, MAX_SECONDS)
    desc = _description(spec)
    tg = meta.tags(meta.NEWS_TAGS, spec.get("extra_tags", []), meta.BASE_TAGS,
                   [f["streamer"].lower() for f in spec["footage"]])
    up = meta.write_upload(out_dir, file=video, title=spec["title"], description=desc, tags_=tg, short=True,
                           visibility=spec.get("visibility", "unlisted"))
    result = {"video": str(video), "duration": round(total, 1), "upload": str(up)}
    (out_dir / f"{name}.build.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _box() -> tuple[int, int, int, int]:
    x, y, w, h = GAME_BOX
    return (x, y, x + w, y + h)


def _description(spec: dict) -> str:
    lines = [spec.get("summary", "").strip(), ""] if spec.get("summary") else []
    lines += ["Sources:"] + [f"- {s['name']}: {s['url']}" for s in (spec.get("sources") or {}).values()]
    lines += ["", "Gameplay footage (go support them):"]
    for f in spec["footage"]:
        l = f["link"] if f["link"].startswith("http") else "https://" + f["link"]
        lines.append(f"- {f['streamer']}: {l}")
    lines += ["", "Script and narration are ours. #gta6 #gtavi #gta6news #shorts"]
    return "\n".join(lines)
