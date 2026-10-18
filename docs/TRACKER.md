# Open items

What is open, what waits on whom, and what was decided. Update with every release (`docs/RELEASING.md`).
IDs: `R*`/`A*`/`#*`/`N*` from the field reports (`twincat-mcp-issues.md`, `twincat-mcp-retest.md`), `W*` from the
tool wishlist (2026-10-18).

## Not yet run on real hardware

| Since | What | Check |
|---|---|---|
| 0.8.0 | native capture engine (`tcmcp-capture.exe`, TcAdsDll router mode) | `tc_doctor` → recorder native, `sha256_verified: true`; `rec_capture symbols=[GVL_Test.aCounter] seconds=10 cycle_ms=1` → `engine: native`, PASS |
| 0.8.1 | sealing on Windows: read-only recording + manifest, `rec_delete` of a read-only file | covered by Windows CI only |
| 0.8.2 | Scope via the XAE Automation Interface (`IMeasurementScope`, late-bound) | `scope_list`; `scope_record` start → stop → save; `rec_start(also_scope=true)` + `rec_stop` → `<id>.svdx/.scope.csv/.scope.json` |
| 0.8.2 | `scope_export` with the real `TC3ScopeExportTool.exe` | `scope_export svd=<saved .svdx>` |
| 0.8.4 | native re-subscription after RUN → CONFIG → RUN and the `_stall` line on PLC STOP (tested on the simulator backend) | `rec_capture … cycle_ms=1 seconds=40`, toggle RUN/CONFIG → `resubscriptions ≥ 1`; PLC stop → `label: INCOMPLETE`, `label_reason: PLC stopped …` |
| 0.8.4 | restart verification (`modes_seen`, `transition_seen`) | `xae_restart_twincat` → `modes_seen` contains a non-RUN state, `duration_s` > 0; without license → error naming CONFIG |
| 0.8.4 | Infosys index time on the real package (one transaction per book) | `tc_doctor` → infosys `complete: true`; time for 282 books |
| 0.8.5 | D44 dirty-document walk (`Document.Saved`, `Project.Saved`, nested `SubProject`) in the real shell | `xae_pou_set` → `xae_build` → `save.dirty_before` names the POU, `edits_on_disk` lists it, the .TcPOU has the text |
| 0.8.6 | D20 Error List: late-bound `ToolWindows.ErrorList`, else the view copy (focus + clipboard, restored) | `xae_diagnostics` with Scope warnings in the Error List → they are in `items`, `source` names the route |
| 0.8.6 | #10 Solution Explorer via the late-bound DTE / window object; 'Set as Active PLC Project' command | `xae_plc('logout')` while logged in → `command: PLC.Logoutfrom<name>`, `selected_node` set |
| 0.8.6 | D41 4026 compiler line format | a build error in a method → `file`, `object`, `line`, `code` filled |
| 0.8.5 | D37 abort timing: `abort_received.during` vs. the run that actually stopped | `exp_run` 10 runs, `exp_abort` during run 6 → state `aborted`, run 6 `aborted`, its `after` ran, no run 7 |
| 0.8.3 | local Infosys against the real `BKINFOSYS3_VS_100_EN-US.2.mshc` (built from the layout in the knowledge README, tested with a synthetic package) | `infosys_declaration ADSREAD` lists NETID…TMOUT; `infosys_get_page FB_CXProfiler` has `version` + `breadcrumb`, markdown without breadcrumb text; `tc_doctor` → infosys `source.kind = helpviewer` |

## Waiting on Ryan

- `D:\TwinCAT-MCP\knowledge\sop\scope\ScopeAI.cs` — the real property names, before `scope_add_channel` /
  `scope_set_axis` / `scope_set_sample_time` can be built (W, N16, #44).
- One raw page HTML from the `.mshc` if the 0.8.3 parse checks above fail.
- D5 (BOOL members inside structs read as 1/0): the exact call and output — the fake decodes them as true/false.
- Retest of 0.8.4.

## Decided (wishlist 2026-10-18)

| Item | Decision |
|---|---|
| `ads_write(dry_run=true)` / `ads_write_many` / `ads_write_raw` | build — never consumes a token, never prompts, result is advisory |
| `rec_window(id, condition)` | build |
| `rec_export_verify(id, path)` | build — with a decoder independent of the exporter |
| `rec_wait(id, timeout_s)` | build — capped (≈ 300 s), returns "still running" past the cap |
| `tc_preflight` | fold into `tc_doctor` (advisory; the gate decides at call time) |
| `rec_engine_status` / `rec_engine_install` | rejected — exe ships in the plugin since 0.8.1; `tc_doctor` shows it |
| tool groups that load together | rejected as a server feature — deferred loading is the client's; `[server] tool_groups` exists |
| `kb_add_note` | index `.md` under the knowledge folder (SOPs) instead; results labelled operator SOP / Beckhoff doc / known error |
| hardware manuals | plugin reads `manualsdb\manuals.db` (built by `manuals_db.py`); PyMuPDF (AGPL-3.0) stays out of the public plugin |
| Infosys source | local Help Viewer package, no network (0.8.3) |
| profiler | `prof_task` first (no code change, `_TaskInfo` via the lossless recorder); per-POU instrumentation later, method per target (CX9020 / WEC7 Arm has no `GETCPUACCOUNT`/`PROFILER` → `FB_CXProfiler`, wall-clock); budgets on max + `CycleTimeExceeded`, not p99; CIs with batch means (autocorrelated cycles); verify that `GETCPUACCOUNT` excludes pre-emption before relying on it; evaluate TE1210 |

## Open from the field reports

| ID | What |
|---|---|
| R12 / A6 | `rec_verify` over many counters (pattern/list) and the 300 × 1 ms acceptance run per recording method; capacity documented |
| #45 | Check all objects does not run |
| #49 / N9 | stale System Manager after solution reopen (partly fixed) |
| #79 | modal dialog raised by the command itself |

### From the 0.8.3 deep test

All items fixed up to 0.8.6 except D5 (waiting on Ryan, above).

## On hold

- TcVIS / HMI bindings (#48, #29) — until Ryan picks it up again.
