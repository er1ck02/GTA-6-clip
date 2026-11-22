"""Per-platform copies of a rendered clip.

The master render is already 9:16 H.264/AAC with loudness normalised, so a platform copy is a stream copy
whenever the master fits that platform's limits, and a constrained re-encode otherwise (frame rate cap,
bitrate cap, or a hard trim to the maximum length, which is logged as a warning). Each copy gets a JSON
sidecar with the title, description and hashtags to paste when posting. Nothing is uploaded.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from clipstudio.core.compat import replace_file
from clipstudio.config import ExportCfg, PlatformPreset
from clipstudio.core.logging import get_logger
from clipstudio.media.ffmpeg import ffmpeg_bin, ffprobe_bin, probe, run

log = get_logger("platforms")


@dataclass
class PlatformOutput:
    platform: str
    path: str
    method: str          # "copy" or "reencode"
    duration: float
    trimmed: bool
    reasons: list[str]
    sidecar: str


def parse_rate(v: str) -> float:
    """'16M' / '800k' / '2500000' -> bits per second."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)\s*", v)
    if not m:
        raise ValueError(f"bad bitrate {v!r}")
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


def video_bitrate(path: Path) -> float:
    cp = run([ffprobe_bin(), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=bit_rate:format=bit_rate",
              "-of", "json", str(path)])
    d = json.loads(cp.stdout or b"{}")
    for v in [s.get("bit_rate") for s in d.get("streams", [])] + [d.get("format", {}).get("bit_rate")]:
        if v and str(v).isdigit():
            return float(v)
    return 0.0


def plan_variant(duration: float, fps: float, bitrate: float, preset: PlatformPreset) -> list[str]:
    """Why a re-encode is needed (empty list = stream copy is fine)."""
    reasons = []
    if duration > preset.max_duration + 0.05:
        reasons.append(f"longer than {preset.max_duration:g}s: trimmed")
    if fps > preset.max_fps + 0.01:
        reasons.append(f"{fps:g} fps above {preset.max_fps}")
    if bitrate > parse_rate(preset.max_video_bitrate) * 1.02:
        reasons.append(f"{bitrate / 1e6:.1f} Mb/s above {preset.max_video_bitrate}")
    return reasons


def export_platforms(master: Path, out_dir: Path, ecfg: ExportCfg, title: str, description: str,
                     platforms: list[str] | None = None) -> list[PlatformOutput]:
    names = ecfg.platforms if platforms is None else platforms
    if not names:
        return []
    info = probe(master)
    rate = video_bitrate(master)
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for name in names:
        preset = ecfg.platform_presets.get(name)
        if preset is None:
            raise ValueError(f"unknown platform {name!r}; choose from {sorted(ecfg.platform_presets)}")
        reasons = plan_variant(info.duration, info.fps, rate, preset)
        dst = out_dir / f"{master.stem}.{name}.mp4"
        tmp = dst.with_name(dst.stem + ".partial.mp4")
        dur = min(info.duration, preset.max_duration)
        cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-y", "-i", str(master), "-t", f"{dur:.3f}",
               "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "-1", "-metadata", f"title={title}"]
        if reasons:
            mr = preset.max_video_bitrate
            cmd += ["-c:v", "libx264", "-preset", ecfg.preset, "-crf", str(ecfg.crf), "-maxrate", mr,
                    "-bufsize", f"{parse_rate(mr) * 2 / 1e6:g}M", "-pix_fmt", "yuv420p", "-profile:v", "high"]
            if info.fps > preset.max_fps:
                cmd += ["-r", str(preset.max_fps)]
            cmd += ["-c:a", "aac", "-b:a", ecfg.audio_bitrate]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        run(cmd)
        replace_file(tmp, dst)
        trimmed = info.duration > preset.max_duration + 0.05
        if trimmed:
            log.warning("%s: clip is %.1fs, %s allows %.0fs; the copy was cut at the limit",
                        name, info.duration, preset.label, preset.max_duration)
        tags = " ".join("#" + h.lstrip("#") for h in preset.hashtags)
        side = dst.with_suffix(".json")
        side.write_text(json.dumps({"platform": preset.label, "title": title, "description": description,
                                    "hashtags": preset.hashtags, "post_text": f"{title} {tags}".strip(),
                                    "file": dst.name}, indent=2))
        results.append(PlatformOutput(name, str(dst), "reencode" if reasons else "copy", round(dur, 3), trimmed,
                                      reasons, str(side)))
        log.info("    %s copy (%s) -> %s", preset.label, "re-encoded: " + "; ".join(reasons) if reasons else "stream copy",
                 dst.name)
    (out_dir / "platforms.json").write_text(json.dumps([asdict(r) for r in results], indent=2))
    return results
