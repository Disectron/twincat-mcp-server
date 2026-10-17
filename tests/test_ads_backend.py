import pytest

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
