// Boot, header, status strip, live events (SSE) and the frame loop.
import { S, $, h, on, emit, api, toast } from './core.js';
import { video, loadProjects, loadProject, refreshProject, loadQC, importSource } from './actions.js';
import { initProgram, drawOverlay, tickProgram, invalidateCaptionLine } from './program.js';
import { initOutput, drawOutput, onRenderEvent, onRenderJobDone } from './output.js';
import { initBin } from './bin.js';
import { initInspector, tickInspector, syncCropInputs } from './inspector.js';
import { initTimeline, tickTimeline } from './timeline.js';
import { initDesk, deskEvent, logEmpty } from './desk.js';
import { startPolling, stopPolling } from './poller.js';

const BOOT = Date.now();
const ORDER = ['ingest', 'metadata', 'extract_audio', 'transcribe', 'loudness', 'candidates', 'plan', 'captions', 'render', 'qc'];
const ANALYZE = ['metadata', 'extract_audio', 'transcribe', 'loudness', 'candidates'];
let lastSeq = 0;
let activity = null;
const finishedJobs = new Map(); // job id -> final job event (a cached render can finish before POST returns) // last live stage event for the status strip

// ------------------------------------------------------------------ header
function initHeader() {
  const sel = $('#projectSelect');
  sel.onchange = () => loadProject(sel.value || null);
  $('#importForm').onsubmit = async e => {
    e.preventDefault();
    const inp = $('#importInput');
    const btn = e.submitter || $('#importForm button');
    btn.disabled = true;
    const ok = await importSource(inp.value);
    btn.disabled = false;
    if (ok) inp.value = '';
  };
  on('projects', () => {
    const name = p => {
      const src = p.source?.url || p.source?.path || '';
      return `${p.id}  ·  ${src.split(/[\\/]/).pop()}`;
    };
    sel.replaceChildren(...(S.projects.length
      ? S.projects.map(p => h('option', { value: p.id, selected: p.id === S.pid }, name(p)))
      : [h('option', { value: '' }, 'no projects yet')]));
  });
  on('project', () => {
    $('#sampleBadge').hidden = !S.project?.sample;
    if ($('#projectSelect').value !== (S.pid || '')) $('#projectSelect').value = S.pid || '';
    renderStatus();
  });
  on('candidates', renderStatus);
  on('system', () => {
    const s = S.system;
    const b = $('#deviceBadge');
    if (!s) { b.textContent = 'SYSTEM ?'; b.className = 'badge badge-bad'; return; }
    const gpu = s.doctor?.system?.gpus?.length ? s.doctor.system.gpus[0] : null;
    b.textContent = `${String(s.device).toUpperCase()} · ${String(s.profile).toUpperCase()}${s.doctor?.ok ? '' : ' · DOCTOR ✕'}`;
    b.className = `badge ${s.doctor?.ok ? 'badge-dim' : 'badge-bad'}`;
    const model = String(s.whisper_model || '').split(/[\\/]/).pop();
    b.title = `device ${s.device}${gpu ? ` (${typeof gpu === 'string' ? gpu : JSON.stringify(gpu)})` : ' · no GPU visible'} · profile ${s.profile} · whisper ${model} · ${s.doctor?.system?.cpu || ''} · doctor ${s.doctor?.ok ? 'ok' : 'has failures'}`;
  });
}

// ------------------------------------------------------------------ status strip
function renderStatus() {
  const step = $('#sbStep'), text = $('#sbText'), chips = $('#sbStages');
  const p = S.project;
  if (!S.pid || !p) {
    step.textContent = 'IDLE';
    text.textContent = S.projects.length ? 'Pick a project.' : 'No projects yet: import a video to start.';
    chips.replaceChildren();
    return;
  }
  const stages = p.stages || {};
  const st = s => S.liveStages[s] || stages[s]?.status || '';
  const running = activity && activity.status === 'running' && activity.project === S.pid ? activity : null;
  if (running) {
    const n = ORDER.indexOf(running.stage) + 1;
    step.textContent = `${String(n).padStart(2, '0')} / ${running.stage.toUpperCase().replace('_', ' ')}`;
    const pr = S.progress[running.stage];
    text.textContent = `${running.stage} running${pr != null ? ` · ${Math.round(pr * 100)}%` : ''}${running.clip ? ` · clip ${running.clip}` : ''}`;
  } else {
    const allDone = ANALYZE.every(s => ['done', 'cached'].includes(st(s)));
    const failed = ANALYZE.find(s => st(s) === 'failed');
    step.textContent = failed ? 'FAILED' : allDone ? 'READY' : 'ANALYSING';
    const m = p.media;
    text.textContent = failed ? `analysis failed at ${failed}: ${stages[failed]?.error || 'see log'}`
      : allDone ? `${S.candidates.length} moments · ${m ? `${m.duration.toFixed(1)}s ${m.width}×${m.height} ${m.video_codec}` : ''}${p.sample ? ' · SYNTHETIC SAMPLE SOURCE' : ''}`
      : 'waiting for analysis stages…';
  }
  chips.replaceChildren(...ANALYZE.map(s => h('span', { class: `sb-stage ${st(s)}`, title: `${s}: ${st(s) || 'not run'}` }, s.replace('_', ' '))));
}

// ------------------------------------------------------------------ live events
function connectEvents() {
  const badge = $('#sseBadge');
  const es = new EventSource('/api/events');
  es.onopen = () => {
    stopPolling();
    badge.textContent = 'EVENTS ● LIVE'; badge.className = 'badge badge-live';
    badge.title = 'Live Server-Sent Events from /api/events';
    $('#deskMeta').textContent = 'LIVE · /api/events';
  };
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) {
      // the stream was refused (non-200): fall back to polling and retry the stream later
      es.close();
      badge.textContent = 'EVENTS ◐ POLLING'; badge.className = 'badge badge-bad';
      badge.title = '/api/events is not available, so stage status is polled from /api/jobs and project state. Retrying the live stream every 30 s.';
      $('#deskMeta').textContent = 'POLLING /api/jobs · event stream unavailable';
      startPolling(ev => handleEvent(ev, false));
      setTimeout(connectEvents, 30000);
    } else {
      badge.textContent = 'EVENTS ○ RECONNECTING'; badge.className = 'badge badge-bad';
    }
  };
  es.onmessage = m => {
    let ev;
    try { ev = JSON.parse(m.data); } catch { return; }
    if (ev.seq <= lastSeq) return; // history replays on reconnect
    lastSeq = ev.seq;
    handleEvent(ev, ev.ts * 1000 < BOOT - 1500);
  };
  setTimeout(logEmpty, 1500);
}

async function handleEvent(ev, historical, replay = false) {
  if (!replay) deskEvent(ev, historical);
  if (historical) return;
  const mine = ev.project === S.pid;
  if (ev.kind === 'stage') {
    activity = ev;
    if (mine && !ev.clip) { S.liveStages[ev.stage] = ev.status; emit('stages'); }
    if (mine) onRenderEvent(ev);
    renderStatus();
  } else if (ev.kind === 'progress') {
    S.progress[ev.stage] = ev.progress;
    if (mine) emit('stages');
    renderStatus();
  } else if (ev.kind === 'job') {
    if (ev.status !== 'done' && ev.status !== 'failed') return;
    if (ev.job) finishedJobs.set(ev.job, ev);
    activity = null;
    if (ev.job_kind === 'analyze') {
      await loadProjects();
      if (mine) {
        if (ev.status === 'failed') toast(`Analysis failed: ${ev.error || 'see event log'}`, 'err', 8000);
        await loadProject(S.pid, { keepClip: true });
      }
    } else if (ev.job_kind === 'render' && mine) {
      const refresh = async () => { await refreshProject(); await loadQC(); };
      const handled = await onRenderJobDone(ev, refresh);
      if (!handled && ev.clip === S.cid) await refresh(); // render started elsewhere (CLI / another tab)
      else if (!handled) await refreshProject();
    }
    renderStatus();
  }
}

// a render already running when the page loads (e.g. after reload)
async function adoptRunningJobs() {
  try {
    const jobs = await api('/api/jobs');
    const r = jobs.find(j => j.kind === 'render' && j.project === S.pid && (j.status === 'queued' || j.status === 'running'));
    if (r) { S.render = { cid: r.clip, job: r.id, status: r.status, stages: {}, error: null }; emit('render'); }
  } catch {}
}

// fallback poll while a project is still analysing (in case the event stream drops)
setInterval(async () => {
  if (!S.pid || !S.project || S.candidates.length) return;
  const before = JSON.stringify(S.project.stages);
  await refreshProject();
  if (S.project && JSON.stringify(S.project.stages) !== before && S.project.stages?.candidates?.status === 'done') {
    await loadProject(S.pid, { keepClip: true });
  }
}, 5000);

// ------------------------------------------------------------------ frame loop
let lastT = -1;
function frame() {
  const v = video();
  const t = v.currentTime;
  if (t !== lastT || S.dirty) {
    lastT = t;
    S.dirty = false;
    try {
      drawOverlay();
      drawOutput();
      tickProgram();
      tickTimeline();
      tickInspector();
      syncCropInputs();
    } catch (e) { console.error(e); }
  }
  requestAnimationFrame(frame);
}

// ------------------------------------------------------------------ boot
async function boot() {
  initHeader(); initProgram(); initOutput(); initBin(); initInspector(); initTimeline(); initDesk();
  on('preview', invalidateCaptionLine);
  on('render', () => {
    const r = S.render, ev = r && finishedJobs.get(r.job);
    if (ev && (r.status === 'queued' || r.status === 'running')) handleEvent({ ...ev, project: S.pid }, false, true);
  });
  video().addEventListener('seeked', () => { S.dirty = true; });
  video().addEventListener('loadeddata', () => { S.dirty = true; });
  connectEvents();
  requestAnimationFrame(frame);

  const [sys, cfg] = await Promise.allSettled([api('/api/system'), api('/api/config')]);
  S.system = sys.status === 'fulfilled' ? sys.value : null;
  if (sys.status === 'rejected') toast(`System check failed: ${sys.reason.message}`, 'err');
  if (cfg.status === 'fulfilled') { S.config = cfg.value; S.presets = cfg.value.caption_presets || []; }
  emit('system');

  try { await loadProjects(); } catch (e) { toast(`Cannot list projects: ${e.message}`, 'err'); }
  let pid = decodeURIComponent(location.hash.slice(1));
  if (!S.projects.some(p => p.id === pid)) {
    try { pid = localStorage.getItem('clipstudio.pid'); } catch { pid = null; }
  }
  if (!S.projects.some(p => p.id === pid)) pid = S.projects[0]?.id || null;
  await loadProject(pid);
  await adoptRunningJobs();
  renderStatus();
}

// read-only handle for debugging / automated UI checks
window.clipstudio = { state: S };

boot();
