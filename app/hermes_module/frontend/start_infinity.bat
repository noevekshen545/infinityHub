@echo off
setlocal
chcp 65001 >nul
title Infinity - Hermes Frontend

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [infinity] Python venv not found: .venv\Scripts\python.exe
  pause
  exit /b 1
)

rem ── torch CPU для Silero TTS: ставим при первом запуске ──────────────────
".venv\Scripts\python.exe" -c "import torch" >nul 2>&1
if errorlevel 1 (
  echo [infinity] Устанавливаю torch CPU ^(нужно для синтеза речи^)...
  ".venv\Scripts\python.exe" -m pip install torch --index-url https://download.pytorch.org/whl/cpu
)

rem ── Запуск: gateway ^+ фронтенд одним процессом ───────────────────────────
echo [infinity] Запуск Infinity: http://127.0.0.1:9120
start "" http://127.0.0.1:9120
".venv\Scripts\python.exe" -m infinity.runner
pause
