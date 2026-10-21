"""Round 2 (the last) of the in-depth review of 0.8.18: its MEDIUM findings as tests (xae_links' are in
test_xae_backend.py)."""
from __future__ import annotations

import anyio
import pytest

from tests.fake_ads import install
from tests.test_scope_xae import fake_xae  # noqa: F401  (fixture)
from tests.test_server_e2e import client_for, mk_cfg, payload, text
from tests.test_xae_backend import ComError, _MsgSysMan, _xae_with
from twincat_mcp import xae_backend as X
from twincat_mcp.server import build_server

REC = {"symbols": ["MAIN.nCycle"], "mode": "change"}


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["start", "new"])
async def test_r2_a_scope_recording_of_its_own_cancelled_during_its_start_check_keeps_scope(
        monkeypatch, project_dir, fake_xae, tmp_path, action):
    """scope_record start / new cancelled during the (up to 2 s) start check: StartRecord had run, Scope recorded with
    no owner — the next rec_start(also_scope) sent StartRecord to it and its part saved and signed the user's run."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="full", allow_elicitation=False)
    log = tmp_path / "TE130X" / "TcScopeView.log"
    log.parent.mkdir()
    log.write_text("(2026-10-21-22:40:00:000)[ INFO ] Scope View: start\n", encoding="utf-8")
    cfg.scope.view_log = str(log)                  # readable; StartRecord writes no line: the check waits 2 s
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        args = {"action": action}
        if action == "new":                        # discarding what Scope holds (nothing saved) needs the user's ok
            tok = payload(await s.call_tool("tc_request_confirmation", {"action": "scope_record",
                                                                        "target": "disconnect:"}))
            args["confirm_token"] = tok["confirm_token"]
        with anyio.move_on_after(1.0):
            await s.call_tool("scope_record", args)
        await anyio.sleep(0.2)
        assert app.scope_standalone is not None and fake_xae.recording, (app.scope_standalone, fake_xae.calls)
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
    assert r.is_error and "Scope records on its own" in text(r) and "was not started" in text(r), text(r)
    assert fake_xae.calls.count("start") == 1 and app.rec.recs == {}, fake_xae.calls


@pytest.mark.anyio
async def test_r2_messages_read_after_a_failed_before_read_keep_their_mark_in_a_later_steps_error(monkeypatch,
                                                                                                 project_dir):
    """xae_deploy: the activation's before-read was rejected (its messages may be an earlier call's — marked), then the
    restart failed: the error listed the activation's messages as this call's earlier steps, without the mark."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    app = srv._tcmcp_app
    app.dialogs_now = lambda: []
    be, _dte, sm = _xae_with(monkeypatch, _MsgSysMan(clears=True))
    monkeypatch.setattr(be, "_modal_dialogs", lambda: [])
    be.MESSAGES_READ_S = 0.3
    be.cfg.com_retry_s = 3.0
    app.xae = be

    async def nothing(*a, **kw):
        return None

    async def tgt(*a, **kw):
        return "192.168.1.5.1.1"

    async def saved(*a, **kw):
        return {"saved": True}

    async def witness(*a, **kw):
        return {"app_timestamp": None}

    async def verify(*a, **kw):
        return {"verified": True, "target_mode": "RUN", "dialogs": [], "polls": 3, "modes_seen": ["CONFIG"], "hint": None}
    for k, v in {"permit": nothing, "xae_target_for": tgt, "check_targets": nothing, "save_verified": saved,
                 "restart_witness": witness, "verify_target_after": verify}.items():
        monkeypatch.setattr(app, k, v)
    sm.buffer = "Error: Init4\\Activate Configuration: ADS error 0x745 (1861) -- an EARLIER call\n"

    def fail():
        raise ComError(-2147467259, "Unspecified error")
    sm.StartRestartTwinCAT = fail
    orig, n = sm.GetLastErrorMessages, {"n": 0}

    def read():
        n["n"] += 1
        if n["n"] <= 3:                            # the read before the activation gives up (busy)
            raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
        return orig()
    sm.GetLastErrorMessages = read
    async with client_for(srv) as s:
        r = await s.call_tool("xae_deploy", {"skip_build": True})
    t = text(r)
    assert r.is_error and "an EARLIER call" in t and "may include earlier calls'" in t, t


@pytest.mark.anyio
async def test_r2_the_answer_new_holds_scope_between_its_disconnect_and_its_start(monkeypatch, project_dir, fake_xae):
    """Reviewer A, N2: 'new' after a recording took no claim between its Disconnect and its StartRecord — a rec_start
    with Scope sent meanwhile took Scope too (two owners, B's link signed the 'new' run's capture)."""
    import time
    from mcp import types as _types
    install(monkeypatch)

    def slow_disconnect():
        fake_xae.calls.append("disconnect")
        time.sleep(1.0)
        fake_xae.recording = False
        return 0
    monkeypatch.setattr(fake_xae, "Disconnect", slow_disconnect)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app

    async def elicit(context, params):
        if "next" in (params.requested_schema or {}).get("properties", {}):
            return _types.ElicitResult(action="accept", content={"next": "new"})
        return _types.ElicitResult(action="decline")
    outs: dict = {}
    async with client_for(srv, elicitation_callback=elicit) as s:
        a = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]

        async def stop_a():
            outs["a"] = payload(await s.call_tool("rec_stop", {"id": a}))

        async def start_b():
            while "disconnect" not in fake_xae.calls:
                await anyio.sleep(0.02)
            outs["b"] = await s.call_tool("rec_start", {**REC, "also_scope": True, "scope_disconnect": True})
        async with anyio.create_task_group() as tg:
            tg.start_soon(stop_a)
            tg.start_soon(start_b)
    b = outs["b"]
    assert b.is_error and "being started by another call (the choice after recording" in text(b), text(b)
    assert outs["a"]["scope_next"]["started"] and fake_xae.calls.count("disconnect") == 1, fake_xae.calls
    assert list(app.scope_parts) == [a] and app.scope_standalone is not None


@pytest.mark.anyio
async def test_r2_a_scope_recording_of_its_own_ends_what_scope_left_said(monkeypatch, project_dir, fake_xae):
    """Reviewer A, N3: after recording A (saved), Scope reset to Config (a TwinCAT restart, Disconnect in XAE) and
    scope_record start — scope_left still said A's saved data: scope_record(disconnect) discarded the unsaved run of its
    own without a confirmation."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        a = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        await s.call_tool("rec_stop", {"id": a})
        assert app.scope_left["saved"]
        payload(await s.call_tool("scope_record", {"action": "start"}))
        assert app.scope_left is None
        n0 = len(fake_xae.calls)
        r = await s.call_tool("scope_record", {"action": "disconnect"})       # guarded, no confirm_token
    assert r.is_error and "needs confirmation" in text(r) and fake_xae.calls[n0:] == [], text(r)


@pytest.mark.anyio
async def test_r2_a_dialog_left_open_after_start_record_is_said_again(monkeypatch, project_dir, fake_xae):
    """Reviewer A, N4 (regression against 0.8.17): StartRecord's job returned a tuple — xae_call adds open_dialogs only
    to a dict, so a dialog the shell showed after StartRecord was no longer said."""
    from twincat_mcp import dialogs as D
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    st = {"dlg": False}
    dlg = D.Dialog(90, 1, "TwinCAT Scope", "Target not reachable — continue?", [(1, "Yes"), (2, "No")]).to_dict()
    monkeypatch.setattr(app, "dialogs_now", lambda: [dlg] if st["dlg"] else [])
    orig = fake_xae.StartRecord

    def start():
        st["dlg"] = True
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))
        st["dlg"] = False
        await s.call_tool("rec_stop", {"id": r["id"]})
    assert "open_dialogs" in r["scope"] and "_log_mark" not in r["scope"], r["scope"]


@pytest.mark.anyio
async def test_r2_scope_record_stop_with_nothing_recording_leaves_no_question(monkeypatch, project_dir, fake_xae):
    """Reviewer A, N6: scope_record stop with nothing the plugin knew of recording set 'Scope holds a recording of its
    own, NOT saved' — the next rec_start asked to discard data that may not exist. And N5: the refusal under a Scope
    recording of its own names disconnect as the way out when StopRecord fails."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        payload(await s.call_tool("scope_record", {"action": "stop"}))
        assert app.scope_left is None
        payload(await s.call_tool("scope_record", {"action": "start"}))
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
        assert r.is_error and "scope_record(action='disconnect') frees it" in text(r), text(r)
        payload(await s.call_tool("scope_record", {"action": "stop"}))
        assert app.scope_left is not None and app.scope_left["rid"] is None      # that run's data: asked about next
