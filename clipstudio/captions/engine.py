"""Caption engine: word-timed chunks, real font measurement, ASS (burn-in) and SRT export.

Rendering is done by libass inside FFmpeg, so the ASS file *is* the caption
render spec. Line breaking is decided here using the same font file libass
will use (resolved through fontconfig), so the QC stage can verify that text
fits the safe area instead of hoping it does.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from clipstudio.config import CaptionsCfg, CaptionStyle
from clipstudio.core.logging import get_logger
from clipstudio.core.models import CaptionChunk, CaptionLayout, CaptionTrack, CaptionWord, CropPlan, Word

log = get_logger("captions")
TERMINAL = re.compile(r"[.!?…]['\")\]]*$")


# ------------------------------------------------------------------ fonts
@lru_cache(maxsize=1)
def _font_index() -> dict[tuple[str, str], str]:
    """{(family_lower, style_lower): file} for fonts in the OS font folders (used when fontconfig is absent)."""
    from clipstudio.core.compat import font_dirs

    try:
        from PIL import ImageFont
    except ImportError:  # pragma: no cover - Pillow is a core dependency
        return {}
    index: dict[tuple[str, str], str] = {}
    for d in font_dirs():
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                continue
            try:
                fam, sty = ImageFont.truetype(str(f), 12).getname()
            except Exception:
                continue
            if fam:
                index.setdefault((fam.lower(), (sty or "regular").lower()), str(f))
    return index


def _scan_font(families: tuple[str, ...], bold: bool) -> tuple[str, str] | None:
    index = _font_index()
    want = ("bold", "semibold", "black", "regular") if bold else ("regular", "book", "normal", "medium")
    for fam in families:
        styles = {sty: f for (fl, sty), f in index.items() if fl == fam.lower()}
        if not styles:
            continue
        for sty in want:
            if sty in styles:
                return fam, styles[sty]
        return fam, sorted(styles.items())[0][1]
    return None


@lru_cache(maxsize=32)
def resolve_font(families: tuple[str, ...], bold: bool) -> tuple[str, str | None]:
    """Return (family, font_file) for the first installed family.

    Uses fontconfig when present (Linux), otherwise scans the OS font folders (Windows, macOS)."""
    if shutil.which("fc-list") and shutil.which("fc-match"):
        installed = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout.lower()
        names = {n.strip() for line in installed.splitlines() for n in line.split(",")}
        for fam in families:
            if fam.lower() in names:
                pat = f"{fam}:{'bold' if bold else 'regular'}"
                f = subprocess.run(["fc-match", "-f", "%{file}", pat], capture_output=True, text=True).stdout.strip()
                return fam, (f or None)
    found = _scan_font(families, bold)
    if found:
        return found
    log.warning("none of the caption fonts %s is installed; libass will substitute one", list(families))
    return families[-1], None


class _Measurer:
    def __init__(self, font_file: str | None, ass_size: int):
        self.ass_size = ass_size
        self.font = None
        if font_file:
            try:
                from PIL import ImageFont

                probe = ImageFont.truetype(font_file, ass_size)
                asc, desc = probe.getmetrics()
                # libass sizes a font so that ascent+descent == Fontsize; PIL sizes by em.
                em = max(1, round(ass_size * ass_size / max(1, asc + desc)))
                self.font = ImageFont.truetype(font_file, em)
            except Exception as e:  # unreadable font: fall back to an estimate and say so
                log.warning("could not load font %s for measurement (%s); using width estimate", font_file, e)

    @property
    def measured(self) -> bool:
        return self.font is not None

    def width(self, text: str) -> float:
        if self.font is not None:
            return float(self.font.getlength(text))
        return 0.62 * self.ass_size * len(text)


# ------------------------------------------------------------------ text helpers
def _clean(text: str, style: CaptionStyle, cfg: CaptionsCfg) -> str:
    t = text.strip()
    if cfg.profanity_filter:
        for bad in cfg.profanity_words:
            t = re.sub(rf"(?i)\b({re.escape(bad)})", lambda m: m.group(1)[0] + "*" * (len(m.group(1)) - 1), t)
    return t.upper() if style.uppercase else t


def _ass_escape(t: str) -> str:
    return t.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def _ass_color(hex_rgb: str) -> str:
    s = hex_rgb.lstrip("#")
    return f"&H00{s[4:6]}{s[2:4]}{s[0:2]}"


def _ts_ass(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000:d}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ts_srt(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def title_slot(plan: CropPlan, block_h: int = 0) -> int:
    """Top y of the title card, which is also where top-placed captions go.

    Right under the seam of a split layout; otherwise at the top of the caption band (in the band above a
    wide gameplay picture, below the apps' top bar). Moved below anything protected (a PiP facecam)."""
    face = next((p.dest for p in plan.panels if p.name == "facecam"), None)
    game = next((p.dest for p in plan.panels if p.name == "gameplay"), None)
    split = face is not None and game is not None and face.y == 0 and game.y >= face.h
    y = max(game.y + int(plan.out_height * 0.018), plan.caption_band[0]) if split else plan.caption_band[0]
    for pr in plan.protected_regions:
        if pr.y < y + block_h and pr.y + pr.h > y:
            y = pr.y + pr.h + int(plan.out_height * 0.02)
    return y


# ------------------------------------------------------------------ build
def build_caption_track(words: list[Word], clip_start: float, clip_end: float, cfg: CaptionsCfg,
                        plan: CropPlan) -> CaptionTrack:
    style = cfg.resolved_style()
    W, H = plan.out_width, plan.out_height
    fs = max(12, int(round(style.size * H)))
    family, font_file = resolve_font(tuple(style.font), style.bold)
    m = _Measurer(font_file, fs)
    if not m.measured:
        log.warning("caption font metrics unavailable; line widths are estimated")
    max_w = int(W * (1 - 2 * cfg.safe_margin_x))
    line_h = int(fs * 1.18)
    dur = clip_end - clip_start

    # clip-relative words, inside the clip only
    cw = []
    for w in words:
        if w.end <= clip_start or w.start >= clip_end:
            continue
        s, e = max(0.0, w.start - clip_start - cfg.lead_seconds), min(dur, w.end - clip_start - cfg.lead_seconds)
        e = max(e, s + 0.04)
        txt = _clean(w.text, style, cfg)
        if txt:
            cw.append(CaptionWord(text=txt, start=round(s, 3), end=round(max(e, s + 0.04), 3)))

    # group into chunks
    groups: list[list[CaptionWord]] = []
    cur: list[CaptionWord] = []
    for w in cw:
        if cur:
            gap = w.start - cur[-1].end
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            if (len(cur) >= style.max_words or gap > style.break_on_pause or TERMINAL.search(cur[-1].text)
                    or chars > style.max_chars_per_line * style.max_lines):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    groups = _merge_orphans(groups, style)

    # wrap into lines by real pixel width (with room for the active-word scale-up)
    chunks: list[CaptionChunk] = []
    scale = style.active_word_scale if style.highlight_active_word else 1.0
    pending = list(groups)
    while pending:
        g = pending.pop(0)
        lines: list[list[CaptionWord]] = [[]]
        for w in g:
            trial = " ".join(x.text for x in lines[-1] + [w])
            if lines[-1] and (m.width(trial) * scale > max_w or len(trial) > style.max_chars_per_line):
                lines.append([w])
            else:
                lines[-1].append(w)
        if len(lines) > style.max_lines:
            keep = [w for ln in lines[: style.max_lines] for w in ln]
            rest = g[len(keep):]
            pending.insert(0, rest)
            lines = lines[: style.max_lines]
        if len(lines) == 1 and m.width(lines[0][0].text) * scale > max_w:
            log.warning("word %r is wider than the safe area at this font size", lines[0][0].text)
        chunks.append(CaptionChunk(index=len(chunks), start=lines[0][0].start, end=lines[-1][-1].end, lines=lines))

    # timing: hold each chunk briefly, never overlap the next, stay inside the clip
    for i, c in enumerate(chunks):
        nxt = chunks[i + 1].start if i + 1 < len(chunks) else dur
        c.end = round(min(max(c.end + 0.25, c.start + 0.5), nxt, dur), 3)

    # vertical placement inside the allowed band
    band_top, band_bottom = plan.caption_band
    block_h = line_h * style.max_lines
    if cfg.placement == "top":
        baseline = min(title_slot(plan, block_h) + block_h, band_bottom)   # bottom of a full caption block
    else:
        baseline = int(cfg.vertical_position * H)
        baseline = min(max(baseline, band_top + block_h), band_bottom)
    layout = CaptionLayout(font_family=family, font_file=font_file, font_size_px=fs, center_x=W // 2,
                           baseline_y=baseline, line_height_px=line_h, max_text_width_px=max_w, measured=m.measured)
    return CaptionTrack(preset=cfg.preset, chunks=chunks, layout=layout, source_word_count=len(cw))


def _merge_orphans(groups: list[list[CaptionWord]], style: CaptionStyle) -> list[list[CaptionWord]]:
    """Fold a lone trailing word ("US?") back into the previous chunk when it was split only by max_words."""
    out: list[list[CaptionWord]] = []
    for g in groups:
        if out and len(g) == 1:
            prev = out[-1]
            gap = g[0].start - prev[-1].end
            chars = sum(len(w.text) + 1 for w in prev) + len(g[0].text)
            if (not TERMINAL.search(prev[-1].text) and gap <= style.break_on_pause
                    and chars <= style.max_chars_per_line * style.max_lines):
                prev.append(g[0])
                continue
        out.append(g)
    return out


# ------------------------------------------------------------------ writers
def write_ass(track: CaptionTrack, style: CaptionStyle, plan: CropPlan, path: Path,
              hook: str | None = None, hook_seconds: float = 0, clip_duration: float | None = None,
              placement: str = "manual") -> Path:
    """Write the ASS file. ``hook`` adds a title card (dark text on a white box) at the top of the gameplay
    picture for the first ``hook_seconds``, or for the whole clip when that is 0."""
    L = track.layout
    outline = max(0.0, style.outline * plan.out_height)
    shadow = max(0.0, style.shadow * plan.out_height)
    hook_fs = int(round(L.font_size_px * 0.62))
    hook_pad = max(4.0, hook_fs * 0.22)                 # box padding around the text
    hook_margin = int(plan.out_width * 0.09)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {plan.out_width}
PlayResY: {plan.out_height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{L.font_family},{hook_fs},&H00101010,&H00101010,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,{hook_pad:.1f},0,8,{hook_margin},{hook_margin},0,1
Style: Cap,{L.font_family},{L.font_size_px},{_ass_color(style.primary_color)},{_ass_color(style.highlight_color)},{_ass_color(style.outline_color)},&H80000000,{-1 if style.bold else 0},0,0,0,100,100,0,0,1,{outline:.1f},{shadow:.1f},2,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    hi = _ass_color(style.highlight_color)
    pct = int(round(style.active_word_scale * 100))
    events: list[str] = []
    # top placement: captions hang down from the title's slot (a one-line caption does not float a line lower),
    # and sit under the title card for as long as it is up
    top = placement == "top"
    slot_y = L.baseline_y - L.line_height_px * style.max_lines
    hook_end = (hook_seconds if hook_seconds > 0 else 1e9) if hook else 0.0
    hook_block = int(hook_fs * 1.25 + 2 * hook_pad + plan.out_height * 0.012)
    for c in track.chunks:
        if top:
            y = slot_y + (hook_block if c.start < hook_end - 0.2 else 0)
            pos = f"\\an8\\pos({L.center_x},{y})"
        else:
            pos = f"\\an2\\pos({L.center_x},{L.baseline_y})"
        flat = [w for ln in c.lines for w in ln]
        n = len(flat)
        spans = []
        if style.highlight_active_word:
            for k, w in enumerate(flat):
                s = c.start if k == 0 else w.start
                e = c.end if k == n - 1 else flat[k + 1].start
                if e > s:
                    spans.append((s, e, k))
        else:
            spans.append((c.start, c.end, -1))
        for idx, (s, e, active) in enumerate(spans):
            parts, k = [], 0
            for li, ln in enumerate(c.lines):
                toks = []
                for w in ln:
                    t = _ass_escape(w.text)
                    if k == active:
                        t = f"{{\\c{hi}&\\fscx{pct}\\fscy{pct}}}{t}{{\\r}}"
                    toks.append(t)
                    k += 1
                parts.append(" ".join(toks))
            pop = "\\fscx82\\fscy82\\t(0,90,\\fscx100\\fscy100)" if style.pop_in and idx == 0 else ""
            events.append(f"Dialogue: 0,{_ts_ass(s)},{_ts_ass(e)},Cap,,0,0,0,,{{{pos}{pop}}}" + "\\N".join(parts))
    if hook:
        # top of the gameplay picture, so the title never sits on the facecam panel of a split layout
        game = next((p.dest for p in plan.panels if p.name == "gameplay"), None)
        y = title_slot(plan, int(hook_fs * 2.9))  # room for up to two lines of title
        text = _ass_escape(hook.strip().upper())
        end = hook_seconds if hook_seconds > 0 else (clip_duration or max([c.end for c in track.chunks] + [3600.0]))
        fade = "\\fad(0,450)" if hook_seconds > 0 else ""
        events.insert(0, f"Dialogue: 1,{_ts_ass(0)},{_ts_ass(end)},Hook,,0,0,0,,"
                         f"{{\\an8\\pos({plan.out_width // 2},{y})\\q0\\fscx80\\fscy80\\t(0,140,\\fscx100\\fscy100)"
                         f"{fade}}}{text}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def write_srt(track: CaptionTrack, path: Path) -> Path:
    blocks = [f"{i}\n{_ts_srt(c.start)} --> {_ts_srt(c.end)}\n{c.text}\n" for i, c in enumerate(track.chunks, 1)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path
