@echo off
rem Starts the local workstation and opens it in the browser. Close this window to stop it.
cd /d "%~dp0\..\.."
if not exist ".venv\Scripts\python.exe" call "%~dp0install.bat"
start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" -m app.cli ui
pause
