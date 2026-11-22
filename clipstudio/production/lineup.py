"""Which channels production watches: the last lineup, who is live now, and who stands in for an offline channel.

Everything here is plain functions over plain data so it can be tested without a network: ``probe`` is the only
thing that talks to the internet (one yt-dlp metadata request per URL, no download).
"""
from __future__ import annotations

import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from clipstudio.core.logging import get_logger

log = get_logger("production")

SESSION_DIR_RE = re.compile(r"^(?P<channel>.+)_(?P<stamp>\d{8}_\d{6})$")
OFFLINE_HINTS = ("not currently live", "is offline", "not live", "no video formats", "does not exist",
                 "channel is not", "offline", "404")


@dataclass
class LiveCheck:
    live: bool | None          # None = could not tell (network, yt-dlp missing)
    title: str = ""
    url: str = ""
    error: str = ""


Probe = Callable[[str], LiveCheck]


@dataclass
class PastChannel:
    channel: str
    urls: list[str] = field(default_factory=list)
    sessions: int = 0
    moments: int = 0
    clips: int = 0
    last_seen: float = 0.0

    @property
    def score(self) -> float:
        return self.clips * 3 + self.moments + self.sessions * 0.5


# ------------------------------------------------------------------ URLs
def resume_url(url: str | None, channel: str | None) -> str | None:
    """The address that finds this channel live next time. A YouTube watch link points at one broadcast that
    ends, so it becomes the channel's /live page; Twitch and Kick channel pages work as they are."""
    if url:
        low = url.lower()
        m = re.search(r"twitch\.tv/(?!videos/)([a-z0-9_]+)", low)
        if m:
            return f"https://www.twitch.tv/{m.group(1)}"
        m = re.search(r"kick\.com/([a-z0-9_]+)", low)
        if m and m.group(1) not in ("video", "videos"):
            return f"https://kick.com/{m.group(1)}"
        m = re.search(r"youtube\.com/@([\w.-]+)", url, re.I)
        if m:
            return f"https://www.youtube.com/@{m.group(1)}/live"
        if "youtu" in low and channel:
            return f"https://www.youtube.com/@{channel}/live"
        if re.match(r"https?://", url):
            return url
    return None


def guess_urls(channel: str) -> list[str]:
    """Where a channel we only know by name may be streaming (older sessions did not record their link)."""
    c = channel.lower().lstrip("@")
    if not re.fullmatch(r"[\w.-]+", c):
        return []
    return [f"https://www.twitch.tv/{c}", f"https://www.youtube.com/@{c}/live", f"https://kick.com/{c}"]


def channel_of(url: str) -> str | None:
    m = re.search(r"(?:twitch\.tv|kick\.com)/([A-Za-z0-9_]+)|youtube\.com/@([\w.-]+)", url)
    return (m.group(1) or m.group(2)).lower() if m else None


# ------------------------------------------------------------------ history
def scan_history(live_dir: Path, known: dict[str, dict] | None = None) -> dict[str, PastChannel]:
    """Every channel ever watched, from the session folders under ``<data_dir>/live`` plus remembered URLs."""
    out: dict[str, PastChannel] = {}

    def get(ch: str) -> PastChannel:
        ch = ch.lower().lstrip("@")
        return out.setdefault(ch, PastChannel(ch))

    if live_dir.is_dir():
        for d in live_dir.iterdir():
            m = SESSION_DIR_RE.match(d.name)
            if not d.is_dir() or not m:
                continue
            doc, src = {}, {}
            try:
                doc = json.loads((d / "queue.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            try:
                src = json.loads((d / "source.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
            ch = doc.get("channel") or src.get("channel") or m.group("channel")
            if not ch or ch == "stream" or re.fullmatch(r"stream-\d+", ch):
                continue
            pc = get(ch)
            pc.sessions += 1
            ms = doc.get("moments") or []
            pc.moments += len(ms)
            pc.clips += sum(1 for x in ms if x.get("status") == "done")
            try:
                pc.last_seen = max(pc.last_seen, d.stat().st_mtime)
            except OSError:
                pass
            u = resume_url(src.get("url"), pc.channel)
            if u and u not in pc.urls:
                pc.urls.insert(0, u)
    for ch, info in (known or {}).items():
        pc = get(ch)
        u = resume_url(info.get("url"), pc.channel)
        if u and u not in pc.urls:
            pc.urls.insert(0, u)
        pc.last_seen = max(pc.last_seen, float(info.get("last_seen") or 0))
    return out


def rank(history: dict[str, PastChannel], allow: list[str] | None = None) -> list[PastChannel]:
    """Best first: channels you may post (allowlist), then the ones that gave the most clips."""
    allowed = {a.lower().lstrip("@") for a in (allow or [])}
    return sorted(history.values(), key=lambda p: (p.channel not in allowed, -p.score, -p.last_seen, p.channel))


# ------------------------------------------------------------------ live checks
def probe(url: str, timeout: float = 45) -> LiveCheck:
    """Is ``url`` live right now? One yt-dlp metadata request, nothing downloaded."""
    from clipstudio.live.buffer import _ytdlp_cmd

    try:
        cmd = _ytdlp_cmd() + ["--quiet", "--no-warnings", "--skip-download", "--no-playlist",
                              "--print", "%(live_status)s|%(title)s", url]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return LiveCheck(None, url=url, error="timed out")
    except Exception as e:  # noqa: BLE001 - yt-dlp missing etc.
        return LiveCheck(None, url=url, error=str(e))
    first = (cp.stdout.strip().splitlines() or [""])[0]
    status, _, title = first.partition("|")
    if cp.returncode == 0 and status:
        return LiveCheck(status == "is_live", title=title.strip(), url=url)
    err = (cp.stderr or "").strip().splitlines()
    msg = err[-1] if err else f"yt-dlp exit {cp.returncode}"
    low = (cp.stderr or "").lower()
    return LiveCheck(False if any(h in low for h in OFFLINE_HINTS) else None, url=url, error=msg[-300:])


def first_live(urls: list[str], check: Probe) -> LiveCheck:
    """Try a channel's addresses in order; the first live one wins (else the most certain answer)."""
    best = LiveCheck(None, error="no address known")
    for u in urls:
        r = check(u)
        if r.live:
            return r
        if r.live is False or best.live is None:
            best = r
    return best


def title_matches(title: str, keywords: list[str]) -> bool:
    low = (title or "").lower()
    return not keywords or any(k.lower() in low for k in keywords)


# ------------------------------------------------------------------ planning
@dataclass
class SlotPlan:
    wanted: str                      # the lineup channel this slot is for
    wanted_url: str | None
    action: str                      # watch | substitute | keep | wait
    channel: str | None = None       # what will actually be watched
    url: str | None = None
    note: str = ""
    title: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def plan_lineup(lineup: list[dict], ranked: list[PastChannel], check: Probe, *, substitute: bool = True,
                keywords: list[str] | None = None, max_candidates: int = 12,
                busy: set[str] | None = None, workers: int = 8) -> list[SlotPlan]:
    """For each lineup entry ({channel, url}) decide: watch it (live), stand someone in (offline), or wait.

    ``busy`` are channels already being watched (kept as they are, never picked as a substitute twice).
    """
    keywords = list(keywords or [])
    busy = {b.lower() for b in (busy or set())}
    plans: list[SlotPlan] = []
    todo: list[tuple[int, dict]] = []
    for e in lineup:
        ch = (e.get("channel") or channel_of(e.get("url") or "") or "").lower()
        if ch in busy:
            plans.append(SlotPlan(ch, e.get("url"), "keep", ch, e.get("url"), "already being watched"))
        else:
            plans.append(SlotPlan(ch, e.get("url"), "wait"))
            todo.append((len(plans) - 1, e))

    def urls_for(e: dict) -> list[str]:
        ch = (e.get("channel") or "").lower()
        own = [u for u in [resume_url(e.get("url"), ch)] if u]
        return own + [u for u in guess_urls(ch) if u not in own] if ch else own

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(lambda ie: first_live(urls_for(ie[1]), check), todo))
    for (i, _e), r in zip(todo, results):
        p = plans[i]
        if r.live:
            p.action, p.channel, p.url, p.title = "watch", p.wanted, r.url, r.title
            p.note = "live"
            busy.add(p.wanted)
        else:
            p.note = "offline" if r.live is False else f"not reachable ({r.error or 'unknown error'})"

    waiting = [p for p in plans if p.action == "wait"]
    if not (substitute and waiting):
        return plans
    lineup_names = {p.wanted for p in plans}
    pool = [c for c in ranked if c.channel not in lineup_names and c.channel not in busy][:max_candidates]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        checks = list(ex.map(lambda c: first_live(c.urls + [u for u in guess_urls(c.channel) if u not in c.urls],
                                                  check), pool))
    live_pool = [(c, r) for c, r in zip(pool, checks) if r.live and title_matches(r.title, keywords)]
    for p in waiting:
        if not live_pool:
            why = "none of your past streamers" + (f" streaming {'/'.join(keywords)}" if keywords else "")
            p.note = f"{p.wanted} is {p.note}; {why} is live right now, checking again later"
            continue
        c, r = live_pool.pop(0)
        p.action, p.channel, p.url, p.title = "substitute", c.channel, r.url, r.title
        p.note = (f"{p.wanted} is {p.note}; watching {c.channel} instead "
                  f"({c.clips} clips from {c.sessions} past session{'s' if c.sessions != 1 else ''})")
        busy.add(c.channel)
    return plans
