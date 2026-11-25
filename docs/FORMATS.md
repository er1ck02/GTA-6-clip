# Original formats (YPP)

YouTube keeps channels of raw streamer clips out of the Partner Program ("reused content"). Its policy allows
"edited footage from other creators where you add a storyline and commentary". These two formats add our own
ranking, script and narration. Code: `clipstudio/formats/`.

| Format | Output | Cadence |
|---|---|---|
| Weekly top 5 GTA 6 streamer moments | 16:9 long-form (aim 4-8 min) + one vertical Short per moment | weekly |
| GTA 6 news / easter-egg Short | 9:16, 30-60 s, every fact sourced on screen and in the description | 2-4 a week |

## Rules the build enforces
- Never "rp" / "role play" anywhere (title, script, tags, description).
- Every clip credits its streamer: on screen for the whole clip, in the end credits, in the description.
- Every news beat that states a fact cites a source (publication, date, URL); otherwise the build stops.
  Hedged reports (players say, reportedly) must be said hedged in the script: the writer's job.
- No TODO left in the script; tags on every upload (within YouTube's 500 characters).
- `upload.json` says visibility `unlisted`: the first video of each format goes up unlisted for Erick's review.
  Change to public only after his yes. Upload only to Ricky Spanish (@gta6clipsnn), never @wettoefungus.
- Titles must describe what is in the video. Checked by a person.

## Voice
Free neural TTS, no account: `edge` (Microsoft Edge read-aloud voices via `edge-tts`, needs internet;
default `en-US-BrianMultilingualNeural`, +6% rate), falling back to `kokoro` (open-weight Kokoro-82M, offline,
needs PyTorch). Audition: `python -m clipstudio.cli formats voice "line" --out test.wav --edge-voice en-US-AndrewMultilingualNeural`.
Write the script to be spoken: short sentences, contractions, a joke per moment, no lists of numbers.

## Weekly top 5
1. `python -m clipstudio.cli formats draft --approved yt_data\approved --out yt_data\formats\top5_wNN.json`
   picks the newest approved clips and leaves the script as TODO.
2. A writer (Claude thread or Erick) watches the clips, sets ranks, `in`/`out`, and writes `intro`, each
   moment's `setup` (what's going on, why it matters) and `react` (our take after the payoff), and `outro`.
   A running storyline across the five (a theme for the week) is what makes it commentary, not a compilation.
3. `python -m clipstudio.cli formats top5 yt_data\formats\top5_wNN.json --out yt_data\formats\top5_wNN`
   (add `"encoder": "h264_nvenc"` in the spec on the RTX PC).
4. Review `episode.mp4`, `thumbnail.jpg`, `upload.json`; Shorts in `shorts/` with their own upload files.

Episode structure: title card + intro narration -> for each rank 5..1: rank card with setup narration over the
blurred clip, the clip in widescreen (gameplay full frame, facecam picture-in-picture, credit top-left), a
freeze-frame with our reaction -> credits card with outro. Chapters go in the description.

Shorts cut-down: the clip starts on the action (payoff-first rule) with a "#N this week's top 5" badge in the
band under the gameplay, then our reaction over the last frame and "full top 5 on the channel".

## News / easter-egg Short
Spec: `title`, `kicker`, `sources` {id: {name, url}}, `footage` [{streamer, link}], `clip`, `beats`
[{say, show, sources, clip?, in?}] (`"claim": false` for lines that state no fact, like the follow line).
`python -m clipstudio.cli formats news SPEC.json --out yt_data\formats\news\SLUG`.
Layout: on-screen text on top, gameplay unzoomed in a window, source line under it. The streamer's mic is
muted; only our narration plays. Pick footage that matches what the line says.

Episode specs live in `docs/formats/episodes/` (first ones) and `yt_data\formats\` on the PC.
