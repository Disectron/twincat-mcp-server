"""Test-bench, diagnostics, snapshots and RPC helpers on top of the ADS backend.

Everything here is synchronous and runs on the server's ADS worker thread.
Writes are *not* gated here — the server checks the guard for every symbol a
scenario or stimulus will write before calling in.

* ``expect``            evaluate a list of conditions, optionally waiting
                        until all hold (within_ms) — assertion primitive.
* ``assert_stable``     value does not move (within tolerance) for a period —
                        settle checks on weights, pressures, PID outputs.
* ``measure_response``  write a stimulus, measure the PLC-timestamped latency
                        to a response condition via a 1 ms change recording.
* ``scenario``          scripted stimulus/expect sequence against a running
                        FB with a recording in parallel → pass/fail report.
* ``app_info`` / ``task_stats`` / ``ping``  runtime diagnostics from
                        TwinCAT_SystemInfoVarList and ADS round-trips.
* ``snapshot_save`` / ``snapshot_compare``  value sets for regression.
* ``call_method``       invoke a ``{attribute 'TcRpcEnable'}`` method via ADS.
"""
from __future__ import annotations

import ctypes
import json
import math
import struct
import time
from pathlib import Path
from typing import Any

import pyads
from pyads import constants as C

from . import types_tc as T
from .ads_backend import AdsBackend, AdsError, _fmt_exc
from .recorder import Recorder, Condition, _OPS, _numeric_dec

APP_INFO_MEMBERS = ["ObjId", "TaskCnt", "OnlineChangeCnt", "Flags", "AdsPort", "BootDataLoaded", "OldBootData",
                    "AppTimestamp", "KeepOutputsOnBP", "ShutdownInProgress", "LicensesPending", "BSODOccured",
                    "LoggedIn", "AppName", "ProjectName"]
TASK_INFO_MEMBERS = ["ObjId", "CycleTime", "Priority", "AdsPort", "CycleCount", "DcTaskTime", "LastExecTime",
                     "FirstCycle", "CycleTimeExceeded", "InCallAfterOutputUpdate", "RTViolation", "TaskName"]


class TestError(RuntimeError):
    pass


def _raw(v: Any) -> Any:
    """Decoded value → comparable raw (TIME dict → ms, etc.)."""
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in v:
                return v[k]
    return v


def _cmp(op: str, actual: Any, expected: Any, tolerance: float | None) -> bool:
    a = _raw(actual)
    if op == "==" and tolerance is not None and isinstance(a, (int, float)) and isinstance(expected, (int, float)):
        return abs(a - expected) <= tolerance
    if op == "~=":
        if not isinstance(a, (int, float)):
            return False
        return abs(a - expected) <= (tolerance if tolerance is not None else abs(expected) * 0.01)
    if op == "in":
        return a in expected if isinstance(expected, (list, tuple)) else False
    if op == "between":
        return isinstance(expected, (list, tuple)) and len(expected) == 2 and expected[0] <= a <= expected[1]
    try:
        return _OPS[op](a, expected)
    except (KeyError, TypeError):
        return False


class TestBench:
    def __init__(self, ads: AdsBackend, rec: Recorder, snapshot_dir: Path):
        self.ads = ads
        self.rec = rec
        self.snapshot_dir = snapshot_dir

    # ------------------------------------------------------------ expectations
    def _encode_expected(self, symbol: str, value: Any) -> Any:
        s = self.ads.symbol_info(symbol)
        tt = T.parse_type(s.type, s.size)
        if isinstance(value, (list, tuple)):
            return [self._encode_scalar(tt, v) for v in value]
        return self._encode_scalar(tt, value)

    @staticmethod
    def _encode_scalar(tt: T.TcType, v: Any) -> Any:
        if tt.ctype is None or tt.opaque:
            return v
        try:
            return T.encode(tt, v)
        except Exception:  # noqa: BLE001
            return v

    def expect(self, checks: list[dict[str, Any]], within_ms: int = 0, interval_ms: int = 20) -> dict[str, Any]:
        """checks: [{symbol, op, value, tolerance?}] op ∈ == != > >= < <= ~= in between.
        With within_ms > 0, polls until every check passes or the time is up."""
        if not checks:
            raise TestError("no checks given")
        prepared = []
        for ch in checks:
            if "symbol" not in ch or "op" not in ch or "value" not in ch:
                raise TestError(f"check needs symbol/op/value: {ch}")
            prepared.append({**ch, "_exp": self._encode_expected(ch["symbol"], ch["value"])})
        names = [c["symbol"] for c in prepared]
        t0 = time.perf_counter()
        deadline = t0 + within_ms / 1000.0
        met_at: dict[int, float] = {}
        last: dict[str, Any] = {}
        while True:
            vals = self.ads.read_many(names)
            now = (time.perf_counter() - t0) * 1000
            all_ok = True
            for i, c in enumerate(prepared):
                v = vals.get(c["symbol"])
                last[c["symbol"]] = v
                ok = not (isinstance(v, dict) and "error" in v) and _cmp(c["op"], v, c["_exp"], c.get("tolerance"))
                if ok and i not in met_at:
                    met_at[i] = now
                if not ok:
                    all_ok = False
                    met_at.pop(i, None)
            if all_ok or time.perf_counter() >= deadline:
                break
            time.sleep(interval_ms / 1000.0)
        results = []
        for i, c in enumerate(prepared):
            results.append({"symbol": c["symbol"], "op": c["op"], "expected": c["value"], "actual": last.get(c["symbol"]),
                            "pass": i in met_at, "met_after_ms": round(met_at[i], 1) if i in met_at else None})
        return {"pass": all(r["pass"] for r in results), "checks": results,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1), "within_ms": within_ms}

    def assert_stable(self, symbol: str, duration_ms: int = 1000, tolerance: float = 0.0,
                      interval_ms: int = 20) -> dict[str, Any]:
        s = self.ads.symbol_info(symbol)
        tt = T.parse_type(s.type, s.size)
        if tt.ctype is None or tt.opaque:
            raise TestError("assert_stable needs a scalar symbol")
        w = self.ads.watch([s.name], duration_ms, interval_ms, change_only=False)
        vals = [_raw(r[s.name]) for r in w["samples"]]
        if not vals:
            raise TestError("no samples")
        numeric = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals)
        if numeric:
            lo, hi = min(vals), max(vals)
            stable = (hi - lo) <= tolerance
            return {"symbol": s.name, "stable": stable, "min": lo, "max": hi, "spread": hi - lo, "tolerance": tolerance,
                    "mean": sum(vals) / len(vals), "samples": len(vals), "duration_ms": duration_ms}
        changes = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
        return {"symbol": s.name, "stable": changes == 0, "changes": changes, "first": vals[0], "last": vals[-1],
                "samples": len(vals), "duration_ms": duration_ms}

    # ------------------------------------------------------------ response timing
    def measure_response(self, stimulus: dict[str, Any], response: str, timeout_ms: int = 5000,
                         restore: bool = True) -> dict[str, Any]:
        """Write ``stimulus`` ({symbol: value}), measure how long until ``response``
        ('MAIN.bOut == TRUE') holds — both ends PLC-timestamped (1 ms change recording)."""
        cond_sym = response.split("==")[0].split("!=")[0].split(">")[0].split("<")[0].strip()
        syms = list(stimulus.keys()) + [cond_sym]
        before = {k: self.ads.read(k, expand_struct=False).get("value") for k in stimulus}
        st = self.rec.start(symbols=syms, prefix=None, pattern=None, mode="change", cycle_ms=1, duration_s=None,
                            max_events=200_000, start_when=None, stop_when=response, pre_trigger_ms=0,
                            name="measure_response", max_symbols=50)
        rid = st["id"]
        time.sleep(0.05)
        t_write = time.time()
        for k, v in stimulus.items():
            self.ads.write(k, v)
        deadline = time.time() + timeout_ms / 1000.0
        while self.rec.status(rid)["active"] and time.time() < deadline:
            time.sleep(0.005)
        out = self.rec.stop(rid)
        if restore:
            for k, v in before.items():
                try:
                    self.ads.write(k, _raw(v))
                except Exception:  # noqa: BLE001
                    pass
        rows = self.rec.read(rid, None, None, None, 100_000, False)["rows"]
        stim_t = None
        resp_t = None
        for r in rows:
            if r["symbol"] in stimulus and stim_t is None and r["value"] is not None:
                # first change of any stimulus symbol after the write
                if _raw(r["value"]) == _raw(self._encode_expected(r["symbol"], stimulus[r["symbol"]])):
                    stim_t = r["t_ms"]
            if r["symbol"] == cond_sym and stim_t is not None:
                resp_t = r["t_ms"]
        met = out.get("stop_reason", "").startswith("stop_when")
        return {"met": met, "latency_ms": None if (stim_t is None or resp_t is None or not met) else round(resp_t - stim_t, 3),
                "stimulus_seen_at_ms": stim_t, "response_at_ms": resp_t, "recording": rid,
                "wall_timeout_ms": timeout_ms, "note": "latency uses PLC notification timestamps; "
                "resolution = task cycle time" if met else "response condition not reached before timeout"}

    # ------------------------------------------------------------ scenario
    def scenario(self, spec: dict[str, Any]) -> dict[str, Any]:
        """spec = {name, record:{symbols|prefix|pattern, mode, cycle_ms}, steps:[...], final_expect:[...],
        timeout_ms, restore}. Step kinds: {at_ms?, write:{sym:val}} | {expect:[checks], within_ms}
        | {wait_ms} | {wait_until:{symbol,op,value,timeout_ms}} | {stable:{symbol,duration_ms,tolerance}}."""
        name = spec.get("name") or "scenario"
        steps = spec.get("steps") or []
        timeout_ms = int(spec.get("timeout_ms", 120_000))
        restore = bool(spec.get("restore", True))
        rec_spec = spec.get("record") or {}
        write_syms: list[str] = []
        for stp in steps:
            for k in (stp.get("write") or {}):
                if k not in write_syms:
                    write_syms.append(k)
        originals = {k: _raw(self.ads.read(k, expand_struct=False).get("value")) for k in write_syms}
        rid = None
        if rec_spec:
            rec_syms = list(rec_spec.get("symbols") or [])
            for k in write_syms:
                if k not in rec_syms:
                    rec_syms.append(k)
            st = self.rec.start(symbols=rec_syms or None, prefix=rec_spec.get("prefix"), pattern=rec_spec.get("pattern"),
                                mode=rec_spec.get("mode", "change"), cycle_ms=float(rec_spec.get("cycle_ms", 10)),
                                duration_s=None, max_events=int(rec_spec.get("max_events", 2_000_000)),
                                start_when=None, stop_when=None, pre_trigger_ms=0, name=name,
                                max_symbols=int(rec_spec.get("max_symbols", 500)))
            rid = st["id"]
            time.sleep(0.05)
        t0 = time.perf_counter()
        report: list[dict[str, Any]] = []
        overall = True
        try:
            for i, stp in enumerate(steps):
                now_ms = (time.perf_counter() - t0) * 1000
                if now_ms > timeout_ms:
                    report.append({"step": i, "error": "scenario timeout"}); overall = False
                    break
                if "at_ms" in stp:
                    wait = float(stp["at_ms"]) - now_ms
                    if wait > 0:
                        time.sleep(wait / 1000.0)
                entry: dict[str, Any] = {"step": i, "t_ms": round((time.perf_counter() - t0) * 1000, 1)}
                if "write" in stp:
                    res = {}
                    for k, v in stp["write"].items():
                        try:
                            r = self.ads.write(k, v)
                            res[k] = {"before": r.get("before"), "after": r.get("after")}
                        except AdsError as exc:
                            res[k] = {"error": str(exc)}; overall = False
                    entry["write"] = res
                if "wait_ms" in stp:
                    time.sleep(float(stp["wait_ms"]) / 1000.0)
                    entry["waited_ms"] = stp["wait_ms"]
                if "wait_until" in stp:
                    wu = stp["wait_until"]
                    r = self.ads.wait_until(wu["symbol"], wu.get("op", "=="), wu["value"], int(wu.get("timeout_ms", 10_000)),
                                            int(wu.get("interval_ms", 10)))
                    entry["wait_until"] = r
                    if not r["met"]:
                        overall = False
                if "expect" in stp:
                    r = self.expect(stp["expect"], int(stp.get("within_ms", 0)), int(stp.get("interval_ms", 20)))
                    entry["expect"] = r
                    if not r["pass"]:
                        overall = False
                if "stable" in stp:
                    sb = stp["stable"]
                    r = self.assert_stable(sb["symbol"], int(sb.get("duration_ms", 1000)), float(sb.get("tolerance", 0.0)),
                                           int(sb.get("interval_ms", 20)))
                    entry["stable"] = r
                    if not r["stable"]:
                        overall = False
                report.append(entry)
                if stp.get("stop_on_fail", True) and not overall and spec.get("stop_on_fail", False):
                    break
            if spec.get("final_expect"):
                r = self.expect(spec["final_expect"], int(spec.get("final_within_ms", 0)))
                report.append({"step": "final", "expect": r})
                if not r["pass"]:
                    overall = False
        finally:
            restored = {}
            if restore:
                for k, v in originals.items():
                    try:
                        self.ads.write(k, v)
                        restored[k] = v
                    except Exception as exc:  # noqa: BLE001
                        restored[k] = f"restore failed: {exc}"
            rec_out = None
            if rid:
                time.sleep(0.05)
                rec_out = self.rec.stop(rid)
        out = {"name": name, "pass": overall, "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
               "steps": report, "restored": restored if restore else None}
        if rec_out:
            out["recording"] = {"id": rid, "events": rec_out["events"], "file": rec_out["file"],
                                "stats": rec_out.get("stats")}
        return out

    # ------------------------------------------------------------ diagnostics
    def app_info(self) -> dict[str, Any]:
        names = [f"TwinCAT_SystemInfoVarList._AppInfo.{m}" for m in APP_INFO_MEMBERS]
        vals = self.ads.read_many(names)
        out = {m: vals.get(f"TwinCAT_SystemInfoVarList._AppInfo.{m}") for m in APP_INFO_MEMBERS}
        ts = _raw(out.get("AppTimestamp"))
        if isinstance(ts, int):
            import datetime as dt
            out["AppTimestamp_text"] = dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()
        return out

    def task_stats(self, task_index: int = 1, samples: int = 1, interval_ms: int = 50) -> dict[str, Any]:
        base = f"TwinCAT_SystemInfoVarList._TaskInfo[{task_index}]"
        names = [f"{base}.{m}" for m in TASK_INFO_MEMBERS]
        first = self.ads.read_many(names)
        if all(isinstance(v, dict) and "error" in v for v in first.values()):
            raise TestError(f"{base} not readable: {list(first.values())[0]}")
        exec_us: list[float] = []
        exceeded0 = _raw(first.get(f"{base}.CycleTimeExceeded"))
        count0 = _raw(first.get(f"{base}.CycleCount"))
        last_exec = _raw(first.get(f"{base}.LastExecTime"))
        if isinstance(last_exec, (int, float)):
            exec_us.append(last_exec / 10.0)
        t_start = time.perf_counter()
        for _ in range(max(0, samples - 1)):
            time.sleep(interval_ms / 1000.0)
            v = self.ads.read_many([f"{base}.LastExecTime", f"{base}.CycleTimeExceeded", f"{base}.CycleCount"])
            le = _raw(v.get(f"{base}.LastExecTime"))
            if isinstance(le, (int, float)):
                exec_us.append(le / 10.0)
            exceeded1 = _raw(v.get(f"{base}.CycleTimeExceeded"))
            count1 = _raw(v.get(f"{base}.CycleCount"))
        else:
            exceeded1, count1 = exceeded0, count0
        cycle_100ns = _raw(first.get(f"{base}.CycleTime"))
        cycle_us = cycle_100ns / 10.0 if isinstance(cycle_100ns, (int, float)) else None
        out: dict[str, Any] = {"task_index": task_index,
                               "task_name": first.get(f"{base}.TaskName"),
                               "priority": first.get(f"{base}.Priority"),
                               "ads_port": first.get(f"{base}.AdsPort"),
                               "cycle_time_us": cycle_us,
                               "cycle_count": count1,
                               "cycle_time_exceeded_total": exceeded1,
                               "exceeded_during_sample": (exceeded1 - exceeded0) if isinstance(exceeded1, int) and isinstance(exceeded0, int) else None,
                               "rt_violation": first.get(f"{base}.RTViolation"),
                               "samples": len(exec_us), "sample_window_ms": round((time.perf_counter() - t_start) * 1000, 1)}
        if exec_us:
            out["exec_time_us"] = {"last": exec_us[-1], "min": min(exec_us), "max": max(exec_us), "mean": sum(exec_us) / len(exec_us)}
            if cycle_us:
                out["load_percent"] = {"mean": round(100 * out["exec_time_us"]["mean"] / cycle_us, 2),
                                       "max": round(100 * out["exec_time_us"]["max"] / cycle_us, 2)}
        return out

    def ping(self, count: int = 10, port: int | None = None) -> dict[str, Any]:
        c = self.ads.conn(port)
        rtts = []
        for _ in range(max(1, min(count, 1000))):
            t = time.perf_counter()
            try:
                c.read_state()
            except Exception as exc:  # noqa: BLE001
                raise AdsError(f"ping failed: {_fmt_exc(exc)}") from exc
            rtts.append((time.perf_counter() - t) * 1000)
        rtts.sort()
        return {"port": c.ams_net_port, "count": len(rtts), "min_ms": round(rtts[0], 3), "median_ms": round(rtts[len(rtts) // 2], 3),
                "max_ms": round(rtts[-1], 3), "mean_ms": round(sum(rtts) / len(rtts), 3)}

    # ------------------------------------------------------------ snapshots
    def snapshot_save(self, name: str, symbols: list[str] | None, prefix: str | None, pattern: str | None,
                      max_symbols: int = 2000) -> dict[str, Any]:
        syms = self.rec.expand_symbols(symbols, prefix, pattern, max_symbols)
        vals = self.ads.read_many([s.name for s in syms])
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        p = self.snapshot_dir / f"{_safe(name)}.json"
        doc = {"name": name, "taken": time.strftime("%Y-%m-%dT%H:%M:%S"), "ams_net_id": self.ads.net_id,
               "port": self.ads.port, "values": {s.name: {"type": s.type, "value": vals.get(s.name)} for s in syms}}
        p.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
        return {"name": name, "file": str(p), "symbols": len(syms)}

    def snapshot_compare(self, a: str, b: str = "live", tolerance: float = 0.0, symbols: list[str] | None = None) -> dict[str, Any]:
        da = self._load_snapshot(a)
        if b == "live":
            names = list(da["values"].keys())
            live = self.ads.read_many(names)
            db = {"name": "live", "values": {n: {"type": da["values"][n]["type"], "value": live.get(n)} for n in names}}
        else:
            db = self._load_snapshot(b)
        want = {s.lower() for s in symbols} if symbols else None
        diffs, same, missing = [], 0, []
        for n, ea in da["values"].items():
            if want and n.lower() not in want:
                continue
            if n not in db["values"]:
                missing.append(n); continue
            va, vb = _raw(ea["value"]), _raw(db["values"][n]["value"])
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and not isinstance(va, bool):
                if abs(va - vb) <= tolerance:
                    same += 1; continue
            elif va == vb:
                same += 1; continue
            diffs.append({"symbol": n, "type": ea["type"], "a": ea["value"], "b": db["values"][n]["value"]})
        return {"a": da["name"], "b": db["name"], "compared": same + len(diffs), "same": same, "different": len(diffs),
                "diffs": diffs[:500], "missing_in_b": missing[:200], "tolerance": tolerance}

    def snapshot_apply(self, name: str, symbols: list[str] | None = None, dry_run: bool = False) -> dict[str, Any]:
        """Write a snapshot's values back (recipe/parameter restore). The server gates each
        symbol before calling this; dry_run lists the differences only."""
        d = self._load_snapshot(name)
        want = {s.lower() for s in symbols} if symbols else None
        live = self.ads.read_many(list(d["values"].keys()))
        plan, done, errors = [], [], []
        for n, ent in d["values"].items():
            if want and n.lower() not in want:
                continue
            tv = _raw(ent["value"])
            lv = _raw(live.get(n))
            if isinstance(tv, dict) or tv is None:
                continue
            if lv == tv:
                continue
            plan.append({"symbol": n, "from": lv, "to": tv})
            if not dry_run:
                try:
                    self.ads.write(n, tv)
                    done.append(n)
                except AdsError as exc:
                    errors.append({"symbol": n, "error": str(exc)})
        return {"snapshot": name, "differences": plan, "written": done, "errors": errors, "dry_run": dry_run}

    def snapshot_list(self) -> list[dict[str, Any]]:
        if not self.snapshot_dir.is_dir():
            return []
        out = []
        for p in sorted(self.snapshot_dir.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out.append({"name": d.get("name"), "taken": d.get("taken"), "symbols": len(d.get("values", {})), "file": str(p)})
            except Exception:  # noqa: BLE001
                continue
        return out

    def _load_snapshot(self, name: str) -> dict[str, Any]:
        p = self.snapshot_dir / f"{_safe(name)}.json"
        if not p.is_file():
            raise TestError(f"snapshot {name!r} not found under {self.snapshot_dir}")
        return json.loads(p.read_text(encoding="utf-8"))

    # ------------------------------------------------------------ RPC
    def call_method(self, instance: str, method: str, inputs: list[dict[str, Any]] | None,
                    return_type: str | None, outputs: list[str] | None, port: int | None = None) -> dict[str, Any]:
        """Invoke ``instance.method`` via ADS RPC. The method needs {attribute 'TcRpcEnable'}.
        inputs: [{type: 'LREAL', value: 1.5}, …] in VAR_INPUT order; outputs: ['BOOL', 'DINT'] in
        VAR_OUTPUT order. Parameters are packed without padding (TwinCAT RPC layout)."""
        c = self.ads.conn(port)
        in_bytes = b"".join(pack_value(p["type"], p["value"]) for p in (inputs or []))
        out_types = ([return_type] if return_type and return_type.upper() not in ("", "VOID") else []) + list(outputs or [])
        out_size = sum(T.parse_type(t, None).size or 0 for t in out_types)
        rpc_name = f"{instance}#{method}"
        try:
            handle = c.read_write(C.ADSIGRP_SYM_HNDBYNAME, 0, C.PLCTYPE_UDINT, rpc_name, C.PLCTYPE_STRING)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"no RPC handle for {rpc_name!r} (is the method marked {{attribute 'TcRpcEnable'}}?): {_fmt_exc(exc)}") from exc
        try:
            data = c.read_write(C.ADSIGRP_SYM_VALBYHND, handle,
                                (ctypes.c_ubyte * out_size) if out_size else None,
                                list(in_bytes), (ctypes.c_ubyte * len(in_bytes)) if in_bytes else None,
                                return_ctypes=True, check_length=False)
            raw = bytes(data) if data is not None else b""
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"RPC call {rpc_name!r} failed: {_fmt_exc(exc)}") from exc
        finally:
            try:
                c.write(C.ADSIGRP_SYM_RELEASEHND, 0, handle, C.PLCTYPE_UDINT)
            except Exception:  # noqa: BLE001
                pass
        res: dict[str, Any] = {"method": rpc_name, "raw_out_hex": raw.hex()}
        off = 0
        decoded = []
        for t in out_types:
            v, off = unpack_value(t, raw, off)
            decoded.append(v)
        if return_type and return_type.upper() not in ("", "VOID"):
            res["return"] = decoded[0] if decoded else None
            res["outputs"] = decoded[1:]
        else:
            res["outputs"] = decoded
        return res


# ----------------------------------------------------------------------------
_FMT = {"bool": "?", "int": {1: "b", 2: "h", 4: "i", 8: "q"}, "uint": {1: "B", 2: "H", 4: "I", 8: "Q"},
        "float": {4: "f", 8: "d"}}


def pack_value(type_str: str, value: Any) -> bytes:
    tt = T.parse_type(type_str, None)
    if tt.kind in ("string",):
        s = str(value).encode("latin-1", "replace")[: (tt.size or 81) - 1]
        return s.ljust(tt.size or 81, b"\x00")
    if tt.kind == "wstring":
        s = str(value).encode("utf-16-le")[: (tt.size or 162) - 2]
        return s.ljust(tt.size or 162, b"\x00")
    if tt.kind == "array" and tt.elem:
        return b"".join(pack_value(tt.elem.name, v) for v in T._flatten(value))
    enc = T.encode(tt, value)
    if tt.kind == "bool":
        return struct.pack("<?", bool(enc))
    if tt.kind == "float":
        return struct.pack("<" + _FMT["float"][tt.size or 8], float(enc))
    size = tt.size or 4
    signed = tt.kind in ("int", "enum")
    return struct.pack("<" + (_FMT["int"] if signed else _FMT["uint"])[size], int(enc))


def unpack_value(type_str: str, buf: bytes, off: int) -> tuple[Any, int]:
    tt = T.parse_type(type_str, None)
    size = tt.size or 0
    chunk = buf[off: off + size]
    if len(chunk) < size:
        return None, off + size
    if tt.kind == "string":
        return chunk.split(b"\x00", 1)[0].decode("latin-1", "replace"), off + size
    if tt.kind == "wstring":
        return chunk.decode("utf-16-le", "replace").split("\x00", 1)[0], off + size
    if tt.kind == "array" and tt.elem:
        vals = []
        o = off
        n = size // (tt.elem.size or 1)
        for _ in range(n):
            v, o = unpack_value(tt.elem.name, buf, o)
            vals.append(v)
        return vals, o
    if tt.kind == "bool":
        return bool(chunk[0]), off + size
    if tt.kind == "float":
        return struct.unpack("<" + _FMT["float"][size], chunk)[0], off + size
    signed = tt.kind in ("int", "enum")
    raw = struct.unpack("<" + (_FMT["int"] if signed else _FMT["uint"])[size], chunk)[0]
    return T.decode(tt, raw), off + size


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)[:80]
