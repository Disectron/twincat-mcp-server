"""MCP SDK 2.3 behaviour through its own Client, on both protocol eras: 2026-07-28 (Client's default 'auto' mode —
no server-to-client requests, questions returned as input_required results) and the legacy handshake.

N89: confirmations never reached the user on 2026-07-28 (ctx.elicit raised NoBackChannelError). N90: the refusal
blamed the client. mcp >= 2.1: only a ToolError's text reaches the model."""
from __future__ import annotations

import json
import logging

import pytest
from pydantic import BaseModel
from mcp import Client, types
from mcp.server.mcpserver import Context

from twincat_mcp import prompting as ASK
from twincat_mcp.server import TcMcpServer, build_server
from tests.fake_ads import install
from tests.test_server_e2e import mk_cfg, payload, text


class Answers:
    """An elicitation callback that answers from a list and records what it was asked."""

    def __init__(self, *answers: types.ElicitResult) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    async def __call__(self, context, params: types.ElicitRequestParams) -> types.ElicitResult:
        self.asked.append(params.message)
        return self.answers.pop(0)


ACCEPT = types.ElicitResult(action="accept", content={"confirm": True})


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_n89_a_confirmation_prompt_reaches_the_user_on_both_protocol_eras(monkeypatch, project_dir, mode):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    cb = Answers(ACCEPT)
    async with Client(srv, mode=mode, elicitation_callback=cb) as c:
        proto = c.protocol_version
        res = await c.call_tool("ads_plc_state", {"action": "stop"})
        assert not res.is_error, text(res)
        assert payload(res)["after"]["ads_state_name"] == "STOP"
        assert len(cb.asked) == 1 and cb.asked[0].startswith("MACHINE CONTROL: ads_plc_state")
        tail = payload(await c.call_tool("tc_audit_tail", {"n": 10}))
    assert (proto >= "2026-07-28") == (mode == "auto")
    outcomes = [e.get("outcome") for e in (tail if isinstance(tail, list) else tail.get("entries", []))]
    assert "human-confirmed-elicitation" in outcomes and outcomes[-1] == "ok", outcomes
    if mode == "auto":
        assert "asked" in outcomes                     # round 1 returned the question (input_required)


@pytest.mark.anyio
async def test_n89_a_declined_prompt_does_nothing(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    cb = Answers(types.ElicitResult(action="decline"))
    async with Client(srv, elicitation_callback=cb) as c:
        res = await c.call_tool("ads_plc_state", {"action": "stop"})
    assert res.is_error and "declined by the user" in text(res)
    from pyads import constants as PC
    from tests.fake_ads import FakeConnection
    assert FakeConnection.shared_state.get(851, (PC.ADSSTATE_RUN, 0))[0] == PC.ADSSTATE_RUN    # nothing was sent
    assert len(cb.asked) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_n90_without_elicitation_the_refusal_says_what_blocks_the_prompt(monkeypatch, project_dir, mode):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    async with Client(srv, mode=mode) as c:                 # no callback: the client declares no elicitation
        res = await c.call_tool("ads_plc_state", {"action": "stop"})
        doc = payload(await c.call_tool("tc_doctor", {}))
    t = text(res)
    assert res.is_error and "did not declare the elicitation capability" in t and "tc_request_confirmation" in t, t
    assert "this client does not support" not in t and "NoBackChannel" not in t
    assert doc["elicitation"]["works"] is False and doc["elicitation"]["client_declared_elicitation"] is False
    assert "confirmation prompts cannot reach the user" in doc["summary"]


@pytest.mark.anyio
@pytest.mark.parametrize("mode,route", [("auto", "input_required"), ("legacy", "server_request")])
async def test_tc_doctor_reports_how_a_prompt_travels(monkeypatch, project_dir, mode, route):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with Client(srv, mode=mode, elicitation_callback=Answers()) as c:
        doc = payload(await c.call_tool("tc_doctor", {}))
    el = doc["elicitation"]
    assert el["works"] is True and el["route"] == route and el["client_declared_elicitation"] is True, el


# ------------------------------------------------------------------ the tool boundary and the question flow
class Pick(BaseModel):
    ok: bool


def _mini() -> TcMcpServer:
    srv = TcMcpServer("mini", subscriptions=False)

    @srv.tool()
    async def two_questions(ctx: Context) -> dict:
        a, _ = await ASK.ask(ctx, "first question", Pick)
        b, _ = await ASK.ask(ctx, "second question", Pick)
        return {"a": a.data.ok, "b": b.data.ok}

    @srv.tool()
    async def raises_value() -> dict:
        raise ValueError("STRING(5) holds 5 characters, 11 given")

    @srv.tool()
    async def raises_os() -> dict:
        raise PermissionError(13, "Permission denied", "C:\\Windows\\x.csv")

    @srv.tool()
    async def crashes() -> dict:
        return {}["missing"]

    return srv


@pytest.mark.anyio
async def test_two_questions_in_one_call_are_each_asked_once():
    cb = Answers(types.ElicitResult(action="accept", content={"ok": True}),
                 types.ElicitResult(action="accept", content={"ok": False}))
    async with Client(_mini(), elicitation_callback=cb) as c:
        res = await c.call_tool("two_questions", {})
    assert not res.is_error, text(res)
    assert payload(res) == {"a": True, "b": False} and cb.asked == ["first question", "second question"]


@pytest.mark.anyio
async def test_errors_keep_their_text_and_a_defect_is_logged(caplog):
    async with Client(_mini()) as c:
        r1 = await c.call_tool("raises_value", {})
        r2 = await c.call_tool("raises_os", {})
        with caplog.at_level(logging.ERROR, logger="twincat_mcp.server"):
            r3 = await c.call_tool("crashes", {})
    assert r1.is_error and "STRING(5) holds 5 characters" in text(r1)
    assert r2.is_error and "Permission denied" in text(r2) and "x.csv" in text(r2)
    t3 = text(r3)
    assert r3.is_error and "internal error in crashes (KeyError: 'missing')" in t3 and "crash " in t3, t3
    cid = t3.rsplit("crash ", 1)[1].strip()
    assert any(cid in rec.getMessage() and rec.exc_info for rec in caplog.records)


@pytest.mark.anyio
async def test_nothing_is_advertised_that_is_never_published(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with Client(srv) as c:
        caps = c.server_capabilities
    res = getattr(caps, "resources", None)
    assert res is None or not getattr(res, "subscribe", False)
    tools = getattr(caps, "tools", None)
    assert tools is None or not getattr(tools, "list_changed", False)


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["local", "web"])
async def test_r30_infosys_tools_describe_the_configured_source(monkeypatch, project_dir, source):
    install(monkeypatch)
    cfg = mk_cfg(project_dir)
    cfg.docs.source = source
    srv = build_server(cfg)
    async with Client(srv) as c:
        desc = {t.name: t.description for t in (await c.list_tools()).tools if t.name.startswith("infosys_")}
    text = " ".join(desc.values())
    if source == "local":
        assert "DuckDuckGo" not in text and "fetched live" not in text.lower() and "on first use" not in text
        assert "local Help Viewer" in desc["infosys_search"] and "changes nothing" in desc["infosys_index"]
    else:
        assert "DuckDuckGo" in desc["infosys_search"] and "fetched live" in desc["infosys_get_page"]


@pytest.mark.anyio
async def test_n94_a_confirmation_without_the_tick_says_so(monkeypatch, project_dir):
    """N94 (0.8.11 retest): a prompt submitted with the confirm box not ticked came back 'declined by the user
    (accept)' — a correct refusal that read as a contradiction."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    cb = Answers(types.ElicitResult(action="accept", content={"confirm": False}))
    async with Client(srv, elicitation_callback=cb) as c:
        res = await c.call_tool("ads_plc_state", {"action": "stop"})
        tail = payload(await c.call_tool("tc_audit_tail", {"n": 5}))
    t = text(res)
    assert res.is_error and "confirm box not ticked" in t and "declined by the user (accept)" not in t, t
    outcomes = [e.get("outcome") for e in (tail if isinstance(tail, list) else tail.get("entries", []))]
    assert "declined-elicitation:accept-unticked" in outcomes, outcomes


@pytest.mark.anyio
async def test_n96_kb_rebuild_checks_before_it_asks(monkeypatch, project_dir, tmp_path):
    """N96 (0.8.11 retest): kb_rebuild(index='manuals') asked for the confirmation, then failed: this server's Python
    has no PDF library. Everything that can stop the rebuild is checked first; nothing is asked when it cannot run."""
    import importlib.util as iu
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded")
    kb = tmp_path / "kb"
    (kb / "manuals").mkdir(parents=True)
    (kb / "index").mkdir()
    cfg.knowledge.location = str(kb)
    srv = build_server(cfg)
    real = iu.find_spec
    monkeypatch.setattr(iu, "find_spec", lambda name, *a, **k: None if name in ("pymupdf", "fitz", "pypdf")
                        else real(name, *a, **k))
    cb = Answers(ACCEPT)
    async with Client(srv, elicitation_callback=cb) as c:
        res = await c.call_tool("kb_rebuild", {"index": "manuals"})
    r = payload(res)
    assert r["started"] is False and "no PDF library" in r["error"] and cb.asked == [], (r, cb.asked)
