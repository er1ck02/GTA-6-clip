// The clipping desk: every tool, lit by real pipeline stage events.
import { S, $, h, on } from './core.js';

const state = {};     // stage -> {status, ts, progress}
let tools = [];
const MAX_LOG = 80;

export function initDesk() {
  on('system', () => { tools = S.system?.tools || []; render(); });
  // seed from what is already on disk for the selected project / clip
  on('project', seedFromDisk);
  on('select', seedFromDisk);
}

function seedFromDisk() {
  const p = S.project;
  if (!p) return;
  const stages = { ...(p.stages || {}) };
  const clipStages = (S.cid && p.clips?.[S.cid]?.stages) || {};
  for (const t of tools) {
    if (!t.stage || t.stage.startsWith('live_') || t.stage === 'upload') continue; // session-wide, not per project
    const live = state[t.stage];
    if (live && live.live) continue; // a live event beats the on-disk state
    const st = stages[t.stage]?.status || clipStages[t.stage];
    state[t.stage] = st ? { status: st, disk: true } : { status: null };
  }
  render();
}

function label(t) {
  const st = t.stage ? state[t.stage] : null;
  if (t.stage && st?.status) {
    const s = st.status;
    let txt = s;
    if (s === 'running' && st.progress != null) txt = `running ${Math.round(st.progress * 100)}%`;
    return { cls: s, txt };
  }
  if (t.stage) {
    if (t.status === 'optional') return { cls: 'optional', txt: 'optional · idle' };
    return { cls: 'ready', txt: 'ready' };
  }
  return { cls: t.status, txt: t.status === 'planned' ? 'planned' : 'optional · not wired' };
}

function render() {
  $('#deskCount').textContent = tools.length || '--';
  $('#deskGrid').replaceChildren(...tools.map((t, i) => {
    const l = label(t);
    return h('div', { class: `tool t-${l.cls}`, title: `${t.name} — ${t.role}${t.stage ? ` · pipeline stage “${t.stage}”` : ' · not integrated yet'}` },
      h('span', { class: 'n' }, String(i + 1).padStart(2, '0')),
      h('span', { class: 'nm' }, t.name, h('small', {}, t.role)),
      h('span', { class: 's' }, l.cls === 'running' || l.cls === 'done' || l.cls === 'cached' || l.cls === 'failed' ? h('span', { class: 'dot' }) : null, l.txt));
  }));
}

export function deskEvent(ev, historical) {
  const forMe = !ev.project || ev.project === S.pid;
  if (ev.kind === 'stage' && forMe) {
    state[ev.stage] = { status: ev.status, ts: ev.ts, live: !historical };
    render();
  } else if (ev.kind === 'progress' && forMe && state[ev.stage]) {
    state[ev.stage].progress = ev.progress;
    render();
  }
  log(ev, historical);
}

function log(ev, historical) {
  if (ev.kind === 'progress') {
    // collapse progress into one line per stage
    const last = $('#eventLog').firstElementChild;
    if (last && last.dataset.prog === `${ev.job}:${ev.stage}`) {
      last.querySelector('.x').textContent = `${ev.stage} ${Math.round(ev.progress * 100)}%${ctx(ev)}`;
      return;
    }
  }
  const t = new Date(ev.ts * 1000).toTimeString().slice(0, 8);
  let text;
  if (ev.kind === 'stage' && ev.stage === 'upload') text = `upload ${ev.moment || ''}: ${ev.decision}${ev.note ? ` — ${ev.note}` : ''}`;
  else if (ev.kind === 'stage') text = `${ev.stage} ${ev.status}${ev.seconds != null ? ` ${ev.seconds}s` : ''}${ev.error ? ` — ${ev.error}` : ''}`;
  else if (ev.kind === 'progress') text = `${ev.stage} ${Math.round(ev.progress * 100)}%`;
  else if (ev.kind === 'job') text = `job ${ev.job_kind || ''} ${ev.status}${ev.error ? ` — ${ev.error}` : ''}`;
  else if (ev.kind === 'live_moment') text = `moment ${ev.moment} ${ev.status}${ev.reasons ? ` (${ev.reasons.join(', ')}, score ${ev.score})` : ''}${ev.upload ? ` · upload: ${ev.upload}` : ''}`;
  else text = JSON.stringify(ev);
  const status = ev.status || '';
  const li = h('li', { class: `ev-${status} ${historical ? 'hist' : ''}`, title: JSON.stringify(ev) },
    h('span', { class: 't' }, t), h('span', { class: 'k' }, ev.kind.toUpperCase().slice(0, 5)), h('span', { class: 'x' }, text + ctx(ev)));
  if (ev.kind === 'progress') li.dataset.prog = `${ev.job}:${ev.stage}`;
  const ol = $('#eventLog');
  ol.querySelector('[data-empty]')?.remove();
  ol.prepend(li);
  while (ol.children.length > MAX_LOG) ol.lastElementChild.remove();
}
const ctx = ev => (ev.clip ? ` · ${ev.clip}` : ev.project && ev.project !== S.pid ? ` · ${ev.project}` : '');

export function logEmpty() {
  const ol = $('#eventLog');
  if (!ol.children.length) ol.append(h('li', { class: 'hist', 'data-empty': '1' }, h('span', { class: 'x' }, 'No pipeline events yet in this server session. Import or render to see stages run.')));
}
