@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-openbench.ps1"
if errorlevel 1 (
  echo.
  pause
)
endlocal
