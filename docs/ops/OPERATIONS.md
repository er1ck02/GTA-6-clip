# Operations handbook

_v3 · 2026-11-25 · owner: operations thread ("Run the media business operations audit")_

The single source of truth for how the GTA 6 clips business runs. Every new agent/thread reads this first. Update it when a strategy changes; bump the version line and add a changelog entry at the bottom.

## 1. Mission and current objective

Grow a GTA 6 streamer-clips brand across short-form platforms and reach **YouTube Partner Program (YPP)** on the Ricky Spanish channel, then add revenue streams that don't hurt audience trust.

North-star metric (for now): **Shorts views in the trailing 90 days** on @gta6clipsnn, plus subscribers. Secondary: followers on Instagram and TikTok.

YPP gates (from YouTube's public help pages, verify before relying on them): full monetization = 1,000 subscribers + 10M public Shorts views in 90 days (or 4,000 long-form watch hours in 12 months). A lower fan-funding tier exists at 500 subscribers. **Country of residence is still unset on the channel; YPP can't be applied for until Erick sets it.**

**Reused-content risk (research thread, 2026-11-25):** raw streamer clips alone can't get the channel into YPP; YouTube rejects channels whose uploads are mostly reposted content. Original formats (a weekly top 5 with commentary, GTA 6 news / easter-egg Shorts) need to become most of the uploads. Erick chose both on 2026-11-25; built in `clipstudio/formats` (see `docs/FORMATS.md`, thread "Launch original GTA 6 formats").

## 2. Channels

| Platform | Account | Status (2026-11-25) | How posts go out | Objective |
|---|---|---|---|---|
| YouTube Shorts | Ricky Spanish, @gta6clipsnn (UCXFO0tCcgzXyZMZC3hKKI0Q) | 15 Shorts public, advanced features on, 1 montage unlisted (KbZO9y4iM5g) waiting for Erick | vidIQ scheduler (3 uploads/day while credits last) or YouTube Studio in Chrome | Retention + subscriber conversion toward YPP |
| Instagram Reels | @gta6clipsnn ("GTA 6 Clips") | ~12 Reels posted or scheduled | Chrome upload (10 MB cap, 1080x1920 30 fps copies in `yt_data\instagram\pending`), vidIQ scheduler | Shares + follows |
| TikTok | @gta6clips8126 | 18 videos, 0 followers, most at 0 views (held back after burst posting). Posting resumed 11-24 by the quality thread | TikTok Studio in Chrome (built-in scheduler works) | Rapid testing of hooks |
| YouTube (personal) | @wettoefungus | **Never post here** | n/a | n/a |

Hard rules (from Erick, all channels): never write "rp"/"role play"; credit the streamer on every post; no captions, facecam on top + full unzoomed gameplay below; casual lowercase title + one emoji; space posts 2-3 h per platform, 5-8/day max, never two at once; no duplicates, never unpost/repost; take down only on a takedown/objection or Erick's ask; English-only stand-in streamers; don't touch the other Claude project's processes or Chrome on the PC.

## 3. Architecture (what exists)

- **Clip Studio** (this repo, Python + FFmpeg, runs on Erick's PC with an RTX 4080): records up to 8 live streams, detects moments (audio, chat, emotes, visual events), edits 9:16 split-screen clips, QC. Production agent with On-Off toggle (`docs/PRODUCTION.md`). Data dir `C:\Users\Erck8\GTA-6-clip\yt_data`.
- **Supervisor** (quality thread on the PC): reviews edited clips and copies approved ones into `yt_data\approved` with a line in `MAP.txt` (title, hashtags, streamer, reason).
- **Posting record**: `yt_data\approved\POSTED.txt` (YouTube + Instagram), `yt_data\instagram\posted.log`. Check before posting anything.
- **Built-in YouTube API uploader** (PR #2) exists but is unused by choice (Erick: no API/quota setup).
- **Postiz** (self-hosted scheduler) half-installed: Docker needs WSL + reboot by Erick. Parked.
- **Research tools**: vidIQ and Virlo both at 0 credits (vidIQ refills 2026-12-16). No paid tool right now.

## 4. Active workstreams

| Workstream | Thread | Owns |
|---|---|---|
| Clip quality + daily posting | "Improve clip quality with a Fable audit" (PC) | Fix doubled facecam/chat, logo/banner on TikTok, run the posting schedule |
| Production pipeline | production threads (PRs #3-#18) | Recording lineup, stand-ins, toggle |
| Operations / strategy | this thread | This doc, backlog, experiments, metrics ledger |

## 5. What works (evidence)

From the public channel page on 2026-11-25 00:50Z (see `metrics.csv`):

- **Payoff in the first second + a big on-camera face reaction** wins: "tg shot through the door 😳" 4.3K, "gta 6 is too realistic for ludwig 😭" 2.4K, "speed found something in gta 6 😳" 1.1K.
- TypicalGamer and Ludwig clips lead; big-name streamers (Speed, xQc) get clicks on name alone.
- New style (split screen, lowercase title) beat nothing decisively yet: old-style titles did 949-1.6K. The difference is the moment, not the title.
- Shorts get a delayed test: uploads sit at 1-3 views for ~11 h, then either take off (~1K+) or die. Judge a Short after 48 h, not 12 h.

## 6. Failed experiments and lessons

| What | Result | Lesson |
|---|---|---|
| Burst uploads (several Shorts within minutes, 11-22) | 1-4 views each | Space posts; never two at once |
| Slow intros / payoff late in the clip | Flops (lacy 281, supertf not ready 115, xqc stealth 683) | Cut in 1-3 s before the trigger |
| TikTok desktop burst posting on a cold account | 0 views, 0 followers | TikTok needs warm-up and spacing; treat as separate business |
| Neon "Ricky Spanish" channel art | Erick rejected | Clean sci-fi look, no name on art |
| American Dad "Ricky Spanish" clips | Rejected before posting | Copyright/Content ID + YPP reused-content risk |
| Whop Content Rewards | Paused 11-21 | 30-min submit window doesn't fit the pipeline |

## 7. Metrics

Ledger: `docs/ops/metrics.csv` (one row per post per snapshot, real numbers only, source column says where each number came from). No analytics access beyond public counts right now: retention, swipe rate and subscriber-per-video need YouTube Studio Analytics (Chrome) or a working vidIQ.

Judge each Short at **48 h** and **7 d**. A clip "hits" at ≥1K views at 48 h (current channel median is below that).

## 8. Top 5 opportunities (ranked 2026-11-25)

Scored 1-5 (impact, confidence, effort where 5 = easy, cost where 5 = free).

| # | Opportunity | Impact | Conf. | Effort | Cost | Why |
|---|---|---|---|---|---|---|
| 1 | **Free metrics loop**: daily snapshot of every post's views/likes/comments via yt-dlp (public, no credits) + weekly YouTube Studio retention read; auto-fills `metrics.csv` | 5 | 5 | 4 | 5 | We can't learn without data and both paid tools are dry. Unblocks every other experiment |
| 2 | **First-second payoff gate in clip picking**: score each moment on "payoff ≤1 s from start" + face-reaction size; reject the rest; A/B a cold-open variant (payoff flash, then setup) | 5 | 4 | 3 | 5 | The single strongest pattern in our own data; today ~half of posts break it |
| 3 | **Free scheduling via YouTube Studio's scheduler + Meta Business Suite (Instagram only)**: one daily batch session schedules the day's Shorts and Reels, ends PC dependence and the vidIQ 3/day cap | 4 | 4 | 3 | 5 | Removes the throughput bottleneck and Erick's 24/7 PC complaint. Facebook Reels skipped: Meta's 2026 rules demonetize reposted clips |
| 4 | **YPP-safe long-form + recurring series**: "GTA 6 streamer moments of the day/week" ranked countdown with original framing (ranking, short typed context cards between clips) | 4 | 3 | 3 | 5 | Long-form watch hours are a second YPP path; added original value lowers "reused content" rejection risk, which is the biggest monetization risk for a clips channel |
| 5 | **TikTok rescue**: fix bio (it still says "RP"), warm the account (1-2 posts/day, in-app engagement), use TikTok's scheduler, test whether a fresh account outperforms | 3 | 3 | 4 | 5 | TikTok is 0 followers after 18 posts; cheapest platform to test hooks once it is out of the penalty box |

## 9. Backlog

**In progress:**
- Metrics loop (opp. 1, ops thread): `python -m clipstudio.ops.metrics snapshot|weekly|daily` (copy at `yt_data\ops\stats_logger.py`), appends public counts to `yt_data\ops\metrics.csv`, weekly winners/losers to `yt_data\ops\weekly\`. Being tested and scheduled daily on the PC.
- Payoff gate (opp. 2): v2 supervisor prompt handed to the clip quality thread.
- Monetization/platform research: research thread.

**Next 3 (highest value):**
1. Free scheduling (opp. 3): YouTube Studio scheduler for Shorts + Meta Business Suite for Instagram Reels; one daily batch session.
2. Original formats for YPP (opp. 4): built (`docs/FORMATS.md`). First top 5 (week of Nov 24) and first news Short (patch 1.002.002) go up UNLISTED for Erick; public only after his yes. Then: top 5 every week (Mon), news/easter-egg Shorts 2-4 a week, each fact sourced.
3. Feed the first weekly stats summary back into clip picking (streamer and payoff-timing patterns).

**Experiments queued:** original-format share: once approved, aim for originals to be most uploads; compare top 5 long-form watch time and news Short views against raw clips at 48 h / 7 d. Narrator voice A/B (Edge Brian vs Andrew) if retention is weak. Top 5 Shorts cut-downs only for moments not already posted as raw Shorts (no duplicates). cold-open vs straight cut (same moment, two days apart, YouTube only); title with vs without streamer name; post time 12-3 PM ET vs 7-11 PM ET.

**Long-term:** Facebook Reels only if original formats exist (reposted clips get demonetized there); original AI-assisted formats (GTA 6 map/easter-egg explainers using our own captures), second brand for another trending game, streamer clip partnerships (paid clipping deals), affiliate (gaming gear) once audience exists.

**Technical:** finish Postiz (needs Erick's WSL reboot) or drop it if Meta Business Suite covers IG/FB; Twitch client secret (stand-in discovery); dedupe `MAP.txt` (kreek line appears 3 times).

## 10. Needs Erick

- Set **country of residence** on Ricky Spanish (YPP blocker).
- Say yes/no to the unlisted montage KbZO9y4iM5g going public.
- Change TikTok bio in the phone app: remove "Best RP moments" (breaks the no-"rp" rule). Suggested: "Daily GTA 6 stream clips | Streamers always credited".
- Optional: New Twitch client secret.

## 11. Operating cycle

Daily (operations thread, short): observe (metrics snapshot + posting record) → pick top weakness → delegate or do → log result here. Weekly: review experiments against `metrics.csv`, rewrite prompts that underperform (bump version in `prompts/`), re-rank opportunities. Report to Erick as done / in progress / proposed / needs you, under 150 words.

## Changelog

- v3 (2026-11-25): original formats built (weekly top 5 + news Shorts), first episodes pending Erick's review.

- v2 (2026-11-25): stats logger in progress; Facebook Reels dropped from scheduling; reused-content risk for YPP; backlog refreshed.

- v1 (2026-11-25): first audit, top 5 opportunities, backlog, prompt registry.
