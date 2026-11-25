"""Free daily stats logger + weekly winners/losers summary for the clips channels.

Public numbers only (yt-dlp metadata requests, nothing downloaded, no paid API, no login unless a cookies
file is given). Every row says where it came from; a platform that can't be read is logged as an error row,
never guessed.

    python -m clipstudio.ops.metrics snapshot [--data-dir yt_data]     # append today's counts
    python -m clipstudio.ops.metrics weekly   [--data-dir yt_data]     # write ops/weekly/<date>.md
    python -m clipstudio.ops.metrics daily    [--data-dir yt_data]     # snapshot, + weekly on Mondays

Output: <data_dir>/ops/metrics.csv (append-only) and <data_dir>/ops/weekly/YYYY-MM-DD.md.
Channels and optional cookies files live in <data_dir>/ops/channels.json (defaults below).
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_CHANNELS = {
    "youtube": {"urls": ["https://www.youtube.com/@gta6clipsnn/shorts", "https://www.youtube.com/@gta6clipsnn/videos"]},
    "tiktok": {"urls": ["https://www.tiktok.com/@gta6clips8126"]},
    "instagram": {"urls": [], "posted_logs": ["instagram/posted.log", "approved/POSTED.txt"]},
}
# never read or log the personal channel
BLOCKED = ("wettoefungus",)

STREAMERS = {  # title word as fans write it -> streamer
    "tg": "typicalgamer", "typicalgamer": "typicalgamer", "ludwig": "ludwig", "speed": "ishowspeed",
    "ishowspeed": "ishowspeed", "xqc": "xqc", "lacy": "lacy", "supertf": "supertf", "kreek": "kreek",
    "crayator": "crayator", "stableronaldo": "stableronaldo", "kai": "kaicenat", "kaicenat": "kaicenat",
    "caseoh": "caseoh", "jynxzi": "jynxzi", "burntpeanut": "burntpeanut",
}
HIT_VIEWS = 1000          # a Short "hits" at >= 1K views at 48 h (docs/ops/OPERATIONS.md section 7)
SOURCE = "yt-dlp public"


@dataclass
class Row:
    snapshot_utc: str
    platform: str
    video_id: str            # "_channel" for the channel's follower row
    url: str = ""
    title: str = ""
    streamer: str = ""
    posted_utc: str = ""
    age_h: str = ""
    duration_s: str = ""
    views: str = ""
    likes: str = ""
    comments: str = ""
    followers: str = ""
    source: str = SOURCE
    error: str = ""


COLUMNS = [f.name for f in fields(Row)]
Fetch = Callable[[list[str]], tuple[int, str, str]]   # args -> (returncode, stdout, stderr)


# ------------------------------------------------------------------ yt-dlp
def _ytdlp() -> list[str]:
    try:
        from clipstudio.live.buffer import _ytdlp_cmd
        return _ytdlp_cmd()
    except Exception:  # noqa: BLE001
        return [sys.executable, "-m", "yt_dlp"]


def run_ytdlp(args: list[str], timeout: float = 120) -> tuple[int, str, str]:
    try:
        cp = subprocess.run(_ytdlp() + ["--quiet", "--no-warnings", "--skip-download"] + args,
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                            check=False)
        return cp.returncode, cp.stdout or "", cp.stderr or ""
    except subprocess.TimeoutExpired:
        return 1, "", "timed out"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def _err(stderr: str, code: int) -> str:
    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    return (lines[-1] if lines else f"yt-dlp exit {code}")[-300:]


def _num(v) -> str:
    return str(int(v)) if isinstance(v, (int, float)) else ""


def guess_streamer(title: str) -> str:
    for w in re.findall(r"[a-z0-9]+", (title or "").lower()):
        if w in STREAMERS:
            return STREAMERS[w]
    return ""


def _posted(info: dict) -> datetime | None:
    ts = info.get("timestamp") or info.get("release_timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    d = info.get("upload_date")
    if isinstance(d, str) and len(d) == 8 and d.isdigit():
        return datetime.strptime(d, "%Y%m%d").replace(tzinfo=timezone.utc)
    return None


def row_from_info(info: dict, platform: str, now: datetime) -> Row:
    posted = _posted(info)
    return Row(snapshot_utc=_iso(now), platform=platform, video_id=str(info.get("id") or ""),
               url=info.get("webpage_url") or info.get("url") or "", title=(info.get("title") or "").strip(),
               streamer=guess_streamer(info.get("title") or info.get("description") or ""),
               posted_utc=_iso(posted) if posted else "",
               age_h=f"{(now - posted).total_seconds() / 3600:.1f}" if posted else "",
               duration_s=_num(info.get("duration")), views=_num(info.get("view_count")),
               likes=_num(info.get("like_count")), comments=_num(info.get("comment_count")))


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def list_videos(url: str, fetch: Fetch, cookie_args: list[str]) -> tuple[list[str], dict, str]:
    """Video URLs on a channel/profile page (flat, one request) + the playlist info + error text.

    ``info["_by_url"]`` maps each URL to its flat entry: TikTok's profile listing carries view/like counts
    while its per-video page often fails, so those counts are used when the video page can't be read."""
    code, out, err = fetch(["--flat-playlist", "-J"] + cookie_args + [url])
    if code != 0 or not out.strip():
        return [], {}, _err(err, code)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return [], {}, "could not parse yt-dlp output"
    urls, by_url = [], {}
    for e in data.get("entries") or []:
        u = e.get("url") or e.get("webpage_url") or ""
        if u and not u.startswith("http") and "youtube" in url:
            u = f"https://www.youtube.com/shorts/{u}" if "/shorts" in url else f"https://www.youtube.com/watch?v={u}"
        if u:
            urls.append(u)
            by_url[u] = e
    data["_by_url"] = by_url
    return urls, data, ""


def video_info(url: str, fetch: Fetch, cookie_args: list[str]) -> tuple[dict, str]:
    code, out, err = fetch(["-j", "--no-playlist"] + cookie_args + [url])
    if code != 0 or not out.strip():
        return {}, _err(err, code)
    try:
        return json.loads(out.strip().splitlines()[0]), ""
    except json.JSONDecodeError:
        return {}, "could not parse yt-dlp output"


INSTAGRAM_URL = re.compile(r"https://www\.instagram\.com/[^\s|]*?/?(?:reel|p)/[A-Za-z0-9_-]+/?")


def instagram_urls(data_dir: Path, logs: Iterable[str]) -> list[str]:
    found: list[str] = []
    for rel in logs:
        p = data_dir / rel
        if p.exists():
            for m in INSTAGRAM_URL.findall(p.read_text(encoding="utf-8-sig", errors="replace")):
                if m not in found:
                    found.append(m)
    return found


def instagram_captions(data_dir: Path, logs: Iterable[str]) -> dict[str, str]:
    """Reel URL -> our caption, from posted.log lines ``time | file | caption | url``."""
    caps: dict[str, str] = {}
    for u in instagram_urls(data_dir, logs):
        caps[u] = ""
    for rel in logs:
        p = data_dir / rel
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            parts = [x.strip() for x in line.split("|")]
            m = INSTAGRAM_URL.search(line)
            if m and len(parts) >= 4 and not caps.get(m.group(0)):
                caps[m.group(0)] = parts[2]
    return caps


def load_channels(data_dir: Path) -> dict:
    p = data_dir / "ops" / "channels.json"
    if p.exists():
        try:
            return {**DEFAULT_CHANNELS, **json.loads(p.read_text(encoding="utf-8"))}
        except json.JSONDecodeError:
            pass
    return DEFAULT_CHANNELS


def snapshot(data_dir: Path, fetch: Fetch = run_ytdlp, now: datetime | None = None) -> list[Row]:
    now = now or datetime.now(timezone.utc)
    rows: list[Row] = []
    for platform, cfg in load_channels(data_dir).items():
        cookies = cfg.get("cookies") or ""
        cookie_args = ["--cookies", cookies] if cookies else []
        urls: list[str] = []
        flat: dict[str, dict] = {}
        captions: dict[str, str] = {}
        followers = ""
        for page in cfg.get("urls") or []:
            if any(b in page.lower() for b in BLOCKED):
                continue
            found, info, err = list_videos(page, fetch, cookie_args)
            if err:
                rows.append(Row(_iso(now), platform, "_page", url=page, error=err))
            followers = followers or _num(info.get("channel_follower_count"))
            flat.update(info.get("_by_url") or {})
            urls += [u for u in found if u not in urls]
        if platform == "instagram":
            captions = instagram_captions(data_dir, cfg.get("posted_logs") or [])
            urls += [u for u in captions if u not in urls]
        for u in urls:
            if any(b in u.lower() for b in BLOCKED):
                continue
            info, err = video_info(u, fetch, cookie_args)
            if err:
                entry = flat.get(u) or {}
                if entry.get("view_count") is not None:     # profile listing still has the counts
                    r = row_from_info({"webpage_url": u, **entry}, platform, now)
                    r.source = SOURCE + " (profile listing)"
                    rows.append(r)
                else:
                    rows.append(Row(_iso(now), platform, u.rstrip("/").rsplit("/", 1)[-1], url=u, error=err))
                continue
            r = row_from_info(info, platform, now)
            r.url = r.url or u
            cap = captions.get(u)
            if cap and (not r.title or r.title.lower().startswith("video by")):
                r.title, r.streamer = cap, guess_streamer(cap)
            rows.append(r)
            followers = followers or _num(info.get("channel_follower_count"))
        if followers:
            rows.append(Row(_iso(now), platform, "_channel", followers=followers))
    append_rows(data_dir / "ops" / "metrics.csv", rows)
    return rows


def append_rows(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ weekly summary
def _f(v: str) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def views_at(snaps: list[dict], hours: float, lo: float, hi: float) -> int | None:
    """Views from the snapshot whose age is closest to ``hours`` (within lo..hi), else None."""
    best = None
    for s in snaps:
        age, views = _f(s.get("age_h")), _f(s.get("views"))
        if age is None or views is None or not (lo <= age <= hi):
            continue
        if best is None or abs(age - hours) < abs(best[0] - hours):
            best = (age, int(views))
    return best[1] if best else None


def summarize(rows: list[dict], now: datetime | None = None, days: int = 7) -> str:
    now = now or datetime.now(timezone.utc)
    by_video: dict[tuple[str, str], list[dict]] = defaultdict(list)
    followers: dict[str, list[tuple[str, int]]] = defaultdict(list)
    errors: dict[str, str] = {}
    for r in rows:
        if r.get("error"):
            errors[r["platform"]] = r["error"]
            continue
        if r["video_id"] == "_channel":
            if _f(r.get("followers")) is not None:
                followers[r["platform"]].append((r["snapshot_utc"], int(float(r["followers"]))))
            continue
        by_video[(r["platform"], r["video_id"])].append(r)

    since = now - timedelta(days=days)
    out = [f"# Weekly stats, {now:%Y-%m-%d}", "",
           f"Public counts from `metrics.csv` (source: {SOURCE}). A hit = {HIT_VIEWS}+ views at 48 h.", ""]
    for platform in sorted({p for p, _ in by_video} | set(followers)):
        vids = []
        for (p, vid), snaps in by_video.items():
            if p != platform:
                continue
            snaps.sort(key=lambda s: s["snapshot_utc"])
            last = snaps[-1]
            posted = last.get("posted_utc") or ""
            vids.append({"id": vid, "title": last.get("title") or vid, "streamer": last.get("streamer") or "?",
                         "posted": posted, "views": None if _f(last.get("views")) is None else int(_f(last.get("views"))),
                         "v48": views_at(snaps, 48, 40, 56), "url": last.get("url") or ""})
        out.append(f"## {platform}")
        fl = sorted(followers.get(platform, []))
        if fl:
            week = [v for t, v in fl if t >= _iso(since)] or [fl[-1][1]]
            out.append(f"- Followers: {fl[-1][1]} ({fl[-1][1] - week[0]:+d} this week)")
        recent = [v for v in vids if v["posted"] and v["posted"] >= _iso(since)]
        out.append(f"- Videos tracked: {len(vids)}, posted this week: {len(recent)}, "
                   f"total views now: {sum(v['views'] or 0 for v in vids)}"
                   + ("" if any(v["views"] is not None for v in vids) or not vids else " (view counts not public)"))
        judged = [v for v in vids if v["v48"] is not None]
        if judged:
            hits = [v for v in judged if v["v48"] >= HIT_VIEWS]
            out.append(f"- Hit rate at 48 h: {len(hits)}/{len(judged)}")
            ranked = sorted(judged, key=lambda v: v["v48"], reverse=True)
            out += ["", "Winners (views at 48 h):"]
            out += [f"1. {v['title']} ({v['streamer']}): {v['v48']}" for v in ranked[:5]]
            if len(ranked) > 5:
                out += ["", "Losers:"]
                out += [f"1. {v['title']} ({v['streamer']}): {v['v48']}" for v in ranked[5:][-3:][::-1]]
        else:
            ranked = sorted((v for v in vids if v["views"] is not None), key=lambda v: v["views"], reverse=True)
            out.append("- No 48 h readings yet (40-56 h old)" + ("; top by total views:" if ranked else "."))
            out += [f"  1. {v['title']} ({v['streamer']}): {v['views']}" for v in ranked[:5]]
        per: dict[str, list[int]] = defaultdict(list)
        for v in judged or [v for v in vids if v["views"] is not None]:
            per[v["streamer"]].append(v["v48"] if v["v48"] is not None else v["views"])
        if per:
            avg = sorted(((sum(x) / len(x), s, len(x)) for s, x in per.items()), reverse=True)
            out += ["", "By streamer (average): " + ", ".join(f"{s} {a:.0f} (n={n})" for a, s, n in avg)]
        out.append("")
    if errors:
        out.append("## Could not read")
        out += [f"- {p}: {e}" for p, e in sorted(errors.items())]
        out.append("")
    return "\n".join(out)


def weekly(data_dir: Path, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    text = summarize(read_rows(data_dir / "ops" / "metrics.csv"), now)
    p = data_dir / "ops" / "weekly" / f"{now:%Y-%m-%d}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m clipstudio.ops.metrics", description=__doc__.split("\n")[0])
    ap.add_argument("action", choices=["snapshot", "weekly", "daily"])
    ap.add_argument("--data-dir", default="yt_data")
    a = ap.parse_args(argv)
    d = Path(a.data_dir)
    if a.action in ("snapshot", "daily"):
        rows = snapshot(d)
        ok = [r for r in rows if not r.error and r.video_id != "_channel"]
        bad = [r for r in rows if r.error]
        print(f"logged {len(ok)} videos to {d / 'ops' / 'metrics.csv'}; {len(bad)} could not be read")
        for r in bad[:10]:
            print(f"  {r.platform}: {r.url} -> {r.error}")
    if a.action == "weekly" or (a.action == "daily" and datetime.now(timezone.utc).weekday() == 0):
        print(f"weekly summary: {weekly(d)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
