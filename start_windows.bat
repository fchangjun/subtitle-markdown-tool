@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Running install_windows.bat first...
  call install_windows.bat
  if errorlevel 1 exit /b 1
)

echo Starting Subtitle Markdown Tool at http://127.0.0.1:8765
start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" web_app.py --host 127.0.0.1 --port 8765
pause
