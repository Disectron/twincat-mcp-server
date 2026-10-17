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
