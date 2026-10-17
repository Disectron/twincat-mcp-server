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
    # read: wide with forward fill from before the window
    w = rec.read(rid, 300, 1000, ["MAIN.fbFill.eState", "MAIN.fbFill.bDone"], 100, wide=True)
    assert w["rows"][0]["MAIN.fbFill.bDone"] is False and w["rows"][0]["MAIN.fbFill.eState"] == 2
    assert w["rows"][-1]["MAIN.fbFill.bDone"] is True
    # export
    e = rec.export(rid, "csv_wide", None, None)
    csv_lines = Path(e["file"]).read_text().splitlines()
    assert csv_lines[0] == "t_ms,plc_filetime,MAIN.fbFill.eState,MAIN.fbFill.bDone,MAIN.fbFill.tSettle"
    # one row per sample time; only what was sampled then (the PLC's own FILETIME per row)
    assert csv_lines[-1] == f"1200.0,{ft(1200)},,False,"
    assert f"900.0,{ft(900)},3,True," in csv_lines                # two changes at the same PLC time → one row
    ef = rec.export(rid, "csv_wide", str(tmp_path / "filled.csv"), None, fill=True)
    assert (tmp_path / "filled.csv").read_text().splitlines()[-1] == f"1200.0,{ft(1200)},3,False,750"
    assert e["rows"] == 6 and ef["rows"] == 6               # sample times 0, 100, 350, 350.5, 900, 1200
    e2 = rec.export(rid, "csv_long", None, ["MAIN.fbFill.eState"])
    assert e2["rows"] == 4
    lst = rec.list()
    assert lst["session"][0]["id"] == rid
    d = rec.delete(rid)
    assert len(d["removed"]) == 3 and not Path(out["file"]).exists()


def test_triggers_pre_and_stop(rec):
    FakeConnection.shared_values.update({"MAIN.bStart": False, "MAIN.fbFill.bDone": False})
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
    c.fire("MAIN.bStart", True, ft(5000))
    for ms in range(5010, 5300, 10):
        c.fire("MAIN.nCycle", ms, ft(ms))
    assert wait_until(lambda: not rec.status(st["id"])["active"])
    s = rec.status(st["id"])
    assert s["stop_reason"].startswith("post_trigger 0.1s")
    r = rec.read(st["id"], None, None, ["MAIN.nCycle"], 1000, False)
    assert max(x["t_ms"] for x in r["rows"]) <= 100 + 10.01   # nothing recorded past the window (+1 sample)


# ---------------------------------------------------------------- recording accuracy (field report A1–A7)
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
    v = rec.verify(st["id"], counter="MAIN.nCycle")
    assert v["integrity"] == "intact" and v["timeline"]["missing"] == 21 and v["verdict"] == "FAIL"
    assert v["counter"]["missing_samples"] == 21 and v["counter"]["step"] == 1 and not v["counter"]["lossless"]
    assert v["t0"]["source"] == "plc" and v["t0"]["filetime"] == ft(0)
    # every sample carries the PLC's FILETIME exactly: t0 + t_ms * 1e4
    rows = rec.read(st["id"], None, None, ["MAIN.nCycle"], 5, False)["rows"]
    assert [v["t0"]["filetime"] + round(r["t_ms"] * 1e4) for r in rows] == [ft(i) for i in range(5)]
    # tampering is detected
    p = Path(out["file"])
    lines = p.read_text().splitlines(keepends=True)
    i = next(k for k, ln in enumerate(lines) if ln.startswith("[500"))
    lines[i] = lines[i].replace("[500", "[501", 1)
    p.write_text("".join(lines))
    assert rec.verify(st["id"])["integrity"].startswith("MODIFIED")


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
