@echo off
title Clip Studio - start production
cd /d "%~dp0\..\.."
if not exist ".venv\Scripts\python.exe" call "%~dp0install.bat"
".venv\Scripts\python.exe" -m app.cli production start %*
if errorlevel 1 (
  echo.
  echo Production did not start. Read the message above.
  pause
  exit /b 1
)
echo.
echo This window closes by itself in 30 seconds.
timeout /t 30 >nul
