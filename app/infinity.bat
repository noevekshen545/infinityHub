@echo off
chcp 65001 >nul
title Infinity Search Server
setlocal enabledelayedexpansion

set "PROJECT_DIR=%~dp0"
set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set LOG_FILE=%TEMP%\infinity-server.log
set PORT=8181

set HAS_WSL=0
wsl --version >nul 2>&1 && set HAS_WSL=1

set HAS_PY=0
set PY_CMD=
where python.exe >nul 2>&1 && set HAS_PY=1 && set PY_CMD=python.exe
if %HAS_PY%==0 where python3.exe >nul 2>&1 && set HAS_PY=1 && set PY_CMD=python3.exe
if %HAS_PY%==0 if exist "%PROJECT_DIR%\AppDir\venv\Scripts\python.exe" set HAS_PY=1 && set PY_CMD="%PROJECT_DIR%\AppDir\venv\Scripts\python.exe"

set HAS_WSL_VENV=0
if %HAS_WSL%==1 wsl bash -c "test -f ~/infinity-venv/bin/python3" >nul 2>&1 && set HAS_WSL_VENV=1

goto :menu

:to_wsl_path
set "WPATH=%~1"
set "WPATH=%WPATH:\=/%"
set "DRIVE=%WPATH:~0,1%"
set "WPATH=/mnt/%DRIVE%%WPATH:~2%"
exit /b

:menu
cls
echo ========================================
echo      Infinity Search Server Manager
echo ========================================
echo.
echo  Project: %PROJECT_DIR%
echo  Port:    %PORT%
echo.
if %HAS_WSL%==1 if %HAS_WSL_VENV%==1 echo  Mode: WSL + infinity-venv [ready]
if %HAS_WSL%==1 if %HAS_WSL_VENV%==0 echo  Mode: WSL [needs setup]
if %HAS_WSL%==0 if %HAS_PY%==1 echo  Mode: Windows Python [ready]
if %HAS_WSL%==0 if %HAS_PY%==0 echo  Mode: NOT CONFIGURED
echo.
echo  1. Start
echo  2. Stop
echo  3. Restart
echo  4. Setup environment
echo  5. Exit
echo.
set /p choice="Select (1-5): "

if "%choice%"=="1" goto start
if "%choice%"=="2" goto stop
if "%choice%"=="3" goto restart
if "%choice%"=="4" goto setup
if "%choice%"=="5" exit /b
goto menu

:setup
cls
echo ========================================
echo         Setup Environment
echo ========================================
echo.
if %HAS_WSL%==1 if %HAS_WSL_VENV%==0 (
    echo  WSL detected but ~/infinity-venv not found.
    echo  Run setup-wsl.sh now? (y/n) - takes 10-30 min.
    set /p sn="> "
    if /i "!sn!"=="y" (
        echo.
        echo  Running setup-wsl.sh...
        call :to_wsl_path "%PROJECT_DIR%"
        wsl bash -c "cd '!WPATH!' && bash setup-wsl.sh"
        if !errorlevel!==0 (
            echo  Setup complete!
        ) else (
            echo  Setup failed. Run manually in WSL:
            echo    cd %PROJECT_DIR% ^&^& bash setup-wsl.sh
        )
    )
) else if %HAS_WSL%==0 if %HAS_PY%==0 (
    echo  Neither WSL nor Python found.
    echo  Install Python: https://python.org
    echo  Or WSL: wsl --install
) else (
    echo  Already configured. Start the server.
)
pause
goto menu

:start
cls
echo Starting server...

rem Убиваем зависшие процессы прошлого запуска (иначе "Address already in use")
if %HAS_WSL%==1 (
    wsl pkill -f "main.py --port" 2>nul
    wsl pkill -f "recovery_guard.py" 2>nul
    wsl pkill -f "search_index_worker.py" 2>nul
    timeout /t 3 /nobreak >nul
) else if %HAS_PY%==1 (
    taskkill /f /fi "WINDOWTITLE eq Infinity Server" 2>nul
    timeout /t 2 /nobreak >nul
)

if %HAS_WSL%==1 (
    call :to_wsl_path "%PROJECT_DIR%"
    if %HAS_WSL_VENV%==1 (
        set VENV=~/infinity-venv
    ) else (
        set VENV=!WPATH!/AppDir/venv
    )
    echo  WSL: !WPATH!
    echo  Venv: !VENV!
    wsl bash -c "cd '!WPATH!' && nohup '!VENV!/bin/python3' main.py --port %PORT% > /tmp/infinity-server.log 2>&1 & echo $! > /tmp/infinity-server.pid"
    timeout /t 3 /nobreak >nul
    wsl bash -c "cat /tmp/infinity-server.pid 2>/dev/null"
) else if %HAS_PY%==1 (
    echo  Python: !PY_CMD!
    start "Infinity Server" /B "!PY_CMD!" "%PROJECT_DIR%\main.py" --port %PORT%
    timeout /t 3 /nobreak >nul
) else (
    echo  No environment available. Select "4. Setup environment".
    pause
    goto menu
)

echo.
echo Server started! Open http://localhost:%PORT%
echo.
pause
goto menu

:stop
cls
echo Stopping server...

if %HAS_WSL%==1 (
    wsl pkill -f "main.py --server --port [%PORT:~0,1%]%PORT:~1%" 2>nul
    wsl bash -c "if [ -f /tmp/infinity-server.pid ]; then kill $(cat /tmp/infinity-server.pid) 2>/dev/null; rm -f /tmp/infinity-server.pid; fi"
    wsl pkill -f "main.py --port [%PORT:~0,1%]%PORT:~1%" 2>nul
    wsl pkill -f "recovery_guard.py" 2>nul
    wsl pkill -f "search_index_worker.py" 2>nul
) else if %HAS_PY%==1 (
    taskkill /f /fi "WINDOWTITLE eq Infinity Server" 2>nul
)

echo Server stopped.
pause
goto menu

:restart
echo.
echo Restarting...
if %HAS_WSL%==1 (
    wsl pkill -f "main.py --server --port [%PORT:~0,1%]%PORT:~1%" 2>nul
    wsl bash -c "if [ -f /tmp/infinity-server.pid ]; then kill $(cat /tmp/infinity-server.pid) 2>/dev/null; rm -f /tmp/infinity-server.pid; fi"
    wsl pkill -f "main.py --port [%PORT:~0,1%]%PORT:~1%" 2>nul
    wsl pkill -f "recovery_guard.py" 2>nul
    wsl pkill -f "search_index_worker.py" 2>nul
) else if %HAS_PY%==1 (
    taskkill /f /fi "WINDOWTITLE eq Infinity Server" 2>nul
)
timeout /t 2 /nobreak >nul
goto start