@echo off
rem Build the GUI variant: dist\esp32p4-agent-ui.exe (single file, windowed).
rem Usage: tools\build-ui.cmd [python.exe]
rem   No argument = use "python" from PATH. Must be an env with Pillow + tkinter.
rem --windowed is the point: no console window, so double-click runs the UI.
setlocal
cd /d "%~dp0.."

set PY=%~1
if "%PY%"=="" set PY=python

echo [1/2] checking deps...
"%PY%" -c "import PIL; print('  Pillow', PIL.__version__)" || goto :fail
"%PY%" -c "import PyInstaller; print('  PyInstaller', PyInstaller.__version__)" 2>nul || (
  echo   PyInstaller missing, install it: "%PY%" -m pip install pyinstaller
  goto :fail
)

echo [2/2] building (windowed)...
"%PY%" -m PyInstaller --noconfirm --onefile --windowed --name esp32p4-agent-ui ^
  --paths tools --paths pc ^
  --hidden-import connector --hidden-import brain --hidden-import eyes --hidden-import visiond ^
  --add-data "%CD%\docs\ai-controller.md;docs" ^
  --add-data "%CD%\tools\draw-rage.py;." ^
  --exclude-module rapidocr_onnxruntime --exclude-module onnxruntime ^
  --exclude-module torch --exclude-module transformers --exclude-module cv2 ^
  --exclude-module matplotlib ^
  --distpath dist --workpath build\pyi-ui --specpath build ^
  tools\agent_ui.py || goto :fail

echo.
for %%F in (dist\esp32p4-agent-ui.exe) do echo done: %%~fF  (%%~zF bytes)
echo note: windowed build has no console; run it from Explorer.
exit /b 0

:fail
echo build failed.
exit /b 1
