# Development status

_Last updated: 2026-11-21 · version 0.6.0_

## Milestones

| Phase | Status |
|---|---|
| 1. Environment and architecture | **Done.** Environment doctor, architecture, tool evaluation, project structure, typed interfaces. |
| 2. First working pipeline | **Done.** Local video → metadata → transcription → ranked candidates → selection/adjustment → 9:16 crop → word-timed captions → MP4 → QC. |
| 3. Intelligent highlight detection | **Done (first version).** Visual pass (scene cuts, motion, flashes, desaturated death screens, optional Tesseract OCR of HUD words), chat replay (4 formats, rate + hype keywords), scene-cut snapping, event and chat seeded windows, optional LLM re-ranker (Claude / OpenAI-compatible / Ollama, validated JSON, provider fallbacks), chunked resumable transcription. |
| 4. Advanced tracking | **Done (first version).** Facecam auto-detection (MediaPipe BlazeFace / OpenCV Haar + persistent-border search), motion-based gameplay tracking with smoothed keyframes, animated crops in the renderer, `auto` and `facecam_pip` layouts, real tracking QC, live boxes and moving crop in the workstation. |
| 5. Audio and visual enhancements | **Done (first version).** Voice chain (off/light/strong: high-pass, compression, FFT noise reduction, presence EQ, de-esser), game ducking under a separate mic track (sidechain compression), punch-in zooms on the peak moment and on-screen events, sharpen/contrast/vibrance on the gameplay picture, optional animated hook title, two-pass loudness normalisation. |
| 6. User interface | **Done (first version).** Workstation: Program/Source with live crop + facecam boxes, Media Bin with batch export, Inspector with cuts, live 9:16 Output with render, platform targets, downloads and QC, multitrack timeline with trim, cut (shift-drag), split, move and an effects track, live Clipping Desk. CLI: `--json` on every command, folder/multi-file batches, cuts, platform copies. |
| 7. Production hardening | **Done (first version).** 2-hour VOD processed end to end with flat audio memory (memory-mapped WAV, block RMS), chunked resumable transcription survived a crash and a container restart, batch runs continue past failing files, `--json` errors, installable wheel that ships the UI (fresh-venv verified), Windows/macOS/Linux setup scripts, and `scripts/acceptance.py`: 12/12 spec acceptance criteria pass on the owner's real Valorant recording. |

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
- Render: one FFmpeg pass (trim, composite, punch zoom, sharpen/colour, captions, voice chain, ducking, two-pass loudnorm), libx264 or NVENC with automatic fallback.
- Enhancements measured in real encodes: with a Game + Mic source, the game tone drops while the mic speaks only when ducking is on (and the voice survives); a punch-in changes only the frames inside its window, and the zoomed frame matches the centre of the plain frame scaled 1.3x. An optional hook title is placed at the top of the gameplay picture (below a top-corner PiP) and QC checks it separately from the captions.
- QC per clip: file, full decode, resolution, aspect, duration, black frames, flat frames, audio presence, A/V stream sync, integrated loudness, true peak, sample-peak clipping, long silence, caption overlap/timeline/word coverage/sync, **caption pixels rendered by libass vs safe area and protected regions**, crop bounds and jumps. Unimplemented checks report `skip`, never `pass`.
- Resumable stages with cache keys; Ctrl+C marks the stage `interrupted`; `resume` continues; caption-only changes don't re-plan or re-transcribe.
- Local HTTP API + SSE event stream, and the workstation UI (`python -m app.cli ui`).
- Cuts inside a clip: video, audio and captions all drop the section (render measured: a 2.2 s cut gives a 3.8 s file with the cut tone gone); QC checks the shortened duration. Split and move work from the timeline and API.
- Platform copies (TikTok, YouTube Shorts, Instagram Reels, or custom presets): stream copy when the master fits, constrained re-encode when not (verified: 24 fps cap and 3 s trim), JSON post sidecar; only for clips that passed QC.
- `--json` on every CLI command, including errors; batches over folders continue past a failing file.
- Live mode (`clipstudio live`): rolling 90 s buffer, triggers on audio jumps, chat spikes, reaction words and "clip" commands, saved moments edited automatically, review/upload queue with a per-streamer allowlist. Workstation LIVE panel (feed, buffer, trigger graph, chat, moment pipeline, FOLLOW). Verified on real encodes played back in real time and in the browser (Playwright, no console errors). YouTube live streams with separate video/audio HLS and expiring URLs are handled (reconnect test against a local HTTP server). The first real attempt on a YouTube live stream failed on the format request; fixed, re-run pending.
- Multi-channel live mode: watch several streams at once (`live --url A --url B`, `--all` from `live.channels`, or WATCH LIVE repeatedly / WATCH ALL in the workstation). One editing gate shared by every stream (`live.max_parallel_edits`), stream cap `live.max_streams`, LIVE panel grid with a tile per stream. Tested with concurrent dry-run streams (server test) and the shared gate (unit test).
- Sources panel (replaces the top-bar import and the live URL form): paste any number of links or file paths at once; each is classified (Twitch/Kick channel = live, /videos/ = VOD, YouTube asked once via yt-dlp) and either watched live or imported and analysed. One table lists every stream and VOD with status, time, moments, ready clips, edit queue and start/stop/remove/open (`POST/GET /api/sources`, `POST /api/live/restart`).
- Production agent (`production start|stop|status`, Windows shortcuts): resumes the saved lineup, stands in a past streamer who is live and playing GTA for an offline channel and swaps back when the original returns, keeps Windows awake, edits what the last stop or crash left unedited; stop drains (or with `--now` shelves) edits, VOD jobs and uploads with a countdown, then closes the server; a supervisor restarts a crashed server. Unit-tested with fake streams and run end to end in the sandbox (start, status, stop, server closes); first Windows run pending.
- GTA VI stream test (Ludwig, 30 min live capture, RTX 4080): 10 candidates at 0.58-0.70, 5 clips in 2 min. Fixed since: audio/video sync on recordings whose streams start late (4 of 5 then passed QC; the 5th is a black in-game phone-camera shot), facecam picking an in-game face.

## Test results

Run on 2026-11-21 in the CPU-only sandbox (4 vCPU, 15.7 GB RAM), whisper small int8. **172 passed** (`pytest -q`, 5 min 22 s).

- `tests/unit` (157): config validation, captions (chunking, timing, ASS/SRT, hook title), layouts across source sizes and modes, candidates and weights, visual/chat signals, LLM provider chain against fakes, facecam and tracking on synthetic frames, chunked transcription resume, audio graph / punch zoom / cut construction, WAV memory-mapping, Windows behaviour (FFmpeg 9 filter list, cuBLAS check and CPU fallback, NVIDIA DLL folders, Ctrl+Break, fonts without fontconfig), CLI JSON and batch input handling, stage runner.
- `tests/integration` (15): full pipeline + interrupt + resume; every layout renders and passes QC; auto facecam split + tracking; visual + chat signals pick the right moment; QC fails a broken file; doctor; the HTTP API (analyse, crop/caption edits, render, cuts, split, batch export with platform copies, SSE); game ducking, punch zoom, cuts and platform copies measured in real encodes; black clip edges detected; a silent Mic stream falls back to the mix and still gets a transcript; QC warns on black that is in the source footage and fails black the render added.
- **Acceptance runner on the owner's real recordings** (`scripts/acceptance.py`):
  - Valorant, 124 s HEVC 1080p60: **12/12** including a real SIGINT during render then `resume`, and a wheel installed into a fresh venv ([report](docs/acceptance/valorant_2026-11-21.md)).
  - Overwatch, 60 s H.264, Game + Mic tracks: 11/12 on the first run; criterion 12 failed on a real dependency conflict (`mediapipe` needs `opencv-contrib-python`, the project asked for the headless build). Fixed; `pip check` is now clean.
- **Owner's PC, Windows 11** (RTX 4080 16 GB, Python 3.12.10, FFmpeg 9.1), first run on 2026-11-21:
  - Install, doctor, UI (HTTP 200), NVENC and whisper large-v3-turbo fp16 on CUDA all worked after local patches.
  - Acceptance on the Valorant recording: 11/12. Criterion 10 failed because the runner sent Ctrl+C to its own console.
  - 45.7-minute Overwatch session (Medal, Game + Mic tracks): 137 s end to end on the GPU. Only 1 clip rendered
    (no candidate reached the minimum score; best 0.45) and it failed QC with 0.58 s of black frames.
  - Bugs found and fixed in the repo since: FFmpeg 9's 2-letter filter flag column made every filter look missing;
    cuBLAS DLLs from pip were not on the Windows search path while the doctor reported CUDA OK; Ctrl+Break was not
    treated as an interrupt; fonts needed fontconfig; the Mic stream on that recording is silent (about -91 dB) so
    transcription produced 0 words; black frames at clip edges were rendered and failed QC.
- **Owner's PC, re-run on commit 268cd94** (no PATH workarounds): doctor clean (cuBLAS loads, Arial found in
  C:\Windows\Fonts, all FFmpeg 9.1 filters, NVENC). Acceptance on Valorant: **12/12** in 63 s, including the real
  interrupt and resume; the recording's silent Mic stream was detected (-120 dBFS) and the mix transcribed (34 words,
  0 before). The 45.7-minute Overwatch session ran in 135 s (visual pass 106 s of that); its one clip still failed
  QC on 0.58 s of black, which turned out to be two in-game blackouts in the middle of the clip (also in the source).
  QC now reports source black as a warning instead of a failure (TD-26).
- **2-hour VOD** (synthetic, 64 concatenated copies of the 112 s sample, 4.2 GB, 1080p30): 25 min wall time on 4 vCPU for analysis + 3 rendered clips. Transcription 15.4 min (12 chunks, 2 reused from cache after the run was killed by a container restart), visual pass 7.3 min, candidates 30 s, each clip about 25 s render + QC. All 3 clips passed QC. Peak RSS 2.0 GB.
- Earlier verification still stands: synthetic stream, the owner's Overwatch/Valorant recordings, workstation driven with Playwright (cuts, split, move, platform chips, effects track: no console errors).

**Not tested yet:** a real GTA stream with a webcam, a real 2+ hour stream (the long test repeats the same 112 s), URL ingestion (video sites are blocked in the sandbox), and macOS. Windows and the GPU path were run on the owner's PC (above).

## Known issues

- Opening-hook scoring prefers starting on the punchline over including the setup sentence (the synthetic sample's best clip starts at "Oh my god" rather than "Wait, what is that?"). Phase 3 LLM judging is the planned fix; until then trim in review.
- In `facecam_split`, the facecam can still appear inside the gameplay crop if it sits near the crop window (no masking yet).
- Category labels are keyword-based and rough.
- When game and voice share one audio track, game announcer lines (Valorant's "Spike planted", "Last player standing") are transcribed and treated as speech. Use a separate mic track when recording, or set `transcription.speech_track`.
- Scores on real footage were all below the 0.55 threshold (best 0.53 on Valorant, 0.45 on a 45-minute Overwatch session): thresholds were tuned on synthetic speech and need calibration on real streams.
- On the GPU, the visual pass is about 77% of analysis time (it decodes on the CPU); hardware decode for that pass is the next speed-up.
- Black frames in the middle of a clip that are in the source (a death screen, a fade) give a QC warning, not a failure; only the edges are trimmed automatically.
- The default centre crop can cut off the crosshair/weapon area in shooters; `fit_blur` or a manual crop X is the workaround until tracking lands.
- Live 9:16 preview in the UI approximates libass text rendering.

## Dependencies

Core: Python ≥ 3.10, FFmpeg with libass + libx264, numpy, pydantic 2, PyYAML,
Pillow, faster-whisper (CTranslate2). UI: fastapi, uvicorn. Optional: yt-dlp.
Dev: pytest, httpx, espeak-ng (sample video synthesis).

## Next tasks

1. Run `scripts/acceptance.py --check-install` and `process` on real GTA streams on the owner's PC, then calibrate weights and `minimum_score` from those results.
2. Paste a real Twitch/YouTube URL on that PC to verify yt-dlp ingestion; chat download helpers.
3. Test facecam detection on a real webcam stream; per-segment layouts inside one clip; optional YOLO tracker input.
4. GPU run (NVENC, whisper large-v3-turbo fp16) and timings for the `quality` profile.
5. Live 9:16 preview: draw the hook title and the punch zoom (the render has them, the preview does not).
