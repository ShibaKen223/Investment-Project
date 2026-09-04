@echo off
REM ============================================================
REM Windows port of daily_run.sh - called by the scheduled task
REM ============================================================
REM Step order matters and is itself a bug fix; see daily_run.sh
REM for the full story. Short version: backfill history FIRST, or
REM the paper engine scans an empty pool, records a fake "no
REM signal today" and locks that date in.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0.."

set "PY=py"
where py >nul 2>&1 || set "PY=python"

echo ==============================================
echo Daily run started %DATE% %TIME%
echo ==============================================

REM --- 1. backfill history --------------------------------
REM Only the last 2 months; months already on disk are skipped.
REM Failure does not stop the run: the position report must still
REM be produced, and data_guard keeps the paper engine from
REM writing a bogus record.
echo.
echo [1/3] backfilling daily bars...
"%PY%" src\history.py --months 2
if errorlevel 1 echo [warn] history backfill failed or partial, continuing

REM --- 2. patch gaps --------------------------------------
echo.
echo [2/3] checking and patching history gaps...
"%PY%" src\history.py --fill-gaps
if errorlevel 1 echo [warn] gap patching failed, continuing

REM --- 3. report + paper engine ---------------------------
echo.
echo [3/3] generating today's report (paper engine advances one day)...
"%PY%" src\main.py --quiet
set "STATUS=%ERRORLEVEL%"

echo.
echo Daily run finished %DATE% %TIME% (main.py exit=%STATUS%)
exit /b %STATUS%
