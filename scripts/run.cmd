@echo off
REM Starts twincat-mcp on stdio (what an MCP client launches). Edit env vars as needed.
cd /d "%~dp0\.."
set TCMCP_MODE=guarded
set TCMCP_MAX_MODE=full
REM set TCMCP_AMS_NET_ID=5.12.34.56.1.1
REM set TCMCP_SOLUTION=C:\Projects\DrumFilling\DrumFilling.sln
REM set TCMCP_WRITE_ALLOW=MAIN.fb*;GVL_Test.*
".venv\Scripts\python.exe" -m twincat_mcp %*
