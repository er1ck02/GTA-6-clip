# Reference projects studied

Read on 2026-11-21 from each project's README and repository page. Studied for
workflow and design ideas only. No code was copied (see TECHNICAL_DECISIONS TD-11
for licences).

| Project | Stack | Licence | What it confirmed / what we take from it |
|---|---|---|---|
| Claggeta/clipper | Python, uv, FFmpeg, faster-whisper, optional Ollama scorer | none shown | Same core path as ours (FFmpeg audio → faster-whisper words → windowed transcript scoring → JSON → render). Facecam box found as "a still face inside a bordered box", stacked over a gameplay crop. Its LLM scorer was *not* better than its heuristic on long videos, which supports keeping our heuristic as the default and using an LLM as a re-ranker. Keeps ADRs per decision. |
| iyansr/gaming-clip | Bun/TypeScript, whisper.cpp, FFmpeg | AGPL-3.0 | Layouts split / fill / fit / custom match ours (facecam_split / gameplay / fit_blur; custom is a future addition). Webcam auto-detect plus a visual rectangle editor. Live preview drawn by the same code as the render (WYSIWYG). Long audio transcribed in chunks cut at pauses and cached per chunk so interrupted runs resume mid-transcription: adopt for multi-hour VODs (Phase 3/7). Every UI action also exists in the CLI with `--json`. |
| haziqahmedqasmi/PeakCut | Python server + web dashboard | MIT | Provider abstraction covering Gemini, OpenAI, Grok, Groq, OpenRouter, Claude, Ollama and any OpenAI-compatible URL, with comma-separated model fallbacks, and loudness-only picking when no key is set. Matches the Phase 3 LLM provider design. Live clipping from a rolling buffer using loudness, chat and transcript. |
| alokbaraii/streamclipper | Python, MediaPipe, Colab | AGPL-3.0 | "Setup, payoff, reaction, hook in the first 2 seconds" as the clip shape. GTA HUD recognition (WASTED, BUSTED, mission screens, wanted stars) as a visual score signal: a strong, cheap Phase 3 signal for this project's target content. Reframe editor that splits a clip into parts, each with its own layout and keyframed pan, which maps onto our `CropPlan` keyframes. Has a `doctor` command too. |

## Adopted into the roadmap

1. Phase 3: GTA HUD text detection (OCR or template match on sampled frames) as a visual signal.
2. Phase 3: chunked, resumable transcription for multi-hour VODs.
3. Phase 3: LLM re-ranker with a provider list and fallbacks; heuristic stays the default.
4. Phase 4: facecam auto-detection (bordered static box + face inside), with the manual rectangle editor as override.
5. Phase 4/6: per-segment layouts within one clip (split timeline into parts).
6. Phase 6: `--json` output for every CLI command so agents can drive the studio.
