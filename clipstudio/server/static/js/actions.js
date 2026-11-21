// Everything that talks to the backend and mutates shared state.
import { S, api, P, C, emit, toast, round } from './core.js';

export const video = () => document.getElementById('video');

export async function loadProjects() {
  S.projects = await api('/api/projects');
  emit('projects');
}

export async function loadProject(pid, { keepClip = false } = {}) {
  const changed = pid !== S.pid;
  S.pid = pid;
  if (changed) {
    S.cid = null; S.preview = null; S.qc = null; S.candidates = []; S.render = null;
    S.liveStages = {}; S.progress = {};
    try { localStorage.setItem('clipstudio.pid', pid); } catch {}
    if (location.hash !== `#${pid}`) history.replaceState(null, '', `#${pid}`);
  }
  if (!pid) { S.project = null; emit('project'); return; }
  try {
    S.project = await api(P(pid));
  } catch (e) {
    S.project = null;
    emit('project');
    toast(`Could not load project: ${e.message}`, 'err');
    return;
  }
  if (changed) {
    const v = video();
    if (S.project.media && S.project.preview && !S.project.preview.ready) {
      // HEVC and similar sources: the server builds an H.264 proxy the browser can play
      v.removeAttribute('src'); v.load();
      fetch(P(pid, '/source')).catch(() => {});  // starts the proxy job
      toast('This source is HEVC: building a browser preview (rendering still uses the original)…');
      waitForPreview(pid);
    } else if (S.project.media) {
      v.src = P(pid, '/source');
    } else {
      v.removeAttribute('src'); v.load();
    }
  }
  emit('project');
  await loadCandidates();
  if (!S.candidates.length) return;
  let next = keepClip && S.cid && S.candidates.some(c => c.id === S.cid) ? S.cid : null;
  if (!next) {
    let saved = null;
    try { saved = localStorage.getItem(`clipstudio.cid.${pid}`); } catch {}
    next = S.candidates.find(c => c.id === saved)?.id
      || S.candidates.find(c => c.status === 'accepted')?.id
      || S.candidates[0].id;
  }
  if (next !== S.cid || !S.preview) await selectClip(next);
}

export async function refreshProject() {
  if (!S.pid) return;
  try {
    S.project = await api(P(S.pid));
    emit('project');
  } catch (e) { console.warn(e); }
}

export async function loadCandidates() {
  if (!S.pid) return;
  try {
    const d = await api(P(S.pid, '/candidates'));
    S.candidates = d.candidates || [];
    S.weights = d.weights || {};
  } catch (e) {
    S.candidates = [];
    toast(`Could not load moments: ${e.message}`, 'err');
  }
  emit('candidates');
}

export async function selectClip(cid, { seek = true } = {}) {
  S.cid = cid;
  S.selChunk = null;
  S.qc = null;
  try { localStorage.setItem(`clipstudio.cid.${S.pid}`, cid); } catch {}
  emit('select');
  await refreshPreview();
  const c = S.preview?.candidate;
  if (seek && c) {
    const v = video();
    v.pause();
    try { v.currentTime = c.start; } catch {}
  }
  loadQC();
}

export async function refreshPreview() {
  if (!S.pid || !S.cid) return;
  const cid = S.cid;
  try {
    const pv = await api(C(S.pid, cid, '/preview'));
    if (cid !== S.cid) return;
    S.preview = pv;
    // keep the bin's copy of the candidate in sync (start/end/status edits)
    const i = S.candidates.findIndex(c => c.id === cid);
    if (i >= 0) S.candidates[i] = pv.candidate;
    if (S.selChunk != null && (!pv.captions || S.selChunk >= pv.captions.chunks.length)) S.selChunk = null;
  } catch (e) {
    toast(`Preview failed: ${e.message}`, 'err');
  }
  emit('preview');
}

export async function patchClip(fields, { quiet = false } = {}) {
  if (!S.pid || !S.cid) return null;
  try {
    const c = await api(C(S.pid, S.cid, ''), { method: 'PATCH', body: fields });
    const i = S.candidates.findIndex(x => x.id === c.id);
    if (i >= 0) S.candidates[i] = c;
    emit('candidates');
    await refreshPreview();
    if (!quiet) toast(`Saved ${Object.keys(fields).join(', ')}`);
    return c;
  } catch (e) {
    toast(`Edit rejected: ${e.message}`, 'err', 6000);
    await refreshPreview(); // roll the UI back to what the server has
    return null;
  }
}

export async function setStatus(cid, status) {
  try {
    const c = await api(C(S.pid, cid, ''), { method: 'PATCH', body: { status } });
    const i = S.candidates.findIndex(x => x.id === c.id);
    if (i >= 0) S.candidates[i] = c;
    if (cid === S.cid && S.preview) S.preview.candidate = c;
    emit('candidates');
  } catch (e) { toast(`Status change failed: ${e.message}`, 'err'); }
}

export async function saveCaptions(track, msg = 'Captions saved') {
  try {
    await api(C(S.pid, S.cid, '/captions'), { method: 'PUT', body: track });
    await refreshPreview();
    toast(msg);
    return true;
  } catch (e) {
    toast(`Captions rejected: ${e.message}`, 'err', 6000);
    await refreshPreview();
    return false;
  }
}

export async function resetCaptions() {
  try {
    await api(C(S.pid, S.cid, '/captions'), { method: 'DELETE' });
    S.selChunk = null;
    await refreshPreview();
    toast('Captions reset to automatic (from transcript)');
  } catch (e) { toast(`Reset failed: ${e.message}`, 'err'); }
}

export async function loadQC() {
  if (!S.pid || !S.cid) return;
  const cid = S.cid;
  try {
    const qc = await api(C(S.pid, cid, '/qc'));
    if (cid === S.cid) S.qc = qc;
  } catch (e) {
    if (cid === S.cid) S.qc = null;
  }
  emit('qc');
}

export async function startRender() {
  if (!S.pid || !S.cid) return;
  const cid = S.cid;
  try {
    const job = await api(C(S.pid, cid, '/render'), { method: 'POST' });
    S.render = { cid, job: job.id, status: job.status, stages: {}, error: null };
    emit('render');
    toast('Render queued');
  } catch (e) {
    toast(`Render failed to start: ${e.message}`, 'err');
  }
}

export async function importSource(value) {
  const v = value.trim();
  if (!v) return;
  const body = /^https?:\/\//i.test(v) ? { url: v } : { input_path: v };
  try {
    const r = await api('/api/projects', { method: 'POST', body });
    toast(`Imported → ${r.project}; analysis queued`);
    await loadProjects();
    await loadProject(r.project);
    return true;
  } catch (e) {
    toast(`Import failed: ${e.message}`, 'err', 7000);
    return false;
  }
}

// crop helper: move the gameplay crop locally (live feedback while dragging), returns centre 0..1
export function setLocalCropCenter(center) {
  const pv = S.preview;
  if (!pv) return null;
  const plan = pv.plan, kf = plan.keyframes[0];
  const g = kf.gameplay;
  const sw = plan.source_width;
  let x = Math.round(center * sw - g.w / 2);
  x = Math.max(0, Math.min(sw - g.w, x));
  g.x = x;
  S.dirty = true;
  return round((x + g.w / 2) / sw, 4);
}


async function waitForPreview(pid) {
  for (let i = 0; i < 720; i++) {
    await new Promise(r => setTimeout(r, 5000));
    if (S.project?.id !== pid) return;
    try {
      const pr = await api(P(pid));
      if (pr.preview?.ready) {
        S.project.preview = pr.preview;
        video().src = P(pid, '/source');
        toast('Browser preview ready');
        return;
      }
    } catch { /* keep waiting */ }
  }
}
