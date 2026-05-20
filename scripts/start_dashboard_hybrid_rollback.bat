@echo off
REM Hybrid rollback launch (one process only).
REM Sets DETECTION_MODE=hybrid for the child dashboard process only;
REM the operator's shell env is unmodified after this script exits.
REM
REM Use this when the post-flip monitoring runbook surfaces a
REM regression and you need to fall back to the legacy DOM paths.
REM See docs\FIBER_ONLY_POST_FLIP_MONITORING.md §6.1.
REM
REM Wrapper for: python tools\run_with_detection_health.py --mode hybrid

setlocal
cd /d "%~dp0\.."

echo Starting dashboard in HYBRID rollback mode.
echo (DETECTION_MODE=hybrid forced for this process only)
python tools\run_with_detection_health.py --mode hybrid
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
