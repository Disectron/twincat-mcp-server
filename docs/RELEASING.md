# Releasing twincat-mcp

The plugin installs from `main`, and Claude Code caches it by version. A version on `main` must therefore never
exist without its matching `tcmcp-capture.exe` (field report N25). The release workflow
(`.github/workflows/release.yml`) guarantees that.

## Flow

1. Work lands on `main` with the version of the **last release** (no bump on `main`). The exe in
   `twincat_mcp/bin/` keeps matching that version, so the native engine keeps working for anyone on `main`.
2. Cut the release from `main`:
   ```
   git checkout -b release/vX.Y.Z
   # bump: twincat_mcp/__init__.py, pyproject.toml, .claude-plugin/plugin.json, .claude-plugin/marketplace.json,
   #       docs/INSTALL.md (plugin cache paths), python scripts/gen_tools_md.py
   # README.md changelog: a **X.Y.Z** entry (the workflow refuses to run without it)
   git push -u origin release/vX.Y.Z
   ```
3. The workflow, on that branch:
   - checks that branch, package, plugin and marketplace versions agree and that the changelog has the entry;
   - builds `tcmcp-capture.exe` on Windows (Release, `/Brepro`), runs `--version` and `--selftest`, then the whole
     test suite against it;
   - commits the exe and `tcmcp-capture.exe.sha256` onto the branch, tags `vX.Y.Z`, fast-forwards `main`
     (refused if `main` moved meanwhile — rebase the release branch);
   - publishes the GitHub release: `twincat-mcp-X.Y.Z-windows-x64.zip`, `twincat-mcp-X.Y.Z.mcpb`,
     `tcmcp-capture.exe`, `SHA256SUMS`, build provenance (`gh attestation verify`).
4. Afterwards, locally: `git checkout main && git pull --ff-only`, then re-run CMake configure + build
   (`cmake -S native/capture -B build/capture && cmake --build build/capture`) so the local exe has the new version.

## Kept, not cleaned up

`release/v*` branches and `v*` tags stay (decision 2026-10-18). Each release branch ends at the same commit as the
tag; `main` contains all of them.

## Released

| Version | Content | Date |
|---|---|---|
| 0.8.1 | recording authenticity (R1–R12: gap markers, label, hash checkpoints, sealed + read-only files, export provenance, datatype GUIDs), 0.8.0 retest fixes, exe shipped in the plugin | 2026-10-18 |
| 0.8.2 | TwinCAT Scope: `scope_export`, `scope_list`, `scope_record`, `rec_start(also_scope)`, `[recorder] also_scope` | 2026-10-18 |
| 0.8.3 | Infosys from the local Help Viewer package (`docs.source = "local"`, no network) | 2026-10-18 |
| 0.8.4 | 0.8.3 deep test: stalled PLC + re-subscription, honest live status, sealing of self-ended recordings, one pattern language rec_start/rec_scan, verified restart, Infosys index speed + reindex rule, ESI parsing | 2026-10-18 |
| 0.8.5 | verified save before build/deploy/tcunit_run (D44); experiment measures honest (D33–D37); tc_doctor CONFIG as info (D45) | 2026-10-18 |
| 0.8.6 | rest of the 0.8.3 deep test: 4026 compiler lines, Error List, active PLC project, TcUnit checks, recorder/profile fixes, Infosys progress | 2026-10-18 |
| 0.8.7 | 0.8.6 retest + robustness (R1–R32): output folders + overwrite, confirmation names, workers that never cancel a running job, argument/condition validation, Automation Interface PLC online commands, Infosys lookup tables, aligned profile groups | 2026-10-18 |
| 0.8.8 | `twincat-docs` skill (offline Infosys + PDF manuals from the knowledge folder, read-only), `kb_*` tools aligned with the knowledge folder, R33–R39 | 2026-10-18 |
| 0.8.9 | Python 3.12 only; 0.8.8 retest fixes (R42 dump pointers/budgets/streaming, #10, R15 verified replace, R41/R7, R23–R32, N31–N37) and the knowledge-folder builders in the plugin (Python 3.12 gate, atomic rebuild, real index state at every connection, links; manuals_db 3) | 2026-10-18 |
| 0.8.10 | 0.8.9 retest fixes at their cause (N38 replace checked against the imported XML and header renamed back, R40 restart witness, N41 one PLC project per call, N40/N42/N69 server-computed token targets, R24 NBSP at the source + re-index, N37/N66 positional running headers, N64/N68 stubs replace only shipped files, N65/N67 docs search tiers, N46 launcher, N39/N47–N55 dump in pieces, N56–N63 recorder end at a PLC time) and an independent review of the fixes | 2026-10-18 |
| 0.8.17 | one owner per recording's Scope part (N182 at its root: the recording's end, rec_stop, rec_delete and a stop asked again taken in order by one task), the 0.8.16 retest fixed at its cause (N186 a Scope start refused in Reply and an earlier capture never linked, N185 the Scope View log header, N187 the Python engine's last program check, N188, N181b), Scope files in 'Scope Recordings', close / new / pause after a recording; two adversarial review rounds of the Scope work | 2026-10-22 |
| 0.8.18 | fixes only: one owner of Scope at a time (N189), Scope's span against its recording in the result and the signed link (N191), the Scope questions audited (N190), xae_links from the instance XML and the mapping table (N192, N193), the in-depth review of the 0.8.16 – 0.8.17 Scope and recorder code (2 rounds) | 2026-10-22 |
| 0.8.16 | XAE messages with each action's result (`ITcSysManager2::GetLastErrorMessages`: TwinCAT's own text, also on success, with the earlier steps' on an error) and the 0.8.15 retests outside the documentation fixed at their cause (N141b, N175, N176, N178–N182, the Python recorder footer: Scope finished when a recording ends by itself, after a TwinCAT restart, at rec_delete; busy and blocked calls said as what ran), three independent reviews; the offline documentation work held (N125, N177, N183, N184) | 2026-10-21 |
| 0.8.15 | the 0.8.14 retests fixed at their cause (N140–N175, N44: Infosys format 7 — kinds from the page's own evidence, symbols documented inside pages, inherited members, Syntax headers under another name, paging and MCP/skill parity; ESI format 2 — one object per index, sub-index notation, hw_search paging; manuals format 6 — product codes of every family; XAE modal windows and busy calls; kb_search), kb_verify I13, three independent reviews | 2026-10-21 |
| 0.8.14 | the 0.8.13 retests fixed at their cause (N127 S1 kb_* changes gated by the mode and audited; N128–N136 knowledge base: folder refusals, error messages and known errors, declaring unit first, parse errors / UTF-16 / stale folders, query handling, location messages; N116–N124 XAE: live COM objects on retry, actions never sent twice, binding flags on every result; Scope N117/N119 and export safety; multi-PLC port binding and pins; Infosys format 6: TOC parents N126, page charset N138, N99b, N121/N122), kb_verify's search fixes, two independent reviews and a review of their fixes | 2026-10-20 |
| 0.8.13 | the 0.8.12 retests fixed at their cause (N116 XAE bound to one solution; N113/N114 PLC projects from the .tsproj, units per file, every reader and analysis inside one PLC project; N103b, N105; N117/N118 Scope CSV from the .svdx, read-only sources, TcScopeView.log; polish), two independent reviews and their fixes | 2026-10-20 |
| 0.8.12 | the ESI index (`index\esi.db`: every ESI file of the PC, hw_search / hw_object / hw_diag / hw_module), the 0.8.11 retests fixed at their cause (N37, N50, N93–N111, D19: Infosys format 5 kinds and return types, one name rule and one search for every reader, identifier words, a code's table first, the project is its .plcproj, rec_compare native, ipc_eventlog), two independent reviews of the work and of their fixes, readers that close their connections (Windows) | 2026-10-19 |
| 0.8.11 | the 0.8.10 retests fixed at their cause: ADS access by symbol handle with the symbol version in the same request, recordings of one program (R43), confirmations on MCP 2026-07-28 (N89/N90, SDK 2.3), XAE per the TE1000 Automation Interface (ITcPlcOnline, SilentMode opt-in, Check all objects, PLCopen options), dialogs that hold a call (N85–N87), D5/N50 types from the datatype table, the project index published whole (N88), roots not on the target (N92), exact dump budget (N76/N47), one Infosys index in `index\` with rebuilds on approval | 2026-10-19 |
