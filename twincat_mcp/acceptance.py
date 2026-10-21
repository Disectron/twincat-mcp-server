"""Acceptance run: exercise every tool the server exposes against the real engineering PC and target,
without changing the machine, and write a pass/fail report per tool.

    python -m twincat_mcp.acceptance                       # read-only, from the solution folder
    python -m twincat_mcp.acceptance --allow-writes --write-symbol GVL_Test.nScratch
    python -m twincat_mcp.acceptance --no-xae --no-infosys --out C:\\temp\\acceptance

What it does
* starts the server in-process with the same configuration the plugin would use (TCMCP_* env,
  ./twincat_mcp.toml) and talks to it through a real MCP client over in-memory streams — the same
  path Claude Code uses, so a tool that passes here is callable from Claude Code;
* discovers inputs from the target itself (a scalar symbol, a BOOL, a struct type, the root program);
* runs the read-only tools, the recorder round trip (a 2 s profile on two symbols), the snapshot
  and expectation tools, the experiment layer in dry-run, the documentation lookup if the web is
  reachable, and the XAE read-only tools if a shell is attached;
* proves the guard: a write outside the allow list, a control action without a token and a
  destructive action without a token must all be REFUSED — a refusal is the pass condition;
* with --allow-writes and --write-symbol, performs one idempotent write (value := current value)
  and restores from a snapshot, exercising the write path end to end;
* lists the tools it deliberately never calls (they change the machine) with the manual step
  from docs/HARDWARE_DAY.md, so coverage is explicit: every tool is either exercised or named.

Statuses: PASS — tool answered and the answer has the expected shape; FAIL — error or wrong shape;
WARN — an optional feature answered with an environmental error (no EtherCAT master, no HMI folder,
no web); SKIP — prerequisite missing (no router, no XAE, no project); MANUAL — changes the machine,
listed with the manual step. Exit code 0 when nothing FAILed.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Awaitable

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from . import __version__
from .config import Config, load as load_config


# --------------------------------------------------------------------------- result model
@dataclass
class Check:
    phase: str
    tool: str
    status: str                 # PASS | FAIL | WARN | SKIP | MANUAL
    ms: float = 0.0
    detail: str = ""
    args: dict[str, Any] = field(default_factory=dict)


# tools this run never calls, with the manual step that exercises each (docs/HARDWARE_DAY.md)
MANUAL: dict[str, str] = {
    "kb_set_location": "only with a folder you name (kb_status says what to ask); then kb_status shows the database",
    "kb_status": "after kb_set_location: location, projects, the ESI index (esi)",
    "kb_rebuild": "only when kb_status shows an index rebuild_needed / outdated: kb_rebuild(index='infosys'|'manuals'|"
                  "'esi') asks for approval (or takes a confirm_token); kb_status then shows the build running, "
                  "then complete",
    "kb_add_project": "with a project folder you name; then kb_search for an FB name you know",
    "kb_remove_project": "remove the folder you added; kb_search no longer finds it",
    "kb_reindex": "after editing a POU in an added folder",
    "kb_search": "an FB name from an added folder, and an error text (e.g. 'ADSError 1861') → known_error",
    "scope_record": "with a Scope configuration in the solution: scope_record start, wait, stop, save → .svdx + .csv in "
                    "<recordings>/Scope Recordings; then rec_start(..., also_scope=true) + rec_stop → <id>.svdx/.scope.csv/.scope.json",
    "scope_export": "save a Scope View recording as .svdx, then scope_export svd=<file> → CSV in <recordings>/Scope Recordings "
                    "plus <csv>.provenance.json; open the CSV next to the Scope View chart",
    "hw_lookup": "'EL2008' (or a terminal you use) → PDOs and CoE objects from the ESI index (index\\esi.db)",
    "hw_object": "hw_object(device='EL3356-0010', index='#x8000') → sub-indices with defaults and enum texts; compare "
                 "with the CoE tab of that terminal in XAE",
    "hw_search": "hw_search('Underrange') → devices with that PDO entry; hw_search('overtemperature', kinds=['diag'])",
    "hw_diag": "a text id from a terminal's diagnosis history (0x10F3) → hw_diag(code=…, device=<its type>)",
    "hw_module": "an AX8000 / EL3314-0090 slot module from hw_lookup's slots → hw_module(ident=…, device=…)",
    "ads_connect": "re-target to a second PLC port: ads_connect ams_port=852 on a box that has one",
    "ads_write_raw": "raw write by index group/offset — only with a known address on the test box",
    "ads_plc_state": "HARDWARE_DAY §7+: ads_plc_state stop/start on the test box with a confirm token",
    "ads_system_mode": "config/run switch on the test box; expect the confirmation prompt",
    "ads_add_route_to_plc": "adding a route needs the CX credentials; run once against the test box",
    "ads_call_method": "needs an FB method with {attribute 'TcRpcEnable'} on the target",
    "test_response": "stimulus → response on a stopped machine, with restore=true",
    "test_scenario": "a multi-step scenario spec on a stopped machine",
    "snap_apply": "covered with --allow-writes (dry run is exercised read-only)",
    "ipc_eventlog": "Windows targets only (wevtutil); not CE7. Token: tc_request_confirmation(action='ipc_eventlog', "
                    "target='Application'); check providers_on_target and that the temp file is gone (ipc_list_dir boot:tcmcp*)",
    "ipc_write_file": "write a scratch file under boot: on the test box, then ipc_delete_file it",
    "ipc_delete_file": "delete the scratch file written above",
    "ipc_start_process": "start an exe that exists on the target (CE7: \\Windows\\cmd.exe), test box only",
    "ipc_reboot": "test box only, at the end of the day",
    "xae_dialog_resolve": "open a harmless dialog in XAE (e.g. Tools → Options) and click its Cancel through the tool",
    "xae_io_disable": "toggles the I/O disable flag in the configuration — test box only",
    "xae_open_solution": "open the solution in a shell started with xae_attach(launch=true)",
    "xae_build": "HARDWARE_DAY §5: compare error/warning counts with the XAE Error List (or run with --with-build)",
    "xae_command": "run a harmless DTE command, e.g. View.Output",
    "xae_plc": "login/logout/start/stop on the test box",
    "xae_target": "set the target NetId to the one already selected (no-op) and confirm",
    "xae_activate_configuration": "test box only, after wf_release_check",
    "xae_restart_twincat": "test box only",
    "xae_deploy": "HARDWARE_DAY §9 on the test box",
    "xae_tree_consume_xml": "import a trivial XML onto a scratch tree item",
    "xae_tree_set_values": "set a value on a scratch tree item and read it back with xae_tree_get",
    "xae_tree_create_child": "create a scratch POU folder, then xae_tree_delete it",
    "xae_tree_delete": "delete the scratch item created above",
    "xae_pou_set": "write a comment line into a scratch POU, build, revert",
    "xae_pou_create": "create FB_Scratch, verify in Solution Explorer, delete",
    "xae_plcopen_import": "import the PLCopen XML exported below into the scratch folder",
    "xae_plcopen_export": "export one FB to PLCopen XML and open the file",
    "xae_boot_project": "test box only",
    "proj_set_code": "edit a comment in a scratch POU on disk, confirm byte-exact round trip with git diff",
    "exp_run": "HARDWARE_DAY §8: one real run (max_runs=1) on the test box after the dry run passes here",
    "exp_abort": "abort the real run above and confirm the factor was restored (snap_compare)",
    "exp_evaluate": "after the real runs",
    "exp_report": "after the real runs; open report.html",
    "exp_compare": "after two experiments exist",
    "tcunit_run": "HARDWARE_DAY §9 on the test box",
    "tc_set_mode": "exercised here only as a no-op (guarded → guarded); escalate to full on the test box and watch tc_audit_tail",
}

OPTIONAL = {  # a failure here is environmental, reported as WARN with the error text
    "ads_ethercat", "ads_discover", "ads_io_read", "hmi_scan", "hmi_bindings", "hmi_vs_runtime",
    "infosys_books", "infosys_index", "infosys_search_symbols", "infosys_declaration", "infosys_search",
    "infosys_search_code", "infosys_get_page", "ads_app_info", "xae_links", "xae_diagnostics", "proj_changed",
    "ipc_list_dir", "ipc_read_file", "rec_pulses", "rec_latency", "ads_wait_until", "test_stable",
}


def _payload(res: types.CallToolResult) -> Any:
    sc = res.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    txt = "".join(c.text for c in res.content if isinstance(c, types.TextContent))
    try:
        return json.loads(txt)
    except Exception:  # noqa: BLE001
        return txt


def _text(res: types.CallToolResult) -> str:
    return "".join(c.text for c in res.content if isinstance(c, types.TextContent))


def _short(v: Any, n: int = 160) -> str:
    s = json.dumps(v, default=str) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------- the run
class Acceptance:
    def __init__(self, session: ClientSession, cfg: Config, out_dir: Path, *, allow_writes: bool,
                 write_symbol: str | None, with_xae: bool, with_infosys: bool, with_build: bool,
                 log: Callable[[str], None]):
        self.s = session
        self.cfg = cfg
        self.out = out_dir
        self.allow_writes = allow_writes
        self.write_symbol = write_symbol
        self.with_xae = with_xae
        self.with_infosys = with_infosys
        self.with_build = with_build
        self.log = log
        self.checks: list[Check] = []
        self.called: set[str] = set()
        self.tools: list[str] = []
        # discovered inputs
        self.sym: str | None = None          # a scalar numeric symbol
        self.sym_val: Any = None
        self.sym2: str | None = None         # a second scalar
        self.bool_sym: str | None = None
        self.struct_sym: str | None = None   # a struct/FB instance
        self.struct_type: str | None = None
        self.root: str | None = None         # a program root (MAIN if present)
        self.fb_inst: str | None = None      # an FB instance for wf_fb_profile
        self.sym_ig: int | None = None
        self.sym_io: int | None = None
        self.sym_size: int = 0
        self.ads_ok = False
        self.proj_ok = False
        self.xae_ok = False
        self.first_unit: str | None = None
        self.first_fb: str | None = None
        self.first_file: str | None = None
        self.plc_project_path: str | None = None

    # -- primitives ----------------------------------------------------------
    async def call(self, tool: str, args: dict[str, Any] | None = None, timeout_s: float = 180) -> tuple[bool, Any, float]:
        self.called.add(tool)
        t0 = time.perf_counter()
        try:
            with anyio.fail_after(timeout_s):
                res = await self.s.call_tool(tool, args or {})
        except TimeoutError:
            return False, f"no answer within {timeout_s} s", (time.perf_counter() - t0) * 1000
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}", (time.perf_counter() - t0) * 1000
        ms = (time.perf_counter() - t0) * 1000
        if res.is_error:
            return False, _text(res), ms
        return True, _payload(res), ms

    def rec(self, phase: str, tool: str, status: str, ms: float = 0.0, detail: str = "", args: dict | None = None) -> None:
        self.checks.append(Check(phase, tool, status, round(ms, 1), detail, args or {}))
        self.log(f"  {status:6} {tool:28} {ms:7.0f} ms  {detail[:110]}")

    async def check(self, phase: str, tool: str, args: dict[str, Any] | None = None,
                    verify: Callable[[Any], str | None] | None = None, timeout_s: float = 180,
                    optional: bool | None = None) -> Any | None:
        """Call a tool and record PASS/FAIL/WARN; returns the payload on success, else None."""
        ok, payload, ms = await self.call(tool, args, timeout_s)
        opt = (tool in OPTIONAL) if optional is None else optional
        if not ok:
            self.rec(phase, tool, "WARN" if opt else "FAIL", ms, str(payload), args)
            return None
        if verify is not None:
            why = verify(payload)
            if why:
                self.rec(phase, tool, "FAIL", ms, f"unexpected answer: {why} — {_short(payload)}", args)
                return payload
        self.rec(phase, tool, "PASS", ms, _short(payload), args)
        return payload

    async def expect_refusal(self, phase: str, tool: str, args: dict[str, Any], must_contain: tuple[str, ...]) -> None:
        """The guard must refuse; a refusal whose text names the reason is the PASS condition."""
        ok, payload, ms = await self.call(tool, args, 60)
        if ok:
            self.rec(phase, tool, "FAIL", ms, f"was NOT refused — the guard let it through: {_short(payload)}", args)
            return
        low = str(payload).lower()
        if any(k.lower() in low for k in must_contain):
            self.rec(phase, tool, "PASS", ms, f"refused as required: {_short(payload, 120)}", args)
        else:
            self.rec(phase, tool, "FAIL", ms, f"refused, but for an unexpected reason: {_short(payload)}", args)

    def skip(self, phase: str, tool: str, why: str) -> None:
        self.rec(phase, tool, "SKIP", 0, why)

    # -- phases ---------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        started = dt.datetime.now(dt.timezone.utc)
        await self.phase_inventory()
        await self.phase_server()
        await self.phase_ads_reads()
        await self.phase_project()
        await self.phase_recorder()
        await self.phase_snapshots_and_tests()
        await self.phase_guard()
        await self.phase_experiments()
        await self.phase_infosys()
        await self.phase_xae()
        await self.phase_ipc()
        await self.phase_writes()
        self.phase_manual()
        # coverage: everything must be exercised or named
        unexercised = sorted(set(self.tools) - self.called - {c.tool for c in self.checks})
        counts: dict[str, int] = {}
        for c in self.checks:
            counts[c.status] = counts.get(c.status, 0) + 1
        return {
            "version": __version__, "started": started.isoformat(), "finished": dt.datetime.now(dt.timezone.utc).isoformat(),
            "platform": platform.platform(), "python": sys.version.split()[0],
            "target": self.cfg.ads.ams_net_id or "(local)", "mode": self.cfg.safety.mode,
            "project_root": self.cfg.project.root, "tools_total": len(self.tools), "counts": counts,
            "unexercised": unexercised, "discovered": {
                "scalar": self.sym, "scalar2": self.sym2, "bool": self.bool_sym, "struct": self.struct_sym,
                "struct_type": self.struct_type, "root": self.root, "fb_instance": self.fb_inst},
            "checks": [asdict(c) for c in self.checks],
        }

    async def phase_inventory(self) -> None:
        ph = "0 inventory"
        self.log("[0] inventory")
        tools = (await self.s.list_tools()).tools
        self.tools = sorted(t.name for t in tools)
        prompts = (await self.s.list_prompts()).prompts
        self.rec(ph, "list_tools", "PASS" if len(tools) >= 100 else "FAIL", 0, f"{len(tools)} tools, {len(prompts)} prompts")
        missing_ann = [t.name for t in tools if t.annotations is None]
        if missing_ann:
            self.rec(ph, "tool_annotations", "FAIL", 0, f"tools without annotations: {missing_ann[:5]}")

    async def phase_server(self) -> None:
        ph = "1 server"
        self.log("[1] server / safety")
        doc = await self.check(ph, "tc_doctor", {}, lambda p: None if isinstance(p, dict) and "router" in p else "no router field")
        if isinstance(doc, dict):
            self.ads_ok = doc.get("router") == "ok" and not str(doc.get("target_state", "")).startswith("FAIL")
            self.proj_ok = bool(doc.get("project_root") or self.cfg.project.root)
            for k in ("router", "local_ams_net_id", "target_state", "config_file", "xae_running", "xae_progids_registered", "infosys"):
                if k in doc:
                    self.log(f"      doctor.{k}: {_short(doc[k], 100)}")
            if not self.ads_ok:
                self.log("      !! ADS not usable — router/target phases will be skipped. Fix this first (INSTALL.md §6 step 1).")
        await self.check(ph, "tc_safety_status", {}, lambda p: None if isinstance(p, dict) and p.get("mode") else "no mode")
        # R2: tokens are only issued for real tool names — a token for deleting a recording that does not exist is harmless
        await self.check(ph, "tc_request_confirmation", {"action": "rec_delete", "target": "acceptance-probe-none"},
                         lambda p: None if isinstance(p, dict) and p.get("confirm_token") else "no token")
        await self.check(ph, "tc_set_mode", {"mode": self.cfg.safety.mode})     # no-op: same mode, audited
        await self.check(ph, "tc_audit_tail", {"n": 20}, lambda p: None if isinstance(p, list) else "not a list")
        await self.check(ph, "tc_reload_config", {}, lambda p: None if isinstance(p, dict) and "applied" in p else "no applied field")
        await self.check(ph, "wf_project_overview", {})
        sc = await self.check(ph, "scope_status", {}, lambda p: None if isinstance(p, dict) and "available" in p else "no available field")
        if isinstance(sc, dict):
            self.log(f"      scope export tool: {sc.get('export_tool') or 'not found'}")
        rep = self.out / "session_report.md"
        await self.check(ph, "tc_session_report", {"out_path": str(rep)}, lambda p: None if rep.exists() else "file not written")

    async def phase_ads_reads(self) -> None:
        ph = "2 ads reads"
        self.log("[2] ADS reads")
        if not self.ads_ok:
            for t in ("ads_status", "ads_list_symbols", "ads_symbol_info", "ads_read", "ads_read_many", "ads_types", "ads_leaves",
                      "ads_watch", "ads_wait_until", "ads_app_info", "ads_task_stats", "ads_ping", "ads_routes", "ads_ethercat",
                      "ads_discover", "ads_io_read", "ads_read_raw", "ads_dump", "ads_export", "wf_debug_symbol", "wf_fb_profile"):
                self.skip(ph, t, "ADS router/target not available (tc_doctor)")
            return
        st = await self.check(ph, "ads_status", {}, lambda p: None if isinstance(p, dict) and "plc" in p else "no plc field")
        syms = await self.check(ph, "ads_list_symbols", {"limit": 400},
                                lambda p: None if isinstance(p, dict) and p.get("symbols") else "no symbols — target in CONFIG or no boot project?")
        if isinstance(syms, dict):
            self._discover(syms.get("symbols", []))
        self.log(f"      discovered scalar={self.sym} bool={self.bool_sym} struct={self.struct_sym} ({self.struct_type}) root={self.root}")
        if not self.sym:
            for t in ("ads_symbol_info", "ads_read", "ads_read_many", "ads_leaves", "ads_watch", "ads_wait_until", "wf_debug_symbol"):
                self.skip(ph, t, "no scalar symbol discovered")
        else:
            await self.check(ph, "ads_symbol_info", {"name": self.sym}, lambda p: None if isinstance(p, dict) and p.get("type") else "no type")
            rd = await self.check(ph, "ads_read", {"name": self.sym}, lambda p: None if isinstance(p, dict) and "value" in p else "no value")
            if isinstance(rd, dict):
                self.sym_val = rd.get("value")
            names = [self.sym] + ([self.sym2] if self.sym2 else [])
            await self.check(ph, "ads_read_many", {"names": names},
                             lambda p: None if isinstance(p, dict) and all(n in p for n in names) else "missing names")
            await self.check(ph, "ads_watch", {"names": [self.sym], "duration_ms": 500, "interval_ms": 100})
            if self.sym_val is not None and not isinstance(self.sym_val, (dict, list)):
                await self.check(ph, "ads_wait_until", {"name": self.sym, "op": "==", "value": self.sym_val, "timeout_ms": 1500})
            await self.check(ph, "wf_debug_symbol", {"symbol": self.sym, "watch_ms": 300}, timeout_s=120)
        if self.struct_sym:
            await self.check(ph, "ads_read", {"name": self.struct_sym},
                             lambda p: None if isinstance(p, dict) and isinstance(p.get("value"), dict) and "raw_hex" not in json.dumps(p)[:4000]
                             else "struct did not decode to members (datatype table — verify item 14)")
        await self.check(ph, "ads_types", {"type_name": self.struct_type} if self.struct_type else {"pattern": "ST_", "limit": 20},
                         lambda p: None if isinstance(p, dict) and (p.get("types") or p.get("members") is not None or p.get("name")) else "empty")
        if self.root:
            await self.check(ph, "ads_leaves", {"name": self.root, "max_leaves": 300, "with_values": True},
                             lambda p: None if isinstance(p, dict) and p.get("leaves") is not None or isinstance(p, list) else "no leaves", timeout_s=240)
        else:
            self.skip(ph, "ads_leaves", "no program root discovered")
        await self.check(ph, "ads_app_info", {})
        await self.check(ph, "ads_task_stats", {"samples": 20},
                         lambda p: None if isinstance(p, dict) and "cycle_time_us" in p else "no cycle_time_us")
        await self.check(ph, "ads_ping", {"count": 3})
        await self.check(ph, "ads_routes", {})
        await self.check(ph, "ads_ethercat", {}, timeout_s=60)
        await self.check(ph, "ads_discover", {"timeout_s": 1.5}, timeout_s=30)
        await self.check(ph, "ads_io_read", {"area": "input", "offset": 0, "size": 1}, timeout_s=30)
        if self.sym_ig is not None:
            # the discovered scalar's own bytes, by index group/offset from the symbol table
            await self.check(ph, "ads_read_raw", {"index_group": self.sym_ig, "index_offset": self.sym_io, "size": self.sym_size})
        else:
            self.skip(ph, "ads_read_raw", "no symbol address discovered")
        if self.root:
            await self.check(ph, "ads_dump", {"pattern": f"{self.root}.*", "out_dir": str(self.out / "dump"), "max_leaves": 500, "max_total_mb": 5}, timeout_s=300)
            await self.check(ph, "ads_export", {"prefix": self.root, "fmt": "csharp_symbols"},
                             lambda p: None if isinstance(p, dict) and p.get("text") else "no text")
        if self.fb_inst:
            await self.check(ph, "wf_fb_profile", {"instance": self.fb_inst, "duration_s": 1.0, "cycle_ms": 10}, timeout_s=120)
        else:
            self.skip(ph, "wf_fb_profile", "no FB instance discovered")

    def _discover(self, symbols: list[dict[str, Any]]) -> None:
        scalar_types = {"BOOL", "BYTE", "WORD", "DWORD", "LWORD", "SINT", "USINT", "INT", "UINT", "DINT", "UDINT", "LINT", "ULINT",
                        "REAL", "LREAL", "TIME", "LTIME", "DATE", "DT", "TOD", "DATE_AND_TIME", "TIME_OF_DAY"}
        roots: dict[str, int] = {}
        names = {s.get("name", "") for s in symbols}

        def _hex(v: Any) -> int | None:
            try:
                return int(str(v), 16) if isinstance(v, str) else int(v)
            except (TypeError, ValueError):
                return None

        for s in symbols:
            name, typ = s.get("name", ""), str(s.get("type", ""))
            if not name or name.startswith(("TwinCAT_", "Constants.", "Global_", "__")):
                continue
            head = name.split(".")[0]
            roots[head] = roots.get(head, 0) + 1
            tu = typ.upper()
            if tu == "BOOL" and self.bool_sym is None:
                self.bool_sym = name
            elif tu in scalar_types and tu != "BOOL" and "STRING" not in tu:
                if self.sym is None:
                    self.sym = name
                    self.sym_ig, self.sym_io = _hex(s.get("index_group")), _hex(s.get("index_offset"))
                    self.sym_size = int(s.get("size") or 0)
                    if self.sym_ig is None or self.sym_io is None or not self.sym_size:
                        self.sym_ig = None
                elif self.sym2 is None:
                    self.sym2 = name
            elif tu.startswith(("ST_", "FB_")):        # a struct or FB instance (enums E_ are scalars to ADS)
                if self.struct_sym is None:
                    self.struct_sym, self.struct_type = name, typ
                # an FB instance is usable for profiling only if its members are in the table
                if tu.startswith("FB_") and self.fb_inst is None and any(n.startswith(name + ".") for n in names):
                    self.fb_inst = name
        if self.sym2 is None and self.bool_sym:
            self.sym2 = self.bool_sym
        if "MAIN" in roots:
            self.root = "MAIN"
        elif roots:
            self.root = max(roots.items(), key=lambda kv: kv[1])[0]

    async def phase_project(self) -> None:
        ph = "3 project"
        self.log("[3] project files")
        tools = ("proj_scan", "proj_list", "proj_get_code", "proj_resolve_symbol", "proj_search", "proj_references", "proj_read_file",
                 "proj_interface", "proj_lint", "proj_call_graph", "proj_variables", "proj_vs_runtime", "proj_changed", "proj_validate_xml",
                 "hmi_scan", "hmi_bindings", "hmi_vs_runtime", "tcunit_scaffold", "tcunit_results", "wf_release_check", "rec_scan",
                 "proj_libraries")
        if not self.cfg.project.root and not self.proj_ok:
            for t in tools:
                self.skip(ph, t, "no project root (xae.solution_path / project.root)")
            return
        sc = await self.check(ph, "proj_scan", {}, lambda p: None if isinstance(p, dict) and (p.get("units") or p.get("files")) else "nothing indexed", timeout_s=300)
        lst = await self.check(ph, "proj_list", {"limit": 50})
        units = (lst or {}).get("units", []) if isinstance(lst, dict) else []
        if units:
            self.first_unit = units[0]["name"]
            self.first_file = units[0].get("file")
            self.first_fb = next((u["name"] for u in units if u.get("kind") == "FUNCTION_BLOCK"), None)
        if self.first_unit:
            await self.check(ph, "proj_get_code", {"name": self.first_unit}, lambda p: None if isinstance(p, dict) and ("declaration" in p or "text" in p) else "no code")
            await self.check(ph, "proj_references", {"name": self.first_unit.split(".")[0]})
            await self.check(ph, "proj_call_graph", {"root": self.root or self.first_unit.split(".")[0], "depth": 3})
        else:
            for t in ("proj_get_code", "proj_references", "proj_call_graph"):
                self.skip(ph, t, "no units indexed")
        await self.check(ph, "proj_libraries", {}, optional=True)
        if self.first_fb:
            await self.check(ph, "proj_interface", {"name": self.first_fb})
        else:
            self.skip(ph, "proj_interface", "no FUNCTION_BLOCK in project")
        if self.sym:
            await self.check(ph, "proj_resolve_symbol", {"symbol": self.sym})
        elif self.first_unit:
            await self.check(ph, "proj_resolve_symbol", {"symbol": self.first_unit})
        await self.check(ph, "proj_search", {"regex": "END_VAR", "max_hits": 5})
        if self.first_file:
            await self.check(ph, "proj_read_file", {"path": self.first_file, "max_chars": 2000})
        lint = await self.check(ph, "proj_lint", {}, lambda p: None if isinstance(p, dict) and "items" in p else "no items", timeout_s=300)
        if isinstance(lint, dict):
            errs = [i for i in lint.get("items", []) if i.get("severity") == "error"]
            if errs:
                self.rec(ph, "proj_lint:errors", "WARN", 0, f"{len(errs)} error-severity findings on a project that builds — report them: "
                         + "; ".join(f"{e['unit']}:{e['rule']}" for e in errs[:5]))
        await self.check(ph, "proj_variables", {"scope": self.root, "max_items": 2000}, timeout_s=300)
        if self.ads_ok:
            await self.check(ph, "proj_vs_runtime", {"scope": self.root, "max_items": 2000}, timeout_s=300)
        else:
            self.skip(ph, "proj_vs_runtime", "needs ADS")
        await self.check(ph, "proj_changed", {})
        vx = await self.check(ph, "proj_validate_xml", {"fix": False}, timeout_s=300)
        if isinstance(vx, dict):
            errs = [i for i in vx.get("items", []) if i.get("severity") == "error"]
            if errs:
                self.rec(ph, "proj_validate_xml:errors", "WARN", 0, f"{len(errs)} error-severity findings on XAE-written files — report them: "
                         + "; ".join(f"{e.get('file','?').rsplit(chr(92),1)[-1]}:{e['rule']}" for e in errs[:5]))
        await self.check(ph, "hmi_scan", {})
        await self.check(ph, "hmi_bindings", {"limit": 20})
        await self.check(ph, "hmi_vs_runtime", {}, timeout_s=120)
        if self.first_fb:
            await self.check(ph, "tcunit_scaffold", {"fb_name": self.first_fb, "out_dir": str(self.out / "scaffold"), "add_to_plcproj": False, "overwrite": True})
        else:
            self.skip(ph, "tcunit_scaffold", "no FB")
        sample = ('<?xml version="1.0"?><testsuites><testsuite name="Acceptance" tests="1" failures="0">'
                  '<testcase name="t" classname="Acceptance" status="PASS"/></testsuite></testsuites>')
        await self.check(ph, "tcunit_results", {"xml_text": sample})
        await self.check(ph, "wf_release_check", {"run_build": False}, timeout_s=300)
        await self.check(ph, "rec_scan", {"prefix": self.root, "source": "project", "show": 20, "max_items": 500}, timeout_s=120)

    async def phase_recorder(self) -> None:
        ph = "4 recorder"
        self.log("[4] recorder round trip")
        tools = ("rec_scan", "rec_profile_save", "rec_profile_list", "rec_profile_get", "rec_profile_start", "rec_list", "rec_status",
                 "rec_snapshot", "rec_stats", "rec_read", "rec_profile_stop", "rec_export", "rec_pulses", "rec_latency", "rec_start",
                 "rec_stop", "rec_compare", "rec_delete", "rec_profile_delete", "rec_verify", "rec_capture")
        if not self.ads_ok or not self.sym:
            for t in tools:
                self.skip(ph, t, "needs ADS and a discovered symbol")
            return
        syms = [self.sym] + ([self.bool_sym] if self.bool_sym else []) + ([self.sym2] if self.sym2 and self.sym2 != self.bool_sym else [])
        await self.check(ph, "rec_scan", {"prefix": self.root, "source": "runtime", "show": 20, "max_items": 300}, timeout_s=120)
        await self.check(ph, "rec_profile_save", {"name": "acceptance", "symbols": syms, "mode": "auto", "cycle_ms": 10, "replace": True})
        await self.check(ph, "rec_profile_list", {}, lambda p: None if isinstance(p, dict) and any(x.get("name") == "acceptance" for x in p.get("profiles", [])) else "profile missing")
        await self.check(ph, "rec_profile_get", {"name": "acceptance"})
        st = await self.check(ph, "rec_profile_start", {"name": "acceptance", "duration_s": 2.0}, timeout_s=120)
        rids: list[str] = []
        if isinstance(st, dict):
            rids = [r.get("id") for r in st.get("recordings", []) if r.get("id")] or ([st["id"]] if st.get("id") else [])
        await asyncio.sleep(2.6)
        lst = await self.check(ph, "rec_list", {})
        if not rids and isinstance(lst, dict):
            rids = [r.get("id") for r in lst.get("recordings", []) if r.get("id")]
        if not rids:
            for t in ("rec_status", "rec_snapshot", "rec_stats", "rec_read", "rec_export", "rec_pulses", "rec_latency", "rec_compare", "rec_delete"):
                self.skip(ph, t, "no recording id returned")
        else:
            rid = rids[0]
            stt = await self.check(ph, "rec_status", {"id": rid}, lambda p: None if isinstance(p, dict) and "events" in p else "no events field")
            # D29: an aligned profile waits for a shared start cycle — give its trigger a moment before judging events
            for _ in range(20):
                if not (isinstance(stt, dict) and stt.get("armed")):
                    break
                await asyncio.sleep(0.25)
                ok_, p_, _ms = await self.call("rec_status", {"id": rid}, 30)
                stt = p_ if ok_ else stt
            if isinstance(stt, dict) and stt.get("armed"):
                trig = (st.get("aligned") or {}).get("trigger") if isinstance(st, dict) else None
                self.rec(ph, "rec_status:armed", "FAIL", 0,
                         f"still armed 5 s later — the start trigger never fired ({trig or 'start_when'}): is the PLC task running?")
            elif isinstance(stt, dict):
                if stt.get("dropped"):
                    self.rec(ph, "rec_status:dropped", "WARN", 0, f"dropped={stt['dropped']} on a 3-symbol profile — thin profiles before trusting timings")
                if not stt.get("events"):
                    self.rec(ph, "rec_status:events", "FAIL", 0, "0 events in 2 s — notifications not arriving (firewall TCP 48898 inbound? CE7 notification support?)")
            await self.check(ph, "rec_snapshot", {"id": rid, "last_n": 10})
            await self.check(ph, "rec_stats", {"id": rid})
            await self.check(ph, "rec_read", {"id": rid, "max_rows": 50})
            await self.check(ph, "rec_profile_stop", {"name": "acceptance"}, optional=True)   # may already have stopped (duration)
            await self.check(ph, "rec_export", {"id": rid, "fmt": "csv_wide", "out_path": str(self.out / "acceptance.wide.csv")},
                             lambda p: None if (self.out / "acceptance.wide.csv").exists() else "csv not written")
            if self.bool_sym:
                await self.check(ph, "rec_pulses", {"id": rid, "symbol": self.bool_sym})
            else:
                self.skip(ph, "rec_pulses", "no BOOL discovered")
            await self.check(ph, "rec_latency", {"id": rid, "from_symbol": self.sym, "from_op": "!=", "from_value": None,
                                                 "to_symbol": self.sym, "to_op": "!=", "to_value": None, "max_pairs": 5})
        r2 = await self.check(ph, "rec_start", {"symbols": [self.sym], "mode": "poll", "cycle_ms": 50, "duration_s": 1.0, "name": "acceptance-poll"}, timeout_s=60)
        rid2 = r2.get("id") if isinstance(r2, dict) else None
        await asyncio.sleep(1.4)
        if rid2:
            await self.check(ph, "rec_stop", {"id": rid2}, optional=True)
            if rids:
                await self.check(ph, "rec_compare", {"id_a": rids[0], "id_b": rid2})
            tok = await self.check(ph, "tc_request_confirmation", {"action": "rec_delete", "target": rid2})
            await self.check(ph, "rec_delete", {"id": rid2, "confirm_token": (tok or {}).get("confirm_token")})
        else:
            for t in ("rec_stop", "rec_compare", "rec_delete"):
                self.skip(ph, t, "poll recording did not start")
        await self.check(ph, "rec_profile_delete", {"name": "acceptance"})
        await self.check(ph, "rec_capture", {"symbols": [self.sym], "seconds": 1.0, "mode": "cycle", "cycle_ms": 10}, timeout_s=90,
                         verify=lambda p: None if isinstance(p, dict) and (p.get("verify") or {}).get("integrity") == "intact" else "not intact")
        await self._recording_accuracy(ph)

    async def _recording_accuracy(self, ph: str) -> None:
        """Ground truth: record the PLC task's own cycle counter at the task cycle for 3 s and prove that every
        cycle is in the file (rec_verify counter=…). This is the check that matters for timing analysis."""
        counter = "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount"
        ct = await self.check(ph, "ads_read", {"name": "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleTime"}, optional=True)
        if not isinstance(ct, dict) or not isinstance(ct.get("value"), (int, float)) or not ct["value"]:
            self.skip(ph, "rec_verify", "task info not readable on this target (TwinCAT_SystemInfoVarList._TaskInfo)")
            return
        cycle_ms = ct["value"] / 10_000.0                     # CycleTime is in 100 ns units
        for engine in ("native", "python"):          # both engines must prove every cycle (0.8: native is the default)
            r = await self.check(ph, "rec_start", {"symbols": [counter], "mode": "cycle", "cycle_ms": cycle_ms, "duration_s": 3.0,
                                                   "name": f"acceptance-accuracy-{engine}", "engine": engine},
                                 timeout_s=60, optional=(engine == "native"))
            rid = r.get("id") if isinstance(r, dict) else None
            if not rid:
                self.skip(ph, "rec_verify", f"accuracy recording ({engine} engine) did not start")
                continue
            if r.get("engine") != engine:
                self.skip(ph, "rec_verify", f"{engine} engine not used: {r.get('engine_note')}")
            await asyncio.sleep(4.0)
            await self.check(ph, "rec_stop", {"id": rid}, optional=True)
            await self.check(ph, "rec_verify", {"id": rid, "counter": counter},
                             lambda p, e=engine: None if isinstance(p, dict) and p.get("verdict") == "PASS" else
                             f"{e} engine: recording at the task cycle ({cycle_ms} ms) is NOT lossless: "
                             f"{(p or {}).get('counter')} / {(p or {}).get('timeline', {}).get('missing')} missing")

    async def phase_snapshots_and_tests(self) -> None:
        ph = "5 snapshots/tests"
        self.log("[5] snapshots and expectations")
        if not self.ads_ok or not self.sym:
            for t in ("snap_save", "snap_list", "snap_compare", "snap_apply", "test_expect", "test_stable"):
                self.skip(ph, t, "needs ADS and a discovered symbol")
            return
        syms = [self.sym] + ([self.sym2] if self.sym2 else [])
        await self.check(ph, "snap_save", {"name": "acceptance", "symbols": syms})
        await self.check(ph, "snap_list", {}, lambda p: None if isinstance(p, list) and any((x.get("name") if isinstance(x, dict) else x) == "acceptance" for x in p) else "snapshot missing")
        await self.check(ph, "snap_compare", {"a": "acceptance"})
        if self.sym_val is not None and not isinstance(self.sym_val, (dict, list)):
            await self.check(ph, "test_expect", {"checks": [{"symbol": self.sym, "op": "==", "value": self.sym_val}], "within_ms": 0},
                             lambda p: None if isinstance(p, dict) else "not a dict")
        await self.check(ph, "test_stable", {"symbol": self.sym, "duration_ms": 300, "tolerance": 1e9})
        if not self.allow_writes:
            # dry run computes the differences and writes nothing; the real apply is covered by --allow-writes
            await self.check(ph, "snap_apply", {"name": "acceptance", "dry_run": True},
                             lambda p: None if isinstance(p, dict) and p.get("dry_run") and not p.get("written") else "dry run wrote something")

    async def phase_guard(self) -> None:
        ph = "6 guard"
        self.log("[6] guard proofs (refusal is the pass condition)")
        if self.cfg.safety.mode == "full":
            self.rec(ph, "guard", "WARN", 0, "mode=full: guard proofs skipped — run the acceptance in guarded mode to prove them")
            return
        if self.ads_ok and self.sym:
            if not self.cfg.safety.write_allowlist or not any(self._fn(self.sym, p) for p in self.cfg.safety.write_allowlist):
                await self.expect_refusal(ph, "ads_write", {"name": self.sym, "value": self.sym_val if self.sym_val is not None else 0},
                                          ("allowlist", "readonly", "denylist"))
                await self.expect_refusal(ph, "ads_write_many", {"values": {self.sym: self.sym_val if self.sym_val is not None else 0}},
                                          ("allowlist", "readonly", "denylist"))
            else:
                self.rec(ph, "ads_write", "SKIP", 0, f"{self.sym} is on the allow list; refusal proof not applicable (use --allow-writes)")
            await self.expect_refusal(ph, "ads_plc_state", {"action": "stop"}, ("confirm", "elicitation", "readonly"))
            await self.expect_refusal(ph, "ads_system_mode", {"action": "config"}, ("confirm", "elicitation", "readonly"))
        else:
            for t in ("ads_write", "ads_write_many", "ads_plc_state", "ads_system_mode"):
                self.skip(ph, t, "needs ADS")
        await self.expect_refusal(ph, "ipc_reboot", {}, ("confirm", "elicitation", "readonly"))
        await self.expect_refusal(ph, "tc_allow_write", {"pattern": "ACCEPTANCE.*"}, ("confirm", "elicitation", "readonly"))
        if self.cfg.safety.mode != "full":
            await self.expect_refusal(ph, "tc_set_mode", {"mode": "full"}, ("confirm", "elicitation", "ceiling"))
        if self.ads_ok and self.sym and isinstance(self.sym_val, (int, float)) and not isinstance(self.sym_val, bool):
            # out-of-range value: must be refused by the type check, never wrapped (even before the guard)
            ok, payload, ms = await self.call("ads_write", {"name": self.sym, "value": 10 ** 30}, 30)
            self.rec(ph, "ads_write:range", "PASS" if (not ok and ("out of range" in str(payload) or "allowlist" in str(payload)
                                                                 or "readonly" in str(payload))) else "FAIL", ms,
                     "refused" if not ok else "WROTE an out-of-range value")
        await self.expect_refusal(ph, "xae_activate_configuration", {}, ("confirm", "elicitation", "readonly", "not attached", "windows"))
        await self.expect_refusal(ph, "xae_restart_twincat", {}, ("confirm", "elicitation", "readonly", "not attached", "windows"))
        await self.expect_refusal(ph, "ipc_start_process", {"path": "nothing.exe"}, ("confirm", "elicitation", "readonly"))

    @staticmethod
    def _fn(name: str, pattern: str) -> bool:
        import fnmatch
        return fnmatch.fnmatch(name.lower(), pattern.lower())

    async def phase_experiments(self) -> None:
        ph = "7 experiments"
        self.log("[7] experiments (dry run only)")
        tpl = await self.check(ph, "exp_template", {}, lambda p: None if isinstance(p, dict) and (p.get("yaml") or p.get("spec")) else "no template")
        if isinstance(tpl, dict):
            # the shipped template names machine-specific symbols; structural validation only
            await self.check(ph, "exp_validate", {"spec": tpl.get("yaml") or tpl.get("spec"), "resolve_symbols": False})
        if not (self.ads_ok and self.sym and self.sym_val is not None and not isinstance(self.sym_val, (dict, list))):
            for t in ("exp_save", "exp_list", "exp_get", "exp_runs", "exp_status", "exp_run"):
                self.skip(ph, t, "needs ADS and a discovered scalar")
            return
        # a minimal spec on the machine's own symbols: one factor at its current value, one measure, no criteria.
        # dry_run resolves every symbol and asks the guard about every write without performing any.
        await self.check(ph, "rec_profile_save", {"name": "acceptance_exp", "symbols": [self.sym] + ([self.bool_sym] if self.bool_sym else []),
                                                  "mode": "auto", "cycle_ms": 10, "replace": True})
        spec = {
            "name": "acceptance_template", "version": 1, "hypothesis": "acceptance run: the pipeline resolves and plans on this machine",
            "machine": {"serial": "acceptance", "target": None}, "profile": "acceptance_exp", "writes_allowed": [self.sym],
            "preconditions": [{"symbol": self.sym, "op": "==", "value": self.sym_val, "timeout_ms": 2000}],
            "factors": [{"name": "f_current", "symbol": self.sym, "levels": [self.sym_val]}],
            "runs_per_level": 1, "max_runs": 1,
            "run": {"done_when": {"symbol": self.sym, "op": "==", "value": self.sym_val, "timeout_ms": 2000}, "settle_ms": 0, "between_runs_ms": 0},
            "measures": {"v_last": {"kind": "last", "symbol": self.sym}},
            "metrics": {"v_mean": {"measure": "v_last", "stat": "mean"}},
            "criteria": [],
        }
        # with the factor symbol off the allow list, the only acceptable problem is the guard saying so
        def _only_guard(p: Any) -> str | None:
            if not isinstance(p, dict):
                return "not a dict"
            bad = [x for x in p.get("problems", []) if "guard refuses" not in str(x)]
            return f"problems: {bad}" if bad else None
        await self.check(ph, "exp_validate", {"spec": spec, "resolve_symbols": True}, _only_guard)
        await self.check(ph, "exp_save", {"spec": spec, "replace": True})
        await self.check(ph, "exp_list", {}, lambda p: None if isinstance(p, dict) and any((e.get("name") if isinstance(e, dict) else e) == "acceptance_template"
                                                                                           for e in p.get("experiments", [])) else "saved experiment not listed")
        await self.check(ph, "exp_get", {"name": "acceptance_template"})
        await self.check(ph, "exp_runs", {"name": "acceptance_template"})
        await self.check(ph, "exp_status", {"name": "acceptance_template"}, optional=True)   # never ran: an error answer is legitimate
        ok, dry, ms = await self.call("exp_run", {"name": "acceptance_template", "dry_run": True}, 120)
        if ok:
            self.rec(ph, "exp_run", "PASS", ms, "dry run planned: " + _short(dry), {"dry_run": True})
        elif "guard refuses" in str(dry):
            self.rec(ph, "exp_run", "PASS", ms, "dry run stopped at the guard (factor symbol not on the allow list) — correct in guarded mode", {"dry_run": True})
        else:
            self.rec(ph, "exp_run", "FAIL", ms, str(dry), {"dry_run": True})
        await self.check(ph, "rec_profile_delete", {"name": "acceptance_exp"})
        try:
            import shutil
            d = Path(self.cfg.project.experiments_dir or "") / "acceptance_template"
            if d.is_dir():
                shutil.rmtree(d)
        except Exception:  # noqa: BLE001
            pass

    async def phase_infosys(self) -> None:
        ph = "8 infosys"
        self.log("[8] documentation lookup")
        tools = ("infosys_books", "infosys_search_symbols", "infosys_declaration", "infosys_search", "infosys_search_code", "infosys_get_page", "infosys_index")
        if not self.with_infosys or not self.cfg.docs.enabled:
            for t in tools:
                self.skip(ph, t, "--no-infosys or docs.enabled=false")
            return
        books = await self.check(ph, "infosys_books", {"limit": 5}, timeout_s=90)
        if books is None:
            for t in tools[1:]:
                self.skip(ph, t, "infosys.beckhoff.com not reachable from this PC")
            return
        pg = await self.check(ph, "infosys_get_page", {"ref": "ADSREAD", "max_chars": 4000}, timeout_s=90,
                              )
        if isinstance(pg, dict):
            title = str(pg.get("title", ""))
            if "Beckhoff Information System" in title or not title:
                self.rec(ph, "infosys_get_page:title", "WARN", 0, f"title={title!r} — the HTML walker does not match the live markup; save one page as HTML and report")
        await self.check(ph, "infosys_index", {"book": "tcplclib_tc2_system", "max_pages": 25}, timeout_s=300)
        await self.check(ph, "infosys_search_symbols", {"query": "ADSREAD", "limit": 5}, timeout_s=90)
        await self.check(ph, "infosys_declaration", {"symbol": "ADSREAD"}, timeout_s=90)
        await self.check(ph, "infosys_search", {"query": "ADSREAD", "limit": 5, "web_fallback": False}, timeout_s=90)
        await self.check(ph, "infosys_search_code", {"query": "ADSREAD", "limit": 5}, timeout_s=90)

    async def phase_xae(self) -> None:
        ph = "9 xae"
        self.log("[9] XAE, read-only")
        tools = ("xae_attach", "xae_status", "xae_plc_projects", "xae_tree_get", "xae_pou_get", "xae_list_commands", "xae_dialogs", "xae_links", "xae_diagnostics", "scope_list")
        if not self.with_xae or sys.platform != "win32":
            for t in tools:
                self.skip(ph, t, "--no-xae or not Windows")
            if not self.with_build:
                self.skip(ph, "xae_build", "see MANUAL / --with-build")
            return
        att = await self.check(ph, "xae_attach", {"launch": False}, timeout_s=120)
        self.xae_ok = att is not None
        if not self.xae_ok:
            for t in tools[1:]:
                self.skip(ph, t, "no XAE shell attached (open the solution in XAE, same elevation as Claude Code)")
            return
        await self.check(ph, "xae_status", {}, lambda p: None if isinstance(p, dict) and p.get("solution") else "no solution")
        pp = await self.check(ph, "xae_plc_projects", {})
        proj_path = None
        if isinstance(pp, dict):
            items = pp.get("projects") or pp.get("items") or []
            if items and isinstance(items[0], dict):
                proj_path = items[0].get("path") or items[0].get("tree_path")
                self.plc_project_path = items[0].get("plcproj") or items[0].get("file") or proj_path
        elif isinstance(pp, list) and pp and isinstance(pp[0], dict):
            proj_path = pp[0].get("path") or pp[0].get("tree_path")
            self.plc_project_path = pp[0].get("plcproj") or pp[0].get("file") or proj_path
        if proj_path:
            await self.check(ph, "xae_tree_get", {"path": proj_path, "depth": 2, "max_items": 50}, timeout_s=120)
            pou = f"{proj_path}^POUs^{self.root}" if self.root else None
            if pou:
                await self.check(ph, "xae_pou_get", {"path": pou, "include_subitems": False}, optional=True, timeout_s=120)
            else:
                self.skip(ph, "xae_pou_get", "no root program")
        else:
            self.skip(ph, "xae_tree_get", "xae_plc_projects returned no path")
            self.skip(ph, "xae_pou_get", "xae_plc_projects returned no path")
        await self.check(ph, "xae_list_commands", {"contains": "Build", "limit": 20}, timeout_s=120)
        sl = await self.check(ph, "scope_list", {}, lambda p: None if isinstance(p, dict) and "count" in p else "no count", timeout_s=120)
        if isinstance(sl, dict):
            self.log(f"      Scope configurations open: {[d.get('name') for d in sl.get('scopes') or []]}")
        await self.check(ph, "xae_dialogs", {}, lambda p: None if isinstance(p, dict) and "scope" in p and "none" not in str(p.get("scope")) else "watchdog has no process scope — pid unknown")
        if self.plc_project_path:
            await self.check(ph, "xae_links", {"plc_project_path": self.plc_project_path}, timeout_s=120)
        else:
            self.skip(ph, "xae_links", "no PLC project path")
        await self.check(ph, "xae_diagnostics", {"tail_lines": 50}, timeout_s=120)
        if self.with_build:
            await self.check(ph, "xae_build", {}, timeout_s=self.cfg.xae.build_timeout_s + 60)

    async def phase_ipc(self) -> None:
        ph = "10 ipc"
        self.log("[10] target file system (read-only)")
        if not self.ads_ok:
            for t in ("ipc_list_dir", "ipc_read_file"):
                self.skip(ph, t, "needs ADS")
            return
        ls = await self.check(ph, "ipc_list_dir", {"pattern": "boot:*", "max_entries": 50}, timeout_s=90,
                              verify=lambda p: None if isinstance(p, dict) and p.get("resolved_pattern") and
                              not any(e.get("name") in (".", "..") for e in p.get("entries", [])) else "prefix not resolved or dot entries present")
        if isinstance(ls, dict) and not any(str(e.get("name", "")).lower() == "currentconfig.xml" for e in ls.get("entries", [])):
            self.rec(ph, "ipc_list_dir:boot", "WARN", 0, f"boot folder resolved to {ls.get('resolved_pattern')} but CurrentConfig.xml is not in it — "
                     "either no configuration was ever activated on this target or the probe picked the wrong folder")
        name = None
        if isinstance(ls, dict):
            for e in ls.get("entries", []):
                if not e.get("is_dir") and str(e.get("name", "")).lower().endswith((".xml", ".txt", ".log", ".csv")):
                    name = e["name"]
                    break
        if name:
            await self.check(ph, "ipc_read_file", {"path": f"boot:{name}", "max_bytes": 4096}, timeout_s=60)
        else:
            await self.check(ph, "ipc_read_file", {"path": "boot:CurrentConfig.xml", "max_bytes": 4096}, timeout_s=60)

    async def phase_writes(self) -> None:
        ph = "11 writes"
        if not self.allow_writes:
            self.log("[11] writes: not requested (--allow-writes --write-symbol)")
            return
        self.log("[11] one idempotent write (value := current value) with snapshot restore")
        sym = self.write_symbol
        if not (self.ads_ok and sym):
            self.skip(ph, "ads_write", "needs ADS and --write-symbol")
            return
        cur = await self.check(ph, "ads_read", {"name": sym})
        if not isinstance(cur, dict) or "value" in cur and isinstance(cur["value"], (dict, list)):
            self.rec(ph, "ads_write", "SKIP", 0, "--write-symbol must be a scalar")
            return
        val = cur.get("value")
        await self.check(ph, "snap_save", {"name": "acceptance_pre", "symbols": [sym]})
        await self.check(ph, "ads_write", {"name": sym, "value": val}, lambda p: None if isinstance(p, dict) else "not a dict")
        await self.check(ph, "ads_write_many", {"values": {sym: val}},
                         lambda p: None if isinstance(p, dict) and p.get("complete") and sym in p.get("applied", {}) else "not complete")
        await self.check(ph, "snap_compare", {"a": "acceptance_pre"}, lambda p: None if isinstance(p, dict) else "not a dict")
        await self.check(ph, "snap_apply", {"name": "acceptance_pre"})
        tail = await self.check(ph, "tc_audit_tail", {"n": 10})
        if isinstance(tail, list) and not any(a.get("tool") == "ads_write" and a.get("outcome") == "ok" for a in tail):
            self.rec(ph, "audit:write", "FAIL", 0, "the write is not in the audit log")

    def phase_manual(self) -> None:
        ph = "12 manual"
        for tool, how in MANUAL.items():
            if tool in self.called:
                continue
            self.rec(ph, tool, "MANUAL", 0, how)


# --------------------------------------------------------------------------- report
def render_markdown(rep: dict[str, Any]) -> str:
    c = rep["counts"]
    lines = [f"# twincat-mcp acceptance — {rep['started'][:19]}Z",
             "",
             f"server {rep['version']} · {rep['platform']} · Python {rep['python']} · target {rep['target']} · mode {rep['mode']}",
             f"project {rep['project_root']}",
             "",
             f"**{c.get('PASS', 0)} PASS · {c.get('FAIL', 0)} FAIL · {c.get('WARN', 0)} WARN · {c.get('SKIP', 0)} SKIP · {c.get('MANUAL', 0)} MANUAL** of {rep['tools_total']} tools",
             ""]
    if rep["unexercised"]:
        lines += ["> Tools neither exercised nor listed as manual (coverage gap in the acceptance script itself): "
                  + ", ".join(f"`{t}`" for t in rep["unexercised"]), ""]
    d = rep["discovered"]
    lines += ["Discovered inputs: " + ", ".join(f"{k}=`{v}`" for k, v in d.items() if v), ""]
    fails = [x for x in rep["checks"] if x["status"] == "FAIL"]
    if fails:
        lines += ["## Failures — send these back", ""]
        for x in fails:
            lines += [f"- **{x['tool']}** ({x['phase']}): {x['detail']}" + (f"  \n  args: `{json.dumps(x['args'], default=str)[:200]}`" if x["args"] else "")]
        lines += [""]
    warns = [x for x in rep["checks"] if x["status"] == "WARN"]
    if warns:
        lines += ["## Warnings — environmental or to be judged", ""]
        for x in warns:
            lines += [f"- **{x['tool']}**: {x['detail']}"]
        lines += [""]
    lines += ["## Every check", "", "| phase | tool | status | ms | detail |", "|---|---|---|---:|---|"]
    for x in rep["checks"]:
        det = x["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {x['phase']} | `{x['tool']}` | {x['status']} | {x['ms']:.0f} | {det[:200]} |")
    lines += ["", "## Manual steps (machine-changing tools)", ""]
    for x in rep["checks"]:
        if x["status"] == "MANUAL":
            lines.append(f"- `{x['tool']}` — {x['detail']}")
    lines += ["", "Statuses: PASS answered with the expected shape · FAIL error or wrong shape · WARN optional feature answered with an "
              "environmental error · SKIP prerequisite missing · MANUAL changes the machine, see docs/HARDWARE_DAY.md.", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- driver
async def run_acceptance(cfg: Config, out_dir: Path, *, allow_writes: bool = False, write_symbol: str | None = None,
                         with_xae: bool = True, with_infosys: bool = True, with_build: bool = False,
                         log: Callable[[str], None] = lambda s: print(s, file=sys.stderr)) -> dict[str, Any]:
    import copy

    from .server import build_server
    out_dir.mkdir(parents=True, exist_ok=True)
    # R4: tools only write inside known roots — the acceptance output folder is one for this run
    cfg = copy.deepcopy(cfg)
    cfg.server.output_dirs = list(cfg.server.output_dirs or []) + [str(out_dir)]
    server = build_server(cfg)
    result: dict[str, Any] = {}
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        cr, cw = client_streams
        sr, sw = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: server._lowlevel_server.run(sr, sw, server._lowlevel_server.create_initialization_options(),
                                                               raise_exceptions=False))
            async with ClientSession(cr, cw) as session:
                await session.initialize()
                acc = Acceptance(session, cfg, out_dir, allow_writes=allow_writes, write_symbol=write_symbol,
                                 with_xae=with_xae, with_infosys=with_infosys, with_build=with_build, log=log)
                result = await acc.run()
            tg.cancel_scope.cancel()
    try:
        stop = getattr(server, "_tcmcp_stop", None)
        if callable(stop):
            stop()
    except Exception:  # noqa: BLE001
        pass
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    (out_dir / f"acceptance_{ts}.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    md = out_dir / f"acceptance_{ts}.md"
    md.write_text(render_markdown(result), encoding="utf-8")
    result["report_md"] = str(md)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m twincat_mcp.acceptance",
                                 description="Exercise every tool against the live engineering PC/target and write a per-tool report.")
    ap.add_argument("--config", help="twincat_mcp.toml (default: TCMCP_CONFIG / ./twincat_mcp.toml)")
    ap.add_argument("--out", default=None, help="report folder (default: <project root or cwd>/acceptance)")
    ap.add_argument("--allow-writes", action="store_true", help="also perform one idempotent write to --write-symbol and restore it")
    ap.add_argument("--write-symbol", default=None, help="scalar symbol on the allow list, e.g. GVL_Test.nScratch")
    ap.add_argument("--no-xae", action="store_true", help="skip the XAE phase")
    ap.add_argument("--no-infosys", action="store_true", help="skip the documentation phase (no web)")
    ap.add_argument("--with-build", action="store_true", help="also run xae_build (slow; needs an attached shell)")
    ap.add_argument("--log", default="WARNING")
    args = ap.parse_args(argv)
    import logging
    logging.basicConfig(level=getattr(logging, args.log.upper(), logging.WARNING), stream=sys.stderr)
    cfg = load_args_config(args.config)
    if args.allow_writes and not args.write_symbol:
        print("--allow-writes needs --write-symbol <scalar on the allow list>", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else Path(cfg.project.root or os.getcwd()) / "acceptance"
    rep = asyncio.run(run_acceptance(cfg, out, allow_writes=args.allow_writes, write_symbol=args.write_symbol,
                                     with_xae=not args.no_xae, with_infosys=not args.no_infosys, with_build=args.with_build))
    c = rep["counts"]
    print(f"\n{c.get('PASS', 0)} PASS  {c.get('FAIL', 0)} FAIL  {c.get('WARN', 0)} WARN  {c.get('SKIP', 0)} SKIP  "
          f"{c.get('MANUAL', 0)} MANUAL  of {rep['tools_total']} tools\nreport: {rep['report_md']}")
    if rep["unexercised"]:
        print("coverage gap (script bug): " + ", ".join(rep["unexercised"]))
    return 1 if c.get("FAIL", 0) else 0


def load_args_config(path: str | None) -> Config:
    return load_config(path)


if __name__ == "__main__":
    raise SystemExit(main())
