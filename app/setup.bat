@echo off
chcp 65001 >nul
title Infinity Search - Setup

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

cls
echo ========================================
echo   Infinity Search - Setup
echo ========================================
echo.
echo  Project: %PROJECT_DIR%
echo.

echo [1/3] Checking WSL...
wsl --version >nul 2>&1
if %errorlevel% equ 0 (
    echo   WSL: OK
    wsl bash -c "python3 --version" >nul 2>&1
    if %errorlevel% equ 0 echo   Python in WSL: OK
    wsl bash -c "test -f ~/infinity-venv/bin/python3" >nul 2>&1
    if %errorlevel% equ 0 (
        echo   ~/infinity-venv: exists
    ) else (
        echo   ~/infinity-venv: not found
        echo.
        echo   Open WSL and run:
        echo     cd "%PROJECT_DIR:\=/%"
        echo     bash setup-wsl.sh
        echo.
    )
) else (
    echo   WSL: not installed
    echo   Install: https://learn.microsoft.com/windows/wsl/install
    echo.
)

echo [2/3] Checking Windows Python...
python --version >nul 2>&1
if %errorlevel% equ 0 (
    python --version
) else (
    python3 --version >nul 2>&1
    if %errorlevel% equ 0 (
        python3 --version
    ) else (
        echo   Not found - install from https://python.org
    )
)

echo.
echo [3/3] Quick start
echo.
echo  If WSL is ready:
echo    Double-click infinity.bat
echo.
python --version >nul 2>&1
if %errorlevel% equ 0 (
    echo  Without WSL (limited features):
    echo    python main.py --port 8181
    echo.
)
echo Press any key to exit...
pause >nul