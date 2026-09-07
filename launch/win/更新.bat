@echo off
rem Windows updater entry point. Double-click this file.
rem Chinese text lives in the .ps1 (UTF-8 with BOM); this wrapper and every path
rem inside it stay ASCII, because cmd.exe parses .bat content in the OEM codepage
rem (cp950/GBK here) and would mangle a Chinese filename into "file not found".
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update.ps1"
