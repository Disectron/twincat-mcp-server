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


@pytest.mark.anyio
async def test_workflows_and_prompts(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        prompts = (await s.list_prompts()).prompts
        assert {p.name for p in prompts} == {"debug_symbol", "fb_validation", "deploy_safely", "performance_audit", "onboard_project"}
        pr = await s.get_prompt("fb_validation", {"instance": "MAIN.fbFill", "criteria": "bDone within 3 s"})
        assert "MAIN.fbFill" in pr.messages[0].content.text and "bDone within 3 s" in pr.messages[0].content.text
        d = payload(await s.call_tool("wf_debug_symbol", {"symbol": "MAIN.fbFill.bDone", "watch_ms": 50}))
        assert d["live"]["value"] is False and d["resolution"]["resolved"]
        assert any(m["unit"] == "MAIN" for m in d["mentions"]) and d["code_excerpts"][0]["unit"] == "MAIN"
        assert "fbFill.bDone" in d["code_excerpts"][0]["text"]
        ov = payload(await s.call_tool("wf_project_overview", {}))
        assert [p["name"] for p in ov["programs"]] == ["MAIN"] and "FB_Fill" in ov["programs"][0]["calls"]
        assert any(fb["name"] == "FB_Valve" and fb["methods"] == ["Open"] for fb in ov["function_blocks"])
        io = payload(await s.call_tool("ads_io_read", {"area": "I", "offset": 0, "size": 1}))
        assert io["area"] == "I" and io["hex"] == "a5" and io["bits_msb_first"] == "10100101"
        sym = payload(await s.call_tool("ads_list_symbols", {"pattern": "MAIN.fbFill.*", "compact": True}))
        assert sym["symbols"][0].startswith("MAIN.fbFill.tSettle : TIME")
        prof = payload(await s.call_tool("wf_fb_profile", {"instance": "MAIN.stRecipe", "duration_s": 0.3}))
        assert prof["symbols_total"] == 3 and prof["task"]["task_name"] == "PlcTask"
        rc = payload(await s.call_tool("wf_release_check", {"run_build": False}))
        assert rc["verdict"].startswith("GO") or rc["verdict"] == "NO-GO"
        assert "source_vs_runtime" in rc["checks"] and rc["checks"]["tcunit"].get("skipped") or rc["checks"]["tcunit"]


@pytest.mark.anyio
async def test_safety_guard_hmi_export_report(monkeypatch, project_dir, tmp_path):
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="full", confirm_destructive=False)
    cfg.project.hmi_root = str(Path(__file__).parent / "fixtures" / "sample_hmi")
    srv = build_server(cfg)
    async with client_for(srv) as s:
        res = await s.call_tool("xae_tree_consume_xml", {"path": "TISC^Safety^Group1", "xml": "<x/>"})
        assert res.is_error and "TwinSAFE" in text(res)
        h = payload(await s.call_tool("hmi_scan", {}))
        assert h["bindings"] == 5
        v = payload(await s.call_tool("hmi_vs_runtime", {}))
        assert v["broken"] == 1 and v["broken_bindings"][0]["symbol"] == "MAIN.fbFill.rGhostValue"
        e = payload(await s.call_tool("ads_export", {"types": ["ST_Step"], "fmt": "csharp"}))
        assert "public struct ST_Step" in e["text"]
        e2 = payload(await s.call_tool("ads_export", {"prefix": "MAIN.arrSteps", "fmt": "csharp_symbols"}))
        assert "MAIN_arrSteps_2_eState" in e2["text"]
        d = payload(await s.call_tool("xae_dialogs", {}))
        assert d["pending"] == [] and d["live"] == []
        val = payload(await s.call_tool("proj_validate_xml", {}))
        assert val["files"] == 5
        rep = payload(await s.call_tool("tc_session_report", {"out_path": str(tmp_path / "r.md")}))
        assert Path(rep["file"]).exists() and "# twincat-mcp session report" in rep["markdown"]
        disc = await s.call_tool("ads_discover", {"timeout_s": 0.2})
        assert not disc.is_error  # no targets on this network, but the call must work


@pytest.mark.anyio
@pytest.mark.parametrize("boom", [
    AttributeError("function 'AdsPortOpenEx' not found"),          # wrong / stand-in TcAdsDll.dll
    OSError("[WinError 126] The specified module could not be found"),
])
async def test_tc_doctor_survives_router_failure(monkeypatch, project_dir, boom):
    """tc_doctor exists to diagnose a dead router; a raw pyads/ctypes error there must become a
    'router: FAIL …' field, never an unexpected-exception tool result (seen on Windows CI)."""
    import pyads
    install(monkeypatch)

    def dead_open_port(*a, **k):
        raise boom
    monkeypatch.setattr(pyads, "open_port", dead_open_port)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        res = await s.call_tool("tc_doctor", {})
        assert not res.is_error, text(res)
        doc = payload(res)
        assert str(doc["router"]).startswith("FAIL"), doc["router"]
        assert "router" in str(doc["router"]).lower()
        assert "local_ams_net_id" not in doc
