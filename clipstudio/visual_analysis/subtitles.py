"""Find when the game's own subtitles are on screen, to tell game dialogue from the streamer's voice.

A stream has one mixed audio track, so speech recognition hears the streamer and the game's characters
alike. Games that subtitle their dialogue give it away on screen: a line of small white text, centred, in
the lower part of the picture, exactly while a character speaks. This module only detects that such a line
is present (no OCR, nothing to install); speech that happens while it is up is treated as game dialogue.

Limits, by design: a game with subtitles switched off gives no signal (everything is kept), and a streamer
talking over a subtitled line is taken for the game.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from clipstudio.core.errors import MediaError
from clipstudio.core.logging import get_logger
from clipstudio.core.models import MediaInfo
from clipstudio.media.ffmpeg import ffmpeg_bin, run

log = get_logger("subtitles")
BAND_W = 960


def _box_mean(a: np.ndarray, k: int) -> np.ndarray:
    p = k // 2
    ap = np.pad(a, p, mode="edge")
    c = np.pad(ap.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    h, w = a.shape
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)


def stroke_map(rgb: np.ndarray) -> np.ndarray:
    """Pixels that look like subtitle lettering: white or light grey (no colour), brighter than their
    surroundings, and thin (a white shirt or the sky is bright but not thin)."""
    f = rgb.astype(np.float32)
    lo, hi = f.min(2), f.max(2)
    g = f.mean(2)
    return (lo > 170) & (hi - lo < 30) & (g - _box_mean(g, 15) > 22)


def text_line_present(rgb: np.ndarray) -> bool:
    """Is there a centred line of small white text in this band (h, w, 3)?"""
    return text_line(rgb) is not None


def text_line(rgb: np.ndarray) -> np.ndarray | None:
    """Signature of the centred line of small white text in this band (a 64-bin profile of where its letters
    are), or None when there is no such line. Two samples of the same subtitle give the same signature; a new
    line of dialogue gives a different one."""
    stroke = stroke_map(rgb)
    h, w = stroke.shape
    mid = stroke[:, int(w * 0.3): int(w * 0.7)]             # every subtitle crosses the middle of the screen
    rows = mid.mean(1)
    lo_h, hi_h = max(5, h // 12), int(h * 0.42)
    text_rows = np.nonzero(rows > 0.03)[0]
    if len(text_rows) < lo_h:
        return None
    best = None
    for run in np.split(text_rows, np.nonzero(np.diff(text_rows) > 2)[0] + 1):
        if lo_h <= run[-1] - run[0] + 1 <= hi_h and (best is None or rows[run].sum() > rows[best].sum()):
            best = run
    if best is None:
        return None
    line = stroke[best[0]: best[-1] + 1]
    cols = line.any(0)
    # grow outwards from the centre over the letters; a gap wider than a few characters ends the line
    gap = int(0.035 * w)
    c = w // 2
    if not cols[c - gap: c + gap].any():
        return None
    left = right = c
    miss = 0
    for x in range(c, -1, -1):
        miss = 0 if cols[x] else miss + 1
        if miss > gap:
            break
        if cols[x]:
            left = x
    miss = 0
    for x in range(c, w):
        miss = 0 if cols[x] else miss + 1
        if miss > gap:
            break
        if cols[x]:
            right = x
    span = right - left + 1
    if span < 0.07 * w or abs((left + right) / 2 - w / 2) > 0.045 * w:
        return None                                        # subtitles are centred; HUD text and signs are not
    box = line[:, left: right + 1]
    if not (0.07 <= float(box.mean()) <= 0.5 and float(box.any(0).mean()) >= 0.5):
        return None
    prof = line.sum(0).astype(np.float32)
    prof[:left], prof[right + 1:] = 0, 0
    return prof[: (w // 64) * 64].reshape(64, -1).sum(1)


def same_line(a: np.ndarray, b: np.ndarray) -> bool:
    """Do two signatures show the same subtitle text?"""
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    return na > 0 and nb > 0 and float(a @ b) / (na * nb) > 0.8


def detect_subtitles(path: Path, media: MediaInfo, fps: float = 4.0, band: tuple[float, float] = (0.80, 0.955),
                     width: tuple[float, float] = (0.2, 0.8)) -> tuple[list[bool], list[int]]:
    """(flags, onsets). ``flags``: one per 1/fps seconds of the source, a subtitle line is on screen.
    ``onsets``: sample numbers where a new line of text appears (from nothing, or replacing another line),
    which is when a game character starts speaking."""
    if media.width <= 0 or media.height <= 0:
        return []
    cw, ch = int(media.width * (width[1] - width[0])) // 2 * 2, int(media.height * (band[1] - band[0])) // 2 * 2
    cx, cy = int(media.width * width[0]), int(media.height * band[0])
    bh = max(8, int(round(BAND_W * ch / cw / 2.0)) * 2)
    cp = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-i", str(path), "-an", "-sn",
              "-vf", f"fps={fps:g},crop={cw}:{ch}:{cx}:{cy},scale={BAND_W}:{bh}:flags=area",
              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    buf = np.frombuffer(cp.stdout, np.uint8)
    n = buf.size // (BAND_W * bh * 3)
    if n == 0:
        raise MediaError(f"no frames decoded from {path.name} for subtitle detection")
    frames = buf[: n * BAND_W * bh * 3].reshape(n, bh, BAND_W, 3)
    sigs = [text_line(f) for f in frames]
    raw = np.array([sg is not None for sg in sigs])
    # a subtitle stays up for a second or more: drop single-sample blips, fill single-sample gaps
    out = raw.copy()
    for i in range(1, n - 1):
        if raw[i - 1] == raw[i + 1] != raw[i]:
            out[i] = raw[i - 1]
    onsets, last = [], None
    for i in range(n):
        if not out[i]:
            last = None
            continue
        if sigs[i] is None:
            continue                      # a filled gap: the same line is still up
        if last is None or not same_line(last, sigs[i]):
            onsets.append(i)
        last = sigs[i]
    return [bool(x) for x in out], onsets


def share_present(flags: list[bool] | np.ndarray | None, fps: float, start: float, end: float) -> float:
    """Fraction of [start, end] during which a subtitle was on screen (0 when unknown)."""
    if flags is None or len(flags) == 0:
        return 0.0
    a, b = max(0, int(start * fps)), min(len(flags), max(int(start * fps) + 1, int(np.ceil(end * fps))))
    return float(np.mean(flags[a:b])) if b > a else 0.0


GAME_SHARE = 0.6   # a line spoken with a subtitle on screen for more than this share of it may be the game's
ONSET_BEFORE, ONSET_AFTER = 0.9, 0.6   # s: a character's line starts when its subtitle appears


def is_game_line(flags, onsets, fps: float, start: float, end: float) -> bool:
    """Was the line spoken in [start, end] said by a game character?

    Yes only when a subtitle is up for most of the line AND that subtitle appeared (or changed) right as the
    line began. A streamer talking while some older subtitle or an on-screen text sits there does not match,
    so their words are kept. When in doubt the answer is no: a missing caption is worse than an extra one."""
    if flags is None or onsets is None or len(flags) == 0:
        return False
    if share_present(flags, fps, start, end) <= GAME_SHARE:
        return False
    return any(start - ONSET_BEFORE <= i / fps <= start + ONSET_AFTER for i in onsets)


def drop_game_dialogue(words: list, flags: list[bool] | np.ndarray | None, fps: float,
                       onsets: list[int] | None = None) -> list:
    """Words (anything with .text/.start/.end) minus the lines spoken by game characters.

    Words are grouped into lines (a pause of 0.7 s or sentence punctuation ends one) and a whole line is
    judged at once, so a subtitle changing mid-sentence does not cut a sentence in half."""
    if flags is None or onsets is None or len(flags) == 0 or not any(flags):
        return list(words)
    out, line = [], []

    def flush() -> None:
        if line and not is_game_line(flags, onsets, fps, line[0].start, line[-1].end):
            out.extend(line)
        line.clear()

    for w in words:
        if line and w.start - line[-1].end > 0.7:
            flush()
        line.append(w)
        if w.text.rstrip()[-1:] in ".!?":
            flush()
    flush()
    return out
