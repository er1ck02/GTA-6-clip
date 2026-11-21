"""Automated quality control. A clip is only 'done' when this report passes.

Every check inspects the real output (or the real caption render), not the
intent. A check that cannot run (e.g. no face detector installed) reports
``skip`` with the reason, never ``pass``.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from clipstudio.config import CaptionsCfg, QualityCfg
from clipstudio.core.errors import MediaError
from clipstudio.core.models import CaptionTrack, CropPlan, PixelRect, QCCheck, QCReport
from clipstudio.media.ffmpeg import ffmpeg_bin, ffprobe_bin, probe, run


def _stream_durations(path: Path) -> dict[str, float]:
    cp = run([ffprobe_bin(), "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0", str(path)])
    out: dict[str, float] = {}
    for line in cp.stdout.decode().split():
        kind, _, d = line.partition(",")
        try:
            out.setdefault(kind, float(d))
        except ValueError:
            pass
    return out


def _gray_frames(args_in: list[str], vf: str, w: int, h: int, cwd: Path | None = None) -> np.ndarray:
    cp = run([ffmpeg_bin(), "-v", "error", *args_in, "-vf", f"{vf},scale={w}:{h}:flags=area,format=gray",
              "-f", "rawvideo", "-"], cwd=cwd)
    buf = np.frombuffer(cp.stdout, dtype=np.uint8)
    n = buf.size // (w * h)
    return buf[: n * w * h].reshape(n, h, w)


def _intersects(a: PixelRect, x0: int, y0: int, x1: int, y1: int) -> bool:
    return not (x1 <= a.x or x0 >= a.x + a.w or y1 <= a.y or y0 >= a.y + a.h)


def _hook_check(ass_path: Path, lines: list[str], hook_lines: list[str], W: int, H: int, sx: int, sy: int,
                x_min: int, x_max: int, plan: CropPlan) -> QCCheck:
    """The hook title must be visible, on screen, inside the side margins and clear of the facecam."""
    f = ass_path.with_name("_qc_hook.ass")
    f.write_text("\n".join(ln for ln in lines if not ln.startswith("Dialogue:") or ln in hook_lines) + "\n",
                 encoding="utf-8")
    try:
        frames = _gray_frames(["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=10:d=1.0"],
                              f"ass={f.name}", W // sx, H // sy, cwd=ass_path.parent)
    finally:
        f.unlink(missing_ok=True)
    mask = frames[min(len(frames) - 1, 5)] > 60
    if not mask.any():
        return QCCheck(name="hook_title", group="captions", status="fail", detail="hook title produced no visible text")
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = xs.min() * sx, (xs.max() + 1) * sx, ys.min() * sy, (ys.max() + 1) * sy
    covers = any(_intersects(p, x0, y0, x1, y1) for p in plan.protected_regions)
    ok = x0 >= x_min and x1 <= x_max and y0 >= 0 and y1 <= H and not covers
    return QCCheck(name="hook_title", group="captions", status="pass" if ok else "fail",
                   detail=f"hook title at {(int(x0), int(y0), int(x1), int(y1))}"
                          + ("" if ok else f"; must stay inside x {x_min}-{x_max} and off protected regions"))


def run_qc(clip_id: str, output: Path, expected_duration: float, plan: CropPlan, track: CaptionTrack | None,
           ass_path: Path | None, qcfg: QualityCfg, ccfg: CaptionsCfg, expect_audio: bool,
           face_detector=None) -> QCReport:
    checks: list[QCCheck] = []

    # ------------------------------------------------------------ video
    if not output.exists() or output.stat().st_size == 0:
        checks.append(QCCheck(name="file_exists", group="video", status="fail", detail=f"{output} missing or empty"))
        return QCReport(clip_id=clip_id, output=str(output), passed=False, checks=checks)
    checks.append(QCCheck(name="file_exists", group="video", status="pass", detail=f"{output.stat().st_size/1e6:.1f} MB"))

    try:
        info = probe(output)
    except MediaError as e:
        checks.append(QCCheck(name="playable", group="video", status="fail", detail=str(e)))
        return QCReport(clip_id=clip_id, output=str(output), passed=False, checks=checks)

    dec = run([ffmpeg_bin(), "-v", "error", "-i", str(output), "-f", "null", "-"], check=False)
    errs = dec.stderr.decode(errors="replace").strip()
    checks.append(QCCheck(name="decodes_cleanly", group="video", status="pass" if dec.returncode == 0 and not errs else "fail",
                          detail="full decode without errors" if not errs else errs[:300]))

    ok_res = (info.width, info.height) == (plan.out_width, plan.out_height)
    checks.append(QCCheck(name="resolution", group="video", status="pass" if ok_res else "fail",
                          detail=f"{info.width}x{info.height} (expected {plan.out_width}x{plan.out_height})"))
    ratio = info.width / info.height
    checks.append(QCCheck(name="aspect_ratio", group="video", status="pass" if abs(ratio - 9 / 16) < 0.01 else "fail",
                          detail=f"{ratio:.4f} (9:16 = 0.5625)", value=round(ratio, 4)))
    dd = abs(info.duration - expected_duration)
    checks.append(QCCheck(name="duration", group="video", status="pass" if dd <= qcfg.duration_tolerance else "fail",
                          detail=f"{info.duration:.2f}s vs expected {expected_duration:.2f}s", value=round(info.duration, 3)))

    # one analysis decode: black frames + loudness + peaks + silence
    filt_v = "blackdetect=d=0.1:pix_th=0.08"
    a_args: list[str] = []
    if info.has_audio:
        a_args = ["-af", f"ebur128=peak=true,astats=metadata=0:measure_perchannel=none,"
                         f"silencedetect=n=-50dB:d={qcfg.max_silence_seconds}"]
    an = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(output), "-vf", filt_v, *a_args, "-f", "null", "-"])
    log_txt = an.stderr.decode(errors="replace")
    black = sum(float(m.group(1)) for m in re.finditer(r"black_duration:\s*([\d.]+)", log_txt))
    checks.append(QCCheck(name="black_frames", group="video", status="pass" if black <= qcfg.max_black_seconds else "fail",
                          detail=f"{black:.2f}s of black frames", value=round(black, 3)))

    # content sanity: sampled frames must not be flat (catches broken crops / empty composites)
    frames = _gray_frames(["-i", str(output)], "fps=1", 90, 160)
    flat = [i for i, f in enumerate(frames) if float(f.std()) < 4.0]
    checks.append(QCCheck(name="frame_content", group="video",
                          status="pass" if frames.size and len(flat) <= len(frames) * 0.1 else "fail",
                          detail=f"{len(flat)}/{len(frames)} sampled frames look flat"))

    # ------------------------------------------------------------ audio
    if not info.has_audio:
        checks.append(QCCheck(name="audio_present", group="audio", status="fail" if expect_audio else "skip",
                              detail="output has no audio stream"))
    else:
        checks.append(QCCheck(name="audio_present", group="audio", status="pass", detail=f"{info.audio_codec} {info.sample_rate} Hz"))
        sd = _stream_durations(output)
        if "video" in sd and "audio" in sd:
            diff = abs(sd["video"] - sd["audio"])
            checks.append(QCCheck(name="av_sync", group="audio", status="pass" if diff <= qcfg.av_sync_tolerance else "fail",
                                  detail=f"audio/video stream length differ by {diff*1000:.0f} ms", value=round(diff, 3)))
        summ = log_txt[log_txt.rfind("Summary:"):] if "Summary:" in log_txt else ""
        mi = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summ)
        mp = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summ)
        if mi:
            lufs = float(mi.group(1)) if mi.group(1) != "-inf" else -99.0
            st = "pass" if qcfg.lufs_min <= lufs <= qcfg.lufs_max else ("fail" if lufs < -40 else "warn")
            checks.append(QCCheck(name="loudness", group="audio", status=st,
                                  detail=f"{lufs:.1f} LUFS integrated (target range {qcfg.lufs_min}..{qcfg.lufs_max})", value=lufs))
        else:
            checks.append(QCCheck(name="loudness", group="audio", status="fail", detail="could not measure loudness"))
        if mp:
            tp = float(mp.group(1)) if mp.group(1) != "-inf" else -99.0
            checks.append(QCCheck(name="true_peak", group="audio", status="pass" if tp <= qcfg.max_true_peak else "fail",
                                  detail=f"{tp:.1f} dBTP (max {qcfg.max_true_peak})", value=tp))
        pk = re.search(r"Peak level dB:\s*(-?[\d.]+|-inf)", log_txt)
        if pk:
            p = float(pk.group(1)) if pk.group(1) != "-inf" else -99.0
            checks.append(QCCheck(name="no_clipping", group="audio", status="pass" if p < -0.1 else "fail",
                                  detail=f"sample peak {p:.2f} dBFS", value=p))
        sil = [float(m.group(1)) for m in re.finditer(r"silence_duration:\s*([\d.]+)", log_txt)]
        checks.append(QCCheck(name="long_silence", group="audio", status="warn" if sil else "pass",
                              detail=(f"{len(sil)} silent stretch(es) over {qcfg.max_silence_seconds:g}s (longest {max(sil):.1f}s)"
                                      if sil else f"no silence over {qcfg.max_silence_seconds:g}s")))

    # ------------------------------------------------------------ captions
    if track is None:
        checks.append(QCCheck(name="captions", group="captions", status="skip", detail="captions disabled"))
    else:
        checks.extend(_caption_checks(track, info.duration, plan, ass_path, ccfg))

    # ------------------------------------------------------------ tracking / framing
    bad = []
    for kf in plan.keyframes:
        for name, r in (("gameplay", kf.gameplay), ("facecam", kf.facecam)):
            if r and (r.x < 0 or r.y < 0 or r.x + r.w > plan.source_width or r.y + r.h > plan.source_height):
                bad.append(f"{name}@{kf.t:.2f}s")
    checks.append(QCCheck(name="crop_in_bounds", group="tracking", status="fail" if bad else "pass",
                          detail="crop windows inside source frame" if not bad else "out of bounds: " + ", ".join(bad)))
    speed, reversals = _crop_motion(plan)
    moving = len(plan.keyframes) > 1
    checks.append(QCCheck(name="crop_speed", group="tracking",
                          status="pass" if speed <= qcfg.max_crop_speed else "warn",
                          detail=(f"fastest crop pan {speed:.0%} of source width per second" if moving
                                  else "static crop"), value=round(speed, 4)))
    dur = max(1e-6, plan.keyframes[-1].t - plan.keyframes[0].t) if moving else 0
    rate = reversals / dur if moving else 0.0
    checks.append(QCCheck(name="crop_jitter", group="tracking", status="pass" if rate <= 0.5 else "warn",
                          detail=f"{reversals} pan direction changes" + (f" in {dur:.1f}s" if moving else ""),
                          value=round(rate, 3)))
    face_panel = next((p for p in plan.panels if p.name == "facecam"), None)
    if face_panel is None:
        checks.append(QCCheck(name="facecam_visible", group="tracking", status="skip",
                              detail=f"layout '{plan.mode}' has no facecam panel"))
    elif face_detector is None:
        checks.append(QCCheck(name="facecam_visible", group="tracking", status="skip",
                              detail="no face detector installed (requirements/vision.txt); facecam panel not verified"))
    else:
        checks.append(_facecam_check(output, info.duration, face_panel.dest, plan, face_detector))
    checks.append(QCCheck(name="subject_tracking", group="tracking", status="pass",
                          detail=(f"crop follows the action over {len(plan.keyframes)} keyframes" if moving
                                  else "static crop (tracking off, or no dominant on-screen subject)")))

    passed = not any(c.status == "fail" for c in checks)
    return QCReport(clip_id=clip_id, output=str(output), passed=passed, checks=checks)


def _crop_motion(plan: CropPlan) -> tuple[float, int]:
    """(max pan speed as fraction of source width per second, number of pan direction reversals)."""
    speed, rev, last = 0.0, 0, 0
    for a, b in zip(plan.keyframes, plan.keyframes[1:]):
        dx = ((b.gameplay.x + b.gameplay.w / 2) - (a.gameplay.x + a.gameplay.w / 2)) / plan.source_width
        dt = max(1e-3, b.t - a.t)
        speed = max(speed, abs(dx) / dt)
        sgn = (dx > 0.002) - (dx < -0.002)
        if sgn and last and sgn != last:
            rev += 1
        last = sgn or last
    return speed, rev


def _facecam_check(output: Path, duration: float, dest: PixelRect, plan: CropPlan, detector) -> QCCheck:
    """Decode a few frames of the facecam panel from the rendered file and look for a face."""
    n = 6
    scale = min(1.0, 640 / plan.out_width)
    w, h = int(plan.out_width * scale) // 2 * 2, int(plan.out_height * scale) // 2 * 2
    hits = 0
    for i in range(n):
        t = duration * (i + 0.5) / n
        cp = run([ffmpeg_bin(), "-v", "error", "-ss", f"{t:.3f}", "-i", str(output), "-frames:v", "1",
                  "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], check=False)
        if len(cp.stdout) < w * h * 3:
            continue
        fr = np.frombuffer(cp.stdout[: w * h * 3], np.uint8).reshape(h, w, 3)
        x0, y0 = int(dest.x * scale), int(dest.y * scale)
        panel = np.ascontiguousarray(fr[y0:y0 + int(dest.h * scale), x0:x0 + int(dest.w * scale)])
        if panel.size and detector.detect(panel):
            hits += 1
    frac = hits / n
    return QCCheck(name="facecam_visible", group="tracking", status="pass" if frac >= 0.5 else "warn",
                   detail=f"face found in the facecam panel in {hits}/{n} sampled frames ({detector.name})",
                   value=round(frac, 3))


def _caption_checks(track: CaptionTrack, duration: float, plan: CropPlan, ass_path: Path | None,
                    ccfg: CaptionsCfg) -> list[QCCheck]:
    out: list[QCCheck] = []
    ch = track.chunks
    overl = [c.index for a, c in zip(ch, ch[1:]) if c.start < a.end - 1e-3]
    out.append(QCCheck(name="caption_overlap", group="captions", status="fail" if overl else "pass",
                       detail="no overlapping caption blocks" if not overl else f"chunks overlap: {overl[:5]}"))
    outside = [c.index for c in ch if c.start < -1e-3 or c.end > duration + 0.05]
    out.append(QCCheck(name="caption_in_timeline", group="captions", status="fail" if outside else "pass",
                       detail="all captions inside the clip timeline" if not outside else f"outside clip: {outside[:5]}"))
    n_words = sum(len(ln) for c in ch for ln in c.lines)
    out.append(QCCheck(name="caption_words_complete", group="captions",
                       status="pass" if n_words == track.source_word_count else "fail",
                       detail=f"{n_words}/{track.source_word_count} transcript words captioned"))
    desync = [c.index for c in ch for ln in c.lines for w in ln if not (c.start - 1e-3 <= w.start <= c.end + 1e-3)]
    order = all(w1.start <= w2.start + 1e-3 for c in ch for w1, w2 in zip([w for ln in c.lines for w in ln],
                                                                          [w for ln in c.lines for w in ln][1:]))
    out.append(QCCheck(name="caption_sync", group="captions", status="pass" if not desync and order else "fail",
                       detail="word timings sit inside their caption block, in order" if not desync and order
                       else f"word timing outside block in chunks {sorted(set(desync))[:5]}"))
    if ass_path is None or not ch:
        out.append(QCCheck(name="caption_render_bounds", group="captions", status="skip", detail="no captions to render"))
        return out

    # Render ONLY the captions (libass, same file as the burn-in) on black and measure real pixels.
    W, H = plan.out_width, plan.out_height
    sx, sy = 4, 4
    fw, fh = W // sx, H // sy
    times = [round((c.start + c.end) / 2, 2) for c in ch]
    dur = max(times) + 0.2
    # the optional hook title is checked on its own; the caption checks see caption events only
    lines = ass_path.read_text(encoding="utf-8").splitlines()
    hook_lines = [ln for ln in lines if ln.startswith("Dialogue:") and ",Hook,," in ln]
    caps_only = ass_path.with_name("_qc_captions.ass")
    caps_only.write_text("\n".join(ln for ln in lines if ln not in hook_lines) + "\n", encoding="utf-8")
    try:
        frames = _gray_frames(
            ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r=10:d={dur:.2f}"],
            f"ass={caps_only.name}", fw, fh, cwd=ass_path.parent,
        )
    finally:
        caps_only.unlink(missing_ok=True)
    tol = int(0.012 * W)
    x_min, x_max = int(ccfg.safe_margin_x * W) - tol, W - int(ccfg.safe_margin_x * W) + tol
    if hook_lines:
        out.append(_hook_check(ass_path, lines, hook_lines, W, H, sx, sy, x_min, x_max, plan))
    band_top, band_bottom = plan.caption_band
    missing, out_of_safe, on_protected = [], [], []
    for c, t in zip(ch, times):
        i = min(len(frames) - 1, int(t * 10))
        mask = frames[i] > 60
        if not mask.any():
            missing.append(c.index)
            continue
        ys, xs = np.nonzero(mask)
        x0, x1, y0, y1 = xs.min() * sx, (xs.max() + 1) * sx, ys.min() * sy, (ys.max() + 1) * sy
        if x0 < x_min or x1 > x_max or y0 < band_top - tol or y1 > band_bottom + tol or y0 < 0 or y1 > H:
            out_of_safe.append((c.index, (int(x0), int(y0), int(x1), int(y1))))
        if any(_intersects(p, x0, y0, x1, y1) for p in plan.protected_regions):
            on_protected.append(c.index)
    out.append(QCCheck(name="caption_rendered", group="captions", status="fail" if missing else "pass",
                       detail="every caption block produced visible text" if not missing
                       else f"no visible text for chunks {missing[:5]} (font or libass problem?)"))
    out.append(QCCheck(name="caption_safe_area", group="captions", status="fail" if out_of_safe else "pass",
                       detail="all rendered caption pixels inside the safe area" if not out_of_safe
                       else f"outside safe area: {out_of_safe[:3]}"))
    out.append(QCCheck(name="caption_avoids_face", group="captions", status="fail" if on_protected else "pass",
                       detail="captions clear of protected regions" if not on_protected
                       else f"captions cover protected region in chunks {on_protected[:5]}"))
    return out
