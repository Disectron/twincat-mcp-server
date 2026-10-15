"""End-to-end through a real MCP client session on in-memory streams."""
import json
from pathlib import Path
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from twincat_mcp.config import Config, validate
from twincat_mcp.server import build_server
from tests.fake_ads import install


@asynccontextmanager
async def client_for(server, elicitation_callback=None):
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        cr, cw = client_streams
        sr, sw = server_streams
        async with anyio.create_task_group() as tg:
            tg.start_soon(lambda: server._lowlevel_server.run(sr, sw, server._lowlevel_server.create_initialization_options(), raise_exceptions=False))
            async with ClientSession(cr, cw, elicitation_callback=elicitation_callback) as session:
                await session.initialize()
                yield session
            tg.cancel_scope.cancel()


def mk_cfg(project_dir, **safety):
    cfg = Config()
    cfg.project.root = str(project_dir)
    cfg.ads.ams_net_id = "5.1.2.3.1.1"
    for k, v in safety.items():
        setattr(cfg.safety, k, v)
    validate(cfg)
    return cfg


def payload(res: types.CallToolResult):
    if res.structured_content is not None:
        sc = res.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(res.content[0].text)


def text(res: types.CallToolResult):
    return "".join(c.text for c in res.content if isinstance(c, types.TextContent))


@pytest.mark.anyio
async def test_tools_listed_and_project_flow(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        tools = (await s.list_tools()).tools
        names = {t.name for t in tools}
        assert {"tc_doctor", "ads_read", "ads_write", "xae_build", "proj_resolve_symbol", "tcunit_run"} <= names
        ann = {t.name: t.annotations for t in tools}
        assert ann["ads_read"].read_only_hint is True
        assert ann["xae_activate_configuration"].destructive_hint is True
        r = payload(await s.call_tool("proj_scan", {}))
        assert r["units"] >= 8
        r = payload(await s.call_tool("proj_resolve_symbol", {"symbol": "MAIN.fbFill.fbValve.bOpen"}))
        assert r["resolved"]
        r = payload(await s.call_tool("tcunit_results", {"local_path": "TcUnit_xUnit_testresults.xml"}))
        assert r["summary"]["failures"] == 1


@pytest.mark.anyio
async def test_ads_read_and_guarded_write(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", write_allowlist=["MAIN.fbFill.*"]))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("ads_read", {"name": "MAIN.fbFill.tSettle"}))
        assert r["value"]["text"] == "T#1s500ms"
        r = payload(await s.call_tool("ads_read_many", {"names": ["MAIN.nCycle", "MAIN.stRecipe"]}))
        assert r["MAIN.nCycle"] == 42 and r["MAIN.stRecipe"]["sName"] == "Vanilla"
        # not in allowlist -> ToolError with the reason
        res = await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 1})
        assert res.is_error and "not in write_allowlist" in text(res)
        res = await s.call_tool("ads_write", {"name": "MAIN.fbFill.tSettle", "value": "T#3s"})
        assert not res.is_error and payload(res)["after"]["raw_ms"] == 3000
        # control op needs a token in guarded mode (no elicitation support on this client)
        res = await s.call_tool("ads_plc_state", {"action": "stop"})
        assert res.is_error and "tc_request_confirmation" in text(res)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ads_plc_state", "target": "stop"}))["confirm_token"]
        res = await s.call_tool("ads_plc_state", {"action": "stop", "confirm_token": tok})
        assert not res.is_error and payload(res)["after"]["ads_state_name"] == "STOP"
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 10}))
        assert [a["tool"] for a in audit][-3:] == ["ads_write", "tc_request_confirmation", "ads_plc_state"]


@pytest.mark.anyio
async def test_elicitation_confirms_destructive(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="full"))
    asked = []

    async def elicit(context, params):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"confirm": True})

    async with client_for(srv, elicitation_callback=elicit) as s:
        res = await s.call_tool("ads_system_mode", {"action": "config"})
        assert not res.is_error, text(res)
        assert asked and "ads_system_mode" in asked[0]

    async def decline(context, params):
        return types.ElicitResult(action="decline")

    srv2 = build_server(mk_cfg(project_dir, mode="full"))
    async with client_for(srv2, elicitation_callback=decline) as s:
        res = await s.call_tool("ads_system_mode", {"action": "config"})
        assert res.is_error and "declined" in text(res)


@pytest.mark.anyio
async def test_mode_ceiling_and_readonly(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="readonly", max_mode="guarded"))
    async with client_for(srv) as s:
        res = await s.call_tool("tc_set_mode", {"mode": "full"})
        assert res.is_error and "ceiling" in text(res)
        res = await s.call_tool("proj_set_code", {"name": "MAIN", "implementation": ";"})
        assert res.is_error and "readonly" in text(res)
        assert payload(await s.call_tool("tc_set_mode", {"mode": "guarded"}))["mode"] == "guarded"
        res = await s.call_tool("proj_set_code", {"name": "MAIN", "implementation": ";"})
        assert not res.is_error


@pytest.mark.anyio
async def test_xae_tools_report_platform_cleanly(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        res = await s.call_tool("xae_status", {})
        # not attached → plain status, no exception
        assert not res.is_error and payload(res)["attached"] is False
        res = await s.call_tool("xae_build", {})
        assert res.is_error and ("Windows" in text(res) or "XAE" in text(res))
        res = await s.call_tool("tc_doctor", {})
        d = payload(res)
        assert d["router"] == "ok" and d["local_ams_net_id"] == "192.168.0.10.1.1"


@pytest.mark.anyio
async def test_recorder_and_bench_tools(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", write_allowlist=["MAIN.n*"]))
    async with client_for(srv) as s:
        st = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle", "MAIN.sLabel"], "mode": "poll", "cycle_ms": 5}))
        rid = st["id"]
        import anyio as _anyio
        await _anyio.sleep(0.15)
        st2 = payload(await s.call_tool("rec_status", {"id": rid}))
        assert st2["events"] >= 4 and st2["active"]
        snap = payload(await s.call_tool("rec_snapshot", {"id": rid, "last_n": 3}))
        assert len(snap["rows"]) == 3
        out = payload(await s.call_tool("rec_stop", {"id": rid}))
        assert not out["active"] and out["stats"]["MAIN.nCycle"]["last"] == 42
        exp = payload(await s.call_tool("rec_export", {"id": rid, "fmt": "csv_long"}))
        assert exp["rows"] > 0
        pl = payload(await s.call_tool("rec_pulses", {"id": rid, "symbol": "MAIN.nCycle"}))
        assert pl["runs"] == 1
        # bench: expect works read-only; scenario writes gated per symbol
        e = payload(await s.call_tool("test_expect", {"checks": [{"symbol": "MAIN.nCycle", "op": "==", "value": 42}]}))
        assert e["pass"]
        res = await s.call_tool("test_scenario", {"spec": {"steps": [{"write": {"MAIN.bStart": True}}]}})
        assert res.is_error and "not in write_allowlist" in text(res)
        res = await s.call_tool("test_scenario", {"spec": {"steps": [{"write": {"MAIN.nCycle": 7}}, {"expect": [{"symbol": "MAIN.nCycle", "op": "==", "value": 7}]}]}})
        assert not res.is_error and payload(res)["pass"]
        d = payload(await s.call_tool("ads_task_stats", {"task_index": 1, "samples": 2, "interval_ms": 1}))
        assert d["task_name"] == "PlcTask"
        lint = payload(await s.call_tool("proj_lint", {"rules": ["unused"]}))
        assert lint["by_rule"]["unused"] >= 1
        iface = payload(await s.call_tool("proj_interface", {"name": "FB_Valve"}))
        assert iface["properties"][0]["name"] == "IsOpen"


@pytest.mark.anyio
async def test_token_bound_to_target_and_raw_write_denylist(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="full", write_denylist=["MAIN.bStart"]))
    async with client_for(srv) as s:
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ads_plc_state", "target": "reset"}))
        assert tok["scope"] == "5.1.2.3.1.1:851"
        # re-target (full mode: free) then try to spend the old token → refused
        await s.call_tool("ads_connect", {"ams_net_id": "5.9.9.9.1.1"})
        res = await s.call_tool("ads_plc_state", {"action": "reset", "confirm_token": tok["confirm_token"]})
        assert res.is_error and "issued while connected to 5.1.2.3.1.1:851" in text(res)
        # raw write overlapping a denylisted symbol is refused even in full mode
        info = payload(await s.call_tool("ads_symbol_info", {"name": "MAIN.bStart"}))
        ig, io = int(info["index_group"], 16), int(info["index_offset"], 16)
        res = await s.call_tool("ads_write_raw", {"index_group": ig, "index_offset": io, "hex_data": "01"})
        assert res.is_error and "denylist" in text(res)
        res = await s.call_tool("ads_write_raw", {"index_group": ig, "index_offset": io, "hex_data": "01", "port": 10000})
        assert res.is_error and "851-859" in text(res)


@pytest.mark.anyio
async def test_whole_project_variable_tools(monkeypatch, project_dir, tmp_path):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        d = payload(await s.call_tool("ads_dump", {"out_dir": str(tmp_path / "dumps")}))
        assert d["leaves"] >= 25 and d["datatype_table"] and Path(d["csv"]).exists()
        lv = payload(await s.call_tool("ads_leaves", {"name": "MAIN.arrSteps", "with_values": True}))
        assert lv["count"] == 8 and lv["leaves"][1]["value"]["text"] == "T#1s500ms" and lv["leaves"][3]["value"]["name"] == "Bulk"
        allm = payload(await s.call_tool("ads_leaves", {"name": "MAIN"}))
        assert allm["count"] == 25
        t = payload(await s.call_tool("ads_types", {"type_name": "ST_Step"}))
        assert t["members"][1]["name"] == "tDwell"
        r = payload(await s.call_tool("ads_read", {"name": "MAIN.arrSteps"}))
        assert r["value"][1]["nId"] == 2
        pv = payload(await s.call_tool("proj_variables", {}))
        assert pv["count"] > 20
        cmp_ = payload(await s.call_tool("proj_vs_runtime", {}))
        missing = {m["path"] for m in cmp_["missing_online"]}
        extra = {e["path"] for e in cmp_["extra_online"]}
        assert "MAIN.fbFill.fbValve.bOpen" in missing and "GVL_Machine.bEStop" in missing and "MAIN.fbFill.arrWeights[7]" in missing
        assert "MAIN.fbFill.arrWeights[2]" not in missing and "MAIN.nCycle" not in missing
        assert "main.slabel" in extra and "main.arrsteps[1].nid" in extra and "main.fbvalve2.bopen" in extra
        assert cmp_["matched"] >= 10
