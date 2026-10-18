@echo off
rem MCP stdio server launcher (for MCP client config on Windows).
rem Usage: tools\mcp-serve.cmd [python.exe]
rem   No argument = try the "py -3" launcher, then "python" from PATH.
rem   Note: on a machine where "python" is the Microsoft Store placeholder,
rem   pass a real interpreter:  tools\mcp-serve.cmd C:\path\to\python.exe
rem   That interpreter needs this repo deps (Pillow etc.) for board tools;
rem   board_caps / board_manual / board_services work with a bare python.
rem stdout is the MCP channel: this script must not echo anything.
setlocal
if not "%~1"=="" goto arg
where py >nul 2>nul
if not errorlevel 1 goto pyl
goto pyth
:arg
"%~1" "%~dp0mcp_server.py"
exit /b %ERRORLEVEL%
:pyl
py -3 "%~dp0mcp_server.py"
exit /b %ERRORLEVEL%
:pyth
python "%~dp0mcp_server.py"
exit /b %ERRORLEVEL%
