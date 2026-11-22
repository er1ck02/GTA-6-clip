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
        pay = peak if peak is not None else payoff_time(group, loud, start, end)
        f["payoff_position"] = round(position_score(pay, start, end), 4)
        if sig.anchor is not None:
            f["trigger_moment"] = round(position_score(sig.anchor, start, end), 4)
        contrib = {k: round(weights[k] * f[k] / wsum, 4) for k in weights}
        score = sum(contrib.values())
        score *= 1.0 - 0.15 * abs(dur - cfg.target_duration) / span
        dead_air = f["silence_fraction"] > 0.4 and f["audio_intensity"] < 0.3
        if dead_air:
            score *= 1.0 - 0.5 * (f["silence_fraction"] - 0.4)
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
        pk = peak if peak is not None else (_peak_sentence(group, loud).start if group else start)
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
            category=cat, score=round(score, 4), passes_threshold=score >= cfg.minimum_score,
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
