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
| 0.8.17 | Scope part with one owner: `rec_start(also_scope=true, duration_s=5)` and let it end; again with `rec_stop` cancelled (stop the client call) during its stop; `rec_delete` of a recording while Scope runs; a second `rec_start` while the first one ends | `rec_status` → `scope.state` recording → finishing → finished, `finished_by` "the recording ended by itself (duration …)" / "rec_stop" / "rec_stop (the call ended before its Scope part — finished after it)" / "rec_delete"; `rec_delete` of a recording whose Scope still records → no .svdx / .scope.csv of that id in `<recordings>\scope`; of one whose Scope part was saved (it had ended) → the files stay, listed in `scope_files_kept`; Scope stops every time (`scope_list` / the Scope View) |
| 0.8.17 | XAE busy or a dialog open when a recording with Scope ends: open Tools → Options in XAE, let the recording end, close Options, `rec_stop` | while Options is open `rec_status` → `scope.state: recording`, `stop_refused`, note "rec_stop asks again"; after closing it `rec_stop` stops and saves Scope; a dialog Scope itself opens during StopRecord → `state: stop_unknown`, StopRecord not sent again |
| 0.8.17 | N186: `rec_start(also_scope=true, duration_s=5)`, let it end, then a 2nd `rec_start(also_scope=true, duration_s=3)` without touching Scope | the 2nd: `scope.error` "Scope recording NOT started: Scope refused StartRecord — it is in state Reply …" (no Scope files for it); after Disconnect in XAE's Scope View a 3rd one records (`start_check` "confirmed by the Scope View log …" — or which line it found); no `.scope.json` ever links a CSV whose "Starttime of export" lies before its recording |
| 0.8.17 | N185: any also_scope recording; count today's `[ ERROR ]` lines in `TcScopeView*.log` since the call | `scope_view_log` has exactly those (no "a new log" note while the log only grew); `truncated` when more than 20 |
| 0.8.17 | N187: `rec_capture engine=python` 5 s 1 ms `MAIN.nCycleCount`; `rec_start engine=python start_when=…` | no TypeError, no `unverified_rows`, verdict PASS with the counter |
| 0.8.17 | N188 / N181b: `xae_tree_consume_xml` bad XML; `xae_build` with an error; Tools → Options open, then `xae_tree_consume_xml` | error says "XAE messages (…): none, the buffer was read and is empty" or "not read: …"; a successful action's result has `xae_messages_note` when the buffer gave nothing; with Options open: "Nothing of this call ran (it was refused at its first step)" |
| 0.8.17 | N178: answer a confirmation question after more than 10 minutes; request a `confirm_token` and use it after `confirm_ttl_s` (180 s default) | two limits, both by design: the question (elicitation round) expires after 10 minutes — "confirmation expired … nothing was done"; a token from `tc_request_confirmation` after `confirm_ttl_s` |
| 0.8.16 | XAE messages (ITcSysManager2::GetLastErrorMessages): `xae_activate_configuration`, `xae_build` with an error, `xae_tree_consume_xml` with a wrong XML, `xae_deploy` | the results carry `xae_messages` (action, messages, lines) with TwinCAT's own text, also warnings on success; a failed action's error ends with "XAE messages (…)"; an `earlier_messages` block when the buffer held something before the action |
| 0.8.16 | N175: `xae_plcopen_export` + `xae_plcopen_import` replace of an interface (I_Module) on TwinCAT 3.1.4026; then of an interface with a property | `checked_against: xml`; methods from the export's `InterfaceAsPlainText`; no refusal for a member that is there. The property shape (`<Properties><Property>` with `InterfaceAsPlainText`, `GetAccessor`) is tested on a constructed file, not yet on a real 4026 export — send the export if the property is refused or `not_in_xml` |
| 0.8.16 | N179 / N180 / N182 Scope: a numbered `TcScopeView_1.log`; RUN→CONFIG→RUN during `rec_start(also_scope=true)`; a recording that ends by itself (duration) | `scope_view_log.logs_checked` lists every numbered log, `logs_written` the one written; the save says "Scope was reset to Config by a TwinCAT restart at <time> (Scope View log)" (Config without a reset line since the call began: "the Scope View log names no reset since …"; a log missing or unreadable: "whether TwinCAT was restarted is not known"; another state: only the state Scope is in) and the CSV is `recovered` or `lost`; `rec_status` / `rec_list` show the Scope part `finished` with `finished_by: "the recording ended by itself (duration …)"` without `rec_stop`; a `rec_stop` gives `finished_by: rec_stop` — one cancelled or timed out before its Scope part `rec_stop (the call ended before its Scope part — finished after it)`; `rec_delete` stops Scope and saves nothing (`finished_by: rec_delete`; a delete that fails after stopping the recording: `rec_delete (the call ended …)`, saved) |
| 0.8.16 | N178: leave a confirmation question open for more than 10 minutes, then answer | the question says it expires after 10 minutes; the late answer is refused, logged in server.log and audited `confirmation expired` |
| 0.8.16 | N181 / N176: open Tools → Options, then `xae_tree_get TIPC` and `xae_tree_consume_xml` | the read says "Nothing was changed (a read-only call) … call again"; the edit says "Steps of this call before it may have run … check the result"; XAE's own main frame is never listed by `xae_dialogs` as a dialog — when the shell is modal and no window can be named, it is one `unidentified` entry naming the main window |
| 0.8.16 | N141b with Plc2 + PRG_B: `wf_debug_symbol PRG_B.nBValue`; Python engine footer: `rec_capture … engine=python start_when=…` | the resolution names Plc2 as `proj_resolve_symbol` does; footer `verdict_hint: PASS`, `overflow_samples: 0`, `missing_head/gaps/tail` |
| 0.8.15 | Infosys format 7: approve `kb_rebuild(index='infosys')` | before approval the format-6 index answers and says `rebuild_needed`; after: the build log counts "symbols documented inside pages"; `docs_declaration symbol=RecipeManCommands.CreateRecipe` → Method, return DWORD, `section`; `symbol=ERR_RECIPE_NOT_FOUND` → Constant `16#4003`, note "reconstructed"; `symbol=FB_TcMessage.Send` → I_TcMessage's page, `defined_in`; `symbol=FB_ActuatorActuals` → the `ActualsActuator` page with `declared_as`; `symbol=F_TimeToSeconds()` → no declaration, `syntax_header_note` naming F_SecondsToTime's page; `infosys_search "THIS pointer"` → THIS first; `infosys_search ADS limit=5 offset=5` → the next five, none repeated |
| 0.8.15 | ESI format 2: approve `kb_rebuild(index='esi')` | `hw_lookup EL6653` and `EL6653-0010` → `coe_count` equals the length of `coe_objects` (17); `hw_object device=EL3356-0010 index=8000:11` → that one sub-index (17); `hw_search query=Ch limit=100` → under a second, `next_offset` set |
| 0.8.15 | manuals format 6: approve `kb_rebuild(index='manuals')` | `manual_product code=ED2008` (or an AL / PS code) → its manual, no "copy its PDF"; `manual_search query=0x8000:11` and `query=°C` → hits |
| 0.8.15 | N173 / N174: open Help → About (then Tools → Options) in XAE, then any `xae_*` call; again with `xae.visible=false` | the call says a modal window blocks XAE and names it; a call XAE rejects → "XAE is busy … did not run this call"; a server crash leaves its traceback in `~\.twincat_mcp\logs\server.log` |
| 0.8.15 | N162: reconnect the MCP server after a knowledge-folder change | the connection instructions say when they were read and point to `kb_status` |
| 0.8.14 | Infosys format 6: approve `kb_rebuild(index='infosys')` | before approval the format-5 index answers and says `rebuild_needed`; after: `docs_page FB_WG_ComboFilter` → `children` Configure / Call / Reset with refs; `docs_declaration FB_WG_ComboFilter.Call` → the Call page, `parent` FB_WG_ComboFilter; the 4 tf5200 pages without U+FFFD; `docs_search 0x745` → the ADS return-code table first, tf6500's copies in `same_page_in`; `Tc3_EventLogger` `EqualsTo` of two FBs stays two answers |
| 0.8.14 | N127 / N128 / N136: `tc_set_mode readonly`, then `kb_reindex`, `kb_add_project`, `kb_rebuild index=esi force=true` | all refused "readonly mode", each a `refused` line in `tc_audit_tail`; in guarded: `kb_reindex` audited ok; `kb_add_project` of the plugin folder, the knowledge folder, `…\Project15\Plc` (inside an indexed folder) refused with the reason; `kb_set_location` on a file → "a file, not a folder" |
| 0.8.14 | N129–N135 on P15: `kb_search "ADS error 1808 fbCounter"`, `"nCycleCount 0x705"`, `"License"`, `"Ads-Error 0xA2"`, `"MAX_ALARMS"`, `"nErrorCode"`, `"bEnable OR bReset"`, `kinds=["POU"]` | fbCounter's unit + known_error 0x710 with code; nCycleCount hits, no known_error; no licence note; `known_error: null` + covered, no word search; GVL_Param / ST_ModuleStatus first with `declares`; `operators_note`; POUs listed |
| 0.8.14 | N132 / N133 with a scratch folder (UTF-16 `.TcPOU`, cp1252 file, malformed XML, `Backup\` copy) | `kb_add_project` names `parse_error_files`, `read_notes` (Windows-1252), `not_in_project`; the UTF-16 unit is searchable; after editing a file or renaming the folder, `kb_search` lists it in `stale` and `kb_status` gives its state |
| 0.8.14 | XAE: reopen Project15.sln in the same shell, then `xae_status` / `xae_plc_projects`; close the shell and open the solution in a new one, then `xae_plc login` | the first call after the reopen works (N120); `reattached` is on the `xae_plc` result itself |
| 0.8.14 | multi-PLC: a GVL linked into both PLC projects (`..\Common\GVL_Common.TcGVL`), Plc2 on 852 | `proj_variables` on 852 lists GVL_Common; `proj_variables scope=Plc2@MAIN` on 851 answers Plc2's MAIN |
| 0.8.13 | N116 with two XAE shells (Project15 configured, DrumFillingMachine open in a second shell) | close Project15's solution → `xae_status` says the solution is not open, every `xae_*` call refused, nothing answered for DrumFillingMachine; reopen it → the next call re-attaches (`reattached`) without `xae_attach`; `xae_attach(solution_path=<other>)` → `solution_mismatch: true` |
| 0.8.13 | N113 / N114 with a second PLC project (both with MAIN) and after removing it (its folder left on disk) | `xae_pou_set` on Plc2's MAIN → `xae_build` (save_first) → `edits_on_disk`; `rec_start` on port 852 → `task_check.MAIN` names Plc2's task; after Plc2 is removed from the tree: `proj_scan` → `plcproj_not_in_project` lists Plc2, `rec_start` on 851 → `task_check.MAIN` names PlcTask |
| 0.8.13 | N117 / N118 / Scope View log: `rec_start(also_scope=true)` 60 s, `rec_stop`; `scope_export` of that `.svdx` | `export_csv.via` = export tool from the saved .svdx, CSV rows = the .svdx's export; `scope_export` of the read-only file → CSV with `exported_from_copy`; `scope_view_log.errors` lists new ERROR lines (e.g. after RUN→CONFIG→RUN with the Scope in Reply) |
| 0.8.12 | ESI index first build on the real knowledge folder (`manuals\Beckhoff_EtherCAT_XML`, ≈ 230 AX5000 dictionaries, and TwinCAT's `Config\Io\EtherCAT`) | starts by itself at server start; `kb_status` → esi complete, devices / files / `missing_refs` count 0 (all Dict files present); time and size of `index\esi.db`; `hw_lookup EL3356-0010` (PDOs, assignments, DC), `hw_object AX5206-0000-0216 #x…` (a drive parameter from the Dict file), `hw_diag #x4101 device=…`, `hw_module` of an AX8000 slot |
| 0.8.12 | Infosys format 5 / manuals format 5: approve `kb_rebuild(index='infosys')` and `kb_rebuild(index='manuals')` | `infosys_declaration MEMCPY` → Function, return UDINT; `LREAL_TO_FMTSTR` → STRING(510); `Attribute 'linkalways'` → Page; `docs_declaration T_MaxString` and `ADSREAD` answer directly (same as `infosys_declaration`); `docs_search "tcp client connect socket"` → FB_SocketConnect first; `docs_search 0x745` → the ADS return-code table first, `same_page_in` lists its copies |
| 0.8.12 | `kb_search` on the retest project after `kb_reindex()` (the search table is rebuilt from the stored units) | `search_table` in the answer once; "alarm text" → FB_AlarmList; "EXTENDS FB_ModuleBase" → FB_CounterModule first, comment-only matches marked `match_in: comments` |
| 0.8.12 | N107 / N110 / N106 live: `rec_compare` of two native recordings, `ipc_eventlog`, `proj_*` on the project with `retest_plcopen\` backups | rec_compare answers; the event log is read (boot folder resolved); the backups are in `not_in_project`, not units |
| 0.8.11 | layout move on the real knowledge folder (first server start after the update) | `index\` holds `knowledge.db`, `manuals.db`, `infosys-1033.db` (renamed from `knowledge.db`, `manualsdb\`, `infosys\helpviewer-1033\index.sqlite`), `index\README.md`; `kb_status` → `layout.obsolete_files` lists `helpviewer\infosys_en.db` (≈ 288 MB), nothing deleted; your `manuals\`, `sop\`, `tools\*OLD.py` untouched |
| 0.8.11 | Infosys format 4: the moved index reads `rebuild_needed`, answers as it is; `kb_rebuild(index='infosys')` approved | before approval no build starts (also across a restart); after: `tc_doctor` → rebuild running with pages done, the old index answers meanwhile, then `complete`, format 4; time and size of the build; on Windows the swap while the server reads (`replaced`, or `waiting_to_replace` → swapped at the next start) |
| 0.8.11 | N77 / N79 on the real index: `infosys_declaration FB_WG_ComboFilter`, a property with a linked type; `docs_search "how to a the"` | property `type` plain text + `type_ref`; the stop-word query refused like `kb_search` |
| 0.8.11 | N37 format 4 on the four manuals (`kb_rebuild(index='manuals')` approved) | EL3356 headings and toc titles once (no invisible `\| Signal flow diagram` copy); `manuals_db.py stats` → complete, text_format 4 |
| 0.8.11 | N76 `ads_dump(max_total_mb=…)` with a limit below the full size, and one above it by less than the footer | `written_mib` ≤ the limit; `limit_counts` = CSV + JSON on disk; skipped roots in the footer; a limit below the files' frame refused |
| 0.8.11 | reads by name after an online change / activate that moves variables (`ads_read`, `ads_watch`, `rec_start` poll) | the new address is used at once (`symbol_version` changes); no value of a neighbour variable |
| 0.8.11 | N75 / N5: `xae_deploy` and `xae_restart_twincat` from RUN | the after-witness is from the runtime that came back (values, not all null); `steps.restart.duration_s` lasts until RUN was seen again, `command_s` the shell part |
| 0.8.11 | R43 a recording across a download / online change (both engines; approval) | the recording ends at the last verified sample: footer `program_changed` (after_ms, read_by_ms, symbol_version), no row of the new program; a restart of the same program continues (`program_watch.notes`); `program_watch.reads` > 0, `failed_reads` 0 in a quiet run |
| 0.8.11 | N89 / N90 confirmations on the client's protocol (`allow_elicitation = true`) | `tc_doctor` → `elicitation.works` true, `route` `input_required` (2026-07-28) or `server_request`; `ads_plc_state stop` without a token → the prompt appears, audit `asked` then `human-confirmed-elicitation` |
| 0.8.11 | TE1000 routes: `xae_plc login/logout/start/stop/reset_cold` | `method` ITcPlcOnline, `before` / `after` state read back, `after` matches `ads_status`; if this XAE has no ITcPlcOnline, `method` names the ConsumeXml route |
| 0.8.11 | `xae.silent_mode = true` (opt-in) around `xae_activate_configuration` / `xae_restart_twincat` | no "Activate configuration?" / "Restart in run mode?" dialog; `TcAutomationSettings.SilentMode` back to its old value afterwards |
| 0.8.11 | PLCopen import option value from the type library | result `option_value` for `replace`; if it is 1 (REPLACE) the import replaces in place — the R15/N38 copy handling should then report no copy (send the result if it still renames) |
| 0.8.11 | N82 `xae_check_all_objects` twice, with a warning fixed in between | the second run does not list the fixed warning, no item counted twice; a failed check returns its findings with `passed` false, `method` ITcPlcIECProject2.CheckAllObjects |
| 0.8.11 | N85 / N86 / N87 a call that opens a modal dialog (`xae_command` that asks), and one started while a dialog is open | after ≈ 2 s `blocked_by_dialog` with title, text, push buttons; check boxes under `options`, never clicked; XAE calls refused until it is answered; the call's late result in `xae_dialogs` (`late_results`) |
| 0.8.11 | D5 / N50 on the real 4026 tables: `ads_read` of a struct with BOOL members; temp alias `TYPE PT_X : POINTER TO INT` in a GVL with `{attribute 'linkalways'}` | `true/false`; the alias read as an address `0x…` with the pointer note in `ads_read`, `ads_read_many`, `ads_dump` (NULL `0x0000000000000000`) |
| 0.8.11 | N92 a GVL nothing references (no `linkalways`) | `xae_build` → `link_warning` names it; after the download `ads_dump` → `not_on_target` with cause "not linked", `proj_vs_runtime` → `roots_not_on_target` |
| 0.8.11 | N88 / N47: 3 `ads_dump` in parallel right after a server start; `ads_dump(max_total_mb=2/4/8)` | all three: "the project's own programs and GVLs first"; files on disk ≥ 97 % of the limit (and ≤ it) |
| 0.8.10 | N38 `plcopen_import(option='replace')` on the real shell (A7): export `95_UnitTests.PRG_TcUnitRunner`, import `replace` | `replaced` + `header_renamed` (`PRG_TcUnitRunner_1` → `PRG_TcUnitRunner`), `checked_against: xml`; disk file `PROGRAM PRG_TcUnitRunner`; no `_1` in tree / `.plcproj`; `xae_build` 0 errors. If refused: the error names the differing line — that is TwinCAT's transformation, report it |
| 0.8.10 | R40 restart proof (B6): `xae_deploy` from CONFIG ×3, from RUN ×3, `xae_restart_twincat` from RUN | `deployed` / `restarted` true every time; `steps.verify.transition_by` mode_change (from CONFIG) or witness/poll (from RUN); `ads_status` RUN |
| 0.8.10 | N41 / N42 `xae_plc` with two PLC projects (or one) | without `plc_project_path` and two projects → refused; token via `tc_request_confirmation(action='xae_plc', args={...})` → `login:<project>@<NetId>`; a token for B never acts on A |
| 0.8.10 | R24 / Infosys re-index (INDEXER_VERSION 3): the first lookup after the update re-reads the package in the background | `tc_doctor` → infosys rebuilding, then complete; `infosys_declaration F_GetTaskTime` → `FUNCTION F_GetTaskTime : ULINT`, no `VAR_OUTPUT`, no non-breaking spaces |
| 0.8.10 | N37 / N66 positional running headers (TEXT_FORMAT 3, re-read once) on the four manuals | EL3356 p46 starts with the heading (no `\| Signal flow diagram`), toc titles without `\|`, p56 without the footer, p80 keeps `Index (hex) Name Meaning …`; CX9020 p23–26 keep `Document name` |
| 0.8.10 | N64 / N68 stubs: only files the plugin shipped are replaced; your own scripts are left alone | `kb_status` → `docs.builders.*.copy_kind`; your `*OLD.py` untouched; pointer written before the stubs |
| 0.8.10 | N56 stop on request: every block ends at one PLC time | `rec_stop` (cycle, max_delay_ms 100) → footer `stop_on_request`, `rec_verify` PASS with allowance 1, `block_ends` spread ≤ 1 cycle; independent `rec_verify_stream.py` LOSSLESS |
| 0.8.10 | N39 / N47–N55 `ads_dump` on the TcUnit project | `GVL_TcUnit.TcUnitRunner` read in pieces (no 1802), or named in `failed_roots`, the CSV and the JSON footer; `AdsMessageQueue.ArrayBuffer` → `not expanded` in both files; NULL pointers `0x0000000000000000`; sizes in MiB |
| 0.8.10 | N46 launcher: a plugin update while another Claude window runs the server | the new venv is built beside the live one and swapped on a later start; the running server keeps working; an older pending build never replaces a newer install |
| 0.8.10 | Python recorder `stop_when` / `max_events` with two or more blocks (engine='python', max_delay_ms 100) | footer `stop_at` (by stop_when), `window_end_ms` = the firing sample's time, `label` COMPLETE, `rec_verify` PASS |
| 0.8.10 | A recording where one block never delivers (a symbol on a task that is stopped) | `label` INCOMPLETE, block `period_note` "never delivered", `rec_verify` FAIL with `unprovable_blocks` |
| 0.8.10 | N70–N74 (your SOP §3): `kb_search` with `ADS error 1861`, `ADSError 1861`, `0x745`, the C0046 / C0032 texts, `a the`, `PRG_TcUnitRunner_9`; `hw_lookup EL3356-0010` (detail newest / summary / full); `infosys_declaration FB_WG_ComboFilter` | known_error for all five; C0032 → no code hits, note; `a the` refused; copy name → base-name hits + note; hw_lookup default = newest revision's PDOs only; `methods` (Configure, Call, Reset) and `properties` fields, no method list in `description` (after the one Infosys re-index) |
| 0.8.10 | PLCopen `replace` of a CFC / FBD POU, and of an FB with a property (real export) | graphical: refused, copy kept, original untouched; property: `checked_against: xml` and a changed Get body is caught. If the property is refused for a structure reason, send the export |
| 0.8.9 | Python 3.12 only: the existing plugin venv (built by 0.8.8) on the engineering PC | after `/plugin` update, the first start rebuilds the venv if it was not 3.12 (background install, reconnect); `tc_doctor` → `python` 3.12.10 |
| 0.8.9 | R42 `ads_dump` on the TcUnit project (`GVL_TcUnit.TestSuiteAddresses`, 1,000 × POINTER TO FB_TestSuite) | `ads_dump()` defaults on the grown TcUnit project (0.8.12 retest) stop at the 64 MiB limit: `complete false`, `written_mib` ≤ 64, skipped roots named, no `.partial`; with a scope that fits (`pattern='GVL_TcUnit.*'`) → `TestSuiteAddresses` 1,000 rows of `0x…` addresses; a forced timeout → no final files, message names the phase. (The SOP's old ≈5 s / 4 MB expectation predates the larger TcUnit project.) |
| 0.8.9 | R15 `plcopen_import(option='replace')` of a POU living in a folder | `added` names the copy, `replaced` names the original, `copies_removed` the copy; the original has the new text after save + build |
| 0.8.9 | R41 level trigger with several notification blocks (simulator only) | `rec_start … start_when='MAIN.nCycle >= 0'` with symbols from two tasks/blocks → every block's first sample on the trigger cycle |
| 0.8.9 | R32 wrapped type names on the real `.mshc` pages (synthetic HTML only) | `infosys_get_page ADSREAD` → `T_AmsNetId` in one piece; if not, one raw page HTML (Waiting on Ryan) |
| 0.8.9 | N37 `manuals_db.py update` on the four manuals (first run reads all again) | EL3356 `toc` without `capacity` / `only)` / `parameterization` as level-1 chapters; `show EL3356 56` without the running header |
| 0.8.9 | builders v2 on the real package (≈ 51,656 pages): first `index` upgrades the version-1 `infosys_en.db` (link table), swap while the MCP server is running | `<Python 3.12> builders\infosys_local.py index --kb D:\TwinCAT-MCP\knowledge` → "older index format", then `stats` → complete with `links_resolved`; reconnect → instructions line says complete |

## Waiting on Ryan

- `D:\TwinCAT-MCP\knowledge\sop\scope\ScopeAI.cs` — the real property names, before `scope_add_channel` /
  `scope_set_axis` / `scope_set_sample_time` can be built (W, N16, #44).
- One raw page HTML from the `.mshc` if the 0.8.3 parse checks above fail.
- Retest of 0.8.16 (the 0.8.16 rows above). Not in 0.8.16 (held by Ryan, 2026-10-21: offline documentation later):
  the error index and `err_lookup` (N125), the 0.8.15 retest's documentation items N177, N183, N184, typo-near
  symbols, manual kinds in `hw_lookup` — built and reviewed eleven rounds on branch `feat/0.8.16-errors-index`
  (Infosys format 8 there); the docs direct-access spec.
  The fourteenth review's LOW findings are fixed in 0.8.17 (one owner per recording's Scope part).
- 0.8.17 adversarial review (Ryan, 2026-10-21: review before the release; at most 2 rounds, the 0.8.17 changes only,
  HIGH / MEDIUM block, LOW to this list). Round 1: 4 HIGH + 1 MEDIUM fixed. LOW, left: an automatic finish takes the
  one-time `reattached` note into the Scope result (rec_status does not show it, the next XAE tool no longer gets it);
  "XAE refused StartRecord" is also said for the worker busy / XAE held by another call's dialog; `stop_refused` texts
  name the job `scope_action`; the `stop_unknown` note advises `scope_record(save)` also for a deleted recording.
  Older, outside the 0.8.17 change: at server shutdown (`atexit` stop_all, no event loop) no Scope part is finished;
  StopRecord looks the Scope up by name — with `scope` unset and a second Scope configuration opened meanwhile the
  lookup fails before StopRecord runs and the part is final; in `_scope_reset` an ExportCSV refused as busy is said
  "lost"; a generic StopRecord failure still sets `scope_stopped`; a SaveSVD that timed out can be sealed while XAE
  may still write it; two `also_scope` recordings on one Scope configuration — the first one's StopRecord stops the
  second. Decision for Ryan: should `rec_delete` also delete the recording's Scope files (.svdx, .scope.csv,
  .xae.csv, .scope.json)? It never has; it now lists them (`scope_files_kept`). Decision for Ryan (N186): after a
  Scope part is saved, Scope stays in Reply and the next also_scope recording is refused until Scope is
  disconnected in XAE — should the plugin call IMeasurementScope.Disconnect() itself once the .svdx is saved
  (Scope's view in XAE is then cleared; the data is in the .svdx)?
- Retest of 0.8.15 (the 0.8.15 rows above; approve the Infosys 7, ESI 2 and manuals 6 rebuilds; `kb_verify` 1.3).
  Not in 0.8.15: the error index (N125, 0.8.16); title ranking by word rarity (the 'OPC UA server security'
  trade-off); the index proposals of 2026-10-21 (a golden query set, the real-data diff as a repo tool, a library
  version check against `proj_libraries`, re-deriving declarations instead of a rebuild, declarations checked
  against the libraries through XAE, one provenance field, a test that the standard-library-only modules import
  nothing else); N173's 1 s UI Automation budget needs a live check.
- Retest of 0.8.14 (the 0.8.14 rows above; approve the Infosys rebuild to format 6). Not in 0.8.14: the error index
  (`twincat-mcp-error-index-spec.md`, N125) — AL status codes as page links when it is built; the ESI count of
  EL6653 (19 vs 17: duplicate #x1C32/#x1C33 in Beckhoff's EL66xx.xml) needs a decision (an ESI format change) —
  decided in 0.8.15: one object per index (ESI format 2).
- Retest of 0.8.12 (the 0.8.12 rows above; approve the Infosys and manuals rebuilds to format 5).
- Retest of 0.8.11 (the 0.8.11 rows above; the format-4 rebuilds are superseded by format 5; delete
  `helpviewer\infosys_en.db` when you like).
- Retest of 0.8.10 (N38, R40, N41, N42/N40/N69, R24, N37/N66, N64/N68, N65, N67, N46, N39/N47–N55, N56–N63, and the review fixes: recorder blocks with < 2 samples, Python stop_when end, dump names, restart proof without a counter reset = "cannot tell"; the hardware rows above).

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
| hardware manuals | plugin reads `index\manuals.db` (built by `manuals_db.py`); PyMuPDF (AGPL-3.0) stays out of the public plugin |
| knowledge base rules (2026-10-18) | `docs/KNOWLEDGE_BASE.md`: one `index\` folder, one index per source, normalised once at index time, format fingerprints in tests, first build automatic and every later rebuild only after approval (`kb_rebuild`), nothing deleted by the plugin — changes need Ryan's approval |
| Infosys source | local Help Viewer package, no network (0.8.3) |
| profiler | `prof_task` first (no code change, `_TaskInfo` via the lossless recorder); per-POU instrumentation later, method per target (CX9020 / WEC7 Arm has no `GETCPUACCOUNT`/`PROFILER` → `FB_CXProfiler`, wall-clock); budgets on max + `CycleTimeExceeded`, not p99; CIs with batch means (autocorrelated cycles); verify that `GETCPUACCOUNT` excludes pre-emption before relying on it; evaluate TE1210 |

## Open from the field reports

| ID | What |
|---|---|
| R12 / A6 | `rec_verify` over many counters (pattern/list) and the 300 × 1 ms acceptance run per recording method; capacity documented — not built (`rec_verify` takes one counter) |
| #45 | Check all objects did not run — 0.8.7 calls `ITcPlcIECProject2.CheckAllObjects` first (unverified, above) |
| SOP indexing | `sop\*.md` listed by `kb_status`, not searchable yet (decided: index them; wishlist above) |
| R32 (part) | Infosys "T_Ams NetId" table split — joined when the index is built since 0.8.11 (format 4; 0.8.9 joined it when a page was read), verified in the 0.8.9 retest; the cause in the raw HTML is still not seen |
| #49 / N9 | stale System Manager after solution reopen (partly fixed) |
| #79 | modal dialog raised by the command itself |
| launcher POSIX | on Linux / macOS a directory rename succeeds while a server runs from it, so a replaced venv (`.old-*`) can be removed under another window's server; on Windows the rename fails and the venv stays (N46). Windows is the target platform |
| R40 back-to-back | a restart soon after another (task 1 had run for less than the time the command took) is "cannot tell" by the counter when the counter did not go back; the mode poll and the compile timestamp still prove most cases |

### From the 0.8.3 deep test

All items fixed: up to 0.8.7, D5 in 0.8.11, D19 (ESI PDO templates) in 0.8.12.

## On hold

- TcVIS / HMI bindings (#48, #29) — until Ryan picks it up again.
