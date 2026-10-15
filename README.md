# twincat-mcp

One MCP server, one Python process, full reach into a Beckhoff TwinCAT 3 setup:

| Layer | What the agent can do | Backend |
|---|---|---|
| **Runtime (target IPC)** | PLC/system state, symbol browse, typed read/write (structs, arrays, TIME/DATE decoded), raw index-group access, polling trace, wait-until, run/stop/reset, RUN↔CONFIG mode, EtherCAT master state, routes | pyads (ADS) |
| **IPC file system / OS** | read/write/list/delete files on the target over ADS (TcUnit results, logs, boot dir), start a process, reboot | ADS System Service (port 10000) |
| **Engineering (XAE)** | attach to the running XAE shell, open/build with file:line errors, PLC login/start/stop, set target, activate configuration, restart TwinCAT, one-shot deploy, System Manager tree read/write (XML), create/edit POUs in place, PLCopen import/export, boot project | TE1000 Automation Interface over COM (pywin32) |
| **Project files (offline)** | index .TcPOU/.TcDUT/.TcGVL/.TcIO, code with line numbers, **ADS symbol → file:line resolution**, regex search, references, write-back with LineIds hygiene | XML + regex, no XAE needed |
| **TcUnit** | build → boot → activate → restart → fetch xUnit XML from the target → pass/fail summary | all three |

Runs **on the engineering PC** (Windows, TwinCAT XAE installed) and talks to the IPC through the TwinCAT router. No C# helper, no daemon, no PowerShell, no vector DB.

---

## Why another one

Existing servers each cover a slice; this one was built after reading all of them:

| Server | Gap this project closes |
|---|---|
| [malcolm-mill/beckhoff_mcp](https://github.com/malcolm-mill/beckhoff_mcp) (pyads, read-only) | writes, structs/arrays, TIME/DATE decoding, watch/trace, XAE, files on target |
| [Edge-JB/TwinCAT-XAE-MCP](https://github.com/Edge-JB/TwinCAT-XAE-MCP) (Node + .NET daemon, XAE only) | needs an MSBuild step and the 64-bit shell; no runtime reads, no TcUnit. Confirmation-token and safety-project ideas kept. |
| [idomp/twincat-mcp-public](https://github.com/idomp/twincat-mcp-public) (Python + C# `TcAutomation.exe`) | needs MSBuild + Windows PowerShell 5.1 to build the helper. Arm/confirm + persistent-shell + TcUnit flow kept, in pure Python. |
| [Auda29/twincat-mcp-mono](https://github.com/Auda29/twincat-mcp-mono) (TypeScript ADS) | three-gate write model kept (mode + runtime + allowlist); XAE/files added. |
| [agenticcontrolio/twincat-validator-mcp](https://github.com/agenticcontrolio/twincat-validator-mcp) | offline XML lint/auto-fix. Complementary; run both. |
| [idomp/twincat-index-mcp](https://github.com/idomp/twincat-index-mcp) | semantic search needs Qdrant + embeddings. Here: deterministic symbol-to-source resolution instead. |

Extra: human-in-the-loop via **MCP elicitation** — when the client supports it (Claude Code does) the destructive tools ask *you* in the client UI; the token flow is only the fallback.

---

## Requirements

* Windows 10/11 engineering PC with **TwinCAT XAE 3.1.4024+** (XAE Shell, 32-bit `TcXaeShell.DTE.15.0`, or 64-bit 4026 `TcXaeShell.DTE.17.0`, or VS 2019/2022/2026 with TwinCAT).
* Python **3.10+ 64-bit** (3.11+ for TOML config). pywin32 and pyads are installed by `pip`.
* An ADS route from this PC to the target (XAE → Choose Target → Add Route). `ads_routes` lists what exists.
* Open the solution in XAE before using `xae_*` tools; the server attaches to the running instance (it can launch one, but attaching is faster and visible).

Linux/macOS: only `proj_*`, `tcunit_results(local_path/xml_text)` and the safety tools work. Everything else needs the router/COM.

## Install

```bat
git clone <this repo> C:\tools\twincat-mcp
cd C:\tools\twincat-mcp
scripts\install.cmd            :: venv + pip install -e .[dev] + prints effective config
.venv\Scripts\python -m pytest :: 60 tests, all mocked, run anywhere
```

### Claude Code

```bat
cd C:\Projects\DrumFilling
claude mcp add twincat -e TCMCP_SOLUTION=%CD%\DrumFilling.sln -e TCMCP_AMS_NET_ID=5.12.34.56.1.1 -e TCMCP_MODE=guarded -e TCMCP_MAX_MODE=full -- C:\tools\twincat-mcp\.venv\Scripts\python.exe -m twincat_mcp
```

or copy `.mcp.json.example` → `.mcp.json` in the solution folder. Claude Desktop / Cursor use the same JSON shape.

First prompt: *"run tc_doctor"*. It reports router, local NetId, target state, registered ProgIDs, running shells, project root and the safety mode. Fix what it flags before anything else.

## Configuration

Env vars override `twincat_mcp.toml` (`./`, `~/.twincat_mcp/config.toml`, or `TCMCP_CONFIG`). See `twincat_mcp.toml.example`.

| Env | Meaning | Default |
|---|---|---|
| `TCMCP_MODE` | start mode `readonly` / `guarded` / `full` | `guarded` |
| `TCMCP_MAX_MODE` | ceiling for `tc_set_mode` | = mode |
| `TCMCP_CONFIRM` | `0` → destructive ops in **full** mode need no confirmation | `1` |
| `TCMCP_WRITE_ALLOW` / `TCMCP_WRITE_DENY` | `;`-separated fnmatch patterns (deny always wins) | empty |
| `TCMCP_AMS_NET_ID`, `TCMCP_AMS_PORT` | target | local, `851` |
| `TCMCP_SOLUTION` | `.sln` to attach to; also sets project root | — |
| `TCMCP_PROJECT_ROOT` | folder for `proj_*` | solution folder |
| `TCMCP_PROGIDS` | `;`-separated DTE ProgIDs to try, in order | 17.0, 15.0, VS 18/17/16 |
| `TCMCP_XAE_ATTACH_ONLY` | `0` → `xae_attach` may launch a shell | `1` |
| `TCMCP_TCUNIT_RESULT` | result path on the target | `C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml` |
| `TCMCP_AUDIT_LOG`, `TCMCP_AUDIT_READS` | JSONL audit file; also log reads | `~/.twincat_mcp/audit.jsonl`, `0` |

## Safety model

| op class | examples | readonly | guarded | full |
|---|---|---|---|---|
| read | any `ads_read*`, `proj_*`, `xae_status`, `ipc_read_file` | ok | ok | ok |
| edit | `proj_set_code`, `xae_pou_set`, `xae_tree_consume_xml`, `xae_build`, `xae_plcopen_import` | — | ok | ok |
| write | `ads_write`, `ads_write_raw` | — | allowlist ∧ ¬denylist | ¬denylist |
| control | `ads_plc_state run/stop`, `xae_plc login/start/stop`, `xae_target set`, `xae_command`, `ipc_write_file`, `ads_add_route_to_plc` | — | confirm | ok |
| destructive | `xae_activate_configuration`, `xae_restart_twincat`, `xae_deploy`, `ads_system_mode`, `ads_plc_state reset`, `xae_tree_delete`, `ipc_delete_file`, `ipc_start_process`, `ipc_reboot`, `tcunit_run`, `xae_plc download` | — | confirm | confirm* |

\* unless `TCMCP_CONFIRM=0`.

"Confirm" = elicitation prompt in the client when supported, otherwise `tc_request_confirmation(action, target)` → single-use token, 180 s TTL, bound to that action and target. The agent can escalate its own mode only up to `TCMCP_MAX_MODE`, which it cannot change. Every mutating call lands in the audit log (`tc_audit_tail`).

**"Absolute control" setting:** `TCMCP_MODE=full TCMCP_MAX_MODE=full TCMCP_CONFIRM=0`. In that configuration a single mis-aimed tool call can stop the PLC, activate a half-built configuration or reboot the IPC. The server will do exactly what it is told; the only remaining gate is the MCP client's own tool-permission prompt. Keep a denylist for E-stop/safety symbols regardless.

## Tools (62)

**Meta** `tc_doctor` · `tc_safety_status` · `tc_set_mode` · `tc_request_confirmation` · `tc_audit_tail`

**Runtime (ADS)** `ads_connect` · `ads_status[port]` · `ads_list_symbols` · `ads_symbol_info` · `ads_read` · `ads_read_many` · `ads_write` · `ads_write_many` · `ads_read_raw` · `ads_write_raw` · `ads_watch` · `ads_wait_until` · `ads_plc_state` · `ads_system_mode` · `ads_ethercat` · `ads_routes` · `ads_add_route_to_plc`

**Target OS (System Service)** `ipc_read_file` · `ipc_list_dir` · `ipc_write_file` · `ipc_delete_file` · `ipc_start_process` · `ipc_reboot`

**XAE** `xae_attach` · `xae_status` · `xae_open_solution` · `xae_build` · `xae_diagnostics` · `xae_list_commands` · `xae_command` · `xae_plc` · `xae_target` · `xae_activate_configuration` · `xae_restart_twincat` · `xae_deploy` · `xae_tree_get` · `xae_tree_consume_xml` · `xae_tree_set_values` · `xae_tree_create_child` · `xae_tree_delete` · `xae_plc_projects` · `xae_pou_get` · `xae_pou_set` · `xae_pou_create` · `xae_plcopen_import` · `xae_plcopen_export` · `xae_boot_project`

**Project files** `proj_scan` · `proj_list` · `proj_get_code` · `proj_resolve_symbol` · `proj_search` · `proj_references` · `proj_read_file` · `proj_set_code`

**TcUnit** `tcunit_results` · `tcunit_run`

Resources: `twincat://config`, `twincat://safety`, `twincat://audit/recent`.

## A debugging session

```
> ads_status                              → PLC RUN, system RUN, TwinCAT 3.1.4024.x
> ads_read MAIN.fbFill                    → whole FB instance expanded: eState=3, tSettle=T#500ms, …
> ads_watch [MAIN.fbFill.eState, MAIN.fbFill.fbValve.bOpen] 5000ms 20ms
                                          → eState 3→4 at 812 ms, bOpen never true
> proj_resolve_symbol MAIN.fbFill.fbValve.bOpen
                                          → FB_Fill.TcPOU:14 (fbValve : FB_Valve), FB_Valve.TcPOU:5 (bOpen AT %Q*)
> proj_get_code FB_Fill                   → CASE eState OF … E_FillState.Bulk: fbValve.Open();
> proj_search "fbValve\.Open"             → only one call site; the state that reaches Bulk is never entered
> xae_pou_set …  / proj_set_code …        → fix
> xae_build                               → 0 errors, 1 warning (file:line)
> xae_deploy                              → asks for confirmation, then save/build/activate/restart
> ads_wait_until MAIN.fbFill.bDone == TRUE 30000
```

## Verify-on-hardware list

The XAE and System-Service paths are written against Beckhoff's Infosys documentation and tested against fake COM/ADS objects; this repo was built on Linux, so the following have **not** been executed against a real XAE shell or IPC yet. Check them in this order on the engineering PC, with a non-production target:

1. `tc_doctor` → router ok, ProgIDs registered, running shell listed.
2. `xae_attach` → correct instance/solution; `xae_status` shows `target_net_id`.
3. `xae_build` → `error_source` should be `Windows.ErrorList` or `DTE2.ToolWindows`; if it says `OutputPane` the late-binding fallback is in use (still works, less precise).
4. `xae_pou_get` on a POU → `declaration`/`implementation` populated (late-bound `DeclarationText`; Beckhoff's own PowerShell examples use it this way).
5. `xae_plc login` → if "no working DTE command", run `xae_list_commands PLC` and put the right name into `PLC_LOGIN_COMMANDS` in `xae_backend.py`. On the 64-bit 4026 shell the `PLC.*` commands are reported missing by other projects; `ads_plc_state` covers run/stop/reset without the shell.
6. `ipc_read_file C:\TwinCAT\3.1\TwinCAT.xml` → file access (FOPEN/FREAD/FCLOSE).
7. `ipc_list_dir C:\TwinCAT\3.1\Boot\*` → FFILEFIND struct layout (4-byte handle + WIN32_FIND_DATAA) is the least certain piece here.
8. `ads_ethercat` → index groups 0x3/0x100 (master state) and 0x6 (slave count) on the master's ADS port.
9. `ipc_start_process` with `notepad.exe`, then `ipc_reboot` on a test box.
10. `tcunit_run` end-to-end.

Anything that fails: the tool error text contains the ADS/HRESULT code; paste it back and the fix is usually one constant.

## Not covered (deliberately)

Scope/TE13xx recording, TE1200 static analysis, NC axis commissioning, Safety (TISC) editing, library repository management, semantic code search, OPC UA. The tree XML tools (`xae_tree_get` / `xae_tree_consume_xml`) reach most of the rest of the System Manager generically.

## Development

```
python -m pytest            # 60 tests (safety matrix, type codec, project parser, fake ADS, fake COM, MCP e2e incl. elicitation)
python -m twincat_mcp --print-config
python -m twincat_mcp --http 8765   # streamable-http on loopback only
```

MIT.
