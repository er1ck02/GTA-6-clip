// LIVE / STREAM WATCH: the rolling buffer, chat, trigger signals and the moment queue, polled from /api/live.
// With FOLLOW on, each moment the editor picks up is opened in the workstation so its analysis, captions,
// render and QC play out in the normal panels and on the clipping desk.
import { S, $, h, api, toast } from './core.js';
import { loadProjects, loadProject, selectClip } from './actions.js';

let snap = null;
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
  $('#liveForm').onsubmit = async e => {
    e.preventDefault();
    const v = $('#liveUrl').value.trim();
    if (!v) return;
    const isUrl = /^https?:\/\//i.test(v);
    const btn = $('#liveStart');
    btn.disabled = true;
    try {
      lastKeys = {};
      snap = await api('/api/live/start', { method: 'POST', body: {
        url: isUrl ? v : null, file: isUrl ? null : v, channel: $('#liveChannel').value.trim() || null } });
      toast(`Watching ${snap.channel || v}`);
      followed = new Set();
      render();
      poll();
    } catch (err) { toast(`Live mode: ${err.message}`, 'err', 8000); }
    btn.disabled = false;
  };
  $('#liveStop').onclick = async () => {
    try { snap = await api('/api/live/stop', { method: 'POST' }); render(); toast('Stopping: pending moments are still saved and edited'); }
    catch (err) { toast(err.message, 'err'); }
  };
  poll();
}

async function poll() {
  clearTimeout(timer);
  try { snap = await api('/api/live'); } catch { snap = snap || { active: false }; }
  render();
  // slower when the tab is hidden; the editor keeps working on the server either way
  timer = setTimeout(poll, document.hidden ? 5000 : snap?.running ? 1500 : 5000);
}

// called for every pipeline event (from app.js)
export function liveEvent(ev) {
  if (ev.kind !== 'live_moment' || !follow) return;
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
  $('#liveStop').disabled = !running;
  $('#liveStart').disabled = running;
  const badge = $('#liveBadge');
  badge.textContent = running ? '● REC' : active ? 'ENDED' : 'OFF';
  badge.className = `badge ${running ? 'badge-rec' : 'badge-dim'}`;
  if (!active) {
    $('#liveMeta').textContent = snap?.error ? `last session failed: ${snap.error}` : 'paste a Twitch / YouTube / Kick stream URL';
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

function renderFeed() {
  const img = $('#liveFrame');
  if (snap.segments.length && !document.hidden) img.src = `/api/live/frame?i=${snap.segments.at(-1).i}`;
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
  st.edit = m.status === 'saved' ? 'queued' : m.status === 'processing' ? 'running' : m.status === 'failed' ? 'failed' : 'done';
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
