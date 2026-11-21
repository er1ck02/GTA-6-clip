"""Load chat replays from common export formats and turn them into a per-second activity signal.

Supported (auto-detected):
* TwitchDownloader JSON: {"comments": [{"content_offset_seconds", "commenter": {"display_name"}, "message": {"body"}}]}
* chat-downloader JSON: [{"time_in_seconds", "message", "author": {"name"}}]
* yt-dlp YouTube live chat (``.live_chat.json``, one JSON object per line with replayChatItemAction)
* CSV / TSV with a header containing a time column (``time``, ``t``, ``offset``, ``seconds``) and a text column
  (``message``, ``text``, ``body``); times may be seconds or H:MM:SS.

Chat is opt-in by presence: nothing is downloaded. Put the export next to the video
(``<video name>.chat.json``, ``.live_chat.json``, ``.chat.csv``) or pass ``--chat``.
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path

import numpy as np

from clipstudio.core.errors import MediaError
from clipstudio.core.models import ChatMessage, ChatTrack

SIDECAR_SUFFIXES = (".chat.json", ".live_chat.json", ".chat.csv", ".chat.tsv", ".json")


def find_sidecar(video: Path) -> Path | None:
    stem = video.with_suffix("")
    for suf in SIDECAR_SUFFIXES:
        p = Path(str(stem) + suf)
        if p.exists() and p.is_file() and p != video:
            return p
    return None


def _ts(v) -> float:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s)
    parts = [float(x) for x in s.split(":")]
    t = 0.0
    for x in parts:
        t = t * 60 + x
    return t


def load_chat(path: Path) -> ChatTrack:
    raw = path.read_text(encoding="utf-8", errors="replace")
    head = raw.lstrip()[:1]
    msgs: list[ChatMessage] = []
    fmt = "unknown"
    if head == "{" and "\n{" in raw.strip() and "replayChatItemAction" in raw[:5000]:
        fmt = "youtube_live_chat"
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                act = d["replayChatItemAction"]
                t = int(act.get("videoOffsetTimeMsec", 0)) / 1000
                for a in act.get("actions", []):
                    r = a.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
                    if not r:
                        continue
                    text = "".join(x.get("text", x.get("emoji", {}).get("shortcuts", [""])[0])
                                   for x in r.get("message", {}).get("runs", []))
                    msgs.append(ChatMessage(t=t, user=r.get("authorName", {}).get("simpleText", ""), text=text))
            except (KeyError, ValueError, TypeError):
                continue
    elif head in "{[":
        try:
            d = json.loads(raw)
        except ValueError as e:
            raise MediaError(f"chat file {path.name} is not valid JSON: {e}") from e
        if isinstance(d, dict) and "comments" in d:
            fmt = "twitchdownloader"
            for c in d["comments"]:
                body = c.get("message", {}).get("body") if isinstance(c.get("message"), dict) else c.get("message")
                if body is None:
                    continue
                msgs.append(ChatMessage(t=float(c.get("content_offset_seconds", 0)),
                                        user=(c.get("commenter") or {}).get("display_name", ""), text=str(body)))
        elif isinstance(d, list):
            fmt = "chat_downloader"
            for c in d:
                if not isinstance(c, dict):
                    continue
                t = c.get("time_in_seconds", c.get("time", c.get("offset")))
                text = c.get("message", c.get("text"))
                if t is None or text is None:
                    continue
                author = c.get("author")
                msgs.append(ChatMessage(t=_ts(t), user=author.get("name", "") if isinstance(author, dict) else str(author or ""),
                                        text=str(text)))
        else:
            raise MediaError(f"unrecognised chat JSON layout in {path.name}")
    else:
        fmt = "csv"
        dialect = "excel-tab" if "\t" in raw.splitlines()[0] else "excel"
        rows = list(csv.DictReader(io.StringIO(raw), dialect=dialect))
        if not rows:
            raise MediaError(f"chat file {path.name} is empty")
        keys = {k.lower().strip(): k for k in rows[0] if k}
        tk = next((keys[k] for k in ("time", "t", "offset", "seconds", "time_in_seconds", "timestamp") if k in keys), None)
        mk = next((keys[k] for k in ("message", "text", "body", "content") if k in keys), None)
        uk = next((keys[k] for k in ("user", "author", "name", "username") if k in keys), None)
        if not tk or not mk:
            raise MediaError(f"chat CSV {path.name} needs a time column and a message column")
        for r in rows:
            try:
                msgs.append(ChatMessage(t=_ts(r[tk]), user=r.get(uk, "") if uk else "", text=r[mk] or ""))
            except (ValueError, TypeError):
                continue
    msgs.sort(key=lambda m: m.t)
    return ChatTrack(source=str(path), format=fmt, messages=msgs)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def activity(chat: ChatTrack, duration: float, keywords: list[str]) -> tuple[np.ndarray, np.ndarray]:
    """Per-second message counts and keyword-hit counts."""
    n = int(np.ceil(max(duration, 1)))
    rate = np.zeros(n, np.float32)
    hype = np.zeros(n, np.float32)
    singles = {k for k in keywords if " " not in k}
    phrases = [k for k in keywords if " " in k]
    for m in chat.messages:
        i = int(m.t)
        if 0 <= i < n:
            rate[i] += 1
            toks = _tokens(m.text)
            low = " ".join(toks)
            if any(t in singles for t in toks) or any(p in low for p in phrases):
                hype[i] += 1
    return rate, hype


def chat_score(rate: np.ndarray, hype: np.ndarray, start: float, end: float, window: float) -> float:
    """0..1: how far the busiest ``window`` seconds in [start, end+4] exceed the stream's typical chat rate.

    Chat lags the action by a few seconds, so the window looks a little past the clip end.
    """
    if rate.sum() == 0:
        return 0.0
    w = max(1, int(round(window)))
    k = np.ones(w, np.float32)
    r = np.convolve(rate, k, mode="same")
    hy = np.convolve(hype, k, mode="same")
    base = float(np.median(r)) + 1.0
    a, b = max(0, int(start)), min(len(r), int(end + 4) + 1)
    if b <= a:
        return 0.0
    peak = float(r[a:b].max())
    hype_frac = float(hy[a:b].max()) / max(1.0, peak)
    lift = (peak / base - 1.0) / 3.0  # 4x the typical rate saturates
    return float(min(1.0, max(0.0, 0.75 * min(1.0, lift) + 0.25 * hype_frac)))


def chat_peaks(rate: np.ndarray, window: float, min_lift: float = 2.5, radius: float = 20.0) -> list[float]:
    """Times (s) where chat activity is at least ``min_lift`` x its median, local maxima ``radius`` apart."""
    if rate.sum() == 0:
        return []
    w = max(1, int(round(window)))
    r = np.convolve(rate, np.ones(w, np.float32), mode="same")
    base = float(np.median(r)) + 1.0
    order = np.argsort(-r)
    out: list[float] = []
    for i in order:
        if r[i] < base * min_lift:
            break
        if all(abs(i - j) >= radius for j in out):
            out.append(float(i))
    # chat reacts after the moment: seed the window a few seconds earlier
    return sorted(max(0.0, t - 4.0) for t in out)
