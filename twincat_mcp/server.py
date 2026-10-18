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
elicitation, the human is asked directly (``ctx.elicit``); otherwise the
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
from typing import Any

from pydantic import BaseModel, Field

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
from .xae_backend import XaeBackend, XaeError, sta_init, sta_teardown
from .project_backend import ProjectIndex, ProjectError, parse_tcunit_xml
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
to the twincat-docs skill of this plugin (offline: <kb>\\helpviewer\\infosys_en.db + <kb>\\manualsdb\\manuals.db;
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


class ConfirmSchema(BaseModel):
    confirm: bool = Field(description="Set true to allow this operation on the target")


class AlsoScopeSchema(BaseModel):
    also_scope: bool = Field(description="Also record with the TwinCAT Scope configuration open in XAE")


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
}
TOKEN_TARGET_ARGS = {"xae_target": "net_id", "ads_plc_state": "action", "ads_system_mode": "action",
                     "ads_connect": "ams_net_id", "ads_add_route_to_plc": "plc_ip", "ipc_write_file": "path",
                     "ipc_delete_file": "path", "ipc_start_process": "path", "ipc_eventlog": "log",
                     "ipc_reboot": "", "rec_delete": "id", "tc_set_mode": "mode", "exp_run": "name"}


def _plc_iec_path(path: str) -> str:
    from .xae_backend import _plc_split
    return _plc_split(path)[1]


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
        self.proj = ProjectIndex(cfg.project)
        # D44: code set in the shell (xae_pou_set / xae_pou_create) until it is proven on disk
        self.pending_edits: dict[str, dict[str, Any]] = {}
        self.ads.write_guard = self.constant_reason       # every write path: CONSTANT / no-source checks
        self.ads.interface_hint = self.is_source_interface
        from .config import _home
        self.kb = K.Knowledge(_home(), cfg.knowledge.location, cfg.knowledge.esi_dirs, cfg.knowledge.library_dirs)
        # the builder stubs in <kb>\tools run this plugin's builders: record where they are, turn old copies into stubs
        threading.Thread(target=self._builder_upkeep, name="tcmcp-builders", daemon=True).start()
        self.rec = Recorder(self.ads, Path(cfg.safety.audit_log).parent / "recordings")
        self.scope_runs: dict[str, dict[str, Any]] = {}     # recording id → Scope recording started with it
        self.rec.rcfg = cfg.recorder

        # N62: the recorder compares cycle_ms with the task that runs each recorded program (project task config)
        def _task_of(name: str) -> list[dict[str, Any]] | None:
            return self.proj.tasks_of(name)
        self.rec.task_of = _task_of
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
        if cfg.docs.enabled:
            try:
                local = None
                cache = cfg.docs.cache_dir
                if cfg.docs.source == "local":
                    from . import helpviewer as HV
                    pk = HV.find_packages(cfg.docs.mshc)
                    if not pk:
                        where = cfg.docs.mshc or " / ".join(str(r) for r in HV.catalog_roots())
                        raise RuntimeError(
                            f"docs.source = 'local' but no Beckhoff Infosys Help Viewer package (BKINFOSYS3_*.mshc) was "
                            f"found under {where}. Install the TwinCAT documentation for the Help Viewer, set "
                            "[docs] mshc = '<path to the .mshc>', or docs.source = 'web' to use infosys.beckhoff.com.")
                    local = HV.HelpViewerSource(pk[0])
                    # the full package index (≈ 50k pages) belongs next to the knowledge base when one is chosen
                    kb_dir = self.kb.location()[0]
                    base = Path(cache) if (cfg.docs_cache_explicit or kb_dir is None) else kb_dir / "infosys"
                    cache = str(base / f"helpviewer-{local.lcid}")
                self.docs = InfosysIndex(cache, books=cfg.docs.books, exclude_books=cfg.docs.exclude_books,
                                         tc3_only=cfg.docs.tc3_only, max_workers=cfg.docs.max_workers,
                                         max_index_pages=cfg.docs.max_index_pages, sitemap_ttl_days=cfg.docs.sitemap_ttl_days,
                                         web_fallback=cfg.docs.web_fallback, fetch_timeout_s=cfg.docs.fetch_timeout_s,
                                         local=local)
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
        try:
            r = self.docs.warm()
            self.docs_warm = {"state": "warm", **r}
        except Exception as exc:  # noqa: BLE001
            self.docs_warm = {"state": "failed", "error": str(exc)[:160]}

    def _builder_upkeep(self) -> None:
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

    def _dialog_hint(self) -> str:
        """What is open in the shell right now — the usual reason a COM call is rejected or hangs."""
        try:
            from . import dialogs as _dlg
            live = [d.to_dict() for d in _dlg.list_dialogs(self.xae.pid)]
        except Exception:  # noqa: BLE001
            live = []
        pend = self.xae.watchdog.status().get("pending") or []
        seen = {d.get("hwnd") for d in live}
        live += [d for d in pend if d.get("hwnd") not in seen]
        if not live:
            return " No dialog is visible in the shell (xae_dialogs); the shell may be mid-build or mid-login — retry in a few seconds."
        return " Open dialog(s): " + "; ".join(f"'{d.get('title')}' [{', '.join(d.get('buttons') or [])}] {str(d.get('text', ''))[:160]}"
                                            for d in live[:3]) + " — answer it with xae_dialog_resolve(button=...)."

    async def xae_call(self, fn, *a, timeout: float | None = None, **kw) -> Any:
        try:
            return await self.xae_worker.run(fn, *a, timeout=timeout or self.cfg.xae.build_timeout_s + 60, **kw)
        except XaeError as exc:
            msg = str(exc)
            low = msg.lower()
            if "blocked by dialog" in low:
                pass                                   # the backend already named the dialog
            elif ("0x80010001" in low or "0x8001010a" in low or "-2147418111" in low or "-2147417846" in low
                    or "call was rejected" in low or "retry later" in low):
                msg += " — XAE refused the COM call: it is busy or a modal dialog has the shell." + await asyncio.to_thread(self._dialog_hint)
            lic = _license_hint(msg)
            if lic:
                msg += " " + lic
            raise ToolError(K.annotate(msg)) from exc
        except WorkerBusy as exc:
            raise ToolError(f"{exc} — the XAE call was NOT started; the shell is still busy with the earlier call "
                            "(a build, an activation or a modal dialog — xae_dialogs).") from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            pend = self.xae.watchdog.status().get("pending") or []
            hint = (" Open dialog(s): " + "; ".join(f"'{d['title']}' buttons {d['buttons']}" for d in pend[:3])
                    + " — xae_dialog_resolve(button=...)") if pend else " (xae_dialogs shows open prompts)"
            raise ToolError("XAE call timed out; a modal dialog in the shell is the usual cause." + hint) from exc

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
            self._start_local_index_bg()
            return
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

    def _start_local_index_bg(self) -> None:
        """Local Help Viewer package: the default books first (what most lookups need), then every other TwinCAT 3
        book, in a daemon thread. A package already indexed at the same stamp is skipped outright."""
        docs = self.docs
        assert docs is not None and docs.local is not None
        try:
            if docs.index_complete():          # same package content, same indexer version: nothing to do
                return
        except Exception:  # noqa: BLE001
            return
        self.docs_bg = {"started": time.time(), "finished": None, "books": ["all books of " + docs.local.path.name],
                        "done_books": 0, "total_books": None, "result": None}

        def _run() -> None:
            out: list[dict[str, Any]] = []
            try:
                for b in self.cfg.docs.books:
                    try:
                        r = docs.index_book(b)
                        out.append({k: r.get(k) for k in ("book", "fetched", "seconds")})
                    except Exception as exc:  # noqa: BLE001
                        out.append({"book": b, "error": str(exc)})

                def prog(book: str, i: int, n: int) -> None:
                    self.docs_bg["done_books"], self.docs_bg["total_books"] = i, n
                    self.docs_bg["current_book"] = book
                    self.docs_bg["current_book_pages"] = None
                    self.docs_bg["last_progress"] = time.time()

                def page_prog(book: str, d: int, t: int) -> None:
                    self.docs_bg["current_book_pages"] = f"{d}/{t}"
                    self.docs_bg["last_progress"] = time.time()
                out.append(docs.index_all(prog, page_prog))
            except Exception as exc:  # noqa: BLE001
                out.append({"error": f"{type(exc).__name__}: {exc}"})
            finally:
                self.docs_bg["result"] = out
                self.docs_bg["finished"] = time.time()

        threading.Thread(target=_run, name="infosys-helpviewer-index", daemon=True).start()

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
            st = self.docs_index_status()
            busy = st.get("started") and not st.get("finished")
            raise ToolError(f"Infosys index busy ({exc})" + (f" — {', '.join(st.get('books') or [])} being indexed in the background "
                            f"({st.get('running_for_s', 0):.0f} s so far); retry in a moment or search with web_fallback=true" if busy else "")) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ToolError(f"Infosys call exceeded {timeout:.0f} s" + (" (the package index is still being built — retry "
                            "in a minute)" if self.docs.local is not None else " (slow network? smaller max_pages?)")) from exc
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

    def scope(self) -> str:
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
                               "modes_seen": modes_seen, "verify_s": round(time.time() - t0, 1)}
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
        project, solution, [server] output_dirs) and must not silently replace an existing file."""
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
        return rp

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
        units = self.proj.units
        bad: list[dict[str, Any]] = []
        for path, e in self.pending_edits.items():
            segs = [x for x in path.split("^") if x]
            keys = ([f"{segs[-2]}.{segs[-1]}".lower()] if len(segs) >= 2 else []) + ([segs[-1].lower()] if segs else [])
            u = next((units[k] for k in keys if k in units), None)
            if u is None:
                bad.append({"path": path, "edited_at": e["at"], "why": "not in any source file under the project root"})
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
        out["edits_checked"] = len(self.pending_edits)
        if bad:
            out["edits_not_on_disk"] = bad
            raise ToolError(f"{tool}: File.SaveAll ran but {len(bad)} edit(s) made in the shell are not in the source files "
                            f"({'; '.join(b['path'] + ': ' + b['why'] for b in bad[:5])}). Nothing was built — an external "
                            "reload would discard them. Save in XAE (File > Save All) or xae_command('File.SaveAll') and "
                            f"retry. Still unsaved in the shell: {out.get('dirty_after') or 'none reported'}.")
        out["edits_on_disk"] = sorted(self.pending_edits)
        self.pending_edits.clear()
        return out

    def is_source_interface(self, type_name: str) -> bool:
        """The project declares `type_name` (qualified or not) as an INTERFACE."""
        if self.proj.root is None:
            return False
        try:
            self.proj._ensure()
        except Exception:  # noqa: BLE001
            return False
        base = self.proj._base_type_name(type_name or "").split(".")[-1].lower()
        u = self.proj.units.get(base)
        return bool(u is not None and u.kind == "INTERFACE")

    def constant_reason(self, name: str) -> str | None:
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
            r = self.proj.resolve_symbol(name)
        except Exception:  # noqa: BLE001 — unknown in source: the runtime check is all we have
            return None
        for h in r.get("hops", []):
            if h.get("constant"):
                return (f"{name!r}: {h['segment']} is declared in a VAR ... CONSTANT block ({h.get('declared_in')}, "
                        f"{Path(h.get('file') or '').name}:{h.get('line')}). The runtime accepts the write, but a constant changed "
                        "at runtime desynchronises array bounds and code compiled against it — change it in source and download")
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
                self.guard.check(op, action=action, target="", confirm_token=confirm_token, scope=self.scope())
            except SafetyError as exc:
                self.guard.audit(action, {"target": "", "scope": self.scope()}, op, "refused", str(exc))
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
                self.guard.check(op, action=action, target="", confirm_token=confirm_token, scope=self.scope())
            except SafetyError as exc:
                self.guard.audit(action, {"target": "", "scope": self.scope()}, op, "refused", str(exc))
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
                raise ToolError("xae_plc's token names the action: pass args={'action': 'login', 'plc_project_path': …}")
            path = a.get("plc_project_path")
            # N78: the token names a PLC project of the open solution — one that does not exist gets no token (and
            # the tool, which computes its target the same way, refuses it before anything is done)
            projs = await self.xae_call(self.xae._nested_projects, None, timeout=120)
            if path:
                want = _plc_iec_path(str(path))
                hit = [p for p in projs if p.lower() == want.lower()]
                if not hit:
                    raise ToolError(f"no PLC project {str(path)!r} in the open solution — its PLC projects: {projs} "
                                    "(xae_plc_projects lists them)")
                proj = hit[0]
            else:
                if len(projs) != 1:
                    raise ToolError(f"{len(projs)} PLC projects — xae_plc needs plc_project_path, and its token names it: "
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
                     confirm_token: str | None = None, mode_gate: bool = True) -> None:
        """Check the guard; for CONTROL/DESTRUCTIVE without a token, try to
        ask the human through elicitation before failing. ``mode_gate=False`` skips the
        readonly/guarded gating and only demands the confirmation — for tc_set_mode, which must be
        able to leave readonly mode with a human's consent."""
        scope = self.scope()
        if op is OpClass.EDIT and action in CODE_EDIT_ACTIONS and self.cfg.safety.edit_requires_config_mode:
            await self._require_config_mode(action, target)
        try:
            if mode_gate:
                self.guard.check(op, action=action, target=target, confirm_token=confirm_token, scope=scope)
            else:
                self.guard.consume_token(confirm_token, action, target, scope)
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
        kind = "DESTRUCTIVE" if op is OpClass.DESTRUCTIVE else "MACHINE CONTROL"
        prompt = (f"{kind}: {action}\n"
                  f"Target: {target or '(current target)'}\n"
                  f"Machine: {scope}   Mode: {self.guard.mode}\n"
                  f"Effect: {effect}\n"
                  f"Accept only if you intend this to happen now.")
        t_prompt = time.monotonic()
        try:
            res = await ctx.elicit(prompt, ConfirmSchema)
        except Exception as exc:  # noqa: BLE001 — client has no elicitation support
            self.guard.audit(action, {"target": target, "scope": scope}, op, "refused",
                             f"needs confirm_token; client has no elicitation ({type(exc).__name__})")
            raise ToolError(
                f"{action} on {target!r} needs confirmation and this client does not support "
                f"elicitation ({type(exc).__name__}). Call tc_request_confirmation(action={action!r}, "
                f"target={target!r}) and pass the confirm_token."
            ) from exc
        answered_s = round(time.monotonic() - t_prompt, 2)
        timing = {"prompt_open_s": answered_s}
        if answered_s < 1.5:
            timing["fast_answer"] = "answered in under 1.5 s — check that the operator saw and read the prompt"
        if res.action == "accept" and res.data is not None and res.data.confirm:
            self.guard.audit(action, {"target": target, "scope": scope}, op, "human-confirmed-elicitation", timing)
            return
        self.guard.audit(action, {"target": target, "scope": scope}, op, f"declined-elicitation:{res.action}", timing)
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
        except BaseException as exc:
            self.guard.audit(tool, args, op, "error", str(exc)[:400])
            raise
        self.guard.audit(tool, args, op, "ok", res)
        return res


async def _vs_runtime(app: "App", scope: str | None, max_items: int) -> dict[str, Any]:
    max_items = max(int(max_items), 1)
    proj = await app.proj_call(app.proj.variables, scope, 64, max_items, False)
    src_cut = bool(proj.get("truncated"))
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
    blind = online_cut | {u.lower() for u in unreadable}
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
        out["not_judged"] = {"missing_candidates": len(not_judged_missing), "extra_candidates": not_judged_extra,
                             "reason": "; ".join(why) + ". Raise max_items or narrow with scope= for a full verdict."}
    if len(missing_final) > cap or len(extra) > cap:
        out["truncated"] = f"lists capped at {cap}; narrow with scope="
    return out



# PLC code / project edits gated by safety.edit_requires_config_mode
CODE_EDIT_ACTIONS = frozenset({"xae_pou_set", "xae_pou_create", "proj_set_code", "xae_tree_consume_xml", "xae_tree_set_values",
                               "xae_tree_create_child", "xae_plcopen_import", "tcunit_scaffold", "xae_io_disable"})

# what the confirmation prompt says will happen
ACTION_EFFECTS = {
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
    line = f"\nOffline documentation, state at this connection ({ds.get('knowledge_folder', 'no folder')}): {ds['summary']}"
    if not ds.get("ok") and ds.get("infosys_command"):
        line += f" To rebuild: {ds['infosys_command']}."
    return INSTRUCTIONS + line + "\n"


def build_server(cfg: Config | None = None) -> MCPServer:
    cfg = cfg or load_config()
    app = App(cfg)
    mcp = MCPServer("twincat-mcp", instructions=connection_instructions(app), version=__version__)

    # ======================================================================
    # tc_* meta
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def tc_doctor(verbose: bool = False) -> dict[str, Any]:
        """Environment check: platform, pyads/router, local AMS NetId, XAE ProgIDs in the
        registry, running XAE shells, project root, safety mode. Run this first. Starts with a one-line `summary`;
        verbose=true adds the full loaded configuration."""
        rep: dict[str, Any] = {"version": __version__, "python": sys.version.split()[0],
                               "platform": platform.platform(), "windows": is_windows(),
                               "safety": app.guard.status()}
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
                rep["infosys"] = {"source": app.docs.source(), "cache": str(app.docs.dir), "fts5": app.docs.has_fts,
                                  "complete": await asyncio.to_thread(app.docs.index_complete) if app.docs.local is not None else None,
                                  "indexed_books": len(await asyncio.to_thread(app.docs.indexed_books))
                                  if app.docs.local is not None else await asyncio.to_thread(app.docs.indexed_books),
                                  "background_index": app.docs_index_status(), "warm": app.docs_warm,
                                  "default_books": cfg.docs.books}
            except ToolError as exc:
                rep["infosys"] = f"FAIL: {exc}"
        else:
            rep["infosys"] = f"FAIL: {app.docs_error}" if app.docs_error else "disabled"
        try:
            ds = await asyncio.to_thread(app.kb.docs_status)
            rep["offline_docs"] = ds
            # an index that exists but is not whole / current is worth a line; no folder or no index is not
            if ds.get("infosys_state") in ("outdated", "incomplete", "old_format") or ds.get("infosys_rebuild"):
                infos.append("offline docs: Infosys index " + ds["infosys"])
        except Exception as exc:  # noqa: BLE001
            rep["offline_docs"] = {"error": f"{type(exc).__name__}: {exc}"}
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
        """Knowledge base: where it is stored (or the question to ask the user if no folder was chosen yet), the
        project folders indexed for kb_search, ESI devices indexed for hw_lookup, known-error entries — and under
        `docs` the offline documentation in the same folder that the twincat-docs skill reads: helpviewer\\infosys_en.db
        (Infosys, built by tools\\infosys_local.py), manualsdb\\manuals.db (PDF manuals, tools\\manuals_db.py) with the
        PDFs not indexed yet, the SOPs, and the exact command that runs the skill's script on this folder."""
        res = await app.kb_call(app.kb.status)
        if app.docs is not None and isinstance(res.get("docs"), dict):
            idx = Path(app.docs.dir) / "index.sqlite"
            res["docs"]["infosys_tools_index"] = {
                "database": str(idx), "size_mb": round(idx.stat().st_size / 1e6, 1) if idx.exists() else 0,
                "note": "the infosys_* tools' own index of the same Help Viewer package (fallback); the twincat-docs "
                        "skill reads helpviewer\\infosys_en.db"}
        return res

    @mcp.tool(annotations=EDIT)
    async def kb_set_location(path: str | None = None, use_fallback: bool = False) -> dict[str, Any]:
        """Choose the folder for the knowledge database — ONLY with a path the user gave you (or use_fallback=true
        once the user agreed to D:\\TwinCAT-MCP\\knowledge). The folder is checked: local fixed drive (no network
        share / mapped drive / USB), not inside OneDrive/Dropbox/Google Drive/iCloud, not inside the plugin folder,
        writable, >= 1 GB free — a failing condition is named and nothing is changed. An existing database is never
        moved or deleted. Without arguments it returns the current location (and changes nothing)."""
        res = await app.kb_call(app.kb.set_location, path, use_fallback)
        app.audit("kb_set_location", {"path": path, "use_fallback": use_fallback}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def kb_add_project(path: str, label: str | None = None) -> dict[str, Any]:
        """Index a project folder for kb_search (cross-project pattern search) — ONLY a folder the user named. Reads
        .TcPOU/.TcDUT/.TcGVL/.TcIO beneath it into the knowledge database; nothing is indexed by default."""
        res = await app.kb_call(app.kb.add_project, path, label)
        app.audit("kb_add_project", {"path": path}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def kb_remove_project(path: str) -> dict[str, Any]:
        """Remove a project folder from the knowledge base (index entries only; the files are untouched)."""
        res = await app.kb_call(app.kb.remove_project, path)
        app.audit("kb_remove_project", {"path": path}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def kb_reindex(path: str | None = None) -> dict[str, Any]:
        """Re-index one project folder (or every changed one) after edits."""
        return await app.kb_call(app.kb.reindex, path)

    @mcp.tool(annotations=READ)
    async def kb_search(query: str, kinds: list[str] | None = None, project: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Search the code of the project folders the user added ("alarm handler", "FB_Valve", "CONCAT"): unit, kind,
        file and the matching declaration/code excerpts, best first. Units with EVERY word first, then every word as a
        prefix ('CounterMod' finds FB_CounterModule); only when none has them all, units with some words
        (match='some words', with the words each unit holds; words on no unit or on most units are not used and
        are listed). Stop words ('how', 'to', 'the') are not searched; a query of stop words only is refused. A copy
        name (PRG_X_9) that no unit has falls back to the name it was copied from. "quoted text" is a phrase. kinds
        (checked): PROGRAM, FUNCTION_BLOCK/FB, FUNCTION, METHOD, PROPERTY, ACTION, INTERFACE, STRUCT, ENUM, GVL, DUT
        (= every data type) …; project = the folder (any spelling) or its label; limit 1–200. An error message gives
        its known cause/fix (ADS errors 6, 7, 1793, 1796, 1797, 1799, 1808, 1861 in any notation — ADSError 1861,
        ADS error 1861, 0x745 —, license, XAE busy / dialog / disconnected, logged in, compiler: identifier not
        defined, cannot convert type, unknown type) and is searched only for the names it quotes.
        Beckhoff documentation is not here: the twincat-docs skill."""
        return await app.kb_call(app.kb.search, query, kinds, project, limit)

    @mcp.tool(annotations=READ)
    async def hw_lookup(query: str, limit: int = 3, revision: int | str | None = None,
                        detail: str = "newest") -> dict[str, Any]:
        """EtherCAT terminal/box data from the installed ESI files (K4): name, product code, revisions, PDOs with
        their entries (index, subindex, bit length, type), CoE objects. query = type ('EL2008'), part of a name, or a
        product code ('#x07D83052'). detail: 'newest' (default) = PDOs and CoE objects of the newest revision of each
        type (or of `revision`), other revisions as one line each; 'summary' = counts only; 'full' = everything for
        every revision. The first call indexes TwinCAT's ESI folder into the knowledge database (can take a minute).
        `manuals` names the indexed PDF manuals (knowledge folder, manuals_db.py) that mention the product, with
        pages — the twincat-docs skill reads them."""
        return await app.kb_call(app.kb.hw_lookup, query, limit, revision, detail, timeout=900)

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
        tok = app.guard.request_token(act, target, app.scope())
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
        (AppTimestamp, OnlineChangeCnt, tasks, TwinCAT build, symbol version). Conditions are levels: a start_when that
        already holds fires at once — edge_triggers=true fires only when it BECOMES true. A condition may name one
        element of an array recorded as a whole (GVL_Test.aCounter[1] when GVL_Test.aCounter is recorded).
        engine (cycle mode): 'native' = the C++ capture service (Beckhoff AdsLib on TcAdsDll: ADS callback → ring →
        file, no Python on the data path), 'python' = the in-process recorder, None = recorder.engine from the config
        (default 'auto': native when installed). The response says which engine ran and why. After stopping: rec_verify.
        also_scope: also run the TwinCAT Scope configuration open in XAE (scope = its name when several are open) —
        StartRecord now; at rec_stop StopRecord, SaveSVD and ExportCSV into <recordings>/scope/<id>.svdx / .scope.csv
        with a signed link file. What Scope records (channels, sample time) is set in that configuration. With
        recorder.also_scope = 'ask' (default) and a Scope configuration open, leaving also_scope unset asks the user
        (prompt, or an error that tells you to ask and call again with true/false)."""
        # R6: arguments that can never work are refused here, before the call queues behind a long job on the worker
        if mode not in ("cycle", "change", "poll"):
            raise ToolError("mode must be cycle | change | poll")
        if engine is not None and str(engine).lower() not in ("auto", "native", "python"):
            raise ToolError("engine must be auto | native | python")
        want_scope = await _scope_decide(also_scope, scope, ctx)
        res = await app.rec_call(app.rec.start, symbols=symbols, prefix=prefix, pattern=pattern, mode=mode,
                                 cycle_ms=cycle_ms, duration_s=duration_s, max_events=max_events,
                                 start_when=start_when, stop_when=stop_when, pre_trigger_ms=pre_trigger_ms,
                                 name=name, max_symbols=max_symbols, post_trigger_s=post_trigger_s,
                                 allow_partial=allow_partial, max_delay_ms=max_delay_ms, cycle_counter=cycle_counter,
                                 edge_triggers=edge_triggers, engine=engine, timeout=120)
        app.audit("rec_start", {"id": res["id"], "symbols": res["symbols"], "mode": mode, "engine": res.get("engine")},
                  OpClass.EDIT, "ok")
        if want_scope is not None:
            res["scope"] = await _scope_start(res["id"], want_scope)
        return res

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
                ans = await ctx.elicit(q, AlsoScopeSchema)
                if ans.action == "accept" and ans.data is not None:
                    return name if ans.data.also_scope else None
            except Exception:  # noqa: BLE001 — no elicitation in this client: fall through to asking via the agent
                pass
        raise ToolError(f"recorder.also_scope = 'ask': {q} Ask the user, then call rec_start again with also_scope=true "
                        f"(scope=<name> if several) or also_scope=false. Nothing was started. "
                        f"([recorder] also_scope = 'always' / 'never' makes this permanent.)")

    async def _scope_start(rid: str, name: str) -> dict[str, Any]:
        try:
            r = await app.xae_call(app.xae.scope_action, "start", name or None, timeout=60)
        except ToolError as exc:
            return {"error": f"Scope recording NOT started: {exc}. The ADS recording {rid} runs regardless."}
        app.scope_runs[rid] = {"scope": r["scope"], "name": name or None, "started": dt.datetime.now(dt.timezone.utc).isoformat(),
                               "start_result": r.get("result_code")}
        app.audit("scope_start", {"id": rid, "scope": r["scope"]}, OpClass.EDIT, "ok")
        return {"started": True, **r, "note": "Scope records independently (its own ADS acquisition and timestamps); "
                                              "it is stopped, saved and exported at rec_stop"}

    async def _scope_finish(rid: str) -> dict[str, Any] | None:
        run = app.scope_runs.pop(rid, None)
        if run is None:
            return None
        d = _scope_dir()
        svdx, csv = d / f"{rid}.svdx", d / f"{rid}.scope.csv"
        out: dict[str, Any] = {"scope": run["scope"]}
        steps = (("stop", None), ("save_svd", str(svdx)), ("export_csv", str(csv)))
        for action, path in steps:
            try:
                r = await app.xae_call(app.xae.scope_action, action, run["name"], path, timeout=600)
                out[action] = {k: r[k] for k in ("result_code", "path", "warning") if k in r}
            except ToolError as exc:
                out[action] = {"error": str(exc)}
                if action == "stop":
                    out["note"] = "Scope did not stop — stop it in XAE and save it by hand (scope_record)"
                    return out
        if svdx.is_file() and not csv.is_file():
            # the export tool from the saved file instead (same CSV writer, no XAE needed)
            from . import scope as S
            tool = await asyncio.to_thread(S.find_tool, app.cfg.scope.export_tool)
            if tool is not None:
                try:
                    await asyncio.to_thread(S.export, svdx, csv, tool=tool, key=app.rec.key, version=__version__)
                    out["export_csv"] = {"path": str(csv), "via": "TC3ScopeExportTool.exe"}
                except S.ScopeError as exc:
                    out["export_csv"] = {"error": str(exc)}
        out["link"] = await asyncio.to_thread(_scope_link, rid, run, svdx, csv)
        app.audit("scope_finish", {"id": rid, "scope": run["scope"], "files": out["link"].get("files")}, OpClass.EDIT, "ok")
        return out

    def _scope_link(rid: str, run: dict[str, Any], svdx: Path, csv: Path) -> dict[str, Any]:
        """Signed <id>.scope.json tying the ADS recording to the Scope files (SHA-256 each); the files are set
        read-only like the recording (R9)."""
        from .recorder import _manifest_sig, file_sha256
        import os as _os
        import stat as _stat
        files = {}
        for p in (svdx, csv):
            if p.is_file():
                sha, n = file_sha256(p)
                files[p.name] = {"sha256": sha, "bytes": n}
        link: dict[str, Any] = {"_scope_link": 1, "recording": rid, "scope": run["scope"], "started": run["started"],
                                "stopped": dt.datetime.now(dt.timezone.utc).isoformat(), "files": files,
                                "recorder": f"twincat-mcp {__version__}"}
        if app.rec.key:
            link["hmac"] = _manifest_sig(app.rec.key, link)
        lp = svdx.with_name(f"{rid}.scope.json")
        lp.write_text(json.dumps(link, indent=1) + "\n", encoding="utf-8")
        for p in (svdx, csv, lp):
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
        if source == "runtime":
            leaves = await app.ads_call(_scan_runtime, prefix, pattern, max_items, timeout=300, slow=True)
        elif source == "project":
            from .recorder import path_selected, selection_regex
            rx = selection_regex(pattern) if pattern else None
            pv = await app.proj_call(app.proj.variables, prefix, 64, max_items * 4, False)
            leaves = [{"path": it["path"], "type": it["type"], "size": None, "attrs": it.get("attrs")}
                      for it in pv["items"] if "[*]" not in it["path"]
                      and (rx is None or path_selected(rx, it["path"]))][:max_items]
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
            pv = await app.proj_call(app.proj.variables, scope, 64, 200_000, False)
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
        (paged: limit/offset; on_disk_total says how many exist)."""
        return await app.mem_call(app.rec.list, max(1, min(limit, 500)), max(0, offset))

    @mcp.tool(annotations=READ)
    async def rec_status(id: str) -> dict[str, Any]:
        """Live status of one recording: samples, events, rate, missing_samples (holes in the PLC timeline, cycle
        mode), completeness, dropped (lost on this PC), backlog, stats_coverage, per-block report, warnings,
        armed/triggered, last value per symbol."""
        return await app.mem_call(app.rec.status, id)

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
    async def rec_stop(id: str) -> dict[str, Any]:
        """Stop a recording, release the ADS notifications, finalize the file; returns status + stats."""
        res = await app.rec_call(app.rec.stop, id, "manual (rec_stop)", timeout=60)
        app.audit("rec_stop", {"id": id, "events": res.get("events")}, OpClass.EDIT, "ok")
        sc = await _scope_finish(id)
        if sc is not None:
            res["scope"] = sc
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
        return Path(app.cfg.scope.export_dir) if app.cfg.scope.export_dir else app.rec.root / "scope"

    @mcp.tool(annotations=EDIT)
    async def scope_export(svd: str, target: str | None = None, overwrite: bool = False) -> dict[str, Any]:
        """Export a saved TwinCAT Scope recording (.svdx / .svd) to CSV with Beckhoff's TC3ScopeExportTool.exe
        (command line `"svd=<file>" target=<csv> silent`; CSV needs no extra licence). Default target:
        <recordings>/scope/<name>.csv ([scope] export_dir). The CSV is Beckhoff's output, unchanged — channels, time
        column and alignment of channels with different sample times follow the Scope project's export settings. A
        signed <csv>.provenance.json records the source file and SHA-256, the tool and its SHA-256, and the command."""
        from . import scope as S
        tool = await asyncio.to_thread(S.find_tool, app.cfg.scope.export_tool)
        if tool is None:
            raise ToolError(S.status(app.cfg.scope.export_tool)["note"])
        src = Path(svd)
        out = app.out_path(target, overwrite, "scope export") if target else _scope_dir() / (src.stem + ".csv")
        try:
            res = await asyncio.to_thread(S.export, src, out, tool=tool, overwrite=overwrite, key=app.rec.key,
                                          version=__version__)
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
    async def scope_record(action: str, scope: str | None = None, path: str | None = None) -> dict[str, Any]:
        """Drive a Scope configuration open in XAE (Automation Interface IMeasurementScope): action = start | stop |
        save (SaveSVD + ExportCSV → <recordings>/scope/<scope>-<time>.svdx/.csv, or path=<file>.svdx). scope = its
        name when several are open. Affects only the Scope recording, not the PLC."""
        if action in ("start", "stop"):
            r = await app.xae_call(app.xae.scope_action, action, scope, None, timeout=120)
        elif action == "save":
            base = app.out_path(str(Path(path).with_suffix(".svdx")), False, "scope file").with_suffix("") if path else _scope_dir() / (
                re.sub(r"[^\w.-]+", "_", scope or "scope") + "-" + time.strftime("%Y%m%d-%H%M%S"))
            r = {"save_svd": await app.xae_call(app.xae.scope_action, "save_svd", scope, str(base) + ".svdx", timeout=600)}
            try:
                r["export_csv"] = await app.xae_call(app.xae.scope_action, "export_csv", scope, str(base) + ".csv", timeout=600)
            except ToolError as exc:
                r["export_csv"] = {"error": str(exc), "hint": "scope_export(svd=…) runs Beckhoff's export tool on the saved file"}
        else:
            raise ToolError("action must be start | stop | save")
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
        res = await app.confirmed("rec_delete", {"id": path.stem, **fp}, OpClass.DESTRUCTIVE, app.rec_call(app.rec.delete, path.stem))
        res["deleted_file_sha256"] = fp["sha256"]
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
        return await app.proj_call(codecheck.fb_interface, app.proj, name)

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
        return await app.proj_call(codecheck.lint, app.proj, name, rules, prefixes or cfg.project.naming_prefixes or None)

    @mcp.tool(annotations=READ)
    async def proj_call_graph(root: str, depth: int = 4) -> dict[str, Any]:
        """Call graph from a program/FB: FB instances it invokes, functions and methods it calls,
        recursively to `depth` (what runs when this POU runs)."""
        return await app.proj_call(codecheck.call_graph, app.proj, root, min(depth, 10))

    @mcp.tool(annotations=EDIT)
    async def tcunit_scaffold(fb_name: str, out_dir: str | None = None, add_to_plcproj: bool = True,
                              overwrite: bool = False) -> dict[str, Any]:
        """Generate <FB>_Tests.TcPOU (TcUnit.FB_TestSuite) with Test_InitialState and one test
        method per public method, arranged from the FB interface, and register it in the
        .plcproj. Assertions are left as TODO stubs."""
        if out_dir:
            out_dir = str(app.out_path(out_dir, what="scaffold folder", is_dir=True))       # R4
        touches_project = add_to_plcproj or out_dir is None
        if not touches_project and app.proj.root is not None:
            try:
                touches_project = Path(app.proj.root).resolve() in Path(out_dir).resolve().parents or \
                    Path(out_dir).resolve() == Path(app.proj.root).resolve()
            except OSError:
                touches_project = True
        # N12: a scaffold written outside the project (and not registered in the .plcproj) is not a code edit of the
        # PLC project, so edit_requires_config_mode does not apply to it
        await app.permit(None, OpClass.EDIT, "tcunit_scaffold" if touches_project else "tcunit_scaffold_external", fb_name)
        res = await app.confirmed("tcunit_scaffold", {"fb": fb_name}, OpClass.EDIT, app.proj_call(codecheck.tcunit_scaffold, app.proj, fb_name, out_dir, add_to_plcproj, overwrite))
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
        in the CSV note column and the JSON footer). Returns counts, largest_roots, file paths and a 50-leaf
        preview."""
        od = str(app.out_path(out_dir, what="dump folder", is_dir=True)) if out_dir else str(Path(cfg.safety.audit_log).parent / "dumps")
        if not 1 <= max_total_mb <= 4096 or not 1 <= max_leaves <= 5_000_000:
            raise ToolError("max_total_mb must be 1..4096 and max_leaves 1..5,000,000")
        own: set[str] | None = None
        try:
            if app.proj.root is not None:
                await asyncio.to_thread(app.proj.ensure_fresh)
                own = {u.name.lower() for u in app.proj.units.values() if u.kind in ("PROGRAM", "GVL")} or None
        except Exception:  # noqa: BLE001 — ordering is a nicety; without the project the order is alphabetical
            own = None

        def on_timeout() -> str:
            # N52: the verdict is read under the dump's commit lock — "kept" only if it was renamed into place
            return dump_timeout_message(app.ads.dump_outcome(), 900)
        return await app.ads_call(app.ads.dump, pattern, include_system, od, max_total_mb * MIB, max_leaves,
                                  cancel=app.ads_bulk_worker.cancel_event, own_roots=own, timeout=900, bulk=True,
                                  timeout_msg=on_timeout)

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
        item with their type."""
        return await app.proj_call(app.proj.variables, scope, max_array, max_items, include_temp)

    @mcp.tool(annotations=READ)
    async def proj_vs_runtime(scope: str | None = None, max_items: int = 200_000) -> dict[str, Any]:
        """Cross-check the source against the running PLC: variables declared in the project but
        not present online (not compiled / not activated / optimised away) and online variables
        under the same programs/GVLs that the source does not declare (stale boot project, library
        internals). Use after a deploy to prove the target runs this source."""
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
        """Modal dialogs currently open in the attached XAE shell (title, text, buttons), the
        watchdog's auto-answer rules and its recent log. A pending dialog blocks every COM call."""
        return await asyncio.to_thread(lambda: {**app.xae.watchdog.status(), "live": [d.to_dict() for d in __import__("twincat_mcp.dialogs", fromlist=["list_dialogs"]).list_dialogs(app.xae.pid)]})

    @mcp.tool(annotations=DESTR)
    async def xae_dialog_resolve(button: str, hwnd: int | None = None, remember: bool = False,
                                 confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Click a button on an open XAE dialog ('OK', 'Yes', 'No', 'Cancel'…). remember=true adds an
        auto-answer rule for that title (never for machine-affecting prompts). Control-class;
        machine-affecting prompts (activate/run/download/login/delete/safety) are destructive-class."""
        pend = await asyncio.to_thread(lambda: __import__("twincat_mcp.dialogs", fromlist=["list_dialogs"]).list_dialogs(app.xae.pid))
        target = next((d for d in pend if hwnd is None or d.hwnd == hwnd), None)
        op = OpClass.DESTRUCTIVE if (target and target.machine_affecting()) else OpClass.CONTROL
        await app.permit(ctx, op, "xae_dialog_resolve", f"{(target.title if target else '?')}:{button}", confirm_token)
        res = await asyncio.to_thread(app.xae.watchdog.resolve, button, hwnd, remember)
        app.audit("xae_dialog_resolve", {"button": button, "dialog": target.title if target else None}, op, "ok" if res.get("resolved") else "not-found", res)
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
        """Variable links of the PLC instance: every AT %I*/%Q* variable with its link target;
        lists the unlinked ones (the usual reason an output never moves)."""
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
        if check_source:
            try:
                pv = await app.proj_call(app.proj.variables, None, 64, 200_000, False)
                declared = {it["path"].lower() for it in pv["items"] if "[*]" not in it["path"]}
            except ToolError:
                declared = None
        return await asyncio.to_thread(app.hmi.compare, online, declared)



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
        """Index a book (or glob, e.g. 'tcplclib_tc3_*'): pages new or changed since the last run, title/kind/library/
        declaration, full-text index. With the local Help Viewer package every book is indexed in the background on
        first use anyway (≈ 50k pages, a few minutes); this indexes one book now. With docs.source='web' it fetches
        from infosys.beckhoff.com (Tc2_System ≈ 300 pages ≈ 30-60 s)."""
        if any(c in book for c in "*?"):
            return await app.docs_call("index_books", book, force, max_pages, timeout=3600)
        return await app.docs_call("index_book", book, force, max_pages, timeout=1800)

    @mcp.tool(annotations=READ)
    async def infosys_search_symbols(query: str, kind: str | None = None, library: str | None = None,
                                     limit: int = 10) -> dict[str, Any]:
        """Find library symbols (function blocks, functions, structs, enums, aliases, interfaces,
        GVLs) by name fragment in the indexed books; exact > prefix > contains. kind: FunctionBlock |
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
        before writing code against an FB you have not used before."""
        return await app.docs_call("declaration", symbol, library, timeout=1800)

    @mcp.tool(annotations=READ)
    async def infosys_search(query: str, library: str | None = None, limit: int = 10,
                             web_fallback: bool | None = None) -> dict[str, Any]:
        """Full-text search over the indexed Infosys pages (FTS5, bm25-ranked, excerpt with [hits]); "quoted phrases"
        stay together, limit 1..50. Local Help Viewer package (default): no network (web_fallback is ignored and said
        so), results carry the page's doc version and the package date. docs.source='web': when the index has nothing,
        a DuckDuckGo site: query verified against the sitemap; the 'backend' field says which path answered."""
        return await app.docs_call("search", query, library, limit, web_fallback, timeout=300)

    @mcp.tool(annotations=READ)
    async def infosys_search_code(query: str, language: str | None = None, limit: int = 10,
                                  ignore_spacing: bool = False) -> dict[str, Any]:
        """Search the code examples of the indexed pages for a fragment — a call pattern, an attribute, a constant
        (case-insensitive substring of the code as written; non-breaking spaces count as spaces). language: one
        that occurs in the index (an unknown one is refused with the list), '' / 'none' = samples without a label.
        limit 1..50; total_matches says how many there are. ignore_spacing=true also matches when only the
        spacing around punctuation differs. Returns the matching lines with context and the page URL."""
        return await app.docs_call("search_code", query, language, limit, ignore_spacing, timeout=120)

    @mcp.tool(annotations=READ)
    async def infosys_get_page(ref: str, refresh: bool = False, max_chars: int = 60000) -> dict[str, Any]:
        """A documentation page as Markdown (headings, tables, fenced code). ref: full Infosys URL, 'book/pageid', a
        bare page id, or a symbol name ('ADSREAD'). Read from the local Help Viewer package (any page in it, indexed or
        not; doc version + breadcrumb included) or, with docs.source='web', fetched live. Use max_chars to keep it short."""
        return await app.docs_call("get_page", ref, refresh, max_chars, timeout=600)

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
        res = await app.proj_call(codecheck.validate_xml, app.proj, name, fix, regenerate_lineids)
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
            folder = await app.ads_call(app.files.resolve_prefix_dir, "boot:", timeout=60)
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
        lines = [f"# twincat-mcp session report", f"- started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(app.started))}",
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
        """Attach to the running XAE shell / Visual Studio (Running Object Table). Picks the
        instance holding solution_path when given. launch=true starts a new shell (slow)."""
        return await app.xae_call(app.xae.attach, progid, solution_path, launch, timeout=180)

    @mcp.tool(annotations=READ)
    async def xae_status() -> dict[str, Any]:
        """Shell version, open solution, projects, TwinCAT project name, target NetId, started flag.
        Attaches to the shell first if not attached yet (as every xae_* tool does)."""
        def _st():
            if app.xae.dte is None:
                try:
                    app.xae.attach()
                except XaeError as exc:
                    return {"attached": False, "reason": str(exc)}
            return app.xae.status()
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
        tail. check_all_objects=true runs PLC 'Check all objects' instead (compiles unused POUs)."""
        await app.permit(None, OpClass.EDIT, "xae_build")
        saved = await app.save_verified("xae_build") if save_first else {"skipped": "save_first=false"}
        if check_all_objects:
            res = await app.xae_call(app.xae.check_all_objects, timeout=600)
            res["save"] = saved
            return res
        res = await app.xae_call(app.xae.build, clean, rebuild, timeout=cfg.xae.build_timeout_s + 60)
        res["save"] = saved
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
        tgt = await app.xae_target_for("xae_activate_configuration", OpClass.DESTRUCTIVE, confirm_token)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_activate_configuration", tgt, confirm_token)
        res = await app.xae_call(app.xae.activate_configuration, timeout=600)
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
        tgt = await app.xae_target_for("xae_restart_twincat", OpClass.DESTRUCTIVE, confirm_token)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_restart_twincat", tgt, confirm_token)
        t0 = time.monotonic()
        before = await app.restart_witness()                  # R40: what a restart must change, read before it
        res = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        ver = await app.verify_target_after("xae_restart_twincat", expect_run=True, require_transition=True, before=before)
        # N5: the shell returns as soon as the command is accepted; the restart is over when the target reports RUN
        res.update({"restarted": bool(ver["verified"]), "target_mode": ver["target_mode"], "dialogs": ver["dialogs"],
                    "verification_polls": ver["polls"], "modes_seen": ver["modes_seen"],
                    "duration_s": round(time.monotonic() - t0, 1)})
        res.update({k: ver[k] for k in _TRANSITION_KEYS if k in ver})
        if ver.get("hint"):
            res["hint"] = ver["hint"]
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
        tgt = await app.xae_target_for("xae_deploy", OpClass.DESTRUCTIVE, confirm_token)
        await app.check_targets(tgt, allow_target_mismatch)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_deploy", tgt, confirm_token)
        steps: dict[str, Any] = {}
        steps["save"] = await app.save_verified("xae_deploy")
        if not skip_build:
            b = await app.xae_call(app.xae.build, False, False, timeout=cfg.xae.build_timeout_s + 60)
            steps["build"] = {k: b[k] for k in ("success", "errors", "warnings", "duration_s")}
            if not b["success"]:
                steps["build"]["items"] = b["items"][:50]
                app.audit("xae_deploy", {"target": tgt}, OpClass.DESTRUCTIVE, "build-failed", steps)
                return {"deployed": False, "steps": steps}
        if plc_project_path:
            steps["boot_project"] = await app.xae_call(app.xae.boot_project, plc_project_path, True, True, False, timeout=300)
        before = await app.restart_witness()                  # R40: before the activation — what a restart must change
        steps["activate"] = await app.xae_call(app.xae.activate_configuration, timeout=600)
        t_restart = time.monotonic()
        steps["restart"] = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        # P17: a deploy is done when the target runs it, not when the shell accepted the commands
        ver = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before)
        # N5: the restart lasted from its command until the target was seen running again (command_s: the shell part)
        steps["restart"]["duration_s"] = round(time.monotonic() - t_restart, 1)
        steps["verify"] = {"running": bool(ver["verified"]), "target_mode": ver["target_mode"], "dialogs": ver["dialogs"],
                           "polls": ver["polls"], "modes_seen": ver["modes_seen"]}
        steps["verify"].update({k: ver[k] for k in _TRANSITION_KEYS if k in ver})
        if ver.get("hint"):
            steps["verify"]["hint"] = ver["hint"]
        ok = bool(ver["verified"])
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
        Roots: TIID (I/O), TIPC (PLC), TIRT (tasks), TIRC (real-time settings), TIRR (routes), TINC (NC)."""
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
                             declaration: str | None = None, implementation: str | None = None) -> dict[str, Any]:
        """Create a PLC object under a folder/POU: kind = program | fb | function | method |
        property | action | transition | struct | enum | union | alias | gvl | interface |
        itf_method | itf_property | folder | parameter_list. Optional code is set after creation."""
        app.guard_safety_path(parent_path, "xae_pou_create")
        await app.permit(None, OpClass.EDIT, "xae_pou_create", f"{parent_path}^{name}")
        res = await app.xae_call(app.xae.pou_create, parent_path, name, kind, language, return_type,
                                 extends, implements, access, declaration, implementation, timeout=120)
        if kind.lower() != "folder":
            app.note_edit(f"{parent_path}^{name}", declaration, implementation)
        app.audit("xae_pou_create", {"parent": parent_path, "name": name, "kind": kind}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_plcopen_import(plc_project_path: str, xml_file: str, option: str = "none") -> dict[str, Any]:
        """Import a PLCopen XML file into a PLC project (path from xae_plc_projects).
        option: none | rename | replace | skip (conflict handling).
        replace: TwinCAT replaces an object only at the same place; an object that lives in a folder is put at the
        project root as a renamed copy (PRG_X_1, header renamed too). The tool finishes the replace: the copy's header
        is named back, the texts are checked against the XML, written into the existing object, read back, and the
        declared name must equal the object's name. Only then is the copy — created by this import, it was not in the
        tree before — deleted (nothing that existed before the import is ever deleted). Anything not proven → nothing
        written, the copy kept and named in the error. Result: replaced, header_renamed, copies_removed,
        build_required + objects_to_build (build next)."""
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
        (Tc2_Standard.CTU.CV) are completed from the runtime's datatype table when the target is reachable."""
        res = await app.proj_call(app.proj.resolve_symbol, symbol)
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
            steps["build"] = {k: b[k] for k in ("success", "errors", "warnings")}
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
                where = await app.ads_call(app.files.resolve_prefix_dir, "boot:", timeout=30) if rp.lower().startswith("boot:") else None
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
    _apply_tool_groups(mcp, cfg)
    return mcp
