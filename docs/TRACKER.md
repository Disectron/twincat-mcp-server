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
| 0.8.3 | local Infosys against the real `BKINFOSYS3_VS_100_EN-US.2.mshc` (built from the layout in the knowledge README, tested with a synthetic package) | `infosys_declaration ADSREAD` lists NETID…TMOUT; `infosys_get_page FB_CXProfiler` has `version` + `breadcrumb`, markdown without breadcrumb text; `tc_doctor` → infosys `source.kind = helpviewer` |

## Waiting on Ryan

- `D:\TwinCAT-MCP\knowledge\sop\scope\ScopeAI.cs` — the real property names, before `scope_add_channel` /
  `scope_set_axis` / `scope_set_sample_time` can be built (W, N16, #44).
- One raw page HTML from the `.mshc` if the 0.8.3 parse checks above fail.
- Retest of 0.8.1–0.8.3.

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
| #17 | `xae_tree_get('TIPC', depth=3)` does not descend into the project |
| #45 | Check all objects does not run |
| #10 | login needs the PLC project node selected |
| #49 / N9 | stale System Manager after solution reopen (partly fixed) |
| #79 | modal dialog raised by the command itself |

## On hold

- TcVIS / HMI bindings (#48, #29) — until Ryan picks it up again.
