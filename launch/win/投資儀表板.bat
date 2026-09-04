@echo off
rem Open the investment dashboard.
start "" powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0dashboard.ps1"
