# twincat-mcp 0.8.16 — tool reference

160 tools, 5 prompts, 3 resources. Generated from the server (`python scripts/gen_tools_md.py`); one line per tool. Class: R = read-only, E = edit (files/XAE project, no effect on the running machine), W = write to the PLC (allow/deny lists), C = control (confirm in guarded), D = destructive (confirm unless TCMCP_CONFIRM=0 in full).

## Meta & safety (8)

- `tc_doctor` [R] — Environment check: platform, pyads/router, local AMS NetId, XAE ProgIDs in the registry, running XAE shells, project root, safety mode, whether a confirmation prompt can reach the user in this session (`elicitation`: the negotiated MCP revision and the client's declared capability). Run this first. Starts with a…
- `tc_safety_status` [R] — Current safety mode (and where it came from), ceiling, allow/deny lists, pending confirmation tokens.
- `tc_set_mode` [C] — Switch safety mode: readonly | guarded | full. Lowering is free. Escalating (guarded→full, readonly→guarded) is capped at max_mode AND needs a confirmation (prompt or confirm_token) — an agent must not be able to widen its own permissions silently.
- `tc_allow_write` [C] — Add (or remove) a write allow-list pattern for THIS session only — the in-session alternative to editing twincat_mcp.toml and reconnecting. fnmatch, case-insensitive: 'GVL_Test.*', 'MAIN.fbDispense.rSetpoint'. Control-class: needs a confirmation (prompt or confirm_token from…
- `tc_reload_config` [E] — Re-read twincat_mcp.toml (and TCMCP_* env) without a restart. Applies: safety allow/deny lists, confirm_destructive, confirm_ttl_s, docs settings, naming prefixes, dialog rules, tcunit result path. Reports — but does not apply — changes that need a restart: ads target/port, solution path, project root, mode/max_mode…
- `tc_request_confirmation` [R] — Issue a single-use confirm_token for a control/destructive tool: action = the tool's exact name. Easiest: pass the tool's own arguments as args (e.g. action='xae_plc', args={'action': 'login', 'plc_project_path': 'TIPC^Plc^Plc Project'}) — the server computes the target the tool will check, the same way the tool…
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
- `ads_write_many` [W] — Write several symbols ({name: value}) together. Every symbol is gated (allow/deny list) and pre-checked (type, range, enum name, array/string length, CONSTANT, pointer/interface) BEFORE anything is written — one refusal → nothing is written and every problem is listed. Then ONE ADS sum-write: the runtime applies all…
- `ads_io_read` [R] — Raw process image of the PLC: area 'I' (%IB inputs) or 'Q' (%QB outputs) at a byte offset — what the task actually sees/wrote, independent of symbol names (link checks).
- `ads_read_raw` [R] — Raw ADS read by index group/offset on any port (hex + little-endian uint).
- `ads_write_raw` [W] — Raw ADS write by index group/offset — PLC runtime ports (851-859) only. Every symbol whose memory overlaps the written range is checked against the write allow/deny lists; the raw target itself is 'raw:<port>/<igrp hex>/<ioffs hex>'.
- `ads_watch` [R] — Trace symbols for duration_ms. Uses ADS notifications (PLC-timestamped, every change the runtime sees at its task cycle — not 50 ms polling from this PC): change_only=true records changes, false one sample per interval_ms. Falls back to polling when notifications are unavailable (method field says which). Caps: 60 s,…
- `ads_wait_until` [R] — Block until symbol <op> value (==, !=, >, >=, <, <=) or timeout. Uses an ADS change notification, so it returns as soon as the runtime sees the condition (met_at_plc_ms = PLC-relative timestamp of that sample); falls back to polling every interval_ms when notifications are unavailable. Returns met/value/elapsed.
- `ads_plc_state` [C/D] — PLC runtime control via ADS: run | stop | reset. 'reset' is a cold reset (all non-persistent data lost). Control-class: confirmation in guarded mode.
- `ads_system_mode` [D] — Switch TwinCAT on the target between 'run' and 'config' mode (restarts the runtime, I/O goes down). Destructive-class. Waits (≤20 s) until the target reports the requested mode and returns before/after; reached=false when it did not get there (license / configuration error).
- `ads_ethercat` [R] — EtherCAT master state (INIT/PREOP/SAFEOP/OP) and slave count via the master's ADS port (default 65535 for the first master; see Device → ADS tab in XAE).
- `ads_routes` [R] — Local static ADS routes (parsed from StaticRoutes.xml on this PC).
- `ads_add_route_to_plc` [C] — Create the PLC-side route back to this PC (UDP discovery protocol, needs the target's Windows/TC-BSD credentials). Control-class.
- `ads_app_info` [R] — PLC application info (TwinCAT_SystemInfoVarList._AppInfo): project/app name, online change count, boot data loaded, task count, logged-in flag, app timestamp.
- `ads_task_stats` [R] — PLC task timing (TwinCAT_SystemInfoVarList._TaskInfo[n]): cycle time, last/min/max/mean execution time over `samples`, load %, cycle-time-exceeded counter delta — the FB's cost on the real-time task.
- `ads_ping` [R] — ADS round-trip time statistics to the target (router/route/network health).
- `ads_call_method` [W] — Invoke an FB method on the running PLC via ADS RPC. The method must carry {attribute 'TcRpcEnable'}. inputs: [{type:'LREAL', value: 12.5}, …] in VAR_INPUT order (use base types: INT not E_Enum); outputs: ['BOOL', …] in VAR_OUTPUT order. Write-class.
- `ads_dump` [R] — Read EVERY variable on the PLC (all top-level symbols, fully expanded through the datatype table — structs, FB instances, arrays of structs) in one pass and write a flat CSV (path,type,value,note) and a nested JSON tree built from the same values, so the two files never disagree. pattern limits the roots ('MAIN.*',…
- `ads_leaves` [R] — Enumerate every scalar leaf under a symbol with byte offsets (datatype table): 'MAIN' → all program variables, 'MAIN.arrSteps' → arrSteps[1].nId … With values=true each leaf is decoded from the symbol's bytes, read in bounded pieces (a value that is not expanded: its head only); value_note says why a value is missing…
- `ads_types` [R] — Runtime datatype table: describe one type (members with offsets/sizes, array bounds, enum literals, attributes) or list types matching a pattern.
- `ads_discover` [R] — Broadcast search for TwinCAT targets on the local network (UDP 48899): NetId, host name, TwinCAT version, IP — the same thing XAE's 'Broadcast Search' does.
- `ads_export` [R] — Generate code from the runtime: fmt=csharp (explicit-offset structs + enums for Beckhoff.TwinCAT.Ads ReadAny/WriteAny), typescript (interfaces), json (type descriptions), or csharp_symbols (const symbol paths under prefix). types = struct/enum names; prefix = take the types of every symbol under it.

## Real-time recorder (21)

- `rec_start` [E] — Start a continuous real-time recording of online PLC variables (runs in the background until rec_stop, duration_s, max_events or stop_when). Select by explicit symbols (an FB or struct instance expands to all its leaves), prefix ('MAIN.fbFill', 'GVL_Sim.aCh[1]' → every leaf under it) or pattern — the same leaves…
- `rec_scan` [R] — Enumerate the variables that could be recorded and what each would cost. source='runtime' walks the live symbol/datatype tables (every scalar leaf under prefix, or all programs/GVLs); source='project' walks the source declarations (works offline). pattern selects leaves exactly as rec_start does: '*'/'?' wildcards,…
- `rec_profile_save` [E] — Save a named capture profile. Sources combine: explicit symbols (mode='auto' picks cycle for REAL/TIME, change for BOOL/INT/enum, poll for strings — or give cycle|change|poll), a marked checklist (path, or the name given to rec_scan), and/or {attribute 'tcmcp.record' := 'cycle'} pragmas in the source…
- `rec_profile_list` [R] — Saved capture profiles (name, symbol count, modes, source).
- `rec_profile_get` [R] — One profile in full: every symbol with its mode and cycle, triggers, per-group cost estimate.
- `rec_profile_delete` [E] — Delete a profile and its checklist file.
- `rec_profile_start` [E] — Start recording a saved profile: one recording per (mode, cycle_ms) group, all named '<profile>/<mode>@<cycle>'. Triggers given here override the profile's. Returns the recording ids; rec_profile_stop(name) stops them together, rec_stats/rec_export work per id. align (default, several groups, no start_when): every…
- `rec_profile_stop` [E] — Stop every live recording started from this profile; returns per-recording status + stats.
- `rec_list` [R] — Recordings of this session (compact status) and recording files on disk, newest first (paged: limit/offset; on_disk_total says how many exist); a recording with Scope: its scope state.
- `rec_status` [R] — Live status of one recording: samples, events, rate, missing_samples (holes in the PLC timeline, cycle mode), completeness, dropped (lost on this PC), backlog, stats_coverage, per-block report, warnings, armed/triggered, last value per symbol; with Scope: scope {state recording | finishing | finished} (N182).
- `rec_snapshot` [R] — Most recent events of a running (or finished) recording — the live tail.
- `rec_stats` [R] — Per-symbol statistics over the whole recording so far: count, first/last, min/max/mean, change count, BOOL rising/falling edges with timestamps, and value runs (state timeline with dwell times). Live (incremental) while complete; recomputed from the file when the ingest had to skip statistics under load, for…
- `rec_capture` [E] — One call for "record X for N s and give me the stats": starts a recording, waits (≤ 60 s), stops, and returns per-symbol statistics, completeness and the rec_verify verdict (integrity + PLC timeline, plus the optional per-cycle `counter`) with the file path. For longer or triggered captures use rec_start.
- `rec_verify` [R] — Prove a finished recording. integrity: the file is byte-for-byte what the recorder wrote (sha256 of every line, signed with this install's key) — 'MODIFIED' if anything was edited. timeline (cycle mode): per notification block, holes between the PLC's own sample timestamps. counter (optional): a PLC variable that…
- `rec_read` [R] — Read a time window of a recording from its file. wide=true: one row per PLC timestamp with one column per selected symbol; a cell is null when that symbol has no sample at that time — nothing is invented (R4). fill=true repeats the last known value into those cells and lists them per row under "_filled" (handy for…
- `rec_stop` [E] — Stop a recording, release the ADS notifications, finalize the file; returns status + stats.
- `rec_export` [R] — Export a recording to CSV. csv_wide: one row per PLC sample time, one column per variable and per array element (name[i]); an empty cell means no sample of that variable at that time (fill=true forward-fills — handy for charts, but a cell then no longer means "sampled now"). csv_long: t_ms,symbol,value. Both include…
- `rec_delete` [D] — Stop (if running) and delete a recording and its exports from disk. Destructive-class (field report R11): a recording is evidence — guarded mode needs tc_request_confirmation(action='rec_delete', target=<id>); the audit keeps the deleted file's size and SHA-256 so its content stays identifiable.
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

- `ipc_eventlog` [D] — Windows event log of the target (Windows 7/10/11 IPCs): runs wevtutil on the IPC via the System Service, reads the result back and deletes the temp file. Also lists the event providers registered on the target whose name contains 'TwinCAT' or 'Beckhoff', so a provider that does not exist there (the default name…
- `ipc_read_file` [R] — Read a file from the target IPC's file system over ADS (no share needed). Binary → base64. Prefer the OS-independent prefixes, which the target resolves itself: 'boot:<name>' (the live TwinCAT boot folder — C:\ProgramData\Beckhoff\TwinCAT\3.1\Boot on 3.1.4026, \Hard Disk\TwinCAT\3.1\Boot on CE7), 'bootprj:',…
- `ipc_list_dir` [R] — List files on the target matching a Win32 pattern. Prefer 'boot:*' (the live boot folder on any target OS; the result names the absolute folder in resolved_pattern) over a hard-coded 'C:\TwinCAT\3.1\Boot\*', which is the stale legacy folder on 3.1.4026. Other prefixes: see ipc_read_file.
- `ipc_write_file` [C] — Write a file on the target IPC (text or base64). Control-class.
- `ipc_delete_file` [D] — Delete a file on the target IPC. Destructive-class.
- `ipc_start_process` [D] — Start a process on the target IPC (NT_StartProcess equivalent). Destructive-class.
- `ipc_reboot` [D] — Reboot (or shut down) the target IPC via the System Service. Destructive-class.

## XAE Automation Interface (28)

- `xae_dialogs` [R] — Modal dialogs currently open in the attached XAE shell (title, class, text, buttons), the watchdog's auto-answer rules and its recent log. A pending dialog blocks every COM call. Any window that holds the shell modal (its main window disabled) is listed, whatever its class (WPF About / Options, WinForms): its buttons…
- `xae_dialog_resolve` [C/D] — Click a button on an open XAE dialog ('OK', 'Yes', 'No', 'Cancel'…). remember=true adds an auto-answer rule for that title (never for machine-affecting prompts). Control-class; machine-affecting prompts (activate/run/download/login/delete/safety) are destructive-class. A window that is not a standard dialog is…
- `xae_io_disable` [E] — Set/clear the Disabled flag on I/O devices under TIID (all, or the named ones) so a configuration activates without the hardware present — TcUnit on a PC, dry runs.
- `xae_links` [R] — Variable links of the PLC instance: every AT %I*/%Q* variable with its link target; lists the unlinked ones (the usual reason an output never moves).
- `xae_attach` [E] — Attach to the running XAE shell / Visual Studio (Running Object Table) that has the solution: solution_path when given (from then on every xae_* tool works on it — flagged solution_mismatch when it is not xae.solution_path), else xae.solution_path. A solution no shell has open is never replaced by another one…
- `xae_status` [R] — Shell version, open solution, projects, TwinCAT project name, target NetId, started flag. Attaches to the shell first if not attached yet (as every xae_* tool does). solution_bound: the solution the xae_* tools work on; solution_mismatch / refused: it is not open (they refuse); reattached: it was found in a shell…
- `xae_open_solution` [E] — Open a .sln in the attached shell.
- `xae_build` [E] — Build the solution. save_first (default) runs File.SaveAll, saves whatever the shell still reports unsaved, and checks that every xae_pou_set / xae_pou_create edit of this session is in the source files — it refuses to build otherwise (an external reload would discard the edits). Returns success, error/warning items…
- `xae_diagnostics` [R] — Current XAE Error List (errors/warnings/messages with file:line — TwinCAT's own entries too, read from the list view when the automation interface reports none; this focuses the Error List window) merged with the Build output pane, plus the pane's tail, without rebuilding. `source` names where each part came from.
- `xae_list_commands` [R] — List DTE command names containing a substring (to discover what this shell exposes).
- `xae_command` [C/D] — Execute an arbitrary DTE command (e.g. 'PLC.Login'). Control-class; commands whose name contains download/reset/activate/restart/delete/remove/shutdown are destructive-class.
- `xae_plc` [C/D] — PLC online actions: login | logout | start | stop | reset_cold | reset_origin | download. First the Automation Interface (ConsumeXml online commands on '<Name> Project' — no active project needed; plc_project_path when there are several PLC projects), then the shell's DTE commands (names vary by build).…
- `xae_target` [C] — Get (net_id=None) or set the target AMS NetId of the TwinCAT project. Setting is control-class.
- `xae_activate_configuration` [D] — ActivateConfiguration on the current target (overwrites the boot project on the IPC). Destructive-class. Follow with xae_restart_twincat.
- `xae_restart_twincat` [D] — StartRestartTwinCAT on the current target (runtime restarts in RUN mode). Destructive-class.
- `xae_deploy` [D] — Save → build → (boot project autostart on) → activate → restart, in one call. Stops at the first build error. Destructive-class (one confirmation covers all steps). Refuses when the XAE target and the ADS target of this server differ. Done = the target runs the new configuration: in RUN and a restart proven…
- `xae_tree_get` [R] — System Manager tree item: properties, children to `depth`, and ProduceXml when xml=true. Path syntax 'TIPC^Plc^Plc Project^POUs^MAIN' (xae_plc_projects gives the '<Name> Project' path; its children POUs/DUTs/GVLs are walked even though the node itself reports no ChildCount). Roots (ITcSysManager::LookupTreeItem…
- `xae_tree_consume_xml` [E] — ConsumeXml on a tree item — the generic write path for any configuration (add I/O boxes, link variables, set task cycle time, add project routes on TIRR …).
- `xae_tree_set_values` [E] — Set element texts in a tree item's XML by relative XPath, e.g. {'TaskDef/CycleTime': '10000'} (unit 100 ns).
- `xae_tree_create_child` [E] — Generic CreateChild (tree item types/subtypes per Infosys). For PLC objects prefer xae_pou_create.
- `xae_tree_delete` [D] — Delete a tree item (POU, box, task …). Destructive-class; the confirmation names the item: tc_request_confirmation(action='xae_tree_delete', target='<parent_path>^<name>').
- `xae_plc_projects` [R] — PLC projects in the TwinCAT project with their tree paths (needed by PLCopen / boot tools).
- `xae_pou_get` [R] — Declaration/implementation text of a POU/DUT/GVL/interface tree item and its methods/properties/actions.
- `xae_pou_set` [E] — Replace declaration and/or implementation text of a POU item inside the live shell (no file round-trip, XAE keeps LineIds consistent). Build afterwards.
- `xae_pou_create` [E] — Create a PLC object under a folder/POU. kind = program | fb | function | method | property | action | transition | struct | enum | union | alias | gvl | interface | itf_method | itf_property | folder | parameter_list, and accessors on their own: property_get | property_set (under a property) | itf_property_get |…
- `xae_plcopen_import` [E] — Import a PLCopen XML file into a PLC project (path from xae_plc_projects). option: none | rename | replace | skip (conflict handling). replace: the existing object keeps its place (folder) and identity and gets the imported text. TwinCAT's own replace deletes it and re-creates the import at the project root, so the…
- `xae_plcopen_export` [E] — Export PLC objects ('POUs.FB_X', 'DUTs.ST_Y' — folder.object) to a PLCopen XML file.
- `xae_boot_project` [E/D] — Boot project settings: set Autostart and/or GenerateBootProject (local, no activate unless activate_after_generate=true, which is then destructive-class).

## Experiments (define → run → evaluate → report) (12)

- `exp_template` [R] — A complete example experiment spec to edit (YAML text + the same as JSON): factors × levels, runs per level, preconditions, start/done conditions, measures (last/latency/pulses/settle/ dwell/edges/expr…), metrics (mean/std/cpk/p95), criteria, writes_allowed. kind: dosing_fill.
- `exp_validate` [R] — Validate a spec (YAML/JSON text, a dict, or a saved experiment by name): structure, measures, metrics, criteria, writes_allowed vs what the spec writes, the server guard's view of each write, symbol resolution on the connected PLC, planned run count and a duration estimate.
- `exp_save` [E] — Save an experiment spec (YAML or JSON) under <experiments_dir>/<name>/spec.yaml. Runs already recorded for that name are kept; resume picks up where they stopped.
- `exp_list` [R] — Experiments on disk with run counts, state and whether a report exists.
- `exp_get` [R] — Spec, status and run summary of one experiment.
- `exp_runs` [R] — Run records: level, status, measures, recording ids, duration, error.
- `exp_run` [C] — Run an experiment in the background: per level × repetition → preconditions → factor values written → snapshot → capture profile started → stimulus → start acknowledged (done_when must go false) → done_when → settle → stop → measures → runs.jsonl. Factor values are read first and restored at the end or on abort (read…
- `exp_status` [R] — Live state of a running experiment: planned/done/failed, current level, ETA, last error, live recording ids. For an experiment from an earlier session, the persisted status.
- `exp_abort` [E] — Stop a running experiment after the current step; recordings are closed, factor values restored.
- `exp_evaluate` [R] — Metrics per level with 95 % CIs and the criteria verdicts (pass / fail / inconclusive with the run count that would settle it). Deterministic from runs.jsonl; rerunnable by anyone.
- `exp_report` [E] — Write <experiments_dir>/<name>/report.md and report.html: identity (machine, TwinCAT version, project, git state, safety mode), criteria table, metrics per level, charts (primary measure per run; per-level mean/CI), run table, the spec, the audit log of writes during the run, and the interpretation — stored…
- `exp_compare` [R] — Two experiments with the same levels side by side: metric deltas and whether their 95 % CIs separate (the before/after question with error bars).

## Project files (offline) (16)

- `proj_libraries` [R] — Library versions (K3): every library reference of each PLC project — the version it asks for, the version it effectively resolves to on this PC ('*' = newest installed) and what is installed in TwinCAT's library repository. Flags a pinned version that is not installed. Reads the .plcproj and folder names only.
- `proj_interface` [R] — Public interface of an FB/program: inputs/outputs/in-out with types and comments, methods (access, signature), properties (get/set), actions, extends/implements.
- `proj_lint` [R] — Static checks over one object or the whole project: unused locals, naming prefixes (override map via `prefixes`), undocumented VAR_INPUT/OUTPUT, TODO markers, REAL compared with =, VAR_INPUT assigned inside an FB, empty methods, GVL/ENUM without qualified_only, magic numbers, FB_init/FB_exit without SUPER^;…
- `proj_call_graph` [R] — Call graph from a program/FB: FB instances it invokes, functions and methods it calls, recursively to `depth` (what runs when this POU runs).
- `proj_variables` [R] — Every variable the project source declares, flattened to runtime instance paths by walking programs and GVLs through FB/STRUCT definitions (MAIN.fbFill.fbValve.bOpen …). scope = one program/GVL/FB to walk instead of all. Library-typed variables stay as one item with their type. A name declared in several PLC projects…
- `proj_vs_runtime` [R] — Cross-check the source against the running PLC: variables declared in the project but not present online (not compiled / not activated / optimised away) and online variables under the same programs/GVLs that the source does not declare (stale boot project, library internals); roots_not_on_target names a program/GVL…
- `proj_validate_xml` [E] — Structural validation of .TcPOU/.TcDUT/.TcGVL files: XML well-formedness, Name/Id, GUID format + project-wide uniqueness, Declaration/Implementation presence, property accessor VAR blocks, LineIds vs implementation line counts, EOF newline. fix=true applies safe fixes (.bak written); regenerate_lineids rebuilds…
- `proj_scan` [R] — Index the project tree (.sln/.tsproj/.plcproj/.TcPOU/.TcDUT/.TcGVL/.TcIO). incremental=true re-indexes only files whose mtime changed (fast after XAE saves).
- `proj_changed` [R] — Units whose source differs from a git revision (default: uncommitted work) plus untracked files — 'what did I change?'; pair with proj_vs_runtime for 'is it deployed?'.
- `proj_list` [R] — List indexed objects. kind: PROGRAM | FUNCTION_BLOCK | FUNCTION | STRUCT | ENUM | UNION | ALIAS | GVL | INTERFACE | METHOD | PROPERTY | ACTION (case-insensitive).
- `proj_get_code` [R] — Source of an object ('FB_Valve', 'FB_Valve.Open', 'MAIN'): declaration/implementation with file line numbers, parsed VAR declarations, sub-items.
- `proj_resolve_symbol` [R] — Map an ADS symbol path (MAIN.fbFill.fbValve.bOpen) to the file:line where each hop is declared, following variable types through FB/STRUCT definitions. Hops inside a library type (Tc2_Standard.CTU.CV) are completed from the runtime's datatype table when the target is reachable. A first name declared in several PLC…
- `proj_search` [R] — Regex search over all declarations/implementations → unit, file, line, text.
- `proj_references` [R] — Where a type/object is instantiated (variables of that type) and mentioned.
- `proj_read_file` [R] — Raw text of a project file (relative to project root or absolute).
- `proj_set_code` [E] — Write declaration/implementation back into the .TcPOU/.TcDUT/.TcGVL file (keeps the rest of the XML byte-identical, writes a .bak, drops the object's stale <LineIds>). Use when XAE is closed or for batch edits; otherwise prefer xae_pou_set.

## HMI bindings (3)

- `hmi_scan` [R] — Index HMI symbol bindings: TE2000 .view/.content/.usercontrol/.json (%s%PLC1.X%/s%) and TF1800 .TcVIS (VisualElemMember paths -> kind 'tcvis'; ST input-action assignments -> kind 'tcvis_write'). root defaults to project.hmi_root, else the project root (which is where .TcVIS files live).
- `hmi_bindings` [R] — Which HMI files/controls bind a PLC symbol (fnmatch on the symbol path).
- `hmi_vs_runtime` [R] — Broken HMI bindings: symbols bound in the HMI that do not exist on the running PLC (and, with check_source, whether the source declares them), plus declared variables with no HMI.

## TcUnit (3)

- `tcunit_scaffold` [E] — Generate <FB>_Tests.TcPOU (TcUnit.FB_TestSuite) with Test_InitialState and one test method per public method, arranged from the FB interface, and register it in the .plcproj. Assertions are left as TODO stubs.
- `tcunit_results` [R] — Parse a TcUnit xUnit result into a pass/fail summary. Source: the file on the target over ADS (default: configured remote path), a local file, or raw XML text.
- `tcunit_run` [D] — Full TcUnit cycle: save (checked) → build → boot project autostart+generate → activate → restart → delete old result → wait for the xUnit file on the target → parse. Destructive-class (it activates a configuration on the target): tc_request_confirmation(action='tcunit_run', target=<NetId XAE targets>).…

## Beckhoff Infosys reference (TwinCAT 3 docs) (7)

- `infosys_books` [R] — List the Infosys books (documentation sets) of the Help Viewer package installed with TwinCAT, with page counts — offline, one index of every book. pattern is a glob on the slug ('tcplclib_*', '*json*'); category: plc-library | function-tf | engineering-te | twincat-system | hardware | other. TwinCAT 2 books are…
- `infosys_index` [R] — The local Help Viewer index holds every book and is built as a whole (the first time by itself, later only with kb_rebuild(index='infosys') once the user approves) — this tool changes nothing here and says so; kb_status / tc_doctor show the index state.
- `infosys_search_symbols` [R] — Find library symbols (function blocks, functions, structs, enums, aliases, interfaces, GVLs) by name fragment in the local Help Viewer index; exact > prefix > contains. kind: FunctionBlock | Function | Program | Method | Property | Action | Enum | Struct | Union | Alias | Interface | GlobalVariable | Library.…
- `infosys_declaration` [R] — Declaration of a library symbol as data: kind, inputs/outputs/in_out (name, type, default, description from the page's table), struct members, enum values, return type, EXTENDS / IMPLEMENTS, methods (name, definition location, description) and properties (type, access, initial value) of an FB, plus the ST declaration…
- `infosys_search` [R] — Full-text search over the local Help Viewer index (FTS5, bm25-ranked, excerpt with [hits]); "quoted phrases" stay together, limit 1..50. Offline — no web search (web_fallback is ignored and said so); results carry the page's doc version and the package date.
- `infosys_search_code` [R] — Search the code examples of the indexed pages for a fragment — a call pattern, an attribute, a constant (case-insensitive substring of the code as written; non-breaking spaces count as spaces). language: one that occurs in the index (an unknown one is refused with the list), '' / 'none' = samples without a label.…
- `infosys_get_page` [R] — A documentation page as Markdown (headings, tables, fenced code), read from the local Help Viewer package — any page in it, indexed or not; doc version + breadcrumb included. ref: full Infosys URL, 'book/pageid', a bare page id, or a symbol name ('ADSREAD'). Offline. Use max_chars to keep it short.

## Other (16)

- `kb_status` [R] — Knowledge base (layout and rules: docs/KNOWLEDGE_BASE.md): where it is (or the question to ask the user if no folder was chosen yet), the project folders indexed for kb_search, the ESI index for hw_lookup — and under `docs` the documentation indexes the infosys_* tools and the twincat-docs skill read:…
- `kb_rebuild` [E/C] — Rebuild one index of the knowledge folder from its source on this PC — ONLY with the user's approval (docs/KNOWLEDGE_BASE.md rule 6: the user is asked, or pass the confirm_token of tc_request_confirmation(action='kb_rebuild', args={'index': …})). index: 'infosys' (the Help Viewer package, the plugin's extractor, in…
- `kb_set_location` [E] — Choose the folder for the knowledge database — ONLY with a path the user gave you (or use_fallback=true once the user agreed to D:\TwinCAT-MCP\knowledge; not both). The folder is checked: a folder (not a file, not a drive root) on a local fixed drive (no network share / mapped drive / USB), not inside…
- `kb_add_project` [E] — Index a project folder for kb_search (cross-project pattern search) — ONLY a folder the user named. Reads .TcPOU/.TcDUT/.TcGVL/.TcIO beneath it into the knowledge database; nothing is indexed by default. Refused: the plugin's folders, the knowledge folder, a folder inside or around one already indexed (its units…
- `kb_remove_project` [E] — Remove a project folder from the knowledge base (index entries only; the files are untouched). Refused in readonly mode; audited (N127).
- `kb_reindex` [E] — Re-index one project folder (or every changed one) after edits. Refused in readonly mode; audited (N127).
- `kb_search` [R] — Search the code of the project folders the user added ("alarm handler", "FB_Valve", "CONCAT"): unit, kind, file and the matching declaration/code excerpts, best first. Names and identifiers count as their words (FB_AlarmList: alarm, list). Units with EVERY word first, then every word as a prefix ('CounterMod' finds…
- `hw_lookup` [R] — EtherCAT device data from the ESI index (index\esi.db: the ESI files in the knowledge folder's manuals\ and TwinCAT's ESI folders): name, product code, revisions, E-bus current, mailbox, DC modes, sync managers, PDOs with every entry (index, subindex, bit length, type; gaps marked), the predefined PDO assignments,…
- `hw_search` [R] — Full-text search of the ESI index: devices (names, PDOs and their entries, PDO assignments), CoE objects (names, descriptions, sub-index names), diagnosis messages (text, description, causes, remedies) and modules. kinds: any of 'device', 'object', 'diag', 'module' (default all; a module is found by its own name and…
- `hw_object` [R] — One CoE object of a device from its ESI (newest revision unless `revision`): every sub-index with type, bit size and offset, access, PDO mapping, backup/setting flags, the default (decoded from DefaultData, enum text when there is one), min/max, unit code, enumeration texts; array elements one by one. index: '#x8000'…
- `hw_diag` [R] — A diagnosis message by its text id as the diagnosis history shows it (object 0x10F3; '#x4101', '0x4101'): message text, description, causes and remedies, class, reaction, reset — from the device's own catalogue when `device` is given, else every distinct message with that id and the device types / modules that use it.
- `hw_module` [R] — A module of a modular device (AX8000, EP/EL with slots, TSC …) by its ModuleIdent as hw_lookup's slots give it: PDOs, CoE objects, diagnosis messages. With `device`, the module that device's slot means (hw_lookup's rule: the device's own file, then the module file paired with it by name, then the only one), else…
- `scope_status` [R] — TwinCAT Scope on this PC: where TC3ScopeExportTool.exe is (TE130x Scope View / TF3300 Scope Server) and the default export folder.
- `scope_export` [E] — Export a saved TwinCAT Scope recording (.svdx / .svd) to CSV with Beckhoff's TC3ScopeExportTool.exe (command line `"svd=<file>" target=<csv> silent`; CSV needs no extra licence). Default target: <recordings>/scope/<name>.csv ([scope] export_dir). The CSV is Beckhoff's output, unchanged — channels, time column and…
- `scope_list` [R] — TwinCAT Scope configurations in the solution open in XAE (TwinCAT Measurement projects). Their channels, sample time and record time are what scope_record / rec_start(also_scope=true) record.
- `scope_record` [E] — Drive a Scope configuration open in XAE (Automation Interface IMeasurementScope): action = start | stop | save (SaveSVD → <recordings>/scope/<scope>-<time>.svdx, or path=<file>.svdx; the .csv exported from that file with Beckhoff's export tool — XAE's ExportCSV only without it, flagged, and never over an existing…

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
