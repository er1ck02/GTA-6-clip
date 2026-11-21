# Development status

_Last updated: 2026-11-21 · version 0.4.0_

## Milestones

| Phase | Status |
|---|---|
| 1. Environment and architecture | **Done.** Environment doctor, architecture, tool evaluation, project structure, typed interfaces. |
| 2. First working pipeline | **Done.** Local video → metadata → transcription → ranked candidates → selection/adjustment → 9:16 crop → word-timed captions → MP4 → QC. |
| 3. Intelligent highlight detection | **Done (first version).** Visual pass (scene cuts, motion, flashes, desaturated death screens, optional Tesseract OCR of HUD words), chat replay (4 formats, rate + hype keywords), scene-cut snapping, event and chat seeded windows, optional LLM re-ranker (Claude / OpenAI-compatible / Ollama, validated JSON, provider fallbacks), chunked resumable transcription. |
| 4. Advanced tracking | **Done (first version).** Facecam auto-detection (MediaPipe BlazeFace / OpenCV Haar + persistent-border search), motion-based gameplay tracking with smoothed keyframes, animated crops in the renderer, `auto` and `facecam_pip` layouts, real tracking QC, live boxes and moving crop in the workstation. |
| 5. Audio and visual enhancements | Partial: two-pass loudness normalisation and optional light voice EQ/compression. Nothing else. |
| 6. User interface | **First version working.** Local workstation wired to the real API: Program/Source with live crop + facecam boxes, Media Bin, Inspector, live 9:16 Output with render + QC, multitrack timeline, live Clipping Desk. |
| 7. Production hardening | Not started beyond the test suite and resumability. |

## What works today (verified against real output files)

- `doctor`: OS, Python, CPU/RAM/GPU, disk, FFmpeg filters and encoders, Python deps, CUDA for CTranslate2, fonts, model availability, recommendations. JSON report in `logs/doctor.json`.
- Local ingestion with validation and content fingerprint; deterministic project ids.
- Multi-track recordings (OBS / SteelSeries / Medal write Game and Mic as separate audio streams): the mic stream is auto-detected by name and transcribed on its own (levelled for recognition only), loudness analysis and the rendered clip use the mix of all streams.
- HEVC sources: rendering reads the original; the workstation gets an automatic 720p H.264 preview proxy because browsers can't reliably play HEVC.
- URL ingestion via yt-dlp: **implemented but untested** (the sandbox blocks video sites).
- Single audio decode to 16 kHz WAV, shared by transcription and loudness analysis.
- faster-whisper transcription with word timestamps, VAD, CPU int8 or CUDA fp16, progress events.
- Highlight candidates from two sources: whole-sentence windows (short lone reactions are padded with surrounding context) and loud audio peaks, so gameplay with little or no commentary still produces moments. Then six weighted features, dead-air and duration penalties, peak-aware de-duplication, stored reasons/contributions/confidence, threshold flag.
- Review: accept/reject, trim, per-clip layout, crop centre, facecam rectangle, caption preset; stored as an overlay so regeneration never loses edits.
- Layouts: `auto` (split when a facecam is found), `gameplay`, `fit_blur`, `facecam_split`, `facecam_pip`.
- Facecam auto-detection once per source (stage `facecam`): synthetic stream with a real face in a bordered panel found at confidence 0.98 with both MediaPipe and OpenCV; the user's Valorant and Overwatch recordings (no webcam) correctly produce no facecam (a gloved first-person hand is held at 0.50, below the 0.6 threshold).
- Gameplay tracking per clip (stage `track`): crop follows the moving subject on the synthetic stream (verified in the rendered pixels: a 203 px pan at the planned time), holds centre under frame-wide motion. QC checks pan speed, direction reversals and that a face is visible in the rendered facecam panel.
- Captions: font-measured wrapping, active-word highlight with scale pop, 4 presets + custom presets, safe margins, avoidance of the facecam panel, profanity masking, ASS + SRT export, hand-edited tracks preserved.
- Visual signals verified on a synthetic stream with a hard cut, white flash and a desaturated red WASTED screen: cut found at the exact second, flash and colour drain found, OCR reads WASTED (Tesseract 5.3). With a chat replay next to the video, the top candidate covers the event with game_event 1.0 and chat_activity above 0.5.
- Chunked transcription: interrupted multi-chunk runs resume with only the unfinished chunk re-transcribed (unit-tested with a fake model; the real model path is exercised whenever audio exceeds `transcription.chunk_seconds`).
- LLM re-ranking: provider fallback chain, JSON-schema validation, refusal handling and score blending tested against local fake Ollama/OpenAI servers and a mocked Anthropic client. Not run against a real provider.
- Render: one FFmpeg pass (trim, composite, captions, two-pass loudnorm), libx264 or NVENC with automatic fallback.
- QC per clip: file, full decode, resolution, aspect, duration, black frames, flat frames, audio presence, A/V stream sync, integrated loudness, true peak, sample-peak clipping, long silence, caption overlap/timeline/word coverage/sync, **caption pixels rendered by libass vs safe area and protected regions**, crop bounds and jumps. Unimplemented checks report `skip`, never `pass`.
- Resumable stages with cache keys; Ctrl+C marks the stage `interrupted`; `resume` continues; caption-only changes don't re-plan or re-transcribe.
- Local HTTP API + SSE event stream, and the workstation UI (`python -m app.cli ui`).

## Test results

Run on 2026-11-21 in the CPU-only sandbox (4 vCPU), whisper small int8. **91 passed.**

- `tests/unit` (85): config validation, caption chunking/timing/ASS/SRT, layouts across 6 source sizes × 3 modes × 3 crop positions, candidate ranking/dedupe/weights, no-speech audio-peak candidates, padding of short reactions, mic-track selection, stage runner cache/failure/interrupt.
- `tests/integration` (6): full pipeline + interrupted render + resume without re-transcription + caption-only re-render; `fit_blur` and `facecam_split` renders pass QC with captions clear of the face; QC fails a broken black/silent/wrong-size file on 6 checks; doctor CLI; the HTTP API drives analyse → crop edit → caption edit → render → QC (edited caption reaches the burned-in ASS), rejects invalid edits, resets fields, shifts hand-edited captions on trim, and streams SSE events.
- **Synthetic stream (112 s):** top candidate is the loud "car exploded" reaction (0.77, next 0.39); clips in all three layouts pass QC; frames inspected visually.
- **Real recordings from the user's PC** (read only, copied into the sandbox):
  - Overwatch, 60 s, H.264 1080p60, Game + Mic tracks. First run found **no** candidates: the pipeline only read audio track 0 (game) and needed whole sentences to fill 10 s. Fixed (mic-track detection, padding, audio-peak candidates). Now: mic transcribed ("I'm dead bro."), top clip 11-21 s rendered and passed QC 22/22 + 2 skips.
  - Valorant, 124 s, **HEVC** 1080p60, two identical unnamed tracks. 7 candidates (speech and audio-peak); top clip rendered and passed QC. The browser could not play HEVC in the workstation; fixed with the preview proxy (built in 53 s for 124 s of footage).
- **Workstation UI:** verified with Playwright at 1600×1000 and 1280×800 (21 scripted checks): crop drag persists and changes both the overlay and the 9:16 canvas, caption text and timing edits persist and show in the preview, trims and facecam edits persist, render shows live stage chips and the QC table, no console errors except the expected 503 while an HEVC preview proxy is being built.

**Not tested yet:** a real GTA stream with a facecam (none available), multi-hour VODs, URL ingestion (video sites blocked in the sandbox), GPU/NVENC, Windows and macOS runs.

## Known issues

- Opening-hook scoring prefers starting on the punchline over including the setup sentence (the synthetic sample's best clip starts at "Oh my god" rather than "Wait, what is that?"). Phase 3 LLM judging is the planned fix; until then trim in review.
- In `facecam_split`, the facecam can still appear inside the gameplay crop if it sits near the crop window (no masking yet).
- Transcription is a single stage; an interruption mid-transcription restarts that stage (chunked, resumable transcription is planned).
- Category labels are keyword-based and rough.
- When game and voice share one audio track, game announcer lines (Valorant's "Spike planted", "Last player standing") are transcribed and treated as speech. Use a separate mic track when recording, or set `transcription.speech_track`.
- Scores on real footage were all below the 0.55 threshold (best 0.53): thresholds were tuned on synthetic speech and need calibration on real streams.
- The default centre crop can cut off the crosshair/weapon area in shooters; `fit_blur` or a manual crop X is the workaround until tracking lands.
- Live 9:16 preview in the UI approximates libass text rendering.

## Dependencies

Core: Python ≥ 3.10, FFmpeg with libass + libx264, numpy, pydantic 2, PyYAML,
Pillow, faster-whisper (CTranslate2). UI: fastapi, uvicorn. Optional: yt-dlp.
Dev: pytest, httpx, espeak-ng (sample video synthesis).

## Next tasks

1. Run on real GTA streams on the user's PC (doctor + `process` + `ui`), calibrate weights and `minimum_score` from the results.
2. Phase 3 follow-ups: calibrate visual/chat weights on real GTA 6 streams; one real LLM provider run; chat download helpers (the user exports chat themselves today).
3. Phase 4 follow-ups: test facecam detection on real webcam streams; per-segment layouts inside one clip; optional YOLO object path as an alternative tracker input.
4. UI: split/move segments, effects track, batch export, platform presets with per-platform encodes.
5. `--json` output on every CLI command so agents can drive the studio.
