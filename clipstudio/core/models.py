"""Typed data structures shared between modules and persisted as JSON.

Every stage writes one of these to disk, which is what makes the pipeline
resumable: a later stage only needs the JSON, not the earlier stage's code.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T", bound=BaseModel)


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)  # atomic: a crash never leaves half-written JSON

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
    source: Literal["auto", "manual"] = "auto"
    notes: list[str] = Field(default_factory=list)


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
