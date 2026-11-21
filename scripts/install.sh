#!/usr/bin/env bash
# One-time setup on macOS / Linux: virtual environment, Python packages, system check.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10+ is required"; exit 1; }
command -v ffmpeg >/dev/null || echo "WARNING: ffmpeg not found (macOS: brew install ffmpeg, Ubuntu: sudo apt install ffmpeg)"
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[ui,vision,ingest,ocr]"
.venv/bin/python -m app.cli doctor || true
echo "Setup finished. Start the studio: .venv/bin/python -m app.cli ui"
