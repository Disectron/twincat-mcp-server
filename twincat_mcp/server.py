"""MCP server wiring: tools, resources, safety plumbing.

Tool prefixes
  tc_*     meta / safety / diagnostics
  ads_*    runtime (target IPC) over ADS
  ipc_*    target file system / OS via the TwinCAT System Service
  xae_*    TwinCAT XAE Automation Interface (engineering PC)
  proj_*   offline project files
  tcunit_* TcUnit orchestration + result parsing
  exp_*    experiments: spec → runs → metrics with CIs → verdict → report
  infosys_* Beckhoff Infosys reference (TwinCAT 3 docs, locally indexed)

Confirmation for destructive operations: if the MCP client supports
elicitation, the human is asked directly (``ctx.elicit``); otherwise the
two-step confirm_token flow applies (``tc_request_confirmation``).
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import platform
import re
import sys
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
from .worker import SerialWorker
import pyads

from .ads_backend import AdsBackend, AdsError, _fmt_exc as _fmt_ads_exc
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

Reference: infosys_declaration <FB|struct|enum> gives the library symbol's declaration with
descriptions; infosys_search_symbols / infosys_search / infosys_search_code / infosys_get_page
look up Beckhoff Infosys (TwinCAT 3 only, locally indexed per library, verified against the
site's sitemap). Use it before guessing a library FB's inputs.

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


class ConfirmSchema(BaseModel):
    confirm: bool = Field(description="Set true to allow this operation on the target")


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.guard = Guard(cfg.safety)
        self.ads = AdsBackend(cfg.ads)
        self.files = SystemServiceFiles(self.ads)
        import threading
        self.ads_worker = SerialWorker("tcmcp-ads", cancel_event=threading.Event())       # quick calls
        self.ads_slow_worker = SerialWorker("tcmcp-ads-slow", cancel_event=self.ads.cancel)  # watch/wait/scenario/dump
        self.xae = XaeBackend(cfg.xae)
        self.xae_worker = SerialWorker("tcmcp-xae-sta", init=sta_init, teardown=sta_teardown)
        self.proj = ProjectIndex(cfg.project)
        self.rec = Recorder(self.ads, Path(cfg.safety.audit_log).parent / "recordings")
        self.bench = TestBench(self.ads, self.rec, Path(cfg.safety.audit_log).parent / "snapshots")
        self.hmi = HmiIndex()
        self.profiles = capture.ProfileStore(Path(cfg.safety.audit_log).parent / "profiles")
        self.exp_store = X.ExperimentStore(cfg.project.experiments_dir)
        self.runners: dict[str, X.Runner] = {}
        self.docs: InfosysIndex | None = None
        if cfg.docs.enabled:
            try:
                self.docs = InfosysIndex(cfg.docs.cache_dir, books=cfg.docs.books, exclude_books=cfg.docs.exclude_books,
                                         tc3_only=cfg.docs.tc3_only, max_workers=cfg.docs.max_workers,
                                         max_index_pages=cfg.docs.max_index_pages, sitemap_ttl_days=cfg.docs.sitemap_ttl_days,
                                         web_fallback=cfg.docs.web_fallback, fetch_timeout_s=cfg.docs.fetch_timeout_s)
            except Exception as exc:  # noqa: BLE001 — a broken cache dir must not take the server down
                log.warning("infosys index disabled: %s", exc)
        self.started = time.time()

    # --- runners -----------------------------------------------------------
    async def ads_call(self, fn, *a, timeout: float | None = None, slow: bool = False, **kw) -> Any:
        """Run on the quick ADS worker, or on the slow one (``slow=True``) for anything that
        blocks for seconds — so a long watch never stalls an ads_read."""
        worker = self.ads_slow_worker if slow else self.ads_worker
        try:
            return await worker.run(fn, *a, timeout=timeout or (self.cfg.ads.max_watch_duration_ms / 1000 + 30), **kw)
        except (AdsError, RecorderError, TestError) as exc:
            raise ToolError(str(exc)) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ToolError("ADS call timed out (router/route/firewall? run tc_doctor)") from exc
        except (pyads.ADSError, OSError) as exc:
            # safety net for a backend path that did not translate the pyads/socket error itself
            raise ToolError(f"ADS error: {_fmt_ads_exc(exc)}") from exc

    async def xae_call(self, fn, *a, timeout: float | None = None, **kw) -> Any:
        try:
            return await self.xae_worker.run(fn, *a, timeout=timeout or self.cfg.xae.build_timeout_s + 60, **kw)
        except XaeError as exc:
            raise ToolError(str(exc)) from exc
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
            raise ToolError(str(exc)) from exc

    async def rec_call(self, fn, *a, timeout: float = 60, **kw) -> Any:
        return await self.ads_call(fn, *a, timeout=timeout, slow=True, **kw)

    async def docs_call(self, fn, *a, timeout: float = 120, **kw) -> Any:
        """Infosys lookups: network + SQLite, off the event loop; no ADS/COM worker involved."""
        if self.docs is None:
            raise ToolError("Infosys lookup is disabled (docs.enabled=false / TCMCP_DOCS=0)")
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *a, **kw), timeout)
        except InfosysError as exc:
            raise ToolError(str(exc)) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ToolError(f"Infosys call exceeded {timeout:.0f} s (slow network? smaller max_pages?)") from exc

    async def mem_call(self, fn, *a, **kw) -> Any:
        """Recorder queries that touch only memory/files — no ADS worker needed."""
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except (RecorderError, AdsError, TestError) as exc:
            raise ToolError(str(exc)) from exc

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
                     confirm_token: str | None = None) -> None:
        """Check the guard; for CONTROL/DESTRUCTIVE without a token, try to
        ask the human through elicitation before failing."""
        scope = self.scope()
        try:
            self.guard.check(op, action=action, target=target, confirm_token=confirm_token, scope=scope)
            return
        except SafetyError as exc:
            # elicitation only replaces a *missing* token; a wrong/stale/foreign token is a hard error
            needs_token = confirm_token is None and "needs a confirm_token" in str(exc) \
                and op in (OpClass.CONTROL, OpClass.DESTRUCTIVE)
            if not needs_token or ctx is None:
                raise ToolError(str(exc)) from exc
        # elicitation path
        try:
            res = await ctx.elicit(
                f"twincat-mcp wants to run {action} on {target or 'the target'} — machine {scope} "
                f"(mode={self.guard.mode}). Allow?", ConfirmSchema)
        except Exception as exc:  # noqa: BLE001 — client has no elicitation support
            raise ToolError(
                f"{action} on {target!r} needs confirmation and this client does not support "
                f"elicitation ({type(exc).__name__}). Call tc_request_confirmation(action={action!r}, "
                f"target={target!r}) and pass the confirm_token."
            ) from exc
        if res.action == "accept" and res.data is not None and res.data.confirm:
            self.guard.audit(action, {"target": target, "scope": scope}, op, "human-confirmed-elicitation")
            return
        raise ToolError(f"{action} on {target!r} was declined by the user ({res.action}).")

    def audit(self, tool: str, args: dict[str, Any], op: OpClass, outcome: str, detail: Any = None) -> None:
        self.guard.audit(tool, args, op, outcome, detail)


async def _vs_runtime(app: "App", scope: str | None, max_items: int) -> dict[str, Any]:
    proj = await app.proj_call(app.proj.variables, scope, 64, max_items, False)
    declared = {it["path"].lower(): it for it in proj["items"] if "[*]" not in it["path"]}
    roots = {p.split(".")[0] for p in declared}
    online: set[str] = set()
    online_types: dict[str, str] = {}
    for r in roots:
        try:
            groups = await app.ads_call(app.ads.leaves_under, r, max_items, timeout=300)
        except ToolError:
            continue
        for root, leaves in groups:
            for lf in leaves:
                online.add(lf.path.lower())
                online_types[lf.path.lower()] = lf.type
    missing = [declared[k] for k in sorted(set(declared) - online)]
    extra = sorted(online - set(declared))
    # declared composite (library type) items match if any online leaf starts with them
    missing_final = []
    for it in missing:
        pre = it["path"].lower() + "."
        if any(o.startswith(pre) or o.startswith(it["path"].lower() + "[") for o in online):
            continue
        missing_final.append(it)
    return {"declared": len(declared), "online_under_same_roots": len(online),
            "matched": len(declared) - len(missing_final), "missing_online": missing_final[:500],
            "extra_online": [{"path": e, "type": online_types.get(e)} for e in extra[:500]],
            "roots": sorted(roots)}



def build_server(cfg: Config | None = None) -> MCPServer:
    cfg = cfg or load_config()
    app = App(cfg)
    mcp = MCPServer("twincat-mcp", instructions=INSTRUCTIONS, version=__version__)

    # ======================================================================
    # tc_* meta
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def tc_doctor() -> dict[str, Any]:
        """Environment check: platform, pyads/router, local AMS NetId, XAE ProgIDs in the
        registry, running XAE shells, project root, safety mode. Run this first."""
        rep: dict[str, Any] = {"version": __version__, "python": sys.version.split()[0],
                               "platform": platform.platform(), "windows": is_windows(),
                               "safety": app.guard.status()}
        # pyads / router
        try:
            rep["local_ams_net_id"] = await app.ads_call(app.ads.local_net_id, timeout=10)
            rep["router"] = "ok"
        except ToolError as exc:
            rep["router"] = f"FAIL: {exc} — is TwinCAT XAE/XAR installed and the TwinCAT System Service running?"
        rep["ads_target"] = {"ams_net_id": cfg.ads.ams_net_id or "(local)", "port": cfg.ads.ams_port}
        try:
            rep["target_state"] = await app.ads_call(app.ads.state, timeout=10)
        except ToolError as exc:
            rep["target_state"] = f"FAIL: {exc}"
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
        rep["project_root"] = cfg.project.root
        rep["solution"] = cfg.xae.solution_path
        rep["config_file"] = cfg.config_file
        if app.docs is not None:
            try:
                rep["infosys"] = {"cache": str(app.docs.dir), "fts5": app.docs.has_fts,
                                  "indexed_books": await app.docs_call(app.docs.indexed_books, timeout=15),
                                  "default_books": cfg.docs.books}
            except ToolError as exc:
                rep["infosys"] = f"FAIL: {exc}"
        else:
            rep["infosys"] = "disabled"
        rep["config"] = cfg.to_dict()
        return rep

    @mcp.tool(annotations=READ)
    async def tc_safety_status() -> dict[str, Any]:
        """Current safety mode, ceiling, allow/deny lists, pending confirmation tokens."""
        return app.guard.status()

    @mcp.tool(annotations=EDIT)
    async def tc_set_mode(mode: str) -> dict[str, Any]:
        """Switch safety mode: readonly | guarded | full. Escalation is capped at the
        configured max_mode (TCMCP_MAX_MODE); lowering is always allowed."""
        try:
            old = app.guard.set_mode(mode)
        except SafetyError as exc:
            raise ToolError(str(exc)) from exc
        return {"previous": old, "mode": app.guard.mode}

    @mcp.tool(annotations=READ)
    async def tc_request_confirmation(action: str, target: str = "") -> dict[str, Any]:
        """Issue a single-use confirm_token for a control/destructive tool. Use the exact
        action name of the tool (e.g. 'xae_activate_configuration') and its target."""
        return app.guard.request_token(action, target, app.scope())

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
        res = await app.ads_call(app.ads.connect, ams_net_id, ams_port, ip_address, timeout=20)
        app.audit("ads_connect", {"ams_net_id": ams_net_id, "port": ams_port}, OpClass.CONTROL, "ok")
        return res

    @mcp.tool(annotations=READ)
    async def ads_status(port: int | None = None) -> dict[str, Any]:
        """Composite status: connection, PLC runtime state, TwinCAT system mode (RUN/CONFIG),
        device name/version. With port=N returns only the ADS state of that port (852 PLC2,
        500 NC, 10000 system service, 65535 EtherCAT master 1)."""
        if port is not None:
            return await app.ads_call(app.ads.state, port, timeout=15)
        out: dict[str, Any] = {"connection": app.ads.connection_status()}
        for key, fn in (("plc", app.ads.state), ("system", app.ads.system_state), ("device", app.ads.device_info)):
            try:
                out[key] = await app.ads_call(fn, timeout=15)
            except ToolError as exc:
                out[key] = {"error": str(exc)}
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
        return await app.ads_call(app.ads.read_many, names, timeout=60)

    @mcp.tool(annotations=WRITE)
    async def ads_write(name: str, value: Any, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write a symbol on the running PLC. Accepts JSON scalars, lists for arrays, and
        literals like 'T#1s500ms', 'D#2026-10-15', '16#FF'. Returns before/after.
        Gated: readonly=never; guarded=write_allowlist; full=denylist only."""
        await app.permit(ctx, OpClass.WRITE, "ads_write", name)
        for member in await app.ads_call(app.ads.members, name, timeout=30):  # opaque hex write touches every member
            await app.permit(ctx, OpClass.WRITE, "ads_write", member.name)
        try:
            res = await app.ads_call(app.ads.write, name, value, timeout=30)
        except ToolError as exc:
            app.audit("ads_write", {"name": name, "value": value}, OpClass.WRITE, "error", str(exc))
            raise
        app.audit("ads_write", {"name": name, "value": value}, OpClass.WRITE, "ok", res)
        return res

    @mcp.tool(annotations=WRITE)
    async def ads_write_many(values: dict[str, Any], ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write several symbols sequentially ({name: value}). Each one is gated and audited;
        stops at the first refusal."""
        out: dict[str, Any] = {}
        for n, v in values.items():
            await app.permit(ctx, OpClass.WRITE, "ads_write", n)
            try:
                out[n] = await app.ads_call(app.ads.write, n, v, timeout=30)
                app.audit("ads_write_many", {"name": n, "value": v}, OpClass.WRITE, "ok", out[n])
            except ToolError as exc:
                out[n] = {"error": str(exc)}
                app.audit("ads_write_many", {"name": n, "value": v}, OpClass.WRITE, "error", str(exc))
        return out

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
        await app.permit(ctx, OpClass.WRITE, "ads_write_raw", tgt)
        overlapped = await app.ads_call(app.ads.symbols_overlapping, index_group, index_offset, size, port, timeout=60)
        for nm in overlapped:
            await app.permit(ctx, OpClass.WRITE, "ads_write_raw", nm)
        res = await app.ads_call(app.ads.write_raw, index_group, index_offset, hex_data, port, ams_net_id, timeout=30)
        res["overlapping_symbols"] = overlapped
        app.audit("ads_write_raw", {"target": tgt, "hex": hex_data[:64]}, OpClass.WRITE, "ok", res)
        return res

    @mcp.tool(annotations=READ)
    async def ads_watch(names: list[str], duration_ms: int = 2000, interval_ms: int = 50,
                        change_only: bool = True) -> dict[str, Any]:
        """Sample symbols for duration_ms every interval_ms (polling trace). change_only keeps
        only rows where a value changed. Caps: 60 s, ≥10 ms, 5000 rows. Use for timing
        questions: 'does bDone pulse?', 'how long is the valve open?'."""
        return await app.ads_call(app.ads.watch, names, duration_ms, interval_ms, change_only,
                                  timeout=cfg.ads.max_watch_duration_ms / 1000 + 30, slow=True)

    @mcp.tool(annotations=READ)
    async def ads_wait_until(name: str, op: str, value: Any, timeout_ms: int = 10000,
                             interval_ms: int = 50) -> dict[str, Any]:
        """Block until symbol <op> value (==, !=, >, >=, <, <=) or timeout. Returns met/elapsed."""
        return await app.ads_call(app.ads.wait_until, name, op, value, timeout_ms, interval_ms,
                                  timeout=min(timeout_ms, 600_000) / 1000 + 10, slow=True)

    @mcp.tool(annotations=DESTR)
    async def ads_plc_state(action: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """PLC runtime control via ADS: run | stop | reset. 'reset' is a cold reset (all
        non-persistent data lost). Control-class: confirmation in guarded mode."""
        op = OpClass.DESTRUCTIVE if action == "reset" else OpClass.CONTROL
        await app.permit(ctx, op, "ads_plc_state", action, confirm_token)
        res = await app.ads_call(app.ads.set_plc_state, action, timeout=30)
        app.audit("ads_plc_state", {"action": action}, op, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def ads_system_mode(action: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Switch TwinCAT on the target between 'run' and 'config' mode (restarts the
        runtime, I/O goes down). Destructive-class."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ads_system_mode", action, confirm_token)
        res = await app.ads_call(app.ads.set_system_mode, action, timeout=30)
        app.audit("ads_system_mode", {"action": action}, OpClass.DESTRUCTIVE, "ok", res)
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
        res = await app.ads_call(app.ads.add_route_to_plc, plc_ip, plc_username, plc_password, route_name, local_net_id, hostname, timeout=30)
        app.audit("ads_add_route_to_plc", {"plc_ip": plc_ip, "route_name": route_name}, OpClass.CONTROL, "ok", res)
        return res


    # ======================================================================
    # rec_* real-time recorder
    # ======================================================================
    @mcp.tool(annotations=EDIT)
    async def rec_start(symbols: list[str] | None = None, prefix: str | None = None, pattern: str | None = None,
                        mode: str = "change", cycle_ms: float = 10, duration_s: float | None = None,
                        max_events: int | None = 5_000_000, start_when: str | None = None,
                        stop_when: str | None = None, pre_trigger_ms: float = 0, name: str | None = None,
                        max_symbols: int = 500) -> dict[str, Any]:
        """Start a continuous real-time recording of online PLC variables (runs in the background
        until rec_stop, duration_s, max_events or stop_when). Select by explicit symbols (an FB or
        struct instance expands to all its leaves), prefix ('MAIN.fbFill' → every leaf under it)
        or fnmatch pattern. mode: 'cycle' = one PLC-timestamped sample per symbol per cycle_ms
        (ADS notification, cycle-accurate); 'change' = PLC-timestamped sample only when the value
        changes (checked every cycle_ms); 'poll' = sum-read every cycle_ms from this PC.
        Triggers: start_when='MAIN.bStart == TRUE' with pre_trigger_ms keeps the events before the
        trigger (scope-style); stop_when='MAIN.fbFill.bDone == TRUE'. Events stream to a JSONL file."""
        res = await app.rec_call(app.rec.start, symbols=symbols, prefix=prefix, pattern=pattern, mode=mode,
                                 cycle_ms=cycle_ms, duration_s=duration_s, max_events=max_events,
                                 start_when=start_when, stop_when=stop_when, pre_trigger_ms=pre_trigger_ms,
                                 name=name, max_symbols=max_symbols, timeout=120)
        app.audit("rec_start", {"id": res["id"], "symbols": res["symbols"], "mode": mode}, OpClass.EDIT, "ok")
        return res


    # ======================================================================
    # Capture profiles: scan → choose → save → start
    # ======================================================================
    def _scan_runtime(prefix: str | None, pattern: str | None, max_items: int) -> list[dict[str, Any]]:
        import fnmatch
        out: list[dict[str, Any]] = []
        if prefix:
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
        pat = pattern.lower() if pattern else None
        for _root, leaves in groups:
            for lf in leaves:
                if pat and not fnmatch.fnmatchcase(lf.path.lower(), pat):
                    continue
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
        source='project' walks the source declarations (works offline). pattern = fnmatch filter on the
        full path. Returns counts by type and by group (program.instance) with bytes/s estimates at
        cycle_ms, a suggested mode per leaf, and — when checklist='<name>' — writes
        ~/.twincat_mcp/profiles/<name>.checklist.csv with a 'select' column to mark (preselect =
        patterns already marked). Then rec_profile_save(name, from_checklist=name)."""
        if source == "runtime":
            leaves = await app.ads_call(_scan_runtime, prefix, pattern, max_items, timeout=300, slow=True)
        elif source == "project":
            import fnmatch
            pv = await app.proj_call(app.proj.variables, prefix, 64, max_items * 4, False)
            leaves = [{"path": it["path"], "type": it["type"], "size": None, "attrs": it.get("attrs")}
                      for it in pv["items"] if "[*]" not in it["path"]
                      and (not pattern or fnmatch.fnmatchcase(it["path"].lower(), pattern.lower()))][:max_items]
        else:
            raise ToolError("source must be runtime | project")
        if not leaves:
            raise ToolError("scan found no variables (prefix/pattern wrong, or not connected / not scanned)")
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
        return {"deleted": ok}

    @mcp.tool(annotations=EDIT)
    async def rec_profile_start(name: str, start_when: str | None = None, stop_when: str | None = None,
                                duration_s: float | None = None, max_events: int | None = 5_000_000) -> dict[str, Any]:
        """Start recording a saved profile: one recording per (mode, cycle_ms) group, all named
        '<profile>/<mode>@<cycle>'. Triggers given here override the profile's. Returns the recording
        ids; rec_profile_stop(name) stops them together, rec_stats/rec_export work per id."""
        try:
            prof = await asyncio.to_thread(app.profiles.get, name)
        except capture.ProfileError as exc:
            raise ToolError(str(exc)) from exc
        started: list[dict[str, Any]] = []
        # a trigger's symbol must be inside each recording that uses the trigger
        cond_syms = [m.group(1) for c in (start_when or prof.start_when, stop_when or prof.stop_when) if c
                     for m in [re.match(r"\s*([A-Za-z_][\w\.\[\]]*)", c)] if m]
        try:
            for (mode, cyc), its in prof.groups().items():
                syms = [i.symbol for i in its]
                low = {x.lower() for x in syms}
                syms += [c for c in cond_syms if c.lower() not in low]
                res = await app.rec_call(app.rec.start, symbols=syms, prefix=None, pattern=None,
                                         mode=mode, cycle_ms=cyc, duration_s=duration_s or prof.duration_s,
                                         max_events=max_events, start_when=start_when or prof.start_when,
                                         stop_when=stop_when or prof.stop_when, pre_trigger_ms=prof.pre_trigger_ms,
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
        return {"profile": name, "recordings": started, "groups": len(started)}

    @mcp.tool(annotations=EDIT)
    async def rec_profile_stop(name: str) -> dict[str, Any]:
        """Stop every live recording started from this profile; returns per-recording status + stats."""
        out = []
        for rid, rec in list(app.rec.recs.items()):
            if rec.active and (rec.name or "").startswith(f"{name}/"):
                out.append(await app.rec_call(app.rec.stop, rid, timeout=60))
        if not out:
            raise ToolError(f"no live recording of profile {name!r} (rec_list shows what is running)")
        app.audit("rec_profile_stop", {"profile": name, "recordings": [o["id"] for o in out]}, OpClass.EDIT, "ok")
        return {"profile": name, "stopped": out}

    @mcp.tool(annotations=READ)
    async def rec_list() -> dict[str, Any]:
        """Recordings of this session (live status) and recording files on disk."""
        return await app.mem_call(app.rec.list)

    @mcp.tool(annotations=READ)
    async def rec_status(id: str) -> dict[str, Any]:
        """Live status of one recording: events, rate, drops, armed/triggered, last value per symbol."""
        return await app.mem_call(app.rec.status, id)

    @mcp.tool(annotations=READ)
    async def rec_snapshot(id: str, last_n: int = 100, symbols: list[str] | None = None) -> dict[str, Any]:
        """Most recent events of a running (or finished) recording — the live tail."""
        return await app.mem_call(app.rec.snapshot, id, min(last_n, 5000), symbols)

    @mcp.tool(annotations=READ)
    async def rec_stats(id: str, symbols: list[str] | None = None, include_runs: bool = True,
                        max_runs: int = 200) -> dict[str, Any]:
        """Per-symbol statistics over the whole recording so far: count, first/last, min/max/mean,
        change count, BOOL rising/falling edges with timestamps, and value runs (state timeline
        with dwell times) — computed incrementally, no re-read of the file."""
        return await app.mem_call(app.rec.stats, id, symbols, include_runs, min(max_runs, 5000))

    @mcp.tool(annotations=READ)
    async def rec_read(id: str, t_from_ms: float | None = None, t_to_ms: float | None = None,
                       symbols: list[str] | None = None, max_rows: int = 2000, wide: bool = False) -> dict[str, Any]:
        """Read a time window of a recording from its file. wide=true pivots to one row per event
        with all selected symbols forward-filled (what you want for 'what else was true when X
        happened'). Works on finished recordings from earlier sessions too."""
        return await app.mem_call(app.rec.read, id, t_from_ms, t_to_ms, symbols, min(max_rows, 50_000), wide)

    @mcp.tool(annotations=EDIT)
    async def rec_stop(id: str) -> dict[str, Any]:
        """Stop a recording, release the ADS notifications, finalize the file; returns status + stats."""
        res = await app.rec_call(app.rec.stop, id, timeout=60)
        app.audit("rec_stop", {"id": id, "events": res.get("events")}, OpClass.EDIT, "ok")
        return res

    @mcp.tool(annotations=READ)
    async def rec_export(id: str, fmt: str = "csv_wide", out_path: str | None = None,
                         symbols: list[str] | None = None) -> dict[str, Any]:
        """Export a recording to CSV: csv_wide (t_ms + one column per symbol, forward-filled) or
        csv_long (t_ms,symbol,value). Default path next to the JSONL."""
        return await app.mem_call(app.rec.export, id, fmt, out_path, symbols)

    @mcp.tool(annotations=DESTR)
    async def rec_delete(id: str) -> dict[str, Any]:
        """Stop (if running) and delete a recording and its exports from disk."""
        res = await app.rec_call(app.rec.delete, id)
        app.audit("rec_delete", {"id": id}, OpClass.EDIT, "ok", res)
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
        res = await app.ads_call(app.bench.call_method, instance, method, inputs, return_type, outputs, timeout=60)
        app.audit("ads_call_method", {"instance": instance, "method": method, "inputs": inputs}, OpClass.WRITE, "ok", res)
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
        await app.permit(None, OpClass.EDIT, "tcunit_scaffold", fb_name)
        res = await app.proj_call(codecheck.tcunit_scaffold, app.proj, fb_name, out_dir, add_to_plcproj, overwrite)
        app.audit("tcunit_scaffold", {"fb": fb_name, "file": res.get("file")}, OpClass.EDIT, "ok")
        return res


    # ======================================================================
    # whole-project variable access
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ads_dump(pattern: str | None = None, include_system: bool = False, out_dir: str | None = None,
                       max_total_mb: int = 64, max_leaves: int = 500_000) -> dict[str, Any]:
        """Read EVERY variable on the PLC (all top-level symbols, fully expanded through the
        datatype table — structs, FB instances, arrays of structs) in one pass and write a nested
        JSON tree plus a flat CSV (path,type,value). pattern limits the roots ('MAIN.*', 'GVL_*');
        system/library symbols excluded unless include_system. Returns counts, file paths and a
        50-leaf preview; use proj_read_file / rec-style tooling on the files for detail."""
        od = out_dir or str(Path(cfg.safety.audit_log).parent / "dumps")
        return await app.ads_call(app.ads.dump, pattern, include_system, od, max_total_mb * 1024 * 1024, max_leaves, timeout=900, slow=True)

    @mcp.tool(annotations=READ)
    async def ads_leaves(name: str, max_leaves: int = 5000, with_values: bool = False) -> dict[str, Any]:
        """Enumerate every scalar leaf under a symbol with byte offsets (datatype table):
        'MAIN' → all program variables, 'MAIN.arrSteps' → arrSteps[1].nId … With values=true
        each leaf is decoded from one read of the whole symbol."""
        groups = await app.ads_call(app.ads.leaves_under, name, max_leaves, timeout=120)
        out: list[dict[str, Any]] = []
        for root, leaves in groups:
            vals: dict[str, Any] = {}
            if with_values:
                try:
                    vals = await app.ads_call(app.ads.leaf_values, root, leaves, timeout=120)
                except ToolError as exc:
                    vals = {"__error__": str(exc)}
            for lf in leaves:
                d = {"path": lf.path, "type": lf.type, "offset": lf.offset, "size": lf.size, "root": root.name}
                if lf.enum:
                    d["enum"] = lf.enum
                if lf.note:
                    d["note"] = lf.note
                if with_values:
                    d["value"] = vals.get(lf.path, vals.get("__error__"))
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
        res = await app.xae_call(app.xae.io_disable, disable, only, timeout=120)
        app.audit("xae_io_disable", {"disable": disable, "only": only}, OpClass.EDIT, "ok", res)
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
        """Index HMI symbol bindings: TE2000 .view/.content/.usercontrol/.json (%s%PLC1.X%/s%) and
        TcVIS files. root defaults to project.hmi_root, else the project root."""
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
                "measure_kinds": X.MEASURE_KINDS, "stats": X.STATS, "criterion_ops": X.CRIT_OPS}

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
                meta["firmware_match"] = f"{vr['matched']}/{vr['declared']} declared symbols online, {len(vr['missing_online'])} missing"
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
        """List Infosys books (documentation sets) from the site's own sitemap, with page counts and
        whether they are indexed locally. pattern is a glob on the slug ('tcplclib_*', '*json*');
        category: plc-library | function-tf | engineering-te | twincat-system | hardware | other.
        TwinCAT 2 books are excluded. First call downloads the sitemap (a few MB, cached 7 days)."""
        return await app.docs_call(app.docs.books, pattern, category, limit, timeout=300)

    @mcp.tool(annotations=READ)
    async def infosys_index(book: str, force: bool = False, max_pages: int | None = None) -> dict[str, Any]:
        """Index a book (or glob of books, e.g. 'tcplclib_tc3_*') into the local cache: fetches every page
        that is new or changed since the last run (sitemap lastmod), extracts title/kind/library/
        declaration, builds the full-text index. Tc2_System ≈ 300 pages ≈ 30-60 s. Needed before
        infosys_search_symbols/search_code can see a library; infosys_search falls back to the web."""
        if any(c in book for c in "*?"):
            return await app.docs_call(app.docs.index_books, book, force, max_pages, timeout=3600)
        return await app.docs_call(app.docs.index_book, book, force, max_pages, timeout=1800)

    @mcp.tool(annotations=READ)
    async def infosys_search_symbols(query: str, kind: str | None = None, library: str | None = None,
                                     limit: int = 10) -> dict[str, Any]:
        """Find library symbols (function blocks, functions, structs, enums, aliases, interfaces,
        GVLs) by name fragment in the indexed books; exact > prefix > contains. kind: FunctionBlock |
        Function | Program | Method | Property | Action | Enum | Struct | Union | Alias | Interface |
        GlobalVariable | Library. library: 'Tc2_System' or the book slug. Indexes the default
        libraries on first use if nothing is indexed."""
        return await app.docs_call(app.docs.search_symbols, query, kind, library, limit, timeout=1800)

    @mcp.tool(annotations=READ)
    async def infosys_declaration(symbol: str, library: str | None = None) -> dict[str, Any]:
        """Declaration of a library symbol as data: kind, inputs/outputs/in_out (name, type, default,
        description from the page's table), struct members, enum values, return type, EXTENDS /
        IMPLEMENTS, plus the ST declaration text and the page URL. The thing to call before writing
        code against an FB you have not used before."""
        return await app.docs_call(app.docs.declaration, symbol, library, timeout=1800)

    @mcp.tool(annotations=READ)
    async def infosys_search(query: str, library: str | None = None, limit: int = 10,
                             web_fallback: bool | None = None) -> dict[str, Any]:
        """Full-text search over the indexed Infosys pages (FTS5, bm25-ranked, excerpt with [hits]).
        When the local index has nothing, falls back to a DuckDuckGo site: query whose results are
        verified against the Infosys sitemap (TwinCAT 3 only) and fetched for title/excerpt; the
        'backend' field says which path answered."""
        return await app.docs_call(app.docs.search, query, library, limit, web_fallback, timeout=300)

    @mcp.tool(annotations=READ)
    async def infosys_search_code(query: str, language: str | None = None, limit: int = 10) -> dict[str, Any]:
        """Search the code examples (<pre> blocks) of indexed pages for a fragment — a call pattern,
        an attribute, a constant. language: st | xml | cpp | csharp. Returns the matching lines
        with context and the page URL."""
        return await app.docs_call(app.docs.search_code, query, language, limit, timeout=120)

    @mcp.tool(annotations=READ)
    async def infosys_get_page(ref: str, refresh: bool = False, max_chars: int = 60000) -> dict[str, Any]:
        """A documentation page as Markdown (headings, tables, fenced code). ref: full Infosys URL,
        'book/pageid', a bare page id, or a symbol name ('ADSREAD'). Fetched live and cached; works
        for any page that exists on the site, indexed or not. refresh=true bypasses the cache."""
        return await app.docs_call(app.docs.get_page, ref, refresh, max_chars, timeout=600)

    # ======================================================================
    # exports for MES / HMI code, snapshots apply, XML validation, reports
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ads_export(types: list[str] | None = None, prefix: str | None = None, fmt: str = "csharp",
                         namespace: str = "Transflo.Plc", out_path: str | None = None) -> dict[str, Any]:
        """Generate code from the runtime: fmt=csharp (explicit-offset structs + enums for
        Beckhoff.TwinCAT.Ads ReadAny/WriteAny), typescript (interfaces), json (type descriptions),
        or csharp_symbols (const symbol paths under prefix). types = struct/enum names; prefix =
        take the types of every symbol under it."""
        ts = await app.ads_call(app.ads.typesystem, timeout=60)
        if ts is None and fmt != "csharp_symbols":
            raise ToolError("datatype table unavailable on this target")
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
            Path(out_path).write_text(text, encoding="utf-8")
            res["file"] = out_path
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
        the System Service, then reads the result file back. Not available on Windows CE / TC-BSD
        targets (use the XAE error list for ADS logger messages there). Destructive-class because
        it starts a process on the IPC."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_start_process", "wevtutil", confirm_token)
        out = r"C:\TwinCAT\3.1\Boot\tcmcp_eventlog.txt"
        q = f"*[System[Provider[@Name='{provider}']]]"
        cmd = f'/c wevtutil qe {log} /q:"{q}" /c:{int(count)} /rd:true /f:text > "{out}" 2>&1'
        await app.ads_call(app.files.start_process, r"C:\Windows\System32\cmd.exe", r"C:\Windows\System32", cmd, timeout=30)
        waited = await app.ads_call(app.files.wait_for_file, out, 20, 1.0, timeout=40, slow=True)
        if not waited.get("exists"):
            raise ToolError("wevtutil produced no output file (Windows CE/TC-BSD target, or no permission)")
        await asyncio.sleep(1.0)
        txt = await app.ads_call(app.files.read_file, out, 2_000_000, True, "utf-8", timeout=120, slow=True)
        app.audit("ipc_eventlog", {"provider": provider, "count": count}, OpClass.DESTRUCTIVE, "ok")
        return {"provider": provider, "log": log, "entries_text": txt["text"], "bytes": txt["bytes"], "file_on_target": out}

    @mcp.tool(annotations=READ)
    async def tc_session_report(out_path: str | None = None) -> dict[str, Any]:
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
        p = Path(out_path) if out_path else Path(cfg.safety.audit_log).parent / "reports" / f"{time.strftime('%Y%m%d-%H%M%S')}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return {"file": str(p), "mutating_calls": sum(1 for r in mine if r.get("op") != "read"), "markdown": text[:8000]}

    # ======================================================================
    # ipc_* target OS via System Service
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ipc_read_file(path: str, max_bytes: int | None = None, as_text: bool = True,
                            encoding: str = "utf-8") -> dict[str, Any]:
        r"""Read a file from the target IPC's file system over ADS (no share needed), e.g.
        'C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml' or a log. Binary → base64. OS-independent
        prefixes resolve on the target: 'boot:<name>' (TwinCAT boot folder — \Hard Disk\TwinCAT\3.1\Boot on
        CE7), 'bootprj:', 'bootdata:', 'target:', 'config:', 'install:', 'user1:'..'user9:'."""
        return await app.ads_call(app.files.read_file, path, max_bytes, as_text, encoding, timeout=120, slow=True)

    @mcp.tool(annotations=READ)
    async def ipc_list_dir(pattern: str, max_entries: int = 500) -> dict[str, Any]:
        r"""List files on the target matching a Win32 pattern, e.g. 'C:\TwinCAT\3.1\Boot\*' or 'boot:*'
        (boot folder on any target OS; see ipc_read_file for the other prefixes)."""
        return await app.ads_call(app.files.list_dir, pattern, max_entries, timeout=60)

    @mcp.tool(annotations=WRITE)
    async def ipc_write_file(path: str, text: str | None = None, base64_data: str | None = None,
                             append: bool = False, confirm_token: str | None = None,
                             ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write a file on the target IPC (text or base64). Control-class."""
        await app.permit(ctx, OpClass.CONTROL, "ipc_write_file", path, confirm_token)
        data = base64.b64decode(base64_data) if base64_data else (text or "").encode("utf-8")
        res = await app.ads_call(app.files.write_file, path, data, append, timeout=300)
        app.audit("ipc_write_file", {"path": path, "bytes": len(data)}, OpClass.CONTROL, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def ipc_delete_file(path: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Delete a file on the target IPC. Destructive-class."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_delete_file", path, confirm_token)
        res = await app.ads_call(app.files.delete_file, path, timeout=30)
        app.audit("ipc_delete_file", {"path": path}, OpClass.DESTRUCTIVE, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def ipc_start_process(path: str, directory: str = "", command_line: str = "",
                                confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Start a process on the target IPC (NT_StartProcess equivalent). Destructive-class."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_start_process", path, confirm_token)
        res = await app.ads_call(app.files.start_process, path, directory, command_line, timeout=30)
        app.audit("ipc_start_process", {"path": path, "cmd": command_line}, OpClass.DESTRUCTIVE, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def ipc_reboot(delay_s: int = 0, shutdown_only: bool = False, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Reboot (or shut down) the target IPC via the System Service. Destructive-class."""
        await app.permit(ctx, OpClass.DESTRUCTIVE, "ipc_reboot", "target", confirm_token)
        res = await app.ads_call(app.files.reboot, delay_s, not shutdown_only, timeout=30)
        app.audit("ipc_reboot", {"delay_s": delay_s, "shutdown_only": shutdown_only}, OpClass.DESTRUCTIVE, "ok", res)
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
        """Shell version, open solution, projects, TwinCAT project name, target NetId, started flag."""
        return await app.xae_call(app.xae.status, timeout=60)

    @mcp.tool(annotations=EDIT)
    async def xae_open_solution(path: str) -> dict[str, Any]:
        """Open a .sln in the attached shell."""
        await app.permit(None, OpClass.EDIT, "xae_open_solution", path)
        res = await app.xae_call(app.xae.open_solution, path, timeout=180)
        app.audit("xae_open_solution", {"path": path}, OpClass.EDIT, "ok")
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_build(clean: bool = False, rebuild: bool = False, save_first: bool = True,
                        check_all_objects: bool = False) -> dict[str, Any]:
        """Build the solution (File.SaveAll first by default). Returns success, error/warning
        items with file:line (VS error list, or parsed from the Build output pane) and the output
        tail. check_all_objects=true runs PLC 'Check all objects' instead (compiles unused POUs)."""
        await app.permit(None, OpClass.EDIT, "xae_build")
        if save_first:
            await app.xae_call(app.xae.save_all, timeout=60)
        if check_all_objects:
            return await app.xae_call(app.xae.check_all_objects, timeout=600)
        res = await app.xae_call(app.xae.build, clean, rebuild, timeout=cfg.xae.build_timeout_s + 60)
        app.audit("xae_build", {"clean": clean, "rebuild": rebuild}, OpClass.EDIT,
                  "ok" if res.get("success") else "build-failed", {"errors": res.get("errors")})
        return res

    @mcp.tool(annotations=READ)
    async def xae_diagnostics(tail_lines: int = 200) -> dict[str, Any]:
        """Current error list (errors/warnings/messages with file:line) plus the tail of the
        Build output pane, without rebuilding."""
        errs = await app.xae_call(app.xae.errors, timeout=60)
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
        await app.permit(ctx, op, "xae_command", name, confirm_token)
        res = await app.xae_call(app.xae.command, name, args, timeout=120)
        app.audit("xae_command", {"name": name, "args": args}, op, "ok")
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_plc(action: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """PLC online actions through the shell: login | logout | start | stop | reset_cold |
        reset_origin | download. Command names vary by shell version; all candidates are tried.
        download/reset_cold/reset_origin are destructive-class, the rest control-class."""
        op = OpClass.DESTRUCTIVE if action in ("download", "reset_origin", "reset_cold") else OpClass.CONTROL
        await app.permit(ctx, op, "xae_plc", action, confirm_token)
        res = await app.xae_call(app.xae.plc_action, action, timeout=300)
        app.audit("xae_plc", {"action": action}, op, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_target(net_id: str | None = None, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Get (net_id=None) or set the target AMS NetId of the TwinCAT project. Setting is
        control-class."""
        if net_id is None:
            return await app.xae_call(app.xae.target_get, timeout=60)
        await app.permit(ctx, OpClass.CONTROL, "xae_target", net_id, confirm_token)
        res = await app.xae_call(app.xae.target_set, net_id, timeout=60)
        app.audit("xae_target", {"net_id": net_id}, OpClass.CONTROL, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_activate_configuration(confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """ActivateConfiguration on the current target (overwrites the boot project on the IPC).
        Destructive-class. Follow with xae_restart_twincat."""
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_activate_configuration", tgt, confirm_token)
        res = await app.xae_call(app.xae.activate_configuration, timeout=600)
        await app.ads_call(app.ads.disconnect, timeout=10)
        app.audit("xae_activate_configuration", {"target": tgt}, OpClass.DESTRUCTIVE, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_restart_twincat(confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """StartRestartTwinCAT on the current target (runtime restarts in RUN mode). Destructive-class."""
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_restart_twincat", tgt, confirm_token)
        res = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        app.audit("xae_restart_twincat", {"target": tgt}, OpClass.DESTRUCTIVE, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_deploy(skip_build: bool = False, plc_project_path: str | None = None,
                         allow_target_mismatch: bool = False, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Save → build → (boot project autostart on) → activate → restart, in one call.
        Stops at the first build error. Destructive-class (one confirmation covers all steps).
        Refuses when the XAE target and the ADS target of this server differ."""
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
        await app.check_targets(tgt, allow_target_mismatch)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_deploy", tgt, confirm_token)
        steps: dict[str, Any] = {}
        steps["save"] = await app.xae_call(app.xae.save_all, timeout=60)
        if not skip_build:
            b = await app.xae_call(app.xae.build, False, False, timeout=cfg.xae.build_timeout_s + 60)
            steps["build"] = {k: b[k] for k in ("success", "errors", "warnings", "duration_s")}
            if not b["success"]:
                steps["build"]["items"] = b["items"][:50]
                app.audit("xae_deploy", {"target": tgt}, OpClass.DESTRUCTIVE, "build-failed", steps)
                return {"deployed": False, "steps": steps}
        if plc_project_path:
            steps["boot_project"] = await app.xae_call(app.xae.boot_project, plc_project_path, True, True, False, timeout=300)
        steps["activate"] = await app.xae_call(app.xae.activate_configuration, timeout=600)
        steps["restart"] = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        app.audit("xae_deploy", {"target": tgt}, OpClass.DESTRUCTIVE, "ok", steps)
        return {"deployed": True, "target": tgt, "steps": steps}

    # ---- tree ------------------------------------------------------------
    @mcp.tool(annotations=READ)
    async def xae_tree_get(path: str, depth: int = 0, xml: bool = False, max_items: int = 2000) -> dict[str, Any]:
        """System Manager tree item: properties, children to `depth`, and ProduceXml when xml=true.
        Path syntax 'TIPC^Plc^Plc Project^POUs^MAIN'. Roots: TIID (I/O), TIPC (PLC), TIRC
        (real-time/tasks), TIRR (routes), TINC (NC)."""
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
        res = await app.xae_call(app.xae.tree_consume_xml, path, xml, timeout=300)
        app.audit("xae_tree_consume_xml", {"path": path, "xml_bytes": len(xml)}, OpClass.EDIT, "ok")
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_tree_set_values(path: str, assignments: dict[str, str]) -> dict[str, Any]:
        """Set element texts in a tree item's XML by relative XPath, e.g.
        {'TaskDef/CycleTime': '10000'} (unit 100 ns)."""
        app.guard_safety_path(path, "xae_tree_set_values")
        await app.permit(None, OpClass.EDIT, "xae_tree_set_values", path)
        res = await app.xae_call(app.xae.tree_set_values, path, assignments, timeout=300)
        app.audit("xae_tree_set_values", {"path": path, "assignments": assignments}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_tree_create_child(parent_path: str, name: str, subtype: int, before: str = "",
                                    info: Any = None) -> dict[str, Any]:
        """Generic CreateChild (tree item types/subtypes per Infosys). For PLC objects prefer
        xae_pou_create."""
        app.guard_safety_path(parent_path, "xae_tree_create_child")
        await app.permit(None, OpClass.EDIT, "xae_tree_create_child", f"{parent_path}^{name}")
        res = await app.xae_call(app.xae.tree_create_child, parent_path, name, subtype, before, info, timeout=120)
        app.audit("xae_tree_create_child", {"parent": parent_path, "name": name, "subtype": subtype}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_tree_delete(parent_path: str, name: str, confirm_token: str | None = None,
                              ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Delete a tree item (POU, box, task …). Destructive-class."""
        tgt = f"{parent_path}^{name}"
        app.guard_safety_path(parent_path, "xae_tree_delete")
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_tree_delete", tgt, confirm_token)
        res = await app.xae_call(app.xae.tree_delete_child, parent_path, name, timeout=120)
        app.audit("xae_tree_delete", {"target": tgt}, OpClass.DESTRUCTIVE, "ok", res)
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
        res = await app.xae_call(app.xae.pou_set, path, declaration, implementation, timeout=120)
        app.audit("xae_pou_set", {"path": path, "decl": declaration is not None, "impl": implementation is not None}, OpClass.EDIT, "ok", res)
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
        app.audit("xae_pou_create", {"parent": parent_path, "name": name, "kind": kind}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_plcopen_import(plc_project_path: str, xml_file: str, option: str = "none") -> dict[str, Any]:
        """Import a PLCopen XML file into a PLC project (path from xae_plc_projects).
        option: none | rename | replace | skip (conflict handling)."""
        app.guard_safety_path(plc_project_path, "xae_plcopen_import")
        await app.permit(None, OpClass.EDIT, "xae_plcopen_import", xml_file)
        res = await app.xae_call(app.xae.plcopen_import, plc_project_path, xml_file, option, timeout=300)
        app.audit("xae_plcopen_import", {"project": plc_project_path, "file": xml_file, "option": option}, OpClass.EDIT, "ok", res)
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
        await app.permit(ctx, op, "xae_boot_project", plc_project_path, confirm_token)
        res = await app.xae_call(app.xae.boot_project, plc_project_path, autostart, generate, activate_after_generate, timeout=300)
        app.audit("xae_boot_project", {"project": plc_project_path, "autostart": autostart, "generate": generate}, op, "ok", res)
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
        declared, following variable types through FB/STRUCT definitions."""
        return await app.proj_call(app.proj.resolve_symbol, symbol)

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
        res = await app.proj_call(app.proj.set_code, name, declaration, implementation, strip_lineids, regenerate_lineids)
        app.audit("proj_set_code", {"name": name, "changed": res.get("changed")}, OpClass.EDIT, "ok", res)
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
                         disable_io: bool = False, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Full TcUnit cycle: build → boot project autostart+generate → activate → restart →
        delete old result → wait for the xUnit file on the target → parse. Destructive-class
        (it activates a configuration on the target). plc_project_path from xae_plc_projects."""
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
        rp = remote_result_path or cfg.project.tcunit_remote_result
        if not rp.lower().endswith(".xml"):
            raise ToolError("remote_result_path must be the TcUnit xUnit .xml file")
        await app.check_targets(tgt, allow_target_mismatch)
        await app.permit(ctx, OpClass.DESTRUCTIVE, "tcunit_run", f"{tgt} {rp}", confirm_token)
        steps: dict[str, Any] = {"target": tgt}
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
        waited = await app.ads_call(app.files.wait_for_file, rp, timeout_s, 3.0, timeout=timeout_s + 30, slow=True)
        steps["wait"] = waited
        if not waited.get("exists"):
            app.audit("tcunit_run", {"target": tgt}, OpClass.DESTRUCTIVE, "timeout", steps)
            return {"ran": True, "result": None, "steps": steps,
                    "note": "result file never appeared — is the TcUnit task running and the "
                            "result path right? (default C:\\TwinCAT\\3.1\\Boot\\TcUnit_xUnit_testresults.xml)"}
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
    return mcp
