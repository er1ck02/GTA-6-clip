"""Supervisor review: nothing is postable until someone has looked at the rendered clip and approved it.

The automatic picker can tell that chat and the streamer reacted; it cannot tell whether the clip is actually
a moment, whether the title fits, or whether it is something that should not be posted. So every rendered
clip gets a review packet next to it:

* ``review.jpg``  a contact sheet of the finished clip (what a viewer will see, in order);
* ``review.json`` what the picker thought (payoff signs, score, the words spoken, QC), and the verdict.

A reviewer (a person, or an assistant session that can look at pictures) reads the packet, watches the clip
if in doubt, and records ``approve`` (optionally with a better title, which is burned in) or ``reject`` with
a reason. Approved clips are copied to ``<data_dir>/approved/`` with a line in ``MAP.txt``; that folder is
the only place an uploader should post from. A clip that is rendered again goes back to "pending".
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from clipstudio.core.logging import get_logger
from clipstudio.media.ffmpeg import ffmpeg_bin, probe, run

log = get_logger("supervise")
NOT_RP = re.compile(r"(?<![a-z])(gta\s*)?(rp|role\s*-?play\w*)(?![a-z])", re.I)   # owner: it is GTA 6, not roleplay
SHEET_COLS, SHEET_ROWS = 6, 2
# what a reviewer checks; all four must hold to approve
PAYOFF_TEST = [
    "a visible game event or a quotable line",
    "a loud streamer reaction within about 2 s of it",
    "makes sense in under 5 s with no backstory",
    "the streamer is on screen",
]
REJECT = ["talk with no event", "menus or loading screens", "long drives", "reading chat", "sponsor segments",
          "needs backstory", "nudity or sexual content"]
TITLE_RULES = ("lowercase, 3-8 words, one emoji at the end (😭 first, then 💀 or 😳), name the streamer the way fans do, "
               "do not spoil the punchline, no Title Case or CAPS, never 'rp' or 'roleplay' (it is GTA 6, not roleplay)")


def packet_paths(clip_file: Path) -> tuple[Path, Path]:
    return clip_file.parent / "review.jpg", clip_file.parent / "review.json"


def write_packet(clip_file: Path, render_key: str, info: dict) -> dict:
    """Create (or keep) the review packet for a rendered clip. A verdict survives only for the same render."""
    sheet, meta = packet_paths(clip_file)
    old = read_packet(clip_file)
    if old and old.get("render_key") == render_key and sheet.exists():
        return old
    dur = max(0.5, probe(clip_file).duration)
    n = SHEET_COLS * SHEET_ROWS
    run([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-y", "-i", str(clip_file),
         "-vf", f"fps={n / dur:.5f},scale=270:480,tile={SHEET_COLS}x{SHEET_ROWS}", "-frames:v", "1", "-q:v", "4", str(sheet)])
    doc = {"status": "pending", "render_key": render_key, "clip": str(clip_file), "sheet": str(sheet),
           "created": time.time(), "reviewer": None, "reason": None, "approve_only_if": PAYOFF_TEST,
           "reject_if": REJECT, "title_rules": TITLE_RULES, **info}
    meta.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return doc


def read_packet(clip_file: Path) -> dict | None:
    _, meta = packet_paths(clip_file)
    try:
        return json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else None
    except (OSError, ValueError):
        return None


def is_approved(clip_file: Path | str | None) -> bool:
    if not clip_file:
        return False
    doc = read_packet(Path(clip_file))
    return bool(doc and doc.get("status") == "approved")


def record_verdict(clip_file: Path, approved: bool, reason: str, reviewer: str, data_dir: Path,
                   post_title: str | None = None) -> dict:
    """Store the verdict; an approved clip is copied to ``<data_dir>/approved`` and listed in its MAP.txt."""
    if approved and post_title and NOT_RP.search(post_title):
        raise ValueError("titles never say 'rp' or 'roleplay': this is GTA 6, not roleplay")
    doc = read_packet(clip_file)
    if doc is None:
        raise FileNotFoundError(f"no review packet next to {clip_file}; render the clip first")
    doc.update(status="approved" if approved else "rejected", reason=reason, reviewer=reviewer, reviewed=time.time())
    if post_title:
        doc["post_title"] = post_title
    _, meta = packet_paths(clip_file)
    meta.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    out = Path(data_dir) / "approved"
    dst = out / clip_file.name
    if approved:
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(clip_file, dst)
        ch = "".join(c for c in str(doc.get("channel") or "").lower() if c.isalnum() or c == "_")
        tags = "#gta6" + (f" #{ch}" if ch else "") + " + #shorts (YouTube) or #fyp (TikTok)"
        line = (f"{dst.name} | title: {doc.get('post_title') or doc.get('title') or ''} | hashtags: {tags}"
                f" | channel: {doc.get('channel') or ''} | approved by {reviewer}: {reason}\n")
        with open(out / "MAP.txt", "a", encoding="utf-8") as f:
            f.write(line)
    elif dst.exists():
        dst.unlink()   # a clip that was approved earlier and is now rejected leaves the folder
    log.info("review: %s %s (%s)", clip_file.name, doc["status"], reason)
    return doc


def pending(data_dir: Path) -> list[dict]:
    """Every review packet under the projects folder that has no verdict yet, oldest first."""
    out = []
    for meta in (Path(data_dir) / "projects").glob("*/clips/*/review.json"):
        try:
            doc = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("status") == "pending" and Path(doc.get("clip", "")).exists():
            out.append(doc)
    return sorted(out, key=lambda d: d.get("created", 0))
