@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Virtual environment not found. Running install_windows.bat first...
  call install_windows.bat
  if errorlevel 1 exit /b 1
)

if "%SUBTITLE_REQUEST_MIN_INTERVAL_SECONDS%"=="" set "SUBTITLE_REQUEST_MIN_INTERVAL_SECONDS=8"
if "%SUBTITLE_REQUEST_JITTER_SECONDS%"=="" set "SUBTITLE_REQUEST_JITTER_SECONDS=4"
if "%SUBTITLE_LIMIT_BACKOFF_SECONDS%"=="" set "SUBTITLE_LIMIT_BACKOFF_SECONDS=180"
if "%SUBTITLE_AUTO_BATCH_SIZE%"=="" set "SUBTITLE_AUTO_BATCH_SIZE=30"
if "%SUBTITLE_AUTO_BATCH_COOLDOWN_SECONDS%"=="" set "SUBTITLE_AUTO_BATCH_COOLDOWN_SECONDS=300"
if "%SUBTITLE_IP_BLOCK_COOLDOWN_SECONDS%"=="" set "SUBTITLE_IP_BLOCK_COOLDOWN_SECONDS=900"

echo Starting Subtitle Markdown Tool for LAN access at http://0.0.0.0:8765
echo Use ipconfig to find this Windows computer's IPv4 address.
echo Slow mode: serial queue, request gap=%SUBTITLE_REQUEST_MIN_INTERVAL_SECONDS%s + jitter, batch=%SUBTITLE_AUTO_BATCH_SIZE%
".venv\Scripts\python.exe" web_app.py --host 0.0.0.0 --port 8765
pause
