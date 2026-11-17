@echo off
chcp 65001 >nul
title Infinity Search - dev server
setlocal

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
cd /d "%ROOT%"

set "INFINITY_ROOT=%ROOT%"
set "INFINITY_DATA_DIR=%ROOT%\data"
set "PYTHONIOENCODING=utf-8"
set "PORT=8281"

rem Сначала окружение bootstrap, потом портативный Python из релизной сборки,
rem иначе системный Python.
if exist "%ROOT%\.venv\Scripts\python.exe" (
    set "PY=%ROOT%\.venv\Scripts\python.exe"
) else if exist "%ROOT%\python\python.exe" (
    set "PY=%ROOT%\python\python.exe"
) else (
    set "PY=python"
)

echo ===============================================
echo   Infinity Search - source run
echo   Root: %ROOT%
echo   URL:  http://127.0.0.1:%PORT%
echo ===============================================
echo.

cd /d "%ROOT%\app"
"%PY%" main.py --server --port %PORT% %*
set RC=%ERRORLEVEL%
endlocal & exit /b %RC%
