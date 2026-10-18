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
| 0.8.4 | native re-subscription after RUN → CONFIG → RUN and the `_stall` line on PLC STOP (tested on the simulator backend) | `rec_capture … cycle_ms=1 seconds=40`, toggle RUN/CONFIG → `resubscriptions ≥ 1`; PLC stop → `label: INCOMPLETE`, `label_reason: PLC stopped …` |
| 0.8.4 | restart verification (`modes_seen`, `transition_seen`) | `xae_restart_twincat` → `modes_seen` contains a non-RUN state, `duration_s` > 0; without license → error naming CONFIG |
| 0.8.4 | Infosys index time on the real package (one transaction per book) | `tc_doctor` → infosys `complete: true`; time for 282 books |
| 0.8.5 | D44 dirty-document walk (`Document.Saved`, `Project.Saved`, nested `SubProject`) in the real shell | `xae_pou_set` → `xae_build` → `save.dirty_before` names the POU, `edits_on_disk` lists it, the .TcPOU has the text |
| 0.8.5 | D37 abort timing: `abort_received.during` vs. the run that actually stopped | `exp_run` 10 runs, `exp_abort` during run 6 → state `aborted`, run 6 `aborted`, its `after` ran, no run 7 |
| 0.8.6 | D20 Error List: late-bound `ToolWindows.ErrorList`, else the view copy (focus + clipboard, restored) | `xae_diagnostics` with Scope warnings in the Error List → they are in `items`, `source` names the route |
| 0.8.6 | #10 Solution Explorer via the late-bound DTE / window object; 'Set as Active PLC Project' command | `xae_plc('logout')` while logged in → `command: PLC.Logoutfrom<name>`, `selected_node` set |
| 0.8.6 | D41 4026 compiler line format | a build error in a method → `file`, `object`, `line`, `code` filled |
| 0.8.7 | #10/#45 PLC online commands and Check all objects via the Automation Interface (`ConsumeXml` `<OnlineSettings><Commands>` on `TIPC^<Plc>^<Plc> Project`; `ITcPlcIECProject2.CheckAllObjects`) — `ResetColdCmd` is an assumed tag name | `xae_plc('login')` with no active PLC project → `route: automation_interface`; `xae_check_all_objects` → `route` names CheckAllObjects, errors in the Error List |
| 0.8.7 | R14/R15 `plcopen_import` on the nested IEC project with `bFolderStructure` | import an exported POU with `options='replace'` → `added` empty, `duplicates_created` empty |
| 0.8.7 | R7 native tail slack after a manual stop (hypothesis: blocks unsubscribed one after another; simulator test only) | `rec_start … cycle_ms=1`, `rec_stop` → `missing_tail` 0, `completeness` 1.0 |
| 0.8.7 | D29 aligned profile groups (`_TaskInfo[1].CycleCount` trigger) | `rec_profile_start` on a change + cycle profile → `aligned.trigger`; both files start within one task cycle |
| 0.8.7 | R18 Infosys lookup tables built in the background on the real package | `tc_doctor` → infosys `warm: true` within a minute; first `infosys_search_code ADSREAD(` fast |
| 0.8.8 | `twincat-docs` against the real `infosys_en.db` (51,656 pages) and `manuals.db` — the acceptance table of the docs-skill spec, run on fixtures built by the real builders | `kb_docs.py docs_search query=ADSREAD limit=3` → total 39, first `tc3_c/743169291`; `docs_declaration symbol=MC_MoveAbsolute` → `ambiguous`; `manual_search query=watchdog` → CX9020.pdf p62 |
| 0.8.8 | the skill is picked up from the installed plugin and `kb_status` → `docs.call` names the real script (launcher passes `TCMCP_PLUGIN_SRC`) | `/plugin` update → a documentation question loads twincat-docs; `kb_status` → `docs.call` runs as given |
| next | builder stubs: the server turns the `tools\` copies into stubs at start; the stubs find the plugin's builders (pointer, else the plugin cache) | `kb_status` → `docs.builders.*.copy_kind: stub`, `tools\*.v1.bak` beside them; `tools\manuals_db.py stats` works; after the next plugin update it runs the new builder without any copy |
| 0.8.9 | Python 3.12 only: the existing plugin venv (built by 0.8.8) on the engineering PC | after `/plugin` update, the first start rebuilds the venv if it was not 3.12 (background install, reconnect); `tc_doctor` → `python` 3.12.10 |
| 0.8.9 | R42 `ads_dump` on the TcUnit project (`GVL_TcUnit.TestSuiteAddresses`, 1,000 × POINTER TO FB_TestSuite) | `ads_dump()` defaults → `TestSuiteAddresses` 1,000 rows of `0x…` addresses, `MAIN.*` / `GVL_Test.*` present, JSON < 64 MB, no `.partial` left; a forced timeout → no final files, message names the phase |
| 0.8.9 | R15 `plcopen_import(option='replace')` of a POU living in a folder | `added` names the copy, `replaced` names the original, `copies_removed` the copy; the original has the new text after save + build |
| 0.8.9 | R41 level trigger with several notification blocks (simulator only) | `rec_start … start_when='MAIN.nCycle >= 0'` with symbols from two tasks/blocks → every block's first sample on the trigger cycle |
| 0.8.9 | R32 wrapped type names on the real `.mshc` pages (synthetic HTML only) | `infosys_get_page ADSREAD` → `T_AmsNetId` in one piece; if not, one raw page HTML (Waiting on Ryan) |
| 0.8.9 | N37 `manuals_db.py update` on the four manuals (first run reads all again) | EL3356 `toc` without `capacity` / `only)` / `parameterization` as level-1 chapters; `show EL3356 56` without the running header |
| 0.8.9 | builders v2 on the real package (≈ 51,656 pages): first `index` upgrades the version-1 `infosys_en.db` (link table), swap while the MCP server is running | `<Python 3.12> builders\infosys_local.py index --kb D:\TwinCAT-MCP\knowledge` → "older index format", then `stats` → complete with `links_resolved`; reconnect → instructions line says complete |

## Waiting on Ryan

- `D:\TwinCAT-MCP\knowledge\sop\scope\ScopeAI.cs` — the real property names, before `scope_add_channel` /
  `scope_set_axis` / `scope_set_sample_time` can be built (W, N16, #44).
- One raw page HTML from the `.mshc` if the 0.8.3 parse checks above fail.
- D5 (BOOL members inside structs read as 1/0): the exact call and output — the fake decodes them as true/false.
- Retest of 0.8.9 (the 0.8.8 retest items R42, #10, R7, R41, R15, R23, R11, R24, R32, N31, N32, N34–N37; builders).
- Knowledge folder: after the plugin update the server turns `tools\infosys_local.py` / `manuals_db.py` into stubs (old copies kept as `.bak`); the slim README.md (no builder internals) replaces the old one once.
- D19 (ESI PDOs read empty): the PDOs are template references (`Ref="TxEL3xxNComplex10-10V"`, `Chn`); the template
  definition's format is unknown here — the ESI file that holds it (e.g. `C:\TwinCAT\3.1\Config\Io\EtherCAT\Beckhoff EL31xx.xml`).

## Decided (wishlist 2026-10-18)

Not built yet: everything marked "build" / "fold" / "index" / "reads" below, and `prof_task`.

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
| R12 / A6 | `rec_verify` over many counters (pattern/list) and the 300 × 1 ms acceptance run per recording method; capacity documented — not built (`rec_verify` takes one counter) |
| #45 | Check all objects did not run — 0.8.7 calls `ITcPlcIECProject2.CheckAllObjects` first (unverified, above) |
| infosys index twice | the `infosys_*` tools keep their own index of the Help Viewer package (`<kb>\infosys\helpviewer-1033`, ≈ 372 MB) next to `helpviewer\infosys_en.db`; one index (the tools reading `infosys_en.db`) is open — docs-skill spec §8 |
| SOP indexing | `sop\*.md` listed by `kb_status`, not searchable yet (decided: index them; wishlist above) |
| R32 (part) | Infosys "T_Ams NetId" table split — 0.8.9 joins it when a page is read (declared name or Type column); the cause in the real HTML is not seen yet (unverified, above) |
| D19 | ESI PDOs that are template references (`Ref=…`, `Chn`) are not resolved — waiting on the ESI file |
| #49 / N9 | stale System Manager after solution reopen (partly fixed) |
| #79 | modal dialog raised by the command itself |

### From the 0.8.3 deep test

All items fixed up to 0.8.7 except D5 and D19 (waiting on Ryan, above).

## On hold

- TcVIS / HMI bindings (#48, #29) — until Ryan picks it up again.
