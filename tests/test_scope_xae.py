"""rec_start(also_scope=…) / recorder.also_scope and the XAE Scope tools, against a fake IMeasurementScope."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from mcp import types

from twincat_mcp import xae_backend as X
from twincat_mcp.recorder import file_sha256, is_read_only
from twincat_mcp.server import build_server
from tests.fake_ads import install
from tests.test_server_e2e import client_for, mk_cfg, payload, text


class FakeScope:
    """What the Automation Interface hands out for a Scope configuration (late-bound IMeasurementScope)."""
    def __init__(self):
        self.calls: list[str] = []
        self.recording = False

    def StartRecord(self):
        self.calls.append("start")
        self.recording = True
        return 0

    def StopRecord(self):
        self.calls.append("stop")
        self.recording = False
        return 0

    def SaveSVD(self, path):
        self.calls.append("save")
        Path(path).write_bytes(b"SVDX" * 64)
        return 0

    def ExportCSV(self, path):
        self.calls.append("csv")
        Path(path).write_text("Name;MAIN.nCycle\nTime;Value\n0;1\n", encoding="utf-8")
        return 0


@pytest.fixture
def fake_xae(monkeypatch):
    sc = FakeScope()
    monkeypatch.setattr(X.XaeBackend, "_scope_objects",
                        lambda self: [{"name": "Scope_Counter", "project": "Measurement", "file": "M.tcmproj", "_obj": sc}])
    orig = X.XaeBackend.__init__

    def init(self, cfg):
        orig(self, cfg)
        self.dte = object()              # "attached"
    monkeypatch.setattr(X.XaeBackend, "__init__", init)
    return sc


@pytest.mark.anyio
async def test_ask_policy_asks_before_starting(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change"})
        assert r.is_error and "Ask the user" in text(r) and "Measurement\\Scope_Counter" in text(r)
        assert payload(await s.call_tool("rec_list", {}))["session"] == []          # nothing was started
        assert fake_xae.calls == []
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": False}))
        assert "scope" not in r
        await s.call_tool("rec_stop", {"id": r["id"]})


@pytest.mark.anyio
async def test_elicitation_answer_decides(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    asked = []

    async def elicit(context, params):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"also_scope": True})
    async with client_for(srv, elicitation_callback=elicit) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change"}))
        assert asked and "Scope_Counter" in asked[0] and r["scope"]["started"] and fake_xae.calls == ["start"]
        await s.call_tool("rec_stop", {"id": r["id"]})


@pytest.mark.anyio
async def test_also_scope_records_saves_exports_and_links(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        assert r["scope"]["started"] and r["scope"]["scope"] == "Measurement\\Scope_Counter"
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        assert fake_xae.calls == ["start", "stop", "save", "csv"]
        sc = out["scope"]
        svdx, csv = Path(sc["save_svd"]["path"]), Path(sc["export_csv"]["path"])
        assert svdx.name == f"{r['id']}.svdx" and csv.name == f"{r['id']}.scope.csv"
        assert svdx.parent == Path(out["file"]).parent / "scope"                  # next to the recording
        link = json.loads(Path(sc["link"]["file"]).read_text(encoding="utf-8"))
        assert link["recording"] == r["id"] and link["files"][svdx.name]["sha256"] == file_sha256(svdx)[0] and link["hmac"]
        assert is_read_only(svdx) and is_read_only(csv)


@pytest.mark.anyio
async def test_n117_rec_stop_exports_the_csv_from_the_saved_svdx(monkeypatch, project_dir, fake_xae, tmp_path):
    """N117 (0.8.12 retest): the CSV XAE's ExportCSV wrote right after SaveSVD lacked the end of the .svdx (the last
    600 ms; 3 rows of 58 s). With the export tool installed the CSV is exported from the saved .svdx — it holds what
    the file holds — and XAE's ExportCSV is not called. The Scope View log's new errors come with the result."""
    from tests.test_scope import FAKE
    import sys
    d = tmp_path / "TE130X-Scope-View"
    d.mkdir()
    (d / "fake_export.py").write_text(FAKE)
    exe = d / ("TC3ScopeExportTool.cmd" if os.name == "nt" else "TC3ScopeExportTool.exe")
    exe.write_text(f'@"{sys.executable}" "{d / "fake_export.py"}" %*\r\n' if os.name == "nt" else
                   f'#!/bin/sh\nexec "{sys.executable}" "{d / "fake_export.py"}" "$@"\n')
    exe.chmod(0o755)
    log = tmp_path / "TcScopeView.log"
    log.write_text("2026-10-18 06:18:00 [ERROR] old\n", encoding="utf-8")
    monkeypatch.setenv("TCMCP_FAKE_SCOPE_LOG", str(log))
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.scope.export_tool, cfg.scope.view_log = str(exe), str(log)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        assert fake_xae.calls == ["start", "stop", "save", "csv"]               # ExportCSV only as the cross-check
        sc = out["scope"]
        assert sc["export_csv"]["via"] == "TC3ScopeExportTool.exe from the saved .svdx" and sc["export_csv"]["lines"] == 5
        assert Path(sc["export_csv"]["path"]).name == f"{r['id']}.scope.csv"
        # N117 (0.8.13 retest): the CSV of the saved .svdx and XAE's in-memory ExportCSV, compared every time
        cc = sc["cross_check"]
        assert (cc["svdx_export_lines"], cc["xae_export_lines"], cc["differ_by"]) == (5, 3, -2)
        assert "2 line(s) more than XAE's ExportCSV" in cc["warning"] and Path(cc["xae_export"]).name == f"{r['id']}.scope.xae.csv"
        link = json.loads(Path(sc["link"]["file"]).read_text(encoding="utf-8"))
        assert f"{r['id']}.scope.xae.csv" in link["files"] and is_read_only(Path(cc["xae_export"]))
        assert [e.endswith("Ads-Error 0xA2 (162)") for e in sc["scope_view_log"]["errors"]] == [True]   # not the old one
        # without the tool: XAE's ExportCSV, flagged
        cfg.scope.export_tool = str(tmp_path / "missing.exe")
        sv = payload(await s.call_tool("scope_record", {"action": "save"}))
        assert sv["export_csv"]["via"] == "XAE ExportCSV" and "N117" in sv["export_csv"]["check"]
        assert "not found" in sv["export_csv"]["export_tool"] and fake_xae.calls[-2:] == ["save", "csv"]
        # scope audit (0.8.13): the fallback never writes over an existing CSV, and an unchanged file is not 'written'
        monkeypatch.setattr("twincat_mcp.server.time.strftime", lambda fmt, *a: "20261020-120000")
        payload(await s.call_tool("scope_record", {"action": "save"}))
        sv = payload(await s.call_tool("scope_record", {"action": "save"}))         # the same names again
        assert "exists — XAE's ExportCSV was not run over it" in sv["export_csv"]["error"]


@pytest.mark.anyio
async def test_never_policy_refuses_and_record_tool(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.recorder.also_scope = "never"
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True})
        assert r.is_error and "never" in text(r)
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change"}))   # no question
        await s.call_tool("rec_stop", {"id": r["id"]})
        assert payload(await s.call_tool("scope_list", {}))["count"] == 1
        assert payload(await s.call_tool("scope_record", {"action": "start"}))["result_code"] == 0
        assert payload(await s.call_tool("scope_record", {"action": "stop"}))["result_code"] == 0
        sv = payload(await s.call_tool("scope_record", {"action": "save"}))
        assert Path(sv["save_svd"]["path"]).is_file() and Path(sv["export_csv"]["path"]).is_file()
        assert fake_xae.calls == ["start", "stop", "save", "csv"]


def test_r11_com_methods_are_invoked_not_read_as_properties():
    """R11: on a late-bound dispatch getattr(obj, 'StartRecord') already runs the member as a property get and returns
    its int; calling that int raised "'int' object is not callable"."""
    from twincat_mcp import xae_backend as X
    calls = []

    class Ole:
        def GetIDsOfNames(self, name):
            return {"StartRecord": 7, "SaveSVD": 9}[name]

        def Invoke(self, dispid, lcid, flags, want, *args):
            calls.append((dispid, flags, args))
            return 0

    class LateBound:
        _oleobj_ = Ole()

        def __getattr__(self, name):               # what win32com's dynamic dispatch does for a no-arg member
            return 0
    assert X._com_call(LateBound(), "StartRecord") == 0 and calls == [(7, 1, ())]
    X._com_call(LateBound(), "SaveSVD", "C:/x.svdx")
    assert calls[-1] == (9, 1, ("C:/x.svdx",))


def test_r11_retest_start_record_returning_1_is_not_a_warning(monkeypatch, fake_xae, tmp_path):
    """R11 (0.8.8 retest): StartRecord returns 1 (CONFIG and RUN) while recording normally — no warning; a negative
    value (HRESULT) still is one."""
    from twincat_mcp.config import XaeConfig
    xb = X.XaeBackend(XaeConfig())
    monkeypatch.setattr(fake_xae, "StartRecord", lambda: 1)
    out = xb.scope_action("start")
    assert out["result_code"] == 1 and "warning" not in out and "not an error" in out["result_note"]
    monkeypatch.setattr(fake_xae, "StopRecord", lambda: -2147467259)
    out = xb.scope_action("stop")
    assert "an error code" in out["warning"] and "0x80004005" in out["warning"]
    assert "result_note" not in xb.scope_action("save_svd", path=str(tmp_path / "r11.svdx"))


def test_review_0813_an_unchanged_file_is_not_written(monkeypatch, fake_xae, tmp_path):
    """Scope audit (0.8.13 retest): 'written' was 'is a file and not empty' — a CSV left from before counted as the
    one ExportCSV wrote, although ExportCSV wrote nothing."""
    from twincat_mcp.config import XaeConfig
    xb = X.XaeBackend(XaeConfig())
    old = tmp_path / "old.csv"
    old.write_text("Name;x\n0;1\n", encoding="utf-8")
    monkeypatch.setattr(fake_xae, "ExportCSV", lambda path: 0)                 # returns, writes nothing
    with pytest.raises(X.XaeError, match="the file that was there is unchanged"):
        xb.scope_action("export_csv", path=str(old))
    with pytest.raises(X.XaeError, match="no file was written"):
        xb.scope_action("export_csv", path=str(tmp_path / "new.csv"))


@pytest.mark.anyio
async def test_review_0814_a_svdx_still_being_written_is_not_sealed(monkeypatch, project_dir, fake_xae):
    """Review of 0.8.14: when the saved .svdx still grew, the export was skipped but _scope_link hashed the partial
    file into the signed link and set it read-only; the message suggested scope_export(svd_path=…) — the parameter is
    svd."""
    import threading
    import time as _t
    from twincat_mcp import server as SV
    monkeypatch.setattr(SV, "SCOPE_SETTLE_S", (0.2, 0.6))

    def save_growing(path):
        fake_xae.calls.append("save")
        Path(path).write_bytes(b"SVDX")

        def grow():
            for _ in range(20):
                _t.sleep(0.1)
                with open(path, "ab") as fh:
                    fh.write(b"more")
        threading.Thread(target=grow, daemon=True).start()
        return 0
    monkeypatch.setattr(fake_xae, "SaveSVD", save_growing)
    install(monkeypatch)
    async with client_for(build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        sc = payload(await s.call_tool("rec_stop", {"id": r["id"]}))["scope"]
        svdx = Path(sc["save_svd"]["path"])
        assert "scope_export(svd=" in sc["export_csv"]["skipped"] and "svd_path" not in sc["export_csv"]["skipped"]
        link = json.loads(Path(sc["link"]["file"]).read_text(encoding="utf-8"))
        assert svdx.name not in link["files"] and link["not_sealed"]["file"] == svdx.name, link
        assert not is_read_only(svdx)
    _t.sleep(2.2)                                    # let the writer thread finish before tmp_path goes


@pytest.mark.anyio
async def test_n182_scope_is_finished_when_the_recording_ends_by_itself(monkeypatch, project_dir, fake_xae):
    """N182 (0.8.15 retest, S3): a recording with also_scope that ended by itself (duration 5 s) left Scope
    recording until a rec_stop came ~25 s later (its CSV spanned 28 s), and rec_status showed no scope section. C1:
    the end finishes Scope (stop, save, export, link) and rec_status says so; C3: a later rec_stop returns the same
    result and StopRecord is not sent twice; C2: also for a recording that stops on max_events."""
    import anyio
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        for extra in ({"mode": "change", "duration_s": 1}, {"mode": "poll", "cycle_ms": 50, "max_events": 3}):
            fake_xae.calls.clear()
            r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "also_scope": True, **extra}))
            assert r["scope"]["started"], r
            st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
            seen = [st.get("scope", {}).get("state")]
            for _ in range(80):
                if st.get("scope", {}).get("state") == "finished":
                    break
                await anyio.sleep(0.25)
                st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
                seen.append(st.get("scope", {}).get("state"))
            assert st["scope"]["state"] == "finished" and not st["active"], (extra, seen, st.get("scope"))
            assert "ended by itself" in st["scope"]["finished_by"] and st["scope"]["files"], st["scope"]
            assert fake_xae.calls == ["start", "stop", "save", "csv"], fake_xae.calls
            out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
            assert fake_xae.calls == ["start", "stop", "save", "csv"], fake_xae.calls      # StopRecord once
            assert "ended by itself" in out["scope"]["finished_by"] and out["scope"]["link"]["files"], out["scope"]
            link = json.loads(Path(out["scope"]["link"]["file"]).read_text(encoding="utf-8"))
            assert "ended by itself" in link["finished_by"] and link["recording_ended"]["stop_reason"], link
            lst = payload(await s.call_tool("rec_list", {}))
            mine = [x for x in lst["session"] if x.get("id") == r["id"]]
            assert mine and mine[0]["scope"]["state"] == "finished", mine
    audit = Path(cfg.safety.audit_log).read_text(encoding="utf-8")
    assert audit.count('"scope_finish"') == 2, audit.count('"scope_finish"')          # one per recording


@pytest.mark.anyio
async def test_n180_save_after_a_twincat_restart_is_said_and_recovered_or_lost(monkeypatch, project_dir, fake_xae,
                                                                               tmp_path):
    """N180 (0.8.15 retest, S3): after a TwinCAT RUN→CONFIG→RUN during the recording, rec_stop's SaveSVD gave the raw
    'com_error: Invalid ScopeState! Reply requested, actual: Config (HRESULT 0x80020009)', no .svdx, no CSV. D1: said
    as the reset (with the time from the Scope View log), the raw text in detail; what ExportCSV still gets is kept
    (recovered) — or the run's Scope data is said lost. D2 (no restart) is the existing also_scope test."""
    install(monkeypatch)
    log = tmp_path / "TE130X" / "TcScopeView.log"
    log.parent.mkdir()
    log.write_text("(2026-10-21-15:15:00:000)[ INFO  ] start\n", encoding="utf-8")

    def refused(path):
        fake_xae.calls.append("save")
        raise RuntimeError("Invalid ScopeState! Reply requested, actual: Config")
    monkeypatch.setattr(fake_xae, "SaveSVD", refused)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.scope.view_log = str(log)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("(2026-10-21-15:16:25:123)[ INFO  ] Scope View: Scope Project state changes from 'ServerReset' "
                     "to 'Config'\n")
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        sv = out["scope"]["save_svd"]
        assert sv["error"].startswith("Scope was reset to Config by a TwinCAT restart at 2026-10-21 15:16:25"), sv
        assert "Invalid ScopeState" in sv["detail"] and "nothing was saved" in sv["error"], sv
        ex = out["scope"]["export_csv"]
        assert ex["samples"] == 1 and "no .svdx" in ex["recovered"], ex          # what Scope still held: kept
        # ExportCSV refused as well: the run's Scope data is said lost
        def no_csv(path):
            fake_xae.calls.append("csv")
            raise RuntimeError("Invalid ScopeState! Reply requested, actual: Config")
        monkeypatch.setattr(fake_xae, "ExportCSV", no_csv)
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        # eleventh review: no reset line since this recording began — the state is said, no restart invented
        sv = out["scope"]["save_svd"]
        assert sv["error"].startswith("Scope is in state Config, not Reply — the Scope View log names no reset"), sv
        assert "restart" not in sv["error"] and "reset_seen" not in sv, sv
        assert "Scope was in state Config and refused both" in out["scope"]["export_csv"]["lost"], out["scope"]

        def record_state(path):
            raise RuntimeError("Invalid ScopeState! Reply requested, actual: Record")
        monkeypatch.setattr(fake_xae, "SaveSVD", record_state)
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        assert out["scope"]["save_svd"]["error"].startswith("Scope is in state Record, not Reply"), out["scope"]


@pytest.mark.anyio
async def test_review11_0816_rec_stop_is_never_taken_for_an_end_of_its_own(monkeypatch, project_dir, fake_xae):
    """Eleventh review of 0.8.16: a recording turns inactive at the start of its stop; while rec_stop sealed a large
    file (0.3 s and more) the N182 watch finished the Scope run and wrote 'the recording ended by itself (manual
    (rec_stop))' into rec_stop's result and the signed .scope.json link. rec_stop's stop is its own."""
    import json as _json
    import time as _t
    from pathlib import Path
    from twincat_mcp import recorder as R
    from twincat_mcp.server import build_server
    from tests.fake_ads import install
    from tests.test_server_e2e import client_for, mk_cfg, payload
    install(monkeypatch)
    orig = R.Recorder.stop

    def slow_stop(self, rid, reason="requested"):
        st = orig(self, rid, reason)
        _t.sleep(1.2)                                  # the seal / stats of a large recording, after it turned inactive
        return st
    monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    assert out["scope"]["finished_by"] == "rec_stop" and "recording_ended" not in out["scope"], out["scope"]
    link = _json.loads(Path(out["scope"]["link"]["file"]).read_text(encoding="utf-8"))
    assert link.get("finished_by") == "rec_stop", link
