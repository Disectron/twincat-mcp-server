# Connecting Claude Code to twincat-mcp (Windows engineering PC)

Claude Code talks to MCP servers over **stdio**: it starts the server process itself when a
session begins, sends JSON-RPC on stdin/stdout, and stops it when the session ends. Nothing
listens on a port, nothing is exposed on the network, and the server runs with your Windows
user (so it sees your TwinCAT router, your ADS routes and your open XAE shell).

## 0. Prerequisites

* TwinCAT XAE installed on this PC, an ADS route to the target (XAE → Choose Target).
* Python 3.10+ 64-bit on PATH (`py -3 --version`).
* Claude Code installed (`claude --version`).
* twincat-mcp unpacked, e.g. `C:\tools\twincat-mcp`, and `scripts\install.cmd` run once
  (creates `.venv`, installs mcp/pyads/pywin32, runs the tests).

## 1. Register the server

Pick a scope:

| scope | where the config lives | when to use |
|---|---|---|
| `project` | `.mcp.json` in the solution folder, committed to git | one machine/solution per repo — **recommended** |
| `local` | your user settings, for this folder only | same as project but not shared |
| `user` | your user settings, every folder | you always target the same test IPC |

From the solution folder (PowerShell or cmd):

```bat
cd C:\Projects\DrumFilling
claude mcp add twincat --scope project ^
  -e TCMCP_SOLUTION=C:\Projects\DrumFilling\DrumFilling.sln ^
  -e TCMCP_AMS_NET_ID=5.12.34.56.1.1 ^
  -e TCMCP_MODE=guarded ^
  -e TCMCP_MAX_MODE=full ^
  -e TCMCP_WRITE_ALLOW="MAIN.fb*;GVL_Test.*;GVL_Hmi.*" ^
  -e TCMCP_WRITE_DENY="*.bEStop*;GVL_Safety.*" ^
  -- C:\tools\twincat-mcp\.venv\Scripts\python.exe -m twincat_mcp
```

or copy `integrations/claude-code/.mcp.json` into the solution folder and edit the paths.
`scripts\register-claude-code.cmd <solution.sln> <AmsNetId>` does the same thing.

Check: `claude mcp list` → `twincat: ... - ✓ Connected`. `claude mcp get twincat` shows the
command and env. On the first use Claude Code asks you to approve the project-scoped server.

## 2. First session

```
claude
> run tc_doctor
```

It reports router/local NetId, target state, registered XAE ProgIDs, running XAE instances,
project root and the safety mode. Then:

```
> /mcp                      # shows the server, its 100 tools and 5 prompts
> /twincat:onboard_project  # a prompt = a workflow recipe (also: debug_symbol, fb_validation,
                            #   deploy_safely, performance_audit)
```

## 3. Day-to-day

* Tool permissions: Claude Code asks before running tools that are not read-only. Allow the
  `ads_read*`, `proj_*`, `rec_status/snapshot/stats`, `xae_status/diagnostics` family
  permanently; keep asking for `ads_write*`, `xae_deploy`, `xae_activate_configuration`,
  `ipc_*`, `tcunit_run`. (`/permissions` or `.claude/settings.json`, `permissions.allow`.)
* Destructive tools additionally ask **inside** the tool through MCP elicitation (Claude Code
  shows a confirm dialog). Without elicitation support the agent must mint a token first;
  tokens are bound to the connected machine.
* Put `CLAUDE.md.snippet` into your repo's `CLAUDE.md` so every session knows the conventions,
  and copy `skills/twincat-mcp/` into `.claude/skills/` so the agent picks the right tools
  without being told.
* Logs: `claude --mcp-debug` prints the server's stderr. To raise the server's own log level
  put `"--log", "DEBUG"` into the `args` of `.mcp.json`.
* Several solutions: one `.mcp.json` per solution folder, each with its own `TCMCP_SOLUTION`
  and `TCMCP_AMS_NET_ID`. The server attaches to the XAE instance holding that solution.

## 4. What about this phone / claude.ai?

claude.ai connectors need an HTTP endpoint reachable from Anthropic's side. The server can
serve `streamable-http` on loopback (`python -m twincat_mcp --http 8765`), but exposing a
process that holds ADS routes to live machines through a tunnel is a decision to make
deliberately, with auth in front of it; it is not configured here. Claude Code on the
engineering PC is the intended client.

## 5. Troubleshooting

| symptom | cause / fix |
|---|---|
| `claude mcp list` shows ✗ | run the command line from the config by hand; a Python import error or a wrong path shows on stderr |
| `tc_doctor` router FAIL | TwinCAT System Service not running, or 32-bit Python (TcAdsDll is 64-bit) |
| `xae_attach` finds nothing | XAE is not open, or a different ProgID; `TCMCP_PROGIDS=TcXaeShell.DTE.15.0` for the 4024 shell |
| every ADS call times out | no route, firewall blocks TCP 48898, or the target is in CONFIG mode |
| tools missing in `/mcp` | Claude Code loads large tool sets lazily; ask for the tool by name |
