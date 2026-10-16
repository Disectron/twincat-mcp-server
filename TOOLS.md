# twincat-mcp 0.6.0 — tool reference

127 tools, 5 prompts, 3 resources. Generated from the server (`python scripts/gen_tools_md.py`); one line per tool. Class: R = read-only, E = edit (files/XAE project, no effect on the running machine), W = write to the PLC (allow/deny lists), C = control (confirm in guarded), D = destructive (confirm unless TCMCP_CONFIRM=0 in full).

## Meta & safety (6)

- `tc_doctor` [R] — Environment check: platform, pyads/router, local AMS NetId, XAE ProgIDs in the registry, running XAE shells, project root, safety mode. Run this first.
- `tc_safety_status` [R] — Current safety mode, ceiling, allow/deny lists, pending confirmation tokens.
- `tc_set_mode` [E] — Switch safety mode: readonly | guarded | full. Escalation is capped at the configured max_mode (TCMCP_MAX_MODE); lowering is always allowed.
- `tc_request_confirmation` [R] — Issue a single-use confirm_token for a control/destructive tool. Use the exact action name of the tool (e.g. 'xae_activate_configuration') and its target.
- `tc_audit_tail` [R] — Last n audit records (mutating calls, confirmations, mode changes).
- `tc_session_report` [R] — Markdown handover of this session: mode changes, writes, deploys and other mutating calls from the audit log, recordings, snapshots and dumps produced. Written to ~/.twincat_mcp/reports/<ts>.md (or out_path).

## Workflows (one call, whole chain) (4)

- `wf_debug_symbol` [R] — One-call debugging dossier for a variable: live value, declaring file:line chain, the statements that write it, code excerpts around them, and a short change watch.
- `wf_fb_profile` [E] — Profile an FB instance for duration_s: records every leaf (change mode), samples task timing in parallel, then reports most-active symbols, boolean pulse rates, state dwell timelines, silent symbols and the FB's cost on the task. Read-only on the PLC.
- `wf_release_check` [E] — Go/no-go before a deploy: git changes, build result, lint, source-vs-runtime, TcUnit result on the target, task health. Writes nothing to the PLC.
- `wf_project_overview` [R] — Orientation in one call: index summary, programs with call trees, FBs with interfaces, GVLs/DUTs, lint summary, git state.

## Runtime — ADS (27)

- `ads_connect` [C] — (Re)connect to a target. ams_net_id like '5.12.34.56.1.1', 'local' for this PC, None = keep current; port 851 = PLC runtime 1. Re-targeting to a NetId other than the configured one is control-class (it changes which machine every later write hits).
- `ads_status` [R] — Composite status: connection, PLC runtime state, TwinCAT system mode (RUN/CONFIG), device name/version. With port=N returns only the ADS state of that port (852 PLC2, 500 NC, 10000 system service, 65535 EtherCAT master 1).
- `ads_list_symbols` [R] — Browse the runtime symbol table. pattern is fnmatch ('MAIN.*', '*valve*'; bare text is wrapped in '*'). type_filter substring-matches the type ('ST_', 'BOOL', 'FB_'). flag filters by symbol flag: PERSISTENT, READONLY, STATIC, ATTRIBUTES … compact=true returns 'name : type' strings only (one fifth of the tokens).
- `ads_symbol_info` [R] — Type, size, index group/offset, flags (READONLY, PERSISTENT …) and comment of one symbol.
- `ads_read` [R] — Read one symbol with type-aware decoding. Structs/FB instances/arrays of structs decode into a nested tree (datatype table); TIME/DATE/DT/TOD get readable text; enums get names. Trees larger than max_chars are summarised (member names + sizes) — use ads_leaves or ads_dump for the full content.
- `ads_read_many` [R] — Read several symbols in one ADS sum-command. Returns {name: value | {error}}.
- `ads_write` [W] — Write a symbol on the running PLC. Accepts JSON scalars, lists for arrays, and literals like 'T#1s500ms', 'D#2026-10-15', '16#FF'. Returns before/after. Gated: readonly=never; guarded=write_allowlist; full=denylist only.
- `ads_write_many` [W] — Write several symbols sequentially ({name: value}). Each one is gated and audited; stops at the first refusal.
- `ads_io_read` [R] — Raw process image of the PLC: area 'I' (%IB inputs) or 'Q' (%QB outputs) at a byte offset — what the task actually sees/wrote, independent of symbol names (link checks).
- `ads_read_raw` [R] — Raw ADS read by index group/offset on any port (hex + little-endian uint).
- `ads_write_raw` [W] — Raw ADS write by index group/offset — PLC runtime ports (851-859) only. Every symbol whose memory overlaps the written range is checked against the write allow/deny lists; the raw target itself is 'raw:<port>/<igrp hex>/<ioffs hex>'.
- `ads_watch` [R] — Sample symbols for duration_ms every interval_ms (polling trace). change_only keeps only rows where a value changed. Caps: 60 s, ≥10 ms, 5000 rows. Use for timing questions: 'does bDone pulse?', 'how long is the valve open?'.
- `ads_wait_until` [R] — Block until symbol <op> value (==, !=, >, >=, <, <=) or timeout. Returns met/elapsed.
- `ads_plc_state` [C/D] — PLC runtime control via ADS: run | stop | reset. 'reset' is a cold reset (all non-persistent data lost). Control-class: confirmation in guarded mode.
- `ads_system_mode` [D] — Switch TwinCAT on the target between 'run' and 'config' mode (restarts the runtime, I/O goes down). Destructive-class.
- `ads_ethercat` [R] — EtherCAT master state (INIT/PREOP/SAFEOP/OP) and slave count via the master's ADS port (default 65535 for the first master; see Device → ADS tab in XAE).
- `ads_routes` [R] — Local static ADS routes (parsed from StaticRoutes.xml on this PC).
- `ads_add_route_to_plc` [C] — Create the PLC-side route back to this PC (UDP discovery protocol, needs the target's Windows/TC-BSD credentials). Control-class.
- `ads_app_info` [R] — PLC application info (TwinCAT_SystemInfoVarList._AppInfo): project/app name, online change count, boot data loaded, task count, logged-in flag, app timestamp.
- `ads_task_stats` [R] — PLC task timing (TwinCAT_SystemInfoVarList._TaskInfo[n]): cycle time, last/min/max/mean execution time over `samples`, load %, cycle-time-exceeded counter delta — the FB's cost on the real-time task.
- `ads_ping` [R] — ADS round-trip time statistics to the target (router/route/network health).
- `ads_call_method` [W] — Invoke an FB method on the running PLC via ADS RPC. The method must carry {attribute 'TcRpcEnable'}. inputs: [{type:'LREAL', value: 12.5}, …] in VAR_INPUT order (use base types: INT not E_Enum); outputs: ['BOOL', …] in VAR_OUTPUT order. Write-class.
- `ads_dump` [R] — Read EVERY variable on the PLC (all top-level symbols, fully expanded through the datatype table — structs, FB instances, arrays of structs) in one pass and write a nested JSON tree plus a flat CSV (path,type,value). pattern limits the roots ('MAIN.*', 'GVL_*'); system/library symbols excluded unless include_system.…
- `ads_leaves` [R] — Enumerate every scalar leaf under a symbol with byte offsets (datatype table): 'MAIN' → all program variables, 'MAIN.arrSteps' → arrSteps[1].nId … With values=true each leaf is decoded from one read of the whole symbol.
- `ads_types` [R] — Runtime datatype table: describe one type (members with offsets/sizes, array bounds, enum literals, attributes) or list types matching a pattern.
- `ads_discover` [R] — Broadcast search for TwinCAT targets on the local network (UDP 48899): NetId, host name, TwinCAT version, IP — the same thing XAE's 'Broadcast Search' does.
- `ads_export` [R] — Generate code from the runtime: fmt=csharp (explicit-offset structs + enums for Beckhoff.TwinCAT.Ads ReadAny/WriteAny), typescript (interfaces), json (type descriptions), or csharp_symbols (const symbol paths under prefix). types = struct/enum names; prefix = take the types of every symbol under it.

## Real-time recorder (19)

- `rec_start` [E] — Start a continuous real-time recording of online PLC variables (runs in the background until rec_stop, duration_s, max_events or stop_when). Select by explicit symbols (an FB or struct instance expands to all its leaves), prefix ('MAIN.fbFill' → every leaf under it) or fnmatch pattern. mode: 'cycle' = one…
- `rec_scan` [R] — Enumerate the variables that could be recorded and what each would cost. source='runtime' walks the live symbol/datatype tables (every scalar leaf under prefix, or all programs/GVLs); source='project' walks the source declarations (works offline). pattern = fnmatch filter on the full path. Returns counts by type and…
- `rec_profile_save` [E] — Save a named capture profile. Sources combine: explicit symbols (mode='auto' picks cycle for REAL/TIME, change for BOOL/INT/enum, poll for strings — or give cycle|change|poll), a marked checklist (path, or the name given to rec_scan), and/or {attribute 'tcmcp.record' := 'cycle'} pragmas in the source…
- `rec_profile_list` [R] — Saved capture profiles (name, symbol count, modes, source).
- `rec_profile_get` [R] — One profile in full: every symbol with its mode and cycle, triggers, per-group cost estimate.
- `rec_profile_delete` [E] — Delete a profile and its checklist file.
- `rec_profile_start` [E] — Start recording a saved profile: one recording per (mode, cycle_ms) group, all named '<profile>/<mode>@<cycle>'. Triggers given here override the profile's. Returns the recording ids; rec_profile_stop(name) stops them together, rec_stats/rec_export work per id.
- `rec_profile_stop` [E] — Stop every live recording started from this profile; returns per-recording status + stats.
- `rec_list` [R] — Recordings of this session (live status) and recording files on disk.
- `rec_status` [R] — Live status of one recording: events, rate, drops, armed/triggered, last value per symbol.
- `rec_snapshot` [R] — Most recent events of a running (or finished) recording — the live tail.
- `rec_stats` [R] — Per-symbol statistics over the whole recording so far: count, first/last, min/max/mean, change count, BOOL rising/falling edges with timestamps, and value runs (state timeline with dwell times) — computed incrementally, no re-read of the file.
- `rec_read` [R] — Read a time window of a recording from its file. wide=true pivots to one row per event with all selected symbols forward-filled (what you want for 'what else was true when X happened'). Works on finished recordings from earlier sessions too.
- `rec_stop` [E] — Stop a recording, release the ADS notifications, finalize the file; returns status + stats.
- `rec_export` [R] — Export a recording to CSV: csv_wide (t_ms + one column per symbol, forward-filled) or csv_long (t_ms,symbol,value). Default path next to the JSONL.
- `rec_delete` [E] — Stop (if running) and delete a recording and its exports from disk.
- `rec_pulses` [R] — Pulse / dwell analysis of one recorded symbol: for BOOL → pulse count, high/low widths, period, frequency, duty (fine-dosing valve pulses); for other types → state timeline with dwell times.
- `rec_latency` [R] — Edge-to-edge latency in a recording: from each time `from_symbol from_op from_value` becomes true to the next time `to_symbol to_op to_value` becomes true (PLC timestamps). E.g. bOpenCmd == TRUE → rFlow > 0.5.
- `rec_compare` [R] — Before/after comparison of two recordings: per-symbol stat deltas (count, min, max, mean, changes, edges) and pulse width/period deltas for BOOLs — timing regression after a code change.

## Test bench (4)

- `test_expect` [R] — Assert conditions on live values. checks: [{symbol, op, value, tolerance?}], op ∈ == != > >= < <= ~= (relative/abs tolerance) in between. within_ms>0 waits until all hold. Returns pass/fail per check with the time each one was met.
- `test_stable` [R] — Assert a value does not move more than tolerance for duration_ms (settle check for a weight, pressure, PID output, state).
- `test_response` [W] — Write a stimulus ({symbol: value}) and measure the PLC-timestamped latency until `response` ('MAIN.fbValve.bOpen == TRUE') holds — how fast does the FB react? Writes are gated per symbol; originals are restored afterwards unless restore=false.
- `test_scenario` [W] — Scripted FB test against the live PLC with a recording in parallel. spec = {name, record:{symbols|prefix|pattern, mode, cycle_ms}, steps:[{at_ms?, write:{sym:val}} | {expect:[checks], within_ms} | {wait_ms} | {wait_until:{symbol,op,value,timeout_ms}} | {stable:{symbol,duration_ms,tolerance}}], final_expect:[checks],…

## Snapshots (4)

- `snap_save` [E] — Save a named snapshot of live values (expanded like rec_start) for regression comparison — parameters, recipes, whole FB instances before/after a change.
- `snap_compare` [R] — Diff snapshot `a` against `b` (another snapshot or 'live'), numeric tolerance applies.
- `snap_list` [R] — Saved snapshots.
- `snap_apply` [W] — Write a saved snapshot back to the PLC (recipe / parameter restore). dry_run lists the differences only. Every differing symbol is gated like ads_write.

## Target OS — System Service (7)

- `ipc_eventlog` [D] — Windows event log of the target (Windows 7/10/11 IPCs): runs wevtutil on the IPC via the System Service, then reads the result file back. Not available on Windows CE / TC-BSD targets (use the XAE error list for ADS logger messages there). Destructive-class because it starts a process on the IPC.
- `ipc_read_file` [R] — Read a file from the target IPC's file system over ADS (no share needed), e.g. 'C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml' or a log. Binary → base64.
- `ipc_list_dir` [R] — List files on the target matching a Win32 pattern, e.g. 'C:\TwinCAT\3.1\Boot\*'.
- `ipc_write_file` [C] — Write a file on the target IPC (text or base64). Control-class.
- `ipc_delete_file` [D] — Delete a file on the target IPC. Destructive-class.
- `ipc_start_process` [D] — Start a process on the target IPC (NT_StartProcess equivalent). Destructive-class.
- `ipc_reboot` [D] — Reboot (or shut down) the target IPC via the System Service. Destructive-class.

## XAE Automation Interface (28)

- `xae_dialogs` [R] — Modal dialogs currently open in the attached XAE shell (title, text, buttons), the watchdog's auto-answer rules and its recent log. A pending dialog blocks every COM call.
- `xae_dialog_resolve` [C/D] — Click a button on an open XAE dialog ('OK', 'Yes', 'No', 'Cancel'…). remember=true adds an auto-answer rule for that title (never for machine-affecting prompts). Control-class; machine-affecting prompts (activate/run/download/login/delete/safety) are destructive-class.
- `xae_io_disable` [E] — Set/clear the Disabled flag on I/O devices under TIID (all, or the named ones) so a configuration activates without the hardware present — TcUnit on a PC, dry runs.
- `xae_links` [R] — Variable links of the PLC instance: every AT %I*/%Q* variable with its link target; lists the unlinked ones (the usual reason an output never moves).
- `xae_attach` [E] — Attach to the running XAE shell / Visual Studio (Running Object Table). Picks the instance holding solution_path when given. launch=true starts a new shell (slow).
- `xae_status` [R] — Shell version, open solution, projects, TwinCAT project name, target NetId, started flag.
- `xae_open_solution` [E] — Open a .sln in the attached shell.
- `xae_build` [E] — Build the solution (File.SaveAll first by default). Returns success, error/warning items with file:line (VS error list, or parsed from the Build output pane) and the output tail. check_all_objects=true runs PLC 'Check all objects' instead (compiles unused POUs).
- `xae_diagnostics` [R] — Current error list (errors/warnings/messages with file:line) plus the tail of the Build output pane, without rebuilding.
- `xae_list_commands` [R] — List DTE command names containing a substring (to discover what this shell exposes).
- `xae_command` [C/D] — Execute an arbitrary DTE command (e.g. 'PLC.Login'). Control-class; commands whose name contains download/reset/activate/restart/delete/remove/shutdown are destructive-class.
- `xae_plc` [C/D] — PLC online actions through the shell: login | logout | start | stop | reset_cold | reset_origin | download. Command names vary by shell version; all candidates are tried. download/reset_cold/reset_origin are destructive-class, the rest control-class.
- `xae_target` [C] — Get (net_id=None) or set the target AMS NetId of the TwinCAT project. Setting is control-class.
- `xae_activate_configuration` [D] — ActivateConfiguration on the current target (overwrites the boot project on the IPC). Destructive-class. Follow with xae_restart_twincat.
- `xae_restart_twincat` [D] — StartRestartTwinCAT on the current target (runtime restarts in RUN mode). Destructive-class.
- `xae_deploy` [D] — Save → build → (boot project autostart on) → activate → restart, in one call. Stops at the first build error. Destructive-class (one confirmation covers all steps). Refuses when the XAE target and the ADS target of this server differ.
- `xae_tree_get` [R] — System Manager tree item: properties, children to `depth`, and ProduceXml when xml=true. Path syntax 'TIPC^Plc^Plc Project^POUs^MAIN'. Roots: TIID (I/O), TIPC (PLC), TIRC (real-time/tasks), TIRR (routes), TINC (NC).
- `xae_tree_consume_xml` [E] — ConsumeXml on a tree item — the generic write path for any configuration (add I/O boxes, link variables, set task cycle time, add project routes on TIRR …).
- `xae_tree_set_values` [E] — Set element texts in a tree item's XML by relative XPath, e.g. {'TaskDef/CycleTime': '10000'} (unit 100 ns).
- `xae_tree_create_child` [E] — Generic CreateChild (tree item types/subtypes per Infosys). For PLC objects prefer xae_pou_create.
- `xae_tree_delete` [D] — Delete a tree item (POU, box, task …). Destructive-class.
- `xae_plc_projects` [R] — PLC projects in the TwinCAT project with their tree paths (needed by PLCopen / boot tools).
- `xae_pou_get` [R] — Declaration/implementation text of a POU/DUT/GVL/interface tree item and its methods/properties/actions.
- `xae_pou_set` [E] — Replace declaration and/or implementation text of a POU item inside the live shell (no file round-trip, XAE keeps LineIds consistent). Build afterwards.
- `xae_pou_create` [E] — Create a PLC object under a folder/POU: kind = program | fb | function | method | property | action | transition | struct | enum | union | alias | gvl | interface | itf_method | itf_property | folder | parameter_list. Optional code is set after creation.
- `xae_plcopen_import` [E] — Import a PLCopen XML file into a PLC project (path from xae_plc_projects). option: none | rename | replace | skip (conflict handling).
- `xae_plcopen_export` [E] — Export PLC objects ('POUs.FB_X', 'DUTs.ST_Y' — folder.object) to a PLCopen XML file.
- `xae_boot_project` [E/D] — Boot project settings: set Autostart and/or GenerateBootProject (local, no activate unless activate_after_generate=true, which is then destructive-class).

## Project files (offline) (15)

- `proj_interface` [R] — Public interface of an FB/program: inputs/outputs/in-out with types and comments, methods (access, signature), properties (get/set), actions, extends/implements.
- `proj_lint` [R] — Static checks over one object or the whole project: unused locals, naming prefixes (override map via `prefixes`), undocumented VAR_INPUT/OUTPUT, TODO markers, REAL compared with =, VAR_INPUT assigned inside an FB, empty methods, GVL/ENUM without qualified_only, magic numbers, FB_init/FB_exit without SUPER^. rules=…
- `proj_call_graph` [R] — Call graph from a program/FB: FB instances it invokes, functions and methods it calls, recursively to `depth` (what runs when this POU runs).
- `proj_variables` [R] — Every variable the project source declares, flattened to runtime instance paths by walking programs and GVLs through FB/STRUCT definitions (MAIN.fbFill.fbValve.bOpen …). scope = one program/GVL/FB to walk instead of all. Library-typed variables stay as one item with their type.
- `proj_vs_runtime` [R] — Cross-check the source against the running PLC: variables declared in the project but not present online (not compiled / not activated / optimised away) and online variables under the same programs/GVLs that the source does not declare (stale boot project, library internals). Use after a deploy to prove the target…
- `proj_validate_xml` [E] — Structural validation of .TcPOU/.TcDUT/.TcGVL files: XML well-formedness, Name/Id, GUID format + project-wide uniqueness, Declaration/Implementation presence, property accessor VAR blocks, LineIds vs implementation line counts, EOF newline. fix=true applies safe fixes (.bak written); regenerate_lineids rebuilds…
- `proj_scan` [R] — Index the project tree (.sln/.tsproj/.plcproj/.TcPOU/.TcDUT/.TcGVL/.TcIO). incremental=true re-indexes only files whose mtime changed (fast after XAE saves).
- `proj_changed` [R] — Units whose source differs from a git revision (default: uncommitted work) plus untracked files — 'what did I change?'; pair with proj_vs_runtime for 'is it deployed?'.
- `proj_list` [R] — List indexed objects. kind: PROGRAM | FUNCTION_BLOCK | FUNCTION | STRUCT | ENUM | UNION | ALIAS | GVL | INTERFACE | METHOD | PROPERTY | ACTION (case-insensitive).
- `proj_get_code` [R] — Source of an object ('FB_Valve', 'FB_Valve.Open', 'MAIN'): declaration/implementation with file line numbers, parsed VAR declarations, sub-items.
- `proj_resolve_symbol` [R] — Map an ADS symbol path (MAIN.fbFill.fbValve.bOpen) to the file:line where each hop is declared, following variable types through FB/STRUCT definitions.
- `proj_search` [R] — Regex search over all declarations/implementations → unit, file, line, text.
- `proj_references` [R] — Where a type/object is instantiated (variables of that type) and mentioned.
- `proj_read_file` [R] — Raw text of a project file (relative to project root or absolute).
- `proj_set_code` [E] — Write declaration/implementation back into the .TcPOU/.TcDUT/.TcGVL file (keeps the rest of the XML byte-identical, writes a .bak, drops the object's stale <LineIds>). Use when XAE is closed or for batch edits; otherwise prefer xae_pou_set.

## HMI bindings (3)

- `hmi_scan` [R] — Index HMI symbol bindings: TE2000 .view/.content/.usercontrol/.json (%s%PLC1.X%/s%) and TcVIS files. root defaults to project.hmi_root, else the project root.
- `hmi_bindings` [R] — Which HMI files/controls bind a PLC symbol (fnmatch on the symbol path).
- `hmi_vs_runtime` [R] — Broken HMI bindings: symbols bound in the HMI that do not exist on the running PLC (and, with check_source, whether the source declares them), plus declared variables with no HMI.

## TcUnit (3)

- `tcunit_scaffold` [E] — Generate <FB>_Tests.TcPOU (TcUnit.FB_TestSuite) with Test_InitialState and one test method per public method, arranged from the FB interface, and register it in the .plcproj. Assertions are left as TODO stubs.
- `tcunit_results` [R] — Parse a TcUnit xUnit result into a pass/fail summary. Source: the file on the target over ADS (default: configured remote path), a local file, or raw XML text.
- `tcunit_run` [D] — Full TcUnit cycle: build → boot project autostart+generate → activate → restart → delete old result → wait for the xUnit file on the target → parse. Destructive-class (it activates a configuration on the target). plc_project_path from xae_plc_projects.

## Beckhoff Infosys reference (TwinCAT 3 docs) (7)

- `infosys_books` [R] — List Infosys books (documentation sets) from the site's own sitemap, with page counts and whether they are indexed locally. pattern is a glob on the slug ('tcplclib_*', '*json*'); category: plc-library | function-tf | engineering-te | twincat-system | hardware | other. TwinCAT 2 books are excluded. First call…
- `infosys_index` [R] — Index a book (or glob of books, e.g. 'tcplclib_tc3_*') into the local cache: fetches every page that is new or changed since the last run (sitemap lastmod), extracts title/kind/library/ declaration, builds the full-text index. Tc2_System ≈ 300 pages ≈ 30-60 s. Needed before infosys_search_symbols/search_code can see…
- `infosys_search_symbols` [R] — Find library symbols (function blocks, functions, structs, enums, aliases, interfaces, GVLs) by name fragment in the indexed books; exact > prefix > contains. kind: FunctionBlock | Function | Program | Method | Property | Action | Enum | Struct | Union | Alias | Interface | GlobalVariable | Library. library:…
- `infosys_declaration` [R] — Declaration of a library symbol as data: kind, inputs/outputs/in_out (name, type, default, description from the page's table), struct members, enum values, return type, EXTENDS / IMPLEMENTS, plus the ST declaration text and the page URL. The thing to call before writing code against an FB you have not used before.
- `infosys_search` [R] — Full-text search over the indexed Infosys pages (FTS5, bm25-ranked, excerpt with [hits]). When the local index has nothing, falls back to a DuckDuckGo site: query whose results are verified against the Infosys sitemap (TwinCAT 3 only) and fetched for title/excerpt; the 'backend' field says which path answered.
- `infosys_search_code` [R] — Search the code examples (<pre> blocks) of indexed pages for a fragment — a call pattern, an attribute, a constant. language: st | xml | cpp | csharp. Returns the matching lines with context and the page URL.
- `infosys_get_page` [R] — A documentation page as Markdown (headings, tables, fenced code). ref: full Infosys URL, 'book/pageid', a bare page id, or a symbol name ('ADSREAD'). Fetched live and cached; works for any page that exists on the site, indexed or not. refresh=true bypasses the cache.

## Prompts (5)

- `debug_symbol`(symbol) — Investigate why a PLC variable has the value it has
- `fb_validation`(instance, criteria) — Validate a function block against its specification on the live machine
- `deploy_safely`(change) — Build, check and deploy a change to the target with evidence
- `performance_audit`(scope) — Find what costs cycle time and where timing is marginal
- `onboard_project` — Get oriented in an unfamiliar TwinCAT project

## Resources (3)

- `twincat://config` — res_config
- `twincat://audit/recent` — res_audit
- `twincat://safety` — res_safety
