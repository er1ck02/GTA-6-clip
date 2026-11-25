// Fallback when /api/events (SSE) is unavailable: poll /api/jobs and the on-disk stage
// records of the projects with active jobs, and turn the differences into the same
// event shapes the SSE stream would deliver ({kind:'stage'|'job', ...}).
import { api, P } from './core.js';

let timer = null, seq = 1e9, jobsSeen = null;
const stageSeen = new Map(); // "pid|clip|stage" -> status

export function startPolling(onEvent) {
  if (timer) return;
  const tick = async () => {
    let active = false;
    try { active = await poll(onEvent); } catch (e) { console.warn('poll', e.message); }
    timer = setTimeout(tick, active ? 900 : 2500);
  };
  tick();
}
export function stopPolling() { clearTimeout(timer); timer = null; }
export const polling = () => !!timer;

async function poll(onEvent) {
  const jobs = await api('/api/jobs');
  const first = jobsSeen == null;
  const prev = jobsSeen || new Map();
  jobsSeen = new Map(jobs.map(j => [j.id, j.status]));
  const changed = jobs.filter(j => prev.get(j.id) !== j.status);
  const live = jobs.filter(j => j.status === 'queued' || j.status === 'running');
  const pids = new Set([...live, ...(first ? [] : changed)].map(j => j.project).filter(Boolean));
  const now = Date.now() / 1000;
  const ctxFor = (pid, clip) => {
    const j = live.find(x => x.project === pid && (!clip || x.clip === clip)) || changed.find(x => x.project === pid);
    return j ? { job: j.id } : {};
  };

  // queued/running job events first, so the UI knows a job is underway
  if (!first) for (const j of changed) if (j.status === 'queued' || j.status === 'running') {
    onEvent({ seq: seq++, ts: now, kind: 'job', job: j.id, status: j.status, job_kind: j.kind, project: j.project, clip: j.clip, polled: true });
  }

  for (const pid of pids) {
    let p;
    try { p = await api(P(pid)); } catch { continue; }
    const rows = Object.entries(p.stages || {}).map(([s, r]) => [null, s, r.status, r]);
    for (const [cid, c] of Object.entries(p.clips || {})) for (const [s, st] of Object.entries(c.stages || {})) rows.push([cid, s, st, null]);
    for (const [clip, stage, status, rec] of rows) {
      const k = `${pid}|${clip || ''}|${stage}`;
      const was = stageSeen.get(k);
      stageSeen.set(k, status);
      if (was === status) continue;
      if (was === undefined && status !== 'running') continue; // baseline: only surface what is running now
      onEvent({ seq: seq++, ts: now, kind: 'stage', stage, status, project: pid, clip: clip || undefined,
        seconds: rec?.seconds ?? undefined, error: rec?.error || undefined, polled: true, ...ctxFor(pid, clip) });
    }
  }

  if (!first) for (const j of changed) if (j.status === 'done' || j.status === 'failed') {
    onEvent({ seq: seq++, ts: now, kind: 'job', job: j.id, status: j.status, job_kind: j.kind, project: j.project, clip: j.clip, error: j.error, polled: true });
  }
  return live.length > 0;
}
