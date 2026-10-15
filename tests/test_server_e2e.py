"""End-to-end through a real MCP client session on in-memory streams."""
import json
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
