// LIVE / STREAM WATCH: the rolling buffer, chat, trigger signals and the moment queue, polled from /api/live.
// With FOLLOW on, each moment the editor picks up is opened in the workstation so its analysis, captions,
// render and QC play out in the normal panels and on the clipping desk.
import { S, $, h, api, toast } from './core.js';
import { loadProjects, loadProject, selectClip } from './actions.js';

let snap = null;
let selected = null;
let srcRows = [];
let srcAt = 0;   // which stream the detail panel shows (null = the newest running one)
let follow = true;
let timer = null;
let followed = new Set();
let lastKeys = {};
const changed = (name, key) => (lastKeys[name] === key ? false : ((lastKeys[name] = key), true));

export function initLive() {
  try { follow = localStorage.getItem('clipstudio.follow') !== '0'; } catch {}
  $('#liveFollow').classList.toggle('on', follow);
  $('#liveFollow').onclick = () => {
    follow = !follow;
    $('#liveFollow').classList.toggle('on', follow);
    try { localStorage.setItem('clipstudio.follow', follow ? '1' : '0'); } catch {}
  };
  const inp = $('#srcInput');
  const count = () => {
    const n = splitLinks(inp.value).length;
    $('#srcCount').textContent = n; $('#srcPlural').textContent = n === 1 ? '' : 's';
    inp.rows = Math.min(6, Math.max(1, inp.value.split('\n').length));
  };
  inp.oninput = count;
  inp.onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $('#srcForm').requestSubmit(); } };
  $('#srcForm').onsubmit = async e => {
    e.preventDefault();
    if (!splitLinks(inp.value).length) return toast('Paste a link first', 'err');
    const btn = $('#srcAdd');
    btn.disabled = true; btn.textContent = 'ADDING…';
    try {
      const r = await api('/api/sources', { method: 'POST', body: { text: inp.value } });
      const ok = r.added.filter(x => x.ok), bad = r.added.filter(x => !x.ok);
      const nl = ok.filter(x => x.kind === 'live').length, nv = ok.length - nl;
      toast([nl && `watching ${nl} live`, nv && `analysing ${nv} VOD${nv === 1 ? '' : 's'}`].filter(Boolean).join(' · ') || 'nothing added',
        bad.length ? 'err' : undefined);
      for (const x of bad) toast(`${x.input}: ${x.error}`, 'err', 9000);
      inp.value = bad.map(x => x.input).join('\n');  // keep only what failed, to fix and retry
      count();
      const firstLive = ok.find(x => x.kind === 'live');
      if (firstLive) selected = firstLive.id;
      srcRows = r.sources.rows; lastKeys = {};
      if (ok.some(x => x.kind !== 'live')) loadProjects().catch(() => {});
      poll();
    } catch (err) { toast(`Add: ${err.message}`, 'err', 8000); }
    btn.disabled = false; btn.textContent = 'ADD';
  };
  $('#liveStopAll').onclick = async () => {
    try { snap = await api('/api/live/stop', { method: 'POST', body: {} }); render(); toast('Stopping every stream: pending moments are still saved and edited'); }
    catch (err) { toast(err.message, 'err'); }
  };
  $('#liveAll').onclick = async () => {
    try {
      const r = await api('/api/live/start-all', { method: 'POST', body: {} });
      const bad = Object.keys(r.failed || {}).length;
      toast(`Watching ${r.started.length} stream${r.started.length === 1 ? '' : 's'}${bad ? ` · ${bad} could not start` : ''}`, bad ? 'err' : undefined, bad ? 8000 : undefined);
      lastKeys = {}; poll();
    } catch (err) { toast(`Watch all: ${err.message}`, 'err', 8000); }
  };
  poll();
}

const splitLinks = t => [...new Set(t.split(/\n/).flatMap(l => {
  l = l.trim(); if (!l) return [];
  const urls = l.match(/https?:\/\/\S+/g);
  return urls ? urls.map(u => u.replace(/[,;)]+$/, '')) : [l];
}))];

async function poll() {
  clearTimeout(timer);
  if (Date.now() - srcAt > 2500) {
    srcAt = Date.now();
    try { srcRows = (await api('/api/sources')).rows; } catch {}
  }
  try { snap = await api(selected ? `/api/live?id=${encodeURIComponent(selected)}` : '/api/live'); }
  catch { snap = snap || { active: false }; }
  if (snap?.id && selected && snap.id !== selected) selected = snap.id;  // the selected stream was removed
  render();
  // slower when the tab is hidden; the editor keeps working on the server either way
  timer = setTimeout(poll, document.hidden ? 5000 : snap?.running ? 1500 : 5000);
}

// called for every pipeline event (from app.js)
export function liveEvent(ev) {
  if (ev.kind !== 'live_moment' || !follow) return;
  // with several streams, only the one on screen takes over the workstation
  if (snap?.sessions?.length > 1 && ev.channel !== snap.channel) return;
  if (ev.status === 'processing' && ev.project && !followed.has(ev.moment)) {
    followed.add(ev.moment);
    loadProjects().then(() => loadProject(ev.project)).catch(() => {});
    toast(`Editing ${ev.moment} live`);
  } else if (ev.status === 'done' && ev.project) {
    loadProjects().then(async () => {
      await loadProject(ev.project);
      const c = S.candidates?.[0];
      if (c) await selectClip(c.id);
      document.getElementById('tabRendered')?.click();
    }).catch(() => {});
  }
}

const fmt = t => {
  t = Math.max(0, Math.floor(t || 0));
  const hh = Math.floor(t / 3600), mm = Math.floor(t / 60) % 60, ss = t % 60;
  return `${hh ? hh + ':' : ''}${String(mm).padStart(hh ? 2 : 1, '0')}:${String(ss).padStart(2, '0')}`;
};

function render() {
  const panel = $('#livePanel');
  const active = !!(snap && snap.active);
  const running = !!(snap && snap.running);
  panel.classList.toggle('is-active', active);
  panel.classList.toggle('is-running', running);
  const sessions = snap?.sessions || [];
  $('#liveStopAll').disabled = !sessions.some(x => x.running);
  if (changed('rows', JSON.stringify(srcRows.map(x => [x.kind, x.id, x.running, x.status, x.error, Math.floor((x.head || 0) / 6), x.moments, x.done, x.editing, x.queued, x.clips])) + snap?.id)) renderRows();
  const badge = $('#liveBadge');
  const nrun = sessions.filter(x => x.running).length;
  const nvod = srcRows.filter(x => x.kind === 'vod' && ['queued', 'running'].includes(x.status)).length;
  badge.textContent = nrun ? `● ${nrun} LIVE` : nvod ? `${nvod} ANALYSING` : 'IDLE';
  badge.className = `badge ${nrun ? 'badge-rec' : nvod ? 'badge-on' : 'badge-dim'}`;
  if (!active) {
    $('#liveMeta').textContent = snap?.error ? `last session failed: ${snap.error}` : 'paste links below: live streams are watched, VODs are analysed';
    return;
  }
  $('#liveMeta').textContent = [
    snap.channel ? `@${snap.channel}` : null,
    snap.allowed ? 'ON UPLOAD ALLOWLIST' : 'REVIEW ONLY',
    `chat ${snap.chat_source}`,
    running ? `live ${fmt(snap.head)}` : `stopped: ${snap.error || snap.stopped || ''}`,
  ].filter(Boolean).join(' · ');
  // redraw only the parts whose data changed since the last poll
  if (changed('feed', `${snap.segments.at(-1)?.i}|${snap.running}`)) renderFeed();
  if (changed('buf', `${snap.segments.length}|${snap.segments.at(-1)?.i}|${snap.discarded}|${snap.segments.filter(x => x.pinned).length}`)) renderBuffer();
  if (changed('sig', `${snap.signals.at(-1)?.t}|${snap.chat_count}|${(snap.triggers || []).length}|${$('#liveSignals').clientWidth}`)) drawSignals();
  if (changed('chat', `${snap.chat_count}`)) renderChat();
  if (changed('mom', JSON.stringify((snap.moments || []).map(m => [m.id, m.status, m.qc_passed, m.upload])))) renderMoments();
}

const COLS = ['', 'SOURCE', 'TYPE', 'STATUS', 'TIME', 'MOMENTS', 'READY', 'QUEUE', ''];

function rowStatus(x) {
  if (x.kind === 'live') {
    if (x.error) return ['err', 'failed'];
    if (x.running) return x.recording ? ['rec', 'recording'] : ['on', 'finishing edits'];
    return ['off', x.stopped ? `stopped · ${x.stopped}` : 'stopped'];
  }
  return { queued: ['on', 'queued'], running: ['rec', 'analysing'], done: ['ok', 'analysed'], failed: ['err', 'failed'] }[x.status] || ['off', x.status];
}

async function rowAction(path, body, msg) {
  try { await api(path, { method: path.startsWith('DELETE ') ? 'DELETE' : 'POST', body }); if (msg) toast(msg); srcAt = 0; lastKeys = {}; poll(); }
  catch (err) { toast(err.message, 'err', 7000); }
}

function renderRows() {
  const box = $('#srcTable');
  if (!srcRows.length) {
    box.replaceChildren(h('div', { class: 'src-empty' },
      h('b', {}, 'Nothing yet.'), ' Paste stream or VOD links above. Every live stream is watched at the same time; ',
      'moments queue for editing so the GPU never gets overloaded.'));
    return;
  }
  const head = h('div', { class: 'src-row src-head', role: 'row' }, ...COLS.map(c => h('span', { role: 'columnheader' }, c)));
  box.replaceChildren(head, ...srcRows.map(x => {
    const [cls, label] = rowStatus(x);
    const isLive = x.kind === 'live';
    const name = isLive ? `@${x.channel || x.id}` : (x.source || x.id).split(/[\\/]/).filter(Boolean).pop();
    const queue = isLive ? (x.editing ? `editing${x.queued ? ` +${x.queued}` : ''}` : x.queued ? `${x.queued} waiting` : '—') : '—';
    const actions = isLive
      ? [x.running
          ? h('button', { class: 'btn btn-ghost btn-xs', title: 'Stop watching (saved moments are still edited)', onclick: e => { e.stopPropagation(); rowAction('/api/live/stop', { id: x.id }, `Stopping @${x.channel || x.id}`); } }, 'STOP')
          : h('button', { class: 'btn btn-ghost btn-xs', title: 'Watch this stream again', onclick: e => { e.stopPropagation(); rowAction('/api/live/restart', { id: x.id }, `Watching @${x.channel || x.id} again`); } }, 'START'),
         !x.running ? h('button', { class: 'ibtn btn-xs', title: 'Remove from the list (files stay on disk)', onclick: e => { e.stopPropagation(); rowAction(`/api/live/${encodeURIComponent(x.id)}`, undefined); } }, '✕') : null]
      : [h('button', { class: 'btn btn-ghost btn-xs', title: 'Open in the workstation', onclick: e => { e.stopPropagation(); openProject(x.project); } }, 'OPEN')];
    return h('div', {
      class: `src-row ${isLive ? 'is-live' : 'is-vod'} st-${cls}${isLive && snap?.id === x.id ? ' sel' : ''}`, role: 'row', tabindex: 0,
      title: `${x.source || ''}${x.error ? `\n${x.error}` : ''}`,
      onclick: () => isLive ? (selected = x.id, lastKeys = {}, poll()) : openProject(x.project),
    },
      h('span', { class: 'src-thumb' }, isLive && x.head > 0 && !document.hidden
        ? h('img', { src: `/api/live/frame?id=${encodeURIComponent(x.id)}&t=${Math.floor(x.head / 6)}`, alt: '' })
        : h('i', {}, isLive ? '◉' : '▶')),
      h('span', { class: 'src-name' }, h('b', {}, name), h('small', {}, x.source || '')),
      h('span', {}, h('span', { class: `pill ${isLive ? 'pill-live' : 'pill-vod'}` }, isLive ? 'LIVE' : 'VOD')),
      h('span', { class: 'src-status' }, h('i', { class: 'dot' }), label),
      h('span', { class: 'mono' }, isLive ? fmt(x.head) : x.duration ? fmt(x.duration) : '—'),
      h('span', { class: 'num' }, isLive ? String(x.moments) : x.clips == null ? '—' : String(x.clips)),
      h('span', { class: 'num ok' }, isLive ? String(x.done || 0) : '—'),
      h('span', { class: 'mono' }, queue),
      h('span', { class: 'src-act' }, ...actions));
  }));
}

async function openProject(pid) {
  if (!pid) return;
  await loadProjects();
  await loadProject(pid);
}

function renderFeed() {
  const img = $('#liveFrame');
  if (snap.segments.length && !document.hidden) img.src = `/api/live/frame?id=${encodeURIComponent(snap.id)}&i=${snap.segments.at(-1).i}`;
  $('#liveFeedMsg').hidden = snap.segments.length > 0;
  $('#liveFeedMsg').textContent = snap.running ? 'connecting to the stream…' : 'no video recorded';
  $('#liveHead').textContent = fmt(snap.head);
}

function renderBuffer() {
  const segs = snap.segments;
  const span = snap.buffer_seconds + snap.pre + snap.post;
  const end = snap.head, start = end - span;
  const bar = $('#liveBuffer');
  bar.replaceChildren(...segs.filter(s => s.end > start).map(s => h('span', {
    class: `seg${s.pinned ? ' pinned' : ''}${s.end < end - snap.buffer_seconds ? ' old' : ''}`,
    style: { left: `${((s.start - start) / span) * 100}%`, width: `${((s.end - s.start) / span) * 100}%` },
    title: `segment ${s.i}: ${fmt(s.start)}-${fmt(s.end)}${s.pinned ? ' · kept for a moment' : ''}`,
  })), h('span', { class: 'buf-edge', style: { left: `${((end - snap.buffer_seconds - start) / span) * 100}%` },
    title: `older than ${snap.buffer_seconds}s is deleted` }));
  $('#liveBufMeta').textContent =
    `${segs.length} segments · ${snap.disk_mb} MB on disk · ${snap.discarded} discarded${snap.reconnects ? ` · ${snap.reconnects} reconnects` : ''}`;
}

function drawSignals() {
  const cv = $('#liveSignals');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = cv.clientHeight;
  if (!W || !H) return;
  cv.width = W * dpr; cv.height = H * dpr;
  const g = cv.getContext('2d');
  g.scale(dpr, dpr);
  g.clearRect(0, 0, W, H);
  const span = 180, end = Math.max(snap.head, span), start = end - span;
  const x = t => ((t - start) / span) * W;
  const css = getComputedStyle(document.documentElement);
  const teal = css.getPropertyValue('--teal').trim(), coral = css.getPropertyValue('--coral').trim();
  const amber = css.getPropertyValue('--amber').trim(), dim = css.getPropertyValue('--muted').trim();
  // chat bars (messages per second), hype in coral on top
  const rate = snap.chat_rate || {}, hype = snap.hype || {}, cmd = snap.commands || {};
  const maxRate = Math.max(4, ...Object.values(rate));
  for (const [k, v] of Object.entries(rate)) {
    const bh = (v / maxRate) * (H * 0.45);
    g.fillStyle = 'rgba(11,154,148,.35)';
    g.fillRect(x(+k), H - bh, Math.max(1, W / span - 0.5), bh);
    const hv = (hype[k] || 0) + (cmd[k] || 0);
    if (hv) { g.fillStyle = coral; g.fillRect(x(+k), H - (hv / maxRate) * (H * 0.45), Math.max(1, W / span - 0.5), (hv / maxRate) * (H * 0.45)); }
  }
  // audio level line (dBFS, -60..0)
  const aud = Object.entries(snap.audio || {}).map(([k, v]) => [+k, v]).sort((a, b) => a[0] - b[0]);
  g.strokeStyle = dim; g.lineWidth = 1; g.beginPath();
  aud.forEach(([t, v], i) => { const y = H - ((Math.max(-60, v) + 60) / 60) * H; i ? g.lineTo(x(t), y) : g.moveTo(x(t), y); });
  g.stroke();
  // trigger score and threshold (score axis 0..2x threshold)
  const top = Math.max(2 * snap.threshold, ...snap.signals.map(s => s.score));
  const ys = v => H - (v / top) * H;
  g.setLineDash([4, 4]); g.strokeStyle = amber; g.beginPath(); g.moveTo(0, ys(snap.threshold)); g.lineTo(W, ys(snap.threshold)); g.stroke(); g.setLineDash([]);
  g.strokeStyle = teal; g.lineWidth = 2; g.beginPath();
  snap.signals.forEach((s, i) => (i ? g.lineTo(x(s.t), ys(s.score)) : g.moveTo(x(s.t), ys(s.score))));
  g.stroke();
  for (const tr of snap.triggers || []) {
    if (tr.t < start) continue;
    g.fillStyle = coral; g.fillRect(x(tr.t) - 1, 0, 2, H);
    g.font = '10px ' + css.getPropertyValue('--mono'); g.fillText(tr.id.split('_')[0], x(tr.t) + 3, 11);
  }
  const last = snap.signals.at(-1);
  $('#liveScore').textContent = last
    ? `${last.score.toFixed(2)} / ${snap.threshold}  (audio ${last.audio.toFixed(1)} · chat ${last.chat.toFixed(1)} · hype ${last.hype.toFixed(1)} · clip ${last.command.toFixed(1)})`
    : 'warming up: learning what normal looks like…';
}

function renderChat() {
  const list = $('#liveChat');
  const msgs = snap.recent_chat || [];
  const kw = /\b(clip|clip it|clipit|kekw|lul|lmao|pog\w*|no way|w|holy|wtf|insane|omegalul)\b/i;
  list.replaceChildren(...msgs.slice(-18).reverse().map(m => h('li', { class: kw.test(m.text) ? 'hype' : '' },
    h('span', { class: 'u' }, m.user || '·'), h('span', { class: 'm' }, m.text))));
  $('#liveChatMeta').textContent = snap.chat_source === 'none' ? 'no chat · audio only' : `${snap.chat_count} messages`;
}

const STEPS = ['capture', 'edit', 'render', 'qc', 'upload'];
function stepState(m) {
  const st = {};
  st.capture = m.status === 'pending' ? 'running' : m.status === 'failed' && !m.path ? 'failed' : 'done';
  if (['pending'].includes(m.status)) return st;
  st.edit = ['saved', 'queued'].includes(m.status) ? 'queued' : m.status === 'processing' ? 'running' : m.status === 'failed' ? 'failed' : 'done';
  if (m.status !== 'done') return st;
  st.render = 'done';
  st.qc = m.qc_passed ? 'done' : 'failed';
  st.upload = m.upload === 'allowed' ? 'done' : 'review';
  return st;
}

function renderMoments() {
  const list = $('#liveMoments');
  const ms = (snap.moments || []).slice().reverse();
  $('#liveMomentsMeta').textContent = `${ms.length} moment${ms.length === 1 ? '' : 's'}`;
  if (!ms.length) {
    list.replaceChildren(h('div', { class: 'empty' }, snap.running ? 'Watching… a moment is saved when chat or audio pops off.' : 'No moments.'));
    return;
  }
  list.replaceChildren(...ms.map(m => {
    const st = stepState(m);
    return h('div', { class: `lmoment m-${m.status}` },
      h('div', { class: 'mo-head' },
        h('b', {}, m.id.split('_')[0].toUpperCase()),
        h('span', { class: 'mo-t' }, `@ ${fmt(m.trigger.t)} · ${m.trigger.score.toFixed(2)}`),
        h('span', { class: 'mo-r' }, (m.trigger.reasons || []).join(' · ') || 'audio')),
      h('div', { class: 'mo-steps' }, ...STEPS.map(s => h('span', { class: `step ${st[s] || ''}`, title: `${s}: ${st[s] || 'waiting'}` }, s))),
      m.error ? h('div', { class: 'mo-err' }, m.error) : null,
      m.status === 'done' ? h('div', { class: 'mo-foot' },
        h('span', { class: m.upload === 'allowed' ? 'ok' : 'rev', title: m.upload_note }, m.upload === 'allowed' ? 'READY TO POST' : 'NEEDS REVIEW'),
        h('button', { class: 'btn btn-ghost btn-sm', onclick: () => openMoment(m) }, 'OPEN')) : null);
  }));
}

async function openMoment(m) {
  if (!m.project) return;
  await loadProjects();
  await loadProject(m.project);
  const c = S.candidates?.[0];
  if (c) await selectClip(c.id);
}
