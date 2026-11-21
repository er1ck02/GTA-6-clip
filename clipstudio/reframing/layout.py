"""9:16 layout planning. Produces a CropPlan (pure data) that the renderer executes.

Phase 2 supports static plans for three layouts. Facecam *detection* and
gameplay subject *tracking* (Phase 4) will emit time-varying keyframes into
the same CropPlan structure, so rendering and QC do not change.
"""
from __future__ import annotations

from clipstudio.config import CaptionsCfg, ReframingCfg, VideoCfg
from clipstudio.core.logging import get_logger
from clipstudio.core.models import CropKeyframe, CropPlan, MediaInfo, Panel, PixelRect

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


def plan_layout(media: MediaInfo, rcfg: ReframingCfg, vcfg: VideoCfg, ccfg: CaptionsCfg) -> CropPlan:
    W, H = vcfg.width, vcfg.height
    sw, sh = media.width, media.height
    safe_top = int(H * ccfg.safe_margin_top)
    safe_bottom = H - int(H * ccfg.safe_margin_bottom)
    notes: list[str] = []
    mode = rcfg.mode

    if mode == "facecam_split" and rcfg.facecam_region is None:
        notes.append("facecam_split requested but no facecam_region configured and automatic facecam "
                     "detection is not implemented yet (Phase 4); fell back to gameplay crop")
        log.warning(notes[-1])
        mode = "gameplay"

    if mode == "gameplay":
        g = _crop_for_aspect(sw, sh, W / H, rcfg.gameplay_center_x * sw, sh / 2)
        return CropPlan(
            mode=mode, source_width=sw, source_height=sh, out_width=W, out_height=H,
            panels=[Panel(name="gameplay", dest=PixelRect(x=0, y=0, w=W, h=H))],
            keyframes=[CropKeyframe(t=0.0, gameplay=g)],
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

    # facecam_split
    fr = rcfg.facecam_region
    assert fr is not None
    ph = _even(H * rcfg.facecam_panel_ratio)
    face_src = _crop_for_aspect(
        sw, sh, W / ph, (fr.x + fr.w / 2) * sw, (fr.y + fr.h / 2) * sh,
        min_w=int(fr.w * sw), min_h=int(fr.h * sh),
    )
    g = _crop_for_aspect(sw, sh, W / (H - ph), rcfg.gameplay_center_x * sw, sh / 2)
    face_dest = PixelRect(x=0, y=0, w=W, h=ph)
    return CropPlan(
        mode=mode, source_width=sw, source_height=sh, out_width=W, out_height=H,
        panels=[Panel(name="facecam", dest=face_dest), Panel(name="gameplay", dest=PixelRect(x=0, y=ph, w=W, h=H - ph))],
        keyframes=[CropKeyframe(t=0.0, gameplay=g, facecam=face_src)],
        protected_regions=[face_dest],
        caption_band=(max(safe_top, ph + int(0.02 * H)), safe_bottom),
        source="manual", notes=notes,
    )
