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
from typing import Any

import pyads
from pyads import constants as C
from pyads.structs import AmsAddr

from .config import AdsConfig
from . import types_tc as T

SYSTEM_SERVICE_PORT = 10000
ROUTER_PORT = 1

SYMFLAG = {
    0x0001: "PERSISTENT", 0x0002: "BITVALUE", 0x0004: "REFERENCETO", 0x0008: "TYPEGUID",
    0x0010: "TCCOMIFACEPTR", 0x0020: "READONLY", 0x0040: "ITFMETHODACCESS",
    0x0080: "METHODDEREF", 0x1000: "ATTRIBUTES", 0x2000: "STATIC", 0x4000: "INITONRESET",
    0x8000: "EXTENDEDFLAGS",
}


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

    def set_system_mode(self, action: str) -> dict[str, Any]:
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
        # the system service drops connections while switching
        self.disconnect()
        return {"before": before, "requested": action,
                "note": "TwinCAT restarts; call ads_system_state after a few seconds."}

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

    def symbols_cached(self, refresh: bool = False, port: int | None = None) -> tuple[list[SymbolEntry], dict[str, SymbolEntry]]:
        c = self.conn(port)
        key = (c.ams_net_id, c.ams_net_port)
        hit = self._symcache.get(key)
        if hit and not refresh and time.time() - hit[0] < self.cfg.symbol_cache_ttl_s:
            return hit[1], hit[2]
        lst, idx = self._load_symbols(c)
        self._symcache[key] = (time.time(), lst, idx)
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
        tt, val = self._typed_read(c, s)
        out: dict[str, Any] = {"name": s.name, "type": s.type, "size": s.size, "flags": s.flag_names()}
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

    def write(self, name: str, value: Any, port: int | None = None) -> dict[str, Any]:
        c = self.conn(port)
        s = self.symbol_info(name, port)
        if s.flags & 0x0020:
            raise AdsError(f"{s.name!r} is flagged READONLY by the runtime")
        tt = T.parse_type(s.type, s.size)
        before = self.read(name, expand_struct=False, port=port).get("value")
        if tt.ctype is None or tt.opaque:
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]*", value) and len(value) == s.size * 2:
                data = bytes.fromhex(value)
                c.write(s.index_group, s.index_offset, (ctypes.c_ubyte * len(data))(*data), ctypes.c_ubyte * len(data))
                return {"name": s.name, "type": s.type, "before": before, "written_hex": value}
            raise AdsError(
                f"{s.name!r} has opaque type {s.type}; write its members individually "
                f"(see ads_read with expand_struct) or pass a {s.size*2}-char hex string.")
        try:
            enc = T.encode(tt, value)
            c.write_by_name(s.name, enc, tt.ctype)
        except AdsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"write {s.name!r}={value!r} as {s.type}: {_fmt_exc(exc)}") from exc
        after = self.read(name, expand_struct=False, port=port).get("value")
        return {"name": s.name, "type": s.type, "before": before, "after": after}

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

    def local_net_id(self) -> str:
        pyads.open_port()
        try:
            return pyads.get_local_address().netid  # type: ignore[union-attr]
        finally:
            pyads.close_port()


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


def _fmt_exc(exc: BaseException) -> str:
    if isinstance(exc, pyads.ADSError):
        return f"ADSError {exc.err_code} (0x{exc.err_code:X}): {exc.msg}"
    return f"{type(exc).__name__}: {exc}"
