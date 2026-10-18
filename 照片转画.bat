@echo off
rem 照片 -> 画：双击打开小窗口（选照片 -> 预览 -> 开始画）
setlocal
cd /d "%~dp0"

rem 1) 优先用 DSH 自带运行时（这台机器上一定有）
set "PWW=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
if exist "%PWW%" (
  start "" "%PWW%" "tools\photo-draw-gui.py"
  exit /b 0
)

rem 2) 其次 py 启动器
where py >nul 2>nul
if %errorlevel%==0 (
  start "" py -3w "tools\photo-draw-gui.py"
  exit /b 0
)

rem 3) 最后 pythonw / python（注意 WindowsApps 里的 python.exe 可能只是商店占位符）
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw "tools\photo-draw-gui.py"
  exit /b 0
)

echo.
echo [ERROR] 没找到可用的 Python 3。
echo         检查这个路径是否存在：
echo         %PWW%
echo.
pause
