"""Operating-system differences in one place.

* CUDA on Windows: the cuBLAS/cuDNN DLLs that ``pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`` provides
  live in ``site-packages/nvidia/*/bin``, which Windows does not search. CTranslate2 then reports a CUDA
  device but fails at the first matrix multiply ("cublas64_12.dll is not found"). ``register_cuda_dlls``
  adds those folders to the DLL search path, and ``cuda_usable`` actually loads cuBLAS instead of only
  counting devices.
* Ctrl+Break on Windows: a console process started in its own process group only receives
  CTRL_BREAK_EVENT, which Python raises as SIGBREAK. ``install_interrupt_handlers`` maps it to
  KeyboardInterrupt so an interrupted run is recorded as "interrupted" exactly like Ctrl+C on Linux.
* Font folders when fontconfig (fc-list) is not installed, as on stock Windows and macOS.
"""
from __future__ import annotations

import ctypes
import os
import signal
import sys
from functools import lru_cache
from pathlib import Path

IS_WINDOWS = os.name == "nt"
_CUDA_DLL_PACKAGES = ("cublas", "cudnn", "cuda_nvrtc", "cuda_runtime")
_dll_handles: list = []  # keep os.add_dll_directory handles alive


def nvidia_dll_dirs() -> list[Path]:
    """``site-packages/nvidia/<pkg>/bin`` folders that exist, for every entry on sys.path."""
    found: list[Path] = []
    for base in sys.path:
        root = Path(base or ".") / "nvidia"
        if not root.is_dir():
            continue
        for pkg in _CUDA_DLL_PACKAGES:
            d = root / pkg / "bin"
            if d.is_dir() and d not in found:
                found.append(d)
    return found


@lru_cache(maxsize=1)
def register_cuda_dlls() -> list[str]:
    """Make pip-installed NVIDIA DLLs loadable (Windows only; a no-op elsewhere). Returns the folders added."""
    if not IS_WINDOWS:
        return []
    added = []
    for d in nvidia_dll_dirs():
        try:
            _dll_handles.append(os.add_dll_directory(str(d)))  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            continue
        # CTranslate2 loads cuBLAS lazily with LoadLibrary, which also honours PATH
        os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
        added.append(str(d))
    return added


def _cublas_names() -> list[str]:
    if IS_WINDOWS:
        return ["cublas64_12.dll", "cublas64_11.dll"]
    if sys.platform == "darwin":
        return []
    return ["libcublas.so.12", "libcublas.so.11", "libcublas.so"]


def load_cublas() -> str | None:
    """Return the name of a cuBLAS library that loads, or None."""
    register_cuda_dlls()
    for name in _cublas_names():
        try:
            ctypes.CDLL(name)
            return name
        except OSError:
            continue
    return None


@lru_cache(maxsize=1)
def cuda_usable() -> tuple[bool, str]:
    """(usable, detail): a CUDA device is visible to CTranslate2 AND cuBLAS loads."""
    register_cuda_dlls()
    try:
        import ctranslate2
    except Exception as e:  # pragma: no cover - ctranslate2 is a core dependency
        return False, f"ctranslate2 unavailable: {e}"
    try:
        n = ctranslate2.get_cuda_device_count()
    except Exception as e:
        return False, f"CUDA query failed: {e}"
    if n <= 0:
        return False, "no CUDA device"
    lib = load_cublas()
    if lib is None:
        hint = ("pip install nvidia-cublas-cu12 nvidia-cudnn-cu12" if IS_WINDOWS
                else "install the CUDA 12 cuBLAS runtime (or pip install nvidia-cublas-cu12)")
        return False, f"{n} CUDA device(s) but cuBLAS could not be loaded; {hint}"
    return True, f"{n} CUDA device(s), {lib} loads"


def install_interrupt_handlers() -> None:
    """Treat Ctrl+Break (Windows) like Ctrl+C so stages are marked interrupted and the run can resume."""
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:
        try:
            signal.signal(sigbreak, signal.default_int_handler)
        except (ValueError, OSError):  # not on the main thread
            pass


def font_dirs() -> list[Path]:
    """Folders where installed fonts live, for systems without fontconfig."""
    home = Path.home()
    if IS_WINDOWS:
        dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs = [home / "Library" / "Fonts", Path("/Library/Fonts"), Path("/System/Library/Fonts"),
                Path("/System/Library/Fonts/Supplemental")]
    else:
        dirs = [home / ".local" / "share" / "fonts", home / ".fonts", Path("/usr/share/fonts"),
                Path("/usr/local/share/fonts")]
    return [d for d in dirs if d.is_dir()]
