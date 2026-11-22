"""Typed, validated configuration.

Precedence (lowest to highest):
  1. Defaults defined in the pydantic models below.
  2. Processing profile (fast / balanced / quality) presets.
  3. YAML files passed with ``--config`` (merged left to right), or
     ``configs/default.yaml`` when none are given and it exists.
  4. ``--set dotted.key=value`` CLI overrides.
  5. A few environment variables (see ``ENV_OVERRIDES``).

Business logic only ever receives a validated :class:`Config` object.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ProjectCfg(_Strict):
    name: str = "GTA 6 Clip Studio"


class PathsCfg(_Strict):
    data_dir: str = "data"
    logs_dir: str = "logs"
    models_dir: str = "models"


class ProcessingCfg(_Strict):
    profile: Literal["fast", "balanced", "quality"] = "balanced"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    workers: int | Literal["auto"] = "auto"
    resume_enabled: bool = True
    cache_enabled: bool = True


class TranscriptionCfg(_Strict):
    backend: Literal["faster_whisper"] = "faster_whisper"
    # A faster-whisper model name ("small", "large-v3-turbo", ...) or a local
    # directory holding a CTranslate2 conversion.
    model: str = "small"
    compute_type: str = "auto"  # auto -> int8 on CPU, float16 on CUDA
    language: str | None = None  # None = auto-detect
    beam_size: int = Field(5, ge=1, le=10)
    vad_filter: bool = True
    word_timestamps: bool = True
    # which audio stream(s) to transcribe: "auto" picks a stream titled Mic/Voice if present,
    # otherwise mixes all; "mix" always mixes; or an audio stream number (0, 1, ...)
    speech_track: str | int = "auto"
    normalize_speech: bool = True  # level quiet mic tracks for recognition (output audio untouched)
    # long audio is transcribed in chunks cut at quiet moments; each chunk is cached so an
    # interrupted multi-hour run resumes where it stopped. 0 = never chunk.
    chunk_seconds: int = Field(600, ge=0)


class AudioAnalysisCfg(_Strict):
    window_seconds: float = Field(0.5, gt=0.05, le=5.0)


class ScoringWeights(_Strict):
    """Relative weights. They are normalised at scoring time, so only ratios matter."""

    audio_intensity: float = Field(0.25, ge=0)
    audio_spike: float = Field(0.15, ge=0)
    reaction_language: float = Field(0.25, ge=0)
    speech_energy: float = Field(0.10, ge=0)
    opening_hook: float = Field(0.15, ge=0)
    completeness: float = Field(0.10, ge=0)
    # used only when the source has that signal (visual analysis on / a chat replay file)
    visual_action: float = Field(0.10, ge=0)
    game_event: float = Field(0.20, ge=0)
    chat_activity: float = Field(0.20, ge=0)
    # where the strongest moment sits in the clip: best with a setup before it and a reaction after it
    payoff_position: float = Field(0.15, ge=0)
    # live mode only: how well the clip frames the moment that set off the trigger
    trigger_moment: float = Field(0.40, ge=0)

    @model_validator(mode="after")
    def _not_all_zero(self) -> "ScoringWeights":
        base = {k: v for k, v in self.model_dump().items()
                if k not in ("visual_action", "game_event", "chat_activity", "payoff_position", "trigger_moment")}
        if sum(base.values()) <= 0:
            raise ValueError("at least one transcript/audio highlight weight must be > 0 "
                             "(visual and chat signals are not available for every source)")
        return self


class HighlightsCfg(_Strict):
    minimum_duration: float = Field(15, gt=1)
    maximum_duration: float = Field(45, gt=1)
    target_duration: float = Field(25, gt=1)
    minimum_score: float = Field(0.55, ge=0, le=1)
    max_candidates: int = Field(10, ge=1, le=200)
    remove_duplicates: bool = True
    duplicate_overlap: float = Field(0.3, ge=0, le=1)
    max_internal_silence: float = Field(4.0, gt=0)
    lead_in: float = Field(0.25, ge=0, le=3)
    tail: float = Field(0.6, ge=0, le=3)
    # cut quiet, speechless seconds off the start and end of a candidate (never below three quarters of
    # target_duration: quiet gameplay is often the setup; never the peak moment), so a clip opens on something happening and ends when the reaction does
    tighten_edges: bool = True
    # A clip needs a payoff, and a payoff shows up in more than one place at once. Four independent signs are
    # checked: an audio peak, a spoken reaction (reaction words, laughter, shouting), a real burst of chat,
    # an on-screen game event. A candidate with fewer than this many is marked as not passing and loses score;
    # live mode does not edit it at all. 0 turns the check off.
    min_evidence: int = Field(2, ge=0, le=4)
    edge_keep: float = Field(2.5, ge=0, le=5)   # quiet seconds left at each edge after tightening
    weights: ScoringWeights = ScoringWeights()

    @model_validator(mode="after")
    def _durations(self) -> "HighlightsCfg":
        if not (self.minimum_duration <= self.target_duration <= self.maximum_duration):
            raise ValueError(
                "highlights durations must satisfy minimum_duration <= target_duration <= maximum_duration"
            )
        return self


class Rect(_Strict):
    """A rectangle in normalised source coordinates (0..1)."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def _inside(self) -> "Rect":
        if self.x + self.w > 1.0001 or self.y + self.h > 1.0001:
            raise ValueError("rect must lie inside the frame (x+w <= 1, y+h <= 1)")
        return self


class ReframingCfg(_Strict):
    # gameplay:   9:16 crop of the gameplay, full screen.
    # fit_blur:   whole 16:9 frame, letterboxed over a blurred fill (never cuts action).
    # facecam_split: facecam panel on top, gameplay crop below.
    # facecam_pip:   gameplay crop full screen with the facecam as a picture-in-picture box.
    # auto:          facecam_split when a facecam is detected (or configured), else gameplay.
    mode: Literal["auto", "gameplay", "fit_blur", "facecam_split", "facecam_pip"] = "auto"
    gameplay_center_x: float = Field(0.5, ge=0, le=1)
    # follow the action with the gameplay crop (needs vision.enabled); off = locked crop at gameplay_center_x.
    # Off by default: games keep the player centred, and stream overlays (ads, alerts, chat) attract the tracker.
    tracking: bool = False
    # manual facecam box; overrides automatic detection
    facecam_region: Rect | None = None
    facecam_panel_ratio: float = Field(0.3334, ge=0.2, le=0.6)  # top third: 1080x640 over 1080x1280
    pip_width_ratio: float = Field(0.40, ge=0.2, le=0.6)
    pip_corner: Literal["top_left", "top_right"] = "top_left"
    # keep the gameplay crop off the facecam overlay when that costs at most this much horizontal shift
    avoid_facecam_shift: float = Field(0.12, ge=0, le=0.5)
    # move and slightly zoom the locked gameplay crop so stream overlays (sponsor banners, alert boxes, a second
    # camera) stay out of the picture; the zoom never exceeds overlay_max_zoom on top of the normal crop
    avoid_overlays: bool = True
    overlay_max_zoom: float = Field(1.3, ge=1.0, le=1.6)


class VisualCfg(_Strict):
    """Whole-video visual signals for highlight detection (scene cuts, motion, flashes, death screens, HUD text)."""
    enabled: bool = True
    sample_fps: float = Field(4.0, gt=0, le=15)
    width: int = Field(128, ge=32, le=640)
    cut_threshold: float = Field(0.35, gt=0, le=1)   # histogram change (0..1) that can be a cut
    flash_rise: float = Field(0.45, gt=0)            # brightness rise over the minute's median
    desat_ratio: float = Field(0.45, gt=0, lt=1)     # saturation below this fraction of the median
    min_desat_seconds: float = Field(1.2, gt=0)
    snap_to_cuts: bool = True                        # move clip edges onto nearby scene cuts
    # optional OCR of on-screen text at event moments (needs Tesseract + pytesseract)
    ocr: Literal["auto", "on", "off"] = "auto"
    ocr_words: list[str] = Field(default_factory=lambda: ["WASTED", "BUSTED", "MISSION PASSED", "MISSION FAILED",
                                                          "ELIMINATED", "VICTORY", "DEFEAT", "KNOCKED"])
    max_ocr_frames: int = Field(60, ge=0)


class ChatCfg(_Strict):
    """Chat replay signal. A file next to the video (<name>.chat.json / .live_chat.json / .chat.csv) is used
    automatically; or pass --chat."""
    enabled: bool = True
    window_seconds: float = Field(5.0, gt=0)
    keywords: list[str] = Field(default_factory=lambda: [
        "lol", "lmao", "lul", "kekw", "omegalul", "pog", "pogchamp", "poggers", "pogu", "clip", "clip it",
        "w", "ww", "www", "no way", "wtf", "holy", "insane", "oof", "f", "ez", "gg", "monkas", "pepelaugh"])


class LLMProvider(_Strict):
    kind: Literal["anthropic", "openai_compatible", "ollama"]
    model: str
    base_url: str | None = None          # openai_compatible / ollama endpoint
    api_key_env: str | None = None       # name of the environment variable holding the key (never the key)


class LLMCfg(_Strict):
    """Optional re-ranking of the top heuristic candidates by a language model.

    Off by default. When on, the top candidates' transcripts and signal summaries are sent to the
    first provider that answers; if every provider fails, the heuristic ranking is kept.
    """
    # "auto": on as soon as a provider's API key is set in the environment (off without one)
    enabled: bool | Literal["auto"] = "auto"
    providers: list[LLMProvider] = Field(default_factory=lambda: [
        LLMProvider(kind="anthropic", model="claude-opus-5-5", api_key_env="ANTHROPIC_API_KEY"),
        LLMProvider(kind="ollama", model="llama3.1:8b", base_url="http://localhost:11434"),
    ])
    top_k: int = Field(12, ge=1, le=50)
    weight: float = Field(0.5, ge=0, le=1)   # final = (1-weight)*heuristic + weight*llm
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"  # Anthropic models only
    timeout_seconds: float = Field(180, gt=0)
    retitle: bool = True                     # use the model's short title for each clip

    def active(self) -> bool:
        """Should the LLM pass run? ``auto`` means: only when a provider's key is present."""
        if self.enabled != "auto":
            return bool(self.enabled)
        return any(p.api_key_env and os.environ.get(p.api_key_env) for p in self.providers)


class VisionCfg(_Strict):
    enabled: bool = True
    face_detector: Literal["auto", "mediapipe", "opencv", "none"] = "auto"
    download_models: bool = True          # fetch the 230 KB BlazeFace model once if missing
    facecam_auto: bool = True
    facecam_min_confidence: float = Field(0.6, ge=0, le=1)
    facecam_samples: int = Field(24, ge=4, le=200)
    tracking_fps: float = Field(4.0, gt=0, le=15)
    deadzone: float = Field(0.04, ge=0, le=0.3)       # fraction of source width
    max_speed: float = Field(0.30, gt=0, le=3)        # fraction of source width per second
    smoothing_s: float = Field(0.7, ge=0, le=5)


class VideoCfg(_Strict):
    aspect_ratio: Literal["9:16"] = "9:16"
    width: int = Field(1080, ge=240, le=4320)
    height: int = Field(1920, ge=426, le=7680)
    fps: int | Literal["source"] = 60

    @field_validator("width", "height")
    @classmethod
    def _even(cls, v: int) -> int:
        if v % 2:
            raise ValueError("video dimensions must be even for H.264")
        return v

    @model_validator(mode="after")
    def _ratio(self) -> "VideoCfg":
        if abs(self.width / self.height - 9 / 16) > 0.01:
            raise ValueError("width/height must be 9:16")
        return self


class CaptionStyle(_Strict):
    font: list[str] = ["Montserrat", "Inter", "Arial", "DejaVu Sans"]
    bold: bool = True
    uppercase: bool = False
    size: float = Field(0.045, gt=0.01, le=0.15, description="font size as a fraction of frame height")
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#FFD400"
    outline_color: str = "#000000"
    outline: float = Field(0.004, ge=0, le=0.02, description="fraction of frame height")
    shadow: float = Field(0.002, ge=0, le=0.02)
    highlight_active_word: bool = True
    active_word_scale: float = Field(1.08, ge=1.0, le=1.5)
    pop_in: bool = True
    max_words: int = Field(4, ge=1, le=12)
    max_lines: int = Field(2, ge=1, le=3)
    max_chars_per_line: int = Field(18, ge=6, le=60)
    break_on_pause: float = Field(0.45, gt=0)

    @field_validator("primary_color", "highlight_color", "outline_color")
    @classmethod
    def _hex(cls, v: str) -> str:
        s = v.lstrip("#")
        if len(s) != 6 or any(c not in "0123456789abcdefABCDEF" for c in s):
            raise ValueError(f"colour must be #RRGGBB, got {v!r}")
        return "#" + s.upper()


BUILTIN_CAPTION_PRESETS: dict[str, dict[str, Any]] = {
    "clean_gaming": {"size": 0.042, "uppercase": False, "max_words": 4, "highlight_color": "#7CF7FF"},
    "bold_viral": {"size": 0.058, "uppercase": True, "max_words": 3, "max_chars_per_line": 14,
                   "font": ["Montserrat ExtraBold", "Montserrat", "Arial Black", "Impact", "Arial", "DejaVu Sans"],
                   "highlight_color": "#FFD400", "outline": 0.007, "shadow": 0.004, "active_word_scale": 1.15},
    "minimal": {"size": 0.034, "bold": False, "highlight_active_word": False, "pop_in": False,
                "max_words": 7, "max_chars_per_line": 30, "outline": 0.0025},
    "high_energy": {"size": 0.058, "uppercase": True, "max_words": 2, "max_chars_per_line": 12,
                    "highlight_color": "#39FF14", "active_word_scale": 1.2, "outline": 0.007},
}


class CaptionsCfg(_Strict):
    enabled: bool = True
    word_level: bool = True
    preset: str = "bold_viral"
    style: dict[str, Any] = Field(default_factory=dict, description="overrides applied on top of the preset")
    presets: dict[str, dict[str, Any]] = Field(default_factory=dict, description="extra user presets")
    vertical_position: float = Field(0.72, ge=0.05, le=0.95, description="baseline as fraction of height")
    safe_margin_x: float = Field(0.07, ge=0, le=0.3)
    safe_margin_top: float = Field(0.10, ge=0, le=0.3)
    safe_margin_bottom: float = Field(0.16, ge=0, le=0.4)
    profanity_filter: bool = False
    profanity_words: list[str] = Field(default_factory=lambda: ["fuck", "shit", "bitch", "cunt"])
    export_srt: bool = True
    export_ass: bool = True

    def resolved_style(self) -> CaptionStyle:
        all_presets = {**BUILTIN_CAPTION_PRESETS, **self.presets}
        if self.preset not in all_presets:
            raise ValueError(f"unknown caption preset {self.preset!r}; available: {sorted(all_presets)}")
        return CaptionStyle(**{**all_presets[self.preset], **self.style})


class AudioCfg(_Strict):
    tracks: Literal["all", "first"] = "all"  # mix every audio stream (game + mic) into the clip, or only the first
    normalization: bool = True
    target_lufs: float = Field(-14.0, ge=-30, le=-6)
    true_peak: float = Field(-1.5, ge=-6, le=0)
    loudness_range: float = Field(11.0, ge=1, le=20)
    # light: high-pass + gentle compression. strong: adds FFT noise reduction, presence EQ and a de-esser.
    voice_enhancement: Literal["off", "light", "strong"] = "light"
    # with a separate mic track: lower the game audio while the streamer talks (sidechain compression)
    duck_game: bool = True
    duck_ratio: float = Field(4.0, ge=1, le=20)

    @field_validator("voice_enhancement", mode="before")
    @classmethod
    def _yaml_off(cls, v):  # YAML reads a bare `off` as False
        return "off" if v is False else v


class EnhanceCfg(_Strict):
    """Visual polish applied to the gameplay picture at render time."""
    sharpen: float = Field(0.3, ge=0, le=1.0)         # contrast-adaptive sharpening after scaling (0 = off)
    # smooth the source's compression blocks before the crop is enlarged, so the upscale and the sharpening
    # do not magnify them (stream recordings are heavily compressed; fast motion shows it first)
    deblock: bool = True
    vibrance: float = Field(0.06, ge=0, le=0.5)      # saturation boost (0 = off)
    contrast: float = Field(0.03, ge=0, le=0.3)
    # quick zoom on the clip's peak moment and on on-screen events (scale factor, 1.0 = off)
    punch_zoom: float = Field(1.10, ge=1.0, le=1.5)
    punch_max: int = Field(3, ge=0, le=10)           # at most this many punches per clip
    # title card at the top of the gameplay picture. Shown only for a title written by a person or by the
    # LLM re-rank: the automatic fallback title is a line of the transcript, which is not a hook.
    hook_title: bool = True
    hook_title_seconds: float = Field(0, ge=0, le=600)   # 0 = stays up for the whole clip
    # drop black frames at the very start/end of a clip (loading screens, fades); skipped when you set the bounds
    trim_black_edges: bool = True


class PlatformPreset(_Strict):
    """Upload target. Limits are editable because platforms change them; check current limits before relying on one."""
    label: str
    max_duration: float = Field(gt=0)
    max_fps: int = Field(60, ge=24, le=120)
    max_video_bitrate: str = "16M"   # re-encode above this (FFmpeg -maxrate syntax)
    hashtags: list[str] = Field(default_factory=list)


DEFAULT_PLATFORMS = {
    "tiktok": {"label": "TikTok", "max_duration": 600, "max_video_bitrate": "16M", "hashtags": ["gta6", "gaming"]},
    "youtube_shorts": {"label": "YouTube Shorts", "max_duration": 180, "max_video_bitrate": "20M",
                       "hashtags": ["shorts", "gta6"]},
    "instagram_reels": {"label": "Instagram Reels", "max_duration": 180, "max_fps": 60, "max_video_bitrate": "12M",
                        "hashtags": ["reels", "gta6", "gaming"]},
}


class ExportCfg(_Strict):
    format: Literal["mp4"] = "mp4"
    video_codec: Literal["h264"] = "h264"
    encoder: Literal["auto", "libx264", "h264_nvenc"] = "auto"
    audio_codec: Literal["aac"] = "aac"
    audio_bitrate: str = "192k"
    crf: int = Field(18, ge=10, le=35)
    preset: str = "medium"
    # ceiling for the master file; platforms re-encode every upload, and anything above this is wasted on them
    max_bitrate: str = "16M"
    auto_render_top: int = Field(3, ge=0, le=50)
    # per-platform copies written next to each rendered clip ([] = master file only)
    platforms: list[str] = Field(default_factory=list)
    platform_presets: dict[str, PlatformPreset] = Field(
        default_factory=lambda: {k: PlatformPreset(**v) for k, v in DEFAULT_PLATFORMS.items()})

    @field_validator("platform_presets", mode="before")
    @classmethod
    def _merge_presets(cls, v):  # partial overrides of a built-in preset keep its other values
        if not isinstance(v, dict):
            return v
        out = {k: dict(d) for k, d in DEFAULT_PLATFORMS.items()}
        for k, d in v.items():
            d = d.model_dump() if isinstance(d, BaseModel) else dict(d or {})
            out[k] = {**out.get(k, {}), **d}
        return out

    @model_validator(mode="after")
    def _known_platforms(self):
        unknown = [p for p in self.platforms if p not in self.platform_presets]
        if unknown:
            raise ValueError(f"unknown export platform(s) {unknown}; define them under export.platform_presets "
                             f"or choose from {sorted(self.platform_presets)}")
        return self


class LiveCfg(_Strict):
    """Live mode: watch a stream with a short rolling buffer and keep only the moments that trigger."""
    buffer_seconds: float = Field(90, ge=20, le=900)     # footage older than this is deleted unless a moment needs it
    segment_seconds: float = Field(2.0, ge=1, le=10)
    pre_seconds: float = Field(35, ge=5)                  # kept before the trigger (the setup)
    post_seconds: float = Field(15, ge=3)                 # kept after the trigger (the payoff and reaction)
    threshold: float = Field(1.0, gt=0)                   # trigger score needed to save a moment
    cooldown_seconds: float = Field(45, ge=0)             # no new moment this soon after the last one's end
    baseline_seconds: float = Field(180, ge=20)           # how far back "normal" chat and audio levels are measured
    warmup_seconds: float = Field(20, ge=0)               # no triggers until the baseline has this much data
    audio_jump_db: float = Field(9.0, gt=0)               # loudness this far above the baseline counts fully
    chat_lift: float = Field(3.0, gt=1)                   # chat this many times busier than normal counts fully
    clip_commands: list[str] = Field(default_factory=lambda: ["clip", "clip it", "clip that", "clipit", "!clip"])
    clip_command_count: int = Field(3, ge=1)              # this many viewers asking for a clip within 10 s triggers
    weights: dict[str, float] = Field(default_factory=lambda: {"audio": 0.6, "chat": 0.7, "hype": 0.5, "command": 1.0})
    auto_process: bool = True                             # edit each saved moment right away
    # chat reacts a few seconds after the thing it reacts to; the clip is framed on trigger time minus this
    chat_lag_seconds: float = Field(6.0, ge=0, le=30)
    # a moment whose best clip scores below this is kept on disk but not edited or queued (0 = edit everything)
    min_clip_score: float = Field(0.35, ge=0, le=1)
    max_moments: int = Field(0, ge=0)                     # stop after this many moments (0 = no limit)
    channels: list[str] = Field(default_factory=list)     # stream URLs watched together by `live --all` / WATCH ALL
    max_streams: int = Field(8, ge=1, le=32)              # streams watched at once (recording is cheap; editing is not)
    max_parallel_edits: int = Field(1, ge=1, le=8)        # moments edited at the same time across all streams


class UploadCfg(_Strict):
    """Publishing is opt-in per streamer. Nothing is ever uploaded for a channel that is not listed here."""
    allow_channels: list[str] = Field(default_factory=list)  # streamers who allow clipping (channel names, lower case)
    platforms: list[str] = Field(default_factory=lambda: ["tiktok", "youtube_shorts", "instagram_reels"])
    require_qc_pass: bool = True


class QualityCfg(_Strict):
    duration_tolerance: float = Field(0.25, gt=0)
    max_black_seconds: float = Field(0.5, ge=0)
    lufs_min: float = -20.0
    lufs_max: float = -9.0
    max_true_peak: float = -0.5
    max_silence_seconds: float = Field(6.0, gt=0)
    av_sync_tolerance: float = Field(0.1, gt=0)
    max_crop_speed: float = Field(0.5, gt=0)  # fraction of source width per second


class Config(_Strict):
    project: ProjectCfg = ProjectCfg()
    paths: PathsCfg = PathsCfg()
    processing: ProcessingCfg = ProcessingCfg()
    transcription: TranscriptionCfg = TranscriptionCfg()
    audio_analysis: AudioAnalysisCfg = AudioAnalysisCfg()
    highlights: HighlightsCfg = HighlightsCfg()
    reframing: ReframingCfg = ReframingCfg()
    vision: VisionCfg = VisionCfg()
    visual: VisualCfg = VisualCfg()
    chat: ChatCfg = ChatCfg()
    llm: LLMCfg = LLMCfg()
    video: VideoCfg = VideoCfg()
    captions: CaptionsCfg = CaptionsCfg()
    audio: AudioCfg = AudioCfg()
    enhance: EnhanceCfg = EnhanceCfg()
    export: ExportCfg = ExportCfg()
    quality: QualityCfg = QualityCfg()
    live: LiveCfg = LiveCfg()
    upload: UploadCfg = UploadCfg()

    def section_hash(self, *sections: str) -> str:
        """Stable hash of the given config sections, used for stage cache keys."""
        data = {s: _dig(self.model_dump(mode="json"), s) for s in sections}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


PROFILES: dict[str, dict[str, Any]] = {
    "fast": {"transcription": {"model": "base", "beam_size": 1}, "export": {"preset": "veryfast", "crf": 22},
             "video": {"fps": 30}},
    "balanced": {"transcription": {"model": "small", "beam_size": 5}, "export": {"preset": "medium", "crf": 18}},
    "quality": {"transcription": {"model": "large-v3-turbo", "beam_size": 5},
                "export": {"preset": "slow", "crf": 17}},
}

ENV_OVERRIDES = {
    "CLIPSTUDIO_WHISPER_MODEL": "transcription.model",
    "CLIPSTUDIO_DEVICE": "processing.device",
    "CLIPSTUDIO_DATA_DIR": "paths.data_dir",
}


def _dig(d: dict, dotted: str) -> Any:
    for part in dotted.split("."):
        d = d[part]
    return d


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _set_dotted(d: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def parse_override(expr: str) -> tuple[str, Any]:
    if "=" not in expr:
        raise ValueError(f"override must look like key.path=value, got {expr!r}")
    key, raw = expr.split("=", 1)
    return key.strip(), yaml.safe_load(raw) if raw.strip() else None


def load_config(
    files: list[str | Path] | None = None,
    overrides: list[str] | None = None,
    profile: str | None = None,
) -> Config:
    files = [Path(f) for f in (files or [])]
    if not files and Path("configs/default.yaml").exists():
        files = [Path("configs/default.yaml")]

    user: dict[str, Any] = {}
    for f in files:
        if not f.exists():
            raise FileNotFoundError(f"config file not found: {f}")
        loaded = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config file {f} must contain a mapping at the top level")
        user = deep_merge(user, loaded)

    cli: dict[str, Any] = {}
    for expr in overrides or []:
        k, v = parse_override(expr)
        _set_dotted(cli, k, v)
    for env, dotted in ENV_OVERRIDES.items():
        if os.environ.get(env):
            _set_dotted(cli, dotted, os.environ[env])

    chosen = profile or _dig_or(cli, "processing.profile") or _dig_or(user, "processing.profile") or "balanced"
    if chosen not in PROFILES:
        raise ValueError(f"unknown profile {chosen!r}; choose from {sorted(PROFILES)}")
    merged = deep_merge(PROFILES[chosen], user)
    merged = deep_merge(merged, cli)
    _set_dotted(merged, "processing.profile", chosen)
    cfg = Config(**merged)
    cfg.captions.resolved_style()  # validate preset early
    return cfg


def _dig_or(d: dict, dotted: str) -> Any:
    try:
        return _dig(d, dotted)
    except (KeyError, TypeError):
        return None
