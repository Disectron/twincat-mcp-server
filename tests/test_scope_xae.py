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
