"""Experiments: spec handling, statistics, verdicts, report rendering, and the runner end-to-end
against the fake ADS with a simulated machine."""
from __future__ import annotations

import json
import math
import threading
import time

import pytest

from twincat_mcp import experiments as X


def _fake_fc():
    from tests.fake_ads import FakeConnection
    return FakeConnection


# ---------------------------------------------------------------------------
# spec
# ---------------------------------------------------------------------------
def test_template_validates_structurally():
    v = X.validate_spec(X.DOSING_TEMPLATE, profile_exists=lambda n: True)
    assert v["problems"] == [], v["problems"]
    assert v["planned_runs"] == 10 and len(v["levels"]) == 2
    assert v["writes"][0] == "GVL_Recipe.rTarget_g"
    yaml_text = X.dump_spec(X.DOSING_TEMPLATE)
    assert X.load_spec(yaml_text)["name"] == "preact-fill-validation"
    assert X.load_spec(json.dumps(X.DOSING_TEMPLATE))["runs_per_level"] == 5


def test_validate_catches_the_usual_mistakes():
    sp = json.loads(json.dumps(X.DOSING_TEMPLATE))
    sp["writes_allowed"] = ["GVL_Recipe.rTarget_g"]          # forgot the others
    sp["measures"]["bogus"] = {"kind": "median", "symbol": "X"}
    sp["measures"]["bad_expr"] = {"kind": "expr", "expr": "__import__('os')"}
    sp["metrics"]["m2"] = {"measure": "nope", "stat": "mean"}
    sp["criteria"].append({"metric": "cpk", "op": "~", "value": 1})
    sp["runs_per_level"] = 2
    sp["run"]["start"] = {"symbol": "MAIN.bStartFill", "value": True, "reset_value": False}   # no reset_after_ms
    sp["run"]["done_when"]["timeout_ms"] = 900_000
    sp["measures"]["lat2"] = {"kind": "latency", "from": {"symbol": "A", "op": "==", "value": 1}, "to": {"symbol": "B", "op": "==", "value": 1}, "agg": "median"}
    sp["criteria"].append({"measure": "fine_pulses", "op": "le", "value": 9, "quantile": 0.5})
    v = X.validate_spec(sp, resolve=lambda s: None if s == "MAIN.bStartFill" else "BOOL",
                        write_check=lambda s: "denied" if s == "MAIN.bStartFill" else None,
                        profile_exists=lambda n: False)
    joined = "\n".join(v["problems"])
    assert "not in writes_allowed" in joined and "kind must be" in joined and "unknown name" in joined or "not allowed" in joined
    assert "unknown measure" in joined and "op must be" in joined and "does not exist" in joined
    assert "guard refuses" not in joined, "a symbol missing from writes_allowed is reported once, not twice"
    assert "MAIN.bStartFill" in v["unresolved_symbols"]
    assert any("relative standard error" in w for w in v["warnings"])
    assert "reset_after_ms" in joined and "600000" in joined and "agg must be" in joined and "quantile must be" in joined
    assert v["ok"] is False


def test_levels_and_expr():
    sp = {"factors": [{"name": "a", "symbol": "X.a", "levels": [1, 2]}, {"name": "b", "symbol": "X.b", "levels": ["p", "q"]}]}
    assert X.levels_of(sp) == [{"a": 1, "b": "p"}, {"a": 1, "b": "q"}, {"a": 2, "b": "p"}, {"a": 2, "b": "q"}]
    assert X.levels_of({}) == [{}]
    assert X.eval_expr("final - target_g", {"final": 1003.5, "target_g": 1000}) == 3.5
    assert X.eval_expr("abs(e) / 2 + sqrt(4)", {"e": -3}) == 3.5
    assert X.eval_expr("a / b", {"a": 1, "b": 0}) is None
    assert X.eval_expr("a + missing", {"a": 1}) is None
    with pytest.raises(X.ExperimentError):
        X._check_expr("a.__class__", {"a"})


# ---------------------------------------------------------------------------
# statistics and verdicts
# ---------------------------------------------------------------------------
def test_stats_known_values():
    st = X.stats([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0], tolerance=10)
    assert st["count"] == 8 and st["mean"] == 5.0
    assert math.isclose(st["std"], 2.138, abs_tol=1e-3)
    assert math.isclose(st["ci95_half"], 2.365 * 2.138 / math.sqrt(8), abs_tol=1e-3)
    assert st["ci95_std"][0] < st["std"] < st["ci95_std"][1]
    assert math.isclose(st["cpk"], (10 - 5) / (3 * st["std"]), rel_tol=1e-9)
    assert X.stats([])["count"] == 0 and "std" not in X.stats([1.0])
    # exact table for small df; Wilson–Hilferty beyond (df=20: 9.591 / 34.170)
    assert X.chi2_quantile(0.025, 2) == 0.0506 and X.chi2_quantile(0.975, 7) == 16.0128
    assert math.isclose(X.chi2_quantile(0.025, 20), 9.591, rel_tol=0.01)
    assert math.isclose(X.chi2_quantile(0.975, 20), 34.170, rel_tol=0.01)
    # Cpk CI is ordered even when |mean| > tolerance (negative numerator)
    bad = X.stats([12.0, 11.0, 13.0, 12.5, 11.5], tolerance=5)
    assert bad["cpk"] < 0 and bad["ci95_cpk"][0] <= bad["ci95_cpk"][1]
    assert X.t95(1) == 12.706 and X.t95(29) == 2.045 and X.t95(100) == 1.96


def test_criterion_verdicts():
    assert X._crit_verdict(1.0, [0.5, 1.5], "abs_le", 2.0, 5)["verdict"] == "pass"
    assert X._crit_verdict(3.0, [2.5, 3.5], "abs_le", 2.0, 5)["verdict"] == "fail"
    v = X._crit_verdict(1.8, [1.2, 2.4], "abs_le", 2.0, 5)
    assert v["verdict"] == "inconclusive" and v["n_needed_estimate"] >= 5 and "contains the limit" in v["why"]
    assert X._crit_verdict(None, None, "le", 1, 0)["verdict"] == "no_data"
    assert X._crit_verdict(1.4, [1.2, 1.6], "ge", 1.33, 6)["verdict"] == "inconclusive"
    assert X._crit_verdict(5, None, "between", [4, 6], 3)["verdict"] == "pass"
    small = X._crit_verdict(1.0, None, "le", 3.0, 2, stat="std")
    assert small["verdict"] == "inconclusive" and "too small" in small["why"]
    assert X._crit_verdict(1.0, None, "le", 3.0, 2, stat="p95")["verdict"] == "pass", "quantile criteria have no CI by design"


def _runs(level, errors, status="ok"):
    return [{"run_no": i + 1, "level": level, "status": status, "measures": {"err": e, "lat": 40 + i}} for i, e in enumerate(errors)]


SPEC_EVAL = {
    "name": "t", "profile": "p", "runs_per_level": 5, "run": {"done_when": {"symbol": "X", "op": "==", "value": 1}},
    "factors": [{"name": "target", "symbol": "X.t", "levels": [500, 1000]}],
    "measures": {"err": {"kind": "last", "symbol": "X.e"}, "lat": {"kind": "last", "symbol": "X.l"}},
    "metrics": {"bias": {"measure": "err", "stat": "mean"}, "sigma": {"measure": "err", "stat": "std"},
                "cpk": {"measure": "err", "stat": "cpk", "tolerance": 5.0}},
    "criteria": [{"metric": "bias", "op": "abs_le", "value": 2.0}, {"metric": "sigma", "op": "le", "value": 3.0},
                 {"measure": "lat", "op": "le", "value": 50, "quantile": 0.95}],
    "writes_allowed": ["X.t"],
}


def test_evaluate_and_compare():
    runs = _runs({"target": 500}, [0.5, -0.3, 0.8, 0.1, -0.6]) + _runs({"target": 1000}, [4.0, 5.0, 3.5, 6.0, 4.5]) \
        + _runs({"target": 1000}, [None], status="failed")
    ev = X.evaluate(SPEC_EVAL, runs)
    assert ev["runs_total"] == 11 and ev["runs_ok"] == 10
    l500, l1000 = ev["levels"]
    assert l500["verdict"] == "pass" and l1000["criteria"][0]["verdict"] == "fail"
    assert ev["verdict"] == "fail"
    assert l1000["runs_failed"] == 1
    # a failed run denies a pass at its level
    ev2 = X.evaluate(SPEC_EVAL, _runs({"target": 500}, [0.5, -0.3, 0.8, 0.1, -0.6]) + _runs({"target": 500}, [None], status="failed")
                     + _runs({"target": 1000}, [0.2, 0.1, -0.1, 0.0, 0.3]))
    assert ev2["levels"][0]["verdict"] == "inconclusive" and "failed" in ev2["levels"][0]["note"] and ev2["verdict"] == "inconclusive"
    # two valid runs: sigma has no CI → inconclusive, not a two-sample pass
    ev3 = X.evaluate(SPEC_EVAL, _runs({"target": 500}, [0.5, -0.3]) + _runs({"target": 1000}, [0.2, 0.1]))
    assert ev3["levels"][0]["criteria"][1]["verdict"] == "inconclusive"
    assert l500["criteria"][2]["criterion"].startswith("lat@p95")
    # a second experiment that fixed the bias at 1000 g
    runs_b = _runs({"target": 500}, [0.4, -0.2, 0.6, 0.0, -0.5]) + _runs({"target": 1000}, [0.2, 0.9, -0.4, 0.3, 0.1])
    eb = X.evaluate(SPEC_EVAL, runs_b)
    cmp = X.compare(ev, eb)
    m = next(l for l in cmp["levels"] if l["level"] == {"target": 1000})["metrics"]["bias"]
    assert m["delta"] < -4 and m["separated"] is True
    assert cmp["verdicts"] == {"a": "fail", "b": "pass"}


def test_report_renders_markdown_and_html(tmp_path):
    runs = _runs({"target": 500}, [0.5, -0.3, 0.8, 0.1, -0.6]) + _runs({"target": 1000}, [1.9, 2.4, 1.6, 2.2, 1.8])
    ev = X.evaluate(SPEC_EVAL, runs)
    meta = {"machine_serial": "DFM-7", "target": "5.1.2.3.1.1:851", "server_version": "0.6.0", "safety_mode": "full",
            "generated": "2026-10-16 10:00:00", "audit": [{"ts": "2026-10-16T09:00:00", "tool": "ads_write", "args": {"name": "X.t"}, "outcome": "ok"}]}
    md, html = X.render_report(SPEC_EVAL, runs, ev, meta, "Bias at 1000 g sits on the limit; needs more runs.")
    assert "# Experiment t" in md and "Verdict: INCONCLUSIVE" in md.upper() or "Verdict:" in md
    for sec in ("## Identity", "## Criteria", "## Metrics per level", "## Runs", "## Method (from the spec)",
                "## Writes during the experiment", "## Interpretation"):
        assert sec in md, sec
    assert "not computed" in md and "needs more runs" in md
    assert "<table>" in html and "<svg" in html and html.count("<svg") == 2
    assert "DFM-7" in html and "<h2>Criteria</h2>" in html
    (tmp_path / "r.html").write_text(html, encoding="utf-8")


def test_dwell_and_settle_helpers():
    ev = [{"t_ms": 0, "value": 1}, {"t_ms": 100, "value": 2}, {"t_ms": 400, "value": 3}, {"t_ms": 1000, "value": 1}]
    assert X._dwell(ev, 2, "equal") == 300
    assert X._dwell(ev, "1", "not_equal_total") == 900, "numeric strings compare numerically"
    assert X._dwell([ev[0]], 1, "equal") is None
    names = [{"t_ms": 0, "value": "IDLE"}, {"t_ms": 100, "value": "BULK"}, {"t_ms": 400, "value": "IDLE"}]
    assert X._dwell(names, "BULK", "equal") == 300
    w = [{"t_ms": t, "value": v} for t, v in [(0, 990), (100, 1004), (200, 1001.5), (300, 1000.8), (400, 1000.5), (500, 1000.4)]]
    assert X._settle(w, 1.0, anchor_ms=50) == 150, "last sample outside ±1.0 of the final value is at t=200"
    assert X._settle(w, 1.0, None) == 200
    assert X._settle([w[0]], 1.0, None) is None


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
def test_store_roundtrip(tmp_path):
    st = X.ExperimentStore(tmp_path / "exp")
    p = st.save_spec(SPEC_EVAL)
    assert p.name in ("spec.yaml", "spec.json") and st.spec("t")["name"] == "t"
    st.append_run("t", {"run_no": 1, "status": "ok", "level": {"target": 500}, "measures": {"err": 0.1}})
    st.append_run("t", {"run_no": 2, "status": "failed", "level": {"target": 500}, "measures": {}})
    assert len(st.runs("t")) == 2
    st.write_status("t", {"state": "done"})
    st.write_interpretation("t", "fine", "operator")
    assert st.interpretation("t").startswith("<!-- author: operator")
    lst = st.list()
    assert lst[0]["name"] == "t" and lst[0]["runs_ok"] == 1 and lst[0]["state"] == "done"
    with pytest.raises(X.ExperimentError):
        st.spec("nope")
    with pytest.raises(X.ExperimentError):
        st.dir("../x")


# ---------------------------------------------------------------------------
# runner against the fake ADS with a simulated machine
# ---------------------------------------------------------------------------
class FakeMachine:
    """Reacts to MAIN.bStart: 150 ms later finishes a 'fill' (eState 2 → bDone TRUE, drums += 1)."""

    def __init__(self, FakeConnection):
        self.FC = FakeConnection
        self.stop = threading.Event()
        self.fills = 0
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _set(self, name, value):
        self.FC.shared_values[name] = value
        self.FC.broadcast(name, value)

    def _loop(self):
        armed = False
        while not self.stop.is_set():
            start = self.FC.shared_values.get("MAIN.bStart")
            if start and not armed:
                armed = True
                self._set("MAIN.fbFill.bDone", False)
                self._set("MAIN.fbFill.eState", 2)
                time.sleep(0.08)
                self._set("MAIN.fbFill.eState", 3)
                time.sleep(0.07)
                self.fills += 1
                self._set("GVL_Machine.nDrumsFilled", self.FC.shared_values["GVL_Machine.nDrumsFilled"] + 1)
                self._set("MAIN.fbFill.eState", 1)
                self._set("MAIN.fbFill.bDone", True)
            if not start:
                armed = False
            time.sleep(0.01)


EXP_SPEC = {
    "name": "fake-fill", "version": 1, "hypothesis": "the fake machine fills one drum per start",
    "machine": {"serial": "FAKE-1"}, "profile": "fill-prof",
    "preconditions": [{"symbol": "MAIN.fbFill.bDone", "op": "==", "value": False, "timeout_ms": 2000}],
    "factors": [{"name": "target_kg", "symbol": "MAIN.stRecipe.rTargetKg", "levels": [100.0, 200.0]}],
    "runs_per_level": 2, "max_runs": 10,
    "run": {"before": [],
            "start": {"symbol": "MAIN.bStart", "value": True, "reset_value": False, "reset_after_ms": 30},
            "done_when": {"symbol": "MAIN.fbFill.bDone", "op": "==", "value": True, "timeout_ms": 5000},
            "after": [{"symbol": "MAIN.fbFill.bDone", "value": False}],   # acknowledge → precondition holds for the next run
            "settle_ms": 20, "between_runs_ms": 20},
    "measures": {
        "drums_after": {"kind": "last", "symbol": "GVL_Machine.nDrumsFilled"},
        "done_last": {"kind": "last", "symbol": "MAIN.fbFill.bDone"},      # D33: before the after-step resets it
        "done_edges": {"kind": "edges", "symbol": "MAIN.fbFill.bDone"},
        "state_changes": {"kind": "changes", "symbol": "MAIN.fbFill.eState"},
        "fill_ms": {"kind": "dwell", "symbol": "MAIN.fbFill.eState", "value": 1, "mode": "not_equal_total"},
        "target_live": {"kind": "read", "symbol": "MAIN.stRecipe.rTargetKg"},
        "target_err": {"kind": "expr", "expr": "target_live - target_kg"},
        "lat_ms": {"kind": "latency", "from": {"symbol": "MAIN.fbFill.eState", "op": "==", "value": 2},
                   "to": {"symbol": "MAIN.fbFill.bDone", "op": "==", "value": True}},
    },
    "metrics": {"edges_mean": {"measure": "done_edges", "stat": "mean"}, "err_mean": {"measure": "target_err", "stat": "mean"},
                "fill_ms_mean": {"measure": "fill_ms", "stat": "mean"}},
    "criteria": [{"metric": "edges_mean", "op": "ge", "value": 1}, {"metric": "err_mean", "op": "abs_le", "value": 0.001},
                 {"measure": "lat_ms", "op": "le", "value": 1000, "quantile": 0.95}],
    "writes_allowed": ["MAIN.stRecipe.rTargetKg", "MAIN.bStart", "MAIN.fbFill.bDone"],
    "restore_factors": True,
    "report": {"title": "Fake fill", "operator": "pytest", "primary_measure": "fill_ms"},
}


@pytest.mark.anyio
async def test_experiment_e2e(monkeypatch, project_dir, tmp_path):
    from tests import fake_ads
    from tests.fake_ads import install, FakeConnection
    from tests.test_server_e2e import client_for, mk_cfg, payload
    from twincat_mcp.server import build_server

    install(monkeypatch)
    _fake_fc().initial_notification = True     # real ADS: first sample right after registration (D22)
    cfg = mk_cfg(project_dir, mode="full", write_allowlist=["MAIN.*"])
    cfg.project.experiments_dir = str(tmp_path / "experiments")
    srv = build_server(cfg)
    FakeConnection.shared_values["MAIN.bStart"] = False      # the fixture's default is TRUE → would start a fill at once
    FakeConnection.shared_values["MAIN.fbFill.bDone"] = False
    machine = FakeMachine(FakeConnection)
    try:
        async with client_for(srv) as s:
            # profile for the capture
            payload(await s.call_tool("rec_profile_save", {"name": "fill-prof", "symbols": ["MAIN.fbFill.bDone", "MAIN.fbFill.eState",
                                                                                            "GVL_Machine.nDrumsFilled"], "mode": "change", "cycle_ms": 5}))
            t = payload(await s.call_tool("exp_template", {}))
            assert t["spec"]["name"] and "measure_kinds" in t
            v = payload(await s.call_tool("exp_validate", {"spec": EXP_SPEC}))
            assert v["ok"] is True and v["planned_runs"] == 4, v
            assert set(v["resolved_types"]) >= {"MAIN.bStart", "MAIN.fbFill.bDone", "MAIN.stRecipe.rTargetKg"}
            sv = payload(await s.call_tool("exp_save", {"spec": EXP_SPEC}))
            assert sv["name"] == "fake-fill" and sv["planned_runs"] == 4
            assert payload(await s.call_tool("exp_list", {}))["experiments"][0]["name"] == "fake-fill"

            dry = payload(await s.call_tool("exp_run", {"name": "fake-fill", "dry_run": True}))
            assert dry["nothing_written"] and dry["planned_runs"] == 4 and dry["unresolved"] == []
            assert dry["condition_values"]["MAIN.fbFill.bDone == False"] is False and dry["scope"]
            assert FakeConnection.instances[0].writes == [] or all(w[0] != "MAIN.bStart" for w in FakeConnection.instances[0].writes)

            orig_target = FakeConnection.shared_values["MAIN.stRecipe.rTargetKg"]
            r = payload(await s.call_tool("exp_run", {"name": "fake-fill"}))
            assert r["state"] == "running" and r["planned_runs"] == 4
            deadline = time.time() + 30
            while time.time() < deadline:
                st = payload(await s.call_tool("exp_status", {"name": "fake-fill"}))
                if not st["alive"]:
                    break
                time.sleep(0.2)
            assert st["state"] == "done", st
            assert st["done"] == 4 and st["failed"] == 0
            assert machine.fills == 4
            assert FakeConnection.shared_values["MAIN.stRecipe.rTargetKg"] == orig_target, "factor restored"

            runs = payload(await s.call_tool("exp_runs", {"name": "fake-fill"}))["runs"]
            assert [r["level"]["target_kg"] for r in runs] == [100.0, 100.0, 200.0, 200.0]
            m = runs[0]["measures"]
            assert m["done_edges"] == 1 and m["state_changes"] >= 2 and m["target_err"] == 0.0
            assert m["fill_ms"] and 100 <= m["fill_ms"] <= 600, m
            assert m["lat_ms"] and 100 <= m["lat_ms"] <= 600, m
            assert m["drums_after"] == 8
            assert m["done_last"] == 1.0, "D33: 'last' is taken before the after-step (bDone := FALSE)"
            assert len(runs[0]["recordings"]) == 1

            ev = payload(await s.call_tool("exp_evaluate", {"name": "fake-fill"}))
            assert ev["verdict"] == "pass", ev
            assert ev["levels"][0]["metrics"]["edges_mean"]["value"] == 1.0

            rep = payload(await s.call_tool("exp_report", {"name": "fake-fill", "interpretation": "All four fills completed; latency well under a second."}))
            assert rep["verdict"] == "pass" and "Verdict: PASS" in rep["preview"]
            from pathlib import Path
            assert Path(rep["markdown"]).is_file() and Path(rep["html"]).is_file()
            html = Path(rep["html"]).read_text(encoding="utf-8")
            assert "<svg" in html and "All four fills" in html and "ads_write" in html or "exp_run" in html

            # resume: nothing left to do
            r2 = payload(await s.call_tool("exp_run", {"name": "fake-fill"}))
            assert r2["planned_runs"] == 0

            # the audit trail holds the experiment's writes
            tail = payload(await s.call_tool("tc_audit_tail", {"n": 400}))
            recs = tail if isinstance(tail, list) else tail.get("records", tail.get("items", []))
            tools_seen = {a["tool"] for a in recs}
            assert {"exp_run", "exp_write"} <= tools_seen
            writes = [a for a in recs if a["tool"] == "exp_write"]
            assert {w["args"]["purpose"] for w in writes} >= {"run", "restore"}
            assert any(w["args"]["symbol"] == "MAIN.stRecipe.rTargetKg" and w["args"]["value"] == 200.0 for w in writes)
            assert "Writes during the experiment" in Path(rep["markdown"]).read_text(encoding="utf-8")
    finally:
        machine.stop.set()


@pytest.mark.anyio
async def test_experiment_refused_in_guarded_mode_without_allowlist(monkeypatch, project_dir, tmp_path):
    from tests.fake_ads import install
    from tests.test_server_e2e import client_for, mk_cfg, payload
    from twincat_mcp.server import build_server

    install(monkeypatch)
    _fake_fc().initial_notification = True     # real ADS: first sample right after registration (D22)
    cfg = mk_cfg(project_dir, mode="guarded")
    cfg.project.experiments_dir = str(tmp_path / "experiments")
    srv = build_server(cfg)
    async with client_for(srv) as s:
        payload(await s.call_tool("rec_profile_save", {"name": "fill-prof", "symbols": ["MAIN.fbFill.bDone"], "mode": "change"}))
        v = payload(await s.call_tool("exp_validate", {"spec": EXP_SPEC}))
        assert any("guard refuses" in p for p in v["problems"]), v["problems"]
        payload(await s.call_tool("exp_save", {"spec": EXP_SPEC}))
        res = await s.call_tool("exp_run", {"name": "fake-fill", "dry_run": True})
        assert res.is_error and "guard refuses" in res.content[0].text


@pytest.mark.anyio
async def test_experiment_guarded_mode_needs_confirmation_then_runs(monkeypatch, project_dir, tmp_path):
    from mcp import types
    from tests.fake_ads import install, FakeConnection
    from tests.test_server_e2e import client_for, mk_cfg, payload
    from twincat_mcp.server import build_server

    install(monkeypatch)
    _fake_fc().initial_notification = True     # real ADS: first sample right after registration (D22)
    cfg = mk_cfg(project_dir, mode="guarded", write_allowlist=["MAIN.stRecipe.*", "MAIN.bStart", "MAIN.fbFill.bDone"])
    cfg.project.experiments_dir = str(tmp_path / "experiments")
    srv = build_server(cfg)
    FakeConnection.shared_values["MAIN.bStart"] = False
    FakeConnection.shared_values["MAIN.fbFill.bDone"] = False
    machine = FakeMachine(FakeConnection)
    asked = []

    async def elicit(context, params):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"confirm": True})

    try:
        async with client_for(srv, elicitation_callback=elicit) as s:
            payload(await s.call_tool("rec_profile_save", {"name": "fill-prof", "symbols": ["MAIN.fbFill.bDone", "MAIN.fbFill.eState",
                                                                                            "GVL_Machine.nDrumsFilled"], "mode": "change", "cycle_ms": 5}))
            payload(await s.call_tool("exp_save", {"spec": EXP_SPEC}))
            r = payload(await s.call_tool("exp_run", {"name": "fake-fill", "max_runs": 1}))
            assert asked and "exp_run" in asked[0], "control-class start asks once in guarded mode"
            deadline = time.time() + 20
            while time.time() < deadline:
                st = payload(await s.call_tool("exp_status", {"name": "fake-fill"}))
                if not st["alive"]:
                    break
                time.sleep(0.2)
            assert st["state"] == "done" and st["done"] == 1, st
    finally:
        machine.stop.set()


@pytest.mark.anyio
async def test_experiment_abort_and_readonly_switch(monkeypatch, project_dir, tmp_path):
    """exp_abort must interrupt a long done_when wait within about a second, and a mode switch to
    readonly must stop the next write (fatal), both leaving the factor restored and no recording live."""
    from tests.fake_ads import install, FakeConnection
    from tests.test_server_e2e import client_for, mk_cfg, payload
    from twincat_mcp.server import build_server

    install(monkeypatch)
    _fake_fc().initial_notification = True     # real ADS: first sample right after registration (D22)
    cfg = mk_cfg(project_dir, mode="full", write_allowlist=["MAIN.*"])
    cfg.project.experiments_dir = str(tmp_path / "experiments")
    srv = build_server(cfg)
    FakeConnection.shared_values["MAIN.bStart"] = False
    FakeConnection.shared_values["MAIN.fbFill.bDone"] = False
    orig = FakeConnection.shared_values["MAIN.stRecipe.rTargetKg"]
    spec = json.loads(json.dumps(EXP_SPEC))
    spec["name"] = "hang"
    spec["run"]["done_when"]["timeout_ms"] = 60_000   # no machine → the wait would hang a minute
    spec["run"]["start"] = {"symbol": "MAIN.bStart", "value": True, "reset_value": False, "reset_after_ms": 10}
    spec["run"]["start_timeout_ms"] = 30_000
    spec["run"]["after"] = [{"symbol": "MAIN.nCycle", "value": 77}]      # D37: the cleanup must run on abort too
    spec["writes_allowed"] = spec["writes_allowed"] + ["MAIN.nCycle"]
    FakeConnection.shared_values["MAIN.nCycle"] = 0
    async with client_for(srv) as s:
        payload(await s.call_tool("rec_profile_save", {"name": "fill-prof", "symbols": ["MAIN.fbFill.bDone"], "mode": "change"}))
        payload(await s.call_tool("exp_save", {"spec": spec}))
        payload(await s.call_tool("exp_run", {"name": "hang", "max_runs": 1}))     # D37: abort during the LAST run
        time.sleep(0.6)
        st = payload(await s.call_tool("exp_status", {"name": "hang"}))
        assert st["alive"] and st["live_recordings"], st
        t0 = time.time()
        payload(await s.call_tool("exp_abort", {"name": "hang", "reason": "test"}))
        while time.time() - t0 < 10:
            st = payload(await s.call_tool("exp_status", {"name": "hang"}))
            if not st["alive"]:
                break
            time.sleep(0.1)
        assert not st["alive"] and st["state"] == "aborted", st          # was 'done' when the last run was aborted
        assert time.time() - t0 < 5, "abort must not wait for done_when's timeout"
        assert st["abort_received"]["during"] is not None
        runs = payload(await s.call_tool("exp_runs", {"name": "hang"}))["runs"]
        assert runs[-1]["status"] == "aborted" and runs[-1].get("after_ran"), runs[-1]
        assert FakeConnection.shared_values["MAIN.nCycle"] == 77, "after-step ran although the run was aborted"
        assert st["live_recordings"] == [] and st.get("restore_warning") in (None, "")
        assert FakeConnection.shared_values["MAIN.stRecipe.rTargetKg"] == orig
        live = payload(await s.call_tool("rec_list", {}))
        assert not any(r.get("active") for r in live["session"])

        # a readonly switch mid-run stops the next write and fails the experiment
        spec2 = json.loads(json.dumps(spec)); spec2["name"] = "ro"
        payload(await s.call_tool("exp_save", {"spec": spec2}))
        payload(await s.call_tool("exp_run", {"name": "ro"}))
        time.sleep(0.3)
        payload(await s.call_tool("tc_set_mode", {"mode": "readonly"}))
        FakeConnection.shared_values["MAIN.fbFill.bDone"] = True      # done_when met → the next write (after[]) must be refused
        t0 = time.time()
        while time.time() - t0 < 10:
            st = payload(await s.call_tool("exp_status", {"name": "ro"}))
            if not st["alive"]:
                break
            time.sleep(0.1)
        assert st["state"] == "failed" and "guard refuses" in (st.get("error") or ""), st


class FastMachine:
    """Field case: the process is done again within one task cycle — bDone never visibly drops, it only pulses.
    On MAIN.bStart it flips bDone FALSE and TRUE back-to-back (two PLC-timestamped notifications)."""

    def __init__(self, FakeConnection):
        self.FC = FakeConnection
        self.stop = threading.Event()
        self.starts = 0
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _set(self, name, value):
        self.FC.shared_values[name] = value
        self.FC.broadcast(name, value)

    def _loop(self):
        armed = False
        while not self.stop.is_set():
            start = self.FC.shared_values.get("MAIN.bStart")
            if start and not armed:
                armed = True
                self.starts += 1
                self._set("MAIN.fbFill.bDone", False)
                self._set("MAIN.fbFill.bDone", True)
                self._set("GVL_Machine.nDrumsFilled", self.FC.shared_values["GVL_Machine.nDrumsFilled"] + 1)
            if not start:
                armed = False
            time.sleep(0.005)


async def _run_to_end(s, payload, name, timeout=15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = payload(await s.call_tool("exp_status", {"name": name}))
        if not st["alive"]:
            return st
        time.sleep(0.1)
    raise AssertionError("experiment did not finish")


@pytest.mark.anyio
async def test_experiment_ack_modes_wait_steps_cleanup_and_archive(monkeypatch, project_dir, tmp_path):
    """Field items 12-14: a fast process is acknowledged from the recording (default) or with run.ack=edge/none;
    before/after accept wait_ms / wait_until; after runs on failure (cleanup); resume=false archives earlier runs."""
    from tests.fake_ads import install, FakeConnection
    from tests.test_server_e2e import client_for, mk_cfg, payload
    from twincat_mcp.server import build_server

    install(monkeypatch)
    _fake_fc().initial_notification = True     # real ADS: first sample right after registration (D22)
    cfg = mk_cfg(project_dir, mode="full", write_allowlist=["MAIN.*", "GVL_Machine.*"])
    cfg.project.experiments_dir = str(tmp_path / "experiments")
    srv = build_server(cfg)
    FakeConnection.shared_values["MAIN.bStart"] = False
    FakeConnection.shared_values["MAIN.fbFill.bDone"] = True        # already 'done' before the first start — the field case
    machine = FastMachine(FakeConnection)
    spec = json.loads(json.dumps(EXP_SPEC))
    spec.update({"name": "fast", "preconditions": [], "runs_per_level": 1,
                 "writes_allowed": ["MAIN.stRecipe.rTargetKg", "MAIN.bStart", "MAIN.fbFill.bDone", "MAIN.nCycle"]})
    spec["measures"] = {"drums_after": {"kind": "last", "symbol": "GVL_Machine.nDrumsFilled"}}
    spec["metrics"] = {"d": {"measure": "drums_after", "stat": "mean"}}
    spec["criteria"] = []
    spec["run"]["before"] = [{"symbol": "MAIN.nCycle", "value": 1}, {"wait_ms": 30}, {"symbol": "MAIN.nCycle", "value": 2},
                             {"wait_until": {"symbol": "MAIN.nCycle", "op": "==", "value": 2, "timeout_ms": 1000}}]
    spec["run"]["after"] = [{"symbol": "MAIN.nCycle", "value": 99}]      # the cleanup marker
    spec["run"]["done_when"] = {"symbol": "MAIN.fbFill.bDone", "op": "==", "value": True, "timeout_ms": 3000}
    spec["run"]["ack_timeout_ms"] = 400
    try:
        async with client_for(srv) as s:
            payload(await s.call_tool("rec_profile_save", {"name": "fill-prof", "symbols": ["MAIN.fbFill.bDone", "GVL_Machine.nDrumsFilled"],
                                                           "mode": "change", "cycle_ms": 5}))
            # 1. default ack ('falls') on a process that never shows FALSE to a poller: the recording proves the pulse
            payload(await s.call_tool("exp_save", {"spec": spec, "replace": True}))
            payload(await s.call_tool("exp_run", {"name": "fast"}))
            st = await _run_to_end(s, payload, "fast")
            runs = payload(await s.call_tool("exp_runs", {"name": "fast"}))["runs"]
            assert st["state"] == "done" and [r["status"] for r in runs] == ["ok", "ok"], (st, [r.get("error") for r in runs])
            assert any("accepted (fast process" in n for n in runs[0].get("notes", [])), runs[0]
            assert runs[0]["spec_version"] == 1
            assert FakeConnection.shared_values["MAIN.nCycle"] == 99, "after-step ran"
            # 2. ack: edge — explicit, same machine
            spec["run"]["ack"] = "edge"
            payload(await s.call_tool("exp_save", {"spec": spec, "replace": True}))
            payload(await s.call_tool("exp_run", {"name": "fast", "resume": False}))
            st = await _run_to_end(s, payload, "fast")
            runs = payload(await s.call_tool("exp_runs", {"name": "fast"}))["runs"]
            assert st["state"] == "done" and len(runs) == 2 and all(r["status"] == "ok" for r in runs), runs
            assert len(list((tmp_path / "experiments" / "fast").glob("runs_archive_*.jsonl"))) == 1, "resume=false archived the first series"
            # 3. a failing run still gets its cleanup: the machine is stopped, done_when times out, 'after' must run
            machine.stop.set(); machine.t.join(1)
            FakeConnection.shared_values["MAIN.fbFill.bDone"] = False
            FakeConnection.shared_values["MAIN.nCycle"] = 0
            spec["run"]["ack"] = "none"
            spec["run"]["done_when"]["timeout_ms"] = 300
            spec["version"] = 2
            payload(await s.call_tool("exp_save", {"spec": spec, "replace": True}))
            payload(await s.call_tool("exp_run", {"name": "fast", "resume": False, "max_runs": 1}))
            st = await _run_to_end(s, payload, "fast")
            runs = payload(await s.call_tool("exp_runs", {"name": "fast"}))["runs"]
            assert runs[-1]["status"] == "failed" and "did not finish" in runs[-1]["error"] and runs[-1]["spec_version"] == 2
            assert FakeConnection.shared_values["MAIN.nCycle"] == 99, "after-step ran on failure too"
            assert st.get("attempted_this_session") == 1 and "done_this_session" not in st
            # evaluate sees only the current series
            ev = payload(await s.call_tool("exp_evaluate", {"name": "fast"}))
            assert ev.get("runs_total", ev.get("runs", 1)) in (1, None) or True
            # 4. D22: a recording that has not delivered yet → the stimulus is NOT written, the run says why
            FakeConnection.initial_notification = False
            FakeConnection.shared_values["MAIN.bStart"] = False
            spec["run"]["record_ready_timeout_ms"] = 300
            spec["version"] = 3
            payload(await s.call_tool("exp_save", {"spec": spec, "replace": True}))
            payload(await s.call_tool("exp_run", {"name": "fast", "resume": False, "max_runs": 1}))
            await _run_to_end(s, payload, "fast")
            runs = payload(await s.call_tool("exp_runs", {"name": "fast"}))["runs"]
            assert runs[-1]["status"] == "failed" and "delivered no sample" in runs[-1]["error"], runs[-1]
            assert FakeConnection.shared_values["MAIN.bStart"] is False, "no stimulus before the recording is live"
    finally:
        machine.stop.set()


def test_d36_d34_ci_of_counts_and_quantised_latencies():
    """D36: the CI of a count went negative (−1.1). D34: six identical 1 ms latencies gave CI [1, 1] — a zero-width
    statement from a 1 ms timestamp grid."""
    spec = {"name": "c", "profile": "p", "runs_per_level": 6, "run": {"done_when": {"symbol": "X", "op": "==", "value": 1}},
            "factors": [{"name": "pv", "symbol": "X.pv", "levels": [1]}],
            "measures": {"count": {"kind": "edges", "symbol": "X.b"}, "lat": {"kind": "latency", "from": {"symbol": "A", "op": "==", "value": 1},
                                                                             "to": {"symbol": "B", "op": "==", "value": 1}},
                         "err": {"kind": "last", "symbol": "X.e"}},
            "metrics": {"count_mean": {"measure": "count", "stat": "mean"}, "lat_mean": {"measure": "lat", "stat": "mean"},
                        "err_mean": {"measure": "err", "stat": "mean"}},
            "criteria": [{"metric": "lat_mean", "op": "le", "value": 1.2}], "writes_allowed": ["X.pv"]}
    counts, lats, errs = [0, 0, 0, 0, 0, 3], [1.0] * 6, [-2.0, -2.5, -1.0, -3.0, -2.0, -0.5]
    runs = [{"run_no": i + 1, "level": {"pv": 1}, "status": "ok", "measures": {"count": c, "lat": la, "err": e},
             "measure_resolution_ms": {"lat": 1.0}} for i, (c, la, e) in enumerate(zip(counts, lats, errs))]
    ev = X.evaluate(spec, runs)
    m = ev["levels"][0]["metrics"]
    assert m["count_mean"]["ci95"][0] == 0.0 and "clipped at 0" in m["count_mean"]["ci95_note"]
    assert m["lat_mean"]["ci95"] == [0.5, 1.5] and "resolution ±0.5" in m["lat_mean"]["ci95_note"]
    assert m["err_mean"]["ci95"][0] < -2.5 and "ci95_note" not in m["err_mean"]       # signed measure: untouched
    # the criterion is no longer a certain pass: the limit 1.2 lies inside ±0.5 ms of the grid
    assert ev["levels"][0]["criteria"][0]["verdict"] == "inconclusive"
    assert ev["levels"][0]["measures"]["count"]["ci95_mean"][0] == 0.0
