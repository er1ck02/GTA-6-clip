// UPLOADS / YOUTUBE SHORTS: the upload queue, polled from /api/uploads and nudged by 'upload' events.
// Allowlisted streamers post by themselves (spaced out, within quota); everything else waits here for review.
import { S, $, h, api, toast } from './core.js';

let st = null;
let timer = null;
let lastKey = '';

export function initUploads() {
  $('#upConnect').onclick = connect;
  $('#upQueueClip').onclick = queueCurrent;
  poll();
}

async function poll() {
  clearTimeout(timer);
  try { st = await api('/api/uploads'); } catch { st = st || null; }
  render();
  const busy = st && (st.current || st.counts?.queued);
  timer = setTimeout(poll, document.hidden ? 15000 : busy ? 3000 : 10000);
}

// called for every pipeline event (from app.js)
export function uploadEvent(ev) {
  if (ev.kind === 'upload' || ev.kind === 'youtube_auth') {
    if (ev.kind === 'youtube_auth') toast(ev.ok ? 'YouTube connected' : 'YouTube sign-in failed', ev.ok ? '' : 'err');
    if (ev.kind === 'upload' && ev.status === 'done' && ev.url) toast(`Posted to YouTube: ${ev.url}`, '', 8000);
    clearTimeout(timer);
    timer = setTimeout(poll, 250);
  }
}

async function connect() {
  try {
    const { url } = await api('/api/youtube/connect', { method: 'POST' });
    window.open(url, '_blank', 'noopener');
    toast('Sign in to YouTube in the new tab');
  } catch (err) { toast(`YouTube: ${err.message}`, 'err', 9000); }
}

async function queueCurrent() {
  if (!S.pid || !S.cid) { toast('Open a moment and render it first', 'err'); return; }
  try {
    const j = await api('/api/uploads', { method: 'POST', body: { project: S.pid, clip: S.cid } });
    toast(j.status === 'queued' ? 'Queued: posts automatically' : `Added for review: ${j.reason}`);
    poll();
  } catch (err) { toast(`Upload: ${err.message}`, 'err', 8000); }
}

async function act(j, action) {
  try { await api(`/api/uploads/${j.id}/${action}`, { method: 'POST' }); poll(); }
  catch (err) { toast(err.message, 'err'); }
}

async function saveTitle(j, el) {
  const title = el.value.trim();
  if (!title || title === j.title) return;
  try { await api(`/api/uploads/${j.id}`, { method: 'PATCH', body: { title } }); toast('Title saved'); poll(); }
  catch (err) { toast(err.message, 'err'); el.value = j.title; }
}

const LABEL = { review: 'NEEDS REVIEW', queued: 'QUEUED', uploading: 'UPLOADING', done: 'POSTED', failed: 'FAILED', rejected: 'REJECTED' };
const ago = t => {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 90 ? 'just now' : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
};

function render() {
  const badge = $('#upBadge');
  if (!st) { badge.textContent = 'OFFLINE'; badge.className = 'badge badge-bad'; return; }
  const posting = !!st.current;
  badge.textContent = posting ? '● POSTING' : !st.signed_in ? 'NOT CONNECTED' : !st.enabled ? 'OFF' : st.waiting ? 'WAITING' : 'READY';
  badge.className = `badge ${posting ? 'badge-live' : st.signed_in && st.enabled ? 'badge-dim' : 'badge-bad'}`;
  $('#upConnect').hidden = st.signed_in;
  $('#upConnect').disabled = !st.has_client;
  $('#upConnect').title = st.has_client ? 'One-time Google sign-in for the channel Clip Studio posts to' : 'Save the OAuth client file first (docs/YOUTUBE_SETUP.md)';
  $('#upMeta').textContent = [
    st.channel?.title ? `▶ ${st.channel.title}` : null,
    `${st.today.posts}/${st.today.max_posts} today`,
    `quota ${st.today.quota_used}/${st.today.quota}`,
    st.privacy.toUpperCase(),
    st.allow_channels.length ? `auto: ${st.allow_channels.join(', ')}` : 'no allowlisted streamers',
  ].filter(Boolean).join(' · ');
  $('#upWaiting').textContent = st.waiting || '';
  $('#upWaiting').hidden = !st.waiting;
  const key = JSON.stringify(st.jobs.map(j => [j.id, j.status, Math.round(j.progress * 50), j.reason, j.title]));
  if (key === lastKey) return;
  lastKey = key;
  const list = $('#upList');
  if (!st.jobs.length) {
    list.replaceChildren(h('div', { class: 'empty' }, 'No clips yet. Finished live clips land here; allowlisted streamers post by themselves.'));
    return;
  }
  list.replaceChildren(...st.jobs.map(j => {
    const editable = ['review', 'queued', 'failed'].includes(j.status);
    return h('div', { class: `upjob u-${j.status}` },
      h('div', { class: 'uj-head' },
        h('span', { class: `uj-st s-${j.status}` }, LABEL[j.status] || j.status),
        h('span', { class: 'uj-ch' }, j.channel ? `@${j.channel}` : '—'),
        h('span', { class: 'uj-t' }, ago(j.created))),
      editable
        ? h('input', { class: 'uj-title', value: j.title, maxlength: 100, spellcheck: 'false', title: 'YouTube title (Enter to save)',
            onkeydown: e => { if (e.key === 'Enter') e.target.blur(); }, onchange: e => saveTitle(j, e.target) })
        : h('div', { class: 'uj-title ro' }, j.title),
      j.status === 'uploading' ? h('div', { class: 'uj-bar' }, h('i', { style: { width: `${Math.round(j.progress * 100)}%` } })) : null,
      h('div', { class: 'uj-foot' },
        h('span', { class: 'uj-reason', title: j.error || j.reason }, j.error && j.status !== 'done' ? `${j.reason}: ${j.error}` : j.reason),
        h('span', { class: 'uj-btns' },
          j.status === 'review' ? h('button', { class: 'btn btn-accent btn-sm', onclick: () => act(j, 'approve'), title: 'Post this clip (the streamer allows it)' }, 'APPROVE') : null,
          j.status === 'failed' ? h('button', { class: 'btn btn-accent btn-sm', onclick: () => act(j, 'retry') }, 'RETRY') : null,
          ['review', 'queued', 'failed'].includes(j.status) ? h('button', { class: 'btn btn-ghost btn-sm', onclick: () => act(j, 'reject') }, 'REJECT') : null,
          j.url ? h('a', { class: 'btn btn-ghost btn-sm', href: j.url, target: '_blank', rel: 'noopener' }, 'OPEN') : null)));
  }));
}
