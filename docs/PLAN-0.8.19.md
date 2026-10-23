# 0.8.19 plan: PLC libraries through XAE, IPC diagnostics, the 0.8.18 retest fixes

Ryan, 2026-10-22: "we will add Libraries and GetLastErrorMessage in build 0.8.19 after 0.8.18 is finished"; later
the same day IPC diagnostics (§8) beside the libraries, and the fixes of the 0.8.18 retest first (N194 – N196, N180:
branch `fix/0.8.19-retest`, PR #14). The release rules stay the same:
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

**Taken (2026-10-23, stated to Ryan, not objected): c and a.** c: the library actions' messages come with their
results (tested). a: a retest row (§5). b stays held, no slot.

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
| `xae_library_remove` | C | RemoveReference(name, version, distributor) | Warns that code using the library fails at the next build. A name referenced twice (VisuSymbols: placeholder and library) is refused until `kind` / `version` says which; the result says when XAE removed another one. |
| `xae_placeholder_add` | C | AddPlaceholder | placeholder, library, version, distributor |
| `xae_placeholder_resolve` | C | SetEffectiveResolution | The effective resolution after it is checked against what was asked. |
| `xae_placeholder_freeze` | C | FreezePlaceholder | one placeholder, or all |

**Corrected during the build (TE1000 pages read 2026-10-23):** there is no "frozen" lock. FreezePlaceholder "freezes
the EffectiveResolution" — the placeholder keeps the version it resolves to now instead of following `*`; no member
of ITcPlcPlaceholderRef says it is frozen, and SetEffectiveResolution may set another version afterwards. The plan's
"refuse a frozen placeholder" was a wrong reading and is dropped; the result of a freeze says what it does. AddLibrary
"forces a project reload": the references are read again after a pause when the first listing after an action lacks
one (N195). Token targets: `'<iec project path>:<library>'`, `':<placeholder>'`, resolve `':<placeholder>=<library>'`,
freeze `':*'` for all. Classes: remove is destructive-class (asked in guarded, and in full with
`confirm_destructive`); the other changes are asked in guarded mode, run in full.

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
- **Frozen placeholders:** no such state in the Automation Interface (see §2, corrected).
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
| placeholder add → resolve → freeze → `xae_build(save_first=true)` (on TwinCAT Project15), then the .plcproj | the effective version after each step as asked; after the save the .plcproj has a `PlaceholderResolution` for it; `xae_library_list` → `saved_plcproj.same` true |
| `xae_library_remove(library='VisuSymbols')` on a copy of DrumFillingMachine | refused "referenced 2 times … kind='placeholder' or kind='library'"; with `kind='library'` only the library goes |
| `xae_library_repositories` | the repositories, or "not readable through this XAE's Automation Interface" (the probe's E_NOINTERFACE) — then drop the tool |
| Options open in XAE, then `xae_library_add` | "Nothing of this call ran" |
| §0 a: right after a failed `xae_activate_configuration`, the probe (`D:\TwinCAT-MCP\probe`) | `messages.verdict` says whether a read clears the buffer |

## 6. Order of work after 0.8.18

1. §0 taken (c, a). The branch `feat/0.8.19-libraries` is cut from main (2026-10-23).
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
## 8. IPC diagnostics (Beckhoff Device Manager / MDP) — added 2026-10-22

Ryan, 2026-10-22: "cpu, temp, disk and version etc."; tools `ipc_info` + `ipc_mdp_read`, in 0.8.19 beside the
libraries, on their own branch with their own review. Either part can ship alone as 0.8.19 if the other blocks.

**Information model (Beckhoff Device Manager, Infosys `devicemanager`):**
- A table index is 16 bits `0xAnnT`: area `A` (4 bits), module ID `nn` (8 bits, made at start-up), table `T` (4 bits).
- Table `0xF020` (device area) lists the modules: each row holds the module type in the high word and the module ID
  in the low word; the list is sorted by module ID.
- Every module has a header table `0x8nn0`: 1 Address (UDINT), 2 Type (STRING), 3 Name (STRING), 4 Dev type (UDINT).
- CPU (type `0x000B`), table `0x8nn1`: 1 CPU frequency (UDINT), 2 CPU usage (UINT, %), 3 CPU temperature (INT, °C;
  needs the BIOS API — not every device has it). One CPU module per core on some devices.
- The other module types (`E_IPCDiag_ModuleType`): NIC 2, Time 3, TwinCAT 8, Software 0xA, Memory 0xC, PLC 0x12,
  Silicon drive 0x17, OS 0x18, Fan 0x1B, Mainboard 0x1C, Disk management 0x1D, UPS 0x1E, Physical drive 0x1F, Mass
  storage 0x20, Misc 0x100. Their tables are read from the Device Manager pages during the build, one module type at
  a time; a field whose layout is not documented there is not decoded (raw bytes, said).

**ADS access — NOT confirmed:** the AMS port (10000, the system service) and the index group (`0xF302`, with the
index offset `index << 16 | flags << 8 | subindex`, as Tc3_IPCDiag's `nIndex` / `nFlags` / `nSubIndex` suggest) come
from memory and the library's inputs, not from a page read in this session (the Device Manager pages found say
nothing about ADS). The tools are built with the address in one constant; they are not released before this check
passes on hardware:
`ads_read_raw(index_group=0xF302, index_offset=0xF0200000, size=2, port=10000)` on the CX9020 → the number of
modules (UINT), then `index_offset=0xF0200001, size=4` → the first module (type in the high word). An ADS error, or
values that are not a module list, keep the IPC part out of 0.8.19 until the right address is known.

**Tools (+2):**

| Tool | Class | What |
|---|---|---|
| `ipc_info` | R | The module list (0xF020), then per module its header and the tables this plan decodes: CPU (frequency, usage, temperature), memory, TwinCAT version and state, OS version, mainboard (serial, temperature, boot count, operating time), fans, disks (drive, size, free), time. Each value with its MDP address; a module that answers with an ADS error is listed with the error, never left out. |
| `ipc_mdp_read` | R | One MDP address (index, subindex, type) — raw bytes plus the decoded value; for what `ipc_info` does not decode. |

- Both read only: no MDP write (IP addresses, device name) — that would be a separate decision.
- The target is the configured ADS target (`ams_net_id`); `ams_net_id=` reaches another device with a route.
- Timeouts: one ADS read per value, the call bounded (a CX9020 answers each in a few ms; 60 modules × 5 tables is the
  upper bound).
- Tests: a fake ADS server answering 0xF302 with a synthetic module list (2 CPUs, memory, TwinCAT, one fan, one disk)
  — synthetic values only (public repo). An ADS error per module; an unknown module type (raw); a STRING row.

**Retest rows:**

| What | Check |
|---|---|
| the address check above on the CX9020 | a module count (UINT) — or the ADS error that stops the IPC part |
| `ipc_info` on the CX9020 | CPU usage / temperature agree with the Device Manager web page (`https://<ip>/config`), TwinCAT version = `ads_status`, disk free agrees with the Device Manager |
| `ipc_info` on the engineering PC (`ams_net_id` local) | modules listed; a value the PC does not offer (CPU temperature without BIOS API) → its ADS error, not 0 |
| `ipc_mdp_read` of the CPU table `0x8nn1`:3 (nn from `ipc_info`) | the same temperature as `ipc_info` |
