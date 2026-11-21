// 9:16 output: live canvas preview through the real crop plan + captions, render/export, QC.
import { S, $, h, on, cand, C, activeChunk, toast, clamp, kfAt } from './core.js';
import { video, startRender } from './actions.js';

let cv, ctx, tab = 'live', fitStage = () => {}, hasFrame = false;
const STAGES = ['plan', 'captions', 'render', 'qc'];

export function initOutput() {
  cv = $('#outCanvas'); ctx = cv.getContext('2d');
  const stage = $('#outStage');
  new ResizeObserver(() => {
    const r = stage.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(r.width * dpr); cv.height = Math.round(r.height * dpr);
    S.dirty = true;
  }).observe(stage);
  // size the 9:16 stage so stage + title + export + stage chips fit the visible panel
  const body = $('#outBody');
  const fit = () => {
    const fixed = ['#outTitle', '#btnRender', '#renderStages'].reduce((a, s) => a + ($(s).offsetHeight || 0), 0);
    const avail = body.clientHeight - fixed - 7 * 3 - 20;
    const hh = Math.round(clamp(avail, 220, 560));
    const ww = Math.round(Math.min(hh * 9 / 16, body.clientWidth - 20));
    stage.style.width = `${ww}px`; stage.style.height = `${Math.round(ww * 16 / 9)}px`;
  };
  new ResizeObserver(fit).observe(body);
  fitStage = fit;

  $('#tabLive').onclick = () => setTab('live');
  $('#tabRendered').onclick = () => setTab('rendered');
  $('#btnRender').onclick = startRender;
  $('#btnGuides').onclick = e => { e.currentTarget.classList.toggle('on'); S.dirty = true; };

  on('select', () => { setTab('live'); renderPanel(); });
  on('preview', renderPanel);
  on('qc', renderPanel);
  on('render', renderPanel);
  on('project', renderPanel);
}

function setTab(t) {
  tab = t;
  $('#tabLive').classList.toggle('on', t === 'live');
  $('#tabRendered').classList.toggle('on', t === 'rendered');
  cv.hidden = t !== 'live';
  const rv = $('#renderVideo');
  rv.hidden = t !== 'rendered';
  if (t === 'rendered') {
    video().pause();
    const want = C(S.pid, S.cid, `/video?v=${S.renderedVersion}`);
    if (rv.getAttribute('src') !== want) rv.src = want;
  } else rv.pause();
}

function hasOutput() {
  return !!(S.cid && S.project?.clips?.[S.cid]?.output);
}

function renderPanel() {
  const c = cand(), pv = S.preview;
  const out = $('#outTitle');
  if (!c || !pv) {
    out.textContent = '';
    $('#btnRender').disabled = true;
    $('#renderStages').replaceChildren();
    $('#downloads').replaceChildren();
    $('#qcBox').replaceChildren();
    $('#tabRendered').disabled = true;
    return;
  }
  const modeLabel = { gameplay: 'GAMEPLAY CROP', fit_blur: 'FIT + BLUR FILL', facecam_split: 'FACECAM + GAMEPLAY', facecam_pip: 'GAMEPLAY + FACECAM PIP' }[pv.plan.mode] || pv.plan.mode;
  out.replaceChildren(`SHORT_${String(c.rank).padStart(2, '0')}`,
    h('small', {}, `${pv.plan.out_width}×${pv.plan.out_height} · ${modeLabel} · ${pv.captions ? 'CAPS ' + pv.captions.preset.toUpperCase() : 'NO CAPS'}`));

  // render progress
  const r = S.render && S.render.cid === S.cid ? S.render : null;
  const busy = r && (r.status === 'queued' || r.status === 'running');
  const btn = $('#btnRender');
  btn.disabled = busy;
  btn.textContent = busy ? (r.status === 'queued' ? 'QUEUED…' : 'RENDERING…') : (hasOutput() ? 'RE-RENDER / H.264' : 'EXPORT / H.264');
  const clipStages = S.project?.clips?.[S.cid]?.stages || {};
  $('#renderStages').replaceChildren(...STAGES.map(s => {
    const st = (r && r.stages[s]) || (busy ? 'queued' : clipStages[s] || '');
    return h('span', { class: `rs ${st}`, title: st ? `${s}: ${st}` : `${s}: not run` }, s);
  }));
  const err = $('#renderErr');
  err.hidden = !(r && r.error);
  if (r?.error) err.textContent = `Render failed: ${r.error}`;

  $('#tabRendered').disabled = !hasOutput();
  if (!hasOutput() && tab === 'rendered') setTab('live');

  const dl = $('#downloads');
  if (hasOutput()) {
    dl.replaceChildren(
      h('a', { class: 'btn btn-ghost btn-sm', href: C(S.pid, S.cid, `/video?v=${S.renderedVersion}`), download: `${S.pid}_${S.cid}.mp4` }, '↓ MP4'),
      h('a', { class: 'btn btn-ghost btn-sm', href: C(S.pid, S.cid, '/captions.srt'), download: 'captions.srt' }, '↓ SRT'));
  } else dl.replaceChildren(h('div', { class: 'plat-note' }, 'Not rendered yet: export to get the MP4, SRT and QC report.'));

  renderQC();
  fitStage();
}

function renderQC() {
  const box = $('#qcBox');
  const qc = S.qc;
  if (!qc) { box.replaceChildren(); return; }
  const counts = {};
  for (const ch of qc.checks) counts[ch.status] = (counts[ch.status] || 0) + 1;
  box.replaceChildren(
    h('div', { class: 'qc-head' },
      h('span', {}, 'QC REPORT'),
      h('span', { class: qc.passed ? 'passed' : 'failed' }, `${qc.passed ? 'PASSED' : 'FAILED'} · ${Object.entries(counts).map(([k, v]) => `${v} ${k}`).join(' · ')}`)),
    h('table', {},
      h('thead', {}, h('tr', {}, h('th', {}, 'CHECK'), h('th', {}, ''), h('th', {}, 'DETAIL'))),
      h('tbody', {}, qc.checks.map(ch => h('tr', {},
        h('td', { title: ch.group }, ch.name),
        h('td', {}, h('span', { class: `pill ${ch.status}` }, ch.status)),
        h('td', { class: 'd' }, ch.detail))))));
}

// called by SSE handler
export function onRenderEvent(ev) {
  const r = S.render;
  if (ev.kind === 'stage' && ev.clip && STAGES.includes(ev.stage)) {
    if (r && r.cid === ev.clip) { r.stages[ev.stage] = ev.status; r.status = 'running'; renderPanel(); }
  }
}
export async function onRenderJobDone(ev, refresh) {
  const r = S.render;
  if (!r || r.job !== ev.job) return false;
  r.status = ev.status;
  r.error = ev.error || null;
  if (ev.status === 'done') {
    S.renderedVersion = Date.now();
    await refresh();
    if (S.cid === r.cid) {
      toast('Render finished · QC loaded');
      setTab('rendered');
    }
  } else if (ev.status === 'failed') {
    toast(`Render failed: ${ev.error || 'unknown error'}`, 'err', 8000);
  }
  renderPanel();
  return true;
}

// ------------------------------------------------------------------ live 9:16 frame
export function drawOutput() {
  if (tab !== 'live') return;
  const W = cv.width, H = cv.height;
  const pv = S.preview, v = video(), c = cand();
  // while a seek is decoding keep the last good frame instead of flashing a placeholder
  if (pv && c && v.readyState < 2 && hasFrame && v.getAttribute('src')) { S.dirty = true; return; }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = '#0f0e0d'; ctx.fillRect(0, 0, W, H);
  if (!pv || !c) { msg('Select a moment'); hasFrame = false; return; }
  const plan = pv.plan, kf = kfAt(plan, v.currentTime - c.start);
  const s = W / plan.out_width;
  const ready = v.readyState >= 2;
  if (!ready) msg('Loading frame…');
  else {
    hasFrame = true;
    // the browser may be playing a lower-resolution proxy (HEVC sources): map source px -> video px
    const vk = v.videoWidth ? v.videoWidth / plan.source_width : 1;
    for (const p of plan.panels) {
      const d = p.dest;
      if (p.name === 'background') {
        // cover-fill of the full frame, blurred + darkened
        const sw = plan.source_width, sh = plan.source_height;
        const k = Math.max(d.w / sw, d.h / sh);
        const dw = sw * k, dh = sh * k;
        ctx.save();
        ctx.beginPath(); ctx.rect(d.x * s, d.y * s, d.w * s, d.h * s); ctx.clip();
        ctx.filter = `blur(${Math.max(2, Math.round(24 * s))}px) brightness(0.55)`;
        ctx.drawImage(v, 0, 0, sw * vk, sh * vk, (d.x + (d.w - dw) / 2) * s, (d.y + (d.h - dh) / 2) * s, dw * s, dh * s);
        ctx.restore();
        ctx.filter = 'none';
      } else {
        const src = p.name === 'facecam' ? kf.facecam : kf.gameplay;
        if (!src) continue;
        if (p.name === 'facecam' && plan.mode === 'facecam_pip') {
          const b = Math.max(2, Math.round(plan.out_width / 180)) * s;
          ctx.fillStyle = '#F2E8D5'; ctx.fillRect(d.x * s - b, d.y * s - b, d.w * s + 2 * b, d.h * s + 2 * b);
        }
        ctx.drawImage(v, src.x * vk, src.y * vk, src.w * vk, src.h * vk, d.x * s, d.y * s, d.w * s, d.h * s);
      }
    }
    if (plan.mode === 'facecam_split') {
      const fp = plan.panels.find(p => p.name === 'facecam');
      if (fp) { ctx.fillStyle = 'rgba(0,0,0,.6)'; ctx.fillRect(0, (fp.dest.y + fp.dest.h) * s - 1, W, 2); }
    }
  }
  if ($('#btnGuides').classList.contains('on')) guides(plan, s);
  if (pv.captions) drawCaptions(pv.captions, pv.caption_style, v.currentTime - c.start, s);
}

function msg(t) {
  ctx.fillStyle = '#8f877a'; ctx.font = `${Math.round(11 * (window.devicePixelRatio || 1))}px "JetBrains Mono", monospace`;
  ctx.textAlign = 'center'; ctx.fillText(t, cv.width / 2, cv.height / 2); ctx.textAlign = 'left';
}

function guides(plan, s) {
  const [top, bottom] = plan.caption_band;
  ctx.save();
  ctx.setLineDash([6, 5]); ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(47,224,210,.85)';
  for (const y of [top, bottom]) { ctx.beginPath(); ctx.moveTo(0, y * s); ctx.lineTo(cv.width, y * s); ctx.stroke(); }
  ctx.fillStyle = 'rgba(255,92,122,.18)'; ctx.strokeStyle = 'rgba(255,92,122,.8)';
  for (const r of plan.protected_regions) { ctx.fillRect(r.x * s, r.y * s, r.w * s, r.h * s); ctx.strokeRect(r.x * s, r.y * s, r.w * s, r.h * s); }
  ctx.restore();
}

function drawCaptions(track, style, t, s) {
  const ch = activeChunk(track, t);
  if (!ch) return;
  const L = track.layout;
  const outH = cv.height / s;
  const fs = L.font_size_px * s;
  const weight = style.bold ? 800 : 500;
  const fam = `"${L.font_family}", Inter, Montserrat, Arial, sans-serif`;
  ctx.save();
  ctx.font = `${weight} ${fs}px ${fam}`;
  ctx.textBaseline = 'alphabetic';
  ctx.lineJoin = 'round';
  const outline = style.outline * outH * s;
  const shadow = style.shadow * outH * s;
  const space = ctx.measureText(' ').width;
  // pop-in: brief scale-up at the chunk start
  let pop = 1;
  if (style.pop_in) { const a = Math.min(1, (t - ch.start) / 0.12); pop = 0.88 + 0.12 * a; }
  const n = ch.lines.length;
  const descent = fs * 0.22;
  ch.lines.forEach((line, li) => {
    const words = line.map(w => (style.uppercase ? w.text.toUpperCase() : w.text));
    const widths = words.map(w => ctx.measureText(w).width);
    const total = widths.reduce((a, b) => a + b, 0) + space * (words.length - 1);
    const baseY = (L.baseline_y * s) - descent - (n - 1 - li) * L.line_height_px * s;
    let x = L.center_x * s - total / 2;
    words.forEach((w, wi) => {
      const cw = line[wi];
      const active = style.highlight_active_word && t >= cw.start && t < cw.end;
      const sc = (active ? style.active_word_scale : 1) * pop;
      const cx = x + widths[wi] / 2;
      ctx.save();
      ctx.translate(cx, baseY - fs * 0.35);
      ctx.scale(sc, sc);
      ctx.translate(-widths[wi] / 2, fs * 0.35);
      if (shadow > 0) { ctx.fillStyle = 'rgba(0,0,0,.65)'; ctx.fillText(w, shadow, shadow); }
      if (outline > 0) { ctx.strokeStyle = style.outline_color; ctx.lineWidth = outline * 2; ctx.strokeText(w, 0, 0); }
      ctx.fillStyle = active ? style.highlight_color : style.primary_color;
      ctx.fillText(w, 0, 0);
      ctx.restore();
      x += widths[wi] + space;
    });
  });
  ctx.restore();
}
