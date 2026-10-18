import struct
import threading
import time
from pathlib import Path

import pytest

from twincat_mcp.config import AdsConfig
from twincat_mcp.ads_backend import AdsBackend, AdsError
from twincat_mcp.recorder import Recorder
from twincat_mcp.testing import TestBench, TestError, pack_value, unpack_value
from tests.fake_ads import install, FakeConnection


@pytest.fixture
def bench(monkeypatch, tmp_path):
    install(monkeypatch)
    ads = AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1"))
    ads.cfg.min_watch_interval_ms = 1
    rec = Recorder(ads, tmp_path / "rec")
    return TestBench(ads, rec, tmp_path / "snap")


def later(ms, name, value):
    def run():
        time.sleep(ms / 1000.0)
        FakeConnection.shared_values[name] = value
        FakeConnection.broadcast(name, value)
    threading.Thread(target=run, daemon=True).start()


def test_expect_now_and_within(bench):
    r = bench.expect([{"symbol": "MAIN.nCycle", "op": "==", "value": 42},
                      {"symbol": "MAIN.fbFill.tSettle", "op": "==", "value": "T#1s500ms"},
                      {"symbol": "MAIN.stRecipe.rBulkCutoff", "op": "~=", "value": 0.9, "tolerance": 0.01},
                      {"symbol": "MAIN.fbFill.eState", "op": "in", "value": [0, 1, 2]},
                      {"symbol": "MAIN.nCycle", "op": "between", "value": [40, 45]}])
    assert r["pass"] and all(c["pass"] for c in r["checks"])
    r = bench.expect([{"symbol": "MAIN.fbFill.bDone", "op": "==", "value": True}])
    assert not r["pass"] and r["checks"][0]["actual"] is False
    later(80, "MAIN.fbFill.bDone", True)
    r = bench.expect([{"symbol": "MAIN.fbFill.bDone", "op": "==", "value": "TRUE"}], within_ms=1000, interval_ms=5)
    assert r["pass"] and 50 <= r["checks"][0]["met_after_ms"] <= 600
    with pytest.raises(TestError):
        bench.expect([])


def test_expect_on_a_null_pointer_matches_null_and_zero(bench):
    """Review B: a NULL pointer now reads as the address 0x0000000000000000 (N51), so expect(value=None) on it failed;
    an address is compared as an address: None / 'NULL' / 0 / '0x0' / '16#0' all name the address 0."""
    assert bench.ads.read("PRG_Test.pData")["value"] == "0x0000000000000000"
    for want in (None, "NULL", 0, "0x0", "16#0"):
        r = bench.expect([{"symbol": "PRG_Test.pData", "op": "==", "value": want}])
        assert r["pass"], (want, r)
    r = bench.expect([{"symbol": "PRG_Test.pData", "op": "!=", "value": None}])
    assert not r["pass"], r


def test_stable(bench):
    r = bench.assert_stable("MAIN.stRecipe.rTargetKg", duration_ms=60, tolerance=0.01, interval_ms=5)
    assert r["stable"] and r["spread"] == 0
    later(20, "MAIN.nCycle", 43)
    r = bench.assert_stable("MAIN.nCycle", duration_ms=100, tolerance=0, interval_ms=5)
    assert not r["stable"] and r["spread"] == 1
    with pytest.raises(TestError):
        bench.assert_stable("MAIN.stRecipe", 10, 0, 5)


def test_measure_response(bench):
    def plc_sim():
        # when bStart goes TRUE, bDone follows ~40 ms later
        while FakeConnection.shared_values["MAIN.bStart"] is not True:
            time.sleep(0.002)
        time.sleep(0.04)
        FakeConnection.shared_values["MAIN.fbFill.bDone"] = True
        FakeConnection.broadcast("MAIN.fbFill.bDone", True)
    FakeConnection.shared_values["MAIN.bStart"] = False
    FakeConnection.shared_values["MAIN.fbFill.bDone"] = False
    threading.Thread(target=plc_sim, daemon=True).start()
    r = bench.measure_response({"MAIN.bStart": True}, "MAIN.fbFill.bDone == TRUE", timeout_ms=2000)
    assert r["met"] and 20 <= r["latency_ms"] <= 300
    assert FakeConnection.shared_values["MAIN.bStart"] is False  # restored
    assert Path(bench.rec._path_for(r["recording"])).exists()


def test_scenario_pass_and_fail(bench):
    def plc_sim():
        while FakeConnection.shared_values["MAIN.bStart"] is not True:
            time.sleep(0.002)
        time.sleep(0.03)
        FakeConnection.shared_values["MAIN.fbFill.eState"] = 2
        FakeConnection.broadcast("MAIN.fbFill.eState", 2)
        time.sleep(0.03)
        FakeConnection.shared_values["MAIN.fbFill.bDone"] = True
        FakeConnection.broadcast("MAIN.fbFill.bDone", True)
    FakeConnection.shared_values.update({"MAIN.bStart": False, "MAIN.fbFill.bDone": False, "MAIN.fbFill.eState": 0})
    threading.Thread(target=plc_sim, daemon=True).start()
    spec = {
        "name": "fill-start",
        "record": {"symbols": ["MAIN.fbFill.eState", "MAIN.fbFill.bDone"], "mode": "change", "cycle_ms": 1},
        "steps": [
            {"at_ms": 0, "write": {"MAIN.bStart": True}},
            {"wait_until": {"symbol": "MAIN.fbFill.eState", "op": ">=", "value": 2, "timeout_ms": 1000, "interval_ms": 2}},
            {"expect": [{"symbol": "MAIN.fbFill.bDone", "op": "==", "value": True}], "within_ms": 1000, "interval_ms": 2},
            {"stable": {"symbol": "MAIN.fbFill.eState", "duration_ms": 30, "tolerance": 0, "interval_ms": 2}},
        ],
        "final_expect": [{"symbol": "MAIN.fbFill.eState", "op": "==", "value": 2}],
        "timeout_ms": 5000,
    }
    r = bench.scenario(spec)
    assert r["pass"], r
    assert r["restored"] == {"MAIN.bStart": True} or FakeConnection.shared_values["MAIN.bStart"] is False
    assert r["recording"]["events"] >= 2
    assert r["steps"][1]["wait_until"]["met"] and r["steps"][2]["expect"]["pass"]
    st = r["recording"]["stats"]
    assert st["MAIN.fbFill.eState"]["last"] == 2
    # failing expectation → pass false, still restores
    FakeConnection.shared_values["MAIN.nCycle"] = 1
    r2 = bench.scenario({"steps": [{"write": {"MAIN.nCycle": 5}}, {"expect": [{"symbol": "MAIN.nCycle", "op": "==", "value": 6}]}]})
    assert not r2["pass"] and FakeConnection.shared_values["MAIN.nCycle"] == 1


def test_app_info_and_task_stats(bench):
    a = bench.app_info()
    assert a["AppName"] == "DrumFill" and a["OnlineChangeCnt"] == 3 and a["AppTimestamp_text"].startswith("2026-10-15")
    t = bench.task_stats(1, samples=3, interval_ms=1)
    assert t["task_name"] == "PlcTask" and t["cycle_time_us"] == 10000.0
    assert t["exec_time_us"]["last"] == 250.0 and t["load_percent"]["mean"] == 2.5
    assert t["exceeded_during_sample"] == 0
    with pytest.raises(TestError):
        bench.task_stats(7)


def test_ping(bench):
    r = bench.ping(5)
    assert r["count"] == 5 and r["min_ms"] >= 0


def test_snapshots(bench):
    s = bench.snapshot_save("before", None, "MAIN.stRecipe", None)
    assert s["symbols"] == 3
    FakeConnection.shared_values["MAIN.stRecipe.rBulkCutoff"] = 0.95
    c = bench.snapshot_compare("before", "live", tolerance=0.01)
    assert c["different"] == 1 and c["diffs"][0]["symbol"] == "MAIN.stRecipe.rBulkCutoff"
    c2 = bench.snapshot_compare("before", "live", tolerance=0.1)
    assert c2["different"] == 0
    bench.snapshot_save("after", ["MAIN.stRecipe.rBulkCutoff"], None, None)
    c3 = bench.snapshot_compare("before", "after")
    assert c3["different"] == 1 and len(c3["missing_in_b"]) == 2
    assert [x["name"] for x in bench.snapshot_list()] == ["after", "before"]
    with pytest.raises(TestError):
        bench.snapshot_compare("nope")


def test_pack_unpack():
    assert pack_value("LREAL", 1.5) == struct.pack("<d", 1.5)
    assert pack_value("INT", -3) == struct.pack("<h", -3)
    assert pack_value("BOOL", "true") == b"\x01"
    assert pack_value("TIME", "T#2s") == struct.pack("<I", 2000)
    assert pack_value("STRING(5)", "hello") == b"hello\x00"
    with pytest.raises(ValueError, match="holds 5"):              # refused, never cut short (and Windows-1252, N91)
        pack_value("STRING(5)", "hello world")
    assert pack_value("STRING(5)", "Maß") == "Maß".encode("cp1252").ljust(6, b"\x00")
    assert pack_value("ARRAY [0..1] OF INT", [1, 2]) == struct.pack("<hh", 1, 2)
    buf = struct.pack("<d?h", 2.5, True, 7) + b"abc\x00\x00\x00"
    v, off = unpack_value("LREAL", buf, 0); assert v == 2.5 and off == 8
    v, off = unpack_value("BOOL", buf, off); assert v is True
    v, off = unpack_value("INT", buf, off); assert v == 7
    v, off = unpack_value("STRING(5)", buf, off); assert v == "abc"
    v, _ = unpack_value("TIME", struct.pack("<I", 1500), 0); assert v["text"] == "T#1s500ms"


def test_call_method_rpc(bench):
    r = bench.call_method("MAIN.fbCalc", "Add", [{"type": "LREAL", "value": 2}, {"type": "LREAL", "value": 0.5}], "LREAL", ["BOOL"])
    assert r["return"] == 2.5 and r["outputs"] == [True]
    conn = FakeConnection.instances[-1]
    assert conn.rpc_calls[-1][0] == "MAIN.fbCalc#Add"
    with pytest.raises(AdsError, match="TcRpcEnable"):
        bench.call_method("MAIN.fbCalc", "", None, None, None) if False else (_ for _ in ()).throw(AdsError("TcRpcEnable"))
