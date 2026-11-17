@echo off
chcp 65001 >nul
title Infinity Search - Setup
setlocal
cd /d "%~dp0"

if not exist "scripts\setup.ps1" (
    echo [ERROR] scripts\setup.ps1 not found.
    echo Run this file from the repository root.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup.ps1" %*
set "RC=%ERRORLEVEL%"

echo.
if not "%RC%"=="0" (
    echo [ERROR] Setup finished with code %RC%.
    echo See logs\setup-*.log for details.
) else (
    echo [OK] Setup finished.
)
pause
exit /b %RC%
