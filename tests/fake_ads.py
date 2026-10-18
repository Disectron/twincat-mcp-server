"""A fake pyads.Connection good enough to exercise AdsBackend/SystemServiceFiles."""
from __future__ import annotations

import ctypes
import threading
import struct
from typing import Any

import pyads
from pyads import constants as C

from pyads.structs import SAdsNotificationHeader

from twincat_mcp.ads_types import DtEntry, encode_entry


def _entry(name: str, typ: str, size: int, igrp: int, ioffs: int, flags: int = 0, comment: str = "") -> bytes:
    n, t, c = name.encode(), typ.encode(), comment.encode()
    body = struct.pack("<HHH", len(n), len(t), len(c)) + n + b"\x00" + t + b"\x00" + c + b"\x00"
    entry_len = 24 + len(body)
    # pad to 4
    pad = (-entry_len) % 4
    entry_len += pad
    return struct.pack("<IIIIII", entry_len, igrp, ioffs, size, 0, flags) + body + b"\x00" * pad


SYMBOLS: list[tuple[str, str, int, Any, int]] = [
    # name, type, size, value, flags
    ("MAIN.nCycle", "UDINT", 4, 42, 0),
    ("MAIN.bStart", "BOOL", 1, True, 0),
    ("MAIN.fbFill.tSettle", "TIME", 4, 1500, 0),
    ("MAIN.fbFill.eState", "E_FillState", 2, 1, 0),
    ("MAIN.fbFill.arrWeights", "ARRAY [0..3] OF LREAL", 32, [1.0, 2.5, 0.0, -1.0], 0),
    ("MAIN.fbFill.bDone", "BOOL", 1, False, 0),
    ("MAIN.stRecipe", "ST_Recipe", 57, None, 0),
    ("MAIN.stRecipe.rTargetKg", "LREAL", 8, 180.5, 0),
    ("MAIN.stRecipe.rBulkCutoff", "LREAL", 8, 0.9, 0),
    ("MAIN.stRecipe.sName", "STRING(40)", 41, "Vanilla", 0),
    ("GVL_Machine.nDrumsFilled", "UDINT", 4, 7, 0),
    ("Constants.CompilerVersion", "VERSION", 8, None, 0x20),
    ("MAIN.sLabel", "STRING(10)", 11, "hello", 0),
    ("MAIN.arrSteps", "ARRAY [1..2] OF ST_Step", 32, None, 0),
    ("MAIN.fbValve2", "FB_Valve", 16, None, 0),
    ("PRG_Test.ipModule", "I_Module", 8, 0, 0),                      # interface variable: a pointer the PLC calls through
    ("PRG_Test.pData", "POINTER TO INT", 8, 0, 0),
    ("PRG_Test.abStateLit", "ARRAY [0..7] OF BOOL", 8, [False, False, False, False, False, True, False, False], 0),
    ("GVL_Machine.MAX_MODULES", "INT", 2, 16, 0),               # VAR_GLOBAL CONSTANT in the sample project; no READONLY flag
    ("PRG_Test.eCmd", "E_FillState", 2, 0, 0),
    ("PRG_Test.ipThing", "I_Thing", 8, -114343974404328, 0),          # interface NOT recognisable in the datatype table
    ("PRG_Test.aThings", "ARRAY [1..2] OF I_Thing", 16, [0, -114343974404328], 0),
    ("PRG_Test.pAlias", "PT_Int", 8, -114343571583168, 0),           # N50: TYPE PT_Int : POINTER TO INT (0xFFFF980150088F40)
    ("PRG_Test.pAlias4026", "PT_RetestAlarm", 8, -114343571591392, 0),  # N50 (0.8.11 retest): the 4026 table gives it no base
]


def _dt(name, typ, size, offs=0, dtype=0, dims=(), subs=(), enum=None, flags=0, guid=""):
    return DtEntry(name, typ, size, offs, dtype, flags, "", list(dims), list(subs), dict(enum or {}), guid=guid)


# datatype table of the fake runtime
DATATYPES = [
    _dt("BOOL", "", 1, dtype=33), _dt("INT", "", 2, dtype=2), _dt("UDINT", "", 4, dtype=19), _dt("LREAL", "", 8, dtype=5),
    _dt("TIME", "", 4, dtype=19), _dt("STRING(40)", "", 41, dtype=30), _dt("STRING(10)", "", 11, dtype=30),
    _dt("E_FillState", "INT", 2, dtype=2, enum={0: "Idle", 1: "Bulk", 2: "Fine", 3: "Done"},
        guid="{0E7A1C5D-3B2F-4C10-9A8B-1F2E3D4C5B6A}"),
    _dt("ST_Recipe", "", 57, subs=[_dt("rTargetKg", "LREAL", 8, 0, 5), _dt("rBulkCutoff", "LREAL", 8, 8, 5), _dt("sName", "STRING(40)", 41, 16, 30)]),
    _dt("ST_Step", "", 16, subs=[_dt("nId", "INT", 2, 0, 2), _dt("tDwell", "TIME", 4, 4, 19), _dt("bDone", "BOOL", 1, 8, 33),
                                  _dt("eState", "E_FillState", 2, 10, 2)], guid="{51E9D2A4-77C0-4E1B-8F3A-0C9B8A7D6E5F}"),
    _dt("ARRAY [1..2] OF ST_Step", "ST_Step", 32, dims=[(1, 2)]),
    _dt("ARRAY [0..3] OF LREAL", "LREAL", 32, dtype=5, dims=[(0, 4)]),
    _dt("FB_Valve", "", 16, subs=[_dt("bOpen", "BOOL", 1, 0, 33), _dt("tOpened", "TIME", 4, 4, 19),
                                   _dt("arrHist", "ARRAY [0..1] OF INT", 4, 8, 2, dims=[(0, 2)])]),
    _dt("ARRAY [0..1] OF INT", "INT", 4, dtype=2, dims=[(0, 2)]),
    _dt("I_Module", "", 8, dtype=65, flags=0x800),                 # METHODINFOS, no members, pointer-sized → interface
    _dt("POINTER TO INT", "INT", 8, dtype=21),
    _dt("PT_Int", "POINTER TO INT", 8, dtype=21),
    _dt("PT_RetestAlarm", "", 8, dtype=65),           # as 4026 lists TYPE PT_RetestAlarm : POINTER TO FB_AlarmList
    _dt("ARRAY [0..7] OF BOOL", "BOOL", 8, dtype=33, dims=[(0, 8)]),
]
DATATYPE_BLOB = b"".join(encode_entry(e) for e in DATATYPES)

# raw memory images for composite symbols (little endian)
RAW_IMAGES = {
    "MAIN.stRecipe": struct.pack("<dd", 180.5, 0.9) + b"Vanilla".ljust(41, b"\x00"),
    "MAIN.arrSteps": struct.pack("<hxxIB?h", 1, 1500, 1, 0, 1).ljust(16, b"\x00") + struct.pack("<hxxIB?h", 2, 250, 0, 0, 3).ljust(16, b"\x00"),
    "MAIN.fbValve2": struct.pack("<B3xIhh", 1, 42, 7, -3).ljust(16, b"\x00"),
}


# symbols that exist on the runtime but are not in the uploaded table (resolved by name)
EXTRA_SYMBOLS: dict[str, tuple[str, int, Any]] = {
    "TwinCAT_SystemInfoVarList._AppInfo.AppName": ("STRING(63)", 64, "DrumFill"),
    "TwinCAT_SystemInfoVarList._AppInfo.ProjectName": ("STRING(63)", 64, "Plc"),
    "TwinCAT_SystemInfoVarList._AppInfo.OnlineChangeCnt": ("UDINT", 4, 3),
    "TwinCAT_SystemInfoVarList._AppInfo.TaskCnt": ("UDINT", 4, 1),
    "TwinCAT_SystemInfoVarList._AppInfo.BootDataLoaded": ("BOOL", 1, True),
    "TwinCAT_SystemInfoVarList._AppInfo.AppTimestamp": ("DT", 4, 1792022400),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleTime": ("UDINT", 4, 100000),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].LastExecTime": ("UDINT", 4, 2500),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount": ("UDINT", 4, 123456),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleTimeExceeded": ("UDINT", 4, 2),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].Priority": ("UDINT", 4, 20),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].AdsPort": ("UINT", 2, 350),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].RTViolation": ("BOOL", 1, False),
    "TwinCAT_SystemInfoVarList._TaskInfo[1].TaskName": ("STRING(63)", 64, "PlcTask"),
}


_DISPATCH = threading.RLock()     # one notification dispatcher: callbacks and deletions in turn (AdsLib)


class FakeConnection:
    instances: list["FakeConnection"] = []
    shared_values: dict[str, Any] = {}
    fail_reads: set[str] = set()
    max_read_bytes: int | None = None
    read_log: list[tuple[str, int, int]] = []
    # the runtime's symbol version (0xF008) and its symbol handles: a test that "downloads" (moves variables) bumps
    # the version; a handle made before refuses with 0x711 (handles_survive = False) or follows its variable (True)
    symbol_version: int = 1
    handles: dict[int, tuple[str, int]] = {}
    next_handle: int = 1000
    handles_survive: bool = False
    fail_version_reads: bool = False

    def __init__(self, ams_net_id: str = None, ams_net_port: int = None, ip_address: str = None):
        self.ams_net_id, self.ams_net_port, self.ip_address = ams_net_id, ams_net_port, ip_address
        self.is_open = False
        self._port = 30000
        self._adr = pyads.AmsAddr(ams_net_id or "1.1.1.1.1.1", ams_net_port or 851)
        self.types = {n: (t, s, f) for n, t, s, v, f in SYMBOLS}
        self.writes: list[tuple[str, Any]] = []
        self.control: list[tuple[int, int, Any]] = []
        self.timeout = None
        self.rpc_calls: list[tuple[str, bytes]] = []
        FakeConnection.instances.append(self)

    @property
    def values(self) -> dict[str, Any]:
        return FakeConnection.shared_values

    @classmethod
    def reset_values(cls) -> None:
        cls.shared_values = {n: v for n, t, s, v, f in SYMBOLS}
        cls.shared_values.update({n: v for n, (t, s, v) in EXTRA_SYMBOLS.items()})

    # lifecycle
    def open(self): self.is_open = True
    def close(self): self.is_open = False
    def set_timeout(self, ms): self.timeout = ms

    # state / info — shared per AMS port, like the real runtime (a reconnect sees the same state)
    shared_state: dict[int, tuple[int, int]] = {}

    @property
    def state(self):
        return FakeConnection.shared_state.get(self.ams_net_port or 851, (C.ADSSTATE_RUN, 0))

    @state.setter
    def state(self, v):
        FakeConnection.shared_state[self.ams_net_port or 851] = v

    def read_state(self): return self.state
    def read_device_info(self):
        ver = type("V", (), {"version": 3, "revision": 1, "build": 4024})()
        return "TwinCAT PLC Server", ver
    def write_control(self, ads_state, device_state, data, plc_datatype):
        self.control.append((ads_state, device_state, data))
        if ads_state == C.ADSSTATE_STOP: self.state = (C.ADSSTATE_STOP, 0)
        if ads_state == C.ADSSTATE_RUN: self.state = (C.ADSSTATE_RUN, 0)
        if ads_state == C.ADSSTATE_RECONFIG: self.state = (C.ADSSTATE_CONFIG, 0)      # system service ends in CONFIG
        if ads_state == C.ADSSTATE_RESET and (self.ams_net_port == 10000): self.state = (C.ADSSTATE_RUN, 0)

    # symbol table
    @staticmethod
    def _sym_index(name):
        return next((k for k, x in enumerate(SYMBOLS) if x[0] == name), None)

    def _handle_name(self, h):
        if h not in FakeConnection.handles:
            raise pyads.ADSError(0x710)
        name, ver = FakeConnection.handles[h]
        if ver != FakeConnection.symbol_version and not FakeConnection.handles_survive:
            raise pyads.ADSError(0x711)                 # symbol version invalid: the program changed
        return name

    def _read_symbol(self, name, n):
        """The current bytes of a variable by name (where the program has it NOW)."""
        if name in EXTRA_SYMBOLS:
            return memory_image(_EXTRA_OFFSET[name], n)
        i = self._sym_index(name)
        if i is None:
            raise pyads.ADSError(0x710)
        return bytes(self.read(0x4040, i * 64, ctypes.c_ubyte * n, return_ctypes=True))

    def read(self, index_group, index_offset, plc_datatype, return_ctypes=False, check_length=True):
        if index_group == 0xF008:                        # ADSIGRP_SYM_VERSION
            if FakeConnection.fail_version_reads:
                raise pyads.ADSError(1861)              # timeout: the runtime does not answer
            if plc_datatype is ctypes.c_ubyte:
                return FakeConnection.symbol_version
            n = ctypes.sizeof(plc_datatype)
            return (ctypes.c_ubyte * n)(*bytes([FakeConnection.symbol_version & 0xFF]).ljust(n, b"\x00"))
        if index_group == C.ADSIGRP_SYM_VALBYHND:
            n = ctypes.sizeof(plc_datatype)
            raw = self._read_symbol(self._handle_name(index_offset), n)
            return (ctypes.c_ubyte * n)(*raw[:n].ljust(n, b"\x00"))
        if index_group == C.ADSIGRP_SYM_UPLOADINFO2:
            blob = b"".join(_entry(n, t, s, 0x4040, i * 64, f) for i, (n, t, s, v, f) in enumerate(SYMBOLS))
            self._blob = blob
            buf = struct.pack("<IIII", len(SYMBOLS), len(blob), len(DATATYPES), len(DATATYPE_BLOB)).ljust(64, b"\x00")
            return (ctypes.c_ubyte * 64)(*buf)
        if index_group == C.ADSIGRP_SYM_UPLOAD:
            return (ctypes.c_ubyte * len(self._blob))(*self._blob)
        if index_group == C.ADSIGRP_SYM_DT_UPLOAD:
            if getattr(FakeConnection, "no_datatypes", False):
                raise pyads.ADSError(1793)
            return (ctypes.c_ubyte * len(DATATYPE_BLOB))(*DATATYPE_BLOB)
        if index_group == 0x4040 and 0x9000 <= index_offset < 0x9000 + 64 * len(EXTRA_SYMBOLS):
            want = ctypes.sizeof(plc_datatype)                   # TwinCAT_SystemInfoVarList (resolved by name)
            return (ctypes.c_ubyte * want)(*memory_image(index_offset, want))
        if index_group == 0x4040:
            # raw read of a symbol by offset -> find by ioffs (a read inside a big symbol: the symbol containing it)
            i, rel = _symbol_at(index_offset)
            name = SYMBOLS[i][0]
            size = SYMBOLS[i][2]
            want = ctypes.sizeof(plc_datatype)
            if name in FakeConnection.fail_reads:
                raise pyads.ADSError(1802)                       # ADS 'out of memory' / not readable
            if FakeConnection.max_read_bytes and want > FakeConnection.max_read_bytes:
                raise pyads.ADSError(1802)
            FakeConnection.read_log.append((name, rel, want))
            raw = RAW_IMAGES.get(name)
            if callable(raw):
                raw = raw(rel, want)                             # big images generated on demand
            else:
                if raw is None:
                    v = self.values.get(name)
                    raw = _scalar_bytes(SYMBOLS[i][1], size, v)
                raw = raw[rel:rel + want]
            raw = raw[:want].ljust(want, b"\x00")
            return (ctypes.c_ubyte * want)(*raw)
        if index_group in (C.ADSIGRP_IOIMAGE_RWIB, C.ADSIGRP_IOIMAGE_RWOB):
            n = ctypes.sizeof(plc_datatype)
            return (ctypes.c_ubyte * n)(*([0xA5] * n))
        if index_group == 0x3 and index_offset == 0x100:
            return (ctypes.c_ubyte * 2)(*struct.pack("<H", 8))
        if index_group == 0x6:
            return (ctypes.c_ubyte * 2)(*struct.pack("<H", 12))
        raise pyads.ADSError(1793)  # service not supported

    def write(self, index_group, index_offset, value, plc_datatype):
        if index_group == C.ADSIGRP_SYM_VALBYHND:
            code = self._write_handle(index_offset, bytes(value))
            if code:
                raise pyads.ADSError(code)
            return
        self.writes.append((f"raw:{index_group:X}/{index_offset:X}", bytes(value)))
        self._apply_raw(index_group, index_offset, bytes(value))

    def _write_handle(self, h, data):
        try:
            name = self._handle_name(h)
        except pyads.ADSError as exc:
            return exc.err_code
        i = self._sym_index(name)
        if i is None:
            return 0x710
        self.writes.append((f"hnd:{name}", data))
        return self._apply_raw(0x4040, i * 64, data)

    def _apply_raw(self, igrp, ioffs, data):
        """A raw write into PLC memory updates the symbol at that offset (scalars/arrays of the fake layout)."""
        if igrp != 0x4040 or ioffs % 64:
            return 0
        i = ioffs // 64
        if i >= len(SYMBOLS):
            return 1793
        name, typ, size, _v, _f = SYMBOLS[i]
        from twincat_mcp import types_tc as _T
        tt = _T.parse_type(typ, size)
        try:
            if tt.kind == "string":
                val = data.split(b"\x00", 1)[0].decode("cp1252")
            elif tt.ctype is not None and not tt.opaque:
                obj = tt.ctype.from_buffer_copy(data[:ctypes.sizeof(tt.ctype)])
                val = list(obj) if tt.kind == "array" else obj.value
            else:
                return 0
        except Exception:  # noqa: BLE001
            return 1797
        self.values[name] = val
        FakeConnection.broadcast(name, val)
        return 0

    def read_by_name(self, name, plc_datatype=None, **kw):
        if name not in self.values:
            raise pyads.ADSError(1808)  # symbol not found
        if plc_datatype is not None and getattr(plc_datatype, "_type_", None) is ctypes.c_ubyte and kw.get("return_ctypes"):
            n = ctypes.sizeof(plc_datatype)                 # raw bytes by name: a fresh handle, the current address
            return (ctypes.c_ubyte * n)(*self._read_symbol(name, n)[:n].ljust(n, b"\x00"))
        return self.values[name]

    def write_by_name(self, name, value, plc_datatype=None, **kw):
        if name not in self.values:
            raise pyads.ADSError(1808)
        self.values[name] = value
        self.writes.append((name, value))
        FakeConnection.broadcast(name, value)

    @classmethod
    def broadcast(cls, name, value, ts=None):
        """Deliver change notifications on every connection that subscribed to `name` (ts: FILETIME, default now)."""
        import time as _t
        if ts is None:
            ts = int(_t.time() * 1e7) + 116444736000000000
        for inst in list(cls.instances):
            inst._deliver(name, value, ts)

    def read_write(self, index_group, index_offset, plc_read_datatype, value, plc_write_datatype, return_ctypes=False, check_length=True):
        if index_group == 0xF080:                     # ADSIGRP_SUMUP_READ: n × (igrp, ioffs, len) → n codes, then the data
            if getattr(FakeConnection, "no_sum_commands", False):
                raise pyads.ADSError(1793)
            raw = bytes(value)
            n = index_offset
            hdr = [struct.unpack_from("<III", raw, 12 * k) for k in range(n)]
            FakeConnection.sum_reads = getattr(FakeConnection, "sum_reads", 0) + 1
            codes, data = [], b""
            for igrp, ioffs, ln in hdr:
                try:
                    part = bytes(self.read(igrp, ioffs, ctypes.c_ubyte * ln, return_ctypes=True))
                    codes.append(0)
                except pyads.ADSError as exc:
                    codes.append(exc.err_code)
                    part = b""
                data += part[:ln].ljust(ln, b"\x00")
            out = struct.pack(f"<{n}I", *codes) + data
            return (ctypes.c_ubyte * len(out))(*out)
        if index_group == 0xF081:                     # ADSIGRP_SUMUP_WRITE: n × (igrp, ioffs, len) then the data
            raw = bytes(value)
            n = index_offset
            hdr = [struct.unpack_from("<III", raw, 12 * k) for k in range(n)]
            pos = 12 * n
            codes = []
            self.sum_writes = getattr(self, "sum_writes", 0) + 1
            for igrp, ioffs, ln in hdr:
                if igrp == C.ADSIGRP_SYM_VALBYHND:
                    codes.append(self._write_handle(ioffs, raw[pos:pos + ln]))
                elif igrp == C.ADSIGRP_SYM_RELEASEHND:
                    FakeConnection.handles.pop(struct.unpack_from("<I", raw, pos)[0], None)
                    codes.append(0)
                else:
                    codes.append(self._apply_raw(igrp, ioffs, raw[pos:pos + ln]))
                    self.writes.append((f"sum:{igrp:X}/{ioffs:X}", raw[pos:pos + ln]))
                pos += ln
            return (ctypes.c_uint32 * n)(*codes)
        if index_group == 0xF082:                     # ADSIGRP_SUMUP_READWRITE: n × (igrp, ioffs, rlen, wlen) then data
            if getattr(FakeConnection, "no_sum_commands", False):
                raise pyads.ADSError(1793)
            raw = bytes(value)
            n = index_offset
            hdr = [struct.unpack_from("<IIII", raw, 16 * k) for k in range(n)]
            pos = 16 * n
            res, data = [], b""
            for igrp, ioffs, rlen, wlen in hdr:
                wd = raw[pos:pos + wlen]
                pos += wlen
                try:
                    if igrp == C.ADSIGRP_SYM_HNDBYNAME:
                        name = wd.rstrip(b"\x00").decode("latin-1")
                        if name not in self.values and self._sym_index(name) is None:
                            raise pyads.ADSError(0x710)
                        h = FakeConnection.next_handle
                        FakeConnection.next_handle += 1
                        FakeConnection.handles[h] = (name, FakeConnection.symbol_version)
                        part = struct.pack("<I", h)
                    else:
                        part = bytes(self.read(igrp, ioffs, ctypes.c_ubyte * rlen, return_ctypes=True))[:rlen]
                    res.append((0, len(part)))
                    data += part
                except pyads.ADSError as exc:
                    res.append((exc.err_code, 0))
            out = b"".join(struct.pack("<II", e, ln) for e, ln in res) + data
            return (ctypes.c_ubyte * len(out))(*out)
        # RPC emulation: handle by name for "inst#method", then VALBYHND = call
        if index_group == C.ADSIGRP_SYM_HNDBYNAME:
            if "#" not in value:
                raise pyads.ADSError(1808)
            self._rpc_name = value
            return 77
        if index_group == C.ADSIGRP_SYM_VALBYHND and index_offset == 77:
            in_bytes = bytes(value) if value else b""
            self.rpc_calls.append((self._rpc_name, in_bytes))
            if self._rpc_name.endswith("#Add"):  # Add(a: LREAL, b: LREAL) : LREAL ; VAR_OUTPUT bOk : BOOL
                a, b = struct.unpack("<dd", in_bytes)
                out = struct.pack("<d?", a + b, True)
            else:
                out = b""
            if plc_read_datatype is None:
                return None
            n = ctypes.sizeof(plc_read_datatype)
            return (ctypes.c_ubyte * n)(*out[:n].ljust(n, b"\x00"))
        raise pyads.ADSError(1793)

    def read_list_by_name(self, names, cache_symbol_info=True, **kw):
        """Like pyads: each name's address (index group/offset, size, type) is looked up once per connection and kept
        — a download that moves the variable is not noticed; the old offset is read."""
        from twincat_mcp import types_tc as _T
        cache = self.__dict__.setdefault("_symbol_info_cache", {})
        out = {}
        for n in names:
            if n not in cache or not cache_symbol_info:
                i = next((k for k, x in enumerate(SYMBOLS) if x[0] == n), None)
                if i is None:
                    raise pyads.ADSError(1808)
                cache[n] = (0x4040, i * 64, SYMBOLS[i][2], SYMBOLS[i][1])
            _g, off, size, typ = cache[n]
            tt = _T.parse_type(typ, size)
            raw = memory_image(off, size)
            obj = tt.ctype.from_buffer_copy(raw[:ctypes.sizeof(tt.ctype)])
            out[n] = list(obj) if tt.kind == "array" else obj.value
        return out

    # notifications
    def add_device_notification(self, data, attr, callback, user_handle=None):
        """By name (legacy callers) or by (index_group, index_offset) — the recorder's block notifications."""
        if isinstance(data, tuple):
            igrp, ioffs = data
            if getattr(FakeConnection, "fail_offsets", None) and ioffs in FakeConnection.fail_offsets:
                raise pyads.ADSError(1861)
            key = ("blk", igrp, ioffs, attr.length)
        else:
            if data not in self.values:
                raise pyads.ADSError(1808)
            key = data
        self.notifications = getattr(self, "notifications", {})
        h = len(self.notifications) + 1
        self.notifications[h] = (key, attr, callback)
        if getattr(FakeConnection, "initial_notification", False):
            # real ADS sends the current value right after the registration (both server-cycle and on-change)
            import time as _t
            ts = int(_t.time() * 1e7) + 116444736000000000
            if isinstance(key, tuple):
                data = memory_image(key[2], key[3])
                buf = ctypes.create_string_buffer(struct.pack("<IQI", h, ts, len(data)) + data)
                callback(ctypes.cast(buf, ctypes.POINTER(SAdsNotificationHeader)), (key[1], key[2]))
            else:
                callback(type("N", (), {"h": h, "ts": ts, "value": self.values.get(key)})(), key)
        return (h, h)

    def del_device_notification(self, nh, uh):
        # one dispatcher for callbacks and deletions (Beckhoff's AdsLib NotificationDispatcher holds one mutex for
        # both): a deletion waits for a callback in progress, and no callback of a deleted handle runs after it
        # returned. The fake called a callback it had listed before a deletion after it (release CI 0.8.11: 82
        # delivered, 81 counted — the 82nd landed after the recording's footer); the recorder counts every sample
        # that reaches it before its footer is written
        with _DISPATCH:
            self.notifications.pop(nh, None)

    def parse_notification(self, notification, plc_datatype, timestamp_as_filetime=False):
        return notification.h, notification.ts, notification.value

    def _deliver(self, name, value, ts):
        off = _SYM_OFFSET.get(name, _EXTRA_OFFSET.get(name))
        n = 0
        with _DISPATCH:
            return self._deliver_locked(name, value, ts, off, n)

    def _deliver_locked(self, name, value, ts, off, n):
        for h, (key, attr, cb) in list(getattr(self, "notifications", {}).items()):
            if key == name:
                cb(type("N", (), {"h": h, "ts": ts, "value": value})(), name)
                n += 1
            elif isinstance(key, tuple) and key[0] == "blk" and key[1] == 0x4040 and off is not None \
                    and key[2] <= off < key[2] + key[3]:
                data = memory_image(key[2], key[3])
                buf = ctypes.create_string_buffer(struct.pack("<IQI", h, ts, len(data)) + data)
                ptr = ctypes.cast(buf, ctypes.POINTER(SAdsNotificationHeader))
                cb(ptr, (key[1], key[2]))
                n += 1
        return n

    def fire(self, name, value, ts_filetime):
        """Test helper: deliver a notification for `name` as the DLL would (block subscribers get the whole block).
        -> how many subscriptions it reached (0 once the recording unsubscribed)."""
        self.values[name] = value
        return self._deliver(name, value, ts_filetime)


_SYM_OFFSET = {n: i * 64 for i, (n, t, s, v, f) in enumerate(SYMBOLS)}
# the runtime-only symbols sit apart from the table (each its own 64-byte slot), so block notifications on them work
_EXTRA_OFFSET = {n: 0x9000 + k * 64 for k, n in enumerate(EXTRA_SYMBOLS)}


def memory_image(start: int, length: int) -> bytes:
    """PLC memory (index group 0x4040) as the fake lays it out: symbol i at offset i*64."""
    buf = bytearray(b"\x00" * length)
    for i, (n, t, size, v0, f) in enumerate(SYMBOLS):
        off = i * 64
        if off + 64 <= start or off >= start + length:
            continue
        raw = RAW_IMAGES.get(n)
        if raw is None:
            raw = _scalar_bytes(t, size, FakeConnection.shared_values.get(n, v0))
        raw = bytes(raw[:64])
        for j, b in enumerate(raw):
            k = off + j - start
            if 0 <= k < length:
                buf[k] = b
    for n, off in _EXTRA_OFFSET.items():
        if off + 64 <= start or off >= start + length:
            continue
        t, size, v0 = EXTRA_SYMBOLS[n]
        raw = bytes(_scalar_bytes(t, size, FakeConnection.shared_values.get(n, v0))[:64])
        for j, b in enumerate(raw):
            k = off + j - start
            if 0 <= k < length:
                buf[k] = b
    return bytes(buf)


def _symbol_at(offset: int) -> tuple[int, int]:
    """(symbol index, offset inside it) for a raw 0x4040 read: symbol i lives at i * 64; a big symbol appended last
    extends past the next slots, so a read beyond the table's slots belongs to the symbol that contains it."""
    i = offset // 64
    if offset % 64 == 0 and i < len(SYMBOLS):
        return i, 0
    for k in range(len(SYMBOLS) - 1, -1, -1):
        base = k * 64
        if base <= offset < base + max(SYMBOLS[k][2], 64):
            return k, offset - base
    return min(i, len(SYMBOLS) - 1), 0


def _scalar_bytes(typ, size, v):
    t = typ.upper()
    if v is None:
        return b"\x11" * size
    if t == "BOOL":
        return bytes([1 if v else 0])
    if t in ("INT",):
        return struct.pack("<h", v)
    if t in ("UDINT", "TIME"):
        return struct.pack("<I", v)
    if t == "LREAL":
        return struct.pack("<d", v)
    if t.startswith("STRING"):
        return str(v).encode("cp1252").ljust(size, b"\x00")      # the PLC's STRING: Windows-1252
    if t.startswith("ARRAY") and isinstance(v, list) and t.endswith("LREAL"):
        return b"".join(struct.pack("<d", x) for x in v)
    if t == "E_FillState":
        return struct.pack("<h", v)
    try:
        from twincat_mcp import types_tc as _T
        tt = _T.parse_type(typ, size)
        if tt.kind == "array" and isinstance(v, list) and tt.ctype is not None:
            return bytes(tt.ctype(*v))
        if tt.ctype is not None and tt.kind in ("int", "uint", "enum", "bool", "float", "pointer", "dt", "date", "tod",
                                                "time"):
            return bytes(tt.ctype(v))
    except Exception:  # noqa: BLE001
        pass
    return b"\x11" * size


def install(monkeypatch):
    """Patch pyads so AdsBackend uses FakeConnection and a fake local NetId."""
    FakeConnection.instances.clear()
    FakeConnection.shared_state = {}
    FakeConnection.reset_values()
    FakeConnection.no_datatypes = False
    FakeConnection.no_sum_commands = False             # a runtime without ADS sum commands (ADS 1793)
    FakeConnection.fail_reads = set()                  # symbol names whose raw reads fail (ADS 1802)
    FakeConnection.max_read_bytes = None               # a read asking for more fails with ADS 1802, like the router
    FakeConnection.read_log = []                       # (symbol, offset inside it, bytes) of every raw 0x4040 read
    FakeConnection.initial_notification = False      # tests that model real ADS registration behaviour turn it on
    FakeConnection.symbol_version = 1
    FakeConnection.fail_version_reads = False
    FakeConnection.handles = {}
    FakeConnection.next_handle = 1000
    FakeConnection.handles_survive = False

    def fake_symbol_info(port, adr, name):
        if name in EXTRA_SYMBOLS:
            t, size, v = EXTRA_SYMBOLS[name]
            return type("E", (), {"name": name, "symbol_type": t, "size": size, "iGroup": 0x4040, "iOffs": _EXTRA_OFFSET[name],
                                  "flags": 0, "comment": ""})()
        raise pyads.ADSError(1808)
    monkeypatch.setattr(pyads.pyads_ex, "adsGetSymbolInfo", fake_symbol_info)
    monkeypatch.setattr(pyads, "Connection", FakeConnection)
    monkeypatch.setattr(pyads, "open_port", lambda: 30000)
    monkeypatch.setattr(pyads, "close_port", lambda: None)
    monkeypatch.setattr(pyads, "get_local_address", lambda: pyads.AmsAddr("192.168.0.10.1.1", 30000))
    import twincat_mcp.ads_backend as ab
    monkeypatch.setattr(ab.pyads, "Connection", FakeConnection)
    return FakeConnection
