"""Stream chat burned into the clip: the last few messages, bottom-left, rolling up as new ones arrive.

Written as its own ASS file (rendered by libass before the captions), so caption QC is not affected. The
block sits between the captions and the bottom safe margin (where the apps draw their own text), on the left:
the right-hand side of a short is covered by the like/comment buttons.

Messages are text only. Emote images are not drawn: chat sources give emotes as text codes, and codes that
are not real words (":face-blue-smiling:") are removed so they do not show up as noise.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from clipstudio.captions.engine import _ass_escape, _Measurer, _ts_ass, resolve_font
from clipstudio.config import ChatOverlayCfg
from clipstudio.core.models import ChatMessage, CropPlan

# readable on any footage with a dark outline (BGR hex, as ASS wants it)
NAME_COLOURS = ["4545FF", "FF901E", "7FFF00", "B469FF", "00D7FF", "E22B8A", "71B33C", "00A5FF", "D4FF7F", "CD5A6A"]
MIN_GAP = 0.3       # seconds between two new lines, so a burst rolls instead of flickering
MAX_LAG = 3.0       # a message that would show this much later than it was sent is dropped (very busy chat)
SHORTCODE = re.compile(r":[A-Za-z0-9_\-+]{2,40}:")
BOTS = {"nightbot", "fossabot", "streamelements", "streamlabs", "moobot", "wizebot", "soundalerts", "sery_bot"}


def is_noise(user: str, text: str) -> bool:
    """Bot announcements and chat commands (``!giveaway``) are not reactions."""
    u = user.lstrip("@").lower()
    return text.lstrip().startswith("!") or u in BOTS or u.endswith("bot")


def clean_text(text: str) -> str:
    t = SHORTCODE.sub(" ", text)
    t = "".join(ch for ch in t if 0x20 <= ord(ch) <= 0x2FFF and ch != "�")
    return re.sub(r"\s+", " ", t).strip()


def _colour(user: str) -> str:
    return NAME_COLOURS[int(hashlib.sha1(user.encode("utf-8", "replace")).hexdigest(), 16) % len(NAME_COLOURS)]


def schedule(messages: list[ChatMessage], dur: float) -> list[tuple[float, ChatMessage]]:
    """(time the line appears, message) for clip-relative messages; messages from just before the clip
    (negative times) are on screen from the first frame."""
    out: list[tuple[float, ChatMessage]] = []
    last = -1e9
    for m in sorted(messages, key=lambda m: m.t):
        if m.t >= dur:
            break
        if m.t < 0:
            out.append((0.0, m))
            continue
        show = max(m.t, last + MIN_GAP)
        if show - m.t > MAX_LAG or show >= dur:
            continue
        out.append((round(show, 3), m))
        last = show
    return out


def chat_region(plan: CropPlan, cfg: ChatOverlayCfg, caption_bottom: int | None = None) -> tuple[int, int, int, float, int, int]:
    """(left x, bottom y, lines, max line width, font size, line height) of the chat block, in output pixels."""
    W, H = plan.out_width, plan.out_height
    fs = max(12, int(round(cfg.size * H)))
    line_h = int(round(fs * 1.32))
    bottom = int(cfg.bottom * H)
    lines = cfg.lines
    max_w = cfg.width * W
    face = next((p.dest for p in plan.panels if p.name == "facecam"), None)
    in_face = cfg.position == "facecam" and face is not None and face.w == W
    if in_face:
        # bottom-left corner of the streamer's section: off the gameplay, under the face, clear of the captions
        bottom = face.y + face.h - int(0.01 * H)
        lines = max(1, min(lines, int(face.h * 0.45) // line_h))
        max_w = min(max_w, 0.56 * W)
    elif caption_bottom is not None and caption_bottom < bottom:  # stay clear of captions sitting above the block
        lines = max(0, min(lines, (bottom - caption_bottom - int(0.012 * H)) // line_h))
    return int(cfg.margin_x * W), bottom, lines, max_w, fs, line_h


def write_chat_ass(messages: list[ChatMessage], plan: CropPlan, cfg: ChatOverlayCfg, dur: float, path: Path,
                   caption_bottom: int | None = None) -> int:
    """Write the chat overlay for one clip. ``messages``: clip-relative times (negative = just before the clip).
    ``caption_bottom``: y of the caption baseline, which the block stays below. Returns the number of lines
    drawn (0 = nothing written worth showing; the file is still created, empty of events)."""
    W, H = plan.out_width, plan.out_height
    x, bottom, lines, max_w, fs, line_h = chat_region(plan, cfg, caption_bottom)
    family, font_file = resolve_font(tuple(cfg.font), True)
    meas = _Measurer(font_file, fs)

    rows: list[tuple[float, str]] = []
    for show, m in schedule(messages, dur):
        text = clean_text(m.text)
        if not text or is_noise(m.user, m.text):
            continue
        user = clean_text(m.user).lstrip("@")[:18] or "chat"
        while len(text) > 3 and meas.width(f"{user}: {text}") * 1.04 > max_w:
            text = text[: max(3, int(len(text) * 0.85))].rstrip() + "…"
        rows.append((show, f"{{\\c&H{_colour(user)}&}}{_ass_escape(user)}{{\\c&HFFFFFF&}}: {_ass_escape(text)}"))
    # lines already on screen at 0 s: only the newest ones fit
    n_pre = sum(1 for show, _ in rows if show == 0.0)
    rows = rows[max(0, n_pre - lines):] if n_pre > lines else rows

    events: list[str] = []
    if lines > 0 and len(rows) >= cfg.min_messages:
        times = [t for t, _ in rows]
        for j, (t0, text) in enumerate(rows):
            for k in range(lines):  # slot 0 is the newest line, at the bottom
                if j + k >= len(rows) and k > 0:
                    break
                start = times[j + k] if j + k < len(times) else t0
                nxt = times[j + k + 1] if j + k + 1 < len(times) else dur
                end = min(nxt, t0 + cfg.max_age, dur)
                if start >= min(t0 + cfg.max_age, dur):
                    break  # too old to still be on screen
                if end - start < 0.04:
                    continue  # pushed up again at once (two lines at the same instant)
                y = bottom - k * line_h
                alpha = f"\\alpha&H{min(160, 30 * k):02X}&" if k else ""
                if k == 0:
                    tag = f"\\an1\\pos({x},{y})" + ("\\fad(140,0)" if t0 > 0 else "")
                else:
                    tag = f"\\an1\\move({x},{y + line_h},{x},{y},0,140){alpha}"
                events.append(f"Dialogue: 0,{_ts_ass(start)},{_ts_ass(end)},Chat,,0,0,0,,{{{tag}}}{text}")
                if end >= min(t0 + cfg.max_age, dur):
                    break
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Chat,{family},{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,{max(2.0, fs * 0.09):.1f},{max(1.0, fs * 0.04):.1f},1,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n".join(events) + ("\n" if events else ""), encoding="utf-8")
    return len(rows) if events else 0


# ------------------------------------------------------------------ picture renderer (emotes as images)
VIDEO_FPS = 12
SLIDE = 0.15   # seconds a new line takes to push the others up


def _rgb(bgr_hex: str) -> tuple[int, int, int]:
    return int(bgr_hex[4:6], 16), int(bgr_hex[2:4], 16), int(bgr_hex[0:2], 16)


def _emote_frames(path: Path, height: int) -> tuple[list, list[float]] | None:
    """(frames as RGBA images of ``height``, seconds each is shown) for an emote picture; None if unreadable."""
    try:
        from PIL import Image, ImageSequence
        im = Image.open(path)
        frames, durs = [], []
        for fr in ImageSequence.Iterator(im):
            rgba = fr.convert("RGBA")
            w = max(1, min(int(round(rgba.width * height / max(1, rgba.height))), height * 4))
            frames.append(rgba.resize((w, height), Image.LANCZOS))
            durs.append(max(0.02, float(fr.info.get("duration", 80) or 80) / 1000.0))
            if len(frames) >= 90:
                break
        return (frames, durs) if frames else None
    except Exception:  # noqa: BLE001 - a broken or unsupported picture: the word stays text
        return None


def render_chat_video(messages: list[ChatMessage], plan: CropPlan, cfg: ChatOverlayCfg, dur: float, out: Path,
                      emotes=None, caption_bottom: int | None = None) -> dict | None:
    """Draw the chat block as a small transparent video (``out``, QuickTime Animation with alpha) so that emote
    words can be pictures, animated ones included. Returns where it goes (``{"x","y","w","h","lines","emotes"}``)
    or None when there is too little chat to show. ``emotes``: an ``EmoteIndex`` (or None for text only)."""
    import subprocess

    from PIL import Image, ImageDraw, ImageFont

    from clipstudio.media.ffmpeg import ffmpeg_bin

    x0, bottom, lines, max_w, fs, line_h = chat_region(plan, cfg, caption_bottom)
    if lines <= 0:
        return None
    _, font_file = resolve_font(tuple(cfg.font), True)
    try:
        font = ImageFont.truetype(font_file, int(round(fs * 0.9))) if font_file else ImageFont.load_default()
    except OSError:
        font = ImageFont.load_default()
    stroke = max(2, int(round(fs * 0.09)))
    eh = int(round(fs * 1.28))                      # emotes stand a little taller than the text, as in chat
    space = max(3, int(font.getlength(" ")))
    cache: dict[str, tuple[list, list[float]] | None] = {}

    def emote(word: str):
        if emotes is None or not emotes.is_emote(word):
            return None
        if word not in cache:
            pth = emotes.image_path(word)
            cache[word] = _emote_frames(pth, eh) if pth else None
        return cache[word]

    rows = []   # (show time, static strip RGBA, [(x, frames, durations)] animated emotes)
    used = 0
    for show, m in schedule(messages, dur):
        if is_noise(m.user, m.text):
            continue
        words = [w for w in SHORTCODE.sub(" ", m.text).split() if w]
        words = [w for w in words if emote(w) or clean_text(w)]
        if not words:
            continue
        user = clean_text(m.user).lstrip("@")[:18] or "chat"
        strip = Image.new("RGBA", (int(max_w) + 2 * stroke, line_h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(strip)
        ty = (line_h - int(fs * 1.05)) // 2
        label = f"{user}:"
        dr.text((stroke, ty), label, font=font, fill=_rgb(_colour(user)) + (255,), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        cx = stroke + int(font.getlength(label)) + space
        anim = []
        for i, w in enumerate(words):
            em = emote(w)
            wd = em[0][0].width if em else int(font.getlength(clean_text(w)))
            if cx + wd > max_w - (int(font.getlength("…")) if i < len(words) - 1 else 0):
                dr.text((cx, ty), "…", font=font, fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
                break
            if em:
                used += 1
                if len(em[0]) > 1:
                    anim.append((cx, em[0], em[1]))
                else:
                    strip.alpha_composite(em[0][0], (cx, (line_h - eh) // 2))
            else:
                dr.text((cx, ty), clean_text(w), font=font, fill=(255, 255, 255, 255), stroke_width=stroke,
                        stroke_fill=(0, 0, 0, 255))
            cx += wd + space
        rows.append((show, strip, anim))
    n_pre = sum(1 for r in rows if r[0] == 0.0)
    rows = rows[max(0, n_pre - lines):] if n_pre > lines else rows
    if len(rows) < cfg.min_messages:
        return None

    cw, chh = (int(max_w) + 2 * stroke) // 2 * 2 + 2, (lines * line_h + line_h // 2) // 2 * 2
    n_frames = max(1, int(round(dur * VIDEO_FPS)))
    tmp = out.with_name(out.stem + ".partial.mov")
    proc = subprocess.Popen([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-y", "-f", "rawvideo",
                             "-pix_fmt", "rgba", "-s", f"{cw}x{chh}", "-r", str(VIDEO_FPS), "-i", "-",
                             "-c:v", "qtrle", "-pix_fmt", "argb", str(tmp)], stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        times = [r[0] for r in rows]
        for fi in range(n_frames):
            t = fi / VIDEO_FPS
            j_last = max((j for j, ts in enumerate(times) if ts <= t), default=-1)
            canvas = Image.new("RGBA", (cw, chh), (0, 0, 0, 0))
            if j_last >= 0:
                p_in = 1.0 if times[j_last] <= 0 else min(1.0, (t - times[j_last]) / SLIDE)
                for k in range(lines):
                    j = j_last - k
                    if j < 0 or t - times[j] > cfg.max_age:
                        break
                    show, strip, anim = rows[j]
                    y = chh - (k + 1) * line_h + int(round(line_h * (1.0 - p_in))) if k else chh - line_h
                    a = (1.0 - min(160, 30 * k) / 255.0) * (p_in if k == 0 else 1.0)
                    line = strip
                    if anim:
                        line = strip.copy()
                        for ax, frames, durs in anim:
                            tt, total = (t - show) % sum(durs), 0.0
                            idx = 0
                            for q, dq in enumerate(durs):
                                total += dq
                                if tt < total:
                                    idx = q
                                    break
                            line.alpha_composite(frames[idx], (ax, (line_h - eh) // 2))
                    if a < 0.999:
                        line = line.copy() if line is strip else line
                        line.putalpha(line.getchannel("A").point(lambda v, a=a: int(v * a)))
                    if y < chh:
                        canvas.alpha_composite(line.crop((0, 0, min(cw, line.width), line_h)), (0, max(0, y)))
            proc.stdin.write(canvas.tobytes())
        proc.stdin.close()
        err = proc.stderr.read().decode(errors="replace")
        if proc.wait() != 0:
            raise RuntimeError(f"ffmpeg could not write the chat overlay: {err.strip()[-200:]}")
    except BaseException:
        proc.kill()
        raise
    tmp.replace(out)
    return {"x": x0 - stroke, "y": bottom - chh + line_h // 4, "w": cw, "h": chh, "lines": len(rows), "emotes": used}
