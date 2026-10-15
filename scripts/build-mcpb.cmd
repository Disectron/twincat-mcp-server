@echo off
REM Builds dist\twincat-mcp-<version>.mcpb for Claude Desktop (drag-and-drop install).
REM Needs the dev venv from scripts\install.cmd (the manifest's tool list is generated
REM from the live server registry). Node/npx optional: runs the official `mcpb validate`.
setlocal
cd /d "%~dp0\.."
if not exist .venv\Scripts\python.exe (echo run scripts\install.cmd first & exit /b 1)
.venv\Scripts\python.exe packaging\mcpb\build.py %*
endlocal
