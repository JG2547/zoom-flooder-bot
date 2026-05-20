@echo off
REM Fiber-only health check (no dashboard start, no Zoom join).
REM Exits 0 on PASS, 1 on FAIL.
REM Wrapper for: python tools\run_with_detection_health.py --no-start

setlocal
cd /d "%~dp0\.."

python tools\run_with_detection_health.py --no-start
set "RC=%ERRORLEVEL%"

if "%RC%"=="0" (
    echo.
    echo health: OK
) else (
    echo.
    echo health: FAILED (exit code %RC%^)
)

endlocal & exit /b %RC%
