// Multitrack sequence for the selected clip: source thumbs + trim, captions (retime), face, audio, crop keyframes, effects.
import { S, $, h, on, cand, P, api, fmtT, clamp, round, chunkText, activeChunk, layoutOf, hasFacePanel } from './core.js';
import { video, patchClip, saveCaptions } from './actions.js';
import { selectChunk, onChunkSelect } from './inspector.js';

let area, win = null, winKey = '', wave = null, waveKey = '', tip;
let trim = null;     // {which, start, end} while dragging a trim handle
let capDrag = null;  // {i, edge, track}
const MIN_CLIP = 1.0, MIN_CHUNK = 0.15;

export function initTimeline() {
  area = $('#tlArea');
  tip = h('div', { class: 'tl-tip', hidden: true });
  document.body.append(tip);
  new ResizeObserver(() => { thumbsKey = ''; render(); }).observe(area);
  on('select', () => { win = null; render(); });
  on('preview', render);
  on('project', render);
  onChunkSelect(() => { const c = cand(); if (c && S.preview) renderCaptions(c, S.preview); });

  // click / drag anywhere that is not a handle or block = scrub
  area.addEventListener('pointerdown', e => {
    if (e.target.closest('.trim, .cap-block, .cap-edge') || !win) return;
    area.setPointerCapture(e.pointerId);
    const v = video(); v.pause();
    const seek = ev => { v.currentTime = clamp(tAt(ev.clientX), 0, S.project?.media?.duration || 1e9); };
    seek(e);
    const mv = ev => seek(ev);
    const up = () => { area.removeEventListener('pointermove', mv); area.removeEventListener('pointerup', up); };
    area.addEventListener('pointermove', mv);
    area.addEventListener('pointerup', up);
  });
}

const pct = t => `${((t - win.w0) / (win.w1 - win.w0)) * 100}%`;
function tAt(clientX) {
  const r = $('#trSrc').getBoundingClientRect();
  return win.w0 + clamp((clientX - r.left) / r.width, 0, 1) * (win.w1 - win.w0);
}

function computeWin(c) {
  const dur = S.project?.media?.duration || c.end + 5;
  const pad = Math.max(3, (c.end - c.start) * 0.2);
  return { w0: Math.max(0, c.start - pad), w1: Math.min(dur, c.end + pad) };
}

function render() {
  const c = cand(), pv = S.preview;
  $('#tlEmpty').hidden = !!(c && pv);
  if (!c || !pv) {
    $('#seqTitle').textContent = 'SEQUENCE';
    $('#tlReadout').textContent = '';
    return;
  }
  if (!trim && (!win || c.start < win.w0 || c.end > win.w1)) win = computeWin(c);
  $('#seqTitle').textContent = `SEQUENCE / SHORT_${String(c.rank).padStart(2, '0')} · ${c.title}`;
  const s = trim ? trim.start : c.start, e = trim ? trim.end : c.end;
  $('#tlReadout').textContent = `IN ${fmtT(s, 2)} · OUT ${fmtT(e, 2)} · ${(e - s).toFixed(2)}s · WINDOW ${fmtT(win.w0)}–${fmtT(win.w1)}`;
  renderRuler();
  renderSource(s, e);
  renderCaptions(c, pv);
  renderFace(c, pv, s, e);
  renderKf(c, pv);
  renderWave(s, e);
}

function renderRuler() {
  const span = win.w1 - win.w0;
  const step = span > 60 ? 10 : span > 25 ? 5 : span > 10 ? 2 : 1;
  const ticks = [];
  for (let t = Math.ceil(win.w0); t <= win.w1; t += 1) {
    if (span > 40 && t % 2) continue;
    const major = t % step === 0;
    ticks.push(h('div', { class: `tl-tick ${major ? 'major' : ''}`, style: { left: pct(t) } }, major ? h('span', {}, fmtT(t, 0)) : null));
  }
  $('#tlRuler').replaceChildren(...ticks);
}

let thumbsKey = '';
function renderSource(s, e) {
  const tr = $('#trSrc');
  const width = tr.getBoundingClientRect().width || 800;
  const n = clamp(Math.round(width / 74), 4, 30);
  const key = `${S.pid}:${win.w0}:${win.w1}:${n}`;
  let thumbs = tr.querySelector('.tl-thumbs');
  if (key !== thumbsKey || !thumbs) {
    thumbsKey = key;
    thumbs = h('div', { class: 'tl-thumbs' }, Array.from({ length: n }, (_, i) => {
      const t = win.w0 + (i + 0.5) * (win.w1 - win.w0) / n;
      return h('img', { src: P(S.pid, `/frame?t=${t.toFixed(1)}&w=160`), alt: '', draggable: 'false' });
    }));
  }
  const dimL = h('div', { class: 'tl-dim', style: { left: 0, width: pct(s) } });
  const dimR = h('div', { class: 'tl-dim', style: { left: pct(e), right: 0 } });
  const range = h('div', { class: 'tl-range', style: { left: pct(s), width: `calc(${pct(e)} - ${pct(s)})` } },
    h('span', { class: 'tl-range-tag' }, `CLIP ${(e - s).toFixed(1)}s`));
  const hs = h('div', { class: `trim ${trim?.which === 'start' ? 'drag' : ''}`, style: { left: pct(s) }, title: 'Drag to trim the clip start', 'data-which': 'start' });
  const he = h('div', { class: `trim ${trim?.which === 'end' ? 'drag' : ''}`, style: { left: pct(e) }, title: 'Drag to trim the clip end', 'data-which': 'end' });
  for (const hd of [hs, he]) hd.addEventListener('pointerdown', startTrim);
  tr.replaceChildren(thumbs, dimL, dimR, range, hs, he);
}

function startTrim(e) {
  e.stopPropagation(); e.preventDefault();
  const c = cand(); if (!c) return;
  const el = window;
  trim = { which: e.currentTarget.dataset.which, start: c.start, end: c.end };
  const dur = S.project?.media?.duration || 1e9;
  const mv = ev => {
    const t = round(tAt(ev.clientX), 2);
    if (trim.which === 'start') trim.start = clamp(t, 0, trim.end - MIN_CLIP);
    else trim.end = clamp(t, trim.start + MIN_CLIP, dur);
    showTip(ev, `${trim.which.toUpperCase()} ${fmtT(trim.which === 'start' ? trim.start : trim.end, 2)}`);
    render();
  };
  const up = async () => {
    el.removeEventListener('pointermove', mv); el.removeEventListener('pointerup', up);
    hideTip();
    const t = trim; trim = null;
    if (Math.abs(t.start - c.start) > 0.005 || Math.abs(t.end - c.end) > 0.005) {
      const body = {};
      if (Math.abs(t.start - c.start) > 0.005) body.start = round(t.start, 2);
      if (Math.abs(t.end - c.end) > 0.005) body.end = round(t.end, 2);
      await patchClip(body);
      const v = video(); if (v.currentTime < t.start || v.currentTime > t.end) v.currentTime = t.start;
    } else render();
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', up);
}

function renderCaptions(c, pv) {
  const tr = $('#trCap');
  const track = capDrag ? capDrag.track : pv.captions;
  if (!track) { tr.replaceChildren(h('span', { class: 'tl-muted' }, 'captions disabled in config')); return; }
  if (!track.chunks.length) { tr.replaceChildren(h('span', { class: 'tl-muted' }, 'no speech in this range → no captions')); return; }
  tr.replaceChildren(...track.chunks.map((ch, i) => {
    const b = h('div', {
      class: `cap-block ${i === S.selChunk ? 'sel' : ''}`, 'data-i': i,
      style: { left: pct(c.start + ch.start), width: `calc(${pct(c.start + ch.end)} - ${pct(c.start + ch.start)})` },
      title: `${chunkText(ch).replace(/\n/g, ' ')}  (${ch.start.toFixed(2)}–${ch.end.toFixed(2)}s)`,
    }, chunkText(ch).replace(/\n/g, ' '),
    h('span', { class: 'cap-edge l', 'data-edge': 'start' }), h('span', { class: 'cap-edge r', 'data-edge': 'end' }));
    b.addEventListener('pointerdown', e => {
      const edge = e.target.dataset.edge;
      e.stopPropagation();
      if (!edge) { selectChunk(i, true); return; }
      startCapDrag(e, i, edge);
    });
    return b;
  }));
  lastCap = -2;
  markActiveCap();
}

function startCapDrag(e, i, edge) {
  e.preventDefault();
  const pv = S.preview, c = cand();
  const el = window;
  const orig = pv.captions.chunks[i];
  capDrag = { i, edge, track: structuredClone(pv.captions), moved: false };
  const dur = c.end - c.start;
  const mv = ev => {
    const chs = capDrag.track.chunks, ch = chs[i];
    const t = round(tAt(ev.clientX) - c.start, 2);
    if (edge === 'start') {
      const lo = i > 0 ? chs[i - 1].end : 0;
      ch.start = clamp(t, lo, ch.end - MIN_CHUNK);
    } else {
      const hi = i < chs.length - 1 ? chs[i + 1].start : dur;
      ch.end = clamp(t, ch.start + MIN_CHUNK, hi);
    }
    capDrag.moved = true;
    showTip(ev, `#${i + 1} ${ch.start.toFixed(2)}–${ch.end.toFixed(2)}s`);
    renderCaptions(c, pv);
  };
  const up = async () => {
    el.removeEventListener('pointermove', mv); el.removeEventListener('pointerup', up);
    hideTip();
    const d = capDrag; capDrag = null;
    if (!d.moved) { renderCaptions(c, pv); return; }
    const ch = d.track.chunks[i];
    remapWords(ch, orig.start, orig.end);
    S.selChunk = i;
    await saveCaptions(d.track, `Caption ${i + 1} retimed → ${ch.start.toFixed(2)}–${ch.end.toFixed(2)}s`);
  };
  el.addEventListener('pointermove', mv);
  el.addEventListener('pointerup', up);
}

// keep word timing proportional inside a retimed chunk
function remapWords(ch, o0, o1) {
  const k = (ch.end - ch.start) / Math.max(0.001, o1 - o0);
  for (const line of ch.lines) for (const w of line) {
    w.start = round(ch.start + (w.start - o0) * k, 3);
    w.end = round(Math.min(ch.end, ch.start + (w.end - o0) * k), 3);
  }
}

function renderFace(c, pv, s, e) {
  const tr = $('#trFace');
  const fr = pv.edit?.facecam_region, fc = pv.facecam;
  const r = fr || (fc?.region ? fc.region : null);
  if (hasFacePanel(pv.plan.mode) && r) {
    const src = fr ? 'manual' : `auto ${Math.round(fc.confidence * 100)}%`;
    tr.replaceChildren(h('div', { class: 'face-bar', style: { left: pct(s), width: `calc(${pct(e)} - ${pct(s)})` } },
      `FACE · ${src} x${r.x.toFixed(2)} y${r.y.toFixed(2)} ${r.w.toFixed(2)}×${r.h.toFixed(2)}`));
  } else {
    const why = fc?.region
      ? `facecam candidate ${Math.round(fc.confidence * 100)}% (${fc.method}) · below auto threshold or layout has no face panel`
      : fc ? `no facecam detected (${fc.detector}) · layout ${pv.plan.mode}` : 'facecam detection has not run for this project';
    tr.replaceChildren(h('span', { class: 'tl-muted' }, why));
  }
}

function renderKf(c, pv) {
  const plan = pv.plan;
  $('#trKf').replaceChildren(...plan.keyframes.map(k => h('div', {
    class: 'kf-dmd', style: { left: pct(c.start + k.t) },
    title: `crop keyframe t=${k.t}s · gameplay ${k.gameplay.x},${k.gameplay.y} ${k.gameplay.w}×${k.gameplay.h}`,
  })), h('span', { class: 'tl-muted', style: { left: `calc(${pct(c.start)} + 12px)` } }, plan.keyframes.length > 1 ? `${plan.keyframes.length} tracking keyframes` : '1 static keyframe'));
}

// ------------------------------------------------------------------ waveform
async function renderWave(s, e) {
  const cvs = $('#waveCanvas');
  const r = cvs.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const bins = clamp(Math.round(r.width / 3), 50, 1200);
  const key = `${S.pid}:${win.w0.toFixed(2)}:${win.w1.toFixed(2)}:${bins}`;
  if (key !== waveKey) {
    waveKey = key;
    wave = null;
    try {
      wave = await api(P(S.pid, `/waveform?start=${win.w0.toFixed(2)}&end=${win.w1.toFixed(2)}&bins=${bins}`));
    } catch (err) {
      wave = { error: err.message };
    }
    if (key !== waveKey) return;
  }
  if (!wave) return;
  cvs.width = Math.round(r.width * dpr); cvs.height = Math.round(r.height * dpr);
  const g = cvs.getContext('2d');
  g.clearRect(0, 0, cvs.width, cvs.height);
  if (wave.error) {
    g.fillStyle = '#7d7568'; g.font = `${10 * dpr}px "JetBrains Mono", monospace`;
    g.fillText(`waveform unavailable: ${wave.error}`, 8 * dpr, cvs.height / 2 + 3 * dpr);
    return;
  }
  const n = wave.bins.length, bw = cvs.width / n, mid = cvs.height / 2;
  for (let i = 0; i < n; i++) {
    const t = win.w0 + (i + 0.5) * (win.w1 - win.w0) / n;
    const inside = t >= s && t <= e;
    const a = Math.max(0.03, wave.bins[i]) * (mid - 2 * dpr);
    g.fillStyle = inside ? '#0b9a94' : '#bdb3a0';
    g.fillRect(i * bw + bw * 0.15, mid - a, Math.max(1, bw * 0.7), a * 2);
  }
  // -60..0 dBFS scale; mark the median level
  const med = clamp((wave.median_db + 60) / 60, 0, 1) * (mid - 2 * dpr);
  g.strokeStyle = 'rgba(239,71,102,.5)'; g.setLineDash([3 * dpr, 3 * dpr]); g.lineWidth = dpr;
  for (const y of [mid - med, mid + med]) { g.beginPath(); g.moveTo(0, y); g.lineTo(cvs.width, y); g.stroke(); }
}

// ------------------------------------------------------------------ per frame
let lastCap = -2;
export function tickTimeline() {
  const ph = $('#tlPlayhead');
  if (!win || !cand()) { ph.hidden = true; return; }
  const t = video().currentTime;
  const inWin = t >= win.w0 && t <= win.w1;
  ph.hidden = !inWin;
  if (inWin) {
    // playhead lives in tl-area; tracks start at x=0 of the area
    ph.style.left = pct(t);
  }
  markActiveCap();
}
function markActiveCap() {
  const c = cand(), tr = S.preview?.captions;
  if (!c || !tr) return;
  const ch = activeChunk(tr, video().currentTime - c.start);
  const i = ch ? ch.index : -1;
  if (i === lastCap && !capDrag) return;
  lastCap = i;
  for (const b of document.querySelectorAll('.cap-block')) b.classList.toggle('active', +b.dataset.i === i);
}

function showTip(ev, text) { tip.hidden = false; tip.textContent = text; tip.style.left = `${ev.clientX + 12}px`; tip.style.top = `${ev.clientY - 26}px`; }
function hideTip() { tip.hidden = true; }
