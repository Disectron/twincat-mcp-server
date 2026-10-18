import time

import pytest
from pathlib import Path

from twincat_mcp.config import AdsConfig
from twincat_mcp.ads_backend import AdsBackend, AdsError
from tests.fake_ads import install


@pytest.fixture
def ads(monkeypatch):
    install(monkeypatch)
    return AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1", ams_port=851))


def test_connect_and_state(ads):
    info = ads.connect(None, None, None)
    assert info["ads_state_name"] == "RUN"
    assert info["device"]["name"] == "TwinCAT PLC Server"
    st = ads.state()
    assert st["ads_state"] == 5 and st["port"] == 851
    assert ads.connection_status()["open"][0]["port"] == 851


def test_local_target_uses_local_netid(monkeypatch):
    Fake = install(monkeypatch)
    b = AdsBackend(AdsConfig(ams_net_id=None))
    b.state()
    assert Fake.instances[-1].ams_net_id == "192.168.0.10.1.1"


def test_symbol_table_parsing(ads):
    res = ads.list_symbols("MAIN.fbFill.*", 100, 0, False)
    names = [s["name"] for s in res["symbols"]]
    assert names == ["MAIN.fbFill.tSettle", "MAIN.fbFill.eState", "MAIN.fbFill.arrWeights", "MAIN.fbFill.bDone"]
    assert res["symbols"][0]["type"] == "TIME" and res["symbols"][0]["size"] == 4
    ro = ads.symbol_info("Constants.CompilerVersion")
    assert "READONLY" in ro.flag_names()
    # bare text is wrapped in wildcards; type filter works
    assert ads.list_symbols("recipe", 100, 0, False, type_filter="LREAL")["total"] == 2
    # pagination
    assert ads.list_symbols("*", 2, 1, False)["returned"] == 2


def test_read_decodes_types(ads):
    assert ads.read("MAIN.nCycle")["value"] == 42
    t = ads.read("MAIN.fbFill.tSettle")
    assert t["value"] == {"raw_ms": 1500, "text": "T#1s500ms"}
    assert ads.read("MAIN.fbFill.eState")["value"] == 1
    assert ads.read("MAIN.fbFill.arrWeights")["value"] == [1.0, 2.5, 0.0, -1.0]
    assert ads.read("MAIN.sLabel")["value"] == "hello"


def test_read_struct_via_datatype_table(ads):
    r = ads.read("MAIN.stRecipe")
    assert r["type"] == "ST_Recipe" and r["decoder"] == "datatype-table"
    assert r["value"] == {"rTargetKg": 180.5, "rBulkCutoff": 0.9, "sName": "Vanilla"}
    arr = ads.read("MAIN.arrSteps")["value"]
    assert arr[0]["nId"] == 1 and arr[0]["tDwell"]["text"] == "T#1s500ms" and arr[0]["eState"] == {"value": 1, "name": "Bulk"}
    assert arr[1]["bDone"] is False and arr[1]["eState"]["name"] == "Done"
    fb = ads.read("MAIN.fbValve2")["value"]
    assert fb == {"bOpen": True, "tOpened": {"raw_ms": 42, "text": "T#42ms"}, "arrHist": [7, -3]}
    r2 = ads.read("MAIN.stRecipe", expand_struct=False)
    assert r2["value"] is None and "raw_hex" in r2


def test_read_struct_fallback_without_datatype_table(ads, monkeypatch):
    from tests.fake_ads import FakeConnection
    FakeConnection.no_datatypes = True
    assert ads.typesystem(refresh=True) is None
    r = ads.read("MAIN.stRecipe")
    assert r["members"] == 3 and r["value"]["sName"] == "Vanilla"   # published member symbols
    r = ads.read("MAIN.arrSteps")
    assert r["value"] is None and "raw_hex" in r                   # nothing published beneath it
    FakeConnection.no_datatypes = False
    assert ads.typesystem(refresh=True) is not None


def test_leaves_roots_and_dump(ads, tmp_path):
    s, lv = ads.leaves("MAIN.arrSteps")
    assert [(l.path, l.offset) for l in lv][:5] == [("MAIN.arrSteps[1].nId", 0), ("MAIN.arrSteps[1].tDwell", 4), ("MAIN.arrSteps[1].bDone", 8),
                                                   ("MAIN.arrSteps[1].eState", 10), ("MAIN.arrSteps[2].nId", 16)]
    assert lv[3].enum == {0: "Idle", 1: "Bulk", 2: "Fine", 3: "Done"}
    roots = [r.name for r in ads.roots()]
    assert "MAIN.stRecipe" in roots and "MAIN.stRecipe.sName" not in roots and "Constants.CompilerVersion" not in roots
    assert "Constants.CompilerVersion" in [r.name for r in ads.roots(include_system=True)]
    assert sorted(r.name for r in ads.roots("GVL_*")) == ["GVL_Machine.MAX_MODULES", "GVL_Machine.nDrumsFilled"]
    d = ads.dump(None, False, str(tmp_path / "dumps"))
    assert d["roots"] == len(roots) and d["leaves"] >= 25 and not d["errors"]
    import json, csv
    tree = json.loads((tmp_path / "dumps" / d["json"].split("/")[-1]).read_text())["values"]
    assert tree["MAIN.arrSteps"][1]["eState"]["name"] == "Done" and tree["MAIN.nCycle"] == 42
    rows = list(csv.DictReader(open(d["csv"], encoding="utf-8")))
    paths = {r["path"]: r["value"] for r in rows}
    assert paths["MAIN.arrSteps[2].tDwell"] == "250" and paths["MAIN.arrSteps[1].eState"] == "1 (Bulk)" and paths["MAIN.sLabel"] == "hello"
    assert ads.type_info("ST_Step")["members"][3]["name"] == "eState"
    assert ads.list_types("ST_*", 10)["total"] == 2
    with pytest.raises(AdsError, match="not in datatype table"):
        ads.type_info("ST_Nope")


def test_read_many_mixed(ads):
    r = ads.read_many(["MAIN.nCycle", "MAIN.stRecipe", "MAIN.sLabel", "Nope.x"])
    assert r["MAIN.nCycle"] == 42
    assert r["MAIN.stRecipe"]["sName"] == "Vanilla"
    assert r["MAIN.sLabel"] == "hello"
    assert "error" in r["Nope.x"]


def test_write_typed_and_readonly(ads, monkeypatch):
    r = ads.write("MAIN.fbFill.tSettle", "T#2s")
    assert r["before"]["raw_ms"] == 1500 and r["read_back"]["raw_ms"] == 2000
    r = ads.write("MAIN.bStart", "false")
    assert r["read_back"] is False
    with pytest.raises(AdsError, match="READONLY"):
        ads.write("Constants.CompilerVersion", 1)
    with pytest.raises(AdsError, match="opaque"):
        ads.write("MAIN.stRecipe", 5)
    # opaque hex write of exact size
    r = ads.write("MAIN.stRecipe", "ab" * 57)
    assert r["written_hex"] == "ab" * 57


def test_watch_and_wait(ads, monkeypatch):
    import itertools
    from tests.fake_ads import FakeConnection
    ads.conn()
    conn = FakeConnection.instances[-1]
    seq = itertools.chain([0, 0, 1, 1, 2], itertools.repeat(2))
    orig = conn.read_list_by_name

    def rl(names, **kw):
        conn.values["MAIN.nCycle"] = next(seq)
        return orig(names)
    conn.read_list_by_name = rl
    ads.cfg.min_watch_interval_ms = 1
    w = ads.watch(["MAIN.nCycle"], duration_ms=60, interval_ms=5, change_only=True)
    vals = [s["MAIN.nCycle"] for s in w["samples"]]
    assert vals == [0, 1, 2] and w["changes"] == 2
    conn.values["MAIN.nCycle"] = 0
    conn.read_by_name = lambda n, t=None, **k: (conn.values.__setitem__(n, conn.values[n] + 1) or conn.values[n])
    r = ads.wait_until("MAIN.nCycle", ">=", 3, timeout_ms=500, interval_ms=1)
    assert r["met"] and r["value"] >= 3
    with pytest.raises(AdsError):
        ads.wait_until("MAIN.stRecipe", "==", 1, 10, 1)


def test_plc_state_and_system_mode(ads):
    r = ads.set_plc_state("stop")
    assert r["after"]["ads_state_name"] == "STOP"
    r = ads.set_plc_state("run")
    assert r["after"]["ads_state_name"] == "RUN"
    with pytest.raises(AdsError):
        ads.set_plc_state("explode")
    r = ads.set_system_mode("config")
    assert r["requested"] == "config" and r["reached"] and r["after"]["twincat_mode"] == "CONFIG"   # #75: verified, not assumed
    r = ads.set_system_mode("run", timeout_s=1.5)
    assert r["reached"] and r["after"]["twincat_mode"] == "RUN"


def test_ethercat_overview(ads):
    r = ads.ethercat_overview()
    assert r["master_state"] == "OP" and r["slave_count"] == 12


def test_local_routes_missing_file(ads, tmp_path):
    ads.cfg.static_routes_xml = str(tmp_path / "StaticRoutes.xml")
    assert ads.local_routes()["routes"] == []
    (tmp_path / "StaticRoutes.xml").write_text(
        '<?xml version="1.0"?><TcConfig><RemoteConnections><Route><Name>CX-1</Name><Address>192.168.0.5</Address>'
        '<NetId>5.1.2.3.1.1</NetId><Type>TCP_IP</Type></Route></RemoteConnections></TcConfig>')
    r = ads.local_routes()
    assert r["routes"][0]["NetId"] == "5.1.2.3.1.1"


def test_write_reports_read_back_changed_by_plc(ads, monkeypatch):
    """#77: a one-shot the PLC clears every cycle reads back as 0 — that is not a failed write."""
    from tests.fake_ads import FakeConnection
    orig = FakeConnection.write_by_name

    def one_shot(self, name, value, plc_datatype=None, **kw):
        orig(self, name, value, plc_datatype)
        self.values[name] = 0                      # the PLC consumed the command in the same cycle
    monkeypatch.setattr(FakeConnection, "write_by_name", one_shot)
    r = ads.write("MAIN.nCycle", 1)
    assert r["written"] == 1 and r["read_back"] == 0 and "PLC changed it after the write" in r["note"]


def test_symbol_cache_follows_symbol_version(ads, monkeypatch):
    """P7: the cache is reloaded when the runtime's symbol version changes (download / online change), not on a timer."""
    from tests.fake_ads import FakeConnection
    ver = {"v": 3}
    orig = FakeConnection.read

    def read(self, igrp, ioffs, dt, return_ctypes=False, check_length=True):
        if igrp == 0xF008:
            return ver["v"]
        return orig(self, igrp, ioffs, dt, return_ctypes, check_length)
    monkeypatch.setattr(FakeConnection, "read", read)
    loads = {"n": 0}
    orig_load = ads._load_symbols

    def counting(c):
        loads["n"] += 1
        return orig_load(c)
    monkeypatch.setattr(ads, "_load_symbols", counting)
    ads.cfg.symbol_cache_ttl_s = 0            # a timer would reload on every call
    ads.symbols_cached()
    ads.__dict__.get("_symver", {}).clear()
    ads.symbols_cached()
    assert loads["n"] == 1                     # same version: cached despite the zero TTL
    ver["v"] = 4
    ads.__dict__.get("_symver", {}).clear()
    ads.symbols_cached()
    assert loads["n"] == 2                     # online change: reloaded


def _big_root(monkeypatch):
    """A 4,000-element UDINT array (≈ 4,000 CSV rows) on the fake runtime."""
    import struct as _s
    from tests import fake_ads as F
    from tests.fake_ads import _dt, encode_entry
    arr = _dt("ARRAY [1..4000] OF UDINT", "UDINT", 16000, dtype=19, dims=[(1, 4000)])
    monkeypatch.setattr(F, "SYMBOLS", F.SYMBOLS + [("GVL_Big.aValues", arr.name, 16000, None, 0)])
    monkeypatch.setattr(F, "DATATYPES", F.DATATYPES + [arr])
    monkeypatch.setattr(F, "DATATYPE_BLOB", b"".join(encode_entry(e) for e in F.DATATYPES))
    monkeypatch.setattr(F, "RAW_IMAGES", {**F.RAW_IMAGES, "GVL_Big.aValues": _s.pack("<4000I", *range(4000))})


def test_r42_dump_budget_order_streaming_and_no_rename_after_the_call_ended(ads, tmp_path, monkeypatch):
    import json, threading
    out = tmp_path / "dumps"
    full = ads.dump(None, False, str(out), own_roots={"prg_test"})
    assert not full["truncated"] and full["largest_roots"][0]["json_bytes"] > 0
    doc = json.loads(open(full["json"], encoding="utf-8").read())             # streamed root by root, still one JSON
    names = list(doc["values"])
    assert names[0].startswith("PRG_Test.") and doc["leaves"] == full["leaves"] and doc["truncated"] is False
    assert doc["values"]["PRG_Test.pData"] == "0x0000000000000000"                # POINTER TO INT: the address 0 (N51), not null
    # one leaf budget for CSV and JSON: a cut dump names what is missing
    cut = ads.dump(None, False, str(out), max_leaves=5)
    assert cut["truncated"] and cut["leaves"] == 5 and cut["skipped_total"] >= 1 and "max_leaves 5" in cut["truncated_reason"]
    j = json.loads(open(cut["json"], encoding="utf-8").read())
    assert j["truncated"] is True and sum(1 for _ in open(cut["csv"], encoding="utf-8")) == 6
    # max_total_mb bounds what is written; below the files' own frame (header, head, footer) nothing can fit: refused
    with pytest.raises(AdsError, match="smaller than the files' own frame"):
        ads.dump(None, False, str(out), max_total_bytes=200)
    tiny = ads.dump(None, False, str(out), max_total_bytes=3000)
    assert tiny["truncated"] and "size limit" in tiny["truncated_reason"] and tiny["csv_bytes"] + tiny["json_bytes"] <= 3000
    # review: one big root — its JSON (written after its CSV rows) counts against the limit as well
    _big_root(monkeypatch)
    ads._symcache.clear()
    ads._dtcache.clear()
    lim = 60_000
    one = ads.dump("GVL_Big.*", False, str(out), max_total_bytes=lim)
    # N76: exact — never past the limit, and not stopped early by an estimate (the JSON was counted as large as the
    # CSV: 45 of 64 MiB). What is left is the room kept for the endings the dump may still have to write: the root cut
    # (wrapper + footer) or a read error (its text at most _ERR_JSON_MAX, in the JSON, the CSV and the footer)
    from twincat_mcp.ads_backend import _ERR_JSON_MAX
    total = one["csv_bytes"] + one["json_bytes"]
    assert one["truncated"] and lim - 5 * _ERR_JSON_MAX - 400 <= total <= lim, one
    assert one["written_mib"] == round(total / (1024 * 1024), 3) and "on disk" in one["limit_counts"]
    files = sum(Path(one[k]).stat().st_size for k in ("csv", "json"))
    assert files == total                                       # counted = on disk
    doc = json.loads(open(one["json"], encoding="utf-8").read())
    assert doc["values"]["GVL_Big.aValues"]["_truncated"] is True
    # the client's timeout set the cancel event while the dump was finishing: nothing is renamed into place
    before = sorted(p.name for p in out.iterdir())
    ev = threading.Event()
    real = ads._dump

    def late(*a, **kw):
        r = real(*a, **kw)
        ev.set()
        return r
    ads._dump = late
    with pytest.raises(AdsError, match="call had already ended"):
        ads.dump(None, False, str(out), cancel=ev)
    assert sorted(p.name for p in out.iterdir()) == before


def test_n76_the_limit_is_the_bytes_on_disk_for_any_limit(ads, tmp_path, monkeypatch):
    """N76: for every limit the files on disk stay within it, are valid, and hold exactly what the result says."""
    import csv as _csv
    import json
    from twincat_mcp.ads_backend import _ERR_JSON_MAX
    _big_root(monkeypatch)
    ads._symcache.clear()
    ads._dtcache.clear()
    full = ads.dump(None, False, str(tmp_path / "full"))
    need = full["csv_bytes"] + full["json_bytes"]
    # a dump stops when the next row and the ending it would still have to write (the footer naming the roots left
    # out) no longer fit: at exactly its own size it may leave a few small roots out, a little above it is complete
    for k, lim in enumerate([*range(3000, need, max(1, need // 9)), need, need + 1024]):
        r = ads.dump(None, False, str(tmp_path / f"d{k}"), max_total_bytes=lim)
        total = sum(Path(r[x]).stat().st_size for x in ("csv", "json"))
        assert total == r["csv_bytes"] + r["json_bytes"] <= lim, (lim, r)
        doc = json.loads(open(r["json"], encoding="utf-8").read())
        rows = list(_csv.reader(open(r["csv"], encoding="utf-8", newline="")))
        assert len(rows) - 1 == r["leaves"] == doc["leaves"]
        if lim >= need + 1024:
            assert r["complete"] and not r["truncated"]
        elif lim >= need:
            assert lim - total <= 600, (lim, total)
        else:
            assert r["truncated"] and lim - total <= 5 * _ERR_JSON_MAX + 600 + max(len(",".join(x)) for x in rows), (lim, total)


@pytest.mark.anyio
async def test_r42_timeout_names_the_phase_not_the_router(monkeypatch, project_dir):
    import time as _t
    from tests.test_server_e2e import mk_cfg
    from twincat_mcp.server import App, ToolError
    install(monkeypatch)
    app = App(mk_cfg(project_dir))
    with pytest.raises(ToolError, match="phase: writing JSON") as ei:
        await app.ads_call(lambda: _t.sleep(1.5), timeout=0.2, bulk=True, timeout_msg=lambda: "phase: writing JSON")
    assert "router" not in str(ei.value)


def test_r42_review_dump_json_is_strict_for_nan_and_infinity():
    import json
    from twincat_mcp.ads_backend import _json_strict
    v = {"r": float("nan"), "a": [1.5, float("inf"), float("-inf")], "s": "x"}
    txt = _json_strict(v)
    assert "NaN" in txt and json.loads(txt, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c))) == \
        {"r": "NaN", "a": [1.5, "Infinity", "-Infinity"], "s": "x"}


# ---------------------------------------------------------------------------------------------- 0.8.9 retest (dump)
def _add_symbols(monkeypatch, symbols, types=()):
    """Append symbols (name, type, size, raw bytes | callable(offset, n) | None) and datatype entries to the fake."""
    from tests import fake_ads as F
    from tests.fake_ads import encode_entry
    monkeypatch.setattr(F, "SYMBOLS", F.SYMBOLS + [(n, t, sz, None, 0) for n, t, sz, _raw in symbols])
    monkeypatch.setattr(F, "DATATYPES", F.DATATYPES + list(types))
    monkeypatch.setattr(F, "DATATYPE_BLOB", b"".join(encode_entry(e) for e in F.DATATYPES))
    monkeypatch.setattr(F, "RAW_IMAGES", {**F.RAW_IMAGES, **{n: raw for n, _t, _s, raw in symbols if raw is not None}})


def _files(res):
    import csv
    import json
    rows = list(csv.reader(open(res["csv"], encoding="utf-8", newline="")))
    return rows, json.loads(open(res["json"], encoding="utf-8").read())


def test_n39_n49_big_root_is_read_in_pieces_and_an_unreadable_root_is_named(ads, tmp_path, monkeypatch):
    """N39: a 320 KB root was fetched in one ADS read → ADS 1802 'out of memory'. N49: such a root was only in
    `errors` — not in skipped_roots, truncated false, neither file mentioned it."""
    from tests.fake_ads import FakeConnection, _dt
    blk = _dt("ST_Blk", "", 80, subs=[_dt("sName", "STRING(79)", 80, 0, 30)])
    arr = _dt("ARRAY [1..4000] OF ST_Blk", "ST_Blk", 320_000, dims=[(1, 4000)])
    s79 = _dt("STRING(79)", "", 80, dtype=30)
    image = b"".join(f"blk{i}".encode().ljust(80, b"\x00") for i in range(1, 4001))
    _add_symbols(monkeypatch, [("GVL_Bad.nX", "UDINT", 4, None), ("GVL_Big.aBlk", arr.name, 320_000, image)],
                 [blk, arr, s79])
    FakeConnection.max_read_bytes = 300_000                  # like the router: one huge request fails with 1802
    FakeConnection.fail_reads = {"GVL_Bad.nX"}               # this one cannot be read at all
    res = ads.dump("GVL_B*", False, str(tmp_path))
    rows, doc = _files(res)
    vals = {r[0]: r for r in rows[1:]}
    # every element arrived, also the one that straddles the 256 KiB piece boundary (offset 262,080)
    assert vals["GVL_Big.aBlk[3277].sName"][2] == "blk3277" and vals["GVL_Big.aBlk[4000].sName"][2] == "blk4000"
    assert doc["values"]["GVL_Big.aBlk"][3276]["sName"] == "blk3277"
    big_reads = [n for name, _off, n in FakeConnection.read_log if name == "GVL_Big.aBlk"]
    assert len(big_reads) >= 2 and max(big_reads) <= 256 * 1024
    # the unreadable root: result, JSON footer, JSON node and a CSV row all say so
    assert res["complete"] is False and res["failed_roots"][0]["root"] == "GVL_Bad.nX" and "1802" in res["failed_roots"][0]["error"]
    assert any(s["root"] == "GVL_Bad.nX" and s["reason"].startswith("not read") for s in res["skipped_roots"])
    assert doc["complete"] is False and doc["failed_roots"][0]["root"] == "GVL_Bad.nX"
    assert "_not_read" in doc["values"]["GVL_Bad.nX"]
    assert vals["GVL_Bad.nX"][3].startswith("NOT READ") and vals["GVL_Bad.nX"][2] == ""


def test_n76_a_read_error_inside_a_big_root_still_ends_within_the_limit(ads, tmp_path, monkeypatch):
    """N76: while bytes of a root are still to be read, the dump keeps room for the INCOMPLETE ending (its text
    bounded by _ERR_JSON_MAX); a root read completely by its first request needs none."""
    import pyads as _pyads
    from tests.fake_ads import _dt
    from twincat_mcp.ads_backend import _ERR_JSON_MAX
    blk = _dt("ST_Blk", "", 80, subs=[_dt("sName", "STRING(79)", 80, 0, 30)])
    arr = _dt("ARRAY [1..4000] OF ST_Blk", "ST_Blk", 320_000, dims=[(1, 4000)])
    s79 = _dt("STRING(79)", "", 80, dtype=30)
    image = b"".join(f"blk{i}".encode().ljust(80, b"\x00") for i in range(1, 4001))

    def second_piece_fails(off, n):
        if off + n > 256 * 1024:                     # the second piece
            raise _pyads.ADSError(1861)
        return image[off:off + n]
    _add_symbols(monkeypatch, [("GVL_Big.aBlk", arr.name, 320_000, second_piece_fails)], [blk, arr, s79])
    free = ads.dump("GVL_Big.*", False, str(tmp_path / "a"))
    assert free["failed_roots"][0]["leaves_written"] == 3276 and "1861" in free["errors"][0]["error"]
    rows, doc = _files(free)
    assert rows[-1][3].startswith("INCOMPLETE: read stopped after 3276 of 4000 leaves")
    assert doc["values"]["GVL_Big.aBlk"]["_incomplete"] and len(doc["values"]["GVL_Big.aBlk"]["_partial"]) == 3276
    need = free["csv_bytes"] + free["json_bytes"]
    for k, lim in enumerate((need - 1, need - 3000, need // 2)):
        r = ads.dump("GVL_Big.*", False, str(tmp_path / f"b{k}"), max_total_bytes=lim)
        rows, doc = _files(r)
        assert r["csv_bytes"] + r["json_bytes"] <= lim and len(rows) - 1 >= 1, (lim, r)
        node = doc["values"].get("GVL_Big.aBlk")
        assert node is not None and ("_incomplete" in node or "_truncated" in node), node
    assert len('"' + "x" * 10 + '"') <= _ERR_JSON_MAX


def test_n39_ads_leaves_and_read_full_read_big_symbols_in_pieces(ads, monkeypatch):
    """N39 had the same root in ads_leaves (leaf_values) and the full read: one ADS request for the whole symbol."""
    from tests.fake_ads import FakeConnection, _dt
    blk = _dt("ST_Blk", "", 80, subs=[_dt("sName", "STRING(79)", 80, 0, 30)])
    arr = _dt("ARRAY [1..4000] OF ST_Blk", "ST_Blk", 320_000, dims=[(1, 4000)])
    s79 = _dt("STRING(79)", "", 80, dtype=30)
    image = b"".join(f"blk{i}".encode().ljust(80, b"\x00") for i in range(1, 4001))
    _add_symbols(monkeypatch, [("GVL_Big.aBlk", arr.name, 320_000, image)], [blk, arr, s79])
    FakeConnection.max_read_bytes = 300_000
    s, lv = ads.leaves("GVL_Big.aBlk", 10_000)
    vals, notes = ads.leaf_values(s, lv)
    assert vals["GVL_Big.aBlk[3277].sName"] == "blk3277" and vals["GVL_Big.aBlk[4000].sName"] == "blk4000"
    assert not notes
    full = ads.read_full("GVL_Big.aBlk", max_bytes=1_000_000)
    assert full["size"] == 320_000
    assert max(n for name, _o, n in FakeConnection.read_log if name == "GVL_Big.aBlk") <= 256 * 1024


def test_n47_budget_holds_per_row_and_a_huge_leaf_is_never_read_whole(ads, tmp_path, monkeypatch):
    """N47: the budget was checked at root start and every 256 leaves → < 256 big rows, or one huge leaf (an 8 MiB
    ring buffer) was read into RAM and written whole."""
    from tests.fake_ads import FakeConnection, _dt
    ring = _dt("ARRAY [0..8388607] OF BYTE", "BYTE", 8 * 1024 * 1024, dtype=17, dims=[(0, 8 * 1024 * 1024)])
    names = _dt("ARRAY [1..200] OF STRING(255)", "STRING(255)", 200 * 256, dims=[(1, 200)])
    s255 = _dt("STRING(255)", "", 256, dtype=30)
    _add_symbols(monkeypatch, [("GVL_Ring.aBuf", ring.name, 8 * 1024 * 1024,
                                lambda off, n: bytes((off + k) % 251 for k in range(n))),
                               ("GVL_Str.aNames", names.name, 200 * 256, (b"x" * 255 + b"\x00") * 200)],
                 [ring, names, s255])
    res = ads.dump("GVL_Ring.*", False, str(tmp_path), max_total_bytes=256 * 1024)
    assert res["csv_bytes"] + res["json_bytes"] < 4096 and res["bytes_read"] <= 64
    assert sum(n for name, _o, n in FakeConnection.read_log if name == "GVL_Ring.aBuf") <= 64
    small = ads.dump("GVL_Str.*", False, str(tmp_path / "s"), max_total_bytes=16 * 1024)
    assert small["truncated"] and small["complete"] is False
    assert small["csv_bytes"] + small["json_bytes"] <= 16 * 1024                 # N76: never past the limit


def test_n48_a_value_that_is_not_expanded_is_the_same_in_csv_and_json(ads, tmp_path, monkeypatch):
    """N48: JSON wrote raw_hex[:512] 'not expanded', CSV decoded the whole array (3.1 MB cell)."""
    import json
    import struct
    from tests.fake_ads import _dt
    arr = _dt("ARRAY [1..100000] OF UDINT", "UDINT", 400_000, dtype=19, dims=[(1, 100000)])
    _add_symbols(monkeypatch, [("GVL_Q.aWords", arr.name, 400_000, struct.pack("<100000I", *range(100000)))], [arr])
    res = ads.dump("GVL_Q.*", False, str(tmp_path))
    rows, doc = _files(res)
    row = next(r for r in rows if r[0] == "GVL_Q.aWords")
    assert json.loads(row[2]) == doc["values"]["GVL_Q.aWords"]
    m = doc["values"]["GVL_Q.aWords"]
    assert m["elements"] == 100000 and m["bytes"] == 400_000 and m["head_hex"] == struct.pack("<16I", *range(16)).hex()
    assert "not expanded" in row[3] and res["bytes_read"] < 1024             # only the head was read


def test_n52_rename_is_both_or_neither_and_the_timeout_verdict_matches_the_files(ads, tmp_path, monkeypatch):
    """N52: a failed second os.replace left the CSV renamed and was reported as an ADS error; a cancel arriving while
    the files were being renamed left both files although the client was told nothing is kept."""
    import os
    import threading
    from twincat_mcp import ads_backend as AB
    from twincat_mcp.server import dump_timeout_message
    real = AB._move_new
    calls = []

    def second_fails(a, b):
        calls.append((a, b))
        if len(calls) == 2:
            raise OSError("disk full")
        return real(a, b)
    monkeypatch.setattr(AB, "_move_new", second_fails)
    with pytest.raises(AdsError, match="a file error, not an ADS error"):
        ads.dump("MAIN.*", False, str(tmp_path / "a"))
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == []            # neither file, no .partial
    # the timeout fires while the dump is committing: it committed, and the verdict says the files were kept
    ev = threading.Event()

    def cancel_during_rename(a, b):
        ev.set()
        return real(a, b)
    monkeypatch.setattr(AB, "_move_new", cancel_during_rename)
    res = ads.dump("MAIN.*", False, str(tmp_path / "b"), cancel=ev)
    out = ads.dump_outcome()
    assert out["phase"] == "done" and os.path.isfile(res["csv"]) and os.path.isfile(res["json"])
    assert "WERE kept" in dump_timeout_message(out, 900) and res["csv"] in dump_timeout_message(out, 900)
    # cancelled before the commit: nothing kept, and the verdict says so
    monkeypatch.setattr(AB, "_move_new", real)
    ev2 = threading.Event()
    realdump = ads._dump

    def then_cancel(*a, **kw):
        r = realdump(*a, **kw)
        ev2.set()
        return r
    monkeypatch.setattr(ads, "_dump", then_cancel)
    with pytest.raises(AdsError, match="cancelled"):
        ads.dump("MAIN.*", False, str(tmp_path / "c"), cancel=ev2)
    assert list((tmp_path / "c").iterdir()) == [] and "nothing is kept" in dump_timeout_message(ads.dump_outcome(), 900)


def test_n53_sizes_are_utf8_bytes_in_mib(ads, tmp_path, monkeypatch):
    """N53: csv_bytes counted characters; the limit is MiB but progress divided by 1e6."""
    import os
    from tests.fake_ads import _dt
    s20 = _dt("STRING(20)", "", 21, dtype=30)
    raw = "Grüße ok".encode("latin-1").ljust(21, b"\x00")
    _add_symbols(monkeypatch, [("GVL_Txt.sName", "STRING(20)", 21, raw)], [s20])
    res = ads.dump("GVL_Txt.*", False, str(tmp_path))
    assert res["csv_bytes"] == os.path.getsize(res["csv"]) and res["json_bytes"] == os.path.getsize(res["json"])
    assert res["limit_mib"] == 64 and "Grüße" in open(res["csv"], encoding="utf-8").read()


def test_n54_without_a_project_the_order_is_not_guessed_from_names(ads, tmp_path, monkeypatch):
    """N54: user GVLs named Param_*, Constants*, Global_Version* were ranked as library roots and dumped last."""
    _add_symbols(monkeypatch, [("ConstantsUser.nY", "UDINT", 4, None), ("Param_Machine.nX", "UDINT", 4, None)])
    res = ads.dump(None, False, str(tmp_path))
    names = list(_files(res)[1]["values"])
    assert names == sorted(names, key=str.lower) and names[0] == "ConstantsUser.nY"
    assert "unknown" in res["order"]
    own = ads.dump(None, False, str(tmp_path / "p"), own_roots={"param_machine"})
    assert list(_files(own)[1]["values"])[0] == "Param_Machine.nX" and "own programs" in own["order"]


def test_n55_a_cancel_stops_inside_a_root(ads, tmp_path, monkeypatch):
    """N55: cancel was checked only between roots / every 2048 rows; one big root ran on after the timeout."""
    import struct
    import threading
    from tests.fake_ads import _dt
    from twincat_mcp import ads_backend as AB
    ev = threading.Event()
    arr = _dt("ARRAY [1..3000] OF UDINT", "UDINT", 12_000, dtype=19, dims=[(1, 3000)])
    image = struct.pack("<3000I", *range(3000))

    def first_read_times_out(off, n):
        ev.set()                                   # the client's timeout fires while the root is being read
        return image[off:off + n]
    _add_symbols(monkeypatch, [("GVL_C.aVals", arr.name, 12_000, first_read_times_out)], [arr])
    seen = []
    real = AB._csv_val
    monkeypatch.setattr(AB, "_csv_val", lambda v: seen.append(1) or real(v))
    with pytest.raises(AdsError, match="cancelled"):
        ads.dump("GVL_C.*", False, str(tmp_path), cancel=ev)
    assert len(seen) < 300, len(seen)                 # stopped within the root, not after it


def test_dump_names_are_unique_and_a_finished_file_is_never_overwritten(ads, tmp_path, monkeypatch):
    """Review B: dump-<second>.json/.csv — two dumps in the same second wrote the same .partial files and the second
    replaced the first's finished files. Names are reserved exclusively; a target that appears meanwhile is not
    overwritten; a rollback that fails is reported as a partial commit, not as 'nothing kept'."""
    import os
    from twincat_mcp.server import dump_timeout_message
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: "20261018-101500")
    out = tmp_path / "d"
    a = ads.dump("MAIN.*", False, str(out))
    b = ads.dump("MAIN.*", False, str(out))
    assert a["json"] != b["json"] and a["csv"] != b["csv"] and b["json"].endswith("dump-20261018-101500-2.json"), b
    assert os.path.isfile(a["json"]) and os.path.isfile(b["json"])
    # a file with the reserved name appears while the dump runs (another program): it is not replaced
    real = ads._dump

    def squat(*args, **kw):
        r = real(*args, **kw)
        (out / "dump-20261018-101500-3.csv").write_text("theirs", encoding="utf-8")
        return r
    monkeypatch.setattr(ads, "_dump", squat)
    with pytest.raises(AdsError, match="not overwritten"):
        ads.dump("MAIN.*", False, str(out))
    assert (out / "dump-20261018-101500-3.csv").read_text(encoding="utf-8") == "theirs"
    assert not list(out.glob("*.partial"))
    monkeypatch.setattr(ads, "_dump", real)
    # a file that appears between the check and the move is not replaced either (the move itself refuses)
    from twincat_mcp import ads_backend as AB
    mv = AB._move_new

    def squat_at_move(src, dst):
        if str(dst).endswith(".csv"):
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write("theirs too")
        return mv(src, dst)
    monkeypatch.setattr(AB, "_move_new", squat_at_move)
    with pytest.raises(AdsError, match="not overwritten"):
        ads.dump("MAIN.*", False, str(tmp_path / "q"))
    assert (tmp_path / "q" / "dump-20261018-101500.csv").read_text(encoding="utf-8") == "theirs too"
    assert not list((tmp_path / "q").glob("*.partial"))
    # the JSON move fails and so does moving the CSV back: say what is where, remove nothing
    rep = os.replace

    def json_fails(src, dst):
        if str(dst).endswith(".json"):
            raise OSError("locked")
        return mv(src, dst)

    def back_fails(src, dst):
        raise OSError("locked")
    monkeypatch.setattr(AB, "_move_new", json_fails)
    monkeypatch.setattr(os, "replace", back_fails)
    with pytest.raises(AdsError, match="PARTIAL commit") as ei:
        ads.dump("MAIN.*", False, str(tmp_path / "p"))
    monkeypatch.setattr(os, "replace", rep)
    monkeypatch.setattr(AB, "_move_new", mv)
    left = sorted(p.name for p in (tmp_path / "p").iterdir())
    assert left == ["dump-20261018-101500.csv", "dump-20261018-101500.json.partial"], left
    st = ads.dump_outcome()
    assert st["phase"] == "partial" and "PARTIAL" in dump_timeout_message(st, 900), st
    assert "json.partial" in str(ei.value)


def test_leaf_values_reads_the_head_of_a_value_that_is_not_expanded_and_never_past_the_symbol(ads, monkeypatch,
                                                                                              tmp_path):
    """Review B: leaf_values read lf.size for a leaf that is not expanded (a whole 8 MiB buffer for a 64-byte preview)
    and skipped a leaf the datatype table places past the symbol's end without a word; the dump read such a leaf from
    the next variable's bytes."""
    import dataclasses
    from tests.fake_ads import FakeConnection, _dt
    ring = _dt("ARRAY [0..8388607] OF BYTE", "BYTE", 8 * 1024 * 1024, dtype=17, dims=[(0, 8 * 1024 * 1024)])
    _add_symbols(monkeypatch, [("GVL_R.aRing", ring.name, 8 * 1024 * 1024, bytes(range(256)) * 32768)], [ring])
    s, lv = ads.leaves("GVL_R.aRing", 100)
    assert len(lv) == 1 and lv[0].not_expanded
    FakeConnection.read_log.clear()
    vals, notes = ads.leaf_values(s, lv)
    assert sum(n for name, _o, n in FakeConnection.read_log if name == "GVL_R.aRing") <= 1024, FakeConnection.read_log
    assert "not expanded" in notes["GVL_R.aRing"]
    # a leaf past the end of the symbol: no value, a note — never the neighbour's bytes
    past = dataclasses.replace(lv[0], path="GVL_R.aRing.x", offset=8 * 1024 * 1024 - 2, size=4, elements=None)
    vals, notes = ads.leaf_values(s, [past])
    assert vals["GVL_R.aRing.x"] is None and "outside GVL_R.aRing" in notes["GVL_R.aRing.x"], notes
    # the dump: that leaf gets a note row, the root's other leaves are still read
    import csv as _csv
    from twincat_mcp.ads_types import TypeSystem
    real = TypeSystem.leaves

    def plus_past(self, name, *a, **kw):
        got = real(self, name, *a, **kw)
        return got + [past] if name == "GVL_R.aRing" else got
    monkeypatch.setattr(TypeSystem, "leaves", plus_past)
    res = ads.dump("GVL_R.*", False, str(tmp_path / "d"))
    rows = {r[0]: r for r in _csv.reader(open(res["csv"], encoding="utf-8"))}
    assert "outside GVL_R.aRing" in rows["GVL_R.aRing.x"][3] and rows["GVL_R.aRing.x"][2] == "", rows
    assert "not expanded" in rows["GVL_R.aRing"][3] and not res.get("failed_roots"), res
    # review: such a leaf makes the dump incomplete — it was complete: true with a bare null in the JSON
    import json as _json
    doc = _json.loads(open(res["json"], encoding="utf-8").read())
    assert res["complete"] is False and res["unreadable_total"] == 1 and "outside their symbol" in res["hint"], res
    assert doc["complete"] is False and doc["unreadable_total"] == 1
    assert "outside GVL_R.aRing" in str(doc["values"]["GVL_R.aRing"]), doc["values"]
