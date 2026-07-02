@echo off
title AlphaBot Watchdog
color 0A
chcp 65001 > nul

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
timeout /t 10 /nobreak > nul
goto restart
