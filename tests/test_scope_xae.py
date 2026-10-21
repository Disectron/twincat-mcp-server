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


def _done(app, rid):
    """The result of the recording's Scope part once a finish ran (finished, refused, unknown) — else None."""
    part = app.scope_parts.get(rid)
    return part.result if part is not None and part.state != "finishing" else None


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
        assert out["scope"]["save_svd"]["error"].startswith("Scope is in state Record, not Reply — SaveSVD"), out["scope"]


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



# ---- twelfth review of 0.8.16: the Scope part's bookkeeping (who finishes it, an interrupted rec_stop, an id prefix,
# a failed part with two waiters), the cause of a refused save, scope_record's save after a reset
import time as _t12
import anyio as _anyio12
from twincat_mcp.server import build_server
from tests.fake_ads import install
from tests.test_server_e2e import client_for, mk_cfg, payload, text
@pytest.mark.anyio
async def test_review12_0816_finishing_names_who_finishes(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    orig = fake_xae.StopRecord

    def slow_stop():
        _t12.sleep(2.0)
        return orig()
    monkeypatch.setattr(fake_xae, "StopRecord", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "duration_s": 1,
                                                     "also_scope": True}))
        seen = []
        for _ in range(40):
            st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
            sc = st.get("scope") or {}
            seen.append((sc.get("state"), sc.get("finished_by")))
            if sc.get("state") == "finished":
                break
            await _anyio12.sleep(0.2)
        assert ("finishing", None) not in seen and any(st == "finishing" and "ended by itself" in str(by)
                                                       for st, by in seen), seen


@pytest.mark.anyio
async def test_review12_0816_rec_stop_by_an_id_prefix_finishes_its_scope(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        pre = r["id"][:-2]
        st = payload(await s.call_tool("rec_status", {"id": pre}))
        out = payload(await s.call_tool("rec_stop", {"id": pre}))
        for _ in range(40):
            st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
            if (st.get("scope") or {}).get("state") == "finished":
                break
            await _anyio12.sleep(0.2)
        assert out["scope"]["finished_by"] == "rec_stop" and st["scope"]["finished_by"] == "rec_stop", (out, st)


@pytest.mark.anyio
async def test_review12_0816_rec_stop_cancelled_in_the_scope_part(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    orig = fake_xae.SaveSVD

    def slow_save(path):
        _t12.sleep(2.0)
        return orig(path)
    monkeypatch.setattr(fake_xae, "SaveSVD", slow_save)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        with _anyio12.move_on_after(1.0):
            await s.call_tool("rec_stop", {"id": r["id"]})
        await _anyio12.sleep(6.0)                       # the Scope part has long finished (stop, save 2 s, csv)
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        assert (st.get("scope") or {}).get("state") == "finished", st.get("scope")


@pytest.mark.anyio
async def test_review12_0816_rec_stop_cancelled_in_the_ads_stop(monkeypatch, project_dir, fake_xae):
    from twincat_mcp import recorder as R
    install(monkeypatch)
    orig = R.Recorder.stop

    def slow_stop(self, rid, reason="requested"):
        st = orig(self, rid, reason)
        _t12.sleep(1.5)                      # sealing / stats of a large recording
        return st
    monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        with _anyio12.move_on_after(0.5):
            await s.call_tool("rec_stop", {"id": r["id"]})
        for _ in range(40):
            await _anyio12.sleep(0.25)
            st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
            if (st.get("scope") or {}).get("state") == "finished":
                break
        assert st["scope"]["state"] == "finished" and st["scope"]["finished_by"].startswith(
            "rec_stop (the call ended before its Scope part"), st.get("scope")


@pytest.mark.anyio
async def test_review12_0816_a_failed_scope_part_is_stored_once(monkeypatch, project_dir, fake_xae):
    from twincat_mcp import scope as S
    install(monkeypatch)
    orig = fake_xae.StopRecord

    def slow_stop():
        _t12.sleep(2.0)
        return orig()
    monkeypatch.setattr(fake_xae, "StopRecord", slow_stop)

    def boom(mark, limit=20):
        raise RuntimeError("log unreadable")
    monkeypatch.setattr(S, "log_errors_since", boom)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "duration_s": 1,
                                                     "also_scope": True}))
        for _ in range(40):
            st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
            if (st.get("scope") or {}).get("state") == "finishing":
                break
            await _anyio12.sleep(0.2)
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        assert "ended by itself" in str(out["scope"].get("finished_by")), out["scope"]
        assert st["scope"]["finished_by"] == out["scope"]["finished_by"] and "check in XAE whether Scope stopped" in             out["scope"]["error"], (out["scope"], st["scope"])


@pytest.mark.anyio
async def test_review12_0816_the_start_transition_is_no_restart(monkeypatch, project_dir, fake_xae, tmp_path):
    install(monkeypatch)
    log = tmp_path / "TE130X" / "TcScopeView.log"
    log.parent.mkdir()
    log.write_text("(2026-10-21-15:15:00:000)[ INFO  ] start\n", encoding="utf-8")
    orig_start = fake_xae.StartRecord

    def start():
        with open(log, "a", encoding="utf-8") as fh:     # what the Scope View logs when a recording starts (assumed)
            fh.write("(2026-10-21-15:15:01:000)[ INFO  ] Scope View: Scope Project state changes from 'Reply' to 'Record'\n")
        return orig_start()
    monkeypatch.setattr(fake_xae, "StartRecord", start)

    def record_state(path):
        raise RuntimeError("Invalid ScopeState! Reply requested, actual: Record")
    monkeypatch.setattr(fake_xae, "SaveSVD", record_state)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.scope.view_log = str(log)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    # thirteenth review: the log is searched for a reset to Config only — for another state it is not said read
    assert out["scope"]["save_svd"]["error"] == ("Scope is in state Record, not Reply — SaveSVD refused; nothing was "
                                                 "saved as .svdx"), out["scope"]["save_svd"]


@pytest.mark.anyio
async def test_review12_0816_scope_record_save_in_config_state(monkeypatch, project_dir, fake_xae, tmp_path):
    install(monkeypatch)

    def refused(path):
        fake_xae.calls.append("save")
        raise RuntimeError("Invalid ScopeState! Reply requested, actual: Config")
    monkeypatch.setattr(fake_xae, "SaveSVD", refused)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.scope.view_log = str(tmp_path / "nowhere" / "TcScopeView.log")
    srv = build_server(cfg)
    out = tmp_path / "home" / "recordings" / "manual.svdx"
    async with client_for(srv) as s:
        r = await s.call_tool("scope_record", {"action": "save", "path": str(out)})
    csvp = tmp_path / "home" / "recordings" / "manual.csv"
    t = text(r)
    assert r.is_error and csvp.exists() and f"recovered as {csvp}" in t and "could not be read" in t, t
    assert "since the recording began" not in t, t


# ---- thirteenth review of 0.8.16: who finishes the Scope part when rec_stop / rec_delete race the watch -------------

class _R13Busy(Exception):
    hresult = -2147418111          # RPC_E_CALL_REJECTED: the shell did not run the call


async def _r13_finished(s, rid, n=60):
    st: dict = {}
    for _ in range(n):
        st = payload(await s.call_tool("rec_status", {"id": rid}))
        if (st.get("scope") or {}).get("state") == "finished":
            break
        await _anyio12.sleep(0.25)
    return st


@pytest.mark.anyio
async def test_review13_0816_rec_stop_cancelled_while_the_recording_is_still_active(monkeypatch, project_dir, fake_xae):
    """rec_stop ends (cancelled, its timeout) before the ADS stop made the recording inactive: the watch finishes the
    Scope part — as rec_stop's, never as 'the recording ended by itself'."""
    from twincat_mcp import recorder as R
    install(monkeypatch)
    orig = R.Recorder.stop

    def slow_stop(self, rid, reason="requested"):
        _t12.sleep(1.5)                      # the stop is still under way, the recording still active
        return orig(self, rid, reason)
    monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        with _anyio12.move_on_after(0.5):
            await s.call_tool("rec_stop", {"id": r["id"]})
        st = await _r13_finished(s, r["id"])
    assert st["scope"]["finished_by"] == "rec_stop (the call ended before its Scope part — finished after it)", st
    assert "recording_ended" not in _done(srv._tcmcp_app, r["id"]) and fake_xae.calls == ["start", "stop", "save",
                                                                                                  "csv"], fake_xae.calls


@pytest.mark.anyio
async def test_review13_0816_a_second_rec_stop_cancelled_keeps_the_first_ones_part(monkeypatch, project_dir, fake_xae):
    """Two rec_stops of one recording, the second cancelled while the first is in its ADS stop: the first one still
    finishes the Scope part as its own."""
    from twincat_mcp import recorder as R
    install(monkeypatch)
    orig = R.Recorder.stop

    def slow_stop(self, rid, reason="requested"):
        st = orig(self, rid, reason)         # inactive now
        _t12.sleep(1.5)                      # sealing / stats of a large recording
        return st
    monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        first: dict = {}

        async def stop1():
            first["r"] = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        async with _anyio12.create_task_group() as tg:
            tg.start_soon(stop1)
            await _anyio12.sleep(0.2)
            with _anyio12.move_on_after(0.3):
                await s.call_tool("rec_stop", {"id": r["id"]})
    assert first["r"]["scope"]["finished_by"] == "rec_stop" and fake_xae.calls.count("stop") == 1, (first, fake_xae.calls)


@pytest.mark.anyio
async def test_review13_0816_two_rec_stops_share_one_scope_part(monkeypatch, project_dir, fake_xae):
    from twincat_mcp import recorder as R
    install(monkeypatch)
    orig = R.Recorder.stop

    def slow_stop(self, rid, reason="requested"):
        st = orig(self, rid, reason)
        _t12.sleep(0.8)
        return st
    monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        outs: list = []

        async def stop():
            outs.append(payload(await s.call_tool("rec_stop", {"id": r["id"]})))
        async with _anyio12.create_task_group() as tg:
            tg.start_soon(stop)
            await _anyio12.sleep(0.1)
            tg.start_soon(stop)
    assert [o["scope"]["finished_by"] for o in outs] == ["rec_stop", "rec_stop"], outs
    assert fake_xae.calls == ["start", "stop", "save", "csv"], fake_xae.calls


@pytest.mark.anyio
@pytest.mark.parametrize("slow", [False, True])
async def test_review13_0816_rec_delete_stops_scope_and_saves_nothing(monkeypatch, project_dir, fake_xae, slow):
    """rec_delete of a recording with a Scope part: Scope is stopped, nothing saved (the recording is gone) — also when
    the ADS stop takes a while and the watch sees the recording end first; a later rec_stop starts nothing."""
    from twincat_mcp import recorder as R
    install(monkeypatch)
    if slow:
        orig = R.Recorder.stop

        def slow_stop(self, rid, reason="requested"):
            st = orig(self, rid, reason)
            _t12.sleep(1.5)
            return st
        monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": r["id"]}))
        d = payload(await s.call_tool("rec_delete", {"id": r["id"], "confirm_token": tok["confirm_token"]}))
        await _anyio12.sleep(2.0)
        rs = await s.call_tool("rec_stop", {"id": r["id"]})
        await _anyio12.sleep(1.0)
    res = _done(app, r["id"])
    assert not fake_xae.recording and fake_xae.calls == ["start", "stop"], fake_xae.calls
    assert res["finished_by"] == "rec_delete" and "nothing was saved" in res["note"], res
    assert d["scope"]["finished_by"] == "rec_delete" and rs.is_error and app.scope_parts[r["id"]].state == "finished", (d, text(rs))


@pytest.mark.anyio
async def test_review13_0816_a_stop_refused_after_the_end_is_no_step_of_rec_start(monkeypatch, project_dir, fake_xae):
    """The recording ends by itself and XAE refuses StopRecord: the watch runs in a context of its own — the refusal
    names no 'earlier step' of rec_start, is stored as not run, and rec_stop asks again (Scope then stops and saves)."""
    install(monkeypatch)
    orig = fake_xae.StopRecord
    state = {"busy": True}

    def stop():
        if state["busy"]:
            fake_xae.calls.append("stop-rejected")
            raise _R13Busy("call rejected")
        return orig()
    monkeypatch.setattr(fake_xae, "StopRecord", stop)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "duration_s": 1,
                                                     "also_scope": True}))
        for _ in range(60):
            if _done(app, r["id"]) is not None:
                break
            await _anyio12.sleep(0.25)
        refused = dict(_done(app, r["id"]))
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        state["busy"] = False                       # XAE answers again
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    assert refused["stop"].get("not_run") and "earlier step" not in str(refused["stop"]), refused
    assert st["scope"]["state"] == "recording" and f"rec_stop(id='{r['id']}') asks again" in st["scope"]["note"], \
        st["scope"]
    assert not fake_xae.recording and fake_xae.calls[-3:] == ["stop", "save", "csv"], fake_xae.calls
    assert out["scope"]["finished_by"] == "rec_stop" and out["scope"]["save_svd"]["result_code"] == 0, out["scope"]


@pytest.mark.anyio
async def test_review13_0816_scope_start_refused_never_says_repeat_rec_start(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)

    def busy():
        fake_xae.calls.append("start-rejected")
        raise _R13Busy("call rejected")
    monkeypatch.setattr(fake_xae, "StartRecord", busy)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        await s.call_tool("rec_stop", {"id": r["id"]})
    e = r["scope"]["error"]
    assert e.startswith("Scope recording NOT started: XAE refused StartRecord") and "do not repeat rec_start" in e, e
    assert "repeat it" not in e and "start-rejected" in fake_xae.calls and "stop" not in fake_xae.calls, (e, fake_xae.calls)


# ---- thirteenth review of 0.8.16: the cause of a refused save is said only from what the Scope View log was read for --

_R13_RESET = "(2026-10-21-15:16:25:000)[ INFO  ] Scope View: Scope Project state changes from 'ServerReset' to 'Config'\n"


def _r13_saved_in(monkeypatch, project_dir, fake_xae, tmp_path, state, during=None):
    install(monkeypatch)
    log = tmp_path / "TE130X" / "TcScopeView.log"
    log.parent.mkdir()
    log.write_text("(2026-10-21-15:15:00:000)[ INFO  ] start\n", encoding="utf-8")
    orig_start = fake_xae.StartRecord

    def start():
        rc = orig_start()
        if during:
            during(log)
        return rc
    monkeypatch.setattr(fake_xae, "StartRecord", start)

    def refused(path):
        raise RuntimeError(f"Invalid ScopeState! Reply requested, actual: {state}")
    monkeypatch.setattr(fake_xae, "SaveSVD", refused)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.scope.view_log = str(log)
    return build_server(cfg), log


async def _r13_save_error(srv):
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    return out["scope"]["save_svd"]["error"]


@pytest.mark.anyio
async def test_review13_0816_a_state_other_than_config_names_no_log(monkeypatch, project_dir, fake_xae, tmp_path):
    def during(log):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(_R13_RESET)
    srv, _ = _r13_saved_in(monkeypatch, project_dir, fake_xae, tmp_path, "Record", during)
    e = await _r13_save_error(srv)
    assert e == "Scope is in state Record, not Reply — SaveSVD refused; nothing was saved as .svdx", e


@pytest.mark.anyio
async def test_review13_0816_an_unreadable_log_is_not_said_read(monkeypatch, project_dir, fake_xae, tmp_path):
    from twincat_mcp import scope as S

    def during(log):
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(_R13_RESET)

        def locked(path, start, end):
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        monkeypatch.setattr(S, "_new_lines", locked)
    srv, _ = _r13_saved_in(monkeypatch, project_dir, fake_xae, tmp_path, "Config", during)
    e = await _r13_save_error(srv)
    assert e == ("Scope is in state Config, not Reply (the Scope View log is missing or could not be read: whether "
                 "TwinCAT was restarted is not known) — SaveSVD refused; nothing was saved as .svdx"), e


@pytest.mark.anyio
async def test_review13_0816_a_rotated_log_is_read_from_its_start(monkeypatch, project_dir, fake_xae, tmp_path):
    def during(log):
        old = log.read_bytes()
        log.write_text(_R13_RESET + "(2026-10-21-15:16:26:000)[ INFO  ] " + "x" * (len(old) + 50) + "\n", encoding="utf-8")
    srv, log = _r13_saved_in(monkeypatch, project_dir, fake_xae, tmp_path, "Config", during)
    log.write_text("".join(f"(2026-10-21-15:{i:02d}:00:000)[ INFO  ] an earlier line of the old log\n" for i in range(20)),
                   encoding="utf-8")
    e = await _r13_save_error(srv)
    assert e.startswith("Scope was reset to Config by a TwinCAT restart at 2026-10-21 15:16:25 (Scope View log)"), e


@pytest.mark.anyio
async def test_review13_0816_a_log_unread_at_the_mark_names_no_old_reset(monkeypatch, project_dir, fake_xae, tmp_path):
    from twincat_mcp import scope as S
    srv, log = _r13_saved_in(monkeypatch, project_dir, fake_xae, tmp_path, "Config")
    log.write_text("(2026-10-18-06:00:00:000)[ INFO  ] Scope View: Scope Project state changes from 'Run' to 'Config'\n",
                   encoding="utf-8")
    orig_mark = S._mark_one

    def mark_fails(path):
        m = orig_mark(path)
        return {"path": m["path"], "size": None, "error": "PermissionError: locked"}
    monkeypatch.setattr(S, "_mark_one", mark_fails)
    e = await _r13_save_error(srv)
    assert "2026-10-18" not in e and "is missing or could not be read" in e, e


@pytest.mark.anyio
async def test_review13_0816_scope_record_save_names_its_own_way_out(monkeypatch, project_dir, fake_xae, tmp_path):
    """scope_record(save) refused, the CSV beside the path exists: the error names scope_record's way out (another
    path) — scope_record has no overwrite argument."""
    install(monkeypatch)

    def refused(path):
        raise RuntimeError("Invalid ScopeState! Reply requested, actual: Config")
    monkeypatch.setattr(fake_xae, "SaveSVD", refused)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.scope.view_log = str(tmp_path / "nowhere" / "TcScopeView.log")
    srv = build_server(cfg)
    out = tmp_path / "home" / "recordings" / "manual.svdx"
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / "manual.csv").write_text("old\n", encoding="utf-8")
    async with client_for(srv) as s:
        r = await s.call_tool("scope_record", {"action": "save", "path": str(out)})
    t = text(r)
    assert r.is_error and "overwrite" not in t and "manual.csv exists — not written over (give scope_record another " \
        "path)" in t, t
    assert (out.parent / "manual.csv").read_text(encoding="utf-8") == "old\n"


@pytest.mark.anyio
async def test_review13_0816_a_failed_rec_delete_still_finishes_scope(monkeypatch, project_dir, fake_xae):
    """rec_delete stopped the recording, then the file could not be removed (a handle on Windows): the watch had left
    the Scope part to rec_delete — it is finished after it, saved since the recording is still on disk."""
    from twincat_mcp import recorder as R
    install(monkeypatch)

    def locked(p, tries=10):
        raise R.RecorderError(f"cannot delete {p.name}: [WinError 32] used by another process")
    monkeypatch.setattr(R, "_remove_file", locked)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": r["id"]}))
        d = await s.call_tool("rec_delete", {"id": r["id"], "confirm_token": tok["confirm_token"]})
        st = await _r13_finished(s, r["id"])
    assert d.is_error and "WinError 32" in text(d), text(d)
    assert st["scope"]["finished_by"] == "rec_delete (the call ended before its Scope part — finished after it)", st
    assert not fake_xae.recording and fake_xae.calls == ["start", "stop", "save", "csv"], fake_xae.calls
    assert "recording_ended" not in _done(app, r["id"]), _done(app, r["id"])


@pytest.mark.anyio
async def test_review13_0816_rec_delete_with_stop_refused_names_scope_record(monkeypatch, project_dir, fake_xae):
    """rec_delete, XAE refuses StopRecord: the recording is gone — first review of 0.8.17: the part stays (a note no
    tool could show left Scope recording with nothing to stop it); rec_list lists it, rec_status shows it, and
    rec_stop(id) asks again once XAE is free — nothing is saved for the deleted recording."""
    install(monkeypatch)
    orig = fake_xae.StopRecord
    state = {"busy": True}

    def stop():
        if state["busy"]:
            fake_xae.calls.append("stop-rejected")
            raise _R13Busy("call rejected")
        return orig()
    monkeypatch.setattr(fake_xae, "StopRecord", stop)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": r["id"]}))
        d = payload(await s.call_tool("rec_delete", {"id": r["id"], "confirm_token": tok["confirm_token"]}))
        lst = payload(await s.call_tool("rec_list", {}))
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        state["busy"] = False
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    sc = d["scope"]
    assert sc["stop"]["not_run"] and f"rec_stop(id='{r['id']}') asks again" in sc["note"], sc
    assert "the recording was deleted: nothing will be saved" in sc["note"], sc
    assert [o["id"] for o in lst.get("scope_without_recording") or []] == [r["id"]], lst.get("scope_without_recording")
    assert st["scope"]["state"] == "recording" and st["scope"].get("stop_refused"), st
    assert not fake_xae.recording and "save" not in fake_xae.calls, fake_xae.calls
    assert out["scope"]["stop"]["result_code"] == 0 and "nothing was saved" in out["scope"]["note"], out["scope"]
    assert app.scope_parts[r["id"]].state == "finished"


# ---- fourteenth review of 0.8.16: an error of the status call is no end of the recording -----------------------------

@pytest.mark.anyio
async def test_review14_0816_a_failed_status_call_does_not_stop_scope(monkeypatch, project_dir, fake_xae):
    """One status call of the watch fails while the recording runs: Scope records on; rec_stop finishes it as its own."""
    import threading as _th
    from twincat_mcp import recorder as R
    install(monkeypatch)
    orig = R.Recorder.status
    arm = {"on": False, "hit": False}

    def status(self, rid):
        if arm["on"] and _th.current_thread().name != "MainThread":
            arm["on"], arm["hit"] = False, True
            raise RuntimeError("dictionary changed size during iteration")
        return orig(self, rid)
    monkeypatch.setattr(R.Recorder, "status", status)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        arm["on"] = True
        for _ in range(40):
            if arm["hit"]:
                break
            await _anyio12.sleep(0.1)
        await _anyio12.sleep(1.5)                          # several watch periods after the failed call
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    assert arm["hit"] and st["active"] and st["scope"]["state"] == "recording", st
    assert fake_xae.recording is False and out["scope"]["finished_by"] == "rec_stop", out["scope"]
    assert "recording_ended" not in _done(app, r["id"]), _done(app, r["id"])


@pytest.mark.anyio
async def test_review14_0816_a_rec_start_during_a_slow_reap_does_not_stop_scope(monkeypatch, project_dir, fake_xae):
    """The recorder's own race: a recording that ended by itself is reaped slowly (a slow ADS close) while a rec_start
    adds another recording — the reap loop failed every status call, and the watch stopped Scope of a running one."""
    import threading as _th
    from tests import fake_ads as FA
    fc = install(monkeypatch)
    slow = {"armed": False, "in": _th.Event()}
    orig_close = fc.close

    def close(self):
        if slow["armed"] and not _th.current_thread().name.startswith("rec-poll"):
            slow["armed"] = False
            slow["in"].set()
            _t12.sleep(1.5)                                # a slow ADS close (router busy, target gone)
        return orig_close(self)
    monkeypatch.setattr(FA.FakeConnection, "close", close)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "poll", "cycle_ms": 50,
                                                "max_events": 3, "also_scope": False}))
        b = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        slow["armed"] = True
        for _ in range(100):
            if slow["in"].is_set():
                break
            await _anyio12.sleep(0.05)
        c = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": False}))
        await _anyio12.sleep(2.5)
        st = payload(await s.call_tool("rec_status", {"id": b["id"]}))
        await s.call_tool("rec_stop", {"id": b["id"]})
        await s.call_tool("rec_stop", {"id": c["id"]})
    assert slow["in"].is_set(), "the slow close was never reached"
    assert st["active"] and st["scope"]["state"] == "recording", st


# ---- 0.8.17: one owner per recording's Scope part; the fourteenth review's LOW findings ------------------------------

@pytest.mark.anyio
async def test_0817_rec_stop_with_scope_carries_the_binding_flags(monkeypatch, project_dir, fake_xae):
    """N116: rec_stop's Scope part runs in its owner task — its result still says what the XAE binding was."""
    install(monkeypatch)
    monkeypatch.setattr(X.XaeBackend, "mismatch_note", lambda self: "bound to C:\\P\\Other.sln, not xae.solution_path")
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    assert r.get("solution_mismatch") and out.get("solution_mismatch"), sorted(out)
    assert out["scope"].get("solution_mismatch"), out["scope"]


@pytest.mark.anyio
async def test_0817_save_refused_after_the_stop_ran_is_said_as_that(monkeypatch, project_dir, fake_xae):
    """SaveSVD refused (busy) right after StopRecord ran: no 'repeat it' — what Scope holds is saved by scope_record."""
    install(monkeypatch)
    orig = fake_xae.SaveSVD

    def save(path):
        fake_xae.calls.append("save-rejected")
        raise _R13Busy("call rejected")
    monkeypatch.setattr(fake_xae, "SaveSVD", save)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    sv = out["scope"]["save_svd"]
    assert sv.get("not_run") and "repeat it" not in sv["error"] and "retry" not in sv["error"], sv
    assert "earlier step(s) ran (Scope StopRecord)" in sv["error"], sv
    assert "scope_record(action='save') saves it once XAE is free" in out["scope"]["note"], out["scope"]
    assert orig is not None and fake_xae.calls.count("stop") == 1


@pytest.mark.anyio
async def test_0817_a_stop_refused_by_a_held_dialog_is_asked_again(monkeypatch, project_dir, fake_xae):
    """XAE held by a dialog another call opened when the recording ends: StopRecord did not run — Scope still
    records, rec_status says so, and rec_stop asks again once the dialog is answered."""
    import concurrent.futures as _cf
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    fut: _cf.Future = _cf.Future()
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "duration_s": 1,
                                                     "also_scope": True}))
        app.xae_held = {"job": "xae_tree_get", "since": _t12.monotonic(), "future": fut,
                        "dialog": {"hwnd": 77, "title": "Options", "class": "#32770", "buttons": ["OK", "Cancel"],
                                   "text": ""}}
        for _ in range(60):
            if _done(app, r["id"]) is not None:
                break
            await _anyio12.sleep(0.25)
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        fut.set_result({"done": True})                     # the dialog was answered: XAE is free
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    assert st["scope"]["state"] == "recording" and f"rec_stop(id='{r['id']}') asks again" in st["scope"]["note"], \
        st["scope"]
    assert "NOT started: XAE is held by the dialog" in st["scope"]["stop_refused"], st["scope"]
    assert not fake_xae.recording and out["scope"]["finished_by"] == "rec_stop", (fake_xae.calls, out["scope"])
    assert out["scope"]["save_svd"]["result_code"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("stop_first", [False, True])
async def test_0817_a_cancelled_rec_delete_saves_nothing_and_names_itself(monkeypatch, project_dir, fake_xae,
                                                                          stop_first):
    """rec_delete cancelled — after its stop, while the file is removed (stop_first=False), or while its stop is still
    under way (stop_first=True): the part waits for the delete's outcome, saves nothing for the deleted recording and
    says rec_delete ended it."""
    from twincat_mcp import recorder as R
    install(monkeypatch)
    if stop_first:
        orig_stop = R.Recorder.stop

        def slow_stop(self, rid, reason="requested"):
            _t12.sleep(1.5)                                # the stop not yet under way: the recording is active
            return orig_stop(self, rid, reason)
        monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    else:
        orig_rm = R._remove_file

        def slow_remove(p, tries=10):
            _t12.sleep(1.5)                                # Windows: a scanner holds the file, its removal retried
            return orig_rm(p, tries)
        monkeypatch.setattr(R, "_remove_file", slow_remove)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": r["id"]}))
        with _anyio12.move_on_after(0.4):
            await s.call_tool("rec_delete", {"id": r["id"], "confirm_token": tok["confirm_token"]})
        for _ in range(100):
            if _done(app, r["id"]) is not None:
                break
            await _anyio12.sleep(0.1)
        await _anyio12.sleep(1.0)
    res = _done(app, r["id"])
    left = sorted(p.name for p in (app.rec.root / "scope").iterdir()) if (app.rec.root / "scope").is_dir() else []
    assert not (app.rec.root / f"{r['id']}.jsonl").exists(), "the delete did not go through"
    assert not any(n.startswith(r["id"]) for n in left), left
    assert res["finished_by"] == "rec_delete (the call ended before its Scope part — finished after it)", res
    assert "nothing was saved" in res["note"] and not fake_xae.recording and "save" not in fake_xae.calls, res


@pytest.mark.anyio
async def test_0817_a_start_held_by_a_dialog_stays_with_the_recording(monkeypatch, project_dir, fake_xae):
    """StartRecord held by a dialog it opened: its outcome is not known — said so, and the part stays with the
    recording, so rec_stop stops Scope once it has started (0.8.16: 'NOT started', and Scope recorded unowned)."""
    from twincat_mcp import dialogs as D
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    st = {"dlg": False}
    dlg = D.Dialog(90, 1, "TwinCAT Scope", "Overwrite?", [(1, "Yes"), (2, "No")]).to_dict()
    monkeypatch.setattr(app, "dialogs_now", lambda: [dlg] if st["dlg"] else [])
    orig = fake_xae.StartRecord

    def start():
        st["dlg"] = True
        _t12.sleep(3.5)                                    # the shell waits on its dialog; the user answers it
        st["dlg"] = False
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        sc0 = payload(await s.call_tool("rec_status", {"id": r["id"]}))["scope"]
        await _anyio12.sleep(3.0)
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    assert r["scope"]["start_unknown"] and "NOT started" not in str(r["scope"]), r["scope"]
    assert sc0["state"] == "recording" and "BLOCKED BY DIALOG" in sc0["start_unknown"], sc0
    assert not fake_xae.recording and out["scope"]["finished_by"] == "rec_stop", (fake_xae.calls, out["scope"])
    assert out["scope"]["start_unknown"], out["scope"]


@pytest.mark.anyio
async def test_0817_a_stop_held_by_a_dialog_is_never_sent_again(monkeypatch, project_dir, fake_xae):
    """StopRecord held by a dialog it opened: whether Scope stopped is not known — it is not sent again (it may have
    run), and nothing claims Scope did or did not stop (0.8.16: 'Scope did not stop')."""
    from twincat_mcp import dialogs as D
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    st = {"dlg": False}
    dlg = D.Dialog(91, 1, "TwinCAT Scope", "Stop the record?", [(1, "Yes"), (2, "No")]).to_dict()
    monkeypatch.setattr(app, "dialogs_now", lambda: [dlg] if st["dlg"] else [])
    orig = fake_xae.StopRecord

    def stop():
        fake_xae.calls.append("stop-asked")
        st["dlg"] = True
        _t12.sleep(4.5)                                    # well past the 2 s a dialog needs to count as blocking
        st["dlg"] = False
        return orig()
    monkeypatch.setattr(fake_xae, "StopRecord", stop)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        await _anyio12.sleep(3.0)
        again = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        stt = payload(await s.call_tool("rec_status", {"id": r["id"]}))
    sc = out["scope"]
    assert sc["stop"]["outcome_unknown"] and "whether Scope stopped is not known" in sc["note"], sc
    assert "did not stop" not in sc["note"] and fake_xae.calls.count("stop-asked") == 1, (sc, fake_xae.calls)
    assert again["scope"] == sc and stt["scope"]["state"] == "stop_unknown", stt["scope"]


@pytest.mark.anyio
async def test_0817_scope_record_save_with_the_export_tool_names_its_own_way_out(monkeypatch, project_dir, fake_xae,
                                                                                 tmp_path):
    from twincat_mcp import scope as S
    install(monkeypatch)
    monkeypatch.setattr(S, "find_tool", lambda configured=None: Path("C:/TwinCAT/Functions/TE130X/TC3ScopeExportTool.exe"))
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    out = tmp_path / "home" / "recordings" / "manual.svdx"
    out.parent.mkdir(parents=True, exist_ok=True)
    (out.parent / "manual.csv").write_text("old\n", encoding="utf-8")
    async with client_for(srv) as s:
        r = await s.call_tool("scope_record", {"action": "save", "path": str(out)})
    t = text(r) if r.is_error else json.dumps(payload(r))
    assert "overwrite=true" not in t and "give scope_record another path" in t, t
    assert (out.parent / "manual.csv").read_text(encoding="utf-8") == "old\n"


@pytest.mark.anyio
async def test_0817_rec_stop_and_rec_delete_together_stop_scope_once(monkeypatch, project_dir, fake_xae):
    """rec_stop and rec_delete of one recording at the same time: one StopRecord; nothing saved for the deleted one."""
    from twincat_mcp import recorder as R
    install(monkeypatch)
    orig = R.Recorder.stop

    def slow_stop(self, rid, reason="requested"):
        st = orig(self, rid, reason)
        _t12.sleep(0.8)
        return st
    monkeypatch.setattr(R.Recorder, "stop", slow_stop)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": r["id"]}))
        outs: dict = {}

        async def stop():
            outs["stop"] = await s.call_tool("rec_stop", {"id": r["id"]})

        async def delete():
            outs["delete"] = await s.call_tool("rec_delete", {"id": r["id"], "confirm_token": tok["confirm_token"]})
        async with _anyio12.create_task_group() as tg:
            tg.start_soon(stop)
            await _anyio12.sleep(0.1)
            tg.start_soon(delete)
        await _anyio12.sleep(1.0)
    assert fake_xae.calls.count("stop") == 1 and not fake_xae.recording, fake_xae.calls
    part = app.scope_parts[r["id"]]
    assert part.state == "finished" and part.holds == 0, (part.state, part.holds)
    assert not outs["stop"].is_error and not outs["delete"].is_error, (text(outs["stop"]), text(outs["delete"]))
    assert payload(outs["stop"])["scope"] == payload(outs["delete"])["scope"], "one Scope part, one result"
    # rec_stop came first: its Scope part is saved and linked; rec_delete then removes the ADS recording and says the
    # Scope files stay (first review of 0.8.17)
    d = payload(outs["delete"])
    assert d["scope_files_kept"]["files"] == sorted(part.result["link"]["files"]), d
