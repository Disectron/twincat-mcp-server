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
    reads: list[tuple[int, int, int]] = []
    real = FakeConnection.read

    def read(self, index_group, index_offset, plc_datatype, return_ctypes=False, check_length=True):
        if index_group != M.MDP_INDEX_GROUP:
            return real(self, index_group, index_offset, plc_datatype, return_ctypes, check_length)
        assert self.ams_net_port == M.MDP_PORT
        idx, sub = index_offset >> 16, index_offset & 0xFF
        reads.append((idx, sub, ctypes.sizeof(plc_datatype)))
        if (idx, sub) not in dev:
            raise pyads.ADSError(1795)                     # invalid index offset
        n = ctypes.sizeof(plc_datatype)
        return (ctypes.c_ubyte * n)(*dev[(idx, sub)][:n].ljust(n, b"\x00"))
    monkeypatch.setattr(FakeConnection, "read", read)
    return dev, reads


def test_read_info_decodes_the_documented_tables_and_lists_failures(device):
    dev, reads = device

    def reader(i, s, n):
        if (i, s) not in dev:
            raise M.MdpError("ADS 1795")
        return dev[(i, s)][:n]
    r = M.read_info(reader)
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


def test_read_info_budget_and_a_module_list_that_cannot_be_read(device):
    dev, _ = device
    r = M.read_info(lambda i, s, n: dev[(i, s)][:n] if (i, s) in dev else b"\x00" * n, max_reads=12)
    assert r["reads"] == 12 and "truncated" in r, r
    with pytest.raises(M.MdpError, match=r"module list \(0xF020:00\) could not be read"):
        M.read_info(lambda i, s, n: (_ for _ in ()).throw(M.MdpError("ADS 1861 timeout")))


@pytest.mark.anyio
async def test_ipc_info_through_the_server(device, project_dir):
    dev, reads = device
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("ipc_info", {"module_types": ["CPU", "twincat"]}))
        assert [m["type"] for m in r["modules"]] == ["CPU", "CPU", "TwinCAT"] and r["modules_not_asked"] == 5, r
        assert r["ads_address"]["confirmed"].startswith("not yet on hardware"), r
        bad = await s.call_tool("ipc_info", {"module_types": ["GPU"]})
        assert bad.is_error and "unknown module type(s) ['GPU']" in text(bad), text(bad)


@pytest.mark.anyio
async def test_ipc_mdp_read_decodes_one_address(device, project_dir):
    dev, reads = device
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("ipc_mdp_read", {"index": 0x8011, "subindex": 3, "type": "i16"}))
        assert r["value"] == 51 and r["ads_address"]["index_offset"] == "0x80110003", r
        r = payload(await s.call_tool("ipc_mdp_read", {"index": 0x8041, "subindex": 4, "type": "string"}))
        assert r["value"] == "5.1.2.3.1.1"
        r = await s.call_tool("ipc_mdp_read", {"index": 0x8999, "subindex": 1, "type": "u16"})
        assert r.is_error and "MDP 0x8999:01" in text(r) and "0xF302" in text(r), text(r)
