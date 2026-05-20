@echo off
REM Health-gated dashboard start using the on-disk default DETECTION_MODE
REM (currently "fiber_only" per Phase 7 flip in commit 280cc44).
REM
REM This script never persistently changes DETECTION_MODE in the
REM operator's shell — the launcher scrubs the var from the child env
REM so config.py's on-disk default is honored.
REM
REM Wrapper for: python tools\run_with_detection_health.py --mode default

setlocal
cd /d "%~dp0\.."

echo Starting dashboard in DEFAULT mode (DETECTION_MODE on-disk).
python tools\run_with_detection_health.py --mode default
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
