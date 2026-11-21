# Development status

_Last updated: 2026-11-21 · version 0.2.0_

## Milestones

| Phase | Status |
|---|---|
| 1. Environment and architecture | **Done.** Environment doctor, architecture, tool evaluation, project structure, typed interfaces. |
| 2. First working pipeline | **Done.** Local video → metadata → transcription → ranked candidates → selection/adjustment → 9:16 crop → word-timed captions → MP4 → QC. |
| 3. Intelligent highlight detection | Not started. Transcript + loudness signals exist; scene, HUD, chat and LLM signals are next. |
| 4. Advanced tracking | Not started. `CropPlan` keyframes are ready for it; layouts accept a manual facecam rectangle today. |
| 5. Audio and visual enhancements | Partial: two-pass loudness normalisation and optional light voice EQ/compression. Nothing else. |
| 6. User interface | **First version.** Local workstation wired to the real API (see below). |
| 7. Production hardening | Not started beyond the test suite and resumability. |

## What works today (verified against real output files)

- `doctor`: OS, Python, CPU/RAM/GPU, disk, FFmpeg filters and encoders, Python deps, CUDA for CTranslate2, fonts, model availability, recommendations. JSON report in `logs/doctor.json`.
- Local ingestion with validation and content fingerprint; deterministic project ids.
- URL ingestion via yt-dlp: **implemented but untested** (the sandbox blocks video sites).
- Single audio decode to 16 kHz WAV, shared by transcription and loudness analysis.
- faster-whisper transcription with word timestamps, VAD, CPU int8 or CUDA fp16, progress events.
- Highlight candidates from whole sentences, six weighted features, dead-air and duration penalties, peak-aware de-duplication, stored reasons/contributions/confidence, threshold flag.
- Review: accept/reject, trim, per-clip layout, crop centre, facecam rectangle, caption preset; stored as an overlay so regeneration never loses edits.
- Layouts: `gameplay`, `fit_blur`, `facecam_split` (manual facecam region).
- Captions: font-measured wrapping, active-word highlight with scale pop, 4 presets + custom presets, safe margins, avoidance of the facecam panel, profanity masking, ASS + SRT export, hand-edited tracks preserved.
- Render: one FFmpeg pass (trim, composite, captions, two-pass loudnorm), libx264 or NVENC with automatic fallback.
- QC per clip: file, full decode, resolution, aspect, duration, black frames, flat frames, audio presence, A/V stream sync, integrated loudness, true peak, sample-peak clipping, long silence, caption overlap/timeline/word coverage/sync, **caption pixels rendered by libass vs safe area and protected regions**, crop bounds and jumps. Unimplemented checks report `skip`, never `pass`.
- Resumable stages with cache keys; Ctrl+C marks the stage `interrupted`; `resume` continues; caption-only changes don't re-plan or re-transcribe.
- Local HTTP API + SSE event stream, and the workstation UI.

## Test results

Run on 2026-11-21 in the CPU-only sandbox (4 vCPU), whisper small int8:

- `tests/unit`: 80 passed (config validation, caption chunking/timing/ASS/SRT, layouts across 6 source sizes × 3 modes × 3 crop positions, candidate ranking/dedupe/weights, stage runner cache/failure/interrupt).
- `tests/integration`: 6 passed (full pipeline + interrupted render + resume without re-transcription + caption-only re-render; `fit_blur` and `facecam_split` renders pass QC with captions clear of the face; QC fails a broken black/silent/wrong-size file on 6 checks; doctor CLI; API drives analyse → edit crop → edit captions → render → QC, and the edited caption appears in the burned-in ASS).
- Manual run on the 112 s synthetic stream: top candidate is the loud "car exploded" reaction (score 0.77, next best 0.39); rendered clips pass QC 22/22 with 2 honest skips (facecam, subject tracking). Frames inspected visually for captions and all three layouts.

**Not tested yet:** real gaming VODs (none available in the sandbox: video sites blocked), URL ingestion, GPU/NVENC paths, Windows and macOS.

## Known issues

- Opening-hook scoring prefers starting on the punchline over including the setup sentence (the synthetic sample's best clip starts at "Oh my god" rather than "Wait, what is that?"). Phase 3 LLM judging is the planned fix; until then trim in review.
- In `facecam_split`, the facecam can still appear inside the gameplay crop if it sits near the crop window (no masking yet).
- Transcription is a single stage; an interruption mid-transcription restarts that stage (chunked, resumable transcription is planned).
- Category labels are keyword-based and rough.
- Live 9:16 preview in the UI approximates libass text rendering.

## Dependencies

Core: Python ≥ 3.10, FFmpeg with libass + libx264, numpy, pydantic 2, PyYAML,
Pillow, faster-whisper (CTranslate2). UI: fastapi, uvicorn. Optional: yt-dlp.
Dev: pytest, httpx, espeak-ng (sample video synthesis).

## Next tasks

1. Test on real GTA VODs on the user's PC (doctor + `process`), tune weights and thresholds from what comes out.
2. Phase 3: scene cuts (PySceneDetect or FFmpeg `scdet`) for boundary snapping; GTA HUD detection (WASTED/BUSTED) on sampled frames; Twitch chat replay ingestion + spike signal; LLM re-ranker with provider abstraction (Ollama, OpenAI-compatible, Anthropic) and validated JSON output; chunked resumable transcription.
3. Phase 4: facecam auto-detection (bordered static region + MediaPipe face), gameplay subject tracking on sampled frames, smoothed keyframes, dynamic layouts, keyframe interpolation in the renderer.
4. UI: split/move segments, effects track, batch export, platform presets with per-platform encodes.
5. `--json` output on every CLI command so agents can drive the studio.
