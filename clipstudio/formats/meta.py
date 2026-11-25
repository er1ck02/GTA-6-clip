"""Upload metadata and the channel's hard rules, checked before anything is rendered.

Rules (owner's, all channels): never say "rp"/"role play" (it is GTA 6); every clip credits its streamer with
a link; tags on every upload; first video of a format goes up unlisted for review; no misleading titles (the
title must describe what is in the video: checked by a person, the build only checks the mechanical rules).
"""
from __future__ import annotations

import json
from pathlib import Path

from clipstudio.supervise import NOT_RP

BASE_TAGS = ["gta 6", "gta6", "gta vi", "gta 6 clips", "gta 6 gameplay", "gta 6 funny moments", "gta 6 streamer",
             "gta 6 highlights"]
TOP5_TAGS = ["gta 6 top 5", "gta 6 best moments", "top 5 gta 6 moments", "gta 6 funniest moments",
             "gta 6 streamer moments", "gta 6 weekly"]
NEWS_TAGS = ["gta 6 news", "gta 6 update", "gta 6 easter eggs", "gta 6 secrets", "gta 6 facts", "gta 6 patch notes"]
TAG_CHARS = 480          # YouTube allows 500 characters of tags in total


class RuleError(ValueError):
    pass


def check_text(*texts: str, where: str = "") -> None:
    for t in texts:
        if t and NOT_RP.search(t):
            raise RuleError(f"{where or 'text'} says 'rp'/'role play' ({t[:60]!r}): it is GTA 6, never say that")


def check_todo(*texts: str, where: str = "") -> None:
    for t in texts:
        if t is None or not str(t).strip() or "TODO" in str(t):
            raise RuleError(f"{where} still has a TODO or is empty")


def tags(*groups: list[str]) -> list[str]:
    out, seen, total = [], set(), 0
    for g in groups:
        for t in g:
            t = " ".join(str(t).lower().split())
            if not t or t in seen:
                continue
            cost = len(t) + (2 if " " in t else 0) + 1
            if total + cost > TAG_CHARS:
                return out
            seen.add(t)
            out.append(t)
            total += cost
    check_text(*out, where="tags")
    return out


def stamp(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def write_upload(out_dir: Path, *, file: Path, title: str, description: str, tags_: list[str], short: bool,
                 thumbnail: Path | None = None, visibility: str = "unlisted") -> Path:
    check_text(title, description, where="upload text")
    if len(title) > 100:
        raise RuleError(f"title over YouTube's 100 characters: {title!r}")
    if not tags_:
        raise RuleError("every upload needs tags")
    doc = {"channel": "Ricky Spanish (@gta6clipsnn, UCXFO0tCcgzXyZMZC3hKKI0Q) ONLY, never @wettoefungus",
           "file": str(file), "title": title, "description": description, "tags": tags_,
           "visibility": visibility, "short": short, "category": "Gaming", "made_for_kids": False,
           "paid_promotion": False, "altered_or_synthetic": False,
           "thumbnail": str(thumbnail) if thumbnail else None}
    p = out_dir / ("upload.json" if not short else f"{file.stem}.upload.json")
    p.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return p
