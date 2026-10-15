import json
import time
from pathlib import Path

import pytest

from twincat_mcp.config import AdsConfig
from twincat_mcp.ads_backend import AdsBackend, AdsError
from twincat_mcp.recorder import Recorder, RecorderError, FILETIME_EPOCH_OFFSET_100NS
from tests.fake_ads import install, FakeConnection


_BASE = int(time.time() * 1e7) + FILETIME_EPOCH_OFFSET_100NS


def ft(ms: float) -> int:
    """FILETIME for a relative time in ms on top of a fixed base."""
    return _BASE + int(ms * 1e4)


@pytest.fixture
def rec(monkeypatch, tmp_path):
    install(monkeypatch)
    ads = AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1"))
    return Recorder(ads, tmp_path / "recordings", ring_events=1000, max_runs=100)


def wait_until(pred, timeout=2.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _conn():
    # the recorder opens its own connection: the last FakeConnection instance
    return FakeConnection.instances[-1]


def test_expand_symbols(rec):
    s = rec.expand_symbols(None, "MAIN.fbFill", None, 500)
    assert [x.name for x in s] == ["MAIN.fbFill.arrWeights[0]", "MAIN.fbFill.arrWeights[1]", "MAIN.fbFill.arrWeights[2]",
                                   "MAIN.fbFill.arrWeights[3]", "MAIN.fbFill.bDone", "MAIN.fbFill.eState", "MAIN.fbFill.tSettle"]
    s = rec.expand_symbols(["MAIN.stRecipe", "MAIN.nCycle"], None, None, 500)
    assert {x.name for x in s} == {"MAIN.stRecipe.rTargetKg", "MAIN.stRecipe.rBulkCutoff", "MAIN.stRecipe.sName", "MAIN.nCycle"}
    # arrays of structs come from the datatype table, with offsets for raw access
    s = rec.expand_symbols(["MAIN.arrSteps"], None, None, 500)
    assert [x.name for x in s][:3] == ["MAIN.arrSteps[1].nId", "MAIN.arrSteps[1].tDwell", "MAIN.arrSteps[1].bDone"]
    assert s[4].index_offset - s[0].index_offset == 16
    everything = rec.expand_symbols(None, "MAIN", None, 500)
    assert len(everything) == 25
    s = rec.expand_symbols(None, None, "GVL_*", 500)
    assert [x.name for x in s] == ["GVL_Machine.nDrumsFilled"]
    with pytest.raises(AdsError, match="limit"):
        rec.expand_symbols(None, "MAIN", None, 2)
    with pytest.raises(AdsError, match="not found"):
        rec.expand_symbols(["Nope"], None, None, 5)


def test_change_mode_capture_stats_export_read(rec, tmp_path):
    st = rec.start(symbols=["MAIN.fbFill.eState", "MAIN.fbFill.bDone", "MAIN.fbFill.tSettle"], prefix=None, pattern=None,
                   mode="change", cycle_ms=10, duration_s=None, max_events=None, start_when=None, stop_when=None,
                   pre_trigger_ms=0, name="fill-cycle", max_symbols=500)
    rid = st["id"]
    assert st["active"] and st["symbols"] == 3
    c = _conn()
    assert len(c.notifications) == 3
    seq = [(0, "MAIN.fbFill.eState", 0), (0, "MAIN.fbFill.bDone", False), (0, "MAIN.fbFill.tSettle", 500),
           (100, "MAIN.fbFill.eState", 1), (350, "MAIN.fbFill.eState", 2), (350.5, "MAIN.fbFill.tSettle", 750),
           (900, "MAIN.fbFill.eState", 3), (900, "MAIN.fbFill.bDone", True), (1200, "MAIN.fbFill.bDone", False)]
    for ms, n, v in seq:
        c.fire(n, v, ft(ms))
    assert wait_until(lambda: rec.status(rid)["events"] == len(seq))
    s = rec.status(rid)
    assert s["last_values"]["MAIN.fbFill.eState"] == 3 and s["last_values"]["MAIN.fbFill.tSettle"]["text"] == "T#750ms"
    snap = rec.snapshot(rid, 3, None)["rows"]
    assert [r["symbol"] for r in snap] == ["MAIN.fbFill.eState", "MAIN.fbFill.bDone", "MAIN.fbFill.bDone"]
    assert abs(snap[-1]["t_ms"] - 1200) < 0.01
    stats = rec.stats(rid, None, True, 100)["symbols"]
    es = stats["MAIN.fbFill.eState"]
    assert es["count"] == 4 and es["changes"] == 3 and es["min"] == 0 and es["max"] == 3
    assert [r["value"] for r in es["runs"]] == [0, 1, 2, 3]
    assert abs(es["runs"][1]["dwell_ms"] - 250) < 0.01
    bd = stats["MAIN.fbFill.bDone"]
    assert bd["rising_edges"] == 1 and bd["falling_edges"] == 1 and abs(bd["first_edges"][0]["t_ms"] - 900) < 0.01
    assert stats["MAIN.fbFill.tSettle"]["max"] == 750
    out = rec.stop(rid)
    assert not out["active"] and out["stop_reason"] == "requested" and c.notifications == {}
    # file
    lines = Path(out["file"]).read_text().splitlines()
    head, foot = json.loads(lines[0]), json.loads(lines[-1])
    assert head["_header"] and head["name"] == "fill-cycle" and foot["events"] == len(seq)
    # read: long + window
    r = rec.read(rid, 300, 1000, None, 100, wide=False)
    assert [x["symbol"] for x in r["rows"]] == ["MAIN.fbFill.eState", "MAIN.fbFill.tSettle", "MAIN.fbFill.eState", "MAIN.fbFill.bDone"]
    # read: wide with forward fill from before the window
    w = rec.read(rid, 300, 1000, ["MAIN.fbFill.eState", "MAIN.fbFill.bDone"], 100, wide=True)
    assert w["rows"][0]["MAIN.fbFill.bDone"] is False and w["rows"][0]["MAIN.fbFill.eState"] == 2
    assert w["rows"][-1]["MAIN.fbFill.bDone"] is True
    # export
    e = rec.export(rid, "csv_wide", None, None)
    csv_lines = Path(e["file"]).read_text().splitlines()
    assert csv_lines[0] == "t_ms,MAIN.fbFill.eState,MAIN.fbFill.bDone,MAIN.fbFill.tSettle"
    assert csv_lines[-1].startswith("1200.0,3,False,750")
    e2 = rec.export(rid, "csv_long", None, ["MAIN.fbFill.eState"])
    assert e2["rows"] == 4
    lst = rec.list()
    assert lst["session"][0]["id"] == rid
    d = rec.delete(rid)
    assert len(d["removed"]) == 3 and not Path(out["file"]).exists()


def test_triggers_pre_and_stop(rec):
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.bStart", "MAIN.fbFill.bDone"], prefix=None, pattern=None, mode="cycle",
                   cycle_ms=10, duration_s=None, max_events=None, start_when="MAIN.bStart == TRUE",
                   stop_when="MAIN.fbFill.bDone == true", pre_trigger_ms=50, name=None, max_symbols=500)
    rid = st["id"]
    assert st["armed"]
    c = _conn()
    for ms in range(0, 200, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
    c.fire("MAIN.bStart", True, ft(200))
    for ms in range(210, 300, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
    c.fire("MAIN.fbFill.bDone", True, ft(300))
    assert wait_until(lambda: not rec.status(rid)["active"])
    s = rec.status(rid)
    assert s["stop_reason"].startswith("stop_when")
    assert not s["armed"] and abs(s["triggered_at_ms"] - 200) < 0.01
    r = rec.read(rid, None, None, ["MAIN.nCycle"], 1000, False)
    ts = [x["t_ms"] for x in r["rows"]]
    assert min(ts) >= 150 - 0.01 and 150 in [round(t) for t in ts]  # pre-trigger window kept
    assert 140 not in [round(t) for t in ts]
    rec.reap()
    assert c.notifications == {}


def test_max_events_limit_and_condition_errors(rec):
    with pytest.raises(RecorderError, match="not part of the recording"):
        rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="change", cycle_ms=10, duration_s=None,
                  max_events=None, start_when="MAIN.bStart == TRUE", stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
    with pytest.raises(RecorderError, match="mode must be"):
        rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="turbo", cycle_ms=10, duration_s=None,
                  max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="change", cycle_ms=10, duration_s=None,
                   max_events=3, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
    c = _conn()
    for i in range(6):
        c.fire("MAIN.nCycle", i, ft(i * 10))
    assert wait_until(lambda: not rec.status(st["id"])["active"])
    assert rec.status(st["id"])["stop_reason"] == "max_events 3"


def test_poll_mode(rec):
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.sLabel"], prefix=None, pattern=None, mode="poll", cycle_ms=5,
                   duration_s=None, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
    rid = st["id"]
    c = _conn()
    assert wait_until(lambda: rec.status(rid)["events"] >= 6)
    c.values["MAIN.nCycle"] = 99
    assert wait_until(lambda: rec.status(rid)["last_values"].get("MAIN.nCycle") == 99)
    out = rec.stop(rid)
    assert not out["active"] and out["stats"]["MAIN.nCycle"]["max"] == 99
    assert out["stats"]["MAIN.sLabel"]["last"] == "hello"
    assert out["events_per_s"] > 10


def test_duration_limit(rec):
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="poll", cycle_ms=5, duration_s=0.3,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
    assert wait_until(lambda: not rec.status(st["id"])["active"], timeout=3)
    assert rec.status(st["id"])["stop_reason"].startswith("duration")


def test_compare_recordings(rec):
    def record(values):
        st = rec.start(symbols=["MAIN.fbFill.bDone", "MAIN.nCycle"], prefix=None, pattern=None, mode="change", cycle_ms=10,
                       duration_s=None, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
        c = _conn()
        for ms, n, v in values:
            c.fire(n, v, ft(ms))
        assert wait_until(lambda: rec.status(st["id"])["events"] == len(values))
        rec.stop(st["id"])
        return st["id"]
    a = record([(0, "MAIN.fbFill.bDone", False), (0, "MAIN.nCycle", 1), (100, "MAIN.fbFill.bDone", True), (150, "MAIN.fbFill.bDone", False), (300, "MAIN.nCycle", 5)])
    b = record([(0, "MAIN.fbFill.bDone", False), (0, "MAIN.nCycle", 1), (100, "MAIN.fbFill.bDone", True), (180, "MAIN.fbFill.bDone", False), (300, "MAIN.nCycle", 9)])
    c = rec.compare(a, b)
    rows = {r["symbol"]: r for r in c["rows"]}
    assert c["common_symbols"] == 2
    assert rows["MAIN.nCycle"]["delta"]["max"] == 4
    assert rows["MAIN.fbFill.bDone"]["pulses"]["a"]["high_ms"]["mean"] == 50 and rows["MAIN.fbFill.bDone"]["pulses"]["b"]["high_ms"]["mean"] == 80
