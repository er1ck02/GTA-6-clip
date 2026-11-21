@echo off
rem One-time setup on Windows: virtual environment, Python packages, system check.
setlocal
cd /d "%~dp0\..\.."
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" 2>nul
if errorlevel 1 (
  echo Python 3.10 or newer is required: https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^)
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 echo WARNING: ffmpeg is not on PATH. Install it with:  winget install Gyan.FFmpeg   then open a new window.
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto :fail
".venv\Scripts\python.exe" -m pip install --upgrade pip || goto :fail
".venv\Scripts\python.exe" -m pip install -e ".[ui,vision,ingest,ocr]" || goto :fail
".venv\Scripts\python.exe" -m app.cli doctor
echo.
echo Setup finished. Start the studio with "Clip Studio.bat".
pause
exit /b 0
:fail
echo Setup failed; see the messages above.
pause
exit /b 1
