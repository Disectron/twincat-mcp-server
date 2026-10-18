"""N85 / N86 / N87: modal dialogs of the XAE shell around Automation Interface calls (dialog enumeration faked)."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from twincat_mcp import dialogs as D
from twincat_mcp.server import App, ToolError
from tests.test_server_e2e import mk_cfg


def test_n86_only_push_buttons_answer_a_dialog():
    """N86: every child of class Button was a 'button' — xae_dialog_resolve(button='Show status bar') would have
    toggled an IDE setting. BS_TYPEMASK decides: push buttons answer, check boxes / radio buttons are options."""
    assert [D.button_kind(s) for s in (0x0, 0x1, 0x50010001, 0xB, 0xE)] == ["push"] * 5
    assert [D.button_kind(s) for s in (0x2, 0x3, 0x50010003, 0x6, 0x9)] == ["option"] * 5
    assert D.button_kind(0x7) == "other"                                    # group box
    d = D.Dialog(1, 2, "Options", "", [(3, "OK"), (4, "Cancel")], ["Show status bar"])
    assert d.to_dict()["buttons"] == ["OK", "Cancel"] and d.to_dict()["options"] == ["Show status bar"]
    assert d.awaits_answer() and not D.Dialog(5, 2, "Building", "", [(6, "Cancel")]).awaits_answer()


@pytest.mark.anyio
async def test_n85_n87_a_dialog_holds_the_shell(monkeypatch, project_dir):
    """N85: a dialog the running call opened gave no answer until the tool timeout, and the timeout did not free
    the worker. Now it is reported within ~2 s, the shell is marked held (every XAE call refused at once, naming the
    dialog), and the held call's own result is listed once the dialog is answered. N87: a dialog already open is
    named in every result, and nothing that changes the machine runs while it waits."""
    app = App(mk_cfg(project_dir))
    dialogs: list[D.Dialog] = []
    monkeypatch.setattr(D, "list_dialogs", lambda pid: list(dialogs))
    release = threading.Event()

    def opens_dialog():
        dialogs.append(D.Dialog(77, 1, "Options", "Environment", [(1, "OK"), (2, "Cancel")], ["Show status bar"]))
        release.wait(20)
        return {"done": True}
    t0 = time.monotonic()
    with pytest.raises(ToolError, match=r"opens_dialog: BLOCKED BY DIALOG 'Options'.*blocked_by_dialog: \{\"hwnd\": 77"):
        await app.xae_call(opens_dialog, timeout=60)
    assert 2.0 <= time.monotonic() - t0 < 6.0
    with pytest.raises(ToolError, match=r"NOT started: XAE is held by the dialog 'Options' that opens_dialog opened"):
        await app.xae_call(lambda: {"x": 1}, timeout=5)
    dialogs.clear()
    release.set()
    for _ in range(100):
        if app.xae_held is None:
            break
        await asyncio.sleep(0.02)
    assert app.xae_held is None and app.xae_late[-1]["outcome"] == "ok" and app.xae_late[-1]["result"] == {"done": True}
    # N87: an open dialog does not stop a call (the shell answers COM inside its modal loop) — it is said in the result
    dialogs.append(D.Dialog(78, 1, "Tools", "Restart?", [(1, "Yes"), (2, "No")]))
    r = await app.xae_call(lambda: {"y": 1}, timeout=5)
    assert r["y"] == 1 and r["open_dialogs"][0]["title"] == "Tools"
    with pytest.raises(ToolError, match="xae_restart_twincat refused: the XAE shell has a dialog open — 'Tools'"):
        app.refuse_if_dialog("xae_restart_twincat")


@pytest.mark.anyio
async def test_review_a_deploy_stops_before_the_next_step_when_a_dialog_waits(monkeypatch, project_dir):
    """Review of N87: xae_deploy checked for a dialog only at its start; a dialog the build left open did not stop the
    activation. Now each step waits 2 s for the watchdog's rules, then stops with what was done."""
    from twincat_mcp.server import build_server
    from tests.fake_ads import install
    from tests.test_server_e2e import client_for, text
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    app = srv._tcmcp_app
    dialogs: list[D.Dialog] = []
    monkeypatch.setattr(app, "dialogs_now", lambda: [d.to_dict() for d in dialogs])
    ran: list[str] = []

    async def ok(*a, **kw):
        return "PLC@192.168.0.10.1.1"

    async def nothing(*a, **kw):
        return None

    async def saved(tool):
        return {"saved": True}

    async def xae_call(fn, *a, **kw):
        ran.append(fn.__name__)
        if fn.__name__ == "build":
            dialogs.append(D.Dialog(90, 1, "TwinCAT", "Overwrite the boot project?", [(1, "Yes"), (2, "No")]))
            return {"success": True, "errors": 0, "warnings": 0, "duration_s": 1.0}
        return {}
    for name, fn in (("xae_target_for", ok), ("check_targets", nothing), ("permit", nothing),
                     ("save_verified", saved), ("xae_call", xae_call)):
        monkeypatch.setattr(app, name, fn)
    t0 = time.monotonic()
    async with client_for(srv) as s:
        res = await s.call_tool("xae_deploy", {"confirm_token": "x"})
    t = text(res)
    assert res.is_error and "xae_deploy before activation refused" in t and "'TwinCAT'" in t, t
    assert "Done: save, build; nothing after that was started" in t and ran == ["build"], (t, ran)
    assert time.monotonic() - t0 >= 2.0                                       # the rules had their 2 s
    # a progress window (Cancel only) waits for no answer: not a reason to stop
    dialogs[:] = [D.Dialog(91, 1, "Building", "", [(1, "Cancel")])]
    ran.clear()

    async def xae_call2(fn, *a, **kw):
        ran.append(fn.__name__)
        return {"success": True, "errors": 0, "warnings": 0, "duration_s": 1.0} if fn.__name__ == "build" else {}
    monkeypatch.setattr(app, "xae_call", xae_call2)
    monkeypatch.setattr(app, "restart_witness", lambda *a, **kw: _value({"mode": "RUN"}))
    monkeypatch.setattr(app, "verify_target_after", lambda *a, **kw: _value(
        {"verified": True, "target_mode": "RUN", "dialogs": [], "polls": 1, "modes_seen": ["CONFIG", "RUN"]}))
    async with client_for(srv) as s:
        r = await s.call_tool("xae_deploy", {"confirm_token": "x"})
    assert not r.is_error, text(r)
    assert ran == ["build", "activate_configuration", "restart_twincat"], ran


async def _value(v):
    return v


@pytest.mark.anyio
async def test_n93_a_run_to_run_restart_compares_the_application_it_read(monkeypatch, project_dir):
    """N93 (0.8.11 retest): xae_restart_twincat RUN -> RUN read the compile time before the restart, but compared with
    the witness of an earlier activation made in CONFIG (no compile time): 'nothing to compare'."""
    from twincat_mcp.server import build_server
    from tests.fake_ads import install
    from tests.test_server_e2e import client_for, payload
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    app = srv._tcmcp_app
    monkeypatch.setattr(app, "dialogs_now", lambda: [])

    async def ok(*a, **kw):
        return "PLC@192.168.0.10.1.1"

    async def nothing(*a, **kw):
        return None

    async def xae_call(fn, *a, **kw):
        return {}
    witnesses = iter([{"mode": "RUN", "app_timestamp": 1792380152}])
    monkeypatch.setattr(app, "xae_target_for", ok)
    monkeypatch.setattr(app, "permit", nothing)
    monkeypatch.setattr(app, "xae_call", xae_call)
    monkeypatch.setattr(app, "restart_witness", lambda *a, **kw: _value(next(witnesses)))
    monkeypatch.setattr(app, "verify_target_after", lambda *a, **kw: _value(
        {"verified": True, "target_mode": "RUN", "dialogs": [], "polls": 2, "modes_seen": ["RUN", "CONFIG", "RUN"],
         "witness_after": {"mode": "RUN", "app_timestamp": 1792380999}}))
    app.last_activation = {"mono": time.monotonic(), "witness": {"mode": "CONFIG", "app_timestamp": None}}
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_restart_twincat", {"confirm_token": "x"}))
    pa = r["plc_application"]
    assert pa["changed"] is True and pa["compiled_before"] is not None and pa["compared_with"] == "before this restart", pa
