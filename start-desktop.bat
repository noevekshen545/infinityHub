@echo off
chcp 65001 >nul
title Infinity Search
setlocal
set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%\desktop"

if not exist "%ROOT%\desktop\node_modules\electron\dist\electron.exe" (
    echo [ERROR] Electron is not installed in desktop\node_modules
    pause
    exit /b 1
)

if not exist "%ROOT%\InfinitySearch.exe" (
    echo [ERROR] InfinitySearch.exe not found next to this script.
    pause
    exit /b 1
)

rem Electron starts the server itself (main.js spawns InfinitySearch.exe).
rem Set INFINITY_PORT to change the port (default 8281).
rem INFINITY_HOST=0.0.0.0 makes the server listen on all interfaces (LAN access).
set "INFINITY_PORT=8281"
set "INFINITY_HOST=0.0.0.0"
"%ROOT%\desktop\node_modules\electron\dist\electron.exe" "%ROOT%\desktop"
endlocal
