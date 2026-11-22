"""Highlight engine: transcript, audio, visual and chat signals with configurable weights.

Candidates are built from whole sentences (so a clip never starts or ends
mid-word) and from peaks in the audio, visual and chat signals, scored with
explainable features, snapped to nearby scene cuts, then de-duplicated.

This is deliberately a transparent heuristic. Signals that are not available
for a source (no chat file, visual analysis off) drop out of the weighting
instead of counting as zero. The optional LLM pass (``llm.py``) re-ranks the
top candidates using the features and contributions stored here.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

import numpy as np

from clipstudio.audio_analysis.loudness import slice_db
from clipstudio.config import HighlightsCfg
from clipstudio.core.models import Candidate, CandidateSet, LoudnessTrack, Transcript, VisualTrack, Word

# phrase -> weight. Multi-word phrases are matched on normalised text.
REACTION_LEXICON: dict[str, float] = {
    "oh my god": 2.0, "oh my gosh": 1.5, "no way": 1.8, "what the": 1.5, "are you kidding": 1.5,
    "let's go": 1.8, "lets go": 1.8, "holy": 1.5, "insane": 1.5, "crazy": 1.2, "wow": 1.2, "bro": 0.8,
    "dude": 0.8, "yo": 0.6, "wait": 0.8, "what": 0.5, "nooo": 1.8, "no no no": 1.8, "oh no": 1.5,
    "did you see": 1.5, "look at": 0.8, "unbelievable": 1.5, "clutch": 1.2, "run": 0.6, "go go": 1.2,
    "absolutely": 0.6, "help": 0.8, "wtf": 1.5, "omg": 1.5, "dead": 0.8, "yes": 0.5,
}
# reactions nobody says in ordinary talk; "insane", "crazy", "what" or "bro" alone are just how streamers speak
STRONG_REACTIONS = ("oh my god", "oh my gosh", "no way", "what the", "are you kidding", "let's go", "lets go",
                    "holy", "nooo", "no no no", "oh no", "did you see", "unbelievable", "wtf", "omg")
LAUGH_WORDS = {"haha", "hahaha", "lol", "lmao", "laughing", "hehe", "ahaha"}
ACTION_WORDS = {"exploded", "explosion", "explode", "crash", "crashed", "shoot", "shot", "police", "cops",
                "chase", "wanted", "killed", "kill", "died", "fire", "car", "heist", "bomb", "jump"}
TERMINAL = re.compile(r"[.!?…]['\")\]]*$")


@dataclass
class Signals:
    """Optional non-transcript signals for one source."""
    visual: VisualTrack | None = None
    chat_rate: np.ndarray | None = None
    chat_hype: np.ndarray | None = None
    chat_window: float = 5.0
    snap_to_cuts: bool = True
    chat_react: np.ndarray | None = None   # per-second count of reaction messages (laughter, shock)
    game_subs: list[bool] | None = None    # the game's own subtitle is on screen, one flag per 1/game_subs_fps s
    game_subs_fps: float = 4.0
    game_onsets: list[int] | None = None   # samples where a new subtitle line appears

    def streamer_words(self, sents: list["Sentence"], t0: float, t1: float) -> str:
        """The words spoken in [t0, t1] by the streamer (sentences of game characters left out)."""
        from clipstudio.visual_analysis.subtitles import is_game_line
        out = []
        for s in sents:
            if s.end < t0 or s.start > t1:
                continue
            if self.game_subs and is_game_line(self.game_subs, self.game_onsets, self.game_subs_fps, s.start, s.end):
                continue
            out += [x.text for x in s.words if t0 <= x.start <= t1]
        return " ".join(out)
    # live mode: the moment (source seconds) the trigger fired for, already corrected for chat lag
    anchor: float | None = None

    @property
    def has_chat(self) -> bool:
        return self.chat_rate is not None and float(self.chat_rate.sum()) > 0


EVENT_WEIGHT = {"hud_text": 1.0, "desaturation": 0.8, "flash": 0.55}


def visual_features(v: VisualTrack, start: float, end: float) -> dict[str, float]:
    i0, i1 = int(start * v.sample_fps), int(end * v.sample_fps) + 1
    mo = np.asarray(v.motion[i0:i1], np.float32)
    if mo.size:
        top = np.sort(mo)[-max(1, int(len(mo) * 0.3)):].mean()
        act = _clip01((float(top) / max(1.0, v.motion_median) - 1.0) / 2.0)
    else:
        act = 0.0
    ev = [EVENT_WEIGHT[e.kind] * (e.strength if e.kind != "hud_text" else 1.0)
          for e in v.events if e.start < end and e.end > start]
    return {"visual_action": round(act, 4), "game_event": round(max(ev, default=0.0), 4)}


def _event_label(v: VisualTrack | None, start: float, end: float) -> str:
    if v is None:
        return ""
    hud = [e.label for e in v.events if e.kind == "hud_text" and start <= e.start <= end]
    if hud:
        return hud[0]
    kinds = {e.kind for e in v.events if e.start < end and e.end > start}
    return "death/kill screen" if "desaturation" in kinds else ("flash" if "flash" in kinds else "")


def _snap(t: float, cuts: list[float], lo: float, hi: float, sents: list["Sentence"]) -> float:
    """Nearest scene cut within [t+lo, t+hi] that does not fall inside a sentence; else t."""
    best = t
    for c in cuts:
        if t + lo <= c <= t + hi and not any(s.start < c < s.end for s in sents):
            if best == t or abs(c - t) < abs(best - t):
                best = c
    return best


@dataclass
class Sentence:
    start: float
    end: float
    words: list[Word] = field(default_factory=list)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def build_sentences(tr: Transcript, pause_split: float = 0.8) -> list[Sentence]:
    sents: list[Sentence] = []
    cur: list[Word] = []
    for w in tr.words():
        if cur and (w.start - cur[-1].end) > pause_split:
            sents.append(Sentence(cur[0].start, cur[-1].end, cur))
            cur = []
        cur.append(w)
        if TERMINAL.search(w.text):
            sents.append(Sentence(cur[0].start, cur[-1].end, cur))
            cur = []
    if cur:
        sents.append(Sentence(cur[0].start, cur[-1].end, cur))
    return sents


def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z' ]+", " ", text.lower()).replace("  ", " ") + " "


def reaction_score(text: str) -> float:
    t = _norm(text)
    s = sum(w * t.count(f" {p} ") for p, w in REACTION_LEXICON.items())
    s += sum(1.5 for tok in t.split() if tok in LAUGH_WORDS)
    s += 0.5 * text.count("!") + 0.15 * text.count("?")
    return s


def is_spoken_reaction(text: str) -> bool:
    """Is this the streamer reacting out loud (not just talking)? Needs a strong reaction phrase or laughter,
    and enough of it: one "what the" in a question is conversation."""
    t = _norm(text)
    strong = sum(t.count(f" {p} ") for p in STRONG_REACTIONS) + sum(1 for tok in t.split() if tok in LAUGH_WORDS)
    return strong >= 1 and reaction_score(text) >= 2.0 and (strong >= 2 or text.count("!") >= 1
                                                              or reaction_score(text) >= 3.0)


def held_words(text: str, words: list[str]) -> list[str]:
    """Which of the hold words (nudity / sexual content) appear in ``text`` as whole words."""
    t = _norm(text)
    return [w for w in words if f" {w.lower()} " in t]


def _sat(x: float, k: float) -> float:
    """Saturating map [0, inf) -> [0, 1)."""
    return 1.0 - math.exp(-max(0.0, x) / k)


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x)))


def compute_features(sents: list[Sentence], loud: LoudnessTrack, start: float, end: float,
                     prev_end: float, next_start: float) -> dict[str, float]:
    dur = max(end - start, 1e-6)
    text = " ".join(s.text for s in sents)
    words = [w for s in sents for w in s.words]

    db = slice_db(loud, start, end)
    if db.size:
        top = np.sort(db)[-max(1, int(len(db) * 0.3)):]
        intensity = _clip01((float(top.mean()) - loud.median_db) / 12.0)
        # spike: largest jump vs the trailing 5 s level, within the window
        look = max(1, int(5.0 / loud.window))
        full = np.asarray(loud.rms_db)
        i0 = int(start / loud.window)
        jumps = []
        for k in range(len(db)):
            gi = i0 + k
            base = full[max(0, gi - look):gi]
            if base.size:
                # baseline never below the source's typical level, so speech onset after a pause isn't a 'spike'
                jumps.append(db[k] - max(float(np.median(base)), loud.median_db))
        spike = _clip01(max(jumps, default=0.0) / 15.0)
    else:
        intensity = spike = 0.0

    reaction = _sat(reaction_score(text) / (dur / 10.0), 2.5)
    wps = len(words) / dur
    speech = _clip01((wps - 1.0) / 2.0)

    hook_words = [w for w in words if w.start < start + 3.0]
    hook_db = slice_db(loud, start, min(end, start + 3.0))
    hook_int = _clip01((float(hook_db.max()) - loud.median_db) / 12.0) if hook_db.size else 0.0
    hook = 0.5 * _sat(reaction_score(" ".join(w.text for w in hook_words)), 1.5) + 0.5 * hook_int

    complete = 0.0
    if words and TERMINAL.search(words[-1].text):
        complete += 0.5
    if start - prev_end >= 0.3 or prev_end <= 0:
        complete += 0.25
    if next_start - end >= 0.3:
        complete += 0.25

    spoken = sum(w.end - w.start for w in words)
    silence_frac = _clip01(1.0 - spoken / dur)
    mean_prob = float(np.mean([w.probability for w in words])) if words else 0.0
    return {
        "audio_intensity": round(intensity, 4), "audio_spike": round(spike, 4),
        "reaction_language": round(reaction, 4), "speech_energy": round(speech, 4),
        "opening_hook": round(hook, 4), "completeness": round(complete, 4),
        "silence_fraction": round(silence_frac, 4), "mean_word_probability": round(mean_prob, 4),
    }


def payoff_time(sents: list[Sentence], loud: LoudnessTrack, start: float, end: float) -> float:
    """The strongest moment inside [start, end]: a clear loudness jump, else the most reactive sentence."""
    db = slice_db(loud, start, end)
    if db.size and float(db.max()) - loud.median_db >= 6.0:
        return start + (int(np.argmax(db)) + 0.5) * loud.window
    return _peak_sentence(sents, loud).start if sents else start + 0.5 * (end - start)


def position_score(t: float, start: float, end: float) -> float:
    """1 when ``t`` sits where a payoff belongs (35-80 % into the clip: a setup before, a reaction after),
    falling to 0 at the very start (no setup) and the very end (no reaction)."""
    pos = (t - start) / max(1e-6, end - start)
    if pos < 0 or pos > 1:
        return 0.0
    if pos < 0.35:
        return _clip01((pos - 0.03) / 0.32)
    if pos > 0.80:
        return _clip01((0.98 - pos) / 0.18)
    return 1.0


def tighten(start: float, end: float, sents: list[Sentence], loud: LoudnessTrack, keep: float, min_dur: float,
            protect: float | None = None) -> tuple[float, float]:
    """Cut quiet, speechless seconds off both ends (leaving ``keep`` s), never below ``min_dur``."""
    w = loud.window
    db = slice_db(loud, start, end)
    if not db.size:
        return start, end
    quiet = db < loud.median_db + 3.0
    lead = 0
    while lead < len(quiet) and quiet[lead]:
        lead += 1
    trail = 0
    while trail < len(quiet) - lead and quiet[len(quiet) - 1 - trail]:
        trail += 1
    s2, e2 = start + max(0.0, lead * w - keep), end - max(0.0, trail * w - keep)
    words = [x for s in sents for x in s.words if x.end > start and x.start < end]
    if words:  # speech is never trimmed
        s2, e2 = min(s2, words[0].start - 0.25), max(e2, words[-1].end + 0.6)
    if protect is not None:
        s2, e2 = min(s2, protect - 2.0), max(e2, protect + 2.0)
    s2, e2 = max(start, s2), min(end, e2)
    short = min_dur - (e2 - s2)
    if short > 0:  # give back what the minimum length needs, the start first (the setup)
        back = min(short, s2 - start)
        s2 -= back
        e2 = min(end, e2 + short - back)
    return s2, e2


EVIDENCE_AUDIO_DB = 8.0   # a jump this far over the level just before it (and over the source's typical level)
EVIDENCE_SPEECH = 1.4     # reaction_score of the words around the moment ("oh my god", "no way", laughter)
EVIDENCE_CHAT = 3         # reaction messages in the CHAT_WINDOW after the moment, and a fifth of all messages then
CHAT_WINDOW = 20.0        # seconds: typing time plus stream delay (YouTube chat runs 10-20 s behind the picture)
EVIDENCE_EVENT = 0.5      # on-screen game event weight (death screen, HUD text, flash)
REACTIONS = ("speech", "chat")


def evidence_at(t: float, sents: list[Sentence], loud: LoudnessTrack, sig: "Signals") -> list[str]:
    """Signs of a payoff at source time ``t``: any of "audio", "speech", "chat", "game event".

    Everything is measured right around ``t``: the cause just before or at it, the reactions just after.
    A loud sound early in a clip and a "wow" twenty seconds later are two unrelated things, not one moment."""
    ev = []
    w = loud.window
    db = np.asarray(loud.rms_db, np.float32)
    i0, i1 = max(0, int((t - 3) / w)), min(len(db), int((t + 3) / w) + 1)
    if i1 > i0:
        before = db[max(0, int((t - 12) / w)): i0]
        base = max(float(np.median(before)) if before.size else loud.median_db, loud.median_db)
        if float(db[i0:i1].max()) - base >= EVIDENCE_AUDIO_DB:
            ev.append("audio")
    # only the streamer's own words count as a reaction: a game character shouting is part of the game
    if is_spoken_reaction(sig.streamer_words(sents, t - 2, t + 8)):
        ev.append("speech")
    # the loudest moments of the source, not just louder than the second before (any sentence after a pause is)
    if "audio" in ev and float(db[i0:i1].max()) < loud.p90_db:
        ev.remove("audio")
    if sig.has_chat:
        a, b = max(0, int(t)), min(len(sig.chat_rate), int(t + CHAT_WINDOW) + 1)
        if b > a:
            total = float(sig.chat_rate[a:b].sum())
            if sig.chat_react is not None:
                react = float(sig.chat_react[a:b].sum())
                if react >= EVIDENCE_CHAT and react >= 0.2 * total:
                    ev.append("chat")
            else:  # no message texts: fall back to a real burst of messages
                burst = float(np.convolve(sig.chat_rate, np.ones(5, np.float32), mode="same")[a:b].max())
                if burst >= 5 and burst >= 2.5 * (float(np.median(sig.chat_rate)) * 5 + 1):
                    ev.append("chat")
    if sig.visual is not None and any(EVENT_WEIGHT[e.kind] * (e.strength if e.kind != "hud_text" else 1.0)
                                      >= EVIDENCE_EVENT and t - 5 <= e.start <= t + 3 for e in sig.visual.events):
        ev.append("game event")
    return ev


def find_payoff(sents: list[Sentence], loud: LoudnessTrack, sig: "Signals", start: float, end: float) -> tuple[float, list[str]]:
    """(time, signs) of the strongest moment in [start, end]: most signs at once, a reaction preferred.

    In live mode the search stays near the trigger's moment, which is what viewers reacted to."""
    lo, hi = start + 1.0, end - 1.0
    near = sig.anchor if sig.anchor is not None and start <= sig.anchor <= end else None
    if near is not None:
        # the trigger is placed on chat's reaction, which can run well behind the picture: look further back
        lo, hi = max(lo, near - 14.0), min(hi, near + 6.0)
    ref = near if near is not None else start + 0.6 * (end - start)
    best_t, best_ev, best_key = ref, [], (-1.0, -1.0, 0.0)
    t = lo
    while t <= hi + 1e-6:
        ev = evidence_at(t, sents, loud, sig)
        key = (1.0 if any(e in REACTIONS for e in ev) else 0.0, float(len(ev)), -abs(t - ref))
        if key > best_key:
            best_t, best_ev, best_key = t, ev, key
        t += 1.0
    return round(best_t, 3), best_ev


def cause_time(t: float, loud: LoudnessTrack, sig: "Signals", back: float = 25.0) -> float | None:
    """When the thing being reacted to at ``t`` happened: the biggest sudden jump in loudness, or an on-screen
    game event, in the ``back`` seconds before ``t``. None when nothing stands out (the cause is then
    usually something said, which the lead-in over the preceding talk covers)."""
    w = loud.window
    db = np.asarray(loud.rms_db, np.float32)
    found: list[float] = []
    best_u, best_jump = None, EVIDENCE_AUDIO_DB
    u = max(0.0, t - back)
    while u <= t + 1.0:
        i = int(u / w)
        seg, before = db[i: i + max(1, int(1.0 / w))], db[max(0, i - int(8.0 / w)): i]
        if seg.size and before.size:
            jump = float(seg.max()) - max(float(np.median(before)), loud.median_db)
            if jump > best_jump:
                best_u, best_jump = u, jump
        u += 1.0
    if best_u is not None:
        found.append(best_u)
    if sig.visual is not None:
        found += [e.start for e in sig.visual.events
                  if EVENT_WEIGHT[e.kind] * (e.strength if e.kind != "hud_text" else 1.0) >= EVIDENCE_EVENT
                  and t - back <= e.start <= t + 1.0]
    return min(found) if found else None


def lead_in_start(t: float, start: float, sents: list[Sentence], lead: float, lead_max: float) -> float:
    """Where a clip about the moment at ``t`` should start so the viewer has the context for it.

    At least ``lead`` seconds before ``t``, then further back over the talk that runs straight into it
    (sentences less than 2 s apart), up to ``lead_max`` seconds before ``t``. Never mid-sentence."""
    new = min(start, t - lead)
    for s in sorted(sents, key=lambda s: -s.start):
        if s.end < new - 2.0 or s.start >= new:
            continue
        if t - (s.start - 0.25) <= lead_max:
            new = s.start - 0.25          # this sentence leads into the clip: take all of it
        elif s.start < new < s.end:
            new = s.end + 0.1             # too far back: start right after it instead of inside it
            break
        else:
            break
    return max(0.0, new)


def extend_setup(start: float, end: float, v: VisualTrack, sents: list[Sentence], max_back: float,
                 max_dur: float) -> float:
    """New start time: pulled back over the build-up to the clip, or ``start`` when there is none.

    Walks back one second at a time while the game picture is clearly in motion (twice the source's typical
    level), allowing two calm seconds in a row, and stops at a scene cut, at ``max_back`` seconds and at
    ``max_dur`` total length. The result never lands inside a sentence."""
    fps = v.sample_fps
    mo = np.asarray(v.motion, np.float32)
    if max_back <= 0 or not mo.size or start < 1.0:
        return start
    thr = 2.0 * max(v.motion_median, 1e-6)

    def active(t0: float) -> bool:
        seg = mo[int(t0 * fps): int((t0 + 1.0) * fps)]
        return bool(seg.size) and float(seg.mean()) >= thr

    if not (active(start) or active(max(0.0, start - 1.0))):
        return start  # the clip itself does not open on action: nothing to build up to
    limit = max(0.0, start - max_back, end - max_dur)
    cut = max((c for c in v.scene_cuts if limit < c <= start - 0.5), default=None)
    if cut is not None:
        limit = cut
    new, calm, t = start, 0, start - 1.0
    while t >= limit - 1e-6:
        if active(t):
            new, calm = t, 0
        else:
            calm += 1
            if calm > 2:
                break
        t -= 1.0
    new = max(new, limit)
    for s in sents:  # do not start mid-sentence: take the whole sentence if it fits, else start after it
        if s.start < new < s.end:
            new = s.start - 0.25 if s.start - 0.25 >= max(0.0, limit - 2.0) else s.end + 0.1
    return round(min(start, max(0.0, new)), 3) if start - new >= 1.0 else start


def _category(text: str, f: dict[str, float]) -> str:
    toks = set(_norm(text).split())
    if toks & LAUGH_WORDS:
        return "funny"
    if f["reaction_language"] >= 0.45 or f["audio_spike"] >= 0.6:
        return "reaction"
    if toks & ACTION_WORDS:
        return "action"
    return "commentary"


def _peak_sentence(sents: list[Sentence], loud: LoudnessTrack) -> Sentence:
    def strength(s: Sentence) -> float:
        db = slice_db(loud, s.start, s.end)
        return reaction_score(s.text) + (max(0.0, float(db.max()) - loud.median_db) / 6.0 if db.size else 0.0)
    return max(sents, key=lambda s: (strength(s), len(s.words)))


def _title(sents: list[Sentence], loud: LoudnessTrack) -> str:
    best = _peak_sentence(sents, loud)
    t = re.sub(r"\s+", " ", best.text).strip(" ,")
    if len(t) > 60:
        t = t[:57].rsplit(" ", 1)[0] + "..."
    return t[:1].upper() + t[1:]


def _fmt(t: float) -> str:
    return f"{int(t // 3600):d}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}" if t >= 3600 else f"{int(t // 60):d}:{int(t % 60):02d}"


def _overlap(a: Candidate, b: Candidate) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(1e-6, min(a.duration, b.duration))


def is_duplicate(a: Candidate, b: Candidate, max_overlap: float) -> bool:
    """Same event if they overlap heavily, or if either clip contains the other's peak moment."""
    if _overlap(a, b) > max_overlap:
        return True
    return (b.start <= a.peak_time <= b.end) or (a.start <= b.peak_time <= a.end)


def _audio_peaks(loud: LoudnessTrack, min_rise_db: float = 6.0, radius_s: float = 5.0) -> list[float]:
    """Times of local loudness maxima that stand clearly above the source's typical level."""
    db = np.asarray(loud.rms_db)
    r = max(1, int(radius_s / loud.window))
    peaks = []
    for k in range(len(db)):
        if db[k] < loud.median_db + min_rise_db:
            continue
        lo, hi = max(0, k - r), min(len(db), k + r + 1)
        if db[k] >= db[lo:hi].max() and (not peaks or (k * loud.window - peaks[-1]) > radius_s):
            peaks.append(k * loud.window)
    return peaks


def generate_candidates(tr: Transcript, loud: LoudnessTrack, cfg: HighlightsCfg, media_duration: float,
                        signals: Signals | None = None) -> CandidateSet:
    """Two candidate sources, scored the same way and de-duplicated together:

    * sentence windows: whole sentences grouped to the target length (padded with
      surrounding context when speech is short, e.g. a lone "I'm dead bro");
    * audio-peak windows: built around loud moments, so gameplay with little or no
      commentary still produces candidates. Window edges never cut a sentence.
    """
    sig = signals or Signals()
    weights = cfg.weights.model_dump()
    # signals that don't exist for this source drop out instead of scoring zero
    if sig.visual is None:
        weights.pop("visual_action"), weights.pop("game_event")
    if not sig.has_chat:
        weights.pop("chat_activity")
    if sig.anchor is None:
        weights.pop("trigger_moment")
    wsum = sum(weights.values()) or 1.0
    cuts = sig.visual.scene_cuts if (sig.visual is not None and sig.snap_to_cuts) else []
    sents = build_sentences(tr)
    raw: list[Candidate] = []
    span = max(1e-6, cfg.maximum_duration - cfg.minimum_duration)

    def make(start: float, end: float, source: str, peak: float | None = None) -> None:
        start, end = max(0.0, start), min(media_duration, end)
        if cuts:
            s2 = _snap(start, cuts, -1.5, 0.8, sents)
            e2 = _snap(end, cuts, -0.8, 1.5, sents)
            if cfg.minimum_duration - 1e-6 <= e2 - s2 <= cfg.maximum_duration + 1e-6 and (peak is None or s2 <= peak <= e2):
                start, end = s2, e2
        if cfg.tighten_edges and end - start <= cfg.maximum_duration + 1e-6:
            inside = [s for s in sents if s.start >= start - 1e-3 and s.end <= end + 1e-3]
            keep_pt = peak if peak is not None else (sig.anchor if sig.anchor is not None and start <= sig.anchor <= end
                                                     else None)
            floor = max(cfg.minimum_duration, 0.75 * cfg.target_duration)
            start, end = tighten(start, end, inside, loud, cfg.edge_keep, floor, keep_pt)
        # the moment this clip is about, and enough before it to understand it / after it to see the reaction
        pay, evidence = find_payoff(sents, loud, sig, start, end)
        if peak is not None and not evidence:
            pay = peak
        if evidence:
            start = min(start, lead_in_start(pay, start, sents, cfg.lead_in_seconds, cfg.lead_in_max))
            cause = cause_time(pay, loud, sig)
            if cause is not None and cause - cfg.cause_lead < start:
                start = max(0.0, cause - cfg.cause_lead)   # open before the cause, not on its aftermath
                for snt in sents:
                    if snt.start < start < snt.end:
                        start = max(0.0, snt.start - 0.25)
            end = max(end, min(media_duration, pay + cfg.reaction_tail))
            for snt in sents:  # the reaction is not cut off mid-sentence
                if snt.start < end < snt.end and snt.end + cfg.tail - start <= cfg.maximum_duration:
                    end = min(media_duration, snt.end + cfg.tail)
            if end - start > cfg.maximum_duration:
                start = end - cfg.maximum_duration
                for snt in sents:
                    if snt.start < start < snt.end:
                        start = snt.end + 0.1
        if sig.visual is not None and cfg.setup_seconds > 0:
            start = extend_setup(start, end, sig.visual, sents, cfg.setup_seconds, cfg.maximum_duration)
        dur = end - start
        if dur < cfg.minimum_duration - 1e-6 or dur > cfg.maximum_duration + 1e-6:
            return
        group = [s for s in sents if s.start >= start - 1e-3 and s.end <= end + 1e-3]
        prev_end = max((s.end for s in sents if s.end <= start + 1e-3), default=0.0)
        next_start = min((s.start for s in sents if s.start >= end - 1e-3), default=media_duration)
        f = compute_features(group, loud, start, end, prev_end, next_start)
        if sig.visual is not None:
            f.update(visual_features(sig.visual, start, end))
        if sig.has_chat:
            from clipstudio.chat.replay import chat_score
            f["chat_activity"] = round(chat_score(sig.chat_rate, sig.chat_hype, start, end, sig.chat_window), 4)
        f["payoff_time"] = round(pay - start, 3)
        f["payoff_position"] = round(position_score(pay, start, end), 4)
        if sig.anchor is not None:
            f["trigger_moment"] = round(position_score(sig.anchor, start, end), 4)
        contrib = {k: round(weights[k] * f[k] / wsum, 4) for k in weights}
        score = sum(contrib.values())
        score *= 1.0 - 0.15 * abs(dur - cfg.target_duration) / span
        dead_air = f["silence_fraction"] > 0.4 and f["audio_intensity"] < 0.3
        if dead_air:
            score *= 1.0 - 0.5 * (f["silence_fraction"] - 0.4)
        f["payoff_evidence"] = float(len(evidence))
        reacted = any(e in REACTIONS for e in evidence)
        weak = cfg.min_evidence > 0 and (len(evidence) < cfg.min_evidence or (cfg.require_reaction and not reacted))
        why_not = ""
        # an active chat that did not react: nobody watching thought it was a moment
        chat_live = sig.has_chat and sig.chat_react is not None and float(sig.chat_rate.sum()) >= 25
        if cfg.min_evidence > 0 and cfg.chat_must_react and chat_live and "chat" not in evidence:
            weak, why_not = True, "chat did not react"
        if sig.visual is not None and sig.visual.brightness:
            br = np.asarray(sig.visual.brightness[int(start * sig.visual.sample_fps): int(end * sig.visual.sample_fps) + 1])
            f["dark_share"] = round(float((br < 22).mean()), 3) if br.size else 0.0   # near black, not just night
            if cfg.min_evidence > 0 and f["dark_share"] > cfg.max_dark_share:
                weak, why_not = True, f"{f['dark_share']:.0%} of it is too dark to read"
        held = held_words(" ".join(x.text for s in sents for x in s.words if start - 3 <= x.start <= end + 3), cfg.hold_words)
        if held:
            f["content_hold"] = 1.0
            weak, why_not = True, "possible nudity or sexual content (" + ", ".join(sorted(set(held))[:3]) + "): a person must look"
        if weak:
            f["payoff_evidence"] = min(f["payoff_evidence"], float(cfg.min_evidence - 1))  # reads as "not enough"
            score *= 0.6 if not evidence else 0.8
        if group:
            agreement = 1.0 - abs(f["audio_intensity"] - f["reaction_language"])
            confidence = f["mean_word_probability"] * (0.6 + 0.4 * agreement)
        else:
            confidence = 0.35 + 0.3 * f["audio_intensity"]  # audio-only evidence is weaker
        text = " ".join(s.text for s in group)
        top = sorted(contrib.items(), key=lambda kv: -kv[1])[:3]
        reason = ("Strongest signals: " + ", ".join(f"{k.replace('_', ' ')} ({f[k]:.2f})" for k, _ in top)
                  + ("" if source == "speech" else f"; found from a{'n' if source[0] in 'aeiou' else ''} {source} peak"))
        ev_label = _event_label(sig.visual, start, end)
        if ev_label and f.get("game_event", 0) >= 0.5:
            reason += f"; on-screen event: {ev_label}"
        if dead_air:
            reason += f"; penalised for {f['silence_fraction']:.0%} dead air"
        reason += "; payoff seen in: " + (", ".join(evidence) if evidence else "nothing")
        if evidence:
            reason += f" at {_fmt(pay)}"
        if weak and why_not:
            reason += f" ({why_not}: not queued)"
        elif weak:
            reason += (f" (needs {cfg.min_evidence} together" + (", one of them a reaction" if cfg.require_reaction else "")
                       + ": probably not a real moment)")
        pk = pay if evidence else (peak if peak is not None else (_peak_sentence(group, loud).start if group else start))
        if group and peak is None:
            title = _title(group, loud)
        elif group:
            title = _title(group, loud)
        elif ev_label and f.get("game_event", 0) >= 0.5:
            title = f"{ev_label.title()} at {_fmt(pk)}"
        else:
            title = f"{'Chat spike' if source == 'chat' else 'Loud moment'} at {_fmt(pk)} (no speech)"
        cat = _category(text, f) if group else ("action" if f["audio_spike"] >= 0.5 else "commentary")
        if f.get("game_event", 0) >= 0.75 and cat == "commentary":
            cat = "action"
        cid = "c-" + hashlib.sha1(f"{start:.2f}-{end:.2f}".encode()).hexdigest()[:6]
        raw.append(Candidate(
            id=cid, rank=0, start=round(start, 3), end=round(end, 3), duration=round(dur, 3),
            peak_time=round(pk, 3), title=title,
            description=f"{cat.title()} moment at {_fmt(start)}: " + (group[0].text[:120] if group else "no commentary"),
            category=cat, score=round(score, 4), passes_threshold=score >= cfg.minimum_score and not weak,
            confidence=round(confidence, 4), features=f, contributions=contrib, reason=reason, transcript=text,
        ))

    # 1) sentence windows
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            if j > i and sents[j].start - sents[j - 1].end > cfg.max_internal_silence:
                break  # don't bridge long dead air
            prev_end = sents[i - 1].end if i > 0 else 0.0
            next_start = sents[j + 1].start if j + 1 < len(sents) else media_duration
            start = max(prev_end, sents[i].start - cfg.lead_in, 0.0)
            end = min(next_start, sents[j].end + cfg.tail, media_duration)
            if end - start > cfg.maximum_duration:
                break
            if end - start < cfg.minimum_duration:
                if j + 1 < len(sents) and sents[j + 1].start - sents[j].end <= cfg.max_internal_silence:
                    continue  # the next sentence can still extend this window
                # pad with surrounding context (setup before, reaction after) without touching other speech
                need = cfg.minimum_duration - (end - start)
                room_before, room_after = start - prev_end, next_start - end
                before = min(room_before, max(need * 0.6, need - room_after))
                after = min(room_after, need - before)
                start, end = start - before, end + after
            make(start, end, "speech")

    # 2) peak windows: loud audio, on-screen events, chat spikes
    seeds = [(pk, "audio") for pk in _audio_peaks(loud)]
    if sig.visual is not None:
        seeds += [(e.start, "on-screen event") for e in sig.visual.events if EVENT_WEIGHT[e.kind] * e.strength >= 0.4]
    if sig.has_chat:
        from clipstudio.chat.replay import chat_peaks
        seeds += [(t, "chat") for t in chat_peaks(sig.chat_rate, sig.chat_window)]
    if sig.anchor is not None and 0 <= sig.anchor <= media_duration:
        seeds.append((sig.anchor, "trigger"))
    for pk, src in seeds:
        # the moment sits ~60 % in: room for the setup before it and the reaction after it
        start = pk - (0.6 if src == "trigger" else 0.4) * cfg.target_duration
        # a scene cut in the pre-roll is a natural start: the shot that leads into the moment
        pre = [c for c in cuts if start <= c <= pk - 1.0]
        if pre:
            start = pre[-1]
        end = start + cfg.target_duration
        for snt in sents:  # never cut a sentence at either edge
            if snt.start < start < snt.end:
                start = snt.start - cfg.lead_in
            if snt.start < end < snt.end:
                end = snt.end + cfg.tail
        if start < 0:
            end, start = end - start, 0.0
        if end > media_duration:
            start, end = max(0.0, start - (end - media_duration)), media_duration
        make(start, end, src, peak=pk)

    raw.sort(key=lambda c: -c.score)
    chosen: list[Candidate] = []
    for c in raw:
        if cfg.remove_duplicates and any(is_duplicate(c, k, cfg.duplicate_overlap) for k in chosen):
            continue
        chosen.append(c)
        if len(chosen) >= cfg.max_candidates:
            break
    for r, c in enumerate(chosen, 1):
        c.rank = r
    return CandidateSet(weights=weights, candidates=chosen)
