"""MCP server wiring: tools, resources, safety plumbing.

Tool prefixes
  tc_*     meta / safety / diagnostics
  ads_*    runtime (target IPC) over ADS
  ipc_*    target file system / OS via the TwinCAT System Service
  xae_*    TwinCAT XAE Automation Interface (engineering PC)
  proj_*   offline project files
  tcunit_* TcUnit orchestration + result parsing
  exp_*    experiments: spec → runs → metrics with CIs → verdict → report
  infosys_* Beckhoff Infosys reference (TwinCAT 3 docs from the local Help Viewer package, offline)

Confirmation for destructive operations: if the MCP client supports
elicitation, the human is asked directly (prompting.ask: returned as an input_required
result on MCP 2026-07-28, an elicitation request on earlier revisions); otherwise the
two-step confirm_token flow applies (``tc_request_confirmation``).
"""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import inspect
import datetime as dt
import json
import logging
import os
import platform
import re
import sqlite3
import sys
import threading
import time
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, Field

import contextvars
import copy
import functools
import secrets

from mcp import MCPError
from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

log = logging.getLogger(__name__)

from . import __version__
from .config import Config, load as load_config, is_windows
from .safety import Guard, OpClass, SafetyError
from .worker import SerialWorker, WorkerBusy
import pyads

from .ads_backend import MIB, AdsBackend, AdsError, _fmt_exc as _fmt_ads_exc
from .ads_files import SystemServiceFiles
from .xae_backend import XaeActedUnknown, XaeBackend, XaeBusy, XaeError, XaeExplained, sta_init, sta_teardown
from .project_backend import PlcView, ProjectIndex, ProjectError, declaring, not_on_target, parse_tcunit_xml
from .recorder import Recorder, RecorderError
from .testing import TestBench, TestError
from . import codecheck
from . import workflows
from . import exporters
from .hmi import HmiIndex
from .infosys import InfosysIndex, InfosysError
from . import profiles as capture
from . import knowledge as K
from .recorder import read_footer as K_footer
from . import experiments as X
from . import prompting as ASK
from pathlib import Path

INSTRUCTIONS = """\
twincat-mcp: control of a Beckhoff TwinCAT 3 engineering PC and its target IPC.

Workflow for debugging a running machine:
  1. tc_doctor → confirms ADS router, XAE shell, project root, safety mode.
  2. ads_status → PLC state, TwinCAT run/config mode, device info.
  3. ads_list_symbols / ads_read / ads_read_many → inspect live values
     (structs, FB instances and arrays of structs decode fully via the datatype
     table; TIME/DATE decoded; enums named). ads_leaves 'MAIN' lists every
     variable under a program; ads_dump reads every variable on the PLC to
     JSON+CSV; proj_variables lists what the source declares; proj_vs_runtime
     proves the target runs this source.
  4. ads_watch / ads_wait_until → short timing captures (≤60 s, inline).
     rec_start → continuous real-time capture while the program runs (PLC-time-
     stamped notifications, hours long, file-backed); rec_snapshot / rec_stats /
     rec_read while it runs; rec_stop; rec_export to CSV. Use stop_when /
     start_when + pre_trigger_ms as a software scope.
  5. proj_resolve_symbol → jump from a live symbol to file:line in the source.
  6. proj_get_code / proj_search / proj_references → read the logic.
  7. Edit (proj_set_code or xae_pou_set), xae_build → errors with file:line.
  8. xae_activate_configuration + xae_restart_twincat (or xae_deploy) → deploy.
     These need a confirmation (elicitation, or tc_request_confirmation token).
  9. tcunit_run → build, boot, activate, wait for xUnit result, parse.

Experiments: exp_template → edit → exp_save → exp_run (dry_run first) → exp_status → exp_evaluate →
exp_report. Runs are repeatable, metrics carry 95 % CIs, verdicts are pass/fail/inconclusive; the
report separates computed sections from interpretation. Use it for every "did the change help?"
question instead of comparing two fills by eye.

Reference: documentation questions (library FBs, types, error codes, concepts, hardware manuals) go
to the twincat-docs skill of this plugin (offline: <kb>\\index\\infosys-1033.db + <kb>\\index\\manuals.db;
kb_status shows them). The ads_* tools read the running PLC only and never return documentation.
infosys_declaration <FB|struct|enum> gives the library symbol's declaration with descriptions; infosys_search_symbols / infosys_search / infosys_search_code / infosys_get_page
look up Beckhoff Infosys (TwinCAT 3 only) from the Help Viewer package installed with XAE — indexed
locally, no network (docs.source='web' uses the website instead). Use it before guessing a library FB's inputs.

Safety: tc_safety_status shows the mode. readonly blocks all changes;
guarded needs a write allowlist and confirmations; full is unrestricted except
for destructive ops, which still confirm unless TCMCP_CONFIRM=0.
Every mutating call is appended to the audit log (tc_audit_tail).
Never call ads_write on an output-mapped symbol of a running machine without
knowing what the actuator does. Reads are always safe.
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
DESTR = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False)
CONTROL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)   # changes the server's own state under confirmation


# N169 (0.8.14 retest): ranges are in the tool schemas, not only in the refusal. A search's limit has no maximum in
# the schema: a larger one is used as the maximum and said so (N167, clamped); max_chars is refused out of range, with
# the way to read on — so the bounds are the schema's, the check (and its message) the tool's
PageLimit = Annotated[int, Field(json_schema_extra={"minimum": 1}, description="hits per page, 1-50; a larger limit is "
                                 "used as 50 (clamped: true)")]
HwLimit = Annotated[int, Field(json_schema_extra={"minimum": 1}, description="items per page, 1-100; a larger limit is "
                               "used as 100 (clamped: true)")]
Offset = Annotated[int, Field(json_schema_extra={"minimum": 0}, description="skipped first: the next page is "
                              "offset=<next_offset>")]
Limit50 = Annotated[int, Field(json_schema_extra={"minimum": 1, "maximum": 50}, description="results shown, 1-50")]
PageChars = Annotated[int, Field(json_schema_extra={"minimum": 200, "maximum": 60000},
                                 description="characters shown, 200-60000; read on with section= or offset=")]


class ConfirmSchema(BaseModel):
    confirm: bool = Field(description="Set true to allow this operation on the target")


class AlsoScopeSchema(BaseModel):
    also_scope: bool = Field(description="Also record with the TwinCAT Scope configuration open in XAE")


class ScopeNextSchema(BaseModel):
    next: Literal["close", "new", "pause"] = Field(
        description="close: disconnect Scope (ready for the next recording) · new: disconnect and start a new Scope "
                    "recording now · pause: leave Scope holding the data to look at in XAE")


class ScopeDisconnectSchema(BaseModel):
    disconnect: bool = Field(description="Disconnect Scope (it discards what it holds) and record with it now")


_LICENSE_CODES = {
    "0x98110724": "no valid TwinCAT license for the target (2551252772 / 0x98110724).",
    "2551252772": "no valid TwinCAT license for the target (2551252772 / 0x98110724).",
}


def _tcunit_publish_in_source(app: "App") -> dict[str, Any]:
    """D40: TcUnit's xUnitEnablePublish as the PLC project source sets it: True / False (TcUnit referenced, parameter
    absent or FALSE) / None (no source, or no TcUnit reference)."""
    out: dict[str, Any] = {"value": None, "plcproj": None}
    if app.proj.root is None:
        out["why"] = "no project source"
        return out
    try:
        app.proj._ensure()
        files = [pp["file"] for pp in app.proj.plcproj]
    except Exception as exc:  # noqa: BLE001
        out["why"] = str(exc)[:120]
        return out
    for f in files:
        try:
            txt = Path(f).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if not re.search(r'(?:Placeholder|Library)Reference\s+Include="TcUnit[",]', txt):
            continue
        m = re.search(r"<Key>\s*xUnitEnablePublish\s*</Key>\s*<Value>\s*(\w+)\s*</Value>", txt, re.I)
        out.update({"plcproj": f, "value": bool(m and m.group(1).upper() == "TRUE"),
                    "source": "parameter in the .plcproj" if m else "not set (TcUnit default FALSE)"})
        return out
    out["why"] = "no TcUnit reference in the project source"
    return out


def _call_port(fn: Any, a: tuple, kw: dict) -> Any:
    """The ADS port a backend call targets, passed by keyword or positionally (D24)."""
    if kw.get("port") is not None:
        return kw["port"]
    try:
        bound = inspect.signature(fn).bind_partial(*a, **kw)
    except (TypeError, ValueError):
        return None
    return bound.arguments.get("port")


def _license_hint(text: str) -> str | None:
    """Beckhoff reports license problems as an opaque number; say what it means and what to do."""
    low = (text or "").lower()
    if not ("license" in low or "licence" in low or any(c in low for c in _LICENSE_CODES)):
        return None
    what = next((v for k, v in _LICENSE_CODES.items() if k in low), "the target reports a TwinCAT license problem.")
    return (what + " The runtime falls back to CONFIG. Fix: in XAE open SYSTEM → License → Order Information, "
            "click '7 Days Trial License', type the captcha, then ActivateConfiguration again (xae_activate_configuration) "
            "and restart (xae_restart_twincat). A purchased license is entered on the same page (TC1200 for the PLC runtime). "
            "On a CX with pre-installed licenses this means the activated configuration asks for a function level or a TFxxxx the box does not have — check Project → System → License → Manage Licenses.")


_RESTART_WITNESS = ("TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount",
                    "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleTime",
                    "TwinCAT_SystemInfoVarList._AppInfo.AppTimestamp")


def restart_proven(before: dict[str, Any], after: dict[str, Any]) -> tuple[bool | None, str]:
    """R40: did the runtime restart between two witnesses (restart_witness)? True = restarted, False = provably not,
    None = cannot tell. A counter below its earlier value has started again (unless it could have wrapped). Task 1's
    CycleCount × cycle time is how long the task has run: at least the time since the first witness → no restart in
    that time. Shorter is NOT proof of a restart when the counter did not go back: a task that stood still (breakpoint,
    PLC stopped while TwinCAT stays in RUN) also counts fewer cycles than the time allows — a frozen counter (30 s of
    cycles, 35 s elapsed) was reported as 'started within that time'. That case is 'cannot tell'."""
    a_ts, b_ts = after.get("app_timestamp"), before.get("app_timestamp")
    if a_ts is not None and b_ts is not None and a_ts != b_ts:
        # AppTimestamp is the time the PLC application was compiled (InfoSys PlcAppSystemInfo): a different value means a
        # different application is running — it was loaded, which takes a start of the runtime's PLC
        return True, f"the PLC application's compile timestamp changed ({b_ts} → {a_ts}): a newly built application runs"
    b, a = before.get("cycle_count"), after.get("cycle_count")
    if not isinstance(b, int) or not isinstance(a, int):
        miss = [f"{side} ({(w.get('unreadable') or {}).get('cycle_count') or (w.get('unreadable') or {}).get('all') or 'not read: TwinCAT was not in RUN'})"
                for side, w, v in (("before the command", before, b), ("after it", after, a)) if not isinstance(v, int)]
        return None, "task 1's cycle counter was not readable " + " and ".join(miss)
    elapsed = max(0.0, float(after.get("t", 0)) - float(before.get("t", 0)))
    ct = after.get("cycle_time_100ns") or before.get("cycle_time_100ns")
    if not isinstance(ct, (int, float)) or ct <= 0:
        if a < b and b < 0x80000000:
            return True, f"task 1's CycleCount started again ({b} → {a})"
        if a < b:          # without the cycle time the wrap window is unknown: a high counter may have wrapped
            return None, f"CycleCount {b} → {a}: the counter may have wrapped (task cycle time unknown)"
        return None, f"CycleCount {b} → {a}, but the task cycle time is unknown"
    cycle_s = ct / 1e7
    window = elapsed / cycle_s                                  # cycles since the first witness
    if a < b:
        if b > 0xFFFFFFFF - 2 * window:
            return None, f"CycleCount {b} → {a}: the counter may have wrapped"
        return True, f"task 1's CycleCount started again ({b} → {a})"
    uptime = a * cycle_s
    if uptime >= elapsed - 0.5:
        return False, (f"task 1's CycleCount went on from {b} to {a}: the task has run for {uptime:.0f} s, longer than "
                       f"the {elapsed:.0f} s since the command — no restart")
    if a == b:
        return None, (f"task 1's CycleCount did not advance ({a}) in {elapsed:.1f} s: the task stands still (breakpoint, "
                      "PLC stopped) — a restart can be neither shown nor excluded by it")
    return None, (f"task 1's CycleCount went on from {b} to {a} ({uptime:.1f} s of cycles, {elapsed:.1f} s elapsed): a "
                  "restart within that time, or a task that stood still for part of it — the counter cannot tell "
                  "which (it did not go back)")


_TRANSITION_KEYS = ("transition_seen", "transition_by", "mode_before", "witness_before", "witness_after", "witness")


def _app_ts_text(v: Any) -> str | None:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        return dt.datetime.fromtimestamp(int(v), dt.timezone.utc).strftime("DT#%Y-%m-%d-%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def plc_application(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, Any]:
    """Which PLC application runs after an activation + restart, compared with the one before (AppTimestamp = the
    application's compile time). Field test 0.8.10: an activation without a freshly generated boot project kept the
    old application while every step reported success."""
    b = (before or {}).get("app_timestamp")
    a = (after or {}).get("app_timestamp")
    out: dict[str, Any] = {"compiled_before": _app_ts_text(b) or b, "compiled_now": _app_ts_text(a) or a}
    if a is None:
        out["changed"] = None
        out["note"] = "the running application's compile time was not readable after the restart"
    elif b is None:
        out["changed"] = None
        out["note"] = "no compile time from before (TwinCAT was not in RUN then): nothing to compare"
    else:
        out["changed"] = a != b
        if a == b:
            out["note"] = ("the target runs the same PLC application as before (same compile time). Expected when the PLC "
                           "code did not change; if it did, the target did not get it — the boot project was not "
                           "regenerated: xae_boot_project(generate=true) or xae_deploy, then activate again")
    return out


def _restart_timing(t_cmd: float, ver: dict[str, Any]) -> dict[str, Any]:
    """N5: how long a restart took — from its command until a poll saw the target running again after a proven
    transition. Not seen running again (timeout, an error dialog, no transition): no duration, and why."""
    seen = ver.pop("_run_seen_mono", None)
    poll_s = ver.pop("_poll_s", None)
    if seen is None:
        return {"duration_s": None, "duration_note": "the target was not seen running again after the restart — its "
                                                     "duration is not known"}
    return {"duration_s": round(seen - t_cmd, 1),
            "duration_note": f"until a poll saw RUN again (polled every {poll_s:g} s: the restart may have ended up "
                             "to one poll earlier)"}


def _restart_outcome(ver: dict[str, Any]) -> str:
    """Audit outcome of a restart that was not verified — what was actually wrong."""
    if ver.get("dialogs"):
        return "dialog-after-restart"
    return "restart-not-proven" if ver.get("target_mode") == "RUN" else "not-running-after-restart"


def _restart_failure(ver: dict[str, Any]) -> str:
    """The error text: 'not in RUN' only when the mode is not RUN (R40: '… not in RUN afterwards (mode RUN)')."""
    if ver.get("dialogs"):
        d = ver["dialogs"][0]
        msg = f"the shell shows a dialog ({d.get('title')}: {str(d.get('text', ''))[:300]}); target mode {ver['target_mode']}"
    elif ver.get("target_mode") == "RUN":
        msg = "the target is in RUN while a restart into the new configuration was not proven"
    else:
        msg = f"the target is not in RUN afterwards (mode {ver['target_mode']})"
    return msg + (f". {ver['hint']}" if ver.get("hint") else "")


# N40: what each confirmable action's token names. The entries with an argument name take that argument as it is;
# the others are computed in App.token_target. tc_request_confirmation's docstring is generated from this table.
TOKEN_TARGET_DOC = {
    "xae_deploy / xae_restart_twincat / xae_activate_configuration / tcunit_run": "the XAE target NetId",
    "xae_plc": "'<action>:<PLC project path>@<XAE target NetId>'",
    "xae_command": "'<command name>@<XAE target NetId>'",
    "xae_boot_project": "the plc_project_path (with '@<XAE target NetId>' when activate_after_generate)",
    "xae_tree_delete": "'<parent_path>^<name>'",
    "xae_target": "the new NetId", "xae_dialog_resolve": "'<dialog title>:<button>'",
    "ads_plc_state / ads_system_mode": "the action (run, stop, reset / run, config)",
    "ads_connect": "the NetId, or 'local'", "ads_add_route_to_plc": "the PLC's IP address",
    "ipc_write_file / ipc_delete_file / ipc_start_process": "the remote path", "ipc_eventlog": "the log name",
    "ipc_reboot": "the literal 'target'", "rec_delete": "the recording id", "tc_set_mode": "the mode",
    "tc_allow_write": "the pattern ('-pattern' to remove)", "exp_run": "the experiment name",
    "kb_rebuild": "the index: 'infosys', 'manuals' or 'esi'",
}
TOKEN_TARGET_ARGS = {"xae_target": "net_id", "ads_plc_state": "action", "ads_system_mode": "action",
                     "ads_connect": "ams_net_id", "ads_add_route_to_plc": "plc_ip", "ipc_write_file": "path",
                     "ipc_delete_file": "path", "ipc_start_process": "path", "ipc_eventlog": "log",
                     "ipc_reboot": "", "rec_delete": "id", "tc_set_mode": "mode", "exp_run": "name",
                     "kb_rebuild": "index"}


def _plc_iec_path(path: str) -> str:
    from .xae_backend import _plc_split
    return _plc_split(path)[1]


def _builder_pid(proc: Any, lock: Path, wait_s: float = 3.0) -> dict[str, Any]:
    """The pid of the build a kb_rebuild started, as kb_status gives it: the one its lock names. S4 (0.8.14 retest):
    kb_rebuild returned the pid of the process it started and kb_status another — on Windows a venv's python.exe
    starts the interpreter that builds as its child. Nothing else builds meanwhile (checked before the start), so the
    pid in the held lock is this build's; not written yet within wait_s, or the process ended: the process started."""
    from . import kb_lock
    end = time.monotonic() + wait_s
    while time.monotonic() < end:
        if kb_lock.held(lock):
            pid = kb_lock.read(lock)[0].get("pid")
            if isinstance(pid, int):
                return {"pid": pid} if pid == proc.pid else {"pid": pid, "launcher_pid": proc.pid}
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    return {"pid": proc.pid, "pid_note": "the process started; the build had not taken its lock yet — on Windows a "
                                         "venv's python.exe runs it in a child process, whose pid kb_status gives"}


def _merge_checks(outs: list[tuple[str | None, dict[str, Any]]]) -> dict[str, Any]:
    """N113: the results of one analysis per PLC project as one: numbers added, number tables added per key, lists
    joined (each dict item tagged with its plc_project), other values kept per PLC project when they differ."""
    merged: dict[str, Any] = {"plc_projects": [plc for plc, _r in outs]}
    other: dict[str, dict[str, Any]] = {}
    for plc, r in outs:
        for k, v in r.items():
            if isinstance(v, bool) or v is None or isinstance(v, str):
                other.setdefault(k, {})[plc or ""] = v
            elif isinstance(v, (int, float)):
                merged[k] = merged.get(k, 0) + v
            elif isinstance(v, list):
                merged.setdefault(k, []).extend({**x, "plc_project": plc} if isinstance(x, dict) else x for x in v)
            elif isinstance(v, dict) and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v.values()):
                tab = merged.setdefault(k, {})
                for kk, vv in v.items():
                    tab[kk] = tab.get(kk, 0) + vv
            else:
                other.setdefault(k, {})[plc or ""] = v
    for k, per in other.items():
        vals = list(per.values())
        merged[k] = vals[0] if all(x == vals[0] for x in vals) else per
    sev = {"error": 0, "warning": 1, "info": 2}
    if isinstance(merged.get("items"), list) and all(isinstance(x, dict) and "severity" in x for x in merged["items"]):
        merged["items"].sort(key=lambda f: (sev.get(f["severity"], 3), f.get("file") or "", f.get("line") or 0))
    if isinstance(merged.get("items"), list) and len(merged["items"]) > 1000:
        merged["items_shown"] = f"1000 of {len(merged['items'])} (the most severe first)"
        merged["items"] = merged["items"][:1000]
    return merged


class TargetArgsError(ToolError):
    """token_target: the shell answered, but the arguments name no target (N41/N42: which PLC project)."""


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.guard = Guard(cfg.safety)
        self.ads = AdsBackend(cfg.ads)
        self.files = SystemServiceFiles(self.ads)
        import threading
        self.ads_worker = SerialWorker("tcmcp-ads", cancel_event=threading.Event())       # quick calls
        self.ads_slow_worker = SerialWorker("tcmcp-ads-slow", cancel_event=self.ads.cancel)  # watch/wait/scenario/recorder
        self.ads_bulk_worker = SerialWorker("tcmcp-ads-bulk")                                  # ads_dump (R6)
        self.xae = XaeBackend(cfg.xae)
        self.xae_worker = SerialWorker("tcmcp-xae-sta", init=sta_init, teardown=sta_teardown)
        # N85: an XAE call held by a dialog it opened (the call is still inside the shell's modal loop) and what such
        # calls returned once their dialog was answered
        self.xae_held: dict[str, Any] | None = None
        self.xae_late: list[dict[str, Any]] = []
        self.last_activation: dict[str, Any] | None = None     # witness before the last xae_activate_configuration
        self.proj = ProjectIndex(cfg.project)
        # D44: code set in the shell (xae_pou_set / xae_pou_create) until it is proven on disk
        self.pending_edits: dict[str, dict[str, Any]] = {}
        self.ads.write_guard = self.constant_reason       # every write path: CONSTANT / no-source checks
        self.ads.interface_hint = self.is_source_interface
        self.ads.alias_hint = self.source_alias
        from .config import _home
        self.kb = K.Knowledge(_home(), cfg.knowledge.location, cfg.knowledge.esi_dirs, cfg.knowledge.library_dirs)
        # the permanent layout of the knowledge folder (docs/KNOWLEDGE_BASE.md) before anything opens an index
        self.kb.layout()
        # the builder stubs in <kb>\tools run this plugin's builders: record where they are, turn old copies into stubs
        threading.Thread(target=self._builder_upkeep, name="tcmcp-builders", daemon=True).start()
        # N174: crash tracebacks (and rejected COM calls) in a file next to the other logs, named in the error message
        self.server_log = install_server_log(Path(cfg.safety.audit_log).parent / "logs" / "server.log")
        self.rec = Recorder(self.ads, Path(cfg.safety.audit_log).parent / "recordings")
        # N182 (0.8.15 retest): recording id → the Scope part recorded with it (build_server's _ScopePart), each owned
        # by one task — 0.8.17: five shared tables and four actors raced in every review round of 0.8.16
        self.scope_parts: dict[str, Any] = {}
        # 0.8.17 (Ryan, 2026-10-21): what Scope holds after a recording's Scope part stopped it — Scope stays in Reply
        # with that recording until it is disconnected (N186); the user chooses close / new / pause
        self.scope_left: dict[str, Any] | None = None
        # N189 (0.8.17 retest): there is one Scope — a call that starts it holds it from its check on (scope_claim),
        # a Scope recording on its own (scope_record start / new) holds it until it is stopped (scope_standalone)
        self.scope_claim: str | None = None
        self.scope_standalone: dict[str, Any] | None = None
        self.rec.rcfg = cfg.recorder

        # N62: the recorder compares cycle_ms with the task that runs each recorded program (project task config)
        def _task_of(name: str, port: int | None = None) -> list[dict[str, Any]] | None:
            return self.proj.tasks_of(name, port)
        self.rec.task_of = _task_of
        self.rec.task_reason = lambda name, port=None: self.proj.task_reason(name, port)
        self.bench = TestBench(self.ads, self.rec, Path(cfg.safety.audit_log).parent / "snapshots")
        self.hmi = HmiIndex()
        self.profiles = capture.ProfileStore(Path(cfg.safety.audit_log).parent / "profiles")
        self.exp_store = X.ExperimentStore(cfg.project.experiments_dir)
        self.runners: dict[str, X.Runner] = {}
        self.docs_bg: dict[str, Any] = {}
        self.session_allow: list[str] = []      # tc_allow_write additions; survive tc_reload_config (N6)
        threading.Thread(target=self._seal_sweep, name="tcmcp-seal-sweep", daemon=True).start()
        self.docs: InfosysIndex | None = None
        self.docs_error: str | None = None
        self.docs_build_target: Path | None = None     # where a build of the local package's index writes
        self.infosys_build_proc: Any = None
        if cfg.docs.enabled:
            try:
                local = None
                cache = cfg.docs.cache_dir
                db_file: Path | None = None
                if cfg.docs.source == "local":
                    from . import helpviewer as HV
                    from . import infosys_build as IB
                    pk = HV.find_packages(cfg.docs.mshc)
                    if not pk:
                        where = cfg.docs.mshc or " / ".join(str(r) for r in HV.catalog_roots())
                        raise RuntimeError(
                            f"docs.source = 'local' but no Beckhoff Infosys Help Viewer package (BKINFOSYS3_*.mshc) was "
                            f"found under {where}. Install the TwinCAT documentation for the Help Viewer, set "
                            "[docs] mshc = '<path to the .mshc>', or docs.source = 'web' to use infosys.beckhoff.com.")
                    local = HV.HelpViewerSource(pk[0])
                    db_file = self.docs_target(local)
                    IB.prepare_for_readers(db_file)
                    self.kb.docs_local = local
                self.docs = InfosysIndex(cache, books=cfg.docs.books, exclude_books=cfg.docs.exclude_books,
                                         tc3_only=cfg.docs.tc3_only, max_workers=cfg.docs.max_workers,
                                         max_index_pages=cfg.docs.max_index_pages, sitemap_ttl_days=cfg.docs.sitemap_ttl_days,
                                         web_fallback=cfg.docs.web_fallback, fetch_timeout_s=cfg.docs.fetch_timeout_s,
                                         local=local, db_file=db_file, read_only=local is not None)
            except Exception as exc:  # noqa: BLE001 — a broken cache dir / missing package must not take the server down
                self.docs_error = str(exc)
                log.warning("infosys index disabled: %s", exc)
        # R18: the first Infosys query after a start read a cold 372 MB database (9–17 s); warm it in the background
        self.docs_warm: dict[str, Any] = {"state": "off"}
        if self.docs is not None:
            self.docs_warm = {"state": "warming", "started": time.time()}
            threading.Thread(target=self._warm_docs, name="tcmcp-infosys-warm", daemon=True).start()
        self.started = time.time()

    def _warm_docs(self) -> None:
        if self.docs is not None and self.docs.local is not None:
            try:
                self.infosys_upkeep()
            except Exception as exc:  # noqa: BLE001 — the tools work without it; tc_doctor shows the state
                log.warning("infosys index upkeep: %s", exc)
        try:
            r = self.docs.warm()
            self.docs_warm = {"state": "warm", **r}
        except Exception as exc:  # noqa: BLE001
            self.docs_warm = {"state": "failed", "error": str(exc)[:160]}

    # --- the Infosys index of the local package (docs/KNOWLEDGE_BASE.md rule 6) ---------------------------------
    def docs_target(self, local: Any) -> Path:
        """ONE file for the local package's index, read and built at the same place: <kb>\\index\\infosys-<lcid>.db —
        or, while its move into index\\ waits (the old file was in use), the old file itself; without a knowledge
        folder, the server's own folder. Sets docs_build_target."""
        from . import kb_layout as L
        kb_dir = self.kb.location()[0]
        if kb_dir is not None:
            self.docs_build_target = L.locate(kb_dir, "infosys", local.lcid)
        else:
            self.docs_build_target = Path(self.cfg.docs.cache_dir) / f"helpviewer-{local.lcid}" / "index.sqlite"
        return self.docs_build_target

    def docs_retarget(self) -> dict[str, Any] | None:
        """After kb_set_location: the index moves with the knowledge folder — read (and built) from the new folder at
        once; a folder without one gets its first build (rule 6). None when there is no local package."""
        if self.docs is None or self.docs.local is None:
            return None
        from . import infosys_build as IB
        before = self.docs.db_file
        target = self.docs_target(self.docs.local)
        if target == before:
            return None
        IB.prepare_for_readers(target)
        self.docs.db_file = target
        self.docs._probed_sig = None
        out: dict[str, Any] = {"infosys_index": str(target), "was": str(before)}
        if not target.is_file():
            out["first_build"] = self.spawn_infosys_build(first=True)
        return out

    def infosys_upkeep(self) -> dict[str, Any]:
        """At start: a finished build that could not be swapped in (an approved one, or the first) is swapped in; the
        FIRST build of an index that does not exist yet starts by itself. An index that exists is never rebuilt
        here — a new format or another package is reported until kb_rebuild is approved."""
        from . import infosys_build as IB
        assert self.docs is not None and self.docs.local is not None and self.docs_build_target is not None
        out: dict[str, Any] = {}
        done = IB.finish_pending(self.docs_build_target, self.docs.local)
        if done:
            out["finished_pending"] = done
            self.docs._probe()
        if not self.docs.db_file.is_file():
            out["first_build"] = self.spawn_infosys_build(first=True)
        return out

    def spawn_infosys_build(self, force: bool = False, first: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """The build of the local package's index, in its own process (the parse is CPU-bound for minutes: in the
        server it would take the time of the ADS and recorder threads) — the builder the knowledge folder's stub runs,
        with this server's Python. It survives a restart of the server; its state is infosys_build.state. dry_run:
        every check, nothing started -> {'would_start': True} or why not (N96: asked before it could run)."""
        import subprocess
        from . import infosys_build as IB
        if self.docs is None or self.docs.local is None or self.docs_build_target is None:
            return {"started": False, "error": "no local Help Viewer package (docs.source = 'local')"}
        b = K.builders_dir()
        if b is None or not (b / "infosys_local.py").is_file():
            return {"started": False, "error": "the plugin's builders (skills/twincat-docs/builders) were not found"}
        target = self.docs_build_target
        st = IB.state(target, local=self.docs.local)
        if (st.get("rebuild") or {}).get("state") == "running":
            return {"started": False, "already_running": st["rebuild"], "message": st["message"]}
        if st["state"] == "complete" and not force and not st.get("rebuild"):
            return {"started": False, "up_to_date": True, "message": st["message"]}
        if dry_run:
            return {"would_start": True}
        # a build left behind (interrupted, finished but not swapped in, stale) is the builder's to finish or discard
        logs = Path(self.cfg.safety.audit_log).parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_file = logs / "infosys-build.log"
        lang = {"1033": "en", "1031": "de"}.get(self.docs.local.lcid, "en")
        cmd = [sys.executable, str(b / "infosys_local.py"), "index", "--db", str(target), "--mshc",
               str(self.docs.local.path), "--lang", lang, "--built-by", f"twincat-mcp {__version__}"]
        if force:
            cmd.append("--force")
        kw: dict[str, Any] = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000      # CREATE_NO_WINDOW
        else:
            kw["start_new_session"] = True
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, "ab") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {'first build' if first else 'rebuild'}: "
                     f"{' '.join(cmd)}\n".encode())
            fh.flush()
            self.infosys_build_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh,
                                                       stderr=subprocess.STDOUT, cwd=str(target.parent), **kw)
        log.info("infosys index: %s started (pid %s) → %s", "first build" if first else "rebuild",
                 self.infosys_build_proc.pid, target)
        return {"started": True, **_builder_pid(self.infosys_build_proc, IB.side_files(target)[1]), "index": str(target),
                "log": str(log_file),
                "why": "no index yet" if first else (st.get("reason") or st["state"]) if not force else "forced",
                "note": "the current index (if any) stays in use until the new one is complete; kb_status / tc_doctor "
                        "show the progress"}

    def spawn_manuals_update(self, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """manuals_db.py update for the knowledge folder in its own process, with this server's Python — when that
        Python can read PDFs (pypdf or PyMuPDF); otherwise the command for the user's Python. dry_run: every check,
        nothing started -> {'would_start': True} or why not (N96)."""
        import importlib.util
        import subprocess
        loc = self.kb.location()[0]
        b = K.builders_dir()
        if loc is None or b is None:
            return {"started": False, "error": "no knowledge folder chosen yet" if loc is None else
                    "the plugin's builders (skills/twincat-docs/builders) were not found"}
        kd = K.kb_docs_module()
        if kd is not None:
            from . import kb_layout as L
            st = kd.manuals_state(L.locate(loc, "manuals"), loc / "manuals")
            if (st.get("updating") or {}).get("state") == "running":
                return {"started": False, "already_running": st["updating"], "message": st["message"]}
            if st["state"] == "complete" and not force:
                return {"started": False, "up_to_date": True, "message": st["message"]}
        cmd = [sys.executable, str(b / "manuals_db.py"), "update", "--kb", str(loc)] + (["--force"] if force else [])
        if not any(importlib.util.find_spec(m) for m in ("pymupdf", "fitz", "pypdf")):
            return {"started": False, "error": f"this server's Python ({sys.executable}) has no PDF library (pypdf or "
                    "PyMuPDF) — run the update with your own Python 3.12 that has one",
                    "command": f'<Python 3.12+ with pypdf or PyMuPDF> "{loc / "tools" / "manuals_db.py"}" update --kb "{loc}"'}
        if dry_run:
            return {"would_start": True}
        logs = Path(self.cfg.safety.audit_log).parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_file = logs / "manuals-update.log"
        kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} if os.name == "nt" \
            else {"start_new_session": True}
        with open(log_file, "ab") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(cmd)}\n".encode())
            fh.flush()
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, cwd=str(loc), **kw)
        self.manuals_update_proc = p
        return {"started": True, "pid": p.pid, "log": str(log_file),
                "note": "the current manuals index stays in use until the update is complete; kb_status shows it"}

    def spawn_esi_build(self, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
        """esi_db.py update for the knowledge folder in its own process, with this server's Python (standard library
        only), from the knowledge folder's manuals\\ and TwinCAT's ESI folders plus knowledge.esi_dirs. dry_run:
        every check, nothing started -> {'would_start': True} or why not (N96)."""
        import subprocess
        from . import esi_index as E
        from . import kb_layout as L
        loc = self.kb.location()[0]
        b = K.builders_dir()
        if loc is None or b is None or not (b / "esi_db.py").is_file():
            return {"started": False, "error": "no knowledge folder chosen yet" if loc is None else
                    "the plugin's builder esi_db.py (skills/twincat-docs/builders) was not found"}
        st = self.kb.esi_state()
        if (st.get("rebuild") or {}).get("state") == "running":
            return {"started": False, "already_running": st["rebuild"], "message": st["message"]}
        if st["state"] == "complete" and not force and not st.get("rebuild"):
            return {"started": False, "up_to_date": True, "message": st["message"]}
        if st["state"] == "complete" and not force and (st.get("rebuild") or {}).get("state") == "stale":
            # a work file left next to the complete, current index: no rebuild — discarded (rule 7), nothing asked
            if dry_run:
                return {"started": False, "up_to_date": True, "stale_work_file": st["rebuild"].get("file"),
                        "message": st["message"]}
            try:
                r = E.build(L.index_file(loc, "esi"), loc, self.kb.esi_dirs, log=lambda m: None,
                            built_by="twincat-mcp server")
            except E.EsiError as exc:
                return {"started": False, "error": str(exc)}
            return {"started": False, "up_to_date": True, **({"discarded": r["discarded"]} if r.get("discarded") else {}),
                    "message": self.kb.esi_state()["message"]}
        folders = E.source_folders(loc, self.kb.esi_dirs, E.recorded_twincat(L.index_file(loc, "esi")))
        if not E.has_esi(E.xml_files(folders)):
            return {"started": False, "error": "no ESI files: no .xml whose root is an ESI document (EtherCATInfo, "
                    "EtherCATModule, EtherCATDict, EtherCATDiag) in " + (", ".join(str(f) for f, _s in folders)
                    or "the knowledge folder's manuals\\, and no TwinCAT ESI folder (Config\\Io\\EtherCAT) on this PC")
                    + " — copy the ESI files into manuals\\ or set knowledge.esi_dirs"}
        if dry_run:
            return {"would_start": True}
        dirs = [a for d in self.kb.esi_dirs for a in ("--esi-dir", str(d))] or ["--no-esi-dirs"]
        cmd = [sys.executable, str(b / "esi_db.py"), "update", "--kb", str(loc), *dirs] + (["--force"] if force else [])
        logs = Path(self.cfg.safety.audit_log).parent / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_file = logs / "esi-build.log"
        kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} if os.name == "nt" \
            else {"start_new_session": True}
        with open(log_file, "ab") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}: {' '.join(cmd)}\n".encode())
            fh.flush()
            p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, cwd=str(loc), **kw)
        self.esi_build_proc = p
        return {"started": True, **_builder_pid(p, E.side_files(L.index_file(loc, "esi"))[1]), "log": str(log_file),
                "index": str(L.index_file(loc, "esi")),
                "note": ("the first build of the ESI index" if st["state"] == "missing" else
                         "the current ESI index stays in use until the new one is complete") + "; kb_status shows it"}

    def esi_first_build(self) -> dict[str, Any] | None:
        """Rule 6: the first build of an ESI index that does not exist yet starts by itself (when an ESI source
        exists). An index that exists is left to the user's approval. None when there is nothing to do."""
        from . import esi_index as E
        from . import kb_layout as L
        loc = self.kb.location()[0]
        if loc is None or L.index_file(loc, "esi").exists() or \
                not E.has_esi(E.xml_files(E.source_folders(loc, self.kb.esi_dirs))):
            return None
        st = self.kb.esi_state()
        if st["state"] != "missing" or (st.get("rebuild") or {}).get("state") == "running":
            return None
        return self.spawn_esi_build()

    def manuals_first_build(self) -> dict[str, Any] | None:
        """Rule 6: the first build of a manuals index that does not exist yet starts by itself — when the knowledge
        folder has PDFs in manuals\\ and this server's Python can read them. An index that exists is left to the user's
        approval. None when there is nothing to do."""
        from . import kb_layout as L
        loc = self.kb.location()[0]
        if loc is None or not (loc / "manuals").is_dir() or L.locate(loc, "manuals").exists():
            return None
        if not any(p.suffix.lower() == ".pdf" for p in (loc / "manuals").rglob("*")):
            return None
        return self.spawn_manuals_update()

    def _builder_upkeep(self) -> None:
        try:
            first = self.manuals_first_build()
            if first:
                log.info("manuals index: first build %s", "started" if first.get("started") else first)
        except Exception as exc:  # noqa: BLE001 — a convenience; kb_status shows the state either way
            log.warning("manuals first build: %s", exc)
        try:
            first = self.esi_first_build()
            if first:
                log.info("ESI index: first build %s", "started" if first.get("started") else first)
        except Exception as exc:  # noqa: BLE001 — a convenience; kb_status shows the state either way
            log.warning("ESI first build: %s", exc)
        res = self.kb.builder_upkeep(__version__)
        for n, v in (res.get("tools") or {}).items():
            if isinstance(v, dict) and v.get("state") == "replaced":
                log.info("knowledge folder: tools\\%s (%s) replaced by the builder stub; the old file is %s", n, v["was"], v["backup"])
        if res.get("error"):
            log.warning("builder stub upkeep: %s", res["error"])

    def _seal_sweep(self) -> None:
        """N27: recordings that finished while no server was there to seal them get their manifest now."""
        try:
            r = self.rec.seal_unsealed()
            if r.get("sealed"):
                log.info("sealed %d recording(s) left unsealed by an earlier server: %s", len(r["sealed"]), r["sealed"][:10])
        except Exception as exc:  # noqa: BLE001
            log.warning("seal sweep failed: %s", exc)

    # --- runners -----------------------------------------------------------
    async def ads_call(self, fn, *a, timeout: float | None = None, slow: bool = False, bulk: bool = False,
                       timeout_msg: Any = None, **kw) -> Any:
        """Run on the quick ADS worker, on the slow one (``slow=True``) for anything that blocks for seconds — so a
        long watch never stalls an ads_read — or on the bulk worker (``bulk=True``: whole-PLC dumps) so a dump
        never stalls recordings and watches (R6)."""
        worker = self.ads_bulk_worker if bulk else self.ads_slow_worker if slow else self.ads_worker
        try:
            return await worker.run(fn, *a, timeout=timeout or (self.cfg.ads.max_watch_duration_ms / 1000 + 30), **kw)
        except (AdsError, RecorderError, TestError) as exc:
            raise ToolError(await self._ads_explain(str(exc), _call_port(fn, a, kw))) from exc
        except WorkerBusy as exc:
            raise ToolError(f"{exc} — the call was NOT started (nothing was read or written). Wait for it to finish, or "
                            "stop it (rec_stop / a smaller ads_dump), then retry.") from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            if callable(timeout_msg):
                # R42 #6: say what the call was doing — a long dump spends its time in Python, not on ADS
                raise ToolError(timeout_msg()) from exc
            raise ToolError("ADS call timed out after it started (router/route/firewall? run tc_doctor)") from exc
        except (pyads.ADSError, OSError) as exc:
            # safety net for a backend path that did not translate the pyads/socket error itself
            raise ToolError(await self._ads_explain(f"ADS error: {_fmt_ads_exc(exc)}", _call_port(fn, a, kw))) from exc

    async def _config_hint(self) -> str:
        """' — TwinCAT is in CONFIG …' when it is (R5), else ''."""
        try:
            mode = (await self.ads_worker.run(self.ads.system_state, timeout=5)).get("twincat_mode")
        except Exception:  # noqa: BLE001
            return ""
        return (" — TwinCAT is in CONFIG: the PLC runtime (and its symbol and datatype tables) is not running. "
                "ads_system_mode('run') or activate + restart, then retry") if mode == "CONFIG" else ""

    async def _ads_explain(self, msg: str, port: Any) -> str:
        """Known cause/fix for an ADS error; ADSError 6 is explained from the real port and TwinCAT mode (N17)."""
        if "datatype table unavailable" in msg:
            return K.annotate(msg + await self._config_hint())
        if re.search(r"ADSError 6 \(0x6\)", msg):
            p = port if isinstance(port, int) else self.ads.port
            mode = None
            if p != 10000:
                try:
                    mode = (await self.ads_worker.run(self.ads.system_state, timeout=5)).get("twincat_mode")
                except Exception:  # noqa: BLE001
                    mode = None
            k = K.ads6_context(p, mode if isinstance(mode, str) else None)
            return f"{msg} [cause: {k['cause']}. fix: {k['fix']}]"
        return K.annotate(msg)

    def _dialog_hint(self, retry: bool = True) -> str:
        """What is open in the shell right now — the usual reason a COM call is rejected or hangs. retry=False: the text
        before it withheld 'repeat it' (an earlier step of the call ran) — fourteenth review of 0.8.16: 'retry in a few
        seconds' followed it and invited a second activation."""
        from . import dialogs as _dlg
        try:
            live = [d.to_dict() for d in _dlg.list_dialogs(self.xae.pid)]
        except Exception:  # noqa: BLE001
            live = []
        pend = self.xae.watchdog.status().get("pending") or []
        seen = {d.get("hwnd") for d in live}
        live += [d for d in pend if d.get("hwnd") not in seen]
        if not live:
            return (" No dialog is visible in the shell (xae_dialogs); the shell may be mid-build or mid-login"
                    + (" — retry in a few seconds." if retry else "."))
        # N173: a modal window that is not a #32770 dialog has its class and, when unknown, says its buttons are unknown
        return " Open dialog(s): " + "; ".join(f"{_dlg.label(d)} [{_dlg.buttons_text(d)}] {str(d.get('text', ''))[:160]}"
                                            for d in live[:3]) + " — answer it with xae_dialog_resolve(button=...)."

    # --- XAE dialogs (N85 / N87) ----------------------------------------------
    def dialogs_now(self) -> list[dict[str, Any]]:
        """The shell's open dialogs right now (Windows; [] elsewhere or without a known shell process)."""
        try:
            from . import dialogs as _dlg
            return [d.to_dict() for d in _dlg.list_dialogs(self.xae.pid)]
        except Exception:  # noqa: BLE001
            return []

    def _held_check(self, job: str) -> None:
        held = self.xae_held
        if held is None:
            return
        if held["future"].done():
            self.xae_held = None
            return
        d = held["dialog"]
        from . import dialogs as _dlg
        raise _NotRun(f"{job} NOT started: XAE is held by the dialog {_dlg.label(d)} that {held['job']} opened "
                        f"{time.monotonic() - held['since']:.0f} s ago ({_dlg.buttons_text(d)}; text "
                        f"{str(d.get('text', ''))[:200]!r}). Answer it with xae_dialog_resolve(button=...) — the held "
                        "call's own result is then listed by xae_dialogs (late_results).")

    def _hold(self, job: str, dialog: dict[str, Any], fut: Any) -> None:
        self.xae_held = {"job": job, "dialog": dialog, "since": time.monotonic(),
                         "since_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "future": fut}

        def done(f: Any) -> None:            # worker thread: the dialog was answered and the call came back
            ent: dict[str, Any] = {"job": job, "dialog": dialog.get("title"),
                                   "returned_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
            if f.cancelled():
                ent["outcome"] = "cancelled"
            elif f.exception() is not None:
                ent["outcome"] = "error"
                ent["error"] = str(f.exception())[:500]
            else:
                ent["outcome"] = "ok"
                txt = json.dumps(f.result(), default=str)
                ent["result"] = json.loads(txt) if len(txt) <= 8000 else txt[:8000] + " …"
            self.xae_late.append(ent)
            del self.xae_late[:-20]
            if self.xae_held is not None and self.xae_held.get("future") is f:
                self.xae_held = None
        fut.add_done_callback(done)

    def refuse_if_dialog(self, action: str, done: str = "Nothing was done.") -> None:
        """N87: the shell answers COM inside a modal loop, so an open dialog does not stop a call — but nothing that
        changes the machine runs while one waits for an answer (a progress window, Cancel only, waits for none)."""
        live = [d for d in self.dialogs_now() if _dialog_awaits(d)]
        if live:
            d = live[0]
            from . import dialogs as _dlg
            raise ToolError(f"{action} refused: the XAE shell has a dialog open — {_dlg.label(d)} "
                            f"({_dlg.buttons_text(d)}; text {str(d.get('text', ''))[:200]!r}). Answer it first "
                            f"(xae_dialog_resolve(button=...)), then retry. {done}")

    def xae_flags(self) -> dict[str, Any]:
        """N116: what the result of a tool that used the XAE shell says about the binding — bound to another solution
        than xae.solution_path (every time), re-attached since the last such result (once)."""
        out: dict[str, Any] = {}
        note = getattr(self.xae, "mismatch_note", lambda: None)()
        if note:
            out.update(solution_mismatch=True, solution_note=note)
        again = getattr(self.xae, "reattach_note", None)
        if again:
            out["reattached"] = again
            self.xae.reattach_note = None
        return out

    async def xae_call(self, fn, *a, timeout: float | None = None, **kw) -> Any:
        _XAE_USED.set(True)
        job = getattr(fn, "__name__", "XAE call")
        self._held_check(job)
        before = {d.get("hwnd") for d in self.dialogs_now()}
        first_seen: dict[Any, float] = {}

        def watch(running_s: float) -> BaseException | None:
            # N85: a dialog the running call opened holds it inside the shell's modal loop; COM is never rejected, so
            # only the dialog says so. 2 s of it (rules auto-answer within 0.5 s; a progress window offers no answer)
            now = time.monotonic()
            live = [d for d in self.dialogs_now() if d.get("hwnd") not in before and _dialog_awaits(d)]
            for h in [h for h in first_seen if h not in {d.get("hwnd") for d in live}]:
                first_seen.pop(h)
            for d in live:
                t = first_seen.setdefault(d.get("hwnd"), now)
                if now - t >= 2.0 and running_s >= 2.0:
                    return _BlockedByDialog(job, d)
            return None
        box: dict[str, Any] = {"acted": []}

        def run(*a2: Any, **kw2: Any) -> Any:
            # the System Manager's messages of THIS call's actions (XaeBackend._sm_messages_of), taken in the worker
            # thread that ran them — review of 0.8.16: read later in the event loop, a call queued behind this one had
            # already emptied the list (its messages lost) or filled it (another call's messages in this result)
            be = self.xae
            keep = isinstance(getattr(be, "messages", None), list)
            if keep:
                be.messages = []
            if isinstance(getattr(be, "acted", None), list):
                be.acted = []
                box["acted"] = be.acted            # the actions that ran in this call (the worker fills it)
            if isinstance(getattr(be, "msg_reads", None), list):
                be.msg_reads = []
            if isinstance(getattr(be, "calls_ok", None), int):
                be.calls_ok = 0
            try:
                res = fn(*a2, **kw2)
            except XaeError as exc:
                # N181b: how many COM calls of this call ran before the error — taken here, in the worker that ran them
                exc.calls_ok = getattr(be, "calls_ok", None)
                earlier = [m for m in (be.messages if keep else []) if not m.get("failed") and m.get("messages")]
                if earlier:            # review of 0.8.16: what the call's earlier steps logged, not only the last one's
                    more = " — XAE messages of the steps before it: " + "; ".join(
                        f"{m['action']}{_msg_mark(m)}: {m['messages'][:500]}" for m in earlier)
                    new = copy.copy(exc)               # keeps what the error carries (XaeBusy.dialog, N181)
                    new.args = (f"{exc}{more}",)
                    new.steps_note = getattr(exc, "steps_note", "") + more
                    raise new from exc
                raise
            if keep and be.messages and isinstance(res, dict):
                res = {**res, "xae_messages": list(be.messages)}
            reads = [m for m in (getattr(be, "msg_reads", None) or []) if not m.get("read") or m.get("empty")]
            if reads and isinstance(res, dict):
                # N188 (0.8.16 retest): an action whose buffer gave nothing says so — read and empty, or not read
                res = {**res, "xae_messages_note": "; ".join(
                    f"{m['action']}: " + ("XAE's message buffer was read — empty" if m.get("read") else
                                         f"XAE's message buffer was not read ({m.get('why')})") for m in reads)}
            box["calls_ok"] = getattr(be, "calls_ok", None)
            return res
        run.__name__ = job                 # the worker names the job (busy / timeout messages) by it
        try:
            res = await self.xae_worker.run_watched(run, *a, timeout=timeout or self.cfg.xae.build_timeout_s + 60,
                                                    watch=watch, **kw)
        except _BlockedByDialog as b:
            self._hold(job, b.dialog, b.job_future)  # type: ignore[attr-defined]
            d = b.dialog
            from . import dialogs as _dlg
            # N173: the window's class, and buttons_note when its buttons could not be read (then: close it in XAE)
            answer = ("Answer with xae_dialog_resolve(button=...)" if d.get("buttons") is not None else
                      "Its buttons can't be pressed by the tool: close it in XAE")
            raise _Pending(f"{job}: BLOCKED BY DIALOG {_dlg.label(d)} — the shell opened it during this call and "
                            f"waits for an answer ({_dlg.buttons_text(d)}; text {str(d.get('text', ''))[:300]!r}). "
                            f"The call is still pending inside XAE: what it does depends on the answer. {answer}; the "
                            "call's own result is then listed by xae_dialogs (late_results). Until then every XAE call "
                            "is refused at once. blocked_by_dialog: "
                            + json.dumps(_dialog_fields(d), default=str)) from None
        except XaeError as exc:
            msg = str(exc)
            low = msg.lower()
            if isinstance(exc, XaeBusy) and exc.dialog is not None:
                # N181 (0.8.15 retest): the backend named the dialog; what the refusal means depends on this tool.
                # N181b (0.8.16 retest): refused at the first COM call of the tool — nothing of it ran
                first = getattr(exc, "calls_ok", None) == 0 and not any(
                    st.get("acted") or (st.get("calls_ok") or 0) > 0 for st in (_XAE_STEPS.get() or []))
                msg = (XaeBusy.blocked_text(exc.what, exc.dialog, _READ_ONLY_CALL.get(), nothing_ran=first)
                       + getattr(exc, "steps_note", ""))
            elif "blocked by dialog" in low:
                pass                                   # the backend already named the dialog
            elif isinstance(exc, XaeBusy):
                ran = [x for st in (_XAE_STEPS.get() or []) for x in st.get("acted") or []]
                if ran and exc.busy:
                    # twelfth review of 0.8.16: earlier XAE calls of this tool ran actions — no 'repeat it'
                    msg = (XaeBusy.busy_text(exc.what, exc.busy[0], exc.busy[1], ran + list(box["acted"]))
                           + getattr(exc, "steps_note", ""))
                # N174: it says busy itself; what is open — and no 'retry' after a text that withheld 'repeat it'
                msg += await asyncio.to_thread(self._dialog_hint, "earlier step(s) ran" not in msg)
            elif ("0x80010001" in low or "0x8001010a" in low or "-2147418111" in low or "-2147417846" in low
                    or "call was rejected" in low or "retry later" in low):
                msg += " — XAE refused the COM call: it is busy or a modal dialog has the shell." + await asyncio.to_thread(self._dialog_hint)
            if isinstance(exc, XaeActedUnknown):
                raise _Pending(msg) from exc           # review 1 of 0.8.18: it may have run — never 'did not run'
            if isinstance(exc, XaeExplained):
                raise ToolError(msg) from exc          # it carries its own cause and what to do
            lic = _license_hint(msg)
            if lic:
                msg += " " + lic
            # fourteenth review of 0.8.16: a call the shell refused did not run — the one refusal that may be repeated
            raise (_NotRun if isinstance(exc, XaeBusy) else ToolError)(K.annotate(msg)) from exc
        except WorkerBusy as exc:
            raise _NotRun(f"{exc} — the XAE call was NOT started; the shell is still busy with the earlier call "
                          "(a build, an activation or a modal dialog — xae_dialogs).") from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise _Pending(f"{job}: XAE call timed out — "
                           + await asyncio.to_thread(self._timeout_cause, _READ_ONLY_CALL.get())) from exc
        steps = _XAE_STEPS.get()
        if steps is not None:
            steps.append({"job": job, "acted": list(box["acted"]), "calls_ok": box.get("calls_ok"),
                          "messages": [m for m in (res.get("xae_messages") or []) if isinstance(m, dict)]
                          if isinstance(res, dict) else []})
        if isinstance(res, dict):
            live = self.dialogs_now()
            if live:                       # N87: the call ran, but the shell has a dialog waiting — say so
                res = {**res, "open_dialogs": [_dialog_fields(d) for d in live[:5]]}
        return res              # N116: the binding's flags are added to the tool's own result (_tool_boundary)

    def _timeout_cause(self, read_only: bool = True) -> str:
        """N173 (0.8.14 retest): a timed-out call said 'a modal dialog in the shell is the usual cause' from the
        watchdog's last list, which never had a non-#32770 window. The shell is looked at again now: the modal window by
        name, a main window that is disabled with no window to name, or no modal window at all."""
        from . import dialogs as _dlg
        if self.xae.pid is None:
            return ("no XAE process is known, so its windows could not be looked at — xae_status; a modal window in the "
                    "shell is the usual cause")
        live = self.dialogs_now()
        if not live:
            # thirteenth review of 0.8.16: 'then retry' after an action that is still running queued it a second time
            return ("no modal window is open in XAE now (its main window is not disabled): the call is still running "
                    "inside the shell (a long build, activation or login?) — xae_status / xae_dialogs, then "
                    + ("retry" if read_only else "check its result when it is done; do not repeat it before (it would "
                       "run a second time)"))
        return ("open in XAE now: " + "; ".join(f"{_dlg.label(d)} ({_dlg.buttons_text(d)}) "
                                                f"{str(d.get('text', ''))[:160]!r}" for d in live[:3])
                + (" — answer it with xae_dialog_resolve(button=...)" if any(d.get("buttons") is not None for d in live)
                   else " — the tool cannot press its buttons: close it in XAE")
                + "; blocked_by_dialog: " + json.dumps(_dialog_fields(live[0]), default=str))

    def guard_safety_path(self, path: str, action: str) -> None:
        """TwinSAFE (TISC / Safety) projects are never edited unless explicitly enabled."""
        low = (path or "").lower()
        if (low.startswith("tisc") or "^safety" in low or "twinsafe" in low or low.endswith(".sal")) and not self.cfg.xae.allow_safety:
            raise ToolError(f"{action}: {path!r} is under the TwinSAFE/safety project; refused "
                            "(set TCMCP_ALLOW_SAFETY=1 to permit, and know what you are doing)")

    async def proj_call(self, fn, *a, **kw) -> Any:
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except ProjectError as exc:
            raise ToolError(K.annotate(str(exc))) from exc

    # -- N113: the source analyses run inside ONE PLC project when names are declared in several ------------------
    def proj_views(self, name: str | None, shared: bool = False) -> list[ProjectIndex]:
        """The index itself when no name is declared twice; else the PLC project that declares `name` (several and no
        '<PLC project>@name' pin: refused, nothing is picked), or — no name — each PLC project in turn. shared: a name
        whose one file several PLC projects compile gives each of them (N142, the caller compares the results)."""
        p = self.proj
        p._ensure()
        # multi-PLC audit (0.8.13): split only when names were declared twice — with two PLC projects a lint of the whole
        # tree compared one project's declarations with the other's code. Several PLC projects: each in turn, always
        if not p.duplicates and len(p.plcproj) <= 1:
            return [p]
        if name:
            plcs, files = declaring(p, name)
            if len(plcs) > 1:
                if shared and len(files) == 1:
                    return [PlcView(p, plc) for plc in plcs]
                names = [p.plc_label(x) for x in plcs]
                raise ToolError(f"{name!r} is " + (f"one file ({files[0]}) compiled by several PLC projects {names}"
                                                   if len(files) == 1 else f"declared in several PLC projects {names}")
                                + f" — pin one: '<PLC project>@{name.split('@')[-1]}' (nothing was chosen)")
            return [PlcView(p, plcs[0])] if plcs else [p]       # not declared: the analysis says so itself
        return [PlcView(p, plc) for plc in sorted({p._plc_of(f) for f in p._by_file}, key=lambda x: x or "")]

    def proj_for_port(self, port: int | None = None) -> ProjectIndex:
        """The source of the runtime on ADS `port` (default: the connected one): its PLC project when names are
        declared in several and the .tsproj maps the port; else the whole index (its lookups refuse ambiguous names)."""
        p = self.proj
        if p.root is None:
            return p
        p._ensure()
        if not p.duplicates and len(p.plcproj) <= 1:
            return p
        plc = p.plc_project(port=port or self.ads.port)
        return PlcView(p, plc) if plc is not None else p

    def proj_view_map(self) -> Any:
        """A function file -> the view of its PLC project, the views built once (in the worker thread that calls this)."""
        p = self.proj
        p._ensure()
        if not p.duplicates and len(p.plcproj) <= 1:
            return lambda _f: p
        views: dict[str | None, ProjectIndex] = {}

        def of(f: str) -> ProjectIndex:
            plc = p.plc_of_file(f)
            if plc not in views:
                views[plc] = PlcView(p, plc)
            return views[plc]
        for f in list(p._by_file):
            of(f)
        return of

    async def proj_check(self, fn, name: str | None, *a: Any) -> dict[str, Any]:
        """A codecheck analysis fn(index, name, *a) on the views of proj_views: one result, or the results of each PLC
        project merged (counts added, lists joined with plc_project on each item)."""
        views = await self.proj_call(self.proj_views, name, True)
        if len(views) == 1:
            r = await self.proj_call(fn, views[0], name, *a)
            if isinstance(views[0], PlcView) and isinstance(r, dict):
                r["plc_project"] = views[0].plc
            return r
        outs = [(v.plc, await self.proj_call(fn, v, name, *a)) for v in views]

        def core(r: Any) -> Any:              # what the analysis says, not how many files its PLC project has
            return {k: v for k, v in r.items() if k != "files_indexed"} if isinstance(r, dict) else r
        if name and isinstance(outs[0][1], dict) and all(core(r) == core(outs[0][1]) for _plc, r in outs[1:]):
            # N142 (0.8.14 retest): one file several PLC projects compile — the same answer in each is given once
            out = {**outs[0][1], "plc_projects": [plc for plc, _r in outs],
                   "note_plc_projects": f"one file compiled by {len(outs)} PLC projects — the same result in each"}
            if "files_indexed" in out:
                out["files_indexed"] = {plc or "": r.get("files_indexed") for plc, r in outs}
            return out
        return _merge_checks(outs)

    async def kb_call(self, fn, *a, timeout: float = 600, **kw) -> Any:
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *a, **kw), timeout)
        except K.KnowledgeError as exc:
            raise ToolError(str(exc)) from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ToolError(f"knowledge base call timed out after {timeout:.0f} s (still running in the background)") from exc
        except sqlite3.Error as exc:
            # R37: never an empty "Error executing tool" — say what the database said
            raise ToolError(f"knowledge database error ({type(exc).__name__}): {str(exc) or 'no detail'}") from exc

    async def rec_call(self, fn, *a, timeout: float = 60, **kw) -> Any:
        return await self.ads_call(fn, *a, timeout=timeout, slow=True, **kw)

    def docs_index_status(self) -> dict[str, Any]:
        if self.docs is not None and self.docs.local is not None and self.docs_build_target is not None:
            # the local package's index: its real state (a build runs in its own process, also across restarts)
            from . import infosys_build as IB
            st = IB.state(self.docs_build_target, local=self.docs.local)
            p = self.infosys_build_proc
            if p is not None:
                rc = p.poll()
                # S4 (0.8.14 retest): the process this server started — on Windows a venv launcher whose child builds
                # (rebuild.pid, from the build's lock); named so, not a second 'pid'
                st["started_here"] = {"process_pid": p.pid, **({"exit_code": rc} if rc is not None else {"running": True})}
            return st
        st = dict(self.docs_bg)
        if st.get("started") and not st.get("finished"):
            st["running_for_s"] = round(time.time() - st["started"], 0)
            if st.get("last_progress"):
                # D25: progress inside a book too — a number that stops growing is a real stall
                st["seconds_since_progress"] = round(time.time() - st["last_progress"], 0)
        st.pop("last_progress", None)
        errs = [r for r in (st.get("result") or []) if isinstance(r, dict) and r.get("error")]
        if errs:
            st["errors"] = errs          # N21: a book that fails to index is reported, not silently retried
        return st

    def project_library_books(self) -> list[str]:
        """Infosys books for the Beckhoff libraries the PLC projects reference (K1): Tc2_Smtp → tcplclib_tc2_smtp."""
        try:
            if self.proj.root is not None:
                self.proj._ensure()
        except Exception:  # noqa: BLE001
            return []
        libs = set()
        for pp in self.proj.plcproj:
            for lib in pp.get("libraries") or []:
                name = str(lib or "").split(",")[0].strip()
                if re.fullmatch(r"Tc[23]_\w+", name, re.I):
                    libs.add("tcplclib_" + name.lower())
        return sorted(libs)

    def start_docs_index_bg(self) -> None:
        """First documentation lookup: index, in a daemon thread, the configured default books (fresh cache) and
        every Beckhoff library book the project references that is not indexed yet — the first call does not wait."""
        if self.docs is None or self.docs_bg.get("started"):
            return
        if self.docs.local is not None:
            return                    # the local package: one index built as a whole (infosys_upkeep, kb_rebuild)
        try:
            have = set(self.docs.indexed_books())
        except Exception:  # noqa: BLE001
            return
        todo = [] if have else list(self.cfg.docs.books)
        todo += [b for b in self.project_library_books() if b not in have and b not in todo]
        if not todo:
            return
        self.docs_bg = {"started": time.time(), "finished": None, "books": todo, "result": None}

        def _run() -> None:
            out = []
            try:
                for b in todo:
                    try:
                        out.append(self.docs.index_book(b))
                    except Exception as exc:  # noqa: BLE001 — unknown slug for a third-party-looking name: report, go on
                        out.append({"book": b, "error": str(exc)})
            finally:
                self.docs_bg["result"] = out
                self.docs_bg["finished"] = time.time()

        threading.Thread(target=_run, name="infosys-default-index", daemon=True).start()

    async def docs_call(self, fn, *a, timeout: float = 120, **kw) -> Any:
        """Infosys lookups: local package (or network) + SQLite, off the event loop; no ADS/COM worker involved."""
        if self.docs is None:
            raise ToolError(self.docs_error or "Infosys lookup is disabled (docs.enabled=false / TCMCP_DOCS=0)")
        if isinstance(fn, str):
            fn = getattr(self.docs, fn)
        self.start_docs_index_bg()
        try:
            res = await asyncio.wait_for(asyncio.to_thread(fn, *a, **kw), timeout)
        except InfosysError as exc:
            raise ToolError(str(exc) or f"{type(exc).__name__} (no message)") from exc
        except sqlite3.OperationalError as exc:
            if self.docs.local is not None:
                raise ToolError(f"Infosys index not readable ({exc}) — {self.docs_index_status().get('message')}") from exc
            st = self.docs_index_status()
            busy = st.get("started") and not st.get("finished")
            raise ToolError(f"Infosys index busy ({exc})" + (f" — {', '.join(st.get('books') or [])} being indexed in the background "
                            f"({st.get('running_for_s', 0):.0f} s so far); retry in a moment or search with web_fallback=true" if busy else "")) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ToolError(f"Infosys call exceeded {timeout:.0f} s" + (" (slow network? smaller max_pages?)"
                            if self.docs.local is None else "")) from exc
        except Exception as exc:  # noqa: BLE001 — never an empty error message to the client
            raise ToolError(f"Infosys lookup failed: {type(exc).__name__}: {exc}") from exc
        st = self.docs_bg
        if isinstance(res, dict) and st.get("started") and not st.get("finished"):
            books = ", ".join(st.get("books") or [])
            msg = f"indexing in the background: {books} ({round(time.time() - st['started'])} s so far"
            if st.get("total_books"):
                msg += f", {st.get('done_books')}/{st['total_books']} books"
            msg += ")"
            backend = str(res.get("backend") or "")
            if backend and "web" in backend.lower():
                msg += "; this answer came from the web fallback"
            elif backend:
                msg += f"; this answer came from the local index ({backend}) — results from those books follow once indexed"
            res["index_status"] = msg
        return res

    async def mem_call(self, fn, *a, **kw) -> Any:
        """Recorder queries that touch only memory/files — no ADS worker needed."""
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except (RecorderError, AdsError, TestError) as exc:
            raise ToolError(str(exc)) from exc
        except OSError as exc:            # a file the OS refuses (read-only, in use): the reason, never an empty error (D7)
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    def scope(self, action: str = "") -> str:
        """The machine a confirmation is bound to: the ADS target '<NetId>:<port>'; for an xae_* action the machine
        alone — the PLC project (and so its port) is in the token's target (N42, 0.8.13 retest: a token for Plc2 on
        port 852 said ':851', the ADS connection's port)."""
        if action.startswith("xae_"):
            return self.ads.net_id or "local"
        return self.ads.target_label()

    def write_check(self, symbol: str) -> str | None:
        """Would ads_write to this symbol be allowed right now, without a confirmation? Reason or None."""
        try:
            self.guard.check(OpClass.WRITE, action="exp_run", target=symbol, confirm_token=None, scope=self.scope())
            return None
        except SafetyError as exc:
            return str(exc)

    def exp_deps(self) -> "X.RunnerDeps":
        return X.RunnerDeps(
            call=lambda fn, *a, timeout=None, **kw: self.ads_worker.call(fn, *a, timeout=timeout, **kw),
            slow_call=lambda fn, *a, timeout=None, **kw: self.ads_slow_worker.call(fn, *a, timeout=timeout, **kw),
            ads=self.ads, rec=self.rec, bench=self.bench, profiles=self.profiles, write_check=self.write_check,
            audit=lambda tool, args, outcome: self.guard.audit(tool, args, OpClass.WRITE, outcome),
            scope=self.scope, log=log.warning)

    def stop_experiments(self, reason: str = "server shutdown", join_s: float = 20.0) -> None:
        """Abort every live runner and wait (bounded) for its restore path — registered with atexit so a
        client disconnect does not leave factors at the last level."""
        live = [r for r in self.runners.values() if r.status().get("alive")]
        for r in live:
            r.abort(reason)
        for r in live:
            r.join(join_s)

    async def restart_witness(self, mode: str | None = None) -> dict[str, Any]:
        """R40: taken BEFORE a restart command (and again at each RUN poll after it, with the mode already known).
        The System Service mode, and — when TwinCAT runs — what a runtime restart changes: task 1's cycle counter
        (starts again from 0 — below its earlier value proves a restart; × cycle time ≥ the time since this witness
        proves none) and the application's compile timestamp. Unreadable values stay None."""
        w: dict[str, Any] = {"t": time.monotonic(), "mode": mode, "cycle_count": None, "cycle_time_100ns": None,
                             "app_timestamp": None}
        if mode is None:
            try:
                st = await self.ads_call(self.ads.system_state, timeout=10)
                w["mode"] = str(st.get("twincat_mode") or st.get("ads_state_name") or "unknown")
            except ToolError:
                w["mode"] = "unreachable"
        if w["mode"] == "RUN":
            try:
                vals = await self.ads_call(self.ads.read_many, list(_RESTART_WITNESS), timeout=10)
            except ToolError as exc:
                vals = {}
                w["unreadable"] = {"all": str(exc)[:200]}
            if isinstance(vals, dict):
                for key, sym in zip(("cycle_count", "cycle_time_100ns", "app_timestamp"), _RESTART_WITNESS):
                    v = vals.get(sym)
                    if isinstance(v, dict) and "raw" in v:
                        v = v["raw"]                # DT (AppTimestamp): the decoded value carries the number it compares
                    if isinstance(v, (int, float, str)) and not isinstance(v, bool):
                        w[key] = v
                    elif isinstance(v, dict) and "error" in v:
                        w.setdefault("unreadable", {})[key] = str(v["error"])[:200]
        return w

    async def verify_target_after(self, action: str, expect_run: bool, timeout_s: float = 90.0,
                                  require_transition: bool = False, leave_timeout_s: float = 20.0,
                                  poll_s: float = 2.0, before: dict[str, Any] | None = None) -> dict[str, Any]:
        """After ActivateConfiguration / StartRestartTwinCAT the Automation Interface reports success as soon as
        the command was accepted; whether the target actually came up is a separate question (a missing
        license drops it back to CONFIG with a dialog in the shell). Poll the target's System Service and
        the shell's dialogs and report what really happened.

        ``require_transition`` (#4): a restart must be proven, not assumed from the first RUN poll (the old runtime's
        RUN before the restart had begun). R40: polling starts after the commands returned, so a restart that was
        already over, or a start in CONFIG (the activation itself brings TwinCAT to RUN), was never *seen* leaving
        RUN — a false failure. Proven by any of: ``poll`` (a poll saw another state or no answer), ``mode_change``
        (``before`` — the witness taken before the commands — was not RUN, now it is), ``witness`` (task 1's cycle
        counter restarted, or the application timestamp changed, compared with ``before``). The first phase polls
        every 0.5 s for ``leave_timeout_s``."""
        from . import dialogs as _dlg
        t0 = time.time()
        deadline = t0 + timeout_s
        leave_deadline = t0 + leave_timeout_s
        mode: str = "unknown"
        err_dialogs: list[dict[str, Any]] = []
        polls = 0
        left_run = not require_transition
        left_at: float | None = None
        transition_by: str | None = None
        witness_after: dict[str, Any] | None = None
        witness_verdict: tuple[bool | None, str] | None = None
        modes_seen: list[str] = []
        run_seen: float | None = None
        while True:
            polls += 1
            try:
                st = await self.ads_call(self.ads.system_state, timeout=10)
                mode = str(st.get("twincat_mode") or st.get("ads_state_name") or "unknown")
            except ToolError as exc:
                mode = f"unreachable ({str(exc)[:80]})"
            short = mode.split(" ", 1)[0]
            if not modes_seen or modes_seen[-1] != short:
                modes_seen.append(short)
            if mode != "RUN" and not left_run:
                left_run, left_at, transition_by = True, time.time(), "poll"
            elif mode == "RUN" and not left_run and before is not None:
                if before.get("mode") != "RUN":
                    left_run, left_at, transition_by = True, time.time(), "mode_change"
                else:
                    witness_after = await self.restart_witness(mode)
                    witness_verdict = restart_proven(before, witness_after)
                    if witness_verdict[0]:
                        left_run, left_at, transition_by = True, time.time(), "witness"
            try:
                dl = await asyncio.to_thread(lambda: [d.to_dict() for d in _dlg.list_dialogs(self.xae.pid)])
            except Exception:  # noqa: BLE001
                dl = []
            err_dialogs = [d for d in dl if any(k in (str(d.get("title", "")) + " " + str(d.get("text", ""))).lower()
                                                 for k in ("error", "license", "licence", "fatal", "failed"))]
            reached = (mode == "RUN") if expect_run else (mode in ("RUN", "CONFIG"))
            if left_run and reached and run_seen is None:
                run_seen = time.monotonic()           # N5: the poll that saw it running again (after a transition)
            if err_dialogs or (left_run and reached):
                break
            now = time.time()
            if now >= deadline or (not left_run and now >= leave_deadline):
                break
            await asyncio.sleep(0.5 if not left_run else poll_s)
        if (require_transition and transition_by == "poll" and mode == "RUN" and before is not None
                and before.get("mode") == "RUN"):
            # N75: the witness compared is the one of the runtime that came back — taken now, in RUN after the
            # transition — never an attempt from a poll while the old runtime was going down. A counter that ran on
            # (provably no restart) outweighs one poll without an answer
            witness_after = await self.restart_witness(mode)
            witness_verdict = restart_proven(before, witness_after)
            if witness_verdict[0] is False:
                left_run, left_at, transition_by = False, None, None
        ok = ((mode == "RUN") if expect_run else (mode in ("RUN", "CONFIG"))) and left_run and not err_dialogs
        out: dict[str, Any] = {"target_mode": mode, "verified": ok, "polls": polls, "dialogs": err_dialogs,
                               "modes_seen": modes_seen, "verify_s": round(time.time() - t0, 1),
                               "_run_seen_mono": run_seen if ok else None, "_poll_s": poll_s}
        if require_transition:
            out["transition_seen"] = left_run
            out["transition_by"] = transition_by
            if left_at is not None:
                out["left_run_after_s"] = round(left_at - t0, 1)
            if before is not None:
                out["mode_before"] = before.get("mode")
                out["witness_before"] = {k: v for k, v in before.items() if k != "t"}
                if witness_after is not None:
                    out["witness_after"] = {k: v for k, v in witness_after.items() if k != "t"}
                if witness_verdict is not None:
                    out["witness"] = witness_verdict[1]
        hints = [h for h in (_license_hint(json.dumps(err_dialogs)),) if h]
        if require_transition and not left_run and not err_dialogs and mode == "RUN":
            # in RUN, but no restart proven — never phrased as "not in RUN" (R40)
            if witness_verdict is not None and witness_verdict[0] is False:
                hints.append(f"TwinCAT was in RUN before and after the command; {witness_verdict[1]} — the activation "
                             "did not restart the runtime. Check the XAE Output / Error List.")
            else:
                why = (witness_verdict[1] if witness_verdict is not None else "no witness was taken before the command")
                hints.append(f"TwinCAT is in RUN, but a restart could not be proven: no other state was seen in "
                             f"{leave_timeout_s:.0f} s and {why}. Check the XAE Output / Error List and ads_status before "
                             "relying on the new configuration.")
        elif expect_run and mode != "RUN" and not err_dialogs:
            hints.append(f"target did not reach RUN within {timeout_s:.0f} s — xae_dialogs for a prompt in the shell; "
                         "ads_status for the runtime; the XAE Error List for 'Target system reports a fatal error'.")
        if hints:
            out["hint"] = " ".join(hints)
        return out

    def output_roots(self) -> list[Path]:
        roots: list[Path] = []
        for r in ([Path(self.cfg.safety.audit_log).parent if self.cfg.safety.audit_log else None, self.rec.root,
                   Path(self.cfg.project.root) if self.cfg.project.root else None,
                   Path(self.cfg.xae.solution_path).parent if self.cfg.xae.solution_path else None,
                   Path(self.cfg.scope.export_dir) if self.cfg.scope.export_dir else None,
                   Path(self.cfg.project.experiments_dir) if getattr(self.cfg.project, "experiments_dir", None) else None]
                  + [Path(d) for d in (self.cfg.server.output_dirs or [])]):
            if r is None:
                continue
            try:
                rr = r.expanduser().resolve()
            except OSError:
                continue
            if rr not in roots:
                roots.append(rr)
        return roots

    def out_path(self, path: str, overwrite: bool = False, what: str = "file", is_dir: bool = False) -> Path:
        """R4: a caller-chosen output path must lie in a folder this server writes to (plugin data, recordings,
        project, solution, [server] output_dirs) and must not silently replace an existing file. N98: a folder that
        does not exist yet (inside those) is created — the folder itself for is_dir, else the file's folder — so
        every writer can write there (rec_export into a new folder raised FileNotFoundError of its temp file)."""
        p = Path(path).expanduser()
        try:
            rp = p.resolve()
        except OSError as exc:
            raise ToolError(f"{what} path {path!r} cannot be resolved: {exc}") from exc
        roots = self.output_roots()
        if not any(rp == r or r in rp.parents for r in roots):
            raise ToolError(f"{what} path {str(rp)!r} is outside the folders this server writes to: "
                            f"{[str(r) for r in roots]}. Write there, or add the folder to [server] output_dirs.")
        if not is_dir and rp.exists() and not overwrite:
            raise ToolError(f"{str(rp)!r} exists — pass overwrite=true to replace it (nothing was written)")
        folder = rp if is_dir else rp.parent
        if folder.exists() and not folder.is_dir():
            raise ToolError(f"{what} path {str(rp)!r}: {str(folder)!r} is a file, not a folder (nothing was written)")
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(f"{what} path {str(rp)!r}: its folder could not be created ({exc}) — nothing was written") from exc
        return rp

    def drop_edits_under(self, path: str) -> list[str]:
        """N124: forget the pending shell edits of an object and everything below it (it was deleted)."""
        low = path.lower()
        gone = [p for p in self.pending_edits if p.lower() == low or p.lower().startswith(low + "^")]
        for p in gone:
            self.pending_edits.pop(p, None)
        return sorted(gone)

    def note_edit(self, path: str, declaration: str | None, implementation: str | None) -> None:
        cur = self.pending_edits.setdefault(path, {"path": path, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        if declaration is not None:
            cur["declaration"] = declaration
        if implementation is not None:
            cur["implementation"] = implementation
        cur["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    def _edits_not_on_disk(self) -> list[dict[str, Any]]:
        """Pending shell edits whose text is not (yet) in the source files."""
        def norm(t: str | None) -> str:
            return re.sub(r"\s+", " ", (t or "").replace("\r", "")).strip()
        self.proj.ensure_fresh()
        bad: list[dict[str, Any]] = []
        for path, e in self.pending_edits.items():
            # N113: looked up in the PLC project the tree path names — by name alone, MAIN of Plc2 was compared with
            # MAIN of Plc1 and every build refused 'declaration on disk differs'
            u, why = self.proj.unit_at_tree_path(path)
            if u is None:
                bad.append({"path": path, "edited_at": e["at"], "why": why})
                continue
            for part in ("declaration", "implementation"):
                if part in e and norm(e[part]) != norm(getattr(u, part)):
                    bad.append({"path": path, "edited_at": e["at"], "file": u.file,
                                "why": f"{part} on disk differs from the text set in the shell"})
                    break
        return bad

    async def save_verified(self, tool: str) -> dict[str, Any]:
        """D44: File.SaveAll, unsaved documents/projects saved one by one, then every edit made through this server
        (xae_pou_set / xae_pou_create) checked in the source files. A build/deploy/test on unsaved text is refused:
        an external reload of the project discards what is only in the shell's memory."""
        r = await self.xae_call(self.xae.save_all, timeout=120)
        out: dict[str, Any] = dict(r)
        if not self.pending_edits:
            out["edits_checked"] = 0
            return out
        if self.proj.root is None:
            out["edits_checked"] = 0
            out["edits_on_disk"] = ("not checked: no project source (project.root / xae.solution_path) — "
                                    f"{len(self.pending_edits)} shell edit(s) unverified")
            return out
        bad = await self.proj_call(self._edits_not_on_disk)
        if bad:
            await asyncio.sleep(1.5)                       # the shell may still be writing
            r = await self.xae_call(self.xae.save_all, timeout=120)
            out.update(r)
            bad = await self.proj_call(self._edits_not_on_disk)
        # N124 (0.8.13 retest, S2): an edit of an object deleted since (in XAE, or its file) can never reach a source
        # file — it blocked every save_first build until the server restarted. The shell is asked whether the object
        # still exists; one that is gone has its edit dropped and said (unknown — shell busy — stays a refusal)
        dropped = []
        for b in [b for b in bad if "file" not in b]:
            if await self.xae_call(self.xae.item_exists, b["path"], timeout=60) is False:
                self.pending_edits.pop(b["path"], None)
                dropped.append({"path": b["path"], "edited_at": b["edited_at"],
                                "why": "the object no longer exists in the shell (deleted) — its edit cannot be saved"})
        if dropped:
            out["edits_dropped"] = dropped
            bad = [b for b in bad if b["path"] in self.pending_edits]
        out["edits_checked"] = len(self.pending_edits) + len(dropped)
        if bad:
            out["edits_not_on_disk"] = bad
            raise ToolError(f"{tool}: File.SaveAll ran but {len(bad)} edit(s) made in the shell are not in the source files "
                            f"({'; '.join(b['path'] + ': ' + b['why'] for b in bad[:5])}). Nothing was built — an external "
                            "reload would discard them. Save in XAE (File > Save All) or xae_command('File.SaveAll') and "
                            f"retry. Still unsaved in the shell: {out.get('dirty_after') or 'none reported'}.")
        out["edits_on_disk"] = sorted(self.pending_edits)
        self.pending_edits.clear()
        return out

    def source_alias(self, type_name: str, port: int | None = None) -> str | None:
        """N50: the base the project source declares for an alias type (TYPE PT_X : POINTER TO FB_X), or None. port:
        the runtime's — a name declared in several PLC projects is that port's PLC project's (N113)."""
        if self.proj.root is None:
            return None
        try:
            return self.proj.type_alias(type_name, port or self.ads.port)
        except Exception:  # noqa: BLE001
            return None

    def is_source_interface(self, type_name: str, port: int | None = None) -> bool:
        """The project declares `type_name` (qualified or not) as an INTERFACE (in the PLC project of ADS `port`)."""
        if self.proj.root is None:
            return False
        try:
            self.proj._ensure()
        except Exception:  # noqa: BLE001
            return False
        base = self.proj._base_type_name(type_name or "").split(".")[-1]
        u = self.proj.named(base, port or self.ads.port)
        return bool(u is not None and u.kind == "INTERFACE")

    def constant_reason(self, name: str, port: int | None = None) -> str | None:
        """Source-level write check, installed as AdsBackend.write_guard so every write path runs it.
        Runtime symbol flags do not mark VAR CONSTANT as READONLY on 4026, so the project source decides: a write
        to anything declared in a CONSTANT block is refused. Without a project source the check cannot run: in
        guarded mode that refuses the write, in full mode it is allowed (decision 2026-10-17)."""
        if self.proj.root is None:
            if self.guard.mode == "guarded":
                return (f"{name!r}: no project source is loaded (project.root / xae.solution_path), so a write to a "
                        "CONSTANT cannot be ruled out — guarded mode refuses writes without it. Set the project root "
                        "(tc_reload_config) or switch to full mode.")
            return None
        try:
            self.proj._ensure()
            r = self.proj.resolve_symbol(name, port or self.ads.port)
        except ProjectError as exc:
            if "several PLC projects" in str(exc) and self.guard.mode == "guarded":
                # N113: the name is in the source twice and which one the target runs is not known — the CONSTANT
                # check cannot be made; guarded mode refuses (as without a project source)
                return (f"{name!r}: {exc}. A write to a CONSTANT cannot be ruled out — guarded mode refuses it. Name "
                        "the PLC projects' ADS ports in the .tsproj (AmsPort), or switch to full mode.")
            if "several PLC projects" in str(exc):
                # full mode (0.8.13 retest, S3): the check was skipped silently. It is made in every PLC project that
                # declares the name: a CONSTANT in any of them refuses the write (which one the target runs is unknown)
                return self._constant_in_any_plc(name)
            return None                                 # unknown in source: the runtime check is all we have
        except Exception:  # noqa: BLE001
            return None
        for h in r.get("hops", []):
            if h.get("constant"):
                return (f"{name!r}: {h['segment']} is declared in a VAR ... CONSTANT block ({h.get('declared_in')}, "
                        f"{Path(h.get('file') or '').name}:{h.get('line')}). The runtime accepts the write, but a constant changed "
                        "at runtime desynchronises array bounds and code compiled against it — change it in source and download")
        return None

    def _constant_in_any_plc(self, name: str) -> str | None:
        from .project_backend import plc_projects_declaring
        head = re.split(r"\.(?![^\[]*\])", name.strip())[0]
        for plc in plc_projects_declaring(self.proj, head):
            try:
                r = self.proj.resolve_symbol(name, plc=plc)
            except ProjectError:
                continue
            for h in r.get("hops", []):
                if h.get("constant"):
                    return (f"{name!r}: {h['segment']} is declared in a VAR ... CONSTANT block in PLC project "
                            f"{Path(plc).stem if plc else '(none)'} ({h.get('declared_in')}, {Path(h.get('file') or '').name}:"
                            f"{h.get('line')}) — the name is declared in several PLC projects and which one the target "
                            "runs is not known, so the write is refused (a constant changed at runtime desynchronises "
                            "code compiled against it). Name the ADS ports in the .tsproj (AmsPort) to tell them apart.")
        return None

    async def write_preflight(self, name: str, value: Any, port: int | None = None) -> None:
        await self.ads_call(self.ads.check_write, name, value, port, timeout=30)   # includes constant_reason

    async def _require_config_mode(self, action: str, target: str) -> None:
        """safety.edit_requires_config_mode: refuse code/project edits unless TwinCAT on the ADS target is in CONFIG.
        Fails closed — an unreadable system state refuses the edit."""
        try:
            st = await self.ads_call(self.ads.system_state, timeout=10)
            mode = st.get("twincat_mode")
        except ToolError as exc:
            self.guard.audit(action, {"target": target}, OpClass.EDIT, "refused", f"config-mode check failed: {exc}")
            raise ToolError(f"{action}: edits require CONFIG mode (safety.edit_requires_config_mode) and the TwinCAT system "
                            f"state could not be read: {exc}") from exc
        if mode != "CONFIG":
            self.guard.audit(action, {"target": target}, OpClass.EDIT, "refused", f"TwinCAT is in {mode}")
            raise ToolError(f"{action}: TwinCAT on {self.scope()} is in {mode} — switch to CONFIG first "
                            "(safety.edit_requires_config_mode = true). ads_system_mode('config') or the XAE toolbar.")

    async def guarded_target(self, action: str, args: dict[str, Any], op: OpClass, confirm_token: str | None) -> str:
        """token_target for an XAE-bound action; when the shell cannot be reached the guard still gets the first word
        (as xae_target_for)."""
        try:
            return await self.token_target(action, args)
        except ToolError as xae_exc:
            try:
                self.guard.check(op, action=action, target="", confirm_token=confirm_token, scope=self.scope(action))
            except SafetyError as exc:
                self.guard.audit(action, {"target": "", "scope": self.scope(action)}, op, "refused", str(exc))
                if isinstance(xae_exc, TargetArgsError) and "needs a confirm_token" in str(exc):
                    # N41/N42 polish: the shell answered, the arguments do not name a target — that reason first; the
                    # guard's "needs a confirm_token … target ''" suggested a token for '', which can never be right
                    raise ToolError(f"{xae_exc} — nothing was done. It needs a confirm_token too, issued for its "
                                    f"target once the arguments name one: tc_request_confirmation(action={action!r}, "
                                    "args={…the same arguments…})") from exc
                raise ToolError(f"{exc} (target not known: {str(xae_exc)[:200]})") from exc
            raise

    async def xae_target_for(self, action: str, op: OpClass, confirm_token: str | None) -> str:
        """The shell's target NetId for a target-bound XAE action. If no shell can be reached, the guard still
        gets the first word: in readonly/guarded-without-token the refusal is the guard's (the human never
        needed a shell to learn that), only a permitted call surfaces the attach error."""
        try:
            return (await self.xae_call(self.xae.target_get, timeout=60)).get("target_net_id", "")
        except ToolError as xae_exc:
            try:
                self.guard.check(op, action=action, target="", confirm_token=confirm_token, scope=self.scope(action))
            except SafetyError as exc:
                self.guard.audit(action, {"target": "", "scope": self.scope(action)}, op, "refused", str(exc))
                raise ToolError(f"{exc} (and no XAE shell is attached: {str(xae_exc)[:160]})") from exc
            raise

    async def _xae_netid(self) -> str:
        return str((await self.xae_call(self.xae.target_get, timeout=60)).get("target_net_id", ""))

    async def token_target(self, action: str, args: dict[str, Any]) -> str:
        """N40 / N42: the confirmation target of an action, computed from the tool's arguments and the server state —
        by the tool when it checks a token AND by tc_request_confirmation when it issues one, so they cannot differ
        and nobody has to guess the format. Machine-affecting XAE actions name the machine (the XAE target NetId),
        because the token's scope only binds the ADS target; xae_plc names the action, the PLC project and the NetId."""
        a = args or {}
        if action in ("xae_activate_configuration", "xae_restart_twincat", "xae_deploy", "tcunit_run"):
            return await self._xae_netid()
        if action == "xae_plc":
            act = str(a.get("action") or "")
            if not act:
                raise TargetArgsError("xae_plc's token names the action: pass args={'action': 'login', 'plc_project_path': …}")
            path = a.get("plc_project_path")
            # N78: the token names a PLC project of the open solution — one that does not exist gets no token (and
            # the tool, which computes its target the same way, refuses it before anything is done)
            projs = await self.xae_call(self.xae._nested_projects, None, timeout=120)
            if path:
                want = _plc_iec_path(str(path))
                hit = [p for p in projs if p.lower() == want.lower()]
                if not hit:
                    raise TargetArgsError(f"no PLC project {str(path)!r} in the open solution — its PLC projects: {projs} "
                                    "(xae_plc_projects lists them)")
                proj = hit[0]
            else:
                if len(projs) != 1:
                    raise TargetArgsError(f"{len(projs)} PLC projects — xae_plc needs plc_project_path, and its token names it: "
                                    f"{projs}")
                proj = projs[0]
            return f"{act}:{proj}@{await self._xae_netid()}"
        if action == "xae_command":
            return f"{a.get('name', '')}@{await self._xae_netid()}"
        if action == "xae_boot_project":
            path = str(a.get("plc_project_path") or "")
            return f"{path}@{await self._xae_netid()}" if a.get("activate_after_generate") else path
        if action == "xae_tree_delete":
            return f"{a.get('parent_path', '')}^{a.get('name', '')}"
        if action == "tc_allow_write":
            t = str(a.get("pattern") if "pattern" in a else a.get("target", "")).strip()
            return t[1:] if t.startswith("+") else t
        key = TOKEN_TARGET_ARGS.get(action)
        if key is not None:
            return str(a.get(key, "") if key else "target")
        return str(a.get("target", ""))

    async def check_targets(self, xae_target: str, allow_mismatch: bool) -> None:
        """Refuse deploy/tcunit when XAE targets one machine and ADS another."""
        ads_t = self.ads.net_id
        if ads_t is None:
            try:
                ads_t = await self.ads_call(self.ads.local_net_id, timeout=10)
            except ToolError:
                ads_t = None
        local = {None, "127.0.0.1.1.1", ads_t}
        if xae_target and xae_target not in local and not allow_mismatch:
            raise ToolError(
                f"XAE targets {xae_target} but the ADS side of this server targets {ads_t or 'local'}. "
                f"ads_connect(ams_net_id={xae_target!r}) first, or pass allow_target_mismatch=true if that is intended.")

    # --- safety ------------------------------------------------------------
    async def permit(self, ctx: Context | None, op: OpClass, action: str, target: str = "",
                     confirm_token: str | None = None, mode_gate: bool = True, always_confirm: bool = False) -> None:
        """Check the guard; for CONTROL/DESTRUCTIVE without a token, try to
        ask the human through elicitation before failing. ``mode_gate=False`` skips the
        readonly/guarded gating and only demands the confirmation — for tc_set_mode, which must be
        able to leave readonly mode with a human's consent. ``always_confirm``: readonly refuses it like any other
        change, and every other mode demands the confirmation (kb_rebuild: the user approves every rebuild, rule 6 —
        N127: it ran in readonly because it skipped the mode gate to get that)."""
        scope = self.scope(action)
        if op is OpClass.EDIT and action in CODE_EDIT_ACTIONS and self.cfg.safety.edit_requires_config_mode:
            await self._require_config_mode(action, target)
        try:
            if not mode_gate:
                self.guard.consume_token(confirm_token, action, target, scope)
            elif always_confirm:
                self.guard.check_mode(op, action)
                self.guard.consume_token(confirm_token, action, target, scope)
            else:
                self.guard.check(op, action=action, target=target, confirm_token=confirm_token, scope=scope)
            return
        except SafetyError as exc:
            # elicitation only replaces a *missing* token; a wrong/stale/foreign token is a hard error
            needs_token = confirm_token is None and "needs a confirm_token" in str(exc) \
                and op in (OpClass.CONTROL, OpClass.DESTRUCTIVE)
            if needs_token and ctx is not None and not self.cfg.safety.allow_elicitation:
                self.guard.audit(action, {"target": target, "scope": scope}, op, "refused", "allow_elicitation=false: token required")
                raise ToolError(f"{action} on {target!r} needs confirmation, and in-chat prompts are disabled "
                                f"(safety.allow_elicitation = false). Call tc_request_confirmation(action={action!r}, "
                                f"target={target!r}) and pass the confirm_token — the approval then shows up as its own step.") from exc
            if not needs_token or ctx is None:
                # a refusal is evidence too: who tried what, and why it did not happen
                self.guard.audit(action, {"target": target, "scope": scope}, op, "refused", str(exc))
                raise ToolError(str(exc)) from exc
        # elicitation path — the prompt must read as what it is: a safety confirmation, with the exact effect
        effect = ACTION_EFFECTS.get(action, "changes the state of the machine or project")
        kind = ("DESTRUCTIVE" if op is OpClass.DESTRUCTIVE else
                "KNOWLEDGE BASE" if action.startswith("kb_") else "MACHINE CONTROL")
        prompt = (f"{kind}: {action}\n"
                  f"Target: {target or '(current target)'}\n"
                  f"Machine: {scope}   Mode: {self.guard.mode}\n"
                  f"Effect: {effect}\n"
                  f"Accept only if you intend this to happen now.")
        # N89: on MCP 2026-07-28 the server cannot send elicitation/create — the question is returned to the client
        # (input_required) and answered on the next round of this same call; earlier revisions get the request
        try:
            res, answered_s = await ASK.ask(ctx, prompt, ConfirmSchema)
        except ASK.AskUser:
            self.guard.audit(action, {"target": target, "scope": scope}, op, "asked",
                             "confirmation prompt returned to the client (input_required)")
            raise
        except ASK.NoPrompt as exc:
            # N90: say what blocks the prompt (the client's declared capability, the transport) — not a guess
            self.guard.audit(action, {"target": target, "scope": scope}, op, "refused", f"needs confirm_token: {exc}")
            raise ToolError(
                f"{action} on {target!r} needs confirmation and no confirmation prompt can be shown in this session: "
                f"{exc}. Call tc_request_confirmation(action={action!r}, target={target!r}) and pass the "
                "confirm_token.") from exc
        timing: dict[str, Any] = {"prompt_open_s": answered_s}
        if answered_s is not None and answered_s < 1.5:
            timing["fast_answer"] = "answered in under 1.5 s — check that the operator saw and read the prompt"
        if res.action == "accept" and res.data is not None and res.data.confirm:
            self.guard.audit(action, {"target": target, "scope": scope}, op, "human-confirmed-elicitation", timing)
            return
        unticked = res.action == "accept"          # submitted, but the confirm box was not ticked
        self.guard.audit(action, {"target": target, "scope": scope}, op,
                         "declined-elicitation:" + ("accept-unticked" if unticked else res.action), timing)
        if unticked:
            # N94: 'declined by the user (accept)' read as a contradiction — the form came back without the tick
            raise ToolError(f"{action} on {target!r}: the confirmation was submitted with the confirm box not ticked, "
                            "so it counts as not confirmed. Nothing was done. Run the tool again and tick the box to "
                            "allow it.")
        if res.action == "cancel":
            # 'cancel' is also what the client sends when the prompt was dismissed or timed out unseen
            raise ToolError(f"{action} on {target!r}: the confirmation prompt came back cancelled — it was dismissed, timed out, "
                            f"or never shown. Nothing was done. Either run the tool again and answer the prompt, or confirm "
                            f"explicitly: tc_request_confirmation(action={action!r}, target={target!r}) and pass the confirm_token.")
        raise ToolError(f"{action} on {target!r} was declined by the user ({res.action}). Nothing was done.")

    def audit(self, tool: str, args: dict[str, Any], op: OpClass, outcome: str, detail: Any = None) -> None:
        self.guard.audit(tool, args, op, outcome, detail)

    async def confirmed(self, tool: str, args: dict[str, Any], op: OpClass, coro: Any) -> Any:
        """Run the body of a confirmed action and audit its *final* outcome — ok with the result, or error
        with the message — so the trail never ends at 'human-confirmed' for a command that then failed."""
        try:
            res = await coro
        except asyncio.CancelledError:
            # mcp 2.x cancels the handler when the client gives up (stop / timeout): a job that had not started is
            # withdrawn by its worker; one that had started runs on in its thread (asked to stop where it can)
            self.guard.audit(tool, args, op, "cancelled",
                             "the client gave up waiting: a command already running in TwinCAT / XAE may still "
                             "complete — check the state (ads_status, xae_status) before repeating it")
            raise
        except BaseException as exc:
            self.guard.audit(tool, args, op, "error", str(exc)[:400])
            raise
        self.guard.audit(tool, args, op, "ok", res)
        return res


async def _vs_runtime(app: "App", scope: str | None, max_items: int) -> dict[str, Any]:
    max_items = max(int(max_items), 1)
    proj = await app.proj_call(app.proj.variables, scope, 64, max_items, False, port=app.ads.port)
    src_cut = bool(proj.get("truncated"))
    # review of N113: roots declared in several PLC projects whose port maps to none — both sources are walked, so a
    # variable of the other PLC project would read as 'missing online': those roots are not judged
    ambiguous = {r.lower() for r in proj.get("ambiguous_roots") or []}
    declared = {it["path"].lower(): it for it in proj["items"] if "[*]" not in it["path"]}
    # source arrays are declared once as x[*].y — the runtime has one leaf per element: match by pattern
    templates = {it["path"].lower(): it for it in proj["items"] if "[*]" in it["path"]}
    tmpl_keys = list(templates)
    tmpl_re = None
    if tmpl_keys:
        alts = "|".join("(" + re.escape(k).replace(r"\[\*\]", r"\[[0-9, ]+\]") + ")" for k in tmpl_keys)
        tmpl_re = re.compile(r"^(?:" + alts + r")(?=$|[.\[])")
    root_names = {it["path"].split(".")[0].lower(): it["path"].split(".")[0] for it in proj["items"]}
    roots = set(root_names)
    online: set[str] = set()
    online_types: dict[str, str] = {}
    online_names: dict[str, str] = {}
    online_cut: set[str] = set()              # roots whose online enumeration hit max_items (D23)
    unreadable: list[str] = []
    for r in sorted(roots):
        try:
            groups = await app.ads_call(app.ads.leaves_under, root_names[r], max_items, timeout=300)
        except ToolError:
            unreadable.append(root_names[r])
            continue
        n = 0
        for root, leaves in groups:
            n += len(leaves)
            for lf in leaves:
                online.add(lf.path.lower())
                online_types[lf.path.lower()] = lf.type
                online_names[lf.path.lower()] = lf.path          # keep the PLC's spelling in the report
        if n >= max_items:
            online_cut.add(r)
    tmpl_hit: set[str] = set()
    via_template: set[str] = set()
    if tmpl_re is not None:
        for o in online:
            m = tmpl_re.match(o)
            if m:
                tmpl_hit.add(tmpl_keys[m.lastindex - 1])
                via_template.add(o)
    missing = [declared[k] for k in sorted(set(declared) - online)]
    # declared composite (library type) items match if any online leaf starts with them — and those leaves
    # (CTU.CV, TON.Q, R_TRIG.CLK …) are then *expected*, not "extra": the project cannot see inside a library FB
    missing_final = []
    composite_prefixes: list[str] = []
    for it in missing:
        pre = it["path"].lower() + "."
        if any(o.startswith(pre) or o.startswith(it["path"].lower() + "[") for o in online):
            composite_prefixes.append(it["path"].lower())
            continue
        missing_final.append(it)
    missing_final += [templates[k] for k in tmpl_keys if k not in tmpl_hit]
    # D23: a verdict needs both sides complete. A declared item under a root whose online list was cut (or could not
    # be read) is not "missing", and with the source list cut no online leaf can be called "extra".
    blind = online_cut | {u.lower() for u in unreadable} | ambiguous
    not_judged_missing = [it for it in missing_final if it["path"].split(".")[0].lower() in blind]
    missing_final = [it for it in missing_final if it["path"].split(".")[0].lower() not in blind]
    extra_raw = []
    for o in sorted(online - set(declared) - via_template):
        if any(o.startswith(cp + ".") or o.startswith(cp + "[") for cp in composite_prefixes):
            continue
        extra_raw.append(o)
    not_judged_extra = 0
    if src_cut:
        not_judged_extra, extra_raw = len(extra_raw), []
    # collapse array elements: aAlarms[1..32].x → one line with a count
    collapsed: dict[str, dict[str, Any]] = {}
    for e in extra_raw:
        name = online_names.get(e, e)
        key = re.sub(r"\[[0-9, ]+\]", "[*]", name)
        c = collapsed.setdefault(key.lower(), {"path": key, "type": online_types.get(e), "count": 0, "example": name})
        c["count"] += 1
    extra = list(collapsed.values())
    cap = 300
    out = {"declared": len(declared) + len(templates), "online_under_same_roots": len(online),
           "matched": len(declared) + len(templates) - len(missing_final) - len(not_judged_missing),
           "missing_online": missing_final[:cap],
           "extra_online": extra[:cap], "extra_online_leaves": len(extra_raw),
           "array_templates_matched": len(tmpl_hit), "array_elements_matched": len(via_template),
           "library_instances": [declared[cp]["path"] for cp in composite_prefixes][:200],
           "roots": sorted(root_names[r] for r in roots),
           "complete": not (src_cut or blind)}
    if src_cut or blind:
        why = []
        if src_cut:
            why.append(f"the source list stopped at max_items={max_items}: {not_judged_extra} online leaves not judged "
                       "(they may be declared in the part that was cut)")
        if online_cut:
            why.append(f"the online list of {sorted(root_names[r] for r in online_cut)} stopped at max_items={max_items}: "
                       f"{len(not_judged_missing)} declared items under them not judged")
        if unreadable:
            why.append(f"online symbols of {unreadable} could not be read: their declared items are not judged")
        if ambiguous:
            why.append(f"{proj.get('note')} — their declared items are not judged")
        out["not_judged"] = {"missing_candidates": len(not_judged_missing), "extra_candidates": not_judged_extra,
                             "reason": "; ".join(why) + ". Raise max_items or narrow with scope= for a full verdict."}
    if len(missing_final) > cap or len(extra) > cap:
        out["truncated"] = f"lists capped at {cap}; narrow with scope="
    # N92: a root of the source with no symbol at all on the target — one line with its cause, not only its items
    try:
        decl = await app.proj_call(app.proj.declared_roots)
        nt = not_on_target({k: d for k, d in decl.items() if k in roots}, await app.ads_call(app.ads.root_names,
                                                                                            timeout=60))
    except ToolError as exc:
        out["roots_not_on_target_error"] = f"not checked: {exc}"
    else:
        if nt["roots"]:
            out["roots_not_on_target"] = nt["roots"]
        if nt["not_judged"]:
            out["roots_not_on_target_not_judged"] = nt["not_judged"]
    return out



# PLC code / project edits gated by safety.edit_requires_config_mode
CODE_EDIT_ACTIONS = frozenset({"xae_pou_set", "xae_pou_create", "proj_set_code", "xae_tree_consume_xml", "xae_tree_set_values",
                               "xae_tree_create_child", "xae_plcopen_import", "tcunit_scaffold", "xae_io_disable"})

# what the confirmation prompt says will happen
ACTION_EFFECTS = {
    "scope_record": "disconnects the TwinCAT Scope configuration: what Scope holds (a capture the plugin has not saved) "
                    "is discarded and XAE's Scope view is cleared — the PLC and every ADS recording are not touched",
    "kb_rebuild": "rebuilds a knowledge-base index from its source on this PC; the current index stays in use until the "
                  "new one is complete, then replaces it — nothing else is changed or deleted (docs/KNOWLEDGE_BASE.md)",
    "xae_activate_configuration": "overwrites the boot configuration on the target; the runtime goes to CONFIG/restart",
    "xae_restart_twincat": "restarts the TwinCAT runtime on the target: the PLC stops and starts, outputs drop during the restart",
    "xae_deploy": "build + activate + restart on the target: the running program is replaced and the PLC restarts",
    "xae_plc": "PLC online action (login / download / online change / start / stop / reset) on the target",
    "xae_command": "runs an arbitrary XAE shell command — may download, log in, activate or delete",
    "xae_tree_delete": "deletes the item from the TwinCAT project",
    "ads_plc_state": "changes the PLC run state (RUN/STOP/RESET) — the machine stops or starts executing",
    "ads_system_mode": "switches TwinCAT between RUN and CONFIG — the PLC stops in CONFIG",
    "ipc_reboot": "reboots or shuts down the IPC",
    "ipc_start_process": "starts a program on the IPC",
    "ipc_eventlog": "starts wevtutil on the IPC to export the event log (read-only, but a process start)",
    "ipc_delete_file": "deletes a file on the IPC",
    "ipc_write_file": "writes a file on the IPC",
    "tc_set_mode": "raises the server's safety mode — more tools will be allowed to act on the machine",
    "tc_allow_write": "adds a symbol pattern to the write allow-list for this session",
    "exp_run": "runs an experiment that writes PLC variables on the machine",
}


def _apply_tool_groups(mcp: MCPServer, cfg: Config) -> None:
    """server.tool_groups (P3): expose only the named groups. Unknown names are reported in tc_doctor."""
    groups = cfg.server.tool_groups
    if not groups:
        return
    from .config import TOOL_GROUPS
    want = {g.lower() for g in groups} | {"tc"}
    unknown = sorted(want - set(TOOL_GROUPS))
    if unknown:
        cfg.warnings.append(f"server.tool_groups: unknown group(s) {unknown}; known: {list(TOOL_GROUPS)}")
    for t in list(mcp._tool_manager.list_tools()):
        grp = t.name.split("_", 1)[0]
        if grp not in want:
            mcp.remove_tool(t.name)


# R30: each Infosys tool describes the source this server is configured for — a client reads the description to decide
# what to call; "falls back to a DuckDuckGo site: query" / "fetched live" / "indexes the default libraries on first
# use" were read on a server that has none of that (the local Help Viewer index)
_DOCS_LOCAL = {
    "infosys_books": "List the Infosys books (documentation sets) of the Help Viewer package installed with TwinCAT, "
                     "with page counts — offline, one index of every book. pattern is a glob on the slug ('tcplclib_*', "
                     "'*json*'); category: plc-library | function-tf | engineering-te | twincat-system | hardware | other. "
                     "TwinCAT 2 books are excluded.",
    "infosys_index": "The local Help Viewer index holds every book and is built as a whole (the first time by itself, "
                     "later only with kb_rebuild(index='infosys') once the user approves) — this tool changes nothing "
                     "here and says so; kb_status / tc_doctor show the index state.",
    "infosys_search_symbols": "Find library symbols (function blocks, functions, structs, enums, aliases, interfaces, "
                              "GVLs) by name fragment in the local Help Viewer index; exact > prefix > contains. kind: "
                              "FunctionBlock | Function | Program | Method | Property | Action | Enum | Struct | Union | "
                              "Alias | Interface | GlobalVariable | Library. library: 'Tc2_System' or the book slug. "
                              "Offline; while the index is still being built the answer says so.",
    "infosys_search": "Full-text search over the local Help Viewer index (FTS5, bm25-ranked, excerpt with [hits]); "
                      "\"quoted phrases\" stay together, limit 1..50. Offline — no web search (web_fallback is ignored "
                      "and said so); results carry the page's doc version and the package date.",
    "infosys_get_page": "A documentation page as Markdown (headings, tables, fenced code), read from the local Help "
                        "Viewer package — any page in it, indexed or not; doc version + breadcrumb included. ref: full "
                        "Infosys URL, 'book/pageid', a bare page id, or a symbol name ('ADSREAD'). Offline. Use "
                        "max_chars to keep it short.",
}
_DOCS_WEB = {
    "infosys_books": "List Infosys books (documentation sets) from infosys.beckhoff.com's sitemap with page counts and "
                     "whether they are indexed. pattern is a glob on the slug ('tcplclib_*', '*json*'); category: "
                     "plc-library | function-tf | engineering-te | twincat-system | hardware | other. TwinCAT 2 books are "
                     "excluded.",
    "infosys_index": "Index a book (or glob, e.g. 'tcplclib_tc3_*') from infosys.beckhoff.com — pages new or changed "
                     "since the last run (Tc2_System ≈ 300 pages ≈ 30-60 s).",
    "infosys_search_symbols": "Find library symbols (function blocks, functions, structs, enums, aliases, interfaces, "
                              "GVLs) by name fragment in the indexed books; exact > prefix > contains. kind: "
                              "FunctionBlock | Function | Program | Method | Property | Action | Enum | Struct | Union | "
                              "Alias | Interface | GlobalVariable | Library. library: 'Tc2_System' or the book slug. "
                              "Indexes the default libraries in the background on first use if nothing is indexed.",
    "infosys_search": "Full-text search over the indexed Infosys pages (FTS5, bm25-ranked, excerpt with [hits]); "
                      "\"quoted phrases\" stay together, limit 1..50. When the index has nothing: a DuckDuckGo site: query "
                      "verified against the sitemap (web_fallback); the 'backend' field says which path answered.",
    "infosys_get_page": "A documentation page as Markdown (headings, tables, fenced code), fetched live from "
                        "infosys.beckhoff.com and cached. ref: full Infosys URL, 'book/pageid', a bare page id, or a "
                        "symbol name ('ADSREAD'). Use max_chars to keep it short.",
}


def _describe_docs_tools(mcp: MCPServer, source: str) -> None:
    texts = _DOCS_WEB if str(source or "local").lower() == "web" else _DOCS_LOCAL
    for name, text in texts.items():
        t = mcp._tool_manager.get_tool(name)
        if t is not None:
            t.description = text


def dump_timeout_message(p: dict[str, Any], limit_s: float) -> str:
    """What a timed-out ads_dump tells the client, from the state read under the commit lock (N52). The cancel event
    is already set when this runs: a dump that had not committed will not be kept; one that committed is."""
    el = time.time() - p.get("started", time.time())
    if p.get("phase") == "done":
        # the paths as text: a list's repr doubles every backslash of a Windows path (C:\\Users\\…)
        return (f"ads_dump finished just as the {limit_s:.0f} s limit ran out ({el:.0f} s), so its result did not reach "
                f"this call — the files WERE kept: {', '.join(str(f) for f in p.get('files') or [])}")
    if p.get("phase") == "partial":
        return f"ads_dump finished just as the {limit_s:.0f} s limit ran out ({el:.0f} s), then: {p.get('error')}"
    where = (f" root {p.get('index')} of {p.get('of')} ({p.get('root')}), {p.get('leaves', 0)} leaves, "
             f"{p.get('written_mib', 0)} MiB written" if p.get("root") else "")
    return (f"ads_dump did not finish within {limit_s:.0f} s ({el:.0f} s): it was {p.get('phase', '?')}{where} — the "
            "time went into the dump itself, not into ADS (other ADS calls keep answering). It is being stopped and its "
            "partial files are removed; nothing is kept. Narrow it with pattern= or lower max_leaves / max_total_mb.")


def connection_instructions(app: "App") -> str:
    """INSTRUCTIONS plus the real state of the offline documentation, read when the server starts — i.e. at every
    connection and reconnection of the client: a rebuild that is running, interrupted or outdated is said up front."""
    try:
        ds = app.kb.docs_status()
    except Exception as exc:  # noqa: BLE001 — never block the start
        return INSTRUCTIONS + f"\nOffline documentation: state not readable at this connection ({type(exc).__name__}: {exc}).\n"
    # N162 (0.8.14 retest): this text is sent once, when the client connects, and never again — a rebuild finished
    # later in the session left it saying 'REBUILD is due — format 5' about a format-6 index, and agents asked the
    # user for a rebuild nobody needed. It says when it was read, and that kb_status is the current state
    line = (f"\nOffline documentation, state at this connection ({ds.get('knowledge_folder', 'no folder')}; read "
            f"{time.strftime('%Y-%m-%d %H:%M')}, not refreshed while the session runs — kb_status gives the current "
            f"state): {ds['summary']}")
    if not ds.get("ok") and ds.get("infosys_command"):
        line += (" An index that exists is rebuilt only when the user approves it (docs/KNOWLEDGE_BASE.md): check "
                 "kb_status first (a rebuild may have finished since this was read), then tell the user, and on "
                 "approval kb_rebuild(index='infosys') — or the user runs " + ds["infosys_command"] + ".")
    return INSTRUCTIONS + line + "\n"


def _exc_text(exc: BaseException) -> str:
    return str(exc) or f"{type(exc).__name__} (no message)"


def _deliberate(exc: BaseException) -> bool:
    """An error this server raises on purpose, or one from the OS / database / ADS whose text is the explanation —
    as opposed to a defect (KeyError, TypeError … from a bug)."""
    return (type(exc).__module__.startswith("twincat_mcp")
            or isinstance(exc, (OSError, sqlite3.Error, ValueError, pyads.ADSError)))


# how SaveSVD's file is watched after the call returned: unchanged for (seconds), given up after (seconds)
SCOPE_SETTLE_S = (0.5, 15.0)

# set by every XAE call of the tool invocation running in this task: its result then carries the binding's flags
_XAE_USED: contextvars.ContextVar[bool] = contextvars.ContextVar("tcmcp_xae_used", default=False)
# N181 (0.8.15 retest): the running tool is read-only (its readOnlyHint) — a call a dialog refused changed nothing
_READ_ONLY_CALL: contextvars.ContextVar[bool] = contextvars.ContextVar("tcmcp_read_only_call", default=False)
# the XAE calls of the tool invocation running in this task: each one's XAE messages and the actions that ran in it —
# twelfth review of 0.8.16: xae_deploy / xae_activate_configuration / xae_restart_twincat / tcunit_run make one XAE
# call per step, and a later step's error lost what the earlier steps logged (an activation's ADS error 0x745)
_XAE_STEPS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar("tcmcp_xae_steps", default=None)


# N174 (0.8.14 retest): a crash's traceback was in no file the user could find (the client's MCP log has only the tool
# result; ~/.twincat_mcp/logs held only build logs). WARNING and above of the twincat_mcp loggers go to
# <audit log folder>/logs/server.log, rotating over 5 files of 5 MB; the internal-error message names it.
SERVER_LOG_BYTES = 5 * 1024 * 1024
SERVER_LOG_FILES = 5
_SERVER_LOG: dict[str, Any] = {"path": None, "handler": None}
# loggers besides 'twincat_mcp' whose warnings belong in server.log — N178 (0.8.15 retest): the SDK logs why it
# refused a confirmation round ('requestState rejected …: expired') on its own logger, which server.log did not get
_SERVER_LOG_ALSO = ("mcp.server.request_state",)


class _RoundRejected(logging.Handler):
    """N178: an audit line when the SDK refuses a confirmation round (expired, or not this call's) — the audit showed
    'asked' and nothing after it. The SDK offers no hook; its warning on mcp.server.request_state is the signal."""

    def __init__(self, app: Any) -> None:
        super().__init__(logging.WARNING)
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if "rejected" not in msg:
                return
            why = msg.split(":", 1)[-1].strip()
            # the class of a confirmation (CONTROL): an expiry is never a read that audit_reads=false drops
            self.app.audit("confirmation", {"round": msg.split(":", 1)[0]}, OpClass.CONTROL,
                           "expired" if "expir" in why.lower() else "rejected",
                           f"the SDK refused the answer: {why} — nothing was done; the client was told "
                           "'Invalid or expired requestState'")
        except Exception:  # noqa: BLE001 — a log handler never raises
            pass


def _audit_rejected_rounds(app: Any) -> None:
    lg = logging.getLogger("mcp.server.request_state")
    for h in [h for h in lg.handlers if isinstance(h, _RoundRejected)]:
        lg.removeHandler(h)                       # one per process: the newest server's App
    lg.addHandler(_RoundRejected(app))
    if lg.level == logging.NOTSET or lg.level > logging.WARNING:
        lg.setLevel(logging.WARNING)


def install_server_log(path: Path) -> Path | None:
    """One rotating file handler per process on the 'twincat_mcp' logger, moved when the path changes (a test or a
    second App with another home); None when the folder cannot be written."""
    import logging.handlers
    path = Path(path)
    cur = _SERVER_LOG.get("handler")
    if cur is not None and _SERVER_LOG.get("path") == path:
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=SERVER_LOG_BYTES, backupCount=SERVER_LOG_FILES - 1,
                                                 encoding="utf-8", delay=True)
    except OSError as exc:
        log.warning("server log %s cannot be written: %s", path, exc)
        return None
    h.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    pkg = logging.getLogger("twincat_mcp")
    if cur is not None:
        pkg.removeHandler(cur)
        for name in _SERVER_LOG_ALSO:
            logging.getLogger(name).removeHandler(cur)
        cur.close()
    pkg.addHandler(h)
    for name in _SERVER_LOG_ALSO:
        logging.getLogger(name).addHandler(h)
    _SERVER_LOG.update(path=path, handler=h)
    return path


_RPC_BUSY = (-2147418111, -2147417846)      # RPC_E_CALL_REJECTED 0x80010001, RPC_E_SERVERCALL_RETRYLATER 0x8001010A


def _com_hresult(exc: BaseException) -> int | None:
    """The HRESULT of a COM error (pywintypes.com_error: .hresult, or args[0]); None for anything else."""
    hr = getattr(exc, "hresult", None)
    if hr is None and type(exc).__name__ == "com_error" and exc.args and isinstance(exc.args[0], int):
        hr = exc.args[0]
    return hr if isinstance(hr, int) else None


def _com_detail(exc: BaseException, hr: int) -> str:
    """A COM error as a defect message names it: the HRESULT in hex, its text and the COM source / description."""
    info = getattr(exc, "excepinfo", None) or (exc.args[2] if type(exc).__name__ == "com_error" and len(exc.args) > 2
                                               else None) or ()
    text = getattr(exc, "strerror", None) or (exc.args[1] if len(exc.args) > 1 and isinstance(exc.args[1], str) else str(exc))
    src = str(info[1]) if len(info) > 1 and info[1] else ""
    desc = str(info[2]) if len(info) > 2 and info[2] else ""
    return (f"{type(exc).__name__} 0x{hr & 0xFFFFFFFF:08X} {str(text)[:200]!r}" + (f", source {src!r}" if src else "")
            + (f": {desc[:300]}" if desc else ""))


def _tool_boundary(fn: Any, xae_flags: Callable[[], Callable[[], dict[str, Any]] | None] = lambda: None,
                   dialog_hint: Callable[[], Callable[[], str] | None] = lambda: None, read_only: bool = False) -> Any:
    """What leaves a tool. mcp >= 2.1 passes only a ToolError's text to the client — any other exception arrives as a
    bare "Error executing tool X" (R37: never an empty error): a deliberate error keeps its text; a defect says its
    type and message and is logged with its traceback under a crash id (N174: in server.log, which the message
    names). A COM call the XAE shell rejected (busy) is said as that, never as a defect. A question for the user
    (prompting.AskUser) is this round's result. MCPError (a protocol error) and cancellation pass through."""
    name = getattr(fn, "__name__", "tool")

    @functools.wraps(fn)
    async def run(*args: Any, **kwargs: Any) -> Any:
        ro_tok = _READ_ONLY_CALL.set(read_only)      # N181: what a call a dialog refused means for this tool
        steps: list[dict[str, Any]] = []
        st_tok = _XAE_STEPS.set(steps)
        try:
            return await _run(*args, **kwargs)
        except ToolError as exc:
            # twelfth review of 0.8.16: a tool that failed after XAE calls that logged messages (an activation's ADS
            # error, then a restart that failed) — what they logged is part of the error
            told = str(exc)
            more = [f"{m.get('action')}{_msg_mark(m)}: {str(m['messages'])[:500]}" for st in steps
                    for m in st["messages"] if m.get("messages") and str(m["messages"])[:200] not in told]
            if more:
                raise ToolError(f"{told} — XAE messages of this call's earlier steps: " + "; ".join(more)) from exc
            raise
        finally:
            _XAE_STEPS.reset(st_tok)
            _READ_ONLY_CALL.reset(ro_tok)

    async def _run(*args: Any, **kwargs: Any) -> Any:
        used = _XAE_USED.set(False)
        try:
            res = await fn(*args, **kwargs)
            flags = xae_flags()
            if _XAE_USED.get() and flags is not None and isinstance(res, dict):
                # N116 / review of 0.8.14: the binding's flags go on the result the tool returns — not on the first
                # XAE call's (a token-target lookup's result is thrown away, a Scope result copies only its own keys)
                res = {**res, **{k: v for k, v in flags().items() if k not in res}}
            return res
        except ASK.AskUser as ask:
            return ask.result
        except (ToolError, MCPError):
            raise
        except Exception as exc:  # noqa: BLE001
            if _deliberate(exc):
                raise ToolError(_exc_text(exc)) from exc
            cid = secrets.token_hex(4)
            hr = _com_hresult(exc)
            if hr in _RPC_BUSY:
                # N174 (0.8.14 retest): 'Call was rejected by callee' left xae_build / xae_deploy as "internal error … a
                # defect" — a rejected call ran nothing; the backend retries it, and one that escapes is said as busy
                log.warning("tool %s: XAE rejected a COM call (0x%08X, busy) — crash %s", name, hr & 0xFFFFFFFF, cid,
                            exc_info=exc)
                hint = ""
                get_hint = dialog_hint()
                if get_hint is not None:
                    try:
                        hint = await asyncio.to_thread(get_hint)
                    except Exception:  # noqa: BLE001
                        hint = ""
                raise ToolError(f"{name}: XAE is busy (call rejected, 0x{hr & 0xFFFFFFFF:08X}) and did not run this "
                                "call — repeat it; if it keeps happening, a modal window or a long build holds the shell "
                                f"(xae_dialogs).{hint} (logged as {cid})") from exc
            log.error("tool %s: crash %s", name, cid, exc_info=exc)
            detail = (_com_detail(exc, hr) if hr is not None
                      else f"{type(exc).__name__}: {str(exc)[:300] or 'no message'}")
            where = _SERVER_LOG.get("path")
            raise ToolError(f"internal error in {name} ({detail}) — a defect in twincat-mcp, not in the call; "
                            + (f"traceback: {where}, crash {cid}" if where is not None else
                               f"its traceback is in the server log as crash {cid}")) from exc
        finally:
            _XAE_USED.reset(used)
    return run


class _NotRun(ToolError):
    """An XAE call that did not run: the shell refused it (busy, or a dialog holding it — XaeBusy), or it was refused
    before it started (a dialog an earlier call opened holds XAE; the worker is busy with another call). Repeating it
    runs it once — fourteenth review of 0.8.16: only the busy refusal was told apart, so a Scope stop refused while a
    dialog held XAE was final and Scope recorded on."""


def _msg_mark(m: dict[str, Any]) -> str:
    """Review 2 of 0.8.18: messages read after a failed before-read keep their mark wherever they are quoted."""
    return " (may include earlier calls' — the read before it failed)" if m.get("messages_note") else ""


class _HalfDone(ToolError):
    """A step of a two-step Scope action (disconnect, then StartRecord) ran, the next did not (or its outcome is not
    known) — never 'not done' (review 1 of 0.8.18)."""


class _Pending(ToolError):
    """An XAE call whose outcome is not known: a dialog it opened holds it inside the shell, or it timed out — it may
    still run, or have run. Never repeated (fourteenth review of 0.8.16: said 'NOT started' / 'did not stop')."""


class _BlockedByDialog(Exception):
    def __init__(self, job: str, dialog: dict[str, Any]) -> None:
        super().__init__(f"{job} blocked by dialog {dialog.get('title')!r}")
        self.job = job
        self.dialog = dialog


def _dialog_awaits(d: dict[str, Any]) -> bool:
    """A dialog the shell waits on: a push button other than Cancel / Abort (a progress window offers only those).
    N173: a modal window whose buttons are unknown is taken as waiting."""
    from .dialogs import awaits
    return awaits(d)


def _dialog_fields(d: dict[str, Any]) -> dict[str, Any]:
    """What a result says about one dialog: hwnd, title, class, text, buttons — and (N173) buttons_note when its
    buttons are unknown, unidentified when the shell is modal but no window could be named."""
    out = {k: d.get(k) for k in ("hwnd", "title", "class", "text", "buttons")}
    out.update({k: d[k] for k in ("buttons_note", "unidentified") if d.get(k)})
    return out


class TcMcpServer(MCPServer):
    """MCPServer whose tools go through _tool_boundary."""

    xae_flags: Callable[[], dict[str, Any]] | None = None     # App.xae_flags, set by build_server
    dialog_hint: Callable[[], str] | None = None              # App._dialog_hint (N174), set by build_server

    def tool(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        register = super().tool(*args, **kwargs)

        ann = kwargs.get("annotations")             # N181: the tool's readOnlyHint (mcp 2.x: read_only_hint)
        ro = bool(getattr(ann, "read_only_hint", None) or getattr(ann, "readOnlyHint", None))

        def decorator(fn: Any) -> Any:
            register(_tool_boundary(fn, lambda: self.xae_flags, lambda: self.dialog_hint, read_only=ro))
            return fn                     # the closure keeps the plain function (tools that call each other)
        return decorator


def build_server(cfg: Config | None = None) -> MCPServer:
    cfg = cfg or load_config()
    app = App(cfg)
    # subscriptions=False: nothing here publishes a change (resources are read on demand, the tool list is fixed at
    # start), so subscribe / listChanged are not advertised (mcp 2.3)
    # N178 (0.8.15 retest): the sealed confirmation round's lifetime is set here, not left to the SDK's default, so
    # the question can say it (prompting.ROUND_TTL_S); an expired round is logged and audited (_RoundRejected)
    from mcp.server.request_state import RequestStateSecurity
    mcp = TcMcpServer("twincat-mcp", instructions=connection_instructions(app), version=__version__,
                      subscriptions=False,
                      request_state_security=RequestStateSecurity.ephemeral(ttl=ASK.ROUND_TTL_S))
    _audit_rejected_rounds(app)
    mcp.xae_flags = app.xae_flags
    mcp.dialog_hint = app._dialog_hint
    # N162: the instructions are read again whenever a connection is initialised (a transport that serves several
    # sessions from one process asks for them per session), not only when the process started
    low = mcp._lowlevel_server
    _init_options = low.create_initialization_options

    def _fresh_init_options(*a: Any, **kw: Any) -> Any:
        low.instructions = connection_instructions(app)
        return _init_options(*a, **kw)
    low.create_initialization_options = _fresh_init_options  # type: ignore[method-assign]

    # ======================================================================
    # tc_* meta
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def tc_doctor(verbose: bool = False, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Environment check: platform, pyads/router, local AMS NetId, XAE ProgIDs in the
        registry, running XAE shells, project root, safety mode, whether a confirmation prompt can reach the user
        in this session (`elicitation`: the negotiated MCP revision and the client's declared capability). Run this
        first. Starts with a one-line `summary`; verbose=true adds the full loaded configuration."""
        rep: dict[str, Any] = {"version": __version__, "python": sys.version.split()[0],
                               "platform": platform.platform(), "windows": is_windows(),
                               "safety": app.guard.status()}
        el = ASK.report(ctx, cfg.safety.allow_elicitation)
        rep["elicitation"] = el
        infos_el = None if el["works"] or not cfg.safety.allow_elicitation else (
            f"confirmation prompts cannot reach the user here ({el['why']}): confirmations need tc_request_confirmation")
        if cfg.warnings:
            rep["config_FIX_THESE"] = list(cfg.warnings)
        if cfg.overrides:
            rep["config_overridden_by_env"] = [o["note"] for o in cfg.overrides]
        # pyads / router
        try:
            rep["local_ams_net_id"] = await app.ads_call(app.ads.local_net_id, timeout=10)
            rep["router"] = "ok"
        except ToolError as exc:
            rep["router"] = f"FAIL: {exc} — is TwinCAT XAE/XAR installed and the TwinCAT System Service running?"
        rep["ads_target"] = {"ams_net_id": cfg.ads.ams_net_id or "(local)", "port": cfg.ads.ams_port}
        infos: list[str] = []
        try:
            rep["target_state"] = await app.ads_call(app.ads.state, timeout=10)
        except ToolError as exc:
            # D45: CONFIG is a state the operator chooses, not a fault — the PLC port is simply not there
            sysmode = None
            try:
                sysmode = (await app.ads_call(app.ads.system_state, timeout=10)).get("twincat_mode")
            except ToolError:
                pass
            if sysmode == "CONFIG":
                rep["target_state"] = {"info": "TwinCAT in CONFIG — PLC runtime not running", "twincat_mode": "CONFIG",
                                       "plc_port": cfg.ads.ams_port, "detail": str(exc)[:160]}
                infos.append("TwinCAT in CONFIG — PLC runtime not running")
            else:
                rep["target_state"] = f"FAIL: {exc}" + (f" (TwinCAT system mode {sysmode})" if sysmode else "")
        # XAE
        if is_windows():
            try:
                import winreg  # type: ignore
                found = []
                for pid in cfg.xae.progids:
                    try:
                        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, pid))
                        found.append(pid)
                    except OSError:
                        pass
                rep["xae_progids_registered"] = found
            except Exception as exc:  # noqa: BLE001
                rep["xae_progids_registered"] = f"registry check failed: {exc}"
            try:
                rep["xae_running"] = await app.xae_call(lambda: [
                    {"progid": c["progid"], "solution": c["solution"]} for c in app.xae._rot_candidates()], timeout=30)
            except ToolError as exc:
                rep["xae_running"] = f"FAIL: {exc}"
        else:
            rep["xae"] = "unavailable (not Windows)"
        try:
            rep["recorder"] = await asyncio.to_thread(app.rec.engine_info)
        except Exception as exc:  # noqa: BLE001
            rep["recorder"] = {"error": f"{type(exc).__name__}: {exc}"}
        rep["project_root"] = cfg.project.root
        rep["solution"] = cfg.xae.solution_path
        rep["config_file"] = cfg.config_file
        if app.docs is not None:
            try:
                if app.docs.local is not None:
                    # one index of the local package (docs/KNOWLEDGE_BASE.md): its state, read-only here
                    rep["infosys"] = {"source": app.docs.source(), "index": str(app.docs.db_file),
                                      "state": await asyncio.to_thread(app.docs_index_status), "warm": app.docs_warm}
                else:
                    rep["infosys"] = {"source": app.docs.source(), "cache": str(app.docs.dir), "fts5": app.docs.has_fts,
                                      "indexed_books": await asyncio.to_thread(app.docs.indexed_books),
                                      "background_index": app.docs_index_status(), "warm": app.docs_warm,
                                      "default_books": cfg.docs.books}
            except (ToolError, InfosysError) as exc:
                rep["infosys"] = f"FAIL: {exc}"
        else:
            rep["infosys"] = f"FAIL: {app.docs_error}" if app.docs_error else "disabled"
        try:
            ds = await asyncio.to_thread(app.kb.docs_status)
            rep["offline_docs"] = ds
            # an index that exists but is not the current one, or a build, is worth a line; no folder is not
            if ds.get("infosys_state") in ("outdated", "rebuild_needed") or ds.get("infosys_rebuild") in (
                    "running", "interrupted", "waiting_to_replace"):
                infos.append("offline docs: Infosys index " + ds["infosys"])
            if ds.get("manuals_state") in ("outdated", "rebuild_needed"):
                infos.append("offline docs: manuals index " + ds["manuals"])
            if ds.get("esi_state") in ("outdated", "rebuild_needed") or ds.get("esi_rebuild") in (
                    "running", "interrupted", "waiting_to_replace"):
                infos.append("ESI index: " + ds["esi"])
        except Exception as exc:  # noqa: BLE001
            rep["offline_docs"] = {"error": f"{type(exc).__name__}: {exc}"}
        if infos_el:
            infos.append(infos_el)
        problems = [k for k, v in rep.items() if isinstance(v, str) and v.startswith("FAIL")]
        if cfg.warnings:
            problems.append("config_FIX_THESE")
        rep = {"summary": ("OK" if not problems else "PROBLEMS: " + ", ".join(problems)) +
                          (f" (info: {'; '.join(infos)})" if infos else "") +
                          f" — target {rep['ads_target']['ams_net_id']}:{rep['ads_target']['port']}, mode {app.guard.mode}", **rep}
        if verbose:
            rep["config"] = cfg.to_dict()
        return rep

    @mcp.tool(annotations=READ)
    async def tc_safety_status() -> dict[str, Any]:
        """Current safety mode (and where it came from), ceiling, allow/deny lists, pending confirmation tokens."""
        st = app.guard.status()
        src = next((o for o in cfg.overrides if o["setting"] == "safety.mode"), None)
        st["mode_source"] = (src["note"] if src else
                             "TOML" if "safety.mode" in cfg.toml_keys else
                             "environment TCMCP_MODE" if os.environ.get("TCMCP_MODE") else "default")
        if app.guard.mode != cfg.safety.mode:
            st["mode_source"] += f"; changed in this session with tc_set_mode (started as {cfg.safety.mode})"
        return st

    @mcp.tool(annotations=CONTROL)
    async def tc_set_mode(mode: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Switch safety mode: readonly | guarded | full. Lowering is free. Escalating (guarded→full,
        readonly→guarded) is capped at max_mode AND needs a confirmation (prompt or confirm_token) —
        an agent must not be able to widen its own permissions silently."""
        from .config import MODE_RANK
        new_rank, cur_rank = MODE_RANK.get(mode, -1), MODE_RANK.get(app.guard.mode, 0)
        if new_rank < 0:
            raise ToolError(f"mode must be one of readonly | guarded | full, got {mode!r}")
        if new_rank > cur_rank:
            await app.permit(ctx, OpClass.CONTROL, "tc_set_mode", target=mode, confirm_token=confirm_token, mode_gate=False)
        try:
            old = app.guard.set_mode(mode)
        except SafetyError as exc:
            raise ToolError(str(exc)) from exc
        return {"previous": old, "mode": app.guard.mode, "escalated": new_rank > cur_rank}

    @mcp.tool(annotations=CONTROL)
    async def tc_allow_write(pattern: str, remove: bool = False, confirm_token: str | None = None,
                             ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Add (or remove) a write allow-list pattern for THIS session only — the in-session alternative
        to editing twincat_mcp.toml and reconnecting. fnmatch, case-insensitive: 'GVL_Test.*',
        'MAIN.fbDispense.rSetpoint'. Control-class: needs a confirmation (prompt or confirm_token from
        tc_request_confirmation(action='tc_allow_write', target='<pattern>'), or target='-<pattern>' to remove).
        The deny list still wins; the change is audited and lost at the next server start."""
        pat = (pattern or "").strip()
        if not pat:
            raise ToolError("pattern is empty")
        # token target: the pattern itself to add, '-<pattern>' to remove (N1)
        await app.permit(ctx, OpClass.CONTROL, "tc_allow_write", target=("-" + pat) if remove else pat, confirm_token=confirm_token)
        lst = app.guard.cfg.write_allowlist
        if remove:
            before = len(lst)
            lst[:] = [x for x in lst if x.lower() != pat.lower()]
            changed = len(lst) != before
            app.session_allow = [x for x in app.session_allow if x.lower() != pat.lower()]
        else:
            changed = pat.lower() not in {x.lower() for x in lst}
            if changed:
                lst.append(pat)
                app.session_allow.append(pat)
        denied = [d for d in app.guard.cfg.write_denylist if fnmatch.fnmatch(pat.lower(), d.lower()) or fnmatch.fnmatch(d.lower(), pat.lower())]
        app.audit("tc_allow_write", {"pattern": pat, "remove": remove}, OpClass.CONTROL, "ok" if changed else "unchanged",
                  {"write_allowlist": list(lst)})
        return {"write_allowlist": list(lst), "changed": changed, "session_only": True,
                "note": ("overlaps the deny list, which always wins: " + ", ".join(denied)) if denied else
                        "persist it in twincat_mcp.toml [safety] write_allowlist to keep it across restarts"}

    @mcp.tool(annotations=EDIT)
    async def tc_reload_config() -> dict[str, Any]:
        """Re-read twincat_mcp.toml (and TCMCP_* env) without a restart. Applies: safety allow/deny lists,
        confirm_destructive, confirm_ttl_s, docs settings, naming prefixes, dialog rules, tcunit result path.
        Reports — but does not apply — changes that need a restart: ads target/port, solution path, project
        root, mode/max_mode (use tc_set_mode)."""
        try:
            fresh = load_config(cfg.config_file)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"config reload failed: {exc}") from exc
        applied: dict[str, Any] = {}
        needs_restart: dict[str, Any] = {}

        def _apply(section: str, key: str) -> None:
            new_v, old_v = getattr(getattr(fresh, section), key), getattr(getattr(cfg, section), key)
            if new_v != old_v:
                setattr(getattr(cfg, section), key, new_v)
                applied[f"{section}.{key}"] = {"from": old_v, "to": new_v}

        for key in ("write_allowlist", "write_denylist", "confirm_destructive", "confirm_ttl_s", "audit_reads",
                    "allow_elicitation", "edit_requires_config_mode"):
            _apply("safety", key)
        for key in ("books", "exclude_books", "web_fallback", "max_workers", "max_index_pages", "enabled"):
            _apply("docs", key)
        for key in ("naming_prefixes", "tcunit_remote_result", "hmi_root"):
            _apply("project", key)
        for key in ("dialog_rules", "allow_safety", "build_timeout_s", "attach_only"):
            _apply("xae", key)
        app.xae.watchdog.rules = list(cfg.xae.dialog_rules)
        kept = [x for x in app.session_allow if x.lower() not in {y.lower() for y in cfg.safety.write_allowlist}]
        cfg.safety.write_allowlist = list(cfg.safety.write_allowlist) + kept     # N6: session additions are kept
        for section, key in (("ads", "ams_net_id"), ("ads", "ams_port"), ("xae", "solution_path"), ("project", "root"),
                             ("safety", "mode"), ("safety", "max_mode")):
            new_v, old_v = getattr(getattr(fresh, section), key), getattr(getattr(cfg, section), key)
            if new_v != old_v:
                needs_restart[f"{section}.{key}"] = {"running": old_v, "file": new_v}
        app.audit("tc_reload_config", {"file": fresh.config_file}, OpClass.EDIT, "ok", {"applied": list(applied), "needs_restart": list(needs_restart)})
        out = {"config_file": fresh.config_file, "applied": applied, "needs_restart": needs_restart,
               "warnings": fresh.warnings, "write_allowlist": list(cfg.safety.write_allowlist)}
        if fresh.overrides:
            out["overridden_by_env"] = [o["note"] for o in fresh.overrides]
        if kept:
            out["kept_session_allow"] = kept
        return out

    # ---- knowledge base (decisions 2026-10-17) -----------------------------
    @mcp.tool(annotations=READ)
    async def kb_status() -> dict[str, Any]:
        """Knowledge base (layout and rules: docs/KNOWLEDGE_BASE.md): where it is (or the question to ask the user if
        no folder was chosen yet), the project folders indexed for kb_search, the ESI index for hw_lookup — and under
        `docs` the documentation indexes the infosys_* tools and the twincat-docs skill read: index\\infosys-<lcid>.db
        (the one Infosys index) and index\\manuals.db (PDF manuals, with the PDFs not in it yet), the SOPs, and the
        command that runs the skill's script on this folder. Each index: complete, or rebuild_needed / outdated (in use
        as it is until the user approves kb_rebuild), or a build running / interrupted. `layout` lists files the
        plugin no longer reads (never deleted by it) with their size."""
        return await app.kb_call(app.kb.status)

    @mcp.tool(annotations=CONTROL)
    async def kb_rebuild(index: str, force: bool = False, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Rebuild one index of the knowledge folder from its source on this PC — ONLY with the user's approval
        (docs/KNOWLEDGE_BASE.md rule 6: the user is asked, or pass the confirm_token of
        tc_request_confirmation(action='kb_rebuild', args={'index': …})). index: 'infosys' (the Help Viewer package,
        the plugin's extractor, in its own process — minutes; the current index stays in use until the new one is
        complete and then replaces it), 'manuals' (the PDFs in manuals\\, in its own process), 'esi' (the ESI files in
        manuals\\ and TwinCAT's ESI folders → index\\esi.db, in its own process — minutes; the current index stays in
        use until the new one is complete). Without force an index that is complete and current is left as it is.
        Nothing is deleted. kb_status / tc_doctor show the progress."""
        idx = (index or "").strip().lower()
        if idx not in ("infosys", "manuals", "esi"):
            raise ToolError(f"index must be 'infosys', 'manuals' or 'esi' (got {index!r})")
        if idx == "infosys" and (app.docs is None or app.docs.local is None):
            raise ToolError("no index of a local Help Viewer package here (docs.source = 'local' and a package are "
                            "needed) — " + (app.docs_error or "docs.source = 'web' keeps a web cache, not an index"))
        # N96: everything that can stop the rebuild is checked BEFORE the user is asked — the prompt came first and
        # then 'this server's Python has no PDF library'
        pre = await asyncio.to_thread({"infosys": app.spawn_infosys_build, "manuals": app.spawn_manuals_update,
                                       "esi": app.spawn_esi_build}[idx], force, dry_run=True)
        if not pre.get("would_start"):
            if pre.get("stale_work_file"):          # not a rebuild: the left work file is discarded, the index unchanged
                await app.permit(None, OpClass.EDIT, "kb_rebuild", idx)          # N127: a change — not in readonly
                pre = await asyncio.to_thread(app.spawn_esi_build, force)
            app.audit("kb_rebuild", {"index": idx, "force": force}, OpClass.CONTROL, "not-started", pre)
            return pre
        await app.permit(ctx, OpClass.CONTROL, "kb_rebuild", idx, confirm_token, always_confirm=True)
        if idx == "infosys":
            res = await asyncio.to_thread(app.spawn_infosys_build, force)
        elif idx == "manuals":
            res = await asyncio.to_thread(app.spawn_manuals_update, force)
        else:
            res = await asyncio.to_thread(app.spawn_esi_build, force)
        app.audit("kb_rebuild", {"index": idx, "force": force}, OpClass.CONTROL,
                  "ok" if res.get("started", True) or res.get("up_to_date") else "not-started", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def kb_set_location(path: str | None = None, use_fallback: bool = False) -> dict[str, Any]:
        """Choose the folder for the knowledge database — ONLY with a path the user gave you (or use_fallback=true
        once the user agreed to D:\\TwinCAT-MCP\\knowledge; not both). The folder is checked: a folder (not a file, not a
        drive root) on a local fixed drive (no network share / mapped drive / USB), not inside OneDrive/Dropbox/Google
        Drive/iCloud/Box, not inside the plugin folder, writable, >= 1 GB free — a failing condition is named and
        nothing is changed. An existing database is never
        moved or deleted. Without arguments it returns the current location (and changes nothing). A change is
        refused in readonly mode and audited, as every EDIT tool (N127)."""
        if not path and not use_fallback:                    # R38: a question, not a change — no gate, no audit
            return await app.kb_call(app.kb.set_location, None, False)
        args = {"path": path, "use_fallback": use_fallback}
        await app.permit(None, OpClass.EDIT, "kb_set_location", path or "fallback")

        async def body() -> dict[str, Any]:
            res = await app.kb_call(app.kb.set_location, path, use_fallback)
            moved = await asyncio.to_thread(app.docs_retarget)
            if moved:
                res["infosys_index"] = moved
            return res
        return await app.confirmed("kb_set_location", args, OpClass.EDIT, body())

    @mcp.tool(annotations=EDIT)
    async def kb_add_project(path: str, label: str | None = None) -> dict[str, Any]:
        """Index a project folder for kb_search (cross-project pattern search) — ONLY a folder the user named. Reads
        .TcPOU/.TcDUT/.TcGVL/.TcIO beneath it into the knowledge database; nothing is indexed by default. Refused: the
        plugin's folders, the knowledge folder, a folder inside or around one already indexed (its units would be
        there twice). The answer names the files that could not be read (`parse_errors`) and the files no PLC project
        compiles (`not_in_project`). Refused in readonly mode; audited (N127)."""
        await app.permit(None, OpClass.EDIT, "kb_add_project", path)
        return await app.confirmed("kb_add_project", {"path": path, "label": label}, OpClass.EDIT,
                                   app.kb_call(app.kb.add_project, path, label))

    @mcp.tool(annotations=EDIT)
    async def kb_remove_project(path: str) -> dict[str, Any]:
        """Remove a project folder from the knowledge base (index entries only; the files are untouched). Refused in
        readonly mode; audited (N127)."""
        await app.permit(None, OpClass.EDIT, "kb_remove_project", path)
        return await app.confirmed("kb_remove_project", {"path": path}, OpClass.EDIT,
                                   app.kb_call(app.kb.remove_project, path))

    @mcp.tool(annotations=EDIT)
    async def kb_reindex(path: str | None = None) -> dict[str, Any]:
        """Re-index one project folder (or every changed one) after edits. Refused in readonly mode; audited (N127)."""
        await app.permit(None, OpClass.EDIT, "kb_reindex", path or "every changed folder")
        return await app.confirmed("kb_reindex", {"path": path}, OpClass.EDIT, app.kb_call(app.kb.reindex, path))

    @mcp.tool(annotations=READ)
    async def kb_search(query: str, kinds: list[str] | None = None, project: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Search the code of the project folders the user added ("alarm handler", "FB_Valve", "CONCAT"): unit, kind,
        file and the matching declaration/code excerpts, best first. Names and identifiers count as their words
        (FB_AlarmList: alarm, list). Units with EVERY word first, then every word as a prefix ('CounterMod' finds
        FB_CounterModule); a unit whose code holds the words before one that has them only in comments
        (match_in='comments'); only when none has them all, units with some words
        (match='some words', with the words each unit holds; words on no unit or on most units are not used and
        are listed). A unit that declares the name (variable, constant, member, enum value, its own name) comes first
        (`declares`). Stop words ('how', 'to', 'the') and, in a query of several words, single characters are not
        searched; a query of stop words only is refused. There are no query operators: OR / AND / NOT / NEAR, '-',
        '^', '*' are not read as such (`operators_note`). A copy name (PRG_X_9) that no unit has falls back to the
        name it was copied from. "quoted text" is a phrase; tokens joined by punctuation ('STRING(40)', 'fbTimer(IN:=')
        are searched as one sequence (`searched_as_sequence`), a qualified name ('GVL_X.nY') as its names. A unit whose
        own name holds the words comes before its members. kinds (checked): PROGRAM, FUNCTION_BLOCK/FB, FUNCTION,
        POU (= those three), METHOD, PROPERTY, ACTION, INTERFACE, STRUCT, ENUM, GVL, DUT (= every data type) …; with
        kinds, a member is answered as its owner of those kinds (`via_member`: 'Execute' kinds=[INTERFACE] → I_Module);
        no hit names the units of other kinds that have the words. `words_not_in_code`: the words a hit has only in
        comments / string literals. project = the folder (any spelling) or its label; limit 1–200. `stale`: folders
        changed, renamed or deleted since they were indexed — their hits are those of the last indexing (kb_reindex);
        `changed_files` / `added_files` / `removed_files` name them. An error message — a code in
        a notation that says so (ADSError 1861, pyads' 'ADSError: … (1861)', ADS error 1861, error 1861, nErrId :=
        1861, 0x98110745, ADSERR_CLIENT_SYNCTIMEOUT), a compiler message (C0046: …), or a 0x number alone; not the
        word 'error', a decimal / 16# literal alone, a hex number anywhere or an ERR_… identifier of the code — gets the plugin's note
        in known_error (ADS return codes 6, 7, 1793, 1796, 1797, 1799, 1808, 1861 with code and symbol, license, XAE
        busy / dialog / disconnected, logged in, compiler: identifier not defined, cannot convert type, unknown type;
        null + known_errors_covered when it has none) and is searched only for the names of the code it holds
        (quoted, or identifiers such as fbCounter, FB_Valve, MAIN.fbX).
        Beckhoff documentation is not here: the twincat-docs skill."""
        return await app.kb_call(app.kb.search, query, kinds, project, limit)

    @mcp.tool(annotations=READ)
    async def hw_lookup(query: str, limit: int = 3, revision: int | str | None = None,
                        detail: str = "newest") -> dict[str, Any]:
        """EtherCAT device data from the ESI index (index\\esi.db: the ESI files in the knowledge folder's manuals\\
        and TwinCAT's ESI folders): name, product code, revisions, E-bus current, mailbox, DC modes, sync managers,
        PDOs with every entry (index, subindex, bit length, type; gaps marked), the predefined PDO assignments, slots
        of modular devices with their modules, the CoE object list, the diagnosis catalogue size, the ESI file(s).
        query = type / order code ('EL3356-0010'), part of a name, or a product code ('#x07D83052', '0x07D83052'; a
        number is read as a decimal product code and as digits of a type or name — `read_as`). revision: '#x…' as
        shown, '0x…' or decimal. detail: 'newest' (default) = all of that for the newest revision of each type (or of
        `revision`), other revisions as one line each; 'summary' = counts only; 'full' = everything for every
        revision. Nothing found: `note` says what is not there — a revision of an indexed device
        (`revisions_available`), or the device (then: add its ESI file). One CoE object with sub-indices, defaults
        and enum texts: hw_object; full-text: hw_search; a diagnosis text id: hw_diag. The index's first build starts
        by itself (minutes); until it is there the answer says so. `source`: the index the answer came from (state,
        built; `state_note` when it is not current). `manuals` names the indexed PDF manuals that mention the
        product, with pages (the twincat-docs skill reads them)."""
        first = await asyncio.to_thread(app.esi_first_build)
        res = await app.kb_call(app.kb.hw_lookup, query, limit, revision, detail, timeout=120)
        if first:
            res["esi_first_build"] = first
        return res

    @mcp.tool(annotations=READ)
    async def hw_search(query: str, kinds: list[str] | None = None, limit: HwLimit = 20,
                        offset: Offset = 0) -> dict[str, Any]:
        """Full-text search of the ESI index: devices (names, PDOs and their entries, PDO assignments), CoE objects
        (names, descriptions, sub-index names), diagnosis messages (text, description, causes, remedies) and modules.
        kinds: any of 'device', 'object', 'diag', 'module' (default all; a module is found by its own name and PDOs
        and by the devices whose slots take it — type, name, group). FTS5 syntax (words ANDed, "phrase", prefix*);
        other text is searched as one phrase. Hits with the same title (revisions of one type, one text in several
        catalogues) are one item with `variants`; objects and messages name the device types that have them. Pages:
        total items, offset / next_offset; a limit above 100 is used as 100 (clamped)."""
        first = await asyncio.to_thread(app.esi_first_build)
        res = await app.kb_call(app.kb.hw_search, query, kinds, limit, offset, timeout=120)
        if first:
            res["esi_first_build"] = first
        return res

    @mcp.tool(annotations=READ)
    async def hw_object(device: str, index: str, revision: int | str | None = None) -> dict[str, Any]:
        """One CoE object of a device from its ESI (newest revision unless `revision`): every sub-index with type, bit
        size and offset, access, PDO mapping, backup/setting flags, the default (decoded from DefaultData, enum text
        when there is one), min/max, unit code, enumeration texts; array elements one by one. index: '#x8000' or
        '0x8000' (4 hex digits alone are read as hex); one sub-index in CoE notation '8000:11' (the sub-index in hex,
        as Beckhoff's manuals write it: 8000:11 = sub-index 17) → `sub` and `subitems` with just that one. device:
        the exact type ('EL3356-0010')."""
        first = await asyncio.to_thread(app.esi_first_build)
        res = await app.kb_call(app.kb.hw_object, device, index, revision, timeout=60)
        if first:
            res["esi_first_build"] = first
        return res

    @mcp.tool(annotations=READ)
    async def hw_diag(code: str, device: str | None = None, revision: int | str | None = None) -> dict[str, Any]:
        """A diagnosis message by its text id as the diagnosis history shows it (object 0x10F3; '#x4101', '0x4101'):
        message text, description, causes and remedies, class, reaction, reset — from the device's own catalogue when
        `device` is given, else every distinct message with that id and the device types / modules that use it."""
        first = await asyncio.to_thread(app.esi_first_build)
        res = await app.kb_call(app.kb.hw_diag, code, device, revision, timeout=60)
        if first:
            res["esi_first_build"] = first
        return res

    @mcp.tool(annotations=READ)
    async def hw_module(ident: str, device: str | None = None, revision: int | str | None = None) -> dict[str, Any]:
        """A module of a modular device (AX8000, EP/EL with slots, TSC …) by its ModuleIdent as hw_lookup's slots give
        it: PDOs, CoE objects, diagnosis messages. With `device`, the module that device's slot means (hw_lookup's
        rule: the device's own file, then the module file paired with it by name, then the only one), else every
        module with that ident."""
        first = await asyncio.to_thread(app.esi_first_build)
        res = await app.kb_call(app.kb.hw_module, ident, device, revision, timeout=60)
        if first:
            res["esi_first_build"] = first
        return res

    async def tc_request_confirmation(action: str, target: str = "", args: dict[str, Any] | None = None) -> dict[str, Any]:
        act = (action or "").strip()
        # R2: a token for an action no tool performs ('format_c_drive') is noise at best, a decoy at worst
        known = {t.name for t in await mcp.list_tools()} | {"tcunit_scaffold_external"}
        if act not in known:
            import difflib
            near = difflib.get_close_matches(act, sorted(known), n=5, cutoff=0.5)
            raise ToolError(f"no tool performs {act!r}; a token is only issued for the name of the tool that will use it"
                            + (f" — did you mean {near}?" if near else ""))
        netid_actions = ("xae_activate_configuration", "xae_restart_twincat", "xae_deploy", "tcunit_run")
        a = dict(args) if args is not None else None
        if a is None and target and act in ("xae_plc", "xae_command"):
            # N40: a hand-written 'login' / 'login:<project>' / 'PLC.Login' is completed by the server, not refused
            body = target.strip().split("@", 1)[0]
            if act == "xae_plc":
                verb, _, proj = body.partition(":")
                a = {"action": verb, **({"plc_project_path": proj} if proj else {})}
            else:
                a = {"name": body}
        computed: str | None = None
        if a is not None or act in netid_actions or act == "tc_allow_write":
            try:
                computed = await app.token_target(act, a if a is not None else {"target": target})
            except ToolError as exc:
                raise ToolError(f"cannot issue a token for {act}: {exc}") from exc
            hand = target.strip()
            if act == "tc_allow_write":
                hand = hand[1:] if hand.startswith("+") else hand
            # a complete hand-written target must match; a partial one ('login') was completed above
            if hand and hand != computed and (act in netid_actions or "@" in hand or a is None or args is not None):
                raise ToolError(f"{act} checks the target {computed!r}, not {target!r} — request the token for "
                                f"{computed!r} (or pass the tool's arguments as args and the server computes it). "
                                + ("Change the XAE target first with xae_target if that is what you meant."
                                   if act in netid_actions else ""))
            target = computed
        xae_bound = act in netid_actions + ("xae_plc", "xae_command") or (
            act == "xae_boot_project" and bool((a or {}).get("activate_after_generate")))
        tok = app.guard.request_token(act, target, app.scope(act))
        if xae_bound:
            real = await app._xae_netid()
            tok["xae_target"] = real
            if app.ads.net_id and app.ads.net_id != real:
                tok["warning"] = (f"ADS side targets {app.ads.net_id} while XAE targets {real}: the action hits the XAE "
                                  f"target and the ADS tools keep reading the other machine. ads_connect(ams_net_id={real!r}) to align.")
        return tok

    # the docstring carries the per-tool target table (TOKEN_TARGET_DOC), so it is set before the tool is registered
    tc_request_confirmation.__doc__ = (
        "Issue a single-use confirm_token for a control/destructive tool: action = the tool's exact name. Easiest: "
        "pass the tool's own arguments as args (e.g. action='xae_plc', args={'action': 'login', 'plc_project_path': "
        "'TIPC^Plc^Plc Project'}) — the server computes the target the tool will check, the same way the tool does. "
        "A target given by hand must equal it (a partial one — 'login', 'PLC.Login' — is completed); the error names "
        "the right one. Targets: " + "; ".join(f"{k}: {v}" for k, v in TOKEN_TARGET_DOC.items()) + ".")
    mcp.tool(annotations=READ)(tc_request_confirmation)

    @mcp.tool(annotations=READ)
    async def tc_audit_tail(n: int = 50) -> list[dict[str, Any]]:
        """Last n audit records (mutating calls, confirmations, mode changes)."""
        return app.guard.tail_audit(max(1, min(n, 1000)))

    # ======================================================================
    # ads_* runtime
    # ======================================================================
    @mcp.tool(annotations=DESTR)
    async def ads_connect(ams_net_id: str | None = None, ams_port: int | None = None,
                          ip_address: str | None = None, confirm_token: str | None = None,
                          ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """(Re)connect to a target. ams_net_id like '5.12.34.56.1.1', 'local' for this PC, None =
        keep current; port 851 = PLC runtime 1. Re-targeting to a NetId other than the configured
        one is control-class (it changes which machine every later write hits)."""
        configured = {cfg.ads.ams_net_id, None, "local"}
        if ams_net_id not in configured and ams_net_id != app.ads.net_id:
            await app.permit(ctx, OpClass.CONTROL, "ads_connect", ams_net_id or "local", confirm_token)
        res = await app.confirmed("ads_connect", {"ams_net_id": ams_net_id, "port": ams_port}, OpClass.CONTROL, app.ads_call(app.ads.connect, ams_net_id, ams_port, ip_address, timeout=20))
        return res

    @mcp.tool(annotations=READ)
    async def ads_status(port: int | None = None) -> dict[str, Any]:
        """Composite status: connection, PLC runtime state, TwinCAT system mode (RUN/CONFIG),
        device name/version. With port=N returns only the ADS state of that port (852 PLC2,
        500 NC, 10000 system service, 65535 EtherCAT master 1)."""
        if port is not None:
            return await app.ads_call(app.ads.state, port, timeout=15)
        out: dict[str, Any] = {}
        for key, fn in (("plc", app.ads.state), ("system", app.ads.system_state), ("device", app.ads.device_info)):
            try:
                out[key] = await app.ads_call(fn, timeout=15)
            except ToolError as exc:
                out[key] = {"error": str(exc)}
        out["connection"] = app.ads.connection_status()       # after the reads: the connections they opened (#76)
        return out

    @mcp.tool(annotations=READ)
    async def ads_list_symbols(pattern: str = "*", type_filter: str | None = None, limit: int = 200,
                               offset: int = 0, refresh: bool = False, flag: str | None = None,
                               compact: bool = False) -> dict[str, Any]:
        """Browse the runtime symbol table. pattern is fnmatch ('MAIN.*', '*valve*'; bare text
        is wrapped in '*'). type_filter substring-matches the type ('ST_', 'BOOL', 'FB_').
        flag filters by symbol flag: PERSISTENT, READONLY, STATIC, ATTRIBUTES … compact=true returns
        'name : type' strings only (one fifth of the tokens)."""
        res = await app.ads_call(app.ads.list_symbols, pattern, 100_000 if flag else min(limit, 2000), 0 if flag else offset, refresh, type_filter, timeout=60)
        if flag:
            fl = flag.upper()
            sel = [x for x in res["symbols"] if fl in x["flags"]]
            res = {"total": len(sel), "offset": offset, "returned": len(sel[offset:offset + limit]), "symbols": sel[offset:offset + limit]}
        if compact:
            res["symbols"] = [f"{x['name']} : {x['type']}" for x in res["symbols"]]
        return res

    @mcp.tool(annotations=READ)
    async def ads_symbol_info(name: str) -> dict[str, Any]:
        """Type, size, index group/offset, flags (READONLY, PERSISTENT …) and comment of one symbol."""
        s = await app.ads_call(app.ads.symbol_info, name, timeout=30)
        return s.to_dict()

    @mcp.tool(annotations=READ)
    async def ads_read(name: str, expand_struct: bool = True, max_members: int = 200, max_chars: int = 60_000) -> dict[str, Any]:
        """Read one symbol with type-aware decoding. Structs/FB instances/arrays of structs decode
        into a nested tree (datatype table); TIME/DATE/DT/TOD get readable text; enums get names.
        Trees larger than max_chars are summarised (member names + sizes) — use ads_leaves or
        ads_dump for the full content."""
        res = await app.ads_call(app.ads.read, name, expand_struct, max_members, timeout=60)
        if isinstance(res.get("value"), int) and res.get("type"):
            res["value"] = await app.ads_call(app.ads.enum_named, res["type"], res["value"], timeout=30)
        if len(json.dumps(res, default=str)) > max_chars and isinstance(res.get("value"), (dict, list)):
            v = res["value"]
            res["value_summary"] = {"members": list(v.keys())[:200] if isinstance(v, dict) else f"list[{len(v)}]",
                                    "note": f"tree exceeds {max_chars} chars; call ads_leaves / ads_dump, or ads_read on a member"}
            res["value"] = None
        return res

    @mcp.tool(annotations=READ)
    async def ads_read_many(names: list[str]) -> dict[str, Any]:
        """Read several symbols in one ADS sum-command. Returns {name: value | {error}}."""
        if len(names) > 500:
            raise ToolError("max 500 symbols per call")
        res = await app.ads_call(app.ads.read_many, names, timeout=60)

        def _named() -> dict[str, Any]:
            out = {}
            for n, v in res.items():
                if isinstance(v, int) and not isinstance(v, bool):
                    try:
                        v = app.ads.enum_named(app.ads.symbol_info(n).type, v)
                    except Exception:  # noqa: BLE001
                        pass
                out[n] = v
            return out
        return await app.ads_call(_named, timeout=60)

    @mcp.tool(annotations=WRITE)
    async def ads_write(name: str, value: Any, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write a symbol on the running PLC. Accepts JSON scalars, lists for arrays, and
        literals like 'T#1s500ms', 'D#2026-10-15', '16#FF'. Returns before/after.
        Gated: readonly=never; guarded=write_allowlist; full=denylist only."""
        await app.permit(ctx, OpClass.WRITE, "ads_write", name)
        for member in await app.ads_call(app.ads.members, name, timeout=30):  # opaque hex write touches every member
            await app.permit(ctx, OpClass.WRITE, "ads_write", member.name)
        try:
            await app.write_preflight(name, value)
            res = await app.ads_call(app.ads.write, name, value, timeout=30)
        except ToolError as exc:
            app.audit("ads_write", {"name": name, "value": value}, OpClass.WRITE, "error", str(exc))
            raise
        app.audit("ads_write", {"name": name, "value": value}, OpClass.WRITE, "ok", res)
        return res

    @mcp.tool(annotations=WRITE)
    async def ads_write_many(values: dict[str, Any], ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write several symbols ({name: value}) together. Every symbol is gated (allow/deny list) and pre-checked
        (type, range, enum name, array/string length, CONSTANT, pointer/interface) BEFORE anything is written —
        one refusal → nothing is written and every problem is listed. Then ONE ADS sum-write: the runtime applies
        all values between the same two task cycles (method says if it had to fall back to one-by-one). Per-item
        before/written/read_back; applied/failed/not_attempted if the PLC rejects one."""
        problems: dict[str, str] = {}
        for n, v in values.items():
            try:
                await app.permit(ctx, OpClass.WRITE, "ads_write", n)
                await app.write_preflight(n, v)
            except ToolError as exc:
                problems[n] = str(exc)
        if problems:
            app.audit("ads_write_many", {"names": list(values)}, OpClass.WRITE, "refused", problems)
            raise ToolError("nothing was written — refused in pre-check: "
                            + "; ".join(f"{k}: {v}" for k, v in problems.items()))
        try:
            res = await app.ads_call(app.ads.write_many, values, timeout=60)
        except ToolError as exc:
            app.audit("ads_write_many", {"values": values}, OpClass.WRITE, "error", str(exc))
            raise
        app.audit("ads_write_many", {"values": values}, OpClass.WRITE, "ok" if res["complete"] else "partial",
                  {"method": res["method"], "applied": res["applied"], "failed": res["failed"]})
        if not res["complete"]:
            res["note"] = "the values in 'applied' took effect; snap_apply a snapshot to roll back if needed"
        return res

    @mcp.tool(annotations=READ)
    async def ads_io_read(area: str, offset: int, size: int = 1, port: int = 851) -> dict[str, Any]:
        """Raw process image of the PLC: area 'I' (%IB inputs) or 'Q' (%QB outputs) at a byte
        offset — what the task actually sees/wrote, independent of symbol names (link checks)."""
        return await app.ads_call(app.ads.io_read, area, offset, min(size, 4096), port, timeout=30)

    @mcp.tool(annotations=READ)
    async def ads_read_raw(index_group: int, index_offset: int, size: int, port: int = 851,
                           ams_net_id: str | None = None) -> dict[str, Any]:
        """Raw ADS read by index group/offset on any port (hex + little-endian uint)."""
        return await app.ads_call(app.ads.read_raw, index_group, index_offset, min(size, 1 << 20), port, ams_net_id, timeout=30)

    @mcp.tool(annotations=WRITE)
    async def ads_write_raw(index_group: int, index_offset: int, hex_data: str, port: int = 851,
                            ams_net_id: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Raw ADS write by index group/offset — PLC runtime ports (851-859) only. Every symbol whose
        memory overlaps the written range is checked against the write allow/deny lists; the raw
        target itself is 'raw:<port>/<igrp hex>/<ioffs hex>'."""
        if not (851 <= port <= 859):
            raise ToolError("raw writes are limited to PLC runtime ports 851-859; use ipc_*/ads_* tools for the system service")
        if ams_net_id and ams_net_id != app.ads.net_id:
            raise ToolError("raw writes go to the connected target only; ads_connect first")
        size = len(bytes.fromhex(hex_data))
        tgt = f"raw:{port}/0x{index_group:X}/0x{index_offset:X}"
        overlapped = await app.ads_call(app.ads.symbols_overlapping, index_group, index_offset, size, port, timeout=60)
        # two gates: every symbol the bytes overlap must be writable, AND the raw address itself must be allowed
        # ('raw:851/*' in the allow list) — a raw write can straddle a symbol boundary and reach memory no symbol names
        for nm in overlapped:
            await app.permit(ctx, OpClass.WRITE, "ads_write_raw", nm)
        try:
            await app.permit(ctx, OpClass.WRITE, "ads_write_raw", tgt)
        except ToolError as exc:
            raise ToolError(f"{exc} — raw writes need their own allow-list entry even when every overlapped symbol "
                            f"({overlapped or 'none'}) is allowed: add 'raw:{port}/*' (or this exact {tgt}) with tc_allow_write "
                            f"or in [safety] write_allowlist. Prefer ads_write by symbol name.") from exc
        res = await app.ads_call(app.ads.write_raw, index_group, index_offset, hex_data, port, ams_net_id, timeout=30)
        res["overlapping_symbols"] = overlapped
        app.audit("ads_write_raw", {"target": tgt, "hex": hex_data[:64]}, OpClass.WRITE, "ok", res)
        return res

    @mcp.tool(annotations=READ)
    async def ads_watch(names: list[str], duration_ms: int = 2000, interval_ms: int = 50,
                        change_only: bool = True) -> dict[str, Any]:
        """Trace symbols for duration_ms. Uses ADS notifications (PLC-timestamped, every change the runtime sees at
        its task cycle — not 50 ms polling from this PC): change_only=true records changes, false one sample per
        interval_ms. Falls back to polling when notifications are unavailable (method field says which). Caps: 60 s,
        5000 rows. The wait happens off the ADS workers, so writes issued concurrently land during the watch — a
        client that sends calls one after another still sees them after it; for stimulus→response use
        test_response, or rec_start + the write + rec_stop."""
        duration_ms = int(min(max(duration_ms, 10), cfg.ads.max_watch_duration_ms))
        interval_ms = int(max(1, interval_ms))
        try:
            # D6: interval_ms is the notification cycle in both modes — change mode looks for changes every interval_ms
            # (a value changing every PLC cycle gives one row per interval, not one per cycle)
            st = await app.rec_call(app.rec.start, symbols=names, prefix=None, pattern=None,
                                    mode="change" if change_only else "cycle", cycle_ms=float(interval_ms),
                                    duration_s=None, max_events=200_000, start_when=None, stop_when=None, pre_trigger_ms=0,
                                    name="ads_watch", max_symbols=200, scratch=True, timeout=60)
        except ToolError as exc:
            res = await app.ads_call(app.ads.watch, names, duration_ms, interval_ms, change_only,
                                     timeout=cfg.ads.max_watch_duration_ms / 1000 + 30, slow=True)
            res["method"] = f"polling from this PC every {interval_ms} ms (notifications failed: {str(exc)[:160]})"
            return res
        rid = st["id"]
        cleanup_error = None
        try:
            await asyncio.sleep(duration_ms / 1000.0)
            stop = await app.rec_call(app.rec.stop, rid, "ads_watch done", timeout=90)
            rows = await app.mem_call(app.rec.read, rid, None, float(duration_ms), None, 5000, False)
        finally:
            try:
                await app.mem_call(app.rec.delete, rid)
            except Exception as exc:  # noqa: BLE001
                cleanup_error = f"{type(exc).__name__}: {exc}"[:300]
        out = {"method": "ADS notifications (PLC timestamps)",
               "mode": f"change, checked every {interval_ms} ms" if change_only else f"cycle {interval_ms} ms",
               "duration_ms": duration_ms, "rows": [{"t_ms": r["t_ms"], "symbol": r["symbol"], "value": r["value"]} for r in rows["rows"]],
               "count": rows["count"], "truncated": rows["truncated"]}
        if cleanup_error:
            out["cleanup_warning"] = f"temporary recording {rid} could not be removed: {cleanup_error}"
        if stop.get("warnings"):
            out["warnings"] = stop["warnings"]
        return out

    @mcp.tool(annotations=READ)
    async def ads_wait_until(name: str, op: str, value: Any, timeout_ms: int = 10000,
                             interval_ms: int = 50) -> dict[str, Any]:
        """Block until symbol <op> value (==, !=, >, >=, <, <=) or timeout. Uses an ADS change notification, so it
        returns as soon as the runtime sees the condition (met_at_plc_ms = PLC-relative timestamp of that sample);
        falls back to polling every interval_ms when notifications are unavailable. Returns met/value/elapsed."""
        if op not in ("==", "!=", ">", ">=", "<", "<="):
            raise ToolError("op must be one of ==, !=, >, >=, <, <=")
        lit = json.dumps(value) if isinstance(value, bool) else str(value)
        timeout_ms = int(min(timeout_ms, 600_000))
        t0 = time.perf_counter()
        try:
            st = await app.rec_call(app.rec.start, symbols=[name], prefix=None, pattern=None, mode="change", cycle_ms=1.0,
                                    duration_s=None, max_events=None, start_when=None, stop_when=f"{name} {op} {lit}",
                                    pre_trigger_ms=0, name="ads_wait_until", max_symbols=1, scratch=True, timeout=60)
        except ToolError as exc:
            res = await app.ads_call(app.ads.wait_until, name, op, value, timeout_ms, interval_ms,
                                     timeout=timeout_ms / 1000 + 10, slow=True)
            res["method"] = f"polling (notifications failed: {str(exc)[:160]})"
            return res
        rid = st["id"]
        try:
            met = False
            # already true before the first change arrives? (a change notification only fires on change)
            cond = app.rec.recs[rid].stop_when
            try:
                cur = (await app.ads_call(app.ads.read, name, False, timeout=10)).get("value")
                if cond is not None and cond.test(cur):
                    await app.rec_call(app.rec.stop, rid, "already true", timeout=60)
                    return {"met": True, "value": cur, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                            "method": "read (condition already held)"}
            except ToolError:
                pass
            while (time.perf_counter() - t0) * 1000 < timeout_ms:
                s_ = await app.mem_call(app.rec.status, rid)
                if not s_["active"]:
                    met = str(s_.get("stop_reason") or "").startswith("stop_when")
                    break
                await asyncio.sleep(0.02)
            fin = await app.rec_call(app.rec.stop, rid, "ads_wait_until timeout", timeout=60)
            met = met or str(fin.get("stop_reason") or "").startswith("stop_when")
            last = (fin.get("last_values") or {}).get(name)
            if last is None:
                last = next(iter((fin.get("last_values") or {}).values()), None)
        finally:
            # the helper recording is scratch: whatever happens to its file must not turn a met condition into an
            # error (D7). Anything left behind is reported, not hidden.
            cleanup_error = None
            try:
                await app.mem_call(app.rec.delete, rid)
            except Exception as exc:  # noqa: BLE001
                cleanup_error = f"{type(exc).__name__}: {exc}"[:300]
        out = {"met": met, "value": last, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
               "method": "ADS change notification"}
        if met:
            out["met_at_plc_ms"] = fin.get("plc_span_s", 0) * 1000.0
        if cleanup_error:
            out["cleanup_warning"] = f"temporary recording {rid} could not be removed: {cleanup_error}"
        return out

    @mcp.tool(annotations=DESTR)
    async def ads_plc_state(action: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """PLC runtime control via ADS: run | stop | reset. 'reset' is a cold reset (all
        non-persistent data lost). Control-class: confirmation in guarded mode."""
        op = OpClass.DESTRUCTIVE if action == "reset" else OpClass.CONTROL
        await app.permit(ctx, op, "ads_plc_state", action, confirm_token)
        res = await app.confirmed("ads_plc_state", {"action": action}, op, app.ads_call(app.ads.set_plc_state, action, timeout=30))
        return res

    @mcp.tool(annotations=DESTR)
    async def ads_system_mode(action: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Switch TwinCAT on the target between 'run' and 'config' mode (restarts the
        runtime, I/O goes down). Destructive-class. Waits (≤20 s) until the target reports the requested mode and
        returns before/after; reached=false when it did not get there (license / configuration error)."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ads_system_mode", action, confirm_token)
        res = await app.confirmed("ads_system_mode", {"action": action}, OpClass.DESTRUCTIVE, app.ads_call(app.ads.set_system_mode, action, timeout=40))
        if isinstance(res, dict) and not res.get("reached", True):
            res["dialogs"] = await asyncio.to_thread(app._dialog_hint)
        return res

    @mcp.tool(annotations=READ)
    async def ads_ethercat(master_port: int | None = None) -> dict[str, Any]:
        """EtherCAT master state (INIT/PREOP/SAFEOP/OP) and slave count via the master's ADS
        port (default 65535 for the first master; see Device → ADS tab in XAE)."""
        return await app.ads_call(app.ads.ethercat_overview, master_port, timeout=20)

    @mcp.tool(annotations=READ)
    async def ads_routes() -> dict[str, Any]:
        """Local static ADS routes (parsed from StaticRoutes.xml on this PC)."""
        return await app.ads_call(app.ads.local_routes, timeout=10)

    @mcp.tool(annotations=DESTR)
    async def ads_add_route_to_plc(plc_ip: str, plc_username: str = "Administrator", plc_password: str = "1",
                                   route_name: str | None = None, local_net_id: str | None = None,
                                   hostname: str | None = None, confirm_token: str | None = None,
                                   ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Create the PLC-side route back to this PC (UDP discovery protocol, needs the target's
        Windows/TC-BSD credentials). Control-class."""
        await app.permit(ctx, OpClass.CONTROL, "ads_add_route_to_plc", plc_ip, confirm_token)
        res = await app.confirmed("ads_add_route_to_plc", {"plc_ip": plc_ip, "route_name": route_name}, OpClass.CONTROL, app.ads_call(app.ads.add_route_to_plc, plc_ip, plc_username, plc_password, route_name, local_net_id, hostname, timeout=30))
        return res


    # ======================================================================
    # rec_* real-time recorder
    # ======================================================================
    @mcp.tool(annotations=EDIT)
    async def rec_start(symbols: list[str] | None = None, prefix: str | None = None, pattern: str | None = None,
                        mode: str = "change", cycle_ms: float = 10, duration_s: float | None = None,
                        max_events: int | None = 5_000_000, start_when: str | None = None,
                        stop_when: str | None = None, pre_trigger_ms: float = 0, name: str | None = None,
                        max_symbols: int = 500, post_trigger_s: float | None = None, allow_partial: bool = False,
                        max_delay_ms: float = 0, cycle_counter: str | None = "auto", edge_triggers: bool = False,
                        engine: str | None = None, also_scope: bool | None = None, scope: str | None = None,
                        scope_disconnect: bool | None = None,
                        ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Start a continuous real-time recording of online PLC variables (runs in the background
        until rec_stop, duration_s, max_events or stop_when). Select by explicit symbols (an FB or
        struct instance expands to all its leaves), prefix ('MAIN.fbFill', 'GVL_Sim.aCh[1]' → every leaf under it)
        or pattern — the same leaves rec_scan lists for it: '*'/'?' wildcards, '[ ]' literal ('GVL_Sim.aCh[1].*'),
        a leaf is recorded when it or one of its parents matches ('*bOpen' → every bOpen). mode: 'cycle' = one PLC-timestamped sample per symbol per cycle_ms
        (ADS notification, cycle-accurate); 'change' = PLC-timestamped sample only when the value
        changes (checked every cycle_ms); 'poll' = sum-read every cycle_ms from this PC.
        Triggers: start_when='MAIN.bStart == TRUE' with pre_trigger_ms keeps the events before the
        trigger (scope-style); stop_when='MAIN.fbFill.bDone == TRUE'. Events stream to a JSONL file.
        duration_s is PLC time from the first sample (also while waiting for start_when); post_trigger_s ends the
        recording that long after the start_when trigger fired (scope-style post-trigger window).
        Timestamps (cycle/change) are the TwinCAT runtime's own, stored exactly with the raw bytes it sent; neighbouring
        variables share ONE notification (a block), which is what keeps 1 ms recordings lossless — select whole FBs,
        structs or arrays rather than hundreds of scattered globals. Any notification that cannot be registered fails
        the start (allow_partial=true records the rest and lists unsubscribed_symbols). max_delay_ms > 0 lets the
        runtime batch samples (fewer ADS frames; each sample keeps its own timestamp). cycle mode also records a
        per-cycle counter as ground truth (cycle_counter='auto' = the matching task's _TaskInfo[n].CycleCount, or your
        own e.g. 'MAIN.nCycleCount', or null for none) and the header carries a manifest of what was recorded from
        (AppTimestamp, OnlineChangeCnt, tasks, TwinCAT build, symbol version). A recording is of one program: a download
        / online change while it runs ends it at the last sample verified against the symbol version (program_changed);
        nothing sampled from the new program is written. Conditions are levels: a start_when that
        already holds fires at once — edge_triggers=true fires only when it BECOMES true. A condition may name one
        element of an array recorded as a whole (GVL_Test.aCounter[1] when GVL_Test.aCounter is recorded).
        engine (cycle mode): 'native' = the C++ capture service (Beckhoff AdsLib on TcAdsDll: ADS callback → ring →
        file, no Python on the data path), 'python' = the in-process recorder, None = recorder.engine from the config
        (default 'auto': native when installed). The response says which engine ran and why. After stopping: rec_verify.
        also_scope: also run the TwinCAT Scope configuration open in XAE (scope = its name when several are open) —
        StartRecord now; at rec_stop StopRecord and SaveSVD into <recordings>/Scope Recordings/<id>.svdx, the CSV <id>.scope.csv
        exported from that file with Beckhoff's export tool (XAE's ExportCSV only without it, flagged), XAE's ExportCSV of
        what Scope holds in memory beside it (<id>.scope.xae.csv) with both row counts compared (cross_check — a saved
        .svdx shorter than memory, or the reverse, is a warning), a signed link file, and the Scope View log's new
        ERROR / FATAL lines (scope_view_log). A .svdx still being written 15 s after SaveSVD is not exported. What Scope records (channels, sample time) is set in that configuration. With
        recorder.also_scope = 'ask' (default) and a Scope configuration open, leaving also_scope unset asks the user
        (prompt, or an error that tells you to ask and call again with true/false). After a recording Scope holds its
        data until it is disconnected: a recording with Scope then asks to disconnect it first — scope_disconnect=true
        / false is the user's answer (false: this recording without Scope)."""
        # R6: arguments that can never work are refused here, before the call queues behind a long job on the worker
        if mode not in ("cycle", "change", "poll"):
            raise ToolError("mode must be cycle | change | poll")
        if engine is not None and str(engine).lower() not in ("auto", "native", "python"):
            raise ToolError("engine must be auto | native | python")
        scope_note = None
        h = _scope_holder()
        if also_scope is None and h is not None and str(app.cfg.recorder.also_scope or "ask").lower() == "ask":
            # N189: Scope has another owner — it is not offered (a yes could only be refused)
            want_scope, scope_note = None, f"Scope was not offered: {_scope_owner_text(h)}"
        else:
            want_scope = await _scope_decide(also_scope, scope, ctx)
        claim = None
        if want_scope is not None:
            # N189: Scope is checked and claimed before anything starts — a second rec_start (also in the same batch)
            # sees the claim, and its recording is not started
            try:
                claim = _scope_claim("rec_start")
            except ToolError as exc:
                raise ToolError(f"{exc} This recording was not started: call rec_start again with also_scope=false to "
                                "record without Scope, or once Scope is free.") from exc
        try:
            if want_scope is not None and app.scope_left:
                want_scope = await _scope_free(want_scope, scope_disconnect, ctx)
            res = await app.rec_call(app.rec.start, symbols=symbols, prefix=prefix, pattern=pattern, mode=mode,
                                     cycle_ms=cycle_ms, duration_s=duration_s, max_events=max_events,
                                     start_when=start_when, stop_when=stop_when, pre_trigger_ms=pre_trigger_ms,
                                     name=name, max_symbols=max_symbols, post_trigger_s=post_trigger_s,
                                     allow_partial=allow_partial, max_delay_ms=max_delay_ms,
                                     cycle_counter=cycle_counter, edge_triggers=edge_triggers, engine=engine,
                                     timeout=120)
            app.audit("rec_start", {"id": res["id"], "symbols": res["symbols"], "mode": mode,
                                    "engine": res.get("engine")}, OpClass.EDIT, "ok")
            if want_scope is not None:
                res["scope"] = await _scope_start(res["id"], want_scope)     # the part holds Scope from here on
            elif scope_note:
                res["scope_note"] = scope_note
        finally:
            _scope_unclaim(claim)
        return res

    async def _scope_free(name: str, disconnect: bool | None, ctx: Context | None) -> str | None:
        """Scope still holds an earlier recording (pause, or not asked yet): it is disconnected before this one records
        — the user decides (Ryan, 2026-10-21: the next recording with Scope asks). -> the Scope name, or None (this
        recording without Scope). Raises when nothing could be decided; nothing is started then."""
        left = app.scope_left or {}
        held = (f"Scope still holds {_left_label(left)} (" +
                (f"saved: {Path(left['saved']).name}" if left.get("saved") else "NOT saved — disconnecting discards it")
                + ")")
        via = "scope_disconnect argument" if disconnect is not None else None
        if disconnect is None and ctx is not None and app.cfg.safety.allow_elicitation:
            try:
                ans, _open_s = await ASK.ask(ctx, f"{held}. Disconnect it and record with Scope now?",
                                             ScopeDisconnectSchema)
                via = "prompt"
                if ans.action == "accept" and ans.data is not None:
                    disconnect = ans.data.disconnect
            except ASK.NoPrompt:
                pass
        if via is not None:
            # N190 (0.8.17 retest): the question and its answer are in the audit, as a confirmation is
            app.audit("scope_disconnect_asked", {"held": left.get("rid"), "saved": bool(left.get("saved")), "via": via,
                                                 "answer": disconnect}, OpClass.EDIT, "ok")
        if app.scope_left is not left and app.scope_left is not None:
            raise ToolError("What Scope holds changed while the question was open — call rec_start again. Nothing was "
                            "started.")
        if app.scope_left is None and disconnect is not None:
            return name if disconnect else None   # disconnected meanwhile: nothing left to discard
        if disconnect is None:
            raise ToolError(f"{held}: it records anew only once it is disconnected. Ask the user, then call rec_start "
                            "again with scope_disconnect=true (Scope is disconnected first) or also_scope=false. Nothing "
                            "was started.")
        if not disconnect:
            return None
        try:
            await _scope_disconnect(left.get("name") or name or None)
        except ToolError as exc:
            raise ToolError(f"Scope could not be disconnected ({exc}). Nothing was started.") from exc
        return name

    async def _scope_decide(also_scope: bool | None, scope: str | None, ctx: Context | None) -> str | None:
        """None = no Scope recording; otherwise the Scope name to use ('' = the only one open)."""
        pol = str(app.cfg.recorder.also_scope or "ask").lower()
        if also_scope is False:
            return None
        if pol == "never":
            if also_scope:
                raise ToolError("recorder.also_scope = 'never' in the config: Scope recordings alongside rec_start are off")
            return None
        name = scope or app.cfg.scope.project or ""
        if also_scope or pol == "always":
            return name
        # 'ask': only when XAE is attached and has a Scope configuration open — nothing to offer otherwise
        if app.xae.dte is None:
            return None
        try:
            lst = await app.xae_call(app.xae.scope_list, timeout=20)
        except ToolError:
            return None
        if not lst.get("count"):
            return None
        names = [d["project"] + "\\" + d["name"] for d in lst["scopes"]]
        q = (f"TwinCAT Scope configuration(s) open in XAE: {', '.join(names)}. Also record with Scope while this "
             f"recording runs (saved as .svdx + CSV next to the recording)?")
        if ctx is not None and app.cfg.safety.allow_elicitation:
            try:
                ans, _open_s = await ASK.ask(ctx, q, AlsoScopeSchema)     # AskUser: this round returns the question
                if ans.action == "accept" and ans.data is not None:
                    return name if ans.data.also_scope else None
            except ASK.NoPrompt:
                pass                                   # no prompt in this session: the agent asks (below)
        raise ToolError(f"recorder.also_scope = 'ask': {q} Ask the user, then call rec_start again with also_scope=true "
                        f"(scope=<name> if several) or also_scope=false. Nothing was started. "
                        f"([recorder] also_scope = 'always' / 'never' makes this permanent.)")

    def _view_log() -> Path:
        from . import scope as S
        return S.view_log_path(app.cfg.scope.view_log)

    async def _scope_start(rid: str, name: str) -> dict[str, Any]:
        from . import scope as S
        mark = await asyncio.to_thread(S.log_mark, _view_log())
        run: dict[str, Any] = {"scope": name or None, "name": name or None, "started": None,
                               "start_asked": dt.datetime.now(dt.timezone.utc).isoformat(), "log_mark": mark}
        # first review of 0.8.17: the part exists before StartRecord — a rec_stop meanwhile found none, finished nothing,
        # and the end it caused was later labelled as a call that had ended. Its requests wait for the start
        part = _ScopePart(rid, run)
        app.scope_parts[rid] = part
        try:
            r = await app.xae_call(_marked_start(name or None), timeout=60)
            mark = r.pop("_log_mark", None)
        except asyncio.CancelledError as exc:
            if _did_not_run(exc):
                # second review of 0.8.17: StartRecord was withdrawn from the XAE queue (or refused) — it never ran;
                # the part stopped and saved a Scope this recording never started (someone else's run, or nothing)
                part.not_started("Scope recording NOT started: rec_start was cancelled before StartRecord ran — it "
                                 "did not run")
            else:
                run["start_unknown"] = "rec_start was cancelled while StartRecord ran — whether it started is not known"
                part.begin()
            raise
        except _NotRun as exc:
            # thirteenth review of 0.8.16: the busy text's 'repeat it' meant a second rec_start — a second recording
            c = exc.__cause__
            from . import dialogs as _dlg
            why = (f"a dialog holds the shell: {_dlg.label(c.dialog)}" if isinstance(c, XaeBusy) and c.dialog else
                   f"XAE is busy — call rejected, 0x{c.busy[0] & 0xFFFFFFFF:08X}, asked again for {c.busy[1]:g} s"
                   if isinstance(c, XaeBusy) and c.busy else str(exc)[:400])
            err = (f"Scope recording NOT started: XAE refused StartRecord ({why}; it did not run). The ADS "
                   f"recording {rid} runs regardless — do not repeat rec_start (it would start a second "
                   "recording): scope_record(action='start') starts Scope once XAE is free (not linked to "
                   "this recording), or rec_stop and rec_start again")
            part.not_started(err)
            return {"error": err}
        except _Pending as exc:
            # fourteenth review of 0.8.16: a StartRecord a dialog holds (or that timed out) was said 'NOT started' —
            # once the dialog was answered Scope recorded with no owner, and rec_stop never stopped it. The part stays
            # with the recording: its end (or rec_stop) sends StopRecord and says what it found
            run["start_unknown"] = str(exc)[:600]
            part.begin()
            app.audit("scope_start", {"id": rid, "scope": run["scope"]}, OpClass.EDIT, "error", "outcome not known")
            return {"started": None, "start_unknown": True,
                    "warning": f"Scope StartRecord's outcome is not known ({exc}) — it may still start recording. The "
                               f"ADS recording {rid} runs regardless; when it ends (or at rec_stop) Scope is stopped "
                               "(StopRecord) and saved with it, and the result says what was found"}
        except ToolError as exc:
            err = f"Scope recording NOT started: {exc}. The ADS recording {rid} runs regardless."
            part.not_started(err)
            return {"error": err}
        # first review of 0.8.17: the link's 'started' is when StartRecord ran, not when it was asked (a busy shell
        # retries for up to com_retry_s)
        run.update(scope=r["scope"], start_result=r.get("result_code"),
                   started=dt.datetime.now(dt.timezone.utc).isoformat())
        try:
            state, how = await _start_check(mark)
        except BaseException:
            # review 1 of 0.8.18: rec_start cancelled during the check (or the log read failed) — StartRecord ran; the
            # part was left 'starting' with no owner: rec_stop / rec_delete waited forever and Scope recorded on
            run["start_check"] = "not checked: rec_start was cancelled (or the log read failed) during the check"
            part.begin()
            raise
        if state is not None:
            # N186 (0.8.16 retest): StartRecord returned 1 and Scope did not record — it was still in Reply with the
            # previous recording; that capture was then saved and signed as this recording's
            err = _start_refused_text(state, how, r.get("result_code"), rid)
            part.not_started(err)
            app.audit("scope_start", {"id": rid, "scope": r["scope"]}, OpClass.EDIT, "error", f"Scope in {state}")
            return {"error": err, "scope_state": state}
        run["start_check"] = how
        part.begin()
        app.audit("scope_start", {"id": rid, "scope": r["scope"]}, OpClass.EDIT, "ok")
        r = {k: v for k, v in r.items() if k != "result_note"}     # N186: StartRecord's 1 is not what tells
        return {"started": True, **r, "start_check": how,
                "note": "Scope records independently (its own ADS acquisition and timestamps); it is stopped, saved and "
                        "exported when the recording ends — by itself (duration, stop_when, post_trigger, max_events …) "
                        "or at rec_stop"}

    def _marked_start(name: str | None) -> Callable[[], dict[str, Any]]:
        """StartRecord with the Scope View log's mark taken in the same XAE job, right before it (review 1 of 0.8.18:
        the mark was taken before the call queued — another start meanwhile, its 'Config → Record' line confirmed a
        start Scope refused). -> (mark, StartRecord's result)."""
        from . import scope as S

        def scope_action() -> dict[str, Any]:
            mark = S.log_mark(_view_log())
            # review 2 of 0.8.18: a dict, so xae_call adds open_dialogs / xae_messages to it (a tuple lost them)
            return {**app.xae.scope_action("start", name, None), "_log_mark": mark}
        return scope_action

    def _did_not_run(exc: BaseException) -> bool:
        """A cancellation of an XAE call that never ran: the worker withdrew the queued job, or it came while a
        refusal (XaeBusy / not started) was being handled."""
        e: BaseException | None = exc
        for _ in range(6):
            if e is None:
                return False
            if getattr(e, "job_withdrawn", False) or isinstance(e, (XaeBusy, _NotRun, WorkerBusy)):
                return True
            e = e.__cause__ or e.__context__
        return False

    SCOPE_START_CHECK_S = 2.0
    # N186: what the Scope View logs when StartRecord is refused ('Invalid ScopeState! Config requested, actual- Reply')
    # or when the start goes ahead (the Scope project leaves Config)
    _START_SEEN = re.compile(r"Invalid\s+ScopeState!\s*Config requested,\s*actual\W*(\w+)|"
                             r"Scope Project state changes from 'Config' to '(\w+)'", re.I)

    async def _start_check(mark: dict[str, Any] | None) -> tuple[str | None, str]:
        """N186 (0.8.16 retest): StartRecord's return value does not tell (1 when Scope records, 1 when it refused) —
        the Scope View log does. -> (the state Scope refused the start in, its log line) or (None, how the start was
        checked). Waits up to SCOPE_START_CHECK_S for either line."""
        from . import scope as S
        t_end = time.monotonic() + SCOPE_START_CHECK_S
        while True:
            found = await asyncio.to_thread(S.log_find_since, mark, _START_SEEN)
            if found is None:
                return None, "the Scope View log could not be read: whether Scope accepted StartRecord is not known"
            for _f, ln in found:
                m = _START_SEEN.search(ln)
                if m and m.group(1):
                    return m.group(1), ln[:300]
                if m:
                    return None, f"confirmed by the Scope View log: {ln[:200]}"
            if time.monotonic() >= t_end:
                return None, (f"no line of the Scope View log within {SCOPE_START_CHECK_S:g} s confirms or refuses the "
                              "start")
            await asyncio.sleep(0.2)

    def _start_refused_text(state: str, line: str, rc: Any, rid: str | None) -> str:
        return (f"Scope recording NOT started: Scope refused StartRecord — it is in state {state}, not Config "
                f"(Scope View log: {line!r}; StartRecord returned {rc}). Scope still holds an earlier recording: save it "
                "if it is needed (scope_record(action='save')), then scope_record(action='disconnect') — or Disconnect "
                "in XAE's Scope View; it discards what Scope holds — Scope is back in Config and records again."
                + (f" The ADS recording {rid} runs regardless — do not repeat rec_start (it would start a second "
                   "recording)." if rid else ""))

    SCOPE_WATCH_S = 0.5

    def _bg(coro: Any) -> Any:
        """A background task of the Scope part, in a context of its own — thirteenth review of 0.8.16: a task inherited
        rec_start's XAE steps ('this call's earlier step(s) ran (Scope StartRecord)' in a stop minutes later)."""
        return asyncio.get_running_loop().create_task(coro, context=contextvars.Context())

    # thirteenth review of 0.8.16: who ended a recording is the recorder's own stop reason (both engines keep the first
    # reason given) — a tool's stop is never an end of its own, whether or not the tool was still there to finish Scope
    _STOPPED_BY = {"manual (rec_stop)": "rec_stop", "deleted": "rec_delete", "manual (rec_profile_stop)": "rec_profile_stop"}

    def _end_label(reason: Any, who: str | None = None) -> str:
        tool = _STOPPED_BY.get(str(reason or "")) or (None if reason else who)
        if tool == "rec_profile_stop":
            # first review of 0.8.17: rec_profile_stop has no Scope part — no call 'ended before' it
            return "rec_profile_stop (it does not finish Scope — finished after the recording stopped)"
        if tool:
            return f"{tool} (the call ended before its Scope part — finished after it)"
        return f"the recording ended by itself ({reason or 'no stop reason recorded'})"

    class _ScopePart:
        """N182: the Scope part of one recording, owned by ONE task (0.8.17). Everything that decides it goes through
        that task in order: the recording ended (the task watches it), rec_stop / rec_delete asked to finish it, a stop
        XAE refused (it did not run) asked again by the next rec_stop. In 0.8.16 four actors — the watch, rec_stop,
        rec_delete and the finish task — shared five tables, and each review round found another interleaving.

        state: starting (StartRecord under way: requests wait) → recording → finishing → finished; stop_refused (XAE
        refused StopRecord: Scope still records, the next rec_stop asks again — also after the recording was deleted);
        stop_unknown (StopRecord pending in a dialog or timed out: it may have run — never sent again). holds:
        rec_stop / rec_delete calls stopping the recording now — the watch leaves the end to them, and takes it up again
        when they leave without finishing (cancelled, timed out, failed). idle: no finish is under way."""

        TERMINAL = ("finished", "stop_unknown")

        def __init__(self, rid: str, run: dict[str, Any]) -> None:
            self.rid = rid
            self.run = run
            self.state = "starting"
            self.holds = 0
            self.by: str | None = None
            self.result: dict[str, Any] | None = None
            self.attempts = 0                 # finishes run so far (a refused stop is asked again)
            self.warned = False
            self.queue: asyncio.Queue = asyncio.Queue()
            self.idle = asyncio.Event()
            self.idle.set()
            # review 1 of 0.8.18: whether the recording is kept and the signed link are one step — rec_delete's delete
            # waits for it, or it for the delete (a delete between the check and the link left a link to nothing)
            self.link_lock = asyncio.Lock()
            self.task: Any = None

        def begin(self) -> None:
            """StartRecord ran (or its outcome is not known): the owner takes over — requests queued meanwhile first."""
            self.state = "recording"
            self.task = _bg(self._own())

        def not_started(self, error: str) -> None:
            """StartRecord did not run or failed: nothing to finish — waiting requests get the error."""
            self.state, self.result = "finished", {"scope": self.run.get("scope"), "error": error}
            while not self.queue.empty():
                _b, _s, f = self.queue.get_nowait()
                if not f.done():
                    f.set_result(self.result)

        def hold(self) -> None:
            self.holds += 1

        def release(self) -> None:
            self.holds = max(0, self.holds - 1)

        async def finish(self, by: str, save: bool = True) -> tuple[dict[str, Any] | None, bool]:
            """-> (the part's result, whether a finish ran for this request). The finish itself runs in the owner task:
            a caller that is cancelled meanwhile leaves it running, and its result is kept."""
            if self.state in self.TERMINAL or (self.task is not None and self.task.done()):
                return self.result, False      # finished (or the owner is gone: the server is shutting down)
            n0 = self.attempts
            fut = asyncio.get_running_loop().create_future()
            self.queue.put_nowait((by, save, fut))
            res = await asyncio.shield(fut)
            return res, self.attempts > n0

        async def _own(self) -> None:
            while True:
                try:
                    try:
                        req = await asyncio.wait_for(self.queue.get(), SCOPE_WATCH_S)
                    except (TimeoutError, asyncio.TimeoutError):
                        req = None
                    if req is not None:
                        by, save, fut = req
                        if self.state not in self.TERMINAL:
                            await self._finish(by, save)
                        if not fut.done():
                            fut.set_result(self.result)
                    elif self.state == "recording" and not self.holds:
                        end = await self._ended()
                        # first review of 0.8.17: a rec_stop / rec_delete that came while the status was read owns
                        # this end — it was labelled as a call that had ended (0.8.16 checked again here)
                        if end is not None and not self.holds and self.state == "recording":
                            await self._finish(*end)
                except Exception:  # noqa: BLE001 — the owner never dies with the part unfinished
                    log.exception("Scope part of %s: owner step failed", self.rid)
                    await asyncio.sleep(SCOPE_WATCH_S)
                if self.state in self.TERMINAL:
                    while not self.queue.empty():
                        _b, _s, f = self.queue.get_nowait()
                        if not f.done():
                            f.set_result(self.result)
                    return

        async def _ended(self) -> tuple[str, bool] | None:
            """None while the recording runs; else (who ended it, whether Scope's files are saved)."""
            rid = self.rid
            if rid in app.rec.deleting:
                return None               # a delete is under way (its call may have gone): its outcome decides
            try:
                st = await app.mem_call(app.rec.status, rid)
            except Exception as exc:  # noqa: BLE001
                if rid in app.rec.recs:
                    # fourteenth review of 0.8.16: an error of the status call is no end of the recording
                    if not self.warned:
                        log.warning("Scope part of %s: the recording's status failed (%s) — watched on", rid, exc)
                        self.warned = True
                    return None
                # only rec_delete takes a recording from the recorder: its call ended before the Scope part
                return _end_label(None, "rec_delete"), False
            if st.get("active"):
                return None
            return self.ended(st.get("stop_reason")), True

        def ended(self, reason: Any) -> str:
            """Who ended the recording, by its stop reason; an end of its own is kept for the link (recording_ended)."""
            if str(reason or "") not in _STOPPED_BY:
                self.run.setdefault("recording_ended", {
                    "seen": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds"),
                    "stop_reason": reason})
            return _end_label(reason)

        async def _finish(self, by: str, save: bool) -> None:
            self.state, self.by = "finishing", by
            self.idle.clear()
            try:
                await self._finish_run(by, save)
            finally:
                self.idle.set()

        async def _finish_run(self, by: str, save: bool) -> None:
            self.run["finished_by"] = by
            self.attempts += 1
            # this finish's own XAE steps: a refused step after one that ran is no 'repeat it' (fourteenth review)
            _XAE_STEPS.set([])
            try:
                out = await _scope_finish(self, save)
            except Exception as exc:  # noqa: BLE001
                log.exception("Scope finish of %s failed", self.rid)
                out = {"scope": self.run.get("scope"),
                       "error": f"the Scope part failed ({type(exc).__name__}: {exc}) — the ADS recording {self.rid} "
                                "is stopped and complete; check in XAE whether Scope stopped and saved (scope_list, "
                                "scope_record)"}
                self.state = "finished"
            out["finished_by"] = by
            try:
                # N116: the binding's flags of the XAE calls this finish made (fourteenth review: the part runs in its
                # own task, and rec_stop's result lost them)
                out.update({k: v for k, v in app.xae_flags().items() if k not in out})
            except Exception:  # noqa: BLE001
                pass
            self.result = out
            if self.state == "finishing":
                self.state = "finished"

    def _rec_on_disk(rid: str) -> bool:
        rec = app.rec.recs.get(rid)
        return (rec.path if rec is not None else app.rec.root / f"{rid}.jsonl").is_file()

    def _rec_id(rid: str) -> str:
        """The recording's full id (the recorder takes a unique prefix — twelfth review: a rec_stop by prefix finished
        no Scope part and the watch took the stop for an end of its own)."""
        try:
            return str(app.rec._get(rid).id)
        except Exception:  # noqa: BLE001 — the recorder says it in its own error
            return rid

    # N189 (0.8.17 retest): two rec_start(also_scope=true) at once each got a Scope part of the ONE Scope — B's stopped
    # Scope at B's end and saved A's capture, A saved the same capture again, both signed. One owner of Scope at a
    # time: a recording's part (until it is finished), a call starting it (from its check on, no await in between),
    # a Scope recording on its own. Everything that would start, stop, save or discard Scope under another owner is
    # refused before it acts.
    SCOPE_HOLDING = ("starting", "recording", "stop_refused", "finishing")

    def _scope_holder() -> dict[str, Any] | None:
        for rid, part in list(app.scope_parts.items()):
            if part.state in SCOPE_HOLDING or (part.state == "stop_unknown" and not part.run.get("released_by")):
                return {"rid": rid, "state": part.state}
        if app.scope_claim:
            return {"rid": None, "state": "claimed", "by": app.scope_claim.split(" #", 1)[0]}
        if app.scope_standalone:
            return {"rid": None, "state": "standalone", **app.scope_standalone}
        return None

    def _scope_owner_text(h: dict[str, Any]) -> str:
        if h.get("rid"):
            rid, st = h["rid"], h["state"]
            how = {"stop_refused": f"its StopRecord was refused (XAE busy or a dialog) — rec_stop(id='{rid}') asks again",
                   "stop_unknown": "its StopRecord's outcome is unknown (a dialog held it, it timed out, or the shell "
                                   "went away / failed after it acted): Scope may "
                                   "still record — check XAE's Scope View; scope_record(action='stop') or "
                                   "action='disconnect' takes Scope over"}.get(
                st, f"it ends with that recording (rec_status(id='{rid}'), or rec_stop(id='{rid}'))")
            return f"Scope belongs to recording {rid} (its Scope part: {st}) — {how}"
        if h["state"] == "claimed":
            return f"Scope is being started by another call ({h.get('by')}) — try again once that call has returned"
        return (f"Scope records on its own (scope_record {h.get('action', 'start')}, {h.get('since')}) — "
                "scope_record(action='stop') stops it first (when StopRecord fails — Scope reset to Config by a "
                "TwinCAT restart — scope_record(action='disconnect') frees it)")

    def _scope_claim(by: str) -> str:
        """Check and claim Scope in one step (no await between them): -> the claim; ToolError when it has an owner."""
        h = _scope_holder()
        if h is not None:
            raise ToolError(f"{_scope_owner_text(h)}. One Scope records for one owner at a time.")
        app.scope_claim = f"{by} #{secrets.token_hex(4)}"
        return app.scope_claim

    def _scope_record_allowed(action: str) -> None:
        """scope_record under another owner: refused before it acts (N189)."""
        h = _scope_holder()
        if h is not None and (h.get("rid") and h["state"] != "stop_unknown" or h["state"] == "claimed"
                              or (h["state"] == "stop_unknown" and action == "start")
                              or (h["state"] == "standalone" and action == "start")):
            what = {"start": "start Scope a second time", "stop": "stop Scope under its owner — the owner's own stop "
                    "would then save data it did not stop", "disconnect": "discard what Scope records for its owner",
                    "new": "discard what Scope records for its owner", "save": "save Scope's data while it records "
                    "for its owner"}[action]
            raise ToolError(f"{_scope_owner_text(h)}. scope_record(action='{action}') would {what}: nothing was done.")

    def _scope_unclaim(claim: str | None) -> None:
        if claim is not None and app.scope_claim == claim:
            app.scope_claim = None

    def _scope_state(rid: str) -> dict[str, Any] | None:
        """rec_status / rec_list: the Scope part of a recording — recording, finishing, finished, stop_unknown (N182)."""
        part = app.scope_parts.get(rid)
        if part is None:
            return None
        r = part.result or {}
        if part.state == "recording":
            out: dict[str, Any] = {"state": "recording", "scope": part.run.get("scope"), "started": part.run.get("started")}
            if part.run.get("start_unknown"):
                out["start_unknown"] = part.run["start_unknown"][:400]
            return out
        if part.state == "stop_refused":
            return {"state": "recording", "scope": r.get("scope"), "stop_refused": str((r.get("stop") or {}).get("error"))[:400],
                    "note": r.get("note")}
        if part.state == "finishing":
            return {"state": "finishing", "finished_by": part.by}
        if part.state == "starting":
            return {"state": "starting", "scope": part.run.get("scope"), "start_asked": part.run.get("start_asked")}
        out = {"state": part.state, "finished_by": r.get("finished_by")}
        for k in ("scope", "error", "note"):
            if r.get(k):
                out[k] = r[k]
        if part.state == "stop_unknown":
            out["stop_error"] = str((r.get("stop") or {}).get("error"))[:400]
        if isinstance(r.get("link"), dict):
            out["files"] = sorted((r["link"].get("files") or {}).keys())
        if isinstance(r.get("scope_view_log"), dict):
            out["scope_view_log_errors"] = r["scope_view_log"].get("error_count")
        return out

    def _orphan(id: str) -> tuple[str, Any] | None:
        """(id, part) of a Scope part whose recording was deleted and that is not finished — by its id or a unique
        prefix, as the other rec_* tools take it; None for a recording the recorder still holds (second review of
        0.8.17: rec_status said a live recording was deleted when its status failed)."""
        if id in app.rec.recs or any(r.startswith(id) for r in list(app.rec.recs)):
            return None
        hits = [(r, p) for r, p in list(app.scope_parts.items())
                if (r == id or r.startswith(id)) and p.state != "finished" and r not in app.rec.deleting]
        exact = [h for h in hits if h[0] == id]
        return exact[0] if exact else hits[0] if len(hits) == 1 else None

    def _scope_answer(res: dict[str, Any], got: tuple[dict[str, Any] | None, bool]) -> None:
        """The Scope part's result on a rec_stop / rec_delete result; when a finish ran for this call, the call used
        the XAE shell — its result carries the binding's flags (N116; fourteenth review of 0.8.16)."""
        sc, ran = got
        if sc is None:
            return
        res["scope"] = sc
        if ran:
            _XAE_USED.set(True)
            for k in ("solution_mismatch", "solution_note", "reattached"):
                if k in sc:
                    res.setdefault(k, sc[k])

    async def _scope_save(name: str | None, svdx: Path, csv: Path, overwrite: bool = False,
                          mark: dict[str, Any] | None = None, since: str = "the recording began",
                          exists_hint: str = "overwrite=true") -> dict[str, Any]:
        """SaveSVD, then the CSV FROM THE SAVED FILE with Beckhoff's export tool (N117: XAE's ExportCSV right after
        SaveSVD wrote a CSV that lacked the end of the .svdx — 600 ms, and 3 rows of a 58 s recording; exported from the
        .svdx, the CSV holds what the file holds). XAE's ExportCSV only when the tool is missing or fails, flagged.
        N117 (0.8.13 retest): on another recording the saved .svdx was 34 rows SHORTER than what Scope still had in
        memory — so both are compared every time: XAE's ExportCSV of the in-memory data is written beside it
        (<name>.xae.csv) and the row counts are reported (cross_check), whichever is longer."""
        from . import scope as S
        out: dict[str, Any] = {}
        try:
            r = await app.xae_call(app.xae.scope_action, "save_svd", name, str(svdx), timeout=600)
            out["save_svd"] = {k: r[k] for k in ("result_code", "path", "warning", "result_note") if k in r}
        except ToolError as exc:
            st = re.search(r"Invalid\s+ScopeState!.*?actual:\s*([A-Za-z]+)", str(exc))
            if st is None:
                out["save_svd"] = {"error": str(exc)}
                if isinstance(exc, _NotRun):
                    # fourteenth review of 0.8.16: SaveSVD refused — it did not run, what Scope holds is still there
                    out["save_svd"]["not_run"] = True
                    out["note"] = ("SaveSVD did not run (XAE refused it): what Scope recorded is still in Scope — "
                                   "scope_record(action='save') saves it once XAE is free")
                return out
            return await _scope_reset(name, csv, overwrite, mark, st.group(1), str(exc), since, exists_hint)
        watch = await asyncio.to_thread(_settled, svdx)
        out["save_svd"]["size_watch"] = watch
        if watch.get("still_growing_after_s") or "note" in watch:
            # scope audit (0.8.13): the export ran on a file still being written — its CSV could lack the end
            out["export_csv"] = {"skipped": "the saved .svdx " + ("was still being written after "
                                 f"{watch.get('still_growing_after_s')} s" if watch.get("still_growing_after_s") else
                                 "could not be watched") + " — nothing was exported from it; export it when it is "
                                 f"complete: scope_export(svd={str(svdx)!r})"}
            out["svdx_incomplete"] = out["export_csv"]["skipped"]
            return out
        tool = await asyncio.to_thread(S.find_tool, app.cfg.scope.export_tool)
        failed = None
        if tool is not None:
            try:
                e = await asyncio.to_thread(S.export, svdx, csv, tool=tool, overwrite=overwrite, key=app.rec.key,
                                            version=__version__,
                                            exists_hint=None if exists_hint == "overwrite=true" else exists_hint)
                out["export_csv"] = {"path": e["file"], "via": "TC3ScopeExportTool.exe from the saved .svdx",
                                     "lines": e["lines"], "provenance": e["provenance"],
                                     **{k: e[k] for k in ("warning", "exported_from_copy") if k in e}}
                out["cross_check"] = await _scope_cross_check(name, csv, e["lines"], overwrite, exists_hint)
                return out
            except S.ScopeError as exc:
                failed = str(exc)
        if csv.exists() and not overwrite:
            # scope audit (0.8.13): the fallback wrote over an existing CSV (or counted the stale one as written)
            out["export_csv"] = {"error": f"{csv.name} exists — XAE's ExportCSV was not run over it ({exists_hint})",
                                 "export_tool": failed}
            return out
        try:
            r = await app.xae_call(app.xae.scope_action, "export_csv", name, str(csv), timeout=600)
            out["export_csv"] = {**{k: r[k] for k in ("result_code", "path", "warning", "result_note") if k in r},
                                 "via": "XAE ExportCSV",
                                 "check": ("XAE's ExportCSV wrote a CSV shorter than the .svdx in the 0.8.12 retest "
                                           "(N117) — compare with scope_export of the .svdx")}
        except ToolError as exc:
            out["export_csv"] = {"error": str(exc)}
        out["export_csv"]["export_tool"] = failed or ("TC3ScopeExportTool.exe not found — "
                                                      + S.status(app.cfg.scope.export_tool)["note"])
        return out

    async def _scope_reset(name: str | None, csv: Path, overwrite: bool, mark: dict[str, Any] | None, state: str,
                           raw: str, since: str = "the recording began",
                           exists_hint: str = "overwrite=true") -> dict[str, Any]:
        """N180 (0.8.15 retest): after a TwinCAT RUN→CONFIG→RUN during the recording, SaveSVD answered 'Invalid
        ScopeState! Reply requested, actual: Config (HRESULT 0x80020009)' — a raw COM text, no .svdx, no CSV. Said as
        what it is (Scope was reset by the restart, at the time its log says), and XAE's ExportCSV of what Scope still
        holds is tried: recovered (a CSV, no .svdx) or the run's Scope data is lost — never left unsaid."""
        from . import scope as S
        # twelfth review of 0.8.16: a reset is a change to Config (or the server's reset) — a change to the state Scope
        # is in now is no restart ('actual: Record' matched the recording's own start); and the log is said read only
        # when it was (scope_record save had no mark: 'names no reset since the recording began', with no recording)
        rx = re.compile(r"state changes from '[^']*' to 'Config'|ServerReset", re.I)
        # thirteenth review of 0.8.16: the log is searched only for a reset to Config, and said as read only when it
        # was — a mark taken when the log could not be read, a log unreadable now: None (log_find_since)
        config = state.lower() == "config"
        found = await asyncio.to_thread(S.log_find_since, mark, rx) if config else None
        lines = found or []
        when = None
        for _f, ln in lines:
            t = re.match(r"\((\d{4}-\d\d-\d\d)-(\d\d:\d\d:\d\d)", ln)
            if t:
                when = f"{t.group(1)} {t.group(2)}"
        # eleventh review of 0.8.16: the cause only when the Scope View log says it — any other 'Invalid ScopeState'
        # (actual: Record, no reset line) is said as the state Scope is in, no restart invented
        cause = (f"Scope was reset to {state} by a TwinCAT restart at {when} (Scope View log)" if lines and when else
                 f"Scope was reset to {state} (Scope View log)" if lines else
                 f"Scope is in state {state}, not Reply — the Scope View log names no reset since {since}"
                 if config and found is not None else
                 f"Scope is in state {state}, not Reply (the Scope View log is missing or could not be read: whether "
                 "TwinCAT was restarted is not known)" if config else f"Scope is in state {state}, not Reply")
        out: dict[str, Any] = {"save_svd": {
            "error": cause + " — SaveSVD refused; nothing was saved as .svdx",
            "scope_state": state, "detail": raw[:500]}}
        if lines:
            out["save_svd"]["reset_seen"] = [ln[:200] for _f, ln in lines[-3:]]
        what = f"Scope was reset to {state}" if lines else f"Scope was in state {state}"
        if csv.exists() and not overwrite:
            out["export_csv"] = {"skipped": f"{csv.name} exists — not written over ({exists_hint})"}
            return out
        try:
            r = await app.xae_call(app.xae.scope_action, "export_csv", name, str(csv), timeout=600)
            rows = await asyncio.to_thread(_csv_samples, csv) if csv.is_file() else 0
        except (ToolError, OSError) as exc:
            out["export_csv"] = {"error": str(exc)[:300], "lost": (
                f"the Scope data of this run is lost: {what} and refused both SaveSVD and ExportCSV")}
            return out
        if rows > 0:
            out["export_csv"] = {**{k: r[k] for k in ("result_code", "path") if k in r}, "via": "XAE ExportCSV",
                                 "samples": rows, "recovered": (
                                     f"what Scope still held in {state} state, exported by XAE's ExportCSV — no .svdx; "
                                     "whether it covers the whole run is not known — compare its time span with the "
                                     "recording")}
        else:
            out["export_csv"] = {"samples": 0, "lost": (f"the Scope data of this run is lost: in {state} state Scope "
                                                        "exported no samples")}
        return out

    def _csv_samples(p: Path) -> int:
        """The data rows of a Scope CSV: the lines that begin with a number (its header lines do not)."""
        with open(p, "rb") as fh:
            return sum(1 for ln in fh if re.match(rb"\s*-?\d", ln))

    def _csv_rows(p: Path) -> int:
        with open(p, "rb") as fh:
            return sum(1 for _ in fh)

    async def _scope_cross_check(name: str | None, csv: Path, svdx_lines: int, overwrite: bool,
                                 exists_hint: str = "overwrite=true") -> dict[str, Any]:
        """N117: XAE's ExportCSV of what Scope holds in memory, beside the CSV of the saved .svdx, and how their row
        counts compare — evidence either way (0.8.12: the in-memory CSV lacked the end; 0.8.13: the .svdx did)."""
        xcsv = csv.with_name(csv.name[:-4] + ".xae.csv") if csv.name.lower().endswith(".csv") else csv.with_suffix(".xae.csv")
        if xcsv.exists() and not overwrite:
            return {"skipped": f"{xcsv.name} exists ({exists_hint})"}
        try:
            await app.xae_call(app.xae.scope_action, "export_csv", name, str(xcsv), timeout=600)
            xl = await asyncio.to_thread(_csv_rows, xcsv)
        except (ToolError, OSError) as exc:
            return {"error": f"XAE's ExportCSV of the in-memory data failed: {exc}"}
        d: dict[str, Any] = {"svdx_export_lines": svdx_lines, "xae_export": str(xcsv), "xae_export_lines": xl,
                             "differ_by": xl - svdx_lines}
        if xl != svdx_lines:
            d["warning"] = (f"the CSV of the saved .svdx has {abs(xl - svdx_lines)} line(s) "
                            + ("fewer" if xl > svdx_lines else "more")
                            + " than XAE's ExportCSV of what Scope held in memory — "
                            + ("the saved file may lack the end of the recording" if xl > svdx_lines else
                               "the in-memory export lacks part of what was saved")
                            + " (N117); both files are kept")
        return d

    def _settled(p: Path) -> dict[str, Any]:
        """Whether SaveSVD's file still grew after the call returned (not documented either way): its size watched
        until it stayed the same for quiet_s — what that shows, no more (an unchanged size is not proof the writer is
        done)."""
        quiet_s, limit_s = SCOPE_SETTLE_S
        t0 = time.monotonic()
        try:
            last = p.stat().st_size
        except OSError as exc:
            return {"note": f"the saved file could not be read after SaveSVD returned: {exc}"}
        grew = False
        while True:
            time.sleep(quiet_s)
            try:
                now = p.stat().st_size
            except OSError as exc:
                return {"note": f"the saved file disappeared while it was watched: {exc}"}
            if now == last:
                return {"bytes": now, "grew_after_save": grew, "unchanged_for_s": quiet_s,
                        **({"grew_for_s": round(time.monotonic() - t0 - quiet_s, 2)} if grew else {})}
            grew, last = True, now
            if time.monotonic() - t0 > limit_s:
                return {"bytes": now, "grew_after_save": True, "still_growing_after_s": round(limit_s, 1),
                        "warning": "the .svdx was still being written — the CSV may lack its end"}

    SCOPE_STALE_S = 30.0

    def _scope_not_this(rid: str, run: dict[str, Any], csv: Path) -> str | None:
        """N186: why the Scope CSV just saved is not this recording's — its data ends more than SCOPE_STALE_S before
        the recording's first sample (the target's clock, as Scope's) and before StartRecord ran (this PC's clock);
        None when it is, or when that cannot be told (no CSV header, no sample yet)."""
        from . import scope as S
        span = S.csv_span(csv) if csv.is_file() else None
        if span is None:
            return None
        t0 = app.rec.t0_filetime(rid)
        end, tol = span[1], int(SCOPE_STALE_S * 1e7)
        try:
            host = int((dt.datetime.fromisoformat(run["started"]) - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc))
                       .total_seconds() * 1e7) if run.get("started") else None
        except (TypeError, ValueError):
            host = None
        if t0 is not None:
            # the recording's first sample and Scope's data: the target's clock both (review 1 of 0.8.18: also taking
            # this PC's clock passed an old capture when the target's clock runs ahead)
            if end >= t0 - tol:
                return None
            return (f"its data ends at {S.filetime_iso(end)}, {(t0 - end) / 1e7:.0f} s before the recording's first "
                    f"sample ({S.filetime_iso(t0)}) — an earlier capture Scope still held")
        if str(run.get("start_check") or "").startswith("confirmed"):
            # no PLC time (poll mode): the Scope View log saw this start — what Scope saved is this run's, whatever the
            # two clocks say (review 1 of 0.8.18: a target clock behind the PC's refused the recording's own capture)
            return None
        if host is None and run.get("start_asked"):
            try:                           # a start of unknown outcome: when it was asked (review 1 of 0.8.18)
                host = int((dt.datetime.fromisoformat(run["start_asked"]) - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc))
                           .total_seconds() * 1e7)
            except (TypeError, ValueError):
                host = None
        if host is None or end >= host - tol:
            return None                    # no PLC time (poll mode, no sample yet): StartRecord's time on this PC only
        # review 1 of 0.8.18: said 'an earlier capture' — the target's clock behind this PC's gives the same picture
        return (f"its data ends at {S.filetime_iso(end)}, {(host - end) / 1e7:.0f} s before StartRecord ran (this PC's "
                "clock — the recording has no PLC time and the Scope View log did not confirm the start): an earlier "
                "capture Scope still held, or the target's clock differs from this PC's — which, cannot be told")

    SCOPE_SPAN_TOL_S = 1.0                 # the 0.8.17 retest: Scope begins / ends 0.2 – 0.4 s after the recording

    def _scope_span(rid: str, csv: Path) -> dict[str, Any] | None:
        """N189 / N191 (0.8.17 retest): Scope's capture (the CSV's 'Starttime / Endtime of export') against the
        recording's window (its first sample + window_start / window_end of its footer) — both the target's clock. A
        capture that begins later, ends earlier or runs past the recording by more than SCOPE_SPAN_TOL_S is described;
        None when it matches, or when that cannot be told (no CSV header, no PLC time, no footer window)."""
        from . import scope as S
        span = S.csv_span(csv) if csv.is_file() else None
        t0 = app.rec.t0_filetime(rid)
        if span is None or t0 is None:
            return None
        try:
            ft = K_footer(app.rec._path_for(rid)) or {}
        except Exception:  # noqa: BLE001 — no footer: nothing to compare with
            return None
        wend, wstart = ft.get("window_end_ms"), ft.get("window_start_ms")
        if not isinstance(wend, (int, float)):
            return None
        r0 = t0 + int(float(wstart) * 1e4) if isinstance(wstart, (int, float)) and wstart > 0 else t0
        r1 = t0 + int(float(wend) * 1e4)
        s0, s1 = span
        tol = SCOPE_SPAN_TOL_S * 1e7
        out: dict[str, Any] = {"scope_from": S.filetime_iso(s0), "scope_to": S.filetime_iso(s1),
                               "recording_from": S.filetime_iso(r0), "recording_to": S.filetime_iso(r1)}
        said = []
        if s0 - r0 > tol:
            out["starts_after_s"] = round((s0 - r0) / 1e7, 1)
            said.append(f"began {out['starts_after_s']} s after the recording")
        if r1 - s1 > tol:
            out["ends_before_s"] = round((r1 - s1) / 1e7, 1)
            said.append(f"ended {out['ends_before_s']} s before it")
        if s1 - r1 > tol:
            out["runs_past_s"] = round((s1 - r1) / 1e7, 1)
            said.append(f"ran {out['runs_past_s']} s past its end")
        if not said:
            return None
        out["covers_s"] = round(max(0, min(s1, r1) - max(s0, r0)) / 1e7, 1)
        out["recording_s"] = round((r1 - r0) / 1e7, 1)
        out["note"] = (f"Scope's capture covers {out['covers_s']} s of the recording's {out['recording_s']} s: it "
                       + ", ".join(said))
        return out

    async def _link_if_kept(part: Any, svdx: Path, csv: Path, out: dict[str, Any]) -> bool:
        """The signed link, written only while the recording is kept — under the part's link_lock, which rec_delete's
        delete takes too. -> whether it was written."""
        rid, run = part.rid, part.run
        async with part.link_lock:
            if not await _kept(rid, True):
                return False
            span = await asyncio.to_thread(_scope_span, rid, csv)
            if span is not None:
                # N189 / N191 (0.8.17 retest): a capture that began late, ended early or ran past the recording is
                # said — in the result and in the signed link
                run["span"] = out["scope_span"] = span
                out["note"] = (out["note"] + " · " if out.get("note") else "") + span["note"]
            out["link"] = await asyncio.to_thread(_scope_link, rid, run, svdx, csv, out.get("svdx_incomplete"))
            return True

    async def _kept(rid: str, save: bool) -> bool:
        """Whether Scope's files are saved with the recording: not for one that was deleted — a delete under way
        (rec_delete, also one whose call has gone) has its outcome first."""
        while rid in app.rec.deleting:
            await asyncio.sleep(0.1)
        return save and await asyncio.to_thread(_rec_on_disk, rid)

    async def _scope_finish(part: Any, save: bool = True) -> dict[str, Any]:
        """Stop Scope, save its .svdx and CSV and sign the link to the recording — in the part's owner task. Sets
        part.state when the result is not 'finished' (stop_refused, stop_unknown)."""
        from . import scope as S
        rid, run = part.rid, part.run
        d = _scope_dir()
        svdx, csv = d / f"{rid}.svdx", d / f"{rid}.scope.csv"
        out: dict[str, Any] = {"scope": run["scope"]}
        if run.get("start_unknown"):
            out["start_unknown"] = run["start_unknown"][:400]
        try:
            r = await app.xae_call(app.xae.scope_action, "stop", run["name"], None, timeout=600)
            out["stop"] = {k: r[k] for k in ("result_code", "warning", "result_note") if k in r}
            if not run.get("scope") and r.get("scope"):
                run["scope"] = out["scope"] = r["scope"]       # a start whose outcome was not known: the Scope it is
        except _NotRun as exc:
            # thirteenth / fourteenth review of 0.8.16: StopRecord did not run (XAE refused it, a dialog held XAE, the
            # worker was busy) — Scope still records; the next rec_stop asks again
            out["stop"] = {"error": str(exc), "not_run": True}
            # first review of 0.8.17: also for a recording that was deleted — the part stays, rec_list lists it and
            # rec_stop(id) asks again (a note no tool could show left Scope recording with nothing to stop it)
            part.state = "stop_refused"
            gone = not await _kept(rid, True)              # a delete under way has its outcome first
            out["note"] = (f"Scope did not stop: XAE refused StopRecord (it did not run) — rec_stop(id='{rid}') asks "
                           "again" + (" (the recording was deleted: nothing will be saved)" if gone else ""))
            return out
        except _Pending as exc:
            # fourteenth review of 0.8.16: a StopRecord a dialog holds, or that timed out, may have run — it is never
            # sent again, and Scope is not said to be stopped or not
            out["stop"] = {"error": str(exc), "outcome_unknown": True}
            out["note"] = ("whether Scope stopped is not known: StopRecord is pending inside XAE (a dialog) or timed out "
                           "— it is not sent again (it may have run). Check Scope in XAE; once it has stopped, "
                           "scope_record(action='save') saves what it recorded")
            part.state = "stop_unknown"
            return out
        except ToolError as exc:
            out["stop"] = {"error": str(exc)}
            out["note"] = "StopRecord failed — Scope may still record: check it in XAE and save it by hand (scope_record)"
        else:
            # thirteenth / fourteenth review of 0.8.16: Scope's files belong to the recording — decided after the stop,
            # so a delete under way has its outcome first; none are saved for a recording that was deleted
            if not await _kept(rid, save):
                out["note"] = "the recording was deleted: Scope was stopped, nothing was saved"
                app.audit("scope_finish", {"id": rid, "scope": run["scope"], "saved": False}, OpClass.EDIT, "ok")
                out["scope_stopped"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
                _scope_holds(rid, run, None)
                return out
            out.update(await _scope_save(run["name"], svdx, csv, mark=run.get("log_mark")))
            stale = await asyncio.to_thread(_scope_not_this, rid, run, csv)
            if stale:
                # N186 (0.8.16 retest): an earlier capture Scope still held was saved and signed as this recording's
                out["not_this_recording"] = stale
                out["note"] = ((f"the Scope data saved could not be shown to be this recording's: {stale}"
                                if stale.endswith("cannot be told") else
                                f"the Scope data saved is not this recording's: {stale}")
                               + " — its files are kept, not linked to the recording nor sealed")
                app.audit("scope_finish", {"id": rid, "scope": run["scope"], "linked": False}, OpClass.EDIT, "error",
                          stale)
            elif await _link_if_kept(part, svdx, csv, out):
                app.audit("scope_finish", {"id": rid, "scope": run["scope"], "files": out["link"].get("files")},
                          OpClass.EDIT, "ok")
            else:
                # first review of 0.8.17: the recording was deleted while Scope was saved — no signed link to a
                # recording that is gone; the files written are kept (never deleted by the server), unsealed
                left = sorted(p.name for p in (svdx, csv, csv.with_name(csv.name[:-4] + ".xae.csv")) if p.is_file())
                out["note"] = ("the recording was deleted while Scope was saved: its files are kept, not linked to it "
                               f"nor sealed: {left}")
                app.audit("scope_finish", {"id": rid, "scope": run["scope"], "files": left, "linked": False},
                          OpClass.EDIT, "ok")
        if run.get("log_mark"):
            out["scope_view_log"] = await asyncio.to_thread(S.log_errors_since, run["log_mark"])
        if run.get("recording_ended"):
            out["recording_ended"] = run["recording_ended"]
        out["scope_stopped"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")
        if "error" not in out["stop"]:
            _scope_holds(rid, run, svdx if svdx.is_file() and "error" not in (out.get("save_svd") or {}) else None)
        return out

    def _scope_holds(rid: str, run: dict[str, Any], saved: Path | None) -> None:
        """StopRecord ran: Scope is in Reply with this recording's data until it is disconnected (N186)."""
        app.scope_left = {"rid": rid, "scope": run.get("scope"), "name": run.get("name"),
                          "saved": str(saved) if saved is not None else None, "choice": None}

    def _scope_next_question(left: dict[str, Any]) -> str:
        """The same text on every round of a prompt (prompting.ask matches it by its digest): no clock in it."""
        held = (f"its data is saved ({Path(left['saved']).name})" if left.get("saved") else
                "its data is NOT saved — close and new discard it")
        return (f"Scope ({left.get('scope') or 'the open Scope configuration'}) recorded with {left['rid']} and has "
                f"stopped; {held}. What should Scope do now? close: disconnect it — XAE's Scope view is cleared, Scope is "
                "ready for the next recording · new: disconnect it and start a new Scope recording now (on its own: "
                "scope_record(action='stop') and action='save' stop and save it) · pause: leave it holding the data to "
                "look at in XAE — the next recording with Scope asks to disconnect it")

    _SCOPE_NEXT_HOW = ("ask the user, then: scope_record(action='disconnect') to close, scope_record(action='new') to "
                       "start a new Scope recording, or nothing to keep it (pause)")

    async def _scope_disconnect(name: str | None) -> dict[str, Any]:
        r = await app.xae_call(app.xae.scope_action, "disconnect", name, None, timeout=120)
        app.audit("scope_disconnect", {"scope": name, "held": (app.scope_left or {}).get("rid")}, OpClass.EDIT, "ok")
        app.scope_left = None
        app.scope_standalone = None           # N189: what Scope recorded on its own is discarded with it
        _scope_release("scope_record disconnect")
        return {k: r[k] for k in ("scope", "result_code", "warning", "result_note") if k in r}

    def _scope_release(by: str) -> list[str]:
        """A stop_unknown part (StopRecord may have run) holds Scope until the user takes it over in a way that leaves
        it stopped: scope_record stop / disconnect / new. -> the recordings released."""
        out = []
        for part in list(app.scope_parts.values()):
            if part.state == "stop_unknown" and not part.run.get("released_by"):
                part.run["released_by"] = f"{by} ({dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')})"
                out.append(part.rid)
        return out

    async def _scope_new(name: str | None) -> dict[str, Any]:
        """Disconnect, then StartRecord at once — a Scope recording on its own (linked to no ADS recording); it owns
        Scope until scope_record(action='stop') (N189)."""
        from . import scope as S
        out: dict[str, Any] = {"disconnect": await _scope_disconnect(name)}
        since = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        try:
            r = await app.xae_call(_marked_start(name), timeout=120)
            mark = r.pop("_log_mark", None)
        except _NotRun as exc:
            # review 1 of 0.8.18: 'not done' was said after the Disconnect had run
            raise _HalfDone(f"Scope was disconnected (what it held is discarded); StartRecord was refused and did not "
                            f"run ({exc}) — Scope is in Config: scope_record(action='start') starts it") from exc
        except (_Pending, asyncio.CancelledError) as exc:
            if not (isinstance(exc, asyncio.CancelledError) and _did_not_run(exc)):
                app.scope_standalone = {"action": "new", "scope": name, "since": since, "start_unknown": True}
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _HalfDone(f"Scope was disconnected (what it held is discarded); StartRecord's outcome is not known "
                            f"({exc}) — Scope may record: it is held as a recording of its own until "
                            "scope_record(action='stop')") from exc
        except ToolError as exc:
            raise _HalfDone(f"Scope was disconnected (what it held is discarded); StartRecord failed ({exc})") from exc
        app.scope_standalone = {"action": "new", "scope": name, "since": since}   # review 2: before the check
        state, how = await _start_check(mark)
        if state is not None:
            app.scope_standalone = None
            raise _HalfDone("Scope was disconnected; " + _start_refused_text(state, how, r.get("result_code"), None))
        app.audit("scope_record", {"action": "start", "scope": name}, OpClass.EDIT, "ok")
        out.update(started=True, scope=r.get("scope"), start_check=how,
                   note="a Scope recording on its own, linked to no recording: scope_record(action='stop') then "
                        "scope_record(action='save') stop and save it")
        return out

    def _left_label(left: dict[str, Any]) -> str:
        return (f"the recording of {left['rid']}" if left.get("rid") else
                "a Scope recording of its own (scope_record)")

    async def _scope_apply(choice: str, left: dict[str, Any]) -> dict[str, Any]:
        # N189: the answer can come minutes after the question — Scope may meanwhile have been disconnected, or record
        # for another owner: a choice for data Scope no longer holds does nothing
        if app.scope_left is not left:
            return {"choice": choice, "error": f"Scope no longer holds the data of {left.get('rid')} (it was "
                                               "disconnected, or records again meanwhile): nothing was done"}
        if left.get("choice"):
            # review 1 of 0.8.18: two rec_stop calls each asked, and both answers were applied (Disconnect twice)
            return {"choice": choice, "error": f"already answered in another call ({left['choice']}): nothing was done"}
        claim = None
        if choice != "pause":
            h = _scope_holder()
            if h is not None:
                return {"choice": choice, "error": f"{_scope_owner_text(h)}: nothing was done"}
            # review 2 of 0.8.18: no claim between the Disconnect and the StartRecord of 'new' — a rec_start took
            # Scope in that gap; claimed here with no await since the check
            claim = app.scope_claim = f"the choice after recording {left.get('rid')} #{secrets.token_hex(4)}"
        try:
            return await _scope_apply_owned(choice, left)
        finally:
            _scope_unclaim(claim)

    async def _scope_apply_owned(choice: str, left: dict[str, Any]) -> dict[str, Any]:
        if choice == "pause":
            left["choice"] = "pause"
            return {"choice": "pause", "note": "Scope keeps the data (Reply) — the next recording with Scope asks to "
                                               "disconnect it; scope_record(action='disconnect') closes it any time"}
        left["choice"] = choice                # set before the first await: a second answer finds it
        try:
            got = await (_scope_disconnect(left.get("name")) if choice == "close" else _scope_new(left.get("name")))
        except _HalfDone as exc:
            return {"choice": choice, "error": str(exc)[:600]}
        except _NotRun as exc:
            left["choice"] = None
            return {"choice": choice, "error": str(exc)[:600], "note": f"not done — {_SCOPE_NEXT_HOW}"}
        except ToolError as exc:
            return {"choice": choice, "error": str(exc)[:600],
                    "note": "the Disconnect's outcome is not known — check XAE's Scope View before anything else"}
        return {"choice": choice, **got}

    async def _scope_next(ctx: Context | None, rid: str) -> dict[str, Any] | None:
        """Ryan, 2026-10-21: after a recording, Scope is closed, starts anew or pauses — the user chooses (a prompt
        when the client can show one; else the agent is told to ask). None when Scope holds no data of rid."""
        left = app.scope_left
        if not left or left["rid"] != rid or left.get("choice"):
            return None
        q = _scope_next_question(left)
        if ctx is None or not app.cfg.safety.allow_elicitation:
            return {"ask_user": q, "then": _SCOPE_NEXT_HOW}
        try:
            ans, _open_s = await ASK.ask(ctx, q, ScopeNextSchema)    # AskUser: this round returns the question
        except ASK.NoPrompt as exc:
            return {"ask_user": q, "then": _SCOPE_NEXT_HOW, "no_prompt": str(exc)[:300]}
        if ans.action != "accept" or ans.data is None:
            left["choice"] = "pause"
            app.audit("scope_next", {"id": rid, "answer": None}, OpClass.EDIT, "ok", "not answered: pause")
            return {"choice": "pause", "note": "the question was not answered: Scope keeps the data (pause)"}
        app.audit("scope_next", {"id": rid, "answer": ans.data.next}, OpClass.EDIT, "ok")   # N190
        return await _scope_apply(ans.data.next, left)

    def _scope_link(rid: str, run: dict[str, Any], svdx: Path, csv: Path, incomplete: str | None = None) -> dict[str, Any]:
        """Signed <id>.scope.json tying the ADS recording to the Scope files (SHA-256 each); the files are set
        read-only like the recording (R9). incomplete: the .svdx was still being written — review of 0.8.14: it was
        hashed and set read-only as it was; now it is neither, and the link says why."""
        from .recorder import _manifest_sig, file_sha256
        import os as _os
        import stat as _stat
        files = {}
        xcsv = csv.with_name(csv.name[:-4] + ".xae.csv")
        sealed = [p for p in (csv, xcsv) if not incomplete] + ([] if incomplete else [svdx])
        for p in sealed:
            if p.is_file():
                sha, n = file_sha256(p)
                files[p.name] = {"sha256": sha, "bytes": n}
        link: dict[str, Any] = {"_scope_link": 1, "recording": rid, "scope": run["scope"], "started": run["started"],
                                "stopped": dt.datetime.now(dt.timezone.utc).isoformat(), "files": files,
                                "recorder": f"twincat-mcp {__version__}"}
        if run.get("start_unknown"):
            # first review of 0.8.17: StartRecord's outcome was not known — when Scope started is not known either
            link["start_unknown"] = run["start_unknown"][:400]
            link["start_asked"] = run.get("start_asked")
        if run.get("finished_by"):
            link["finished_by"] = run["finished_by"]          # N182: the recording's own end, or rec_stop
        if run.get("recording_ended"):
            link["recording_ended"] = run["recording_ended"]
        if run.get("span"):
            link["span"] = run["span"]                        # N189 / N191: Scope's capture against the recording
        if incomplete:
            link["not_sealed"] = {"file": svdx.name, "why": incomplete}
        if app.rec.key:
            link["hmac"] = _manifest_sig(app.rec.key, link)
        lp = svdx.with_name(f"{rid}.scope.json")
        lp.write_text(json.dumps(link, indent=1) + "\n", encoding="utf-8")
        for p in sealed + [lp]:
            if p.is_file():
                try:
                    _os.chmod(p, p.stat().st_mode & ~(_stat.S_IWUSR | _stat.S_IWGRP | _stat.S_IWOTH))
                except OSError:
                    pass
        return {"file": str(lp), "files": files}


    # ======================================================================
    # Capture profiles: scan → choose → save → start
    # ======================================================================
    def _scan_runtime(prefix: str | None, pattern: str | None, max_items: int) -> list[dict[str, Any]]:
        from .recorder import pattern_leaves
        out: list[dict[str, Any]] = []
        if pattern:
            # A5: the selection rec_start(pattern=…) records, leaf for leaf
            groups = pattern_leaves(app.ads, pattern, max_items, None, prefix)
        elif prefix:
            groups = app.ads.leaves_under(prefix, max_items)
        else:
            groups = []
            budget = max_items
            for r in app.ads.roots(None, False):
                s, lv = app.ads.leaves(r.name, budget)
                groups.append((s, lv))
                budget -= len(lv)
                if budget <= 0:
                    break
        for _root, leaves in groups:
            for lf in leaves:
                out.append({"path": lf.path, "type": lf.type, "size": lf.size})
                if len(out) >= max_items:
                    return out
        return out

    @mcp.tool(annotations=READ)
    async def rec_scan(prefix: str | None = None, pattern: str | None = None, source: str = "runtime",
                       checklist: str | None = None, preselect: list[str] | None = None, cycle_ms: float = 10,
                       max_items: int = 5000, show: int = 100) -> dict[str, Any]:
        """Enumerate the variables that could be recorded and what each would cost. source='runtime'
        walks the live symbol/datatype tables (every scalar leaf under prefix, or all programs/GVLs);
        source='project' walks the source declarations (works offline). pattern selects leaves exactly as
        rec_start does: '*'/'?' wildcards, '[ ]' literal, a leaf matches when it or a parent matches. Returns counts by type and by group (program.instance) with bytes/s estimates at
        cycle_ms, a suggested mode per leaf, and — when checklist='<name>' — writes
        ~/.twincat_mcp/profiles/<name>.checklist.csv with a 'select' column to mark (preselect =
        patterns already marked). Then rec_profile_save(name, from_checklist=name)."""
        src_note: str | None = None
        if source == "runtime":
            leaves = await app.ads_call(_scan_runtime, prefix, pattern, max_items, timeout=300, slow=True)
        elif source == "project":
            from .recorder import path_selected, selection_regex
            rx = selection_regex(pattern) if pattern else None
            pv = await app.proj_call(app.proj.variables, prefix, 64, max_items * 4, False, port=app.ads.port)
            seen_paths: set[str] = set()
            leaves = []
            for it in pv["items"]:
                # a path listed for two PLC projects (port not mapped) is one leaf to record (review of S4)
                if "[*]" in it["path"] or it["path"].lower() in seen_paths or (rx is not None and not path_selected(rx, it["path"])):
                    continue
                seen_paths.add(it["path"].lower())
                leaves.append({"path": it["path"], "type": it["type"], "size": None, "attrs": it.get("attrs")})
            leaves = leaves[:max_items]
            src_note = pv.get("note")
        else:
            raise ToolError("source must be runtime | project")
        if not leaves:
            raise ToolError("scan found no variables (prefix/pattern wrong, or not connected / not scanned)")
        scan_note = ("rec_start with the same prefix/pattern records exactly these leaves (array elements one by one); "
                     "an array named explicitly in rec_start(symbols=[…]) is recorded as ONE variable (same bytes, "
                     "exported as name[i]). pattern: '*' and '?' are wildcards, '[ ]' are literal (array indices); a "
                     "leaf is listed when the pattern matches it or one of its parents")
        by_type: dict[str, int] = {}
        by_group: dict[str, dict[str, Any]] = {}
        modes: dict[str, int] = {}
        for lf in leaves:
            by_type[lf["type"]] = by_type.get(lf["type"], 0) + 1
            sm = capture.suggest_mode(lf["type"])
            modes[sm] = modes.get(sm, 0) + 1
            g = by_group.setdefault(capture.group_of(lf["path"]), {"leaves": 0, "bytes_per_s_at_cycle": 0.0})
            g["leaves"] += 1
            g["bytes_per_s_at_cycle"] += capture.estimate_bytes_per_s(lf.get("size") or 8, sm, cycle_ms)
        for g in by_group.values():
            g["bytes_per_s_at_cycle"] = round(g["bytes_per_s_at_cycle"])
        res: dict[str, Any] = {"source": source, "count": len(leaves), "by_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
                               "suggested_modes": modes, "groups": by_group,
                               "items": [{"path": lf["path"], "type": lf["type"], "size": lf.get("size"), "suggested_mode": capture.suggest_mode(lf["type"]),
                                          **({"attrs": lf["attrs"]} if lf.get("attrs") else {})} for lf in leaves[:show]],
                               "truncated_items": len(leaves) > show}
        if src_note:
            res["source_note"] = src_note
        if checklist:
            path = app.profiles.checklist_path(checklist)
            await asyncio.to_thread(path.write_text, capture.build_checklist(leaves, cycle_ms, preselect), "utf-8")
            res["checklist"] = str(path)
            res["next"] = f"mark 'x' in the select column, then rec_profile_save(name={checklist!r}, from_checklist={checklist!r})"
        if any("[" in lf["path"] for lf in leaves):
            res["note"] = scan_note
        return res

    @mcp.tool(annotations=EDIT)
    async def rec_profile_save(name: str, symbols: list[str] | None = None, from_checklist: str | None = None,
                               from_attributes: bool = False, scope: str | None = None, mode: str = "auto",
                               cycle_ms: float = 10, start_when: str | None = None, stop_when: str | None = None,
                               pre_trigger_ms: float = 0, duration_s: float | None = None,
                               description: str | None = None, replace: bool = True) -> dict[str, Any]:
        """Save a named capture profile. Sources combine: explicit symbols (mode='auto' picks cycle for
        REAL/TIME, change for BOOL/INT/enum, poll for strings — or give cycle|change|poll), a marked
        checklist (path, or the name given to rec_scan), and/or {attribute 'tcmcp.record' := 'cycle'}
        pragmas in the source (from_attributes=true; scope limits to one program). Triggers and the
        default cycle are stored with the profile. rec_profile_start(name) replays it."""
        items: list[capture.ProfileItem] = []
        src = []
        if symbols:
            for sym in symbols:
                m = mode
                typ = None
                if mode == "auto":
                    try:
                        info = await app.ads_call(app.ads.symbol_info, sym, timeout=15)
                        typ, m = info.type, capture.suggest_mode(info.type)
                    except ToolError:
                        m = "change"
                if m not in capture.MODES:
                    raise ToolError("mode must be auto | cycle | change | poll")
                items.append(capture.ProfileItem(sym, m, None, typ))
            src.append("explicit")
        if from_checklist:
            pth = Path(from_checklist)
            if not pth.is_file():
                pth = app.profiles.checklist_path(from_checklist)
            if not pth.is_file():
                raise ToolError(f"checklist not found: {from_checklist} (rec_scan checklist=... writes it)")
            try:
                items += capture.parse_checklist(pth.read_text(encoding="utf-8-sig"), cycle_ms)
            except capture.ProfileError as exc:
                raise ToolError(str(exc)) from exc
            src.append("checklist")
        if from_attributes:
            pv = await app.proj_call(app.proj.variables, scope, 64, 200_000, False, port=app.ads.port)
            if pv.get("note"):
                raise ToolError(f"from_attributes: {pv['note']} — which PLC project's attributes to take is not known; "
                                "connect to the PLC project's ADS port (ads_connect) or pass scope='<PLC project>@<name>'")
            try:
                found = capture.items_from_attributes(pv["items"], cycle_ms)
            except capture.ProfileError as exc:
                raise ToolError(str(exc)) from exc
            if not found:
                raise ToolError(f"no declaration carries {{attribute '{capture.ATTR_RECORD}' := ...}} in {scope or 'the project'}")
            items += found
            src.append("attributes")
        if not items:
            raise ToolError("give symbols, from_checklist or from_attributes")
        dedup: dict[str, capture.ProfileItem] = {}
        for it in items:
            dedup[it.symbol.lower()] = it     # later sources override earlier ones
        prof = capture.CaptureProfile(name=name, items=list(dedup.values()), description=description, cycle_ms=cycle_ms,
                                start_when=start_when, stop_when=stop_when, pre_trigger_ms=pre_trigger_ms,
                                duration_s=duration_s, source="+".join(src))
        try:
            path = await asyncio.to_thread(app.profiles.save, prof, replace)
        except capture.ProfileError as exc:
            raise ToolError(str(exc)) from exc
        app.audit("rec_profile_save", {"name": name, "count": len(prof.items), "source": prof.source}, OpClass.EDIT, "ok")
        return {"saved": str(path), **prof.to_dict()}

    @mcp.tool(annotations=READ)
    async def rec_profile_list() -> dict[str, Any]:
        """Saved capture profiles (name, symbol count, modes, source)."""
        return {"profiles": await asyncio.to_thread(app.profiles.list), "dir": str(app.profiles.root)}

    @mcp.tool(annotations=READ)
    async def rec_profile_get(name: str) -> dict[str, Any]:
        """One profile in full: every symbol with its mode and cycle, triggers, per-group cost estimate."""
        try:
            return (await asyncio.to_thread(app.profiles.get, name)).to_dict()
        except capture.ProfileError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=EDIT)
    async def rec_profile_delete(name: str) -> dict[str, Any]:
        """Delete a profile and its checklist file."""
        try:
            ok = await asyncio.to_thread(app.profiles.delete, name)
        except capture.ProfileError as exc:
            raise ToolError(str(exc)) from exc
        app.audit("rec_profile_delete", {"name": name}, OpClass.EDIT, "ok" if ok else "absent")
        if not ok:
            names = [x.get("name") for x in await asyncio.to_thread(app.profiles.list)][:30]
            raise ToolError(f"profile {name!r} not found; existing profiles: {names}")
        return {"deleted": True, "name": name}

    @mcp.tool(annotations=EDIT)
    async def rec_profile_start(name: str, start_when: str | None = None, stop_when: str | None = None,
                                duration_s: float | None = None, max_events: int | None = 5_000_000,
                                align: bool = True) -> dict[str, Any]:
        """Start recording a saved profile: one recording per (mode, cycle_ms) group, all named
        '<profile>/<mode>@<cycle>'. Triggers given here override the profile's. Returns the recording
        ids; rec_profile_stop(name) stops them together, rec_stats/rec_export work per id.
        align (default, several groups, no start_when): every group starts on the same PLC cycle — a shared start
        trigger on the task's cycle counter a moment ahead — and duration_s then counts from that cycle, so all files
        cover the same window (D29: groups started ≈ 160 ms apart)."""
        try:
            prof = await asyncio.to_thread(app.profiles.get, name)
        except capture.ProfileError as exc:
            raise ToolError(str(exc)) from exc
        started: list[dict[str, Any]] = []
        aligned_on = None
        dur = duration_s or prof.duration_s
        post = None
        if align and len(prof.groups()) > 1 and not (start_when or prof.start_when):
            counter = "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount"
            try:
                vals = await app.ads_call(app.ads.read_many, [counter, "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleTime"], timeout=15)
                now_c = vals.get(counter)
                ct = vals.get("TwinCAT_SystemInfoVarList._TaskInfo[1].CycleTime")
                ct_ms = (ct / 10_000.0) if isinstance(ct, (int, float)) and ct > 0 else None
                if isinstance(now_c, int) and ct_ms:
                    ahead_ms = 400 * len(prof.groups()) + 500          # time to subscribe every group
                    target = now_c + max(1, int(ahead_ms / ct_ms))
                    start_when = f"{counter} >= {target}"
                    aligned_on = {"trigger": start_when, "cycles_ahead": target - now_c, "task_cycle_ms": ct_ms}
                    if dur:
                        # the window counts from the shared start cycle; duration_s stays as the outer limit (a stopped
                        # PLC never reaches the trigger cycle — the recordings must still end)
                        post, dur = dur, dur + ahead_ms / 1000.0 + 5.0
            except ToolError as exc:
                aligned_on = {"error": f"could not read the task counter, groups start unaligned: {str(exc)[:120]}"}
        # a trigger's symbol must be inside each recording that uses the trigger
        cond_syms = [m.group(1) for c in (start_when or prof.start_when, stop_when or prof.stop_when) if c
                     for m in [re.match(r"\s*([A-Za-z_][\w\.\[\]]*)", c)] if m]
        try:
            for (mode, cyc), its in prof.groups().items():
                syms = [i.symbol for i in its]
                low = {x.lower() for x in syms}
                syms += [c for c in cond_syms if c.lower() not in low]
                if aligned_on and "trigger" in aligned_on:
                    c = aligned_on["trigger"].split(" ")[0]
                    if c.lower() not in {x.lower() for x in syms}:
                        syms.append(c)
                res = await app.rec_call(app.rec.start, symbols=syms, prefix=None, pattern=None,
                                         mode=mode, cycle_ms=cyc, duration_s=dur,
                                         max_events=max_events, start_when=start_when or prof.start_when,
                                         stop_when=stop_when or prof.stop_when,
                                         pre_trigger_ms=0 if aligned_on and "trigger" in aligned_on else prof.pre_trigger_ms,
                                         post_trigger_s=post,
                                         name=f"{name}/{mode}@{cyc:g}", max_symbols=max(len(its), 1) + 50, timeout=180)
                nsym = res.get("symbols")
                started.append({"id": res["id"], "mode": mode, "cycle_ms": cyc,
                                "symbols": nsym if isinstance(nsym, int) else len(nsym or []),
                                "armed": res.get("armed"), "file": res.get("file")})
        except ToolError:
            for s0 in started:
                try:
                    await app.rec_call(app.rec.stop, s0["id"], "profile start failed", timeout=30)
                except ToolError:
                    pass
            raise
        app.audit("rec_profile_start", {"profile": name, "recordings": [s0["id"] for s0 in started]}, OpClass.EDIT, "ok")
        out = {"profile": name, "recordings": started, "groups": len(started)}
        if aligned_on:
            out["aligned"] = aligned_on
        if len(started) > 1 and not (start_when or prof.start_when) and not (aligned_on and "trigger" in aligned_on):
            # D29: groups are separate subscriptions and start one after another — say how far apart, from the PLC's
            # own timestamps of each group's first sample
            offs = await asyncio.to_thread(app.rec.start_offsets_ms, [s0["id"] for s0 in started], 3.0)
            if offs:
                spread = max(offs.values())
                out["start_offsets_ms"] = offs
                out["note"] = (f"the {len(started)} groups started up to {spread:g} ms apart (PLC time of each first "
                               "sample): compare them over the common window, or give the profile a start_when trigger so "
                               "every group starts on the same PLC event")
        return out

    @mcp.tool(annotations=EDIT)
    async def rec_profile_stop(name: str) -> dict[str, Any]:
        """Stop every live recording started from this profile; returns per-recording status + stats."""
        out = []
        mine = [(rid, rec) for rid, rec in list(app.rec.recs.items()) if (rec.name or "").startswith(f"{name}/")]
        for rid, rec in mine:
            if rec.active:
                out.append(await app.rec_call(app.rec.stop, rid, "manual (rec_profile_stop)", timeout=60))
        if not out:
            # D27: the profile's recordings already ended (duration / stop_when): give their final status, not an error
            ended = sorted(mine, key=lambda x: getattr(x[1], "created", 0))
            if ended:
                last_t = getattr(ended[-1][1], "created", 0)
                batch = [rid for rid, rec in ended if abs(getattr(rec, "created", 0) - last_t) < 30]
                finals = [await app.mem_call(app.rec.status, rid) for rid in batch]
                return {"profile": name, "stopped": [], "already_ended": finals,
                        "note": "nothing was live: these recordings of the profile had already ended by themselves"}
            raise ToolError(f"no recording of profile {name!r} in this session (rec_list shows what exists)")
        app.audit("rec_profile_stop", {"profile": name, "recordings": [o["id"] for o in out]}, OpClass.EDIT, "ok")
        return {"profile": name, "stopped": out}

    @mcp.tool(annotations=READ)
    async def rec_list(limit: int = 30, offset: int = 0) -> dict[str, Any]:
        """Recordings of this session (compact status) and recording files on disk, newest first
        (paged: limit/offset; on_disk_total says how many exist); a recording with Scope: its scope state."""
        res = await app.mem_call(app.rec.list, max(1, min(limit, 500)), max(0, offset))
        for r in (res.get("session") or []) if isinstance(res, dict) else []:
            sc = _scope_state(str(r.get("id"))) if isinstance(r, dict) else None
            if sc is not None:
                r["scope"] = sc
        # first review of 0.8.17: a Scope part whose recording was deleted while Scope may still record (a stop XAE
        # refused, or one whose outcome is not known) — listed, so rec_stop(id) can ask again
        orphans = [{"id": rid, "scope": _scope_state(rid)} for rid, part in list(app.scope_parts.items())
                   if part.state != "finished" and rid not in app.rec.recs and rid not in app.rec.deleting]
        if orphans and isinstance(res, dict):
            res["scope_without_recording"] = orphans
        return res

    @mcp.tool(annotations=READ)
    async def rec_status(id: str) -> dict[str, Any]:
        """Live status of one recording: samples, events, rate, missing_samples (holes in the PLC timeline, cycle
        mode), completeness, dropped (lost on this PC), backlog, stats_coverage, per-block report, warnings,
        armed/triggered, last value per symbol; with Scope: scope {state recording | finishing | finished |
        stop_unknown} (N182; recording with stop_refused: XAE refused StopRecord, rec_stop asks again)."""
        try:
            res = await app.mem_call(app.rec.status, id)
        except ToolError:
            orphan = _orphan(id)
            if orphan is None:
                raise                              # a recording the recorder holds: its own error, never 'deleted'
            # first review of 0.8.17: the recording was deleted; its Scope part is still here (and may still record)
            return {"id": orphan[0], "recording": "deleted — not in this session; only its Scope part is left",
                    "scope": _scope_state(orphan[0])}
        sc = _scope_state(_rec_id(id))
        if sc is not None:
            resub = res.get("resubscriptions") or 0
            if sc["state"] == "recording" and ((res.get("gap_markers") or 0) > 0 or resub > 0):
                # N180: a TwinCAT restart resets Scope to Config — said while it records, not only at the save. Eleventh
                # review: a gap marker is a hole in the PLC timeline, not by itself an outage — said as what was seen
                seen = (f"the recording was re-subscribed {resub} time(s) (the runtime went away and came back)"
                        if resub else f"the recording has {res.get('gap_markers')} gap marker(s) (holes in the PLC "
                                      "timeline: an outage, a TwinCAT restart, or samples not delivered)")
                sc["warning"] = (seen + " — if TwinCAT was restarted, Scope was reset to Config and may refuse to save "
                                 "this run (SaveSVD)")
            left = app.scope_left
            if left and left["rid"] == _rec_id(id) and not left.get("choice"):
                # the recording ended by itself: no call asked yet what Scope does now — read-only: asked, not done
                sc["next"] = {"ask_user": _scope_next_question(left), "then": _SCOPE_NEXT_HOW}
            res["scope"] = sc
        return res

    @mcp.tool(annotations=READ)
    async def rec_snapshot(id: str, last_n: int = 100, symbols: list[str] | None = None) -> dict[str, Any]:
        """Most recent events of a running (or finished) recording — the live tail."""
        return await app.mem_call(app.rec.snapshot, id, min(last_n, 5000), symbols)

    @mcp.tool(annotations=READ)
    async def rec_stats(id: str, symbols: list[str] | None = None, include_runs: bool = True,
                        max_runs: int = 200, exact: bool = False) -> dict[str, Any]:
        """Per-symbol statistics over the whole recording so far: count, first/last, min/max/mean,
        change count, BOOL rising/falling edges with timestamps, and value runs (state timeline
        with dwell times). Live (incremental) while complete; recomputed from the file when the ingest had
        to skip statistics under load, for recordings of earlier sessions, or with exact=true."""
        return await app.mem_call(app.rec.stats, id, symbols, include_runs, min(max_runs, 5000), exact or None)

    @mcp.tool(annotations=EDIT)
    async def rec_capture(symbols: list[str] | None = None, seconds: float = 5.0, mode: str = "cycle", cycle_ms: float = 10,
                          prefix: str | None = None, counter: str | None = None, engine: str | None = None) -> dict[str, Any]:
        """One call for "record X for N s and give me the stats": starts a recording, waits (≤ 60 s), stops, and
        returns per-symbol statistics, completeness and the rec_verify verdict (integrity + PLC timeline, plus the
        optional per-cycle `counter`) with the file path. For longer or triggered captures use rec_start."""
        seconds = float(min(max(seconds, 0.1), 60.0))
        st = await app.rec_call(app.rec.start, symbols=symbols, prefix=prefix, pattern=None, mode=mode, cycle_ms=cycle_ms,
                                duration_s=seconds, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0,
                                name="rec_capture", max_symbols=500, engine=engine, timeout=120)
        rid = st["id"]
        t_end = time.monotonic() + seconds + 10
        while time.monotonic() < t_end:
            await asyncio.sleep(0.25)
            if not (await app.mem_call(app.rec.status, rid))["active"]:
                break
        fin = await app.rec_call(app.rec.stop, rid, "rec_capture done", timeout=120)
        ver = await app.mem_call(app.rec.verify, rid, counter, None)
        app.audit("rec_capture", {"id": rid, "symbols": fin.get("symbols"), "seconds": seconds}, OpClass.EDIT, "ok")
        keep = ("id", "file", "mode", "engine", "engine_note", "time_source", "symbols", "samples", "events", "missing_samples",
                "completeness", "dropped", "dropped_on_stop", "overflow_samples", "plc_span_s", "stop_reason", "warnings")
        stats = fin.get("stats")
        if stats is None and (fin.get("symbols") or 0) <= 50:
            stats = (await app.mem_call(app.rec.stats, rid, None, False, 0, True)).get("symbols")
        out = {**{k: fin[k] for k in keep if k in fin}, "verify": {k: ver.get(k) for k in ("verdict", "integrity", "timeline", "counter") if k in ver},
               "stats": stats}
        for k in ("label", "gap_markers"):
            if fin.get(k) is not None:
                out[k] = fin[k]
        if stats is None:
            # N23: say why instead of a bare null
            out["stats_note"] = (f"{fin.get('symbols')} symbols: per-symbol statistics are not inlined above 50 symbols "
                                 f"(response size); rec_stats(id='{rid}', symbols=[…]) computes them from the file")
        out["engine"] = st.get("engine", out.get("engine"))
        if st.get("engine_note"):
            out["engine_note"] = st["engine_note"]
        return out

    @mcp.tool(annotations=READ)
    async def rec_verify(id: str, counter: str | None = None, step: int | None = None) -> dict[str, Any]:
        """Prove a finished recording. integrity: the file is byte-for-byte what the recorder wrote (sha256 of every
        line, signed with this install's key) — 'MODIFIED' if anything was edited. timeline (cycle mode): per
        notification block, holes between the PLC's own sample timestamps. counter (optional): a PLC variable that
        increments every cycle and was recorded with the rest (your nCycleCount, or
        TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount) must step by exactly `step` per sample — ground truth
        for 'no sample lost'. verdict PASS only if all checks hold; INCONCLUSIVE when the file is intact but its
        completeness cannot be shown (a change / poll recording without counter=, or a stop from an older version
        whose blocks ended apart); FAIL on any loss, a block that never delivered, or symbols not subscribed."""
        return await app.mem_call(app.rec.verify, id, counter, step)

    @mcp.tool(annotations=READ)
    async def rec_read(id: str, t_from_ms: float | None = None, t_to_ms: float | None = None,
                       symbols: list[str] | None = None, max_rows: int = 2000, wide: bool = False,
                       fill: bool = False) -> dict[str, Any]:
        """Read a time window of a recording from its file. wide=true: one row per PLC timestamp with one column per
        selected symbol; a cell is null when that symbol has no sample at that time — nothing is invented (R4).
        fill=true repeats the last known value into those cells and lists them per row under "_filled" (handy for
        'what else was true when X happened', but a filled cell is not a sample). Works on finished recordings from
        earlier sessions too."""
        return await app.mem_call(app.rec.read, id, t_from_ms, t_to_ms, symbols, min(max_rows, 50_000), wide, fill)

    @mcp.tool(annotations=EDIT)
    async def rec_stop(id: str, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Stop a recording, release the ADS notifications, finalize the file; returns status + stats. With Scope: its
        part is finished, and Scope (it now holds this recording) is closed, starts a new recording, or keeps the
        data (pause) — the user is asked (scope_next)."""
        rid = _rec_id(id)
        orphan = _orphan(id)
        if orphan is not None:
            # first review of 0.8.17: the recording was deleted, its Scope part was not finished (XAE refused StopRecord)
            # — rec_stop asks again; there is no ADS recording left to stop
            res: dict[str, Any] = {"id": orphan[0],
                                   "recording": "deleted — not in this session; only its Scope part was left"}
            _scope_answer(res, await orphan[1].finish("rec_stop", save=False))
            return res
        part = app.scope_parts.get(rid)
        if part is not None:
            part.hold()                    # its end is rec_stop's: the part's watch leaves it to this call
        try:
            res = await app.rec_call(app.rec.stop, id, "manual (rec_stop)", timeout=60)
            app.audit("rec_stop", {"id": rid, "events": res.get("events")}, OpClass.EDIT, "ok")
            # N182: the Scope part is finished once — a recording that ended by itself has it finished already. A call
            # that ends before it (cancelled, timed out) leaves it to the part's owner, which finishes it after.
            # First review of 0.8.17: a recording that had ended by itself just before (within the watch period) is
            # said as that — its stop reason, not 'rec_stop'
            if part is not None:
                reason = res.get("stop_reason")
                by = part.ended(reason) if reason and str(reason) not in _STOPPED_BY else "rec_stop"
                _scope_answer(res, await part.finish(by))
        finally:
            if part is not None:
                part.release()
        nx = await _scope_next(ctx, rid)
        if nx is not None:
            res["scope_next"] = nx
        return res

    @mcp.tool(annotations=READ)
    async def scope_status() -> dict[str, Any]:
        """TwinCAT Scope on this PC: where TC3ScopeExportTool.exe is (TE130x Scope View / TF3300 Scope Server) and
        the default export folder."""
        from . import scope as S
        d = await asyncio.to_thread(S.status, app.cfg.scope.export_tool)
        d["export_dir"] = str(_scope_dir())
        return d

    def _scope_dir() -> Path:
        # 0.8.17 (Ryan, 2026-10-21): every Scope file the plugin saves goes to 'Scope Recordings' beside the recordings
        # (0.8.16 and before: 'scope' — those files stay where they are); rec_delete never deletes them
        return Path(app.cfg.scope.export_dir) if app.cfg.scope.export_dir else app.rec.root / "Scope Recordings"

    @mcp.tool(annotations=EDIT)
    async def scope_export(svd: str, target: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Export a saved TwinCAT Scope recording (.svdx / .svd) to CSV with Beckhoff's TC3ScopeExportTool.exe
        (command line `"svd=<file>" target=<csv> silent`; CSV needs no extra licence). Default target:
        <recordings>/Scope Recordings/<name>.csv ([scope] export_dir). The CSV is Beckhoff's output, unchanged — channels, time
        column and alignment of channels with different sample times follow the Scope project's export settings. A
        signed <csv>.provenance.json records the source file and SHA-256, the tool and its SHA-256, and the command.
        A read-only source (rec_stop seals its .svdx) is exported from a byte-identical writable copy. scope_view_log:
        the ERROR lines the Scope View logs (TcScopeView.log and its numbered siblings, TcScopeView_1.log …) got during the export."""
        from . import scope as S
        tool = await asyncio.to_thread(S.find_tool, app.cfg.scope.export_tool)
        if tool is None:
            raise ToolError(S.status(app.cfg.scope.export_tool)["note"])
        src = Path(svd)
        out = app.out_path(target, overwrite, "scope export") if target else _scope_dir() / (src.stem + ".csv")
        try:
            res = await asyncio.to_thread(S.export, src, out, tool=tool, overwrite=overwrite, key=app.rec.key,
                                          version=__version__, view_log=_view_log())
        except S.ScopeError as exc:
            raise ToolError(str(exc)) from exc
        app.audit("scope_export", {"svd": str(src), "target": str(out), "sha256": res["export_sha256"]}, OpClass.EDIT, "ok")
        return res

    @mcp.tool(annotations=READ)
    async def scope_list() -> dict[str, Any]:
        """TwinCAT Scope configurations in the solution open in XAE (TwinCAT Measurement projects). Their channels,
        sample time and record time are what scope_record / rec_start(also_scope=true) record."""
        return await app.xae_call(app.xae.scope_list, timeout=60)

    @mcp.tool(annotations=EDIT)
    async def scope_record(action: str, scope: str | None = None, path: str | None = None,
                           confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Drive a Scope configuration open in XAE (Automation Interface IMeasurementScope): action = start | stop |
        disconnect (Scope discards what it holds and is back in Config — needed before it records again; asks for the
        user's confirmation unless what it holds is a recording the plugin saved) | new (disconnect, then start) |
        save (SaveSVD → <recordings>/Scope Recordings/<scope>-<time>.svdx, or path=<file>.svdx; the .csv exported from that file
        with Beckhoff's export tool — XAE's ExportCSV only without it, flagged, and never over an existing CSV; XAE's
        ExportCSV of the in-memory data beside it, <name>.xae.csv, row counts compared in cross_check; the Scope View
        log's new ERROR / FATAL lines).
        scope = its name when several are open. Affects only the Scope recording, not the PLC.
        One Scope, one owner (N189): while a recording's Scope part has Scope, its rec_stop / rec_delete stop and save
        it — start / stop / save / disconnect / new here are refused; a Scope recording started here owns Scope until
        action='stop'."""
        if action not in ("start", "stop", "disconnect", "new", "save"):
            raise ToolError("action must be start | stop | disconnect | new | save")
        _scope_record_allowed(action)
        if action in ("disconnect", "new"):
            left = app.scope_left
            if not (left and left.get("saved")):
                # what Scope holds was not saved by the plugin (or is not known): discarding it is the user's call
                await app.permit(ctx, OpClass.DESTRUCTIVE, "scope_record", f"disconnect:{scope or ''}", confirm_token)
                _scope_record_allowed(action)  # review 1 of 0.8.18: a recording may have taken Scope meanwhile
            claim = None
            if action == "new":
                # the owner checked above is none, a Scope recording of its own or a stop_unknown part — both are taken
                # over here; the claim keeps a rec_start out between the disconnect and StartRecord
                claim = app.scope_claim = f"scope_record new #{secrets.token_hex(4)}"
            try:
                r = await (_scope_disconnect(scope) if action == "disconnect" else _scope_new(scope))
            finally:
                _scope_unclaim(claim)
            app.audit("scope_record", {"action": action, "scope": scope}, OpClass.EDIT, "ok")
            return r
        if action in ("start", "stop"):
            from . import scope as S
            claim = _scope_claim("scope_record start") if action == "start" else None
            try:
                if action == "start":
                    try:
                        r = await app.xae_call(_marked_start(scope), timeout=120)
                        mark = r.pop("_log_mark", None)
                    except (_Pending, asyncio.CancelledError) as exc:
                        # it may record: held as a recording of its own until scope_record stop
                        if not (isinstance(exc, asyncio.CancelledError) and _did_not_run(exc)):
                            app.scope_standalone = {"action": "start", "scope": scope, "start_unknown": True, "since":
                                                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
                        raise
                else:
                    r = await app.xae_call(app.xae.scope_action, action, scope, None, timeout=120)
                if action == "start":
                    # review 2 of 0.8.18: StartRecord ran — Scope is this run's from here on (a call cancelled
                    # during the check left Scope recording with no owner)
                    app.scope_standalone = {"action": "start", "scope": scope,
                                            "since": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
                    left_before, app.scope_left = app.scope_left, None   # review 2: Scope records anew — not A's data
                    state, how = await _start_check(mark)
                    if state is not None:
                        app.scope_standalone, app.scope_left = None, left_before   # refused: Scope holds what it held
                        err = _start_refused_text(state, how, r.get("result_code"), None)
                        app.audit("scope_record", {"action": action, "scope": scope}, OpClass.EDIT, "error", err)
                        raise ToolError(err)
                    r = {k: v for k, v in r.items() if k != "result_note"}
                    r["start_check"] = how
                else:
                    # StopRecord ran: Scope holds this data (Reply) until it is disconnected — the next recording with
                    # Scope asks first, as after a recording (N186)
                    was_own = app.scope_standalone is not None
                    app.scope_standalone = None
                    released = _scope_release("scope_record stop")
                    if app.scope_left is None and (was_own or released):
                        # review 2 of 0.8.18: after a stop_unknown part, what Scope holds is that recording's capture
                        app.scope_left = {"rid": released[0] if released else None, "scope": scope, "name": scope,
                                          "saved": None, "choice": "pause"}
            finally:
                _scope_unclaim(claim)
        elif action == "save":
            from . import scope as S
            base = app.out_path(str(Path(path).with_suffix(".svdx")), False, "scope file").with_suffix("") if path else _scope_dir() / (
                re.sub(r"[^\w.-]+", "_", scope or "scope") + "-" + time.strftime("%Y%m%d-%H%M%S"))
            mark = await asyncio.to_thread(S.log_mark, _view_log())
            r = await _scope_save(scope, Path(str(base) + ".svdx"), Path(str(base) + ".csv"), mark=mark,
                                  since="this call began", exists_hint="give scope_record another path")
            if "error" in r["save_svd"]:
                # twelfth review of 0.8.16: what XAE's ExportCSV still got (a CSV written, or nothing) is part of the
                # answer — it was dropped, and the CSV it wrote went unsaid
                ex = r.get("export_csv") or {}
                more = (f"; recovered as {ex.get('path') or Path(str(base) + '.csv')}: {ex['recovered']}"
                        if ex.get("recovered") else f"; {ex['lost']}" if ex.get("lost") else
                        f"; {ex['skipped']}" if ex.get("skipped") else "")
                app.audit("scope_record", {"action": action, "scope": scope, "path": path}, OpClass.EDIT, "error",
                          r["save_svd"]["error"] + more)
                raise ToolError(f"Scope save: {r['save_svd']['error']}{more}")
            r["scope_view_log"] = await asyncio.to_thread(S.log_errors_since, mark)
            left = app.scope_left
            if left is not None and left.get("rid") is None and not app.scope_standalone:
                left["saved"] = str(Path(str(base) + ".svdx"))   # what Scope holds is saved: disconnect needs no confirmation
        app.audit("scope_record", {"action": action, "scope": scope, "path": path}, OpClass.EDIT, "ok")
        return r

    @mcp.tool(annotations=READ)
    async def rec_export(id: str, fmt: str = "csv_wide", out_path: str | None = None,
                         symbols: list[str] | None = None, fill: bool = False, overwrite: bool = False) -> dict[str, Any]:
        """Export a recording to CSV. csv_wide: one row per PLC sample time, one column per variable and per array
        element (name[i]); an empty cell means no sample of that variable at that time (fill=true forward-fills —
        handy for charts, but a cell then no longer means "sampled now"). csv_long: t_ms,symbol,value. Both include
        plc_filetime (the PLC's own sample timestamp) when available. Default path next to the JSONL. An out_path
        must be in a folder this server writes to ([server] output_dirs adds more). An existing file — also an
        earlier export at the default path with other symbols/fill, or of a recording that has grown since — is only
        replaced with overwrite=true; an identical re-export (same source bytes and parameters) is not refused."""
        if out_path:
            out_path = str(app.out_path(out_path, overwrite, "export"))
        return await app.mem_call(app.rec.export, id, fmt, out_path, symbols, fill, overwrite)

    @mcp.tool(annotations=DESTR)
    async def rec_delete(id: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Stop (if running) and delete a recording and its exports from disk. Destructive-class (field report R11):
        a recording is evidence — guarded mode needs tc_request_confirmation(action='rec_delete', target=<id>); the
        audit keeps the deleted file's size and SHA-256 so its content stays identifiable."""
        try:
            path = await asyncio.to_thread(app.rec._path_for, id)
        except RecorderError as exc:
            raise ToolError(str(exc)) from exc
        await app.permit(ctx, OpClass.DESTRUCTIVE, "rec_delete", path.stem, confirm_token)

        def _fingerprint() -> dict[str, Any]:
            import hashlib
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return {"file": str(path), "bytes": path.stat().st_size, "sha256": h.hexdigest(), "footer": K_footer(path)}
        fp = await asyncio.to_thread(_fingerprint)
        rid = path.stem
        part = app.scope_parts.get(rid)
        if part is not None:
            part.hold()                    # thirteenth review of 0.8.16: its Scope part is rec_delete's
        try:
            if part is not None:
                # first review of 0.8.17: a finish under way (the recording had ended) completes as it began — the
                # recording was deleted under it, and its Scope files were sealed into a link to nothing
                await part.idle.wait()
            if part is not None:
                async with part.link_lock:     # review 1 of 0.8.18: not between a finish's check and its link
                    res = await app.confirmed("rec_delete", {"id": rid, **fp}, OpClass.DESTRUCTIVE,
                                              app.rec_call(app.rec.delete, rid))
            else:
                res = await app.confirmed("rec_delete", {"id": rid, **fp}, OpClass.DESTRUCTIVE,
                                          app.rec_call(app.rec.delete, rid))
            res["deleted_file_sha256"] = fp["sha256"]
            if part is not None:
                # Scope recorded with it: stopped, nothing saved (the recording it belonged to is gone). A delete that
                # failed or a call that ended before this: the part's owner finishes it once the delete's outcome is known
                _scope_answer(res, await part.finish("rec_delete", save=False))
                link = (res.get("scope") or {}).get("link")
                if isinstance(link, dict) and link.get("files"):
                    # saved before this delete (the recording had ended by itself): rec_delete removes the ADS
                    # recording and its exports only — the Scope files and their link stay
                    res["scope_files_kept"] = {"link": link.get("file"), "files": sorted(link["files"])}
        finally:
            if part is not None:
                part.release()
        return res


    # ======================================================================
    # test_* bench: stimulus / expectations against the running PLC
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def test_expect(checks: list[dict[str, Any]], within_ms: int = 0, interval_ms: int = 20) -> dict[str, Any]:
        """Assert conditions on live values. checks: [{symbol, op, value, tolerance?}], op ∈
        == != > >= < <= ~= (relative/abs tolerance) in between. within_ms>0 waits until all hold.
        Returns pass/fail per check with the time each one was met."""
        return await app.ads_call(app.bench.expect, checks, within_ms, interval_ms, timeout=within_ms / 1000 + 30, slow=True)

    @mcp.tool(annotations=READ)
    async def test_stable(symbol: str, duration_ms: int = 1000, tolerance: float = 0.0,
                          interval_ms: int = 20) -> dict[str, Any]:
        """Assert a value does not move more than tolerance for duration_ms (settle check for a
        weight, pressure, PID output, state)."""
        return await app.ads_call(app.bench.assert_stable, symbol, duration_ms, tolerance, interval_ms, timeout=duration_ms / 1000 + 30, slow=True)

    @mcp.tool(annotations=WRITE)
    async def test_response(stimulus: dict[str, Any], response: str, timeout_ms: int = 5000,
                            restore: bool = True, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write a stimulus ({symbol: value}) and measure the PLC-timestamped latency until
        `response` ('MAIN.fbValve.bOpen == TRUE') holds — how fast does the FB react? Writes are
        gated per symbol; originals are restored afterwards unless restore=false."""
        for k in stimulus:
            await app.permit(ctx, OpClass.WRITE, "ads_write", k)
        res = await app.ads_call(app.bench.measure_response, stimulus, response, timeout_ms, restore, timeout=timeout_ms / 1000 + 60, slow=True)
        app.audit("test_response", {"stimulus": stimulus, "response": response}, OpClass.WRITE, "ok", {"latency_ms": res.get("latency_ms")})
        return res

    @mcp.tool(annotations=WRITE)
    async def test_scenario(spec: dict[str, Any], ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Scripted FB test against the live PLC with a recording in parallel. spec = {name,
        record:{symbols|prefix|pattern, mode, cycle_ms}, steps:[{at_ms?, write:{sym:val}} |
        {expect:[checks], within_ms} | {wait_ms} | {wait_until:{symbol,op,value,timeout_ms}} |
        {stable:{symbol,duration_ms,tolerance}}], final_expect:[checks], timeout_ms, restore,
        stop_on_fail}. Every written symbol is gated before the run starts; written values are
        restored at the end. Returns per-step results, overall pass, and the recording id."""
        syms: list[str] = []
        for stp in spec.get("steps") or []:
            syms += list((stp.get("write") or {}).keys())
        for k in dict.fromkeys(syms):
            await app.permit(ctx, OpClass.WRITE, "ads_write", k)
        timeout = float(spec.get("timeout_ms", 120_000)) / 1000 + 120
        res = await app.ads_call(app.bench.scenario, spec, timeout=timeout, slow=True)
        app.audit("test_scenario", {"name": res.get("name"), "writes": syms}, OpClass.WRITE,
                  "pass" if res.get("pass") else "fail", {"recording": (res.get("recording") or {}).get("id")})
        return res

    # ======================================================================
    # diagnostics / snapshots / RPC
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ads_app_info() -> dict[str, Any]:
        """PLC application info (TwinCAT_SystemInfoVarList._AppInfo): project/app name, online
        change count, boot data loaded, task count, logged-in flag, app timestamp."""
        return await app.ads_call(app.bench.app_info, timeout=30)

    @mcp.tool(annotations=READ)
    async def ads_task_stats(task_index: int = 1, samples: int = 20, interval_ms: int = 50) -> dict[str, Any]:
        """PLC task timing (TwinCAT_SystemInfoVarList._TaskInfo[n]): cycle time, last/min/max/mean
        execution time over `samples`, load %, cycle-time-exceeded counter delta — the FB's cost
        on the real-time task."""
        return await app.ads_call(app.bench.task_stats, task_index, min(samples, 1000), interval_ms, timeout=samples * interval_ms / 1000 + 30, slow=True)

    @mcp.tool(annotations=READ)
    async def ads_ping(count: int = 10, port: int | None = None) -> dict[str, Any]:
        """ADS round-trip time statistics to the target (router/route/network health)."""
        return await app.ads_call(app.bench.ping, count, port, timeout=60)

    @mcp.tool(annotations=WRITE)
    async def ads_call_method(instance: str, method: str, inputs: list[dict[str, Any]] | None = None,
                              return_type: str | None = None, outputs: list[str] | None = None,
                              ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Invoke an FB method on the running PLC via ADS RPC. The method must carry
        {attribute 'TcRpcEnable'}. inputs: [{type:'LREAL', value: 12.5}, …] in VAR_INPUT order
        (use base types: INT not E_Enum); outputs: ['BOOL', …] in VAR_OUTPUT order. Write-class."""
        await app.permit(ctx, OpClass.WRITE, "ads_call_method", f"{instance}#{method}")
        res = await app.confirmed("ads_call_method", {"instance": instance, "method": method, "inputs": inputs}, OpClass.WRITE, app.ads_call(app.bench.call_method, instance, method, inputs, return_type, outputs, timeout=60))
        return res

    @mcp.tool(annotations=EDIT)
    async def snap_save(name: str, symbols: list[str] | None = None, prefix: str | None = None,
                        pattern: str | None = None, max_symbols: int = 2000) -> dict[str, Any]:
        """Save a named snapshot of live values (expanded like rec_start) for regression
        comparison — parameters, recipes, whole FB instances before/after a change."""
        return await app.ads_call(app.bench.snapshot_save, name, symbols, prefix, pattern, max_symbols, timeout=120)

    @mcp.tool(annotations=READ)
    async def snap_compare(a: str, b: str = "live", tolerance: float = 0.0, symbols: list[str] | None = None) -> dict[str, Any]:
        """Diff snapshot `a` against `b` (another snapshot or 'live'), numeric tolerance applies."""
        return await app.ads_call(app.bench.snapshot_compare, a, b, tolerance, symbols, timeout=120)

    @mcp.tool(annotations=READ)
    async def snap_list() -> list[dict[str, Any]]:
        """Saved snapshots."""
        return await asyncio.to_thread(app.bench.snapshot_list)

    @mcp.tool(annotations=READ)
    async def rec_pulses(id: str, symbol: str, max_list: int = 200) -> dict[str, Any]:
        """Pulse / dwell analysis of one recorded symbol: for BOOL → pulse count, high/low widths,
        period, frequency, duty (fine-dosing valve pulses); for other types → state timeline with
        dwell times."""
        return await app.mem_call(app.rec.pulses, id, symbol, max_list)

    @mcp.tool(annotations=READ)
    async def rec_latency(id: str, from_symbol: str, from_op: str, from_value: Any, to_symbol: str,
                          to_op: str, to_value: Any, max_pairs: int = 500) -> dict[str, Any]:
        """Edge-to-edge latency in a recording: from each time `from_symbol from_op from_value`
        becomes true to the next time `to_symbol to_op to_value` becomes true (PLC timestamps).
        E.g. bOpenCmd == TRUE → rFlow > 0.5."""
        return await app.mem_call(app.rec.latency, id, from_symbol, from_op, from_value, to_symbol, to_op, to_value, max_pairs)

    # ======================================================================
    # static checks
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def proj_libraries() -> dict[str, Any]:
        """Library versions (K3): every library reference of each PLC project — the version it asks for, the version
        it effectively resolves to on this PC ('*' = newest installed) and what is installed in TwinCAT's library
        repository. Flags a pinned version that is not installed. Reads the .plcproj and folder names only."""
        await app.proj_call(app.proj._ensure)
        files = [pp["file"] for pp in app.proj.plcproj]
        if not files:
            raise ToolError("no .plcproj under the project root (proj_scan / project.root)")
        installed = await asyncio.to_thread(app.kb.installed_libraries)
        out = await asyncio.to_thread(K.library_report, files, installed)
        out["repository"] = [str(d) for d in await asyncio.to_thread(app.kb.library_folders)]
        if not out["repository"]:
            out["note"] += "; no library repository found on this PC (not the engineering PC?) — installed versions unknown"
        return out

    @mcp.tool(annotations=READ)
    async def proj_interface(name: str) -> dict[str, Any]:
        """Public interface of an FB/program: inputs/outputs/in-out with types and comments,
        methods (access, signature), properties (get/set), actions, extends/implements."""
        return await app.proj_check(codecheck.fb_interface, name)

    @mcp.tool(annotations=READ)
    async def proj_lint(name: str | None = None, rules: list[str] | None = None,
                        prefixes: dict[str, list[str]] | None = None) -> dict[str, Any]:
        """Static checks over one object or the whole project: unused locals, naming prefixes
        (override map via `prefixes`), undocumented VAR_INPUT/OUTPUT, TODO markers, REAL
        compared with =, VAR_INPUT assigned inside an FB, empty methods, GVL/ENUM without
        qualified_only, magic numbers, FB_init/FB_exit without SUPER^; OOP/safety: interface_contract
        (every member of each IMPLEMENTS interface present with matching signature — info, not error,
        when the interface or a base lives in a library), extends_cycle, abstract_instantiation,
        shadowed_member (method input/local hides an FB member), unsigned_loop (BY -1 or TO n-1 on an
        unsigned counter), new_delete (__NEW without __DELETE, target lacking enable_dynamic_creation).
        Deterministic and offline; the compiler (xae_build + xae_diagnostics) stays the authority.
        rules= subset to run."""
        return await app.proj_check(codecheck.lint, name, rules, prefixes or cfg.project.naming_prefixes or None)

    @mcp.tool(annotations=READ)
    async def proj_call_graph(root: str, depth: int = 4) -> dict[str, Any]:
        """Call graph from a program/FB: FB instances it invokes, functions and methods it calls,
        recursively to `depth` (what runs when this POU runs)."""
        return await app.proj_check(codecheck.call_graph, root, min(depth, 10))

    @mcp.tool(annotations=EDIT)
    async def tcunit_scaffold(fb_name: str, out_dir: str | None = None, add_to_plcproj: bool = True,
                              overwrite: bool = False) -> dict[str, Any]:
        """Generate <FB>_Tests.TcPOU (TcUnit.FB_TestSuite) with Test_InitialState and one test
        method per public method, arranged from the FB interface, and register it in the
        .plcproj. Assertions are left as TODO stubs."""
        if out_dir:
            out_dir = str(app.out_path(out_dir, what="scaffold folder", is_dir=True))       # R4
        # N12 / N109: the PLC project is what its .plcproj compiles (N106). A scaffold it does not compile — not
        # registered (add_to_plcproj=false), and not over a file the project compiles — is no code edit of the
        # PLC project, wherever it is written, so edit_requires_config_mode does not apply to it. (N109: an out_dir
        # under the project root but outside every PLC project was refused in RUN.)
        view = (await app.proj_call(app.proj_views, fb_name))[0]                    # N113: the FB's own PLC project
        dest = await app.proj_call(codecheck.scaffold_dest, view, fb_name, out_dir)
        touches_project = add_to_plcproj or (Path(dest).exists() and await app.proj_call(app.proj.is_member, dest))
        await app.permit(None, OpClass.EDIT, "tcunit_scaffold" if touches_project else "tcunit_scaffold_external", fb_name)
        res = await app.confirmed("tcunit_scaffold", {"fb": fb_name}, OpClass.EDIT, app.proj_call(codecheck.tcunit_scaffold, view, fb_name, out_dir, add_to_plcproj, overwrite))
        return res


    # ======================================================================
    # whole-project variable access
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ads_dump(pattern: str | None = None, include_system: bool = False, out_dir: str | None = None,
                       max_total_mb: int = 64, max_leaves: int = 500_000) -> dict[str, Any]:
        """Read EVERY variable on the PLC (all top-level symbols, fully expanded through the
        datatype table — structs, FB instances, arrays of structs) in one pass and write a flat CSV
        (path,type,value,note) and a nested JSON tree built from the same values, so the two files never disagree.
        pattern limits the roots ('MAIN.*', 'GVL_*'); system symbols excluded unless include_system. With a project
        configured its own programs and GVLs come first; without one the order is alphabetical (origin unknown).
        POINTER TO / REFERENCE TO / INTERFACE values are addresses (NULL = 0x0000…0000) — the target is never
        expanded. An array beyond 4096 elements or a value beyond 64 KiB is not expanded: both files show the same
        {not_expanded, bytes, elements, head_hex}. Big symbols are read in 256 KiB pieces. max_leaves bounds CSV and
        JSON together; max_total_mb (MiB) bounds the CSV + JSON on disk (written_mib; the bytes read from the PLC are
        not counted): a row is written only when it, the root's JSON with it and the ending still to be written (the
        footer naming what is left out) fit — nothing is estimated. complete=false names
        every root that is missing or cut (skipped_roots) and every root that could not be read (failed_roots, also
        in the CSV note column and the JSON footer). not_on_target names the programs/GVLs of the source that have no
        symbol on the target, with the cause (not linked, or another program on the target). Returns counts,
        largest_roots, file paths and a 50-leaf preview."""
        od = str(app.out_path(out_dir, what="dump folder", is_dir=True)) if out_dir else str(Path(cfg.safety.audit_log).parent / "dumps")
        if not 1 <= max_total_mb <= 4096 or not 1 <= max_leaves <= 5_000_000:
            raise ToolError("max_total_mb must be 1..4096 and max_leaves 1..5,000,000")
        own: set[str] | None = None
        declared: dict[str, dict[str, Any]] | None = None
        try:
            if app.proj.root is not None:
                await asyncio.to_thread(app.proj.ensure_fresh)
                own = await asyncio.to_thread(app.proj.unit_names, "PROGRAM", "GVL") or None    # N88: under the lock
                declared = await asyncio.to_thread(app.proj.declared_roots)
        except Exception:  # noqa: BLE001 — ordering is a nicety; without the project the order is alphabetical
            own = declared = None

        def on_timeout() -> str:
            # N52: the verdict is read under the dump's commit lock — "kept" only if it was renamed into place
            return dump_timeout_message(app.ads.dump_outcome(), 900)
        res = await app.ads_call(app.ads.dump, pattern, include_system, od, max_total_mb * MIB, max_leaves,
                                 cancel=app.ads_bulk_worker.cancel_event, own_roots=own, timeout=900, bulk=True,
                                 timeout_msg=on_timeout)
        if declared:
            # N92: a root of the source with no symbol on the target is named, with its cause (the same symbol table)
            try:
                nt = not_on_target(declared, await app.ads_call(app.ads.root_names, timeout=60), pattern)
            except ToolError as exc:
                res["not_on_target_error"] = f"not checked: {exc}"
            else:
                missing = nt["roots"]
                if nt["not_judged"]:
                    res["not_on_target_not_judged"] = nt["not_judged"]
                if missing:
                    res["not_on_target"] = missing
                    if nt["plc_project"]:
                        res["not_on_target_plc_project"] = nt["plc_project"]
                    res["not_on_target_warning"] = (
                        f"{len(missing)} program/GVL root(s) of the source have no symbol on the target "
                        f"({', '.join(m['root'] for m in missing[:5])}{' …' if len(missing) > 5 else ''}): not in this "
                        "dump — not_on_target says why")
        return res

    @mcp.tool(annotations=READ)
    async def ads_leaves(name: str, max_leaves: int = 5000, with_values: bool = False) -> dict[str, Any]:
        """Enumerate every scalar leaf under a symbol with byte offsets (datatype table):
        'MAIN' → all program variables, 'MAIN.arrSteps' → arrSteps[1].nId … With values=true
        each leaf is decoded from the symbol's bytes, read in bounded pieces (a value that is not
        expanded: its head only); value_note says why a value is missing or partial."""
        groups = await app.ads_call(app.ads.leaves_under, name, max_leaves, timeout=120)
        out: list[dict[str, Any]] = []
        for root, leaves in groups:
            vals: dict[str, Any] = {}
            notes: dict[str, str] = {}
            err: str | None = None
            if with_values:
                try:
                    vals, notes = await app.ads_call(app.ads.leaf_values, root, leaves, timeout=120)
                except ToolError as exc:
                    err = str(exc)
            for lf in leaves:
                d = {"path": lf.path, "type": lf.type, "offset": lf.offset, "size": lf.size, "root": root.name}
                if lf.enum:
                    d["enum"] = lf.enum
                if lf.note:
                    d["note"] = lf.note
                if with_values:
                    d["value"] = vals.get(lf.path)
                    if err is not None or lf.path in notes:
                        d["value_note"] = err if err is not None else notes[lf.path]
                out.append(d)
        return {"name": name, "roots": [g[0].name for g in groups], "count": len(out), "leaves": out[:max_leaves],
                "truncated": len(out) > max_leaves}

    @mcp.tool(annotations=READ)
    async def ads_types(type_name: str | None = None, pattern: str | None = None, limit: int = 200) -> dict[str, Any]:
        """Runtime datatype table: describe one type (members with offsets/sizes, array bounds,
        enum literals, attributes) or list types matching a pattern."""
        if type_name:
            return await app.ads_call(app.ads.type_info, type_name, timeout=60)
        return await app.ads_call(app.ads.list_types, pattern, min(limit, 5000), timeout=60)

    @mcp.tool(annotations=READ)
    async def proj_variables(scope: str | None = None, max_array: int = 64, max_items: int = 200_000,
                             include_temp: bool = False) -> dict[str, Any]:
        """Every variable the project source declares, flattened to runtime instance paths by
        walking programs and GVLs through FB/STRUCT definitions (MAIN.fbFill.fbValve.bOpen …).
        scope = one program/GVL/FB to walk instead of all. Library-typed variables stay as one
        item with their type. A name declared in several PLC projects is walked in the PLC project on the connected ADS
        port (the .tsproj's AmsPort); pin another with '<PLC project>@<name>'."""
        return await app.proj_call(app.proj.variables, scope, max_array, max_items, include_temp, port=app.ads.port)

    @mcp.tool(annotations=READ)
    async def proj_vs_runtime(scope: str | None = None, max_items: int = 200_000) -> dict[str, Any]:
        """Cross-check the source against the running PLC: variables declared in the project but
        not present online (not compiled / not activated / optimised away) and online variables
        under the same programs/GVLs that the source does not declare (stale boot project, library
        internals); roots_not_on_target names a program/GVL of which the target has no symbol at all, with the cause
        (a GVL nothing references is not linked). Use after a deploy to prove the target runs this source."""
        return await _vs_runtime(app, scope, max_items)

    # ======================================================================
    # wf_* composite workflows
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def wf_debug_symbol(symbol: str, watch_ms: int = 1500, context_lines: int = 12) -> dict[str, Any]:
        """One-call debugging dossier for a variable: live value, declaring file:line chain, the
        statements that write it, code excerpts around them, and a short change watch."""
        return await workflows.debug_symbol(app, symbol, watch_ms, context_lines)

    @mcp.tool(annotations=EDIT)
    async def wf_fb_profile(instance: str, duration_s: float = 10.0, cycle_ms: float = 1.0, task_index: int = 1) -> dict[str, Any]:
        """Profile an FB instance for duration_s: records every leaf (change mode), samples task
        timing in parallel, then reports most-active symbols, boolean pulse rates, state dwell
        timelines, silent symbols and the FB's cost on the task. Read-only on the PLC."""
        return await workflows.fb_profile(app, instance, min(duration_s, 300), cycle_ms, task_index)

    @mcp.tool(annotations=EDIT)
    async def wf_release_check(plc_project_path: str | None = None, run_build: bool = True,
                               lint_rules: list[str] | None = None) -> dict[str, Any]:
        """Go/no-go before a deploy: git changes, build result, lint, source-vs-runtime, TcUnit
        result on the target, task health. Writes nothing to the PLC."""
        if run_build:
            await app.permit(None, OpClass.EDIT, "xae_build")
        return await workflows.release_check(app, plc_project_path, run_build, lint_rules)

    @mcp.tool(annotations=READ)
    async def wf_project_overview() -> dict[str, Any]:
        """Orientation in one call: index summary, programs with call trees, FBs with interfaces,
        GVLs/DUTs, lint summary, git state."""
        return await workflows.project_overview(app)

    @mcp.tool(annotations=READ)
    async def rec_compare(id_a: str, id_b: str, symbols: list[str] | None = None) -> dict[str, Any]:
        """Before/after comparison of two recordings: per-symbol stat deltas (count, min, max,
        mean, changes, edges) and pulse width/period deltas for BOOLs — timing regression after a
        code change."""
        return await app.mem_call(app.rec.compare, id_a, id_b, symbols)


    # ======================================================================
    # XAE dialogs / I/O / links / discovery
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def xae_dialogs() -> dict[str, Any]:
        """Modal dialogs currently open in the attached XAE shell (title, class, text, buttons), the
        watchdog's auto-answer rules and its recent log. A pending dialog blocks every COM call. Any window that
        holds the shell modal (its main window disabled) is listed, whatever its class (WPF About / Options, WinForms):
        its buttons read through UI Automation, or buttons null + buttons_note when they cannot be read; unidentified
        when the shell is modal but no window could be named."""
        out = await asyncio.to_thread(lambda: {**app.xae.watchdog.status(), "live": app.dialogs_now()})
        held = app.xae_held
        if held is not None and not held["future"].done():
            out["held_by_dialog"] = {"job": held["job"], "dialog": held["dialog"], "since_utc": held["since_utc"],
                                     "note": "that call is still inside the shell's modal loop; every XAE call is "
                                             "refused until the dialog is answered (xae_dialog_resolve)"}
        if app.xae_late:
            out["late_results"] = list(app.xae_late)
        return out

    @mcp.tool(annotations=DESTR)
    async def xae_dialog_resolve(button: str, hwnd: int | None = None, remember: bool = False,
                                 confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Click a button on an open XAE dialog ('OK', 'Yes', 'No', 'Cancel'…). remember=true adds an
        auto-answer rule for that title (never for machine-affecting prompts). Control-class;
        machine-affecting prompts (activate/run/download/login/delete/safety) are destructive-class. A window that is
        not a standard dialog is pressed through UI Automation; one whose buttons cannot be read is refused (close it
        in XAE) — never closed by the tool."""
        pend = await asyncio.to_thread(lambda: __import__("twincat_mcp.dialogs", fromlist=["list_dialogs"]).list_dialogs(app.xae.pid))
        target = next((d for d in pend if hwnd is None or d.hwnd == hwnd), None)
        op = OpClass.DESTRUCTIVE if (target and target.machine_affecting()) else OpClass.CONTROL
        await app.permit(ctx, op, "xae_dialog_resolve", f"{(target.title if target else '?')}:{button}", confirm_token)
        res = await asyncio.to_thread(app.xae.watchdog.resolve, button, hwnd, remember)
        from .dialogs import CANNOT_PRESS
        outcome = ("ok" if res.get("resolved") else "refused" if str(res.get("reason", "")).endswith(CANNOT_PRESS)
                   else "not-found")                   # N173: a window whose buttons the tool cannot press
        app.audit("xae_dialog_resolve", {"button": button, "dialog": target.title if target else None}, op, outcome, res)
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_io_disable(disable: bool = True, only: list[str] | None = None) -> dict[str, Any]:
        """Set/clear the Disabled flag on I/O devices under TIID (all, or the named ones) so a
        configuration activates without the hardware present — TcUnit on a PC, dry runs."""
        await app.permit(None, OpClass.EDIT, "xae_io_disable", "TIID")
        res = await app.confirmed("xae_io_disable", {"disable": disable, "only": only}, OpClass.EDIT, app.xae_call(app.xae.io_disable, disable, only, timeout=120))
        return res

    @mcp.tool(annotations=READ)
    async def xae_links(plc_project_path: str) -> dict[str, Any]:
        """Variable links of the PLC instance: every AT %I*/%Q* variable (from the instance's XML) with its link
        targets (the project's mapping table, ProduceMappingInfo); lists the unlinked ones — array elements too, the
        usual reason an output never moves — and links whose variable is gone. plc_project_path: plc_path,
        iec_project_path or instance_path. What could not be read is said (unlinked_count null), never counted as 0."""
        return await app.xae_call(app.xae.links, plc_project_path, timeout=300)

    @mcp.tool(annotations=READ)
    async def ads_discover(timeout_s: float = 2.0, broadcast: str = "255.255.255.255") -> dict[str, Any]:
        """Broadcast search for TwinCAT targets on the local network (UDP 48899): NetId, host
        name, TwinCAT version, IP — the same thing XAE's 'Broadcast Search' does."""
        return await app.ads_call(app.ads.discover, min(timeout_s, 10), broadcast, timeout=20, slow=True)

    # ======================================================================
    # HMI (TE2000 / TcVIS) bindings
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def hmi_scan(root: str | None = None) -> dict[str, Any]:
        """Index HMI symbol bindings: TE2000 .view/.content/.usercontrol/.json (%s%PLC1.X%/s%) and TF1800 .TcVIS
        (VisualElemMember paths -> kind 'tcvis'; ST input-action assignments -> kind 'tcvis_write').
        root defaults to project.hmi_root, else the project root (which is where .TcVIS files live)."""
        r = root or cfg.project.hmi_root or cfg.project.root
        if not r:
            raise ToolError("no HMI root: pass root= or set TCMCP_HMI_ROOT")
        try:
            return await asyncio.to_thread(app.hmi.scan, r)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=READ)
    async def hmi_bindings(pattern: str | None = None, kind: str | None = None, limit: int = 500) -> dict[str, Any]:
        """Which HMI files/controls bind a PLC symbol (fnmatch on the symbol path)."""
        try:
            return await asyncio.to_thread(app.hmi.find, pattern, kind, limit)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    @mcp.tool(annotations=READ)
    async def hmi_vs_runtime(check_source: bool = True) -> dict[str, Any]:
        """Broken HMI bindings: symbols bound in the HMI that do not exist on the running PLC (and,
        with check_source, whether the source declares them), plus declared variables with no HMI."""
        try:
            syms = await asyncio.to_thread(app.hmi.symbols)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        roots = {s.split(".")[0] for s in syms}
        online: set[str] = set()
        for r in roots:
            try:
                for root, leaves in await app.ads_call(app.ads.leaves_under, r, 200_000, timeout=300):
                    online.update(lf.path.lower() for lf in leaves)
            except ToolError:
                continue
        declared = None
        src_note = None
        if check_source:
            try:
                pv = await app.proj_call(app.proj.variables, None, 64, 200_000, False, port=app.ads.port)
                src_note = pv.get("note")
                declared = None if src_note else {it["path"].lower() for it in pv["items"] if "[*]" not in it["path"]}
            except ToolError:
                declared = None
        res = await asyncio.to_thread(app.hmi.compare, online, declared)
        if src_note and isinstance(res, dict):
            res["source_not_checked"] = f"{src_note} — the source was not compared"
        return res



    # ======================================================================
    # Experiments: define → run → measure → evaluate → report
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def exp_template(kind: str = "dosing_fill") -> dict[str, Any]:
        """A complete example experiment spec to edit (YAML text + the same as JSON): factors × levels,
        runs per level, preconditions, start/done conditions, measures (last/latency/pulses/settle/
        dwell/edges/expr…), metrics (mean/std/cpk/p95), criteria, writes_allowed. kind: dosing_fill."""
        if kind != "dosing_fill":
            raise ToolError("kinds: dosing_fill")
        return {"yaml": X.dump_spec(X.DOSING_TEMPLATE), "spec": X.DOSING_TEMPLATE,
                "measure_kinds": X.MEASURE_KINDS, "stats": X.STATS, "criterion_ops": X.CRIT_OPS,
                "before_you_save": ["the symbols are an example dosing machine's — replace them with yours (ads_list_symbols)",
                                    "'profile' names a capture profile that must already exist: rec_profile_save it first, "
                                    "with every symbol the measures read",
                                    "'writes_allowed' must list every symbol the spec writes (factors, start, before/after)",
                                    "run.ack: falls (default) | edge (process done again within a cycle) | none; "
                                    "before/after accept {wait_ms: N} and {wait_until: {...}} items"],
                "run_options": {"ack": "falls | edge | none", "ack_timeout_ms": "default min(10000, done_when.timeout_ms)",
                                "before/after items": "{symbol, value} | {wait_ms} | {wait_until: {symbol, op, value, timeout_ms}}"}}

    @mcp.tool(annotations=READ)
    async def exp_validate(spec: str | dict[str, Any] | None = None, name: str | None = None,
                           resolve_symbols: bool = True) -> dict[str, Any]:
        """Validate a spec (YAML/JSON text, a dict, or a saved experiment by name): structure, measures,
        metrics, criteria, writes_allowed vs what the spec writes, the server guard's view of each
        write, symbol resolution on the connected PLC, planned run count and a duration estimate."""
        try:
            sp = X.load_spec(spec) if spec is not None else app.exp_store.spec(name or "")
        except X.ExperimentError as exc:
            raise ToolError(str(exc)) from exc
        types: dict[str, str] = {}
        if resolve_symbols:
            for sym in X.writes_in_spec(sp) + X.reads_in_spec(sp):
                try:
                    types[sym] = (await app.ads_call(app.ads.symbol_info, sym, timeout=20)).type
                except ToolError:
                    pass
        prof_syms = None
        try:
            prof_syms = [i.symbol for i in app.profiles.get(sp.get("profile", "")).items]
        except Exception:  # noqa: BLE001
            pass
        res = await asyncio.to_thread(
            X.validate_spec, sp, (lambda s: types.get(s)) if resolve_symbols else None, app.write_check,
            lambda n: any(p["name"] == n for p in app.profiles.list()), prof_syms)
        return res

    @mcp.tool(annotations=EDIT)
    async def exp_save(spec: str | dict[str, Any], replace: bool = True) -> dict[str, Any]:
        """Save an experiment spec (YAML or JSON) under <experiments_dir>/<name>/spec.yaml. Runs already
        recorded for that name are kept; resume picks up where they stopped."""
        try:
            sp = X.load_spec(spec)
            v = X.validate_spec(sp, profile_exists=lambda n: any(p["name"] == n for p in app.profiles.list()))
            if v["problems"]:
                raise ToolError("spec invalid: " + "; ".join(v["problems"]))
            if not replace and (app.exp_store.dir(sp["name"]) / "spec.yaml").exists():
                raise ToolError(f"experiment {sp['name']!r} exists; replace=true to overwrite the spec")
            path = await asyncio.to_thread(app.exp_store.save_spec, sp)
        except X.ExperimentError as exc:
            raise ToolError(str(exc)) from exc
        app.audit("exp_save", {"name": sp["name"]}, OpClass.EDIT, "ok")
        return {"saved": str(path), "name": sp["name"], "warnings": v["warnings"], "planned_runs": v["planned_runs"],
                "levels": v["levels"]}

    @mcp.tool(annotations=READ)
    async def exp_list() -> dict[str, Any]:
        """Experiments on disk with run counts, state and whether a report exists."""
        return {"dir": str(app.exp_store.root), "experiments": await asyncio.to_thread(app.exp_store.list)}

    @mcp.tool(annotations=READ)
    async def exp_get(name: str) -> dict[str, Any]:
        """Spec, status and run summary of one experiment."""
        try:
            sp = await asyncio.to_thread(app.exp_store.spec, name)
        except X.ExperimentError as exc:
            raise ToolError(str(exc)) from exc
        runs = app.exp_store.runs(name)
        return {"name": name, "spec": sp, "status": app.exp_store.status(name), "runs": len(runs),
                "runs_ok": len([r for r in runs if r.get("status") == "ok"]),
                "levels": X.levels_of(sp), "has_report": (app.exp_store.dir(name) / "report.md").exists()}

    @mcp.tool(annotations=READ)
    async def exp_runs(name: str, last_n: int = 100) -> dict[str, Any]:
        """Run records: level, status, measures, recording ids, duration, error."""
        runs = await asyncio.to_thread(app.exp_store.runs, name)
        return {"name": name, "count": len(runs), "runs": runs[-last_n:]}

    @mcp.tool(annotations=WRITE)
    async def exp_run(name: str, max_runs: int | None = None, resume: bool = True, dry_run: bool = False,
                      confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:
        """Run an experiment in the background: per level × repetition → preconditions → factor values
        written → snapshot → capture profile started → stimulus → start acknowledged (done_when must go
        false) → done_when → settle → stop → measures → runs.jsonl. Factor values are read first and
        restored at the end or on abort (read back and checked). Every write is re-checked against the
        server guard, pinned to the machine the experiment started on, and audited with symbol and value;
        a refusal, a target change or a timed-out write stops the experiment. dry_run resolves every
        symbol, coerces condition values (enum names → ints) and prints the plan without writing.
        Starting is control-class: in guarded mode it asks once (elicitation / confirm_token); in full
        mode it does not. Only one experiment may run at a time."""
        if not dry_run:
            await app.permit(ctx, OpClass.CONTROL, "exp_run", target=name, confirm_token=confirm_token)
            live = [n for n, r in app.runners.items() if r.status().get("alive")]
            if live:
                raise ToolError(f"experiment {live[0]!r} is running on this target; exp_abort it or wait")
        try:
            runner = app.runners.get(name)
            if runner is None or runner.store is not app.exp_store:
                runner = X.Runner(app.exp_store, name, app.exp_deps())
                app.runners[name] = runner
            res = await asyncio.to_thread(runner.start, max_runs, resume, dry_run)
        except X.ExperimentError as exc:
            raise ToolError(str(exc)) from exc
        app.audit("exp_run", {"name": name, "dry_run": dry_run, "max_runs": max_runs}, OpClass.CONTROL, "started" if not dry_run else "dry_run")
        return res

    @mcp.tool(annotations=READ)
    async def exp_status(name: str) -> dict[str, Any]:
        """Live state of a running experiment: planned/done/failed, current level, ETA, last error,
        live recording ids. For an experiment from an earlier session, the persisted status."""
        r = app.runners.get(name)
        if r is not None:
            return r.status()
        st = app.exp_store.status(name)
        if not st:
            raise ToolError(f"no experiment {name!r} or it never ran")
        st["alive"] = False
        return st

    @mcp.tool(annotations=EDIT)
    async def exp_abort(name: str, reason: str = "requested") -> dict[str, Any]:
        """Stop a running experiment after the current step; recordings are closed, factor values restored."""
        r = app.runners.get(name)
        if r is None or not r.status().get("alive"):
            raise ToolError(f"experiment {name!r} is not running")
        app.audit("exp_abort", {"name": name, "reason": reason}, OpClass.EDIT, "ok")
        return r.abort(reason)

    @mcp.tool(annotations=READ)
    async def exp_evaluate(name: str) -> dict[str, Any]:
        """Metrics per level with 95 % CIs and the criteria verdicts (pass / fail / inconclusive with the
        run count that would settle it). Deterministic from runs.jsonl; rerunnable by anyone."""
        try:
            sp = app.exp_store.spec(name)
        except X.ExperimentError as exc:
            raise ToolError(str(exc)) from exc
        return await asyncio.to_thread(X.evaluate, sp, app.exp_store.runs(name))

    @mcp.tool(annotations=EDIT)
    async def exp_report(name: str, interpretation: str | None = None, author: str = "agent",
                         firmware_check: bool = False, fmt: str = "both") -> dict[str, Any]:
        """Write <experiments_dir>/<name>/report.md and report.html: identity (machine, TwinCAT version,
        project, git state, safety mode), criteria table, metrics per level, charts (primary measure
        per run; per-level mean/CI), run table, the spec, the audit log of writes during the run, and
        the interpretation — stored separately and labelled as not computed. interpretation= saves a
        new one (author=agent|operator|<name>). firmware_check=true runs proj_vs_runtime first."""
        try:
            sp = app.exp_store.spec(name)
        except X.ExperimentError as exc:
            raise ToolError(str(exc)) from exc
        runs = app.exp_store.runs(name)
        ev = X.evaluate(sp, runs)
        if interpretation:
            await asyncio.to_thread(app.exp_store.write_interpretation, name, interpretation, author)
        meta: dict[str, Any] = {"machine_serial": (sp.get("machine") or {}).get("serial"), "target": app.scope(),
                                "server_version": __version__, "safety_mode": app.guard.mode,
                                "operator": (sp.get("report") or {}).get("operator") or None,
                                "generated": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            di = await app.ads_call(app.ads.device_info, timeout=10)
            meta["device"] = f"{di.get('name')} {di.get('version')}"
        except ToolError:
            pass
        try:
            meta["project"] = cfg.xae.solution_path or cfg.project.root
            ch = await app.proj_call(app.proj.changed_units)
            meta["git"] = f"{ch.get('head', '')[:12]} {'dirty' if ch.get('files') or ch.get('units') else 'clean'}".strip()
        except ToolError:
            pass
        if firmware_check:
            try:
                vr = await _vs_runtime(app, None, 50_000)
                meta["firmware_match"] = (f"{vr['matched']}/{vr['declared']} declared symbols online, {len(vr['missing_online'])} missing"
                                          + ("" if vr.get("complete", True) else f" (incomplete: {vr['not_judged']['reason']})"))
            except ToolError as exc:
                meta["firmware_match"] = f"check failed: {exc}"
        st = app.exp_store.status(name)
        windows = [(se.get("started"), se.get("finished") or time.time()) for se in (st.get("sessions") or []) if se.get("started")]
        if windows:
            def inside(ts: str) -> bool:
                return any(time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(lo - 1)) <= ts[:19]
                           <= time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(hi + 1)) for lo, hi in windows)
            meta["audit"] = [a for a in app.guard.tail_audit(20000)
                             if inside(str(a.get("ts", ""))) and a.get("tool") in ("exp_run", "exp_write", "ads_write", "ads_write_many", "snap_apply", "test_scenario")]
        md, html = await asyncio.to_thread(X.render_report, sp, runs, ev, meta, app.exp_store.interpretation(name))
        d = app.exp_store.dir(name)
        out = {"name": name, "verdict": ev["verdict"], "runs": len(runs)}
        if fmt in ("md", "both"):
            (d / "report.md").write_text(md, encoding="utf-8")
            out["markdown"] = str(d / "report.md")
        if fmt in ("html", "both"):
            (d / "report.html").write_text(html, encoding="utf-8")
            out["html"] = str(d / "report.html")
        app.audit("exp_report", {"name": name, "fmt": fmt, "interpretation": bool(interpretation)}, OpClass.EDIT, "ok")
        out["preview"] = md[:3000]
        return out

    @mcp.tool(annotations=READ)
    async def exp_compare(a: str, b: str) -> dict[str, Any]:
        """Two experiments with the same levels side by side: metric deltas and whether their 95 % CIs
        separate (the before/after question with error bars)."""
        try:
            ea = X.evaluate(app.exp_store.spec(a), app.exp_store.runs(a))
            eb = X.evaluate(app.exp_store.spec(b), app.exp_store.runs(b))
        except X.ExperimentError as exc:
            raise ToolError(str(exc)) from exc
        return X.compare(ea, eb)

    # ======================================================================
    # Beckhoff Infosys reference (TwinCAT 3 documentation)
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def infosys_books(pattern: str | None = None, category: str | None = None, limit: int = 200) -> dict[str, Any]:
        """List Infosys books (documentation sets) with page counts and whether they are indexed. Source: the Help
        Viewer package installed with TwinCAT (docs.source='local', default — every book, offline) or the website's
        sitemap (docs.source='web'). pattern is a glob on the slug ('tcplclib_*', '*json*'); category: plc-library |
        function-tf | engineering-te | twincat-system | hardware | other. TwinCAT 2 books are excluded."""
        res = await app.docs_call("books", pattern, category, limit, timeout=300)
        st = app.docs_index_status()
        if isinstance(res, dict) and st.get("started") and not st.get("finished"):
            for b in res.get("books", []):
                if not b.get("indexed") and b.get("book") in (st.get("books") or []):
                    b["indexed"] = "indexing"
        return res

    @mcp.tool(annotations=READ)
    async def infosys_index(book: str, force: bool = False, max_pages: int | None = None) -> dict[str, Any]:
        """docs.source='web': index a book (or glob, e.g. 'tcplclib_tc3_*') from infosys.beckhoff.com — pages new or
        changed since the last run (Tc2_System ≈ 300 pages ≈ 30-60 s). The local Help Viewer package (default) has
        ONE index of every book, built as a whole (the first time by itself, later with kb_rebuild(index='infosys')
        once approved) — this tool then changes nothing and says so."""
        if any(c in book for c in "*?"):
            return await app.docs_call("index_books", book, force, max_pages, timeout=3600)
        return await app.docs_call("index_book", book, force, max_pages, timeout=1800)

    @mcp.tool(annotations=READ)
    async def infosys_search_symbols(query: str, kind: str | None = None, library: str | None = None,
                                     limit: Limit50 = 10) -> dict[str, Any]:
        """Find library symbols (function blocks, functions, structs, enums, aliases, interfaces,
        GVLs) by name fragment in the indexed books; exact > prefix > contains; with * or ? a glob on the name
        ('FB_EcCoE*', 'FB_Ec*Write'). kind: FunctionBlock |
        Function | Program | Method | Property | Action | Enum | Struct | Union | Alias | Interface |
        GlobalVariable | Library. library: 'Tc2_System' or the book slug. Indexes the default
        libraries on first use if nothing is indexed."""
        return await app.docs_call("search_symbols", query, kind, library, limit, False, timeout=300)   # indexing runs in the background, not inline

    @mcp.tool(annotations=READ)
    async def infosys_declaration(symbol: str, library: str | None = None) -> dict[str, Any]:
        """Declaration of a library symbol as data: kind, inputs/outputs/in_out (name, type, default,
        description from the page's table), struct members, enum values, return type, EXTENDS /
        IMPLEMENTS, methods (name, definition location, description) and properties (type, access,
        initial value) of an FB, plus the ST declaration text and the page URL. The thing to call
        before writing code against an FB you have not used before. A name documented by different pages (two
        libraries, two methods of one name) is refused as ambiguous with the candidates — pass library=; one page in
        several books is one answer (same_page_in); also_in names the other libraries. A member as '<FB>.<name>'
        (FB_WG_ComboFilter.Call): the page below that FB in the Help Viewer contents (Beckhoff titles a method page with
        its bare name); parent names the FB of a member. The twincat-docs skill's docs_declaration resolves names by
        the same rule."""
        return await app.docs_call("declaration", symbol, library, timeout=1800)

    @mcp.tool(annotations=READ)
    async def infosys_search(query: str, library: str | None = None, limit: PageLimit = 10,
                             web_fallback: bool | None = None, offset: Offset = 0) -> dict[str, Any]:
        """Full-text search over the indexed Infosys pages (FTS5, bm25-ranked, excerpt with [hits]); "quoted phrases"
        stay together. Pages: limit 1..50 (a larger one is used as 50, clamped), offset / next_offset for the next
        hits. A question in words falls back in tiers (query_mode, words_used); identifiers
        count as their words (FB_SocketConnect: socket, connect); a word in capitals is a keyword or acronym and always
        searched (FOR, BY, THIS, ST, AL — in lower case 'for', 'this' are English stop words), and a title that has it
        comes first ('ST instruction FOR'); code punctuation that finds nothing as written is searched as its words
        ('THIS^ SUPER^', 'S= R='); titles that name the query come first; an error
        code (0x745, 1861 alone, 'error 1861') lists the pages whose table defines it first, code tables before other
        pages (code_note); one page in several books — or placed twice in one — is listed once (same_page_in). The twincat-docs skill's docs_search is the same search. Local Help Viewer package (default): no network (web_fallback is ignored and said
        so), results carry the page's doc version and the package date. docs.source='web': when the index has nothing,
        a DuckDuckGo site: query verified against the sitemap; the 'backend' field says which path answered."""
        return await app.docs_call("search", query, library, limit, web_fallback, offset, timeout=300)

    @mcp.tool(annotations=READ)
    async def infosys_search_code(query: str, language: str | None = None, limit: Limit50 = 10,
                                  ignore_spacing: bool = False) -> dict[str, Any]:
        """Search the code examples of the indexed pages for a fragment — a call pattern, an attribute, a constant
        (case-insensitive substring of the code as written; non-breaking spaces count as spaces). language: one
        that occurs in the index (an unknown one is refused with the list), '' / 'none' = samples without a label.
        limit 1..50; total_matches says how many there are. ignore_spacing=true also matches when only the
        spacing around punctuation differs. Returns the matching lines with context and the page URL."""
        return await app.docs_call("search_code", query, language, limit, ignore_spacing, timeout=120)

    @mcp.tool(annotations=READ)
    async def infosys_get_page(ref: str, refresh: bool = False, max_chars: PageChars = 60000,
                               section: str | None = None, offset: Offset = 0) -> dict[str, Any]:
        """A documentation page as Markdown (headings, tables, fenced code). ref: full Infosys URL, 'book/pageid', a
        bare page id, or a symbol name ('ADSREAD'; a method documented in a section of its FB's page opens at that
        section). Read from the local Help Viewer package (any page in it, indexed or not; doc version + breadcrumb
        included) or, with docs.source='web', fetched live. outline lists the headings; section= gives the part under
        one heading (one named exactly wins, else every heading that contains it); max_chars (200-60000) of it from
        offset, next_offset when more follows — as the twincat-docs skill's docs_page.
        parent / children: the page's place in the Help Viewer contents — an FB's page lists its method and property
        pages, a method page names its FB."""
        return await app.docs_call("get_page", ref, refresh, max_chars, section, offset, timeout=600)

    # ======================================================================
    # exports for MES / HMI code, snapshots apply, XML validation, reports
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ads_export(types: list[str] | None = None, prefix: str | None = None, fmt: str = "csharp",
                         namespace: str = "Transflo.Plc", out_path: str | None = None,
                         overwrite: bool = False) -> dict[str, Any]:
        """Generate code from the runtime: fmt=csharp (explicit-offset structs + enums for
        Beckhoff.TwinCAT.Ads ReadAny/WriteAny), typescript (interfaces), json (type descriptions),
        or csharp_symbols (const symbol paths under prefix). types = struct/enum names; prefix =
        take the types of every symbol under it."""
        ts = await app.ads_call(app.ads.typesystem, timeout=60)
        if ts is None and fmt != "csharp_symbols":
            raise ToolError("datatype table unavailable on this target" + await app._config_hint())
        names = list(types or [])
        syms: list[dict[str, Any]] = []
        if prefix:
            groups = await app.ads_call(app.ads.leaves_under, prefix, 200_000, timeout=300)
            for root, leaves in groups:
                if root.type not in names:
                    names.append(root.type)
                syms += [{"name": lf.path, "type": lf.type} for lf in leaves]
        if fmt == "csharp":
            text = exporters.to_csharp(ts, names, namespace)
        elif fmt == "typescript":
            text = exporters.to_typescript(ts, names)
        elif fmt == "json":
            text = exporters.to_json(ts, names)
        elif fmt == "csharp_symbols":
            if not syms:
                raise ToolError("csharp_symbols needs prefix=")
            text = exporters.symbols_to_csharp(syms, namespace=namespace)
        else:
            raise ToolError("fmt must be csharp | typescript | json | csharp_symbols")
        res: dict[str, Any] = {"fmt": fmt, "types": names, "chars": len(text)}
        if out_path:
            op = app.out_path(out_path, overwrite, "export")
            op.write_text(text, encoding="utf-8")
            res["file"] = str(op)
        else:
            res["text"] = text
        return res

    @mcp.tool(annotations=WRITE)
    async def snap_apply(name: str, symbols: list[str] | None = None, dry_run: bool = False,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write a saved snapshot back to the PLC (recipe / parameter restore). dry_run lists the
        differences only. Every differing symbol is gated like ads_write."""
        plan = await app.ads_call(app.bench.snapshot_apply, name, symbols, True, timeout=120)
        if dry_run:
            return plan
        for d in plan["differences"]:
            await app.permit(ctx, OpClass.WRITE, "ads_write", d["symbol"])
        res = await app.ads_call(app.bench.snapshot_apply, name, symbols, False, timeout=300, slow=True)
        app.audit("snap_apply", {"snapshot": name, "written": res["written"]}, OpClass.WRITE, "ok" if not res["errors"] else "partial", res["errors"])
        return res

    @mcp.tool(annotations=EDIT)
    async def proj_validate_xml(name: str | None = None, fix: bool = False, regenerate_lineids: bool = False) -> dict[str, Any]:
        """Structural validation of .TcPOU/.TcDUT/.TcGVL files: XML well-formedness, Name/Id,
        GUID format + project-wide uniqueness, Declaration/Implementation presence, property
        accessor VAR blocks, LineIds vs implementation line counts, EOF newline. fix=true applies
        safe fixes (.bak written); regenerate_lineids rebuilds LineIds blocks."""
        if fix:
            await app.permit(None, OpClass.EDIT, "proj_validate_xml", name or "*")
        res = await app.proj_check(codecheck.validate_xml, name, fix, regenerate_lineids, set())   # one .bak per file
        if fix and res.get("fixed"):
            app.audit("proj_validate_xml", {"name": name, "fixed": res["fixed"]}, OpClass.EDIT, "ok")
        return res

    @mcp.tool(annotations=DESTR)
    async def ipc_eventlog(provider: str = "TwinCAT System Service", count: int = 50, log: str = "Application",
                           confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Windows event log of the target (Windows 7/10/11 IPCs): runs wevtutil on the IPC via
        the System Service, reads the result back and deletes the temp file. Also lists the event providers
        registered on the target whose name contains 'TwinCAT' or 'Beckhoff', so a provider that does not exist there
        (the default name differs between TwinCAT builds) is reported as such instead of an empty result.
        Not available on Windows CE / TC-BSD targets. Destructive-class because it starts a process on the IPC:
        confirm with tc_request_confirmation(action='ipc_eventlog', target=<log>)."""
        for label, v in (("provider", provider), ("log", log)):
            if not re.fullmatch(r"[\w .\-/]{1,120}", v or ""):
                raise ToolError(f"{label} {v!r}: only letters, digits, space, '.', '-', '/' allowed (it goes into a command line on the IPC)")
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_eventlog", log, confirm_token)
        try:
            folder = await app.ads_call(app.files.resolve_prefix_dir, "boot", timeout=60)
        except ToolError as exc:
            # #56: no guessed folder — the old fallback C:\TwinCAT\3.1\Boot is a stale legacy copy on 3.1.4026
            raise ToolError(f"cannot resolve the target's boot folder for the temp file ({exc}); nothing was started "
                            "on the IPC") from exc
        if folder.startswith("/"):
            raise ToolError(f"target boot folder is {folder}: not a Windows target (wevtutil exists only on Windows IPCs)")
        out = folder.rstrip("\\") + "\\tcmcp_eventlog.txt"
        marker = "===TCMCP_PROVIDERS==="
        q = f"*[System[Provider[@Name='{provider}']]]"
        cmd = (f'/c wevtutil qe {log} /q:"{q}" /c:{int(count)} /rd:true /f:text > "{out}" 2>&1 '
               f'& echo {marker} >> "{out}" & wevtutil ep | findstr /i /c:"TwinCAT" /c:"Beckhoff" >> "{out}"')
        await app.ads_call(app.files.start_process, r"C:\Windows\System32\cmd.exe", r"C:\Windows\System32", cmd, timeout=30)
        try:
            waited = await app.ads_call(app.files.wait_for_file, out, 20, 1.0, timeout=40, slow=True)
            if not waited.get("exists"):
                raise ToolError("wevtutil produced no output file (Windows CE/TC-BSD target, or no permission)")
            text_ = ""
            for _ in range(10):        # the provider listing is appended last: wait for the marker and a little after it
                await asyncio.sleep(1.0)
                txt = await app.ads_call(app.files.read_file, out, 2_000_000, True, "utf-8", timeout=120, slow=True)
                text_ = txt.get("text") or ""
                if marker in text_:
                    await asyncio.sleep(0.5)
                    text_ = (await app.ads_call(app.files.read_file, out, 2_000_000, True, "utf-8", timeout=120, slow=True)).get("text") or ""
                    break
        finally:
            # #56: the temp file must not stay behind; cmd.exe may still hold it for a moment → retry, then check
            del_err = ""
            for attempt in range(4):
                try:
                    await app.ads_call(app.files.delete_file, out, timeout=30)
                    del_err = ""
                except ToolError as exc:
                    del_err = str(exc)[:200]
                try:
                    still = await app.ads_call(app.files.exists, out, timeout=15)
                except ToolError:
                    still = None
                if still is False:
                    break
                await asyncio.sleep(1.0 + attempt)
            temp = {"path": out, "deleted": still is False}
            if still is not False:
                temp["error"] = (del_err or "the file is still there after the delete") + " — delete it with ipc_delete_file"
        entries, _, prov_txt = text_.partition(marker)
        providers = sorted({ln.strip() for ln in prov_txt.splitlines() if ln.strip()})
        entries = entries.strip()
        res = {"provider": provider, "log": log, "entries_text": entries, "bytes": len(entries.encode("utf-8")),
               "events": entries.count("Event["), "providers_on_target": providers, "temp_file": temp}
        if not entries or "No events were found" in entries:
            res["entries_text"] = ""
            res["note"] = (f"no events from provider {provider!r} in {log}" +
                           ("" if any(pv.lower() == provider.lower() for pv in providers)
                            else f" — that provider is not registered on this target; registered TwinCAT/Beckhoff providers: {providers or 'none'}"))
        elif entries.lower().startswith(("failed to", "the specified", "access is denied")):
            res["error"] = entries[:300]
        app.audit("ipc_eventlog", {"provider": provider, "count": count, "log": log}, OpClass.DESTRUCTIVE, "ok",
                  {"events": res["events"], "providers": len(providers)})
        return res

    @mcp.tool(annotations=READ)
    async def tc_session_report(out_path: str | None = None, overwrite: bool = False, inline: bool = False) -> dict[str, Any]:
        """Markdown handover of this session: mode changes, writes, deploys and other mutating
        calls from the audit log, recordings, snapshots and dumps produced. Written to
        ~/.twincat_mcp/reports/<ts>.md (or out_path)."""
        recs = app.guard.tail_audit(1000)
        since = time.strftime("%Y-%m-%dT%H:%M", time.localtime(app.started))
        mine = [r for r in recs if r.get("ts", "") >= since]
        lines = ["# twincat-mcp session report", f"- started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(app.started))}",
                 f"- target: {app.scope()}", f"- mode now: {app.guard.mode}", "", "## Mutating calls"]
        for r in mine:
            if r.get("op") in ("write", "control", "destructive", "edit"):
                lines.append(f"- {r['ts']} `{r['tool']}` {json.dumps(r.get('args'), default=str)[:160]} → {r.get('outcome')}")
        lines += ["", "## Recordings"]
        try:
            rl = await app.mem_call(app.rec.list)
            for r in rl["session"]:
                lines.append(f"- `{r['id']}` {r.get('name')} — {r['events']} events, {r['symbols']} symbols, {r.get('stop_reason') or 'running'} — {r['file']}")
        except ToolError:
            pass
        lines += ["", "## Snapshots"]
        for sn in await asyncio.to_thread(app.bench.snapshot_list):
            lines.append(f"- {sn['name']} ({sn['symbols']} symbols, {sn['taken']}) — {sn['file']}")
        dumps = sorted((Path(cfg.safety.audit_log).parent / "dumps").glob("dump-*.json")) if (Path(cfg.safety.audit_log).parent / "dumps").is_dir() else []
        if dumps:
            lines += ["", "## Dumps"] + [f"- {d}" for d in dumps[-10:]]
        text = "\n".join(lines) + "\n"
        p = app.out_path(out_path, overwrite, "report") if out_path else Path(cfg.safety.audit_log).parent / "reports" / f"{time.strftime('%Y%m%d-%H%M%S')}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        out = {"file": str(p), "mutating_calls": sum(1 for r in mine if r.get("op") != "read"),
               "lines": text.count("\n"), "bytes": len(text.encode("utf-8"))}
        # R12: the report is the file; the response carries its head unless inline=true asks for all of it
        if inline:
            out["markdown"] = text
        else:
            out["preview"] = "\n".join(text.splitlines()[:25])
            out["note"] = "full report in the file (proj_read_file / open it), or inline=true"
        return out

    # ======================================================================
    # ipc_* target OS via System Service
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ipc_read_file(path: str, max_bytes: int | None = None, as_text: bool = True,
                            encoding: str = "utf-8") -> dict[str, Any]:
        r"""Read a file from the target IPC's file system over ADS (no share needed). Binary → base64.
        Prefer the OS-independent prefixes, which the target resolves itself: 'boot:<name>' (the live TwinCAT
        boot folder — C:\ProgramData\Beckhoff\TwinCAT\3.1\Boot on 3.1.4026, \Hard Disk\TwinCAT\3.1\Boot on CE7),
        'bootprj:', 'bootdata:', 'target:', 'config:', 'install:', 'user1:'..'user9:'. On 4026 the old absolute
        C:\TwinCAT\3.1\Boot still exists but is a stale legacy copy (e.g. a CurrentConfig.xml from years ago) —
        do not draw conclusions from it."""
        return await app.ads_call(app.files.read_file, path, max_bytes, as_text, encoding, timeout=120, slow=True)

    @mcp.tool(annotations=READ)
    async def ipc_list_dir(pattern: str, max_entries: int = 100) -> dict[str, Any]:
        r"""List files on the target matching a Win32 pattern. Prefer 'boot:*' (the live boot folder on any
        target OS; the result names the absolute folder in resolved_pattern) over a hard-coded
        'C:\TwinCAT\3.1\Boot\*', which is the stale legacy folder on 3.1.4026. Other prefixes: see ipc_read_file."""
        return await app.ads_call(app.files.list_dir, pattern, max_entries, timeout=60)

    @mcp.tool(annotations=WRITE)
    async def ipc_write_file(path: str, text: str | None = None, base64_data: str | None = None,
                             append: bool = False, confirm_token: str | None = None,
                             ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write a file on the target IPC (text or base64). Control-class."""
        await app.permit(ctx, OpClass.CONTROL, "ipc_write_file", path, confirm_token)
        data = base64.b64decode(base64_data) if base64_data else (text or "").encode("utf-8")
        res = await app.confirmed("ipc_write_file", {"path": path, "bytes": len(data)}, OpClass.CONTROL, app.ads_call(app.files.write_file, path, data, append, timeout=300))
        return res

    @mcp.tool(annotations=DESTR)
    async def ipc_delete_file(path: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Delete a file on the target IPC. Destructive-class."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_delete_file", path, confirm_token)
        res = await app.confirmed("ipc_delete_file", {"path": path}, OpClass.DESTRUCTIVE, app.ads_call(app.files.delete_file, path, timeout=30))
        return res

    @mcp.tool(annotations=DESTR)
    async def ipc_start_process(path: str, directory: str = "", command_line: str = "",
                                confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Start a process on the target IPC (NT_StartProcess equivalent). Destructive-class."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_start_process", path, confirm_token)
        res = await app.confirmed("ipc_start_process", {"path": path, "cmd": command_line}, OpClass.DESTRUCTIVE, app.ads_call(app.files.start_process, path, directory, command_line, timeout=30))
        return res

    @mcp.tool(annotations=DESTR)
    async def ipc_reboot(delay_s: int = 0, shutdown_only: bool = False, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Reboot (or shut down) the target IPC via the System Service. Destructive-class."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_reboot", "target", confirm_token)
        res = await app.confirmed("ipc_reboot", {"delay_s": delay_s, "shutdown_only": shutdown_only}, OpClass.DESTRUCTIVE, app.ads_call(app.files.reboot, delay_s, not shutdown_only, timeout=30))
        return res

    # ======================================================================
    # xae_* Automation Interface
    # ======================================================================
    @mcp.tool(annotations=EDIT)
    async def xae_attach(progid: str | None = None, solution_path: str | None = None,
                         launch: bool | None = None) -> dict[str, Any]:
        """Attach to the running XAE shell / Visual Studio (Running Object Table) that has the solution: solution_path
        when given (from then on every xae_* tool works on it — flagged solution_mismatch when it is not
        xae.solution_path), else xae.solution_path. A solution no shell has open is never replaced by another one
        (refused, or launch=true starts a shell with it); several shells with different solutions and none named or
        configured: refused. If the solution is closed later, every xae_* tool refuses until it is open again (in any
        shell — they find it by themselves)."""
        return await app.xae_call(app.xae.attach, progid, solution_path, launch, timeout=180)

    @mcp.tool(annotations=READ)
    async def xae_status() -> dict[str, Any]:
        """Shell version, open solution, projects, TwinCAT project name, target NetId, started flag.
        Attaches to the shell first if not attached yet (as every xae_* tool does). solution_bound: the solution the
        xae_* tools work on; solution_mismatch / refused: it is not open (they refuse); reattached: it was found in
        a shell again."""
        def _st():
            if app.xae.dte is None:
                try:
                    app.xae.attach(_internal=True)      # the bound solution first, as every other xae_* call (_need)
                except XaeError as exc:
                    return {"attached": False, "reason": str(exc)}
            return app.xae.status()
        held = app.xae_held
        if held is not None and not held["future"].done():
            # N85: no COM call while a dialog holds the shell — say what holds it
            return {"attached": app.xae.dte is not None, "held_by_dialog": held["dialog"], "held_job": held["job"],
                    "held_since_utc": held["since_utc"],
                    "note": "answer the dialog (xae_dialog_resolve); the held call's result then shows in xae_dialogs"}
        return await app.xae_call(_st, timeout=120)

    @mcp.tool(annotations=EDIT)
    async def xae_open_solution(path: str) -> dict[str, Any]:
        """Open a .sln in the attached shell."""
        await app.permit(None, OpClass.EDIT, "xae_open_solution", path)
        res = await app.confirmed("xae_open_solution", {"path": path}, OpClass.EDIT, app.xae_call(app.xae.open_solution, path, timeout=180))
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_build(clean: bool = False, rebuild: bool = False, save_first: bool = True,
                        check_all_objects: bool = False) -> dict[str, Any]:
        """Build the solution. save_first (default) runs File.SaveAll, saves whatever the shell still reports
        unsaved, and checks that every xae_pou_set / xae_pou_create edit of this session is in the source files —
        it refuses to build otherwise (an external reload would discard the edits). Returns success, error/warning
        items with file:line (VS error list, or parsed from the Build output pane) and the output
        tail. check_all_objects=true runs PLC 'Check all objects' instead (compiles unused POUs). After a build of 0
        errors, link_check names the GVLs nothing in the source references (TwinCAT does not link them: no symbol on
        the target) unless they carry {attribute 'linkalways'}."""
        await app.permit(None, OpClass.EDIT, "xae_build")
        saved = await app.save_verified("xae_build") if save_first else {"skipped": "save_first=false"}
        if check_all_objects:
            res = await app.xae_call(app.xae.check_all_objects, timeout=600)
            res["save"] = saved
            return res
        res = await app.xae_call(app.xae.build, clean, rebuild, timeout=cfg.xae.build_timeout_s + 60)
        res["save"] = saved
        if res.get("success") and app.proj.root is not None:
            # N92: a build of 0 errors says nothing about what is linked — a GVL nothing uses is not, and gets no symbol
            try:
                ul = await app.proj_call(app.proj.unreferenced_gvls)
            except ToolError as exc:
                res["link_check_error"] = f"not checked: {exc}"
            else:
                if ul["not_linked"] or ul["with_io_not_judged"]:
                    res["link_check"] = {**ul, "note": (
                        "nothing in the source names these GVLs: TwinCAT does not link an object nothing uses — the "
                        "build reports 0 errors and the target gets no symbol of them. Reference them or add "
                        "{attribute 'linkalways'} on the line above VAR_GLOBAL; after a download ads_dump / "
                        "proj_vs_runtime show what the target has. with_io_not_judged: GVLs with AT %I*/%Q* variables "
                        "(whether an I/O mapping keeps one linked is not checked)")}
                    if ul["not_linked"]:
                        res["link_warning"] = (f"{len(ul['not_linked'])} GVL(s) no code references will not be linked "
                                               "(no symbol on the target): "
                                               + ", ".join(x["gvl"] for x in ul["not_linked"][:5])
                                               + (" …" if len(ul["not_linked"]) > 5 else ""))
        app.audit("xae_build", {"clean": clean, "rebuild": rebuild}, OpClass.EDIT,
                  "ok" if res.get("success") else "build-failed", {"errors": res.get("errors")})
        return res

    @mcp.tool(annotations=READ)
    async def xae_diagnostics(tail_lines: int = 200) -> dict[str, Any]:
        """Current XAE Error List (errors/warnings/messages with file:line — TwinCAT's own entries too, read from the
        list view when the automation interface reports none; this focuses the Error List window) merged with the
        Build output pane, plus the pane's tail, without rebuilding. `source` names where each part came from."""
        errs = await app.xae_call(app.xae.errors, True, timeout=60)
        out = await app.xae_call(app.xae.build_output, tail_lines, timeout=60)
        return {"errors": errs, "build_output": out}

    @mcp.tool(annotations=READ)
    async def xae_list_commands(contains: str = "PLC", limit: int = 200) -> dict[str, Any]:
        """List DTE command names containing a substring (to discover what this shell exposes)."""
        return await app.xae_call(app.xae.list_commands, contains, limit, timeout=120)

    @mcp.tool(annotations=DESTR)
    async def xae_command(name: str, args: str = "", confirm_token: str | None = None,
                          ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Execute an arbitrary DTE command (e.g. 'PLC.Login'). Control-class; commands whose name
        contains download/reset/activate/restart/delete/remove/shutdown are destructive-class."""
        op = OpClass.DESTRUCTIVE if re.search(r"download|reset|activate|restart|delete|remove|shutdown|clean", name, re.I) else OpClass.CONTROL
        tgt = await app.guarded_target("xae_command", {"name": name}, op, confirm_token)
        await app.permit(ctx, op, "xae_command", tgt, confirm_token)
        res = await app.confirmed("xae_command", {"name": name, "args": args}, op, app.xae_call(app.xae.command, name, args, timeout=120))
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_plc(action: str, plc_project_path: str | None = None, confirm_token: str | None = None,
                      ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """PLC online actions: login | logout | start | stop | reset_cold | reset_origin | download. First the
        Automation Interface (ConsumeXml online commands on '<Name> Project' — no active project needed;
        plc_project_path when there are several PLC projects), then the shell's DTE commands (names vary by build).
        download/reset_cold/reset_origin are destructive-class, the rest control-class."""
        app.refuse_if_dialog(f"xae_plc {action}")
        op = OpClass.DESTRUCTIVE if action in ("download", "reset_origin", "reset_cold") else OpClass.CONTROL
        tgt = await app.guarded_target("xae_plc", {"action": action, "plc_project_path": plc_project_path}, op, confirm_token)
        await app.permit(ctx, op, "xae_plc", tgt, confirm_token)
        res = await app.confirmed("xae_plc", {"action": action, "target": tgt}, op, app.xae_call(app.xae.plc_action, action, plc_project_path, timeout=300))
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_target(net_id: str | None = None, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Get (net_id=None) or set the target AMS NetId of the TwinCAT project. Setting is
        control-class."""
        if net_id is None:
            return await app.xae_call(app.xae.target_get, timeout=60)
        await app.permit(ctx, OpClass.CONTROL, "xae_target", net_id, confirm_token)
        res = await app.confirmed("xae_target", {"net_id": net_id}, OpClass.CONTROL, app.xae_call(app.xae.target_set, net_id, timeout=60))
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_activate_configuration(confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """ActivateConfiguration on the current target (overwrites the boot project on the IPC).
        Destructive-class. Follow with xae_restart_twincat."""
        app.refuse_if_dialog("xae_activate_configuration")
        tgt = await app.xae_target_for("xae_activate_configuration", OpClass.DESTRUCTIVE, confirm_token)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_activate_configuration", tgt, confirm_token)
        before = await app.restart_witness()     # the application running before: compared after the restart
        res = await app.xae_call(app.xae.activate_configuration, timeout=600)
        app.last_activation = {"witness": before, "mono": time.monotonic()}
        res["plc_application_before"] = _app_ts_text(before.get("app_timestamp")) or before.get("app_timestamp")
        await app.ads_call(app.ads.disconnect, timeout=10)
        ver = await app.verify_target_after("xae_activate_configuration", expect_run=False, timeout_s=30)
        res.update({"target_mode": ver["target_mode"], "dialogs": ver["dialogs"]})
        if ver.get("hint"):
            res["hint"] = ver["hint"]
        app.audit("xae_activate_configuration", {"target": tgt}, OpClass.DESTRUCTIVE, "ok" if ver["verified"] else "dialog-after-activate", res)
        if ver["dialogs"]:
            res["activated"] = False
            raise ToolError("ActivateConfiguration was accepted but the shell shows an error dialog: "
                            f"{ver['dialogs'][0].get('title')}: {ver['dialogs'][0].get('text', '')[:300]}"
                            + (f". {ver['hint']}" if ver.get("hint") else ""))
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_restart_twincat(confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """StartRestartTwinCAT on the current target (runtime restarts in RUN mode). Destructive-class."""
        app.refuse_if_dialog("xae_restart_twincat")
        tgt = await app.xae_target_for("xae_restart_twincat", OpClass.DESTRUCTIVE, confirm_token)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_restart_twincat", tgt, confirm_token)
        before = await app.restart_witness()                  # R40: what a restart must change, read before it
        t0 = time.monotonic()
        res = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        ver = await app.verify_target_after("xae_restart_twincat", expect_run=True, require_transition=True, before=before)
        # N5: the shell returns as soon as the command is accepted; the restart is over when the target reports RUN
        res.update({"restarted": bool(ver["verified"]), "target_mode": ver["target_mode"], "dialogs": ver["dialogs"],
                    "verification_polls": ver["polls"], "modes_seen": ver["modes_seen"], **_restart_timing(t0, ver)})
        res.update({k: ver[k] for k in _TRANSITION_KEYS if k in ver})
        if ver.get("hint"):
            res["hint"] = ver["hint"]
        if ver["verified"]:
            # the application that ran before: from before the activation that preceded this restart (within 30 min)
            # when that witness read one, else from before this restart. N93: an activation made in CONFIG has no
            # compile time; it hid the one read right before a RUN -> RUN restart ('nothing to compare')
            act = app.last_activation if app.last_activation and time.monotonic() - app.last_activation["mono"] < 1800 else None
            app.last_activation = None
            after = ver.get("witness_after") or await app.restart_witness("RUN")
            base, base_is = before, "before this restart"
            if act and (act["witness"] or {}).get("app_timestamp") is not None:
                base, base_is = act["witness"], "before the activation that preceded this restart"
            res["plc_application"] = {**plc_application(base, after), "compared_with": base_is}
        app.audit("xae_restart_twincat", {"target": tgt}, OpClass.DESTRUCTIVE,
                  "ok" if ver["verified"] else _restart_outcome(ver), res)
        if not ver["verified"]:
            raise ToolError("StartRestartTwinCAT was accepted, but " + _restart_failure(ver))
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_deploy(skip_build: bool = False, plc_project_path: str | None = None,
                         allow_target_mismatch: bool = False, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Save → build → (boot project autostart on) → activate → restart, in one call.
        Stops at the first build error. Destructive-class (one confirmation covers all steps).
        Refuses when the XAE target and the ADS target of this server differ.
        Done = the target runs the new configuration: in RUN and a restart proven (steps.verify.transition_by):
        'poll' (another state seen after the commands), 'mode_change' (it was not in RUN before the activation),
        'witness' (task 1's cycle counter went back, or the PLC application's compile timestamp changed). In RUN
        without such proof is reported as that, not as 'not in RUN'."""
        app.refuse_if_dialog("xae_deploy")
        tgt = await app.xae_target_for("xae_deploy", OpClass.DESTRUCTIVE, confirm_token)
        await app.check_targets(tgt, allow_target_mismatch)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_deploy", tgt, confirm_token)
        steps: dict[str, Any] = {}

        async def halt_if_dialog(step: str) -> None:
            # review of N87: a dialog a step left open (the build's, the boot project's) stops the deploy before the
            # next step — it was checked only at the start, and the activation ran with the dialog waiting. The
            # watchdog's rules answer theirs within 0.5 s: 2 s are given, as for a call (xae_call)
            t_end = time.monotonic() + 2.0
            while any(_dialog_awaits(d) for d in app.dialogs_now()) and time.monotonic() < t_end:
                await asyncio.sleep(0.25)
            try:
                app.refuse_if_dialog(f"xae_deploy before {step}",
                                     f"Done: {', '.join(steps) or 'nothing'}; nothing after that was started.")
            except ToolError:
                app.audit("xae_deploy", {"target": tgt}, OpClass.DESTRUCTIVE, "dialog-open", steps)
                raise
        steps["save"] = await app.save_verified("xae_deploy")
        if not skip_build:
            await halt_if_dialog("build")
            b = await app.xae_call(app.xae.build, False, False, timeout=cfg.xae.build_timeout_s + 60)
            steps["build"] = {k: b[k] for k in ("success", "errors", "warnings", "duration_s", "open_dialogs",
                                                "xae_messages") if k in b}
            if not b["success"]:
                steps["build"]["items"] = b["items"][:50]
                app.audit("xae_deploy", {"target": tgt}, OpClass.DESTRUCTIVE, "build-failed", steps)
                return {"deployed": False, "steps": steps}
        if plc_project_path:
            await halt_if_dialog("boot project")
            steps["boot_project"] = await app.xae_call(app.xae.boot_project, plc_project_path, True, True, False, timeout=300)
        await halt_if_dialog("activation")
        before = await app.restart_witness()                  # R40: before the activation — what a restart must change
        steps["activate"] = await app.xae_call(app.xae.activate_configuration, timeout=600)
        await halt_if_dialog("restart")
        t_restart = time.monotonic()
        steps["restart"] = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        # P17: a deploy is done when the target runs it, not when the shell accepted the commands
        ver = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before)
        # N5: the restart lasted from its command until the target was seen running again (command_s: the shell part)
        steps["restart"].update(_restart_timing(t_restart, ver))
        steps["verify"] = {"running": bool(ver["verified"]), "target_mode": ver["target_mode"], "dialogs": ver["dialogs"],
                           "polls": ver["polls"], "modes_seen": ver["modes_seen"]}
        steps["verify"].update({k: ver[k] for k in _TRANSITION_KEYS if k in ver})
        if ver.get("hint"):
            steps["verify"]["hint"] = ver["hint"]
        ok = bool(ver["verified"])
        if ok:
            app.last_activation = None
            steps["verify"]["plc_application"] = plc_application(before, ver.get("witness_after")
                                                                 or await app.restart_witness("RUN"))
        app.audit("xae_deploy", {"target": tgt}, OpClass.DESTRUCTIVE, "ok" if ok else _restart_outcome(ver), steps)
        if not ok:
            raise ToolError("xae_deploy: activated and restarted, but " + _restart_failure(ver))
        return {"deployed": True, "target": tgt, "steps": steps}

    # ---- tree ------------------------------------------------------------
    @mcp.tool(annotations=READ)
    async def xae_tree_get(path: str, depth: int = 0, xml: bool = False, max_items: int = 2000) -> dict[str, Any]:
        """System Manager tree item: properties, children to `depth`, and ProduceXml when xml=true.
        Path syntax 'TIPC^Plc^Plc Project^POUs^MAIN' (xae_plc_projects gives the '<Name> Project' path;
        its children POUs/DUTs/GVLs are walked even though the node itself reports no ChildCount).
        Roots (ITcSysManager::LookupTreeItem shortcuts): TIIC (I/O Configuration), TIID (its I/O Devices), TIRC (Real-Time
        Configuration), TIRT (its Additional Tasks), TIRS (its Real-Time Settings), TIRR (its Route Settings), TIPC (PLC
        Configuration), TINC (NC Configuration)."""
        out = await app.xae_call(app.xae.tree_item, path, timeout=60)
        if depth > 0:
            out["children"] = (await app.xae_call(app.xae.tree_children, path, min(depth, 6), max_items, timeout=300))["items"]
        if xml:
            out["xml"] = (await app.xae_call(app.xae.tree_xml, path, timeout=120))["xml"]
        return out

    @mcp.tool(annotations=EDIT)
    async def xae_tree_consume_xml(path: str, xml: str) -> dict[str, Any]:
        """ConsumeXml on a tree item — the generic write path for any configuration
        (add I/O boxes, link variables, set task cycle time, add project routes on TIRR …)."""
        app.guard_safety_path(path, "xae_tree_consume_xml")
        await app.permit(None, OpClass.EDIT, "xae_tree_consume_xml", path)
        res = await app.confirmed("xae_tree_consume_xml", {"path": path, "xml_bytes": len(xml)}, OpClass.EDIT, app.xae_call(app.xae.tree_consume_xml, path, xml, timeout=300))
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_tree_set_values(path: str, assignments: dict[str, str]) -> dict[str, Any]:
        """Set element texts in a tree item's XML by relative XPath, e.g.
        {'TaskDef/CycleTime': '10000'} (unit 100 ns)."""
        app.guard_safety_path(path, "xae_tree_set_values")
        await app.permit(None, OpClass.EDIT, "xae_tree_set_values", path)
        res = await app.confirmed("xae_tree_set_values", {"path": path, "assignments": assignments}, OpClass.EDIT, app.xae_call(app.xae.tree_set_values, path, assignments, timeout=300))
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_tree_create_child(parent_path: str, name: str, subtype: int, before: str = "",
                                    info: Any = None) -> dict[str, Any]:
        """Generic CreateChild (tree item types/subtypes per Infosys). For PLC objects prefer
        xae_pou_create."""
        app.guard_safety_path(parent_path, "xae_tree_create_child")
        await app.permit(None, OpClass.EDIT, "xae_tree_create_child", f"{parent_path}^{name}")
        res = await app.confirmed("xae_tree_create_child", {"parent": parent_path, "name": name, "subtype": subtype}, OpClass.EDIT, app.xae_call(app.xae.tree_create_child, parent_path, name, subtype, before, info, timeout=120))
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_tree_delete(parent_path: str, name: str, confirm_token: str | None = None,
                              ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Delete a tree item (POU, box, task …). Destructive-class; the confirmation names the item:
        tc_request_confirmation(action='xae_tree_delete', target='<parent_path>^<name>')."""
        tgt = f"{parent_path}^{name}"
        app.guard_safety_path(parent_path, "xae_tree_delete")
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_tree_delete", tgt, confirm_token)
        res = await app.confirmed("xae_tree_delete", {"target": tgt}, OpClass.DESTRUCTIVE, app.xae_call(app.xae.tree_delete_child, parent_path, name, timeout=120))
        gone = app.drop_edits_under(tgt)         # N124: an edit of a deleted object can never reach a source file
        if gone and isinstance(res, dict):
            res = {**res, "pending_edits_dropped": gone}
        return res

    # ---- PLC objects -----------------------------------------------------
    @mcp.tool(annotations=READ)
    async def xae_plc_projects() -> dict[str, Any]:
        """PLC projects in the TwinCAT project with their tree paths (needed by PLCopen / boot tools)."""
        return await app.xae_call(app.xae.plc_projects, timeout=120)

    @mcp.tool(annotations=READ)
    async def xae_pou_get(path: str, include_subitems: bool = True) -> dict[str, Any]:
        """Declaration/implementation text of a POU/DUT/GVL/interface tree item and its
        methods/properties/actions."""
        return await app.xae_call(app.xae.pou_get, path, include_subitems, timeout=120)

    @mcp.tool(annotations=EDIT)
    async def xae_pou_set(path: str, declaration: str | None = None, implementation: str | None = None) -> dict[str, Any]:
        """Replace declaration and/or implementation text of a POU item inside the live shell
        (no file round-trip, XAE keeps LineIds consistent). Build afterwards."""
        app.guard_safety_path(path, "xae_pou_set")
        await app.permit(None, OpClass.EDIT, "xae_pou_set", path)
        res = await app.confirmed("xae_pou_set", {"path": path, "decl": declaration is not None, "impl": implementation is not None}, OpClass.EDIT, app.xae_call(app.xae.pou_set, path, declaration, implementation, timeout=120))
        app.note_edit(path, declaration, implementation)
        res["saved"] = False
        res["note"] = "in the shell only — xae_build / xae_deploy / tcunit_run save and check it on disk first"
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_pou_create(parent_path: str, name: str, kind: str, language: str = "st",
                             return_type: str | None = None, extends: str | None = None,
                             implements: str | None = None, access: str | None = None,
                             declaration: str | None = None, implementation: str | None = None,
                             accessors: str = "get,set") -> dict[str, Any]:
        """Create a PLC object under a folder/POU. kind = program | fb | function | method | property | action |
        transition | struct | enum | union | alias | gvl | interface | itf_method | itf_property | folder |
        parameter_list, and accessors on their own: property_get | property_set (under a property) | itf_property_get |
        itf_property_set (under an interface property). A property / itf_property is created with its accessors
        (accessors='get,set' default, 'get' or 'set'): without one it does not compile (C0373). return_type: function,
        method, property; access: PUBLIC | PRIVATE | PROTECTED | INTERNAL. Optional code is set after creation."""
        app.guard_safety_path(parent_path, "xae_pou_create")
        await app.permit(None, OpClass.EDIT, "xae_pou_create", f"{parent_path}^{name}")
        res = await app.xae_call(app.xae.pou_create, parent_path, name, kind, language, return_type,
                                 extends, implements, access, declaration, implementation, accessors, timeout=120)
        if kind.lower() != "folder":
            app.note_edit(f"{parent_path}^{name}", declaration, implementation)
        app.audit("xae_pou_create", {"parent": parent_path, "name": name, "kind": kind}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_plcopen_import(plc_project_path: str, xml_file: str, option: str = "none") -> dict[str, Any]:
        """Import a PLCopen XML file into a PLC project (path from xae_plc_projects).
        option: none | rename | replace | skip (conflict handling).
        replace: the existing object keeps its place (folder) and identity and gets the imported text. TwinCAT's own
        replace deletes it and re-creates the import at the project root, so the file is imported as renamed copies
        (PRG_X_1, header renamed too) and the tool finishes the replace: the copy's header is named back, the texts are
        checked against the XML, written into the existing object, read back, and the declared name must equal the
        object's name. Only then is the copy — created by this import, it was not in the tree before — deleted
        (nothing that existed before the import is ever deleted). Anything not proven → nothing written, the copy kept
        and named in the error. An object that is not at its place after an import is named (removed / moved).
        Result: replaced, header_renamed, copies_removed, build_required + objects_to_build (build next)."""
        app.guard_safety_path(plc_project_path, "xae_plcopen_import")
        await app.permit(None, OpClass.EDIT, "xae_plcopen_import", xml_file)
        res = await app.confirmed("xae_plcopen_import", {"project": plc_project_path, "file": xml_file, "option": option}, OpClass.EDIT, app.xae_call(app.xae.plcopen_import, plc_project_path, xml_file, option, timeout=300))
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_plcopen_export(plc_project_path: str, items: list[str], xml_file: str) -> dict[str, Any]:
        """Export PLC objects ('POUs.FB_X', 'DUTs.ST_Y' — folder.object) to a PLCopen XML file."""
        await app.permit(None, OpClass.EDIT, "xae_plcopen_export", xml_file)
        return await app.xae_call(app.xae.plcopen_export, plc_project_path, items, xml_file, timeout=300)

    @mcp.tool(annotations=EDIT)
    async def xae_boot_project(plc_project_path: str, autostart: bool | None = None, generate: bool = False,
                               activate_after_generate: bool = False, confirm_token: str | None = None,
                               ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Boot project settings: set Autostart and/or GenerateBootProject (local, no activate
        unless activate_after_generate=true, which is then destructive-class)."""
        op = OpClass.DESTRUCTIVE if activate_after_generate else OpClass.EDIT
        if activate_after_generate:
            app.refuse_if_dialog("xae_boot_project (activate_after_generate)")
        app.guard_safety_path(plc_project_path, "xae_boot_project")
        tgt = (await app.guarded_target("xae_boot_project", {"plc_project_path": plc_project_path,
                                                             "activate_after_generate": True}, op, confirm_token)
               if activate_after_generate else plc_project_path)
        await app.permit(ctx, op, "xae_boot_project", tgt, confirm_token)
        res = await app.confirmed("xae_boot_project", {"project": plc_project_path, "autostart": autostart, "generate": generate}, op, app.xae_call(app.xae.boot_project, plc_project_path, autostart, generate, activate_after_generate, timeout=300))
        return res

    # ======================================================================
    # proj_* offline files
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def proj_scan(root: str | None = None, incremental: bool = False) -> dict[str, Any]:
        """Index the project tree (.sln/.tsproj/.plcproj/.TcPOU/.TcDUT/.TcGVL/.TcIO).
        incremental=true re-indexes only files whose mtime changed (fast after XAE saves)."""
        if incremental and root is None:
            return await app.proj_call(app.proj.refresh)
        return await app.proj_call(app.proj.scan, root)

    @mcp.tool(annotations=READ)
    async def proj_changed(since: str = "HEAD") -> dict[str, Any]:
        """Units whose source differs from a git revision (default: uncommitted work) plus
        untracked files — 'what did I change?'; pair with proj_vs_runtime for 'is it deployed?'."""
        return await app.proj_call(app.proj.changed_units, since)

    @mcp.tool(annotations=READ)
    async def proj_list(kind: str | None = None, pattern: str | None = None, limit: int = 500) -> dict[str, Any]:
        """List indexed objects. kind: PROGRAM | FUNCTION_BLOCK | FUNCTION | STRUCT | ENUM |
        UNION | ALIAS | GVL | INTERFACE | METHOD | PROPERTY | ACTION (case-insensitive)."""
        return await app.proj_call(app.proj.list_units, kind, pattern, limit)

    @mcp.tool(annotations=READ)
    async def proj_get_code(name: str, part: str = "both", with_line_numbers: bool = True) -> dict[str, Any]:
        """Source of an object ('FB_Valve', 'FB_Valve.Open', 'MAIN'): declaration/implementation
        with file line numbers, parsed VAR declarations, sub-items."""
        return await app.proj_call(app.proj.get_code, name, part, with_line_numbers)

    @mcp.tool(annotations=READ)
    async def proj_resolve_symbol(symbol: str) -> dict[str, Any]:
        """Map an ADS symbol path (MAIN.fbFill.fbValve.bOpen) to the file:line where each hop is
        declared, following variable types through FB/STRUCT definitions. Hops inside a library type
        (Tc2_Standard.CTU.CV) are completed from the runtime's datatype table when the target is reachable. A first
        name declared in several PLC projects (MAIN in Plc1 and Plc2) is the one of the PLC project on the connected
        ADS port (AmsPort in the .tsproj)."""
        res = await app.proj_call(app.proj.resolve_symbol, symbol, app.ads.port)
        if res.get("resolved_to_library_type"):
            try:
                info = (await app.ads_call(app.ads.symbol_info, symbol, timeout=30)).to_dict()
                res["runtime"] = {k: info.get(k) for k in ("name", "type", "size", "comment", "index_group", "index_offset") if k in info}
                res["resolved"] = True
            except ToolError as exc:
                res["runtime"] = {"error": str(exc)[:200]}
        return res

    @mcp.tool(annotations=READ)
    async def proj_search(regex: str, kinds: list[str] | None = None, max_hits: int = 200,
                          ignore_case: bool = True) -> dict[str, Any]:
        """Regex search over all declarations/implementations → unit, file, line, text."""
        return await app.proj_call(app.proj.search, regex, kinds, max_hits, ignore_case)

    @mcp.tool(annotations=READ)
    async def proj_references(name: str, max_hits: int = 200) -> dict[str, Any]:
        """Where a type/object is instantiated (variables of that type) and mentioned."""
        return await app.proj_call(app.proj.references, name, max_hits)

    @mcp.tool(annotations=READ)
    async def proj_read_file(path: str, max_chars: int = 200000) -> dict[str, Any]:
        """Raw text of a project file (relative to project root or absolute)."""
        return await app.proj_call(app.proj.read_file, path, max_chars)

    @mcp.tool(annotations=EDIT)
    async def proj_set_code(name: str, declaration: str | None = None, implementation: str | None = None,
                            strip_lineids: bool = True, regenerate_lineids: bool = False) -> dict[str, Any]:
        """Write declaration/implementation back into the .TcPOU/.TcDUT/.TcGVL file (keeps the
        rest of the XML byte-identical, writes a .bak, drops the object's stale <LineIds>).
        Use when XAE is closed or for batch edits; otherwise prefer xae_pou_set."""
        await app.permit(None, OpClass.EDIT, "proj_set_code", name)
        res = await app.confirmed("proj_set_code", {"name": name}, OpClass.EDIT, app.proj_call(app.proj.set_code, name, declaration, implementation, strip_lineids, regenerate_lineids))
        return res

    # ======================================================================
    # tcunit_*
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def tcunit_results(remote_path: str | None = None, local_path: str | None = None,
                             xml_text: str | None = None) -> dict[str, Any]:
        """Parse a TcUnit xUnit result into a pass/fail summary. Source: the file on the target
        over ADS (default: configured remote path), a local file, or raw XML text."""
        if local_path:
            txt = (await app.proj_call(app.proj.read_file, local_path))["text"]
            src = local_path
        elif xml_text:
            txt, src = xml_text, "<text>"
        else:
            src = remote_path or cfg.project.tcunit_remote_result
            txt = (await app.ads_call(app.files.read_file, src, None, True, "utf-8", timeout=120))["text"]
        res = await app.proj_call(parse_tcunit_xml, txt)
        res["source"] = src
        return res

    @mcp.tool(annotations=DESTR)
    async def tcunit_run(plc_project_path: str, timeout_s: int = 600, skip_build: bool = False,
                         remote_result_path: str | None = None, allow_target_mismatch: bool = False,
                         disable_io: bool = False, save_first: bool = True, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Full TcUnit cycle: save (checked) → build → boot project autostart+generate → activate → restart →
        delete old result → wait for the xUnit file on the target → parse. Destructive-class
        (it activates a configuration on the target): tc_request_confirmation(action='tcunit_run', target=<NetId
        XAE targets>). plc_project_path from xae_plc_projects. TcUnit writes the result file only with its library
        parameter GVL_Param_TcUnit.xUnitEnablePublish = TRUE (default FALSE): the run is refused before anything
        is activated when the project source says FALSE, and stops waiting when the PLC reports FALSE (D40)."""
        app.refuse_if_dialog("tcunit_run")
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
        rp = remote_result_path or cfg.project.tcunit_remote_result
        if not rp.lower().endswith(".xml"):
            raise ToolError("remote_result_path must be the TcUnit xUnit .xml file")
        pub = await asyncio.to_thread(_tcunit_publish_in_source, app)
        if pub["value"] is False:
            raise ToolError("tcunit_run: TcUnit's xUnitEnablePublish is FALSE in " + pub["plcproj"] + " (TcUnit default) — "
                            "TcUnit would write no result file and the run would only wait. Set it in XAE: References → "
                            "TcUnit → Parameters → GVL_Param_TcUnit.xUnitEnablePublish = TRUE, save, then tcunit_run. "
                            "Nothing was built or activated.")
        await app.check_targets(tgt, allow_target_mismatch)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "tcunit_run", tgt, confirm_token)
        steps: dict[str, Any] = {"target": tgt, "result_file": rp, "xunit_publish_in_source": pub}
        if save_first:
            steps["save"] = await app.save_verified("tcunit_run")        # D44: tcunit_run did not save at all
        if not skip_build:
            b = await app.xae_call(app.xae.build, False, False, timeout=cfg.xae.build_timeout_s + 60)
            steps["build"] = {k: b[k] for k in ("success", "errors", "warnings", "xae_messages") if k in b}
            if not b["success"]:
                steps["build"]["items"] = b["items"][:50]
                app.audit("tcunit_run", {"target": tgt}, OpClass.DESTRUCTIVE, "build-failed", steps)
                return {"ran": False, "steps": steps}
        try:
            await app.ads_call(app.files.delete_file, rp, timeout=30)
            steps["old_result_deleted"] = True
        except ToolError:
            steps["old_result_deleted"] = False
        if disable_io:
            steps["io_disabled"] = await app.xae_call(app.xae.io_disable, True, None, timeout=120)
        steps["boot_project"] = await app.xae_call(app.xae.boot_project, plc_project_path, True, True, False, timeout=300)
        steps["activate"] = await app.xae_call(app.xae.activate_configuration, timeout=600)
        steps["restart"] = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        t0 = time.time()
        # D40: the PLC's own view of the parameter — FALSE means no file will ever come, do not wait timeout_s for it
        online = None
        for _ in range(10):
            try:
                online = (await app.ads_call(app.ads.read, "GVL_Param_TcUnit.xUnitEnablePublish", timeout=10)).get("value")
                break
            except ToolError:
                await asyncio.sleep(2.0)                       # the runtime may still be starting
        steps["xunit_publish_online"] = online
        if online is False:
            app.audit("tcunit_run", {"target": tgt}, OpClass.DESTRUCTIVE, "publish-disabled", steps)
            return {"ran": True, "result": None, "steps": steps,
                    "note": "GVL_Param_TcUnit.xUnitEnablePublish is FALSE on the PLC: TcUnit ran but writes no result file. "
                            "Set the library parameter to TRUE (References → TcUnit → Parameters), then tcunit_run again."}
        waited = await app.ads_call(app.files.wait_for_file, rp, timeout_s, 3.0, timeout=timeout_s + 30, slow=True)
        steps["wait"] = waited
        if not waited.get("exists"):
            app.audit("tcunit_run", {"target": tgt}, OpClass.DESTRUCTIVE, "timeout", steps)
            try:
                where = await app.ads_call(app.files.resolve_prefix_dir, "boot", timeout=30) if rp.lower().startswith("boot:") else None
            except ToolError:
                where = None
            return {"ran": True, "result": None, "steps": steps,
                    "note": f"result file {rp} never appeared" + (f" (boot: = {where} on this target)" if where else "") +
                            " — is PRG_TcUnitRunner (TcUnit.RUN()) called from a running task, and is "
                            "xUnitEnablePublish TRUE? (online: " + repr(online) + ")"}
        # TcUnit writes the file incrementally; give it a moment to finish
        await asyncio.sleep(2.0)
        f = await app.ads_call(app.files.read_file, rp, None, True, "utf-8", timeout=120)
        res = await app.proj_call(parse_tcunit_xml, f["text"])
        steps["elapsed_s"] = round(time.time() - t0, 1)
        app.audit("tcunit_run", {"target": tgt}, OpClass.DESTRUCTIVE, "ok", res["summary"])
        return {"ran": True, "result": res, "steps": steps}

    # ======================================================================
    # prompts (workflow recipes)
    # ======================================================================
    P = workflows.PROMPTS

    @mcp.prompt(name="debug_symbol", description=P["debug_symbol"][0])
    async def prompt_debug_symbol(symbol: str) -> str:
        return P["debug_symbol"][1].format(symbol=symbol)

    @mcp.prompt(name="fb_validation", description=P["fb_validation"][0])
    async def prompt_fb_validation(instance: str, criteria: str = "see spec") -> str:
        return P["fb_validation"][1].format(instance=instance, criteria=criteria)

    @mcp.prompt(name="deploy_safely", description=P["deploy_safely"][0])
    async def prompt_deploy_safely(change: str = "current working tree") -> str:
        return P["deploy_safely"][1].format(change=change)

    @mcp.prompt(name="performance_audit", description=P["performance_audit"][0])
    async def prompt_performance_audit(scope: str = "MAIN") -> str:
        return P["performance_audit"][1].format(scope=scope)

    @mcp.prompt(name="onboard_project", description=P["onboard_project"][0])
    async def prompt_onboard_project() -> str:
        return P["onboard_project"][1]

    # ======================================================================
    # resources
    # ======================================================================
    @mcp.resource("twincat://config", mime_type="application/json")
    def res_config() -> str:
        return json.dumps(cfg.to_dict(), indent=2, default=str)

    @mcp.resource("twincat://audit/recent", mime_type="application/json")
    def res_audit() -> str:
        return json.dumps(app.guard.tail_audit(100), indent=2, default=str)

    @mcp.resource("twincat://safety", mime_type="application/json")
    def res_safety() -> str:
        return json.dumps(app.guard.status(), indent=2, default=str)

    import atexit
    atexit.register(app.rec.stop_all)
    atexit.register(app.stop_experiments)   # registered after → runs first: restore factors, then close recordings
    mcp._tcmcp_app = app  # type: ignore[attr-defined]  (tests)
    _describe_docs_tools(mcp, cfg.docs.source)
    _apply_tool_groups(mcp, cfg)
    return mcp
