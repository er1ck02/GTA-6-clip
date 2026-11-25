"""Channel look for the original formats: clean sci-fi (the channel art's cyan -> magenta on near-black).

Every graphic is a transparent PNG drawn with Pillow and laid over video by FFmpeg, so no FFmpeg font setup
is needed. No channel name on the art (owner's rule); emoji are never drawn (fonts can't be relied on).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

C1 = (52, 231, 255)      # cyan
C2 = (255, 61, 154)      # magenta
BG = (6, 7, 13)
INK = (244, 246, 255)
MUTED = (170, 182, 210)

_BOLD = ["Inter-ExtraBold.otf", "InterDisplay-Bold.otf", "Inter-Bold.otf", "bahnschrift.ttf", "segoeuib.ttf",
         "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf"]
_DISPLAY = ["impact.ttf", "Anton-Regular.ttf", "BebasNeue-Regular.ttf", "InterDisplay-Black.otf",
            "Inter-Black.otf"] + _BOLD
_DIRS = ["C:/Windows/Fonts", str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"),
         "/usr/share/fonts", "/usr/local/share/fonts", "/Library/Fonts", "/System/Library/Fonts"]


@lru_cache(maxsize=None)
def _font_file(kind: str) -> str | None:
    names = _DISPLAY if kind == "display" else _BOLD
    found: dict[str, str] = {}
    for d in _DIRS:
        if not d or not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                found.setdefault(f.lower(), os.path.join(root, f))
    for n in names:
        if n.lower() in found:
            return found[n.lower()]
    return None


def font(size: int, kind: str = "bold") -> ImageFont.FreeTypeFont:
    f = _font_file(kind)
    if f:
        return ImageFont.truetype(f, size)
    return ImageFont.load_default(size)


def fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int, size: int, kind: str = "bold",
             min_size: int = 18) -> ImageFont.FreeTypeFont:
    while size > min_size:
        f = font(size, kind)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return font(min_size, kind)


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=f) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def gradient_text(img: Image.Image, xy: tuple[int, int], text: str, f: ImageFont.FreeTypeFont,
                  glow: bool = True) -> tuple[int, int]:
    """Draw ``text`` filled with the cyan -> magenta gradient; returns (w, h)."""
    d = ImageDraw.Draw(img)
    l, t, r, b = d.textbbox(xy, text, font=f)
    w, h = r - l, b - t
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=f, fill=255)
    grad = Image.new("RGBA", img.size)
    gd = ImageDraw.Draw(grad)
    for x in range(max(0, l), min(img.width, r + 1)):
        k = (x - l) / max(1, w)
        gd.line([(x, 0), (x, img.height)], fill=tuple(int(C1[i] + (C2[i] - C1[i]) * k) for i in range(3)) + (255,))
    if glow:
        halo = Image.new("RGBA", img.size, C2 + (0,))
        halo.putalpha(mask.filter(ImageFilter.GaussianBlur(14)).point(lambda v: int(v * .55)))
        img.alpha_composite(halo)
    img.paste(grad, (0, 0), mask)
    return w, h


def corners(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int], L: int = 46, width: int = 4,
            color=C1) -> None:
    x0, y0, x1, y1 = box
    for (x, y, sx, sy) in [(x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)]:
        d.line([(x, y + sy * L), (x, y), (x + sx * L, y)], fill=color + (230,), width=width)


def gradient_bar(img: Image.Image, box: tuple[int, int, int, int], alpha: int = 255) -> None:
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    for x in range(x0, x1):
        k = (x - x0) / max(1, x1 - x0)
        d.line([(x, y0), (x, y1)], fill=tuple(int(C1[i] + (C2[i] - C1[i]) * k) for i in range(3)) + (alpha,))


def canvas(w: int, h: int) -> Image.Image:
    return Image.new("RGBA", (w, h), (0, 0, 0, 0))


def save(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


# ------------------------------------------------------------------ 16:9 graphics (1920x1080)

def title_card(path: Path, title: str, kicker: str, w: int = 1920, h: int = 1080) -> Path:
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, h], fill=BG + (120,))
    corners(d, (120, 120, w - 120, h - 120), L=70)
    f = fit_font(d, kicker.upper(), w - 400, 44)
    d.text((w // 2, 300), kicker.upper(), font=f, fill=MUTED + (255,), anchor="mm")
    big = font(150, "display")
    lines = wrap(d, title.upper(), big, w - 360)
    y = h // 2 - (len(lines) * 160) // 2 + 40
    for ln in lines:
        lw = d.textlength(ln, font=big)
        gradient_text(img, (int((w - lw) / 2), y), ln, big)
        y += 160
    gradient_bar(img, (w // 2 - 220, y + 30, w // 2 + 220, y + 38))
    return save(img, path)


def rank_card(path: Path, rank: int, streamer: str, label: str, series: str, w: int = 1920, h: int = 1080,
              dim: int = 110) -> Path:
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, h], fill=BG + (dim,))
    x = 820
    num = fit_font(d, f"#{rank}", x - 150 - 60, 520, "display", min_size=200)
    gradient_text(img, (150, 190 + (520 - num.size) // 3), f"#{rank}", num)
    gradient_bar(img, (x, 400, x + 10, 690))
    f1 = fit_font(d, streamer.upper(), w - x - 160, 150, "display")
    d.text((x + 50, 380), streamer.upper(), font=f1, fill=INK + (255,))
    f2 = font(54)
    for i, ln in enumerate(wrap(d, label, f2, w - x - 200)[:2]):
        d.text((x + 54, 580 + i * 66), ln, font=f2, fill=MUTED + (255,))
    d.rectangle([0, h - 90, w, h], fill=BG + (200,))
    d.text((w // 2, h - 45), series.upper(), font=font(34), fill=MUTED + (255,), anchor="mm")
    gradient_bar(img, (0, h - 92, w, h - 88))
    return save(img, path)


def _place(pos: str, bw: int, bh: int, w: int, h: int, m: tuple[int, int] = (48, 44)) -> tuple[int, int]:
    """Top-left corner for a box of bw x bh at ``pos`` (tl, tr, bl, br)."""
    x = m[0] if pos[1] == "l" else w - m[0] - bw
    y = m[1] if pos[0] == "t" else h - m[1] - bh
    return x, y


def lower_third(path: Path, rank: int | None, streamer: str, link: str, w: int = 1920, h: int = 1080,
                pos: str = "tl") -> Path:
    """Credit shown for the whole clip (default top-left; move it off the streamer's webcam with ``pos``)."""
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    f1, f2 = font(54, "display"), font(30)
    name = streamer.upper()
    tw = max(d.textlength(name, font=f1), d.textlength(link, font=f2))
    bx = 132 if rank is not None else 0
    x0, y0 = _place(pos, int(bx + tw + 60), 120, w, h)
    d.rounded_rectangle([x0, y0, x0 + bx + tw + 60, y0 + 120], radius=18, fill=BG + (205,))
    if rank is not None:
        gradient_bar(img, (x0, y0, x0 + bx, y0 + 120))
        d.text((x0 + bx // 2, y0 + 60), f"#{rank}", font=font(64, "display"), fill=BG + (255,), anchor="mm")
    d.text((x0 + bx + 28, y0 + 10), name, font=f1, fill=INK + (255,))
    d.text((x0 + bx + 30, y0 + 74), link, font=f2, fill=C1 + (255,))
    return save(img, path)


def corner_badge(path: Path, rank: int, w: int = 1920, h: int = 1080, pos: str = "tl") -> Path:
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, h], fill=BG + (95,))
    x, y = _place(pos, 132, 120, w, h)
    gradient_bar(img, (x, y, x + 132, y + 120))
    d.text((x + 66, y + 60), f"#{rank}", font=font(64, "display"), fill=BG + (255,), anchor="mm")
    return save(img, path)


def end_card(path: Path, credits: list[tuple[str, str]], cta: str, w: int = 1920, h: int = 1080) -> Path:
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, h], fill=BG + (175,))
    gradient_text(img, (140, 110), "CLIPS FROM", font(96, "display"))
    room = (h - 240) - 280
    step = min(82, room // max(1, len(credits)))
    f1, f2 = font(max(26, int(step * 0.6))), font(max(20, int(step * 0.4)))
    y = 280
    for name, link in credits:
        d.text((150, y), name, font=f1, fill=INK + (255,))
        d.text((150 + d.textlength(name, font=f1) + 30, y + int(step * 0.17)), link, font=f2, fill=C1 + (255,))
        y += step
    d.text((w // 2, h - 140), cta.upper(), font=fit_font(d, cta.upper(), w - 300, 64, "display"),
           fill=INK + (255,), anchor="mm")
    gradient_bar(img, (w // 2 - 260, h - 90, w // 2 + 260, h - 82))
    return save(img, path)


def thumbnail(frame: Path, path: Path, top: str, big: str) -> Path:
    """1280x720 YouTube thumbnail from a frame of the #1 moment."""
    img = Image.open(frame).convert("RGBA").resize((1280, 720))
    shade = canvas(1280, 720)
    sd = ImageDraw.Draw(shade)
    for x in range(1280):
        sd.line([(x, 0), (x, 720)], fill=BG + (int(235 * max(0, 1 - x / 820)),))
    img.alpha_composite(shade)
    d = ImageDraw.Draw(img)
    d.text((56, 70), top.upper(), font=fit_font(d, top.upper(), 700, 64, "display"), fill=INK + (255,))
    f = font(150, "display")
    y = 170
    for ln in wrap(d, big.upper(), f, 760)[:3]:
        gradient_text(img, (50, y), ln, f)
        y += 150
    out = img.convert("RGB")
    path.parent.mkdir(parents=True, exist_ok=True)
    out.save(path, quality=92)
    return path


# ------------------------------------------------------------------ 9:16 graphics (1080x1920)

def short_badge(path: Path, rank: int, series: str, credit: str, y: int = 1590, w: int = 1080,
                h: int = 1920) -> Path:
    """Rank badge in the band under the gameplay of a split-screen clip."""
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([60, y, w - 60, y + 230], radius=26, fill=BG + (215,))
    gradient_bar(img, (60, y, 230, y + 230))
    d.text((145, y + 115), f"#{rank}", font=font(110, "display"), fill=BG + (255,), anchor="mm")
    f = fit_font(d, series.upper(), w - 360, 46)
    d.text((262, y + 52), series.upper(), font=f, fill=INK + (255,))
    d.text((262, y + 132), credit, font=fit_font(d, credit, w - 360, 36), fill=C1 + (255,))
    return save(img, path)


def short_end(path: Path, line1: str, line2: str, w: int = 1080, h: int = 1920) -> Path:
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, h], fill=BG + (150,))
    f = font(96, "display")
    y = 700
    for ln in wrap(d, line1.upper(), f, w - 140):
        lw = d.textlength(ln, font=f)
        gradient_text(img, (int((w - lw) / 2), y), ln, f)
        y += 110
    d.text((w // 2, y + 60), line2, font=fit_font(d, line2, w - 140, 48), fill=INK + (255,), anchor="mm")
    return save(img, path)


def news_card(path: Path, kicker: str, text: str, source: str, game_box: tuple[int, int, int, int],
              w: int = 1080, h: int = 1920, footage: str = "") -> Path:
    """Fact text above the gameplay window, source line below it."""
    img = canvas(w, h)
    d = ImageDraw.Draw(img)
    gx0, gy0, gx1, gy1 = game_box
    d.rounded_rectangle([40, 150, w - 40, gy0 - 40], radius=30, fill=BG + (215,))
    d.text((90, 200), kicker.upper(), font=font(42), fill=C1 + (255,))
    f = font(112, "display")
    lines = wrap(d, text.upper(), f, w - 180)
    while len(lines) > 4 and f.size > 56:
        f = font(f.size - 8, "display")
        lines = wrap(d, text.upper(), f, w - 180)
    lines = lines[:5]
    lh = int(f.size * 1.12)
    top, bottom = 270, gy0 - 70
    y = top + max(0, (bottom - top - lh * len(lines)) // 2)
    for i, ln in enumerate(lines):
        if i == 0:
            gradient_text(img, (90, y), ln, f, glow=False)
        else:
            d.text((90, y), ln, font=f, fill=INK + (255,))
        y += lh
    gradient_bar(img, (gx0 - 6, gy0 - 6, gx1 + 6, gy0))
    gradient_bar(img, (gx0 - 6, gy1, gx1 + 6, gy1 + 6))
    if source:
        s = "SOURCE: " + source
        d.rounded_rectangle([40, gy1 + 40, w - 40, gy1 + 130], radius=20, fill=BG + (200,))
        d.text((w // 2, gy1 + 85), s, font=fit_font(d, s, w - 140, 36), fill=MUTED + (255,), anchor="mm")
    if footage:
        s = "FOOTAGE: " + footage
        d.rounded_rectangle([40, gy1 + 150, w - 40, gy1 + 240], radius=20, fill=BG + (200,))
        d.text((w // 2, gy1 + 195), s, font=fit_font(d, s, w - 140, 36), fill=C1 + (255,), anchor="mm")
    return save(img, path)
