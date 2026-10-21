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
