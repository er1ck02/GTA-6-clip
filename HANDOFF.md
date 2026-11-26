# AI Clips / GTA-6-clip: handoff (2026-11-26)

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
- Production (recorder) is STOPPED (owner, 2026-11-26). Start: `.venv\Scripts\python.exe -m app.cli production start
  --no-browser`. Stop: `... production stop`. Status: `... production status`.
- Auto-upload is OFF: `~/.clipstudio/config.yaml` has `upload.allow_channels: []`. The old list of 11 streamers is in
  `config.yaml.bak-20261126`. Keep it empty.
- YouTube @gta6clipsnn (2026-11-25): 16 Shorts, 16.3K views, median 816, best 4.3K (tg door shot), 1 follower.
  Single-streamer payoff moments beat compilations. TikTok held back after burst posting: space posts out.
- Transcript library: ~2,000 `yt_data/projects/*/analysis/transcript.json`. Most source videos were deleted after
  editing, so a chosen moment must be re-pulled from the VOD (stream start + moment offset).

## Next
1. Match ChatGPT's moment types (Brain msg #32: NPC realism, animal surprise, hidden detail/Easter egg, streamer
   panic, vehicle chaos; search words listed there) against new transcripts daily.
2. First build: supertf airport-security moment (Nov 23; projects `m011-1255s-b20ec28aae` -> `m014-1613s-db078db09b`),
   re-pulled from the VOD and edited.
3. Merge the formats worktree's newer commits into the main line.

## Repo notes
- Untracked run artefacts (acceptance/long/yt run logs, acc_data*, long_data*, 17 GB yt_data/) are deliberately
  not committed.
