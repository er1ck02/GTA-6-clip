@echo off
title Clip Studio - stop production
cd /d "%~dp0\..\.."
".venv\Scripts\python.exe" -m app.cli production stop %*
echo.
pause
