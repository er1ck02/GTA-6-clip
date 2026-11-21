// Media bin: ranked moments (highlight candidates), or the analysis progress while there are none yet.
import { S, $, h, on, P, fmtT } from './core.js';
import { selectClip, setStatus } from './actions.js';

const ANALYZE = ['metadata', 'extract_audio', 'transcribe', 'loudness', 'candidates'];

export function initBin() {
  on('candidates', render);
  on('select', render);
  on('project', render);
  on('stages', render);
}

function render() {
  const list = $('#binList');
  const meta = $('#binMeta');
  if (!S.pid) {
    meta.textContent = '';
    list.replaceChildren(h('div', { class: 'empty' },
      h('strong', {}, 'No projects yet.'), h('br'), 'Paste a local video path (or a VOD URL, if yt-dlp is installed) into IMPORT above. Analysis runs in the background and its stages light up on the clipping desk.'));
    return;
  }
  if (!S.project) { list.replaceChildren(h('div', { class: 'empty' }, 'Loading project…')); return; }
  if (!S.candidates.length) {
    meta.textContent = 'ANALYSING';
    const stages = S.project.stages || {};
    const failed = ANALYZE.find(s => (S.liveStages[s] || stages[s]?.status) === 'failed');
    list.replaceChildren(h('div', { class: 'empty' },
      failed ? h('strong', {}, `Analysis failed at “${failed}”.`) : h('strong', {}, 'No moments yet.'),
      h('div', {}, failed ? (stages[failed]?.error || 'See the event log.') : 'The project is being analysed; moments appear when the candidates stage finishes.'),
      h('ul', { class: 'stage-list' }, ANALYZE.map(s => {
        const st = S.liveStages[s] || stages[s]?.status || 'waiting';
        const pr = s === 'transcribe' && st === 'running' && S.progress.transcribe != null ? ` ${Math.round(S.progress.transcribe * 100)}%` : '';
        return h('li', {}, h('span', {}, s), h('span', { class: `s-${st}` }, st + pr));
      }))));
    return;
  }
  const acc = S.candidates.filter(c => c.status === 'accepted').length;
  meta.textContent = `${S.candidates.length} · ${acc}✓`;
  const clips = S.project.clips || {};
  list.replaceChildren(...S.candidates.map(c => {
    const rendered = clips[c.id]?.output;
    const el = h('div', { class: `moment ${c.id === S.cid ? 'sel' : ''} ${c.status}`, 'data-cid': c.id, tabindex: 0, title: c.description },
      h('div', { class: 'm-side' },
        h('div', { class: 'm-thumb' },
          h('img', { src: P(S.pid, `/frame?t=${c.peak_time.toFixed(2)}&w=240`), alt: '', loading: 'lazy' }),
          h('span', { class: 'm-rank' }, `#${c.rank}`),
          h('span', { class: 'm-dur' }, `${c.duration.toFixed(1)}s`)),
        h('span', { class: 'm-acts' },
          h('button', { class: `m-act acc ${c.status === 'accepted' ? 'on' : ''}`, title: c.status === 'accepted' ? 'Accepted (click to unset)' : 'Accept', 'aria-label': 'Accept',
            onclick: e => { e.stopPropagation(); setStatus(c.id, c.status === 'accepted' ? 'pending' : 'accepted'); } }, '✓'),
          h('button', { class: `m-act rej ${c.status === 'rejected' ? 'on' : ''}`, title: c.status === 'rejected' ? 'Rejected (click to unset)' : 'Reject', 'aria-label': 'Reject',
            onclick: e => { e.stopPropagation(); setStatus(c.id, c.status === 'rejected' ? 'pending' : 'rejected'); } }, '✕'))),
      h('div', { class: 'm-main' },
        h('div', { class: 'm-title' }, c.title),
        h('div', { class: 'm-row' }, `${fmtT(c.start)} → ${fmtT(c.end)}`, c.user_adjusted ? h('span', { title: 'Trimmed by you' }, '· trimmed') : null),
        h('div', { class: 'm-row' },
          h('span', { class: `cat cat-${c.category}` }, c.category),
          h('div', { class: `m-score ${c.passes_threshold ? '' : 'below'}`, title: c.passes_threshold ? 'passes the minimum score' : 'below the minimum score' },
            h('i', { style: { width: `${Math.round(c.score * 100)}%` } })),
          h('span', {}, c.score.toFixed(2))),
        h('div', { class: 'm-row' },
          h('span', { class: `st st-${c.status}` }, c.status),
          rendered ? h('span', { class: 'm-rendered', title: clips[c.id].qc_passed ? 'Rendered, QC passed' : 'Rendered, QC did not pass' }, clips[c.id].qc_passed ? '· MP4 ✓' : '· MP4 ⚠') : null)));
    el.addEventListener('click', () => { if (c.id !== S.cid) selectClip(c.id); });
    el.addEventListener('keydown', e => { if (e.key === 'Enter') selectClip(c.id); });
    return el;
  }));
}
