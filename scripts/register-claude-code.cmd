@echo off
REM Registers twincat-mcp with Claude Code at project scope (.mcp.json in the current folder).
REM Usage: register-claude-code.cmd <path\to\Solution.sln> <AmsNetId e.g. 5.12.34.56.1.1>
setlocal
if "%~1"=="" (echo usage: %~nx0 ^<solution.sln^> ^<AmsNetId^> & exit /b 1)
set SLN=%~f1
set NETID=%~2
set PY=%~dp0..\.venv\Scripts\python.exe
if not exist "%PY%" (echo run scripts\install.cmd first & exit /b 1)
claude mcp add twincat --scope project -e TCMCP_SOLUTION=%SLN% -e TCMCP_AMS_NET_ID=%NETID% -e TCMCP_MODE=guarded -e TCMCP_MAX_MODE=full -- "%PY%" -m twincat_mcp
claude mcp list
endlocal
