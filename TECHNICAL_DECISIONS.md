# Technical decisions

Architecture decision log for GTA 6 Clip Studio. Each entry says what was
decided, why, and what it costs. Newest decisions are appended at the end.

---

## TD-01 · One Python package, thin `app/` entry point

**Decision.** All code lives in the `clipstudio/` package (sub-packages per
module). `app/cli.py` is a three-line shim so the documented
`python -m app.cli ...` commands work.

**Why.** The suggested layout put `app/` and `modules/` side by side as two
top-level packages. A top-level package called `modules` collides easily with
other installs and makes imports ambiguous. One namespaced package installs
cleanly (`pip install -e .`), keeps imports explicit, and avoids circular
dependencies because `pipeline/` is the only package that imports across
modules.

**Cost.** None beyond the folder name differing from the original sketch.

## TD-02 · FFmpeg is the only media engine

**Decision.** Probing, audio extraction, analysis filters (`ebur128`,
`blackdetect`, `silencedetect`, `astats`), compositing, caption burn-in
(libass via the `ass` filter) and encoding all go through FFmpeg. MoviePy,
Remotion and auto-subtitle are **not** used.

**Why.**
- One decode/encode path means one place for colour, timing and A/V-sync bugs.
- libass gives word-level highlight, outline, shadow, scale "pop" and exact
  pixel positioning, which covers every caption preset in the spec.
- Remotion would add a Node + Chromium render farm and a second compositor
  whose frame timing must be reconciled with FFmpeg's. It is only worth it for
  motion-graphics overlays, which are out of scope until Phase 5. If added
  later, it must render a transparent overlay that FFmpeg composites, not
  replace the pipeline.
- MoviePy re-implements trimming/compositing on top of FFmpeg with slower
  frame-by-frame Python processing; it adds nothing we need.
- auto-subtitle is a thin wrapper over openai-whisper + FFmpeg subtitles with
  no word timing; our caption engine supersedes it.

**Cost.** Filtergraphs are terse. They are generated in one place
(`rendering/render.py`) from a typed `CropPlan`, and are covered by
integration tests that decode the real output.

## TD-03 · faster-whisper is the default transcription backend; WhisperX is optional later

**Decision.** `transcription/base.py` defines a backend interface;
`faster_whisper_backend.py` is the only implementation today.

**Why.** faster-whisper (CTranslate2) runs int8 on CPU and fp16 on CUDA,
needs no PyTorch, ships its own Silero VAD, and already produces word
timestamps (cross-attention alignment) that measured within a few tens of
milliseconds on our tests. WhisperX adds wav2vec2 forced alignment and
pyannote diarization but pulls in PyTorch (~2 GB) and gated Hugging Face
models. It stays an optional backend for users who need diarization or
tighter alignment; it is not a second mandatory stage.

**Notes.**
- We decode audio once to 16 kHz mono WAV with FFmpeg and hand faster-whisper
  a NumPy array, instead of letting it decode with PyAV. That avoids a
  PyAV-version incompatibility we hit (`av` 19 vs faster-whisper 1.2.1) and
  lets loudness analysis reuse the same WAV.
- `condition_on_previous_text=False` reduces repetition loops on long, noisy
  gaming audio.

## TD-04 · Caption line breaking uses real font metrics

**Decision.** The caption engine resolves the configured font with fontconfig
(the same lookup libass does), measures text with Pillow at the size libass
will use, and wraps lines to the safe width before writing ASS.

**Why.** "Text must never leave the frame" cannot be guaranteed by character
counts. QC then *renders the captions alone with libass* and measures the
bounding box of the actual pixels against the safe area and protected regions
(e.g. the facecam panel). Both the intent and the result are checked.

**Cost.** Pillow is a dependency. Without fontconfig (stock Windows) widths
fall back to an estimate and a warning is logged; QC's pixel check still runs.

## TD-05 · Resumability through content-addressed stage keys

**Decision.** Each stage records a cache key = hash(stage version, upstream
keys, the config sections it reads, relevant inputs). A stage is skipped when
its key matches and its outputs exist. Job IDs are deterministic
(`<name>-<content fingerprint>`), so re-running `process` on the same file
reopens the same project.

**Why.** Changing caption style should re-render a clip in seconds, never
re-transcribe a 3-hour VOD. Tests assert exactly this: an interrupted render
resumes without re-transcribing, and a caption-preset change re-runs captions
and render while reusing the plan.

**Details.** Writes are atomic (temp file + rename). User decisions live in
`edits.json` as an overlay on generated candidates, so regenerating candidates
never loses accept/reject/trim decisions. Hand-edited caption tracks are
marked `edited` and preserved.

## TD-06 · Phase 2 highlight scoring is a transparent heuristic

**Decision.** Candidates are built from whole sentences (never mid-word),
scored with six named features (audio intensity, audio spike, reaction
language, speech energy, opening hook, completeness), combined with
user-configurable weights, then penalised for dead air and distance from the
target duration. Every candidate stores raw features, per-feature
contributions, a reason string and a confidence.

**Why.** The spec requires stored reasoning, adjustable weights and no
cloud dependency for basic clipping. Explainable features are also the right
input for the Phase 3 LLM pass.

**De-duplication.** Two candidates are duplicates if they overlap by more than
`duplicate_overlap` of the shorter clip **or** either contains the other's
peak moment. The second rule was added after the first real run produced two
clips about the same explosion that overlapped only 30%.

**Known limitation.** The opening-hook feature favours starting on the
punchline over including the setup line before it. Phase 3's LLM pass is the
intended fix (it judges "does this make sense without the stream?").

## TD-07 · Reframing produces a `CropPlan`; the renderer only executes plans

**Decision.** Layout logic (`reframing/layout.py`) outputs a typed plan:
panels (destination rectangles), crop keyframes in source pixels, protected
regions and the caption band. Rendering, the UI overlay and QC all consume the
same plan.

**Why.** Tracking (Phase 4) only has to emit more keyframes; rendering, UI and
QC do not change. Manual overrides (crop centre, facecam rectangle, layout)
are just edits that produce a different plan, and plans are saved so clips
re-render without repeating analysis.

**Layouts today.** `gameplay` (full-height 9:16 crop), `fit_blur` (whole frame
over a blurred fill: never cuts action), `facecam_split` (facecam panel on top,
gameplay below; needs a facecam rectangle until auto-detection exists).

## TD-08 · Audio: two-pass loudnorm only; Demucs is opt-in later

**Decision.** Each clip is measured, then normalised with linear `loudnorm` to
-14 LUFS / -1.5 dBTP. Optional "light" voice enhancement is a high-pass plus
gentle 2:1 compression. No denoising or source separation by default.

**Why.** Two-pass linear loudnorm changes gain without pumping. Aggressive
denoise and compression produce the robotic voice the spec forbids. Demucs
costs minutes per clip on CPU and needs PyTorch; it will be offered per clip,
with the original audio always kept as fallback.

## TD-09 · Local web UI: FastAPI + vanilla JS, served by the CLI

**Decision.** `python -m app.cli ui` starts a FastAPI server on 127.0.0.1 that
serves a no-build vanilla JS workstation and a JSON API over the same
`Studio` service the CLI uses. Live stage status streams over Server-Sent
Events from an in-process event bus.

**Why not Gradio/Streamlit.** The requested workstation (draggable crop box on
real frames, multitrack timeline with draggable caption blocks, live 9:16
canvas preview, live "clipping desk") needs custom interaction that Gradio and
Streamlit fight against. A plain web front end keeps it local-first and
offline, with no Node build step to install.

**Cost.** The 9:16 live preview is drawn by canvas code, which approximates
the libass render (same font, size, position and colours). The final render
and QC remain the source of truth. One reference project (gaming-clip) uses
the same code for preview and render; we may move to rendering preview frames
through FFmpeg if drift becomes a problem.

## TD-10 · Heavy and optional tools: evaluation summary

| Tool | Verdict | Reason |
|---|---|---|
| yt-dlp | **Use** (optional extra) | Mature, resumable `.part` downloads, metadata JSON. Users are responsible for platform terms. |
| Streamlink | Later (Phase 3+) | Live capture; integrate as a separate ingest that writes segments. |
| TwitchDownloader | Later, optional | .NET CLI. Use only for chat replay JSON (chat signal). Not cross-platform friendly; yt-dlp covers the video. |
| PySceneDetect | Phase 3 | Cheap scene-cut signal and clip boundary snapping. FFmpeg `scdet` is a zero-dependency alternative. |
| auto-editor | Not used | Silence removal conflicts with "quiet moments matter". We detect silence in scoring and QC instead of cutting it. |
| FunClip | Not used | ModelScope/FunASR stack (Paraformer, PyTorch), Chinese-first, LLM clipping prompts. Overlaps our transcript + LLM design with a much heavier footprint. |
| ClipsAI | Not used | Speaker-focused podcast reframing with pyannote; its assumptions don't fit gameplay. Ideas reused, code not. |
| MediaPipe | Phase 4 | Face detection for facecam localisation and reaction cues. Light, CPU-friendly. |
| Ultralytics YOLO | Phase 4, optional | Gameplay subject tracking. **AGPL-3.0**: kept as an optional plugin so the core stays MIT. |
| SAM 2 | Optional, last | Heavy (PyTorch, GPU). Only if box tracking proves insufficient. |
| WhisperX | Optional backend | See TD-03. |
| Remotion | Not used now | See TD-02. |
| MoviePy | Not used | See TD-02. |
| Demucs | Optional, per clip | See TD-08. |
| Video2X | Optional, per clip | Only for low-resolution sources; Vulkan binary, large. |
| Practical-RIFE | Optional, per moment | Slow-motion on selected beats only; PyTorch. |
| LosslessCut | Not used | A GUI app, not a library. Lossless cuts cannot carry burned-in captions or reframing; FFmpeg stream copy covers raw exports if needed. |

## TD-11 · Licences of reference projects

The user pointed at four reference projects. They were studied for ideas and
workflows only; **no code was copied**:

- `iyansr/gaming-clip` and `alokbaraii/streamclipper` are AGPL-3.0;
  copying code would force this project's licence.
- `Claggeta/clipper` showed no licence file (all rights reserved by default).
- `haziqahmedqasmi/PeakCut` is MIT.

Ideas adopted are listed in `docs/REFERENCE_REPOS.md`.

## TD-12 · Sandbox model sourcing (development only)

The cloud development sandbox blocks Hugging Face and GitHub downloads, so the
standard `small` model could not be fetched there. For testing only, a
CTranslate2 int8 conversion of Whisper small was taken from an npm package
(`@skillninja/shortmovie-telop-model-*`), reassembled, and checked against the
package's published SHA-256. It is a weights file (no code). End users get
models the normal way: faster-whisper downloads them from Hugging Face on
first use, or `transcription.model` can point at any local CT2 model folder.

## TD-13 · Multi-track audio: transcribe the mic, mix everything else

**Decision.** `MediaInfo` lists every audio stream. Transcription reads a
separate `speech.wav` built from the stream titled Mic/Voice when one exists
(`transcription.speech_track: auto`), levelled with `dynaudnorm` for
recognition only. Loudness analysis and the rendered clip use the sum of all
streams (`audio.tracks: all`).

**Why.** The first test on the user's own Overwatch recording (SteelSeries
Moments) found nothing: track 0 was game audio and the commentary lived on a
quiet second "Mic" track. OBS, SteelSeries and Medal all record this way.
Transcribing the clean mic track also keeps game announcer lines out of the
transcript.

## TD-14 · Browser preview proxy for non-web codecs

**Decision.** When the source codec is not H.264/VP8/VP9/AV1 (typically HEVC
from Medal or NVIDIA recorders), the server builds a 720p H.264 proxy
(`analysis/preview.mp4`, keyframe every 30 frames for scrubbing) after analysis
or on first request. The workstation plays the proxy and maps crop coordinates
by the resolution ratio. Rendering and QC always read the original file.

**Why.** Chromium cannot decode HEVC without platform support, so the program
view was black on the user's Valorant clip. Transcoding the whole source for
rendering would lose quality; a proxy is only for viewing.

## TD-15 · Candidates from audio peaks as well as speech

**Decision.** Besides sentence windows, every loud local maximum (≥ 6 dB above
the median, ≥ 5 s apart) seeds a window with the peak at 40 % of the target
length; edges are moved so they never cut a sentence. Sentence windows shorter
than the minimum are padded with surrounding context instead of being dropped.
All candidates go through the same scoring and de-duplication.

**Why.** Real gameplay recordings have long stretches without commentary. A
speech-only builder produced zero candidates on a 60 s Overwatch clip whose
best moment was a two-word reaction.


## TD-16 · Facecam detection: face + persistent border, MediaPipe first

**Decision.** Sample 24 frames spread over the whole source. Detect faces with
MediaPipe Tasks BlazeFace (short range, run on the full frame and on 2x2/3x3
tiles because a facecam face is small), falling back to OpenCV's Haar cascade.
Cluster faces by position; a face in the same place in at least 30 % of
samples is a facecam candidate. Then look outward from the face for the
overlay's border, using edges that are present in the same place **with the
same polarity** in most samples (game texture flips polarity at random; an
overlay border does not). Confidence combines face hit rate and border
strength; only ≥ 0.6 is auto-applied. Border-only panels (no face) score ≤ 0.4.
Borderless faces need ≥ 80 % hit rate to reach the threshold.

**Why.** The face is what separates a webcam from HUD panels (minimap, kill
feed, sponsor box), which are also static bordered rectangles. On the user's
Valorant clip a first-person gloved hand was detected as a face in 46 % of
samples, with no border: the confidence rules keep it at 0.50, below the
threshold. MediaPipe is Apache-2.0, CPU-fast and its model is 230 KB; YOLO
(AGPL, weights blocked in the sandbox) and SAM 2 (heavy) are not needed for this.

**Alternatives.** Template matching on common overlay frames (too many
styles), training a detector (no data), user-only rectangles (kept as the
override).

## TD-17 · Gameplay tracking: motion window + offline smoothing

**Decision.** Sample the clip at 4 fps (320 px wide), difference consecutive
frames, sum changed pixels per column (facecam masked out) and take the
crop-wide window with the most motion. The crop moves toward it in proportion
to how concentrated the motion is; with frame-wide motion it holds the
default centre. The path is median-filtered, Gaussian-smoothed in both time
directions, passed through a deadzone and a two-way speed limit (0.3 source
widths/s), then simplified (Ramer-Douglas-Peucker, ≤ 48 keyframes). The
renderer animates the FFmpeg `crop` x/y with a piecewise-linear expression in
`t`, snapped to even pixels; the UI interpolates with the same rule.

**Why.** It is cheap (about 1 s per 20 s clip on 4 vCPU), needs no model, works
for any game and never jitters: QC checks pan speed and direction reversals on
every render. Object detectors (YOLO) would know *what* moved but are AGPL,
need weights and still need the same smoothing; they can be added later as an
alternative source of the raw path.

## TD-18 · Facecam in split/PiP layouts: cover crop and border clearance

**Decision.** The facecam panel uses a *cover* crop inside the detected box
(inset 2 % to drop border pixels), centred on the face, so no gameplay leaks
into the camera panel. In split and PiP layouts the gameplay crop is shifted
off the facecam overlay (plus a 1 % margin for its border) when that costs at
most `reframing.avoid_facecam_shift` (12 % of the width); otherwise it is left
and the overlap is accepted.


## TD-19 · Visual highlight signals from one cheap pass, OCR only at events

**Decision.** Decode the whole source once at 4 fps, 128 px wide (streamed, never
held in memory) and compute brightness, saturation, motion and a 48-bin colour
histogram change per sample. Scene cuts are histogram jumps ≥ 0.35 and 3x the
local median; flashes are brief brightness jumps; "desaturation" is colour
draining to < 45 % of the minute's median for ≥ 1.2 s. Tesseract OCR (optional)
runs only at those event moments, on the centre band, using a saturation mask
so coloured HUD text on a grey death screen is legible.

**Why.** GTA's WASTED/BUSTED and most games' death or kill-cam screens drain
colour; explosions flash. These statistics need no model, no game-specific
templates (GTA 6's exact HUD is unknown to us) and cost one decode. OCR on every
frame would be far too slow; at events it is a handful of calls. FFmpeg's
`scdet` and PySceneDetect were considered; computing cuts in the same pass
avoids a second decode of multi-hour files.

## TD-20 · Chat replay as a signal, imported not downloaded

**Decision.** Read chat exports the user already has (TwitchDownloader JSON,
chat-downloader JSON, yt-dlp YouTube live chat, CSV), found automatically next
to the video. Score = lift of the busiest 5 s of chat over the stream's median
rate (looking 4 s past the clip end because chat lags), plus the share of hype
keywords. Chat spikes also seed candidate windows, shifted 4 s earlier.

**Why.** Chat is the strongest crowd signal for Twitch VODs, but downloading it
means platform APIs and terms the user should control. Importing files keeps
the studio offline and lets any tool produce them.

## TD-21 · LLM as an optional re-ranker with a provider chain

**Decision.** The heuristic stays the default and defines clip boundaries. When
`llm.enabled`, the top-k candidates (transcript, timing, signal summary) go to
the first provider in `llm.providers` that returns a valid answer: Claude via
the official `anthropic` SDK (JSON-schema structured output, server-side refusal
fallback), any OpenAI-compatible endpoint, or local Ollama. Answers are
validated with pydantic; unknown ids are ignored, unjudged candidates keep
their score; final = (1 - w) heuristic + w LLM. Any failure falls back to the
heuristic order with the reason logged.

**Why.** The reference projects found LLM-only picking worse than heuristics on
long videos (clipper) and valued provider fallbacks (PeakCut). Re-ranking the
shortlist uses the model where it adds the most (context, humour, "does this
make sense alone") at a bounded cost, and keeps the studio fully usable
offline.
