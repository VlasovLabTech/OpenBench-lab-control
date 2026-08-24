@echo off
chcp 65001 >nul
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-itech-mho1-experiment.ps1"
echo.
pause
endlocal
