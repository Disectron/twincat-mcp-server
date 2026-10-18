"""The acceptance runner against the fakes: it must exercise or name every tool, never crash on a
tool error, and produce a report. On Linux the XAE/IPC phases skip; the guard proofs must PASS."""
from pathlib import Path

import pytest

from twincat_mcp import acceptance as A
from twincat_mcp.config import Config, validate
from tests.fake_ads import install


@pytest.mark.anyio
async def test_acceptance_runs_against_fakes(monkeypatch, project_dir, tmp_path):
    install(monkeypatch)
    cfg = Config()
    cfg.project.root = str(project_dir)
    cfg.ads.ams_net_id = "5.1.2.3.1.1"
    cfg.safety.mode = "guarded"
    cfg.docs.enabled = False
    validate(cfg)
    logs: list[str] = []
    # the fake target only emits notifications when told to: pump the two discovered symbols like a 100 ms task would
    import asyncio
    from tests.fake_ads import FakeConnection
    stop = asyncio.Event()

    import time as _time
    c = "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount"
    t0 = int(_time.time() * 1e7) + 116444736000000000

    async def pump():
        # a 10 ms task with its own clock: every step is exactly 5 cycles and 50 ms of PLC time, whatever the host's
        # sleep jitter (Windows: ~15.6 ms) — the counter, its timestamps and the other symbols agree (D29, rec_verify)
        i = 0
        while not stop.is_set():
            i += 1
            ts = t0 + i * 500_000
            FakeConnection.broadcast("MAIN.nCycle", 42 + (i % 3), ts)
            FakeConnection.broadcast("MAIN.bStart", bool(i % 2), ts)
            FakeConnection.shared_values[c] = 123456 + 5 * i
            FakeConnection.broadcast(c, FakeConnection.shared_values[c], ts)
            await asyncio.sleep(0.05)
    task = asyncio.create_task(pump())
    try:
        rep = await A.run_acceptance(cfg, tmp_path / "acc", with_xae=False, with_infosys=False, log=logs.append)
    finally:
        stop.set()
        await task
    assert rep["unexercised"] == [], rep["unexercised"]
    by = {(c["tool"], c["phase"]): c for c in rep["checks"]}
    statuses = {c["tool"]: c["status"] for c in rep["checks"]}
    # the guard proofs are the point of guarded mode
    for t in ("ads_write", "ads_write_many", "ads_plc_state", "ads_system_mode", "ipc_reboot", "ipc_start_process"):
        assert statuses[t] == "PASS", (t, by[(t, "6 guard")]["detail"] if (t, "6 guard") in by else statuses[t])
    # core read path must pass on the fake target
    for t in ("tc_doctor", "ads_status", "ads_list_symbols", "ads_read", "ads_read_many", "ads_leaves", "ads_task_stats",
              "proj_scan", "proj_lint", "proj_validate_xml", "rec_profile_save", "rec_profile_start", "rec_stats", "rec_export",
              "snap_save", "snap_compare", "exp_template", "exp_validate", "tc_session_report"):
        assert statuses[t] == "PASS", (t, [c["detail"] for c in rep["checks"] if c["tool"] == t])
    fails = [c for c in rep["checks"] if c["status"] == "FAIL"]
    assert fails == [], fails
    md = Path(rep["report_md"])
    assert md.exists() and "PASS" in md.read_text()
    assert rep["counts"]["MANUAL"] >= 30


def test_manual_list_names_only_real_tools():
    from twincat_mcp.server import build_server
    cfg = Config(); validate(cfg)
    srv = build_server(cfg)
    import asyncio
    names = {t.name for t in asyncio.run(srv.list_tools())}
    unknown = sorted(set(A.MANUAL) - names)
    assert unknown == [], unknown
    assert sorted(set(A.OPTIONAL) - names) == []
