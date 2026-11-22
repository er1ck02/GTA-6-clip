"""Typed data structures shared between modules and persisted as JSON.

Every stage writes one of these to disk, which is what makes the pipeline
resumable: a later stage only needs the JSON, not the earlier stage's code.
"""
from __future__ import annotations

from clipstudio.core.compat import write_text_atomic

from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def save(self, path: Path) -> None:
        write_text_atomic(path, self.model_dump_json(indent=2))  # atomic: a crash never leaves half-written JSON

    @classmethod
    def load(cls: type[T], path: Path) -> T:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- media
class AudioStream(_M):
    order: int  # position among audio streams (ffmpeg 0:a:<order>)
    codec: str
    channels: int | None = None
    title: str | None = None  # stream title / handler name, e.g. "Mic", "Game"


class MediaInfo(_M):
    path: str
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    has_audio: bool
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None
    audio_streams: list[AudioStream] = Field(default_factory=list)
    size_bytes: int
    container: str
    title: str | None = None
    source_url: str | None = None


# --------------------------------------------------------------------------- transcript
class Word(_M):
    text: str
    start: float
    end: float
    probability: float = 1.0


class Segment(_M):
    id: int
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    speaker: str | None = None


class Transcript(_M):
    backend: str
    model: str
    language: str
    language_probability: float
    duration: float
    segments: list[Segment]

    def words(self) -> list[Word]:
        return [w for s in self.segments for w in s.words]

    def words_between(self, start: float, end: float) -> list[Word]:
        return [w for w in self.words() if w.end > start and w.start < end]


# --------------------------------------------------------------------------- audio analysis
class LoudnessTrack(_M):
    window: float
    rms_db: list[float]  # one value per window, dBFS
    median_db: float
    p90_db: float


# --------------------------------------------------------------------------- highlights
class Candidate(_M):
    id: str
    rank: int
    start: float
    end: float
    duration: float
    peak_time: float  # the moment the clip is 'about'; used for de-duplication
    title: str
    # where the title came from: a transcript line (fallback), the LLM re-rank, or the user
    title_source: Literal["transcript", "llm", "user"] = "transcript"
    description: str
    category: Literal["reaction", "funny", "action", "commentary"]
    score: float
    passes_threshold: bool
    confidence: float
    features: dict[str, float]
    contributions: dict[str, float]
    reason: str
    transcript: str
    status: Literal["pending", "accepted", "rejected"] = "pending"
    user_adjusted: bool = False
    # sections removed from inside the clip (absolute source seconds), e.g. dead air between two moments
    cuts: list[tuple[float, float]] = Field(default_factory=list)

    @property
    def cut_seconds(self) -> float:
        return round(sum(b - a for a, b in self.cuts), 3)

    @property
    def output_duration(self) -> float:
        return round(self.end - self.start - self.cut_seconds, 3)

    def rel_cuts(self) -> list[tuple[float, float]]:
        """Cuts relative to the clip start (what the renderer works in)."""
        return [(round(a - self.start, 3), round(b - self.start, 3)) for a, b in self.cuts]


class CandidateSet(_M):
    weights: dict[str, float]
    candidates: list[Candidate]

    def get(self, cid: str) -> Candidate:
        for c in self.candidates:
            if c.id == cid or str(c.rank) == cid:
                return c
        raise KeyError(f"no candidate {cid!r}")


# --------------------------------------------------------------------------- reframing
class PixelRect(_M):
    x: int
    y: int
    w: int
    h: int


class CropKeyframe(_M):
    """A crop window in source pixels at clip-relative time ``t``.

    Phase 2 plans are static (one keyframe). Phase 4 tracking emits many and
    the renderer will interpolate between them.
    """

    t: float
    gameplay: PixelRect
    facecam: PixelRect | None = None


class Panel(_M):
    name: Literal["gameplay", "facecam", "background"]
    # destination rectangle in output pixels
    dest: PixelRect


class CropPlan(_M):
    mode: str
    source_width: int
    source_height: int
    out_width: int
    out_height: int
    panels: list[Panel]
    keyframes: list[CropKeyframe]
    # output-space regions that captions must not cover (e.g. the face)
    protected_regions: list[PixelRect] = Field(default_factory=list)
    caption_band: tuple[int, int]  # allowed vertical band (top, bottom) for captions
    # the facecam source crop is fitted inside its panel (over a blurred fill) instead of filling it
    facecam_fit: bool = False
    source: Literal["auto", "manual"] = "auto"
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- vision
class NormRect(_M):
    """Rectangle in normalised source coordinates (0..1)."""
    x: float
    y: float
    w: float
    h: float


class FacecamDetection(_M):
    region: NormRect | None = None       # the facecam overlay box
    face: NormRect | None = None         # median face box inside it
    confidence: float = 0.0
    method: Literal["face+border", "face", "border_only", "fullframe_face", "none", "unavailable"] = "none"
    detector: str = "none"
    samples: int = 0
    face_hit_rate: float = 0.0
    border_score: float = 0.0
    notes: list[str] = Field(default_factory=list)


class TrackPoint(_M):
    t: float          # clip-relative seconds
    cx: float         # normalised horizontal centre of the action (raw, before smoothing)
    confidence: float


class ActionTrack(_M):
    """Where the action is over time inside one clip, and the smoothed crop centre path."""
    method: str
    sample_fps: float
    raw: list[TrackPoint] = Field(default_factory=list)
    smoothed: list[tuple[float, float]] = Field(default_factory=list)  # (t, cx) keyframes after smoothing
    notes: list[str] = Field(default_factory=list)


class VisualEvent(_M):
    kind: Literal["flash", "desaturation", "hud_text"]
    start: float
    end: float
    strength: float
    label: str = ""


class VisualTrack(_M):
    sample_fps: float
    motion: list[float] = Field(default_factory=list)
    motion_median: float = 0.0
    brightness: list[float] = Field(default_factory=list)
    saturation: list[float] = Field(default_factory=list)
    scene_cuts: list[float] = Field(default_factory=list)
    events: list[VisualEvent] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ChatMessage(_M):
    t: float
    user: str = ""
    text: str


class ChatTrack(_M):
    source: str
    format: str
    messages: list[ChatMessage] = Field(default_factory=list)


# --------------------------------------------------------------------------- captions
class CaptionWord(_M):
    text: str
    start: float  # clip-relative
    end: float


class CaptionChunk(_M):
    index: int
    start: float
    end: float
    lines: list[list[CaptionWord]]

    @property
    def text(self) -> str:
        return "\n".join(" ".join(w.text for w in line) for line in self.lines)


class CaptionLayout(_M):
    font_family: str
    font_file: str | None
    font_size_px: int
    center_x: int
    baseline_y: int
    line_height_px: int
    max_text_width_px: int
    measured: bool  # True when widths came from real font metrics


class CaptionTrack(_M):
    preset: str
    chunks: list[CaptionChunk]
    layout: CaptionLayout
    source_word_count: int
    edited: bool = False


# --------------------------------------------------------------------------- QC
class QCCheck(_M):
    name: str
    group: Literal["video", "audio", "captions", "tracking"]
    status: Literal["pass", "warn", "fail", "skip"]
    detail: str
    value: float | str | None = None


class QCReport(_M):
    clip_id: str
    output: str
    passed: bool
    checks: list[QCCheck]

    def summary(self) -> str:
        counts = {s: sum(1 for c in self.checks if c.status == s) for s in ("pass", "warn", "fail", "skip")}
        return ", ".join(f"{v} {k}" for k, v in counts.items() if v)


def normalize_cuts(cuts: list, start: float, end: float, min_len: float = 0.2) -> list[tuple[float, float]]:
    """Clamp cuts to (start, end), drop tiny ones, merge overlaps, sort."""
    out: list[list[float]] = []
    for a, b in sorted((float(min(c)), float(max(c))) for c in cuts):
        a, b = max(a, start), min(b, end)
        if b - a < min_len:
            continue
        if out and a <= out[-1][1] + 0.05:  # touching cuts become one
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(round(a, 3), round(b, 3)) for a, b in out]


def cut_time(t: float, cuts: list[tuple[float, float]]) -> float:
    """Map a source time to the time it plays at once ``cuts`` are removed (times inside a cut map to its edge)."""
    return t - sum(min(max(t - a, 0.0), b - a) for a, b in cuts)


def remap_words(words: list[Word], cuts: list[tuple[float, float]]) -> list[Word]:
    """Drop words inside cut sections and pull later words back, so times match the cut clip."""
    if not cuts:
        return words
    out = []
    for w in words:
        mid = (w.start + w.end) / 2
        if any(a <= mid < b for a, b in cuts):
            continue
        s, e = cut_time(w.start, cuts), cut_time(w.end, cuts)
        out.append(w.model_copy(update={"start": round(s, 3), "end": round(max(s + 0.05, e), 3)}))
    return out
