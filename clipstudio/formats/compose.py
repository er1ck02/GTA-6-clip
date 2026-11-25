"""FFmpeg building blocks for the formats: every segment is encoded with identical settings, then the
segments are joined with the concat demuxer (stream copy).

Source clips are usually Clip Studio's approved 9:16 split-screen renders (streamer facecam on top, the full
unzoomed gameplay under it). :class:`Layout` says where those two regions are, so a widescreen video can put
the gameplay back to full frame with the facecam as picture-in-picture, instead of letterboxing a phone video.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from clipstudio.core.logging import get_logger
from clipstudio.media.ffmpeg import ffmpeg_bin, probe, run

log = get_logger("formats")

LOUD = "loudnorm=I=-16:TP=-1.5:LRA=11"
FPS = 30


@dataclass
class Layout:
    """Regions in source pixels: ``game`` (x, y, w, h) and optional ``face``."""
    game: tuple[int, int, int, int]
    face: tuple[int, int, int, int] | None = None
    # boxes (x, y, w, h, source pixels) pixelated before anything else: the stream's own chat, donation
    # alerts... Chat can show anything (slurs, spam), so every wide shot of a stream with visible chat masks it.
    mask: list[tuple[int, int, int, int]] = field(default_factory=list)

    def with_mask(self, mask) -> "Layout":
        self.mask = [tuple(int(v) for v in b) for b in (mask or [])]
        return self

    @classmethod
    def split(cls) -> "Layout":
        # Clip Studio's split-screen template (1080x1920): facecam y 0-960, gameplay 16:9 at y 960-1568
        return cls(game=(0, 960, 1080, 608), face=(0, 0, 1080, 960))

    @classmethod
    def for_source(cls, path: Path, spec: dict | str | None = None) -> "Layout":
        if isinstance(spec, dict):
            return cls(game=tuple(spec["game"]), face=tuple(spec["face"]) if spec.get("face") else None)
        info = probe(path)
        w, h = info.width or 1920, info.height or 1080
        if spec == "wide" or (spec in (None, "auto") and w >= h):
            return cls(game=(0, 0, w, h))
        if spec in (None, "auto", "split"):
            if (w, h) == (1080, 1920):
                return cls.split()
            k = w / 1080      # same template at another resolution
            return cls(game=(0, int(960 * k), w, int(608 * k)), face=(0, 0, w, int(960 * k)))
        raise ValueError(f"unknown layout {spec!r}")


@dataclass
class Enc:
    encoder: str = "libx264"        # or h264_nvenc on the RTX PC
    crf: int = 19
    preset: str = "veryfast"
    audio_bitrate: str = "192k"
    extra: list[str] = field(default_factory=list)

    def args(self) -> list[str]:
        if self.encoder == "h264_nvenc":
            v = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(self.crf + 2), "-b:v", "0"]
        else:
            v = ["-c:v", self.encoder, "-preset", self.preset, "-crf", str(self.crf)]
        return v + ["-pix_fmt", "yuv420p", "-r", str(FPS), "-video_track_timescale", "15360",
                    "-c:a", "aac", "-b:a", self.audio_bitrate, "-ar", "48000", "-ac", "2",
                    "-movflags", "+faststart"] + self.extra


def _crop(r: tuple[int, int, int, int]) -> str:
    x, y, w, h = r
    return f"crop={w}:{h}:{x}:{y}"


def _fill(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"


def _masked(src: str, layout: Layout | None, tag: str) -> tuple[list[str], str]:
    """Filters that pixelate ``layout.mask`` boxes on stream ``src``; returns (filters, output label)."""
    if not layout or not layout.mask:
        return [], src
    fs, cur = [], src
    for i, (x, y, w, h) in enumerate(layout.mask):
        w, h = max(16, w // 2 * 2), max(16, h // 2 * 2)
        fs.append(f"[{cur}]split=2[{tag}b{i}][{tag}p{i}]")
        fs.append(f"[{tag}p{i}]crop={w}:{h}:{x}:{y},scale={max(2, w // 28)}:{max(2, h // 28)},"
                  f"scale={w}:{h}:flags=neighbor,boxblur=6:2,eq=brightness=-0.15[{tag}q{i}]")
        fs.append(f"[{tag}b{i}][{tag}q{i}]overlay={x}:{y}[{tag}m{i}]")
        cur = f"{tag}m{i}"
    return fs, cur


def _ff(args: list[str], out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run([ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-y"] + args + [str(out)])
    return out


def frame_png(clip: Path, t: float, out: Path, layout: Layout | None = None, size: tuple[int, int] | None = None) -> Path:
    fs, cur = _masked("0:v", layout, "k")
    tail = []
    if layout:
        tail.append(_crop(layout.game))
    if size:
        tail.append(_fill(*size))
    fs.append(f"[{cur}]" + (",".join(tail) if tail else "null") + "[v]")
    return _ff(["-ss", f"{max(0.0, t):.3f}", "-i", str(clip), "-filter_complex", ";".join(fs), "-map", "[v]",
                "-frames:v", "1"], out)


def card(out: Path, *, bg_clip: Path, bg_t: float, layout: Layout, overlays: list[Path], narration: Path | None,
         dur: float, size: tuple[int, int], enc: Enc, still: bool = False, blur: int = 18, dim: float = -0.18,
         game_box: tuple[int, int, int, int] | None = None, lead: float = 0.25) -> Path:
    """A narration segment over a blurred, darkened piece of gameplay (moving, or a still frame).

    ``game_box`` additionally shows the gameplay unblurred and unzoomed in that box (vertical news layout).
    """
    W, H = size
    inp = (["-loop", "1", "-framerate", str(FPS)] if still else ["-stream_loop", "-1"])
    src = ["-ss", f"{max(0.0, bg_t):.3f}"] + inp + ["-i", str(bg_clip)]
    args = list(src)
    n = 1
    fc, cur = _masked("0:v", layout, "a")
    fc.append(f"[{cur}]{_crop(layout.game)},{_fill(W, H)},boxblur={blur}:2,eq=brightness={dim}:saturation=1.1[bg]")
    last = "bg"
    if game_box:
        # a second decoder of the same clip (split + an unused branch makes ffmpeg buffer without end)
        args += src
        x, y, w, h = game_box
        mf, mcur = _masked(f"{n}:v", layout, "b")
        fc += mf
        fc.append(f"[{mcur}]{_crop(layout.game)},scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1[gw]")
        fc.append(f"[{last}][gw]overlay={x}:{y}[v0]")
        last = "v0"
        n += 1
    for png in overlays:
        args += ["-i", str(png)]
        fc.append(f"[{last}][{n}:v]overlay=0:0[o{n}]")
        last = f"o{n}"
        n += 1
    fc.append(f"[{last}]fps={FPS},format=yuv420p[v]")
    if narration:
        args += ["-i", str(narration)]
        ms = int(lead * 1000)
        fc.append(f"[{n}:a]aformat=channel_layouts=stereo,{LOUD},adelay={ms}|{ms},apad[a]")
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        fc.append(f"[{n}:a]anull[a]")
    args += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}"] + enc.args()
    return _ff(args, out)


def wide_clip(out: Path, *, clip: Path, t_in: float, t_out: float, layout: Layout, overlays: list[Path],
              enc: Enc, size: tuple[int, int] = (1920, 1080), pip_w: int = 500) -> Path:
    """The clip as widescreen: gameplay full frame, facecam picture-in-picture bottom-right, credit overlay."""
    W, H = size
    dur = t_out - t_in
    args = ["-ss", f"{t_in:.3f}", "-t", f"{dur:.3f}", "-i", str(clip)]
    fc, cur = _masked("0:v", layout, "w")
    if layout.face:
        fx, fy, fw, fh = layout.face
        ph = int(pip_w * fh / fw) // 2 * 2
        fc += [f"[{cur}]split=2[s0][s1]", f"[s0]{_crop(layout.game)},{_fill(W, H)}[g]",
               f"[s1]{_crop(layout.face)},scale={pip_w}:{ph},setsar=1,"
               f"pad={pip_w + 8}:{ph + 8}:4:4:color=0x34e7ff[f]",
               f"[g][f]overlay={W - pip_w - 8 - 40}:{H - ph - 8 - 40}[v0]"]
    else:
        fc += [f"[{cur}]{_crop(layout.game)},{_fill(W, H)}[v0]"]
    last = "v0"
    for i, png in enumerate(overlays, start=1):
        args += ["-i", str(png)]
        fc.append(f"[{last}][{i}:v]overlay=0:0[o{i}]")
        last = f"o{i}"
    fc.append(f"[{last}]fps={FPS},format=yuv420p[v]")
    fc.append(f"[0:a]aformat=channel_layouts=stereo,{LOUD},apad[a]")
    args += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}"] + enc.args()
    return _ff(args, out)


def vertical_clip(out: Path, *, clip: Path, t_in: float, t_out: float, overlays: list[Path], enc: Enc,
                  size: tuple[int, int] = (1080, 1920)) -> Path:
    W, H = size
    dur = t_out - t_in
    args = ["-ss", f"{t_in:.3f}", "-t", f"{dur:.3f}", "-i", str(clip)]
    fc = [f"[0:v]{_fill(W, H)}[v0]"]
    last = "v0"
    for i, png in enumerate(overlays, start=1):
        args += ["-i", str(png)]
        fc.append(f"[{last}][{i}:v]overlay=0:0[o{i}]")
        last = f"o{i}"
    fc.append(f"[{last}]fps={FPS},format=yuv420p[v]")
    fc.append(f"[0:a]aformat=channel_layouts=stereo,{LOUD},apad[a]")
    args += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}"] + enc.args()
    return _ff(args, out)


def still_card(out: Path, *, image: Path, overlays: list[Path], narration: Path | None, dur: float, enc: Enc,
               size: tuple[int, int], lead: float = 0.15) -> Path:
    """Narration over a still frame (e.g. the clip's last frame) with overlays."""
    W, H = size
    args = ["-loop", "1", "-framerate", str(FPS), "-i", str(image)]
    fc = [f"[0:v]{_fill(W, H)}[v0]"]
    last = "v0"
    for i, png in enumerate(overlays, start=1):
        args += ["-i", str(png)]
        fc.append(f"[{last}][{i}:v]overlay=0:0[o{i}]")
        last = f"o{i}"
    fc.append(f"[{last}]fps={FPS},format=yuv420p[v]")
    n = len(overlays) + 1
    if narration:
        ms = int(lead * 1000)
        args += ["-i", str(narration)]
        fc.append(f"[{n}:a]aformat=channel_layouts=stereo,{LOUD},adelay={ms}|{ms},apad[a]")
    else:
        args += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        fc.append(f"[{n}:a]anull[a]")
    args += ["-filter_complex", ";".join(fc), "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}"] + enc.args()
    return _ff(args, out)


def concat(segments: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".concat.txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in segments), encoding="utf-8")
    _ff(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart"], out)
    lst.unlink(missing_ok=True)
    return out


def seg_duration(p: Path) -> float:
    return probe(p).duration
