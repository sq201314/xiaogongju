@echo off
rem Create the virtual environment and install dependencies (first run only).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto :check_pkgs

echo [1/2] Creating virtual environment in .venv ...
py -3 -m venv .venv
if errorlevel 1 python -m venv .venv
if errorlevel 1 goto :fail

:check_pkgs
".venv\Scripts\python.exe" -c "import PySide6, psutil, pynvml, winrt._winrt" >nul 2>&1
if not errorlevel 1 goto :done

echo [2/2] Installing dependencies ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

:done
exit /b 0

:fail
echo.
echo Dependency setup failed. Check your network / pip mirror settings.
pause
exit /b 1
