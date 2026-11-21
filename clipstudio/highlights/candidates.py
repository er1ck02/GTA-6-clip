"""Phase 2 highlight engine: transcript + audio signals, configurable weights.

Candidates are built from whole sentences so a clip never starts or ends
mid-word, scored with explainable features, then de-duplicated.

This is deliberately a transparent heuristic. Phase 3 adds visual and chat
signals and an optional LLM re-ranking pass over the top candidates; the
features and per-feature contributions stored here are the LLM's input.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field

import numpy as np

from clipstudio.audio_analysis.loudness import slice_db
from clipstudio.config import HighlightsCfg
from clipstudio.core.models import Candidate, CandidateSet, LoudnessTrack, Transcript, Word

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


def generate_candidates(tr: Transcript, loud: LoudnessTrack, cfg: HighlightsCfg, media_duration: float) -> CandidateSet:
    weights = cfg.weights.model_dump()
    wsum = sum(weights.values())
    sents = build_sentences(tr)
    raw: list[Candidate] = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            if j > i and sents[j].start - sents[j - 1].end > cfg.max_internal_silence:
                break  # don't bridge long dead air
            prev_end = sents[i - 1].end if i > 0 else 0.0
            next_start = sents[j + 1].start if j + 1 < len(sents) else media_duration
            start = max(prev_end, sents[i].start - cfg.lead_in, 0.0)
            end = min(next_start, sents[j].end + cfg.tail, media_duration)
            dur = end - start
            if dur > cfg.maximum_duration:
                break
            if dur < cfg.minimum_duration:
                continue
            group = sents[i:j + 1]
            f = compute_features(group, loud, start, end, prev_end, next_start)
            contrib = {k: round(weights[k] * f[k] / wsum, 4) for k in weights}
            score = sum(contrib.values())
            span = max(1e-6, cfg.maximum_duration - cfg.minimum_duration)
            score *= 1.0 - 0.15 * abs(dur - cfg.target_duration) / span
            if f["silence_fraction"] > 0.4:
                score *= 1.0 - 0.5 * (f["silence_fraction"] - 0.4)
            agreement = 1.0 - abs(f["audio_intensity"] - f["reaction_language"])
            confidence = f["mean_word_probability"] * (0.6 + 0.4 * agreement)
            text = " ".join(s.text for s in group)
            top = sorted(contrib.items(), key=lambda kv: -kv[1])[:3]
            reason = "Strongest signals: " + ", ".join(f"{k.replace('_', ' ')} ({f[k]:.2f})" for k, _ in top)
            if f["silence_fraction"] > 0.4:
                reason += f"; penalised for {f['silence_fraction']:.0%} silence"
            cid = "c-" + hashlib.sha1(f"{start:.2f}-{end:.2f}".encode()).hexdigest()[:6]
            raw.append(Candidate(
                id=cid, rank=0, start=round(start, 3), end=round(end, 3), duration=round(dur, 3),
                peak_time=round(_peak_sentence(group, loud).start, 3), title=_title(group, loud), description=f"{_category(text, f).title()} moment at {_fmt(start)}: {group[0].text[:120]}",
                category=_category(text, f), score=round(score, 4), passes_threshold=score >= cfg.minimum_score,
                confidence=round(confidence, 4), features=f, contributions=contrib, reason=reason, transcript=text,
            ))

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
