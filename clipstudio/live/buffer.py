"""Record a live stream into short segments and keep only a rolling window of them.

FFmpeg's segment muxer writes ``seg_000001.ts``... with stream copy (no re-encode, so recording costs almost no
CPU) and appends a line to a CSV list as each segment is finished. :class:`Ring` keeps the newest
``buffer_seconds`` of segments and deletes the rest, except segments a pending moment has pinned.

Inputs:
* a local file, read in real time (``-re``): simulates a live stream for tests and dry runs;
* a stream URL (Twitch, YouTube, Kick...): opened by streamlink when installed, otherwise yt-dlp, piped into
  FFmpeg. Only public streams; no cookies, no access-control bypass.
"""
from __future__ import annotations

import csv
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from clipstudio.core.errors import DependencyMissing, MediaError
from clipstudio.core.logging import get_logger
from clipstudio.media.ffmpeg import ffmpeg_bin

log = get_logger("live")


@dataclass
class Segment:
    index: int
    path: Path
    start: float     # stream time (seconds since recording started)
    end: float
    arrived: float   # wall clock when the segment was finished

    @property
    def duration(self) -> float:
        return self.end - self.start


def _ytdlp_cmd() -> list[str]:
    if shutil.which("yt-dlp"):
        return ["yt-dlp"]
    try:
        import yt_dlp  # noqa: F401
    except ImportError as e:
        raise DependencyMissing("live mode needs streamlink or yt-dlp: pip install -e \".[ingest]\"") from e
    return [sys.executable, "-m", "yt_dlp"]


def stream_reader_cmd(url: str, max_height: int = 1080) -> list[str]:
    """A command that writes the live stream (muxed video + audio, MPEG-TS) to stdout."""
    if shutil.which("streamlink"):
        return ["streamlink", "--stdout", "--loglevel", "warning", url, f"{max_height}p,{max_height}p60,best"]
    return _ytdlp_cmd() + ["--quiet", "--no-warnings", "--no-part", "--no-playlist",
                           "-f", f"b[height<={max_height}]/b", "-o", "-", url]


class Recorder:
    def __init__(self, work: Path, segment_seconds: float = 2.0, *, file: Path | None = None,
                 url: str | None = None, realtime: bool = True):
        if (file is None) == (url is None):
            raise ValueError("give exactly one of file or url")
        self.work = work
        self.segment_seconds = segment_seconds
        self.file, self.url, self.realtime = file, url, realtime
        self.list_path = work / "segments.csv"
        self._reader: subprocess.Popen | None = None
        self._ff: subprocess.Popen | None = None
        self._seen = 0
        self.started = 0.0

    def start(self) -> None:
        self.work.mkdir(parents=True, exist_ok=True)
        for old in self.work.glob("seg_*.ts"):
            old.unlink()
        self.list_path.unlink(missing_ok=True)
        out = ["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment",
               "-segment_time", f"{self.segment_seconds:g}", "-segment_format", "mpegts",
               "-segment_list", str(self.list_path), "-segment_list_type", "csv",
               "-segment_list_flags", "+live", str(self.work / "seg_%06d.ts")]
        base = [ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.file is not None:
            cmd = base + (["-re"] if self.realtime else []) + ["-i", str(self.file)] + out
            self._ff = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        else:
            self._reader = subprocess.Popen(stream_reader_cmd(self.url), stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE)
            cmd = base[:-1] + ["-i", "pipe:0"] + out
            self._ff = subprocess.Popen(cmd, stdin=self._reader.stdout, stderr=subprocess.PIPE)
            self._reader.stdout.close()  # ffmpeg owns the pipe now
        self.started = time.time()
        log.info("recording %s into %s (%gs segments)", self.file or self.url, self.work, self.segment_seconds)

    @property
    def running(self) -> bool:
        return self._ff is not None and self._ff.poll() is None

    def poll(self) -> list[Segment]:
        """Segments finished since the last call."""
        if not self.list_path.exists():
            return []
        try:
            rows = list(csv.reader(self.list_path.read_text().splitlines()))
        except OSError:
            return []
        new = []
        now = time.time()
        for row in rows[self._seen:]:
            if len(row) < 3:
                break  # line still being written
            try:
                name, a, b = row[0], float(row[1]), float(row[2])
            except ValueError:
                break
            idx = int(Path(name).stem.split("_")[-1])
            new.append(Segment(idx, self.work / Path(name).name, a, b, now))
            self._seen += 1
        return new

    def error_text(self) -> str:
        parts = []
        for p in (self._reader, self._ff):
            if p is not None and p.poll() is not None and p.stderr is not None:
                try:
                    parts.append(p.stderr.read().decode(errors="replace").strip())
                except (OSError, ValueError):
                    pass
        return "\n".join(x for x in parts if x)

    def stop(self) -> None:
        for p in (self._reader, self._ff):
            if p is not None and p.poll() is None:
                p.terminate()
        for p in (self._reader, self._ff):
            if p is not None:
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()


class Ring:
    """The newest ``buffer_seconds`` of segments; older ones are deleted unless pinned by a pending moment."""

    def __init__(self, buffer_seconds: float):
        self.buffer_seconds = buffer_seconds
        self.segments: deque[Segment] = deque()
        self.pins: dict[str, tuple[float, float]] = {}
        self.deleted = 0

    @property
    def head(self) -> float:
        return self.segments[-1].end if self.segments else 0.0

    def add(self, seg: Segment) -> None:
        self.segments.append(seg)

    def pin(self, key: str, start: float, end: float) -> None:
        self.pins[key] = (start, end)

    def unpin(self, key: str) -> None:
        self.pins.pop(key, None)

    def _pinned(self, s: Segment) -> bool:
        return any(s.end > a and s.start < b for a, b in self.pins.values())

    def prune(self) -> int:
        """Delete segments that ended more than ``buffer_seconds`` before the newest one."""
        limit = self.head - self.buffer_seconds
        keep: deque[Segment] = deque()
        n = 0
        for s in self.segments:
            if s.end < limit and not self._pinned(s):
                s.path.unlink(missing_ok=True)
                n += 1
            else:
                keep.append(s)
        self.segments = keep
        self.deleted += n
        return n

    def covering(self, start: float, end: float) -> list[Segment]:
        return [s for s in self.segments if s.end > start and s.start < end]

    def stream_time_at(self, wall: float) -> float:
        """Stream time that was arriving at wall-clock ``wall`` (maps live chat onto the video)."""
        if not self.segments:
            return 0.0
        for s in reversed(self.segments):
            if s.arrived <= wall:
                return max(0.0, s.end + (wall - s.arrived))
        s = self.segments[0]
        return max(0.0, s.end - (s.arrived - wall))

    def disk_bytes(self) -> int:
        return sum(s.path.stat().st_size for s in self.segments if s.path.exists())


def concat_segments(segs: list[Segment], dst: Path) -> Path:
    """Join segments into one MP4 without re-encoding."""
    if not segs:
        raise MediaError("no recorded segments cover this moment (they may have been deleted)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    lst = dst.with_suffix(".txt")
    lst.write_text("".join(f"file '{s.path.resolve().as_posix()}'\n" for s in segs))
    tmp = dst.with_name(dst.stem + ".partial.mp4")
    cp = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                         "-i", str(lst), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart",
                         str(tmp)], capture_output=True)
    if cp.returncode != 0:  # audio that is not AAC rejects the bitstream filter
        cp = subprocess.run([ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                             "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(tmp)], capture_output=True)
    lst.unlink(missing_ok=True)
    if cp.returncode != 0:
        raise MediaError(f"could not join segments: {cp.stderr.decode(errors='replace')[-400:]}")
    tmp.replace(dst)
    return dst
