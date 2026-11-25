# Supervisor clip approval

## v1 (live, reconstructed 2026-11-25 from MAP.txt and research/clip-style-spec.md)

Approve a clip into `yt_data\approved` only if all are true:
1. Something visibly happens in the game (crash, kill, fail, glitch, NPC chaos, win, easter egg) or the streamer says something quotable.
2. The streamer reacts out loud within 2 s.
3. Facecam is visible on top, gameplay full and unzoomed below, no captions, no doubled chat/facecam.
4. Clip is 12-30 s (45 s max only if the buildup is funny) and includes the setup.
5. Nothing hateful, no slur jokes, no "rp"/"role play" anywhere.
Write one MAP.txt line: file | title | hashtags | channel | one-sentence reason.

## v2 (proposed 2026-11-25, not live)

Change: add a hard **first-second payoff** rule and a reaction-size score.
Why: our top 3 Shorts (4.3K, 2.4K, 1.1K) all show the action or a loud reaction in the first second; payoff-late clips (115-683 views) flop.
Add to v1:
6. The first frame already has motion or speech, and the payoff (or a visible lead-in to it) lands by 2 s. If not, re-cut to start 1-3 s before the trigger; if it can't be, reject.
7. Score reaction 1-3 (1 = small smile, 3 = scream/jumps back/hands on head). Approve 1s only for an exceptional moment.
8. Add to the MAP line: `payoff_s=<seconds from start>` and `reaction=<1-3>` so analytics can correlate them with views.
Success metric: share of approved clips reaching 1K views at 48 h rises above the current ~3 of 10.
