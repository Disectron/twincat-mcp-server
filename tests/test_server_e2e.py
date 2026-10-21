"""End-to-end through a real MCP client session on in-memory streams."""
import asyncio
import json
from pathlib import Path
from contextlib import asynccontextmanager

import anyio
import pytest

from tests.conftest import register_sources
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
    """The tool's result. A tool error fails here with its own text — json.loads of an error message said only
    'Expecting value' (Windows CI of 88a221c: the reason infosys_get_page failed was not in the log)."""
    if res.is_error:
        raise AssertionError(f"the tool returned an error: {text(res)}")
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
        assert not res.is_error and payload(res)["read_back"]["raw_ms"] == 3000
        # control op needs a token in guarded mode (no elicitation support on this client)
        res = await s.call_tool("ads_plc_state", {"action": "stop"})
        assert res.is_error and "tc_request_confirmation" in text(res)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ads_plc_state", "target": "stop"}))["confirm_token"]
        res = await s.call_tool("ads_plc_state", {"action": "stop", "confirm_token": tok})
        assert not res.is_error and payload(res)["after"]["ads_state_name"] == "STOP"
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 10}))
        # refusals are evidence: the denied write and the token-less control attempt are logged as 'refused'
        assert [(a["tool"], a["outcome"]) for a in audit][-5:] == [
            ("ads_write", "refused"), ("ads_write", "ok"), ("ads_plc_state", "refused"),
            ("tc_request_confirmation", "issued"), ("ads_plc_state", "ok")]
        assert "not in write_allowlist" in str(audit[-5]["detail"])


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
        # escalation needs a confirmation first (readonly is the start mode here)
        res = await s.call_tool("tc_set_mode", {"mode": "full"})
        assert res.is_error and "tc_request_confirmation" in text(res)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "tc_set_mode", "target": "full"}))["confirm_token"]
        res = await s.call_tool("tc_set_mode", {"mode": "full", "confirm_token": tok})
        assert res.is_error and "ceiling" in text(res), "confirmed, but still above max_mode"
        res = await s.call_tool("proj_set_code", {"name": "MAIN", "implementation": ";"})
        assert res.is_error and "readonly" in text(res)
        res = await s.call_tool("tc_set_mode", {"mode": "guarded"})
        assert res.is_error, "readonly -> guarded is an escalation too"
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "tc_set_mode", "target": "guarded"}))["confirm_token"]
        r = payload(await s.call_tool("tc_set_mode", {"mode": "guarded", "confirm_token": tok}))
        assert r["mode"] == "guarded" and r["escalated"]
        res = await s.call_tool("proj_set_code", {"name": "MAIN", "implementation": ";"})
        assert not res.is_error
        # lowering is free
        r = payload(await s.call_tool("tc_set_mode", {"mode": "readonly"}))
        assert r["mode"] == "readonly" and not r["escalated"]


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
        d = payload(await s.call_tool("ads_dump", {"out_dir": str(project_dir / "dumps")}))   # R4: inside a known folder
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
        extra_l = {e.lower() for e in extra}
        assert "main.slabel" in extra_l and "main.fbvalve2.bopen" in extra_l   # PLC spelling kept
        # array elements are collapsed into one line per member with a count (#57: ~70 KB of aAlarms[1..32].* before)
        steps = next(e for e in cmp_["extra_online"] if e["path"].lower() == "main.arrsteps[*].nid")
        assert steps["count"] == 2 and steps["example"] == "MAIN.arrSteps[1].nId"
        assert cmp_["matched"] >= 10
        # #20: the PLC's / the source's spelling, never lower-cased
        assert "MAIN.sLabel" in extra and "MAIN.fbValve2.bOpen" in extra and cmp_["roots"] == ["GVL_Machine", "MAIN"]
        assert cmp_["complete"] is True and "not_judged" not in cmp_
        # D23: max_items=5 cut both lists; the cut part was reported as missing_online / extra_online
        # (which 5 source items come first depends on the file order of the OS; any verdict given must be a true one)
        small = payload(await s.call_tool("proj_vs_runtime", {"max_items": 5}))
        assert small["complete"] is False and small["extra_online"] == [] and "max_items=5" in small["not_judged"]["reason"]
        assert {m["path"] for m in small["missing_online"]} <= missing, small["missing_online"]
        assert small["not_judged"]["missing_candidates"] + small["not_judged"]["extra_candidates"] >= 1


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
        assert h["bindings"] == 11 and h["files"] == 3  # 5 TE2000 (.view/.json) + 6 TF1800 (.TcVIS)
        tw = payload(await s.call_tool("hmi_bindings", {"pattern": "*", "kind": "tcvis_write"}))
        assert {b["plc_symbol"] for b in tw["bindings"]} == {"MAIN.bStart", "MAIN.nCycle"}
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
        rep = payload(await s.call_tool("tc_session_report", {"out_path": str(project_dir / "r.md"), "inline": True}))
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


@pytest.mark.anyio
async def test_tc_allow_write_and_reload_config(monkeypatch, project_dir, tmp_path):
    """In-session allow-list change (confirmed, audited, session-only) and a config reload that applies
    the list-type settings without a restart but only reports target/mode changes."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded")               # empty allow list: nothing writable
    toml = tmp_path / "twincat_mcp.toml"
    toml.write_text('[safety]\nmode = "guarded"\nwrite_allowlist = ["MAIN.nCycle"]\n[ads]\nams_net_id = "9.9.9.9.1.1"\n', encoding="utf-8")
    cfg.config_file = str(toml)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        res = await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 5})
        assert res.is_error and "write_allowlist is empty" in text(res)
        # control-class: no token, no elicitation on this client → refused with the token hint
        res = await s.call_tool("tc_allow_write", {"pattern": "MAIN.nCycle"})
        assert res.is_error and "tc_request_confirmation" in text(res)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "tc_allow_write", "target": "MAIN.nCycle"}))["confirm_token"]
        r = payload(await s.call_tool("tc_allow_write", {"pattern": "MAIN.nCycle", "confirm_token": tok}))
        assert r["changed"] and r["session_only"] and r["write_allowlist"] == ["MAIN.nCycle"]
        res = await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 5})
        assert not res.is_error
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "tc_allow_write", "target": "-MAIN.nCycle"}))["confirm_token"]
        r = payload(await s.call_tool("tc_allow_write", {"pattern": "MAIN.nCycle", "remove": True, "confirm_token": tok}))
        assert r["write_allowlist"] == []
        res = await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 6})
        assert res.is_error
        # reload: the allow list from the file is applied; the target change is reported, not applied
        r = payload(await s.call_tool("tc_reload_config", {}))
        assert r["applied"]["safety.write_allowlist"]["to"] == ["MAIN.nCycle"]
        assert "ads.ams_net_id" in r["needs_restart"]
        res = await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 7})
        assert not res.is_error
        # N6: a session addition survives a reload
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "tc_allow_write", "target": "+MAIN.bStart"}))["confirm_token"]
        assert payload(await s.call_tool("tc_allow_write", {"pattern": "MAIN.bStart", "confirm_token": tok}))["changed"]
        r = payload(await s.call_tool("tc_reload_config", {}))
        assert r["kept_session_allow"] == ["MAIN.bStart"] and "MAIN.bStart" in r["write_allowlist"]
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 20}))
        assert [(a["tool"], a["outcome"]) for a in audit if a["tool"] in ("tc_allow_write", "tc_reload_config")] == [
            ("tc_allow_write", "refused"), ("tc_allow_write", "ok"), ("tc_allow_write", "ok"), ("tc_reload_config", "ok"),
            ("tc_allow_write", "ok"), ("tc_reload_config", "ok")]


@pytest.mark.anyio
async def test_tc_doctor_reports_placeholder_config(monkeypatch, project_dir):
    install(monkeypatch)
    cfg = Config()
    cfg.project.root = str(project_dir)
    cfg.ads.ams_net_id = "<CX_NETID>"
    cfg.xae.solution_path = r"C:\path\to\YourSolution.sln"
    validate(cfg)
    assert cfg.ads.ams_net_id is None and cfg.xae.solution_path is None
    srv = build_server(cfg)
    async with client_for(srv) as s:
        doc = payload(await s.call_tool("tc_doctor", {}))
        assert any("placeholder" in w for w in doc["config_FIX_THESE"])
        assert doc["ads_target"]["ams_net_id"] == "(local)"


@pytest.mark.anyio
async def test_destructive_xae_tools_refuse_by_guard_before_touching_the_shell(monkeypatch, project_dir):
    """guarded, no token, no elicitation: the refusal must be the guard's even when no XAE shell can be reached
    (on CI there is none; on the engineering PC the shell may be closed). The attach problem is appended, not
    substituted — otherwise the acceptance guard proofs read 'no shell' instead of 'needs confirmation'."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded")
    srv = build_server(cfg)
    async with client_for(srv) as s:
        for tool in ("xae_activate_configuration", "xae_restart_twincat", "xae_deploy"):
            res = await s.call_tool(tool, {})
            assert res.is_error and "confirm_token" in text(res), (tool, text(res))
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 10}))
        assert [a["outcome"] for a in audit if a["tool"].startswith("xae_")] == ["refused"] * 3


@pytest.mark.anyio
async def test_write_refusals_round2(monkeypatch, project_dir):
    """Field report round 2 (#51–#54, #61–#64): every one of these was accepted (or crashed) on the engineering PC."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="full", confirm_destructive=False))
    async with client_for(srv) as s:
        refused = {
            "PRG_Test.ipModule": (0, "INTERFACE"),                       # #52 wild pointer
            "PRG_Test.pData": (0, "POINTER"),
            "PRG_Test.abStateLit": ([True, False], "has 8 elements; got 2"),   # #51 silent zeroing
            "GVL_Machine.MAX_MODULES": (99, "CONSTANT"),                  # #53 no READONLY flag on the symbol
            "MAIN.sLabel": ("x" * 11, "holds at most 10"),                # #63 raw ADSError 1797
            "PRG_Test.eCmd": ("Nope", "valid: Idle, Bulk, Fine, Done"),
        }
        for name, (val, why) in refused.items():
            r = await s.call_tool("ads_write", {"name": name, "value": val})
            assert r.is_error and why in text(r), (name, text(r))
        ok = payload(await s.call_tool("ads_write", {"name": "PRG_Test.eCmd", "value": "E_FillState.Fine"}))   # #61
        assert ok["read_back"] == ok["written"] == {"value": 2, "name": "Fine"}      # ads_read's shape (N50 / D5)
        r = payload(await s.call_tool("ads_read", {"name": "PRG_Test.ipModule"}))                              # #64
        assert r["value"] == "0x0000000000000000" and "INTERFACE" in r["note"]           # N51: unassigned = address 0
        # #54: one bad item → nothing written, every problem named
        r = await s.call_tool("ads_write_many", {"values": {"MAIN.nCycle": 1, "GVL_Machine.MAX_MODULES": 3, "MAIN.sLabel": "y" * 20}})
        assert r.is_error and "nothing was written" in text(r) and "MAX_MODULES" in text(r) and "sLabel" in text(r)
        assert payload(await s.call_tool("ads_read", {"name": "MAIN.nCycle"}))["value"] == 42
        r = payload(await s.call_tool("ads_write_many", {"values": {"MAIN.nCycle": 7, "MAIN.bStart": False}}))
        assert r["complete"] and set(r["applied"]) == {"MAIN.nCycle", "MAIN.bStart"}
        # P6: one ADS sum-write, values verified by read-back
        assert r["method"].startswith("sum-write") and r["results"]["MAIN.nCycle"]["read_back"] == 7
        assert r["results"]["MAIN.bStart"]["read_back"] is False
        t = payload(await s.call_tool("test_expect", {"checks": [{"symbol": "PRG_Test.eCmd", "op": "==", "value": "Fine"}]}))   # #62
        assert t["pass"]


def test_pointer_format():
    from twincat_mcp import types_tc as T
    assert T.fmt_pointer(0) == "0x0000000000000000" and T.fmt_pointer(0, 4) == "0x00000000"     # N51: NULL is the address 0
    assert T.fmt_pointer(None) is None and T.fmt_pointer("x") is None             # no integer: no address
    assert T.fmt_pointer(-114343974404328) == "0xFFFF98002F3C4018"[:2] + format((-114343974404328) & (2**64 - 1), "016X")
    assert T.fmt_pointer(0x1234, 4) == "0x00001234"


@pytest.mark.anyio
async def test_ipc_eventlog_token_provider_cleanup(monkeypatch, project_dir):
    """#55: the token is checked against ipc_eventlog/<log>, not the inner process start; #56: a provider that is
    not registered is named as such with the registered ones listed, and the temp file is deleted."""
    install(monkeypatch)
    from twincat_mcp import ads_files as F
    fs: dict[str, str] = {}
    deleted: list[str] = []
    started: list[str] = []
    # N110: the real resolve_prefix_dir runs (a stand-in that ignored its argument hid 'boot:' being unknown to it);
    # only the target's file system is faked
    boot = r"C:\ProgramData\Beckhoff\TwinCAT\3.1\Boot"
    monkeypatch.setattr(F.SystemServiceFiles, "_ffind_raw", lambda self, pattern, mx: [{"name": "CurrentConfig.xml",
                        "size": 1, "is_dir": False}] if pattern == boot + "\\*" else [])

    def start(self, path, directory="", command_line=""):
        started.append(command_line)
        out = command_line.split('> "', 1)[1].split('"', 1)[0]
        fs[out] = "No events were found that match the specified selection criteria.\r\n===TCMCP_PROVIDERS===\r\nTwinCAT3 Scope Server\r\n"
        return {"started": path}
    monkeypatch.setattr(F.SystemServiceFiles, "start_process", start)
    monkeypatch.setattr(F.SystemServiceFiles, "wait_for_file", lambda self, p, t, poll=2.0: {"exists": p in fs})
    monkeypatch.setattr(F.SystemServiceFiles, "read_file", lambda self, p, mx=None, as_text=True, enc="utf-8": {"text": fs[p], "bytes": len(fs[p])})

    locked = [0]

    def delete(self, p):
        deleted.append(p)
        if locked[0]:
            locked[0] -= 1
            raise F.AdsError(f"FDELETE {p!r}: ADSError 1804 (sharing violation)")
        fs.pop(p, None)
        return {"deleted": p}
    monkeypatch.setattr(F.SystemServiceFiles, "delete_file", delete)
    monkeypatch.setattr(F.SystemServiceFiles, "exists", lambda self, p: p in fs)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    tmp = r"C:\ProgramData\Beckhoff\TwinCAT\3.1\Boot\tcmcp_eventlog.txt"
    async with client_for(srv) as s:
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ipc_eventlog", "target": "Application"}))["confirm_token"]
        r = payload(await s.call_tool("ipc_eventlog", {"confirm_token": tok}))
        assert r["entries_text"] == "" and r["events"] == 0
        assert "not registered" in r["note"] and r["providers_on_target"] == ["TwinCAT3 Scope Server"]
        assert deleted == [tmp] and not fs and r["temp_file"] == {"path": tmp, "deleted": True}
        bad = await s.call_tool("ipc_eventlog", {"provider": "x' & del C:\\*", "confirm_token": "t"})
        assert bad.is_error and "only letters" in text(bad) and len(started) == 1
        # #56: cmd.exe still holds the file → the delete is retried and checked, never claimed
        locked[0], deleted[:] = 1, []
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ipc_eventlog", "target": "Application"}))["confirm_token"]
        r = payload(await s.call_tool("ipc_eventlog", {"confirm_token": tok}))
        assert deleted == [tmp, tmp] and not fs and r["temp_file"]["deleted"] is True
        locked[0], deleted[:] = 99, []
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ipc_eventlog", "target": "Application"}))["confirm_token"]
        r = payload(await s.call_tool("ipc_eventlog", {"confirm_token": tok}))
        assert r["temp_file"]["deleted"] is False and "sharing violation" in r["temp_file"]["error"] and len(deleted) == 4
        fs.clear(); locked[0] = 0

        # #56: no guessed boot folder (C:\TwinCAT\3.1\Boot is a stale copy on 4026) → nothing is started
        def no_boot(self, p):
            raise F.AdsError("FFILEFIND: no boot folder found")
        monkeypatch.setattr(F.SystemServiceFiles, "resolve_prefix_dir", no_boot)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ipc_eventlog", "target": "Application"}))["confirm_token"]
        r = await s.call_tool("ipc_eventlog", {"confirm_token": tok})
        assert r.is_error and "boot folder" in text(r) and len(started) == 3


@pytest.mark.anyio
async def test_vs_runtime_array_templates(monkeypatch, project_dir):
    """#57: arrays with constant bounds are declared as x[*] in source; their online elements must match, not be 'extra'."""
    install(monkeypatch)
    main = project_dir / "Plc" / "POUs" / "MAIN.TcPOU"
    main.write_text(main.read_text().replace("\tnCycle      : UDINT;", "\tnCycle      : UDINT;\n\tarrSteps    : ARRAY[1..GVL_Machine.MAX_MODULES] OF ST_Step;"))
    srv = build_server(mk_cfg(project_dir, mode="readonly"))
    async with client_for(srv) as s:
        v = payload(await s.call_tool("proj_variables", {"scope": "MAIN"}))
        assert any("[*]" in it["path"] and it["path"].startswith("MAIN.arrSteps") for it in v["items"]), [it["path"] for it in v["items"]]
        cmp_ = payload(await s.call_tool("proj_vs_runtime", {}))
        assert not any(e["path"].lower().startswith("main.arrsteps") for e in cmp_["extra_online"])
        assert cmp_["array_elements_matched"] >= 8 and cmp_["array_templates_matched"] >= 1
        assert not any(m["path"].lower().startswith("main.arrsteps") for m in cmp_["missing_online"])


@pytest.mark.anyio
async def test_elicitation_wording_timing_and_policy(monkeypatch, project_dir):
    """#73: the prompt names kind, target, machine, mode and effect; the audit records how long it was open and
    flags sub-second answers; allow_elicitation=false forces the token path even on a prompting client."""
    install(monkeypatch)
    asked = []

    async def elicit(context, params):
        asked.append(params.message)
        return types.ElicitResult(action="accept", content={"confirm": True})
    srv = build_server(mk_cfg(project_dir, mode="full"))
    async with client_for(srv, elicitation_callback=elicit) as s:
        assert not (await s.call_tool("ads_system_mode", {"action": "config"})).is_error
        msg = asked[0]
        assert msg.startswith("DESTRUCTIVE: ads_system_mode") or msg.startswith("MACHINE CONTROL: ads_system_mode"), msg
        assert "Machine: 5.1.2.3.1.1" in msg and "Effect: switches TwinCAT between RUN and CONFIG" in msg
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 5}))
        conf = [a for a in audit if a["outcome"] == "human-confirmed-elicitation"][-1]
        assert "prompt_open_s" in conf["detail"] and "fast_answer" in conf["detail"]
    asked.clear()
    srv = build_server(mk_cfg(project_dir, mode="full", allow_elicitation=False))
    async with client_for(srv, elicitation_callback=elicit) as s:
        res = await s.call_tool("ads_system_mode", {"action": "config"})
        assert res.is_error and "allow_elicitation = false" in text(res) and asked == []
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "ads_system_mode", "target": "config"}))["confirm_token"]
        assert not (await s.call_tool("ads_system_mode", {"action": "config", "confirm_token": tok})).is_error


@pytest.mark.anyio
async def test_edit_requires_config_mode(monkeypatch, project_dir):
    """#74: with safety.edit_requires_config_mode, code edits are refused while TwinCAT is in RUN (and when the
    state cannot be read); in CONFIG they go through."""
    install(monkeypatch)
    from twincat_mcp import ads_backend as AB
    state = {"mode": "RUN"}
    monkeypatch.setattr(AB.AdsBackend, "system_state", lambda self: {"twincat_mode": state["mode"]})
    srv = build_server(mk_cfg(project_dir, mode="full", edit_requires_config_mode=True))
    async with client_for(srv) as s:
        code = "nCycle := nCycle + 1;"
        res = await s.call_tool("proj_set_code", {"name": "MAIN", "implementation": code})
        assert res.is_error and "is in RUN — switch to CONFIG first" in text(res), text(res)
        state["mode"] = "CONFIG"
        res = await s.call_tool("proj_set_code", {"name": "MAIN", "implementation": code})
        assert not res.is_error, text(res)
        # reads are never gated
        assert not (await s.call_tool("proj_get_code", {"name": "MAIN"})).is_error


@pytest.mark.anyio
async def test_watch_and_wait_use_notifications_and_let_writes_through(monkeypatch, project_dir):
    """P4/P5: ads_wait_until returns when the runtime reports the condition (notification), ads_watch records the
    changes a concurrent write makes — neither blocks the write."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="full", confirm_destructive=False))
    async with client_for(srv) as s:
        res: dict[str, Any] = {}

        async def wait():
            res["wait"] = payload(await s.call_tool("ads_wait_until", {"name": "MAIN.nCycle", "op": "==", "value": 7, "timeout_ms": 5000}))

        async def watch():
            res["watch"] = payload(await s.call_tool("ads_watch", {"names": ["MAIN.nCycle"], "duration_ms": 3000}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(wait)
            tg.start_soon(watch)
            # the watch's window opens once its notifications are registered (the first rec start scans the project):
            # on a slow Windows runner that took longer than the 0.5 s this test waited, and the write landed before it
            await anyio.sleep(1.5)
            w = await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 7})
            assert not w.is_error, text(w)
        assert res["wait"]["met"] and res["wait"]["method"] == "ADS change notification" and res["wait"]["elapsed_ms"] < 4000
        assert res["watch"]["method"].startswith("ADS notifications") and any(r["value"] == 7 for r in res["watch"]["rows"])
        already = payload(await s.call_tool("ads_wait_until", {"name": "MAIN.nCycle", "op": ">=", "value": 7, "timeout_ms": 2000}))
        assert already["met"] and already["method"].startswith("read")
        # the temporary recordings are cleaned up — none left on disk either, and never sealed (D7)
        lst = payload(await s.call_tool("rec_list", {}))
        assert lst["session"] == [] and not any(r.get("name") == "ads_wait_until" for r in lst.get("on_disk", []))
        assert "cleanup_warning" not in res["wait"] and "cleanup_warning" not in already


@pytest.mark.anyio
async def test_tool_groups_limit_the_tool_list(monkeypatch, project_dir):
    """P3: server.tool_groups exposes only the named groups (tc_* always)."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="readonly")
    cfg.server.tool_groups = ["ads", "rec", "nonsense"]
    srv = build_server(cfg)
    async with client_for(srv) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert {n.split("_", 1)[0] for n in names} == {"tc", "ads", "rec"}
        doc = payload(await s.call_tool("tc_doctor", {}))
        assert any("nonsense" in w for w in doc["config_FIX_THESE"]) and doc["summary"].startswith("PROBLEMS")


@pytest.mark.anyio
async def test_rec_capture_one_call(monkeypatch, project_dir):
    """P19: record → wait → stop → stats + verification in one call."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_capture", {"symbols": ["MAIN.nCycle"], "seconds": 0.3, "mode": "change"}))
        assert r["id"] and r["verify"]["integrity"] == "intact" and "MAIN.nCycle" in r["stats"] and Path(r["file"]).exists()


def test_project_library_books(project_dir):
    """K1: the Beckhoff libraries the .plcproj references become Infosys books to index (Tc2_Smtp → tcplclib_tc2_smtp)."""
    from twincat_mcp.server import App
    pp = project_dir / "Plc" / "Plc.plcproj"
    pp.write_text(pp.read_text().replace(
        '<PlaceholderReference Include="Tc2_Standard">',
        '<PlaceholderReference Include="Tc2_Smtp"><DefaultResolution>Tc2_Smtp, *</DefaultResolution></PlaceholderReference>'
        '<PlaceholderReference Include="ThirdParty_Lib"></PlaceholderReference>'
        '<PlaceholderReference Include="Tc2_Standard">'))
    cfg = mk_cfg(project_dir)
    app = App(cfg)
    assert app.project_library_books() == ["tcplclib_tc2_smtp", "tcplclib_tc2_standard"]


@pytest.mark.anyio
async def test_source_checks_on_every_write_path(monkeypatch, project_dir):
    """Decision 2026-10-17: the CONSTANT check runs on every write path (not only ads_write); without a project
    source, guarded mode refuses writes and full mode allows them."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="full", confirm_destructive=False))
    async with client_for(srv) as s:
        sc = payload(await s.call_tool("test_scenario", {"spec": {"name": "c", "steps": [{"write": {"GVL_Machine.MAX_MODULES": 99}}]}}))
        assert not sc["pass"] and "CONSTANT" in json.dumps(sc)
    cfg = Config()
    cfg.ads.ams_net_id = "5.1.2.3.1.1"
    cfg.safety.mode = "guarded"
    cfg.safety.write_allowlist = ["MAIN.*"]
    validate(cfg)
    async with client_for(build_server(cfg)) as s:
        r = await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 1})
        assert r.is_error and "no project source is loaded" in text(r)
    cfg.safety.mode = "full"
    async with client_for(build_server(cfg)) as s:
        assert not (await s.call_tool("ads_write", {"name": "MAIN.nCycle", "value": 1})).is_error


@pytest.mark.anyio
async def test_rec_delete_needs_confirmation_and_keeps_a_fingerprint(monkeypatch, project_dir):
    """R11: deleting a recording is destructive-class; the audit keeps its SHA-256."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    async with client_for(srv) as s:
        rid = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change"}))["id"]
        await s.call_tool("rec_stop", {"id": rid})
        r = await s.call_tool("rec_delete", {"id": rid})
        assert r.is_error and "tc_request_confirmation" in text(r)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": rid}))["confirm_token"]
        d = payload(await s.call_tool("rec_delete", {"id": rid, "confirm_token": tok}))
        assert d["removed"] and len(d["deleted_file_sha256"]) == 64
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 10}))
        last = [a for a in audit if a["tool"] == "rec_delete"]
        assert [a["outcome"] for a in last] == ["refused", "ok"] and last[-1]["args"]["sha256"] == d["deleted_file_sha256"]


@pytest.mark.anyio
async def test_interfaces_known_from_source(monkeypatch, project_dir):
    """Retest #52/#64: the runtime's datatype table did not mark I_Module recognisably; the project source does."""
    install(monkeypatch)
    (project_dir / "Plc" / "POUs" / "I_Thing.TcIO").write_text(
        '<?xml version="1.0" encoding="utf-8"?><TcPlcObject Version="1.1.0.1"><Itf Name="I_Thing" Id="{00000000-0000-0000-0003-000000000001}">'
        '<Declaration><![CDATA[INTERFACE I_Thing\n]]></Declaration></Itf></TcPlcObject>')
    register_sources(project_dir / "Plc" / "Plc.plcproj")
    srv = build_server(mk_cfg(project_dir, mode="full", confirm_destructive=False))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("ads_read", {"name": "PRG_Test.ipThing"}))
        assert r["value"] == "0x" + format(-114343974404328 & (2**64 - 1), "016X") and "INTERFACE" in r["note"]
        a = payload(await s.call_tool("ads_read", {"name": "PRG_Test.aThings"}))
        assert a["value"][0] == "0x0000000000000000" and a["value"][1].startswith("0xFFFF")
        for name, val in (("PRG_Test.ipThing", 12345), ("PRG_Test.aThings", [0, 0])):
            w = await s.call_tool("ads_write", {"name": name, "value": val})
            assert w.is_error and "INTERFACE (declared in the project source)" in text(w), text(w)


@pytest.mark.anyio
async def test_debug_symbol_writers_respect_scope(monkeypatch, project_dir):
    """N3: FB_Other's own `nCycle := 0` is not a writer of MAIN.nCycle."""
    install(monkeypatch)
    (project_dir / "Plc" / "POUs" / "FB_Other.TcPOU").write_text(
        '<?xml version="1.0" encoding="utf-8"?><TcPlcObject Version="1.1.0.1"><POU Name="FB_Other" Id="{00000000-0000-0000-0000-000000000077}">'
        '<Declaration><![CDATA[FUNCTION_BLOCK FB_Other\nVAR\n\tnCycle : UDINT;\nEND_VAR\n]]></Declaration>'
        '<Implementation><ST><![CDATA[nCycle := 0;]]></ST></Implementation></POU></TcPlcObject>')
    register_sources(project_dir / "Plc" / "Plc.plcproj")
    srv = build_server(mk_cfg(project_dir, mode="readonly"))
    async with client_for(srv) as s:
        d = payload(await s.call_tool("wf_debug_symbol", {"symbol": "MAIN.nCycle", "watch_ms": 0}))
        units = {w["unit"] for w in d["writers"]}
        assert "MAIN" in units and "FB_Other" not in units, d["writers"]


@pytest.mark.anyio
async def test_review_0812_a_bare_name_of_a_global_variable_is_in_scope_everywhere(monkeypatch, project_dir):
    """Review 0.8.12: a variable of a GVL without {attribute 'qualified_only'} is reached by its bare name from every
    unit that has no variable of that name — wf_debug_symbol dropped those writers and mentions (N111 had narrowed
    a bare name to the declaring POU); an index expression in the qualifier (aFill[i + 1].x) is a qualifier."""
    install(monkeypatch)
    plc = project_dir / "Plc"
    (plc / "GVLs" / "GVL_Free.TcGVL").write_text(
        '<?xml version="1.0" encoding="utf-8"?><TcPlcObject Version="1.1.0.1"><GVL Name="GVL_Free" '
        'Id="{00000000-0000-0000-0000-000000000081}"><Declaration><![CDATA[VAR_GLOBAL\n\tnShared : INT;\nEND_VAR\n]]>'
        '</Declaration></GVL></TcPlcObject>')
    for name, decl, impl in (("PRG_User", "PROGRAM PRG_User\nVAR\nEND_VAR\n", "nShared := nShared + 1;\nIF nShared > 5 THEN nShared := 0; END_IF"),
                             ("FB_Own", "FUNCTION_BLOCK FB_Own\nVAR\n\tnShared : INT;\nEND_VAR\n", "nShared := 3;")):
        (plc / "POUs" / f"{name}.TcPOU").write_text(
            '<?xml version="1.0" encoding="utf-8"?><TcPlcObject Version="1.1.0.1"><POU Name="' + name + '" '
            'Id="{00000000-0000-0000-0000-0000000000' + str(82 + len(name)) + '}"><Declaration><![CDATA[' + decl + ']]>'
            '</Declaration><Implementation><ST><![CDATA[' + impl + ']]></ST></Implementation></POU></TcPlcObject>')
    register_sources(plc / "Plc.plcproj")
    srv = build_server(mk_cfg(project_dir, mode="readonly"))
    async with client_for(srv) as s:
        d = payload(await s.call_tool("wf_debug_symbol", {"symbol": "GVL_Free.nShared", "watch_ms": 0}))
        assert {w["unit"] for w in d["writers"]} == {"PRG_User"}, d["writers"]
        assert {m["unit"] for m in d["mentions"]} == {"PRG_User"}, d["mentions"]


def test_env_override_of_toml_is_reported(tmp_path, monkeypatch):
    """N10: mode = "full" in the TOML silently lost to TCMCP_MODE from the plugin settings."""
    from twincat_mcp.config import load
    toml = tmp_path / "twincat_mcp.toml"
    toml.write_text('[safety]\nmode = "full"\n', encoding="utf-8")
    monkeypatch.setenv("TCMCP_MODE", "guarded")
    cfg = load(str(toml))
    assert cfg.safety.mode == "guarded"
    assert cfg.overrides and "TCMCP_MODE says 'guarded'" in cfg.overrides[0]["note"] and "says 'full'" in cfg.overrides[0]["note"]
    monkeypatch.delenv("TCMCP_MODE")
    assert load(str(toml)).overrides == []


@pytest.mark.anyio
async def test_d4_top_level_enum_reads_carry_the_name(monkeypatch, project_dir):
    """D4: a top-level enum read the same way as an enum inside a struct: {value, name}."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    async with client_for(srv) as s:
        r = payload(await s.call_tool("ads_read", {"name": "MAIN.fbFill.eState"}))
        assert r["value"] == {"value": 1, "name": "Bulk"}, r
        m = payload(await s.call_tool("ads_read_many", {"names": ["MAIN.fbFill.eState", "MAIN.nCycle"]}))
        assert m["MAIN.fbFill.eState"] == {"value": 1, "name": "Bulk"} and m["MAIN.nCycle"] == 42


@pytest.mark.anyio
async def test_d8_published_schemas_declare_every_parameter(monkeypatch, project_dir):
    """D8: parameters the tools accept are in the schemas the client sees (engine / also_scope / scope, …)."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        tools = {t.name: t for t in (await s.list_tools()).tools}
        props = lambda n: set((tools[n].input_schema or {}).get("properties", {}))    # noqa: E731
        assert {"engine", "also_scope", "scope", "edge_triggers", "cycle_counter"} <= props("rec_start")
        assert {"engine", "counter"} <= props("rec_capture")
        assert "confirm_token" in props("tc_set_mode") and "fill" in props("rec_read")


@pytest.mark.anyio
async def test_d24_ads6_hint_names_the_port_that_was_asked(monkeypatch, project_dir):
    """D24: ads_status(port=852) on a target without a second PLC explained the error for port 851."""
    import pyads
    from tests.fake_ads import FakeConnection
    from twincat_mcp.server import _call_port
    install(monkeypatch)
    orig = FakeConnection.read_state

    def read_state(self):
        if self.ams_net_port == 852:
            raise pyads.ADSError(6)
        return orig(self)
    monkeypatch.setattr(FakeConnection, "read_state", read_state)

    def f(name, port=None): ...
    assert _call_port(f, ("x", 852), {}) == 852 and _call_port(f, ("x",), {"port": 853}) == 853
    assert _call_port(f, ("x",), {}) is None and _call_port(len, ([],), {}) is None
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        r = await s.call_tool("ads_status", {"port": 852})
        assert r.is_error
        msg = text(r)
        assert "ADSError 6" in msg and "851 is the first" in msg, msg


@pytest.mark.anyio
async def test_n4_restart_is_verified_only_after_the_target_left_run(monkeypatch, project_dir):
    """#4: the first poll after StartRestartTwinCAT read the old runtime's RUN → 'restarted: true, duration_s: 0'
    while the target then fell back to CONFIG (no license)."""
    import time as _time
    from twincat_mcp import dialogs as _dlg
    from twincat_mcp.server import App, ToolError
    app = App(mk_cfg(project_dir))
    clock = [1000.0]
    monkeypatch.setattr(_time, "time", lambda: clock[0])

    async def fake_sleep(d):
        clock[0] += d
    monkeypatch.setattr("twincat_mcp.server.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(_dlg, "list_dialogs", lambda pid: [])

    def script(*modes):
        seq = list(modes)

        async def ads_call(fn, *a, **kw):
            m = seq.pop(0) if len(seq) > 1 else seq[0]
            if m == "down":
                raise ToolError("ADS error: target port not found")
            return {"twincat_mode": m}
        monkeypatch.setattr(app, "ads_call", ads_call)

    script("RUN", "RUN", "CONFIG")                                   # license fallback
    v = await app.verify_target_after("xae_restart_twincat", expect_run=True, require_transition=True, timeout_s=30)
    assert not v["verified"] and v["transition_seen"] and v["target_mode"] == "CONFIG" and v["modes_seen"] == ["RUN", "CONFIG"]
    from twincat_mcp.server import _restart_timing
    t = _restart_timing(0.0, dict(v))                                # N5 (review): never seen running again —
    assert t["duration_s"] is None and "not known" in t["duration_note"]       # no duration, not the timeout's

    script("RUN")                                                    # nothing happened
    v = await app.verify_target_after("xae_restart_twincat", expect_run=True, require_transition=True, leave_timeout_s=5)
    assert not v["verified"] and v["transition_seen"] is False and "a restart could not be proven" in v["hint"]
    assert "not in RUN" not in v["hint"]
    assert v["verify_s"] >= 5 and v["polls"] >= 10                   # 0.5 s polls in the first phase

    script("RUN", "down", "CONFIG", "RUN")                           # a real restart
    import time as _time
    t_cmd = _time.monotonic()
    v = await app.verify_target_after("xae_restart_twincat", expect_run=True, require_transition=True)
    assert v["verified"] and v["modes_seen"] == ["RUN", "unreachable", "CONFIG", "RUN"] and v["left_run_after_s"] == 0.5
    t = _restart_timing(t_cmd, dict(v))
    assert t["duration_s"] is not None and t["duration_s"] >= 0 and "polled every 2 s" in t["duration_note"]

    script("RUN")                                                    # activation keeps the old contract
    assert (await app.verify_target_after("xae_activate_configuration", expect_run=False))["verified"]


@pytest.mark.anyio
async def test_r40_restart_proven_by_a_witness_taken_before_the_commands(monkeypatch, project_dir):
    """R40: polling started after activate + restart had returned and demanded to SEE RUN being left; a start in
    CONFIG (the activation brings TwinCAT to RUN) or a fast restart was never seen → 'not in RUN afterwards (mode
    RUN)' while the target ran the new configuration. B1–B5 of the fix spec."""
    import time as _time
    from twincat_mcp import dialogs as _dlg
    from twincat_mcp.server import App, ToolError, restart_proven, _restart_failure
    app = App(mk_cfg(project_dir))
    clock = [1000.0]
    mono = [500.0]
    monkeypatch.setattr(_time, "time", lambda: clock[0])
    monkeypatch.setattr(_time, "monotonic", lambda: mono[0])

    async def fake_sleep(d):
        clock[0] += d
        mono[0] += d
    monkeypatch.setattr("twincat_mcp.server.asyncio.sleep", fake_sleep)
    dialogs: list = []
    monkeypatch.setattr(_dlg, "list_dialogs", lambda pid: list(dialogs))
    W = ("TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount", "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleTime",
         "TwinCAT_SystemInfoVarList._AppInfo.AppTimestamp")

    def script(modes, counts, ts=1792300000):
        seq, cnt = list(modes), list(counts)

        async def ads_call(fn, *a, **kw):
            if getattr(fn, "__name__", "") == "read_many":
                c = cnt.pop(0) if len(cnt) > 1 else cnt[0]
                return {W[0]: c, W[1]: 10000, W[2]: ts}                     # 1 ms task
            m = seq.pop(0) if len(seq) > 1 else seq[0]
            if m == "down":
                raise ToolError("ADS error: target port not found")
            return {"twincat_mode": m}
        monkeypatch.setattr(app, "ads_call", ads_call)

    # B1: CONFIG before, RUN at every poll → proven by the mode change
    script(["CONFIG", "RUN"], [0])
    before = await app.restart_witness()
    v = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before)
    assert v["verified"] and v["transition_by"] == "mode_change" and v["mode_before"] == "CONFIG"
    # B2: RUN before and at every poll, CycleCount 900,000 → 1,200 → proven by the witness
    script(["RUN"], [900_000, 1_200])
    before = await app.restart_witness()
    mono[0] += 30
    v = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before)
    assert v["verified"] and v["transition_by"] == "witness" and "started again (900000 → 1200)" in v["witness"]
    assert v["witness_before"]["cycle_count"] == 900_000 and v["witness_after"]["cycle_count"] == 1_200
    # B3: RUN before/after, CycleCount 900,000 → 920,000 after 20 s at 1 ms → no restart; never "not in RUN"
    script(["RUN"], [900_000, 920_000])
    before = await app.restart_witness()
    mono[0] += 20
    v = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before,
                                      leave_timeout_s=5)
    assert not v["verified"] and v["transition_seen"] is False and "went on from 900000 to 920000" in v["hint"]
    assert "did not restart the runtime" in v["hint"] and "not in RUN" not in v["hint"]
    msg = _restart_failure(v)
    assert msg.startswith("the target is in RUN while a restart") and "not in RUN" not in msg
    # B4: RUN → unreachable → RUN (the poll sees it; behaviour kept)
    script(["RUN", "RUN", "down", "RUN"], [900_000, 900_000, 1_500])
    before = await app.restart_witness()
    v = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before)
    assert v["verified"] and v["transition_by"] == "poll"
    # N75: the witness reported is the one of the runtime that came back — taken in RUN after the transition, not the
    # attempt from the first poll (the old runtime going down: every value null, 'not readable before and after')
    assert v["witness_after"]["cycle_count"] == 1_500 and "started again (900000 → 1500)" in v["witness"], v
    # one poll without an answer, but task 1's counter ran on: no restart (the counter outweighs the poll)
    script(["RUN", "RUN", "down", "RUN"], [900_000, 900_000, 990_000])
    before = await app.restart_witness()
    v = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before)
    assert not v["verified"] and "went on from 900000 to 990000" in v["hint"] and "did not restart" in v["hint"]

    # N75: a value that could not be read says why; the DT AppTimestamp is compared by its number
    async def unreadable(fn, *a, **kw):
        if getattr(fn, "__name__", "") == "read_many":
            return {W[0]: {"error": "ADSError 1861 (0x745): timeout"}, W[1]: 10000,
                    W[2]: {"raw": 1792300000, "text": "DT#2026-10-17-00:00:00"}}
        return {"twincat_mode": "RUN"}
    monkeypatch.setattr(app, "ads_call", unreadable)
    w = await app.restart_witness()
    assert w["app_timestamp"] == 1792300000 and "1861" in w["unreadable"]["cycle_count"]
    verdict = restart_proven({"t": 0, "mode": "RUN", "cycle_count": 5}, w)
    assert verdict[0] is None and "after it (ADSError 1861" in verdict[1], verdict
    # B5: ends in CONFIG with a license dialog → not verified, the dialog named
    script(["RUN", "CONFIG"], [900_000])
    before = await app.restart_witness()
    dialogs.append({"title": "TwinCAT System", "text": "License expired", "buttons": ["OK"]})
    monkeypatch.setattr(_dlg, "list_dialogs", lambda pid: [type("D", (), {"to_dict": lambda self: d})() for d in dialogs])
    v = await app.verify_target_after("xae_deploy", expect_run=True, require_transition=True, before=before, timeout_s=10)
    assert not v["verified"] and v["dialogs"] and "License expired" in _restart_failure(v)
    # the decision itself (spec: proof = the counter went back or AppTimestamp differs). A counter above its old value
    # with fewer cycles than the time allows (8 s of cycles, 60 s elapsed) is a restart OR a task that stood still for
    # part of the time — cannot tell (review A: a frozen counter, 30 s of cycles at 35 s, was reported as a restart);
    # one that went on (64 s of cycles) is no restart; wrap; unknown cycle time
    b = {"t": 0, "cycle_count": 5_000, "cycle_time_100ns": 10000}
    assert restart_proven(b, {"t": 60, "cycle_count": 8_000, "cycle_time_100ns": 10000})[0] is None
    assert restart_proven(b, {"t": 60, "cycle_count": 64_000, "cycle_time_100ns": 10000})[0] is False
    frozen = restart_proven({"t": 0, "cycle_count": 3_000, "cycle_time_100ns": 100000},
                            {"t": 35, "cycle_count": 3_000, "cycle_time_100ns": 100000})
    assert frozen[0] is None and "did not advance" in frozen[1], frozen
    assert restart_proven({"t": 0, "cycle_count": 3_000, "app_timestamp": 1}, {"t": 35, "cycle_count": 3_000,
                                                                              "app_timestamp": 2})[0] is True
    assert restart_proven({"t": 0, "cycle_count": 900_000, "cycle_time_100ns": 10000},
                          {"t": 5, "cycle_count": 900_000, "cycle_time_100ns": 10000})[0] is False
    assert restart_proven({"t": 0, "cycle_count": 0xFFFFFFF0, "cycle_time_100ns": 10000},
                          {"t": 1, "cycle_count": 500, "cycle_time_100ns": 10000})[0] is None              # wrapped
    assert restart_proven({"t": 0, "cycle_count": 10}, {"t": 5, "cycle_count": 20})[0] is None
    assert restart_proven({"t": 0, "cycle_count": 0xFFFFFFF0}, {"t": 1, "cycle_count": 500})[0] is None   # may wrap
    assert restart_proven({"t": 0, "cycle_count": 900_000}, {"t": 1, "cycle_count": 500})[0] is True


@pytest.mark.anyio
async def test_d44_build_refuses_until_shell_edits_are_on_disk(monkeypatch, project_dir):
    """D44: xae_build(save_first) ran File.SaveAll but the xae_pou_set text never reached MAIN.TcPOU; an external
    reload then discarded it. The save is now checked against the source files."""
    import os
    from twincat_mcp.server import App, ToolError
    app = App(mk_cfg(project_dir))
    main = project_dir / "Plc" / "POUs" / "MAIN.TcPOU"
    new_impl = "nCycle := nCycle + 2;\nfbFill(bEnable := bStart, stRecipe := stRecipe);"
    writes = {"on": False, "calls": 0}

    async def xae_call(fn, *a, **kw):
        if fn.__name__ == "item_exists":
            return True                    # N124: the created object is in the shell, only not in a file
        assert fn.__name__ == "save_all"
        writes["calls"] += 1
        if writes["on"]:
            txt = main.read_text(encoding="utf-8")
            start = txt.index("<ST><![CDATA[") + len("<ST><![CDATA[")
            end = txt.index("]]></ST>")
            main.write_text(txt[:start] + new_impl + txt[end:], encoding="utf-8")
            st = main.stat()
            os.utime(main, (st.st_atime, st.st_mtime + 5))
        return {"saved": True, "dirty_before": ["MAIN.TcPOU"], "dirty_after": []}
    monkeypatch.setattr(app, "xae_call", xae_call)
    monkeypatch.setattr("twincat_mcp.server.asyncio.sleep", lambda d: _noop())

    assert (await app.save_verified("xae_build"))["edits_checked"] == 0        # nothing edited → nothing to prove
    app.note_edit("TIPC^Plc^Plc Project^POUs^MAIN", None, new_impl)
    with pytest.raises(ToolError, match=r"1 edit\(s\) made in the shell are not in the source files.*MAIN: implementation"):
        await app.save_verified("xae_build")
    assert writes["calls"] == 3 and app.pending_edits                         # retried once, edit still pending
    writes["on"] = True
    r = await app.save_verified("xae_build")
    assert r["edits_on_disk"] == ["TIPC^Plc^Plc Project^POUs^MAIN"] and not app.pending_edits
    # a created object that never reached a file
    app.note_edit("TIPC^Plc^Plc Project^POUs^FB_New", None, None)
    with pytest.raises(ToolError, match="FB_New: not in any source file"):
        await app.save_verified("tcunit_run")
    # a method is found as <FB>.<method>; unchanged text on disk passes
    app.pending_edits.clear()
    start = app.proj.units["fb_fill.start"].implementation
    app.note_edit("TIPC^Plc^Plc Project^POUs^FB_Fill^Start", None, start + "\n")
    assert (await app.save_verified("xae_deploy"))["edits_checked"] == 1



@pytest.mark.anyio
async def test_n113_save_check_compares_each_plc_projects_own_main(monkeypatch, tmp_path):
    """N113: with MAIN in Plc1 and Plc2, every xae_build(save_first) refused 'declaration on disk differs' — the edit of
    TIPC^Plc2^…^MAIN was compared with whichever MAIN the name-only index held. The tree path's PLC project decides."""
    from tests.test_project import _plc, _tsproj
    from twincat_mcp.server import App, ToolError
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _plc(tmp_path, "Plc2", "n := n + 2;", 2000)
    _tsproj(tmp_path, ("Plc1", 851), ("Plc2", 852))
    app = App(mk_cfg(tmp_path))

    async def xae_call(fn, *a, **kw):
        return {"saved": True, "dirty_before": [], "dirty_after": []}
    monkeypatch.setattr(app, "xae_call", xae_call)
    monkeypatch.setattr("twincat_mcp.server.asyncio.sleep", lambda d: _noop())
    app.note_edit("TIPC^Plc1^Plc1 Project^POUs^MAIN", None, "n := n + 1;")
    app.note_edit("TIPC^Plc2^Plc2 Project^POUs^MAIN", None, "n := n + 2;")
    r = await app.save_verified("xae_build")
    assert r["edits_on_disk"] == ["TIPC^Plc1^Plc1 Project^POUs^MAIN", "TIPC^Plc2^Plc2 Project^POUs^MAIN"]
    app.note_edit("TIPC^Plc2^Plc2 Project^POUs^MAIN", None, "n := n + 1;")   # Plc1's text: not what Plc2's file holds
    with pytest.raises(ToolError, match=r"Plc2 Project\^POUs\^MAIN: implementation on disk differs"):
        await app.save_verified("xae_build")



@pytest.mark.anyio
async def test_n124_an_edit_of_a_deleted_object_no_longer_blocks_every_build(monkeypatch, tmp_path):
    """N124 (0.8.13 retest, S2): FB_RetestTmp was edited through the server, then deleted (file and .plcproj entry
    gone): every xae_build (save_first) refused with the same path until the server restarted — the edit was cleared
    only by a successful check. The shell is asked: an object that is gone has its edit dropped (said); one that is
    still there, or a shell that cannot say, keeps the refusal. xae_tree_delete drops the edits below the item."""
    from tests.test_project import _plc, _tsproj
    from twincat_mcp.server import App, ToolError
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _tsproj(tmp_path, ("Plc1", 851))
    app = App(mk_cfg(tmp_path))
    exists = {"v": False}

    async def xae_call(fn, *a, **kw):
        if getattr(fn, "__name__", "") == "item_exists":
            return exists["v"]
        return {"saved": True, "dirty_before": [], "dirty_after": []}
    monkeypatch.setattr(app, "xae_call", xae_call)
    monkeypatch.setattr("twincat_mcp.server.asyncio.sleep", lambda d: _noop())
    gone = "TIPC^Plc1^Plc1 Project^POUs^FB_RetestTmp^Value^Get"
    for shell_says in (True, None):                       # still in the shell / the shell cannot say: refused
        exists["v"] = shell_says
        app.note_edit(gone, None, "Value := nValue;")
        with pytest.raises(ToolError, match="not in any source file"):
            await app.save_verified("xae_build")
    exists["v"] = False                                   # deleted: dropped, the build goes on
    app.note_edit("TIPC^Plc1^Plc1 Project^POUs^MAIN", None, "n := n + 1;")
    r = await app.save_verified("xae_build")
    assert [d["path"] for d in r["edits_dropped"]] == [gone] and "no longer exists" in r["edits_dropped"][0]["why"]
    assert r["edits_on_disk"] == ["TIPC^Plc1^Plc1 Project^POUs^MAIN"] and not app.pending_edits
    app.note_edit(gone, None, "x")
    app.note_edit("TIPC^Plc1^Plc1 Project^POUs^FB_RetestTmpX", None, "x")
    assert app.drop_edits_under("TIPC^Plc1^Plc1 Project^POUs^FB_RetestTmp") == [gone]
    assert list(app.pending_edits) == ["TIPC^Plc1^Plc1 Project^POUs^FB_RetestTmpX"]


@pytest.mark.anyio
async def test_n113_review_analyses_run_inside_one_plc_project(monkeypatch, tmp_path):
    """Review of N113: proj_lint / proj_call_graph / proj_interface read the name -> unit view, where MAIN of one PLC
    project stands for both. Each analysis runs inside one PLC project: a name in two is refused unless pinned, the
    whole-project lint is the lint of each PLC project, its findings tagged."""
    from tests.fake_ads import install
    from tests.test_project import _plc, _tsproj
    install(monkeypatch)
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _plc(tmp_path, "Plc2", "n := n + 2; // TODO Plc2 only", 2000)
    _tsproj(tmp_path, ("Plc1", 851), ("Plc2", 852))
    async with client_for(build_server(mk_cfg(tmp_path))) as s:
        r = await s.call_tool("proj_call_graph", {"root": "MAIN"})
        assert r.is_error and "several PLC projects" in text(r) and "@MAIN" in text(r)
        g = payload(await s.call_tool("proj_call_graph", {"root": "Plc2@MAIN"}))
        assert g["plc_project"].endswith("Plc2.plcproj") and g["root"] == "MAIN"
        li = payload(await s.call_tool("proj_lint", {}))
        assert len(li["plc_projects"]) == 2 and li["units"] == 2
        todo = [f for f in li["items"] if f["rule"] == "todo"]
        assert todo and all(f["plc_project"].endswith("Plc2.plcproj") for f in todo)


@pytest.mark.anyio
async def test_n141_n142_s4_0814_two_plc_projects_and_a_shared_file(monkeypatch, tmp_path):
    """0.8.14 retest §34b. N142 (S3): proj_lint / proj_interface refused a file both PLC projects compile, and the pin
    the refusal asked for ('PlcA@GVL_Common') was refused the same way — a dead end. The pin names its PLC project; the
    unpinned name is analysed in each, one answer when they agree. N141: PRG_B (Plc2) resolved on port 851 said 'no
    program/GVL/FB named'. S4: proj_variables / proj_list / proj_search / proj_references name the PLC project."""
    from tests.fake_ads import install
    from tests.test_project import _plc, _tsproj
    install(monkeypatch)
    _plc(tmp_path, "Plc1", "n := n + 1; GVL_Common.nShared := n;", 10000)
    _plc(tmp_path, "Plc2", "n := n + 2;", 2000)
    _plc(tmp_path / "Plc2", "B", "n := 3;", 2000, prog="PRG_B")
    (tmp_path / "Plc2" / "B" / "B.plcproj").unlink()
    (tmp_path / "Plc2" / "POUs" / "PRG_B.TcPOU").write_bytes((tmp_path / "Plc2" / "B" / "POUs" / "PRG_B.TcPOU").read_bytes())
    import shutil
    shutil.rmtree(tmp_path / "Plc2" / "B")
    common = tmp_path / "Common"
    common.mkdir()
    (common / "GVL_Common.TcGVL").write_text(
        '<?xml version="1.0" encoding="utf-8"?><TcPlcObject><GVL Name="GVL_Common"><Declaration><![CDATA[VAR_GLOBAL\n'
        "  nShared : INT; // shared\nEND_VAR]]></Declaration></GVL></TcPlcObject>", encoding="utf-8")
    from tests.test_project import register_sources
    for plc in ("Plc1", "Plc2"):
        pp = tmp_path / plc / f"{plc}.plcproj"
        register_sources(pp)
        pp.write_text(pp.read_text().replace("</Project>", '<ItemGroup><Compile Include="..\\Common\\GVL_Common.TcGVL">'
                                              "<SubType>Code</SubType></Compile></ItemGroup></Project>"))
    _tsproj(tmp_path, ("Plc1", 851), ("Plc2", 852))
    async with client_for(build_server(mk_cfg(tmp_path))) as s:
        for tool in ("proj_lint", "proj_interface"):
            r = payload(await s.call_tool(tool, {"name": "Plc2@GVL_Common"}))
            assert r["plc_project"].endswith("Plc2.plcproj"), (tool, r)
            r = payload(await s.call_tool(tool, {"name": "GVL_Common"}))
            assert [Path(x).stem for x in r["plc_projects"]] == ["Plc1", "Plc2"] and "same result" in r["note_plc_projects"]
        r = await s.call_tool("proj_interface", {"name": "MAIN"})              # two files: still refused
        assert r.is_error and "declared in several PLC projects" in text(r) and "@MAIN" in text(r)
        r = await s.call_tool("proj_resolve_symbol", {"symbol": "PRG_B.n"})
        assert r.is_error and "not declared in PLC project Plc1 (ADS port 851)" in text(r) and "Plc2" in text(r)
        v = payload(await s.call_tool("proj_variables", {"scope": "Plc2@GVL_Common"}))
        assert v["plc_project"].endswith("Plc2.plcproj") and v["roots"] == ["GVL_Common"]
        assert payload(await s.call_tool("proj_variables", {"scope": "GVL_Common"}))["plc_project"].endswith("Plc1.plcproj")
        units = {(u["name"], Path(u.get("plc_project") or "").stem): u for u in
                 payload(await s.call_tool("proj_list", {}))["units"]}
        assert ("MAIN", "Plc1") in units and ("MAIN", "Plc2") in units and ("PRG_B", "Plc2") in units
        assert [Path(x).stem for x in units[("GVL_Common", "")]["plc_projects"]] == ["Plc1", "Plc2"]
        hits = payload(await s.call_tool("proj_search", {"regex": "n := n"}))["hits"]
        assert sorted(Path(h["plc_project"]).stem for h in hits) == ["Plc1", "Plc2"]
        refs = payload(await s.call_tool("proj_references", {"name": "GVL_Common"}))
        assert [Path(h["plc_project"]).stem for h in refs["mentions"]] == ["Plc1"], refs


async def _noop():
    return None


@pytest.mark.anyio
async def test_d45_doctor_reports_config_mode_as_info(monkeypatch, project_dir):
    """D45: TwinCAT deliberately in CONFIG made tc_doctor say 'PROBLEMS: target_state'."""
    import pyads
    from pyads import constants as C
    from tests.fake_ads import FakeConnection
    install(monkeypatch)
    orig = FakeConnection.read_state

    def read_state(self):
        if (self.ams_net_port or 851) == 851:
            raise pyads.ADSError(6)                      # no PLC runtime in CONFIG
        return orig(self)
    monkeypatch.setattr(FakeConnection, "read_state", read_state)
    FakeConnection.shared_state[10000] = (C.ADSSTATE_CONFIG, 0)
    try:
        async with client_for(build_server(mk_cfg(project_dir))) as s:
            d = payload(await s.call_tool("tc_doctor", {}))
            # (infosys may be a PROBLEM here: the test PC has no Help Viewer package)
            assert "target_state" not in d["summary"] and "(info: TwinCAT in CONFIG — PLC runtime not running" in d["summary"], d["summary"]
            assert d["target_state"]["twincat_mode"] == "CONFIG"
            FakeConnection.shared_state[10000] = (C.ADSSTATE_RUN, 0)        # RUN but no PLC answering: a real fault
            d = payload(await s.call_tool("tc_doctor", {}))
            assert "PROBLEMS: target_state" in d["summary"] and "system mode RUN" in d["target_state"]
    finally:
        FakeConnection.shared_state.pop(10000, None)


def test_d40_xunit_publish_is_read_from_the_project_source(project_dir):
    """D40: TcUnit's xUnitEnablePublish defaults to FALSE → no result file; tcunit_run waited timeout_s for nothing."""
    from twincat_mcp.server import App, _tcunit_publish_in_source
    app = App(mk_cfg(project_dir))
    assert _tcunit_publish_in_source(app)["value"] is None                       # no TcUnit reference
    pp = project_dir / "Plc" / "Plc.plcproj"
    base = pp.read_text()
    ref = '<PlaceholderReference Include="TcUnit"><DefaultResolution>TcUnit, * (www.tcunit.org)</DefaultResolution>'
    pp.write_text(base.replace('<PlaceholderReference Include="Tc2_Standard">', ref + '</PlaceholderReference>'
                               '<PlaceholderReference Include="Tc2_Standard">'))
    app.proj.scan()
    r = _tcunit_publish_in_source(app)
    assert r["value"] is False and "default FALSE" in r["source"]
    pp.write_text(base.replace('<PlaceholderReference Include="Tc2_Standard">', ref +
                               '<Parameters><Parameter ListName="GVL_Param_TcUnit"><Key>xUnitEnablePublish</Key>'
                               '<Value>TRUE</Value></Parameter></Parameters></PlaceholderReference>'
                               '<PlaceholderReference Include="Tc2_Standard">'))
    assert _tcunit_publish_in_source(app)["value"] is True


@pytest.mark.anyio
async def test_d42_tree_delete_token_names_the_item(monkeypatch, project_dir):
    """D42: tc_request_confirmation only issued a NetId token for xae_tree_delete while the tool checked the item path."""
    install(monkeypatch)
    async with client_for(build_server(mk_cfg(project_dir, mode="guarded"))) as s:
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_tree_delete",
                                                                    "target": "TIPC^Plc^Plc Project^POUs^FB_Old"}))
        assert tok["target"] == "TIPC^Plc^Plc Project^POUs^FB_Old" and "xae_target" not in tok


@pytest.mark.anyio
async def test_n40_n42_token_targets_are_computed_by_the_server(monkeypatch, project_dir):
    """N42: xae_plc's token named only the action — a 'download' token approved for project A downloaded B, or to a
    remote XAE target. N40: every tool wanted a different target format and each wrong guess cost a retry. The
    target is now computed by one rule, by the tool and by tc_request_confirmation."""
    from twincat_mcp import xae_backend as X
    install(monkeypatch)
    projects = ["TIPC^PlcA^PlcA Project", "TIPC^PlcB^PlcB Project"]
    done = []
    monkeypatch.setattr(X.XaeBackend, "target_get", lambda self: {"target_net_id": "5.1.2.3.1.1", "twincat_started": True})
    monkeypatch.setattr(X.XaeBackend, "_nested_projects", lambda self, p=None: [X._plc_split(p)[1]] if p else list(projects))
    monkeypatch.setattr(X.XaeBackend, "plc_action", lambda self, a, p=None: done.append((a, p)) or {"action": a, "ok": True})
    async with client_for(build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))) as s:
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_plc", "args": {
            "action": "download", "plc_project_path": "TIPC^PlcB"}}))
        assert tok["target"] == "download:TIPC^PlcB^PlcB Project@5.1.2.3.1.1" and tok["xae_target"] == "5.1.2.3.1.1"
        # N78: no token for a PLC project the solution does not have — and the tool refuses it the same way
        r = await s.call_tool("tc_request_confirmation", {"action": "xae_plc", "args": {
            "action": "download", "plc_project_path": "TIPC^Other^Other Project"}})
        assert r.is_error and "no PLC project 'TIPC^Other^Other Project'" in text(r) and "PlcB Project" in text(r)
        r = await s.call_tool("xae_plc", {"action": "download", "plc_project_path": "TIPC^Other^Other Project",
                                          "confirm_token": tok["confirm_token"]})
        assert r.is_error and "no PLC project" in text(r) and done == []
        # the token for B does not download A
        r = await s.call_tool("xae_plc", {"action": "download", "plc_project_path": "TIPC^PlcA^PlcA Project",
                                          "confirm_token": tok["confirm_token"]})
        assert r.is_error and done == []
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_plc", "args": {
            "action": "download", "plc_project_path": "TIPC^PlcB"}}))
        payload(await s.call_tool("xae_plc", {"action": "download", "plc_project_path": "TIPC^PlcB",
                                              "confirm_token": tok["confirm_token"]}))
        assert done == [("download", "TIPC^PlcB")]
        # two projects and only the action given: the error says what is missing, before any token exists
        r = await s.call_tool("tc_request_confirmation", {"action": "xae_plc", "target": "login"})
        assert r.is_error and "2 PLC projects" in text(r) and "plc_project_path" in text(r)
        # the tool itself: the missing PLC project is said first, the guard's refusal after it (N41/N42 polish)
        r = await s.call_tool("xae_plc", {"action": "login"})
        assert r.is_error and "xae_plc: 2 PLC projects" in text(r) and done[-1:] == [("download", "TIPC^PlcB")]
        assert "target=''" not in text(r) and "needs a confirm_token too" in text(r)
        # a partial hand-written target is completed (one project): no retry needed
        projects[:] = ["TIPC^PlcA^PlcA Project"]
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_plc", "target": "login"}))
        assert tok["target"] == "login:TIPC^PlcA^PlcA Project@5.1.2.3.1.1"
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_command", "target": "PLC.Login"}))
        assert tok["target"] == "PLC.Login@5.1.2.3.1.1"
        # a complete but wrong target is refused with the right one
        r = await s.call_tool("tc_request_confirmation", {"action": "xae_deploy", "target": "TIPC^Untitled1"})
        assert r.is_error and "checks the target '5.1.2.3.1.1', not 'TIPC^Untitled1'" in text(r)
        r = await s.call_tool("tc_request_confirmation", {"action": "xae_command", "target": "PLC.Login@9.9.9.9.1.1"})
        assert r.is_error and "PLC.Login@5.1.2.3.1.1" in text(r)
        # the table is in the tool description
        desc = next(t.description for t in (await s.list_tools()).tools if t.name == "tc_request_confirmation")
        assert "xae_plc: '<action>:<PLC project path>@<XAE target NetId>'" in desc and "ipc_reboot" in desc


@pytest.mark.anyio
async def test_r4_outputs_stay_in_known_folders_and_never_overwrite_silently(monkeypatch, project_dir, tmp_path):
    """R4: rec_export(out_path='C:\\\\Windows\\\\System32\\\\…') was accepted and an existing file silently replaced."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir)
    async with client_for(build_server(cfg)) as s:
        rid = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change"}))["id"]
        await s.call_tool("rec_stop", {"id": rid})
        bad = await s.call_tool("rec_export", {"id": rid, "out_path": str(tmp_path / "elsewhere" / "x.csv")})
        assert bad.is_error and "outside the folders this server writes to" in text(bad)
        target = project_dir / "exports" / "x.csv"
        target.parent.mkdir()
        target.write_text("keep me")
        r = await s.call_tool("rec_export", {"id": rid, "out_path": str(target)})
        assert r.is_error and "overwrite=true" in text(r) and target.read_text() == "keep me"
        ok = payload(await s.call_tool("rec_export", {"id": rid, "out_path": str(target), "overwrite": True}))
        assert Path(ok["file"]).resolve() == target.resolve() and target.read_text() != "keep me"
        again = payload(await s.call_tool("rec_export", {"id": rid}))                       # default path: first export
        assert again["file"] and "replaced" not in again
        same = payload(await s.call_tool("rec_export", {"id": rid}))                        # identical re-export: fine
        assert "identical" in same["replaced"]
        # N32: other parameters at the default path → the earlier export is not replaced silently
        other = await s.call_tool("rec_export", {"id": rid, "fill": True})
        assert other.is_error and "an earlier export (csv_wide, symbols all, fill False" in text(other) \
            and "overwrite=true" in text(other)
        forced = payload(await s.call_tool("rec_export", {"id": rid, "fill": True, "overwrite": True}))
        assert forced["replaced"].startswith("an earlier export (csv_wide")
        Path(forced["file"]).chmod(0o644)
        Path(forced["file"]).write_text("edited by hand")                                   # changed since → refused
        edited = await s.call_tool("rec_export", {"id": rid, "fill": True})
        assert edited.is_error and "changed since it was written" in text(edited)
        sr = payload(await s.call_tool("tc_session_report", {}))                           # R12: preview, not the whole report
        assert "markdown" not in sr and sr["preview"] and Path(sr["file"]).is_file()
        bad = await s.call_tool("tc_session_report", {"out_path": str(tmp_path / "r.md")})
        assert bad.is_error and "output_dirs" in text(bad)


def test_r8_failed_dump_leaves_no_partial_files(monkeypatch, project_dir, tmp_path):
    """R8: a failed whole-PLC dump left a 61 MB CSV and a 0-byte JSON behind, unreported."""
    import threading
    from tests.fake_ads import install
    from twincat_mcp.ads_backend import AdsBackend, AdsError
    from twincat_mcp.config import AdsConfig
    install(monkeypatch)
    ads = AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1"))
    out = tmp_path / "d"
    ok = ads.dump(None, False, str(out))
    assert Path(ok["csv"]).is_file() and Path(ok["json"]).is_file() and not list(out.glob("*.partial"))
    stop = threading.Event()
    stop.set()
    with pytest.raises(AdsError, match=r"cancelled.*partial files removed"):
        ads.dump(None, False, str(out), cancel=stop)
    assert not list(out.glob("*.partial")) and len(list(out.glob("dump-*.csv"))) == 1


@pytest.mark.anyio
async def test_r6_a_call_queued_behind_a_long_job_says_so(monkeypatch, project_dir):
    """R6: behind a 17-min ads_dump every call timed out with 'router/route/firewall?' — even rec_start(mode='bogus')."""
    import time as _t
    from twincat_mcp.worker import SerialWorker, WorkerBusy
    w = SerialWorker("tcmcp-test")

    def ads_dump_like():
        _t.sleep(1.0)
        return "dump done"
    ran = []
    long = asyncio.ensure_future(w.run(ads_dump_like, timeout=10))
    await asyncio.sleep(0.1)
    with pytest.raises(WorkerBusy, match=r"quick not started: tcmcp-test is busy with ads_dump_like for 0 s"):
        await w.run(_named("quick", ran), timeout=0.2)
    assert await long == "dump done"
    await asyncio.sleep(0.1)
    assert ran == [], "a job that timed out in the queue must not run later"
    w.close()


def _named(name, sink):
    def f():
        sink.append(1)
    f.__name__ = name
    return f


@pytest.mark.anyio
async def test_r2_no_tokens_for_actions_no_tool_performs(monkeypatch, project_dir):
    install(monkeypatch)
    async with client_for(build_server(mk_cfg(project_dir, mode="guarded"))) as s:
        r = await s.call_tool("tc_request_confirmation", {"action": "format_c_drive", "target": "C:"})
        assert r.is_error and "no tool performs 'format_c_drive'" in text(r)
        r = await s.call_tool("tc_request_confirmation", {"action": "ads_wrte", "target": "MAIN.x"})
        assert r.is_error and "ads_write" in text(r)
        assert payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": "x"}))["confirm_token"]


@pytest.mark.anyio
async def test_r5_datatype_table_unavailable_names_config_mode(monkeypatch, project_dir):
    from pyads import constants as C
    from tests.fake_ads import FakeConnection
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    app = srv._tcmcp_app
    monkeypatch.setattr(app.ads, "typesystem", lambda *a, **k: None)
    FakeConnection.shared_state[10000] = (C.ADSSTATE_CONFIG, 0)
    try:
        async with client_for(srv) as s:
            r = await s.call_tool("ads_export", {"types": ["ST_Recipe"]})
            assert r.is_error and "TwinCAT is in CONFIG" in text(r), text(r)
    finally:
        FakeConnection.shared_state.pop(10000, None)


@pytest.mark.anyio
async def test_the_first_manuals_build_starts_by_itself_and_only_the_first(monkeypatch, project_dir, tmp_path):
    """Rule 6: the first build of a missing index starts by itself — manuals too, when manuals\\ holds PDFs and the
    server's Python reads PDFs; an index that exists (even outdated) is left to the user's approval."""
    pytest.importorskip("pypdf")
    pytest.importorskip("reportlab")
    import importlib.util
    import time as _t
    from tests.fake_ads import install
    install(monkeypatch)
    spec = importlib.util.spec_from_file_location("mkfix_e2e", Path(__file__).parent / "fixtures" / "kb" / "make_fixtures.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)
    kb = tmp_path / "kb"
    (kb / "manuals" / "IPC").mkdir(parents=True)
    mk.build_pdf(kb / "manuals" / "IPC" / "CX9020.pdf")
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    build_server(cfg)
    db = kb / "index" / "manuals.db"
    t0 = _t.time()
    while _t.time() - t0 < 60 and not db.exists():
        _t.sleep(0.1)
    from twincat_mcp import kb_lock
    while _t.time() - t0 < 60 and kb_lock.held(db.with_name(db.name + ".lock")):
        _t.sleep(0.1)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        st = payload(await s.call_tool("kb_status", {}))["docs"]["manuals"]
        assert st["state"] == "complete" and st["manuals"] == 1, st
    mk.build_pdf(kb / "manuals" / "IPC" / "CX9021.pdf")            # outdated now: not rebuilt by a start
    before = db.stat().st_mtime_ns
    build_server(cfg)
    _t.sleep(1.5)
    assert db.stat().st_mtime_ns == before and not kb_lock.held(db.with_name(db.name + ".lock"))


@pytest.mark.anyio
async def test_offline_docs_state_at_connection_and_in_doctor(monkeypatch, project_dir, kb_copy):
    """Finding 2: the real state of the offline index is known at every (re)connection — in the instructions the
    server sends at initialize — and in tc_doctor; an interrupted rebuild is said up front, with the approval rule."""
    import sqlite3
    from tests.fake_ads import install
    install(monkeypatch)
    kb = kb_copy
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    srv = build_server(cfg)
    ins = srv._lowlevel_server.instructions
    assert "Offline documentation, state at this connection" in ins and "Infosys index: complete (9 pages" in ins
    db = kb / "index" / "infosys-1033.db"
    building = db.with_name(db.name + ".building")
    building.write_bytes(db.read_bytes())
    c = sqlite3.connect(building)
    c.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [("state", "building"), ("pages_total", "51656")])
    c.commit()
    c.close()
    c = sqlite3.connect(db)                                          # the index in use: an older format (approved
    c.execute("UPDATE meta SET value = '3' WHERE key = 'indexer_version'")   # rebuild interrupted half-way)
    c.commit()
    c.close()
    srv = build_server(cfg)                                          # a reconnection = a new server process
    ins = srv._lowlevel_server.instructions
    assert "a build was INTERRUPTED at 9 of 51656 pages" in ins and "infosys_local.py" in ins
    assert "rebuilt only when the user approves it" in ins and "kb_rebuild(index='infosys')" in ins
    async with client_for(srv) as s:
        d = payload(await s.call_tool("tc_doctor", {}))
        assert d["offline_docs"]["infosys_rebuild"] == "interrupted" and "INTERRUPTED" in d["summary"]
        st = payload(await s.call_tool("kb_status", {}))
        assert st["docs"]["infosys"]["rebuild"]["state"] == "interrupted" and "command" in st["docs"]["infosys"]


def _gvl(project_dir, name: str, decl: str) -> None:
    (project_dir / "Plc" / "GVLs" / f"{name}.TcGVL").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<TcPlcObject Version="1.1.0.1" ProductVersion="3.1.4024.12">\n'
        f'  <GVL Name="{name}" Id="{{aaaaaaaa-0000-0000-0000-0000000000{len(name):02d}}}">\n'
        f"    <Declaration><![CDATA[{decl}]]></Declaration>\n  </GVL>\n</TcPlcObject>\n", encoding="utf-8")
    register_sources(project_dir / "Plc" / "Plc.plcproj")              # N106: part of the PLC project


@pytest.mark.anyio
async def test_n92_a_gvl_of_the_source_without_symbols_on_the_target_is_named(monkeypatch, project_dir):
    """N92 (live): a GVL nothing references is not linked — build 0 errors, no symbol on the target — and ads_dump
    still counted it among the project's 'known' roots. Now ads_dump, proj_vs_runtime and xae_build name it."""
    install(monkeypatch)
    _gvl(project_dir, "GVL_Unused", "VAR_GLOBAL\n\tnSpare : INT;\nEND_VAR\n")
    _gvl(project_dir, "GVL_Pinned", "{attribute 'linkalways'}\nVAR_GLOBAL\n\tnKeep : INT;\nEND_VAR\n")
    _gvl(project_dir, "GVL_Consts", "VAR_GLOBAL CONSTANT\n\tcN : INT := 3;\nEND_VAR\n")     # nothing to expect
    _gvl(project_dir, "GVL_Io", "VAR_GLOBAL\n\tbIn AT %I* : BOOL;\nEND_VAR\n")
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        d = payload(await s.call_tool("ads_dump", {"out_dir": str(project_dir / "dumps")}))
        cmp_ = payload(await s.call_tool("proj_vs_runtime", {}))
        only = payload(await s.call_tool("ads_dump", {"out_dir": str(project_dir / "dumps"), "pattern": "MAIN.*"}))
    got = {m["root"]: m for m in d["not_on_target"]}
    assert set(got) == {"GVL_Unused", "GVL_Pinned", "GVL_Io"}, got
    assert "not linked" in got["GVL_Unused"]["cause"] and "linkalways" in got["GVL_Unused"]["cause"]
    assert "another program than this source" in got["GVL_Pinned"]["cause"]
    assert "GVL_Unused" in d["not_on_target_warning"]
    # the order counts what the target has: MAIN and GVL_Machine of the source, not the 6 names it declares
    assert "(2 of them in this dump)" in d["order"], d["order"]
    assert {m["root"] for m in cmp_["roots_not_on_target"]} == {"GVL_Unused", "GVL_Pinned", "GVL_Io"}
    assert "not_on_target" not in only                                          # pattern MAIN.*: none of them asked for

    # xae_build: before any download, from the source alone
    srv = build_server(mk_cfg(project_dir))
    app = srv._tcmcp_app

    async def xae_call(fn, *a, **kw):
        assert fn.__name__ == "build", fn
        return {"success": True, "errors": [], "warnings": 0}
    monkeypatch.setattr(app, "xae_call", xae_call)
    async with client_for(srv) as s:
        b = payload(await s.call_tool("xae_build", {"save_first": False}))
    assert b["success"] and b["warnings"] == 0 and "GVL_Unused" in b["link_warning"], b
    assert [x["gvl"] for x in b["link_check"]["not_linked"]] == ["GVL_Unused"]   # GVL_Pinned: linkalways; Machine: used
    assert [x["gvl"] for x in b["link_check"]["with_io_not_judged"]] == ["GVL_Io"]
    main = project_dir / "Plc" / "POUs" / "MAIN.TcPOU"
    main.write_text(main.read_text(encoding="utf-8").replace(
        "GVL_Machine.nDrumsFilled := GVL_Machine.nDrumsFilled + 1;",
        "GVL_Machine.nDrumsFilled := GVL_Machine.nDrumsFilled + 1;\n\tnSpare := 1;"), encoding="utf-8")
    app.proj.refresh()
    assert app.proj.unreferenced_gvls()["not_linked"] == []                   # an unqualified use is a use


@pytest.mark.anyio
async def test_n98_an_export_into_a_folder_that_does_not_exist_yet(monkeypatch, project_dir, tmp_path):
    """N98 (0.8.11 retest): rec_export(out_path=<new folder>\\x.csv) raised FileNotFoundError of its temp file. A
    folder inside the folders this server writes to is created; outside them nothing is created."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir)
    async with client_for(build_server(cfg)) as s:
        rid = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change"}))["id"]
        await s.call_tool("rec_stop", {"id": rid})
        target = project_dir / "exports" / "new" / "deeper" / "x.csv"
        ok = payload(await s.call_tool("rec_export", {"id": rid, "out_path": str(target)}))
        assert Path(ok["file"]).resolve() == target.resolve() and target.is_file()
        out = project_dir / "types" / "fresh" / "types.ts"                 # another writer through the same check
        payload(await s.call_tool("ads_export", {"types": ["ST_Recipe"], "fmt": "typescript", "out_path": str(out)}))
        assert out.is_file()
        bad = await s.call_tool("rec_export", {"id": rid, "out_path": str(tmp_path / "elsewhere" / "new" / "x.csv")})
        assert bad.is_error and not (tmp_path / "elsewhere").exists()
        (project_dir / "exports" / "afile").write_text("x")
        bad = await s.call_tool("rec_export", {"id": rid, "out_path": str(project_dir / "exports" / "afile" / "x.csv")})
        assert bad.is_error and "is a file, not a folder" in text(bad)


@pytest.mark.anyio
async def test_n109_a_scaffold_the_project_does_not_compile_is_not_gated_by_config_mode(monkeypatch, project_dir):
    """N109 (0.8.11 retest): tcunit_scaffold with add_to_plcproj=false was refused in RUN (edit_requires_config_mode)
    when its out_dir was under the project root. What the PLC project compiles is its .plcproj (N106): a scaffold
    not registered and not over a compiled file is no edit of it, wherever it is written."""
    install(monkeypatch)
    from twincat_mcp import ads_backend as AB
    monkeypatch.setattr(AB.AdsBackend, "system_state", lambda self: {"twincat_mode": "RUN"})
    srv = build_server(mk_cfg(project_dir, mode="full", edit_requires_config_mode=True))
    async with client_for(srv) as s:
        scratch = project_dir / "scratch" / "tests"                       # under the root, in no PLC project
        r = payload(await s.call_tool("tcunit_scaffold", {"fb_name": "FB_Valve", "out_dir": str(scratch),
                                                          "add_to_plcproj": False}))
        assert Path(r["file"]).parent == scratch.resolve() and "FB_Valve_Tests" not in \
            (project_dir / "Plc" / "Plc.plcproj").read_text()
        # registered in the .plcproj: a code edit — refused in RUN
        res = await s.call_tool("tcunit_scaffold", {"fb_name": "FB_Valve", "add_to_plcproj": True})
        assert res.is_error and "is in RUN — switch to CONFIG first" in text(res), text(res)
        # over a file the project compiles (even with add_to_plcproj=false): a code edit — refused in RUN
        pou = project_dir / "Plc" / "POUs" / "Tests" / "FB_Valve_Tests.TcPOU"
        pou.parent.mkdir(parents=True)
        pou.write_text((project_dir / "Plc" / "POUs" / "FB_Valve.TcPOU").read_text()
                       .replace('Name="FB_Valve"', 'Name="FB_Valve_Tests"').replace("FUNCTION_BLOCK FB_Valve",
                                                                                "FUNCTION_BLOCK FB_Valve_Tests"))
        register_sources(project_dir / "Plc" / "Plc.plcproj")
        res = await s.call_tool("tcunit_scaffold", {"fb_name": "FB_Valve", "add_to_plcproj": False, "overwrite": True})
        assert res.is_error and "is in RUN — switch to CONFIG first" in text(res), text(res)


def test_n42_an_xae_token_is_bound_to_the_machine_not_the_ads_port(tmp_path):
    """N42 (0.8.13 retest, S4): the token for xae_plc download on Plc2 (port 852) said scope '…:851' — the ADS
    connection's port. An XAE confirmation is bound to the machine; the PLC project is in its target."""
    from twincat_mcp.server import App
    app = App(mk_cfg(tmp_path))
    app.ads.net_id, app.ads.port = "192.168.1.5.1.1", 851
    assert app.scope("xae_plc") == "192.168.1.5.1.1" and app.scope("xae_deploy") == "192.168.1.5.1.1"
    assert app.scope("ads_write") == "192.168.1.5.1.1:851" and app.scope() == "192.168.1.5.1.1:851"
    tok = app.guard.request_token("xae_plc", "download:TIPC^Plc2^Plc2 Project@192.168.1.5.1.1", app.scope("xae_plc"))
    assert ":851" not in tok["scope"] and ":851" not in tok["note"]


@pytest.mark.anyio
async def test_n127_kb_changes_are_gated_by_the_mode_and_audited(monkeypatch, project_dir, tmp_path):
    """N127 (0.8.13 retest, S1): kb_set_location / kb_add_project / kb_remove_project / kb_reindex are EDIT tools but
    never asked the guard — kb_reindex rewrote 47 units in readonly while xae_pou_set was refused — and kb_reindex
    left no audit record; kb_rebuild skipped the mode gate (to always ask) and so was not blocked by readonly."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="readonly", max_mode="guarded")
    cfg.knowledge.location = str(tmp_path / "kb")
    from tests.test_knowledge import ESI
    (tmp_path / "esi").mkdir()
    (tmp_path / "esi" / "Beckhoff EL2xxx.xml").write_text(ESI, encoding="utf-8")
    cfg.knowledge.esi_dirs = [str(tmp_path / "esi")]
    async with client_for(build_server(cfg)) as s:
        r = payload(await s.call_tool("kb_set_location", {}))           # a question: answered in readonly
        assert r["changed"] is False
        for tool, args in (("kb_add_project", {"path": str(project_dir)}), ("kb_reindex", {}),
                           ("kb_remove_project", {"path": str(project_dir)}),
                           ("kb_set_location", {"path": str(tmp_path / "kb2")}),
                           ("kb_rebuild", {"index": "esi", "force": True})):
            res = await s.call_tool(tool, args)
            assert res.is_error and "readonly mode" in text(res), (tool, text(res))
        assert not (tmp_path / "kb2").exists()
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 20}))
        refused = {a["tool"] for a in audit if a["outcome"] == "refused"}
        assert {"kb_add_project", "kb_reindex", "kb_remove_project", "kb_set_location", "kb_rebuild"} <= refused, audit
    cfg.safety.mode = "guarded"
    async with client_for(build_server(cfg)) as s:
        r = payload(await s.call_tool("kb_add_project", {"path": str(project_dir), "label": "p"}))
        assert r["reindexed"][0]["units"] >= 8
        payload(await s.call_tool("kb_reindex", {"path": "p"}))
        payload(await s.call_tool("kb_remove_project", {"path": "p"}))
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 20}))
        ok = [(a["tool"], a["outcome"]) for a in audit if a["tool"].startswith("kb_")]
        assert ("kb_add_project", "ok") in ok and ("kb_reindex", "ok") in ok and ("kb_remove_project", "ok") in ok, ok
        res = await s.call_tool("kb_reindex", {"path": "nothing-like-this"})
        assert res.is_error
        audit = payload(await s.call_tool("tc_audit_tail", {"n": 3}))
        assert audit[-1]["tool"] == "kb_reindex" and audit[-1]["outcome"] == "error", audit


@pytest.mark.anyio
async def test_review_0814_plugin_composed_xae_errors_get_no_known_error_note(tmp_path):
    """Review of 0.8.14: the plugin's own '… 5 s later the project is logged in, application Run …' (an online command
    that may have acted) got 'XAE is logged in to the PLC … log out first' appended, and 'is not logged in' matched
    the logged-in note. A message the plugin composed carries its own cause; a negated phrase is not the state."""
    from twincat_mcp import xae_backend as XB
    from twincat_mcp.server import App, ToolError
    app = App(mk_cfg(tmp_path))
    app.dialogs_now = lambda: []                                      # no shell on this machine

    def acted():
        raise XB._ActedUnconfirmed("ITcPlcOnline.Login on TIPC^P^P Project returned, but 5 s later the project is "
                                   "logged in, application Stop — the command may have acted; check xae_status")
    with pytest.raises(ToolError) as ei:
        await app.xae_call(acted)
    assert "known cause" not in str(ei.value) and "may have acted" in str(ei.value)

    def raw():
        raise XB.XaeError("ExecuteCommand: the project is not logged in")
    with pytest.raises(ToolError) as ei:
        await app.xae_call(raw)
    assert "known cause" not in str(ei.value), str(ei.value)


@pytest.mark.anyio
async def test_review_0814_the_binding_flags_are_on_the_tools_own_result(monkeypatch, project_dir):
    """Review of 0.8.14: `reattached` went on the first XAE call of a tool — the token-target lookup, whose result is
    thrown away — so xae_plc / xae_activate_configuration never said it; Scope results copied only their own keys."""
    from twincat_mcp import xae_backend as X
    install(monkeypatch)
    lookups = {"n": 0}

    def target_get(self):
        lookups["n"] += 1
        self.reattach_note = f"the XAE shell went away; re-attached (lookup {lookups['n']})"
        return {"target_net_id": "5.1.2.3.1.1", "twincat_started": True}
    monkeypatch.setattr(X.XaeBackend, "target_get", target_get)
    monkeypatch.setattr(X.XaeBackend, "mismatch_note", lambda self: "bound to C:\\P\\Other.sln, not xae.solution_path")
    monkeypatch.setattr(X.XaeBackend, "_nested_projects", lambda self, p=None: ["TIPC^Plc^Plc Project"])
    monkeypatch.setattr(X.XaeBackend, "plc_action", lambda self, a, p=None: {"action": a, "ok": True})
    async with client_for(build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))) as s:
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_plc", "args": {"action": "login"}}))
        r = payload(await s.call_tool("xae_plc", {"action": "login", "confirm_token": tok["confirm_token"]}))
        assert r["ok"] and r["solution_mismatch"] and "re-attached" in r["reattached"], r
        r = payload(await s.call_tool("proj_scan", {}))                   # no XAE call: no XAE flags
        assert "solution_mismatch" not in r and "reattached" not in r


@pytest.mark.anyio
async def test_n162_the_connection_instructions_say_when_they_were_read_and_where_the_current_state_is(
        monkeypatch, project_dir, kb_copy):
    """N162 (S3, 0.8.14 retest): the session-start text said Infosys 'REBUILD is due — format 5 … built 2026-10-19
    14:33' while the index was format 6, built 2026-10-20 16:11 (kb_status agreed with the files). It is sent once
    per connection and cannot follow a rebuild that finishes during the session: it says when it was read and that
    kb_status is the current state — and a new initialisation reads the state again."""
    import sqlite3
    from tests.fake_ads import install
    install(monkeypatch)
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb_copy)
    srv = build_server(cfg)
    ins = srv._lowlevel_server.instructions
    assert "not refreshed while the session runs — kb_status gives the current state" in ins and " read 20" in ins
    db = kb_copy / "index" / "infosys-1033.db"
    c = sqlite3.connect(db)
    c.execute("UPDATE meta SET value = '3' WHERE key = 'indexer_version'")      # the index changes after the start
    c.commit()
    c.close()
    assert "format 3" not in srv._lowlevel_server.instructions
    fresh = srv._lowlevel_server.create_initialization_options().instructions
    assert "check kb_status first" in fresh and "kb_rebuild(index='infosys')" in fresh, fresh


@pytest.mark.anyio
async def test_glem_xae_call_adds_the_call_s_messages_with_their_documented_codes(tmp_path):
    """0.8.16: an XAE call's result carries what the System Manager logged during its actions (xae_messages), each line
    that names a code looked up in the errors index (Beckhoff's text and page, or documented=false) — only this call's:
    the record is emptied before every call."""
    from tests.test_errors_index import build as err_build, make_kb
    from twincat_mcp.server import App
    kb = make_kb(tmp_path)
    assert err_build(kb)["result"] == "built"
    cfg = mk_cfg(tmp_path)
    cfg.knowledge.location = str(kb)
    app = App(cfg)
    app.dialogs_now = lambda: []
    app.xae.messages = [{"action": "earlier call", "messages": "x", "lines": ["x"]}]

    def activate():
        app.xae.messages.append({"action": "ActivateConfiguration", "messages": "Error: ADS error 0x745\nInfo: done",
                                 "lines": ["Error: ADS error 0x745", "Info: done", "Error: C0373 in MAIN"]})
        return {"activated": True}
    res = await app.xae_call(activate)
    m = res["xae_messages"]
    assert [r["action"] for r in m] == ["ActivateConfiguration"] and res["activated"] is True, res
    assert m[0]["codes"][0] == {"line": 0, "read_as": ["0x745"], "documented": True, "matches": [
        {"domain": "ads", "hex": "0x745", "symbol": "ADSERR_CLIENT_SYNCTIMEOUT",
         "text": m[0]["codes"][0]["matches"][0]["text"], "ref": m[0]["codes"][0]["matches"][0]["ref"]}]}, m
    assert m[0]["codes"][1]["line"] == 2 and m[0]["codes"][1]["documented"] is False, m
    assert len(m[0]["codes"]) == 2
    res = await app.xae_call(lambda: {"ok": True})
    assert "xae_messages" not in res and app.xae.messages == [], res


@pytest.mark.anyio
async def test_review_0816_each_xae_call_carries_only_its_own_messages(tmp_path):
    """Review of 0.8.16 (4, 12): the call's messages were read in the event loop after the worker had moved on — a call
    queued right behind emptied them (the activation's warning lost) or added its own (one message in both results).
    They are taken in the worker thread that ran the call. And a call whose later step fails still says what its
    earlier steps logged (a deploy whose restart fails showed only the restart's messages)."""
    import time as _t
    from twincat_mcp.server import App, ToolError
    from twincat_mcp.xae_backend import XaeError
    app = App(mk_cfg(tmp_path))
    app.dialogs_now = lambda: []

    def activate():
        _t.sleep(0.3)
        app.xae.messages.append({"action": "ActivateConfiguration", "messages": "Warning: license expires in 3 days",
                                 "lines": ["Warning: license expires in 3 days"]})
        return {"activated": True}

    def status():
        _t.sleep(0.2)
        return {"attached": True}

    def create():
        app.xae.messages.append({"action": "CreateChild", "messages": "Info: FB_New created",
                                 "lines": ["Info: FB_New created"]})
        _t.sleep(0.2)
        return {"created": True}
    for second in (status, create):
        a, b = await asyncio.gather(app.xae_call(activate), app.xae_call(second))
        assert [m["messages"] for m in a["xae_messages"]] == ["Warning: license expires in 3 days"], (a, b)
        if second is status:
            assert "xae_messages" not in b, b
        else:
            assert [m["messages"] for m in b["xae_messages"]] == ["Info: FB_New created"], b

    def deploy():
        app.xae.messages.append({"action": "ActivateConfiguration", "messages": "Warning: license expires in 3 days",
                                 "lines": ["Warning: license expires in 3 days"]})
        app.xae.messages.append({"action": "StartRestartTwinCAT", "failed": True, "messages": "Error: timeout",
                                 "lines": ["Error: timeout"]})
        raise XaeError("StartRestartTwinCAT: E_FAIL — XAE messages (StartRestartTwinCAT): Error: timeout")
    with pytest.raises(ToolError) as ei:
        await app.xae_call(deploy)
    assert "XAE messages of the steps before it: ActivateConfiguration: Warning: license expires in 3 days" in \
        str(ei.value) and str(ei.value).count("Error: timeout") == 1, str(ei.value)


@pytest.mark.anyio
async def test_0816_err_lookup_takes_the_code_from_the_errors_seen_then_goes_online(monkeypatch, project_dir, tmp_path):
    """0.8.16 (user decision): a question without a code is not answered from a shared word. The code is looked for
    where errors were seen — the errors this server returned (an ads_read that failed with ADS error 1808) and the XAE
    messages of its calls, then the XAE Error List — and when the local documentation has nothing, a site search of
    infosys.beckhoff.com online (docs.web_fallback; off or without a docs index: said so)."""
    from tests.fake_ads import install
    from tests.test_errors_index import build as err_build, make_kb
    install(monkeypatch)
    kb = make_kb(tmp_path)
    assert err_build(kb)["result"] == "built"
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    async with client_for(build_server(cfg)) as s:
        # narrowed (user decision): no code, no name, no 'last error' — no error-log step; online only when asked
        r = payload(await s.call_tool("err_lookup", {"query": "ADS error on port 851"}))
        assert r["documented"] is False and r["matches"] == [] and "from_error_log" not in r and "online" not in r, r
        r = payload(await s.call_tool("err_lookup", {"query": "ADS error on port 851", "online": True}))
        assert r["online"]["skipped"].startswith("the docs index"), r
        r = payload(await s.call_tool("err_lookup", {"query": "why does MAIN.fbAxis stop?"}))
        assert r["from_error_log"]["entries"] == [] and "not attached" in r["from_error_log"]["xae_error_list"], r
        res = await s.call_tool("ads_read", {"name": "MAIN.doesNotExist"})
        assert res.is_error and "1808" in text(res), text(res)
        r = payload(await s.call_tool("err_lookup", {"query": "why does reading MAIN.doesNotExist fail?"}))
        e = r["from_error_log"]["entries"][0]
        assert e["source"] == "recent errors of this server" and e["where"].startswith("ads_read "), e
        assert e["matches"][0]["symbol"] == "ADSERR_DEVICE_SYMBOLNOTFOUND" and e["names"] == ["MAIN.doesNotExist"], e
        assert "online" not in r, r
        # a code the local documentation does not have, online asked: labelled (no docs index here: said so)
        r = payload(await s.call_tool("err_lookup", {"query": "SDO abort 0x06020000", "online": True}))
        assert r["documented"] is False and "from_error_log" not in r and "skipped" in r["online"], r


def test_0816_the_online_step_is_a_labelled_site_search(tmp_path):
    """InfosysIndex.web_search: the search engine's results (url, title, snippet), TwinCAT 2 books left out, whether the
    page is in the local package; a search that cannot be reached is said so."""
    from twincat_mcp.infosys import InfosysError, InfosysIndex
    page = ('<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg='
            'https%3A%2F%2Finfosys.beckhoff.com%2Fcontent%2F1033%2Ftc3_ads_intro%2F374277003.html&amp;rut=x">'
            '<b>ADS</b> Return Codes</a><a class="result__snippet" href="x">0x745 Timeout has occurred</a></div>'
            '<div class="result"><a class="result__a" href="https://infosys.beckhoff.com/content/1033/'
            'tcplclibsystem/1.html">TwinCAT 2 page</a></div>')

    class F:
        def __init__(self, data): self.data = data
        def get(self, url, allow_hosts=None):
            if self.data is None:
                raise InfosysError("no network")
            assert "duckduckgo.com" in url and allow_hosts
            return self.data.encode()
    idx = InfosysIndex(tmp_path / "c", fetcher=F(page))
    items = idx.web_search("0x745 timeout", 5)
    assert items[0]["url"] == "https://infosys.beckhoff.com/content/1033/tc3_ads_intro/374277003.html", items
    assert items[0]["title"] == "ADS Return Codes" and items[0]["snippet"] == "0x745 Timeout has occurred", items
    assert len(items) == 1, items                         # tcplclibsystem: a TwinCAT 2 book (tc3_only)
    assert InfosysIndex(tmp_path / "d", fetcher=F(None)).web_search("x")[0]["error"].startswith("the online search")


@pytest.mark.anyio
async def test_rereview_0816_err_lookup_steps(monkeypatch, project_dir, tmp_path):
    """Re-review of 0.8.16: (7) an error inside a result that succeeded (ads_read_many's per-name ADSError 1808) is an
    error seen; (9) an unrelated older error does not keep the online step from running, and an unfound name is said
    as a name; (8) without an errors index, both steps run as well."""
    from tests.fake_ads import install
    from tests.test_errors_index import build as err_build, make_kb
    from twincat_mcp.server import App
    install(monkeypatch)
    kb = make_kb(tmp_path)
    assert err_build(kb)["result"] == "built"
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    async with client_for(build_server(cfg)) as s:
        res = payload(await s.call_tool("ads_read_many", {"names": ["MAIN.doesNotExist"]}))
        assert "1808" in json.dumps(res), res
        r = payload(await s.call_tool("err_lookup", {"query": "why does reading MAIN.doesNotExist fail?"}))
        e = r["from_error_log"]["entries"][0]
        assert e["where"].startswith("ads_read_many ") and e["matches"][0]["symbol"] == "ADSERR_DEVICE_SYMBOLNOTFOUND", e
        r = payload(await s.call_tool("err_lookup", {"query": "E_FAIL", "online": True}))
        assert "online" in r and r["note"].startswith("E_FAIL: no documented code has this name"), r
    monkeypatch.setattr(App, "errors_first_build", lambda self: None)
    cfg2 = mk_cfg(project_dir)
    cfg2.knowledge.location = str(make_kb(tmp_path / "noerr"))          # no errors.db
    cfg2.docs.enabled = False
    async with client_for(build_server(cfg2)) as s:
        r = payload(await s.call_tool("err_lookup", {"query": "why does MAIN.fbAxis fail?", "online": True}))
        assert r["documented"] is None and "from_error_log" in r and "online" in r, r


@pytest.mark.anyio
async def test_rereview_0816_the_online_step_sends_no_project_names_and_gives_up(tmp_path):
    """Re-review of 0.8.16: (privacy) the whole question went to the search engine, project symbols included — now
    (narrowed by the user) only the codes read, message ids, documented code names and COM's error names are sent;
    (13) a search that does not answer gives up after ONLINE_TIMEOUT_S, not 40 s."""
    import time as _t
    from twincat_mcp.server import App
    cfg = mk_cfg(tmp_path)
    cfg.docs.enabled = False
    app = App(cfg)
    # narrowed (user decision): codes, message ids, documented code names and COM's names — nothing else of a question
    assert app.online_terms("why does reading MAIN.doesNotExist fail with E_FAIL?") == "E_FAIL"
    assert app.online_terms("fbAxis.Execute: 0x4221", {"read_as": [{"token": "0x4221"}]}) == "0x4221"
    assert app.online_terms("EL3356 on Givaudan_Line3 behind Doser_Station_3 shows TIMEOUT") == ""

    class SlowDocs:
        def web_search(self, q, limit=5):
            _t.sleep(1.0)
            return []
    app.docs = SlowDocs()
    app.cfg.docs.web_fallback = True
    app.ONLINE_TIMEOUT_S = 0.2
    t0 = _t.time()
    r = await app.err_online("ADS error 0x4221", {"read_as": [{"token": "0x4221"}]})
    assert _t.time() - t0 < 0.9 and r["items"][0]["error"].startswith("the online search did not answer"), r


def test_rereview_0816_web_search_keeps_each_snippet_with_its_result_and_only_tc3_books(tmp_path):
    """Re-review of 0.8.16 (6): a result without a snippet shifted every later snippet; pages of no known book (another
    language, TwinCAT 2's old form) passed."""
    from twincat_mcp.infosys import InfosysIndex

    def res(url, title, snip=None):
        return (f'<div class="result results_links"><a class="result__a" href="{url}">{title}</a>'
                + (f'<a class="result__snippet" href="x">{snip}</a>' if snip else "") + "</div>")
    page = (res("https://infosys.beckhoff.com/content/1033/tc3_ads_intro/1.html", "First")
            + res("https://infosys.beckhoff.com/content/1033/tcplclibsystem/html/x.htm", "TC2 old form", "tc2")
            + res("https://infosys.beckhoff.com/content/1031/tc3_ads_intro/1.html", "German", "de")
            + res("https://infosys.beckhoff.com/content/1033/tc3_ads_intro/374277003.html", "ADS Return Codes",
                  "0x745 Timeout"))

    class F:
        def get(self, url, allow_hosts=None):
            return page.encode()
    items = InfosysIndex(tmp_path / "c", fetcher=F()).web_search("0x745", 5)
    assert [(i["title"], i["snippet"]) for i in items] == [("First", None), ("ADS Return Codes", "0x745 Timeout")], items


@pytest.mark.anyio
async def test_review3_0816_the_error_log_step(monkeypatch, project_dir, tmp_path):
    """Third review of 0.8.16: (2) an error inside a result is labelled as such and in its time; the plugin's own tools
    (a docs search) are not an error source; (5) 'main' alone does not make an error the answer, and 'the last error'
    is the newest; (4) online_terms sends no name of the question but COM's error names (narrowed by the user)."""
    import time as _t
    from tests.fake_ads import install
    from tests.test_errors_index import build as err_build, make_kb
    from twincat_mcp.server import App
    install(monkeypatch)
    kb = make_kb(tmp_path)
    assert err_build(kb)["result"] == "built"
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    app = App(cfg)
    from twincat_mcp.server import ToolError, _tool_boundary
    seen: list[tuple] = []

    async def infosys_search():
        raise ToolError("web fallback failed: getaddrinfo failed (WSAHOST_NOT_FOUND)")

    async def ads_read():
        raise ToolError("ads_read MAIN.x: ADSError 1808")
    for fn in (infosys_search, ads_read):
        with pytest.raises(ToolError):
            await _tool_boundary(fn, note_error=lambda: (lambda *a, **k: seen.append((a, k))))()
    assert [a[0] for a, _k in seen] == ["ads_read"], seen                 # the plugin's own tools are no source
    app.note_error("ads_write", error="ads_write MAIN.nCounter: ADSError 1808 symbol not found")
    _t.sleep(1.1)
    app.note_error("ads_read_many", result_errors=["MAIN.fbAxis: ADSError 1861 timeout"])
    r = await app.err_from_logs("why does MAIN.fbAxis time out?")
    e = r["entries"][0]
    assert e["where"].startswith("ads_read_many ") and e["where"].endswith("(in its result)"), r
    assert all(x["where"].split()[0] != "ads_write" for x in r["entries"]), r     # 'main' alone: no answer
    r = await app.err_from_logs("what does the last error mean?")
    assert len(r["entries"]) == 1 and r["entries"][0]["newest"] and "1861" in r["entries"][0]["text"], r
    assert await app.err_from_logs("why does the axis not move?") is None      # no name, no 'last error': no step
    # narrowed (user decision): online gets the codes read and COM's names, nothing else of the question
    q = "TwinCAT error 0x80004005 in FB_Valve with ST_Recipe at bEstop and E_FAIL on EL3356 behind EK1100"
    assert app.online_terms(q, {"read_as": [{"token": "0x80004005"}]}) == "0x80004005 E_FAIL"


def test_review3_0816_web_search_drops_twincat2_supplements(tmp_path):
    """Third review of 0.8.16 (12): TwinCAT 2 supplement books (ts6100_tc2_…, tcdbserver) passed the online filter."""
    from twincat_mcp.infosys import InfosysIndex
    page = ('<div class="result"><a class="result__a" href="https://infosys.beckhoff.com/content/1033/'
            'ts6100_tc2_opcua_configurator/374277003.html">TC2 OPC UA</a></div>'
            '<div class="result"><a class="result__a" href="https://infosys.beckhoff.com/content/1033/tcdbserver/1.html">'
            'TC2 DB</a></div><div class="result"><a class="result__a" href="https://infosys.beckhoff.com/content/1033/'
            'tf6100_tc3_opcua/1.html">TC3 OPC UA</a></div>')

    class F:
        def get(self, url, allow_hosts=None):
            return page.encode()
    assert [i["title"] for i in InfosysIndex(tmp_path / "c", fetcher=F()).web_search("opc ua", 5)] == ["TC3 OPC UA"]


@pytest.mark.anyio
async def test_review4_0816_error_log_and_online_terms(monkeypatch, project_dir, tmp_path):
    """Fourth review of 0.8.16: (2) an error inside a result names its item ('MAIN.fbValve.bOpen: ADSError 1793'), so
    a question about the item finds it; (8) the XAE Error List has no newest. (7) and (1) are settled by the user's
    narrowing: no word or tool-name matching ('why does activation fail?': no step), 'the last error' said so is the
    newest; no product name of a question goes online."""
    import time as _t
    from tests.test_errors_index import build as err_build, make_kb
    from twincat_mcp.server import App, _result_errors
    assert _result_errors({"results": {"MAIN.fbValve.bOpen": {"error": "ADSError 1793 (0x701)"}}}) == \
        ["MAIN.fbValve.bOpen: ADSError 1793 (0x701)"]
    kb = make_kb(tmp_path)
    assert err_build(kb)["result"] == "built"
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    app = App(cfg)
    app.note_error("ads_read", error="ads_read MAIN.nOld: ADSError 1861 (0x745) timeout")
    _t.sleep(1.1)
    app.note_error("xae_activate_configuration", error="ActivateConfiguration: failed (HRESULT 0x98110745)")
    _t.sleep(1.1)
    app.note_error("ads_write_many", result_errors=_result_errors(
        {"results": {"MAIN.fbValve.bOpen": {"error": "ADSError 1793 (0x701)"}}}))
    r = await app.err_from_logs("why did writing MAIN.fbValve.bOpen fail?")
    assert r["entries"][0]["text"].startswith("MAIN.fbValve.bOpen: ADSError 1793"), r
    # narrowed (user decision): no word matching — a name of the question, or 'the last error' said so
    assert await app.err_from_logs("why does activation fail?") is None
    assert await app.err_from_logs("what does the error mean?") is None
    r = await app.err_from_logs("is the last error a timeout?")
    assert r["entries"][0]["newest"] and "1793" in r["entries"][0]["text"], r
    items = [("XAE Error List (error)", "C0077: Unknown type: 'FB_X'"), ("XAE Error List (error)", "ADS error 1808")]
    assert app._err_log_entries("what does the last error mean?", items, "XAE Error List", False) == []
    assert app.online_terms("EK1100") == "" and app.online_terms("EL3356 shows weight 0 after CX9020 restart") == ""


@pytest.mark.anyio
async def test_review5_0816_narrowed_error_log_and_online(monkeypatch, project_dir, tmp_path):
    """Fifth review of 0.8.16, settled by the user's narrowing ('Narrow A'): (1) the error log is read for a question
    that names a recorded error's variable / name whole (fbValve is not fbValveB) or says 'last error' — not for 'the
    last time'; (2) online only when asked (online=true), and of the codes and documented code names only — no
    project, product or customer name; (4-6) an error inside a list-shaped result names its entry, and an 'error'
    member of a value that was read is data, not an error seen."""
    import time as _t
    from tests.fake_ads import install
    from tests.test_errors_index import build as err_build, make_kb
    from twincat_mcp.server import App, _result_errors
    assert _result_errors({"errors": [{"symbol": "MAIN.fbValve.bOpen", "error": "ADSError 1808"}]}) == \
        ["MAIN.fbValve.bOpen: ADSError 1808"]
    assert _result_errors({"value": {"error": "x", "nState": 3}, "rows": [{"error": "y"}]}) == []
    assert _result_errors({"blocks": [{"name": "rec1", "error": "rec1: disk full"}]}) == ["rec1: disk full"]
    kb = make_kb(tmp_path)
    assert err_build(kb)["result"] == "built"
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    app = App(cfg)
    r = await app.err_from_logs("what does the last error mean?")
    assert r["entries"] == [] and r["note"] == "no error was seen yet", r
    app.note_error("ads_read", error="ads_read MAIN.fbValveB.bOpen: ADSError 1808")
    _t.sleep(1.1)
    app.note_error("ads_write", error="ads_write MAIN.fbValve.bOpen: ADSError 1861 timeout")
    r = await app.err_from_logs("why does fbValveB fail?")
    assert [e["names"] for e in r["entries"]] == [["fbValveB"]] and "1808" in r["entries"][0]["text"], r
    assert await app.err_from_logs("what happened the last time it worked?") is None
    r = await app.err_from_logs("what does the previous ADS error mean?")
    assert r["entries"][0]["newest"] and "1861" in r["entries"][0]["text"], r
    for q in ("E_VALVE_STATE after ERR_DOSING_TIMEOUT in fbTimer", "MC_MoveAbsolute fails on Line3 of AcmeFoods",
              "EtherCAT OP SAFEOP on EL3356"):
        assert app.online_terms(q) == "", q
    assert app.online_terms("ADSERR_DEVICE_SYMBOLNOTFOUND", {"names": ["ADSERR_DEVICE_SYMBOLNOTFOUND"], "matches": [
        {"symbol": "ADSERR_DEVICE_SYMBOLNOTFOUND", "matched_by": "symbol"}]}) == "ADSERR_DEVICE_SYMBOLNOTFOUND"
    assert app.online_terms("x", {"matches": [{"symbol": "ADSERR_DEVICE_SYMBOLNOTFOUND", "matched_by": "value"}]}) == ""
    # online only when asked: no web search for an undocumented code unless online=true
    install(monkeypatch)
    asked: list[str] = []

    async def fake_online(self, query, res=None):
        asked.append(query)
        return {"items": []}
    monkeypatch.setattr(App, "err_online", fake_online)
    async with client_for(build_server(cfg)) as s:
        r = payload(await s.call_tool("err_lookup", {"query": "SDO abort 0x06020000"}))
        assert r["documented"] is False and "online" not in r and asked == [], r
        r = payload(await s.call_tool("err_lookup", {"query": "SDO abort 0x06020000", "online": True}))
        assert r["online"] == {"items": []} and asked == ["SDO abort 0x06020000"], r
