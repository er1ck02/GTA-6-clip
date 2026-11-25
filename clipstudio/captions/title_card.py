"""The clip's title, drawn as a picture so it can look like a hand-typed caption: casual text, emoji included.

libass (which burns the captions) cannot draw colour emoji, and a title like "what did bro just do 😭" is
half emoji. So the title is rendered with Pillow into a transparent PNG and laid over the clip: bold white
text with a black outline, wrapped to at most two lines, emoji drawn from the system's colour emoji font.
"""
from __future__ import annotations

from pathlib import Path

from clipstudio.captions.engine import resolve_font
from clipstudio.core.compat import font_dirs
from clipstudio.core.logging import get_logger

log = get_logger("title")
EMOJI_FONTS = ("seguiemj.ttf", "NotoColorEmoji.ttf", "Apple Color Emoji.ttc")
TEXT_FONTS = ("Arial", "Segoe UI", "Inter", "Roboto", "DejaVu Sans")


def _is_emoji(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x1F000 or 0x2600 <= o <= 0x27BF or o in (0x200D, 0xFE0F) or 0x2190 <= o <= 0x21FF or 0x2B00 <= o <= 0x2BFF


def _emoji_font(size: int):
    from PIL import ImageFont
    for d in font_dirs():
        for name in EMOJI_FONTS:
            f = Path(d) / name
            if f.is_file():
                try:
                    return ImageFont.truetype(str(f), size)
                except OSError:
                    continue
    return None


def _runs(text: str) -> list[tuple[bool, str]]:
    """Split into (is_emoji, run) pieces, keeping emoji sequences (ZWJ, variation selectors) together."""
    out: list[tuple[bool, str]] = []
    for ch in text:
        e = _is_emoji(ch)
        if out and out[-1][0] == e:
            out[-1] = (e, out[-1][1] + ch)
        else:
            out.append((e, ch))
    return out


def render_title(text: str, out: Path, frame_w: int, frame_h: int, size: float = 0.034, max_width: float = 0.9,
                 max_lines: int = 2, min_scale: float = 0.7) -> tuple[int, int] | None:
    """Write the title picture to ``out`` (PNG with alpha). Returns its (width, height), or None for no text."""
    from PIL import Image, ImageDraw, ImageFont

    text = " ".join(text.split())
    if not text:
        return None
    fs0 = max(14, int(round(size * frame_h)))
    _, font_file = resolve_font(TEXT_FONTS, True)
    limit = int(frame_w * max_width)

    def load(px: int):
        try:
            return (ImageFont.truetype(font_file, px) if font_file else ImageFont.load_default()), _emoji_font(px)
        except OSError:
            return ImageFont.load_default(), _emoji_font(px)

    def width(s: str) -> float:
        return sum((emoji.getlength(r) if (e and emoji) else font.getlength(r)) for e, r in _runs(s) if not (e and not emoji))

    # one line keeps the title off the streamer's face: shrink it (down to min_scale) before wrapping
    fs = fs0
    font, emoji = load(fs)
    while width(text) > limit and fs > max(14, int(fs0 * min_scale)):
        fs -= 2
        font, emoji = load(fs)
    stroke = max(3, int(round(fs * 0.11)))

    lines: list[str] = []
    cur = ""
    for word in text.split(" "):
        trial = f"{cur} {word}".strip()
        if cur and width(trial) > limit:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    line_h = int(fs * 1.28)
    w = int(max(width(ln) for ln in lines)) + 4 * stroke
    h = line_h * len(lines) + 3 * stroke
    img = Image.new("RGBA", (w + (w % 2), h + (h % 2)), (0, 0, 0, 0))
    dr = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        x = (img.width - width(ln)) / 2
        y = stroke + i * line_h
        for is_e, run in _runs(ln):
            if is_e:
                if emoji is None:
                    continue  # no colour emoji font on this machine: the words still read on their own
                dr.text((x, y + int(fs * 0.06)), run, font=emoji, embedded_color=True)
                x += emoji.getlength(run)
            else:
                dr.text((x, y), run, font=font, fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
                x += font.getlength(run)
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return img.width, img.height
