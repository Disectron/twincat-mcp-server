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


class FakeConnection:
    instances: list["FakeConnection"] = []

    def __init__(self, ams_net_id: str = None, ams_net_port: int = None, ip_address: str = None):
        self.ams_net_id, self.ams_net_port, self.ip_address = ams_net_id, ams_net_port, ip_address
        self.is_open = False
        self._port = 30000
        self._adr = pyads.AmsAddr(ams_net_id or "1.1.1.1.1.1", ams_net_port or 851)
        self.values: dict[str, Any] = {n: v for n, t, s, v, f in SYMBOLS}
        self.types = {n: (t, s, f) for n, t, s, v, f in SYMBOLS}
        self.state = (C.ADSSTATE_RUN, 0)
        self.writes: list[tuple[str, Any]] = []
        self.control: list[tuple[int, int, Any]] = []
        self.timeout = None
        FakeConnection.instances.append(self)

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

    def read_list_by_name(self, names, **kw):
        return {n: self.values[n] for n in names}


def install(monkeypatch):
    """Patch pyads so AdsBackend uses FakeConnection and a fake local NetId."""
    FakeConnection.instances.clear()
    monkeypatch.setattr(pyads, "Connection", FakeConnection)
    monkeypatch.setattr(pyads, "open_port", lambda: 30000)
    monkeypatch.setattr(pyads, "close_port", lambda: None)
    monkeypatch.setattr(pyads, "get_local_address", lambda: pyads.AmsAddr("192.168.0.10.1.1", 30000))
    import twincat_mcp.ads_backend as ab
    monkeypatch.setattr(ab.pyads, "Connection", FakeConnection)
    return FakeConnection
