// Inspector / live edit: layout, crop, trim, captions, score breakdown, audio settings, keyframes.
import { S, $, h, on, cand, fmtT, round, clamp, pretty, chunkText, activeChunk, layoutOf, kfAt, clipT, hasFacePanel } from './core.js';
import { video, patchClip, saveCaptions, resetCaptions, setLocalCropCenter } from './actions.js';

const LAYOUTS = [
  ['auto', 'auto · split if a facecam is found'],
  ['gameplay', 'gameplay · 9:16 crop'],
  ['fit_blur', 'fit_blur · whole frame + blur'],
  ['facecam_split', 'facecam_split · face top / game below'],
  ['facecam_pip', 'facecam_pip · game + face corner box'],
];

export function initInspector() {
  on('preview', render);
  on('select', render);
  on('project', () => { if (!S.cid) render(); });
  on('chunk', () => { renderChunks(); renderEditor(); });
}

function sec(title, right, ...body) {
  return h('div', { class: 'sec' }, h('div', { class: 'sec-h' }, h('span', {}, title), right ? h('span', { class: 'r' }, right) : null), ...body);
}
function row(label, ...ctl) { return h('div', { class: 'i-row' }, h('label', {}, label), ...ctl); }

function render() {
  const body = $('#inspBody');
  const pv = S.preview, c = cand();
  if (!S.cid || !pv || !c) {
    body.replaceChildren(h('div', { class: 'empty' }, S.candidates.length ? 'Select a moment from the media bin.' : 'Nothing to inspect yet.'));
    return;
  }
  const scrollTop = body.scrollTop;
  const plan = pv.plan, kf = plan.keyframes[0];
  const cfg = S.config?.config;
  const layout = layoutOf();
  const md = S.project?.media;

  // ---- clip
  const startIn = h('input', { type: 'number', step: '0.1', min: 0, value: c.start.toFixed(2), id: 'inStart' });
  const endIn = h('input', { type: 'number', step: '0.1', min: 0, value: c.end.toFixed(2), id: 'inEnd' });
  const applyRange = () => {
    const s = parseFloat(startIn.value), e = parseFloat(endIn.value);
    if (!isFinite(s) || !isFinite(e)) return;
    if (Math.abs(s - c.start) < 0.005 && Math.abs(e - c.end) < 0.005) return;
    const body = {};
    if (Math.abs(s - c.start) >= 0.005) body.start = round(s, 2);
    if (Math.abs(e - c.end) >= 0.005) body.end = round(e, 2);
    patchClip(body);
  };
  startIn.onchange = applyRange; endIn.onchange = applyRange;
  const clipSec = sec('CLIP', `${c.category} · #${c.rank}`,
    h('div', { class: 'i-title' }, c.title),
    row('START', startIn, h('span', { class: 'i-unit' }, `s · ${fmtT(c.start, 2)}`)),
    row('END', endIn, h('span', { class: 'i-unit' }, `s · ${fmtT(c.end, 2)}`)),
    h('div', { class: 'i-meta' }, `DURATION ${c.duration.toFixed(2)}s · SOURCE ${md ? md.duration.toFixed(1) + 's' : '--'}${c.user_adjusted ? ' · TRIMMED BY YOU' : ''}`),
    pv.captions?.edited && c.user_adjusted ? h('div', { class: 'i-note' }, 'Hand-edited captions keep their clip-relative timing when you trim; check them after changing the start.') : null);

  // ---- reframe
  const laySel = h('select', { id: 'selLayout' }, LAYOUTS.map(([v, l]) => h('option', { value: v, selected: v === layout }, l)));
  laySel.onchange = () => patchClip({ layout: laySel.value });
  const center = (kf.gameplay.x + kf.gameplay.w / 2) / plan.source_width;
  const cropDisabled = plan.mode === 'fit_blur';
  const range = h('input', { type: 'range', min: 0, max: 100, step: 0.1, value: (center * 100).toFixed(1), id: 'cropRange', disabled: cropDisabled });
  const num = h('input', { type: 'number', min: 0, max: 100, step: 0.1, value: (center * 100).toFixed(1), id: 'cropNum', disabled: cropDisabled });
  range.oninput = () => { const v = setLocalCropCenter(range.value / 100); num.value = (v * 100).toFixed(1); };
  range.onchange = () => patchClip({ crop_center_x: round(clamp(range.value / 100, 0, 1), 4) });
  num.onchange = () => { const v = setLocalCropCenter(clamp(parseFloat(num.value) / 100 || 0, 0, 1)); patchClip({ crop_center_x: v }); };
  const fr = pv.edit?.facecam_region;
  const fc = pv.facecam;
  const faceLine = fr
    ? `FACE · manual  x ${fr.x.toFixed(3)}  y ${fr.y.toFixed(3)}  w ${fr.w.toFixed(3)}  h ${fr.h.toFixed(3)}`
    : fc?.region
      ? `FACE · auto ${fc.method} ${Math.round(fc.confidence * 100)}% (${fc.detector})  x ${fc.region.x.toFixed(3)}  y ${fc.region.y.toFixed(3)}  w ${fc.region.w.toFixed(3)}  h ${fc.region.h.toFixed(3)}`
      : `FACE · none detected${fc ? ` (${fc.detector}, ${fc.samples} samples)` : ''}: use FACE BOX to draw one`;
  const reframe = sec('REFRAME', `plan: ${plan.mode} · ${plan.source}`,
    row('LAYOUT', laySel),
    row('CROP X', range, num, h('span', { class: 'i-unit' }, '%')),
    cropDisabled ? h('div', { class: 'i-meta' }, 'fit_blur shows the whole 16:9 frame; crop X does not apply.') : null,
    h('div', { class: 'i-meta' }, faceLine),
    ...(fc?.notes || []).map(n => h('div', { class: 'i-note' }, n)),
    pv.track ? h('div', { class: 'i-note teal' }, `tracking: ${pv.track.notes.join(' · ')}`) : null,
    ...(plan.notes || []).map(n => h('div', { class: 'i-note' }, n)));

  // ---- captions
  const presets = S.presets.length ? S.presets : [pv.captions?.preset].filter(Boolean);
  const curPreset = pv.edit?.caption_preset || cfg?.captions?.preset;
  const preSel = h('select', { id: 'selPreset' }, presets.map(p => h('option', { value: p, selected: p === curPreset }, p)));
  preSel.onchange = () => patchClip({ caption_preset: preSel.value });
  let capBody;
  if (!pv.captions) capBody = [h('div', { class: 'i-note' }, 'Captions are disabled in the config.')];
  else capBody = [
    h('div', { class: 'i-meta' }, `${pv.captions.chunks.length} CHUNKS · ${pv.captions.source_word_count} WORDS · ${pv.captions.edited ? 'HAND-EDITED' : 'AUTO FROM TRANSCRIPT'} · ${pv.captions.layout.font_family} ${pv.captions.layout.font_size_px}px`),
    pv.captions.edited && pv.captions.preset !== curPreset ? h('div', { class: 'i-note' }, `Edited track was saved with preset “${pv.captions.preset}”; text/timing stay as edited.`) : null,
    h('div', { class: 'chunks', id: 'chunkList' }),
    h('div', { id: 'chunkEditor' }),
  ];
  const captions = sec('CAPTIONS', pv.captions?.edited ? 'edited' : 'auto', row('PRESET', preSel), ...capBody);

  // ---- score
  const weights = S.weights || {};
  const maxW = Math.max(0.0001, ...Object.values(weights));
  const score = sec('WHY THIS MOMENT', c.passes_threshold ? 'passes threshold' : 'below threshold',
    h('div', { class: 'i-row' }, h('span', { class: 'score-big' }, c.score.toFixed(3)), h('span', { class: 'i-meta' }, `SCORE · CONFIDENCE ${c.confidence.toFixed(2)}`)),
    h('div', { class: 'reason' }, c.reason),
    ...Object.entries(c.contributions).map(([k, v]) => h('div', { class: 'contrib', title: `feature ${(c.features[k] ?? 0).toFixed(3)} × weight ${(weights[k] ?? 0).toFixed(2)} = ${v.toFixed(3)}` },
      h('span', { class: 'lab' }, pretty(k)),
      h('span', { class: 'bar' }, h('b', { style: { width: `${((weights[k] || 0) / maxW) * 100}%` } }), h('i', { style: { width: `${(v / maxW) * 100}%` } })),
      h('span', { class: 'val' }, v.toFixed(3)))),
    h('div', { class: 'i-meta', style: { margin: '6px 0 4px' } }, 'RAW FEATURES'),
    h('div', { class: 'kv' }, Object.entries(c.features).flatMap(([k, v]) => [h('span', {}, pretty(k)), h('span', {}, typeof v === 'number' ? v.toFixed(3) : String(v))])));

  // ---- audio (read only)
  const a = cfg?.audio, ex = cfg?.export;
  const audio = sec('AUDIO / EXPORT', 'read-only · config',
    a ? h('div', { class: 'kv' },
      h('span', {}, 'normalization'), h('span', {}, a.normalization ? 'on (loudnorm)' : 'off'),
      h('span', {}, 'target'), h('span', {}, `${a.target_lufs} LUFS`),
      h('span', {}, 'true peak'), h('span', {}, `${a.true_peak} dBTP`),
      h('span', {}, 'loudness range'), h('span', {}, `${a.loudness_range} LU`),
      h('span', {}, 'voice enhance'), h('span', {}, a.voice_enhancement),
      h('span', {}, 'video'), h('span', {}, `${ex.video_codec} · crf ${ex.crf} · ${ex.preset}`),
      h('span', {}, 'audio'), h('span', {}, `${ex.audio_codec} ${ex.audio_bitrate}`)) : h('div', { class: 'i-meta' }, 'config unavailable'));

  // ---- keyframes
  const kfs = sec('KEYFRAMES', `${plan.keyframes.length} · ${plan.source}`,
    h('div', { class: 'kf-list' }, plan.keyframes.map(k => h('div', { class: 'kf-item' }, h('span', { class: 'kf-dia' }),
      h('div', {},
        h('div', {}, `t = ${k.t.toFixed(2)}s`),
        h('div', {}, `gameplay ${k.gameplay.x},${k.gameplay.y} ${k.gameplay.w}×${k.gameplay.h}`),
        k.facecam ? h('div', { class: 'face' }, `facecam ${k.facecam.x},${k.facecam.y} ${k.facecam.w}×${k.facecam.h}`) : null)))),
    h('div', { class: 'i-note teal' }, plan.keyframes.length > 1
      ? 'Crop follows the action; the render interpolates linearly between keyframes. Dragging the crop pins it by hand.'
      : 'Static plan: one keyframe (tracking off, crop placed by hand, or no dominant subject).'));

  body.replaceChildren(clipSec, reframe, captions, score, audio, kfs);
  renderChunks(); renderEditor();
  body.scrollTop = scrollTop;
}

export function syncCropInputs() {
  const pv = S.preview; if (!pv) return;
  const kf = kfAt(pv.plan, clipT());
  const center = (kf.gameplay.x + kf.gameplay.w / 2) / pv.plan.source_width;
  const r = $('#cropRange'), n = $('#cropNum');
  if (r && document.activeElement !== r) r.value = (center * 100).toFixed(1);
  if (n && document.activeElement !== n) n.value = (center * 100).toFixed(1);
}

let lastActive = -2;
export function tickInspector() {
  const pv = S.preview, c = cand();
  if (!pv?.captions || !c) return;
  const ch = activeChunk(pv.captions, video().currentTime - c.start);
  const idx = ch ? ch.index : -1;
  if (idx !== lastActive) {
    lastActive = idx;
    for (const el of document.querySelectorAll('.chunk-row')) el.classList.toggle('active', +el.dataset.i === idx);
  }
}

function renderChunks() {
  const list = $('#chunkList');
  const tr = S.preview?.captions;
  if (!list || !tr) return;
  lastActive = -2;
  list.replaceChildren(...tr.chunks.map((ch, i) => {
    const el = h('div', { class: `chunk-row ${i === S.selChunk ? 'sel' : ''}`, 'data-i': i },
      h('span', { class: 'cn' }, String(i + 1).padStart(2, '0')),
      h('span', { class: 'ct' }, `${ch.start.toFixed(2)}–${ch.end.toFixed(2)}`),
      h('span', { class: 'tx' }, chunkText(ch).replace(/\n/g, ' / ')));
    el.onclick = () => selectChunk(i, true);
    return el;
  }));
  const sel = list.querySelector('.sel');
  if (sel) sel.scrollIntoView({ block: 'nearest' });
}

export function selectChunk(i, seek) {
  S.selChunk = i;
  const c = cand(), ch = S.preview?.captions?.chunks[i];
  if (seek && c && ch) { const v = video(); v.pause(); v.currentTime = c.start + ch.start + 0.01; }
  for (const fn of chunkListeners) fn();
  renderChunks(); renderEditor();
}
const chunkListeners = new Set();
export const onChunkSelect = fn => chunkListeners.add(fn);

function renderEditor() {
  const box = $('#chunkEditor');
  const tr = S.preview?.captions;
  if (!box || !tr) return;
  const resetBtn = h('button', { class: 'btn btn-ghost btn-sm', id: 'btnResetCaps', disabled: !tr.edited, title: tr.edited ? 'Delete the hand-edited track and rebuild captions from the transcript' : 'Track is already automatic' }, 'RESET TO AUTO');
  resetBtn.onclick = () => resetCaptions();
  const i = S.selChunk;
  if (i == null || !tr.chunks[i]) {
    box.replaceChildren(h('div', { class: 'i-meta', style: { marginTop: '6px' } }, 'Click a chunk (here or on the timeline) to edit its text. Drag block edges on the timeline to retime.'),
      h('div', { class: 'btn-row' }, resetBtn));
    return;
  }
  const ch = tr.chunks[i];
  const ta = h('textarea', { class: 'cap-edit', id: 'capEdit', spellcheck: 'true', rows: Math.max(2, ch.lines.length) }, chunkText(ch));
  const save = h('button', { class: 'btn btn-accent btn-sm', id: 'btnSaveCap' }, 'SAVE CHUNK');
  const revert = h('button', { class: 'btn btn-ghost btn-sm' }, 'REVERT');
  ta.addEventListener('keydown', e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save.click(); });
  revert.onclick = () => { ta.value = chunkText(ch); };
  save.onclick = async () => {
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    const track = structuredClone(tr);
    track.chunks[i].lines = retext(ch, text);
    save.disabled = true;
    await saveCaptions(track, `Caption ${i + 1} saved`);
  };
  box.replaceChildren(
    h('div', { class: 'i-meta', style: { marginTop: '6px' } }, `CHUNK ${i + 1} · ${ch.start.toFixed(2)}s → ${ch.end.toFixed(2)}s · new line = new caption line`),
    ta, h('div', { class: 'btn-row' }, save, revert, resetBtn));
}

// Rebuild a chunk's words from edited text, keeping word timing when the word count is unchanged,
// otherwise spreading the words across the chunk weighted by length.
function retext(ch, text) {
  const lines = text.split('\n').map(l => l.trim().split(/\s+/).filter(Boolean)).filter(l => l.length);
  const oldWords = ch.lines.flat();
  const newCount = lines.reduce((a, l) => a + l.length, 0);
  if (newCount === oldWords.length) {
    let k = 0;
    return lines.map(l => l.map(t => ({ ...oldWords[k++], text: t })));
  }
  const total = lines.flat().reduce((a, w) => a + w.length + 1, 0);
  const dur = ch.end - ch.start;
  let t = ch.start;
  return lines.map(l => l.map(w => {
    const d = dur * (w.length + 1) / total;
    const cw = { text: w, start: round(t, 3), end: round(Math.min(ch.end, t + d), 3) };
    t += d;
    return cw;
  }));
}
