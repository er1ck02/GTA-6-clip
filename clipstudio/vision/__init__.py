"""Computer vision for reframing: frame sampling, face detection, facecam detection, action tracking.

Everything here works on small RGB frames decoded by FFmpeg (``frames.py``).
Optional backends (MediaPipe, OpenCV) are imported lazily, so the rest of the
studio runs without them and falls back to static crops.
"""
