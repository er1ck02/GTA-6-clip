"""Source ingestion: local files and (optionally) URLs through yt-dlp.

Local ingestion never copies the source; the project records its path and a
content fingerprint, so a moved or edited file is detected on resume.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

from clipstudio.core.errors import DependencyMissing, MediaError
from clipstudio.core.logging import get_logger
from clipstudio.media.ffmpeg import probe

log = get_logger("ingestion")

VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".flv", ".ts", ".m4v", ".avi"}
_CHUNK = 4 * 1024 * 1024


def file_fingerprint(path: Path) -> str:
    """Fast content fingerprint: size + first and last 4 MiB. Stable across renames."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(_CHUNK))
        if size > _CHUNK:
            f.seek(max(size - _CHUNK, _CHUNK))
            h.update(f.read(_CHUNK))
    return h.hexdigest()


def job_id_for_file(path: Path) -> str:
    stem = "".join(c if c.isalnum() else "-" for c in path.stem.lower()).strip("-")[:24] or "video"
    return f"{stem}-{file_fingerprint(path)[:10]}"


def job_id_for_url(url: str) -> str:
    return "url-" + hashlib.sha256(url.strip().encode()).hexdigest()[:12]


def validate_local(path: Path) -> Path:
    path = path.expanduser()
    if not path.exists():
        raise MediaError(f"input file not found: {path}")
    if not path.is_file():
        raise MediaError(f"input is not a file: {path}")
    if path.suffix.lower() not in VIDEO_EXTS:
        log.warning("unusual extension %s; trying anyway", path.suffix)
    info = probe(path)  # raises MediaError with a readable reason
    if not info.has_audio:
        log.warning("%s has no audio track: captions and speech-based highlights will be empty", path.name)
    return path.resolve()


def download_url(url: str, dest_dir: Path) -> Path:
    """Download a VOD with yt-dlp (resumable: yt-dlp keeps .part files).

    Only public or user-authorised content. No DRM or access-control bypass.
    """
    if shutil.which("yt-dlp") is None:
        try:
            import yt_dlp  # noqa: F401
        except ImportError as e:
            raise DependencyMissing("URL ingestion needs yt-dlp: pip install -r requirements/ingest.txt") from e
        cmd = [sys.executable, "-m", "yt_dlp"]
    else:
        cmd = ["yt-dlp"]
    dest_dir.mkdir(parents=True, exist_ok=True)
    args = cmd + [
        "--no-playlist", "--continue", "--no-overwrites", "--restrict-filenames",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--write-info-json",
        "-o", str(dest_dir / "source.%(ext)s"),
        url,
    ]
    log.info("downloading %s", url)
    cp = subprocess.run(args, capture_output=True, text=True)
    if cp.returncode != 0:
        tail = "\n".join(cp.stderr.strip().splitlines()[-8:])
        raise MediaError(f"download failed for {url}:\n{tail}")
    files = [p for p in dest_dir.glob("source.*") if p.suffix.lower() in VIDEO_EXTS]
    if not files:
        raise MediaError(f"yt-dlp finished but no video file was written to {dest_dir}")
    return files[0]
