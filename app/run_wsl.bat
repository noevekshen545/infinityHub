@echo off
REM ============================================================
REM Infinity AI Search System — Запуск из WSL (Ubuntu)
REM ============================================================
REM Просто запусти этот файл из Windows — он откроет WSL с сервером
REM ============================================================

echo Запуск Infinity AI Search System через WSL...
echo.

wsl -d Ubuntu bash /c/Users/infinity/Desktop/infinitysearch/infinitysearch/runjson/run_wsl.sh

pause
