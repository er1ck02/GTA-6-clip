"""Title, description and tags for a Short, built from the clip's own title and the streamer's credit.

Every Short credits the streamer by name and links both the source stream and their channel (the channel kit's
rule). YouTube's limits are enforced here so an upload never fails on metadata: title at most 100 characters
and no angle brackets, description at most 5000 bytes, tags at most 500 characters in total.
"""
from __future__ import annotations

import re

from clipstudio.config import YouTubeCfg

TITLE_MAX = 100
DESC_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500


def streamer_credit(cfg: YouTubeCfg, channel: str | None, source_url: str | None) -> tuple[str, str]:
    """(display name, channel link) for a streamer; config overrides first, else derived from the stream URL."""
    ch = (channel or "").lower().lstrip("@")
    over = cfg.streamers.get(ch) if ch else None
    name = (over.name if over and over.name else None) or (ch[:1].upper() + ch[1:] if ch else "the streamer")
    url = over.url if over and over.url else ""
    if not url and ch:
        src = (source_url or "").lower()
        if "twitch.tv" in src:
            url = f"https://www.twitch.tv/{ch}"
        elif "kick.com" in src:
            url = f"https://kick.com/{ch}"
        elif "youtu" in src or not src:
            url = f"https://www.youtube.com/@{ch}"
    return name, url


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("<", "").replace(">", "")).strip()


def _fit_title(template: str, title: str, fields: dict) -> str:
    full = _clean(template.format(title=title, **fields))
    if len(full) <= TITLE_MAX:
        return full
    # shorten the clip title, not the credit or the #shorts tag
    rest = len(_clean(template.format(title="", **fields)))
    room = max(10, TITLE_MAX - rest - 1)
    short = title[:room].rsplit(" ", 1)[0] if " " in title[:room] else title[:room]
    return _clean(template.format(title=short.rstrip(" ,.;:-") + "…", **fields))[:TITLE_MAX]


def _drop_empty_lines(text: str, fields: dict) -> str:
    """Remove template lines whose link is unknown instead of posting 'Clipped from ...: ' with nothing after."""
    out = []
    for line in text.split("\n"):
        if any("{" + k + "}" in line for k in ("source_url", "streamer_url") if not fields.get(k)):
            continue
        out.append(line)
    return "\n".join(out)


def build_metadata(cfg: YouTubeCfg, clip_title: str | None, channel: str | None, source_url: str | None,
                   extra_tags: list[str] | None = None) -> dict:
    """{'title','description','tags'} ready for videos.insert."""
    name, link = streamer_credit(cfg, channel, source_url)
    title = _clean(clip_title or "") or f"{name} GTA moment"
    tags = []
    for t in [*cfg.tags, name if channel else None, *(extra_tags or [])]:
        t = _clean((t or "").lstrip("#").replace(",", " "))
        if t and t.lower() not in {x.lower() for x in tags}:
            tags.append(t)
    hashtags = " ".join("#" + re.sub(r"[^\w]", "", t) for t in tags[:4] if re.sub(r"[^\w]", "", t))
    fields = {"streamer": name, "streamer_url": link, "source_url": source_url or "", "hashtags": hashtags,
              "channel": channel or ""}
    desc_t = _drop_empty_lines(cfg.description_template, fields)
    desc = desc_t.format(title=title, **fields).replace("<", "").replace(">", "").strip()
    while len(desc.encode("utf-8")) > DESC_MAX_BYTES:
        desc = desc[:-50]
    # tags: YouTube counts quotes around tags with spaces, so budget conservatively
    kept, used = [], 0
    for t in tags:
        cost = len(t) + (2 if " " in t else 0) + 1
        if used + cost > TAGS_MAX_CHARS:
            break
        kept.append(t)
        used += cost
    return {"title": _fit_title(cfg.title_template, title, fields), "description": desc, "tags": kept}
