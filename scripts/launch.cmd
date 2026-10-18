@echo off
REM twincat-mcp launcher for Windows MCP clients (Claude Desktop .mcpb bundle).
REM Finds a 64-bit Python 3.12+ and hands over to launch.py (venv bootstrap + server).
REM Order: TCMCP_PYTHON (extension setting "Python executable") > py -3 > python on PATH
REM        > %LOCALAPPDATA%\Programs\Python\Python3xx > %ProgramFiles%\Python3xx.
REM All diagnostics go to stderr; stdout belongs to the MCP transport.
setlocal EnableExtensions
set "HERE=%~dp0"
set "CHECK=import sys,struct;sys.exit(0 if sys.version_info>=(3,12) and struct.calcsize('P')==8 else 1)"

if defined TCMCP_PYTHON (
    "%TCMCP_PYTHON%" -c "%CHECK%" >nul 2>nul && ( "%TCMCP_PYTHON%" "%HERE%launch.py" %* & goto done )
    echo twincat-mcp launcher: TCMCP_PYTHON="%TCMCP_PYTHON%" is not a 64-bit Python 3.12+, trying PATH 1>&2
)

py -3 -c "%CHECK%" >nul 2>nul && ( py -3 "%HERE%launch.py" %* & goto done )
python -c "%CHECK%" >nul 2>nul && ( python "%HERE%launch.py" %* & goto done )

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
    "%%D\python.exe" -c "%CHECK%" >nul 2>nul && ( "%%D\python.exe" "%HERE%launch.py" %* & goto done )
)
for /d %%D in ("%ProgramFiles%\Python3*") do (
    "%%D\python.exe" -c "%CHECK%" >nul 2>nul && ( "%%D\python.exe" "%HERE%launch.py" %* & goto done )
)

echo twincat-mcp launcher: no 64-bit Python 3.12+ found. Install it from python.org (tick "py launcher"), 1>&2
echo   or set "Python executable" in the extension settings to the full path of python.exe. 1>&2
exit /b 3

:done
exit /b %ERRORLEVEL%
