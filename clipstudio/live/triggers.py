"""Decide, second by second, whether something worth keeping just happened.

Four signals, each scored against the stream's own recent "normal" (so a loud streamer or a busy chat does not
trigger all the time):

* audio: loudness of the loudest second nearby vs the median of the last few minutes;
* chat: messages in a 5 s window vs the usual rate, and never fewer than ``chat_min_messages``;
* hype: messages with reaction words / emotes (LUL, KEKW, W, "no way"...) in that window;
* command: viewers typing "clip" / "clip it" (several within 10 s).

score = sum(weight x signal), scaled down when only one signal is present (except "clip" commands), or half the
audio signal for a very big jump. A moment is saved when the score reaches ``live.threshold``, outside the
cooldown. On a real 50-minute stream the previous rule (no agreement needed) saved 38 moments an hour, most of them
from chat or audio alone.
"""
from __future__ import annotations

import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from clipstudio.chat.replay import _tokens
from clipstudio.config import ChatCfg, LiveCfg
from clipstudio.media.ffmpeg import ffmpeg_bin

CHAT_WINDOW = 5
COMMAND_BACK, COMMAND_AHEAD = 4, 6  # "clip it" comes a few seconds after the moment
PEAK_HOLD = 5   # after the score first crosses the threshold, wait this long and fire at the strongest second
SETTLE = 6  # seconds of chat that must have arrived after a second before it is judged (reactions lag)
SOLO_MIN, SOLO_FACTOR = 0.3, 0.8
CHAT_KNOWN = 120  # seconds of chat history before a quiet chat lets a loud sound count on its own  # a signal counts as present from 0.3; alone it is scaled down by 0.8


@dataclass
class Trigger:
    t: float
    score: float
    signals: dict[str, float]
    reasons: list[str] = field(default_factory=list)


def segment_loudness(path: Path, start: float, sr: int = 16000) -> dict[int, float]:
    """Per-second RMS level (dBFS) of a segment's audio, keyed by absolute stream second."""
    cp = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vn", "-ac", "1",
                         "-ar", str(sr), "-f", "s16le", "-"], capture_output=True)
    x = np.frombuffer(cp.stdout, np.int16).astype(np.float64) / 32768.0
    out: dict[int, float] = {}
    if not len(x):
        return out
    # segment times are not whole seconds: place each sample at its absolute stream time
    t = start + np.arange(len(x)) / sr
    sec = np.floor(t).astype(np.int64)
    for s in np.unique(sec):
        v = x[sec == s]
        if len(v) >= sr // 4:
            rms = float(np.sqrt(np.mean(v * v)))
            out[int(s)] = 20 * np.log10(rms) if rms > 1e-6 else -120.0
    return out


class TriggerDetector:
    def __init__(self, cfg: LiveCfg, chat_cfg: ChatCfg):
        self.cfg = cfg
        self.audio: dict[int, float] = {}
        self.chat: dict[int, int] = defaultdict(int)
        self.hype: dict[int, int] = defaultdict(int)
        self.command: dict[int, int] = defaultdict(int)
        self.has_chat = False
        self.next_second = 0
        self.last_moment_end = -1e9
        self.history: list[tuple[int, float, dict[str, float]]] = []  # (second, score, signals) for the log
        self._armed: list[tuple[int, float, dict[str, float]]] = []     # seconds above threshold awaiting the peak
        kw = [k.lower() for k in chat_cfg.keywords]
        self._hype_single = {k for k in kw if " " not in k}
        self._hype_phrase = [k for k in kw if " " in k]
        cmds = [c.lower() for c in cfg.clip_commands]
        self._cmd_single = {c.lstrip("!") for c in cmds if " " not in c}
        self._cmd_phrase = [c for c in cmds if " " in c]

    # ------------------------------------------------------------ input
    def add_audio(self, levels: dict[int, float]) -> None:
        self.audio.update(levels)

    def add_chat(self, t: float, text: str) -> None:
        s = int(t)
        if not self.has_chat:
            self.first_chat = t
        self.has_chat = True
        self.chat[s] += 1
        low = text.lower()
        toks = _tokens(low)
        joined = " ".join(toks)
        if any(c in toks for c in self._cmd_single) or any(p in joined for p in self._cmd_phrase):
            self.command[s] += 1
        elif any(k in toks for k in self._hype_single) or any(p in joined for p in self._hype_phrase):
            self.hype[s] += 1

    # ------------------------------------------------------------ scoring
    def _window(self, d: dict[int, int], a: int, b: int) -> int:
        return sum(d.get(i, 0) for i in range(a, b))

    def signals_at(self, s: int) -> dict[str, float]:
        c = self.cfg
        base_from = max(0, s - int(c.baseline_seconds))
        sig = {"audio": 0.0, "chat": 0.0, "hype": 0.0, "command": 0.0}
        base_audio = [self.audio[i] for i in range(base_from, s - 3) if i in self.audio]
        near = [self.audio[i] for i in range(s - 1, s + 2) if i in self.audio]
        if len(base_audio) >= 10 and near:
            jump = max(near) - float(np.median(base_audio))
            sig["audio"] = float(np.clip(jump / c.audio_jump_db, 0.0, 2.0))
        if self.has_chat:
            span = max(1, s - base_from)
            rate5 = self._window(self.chat, base_from, s) / span * CHAT_WINDOW
            self._last_rate5 = rate5
            self._chat_age = s - getattr(self, "first_chat", s)
            now5 = self._window(self.chat, s, s + CHAT_WINDOW)
            if now5 >= c.chat_min_messages and span >= 10:
                # the floor on the baseline keeps a near-empty chat from turning a handful of messages into a
                # huge relative spike: chat_min_messages reaches full strength only from a quiet start
                lift = now5 / max(rate5, c.chat_min_messages / c.chat_lift)
                sig["chat"] = float(np.clip((lift - 1) / (c.chat_lift - 1), 0.0, 1.5))
            hype5 = self._window(self.hype, s, s + CHAT_WINDOW)
            if hype5 >= c.hype_min_messages:
                sig["hype"] = float(np.clip(hype5 / max(float(c.hype_min_messages), 1.5 * rate5), 0.0, 1.5))
            cmd = self._window(self.command, s - COMMAND_BACK, s + COMMAND_AHEAD)
            sig["command"] = 1.0 if cmd >= c.clip_command_count else 0.4 * cmd / c.clip_command_count
        return sig

    def score(self, sig: dict[str, float]) -> float:
        if not self.has_chat:  # audio is the only signal: a jump of audio_jump_db alone reaches threshold 1.0
            return sig["audio"]
        combined = sum(self.cfg.weights.get(k, 0.0) * v for k, v in sig.items())
        # one signal on its own is weak evidence (a busy chat, a loud game sound): it has to be agreed with,
        # except viewers asking for a clip, which is the most direct signal there is
        active = [k for k, v in sig.items() if v >= SOLO_MIN]
        if len(active) == 1 and active[0] != "command":
            combined *= SOLO_FACTOR
        # a huge audio jump (2 x audio_jump_db) counts on its own only where chat is too quiet to react; in a busy
        # chat a loud game sound nobody reacts to is not a moment (on Ludwig's stream two thirds of the moments
        # were exactly that)
        # until chat has been read for live.chat_known_seconds its "normal" is unknown (and live chat arrives in late
        # batches at first), so it is not treated as quiet: on Ludwig the first three moments were audio-only
        quiet = (getattr(self, "_chat_age", 0) >= self.cfg.chat_known_seconds
                 and getattr(self, "_last_rate5", 0.0) < self.cfg.chat_min_messages / self.cfg.chat_lift)
        return max(combined, (0.5 if quiet else 0.4) * sig["audio"])

    def evaluate(self, head: float) -> list[Trigger]:
        """Judge every second whose chat and audio have settled; return the triggers found.

        When the score crosses the threshold, the next ``PEAK_HOLD`` seconds are judged too and the trigger is
        placed at the strongest one, so the saved window centres on the moment rather than its first sign."""
        out: list[Trigger] = []
        ready = int(head) - (SETTLE if self.has_chat else 2)
        while self.next_second <= ready:
            s = self.next_second
            self.next_second += 1
            if s < self.cfg.warmup_seconds:
                continue
            sig = self.signals_at(s)
            sc = self.score(sig)
            self.history.append((s, round(sc, 3), sig))
            if self._armed:
                if sc >= self.cfg.threshold:
                    self._armed.append((s, sc, sig))
                if s - self._armed[0][0] >= PEAK_HOLD:
                    out.append(self._fire())
            elif sc >= self.cfg.threshold and s >= self.last_moment_end + self.cfg.cooldown_seconds:
                self._armed = [(s, sc, sig)]
        return out

    def flush(self) -> list[Trigger]:
        """Fire a trigger that is still waiting for its peak (end of stream)."""
        return [self._fire()] if self._armed else []

    def _fire(self) -> Trigger:
        s, sc, sig = max(self._armed, key=lambda x: x[1])
        self._armed = []
        reasons = [k for k, v in sorted(sig.items(), key=lambda kv: -kv[1]) if v >= 0.3]
        self.last_moment_end = s + self.cfg.post_seconds
        return Trigger(float(s), round(sc, 3), {k: round(v, 3) for k, v in sig.items()}, reasons)

    def forget_before(self, s: int) -> None:
        """Drop per-second data older than the baseline window (keeps memory flat on all-day streams)."""
        for d in (self.audio, self.chat, self.hype, self.command):
            for k in [k for k in d if k < s]:
                del d[k]
        if len(self.history) > 2000:
            self.history = self.history[-1000:]
