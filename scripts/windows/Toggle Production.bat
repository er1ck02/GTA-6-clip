@echo off
title Clip Studio - production on/off
cd /d "%~dp0\..\.."
if not exist ".venv\Scripts\python.exe" call "%~dp0install.bat"
".venv\Scripts\python.exe" -m app.cli production toggle %*
if errorlevel 1 (
  echo.
  echo Something went wrong. Read the message above.
  pause
  exit /b 1
)
echo.
echo This window closes by itself in 30 seconds.
timeout /t 30 >nul
