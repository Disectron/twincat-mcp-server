"""A fake pyads.Connection good enough to exercise AdsBackend/SystemServiceFiles."""
from __future__ import annotations

import ctypes
import struct
from typing import Any

import pyads
from pyads import constants as C


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
]


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
        self.state = (C.ADSSTATE_RUN, 0)
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

    # state / info
    def read_state(self): return self.state
    def read_device_info(self):
        ver = type("V", (), {"version": 3, "revision": 1, "build": 4024})()
        return "TwinCAT PLC Server", ver
    def write_control(self, ads_state, device_state, data, plc_datatype):
        self.control.append((ads_state, device_state, data))
        if ads_state == C.ADSSTATE_STOP: self.state = (C.ADSSTATE_STOP, 0)
        if ads_state == C.ADSSTATE_RUN: self.state = (C.ADSSTATE_RUN, 0)

    # symbol table
    def read(self, index_group, index_offset, plc_datatype, return_ctypes=False, check_length=True):
        if index_group == C.ADSIGRP_SYM_UPLOADINFO2:
            blob = b"".join(_entry(n, t, s, 0x4040, i * 64, f) for i, (n, t, s, v, f) in enumerate(SYMBOLS))
            self._blob = blob
            buf = struct.pack("<II", len(SYMBOLS), len(blob)).ljust(64, b"\x00")
            return (ctypes.c_ubyte * 64)(*buf)
        if index_group == C.ADSIGRP_SYM_UPLOAD:
            return (ctypes.c_ubyte * len(self._blob))(*self._blob)
        if index_group == 0x4040:
            # raw read of a symbol by offset -> find by ioffs
            i = index_offset // 64
            name = SYMBOLS[i][0]
            size = SYMBOLS[i][2]
            raw = (b"\x11" * size)
            return (ctypes.c_ubyte * size)(*raw)
        if index_group == 0x3 and index_offset == 0x100:
            return (ctypes.c_ubyte * 2)(*struct.pack("<H", 8))
        if index_group == 0x6:
            return (ctypes.c_ubyte * 2)(*struct.pack("<H", 12))
        raise pyads.ADSError(1793)  # service not supported

    def write(self, index_group, index_offset, value, plc_datatype):
        self.writes.append((f"raw:{index_group:X}/{index_offset:X}", bytes(value)))

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
            for h, (n, attr, cb) in getattr(inst, "notifications", {}).items():
                if n == name:
                    cb(type("N", (), {"h": h, "ts": ts, "value": value})(), name)

    def read_write(self, index_group, index_offset, plc_read_datatype, value, plc_write_datatype, return_ctypes=False, check_length=True):
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
    def add_device_notification(self, name, attr, callback, user_handle=None):
        if name not in self.values:
            raise pyads.ADSError(1808)
        self.notifications = getattr(self, "notifications", {})
        h = len(self.notifications) + 1
        self.notifications[h] = (name, attr, callback)
        return (h, h)

    def del_device_notification(self, nh, uh):
        self.notifications.pop(nh, None)

    def parse_notification(self, notification, plc_datatype, timestamp_as_filetime=False):
        return notification.h, notification.ts, notification.value

    def fire(self, name, value, ts_filetime):
        """Test helper: deliver a notification for `name` as the DLL would."""
        for h, (n, attr, cb) in self.notifications.items():
            if n == name:
                cb(type("N", (), {"h": h, "ts": ts_filetime, "value": value})(), name)


def install(monkeypatch):
    """Patch pyads so AdsBackend uses FakeConnection and a fake local NetId."""
    FakeConnection.instances.clear()
    FakeConnection.reset_values()

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
