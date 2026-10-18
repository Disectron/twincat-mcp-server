import json
import time
from pathlib import Path

import pytest

from twincat_mcp.config import AdsConfig
from twincat_mcp.ads_backend import AdsBackend, AdsError
from twincat_mcp import recorder as R
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
    assert sorted(x.name for x in s) == ["GVL_Machine.MAX_MODULES", "GVL_Machine.nDrumsFilled"]
    with pytest.raises(AdsError, match="limit"):
        rec.expand_symbols(None, "MAIN", None, 2)
    with pytest.raises(AdsError, match="not found"):
        rec.expand_symbols(["Nope"], None, None, 5)


def test_change_mode_capture_stats_export_read(rec, tmp_path):
    # the runtime's values at subscription time (a change notification delivers them first)
    FakeConnection.shared_values.update({"MAIN.fbFill.eState": 0, "MAIN.fbFill.bDone": False, "MAIN.fbFill.tSettle": 500})
    st = rec.start(symbols=["MAIN.fbFill.eState", "MAIN.fbFill.bDone", "MAIN.fbFill.tSettle"], prefix=None, pattern=None,
                   mode="change", cycle_ms=10, duration_s=None, max_events=None, start_when=None, stop_when=None,
                   pre_trigger_ms=0, name="fill-cycle", max_symbols=500)
    rid = st["id"]
    assert st["active"] and st["symbols"] == 3
    c = _conn()
    assert len(c.notifications) == 1          # three neighbouring leaves → one block notification
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
    # read: wide = one row per PLC timestamp, null where nothing was sampled (D9: no invented rows / values)
    w = rec.read(rid, 300, 1000, ["MAIN.fbFill.eState", "MAIN.fbFill.bDone"], 100, wide=True)
    assert [r["t_ms"] for r in w["rows"]] == [350.0, 900.0]                  # PLC timestamps of the selected symbols
    assert w["rows"][0]["MAIN.fbFill.eState"] == 2 and w["rows"][0]["MAIN.fbFill.bDone"] is None and "_filled" not in w["rows"][0]
    assert w["rows"][-1] == {"t_ms": 900.0, "MAIN.fbFill.eState": 3, "MAIN.fbFill.bDone": True}
    # fill=true: forward fill on request, every filled cell named
    wf = rec.read(rid, 300, 1000, ["MAIN.fbFill.eState", "MAIN.fbFill.bDone"], 100, wide=True, fill=True)
    assert wf["rows"][0]["MAIN.fbFill.bDone"] is False and wf["rows"][0]["_filled"] == ["MAIN.fbFill.bDone"]
    assert wf["rows"][-1]["_filled"] == [] and "forward-filled" in wf["note"]
    # export
    e = rec.export(rid, "csv_wide", None, None)
    csv_lines = Path(e["file"]).read_text().splitlines()
    assert csv_lines[0] == "t_ms,plc_filetime,time_utc,MAIN.fbFill.eState,MAIN.fbFill.bDone,MAIN.fbFill.tSettle"
    # one row per sample time; only what was sampled then (the PLC's own FILETIME per row)
    tu = lambda t: ",".join(map(str, R._ft_cols(ft(t))))          # plc_filetime,time_utc
    assert csv_lines[-1] == f"1200.0,{tu(1200)},,False,"
    assert f"900.0,{tu(900)},3,True," in csv_lines                # two changes at the same PLC time → one row
    ef = rec.export(rid, "csv_wide", str(tmp_path / "filled.csv"), None, fill=True)
    assert (tmp_path / "filled.csv").read_text().splitlines()[-1] == f"1200.0,{tu(1200)},3,False,750"
    # R10: every export carries a signed provenance file linking it to the exact source bytes and parameters
    pv = json.loads(R.provenance_path(tmp_path / "filled.csv").read_text(encoding="utf-8"))
    assert pv["source_sha256"] == R.file_sha256(Path(out["file"]))[0] and pv["params"]["fill"] is True
    assert pv["export_sha256"] == R.file_sha256(tmp_path / "filled.csv")[0] and pv["hmac"] and pv["rows"] == 6
    assert e["provenance"]["source_sha256"] == pv["source_sha256"] and e["provenance"]["file"].endswith(".wide.csv.provenance.json")
    assert e["rows"] == 6 and ef["rows"] == 6               # sample times 0, 100, 350, 350.5, 900, 1200
    e2 = rec.export(rid, "csv_long", None, ["MAIN.fbFill.eState"])
    assert e2["rows"] == 4
    lst = rec.list()
    assert lst["session"][0]["id"] == rid
    d = rec.delete(rid)
    assert len(d["removed"]) == 6 and not Path(out["file"]).exists()


def test_triggers_pre_and_stop(rec):
    FakeConnection.shared_values.update({"MAIN.bStart": False, "MAIN.fbFill.bDone": False})
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.bStart", "MAIN.fbFill.bDone"], prefix=None, pattern=None, mode="cycle",
                   cycle_ms=10, duration_s=None, max_events=None, start_when="MAIN.bStart == TRUE",
                   stop_when="MAIN.fbFill.bDone == true", pre_trigger_ms=50, name=None, max_symbols=500)
    rid = st["id"]
    assert st["armed"]
    c = _conn()
    cc = "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount"     # recorded automatically: its own block
    # cycle mode delivers every block every cycle (R41/N57: a level start_when waits for every block's first sample)
    for ms in range(0, 200, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        c.fire(cc, ms // 10, ft(ms))
    c.fire("MAIN.bStart", True, ft(200))
    c.fire(cc, 20, ft(200))
    for ms in range(210, 300, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        c.fire(cc, ms // 10, ft(ms))
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


def test_limit_ends_recording_promptly_even_while_notifications_keep_coming(rec):
    """Field report: rec_status stayed active ~11 s after 'stop_reason: duration', and rec_export had more rows
    than rec_status events. Cause: after the limit the ingest loop kept draining a queue the target kept filling,
    and notifications were only released at the next rec_list. Now the loop stops at the limit and the
    notifications are released from the ingest thread itself."""
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="change", cycle_ms=10, duration_s=None,
                   max_events=5, start_when=None, stop_when=None, pre_trigger_ms=0, name="limit", max_symbols=10)
    rid = st["id"]
    c = _conn()
    for i in range(200):                        # far more than the limit, delivered as fast as the DLL would
        c.fire("MAIN.nCycle", i, ft(i * 10))
    assert wait_until(lambda: not rec.status(rid)["active"], timeout=3.0)
    r = rec.recs[rid]
    assert r.conn is None and r.handles == [], "notifications must be released when the recording ends on its own"
    s = rec.status(rid)
    assert s["events"] == 5 and s["stop_reason"] == "max_events 5"
    # the file agrees with the status: footer events == rows exported
    exp = rec.export(rid, "csv_long", None, None)
    assert exp["rows"] == 5
    for i in range(200, 260):                   # late notifications after the end change nothing
        c.fire("MAIN.nCycle", i, ft(i * 10))
    assert rec.status(rid)["events"] == 5


def test_cycle_mode_duration_independent_of_plc_clock(rec):
    """Round 2, #1 re-check: rec_start(mode='cycle', duration_s=3) was still active at 17.8 s on the engineering PC —
    the cyclic notification stamps come from the task clock, which can be far from Windows time. Duration is now
    this PC's monotonic time from the first sample (or the PLC-relative span), never PLC FILETIME vs time.time()."""
    ahead = int(90 * 1e7)                       # PLC clock 90 s ahead of Windows
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=100, duration_s=0.5,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
    c = _conn()
    c.fire("MAIN.nCycle", 1, ft(0) + ahead)
    assert wait_until(lambda: not rec.status(st["id"])["active"], timeout=3)
    s = rec.status(st["id"])
    assert s["stop_reason"] == "duration 0.5s" and 0.3 < s["elapsed_s"] < 2.5
    # manual stop keeps its own reason even when a limit check races it
    st2 = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=100, duration_s=60,
                    max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10)
    _conn().fire("MAIN.nCycle", 2, ft(0) - ahead)   # and 90 s behind
    out = rec.stop(st2["id"], "manual (rec_stop)")
    assert out["stop_reason"] == "manual (rec_stop)"


def test_post_trigger_window(rec):
    """#69: post_trigger_s ends the recording that long after start_when fired (duration_s counts from the first sample)."""
    with pytest.raises(RecorderError, match="needs start_when"):
        rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=10, duration_s=None,
                  max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=10,
                  post_trigger_s=1)
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.bStart"], prefix=None, pattern=None, mode="cycle", cycle_ms=10,
                   duration_s=None, max_events=None, start_when="MAIN.bStart == TRUE", stop_when=None, pre_trigger_ms=0,
                   name=None, max_symbols=10, post_trigger_s=0.1)
    c = _conn()
    cc = "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount"     # recorded automatically: its own block
    c.fire(cc, 499, ft(4990))                                    # every block delivers (cycle mode)
    c.fire("MAIN.bStart", True, ft(5000))
    c.fire(cc, 500, ft(5000))
    for ms in range(5010, 5300, 10):
        c.fire("MAIN.nCycle", ms, ft(ms))
        c.fire(cc, ms // 10, ft(ms))
    assert wait_until(lambda: not rec.status(st["id"])["active"])
    s = rec.status(st["id"])
    assert s["stop_reason"].startswith("post_trigger 0.1s")
    r = rec.read(st["id"], None, None, ["MAIN.nCycle"], 1000, False)
    assert max(x["t_ms"] for x in r["rows"]) <= 100 + 10.01   # nothing recorded past the window (+1 sample)


# ---------------------------------------------------------------- recording accuracy (field report A1–A7)
def test_r3_header_carries_datatype_guid_and_layout(rec):
    """R3: the header records each symbol's datatype GUID and enum base type from the PLC's datatype table (struct
    member layout too when a struct is recorded whole), so the raw bytes can be decoded later without the PLC."""
    st = _cycle_rec(rec, symbols=["MAIN.nCycle", "MAIN.fbFill.eState", "MAIN.arrSteps"])
    out = rec.stop(st["id"], "manual (rec_stop)")
    head = json.loads(Path(out["file"]).read_text(encoding="utf-8").splitlines()[0])
    sy = {x["name"]: x for x in head["symbols"]}
    g = "{0E7A1C5D-3B2F-4C10-9A8B-1F2E3D4C5B6A}"
    for n in ("MAIN.fbFill.eState", "MAIN.arrSteps[2].eState"):        # struct members are recorded as leaves
        assert sy[n]["type_guid"] == g and sy[n]["base_type"] == "INT", sy[n]
    assert "type_guid" not in sy["MAIN.nCycle"]


def _cycle_rec(rec, **kw):
    args = dict(symbols=["MAIN.nCycle", "MAIN.bStart", "MAIN.fbFill.bDone"], prefix=None, pattern=None, mode="cycle",
                cycle_ms=1, duration_s=None, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0,
                name=None, max_symbols=50, cycle_counter=None)
    args.update(kw)
    return rec.start(**args)


def test_block_notification_gaps_are_counted_and_file_is_provable(rec):
    """A1/A7: one notification per contiguous block; holes in the PLC timeline are counted (never 'dropped: 0' with
    holes); rec_verify proves the file and checks a per-cycle counter."""
    st = _cycle_rec(rec)
    c = _conn()
    assert st["notifications"] == 1 and len(c.notifications) == 1 and st["time_source"] == "plc"
    sent = [i for i in range(1000) if not (300 <= i < 320) and i != 777]       # 21 cycles never arrive
    for i in sent:
        c.fire("MAIN.nCycle", 10_000 + i, ft(i))
    assert wait_until(lambda: rec.status(st["id"])["samples"] == len(sent))
    s = rec.status(st["id"])
    assert s["missing_samples"] == 21 and s["dropped"] == 0 and abs(s["completeness"] - len(sent) / 1000) < 1e-6
    assert s["blocks"][0]["first_gaps"][0] == [299.0, 20] and any("missing" in w for w in s["warnings"])
    out = rec.stop(st["id"], "manual (rec_stop)")
    assert out["missing_samples"] == 21
    # R5: the holes are in the file where they happened, identical format to the native engine
    gl = [json.loads(ln)["_gap"] for ln in Path(out["file"]).read_text(encoding="utf-8").splitlines() if ln.startswith('{"_gap"')]
    assert gl == [{"block": 0, "kind": "gap", "t_from": 299.0, "t_to": 320.0, "missing": 20},
                  {"block": 0, "kind": "gap", "t_from": 776.0, "t_to": 778.0, "missing": 1}]
    assert out["label"] == "INCOMPLETE"
    v = rec.verify(st["id"], counter="MAIN.nCycle")
    assert v["integrity"] == "intact" and v["timeline"]["missing"] == 21 and v["verdict"] == "FAIL"
    assert v["counter"]["missing_samples"] == 21 and v["counter"]["step"] == 1 and not v["counter"]["lossless"]
    assert v["t0"]["source"] == "plc" and v["t0"]["filetime"] == ft(0)
    # every sample carries the PLC's FILETIME exactly: t0 + t_ms * 1e4
    rows = rec.read(st["id"], None, None, ["MAIN.nCycle"], 5, False)["rows"]
    assert [v["t0"]["filetime"] + round(r["t_ms"] * 1e4) for r in rows] == [ft(i) for i in range(5)]
    # R9: sealed — sidecar manifest written, file and manifest read-only
    p = Path(out["file"])
    assert v["seal"] == "matches manifest" and v["read_only"] and R.is_read_only(R.manifest_path(p))
    m = json.loads(R.manifest_path(p).read_text(encoding="utf-8"))
    assert m["sha256_file"] == R.file_sha256(p)[0] and m["label"] == "INCOMPLETE" and m["hmac"]
    # tampering is detected (a deliberate edit has to clear read-only first)
    R.make_writable(p)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    i = next(k for k, ln in enumerate(lines) if ln.startswith("[500"))
    lines[i] = lines[i].replace("[500", "[501", 1)
    p.write_text("".join(lines), encoding="utf-8")
    v2 = rec.verify(st["id"])
    assert v2["integrity"].startswith("MODIFIED") and v2["seal"].startswith("FILE CHANGED SINCE SEALING")


def test_n13_duration_stop_waits_for_in_flight_tail(rec, monkeypatch):
    """N13: this PC's clock reaches the duration while the PLC samples up to the cut are still in flight. The
    recorder keeps the notifications until they have arrived, so a lossless run is COMPLETE, not FAIL by 3–10."""
    monkeypatch.setattr(R, "TAIL_WAIT_S", 5.0)       # a loaded CI runner must not turn the grace into the result
    st = _cycle_rec(rec, symbols=["MAIN.nCycle"], duration_s=0.4)
    c = _conn()
    for i in range(300):
        c.fire("MAIN.nCycle", i, ft(i))
    time.sleep(0.6)                                   # PC deadline passed; PLC samples 300..400 not delivered yet
    assert rec.status(st["id"])["active"]
    for i in range(300, 420):                         # the late tail arrives (and a few after the cut)
        c.fire("MAIN.nCycle", i, ft(i))
    assert wait_until(lambda: not rec.status(st["id"])["active"], 3.0)
    out = rec.stop(st["id"], "manual (rec_stop)")
    assert out["samples"] == 401 and out["missing_samples"] == 0 and out["label"] == "COMPLETE", out
    assert rec.verify(st["id"])["verdict"] == "PASS"


def test_n13_tail_wait_is_bounded(rec):
    st = _cycle_rec(rec, symbols=["MAIN.nCycle"], duration_s=0.3)
    c = _conn()
    for i in range(200):
        c.fire("MAIN.nCycle", i, ft(i))
    assert wait_until(lambda: not rec.status(st["id"])["active"], 0.3 + R.TAIL_WAIT_S + 1.5)
    out = rec.stop(st["id"], "manual (rec_stop)")
    assert out["label"] == "INCOMPLETE" and out["missing_samples"] == 100     # the tail that never came is counted


def test_d11_python_engine_marks_a_frozen_counter_as_stall(rec):
    """D11 (Python engine): notifications keep coming while the PLC is stopped, every value frozen. The cycle counter
    recorded with the data stops advancing → {"_stall"} marker, stalled samples not counted as data, INCOMPLETE."""
    st = _cycle_rec(rec, symbols=["MAIN.nCycle"], cycle_counter="MAIN.nCycle")
    c = _conn()
    for i in range(1000):
        c.fire("MAIN.nCycle", i if not 400 <= i <= 599 else 399, ft(i))
    assert wait_until(lambda: rec.status(st["id"])["samples"] == 1000)
    live = rec.status(st["id"])
    assert live["stalls"] == 1 and live["stalled_samples"] == 200 and not live["stalled"]
    out = rec.stop(st["id"], "manual (rec_stop)")
    assert out["missing_samples"] == 0 and out["label"] == "INCOMPLETE"
    assert out["label_reason"] == "PLC stopped (cycle counter frozen) 399.0000-600.0000 ms"
    assert abs(out["completeness"] - 800 / 1000) < 1e-6
    lines = Path(out["file"]).read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["_stall"] for ln in lines if ln.startswith('{"_stall"')] == [
        {"block": 0, "t_from": 399.0, "t_to": 600.0, "samples": 200}]
    foot = json.loads(lines[-1])
    assert foot["stalls"] == 1 and foot["stalled_samples"] == 200 and foot["stall_counter_block"] == 0
    v = rec.verify(st["id"], counter="MAIN.nCycle")
    # D12: 1000 samples over 800 distinct counter values (0..399 and 600..999 → expected 1000) — never above 100 %
    assert v["counter"]["duplicates"] == 200 and v["counter"]["completeness"] == 0.8 and "PLC stopped" in v["counter"]["note"]
    assert v["verdict"] == "FAIL" and v["footer"]["stalls"] == 1


def test_n29_n30_status_after_trigger_and_values_per_s(rec):
    FakeConnection.shared_values.update({"MAIN.bStart": False})
    st = _cycle_rec(rec, symbols=["MAIN.nCycle", "MAIN.bStart", "MAIN.fbFill.arrWeights"], start_when="MAIN.bStart == TRUE")
    assert st["values_per_sample"] == 6 and st["values_per_s"] == 6000.0       # N30: array elements counted
    c = _conn()
    c.fire("MAIN.bStart", True, ft(5))
    assert wait_until(lambda: rec.status(st["id"])["triggered_at_ms"] is not None)
    assert rec.status(st["id"])["armed"] is False                              # N29
    rec.stop(st["id"], "manual (rec_stop)")


def test_d7_delete_after_self_ended_recording_and_seal_sweep(rec):
    """D7: a recording that ended by itself is sealed (read-only) by its own thread; deleting right after must wait
    for that and succeed. N27: an unsealed finished recording is sealed by the start-up sweep."""
    st = _cycle_rec(rec, symbols=["MAIN.nCycle"], max_events=50)
    c = _conn()
    for i in range(60):
        c.fire("MAIN.nCycle", i, ft(i))
    p = Path(rec.recs[st["id"]].path)
    assert rec.delete(st["id"])["removed"] and not p.exists() and not R.manifest_path(p).exists()
    # sweep: a finished file without manifest (server died before sealing) gets sealed; an unfinished one does not
    st2 = _cycle_rec(rec, symbols=["MAIN.nCycle"])
    c = _conn()
    for i in range(10):
        c.fire("MAIN.nCycle", i, ft(i))
    out = rec.stop(st2["id"], "manual (rec_stop)")
    p2 = Path(out["file"])
    R.make_writable(R.manifest_path(p2)); R.manifest_path(p2).unlink()
    half = p2.with_name("20990101-000000-dead.jsonl")
    half.write_text('{"_header": true, "format": 2}\n[0.0,"#0","AAAA"]\n', encoding="utf-8")
    sw = rec.seal_unsealed()
    assert sw["sealed"] == [p2.stem] and sw["unfinished"] == 1 and R.manifest_path(p2).exists()
    assert not R.manifest_path(half).exists()


def test_lossless_run_passes_verification(rec):
    st = _cycle_rec(rec)
    c = _conn()
    for i in range(2000):
        c.fire("MAIN.nCycle", i, ft(i))
    out = rec.stop(st["id"], "manual (rec_stop)")
    # the stop drained the backlog instead of discarding it (A2)
    assert out["samples"] == 2000 and out["dropped"] == 0 and out["dropped_on_stop"] == 0
    v = rec.verify(st["id"], counter="MAIN.nCycle")
    assert v["verdict"] == "PASS" and v["counter"]["lossless"] and v["timeline"]["completeness"] == 1.0
    # exact statistics from the file match the live ones
    live = rec.stats(st["id"], ["MAIN.nCycle"], False, 0)
    exact = rec.stats(st["id"], ["MAIN.nCycle"], False, 0, exact=True)
    assert exact["source"] == "file" and exact["symbols"]["MAIN.nCycle"]["count"] == 2000
    assert live["symbols"]["MAIN.nCycle"]["max"] == exact["symbols"]["MAIN.nCycle"]["max"] == 1999


def test_partial_subscription_fails_unless_allowed(rec):
    """A3: a notification that cannot be registered fails the start instead of leaving a silent hole.
    MAIN.nCycle (offset 0) and MAIN.fbFill.bDone (offset 320) are too far apart to share a block."""
    FakeConnection.fail_offsets = {5 * 64}
    try:
        kw = dict(symbols=["MAIN.nCycle", "MAIN.fbFill.bDone"], prefix=None, pattern=None, mode="cycle", cycle_ms=1,
                  duration_s=None, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=50,
                  cycle_counter=None)
        with pytest.raises(AdsError, match="1 of 2 notifications could not be registered"):
            rec.start(**kw)
        st = rec.start(**kw, allow_partial=True)
    finally:
        FakeConnection.fail_offsets = None
    assert st["unsubscribed_symbols"] == ["MAIN.fbFill.bDone"] and any("could not be subscribed" in w for w in st["warnings"])
    rec.stop(st["id"])


def test_duration_cut_by_plc_time_and_pattern_expands(rec):
    """A2: duration is PLC time — samples queued within it are written, later ones are not part of the recording.
    A5: a pattern that matches a composite records its leaves (as rec_scan lists them)."""
    st = _cycle_rec(rec, duration_s=0.5)
    c = _conn()
    for i in range(1000):
        c.fire("MAIN.nCycle", i, ft(i))
    assert wait_until(lambda: not rec.status(st["id"])["active"])
    s = rec.status(st["id"])
    assert s["stop_reason"] == "duration 0.5s" and s["samples"] == 501 and s["plc_span_s"] == 0.5
    st2 = rec.start(symbols=None, prefix=None, pattern="MAIN.arrSteps", mode="change", cycle_ms=10, duration_s=None,
                    max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=50)
    assert st2["leaves"] == 8 and st2["notifications"] == 1
    rec.stop(st2["id"])



def test_cycle_mode_records_counter_and_manifest(rec):
    """Provenance: a cycle-mode recording carries the per-cycle task counter (ground truth for rec_verify) and a
    manifest of what it was taken from (application timestamp / online changes, tasks, TwinCAT build)."""
    st = _cycle_rec(rec, cycle_counter="auto", cycle_ms=10)
    assert st["cycle_counter"] == "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount" and st["notifications"] == 2
    out = rec.stop(st["id"])
    head = json.loads(Path(out["file"]).read_text().splitlines()[0])
    m = head["manifest"]
    assert head["cycle_counter"] == st["cycle_counter"] and m["app"]["OnlineChangeCnt"] == 3
    assert m["tasks"][0]["cycle_time_ms"] == 10.0 and m["device"]["version"].startswith("3.1")
    v = rec.verify(st["id"])
    assert v["counter_source"] == "recorded automatically" and v["manifest"]["tasks"][0]["name"] == "PlcTask"
    with pytest.raises(AdsError):
        _cycle_rec(rec, cycle_counter="MAIN.nope")


def test_high_rate_recording_defers_decoding_and_counts_the_tail(rec):
    """Retest 0.7.1: (1) above 20,000 values/s nothing is decoded on the ingest thread (it starved the ADS callback);
    statistics come from the file. (2) N11: a recording that goes quiet before its window ends counts the tail."""
    st = _cycle_rec(rec, cycle_ms=0.1)                     # 3 leaves × 10,000 samples/s = 30,000 values/s
    c = _conn()
    for i in range(100):
        c.fire("MAIN.nCycle", i, ft(i * 0.1))
    assert wait_until(lambda: rec.status(st["id"])["samples"] == 100)
    s = rec.status(st["id"])
    assert str(s["stats_coverage"]).startswith("deferred") and s["last_values"]["MAIN.nCycle"] == 99
    out = rec.stop(st["id"], "manual (rec_stop)")
    assert out["stats"] is None and "rec_stats" in out["stats_note"]
    exact = rec.stats(st["id"], ["MAIN.nCycle"], False, 0)
    assert exact["source"] == "file" and exact["symbols"]["MAIN.nCycle"]["count"] == 100
    # N11: 0.5 s window, the block delivers only the first 200 ms
    st2 = _cycle_rec(rec, duration_s=0.5)
    c = _conn()
    for i in range(200):
        c.fire("MAIN.nCycle", i, ft(i))
    assert wait_until(lambda: not rec.status(st2["id"])["active"], timeout=4)
    s2 = rec.status(st2["id"])
    assert s2["stop_reason"] == "duration 0.5s" and s2["missing_samples"] == 300 and s2["blocks"][0]["missing_tail"] == 300
    v = rec.verify(st2["id"])
    assert v["timeline"]["missing"] == 300 and v["verdict"] == "FAIL"


def test_long_array_values_are_trimmed_in_responses(rec):
    """P1: a 300-element array in rec_status / stats is summarised, not dumped."""
    from twincat_mcp.recorder import trim_value
    assert trim_value(list(range(300))) == {"len": 300, "first": [0, 1, 2, 3, 4], "last": [298, 299]}
    assert trim_value([1, 2]) == [1, 2]


def test_trigger_on_array_element_and_edge(rec):
    """Retest 0.7.1: a trigger may name one element of an array recorded as a whole; edge_triggers fires only on the
    transition, not on a condition that already holds when the recording starts."""
    FakeConnection.shared_values.update({"MAIN.fbFill.arrWeights": [1.0, 2.5, 0.0, -1.0]})
    st = rec.start(symbols=["MAIN.fbFill.arrWeights"], prefix=None, pattern=None, mode="change", cycle_ms=1,
                   duration_s=None, max_events=None, start_when="MAIN.fbFill.arrWeights[2] > 0.5", stop_when=None,
                   pre_trigger_ms=0, name=None, max_symbols=10, edge_triggers=True)
    c = _conn()
    c.fire("MAIN.fbFill.arrWeights", [1.0, 2.5, 0.0, -1.0], ft(0))      # element [2] = 0.0: not yet
    c.fire("MAIN.fbFill.arrWeights", [1.0, 2.5, 0.9, -1.0], ft(10))     # becomes > 0.5 → trigger
    assert wait_until(lambda: not rec.status(st["id"])["armed"])
    assert abs(rec.status(st["id"])["triggered_at_ms"] - 10) < 0.01
    rec.stop(st["id"])
    # level (default): already true → fires on the first sample; edge: never fires while it stays true
    st2 = rec.start(symbols=["MAIN.fbFill.arrWeights"], prefix=None, pattern=None, mode="change", cycle_ms=1,
                    duration_s=None, max_events=None, start_when="MAIN.fbFill.arrWeights[2] > 0.5", stop_when=None,
                    pre_trigger_ms=0, name=None, max_symbols=10, edge_triggers=True)
    c = _conn()
    c.fire("MAIN.fbFill.arrWeights", [1.0, 2.5, 0.9, -1.0], ft(20))
    c.fire("MAIN.fbFill.arrWeights", [1.0, 2.5, 0.95, -1.0], ft(30))
    time.sleep(0.3)
    assert rec.status(st2["id"])["armed"]
    rec.stop(st2["id"])
    with pytest.raises(RecorderError, match="index outside"):
        rec.start(symbols=["MAIN.fbFill.arrWeights"], prefix=None, pattern=None, mode="change", cycle_ms=1, duration_s=None,
                  max_events=None, start_when="MAIN.fbFill.arrWeights[9] > 0", stop_when=None, pre_trigger_ms=0, name=None,
                  max_symbols=10)


def test_a5_pattern_selects_the_same_leaves_in_rec_start_and_rec_scan(monkeypatch, project_dir):
    """A5: rec_start matched patterns against root symbols only ('*bOpen' → nothing, 'GVL_Sim.aCh[[]1].*' → nothing)
    while rec_scan matched leaves. D31: prefix on one element of an array of structs → 'matches no symbol'."""
    from tests.fake_ads import install
    from tests.test_server_e2e import mk_cfg
    from twincat_mcp.recorder import pattern_leaves, selection_regex, path_selected
    from twincat_mcp.server import App
    install(monkeypatch)
    app = App(mk_cfg(project_dir))
    rx = selection_regex("MAIN.arrSteps[1].*")
    assert path_selected(rx, "MAIN.arrSteps[1].nId") and not path_selected(rx, "MAIN.arrSteps[2].nId")
    assert path_selected(selection_regex("main.fbfill"), "MAIN.fbFill.arrWeights[3]")
    for pat in ("*bOpen", "MAIN.arrSteps[1].*", "MAIN.*", "MAIN.fbValve2", "*.arrHist[1]"):
        started = sorted(s.name for s in app.rec.expand_symbols(None, None, pat, 500))
        scanned = sorted(lf.path for _r, lvs in pattern_leaves(app.ads, pat, 5000) for lf in lvs)
        assert started == scanned and started, (pat, started, scanned)
    assert sorted(s.name for s in app.rec.expand_symbols(None, None, "*bOpen", 500)) == ["MAIN.fbValve2.bOpen"]
    assert [s.name for s in app.rec.expand_symbols(None, None, "*.arrHist[1]", 500)] == ["MAIN.fbValve2.arrHist[1]"]
    # system lists only when named
    assert not any(s.name.startswith("TwinCAT_") for s in app.rec.expand_symbols(None, None, "*", 500))
    # D31: one element of an array of structs as prefix
    one = sorted(s.name for s in app.rec.expand_symbols(None, "MAIN.arrSteps[2]", None, 500))
    assert one == ["MAIN.arrSteps[2].bDone", "MAIN.arrSteps[2].eState", "MAIN.arrSteps[2].nId", "MAIN.arrSteps[2].tDwell"], one
    with pytest.raises(Exception, match="matches no symbol"):
        app.rec.expand_symbols(None, "MAIN.arrSteps[9]", None, 500)


def test_d34_d35_latency_ignores_initial_state_and_says_how_far_timestamps_hold(rec, monkeypatch):
    """D34: the condition already true at the first sample (edge before the recording) produced a '1 ms' latency
    from the two first samples. D35: separate change-mode notifications are checked at different phases."""
    rid = "lat-test"
    rec.root.mkdir(parents=True, exist_ok=True)
    head = {"_header": True, "format": 2, "id": rid, "mode": "change", "cycle_ms": 10,
            "symbols": [{"id": 0, "name": "MAIN.bPulse"}, {"id": 1, "name": "MAIN.bDone"}, {"id": 2, "name": "MAIN.st"}],
            "blocks": [{"id": 0, "slots": [[0, 0, 1]]}, {"id": 1, "slots": [[1, 0, 1]]}, {"id": 2, "slots": [[2, 0, 8]]}]}
    (rec.root / f"{rid}.jsonl").write_text(json.dumps(head) + "\n", encoding="utf-8")
    ev = [{"t_ms": 0.0, "symbol": "MAIN.bPulse", "value": True}, {"t_ms": 1.0, "symbol": "MAIN.bDone", "value": True},
          {"t_ms": 50.0, "symbol": "MAIN.bPulse", "value": False}, {"t_ms": 60.0, "symbol": "MAIN.bDone", "value": False}]
    monkeypatch.setattr(rec, "_events_for", lambda r, syms: list(ev))
    r = rec.latency(rid, "MAIN.bPulse", "==", True, "MAIN.bDone", "==", True)
    assert r["pairs"] == 0 and r["from_true_at_start"] and r["to_true_at_start"] and "before the recording" in r["start_note"]
    ev += [{"t_ms": 100.0, "symbol": "MAIN.bPulse", "value": True}, {"t_ms": 120.0, "symbol": "MAIN.bDone", "value": True}]
    r = rec.latency(rid, "MAIN.bPulse", "==", True, "MAIN.bDone", "==", True)
    assert r["pairs"] == 1 and r["samples"][0]["latency_ms"] == 20.0
    assert rec.latency(rid, "MAIN.bPulse", "==", True, "MAIN.bDone", "==", True, t_to=110.0)["pairs"] == 0   # D33 window
    t = r["timing"]
    assert t["trust"] == "unreliable" and not t["same_block"] and "reordered" in t["why"]
    assert rec.timing_of(rid, ["MAIN.st.a", "MAIN.st.b"])["trust"] == "quantised"     # one notification
    head["mode"] = "cycle"
    (rec.root / f"{rid}.jsonl").write_text(json.dumps(head) + "\n", encoding="utf-8")
    assert rec.timing_of(rid, ["MAIN.bPulse", "MAIN.bDone"])["trust"] == "exact"


def test_d26_duty_over_the_pulse_train_not_the_idle_lead_in(rec, monkeypatch):
    """D26: duty_percent over the whole span incl. the idle time before the first pulse (30.6 % instead of ≈ 54 %)."""
    ev = [{"t_ms": 0.0, "symbol": "B", "value": False}]
    t = 500.0                                                      # 500 ms idle lead-in
    for _ in range(5):
        ev += [{"t_ms": t, "symbol": "B", "value": True}, {"t_ms": t + 54.0, "symbol": "B", "value": False}]
        t += 100.0
    ev += [{"t_ms": t + 300.0, "symbol": "B", "value": False}]
    monkeypatch.setattr(rec, "_events_for", lambda r, syms: list(ev))
    p = rec.pulses("x", "B")
    assert p["pulses"] == 5 and p["duty_percent"] == 54.0 and p["period_ms"]["mean"] == 100.0
    assert p["high_share_of_recording_percent"] < 30
    # a recording that starts while the signal is high: that first run is not a pulse of unknown width
    ev2 = [{"t_ms": 0.0, "symbol": "B", "value": True}, {"t_ms": 7.0, "symbol": "B", "value": False}] + ev[1:]
    monkeypatch.setattr(rec, "_events_for", lambda r, syms: list(ev2))
    assert rec.pulses("x", "B")["pulses"] == 5


def test_r9_r10_nonsense_parameters_and_conditions_are_refused(rec):
    """R9: cycle_ms=-5 became 0.1 ms and duration_s=-1 meant no end. R10: 'X >>> import os' was accepted as '>'
    against a string and never fired."""
    base = dict(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=None,
                max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="r9", max_symbols=50)
    for bad, msg in (({"cycle_ms": -5}, "cycle_ms -5 must be between"), ({"duration_s": -1}, "duration_s -1 must be > 0"),
                     ({"cycle_ms": 0}, "cycle_ms 0 must be"), ({"max_events": 0}, "max_events 0"),
                     ({"pre_trigger_ms": -1}, "pre_trigger_ms -1")):
        with pytest.raises(RecorderError, match=msg):
            rec.start(**{**base, **bad})
    assert not rec.recs, "nothing was started"
    for cond, msg in (("MAIN.nCycle >>> import os", "operator not understood"),
                      ("MAIN.nCycle > banana", "not a valid UDINT value"),
                      ("MAIN.nCycle == 1.5", "not a valid UDINT value")):
        with pytest.raises(RecorderError, match=msg):
            rec.start(**{**base, "mode": "change", "stop_when": cond})
    ok = rec.start(**{**base, "mode": "change", "stop_when": "MAIN.nCycle >= 10"})
    rec.stop(ok["id"])


def test_n31_cycle_below_the_fastest_task_is_flagged(rec):
    """N31: cycle_ms=0.1 on a 1 ms task was accepted without a word ('20000 notifications/s'). The fake's task runs
    every 10 ms."""
    base = dict(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=None,
                max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="n31", max_symbols=50)
    st = rec.start(**base)
    rec.stop(st["id"])
    # no project task configuration here: compared with the fastest task, and the warning says so (N62)
    assert "below the cycle of MAIN (no single task) (10 ms" in st["warning"] and st["fastest_task_ms"] == 10.0
    assert "task configuration is not available" in st["warning"] and "use cycle_ms=10" in st["warning"]
    st = rec.start(**{**base, "cycle_ms": 10})
    rec.stop(st["id"])
    assert "below the cycle" not in st.get("warning", "") and "fastest_task_ms" not in st


def test_r13_snapshot_of_a_recording_from_an_earlier_session(rec, monkeypatch, tmp_path):
    """R13: rec_snapshot of a recording made before a server restart → 'unknown in this session'."""
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="poll", cycle_ms=10, duration_s=None,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="r13", max_symbols=50)
    time.sleep(0.3)
    rec.stop(st["id"])
    fresh = Recorder(rec.ads, rec.root, ring_events=1000, max_runs=100)          # "after a restart"
    snap = fresh.snapshot(st["id"], 5, None)
    assert snap["active"] is False and "earlier server session" in snap["source"] and snap["rows"]


def test_d46_counter_check_segments_at_a_restart(rec, monkeypatch):
    """D46: CycleCount restarts at 0 after a TwinCAT restart → expected_samples −165,220, completeness −0.19."""
    from twincat_mcp import recorder as R
    ev = [(float(i), "C", 165_000 + i) for i in range(300)] + [(400.0 + i, "C", i + 1) for i in range(200)]
    monkeypatch.setattr(R, "iter_events", lambda path, want=None: iter(ev))
    monkeypatch.setattr(rec, "_path_for", lambda rid: Path(__file__))
    monkeypatch.setattr(R, "read_footer", lambda p: {})
    monkeypatch.setattr(R, "_read_header", lambda p: {"mode": "change"})
    try:
        v = rec.verify("x", counter="C")
    except Exception:
        pytest.skip("verify needs the integrity part of a real file here")
    c = v["counter"]
    assert c["expected_samples"] == 500 and c["completeness"] == 1.0 and c["segments"] == 2, c
    assert c["resets"][0]["from"] == 165_299 and c["resets"][0]["to"] == 1 and c["lossless_within_segments"]
    assert c["lossless"] is False


# ---------------------------------------------------------------- 0.8.9 retest: N56–N63 (root fixes)
CC = "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount"


def _two_blocks(rec, **kw):
    """MAIN.nCycle and the task's CycleCount: two notification blocks (64-byte slots far apart)."""
    args = dict(symbols=["MAIN.nCycle", CC], prefix=None, pattern=None, mode="cycle", cycle_ms=10, duration_s=None,
                max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name=None, max_symbols=50,
                cycle_counter=None)
    args.update(kw)
    return rec.start(**args)


def test_n60_tail_and_head_counts_are_integer_ticks_like_the_engine():
    """N60: (0.6 − 0.3) / 0.1 is 2.9999999999999996 in float ms → one missing sample less than the engine's
    floor(3000 / 1000) − 1 on 100 ns ticks for the same file."""
    b = R.Block(0, 0x4040, 0, 4, [])
    b.first_t, b.last_t, b.min_step, b.samples = 0.0, 0.3, 0.1, 4
    assert b.tail_missing(0.6) == 2                       # engine: (6000 − 3000) // 1000 − 1
    assert b.head_missing(-0.3) == 3                      # engine: (0 − (−3000)) // 1000
    assert b.missing(0.6, None) == 2


def test_block_with_fewer_than_two_samples_is_counted_with_a_borrowed_period():
    """Review B: a block with one sample or none has no period of its own — it was counted as 0 missing. The other
    blocks' smallest period stands in (engine step_of); a block that never delivered misses the whole window."""
    one = R.Block(1, 0x4040, 0, 4, [])
    one.note_stamp(0.0)
    assert one.missing(300.0, None) == 0                  # no period at all: nothing can be counted …
    assert R.unprovable_blocks([one], None) == [1]        # … so nothing can be proven either
    assert one.missing(300.0, None, fallback=10.0) == 29  # (300 − 0) // 10 − 1, the engine's tail rule
    assert R.unprovable_blocks([one], 10.0) == []
    none = R.Block(2, 0x4040, 64, 4, [])
    assert none.missing(300.0, None, fallback=10.0, origin=0.0) == 31   # (300 − 0) // 10 + 1, the engine's rule
    assert none.missing(300.0, None, fallback=10.0, origin=None) == 0
    assert R.unprovable_blocks([none], 10.0) == [2]
    b = R.Block(3, 0x4040, 128, 4, [])
    for t in (0.0, 10.0, 20.0):
        b.note_stamp(t)
    assert R.fallback_period([one, none, b], 50.0) == 10.0 and R.fallback_period([one, none], 50.0) == 50.0
    assert R.window_origin([one, none, b], None) == 0.0 and R.window_origin([b], -5.0) == -5.0


@pytest.mark.parametrize("cc_samples", [0, 1])
def test_python_recording_with_a_block_that_delivered_once_or_never_is_incomplete(rec, cc_samples):
    """Review B: the Python recorder and rec_verify read a two-block recording as COMPLETE / PASS when one block
    delivered a single sample or none; the native engine counts the window. Both must be INCOMPLETE / FAIL."""
    st = _two_blocks(rec)
    c = _conn()
    for ms in range(0, 310, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        if cc_samples and ms == 0:
            c.fire(CC, 0, ft(0))
    assert wait_until(lambda: rec.status(st["id"])["samples"] >= 31 + cc_samples)
    live = rec.status(st["id"])
    assert live["label"].startswith("INCOMPLETE"), live
    if not cc_samples:
        assert any("have not delivered" in w for w in live["warnings"]), live["warnings"]
    out = rec.stop(st["id"], "manual (rec_stop)")
    footer = json.loads(Path(out["file"]).read_text(encoding="utf-8").splitlines()[-1])
    want = 31 if cc_samples == 0 else 29                  # whole window (end − origin) // 10 + 1, or the tail rule
    assert footer["label"] == "INCOMPLETE" and footer["missing_samples"] == want, footer
    v = rec.verify(st["id"])
    blk = {b["first"]: b for b in v["timeline"]["blocks"]}
    assert blk[CC]["missing_tail"] == want and "period_note" in blk[CC], blk
    assert v["verdict"] == "FAIL", v
    if not cc_samples:
        assert v["timeline"]["unprovable_blocks"] == [blk[CC]["block"]]


def test_python_stop_when_ends_at_the_firing_sample_and_waits_for_the_other_blocks(rec):
    """Python stop_when cut the queue at once, but samples arrive in ADS order: the other block's samples up to the
    firing PLC time were still on their way and its tail read as missing. The end is the firing sample's PLC time and
    every block is waited for up to it (the native engine writes in PLC order, so it reaches that time already)."""
    st = _two_blocks(rec, stop_when="MAIN.nCycle >= 20")
    c = _conn()
    for ms in range(0, 210, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))          # stop_when fires at 200 ms …
    assert wait_until(lambda: rec.recs[st["id"]].stop_at_cut)
    for ms in range(0, 310, 10):
        c.fire(CC, ms // 10, ft(ms))                      # … the counter block's batch arrives afterwards
    assert wait_until(lambda: not rec.status(st["id"])["active"], timeout=5)
    out = rec.stop(st["id"])
    footer = json.loads(Path(out["file"]).read_text(encoding="utf-8").splitlines()[-1])
    assert footer["window_end_ms"] == 200.0 and footer["stop_at"]["by"] == "stop_when", footer
    assert footer["label"] == "COMPLETE" and footer["missing_samples"] == 0 and footer["after_limit"] == 10, footer
    assert footer["stop_reason"].startswith("stop_when")
    v = rec.verify(st["id"])
    assert v["verdict"] == "PASS" and "tail_rule" not in v, v


def test_n56_python_stop_on_request_ends_at_a_plc_time_and_a_block_that_stopped_early_fails(rec):
    """N56 (Python engine): rec_stop ends at the newest PLC time received; every block must reach it. A block that
    stopped delivering 100 ms before the stop has lost those samples — FAIL, in the footer and in rec_verify."""
    st = _two_blocks(rec)
    c = _conn()
    for ms in range(0, 310, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        if ms <= 200:
            c.fire(CC, ms // 10, ft(ms))                  # the counter block goes quiet at 200 ms
    assert wait_until(lambda: rec.status(st["id"])["samples"] >= 52)
    out = rec.stop(st["id"], "manual (rec_stop)")
    footer = json.loads(Path(out["file"]).read_text(encoding="utf-8").splitlines()[-1])
    assert footer["window_end_ms"] == 300.0 and footer["stop_on_request"]["at_ms"] == 300.0, footer
    assert footer["label"] == "INCOMPLETE" and footer["missing_samples"] == 9, footer      # (300 − 200) / 10 − 1
    v = rec.verify(st["id"])
    blk = {b["first"]: b for b in v["timeline"]["blocks"]}
    assert blk[CC]["missing_tail"] == 9 and blk["MAIN.nCycle"]["missing_tail"] == 0
    assert v["verdict"] == "FAIL" and v["block_ends"]["first_to_end"] == blk[CC]["block"], v


def test_n56_python_lossless_stop_on_request_passes_with_allowance_one(rec):
    st = _two_blocks(rec)
    c = _conn()
    for ms in range(0, 310, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        c.fire(CC, ms // 10, ft(ms))
    assert wait_until(lambda: rec.status(st["id"])["samples"] >= 62)
    out = rec.stop(st["id"], "manual (rec_stop)")
    assert out["missing_samples"] == 0 and out["label"] == "COMPLETE", out
    v = rec.verify(st["id"])
    assert v["verdict"] == "PASS" and v["timeline"]["window_end_ms"] == 300.0 and v["block_ends"]["spread_ms"] == 0


@pytest.mark.parametrize("legacy_footer", [
    # ≤ 0.8.9 native: manual_stop, and the LATEST block end as window_end_ms (review B: the old test set the end to
    # None, which no real file has, so the legacy rule was never reached for a real 0.8.9 file)
    {"engine": "native", "manual_stop": True, "window_end_ms": 300.0, "max_delay_ms": 1000.0, "verdict_hint": "PASS",
     "label": "COMPLETE"},
    # ≤ 0.8.9 Python: no window end, no stop_on_request
    {"engine": "python", "window_end_ms": None, "stop_on_request": None},
])
def test_n56_verify_of_a_legacy_stop_without_plc_end_is_inconclusive_not_pass(rec, monkeypatch, legacy_footer):
    """A file written by an engine that unsubscribed at once (≤ 0.8.9): the blocks end where their unsubscription
    happened to be. rec_verify checks up to the earliest end and says the rest is not verified — it does not lend
    the engine's max_delay allowance (900 lost samples passed)."""
    st = _two_blocks(rec)
    c = _conn()
    for ms in range(0, 310, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        if ms <= 200:
            c.fire(CC, ms // 10, ft(ms))
    assert wait_until(lambda: rec.status(st["id"])["samples"] >= 52)
    rec.recs[st["id"]].request_stop("legacy")            # stop now, no PLC end time
    rec.stop(st["id"])
    real = rec._integrity

    def legacy(path):
        out = real(path)
        out["_footer_raw"] = {**(out["_footer_raw"] or {}), **legacy_footer}
        return out
    monkeypatch.setattr(rec, "_integrity", legacy)
    v = rec.verify(st["id"])
    assert v["timeline"]["window_end_ms"] == 200.0 and v["timeline"]["missing"] == 0
    assert v["verdict"] == "INCONCLUSIVE" and "100 ms" in v["inconclusive"], v
    assert v["block_ends"]["spread_ms"] == 100.0


def test_n56_present_engines_footers_are_not_legacy():
    assert not R.end_not_waited_for({"engine": "native", "manual_stop": False, "window_end_ms": 300.0})
    assert not R.end_not_waited_for({"manual_stop": True, "stop_on_request": {"at_ms": 3.0}, "window_end_ms": 3.0})
    assert not R.end_not_waited_for({"engine": "python", "window_end_ms": 3.0, "stop_at": {"by": "stop_when"}})
    assert not R.end_not_waited_for({"engine": "python", "window_end_ms": 1000.0})           # duration cut
    assert R.end_not_waited_for({"engine": "native", "manual_stop": True, "window_end_ms": 300.0})
    assert R.end_not_waited_for({})                                                           # no footer


def test_n57_n63_python_trigger_keeps_the_trigger_cycle_when_the_trigger_block_is_batched(rec):
    """N57: the trigger block's samples come in batches (max_delay 100 ms); the other block runs ahead. A fixed 50 ms
    pre-trigger margin purged the other block's samples of the trigger cycle before the trigger was seen, and with
    pre_trigger_ms 0 there was no head check. Both blocks must start at trigger − pre_trigger_ms."""
    for pre in (0, 50):
        st = _two_blocks(rec, start_when=f"{CC} >= 30", pre_trigger_ms=pre, max_delay_ms=100, post_trigger_s=0.15)
        c = _conn()
        held = []
        for k in range(0, 60):
            c.fire("MAIN.nCycle", k, ft(10 * k))
            held.append(k)
            if k % 10 == 9:                               # the counter block: 10 cycles per notification
                for h in held:
                    c.fire(CC, h, ft(10 * h))
                held = []
        assert wait_until(lambda: not rec.status(st["id"])["active"]), rec.status(st["id"])
        s = rec.status(st["id"])
        assert s["triggered_at_ms"] == 300.0, s
        r = rec.read(st["id"], None, None, ["MAIN.nCycle", CC], 1000, False)
        firsts = {}
        for row in r["rows"]:
            firsts.setdefault(row["symbol"], row["t_ms"])
        assert firsts == {"MAIN.nCycle": 300.0 - pre, CC: 300.0 - pre}, (pre, firsts)
        v = rec.verify(st["id"])
        assert v["timeline"]["window_start_ms"] == 300.0 - pre
        assert all(b["missing_head"] == 0 for b in v["timeline"]["blocks"]), v["timeline"]


def test_n57_head_check_with_pre_trigger_zero(rec):
    """With pre_trigger_ms 0 the window starts at the trigger: a block whose first sample comes later lost its head."""
    st = _two_blocks(rec, start_when="MAIN.nCycle >= 3")
    c = _conn()
    for k in range(0, 12):
        c.fire("MAIN.nCycle", k, ft(10 * k))
        if k >= 6:                                         # the counter block misses cycles 3..5 after the trigger
            c.fire(CC, k, ft(10 * k))
        elif k < 3:
            c.fire(CC, k, ft(10 * k))
    assert wait_until(lambda: rec.status(st["id"])["samples"] >= 15)
    out = rec.stop(st["id"])
    assert out["triggered_at_ms"] == 30.0
    v = rec.verify(st["id"])
    blk = {b["first"]: b for b in v["timeline"]["blocks"]}
    assert v["timeline"]["window_start_ms"] == 30.0 and blk[CC]["missing_head"] == 3 and v["verdict"] == "FAIL", v


def test_n61_python_level_trigger_true_only_during_the_warmup_is_reported(rec):
    """N61: start_when true only in the first cycles, before every block delivered — it does not fire, and the status
    / footer say so instead of nothing; a trigger that fires after the warm-up reports its delay."""
    st = _two_blocks(rec, start_when="MAIN.nCycle <= 2")
    c = _conn()
    for k in range(0, 40):                                  # the counter block starts at cycle 5 (late subscription)
        c.fire("MAIN.nCycle", k, ft(10 * k))
        if k >= 5:
            c.fire(CC, k, ft(10 * k))
    assert wait_until(lambda: rec.status(st["id"]).get("trigger_note"))
    s = rec.status(st["id"])
    assert s["armed"] and "did not fire" in s["trigger_note"] and s["start_when_warmup_true_ms"] == 0.0, s
    rec.stop(st["id"])
    st = _two_blocks(rec, start_when="MAIN.nCycle >= 0")
    c = _conn()
    for k in range(0, 20):
        c.fire("MAIN.nCycle", k, ft(10 * k))
        if k >= 3:
            c.fire(CC, k, ft(10 * k))
    assert wait_until(lambda: rec.status(st["id"]).get("triggered_at_ms") is not None)
    s = rec.status(st["id"])
    assert s["triggered_at_ms"] == 40.0 and s["trigger_delayed_ms"] == 40.0 and "fired 40 ms later" in s["trigger_note"], s
    rec.stop(st["id"])


def test_n58_export_never_writes_over_the_recording_or_a_sidecar(rec, tmp_path):
    st = _two_blocks(rec)
    c = _conn()
    for ms in range(0, 50, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        c.fire(CC, ms // 10, ft(ms))
    rec.stop(st["id"])
    src = Path(rec.status(st["id"])["file"])
    before = R.file_sha256(src)[0]
    first = rec.export(st["id"], "csv_wide", None, None)
    for target in (src, R.manifest_path(src), Path(first["provenance"]["file"]), src.with_name("other.jsonl")):
        with pytest.raises(RecorderError, match="cannot be an export target"):
            rec.export(st["id"], "csv_long", str(target), None, overwrite=True)
    assert R.file_sha256(src)[0] == before


def test_n59_a_failed_export_leaves_the_previous_one_intact(rec, monkeypatch):
    st = _two_blocks(rec)
    c = _conn()
    for ms in range(0, 100, 10):
        c.fire("MAIN.nCycle", ms // 10, ft(ms))
        c.fire(CC, ms // 10, ft(ms))
    rec.stop(st["id"])
    first = rec.export(st["id"], "csv_wide", None, None)
    old = Path(first["file"]).read_bytes()
    old_prov = Path(first["provenance"]["file"]).read_bytes()
    calls = {"n": 0}
    real = R._flat

    def boom(v):
        calls["n"] += 1
        if calls["n"] > 5:
            raise OSError("disk full (simulated)")
        return real(v)
    monkeypatch.setattr(R, "_flat", boom)
    with pytest.raises(OSError, match="disk full"):
        rec.export(st["id"], "csv_wide", None, None, fill=True, overwrite=True)
    assert Path(first["file"]).read_bytes() == old and Path(first["provenance"]["file"]).read_bytes() == old_prov
    assert not [p for p in Path(first["file"]).parent.iterdir() if p.name.endswith(".tmp")]


def test_n62_cycle_compared_with_the_task_that_runs_the_program(rec, monkeypatch):
    """N62: the warning compared with the runtime's fastest task — cycle_ms=1 on variables of a 10 ms task said
    nothing when another task ran every 1 ms."""
    man = rec._manifest(None)
    man = {**man, "tasks": [{"index": 1, "name": "PlcTask", "cycle_time_ms": 10.0},
                            {"index": 2, "name": "FastTask", "cycle_time_ms": 1.0}]}
    monkeypatch.setattr(rec, "_manifest", lambda port=None: man)
    owners = {"MAIN": ["PlcTask"]}
    rec.task_of = lambda name: owners.get(name, [])
    st = rec.start(symbols=["MAIN.nCycle", "GVL_Machine.nDrumsFilled"], prefix=None, pattern=None, mode="cycle",
                   cycle_ms=1, duration_s=None, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0,
                   name="n62", max_symbols=50, cycle_counter=None)
    rec.stop(st["id"])
    assert "below the cycle of MAIN (task PlcTask) (10 ms" in st["warning"], st.get("warning")
    assert st["task_check"]["MAIN"] == {"task": "PlcTask", "cycle_ms": 10.0,
                                        "basis": "its task in the project, cycle from the runtime"}
    assert st["task_check"]["GVL_Machine"]["task"] is None and "GVL" in st["task_check"]["GVL_Machine"]["basis"]
    assert "GVL_Machine" not in st["warning"]               # 1 ms = the fastest task: not below


def test_n62_project_task_config_maps_programs_to_tasks(tmp_path):
    from twincat_mcp.config import ProjectConfig
    from twincat_mcp.project_backend import ProjectIndex
    plc = tmp_path / "Plc"
    (plc / "POUs").mkdir(parents=True)

    def pou(name, impl):
        (plc / "POUs" / f"{name}.TcPOU").write_text(
            f'<?xml version="1.0" encoding="utf-8"?><TcPlcObject><POU Name="{name}"><Declaration><![CDATA[PROGRAM {name}\n'
            f'VAR\n  n : INT;\nEND_VAR]]></Declaration><Implementation><ST><![CDATA[{impl}]]></ST></Implementation></POU>'
            '</TcPlcObject>', encoding="utf-8")
    pou("MAIN", "PRG_Machine();\n// PRG_Fast(); is a comment")
    pou("PRG_Machine", "n := n + 1;")
    pou("PRG_Fast", "n := n + 1;")
    idx = ProjectIndex(ProjectConfig(root=str(tmp_path)))
    assert idx.tasks_of("MAIN") is None                      # no task configuration: cannot tell
    for name, cyc, prog in (("PlcTask", 10000, "MAIN"), ("FastTask", 1000, "PRG_Fast")):
        (plc / f"{name}.TcTTO").write_text(
            f'<?xml version="1.0" encoding="utf-8"?><TcPlcObject><Task Name="{name}"><CycleTime>{cyc}</CycleTime>'
            f'<Priority>20</Priority><PouCall><Name>{prog}</Name></PouCall></Task></TcPlcObject>', encoding="utf-8")
    cfg = idx.task_config()
    assert cfg["PlcTask"]["cycle_ms"] == 10.0 and cfg["FastTask"]["programs"] == ["PRG_Fast"]
    assert idx.tasks_of("MAIN") == ["PlcTask"] and idx.tasks_of("PRG_Machine") == ["PlcTask"]   # called from MAIN
    assert idx.tasks_of("PRG_Fast") == ["FastTask"]          # the commented call in MAIN does not count
    assert idx.tasks_of("GVL_Machine") == []
