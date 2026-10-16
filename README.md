# twincat-mcp

One MCP server, one Python process, full reach into a Beckhoff TwinCAT 3 setup:

| Layer | What the agent can do | Backend |
|---|---|---|
| **Runtime (target IPC)** | PLC/system state, symbol browse, typed read/write (structs, arrays, TIME/DATE decoded), raw index-group access, polling trace, wait-until, run/stop/reset, RUN↔CONFIG mode, EtherCAT master state, routes | pyads (ADS) |
| **IPC file system / OS** | read/write/list/delete files on the target over ADS (TcUnit results, logs, boot dir), start a process, reboot | ADS System Service (port 10000) |
| **Engineering (XAE)** | attach to the running XAE shell, open/build with file:line errors, PLC login/start/stop, set target, activate configuration, restart TwinCAT, one-shot deploy, System Manager tree read/write (XML), create/edit POUs in place, PLCopen import/export, boot project | TE1000 Automation Interface over COM (pywin32) |
| **Real-time recorder** | continuous capture of online variables while the program runs — PLC-timestamped ADS notifications (cycle or on-change) or polling, hours long, file-backed; live snapshot/stats while recording; scope-style start/stop triggers with pre-trigger; CSV export; pulse/dwell and edge-to-edge latency analysis | pyads notifications |
| **Test bench** | scripted scenarios (writes + expectations + parallel recording), assertions with wait, settle checks, stimulus→response latency, RPC method calls, task timing/load, snapshots for regression | ADS |
| **Project files (offline)** | index .TcPOU/.TcDUT/.TcGVL/.TcIO, code with line numbers, **ADS symbol → file:line resolution**, regex search, references, call graph, FB interface summary, lint, write-back with LineIds hygiene, TcUnit suite scaffold | XML + regex, no XAE needed |
| **TcUnit** | build → boot → activate → restart → fetch xUnit XML from the target → pass/fail summary | all three |
| **Reference (Infosys)** | TwinCAT 3 documentation lookup that cannot be incomplete: books and pages from infosys.beckhoff.com's own sitemap, per-library local index (FTS5), symbol search, **FB/struct/enum declarations as data** with the page's descriptions, code-example search, any page as Markdown; web fallback verified against the sitemap | urllib + SQLite, no index to ship |

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
| Infosys documentation servers (`infosys_search` / `infosys_get_page` style, pre-built index) | the shipped index is whatever the author crawled: tested one has no Tc2_System at all (`ADSREAD` → nothing, `get_page` → "not found" for a live page), duplicate `?id=` URLs, BA2 functions ranked above `TON`. Here the page list comes from the site's sitemap and pages are fetched live, so an existing page can never be "not found"; TwinCAT 2 books are filtered out. |

Extra: human-in-the-loop via **MCP elicitation** — when the client supports it (Claude Code does) the destructive tools ask *you* in the client UI; the token flow is only the fallback.

---

## Requirements

* Windows 10/11 engineering PC with **TwinCAT XAE 3.1.4024+** (XAE Shell, 32-bit `TcXaeShell.DTE.15.0`, or 64-bit 4026 `TcXaeShell.DTE.17.0`, or VS 2019/2022/2026 with TwinCAT).
* Python **3.10+ 64-bit** (3.11+ for TOML config). pywin32 and pyads are installed by `pip`.
* An ADS route from this PC to the target (XAE → Choose Target → Add Route). `ads_routes` lists what exists.
* Open the solution in XAE before using `xae_*` tools; the server attaches to the running instance (it can launch one, but attaching is faster and visible).

Linux/macOS: only `proj_*`, `tcunit_results(local_path/xml_text)` and the safety tools work. Everything else needs the router/COM.

## Install

Three ways. All need a 64-bit Python 3.10+ on the engineering PC; nothing else is installed by hand — the
launcher (`scripts/launch.py`) creates a private venv on first start and reinstalls only when the shipped
sources change.

### A. Claude Code plugin (recommended)

The repo is its own plugin marketplace (`.claude-plugin/`). Inside Claude Code:

```
/plugin marketplace add <owner>/<repo>        :: GitHub; or a local clone: /plugin marketplace add C:\tools\twincat-mcp
/plugin install twincat-mcp@twincat-mcp
```

One step on Claude Code ≥ 2.1.275: `/plugin install twincat-mcp --marketplace <owner>/<repo>`. The plugin brings
the MCP server (named `twincat`, tools appear as `mcp__plugin_twincat-mcp_twincat__<tool>`), the `twincat-mcp`
skill and the 5 prompts. `/plugin configure twincat-mcp@twincat-mcp` sets the Python executable (default
`python`), default AmsNetId, start/ceiling safety mode and an optional config TOML; everything project-specific
(solution path, allow/deny lists, HMI root) comes from `twincat_mcp.toml` in the folder you start `claude` in.
First start takes about a minute (venv + pip); if that first session reports the server as failed, the venv is
already there — start a new session. The venv lives in `~/.claude/plugins/data/twincat-mcp-twincat-mcp/venv`
and survives plugin updates.

### B. Claude Desktop bundle

`scripts\build-mcpb.cmd` produces `dist\twincat-mcp-<version>.mcpb` (or take the one from a release). Claude
Desktop → Settings → Extensions → drag the file in. The install dialog asks for the solution, AmsNetId and
safety modes. Windows only (the bundle runs `cmd /d /c server\launch.cmd`, which finds `py -3`, `python` or
`%LOCALAPPDATA%\Programs\Python`). Logs: `%APPDATA%\Claude\logs\mcp-server-twincat-mcp.log`.

### C. By hand (any MCP client, development)

```bat
git clone <this repo> C:\tools\twincat-mcp
cd C:\tools\twincat-mcp
scripts\install.cmd            :: venv + pip install -e .[dev] + prints effective config
.venv\Scripts\python -m pytest :: 146 tests, all mocked, run anywhere
cd C:\Projects\DrumFilling
claude mcp add twincat -e TCMCP_SOLUTION=%CD%\DrumFilling.sln -e TCMCP_AMS_NET_ID=5.12.34.56.1.1 -e TCMCP_MODE=guarded -e TCMCP_MAX_MODE=full -- C:\tools\twincat-mcp\.venv\Scripts\python.exe -m twincat_mcp
```

or copy `.mcp.json.example` → `.mcp.json` in the solution folder. Cursor / Windsurf / Claude Desktop's
`claude_desktop_config.json` use the same JSON shape.

First prompt in any client: *"run tc_doctor"*. It reports router, local NetId, target state, registered
ProgIDs, running shells, project root and the safety mode. Fix what it flags before anything else.

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
| `TCMCP_DOCS`, `TCMCP_DOCS_BOOKS`, `TCMCP_DOCS_EXCLUDE`, `TCMCP_DOCS_WEB_FALLBACK`, `TCMCP_DOCS_CACHE` | Infosys lookup on/off; `;`-separated default books; exclude regexes; web fallback; cache dir | `1`, 8 core PLC libraries, —, `1`, `~/.twincat_mcp/infosys` |

## Safety model

| op class | examples | readonly | guarded | full |
|---|---|---|---|---|
| read | any `ads_read*`, `proj_*`, `xae_status`, `ipc_read_file` | ok | ok | ok |
| edit | `proj_set_code`, `xae_pou_set`, `xae_tree_consume_xml`, `xae_build`, `xae_plcopen_import/export`, `rec_start`, `snap_save`, `tcunit_scaffold` | — | ok | ok |
| write | `ads_write`, `ads_write_raw` (PLC ports only, overlapping symbols checked), `test_scenario`/`test_response` writes, `ads_call_method` | — | allowlist ∧ ¬denylist | ¬denylist |
| control | `ads_plc_state run/stop`, `ads_connect` to a new NetId, `xae_plc login/start/stop`, `xae_target set`, `xae_command`, `ipc_write_file`, `ads_add_route_to_plc` | — | confirm | ok |
| destructive | `xae_activate_configuration`, `xae_restart_twincat`, `xae_deploy`, `ads_system_mode`, `ads_plc_state reset`, `xae_plc download/reset_*`, `xae_command` with download/reset/activate/…, `xae_tree_delete`, `ipc_delete_file`, `ipc_start_process`, `ipc_reboot`, `tcunit_run` | — | confirm | confirm* |

\* unless `TCMCP_CONFIRM=0`.

"Confirm" = elicitation prompt in the client when supported, otherwise `tc_request_confirmation(action, target)` → single-use token, 180 s TTL, bound to that action, target **and the machine the server was connected to when it was issued** (re-targeting with `ads_connect` invalidates it). `xae_deploy` / `tcunit_run` refuse when the XAE target and the ADS target differ. The agent can escalate its own mode only up to `TCMCP_MAX_MODE`, which it cannot change. Every mutating call lands in the audit log (`tc_audit_tail`).

**"Absolute control" setting:** `TCMCP_MODE=full TCMCP_MAX_MODE=full TCMCP_CONFIRM=0`. In that configuration a single mis-aimed tool call can stop the PLC, activate a half-built configuration or reboot the IPC. The server will do exactly what it is told; the only remaining gate is the MCP client's own tool-permission prompt. Keep a denylist for E-stop/safety symbols regardless.

## Tools (127) + 5 prompts

**Workflows (one call = a whole chain)** `wf_debug_symbol` · `wf_fb_profile` · `wf_release_check` · `wf_project_overview`

**Prompts (recipes the agent follows)** `debug_symbol` · `fb_validation` · `deploy_safely` · `performance_audit` · `onboard_project`

**Meta** `tc_doctor` · `tc_safety_status` · `tc_set_mode` · `tc_request_confirmation` · `tc_audit_tail`

**Runtime (ADS)** `ads_discover` · `ads_connect` · `ads_status[port]` · `ads_list_symbols[flag]` · `ads_symbol_info` · `ads_read` · `ads_read_many` · `ads_leaves` · `ads_dump` · `ads_types` · `ads_io_read` · `ads_write` · `ads_write_many` · `ads_read_raw` · `ads_write_raw` · `ads_watch` · `ads_wait_until` · `ads_plc_state` · `ads_system_mode` · `ads_ethercat` · `ads_routes` · `ads_add_route_to_plc` · `ads_app_info` · `ads_task_stats` · `ads_ping` · `ads_call_method` · `ads_export` (C#/TS/JSON from the datatype table)

**Recorder** `rec_start` · `rec_list` · `rec_status` · `rec_snapshot` · `rec_stats` · `rec_read` · `rec_stop` · `rec_export` · `rec_delete` · `rec_pulses` · `rec_latency` · `rec_compare`

**Test bench** `test_expect` · `test_stable` · `test_response` · `test_scenario` · `snap_save` · `snap_compare` · `snap_apply` · `snap_list`

**Target OS (System Service)** `ipc_read_file` · `ipc_list_dir` · `ipc_write_file` · `ipc_delete_file` · `ipc_start_process` · `ipc_reboot` · `ipc_eventlog` (Windows targets)

**XAE** `xae_attach` · `xae_status` · `xae_open_solution` · `xae_build` · `xae_diagnostics` · `xae_list_commands` · `xae_command` · `xae_plc` · `xae_target` · `xae_activate_configuration` · `xae_restart_twincat` · `xae_deploy` · `xae_tree_get` · `xae_tree_consume_xml` · `xae_tree_set_values` · `xae_tree_create_child` · `xae_tree_delete` · `xae_plc_projects` · `xae_pou_get` · `xae_pou_set` · `xae_pou_create` · `xae_plcopen_import` · `xae_plcopen_export` · `xae_boot_project` · `xae_dialogs` · `xae_dialog_resolve` · `xae_io_disable` · `xae_links`

**Project files** `proj_scan` · `proj_list` · `proj_get_code` · `proj_resolve_symbol` · `proj_search` · `proj_references` · `proj_read_file` · `proj_set_code` · `proj_interface` · `proj_lint` · `proj_call_graph` · `proj_variables` · `proj_vs_runtime` · `proj_changed` · `proj_validate_xml`

**HMI (TE2000 / TcVIS)** `hmi_scan` · `hmi_bindings` · `hmi_vs_runtime`

**TcUnit** `tcunit_results` · `tcunit_run[disable_io]` · `tcunit_scaffold`

**Capture profiles** `rec_scan` · `rec_profile_save` · `rec_profile_list` · `rec_profile_get` · `rec_profile_delete` · `rec_profile_start` · `rec_profile_stop`

**Infosys reference (TwinCAT 3)** `infosys_books` · `infosys_index` · `infosys_search_symbols` · `infosys_declaration` · `infosys_search` · `infosys_search_code` · `infosys_get_page`

**Meta** also `tc_session_report` (markdown handover of everything this session changed/produced)

Resources: `twincat://config`, `twincat://safety`, `twincat://audit/recent`. Full one-line-per-tool reference with safety classes: **`TOOLS.md`** (generated from the server).

Claude Code loads MCP tool schemas lazily when a server exposes many tools, so the count costs little context until a tool is used. Performance: quick reads and long operations (watch, scenarios, dumps) run on separate ADS workers, so a 60 s watch never stalls an `ads_read`; recorder queries touch memory/files only; the datatype table and symbol table are cached (TTL 300 s, `refresh=true` to force).

## Connecting Claude Code

Claude Code starts the server itself over stdio — no port, nothing on the network, runs as your Windows user next to the TwinCAT router and the open XAE shell. Plugin install (section A above) is the short path; the manual `claude mcp add` route, scopes, permissions and troubleshooting are in **`integrations/claude-code/README.md`**.

Tool results reach Claude Code as the SDK's indented JSON text plus structured content; Claude Code shows them as collapsible tool-result blocks and the model summarises. For a human-readable handover use `tc_session_report` (markdown).

Also in the repo: `skills/twincat-mcp/SKILL.md` (loaded by the plugin; or drop into `.claude/skills/`), `integrations/claude-code/CLAUDE.md.snippet` (conventions for every session), `integrations/claude-code/.mcp.json` template, `scripts\register-claude-code.cmd`.

## What 0.5.0 closes from the gap list

| gap | tool | status |
|---|---|---|
| modal dialogs stall COM | `xae_dialogs`, `xae_dialog_resolve`, watchdog with `xae.dialog_rules` (never auto-answers activate/run/download/login/delete/safety prompts) | written against win32gui, untested on hardware |
| TwinSAFE guard | every XAE edit refuses `TISC`/`^Safety` paths unless `TCMCP_ALLOW_SAFETY=1` | done |
| XML structure + LineIds | `proj_validate_xml` (GUID format/uniqueness, property VAR blocks, LineIds counts, EOF); `fix=true`, `regenerate_lineids`; `proj_set_code regenerate_lineids=true` | done |
| HMI files | `hmi_scan` / `hmi_bindings` / `hmi_vs_runtime` for TE2000 `%s%PLC1.X%/s%` bindings and TcVIS — broken bindings vs the live PLC and vs source | done |
| TcUnit without hardware | `xae_io_disable`, `tcunit_run disable_io=true` | ITcSmTreeItem.Disabled, untested |
| PLC discovery | `ads_discover` (UDP 48899 broadcast) | protocol per XAE broadcast search, untested |
| variable links | `xae_links` — link target per AT %I*/%Q* variable, unlinked list | instance-tree XML, untested |
| event log | `ipc_eventlog` via wevtutil on Windows targets; CE7/TC-BSD: use the XAE error list | partial |
| MES glue | `ads_export csharp` — explicit-offset structs/enums from the datatype table, `csharp_symbols` constants, TypeScript, JSON | done |
| recipes | `snap_apply` — write a snapshot back, gated per symbol, dry-run first | done |
| handover | `tc_session_report` | done |

Still deliberately absent: Scope/TE13xx, NC, non-EtherCAT fieldbuses, library binary introspection, semantic search, ESI-based box creation.

## Reading every variable

The symbol upload alone does not enumerate elements of `ARRAY OF STRUCT`, nested arrays or deep FB internals. The server also uploads the **datatype table** (`ADSIGRP_SYM_DT_UPLOAD`) and uses it everywhere:

```
> ads_read MAIN.fbFill          → the whole FB instance as one nested tree (one ADS read): structs,
                                  arrays of structs, enums by name, TIME/DATE decoded
> ads_leaves MAIN               → every scalar under the program with type and byte offset
> ads_leaves MAIN.arrSteps with_values=true
> ads_dump                      → every variable on the PLC → dump-<ts>.json (tree) + .csv (path,type,value)
> ads_dump pattern=GVL_*        → only the GVLs
> ads_types ST_Recipe           → members, offsets, sizes; ads_types pattern=E_* → enums
> proj_variables                → what the source declares, flattened to the same instance paths
> proj_vs_runtime               → declared-but-not-online / online-but-not-declared: proves the target
                                  runs this source after a deploy
> rec_start prefix=MAIN         → records every leaf under MAIN, array elements included
```

If the datatype upload fails on a target, every tool falls back to the member symbols the runtime publishes (what the other servers rely on) and says so in `decoder` / `datatype_table`.

## Real-time recording while the program runs

```
> rec_start prefix=MAIN.fbFill mode=change cycle_ms=1 name=fill-cycle
    → every scalar under MAIN.fbFill, PLC-timestamped on every change
> …run the machine, deploy, whatever…
> rec_status <id>          → events, rate, drops, last value of every symbol
> rec_snapshot <id>        → the live tail
> rec_stats <id>           → per symbol: count, min/max/mean, change count, BOOL edges,
                              state runs with dwell times
> rec_stop <id>
> rec_pulses <id> MAIN.fbFill.fbFine.bOpen   → pulse count, width, period, duty
> rec_latency <id> MAIN.fbFill.fbValve.bOpen == TRUE  MAIN.fbFill.rFlow > 0.5
> rec_read <id> t_from_ms=800 t_to_ms=1200 wide=true  → what else was true around the event
> rec_export <id> csv_wide  → spreadsheet
```

Modes: `cycle` = one sample per symbol per `cycle_ms` (set it to the task cycle for a complete trace; expensive), `change` = only when the value changes (cheap, exact edge times), `poll` = sum-read from the PC (any port, no notifications). Triggers: `start_when='MAIN.bStart == TRUE' pre_trigger_ms=500` keeps the half second before the trigger; `stop_when`, `duration_s`, `max_events` end it. Events stream to `~/.twincat_mcp/recordings/<id>.jsonl`; stats are incremental, so a two-hour recording costs no RAM. Recordings from earlier sessions stay readable (`rec_list`, `rec_read`, `rec_pulses`, `rec_export`).

## Choosing what to record: capture profiles

`rec_start` decides the symbol set per call. For batch-after-batch work the set should be decided
once, from a scan of what exists, and replayed. Three ways to choose, all ending in a named profile:

1. **Checklist.** `rec_scan prefix=MAIN.fbDispense checklist=batch` (or `source=project` offline) lists
   every scalar leaf with type, size, a suggested mode (BOOL/INT/enum → `change`, REAL/TIME → `cycle`,
   strings/arrays → `poll`) and the bytes/s it would cost, grouped by `program.instance`, and writes
   `~/.twincat_mcp/profiles/batch.checklist.csv`. Put an `x` in `select`, optionally override `mode` /
   `cycle_ms` per row, then `rec_profile_save name=batch from_checklist=batch`. `preselect=["*.bDone"]`
   pre-marks patterns.
2. **In the source.** `{attribute 'tcmcp.record' := 'cycle'}` above a declaration selects it; on an
   FB/STRUCT instance it selects the whole subtree, an inner `'off'` carves holes,
   `{attribute 'tcmcp.cycle_ms' := '1'}` pins the rate. `rec_profile_save name=batch from_attributes=true`
   collects them through the project index — the selection is versioned with the code and survives
   renames the way nothing in a chat does. TwinCAT ignores unknown attributes.
3. **Explicit.** `rec_profile_save name=x symbols=[...] mode=auto` — `auto` reads the runtime type.

`rec_profile_start batch` starts one recording per (mode, cycle) group, named `batch/cycle@1`,
`batch/change@10`…, with the profile's `start_when` / `stop_when` / `pre_trigger_ms` (trigger symbols
are added to every group automatically); `rec_profile_stop batch` ends them together; `rec_stats`,
`rec_export`, `rec_compare` work per recording as before. Sources combine and later ones override
earlier ones, so a checklist can narrow what the attributes selected.

"By standard" there is no default selection: nothing is recorded that was not chosen, and the
suggested modes are heuristics shown next to the choice, not applied behind it.

## Testing an FB on the live machine

```
> test_scenario {
    "name": "bulk-then-fine",
    "record": {"prefix": "MAIN.fbFill", "mode": "change", "cycle_ms": 1},
    "steps": [
      {"at_ms": 0,    "write": {"MAIN.stRecipe.rTargetKg": 25.0, "MAIN.bStart": true}},
      {"wait_until": {"symbol": "MAIN.fbFill.eState", "op": "==", "value": 2, "timeout_ms": 3000}},
      {"expect": [{"symbol": "MAIN.fbFill.fbValve.bOpen", "op": "==", "value": true}]},
      {"wait_until": {"symbol": "MAIN.fbFill.bDone", "op": "==", "value": true, "timeout_ms": 60000}},
      {"stable": {"symbol": "MAIN.rNetWeight", "duration_ms": 2000, "tolerance": 0.02}}
    ],
    "final_expect": [{"symbol": "MAIN.rNetWeight", "op": "~=", "value": 25.0, "tolerance": 0.05},
                     {"symbol": "MAIN.fbFill.bError", "op": "==", "value": false}]
  }
    → per-step pass/fail with times, written values restored, recording id for rec_* analysis
> test_response {"MAIN.fbFill.fbValve.bOpenCmd": true} "MAIN.fbFill.rFlow > 0.5"
    → PLC-timestamped latency from the write to the response
> ads_task_stats samples=100 → exec time min/max/mean, load %, cycle overruns while the FB runs
> snap_save before prefix=GVL_Params … change code … snap_compare before live tolerance=0.001
> proj_interface FB_Fill · proj_lint FB_Fill · proj_call_graph MAIN
> tcunit_scaffold FB_Fill → FB_Fill_Tests.TcPOU registered in the .plcproj; then tcunit_run
```

Writes in scenarios go through the same allow/deny lists as `ads_write`; nothing is written until every symbol in the script has been permitted. Writing to an `AT %I*` input does nothing useful (the I/O task overwrites it each cycle) — stimulate through unmapped inputs or a simulation GVL.

## Looking up Beckhoff documentation

The `infosys_*` tools exist because the documentation MCP servers available for TwinCAT ship a
pre-built index, and an index is only as complete as the crawl that made it. Against the one tested
here, `infosys_search_symbols("ADSREAD")` returned nothing, `infosys_get_page` reported a live
Tc2_System page as not found, and `TON` ranked BA2 helper functions first. This server does not ship
an index:

* **What exists** comes from `infosys.beckhoff.com/sitemap.xml` (every English page URL with its
  `lastmod`), cached a week. `infosys_books` groups it by book (`tcplclib_tc2_system`, `tf6100_tc3_opcua`,
  `el3xxx`…) with page counts; TwinCAT 2 books are dropped (`tcplclib<name>` without underscore,
  `twincat2/`). `Tc2_System`, `Tc2_Standard`, `Tc2_Utilities` are TwinCAT 3 libraries and stay.
* **What is indexed** is per book, on demand: `infosys_index tcplclib_tc2_system` fetches its pages
  (≈300, parallel, ~30–60 s), converts each to Markdown with our own HTML walker, classifies the page
  (FunctionBlock / Function / Struct / Enum / Alias / Interface / GVL / Library / Page), extracts the
  declaration from the `VAR_INPUT`/`VAR_OUTPUT` code blocks and attaches the description tables, and
  stores it in SQLite with FTS5. A later run refetches only pages whose `lastmod` changed. The first
  `infosys_search_symbols` / `infosys_declaration` indexes the default libraries (`docs.books`) if
  nothing is indexed yet.
* **`infosys_declaration ADSREAD`** → `{kind, inputs:[{name,type,default,description}], outputs,
  in_out, members, enum_values, return_type, extends, implements, declaration, url}` — call it before
  writing code against a library FB you have not used. `infosys_search_code "IDXGRP:=16#F005"` finds the
  example that uses a constant; `infosys_search "rising edge"` is bm25-ranked full text with excerpts.
* **`infosys_get_page`** takes a URL, `book/pageid`, a bare page id, or a symbol name, fetches live
  (cached by `lastmod`) and returns Markdown — for any page the site has, indexed or not. Host-locked
  to infosys.beckhoff.com; the server is not a general web fetcher.
* **Web fallback**: when the local index has no hit, `infosys_search` asks DuckDuckGo for
  `site:infosys.beckhoff.com <query>`, keeps only URLs that are in the sitemap and in TwinCAT 3 books,
  fetches them for title/excerpt and caches them. `backend` in the result says which path answered.
  `docs.web_fallback=false` turns it off.

Config (`[docs]` in the TOML / `TCMCP_DOCS*`): `enabled`, `books` (indexed on first use), `exclude_books`
(regexes), `tc3_only`, `max_workers` (≤16; be polite), `max_index_pages`, `sitemap_ttl_days`,
`web_fallback`, `cache_dir`. Nothing here touches a PLC; all seven tools are read-only class.

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
11. `rec_start mode=change` on a few booleans, toggle them, `rec_stats` → edge timestamps plausible against the task cycle.
12. `ads_task_stats`, `ads_app_info` → `TwinCAT_SystemInfoVarList` members resolve by name (they are not in the uploaded symbol table).
13. `ads_call_method` on a method marked `{attribute 'TcRpcEnable'}` → check the `instance#method` handle name and the packed parameter layout against the result.
14. `ads_types ST_<something>` then `ads_read` of an `ARRAY OF STRUCT` variable → datatype-table parsing (`AdsDatatypeEntry` incl. the optional GUID/attribute/enum tails) against a real table; compare offsets with the TMC. On failure the server falls back to published members and reports `decoder: none` for arrays of structs.

15. `infosys_get_page ADSREAD` and `infosys_index tcplclib_tc2_system` → the HTML walker was written against the page *shape* reported through a Markdown converter, not the raw HTML (this repo was built on a host that cannot reach infosys.beckhoff.com). Check: title = symbol name, `kinds` in the index result mostly FunctionBlock/Function/Struct, `infosys_declaration ADSREAD` lists NETID…TMOUT with descriptions. If titles come back as "Beckhoff Information System" or kinds are all `Page`, the H1/`<pre>` selectors in `infosys.py::_MdWalker` need the real markup — one page's HTML pasted back is enough.
16. `scripts\launch.cmd` (used by the `.mcpb`) → Python discovery order `TCMCP_PYTHON` → `py -3` → `python` → `%LOCALAPPDATA%\Programs\Python\Python3*`; the Microsoft-Store `python.exe` stub fails the version probe and is skipped. Run it once by hand with `--no-run` and watch stderr.

Anything that fails: the tool error text contains the ADS/HRESULT code; paste it back and the fix is usually one constant.

## Changelog

**0.6.0** — capture profiles (7 tools): `rec_scan` of runtime or source with cost estimates and a markable checklist, `{attribute 'tcmcp.record'}` pragmas collected through `proj_variables` (pragmas now parsed and inherited down instance trees), named profiles replayed as one recording per mode group. `infosys_*` reference lookup (7 tools): sitemap-driven book list, per-library local index with FTS5, symbol search, declarations as data, code-example search, live page → Markdown, sitemap-verified web fallback, TwinCAT 2 filtered out; `scripts/gen_tools_md.py`. Packaging: Claude Code plugin + marketplace in `.claude-plugin/` (`/plugin install twincat-mcp@twincat-mcp`, `userConfig` for Python/AmsNetId/modes), Claude Desktop `.mcpb` bundle (`packaging/mcpb/build.py`, manifest 0.3, tool list generated from the server, passes `mcpb validate`), self-bootstrapping launcher `scripts/launch.py` (+ `launch.cmd`) that creates the venv on first start and reinstalls only when the shipped sources change; skill moved to `skills/twincat-mcp/`; HTTP mode bearer-token gate for claude.ai remote connectors. Plugin path verified end-to-end on Linux (install → venv → `tc_doctor` via `mcp__plugin_twincat-mcp_twincat__tc_doctor`); `launch.cmd` Python discovery is untested on Windows.

**0.5.0** — dialog watchdog, TwinSAFE guard, XML validation/fixes + LineIds regeneration, TE2000/TcVIS HMI binding index with runtime/source cross-check, I/O disable, variable links, UDP discovery, Windows event log, C#/TypeScript/JSON exporters, snapshot apply, session report, `naming_prefixes` config.

**0.4.0** — composite workflows (`wf_debug_symbol`, `wf_fb_profile`, `wf_release_check`, `wf_project_overview`), MCP prompts as workflow recipes, `rec_compare`, `proj_changed` (git), `proj_scan incremental`, `ads_io_read` (process image), compact symbol listing, size-guarded `ads_read`, separate ADS workers for quick vs long calls, Claude Code integration kit (`integrations/claude-code/`).

**0.3.0** — datatype-table upload and parsing: full nested decode of any variable in one read (`ads_read`), `ads_leaves`, `ads_dump` (every variable on the PLC → JSON + CSV), `ads_types`; recorder prefixes expand through the table (arrays of structs included); `proj_variables` (source-declared variables flattened to instance paths) and `proj_vs_runtime` (source vs online cross-check).

**0.2.0** — real-time recorder (`rec_*`), test bench (`test_*`, `snap_*`), runtime diagnostics (`ads_app_info`, `ads_task_stats`, `ads_ping`), ADS RPC (`ads_call_method`), static checks (`proj_interface`, `proj_lint`, `proj_call_graph`), `tcunit_scaffold`. Review fixes: ROT enumeration (`EnumRunning`), `vsBuildState` constant, raw writes limited to PLC ports with symbol-overlap deny checks, tokens bound to the connected machine, `ads_connect` re-targeting gated, destructive DTE commands classified, project write-back byte-exact (BOM/EOL preserved) and duplicate-name aware, cooperative cancellation of long ADS loops, blocking calls moved off the event loop.

**0.1.0** — initial release.

## Not covered (deliberately)

Scope/TE13xx recording, TE1200 static analysis, NC axis commissioning, Safety (TISC) editing, library repository management, semantic code search, OPC UA. The tree XML tools (`xae_tree_get` / `xae_tree_consume_xml`) reach most of the rest of the System Manager generically.

## Development

```
python -m pytest            # 146 tests (safety matrix, type codec, project parser, fake ADS incl. notifications/RPC, fake COM, recorder, bench, lint, MCP e2e incl. elicitation, packaging)
python packaging/mcpb/build.py      # dist/twincat-mcp-<version>.mcpb; npx @anthropic-ai/mcpb validate runs if Node is present
claude plugin validate .            # plugin + marketplace manifests
python -m twincat_mcp --print-config
python -m twincat_mcp --http 8765   # streamable-http on loopback; --token / TCMCP_HTTP_TOKEN adds a bearer gate for tunnels
```

MIT.
