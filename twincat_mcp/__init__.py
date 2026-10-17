"""twincat-mcp — MCP server for Beckhoff TwinCAT 3.

Three backends behind one stdio MCP server:

* ``ads``      – runtime access to the target IPC over ADS (pyads): symbols,
                 read/write, state control, watch/trace, system-service file
                 access, process start, reboot.
* ``xae``      – TwinCAT XAE / Visual Studio Automation Interface (TE1000)
                 over COM (pywin32): build, error list, activate, restart,
                 tree XML, POU authoring, PLCopen import/export.
* ``project``  – offline parsing/editing of .tsproj/.plcproj/.TcPOU/.TcDUT/
                 .TcGVL/.TcIO files, symbol-to-source resolution, TcUnit
                 result parsing.

All mutating operations pass through ``safety.Guard`` (mode, allow/deny
lists, confirmation tokens) and are appended to a JSONL audit log.
"""

__version__ = "0.8.1"
