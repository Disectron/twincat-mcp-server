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
    # N178: each question ends with its time limit (input_required rounds)
    assert payload(res) == {"a": True, "b": False} and [a.split("\n\n(Answer within")[0] for a in cb.asked] == \
        ["first question", "second question"], cb.asked


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


class _ComError(Exception):
    """What pywintypes.com_error carries: args (hresult, text, excepinfo, argerror) and the same as attributes."""

    def __init__(self, hresult, text, excepinfo=None):
        super().__init__(hresult, text, excepinfo, None)
        self.hresult, self.strerror, self.excepinfo = hresult, text, excepinfo


@pytest.mark.anyio
async def test_n174_a_rejected_com_call_is_busy_and_a_defect_is_in_server_log(monkeypatch, project_dir, tmp_path):
    """N174 (0.8.14 retest): xae_build / xae_deploy failed with "internal error … com_error (-2147418111, 'Call was
    rejected by callee.') — a defect in twincat-mcp … crash 4ee44bfb", and that traceback was in no log the user could
    find. B2 at the tool boundary: a rejected COM call (RPC_E_CALL_REJECTED / RETRYLATER) that reaches it is "XAE is
    busy … did not run this call — repeat it" with what is open in the shell, logged at WARNING with its id; another
    COM error stays an internal error naming the HRESULT in hex and the COM source; B5: the internal-error message names
    ~/.twincat_mcp/logs/server.log and the crash id is in that file with its traceback."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    srv.dialog_hint = lambda: " Open dialog(s): 'About Microsoft Visual Studio' [buttons ['OK']] — answer it."

    @srv.tool()
    async def rejected() -> dict:
        raise _ComError(-2147418111, "Call was rejected by callee.")

    @srv.tool()
    async def retry_later() -> dict:
        raise _ComError(-2147417846, "The message filter indicated that the application is busy.")

    @srv.tool()
    async def com_fails() -> dict:
        raise _ComError(-2147467259, "Unspecified error", (0, "TcXaeShell", "the project file is locked", None, 0,
                                                           -2147467259))

    @srv.tool()
    async def crashes() -> dict:
        return {}["missing"]

    async with Client(srv) as c:
        r1 = await c.call_tool("rejected", {})
        r1b = await c.call_tool("retry_later", {})
        r2 = await c.call_tool("com_fails", {})
        r3 = await c.call_tool("crashes", {})
    t1, t1b, t2, t3 = text(r1), text(r1b), text(r2), text(r3)
    assert r1.is_error and ("rejected: XAE is busy (call rejected, 0x80010001) and did not run this call "
                                         "— repeat it; if it keeps happening, a modal window or a long build holds the "
                                         "shell (xae_dialogs)") in t1, t1
    assert "internal error" not in t1 and "'About Microsoft Visual Studio'" in t1, t1
    assert "retry_later: XAE is busy (call rejected, 0x8001010A)" in t1b and "internal error" not in t1b, t1b
    assert r2.is_error and ("internal error in com_fails (_ComError 0x80004005 'Unspecified error', source "
                            "'TcXaeShell': the project file is locked)") in t2, t2
    server_log = tmp_path / "home" / "logs" / "server.log"
    assert f"traceback: {server_log}, crash " in t3 and f"traceback: {server_log}, crash " in t2, t3
    body = server_log.read_text(encoding="utf-8")
    cid = t3.rsplit("crash ", 1)[1].strip()
    after = body.split(f"crash {cid}", 1)[1]
    assert "Traceback (most recent call last)" in after and "KeyError: 'missing'" in after, body[-2000:]
    bid = t1.rsplit("(logged as ", 1)[1].rstrip(")")
    assert any("WARNING" in ln and f"crash {bid}" in ln for ln in body.splitlines()), body[-2000:]


@pytest.mark.anyio
async def test_n174_a_shell_that_stays_busy_is_said_through_xae_call(monkeypatch, project_dir):
    """N174 B2 at the tool level: a call the shell rejects for all of com_retry_s ends as "XAE is busy (call rejected,
    0x80010001) and did not run this call" with what is open in the shell — not an internal error, not a raw
    com_error text."""
    from mcp.server.mcpserver.exceptions import ToolError
    from twincat_mcp.server import App
    install(monkeypatch)
    app = App(mk_cfg(project_dir))
    app.xae.cfg.com_retry_s = 0.6

    def build():
        return app.xae._retry(lambda: (_ for _ in ()).throw(_ComError(-2147418111, "Call was rejected by callee.")),
                              "SolutionBuild.Build")
    with pytest.raises(ToolError) as ei:
        await app.xae_call(build, timeout=30)
    msg = str(ei.value)
    assert msg.startswith("SolutionBuild.Build: XAE is busy (call rejected, 0x80010001) and did not run this call — it "
                          "was asked again for 0.6 s; repeat it"), msg
    assert "internal error" not in msg and "No dialog is visible in the shell" in msg, msg


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
async def test_n169_the_ranges_are_in_the_tool_schemas(monkeypatch, project_dir):
    """N169 (S4, 0.8.14 retest): ranges were invisible until a call was refused. The schemas carry them: max_chars
    200-60000 (refused outside, with the way to read on), offset >= 0, a search's limit >= 1 (above its maximum it is
    used as the maximum, N167 — so no maximum in the schema, the description says it)."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with Client(srv) as c:
        tools = {t.name: t.input_schema["properties"] for t in (await c.list_tools()).tools}
    page = tools["infosys_get_page"]
    assert (page["max_chars"]["minimum"], page["max_chars"]["maximum"]) == (200, 60000), page["max_chars"]
    assert page["offset"]["minimum"] == 0 and "section" in page
    for name, hi in (("infosys_search", "50"), ("hw_search", "100")):
        lim = tools[name]["limit"]
        assert lim["minimum"] == 1 and "maximum" not in lim and f"1-{hi}" in lim["description"], (name, lim)
        assert tools[name]["offset"]["minimum"] == 0


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


@pytest.mark.anyio
async def test_n178_an_expired_confirmation_round_is_said_logged_and_audited(monkeypatch, project_dir, tmp_path):
    """N178 (0.8.15 retest, S4): a confirmation answered ~18 min after it was asked got the SDK's fixed 'Invalid or
    expired requestState'; the audit ended at 'asked' and server.log had nothing (the SDK logs on its own logger).
    The question states the limit; the rejection is audited ('expired') and goes to server.log."""
    import anyio
    from twincat_mcp import server as SRV
    install(monkeypatch)
    monkeypatch.setattr(ASK, "ROUND_TTL_S", 1.0)
    cfg = mk_cfg(project_dir, mode="guarded")
    srv = build_server(cfg)
    log = SRV.install_server_log(tmp_path / "logs" / "server.log")

    class Late(Answers):
        async def __call__(self, context, params):
            self.asked.append(params.message)
            await anyio.sleep(2.5)
            return ACCEPT
    cb = Late()
    async with Client(srv, mode="auto", elicitation_callback=cb) as c:
        try:
            res = await c.call_tool("ads_plc_state", {"action": "stop"})
            failed = res.is_error
        except Exception:  # noqa: BLE001 — the SDK refuses the round as a protocol error
            failed = True
        tail = payload(await c.call_tool("tc_audit_tail", {"n": 10}))
    assert failed and cb.asked and "Answer within" in cb.asked[0], cb.asked
    entries = tail if isinstance(tail, list) else tail.get("entries", [])
    assert any(e.get("outcome") == "expired" for e in entries), entries
    for h in logging.getLogger("twincat_mcp").handlers:
        h.flush()
    assert log is not None and "requestState rejected" in log.read_text(encoding="utf-8")
    from pyads import constants as PC
    from tests.fake_ads import FakeConnection
    assert FakeConnection.shared_state.get(851, (PC.ADSSTATE_RUN, 0))[0] == PC.ADSSTATE_RUN    # nothing was done
