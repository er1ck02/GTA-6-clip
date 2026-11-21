// Program / Source monitor: the real source video with the real crop plan drawn on top.
import { S, $, on, cand, media, timecode, fmtT, clamp, round, activeChunk, chunkText, layoutOf } from './core.js';
import { video, patchClip, setLocalCropCenter } from './actions.js';

const TEAL = '#2fe0d2', CORAL = '#ff5c7a';
let cv, ctx, viewer;
let drag = null;        // {kind:'crop'|'face-move'|'face-resize'|'face-draw', ...}
let drawMode = false;
let localFace = null;   // normalised face rect while editing

export function initProgram() {
  cv = $('#overlay'); ctx = cv.getContext('2d'); viewer = $('#viewer');
  const v = video();

  new ResizeObserver(() => { sizeCanvas(); S.dirty = true; }).observe(viewer);

  $('#btnPlay').onclick = togglePlay;
  $('#btnStepBack').onclick = () => step(-1);
  $('#btnStepFwd').onclick = () => step(1);
  $('#btnToStart').onclick = () => { const c = cand(); if (c) v.currentTime = c.start; };
  $('#btnLoop').onclick = e => { S.loop = !S.loop; e.currentTarget.classList.toggle('on', S.loop); };
  $('#btnMute').onclick = e => { v.muted = !v.muted; e.currentTarget.classList.toggle('on', !v.muted); };
  $('#btnMute').classList.toggle('on', !v.muted);
  $('#btnDrawFace').onclick = () => setDrawMode(!drawMode);

  v.addEventListener('play', syncPlayBtn);
  v.addEventListener('pause', syncPlayBtn);
  v.addEventListener('loadedmetadata', () => { sizeCanvas(); S.dirty = true; });
  v.addEventListener('error', () => {
    if (v.getAttribute('src')) $('#viewerMsg').textContent = 'Source video could not be loaded (missing or unsupported file)';
    $('#viewerMsg').hidden = false;
  });
  v.addEventListener('loadeddata', () => { $('#viewerMsg').hidden = true; });

  cv.addEventListener('pointerdown', onDown);
  cv.addEventListener('pointermove', onMove);
  cv.addEventListener('pointerup', onUp);
  cv.addEventListener('pointercancel', () => { drag = null; });

  document.addEventListener('keydown', e => {
    if (e.target.matches('input, textarea, select')) return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.key === ',' || e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
    else if (e.key === '.' || e.key === 'ArrowRight') { e.preventDefault(); step(1); }
  });

  on('project', () => {
    const m = media();
    $('#progMeta').textContent = m ? `${m.width}×${m.height} · ${round(m.fps, 2)} FPS · ORIGINAL SPEED` : '';
    $('#viewerMsg').hidden = !!(m && v.readyState >= 2);
    if (!m) $('#viewerMsg').textContent = S.project ? 'Source still being analysed…' : 'No project selected';
    else if (v.readyState < 2) $('#viewerMsg').textContent = 'Loading source…';
  });
  on('preview', () => { localFace = null; syncTools(); });
  on('select', () => { setDrawMode(false); });
}

function togglePlay() {
  const v = video();
  if (!v.getAttribute('src')) return;
  const c = cand();
  if (v.paused) {
    if (c && S.loop && (v.currentTime < c.start - 0.05 || v.currentTime >= c.end - 0.03)) v.currentTime = c.start;
    v.play().catch(err => console.warn('play()', err.message));
  } else v.pause();
}
function step(n) {
  const v = video();
  v.pause();
  const fps = media()?.fps || 30;
  v.currentTime = clamp(v.currentTime + n / fps, 0, v.duration || 1e9);
}
function syncPlayBtn() { $('#btnPlay').textContent = video().paused ? '▶' : '❚❚'; }

function setDrawMode(onOff) {
  drawMode = onOff;
  $('#btnDrawFace').classList.toggle('on', drawMode);
  cv.style.cursor = drawMode ? 'crosshair' : '';
  syncTools();
}

function syncTools() {
  const pv = S.preview;
  const split = layoutOf() === 'facecam_split';
  $('#btnDrawFace').hidden = !split;
  let hint = '';
  if (!pv) hint = '';
  else if (drawMode) hint = 'drag on the source to draw the facecam box';
  else if (pv.plan.mode === 'fit_blur') hint = 'fit_blur uses the whole frame · no crop to drag';
  else if (split && !pv.edit?.facecam_region) hint = 'no facecam box yet: click FACE BOX and drag';
  else if (split) hint = 'drag teal = gameplay · coral = face (corner resizes)';
  else hint = 'drag the teal crop sideways to reframe';
  $('#progHint').textContent = hint;
}

function sizeCanvas() {
  const r = viewer.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(r.width * dpr));
  cv.height = Math.max(1, Math.round(r.height * dpr));
}

// displayed video rect inside the viewer (object-fit: contain), in CSS px
function dispRect() {
  const r = viewer.getBoundingClientRect();
  const m = media();
  const sw = m?.width || 16, sh = m?.height || 9;
  const s = Math.min(r.width / sw, r.height / sh);
  const w = sw * s, hh = sh * s;
  return { x: (r.width - w) / 2, y: (r.height - hh) / 2, w, h: hh, s, sw, sh };
}
const toScr = (d, rect) => ({ x: d.x + rect.x * d.s, y: d.y + rect.y * d.s, w: rect.w * d.s, h: rect.h * d.s });

function faceRectNorm() {
  if (localFace) return localFace;
  return S.preview?.edit?.facecam_region || null;
}

// ------------------------------------------------------------------ draw (called from the frame loop)
export function drawOverlay() {
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, cv.width, cv.height);
  const pv = S.preview, m = media();
  if (!pv || !m) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const d = dispRect();
  const plan = pv.plan, kf = plan.keyframes[0];
  const W = cv.width / dpr, H = cv.height / dpr;

  if (plan.mode === 'fit_blur') {
    const r = toScr(d, kf.gameplay);
    strokeRect(r, TEAL, 2);
    tag(r.x + 6, r.y + 6, 'FIT · FULL FRAME + BLUR FILL', TEAL, '#06302d');
  } else {
    const g = toScr(d, kf.gameplay);
    // dim everything outside the gameplay crop
    ctx.save();
    ctx.beginPath();
    ctx.rect(0, 0, W, H);
    ctx.rect(g.x, g.y, g.w, g.h);
    ctx.fillStyle = 'rgba(8,8,8,.48)';
    ctx.fill('evenodd');
    ctx.restore();
    strokeRect(g, TEAL, 2);
    // centre line
    ctx.save(); ctx.setLineDash([4, 4]); ctx.strokeStyle = 'rgba(47,224,210,.6)'; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(g.x + g.w / 2, g.y); ctx.lineTo(g.x + g.w / 2, g.y + g.h); ctx.stroke(); ctx.restore();
    // grab bars
    ctx.fillStyle = TEAL;
    for (const x of [g.x, g.x + g.w]) ctx.fillRect(x - 3, g.y + g.h / 2 - 14, 6, 28);
    const cx = Math.round(((kf.gameplay.x + kf.gameplay.w / 2) / plan.source_width) * 1000) / 10;
    const lab = plan.mode === 'facecam_split' ? `GAMEPLAY PANEL · X ${cx}%` : `9:16 / CROP · X ${cx}%`;
    tag(g.x + g.w / 2, g.y + 6, lab, TEAL, '#06302d', true);

    if (plan.mode === 'facecam_split' && kf.facecam) {
      const fp = toScr(d, kf.facecam);
      ctx.save(); ctx.setLineDash([5, 4]); strokeRect(fp, 'rgba(255,92,122,.75)', 1); ctx.restore();
      ctx.fillStyle = 'rgba(255,92,122,.9)'; ctx.font = '600 9px "JetBrains Mono", monospace';
      ctx.textAlign = 'right'; ctx.fillText('face panel crop', fp.x + fp.w - 4, fp.y + 12); ctx.textAlign = 'left';
    }
  }
  const fr = faceRectNorm();
  if (fr && (layoutOf() === 'facecam_split' || plan.mode === 'facecam_split')) {
    const r = { x: d.x + fr.x * d.w, y: d.y + fr.y * d.h, w: fr.w * d.w, h: fr.h * d.h };
    ctx.fillStyle = 'rgba(255,92,122,.12)'; ctx.fillRect(r.x, r.y, r.w, r.h);
    strokeRect(r, CORAL, 2);
    ctx.fillStyle = CORAL; ctx.fillRect(r.x + r.w - 6, r.y + r.h - 6, 10, 10);
    tag(r.x, r.y + r.h + 4, 'FACE · manual', CORAL, '#fff');
  }
  if (drag?.kind === 'face-draw' && drag.rect) {
    const n = drag.rect;
    const r = { x: d.x + n.x * d.w, y: d.y + n.y * d.h, w: n.w * d.w, h: n.h * d.h };
    ctx.save(); ctx.setLineDash([6, 4]); strokeRect(r, CORAL, 2); ctx.restore();
  }
}

function strokeRect(r, color, lw) {
  ctx.strokeStyle = color; ctx.lineWidth = lw;
  ctx.strokeRect(r.x + .5, r.y + .5, r.w - 1, r.h - 1);
}
function tag(x, y, text, bg, fg, centered = false) {
  ctx.font = '700 10px "JetBrains Mono", "DejaVu Sans Mono", monospace';
  const w = ctx.measureText(text).width + 10;
  const x0 = centered ? x - w / 2 : x;
  ctx.fillStyle = bg; ctx.fillRect(x0, y, w, 16);
  ctx.fillStyle = fg; ctx.textBaseline = 'middle'; ctx.fillText(text, x0 + 5, y + 8.5);
  ctx.textBaseline = 'alphabetic';
}

// ------------------------------------------------------------------ interaction
function pt(e) {
  const r = cv.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}
function hit(p) {
  const pv = S.preview; if (!pv) return null;
  const d = dispRect();
  const plan = pv.plan, kf = plan.keyframes[0];
  const split = layoutOf() === 'facecam_split';
  const fr = faceRectNorm();
  if (split && fr) {
    const r = { x: d.x + fr.x * d.w, y: d.y + fr.y * d.h, w: fr.w * d.w, h: fr.h * d.h };
    if (Math.abs(p.x - (r.x + r.w)) < 9 && Math.abs(p.y - (r.y + r.h)) < 9) return 'face-resize';
    if (p.x >= r.x && p.x <= r.x + r.w && p.y >= r.y && p.y <= r.y + r.h) return 'face-move';
  }
  if (plan.mode !== 'fit_blur') {
    const g = toScr(d, kf.gameplay);
    if (p.x >= g.x - 6 && p.x <= g.x + g.w + 6 && p.y >= g.y && p.y <= g.y + g.h) return 'crop';
  }
  return null;
}

function onDown(e) {
  const pv = S.preview; if (!pv) return;
  const p = pt(e), d = dispRect();
  const nx = clamp((p.x - d.x) / d.w, 0, 1), ny = clamp((p.y - d.y) / d.h, 0, 1);
  if (drawMode) {
    drag = { kind: 'face-draw', x0: nx, y0: ny, rect: null };
  } else {
    const k = hit(p);
    if (!k) return;
    if (k === 'crop') {
      const g = pv.plan.keyframes[0].gameplay;
      drag = { kind: 'crop', px: p.x, startCenter: (g.x + g.w / 2) / pv.plan.source_width, center: null };
    } else {
      localFace = { ...faceRectNorm() };
      drag = { kind: k, nx, ny, orig: { ...localFace } };
    }
  }
  cv.setPointerCapture(e.pointerId);
  e.preventDefault();
}

function onMove(e) {
  const p = pt(e);
  if (!drag) {
    if (!drawMode) {
      const k = hit(p);
      cv.style.cursor = k === 'crop' ? 'ew-resize' : k === 'face-resize' ? 'nwse-resize' : k === 'face-move' ? 'move' : '';
    }
    return;
  }
  const d = dispRect();
  const nx = clamp((p.x - d.x) / d.w, 0, 1), ny = clamp((p.y - d.y) / d.h, 0, 1);
  if (drag.kind === 'crop') {
    const dc = (p.x - drag.px) / d.w;
    drag.center = setLocalCropCenter(clamp(drag.startCenter + dc, 0, 1));
    S.dirty = true;
  } else if (drag.kind === 'face-draw') {
    const x = Math.min(drag.x0, nx), y = Math.min(drag.y0, ny);
    drag.rect = { x, y, w: Math.abs(nx - drag.x0), h: Math.abs(ny - drag.y0) };
  } else if (drag.kind === 'face-move') {
    const o = drag.orig;
    localFace = { ...o, x: clamp(o.x + nx - drag.nx, 0, 1 - o.w), y: clamp(o.y + ny - drag.ny, 0, 1 - o.h) };
  } else if (drag.kind === 'face-resize') {
    const o = drag.orig;
    localFace = { ...o, w: clamp(nx - o.x, 0.03, 1 - o.x), h: clamp(ny - o.y, 0.03, 1 - o.y) };
  }
  S.dirty = true;
}

async function onUp() {
  const dg = drag; drag = null;
  if (!dg) return;
  if (dg.kind === 'crop') {
    if (dg.center != null && Math.abs(dg.center - dg.startCenter) > 0.0005) await patchClip({ crop_center_x: dg.center });
  } else if (dg.kind === 'face-draw') {
    const r = dg.rect;
    setDrawMode(false);
    if (!r || r.w < 0.02 || r.h < 0.02) { S.dirty = true; return; }
    localFace = normRect(r);
    await patchClip({ facecam_region: localFace });
  } else {
    const r = normRect(localFace);
    const o = dg.orig;
    if (Math.abs(r.x - o.x) + Math.abs(r.y - o.y) + Math.abs(r.w - o.w) + Math.abs(r.h - o.h) > 0.001) {
      await patchClip({ facecam_region: r });
    } else localFace = null;
  }
  S.dirty = true;
}
function normRect(r) {
  const x = round(clamp(r.x, 0, 0.99), 4), y = round(clamp(r.y, 0, 0.99), 4);
  return { x, y, w: round(clamp(r.w, 0.01, 1 - x), 4), h: round(clamp(r.h, 0.01, 1 - y), 4) };
}

// ------------------------------------------------------------------ per-frame readouts
let lastCapKey = null;
export function tickProgram() {
  const v = video(), c = cand();
  const fps = media()?.fps || 30;
  $('#timecode').textContent = timecode(v.currentTime, fps);
  if (c) {
    const rel = v.currentTime - c.start;
    $('#clipTime').textContent = `CLIP ${fmtT(rel, 2)} / ${fmtT(c.end - c.start, 2)}`;
    if (S.loop && !v.paused && v.currentTime >= c.end) { v.currentTime = c.start; }
    const ch = activeChunk(S.preview?.captions, rel);
    const key = ch ? `${ch.index}:${chunkText(ch)}` : (S.preview?.captions ? 'none' : 'off');
    if (key !== lastCapKey) {
      lastCapKey = key;
      const el = $('#captionLine');
      el.classList.toggle('none', !ch);
      el.textContent = ch ? chunkText(ch).replace(/\n/g, ' ') : (S.preview?.captions ? '· no caption at playhead ·' : '· captions disabled ·');
    }
  } else {
    $('#clipTime').textContent = 'NO CLIP SELECTED';
  }
}
export function invalidateCaptionLine() { lastCapKey = null; }
