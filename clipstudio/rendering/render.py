"""Single-pass FFmpeg render: trim, 9:16 composite, caption burn-in, loudness-normalised audio."""
from __future__ import annotations

import json
import re
from pathlib import Path

from clipstudio.config import AudioCfg, ExportCfg, VideoCfg
from clipstudio.core.errors import MediaError
from clipstudio.core.logging import get_logger
from clipstudio.core.models import CropPlan, MediaInfo
from clipstudio.media.ffmpeg import audio_mix_filter, ffmpeg_bin, ffmpeg_capabilities, run

log = get_logger("render")


def lerp_expr(times: list[float], values: list[int]) -> str:
    """FFmpeg expression in ``t`` that interpolates linearly between keyframes and holds at the ends.

    Values are snapped to even pixels so chroma planes stay aligned.
    """
    if len(set(values)) == 1:
        return str(values[0])
    parts = str(values[-1])
    for i in range(len(times) - 2, -1, -1):
        t0, t1, v0, v1 = times[i], times[i + 1], values[i], values[i + 1]
        seg = f"{v0}+({v1 - v0})*(t-{t0:.3f})/{max(1e-3, t1 - t0):.3f}"
        parts = f"if(lt(t,{t1:.3f}),{seg},{parts})"
    parts = f"if(lt(t,{times[0]:.3f}),{values[0]},{parts})"
    return f"'2*trunc(({parts})/2)'"


def gameplay_crop(plan: CropPlan) -> str:
    """crop filter for the gameplay window, animated when the plan has several keyframes."""
    kfs = plan.keyframes
    g0 = kfs[0].gameplay
    if any(k.gameplay.w != g0.w or k.gameplay.h != g0.h for k in kfs):
        raise ValueError("animated crops must keep a constant size")
    times = [k.t for k in kfs]
    x = lerp_expr(times, [k.gameplay.x for k in kfs])
    y = lerp_expr(times, [k.gameplay.y for k in kfs])
    return f"crop={g0.w}:{g0.h}:{x}:{y}"


def build_video_graph(plan: CropPlan, fps: float, ass_name: str | None) -> str:
    kf = plan.keyframes[0]
    W, H = plan.out_width, plan.out_height
    crop = gameplay_crop(plan)
    if plan.mode == "gameplay":
        graph = f"[0:v]{crop},scale={W}:{H}:flags=lanczos,setsar=1[base]"
    elif plan.mode == "fit_blur":
        fg = next(p.dest for p in plan.panels if p.name == "gameplay")
        sw, sh = W // 4 - (W // 4) % 2, H // 4 - (H // 4) % 2
        graph = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
            f"boxblur=10:2,eq=brightness=-0.10:saturation=1.1,scale={W}:{H}:flags=bilinear,setsar=1[bg];"
            f"[fgsrc]scale={fg.w}:{fg.h}:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay=0:{fg.y}[base]"
        )
    elif plan.mode == "facecam_split":
        f = kf.facecam
        assert f is not None
        top = next(p.dest for p in plan.panels if p.name == "facecam")
        bot = next(p.dest for p in plan.panels if p.name == "gameplay")
        graph = (
            f"[0:v]split=2[fsrc][gsrc];"
            f"[fsrc]crop={f.w}:{f.h}:{f.x}:{f.y},scale={top.w}:{top.h}:flags=lanczos,setsar=1[face];"
            f"[gsrc]{crop},scale={bot.w}:{bot.h}:flags=lanczos,setsar=1[game];"
            f"[face][game]vstack=inputs=2[base]"
        )
    elif plan.mode == "facecam_pip":
        f = kf.facecam
        assert f is not None
        pip = next(p.dest for p in plan.panels if p.name == "facecam")
        b = max(2, (W // 180) // 2 * 2)  # border width
        graph = (
            f"[0:v]split=2[fsrc][gsrc];"
            f"[gsrc]{crop},scale={W}:{H}:flags=lanczos,setsar=1[game];"
            f"[fsrc]crop={f.w}:{f.h}:{f.x}:{f.y},scale={pip.w}:{pip.h}:flags=lanczos,setsar=1,"
            f"pad={pip.w + 2 * b}:{pip.h + 2 * b}:{b}:{b}:color=0xF2E8D5[face];"
            f"[game][face]overlay={pip.x - b}:{pip.y - b}[base]"
        )
    else:
        raise ValueError(f"unsupported layout mode {plan.mode!r}")
    tail = f"[base]fps={fps:g}"
    if ass_name:
        tail += f",ass={ass_name}"
    return graph + ";" + tail + ",format=yuv420p[vout]"


def _audio_filters(acfg: AudioCfg, src: Path, start: float, dur: float, mix: str | None = None) -> str:
    pre = "highpass=f=80,acompressor=threshold=-18dB:ratio=2:attack=20:release=250," if acfg.voice_enhancement == "light" else ""
    if not acfg.normalization:
        return pre + "aresample=48000"
    target = f"I={acfg.target_lufs}:TP={acfg.true_peak}:LRA={acfg.loudness_range}"
    # pass 1: measure this exact segment
    measure = f"{pre}loudnorm={target}:print_format=json"
    af = ["-filter_complex", f"{mix};[amix]{measure}"] if mix else ["-af", measure]
    cp = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
              "-vn", *af, "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", cp.stderr.decode(errors="replace"), re.S)
    if not m:
        log.warning("loudness measurement failed; using single-pass loudnorm")
        return f"{pre}loudnorm={target},aresample=48000"
    meas = json.loads(m.group(0))
    if meas.get("input_i") in ("-inf", None) or float(meas["input_i"]) < -70:
        log.warning("clip audio is effectively silent (%s LUFS); skipping normalisation", meas.get("input_i"))
        return pre + "aresample=48000"
    return (f"{pre}loudnorm={target}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
            f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
            f"offset={meas['target_offset']}:linear=true,aresample=48000")


def choose_encoder(ecfg: ExportCfg, device: str) -> str:
    if ecfg.encoder != "auto":
        return ecfg.encoder
    if device == "cuda" and "h264_nvenc" in ffmpeg_capabilities()["encoders"]:
        return "h264_nvenc"
    return "libx264"


def render_clip(media: MediaInfo, plan: CropPlan, start: float, end: float, ass_path: Path | None,
                out_path: Path, vcfg: VideoCfg, acfg: AudioCfg, ecfg: ExportCfg, device: str) -> Path:
    src = Path(media.path)
    dur = end - start
    fps = media.fps if vcfg.fps == "source" else float(vcfg.fps)
    if fps <= 0:
        fps = 30.0
    workdir = out_path.parent
    workdir.mkdir(parents=True, exist_ok=True)
    # The ass filter is given a bare file name and ffmpeg runs inside the clip folder,
    # which sidesteps filtergraph escaping of Windows drive letters and colons.
    ass_name = None
    if ass_path is not None:
        ass_name = ass_path.name
        if ass_path.parent.resolve() != workdir.resolve():
            raise ValueError("captions file must live in the clip output folder")
    graph = build_video_graph(plan, fps, ass_name)
    n_tracks = len(media.audio_streams) if acfg.tracks == "all" else 1
    mix = audio_mix_filter(n_tracks)
    afilt = _audio_filters(acfg, src, start, dur, mix) if media.has_audio else ""
    encoder = choose_encoder(ecfg, device)
    tmp = out_path.with_name(out_path.stem + ".partial.mp4")

    def cmd(enc: str) -> list[str]:
        c = [ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-y",
             "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
             "-filter_complex", graph, "-map", "[vout]"]
        if media.has_audio:
            c += ["-filter_complex", f"{mix};[amix]{afilt}[aout]", "-map", "[aout]"] if mix else \
                 ["-map", "0:a:0", "-af", afilt]
            c += ["-c:a", "aac", "-b:a", ecfg.audio_bitrate, "-ar", "48000", "-ac", "2"]
        if enc == "h264_nvenc":
            c += ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(ecfg.crf), "-b:v", "0"]
        else:
            c += ["-c:v", "libx264", "-preset", ecfg.preset, "-crf", str(ecfg.crf)]
        c += ["-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart", "-t", f"{dur:.3f}", tmp.name]
        return c

    log.info("rendering %s (%.1fs, %s, %s)", out_path.name, dur, plan.mode, encoder)
    try:
        _run_in(cmd(encoder), workdir)
    except MediaError as e:
        if encoder == "h264_nvenc":
            log.warning("NVENC encode failed (%s); retrying with libx264", str(e).splitlines()[-1])
            _run_in(cmd("libx264"), workdir)
        else:
            raise
    tmp.replace(out_path)
    return out_path


def _run_in(args: list[str], cwd: Path) -> None:
    run(args, cwd=cwd)
