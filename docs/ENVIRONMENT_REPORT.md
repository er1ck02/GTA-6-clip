# Environment report (development sandbox, 2026-11-21)

Produced with `python -m app.cli doctor`. **This is the cloud sandbox the first
version was built and tested in, not the user's PC.** Run the doctor on the
machine you will clip on; it adapts its recommendations to what it finds.

| Item | Found |
|---|---|
| OS | Ubuntu 24.04.5 LTS, Linux 6.18 x86_64 (cloud VM / container) |
| Python | 3.13.18 (project supports >= 3.10) |
| CPU | Intel Xeon @ 2.30 GHz, 4 vCPU |
| RAM | 15.7 GB, no swap |
| GPU | **None.** No NVIDIA driver, no CUDA. CPU-only mode. |
| Disk | ~59 GB free |
| FFmpeg / FFprobe | 6.1.1 (Ubuntu build) with libass, loudnorm, ebur128, blackdetect, silencedetect, astats; encoders libx264 and aac. `h264_nvenc` is compiled in but unusable without a GPU. |
| Fonts | Inter (used for captions), DejaVu, FreeSans via fontconfig |
| Video deps pre-installed | none (no OpenCV, MediaPipe, PyTorch) |
| Model runtimes | onnxruntime present system-wide; CTranslate2 4.9 installed for faster-whisper |
| Network | PyPI, npm and apt reachable. **Hugging Face and GitHub downloads blocked** by the sandbox proxy. |

## Measured performance (CPU-only, 4 vCPU)

| Step | Input | Time |
|---|---|---|
| Transcription, whisper small int8, VAD on | 112 s of speech-dense audio | 18.3 s (≈6x real time) |
| Render, 1080x1920 @ 60 fps, x264 medium, captions burned | 23.4 s clip | 14.3 s |
| QC (full decode + analysis + caption pixel check) | 23.4 s clip | ~6 s |
| Re-render after caption change (analysis cached) | 18.6 s clip | ~18 s total, no re-transcription |
| Full `process --top 3` on a 2-hour VOD | 7168 s, 1080p30, 4.2 GB | 25 min (transcribe 15.4, visual 7.3, render + QC ~25 s/clip), peak RSS 2.0 GB |

## What is realistic here, and on a typical gaming PC

- **This sandbox:** whisper `small` on CPU, x264 encoding. Local LLMs are not
  practical. SAM 2, Demucs, RIFE and Video2X should stay off.
- **Gaming PC with an NVIDIA GPU (8 GB+ VRAM):** profile `quality`
  (`large-v3-turbo`, fp16), NVENC encoding, MediaPipe and YOLO tracking in real
  time on sampled frames; an 8B local LLM via Ollama is practical with 12 GB+
  VRAM.
- **Apple Silicon:** faster-whisper runs on CPU (CTranslate2 has no Metal
  backend); `balanced` is the right default.
