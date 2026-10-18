@echo off
rem Start the desktop monitor widget without a console window.
rem --replace lets this launch take over an instance started earlier
rem (for example one started by a restricted terminal).
cd /d "%~dp0"
call "%~dp0setup.bat" >nul 2>&1
if not exist ".venv\Scripts\pythonw.exe" (
  echo Virtual environment is missing. Run setup.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py" --replace
exit /b 0
