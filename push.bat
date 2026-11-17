@echo off
chcp 65001 >nul
title Infinity Search - Push to GitHub
setlocal
cd /d "%~dp0"

set "REMOTE=https://github.com/noevekshen545/infinitygihub.git"

echo ===============================================
echo   Infinity Search - publish to GitHub
echo   %REMOTE%
echo ===============================================
echo.

git ls-remote "%REMOTE%" >nul 2>&1
if errorlevel 1 (
    echo Repository not found or login required.
    echo.
    echo 1. Create a PUBLIC repository named "infinitygihub":
    echo    https://github.com/new
    echo 2. Do NOT add README, .gitignore or a license.
    echo 3. Return here and press any key.
    echo.
    start "" "https://github.com/new"
    pause >nul
)

echo Pushing branch main...
echo.
git push -u origin main
if errorlevel 1 (
    echo.
    echo [ERROR] Push failed.
    echo.
    echo If Git asks for credentials:
    echo   username: noevekshen545
    echo   password: Personal Access Token with "repo" scope
    echo.
    echo Or sign in in the GitHub window that opens.
    pause
    exit /b 1
)

echo.
echo [OK] Published: https://github.com/noevekshen545/infinitygihub
pause
exit /b 0
