"""Real-time recorder for online PLC variables.

Runs *alongside* the PLC program: start a recording, exercise the machine
(or let ``xae_deploy`` / ``tcunit_run`` do it), query it while it runs, stop,
export. Three capture modes:

``cycle``   ADS device notifications, ADSTRANS_SERVERCYCLE: one sample per
            symbol per ``cycle_ms``, time-stamped by the PLC (100 ns FILETIME)
            — cycle-accurate, independent of this PC's scheduling.
``change``  ADS device notifications, ADSTRANS_SERVERONCHA: a sample only when
            the value changes (checked every ``cycle_ms``); cheap for booleans,
            enums and state machines.
``poll``    sum-read every ``cycle_ms`` from a background thread, timestamped
            here. Works everywhere, including ports that do not support
            notifications.

Every event (t_ms relative to start, symbol, value) is appended to a JSONL
file under ``~/.twincat_mcp/recordings/<id>.jsonl`` as it arrives, so a
recording survives this process and can be gigabytes without being in RAM.
In RAM: a ring of the most recent events (``ring_events``) for snapshots and
*incremental statistics* per symbol (count, min, max, mean, last, change
count, boolean edges, value runs for state timelines) — O(1) memory per
symbol regardless of recording length.

Triggers: ``start_when`` arms the recording and keeps a pre-trigger ring
(``pre_trigger_ms``) so the events *before* the condition fired are kept,
like a scope. ``stop_when`` and ``duration_s`` / ``max_events`` end it.

Thread model: notification callbacks run on the ADS DLL thread and only
enqueue. One ingest thread per recording drains the queue, updates stats,
writes the file. Registration/deregistration of notifications happens on
the caller's thread (the server's ADS worker).
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import math
import os
import queue
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pyads
from pyads import constants as C

from . import types_tc as T
from .ads_backend import AdsBackend, AdsError, SymbolEntry, _fmt_exc

FILETIME_EPOCH_OFFSET_100NS = 116444736000000000  # 1601 → 1970

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
}
_RE_COND = re.compile(r"^\s*(?P<sym>[A-Za-z_][\w\.\[\]]*)\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<val>.+?)\s*$")


class RecorderError(RuntimeError):
    pass


@dataclass
class Condition:
    symbol: str
    op: str
    value: Any

    @classmethod
    def parse(cls, text: str, types: dict[str, T.TcType]) -> "Condition":
        m = _RE_COND.match(text)
        if not m:
            raise RecorderError(f"condition {text!r} must look like 'MAIN.bDone == TRUE' or 'MAIN.nStep >= 4'")
        sym = m.group("sym")
        key = next((k for k in types if k.lower() == sym.lower()), None)
        if key is None:
            raise RecorderError(f"condition symbol {sym!r} is not part of the recording; add it to symbols")
        raw = m.group("val").strip().strip("'\"")
        try:
            val = T.encode(types[key], raw)
        except Exception:  # noqa: BLE001
            val = raw
        return cls(key, m.group("op"), val)

    def test(self, value: Any) -> bool:
        try:
            v = value["raw_ms"] if isinstance(value, dict) and "raw_ms" in value else value
            v = v["raw"] if isinstance(v, dict) and "raw" in v else v
            v = v["raw_ns"] if isinstance(v, dict) and "raw_ns" in v else v
            return _OPS[self.op](v, self.value)
        except Exception:  # noqa: BLE001
            return False


@dataclass
class SymStats:
    count: int = 0
    first: Any = None
    last: Any = None
    last_t: float | None = None
    changes: int = 0
    num_min: float | None = None
    num_max: float | None = None
    num_sum: float = 0.0
    num_n: int = 0
    rising: int = 0
    falling: int = 0
    first_edges: list[dict[str, Any]] = field(default_factory=list)
    runs: list[list[Any]] = field(default_factory=list)   # [t_start, t_end, value]
    runs_truncated: bool = False

    def ingest(self, t: float, v: Any, raw_numeric: float | None, max_runs: int) -> None:
        self.count += 1
        if self.count == 1:
            self.first = v
        if raw_numeric is not None and not (isinstance(raw_numeric, float) and math.isnan(raw_numeric)):
            self.num_n += 1
            self.num_sum += raw_numeric
            self.num_min = raw_numeric if self.num_min is None else min(self.num_min, raw_numeric)
            self.num_max = raw_numeric if self.num_max is None else max(self.num_max, raw_numeric)
        if self.last_t is not None and v != self.last:
            self.changes += 1
            if isinstance(v, bool) and isinstance(self.last, bool):
                if v:
                    self.rising += 1
                else:
                    self.falling += 1
                if len(self.first_edges) < 50:
                    self.first_edges.append({"t_ms": round(t, 3), "edge": "rising" if v else "falling"})
        if not self.runs or self.runs[-1][2] != v:
            if self.runs:
                self.runs[-1][1] = t
            if len(self.runs) < max_runs:
                self.runs.append([t, t, v])
            else:
                self.runs_truncated = True
        else:
            self.runs[-1][1] = t
        self.last = v
        self.last_t = t

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"count": self.count, "first": self.first, "last": self.last,
                             "last_t_ms": None if self.last_t is None else round(self.last_t, 3), "changes": self.changes}
        if self.num_n:
            d.update({"min": self.num_min, "max": self.num_max, "mean": self.num_sum / self.num_n})
        if self.rising or self.falling:
            d.update({"rising_edges": self.rising, "falling_edges": self.falling, "first_edges": self.first_edges})
        return d


class Recording:
    def __init__(self, rid: str, name: str, symbols: list[SymbolEntry], mode: str, cycle_ms: float,
                 path: Path, ring_events: int, max_runs: int, duration_s: float | None,
                 max_events: int | None, start_when: Condition | None, stop_when: Condition | None,
                 pre_trigger_ms: float, meta: dict[str, Any]):
        self.id, self.name, self.mode, self.cycle_ms, self.path = rid, name, mode, cycle_ms, path
        self.symbols = symbols
        self.types: dict[str, T.TcType] = {s.name: T.parse_type(s.type, s.size) for s in symbols}
        self.ring: deque[tuple[float, str, Any]] = deque(maxlen=ring_events)
        self.stats: dict[str, SymStats] = {s.name: SymStats() for s in symbols}
        self.max_runs = max_runs
        self.duration_s, self.max_events = duration_s, max_events
        self.start_when, self.stop_when, self.pre_trigger_ms = start_when, stop_when, pre_trigger_ms
        self.armed = start_when is not None
        self.pre: deque[tuple[float, str, Any]] = deque()
        self.meta = meta
        self.q: "queue.Queue[tuple[int | float, str, Any] | None]" = queue.Queue(maxsize=200_000)
        self.events = 0
        self.dropped = 0
        self.errors: list[str] = []
        self.created = time.time()
        self.t0_wall: float | None = None          # wall clock of first event
        self.t0_ft: int | None = None              # FILETIME of first event (notification modes)
        self.triggered_at: float | None = None
        self.stopped_at: float | None = None
        self.stop_reason: str | None = None
        self.last_values: dict[str, Any] = {}
        self._fh = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.handles: list[tuple[int, int]] = []
        self.conn: pyads.Connection | None = None
        self.on_finish: Callable[[], None] | None = None   # set by Recorder; runs on the ingest thread when the recording ends
        self._poll_thread: threading.Thread | None = None

    # ------------------------------------------------------------ lifecycle
    def start_ingest(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a", encoding="utf-8", buffering=1 << 16)
        header = {"_header": True, "id": self.id, "name": self.name, "mode": self.mode, "cycle_ms": self.cycle_ms,
                  "symbols": [{"name": s.name, "type": s.type, "size": s.size} for s in self.symbols],
                  "created": dt.datetime.fromtimestamp(self.created, dt.timezone.utc).isoformat(), **self.meta}
        self._fh.write(json.dumps(header, default=str) + "\n")
        self._thread = threading.Thread(target=self._ingest_loop, name=f"rec-{self.id}", daemon=True)
        self._thread.start()

    def enqueue(self, ts: int | float, name: str, value: Any) -> None:
        """Called from the ADS callback thread (notification) or the poll thread."""
        try:
            self.q.put_nowait((ts, name, value))
        except queue.Full:
            self.dropped += 1

    def _rel_ms(self, ts: int | float) -> float:
        if self.mode == "poll":
            if self.t0_wall is None:
                self.t0_wall = float(ts)
            return (float(ts) - self.t0_wall) * 1000.0
        if self.t0_ft is None:
            self.t0_ft = int(ts)
            self.t0_wall = (int(ts) - FILETIME_EPOCH_OFFSET_100NS) / 1e7
        return (int(ts) - self.t0_ft) / 1e4

    def _ingest_loop(self) -> None:
        while True:
            try:
                item = self.q.get(timeout=0.25)
            except queue.Empty:
                self._check_limits()
                if self._stop.is_set():
                    break
                continue
            if item is None:
                break
            ts, name, value = item
            t = self._rel_ms(ts)
            self._accept(t, name, value)
            self._check_limits()
            if self._stop.is_set():
                break          # a limit was reached: do not keep draining notifications that are still arriving
        self._finish()

    def _accept(self, t: float, name: str, value: Any) -> None:
        tt = self.types.get(name)
        if tt and tt.kind == "wstring" and isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-16-le", "replace").split("\x00", 1)[0]
        elif isinstance(value, (bytes, bytearray)):
            value = bytes(value).hex()
        dec = T.decode(tt, value) if tt else value
        raw_num = _numeric_dec(dec)
        with self._lock:
            self.last_values[name] = dec
            if self.armed:
                self.pre.append((t, name, dec))
                while self.pre and t - self.pre[0][0] > self.pre_trigger_ms:
                    self.pre.popleft()
                if self.start_when and name == self.start_when.symbol and self.start_when.test(dec):
                    self.armed = False
                    self.triggered_at = t
                    for pt, pn, pv in self.pre:
                        self._commit(pt, pn, pv, _numeric_dec(pv))
                    self.pre.clear()
                return
            self._commit(t, name, dec, raw_num)
            if self.stop_when and name == self.stop_when.symbol and self.stop_when.test(dec):
                self.stop_reason = f"stop_when {self.stop_when.symbol} {self.stop_when.op} {self.stop_when.value}"
                self._stop.set()

    def _commit(self, t: float, name: str, dec: Any, raw_num: float | None) -> None:
        self.events += 1
        self.ring.append((t, name, dec))
        self.stats[name].ingest(t, dec, raw_num, self.max_runs)
        if self._fh:
            self._fh.write(json.dumps([round(t, 3), name, dec], default=str) + "\n")

    def _check_limits(self) -> None:
        if self._stop.is_set():
            return
        if self.max_events and self.events >= self.max_events:
            self.stop_reason = f"max_events {self.max_events}"
            self._stop.set()
        elif self.duration_s and self.t0_wall and (time.time() - self.t0_wall) >= self.duration_s:
            self.stop_reason = f"duration {self.duration_s}s"
            self._stop.set()
        elif self.duration_s and not self.t0_wall and (time.time() - self.created) >= self.duration_s + 5:
            self.stop_reason = "duration elapsed with no data (symbols never updated?)"
            self._stop.set()

    def request_stop(self, reason: str = "requested") -> None:
        if not self._stop.is_set():
            self.stop_reason = self.stop_reason or reason
            self._stop.set()

    def _finish(self) -> None:
        self.stopped_at = time.time()
        cb = self.on_finish
        if cb is not None:
            try:
                cb()                   # Recorder: deregister the notifications now, not at the next rec_list
            except Exception as exc:  # noqa: BLE001
                if len(self.errors) < 20:
                    self.errors.append(f"release on finish: {exc}")
        with self._lock:
            for st in self.stats.values():
                if st.runs and st.last_t is not None:
                    st.runs[-1][1] = st.last_t
            if self._fh:
                self._fh.write(json.dumps({"_footer": True, "events": self.events, "dropped": self.dropped,
                                           "stop_reason": self.stop_reason, "stopped": dt.datetime.now(dt.timezone.utc).isoformat()}) + "\n")
                self._fh.close()
                self._fh = None

    @property
    def active(self) -> bool:
        return self.stopped_at is None

    # ------------------------------------------------------------ views
    def status(self) -> dict[str, Any]:
        with self._lock:
            elapsed = (time.time() - self.t0_wall) if self.t0_wall and self.active else (
                (self.stopped_at - self.t0_wall) if (self.t0_wall and self.stopped_at) else 0.0)
            return {
                "id": self.id, "name": self.name, "active": self.active, "mode": self.mode, "cycle_ms": self.cycle_ms,
                "symbols": len(self.symbols), "events": self.events, "dropped": self.dropped,
                "events_per_s": round(self.events / elapsed, 1) if elapsed > 0 else 0.0,
                "elapsed_s": round(elapsed, 2), "armed": self.armed,
                "triggered_at_ms": None if self.triggered_at is None else round(self.triggered_at, 3),
                "stop_reason": self.stop_reason, "file": str(self.path),
                "file_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "last_values": dict(self.last_values), "errors": self.errors[-5:],
            }

    def snapshot(self, last_n: int, symbols: list[str] | None) -> list[dict[str, Any]]:
        with self._lock:
            ev = list(self.ring)
        if symbols:
            want = {s.lower() for s in symbols}
            ev = [e for e in ev if e[1].lower() in want]
        return [{"t_ms": round(t, 3), "symbol": n, "value": v} for t, n, v in ev[-last_n:]]

    def stats_dict(self, symbols: list[str] | None, include_runs: bool, max_runs_out: int) -> dict[str, Any]:
        with self._lock:
            out = {}
            for name, st in self.stats.items():
                if symbols and name.lower() not in {s.lower() for s in symbols}:
                    continue
                d = st.to_dict()
                if include_runs:
                    runs = st.runs[-max_runs_out:]
                    d["runs"] = [{"t_start_ms": round(a, 3), "t_end_ms": round(b, 3), "dwell_ms": round(b - a, 3), "value": v} for a, b, v in runs]
                    d["runs_truncated"] = st.runs_truncated or len(st.runs) > max_runs_out
                out[name] = d
            return out


def _numeric_dec(dec: Any) -> float | None:
    if isinstance(dec, bool):
        return 1.0 if dec else 0.0
    if isinstance(dec, (int, float)):
        return float(dec)
    if isinstance(dec, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in dec:
                return float(dec[k])
    return None


class Recorder:
    """Owns all recordings. Methods are synchronous; the server calls the
    registration ones on its ADS worker."""

    def __init__(self, ads: AdsBackend, root: Path, ring_events: int = 200_000, max_runs: int = 20_000):
        self.ads = ads
        self.root = root
        self.ring_events = ring_events
        self.max_runs = max_runs
        self.recs: dict[str, Recording] = {}

    # ------------------------------------------------------------ symbol selection
    def expand_symbols(self, symbols: list[str] | None, prefix: str | None, pattern: str | None,
                       max_symbols: int, port: int | None = None) -> list[SymbolEntry]:
        """Resolve a selection to scalar leaves. Composite symbols (FB/struct instances,
        arrays of structs) are expanded through the datatype table, so 'MAIN' or
        'MAIN.fbFill' really means every variable beneath it."""
        lst, idx = self.ads.symbols_cached(False, port)
        chosen: dict[str, SymbolEntry] = {}

        def add_leaves(root: SymbolEntry, leaves) -> None:
            for leaf in leaves:
                if leaf.size <= 0 or leaf.note in ("reference/pointer",) or leaf.note and "not expanded" in leaf.note:
                    continue
                tt = T.parse_type(leaf.type, leaf.size)
                if tt.ctype is None or tt.opaque:
                    continue
                chosen[leaf.path] = SymbolEntry(leaf.path, leaf.type, leaf.size, root.index_group,
                                                root.index_offset + leaf.offset, 0, "")

        for n in symbols or []:
            s = idx.get(n.lower())
            if s is None:
                # nested element not in the table (e.g. MAIN.arr[2].x) → ask the runtime
                try:
                    s = self.ads.symbol_info(n, port)
                except AdsError:
                    raise AdsError(f"symbol {n!r} not found in the runtime symbol table")
            tt = T.parse_type(s.type, s.size)
            if tt.ctype is None or tt.opaque:
                root, leaves = self.ads.leaves(s.name, max_symbols + 1, port)
                add_leaves(root, leaves)
            else:
                chosen[s.name] = s
        if prefix:
            groups = self.ads.leaves_under(prefix, max_symbols + 1, port)
            if not groups:
                raise AdsError(f"prefix {prefix!r} matches no symbol")
            for root, leaves in groups:
                add_leaves(root, leaves)
        if pattern:
            import fnmatch
            pat = pattern.lower()
            for s in lst:
                if fnmatch.fnmatchcase(s.name.lower(), pat):
                    tt = T.parse_type(s.type, s.size)
                    if tt.ctype is not None and not tt.opaque:
                        chosen[s.name] = s
        out = list(chosen.values())
        if not out:
            raise AdsError("no recordable (scalar/array) symbols selected")
        if len(out) > max_symbols:
            raise AdsError(f"{len(out)} symbols selected, limit {max_symbols}; narrow the selection or raise max_symbols")
        return out

    # ------------------------------------------------------------ start / stop
    def start(self, *, symbols: list[str] | None, prefix: str | None, pattern: str | None, mode: str,
              cycle_ms: float, duration_s: float | None, max_events: int | None, start_when: str | None,
              stop_when: str | None, pre_trigger_ms: float, name: str | None, max_symbols: int,
              port: int | None = None) -> dict[str, Any]:
        if mode not in ("cycle", "change", "poll"):
            raise RecorderError("mode must be cycle | change | poll")
        cycle_ms = max(float(cycle_ms), 0.1)
        syms = self.expand_symbols(symbols, prefix, pattern, max_symbols, port)
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        path = self.root / f"{rid}.jsonl"
        types = {s.name: T.parse_type(s.type, s.size) for s in syms}
        sw = Condition.parse(start_when, types) if start_when else None
        ew = Condition.parse(stop_when, types) if stop_when else None
        rate = len(syms) * 1000.0 / cycle_ms
        meta = {"ams_net_id": self.ads.net_id, "port": port or self.ads.port, "start_when": start_when,
                "stop_when": stop_when, "pre_trigger_ms": pre_trigger_ms}
        rec = Recording(rid, name or rid, syms, mode, cycle_ms, path, self.ring_events, self.max_runs,
                        duration_s, max_events, sw, ew, pre_trigger_ms, meta)
        rec.start_ingest()
        try:
            if mode == "poll":
                self._start_poll(rec, port)
            else:
                self._start_notifications(rec, port)
        except Exception:
            rec.request_stop("start failed")
            raise
        self.recs[rid] = rec
        st = rec.status()
        st["estimated_events_per_s"] = round(rate, 1) if mode == "cycle" else f"≤{round(rate, 1)}"
        if mode == "cycle" and rate > 20_000:
            st["warning"] = "high notification rate; expect drops. Use mode='change', a larger cycle_ms or fewer symbols."
        return st

    def _start_notifications(self, rec: Recording, port: int | None) -> None:
        base = self.ads.conn(port)
        conn = pyads.Connection(base.ams_net_id, base.ams_net_port, base.ip_address)
        try:
            conn.open()
            conn.set_timeout(self.ads.cfg.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"recorder connection failed: {_fmt_exc(exc)}") from exc
        rec.conn = conn
        rec.on_finish = lambda: self._stop_notifications(rec)
        trans = C.ADSTRANS_SERVERCYCLE if rec.mode == "cycle" else C.ADSTRANS_SERVERONCHA
        types = rec.types

        def make_cb(sym_name: str, plc_type: Any) -> Callable:
            def cb(notification: Any, data_name: str) -> None:
                try:
                    _h, ts, value = conn.parse_notification(notification, plc_type, timestamp_as_filetime=True)
                except Exception as exc:  # noqa: BLE001
                    if len(rec.errors) < 20:
                        rec.errors.append(f"{sym_name}: {exc}")
                    return
                rec.enqueue(ts, sym_name, value)
            return cb

        failed = []
        for s in rec.symbols:
            tt = types[s.name]
            attr = pyads.NotificationAttrib(s.size if tt.kind in ("string", "wstring") else ctypes.sizeof(tt.ctype),
                                            trans, max_delay=0, cycle_time=rec.cycle_ms)
            try:
                h = conn.add_device_notification(s.name, attr, make_cb(s.name, tt.ctype))
                if h:
                    rec.handles.append(h)
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{s.name}: {_fmt_exc(exc)}")
        if failed:
            rec.errors.extend(failed[:20])
            if len(failed) == len(rec.symbols):
                self._stop_notifications(rec)
                raise AdsError("no notification could be registered: " + failed[0])

    def _stop_notifications(self, rec: Recording) -> None:
        with rec._lock:
            conn, rec.conn = rec.conn, None
            handles, rec.handles = list(rec.handles), []
        if conn is None:
            return
        for nh, uh in handles:
            try:
                conn.del_device_notification(nh, uh)
            except Exception:  # noqa: BLE001
                pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _start_poll(self, rec: Recording, port: int | None) -> None:
        base = self.ads.conn(port)
        conn = pyads.Connection(base.ams_net_id, base.ams_net_port, base.ip_address)
        try:
            conn.open()
            conn.set_timeout(self.ads.cfg.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"recorder connection failed: {_fmt_exc(exc)}") from exc
        rec.conn = conn
        names = [s.name for s in rec.symbols]
        types = rec.types
        interval = rec.cycle_ms / 1000.0

        def loop() -> None:
            use_sum = True
            while not rec._stop.is_set():
                t_cycle = time.perf_counter()
                ts = time.time()
                try:
                    if use_sum:
                        row = conn.read_list_by_name(names)
                        if any(isinstance(v, str) and v.startswith("error") for v in row.values()) and len(names) == 1:
                            raise RuntimeError("sum read error")
                    else:
                        row = {n: conn.read_by_name(n, types[n].ctype) for n in names}
                except Exception as exc:  # noqa: BLE001
                    if use_sum:
                        use_sum = False
                        continue
                    if len(rec.errors) < 20:
                        rec.errors.append(f"poll: {_fmt_exc(exc)}")
                    time.sleep(min(1.0, interval * 5))
                    continue
                for n, v in row.items():
                    rec.enqueue(ts, n, v)
                time.sleep(max(0.0, interval - (time.perf_counter() - t_cycle)))
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        rec._poll_thread = threading.Thread(target=loop, name=f"rec-poll-{rec.id}", daemon=True)
        rec._poll_thread.start()

    def stop(self, rid: str, reason: str = "requested") -> dict[str, Any]:
        rec = self._get(rid)
        if rec.active:
            if rec.mode != "poll":
                self._stop_notifications(rec)
            rec.request_stop(reason)
            if rec._thread:
                rec._thread.join(timeout=10)
            if rec._poll_thread:
                rec._poll_thread.join(timeout=5)
        st = rec.status()
        st["stats"] = rec.stats_dict(None, False, 0)
        return st

    def stop_all(self) -> None:
        for rid, rec in list(self.recs.items()):
            if rec.active:
                try:
                    self.stop(rid, "server shutdown")
                except Exception:  # noqa: BLE001
                    pass

    def reap(self) -> None:
        """Release ADS handles of recordings that ended on their own (limits/stop_when)."""
        for rec in self.recs.values():
            if not rec.active and rec.conn is not None:
                self._stop_notifications(rec)

    # ------------------------------------------------------------ queries
    def _get(self, rid: str) -> Recording:
        rec = self.recs.get(rid)
        if rec is None:
            # allow unique prefix
            cands = [r for k, r in self.recs.items() if k.startswith(rid)]
            if len(cands) == 1:
                return cands[0]
            raise RecorderError(f"recording {rid!r} unknown in this session (rec_list shows files on disk)")
        return rec

    def list(self) -> dict[str, Any]:
        self.reap()
        live = [r.status() for r in self.recs.values()]
        for s in live:
            s.pop("last_values", None)
        files = []
        if self.root.is_dir():
            for p in sorted(self.root.glob("*.jsonl")):
                rid = p.stem
                if rid in self.recs:
                    continue
                try:
                    with open(p, encoding="utf-8") as fh:
                        head = json.loads(fh.readline())
                except Exception:  # noqa: BLE001
                    head = {}
                files.append({"id": rid, "file": str(p), "bytes": p.stat().st_size, "name": head.get("name"),
                              "symbols": len(head.get("symbols", [])), "created": head.get("created")})
        return {"session": live, "on_disk": files, "root": str(self.root)}

    def status(self, rid: str) -> dict[str, Any]:
        self.reap()
        return self._get(rid).status()

    def snapshot(self, rid: str, last_n: int, symbols: list[str] | None) -> dict[str, Any]:
        rec = self._get(rid)
        return {"id": rec.id, "active": rec.active, "events": rec.events, "rows": rec.snapshot(last_n, symbols)}

    def stats(self, rid: str, symbols: list[str] | None, include_runs: bool, max_runs_out: int) -> dict[str, Any]:
        rec = self._get(rid)
        return {"id": rec.id, "active": rec.active, "events": rec.events, "elapsed_ms": self._elapsed_ms(rec),
                "triggered_at_ms": rec.triggered_at, "symbols": rec.stats_dict(symbols, include_runs, max_runs_out)}

    @staticmethod
    def _elapsed_ms(rec: Recording) -> float | None:
        if rec.t0_wall is None:
            return None
        end = rec.stopped_at or time.time()
        return round((end - rec.t0_wall) * 1000, 1)

    def read(self, rid: str, t_from_ms: float | None, t_to_ms: float | None, symbols: list[str] | None,
             max_rows: int, wide: bool) -> dict[str, Any]:
        """Stream the file; optionally pivot to wide rows with forward fill."""
        path = self._path_for(rid)
        want = {s.lower() for s in symbols} if symbols else None
        rows: list[Any] = []
        header: dict[str, Any] = {}
        cur: dict[str, Any] = {}
        cols: list[str] = []
        truncated = False
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    if ev.get("_header"):
                        header = ev
                        cols = [s["name"] for s in ev.get("symbols", []) if not want or s["name"].lower() in want]
                    continue
                t, n, v = ev
                if want and n.lower() not in want:
                    continue
                if t_from_ms is not None and t < t_from_ms:
                    if wide:
                        cur[n] = v
                    continue
                if t_to_ms is not None and t > t_to_ms:
                    break
                if wide:
                    cur[n] = v
                    rows.append({"t_ms": t, **{c: cur.get(c) for c in cols}})
                else:
                    rows.append({"t_ms": t, "symbol": n, "value": v})
                if len(rows) >= max_rows:
                    truncated = True
                    break
        return {"id": rid, "file": str(path), "name": header.get("name"), "rows": rows, "count": len(rows), "truncated": truncated}

    def export(self, rid: str, fmt: str, out_path: str | None, symbols: list[str] | None) -> dict[str, Any]:
        path = self._path_for(rid)
        if fmt not in ("csv_long", "csv_wide", "jsonl"):
            raise RecorderError("fmt must be csv_long | csv_wide | jsonl")
        if fmt == "jsonl":
            return {"id": rid, "file": str(path), "format": "jsonl", "note": "already on disk in this format"}
        out = Path(out_path) if out_path else path.with_suffix(f".{'wide' if fmt == 'csv_wide' else 'long'}.csv")
        want = {s.lower() for s in symbols} if symbols else None
        import csv
        n = 0
        with open(path, encoding="utf-8") as fh, open(out, "w", newline="", encoding="utf-8") as fo:
            w = csv.writer(fo)
            cols: list[str] = []
            cur: dict[str, Any] = {}
            footer: dict[str, Any] | None = None
            for ln in fh:
                try:
                    ev = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict):
                    if ev.get("_header"):
                        cols = [s["name"] for s in ev.get("symbols", []) if not want or s["name"].lower() in want]
                        w.writerow(["t_ms", *cols] if fmt == "csv_wide" else ["t_ms", "symbol", "value"])
                    elif ev.get("_footer"):
                        footer = ev
                    continue
                t, nme, v = ev
                if want and nme.lower() not in want:
                    continue
                v = _flat(v)
                if fmt == "csv_wide":
                    cur[nme] = v
                    w.writerow([t, *[cur.get(c, "") for c in cols]])
                else:
                    w.writerow([t, nme, v])
                n += 1
        out_d: dict[str, Any] = {"id": rid, "file": str(out), "format": fmt, "rows": n,
                                 "note": "one row per event; csv_wide repeats the last known value of the other columns"}
        if footer is not None:
            out_d["events_in_file"] = footer.get("events")
            out_d["dropped"] = footer.get("dropped")
            out_d["stop_reason"] = footer.get("stop_reason")
        else:
            out_d["complete"] = False
            out_d["note"] += "; no footer: the recording is still running or the process died before it ended"
        return out_d

    def delete(self, rid: str) -> dict[str, Any]:
        rec = self.recs.get(rid)
        if rec and rec.active:
            self.stop(rid, "deleted")
        path = self._path_for(rid)
        removed = []
        for p in (path, path.with_suffix(".wide.csv"), path.with_suffix(".long.csv")):
            if p.exists():
                p.unlink()
                removed.append(str(p))
        self.recs.pop(rid, None)
        return {"id": rid, "removed": removed}

    # ------------------------------------------------------------ analysis
    def _events_for(self, rid: str, symbols: list[str]) -> list[dict[str, Any]]:
        return self.read(rid, None, None, symbols, 10_000_000, False)["rows"]

    def pulses(self, rid: str, symbol: str, max_list: int = 200) -> dict[str, Any]:
        """Pulse analysis of one symbol: for BOOL the high/low widths, period, duty;
        for any other type the value runs with dwell times (state machine timing)."""
        ev = self._events_for(rid, [symbol])
        if not ev:
            raise RecorderError(f"no events for {symbol!r} in {rid}")
        runs: list[list[Any]] = []
        for e in ev:
            v = _raw_val(e["value"])
            if not runs or runs[-1][2] != v:
                if runs:
                    runs[-1][1] = e["t_ms"]
                runs.append([e["t_ms"], e["t_ms"], v])
            else:
                runs[-1][1] = e["t_ms"]
        span = ev[-1]["t_ms"] - ev[0]["t_ms"]
        is_bool = all(isinstance(r[2], bool) for r in runs)
        out: dict[str, Any] = {"id": rid, "symbol": symbol, "events": len(ev), "span_ms": round(span, 3), "runs": len(runs)}
        if is_bool:
            highs = [(a, b) for a, b, v in runs[:-1] if v]        # last run has no known end
            lows = [(a, b) for a, b, v in runs[:-1] if not v]
            hw = [b - a for a, b in highs]
            lw = [b - a for a, b in lows]
            starts = [a for a, _ in highs]
            periods = [b - a for a, b in zip(starts, starts[1:])]
            out.update({
                "pulses": len(highs),
                "high_ms": _stat(hw), "low_ms": _stat(lw), "period_ms": _stat(periods),
                "frequency_hz": round(1000.0 / (sum(periods) / len(periods)), 3) if periods else None,
                "duty_percent": round(100 * sum(hw) / span, 2) if span > 0 and hw else None,
                "first_pulses": [{"t_on_ms": round(a, 3), "t_off_ms": round(b, 3), "width_ms": round(b - a, 3)} for a, b in highs[:max_list]],
                "note": "first/last runs are open-ended and excluded from width statistics",
            })
        else:
            dw = [b - a for a, b, _ in runs[:-1]]
            out.update({"dwell_ms": _stat(dw),
                        "timeline": [{"t_start_ms": round(a, 3), "t_end_ms": round(b, 3), "dwell_ms": round(b - a, 3), "value": v}
                                     for a, b, v in runs[:max_list]]})
        return out

    def latency(self, rid: str, from_symbol: str, from_op: str, from_value: Any, to_symbol: str, to_op: str,
                to_value: Any, max_pairs: int = 500) -> dict[str, Any]:
        """Time from each moment ``from_symbol <from_op> from_value`` becomes true to the next
        moment ``to_symbol <to_op> to_value`` becomes true (edge-to-edge, PLC timestamps)."""
        ev = self._events_for(rid, [from_symbol, to_symbol])
        f_prev = t_prev = False
        pending: list[float] = []
        pairs: list[dict[str, Any]] = []
        for e in ev:
            v = _raw_val(e["value"])
            if e["symbol"].lower() == from_symbol.lower():
                now = _OPS.get(from_op, lambda a, b: False)(v, _coerce(from_value, v))
                if now and not f_prev:
                    pending.append(e["t_ms"])
                f_prev = now
            if e["symbol"].lower() == to_symbol.lower():
                now = _OPS.get(to_op, lambda a, b: False)(v, _coerce(to_value, v))
                if now and not t_prev and pending:
                    t_from = pending.pop(0)
                    pairs.append({"t_from_ms": round(t_from, 3), "t_to_ms": round(e["t_ms"], 3), "latency_ms": round(e["t_ms"] - t_from, 3)})
                    pending.clear()
                t_prev = now
            if len(pairs) >= max_pairs:
                break
        lat = [p["latency_ms"] for p in pairs]
        return {"id": rid, "from": f"{from_symbol} {from_op} {from_value}", "to": f"{to_symbol} {to_op} {to_value}",
                "pairs": len(pairs), "unmatched_from_edges": len(pending), "latency_ms": _stat(lat), "samples": pairs[:100]}

    def compare(self, rid_a: str, rid_b: str, symbols: list[str] | None = None) -> dict[str, Any]:
        """Before/after comparison of two recordings over their common symbols: stats deltas
        (count, min, max, mean, changes) and, for BOOL symbols, pulse width/period deltas."""
        def stats_from_file(rid: str) -> tuple[dict[str, SymStats], dict[str, str]]:
            st: dict[str, SymStats] = {}
            types: dict[str, str] = {}
            for e in self.read(rid, None, None, None, 50_000_000, False)["rows"]:
                n = e["symbol"]
                v = _raw_val(e["value"])
                st.setdefault(n, SymStats()).ingest(e["t_ms"], v, _numeric_dec(v), 100_000)
            return st, types
        if rid_a in self.recs and not self.recs[rid_a].active:
            sa = self.recs[rid_a].stats
        else:
            sa, _ = stats_from_file(rid_a)
        if rid_b in self.recs and not self.recs[rid_b].active:
            sb = self.recs[rid_b].stats
        else:
            sb, _ = stats_from_file(rid_b)
        common = sorted(set(sa) & set(sb), key=str.lower)
        if symbols:
            want = {s.lower() for s in symbols}
            common = [c for c in common if c.lower() in want]
        out = []
        for n in common:
            a, b = sa[n].to_dict(), sb[n].to_dict()
            row: dict[str, Any] = {"symbol": n, "a": a, "b": b, "delta": {}}
            for k in ("count", "changes", "min", "max", "mean", "rising_edges", "falling_edges"):
                if isinstance(a.get(k), (int, float)) and isinstance(b.get(k), (int, float)):
                    row["delta"][k] = round(b[k] - a[k], 6)
            if isinstance(a.get("last"), bool) and isinstance(b.get("last"), bool):
                try:
                    pa, pb = self.pulses(rid_a, n), self.pulses(rid_b, n)
                    row["pulses"] = {"a": {k: pa.get(k) for k in ("pulses", "high_ms", "period_ms", "duty_percent")},
                                     "b": {k: pb.get(k) for k in ("pulses", "high_ms", "period_ms", "duty_percent")}}
                except RecorderError:
                    pass
            out.append(row)
        return {"a": rid_a, "b": rid_b, "common_symbols": len(common), "only_in_a": sorted(set(sa) - set(sb))[:50],
                "only_in_b": sorted(set(sb) - set(sa))[:50], "rows": out}

    def _path_for(self, rid: str) -> Path:
        rec = self.recs.get(rid)
        if rec:
            return rec.path
        p = self.root / f"{rid}.jsonl"
        if not p.is_file():
            cands = list(self.root.glob(f"{rid}*.jsonl")) if self.root.is_dir() else []
            if len(cands) == 1:
                return cands[0]
            raise RecorderError(f"no recording file for {rid!r} under {self.root}")
        return p


def _raw_val(v: Any) -> Any:
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in v:
                return v[k]
    return v


def _coerce(expected: Any, like: Any) -> Any:
    if isinstance(like, bool) and isinstance(expected, str):
        return expected.strip().lower() in ("1", "true", "on")
    if isinstance(like, (int, float)) and isinstance(expected, str):
        try:
            return type(like)(float(expected)) if isinstance(like, float) else int(float(expected))
        except ValueError:
            return expected
    return expected


def _stat(vals: list[float]) -> dict[str, Any] | None:
    if not vals:
        return None
    s = sorted(vals)
    return {"count": len(s), "min": round(s[0], 3), "max": round(s[-1], 3), "mean": round(sum(s) / len(s), 3),
            "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 3)}


def _flat(v: Any) -> Any:
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in v:
                return v[k]
        return json.dumps(v)
    if isinstance(v, list):
        return json.dumps(v)
    return v
