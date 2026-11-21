"""Environment doctor: inspects the machine and says what this install can realistically do."""
from __future__ import annotations

import importlib
import importlib.metadata as md
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clipstudio.config import Config


@dataclass
class Item:
    name: str
    status: str  # ok | warn | missing | info
    detail: str
    required: bool = False


@dataclass
class Report:
    system: dict = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.required and i.status == "missing" for i in self.items)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "system": self.system, "items": [asdict(i) for i in self.items],
                "recommendations": self.recommendations}


def _cmd(args: list[str], timeout: float = 10) -> str | None:
    try:
        cp = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return cp.stdout.strip() if cp.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _ram_gb() -> float | None:
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) / 1024 / 1024
        if sys.platform == "darwin":
            out = _cmd(["sysctl", "-n", "hw.memsize"])
            return int(out) / 1024**3 if out else None
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("t", ctypes.c_ulonglong),
                            ("a", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            s = MS()
            s.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return s.t / 1024**3
    except Exception:
        return None
    return None


def _cpu_name() -> str:
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    if sys.platform == "darwin":
        return _cmd(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor()
    return platform.processor() or platform.machine()


def _gpus() -> list[dict]:
    out = _cmd(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    if out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                gpus.append({"vendor": "nvidia", "name": parts[0], "vram_gb": round(float(parts[1]) / 1024, 1),
                             "driver": parts[2]})
    if sys.platform == "darwin" and platform.machine() == "arm64":
        gpus.append({"vendor": "apple", "name": "Apple Silicon (Metal)", "vram_gb": None, "driver": None})
    return gpus


def _pkg(name: str, import_name: str | None = None) -> str | None:
    try:
        importlib.import_module(import_name or name)
        return md.version(name)
    except Exception:
        return None


def run_doctor(cfg: Config) -> Report:
    r = Report()
    ram = _ram_gb()
    data_dir = Path(cfg.paths.data_dir).expanduser()
    probe_dir = data_dir if data_dir.exists() else Path.cwd()
    disk = shutil.disk_usage(probe_dir)
    gpus = _gpus()
    r.system = {
        "os": f"{platform.system()} {platform.release()}", "os_detail": platform.platform(),
        "python": sys.version.split()[0], "python_executable": sys.executable,
        "cpu": _cpu_name(), "cpu_cores": os.cpu_count(), "ram_gb": round(ram, 1) if ram else None,
        "disk_free_gb": round(disk.free / 1024**3, 1), "disk_checked_at": str(probe_dir.resolve()),
        "gpus": gpus,
        "container_hint": Path("/.dockerenv").exists() or bool(os.environ.get("CLAUDE_CODE_REMOTE")),
    }
    add = r.items.append

    # python
    v = sys.version_info
    add(Item("python", "ok" if v >= (3, 10) else "missing", f"{v.major}.{v.minor}.{v.micro} (need >= 3.10)", True))

    # ffmpeg
    ff = shutil.which("ffmpeg")
    fp = shutil.which("ffprobe")
    if ff and fp:
        from clipstudio.media.ffmpeg import ffmpeg_capabilities

        caps = ffmpeg_capabilities()
        add(Item("ffmpeg", "ok", caps["version"], True))
        need = ["ass", "loudnorm", "ebur128", "blackdetect", "silencedetect", "astats", "crop", "scale", "boxblur", "vstack"]
        miss = [f for f in need if f not in caps["filters"]]
        add(Item("ffmpeg_filters", "missing" if miss else "ok",
                 f"missing: {', '.join(miss)} (libass build needed for captions)" if miss else "all required filters present",
                 True))
        add(Item("encoder_libx264", "ok" if "libx264" in caps["encoders"] else "missing", "H.264 software encoder", True))
        add(Item("encoder_aac", "ok" if "aac" in caps["encoders"] else "missing", "AAC audio encoder", True))
        nv = "h264_nvenc" in caps["encoders"]
        has_nv_gpu = any(g["vendor"] == "nvidia" for g in gpus)
        add(Item("encoder_nvenc", "ok" if nv and has_nv_gpu else "info",
                 "NVENC usable" if nv and has_nv_gpu else
                 ("ffmpeg has NVENC compiled in but no NVIDIA GPU is visible; libx264 will be used" if nv
                  else "not available; libx264 will be used")))
    else:
        add(Item("ffmpeg", "missing", "ffmpeg/ffprobe not on PATH (see README > Installing FFmpeg)", True))

    # python deps
    for name, imp, req, why in [
        ("numpy", None, True, "core"), ("pydantic", None, True, "config/data models"),
        ("PyYAML", "yaml", True, "config"), ("Pillow", "PIL", True, "caption font measurement"),
        ("faster-whisper", "faster_whisper", True, "transcription"), ("ctranslate2", None, True, "whisper runtime"),
        ("yt-dlp", "yt_dlp", False, "URL ingestion"), ("fastapi", None, False, "studio UI/API server"),
        ("uvicorn", None, False, "studio UI/API server"),
        ("mediapipe", None, False, "facecam detection, BlazeFace (requirements/vision.txt)"),
        ("opencv-python-headless", "cv2", False, "facecam detection fallback, Haar cascade (requirements/vision.txt)"),
        ("ultralytics", None, False, "optional object tracking plugin (AGPL; not wired)"),
        ("torch", None, False, "needed only by optional heavy modules (Demucs, SAM 2, WhisperX)"),
    ]:
        ver = _pkg(name, imp)
        add(Item(f"py:{name}", "ok" if ver else ("missing" if req else "info"),
                 f"{ver} - {why}" if ver else f"not installed - {why}", req))

    # CUDA for CTranslate2
    cuda_n = 0
    try:
        import ctranslate2

        cuda_n = ctranslate2.get_cuda_device_count()
    except Exception:
        pass
    add(Item("cuda_ctranslate2", "ok" if cuda_n else "info",
             f"{cuda_n} CUDA device(s) usable by faster-whisper" if cuda_n else "no CUDA device: transcription runs on CPU (int8)"))

    # optional CLIs
    for tool, why in [("yt-dlp", "URL ingestion"), ("streamlink", "live capture (not yet integrated)"),
                      ("node", "Remotion captions (not used; libass chosen)"), ("fc-match", "font resolution for captions")]:
        p = shutil.which(tool)
        add(Item(f"bin:{tool}", "ok" if p else "info", (p or "not found") + f" - {why}"))

    # whisper model
    m = cfg.transcription.model
    if Path(m).expanduser().is_dir():
        ok = (Path(m).expanduser() / "model.bin").exists()
        add(Item("whisper_model", "ok" if ok else "missing", f"local model dir {m}" + ("" if ok else " has no model.bin"), True))
    else:
        add(Item("whisper_model", "info", f"'{m}' will be downloaded from Hugging Face on first use "
                                         f"(cache: {Path(cfg.paths.models_dir) / 'whisper'})"))

    # fonts
    from clipstudio.captions.engine import resolve_font

    st = cfg.captions.resolved_style()
    fam, file = resolve_font(tuple(st.font), st.bold)
    add(Item("caption_font", "ok" if file else "warn",
             f"{fam} -> {file}" if file else f"could not resolve any of {st.font}; libass will substitute and widths are estimated"))

    # disk
    add(Item("disk", "ok" if disk.free > 20 * 1024**3 else "warn", f"{disk.free / 1024**3:.0f} GB free"))

    # recommendations
    vram = max((g["vram_gb"] or 0 for g in gpus if g["vendor"] == "nvidia"), default=0)
    if cuda_n and vram >= 8:
        r.recommendations.append(f"GPU with {vram:g} GB VRAM: use profile 'quality' (large-v3-turbo, float16) and NVENC.")
    elif cuda_n:
        r.recommendations.append("Small GPU: profile 'balanced' (whisper small on CUDA).")
    else:
        cores = os.cpu_count() or 1
        r.recommendations.append(
            f"CPU-only ({cores} cores): profile 'balanced' (whisper small int8). Measured on a 4-vCPU cloud VM: "
            "~6x faster than real time on speech-dense audio (about 10 minutes per hour of VOD); "
            "use 'fast' (whisper base) for multi-hour VODs.")
    if ram and ram >= 30 and vram >= 12:
        r.recommendations.append("Local LLM re-ranking (Phase 3) is practical: e.g. an 8B instruct model via Ollama/llama.cpp.")
    elif ram and ram >= 15:
        r.recommendations.append("Local LLM: a 3-4B quantised model on CPU is possible but slow; prefer an API provider "
                                 "or the built-in heuristic scorer.")
    else:
        r.recommendations.append("Local LLM inference is not practical here; use the heuristic scorer or an API provider.")
    if r.system["container_hint"]:
        r.recommendations.append("This looks like a container/cloud sandbox; numbers here may not reflect your own PC. "
                                 "Run `python -m app.cli doctor` on the machine you will clip on.")
    return r


def format_report(r: Report) -> str:
    s = r.system
    gpu = ", ".join(f"{g['name']} ({g['vram_gb']} GB)" if g["vram_gb"] else g["name"] for g in s["gpus"]) or "none detected"
    lines = [
        "GTA 6 Clip Studio - environment doctor", "",
        f"  OS        {s['os_detail']}", f"  Python    {s['python']} ({s['python_executable']})",
        f"  CPU       {s['cpu']} x{s['cpu_cores']}", f"  RAM       {s['ram_gb']} GB",
        f"  GPU       {gpu}", f"  Disk      {s['disk_free_gb']} GB free at {s['disk_checked_at']}", "",
    ]
    mark = {"ok": "  ok ", "warn": " WARN", "missing": " MISS", "info": "  -- "}
    for i in r.items:
        lines.append(f"{mark[i.status]}  {i.name:<22} {i.detail}" + ("  [required]" if i.required and i.status == "missing" else ""))
    lines += ["", "Recommendations:"] + [f"  * {x}" for x in r.recommendations]
    lines += ["", "RESULT: " + ("ready" if r.ok else "missing required components (see MISS lines)")]
    return "\n".join(lines)
