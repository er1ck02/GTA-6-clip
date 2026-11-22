"""Top live streams in a Twitch category, through Twitch's official API (Helix).

Needs a free Twitch developer app: its client id and secret in ``~/.clipstudio/twitch.json``
(``{"client_id": "...", "client_secret": "..."}``). Without it this returns nothing and production falls back
to the Twitch channel list and the YouTube live search. See docs/PRODUCTION.md.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

from clipstudio.core.logging import get_logger
from clipstudio.production.lineup import LiveCheck

log = get_logger("production")

_token: dict = {}
_games: dict[str, str] = {}


def credentials_path() -> Path:
    return Path(os.environ.get("CLIPSTUDIO_HOME") or Path.home() / ".clipstudio") / "twitch.json"


def credentials() -> tuple[str, str] | None:
    try:
        doc = json.loads(credentials_path().read_text(encoding="utf-8"))
        cid, secret = doc.get("client_id", "").strip(), doc.get("client_secret", "").strip()
        return (cid, secret) if cid and secret else None
    except (OSError, ValueError, AttributeError):
        return None


def _call(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed https Twitch hosts
        return json.loads(r.read().decode("utf-8"))


def _app_token(cid: str, secret: str) -> str:
    if _token.get("cid") == cid and _token.get("exp", 0) > time.time() + 60:
        return _token["value"]
    q = urllib.parse.urlencode({"client_id": cid, "client_secret": secret, "grant_type": "client_credentials"})
    doc = _call("https://id.twitch.tv/oauth2/token", data=q.encode())
    _token.update(cid=cid, value=doc["access_token"], exp=time.time() + float(doc.get("expires_in", 3600)))
    return _token["value"]


def top_streams(game: str, limit: int = 40) -> list[LiveCheck]:
    """Live streams in ``game`` (e.g. "Grand Theft Auto VI"), most viewers first ([] without credentials)."""
    cred = credentials()
    if not cred:
        return []
    cid, secret = cred
    try:
        h = {"Client-Id": cid, "Authorization": f"Bearer {_app_token(cid, secret)}"}
        if game not in _games:
            data = _call("https://api.twitch.tv/helix/games?" + urllib.parse.urlencode({"name": game}), headers=h)
            if not data.get("data"):
                log.warning("Twitch has no category called %r", game)
                return []
            _games[game] = data["data"][0]["id"]
        q = urllib.parse.urlencode({"game_id": _games[game], "first": max(1, min(100, limit)), "type": "live"})
        data = _call("https://api.twitch.tv/helix/streams?" + q, headers=h)
    except Exception as e:  # noqa: BLE001 - network, bad credentials: fall back to the other sources
        log.warning("Twitch category lookup failed: %s", e)
        _token.clear()
        return []
    out = [LiveCheck(True, title=s.get("title", ""), url=f"https://www.twitch.tv/{s['user_login'].lower()}",
                     viewers=int(s.get("viewer_count") or 0), category=s.get("game_name") or game)
           for s in data.get("data", []) if s.get("user_login")]
    return sorted(out, key=lambda r: -r.viewers)
