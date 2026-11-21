"""Single-pass FFmpeg render: trim, 9:16 composite, caption burn-in, loudness-normalised audio."""
from __future__ import annotations

import json
import re
from pathlib import Path

from clipstudio.config import AudioCfg, EnhanceCfg, ExportCfg, VideoCfg
from clipstudio.core.errors import MediaError
from clipstudio.core.logging import get_logger
from clipstudio.core.models import CropPlan, MediaInfo
from clipstudio.media.ffmpeg import ffmpeg_bin, ffmpeg_capabilities, run

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


PUNCH_IN, PUNCH_HOLD, PUNCH_OUT = 0.15, 0.5, 0.35


def punch_times(points: list[float], dur: float, limit: int, min_gap: float = 1.5) -> list[float]:
    """Clip-relative punch-in moments: inside the clip, at least ``min_gap`` apart, at most ``limit``."""
    out: list[float] = []
    for t in points:
        if PUNCH_IN <= t <= dur - PUNCH_HOLD - PUNCH_OUT and all(abs(t - o) >= min_gap for o in out):
            out.append(round(t, 3))
        if len(out) >= limit:
            break
    return sorted(out)


def zoom_expr(punches: list[float], zoom: float) -> str:
    """Scale factor in ``t``: ramps to ``zoom`` in 0.15 s, holds 0.5 s, eases back in 0.35 s (smoothstep)."""
    terms = []
    for p in punches:
        a, d = p - PUNCH_IN, p + PUNCH_HOLD + PUNCH_OUT
        e = f"clip((t-{a:.3f})/{PUNCH_IN},0,1)*clip(({d:.3f}-t)/{PUNCH_OUT},0,1)"
        terms.append(f"({e})*({e})*(3-2*({e}))")
    return f"(1+{zoom - 1:.4f}*({'+'.join(terms)}))"


def gameplay_fx(w: int, h: int, enhance: EnhanceCfg | None, punches: list[float] | None) -> str:
    """Filters appended to the scaled gameplay picture (``w``x``h``): punch zoom, then sharpen/colour."""
    fx = ""
    if enhance and punches and enhance.punch_zoom > 1.0:
        z = zoom_expr(punches, enhance.punch_zoom)
        # crop's own in_w/in_h are fixed at configure time, so the centring offset is spelled out
        fx += (f",scale=w='2*trunc({w}*{z}/2)':h='2*trunc({h}*{z}/2)':eval=frame:flags=bicubic,"
               f"crop={w}:{h}:'trunc(({w}*{z}-{w})/2)':'trunc(({h}*{z}-{h})/2)',setsar=1")
    if enhance:
        if enhance.contrast > 0 or enhance.vibrance > 0:
            fx += f",eq=contrast={1 + enhance.contrast:.3f}:saturation={1 + enhance.vibrance:.3f}"
        if enhance.sharpen > 0:
            fx += f",unsharp=5:5:{enhance.sharpen:.2f}:5:5:0"
    return fx


def keep_expr(cuts: list[tuple[float, float]]) -> str:
    """select/aselect expression that drops the clip-relative ``cuts``."""
    return "'not(" + "+".join(f"between(t,{a:.3f},{b - 1e-3:.3f})" for a, b in cuts) + ")'"


def build_video_graph(plan: CropPlan, fps: float, ass_name: str | None, enhance: EnhanceCfg | None = None,
                      punches: list[float] | None = None, cuts: list[tuple[float, float]] | None = None) -> str:
    kf = plan.keyframes[0]
    W, H = plan.out_width, plan.out_height
    crop = gameplay_crop(plan)
    if plan.mode == "gameplay":
        graph = f"[0:v]{crop},scale={W}:{H}:flags=lanczos,setsar=1{gameplay_fx(W, H, enhance, punches)}[base]"
    elif plan.mode == "fit_blur":
        fg = next(p.dest for p in plan.panels if p.name == "gameplay")
        sw, sh = W // 4 - (W // 4) % 2, H // 4 - (H // 4) % 2
        graph = (
            f"[0:v]split=2[bgsrc][fgsrc];"
            f"[bgsrc]scale={sw}:{sh}:force_original_aspect_ratio=increase,crop={sw}:{sh},"
            f"boxblur=10:2,eq=brightness=-0.10:saturation=1.1,scale={W}:{H}:flags=bilinear,setsar=1[bg];"
            f"[fgsrc]scale={fg.w}:{fg.h}:flags=lanczos,setsar=1{gameplay_fx(fg.w, fg.h, enhance, punches)}[fg];"
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
            f"[gsrc]{crop},scale={bot.w}:{bot.h}:flags=lanczos,setsar=1{gameplay_fx(bot.w, bot.h, enhance, punches)}[game];"
            f"[face][game]vstack=inputs=2[base]"
        )
    elif plan.mode == "facecam_pip":
        f = kf.facecam
        assert f is not None
        pip = next(p.dest for p in plan.panels if p.name == "facecam")
        b = max(2, (W // 180) // 2 * 2)  # border width
        graph = (
            f"[0:v]split=2[fsrc][gsrc];"
            f"[gsrc]{crop},scale={W}:{H}:flags=lanczos,setsar=1{gameplay_fx(W, H, enhance, punches)}[game];"
            f"[fsrc]crop={f.w}:{f.h}:{f.x}:{f.y},scale={pip.w}:{pip.h}:flags=lanczos,setsar=1,"
            f"pad={pip.w + 2 * b}:{pip.h + 2 * b}:{b}:{b}:color=0xF2E8D5[face];"
            f"[game][face]overlay={pip.x - b}:{pip.y - b}[base]"
        )
    else:
        raise ValueError(f"unsupported layout mode {plan.mode!r}")
    tail = f"[base]fps={fps:g}"
    if cuts:
        # crop keyframes and punches live in uncut clip time, so the cut happens after them;
        # captions are already in cut time and burn in after it
        tail += f",select={keep_expr(cuts)},setpts=N/({fps:g}*TB)"
    if ass_name:
        tail += f",ass={ass_name}"
    return graph + ";" + tail + ",format=yuv420p[vout]"


VOICE_CHAINS = {
    "off": "",
    "light": "highpass=f=80,acompressor=threshold=-18dB:ratio=2:attack=20:release=250",
    "strong": ("highpass=f=90,afftdn=nf=-25,equalizer=f=3000:t=q:w=1.2:g=3,deesser=i=0.4,"
               "acompressor=threshold=-20dB:ratio=3:attack=10:release=200"),
}


def build_audio_graph(n_tracks: int, voice: int | None, acfg: AudioCfg,
                      cuts: list[tuple[float, float]] | None = None) -> str:
    """Filtergraph from the source's audio streams to ``[amix]`` (with ``cuts`` removed).

    With a separate voice stream: the voice gets the enhancement chain and (optionally) ducks the game
    audio through sidechain compression. Otherwise every stream is summed and the chain applies to the mix.
    """
    g = _audio_mix(n_tracks, voice, acfg)
    if cuts:
        g = g[: -len("[amix]")] + f"[amix0];[amix0]aselect={keep_expr(cuts)},asetpts=N/SR/TB[amix]"
    return g


def _audio_mix(n_tracks: int, voice: int | None, acfg: AudioCfg) -> str:
    chain = VOICE_CHAINS[acfg.voice_enhancement]
    if n_tracks <= 1:
        return f"[0:a:0]{chain or 'anull'}[amix]"
    if voice is None or not (0 <= voice < n_tracks):
        ins = "".join(f"[0:a:{i}]" for i in range(n_tracks))
        return f"{ins}amix=inputs={n_tracks}:normalize=0:duration=longest" + (f",{chain}" if chain else "") + "[amix]"
    others = [i for i in range(n_tracks) if i != voice]
    g = "".join(f"[0:a:{i}]" for i in others)
    game = f"{g}amix=inputs={len(others)}:normalize=0:duration=longest[game]" if len(others) > 1 else f"{g}anull[game]"
    if acfg.duck_game:
        return (f"[0:a:{voice}]{chain + ',' if chain else ''}asplit=2[voice][key];{game};"
                f"[game][key]sidechaincompress=threshold=0.03:ratio={acfg.duck_ratio:g}:attack=15:release=350:makeup=1[gd];"
                f"[voice][gd]amix=inputs=2:normalize=0:duration=longest[amix]")
    return f"[0:a:{voice}]{chain or 'anull'}[voice];{game};[voice][game]amix=inputs=2:normalize=0:duration=longest[amix]"


def _audio_filters(acfg: AudioCfg, src: Path, start: float, dur: float, graph: str) -> str:
    """Loudness chain applied after ``[amix]``: two-pass loudnorm measured on this exact segment."""
    if not acfg.normalization:
        return "aresample=48000"
    target = f"I={acfg.target_lufs}:TP={acfg.true_peak}:LRA={acfg.loudness_range}"
    cp = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
              "-vn", "-filter_complex", f"{graph};[amix]loudnorm={target}:print_format=json", "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", cp.stderr.decode(errors="replace"), re.S)
    if not m:
        log.warning("loudness measurement failed; using single-pass loudnorm")
        return f"loudnorm={target},aresample=48000"
    meas = json.loads(m.group(0))
    if meas.get("input_i") in ("-inf", None) or float(meas["input_i"]) < -70:
        log.warning("clip audio is effectively silent (%s LUFS); skipping normalisation", meas.get("input_i"))
        return "aresample=48000"
    return (f"loudnorm={target}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:"
            f"measured_LRA={meas['input_lra']}:measured_thresh={meas['input_thresh']}:"
            f"offset={meas['target_offset']}:linear=true,aresample=48000")


def choose_encoder(ecfg: ExportCfg, device: str) -> str:
    if ecfg.encoder != "auto":
        return ecfg.encoder
    if device == "cuda" and "h264_nvenc" in ffmpeg_capabilities()["encoders"]:
        return "h264_nvenc"
    return "libx264"


def render_clip(media: MediaInfo, plan: CropPlan, start: float, end: float, ass_path: Path | None,
                out_path: Path, vcfg: VideoCfg, acfg: AudioCfg, ecfg: ExportCfg, device: str,
                voice_track: int | None = None, enhance: EnhanceCfg | None = None,
                punches: list[float] | None = None, cuts: list[tuple[float, float]] | None = None) -> Path:
    src = Path(media.path)
    dur = end - start
    out_dur = dur - sum(b - a for a, b in (cuts or []))
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
    graph = build_video_graph(plan, fps, ass_name, enhance, punches, cuts)
    n_tracks = max(1, len(media.audio_streams)) if acfg.tracks == "all" else 1
    agraph = build_audio_graph(n_tracks, voice_track, acfg, cuts)
    afilt = _audio_filters(acfg, src, start, dur, agraph) if media.has_audio else ""
    encoder = choose_encoder(ecfg, device)
    tmp = out_path.with_name(out_path.stem + ".partial.mp4")

    def cmd(enc: str) -> list[str]:
        c = [ffmpeg_bin(), "-hide_banner", "-nostats", "-v", "error", "-y",
             "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
             "-filter_complex", graph, "-map", "[vout]"]
        if media.has_audio:
            c += ["-filter_complex", f"{agraph};[amix]{afilt}[aout]", "-map", "[aout]"]
            c += ["-c:a", "aac", "-b:a", ecfg.audio_bitrate, "-ar", "48000", "-ac", "2"]
        if enc == "h264_nvenc":
            c += ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(ecfg.crf), "-b:v", "0"]
        else:
            c += ["-c:v", "libx264", "-preset", ecfg.preset, "-crf", str(ecfg.crf)]
        c += ["-pix_fmt", "yuv420p", "-profile:v", "high", "-movflags", "+faststart", "-t", f"{out_dur:.3f}", tmp.name]
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
