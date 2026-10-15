# Connecting Claude Code to twincat-mcp (Windows engineering PC)

Claude Code talks to MCP servers over **stdio**: it starts the server process itself when a
session begins, sends JSON-RPC on stdin/stdout, and stops it when the session ends. Nothing
listens on a port, nothing is exposed on the network, and the server runs with your Windows
user (so it sees your TwinCAT router, your ADS routes and your open XAE shell).

## 0. Prerequisites

* TwinCAT XAE installed on this PC, an ADS route to the target (XAE → Choose Target).
* Python 3.10+ 64-bit (`python --version`; the plugin runs `python` by default — set another
  executable in `/plugin configure` if yours is only reachable as `py` or by full path).
* Claude Code installed (`claude --version`), 2.1.275+ for the one-line install.

## 1a. Install as a plugin (recommended)

The repo is its own marketplace: `.claude-plugin/marketplace.json` lists one plugin,
`.claude-plugin/plugin.json` declares the `twincat` MCP server, `skills/twincat-mcp/` is the skill.

```
/plugin marketplace add <owner>/<repo>          :: or a local clone: /plugin marketplace add C:\tools\twincat-mcp
/plugin install twincat-mcp@twincat-mcp         :: one step on 2.1.275+: /plugin install twincat-mcp --marketplace <owner>/<repo>
/plugin configure twincat-mcp@twincat-mcp       :: python_exe, ams_net_id, mode, max_mode, config_file (all have defaults / may stay empty)
```

What happens on the first session: Claude Code runs `python <plugin>/scripts/launch.py --src <plugin>`;
the launcher creates `~/.claude/plugins/data/twincat-mcp-twincat-mcp/venv`, pip-installs the server
there (about a minute, needs PyPI once) and hands stdio to `python -m twincat_mcp`. Later starts take
~2 s. If the first session says the server failed to start, the venv is already built — open a new
session. `claude --mcp-debug` shows the launcher's stderr.

Per-project settings (solution path, allow/deny lists, HMI root) do not belong in the plugin config;
put a `twincat_mcp.toml` in the folder you start `claude` in (template: `twincat_mcp.toml.example`).
The server reads `./twincat_mcp.toml`, then `%USERPROFILE%\.twincat_mcp\config.toml`, then the env.

Tool names carry the plugin prefix: `mcp__plugin_twincat-mcp_twincat__tc_doctor`. Permissions
(`/permissions`, `.claude/settings.json`) use that form, e.g. allow
`mcp__plugin_twincat-mcp_twincat__ads_read*`.

Updating: `/plugin update twincat-mcp@twincat-mcp` (or pull the clone). The launcher notices the
changed sources (content hash) and reinstalls into the same venv.

## 1b. Register by hand (`claude mcp add`)

Needs `scripts\install.cmd` run once (creates `.venv`, installs mcp/pyads/pywin32, runs the tests).

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
> /mcp                      # shows the server, its 113 tools and 5 prompts
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
* Put `CLAUDE.md.snippet` into your repo's `CLAUDE.md` so every session knows the conventions.
  The plugin ships the `twincat-mcp` skill; with the manual route copy `skills/twincat-mcp/`
  into `.claude/skills/` so the agent picks the right tools without being told.
* Logs: `claude --mcp-debug` prints the server's stderr. To raise the server's own log level
  put `"--log", "DEBUG"` into the `args` of `.mcp.json`.
* Several solutions: one `.mcp.json` per solution folder, each with its own `TCMCP_SOLUTION`
  and `TCMCP_AMS_NET_ID`. The server attaches to the XAE instance holding that solution.

## 4. Claude Desktop app (same PC)

**Bundle:** `scripts\build-mcpb.cmd` → `dist\twincat-mcp-<version>.mcpb`. Claude Desktop → Settings →
Extensions → drag the file in; the dialog asks for solution, AmsNetId, safety modes, optional config
TOML and Python path. The bundle runs `cmd /d /c <ext>\server\launch.cmd`, which finds a 64-bit
Python 3.10+ (`py -3`, `python`, `%LOCALAPPDATA%\Programs\Python\Python3*`) and bootstraps a venv in
`%LOCALAPPDATA%\twincat-mcp\venv`. Logs: `%APPDATA%\Claude\logs\mcp-server-twincat-mcp.log`.
Unverified on a real Windows box: the Python discovery in `launch.cmd` (run it once by hand with
`--no-run`).

**By hand:** same server, same stdio command, different config file. Claude Desktop → Claude menu →
Settings… → **Developer** → **Edit Config** opens `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "twincat": {
      "command": "C:\\tools\\twincat-mcp\\.venv\\Scripts\\python.exe",
      "args": ["-m", "twincat_mcp"],
      "env": { "TCMCP_SOLUTION": "C:\\Projects\\DrumFilling\\DrumFilling.sln",
               "TCMCP_AMS_NET_ID": "5.12.34.56.1.1", "TCMCP_MODE": "guarded", "TCMCP_MAX_MODE": "full" }
    }
  }
}
```

Quit and restart Claude Desktop. Verify: the "+" (Add files, connectors, and more) button at the
bottom-left of the chat box → Connectors → Manage connectors → `twincat` lists the tools. Logs:
`%APPDATA%\Claude\logs\mcp-server-twincat.log` (the server's stderr) and `mcp.log`.
Local servers configured this way are **not** visible from claude.ai web or the mobile app.

## 5. Phone / claude.ai web (remote custom connector)

Custom connectors must be reachable from Anthropic's cloud, support OAuth or **fixed request
headers** (a bearer token), and are not verified by Anthropic. The server supports exactly that:

```bat
set TCMCP_HTTP_TOKEN=<long random secret>
.venv\Scripts\python.exe -m twincat_mcp --http 8765            :: loopback only
cloudflared tunnel --url http://127.0.0.1:8765                      :: or Tailscale Funnel
```

Then in the Claude app: Customize → Connectors → + Add → Add custom connector → URL
`https://<your-tunnel>/mcp`, Authentication "No sign in", Request headers
`Authorization: Bearer <secret>`. The server refuses to bind a non-loopback address without a
token. Treat the token like the key to the machine room: this process holds ADS routes to live
hardware. Keep `TCMCP_MODE=guarded` with a tight allowlist for anything reachable from the
internet, and disable the destructive tools in the connector's tool settings. "Allow always"
on a write tool from a phone is the fastest way to open a valve you cannot see.

## 6. Troubleshooting

| symptom | cause / fix |
|---|---|
| `claude mcp list` shows ✗ | run the command line from the config by hand; a Python import error or a wrong path shows on stderr |
| plugin server missing from `/mcp` | `claude --mcp-debug`; `python` not on PATH → `/plugin configure` python_exe; first-start pip still running → new session; `claude plugin list` shows load errors |
| plugin tools absent but skill present | `CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS` set in the environment, or the server failed — see the debug log under `~/.claude/debug/` |
| `tc_doctor` router FAIL | TwinCAT System Service not running, or 32-bit Python (TcAdsDll is 64-bit) |
| `xae_attach` finds nothing | XAE is not open, or a different ProgID; `TCMCP_PROGIDS=TcXaeShell.DTE.15.0` for the 4024 shell |
| every ADS call times out | no route, firewall blocks TCP 48898, or the target is in CONFIG mode |
| tools missing in `/mcp` | Claude Code loads large tool sets lazily; ask for the tool by name |
