@echo off
title Clip Studio - stop production now
cd /d "%~dp0\..\.."
".venv\Scripts\python.exe" -m app.cli production stop --now %*
echo.
pause
