# Production: one command to start, one to stop

Clip Studio can run unattended for hours (several live streams, a shared editor, uploads). The production agent
starts all of it with one double-click and, when you want your PC back, winds it down without losing clips.

## Windows shortcuts

Run once to put them on the desktop:

```
.venv\Scripts\python.exe -m app.cli production shortcuts
```

| Shortcut | What happens |
|---|---|
| **Production On-Off** | One button, also in the Start menu. Runs a safe stop (every clip in flight finished or saved) if production is running, otherwise starts it. To pin it, right-click it in the Start menu and pick Pin to taskbar. Works without Claude. |
| **Start Production** | Quick checks (FFmpeg, stream recorder, disk space, YouTube sign-in), opens the server in its own window, resumes the channels you were recording last time, stands in for any that are offline, finishes clips the last stop left unedited, opens the workstation in your browser, and keeps Windows from sleeping. |
| **Stop Production** | Stops recording at once, finishes every saved moment, VOD and upload in flight, shows "please wait about N min" with a countdown, then closes the server. Your PC is free when it says so. |
| **Stop Production NOW** | Same, but finishes only the clip being edited right now. Everything else stays on disk and is edited on the next start. |
| **Production Status** | What is running, what is waiting, and what the next start will resume. |

Don't close the server window by hand: it works, but clips mid-edit are only finished on the next start.

## Commands (for scripts and Claude)

```
python -m app.cli production start [--channel URL ...] [--no-browser] [--port N] [--config/--profile/--set ...]
python -m app.cli production stop [--now] [--no-wait] [--keep-server]
python -m app.cli production status
python -m app.cli production shortcuts
```

All accept `--json`. Server settings given to `start` (`--config`, `--profile`, `--set`, `--port`) are remembered in
`~/.clipstudio/production.json`, so the shortcuts need no arguments. Defaults when nothing was given:
`configs/default.yaml` + `~/.clipstudio/config.yaml` (if present), profile `quality`, data in `yt_data/` when that
folder exists.

HTTP: `GET /api/production`, `POST /api/production/start {"channels": [...]}`,
`POST /api/production/stop {"mode": "safe"|"now"}`, `POST /api/production/shutdown`.

## How the lineup works

* **Lineup** = the channels production is meant to watch. It is saved in `<data_dir>/production/state.json`.
  Streams you paste in the workstation join it; streams you stop by hand leave it.
* **First start** with no lineup: `live.channels` from the config, else your top `production.default_streams`
  past streamers.
* **Resume**: each lineup channel is checked (one yt-dlp metadata request). A YouTube watch link becomes the
  channel's `/live` page, so a new broadcast is found.
* **Stand-ins**: if a channel is offline, the best streamer you've clipped before who is live now and whose stream
  title mentions `production.substitute_keywords` (GTA by default) takes the slot. "Best" means on your upload
  allowlist first, then the most finished clips. Every `production.recheck_minutes` the original is checked
  again; when it's back, the stand-in stops (its saved moments still get edited) and the original returns.
* **Always N streams** (`production.target_streams`, 8 by default; 0 turns it off): stand-ins only fill up to
  that many recordings, and if the lineup alone can't reach it, extra streams are added. When your past
  streamers run out, the top live streams of Twitch's `production.discover_twitch_category` directory
  ("Grand Theft Auto VI", most viewers first; see *Twitch category* below) are picked first, then the most-watched live stream among `production.discover_channels` on Twitch and a YouTube
  live search for `production.discover_youtube_search` ("GTA 6") whose title
  mentions `production.discover_keywords` (GTA 6) is picked. When an original comes back, its stand-in keeps
  recording as an extra unless that would go over the target; then the newest extra stops. An extra that goes
  offline is replaced on the next check. Stand-ins' clips post automatically with credit, like the lineup's
  (`production.post_stand_ins`; set it to false to hold them for review).
* **Twitch category** (one-time setup, free): go to https://dev.twitch.tv/console/apps, sign in, *Register
  Your Application* (any name, OAuth redirect `http://localhost`, category *Application Integration*, client
  type *Confidential*), then *Manage* → copy the Client ID and make a *New Secret*. Save both in
  `~/.clipstudio/twitch.json` as `{"client_id": "...", "client_secret": "..."}`. Production reads it on the next
  check; without it the category is skipped and the channel list + YouTube search are used.
* **Sleep / AFK streams**: every `production.recheck_minutes` the streams being recorded are checked again. A
  title or category matching `production.idle_keywords` (sleep stream, AFK, BRB, Just Chatting...) or a known
  category that isn't GTA counts as offline, and so does a picture that barely changes for two checks in a row
  (a still room: a sleep stream that kept its GTA title; `production.idle_motion_threshold`, then not tried
  again for `idle_cooldown_minutes`): the stream stops, a stand-in takes the slot, and the streamer
  comes back once they're playing again.
* **A stream that ends** mid-session is replaced the same way within a few seconds.

## Stopping safely

| In flight | Stop Production | Stop Production NOW |
|---|---|---|
| Recording | stops at once; the moment being captured is saved | same |
| Clip being edited | finished | finished |
| Moments waiting to edit | edited | saved, edited on next start |
| VOD analysis running | finished | finished |
| VOD analyses queued | run | saved, run on next start |
| Upload sending (built-in uploader) | finished | finished |

The wait estimate uses the measured average edit time of this session (about 23 s per moment on an RTX 4080),
divided by `live.max_parallel_edits`. After a crash or a closed window, the next start finds every moment still
marked saved/queued/processing (up to `production.backlog_max_age_hours` old) and edits it.

The supervisor (the server window) restarts the server if it crashes while production is running (up to 6 times
in 10 minutes) and re-engages the lineup.

## Auto-update

While production runs, the server window checks the checkout's branch every `production.update_check_minutes`
(10). When new commits fast-forward a clean checkout, their unit tests run first in a temporary git worktree.
If they pass, production stops quickly (the clip being edited finishes, waiting moments are saved), the
checkout fast-forwards, the server restarts on the new code and resumes the lineup and the saved moments.
Edited files or local commits in the checkout mean someone is working there, so nothing is touched. A commit
whose tests fail is skipped until a newer one arrives. Turn it off with `production.auto_update: false`.

## GTA 6 only

Only a stream that is playing GTA 6 is recorded. Anything else counts as offline and is swapped for a GTA 6 stand-in: another game, GTA V, IRL or Just Chatting, or a sleep stream. Twitch and Kick say which game is on. YouTube doesn't, so the same channel is checked on Twitch and Kick (simulcasts). If the game is still unknown, the title has to mention GTA (`production.require_gta_title`). A recording that has sent no new video for 3 minutes (`production.stall_seconds`) is stopped so its slot is refilled.
