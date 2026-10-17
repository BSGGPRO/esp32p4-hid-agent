@echo off
REM ESP-IDF wrapper (Lead maintained). Uses official export.bat - no PowerShell ExecutionPolicy needed.
REM Usage: tools\idf.cmd build | tools\idf.cmd -p COM7 flash monitor | tools\idf.cmd set-target esp32p4
setlocal EnableExtensions
set "IDF_PATH=D:\esp\idf-full\esp-idf-v5.5.6"
set "IDF_TOOLS_PATH=D:\esp\.espressif"
set "IDF_PYTHON_ENV_PATH=D:\esp\python_env"
if not defined IDF_GITHUB_ASSETS set "IDF_GITHUB_ASSETS=dl.espressif.com/github_assets"
if not defined IDF_TARGET set "IDF_TARGET=esp32p4"
REM venv python MUST come first, else idf.py runs on the DSH python and fails with "No module named click".
set "PATH=%IDF_PYTHON_ENV_PATH%\Scripts;C:\Program Files\Git\cmd;%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python;%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\Scripts;%PATH%"
if not exist "%IDF_PATH%\export.bat" (
    echo [idf.cmd] FATAL: missing %IDF_PATH%\export.bat
    exit /b 1
)
call "%IDF_PATH%\export.bat" >nul 2>&1
if errorlevel 1 (
    echo [idf.cmd] FATAL: export.bat failed ^(is python_env installed?^)
    exit /b 1
)
idf.py %*
exit /b %errorlevel%
