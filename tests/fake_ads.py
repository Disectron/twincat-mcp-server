"""A fake pyads.Connection good enough to exercise AdsBackend/SystemServiceFiles."""
from __future__ import annotations

import ctypes
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


class FakeConnection:
    instances: list["FakeConnection"] = []
    shared_values: dict[str, Any] = {}

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
    def read(self, index_group, index_offset, plc_datatype, return_ctypes=False, check_length=True):
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
        if index_group == 0x4040:
            # raw read of a symbol by offset -> find by ioffs
            i = index_offset // 64
            name = SYMBOLS[i][0]
            size = SYMBOLS[i][2]
            want = ctypes.sizeof(plc_datatype)
            raw = RAW_IMAGES.get(name)
            if raw is None:
                v = self.values.get(name)
                raw = _scalar_bytes(SYMBOLS[i][1], size, v)
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
        self.writes.append((f"raw:{index_group:X}/{index_offset:X}", bytes(value)))
        self._apply_raw(index_group, index_offset, bytes(value))

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
                val = data.split(b"\x00", 1)[0].decode("latin-1")
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
        return self.values[name]

    def write_by_name(self, name, value, plc_datatype=None, **kw):
        if name not in self.values:
            raise pyads.ADSError(1808)
        self.values[name] = value
        self.writes.append((name, value))
        FakeConnection.broadcast(name, value)

    @classmethod
    def broadcast(cls, name, value):
        """Deliver change notifications on every connection that subscribed to `name`."""
        import time as _t
        ts = int(_t.time() * 1e7) + 116444736000000000
        for inst in list(cls.instances):
            inst._deliver(name, value, ts)

    def read_write(self, index_group, index_offset, plc_read_datatype, value, plc_write_datatype, return_ctypes=False, check_length=True):
        if index_group == 0xF081:                     # ADSIGRP_SUMUP_WRITE: n × (igrp, ioffs, len) then the data
            raw = bytes(value)
            n = index_offset
            hdr = [struct.unpack_from("<III", raw, 12 * k) for k in range(n)]
            pos = 12 * n
            codes = []
            self.sum_writes = getattr(self, "sum_writes", 0) + 1
            for igrp, ioffs, ln in hdr:
                codes.append(self._apply_raw(igrp, ioffs, raw[pos:pos + ln]))
                self.writes.append((f"sum:{igrp:X}/{ioffs:X}", raw[pos:pos + ln]))
                pos += ln
            return (ctypes.c_uint32 * n)(*codes)
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

    def read_list_by_name(self, names, **kw):
        return {n: self.values[n] for n in names}

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
        return (h, h)

    def del_device_notification(self, nh, uh):
        self.notifications.pop(nh, None)

    def parse_notification(self, notification, plc_datatype, timestamp_as_filetime=False):
        return notification.h, notification.ts, notification.value

    def _deliver(self, name, value, ts):
        off = _SYM_OFFSET.get(name)
        for h, (key, attr, cb) in list(getattr(self, "notifications", {}).items()):
            if key == name:
                cb(type("N", (), {"h": h, "ts": ts, "value": value})(), name)
            elif isinstance(key, tuple) and key[0] == "blk" and key[1] == 0x4040 and off is not None \
                    and key[2] <= off < key[2] + key[3]:
                data = memory_image(key[2], key[3])
                buf = ctypes.create_string_buffer(struct.pack("<IQI", h, ts, len(data)) + data)
                ptr = ctypes.cast(buf, ctypes.POINTER(SAdsNotificationHeader))
                cb(ptr, (key[1], key[2]))

    def fire(self, name, value, ts_filetime):
        """Test helper: deliver a notification for `name` as the DLL would (block subscribers get the whole block)."""
        self.values[name] = value
        self._deliver(name, value, ts_filetime)


_SYM_OFFSET = {n: i * 64 for i, (n, t, s, v, f) in enumerate(SYMBOLS)}


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
    return bytes(buf)


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
        return str(v).encode().ljust(size, b"\x00")
    if t.startswith("ARRAY") and isinstance(v, list):
        return b"".join(struct.pack("<d", x) for x in v)
    if t == "E_FillState":
        return struct.pack("<h", v)
    try:
        from twincat_mcp import types_tc as _T
        tt = _T.parse_type(typ, size)
        if tt.kind == "array" and isinstance(v, list) and tt.ctype is not None:
            return bytes(tt.ctype(*v))
        if tt.ctype is not None and tt.kind in ("int", "uint", "enum", "bool", "float", "pointer"):
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

    def fake_symbol_info(port, adr, name):
        if name in EXTRA_SYMBOLS:
            t, size, v = EXTRA_SYMBOLS[name]
            return type("E", (), {"name": name, "symbol_type": t, "size": size, "iGroup": 0x4040, "iOffs": 0x9000,
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
