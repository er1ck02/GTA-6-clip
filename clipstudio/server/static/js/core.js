// Shared state, API helpers and small utilities.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'html') el.innerHTML = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// ------------------------------------------------------------------ API
export class ApiError extends Error {
  constructor(msg, status) { super(msg); this.status = status; }
}

export async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError(`server unreachable (${e.message})`, 0);
  }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!res.ok) {
    let msg = res.statusText;
    if (data && typeof data === 'object') {
      if (data.error) msg = data.error;
      else if (typeof data.detail === 'string') msg = data.detail;
      else if (Array.isArray(data.detail)) msg = data.detail.map(d => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
    } else if (typeof data === 'string' && data) msg = data.slice(0, 200);
    throw new ApiError(msg, res.status);
  }
  return data;
}

export const P = (pid, rest = '') => `/api/projects/${encodeURIComponent(pid)}${rest}`;
export const C = (pid, cid, rest = '') => P(pid, `/clips/${encodeURIComponent(cid)}${rest}`);

// ------------------------------------------------------------------ store
export const S = {
  system: null,
  config: null,
  presets: [],
  projects: [],
  pid: null,
  project: null,          // GET /api/projects/{pid}
  candidates: [],
  weights: {},
  cid: null,
  preview: null,          // GET .../preview  {candidate, plan, captions, caption_style, edit}
  selChunk: null,         // index into preview.captions.chunks
  qc: null,
  render: null,           // {cid, job, stages:{}, status, error}
  renderedVersion: Date.now(),
  progress: {},           // stage -> 0..1
  liveStages: {},         // stage -> status (from SSE, current project)
  loop: true,
  dirty: true,            // overlay / output need redraw
};

const subs = new Map();
export function on(ev, fn) {
  if (!subs.has(ev)) subs.set(ev, new Set());
  subs.get(ev).add(fn);
}
export function emit(ev, data) {
  S.dirty = true;
  for (const fn of subs.get(ev) || []) {
    try { fn(data); } catch (e) { console.error(`[${ev}]`, e); }
  }
}

export const cand = () => S.preview?.candidate || S.candidates.find(c => c.id === S.cid) || null;
export const media = () => S.project?.media || null;

// ------------------------------------------------------------------ format
export function fmtT(t, dp = 1) {
  if (t == null || !isFinite(t)) return '--';
  const neg = t < 0; t = Math.abs(t);
  const m = Math.floor(t / 60), s = t - m * 60;
  return `${neg ? '-' : ''}${m}:${s.toFixed(dp).padStart(dp ? 3 + dp : 2, '0')}`;
}
export function timecode(t, fps = 30) {
  t = Math.max(0, t || 0);
  const f = Math.floor((t % 1) * fps + 1e-6);
  const s = Math.floor(t) % 60, m = Math.floor(t / 60) % 60, hh = Math.floor(t / 3600);
  return [hh, m, s, f].map(v => String(v).padStart(2, '0')).join(':');
}
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const round = (v, dp = 2) => Math.round(v * 10 ** dp) / 10 ** dp;
export const pretty = k => k.replace(/_/g, ' ');

// ------------------------------------------------------------------ toast
export function toast(msg, kind = 'ok', ms = 3800) {
  const el = h('div', { class: `toast ${kind === 'err' ? 'err' : ''}` }, msg);
  const box = document.getElementById('toasts');
  box.append(el);
  while (box.children.length > 3) box.firstElementChild.remove();
  setTimeout(() => el.remove(), ms);
}

// caption helpers
export const chunkText = ch => ch.lines.map(l => l.map(w => w.text).join(' ')).join('\n');
export function activeChunk(track, tRel) {
  if (!track) return null;
  return track.chunks.find(ch => tRel >= ch.start && tRel < ch.end) || null;
}

// the layout the user asked for (the plan's mode can differ, e.g. facecam_split without a region falls back)
export const layoutOf = () => S.preview?.edit?.layout || S.config?.config?.reframing?.mode || S.preview?.plan?.mode;
