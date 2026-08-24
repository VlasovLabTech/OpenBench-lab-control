@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-openbench-skill.ps1"
if errorlevel 1 (
  echo.
  echo OpenBench Codex skill installation failed.
  pause
  exit /b 1
)
echo.
echo OpenBench Codex skill is installed.
pause
