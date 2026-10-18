"""Native capture service (tcmcp-capture): protocol, format-2 compatibility and the acceptance checks of the
C++ capture spec that can run without a PLC (the simulator backend stands in for the TwinCAT runtime).

The executable comes from TCMCP_CAPTURE_EXE (CI builds it first) or is built here with CMake; without a
compiler the tests are skipped."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from twincat_mcp import __version__
from twincat_mcp import native_capture as NC
from twincat_mcp.ads_backend import AdsBackend, AdsError
from twincat_mcp.config import AdsConfig, RecorderConfig
from twincat_mcp import recorder as R
from twincat_mcp.recorder import Recorder, RecorderError, recording_key
from tests.fake_ads import install

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native" / "capture"
START_FT = 133_000_000_000_000_000


@pytest.fixture(scope="session")
def capture_exe(tmp_path_factory) -> Path:
    env = os.environ.get("TCMCP_CAPTURE_EXE")
    if env:
        p = Path(env)
        assert p.is_file(), f"TCMCP_CAPTURE_EXE={env} does not exist"
        return p
    if not shutil.which("cmake"):
        pytest.skip("cmake not installed: native capture service not built")
    b = tmp_path_factory.mktemp("capture-build")
    r = subprocess.run(["cmake", "-S", str(NATIVE), "-B", str(b), "-DCMAKE_BUILD_TYPE=Release"], capture_output=True, text=True)
    if r.returncode:
        pytest.skip(f"cmake configure failed: {r.stderr[-400:]}")
    r = subprocess.run(["cmake", "--build", str(b), "--config", "Release", "-j", "4"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    for cand in (b / NC.EXE_NAME, b / "Release" / NC.EXE_NAME):
        if cand.is_file():
            return cand
    pytest.fail("built, but the executable was not found")


# ------------------------------------------------------------------ raw protocol
class Svc:
    def __init__(self, exe: Path):
        self.p = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, bufsize=1)
        self.ready = json.loads(self.p.stdout.readline())

    def call(self, cmd: dict) -> dict:
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()
        while True:
            d = json.loads(self.p.stdout.readline())
            if "reply" in d:
                return d

    def wait_event(self, timeout: float = 30.0) -> dict:
        t = time.time()
        while time.time() - t < timeout:
            ln = self.p.stdout.readline()
            if not ln:
                break
            d = json.loads(ln)
            if d.get("event") == "finished":
                return d
        raise AssertionError("no finished event")

    def close(self) -> None:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.p.wait(timeout=30)


def _start_cmd(tmp: Path, name: str, blocks: list[dict], **kw) -> dict:
    key = tmp / ".recording.key"
    if not key.exists():
        recording_key(tmp)
    cmd = {"cmd": "start", "backend": "sim", "sim": {"speed": 200, "start_ft": START_FT, **kw.pop("sim", {})},
           "file": str(tmp / f"{name}.jsonl"), "hmac_key_path": str(key), "cycle_100ns": 10_000,
           "header": json.dumps({"_header": True, "format": 2, "mode": "cycle", "blocks": []}), "blocks": blocks}
    cmd.update(kw)
    return cmd


def _rows(path: Path) -> list[list]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("[")]


def test_version_and_selftest(capture_exe):
    info = json.loads(subprocess.run([str(capture_exe), "--version"], capture_output=True, text=True).stdout)
    assert info["name"] == "tcmcp-capture" and info["protocol"] == NC.PROTOCOL
    assert info["version"] == __version__, "the exe is built with the package version (pyproject)"
    assert "AdsLib" in info["ads"]
    r = subprocess.run([str(capture_exe), "--selftest"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


def test_spec1_60s_lossless_exact_timestamps(capture_exe, tmp_path):
    """Spec 1 (simulated): a 1200-byte block (300 UDINT) + a 4-byte counter, 1 ms, 60 s → PASS, 60,000 ± 1
    samples, exact PLC timestamps, sha256/hmac as the Python recorder computes them."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "r1", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 1200, "leaves": 300},
                                               {"id": 1, "igrp": 0x4040, "ioffs": 4096, "size": 4, "leaves": 1}],
                              duration_s=60, sim={"speed": 40}))
        assert r["ok"] and r["subscribed"] == 2 and r["engine"] == "native"
        f = s.wait_event(60)["footer"]
    finally:
        s.close()
    assert f["verdict_hint"] == "PASS" and f["missing_samples"] == 0 and f["overflow_samples"] == 0
    blocks = {b["id"]: b for b in f["blocks"]}
    assert all(abs(b["samples"] - 60_000) <= 1 for b in blocks.values())
    assert f["events"] == blocks[0]["samples"] * 300 + blocks[1]["samples"]
    path = tmp_path / "r1.jsonl"
    lines = path.read_bytes().split(b"\n")
    t0 = json.loads(lines[1])["_t0"]
    assert t0["filetime"] == START_FT and t0["source"] == "plc"
    rows = _rows(path)
    # merged by PLC time, every timestamp exact: t_ms = k ms, with 4 decimals
    by_block: dict[str, list[float]] = {}
    for t, k, _b64 in rows:
        by_block.setdefault(k, []).append(t)
    assert by_block["#0"][:3] == [0.0, 1.0, 2.0] and by_block["#0"] == sorted(by_block["#0"])
    assert all(b"[0.0000," in ln for ln in lines[2:4])
    last = base64.b64decode(rows[-1][2])
    assert int.from_bytes(last[:4], "little") == len(by_block["#0"]) - 1       # simulated CycleCount
    # integrity exactly as rec_verify computes it
    import hashlib
    import hmac
    body = b"".join(ln + b"\n" for ln in lines if ln and not ln.startswith(b'{"_footer"'))
    assert hashlib.sha256(body).hexdigest() == f["sha256"]
    key = recording_key(tmp_path)
    assert hmac.new(key, f["sha256"].encode(), hashlib.sha256).hexdigest() == f["hmac"]
    # header + _t0 + sample rows + hash checkpoints (R9); no gaps → no gap markers, label COMPLETE (R5)
    chains = [json.loads(ln) for ln in lines if ln.startswith(b'{"_chain"')]
    assert f["lines"] == len(rows) + 2 + len(chains) and f["chain_checkpoints"] == len(chains) >= 1
    assert f["gap_markers"] == 0 and f["label"] == "COMPLETE"
    # every checkpoint commits to exactly the bytes before it, signed with the install key
    run = hashlib.sha256()
    seen = 0
    for ln in lines:
        if ln.startswith(b'{"_footer"'):
            break
        if ln.startswith(b'{"_chain"'):
            c = json.loads(ln)["_chain"]
            assert c["sha256"] == run.hexdigest() and c["bytes"] == seen
            assert hmac.new(key, c["sha256"].encode(), hashlib.sha256).hexdigest() == c["hmac"]
        run.update(ln + b"\n")
        seen += len(ln) + 1


def test_spec6_overflow_is_reported_never_pass(capture_exe, tmp_path):
    """Spec 6: a ring that cannot hold the stream (tiny + a stalled writer) → overflow_samples > 0, FAIL."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "r6", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 1200}], duration_s=5,
                              ring_mb=0.064, debug_writer_stall_ms=300, sim={"speed": 50}))
        assert r["ok"], r
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    assert f["overflow_samples"] > 0 and f["dropped"] == f["overflow_samples"]
    assert f["verdict_hint"] == "FAIL" and f["overflow_intervals"]
    assert f["missing_samples"] > 0      # the dropped samples are holes in the PLC timeline of the file


def test_spec7_plc_stops_counts_missing_tail(capture_exe, tmp_path):
    """Spec 7: the PLC stops delivering after 300 ms of a 1 s window → missing_tail = the rest, FAIL."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "r7", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 8}], duration_s=1,
                              sim={"speed": 1, "stop_after_cycles": 300}))
        assert r["ok"]
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    assert f["samples"] == 300
    assert 695 <= f["missing_tail"] <= 700 and f["verdict_hint"] == "FAIL"
    assert "stopped delivering" in f["stop_reason"]
    assert f["window_end_ms"] == 1000.0
    # R5: the tail is in the file as a marker, the recording is labelled INCOMPLETE
    gaps = [json.loads(ln)["_gap"] for ln in (tmp_path / "r7.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.startswith('{"_gap"')]
    assert gaps == [{"block": 0, "kind": "tail", "t_from": 299.0, "t_to": 1000.0, "missing": f["missing_tail"]}]
    assert f["label"] == "INCOMPLETE" and f["gap_markers"] == 1


def test_d11_frozen_plc_is_a_stall_not_data(capture_exe, tmp_path):
    """D11: a PLC in STOP keeps sending notifications with frozen values. The cycle counter (block 0, UDINT at
    offset 0) does not advance → the interval is a {"_stall"} marker, the samples count as stalled, the label is
    INCOMPLETE and the verdict FAIL — the bytes themselves stay as sent."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "d11", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 8}], duration_s=1,
                              stall={"block": 0, "off": 0, "size": 4, "after_100ns": 3 * 10_000},
                              sim={"speed": 50, "freeze": [400, 599]}))
        assert r["ok"]
        time.sleep(0.5)                                   # ~1 s of sim time at speed 50 → inside / after the freeze
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    assert f["samples"] == 1001 and f["missing_samples"] == 0          # every notification is in the file
    assert f["stalls"] == 1 and f["stalled_samples"] == 200 and abs(f["stall_s"] - 0.201) < 1e-6
    assert f["label"] == "INCOMPLETE" and f["verdict_hint"] == "FAIL" and f["stall_counter_block"] == 0
    assert f["label_reason"] == "PLC stopped (cycle counter frozen) 399.0000-600.0000 ms"
    assert abs(f["completeness"] - 801 / 1001) < 1e-6
    lines = (tmp_path / "d11.jsonl").read_text(encoding="utf-8").splitlines()
    st = [json.loads(ln)["_stall"] for ln in lines if ln.startswith('{"_stall"')]
    assert st == [{"block": 0, "t_from": 399.0, "t_to": 600.0, "samples": 200}]
    rows = _rows(tmp_path / "d11.jsonl")
    vals = [int.from_bytes(base64.b64decode(r[2])[:4], "little") for r in rows]
    assert vals[399] == 399 and vals[400] == 399 and vals[599] == 399 and vals[600] == 600   # frozen bytes kept as sent


def test_d13_outage_resubscribes_and_marks_the_gap(capture_exe, tmp_path):
    """D13: TwinCAT RUN→CONFIG→RUN kills the notifications. After the silence the engine registers them again (the
    source answers once the runtime is back); the hole is a gap marker, the rest of the window is recorded."""
    s = Svc(capture_exe)
    try:
        # 10 ms cycle at speed 1: 0..0.5 s samples, outage 0.5..2.0 s (handles dead), runtime back at 2.0 s
        r = s.call(_start_cmd(tmp_path, "d13", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 8}], duration_s=4,
                              cycle_100ns=100_000, silent_ms=300, resubscribe_after_ms=600, resubscribe_retry_ms=200,
                              sim={"speed": 1, "outage": [50, 199]}))
        assert r["ok"]
        time.sleep(1.5)
        st = s.call({"cmd": "status"})
        assert st["silent_blocks"] == 1 and st.get("missing_silent_est", 0) > 50 and st["completeness_estimated"]
        assert st["completeness"] < 0.6 and "simulated outage" in st.get("resubscribe_error", "")
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    assert f["resubscriptions"] == 1 and f["label"] == "INCOMPLETE"
    assert f["samples"] >= 200 and f["missing_gaps"] >= 140          # 0..49 + ~200..400 recorded, the outage missing
    gaps = [json.loads(ln)["_gap"] for ln in (tmp_path / "d13.jsonl").read_text(encoding="utf-8").splitlines()
            if ln.startswith('{"_gap"')]
    assert gaps and gaps[0]["kind"] == "gap" and gaps[0]["t_from"] == 490.0 and gaps[0]["missing"] >= 140
    assert any("registered again" in e for e in f["errors"])
    assert f["stop_reason"] == "duration 4s"                          # the window was completed after the outage


def test_r5_gap_markers_where_samples_are_missing(capture_exe, tmp_path):
    """R5: every hole in the PLC timeline is written into the file at the place it happened, with its size."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "r5", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 8}], duration_s=1,
                              sim={"speed": 0, "drop": [[0, 300, 319], [0, 777, 777]]}))
        assert r["ok"], r
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    lines = (tmp_path / "r5.jsonl").read_text(encoding="utf-8").splitlines()
    gaps = [(i, json.loads(ln)["_gap"]) for i, ln in enumerate(lines) if ln.startswith('{"_gap"')]
    assert [g for _i, g in gaps] == [{"block": 0, "kind": "gap", "t_from": 299.0, "t_to": 320.0, "missing": 20},
                                     {"block": 0, "kind": "gap", "t_from": 776.0, "t_to": 778.0, "missing": 1}]
    # each marker sits right before the first sample after the hole
    for i, g in gaps:
        assert json.loads(lines[i + 1])[0] == g["t_to"] and json.loads(lines[i - 1 if not lines[i - 1].startswith("{") else i - 2])[0] == g["t_from"]
    assert f["missing_gaps"] == 21 and f["label"] == "INCOMPLETE" and f["gap_markers"] == 2


def test_spec8_unsubscribable_block_refuses_start(capture_exe, tmp_path):
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "r8", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 8},
                                               {"id": 7, "igrp": 0xDEAD, "ioffs": 0, "size": 8}], duration_s=1))
    finally:
        s.close()
    assert not r["ok"] and r["block"] == 7 and "0x702" in r["error"] and "nothing written" in r["error"]
    assert not (tmp_path / "r8.jsonl").exists()


def test_gap_and_stdin_close_footer(capture_exe, tmp_path):
    """A PLC-side gap is counted from the timestamps; closing stdin (server gone) still writes a signed footer."""
    s = Svc(capture_exe)
    r = s.call(_start_cmd(tmp_path, "rg", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 4}],
                          sim={"speed": 20, "drop": [[0, 100, 109]]}))
    assert r["ok"]
    time.sleep(0.5)
    st = s.call({"cmd": "status"})
    assert st["active"] and st["samples"] > 0 and st["missing_gaps"] == 10
    s.close()
    lines = (tmp_path / "rg.jsonl").read_text(encoding="utf-8").splitlines()
    f = json.loads(lines[-1])
    assert f["_footer"] and f["missing_gaps"] == 10 and "stdin closed" in f["stop_reason"]
    assert f["blocks"][0]["first_gaps"][0] == [99.0, 10]


def test_bad_command_lines(capture_exe):
    s = Svc(capture_exe)
    try:
        assert s.call({"cmd": "status"})["ok"] is False
        s.p.stdin.write("{not json\n")
        s.p.stdin.flush()
        d = json.loads(s.p.stdout.readline())
        assert d["ok"] is False and "JSON" in d["error"]
        r = s.call({"cmd": "start", "blocks": []})
        assert not r["ok"] and "blocks" in r["error"]
    finally:
        s.close()


# ------------------------------------------------------------------ through the Recorder (rec_start path)
@pytest.fixture
def rec(monkeypatch, tmp_path, capture_exe):
    install(monkeypatch)
    ads = AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1"))
    r = Recorder(ads, tmp_path / "recordings", ring_events=1000, max_runs=100)
    r.rcfg = RecorderConfig(engine="native", capture_exe=str(capture_exe))
    r.native_extra = {"backend": "sim", "ams_net_id": "5.1.2.3.1.1", "sim": {"speed": 100, "start_ft": START_FT}}
    yield r
    r.stop_all()


def _wait_done(r: Recorder, rid: str, timeout: float = 30.0) -> None:
    t = time.time()
    while time.time() - t < timeout:
        if not r.status(rid)["active"]:
            return
        time.sleep(0.05)
    raise AssertionError("recording did not end")


def test_recorder_native_duration_verify_pass(rec):
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.stRecipe"], prefix=None, pattern=None, mode="cycle", cycle_ms=1,
                   duration_s=2, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="n1",
                   max_symbols=50, cycle_counter=None)
    assert st["engine"] == "native" and st["active"]
    _wait_done(rec, st["id"])
    fin = rec.stop(st["id"])
    assert fin["engine"] == "native" and fin["stop_reason"].startswith("duration") and fin["verdict_hint"] == "PASS"
    v = rec.verify(st["id"], counter="MAIN.nCycle")
    assert v["integrity"] == "intact" and v["verdict"] == "PASS", v
    assert v["counter"]["lossless"] and v["footer"]["engine"] == "native"
    head = json.loads(Path(fin["file"]).read_text(encoding="utf-8").splitlines()[0])
    assert head["engine"] == "native" and head["engine_version"] == __version__
    stats = rec.stats(st["id"], ["MAIN.nCycle"], False, 0)
    assert stats["source"] == "file" and stats["symbols"]["MAIN.nCycle"]["count"] >= 2000


def test_spec5_pre_trigger_exactly_500(rec):
    """Spec 5: start_when + pre_trigger_ms=500 → exactly 500 samples before the trigger sample, then PASS."""
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=None,
                   max_events=None, start_when="MAIN.nCycle >= 2000", stop_when=None, pre_trigger_ms=500, name="n5",
                   max_symbols=50, post_trigger_s=1.0, cycle_counter=None)
    assert st["engine"] == "native"
    _wait_done(rec, st["id"])
    fin = rec.stop(st["id"])
    assert fin["stop_reason"].startswith("post_trigger")
    rows = _rows(Path(fin["file"]))
    trig = float(fin["triggered_at_ms"])
    before = [r for r in rows if r[0] < trig]
    assert len(before) == 500
    assert fin["missing_samples"] == 0 and fin["verdict_hint"] == "PASS"
    v = rec.verify(st["id"])
    assert v["verdict"] == "PASS" and v["timeline"]["missing"] == 0


def test_n29_native_not_armed_after_trigger(rec):
    """N29 (native): status after the trigger fired and the recording ended must not say armed."""
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=None,
                   max_events=None, start_when="MAIN.nCycle >= 300", stop_when=None, pre_trigger_ms=0, name="n29",
                   max_symbols=50, post_trigger_s=0.3, cycle_counter=None)
    assert st["engine"] == "native"
    _wait_done(rec, st["id"])
    s1 = rec.status(st["id"])
    assert s1["armed"] is False and s1["triggered_at_ms"] is not None
    fin = rec.stop(st["id"])
    assert fin.get("armed", False) is False


def test_pre_trigger_not_filled_is_missing_head(rec):
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=None,
                   max_events=None, start_when="MAIN.nCycle >= 100", stop_when=None, pre_trigger_ms=500, name="nh",
                   max_symbols=50, post_trigger_s=0.2, cycle_counter=None)
    _wait_done(rec, st["id"])
    fin = rec.stop(st["id"])
    assert 395 <= fin["missing_head"] <= 405 and fin["verdict_hint"] == "FAIL"
    v = rec.verify(st["id"])
    assert v["verdict"] == "FAIL" and v["timeline"]["missing"] >= 395


def test_stop_when_native(rec):
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=None,
                   max_events=None, start_when=None, stop_when="MAIN.nCycle == 1234", pre_trigger_ms=0, name="ns",
                   max_symbols=50, cycle_counter=None)
    _wait_done(rec, st["id"])
    fin = rec.stop(st["id"])
    assert fin["stop_reason"].startswith("stop_when")
    rows = _rows(Path(fin["file"]))
    last = int.from_bytes(base64.b64decode(rows[-1][2])[:4], "little")
    assert last == 1234


def test_spec9_one_byte_edit_is_modified(rec):
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=0.5,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="n9", max_symbols=50,
                   cycle_counter=None)
    _wait_done(rec, st["id"])
    fin = rec.stop(st["id"])
    p = Path(fin["file"])
    assert R.is_read_only(p) and R.manifest_path(p).is_file()          # R9: sealed after stop
    R.make_writable(p)
    data = bytearray(p.read_bytes())
    i = data.index(b'"#')
    data[i - 2] = ord("9") if data[i - 2] != ord("9") else ord("8")
    p.write_bytes(bytes(data))
    v = rec.verify(st["id"])
    assert v["integrity"].startswith("MODIFIED") and v["verdict"] == "FAIL"
    assert v["seal"].startswith("FILE CHANGED SINCE SEALING")


def test_r9_truncated_file_verifies_to_last_checkpoint(rec):
    """A file whose footer never got written (power cut) is still proven intact up to its last checkpoint;
    an edit before a checkpoint is located."""
    rec.native_extra["sim"] = {**rec.native_extra["sim"], "speed": 1}     # real time: one checkpoint per second
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=2.5,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="r9t", max_symbols=50,
                   cycle_counter=None)
    _wait_done(rec, st["id"])
    fin = rec.stop(st["id"])
    p = Path(fin["file"])
    R.make_writable(p)
    lines = p.read_bytes().splitlines(keepends=True)        # bytes: write_text would turn \n into \r\n on Windows
    chain_idx = [i for i, ln in enumerate(lines) if ln.startswith(b'{"_chain"')]
    assert len(chain_idx) >= 2
    cut = chain_idx[-1] + 5                                   # a few rows after the last checkpoint, no footer
    p.write_bytes(b"".join(lines[:cut]))
    v = rec.verify(st["id"])
    assert v["integrity"].startswith("NO FOOTER — intact up to the last checkpoint"), v["integrity"]
    assert v["checkpoints"]["verified"] == len(chain_idx) and v["checkpoints"]["last_verified_line"] == chain_idx[-1]
    # change one sample before the first checkpoint
    j = next(i for i, ln in enumerate(lines) if ln.startswith(b"[") and i < chain_idx[0])
    bad = lines[:cut]
    bad[j] = bad[j].replace(b"[", b"[9", 1)
    p.write_bytes(b"".join(bad))
    v = rec.verify(st["id"])
    assert v["integrity"].startswith("MODIFIED") and f"line {chain_idx[0] + 1}" in v["integrity"]


def test_n27_self_ended_native_recording_is_sealed_without_rec_stop(rec):
    """N27: a duration-ended recording gets its manifest when the service reports 'finished', not when someone calls
    rec_stop; the stop time is the finish time (D15), not the time somebody looked."""
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=0.3,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="n27", max_symbols=50,
                   cycle_counter=None)
    p = Path(rec.recs[st["id"]].path)
    t = time.time()
    # the seal writes the manifest, then makes both files read-only: wait for the whole seal, not its first step
    while time.time() - t < 20 and not (R.manifest_path(p).exists() and R.is_read_only(p)):
        time.sleep(0.05)
    assert R.manifest_path(p).exists() and R.is_read_only(p), "sealed by the finished event alone"
    nrec = rec.recs[st["id"]]
    time.sleep(0.5)
    s1 = rec.status(st["id"])
    assert not s1["active"] and nrec.stopped_at <= t + 20 and abs(s1["elapsed_s"] - rec.status(st["id"])["elapsed_s"]) < 1e-6
    assert rec.verify(st["id"])["seal"] == "matches manifest"


def test_r9_delete_removes_read_only_recording_and_manifest(rec):
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=0.3,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="r9d", max_symbols=50,
                   cycle_counter=None)
    _wait_done(rec, st["id"])
    fin = rec.stop(st["id"])
    p = Path(fin["file"])
    assert R.is_read_only(p)
    out = rec.delete(st["id"])
    assert not p.exists() and not R.manifest_path(p).exists() and str(R.manifest_path(p)) in out["removed"]


def test_spec10_export_identical_to_python_engine(rec, tmp_path):
    """Spec 10: the same samples recorded by the Python engine and by the native engine export identically."""
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.stRecipe"], prefix=None, pattern=None, mode="cycle", cycle_ms=1,
                   duration_s=0.3, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="n10",
                   max_symbols=50, cycle_counter=None)
    _wait_done(rec, st["id"])
    nat = rec.stop(st["id"])
    native_path = Path(nat["file"])
    # replay the very same (filetime, block, bytes) through the Python engine's ingest
    lines = native_path.read_text(encoding="utf-8").splitlines()
    t0 = json.loads(lines[1])["_t0"]["filetime"]
    from twincat_mcp.recorder import Recording, build_blocks
    syms = rec.expand_symbols(["MAIN.nCycle", "MAIN.stRecipe"], None, None, 50)
    py = Recording("py10", "py10", syms, "cycle", 1.0, tmp_path / "py10.jsonl", 1000, 100, None, None, None, None, 0,
                   {}, None, rec.key)
    py.set_blocks(build_blocks(syms, py.sids, py.types))
    py.start_ingest()
    for t, k, b64 in _rows(native_path):
        py.enqueue(t0 + int(round(t * 1e4)), int(k[1:]), base64.b64decode(b64))
    time.sleep(0.3)
    py.request_stop("replay done")
    py._thread.join(10)
    rec.recs["py10"] = py
    shutil.copy(tmp_path / "py10.jsonl", rec.root / "py10.jsonl")
    for fmt in ("csv_wide", "csv_long"):
        a = rec.export(st["id"], fmt, str(tmp_path / f"n.{fmt}.csv"), None)
        b = rec.export("py10", fmt, str(tmp_path / f"p.{fmt}.csv"), None)
        assert a["rows"] == b["rows"] > 0
        assert Path(a["file"]).read_text(encoding="utf-8") == Path(b["file"]).read_text(encoding="utf-8"), fmt
    # and the raw rows are byte-identical
    assert [ln for ln in native_path.read_text(encoding="utf-8").splitlines() if ln.startswith("[")] == \
           [ln for ln in (tmp_path / "py10.jsonl").read_text(encoding="utf-8").splitlines() if ln.startswith("[")]


def test_auto_falls_back_to_python_with_reason(monkeypatch, tmp_path):
    install(monkeypatch)
    ads = AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1"))
    r = Recorder(ads, tmp_path / "recordings", ring_events=1000, max_runs=100)
    r.rcfg = RecorderConfig(engine="auto", capture_exe=str(tmp_path / "missing" / NC.EXE_NAME))
    st = r.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=10, duration_s=None,
                 max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="fb", max_symbols=5,
                 cycle_counter=None)
    try:
        assert st["engine"] == "python" and "not installed" in st["engine_note"]
    finally:
        r.stop(st["id"])
    r.rcfg = RecorderConfig(engine="native", capture_exe=str(tmp_path / "missing" / NC.EXE_NAME))
    with pytest.raises(RecorderError, match="native"):
        r.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=10, duration_s=None,
                max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="fb2", max_symbols=5,
                cycle_counter=None)
    with pytest.raises(RecorderError, match="cycle mode only"):
        r.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="change", cycle_ms=10, duration_s=None,
                max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="fb3", max_symbols=5,
                cycle_counter=None, engine="native")


def test_native_refusal_is_not_a_fallback(rec):
    rec.native_extra["sim"]["fail_block"] = 0
    with pytest.raises(AdsError, match="nothing written"):
        rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=1,
                  max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="nf", max_symbols=5,
                  cycle_counter=None)
    assert not list(rec.root.glob("*.jsonl"))


def test_release_exe_must_match_its_pinned_sha256(capture_exe, tmp_path):
    """The release ships tcmcp-capture.exe.sha256 next to the exe; a binary that differs is never run."""
    exe = tmp_path / capture_exe.name
    shutil.copy2(capture_exe, exe)
    pin = exe.with_name(exe.name + ".sha256")
    pin.write_text(NC.exe_sha256(exe) + "  " + exe.name + "\n", encoding="ascii")
    ok, why, info = NC.availability(exe, __version__)
    assert ok and info["sha256_verified"], why
    pin.write_text("0" * 64 + "  " + exe.name + "\n", encoding="ascii")
    ok, why, _ = NC.availability(exe, __version__)
    assert not ok and "does not match the SHA-256" in why


def test_d32_snapshot_of_a_finished_native_recording_counts_its_events(rec):
    """D32: rec_snapshot said events: 0 for a finished native recording while returning rows."""
    st = rec.start(symbols=["MAIN.nCycle"], prefix=None, pattern=None, mode="cycle", cycle_ms=1, duration_s=0.2,
                   max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="d32", max_symbols=50,
                   cycle_counter=None)
    nrec = rec.recs[st["id"]]
    t = time.time()
    while time.time() - t < 20 and not nrec.proc.finished.is_set():
        time.sleep(0.05)
    nrec.events = 0                                   # nobody called status() since the start
    snap = rec.snapshot(st["id"], 10, None)
    assert snap["rows"] and snap["events"] > 0, snap


def test_r7_manual_stop_of_a_lossless_native_recording_is_complete(rec):
    """R7: rec_stop / 'run ended' on a native recording → missing_tail 1, INCOMPLETE/FAIL although the counter is
    lossless: the blocks are unsubscribed one after another, the first stops a cycle or two before the last."""
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.stRecipe"], prefix=None, pattern=None, mode="cycle", cycle_ms=1,
                   duration_s=None, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="r7",
                   max_symbols=50, cycle_counter=None)
    time.sleep(1.0)
    fin = rec.stop(st["id"], "run ended")
    assert fin["engine"] == "native" and fin["missing_samples"] == 0 and fin["verdict_hint"] == "PASS", fin
    v = rec.verify(st["id"], counter="MAIN.nCycle")
    assert v["verdict"] == "PASS" and v["counter"]["lossless"], v


def test_r41_trigger_keeps_every_block_from_the_trigger_cycle(capture_exe, tmp_path):
    """R41: a triggered capture had missing_head 1 on one block although lossless. `start_when nCycle > 0` is true
    at block 0's first sample, which came one cycle before block 1's subscription delivered: the window started
    before block 1 existed. A level condition now fires on the first cycle every block delivers (here cycle 1).
    Arrival skew between blocks (block 1 a cycle ahead) must not lose the trigger-cycle sample either."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "r41", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 4, "leaves": 1},
                                                {"id": 1, "igrp": 0x4040, "ioffs": 64, "size": 4, "leaves": 1}],
                              start_when={"block": 0, "off": 0, "type": "u32", "op": ">=", "value": 0},
                              post_trigger_s=0.3, sim={"speed": 50, "late": [[1, 1]], "skew": [[1, 1]]}))
        assert r["ok"], r
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    blocks = {b["id"]: b for b in f["blocks"]}
    assert blocks[1]["missing_head"] == 0 and blocks[0]["missing_head"] == 0, f["blocks"]
    rows = _rows(tmp_path / "r41.jsonl")
    firsts = {}
    for t, k, b64 in rows:
        firsts.setdefault(k, (t, int.from_bytes(base64.b64decode(b64)[:4], "little")))
    # both blocks start on the same cycle, the first one where every block delivers: block 1 starts at cycle 1, and
    # block 0's sample of cycle 1 is processed before block 1's (same time, block order) → the trigger is cycle 2
    assert firsts["#0"] == firsts["#1"] and firsts["#0"][1] == 2, firsts
    assert f["trigger_delayed_ms"] == 2.0, f
    assert f["verdict_hint"] == "PASS" and f["missing_samples"] == 0


def test_n56_stop_on_request_ends_at_a_plc_time_every_block_reaches(rec):
    """R7 / N56: a stop on request ends at the newest PLC time delivered when it was asked for; the notifications stay
    until every block delivered up to it, so the engine and rec_verify use the same allowance as any other end:
    one period. (0.8.9 unsubscribed at once and allowed 3 periods + max_delay — 900 lost samples passed.)"""
    st = rec.start(symbols=["MAIN.nCycle", "MAIN.stRecipe"], prefix=None, pattern=None, mode="cycle", cycle_ms=1,
                   duration_s=None, max_events=None, start_when=None, stop_when=None, pre_trigger_ms=0, name="r7",
                   max_symbols=50, max_delay_ms=1000)
    time.sleep(0.6)
    fin = rec.stop(st["id"])
    assert fin["missing_samples"] == 0, fin
    footer = json.loads(Path(fin["file"]).read_text(encoding="utf-8").splitlines()[-1])
    assert footer["manual_stop"] is False and footer["stop_on_request"]["at_ms"] == footer["window_end_ms"], footer
    v = rec.verify(st["id"])
    assert v["timeline"]["missing"] == 0 and v["verdict"] == "PASS", v["timeline"]
    assert v["timeline"]["window_end_ms"] == footer["window_end_ms"]
    assert all(b["missing_tail"] == 0 for b in v["timeline"]["blocks"])


def test_n56_n63_manual_stop_with_a_real_tail_drop_fails(capture_exe, tmp_path):
    """N56: block 1 stops delivering 500 cycles before the end; max_delay 1 s. The 0.8.9 engine allowed
    3 + max_delay/period = 1003 periods on a stop on request → PASS with 500 samples lost. Now the stop ends at the
    newest PLC time, block 1 never reaches it → missing_tail 499, FAIL."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "n56", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 4, "leaves": 1},
                                                {"id": 1, "igrp": 0x4040, "ioffs": 64, "size": 4, "leaves": 1}],
                              max_delay_100ns=10_000_000,
                              sim={"speed": 200, "stop_after_cycles": 3000, "drop": [[1, 2500, 10_000_000]]}))
        assert r["ok"], r
        time.sleep(0.5)                                      # 3000 cycles at 200× ≈ 15 ms: the sim has stopped
        f = s.call({"cmd": "stop", "reason": "requested"})["footer"]
    finally:
        s.close()
    blocks = {b["id"]: b for b in f["blocks"]}
    assert f["stop_on_request"]["at_ms"] == f["window_end_ms"] == 2999.0, f
    assert blocks[1]["missing_tail"] == 499 and blocks[0]["missing_tail"] == 0, f["blocks"]
    assert f["verdict_hint"] == "FAIL" and f["label"] == "INCOMPLETE"


def test_n63_duration_end_keeps_allowance_one(capture_exe, tmp_path):
    """stop_when / duration / max_events end at a PLC time too: one period of allowance, a block 3 cycles short →
    missing_tail 2."""
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "n63", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 4, "leaves": 1},
                                                {"id": 1, "igrp": 0x4040, "ioffs": 64, "size": 4, "leaves": 1}],
                              duration_s=0.5, max_delay_100ns=10_000_000,
                              sim={"speed": 100, "drop": [[1, 498, 10_000_000]]}))
        assert r["ok"], r
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    blocks = {b["id"]: b for b in f["blocks"]}
    assert blocks[1]["missing_tail"] == 2 and f["verdict_hint"] == "FAIL", f["blocks"]


def _first_cycles(path: Path) -> dict[str, int]:
    firsts: dict[str, int] = {}
    for t, k, b64 in _rows(path):
        firsts.setdefault(k, int.from_bytes(base64.b64decode(b64)[:4], "little"))
    return firsts


def test_n57_n63_native_trigger_with_a_batched_trigger_block_and_pre_trigger(capture_exe, tmp_path):
    """Parity with the Python engine (test_recorder.py::test_n57_n63_python_trigger_keeps_the_trigger_cycle_when_the_
    trigger_block_is_batched): 10 ms cycle, the trigger block delivered in batches of 10 cycles (max_delay 100 ms),
    start_when counter >= 30, pre_trigger 0 / 50 ms → both blocks start at cycle 30 − pre/10."""
    for pre in (0, 50):
        s = Svc(capture_exe)
        try:
            r = s.call(_start_cmd(tmp_path, f"n57_{pre}", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 4, "leaves": 1},
                                                           {"id": 1, "igrp": 0x4040, "ioffs": 64, "size": 4, "leaves": 1}],
                                  cycle_100ns=100_000, max_delay_100ns=1_000_000, pre_trigger_ms=pre, post_trigger_s=0.15,
                                  start_when={"block": 1, "off": 0, "type": "u32", "op": ">=", "value": 30},
                                  sim={"speed": 20, "batch": [[1, 10]]}))
            assert r["ok"], r
            f = s.wait_event(30)["footer"]
        finally:
            s.close()
        want = 30 - pre // 10
        assert _first_cycles(tmp_path / f"n57_{pre}.jsonl") == {"#0": want, "#1": want}, (pre, f)
        assert f["triggered_at_ms"] == 300.0 and all(b["missing_head"] == 0 for b in f["blocks"]), f


def test_n61_native_level_trigger_true_only_during_the_warmup_is_reported(capture_exe, tmp_path):
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "n61a", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 4, "leaves": 1},
                                                 {"id": 1, "igrp": 0x4040, "ioffs": 64, "size": 4, "leaves": 1}],
                              start_when={"block": 0, "off": 0, "type": "u32", "op": "<=", "value": 2},
                              sim={"speed": 50, "late": [[1, 5]], "max_cycles": 400}))
        assert r["ok"], r
        time.sleep(0.4)
        f = s.call({"cmd": "stop", "reason": "requested"})["footer"]
    finally:
        s.close()
    assert f["start_when_warmup_true_ms"] == 0.0 and any("did not fire" in e for e in f["errors"]), f
    s = Svc(capture_exe)
    try:
        r = s.call(_start_cmd(tmp_path, "n61b", [{"id": 0, "igrp": 0x4040, "ioffs": 0, "size": 4, "leaves": 1},
                                                 {"id": 1, "igrp": 0x4040, "ioffs": 64, "size": 4, "leaves": 1}],
                              start_when={"block": 0, "off": 0, "type": "u32", "op": ">=", "value": 0},
                              post_trigger_s=0.1, sim={"speed": 50, "late": [[1, 3]]}))
        assert r["ok"], r
        f = s.wait_event(30)["footer"]
    finally:
        s.close()
    assert f["triggered_at_ms"] == 4.0 and f["trigger_delayed_ms"] == 4.0 and "already true" in f["trigger_note"], f
