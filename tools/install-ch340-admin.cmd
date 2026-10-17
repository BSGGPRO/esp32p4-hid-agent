@echo off
setlocal EnableExtensions
REM ================================================================
REM  Install the SIGNED CH340 driver (wch.cn - Ports - 3.9.2024.9).
REM  Just DOUBLE-CLICK this file. It will request admin rights itself
REM  (UAC prompt -> click "Yes"). No right-click needed.
REM  Everything is logged to D:\esp\ch340-install.log
REM ================================================================
set "LOG=D:\esp\ch340-install.log"
set "INF=D:\esp\ch340-signed\CH341SER.INF"
set "CH340_SELF=%~f0"

net session >nul 2>&1
if errorlevel 1 goto elevate
goto run

:elevate
echo.
echo  [!] Administrator rights required.
echo  [!] A UAC prompt will open now - click "Yes".
echo.
powershell -NoProfile -Command "Start-Process -FilePath $env:CH340_SELF -Verb RunAs"
exit /b 0

:run
> "%LOG%" echo === CH340 signed driver install ===
>>"%LOG%" echo date  : %DATE% %TIME%
>>"%LOG%" echo user  : %USERNAME%
>>"%LOG%" echo inf   : %INF%
>>"%LOG%" echo.
>>"%LOG%" echo [1/4] pnputil /add-driver "%INF%" /install
pnputil /add-driver "%INF%" /install >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo [2/4] pnputil /scan-devices
pnputil /scan-devices >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo [3/4] pnputil /add-driver (retry after rescan)
pnputil /add-driver "%INF%" /install >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo [4/4] Ports class devices + serial port list
pnputil /enum-devices /class Ports >>"%LOG%" 2>&1
powershell -NoProfile -Command "[System.IO.Ports.SerialPort]::GetPortNames() -join ""; """ >>"%LOG%" 2>&1
>>"%LOG%" echo.
>>"%LOG%" echo === done ===

type "%LOG%"
echo.
echo Log file: %LOG%
echo If no new COM port appeared: unplug the board, plug it back, and run this again.
pause
