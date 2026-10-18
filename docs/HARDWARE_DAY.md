# First day on the real engineering PC and CX9020

Everything in this repository was built and tested against fakes on Linux. This is the order to
exercise it on hardware so that each failure is isolated to one layer, with what to paste back when
something breaks. Use a **non-production target** for anything from step 6 on.

## 0. Prerequisites and install (20 min) — `INSTALL.md` has the long form and the smoke test; do that first

On the engineering PC, before anything else:

| need | check | why |
|---|---|---|
| TwinCAT XAE 4024+ with the solution open, PLC project activated on the CX | shell up, target RUN | every ADS and XAE tool assumes it |
| ADS route PC → CX9020 | XAE System → Routes shows the CX, or `tc_doctor` `target_state` later | no route = every ADS call fails with 0x6 / 0x7 |
| 64-bit Python 3.12 | `python -c "import struct,sys;print(struct.calcsize('P')*8, sys.version)"` → `64 3.12.x` | pyads loads the 64-bit `TcAdsDll.dll`; a 32-bit Python fails at import |
| Claude Code ≥ 2.1, logged in, GitHub account able to read the repo | `claude --version` | plugin install pulls from GitHub |
| XAE and Claude Code at the same elevation level (both normal, or both admin) | — | COM ROT does not cross the elevation boundary; `xae_attach` would not see the shell |

Install, in a terminal opened **in the solution folder** (the server looks for `twincat_mcp.toml` there):

```
claude
/plugin marketplace add Disectron/twincat-mcp-server
/plugin install twincat-mcp@twincat-mcp
/plugin configure twincat-mcp@twincat-mcp      python_exe if "python" is not on PATH; ams_net_id = the CX's NetId (x.x.x.x.1.1)
/mcp                                           ← plugin servers show here, not in `claude mcp list`
```

The first start builds a private venv under `%LOCALAPPDATA%` (about a minute); `/mcp` shows the server as
failed until it finishes — run `/mcp` again or start a new session. `claude --mcp-debug` prints the
launcher's stderr; a missing-DLL start prints its own three-line remedy.

Config: copy `twincat_mcp.toml.example` to `twincat_mcp.toml` in the solution folder and set the solution
path, `tcunit_remote_result = 'boot:TcUnit_xUnit_testresults.xml'` (already the default; CE7's boot folder
is `\Hard Disk\TwinCAT\3.1\Boot`), the allow list for step 7, and `[docs]`. Keep `mode = guarded` all day:
every write needs the allow list, every destructive action needs a confirmation.

Driving it: just ask Claude Code in plain words ("run tc_doctor", "read MAIN.fbDispense"); the
`twincat-mcp` skill picks the tool chain. Tool names appear as `mcp__plugin_twincat-mcp_twincat__<tool>`.

## 1. tc_doctor — the router and the shell (5 min)

Expected: `router: ok`, `local_ams_net_id` = this PC's NetId, `target_state` = RUN/`ads_state_name`,
`xae_progids_registered` contains `TcXaeShell.DTE.15.0` (4024), `xae_running` lists the open shell
with the solution path, `infosys.fts5: true`.

| symptom | layer | paste back |
|---|---|---|
| `router: FAIL … local AMS router not reachable` | TwinCAT System Service not running (tray icon red), or wrong-bitness Python | tray icon colour; Python bitness (`python -c "import struct;print(struct.calcsize('P')*8)"`) |
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
proj_validate_xml                    ← on XAE-written files expect LineIds info/warnings at most, no error
proj_lint                            ← read the error-severity items only
rec_scan prefix=MAIN.fbDispense source=project checklist=dispense
```

Rule for this step: the project builds clean in XAE, so any `error` from `proj_validate_xml` or
`proj_lint` on it is a false positive in this repo — paste the finding and the POU. Warnings and
info are advisory and may be argued with later.

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
ipc_list_dir boot:*                     ← FFILEFIND layout (item 7) AND the path-id protocol: on the CX9020 (CE7) the
                                          boot folder is \Hard Disk\TwinCAT\3.1\Boot, which 'boot:' must resolve to.
                                          If it errors, retry ipc_list_dir '\Hard Disk\TwinCAT\3.1\Boot\*' and report both.
ipc_read_file boot:CurrentConfig.xml    ← FOPEN with path id 4 (the TcUnit result path uses the same route)
ads_ethercat
wf_release_check → xae_deploy            ← confirmation dialog expected
tcunit_run
ipc_start_process <an exe that exists on the target>  ·  ipc_reboot   (test box only; CE7 has no notepad.exe —
                                          use \Windows\cmd.exe or skip; ipc_eventlog is Windows-only, not CE7)
```

## Stop conditions

Stop and report, do not work around: a write to a symbol not on the allow list going through; any tool
changing the machine without a confirmation prompt in guarded mode; `rec_status.dropped` > 0 at a
profile you need; `exp_run` leaving a factor at its last level after abort (`snap_compare` shows it);
an `error` from an offline check on code XAE accepts.

## What to send back for anything that fails

The tool's error text (it carries the ADS / HRESULT code), the `tc_doctor` output once, and for the
datatype-table and Infosys items the raw artefact (TMC excerpt / saved HTML). One constant is the
usual fix; the second most common is a ProgID or a DTE command name.
