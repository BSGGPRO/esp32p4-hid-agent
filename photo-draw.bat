@echo off
rem Photo -> drawing: double-click to open the GUI (pick photo, preview, draw)
setlocal
cd /d "%~dp0"

rem 1) bundled DSH runtime (always present on this machine)
set "PWW=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
if exist "%PWW%" (
  start "" "%PWW%" "%~dp0tools\photo-draw-gui.py"
  exit /b 0
)

rem 2) py launcher
where py >nul 2>nul
if %errorlevel%==0 (
  start "" py -3w "%~dp0tools\photo-draw-gui.py"
  exit /b 0
)

rem 3) pythonw on PATH
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "%~dp0tools\photo-draw-gui.py"
  exit /b 0
)

echo.
echo [ERROR] Python 3 runtime not found.
echo Expected: %PWW%
echo.
pause
