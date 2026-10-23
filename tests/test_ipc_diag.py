"""0.8.19: IPC diagnostics (docs/PLAN-0.8.19.md §8) — ipc_info / ipc_mdp_read against a fake Device Manager answering
MDP reads (synthetic values only)."""
from __future__ import annotations

import ctypes
import struct

import pyads
import pytest

from tests.fake_ads import FakeConnection, install
from tests.test_server_e2e import client_for, mk_cfg, payload, text
from twincat_mcp import mdp as M
from twincat_mcp.server import build_server


def _s(t: str) -> bytes:
    return t.encode() + b"\x00"


def _device() -> dict[tuple[int, int], bytes]:
    """Modules: 2 CPUs, memory, TwinCAT, a fan, disk management, mainboard (no BIOS API: its temperature fails), and
    a type this code does not decode."""
    mods = [(0x000B, 1), (0x000B, 2), (0x000C, 3), (0x0008, 4), (0x001B, 5), (0x001D, 6), (0x001C, 7), (0x0099, 8)]
    d: dict[tuple[int, int], bytes] = {(0xF020, 0): struct.pack("<H", len(mods))}
    for i, (t, mid) in enumerate(mods, 1):
        d[(0xF020, i)] = struct.pack("<I", t << 16 | mid)
        d[(M.table_index(mid, 0), 3)] = _s(M.MODULE_TYPES.get(t, "Thing") + f" {mid}")
    for mid, usage, temp in ((1, 37, 51), (2, 12, -3)):
        d[(M.table_index(mid, 1), 1)] = struct.pack("<I", 1000)
        d[(M.table_index(mid, 1), 2)] = struct.pack("<H", usage)
        d[(M.table_index(mid, 1), 3)] = struct.pack("<h", temp)
    d[(M.table_index(3, 1), 6)] = struct.pack("<Q", 600_000_000)
    d[(M.table_index(3, 1), 7)] = struct.pack("<Q", 400_000_000)
    tc = M.table_index(4, 1)
    d.update({(tc, 1): struct.pack("<H", 3), (tc, 2): struct.pack("<H", 1), (tc, 3): struct.pack("<H", 4024),
              (tc, 12): struct.pack("<H", 56), (tc, 4): _s("5.1.2.3.1.1"), (tc, 6): struct.pack("<H", 5)})
    rt = M.table_index(4, 9)
    d.update({(rt, 1): struct.pack("<Q", 33_554_432), (rt, 2): struct.pack("<Q", 30_000_000),
              (rt, 3): struct.pack("<I", 21)})
    d[(M.table_index(5, 1), 1)] = struct.pack("<h", 2400)
    for table, vals, fmt in ((1, ["C:\\", "D:\\"], None), (3, ["NTFS", "FAT32"], None), (4, [1, 2], "<I"),
                             (5, [32_000_000_000, 2_000_000_000], "<Q"), (6, [10_000_000_000, 1_500_000_000], "<Q")):
        idx = M.table_index(6, table)
        d[(idx, 0)] = struct.pack("<H", len(vals))
        for i, v in enumerate(vals, 1):
            d[(idx, i)] = struct.pack(fmt, v) if fmt else _s(v)
    d[(M.table_index(7, 1), 2)] = _s("SN-0001")
    d[(M.table_index(7, 1), 4)] = struct.pack("<I", 812)
    return d


@pytest.fixture
def device(monkeypatch):
    install(monkeypatch)
    dev = _device()
    reads: list[tuple[int, int]] = []
    timeouts: set[tuple[int, int]] = set()
    real = FakeConnection.read

    def read(self, index_group, index_offset, plc_datatype, return_ctypes=False, check_length=True):
        if index_group != M.MDP_INDEX_GROUP:
            return real(self, index_group, index_offset, plc_datatype, return_ctypes, check_length)
        # pyads 3.6: a STRING type comes back as str (read into a buffer, cut at NUL); a c_ubyte array with
        # check_length raises when fewer bytes came (without it, the zeroed buffer comes back whole)
        assert self.ams_net_port == M.MDP_PORT
        idx, sub = index_offset >> 16, index_offset & 0xFF
        reads.append((idx, sub))
        if (idx, sub) in timeouts:
            raise pyads.ADSError(1861)
        if (idx, sub) not in dev:
            raise pyads.ADSError(1795)                     # invalid index offset
        data = dev[(idx, sub)]
        if plc_datatype is pyads.PLCTYPE_STRING:
            return data.split(b"\x00", 1)[0].decode("cp1252")
        n = ctypes.sizeof(plc_datatype)
        if check_length and len(data) != n:
            raise RuntimeError(f"Insufficient data (expected {n} bytes, {len(data)} were read).")
        return (ctypes.c_ubyte * n)(*data[:n].ljust(n, b"\x00"))
    monkeypatch.setattr(FakeConnection, "read", read)
    return dev, reads, timeouts


def _reader(dev):
    def reader(i, s, t):
        if (i, s) not in dev:
            raise M.MdpError("ADS 1795")
        return M.decode(dev[(i, s)], t)
    return reader


def test_read_info_decodes_the_documented_tables_and_lists_failures(device):
    dev, reads, _ = device
    r = M.read_info(_reader(dev))
    mods = {m["module_id"]: m for m in r["modules"]}
    assert r["module_count"] == 8 and len(mods) == 8
    assert mods[1]["type"] == "CPU" and mods[1]["values"]["cpu_temperature"] == {"value": 51, "unit": "°C",
                                                                                   "mdp": "0x8011:03"}
    assert mods[2]["values"]["cpu_temperature"]["value"] == -3 and mods[2]["values"]["cpu_usage"]["value"] == 12
    assert mods[3]["values"]["program_memory_available_64"]["value"] == 400_000_000
    assert "program_memory_allocated" in mods[3]["errors"]            # >4 GB rule: the 32-bit row may be refused
    tc = mods[4]["values"]
    assert (tc["version_major"]["value"], tc["build"]["value"], tc["revision"]["value"]) == (3, 4024, 56)
    assert tc["router_memory_max"]["value"] == 33_554_432 and tc["ams_net_id"]["value"] == "5.1.2.3.1.1"
    assert mods[5]["values"]["fan_speed"] == {"value": 2400, "unit": "rpm", "mdp": "0x8051:01"}
    assert mods[6]["values"]["volumes"] == [
        {"drive": "C:\\", "filesystem": "NTFS", "drive_type": "fixed", "size": 32_000_000_000, "free": 10_000_000_000},
        {"drive": "D:\\", "filesystem": "FAT32", "drive_type": "removable", "size": 2_000_000_000,
         "free": 1_500_000_000}]
    assert "label_count" in mods[6]["errors"]                          # a table that is not there is said
    assert mods[7]["values"]["serial_number"]["value"] == "SN-0001" and "board_temperature" in mods[7]["errors"]
    assert mods[8]["type"] == "0x0099" and "no documented table" in mods[8]["note"] and "values" not in mods[8]


def test_read_info_budget_stops_cleanly_and_an_exact_budget_is_no_stop(device):
    dev, _, _ = device
    full = M.read_info(_reader(dev))
    exact = M.read_info(_reader(dev), max_reads=full["reads"])
    assert "stopped" not in exact and len(exact["modules"]) == 8, exact
    r = M.read_info(_reader(dev), max_reads=12)
    assert r["reads"] == 12 and "read budget used up" in r["stopped"] and "of 8 module(s) not read" in r["stopped"], r
    assert all("error" not in m for m in r["modules"]), r          # unread rows are not listed as failures
    with pytest.raises(M.MdpError, match=r"module list \(0xF020:00\) could not be read"):
        M.read_info(lambda i, s, t: (_ for _ in ()).throw(M.MdpError("ADS 1861 timeout")))


def test_read_info_refuses_what_cannot_be_a_module_list_and_unaddressable_modules(device):
    dev, _, _ = device
    dev[(0xF020, 0)] = struct.pack("<H", 0xFFFF)                    # a wrong address answering something
    with pytest.raises(M.MdpError, match="65535 modules — more than the 8-bit module ID allows"):
        M.read_info(_reader(dev))
    dev[(0xF020, 0)] = struct.pack("<H", 1)
    dev[(0xF020, 1)] = struct.pack("<I", 0x000B << 16 | 0x101)
    m = M.read_info(_reader(dev))["modules"][0]
    assert m["module_id"] == 257 and "does not fit the 8-bit module field" in m["error"] and "values" not in m, m


def test_read_info_stops_when_the_target_stops_answering(device):
    dev, _, timeouts = device

    def reader(i, s, t):
        if i != 0xF020 and i >> 4 & 0xFF >= 3:                     # from module 3 on every read times out
            raise M.MdpError("ADS error 1861 (0x745): timeout elapsed")
        return _reader(dev)(i, s, t)
    r = M.read_info(reader)
    assert "the target stopped answering (3 reads in a row timed out" in r["stopped"], r
    assert r["reads"] < 30, r


@pytest.mark.anyio
async def test_ipc_info_through_the_server(device, project_dir):
    dev, reads, _ = device
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("ipc_info", {"module_types": ["CPU", "twincat"]}))
        assert [m["type"] for m in r["modules"]] == ["CPU", "CPU", "TwinCAT"] and r["modules_not_asked"] == 5, r
        assert r["ads_address"]["confirmed"].startswith("not yet on hardware"), r
        bad = await s.call_tool("ipc_info", {"module_types": ["GPU"]})
        assert bad.is_error and "unknown module type(s) ['GPU']" in text(bad), text(bad)


@pytest.mark.anyio
async def test_ipc_mdp_read_decodes_one_address(device, project_dir):
    dev, reads, _ = device
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("ipc_mdp_read", {"index": 0x8011, "subindex": 3, "type": "i16"}))
        assert r["value"] == 51 and r["hex"] == "3300" and r["ads_address"]["index_offset"] == "0x80110003", r
        r = payload(await s.call_tool("ipc_mdp_read", {"index": 0x8041, "subindex": 4, "type": "string"}))
        assert r["value"] == "5.1.2.3.1.1" and r["mdp"] == "0x8041:04"
        r = await s.call_tool("ipc_mdp_read", {"index": 0x8999, "subindex": 1, "type": "u16"})
        assert r.is_error and "MDP 0x8999:01" in text(r) and "0xF302" in text(r), text(r)


@pytest.mark.anyio
async def test_a_short_or_empty_answer_is_an_error_never_a_value(device, project_dir):
    """Review 1: the zero-padded buffer read an empty answer as 0 and a 2-byte INT as a 4-byte value."""
    dev, reads, _ = device
    dev[(0x8011, 3)] = b""
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        r = await s.call_tool("ipc_mdp_read", {"index": 0x8011, "subindex": 3, "type": "i16"})
        assert r.is_error and "answered 0 byte(s), i16 needs 2 — not decoded" in text(r), text(r)
        r = await s.call_tool("ipc_mdp_read", {"index": 0x8021, "subindex": 3, "type": "i32"})
        assert r.is_error and "answered 2 byte(s), i32 needs 4" in text(r) and "type='hex', size=2" in text(r), text(r)
        r = payload(await s.call_tool("ipc_mdp_read", {"index": 0x8021, "subindex": 3, "type": "hex", "size": 2}))
        assert r["value"] == "fdff"
        info = payload(await s.call_tool("ipc_info", {"module_types": ["CPU"]}))
        cpu1 = info["modules"][0]
        assert "cpu_temperature" in cpu1["errors"] and "cpu_temperature" not in cpu1["values"], cpu1
        dev[(0xF020, 0)] = b""
        r = await s.call_tool("ipc_info", {})
        assert r.is_error and "module list (0xF020:00) could not be read" in text(r), text(r)


@pytest.mark.anyio
async def test_an_address_that_would_be_cut_is_refused(device, project_dir):
    dev, reads, _ = device
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        for args in ({"index": 0x8011, "subindex": 257}, {"index": 0x18011, "subindex": 1},
                     {"index": -1, "subindex": 0}):
            r = await s.call_tool("ipc_mdp_read", {**args, "type": "u16"})
            assert r.is_error and "is not an MDP address" in text(r) and "nothing was read" in text(r), (args, text(r))
        r = await s.call_tool("ipc_mdp_read", {"index": 0x8011, "subindex": 3, "type": "i16", "size": 8})
        assert r.is_error and "size is for type='hex' only" in text(r)
    assert reads == []


def test_a_cancelled_call_stops_between_reads(device):
    """Review 1: a timed-out ipc_info kept the slow ADS worker busy (each later read waited its ADS timeout)."""
    dev, _, _ = device
    n = {"n": 0}

    def reader(i, s, t):
        n["n"] += 1
        if n["n"] > 20:
            raise M.MdpStop("the call was cancelled")
        return _reader(dev)(i, s, t)
    r = M.read_info(reader)
    assert n["n"] == 21 and r["stopped"].startswith("the call was cancelled"), r
