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
    viewers: int = 0
    category: str = ""          # the stream's game/category when the site reports it ("" = unknown)
    language: str = ""          # the stream's language code when the site reports it ("" = unknown)


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
# Twitch live pages report the title as "Name (live)" and the real stream title as the description, and the
# viewer count as view_count; YouTube has concurrent_view_count and the real title.
PROBE_FORMAT = "%(live_status)s|%(concurrent_view_count,view_count|)s|%(categories.0|)s|%(description)j|%(title)s"


def parse_probe(line: str, url: str = "") -> LiveCheck:
    status, _, rest = line.partition("|")
    viewers, _, rest = rest.partition("|")
    category, _, rest = rest.partition("|")
    desc = ""
    try:
        val, end = json.JSONDecoder().raw_decode(rest)
        desc, rest = (val if isinstance(val, str) else ""), rest[end:].removeprefix("|")
    except ValueError:  # "NA|title"
        rest = rest.partition("|")[2] if rest.startswith("NA|") else rest
    title = rest.strip()
    if desc.strip() and (re.search(r"\((live|rerun)\)$", title) or "twitch.tv" in url):
        title = desc.strip().splitlines()[0]
    category = "" if category.strip() in ("NA", "None") else category.strip()
    if "youtu" in url:  # YouTube categories are "Gaming", "Entertainment"...: not the game being played
        category = ""
    return LiveCheck(status == "is_live", title=title, url=url, category=category,
                     viewers=int(viewers) if viewers.strip().isdigit() else 0)


def probe(url: str, timeout: float = 45) -> LiveCheck:
    """Is ``url`` live right now? One yt-dlp metadata request, nothing downloaded."""
    from clipstudio.live.buffer import _ytdlp_cmd

    try:
        cmd = _ytdlp_cmd() + ["--quiet", "--no-warnings", "--skip-download", "--no-playlist",
                              "--print", PROBE_FORMAT, url]
        cp = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return LiveCheck(None, url=url, error="timed out")
    except Exception as e:  # noqa: BLE001 - yt-dlp missing etc.
        return LiveCheck(None, url=url, error=str(e))
    first = ((cp.stdout or "").strip().splitlines() or [""])[0]
    if cp.returncode == 0 and first.partition("|")[0]:
        return parse_probe(first, url)
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


Idle = Callable[[LiveCheck], str]


def idle_reason(r: LiveCheck, idle_words: list[str], game_words: list[str]) -> str:
    """Why a live stream is not worth recording ("" = it is): a sleep/AFK/Just Chatting stream (title or
    category), or a category that is known and is not GTA."""
    title, cat = (r.title or "").lower(), (r.category or "").lower()
    for w in idle_words:
        if w and (re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", title) or w.lower() in cat):
            return w.lower() if w.lower().endswith("stream") else f"{w.lower()} stream"
    if cat and game_words and not any(g.lower() in cat for g in game_words):
        return f"playing {r.category}, not GTA"
    return ""


# YouTube search, "Live" filter: every live stream matching the query, with its concurrent viewers
YT_LIVE_SEARCH = "https://www.youtube.com/results?search_query={q}&sp=EgJAAQ%253D%253D"
YT_SEARCH_FORMAT = "%(live_status)s|%(concurrent_view_count|)s|%(uploader_id|)s|%(title)s"


def parse_search_line(line: str) -> LiveCheck | None:
    status, _, rest = line.partition("|")
    viewers, _, rest = rest.partition("|")
    handle, _, title = rest.partition("|")
    handle = handle.strip()
    if status != "is_live" or not handle.startswith("@"):
        return None
    return LiveCheck(True, title=title.strip(), url=f"https://www.youtube.com/{handle}/live",
                     viewers=int(viewers) if viewers.strip().isdigit() else 0)


def youtube_live(query: str, limit: int = 40, timeout: float = 90) -> list[LiveCheck]:
    """Live YouTube streams for ``query`` right now (one yt-dlp search request, nothing downloaded)."""
    from urllib.parse import quote_plus

    from clipstudio.live.buffer import _ytdlp_cmd

    try:
        cp = subprocess.run(_ytdlp_cmd() + ["--quiet", "--no-warnings", "--flat-playlist", "--playlist-end",
                                            str(limit), "--print", YT_SEARCH_FORMAT,
                                            YT_LIVE_SEARCH.format(q=quote_plus(query))],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("YouTube live search failed: %s", e)
        return []
    return [r for r in map(parse_search_line, (cp.stdout or "").splitlines()) if r]


# words that show a stream title is not in English (our titles and audience are English)
FOREIGN_WORDS = {
    # Indonesian / Malay
    "main", "bareng", "indonesia", "indo", "seru", "banget", "gak", "nggak", "yuk", "dan", "yang", "ini", "aku",
    "kita", "malam", "sampai", "tamat", "mabar", "ngonten", "gimana", "baru", "cerita",
    # Spanish / Portuguese
    "vivo", "jugando", "directo", "juego", "jogando", "jogo", "nuevo", "novo", "parte", "español", "espanol",
    "brasil", "partida", "aventura", "hoy", "hoje", "historia",
    # French / German / Italian / Turkish / Dutch / Polish
    "découvre", "decouvre", "je", "avec", "les", "des", "mit", "und", "spielen", "deutsch", "giochiamo",
    "oynuyoruz", "ile", "türkçe", "turkce", "nederlands", "polska", "gramy",
}


def looks_english(title: str) -> bool:
    """A title in English: Latin letters only (emoji are fine) and none of the common foreign words above."""
    letters = [ch for ch in title or "" if ch.isalpha()]
    if letters and sum(ord(ch) > 0x24F for ch in letters) / len(letters) > 0.1:  # non-Latin script
        return False
    words = set(re.findall(r"[^\W\d_]+", (title or "").lower()))
    return not (words & FOREIGN_WORDS)


def discover_live(channels: list[str], check: Probe, keywords: list[str] | None = None,
                  workers: int = 8, idle: Idle | None = None,
                  search: Callable[[], list[LiveCheck]] | None = None,
                  category: Callable[[], list[LiveCheck]] | None = None) -> list[LiveCheck]:
    """Live streams worth standing in, most viewers first: big Twitch streamers (``production.discover_channels``)
    plus a YouTube live search (``search``), each with a title matching ``keywords``. Used to keep every slot
    filled once your own past streamers run out. ``category`` (Twitch's GTA VI directory, most viewers first) is
    tried first; its streams don't need a keyword in the title, but sleep/AFK ones are still skipped."""
    first: list[LiveCheck] = []
    if category is not None:
        try:
            first = [r for r in category() if r.live and not (idle and idle(r))]
        except Exception as e:  # noqa: BLE001 - best effort
            log.warning("Twitch category lookup failed: %s", e)
    names = [c.lower().lstrip("@") for c in channels if c and re.fullmatch(r"@?[\w]+", c)]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        found = ex.submit(search) if search else None
        res = list(ex.map(lambda c: check(f"https://www.twitch.tv/{c}"), names))
        if found is not None:
            try:
                res += found.result()
            except Exception as e:  # noqa: BLE001 - best effort
                log.warning("live search failed: %s", e)
    live = [r for r in res if r.live and title_matches(r.title, list(keywords or [])) and not (idle and idle(r))]
    seen = {channel_of(r.url) for r in first}
    return first + sorted((r for r in live if channel_of(r.url) not in seen), key=lambda r: -r.viewers)


# ------------------------------------------------------------------ planning
@dataclass
class SlotPlan:
    wanted: str                      # the lineup channel this slot is for
    wanted_url: str | None
    action: str                      # watch | substitute | keep | wait | fill (extra slot to reach the target)
    channel: str | None = None       # what will actually be watched
    url: str | None = None
    note: str = ""
    title: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def plan_lineup(lineup: list[dict], ranked: list[PastChannel], check: Probe, *, substitute: bool = True,
                keywords: list[str] | None = None, max_candidates: int = 12,
                busy: set[str] | None = None, workers: int = 8, target: int = 0,
                discover: Callable[[], list[LiveCheck]] | None = None,
                idle: Idle | None = None, stand_in_ok: Callable[[LiveCheck], bool] | None = None) -> list[SlotPlan]:
    """For each lineup entry ({channel, url}) decide: watch it (live), stand someone in (offline), or wait.

    ``busy`` are channels already being watched (kept as they are, never picked as a substitute twice).
    ``target`` (0 = off) is how many streams should record: stand-ins only fill up to it, and when the lineup
    alone can't reach it, extra "fill" slots are added. ``idle`` names live streams that count as offline
    (sleep/AFK/Just Chatting, not GTA). Stand-ins come from your past streamers first, then
    from ``discover()`` (live GTA streams on Twitch, most viewers first).
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
        why_idle = idle(r) if (idle and r.live) else ""
        if why_idle:
            p.note = f"on a {why_idle}" if why_idle.endswith("stream") else why_idle
        elif r.live:
            p.action, p.channel, p.url, p.title = "watch", p.wanted, r.url, r.title
            p.note = "live"
            busy.add(p.wanted)
        else:
            p.note = "offline" if r.live is False else f"not reachable ({r.error or 'unknown error'})"

    waiting = [p for p in plans if p.action == "wait"]
    recording = sum(p.action in ("watch", "keep") for p in plans)
    room = (target - recording) if target else len(waiting)
    if not substitute or room <= 0:
        if substitute and target:
            for p in waiting:
                p.note = f"{p.wanted} is {p.note}; all stream slots are full"
        return plans
    lineup_names = {p.wanted for p in plans}
    pool = [c for c in ranked if c.channel not in lineup_names and c.channel not in busy][:max_candidates]
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        checks = list(ex.map(lambda c: first_live(c.urls + [u for u in guess_urls(c.channel) if u not in c.urls],
                                                  check), pool))
    live_pool = [(c, r) for c, r in zip(pool, checks)
                 if r.live and title_matches(r.title, keywords) and not (idle and idle(r))
                 and (stand_in_ok is None or stand_in_ok(r))]
    if discover is not None and len(live_pool) < room:
        taken = lineup_names | busy | {c.channel for c, _ in live_pool}
        try:
            found = discover()
        except Exception as e:  # noqa: BLE001 - discovery is best effort
            log.warning("finding live GTA streams on Twitch failed: %s", e)
            found = []
        for r in found:
            ch = channel_of(r.url)
            if ch and ch not in taken and (stand_in_ok is None or stand_in_ok(r)):
                live_pool.append((PastChannel(ch, [r.url]), r))
                taken.add(ch)

    def why_picked(c: PastChannel, r: LiveCheck) -> str:
        if c.sessions:
            return f"{c.clips} clips from {c.sessions} past session{'s' if c.sessions != 1 else ''}"
        site = "YouTube" if "youtu" in (r.url or "") else "Twitch"
        return f"top live GTA stream on {site}" + (f", {r.viewers:,} viewers" if r.viewers else "")

    for p in waiting:
        if room <= 0:
            p.note = f"{p.wanted} is {p.note}; all stream slots are full"
            continue
        if not live_pool:
            why = "none of your past streamers" + (f" streaming {'/'.join(keywords)}" if keywords else "")
            if discover is not None:
                why = "no past streamer or other live GTA 6 stream"
            p.note = f"{p.wanted} is {p.note}; {why} is free right now, checking again later"
            continue
        c, r = live_pool.pop(0)
        p.action, p.channel, p.url, p.title = "substitute", c.channel, r.url, r.title
        p.note = f"{p.wanted} is {p.note}; watching {c.channel} instead ({why_picked(c, r)})"
        busy.add(c.channel)
        room -= 1
    while target and room > 0 and live_pool:
        c, r = live_pool.pop(0)
        plans.append(SlotPlan(c.channel, r.url, "fill", c.channel, r.url,
                              f"keeping {target} streams going: watching {c.channel} ({why_picked(c, r)})", r.title))
        busy.add(c.channel)
        room -= 1
    return plans
