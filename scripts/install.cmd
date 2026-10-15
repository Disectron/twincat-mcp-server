@echo off
REM twincat-mcp installer for the TwinCAT engineering PC (Windows).
REM Creates a venv next to this repo and installs the server in editable mode.
setlocal
cd /d "%~dp0\.."
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -c "import sys,struct;assert sys.version_info>=(3,10),'Python 3.10+ required';print('Python',sys.version.split()[0],8*struct.calcsize('P'),'bit')" || exit /b 1
if not exist .venv (%PY% -m venv .venv || exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -e .[dev] || exit /b 1
python -m twincat_mcp --print-config
echo.
echo Installed. Register with Claude Code from the solution folder:
echo   claude mcp add twincat -- "%CD%\.venv\Scripts\python.exe" -m twincat_mcp
echo or copy .mcp.json.example to your project as .mcp.json and fix the paths.
endlocal
