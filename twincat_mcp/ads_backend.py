"""ADS runtime backend (pyads).

Everything here is synchronous and must be executed on the backend's
``SerialWorker`` (pyads connections are not thread-safe). The tool layer
does that.

Design points that go beyond the existing read-only community servers:

* full symbol upload parsed locally → name, type, size, flags (READONLY,
  PERSISTENT, …), comment; cached with TTL;
* typed decode of TIME/DATE/DT/TOD/LTIME, enums, nested arrays;
* structs read by expanding the member symbols the runtime already
  publishes (``MAIN.stX.nY``) → readable JSON instead of "unsupported";
* raw index-group/offset access to any ADS port;
* sampled watch/trace and wait-until for debugging timing problems;
* PLC and System-Service state control (run/stop/reset, config/run mode);
* local route listing from StaticRoutes.xml and remote route creation.
"""
from __future__ import annotations

import ctypes
import fnmatch
import re
import struct
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Any

import pyads
from pyads import constants as C
from pyads.structs import AmsAddr

from .config import AdsConfig
from . import types_tc as T
from .ads_types import TypeSystem, Leaf, parse_table, _RE_INDIRECT

SYSTEM_SERVICE_PORT = 10000
ROUTER_PORT = 1

SYMFLAG = {
    0x0001: "PERSISTENT", 0x0002: "BITVALUE", 0x0004: "REFERENCETO", 0x0008: "TYPEGUID",
    0x0010: "TCCOMIFACEPTR", 0x0020: "READONLY", 0x0040: "ITFMETHODACCESS",
    0x0080: "METHODDEREF", 0x1000: "ATTRIBUTES", 0x2000: "STATIC", 0x4000: "INITONRESET",
    0x8000: "EXTENDEDFLAGS",
}


def _same_value(a: Any, b: Any) -> bool:
    def raw(v: Any) -> Any:
        if isinstance(v, dict):
            for k in ("raw_ms", "raw", "raw_ns", "value"):
                if k in v:
                    return v[k]
        return v
    a, b = raw(a), raw(b)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same_value(x, y) for x, y in zip(a, b))
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))    # REAL rounding
        except (TypeError, ValueError):
            return False
    return a == b


def _is_iface_name(ts: Any, type_name: str) -> bool:
    from .ads_types import is_interface
    e = ts.get(type_name)
    if e is None:
        want = type_name.strip().lower()
        hits = [k for k in ts.table if k.lower().rsplit(".", 1)[-1] == want]
        e = ts.table[hits[0]] if len(hits) == 1 else None
    return is_interface(e)


class AdsError(RuntimeError):
    pass


@dataclass
class SymbolEntry:
    name: str
    type: str
    size: int
    index_group: int
    index_offset: int
    flags: int
    comment: str

    def flag_names(self) -> list[str]:
        return [n for bit, n in SYMFLAG.items() if self.flags & bit]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["flags"] = self.flag_names()
        d["index_group"] = f"0x{self.index_group:X}"
        d["index_offset"] = f"0x{self.index_offset:X}"
        return d


class AdsBackend:
    def __init__(self, cfg: AdsConfig):
        self.cfg = cfg
        self._conns: dict[tuple[str | None, int], pyads.Connection] = {}
        self._symcache: dict[tuple[str | None, int], tuple[float, list[SymbolEntry], dict[str, SymbolEntry]]] = {}
        self._dtcache: dict[tuple[str | None, int], tuple[float, TypeSystem | None, str]] = {}
        self.net_id: str | None = cfg.ams_net_id
        self.ip: str | None = cfg.ip_address
        self.port: int = cfg.ams_port
        self.cancel = threading.Event()   # set by the worker when a tool call times out / is cancelled

    # ------------------------------------------------------------ connections
    def _key(self, port: int | None, net_id: str | None) -> tuple[str | None, int]:
        return (net_id or self.net_id, port or self.port)

    def conn(self, port: int | None = None, net_id: str | None = None) -> pyads.Connection:
        key = self._key(port, net_id)
        c = self._conns.get(key)
        if c is None or not c.is_open:
            nid, prt = key
            try:
                if nid is None:
                    # local target: pyads needs the local NetId
                    pyads.open_port()
                    nid = pyads.get_local_address().netid  # type: ignore[union-attr]
                    pyads.close_port()
                c = pyads.Connection(nid, prt, self.ip)
                c.open()
                c.set_timeout(self.cfg.timeout_ms)
            except Exception as exc:  # noqa: BLE001
                raise AdsError(f"ADS connect {nid}:{prt} failed: {_fmt_exc(exc)}") from exc
            self._conns[key] = c
        return c

    def connect(self, net_id: str | None, port: int | None, ip: str | None) -> dict[str, Any]:
        self.disconnect()
        if net_id and net_id.lower() in ("local", "localhost", "none"):
            self.net_id = None
            self.ip = None
        elif net_id:
            self.net_id = net_id
        if port:
            self.port = port
        if ip:
            self.ip = ip
        c = self.conn()
        info = self._state_dict(c)
        info.update({"ams_net_id": c.ams_net_id, "ams_port": c.ams_net_port, "ip_address": c.ip_address})
        try:
            name, ver = c.read_device_info()  # type: ignore[misc]
            info["device"] = {"name": name, "version": f"{ver.version}.{ver.revision}.{ver.build}"}
        except Exception as exc:  # noqa: BLE001
            info["device_error"] = _fmt_exc(exc)
        return info

    def disconnect(self) -> None:
        for c in self._conns.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._conns.clear()
        self._symcache.clear()
        self._dtcache.clear()

    def connection_status(self) -> dict[str, Any]:
        return {
            "configured_net_id": self.net_id, "port": self.port, "ip": self.ip,
            "open": [{"net_id": k[0], "port": k[1]} for k, c in list(self._conns.items()) if c.is_open],
        }

    def target_label(self) -> str:
        return f"{self.net_id or 'local'}:{self.port}"

    def symbols_overlapping(self, index_group: int, index_offset: int, size: int, port: int | None = None) -> list[str]:
        """Names of symbols whose memory overlaps [index_offset, index_offset+size) in index_group."""
        try:
            lst, _ = self.symbols_cached(False, port)
        except AdsError:
            return []
        lo, hi = index_offset, index_offset + max(size, 1)
        return [s.name for s in lst if s.index_group == index_group and s.index_offset < hi and (s.index_offset + max(s.size, 1)) > lo]

    # ------------------------------------------------------------ state
    def _state_dict(self, c: pyads.Connection) -> dict[str, Any]:
        try:
            st = c.read_state()
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"read_state failed: {_fmt_exc(exc)}") from exc
        ads_state, dev_state = st if st else (-1, -1)
        return {"ads_state": ads_state, "ads_state_name": T.ads_state_name(ads_state),
                "device_state": dev_state}

    def state(self, port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        d = self._state_dict(c)
        d["port"] = c.ams_net_port
        return d

    def system_state(self) -> dict[str, Any]:
        """TwinCAT System Service (port 10000): RUN vs CONFIG mode."""
        d = self.state(SYSTEM_SERVICE_PORT)
        d["twincat_mode"] = {5: "RUN", 15: "CONFIG", 16: "RECONFIG"}.get(d["ads_state"], d["ads_state_name"])
        return d

    def device_info(self, port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        try:
            name, ver = c.read_device_info()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"read_device_info failed: {_fmt_exc(exc)}") from exc
        return {"name": name, "version": f"{ver.version}.{ver.revision}.{ver.build}", "port": c.ams_net_port}

    def set_plc_state(self, action: str) -> dict[str, Any]:
        """run | stop | reset (cold reset) on the PLC runtime port."""
        c = self.conn()
        code = {"run": C.ADSSTATE_RUN, "stop": C.ADSSTATE_STOP, "reset": C.ADSSTATE_RESET}.get(action)
        if code is None:
            raise AdsError("action must be run|stop|reset")
        before = self._state_dict(c)
        try:
            c.write_control(code, before["device_state"] if before["device_state"] >= 0 else 0, 0, C.PLCTYPE_INT)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"write_control({action}) failed: {_fmt_exc(exc)}") from exc
        time.sleep(0.3)
        return {"before": before, "after": self._state_dict(c)}

    def set_system_mode(self, action: str, timeout_s: float = 20.0) -> dict[str, Any]:
        """config | run on the System Service. 'run' restarts TwinCAT in run mode
        (same as ADSSTATE_RESET on port 10000); 'config' = ADSSTATE_RECONFIG."""
        c = self.conn(SYSTEM_SERVICE_PORT)
        code = {"run": C.ADSSTATE_RESET, "config": C.ADSSTATE_RECONFIG}.get(action)
        if code is None:
            raise AdsError("action must be run|config")
        before = self._state_dict(c)
        try:
            c.write_control(code, 0, 0, C.PLCTYPE_INT)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"system mode switch ({action}) failed: {_fmt_exc(exc)}") from exc
        # the system service drops connections while switching: reconnect and poll until the requested mode is
        # reached (or the timeout) — the answer is the state TwinCAT is actually in, not the request
        self.disconnect()
        want = "RUN" if action == "run" else "CONFIG"
        t0 = time.time()
        after: dict[str, Any] | str = "unreadable"
        polls = 0
        while time.time() - t0 < timeout_s:
            time.sleep(0.5 if polls < 6 else 1.0)
            polls += 1
            try:
                after = self.system_state()
            except Exception as exc:  # noqa: BLE001 — restarting: the port is briefly gone
                after = f"unreadable: {exc}"
                self.disconnect()
                continue
            if isinstance(after, dict) and after.get("twincat_mode") == want:
                return {"before": before, "requested": action, "after": after, "reached": True,
                        "seconds": round(time.time() - t0, 1)}
        return {"before": before, "requested": action, "after": after, "reached": False, "seconds": round(time.time() - t0, 1),
                "note": f"TwinCAT did not report {want} within {timeout_s:.0f} s — a license error or a configuration "
                        "problem makes it fall back to CONFIG; check xae_dialogs / ads_status"}

    # ------------------------------------------------------------ symbols
    def _load_symbols(self, c: pyads.Connection) -> tuple[list[SymbolEntry], dict[str, SymbolEntry]]:
        try:
            info = c.read(C.ADSIGRP_SYM_UPLOADINFO2, 0, ctypes.c_ubyte * 64, return_ctypes=True, check_length=False)
            n_syms, n_bytes = struct.unpack_from("<II", bytes(info), 0)
            buf = c.read(C.ADSIGRP_SYM_UPLOAD, 0, ctypes.c_ubyte * n_bytes, return_ctypes=True, check_length=False)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"symbol upload failed: {_fmt_exc(exc)}") from exc
        raw = bytes(buf)
        out: list[SymbolEntry] = []
        ptr = 0
        for _ in range(n_syms):
            if ptr + 30 > len(raw):
                break
            entry_len, igrp, ioffs, size, dtype, flags = struct.unpack_from("<IIIIII", raw, ptr)
            nlen, tlen, clen = struct.unpack_from("<HHH", raw, ptr + 24)
            p = ptr + 30
            name = raw[p:p + nlen].decode("latin-1", "replace"); p += nlen + 1
            typ = raw[p:p + tlen].decode("latin-1", "replace"); p += tlen + 1
            com = raw[p:p + clen].decode("latin-1", "replace")
            out.append(SymbolEntry(name, typ, size, igrp, ioffs, flags, com))
            ptr += entry_len
        return out, {s.name.lower(): s for s in out}

    def _symbol_version(self, c: pyads.Connection) -> int | None:
        """ADSIGRP_SYM_VERSION (0xF008): one byte the runtime increments on every download / online change.
        Checked at most every 2 s per target; None where the runtime does not offer it."""
        key = (c.ams_net_id, c.ams_net_port)
        vc = self.__dict__.setdefault("_symver", {})
        hit = vc.get(key)
        if hit and time.time() - hit[0] < 2.0:
            return hit[1]
        try:
            v = int(c.read(0xF008, 0, ctypes.c_ubyte))
        except Exception:  # noqa: BLE001
            v = None
        vc[key] = (time.time(), v)
        return v

    def symbols_cached(self, refresh: bool = False, port: int | None = None) -> tuple[list[SymbolEntry], dict[str, SymbolEntry]]:
        """Symbol table cache. Invalidated when the runtime's symbol version changes (download / online change /
        activation) — not on a fixed timer; the TTL only applies where the version is not readable."""
        c = self.conn(port)
        key = (c.ams_net_id, c.ams_net_port)
        hit = self._symcache.get(key)
        ver = self._symbol_version(c)
        if hit and not refresh:
            cached_ver = hit[3] if len(hit) > 3 else None
            if ver is not None and cached_ver is not None:
                if ver == cached_ver:
                    return hit[1], hit[2]
                self._dtcache.pop(key, None)        # the datatype table changed with it
            elif time.time() - hit[0] < self.cfg.symbol_cache_ttl_s:
                return hit[1], hit[2]
        lst, idx = self._load_symbols(c)
        self._symcache[key] = (time.time(), lst, idx, ver)
        return lst, idx

    def list_symbols(self, pattern: str | None, limit: int, offset: int, refresh: bool,
                     type_filter: str | None = None, port: int | None = None) -> dict[str, Any]:
        lst, _ = self.symbols_cached(refresh, port)
        pat = (pattern or "*").lower()
        if "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        sel = [s for s in lst if fnmatch.fnmatchcase(s.name.lower(), pat)]
        if type_filter:
            tf = type_filter.lower()
            sel = [s for s in sel if tf in s.type.lower()]
        total = len(sel)
        page = sel[offset: offset + limit]
        return {"total": total, "offset": offset, "returned": len(page),
                "symbols": [s.to_dict() for s in page]}

    def symbol_info(self, name: str, port: int | None = None) -> SymbolEntry:
        _, idx = self.symbols_cached(False, port)
        s = idx.get(name.lower())
        if s is None:
            # maybe cache stale → refresh once, else ask runtime directly
            _, idx = self.symbols_cached(True, port)
            s = idx.get(name.lower())
        if s is None:
            c = self.conn(port)
            try:
                e = pyads.pyads_ex.adsGetSymbolInfo(c._port, c._adr, name)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                raise AdsError(f"symbol {name!r} not found: {_fmt_exc(exc)}") from exc
            s = SymbolEntry(e.name, e.symbol_type, e.size, e.iGroup, e.iOffs, e.flags, e.comment)
        return s

    def members(self, name: str, port: int | None = None) -> list[SymbolEntry]:
        """Direct member symbols of a struct/FB instance (one level)."""
        lst, _ = self.symbols_cached(False, port)
        prefix = name.lower() + "."
        out = []
        for s in lst:
            low = s.name.lower()
            if low.startswith(prefix) and "." not in low[len(prefix):] and "[" not in low[len(prefix):]:
                out.append(s)
        return out

    # ------------------------------------------------------------ datatype table
    def _load_datatypes(self, c: pyads.Connection) -> TypeSystem:
        info = c.read(C.ADSIGRP_SYM_UPLOADINFO2, 0, ctypes.c_ubyte * 64, return_ctypes=True, check_length=False)
        n_syms, n_bytes, n_dt, dt_bytes = struct.unpack_from("<IIII", bytes(info), 0)
        if dt_bytes == 0:
            raise AdsError("runtime reports an empty datatype table")
        buf = c.read(C.ADSIGRP_SYM_DT_UPLOAD, 0, ctypes.c_ubyte * dt_bytes, return_ctypes=True, check_length=False)
        return TypeSystem(parse_table(bytes(buf), n_dt or None))

    def typesystem(self, refresh: bool = False, port: int | None = None) -> TypeSystem | None:
        """Parsed datatype table for the connected runtime, or None if the upload fails
        (then struct reads fall back to published member symbols)."""
        c = self.conn(port)
        key = (c.ams_net_id, c.ams_net_port)
        hit = self._dtcache.get(key)
        ver = self._symbol_version(c)
        dtver = self.__dict__.setdefault("_dtver", {})
        if hit and not refresh:
            if ver is not None and dtver.get(key) == ver:
                return hit[1]
            if ver is None and time.time() - hit[0] < self.cfg.symbol_cache_ttl_s:
                return hit[1]
        dtver[key] = ver
        try:
            ts = self._load_datatypes(c)
            err = ""
        except Exception as exc:  # noqa: BLE001
            ts, err = None, _fmt_exc(exc)
        self._dtcache[key] = (time.time(), ts, err)
        return ts

    def type_info(self, type_name: str, port: int | None = None) -> dict[str, Any]:
        ts = self.typesystem(False, port)
        if ts is None:
            raise AdsError(f"datatype table unavailable: {self._dtcache.get(self._key(port, None), (0, None, '?'))[2]}")
        e = ts.get(type_name)
        resolved_as = None
        if e is None:
            # unqualified or differently-cased name: unique match on the part after the last '.' wins (CTU → Tc2_Standard.CTU)
            want = type_name.strip().lower()
            exact = [k for k in ts.table if k.lower() == want or k.lower().rsplit(".", 1)[-1] == want]
            if len(exact) == 1:
                e = ts.table[exact[0]]
                resolved_as = e.name
            elif len(exact) > 1:
                raise AdsError(f"type {type_name!r} is ambiguous in the datatype table: {sorted(ts.table[k].name for k in exact)}; use the qualified name")
        if e is None:
            cands = [k for k in ts.table if type_name.lower() in k][:20]
            raise AdsError(f"type {type_name!r} not in datatype table; similar: {cands}")
        d = e.summary()
        if resolved_as:
            d["resolved_as"] = resolved_as
        return d

    def list_types(self, pattern: str | None, limit: int, port: int | None = None) -> dict[str, Any]:
        ts = self.typesystem(False, port)
        if ts is None:
            raise AdsError("datatype table unavailable")
        pat = (pattern or "*").lower()
        if "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        sel = [e for k, e in ts.table.items() if fnmatch.fnmatchcase(k, pat)]
        sel.sort(key=lambda e: e.name.lower())
        return {"total": len(sel), "types": [{"name": e.name, "size": e.size, "members": len(e.subitems),
                                             "array": bool(e.dims), "enum": bool(e.enum), "base": e.type} for e in sel[:limit]]}

    def leaves(self, name: str, max_leaves: int = 200_000, port: int | None = None) -> tuple[SymbolEntry, list[Leaf]]:
        """Every scalar leaf of a symbol with byte offsets (datatype table)."""
        s = self.symbol_info(name, port)
        ts = self.typesystem(False, port)
        if ts is None or ts.get(s.type) is None:
            tt = T.parse_type(s.type, s.size)
            if tt.ctype is not None and not tt.opaque:
                return s, [Leaf(s.name, s.type, 0, s.size, 0)]
            # fallback: published member symbols
            mem = self.members(s.name, port)
            return s, [Leaf(m.name, m.type, m.index_offset - s.index_offset, m.size, 0, None, "from symbol table") for m in mem]
        return s, ts.leaves(s.name, s.type, max_leaves, size=s.size)

    def roots(self, pattern: str | None = None, include_system: bool = False, port: int | None = None) -> list[SymbolEntry]:
        """Top-level symbols: those not nested under another symbol in the table."""
        lst, _ = self.symbols_cached(False, port)
        names = sorted(lst, key=lambda s: s.name.lower())
        out: list[SymbolEntry] = []
        last: str | None = None
        for s in names:
            low = s.name.lower()
            if last and (low.startswith(last + ".") or low.startswith(last + "[")):
                continue
            out.append(s)
            last = low
        if not include_system:
            out = [s for s in out if not _is_system_symbol(s.name)]
        if pattern:
            pat = pattern.lower()
            if "*" not in pat and "?" not in pat:
                pat = f"{pat}*"
            out = [s for s in out if fnmatch.fnmatchcase(s.name.lower(), pat)]
        return out

    def leaves_under(self, prefix: str, max_leaves: int = 200_000, port: int | None = None) -> list[tuple[SymbolEntry, list[Leaf]]]:
        """Leaves of every root symbol equal to / below ``prefix`` ('MAIN', 'MAIN.fbFill', 'GVL_X')."""
        pre = prefix.rstrip(".").lower()
        _, idx = self.symbols_cached(False, port)
        if pre in idx:
            s, lv = self.leaves(idx[pre].name, max_leaves, port)
            return [(s, lv)]
        out = []
        budget = max_leaves
        for r in self.roots(None, True, port):
            low = r.name.lower()
            if low == pre or low.startswith(pre + ".") or low.startswith(pre + "["):
                s, lv = self.leaves(r.name, budget, port)
                out.append((s, lv))
                budget -= len(lv)
                if budget <= 0:
                    break
            elif pre.startswith(low + ".") or pre.startswith(low + "["):
                # D31: the prefix is inside a root that is not in the symbol table on its own
                # (one element of an array of structs: GVL_Sim.aCh[1]) → that root's leaves below the prefix
                s, lv = self.leaves(r.name, 10 * max_leaves, port)
                sub = [x for x in lv if x.path.lower() == pre or x.path.lower().startswith((pre + ".", pre + "["))]
                if sub:
                    out.append((s, sub[:budget]))
                    budget -= len(sub)
                break
        return out

    def leaf_values(self, root: SymbolEntry, leaves: list[Leaf], port: int | None = None) -> dict[str, Any]:
        """Decode every leaf from one raw read of the root symbol → {path: value}."""
        c = self.conn(port)
        ts = self.typesystem(False, port)
        try:
            raw = bytes(c.read(root.index_group, root.index_offset, ctypes.c_ubyte * max(root.size, 1), return_ctypes=True, check_length=False))
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"read {root.name!r}: {_fmt_exc(exc)}") from exc
        out: dict[str, Any] = {}
        for lf in leaves:
            if lf.size <= 0 or lf.offset + lf.size > len(raw):
                continue
            if ts is not None:
                out[lf.path] = ts.decode_leaf(raw, lf)
            else:
                out[lf.path] = TypeSystem._decode_scalar(raw, lf.offset, lf.type, None, None)
        return out

    def read_full(self, name: str, max_bytes: int | None = None, port: int | None = None) -> dict[str, Any]:
        """One ADS read of the whole variable, decoded into a nested tree via the datatype table."""
        c = self.conn(port)
        s = self.symbol_info(name, port)
        limit = max_bytes or self.cfg.max_file_bytes
        if s.size > limit:
            raise AdsError(f"{s.name} is {s.size} bytes; raise max_bytes or read members individually")
        try:
            raw = bytes(c.read(s.index_group, s.index_offset, ctypes.c_ubyte * max(s.size, 1), return_ctypes=True, check_length=False))
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"read {s.name!r} ({s.size} B): {_fmt_exc(exc)}") from exc
        ts = self.typesystem(False, port)
        out: dict[str, Any] = {"name": s.name, "type": s.type, "size": s.size, "flags": s.flag_names()}
        if ts is not None and ts.get(s.type) is not None:
            out["value"] = ts.decode_tree(raw, s.type, size=s.size)
            out["decoder"] = "datatype-table"
        else:
            tt = T.parse_type(s.type, s.size)
            if tt.ctype is not None and not tt.opaque:
                out["value"] = TypeSystem._decode_scalar(raw, 0, s.type, None, None)
                out["decoder"] = "scalar"
            else:
                out["value"] = None
                out["raw_hex"] = raw.hex()
                out["decoder"] = "none"
        if s.comment:
            out["comment"] = s.comment
        return out

    def dump(self, pattern: str | None, include_system: bool, out_dir: str, max_total_bytes: int = 64 * 1024 * 1024,
             max_leaves: int = 500_000, port: int | None = None, cancel: Any = None,
             own_roots: set[str] | None = None) -> dict[str, Any]:
        """Read every variable (every root symbol, fully expanded) and write JSON + CSV. Written as *.partial and
        renamed when complete; a failed or cancelled dump leaves no file behind and says so (R8). R42: pointers are
        addresses (never the pointee), one leaf budget for CSV and JSON, max_total_bytes counts what is WRITTEN, the
        JSON is streamed root by root, the project's own roots come first, and a dump whose call already ended (the
        client's timeout set `cancel`) is never renamed into place."""
        import os
        from pathlib import Path
        stamp = time.strftime("%Y%m%d-%H%M%S")
        jpath = Path(out_dir) / f"dump-{stamp}.json"
        cpath = Path(out_dir) / f"dump-{stamp}.csv"
        jpart, cpart = jpath.with_name(jpath.name + ".partial"), cpath.with_name(cpath.name + ".partial")
        os.makedirs(out_dir, exist_ok=True)
        self.dump_progress = {"phase": "listing the symbols", "started": time.time()}

        def drop() -> list[str]:
            removed = []
            for p in (cpart, jpart):
                try:
                    if p.exists():
                        size = p.stat().st_size
                        p.unlink()
                        removed.append(f"{p.name} ({size} bytes)")
                except OSError:
                    pass
            return removed
        try:
            res = self._dump(pattern, include_system, cpart, jpart, max_total_bytes, max_leaves, port, cancel, own_roots)
            if cancel is not None and cancel.is_set():
                raise AdsError("cancelled: the call had already ended (timeout) — the files are not kept")
        except BaseException as exc:
            removed = drop()
            self.dump_progress["phase"] = "failed"
            msg = f"ads_dump failed: {_fmt_exc(exc) if not isinstance(exc, AdsError) else exc}"
            raise AdsError(msg + (f" — partial files removed: {removed}" if removed else "")) from exc
        os.replace(cpart, cpath)
        os.replace(jpart, jpath)
        self.dump_progress["phase"] = "done"
        res.update({"json": str(jpath), "csv": str(cpath)})
        return res

    def _dump(self, pattern: str | None, include_system: bool, cpath: Any, jpath: Any, max_total_bytes: int,
              max_leaves: int, port: int | None, cancel: Any, own_roots: set[str] | None = None) -> dict[str, Any]:
        import csv, json
        c = self.conn(port)
        ts = self.typesystem(False, port)
        roots = _dump_order(self.roots(pattern, include_system, port), own_roots)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n_leaves = 0
        n_bytes = 0
        errors: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        sizes: list[dict[str, Any]] = []
        preview: list[dict[str, Any]] = []
        truncated: str | None = None
        t0 = time.time()
        prog = self.dump_progress

        def cancelled() -> bool:
            return cancel is not None and cancel.is_set()

        with open(cpath, "w", newline="", encoding="utf-8") as fh, open(jpath, "w", encoding="utf-8") as jh:
            cw = _Counting(fh)
            jw = _Counting(jh)
            w = csv.writer(cw)
            w.writerow(["path", "type", "value"])
            jw.write('{"target": ' + json.dumps(self.target_label()) + ', "taken": ' + json.dumps(stamp) + ', "values": {')
            first = True
            for i, r in enumerate(roots):
                if cancelled():
                    raise AdsError(f"cancelled at root {i + 1} of {len(roots)} ({r.name}) — the call timed out or was stopped")
                if truncated:
                    skipped.append({"root": r.name, "reason": truncated})
                    continue
                if cw.n + jw.n >= max_total_bytes or n_leaves >= max_leaves:
                    truncated = ("size limit: max_total_mb written" if cw.n + jw.n >= max_total_bytes
                                 else f"leaf limit: max_leaves {max_leaves}")
                    skipped.append({"root": r.name, "reason": truncated})
                    continue
                prog.update(phase="reading", root=r.name, index=i + 1, of=len(roots), leaves=n_leaves,
                            written_mb=round((cw.n + jw.n) / 1e6, 1))
                try:
                    raw = bytes(c.read(r.index_group, r.index_offset, ctypes.c_ubyte * max(r.size, 1), return_ctypes=True, check_length=False))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"symbol": r.name, "error": _fmt_exc(exc)})
                    continue
                n_bytes += len(raw)
                c0, j0 = cw.n, jw.n
                prog["phase"] = "decoding"
                if ts is not None and ts.get(r.type) is not None:
                    left = max_leaves - n_leaves
                    lv = ts.leaves(r.name, r.type, left + 1, size=r.size)
                    cut = len(lv) > left
                    lv = lv[:left]
                    prog["phase"] = "writing CSV"
                    vals: dict[str, Any] = {}
                    for k, leaf in enumerate(lv):
                        val = ts.decode_leaf(raw, leaf) if leaf.size else None
                        w.writerow([leaf.path, leaf.type, _csv_val(val)])
                        if cut:
                            vals[leaf.path] = val
                        if len(preview) < 50:
                            preview.append({"path": leaf.path, "type": leaf.type, "value": val})
                        if k % 256 == 255:
                            if k % 2048 == 2047 and cancelled():
                                raise AdsError(f"cancelled while writing {r.name} — the call timed out or was stopped")
                            # this root's JSON is not written yet: count it as large as its CSV rows (review: the
                            # in-flight root could reach about twice max_total_mb)
                            if cw.n + jw.n + (cw.n - c0) >= max_total_bytes:
                                cut = True
                                lv = lv[:k + 1]
                                vals = {x.path: vals.get(x.path) for x in lv} if vals else {
                                    x.path: (ts.decode_leaf(raw, x) if x.size else None) for x in lv}
                                break
                    n_leaves += len(lv)
                    prog["phase"] = "writing JSON"
                    if cut:
                        # this root did not fit: its JSON is the leaves that were written, flat, and marked
                        node: Any = {"_truncated": True, "_leaves": vals}
                        truncated = truncated or (f"leaf limit: max_leaves {max_leaves}" if n_leaves >= max_leaves
                                                  else "size limit: max_total_mb written")
                    else:
                        node = ts.decode_tree(raw, r.type, size=r.size)
                else:
                    node = (T.fmt_pointer(int.from_bytes(raw, "little"), len(raw))
                            if _RE_INDIRECT.match(r.type or "") and len(raw) in (4, 8)
                            else TypeSystem._decode_scalar(raw, 0, r.type, None, None))
                    w.writerow([r.name, r.type, _csv_val(node)])
                    n_leaves += 1
                    lv = [None]
                    if len(preview) < 50:
                        preview.append({"path": r.name, "type": r.type, "value": node})
                jw.write(("" if first else ",") + "\n" + json.dumps(r.name) + ": " + _json_strict(node))
                first = False
                sizes.append({"root": r.name, "leaves": len(lv), "csv_bytes": cw.n - c0, "json_bytes": jw.n - j0})
            jw.write("\n}, " + json.dumps({"roots": len(roots), "leaves": n_leaves, "truncated": bool(truncated)})[1:] + "\n")
        out = {"roots": len(roots), "leaves": n_leaves, "bytes_read": n_bytes, "csv_bytes": cw.n, "json_bytes": jw.n,
               "duration_s": round(time.time() - t0, 2), "datatype_table": ts is not None, "truncated": bool(truncated),
               "errors": errors[:50], "errors_total": len(errors),
               "largest_roots": sorted(sizes, key=lambda x: -(x["csv_bytes"] + x["json_bytes"]))[:10],
               "preview": preview}
        if truncated:
            out["truncated_reason"] = truncated
            out["skipped_roots"] = skipped[:50]
            out["skipped_total"] = len(skipped)
            out["hint"] = ("the dump was cut — raise max_leaves / max_total_mb, or dump the rest with pattern= "
                           "(skipped_roots lists what is missing)")
        return out

    # ------------------------------------------------------------ read / write
    def _typed_read(self, c: pyads.Connection, s: SymbolEntry) -> tuple[T.TcType, Any]:
        tt = T.parse_type(s.type, s.size)
        if tt.ctype is None or tt.opaque:
            raw = bytes(c.read(s.index_group, s.index_offset, ctypes.c_ubyte * max(s.size, 1), return_ctypes=True, check_length=False))
            return tt, raw
        try:
            val = c.read_by_name(s.name, tt.ctype)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"read {s.name!r} as {s.type}: {_fmt_exc(exc)}") from exc
        return tt, val

    def read(self, name: str, expand_struct: bool = True, max_members: int = 200,
             port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        s = self.symbol_info(name, port)
        tt0 = T.parse_type(s.type, s.size)
        if expand_struct and (tt0.ctype is None or tt0.opaque):
            ts = self.typesystem(False, port)
            if ts is not None and ts.get(s.type) is not None:
                return self.read_full(s.name, None, port)
        tt, val = self._typed_read(c, s)
        out: dict[str, Any] = {"name": s.name, "type": s.type, "size": s.size, "flags": s.flag_names()}
        if tt.kind == "enum" and not isinstance(val, (bytes, bytearray)) and self.is_interface_type(s.type, port):
            out["value"] = T.fmt_pointer(val, s.size)
            out["note"] = "INTERFACE variable: the value is the address of the assigned FB instance (null = unassigned)"
            return out
        if tt.kind == "array" and tt.elem is not None and tt.elem.kind == "enum" and isinstance(val, (list, tuple)) \
                and self.is_interface_type(tt.elem.name, port):
            out["value"] = [T.fmt_pointer(v, tt.elem.size) for v in val]
            out["note"] = "ARRAY OF INTERFACE: each value is the address of the assigned FB instance (null = unassigned)"
            return out
        if isinstance(val, (bytes, bytearray)):
            out["raw_hex"] = val.hex()
            if tt.kind == "array" and tt.elem and tt.elem.kind in ("string", "wstring"):
                out["value"] = _split_string_array(val, tt)
            elif expand_struct:
                mem = self.members(s.name, port)[:max_members]
                if mem:
                    out["value"] = {m.name[len(s.name) + 1:]: self.read(m.name, True, max_members, port).get("value", None)
                                    for m in mem}
                    out["members"] = len(mem)
                else:
                    out["value"] = None
                    out["note"] = "opaque type with no published members; see raw_hex"
            else:
                out["value"] = None
        else:
            out["value"] = T.decode(tt, val)
        if s.comment:
            out["comment"] = s.comment
        return out

    def enum_named(self, type_name: str, value: Any, port: int | None = None) -> Any:
        """D4: a top-level enum read as a plain number gets the same shape as an enum inside a struct:
        {"value": 20, "name": "Idle"}. Anything else is returned unchanged."""
        if not isinstance(value, int) or isinstance(value, bool):
            return value
        try:
            ts = self.typesystem(False, port)
        except Exception:  # noqa: BLE001
            return value
        e = ts.get(type_name) if ts is not None else None
        if e is None or not e.enum:
            return value
        return {"value": value, "name": e.enum.get(value, f"?{value}")}

    def read_many(self, names: list[str], port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        simple: dict[str, T.TcType] = {}
        results: dict[str, Any] = {}
        for n in names:
            try:
                s = self.symbol_info(n, port)
            except AdsError as exc:
                results[n] = {"error": str(exc)}
                continue
            tt = T.parse_type(s.type, s.size)
            if tt.ctype is not None and not tt.opaque and tt.kind not in ("string", "wstring"):
                simple[s.name] = tt
            else:
                try:
                    results[n] = self.read(n, True, 50, port)["value"]
                except AdsError as exc:
                    results[n] = {"error": str(exc)}
        if simple:
            try:
                got = c.read_list_by_name(list(simple.keys()))
                for n, v in got.items():
                    results[n] = T.decode(simple[n], v) if not isinstance(v, str) or not v.startswith("error") else {"error": v}
            except Exception:  # noqa: BLE001  (sum-read unsupported → fallback)
                for n, tt in simple.items():
                    try:
                        results[n] = T.decode(tt, c.read_by_name(n, tt.ctype))
                    except Exception as exc:  # noqa: BLE001
                        results[n] = {"error": _fmt_exc(exc)}
        return results

    # set by the server: True when the project source declares this name as an INTERFACE — the runtime's datatype
    # table does not always mark interface types recognisably (field retest #52/#64)
    interface_hint: Callable[[str], bool] | None = None

    def is_interface_type(self, type_name: str, port: int | None = None) -> bool:
        try:
            ts = self.typesystem(False, port)
        except Exception:  # noqa: BLE001
            ts = None
        if ts is not None and _is_iface_name(ts, type_name):
            return True
        if self.interface_hint is not None:
            try:
                return bool(self.interface_hint(type_name))
            except Exception:  # noqa: BLE001
                return False
        return False

    def unwritable_reason(self, type_name: str, size: int | None, port: int | None = None) -> str | None:
        """Why a variable of this type must not be written through ADS, or None. POINTER/REFERENCE TO are
        addresses; an INTERFACE variable is a pointer to an FB instance's method table — any value the agent
        writes is a wild pointer the PLC will call. The datatype table marks interfaces as pointer-sized BIGTYPEs
        with method infos and no members (and TcCOM interface pointers with TCCOMIFACEPTR)."""
        tt = T.parse_type(type_name, size)
        if tt.kind == "pointer":
            return f"{type_name} is a POINTER/REFERENCE — writing an address is refused; write the variable it points to"
        base = tt.elem.name if tt.kind == "array" and tt.elem else type_name
        if self.interface_hint is not None and self.interface_hint(base):
            return (f"{base} is an INTERFACE (declared in the project source) — writing it would make the PLC call a wild "
                    "pointer; assign interfaces in PLC code, not over ADS")
        try:
            ts = self.typesystem(False, port)
        except Exception:  # noqa: BLE001
            ts = None
        if ts is not None:
            e = ts.get(base)
            if e is None:
                want = base.strip().lower()
                hits = [k for k in ts.table if k.lower() == want or k.lower().rsplit(".", 1)[-1] == want]
                e = ts.table[hits[0]] if len(hits) == 1 else None
            if e is not None:
                from .ads_types import F_TCCOMIFACEPTR, F_REFERENCETO, is_interface
                if e.flags & F_TCCOMIFACEPTR:
                    return f"{base} is a TcCOM interface pointer — not writable through ADS"
                if e.flags & F_REFERENCETO:
                    return f"{base} is a REFERENCE — writing the reference itself is refused; write the referenced variable"
                if is_interface(e):
                    return (f"{base} is an INTERFACE type (pointer to an FB's method table) — writing it would make the PLC call a "
                            "wild pointer; assign interfaces in PLC code, not over ADS")
        return None

    def enum_literal(self, type_name: str, value: Any, port: int | None = None) -> Any:
        """'Idle' / 'E_State.Idle' / 'E_State#Idle' → the enum's integer via the datatype table; anything else unchanged."""
        if not isinstance(value, str):
            return value
        v = value.strip()
        if re.fullmatch(r"[-+]?(\d+|0x[0-9a-fA-F]+|16#[0-9a-fA-F_]+|2#[01_]+)", v):
            return value
        try:
            ts = self.typesystem(False, port)
        except Exception:  # noqa: BLE001
            ts = None
        e = None
        if ts is not None:
            e = ts.get(type_name)
            if e is None:
                want = type_name.strip().lower()
                hits = [k for k in ts.table if k.lower().rsplit(".", 1)[-1] == want]
                e = ts.table[hits[0]] if len(hits) == 1 else None
        if e is None or not e.enum:
            raise AdsError(f"{value!r} is not a number and the datatype table has no enum values for {type_name} "
                           "(write the integer instead)")
        lit = re.split(r"[.#]", v)[-1].lower()
        for num, nm in e.enum.items():
            if nm.lower() == lit:
                return num
        raise AdsError(f"{value!r} is not a value of {type_name}; valid: {', '.join(e.enum.values())}")

    # set by the server: source-level checks (CONSTANT declarations, no project in guarded mode) that every
    # write path must pass — ads_write, ads_write_many, snap_apply, test_*, exp_* all end in check_write
    write_guard: Callable[[str], str | None] | None = None

    def check_write(self, name: str, value: Any, port: int | None = None) -> dict[str, Any]:
        """Everything write() would refuse, without writing: READONLY flag, pointer/reference/interface types,
        source-level CONSTANT, enum names, array length, string length, integer range. Returns the encoded value."""
        if self.write_guard is not None:
            why = self.write_guard(name)
            if why:
                raise AdsError(why)
        s = self.symbol_info(name, port)
        if s.flags & 0x0020:
            raise AdsError(f"{s.name!r} is flagged READONLY by the runtime")
        why = self.unwritable_reason(s.type, s.size, port)
        if why:
            raise AdsError(f"{s.name!r}: {why}")
        tt = T.parse_type(s.type, s.size)
        if tt.ctype is None or tt.opaque:
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]*", value) and len(value) == s.size * 2:
                return {"symbol": s, "tt": tt, "encoded": None, "hex": value}
            raise AdsError(
                f"{s.name!r} has opaque type {s.type}; write its members individually "
                f"(see ads_read with expand_struct) or pass a {s.size*2}-char hex string.")
        if tt.kind == "enum":
            value = self.enum_literal(s.type, value, port)
        try:
            enc = T.encode(tt, value)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"write {s.name!r}={value!r} as {s.type}: {_fmt_exc(exc)}") from exc
        return {"symbol": s, "tt": tt, "encoded": enc, "hex": None}

    def write(self, name: str, value: Any, port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        chk = self.check_write(name, value, port)
        s, tt = chk["symbol"], chk["tt"]
        before = self.read(name, expand_struct=False, port=port).get("value")
        if chk["hex"] is not None:
            data = bytes.fromhex(chk["hex"])
            c.write(s.index_group, s.index_offset, (ctypes.c_ubyte * len(data))(*data), ctypes.c_ubyte * len(data))
            return {"name": s.name, "type": s.type, "before": before, "written_hex": chk["hex"]}
        try:
            enc = chk["encoded"]
            c.write_by_name(s.name, enc, tt.ctype)
        except AdsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"write {s.name!r}={value!r} as {s.type}: {_fmt_exc(exc)}") from exc
        read_back = self.read(name, expand_struct=False, port=port).get("value")
        written = T.decode(tt, enc)
        out = {"name": s.name, "type": s.type, "before": before, "written": written, "read_back": read_back}
        if not _same_value(written, read_back):
            out["note"] = ("read_back differs from the written value: the PLC changed it after the write (a one-shot it "
                           "clears, or a variable the program assigns every cycle). The write itself was accepted.")
        return out

    @staticmethod
    def _value_bytes(s: "SymbolEntry", tt: T.TcType, chk: dict[str, Any]) -> bytes:
        """The exact bytes a write puts into the variable (what a sum-write sends)."""
        if chk.get("hex") is not None:
            return bytes.fromhex(chk["hex"])
        enc = chk["encoded"]
        if tt.kind == "string":
            return enc.encode("latin-1", "replace")[: s.size - 1].ljust(s.size, b"\x00")
        if tt.kind == "wstring":
            return enc.encode("utf-16-le")[: s.size - 2].ljust(s.size, b"\x00")
        if tt.kind == "array":
            return bytes(tt.ctype(*enc))
        return bytes(tt.ctype(enc))

    def write_many(self, items: dict[str, Any], port: int | None = None) -> dict[str, Any]:
        """Pre-check every item (all refusals raised before anything is written), then ONE ADS sum-write
        (ADSIGRP_SUMUP_WRITE): the runtime applies all sub-writes in one request, i.e. between the same two task
        cycles — no cycle sees half of them. Falls back to sequential writes where sum commands are unsupported."""
        c = self.conn(port)
        checks: list[tuple[str, Any, Any, T.TcType, dict[str, Any]]] = []
        problems = {}
        for name, value in items.items():
            try:
                chk = self.check_write(name, value, port)
                checks.append((name, value, chk["symbol"], chk["tt"], chk))
            except AdsError as exc:
                problems[name] = str(exc)
        if problems:
            raise AdsError("nothing was written — " + "; ".join(f"{k}: {v}" for k, v in problems.items()))
        before = self.read_many([s.name for _n, _v, s, _t, _c in checks], port)
        payload_hdr = b"".join(struct.pack("<III", s.index_group, s.index_offset, s.size) for _n, _v, s, _t, _c in checks)
        payload = payload_hdr + b"".join(self._value_bytes(s, tt, chk) for _n, _v, s, tt, chk in checks)
        method = "sum-write (one ADS request: all values applied between the same two PLC cycles)"
        codes: list[int] | None = None
        try:
            res = c.read_write(0xF081, len(checks), ctypes.c_uint32 * len(checks), (ctypes.c_ubyte * len(payload))(*payload),
                               ctypes.c_ubyte * len(payload), return_ctypes=True)
            codes = [int(x) for x in res]
        except Exception as exc:  # noqa: BLE001 — sum commands unsupported here: one by one
            method = f"sequential (sum-write unavailable: {_fmt_exc(exc)[:80]})"
        results: dict[str, Any] = {}
        applied: list[str] = []
        if codes is not None:
            for (name, _v, s, _t, _c), code in zip(checks, codes):
                if code:
                    results[s.name] = {"error": f"ADSError {code} (0x{code:X})"}
                else:
                    applied.append(s.name)
        else:
            for name, value, s, tt, chk in checks:
                try:
                    data = self._value_bytes(s, tt, chk)
                    c.write(s.index_group, s.index_offset, (ctypes.c_ubyte * len(data))(*data), ctypes.c_ubyte * len(data))
                    applied.append(s.name)
                except Exception as exc:  # noqa: BLE001
                    results[s.name] = {"error": _fmt_exc(exc)}
                    break
        after = self.read_many(applied, port) if applied else {}
        for name, value, s, tt, chk in checks:
            if s.name in applied:
                written = T.decode(tt, chk["encoded"]) if chk.get("encoded") is not None else chk.get("hex")
                d = {"before": before.get(s.name), "written": written, "read_back": after.get(s.name)}
                if not _same_value(written, after.get(s.name)):
                    d["note"] = "read_back differs: the PLC changed it after the write"
                results[s.name] = d
        failed = [k for k, v in results.items() if "error" in v]
        return {"method": method, "complete": not failed, "applied": applied, "failed": failed,
                "not_attempted": [s.name for _n, _v, s, _t, _c in checks if s.name not in applied and s.name not in failed],
                "results": results}

    def io_read(self, area: str, offset: int, size: int, port: int | None = None) -> dict[str, Any]:
        """Read the raw process image: area 'I' (inputs, ADSIGRP_IOIMAGE_RWIB) or 'Q' (outputs,
        ADSIGRP_IOIMAGE_RWOB) at byte offset — what the PLC task actually sees/wrote, independent
        of symbol names."""
        igrp = {"I": C.ADSIGRP_IOIMAGE_RWIB, "Q": C.ADSIGRP_IOIMAGE_RWOB}.get(area.upper())
        if igrp is None:
            raise AdsError("area must be 'I' or 'Q'")
        r = self.read_raw(igrp, offset, size, port)
        raw = bytes.fromhex(r["hex"])
        bits = " ".join(f"{b:08b}" for b in raw[:64])
        out = {"area": area.upper(), "offset": offset, "size": len(raw), "hex": r["hex"], "bits_msb_first": bits,
               "note": "offset/size are bytes of the PLC process image (%IB/%QB); map via the TMC or xae_tree_get on TIID"}
        if raw and not any(raw):
            out["note"] += " — all zero: either nothing is mapped at this offset, or no I/O is mapped at all (no terminals, " \
                           "or the process image is empty on a PC runtime)"
        return out

    def read_raw(self, index_group: int, index_offset: int, size: int, port: int | None = None,
                 net_id: str | None = None) -> dict[str, Any]:
        c = self.conn(port, net_id)
        try:
            raw = bytes(c.read(index_group, index_offset, ctypes.c_ubyte * size, return_ctypes=True, check_length=False))
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"raw read 0x{index_group:X}/0x{index_offset:X} ({size} B) on port {c.ams_net_port}: {_fmt_exc(exc)}") from exc
        return {"hex": raw.hex(), "size": len(raw), "as_uint_le": int.from_bytes(raw[:8], "little") if len(raw) <= 8 else None}

    def write_raw(self, index_group: int, index_offset: int, hex_data: str, port: int | None = None,
                  net_id: str | None = None) -> dict[str, Any]:
        c = self.conn(port, net_id)
        data = bytes.fromhex(hex_data)
        try:
            c.write(index_group, index_offset, (ctypes.c_ubyte * len(data))(*data), ctypes.c_ubyte * len(data))
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"raw write failed: {_fmt_exc(exc)}") from exc
        return {"written": len(data)}

    # ------------------------------------------------------------ watch / wait
    def watch(self, names: list[str], duration_ms: int, interval_ms: int, change_only: bool,
              port: int | None = None) -> dict[str, Any]:
        duration_ms = min(max(duration_ms, interval_ms), self.cfg.max_watch_duration_ms)
        interval_ms = max(interval_ms, self.cfg.min_watch_interval_ms)
        c = self.conn(port)
        types: dict[str, T.TcType] = {}
        for n in names:
            s = self.symbol_info(n, port)
            tt = T.parse_type(s.type, s.size)
            if tt.ctype is None or tt.opaque:
                raise AdsError(f"{n!r} is an opaque type; watch its members instead")
            types[s.name] = tt
        keys = list(types.keys())
        samples: list[dict[str, Any]] = []
        changes = 0
        last: dict[str, Any] | None = None
        t0 = time.perf_counter()
        deadline = t0 + duration_ms / 1000
        use_sum = True
        while True:
            now = time.perf_counter()
            try:
                if use_sum:
                    row = c.read_list_by_name(keys)
                else:
                    raise RuntimeError
            except Exception:  # noqa: BLE001
                use_sum = False
                row = {k: c.read_by_name(k, types[k].ctype) for k in keys}
            vals = {k: T.decode(types[k], v) for k, v in row.items()}
            if not change_only or last is None or vals != last:
                samples.append({"t_ms": round((now - t0) * 1000, 2), **vals})
                if last is not None and vals != last:
                    changes += 1
            last = vals
            if len(samples) >= self.cfg.max_watch_points or now >= deadline or self.cancel.is_set():
                break
            time.sleep(max(0.0, interval_ms / 1000 - (time.perf_counter() - now)))
        return {"symbols": keys, "interval_ms": interval_ms, "duration_ms": duration_ms,
                "samples": samples, "sample_count": len(samples), "changes": changes,
                "truncated": len(samples) >= self.cfg.max_watch_points}

    def wait_until(self, name: str, op: str, value: Any, timeout_ms: int, interval_ms: int,
                   port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        s = self.symbol_info(name, port)
        tt = T.parse_type(s.type, s.size)
        if tt.ctype is None or tt.opaque:
            raise AdsError("wait_until needs a scalar symbol")
        target = T.encode(tt, value)
        ops = {"==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b,
               ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}
        if op not in ops:
            raise AdsError(f"op must be one of {list(ops)}")
        t0 = time.perf_counter()
        deadline = t0 + min(timeout_ms, 600_000) / 1000
        polls = 0
        while True:
            cur = c.read_by_name(s.name, tt.ctype)
            polls += 1
            if ops[op](cur, target):
                return {"met": True, "value": T.decode(tt, cur), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1), "polls": polls}
            if time.perf_counter() >= deadline or self.cancel.is_set():
                return {"met": False, "value": T.decode(tt, cur), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1), "polls": polls,
                        "cancelled": self.cancel.is_set()}
            time.sleep(max(interval_ms, self.cfg.min_watch_interval_ms) / 1000)

    # ------------------------------------------------------------ EtherCAT
    def ethercat_overview(self, master_port: int | None = None) -> dict[str, Any]:
        """Master state + slave count via the EtherCAT master ADS port.
        IndexGroups per Tc2_EtherCAT (FB_EcGetMasterState / FB_EcGetSlaveCount)."""
        p = master_port or self.cfg.ethercat_master_port
        out: dict[str, Any] = {"master_port": p}
        try:
            st = self.read_raw(0x00000003, 0x00000100, 2, p)
            code = int.from_bytes(bytes.fromhex(st["hex"]), "little")
            out["master_state"] = {1: "INIT", 2: "PREOP", 3: "BOOT", 4: "SAFEOP", 8: "OP"}.get(code & 0x0F, code)
            out["master_state_raw"] = code
        except AdsError as exc:
            if "(6)" in str(exc) or "port not found" in str(exc).lower() or "0x6" in str(exc):
                return {"master_port": p, "master": None,
                        "note": f"no EtherCAT master answers on ADS port {p} — this target has no EtherCAT master configured "
                                f"(an engineering PC with the local runtime, or a CX without an EtherCAT device in TIID). "
                                f"Nothing is wrong unless you expected terminals here."}
            out["master_state_error"] = str(exc)
        try:
            cnt = self.read_raw(0x00000006, 0x00000000, 2, p)
            out["slave_count"] = int.from_bytes(bytes.fromhex(cnt["hex"]), "little")
        except AdsError as exc:
            out["slave_count_error"] = str(exc)
        try:
            # ADS state of the master device itself
            out["device"] = self.state(p)
        except AdsError as exc:
            out["device_error"] = str(exc)
        return out

    # ------------------------------------------------------------ routes
    def local_routes(self) -> dict[str, Any]:
        p = Path(self.cfg.static_routes_xml)
        if not p.is_file():
            return {"file": str(p), "routes": [], "note": "StaticRoutes.xml not found; is this the engineering PC?"}
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError as exc:
            raise AdsError(f"cannot parse {p}: {exc}") from exc
        routes = []
        for r in root.iter("Route"):
            routes.append({child.tag: (child.text or "").strip() for child in r})
        return {"file": str(p), "routes": routes}

    def add_route_to_plc(self, plc_ip: str, plc_username: str, plc_password: str,
                         route_name: str | None, local_net_id: str | None,
                         hostname: str | None) -> dict[str, Any]:
        """Add a route *on the PLC* pointing back to this PC (UDP 48899 protocol)."""
        import socket
        if local_net_id is None:
            pyads.open_port()
            local_net_id = pyads.get_local_address().netid  # type: ignore[union-attr]
            pyads.close_port()
        host = hostname or socket.gethostname()
        try:
            ok = pyads.add_route_to_plc(local_net_id, host, plc_ip, plc_username, plc_password,
                                        route_name=route_name or host)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"add_route_to_plc failed: {_fmt_exc(exc)}") from exc
        return {"ok": bool(ok), "plc_ip": plc_ip, "local_net_id": local_net_id, "route_name": route_name or host,
                "note": "This created the PLC-side route. The local (PC-side) route is in StaticRoutes.xml / "
                        "the XAE route dialog; use xae_tree_consume_xml on 'TIRR' for a project route."}

    def discover(self, timeout_s: float = 2.0, broadcast: str = "255.255.255.255") -> dict[str, Any]:
        """UDP broadcast discovery (port 48899) of TwinCAT targets on the local networks:
        NetId, hostname, TwinCAT version, IP. Same protocol the XAE 'Broadcast Search' uses."""
        import socket
        try:
            local = self.local_net_id()
        except Exception:  # noqa: BLE001
            local = "1.1.1.1.1.1"
        req = b"\x03\x66\x14\x71\x00\x00\x00\x00\x01\x00\x00\x00" + bytes(int(x) for x in local.split(".")) \
            + struct.pack("<H", 10000) + b"\x00\x00\x00\x00"
        found: dict[str, dict[str, Any]] = {}
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        try:
            try:
                sock.bind(("", 0))
            except OSError:
                pass
            sock.sendto(req, (broadcast, 48899))
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                d = _parse_discovery(data)
                if d:
                    d["ip"] = addr[0]
                    found[d.get("ams_net_id", addr[0])] = d
        finally:
            sock.close()
        return {"count": len(found), "targets": list(found.values()), "broadcast": broadcast, "timeout_s": timeout_s}

    def local_net_id(self) -> str:
        """AmsNetId of the local TwinCAT router. Raises AdsError (never a raw pyads/ctypes error) when the
        router is not running or TcAdsDll.dll is unusable — tc_doctor reports that instead of crashing."""
        try:
            pyads.open_port()
        except (pyads.ADSError, OSError, AttributeError) as exc:
            raise AdsError(f"local AMS router not reachable: {_fmt_exc(exc)} — is the TwinCAT System Service "
                           f"running (TwinCAT icon in the tray, blue or green), and is TcAdsDll.dll the real Beckhoff "
                           f"library?") from exc
        try:
            return pyads.get_local_address().netid  # type: ignore[union-attr]
        except (pyads.ADSError, OSError, AttributeError) as exc:
            raise AdsError(f"get_local_address failed: {_fmt_exc(exc)}") from exc
        finally:
            try:
                pyads.close_port()
            except Exception:  # noqa: BLE001
                pass


# ----------------------------------------------------------------------------
def _split_string_array(raw: bytes, tt: T.TcType) -> list[str]:
    assert tt.elem is not None and tt.elem.size
    n = tt.elem.size
    out = []
    for i in range(0, len(raw), n):
        chunk = raw[i:i + n]
        if tt.elem.kind == "wstring":
            out.append(chunk.decode("utf-16-le", "replace").split("\x00", 1)[0])
        else:
            out.append(chunk.decode("latin-1", "replace").split("\x00", 1)[0])
    return out


def _parse_discovery(data: bytes) -> dict[str, Any] | None:
    if len(data) < 24 or data[:4] != b"\x03\x66\x14\x71" or data[8:12] != b"\x01\x00\x00\x80":
        return None
    out: dict[str, Any] = {"ams_net_id": ".".join(str(b) for b in data[12:18]), "ams_port": struct.unpack_from("<H", data, 18)[0]}
    n_items = struct.unpack_from("<I", data, 20)[0]
    pos = 24
    raw_tags = {}
    for _ in range(n_items):
        if pos + 4 > len(data):
            break
        tag, ln = struct.unpack_from("<HH", data, pos)
        pos += 4
        val = data[pos:pos + ln]
        pos += ln
        if tag == 5:
            out["hostname"] = val.split(b"\x00", 1)[0].decode("latin-1", "replace")
        elif tag == 3 and ln >= 4:
            out["twincat_version"] = f"{val[0]}.{val[1]}.{struct.unpack_from('<H', val, 2)[0]}"
        elif tag == 18 and ln >= 12:
            maj, mi, bld = struct.unpack_from("<III", val, 4) if ln >= 16 else (0, 0, 0)
            out["os_version"] = f"{maj}.{mi}.{bld}"
            raw_tags[tag] = val.hex()
        else:
            raw_tags[tag] = val.hex()[:64]
    if raw_tags:
        out["raw_tags"] = raw_tags
    return out


def _json_strict(v: Any) -> str:
    """JSON that every parser reads: a REAL that is NaN / ±Infinity becomes the string "NaN" / "Infinity" /
    "-Infinity" (json.dumps writes bare NaN, which strict parsers refuse)."""
    import json
    try:
        return json.dumps(v, default=str, allow_nan=False)
    except ValueError:
        def fix(x: Any) -> Any:
            if isinstance(x, float) and (x != x or x in (float("inf"), float("-inf"))):
                return "NaN" if x != x else ("Infinity" if x > 0 else "-Infinity")
            if isinstance(x, dict):
                return {k: fix(y) for k, y in x.items()}
            if isinstance(x, (list, tuple)):
                return [fix(y) for y in x]
            return x
        return json.dumps(fix(v), default=str, allow_nan=False)


class _Counting:
    """File wrapper that counts the characters written (R42: max_total_mb bounds what is written, not what is read)."""

    def __init__(self, fh: Any):
        self.fh, self.n = fh, 0

    def write(self, s: str) -> int:
        self.n += len(s)
        return self.fh.write(s)


_LIBRARY_GLOBALS = ("gvl_tcunit", "gvl_param_tcunit", "global_variables", "global_version", "constants", "tcunit",
                    "param_", "gvl_param_")


def _dump_order(roots: list[SymbolEntry], own: set[str] | None) -> list[SymbolEntry]:
    """R42 #5: the program's own roots first (MAIN, PRG_*, the project's GVLs), library globals last, so a budget is
    spent on what the user wrote. own = PROGRAM / GVL names from the project source when one is configured."""
    def rank(s: SymbolEntry) -> int:
        head = s.name.split(".")[0].split("[")[0].lower()
        if own:
            return 0 if head in own else 2 if head.startswith(_LIBRARY_GLOBALS) else 1
        return 2 if head.startswith(_LIBRARY_GLOBALS) else 0
    return sorted(roots, key=lambda s: (rank(s), s.name.lower()))


def _is_system_symbol(name: str) -> bool:
    low = name.lower()
    return (low.startswith("twincat_systeminfovarlist") or low.startswith("constants.") or low.startswith("__")
            or ".__" in low or low.startswith("global_version.") or low.startswith("tcunit.") or low.startswith("tc2_") or low.startswith("tc3_"))


def _csv_val(v: Any) -> Any:
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns", "name"):
            if k in v:
                return v[k] if k != "name" else f"{v.get('value')} ({v['name']})"
        import json as _j
        return _j.dumps(v)
    if isinstance(v, list):
        import json as _j
        return _j.dumps(v)
    return v


_ADS_HINTS = {
    6: "target port not found — nothing is listening on that AMS port",
    7: "target machine not found — the AMS route to that NetId is missing or the target is off",
    1797: "parameter size not correct — the value does not fit the variable (string too long, wrong array length or type)",
    1808: "symbol not found — check the spelling (ads_list_symbols) or whether the program was downloaded",
    1861: "timeout — the target did not answer (router, route, or the target is busy/stopped)",
}


def _fmt_exc(exc: BaseException) -> str:
    if isinstance(exc, pyads.ADSError):
        hint = _ADS_HINTS.get(exc.err_code)
        return f"ADSError {exc.err_code} (0x{exc.err_code:X}): {exc.msg}" + (f" — {hint}" if hint else "")
    return f"{type(exc).__name__}: {exc}"
