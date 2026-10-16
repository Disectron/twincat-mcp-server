---
name: twincat-mcp
description: Use when working on a Beckhoff TwinCAT 3 project with the `twincat` MCP server connected — debugging live PLC variables, recording real-time data, validating function blocks, building/deploying from XAE, reading or editing .TcPOU files, running TcUnit. Chooses the right tool chain per task.
---

# twincat-mcp tool chains

Always: `tc_doctor` once per session. Prefer reading over assuming. Writes are gated; never
bypass by raw address. State the recording / snapshot ids you create.

## Debug a value
`wf_debug_symbol <sym>` → if dynamic: `rec_start symbols=[sym, inputs…] mode=change cycle_ms=1`
→ reproduce → `rec_stop` → `rec_latency` / `rec_stats` → `proj_get_code` at the writer → fix with
`xae_pou_set` (or `proj_set_code`) → `xae_build` → `xae_deploy` (confirm) → `ads_wait_until`.

## Validate / test an FB
`proj_interface` → `test_scenario {record: {prefix: instance, mode: change, cycle_ms: 1}, steps: [...]}`
→ `rec_pulses` / `rec_latency` on the recording → `ads_task_stats` for cost → report pass/fail
with numbers → `tcunit_scaffold` + `tcunit_run` to make it permanent.

## Batch-after-batch capture
`rec_scan prefix=<chain> checklist=<name>` → user marks the CSV (or `{attribute 'tcmcp.record'}` in
source) → `rec_profile_save <name> from_checklist=<name> stop_when=...` → per batch:
`rec_profile_start <name>` → run → `rec_profile_stop <name>` → `rec_stats` / `rec_compare` against the
previous batch's ids. Check `ads_task_stats` once with the profile live; thin it if exceed counts move.

## Capture everything while the machine runs
`rec_start prefix=MAIN mode=change cycle_ms=<task cycle>` (or `pattern`) → work → `rec_stop` →
`rec_export csv_wide` for spreadsheets; `ads_dump` for a one-shot full snapshot; `snap_save` /
`snap_compare` for parameter regression.

## Deploy
`proj_changed` → `proj_lint` on them → `xae_build` → `wf_release_check` → `snap_save pre` →
`xae_deploy` → `ads_status` → `proj_vs_runtime` → `snap_compare pre live`.

## Performance
`ads_task_stats samples=200` → `wf_fb_profile` per heavy instance → `rec_start mode=cycle` →
`rec_stats` (symbols changing every cycle) → `proj_call_graph` to the hot path.

## Project orientation
`wf_project_overview` → `proj_call_graph` per program → `ads_leaves <program> with_values=true`.

## HMI
`hmi_scan` → `hmi_vs_runtime` (broken bindings) → `hmi_bindings <symbol>` before renaming a PLC variable.

## When XAE stalls
`xae_dialogs` → read title/buttons → `xae_dialog_resolve button=...` (machine-affecting prompts need confirmation).

## Library FB / type you have not used
`infosys_declaration <FB|ST_|E_>` (inputs/outputs with descriptions, struct members, enum values) →
`infosys_search_code "<call pattern>"` for a working example → `infosys_get_page <symbol>` only when
the prose matters. Unknown library: `infosys_books pattern=tcplclib_*` → `infosys_index <book>`.
Never invent a library FB's pins; if `infosys_declaration` has no page, say so.

## MES / C# side
`ads_export types=[ST_Recipe,...] fmt=csharp` and `ads_export prefix=MAIN fmt=csharp_symbols` after every datatype change.

## Prompts (slash)
`/twincat:debug_symbol`, `/twincat:fb_validation`, `/twincat:deploy_safely`,
`/twincat:performance_audit`, `/twincat:onboard_project`.
