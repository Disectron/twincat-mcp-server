"""Experiments: define → run → measure → evaluate → report, for commissioning and validation.

The recorder captures and the test bench stimulates; this layer turns *runs* into *conclusions*
an engineer (or a customer) can sign. Everything numeric here is deterministic: the same runs
produce the same metrics, verdicts and report. The agent designs the spec, supervises the run,
and writes the interpretation — which the report marks as interpretation, separate from what
was computed.

Spec (YAML or JSON; see ``DOSING_TEMPLATE``)::

    name, hypothesis, machine{serial, target}, profile (capture profile), preconditions[],
    factors[{name, symbol, levels[]}] → cartesian product = levels,
    runs_per_level, max_runs, run{before[], start{symbol,value,reset_value,reset_after_ms},
    done_when{symbol,op,value,timeout_ms}, after[], settle_ms, between_runs_ms},
    measures{name: {kind, ...}}, metrics{name: {measure, stat, tolerance}},
    criteria[{metric|measure, op, value, quantile}], writes_allowed[], restore_factors,
    snapshot_prefix, report{title, operator, primary_measure, notes}

Measure kinds (all from the run's recordings, PLC-timestamped): first, last, min, max, mean,
delta (last-first), changes, edges (rising), dwell (time symbol == value), latency (from-event →
to-event), pulses (count; field= picks high_ms.mean etc.), settle (time from an anchor event until
the symbol stays within ±tolerance of its final value), read (live ADS read at run end), expr
(arithmetic over other measures and factor values).

Statistics: mean with a t-based 95 % CI, σ (n-1) with a χ² CI (Wilson–Hilferty), Cpk =
(tol − |mean|)/(3σ) as the verifier defines it. A criterion whose limit lies inside the CI is
*inconclusive*, with the run count that would (roughly) settle it. Small n is reported as small
n; nothing here pretends five fills are a population.

Safety: every symbol the spec writes must be listed in ``writes_allowed`` *and* pass the server's
guard (mode / allow / deny) — checked before the first write, so a refused experiment moves
nothing. Factor values are read before the experiment and restored after it (or on abort).
"""
from __future__ import annotations

import ast
import itertools
import json
import math
import operator
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:  # optional; JSON always works
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore

MEASURE_KINDS = ("first", "last", "min", "max", "mean", "delta", "changes", "edges", "dwell", "latency",
                 "pulses", "settle", "read", "expr")
STATS = ("mean", "std", "min", "max", "median", "p95", "count", "cpk", "range")
CRIT_OPS = ("le", "ge", "lt", "gt", "abs_le", "between")
_OPS = {"==": operator.eq, "!=": operator.ne, ">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}

# two-sided 95 % t critical values, df = 1..30
_T95 = [12.706, 4.303, 3.182, 2.776, 2.571, 2.447, 2.365, 2.306, 2.262, 2.228, 2.201, 2.179, 2.160, 2.145, 2.131,
        2.120, 2.110, 2.101, 2.093, 2.086, 2.080, 2.074, 2.069, 2.064, 2.060, 2.056, 2.052, 2.048, 2.045, 2.042]


class ExperimentError(Exception):
    pass


class FatalRunError(ExperimentError):
    """A run failure after which the experiment must not continue (guard refusal, target change,
    write with unknown outcome)."""


_INVERSE = {"==": "!=", "!=": "==", "<": ">=", ">=": "<", ">": "<=", "<=": ">"}


# ---------------------------------------------------------------------------
# template
# ---------------------------------------------------------------------------
DOSING_TEMPLATE: dict[str, Any] = {
    "name": "preact-fill-validation",
    "version": 1,
    "hypothesis": "With lambda=0.95 the preact controller holds |bias| <= 2 g and sigma <= 3 g at 1000 g targets.",
    "machine": {"serial": "DFM-0001", "target": None},
    "profile": "preact-fill",            # <- a capture profile saved with rec_profile_save BEFORE exp_save; the name is yours
    "preconditions": [
        {"symbol": "MAIN.fbScale.bStable", "op": "==", "value": True, "timeout_ms": 30000},
        {"symbol": "MAIN.fbDispense.ePhase", "op": "==", "value": "IDLE", "timeout_ms": 10000},
    ],
    "factors": [
        {"name": "target_g", "symbol": "GVL_Recipe.rTarget_g", "levels": [500, 1000]},
        {"name": "lambda", "symbol": "MAIN.fbDispense.fbPreact.rLambda", "levels": [0.95]},
    ],
    "runs_per_level": 5,
    "max_runs": 40,
    "run": {
        "before": [{"symbol": "MAIN.fbDispense.bAckResult", "value": True}],
        "start": {"symbol": "MAIN.bStartFill", "value": True, "reset_value": False, "reset_after_ms": 200},
        "done_when": {"symbol": "MAIN.fbDispense.ePhase", "op": "==", "value": "IDLE", "timeout_ms": 180000},
        "after": [],
        "settle_ms": 2000,
        "between_runs_ms": 3000,
    },
    "measures": {
        "final_weight_g": {"kind": "last", "symbol": "MAIN.fbScale.rNet_g"},
        "final_error_g": {"kind": "expr", "expr": "final_weight_g - target_g"},
        "cut_latency_ms": {"kind": "latency", "from": {"symbol": "MAIN.fbDispense.bCut", "op": "==", "value": True},
                           "to": {"symbol": "MAIN.fbFlow.rFlow_gps", "op": "<", "value": 5.0}, "agg": "first"},
        "fine_pulses": {"kind": "pulses", "symbol": "MAIN.fbFine.bPulse"},
        "pulse_width_ms": {"kind": "pulses", "symbol": "MAIN.fbFine.bPulse", "field": "high_ms.mean"},
        "settle_ms": {"kind": "settle", "symbol": "MAIN.fbScale.rNet_g", "tolerance": 2.0,
                      "from": {"symbol": "MAIN.fbDispense.bCut", "op": "==", "value": True}},
        "tau_after": {"kind": "last", "symbol": "MAIN.fbDispense.fbPreact.rTau"},
        "cusum_trips": {"kind": "edges", "symbol": "MAIN.fbDispense.fbPreact.bCusumTrip"},
        "fill_time_ms": {"kind": "dwell", "symbol": "MAIN.fbDispense.ePhase", "value": "BULK", "mode": "not_equal_total"},
    },
    "metrics": {
        "bias_g": {"measure": "final_error_g", "stat": "mean"},
        "sigma_g": {"measure": "final_error_g", "stat": "std"},
        "cpk": {"measure": "final_error_g", "stat": "cpk", "tolerance": 5.0},
        "cut_latency_p95_ms": {"measure": "cut_latency_ms", "stat": "p95"},
        "pulses_mean": {"measure": "fine_pulses", "stat": "mean"},
    },
    "criteria": [
        {"metric": "bias_g", "op": "abs_le", "value": 2.0},
        {"metric": "sigma_g", "op": "le", "value": 3.0},
        {"metric": "cpk", "op": "ge", "value": 1.33},
        {"metric": "cut_latency_p95_ms", "op": "le", "value": 60},
    ],
    "writes_allowed": ["GVL_Recipe.rTarget_g", "MAIN.fbDispense.fbPreact.rLambda", "MAIN.bStartFill", "MAIN.fbDispense.bAckResult"],
    "restore_factors": True,
    "snapshot_prefix": "MAIN.fbDispense.fbPreact",
    "report": {"title": "Preact controller validation — 500/1000 g", "operator": "", "primary_measure": "final_error_g",
               "notes": "Material and ambient temperature recorded by operator."},
}


# ---------------------------------------------------------------------------
# spec handling
# ---------------------------------------------------------------------------
def load_spec(text_or_obj: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(text_or_obj, dict):
        return json.loads(json.dumps(text_or_obj))
    txt = (text_or_obj or "").strip()
    if not txt:
        raise ExperimentError("spec is empty")
    if txt.startswith("{"):
        try:
            data = json.loads(txt)
        except json.JSONDecodeError as exc:
            raise ExperimentError(f"spec is not valid JSON: {exc.msg} at line {exc.lineno}, column {exc.colno}") from exc
    else:
        if yaml is None:
            raise ExperimentError("spec is not JSON and PyYAML is not installed (pip install pyyaml)")
        try:
            data = yaml.safe_load(txt)
        except yaml.YAMLError as exc:      # R1: surfaced as "Error executing tool exp_validate" with no message
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
            problem = getattr(exc, "problem", None) or str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            raise ExperimentError(f"spec is not valid YAML: {problem}{where}") from exc
    if not isinstance(data, dict):
        raise ExperimentError(f"spec must be a mapping (got {type(data).__name__})")
    return data


def dump_spec(spec: dict[str, Any]) -> str:
    if yaml is not None:
        return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
    return json.dumps(spec, indent=2)


def levels_of(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Cartesian product of factor levels → [{factor_name: value}]. No factors → one empty level."""
    factors = spec.get("factors") or []
    if not factors:
        return [{}]
    names = [f["name"] for f in factors]
    return [dict(zip(names, combo)) for combo in itertools.product(*[f["levels"] for f in factors])]


def writes_in_spec(spec: dict[str, Any]) -> list[str]:
    syms = [f["symbol"] for f in spec.get("factors") or []]
    run = spec.get("run") or {}
    syms += [w["symbol"] for w in (list(run.get("before") or []) + list(run.get("after") or [])) if "symbol" in w and "value" in w]
    if run.get("start"):
        syms.append(run["start"]["symbol"])
    out: list[str] = []
    for s in syms:
        if s.lower() not in {o.lower() for o in out}:
            out.append(s)
    return out


def reads_in_spec(spec: dict[str, Any]) -> list[str]:
    syms: list[str] = [p["symbol"] for p in spec.get("preconditions") or []]
    run = spec.get("run") or {}
    if run.get("done_when"):
        syms.append(run["done_when"]["symbol"])
    for m in (spec.get("measures") or {}).values():
        for key in ("symbol",):
            if m.get(key):
                syms.append(m[key])
        for key in ("from", "to"):
            if isinstance(m.get(key), dict) and m[key].get("symbol"):
                syms.append(m[key]["symbol"])
    out: list[str] = []
    for s in syms:
        if s.lower() not in {o.lower() for o in out}:
            out.append(s)
    return out


def validate_spec(spec: dict[str, Any], resolve: Callable[[str], str | None] | None = None,
                  write_check: Callable[[str], str | None] | None = None,
                  profile_exists: Callable[[str], bool] | None = None,
                  profile_symbols: list[str] | None = None) -> dict[str, Any]:
    """Structural validation plus optional symbol resolution (``resolve(symbol) -> type or None``)
    and guard checks (``write_check(symbol) -> reason or None``)."""
    problems: list[str] = []
    warnings: list[str] = []
    if not re.fullmatch(r"[A-Za-z0-9_\-\.]{1,64}", str(spec.get("name", ""))):
        problems.append("name: letters, digits, _ - . (max 64)")
    if not spec.get("profile"):
        problems.append("profile: a capture profile name is required (rec_profile_save first)")
    elif profile_exists and not profile_exists(spec["profile"]):
        problems.append(f"profile {spec['profile']!r} does not exist (rec_profile_list)")
    run = spec.get("run") or {}
    if not run.get("done_when"):
        problems.append("run.done_when is required (how does a run end?)")
    else:
        dw = run["done_when"]
        if dw.get("op") not in _OPS:
            problems.append(f"run.done_when.op must be one of {list(_OPS)}")
        if not dw.get("timeout_ms"):
            warnings.append("run.done_when.timeout_ms missing → 600000 ms")
        if int(dw.get("timeout_ms") or 0) > 600_000:
            problems.append("run.done_when.timeout_ms above 600000 is capped by wait_until; use ≤ 600000")
    if not run.get("start"):
        warnings.append("run.start missing: the experiment relies on the machine starting runs by itself")
    elif "reset_value" in run["start"] and run["start"].get("reset_after_ms") is None:
        problems.append("run.start.reset_value needs reset_after_ms (otherwise the stimulus stays set forever)")
    for w in list(run.get("on_abort") or []):
        if not w.get("symbol") or "value" not in w:
            problems.append("run.on_abort entries need symbol and value")
    for where in ("before", "after"):
        for w in list(run.get(where) or []):
            if "wait_ms" in w or isinstance(w.get("wait_until"), dict):
                continue
            if not w.get("symbol") or "value" not in w:
                problems.append(f"run.{where} entries need symbol+value, or wait_ms, or wait_until{{symbol,op,value}}")
    if str(run.get("ack", "falls")).lower() not in ("falls", "edge", "none"):
        problems.append("run.ack must be falls | edge | none")
    for f in spec.get("factors") or []:
        if not f.get("name") or not f.get("symbol") or not isinstance(f.get("levels"), list) or not f["levels"]:
            problems.append(f"factor {f!r}: needs name, symbol, non-empty levels")
    rpl = int(spec.get("runs_per_level") or 0)
    if rpl < 1:
        problems.append("runs_per_level must be >= 1")
    elif rpl < 5:
        warnings.append(f"runs_per_level={rpl}: a sigma estimate from {rpl} runs has a relative standard error of "
                        f"~{100/math.sqrt(2*max(rpl-1,1)):.0f} %; criteria on sigma/Cpk will mostly be inconclusive")
    measures = spec.get("measures") or {}
    for name, m in measures.items():
        k = m.get("kind")
        if k not in MEASURE_KINDS:
            problems.append(f"measure {name}: kind must be one of {MEASURE_KINDS}")
            continue
        if k in ("first", "last", "min", "max", "mean", "delta", "changes", "edges", "dwell", "pulses", "settle", "read") and not m.get("symbol"):
            problems.append(f"measure {name}: symbol required")
        if k == "latency" and not (isinstance(m.get("from"), dict) and isinstance(m.get("to"), dict)):
            problems.append(f"measure {name}: latency needs from{{symbol,op,value}} and to{{symbol,op,value}}")
        if k == "expr":
            try:
                _check_expr(m.get("expr", ""), set(measures) | {f["name"] for f in spec.get("factors") or []})
            except ExperimentError as exc:
                problems.append(f"measure {name}: {exc}")
        if k == "dwell" and "value" not in m:
            problems.append(f"measure {name}: dwell needs value")
        if k == "dwell" and m.get("mode", "equal") not in ("equal", "not_equal_total"):
            problems.append(f"measure {name}: dwell mode must be equal | not_equal_total")
        if k == "latency" and m.get("agg", "first") not in ("first", "max", "mean"):
            problems.append(f"measure {name}: latency agg must be first | max | mean")
    for name, mt in (spec.get("metrics") or {}).items():
        if mt.get("measure") not in measures:
            problems.append(f"metric {name}: unknown measure {mt.get('measure')!r}")
        if mt.get("stat") not in STATS:
            problems.append(f"metric {name}: stat must be one of {STATS}")
        if mt.get("stat") == "cpk" and not mt.get("tolerance"):
            problems.append(f"metric {name}: cpk needs tolerance (± grams)")
    for c in spec.get("criteria") or []:
        if not (c.get("metric") in (spec.get("metrics") or {}) or c.get("measure") in measures):
            problems.append(f"criterion {c!r}: refers to no metric/measure")
        if c.get("op") not in CRIT_OPS:
            problems.append(f"criterion {c!r}: op must be one of {CRIT_OPS}")
        if c.get("op") == "between" and not (isinstance(c.get("value"), list) and len(c["value"]) == 2):
            problems.append(f"criterion {c!r}: between needs value=[lo, hi]")
        if c.get("quantile") not in (None, 0.95, 1):
            problems.append(f"criterion {c!r}: quantile must be 0.95 or 1 (or omitted for the mean)")
    if profile_symbols is not None:
        pl = [x.lower() for x in profile_symbols]
        for name, m in measures.items():
            for sym in [m.get("symbol")] + [m[k]["symbol"] for k in ("from", "to") if isinstance(m.get(k), dict)]:
                if sym and not any(sym.lower() == x or sym.lower().startswith(x + ".") or sym.lower().startswith(x + "[") for x in pl):
                    warnings.append(f"measure {name}: {sym} is not in profile {spec.get('profile')!r}; the runner adds it to a recording group")
    allowed = {s.lower() for s in spec.get("writes_allowed") or []}
    for s in writes_in_spec(spec):
        if s.lower() not in allowed:
            problems.append(f"{s} is written by the spec but not in writes_allowed")
        elif write_check:
            why = write_check(s)
            if why:
                problems.append(f"{s}: guard refuses writes ({why})")
    unresolved = []
    types: dict[str, str] = {}
    if resolve:
        for s in writes_in_spec(spec) + reads_in_spec(spec):
            t = resolve(s)
            if t is None:
                unresolved.append(s)
            else:
                types[s] = t
    lv = levels_of(spec)
    planned = min(len(lv) * rpl, int(spec.get("max_runs") or 10**9)) if rpl else 0
    dw_t = (run.get("done_when") or {}).get("timeout_ms") or 600_000
    est_s = planned * ((dw_t / 1000) * 0.5 + (run.get("settle_ms") or 0) / 1000 + (run.get("between_runs_ms") or 0) / 1000)
    return {"ok": not problems and not unresolved, "problems": problems, "warnings": warnings, "unresolved_symbols": unresolved,
            "resolved_types": types, "levels": lv, "planned_runs": planned, "writes": writes_in_spec(spec),
            "estimated_duration_min": round(est_s / 60, 1),
            "estimate_note": "assumes a run takes half its done_when timeout; refine after the first run"}


def _check_expr(expr: str, names: set[str]) -> None:
    if not expr or not isinstance(expr, str):
        raise ExperimentError("expr: empty")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ExperimentError(f"expr syntax: {exc.msg}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult,
                             ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Mod)):
            continue
        if isinstance(node, ast.Name):
            if node.id not in names and node.id not in ("abs", "min", "max", "sqrt"):
                raise ExperimentError(f"expr: unknown name {node.id!r}")
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("abs", "min", "max", "sqrt"):
            continue
        if isinstance(node, ast.Load):
            continue
        raise ExperimentError(f"expr: {type(node).__name__} not allowed (arithmetic over measure names only)")


def eval_expr(expr: str, env: dict[str, Any]) -> float | None:
    """Arithmetic over measure/factor names; None if any referenced value is missing."""
    tree = ast.parse(expr, mode="eval")
    funcs = {"abs": abs, "min": min, "max": max, "sqrt": math.sqrt}

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return float(n.value)
        if isinstance(n, ast.Name):
            if n.id in funcs:
                return funcs[n.id]
            v = env.get(n.id)
            if v is None:
                raise KeyError(n.id)
            return float(v)
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            return -v if isinstance(n.op, ast.USub) else v
        if isinstance(n, ast.BinOp):
            a, b = ev(n.left), ev(n.right)
            ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
                   ast.Pow: operator.pow, ast.Mod: operator.mod}
            return ops[type(n.op)](a, b)
        if isinstance(n, ast.Call):
            return ev(n.func)(*[ev(a) for a in n.args])
        raise ExperimentError("bad expr node")

    try:
        return float(ev(tree))
    except (KeyError, ZeroDivisionError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def t95(df: int) -> float:
    if df < 1:
        return float("inf")
    if df <= 30:
        return _T95[df - 1]
    return 2.0 if df <= 60 else 1.96


_CHI2_LO = [0.000982, 0.0506, 0.2158, 0.4844, 0.8312, 1.2373, 1.6899, 2.1797, 2.7004, 3.2470]   # χ²₀.₀₂₅, df 1..10
_CHI2_HI = [5.0239, 7.3778, 9.3484, 11.1433, 12.8325, 14.4494, 16.0128, 17.5345, 19.0228, 20.4832]   # χ²₀.₉₇₅


def chi2_quantile(p: float, df: int) -> float:
    """Exact table for df ≤ 10 (where Wilson–Hilferty is off by up to 48 %), Wilson–Hilferty beyond."""
    if p not in (0.025, 0.975):
        raise ValueError("p must be 0.025 or 0.975")
    if df < 1:
        raise ValueError("df >= 1")
    if df <= 10:
        return (_CHI2_LO if p == 0.025 else _CHI2_HI)[df - 1]
    z = -1.95996 if p == 0.025 else 1.95996
    a = 2.0 / (9.0 * df)
    return df * (1 - a + z * math.sqrt(a)) ** 3


def stats(values: list[float], tolerance: float | None = None) -> dict[str, Any]:
    vals = [float(v) for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    n = len(vals)
    out: dict[str, Any] = {"count": n}
    if n == 0:
        return out
    s = sorted(vals)
    mean = sum(s) / n
    out.update({"mean": mean, "min": s[0], "max": s[-1], "range": s[-1] - s[0],
                "median": s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2,
                "p95": s[min(n - 1, int(math.ceil(0.95 * n)) - 1)]})
    if n >= 2:
        var = sum((v - mean) ** 2 for v in s) / (n - 1)
        sd = math.sqrt(var)
        half = t95(n - 1) * sd / math.sqrt(n)
        out.update({"std": sd, "ci95_mean": [mean - half, mean + half], "ci95_half": half,
                    "sigma_rel_se": 1 / math.sqrt(2 * (n - 1))})
        if n >= 3:
            lo = sd * math.sqrt((n - 1) / chi2_quantile(0.975, n - 1))
            hi = sd * math.sqrt((n - 1) / chi2_quantile(0.025, n - 1))
            out["ci95_std"] = [lo, hi]
        if tolerance is not None and sd > 0:
            out["cpk"] = (tolerance - abs(mean)) / (3 * sd)
            if "ci95_std" in out:
                # σ-uncertainty only (the bias is taken as measured); sorted so a negative numerator cannot invert it
                out["ci95_cpk"] = sorted([(tolerance - abs(mean)) / (3 * out["ci95_std"][1]), (tolerance - abs(mean)) / (3 * out["ci95_std"][0])])
                out["ci95_cpk_note"] = "from the σ interval only"
    return out


# measures that cannot be negative: counts and durations (D36)
_NONNEG_KINDS = frozenset({"changes", "edges", "pulses", "latency", "dwell", "settle"})


def _bound_ci(st: dict[str, Any], lower: float | None, resolution: float | None) -> dict[str, Any]:
    """The t-interval of the mean, made honest for what it describes: never below a measure's physical lower bound
    (a count of −1.1, D36), never narrower than the timestamp resolution (n identical 1 ms readings are a ±0.5 ms
    statement about the mean, not a zero-width one — D34's CI [1, 1])."""
    ci = st.get("ci95_mean")
    if not ci:
        return st
    lo, hi = ci
    notes = []
    if resolution:
        q = resolution / 2.0
        mean = st["mean"]
        if (hi - lo) / 2.0 < q:
            lo, hi = mean - q, mean + q
            notes.append(f"widened to the timestamp resolution ±{q:g}")
    if lower is not None and lo < lower:
        lo = lower
        notes.append(f"clipped at {lower:g} (the measure cannot be lower)")
    if notes:
        st = dict(st)
        st["ci95_mean"] = [lo, hi]
        st["ci95_note"] = "; ".join(notes)
    return st


def _metric_value(st: dict[str, Any], stat: str) -> float | None:
    return st.get(stat)


def _metric_ci(st: dict[str, Any], stat: str) -> list[float] | None:
    if stat == "mean":
        return st.get("ci95_mean")
    if stat == "std":
        return st.get("ci95_std")
    if stat == "cpk":
        return st.get("ci95_cpk")
    return None


def _crit_pass(value: float, op: str, limit: Any) -> bool:
    if op == "le":
        return value <= limit
    if op == "lt":
        return value < limit
    if op == "ge":
        return value >= limit
    if op == "gt":
        return value > limit
    if op == "abs_le":
        return abs(value) <= limit
    if op == "between":
        return limit[0] <= value <= limit[1]
    raise ExperimentError(f"criterion op {op!r}")


def _crit_verdict(value: float | None, ci: list[float] | None, op: str, limit: Any, n: int,
                  stat: str | None = None) -> dict[str, Any]:
    if value is None:
        return {"verdict": "no_data", "value": None}
    ok = _crit_pass(value, op, limit)
    res: dict[str, Any] = {"verdict": "pass" if ok else "fail", "value": value, "ci95": ci, "n": n}
    if stat in ("mean", "std", "cpk") and ci is None:
        res["verdict"] = "inconclusive"
        res["why"] = f"n={n} is too small for a 95 % CI on {stat} (need {2 if stat == 'mean' else 3}+ valid runs)"
        res["n_needed_estimate"] = None
        return res
    if ci is not None:
        lo, hi = ci
        if op == "abs_le":
            lo, hi = min(abs(lo), abs(hi)) if lo * hi > 0 else 0.0, max(abs(lo), abs(hi))
        lims = limit if op == "between" else [limit]
        straddles = any(lo < L < hi for L in lims)   # strictly inside; a zero-width CI on the limit is decided by the point
        if straddles:
            res["verdict"] = "inconclusive"
            L = lims[0] if op != "between" else (lims[0] if abs(value - lims[0]) < abs(value - lims[1]) else lims[1])
            gap = abs((abs(value) if op == "abs_le" else value) - L)
            half = (hi - lo) / 2
            if gap > 0 and n >= 2:
                res["n_needed_estimate"] = int(math.ceil(n * (half / gap) ** 2))
            else:
                res["n_needed_estimate"] = None
            res["why"] = f"95% CI [{lo:.4g}, {hi:.4g}] contains the limit {L}"
    return res


def evaluate(spec: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-level metrics and criteria verdicts from stored run records."""
    ok_runs = [r for r in runs if r.get("status") == "ok"]
    levels = levels_of(spec)
    metrics_spec = spec.get("metrics") or {}
    out_levels = []
    verdicts_all: list[str] = []
    for lv in levels:
        lv_runs = [r for r in ok_runs if r.get("level") == lv]
        metrics: dict[str, Any] = {}
        measure_stats: dict[str, Any] = {}
        for mname in (spec.get("measures") or {}):
            vals = [r["measures"].get(mname) for r in lv_runs if isinstance(r.get("measures"), dict)]
            mk = ((spec.get("measures") or {}).get(mname) or {}).get("kind")
            mres = max([float(r["measure_resolution_ms"][mname]) for r in lv_runs
                        if isinstance(r.get("measure_resolution_ms"), dict)
                        and isinstance(r["measure_resolution_ms"].get(mname), (int, float))] or [0.0]) or None
            measure_stats[mname] = _bound_ci(stats([v for v in vals if isinstance(v, (int, float))]),
                                             0.0 if mk in _NONNEG_KINDS else None, mres)
        for name, mt in metrics_spec.items():
            st = stats([r["measures"].get(mt["measure"]) for r in lv_runs if isinstance(r.get("measures"), dict)
                        and isinstance(r["measures"].get(mt["measure"]), (int, float))], mt.get("tolerance"))
            kind = ((spec.get("measures") or {}).get(mt["measure"]) or {}).get("kind")
            res_ms = max([float(r["measure_resolution_ms"][mt["measure"]]) for r in lv_runs
                          if isinstance(r.get("measure_resolution_ms"), dict)
                          and isinstance(r["measure_resolution_ms"].get(mt["measure"]), (int, float))] or [0.0]) or None
            st = _bound_ci(st, 0.0 if kind in _NONNEG_KINDS else None, res_ms)
            metrics[name] = {"value": _metric_value(st, mt["stat"]), "ci95": _metric_ci(st, mt["stat"]), "n": st["count"],
                             "measure": mt["measure"], "stat": mt["stat"]}
            if st.get("ci95_note") and mt["stat"] == "mean":
                metrics[name]["ci95_note"] = st["ci95_note"]
        crits = []
        for c in spec.get("criteria") or []:
            if c.get("metric"):
                m = metrics.get(c["metric"]) or {}
                v = _crit_verdict(m.get("value"), m.get("ci95"), c["op"], c["value"], m.get("n", 0), m.get("stat"))
                v["criterion"] = f"{c['metric']} {c['op']} {c['value']}"
            else:
                st = measure_stats.get(c["measure"]) or {}
                q = c.get("quantile")
                val = st.get("p95") if q == 0.95 else st.get("max") if q == 1 else st.get("mean")
                v = _crit_verdict(val, None, c["op"], c["value"], st.get("count", 0))
                v["criterion"] = f"{c['measure']}{'@p95' if q == 0.95 else '@max' if q == 1 else '@mean'} {c['op']} {c['value']}"
            crits.append(v)
            verdicts_all.append(v["verdict"])
        lvl_verdict = _combine([c["verdict"] for c in crits]) if crits else "no_criteria"
        n_failed = len([r for r in runs if r.get("level") == lv and r.get("status") != "ok"])
        note = None
        if lvl_verdict == "pass" and n_failed:
            lvl_verdict = "inconclusive"
            note = f"{n_failed} run(s) failed at this level; a pass is not granted while runs are missing"
            verdicts_all.append("inconclusive")
        out_levels.append({"level": lv, "runs_ok": len(lv_runs), "runs_failed": n_failed,
                           "metrics": metrics, "measures": measure_stats, "criteria": crits, "verdict": lvl_verdict,
                           **({"note": note} if note else {})})
    overall = _combine(verdicts_all) if verdicts_all else "no_criteria"
    return {"experiment": spec.get("name"), "runs_total": len(runs), "runs_ok": len(ok_runs), "levels": out_levels, "verdict": overall}


def _combine(vs: list[str]) -> str:
    if not vs:
        return "no_data"
    if any(v == "fail" for v in vs):
        return "fail"
    if any(v in ("inconclusive", "no_data") for v in vs):
        return "inconclusive"
    return "pass"


def compare(eval_a: dict[str, Any], eval_b: dict[str, Any]) -> dict[str, Any]:
    """Same metrics side by side per level; 'separated' when the 95 % CIs do not overlap."""
    out = []
    by_lv_b = {json.dumps(l["level"], sort_keys=True): l for l in eval_b["levels"]}
    for la in eval_a["levels"]:
        lb = by_lv_b.get(json.dumps(la["level"], sort_keys=True))
        if lb is None:
            continue
        row = {"level": la["level"], "metrics": {}}
        for name, ma in la["metrics"].items():
            mb = lb["metrics"].get(name)
            if not mb:
                continue
            d = {"a": ma["value"], "b": mb["value"],
                 "delta": None if ma["value"] is None or mb["value"] is None else mb["value"] - ma["value"]}
            if ma.get("ci95") and mb.get("ci95"):
                d["separated"] = ma["ci95"][1] < mb["ci95"][0] or mb["ci95"][1] < ma["ci95"][0]
            row["metrics"][name] = d
        out.append(row)
    return {"a": eval_a["experiment"], "b": eval_b["experiment"], "levels": out,
            "verdicts": {"a": eval_a["verdict"], "b": eval_b["verdict"]}}


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class ExperimentStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-\.]{1,64}", name):
            raise ExperimentError("experiment name: letters, digits, _ - . (max 64)")
        return self.root / name

    def list(self) -> list[dict[str, Any]]:
        out = []
        for d in sorted(p for p in self.root.iterdir() if p.is_dir()):
            spec_p = self._spec_path(d)
            runs = self.runs(d.name) if spec_p else []
            st = self.status(d.name)
            out.append({"name": d.name, "runs": len(runs), "runs_ok": len([r for r in runs if r.get("status") == "ok"]),
                        "state": st.get("state", "idle"), "has_report": (d / "report.md").exists()})
        return out

    def _spec_path(self, d: Path) -> Path | None:
        for n in ("spec.yaml", "spec.yml", "spec.json"):
            if (d / n).is_file():
                return d / n
        return None

    def save_spec(self, spec: dict[str, Any]) -> Path:
        d = self.dir(spec["name"])
        d.mkdir(parents=True, exist_ok=True)
        p = d / ("spec.yaml" if yaml is not None else "spec.json")
        p.write_text(dump_spec(spec), encoding="utf-8")
        return p

    def spec(self, name: str) -> dict[str, Any]:
        d = self.dir(name)
        p = self._spec_path(d)
        if p is None:
            raise ExperimentError(f"no experiment {name!r} (exp_list)")
        return load_spec(p.read_text(encoding="utf-8"))

    def runs(self, name: str) -> list[dict[str, Any]]:
        p = self.dir(name) / "runs.jsonl"
        if not p.is_file():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out

    def append_run(self, name: str, run: dict[str, Any]) -> None:
        with open(self.dir(name) / "runs.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(run, default=str) + "\n")

    def archive_runs(self, name: str) -> int:
        """exp_run(resume=false): earlier runs are not part of the new series. They are kept (evidence is never
        deleted) in runs_archive_<timestamp>.jsonl and no longer counted by exp_evaluate / exp_report."""
        p = self.dir(name) / "runs.jsonl"
        if not p.is_file():
            return 0
        n = sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
        if n == 0:
            p.unlink()
            return 0
        import time as _t
        stamp = _t.strftime("%Y%m%d-%H%M%S")
        target = self.dir(name) / f"runs_archive_{stamp}.jsonl"
        k = 1
        while target.exists():   # two archives in one second: never overwrite evidence (POSIX rename would)
            k += 1
            target = self.dir(name) / f"runs_archive_{stamp}_{k}.jsonl"
        p.rename(target)
        return n

    def status(self, name: str) -> dict[str, Any]:
        p = self.dir(name) / "status.json"
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return {}

    def write_status(self, name: str, st: dict[str, Any]) -> None:
        d = self.dir(name)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status.json").write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")

    def interpretation(self, name: str) -> str | None:
        p = self.dir(name) / "interpretation.md"
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def write_interpretation(self, name: str, text: str, author: str) -> None:
        d = self.dir(name)
        (d / "interpretation.md").write_text(f"<!-- author: {author}; written: {time.strftime('%Y-%m-%d %H:%M:%S')} -->\n{text.strip()}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
@dataclass
class RunnerDeps:
    """Everything the runner needs, as callables, so it can run on a thread and be tested without
    the server. ``call``/``slow_call`` execute a backend function on the quick / slow ADS worker."""
    call: Callable[..., Any]
    slow_call: Callable[..., Any]
    ads: Any                       # AdsBackend
    rec: Any                       # Recorder
    bench: Any                     # TestBench (snapshots)
    profiles: Any                  # ProfileStore
    write_check: Callable[[str], str | None]     # guard: reason or None
    audit: Callable[[str, dict[str, Any], str], None]
    scope: Callable[[], str] = lambda: ""        # machine identity (net id:port) the guard decisions were made for
    log: Callable[[str], None] = lambda m: None


class Runner:
    def __init__(self, store: ExperimentStore, name: str, deps: RunnerDeps):
        self.store, self.name, self.d = store, name, deps
        self.spec = store.spec(name)
        self._abort = threading.Event()
        self._thread: threading.Thread | None = None
        self.state: dict[str, Any] = {"state": "idle"}
        self._lock = threading.Lock()
        self._start_lock = threading.Lock()
        self._live_recs: list[str] = []
        self._archived_note: str | None = None
        self._scope0 = ""
        self._types: dict[str, Any] = {}        # symbol -> SymbolEntry (resolved at start)
        self._cut: dict[str, float] = {}         # recording id -> its PLC time when the after-steps began (D33)

    # -- lifecycle ------------------------------------------------------------
    def start(self, max_runs: int | None = None, resume: bool = True, dry_run: bool = False) -> dict[str, Any]:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                raise ExperimentError("experiment already running (exp_status / exp_abort)")
            self.spec = self.store.spec(self.name)      # (re)load only when nothing is running
            v = validate_spec(self.spec, write_check=self.d.write_check,
                              profile_exists=lambda n: any(p["name"] == n for p in self.d.profiles.list()))
            if v["problems"]:
                raise ExperimentError("spec invalid: " + "; ".join(v["problems"]))
            self._resolve_types()
            self._archived_note = None
            if resume:
                done = self.store.runs(self.name)
            else:
                archived = self.store.archive_runs(self.name)
                done = []
                self._archived_note = f"{archived} earlier run(s) moved to runs_archive_*.jsonl" if archived else None
            plan = self._plan(done)
            if max_runs is not None:
                plan = plan[:max_runs]
            if dry_run:
                return self._dry_run(plan)
            prev = self.store.status(self.name)
            sessions = list(prev.get("sessions") or [])
            sessions.append({"started": time.time(), "finished": None, "planned": len(plan)})
            self._scope0 = self.d.scope()
            self._set(state="running", planned=len(plan), done=len([r for r in done if r.get("status") == "ok"]), failed=0,
                      current=None, started=sessions[0]["started"], finished=None, error=None, consecutive_failures=0,
                      dry_run=False, levels=len(levels_of(self.spec)), sessions=sessions, scope=self._scope0,
                      attempted_this_session=0, restore_warning=None, abort_reason=None)
            self._abort.clear()
            self._thread = threading.Thread(target=self._main, args=(plan,), name=f"exp-{self.name}", daemon=True)
            self._thread.start()
        out = {"experiment": self.name, "state": "running", "planned_runs": len(plan), "already_done": len(done),
               "estimated_duration_min": v["estimated_duration_min"], "scope": self._scope0}
        if getattr(self, "_archived_note", None):
            out["archived"] = self._archived_note
        return out

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def abort(self, reason: str = "requested") -> dict[str, Any]:
        self._abort.set()
        cur = self.state.get("current")
        got = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "during": cur,
               "runs_attempted": self.state.get("attempted_this_session", 0)}
        self._set(abort_reason=reason, abort_received=got)
        return {"experiment": self.name, "aborting": True, "reason": reason, "received_during": cur,
                "note": "the current run stops at its next step, its after-steps still run (cleanup), no further run starts"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self.state)
        st["experiment"] = self.name
        st["alive"] = bool(self._thread and self._thread.is_alive())
        st["live_recordings"] = list(self._live_recs)
        sess = (st.get("sessions") or [{}])[-1]
        if sess.get("started") and st.get("planned"):
            el = (sess.get("finished") or time.time()) - sess["started"]
            n = st.get("attempted_this_session", 0)
            st["elapsed_min"] = round(el / 60, 1)
            st["eta_min"] = round((st["planned"] - n) * (el / n) / 60, 1) if n else None
        return st

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)
            snap = dict(self.state)
        try:
            self.store.write_status(self.name, snap)
        except OSError:
            pass

    # -- types ---------------------------------------------------------------------
    def _resolve_types(self) -> None:
        """Resolve every written/read symbol once; refuse non-scalar write targets (a struct/FB write
        could reach deny-listed members that the per-symbol guard never sees)."""
        from . import types_tc as T
        self._types = {}
        missing = []
        for sym in writes_in_spec(self.spec) + reads_in_spec(self.spec):
            try:
                self._types[sym.lower()] = self.d.call(self.d.ads.symbol_info, sym, timeout=20)
            except Exception as exc:  # noqa: BLE001
                missing.append(f"{sym}: {str(exc)[:80]}")
        if missing:
            raise ExperimentError("symbols not on the target: " + "; ".join(missing[:8]))
        for sym in writes_in_spec(self.spec):
            info = self._types[sym.lower()]
            tt = T.parse_type(info.type, info.size)
            if tt.ctype is None or tt.opaque:
                raise ExperimentError(f"{sym} is {info.type}: experiments write scalars only (a struct/FB write bypasses the member guard)")

    def _enum_table(self, sym: str) -> dict[str, int] | None:
        info = self._types.get(sym.lower())
        if info is None:
            return None
        try:
            ts = self.d.ads.typesystem(False)
        except Exception:  # noqa: BLE001
            return None
        e = ts.get(info.type) if ts is not None else None
        if e is None or not getattr(e, "enum", None):
            return None
        return {name.lower(): val for val, name in e.enum.items()}

    def coerce(self, sym: str, value: Any) -> Any:
        """Spec values as humans write them → what the PLC compares against: enum names → ints,
        'TRUE'/'FALSE' → bool, numeric strings → numbers. Unknown enum names are an error, not a guess."""
        if not isinstance(value, str):
            return value
        v = value.strip()
        info = self._types.get(sym.lower())
        if info is not None and info.type.upper() == "BOOL":
            if v.lower() in ("true", "1", "on"):
                return True
            if v.lower() in ("false", "0", "off"):
                return False
        tbl = self._enum_table(sym)
        if tbl is not None:
            if v.lower() in tbl:
                return tbl[v.lower()]
            try:
                return int(v, 0)
            except ValueError:
                raise ExperimentError(f"{sym}: {value!r} is not a literal of enum {info.type} ({', '.join(sorted(tbl))})")
        try:
            return float(v) if ("." in v or "e" in v.lower()) else int(v, 0)
        except ValueError:
            return value

    # -- planning -----------------------------------------------------------------
    def _plan(self, done: list[dict[str, Any]]) -> list[tuple[dict[str, Any], int]]:
        rpl = int(self.spec.get("runs_per_level") or 1)
        max_runs = int(self.spec.get("max_runs") or 10**9)
        have: dict[str, int] = {}
        for r in done:
            if r.get("status") == "ok":
                k = json.dumps(r.get("level"), sort_keys=True)
                have[k] = have.get(k, 0) + 1
        plan = []
        for lv in levels_of(self.spec):
            k = json.dumps(lv, sort_keys=True)
            for rep in range(have.get(k, 0), rpl):
                plan.append((lv, rep + 1))
        budget = max(0, max_runs - len([r for r in done if r.get("status") == "ok"]))
        return plan[:budget]

    def _dry_run(self, plan) -> dict[str, Any]:
        syms = writes_in_spec(self.spec) + reads_in_spec(self.spec)
        resolved = {s: self._types[s.lower()].type for s in syms if s.lower() in self._types}
        unresolved = [{"symbol": s} for s in syms if s.lower() not in self._types]
        coerced = {}
        for pc in list(self.spec.get("preconditions") or []) + [(self.spec.get("run") or {}).get("done_when") or {}]:
            if pc.get("symbol"):
                try:
                    coerced[f"{pc['symbol']} {pc['op']} {pc['value']!r}"] = self.coerce(pc["symbol"], pc["value"])
                except ExperimentError as exc:
                    coerced[f"{pc['symbol']} {pc['op']} {pc['value']!r}"] = f"ERROR: {exc}"
        prof = self.d.profiles.get(self.spec["profile"])
        return {"experiment": self.name, "dry_run": True, "planned_runs": len(plan),
                "plan_head": [{"level": lv, "rep": rep} for lv, rep in plan[:10]],
                "writes": writes_in_spec(self.spec), "resolved": resolved, "unresolved": unresolved, "condition_values": coerced,
                "profile": {"name": prof.name, "symbols": len(prof.items), "groups": [f"{m}@{c:g}" for m, c in prof.groups()]},
                "scope": self.d.scope(), "nothing_written": True}

    # -- main loop -----------------------------------------------------------------
    def _main(self, plan) -> None:
        from . import types_tc as T
        originals: dict[str, Any] = {}
        try:
            # guard every write symbol once more, right before anything moves
            for sym in writes_in_spec(self.spec):
                why = self.d.write_check(sym)
                if why:
                    raise ExperimentError(f"guard refuses writes to {sym}: {why}")
            if self.spec.get("restore_factors", True):
                for f in self.spec.get("factors") or []:
                    try:
                        raw = self.d.call(self.d.ads.read, f["symbol"], timeout=15)["value"]
                    except Exception as exc:  # noqa: BLE001
                        raise ExperimentError(f"cannot read the current value of factor {f['symbol']} ({exc}); refusing to modify what cannot be restored")
                    orig = _unwrap(raw)
                    info = self._types.get(f["symbol"].lower())
                    if info is not None:
                        try:
                            T.encode(T.parse_type(info.type, info.size), orig)
                        except Exception as exc:  # noqa: BLE001
                            raise ExperimentError(f"factor {f['symbol']}: current value {raw!r} cannot be written back ({exc})")
                    originals[f["symbol"]] = orig
            self.d.audit("exp_run", {"experiment": self.name, "planned": len(plan), "writes": writes_in_spec(self.spec),
                                     "originals": originals, "scope": self._scope0}, "started")
            consecutive = 0
            n_done = 0
            for lv, rep in plan:
                if self._abort.is_set():
                    self._set(state="aborted", finished=time.time())
                    break
                self._set(current={"level": lv, "rep": rep})
                rec_run = self._run_one(lv, rep)
                self.store.append_run(self.name, rec_run)
                n_done += 1
                if rec_run["status"] == "ok":
                    consecutive = 0
                    self._set(done=self.state.get("done", 0) + 1, attempted_this_session=n_done)
                else:
                    consecutive += 1
                    self._set(failed=self.state.get("failed", 0) + 1, consecutive_failures=consecutive,
                              last_error=rec_run.get("error"), attempted_this_session=n_done)
                    if rec_run.get("fatal"):
                        self._set(state="failed", error=rec_run.get("error"), finished=time.time())
                        break
                    if consecutive >= int(self.spec.get("max_consecutive_failures", 2)):
                        self._set(state="failed", error=f"{consecutive} consecutive failed runs: {rec_run.get('error')}", finished=time.time())
                        break
                between = (self.spec.get("run") or {}).get("between_runs_ms") or 0
                if between and not self._abort.is_set():
                    self._abort.wait(between / 1000)
            else:
                # D37: an abort during the last run is still an abort, not 'done'
                self._set(state="aborted" if self._abort.is_set() else "done", finished=time.time())
            if self._abort.is_set() and self.state.get("state") == "running":
                self._set(state="aborted", finished=time.time())
        except Exception as exc:  # noqa: BLE001
            self._set(state="failed", error=str(exc)[:400], finished=time.time())
        finally:
            self._stop_live_recs("experiment ended")
            if self.state.get("state") in ("aborted", "failed"):
                for w in (self.spec.get("run") or {}).get("on_abort") or []:
                    try:
                        self._write(w["symbol"], w["value"], purpose="on_abort")
                    except Exception as exc:  # noqa: BLE001
                        self._set(restore_warning=f"on_abort write {w['symbol']} failed: {exc}")
            warn = []
            for sym, v in originals.items():
                try:
                    self._write(sym, v, purpose="restore")
                    back = _unwrap(self.d.call(self.d.ads.read, sym, timeout=15)["value"])
                    if not _same(back, v):
                        warn.append(f"{sym}: wrote {v!r}, read back {back!r}")
                except Exception as exc:  # noqa: BLE001
                    warn.append(f"{sym}: not restored ({str(exc)[:120]})")
            if warn:
                self._set(restore_warning="; ".join(warn))
            with self._lock:
                sess = (self.state.get("sessions") or [{}])
                if sess:
                    sess[-1]["finished"] = time.time()
                    sess[-1]["state"] = self.state.get("state")
            self.d.audit("exp_run", {"experiment": self.name, "state": self.state.get("state"),
                                     "restore_warning": self.state.get("restore_warning")}, "finished")
            self._set(current=None)

    def _write(self, symbol: str, value: Any, purpose: str = "run") -> None:
        """Every write: guard re-checked (a tc_set_mode('readonly') mid-experiment stops the next write),
        scope pinned to the machine the experiment started on, audited with symbol and value."""
        why = self.d.write_check(symbol)
        if why:
            raise FatalRunError(f"guard refuses {symbol}: {why}")
        now = self.d.scope()
        if self._scope0 and now != self._scope0:
            raise FatalRunError(f"target changed during the experiment ({self._scope0} → {now}); refusing to write")
        value = self.coerce(symbol, value)
        try:
            self.d.call(self.d.ads.write, symbol, value, timeout=15)
        except TimeoutError as exc:
            # the job was cancelled if it had not started; if it had, the PLC state is unknown → fatal
            raise FatalRunError(f"write {symbol} timed out; PLC state unknown ({exc})") from exc
        self.d.audit("exp_write", {"experiment": self.name, "symbol": symbol, "value": value, "purpose": purpose,
                                   "run": (self.state.get("current") or {})}, "ok")

    def _wait(self, cond: dict[str, Any], default_timeout: int = 600_000, invert: bool = False,
              cleanup: bool = False) -> dict[str, Any]:
        """wait_until in ≤1 s slices so exp_abort interrupts within a second instead of after done_when's timeout.
        ``cleanup`` (the run's after-steps): an abort does not interrupt it — cleanup must finish (D37)."""
        tmo = int(cond.get("timeout_ms") or default_timeout)
        op = _INVERSE[cond["op"]] if invert else cond["op"]
        value = self.coerce(cond["symbol"], cond["value"])
        deadline = time.monotonic() + tmo / 1000
        polls = 0
        while True:
            if self._abort.is_set() and not cleanup:
                raise ExperimentError("aborted")
            slice_ms = int(max(50, min(1000, (deadline - time.monotonic()) * 1000)))
            r = self.d.slow_call(self.d.ads.wait_until, cond["symbol"], op, value, slice_ms,
                                 int(cond.get("interval_ms") or 50), timeout=slice_ms / 1000 + 30)
            polls += int(r.get("polls") or 0)
            if r.get("met"):
                r["polls"] = polls
                return r
            if time.monotonic() >= deadline:
                r["polls"] = polls
                return r

    def _run_one(self, lv: dict[str, Any], rep: int) -> dict[str, Any]:
        run = self.spec.get("run") or {}
        t0 = time.time()
        rec_ids: list[str] = []
        record: dict[str, Any] = {"run_no": len(self.store.runs(self.name)) + 1, "level": lv, "rep": rep,
                                  "spec_version": self.spec.get("version"), "started": t0, "status": "ok",
                                  "recordings": [], "measures": {}, "error": None}
        try:
            for pc in self.spec.get("preconditions") or []:
                r = self._wait(pc, 60_000)
                if not r.get("met"):
                    raise ExperimentError(f"precondition {pc['symbol']} {pc['op']} {pc['value']} not met in {pc.get('timeout_ms', 60000)} ms")
            factor_syms = {f["name"]: f["symbol"] for f in self.spec.get("factors") or []}
            for fname, sym in factor_syms.items():
                self._write(sym, lv[fname])
            self._steps(run.get("before") or [], "before")
            if self.spec.get("snapshot_prefix"):
                snap = self.d.slow_call(self.d.bench.snapshot_save, f"exp-{self.name}-{record['run_no']}-pre", None,
                                        self.spec["snapshot_prefix"], None, timeout=120)
                record["snapshot_pre"] = snap.get("name") or snap.get("file")
            # recordings per profile group; the symbols a measure needs together land in the same group
            prof = self.d.profiles.get(self.spec["profile"])
            dw = run.get("done_when") or {}
            groups = {k: [i.symbol for i in its] for k, its in prof.groups().items()}
            self._place_measure_symbols(groups, dw.get("symbol"))
            for (mode, cyc), syms in groups.items():
                res = self.d.slow_call(self.d.rec.start, symbols=syms, prefix=None, pattern=None, mode=mode, cycle_ms=cyc,
                                       duration_s=None, max_events=5_000_000, start_when=None, stop_when=None,
                                       pre_trigger_ms=0, name=f"exp:{self.name}/{record['run_no']}/{mode}@{cyc:g}",
                                       max_symbols=len(syms) + 50, timeout=180)
                rec_ids.append(res["id"])
                self._live_recs.append(res["id"])
            record["recordings"] = list(rec_ids)
            # D22: the stimulus must come after every recording delivers — a stimulus written before the first sample
            # makes the edge the acknowledgement looks for invisible (the first sample already shows the new state)
            silent = self._wait_delivering(rec_ids, float(run.get("record_ready_timeout_ms") or 5000) / 1000)
            if silent:
                raise ExperimentError(f"recording(s) {silent} delivered no sample within "
                                      f"{int(run.get('record_ready_timeout_ms') or 5000)} ms — stimulus not written "
                                      "(is the PLC running? run.record_ready_timeout_ms to wait longer)")
            # stimulus
            st = run.get("start")
            if st:
                t_stim_ms = self._rec_plc_now_ms(rec_ids)
                self._write(st["symbol"], st["value"])
                if "reset_value" in st:
                    self._abort.wait(float(st.get("reset_after_ms") or 0) / 1000)
                    self._write(st["symbol"], st["reset_value"])
                # Acknowledgement: did the machine actually start?  run.ack:
                #   falls (default) — done_when must stop holding after the stimulus, then hold again
                #   edge             — the done symbol must change after the stimulus (fast processes that are
                #                      done again within one cycle; judged from the PLC-timestamped recording)
                #   none             — trust the stimulus
                mode = str(run.get("ack") or "falls").lower()
                ack_to = int(run.get("ack_timeout_ms") or run.get("start_timeout_ms")
                             or min(10_000, int(dw.get("timeout_ms") or 10_000)))
                if mode == "falls":
                    ack = self._wait({**dw, "timeout_ms": ack_to}, invert=True)
                    if not ack.get("met"):
                        # the process may have run and finished inside the poll interval: accept a change seen by the recorder
                        if self._changed_after(rec_ids, dw.get("symbol"), t_stim_ms):
                            record.setdefault("notes", []).append("ack: done_when never observed false, but the recording shows the "
                                                                  "done symbol changing after the stimulus — accepted (fast process; set run.ack: edge)")
                        else:
                            raise ExperimentError(f"start not acknowledged: {dw['symbol']} still {dw['op']} {dw['value']} "
                                                  f"{ack_to} ms after the stimulus and it never changed (run.ack: none to skip this check)")
                elif mode == "edge":
                    deadline = time.time() + ack_to / 1000
                    while not self._changed_after(rec_ids, dw.get("symbol"), t_stim_ms):
                        if time.time() >= deadline or self._abort.is_set():
                            raise ExperimentError(f"start not acknowledged: {dw['symbol']} did not change within {ack_to} ms after the stimulus")
                        self._abort.wait(0.05)
                elif mode != "none":
                    raise ExperimentError(f"run.ack must be falls | edge | none, got {mode!r}")
            r = self._wait(dw)
            if not r.get("met"):
                raise ExperimentError(f"run did not finish: {dw['symbol']} {dw['op']} {dw['value']} within {dw.get('timeout_ms', 600000)} ms")
            if run.get("settle_ms"):
                self._abort.wait(float(run["settle_ms"]) / 1000)
        except FatalRunError as exc:
            record["status"] = "failed"
            record["error"] = str(exc)[:300]
            record["fatal"] = True
        except Exception as exc:  # noqa: BLE001
            record["status"] = "failed"
            record["error"] = str(exc)[:300]
        finally:
            # D33: measures end where the after-steps begin — 'last' read after the reset was always the reset value
            self._cut = self._cut_points(rec_ids)
            if self._abort.is_set() and not record.get("fatal"):
                # the run was cut short or its data is partial: not a valid observation, and not a failure of the
                # system under test either
                record["status"] = "aborted"
                record["error"] = f"aborted during the run ({self.state.get('abort_reason') or 'requested'})"
            # 'after' has cleanup semantics: it runs on failure AND on abort, so state does not leak (D37); not after
            # a fatal error (guard refusal / target change: writing is what must stop)
            if not record.get("fatal"):
                try:
                    self._steps(run.get("after") or [], "after", cleanup=True)
                except FatalRunError as exc:       # guard refusal / abort / target change during cleanup: stop the series
                    record["status"] = "failed"
                    record["error"] = f"after: {str(exc)[:280]}"
                    record["fatal"] = True
                except Exception as exc:  # noqa: BLE001
                    record.setdefault("notes", []).append(f"after-step failed: {str(exc)[:200]}")
                    if record["status"] == "ok":
                        record["status"] = "failed"
                        record["error"] = f"after: {str(exc)[:280]}"
                if run.get("after"):
                    record["after_ran"] = True
            for rid in list(rec_ids):
                if self._stop_rec(rid, "run ended"):
                    pass
        if record["status"] == "ok":
            try:
                record["measures"], record["measure_notes"] = self._measure(rec_ids, lv)
                if getattr(self, "_last_resolution", None):
                    record["measure_resolution_ms"] = dict(self._last_resolution)
            except Exception as exc:  # noqa: BLE001
                record["status"] = "failed"
                record["error"] = f"measurement: {exc}"[:300]
        record["finished"] = time.time()
        record["duration_s"] = round(record["finished"] - t0, 2)
        return record

    def _steps(self, items: list[dict[str, Any]], where: str, cleanup: bool = False) -> None:
        """before/after items: {symbol, value} writes a value; {wait_ms: N} pauses (so a 10 ms task sees a pulse);
        {wait_until: {symbol, op, value, timeout_ms}} waits for a condition. ``cleanup`` (after-steps): runs to the
        end even when an abort arrives (D37: an abort used to skip the reset and leak PLC state)."""
        for w in items:
            if self._abort.is_set() and not cleanup:
                raise FatalRunError("aborted")
            if "wait_ms" in w:
                if cleanup:
                    time.sleep(min(float(w["wait_ms"]), 60_000) / 1000)
                else:
                    self._abort.wait(float(w["wait_ms"]) / 1000)
            elif "wait_until" in w:
                c = w["wait_until"]
                r = self._wait(c, int(c.get("timeout_ms") or 10_000), cleanup=cleanup)
                if not r.get("met"):
                    raise ExperimentError(f"{where}: {c.get('symbol')} {c.get('op')} {c.get('value')} not met in {c.get('timeout_ms', 10000)} ms")
            elif "symbol" in w and "value" in w:
                self._write(w["symbol"], w["value"])
            else:
                raise ExperimentError(f"{where} item {w!r}: needs symbol+value, wait_ms or wait_until")

    def _cut_points(self, rec_ids: list[str], timeout_s: float = 1.5) -> dict[str, float]:
        """Each recording's PLC time (its own clock) once everything sampled before now has arrived: wait two check
        cycles (a change-mode notification reports a change at its next check), then until the recorded span stops
        growing. Samples after this point belong to the after-steps, not to the run (D33)."""
        cyc = 0.0
        for rid in rec_ids:
            rec = self.d.rec.recs.get(rid)
            cyc = max(cyc, float(getattr(rec, "cycle_ms", 0) or 0))
        time.sleep(min(0.5, 2 * cyc / 1000.0 + 0.02))
        out: dict[str, float] = {}
        deadline = time.monotonic() + timeout_s
        for rid in rec_ids:
            last = None
            while True:
                try:
                    st = self.d.rec.status(rid)
                except Exception:  # noqa: BLE001
                    break
                span = st.get("plc_span_s")
                q = getattr(self.d.rec.recs.get(rid), "q", None)
                busy = q is not None and hasattr(q, "empty") and not q.empty()
                if isinstance(span, (int, float)) and span == last and not busy:
                    break
                last = span if isinstance(span, (int, float)) else last
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
            exact = getattr(self.d.rec.recs.get(rid), "last_t", None)
            if isinstance(exact, (int, float)) and exact > 0:
                out[rid] = float(exact)                       # python engine: the exact last sample time
            elif isinstance(last, (int, float)):
                out[rid] = float(last) * 1000.0 + 0.1         # status rounds to 0.1 ms: keep the last sample inside
        return out

    def _wait_delivering(self, rec_ids: list[str], timeout_s: float) -> list[str]:
        """Wait until every recording (and every notification block in it) has at least one sample; returns the ids
        still silent at the deadline."""
        pending = list(rec_ids)
        deadline = time.monotonic() + max(0.1, timeout_s)
        while pending:
            for rid in list(pending):
                try:
                    st = self.d.rec.status(rid)
                except Exception:  # noqa: BLE001 — gone: nothing to wait for
                    pending.remove(rid)
                    continue
                blocks = st.get("blocks") or []
                if (st.get("samples") or 0) > 0 and all((b.get("samples") or 0) > 0 for b in blocks):
                    pending.remove(rid)
            if not pending or time.monotonic() >= deadline or self._abort.is_set():
                break
            self._abort.wait(0.02)
        return pending

    def _rec_plc_now_ms(self, rec_ids: list[str]) -> float | None:
        """Where the recordings' PLC time stands now (ms since their first sample — the same clock as the value runs
        the acknowledgement reads), latest of all recordings; None when none has delivered yet."""
        best = None
        for rid in rec_ids:
            try:
                st = self.d.rec.status(rid)
            except Exception:  # noqa: BLE001
                continue
            span = st.get("plc_span_s")
            if isinstance(span, (int, float)):
                best = max(best or 0.0, float(span) * 1000.0)
        return best if best is not None else self._rec_elapsed_ms(rec_ids)

    def _rec_elapsed_ms(self, rec_ids: list[str]) -> float | None:
        """Where the first live recording's clock stands now (ms since its first sample), or None."""
        for rid in rec_ids:
            try:
                st = self.d.rec.status(rid)
            except Exception:  # noqa: BLE001
                continue
            if st.get("elapsed_s") is not None:
                return float(st["elapsed_s"]) * 1000.0
        return 0.0

    def _changed_after(self, rec_ids: list[str], symbol: str | None, t_ms: float | None) -> bool:
        """Did ``symbol`` change value after recording time ``t_ms``? Uses the recorder's value runs (PLC
        timestamps), so a change that lasted a single cycle still counts."""
        if not symbol:
            return False
        for rid in rec_ids:
            try:
                st = self.d.rec.stats(rid, [symbol], True, 100_000)
            except Exception:  # noqa: BLE001
                continue
            src = st.get("symbols") or {}
            d = src.get(symbol) or next((v for k, v in src.items() if k.lower() == symbol.lower()), None)
            if not isinstance(d, dict):
                continue
            runs = d.get("runs") or []
            # a value run that started at or after the stimulus, other than the very first sample
            if any(r.get("t_start_ms", 0) >= (t_ms or 0) for r in runs[1:]):
                return True
        return False

    def _place_measure_symbols(self, groups: dict[tuple[str, float], list[str]], done_symbol: str | None) -> None:
        """Symbols a single measure needs together (latency from/to, settle value+anchor, done_when)
        must be in one recording: timestamps are per recording and a lookup finds one recording."""
        def has(syms: list[str], s: str) -> bool:
            return s.lower() in {x.lower() for x in syms}

        def group_with(sym: str) -> list[str] | None:
            for syms in groups.values():
                if has(syms, sym):
                    return syms
            return None

        def ensure_together(primary: str, others: list[str]) -> None:
            tgt = group_with(primary)
            if tgt is None:
                tgt = next(iter(groups.values()))
                tgt.append(primary)
            for o in others:
                if not has(tgt, o):
                    tgt.append(o)

        for m in (self.spec.get("measures") or {}).values():
            k = m.get("kind")
            if k == "latency":
                ensure_together(m["from"]["symbol"], [m["to"]["symbol"]])
            elif k == "settle" and isinstance(m.get("from"), dict):
                ensure_together(m["symbol"], [m["from"]["symbol"]])
            elif k in ("first", "last", "min", "max", "mean", "delta", "changes", "edges", "dwell", "pulses") and m.get("symbol"):
                if group_with(m["symbol"]) is None:
                    next(iter(groups.values())).append(m["symbol"])
        if done_symbol:
            for syms in groups.values():
                if not has(syms, done_symbol):
                    syms.append(done_symbol)

    def _stop_rec(self, rid: str, reason: str) -> bool:
        try:
            self.d.slow_call(self.d.rec.stop, rid, reason, timeout=60)
        except Exception as exc:  # noqa: BLE001
            self.d.log(f"stop {rid}: {exc}")
            rec = self.d.rec.recs.get(rid)
            if rec is not None:
                rec.request_stop(reason)   # cooperative stop; the id stays in _live_recs for a retry
            return False
        if rid in self._live_recs:
            self._live_recs.remove(rid)
        return True

    def _stop_live_recs(self, reason: str) -> None:
        for rid in list(self._live_recs):
            if not self._stop_rec(rid, reason):
                self._stop_rec(rid, reason)   # one retry; the cooperative stop was already requested

    # -- measurement --------------------------------------------------------------------
    def _rid_for(self, rec_ids: list[str], *symbols: str) -> str | None:
        """The recording that holds every requested symbol; None rather than a guess."""
        want = {s.lower() for s in symbols if s}
        for rid in rec_ids:
            rec = self.d.rec.recs.get(rid)
            if rec and want <= {s.name.lower() for s in rec.symbols}:
                return rid
        return None

    def _events(self, rid: str, symbol: str) -> list[dict[str, Any]]:
        """The symbol's samples up to the start of the after-steps (D33)."""
        return self.d.rec.read(rid, None, self._cut.get(rid), [symbol], 10_000_000, False)["rows"]

    def _measure(self, rec_ids: list[str], lv: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        out: dict[str, Any] = {}
        notes: dict[str, str] = {}
        resolution: dict[str, float] = {}
        self._last_resolution = resolution
        specs = self.spec.get("measures") or {}
        # non-expr first, expr last
        order = [k for k, m in specs.items() if m.get("kind") != "expr"] + [k for k, m in specs.items() if m.get("kind") == "expr"]
        for name in order:
            m = specs[name]
            kind = m["kind"]
            try:
                if kind == "expr":
                    env = {**lv, **{k: v for k, v in out.items() if isinstance(v, (int, float))}}
                    out[name] = eval_expr(m["expr"], env)
                    if out[name] is None:
                        notes[name] = "a referenced value is missing"
                    continue
                if kind == "read":
                    v = self.d.call(self.d.ads.read, m["symbol"], timeout=15)["value"]
                    out[name] = _num(v)
                    continue
                if kind == "latency":
                    f, t = m["from"], m["to"]
                    rid = self._rid_for(rec_ids, f["symbol"], t["symbol"])
                    if rid is None:
                        out[name] = None
                        notes[name] = "from/to symbols are not recorded together"
                        continue
                    r = self.d.rec.latency(rid, f["symbol"], f["op"], self.coerce(f["symbol"], f["value"]),
                                           t["symbol"], t["op"], self.coerce(t["symbol"], t["value"]),
                                           t_to=self._cut.get(rid))
                    pairs = r.get("samples") or []
                    agg = m.get("agg", "first")
                    tm = r.get("timing") or {}
                    if tm.get("resolution_ms"):
                        resolution[name] = float(tm["resolution_ms"])
                    if tm.get("trust") == "unreliable":
                        # D35: separate change-mode notifications — a latency from them is not a measurement
                        out[name] = None
                        notes[name] = (f"not measurable from this capture: {tm.get('why')}. Use a cycle-mode profile, or "
                                       "record from and to in one notification (same FB/struct)")
                    elif not pairs:
                        out[name] = None
                        notes[name] = f"no from→to pair ({r.get('unmatched_from_edges', 0)} unmatched from-edges)"
                        if r.get("from_true_at_start"):
                            # D34: the edge was before the first sample; the old code measured from the first sample
                            notes[name] += ("; the from-condition already held at the first sample — the edge happened "
                                            "before the recording delivered, its latency is unknown")
                    elif agg == "first":
                        out[name] = pairs[0]["latency_ms"]
                    elif agg == "max":
                        out[name] = r["latency_ms"]["max"]
                    else:
                        out[name] = r["latency_ms"]["mean"]
                    continue
                rid = self._rid_for(rec_ids, m["symbol"], *([m["from"]["symbol"]] if kind == "settle" and isinstance(m.get("from"), dict) else []))
                if rid is None:
                    out[name] = None
                    notes[name] = "symbol not in the run's recordings (add it to the capture profile)"
                    continue
                if kind == "pulses":
                    r = self.d.rec.pulses(rid, m["symbol"], t_to=self._cut.get(rid))
                    fld = m.get("field")
                    if fld:
                        cur: Any = r
                        for part in fld.split("."):
                            cur = (cur or {}).get(part) if isinstance(cur, dict) else None
                        out[name] = _num(cur)
                    else:
                        out[name] = r.get("pulses")
                    continue
                ev = self._events(rid, m["symbol"])
                if not ev:
                    out[name] = None
                    notes[name] = "no events for the symbol in this run"
                    continue
                vals = [_num(e["value"]) for e in ev]
                nums = [v for v in vals if v is not None]
                if kind == "first":
                    out[name] = nums[0] if nums else None
                elif kind == "last":
                    out[name] = nums[-1] if nums else None
                elif kind == "min":
                    out[name] = min(nums) if nums else None
                elif kind == "max":
                    out[name] = max(nums) if nums else None
                elif kind == "mean":
                    out[name] = sum(nums) / len(nums) if nums else None
                elif kind == "delta":
                    out[name] = (nums[-1] - nums[0]) if len(nums) >= 2 else None
                elif kind == "changes":
                    out[name] = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
                elif kind == "edges":
                    out[name] = sum(1 for a, b in zip(vals, vals[1:]) if (a or 0) == 0 and (b or 0) != 0)
                elif kind == "dwell":
                    out[name] = _dwell(ev, self.coerce(m["symbol"], m["value"]), m.get("mode", "equal"))
                    resolution[name] = float(self.d.rec.timing_of(rid, [m["symbol"]]).get("resolution_ms") or 0) or None
                elif kind == "settle":
                    anchor = None
                    if isinstance(m.get("from"), dict):
                        anchor = self._anchor(rid, m["from"])
                        if anchor is None:
                            out[name] = None
                            notes[name] = f"anchor {m['from']['symbol']} {m['from']['op']} {m['from']['value']} never became true"
                            continue
                    out[name] = _settle(ev, float(m.get("tolerance", 0.0)), anchor)
                    resolution[name] = float(self.d.rec.timing_of(rid, [m["symbol"]]).get("resolution_ms") or 0) or None
                if out.get(name) is None and name not in notes:
                    notes[name] = "no numeric events"
            except Exception as exc:  # noqa: BLE001
                out[name] = None
                notes[name] = str(exc)[:160]
        return out, notes

    def _anchor(self, rid: str, cond: dict[str, Any] | None) -> float | None:
        """PLC time (ms, this recording's clock) at which the condition first becomes true."""
        if not cond:
            return None
        ev = self._events(rid, cond["symbol"])
        op = _OPS.get(cond["op"])
        tgt = _num(self.coerce(cond["symbol"], cond["value"]))
        prev = False
        for e in ev:
            v = _num(e["value"])
            now = bool(op(v, tgt)) if (v is not None and tgt is not None and op) else False
            if now and not prev:
                return e["t_ms"]
            prev = now
        return None


def _unwrap(v: Any) -> Any:
    """ads.read decodes TIME/DATE/… as dicts ({'raw_ms':…,'text':…}); writes want the raw number."""
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in v:
                return v[k]
        if "value" in v and isinstance(v["value"], (int, float, bool, str)):
            return v["value"]
    return v


def _same(a: Any, b: Any) -> bool:
    a, b = _unwrap(a), _unwrap(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-9)
    return a == b


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns", "value"):
            if k in v and isinstance(v[k], (int, float)):
                return float(v[k])
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _dwell(ev: list[dict[str, Any]], target: Any, mode: str) -> float | None:
    """Total ms the symbol spent == target ('equal') or != target ('not_equal_total'), summed between
    consecutive events. Values compare numerically when both sides are numeric (enums arrive as ints),
    else as text — enum names in the spec are resolved to ints by the runner before this is called."""
    if len(ev) < 2:
        return None
    t_num = _num(target)

    def same(v: Any) -> bool:
        n = _num(v)
        if n is not None and t_num is not None:
            return n == t_num
        vv = v.get("name") if isinstance(v, dict) and "name" in v else v
        return str(vv) == str(target)

    total = 0.0
    for a, b in zip(ev, ev[1:]):
        inside = same(a["value"])
        if (mode == "equal" and inside) or (mode == "not_equal_total" and not inside):
            total += b["t_ms"] - a["t_ms"]
    return round(total, 3)


def _settle(ev: list[dict[str, Any]], tol: float, anchor_ms: float | None) -> float | None:
    nums = [(e["t_ms"], _num(e["value"])) for e in ev if _num(e["value"]) is not None]
    if len(nums) < 2:
        return None
    final = nums[-1][1]
    t_settle = nums[0][0]
    for t, v in reversed(nums):
        if abs(v - final) > tol:
            t_settle = t
            break
    start = anchor_ms if anchor_ms is not None else nums[0][0]
    return round(max(0.0, t_settle - start), 3)


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _fmt(v: Any, nd: int = 3) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}g}" if abs(v) < 1e-3 or abs(v) >= 1e6 else f"{v:.{nd}f}".rstrip("0").rstrip(".")
    if isinstance(v, list):
        return "[" + ", ".join(_fmt(x, nd) for x in v) + "]"
    return str(v)


def render_report(spec: dict[str, Any], runs: list[dict[str, Any]], ev: dict[str, Any], meta: dict[str, Any],
                  interpretation: str | None) -> tuple[str, str]:
    """(markdown, html). Computed sections first; interpretation last and labelled."""
    rep = spec.get("report") or {}
    title = rep.get("title") or f"Experiment {spec['name']}"
    L: list[str] = [f"# {title}", "",
                    f"**Experiment** `{spec['name']}` v{spec.get('version', 1)} · **Verdict: {ev['verdict'].upper()}** · "
                    f"{ev['runs_ok']} / {ev['runs_total']} runs valid", ""]
    if spec.get("hypothesis"):
        L += ["**Hypothesis.** " + spec["hypothesis"], ""]
    L += ["## Identity", "", "| | |", "|---|---|"]
    for k in ("machine_serial", "target", "device", "twincat_version", "project", "firmware_match", "git", "server_version",
              "safety_mode", "operator", "generated"):
        if meta.get(k) is not None:
            L.append(f"| {k.replace('_', ' ')} | {_fmt(meta[k])} |")
    L.append("")
    L += ["## Criteria", "", "| level | criterion | value | 95 % CI | n | verdict |", "|---|---|---|---|---|---|"]
    for lv in ev["levels"]:
        lvl = ", ".join(f"{k}={_fmt(v)}" for k, v in lv["level"].items()) or "—"
        for c in lv["criteria"]:
            extra = f" ({c.get('why')}; ~{c.get('n_needed_estimate')} runs to settle)" if c["verdict"] == "inconclusive" else ""
            L.append(f"| {lvl} | {c['criterion']} | {_fmt(c.get('value'))} | {_fmt(c.get('ci95'))} | {c.get('n', '—')} | **{c['verdict']}**{extra} |")
    L.append("")
    L += ["## Metrics per level", ""]
    for lv in ev["levels"]:
        lvl = ", ".join(f"{k}={_fmt(v)}" for k, v in lv["level"].items()) or "single level"
        L += [f"### {lvl} — {lv['runs_ok']} valid runs" + (f", {lv['runs_failed']} failed" if lv["runs_failed"] else ""), "",
              "| metric | value | 95 % CI | n |", "|---|---|---|---|"]
        for name, m in lv["metrics"].items():
            L.append(f"| {name} ({m['stat']} of {m['measure']}) | {_fmt(m['value'])} | {_fmt(m.get('ci95'))} | {m['n']} |")
        L += ["", "| measure | n | mean | std | min | max | p95 |", "|---|---|---|---|---|---|---|"]
        for name, st in lv["measures"].items():
            L.append(f"| {name} | {st.get('count', 0)} | {_fmt(st.get('mean'))} | {_fmt(st.get('std'))} | {_fmt(st.get('min'))} | {_fmt(st.get('max'))} | {_fmt(st.get('p95'))} |")
        L.append("")
    prim = rep.get("primary_measure") or next(iter(spec.get("measures") or {}), None)
    svg_runs = _svg_runs(spec, runs, prim) if prim else ""
    svg_levels = _svg_levels(ev, spec, prim) if prim else ""
    L += ["## Runs", "", "| # | level | status | " + " | ".join(spec.get("measures") or {}) + " | recordings | duration s |",
          "|---|---|---|" + "---|" * len(spec.get("measures") or {}) + "---|---|"]
    for r in runs:
        lvl = ", ".join(f"{k}={_fmt(v)}" for k, v in (r.get("level") or {}).items())
        ms = r.get("measures") or {}
        cells = " | ".join(_fmt(ms.get(k)) for k in (spec.get("measures") or {}))
        status = r.get("status") + (f" ({r.get('error')})" if r.get("error") else "")
        L.append(f"| {r.get('run_no')} | {lvl} | {status} | {cells} | {', '.join(r.get('recordings') or [])} | {_fmt(r.get('duration_s'))} |")
    L.append("")
    L += ["## Method (from the spec)", "", "```yaml" if yaml else "```json", dump_spec(spec).rstrip(), "```", ""]
    if meta.get("audit"):
        L += ["## Writes during the experiment (audit log)", "", "| time | tool | target | outcome |", "|---|---|---|---|"]
        for a in meta["audit"][:200]:
            L.append(f"| {a.get('ts')} | {a.get('tool')} | {_fmt((a.get('args') or {}).get('name') or (a.get('args') or {}).get('target') or (a.get('args') or {}).get('experiment'))} | {a.get('outcome')} |")
        L.append("")
    L += ["## Interpretation", ""]
    if interpretation:
        L += ["> Written by the engineer/agent, not computed. Everything above this heading is reproducible from the stored runs.", "",
              interpretation.strip(), ""]
    else:
        L += ["_None recorded. exp_report(interpretation=...) adds one; it is stored separately from the computed sections._", ""]
    L += ["---", f"_Generated by twincat-mcp {meta.get('server_version', '')} on {meta.get('generated', '')}. "
          f"Metrics: mean with t-based 95 % CI; σ with χ² CI (Wilson–Hilferty); Cpk = (tol − |mean|)/(3σ). "
          f"A criterion is inconclusive when its limit lies inside the CI._"]
    md = "\n".join(L)
    html = _html(title, md, [svg_runs, svg_levels])
    return md, html


def _svg_runs(spec, runs, prim) -> str:
    pts = [(i + 1, r["measures"].get(prim), json.dumps(r.get("level"), sort_keys=True)) for i, r in enumerate(runs)
           if r.get("status") == "ok" and isinstance((r.get("measures") or {}).get(prim), (int, float))]
    if not pts:
        return ""
    flat: list[float] = []
    for c in spec.get("criteria") or []:
        mt = (spec.get("metrics") or {}).get(c.get("metric") or "", {})
        on_prim = c.get("measure") == prim or mt.get("measure") == prim
        if not on_prim or (c.get("metric") and mt.get("stat") not in ("mean", "min", "max", "median", "p95")):
            continue   # σ / Cpk limits are not positions on the value axis
        l = c["value"]
        if isinstance(l, list):
            flat += [float(x) for x in l]
        elif c.get("op") == "abs_le":
            flat += [float(l), -float(l)]
        else:
            flat.append(float(l))
    ys = [p[1] for p in pts] + flat
    lo, hi = min(ys), max(ys)
    if hi == lo:
        hi = lo + 1
    pad = (hi - lo) * 0.1
    lo, hi = lo - pad, hi + pad
    W, H, ml, mb = 720, 260, 60, 30
    def X(i): return ml + (i - 1) * (W - ml - 20) / max(1, len(runs) - 1)
    def Y(v): return 10 + (hi - v) * (H - mb - 10) / (hi - lo)
    levels = sorted({p[2] for p in pts})
    colors = ["#2563eb", "#dc2626", "#059669", "#d97706", "#7c3aed", "#0891b2"]
    col = {l: colors[i % len(colors)] for i, l in enumerate(levels)}
    s = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px;font:12px sans-serif">']
    s.append(f'<line x1="{ml}" y1="10" x2="{ml}" y2="{H-mb}" stroke="#888"/><line x1="{ml}" y1="{H-mb}" x2="{W-20}" y2="{H-mb}" stroke="#888"/>')
    for v in (lo + pad, (lo + hi) / 2, hi - pad):
        s.append(f'<text x="{ml-6}" y="{Y(v)+4}" text-anchor="end" fill="#555">{_fmt(v)}</text>')
    for l in flat:
        s.append(f'<line x1="{ml}" y1="{Y(l)}" x2="{W-20}" y2="{Y(l)}" stroke="#dc2626" stroke-dasharray="4 3"/>')
    for i, v, l in pts:
        s.append(f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="4" fill="{col[l]}"><title>run {i}: {_fmt(v)}</title></circle>')
    s.append(f'<text x="{(ml+W)/2}" y="{H-8}" text-anchor="middle" fill="#555">run → ({prim}; dashed = criterion limit)</text>')
    for k, l in enumerate(levels):
        s.append(f'<rect x="{ml+8+k*170}" y="14" width="10" height="10" fill="{col[l]}"/><text x="{ml+22+k*170}" y="23" fill="#333">{l.strip("{}").replace(chr(34), "")[:26]}</text>')
    s.append("</svg>")
    return "".join(s)


def _svg_levels(ev, spec, prim) -> str:
    rows = [(", ".join(f"{k}={_fmt(v)}" for k, v in lv["level"].items()) or "all", lv["measures"].get(prim) or {}) for lv in ev["levels"]]
    rows = [(n, st) for n, st in rows if st.get("count")]
    if not rows:
        return ""
    vals = []
    for _, st in rows:
        vals += [st.get("min"), st.get("max")] + (st.get("ci95_mean") or [])
    vals = [v for v in vals if v is not None]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        hi = lo + 1
    pad = (hi - lo) * 0.1
    lo, hi = lo - pad, hi + pad
    W, H, ml, mb = 720, 220, 60, 40
    def Y(v): return 10 + (hi - v) * (H - mb - 10) / (hi - lo)
    step = (W - ml - 20) / len(rows)
    s = [f'<svg viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px;font:12px sans-serif">',
         f'<line x1="{ml}" y1="10" x2="{ml}" y2="{H-mb}" stroke="#888"/>']
    for v in (lo + pad, (lo + hi) / 2, hi - pad):
        s.append(f'<text x="{ml-6}" y="{Y(v)+4}" text-anchor="end" fill="#555">{_fmt(v)}</text>')
    for i, (name, st) in enumerate(rows):
        x = ml + step * (i + 0.5)
        s.append(f'<line x1="{x}" y1="{Y(st["min"])}" x2="{x}" y2="{Y(st["max"])}" stroke="#999"/>')
        if st.get("ci95_mean"):
            a, b = st["ci95_mean"]
            s.append(f'<rect x="{x-14}" y="{Y(b)}" width="28" height="{max(1, Y(a)-Y(b))}" fill="#2563eb" opacity="0.35"/>')
        s.append(f'<line x1="{x-18}" y1="{Y(st["mean"])}" x2="{x+18}" y2="{Y(st["mean"])}" stroke="#111" stroke-width="2"/>')
        s.append(f'<text x="{x}" y="{H-mb+16}" text-anchor="middle" fill="#333">{name[:28]}</text>')
        s.append(f'<text x="{x}" y="{H-mb+30}" text-anchor="middle" fill="#777">n={st["count"]}</text>')
    s.append(f'<text x="{(ml+W)/2}" y="{H-4}" text-anchor="middle" fill="#555">{prim}: mean (bar), 95 % CI of mean (box), min–max (line)</text>')
    s.append("</svg>")
    return "".join(s)


def _html(title: str, md: str, svgs: list[str]) -> str:
    """Minimal Markdown → HTML (headings, tables, code fences, paragraphs, bold/italic/code), charts after Criteria."""
    import html as _h
    out: list[str] = []
    lines = md.split("\n")
    i = 0
    in_code = False
    table: list[str] = []
    charts_done = False

    def flush_table():
        nonlocal table
        if not table:
            return
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table if not re.match(r"^\|?\s*-{3,}", r)]
        if rows:
            out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in rows[0]) + "</tr></thead><tbody>")
            for r in rows[1:]:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table>")
        table = []

    def _inline(t: str) -> str:
        t = _h.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
        t = re.sub(r"(?<![\w*])_(.+?)_(?![\w*])", r"<i>\1</i>", t)
        return t

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            flush_table()
            if in_code:
                out.append("</code></pre>")
            else:
                out.append("<pre><code>")
            in_code = not in_code
        elif in_code:
            out.append(_h.escape(ln))
        elif ln.startswith("|"):
            table.append(ln)
        else:
            flush_table()
            m = re.match(r"^(#{1,6})\s+(.*)", ln)
            if m:
                lvl = len(m.group(1))
                out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
                if m.group(2).startswith("Metrics per level") and not charts_done:
                    for sv in svgs:
                        if sv:
                            out.append(f'<figure>{sv}</figure>')
                    charts_done = True
            elif ln.startswith("> "):
                out.append(f"<blockquote>{_inline(ln[2:])}</blockquote>")
            elif ln.strip() == "---":
                out.append("<hr/>")
            elif ln.strip():
                out.append(f"<p>{_inline(ln)}</p>")
        i += 1
    flush_table()
    css = ("body{font:14px/1.45 system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#111}"
           "table{border-collapse:collapse;margin:8px 0 16px;font-size:13px}th,td{border:1px solid #ccc;padding:3px 8px;text-align:left}"
           "th{background:#f3f4f6}pre{background:#f6f8fa;padding:10px;overflow:auto;font-size:12px}code{font-size:12px}"
           "figure{margin:12px 0}blockquote{border-left:3px solid #d97706;margin:8px 0;padding:4px 10px;background:#fffbeb}"
           "h1{font-size:22px}h2{font-size:17px;margin-top:28px;border-bottom:1px solid #e5e7eb}h3{font-size:14px}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{_h.escape(title)}</title><style>{css}</style></head>"
            f"<body>{''.join(out)}</body></html>")
