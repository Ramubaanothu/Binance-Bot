@echo off
title AlphaBot Watchdog
color 0A
chcp 65001 > nul
cd /d "%~dp0"

:restart
echo.
echo  [%DATE% %TIME%] Starting bot...
python bot.py
set EXIT_CODE=%errorlevel%

if exist "STOP_BOT" (
    del "STOP_BOT"
    echo  [%TIME%] Clean shutdown requested. Watchdog exiting.
    exit /b 0
)

echo.
echo  [%DATE% %TIME%] Bot exited (code %EXIT_CODE%). Restarting in 10s...
echo  [WATCHDOG] Close this window or press Ctrl+C to stop restarts.
echo.
rem ping = console-independent sleep (timeout can hang in hidden windows)
ping -n 11 127.0.0.1 > nul
goto restart
