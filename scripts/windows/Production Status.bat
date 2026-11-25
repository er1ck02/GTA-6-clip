@echo off
title Clip Studio - production status
cd /d "%~dp0\..\.."
".venv\Scripts\python.exe" -m app.cli production status %*
echo.
pause
