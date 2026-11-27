# AI Clips / GTA-6-clip: handoff (updated 2026-11-27)

Start here. Coordination happens in Universal Brain (dashboard http://127.0.0.1:4317, project `ai-clips`):
check its inbox first.

## Owner rules (2026-11-26)
- No raw clip re-posting. The recorder may capture streams, but every release is edited by hand first, and
  nothing goes up without the owner's OK.
- Only the original formats continue (weekly top 5, news Shorts): see the formats worktree
  `C:\Users\Erck8\GTA-6-clip-formats` and `docs/FORMATS.md` there.
- Follow trends and numbers: ChatGPT picks what to make (Brain task #14), Claude builds it.
- Narration is optional, used where it improves a video.

## State
- Production (recorder) is STOPPED (owner, 2026-11-26; still stopped 2026-11-27).
  Restart only when the owner says so (Brain task #21). `production status` lists where each channel resumes. Start: `.venv\Scripts\python.exe -m app.cli production start
  --no-browser`. Stop: `... production stop`. Status: `... production status`.
- Auto-upload is OFF: `~/.clipstudio/config.yaml` has `upload.allow_channels: []`. The old list of 11 streamers is in
  `config.yaml.bak-20261126`. Keep it empty.
- YouTube @gta6clipsnn (2026-11-25): 16 Shorts, 16.3K views, median 816, best 4.3K (tg door shot), 1 follower.
  Single-streamer payoff moments beat compilations. TikTok held back after burst posting: space posts out.
- Transcript library: ~2,000 `yt_data/projects/*/analysis/transcript.json`. Most source videos were deleted after
  editing, so a chosen moment must be re-pulled from the VOD (stream start + moment offset).
- 2026-11-27: four edited Shorts are built and waiting for the owner's review in `yt_data/releases/2026-11-27/`
  (mp4 + a .txt sidecar each: title, description with streamer credit and channel link, hashtags, source VOD
  timestamps). Nothing uploaded. All pass QC 26/26.
  1. `01_supertf_airport_security` (20 s): security gate pops his tyre, he takes an airport police truck, "We're
     in the airport!" (the handoff's first build).
  2. `02_supertf_steals_plane` (30 s): same airport session: "How do I steal a plane?", boards a flyUS jet, rolls it
     into the grass, a chat TTS says he's totaled every vehicle. Same source as 01: space them out or pick one.
  3. `03_xqc_scooter_granny` (30 s): mobility-scooter lady robs him mid ride-request, he chases her, then his
     rideshare app suspends him.
  4. `04_crayator_airport_carpet` (24 s): "the most realistic airport carpet... I can smell an airport."
- Clips now carry an on-screen streamer credit (`twitch.tv/<channel>`) under the title for the whole clip
  (`enhance.credit`, default on; `""` turns it off; the channel comes from the moment's `live/<channel>_...` path).
- Re-pulling a deleted moment: `scripts/repull_moment.py <project> [--before S --after S] [--dry-run]` finds the
  VOD that covers the recording and downloads the section into the session's `moments/` folder (so the channel is
  known). Then `process --input <file> --top 0 --set paths.data_dir=yt_data --set captions.enabled=true`, `review
  --clip <id> --start --end --cut A-B --title "..." --accept <id>`, `export --clips <id>` (same `--set`s).
  Without `--set paths.data_dir=yt_data` projects land in `data/`. Captions are off by default: pass
  `captions.enabled=true` for Shorts.

## Next
1. Owner review of the four Shorts in `yt_data/releases/2026-11-27/` (upload unlisted first, read YouTube's
   copyright check: in-car audio in 01). Post one a day at most; strong Speed clips first per the posting rule.
2. Unbuilt moments, best first: IShowSpeed bigfoot sighting (m018-1385s, Nov 22; VOD izXcd-bU72k, segment already
   pulled to `live/ishowspeed_20261122_103526/moments/r01_bigfoot_vod6760.mp4`) and Speed crocodile fight
   (m022-1736s, source still on disk): both already appear in the unlisted Speed top 10, so only as standalone
   Shorts if the owner wants that. Crayator ID check at the airport truck gate (r01-airport-vod27290, 120-130 s+,
   "that's how they check ID here?"). supertf "airport security shit seriously" line (r01-airport-vod10200 374-385 s)
   has no visual payoff (just driving with a wanted level). The selfie-stick NPC, dog rescue and long side quest
   types from ChatGPT's slate are not in the transcript library (Nov 21-26); search chat logs
   (`analysis/chat.json`) too, since chat names visual gags the streamer doesn't say out loud.
3. Match ChatGPT's moment types (Brain msg #32) against new transcripts daily.
4. Merge the formats worktree's newer commits into the main line (owner rules are on branch
   `claude/formats-owner-rules`, pushed 2026-11-26).

## Repo notes
- Untracked run artefacts (acceptance/long/yt run logs, acc_data*, long_data*, 17 GB yt_data/) are deliberately
  not committed.
