"""Composite workflows: the tool combinations that come up every day, packaged
so one call produces the dossier instead of eight round-trips.

Each function takes the ``App`` and returns a report dict. They only call
other backends; gating of writes/destructive steps stays in the server.
"""
from __future__ import annotations

import time
from typing import Any

from . import codecheck
from .ads_backend import AdsError
from .project_backend import ProjectError
from .safety import OpClass


async def debug_symbol(app, symbol: str, watch_ms: int = 1500, context_lines: int = 12) -> dict[str, Any]:
    """Live value + type + where it is declared and written + a short watch."""
    rep: dict[str, Any] = {"symbol": symbol}
    try:
        rep["live"] = await app.ads_call(app.ads.read, symbol, True, 100, timeout=60)
    except Exception as exc:  # noqa: BLE001
        rep["live"] = {"error": str(exc)}
    try:
        res = await app.proj_call(app.proj.resolve_symbol, symbol)
        rep["resolution"] = res
        leaf = symbol.split(".")[-1].split("[")[0]
        writers = await app.proj_call(app.proj.search, rf"\b{leaf}\b\s*:=", None, 50, True)
        mentions = await app.proj_call(app.proj.search, rf"\b{leaf}\b", ["PROGRAM", "FUNCTION_BLOCK", "FUNCTION", "METHOD", "ACTION", "GET", "SET"], 200, True)
        impl_mentions = [h for h in mentions["hits"] if h["part"] == "implementation"]
        rep["writers"] = writers["hits"][:30]
        rep["mentions"] = impl_mentions[:30]
        excerpts: list[dict[str, Any]] = []
        seen_units: list[str] = []
        for h in writers["hits"] + impl_mentions:
            if h["unit"] in seen_units or len(seen_units) >= 4:
                continue
            seen_units.append(h["unit"])
            try:
                code = await app.proj_call(app.proj.get_code, h["unit"], "implementation", False)
            except Exception:  # noqa: BLE001
                continue
            lines = (code.get("implementation") or "").splitlines()
            u = app.proj.get_unit(h["unit"])
            idxs = [i for i, ln in enumerate(lines) if leaf.lower() in ln.lower()][:3]
            for i in idxs:
                lo, hi = max(0, i - context_lines // 2), min(len(lines), i + context_lines // 2 + 1)
                excerpts.append({"unit": h["unit"], "file": h["file"], "from_line": u.impl_line0 + lo,
                                 "text": "\n".join(f"{u.impl_line0 + j}| {lines[j]}" for j in range(lo, hi))})
        rep["code_excerpts"] = excerpts
    except Exception as exc:  # noqa: BLE001
        rep["resolution"] = {"error": str(exc)}
    live = rep.get("live") or {}
    if isinstance(live, dict) and "error" not in live and watch_ms > 0:
        val = live.get("value")
        if not isinstance(val, (dict, list)) or ("raw_ms" in val if isinstance(val, dict) else False):
            try:
                w = await app.ads_call(app.ads.watch, [symbol], watch_ms, 20, True, timeout=watch_ms / 1000 + 30, slow=True)
                rep["watch"] = {"changes": w["changes"], "samples": w["samples"][:50]}
            except Exception as exc:  # noqa: BLE001
                rep["watch"] = {"error": str(exc)}
    return rep


async def fb_profile(app, instance: str, duration_s: float = 10.0, cycle_ms: float = 1.0,
                     task_index: int = 1) -> dict[str, Any]:
    """Record every leaf of an FB instance (change mode) while sampling task timing;
    summarise state dwell, boolean pulses and cost on the task."""
    rep: dict[str, Any] = {"instance": instance, "duration_s": duration_s}
    st = await app.ads_call(app.rec.start, symbols=[instance], prefix=None, pattern=None, mode="change", cycle_ms=cycle_ms,
                            duration_s=duration_s, max_events=2_000_000, start_when=None, stop_when=None, pre_trigger_ms=0,
                            name=f"profile {instance}", max_symbols=2000, timeout=120)
    rid = st["id"]
    rep["recording"] = rid
    try:
        rep["task"] = await app.ads_call(app.bench.task_stats, task_index, max(2, int(duration_s * 1000 / 100)), 100,
                                         timeout=duration_s + 60, slow=True)
    except Exception as exc:  # noqa: BLE001
        rep["task"] = {"error": str(exc)}
    deadline = time.time() + duration_s + 5
    while time.time() < deadline:
        if not (await app.ads_call(app.rec.status, rid, timeout=30))["active"]:
            break
        import asyncio
        await asyncio.sleep(0.25)
    out = await app.ads_call(app.rec.stop, rid, timeout=60)
    stats = out.get("stats") or {}
    active = {k: v for k, v in stats.items() if v.get("changes", 0) > 0}
    rep["events"] = out.get("events")
    rep["symbols_total"] = len(stats)
    rep["symbols_changing"] = len(active)
    rep["most_active"] = sorted(({"symbol": k, "changes": v["changes"]} for k, v in active.items()),
                                key=lambda x: -x["changes"])[:25]
    pulses = {}
    for k, v in list(active.items())[:40]:
        if isinstance(v.get("last"), bool):
            try:
                pz = await app.ads_call(app.rec.pulses, rid, k, 5, timeout=120)
                pulses[k] = {kk: pz.get(kk) for kk in ("pulses", "high_ms", "period_ms", "duty_percent")}
            except Exception:  # noqa: BLE001
                pass
    rep["pulses"] = pulses
    enums = {}
    for k, v in active.items():
        last = v.get("last")
        if isinstance(last, dict) and "name" in last or (isinstance(last, int) and not isinstance(last, bool) and k.split(".")[-1].lower().startswith("e")):
            try:
                tl = await app.ads_call(app.rec.pulses, rid, k, 20, timeout=120)
                enums[k] = {"dwell_ms": tl.get("dwell_ms"), "timeline": tl.get("timeline", [])[:20]}
            except Exception:  # noqa: BLE001
                pass
    rep["state_timelines"] = enums
    rep["silent_symbols"] = [k for k in stats if k not in active][:100]
    return rep


async def release_check(app, plc_project_path: str | None = None, run_build: bool = True,
                        lint_rules: list[str] | None = None) -> dict[str, Any]:
    """Go/no-go before deploying: uncommitted changes, build, lint, source-vs-runtime,
    TcUnit result on the target, task health. Nothing is written."""
    rep: dict[str, Any] = {"checks": {}, "blockers": [], "warnings": []}
    c = rep["checks"]
    try:
        ch = await app.proj_call(app.proj.changed_units)
        c["git"] = {"changed_units": len(ch["units"]), "units": ch["units"][:50]}
        if ch["units"]:
            rep["warnings"].append(f"{len(ch['units'])} source units differ from git {ch['since']}")
    except Exception as exc:  # noqa: BLE001
        c["git"] = {"skipped": str(exc)}
    if run_build:
        try:
            b = await app.xae_call(app.xae.build, False, False, timeout=app.cfg.xae.build_timeout_s + 60)
            c["build"] = {"success": b["success"], "errors": b["errors"], "warnings": b["warnings"], "items": b["items"][:30]}
            if not b["success"]:
                rep["blockers"].append("build failed")
        except Exception as exc:  # noqa: BLE001
            c["build"] = {"skipped": str(exc)}
            rep["warnings"].append("build not run (XAE unavailable)")
    try:
        li = await app.proj_call(codecheck.lint, app.proj, None, lint_rules, None)
        c["lint"] = {"findings": li["findings"], "by_rule": li["by_rule"], "warnings": [f for f in li["items"] if f["severity"] == "warning"][:30]}
        if any(f["severity"] == "warning" for f in li["items"]):
            rep["warnings"].append("lint warnings present")
    except Exception as exc:  # noqa: BLE001
        c["lint"] = {"skipped": str(exc)}
    try:
        from .server import _vs_runtime  # local import to avoid a cycle at module load
        vr = await _vs_runtime(app, None, 200_000)
        c["source_vs_runtime"] = {"declared": vr["declared"], "matched": vr["matched"], "missing_online": len(vr["missing_online"]),
                                  "extra_online": len(vr["extra_online"]), "sample_missing": vr["missing_online"][:10]}
        if vr["missing_online"]:
            rep["warnings"].append(f"{len(vr['missing_online'])} declared variables not online — target not running this source?")
    except Exception as exc:  # noqa: BLE001
        c["source_vs_runtime"] = {"skipped": str(exc)}
    try:
        tr = await app.ads_call(app.files.read_file, app.cfg.project.tcunit_remote_result, None, True, "utf-8", timeout=120, slow=True)
        from .project_backend import parse_tcunit_xml
        res = parse_tcunit_xml(tr["text"])
        c["tcunit"] = {"summary": res["summary"], "all_passed": res["all_passed"], "failed": res["failed_tests"][:20]}
        if not res["all_passed"]:
            rep["blockers"].append("TcUnit failures on the target")
    except Exception as exc:  # noqa: BLE001
        c["tcunit"] = {"skipped": str(exc)}
    try:
        ts = await app.ads_call(app.bench.task_stats, 1, 20, 50, timeout=60, slow=True)
        c["task"] = {k: ts.get(k) for k in ("task_name", "cycle_time_us", "exec_time_us", "load_percent", "exceeded_during_sample", "cycle_time_exceeded_total")}
        lp = (ts.get("load_percent") or {}).get("max")
        if lp and lp > 80:
            rep["warnings"].append(f"task load max {lp}%")
        if ts.get("exceeded_during_sample"):
            rep["blockers"].append("cycle time exceeded during sampling")
    except Exception as exc:  # noqa: BLE001
        c["task"] = {"skipped": str(exc)}
    try:
        c["app"] = await app.ads_call(app.bench.app_info, timeout=30)
    except Exception as exc:  # noqa: BLE001
        c["app"] = {"skipped": str(exc)}
    rep["verdict"] = "NO-GO" if rep["blockers"] else ("GO (with warnings)" if rep["warnings"] else "GO")
    return rep


async def project_overview(app) -> dict[str, Any]:
    """Orientation in one call: index summary, programs and their call trees, FB list with
    interfaces, lint summary, git state."""
    scan = await app.proj_call(app.proj.scan)
    rep: dict[str, Any] = {"index": {k: scan[k] for k in ("root", "files", "units", "by_kind", "plcproj", "duplicate_names")}}
    progs = await app.proj_call(app.proj.list_units, "PROGRAM", None, 50)
    rep["programs"] = []
    for pr in progs["units"]:
        try:
            g = await app.proj_call(codecheck.call_graph, app.proj, pr["name"], 3)
            rep["programs"].append({"name": pr["name"], "file": pr["file"], "calls": [n["name"] for n in g["nodes"][1:] if not n.get("repeat")][:40]})
        except Exception:  # noqa: BLE001
            rep["programs"].append({"name": pr["name"], "file": pr["file"]})
    fbs = await app.proj_call(app.proj.list_units, "FUNCTION_BLOCK", None, 200)
    rep["function_blocks"] = []
    for fb in fbs["units"][:60]:
        try:
            i = await app.proj_call(codecheck.fb_interface, app.proj, fb["name"])
            rep["function_blocks"].append({"name": fb["name"], "extends": i["extends"], "implements": i["implements"],
                                           "inputs": [v["name"] for v in i["inputs"]], "outputs": [v["name"] for v in i["outputs"]],
                                           "methods": [m["name"] for m in i["methods"]], "file": fb["file"]})
        except Exception:  # noqa: BLE001
            pass
    rep["gvls"] = [u["name"] for u in (await app.proj_call(app.proj.list_units, "GVL", None, 100))["units"]]
    rep["duts"] = [u["name"] for u in (await app.proj_call(app.proj.list_units, "STRUCT", None, 200))["units"]] + \
                  [u["name"] for u in (await app.proj_call(app.proj.list_units, "ENUM", None, 200))["units"]]
    try:
        li = await app.proj_call(codecheck.lint, app.proj, None, None, None)
        rep["lint"] = {"findings": li["findings"], "by_rule": li["by_rule"]}
    except Exception as exc:  # noqa: BLE001
        rep["lint"] = {"skipped": str(exc)}
    try:
        rep["git"] = await app.proj_call(app.proj.changed_units)
    except Exception as exc:  # noqa: BLE001
        rep["git"] = {"skipped": str(exc)}
    return rep


PROMPTS: dict[str, tuple[str, str]] = {
    "debug_symbol": ("Investigate why a PLC variable has the value it has", """\
Target symbol: {symbol}

1. wf_debug_symbol {symbol} — live value, declaring file:line, every writer, a short watch.
2. If it changes: ads_watch / rec_start on it plus the inputs the writers depend on; use rec_latency
   between the suspected cause and the symbol.
3. If it never changes: proj_call_graph from the program down to the writer; confirm the branch is
   reachable (read the condition variables with ads_read_many).
4. Report: observed value, expected value, the exact statement responsible (file:line), and the
   input condition that explains it. Only then propose the code change, as a proj_set_code diff.""" ),
    "fb_validation": ("Validate a function block against its specification on the live machine", """\
FB instance: {instance}   Spec / acceptance criteria: {criteria}

1. proj_interface on the FB type; ads_read {instance} for the current state.
2. Write a test_scenario: stimulus writes at times, wait_until for each expected state, expect for
   outputs, stable for settled analog values; record prefix={instance} at 1 ms change mode.
3. Run it (writes are gated; the server restores what it wrote). Then rec_stats / rec_pulses /
   rec_latency on the recording to quantify timing against the criteria.
4. ads_task_stats while it runs — the FB's cost on the task.
5. Deliver: pass/fail per criterion with measured numbers, the recording id, and the gaps.
   If it should be permanent, tcunit_scaffold the FB and port the scenario into TcUnit tests.""" ),
    "deploy_safely": ("Build, check and deploy a change to the target with evidence", """\
Change under test: {change}

1. proj_changed → which units differ from git; proj_lint on them; xae_build (stop on errors).
2. snap_save pre prefix=<affected GVLs/instances>.
3. wf_release_check — go/no-go. Do not deploy on NO-GO.
4. xae_deploy (confirmation will be requested). After restart: ads_status until RUN, proj_vs_runtime
   must show 0 missing_online for the changed units.
5. snap_compare pre live — explain every difference. tcunit_results from the target if tests exist.
6. Summarise: what changed, evidence it is running, what to watch.""" ),
    "performance_audit": ("Find what costs cycle time and where timing is marginal", """\
Scope: {scope}

1. ads_task_stats samples=200 interval_ms=20 — baseline load %, max exec time, overruns.
2. wf_fb_profile on each heavy FB instance in scope (10 s each): most active symbols, pulse rates.
3. rec_start prefix={scope} mode=cycle cycle_ms=<task cycle> for 30 s → rec_stats; look for symbols
   changing every cycle that should not (debounce/filters missing) and for state dwell times far
   above the design value.
4. proj_call_graph + proj_get_code on the hot paths; flag loops over large arrays, string handling,
   LREAL maths in high-frequency branches, blocking loops.
5. Report ranked by estimated cycle-time impact, each with evidence (numbers + file:line).""" ),
    "onboard_project": ("Get oriented in an unfamiliar TwinCAT project", """\
1. tc_doctor; wf_project_overview.
2. For each program: proj_call_graph depth 4; ads_leaves <program> with_values=true for the live picture.
3. proj_lint summary → quick wins. proj_vs_runtime → is the target running this source?
4. Write a one-page map: programs → FBs → I/O (xae_tree_get TIID depth 2), state machines (enum
   variables and their dwell from a 30 s rec_start prefix=MAIN), open risks.""" ),
}
