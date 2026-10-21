# 0.8.19 plan: PLC libraries through XAE

Ryan, 2026-10-22: "we will add Libraries and GetLastErrorMessage in build 0.8.19 after 0.8.18 is finished".
Work starts only after 0.8.18 is released (and retested). The release rules stay the same:
- one addition per release;
- review before the release: at most 2 rounds, HIGH / MEDIUM block, LOW go to docs/TRACKER.md;
- no release without Ryan's go.

## 0. Open before the build: what "GetLastErrorMessage" means for 0.8.19

`ITcSysManager2::GetLastErrorMessages` was already released in 0.8.16: `xae_messages` on activate, restart, build,
ConsumeXml, CreateChild / DeleteChild, PLCopen import, boot project and online commands. In 0.8.17 it gained
`xae_messages_note` (N188), and 0.8.18 marks messages whose before-read failed. Three things could still be meant:

| | What | Effect on 0.8.19 |
|---|---|---|
| a | Settle on Ryan's XAE whether a read clears the buffer. The probe of 2026-10-22 found it empty, so this was not decided. | A probe run right after a failed action. Code changes only if the buffer does not clear (both cases are already handled). |
| b | The error index (`err_lookup`, N125): look up the codes `xae_messages` names. It is held on `feat/0.8.16-errors-index`, which is built on v0.8.15 and is 22 commits behind main. | A second addition: it breaks the one-addition rule unless Ryan decides otherwise. It needs a rebase (server.py conflicts) and its own review. |
| c | Messages of the new library actions. | Part of this plan anyway (§2: every change runs as an action, so its messages come with it). |

**Ryan to answer: a, b or c (several are possible).** Until then this plan covers c, and a as a retest row.

## 1. Scope (decided)

- **1a (read):** the references (library or placeholder, requested and effective version, distributor), what is
  installed, and the repositories.
- **1b (change):** add / remove a reference; add a placeholder, set its resolution, freeze it.
- **Not included:**
  - 1c: InstallLibrary, UninstallLibrary, Insert / Move / RemoveRepository.
  - **1d: dropped (2026-10-22).** The probe found GetLibraryIecDeclaration offered by no object. The signatures fallback
    gives only names and input / output types: no methods, struct members or constant values.

**Facts from the TE1000 probe (2026-10-22, DrumFillingMachineV0, Ryan's XAE):**
- The library manager is `_ITcPlcLibraryManager`, reached from `TIPC^<Plc>^<Plc> Project^References`.
  - These members are present: References, Repositories, AddLibrary, RemoveReference, AddPlaceholder, ScanLibraries,
    SetEffectiveResolution, FreezePlaceholder, ProduceLibrarySignatures.
- **References:** 18 entries.
  - Placeholders are `_ITcPlcPlaceholderRef`: Name `#X`, PlaceholderName, DefaultResolution, EffectiveResolution, for
    example `Tc2_Standard, * (Beckhoff Automation GmbH)` resolving to `3.4.7.0`.
  - Libraries are `_ITcPlcLibrary`: Name, Version, Distributor, DisplayName.
  - `VisuSymbols` is referenced twice: once as a placeholder and once as a library.
- **ScanLibraries:** 277 installed libraries, including several versions of one library.
- **Repositories:** the items of the collection gave E_NOINTERFACE. TE1000 types the property as
  `ITcPlcLibRepositories`; reading it through `Count` / `Item(i)` has not been tried.
- **AddLibrary signature (TE1000):** `AddLibrary(bstrLibName, bstrVersion = "", bstrCompany = "")`.

## 2. Tools: Option B, one tool per job; reads never change anything (+8)

| Tool | Class | Automation Interface | Notes |
|---|---|---|---|
| `xae_library_list` | R | References | Each reference: kind, name, requested and effective version, distributor. Flags: `*` and what it resolved to; a pinned version that is not installed (checked against ScanLibraries); one library referenced twice; differences from the offline `proj_libraries` (.plcproj). |
| `xae_library_installed` | R | ScanLibraries | Name, version, distributor; an optional name filter. |
| `xae_library_repositories` | R | Repositories | Read through Count / Item. If that is not reachable on 4024, the tool says "not available in this XAE"; it never returns invented data. If no route works on the hardware, drop the tool. |
| `xae_library_add` | C | AddLibrary | name, version (`''` = newest), distributor |
| `xae_library_remove` | C | RemoveReference | Warns that code using the library fails at the next build. |
| `xae_placeholder_add` | C | AddPlaceholder | placeholder, library, version, distributor |
| `xae_placeholder_resolve` | C | SetEffectiveResolution | Refuses a frozen placeholder: it is never unfrozen silently. |
| `xae_placeholder_freeze` | C | FreezePlaceholder | |

**Every change (C):**
- **Safety mode:** readonly refuses, guarded asks, full runs. In guarded mode the confirmation is bound to the action,
  the library and the PLC project, so a token for `add Tc2_Utilities` does not work for `remove`.
- **Runs once:** it is an action that must not run twice (`_retry(once=True)`). XAE's messages come with the result
  (§0 c).
- **Refusals:** a call refused by a busy XAE or a dialog says "Nothing of this call ran". "Repeat it" is said only when
  nothing ran.
- **Result:** the reference list before and after, as a diff.
- **No automatic build:** it never builds and never activates. A build is the user's next step (`xae_build`).
- **Path forms:** `plc_project_path` takes the plc_path, the iec_project_path or the instance_path (N193). It is
  required when the solution has more than one PLC project.

## 3. Weak points to settle during the build

- **Version strings:** `*` (newest), a pinned version and a placeholder's resolution resolve differently. Tests cover
  all three.
- **Frozen placeholders:** `set_resolution` must refuse a frozen placeholder, not change its resolution.
- **AddLibrary by name or by display name:** which form XAE takes is checked on the hardware.
- **Removing a library the code uses:** XAE allows it and the next build fails. The result says so; nothing is undone
  automatically.
- **Repositories:** see §1. Decided by one hardware check before the tool is promised.

## 4. Tests

- **A fake `ITcPlcLibraryManager`** shaped like the probe's objects, with synthetic names only (the repo is public):
  - References with placeholder and library items;
  - ScanLibraries with several versions of one library;
  - Repositories through Count / Item.
- **Every tool:**
  - readonly refusal;
  - a guarded token bound to the action, library and project (a token for another action or library is refused);
  - the before / after diff;
  - XAE messages;
  - a busy shell or a dialog refuses the call before it runs;
  - an interface that is missing: "not available in this XAE".
- **Red proof:** each test fails on main before the change.

## 5. Retest rows for Ryan's PC (go into docs/TRACKER.md with the release)

| What | Check |
|---|---|
| `xae_library_list` on DrumFillingMachine | 18 references, VisuSymbols twice; the effective versions match XAE's Library Manager |
| `xae_library_add` of a library the project does not reference, `xae_build`, `xae_library_remove`, `xae_build` | the references are as before; both builds succeed; the XAE messages are shown |
| placeholder add → resolve → freeze → resolve again (on TwinCAT Project15) | the second resolve is refused: "frozen" |
| Options open in XAE, then `xae_library_add` | "Nothing of this call ran" |
| §0 a: right after a failed `xae_activate_configuration`, the probe (`D:\TwinCAT-MCP\probe`) | `messages.verdict` says whether a read clears the buffer |

## 6. Order of work after 0.8.18

1. Ryan answers §0. The branch `feat/0.8.19-libraries` is cut from main.
2. Reads first (`xae_library_list`, `_installed`, `_repositories`), with tests; then the changes, with tests.
3. README entry for 0.8.19, TOOLS.md regenerated, TRACKER rows (§5).
4. Review (2 rounds max), PR, CI green on both OS, Ryan's go, release, Ryan's retest.

## 7. After 0.8.19 (the order Ryan decided on 2026-10-22)

| Version | Contents |
|---|---|
| 0.8.20 | Linking. Its read side came with the N192 fix in 0.8.18. Still to do: `xae_link` / `xae_unlink` (LinkVariables / UnlinkVariables) and `xae_links_import` (ConsumeMappingInfo, with an automatic backup and a diff before the confirmation). New: show the links XAE kept but could not restore. The `.tsproj` marks them `RestoreInfo="ANotFound"`; DrumFillingMachineV0 has 5 on Term 6, pointing to variables that moved to `GVL_WeighingScale`. ProduceMappingInfo does not list them. |
| 0.8.21 | EtherCAT |
| 0.8.22 | Snapshots |
| 0.8.23 | Tasks |

The error index has no slot yet (§0 b).
