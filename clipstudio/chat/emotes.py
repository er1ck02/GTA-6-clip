"""Emote images for the chat overlay: which words in chat are emotes, and their pictures.

Sources, all public and without a login:

* 7TV, BetterTTV and FrankerFaceZ: the global sets and the channel's own sets (found through the channel's
  Twitch name, also for a YouTube stream of the same creator, whose viewers type the same emote names);
* a short list of classic Twitch global emotes (Kappa, LUL, PogChamp ...) from Twitch's image server.

Twitch subscriber emotes (a channel's paid emotes, e.g. "xqcL") are not in any public list by name: live mode
records their ids from Twitch chat's message tags into ``<session>/emotes.json``, and ``add_twitch_ids`` (or
``from_source``) turns those into pictures too.

Lists are cached for a day and pictures forever under ``<cache_dir>/emotes``. Everything is best effort:
with no network, or an unknown channel, words simply stay words. Only the channel's public name is sent.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

from clipstudio.core.logging import get_logger

log = get_logger("emotes")
TIMEOUT = 8.0
LIST_TTL = 24 * 3600
UA = {"User-Agent": "clipstudio-chat-overlay"}
# classic Twitch global emotes: name -> emote id on static-cdn.jtvnw.net
TWITCH_GLOBAL = {"Kappa": "25", "LUL": "425618", "PogChamp": "305954156", "BibleThump": "86", "Kreygasm": "41",
                 "ResidentSleeper": "245", "DansGame": "33", "NotLikeThis": "58765", "WutFace": "28087",
                 "4Head": "354", "PJSalt": "36", "SeemsGood": "64138", "HeyGuys": "30259", "Jebaited": "114836",
                 "cmonBruh": "84608", "BabyRage": "22639"}
SESSION_SUFFIX = re.compile(r"_\d{8}_\d{6}$")


def channel_from_source(source: Path) -> str | None:
    """Channel name from a live moment's path: ``.../live/<channel>_<date>_<time>/moments/m001.mp4``."""
    if source.parent.name != "moments":
        return None
    name = SESSION_SUFFIX.sub("", source.parent.parent.name)
    return name.lower() or None


def _get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


class EmoteIndex:
    """name -> picture URL for one channel; pictures are fetched on first use."""

    def __init__(self, cache_dir: Path, channel: str | None = None, providers: tuple[str, ...] = ("7tv", "bttv", "ffz", "twitch")):
        self.dir = Path(cache_dir) / "emotes"
        self.channel = (channel or "").lower().lstrip("@") or None
        self.providers = providers
        self.urls: dict[str, tuple[str, str]] = {}   # name -> (provider/id key, url)
        self._loaded = False

    @classmethod
    def from_source(cls, cache_dir: Path, source: Path, **kw) -> "EmoteIndex":
        """Index for a clip's source; a live moment also gets the emote ids its session recorded from chat."""
        idx = cls(cache_dir, channel_from_source(source), **kw)
        f = source.parent.parent / "emotes.json" if source.parent.name == "moments" else None
        if f is not None and f.is_file():
            try:
                idx.add_twitch_ids(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError) as e:
                log.debug("emote ids in %s unreadable: %s", f, e)
        return idx

    def add_twitch_ids(self, ids: dict[str, str]) -> None:
        """Emote name -> Twitch id seen in chat; these win over lists (they are exactly what chat sent)."""
        for name, eid in (ids or {}).items():
            if name and re.fullmatch(r"[\w-]+", str(eid)):
                self.urls[name] = (f"twitch/{eid}", f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0")

    # ------------------------------------------------------------ lists
    def _cached(self, key: str, fetch):
        f = self.dir / "lists" / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', key)}.json"
        try:
            if f.exists() and time.time() - f.stat().st_mtime < LIST_TTL:
                return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            data = fetch()
        except Exception as e:  # noqa: BLE001 - offline, 404 for an unknown channel, bad JSON: all "no emotes"
            log.debug("emote list %s unavailable: %s", key, e)
            if f.exists():
                try:
                    return json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return None
            return None
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass
        return data

    def load(self) -> "EmoteIndex":
        if self._loaded:
            return self
        self._loaded = True
        add = self.urls.setdefault
        twitch_id = None
        room = None
        if self.channel and "ffz" in self.providers:
            room = self._cached(f"ffz_room_{self.channel}", lambda: _get_json(f"https://api.frankerfacez.com/v1/room/{self.channel}"))
            twitch_id = str((room or {}).get("room", {}).get("twitch_id") or "") or None
        # the channel's own sets win over global ones of the same name: that is what its chat means by the word
        if twitch_id and "7tv" in self.providers:
            d = self._cached(f"7tv_user_{twitch_id}", lambda: _get_json(f"https://7tv.io/v3/users/twitch/{twitch_id}"))
            self._add_7tv(((d or {}).get("emote_set") or {}).get("emotes") or [])
        if twitch_id and "bttv" in self.providers:
            d = self._cached(f"bttv_user_{twitch_id}", lambda: _get_json(f"https://api.betterttv.net/3/cached/users/twitch/{twitch_id}")) or {}
            for e in (d.get("channelEmotes") or []) + (d.get("sharedEmotes") or []):
                add(e["code"], (f"bttv/{e['id']}", f"https://cdn.betterttv.net/emote/{e['id']}/2x"))
        for s in ((room or {}).get("sets") or {}).values():
            self._add_ffz(s.get("emoticons") or [])
        if "7tv" in self.providers:
            d = self._cached("7tv_global", lambda: _get_json("https://7tv.io/v3/emote-sets/global"))
            self._add_7tv((d or {}).get("emotes") or [])
        if "bttv" in self.providers:
            for e in self._cached("bttv_global", lambda: _get_json("https://api.betterttv.net/3/cached/emotes/global")) or []:
                add(e["code"], (f"bttv/{e['id']}", f"https://cdn.betterttv.net/emote/{e['id']}/2x"))
        if "ffz" in self.providers:
            d = self._cached("ffz_global", lambda: _get_json("https://api.frankerfacez.com/v1/set/global")) or {}
            for s in (d.get("sets") or {}).values():
                self._add_ffz(s.get("emoticons") or [])
        if "twitch" in self.providers:
            for name, eid in TWITCH_GLOBAL.items():
                add(name, (f"twitch/{eid}", f"https://static-cdn.jtvnw.net/emoticons/v2/{eid}/default/dark/2.0"))
        log.info("emotes: %d known for %s", len(self.urls), self.channel or "no channel (global sets only)")
        return self

    def _add_7tv(self, emotes: list) -> None:
        for e in emotes:
            host = ((e.get("data") or {}).get("host") or {}).get("url")
            if host and e.get("name"):
                self.urls.setdefault(e["name"], (f"7tv/{e.get('id')}", f"https:{host}/2x.webp"))

    def _add_ffz(self, emotes: list) -> None:
        for e in emotes:
            urls = e.get("urls") or {}
            url = urls.get("2") or urls.get("1")
            if url and e.get("name"):
                self.urls.setdefault(e["name"], (f"ffz/{e['id']}", url if url.startswith("http") else "https:" + url))

    # ------------------------------------------------------------ pictures
    def is_emote(self, word: str) -> bool:
        return word in self.load().urls

    def image_path(self, word: str) -> Path | None:
        """Local file of the emote's picture (downloaded once), or None if it is not an emote / unavailable."""
        hit = self.load().urls.get(word)
        if hit is None:
            return None
        key, url = hit
        f = self.dir / "img" / (key.replace("/", "_") + ".img")
        if f.exists() and f.stat().st_size > 0:
            return f
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = r.read(4 * 1024 * 1024)
            if not data:
                return None
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(f)
            return f
        except Exception as e:  # noqa: BLE001
            log.debug("emote picture %s unavailable: %s", word, e)
            return None
