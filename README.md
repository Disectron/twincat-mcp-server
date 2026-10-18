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
| **Experiments** | spec (factors × levels, runs, preconditions, stimulus, done condition, measures, metrics, criteria, allowed writes) → background runner with snapshot + capture profile per run, factor restore, abort → metrics with 95 % CIs (t / χ²), Cpk → pass / fail / **inconclusive** with the n that would settle it → Markdown + HTML report with identity, charts, audit trail and a separately-labelled interpretation; compare two experiments with error bars | recorder + bench + SQLite-free JSONL |
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
| [agenticcontrolio/twincat-validator-mcp](https://github.com/agenticcontrolio/twincat-validator-mcp) | evaluated 2026-10 and not adopted: its LineIds model (one entry per block, Ids from 1), an invented OVERRIDE marker, "base FB must be nearby" and "undeclared target" for FB members inside methods all fire as errors on real XAE files, and its default autofix re-indents every tab-indented line. The ideas that hold (interface contract, `__NEW`/`__DELETE` pairing, unsigned-loop wrap, abstract instantiation) are `proj_lint` rules here, resolved against the project index so library bases produce *info*, not false errors. |
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
/plugin marketplace add Disectron/twincat-mcp-server        :: GitHub; or a local clone: /plugin marketplace add C:\tools\twincat-mcp
/plugin install twincat-mcp@twincat-mcp
```

One step on Claude Code ≥ 2.1.275: `/plugin install twincat-mcp --marketplace Disectron/twincat-mcp-server`. The plugin brings
the MCP server (named `twincat`, tools appear as `mcp__plugin_twincat-mcp_twincat__<tool>`), two skills —
`twincat-mcp` (tool chains) and `twincat-docs` (offline Beckhoff documentation, below) — and the 5 prompts. `/plugin configure twincat-mcp@twincat-mcp` sets the Python executable (default
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
.venv\Scripts\python -m pytest :: 159 tests, all mocked, run anywhere
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
| `TCMCP_DOCS`, `TCMCP_DOCS_SOURCE`, `TCMCP_DOCS_MSHC`, `TCMCP_DOCS_BOOKS`, `TCMCP_DOCS_EXCLUDE`, `TCMCP_DOCS_WEB_FALLBACK`, `TCMCP_DOCS_CACHE` | Infosys lookup on/off; `local` (Help Viewer package) or `web`; the .mshc; `;`-separated books indexed first; exclude regexes; web fallback (web only); cache dir | `1`, `local`, newest found, 8 core PLC libraries, —, `1`, `~/.twincat_mcp/infosys` |

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

## Tools (151) + 5 prompts

**Workflows (one call = a whole chain)** `wf_debug_symbol` · `wf_fb_profile` · `wf_release_check` · `wf_project_overview`

**Prompts (recipes the agent follows)** `debug_symbol` · `fb_validation` · `deploy_safely` · `performance_audit` · `onboard_project`

**Meta** `tc_doctor` · `tc_safety_status` · `tc_set_mode` · `tc_request_confirmation` · `tc_audit_tail`

**Runtime (ADS)** `ads_discover` · `ads_connect` · `ads_status[port]` · `ads_list_symbols[flag]` · `ads_symbol_info` · `ads_read` · `ads_read_many` · `ads_leaves` · `ads_dump` · `ads_types` · `ads_io_read` · `ads_write` · `ads_write_many` · `ads_read_raw` · `ads_write_raw` · `ads_watch` · `ads_wait_until` · `ads_plc_state` · `ads_system_mode` · `ads_ethercat` · `ads_routes` · `ads_add_route_to_plc` · `ads_app_info` · `ads_task_stats` · `ads_ping` · `ads_call_method` · `ads_export` (C#/TS/JSON from the datatype table)

**Recorder** `rec_start` · `rec_capture` · `rec_list` · `rec_status` · `rec_snapshot` · `rec_stats` · `rec_read` · `rec_stop` · `rec_verify` · `rec_export` · `rec_delete` · `rec_pulses` · `rec_latency` · `rec_compare`

**Test bench** `test_expect` · `test_stable` · `test_response` · `test_scenario` · `snap_save` · `snap_compare` · `snap_apply` · `snap_list`

**Target OS (System Service)** `ipc_read_file` · `ipc_list_dir` · `ipc_write_file` · `ipc_delete_file` · `ipc_start_process` · `ipc_reboot` · `ipc_eventlog` (Windows targets)

**XAE** `xae_attach` · `xae_status` · `xae_open_solution` · `xae_build` · `xae_diagnostics` · `xae_list_commands` · `xae_command` · `xae_plc` · `xae_target` · `xae_activate_configuration` · `xae_restart_twincat` · `xae_deploy` · `xae_tree_get` · `xae_tree_consume_xml` · `xae_tree_set_values` · `xae_tree_create_child` · `xae_tree_delete` · `xae_plc_projects` · `xae_pou_get` · `xae_pou_set` · `xae_pou_create` · `xae_plcopen_import` · `xae_plcopen_export` · `xae_boot_project` · `xae_dialogs` · `xae_dialog_resolve` · `xae_io_disable` · `xae_links`

**Project files** `proj_scan` · `proj_list` · `proj_get_code` · `proj_resolve_symbol` · `proj_search` · `proj_references` · `proj_read_file` · `proj_set_code` · `proj_interface` · `proj_lint` · `proj_call_graph` · `proj_variables` · `proj_vs_runtime` · `proj_changed` · `proj_validate_xml`

**HMI (TE2000 / TcVIS)** `hmi_scan` · `hmi_bindings` · `hmi_vs_runtime`

**TcUnit** `tcunit_results` · `tcunit_run[disable_io]` · `tcunit_scaffold`

**Experiments** `exp_template` · `exp_validate` · `exp_save` · `exp_list` · `exp_get` · `exp_runs` · `exp_run[dry_run]` · `exp_status` · `exp_abort` · `exp_evaluate` · `exp_report` · `exp_compare`

**Knowledge base** `kb_set_location` · `kb_status` · `kb_add_project` · `kb_remove_project` · `kb_reindex` · `kb_search` · `hw_lookup` · `proj_libraries` — the database lives in a folder you name; only project folders you add are indexed

**Offline documentation — skill `twincat-docs`** (no MCP tools; a read-only script the skill runs). The knowledge
folder also holds the documentation its own tools build:

| file in the knowledge folder | content | built by |
|---|---|---|
| `knowledge.db` | projects for `kb_search`, ESI devices for `hw_lookup`, known errors | the `kb_*` tools |
| `helpviewer\infosys_en.db` | every Infosys page of the Help Viewer package (≈ 51,000 pages, 283 books) | `tools\infosys_local.py index` |
| `manualsdb\manuals.db` | the PDF manuals in `manuals\` — page text, chapters, product codes | `tools\manuals_db.py update` |
| `sop\` | operator SOPs (Markdown) | by hand |

`skills/twincat-docs/kb_docs.py` answers `docs_search`, `docs_declaration`, `docs_page`, `docs_books`,
`manual_search`, `manual_product`, `manual_toc`, `manual_page`, `manual_docs`, `status` as one JSON object each
(standard library, opens both databases read-only, same ranking and page references as the builder scripts). A name
that exists in several libraries (`MC_MoveAbsolute` in Tc2_MC2 and Tc3_DriveMotionControl) is never resolved
silently: the answer is `ambiguous` with the candidates. `kb_status` → `docs` shows both databases, the PDFs not
indexed yet and the exact command for this folder; `hw_lookup` lists the manuals that mention a product code.
The `infosys_*` tools stay as the fallback (their own index of the same package).

**Capture profiles** `rec_scan` · `rec_profile_save` · `rec_profile_list` · `rec_profile_get` · `rec_profile_delete` · `rec_profile_start` · `rec_profile_stop`

**Infosys reference (TwinCAT 3)** `infosys_books` · `infosys_index` · `infosys_search_symbols` · `infosys_declaration` · `infosys_search` · `infosys_search_code` · `infosys_get_page`

**Meta** also `tc_session_report` (markdown handover of everything this session changed/produced)

Resources: `twincat://config`, `twincat://safety`, `twincat://audit/recent`. Full one-line-per-tool reference with safety classes: **`TOOLS.md`** (generated from the server).

Claude Code loads MCP tool schemas lazily when a server exposes many tools, so the count costs little context until a tool is used. Performance: quick reads and long operations (watch, scenarios, dumps) run on separate ADS workers, so a 60 s watch never stalls an `ads_read`; recorder queries touch memory/files only; the datatype table and symbol table are cached (TTL 300 s, `refresh=true` to force).

## Connecting Claude Code

Claude Code starts the server itself over stdio — no port, nothing on the network, runs as your Windows user next to the TwinCAT router and the open XAE shell. Plugin install (section A above) is the short path; the manual `claude mcp add` route, scopes, permissions and troubleshooting are in **`integrations/claude-code/README.md`**.

Tool results reach Claude Code as the SDK's indented JSON text plus structured content; Claude Code shows them as collapsible tool-result blocks and the model summarises. For a human-readable handover use `tc_session_report` (markdown).

Also in the repo: `skills/twincat-mcp/SKILL.md` and `skills/twincat-docs/` (loaded by the plugin; or drop into `.claude/skills/`), `integrations/claude-code/CLAUDE.md.snippet` (conventions for every session), `integrations/claude-code/.mcp.json` template, `scripts\register-claude-code.cmd`.

## What 0.5.0 closes from the gap list

| gap | tool | status |
|---|---|---|
| modal dialogs stall COM | `xae_dialogs`, `xae_dialog_resolve`, watchdog with `xae.dialog_rules` (never auto-answers activate/run/download/login/delete/safety prompts); scoped to the XAE process — with no known shell pid nothing is listed or clicked | window enumeration verified on Windows CI (it found a real system dialog); button clicks untested on hardware |
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
> rec_status <id>          → samples, missing_samples, completeness, drops, backlog, last values
> rec_snapshot <id>        → the live tail
> rec_stats <id>           → per symbol: count, min/max/mean, change count, BOOL edges,
                              state runs with dwell times
> rec_stop <id>
> rec_pulses <id> MAIN.fbFill.fbFine.bOpen   → pulse count, width, period, duty
> rec_latency <id> MAIN.fbFill.fbValve.bOpen == TRUE  MAIN.fbFill.rFlow > 0.5
> rec_read <id> t_from_ms=800 t_to_ms=1200 wide=true  → what else was true around the event
> rec_export <id> csv_wide  → spreadsheet (with the PLC's FILETIME per row)
> rec_verify <id> counter=MAIN.nCycleCount → PASS/FAIL: unmodified, no gaps, every cycle present
```

Modes: `cycle` = one sample per `cycle_ms` (set it to the task cycle for a complete trace), `change` = only when the value changes (cheap, exact edge times), `poll` = sum-read from the PC (any port, no notifications; timestamps are this PC's, labelled `time_source: pc`). Triggers: `start_when='MAIN.bStart == TRUE' pre_trigger_ms=500` keeps the half second before the trigger; `stop_when`, `duration_s` (PLC time), `post_trigger_s`, `max_events` end it. Recordings from earlier sessions stay readable (`rec_list`, `rec_read`, `rec_pulses`, `rec_export`).

**Accuracy (0.7).** Neighbouring variables share one ADS notification per contiguous memory block (an FB, a struct, an array): the runtime sends the whole block per sample and the leaves are decoded on the PC. On a 1 ms task, 300 counters as one block were lossless; as 300 separate notifications ~5 % complete — so select whole FBs/structs/arrays rather than hundreds of scattered globals. Every loss is counted: `missing_samples` / `completeness` per block from the PLC timestamps (cycle mode), `dropped` (backlog full), `dropped_on_stop`. A notification that cannot be registered fails the start (`allow_partial=true` records the rest and lists `unsubscribed_symbols`).

**Provenance.** The file (`~/.twincat_mcp/recordings/<id>.jsonl`) stores the raw bytes the runtime sent and the runtime's own sample timestamps: `_t0.filetime` is the PLC FILETIME of the first sample and `t_ms` has 0.1 µs resolution, so `filetime + t_ms·10⁴` is each sample's exact PLC time (`rec_export` adds it as `plc_filetime`). The footer carries a SHA-256 over every line, signed with a per-install key. `rec_verify <id> counter=MAIN.nCycleCount` proves (1) the file is unmodified, (2) no holes in the PLC timeline, (3) the per-cycle counter steps by exactly 1 — the ground truth for "no sample lost". The acceptance run does this with `TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount` at the task cycle.

**Native capture engine (0.8).** Cycle-mode recordings run in `tcmcp-capture.exe` (`twincat_mcp/bin/`), a C++ service built on Beckhoff's **AdsLib** (`AdsDevice` / `AdsNotification`) over Beckhoff's **TcAdsDll** — the TwinCAT router. The ADS callback copies each sample into a lock-free ring and returns; a writer thread merges the blocks by PLC timestamp and writes the same format-2 file, signed the same way. Python is no longer on the data path, so the 300-leaf/1 ms loss of the Python recorder does not apply (the simulator test writes 60 s of a 1200-byte block + counter at 40× real time without a loss). The footer adds `missing_head` (pre-trigger buffer not full), `missing_gaps`, `missing_tail` (PLC stopped before the window end), `overflow_samples` (ring full on this PC — reported, never silent), `requested_s`, `plc_span_s`, `window_start_ms`/`window_end_ms` and `verdict_hint`. One process per recording; nothing is subscribed unless every block is. `engine='python'` (or `[recorder] engine = "python"`) keeps the in-process recorder; without a matching exe, `auto` falls back to Python and says why (`engine_note`). `change`/`poll` modes and `allow_partial` stay on the Python recorder. `tc_doctor` → `recorder` shows which engine cycle recordings use. Build and protocol: `native/capture/README.md`.

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

## Experiments: from runs to a report someone can sign

`rec_*` captures and `test_*` stimulates; `exp_*` turns repeated runs into conclusions. The spec is
YAML (or JSON) in `<project>/experiments/<name>/spec.yaml` — versioned with the code:

```yaml
name: preact-fill-validation
hypothesis: With lambda=0.95 the preact controller holds |bias| <= 2 g and sigma <= 3 g at 1000 g.
profile: preact-fill                      # capture profile started for every run
preconditions: [{symbol: MAIN.fbScale.bStable, op: "==", value: true, timeout_ms: 30000}]
factors:
  - {name: target_g, symbol: GVL_Recipe.rTarget_g, levels: [500, 1000]}
  - {name: lambda,   symbol: MAIN.fbDispense.fbPreact.rLambda, levels: [0.95]}
runs_per_level: 5
run:
  start:     {symbol: MAIN.bStartFill, value: true, reset_value: false, reset_after_ms: 200}
  done_when: {symbol: MAIN.fbDispense.ePhase, op: "==", value: IDLE, timeout_ms: 180000}
  settle_ms: 2000
measures:
  final_weight_g: {kind: last, symbol: MAIN.fbScale.rNet_g}
  final_error_g:  {kind: expr, expr: final_weight_g - target_g}
  cut_latency_ms: {kind: latency, from: {symbol: MAIN.fbDispense.bCut, op: "==", value: true},
                                  to:   {symbol: MAIN.fbFlow.rFlow_gps, op: "<", value: 5.0}}
  fine_pulses:    {kind: pulses, symbol: MAIN.fbFine.bPulse}
metrics:
  bias_g:  {measure: final_error_g, stat: mean}
  sigma_g: {measure: final_error_g, stat: std}
  cpk:     {measure: final_error_g, stat: cpk, tolerance: 5.0}
criteria:
  - {metric: bias_g,  op: abs_le, value: 2.0}
  - {metric: sigma_g, op: le,     value: 3.0}
  - {metric: cpk,     op: ge,     value: 1.33}
writes_allowed: [GVL_Recipe.rTarget_g, MAIN.fbDispense.fbPreact.rLambda, MAIN.bStartFill]
```

`exp_template` returns a complete one for a dosing machine; `exp_validate` checks structure, resolves
every symbol on the connected PLC, and asks the server guard whether each write would be allowed;
`exp_run dry_run=true` prints the plan and writes nothing. `exp_run` then executes in the background —
per run: preconditions (`wait_until`) → factor values written → `snap_save` → capture profile started
→ stimulus → `done_when` → settle → stop → measures from the PLC-timestamped recording → append to
`runs.jsonl`. Factor symbols are read first and restored at the end or on `exp_abort`; two consecutive
failed runs stop the experiment. Resume continues where runs.jsonl stopped.

`exp_evaluate` is deterministic from `runs.jsonl`: per level the metrics with 95 % CIs (t for the
mean, χ² for σ, propagated for Cpk) and each criterion as **pass / fail / inconclusive** — inconclusive
when the limit lies inside the CI, with the run count that would (roughly) settle it. Five fills give
σ a ±40 % standard error; the report says so instead of rounding it away. `exp_report` writes
`report.md` + `report.html`: machine serial, device/TwinCAT version, project + git state, safety mode,
criteria table, metrics per level, charts (primary measure per run with limit lines; per-level mean/CI),
run table with recording ids, the spec as run, the audit log of every write during the experiment,
and an **Interpretation** section stored separately and labelled "not computed". `exp_compare a b`
puts two experiments side by side with "separated" where the CIs do not overlap.

Safety: every written symbol must be in `writes_allowed` *and* pass the guard — checked before anything
moves **and again before every single write**, so `tc_set_mode readonly` is a working kill switch; each
write is pinned to the machine the experiment started on (an `ads_connect` elsewhere makes the next
write fatal) and audited with symbol, value and run. Writes are scalars only (a struct write would bypass
the member deny-list). A run counts as started only when the machine leaves the done state within
`start_timeout_ms`; a rejected start is a failed run, not an instant pass. Factor originals are read and
checked for writability before the first run, restored and read back afterwards (or on abort, after any
`run.on_abort` safe-state writes); an abort interrupts a wait within a second; a timed-out write ends the
experiment because the PLC state is unknown. One experiment per target at a time; starting is
control-class (confirmation in guarded mode). The physical safeguards stay yours.

## Looking up Beckhoff documentation

The `infosys_*` tools exist because the documentation MCP servers available for TwinCAT ship a
pre-built index, and an index is only as complete as the crawl that made it. Against the one tested
here, `infosys_search_symbols("ADSREAD")` returned nothing, `infosys_get_page` reported a live
Tc2_System page as not found, and `TON` ranked BA2 helper functions first. This server does not ship
an index:

* **Source (0.8.3+): the Help Viewer package on this PC, by default.** TwinCAT XAE installs the complete
  Information System as `BKINFOSYS3_VS_100_EN-US.<n>.mshc` under
  `%ProgramData%\Microsoft\HelpLibrary2\Catalogs\<catalog>\ContentStore\EN-US\` (≈ 1 GB, ≈ 50k pages,
  ≈ 280 books). It is a zip of `<book>/1033/<pageid>.html` with the **same page ids as the website**, so
  every page is stored under its real `https://infosys.beckhoff.com/content/1033/<book>/<id>.html` URL.
  `docs.source = "local"` (default): no network call at all, no web fallback; the newest package is found
  automatically (`[docs] mshc` to pin one); every TwinCAT 3 book is indexed in the background on first use
  (default books first), into `<knowledge location>\infosys\helpviewer-1033` when a knowledge base folder is
  chosen. A Help Viewer content update changes the package stamp and re-indexes everything; an unchanged
  package is never re-read. Results carry the page's doc version (`Microsoft.Help.DisplayVersion`) and
  `source.modified` — **the package is as old as the last Help Viewer update on this PC**; check that date
  before trusting a page about a new build or library version. `infosys_get_page` reads any page in the package,
  indexed or not, with its breadcrumb. `docs.source = "web"` brings back the behaviour below.
* **What exists** (web) comes from `infosys.beckhoff.com/sitemap.xml` (every English page URL with its
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

Config (`[docs]` in the TOML / `TCMCP_DOCS*`): `source` (local | web), `mshc`, `enabled`, `books` (indexed first), `exclude_books`
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

Install, configuration and a 14-step zero-write smoke test: **`docs/INSTALL.md`**. Ordered first-day script that then exercises every layer, with expected outputs and what to paste back per failure: **`docs/HARDWARE_DAY.md`**.

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

15. `exp_run <name> max_runs=1` on a harmless spec → the stimulus write, the start acknowledgement, the done condition and the restore appear in `tc_audit_tail` as `exp_write` rows; then `exp_abort` during a run → factor restored, no recording left in `rec_list`.
16. `infosys_get_page ADSREAD` and `infosys_index tcplclib_tc2_system` → the HTML walker was written against the page *shape* reported through a Markdown converter, not the raw HTML (this repo was built on a host that cannot reach infosys.beckhoff.com). Check: title = symbol name, `kinds` in the index result mostly FunctionBlock/Function/Struct, `infosys_declaration ADSREAD` lists NETID…TMOUT with descriptions. If titles come back as "Beckhoff Information System" or kinds are all `Page`, the H1/`<pre>` selectors in `infosys.py::_MdWalker` need the real markup — one page's HTML pasted back is enough.
17. `scripts\launch.cmd` (used by the `.mcpb`) → Python discovery order `TCMCP_PYTHON` → `py -3` → `python` → `%LOCALAPPDATA%\Programs\Python\Python3*`; the Microsoft-Store `python.exe` stub fails the version probe and is skipped. Run it once by hand with `--no-run` and watch stderr.

Anything that fails: the tool error text contains the ADS/HRESULT code; paste it back and the fix is usually one constant.

## Changelog

**0.8.8** — offline documentation as a skill, `kb_*` tools aligned with the knowledge folder, R33–R39. New skill `twincat-docs` (`skills/twincat-docs/`: `SKILL.md` + `kb_docs.py`, standard library, read-only) over the databases the knowledge folder's own tools build — `helpviewer\infosys_en.db` (`tools\infosys_local.py`, every Infosys page of the Help Viewer package) and `manualsdb\manuals.db` (`tools\manuals_db.py`, PDF manuals): `docs_search`, `docs_declaration`, `docs_page`, `docs_books`, `manual_search`, `manual_product`, `manual_toc`, `manual_page`, `manual_docs`, `status`, one JSON object each, with the builders' ranking, snippets and page references; a name in several libraries is refused as `ambiguous` with the candidates, filters run before the row limit (`total` exact), ranges are refused rather than clamped, invalid FTS5 is retried once as a phrase, `ms-xhelp` links open via the help id; the `twincat-mcp` skill and the server instructions send documentation questions there, never to `ads_*`. `kb_status` → `docs`: both databases with counts and package stamp, the PDFs not indexed / changed / gone, the folder's tools and SOPs, the exact command for this folder; `kb_set_location` names the documentation it finds and without arguments returns the current folder (R38); `hw_lookup` lists the manuals that mention the product code. Fixes: the whole `~/.claude/plugins` tree is refused as a location (R33); reads never write-probe, a choice probes under a name of its own (R34); one project per folder in any spelling, the label works for `project=`, `kb_reindex` and `kb_remove_project`, earlier double entries merge (R35); `kb_search` finds units with every word first, then every word as a prefix (`CounterMod` → FB_CounterModule), only then some words (`match` says which), `"quoted"` phrases (R36); punctuation-only queries refused with a message (R37); `kinds`, `project`, `limit` checked (R39); relative paths refused; a label shared by two folders is not removed blindly.

**0.8.7** — 0.8.6 retest and robustness pass (R1–R32, D46, D29, N29, #10/#45, D19). Safety of side effects: tools write files only inside known folders (recordings, project, solution, scope export, experiments, audit log, plus `[server] output_dirs`) and never replace an existing file without `overwrite=true` — `rec_export`, `ads_dump`, `ads_export`, `tc_session_report`, `scope_record`, `scope_export`, `tcunit_scaffold` (R4); `tc_request_confirmation` issues tokens only for real tool names (R2); `tc_session_report` returns a 25-line preview unless `inline=true` (R12). Workers: `ads_dump` runs on its own worker and writes `*.partial` files renamed on success; a queued call whose timeout ran out is withdrawn and says what the worker is busy with instead of cancelling the running job (R6). Recorder: every numeric `rec_start` argument validated before anything starts (R9); conditions refuse operator-led values, unknown types and bad literals and resolve enum names (R10); `rec_snapshot` reads the file for recordings of an earlier session (R13); counter checks per segment across PLC resets (D46); `armed` is false once the trigger fired or the recording ended, native engine included (N29); native tail slack after a manual stop (R7); `rec_profile_start` starts all groups on one PLC cycle via a shared `CycleCount` trigger, `align=false` for the old behaviour (D29). XAE: PLC paths split into the outer `ITcPlcProject` and nested `ITcPlcIECProject` item (R14); `xae_plc` login/logout/start/stop and Check all objects go through the Automation Interface (`ConsumeXml` online commands, `CheckAllObjects`) before the DTE commands (#10, #45); `plcopen_import` reports added objects and refuses a 'replace' that created duplicates (R15); COM methods invoked as methods (R11). TcUnit: results counted from the test cases, suite attribute mismatches reported (R3). Infosys: small lookup tables of what is indexed (catalog, code blocks, help ids) built once in the background — no page is re-parsed and no warm-up query runs (R18); code search normalises non-breaking spaces, `ignore_spacing`, validated languages (R17–R22); `ms-xhelp` links become page URLs, images and empty links removed, declarations tidied, quoted phrases kept, ambiguous declarations name `also_in` (R23–R32). Also: readable spec errors with line/column (R1), the 'datatype table unavailable' answers carry the config hint (R5), RPC 1797 explained with the parameter sizes (R16), unparsable ESI PDO rows carry the raw element (D19, parser v3).

**0.8.6** — 0.8.3 deep test, last part (D20, D25–D29, D32, D38–D43, #10, N19b). XAE: `xae_build` / `xae_diagnostics` parse the 4026 compiler format `File.TcPOU@Method(9) : error: C0185 …` (file, `object`, line, column, `code`; they were empty, D41); `xae_diagnostics` reads the Error List late-bound (the cached 7.0 DTE wrapper has no ToolWindows), copies the Error List view when the automation interface reports nothing (TwinCAT's own Scope / Measurement warnings), merges the Build pane and names each source (D20); `xae_plc` login/logout finds Solution Explorer the same way and selects the PLC project node, then uses 'Set as Active PLC Project' where the shell offers it (#10); `xae_status` with no solution says so instead of a raw COM error. TcUnit: `proj_libraries` scans the 4026 repository `ProgramData\Beckhoff\TwinCAT\PlcEngineering\Managed Libraries` (D38) and gives `effective` for profile-managed placeholders (the only installed version, else "profile", N19b); `tcunit_scaffold` arranges enum inputs with a real member (`E_Command.None`, not `E_Command.0`) and lists the stub tests that fail until written (D39); `tcunit_run` refuses before activating anything when the project leaves `GVL_Param_TcUnit.xUnitEnablePublish` FALSE, stops waiting when the PLC reports FALSE, and names the resolved `boot:` folder (D40); its token target is the NetId (D43); `xae_tree_delete` tokens name the item (D42). Recorder: `rec_pulses` `duty_percent` over complete periods (the idle lead-in counted, D26) and the open first run is no pulse; `rec_profile_stop` after the profile ended gives the final status (D27); `rec_profile_list` `updated` always ISO (D28); `rec_profile_start` reports each group's `start_offsets_ms` from the PLC timestamps (D29); `rec_snapshot` counts a finished native recording's events (D32). Infosys: the background index reports pages inside the current book and `seconds_since_progress` (D25).

**0.8.5** — 0.8.3 deep test, second part (D33–D37, D44, D45). `xae_build(save_first)`, `xae_deploy` and `tcunit_run` (which did not save at all; new `save_first`) run File.SaveAll, save each document/project the shell still reports unsaved, and check every `xae_pou_set` / `xae_pou_create` edit of the session in the source files — they refuse to build while one is missing (an external reload had discarded edits that were only in the shell, D44); `save` in the result lists what was dirty before and after. Experiments: a condition that already holds at a recording's first sample is its initial state, not an edge — no latency is measured from it (the first-sample "1 ms" values that passed a criterion, D34); a latency between symbols in separate change-mode notifications is not reported (timestamps up to `cycle_ms` off, order not kept — use cycle mode or one notification; `rec_latency` returns `timing.trust` exact / quantised / unreliable, D35); measures use the samples up to the start of the `after` steps (`last` read the reset value, D33); `exp_abort` still runs the current run's `after` steps, marks that run `aborted` and ends the experiment `aborted` also when it was the last run (`abort_received` shows when it arrived, D37); the 95 % CI of a count or duration is clipped at 0 and never narrower than the timestamp resolution (`ci95_note`, D36). `tc_doctor`: TwinCAT in CONFIG is reported as info, not as a problem (D45).

**0.8.4** — 0.8.3 deep test (D1–D31, N5, N27–N30, #17, #20, #28, #56, #78, A5). Recordings: a PLC STOP (cycle counter frozen while 1 ms notifications keep coming) is a `{"_stall":…}` line, counts as missing and labels the file INCOMPLETE with `label_reason` (D11); the native engine re-subscribes when every block went silent, e.g. after RUN → CONFIG → RUN (D13, `resubscriptions` in the footer); live status counts silent time as missing (D14) and stops on the PC clock when PLC time no longer advances (D15); a recording that ends by itself is sealed at once and a start-up sweep seals any left unsealed (N27); `rec_read(wide=true)` gives one row per PLC timestamp with null where nothing was sampled, `fill=true` marks filled cells (D9); counter completeness capped at 1 with duplicates reported (D12); `armed` false once triggered (N29), `values_per_s` counts array elements (N30); `rec_list` pages the session list too (D21). `rec_start(pattern=…)` selects exactly the leaves `rec_scan` lists: `*`/`?` wildcards, `[ ]` literal (`GVL_Sim.aCh[1].*`), a leaf matches when it or a parent matches (A5); `prefix` may name one element of an array of structs (D31). `ads_wait_until` / `ads_watch` use scratch recordings that are always removed, `interval_ms` honoured (D7, D6); top-level enums read as `{value, name}` (D4). `exp_run` writes the stimulus only after every recording delivers (D22). `xae_restart_twincat` / `xae_deploy` count a restart only after the target was seen leaving RUN and coming back (`modes_seen`, `transition_seen`; the first poll read the old runtime's RUN, N5/#4). ADSError 6 is explained for the port that was asked (D24). `proj_vs_runtime` gives no missing/extra verdict from a list cut at `max_items` (`complete`, `not_judged`, D23), keeps the PLC's and the source's spelling (#20). `xae_tree_get` walks the nested `<Name> Project` (#17). `ipc_eventlog` never guesses the boot folder and reports whether its temp file is really gone (#56). Infosys: one transaction per book, FTS optimised once, external-content FTS (10,000 synthetic pages in 11 s here, DB −30 %; not yet timed on the real package), CRC-based page identity so an unchanged package (or the same package on another PC) is never re-read, re-index only when the indexer version changes (an existing 0.8.3 index rebuilds once), progress names the book being indexed (D1, D25); `version` only from `Microsoft.Help.DisplayVersion` (D2); answers carry `doc_date` + `indexed_books_count` instead of the book list (D3). `proj_lint real_eq` ignores `:=` (D17); `proj_interface` inherited members have `file` (D16); `proj_variables` keeps the CONSTANT qualifier (D18); `hw_lookup` reads namespaced ESI files (PDOs, entries, CoE; ESI files are re-read once, D19). Scope found through `IMeasurementScope` (N28, not yet run on hardware). D8: the server already published `engine` / `also_scope` / `scope` (a regression test now pins them) — a client that does not show them is using a cached schema.

**0.8.3** — Infosys offline from the Help Viewer package. `docs.source = "local"` (default) reads the `BKINFOSYS3_*.mshc` TwinCAT XAE installs (found under `%ProgramData%\Microsoft\HelpLibrary2`, or `[docs] mshc`): every TwinCAT 3 book indexed in the background (default books first), pages keyed by their real infosys.beckhoff.com URL, no network call and no web fallback; results carry the doc version and the package date; a new package re-indexes, an unchanged one is never re-read; index next to the knowledge base. Missing package → the infosys tools say where they looked and how to fix it (`docs.source = "web"` keeps the website path). Infosys tools no longer crash when the index is disabled.

**0.8.2** — TwinCAT Scope next to the ADS recorder. `scope_export` runs Beckhoff's `TC3ScopeExportTool.exe` (`"svd=<file>" target=<csv> silent`, TE130x / TF3300 folders, `[scope] export_tool`) on a saved `.svdx`, never edits its CSV, and writes a signed `<csv>.provenance.json` (source and tool SHA-256, command line). Through the XAE Automation Interface (`IMeasurementScope` of a Measurement project): `scope_list`, `scope_record` start / stop / save (SaveSVD + ExportCSV into `<recordings>/scope`). `rec_start(also_scope=true, scope=…)` starts the Scope configuration with the recording; `rec_stop` stops it, saves `<id>.svdx`, exports `<id>.scope.csv` (falls back to the export tool) and writes a signed `<id>.scope.json` with both SHA-256s; the files are read-only. `[recorder] also_scope = "ask"` (default) asks the user whenever XAE has a Scope configuration open — by prompt, or with `allow_elicitation = false` by an error telling the agent to ask and call again; `always` / `never`. New tool group `scope`; 155 tools. The XAE Scope calls are late-bound against the documented interface and not yet run against a real Scope View.

**0.8.1** — recording authenticity (field report R1–R12) and the 0.8.0 retest. Both engines write `{"_gap":…}` lines where samples are missing (inside the run, before the first and after the last sample), a footer `label` COMPLETE / INCOMPLETE / NOT_PROVABLE, and a signed `{"_chain":…}` checkpoint (running SHA-256 + HMAC) every second, so a file whose footer never got written is still proven intact up to its last checkpoint. After a recording ends it is sealed: `<id>.manifest.json` (whole-file SHA-256, footer checksum and signature, summary, HMAC-signed) and both files read-only; `rec_verify` checks every checkpoint, names the first bad line, checks the manifest and reports `read_only`; `rec_delete` clears read-only and removes the manifest. Every export writes a signed `<export>.provenance.json` (source file and SHA-256, footer checksum, label, seal state, parameters incl. `fill`, exporter version, export SHA-256); CSV gets an exact `time_utc` column (7 decimals, integer maths). The header records each symbol's datatype GUID, base type and (structs recorded whole) member layout. Retest: N13 duration stop waits ≤ 1 s for in-flight samples up to the cut (lossless runs no longer FAIL by 3–10), N17 ADSError 6 explained from port and RUN/CONFIG, N18 PROGRAM calls in `proj_call_graph`, N19 `(System)` placeholders are info, N21 infosys names the books being indexed and the real backend, N22 BOOL writes accept only true/false/0/1, N23 `rec_capture` explains missing stats, N24 `edge_triggers` in the header, N26 project index scans serialised, #66 tcunit ProductVersion from the project. N25: `tcmcp-capture.exe` now ships in the plugin, built and tested by the release workflow (`release/v*` branch → exe + SHA-256 committed, tag, release with zip, `.mcpb`, `SHA256SUMS`, build provenance); the server refuses an exe whose SHA-256 differs from the pinned one; `/Brepro` makes the build deterministic.

**0.8.0** — native capture service (C++ capture spec). `tcmcp-capture` on Beckhoff's AdsLib (vendored, MIT) in TwinCAT mode over TcAdsDll.dll, bound at start so CI builds the exe without the TwinCAT SDK (`-DTCMCP_TCADSDLL_SDK` builds against Beckhoff's headers and lib, same layout asserts); AdsLib standalone elsewhere. Lock-free per-block rings, PLC-time merge, format-2 file byte-compatible with the Python recorder (spec test 10: identical rows and exports), SHA-256 + HMAC footer, head/gap/tail/overflow accounting, triggers and stop conditions evaluated on raw bytes, all-or-nothing subscription, clean stop when the server goes away. `rec_start` / `rec_capture` `engine` parameter, `[recorder]` config section, engine and reason in every response, `tc_doctor` → `recorder`. Python recorder: pre-trigger samples that are discarded no longer count in the block timeline; `window_start_ms` (trigger − pre_trigger_ms) and `missing_head`; `rec_verify` fails on overflow and on a FAIL verdict hint. Simulator backend + 15 tests for spec items 1, 5–10. 151 tools.

**0.7.2** — knowledge base and the 0.7.1 retest. Knowledge base (decisions of 2026-10-17): the database lives in a folder the user names (`kb_set_location`; checked: local fixed drive, not cloud-synced, not the plugin folder, writable, ≥ 1 GB; `D:\TwinCAT-MCP\knowledge` only when the user agrees); `kb_add_project` / `kb_search` over folders the user adds (nothing by default); `hw_lookup` (PDOs, CoE objects from TwinCAT's ESI files); `proj_libraries` (what each library reference resolves to on this PC); known causes/fixes attached to tool errors. Recorder: above 20,000 values/s nothing is decoded on the ingest thread (300 leaves × 1 ms lost samples because decoding starved the ADS callback); the GIL switch interval is 0.5 ms while recording; the missing tail up to the window end is counted (N11); responses trim long arrays; triggers on elements of recorded arrays and `edge_triggers`. `rec_delete` needs a confirmation and the audit keeps the file's SHA-256 (R11). Interfaces are recognised from the project source when the datatype table does not mark them: reads as `0x…`, writes refused (#52/#64). `tc_allow_write` tokens for the pattern as typed (N1); session allow-list kept over `tc_reload_config` (N6); writers scoped to the declaring unit (N3); System Manager refreshed after a solution reopen (N9); env overrides of TOML values reported (N10); external `tcunit_scaffold` not gated (N12); restart `duration_s` until RUN (N5); Check all objects selects the PLC project node and reports the real refusal (#45). 151 tools.

**0.7.1** — decisions of 2026-10-17. The CONSTANT check runs on every write path (`snap_apply`, `test_*`, `exp_*` too); with no project source loaded, guarded mode refuses writes and full mode allows them. Recordings carry a manifest (AppTimestamp, OnlineChangeCnt, tasks and cycle times, TwinCAT build, symbol version) and, in cycle mode, the task's per-cycle counter as ground truth that `rec_verify` uses automatically. `csv_wide` is one row per PLC sample time with array elements as columns; empty cell = not sampled then (`fill=true` for charts). The example config carries the agreed machine-PC settings (`allow_elicitation = false`, `edit_requires_config_mode = true`, tool groups, narrow allow-list). 143 tools.

**0.7.0** — recording accuracy and field report round 3 (A1–A7, #75–79, P2–P19, K1). Recorder: one ADS notification per contiguous memory block instead of per symbol (the field test: 300 values as one block lossless, as 300 notifications ~5 %); the callback only copies the raw sample; file format 2 keeps the runtime's raw bytes and exact PLC FILETIME per sample, a symbol table and block layout in the header, and a signed SHA-256 footer; `missing_samples`/`completeness` per block from the PLC timestamps, `dropped`, `dropped_on_stop`; a stop drains the backlog to the file; duration is PLC time; a notification that cannot be registered fails the start (`allow_partial`); patterns expand to leaves like `rec_scan`; live statistics step aside under load and `rec_stats` recomputes them from the file; new `rec_verify` (integrity, timeline, per-cycle counter) and `rec_capture` (record → stats → verdict in one call); the acceptance run proves a lossless recording of `_TaskInfo[1].CycleCount` at the task cycle. `ads_watch` / `ads_wait_until` on ADS notifications, off the ADS workers; `ads_write_many` as one sum-write (all values in the same PLC cycle); writes report `written` vs `read_back`; symbol/datatype caches follow the runtime's symbol version instead of a 300 s timer; `ads_system_mode` waits for the requested mode; COM calls blocked by a modal dialog fail within a second naming it; `xae_deploy` fails unless the target runs afterwards; `tc_doctor` one-line summary (`verbose` for the config); `server.tool_groups`; launcher skips hashing unchanged sources; Infosys indexes the books of the libraries the project references. Older recording files stay readable. 143 tools.

**0.6.3** — field report round 2 (items 51–74, framework project with inheritance, interfaces, enums, strings, arrays). Writes: an array write must supply every element (a short list zeroed the rest); POINTER/REFERENCE and INTERFACE variables are refused (an interface is a pointer the PLC calls through) and read as `0x…` hex or null; writes to anything declared in a `VAR … CONSTANT` block are refused from the project source (the runtime does not flag them READONLY); STRING(n) length is checked before ADS; enum names are accepted (`Idle`, `E_State.Idle`) in `ads_write` and `test_expect`; `ads_write_many` pre-checks every item before the first write and reports applied/failed/not_attempted if the PLC rejects one. Recorder: `duration_s` no longer compares the PLC's task clock with Windows time (cycle mode ran on for 30 s), `rec_stop` keeps its own stop reason, the file is flushed every second, `post_trigger_s` ends a triggered recording after the trigger. Project: the index refreshes incrementally (mtime) before every index-based tool; `proj_vs_runtime` matches `x[*]` source arrays to their elements, collapses arrays and caps output; `proj_call_graph` follows interface calls to every implementing FB, inherited methods and THIS^ overrides; `proj_interface` merges the EXTENDS chain (`inherited_from`) and inherited IMPLEMENTS; `wf_debug_symbol` no longer calls `fb(x := y)` a write of y and lists .TcVIS input-action writes; `tcunit_scaffold` takes the project's ProductVersion, keeps declared input defaults, includes inherited methods; `empty_method` skips interface/abstract methods; C# enums use the declared base type. Safety: confirmation prompts read `DESTRUCTIVE: <tool> / Target / Machine / Mode / Effect`, the audit records how long the prompt was open and flags sub-1.5 s answers, `safety.allow_elicitation = false` forces the token path, `safety.edit_requires_config_mode = true` refuses code edits unless TwinCAT is in CONFIG; destructive XAE tools refuse via the guard even with no shell attached. IPC: `ipc_eventlog` is confirmed as itself, lists the registered TwinCAT/Beckhoff providers, deletes its temp file; `boot:` resolves to the live 4026 folder (ProgramData), not the stale `C:\TwinCAT\3.1\Boot`. ADS errors 6/7/1797/1808/1861 carry a plain-language hint; TOML config loads on Python 3.10 (tomli). 141 tools.

**0.6.2** — second and third field reports (50 items, same engineering PC). Integer writes are range-checked against the ADS type and BOOL accepts only true/false/0/1 — 70000 into a WORD is refused, not wrapped to 4464; `tc_set_mode` escalation needs a confirmation (lowering stays free); confirmation tokens for XAE destructive actions are bound to the shell's real target at issue time; `ipc_list_dir boot:*` resolves the real boot folder by probing (FFIND ignores path ids) and drops `.`/`..`; `proj_read_file` is confined to the project roots; 4026 PLC command names (`PLC.Loginto<Project>`) are discovered from the shell and the project node is selected when none is active; the nested PLC project node (ItemType 56) is recognised and walked; experiments gain `run.ack: falls|edge|none`, `ack_timeout_ms`, `wait_ms`/`wait_until` steps, `after` as cleanup on failure, `resume=false` archiving earlier runs, `spec_version` on every run; BOOL leaves keep their declared name on 4026 tables; library FB members are expected in `proj_vs_runtime`, counted as instances by `proj_references`, shown as leaf calls in `proj_call_graph`, resolved via the runtime in `proj_resolve_symbol`; `=> x` counts as a write; unqualified type names resolve when unique; confirmed actions audit their final outcome (ok/error); `rec_latency` reports absorbed edges; Infosys DB in WAL mode with serialised indexing; clearer EtherCAT/IO/HMI/AppTimestamp/raw-write messages. Third report: `xae_build check_all_objects=true` discovers the shell's actual command name (`PLC.CheckAllObjects`, localized or 4026-prefixed variants) instead of assuming one; a shell closed and reopened behind the server (RPC_S_SERVER_UNAVAILABLE 0x800706BA) is re-attached automatically on the next call; `proj_lint` reports `root`/`files_indexed`/`parse_errors` and says plainly when `proj_scan` has not run instead of `units: 0`; `eof_newline` removed (XAE never writes one) and `lineids_missing` only fires when a POU is inconsistent (some blocks have LineIds, others not); `hmi_scan` parses TF1800 `.TcVIS` files — `VisualElemMember` paths are read bindings (kind `tcvis`, tagged with the member id name where known), ST input-action assignments are write bindings (kind `tcvis_write`), so `hmi_vs_runtime` covers classic visualizations; `xae_status` attaches to the shell itself. Not done: TwinCAT Scope/Measurement control (`IMeasurementScope` is not IDispatch — needs a .NET host) and visualization *creation* (`.TcVIS` is an opaque XmlArchive; editing it blind would corrupt the file). 141 tools.

**0.6.1** — field fixes from the first real session (Windows 11, TwinCAT 3.1.4026, engineering PC): the launcher no longer blocks the MCP handshake on first start — it builds the venv in a detached background process (exit code 5, reconnect once; a SessionStart hook pre-builds it; `--sync-install` keeps the old behaviour); template placeholders (`<CX_NETID>`, `C:\path\to\…`) and malformed AmsNetIds are dropped with a `config_FIX_THESE` list in `tc_doctor` instead of being loaded; `tc_allow_write` (session allow-list entry, confirmed, audited) and `tc_reload_config` (apply list-type settings without a restart); `xae_restart_twincat` / `xae_activate_configuration` verify the target's mode over ADS and the shell's dialogs and fail with the dialog text instead of reporting success; TwinCAT license errors (0x98110724) mapped to the 7-day-trial remedy; rejected COM calls name the open dialog; a cancelled confirmation prompt is distinguished from a decline; recordings end at their limit immediately and release their notifications from the ingest thread (`rec_status` no longer lags, `rec_export` rows == events, footer reported); Infosys default books index in the background on first use; `python -m twincat_mcp.acceptance` exercises every tool against the live PC/target with a PASS/FAIL/WARN/SKIP/MANUAL report and guard-refusal proofs. 141 tools.

**0.6.0** — experiments (12 tools): YAML spec, background runner with snapshot/profile per run and factor restore, measures from recordings (last/latency/pulses/settle/dwell/edges/expr…), metrics with t/χ² CIs and Cpk, pass/fail/inconclusive verdicts with n-to-settle, Markdown+HTML report with identity/charts/audit/interpretation, compare. Capture profiles (7 tools): `rec_scan` of runtime or source with cost estimates and a markable checklist, `{attribute 'tcmcp.record'}` pragmas collected through `proj_variables` (pragmas now parsed and inherited down instance trees), named profiles replayed as one recording per mode group. `infosys_*` reference lookup (7 tools): sitemap-driven book list, per-library local index with FTS5, symbol search, declarations as data, code-example search, live page → Markdown, sitemap-verified web fallback, TwinCAT 2 filtered out; `scripts/gen_tools_md.py`. Packaging: Claude Code plugin + marketplace in `.claude-plugin/` (`/plugin install twincat-mcp@twincat-mcp`, `userConfig` for Python/AmsNetId/modes), Claude Desktop `.mcpb` bundle (`packaging/mcpb/build.py`, manifest 0.3, tool list generated from the server, passes `mcpb validate`), self-bootstrapping launcher `scripts/launch.py` (+ `launch.cmd`) that creates the venv on first start and reinstalls only when the shipped sources change; skill moved to `skills/twincat-mcp/`; HTTP mode bearer-token gate for claude.ai remote connectors. Plugin path verified end-to-end on Linux (install → venv → `tc_doctor` via `mcp__plugin_twincat-mcp_twincat__tc_doctor`); `launch.cmd` Python discovery is untested on Windows. Windows CI (`.github/workflows/ci.yml`, ubuntu + windows × py3.10/3.12 with a stand-in `TcAdsDll.dll`) found and fixed: `tc_doctor` crashed instead of reporting a dead router; the dialog scan looked at every program's dialogs when no XAE pid was known; a PC without TwinCAT got a ctypes traceback at start instead of the cause. `proj_lint` gains six OOP/safety rules (`interface_contract`, `extends_cycle`, `abstract_instantiation`, `shadowed_member`, `unsigned_loop`, `new_delete`), default-on, zero findings on the sample project. System Service file paths accept OS-independent prefixes (`boot:`, `bootprj:`, `config:`, `user1:`… = ADS path ids); `tcunit_remote_result` defaults to `boot:TcUnit_xUnit_testresults.xml` so the same config serves Windows, CE7 and TC/BSD targets. Refused and declined actions are now audited (`outcome: refused` / `declined-elicitation:*`), not only the ones that ran. `docs/INSTALL.md`: install → config → 14-step zero-write smoke test.

**0.5.0** — dialog watchdog, TwinSAFE guard, XML validation/fixes + LineIds regeneration, TE2000/TcVIS HMI binding index with runtime/source cross-check, I/O disable, variable links, UDP discovery, Windows event log, C#/TypeScript/JSON exporters, snapshot apply, session report, `naming_prefixes` config.

**0.4.0** — composite workflows (`wf_debug_symbol`, `wf_fb_profile`, `wf_release_check`, `wf_project_overview`), MCP prompts as workflow recipes, `rec_compare`, `proj_changed` (git), `proj_scan incremental`, `ads_io_read` (process image), compact symbol listing, size-guarded `ads_read`, separate ADS workers for quick vs long calls, Claude Code integration kit (`integrations/claude-code/`).

**0.3.0** — datatype-table upload and parsing: full nested decode of any variable in one read (`ads_read`), `ads_leaves`, `ads_dump` (every variable on the PLC → JSON + CSV), `ads_types`; recorder prefixes expand through the table (arrays of structs included); `proj_variables` (source-declared variables flattened to instance paths) and `proj_vs_runtime` (source vs online cross-check).

**0.2.0** — real-time recorder (`rec_*`), test bench (`test_*`, `snap_*`), runtime diagnostics (`ads_app_info`, `ads_task_stats`, `ads_ping`), ADS RPC (`ads_call_method`), static checks (`proj_interface`, `proj_lint`, `proj_call_graph`), `tcunit_scaffold`. Review fixes: ROT enumeration (`EnumRunning`), `vsBuildState` constant, raw writes limited to PLC ports with symbol-overlap deny checks, tokens bound to the connected machine, `ads_connect` re-targeting gated, destructive DTE commands classified, project write-back byte-exact (BOM/EOL preserved) and duplicate-name aware, cooperative cancellation of long ADS loops, blocking calls moved off the event loop.

**0.1.0** — initial release.

## Not covered (deliberately)

Scope/TE13xx recording, TE1200 static analysis, NC axis commissioning, Safety (TISC) editing, library repository management, semantic code search, OPC UA. The tree XML tools (`xae_tree_get` / `xae_tree_consume_xml`) reach most of the rest of the System Manager generically.

## Development

```
python -m pytest            # 159 tests (safety matrix, type codec, project parser, fake ADS incl. notifications/RPC, fake COM, recorder, bench, lint, MCP e2e incl. elicitation, packaging)
python packaging/mcpb/build.py      # dist/twincat-mcp-<version>.mcpb; npx @anthropic-ai/mcpb validate runs if Node is present
claude plugin validate .            # plugin + marketplace manifests
python -m twincat_mcp --print-config
python -m twincat_mcp --http 8765   # streamable-http on loopback; --token / TCMCP_HTTP_TOKEN adds a bearer gate for tunnels
```

MIT.
