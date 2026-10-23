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
import os
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
from .ads_types import ADST_NAME, POINTER_NOTE, TypeSystem, Leaf, WalkCancelled, parse_table

SYSTEM_SERVICE_PORT = 10000
ROUTER_PORT = 1

SYMFLAG = {
    0x0001: "PERSISTENT", 0x0002: "BITVALUE", 0x0004: "REFERENCETO", 0x0008: "TYPEGUID",
    0x0010: "TCCOMIFACEPTR", 0x0020: "READONLY", 0x0040: "ITFMETHODACCESS",
    0x0080: "METHODDEREF", 0x1000: "ATTRIBUTES", 0x2000: "STATIC", 0x4000: "INITONRESET",
    0x8000: "EXTENDEDFLAGS",
}


def _same_value(a: Any, b: Any) -> bool:
    a, b = T.raw_value(a), T.raw_value(b)
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


MIB = 1024 * 1024                 # ads_dump: max_total_mb and every size it reports are MiB (N53)
_DUMP_CHUNK = 256 * 1024          # N39: one ADS read never asks for more (ADS 1802 'out of memory' on big symbols)


class AdsError(RuntimeError):
    pass


class ProgramChanged(AdsError):
    """R43: the symbol version read with the values is not the one the caller works with (download / online change):
    the values are not returned."""

    def __init__(self, expected: int, now: int | None) -> None:
        super().__init__(f"the PLC program changed (symbol version {expected} -> {now}: download / online change)"
                         if now is not None else "the symbol version could not be read with the values: they are not "
                         "verified against a program change")
        self.expected = expected
        self.now = now


@dataclass
class SymbolEntry:
    name: str
    type: str
    size: int
    index_group: int
    index_offset: int
    flags: int
    comment: str
    data_type: int = 0          # ADS data type id the runtime gives the symbol (ADST_*)

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
        import weakref
        # symbol handles per connection object, all of one symbol version (_get_handles)
        self._handles: "weakref.WeakKeyDictionary[Any, dict[str, Any]]" = weakref.WeakKeyDictionary()
        self.net_id: str | None = cfg.ams_net_id
        self.ip: str | None = cfg.ip_address
        self.port: int = cfg.ams_port
        self.cancel = threading.Event()   # set by the worker when a tool call times out / is cancelled
        self.dump_progress: dict[str, Any] = {}
        # N52: the dump's commit (rename into place) and the timeout's verdict ("kept" / "not kept") are decided under
        # this lock, so the client is never told "nothing is kept" for files that were renamed into place
        self._dump_lock = threading.Lock()

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
            com = T.plc_text(raw[p:p + clen])                  # N95: the comment is the source's UTF-8 text
            out.append(SymbolEntry(name, typ, size, igrp, ioffs, flags, com, dtype))
            ptr += entry_len
        return out, {s.name.lower(): s for s in out}

    def _table_identity(self, c: pyads.Connection) -> tuple[Any, ...] | None:
        """What says the runtime still has the symbol table that was loaded: its symbol version (ADSIGRP_SYM_VERSION
        0xF008, +1 on every download / online change), the sizes of its symbol and datatype tables
        (ADSIGRP_SYM_UPLOADINFO2) and the PLC application's compile timestamp — after a TwinCAT restart with another
        build the version byte can stand where the old one stood, the build's timestamp cannot. Read at most every
        2 s per target; None where none of it is readable (then the TTL applies)."""
        key = (c.ams_net_id, c.ams_net_port)
        vc = self.__dict__.setdefault("_symver", {})
        hit = vc.get(key)
        if hit and time.time() - hit[0] < 2.0:
            return hit[1]
        parts: list[Any] = []
        try:
            parts.append(int(c.read(0xF008, 0, ctypes.c_ubyte)))
        except Exception:  # noqa: BLE001
            parts.append(None)
        try:
            info = bytes(c.read(C.ADSIGRP_SYM_UPLOADINFO2, 0, ctypes.c_ubyte * 64, return_ctypes=True, check_length=False))
            parts.append(struct.unpack_from("<IIII", info, 0))
        except Exception:  # noqa: BLE001
            parts.append(None)
        try:
            parts.append(int(c.read_by_name("TwinCAT_SystemInfoVarList._AppInfo.AppTimestamp", pyads.PLCTYPE_UDINT)))
        except Exception:  # noqa: BLE001 — not a PLC runtime port
            parts.append(None)
        ident = tuple(parts) if any(x is not None for x in parts) else None
        vc[key] = (time.time(), ident)
        return ident

    def symbols_cached(self, refresh: bool = False, port: int | None = None) -> tuple[list[SymbolEntry], dict[str, SymbolEntry]]:
        """Symbol table cache. Invalidated when the runtime's table identity changes (download / online change /
        activation / a restart with another build) — not on a fixed timer; the TTL only applies where the identity
        is not readable. Every read by name goes through this table (read_values): one source for name → address."""
        c = self.conn(port)
        key = (c.ams_net_id, c.ams_net_port)
        hit = self._symcache.get(key)
        ver = self._table_identity(c)
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

    def refresh_symbols(self, port: int | None = None) -> None:
        """Load the symbol table again now — not from the identity read of the last 2 s (R43: the program changed)."""
        c = self.conn(port)
        self.__dict__.setdefault("_symver", {}).pop((c.ams_net_id, c.ams_net_port), None)
        self._drop_handles(c)
        self.symbols_cached(True, port)

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
            s = SymbolEntry(e.name, e.symbol_type, e.size, e.iGroup, e.iOffs, e.flags, e.comment,
                            int(getattr(e, "dataType", 0) or 0))
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
        ver = self._table_identity(c)
        dtver = self.__dict__.setdefault("_dtver", {})
        if hit and not refresh:
            if hit[1] is not None:
                hit[1].declared = self._alias_hook(c.ams_net_port)   # the project's aliases (N50) — set again: it may change
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
        if ts is not None:
            ts.declared = self._alias_hook(c.ams_net_port)
        return ts

    def _alias_hook(self, port: int) -> Callable[[str], str | None] | None:
        """The project's alias lookup for the runtime on this ADS port (N113: an alias declared in two PLC projects is
        the one of the PLC project on this port)."""
        hint = self.alias_hint
        return None if hint is None else (lambda name: hint(name, port))

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
            tt = T.parse_type(s.type, s.size, s.data_type)
            if tt.ctype is not None and not tt.opaque:
                return s, [Leaf(s.name, s.type, 0, s.size, s.data_type,
                                None, None if T.is_elementary(tt) or not tt.guessed else "type not in datatype table")]
            # fallback: published member symbols
            mem = self.members(s.name, port)
            return s, [Leaf(m.name, m.type, m.index_offset - s.index_offset, m.size, 0, None, "from symbol table") for m in mem]
        return s, ts.leaves(s.name, s.type, max_leaves, size=s.size)

    def root_names(self, port: int | None = None) -> set[str]:
        """N92: the first name segment (lower case) of every symbol of the runtime's table — the PROGRAMs, GVLs and
        library roots the target has (a GVL without symbols is not there)."""
        lst, _ = self.symbols_cached(False, port)
        return {s.name.split(".")[0].split("[")[0].lower() for s in lst}

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

    def leaf_values(self, root: SymbolEntry, leaves: list[Leaf],
                    port: int | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        """Decode every leaf of the root symbol → ({path: value}, {path: note}). Read through _RootReader like ads_dump
        (N39): bounded chunks, only the bytes a leaf needs — the head of a value that is not expanded (reading its full
        size read a whole 8 MiB buffer for a 64-byte preview) — so a big root is never one ADS request. A leaf whose
        value is not given says why in the notes (it was dropped without a word)."""
        c = self.conn(port)
        ts = self.typesystem(False, port)
        need = max((lf.offset + lf.need for lf in leaves if lf.size > 0), default=0)
        reader = _RootReader(c, root, lambda: False, need_end=need or None)
        out: dict[str, Any] = {}
        notes: dict[str, str] = {}
        for lf in leaves:
            if lf.size <= 0:
                out[lf.path] = None
                notes[lf.path] = _leaf_note(lf, None)
                continue
            try:
                raw = reader.get(lf.offset, lf.need)
            except _OutsideRoot as exc:
                out[lf.path] = None
                notes[lf.path] = str(exc)
                continue
            except _ReadFailed as exc:
                raise AdsError(f"read {root.name!r}: {exc}") from exc
            z = _at_zero(lf)
            val = ts.decode_leaf(raw, z) if ts is not None else TypeSystem._decode_scalar(raw, 0, lf.type, None, None)
            out[lf.path] = val
            if lf.not_expanded or val is None:
                notes[lf.path] = _leaf_note(lf, val)
        return out, notes

    def read_full(self, name: str, max_bytes: int | None = None, port: int | None = None) -> dict[str, Any]:
        """One ADS read of the whole variable, decoded into a nested tree via the datatype table."""
        c = self.conn(port)
        s = self.symbol_info(name, port)
        limit = max_bytes or self.cfg.max_file_bytes
        if s.size > limit:
            raise AdsError(f"{s.name} is {s.size} bytes; raise max_bytes or read members individually")
        reader = _RootReader(c, s, lambda: False, expect_version=self._table_version(c))
        try:                                       # N39: in bounded chunks, never one request for a big variable
            raw = b"".join(reader.get(o, min(_DUMP_CHUNK, max(s.size, 1) - o)) for o in range(0, max(s.size, 1), _DUMP_CHUNK))
        except _ReadFailed as exc:
            raise AdsError(f"read {s.name!r} ({s.size} B): {exc}") from exc
        ts = self.typesystem(False, port)
        out: dict[str, Any] = {"name": s.name, "type": s.type, "size": s.size, "flags": s.flag_names()}
        tt = T.parse_type(s.type, s.size)
        if ts is not None and (ts.get(s.type) is not None or (tt.kind == "array" and not T.is_elementary(tt))):
            out["value"] = ts.decode_tree(raw, s.type, size=s.size)
            out["decoder"] = "datatype-table"
            e = ts.get(s.type)
            if ts.is_indirect(e, s.type):
                out["note"] = POINTER_NOTE
            src = ts.source_base(e)
            if src:
                out["type_source"] = (f"project source: TYPE {s.type} : {src} — the runtime's datatype table gives "
                                      f"{s.type} no base type (N50)")
            elif e is not None and not (e.type or e.subitems or e.dims or e.enum) and isinstance(out["value"], dict) \
                    and "raw_hex" in out["value"]:
                out["type_note"] = (f"the runtime's datatype table describes {s.type} only as {e.size} bytes "
                                    f"({ADST_NAME.get(e.data_type, e.data_type)}, no base type, no members): shown as raw "
                                    "bytes. An alias declared in the project source (TYPE … : POINTER TO …) is decoded "
                                    "as what it is once the project is configured (project.root)")
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

    def dump(self, pattern: str | None, include_system: bool, out_dir: str, max_total_bytes: int = 64 * MIB,
             max_leaves: int = 500_000, port: int | None = None, cancel: Any = None,
             own_roots: set[str] | None = None) -> dict[str, Any]:
        """Read every variable (every root symbol, fully expanded) and write JSON + CSV. Written as *.partial and
        renamed when complete; a failed or cancelled dump leaves no file behind and says so (R8). R42: pointers are
        addresses (never the pointee), one leaf budget for CSV and JSON, max_total_bytes counts the bytes WRITTEN,
        the JSON is streamed root by root. N52: the rename into place happens under _dump_lock after a last cancel
        check, both renames or neither (rolled back on a file error); dump_outcome() gives the timeout its verdict
        under the same lock."""
        import os
        os.makedirs(out_dir, exist_ok=True)
        with self._dump_lock:
            jpath, cpath, jpart, cpart = _reserve_dump_names(out_dir)
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

        def fail(msg: str, exc: BaseException | None = None) -> AdsError:
            removed = drop()
            self.dump_progress["phase"] = "failed"
            self.dump_progress["error"] = msg
            return AdsError(msg + (f" — partial files removed: {removed}" if removed else ""))
        try:
            res = self._dump(pattern, include_system, cpart, jpart, max_total_bytes, max_leaves, port, cancel, own_roots)
        except BaseException as exc:
            with self._dump_lock:
                raise fail(f"ads_dump failed: {_fmt_exc(exc) if not isinstance(exc, AdsError) else exc}") from exc
        with self._dump_lock:
            if cancel is not None and cancel.is_set():
                raise fail("ads_dump cancelled: the call had already ended (timeout) — the files are not kept")
            def taken(exc: BaseException | None = None) -> AdsError:
                return fail(f"ads_dump: {', '.join(p.name for p in (cpath, jpath) if p.exists())} appeared in {out_dir} "
                            "while the dump ran (another program?) — not overwritten, nothing kept")
            if cpath.exists() or jpath.exists():       # the names were free when reserved; never overwrite a file
                raise taken()
            try:
                _move_new(cpart, cpath)                # fails, never replaces, if the name was taken meanwhile
            except FileExistsError as exc:
                raise taken(exc) from exc
            except OSError as exc:
                raise fail(f"ads_dump: the finished files could not be moved into place (a file error, not an ADS "
                           f"error): {exc}") from exc
            try:
                _move_new(jpart, jpath)
            except OSError as exc:
                try:
                    os.replace(cpath, cpart)           # both or neither: the CSV goes back to .partial
                except OSError as back:
                    # neither both nor neither: say exactly what is where. Both files are complete; nothing is removed
                    msg = (f"ads_dump: PARTIAL commit — the CSV is in place ({cpath}), the JSON could not be renamed "
                           f"({exc}) and the CSV could not be moved back ({back}). Both files are complete; the JSON is "
                           f"at {jpart} (rename it to {jpath.name}).")
                    self.dump_progress.update(phase="partial", files=[str(cpath)], left=[str(jpart)], error=msg)
                    raise AdsError(msg) from exc
                raise fail(f"ads_dump: the finished files could not be moved into place (a file error, not an ADS "
                           f"error): {exc}") from exc
            self.dump_progress.update(phase="done", files=[str(cpath), str(jpath)])
        res.update({"json": str(jpath), "csv": str(cpath)})
        return res

    def dump_outcome(self) -> dict[str, Any]:
        """The dump's state, read under the commit lock (N52): 'done' with the files when it was renamed into place,
        anything else means it is not (and, once the cancel event is set, will not be) kept."""
        with self._dump_lock:
            return dict(self.dump_progress)

    def _dump(self, pattern: str | None, include_system: bool, cpath: Any, jpath: Any, max_total_bytes: int,
              max_leaves: int, port: int | None, cancel: Any, own_roots: set[str] | None = None) -> dict[str, Any]:
        """One pass over the roots. CSV and JSON come from the SAME decoded leaves (N48/N50: the JSON tree is
        nested from the leaf values, so the two files cannot disagree); a root is read in bounded chunks only where
        a leaf needs bytes (N39/N47: never one read of a huge symbol, never a huge root in RAM); the byte budget is
        checked after every row (N47) and the cancel event inside the leaf walk, the row loop and the chunk reads
        (N55). A root that cannot be read is named in the result, the JSON footer and a CSV row (N49)."""
        import csv, io, json
        c = self.conn(port)
        table = self.typesystem(False, port)
        ts = table if table is not None else TypeSystem({})
        listed = self.roots(pattern, include_system, port)
        roots = _dump_order(listed, own_roots)
        # N92: counted on the target, not in the source (a GVL nothing references is declared but has no symbol)
        own_listed = len({r.name.split(".")[0].split("[")[0].lower() for r in listed} & (own_roots or set()))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        n_leaves = 0
        n_read = 0
        errors: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        unreadable: list[dict[str, str]] = []
        sizes: list[dict[str, Any]] = []
        preview: list[dict[str, Any]] = []
        truncated: str | None = None
        t0 = time.time()
        prog = self.dump_progress

        def cancelled() -> bool:
            return cancel is not None and cancel.is_set()

        def stop(where: str) -> AdsError:
            return AdsError(f"cancelled {where} — the call timed out or was stopped")

        # N76: max_total_bytes bounds the CSV + JSON ON DISK, nothing else (the bytes read from the PLC are not
        # counted). Nothing is estimated: before a row is written, the dump knows exactly what that row adds, what the
        # root's JSON adds with it (_TreeStream), and the largest ending it may still have to write — the root cut
        # here, or a read error on a later leaf (its text bounded by _ERR_JSON_MAX), and the footer with every later
        # root listed as skipped. A row that would make that pass the limit is not written: the root is cut there.
        size_reason = f"size limit: CSV + JSON on disk would pass max_total_mb ({max_total_bytes / MIB:g} MiB)"
        leaf_reason = f"leaf limit: max_leaves {max_leaves}"
        long_reason = max(size_reason, leaf_reason, key=len)
        ph = "x" * _ERR_JSON_MAX            # stands for the longest error text the files can hold
        rowbuf = io.StringIO()
        rw = csv.writer(rowbuf)

        def csv_line(row: list[Any]) -> str:
            rowbuf.seek(0)
            rowbuf.truncate()
            rw.writerow(row)
            return rowbuf.getvalue()

        def blen(s: str) -> int:
            return len(s.encode("utf-8"))

        def footer(leaves_v: int, truncated_v: str | None, unread: list[dict[str, str]], fail: list[dict[str, Any]],
                   skip: list[dict[str, str]]) -> str:
            complete_v = not truncated_v and not fail and not unread
            return "\n}, " + json.dumps({"roots": len(roots), "leaves": leaves_v, "complete": complete_v,
                                         "truncated": bool(truncated_v), "truncated_reason": truncated_v,
                                         "unreadable_leaves": unread[:200], "unreadable_total": len(unread),
                                         "failed_roots": fail[:200], "failed_total": len(fail),
                                         "skipped_roots": skip[:200], "skipped_total": len(skip)})[1:] + "\n"

        w_cut = len('{"_truncated": true, "_reason": ') + len(json.dumps(long_reason)) + len(', "_partial": ') + 1
        w_err = len('{"_incomplete": ') + _ERR_JSON_MAX + len(', "_partial": ') + 1

        def later_skipped(i: int) -> list[dict[str, str]]:
            return [{"root": x.name, "reason": long_reason} for x in roots[i + 1:]]

        def endings(i: int, r: Any, n_lv: int, cut_leaves: bool, unread: list[dict[str, str]]) -> tuple[int, int, int]:
            """(cut, read error, normal end): the most each ending of root i can add beyond its tree, with the footer
            in which every later root is skipped — computed from the real state, nothing estimated."""
            later = later_skipped(i)
            f_cut = footer(n_leaves + n_lv, long_reason, unread, failed,
                           skipped + [{"root": r.name, "reason": f"cut after {n_lv} leaves: {long_reason}"}] + later)
            f_err = footer(n_leaves + n_lv, long_reason, unread,
                           failed + [{"root": r.name, "error": ph, "leaves_written": n_lv}],
                           skipped + [{"root": r.name, "reason": f"read stopped after {n_lv} of {n_lv} leaves: {ph}"}]
                           + later)
            row_err = blen(csv_line([r.name, r.type, "", f"INCOMPLETE: read stopped after {n_lv} of {n_lv} leaves: {ph}"]))
            f_done = footer(n_leaves + n_lv, long_reason if later else (leaf_reason if cut_leaves else None), unread,
                            failed, skipped + ([{"root": r.name, "reason": f"cut after {n_lv} leaves: {leaf_reason}"}]
                                               if cut_leaves else []) + later)
            return w_cut + len(f_cut), w_err + row_err + len(f_err), (w_cut if cut_leaves else 0) + len(f_done)

        with open(cpath, "wb") as fh, open(jpath, "wb") as jh:
            cw, jw = _Counting(fh), _Counting(jh)
            cw.write(csv_line(["path", "type", "value", "note"]))
            jw.write('{"target": ' + json.dumps(self.target_label()) + ', "taken": ' + json.dumps(stamp) + ', "values": {')
            frame = cw.n + jw.n + len(footer(0, long_reason, [], [], later_skipped(-1)))
            if frame > max_total_bytes:
                raise AdsError(f"max_total_mb ({max_total_bytes} bytes) is smaller than the files' own frame "
                               f"({frame} bytes: CSV header, JSON head, the footer naming the {len(roots)} roots) — "
                               "nothing would fit")
            first = True
            for i, r in enumerate(roots):
                if cancelled():
                    raise stop(f"at root {i + 1} of {len(roots)} ({r.name})")
                if truncated:
                    skipped.append({"root": r.name, "reason": truncated})
                    continue
                if n_leaves >= max_leaves:
                    truncated = leaf_reason
                    skipped.append({"root": r.name, "reason": truncated})
                    continue
                prog.update(phase="listing leaves", root=r.name, index=i + 1, of=len(roots), leaves=n_leaves,
                            written_mib=round((cw.n + jw.n) / MIB, 2))
                left = max_leaves - n_leaves
                try:
                    lv = ts.leaves(r.name, r.type, left + 1, size=r.size, cancel=cancelled)
                except WalkCancelled as exc:
                    raise stop(f"while listing the leaves of {r.name}") from exc
                cut = len(lv) > left
                lv = lv[:left]
                head = ("" if first else ",") + "\n" + json.dumps(r.name) + ": "
                # bytes past the last one any leaf needs are never requested (a value that is not expanded needs
                # only its head): an 8 MiB buffer costs one 64-byte read
                reader = _RootReader(c, r, cancelled, need_end=max((x.offset + x.need for x in lv), default=0),
                                     expect_version=self._table_version(c))
                # what the leaves after leaf k still need (N76): suffix bounds over the leaves that read anything
                suf_lo, suf_hi = [0] * (len(lv) + 1), [0] * (len(lv) + 1)
                lo_, hi_ = 1 << 62, -1
                suf_lo[len(lv)], suf_hi[len(lv)] = lo_, hi_
                for kk in range(len(lv) - 1, -1, -1):
                    x = lv[kk]
                    if x.size and x.need and 0 <= x.offset and x.offset + x.need <= reader.size:
                        lo_, hi_ = min(lo_, x.offset), max(hi_, x.offset + x.need)
                    suf_lo[kk], suf_hi[kk] = lo_, hi_
                tree = _TreeStream(r.name)
                c0, j0 = cw.n, jw.n
                read_error: str | None = None
                read_error_full = ""
                fits = True
                # whether the root is readable is known before anything of it is written: its first bytes are read
                # first (a root up to one chunk is then read completely — no later read, so no read error, can follow)
                lead = next((x for x in lv if x.size and x.need), None)
                # a leaf list that does not form the tree in order (it cannot be streamed) is known before anything is
                # written: the root is reported like one that could not be read — within the limit (N76)
                order = _tree_order_problem(r.name, [x.path for x in lv])
                if order is not None:
                    read_error_full = f"internal: the leaves of {r.name} are not in tree order ({order}) — not written"
                    read_error = _err_text(read_error_full)
                    lead = None
                if lead is not None:
                    try:
                        reader.get(lead.offset, lead.need)
                    except _DumpCancelled as exc:
                        raise stop(f"while reading {r.name}") from exc
                    except _OutsideRoot:
                        pass                                # that leaf's own note, below
                    except _ReadFailed as exc:
                        read_error, read_error_full = _err_text(exc), str(exc)
                if read_error is not None:
                    later = later_skipped(i)
                    f = footer(n_leaves, long_reason if later else None, unreadable,
                               failed + [{"root": r.name, "error": read_error, "leaves_written": 0}],
                               skipped + [{"root": r.name, "reason": f"not read: {read_error}"}] + later)
                    end_b = (blen(csv_line([r.name, r.type, "", f"NOT READ: {read_error}"])) + len(head)
                             + len('{"_not_read": ' + json.dumps(read_error) + "}") + len(f))
                    fits = cw.n + jw.n + end_b <= max_total_bytes
                elif not lv:
                    fits = cw.n + jw.n + len(head) + 2 + endings(i, r, 0, cut, unreadable)[2] <= max_total_bytes
                res_cut, res_err, res_done = endings(i, r, len(lv), cut, unreadable)
                prog["phase"] = "reading and writing"
                for k, leaf in enumerate(lv if read_error is None and fits else []):
                    if k % 256 == 255 and cancelled():
                        raise stop(f"while writing {r.name} (leaf {k + 1} of {len(lv)})")
                    outside: str | None = None
                    try:
                        val = ts.decode_leaf(reader.get(leaf.offset, leaf.need), _at_zero(leaf)) if leaf.size else None
                    except _DumpCancelled as exc:
                        raise stop(f"while reading {r.name}") from exc
                    except _OutsideRoot as exc:
                        val, outside = None, str(exc)      # this leaf only: the others are inside and readable
                    except _ReadFailed as exc:
                        read_error, read_error_full = _err_text(exc), str(exc)
                        break
                    line = csv_line([leaf.path, leaf.type, _csv_val(val), outside or _leaf_note(leaf, val)])
                    # the JSON says why too: a bare null there stood for a value that was never read
                    vjson = _json_strict({"_not_read": outside} if outside else val)
                    st = tree.steps(leaf.path)
                    try:
                        cost = tree.plan(st, vjson)
                    except _TreeOrder as exc:
                        read_error_full = (f"internal: leaf {leaf.path} does not continue the tree of {r.name} ({exc}) "
                                           "— not written")
                        read_error = _err_text(read_error_full)
                        break
                    entry = {"leaf": leaf.path, "error": outside} if outside else None
                    if entry is not None:
                        res_cut, res_err, res_done = endings(i, r, len(lv), cut, unreadable + [entry])
                    # the endings still possible after this leaf: after the last one only the normal end; before it
                    # a cut — and a read error only while bytes of the root are still to be read
                    room = (res_done if k == len(lv) - 1 else
                            res_cut if reader.covers(suf_lo[k + 1], suf_hi[k + 1]) else max(res_cut, res_err))
                    if cw.n + jw.n + blen(line) + len(head) + tree.size_after(cost, st) + room > max_total_bytes:
                        truncated = size_reason
                        if k == 0:
                            fits = False                    # not even its first row: the root is skipped, not cut
                        else:
                            cut = True
                        if entry is not None:
                            res_cut, res_err, res_done = endings(i, r, len(lv), cut, unreadable)
                        break
                    cw.write(line)
                    tree.apply(st, vjson)
                    if entry is not None:
                        errors.append({"symbol": leaf.path, "error": outside})
                        unreadable.append(entry)
                    if len(preview) < 50:
                        preview.append({"path": leaf.path, "type": leaf.type, "value": val})
                n_read += reader.bytes_read
                if not fits:
                    truncated = size_reason
                    skipped.append({"root": r.name, "reason": truncated})
                    if read_error is not None:
                        errors.append({"symbol": r.name, "error": read_error_full + " (not in the files: the size limit "
                                       "left no room for it)"})
                    continue
                done = tree.leaves
                n_leaves += done
                body = tree.text()
                if read_error is not None:
                    # N49: a root (or the rest of one) that could not be read is never silent
                    errors.append({"symbol": r.name, "error": read_error_full or read_error})   # the result: whole
                    failed.append({"root": r.name, "error": read_error, "leaves_written": done})
                    skipped.append({"root": r.name, "reason": f"not read: {read_error}" if not done else
                                    f"read stopped after {done} of {len(lv)} leaves: {read_error}"})
                    cw.write(csv_line([r.name, r.type, "", f"NOT READ: {read_error}" if not done else
                                       f"INCOMPLETE: read stopped after {done} of {len(lv)} leaves: {read_error}"]))
                    node = ('{"_not_read": ' + json.dumps(read_error) + "}" if not done else
                            '{"_incomplete": ' + json.dumps(read_error) + ', "_partial": ' + body + "}")
                elif cut:
                    truncated = truncated or leaf_reason
                    skipped.append({"root": r.name, "reason": f"cut after {done} leaves: {truncated}"})
                    node = '{"_truncated": true, "_reason": ' + json.dumps(truncated) + ', "_partial": ' + body + "}"
                else:
                    node = body
                jw.write(head + node)
                first = False
                sizes.append({"root": r.name, "leaves": done, "bytes_read": reader.bytes_read,
                              "csv_bytes": cw.n - c0, "json_bytes": jw.n - j0})
            complete = not truncated and not failed and not unreadable
            jw.write(footer(n_leaves, truncated, unreadable, failed, skipped))
        out = {"roots": len(roots), "leaves": n_leaves, "bytes_read": n_read, "csv_bytes": cw.n, "json_bytes": jw.n,
               "written_mib": round((cw.n + jw.n) / MIB, 3),
               "limit_mib": round(max_total_bytes / MIB, 3),
               "limit_counts": "the CSV + JSON on disk (the bytes read from the PLC are not counted)",
               "duration_s": round(time.time() - t0, 2),
               "datatype_table": table is not None, "complete": complete, "truncated": bool(truncated),
               "errors": errors[:50], "errors_total": len(errors),
               "largest_roots": sorted(sizes, key=lambda x: -(x["csv_bytes"] + x["json_bytes"]))[:10],
               "order": (f"the project's own programs and GVLs first ({own_listed} of them in this dump), then the rest"
                         if own_roots else "alphabetical — the project's own programs/GVLs are unknown (no project "
                         "configured), so library roots are not put last"),
               "preview": preview}
        if failed:
            out["failed_roots"] = failed[:50]
        if truncated or failed:
            out["skipped_roots"] = skipped[:50]
            out["skipped_total"] = len(skipped)
        if truncated:
            out["truncated_reason"] = truncated
        if unreadable:
            out["unreadable_leaves"] = unreadable[:50]
            out["unreadable_total"] = len(unreadable)
        if not complete:
            out["hint"] = ("the dump is not complete — skipped_roots says what is missing and why (cut by max_leaves / "
                           "max_total_mb, or not readable); raise the limits or dump the rest with pattern="
                           + ("; unreadable_leaves lie outside their symbol (datatype and symbol table disagree — "
                              "online change in progress?): dump again after it" if unreadable else ""))
        return out

    # ------------------------------------------------------------ read / write
    def _typed_read(self, c: pyads.Connection, s: SymbolEntry) -> tuple[T.TcType, Any]:
        """One variable by its handle, the symbol version checked in the same request (_raw_many); bytes for an
        opaque type, else the value as pyads gives it — STRING as the PLC's Windows-1252 text (N91: pyads decoded
        UTF-8 and failed on 'ü'; the dump, the watch and the recorder already used one decoder, _ctype_value)."""
        tt = T.parse_type(s.type, s.size)
        try:
            entries, got = self._raw_many(c, [(s, tt)])
            e = entries[s.name]
            if isinstance(e, str):
                raise AdsError(f"read {s.name!r}: {e}")
            s, tt = e
            err, data = got[s.name]
        except AdsError:
            raise
        except Exception:  # noqa: BLE001 — no sum commands: a fresh handle for this one read
            try:
                data, err = bytes(c.read_by_name(s.name, ctypes.c_ubyte * max(s.size, 1), return_ctypes=True)), 0
            except pyads.ADSError as exc:
                data, err = b"", int(exc.err_code or 1)
            except Exception as exc:  # noqa: BLE001
                raise AdsError(f"read {s.name!r} as {s.type}: {_fmt_exc(exc)}") from exc
        if err:
            raise AdsError(f"read {s.name!r} as {s.type}: {_fmt_exc(pyads.ADSError(err))}")
        if tt.ctype is None or tt.opaque:
            return tt, data
        try:
            return tt, _ctype_value(tt, data)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"read {s.name!r} as {s.type}: {_fmt_exc(exc)}") from exc

    def read(self, name: str, expand_struct: bool = True, max_members: int = 200,
             port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        s = self.symbol_info(name, port)
        tt0 = T.parse_type(s.type, s.size)
        el_name = tt0.elem.name if tt0.kind == "array" and tt0.elem is not None else s.type
        if (not T.is_elementary(tt0) and (expand_struct or tt0.ctype is not None)
                and not (tt0.ctype is not None and self.is_interface_type(el_name, port))):
            # N50 / D5: everything but an elementary type is decoded by the datatype table — one decoder for the read,
            # read_many and the dump (enums with their names, POINTER / REFERENCE aliases as addresses, BOOL as BOOL)
            ts = self.typesystem(False, port)
            if ts is not None and (ts.get(s.type) is not None or tt0.kind == "array"):
                return self.read_full(s.name, None, port)
        tt, val = self._typed_read(c, s)
        out: dict[str, Any] = {"name": s.name, "type": s.type, "size": s.size, "flags": s.flag_names()}
        if tt.kind == "enum" and not isinstance(val, (bytes, bytearray)) and self.is_interface_type(s.type, port):
            out["value"] = T.fmt_pointer(val, s.size)
            out["note"] = "INTERFACE variable: the value is the address of the assigned FB instance (0x0…0 = unassigned)"
            return out
        if tt.kind == "array" and tt.elem is not None and tt.elem.kind == "enum" and isinstance(val, (list, tuple)) \
                and self.is_interface_type(tt.elem.name, port):
            out["value"] = [T.fmt_pointer(v, tt.elem.size) for v in val]
            out["note"] = "ARRAY OF INTERFACE: each value is the address of the assigned FB instance (0x0…0 = unassigned)"
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
                    out["decoder"] = "members"        # D5: which path decoded it (datatype-table / scalar / members)
                else:
                    out["value"] = None
                    out["note"] = "opaque type with no published members; see raw_hex"
            else:
                out["value"] = None
        else:
            out["value"] = T.decode(tt, val)
            out["decoder"] = "scalar"
            if tt.guessed:
                out["note"] = (f"type {s.type} is not in the datatype table: read as a signed integer of its "
                               f"{s.size} bytes, which it may not be")
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
        syms: dict[str, SymbolEntry] = {}
        results: dict[str, Any] = {}
        for n in names:
            try:
                s = self.symbol_info(n, port)
            except AdsError as exc:
                results[n] = {"error": str(exc)}
                continue
            tt = T.parse_type(s.type, s.size)
            if T.is_elementary(tt) and tt.ctype is not None and not tt.opaque and tt.kind not in ("string", "wstring"):
                simple[s.name] = tt
                syms[s.name] = s
            else:                              # the datatype table decodes it, as ads_read does (N50 / D5)
                try:
                    results[n] = self.read(n, True, 50, port)["value"]
                except AdsError as exc:
                    results[n] = {"error": str(exc)}
        if simple:
            results.update(self.read_values(c, [(syms[n], tt) for n, tt in simple.items()]))
        return results

    # ------------------------------------------------------------ access by name: handles + symbol version
    # A variable is reached through a symbol handle (ADSIGRP_SYM_HNDBYNAME), never through an index group/offset kept
    # from an earlier table: a handle names the variable, so a download or online change that moves it cannot make an
    # access land on another variable — the runtime answers for the variable, or refuses the handle (0x710/0x711).
    # Every sum read also reads the symbol version (0xF008) in the SAME request: values that belong to another
    # version than the handles (and the symbol table whose types decode them) are never used — the table and the
    # handles are renewed and the read is made again. pyads' read_list_by_name kept each name's address for the life
    # of the connection, and a 2 s check of the table left a window: both read another variable's bytes with no error.
    def _handle_cache(self, c: Any) -> dict[str, Any]:
        hc = self._handles.get(c)
        if hc is None:
            hc = {"ver": None, "h": {}}
            self._handles[c] = hc
        return hc

    def _drop_handles(self, c: Any) -> None:
        """Forget (and release, best effort) every handle of this connection."""
        hc = self._handles.pop(c, None)
        if not hc or not hc["h"]:
            return
        hs = list(hc["h"].values())
        try:
            for k in range(0, len(hs), _SUM_MAX):
                part = hs[k:k + _SUM_MAX]
                payload = b"".join(struct.pack("<III", 0xF006, 0, 4) for _h in part) + b"".join(
                    struct.pack("<I", h) for h in part)
                c.read_write(0xF081, len(part), ctypes.c_uint32 * len(part), (ctypes.c_ubyte * len(payload))(*payload),
                             ctypes.c_ubyte * len(payload), return_ctypes=True)
        except Exception:  # noqa: BLE001 — handles of a runtime that restarted are gone anyway
            pass

    def _get_handles(self, c: Any, names: list[str], _depth: int = 0) -> tuple[int | None, dict[str, int], dict[str, int]]:
        """(symbol version, {name: handle}, {name: ADS error}) — missing handles made in ONE sum read-write with the
        symbol version read in the same request; all cached handles belong to one version (another version: all are
        made again)."""
        hc = self._handle_cache(c)
        errors: dict[str, int] = {}
        want = [n for n in dict.fromkeys(names) if n not in hc["h"]]
        if want:
            got = _sum_readwrite(c, [(ADSIGRP_SYM_VERSION, 0, 1, b"")]
                                 + [(C.ADSIGRP_SYM_HNDBYNAME, 0, 4, n.encode("latin-1", "replace") + b"\x00")
                                    for n in want])
            verr, vb = got[0]
            ver = vb[0] if not verr and vb else None
            if hc["h"] and ver != hc["ver"] and _depth < 3:
                fresh = [(n, (e, d)) for n, (e, d) in zip(want, got[1:]) if not e]
                self._drop_handles(c)                    # the old handles are of another program version
                hc = self._handle_cache(c)
                for n, (_e, d) in fresh:
                    hc["h"][n] = struct.unpack_from("<I", d, 0)[0]
                hc["ver"] = ver
                return self._get_handles(c, names, _depth + 1)
            hc["ver"] = ver
            for n, (err, data) in zip(want, got[1:]):
                if err or len(data) < 4:
                    errors[n] = err or 0x705
                else:
                    hc["h"][n] = struct.unpack_from("<I", data, 0)[0]
        return hc["ver"], {n: hc["h"][n] for n in names if n in hc["h"]}, errors

    def _table_version(self, c: Any) -> int | None:
        """The symbol version of the symbol table this server decodes with (None: not readable / not loaded)."""
        hit = self._symcache.get((c.ams_net_id, c.ams_net_port))
        ident = hit[3] if hit and len(hit) > 3 else None
        return ident[0] if ident else None

    def _reresolve(self, c: Any, names: list[str]) -> dict[str, tuple["SymbolEntry", T.TcType] | str]:
        """Each name in a freshly loaded symbol table: (entry, type), or why not."""
        self.__dict__.setdefault("_symver", {}).pop((c.ams_net_id, c.ams_net_port), None)
        _lst, idx = self.symbols_cached(True, c.ams_net_port)
        out: dict[str, tuple[SymbolEntry, T.TcType] | str] = {}
        for n in names:
            s = idx.get(n.lower())
            out[n] = (s, T.parse_type(s.type, s.size)) if s is not None else "no longer in the PLC program"
        return out

    def _raw_many(self, c: Any, items: list[tuple["SymbolEntry", T.TcType]], expect_version: int | None = None
                  ) -> tuple[dict[str, tuple["SymbolEntry", T.TcType] | str], dict[str, tuple[int, bytes]]]:
        """The bytes of each variable by its handle, the symbol version read in the same sum request. Values of
        another version than the handles / the symbol table are never returned: renewed and read again (at most
        twice more). expect_version (a recording of one program, R43): values read with another version raise
        ProgramChanged instead of being renewed. -> ({name: (entry, type) it decodes with, or why not},
        {name: (ADS error, bytes)})."""
        names = [s.name for s, _t in items]
        entries: dict[str, tuple[SymbolEntry, T.TcType] | str] = {s.name: (s, t) for s, t in items}
        for attempt in range(3):
            live = [n for n in names if isinstance(entries[n], tuple)]
            ver, hs, herr = self._get_handles(c, live)
            got = _sum_read(c, [(ADSIGRP_SYM_VERSION, 0, 1)]
                            + [(0xF005, hs[n], max(entries[n][0].size, 1)) for n in live if n in hs])  # type: ignore[index]
            verr, vb = got[0]
            now = vb[0] if not verr and vb else None
            if expect_version is not None and now != expect_version:
                raise ProgramChanged(expect_version, now)
            codes = dict(zip([n for n in live if n in hs], got[1:]))
            tv = self._table_version(c)
            changed = ((ver is not None and now != ver) or (tv is not None and now is not None and tv != now)
                       or any(e in _HANDLE_GONE for e, _d in codes.values())
                       # a handle refused when made: the program changed — unless the table was just renewed and the
                       # name is still in it (then the runtime simply does not resolve it: that item's error)
                       or (attempt == 0 and any(e in _HANDLE_GONE for e in herr.values())))
            if changed:
                self._drop_handles(c)
                entries = {**entries, **self._reresolve(c, names)}
                continue
            out = {n: codes[n] if n in codes else (herr.get(n, 0x710), b"") for n in live}
            return entries, out
        raise AdsError("the PLC program changed while reading (download / online change in progress) — read again")

    def read_values(self, c: pyads.Connection, items: list[tuple["SymbolEntry", T.TcType]],
                    decode: bool = True, expect_version: int | None = None) -> dict[str, Any]:
        """Values of simple symbols in ONE sum read by their handles, the symbol version read in the same request
        (_raw_many: a value of another program version is never decoded). A runtime without sum commands: a fresh
        handle per symbol (pyads read by name). An item the runtime refuses is {"error": …}, never decoded.
        decode=False: the raw value (as pyads gives it) for callers that decode themselves (the recorder).
        expect_version (R43): every value is of that program version, or ProgramChanged is raised — without sum
        commands the version is read before and after the reads (it only ever rises).
        -> {symbol name: value}."""
        out: dict[str, Any] = {}
        if not items:
            return out
        try:
            entries, got = self._raw_many(c, items, expect_version)
        except AdsError:
            raise
        except Exception:  # noqa: BLE001 — no sum commands on this runtime (ADS 1793 / 1794)
            entries, got = {s.name: (s, t) for s, t in items}, None
        if got is None and expect_version is not None:
            self._expect_version(c, expect_version)
        try:
            return self._values_out(c, items, entries, got, decode, out)
        finally:
            if got is None and expect_version is not None:
                self._expect_version(c, expect_version)

    @staticmethod
    def _expect_version(c: Any, want: int) -> None:
        try:
            now = int(c.read(ADSIGRP_SYM_VERSION, 0, ctypes.c_ubyte))
        except Exception:  # noqa: BLE001
            now = None
        if now != want:
            raise ProgramChanged(want, now)

    def _values_out(self, c: Any, items: list[tuple["SymbolEntry", T.TcType]], entries: dict[str, Any],
                    got: dict[str, tuple[int, bytes]] | None, decode: bool, out: dict[str, Any]) -> dict[str, Any]:
        for s0, _t0 in items:
            e = entries.get(s0.name)
            if isinstance(e, str):
                out[s0.name] = {"error": f"{s0.name}: {e}"}
                continue
            s, tt = e  # type: ignore[misc]
            if got is not None:
                err, data = got[s.name]
            else:
                try:
                    # pyads by name with a type: a fresh handle for this one read (never a kept address)
                    data = bytes(c.read_by_name(s.name, ctypes.c_ubyte * max(s.size, 1), return_ctypes=True))
                    err = 0
                except pyads.ADSError as exc:
                    err, data = int(exc.err_code or 1), b""
                except Exception as exc:  # noqa: BLE001
                    out[s.name] = {"error": _fmt_exc(exc)}
                    continue
            if err:
                out[s.name] = {"error": _fmt_exc(pyads.ADSError(err))}
                continue
            try:
                raw = _ctype_value(tt, data)
                out[s.name] = T.decode(tt, raw) if decode else raw
            except Exception as exc:  # noqa: BLE001
                out[s.name] = {"error": f"not decodable as {s.type}: {type(exc).__name__}: {exc}"}
        return out

    # set by the server: True when the project source declares this name as an INTERFACE — the runtime's datatype
    # table does not always mark interface types recognisably (field retest #52/#64)
    interface_hint: Callable[[str, int | None], bool] | None = None
    # set by the server: the base the project source declares for an alias type ('PT_X' -> 'POINTER TO FB_X') — the
    # 4026 datatype table gives an alias of a pointer no base type (N50)
    alias_hint: Callable[[str, int | None], str | None] | None = None

    def is_interface_type(self, type_name: str, port: int | None = None) -> bool:
        try:
            ts = self.typesystem(False, port)
        except Exception:  # noqa: BLE001
            ts = None
        if ts is not None and _is_iface_name(ts, type_name):
            return True
        if self.interface_hint is not None:
            try:
                return bool(self.interface_hint(type_name, port or self.port))
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
        if self.interface_hint is not None and self.interface_hint(base, port or self.port):
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
                # the dump's rule (is_indirect: the alias chain followed) — TYPE PT_X : POINTER TO FB_X, or an alias of
                # an interface, is as much an address as the type it names; only the entry itself was checked
                kind = ts.indirect_kind(e, base)
                alias = bool(e.type) and e.type.strip().lower() != base.strip().lower() and not (e.subitems or e.dims)
                via = f" (an alias of {e.type})" if alias else ""
                if kind == "tccom interface":
                    return f"{base}{via} is a TcCOM interface pointer — not writable through ADS"
                if kind == "reference":
                    return f"{base}{via} is a REFERENCE — writing the reference itself is refused; write the referenced variable"
                if kind == "interface":
                    return (f"{base}{via} is an INTERFACE type (pointer to an FB's method table) — writing it would make the PLC "
                            "call a wild pointer; assign interfaces in PLC code, not over ADS")
                if kind == "pointer":
                    return (f"{base}{via} is a POINTER — writing an address is refused; write the variable it points to")
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
    write_guard: Callable[[str, int | None], str | None] | None = None

    def check_write(self, name: str, value: Any, port: int | None = None) -> dict[str, Any]:
        """Everything write() would refuse, without writing: READONLY flag, pointer/reference/interface types,
        source-level CONSTANT, enum names, array length, string length, integer range. Returns the encoded value."""
        if self.write_guard is not None:
            why = self.write_guard(name, port or self.port)
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
            chk = {"symbol": s, "tt": tt, "encoded": enc, "hex": None}
            self._value_bytes(s, tt, chk)              # the exact bytes are known before anything is written
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"write {s.name!r}={value!r} as {s.type}: {_fmt_exc(exc)}") from exc
        return chk

    def write(self, name: str, value: Any, port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        chk = self.check_write(name, value, port)
        s, tt = chk["symbol"], chk["tt"]
        before = self.read(name, expand_struct=False, port=port).get("value")
        if chk["hex"] is not None:
            data = bytes.fromhex(chk["hex"])
            self._write_by_handle(c, s, data)
            return {"name": s.name, "type": s.type, "before": before, "written_hex": chk["hex"]}
        enc = chk["encoded"]
        try:
            data = self._value_bytes(s, tt, chk)       # the PLC's bytes (STRING: Windows-1252, N91)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"write {s.name!r}={value!r} as {s.type}: {_fmt_exc(exc)}") from exc
        self._write_by_handle(c, s, data)
        read_back = self.read(name, expand_struct=False, port=port).get("value")
        written = T.decode(tt, enc)
        if not T.is_elementary(T.parse_type(s.type, s.size)):
            ts = self.typesystem(False, port)
            if ts is not None and ts.get(s.type) is not None:
                written = ts.decode_tree(data, s.type, size=s.size)     # the shape ads_read gives (enum + name)
        out = {"name": s.name, "type": s.type, "before": before, "written": written, "read_back": read_back}
        if not _same_value(written, read_back):
            out["note"] = ("read_back differs from the written value: the PLC changed it after the write (a one-shot it "
                           "clears, or a variable the program assigns every cycle). The write itself was accepted.")
        return out

    def _write_by_handle(self, c: Any, s: "SymbolEntry", data: bytes) -> None:
        """Raw bytes into the variable by its handle — never at an index offset from a table that may be older than
        the program (a handle names the variable; a refused handle is renewed once)."""
        for attempt in range(2):
            _ver, hs, herr = self._get_handles(c, [s.name])
            if s.name not in hs:
                raise AdsError(f"write {s.name!r}: no handle ({_fmt_exc(pyads.ADSError(herr.get(s.name, 0x710)))})")
            try:
                c.write(0xF005, hs[s.name], (ctypes.c_ubyte * len(data))(*data), ctypes.c_ubyte * len(data))
                return
            except pyads.ADSError as exc:
                if attempt == 0 and int(exc.err_code or 0) in _HANDLE_GONE:
                    self._drop_handles(c)
                    continue
                raise AdsError(f"write {s.name!r}: {_fmt_exc(exc)}") from exc

    @staticmethod
    def _value_bytes(s: "SymbolEntry", tt: T.TcType, chk: dict[str, Any]) -> bytes:
        """The exact bytes a write puts into the variable (what a sum-write sends)."""
        if chk.get("hex") is not None:
            return bytes.fromhex(chk["hex"])
        enc = chk["encoded"]
        if tt.kind == "string":
            return T.plc_string_bytes(enc, s.size)
        if tt.kind == "wstring":
            return enc.encode("utf-16-le")[: s.size - 2].ljust(s.size, b"\x00")
        if tt.kind == "array":
            return bytes(tt.ctype(*enc))
        return bytes(tt.ctype(enc))

    def write_many(self, items: dict[str, Any], port: int | None = None) -> dict[str, Any]:
        """Pre-check every item (all refusals raised before anything is written), then ONE ADS sum-write
        (ADSIGRP_SUMUP_WRITE) by the variables' handles: the runtime applies all sub-writes in one request, i.e.
        between the same two task cycles — no cycle sees half of them — and each lands on the variable it names, also
        right after a download / online change (an index offset kept from an older table wrote another variable and
        its read-back at the same offset confirmed it). Falls back to sequential writes where sum commands are
        unsupported."""
        c = self.conn(port)

        def checks_for() -> list[tuple[str, Any, Any, T.TcType, dict[str, Any]]]:
            out_: list[tuple[str, Any, Any, T.TcType, dict[str, Any]]] = []
            problems = {}
            for name, value in items.items():
                try:
                    chk = self.check_write(name, value, port)
                    out_.append((name, value, chk["symbol"], chk["tt"], chk))
                except AdsError as exc:
                    problems[name] = str(exc)
            if problems:
                raise AdsError("nothing was written — " + "; ".join(f"{k}: {v}" for k, v in problems.items()))
            return out_

        checks = checks_for()
        before = self.read_many([s.name for _n, _v, s, _t, _c in checks], port)
        method = "sum-write by handle (one ADS request: all values applied between the same two PLC cycles)"
        codes: list[int] | None = None
        ver_before: int | None = None
        try:
            for attempt in range(2):
                ver_before, hs, herr = self._get_handles(c, [s.name for _n, _v, s, _t, _c in checks])
                tv = self._table_version(c)
                if herr or (tv is not None and ver_before is not None and tv != ver_before):
                    if attempt == 0:                # the program changed: the checks again, on the current table
                        self._drop_handles(c)
                        self._reresolve(c, [s.name for _n, _v, s, _t, _c in checks])
                        checks = checks_for()
                        continue
                    raise AdsError("nothing was written — no handle for " + ", ".join(
                        f"{n} ({_fmt_exc(pyads.ADSError(e))})" for n, e in herr.items()) if herr else
                        "nothing was written — the PLC program changed while preparing the write; write again")
                datas = [self._value_bytes(s, tt, chk) for _n, _v, s, tt, chk in checks]
                payload = (b"".join(struct.pack("<III", 0xF005, hs[s.name], len(d))
                                    for (_n, _v, s, _t, _c), d in zip(checks, datas)) + b"".join(datas))
                res = c.read_write(0xF081, len(checks), ctypes.c_uint32 * len(checks),
                                   (ctypes.c_ubyte * len(payload))(*payload), ctypes.c_ubyte * len(payload),
                                   return_ctypes=True)
                codes = [int(x) for x in res]
                break
        except AdsError:
            raise
        except Exception as exc:  # noqa: BLE001 — sum commands unsupported here: one by one
            method = f"sequential by handle (sum-write unavailable: {_fmt_exc(exc)[:80]})"
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
                    self._write_by_handle(c, s, self._value_bytes(s, tt, chk))
                    applied.append(s.name)
                except Exception as exc:  # noqa: BLE001
                    results[s.name] = {"error": _fmt_exc(exc)}
                    break
        after = self.read_many(applied, port) if applied else {}
        changed_during = (ver_before is not None and self._handle_cache(c)["ver"] not in (None, ver_before))
        for name, value, s, tt, chk in checks:
            if s.name in applied:
                written = T.decode(tt, chk["encoded"]) if chk.get("encoded") is not None else chk.get("hex")
                d = {"before": before.get(s.name), "written": written, "read_back": after.get(s.name)}
                if not _same_value(written, after.get(s.name)):
                    d["note"] = "read_back differs: the PLC changed it after the write"
                results[s.name] = d
        failed = [k for k, v in results.items() if "error" in v]
        out = {"method": method, "complete": not failed, "applied": applied, "failed": failed,
               "not_attempted": [s.name for _n, _v, s, _t, _c in checks if s.name not in applied and s.name not in failed],
               "results": results}
        if changed_during:
            out["warning"] = ("the PLC program changed between the write and its read-back (download / online change): "
                              "each value went to the variable it names, but a type that changed with it was written "
                              "as the old type — check the read-back values")
        return out

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

    def mdp_raw(self, index: int, subindex: int, size: int, net_id: str | None = None, flags: int = 0) -> bytes:
        """One MDP value (IPC diagnostics, 0.8.19) as raw bytes — the ADS address of mdp.py (NOT confirmed on
        hardware: port 10000, index group 0xF302)."""
        from . import mdp as M
        c = self.conn(M.MDP_PORT, net_id)
        io = M.index_offset(index, subindex, flags)
        try:
            raw = c.read(M.MDP_INDEX_GROUP, io, ctypes.c_ubyte * size, return_ctypes=True, check_length=False)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"MDP 0x{index:04X}:{subindex:02d} (ADS index group 0x{M.MDP_INDEX_GROUP:X}, offset "
                           f"0x{io:08X}, port {M.MDP_PORT}): {_fmt_exc(exc)}") from exc
        if raw is None:
            raise AdsError(f"MDP 0x{index:04X}:{subindex:02d}: no answer (the connection is closed)")
        return bytes(raw)

    def ipc_info(self, net_id: str | None = None, types: set[int] | None = None,
                 max_reads: int = 600) -> dict[str, Any]:
        from . import mdp as M
        res = M.read_info(lambda i, s, n: self.mdp_raw(i, s, n, net_id), max_reads, types)
        res["target"] = net_id or self.net_id or "(local)"
        res["ads_address"] = {"port": M.MDP_PORT, "index_group": f"0x{M.MDP_INDEX_GROUP:X}",
                              "index_offset": "index << 16 | subindex",
                              "confirmed": "not yet on hardware (docs/PLAN-0.8.19.md §8)"}
        return res

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
        items: list[tuple[SymbolEntry, T.TcType]] = []
        for n in names:
            s = self.symbol_info(n, port)
            tt = T.parse_type(s.type, s.size)
            if tt.ctype is None or tt.opaque:
                raise AdsError(f"{n!r} is an opaque type; watch its members instead")
            types[s.name] = tt
            items.append((s, tt))
        keys = list(types.keys())
        samples: list[dict[str, Any]] = []
        changes = 0
        last: dict[str, Any] | None = None
        t0 = time.perf_counter()
        deadline = t0 + duration_ms / 1000
        while True:
            now = time.perf_counter()
            # one sum read at the addresses resolved above (read_values) — a refused item is {"error": …} in the sample
            vals = self.read_values(c, items)
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
            got = self.read_values(c, [(s, tt)], decode=False).get(s.name)
            if isinstance(got, dict) and set(got) == {"error"}:
                raise AdsError(f"wait_until {s.name}: {got['error']}")
            cur = got
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
    """Text writer over a binary file that counts the UTF-8 BYTES written (N53; R42: max_total_mb bounds what is
    written, not what is read). csv.writer and the JSON stream write str; one encode per write."""

    def __init__(self, fh: Any):
        self.fh, self.n = fh, 0

    def write(self, s: str) -> int:
        b = s.encode("utf-8")
        self.n += len(b)
        self.fh.write(b)
        return len(s)


class _ReadFailed(Exception):
    pass


class _DumpCancelled(Exception):
    pass


class _OutsideRoot(_ReadFailed):
    """A leaf the datatype table places past the end of the symbol (the two tables disagree — an online change in
    progress, a library mismatch). Reading it would decode the next variable's bytes as this leaf's value."""


class _RootReader:
    """The bytes of one root symbol, read in bounded chunks only where a leaf needs them (N39/N47/N55): a root up to
    one chunk is read in one request as before; a bigger one is never read whole, never held whole in RAM, and the
    cancel event is checked before every request. The symbol's index offset is linear (PLC memory areas)."""

    def __init__(self, conn: Any, sym: "SymbolEntry", cancelled: Callable[[], bool], chunk: int = _DUMP_CHUNK,
                 need_end: int | None = None, expect_version: int | None = None):
        self.c, self.s, self.cancelled, self.chunk = conn, sym, cancelled, chunk
        self.size = max(sym.size, 1)
        self.end = min(self.size, need_end) if need_end else self.size      # nothing beyond this is ever needed
        self.w0, self.buf = 0, b""
        self.bytes_read = 0
        # the symbol version of the table these offsets come from: every chunk is read with the version in the same
        # sum request, and bytes of another program version are never decoded (an online change during the dump)
        self.expect_version = expect_version

    def get(self, off: int, n: int) -> bytes:
        if off < 0 or off + n > self.size:
            raise _OutsideRoot(f"bytes {off}..{off + n} lie outside {self.s.name} ({self.s.size} bytes): the datatype "
                               "table and the symbol table disagree (online change in progress?) — not read")
        if self.w0 <= off and off + n <= self.w0 + len(self.buf):
            return self.buf[off - self.w0:off - self.w0 + n]
        if self.end <= self.chunk:
            start, length = 0, max(self.end, off + n)
        else:
            start, length = off, max(min(self.chunk, self.end - off), n)
        if self.cancelled():
            raise _DumpCancelled()
        where = f" (bytes {start}..{start + length} of {self.size})" if self.size > self.chunk else ""
        data: bytes | None = None
        if self.expect_version is not None:
            try:
                (verr, vb), (err, chunk) = _sum_read(self.c, [(ADSIGRP_SYM_VERSION, 0, 1),
                                                              (self.s.index_group, self.s.index_offset + start, length)])
            except Exception:  # noqa: BLE001 — no sum commands: plain reads from here on
                self.expect_version = None
            else:
                now = vb[0] if not verr and vb else None
                if now is not None and now != self.expect_version:
                    raise _ReadFailed(f"the PLC program changed during the dump (symbol version {self.expect_version} → "
                                      f"{now}: download / online change) — not read; dump again" + where)
                if err:
                    raise _ReadFailed(_fmt_exc(pyads.ADSError(err)) + where)
                data = chunk
        if data is None:
            try:
                data = bytes(self.c.read(self.s.index_group, self.s.index_offset + start, ctypes.c_ubyte * max(length, 1),
                                         return_ctypes=True, check_length=False))
            except Exception as exc:  # noqa: BLE001
                raise _ReadFailed(_fmt_exc(exc) + where) from exc
        self.w0, self.buf = start, data
        self.bytes_read += len(data)
        return data[off - start:off - start + n]

    def covers(self, lo: int, hi: int) -> bool:
        """Are the bytes lo..hi (what the leaves still to come need) already in hand? Then no further request — so no
        read error — can follow (N76). Nothing needed (lo >= hi): yes."""
        return lo >= hi or (self.w0 <= lo and hi <= self.w0 + len(self.buf))


def _at_zero(leaf: Leaf) -> Leaf:
    """The same leaf with offset 0: _RootReader.get returns exactly the leaf's bytes."""
    import dataclasses
    return dataclasses.replace(leaf, offset=0)


def _leaf_note(leaf: Leaf, val: Any) -> str:
    """Why a CSV cell is what it is, in its own column (N51): an empty value never stands for two things."""
    if leaf.not_expanded:
        return leaf.note if leaf.note and "not expanded" in leaf.note else f"not expanded: {leaf.size} bytes"
    if leaf.size == 0:
        return "no storage (size 0)"
    if val is None:
        return "not decodable from the bytes read"
    return leaf.note or ""


_SEG = re.compile(r"\.([^.\[\]]+)|\[([^\]]+)\]")


def _nest(root: str, vals: dict[str, Any]) -> Any:
    """The JSON tree of one root built from its leaf values (N48/N50: CSV and JSON come from the same decode):
    '.name' segments become object keys, '[i]' / '[i,j]' indices become (nested) lists in index order — the shape
    decode_tree gives for a complete root."""
    if list(vals) == [root]:
        return vals[root]
    tree: dict[Any, Any] = {}
    for path, v in vals.items():
        rest = path[len(root):] if path.startswith(root) else path
        segs: list[Any] = []
        for m in _SEG.finditer(rest):
            if m.group(1) is not None:
                segs.append(m.group(1))
            else:
                try:
                    segs.append(tuple(int(x) for x in m.group(2).split(",")))
                except ValueError:
                    segs.append(m.group(2))
        if not segs:
            tree[""] = v
            continue
        node = tree
        for sg in segs[:-1]:
            node = node.setdefault(sg, {})
            if not isinstance(node, dict):
                break
        else:
            node[segs[-1]] = v

    def fin(n: Any) -> Any:
        if not isinstance(n, dict):
            return n
        if n and all(isinstance(k, tuple) for k in n):
            dims = len(next(iter(n)))
            if all(len(k) == dims for k in n):
                if dims == 1:
                    return [fin(n[k]) for k in sorted(n)]
                groups: dict[int, dict[tuple, Any]] = {}
                for k, x in n.items():
                    groups.setdefault(k[0], {})[k[1:]] = x
                return [fin(groups[g]) for g in sorted(groups)]
        return {(k if isinstance(k, str) else str(list(k))): fin(x) for k, x in n.items()}
    return fin(tree)


class _TreeOrder(Exception):
    """A leaf that does not continue the tree depth first (a type walk never yields one)."""


def _tree_order_problem(root: str, paths: list[str]) -> str | None:
    """Do these leaf paths continue one tree depth first, as _TreeStream needs? None, or the first that does not."""
    probe = _TreeStream(root)
    for p in paths:
        st = probe.steps(p)
        try:
            probe.plan(st, "0")
        except _TreeOrder as exc:
            return f"{p}: {exc}"
        probe.apply(st, "0")
        probe.parts.clear()
    return None


class _TreeStream:
    """N76: the JSON of one root, produced as its leaves arrive — the same text as _json_strict(_nest(root, vals))
    ('.name' → object member, '[i]' / '[i,j]' → nested lists in index order) without building the tree, so the
    exact number of bytes the root will add is known BEFORE a leaf is accepted. plan() prices a leaf without changing
    anything, apply() writes it. A type walk yields the leaves depth first (every struct and array contiguous,
    indices ascending); a leaf that does not continue the tree that way raises _TreeOrder and is never written in the
    wrong place. JSON text is ASCII (json.dumps), so its length is its size in bytes."""

    def __init__(self, root: str):
        import json as _json
        self._dumps = _json.dumps
        self.root = root
        self.parts: list[str] = []
        self.n = 0                       # bytes in parts
        # open containers, outermost first: [kind 'o' | 'a', the step that opened it, used keys | last index, members]
        self.frames: list[list[Any]] = []
        self.scalar: str | None = None   # the root's own value (a root that is one leaf)
        self.leaves = 0

    def steps(self, path: str) -> list[tuple[str, Any]]:
        rest = path[len(self.root):] if path.startswith(self.root) else path
        out: list[tuple[str, Any]] = []
        for m in _SEG.finditer(rest):
            if m.group(1) is not None:
                out.append(("o", m.group(1)))
                continue
            try:
                idx = tuple(int(x) for x in m.group(2).split(","))
            except ValueError:
                out.append(("o", m.group(2)))        # a non-numeric index is a member name in _nest too
            else:
                out.extend(("a", i) for i in idx)    # '[i,j]': a list of lists
        return out

    def _common(self, st: list[tuple[str, Any]]) -> int:
        p = 0
        while p + 1 < len(self.frames) and p < len(st) - 1 and self.frames[p + 1][1] == st[p]:
            p += 1
        return p

    def _convert(self) -> None:
        """The root's own value was not its only leaf: it becomes the member "" of the root object (as in _nest)."""
        s, self.scalar = self.scalar or "", None
        self.frames = [["o", None, {""}, 1]]
        self.parts, self.n = ["{", '"": ', s], 5 + len(s)

    def plan(self, st: list[tuple[str, Any]], vjson: str) -> int:
        """Bytes this leaf adds to parts (closers of the containers it leaves, openers, separators, keys, value)."""
        if not st:
            if not self.leaves:
                return len(vjson)                           # the root's own value (alone: the root is that value)
            st = [("o", "")]
        if self.scalar is not None:
            if st[0][0] != "o":
                raise _TreeOrder("list elements next to the root's own value")
            tmp = _TreeStream(self.root)
            tmp.scalar, tmp.leaves = self.scalar, self.leaves
            tmp._convert()
            return (tmp.n - self.n) + tmp.plan(st, vjson)
        cost = 0
        if not self.frames:
            cost += 1                                       # the root's '{' / '['
            p = 0
        else:
            if self.frames[0][0] != st[0][0]:
                raise _TreeOrder(f"{st[0]} in a {'list' if self.frames[0][0] == 'a' else 'struct'}")
            p = self._common(st)
            cost += len(self.frames) - 1 - p                # closers of the containers left
            f = self.frames[p]
            kind, key = st[p]
            if kind != f[0] or (key in f[2] if kind == "o" else (f[2] is not None and key <= f[2])):
                raise _TreeOrder(f"{kind}:{key} repeats or goes back in its container")
        for d in range(p, len(st)):
            kind, key = st[d]
            members = self.frames[p][3] if (d == p and self.frames) else 0
            cost += (2 if members else 0) + (len(self._dumps(key)) + 2 if kind == "o" else 0)
            cost += 1 if d < len(st) - 1 else len(vjson)
        return cost

    def apply(self, st: list[tuple[str, Any]], vjson: str) -> None:
        def w(s: str) -> None:
            self.parts.append(s)
            self.n += len(s)
        if not st:
            if not self.leaves:
                self.scalar, self.leaves = vjson, 1
                return
            st = [("o", "")]
        if self.scalar is not None:
            self._convert()
        self.leaves += 1
        if not self.frames:
            self.frames.append([st[0][0], None, set() if st[0][0] == "o" else None, 0])
            w("{" if st[0][0] == "o" else "[")
            p = 0
        else:
            p = self._common(st)
            while len(self.frames) > p + 1:
                w("}" if self.frames.pop()[0] == "o" else "]")
        for d in range(p, len(st)):
            kind, key = st[d]
            f = self.frames[d]
            if f[3]:
                w(", ")
            if kind == "o":
                w(self._dumps(key) + ": ")
                f[2].add(key)
            else:
                f[2] = key
            f[3] += 1
            if d < len(st) - 1:
                nk = st[d + 1][0]
                self.frames.append([nk, st[d], set() if nk == "o" else None, 0])
                w("{" if nk == "o" else "[")
            else:
                w(vjson)

    def size_after(self, cost: int, st: list[tuple[str, Any]]) -> int:
        """Bytes of the finished tree if this (priced) leaf were the last one."""
        if not st:
            return self.n + cost + (1 if self.leaves else 0)
        return self.n + cost + len(st)          # one closer per container still open after it

    def text(self) -> str:
        if self.scalar is not None:
            return self.scalar
        if not self.frames:
            return "{}"
        return "".join(self.parts) + "".join("}" if f[0] == "o" else "]" for f in reversed(self.frames))


# N76: an error text written into the dump files is at most this long JSON-escaped (ASCII, no control characters —
# and so at most this long in the CSV too): the procedure's own bound, which gives a read error's ending a known size
_ERR_JSON_MAX = 300


def _err_text(msg: Any) -> str:
    import json
    s = "".join(ch for ch in " ".join(str(msg).split()) if ch >= " ")
    s = s.encode("ascii", "backslashreplace").decode("ascii")
    if len(json.dumps(s)) <= _ERR_JSON_MAX:
        return s
    while s and len(json.dumps(s + "...")) > _ERR_JSON_MAX:
        s = s[:-1]
    return s + "..."


_SUM_MAX = 500                    # sub-commands per ADS sum request (Beckhoff's recommendation, pyads' MAX_ADS_SUB_COMMANDS)


def _sum_read(c: Any, items: list[tuple[int, int, int]]) -> list[tuple[int, bytes]]:
    """ADSIGRP_SUMUP_READ (0xF080) over (index group, index offset, length), _SUM_MAX per request. The answer holds an
    error code per item, then each item's bytes (its full length, also for an item that failed).
    -> [(ADS error code, bytes)] in the order asked."""
    out: list[tuple[int, bytes]] = []
    for k in range(0, len(items), _SUM_MAX):
        part = items[k:k + _SUM_MAX]
        req = b"".join(struct.pack("<III", g, o, max(n, 1)) for g, o, n in part)
        rlen = 4 * len(part) + sum(max(n, 1) for _g, _o, n in part)
        raw = bytes(c.read_write(0xF080, len(part), ctypes.c_ubyte * rlen, (ctypes.c_ubyte * len(req))(*req),
                                 ctypes.c_ubyte * len(req), return_ctypes=True))
        pos = 4 * len(part)
        for j, (_g, _o, n) in enumerate(part):
            out.append((struct.unpack_from("<I", raw, 4 * j)[0], raw[pos:pos + max(n, 1)]))
            pos += max(n, 1)
    return out


ADSIGRP_SYM_VERSION = 0xF008      # the runtime's symbol version: +1 on every download / online change
_HANDLE_GONE = frozenset({0x710, 0x711})   # symbol not found / symbol version invalid: the program changed


def _sum_readwrite(c: Any, items: list[tuple[int, int, int, bytes]]) -> list[tuple[int, bytes]]:
    """ADSIGRP_SUMUP_READWRITE (0xF082) over (index group, index offset, read length, write data), _SUM_MAX per request.
    The answer holds an error code and the returned length per item, then each item's returned bytes.
    -> [(ADS error code, bytes)] in the order asked."""
    out: list[tuple[int, bytes]] = []
    for k in range(0, len(items), _SUM_MAX):
        part = items[k:k + _SUM_MAX]
        req = (b"".join(struct.pack("<IIII", g, o, rl, len(wd)) for g, o, rl, wd in part)
               + b"".join(wd for _g, _o, _rl, wd in part))
        rlen = 8 * len(part) + sum(rl for _g, _o, rl, _wd in part)
        raw = bytes(c.read_write(0xF082, len(part), ctypes.c_ubyte * rlen, (ctypes.c_ubyte * len(req))(*req),
                                 ctypes.c_ubyte * len(req), return_ctypes=True, check_length=False))
        pos = 8 * len(part)
        for j in range(len(part)):
            err, n = struct.unpack_from("<II", raw, 8 * j)
            out.append((err, raw[pos:pos + n]))
            pos += n
    return out


def _ctype_value(tt: T.TcType, data: bytes) -> Any:
    """The raw value of these bytes, as pyads' read_by_name gives it (get_value_from_ctype_data); STRING is the PLC's
    Windows-1252 text up to its NUL, WSTRING UTF-16 up to its double NUL."""
    if tt.kind == "string":
        return T.plc_string(data)
    if tt.kind == "wstring":
        b = bytes(data)
        end = next((i for i in range(0, len(b) - 1, 2) if b[i] == 0 and b[i + 1] == 0), len(b) - len(b) % 2)
        return b[:end].decode("utf-16-le", "replace")
    n = ctypes.sizeof(tt.ctype)
    obj = tt.ctype.from_buffer_copy(bytes(data[:n]).ljust(n, b"\x00"))
    return pyads.pyads_ex.get_value_from_ctype_data(obj, tt.ctype)


def _move_new(src: Any, dst: Any) -> None:
    """Move src to dst, never over an existing dst (FileExistsError): os.replace overwrote a file that appeared
    between the check and the move. Windows: rename refuses an existing target. POSIX: a hard link refuses it, then the
    old name goes; a file system without hard links falls back to check-then-rename (the narrow window again)."""
    import errno
    if os.name == "nt":
        os.rename(src, dst)
        return
    try:
        os.link(src, dst)
    except FileExistsError:
        raise
    except OSError:
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "file exists", str(dst)) from None
        os.rename(src, dst)
        return
    try:
        os.unlink(src)
    except OSError:
        try:
            os.unlink(dst)                      # undo: never two names for one file, never a half-done move
        except OSError as exc2:
            raise OSError(f"{dst} was linked but {src} could not be removed, nor the link undone ({exc2})") from None
        raise


def _reserve_dump_names(out_dir: str) -> tuple[Any, Any, Any, Any]:
    """dump-<stamp>[-n].json/.csv names no file in out_dir has, reserved by creating both .partial files exclusively —
    two dumps in the same second (or from two servers) got the same names, and the second replaced the first's files.
    Caller holds _dump_lock (within this process; O_EXCL covers other processes)."""
    from pathlib import Path
    out = Path(out_dir)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1, 1000):
        base = f"dump-{stamp}" + ("" if n == 1 else f"-{n}")
        jpath, cpath = out / f"{base}.json", out / f"{base}.csv"
        jpart, cpart = out / f"{base}.json.partial", out / f"{base}.csv.partial"
        if jpath.exists() or cpath.exists():
            continue
        made = []
        try:
            for p in (cpart, jpart):
                with open(p, "x"):
                    made.append(p)
        except FileExistsError:
            for p in made:
                try:
                    p.unlink()
                except OSError:
                    pass
            continue
        return jpath, cpath, jpart, cpart
    raise AdsError(f"ads_dump: no free file name dump-{stamp}[-n] in {out_dir}")


def _dump_order(roots: list[SymbolEntry], own: set[str] | None) -> list[SymbolEntry]:
    """R42 #5 / N54: the program's own roots (PROGRAM / GVL names from the project source) first, so a budget is
    spent on what the user wrote. Without the project nothing is known about origin: no guessing from names
    (Param_*, Constants* …) — alphabetical."""
    if not own:
        return sorted(roots, key=lambda s: s.name.lower())

    def rank(s: SymbolEntry) -> int:
        return 0 if s.name.split(".")[0].split("[")[0].lower() in own else 1
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
