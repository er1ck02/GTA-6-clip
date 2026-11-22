"""Live chat sources. Each yields (time, user, text) where time is either wall-clock (live) or stream time (replay).

* Twitch: anonymous read-only IRC (``justinfan`` login, the same way the public web player reads chat). No
  account, token or posting.
* YouTube: yt-dlp's ``live_chat`` subtitle writer, which appends messages to a file while the stream runs; the
  file is tailed.
* Replay: a chat export (any format :func:`clipstudio.chat.replay.load_chat` reads) played back against stream
  time, for testing live mode on a recorded VOD.
"""
from __future__ import annotations

import json
import queue
import random
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from clipstudio.core.logging import get_logger

log = get_logger("live.chat")


@dataclass
class LiveMessage:
    t: float          # wall clock for live sources, stream seconds for replay
    user: str
    text: str
    stream_time: bool = False
    emotes: dict[str, str] | None = None  # Twitch emote name -> id (from the IRC tags), for the overlay's pictures


class ChatSource:
    name = "none"

    def start(self) -> None: ...

    def poll(self, stream_now: float) -> list[LiveMessage]:
        return []

    def stop(self) -> None: ...


class _Threaded(ChatSource):
    def __init__(self) -> None:
        self.q: queue.Queue[LiveMessage] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_safe, daemon=True, name=f"chat-{self.name}")
        self._thread.start()

    def _run_safe(self) -> None:
        try:
            self._run()
        except Exception as e:  # chat is a signal, never a reason to stop recording
            log.warning("%s chat stopped: %s", self.name, e)

    def _run(self) -> None:
        raise NotImplementedError

    def poll(self, stream_now: float) -> list[LiveMessage]:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except queue.Empty:
                return out

    def stop(self) -> None:
        self._stop.set()


PRIVMSG = re.compile(r"^(?:@\S+ )?:(\w+)!\S+ PRIVMSG #\w+ :(.*)$")


def parse_irc_line(line: str) -> tuple[str, str] | None:
    m = PRIVMSG.match(line.strip("\r\n"))
    return (m.group(1), m.group(2)) if m else None


def irc_emotes(line: str, text: str) -> dict[str, str]:
    """Emote name -> Twitch emote id from a tagged PRIVMSG (``@...;emotes=25:0-4,6-10/1902:12-16;...``).

    Positions count characters of the message text, so the names include subscriber emotes (e.g. "xqcL") that no
    public list knows by name. Empty without the twitch.tv/tags capability or when the message has no emotes."""
    if not line.startswith("@"):
        return {}
    tags = line[1:].split(" ", 1)[0]
    raw = next((kv[7:] for kv in tags.split(";") if kv.startswith("emotes=")), "")
    out: dict[str, str] = {}
    for part in raw.split("/"):
        eid, _, spans = part.partition(":")
        if not eid or not spans:
            continue
        a, _, b = spans.split(",")[0].partition("-")
        try:
            name = text[int(a): int(b) + 1]
        except ValueError:
            continue
        if name and re.fullmatch(r"[\w-]+", eid):
            out[name] = eid
    return out


class TwitchChat(_Threaded):
    name = "twitch"
    HOST, PORT = "irc.chat.twitch.tv", 6667

    def __init__(self, channel: str):
        super().__init__()
        self.channel = channel.lower().lstrip("#")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with socket.create_connection((self.HOST, self.PORT), timeout=10) as s:
                    s.settimeout(1.0)
                    nick = f"justinfan{random.randint(10000, 99999)}"
                    # tags carry each message's emote ids (subscriber emotes have no public list by name)
                    s.sendall(f"CAP REQ :twitch.tv/tags\r\nPASS SCHMOOPIIE\r\nNICK {nick}\r\n"
                              f"JOIN #{self.channel}\r\n".encode())
                    log.info("reading Twitch chat for #%s (anonymous, read-only)", self.channel)
                    buf = b""
                    while not self._stop.is_set():
                        try:
                            data = s.recv(65536)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        buf += data
                        *lines, buf = buf.split(b"\r\n")
                        for raw in lines:
                            line = raw.decode(errors="replace")
                            if line.startswith("PING"):
                                s.sendall(b"PONG :tmi.twitch.tv\r\n")
                            elif (m := parse_irc_line(line)) is not None:
                                self.q.put(LiveMessage(time.time(), m[0], m[1],
                                                       emotes=irc_emotes(line, m[1]) or None))
            except OSError as e:
                log.warning("Twitch chat connection dropped (%s); reconnecting", e)
                self._stop.wait(5)


def youtube_chat_lines(line: str) -> list[tuple[str, str]]:
    """(user, text) pairs from one line of yt-dlp's live_chat JSON (live or replay layout)."""
    try:
        d = json.loads(line)
    except ValueError:
        return []
    act = d.get("replayChatItemAction", d)
    out = []
    for a in act.get("actions", []):
        r = a.get("addChatItemAction", {}).get("item", {}).get("liveChatTextMessageRenderer")
        if not r:
            continue
        text = "".join(x.get("text", (x.get("emoji", {}).get("shortcuts") or [""])[0])
                       for x in r.get("message", {}).get("runs", []))
        out.append((r.get("authorName", {}).get("simpleText", ""), text))
    return out


class YouTubeChat(_Threaded):
    name = "youtube"

    def __init__(self, url: str, work: Path):
        super().__init__()
        self.url, self.work = url, work
        self.proc: subprocess.Popen | None = None

    def _run(self) -> None:
        from clipstudio.live.buffer import _ytdlp_cmd

        self.work.mkdir(parents=True, exist_ok=True)
        for f in self.work.glob("chat*"):
            f.unlink()
        self.proc = subprocess.Popen(_ytdlp_cmd() + ["--quiet", "--no-warnings", "--skip-download", "--write-subs",
                                                     "--sub-langs", "live_chat", "-o", str(self.work / "chat"),
                                                     self.url],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        log.info("reading YouTube live chat through yt-dlp")
        pos, path = 0, None
        while not self._stop.is_set():
            if path is None:
                found = sorted(self.work.glob("chat*live_chat.json*"))
                path = found[0] if found else None
            if path is not None and path.exists():
                with open(path, "rb") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                cut = chunk.rfind(b"\n") + 1
                pos += cut
                now = time.time()
                for line in chunk[:cut].decode(errors="replace").splitlines():
                    for user, text in youtube_chat_lines(line):
                        self.q.put(LiveMessage(now, user, text))
            elif self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""
                log.warning("YouTube chat unavailable: %s", err[-300:] or "yt-dlp exited")
                return
            self._stop.wait(1.0)

    def stop(self) -> None:
        super().stop()
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()


class ReplayChat(ChatSource):
    """Plays a chat export back against stream time (for live-mode dry runs on a recorded VOD)."""
    name = "replay"

    def __init__(self, path: Path, offset: float = 0.0):
        from clipstudio.chat.replay import load_chat

        self.msgs = [m for m in load_chat(path).messages if m.t >= offset]
        self.offset = offset
        self.i = 0

    def poll(self, stream_now: float) -> list[LiveMessage]:
        out = []
        while self.i < len(self.msgs) and self.msgs[self.i].t - self.offset <= stream_now:
            m = self.msgs[self.i]
            out.append(LiveMessage(m.t - self.offset, m.user, m.text, stream_time=True))
            self.i += 1
        return out


def channel_from_url(url: str) -> str | None:
    m = re.search(r"twitch\.tv/(?!videos/)([A-Za-z0-9_]+)", url)
    if m:
        return m.group(1).lower()
    m = re.search(r"kick\.com/([A-Za-z0-9_]+)", url)
    if m:
        return m.group(1).lower()
    m = re.search(r"youtube\.com/@([A-Za-z0-9_.-]+)", url)
    return m.group(1).lower() if m else None


def youtube_channel(url: str) -> str | None:
    """The channel handle of a YouTube video/stream (one metadata request, no download)."""
    from clipstudio.live.buffer import _ytdlp_cmd

    try:
        cp = subprocess.run(_ytdlp_cmd() + ["--quiet", "--no-warnings", "--skip-download", "--print",
                                            "%(uploader_id)s|%(channel)s", url],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = (cp.stdout.strip().splitlines() or [""])[0]
    handle, _, name = first.partition("|")
    handle = handle.strip().lstrip("@")
    if handle and handle != "NA":
        return handle.lower()
    return name.strip().lower() or None


def make_chat_source(spec: str | None, url: str | None, work: Path) -> ChatSource:
    """``spec``: None/"auto" (from the URL), "none", "twitch:<channel>", "youtube", or a chat export path."""
    if spec == "none":
        return ChatSource()
    if spec and spec not in ("auto",):
        if spec.startswith("twitch:"):
            return TwitchChat(spec.split(":", 1)[1])
        if spec == "youtube" and url:
            return YouTubeChat(url, work / "chat")
        p = Path(spec).expanduser()
        if p.is_file():
            return ReplayChat(p)
        raise ValueError(f"unknown chat source {spec!r}: use none, twitch:<channel>, youtube or a chat file")
    if url:
        if "twitch.tv/" in url and (ch := channel_from_url(url)):
            return TwitchChat(ch)
        if "youtube.com" in url or "youtu.be" in url:
            return YouTubeChat(url, work / "chat")
    return ChatSource()


LINK_RE = re.compile(r"(https?://\S+)")


def split_links(text: str) -> list[str]:
    """Links and file paths from pasted text: one per line, or several URLs on a line separated by spaces/commas."""
    out: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip().strip(",")
        if not line:
            continue
        urls = LINK_RE.findall(line)
        out += [u.rstrip(",;)") for u in urls] if urls else [line]
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def classify_link(link: str, probe: bool = True) -> str:
    """"live", "vod" or "file". Twitch/Kick channel pages are live and their /videos/ pages are VODs; a YouTube
    link is asked about once with yt-dlp (one metadata request, no download) because watch URLs serve both."""
    if not re.match(r"https?://", link, re.I):
        return "file"
    low = link.lower()
    if re.search(r"twitch\.tv/(videos/|\w+/clip/|\w+/video/)|clips\.twitch\.tv|kick\.com/\w+/videos?/|"
                 r"kick\.com/video/|youtube\.com/shorts/", low):
        return "vod"
    if re.search(r"twitch\.tv/\w+/?($|\?)|kick\.com/\w+/?($|\?)", low):
        return "live"
    if re.search(r"youtube\.com/(@[\w.-]+|channel/[\w-]+|c/[\w-]+)/live", low) or "/live/" in low:
        return "live"
    if not probe:
        return "vod"
    from clipstudio.live.buffer import _ytdlp_cmd

    try:
        cp = subprocess.run(_ytdlp_cmd() + ["--quiet", "--no-warnings", "--skip-download", "--no-playlist",
                                            "--print", "%(live_status)s", link],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return "vod"
    status = (cp.stdout.strip().splitlines() or [""])[0]
    return "live" if status in ("is_live", "is_upcoming") else "vod"
