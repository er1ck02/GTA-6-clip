# Free scheduling (v1, 2026-11-25)

Goal: one short batch session a day queues every approved clip for the next ~24 h, so posts go out on time from the platforms' own servers. The PC doesn't have to be on when a post goes live, and vidIQ's 3-a-day cap and credits stop mattering.

## Tools (all free, built into the platforms)

| Platform | Scheduler | Notes |
|---|---|---|
| YouTube Shorts | YouTube Studio upload → Visibility → **Schedule** (date + time) | Works with advanced features (enabled 2026-11-22). Upload limit lifted. Shorts posted as scheduled still count as Shorts (vertical, ≤3 min). |
| Instagram Reels | **Meta Business Suite** (business.facebook.com → Planner / Create reel → Schedule) | Needs @gta6clipsnn connected to Business Suite once (Erick's login step). Uploads full-size files, so the 10 MB Chrome cap goes away. |
| TikTok | TikTok Studio → upload → **Schedule** | Already works (up to 10 days ahead). |
| Facebook Reels | not used | Meta's 2026 rules demonetize reposted clips; revisit with original formats. |

## Daily batch (run once, ideally late morning MT)

1. Read `yt_data\approved\MAP.txt` and `POSTED.txt`. Take approved clips not yet posted on a platform, best first (payoff in the first second + big reaction).
2. Slot times per platform, 2-3 h apart, max 5-8/day, never two at the same time on one platform. Prefer 12-3 PM and 7-11 PM Eastern (10 AM-1 PM and 5-9 PM MT). Stagger platforms by ~15-30 min so one clip doesn't drop everywhere in the same minute.
3. Schedule each clip with the same title rules as always (`prompts/titles.md`), credit line, #gta6 + streamer tag, YouTube tags. Never "rp".
4. Write one line per clip per platform in `POSTED.txt` with `scheduled <UTC time>` and the link, so nothing is double-posted.
5. The daily stats logger (`yt_data\ops\stats_logger.py`, 9 AM) picks the posts up automatically once they are live.

## Rules that still apply

Only the Ricky Spanish channel and @gta6clipsnn / @gta6clips8126; never @wettoefungus. Don't touch the other Claude project's Chrome windows or processes. Never unpost/repost; take down only on a takedown or Erick's ask.
