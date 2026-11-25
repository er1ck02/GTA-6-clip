# Architecture

## Pipeline, source to export

```
                        ┌──────────────────────── analysis (once per source, cached) ────────────────────────┐
 local file ──┐         │                                                                                     │
              ├─ ingest ─┼─▶ metadata ─▶ extract_audio ─┬─▶ transcribe ───────────┐                          │
 URL (yt-dlp)─┘ validate │   ffprobe     FFmpeg 16k WAV │   faster-whisper words   ├─▶ candidates ─▶ edits ─┐ │
                         │                              └─▶ loudness (RMS dB) ─────┘   score+dedupe   overlay│ │
                         └─────────────────────────────────────────────────────────────────────────────────┼─┘
                                                                                                           │
                        ┌────────────────────────── per clip (cached per clip) ─────────────────────────────┘
                        │
                        ├─▶ plan ─────────▶ captions ─────────▶ render ─────────────────────▶ qc ─▶ export
                        │   CropPlan        word chunks          one FFmpeg pass:              probe, full decode,
                        │   (layout, crop   font-measured        trim → crop/composite →       black/flat frames,
                        │   keyframes,      ASS + SRT            ASS burn-in → 2-pass          LUFS/peak/silence,
                        │   caption band)                        loudnorm → H.264/AAC          caption pixels vs
                        │                                                                      safe area & face
```

Each box is a **stage** run by `pipeline/project.py::run_stage`. A stage has a
cache key built from its version, upstream keys and the config sections it
reads; it records status, timing, error and outputs in `project.json` (analysis)
or `clips/<id>/state.json` (per clip), and publishes `stage` events.

Phase 3 adds scene, chat and LLM signals before `candidates`. Phase 4 adds
facecam detection and subject tracking before `plan` (more keyframes in the
same `CropPlan`). Phase 5 adds optional audio and visual enhancement stages
between `captions` and `render`.

## Components

```
            ┌───────────────┐      ┌────────────────────────────┐
  CLI ─────▶│               │      │ FastAPI server (server/)   │◀── workstation UI (static/, vanilla JS)
 (cli.py)   │ Studio        │◀─────│  REST: projects, moments,  │      Program/Source · Media Bin ·
            │ (pipeline/    │      │  preview, captions, render │      Inspector · 9:16 Output ·
            │  studio.py)   │      │  SSE: /api/events ─────────┼──▶   Timeline · Clipping Desk
            └──────┬────────┘      │  JobRunner (1 worker)      │
                   │               └────────────────────────────┘
     ┌─────────────┼───────────────┬──────────────┬──────────────┬──────────────┐
 ingestion/   transcription/   audio_analysis/  highlights/   reframing/    captions/   rendering/   quality_control/
 sources.py   base.py +        loudness.py      candidates.py layout.py     engine.py   render.py    checks.py
              faster_whisper
                   │
              media/ffmpeg.py  (the only module that shells out to ffmpeg/ffprobe)
              core/  models.py (typed JSON) · events.py · logging.py · errors.py · config.py
```

Rules: modules depend on `core/` and `media/` only; only `pipeline/` composes
modules; the server and CLI only talk to `Studio`. No module imports the UI.

## On-disk project layout

```
data/projects/<job_id>/
  project.json            source + fingerprint, config snapshot, stage records, requested clips
  edits.json              accept/reject, trims, layout/crop/facecam overrides, caption preset
  analysis/media.json     ffprobe summary
  analysis/audio.wav      16 kHz mono, shared by transcription and loudness
  analysis/transcript.json  segments + words (start, end, probability)
  analysis/loudness.json  RMS dB per 0.5 s
  candidates.json         ranked candidates with features, contributions, reason
  clips/<clip_id>/
    state.json  cropplan.json  captions.json  captions.ass  captions.srt
    <job_id>_<clip_id>.mp4  qc.json
  project.log.jsonl       structured log
```

## Key data types (`clipstudio/core/models.py`)

`MediaInfo`, `Transcript{segments[words]}`, `LoudnessTrack`, `Candidate`
(start/end/peak_time/score/features/contributions/reason/confidence/status),
`CropPlan` (panels, keyframes, protected regions, caption band),
`CaptionTrack` (chunks of lines of words, clip-relative times, layout),
`QCReport` (checks with pass/warn/fail/skip).
