@echo off
rem Start elevated so PresentMon can read real per-game frame rates.
rem --replace closes an earlier instance before starting the new one.
cd /d "%~dp0"
call "%~dp0setup.bat" >nul 2>&1
if not exist ".venv\Scripts\pythonw.exe" (
  echo Virtual environment is missing. Run setup.bat first.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~dp0.venv\Scripts\pythonw.exe' -ArgumentList '\"%~dp0main.py\" --replace' -WorkingDirectory '%~dp0' -Verb RunAs"
exit /b 0
