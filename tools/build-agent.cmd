@echo off
rem Build the controller: dist\esp32p4-agent.exe (single file).
rem Usage: tools\build-agent.cmd [python.exe]
rem   No argument = use "python" from PATH. Must be an env with Pillow installed.
rem Not building? "python tools\agent.py ..." behaves the same.
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

echo [2/2] building...
"%PY%" -m PyInstaller --noconfirm --onefile --name esp32p4-agent ^
  --paths tools --paths pc ^
  --hidden-import connector --hidden-import brain --hidden-import eyes --hidden-import visiond ^
  --add-data "%CD%\docs\ai-controller.md;docs" ^
  --add-data "%CD%\tools\draw-rage.py;." ^
  --exclude-module rapidocr_onnxruntime --exclude-module onnxruntime ^
  --exclude-module torch --exclude-module transformers --exclude-module cv2 ^
  --exclude-module matplotlib --exclude-module tkinter ^
  --distpath dist --workpath build\pyi --specpath build ^
  tools\agent.py || goto :fail

echo.
for %%F in (dist\esp32p4-agent.exe) do echo done: %%~fF  (%%~zF bytes)
echo self-test: dist\esp32p4-agent.exe --caps
exit /b 0

:fail
echo build failed.
exit /b 1
