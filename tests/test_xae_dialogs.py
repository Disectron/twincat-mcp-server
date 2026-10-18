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
