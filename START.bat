@echo off
title AlphaBot v5.0 Launcher
color 0A
chcp 65001 > nul
cls

echo.
echo  ============================================================
echo   AlphaBot v5.0  -  ONE-CLICK LAUNCHER
echo   Binance Futures Testnet  +  Terminal Dashboard
echo  ============================================================
echo.

cd /d "%~dp0trading"

python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found!
    echo  Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo  Python : %PYVER%
echo  Folder : %CD%
echo.

echo  [1/3] Installing dependencies...
pip install rich "websockets>=12.0,<14.0" requests pandas numpy -q
echo  Done.
echo.

echo  [2/3] Freeing port 8765...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8765 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak > nul
echo  Done.
echo.

echo  [3/3] Starting bot engine (independent process - survives after this window closes)...
start "AlphaBot v5.0 Engine" /min python bot.py
echo  Waiting for bot to initialise (8 seconds)...
timeout /t 8 /nobreak > nul

netstat -ano 2>nul | findstr ":8765 " | findstr "LISTENING" > nul
if %errorlevel% equ 0 (
    echo  Bot is RUNNING on ws://localhost:8765
) else (
    echo  [WARN] Port 8765 not detected yet - check bot.log if issues persist
    echo  Starting dashboard anyway...
)
echo.
echo  ============================================================
echo   Launching Terminal Dashboard
echo   Controls:  Q = Quit   R = Restart Bot   B = Browser
echo  ============================================================
echo.

set PYTHONIOENCODING=utf-8
python tui.py

echo.
echo  ============================================================
echo   Dashboard closed. Bot engine still running in background.
echo  ============================================================
echo.
set /p STOP=Stop bot engine? [Y/N]:
if /i "%STOP%"=="Y" (
    taskkill /F /FI "WINDOWTITLE eq AlphaBot v5.0 Engine" >nul 2>&1
    for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8765 " ^| findstr "LISTENING"') do (
        taskkill /F /PID %%a >nul 2>&1
    )
    echo  Bot stopped.
)
echo.
pause
