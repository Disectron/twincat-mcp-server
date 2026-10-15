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

import base64
import json
import platform
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

INSTRUCTIONS = """\
twincat-mcp: control of a Beckhoff TwinCAT 3 engineering PC and its target IPC.

Workflow for debugging a running machine:
  1. tc_doctor → confirms ADS router, XAE shell, project root, safety mode.
  2. ads_status → PLC state, TwinCAT run/config mode, device info.
  3. ads_list_symbols / ads_read / ads_read_many → inspect live values
     (structs and arrays are expanded, TIME/DATE decoded).
  4. ads_watch / ads_wait_until → capture timing, catch transient states.
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

    # --- runners -----------------------------------------------------------
    async def ads_call(self, fn, *a, timeout: float | None = None, **kw) -> Any:
        try:
            return await self.ads_worker.run(fn, *a, timeout=timeout or (self.cfg.ads.max_watch_duration_ms / 1000 + 30), **kw)
        except AdsError as exc:
            raise ToolError(str(exc)) from exc
        except TimeoutError as exc:
            raise ToolError("ADS call timed out (router/route/firewall? run tc_doctor)") from exc

    async def xae_call(self, fn, *a, timeout: float | None = None, **kw) -> Any:
        try:
            return await self.xae_worker.run(fn, *a, timeout=timeout or self.cfg.xae.build_timeout_s + 60, **kw)
        except XaeError as exc:
            raise ToolError(str(exc)) from exc
        except TimeoutError as exc:
            raise ToolError("XAE call timed out; a modal dialog in the shell is the usual cause") from exc

    def proj_call(self, fn, *a, **kw) -> Any:
        try:
            return fn(*a, **kw)
        except ProjectError as exc:
            raise ToolError(str(exc)) from exc

    # --- safety ------------------------------------------------------------
    async def permit(self, ctx: Context | None, op: OpClass, action: str, target: str = "",
                     confirm_token: str | None = None) -> None:
        """Check the guard; for CONTROL/DESTRUCTIVE without a token, try to
        ask the human through elicitation before failing."""
        try:
            self.guard.check(op, action=action, target=target, confirm_token=confirm_token)
            return
        except SafetyError as exc:
            needs_token = "confirm_token" in str(exc) and op in (OpClass.CONTROL, OpClass.DESTRUCTIVE)
            if not needs_token or ctx is None:
                raise ToolError(str(exc)) from exc
        # elicitation path
        try:
            res = await ctx.elicit(
                f"twincat-mcp wants to run {action} on {target or 'the target'} "
                f"(mode={self.guard.mode}). Allow?", ConfirmSchema)
        except Exception as exc:  # noqa: BLE001 — client has no elicitation support
            raise ToolError(
                f"{action} on {target!r} needs confirmation and this client does not support "
                f"elicitation ({type(exc).__name__}). Call tc_request_confirmation(action={action!r}, "
                f"target={target!r}) and pass the confirm_token."
            ) from exc
        if res.action == "accept" and res.data is not None and res.data.confirm:
            self.guard.audit(action, {"target": target}, op, "human-confirmed-elicitation")
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
        return app.guard.request_token(action, target)

    @mcp.tool(annotations=READ)
    async def tc_audit_tail(n: int = 50) -> list[dict[str, Any]]:
        """Last n audit records (mutating calls, confirmations, mode changes)."""
        return app.guard.tail_audit(max(1, min(n, 1000)))

    # ======================================================================
    # ads_* runtime
    # ======================================================================
    @mcp.tool(annotations=EDIT)
    async def ads_connect(ams_net_id: str | None = None, ams_port: int | None = None,
                          ip_address: str | None = None) -> dict[str, Any]:
        """(Re)connect to a target. ams_net_id like '5.12.34.56.1.1' (None = local), port 851
        = PLC runtime 1. The route must already exist (ads_routes)."""
        return await app.ads_call(app.ads.connect, ams_net_id, ams_port, ip_address, timeout=20)

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
                               offset: int = 0, refresh: bool = False) -> dict[str, Any]:
        """Browse the runtime symbol table. pattern is fnmatch ('MAIN.*', '*valve*'; bare text
        is wrapped in '*'). type_filter substring-matches the type ('ST_', 'BOOL', 'FB_')."""
        return await app.ads_call(app.ads.list_symbols, pattern, min(limit, 2000), offset, refresh, type_filter, timeout=60)

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
        """Raw ADS write by index group/offset. Target string for the allowlist is
        'raw:<port>/<igrp hex>/<ioffs hex>'."""
        tgt = f"raw:{port}/0x{index_group:X}/0x{index_offset:X}"
        await app.permit(ctx, OpClass.WRITE, "ads_write_raw", tgt)
        res = await app.ads_call(app.ads.write_raw, index_group, index_offset, hex_data, port, ams_net_id, timeout=30)
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
        """Execute an arbitrary DTE command (e.g. 'PLC.Login'). Control-class."""
        await app.permit(ctx, OpClass.CONTROL, "xae_command", name, confirm_token)
        res = await app.xae_call(app.xae.command, name, args, timeout=120)
        app.audit("xae_command", {"name": name, "args": args}, OpClass.CONTROL, "ok")
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_plc(action: str, confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """PLC online actions through the shell: login | logout | start | stop | reset_cold |
        reset_origin | download. Command names vary by shell version; all candidates are tried.
        download/reset_origin are destructive-class, the rest control-class."""
        op = OpClass.DESTRUCTIVE if action in ("download", "reset_origin") else OpClass.CONTROL
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
        app.audit("xae_activate_configuration", {"target": tgt}, OpClass.DESTRUCTIVE, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_restart_twincat(confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """StartRestartTwinCAT on the current target (runtime restarts in RUN mode). Destructive-class."""
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
        await app.permit(ctx, OpClass.DESTRUCTIVE, "xae_restart_twincat", tgt, confirm_token)
        res = await app.xae_call(app.xae.restart_twincat, timeout=300)
        app.audit("xae_restart_twincat", {"target": tgt}, OpClass.DESTRUCTIVE, "ok", res)
        return res

    @mcp.tool(annotations=DESTR)
    async def xae_deploy(skip_build: bool = False, plc_project_path: str | None = None,
                         confirm_token: str | None = None, ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Save → build → (boot project autostart on) → activate → restart, in one call.
        Stops at the first build error. Destructive-class (one confirmation covers all steps)."""
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
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
        app.ads.disconnect()
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

    @mcp.tool(annotations=READ)
    async def xae_plcopen_export(plc_project_path: str, items: list[str], xml_file: str) -> dict[str, Any]:
        """Export PLC objects ('POUs.FB_X', 'DUTs.ST_Y' — folder.object) to a PLCopen XML file."""
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
        return app.proj_call(app.proj.scan, root)

    @mcp.tool(annotations=READ)
    async def proj_list(kind: str | None = None, pattern: str | None = None, limit: int = 500) -> dict[str, Any]:
        """List indexed objects. kind: PROGRAM | FUNCTION_BLOCK | FUNCTION | STRUCT | ENUM |
        UNION | ALIAS | GVL | INTERFACE | METHOD | PROPERTY | ACTION (case-insensitive)."""
        return app.proj_call(app.proj.list_units, kind, pattern, limit)

    @mcp.tool(annotations=READ)
    async def proj_get_code(name: str, part: str = "both", with_line_numbers: bool = True) -> dict[str, Any]:
        """Source of an object ('FB_Valve', 'FB_Valve.Open', 'MAIN'): declaration/implementation
        with file line numbers, parsed VAR declarations, sub-items."""
        return app.proj_call(app.proj.get_code, name, part, with_line_numbers)

    @mcp.tool(annotations=READ)
    async def proj_resolve_symbol(symbol: str) -> dict[str, Any]:
        """Map an ADS symbol path (MAIN.fbFill.fbValve.bOpen) to the file:line where each hop is
        declared, following variable types through FB/STRUCT definitions."""
        return app.proj_call(app.proj.resolve_symbol, symbol)

    @mcp.tool(annotations=READ)
    async def proj_search(regex: str, kinds: list[str] | None = None, max_hits: int = 200,
                          ignore_case: bool = True) -> dict[str, Any]:
        """Regex search over all declarations/implementations → unit, file, line, text."""
        return app.proj_call(app.proj.search, regex, kinds, max_hits, ignore_case)

    @mcp.tool(annotations=READ)
    async def proj_references(name: str, max_hits: int = 200) -> dict[str, Any]:
        """Where a type/object is instantiated (variables of that type) and mentioned."""
        return app.proj_call(app.proj.references, name, max_hits)

    @mcp.tool(annotations=READ)
    async def proj_read_file(path: str, max_chars: int = 200000) -> dict[str, Any]:
        """Raw text of a project file (relative to project root or absolute)."""
        return app.proj_call(app.proj.read_file, path, max_chars)

    @mcp.tool(annotations=EDIT)
    async def proj_set_code(name: str, declaration: str | None = None, implementation: str | None = None,
                            strip_lineids: bool = True) -> dict[str, Any]:
        """Write declaration/implementation back into the .TcPOU/.TcDUT/.TcGVL file (keeps the
        rest of the XML byte-identical, writes a .bak, drops the object's stale <LineIds>).
        Use when XAE is closed or for batch edits; otherwise prefer xae_pou_set."""
        await app.permit(None, OpClass.EDIT, "proj_set_code", name)
        res = app.proj_call(app.proj.set_code, name, declaration, implementation, strip_lineids)
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
            txt = app.proj_call(app.proj.read_file, local_path)["text"]
            src = local_path
        elif xml_text:
            txt, src = xml_text, "<text>"
        else:
            src = remote_path or cfg.project.tcunit_remote_result
            txt = (await app.ads_call(app.files.read_file, src, None, True, "utf-8", timeout=120))["text"]
        res = app.proj_call(parse_tcunit_xml, txt)
        res["source"] = src
        return res

    @mcp.tool(annotations=DESTR)
    async def tcunit_run(plc_project_path: str, timeout_s: int = 600, skip_build: bool = False,
                         remote_result_path: str | None = None, confirm_token: str | None = None,
                         ctx: Context = None) -> dict[str, Any]:  # type: ignore[assignment]
        """Full TcUnit cycle: build → boot project autostart+generate → activate → restart →
        delete old result → wait for the xUnit file on the target → parse. Destructive-class
        (it activates a configuration on the target). plc_project_path from xae_plc_projects."""
        tgt = (await app.xae_call(app.xae.target_get, timeout=60)).get("target_net_id", "")
        await app.permit(ctx, OpClass.DESTRUCTIVE, "tcunit_run", tgt, confirm_token)
        rp = remote_result_path or cfg.project.tcunit_remote_result
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
        app.ads.disconnect()
        t0 = time.time()
        waited = await app.ads_call(app.files.wait_for_file, rp, timeout_s, 3.0, timeout=timeout_s + 30)
        steps["wait"] = waited
        if not waited.get("exists"):
            app.audit("tcunit_run", {"target": tgt}, OpClass.DESTRUCTIVE, "timeout", steps)
            return {"ran": True, "result": None, "steps": steps,
                    "note": "result file never appeared — is the TcUnit task running and the "
                            "result path right? (default C:\\TwinCAT\\3.1\\Boot\\TcUnit_xUnit_testresults.xml)"}
        # TcUnit writes the file incrementally; give it a moment to finish
        time.sleep(2.0)
        f = await app.ads_call(app.files.read_file, rp, None, True, "utf-8", timeout=120)
        res = app.proj_call(parse_tcunit_xml, f["text"])
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

    mcp._tcmcp_app = app  # type: ignore[attr-defined]  (tests)
    return mcp
