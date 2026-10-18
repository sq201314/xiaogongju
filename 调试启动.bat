@echo off
rem Debug launcher: keeps a console window so Python errors stay visible.
cd /d "%~dp0"
call "%~dp0setup.bat"
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" "%~dp0main.py"
pause
