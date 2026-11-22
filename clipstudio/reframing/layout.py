"""9:16 layout planning. Produces a CropPlan (pure data) that the renderer executes.

Inputs: source size, the reframing config, an optional automatic facecam
detection and an optional action track for the clip. Output: panels (where
each piece goes in the 9:16 frame) and crop keyframes over clip-relative time.
The renderer interpolates linearly between keyframes.
"""
from __future__ import annotations

from clipstudio.config import CaptionsCfg, ReframingCfg, VideoCfg
from clipstudio.core.logging import get_logger
from clipstudio.core.models import ActionTrack, CropKeyframe, CropPlan, FacecamDetection, MediaInfo, NormRect, Panel, PixelRect

log = get_logger("reframing")


def _even(x: float) -> int:
    return max(2, int(round(x / 2.0)) * 2)


def _crop_for_aspect(src_w: int, src_h: int, aspect: float, cx: float, cy: float,
                     min_w: int = 0, min_h: int = 0) -> PixelRect:
    """Largest-needed crop with width/height == aspect, centred on (cx, cy), inside the frame.

    The crop starts at the full source height (or width) and shrinks only if
    it must, so we keep as much resolution as possible.
    """
    h = src_h
    w = h * aspect
    if w > src_w:
        w = src_w
        h = w / aspect
    # if a minimum region was requested (e.g. the facecam), make the crop just big enough to hold it
    if min_w or min_h:
        need_w = max(min_w, min_h * aspect)
        need_h = need_w / aspect
        if need_w <= src_w and need_h <= src_h:
            w, h = need_w, need_h
    w, h = _even(w), _even(h)
    w, h = min(w, src_w - src_w % 2), min(h, src_h - src_h % 2)
    x = int(round(cx - w / 2))
    y = int(round(cy - h / 2))
    x = min(max(0, x), src_w - w)
    y = min(max(0, y), src_h - h)
    return PixelRect(x=x, y=y, w=w, h=h)


def _cover_crop(region: PixelRect, aspect: float, focus_x: float, focus_y: float) -> PixelRect:
    """Largest crop of ``aspect`` that fits inside ``region``, centred on the focus point where possible."""
    w, h = region.w, region.w / aspect
    if h > region.h:
        h, w = region.h, region.h * aspect
    w, h = _even(w), _even(h)
    w, h = min(w, region.w - region.w % 2), min(h, region.h - region.h % 2)
    x = int(round(min(max(region.x, focus_x - w / 2), region.x + region.w - w)))
    y = int(round(min(max(region.y, focus_y - h / 2), region.y + region.h - h)))
    return PixelRect(x=x, y=y, w=w, h=h)


def resolve_facecam(rcfg: ReframingCfg, facecam: FacecamDetection | None,
                    min_conf: float) -> tuple[NormRect | None, NormRect | None, str]:
    """(region, face, source) from the manual setting or a confident detection."""
    if rcfg.facecam_region is not None:
        r = rcfg.facecam_region
        return NormRect(x=r.x, y=r.y, w=r.w, h=r.h), None, "manual"
    if facecam is not None and facecam.region is not None and facecam.confidence >= min_conf:
        return facecam.region, facecam.face, "auto"
    return None, None, "none"


def _avoid(g: PixelRect, fr: PixelRect | None, sw: int, max_shift: float) -> PixelRect:
    """Shift a gameplay crop sideways off the facecam overlay if that is a small move."""
    if fr is None or max_shift <= 0:
        return g
    # detected regions are the inside of the overlay's border; keep clear of the border too
    pad = max(8, int(0.01 * sw))
    fr = PixelRect(x=fr.x - pad, y=fr.y - pad, w=fr.w + 2 * pad, h=fr.h + 2 * pad)
    if g.x >= fr.x + fr.w or fr.x >= g.x + g.w or g.y >= fr.y + fr.h or fr.y >= g.y + g.h:
        return g
    right = fr.x + fr.w - g.x            # shift right to clear an overlay on the left
    left = g.x + g.w - fr.x              # shift left to clear an overlay on the right
    for dx in sorted((right, -left), key=abs):
        nx = g.x + dx
        if abs(dx) <= max_shift * sw and 0 <= nx <= sw - g.w:
            return PixelRect(x=nx, y=g.y, w=g.w, h=g.h)
    return g


def plan_layout(media: MediaInfo, rcfg: ReframingCfg, vcfg: VideoCfg, ccfg: CaptionsCfg,
                facecam: FacecamDetection | None = None, track: ActionTrack | None = None,
                min_facecam_conf: float = 0.6, framing: dict | None = None) -> CropPlan:
    """``framing``: ``{"cx", "cy", "zoom"}`` for the locked gameplay crop (see ``vision.overlays``); ignored
    when the crop follows the action."""
    W, H = vcfg.width, vcfg.height
    sw, sh = media.width, media.height
    safe_top = int(H * ccfg.safe_margin_top)
    safe_bottom = H - int(H * ccfg.safe_margin_bottom)
    notes: list[str] = []
    mode = rcfg.mode
    region, face, fsource = resolve_facecam(rcfg, facecam, min_facecam_conf)

    if mode == "auto":
        mode = "facecam_split" if region is not None else "gameplay"
        notes.append(f"auto layout: {mode}" + (f" ({fsource} facecam)" if region is not None else
                                               " (no confident facecam)"))
    if mode in ("facecam_split", "facecam_pip") and region is None:
        why = ("detected facecam confidence %.2f below %.2f" % (facecam.confidence, min_facecam_conf)
               if facecam is not None and facecam.region is not None else "no facecam configured or detected")
        notes.append(f"{mode} requested but {why}; fell back to gameplay crop")
        log.warning(notes[-1])
        mode = "gameplay"

    fr_px = cam_px = None
    if region is not None:
        fr_px = PixelRect(x=int(region.x * sw), y=int(region.y * sh), w=int(region.w * sw), h=int(region.h * sh))
        # the camera picture itself: a small inset drops any leftover border pixels
        ix, iy = max(2, int(0.02 * fr_px.w)), max(2, int(0.02 * fr_px.h))
        cam_px = PixelRect(x=fr_px.x + ix, y=fr_px.y + iy, w=fr_px.w - 2 * ix, h=fr_px.h - 2 * iy)

    path = [(0.0, rcfg.gameplay_center_x)]
    if track is not None and track.smoothed and rcfg.tracking and mode != "fit_blur":
        path = list(track.smoothed)
        notes.append(f"gameplay crop follows the action: {len(path)} keyframes ({track.method})")

    tracked = len(path) > 1
    if framing and not tracked and mode != "fit_blur":
        notes.append(f"gameplay crop placed on the action and off stream overlays: zoom {framing['zoom']:.2f}, "
                     f"centre ({framing['cx']:.2f}, {framing['cy']:.2f}); overlay share "
                     f"{framing.get('cover_before', 0):.0%} -> {framing.get('cover_after', 0):.0%}")

    def gameplay_keys(aspect: float, avoid: bool) -> list[PixelRect]:
        if framing and not tracked:
            base = _crop_for_aspect(sw, sh, aspect, sw / 2, sh / 2)
            w, h = _even(base.w / framing["zoom"]), _even(base.h / framing["zoom"])
            x = min(max(0, int(round(framing["cx"] * sw - w / 2))), sw - w)
            y = min(max(0, int(round(framing["cy"] * sh - h / 2))), sh - h)
            rects = [PixelRect(x=x, y=y, w=w, h=h)]
            return [_avoid(r, fr_px, sw, rcfg.avoid_facecam_shift) for r in rects] if avoid else rects
        rects = [_crop_for_aspect(sw, sh, aspect, cx * sw, sh / 2) for _, cx in path]
        if avoid:
            rects = [_avoid(r, fr_px, sw, rcfg.avoid_facecam_shift) for r in rects]
        return rects

    def dedupe(keys: list[CropKeyframe]) -> list[CropKeyframe]:
        """Drop keyframes in the middle of a run with the same crop (the renderer holds anyway)."""
        out = [k for i, k in enumerate(keys)
               if i in (0, len(keys) - 1) or not (keys[i - 1].gameplay == k.gameplay == keys[i + 1].gameplay)]
        return out

    def view_height() -> int:
        """Height of a full-width gameplay panel that shows ``gameplay_view`` of the source's width."""
        return min(_even(H * 2 / 3), _even(W * sh / max(1.0, min(1.0, rcfg.gameplay_view) * sw)))

    if mode == "gameplay" and rcfg.gameplay_fill == "wide":
        gh = view_height()
        gy = _even((H - gh) * 0.45)   # a band above for the title, below the apps' top bar
        rects = gameplay_keys(W / gh, avoid=False)
        notes.append("no facecam: gameplay shown wide (less zoom) over a blurred fill, text in the band above it")
        return CropPlan(
            mode=mode, source_width=sw, source_height=sh, out_width=W, out_height=H,
            panels=[Panel(name="background", dest=PixelRect(x=0, y=0, w=W, h=H)),
                    Panel(name="gameplay", dest=PixelRect(x=0, y=gy, w=W, h=gh))],
            keyframes=dedupe([CropKeyframe(t=t, gameplay=g) for (t, _), g in zip(path, rects)]),
            caption_band=(safe_top, safe_bottom), notes=notes,
        )

    if mode == "gameplay":
        rects = gameplay_keys(W / H, avoid=False)
        return CropPlan(
            mode=mode, source_width=sw, source_height=sh, out_width=W, out_height=H,
            panels=[Panel(name="gameplay", dest=PixelRect(x=0, y=0, w=W, h=H))],
            keyframes=dedupe([CropKeyframe(t=t, gameplay=g) for (t, _), g in zip(path, rects)]),
            caption_band=(safe_top, safe_bottom), notes=notes,
        )

    if mode == "fit_blur":
        fit_h = _even(W * sh / sw)
        y0 = _even((H - fit_h) / 2)
        fit = PixelRect(x=0, y=y0, w=W, h=fit_h)
        band_top = y0 + fit_h + int(0.01 * H)
        band = (band_top, safe_bottom) if safe_bottom - band_top > 0.12 * H else (safe_top, safe_bottom)
        return CropPlan(
            mode=mode, source_width=sw, source_height=sh, out_width=W, out_height=H,
            panels=[Panel(name="background", dest=PixelRect(x=0, y=0, w=W, h=H)), Panel(name="gameplay", dest=fit)],
            keyframes=[CropKeyframe(t=0.0, gameplay=PixelRect(x=0, y=0, w=sw - sw % 2, h=sh - sh % 2))],
            caption_band=band, notes=notes,
        )

    assert fr_px is not None and cam_px is not None and region is not None
    fx = (face.x + face.w / 2) * sw if face else fr_px.x + fr_px.w / 2
    fy = (face.y + face.h / 2) * sh if face else fr_px.y + fr_px.h / 2
    src_source = "manual" if fsource == "manual" else "auto"

    if mode == "facecam_split":
        ph = _even(H * rcfg.facecam_panel_ratio)
        face_src = _cover_crop(cam_px, W / ph, fx, fy)
        fit = W / max(1, face_src.w) > rcfg.facecam_max_upscale
        if fit:  # a small webcam: show all of it rather than a blurry close-up of the face
            face_src = PixelRect(x=cam_px.x, y=cam_px.y, w=_even(cam_px.w) if cam_px.w % 2 == 0 else cam_px.w - 1,
                                 h=_even(cam_px.h) if cam_px.h % 2 == 0 else cam_px.h - 1)
            # never enlarged past facecam_max_upscale: a smaller, cleaner picture beats a bigger, blockier one
            ih = min(ph, _even(face_src.h * rcfg.facecam_max_upscale))
            iw = min(W, _even(ih * face_src.w / face_src.h))
            inner = PixelRect(x=(W - iw) // 2, y=(ph - ih) // 2, w=iw, h=ih)
            notes.append(f"small webcam ({cam_px.w}x{cam_px.h} px): shown whole over a blurred fill, "
                         f"enlarged {ih / max(1, face_src.h):.1f}x instead of {W / max(1, _cover_crop(cam_px, W / ph, fx, fy).w):.1f}x")
        # streamer on top, game below. The game panel shows gameplay_view of the screen's width; what is left
        # of the 9:16 frame above and below the pair is a blurred fill (the band above carries the title)
        gh = min(H - ph, view_height())
        top = _even((H - ph - gh) * rcfg.stack_top) if rcfg.stack_top > 0 else 0
        rects = gameplay_keys(W / gh, avoid=gh >= H - ph)   # a wide view cannot step around the webcam anyway
        face_dest = PixelRect(x=0, y=top, w=W, h=ph)
        panels = [Panel(name="facecam", dest=face_dest), Panel(name="gameplay", dest=PixelRect(x=0, y=top + ph, w=W, h=gh))]
        if ph + gh < H:
            panels.insert(0, Panel(name="background", dest=PixelRect(x=0, y=0, w=W, h=H)))
        return CropPlan(
            mode=mode, source_width=sw, source_height=sh, out_width=W, out_height=H,
            panels=panels,
            keyframes=dedupe([CropKeyframe(t=t, gameplay=g, facecam=face_src) for (t, _), g in zip(path, rects)]),
            protected_regions=[face_dest],
            caption_band=(max(safe_top, top + ph + int(0.02 * H)), safe_bottom),
            facecam_fit=fit, facecam_inner=inner if fit else None, source=src_source, notes=notes,
        )

    # facecam_pip: full-screen gameplay with the facecam boxed in a top corner
    pw = _even(W * rcfg.pip_width_ratio)
    cam_aspect = max(0.5, min(2.4, fr_px.w / max(1, fr_px.h)))
    pph = _even(pw / cam_aspect)
    margin = _even(0.035 * W)
    px = margin if rcfg.pip_corner == "top_left" else W - pw - margin
    py = _even(max(0.04 * H, safe_top * 0.5))
    pip = PixelRect(x=px, y=py, w=pw, h=pph)
    face_src = _cover_crop(cam_px, pw / pph, fx, fy)
    rects = gameplay_keys(W / H, avoid=True)
    return CropPlan(
        mode=mode, source_width=sw, source_height=sh, out_width=W, out_height=H,
        panels=[Panel(name="gameplay", dest=PixelRect(x=0, y=0, w=W, h=H)), Panel(name="facecam", dest=pip)],
        keyframes=dedupe([CropKeyframe(t=t, gameplay=g, facecam=face_src) for (t, _), g in zip(path, rects)]),
        protected_regions=[pip],
        caption_band=(max(safe_top, py + pph + int(0.02 * H)), safe_bottom),
        source=src_source, notes=notes,
    )
