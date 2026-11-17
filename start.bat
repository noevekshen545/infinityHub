@echo off
chcp 65001 >nul
title Infinity Search Portable
setlocal enabledelayedexpansion

rem ===========================================================================
rem  Infinity Search - portable (Windows). Без venv и Python:
rem  все зависимости внутри InfinitySearch.exe, код - в app\ (правится без пересборки).
rem  Run: start.bat
rem ===========================================================================

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

set "PORT=8281"

set "INFINITY_ROOT=%ROOT%"
set "INFINITY_DATA_DIR=%ROOT%\data"
set "INFINITY_ENGINE_DIR=%ROOT%\engine"
set "INFINITY_MODELS_DIR=%ROOT%\models"
set "INFINITY_CONFIG=%ROOT%\app\config.json"
set "HERMES_INT_PYTHON=%ROOT%\hermes\HermesAgent.exe"
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\ms-playwright"
set "PYTHONIOENCODING=utf-8"

echo ===============================================
echo   Infinity Search - Portable (frozen)
echo   Root:  %ROOT%
echo   URL:   http://127.0.0.1:%PORT%
echo ===============================================
echo.

if not exist "%ROOT%\InfinitySearch.exe" (
    echo [ERROR] InfinitySearch.exe not found next to this script.
    pause
    exit /b 1
)

for %%D in (data data\books data\notes data\zim data\index data\html apps logs models models\gguf models\stt models\tts engine) do (
    if not exist "%ROOT%\%%D" mkdir "%ROOT%\%%D" 2>nul
)

echo [INFO] Starting server...
echo.
"%ROOT%\InfinitySearch.exe" --port %PORT% %*
set RC=%ERRORLEVEL%

echo.
if %RC% NEQ 0 (
    echo [ERROR] Server exited with code %RC%
    echo   Log: %ROOT%\logs\server.log
    pause
)
endlocal & exit /b %RC%
