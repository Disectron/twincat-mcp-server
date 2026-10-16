# First day on the real engineering PC and CX9020

Everything in this repository was built and tested against fakes on Linux. This is the order to
exercise it on hardware so that each failure is isolated to one layer, with what to paste back when
something breaks. Use a **non-production target** for anything from step 6 on.

## 0. Install (10 min)

```
/plugin marketplace add C:\tools\twincat-mcp        (or <owner>/<repo>)
/plugin install twincat-mcp@twincat-mcp
/plugin configure twincat-mcp@twincat-mcp           python_exe if "python" is not on PATH; ams_net_id of the CX
```

First session start builds the venv (about a minute). If the server shows as failed, start a new
session — the venv is already there. `claude --mcp-debug` shows the launcher's stderr.
Put a `twincat_mcp.toml` in the solution folder (copy `twincat_mcp.toml.example`): solution path,
allow/deny lists, `[docs]`, and set `TCMCP_MODE=guarded` for the whole day.

## 1. tc_doctor — the router and the shell (5 min)

Expected: `router: ok`, `local_ams_net_id` = this PC's NetId, `target_state` = RUN/`ads_state_name`,
`xae_progids_registered` contains `TcXaeShell.DTE.15.0` (4024), `xae_running` lists the open shell
with the solution path, `infosys.fts5: true`.

| symptom | layer | paste back |
|---|---|---|
| `router: FAIL` | pyads / TcAdsDll | Python bitness (`python -c "import struct;print(struct.calcsize('P')*8)"`) |
| `target_state: FAIL … 0x6` | no route / target CONFIG | `ads_routes` output |
| `xae_running: []` with the shell open | elevation mismatch or ProgID | whether XAE runs as admin; `xae_list_commands` error text |

## 2. Reads only (15 min) — verify items 1, 12, 14

```
ads_status
ads_list_symbols pattern=MAIN.*  limit=50
ads_read MAIN.fbDispense                        ← a whole FB; expect a nested dict, no raw_hex
ads_types ST_<one of your DUTs>
ads_leaves MAIN.fbDispense with_values=true     ← offsets must match the TMC; compare three members by hand
ads_read <an ARRAY OF STRUCT variable>
ads_task_stats samples=50
ads_app_info
```

If `ads_read` on an FB returns `decoder: none` or wrong members: paste the `ads_types` output for that
type and the TMC excerpt. That is the datatype-table tail parsing (verify item 14) and it is the one
most likely to need a constant changed.

## 3. Documentation (5 min) — verify item 15

```
infosys_get_page ADSREAD
infosys_index tcplclib_tc2_system
infosys_declaration ADSREAD
```

Expected: title `ADSREAD`, `kinds` mostly FunctionBlock/Function/Struct/Enum, inputs NETID…TMOUT with
descriptions. If titles read "Beckhoff Information System" or everything is `Page`: save one page
(Ctrl+S, HTML only) and paste it — the HTML walker needs the real markup.

## 4. Project files, no PLC involvement (10 min)

```
proj_scan
proj_variables scope=MAIN           ← compare count with ads_leaves MAIN
proj_vs_runtime                      ← should be clean on a freshly activated project
proj_lint
rec_scan prefix=MAIN.fbDispense source=project checklist=dispense
```

## 5. XAE, read-only (10 min) — verify items 2–4

```
xae_attach
xae_status                            ← target_net_id, solution, projects
xae_pou_get MAIN
xae_tree_get "TIPC^<your PLC project>"  (path from xae_plc_projects)
xae_build                              ← error_source should be Windows.ErrorList or DTE2.ToolWindows
xae_list_commands PLC                  ← note which PLC.* commands the 4024 shell has (item 5)
```

Modal dialogs: `xae_dialogs` shows them; nothing is auto-answered except what `dialog_rules` lists.

## 6. First recording (15 min) — verify item 11

```
rec_profile_save smoke symbols=[MAIN.fbDispense.ePhase, MAIN.fbScale.rNet_g, <a valve BOOL>] mode=auto cycle_ms=1
rec_profile_start smoke
   … run one fill by hand …
rec_profile_stop smoke
rec_stats <id>  ·  rec_pulses <id> <valve BOOL>  ·  rec_export <id> csv_wide
ads_task_stats samples=200          ← run it with and without a profile live; exceed counter must not move
```

Edge timestamps should be plausible against the task cycle (1 ms). If `dropped` > 0 in `rec_status`,
thin the profile before trusting timings.

## 7. One controlled write (5 min)

With `TCMCP_WRITE_ALLOW` naming exactly one harmless symbol (a recipe setpoint on a stopped machine):

```
snap_save pre prefix=GVL_Recipe
ads_write GVL_Recipe.rTarget_g 1000
snap_compare pre live
snap_apply pre
tc_audit_tail
```

## 8. Experiment, dry first (20 min + fills)

```
exp_template                                   ← adapt symbols to your names; keep runs_per_level ≥ 5
exp_validate spec=<yaml>                       ← every symbol must resolve; no "guard refuses"
exp_save spec=<yaml>
exp_run <name> dry_run=true                    ← plan, nothing written
exp_run <name> max_runs=1                      ← one real fill, watch exp_status
exp_runs <name>                                ← measures sane? latency, pulses, final error
exp_run <name>                                 ← the rest
exp_evaluate <name>  ·  exp_report <name> interpretation="..."
```

Open `experiments/<name>/report.html`. Items to check against your own numbers: final error per run,
cut latency vs what the HMI shows, pulse count vs the valve counter.

## 9. Deploy path (only on the test box) — verify items 5–10

```
xae_plc login                           ← if "no working DTE command", paste xae_list_commands PLC
ipc_read_file C:\TwinCAT\3.1\TwinCAT.xml
ipc_list_dir C:\TwinCAT\3.1\Boot\*       ← FFILEFIND layout (item 7)
ads_ethercat
wf_release_check → xae_deploy            ← confirmation dialog expected
tcunit_run
ipc_start_process notepad.exe  ·  ipc_reboot   (test box only)
```

## What to send back for anything that fails

The tool's error text (it carries the ADS / HRESULT code), the `tc_doctor` output once, and for the
datatype-table and Infosys items the raw artefact (TMC excerpt / saved HTML). One constant is the
usual fix; the second most common is a ProgID or a DTE command name.
