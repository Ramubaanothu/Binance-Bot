@echo off
title AlphaBot v5.0 — Poly Launcher
color 0B
chcp 65001 > nul
cls

echo.
echo  ╔══════════════════════════════════════════════════════╗
echo  ║       ⚡  PolyAlphaBot v5.0  —  LAUNCHER            ║
echo  ║         Polymarket Prediction Market Bot             ║
echo  ╚══════════════════════════════════════════════════════╝
echo.

cd /d "%~dp0trading"

python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found!
    pause
    exit /b 1
)

echo  [1/3] Checking dependencies...
pip install -q rich websockets requests pandas numpy 2>nul
echo         OK
echo.

echo  [2/3] Cleaning up old processes...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8766 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
timeout /t 1 /nobreak > nul
echo         Ports clear
echo.

echo  [3/3] Starting Poly bot...
start /B python poly_bot.py > poly_bot.log 2>&1
timeout /t 5 /nobreak > nul

netstat -ano 2>nul | findstr ":8766 " | findstr "LISTENING" > nul
if %errorlevel% equ 0 (
    echo         Poly bot running on ws://localhost:8766
) else (
    echo         [WARN] Port 8766 not detected yet
)
echo.

echo  Launching Poly dashboard...
echo.
python tui.py --poly

echo.
echo  Dashboard closed.
echo.
pause
