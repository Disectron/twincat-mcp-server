"""MCP server wiring: tools, resources, safety plumbing.

Tool prefixes
  tc_*     meta / safety / diagnostics
  ads_*    runtime (target IPC) over ADS
  ipc_*    target file system / OS via the TwinCAT System Service
  xae_*    TwinCAT XAE Automation Interface (engineering PC)
  proj_*   offline project files
  tcunit_* TcUnit orchestration + result parsing

Confirmation for destructive operations: if the MCP client supports
elicitation, the human is asked directly (``ctx.elicit``); otherwise the
two-step confirm_token flow applies (``tc_request_confirmation``).
"""
from __future__ import annotations

import asyncio
import base64
import json
import platform
import re
import sys
import time
from typing import Any

from pydantic import BaseModel, Field

from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .config import Config, load as load_config, is_windows
from .safety import Guard, OpClass, SafetyError
from .worker import SerialWorker
from .ads_backend import AdsBackend, AdsError
from .ads_files import SystemServiceFiles
from .xae_backend import XaeBackend, XaeError, sta_init, sta_teardown
from .project_backend import ProjectIndex, ProjectError, parse_tcunit_xml
from .recorder import Recorder, RecorderError
from .testing import TestBench, TestError
from . import codecheck
from pathlib import Path

INSTRUCTIONS = """\
twincat-mcp: control of a Beckhoff TwinCAT 3 engineering PC and its target IPC.

Workflow for debugging a running machine:
  1. tc_doctor → confirms ADS router, XAE shell, project root, safety mode.
  2. ads_status → PLC state, TwinCAT run/config mode, device info.
  3. ads_list_symbols / ads_read / ads_read_many → inspect live values
     (structs and arrays are expanded, TIME/DATE decoded).
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
        self.ads_worker = SerialWorker("tcmcp-ads")
        self.xae = XaeBackend(cfg.xae)
        self.xae_worker = SerialWorker("tcmcp-xae-sta", init=sta_init, teardown=sta_teardown)
        self.proj = ProjectIndex(cfg.project)
        self.rec = Recorder(self.ads, Path(cfg.safety.audit_log).parent / "recordings")
        self.bench = TestBench(self.ads, self.rec, Path(cfg.safety.audit_log).parent / "snapshots")

    # --- runners -----------------------------------------------------------
    async def ads_call(self, fn, *a, timeout: float | None = None, **kw) -> Any:
        try:
            return await self.ads_worker.run(fn, *a, timeout=timeout or (self.cfg.ads.max_watch_duration_ms / 1000 + 30), **kw)
        except (AdsError, RecorderError, TestError) as exc:
            raise ToolError(str(exc)) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ToolError("ADS call timed out (router/route/firewall? run tc_doctor)") from exc

    async def xae_call(self, fn, *a, timeout: float | None = None, **kw) -> Any:
        try:
            return await self.xae_worker.run(fn, *a, timeout=timeout or self.cfg.xae.build_timeout_s + 60, **kw)
        except XaeError as exc:
            raise ToolError(str(exc)) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise ToolError("XAE call timed out; a modal dialog in the shell is the usual cause") from exc

    async def proj_call(self, fn, *a, **kw) -> Any:
        try:
            return await asyncio.to_thread(fn, *a, **kw)
        except ProjectError as exc:
            raise ToolError(str(exc)) from exc

    async def rec_call(self, fn, *a, timeout: float = 60, **kw) -> Any:
        return await self.ads_call(fn, *a, timeout=timeout, **kw)

    def scope(self) -> str:
        return self.ads.target_label()

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
                               offset: int = 0, refresh: bool = False, flag: str | None = None) -> dict[str, Any]:
        """Browse the runtime symbol table. pattern is fnmatch ('MAIN.*', '*valve*'; bare text
        is wrapped in '*'). type_filter substring-matches the type ('ST_', 'BOOL', 'FB_').
        flag filters by symbol flag: PERSISTENT, READONLY, STATIC, ATTRIBUTES …"""
        res = await app.ads_call(app.ads.list_symbols, pattern, 100_000 if flag else min(limit, 2000), 0 if flag else offset, refresh, type_filter, timeout=60)
        if flag:
            fl = flag.upper()
            sel = [x for x in res["symbols"] if fl in x["flags"]]
            res = {"total": len(sel), "offset": offset, "returned": len(sel[offset:offset + limit]), "symbols": sel[offset:offset + limit]}
        return res

    @mcp.tool(annotations=READ)
    async def ads_symbol_info(name: str) -> dict[str, Any]:
        """Type, size, index group/offset, flags (READONLY, PERSISTENT …) and comment of one symbol."""
        s = await app.ads_call(app.ads.symbol_info, name, timeout=30)
        return s.to_dict()

    @mcp.tool(annotations=READ)
    async def ads_read(name: str, expand_struct: bool = True, max_members: int = 200) -> dict[str, Any]:
        """Read one symbol with type-aware decoding. Structs/FB instances are expanded into
        their members; arrays are nested lists; TIME/DATE/DT/TOD get readable text."""
        return await app.ads_call(app.ads.read, name, expand_struct, max_members, timeout=60)

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
                                  timeout=cfg.ads.max_watch_duration_ms / 1000 + 30)

    @mcp.tool(annotations=READ)
    async def ads_wait_until(name: str, op: str, value: Any, timeout_ms: int = 10000,
                             interval_ms: int = 50) -> dict[str, Any]:
        """Block until symbol <op> value (==, !=, >, >=, <, <=) or timeout. Returns met/elapsed."""
        return await app.ads_call(app.ads.wait_until, name, op, value, timeout_ms, interval_ms,
                                  timeout=min(timeout_ms, 600_000) / 1000 + 10)

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

    @mcp.tool(annotations=READ)
    async def rec_list() -> dict[str, Any]:
        """Recordings of this session (live status) and recording files on disk."""
        return await app.rec_call(app.rec.list)

    @mcp.tool(annotations=READ)
    async def rec_status(id: str) -> dict[str, Any]:
        """Live status of one recording: events, rate, drops, armed/triggered, last value per symbol."""
        return await app.rec_call(app.rec.status, id)

    @mcp.tool(annotations=READ)
    async def rec_snapshot(id: str, last_n: int = 100, symbols: list[str] | None = None) -> dict[str, Any]:
        """Most recent events of a running (or finished) recording — the live tail."""
        return await app.rec_call(app.rec.snapshot, id, min(last_n, 5000), symbols)

    @mcp.tool(annotations=READ)
    async def rec_stats(id: str, symbols: list[str] | None = None, include_runs: bool = True,
                        max_runs: int = 200) -> dict[str, Any]:
        """Per-symbol statistics over the whole recording so far: count, first/last, min/max/mean,
        change count, BOOL rising/falling edges with timestamps, and value runs (state timeline
        with dwell times) — computed incrementally, no re-read of the file."""
        return await app.rec_call(app.rec.stats, id, symbols, include_runs, min(max_runs, 5000))

    @mcp.tool(annotations=READ)
    async def rec_read(id: str, t_from_ms: float | None = None, t_to_ms: float | None = None,
                       symbols: list[str] | None = None, max_rows: int = 2000, wide: bool = False) -> dict[str, Any]:
        """Read a time window of a recording from its file. wide=true pivots to one row per event
        with all selected symbols forward-filled (what you want for 'what else was true when X
        happened'). Works on finished recordings from earlier sessions too."""
        return await app.rec_call(app.rec.read, id, t_from_ms, t_to_ms, symbols, min(max_rows, 50_000), wide, timeout=300)

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
        return await app.rec_call(app.rec.export, id, fmt, out_path, symbols, timeout=600)

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
        return await app.ads_call(app.bench.expect, checks, within_ms, interval_ms, timeout=within_ms / 1000 + 30)

    @mcp.tool(annotations=READ)
    async def test_stable(symbol: str, duration_ms: int = 1000, tolerance: float = 0.0,
                          interval_ms: int = 20) -> dict[str, Any]:
        """Assert a value does not move more than tolerance for duration_ms (settle check for a
        weight, pressure, PID output, state)."""
        return await app.ads_call(app.bench.assert_stable, symbol, duration_ms, tolerance, interval_ms, timeout=duration_ms / 1000 + 30)

    @mcp.tool(annotations=WRITE)
    async def test_response(stimulus: dict[str, Any], response: str, timeout_ms: int = 5000,
                            restore: bool = True, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Write a stimulus ({symbol: value}) and measure the PLC-timestamped latency until
        `response` ('MAIN.fbValve.bOpen == TRUE') holds — how fast does the FB react? Writes are
        gated per symbol; originals are restored afterwards unless restore=false."""
        for k in stimulus:
            await app.permit(ctx, OpClass.WRITE, "ads_write", k)
        res = await app.ads_call(app.bench.measure_response, stimulus, response, timeout_ms, restore, timeout=timeout_ms / 1000 + 60)
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
        res = await app.ads_call(app.bench.scenario, spec, timeout=timeout)
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
        return await app.ads_call(app.bench.task_stats, task_index, min(samples, 1000), interval_ms, timeout=samples * interval_ms / 1000 + 30)

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
        return await app.rec_call(app.rec.pulses, id, symbol, max_list, timeout=300)

    @mcp.tool(annotations=READ)
    async def rec_latency(id: str, from_symbol: str, from_op: str, from_value: Any, to_symbol: str,
                          to_op: str, to_value: Any, max_pairs: int = 500) -> dict[str, Any]:
        """Edge-to-edge latency in a recording: from each time `from_symbol from_op from_value`
        becomes true to the next time `to_symbol to_op to_value` becomes true (PLC timestamps).
        E.g. bOpenCmd == TRUE → rFlow > 0.5."""
        return await app.rec_call(app.rec.latency, id, from_symbol, from_op, from_value, to_symbol, to_op, to_value, max_pairs, timeout=300)

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
        qualified_only, magic numbers, FB_init/FB_exit without SUPER^. rules= subset to run."""
        return await app.proj_call(codecheck.lint, app.proj, name, rules, prefixes)

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
    # ipc_* target OS via System Service
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def ipc_read_file(path: str, max_bytes: int | None = None, as_text: bool = True,
                            encoding: str = "utf-8") -> dict[str, Any]:
        r"""Read a file from the target IPC's file system over ADS (no share needed), e.g.
        'C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml' or a log. Binary → base64."""
        return await app.ads_call(app.files.read_file, path, max_bytes, as_text, encoding, timeout=120)

    @mcp.tool(annotations=READ)
    async def ipc_list_dir(pattern: str, max_entries: int = 500) -> dict[str, Any]:
        r"""List files on the target matching a Win32 pattern, e.g. 'C:\TwinCAT\3.1\Boot\*'."""
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
        await app.permit(None, OpClass.EDIT, "xae_tree_consume_xml", path)
        res = await app.xae_call(app.xae.tree_consume_xml, path, xml, timeout=300)
        app.audit("xae_tree_consume_xml", {"path": path, "xml_bytes": len(xml)}, OpClass.EDIT, "ok")
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_tree_set_values(path: str, assignments: dict[str, str]) -> dict[str, Any]:
        """Set element texts in a tree item's XML by relative XPath, e.g.
        {'TaskDef/CycleTime': '10000'} (unit 100 ns)."""
        await app.permit(None, OpClass.EDIT, "xae_tree_set_values", path)
        res = await app.xae_call(app.xae.tree_set_values, path, assignments, timeout=300)
        app.audit("xae_tree_set_values", {"path": path, "assignments": assignments}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_tree_create_child(parent_path: str, name: str, subtype: int, before: str = "",
                                    info: Any = None) -> dict[str, Any]:
        """Generic CreateChild (tree item types/subtypes per Infosys). For PLC objects prefer
        xae_pou_create."""
        await app.permit(None, OpClass.EDIT, "xae_tree_create_child", f"{parent_path}^{name}")
        res = await app.xae_call(app.xae.tree_create_child, parent_path, name, subtype, before, info, timeout=120)
        app.audit("xae_tree_create_child", {"parent": parent_path, "name": name, "subtype": subtype}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_tree_delete(parent_path: str, name: str, confirm_token: str | None = None,
                              ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Delete a tree item (POU, box, task …). Destructive-class."""
        tgt = f"{parent_path}^{name}"
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
        await app.permit(None, OpClass.EDIT, "xae_pou_create", f"{parent_path}^{name}")
        res = await app.xae_call(app.xae.pou_create, parent_path, name, kind, language, return_type,
                                 extends, implements, access, declaration, implementation, timeout=120)
        app.audit("xae_pou_create", {"parent": parent_path, "name": name, "kind": kind}, OpClass.EDIT, "ok", res)
        return res

    @mcp.tool(annotations=EDIT)
    async def xae_plcopen_import(plc_project_path: str, xml_file: str, option: str = "none") -> dict[str, Any]:
        """Import a PLCopen XML file into a PLC project (path from xae_plc_projects).
        option: none | rename | replace | skip (conflict handling)."""
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
        await app.permit(ctx, op, "xae_boot_project", plc_project_path, confirm_token)
        res = await app.xae_call(app.xae.boot_project, plc_project_path, autostart, generate, activate_after_generate, timeout=300)
        app.audit("xae_boot_project", {"project": plc_project_path, "autostart": autostart, "generate": generate}, op, "ok", res)
        return res

    # ======================================================================
    # proj_* offline files
    # ======================================================================
    @mcp.tool(annotations=READ)
    async def proj_scan(root: str | None = None) -> dict[str, Any]:
        """Index the project tree (.sln/.tsproj/.plcproj/.TcPOU/.TcDUT/.TcGVL/.TcIO). Re-run
        after external edits."""
        return await app.proj_call(app.proj.scan, root)

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
                            strip_lineids: bool = True) -> dict[str, Any]:
        """Write declaration/implementation back into the .TcPOU/.TcDUT/.TcGVL file (keeps the
        rest of the XML byte-identical, writes a .bak, drops the object's stale <LineIds>).
        Use when XAE is closed or for batch edits; otherwise prefer xae_pou_set."""
        await app.permit(None, OpClass.EDIT, "proj_set_code", name)
        res = await app.proj_call(app.proj.set_code, name, declaration, implementation, strip_lineids)
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
                         confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
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
        steps["boot_project"] = await app.xae_call(app.xae.boot_project, plc_project_path, True, True, False, timeout=300)
        steps["activate"] = await app.xae_call(app.xae.activate_configuration, timeout=600)
        steps["restart"] = await app.xae_call(app.xae.restart_twincat, timeout=300)
        await app.ads_call(app.ads.disconnect, timeout=10)
        t0 = time.time()
        waited = await app.ads_call(app.files.wait_for_file, rp, timeout_s, 3.0, timeout=timeout_s + 30)
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
    mcp._tcmcp_app = app  # type: ignore[attr-defined]  (tests)
    return mcp
