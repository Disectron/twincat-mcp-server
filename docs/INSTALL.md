# Installing twincat-mcp on the engineering PC and running it for the first time

This is the install-to-first-green-light guide. It ends with a 20-minute smoke test that proves the
server, the ADS router, the target, the XAE shell and the safety guard each work, without writing a
single value to the machine. The deeper exercise of every layer is `HARDWARE_DAY.md`; do this one
first.

Conventions: `>` lines are typed in a terminal (cmd or PowerShell); `/…` lines are typed into the
Claude Code prompt (CLI or desktop app, same thing); "ask:" lines are plain-English requests to Claude
Code — it picks the tool. Replace `<CX_NETID>`, `<SOLUTION>` and `<PROJECT_DIR>` with your values.

---

## 1. What gets installed, and where

| piece | location | notes |
|---|---|---|
| plugin files (server sources, skill, launcher) | `%USERPROFILE%\.claude\plugins\…\twincat-mcp\` | managed by Claude Code; do not edit in place |
| private Python venv with `mcp`, `pyads`, `pywin32`, `pyyaml`, `uvicorn` | `%USERPROFILE%\.claude\plugins\data\twincat-mcp-twincat-mcp\venv\` (plugin) or `%LOCALAPPDATA%\twincat-mcp\venv\` (.mcpb) | built on first start, rebuilt only when the shipped sources change |
| per-project config | `<PROJECT_DIR>\twincat_mcp.toml` | solution path, allow/deny lists, HMI root — **you write this** |
| audit log (every write, every confirmation, every experiment write) | `%USERPROFILE%\.twincat_mcp\audit.jsonl` | append-only JSONL |
| recordings | `%USERPROFILE%\.twincat_mcp\recordings\<id>.jsonl` | one file per recording, can be large |
| capture profiles | `%USERPROFILE%\.twincat_mcp\profiles\` | |
| Infosys documentation index (`infosys_*` tools) | `<knowledge folder>\infosys\helpviewer-1033\` (else `%USERPROFILE%\.twincat_mcp\infosys\`) | SQLite + FTS5, built in the background |
| knowledge folder (`kb_set_location`, e.g. `D:\TwinCAT-MCP\knowledge`) | `knowledge.db`; the offline documentation the `twincat-docs` skill reads: `helpviewer\infosys_en.db` and `manualsdb\manuals.db` (PDFs in `manuals\`), built by the builders shipped with the plugin (`skills\twincat-docs\builders`, Python 3.12+), `sop\` | **you choose it**; `kb_status` → `docs` shows what is there |
| experiments (specs, runs, reports) | `<PROJECT_DIR>\experiments\` | next to the solution so they version with it |

Nothing is installed system-wide, nothing listens on a port, and the server runs as your Windows
user: it sees your TwinCAT router, your routes and your open XAE shell, and nothing else.

---

## 2. Prerequisites — verify each before installing

### 2.1 TwinCAT side

| requirement | how to verify | why |
|---|---|---|
| TwinCAT 3 XAE 4024 or later installed on this PC | `C:\TwinCAT\3.1\` exists; `echo %TWINCAT3DIR%` prints a path | `TcAdsDll.dll` comes with it; pyads cannot run without it |
| TwinCAT System Service running | tray icon is blue (CONFIG) or green (RUN) — not red | every ADS call goes through the local router |
| ADS route PC → CX9020 | XAE: System → Routes lists the CX, or **Choose Target** finds it | without a route every target call fails with ADS error 0x6 / 0x7 |
| CX9020 in RUN with the PLC project activated | XAE target state RUN; PLC login works | reads on a CONFIG-mode target return no symbols |
| Firewall | TCP 48898 inbound/outbound allowed for the TwinCAT System Service; UDP 48899 for discovery | symptoms otherwise: timeouts, `ads_discover` finds nothing |

If no route exists yet, add it from XAE (System → Routes → Add Route → Broadcast Search → select the CX
→ Add Route with the CX credentials). The server can also do it later with `ads_add_route_to_plc`, but
on day one do it in XAE so there is one less variable.

### 2.2 Python

```
> python -c "import struct,sys;print(struct.calcsize('P')*8, sys.version)"
64 3.12.x ...
```

Pass criterion: first token **64**, version **3.12 or newer** (3.12 is the version the project targets and tests). If it prints `32`, or `python` is not
found, install 64-bit Python from python.org and tick both **Add python.exe to PATH** and **py launcher**.
A 32-bit Python installs fine and then fails at the first ADS import, because `TcAdsDll.dll` is 64-bit.
An older Python (3.10, 3.11) is refused with one line naming the version found. Coming from a plugin version before
0.8.9 whose venv was built with an older Python: the launcher sees it and rebuilds the venv with the configured
Python 3.12 on the next start (one background install, 1–2 min, then reconnect).

`pip` must be able to reach PyPI once (venv build). Behind a corporate proxy set `HTTPS_PROXY` in the
environment Claude Code is started from.

### 2.3 Claude Code

```
> claude --version
```

2.1 or newer for plugins; 2.1.275+ for the one-line `--marketplace` install form. Logged in, and the
GitHub account Claude Code uses must be able to read `Disectron/twincat-mcp-server` (public repo:
any account; private: that account needs access).

### 2.4 Elevation

Run XAE and Claude Code at the **same** elevation — both as a normal user, or both "as administrator".
COM's Running Object Table does not cross the elevation boundary: an elevated XAE is invisible to a
non-elevated Claude Code, and `xae_attach` reports "no running XAE shell" even though it is open.

---

## 3. Install

### 3.1 Claude Code (CLI or desktop app, local session) — recommended

Needs **Git for Windows** on the PC: `claude plugin marketplace add owner/repo` clones the repository
(`git --version` must work in a new terminal). Then, in PowerShell, from the folder holding the `.sln`
(the server reads `twincat_mcp.toml` from the folder Claude Code is started in):

```powershell
cd <PROJECT_DIR>
claude plugin marketplace add Disectron/twincat-mcp-server
claude plugin install twincat-mcp@twincat-mcp
claude plugin list                               # must list twincat-mcp
```

Set the five plugin options from the shell (the in-session `/plugin configure` form reports "not installed
in this project" for a user-scope install on some Claude Code versions):

```powershell
'{"ams_net_id":"5.12.34.56.1.1","mode":"guarded","max_mode":"full"}' | claude plugin configure twincat-mcp@twincat-mcp --values-stdin
claude plugin configure twincat-mcp@twincat-mcp  # shows the values
```

| option | set it to | default if empty |
|---|---|---|
| `python_exe` | leave `python` if §2.2 passed with that name; otherwise the full path to a 64-bit `python.exe`. Never `py` (needs `-3`) | `python` |
| `ams_net_id` | the CX's AmsNetId, six dotted numbers, e.g. `5.12.34.56.1.1` (XAE → System → Routes). Leave empty to talk to the **local** runtime on the engineering PC | local runtime |
| `mode` | `guarded` | `guarded` |
| `max_mode` | `full` | `full` |
| `config_file` | full path to your `twincat_mcp.toml` (recommended — removes the dependence on the start folder) | `./twincat_mcp.toml`, then `%USERPROFILE%\.twincat_mcp\config.toml` |

If `claude plugin configure` also refuses, the options are plain environment variables and can be set in
`%USERPROFILE%\.claude\settings.json` instead — the launcher passes them through unchanged:

```json
{ "env": { "TCMCP_AMS_NET_ID": "5.12.34.56.1.1", "TCMCP_MODE": "guarded", "TCMCP_MAX_MODE": "full",
           "TCMCP_CONFIG": "C:\\Projects\\DrumFilling\\twincat_mcp.toml" } }
```

**Pre-build the venv now** (optional but recommended — it removes the one-minute first-start wait from §5):

```powershell
python "$env:USERPROFILE\.claude\plugins\cache\twincat-mcp\twincat-mcp\0.8.8\scripts\launch.py" --no-run
```
(the version folder changes with each plugin version; `dir $env:USERPROFILE\.claude\plugins\cache\twincat-mcp\twincat-mcp` lists it.)

In the desktop app the same plugin is available from **+ → Plugins → Add plugin** once the marketplace is
configured. Cloud and WSL sessions cannot run this plugin (no TwinCAT there); use a **Local** session.

Updating later: `claude plugin update twincat-mcp@twincat-mcp`. The launcher detects changed sources by
content hash and reinstalls into the same venv on the next start (in the background again — reconnect once).

### 3.2 Claude Desktop (the chat app) — the `.mcpb` bundle

Settings → Extensions → drag `dist\twincat-mcp-<version>.mcpb` in (or build it: `scripts\build-mcpb.cmd`).
The install dialog asks for the same five values. Same server, same tools, no skill file; the venv
lands in `%LOCALAPPDATA%\twincat-mcp\venv`. Python discovery order: the "Python executable" setting →
`py -3` → `python` → `%LOCALAPPDATA%\Programs\Python\Python3*` → `%ProgramFiles%\Python3*`.

### 3.3 By hand (fallback if the plugin path fails)

```
> git clone https://github.com/Disectron/twincat-mcp-server C:\tools\twincat-mcp
> cd C:\tools\twincat-mcp
> python -m venv .venv && .venv\Scripts\pip install -e .
> .venv\Scripts\python -m pytest -q          :: 170 tests; all must pass on this PC
> claude mcp add twincat --scope project ^
    -e TCMCP_CONFIG=<PROJECT_DIR>\twincat_mcp.toml -e TCMCP_MODE=guarded ^
    -- C:\tools\twincat-mcp\.venv\Scripts\python.exe -m twincat_mcp
```

Tool names then lack the `plugin_` prefix (`mcp__twincat__tc_doctor`) and the skills are not installed;
copy `skills\twincat-mcp\` and `skills\twincat-docs\` to `%USERPROFILE%\.claude\skills\` if you want them.

---

## 4. Configuration — `twincat_mcp.toml`

Copy `twincat_mcp.toml.example` to `<PROJECT_DIR>\twincat_mcp.toml` and edit. A day-one version for
the drum-filling machine, deliberately strict:

```toml
[ads]
ams_net_id = "<CX_NETID>"            # the CX9020; omit to talk to the local runtime instead
ams_port = 851                       # PLC runtime 1
timeout_ms = 5000

[xae]
solution_path = '<SOLUTION>'         # e.g. 'C:\Projects\DrumFilling\DrumFilling.sln' — picks this XAE instance in the ROT
attach_only = true                   # never start a shell by itself
# progids = ["TcXaeShell.DTE.15.0"]  # uncomment if tc_doctor shows only the 15.0 ProgID registered (4024 shell)
build_timeout_s = 900
dialog_watchdog = true
dialog_rules = []                    # start with none; add rules only for prompts you have seen and want auto-answered
allow_safety = false                 # TwinSAFE / TISC edits refused

[project]
# root = '<PROJECT_DIR>'             # defaults to the solution folder
tcunit_remote_result = 'boot:TcUnit_xUnit_testresults.xml'   # 'boot:' resolves on the target: \Hard Disk\TwinCAT\3.1\Boot on CE7
# hmi_root = '<PROJECT_DIR>\Hmi'     # TE2000 project folder, for hmi_scan / hmi_vs_runtime

[server]
tool_groups = ["ads", "rec", "snap", "test", "proj", "xae", "infosys", "wf"]   # only these groups (tc_* always); remove the line for all
# output_dirs = ['D:\Exports']        # extra folders tools may write files to; the recordings, project, solution, scope-export,
#                                    # experiments and audit-log folders are always allowed (existing files need overwrite=true)

[safety]
mode = "guarded"                     # readonly | guarded | full
max_mode = "full"                    # tc_set_mode may not go above this
confirm_destructive = true           # activate / restart / reboot / deploy need a confirmation token
confirm_ttl_s = 180
write_allowlist = []                 # EMPTY on day one: guarded mode then refuses every write (verified behaviour)
write_denylist  = ["*.bEStop*", "*EStop*", "GVL_Safety.*", "*.bEmergency*"]   # never writable in any mode
audit_reads = false
allow_elicitation = false            # every approval is a separate tc_request_confirmation token (field report #73)
edit_requires_config_mode = true     # code/project edits refused unless TwinCAT on the target is in CONFIG

[docs]
enabled = true
books = ["tcplclib_tc2_system", "tcplclib_tc2_standard", "tcplclib_tc2_utilities", "tcplclib_tc3_module",
         "tcplclib_tc2_math", "tcplclib_tc2_iofunctions", "tcplclib_tc3_eventlogger", "tcplclib_tc3_jsonxml"]
tc3_only = true
web_fallback = true
```

Precedence: environment variables `TCMCP_*` (that is what the plugin settings become) override the
TOML; the TOML overrides built-in defaults. The TOML is read from `TCMCP_CONFIG` if set, else
`.\twincat_mcp.toml` in the folder Claude Code was started in, else `%USERPROFILE%\.twincat_mcp\config.toml`.
`tc_doctor` prints which file it loaded (`config_file`); if that is `null`, the file was not found.

What each safety setting buys you:

- `mode = "guarded"` — reads free; writes only to `write_allowlist` and never to `write_denylist`;
  anything that changes machine state (activate, restart, deploy, reboot, start process, dialog
  buttons on machine-affecting prompts) returns a confirmation token and does nothing until the token
  is passed back within `confirm_ttl_s`.
- `write_allowlist = []` — the first write test (§6, step 10) adds exactly one pattern. Widen it only
  after that passes.
- `write_denylist` — wins over everything, including `full` mode and `tc_set_mode`.
- `max_mode` — if you want to make `full` unreachable for a session, set `max_mode = "guarded"`.

---

## 5. First start

Start a session in the project folder (`claude` in the terminal, or open the folder in the desktop app
as a Local session) and run `/mcp`.

What happens underneath: Claude Code runs `python <plugin>\scripts\launch.py --src <plugin>`. An MCP client
gives a server about 30 s to answer the handshake, and `pip install` takes longer, so on a **first start
(or after an update) the launcher does not install inline**: it starts the install as a detached background
process (log: `%USERPROFILE%\.claude\plugins\data\twincat-mcp-twincat-mcp\twincat-mcp-install.log`), and
exits with code 5. `/mcp` shows the server as failed for that one attempt. Reconnect after a minute
(`/mcp` → reconnect, or a new session) and it starts in about two seconds. The plugin's SessionStart hook
also kicks the same background build off, so usually the venv is ready by the time you reconnect. If you
ran the pre-build command in §3.1, none of this happens.

| `/mcp` shows | meaning | do |
|---|---|---|
| `twincat` connected, 151 tools | done | continue to §6 |
| `twincat` failed, first start or after an update | background install running (exit code 5) | wait a minute, reconnect from `/mcp` or start a new session |
| `twincat` failed, repeatedly | launcher could not start the server | `> claude --mcp-debug` and read the launcher's stderr; the common causes are the table below. Also read the install log named above |
| no `twincat` entry at all | plugin not loaded | `> claude plugin list` shows load errors; `CLAUDE_CODE_SKIP_PLUGIN_MCP_SERVERS` in the environment disables plugin servers |

First-start failures and their one-line fixes:

| stderr / install log says | fix |
|---|---|
| `'python' is not recognized` | §2.2; or set `python_exe` to the full path |
| `twincat-mcp: pyads could not load TcAdsDll.dll` + three numbered fixes | 32-bit Python, or TwinCAT not installed on this PC, or `TWINCAT3DIR` missing from the environment Claude Code runs in — the message names which |
| `pip … Could not find a version` / SSL / proxy errors | PyPI unreachable: set `HTTPS_PROXY`, or build the venv once by hand (§3.3) and point `TCMCP_VENV` at it |
| `first-start install is running in the background for N s` on every start, for more than 5 min | the background install died: read the install log; delete `%USERPROFILE%\.claude\plugins\data\twincat-mcp-twincat-mcp\.twincat-mcp-install.lock` and run the pre-build command from §3.1 by hand to see the error |
| `No module named twincat_mcp` after an update | stale venv: `> python <plugin>\scripts\launch.py --src <plugin> --reinstall --no-run` |
| server starts, `/mcp` shows tools, every ADS tool times out | not an install problem — §6 step 2 |

**First thing after `/mcp` is green: `run tc_doctor` and read `config_FIX_THESE`.** A value copied from a
template without editing (`<CX_NETID>`, `C:\path\to\YourSolution.sln`, `x.x.x.x.1.1`) or a malformed
AmsNetId is *not* loaded: the server falls back (local runtime, no solution path) and lists the offending
value there. `ads_target.ams_net_id` tells you which runtime you are actually talking to.

Config precedence: plugin options (they become `TCMCP_*` environment variables) override the TOML, the TOML
overrides built-in defaults. `tc_doctor.config_file` shows which TOML was read (`null` = none found).

Two things no longer need a reconnect:
- `tc_reload_config` re-reads the TOML and applies the allow/deny lists, confirmation settings, docs and
  dialog rules; it *reports* target, solution path and mode changes, which still need a restart.
- `tc_allow_write "GVL_Test.*"` adds an allow-list pattern for this session only, behind a confirmation,
  audited. The deny list still wins.

Behaviours that look like failures and are not (0.6.2 / 0.6.3):
- `tc_set_mode full` from `readonly`/`guarded` returns a confirmation request first. Escalation is confirmed
  and audited; lowering the mode is free. Loading `mode = "full"` from the TOML still needs no confirmation.
- `ads_write` of 70000 into a WORD (or any out-of-range integer, or `"yes"` into a BOOL) is refused with
  the type's range. Nothing is wrapped or coerced. Write the value the type can hold.
- `ipc_list_dir "boot:*"` reports `resolved_pattern` — the absolute folder the `boot:` prefix was mapped to
  by probing, because the target's FFIND ignores ADS path ids. If it resolved to the wrong folder, pass the
  absolute path and send the `resolved_pattern` back.
- `proj_lint` with `units: 0` and a note means `proj_scan` has not indexed that root yet — run it first;
  the result now names `root`, `files_indexed` and `parse_errors`.
- `xae_status`, `xae_build` and the `xae_plc` actions attach to the running shell themselves; after you
  close and reopen the XAE shell the next call re-attaches (one retry) instead of failing with
  `RPC server unavailable`.
- `hmi_scan` on a PLC project folder reports `.TcVIS` bindings (kinds `tcvis` and `tcvis_write`); point
  `project.hmi_root` at the TE2000 folder only if you have one.
- `ads_write` refuses, by design: a list shorter than the array (`has 8 elements; got 2`), POINTER/REFERENCE and
  INTERFACE variables, anything declared in a `VAR ... CONSTANT` block, a string longer than its STRING(n).
  `ads_write_many` writes nothing if any item would be refused, and lists every problem.
- A confirmation prompt now starts with `DESTRUCTIVE:` or `MACHINE CONTROL:` and names target, machine, mode and
  effect. `tc_audit_tail` shows `prompt_open_s`; `fast_answer` marks an accept under 1.5 s. If prompts get clicked
  through, set `allow_elicitation = false` and approve with tokens instead.
- `ipc_eventlog` is confirmed as `tc_request_confirmation(action='ipc_eventlog', target='Application')`.
  An empty result with `note: … not registered` and `providers_on_target` means the default provider name does not
  exist on this TwinCAT build — pass one of the listed names.
- `rec_start(..., start_when=…, duration_s=…)`: `duration_s` counts from the first sample, including the wait for
  the trigger. Use `post_trigger_s` for "N seconds after the trigger".
- (0.7) `rec_start` fails if any notification cannot be registered — by design, a recording with silent holes is
  worse than none. `allow_partial=true` records the rest and lists `unsubscribed_symbols`.
- (0.7) `rec_status` shows `samples`, `missing_samples`, `completeness`, `backlog`. `missing_samples > 0` in cycle
  mode means holes in the PLC timeline — trust timings only after `rec_verify <id> counter=<per-cycle counter>`
  says PASS. `stats_coverage < 1` only means the live statistics skipped samples under load; `rec_stats` after
  `rec_stop` recomputes them exactly from the file.
- (0.7) Recordings made by 0.6.x stay readable; new files are format 2 (raw bytes + PLC FILETIME, signed footer).
- (0.7) `tc_doctor` returns a one-line `summary` first; `tc_doctor verbose=true` for the full configuration.
  `[server] tool_groups = ["ads", "rec", "proj"]` exposes only those groups (tc_* always).

Tool names: every tool is `mcp__plugin_twincat-mcp_twincat__<name>`; you never type that, Claude does.
`/permissions` rules use that form, e.g. allow `mcp__plugin_twincat-mcp_twincat__ads_read*` to stop the
prompt on every read.

---

## 6. First-run test check (≈20 minutes, zero writes)

Run in order. Each step names the request, the expected result and the **pass criterion**; stop at the
first failure and send back the tool's error text plus one `tc_doctor` output. Steps 1–9 cannot change
anything on the machine: `write_allowlist` is empty and every destructive action needs a token.

| # | ask Claude Code | expected | pass if |
|---|---|---|---|
| 1 | "run tc_doctor" | a JSON report | `router: ok`; `local_ams_net_id` is this PC's NetId; `config_file` is your TOML; `safety.mode = guarded`; `target_state` RUN with `ads_state_name`; `xae_progids_registered` non-empty; `xae_running` lists your shell and solution; `infosys.fts5 = true` |
| 2 | "ads_status" | state RUN, device name, TwinCAT version of the **CX** | version is the CX's (e.g. 3.1.4024.x), not the PC's — proves the route reaches the target, not the local runtime |
| 3 | "list symbols matching MAIN.*, limit 30" | your program's variables with types | names you recognise; count plausible |
| 4 | "read MAIN.<a small FB instance>" | nested dict of members | no `raw_hex`, member names match the declaration, a REAL shows as a float |
| 5 | "ads_types ST_<one of your DUTs>" and "ads_leaves MAIN.<that FB> with values" | member offsets and sizes | compare three offsets against the TMC / Project → Compile info by hand: all three equal |
| 6 | "ads_task_stats, 50 samples" | cycle time, exceed counter per task | exceed counter does not increase during the sampling |
| 7 | "proj_scan" then "proj_variables scope=MAIN" | counts of POUs/DUTs/GVLs; flattened instance paths | file counts match the Solution Explorer; `proj_variables` count ≈ `ads_leaves MAIN` count |
| 8 | "proj_validate_xml" and "proj_lint" | findings lists | **no `error`-severity item** — the project builds, so any error here is a false positive in this repo: report it, do not edit the file |
| 9 | "xae_attach" then "xae_status" and "xae_dialogs" | attached to the shell holding `<SOLUTION>`; dialogs report shows `scope: XAE process <pid>` | `xae_status.solution` is your `.sln`; `scope` is not "none"; `live: []` with no dialog open |
| 10 | "xae_build" | build result with error/warning counts | matches what XAE's Error List shows for the same build |
| 11 | "infosys_get_page ADSREAD" | page title `ADSREAD`, inputs NETID…TMOUT with descriptions | if the title is "Beckhoff Information System" or everything is `Page`, save one Infosys page as HTML and send it — the HTML walker needs the real markup |
| 12 | "try to write MAIN.<any variable> := its current value" | **refused**: `guarded mode: write_allowlist is empty, so no symbol is writable` | the refusal text appears; nothing written |
| 13 | "try xae_activate_configuration" | **not executed**. With `allow_elicitation = false` (the recommended setting) the answer is a refusal that names `tc_request_confirmation(action=…, target=…)`; with `true` you get a `DESTRUCTIVE: xae_activate_configuration` prompt naming target, machine, mode and effect — answer **No** | nothing happens on the machine; if a token is offered, do **not** pass it back |
| 14 | "tc_audit_tail 20" | the refused write and the refused/declined activate | entries with `tool: ads_write, outcome: refused` (detail: the allow-list reason) and `tool: xae_activate_configuration, outcome: refused` ("allow_elicitation=false: token required" / "needs confirm_token") or `declined-elicitation:…` with `prompt_open_s` |

All 14 green means: install correct, router and route correct, target reachable and decodable, XAE
attached with dialog scope, offline checks silent on your code, guard refusing as configured, audit
trail recording. From here `HARDWARE_DAY.md` §6 onward (first recording, one controlled write,
experiments) continues on a **test box**, not the production machine.

If only step 11 fails, everything else is usable; Infosys lookup is the one layer that needs the real
website's markup to be verified.

---

## 6a. Native capture service — check it once

Cycle-mode recordings use `twincat_mcp\bin\tcmcp-capture.exe` (C++, Beckhoff AdsLib over TcAdsDll). Since 0.8.1 it
ships inside the plugin: the release workflow builds it, runs the test suite against it and commits it with
`tcmcp-capture.exe.sha256`; the server refuses an exe that does not match that hash. The same files are on the
GitHub release (`twincat-mcp-<version>-windows-x64.zip`, `.mcpb`, `SHA256SUMS`). Check where it came from:
`gh attestation verify tcmcp-capture.exe -R Disectron/twincat-mcp-server`. It is not code-signed: if SmartScreen
or the virus scanner quarantines it, allow it or set `[recorder] engine = "python"`.

```powershell
$exe = "$env:USERPROFILE\.claude\plugins\cache\twincat-mcp\twincat-mcp\0.8.8\twincat_mcp\bin\tcmcp-capture.exe"
& $exe --version      # {"version": "0.8.8", ... "ads": "Beckhoff AdsLib on TcAdsDll ..."}
& $exe --selftest     # six "ok" lines
```

Then in Claude: `tc_doctor` → `recorder.cycle_mode_uses` must be `native`. `rec_capture symbols=[GVL_Test.aCounter]
seconds=10 mode=cycle cycle_ms=1` → `engine: native`, `verify.verdict: PASS`. If it says `engine: python`, the
`engine_note` gives the reason (exe missing, other version, TcAdsDll.dll not found).

## 6b. Every tool, automatically — the acceptance run

The 14 steps above are the hand check. To exercise **every** tool against the live PC and target and get a
per-tool PASS/FAIL report, run the acceptance script from the solution folder (same config the plugin
uses; it needs the venv the launcher built):

```powershell
$v = "$env:USERPROFILE\.claude\plugins\data\twincat-mcp-twincat-mcp\venv\Scripts\python.exe"
& $v -m twincat_mcp.acceptance                      # read-only; XAE phase if a shell is attached; docs phase if the web is reachable
& $v -m twincat_mcp.acceptance --no-xae --no-infosys
& $v -m twincat_mcp.acceptance --allow-writes --write-symbol GVL_Test.nScratch   # one idempotent write + snapshot restore
```

It discovers its own inputs from the target (a scalar, a BOOL, a struct, the root program), runs the
read-only tools, a 2 s recording round trip, snapshots and expectations, the experiment pipeline in dry
run, the documentation lookup, the XAE read-only tools, and — the point of guarded mode — proves that a
write outside the allow list, a control action and a destructive action are **refused**. Tools that would
change the machine are listed as MANUAL with the step from `HARDWARE_DAY.md` that covers each. The report
lands in `<project>\acceptance\acceptance_<timestamp>.md` (+ `.json`); exit code 0 means no FAIL.
Send the `.md` back when anything fails — it carries the exact tool, arguments and error text.

Statuses: PASS answered with the expected shape · FAIL error or wrong shape · WARN an optional feature
(EtherCAT master, HMI folder, web) answered with an environmental error · SKIP prerequisite missing ·
MANUAL changes the machine. Any `error`-severity finding from `proj_lint` / `proj_validate_xml` on a project
that builds is reported as a WARN with the rule name: those are the false positives I want to hear about.

---

## 7. Resetting, updating, removing

| action | how |
|---|---|
| rebuild the venv | `> python <plugin>\scripts\launch.py --src <plugin> --reinstall --no-run`, or delete the venv folder from §1 and start a session (the first start then installs in the background — reconnect once) |
| update the server | `/plugin update twincat-mcp@twincat-mcp`; the next start reinstalls automatically |
| change safety mode for one session | ask "tc_set_mode full" (bounded by `max_mode`; written to the audit log) |
| allow one symbol for this session | ask "tc_allow_write GVL_Test.*" — confirmation prompt, audited, gone at restart |
| apply an edited TOML without restarting | ask "tc_reload_config" |
| see what the server loaded | `tc_doctor` → `config_file`, `safety`, `ads_target` |
| remove | `/plugin uninstall twincat-mcp@twincat-mcp`; delete `%USERPROFILE%\.twincat_mcp\` if you want the audit log, recordings and index gone too |

---

## 8. What to send back when something fails

1. The tool's error text verbatim (it carries the ADS or HRESULT code).
2. One `tc_doctor` output.
3. For a datatype decode mismatch (step 4/5): the `ads_types` output and the TMC excerpt for that type.
4. For step 11: one Infosys page saved as HTML.
5. For a plugin that does not start: the `claude --mcp-debug` lines from the launcher.

One constant, one ProgID or one DTE command name is the usual fix.
