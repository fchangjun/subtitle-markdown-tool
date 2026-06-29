@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Running install_windows.bat first...
  call install_windows.bat
  if errorlevel 1 exit /b 1
)

echo Starting Subtitle Markdown Tool for LAN access at http://0.0.0.0:8765
echo Use ipconfig to find this Windows computer's IPv4 address.
".venv\Scripts\python.exe" web_app.py --host 0.0.0.0 --port 8765
pause
