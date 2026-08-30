@echo off
REM ============================================================
REM Windows launcher for the dashboard (the Mac side uses the .app)
REM ============================================================
REM Body is kept ASCII on purpose: cmd re-reads this file line by
REM line in the OEM codepage, so non-ASCII text here comes out as
REM mojibake. Chinese output from Python is fine - chcp 65001 plus
REM PYTHONUTF8 below take care of it.
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0.."

set "PY=py"
where py >nul 2>&1 || set "PY=python"

"%PY%" webapp\app.py
if errorlevel 1 pause
