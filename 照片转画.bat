@echo off
rem 照片 -> 画：双击打开小窗口，选照片 -> 预览 -> 开始画
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "tools\photo-draw-gui.py"
  exit /b 0
)
where python >nul 2>nul
if %errorlevel%==0 (
  python "tools\photo-draw-gui.py"
  exit /b 0
)
echo.
echo [ERROR] Python 3 not found. Install from https://www.python.org/downloads/
echo         Tick "Add python.exe to PATH" during install.
echo.
pause
