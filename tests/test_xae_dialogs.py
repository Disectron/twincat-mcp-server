"""N85 / N86 / N87 / N173: modal dialogs of the XAE shell around Automation Interface calls (dialog enumeration faked)."""
from __future__ import annotations

import asyncio
import os
import sys
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


# ---------------------------------------------------------------------------------------------------------------------
# N173 (0.8.14 retest, S2): a modal window that is not a #32770 dialog (VS's About box, the Options window — WPF
# HwndWrapper[…] or WinForms) was never seen: dialogs.py kept only top-level #32770 windows, so xae_command Help.About
# hung 120 s and ended as a timeout, and xae_dialogs listed nothing. The window tree is faked below (EnumWindows,
# GetWindow(GW_OWNER), IsWindowEnabled, …) and UI Automation is a stand-in: what a Windows desktop reports.
# ---------------------------------------------------------------------------------------------------------------------
WPF = "HwndWrapper[DefaultDomain;;6f6f2c5e-0a4c-4f3c-9a39-7c1d5c0f3a11]"
WINFORMS = "WindowsForms10.Window.8.app.0.141b42a_r6_ad1"


class _Desk:
    """Top-level windows (and the child controls of #32770 dialogs) as Windows would report them."""

    def __init__(self) -> None:
        self.w: dict[int, dict] = {}
        self.posted: list[tuple[int, int]] = []
        self.lap: dict[int, int] = {}              # GetLastActivePopup(owner) → popup
        self.invoked: list[tuple[int, str]] = []

    def top(self, hwnd, cls, title="", pid=100, owner=0, enabled=True, visible=True, ex=0):
        self.w[hwnd] = {"cls": cls, "title": title, "pid": pid, "owner": owner, "enabled": enabled, "visible": visible,
                        "ex": ex, "style": 0, "kids": [], "top": True}
        return hwnd

    def child(self, parent, hwnd, cls, text, style=0):
        self.w[hwnd] = {"cls": cls, "title": text, "pid": self.w[parent]["pid"], "owner": 0, "enabled": True,
                        "visible": True, "ex": 0, "style": style, "kids": [], "top": False}
        self.w[parent]["kids"].append(hwnd)

    def install(self, monkeypatch, uia=None):
        """Point dialogs.py at this desktop: win32gui / win32process / win32con, GetLastActivePopup and UI Automation
        (uia: {hwnd: (buttons, texts)}; a window not in it cannot be read through UI Automation)."""
        import types
        w = self.w

        def enum_windows(cb, extra):
            for h in [h for h in w if w[h]["top"]]:
                if cb(h, extra) is False:
                    break

        def enum_children(h, cb, extra):
            for c in list(w[h]["kids"]):
                cb(c, extra)

        def get_long(h, idx):
            return w[h]["ex"] if idx == -20 else w[h]["style"]
        g = types.SimpleNamespace(
            EnumWindows=enum_windows, EnumChildWindows=enum_children, IsWindowVisible=lambda h: w[h]["visible"],
            IsWindow=lambda h: h in w,
            IsWindowEnabled=lambda h: w[h]["enabled"], GetClassName=lambda h: w[h]["cls"],
            GetWindowText=lambda h: w[h]["title"], GetWindow=lambda h, cmd: w[h]["owner"] if cmd == 4 else 0,
            GetWindowLong=get_long, PostMessage=lambda h, msg, a, b: self.posted.append((h, msg)))
        p = types.SimpleNamespace(GetWindowThreadProcessId=lambda h: (1, w[h]["pid"]))
        c = types.SimpleNamespace(GWL_STYLE=-16, GWL_EXSTYLE=-20, GW_OWNER=4, BM_CLICK=0xF5, WM_CLOSE=0x10)
        uia = dict(uia or {})
        monkeypatch.setattr(D, "IS_WIN", True)
        monkeypatch.setattr(D, "win32gui", g, raising=False)
        monkeypatch.setattr(D, "win32process", p, raising=False)
        monkeypatch.setattr(D, "win32con", c, raising=False)
        monkeypatch.setattr(D, "_last_active_popup", lambda h: self.lap.get(h, h), raising=False)
        monkeypatch.setattr(D, "_uia_read", lambda h: uia.get(h), raising=False)
        monkeypatch.setattr(D, "_uia_invoke", lambda h, name: self.invoked.append((h, name)) or True, raising=False)
        return self


def _vs(desk: _Desk, main_enabled: bool) -> int:
    """The shell's main window (WPF) and the windows that are always there: four glow windows around it (layered,
    click-through, never activated) and a hidden helper window."""
    m = desk.top(10, WPF, "Project15 - TcXaeShell", enabled=main_enabled)
    for i in range(4):
        desk.top(20 + i, "VisualStudioGlowWindow", owner=m, ex=0x08000020 | 0x80000)
    desk.top(30, "GDI+ Window", visible=False)
    return m


def test_n173_a_disabled_main_window_names_its_modal_window_of_any_class(monkeypatch):
    """A8: the main window disabled + an owned window of class HwndWrapper (WPF About), WindowsForms10.* or #32770 →
    that window is the dialog, with hwnd, title, class and the buttons UI Automation reads (or buttons unknown)."""
    desk = _Desk()
    main = _vs(desk, main_enabled=False)
    desk.top(40, WPF, "About Microsoft Visual Studio", owner=main)
    desk.install(monkeypatch, uia={40: (["OK", "Copy Info"], ["Microsoft Visual Studio 2022", "TwinCAT XAE Shell"])})
    ds = D.list_dialogs(100)
    assert [(d.hwnd, d.title, getattr(d, "cls", None)) for d in ds] == [(40, "About Microsoft Visual Studio", WPF)], ds
    dd = ds[0].to_dict()
    assert dd["buttons"] == ["OK", "Copy Info"] and dd["class"] == WPF and "TwinCAT XAE Shell" in dd["text"], dd
    assert "buttons_note" not in dd and ds[0].awaits_answer()
    # the same window when UI Automation cannot read it: still reported — buttons unknown, never dropped
    desk.install(monkeypatch, uia={})
    dd = D.list_dialogs(100)[0].to_dict()
    assert dd["hwnd"] == 40 and dd["buttons"] is None and dd["buttons_note"] == "not a standard dialog — buttons unknown"
    assert D.list_dialogs(100)[0].awaits_answer()              # unknown buttons: taken as waiting for an answer
    # a WinForms window, and a #32770 one (read from its child controls, listed once)
    desk.w[40]["cls"] = WINFORMS
    desk.install(monkeypatch, uia={40: (["Close"], [])})
    assert [(d.hwnd, d.cls, d.captions()) for d in D.list_dialogs(100)] == [(40, WINFORMS, ["Close"])]
    del desk.w[40]
    desk.top(41, "#32770", "Microsoft Visual Studio", owner=main)
    desk.child(41, 42, "Static", "Save changes to the following items?")
    desk.child(41, 43, "Button", "&Yes", style=0x1)
    desk.child(41, 44, "Button", "&No")
    desk.install(monkeypatch)
    ds = D.list_dialogs(100)
    assert [(d.hwnd, d.cls, d.captions(), d.text) for d in ds] == [
        (41, "#32770", ["Yes", "No"], "Save changes to the following items?")], ds


def test_n173_no_dialog_while_the_main_window_is_enabled(monkeypatch):
    """A8 / A6: a floating tool window, the Error List torn off as its own window, a splash or progress window while the
    main window stays enabled are not dialogs — a visible extra window alone is never one."""
    desk = _Desk()
    main = _vs(desk, main_enabled=True)
    desk.top(50, WPF, "Error List", owner=main)                       # torn off
    desk.top(51, WPF, "Solution Explorer", owner=main, ex=0x80)       # floating tool window
    desk.top(52, "SplashWindow", "TcXaeShell")                        # unowned splash
    desk.top(53, WPF, "Building…", owner=main)                        # progress window, main enabled
    desk.install(monkeypatch, uia={50: (["Clear"], []), 53: (["Cancel"], [])})
    assert D.list_dialogs(100) == []
    # windows of another process are never this shell's, whatever their state
    desk.top(60, "#32770", "System Properties", pid=999)
    desk.top(61, WPF, "Other app", pid=999, enabled=False)
    desk.top(62, WPF, "Its dialog", pid=999, owner=61)
    desk.install(monkeypatch, uia={62: (["OK"], [])})
    assert D.list_dialogs(100) == []


def test_n173_the_modal_window_among_other_windows_and_when_none_can_be_named(monkeypatch):
    """A8: with the main window disabled, the window that holds it is the one its owner last activated (not a floating
    tool window that stays enabled, not a glow window); a dialog opened from a dialog is the one that waits; a main
    window disabled with no window to name is said, with the main window's hwnd and class."""
    desk = _Desk()
    main = _vs(desk, main_enabled=False)
    desk.top(51, WPF, "Solution Explorer", owner=main)                # floating tool window, still enabled
    desk.top(70, WPF, "Options", owner=main, enabled=False)           # VS Options, itself held by …
    desk.top(71, WINFORMS, "TwinCAT Settings", owner=70)               # … a dialog it opened
    desk.lap[main] = 71
    desk.install(monkeypatch, uia={51: (["Close"], []), 70: (["OK", "Cancel"], []), 71: (["OK", "Cancel"], [])})
    assert [(d.hwnd, d.title) for d in D.list_dialogs(100)] == [(71, "TwinCAT Settings")]
    # nothing that can take input is left: XAE is modal, but the window cannot be named
    for h in (51, 70, 71):
        del desk.w[h]
    desk.lap.clear()
    desk.install(monkeypatch)
    ds = D.list_dialogs(100)
    assert len(ds) == 1 and not ds[0].identified and ds[0].hwnd == main and ds[0].cls == WPF, ds
    dd = ds[0].to_dict()
    assert dd["unidentified"] is True and dd["buttons"] is None and D.label(dd).startswith(
        "XAE's main window is disabled (a modal window is open) but it could not be identified"), dd
    assert f"hwnd {main}" in D.label(dd) and WPF in D.label(dd)


def test_n173_the_main_window_the_shell_names_is_the_one_watched(monkeypatch):
    """A8: with DTE.MainWindow.HWnd known (set_main_window at attach), that window decides — an unowned window of the
    same process that is disabled (a floating document window) is not taken for the main window."""
    desk = _Desk()
    main = _vs(desk, main_enabled=True)
    desk.top(80, WPF, "MAIN.TcPOU", enabled=False)                    # an unowned floating document window, disabled
    desk.install(monkeypatch)
    monkeypatch.setattr(D, "_MAIN_WINDOWS", {}, raising=False)
    D.set_main_window(100, main)
    assert D.list_dialogs(100) == []
    desk.w[main]["enabled"] = False                                    # the main window held by the About box
    desk.top(40, WPF, "About Microsoft Visual Studio", owner=main)
    desk.install(monkeypatch, uia={40: (["OK"], [])})
    assert [(d.hwnd, d.captions()) for d in D.list_dialogs(100)] == [(40, ["OK"])]
    D.set_main_window(100, None)
    assert D._MAIN_WINDOWS == {}


def test_review_0815_a_hidden_main_window_still_decides(monkeypatch):
    """Review of 0.8.15 (N173): with xae.visible=false the shell's main window is hidden — only the visible windows
    were looked at, so the main window was never checked and a modal window it owns (a WPF box) went unseen. The
    recorded main window decides while it is a window of the process, visible or not."""
    desk = _Desk()
    main = desk.top(10, WPF, "Project15 - TcXaeShell", enabled=False, visible=False)
    desk.top(40, WPF, "Options", owner=main)
    desk.install(monkeypatch, uia={40: (["OK", "Cancel"], [])})
    monkeypatch.setattr(D, "_MAIN_WINDOWS", {}, raising=False)
    D.set_main_window(100, main)
    assert [(d.hwnd, d.captions()) for d in D.list_dialogs(100)] == [(40, ["OK", "Cancel"])]
    desk.w[main]["enabled"] = True                                     # nothing modal: the visible window is no dialog
    assert D.list_dialogs(100) == []
    D.set_main_window(100, None)


def test_n173_resolve_presses_ui_automation_buttons_and_refuses_unknown_ones(monkeypatch):
    """Spec 1.3.6 / 1.3.8: a non-#32770 window's button is pressed through UI Automation (Invoke); a window whose buttons
    are unknown is refused — 'close it in XAE' — and nothing is sent to it (no WM_CLOSE, no Escape); an auto-answer
    rule never fires on such a window; remember=true on a window whose buttons were read adds a rule as for #32770."""
    desk = _Desk()
    main = _vs(desk, main_enabled=False)
    desk.top(40, WPF, "About Microsoft Visual Studio", owner=main)
    desk.install(monkeypatch, uia={40: (["OK", "Copy Info"], [])})
    wd = D.DialogWatchdog([])
    wd.pid = 100
    r = wd.resolve("ok", remember=True)
    assert r["resolved"] and desk.invoked == [(40, "OK")] and desk.posted == [] and r["remembered"], r
    assert wd.rules == [{"title_contains": "About Microsoft Visual Studio", "button": "ok"}]
    desk.invoked.clear()
    assert wd.poll() == [] and desk.invoked == [(40, "OK")]           # the remembered rule answers it next time
    # buttons unknown: refused, nothing sent, and the rule (matching on the title) does not fire
    desk.install(monkeypatch, uia={})
    desk.invoked.clear()
    left = wd.poll()
    assert [d.hwnd for d in left] == [40] and desk.invoked == [] and desk.posted == []
    r = wd.resolve("OK")
    assert not r["resolved"] and "buttons can't be pressed by the tool — close it in XAE" in r["reason"], r
    assert desk.invoked == [] and desk.posted == []
    # the main window disabled, nothing to name: refused the same way
    del desk.w[40]
    desk.install(monkeypatch)
    r = wd.resolve("OK")
    assert not r["resolved"] and "close it in XAE" in r["reason"] and desk.posted == [], r


def test_n173_a_call_the_shell_rejects_names_the_modal_window_of_any_class(monkeypatch):
    """A7: the About box opened by hand, then a call — the shell rejects COM while it is modal; after a second the
    rejected call names the window (BLOCKED BY DIALOG 'About …', class, buttons) instead of retrying for com_retry_s.
    A progress window that offers only Cancel is no reason to stop: the call is asked again."""
    from twincat_mcp import xae_backend as X
    from twincat_mcp.config import XaeConfig
    desk = _Desk()
    main = _vs(desk, main_enabled=False)
    desk.top(40, WPF, "About Microsoft Visual Studio", owner=main)
    desk.install(monkeypatch, uia={40: (["OK"], [])})
    be = X.XaeBackend(XaeConfig(com_retry_s=6))
    be.pid = 100

    class Rejected(Exception):
        hresult = X.RPC_E_CALL_REJECTED

    def rejected():
        raise Rejected("Call was rejected by callee.")
    t0 = time.monotonic()
    with pytest.raises(X.XaeError, match=r"LookupTreeItem TIPC: BLOCKED BY DIALOG 'About Microsoft Visual Studio' — "
                                         r"buttons \['OK'\].*\"class\": \"HwndWrapper"):
        be._retry(rejected, "LookupTreeItem TIPC")
    assert time.monotonic() - t0 < 5.0
    # a progress window (Cancel only) holds the shell: asked again until com_retry_s, then 'busy'
    desk.install(monkeypatch, uia={40: (["Cancel"], [])})
    be.cfg.com_retry_s = 1.5
    with pytest.raises(X.XaeError, match=r"XAE is busy \(call rejected, 0x80010001\) and did not run this call"):
        be._retry(rejected, "LookupTreeItem TIPC")


@pytest.mark.anyio
async def test_n173_a_call_held_by_a_non_standard_modal_window_is_blocked_within_seconds(monkeypatch, project_dir):
    """A1 / A2: xae_command Help.About opened the About box (WPF); the call hung 120 s and ended 'XAE call timed out; a
    modal dialog in the shell is the usual cause', xae_dialogs listed nothing. Now BLOCKED BY DIALOG within ≤ 5 s with
    hwnd, title, class and buttons, and xae_dialogs lists it (live) with the same hwnd."""
    desk = _Desk()
    main = _vs(desk, main_enabled=True)
    desk.install(monkeypatch, uia={40: (["OK"], ["Microsoft Visual Studio"])})
    app = App(mk_cfg(project_dir))
    app.xae.pid = 100
    release = threading.Event()

    def command():                         # what ExecuteCommand('Help.About') does: the shell is modal until it closes
        desk.w[main]["enabled"] = False
        desk.top(40, WPF, "About Microsoft Visual Studio", owner=main)
        release.wait(20)
        return {"executed": "Help.About"}
    t0 = time.monotonic()
    try:
        with pytest.raises(ToolError, match=r"command: BLOCKED BY DIALOG 'About Microsoft Visual Studio'.*blocked_by_dialog: "
                                            r"\{\"hwnd\": 40, \"title\": \"About Microsoft Visual Studio\", \"class\": "
                                            r"\"HwndWrapper.*\"buttons\": \[\"OK\"\]"):
            await app.xae_call(command, timeout=8)
        assert time.monotonic() - t0 <= 5.0
        live = app.dialogs_now()
        assert [d["hwnd"] for d in live] == [40] and live[0]["class"] == WPF
    finally:
        release.set()


@pytest.mark.anyio
async def test_n173_a_timeout_names_the_modal_state_found_at_that_moment(monkeypatch, project_dir):
    """Spec 1.3.5: a call that still times out enumerates again at that moment: the main window disabled and no window
    to name → it says so with the main window's hwnd/class (not 'a modal dialog is the usual cause'); nothing modal →
    it says that no modal window is open."""
    desk = _Desk()
    main = _vs(desk, main_enabled=False)                  # modal before the call (not the call's own dialog)
    desk.install(monkeypatch)
    app = App(mk_cfg(project_dir))
    app.xae.pid = 100
    gate = threading.Event()

    def hangs():
        gate.wait(10)
        return {}
    try:
        with pytest.raises(ToolError) as ei:
            await app.xae_call(hangs, timeout=1.5)
        msg = str(ei.value)
        assert "XAE's main window is disabled (a modal window is open) but it could not be identified" in msg, msg
        assert f"hwnd {main}" in msg and "usual cause" not in msg, msg
    finally:
        gate.set()
    await asyncio.sleep(0.3)
    desk.w[main]["enabled"] = True
    gate.clear()
    try:
        with pytest.raises(ToolError) as ei:
            await app.xae_call(hangs, timeout=1.5)
        assert "no modal window is open in XAE" in str(ei.value) and "usual cause" not in str(ei.value), str(ei.value)
    finally:
        gate.set()


@pytest.mark.anyio
@pytest.mark.parametrize("read_only", [True, False])
async def test_review13_0816_a_timed_out_action_is_not_to_be_repeated(monkeypatch, project_dir, read_only):
    """Thirteenth review: a call that times out with no modal window is still running in the shell — 'then retry' fits
    a read; an action retried then is queued a second time behind the one still running."""
    from twincat_mcp.server import _READ_ONLY_CALL
    desk = _Desk()
    _vs(desk, main_enabled=True)
    desk.install(monkeypatch)
    app = App(mk_cfg(project_dir))
    app.xae.pid = 100
    gate = threading.Event()

    def hangs():
        gate.wait(10)
        return {}
    tok = _READ_ONLY_CALL.set(read_only)
    try:
        with pytest.raises(ToolError) as ei:
            await app.xae_call(hangs, timeout=1.5)
    finally:
        _READ_ONLY_CALL.reset(tok)
        gate.set()
    msg = str(ei.value)
    assert "no modal window is open in XAE" in msg, msg
    if read_only:
        assert msg.endswith("xae_status / xae_dialogs, then retry"), msg
    else:
        assert "then check its result when it is done; do not repeat it before (it would run a second time)" in msg \
            and "retry" not in msg, msg


# --------------------------------------------------------------------------- N173: UI Automation through COM vtables
import ctypes as _ct


class _FakeCom:
    """A COM object in memory as UI Automation hands one out: a pointer to a table of function pointers (IUnknown's
    Release at slot 2, the methods at their slots). slots: {slot: (argtypes, python function)}."""

    def __init__(self, slots, **attrs):
        self.released = 0
        self.__dict__.update(attrs)
        self._keep = []
        table = (_ct.c_void_p * (max(list(slots) + [2]) + 1))()
        rel = _ct.CFUNCTYPE(_ct.c_ulong, _ct.c_void_p)(self._release)
        self._keep.append(rel)
        table[2] = _ct.cast(rel, _ct.c_void_p).value
        for slot, (argtypes, fn) in slots.items():
            cb = _ct.CFUNCTYPE(_ct.c_long, _ct.c_void_p, *argtypes)(fn)
            self._keep.append(cb)
            table[slot] = _ct.cast(cb, _ct.c_void_p).value
        self._table = table
        self._obj = _ct.c_void_p(_ct.addressof(table))
        self.addr = _ct.addressof(self._obj)

    def _release(self, this):
        self.released += 1
        return 0


def _fake_uia(names_by_type, fail_element=False):
    """IUIAutomation → window element → FindAll(ControlType == …) → element arrays → elements with names; the Invoke
    pattern of a button. Returns (create, objects, record)."""
    PP = _ct.POINTER(_ct.c_void_p)
    rec = {"invoked": [], "freed": [], "conditions": [], "patterns": []}
    bstrs = {}

    def element(name):
        buf = _ct.create_unicode_buffer(name)
        bstrs[_ct.addressof(buf)] = buf

        def get_name(this, out):
            out[0] = _ct.addressof(buf)
            return 0
        pat = _FakeCom({3: ((), lambda this: rec["invoked"].append(name) or 0)})

        def pattern_as(this, pid, iid, out):
            rec["patterns"].append((pid, bytes(iid.contents)))
            out[0] = pat.addr
            return 0
        return _FakeCom({23: ((PP,), get_name), 14: ((_ct.c_int, _ct.POINTER(D._GUID), PP), pattern_as)}, name=name,
                        pattern=pat)
    elems = {ct: [element(n) for n in names] for ct, names in names_by_type.items()}

    def array(items):
        def length(this, out):
            out[0] = len(items)
            return 0

        def get(this, i, out):
            out[0] = items[i].addr
            return 0
        return _FakeCom({3: ((_ct.POINTER(_ct.c_int),), length), 4: ((_ct.c_int, PP), get)}, items=items)
    arrays = {ct: array(items) for ct, items in elems.items()}
    conds: dict = {}

    def find_all(this, scope, cond, out):
        assert scope == D.TreeScope_Descendants
        out[0] = arrays[conds[cond]].addr
        return 0
    window = _FakeCom({6: ((_ct.c_int, _ct.c_void_p, PP), find_all)})

    def from_handle(this, hwnd, out):
        if fail_element:
            return -2147467259                       # E_FAIL
        rec["hwnd"] = hwnd
        out[0] = window.addr
        return 0

    def condition(this, prop, v, out):
        rec["conditions"].append((prop, v.vt, v.v.lVal))
        c = _FakeCom({})
        conds[c.addr] = v.v.lVal
        rec.setdefault("cond_objs", []).append(c)
        out[0] = c.addr
        return 0
    auto = _FakeCom({6: ((_ct.c_void_p, PP), from_handle), 23: ((_ct.c_int, D._VARIANT, PP), condition)})
    calls = D._UiaCalls(_ct.CFUNCTYPE, lambda: auto.addr, lambda b: _ct.wstring_at(b),
                        lambda b: rec["freed"].append(b))
    return calls, {"auto": auto, "window": window, "arrays": arrays, "elems": elems}, rec


def test_n173_ui_automation_reads_and_presses_a_button_through_the_vtables():
    """N173: a non-#32770 window is read through UI Automation — pywin32 has no IUIAutomation and comtypes is no
    dependency, so dialogs.py calls the COM vtables with ctypes. Against fake COM objects: ElementFromHandle(hwnd),
    CreatePropertyCondition(ControlType == Button / Text, VT_I4), FindAll(TreeScope_Descendants), the names, Invoke
    through GetCurrentPatternAs(Invoke, IID_IUIAutomationInvokePattern) — and every object released, every BSTR freed."""
    calls, objs, rec = _fake_uia({D.UIA_ButtonControlTypeId: ["OK", "Copy &Info"],
                                  D.UIA_TextControlTypeId: ["Microsoft Visual Studio", "TwinCAT XAE Shell"]})
    assert calls.read(0x1234) == (["OK", "Copy &Info"], ["Microsoft Visual Studio", "TwinCAT XAE Shell"])
    assert rec["hwnd"] == 0x1234
    assert rec["conditions"] == [(30003, 3, 50000), (30003, 3, 50020)]          # ControlType == Button, then Text
    assert len(rec["freed"]) == 4 and objs["window"].released == 1
    assert all(c.released == 1 for c in rec["cond_objs"]) and all(a.released == 1 for a in objs["arrays"].values())
    assert all(e.released == 1 for es in objs["elems"].values() for e in es)
    assert calls.invoke(0x1234, "Copy Info") is True and rec["invoked"] == ["Copy &Info"]
    iid = D._guid(D.IID_IUIAutomationInvokePattern)
    assert rec["patterns"] == [(D.UIA_InvokePatternId, bytes(iid))]
    assert objs["elems"][D.UIA_ButtonControlTypeId][1].pattern.released == 1
    assert calls.invoke(0x1234, "Cancel") is False and rec["invoked"] == ["Copy &Info"]
    # a failing call is an error (buttons unknown upstream), not a crash
    bad, _o, _r = _fake_uia({}, fail_element=True)
    with pytest.raises(OSError, match="0x80004005"):
        bad.read(0x1234)
    # the worker: a window that does not answer in time gives None now and its answer on a later scan
    gate = threading.Event()

    class Slow:
        def read(self, hwnd):
            gate.wait(5)
            return (["OK"], [])
    w = D._UiaWorker(lambda: Slow())
    assert w.read(7, 0.2) is None
    gate.set()
    for _ in range(50):
        if w.read(7, 0.2) is not None:
            break
        time.sleep(0.05)
    assert w.read(7, 0.2) == (["OK"], []) and w._reads[7]["keep"]
    w.forget({8})
    assert 7 not in w._reads


@pytest.mark.skipif(sys.platform != "win32", reason="a real window and UI Automation need Windows")
def test_n173_windows_a_real_owned_window_is_found_read_and_pressed():  # pragma: no cover - Windows CI
    """N173 on Windows (CI): a real top-level window of this process, disabled, owning an enabled window that is not a
    #32770 dialog and holds a push button → list_dialogs finds it by modality, reads its button through UI
    Automation (the real vtables), and xae_dialog_resolve's click presses it (BM_CLICK reaches the window)."""
    import win32api
    import win32con
    import win32gui
    clicked = threading.Event()
    made: dict = {}
    ready = threading.Event()

    def wndproc(hwnd, msg, wp, lp):
        if msg == win32con.WM_COMMAND and (wp >> 16) == win32con.BN_CLICKED:
            clicked.set()
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wp, lp)

    def ui():
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = wndproc
        wc.lpszClassName = "TcMcpN173Test"
        wc.hInstance = win32api.GetModuleHandle(None)
        win32gui.RegisterClass(wc)
        main = win32gui.CreateWindowEx(0, "TcMcpN173Test", "Shell", win32con.WS_OVERLAPPEDWINDOW | win32con.WS_VISIBLE,
                                       0, 0, 400, 300, 0, 0, wc.hInstance, None)
        modal = win32gui.CreateWindowEx(0, "TcMcpN173Test", "About Test Shell",
                                        win32con.WS_POPUP | win32con.WS_CAPTION | win32con.WS_VISIBLE,
                                        50, 50, 300, 200, main, 0, wc.hInstance, None)
        win32gui.CreateWindowEx(0, "Button", "OK", win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
                                10, 10, 80, 30, modal, 1, wc.hInstance, None)
        win32gui.EnableWindow(main, False)
        made.update(main=main, modal=modal)
        ready.set()
        win32gui.PumpMessages()
    t = threading.Thread(target=ui, daemon=True)
    t.start()
    assert ready.wait(10)
    try:
        # the vtable calls themselves, without the scan's 1 s budget (the first UI Automation call loads its proxies)
        try:
            read = D._uia_worker().submit(lambda c: c.read(made["modal"])).result(timeout=20)
        except OSError as exc:
            if "CoCreateInstance" in str(exc):
                pytest.skip(f"UI Automation is not available in this session: {exc}")
            raise
        assert read[0] == ["OK"], read
        D.set_main_window(os.getpid(), made["main"])
        ds = [d for d in D.list_dialogs(os.getpid()) if d.hwnd == made["modal"]]
        assert len(ds) == 1 and ds[0].title == "About Test Shell" and ds[0].cls == "TcMcpN173Test", ds
        assert ds[0].captions() == ["OK"] and ds[0].via == "uia", ds[0].to_dict()
        assert D.click(ds[0], "ok") and clicked.wait(5)
        win32gui.EnableWindow(made["main"], True)                       # not modal any more: nothing is listed
        assert [d for d in D.list_dialogs(os.getpid()) if d.hwnd == made["modal"]] == []
    finally:
        D.set_main_window(os.getpid(), None)
        try:
            win32api.PostThreadMessage(t.native_id, win32con.WM_QUIT, 0, 0)
        except Exception:  # noqa: BLE001
            pass


def test_n176_the_shells_own_frame_is_never_a_dialog(monkeypatch):
    """N176 (0.8.15 retest, S4): when Options opened, the watchdog logged the shell's own frame ("TwinCAT Project15 -
    TcXaeShell", HwndWrapper, buttons null) as a 'seen' dialog. A window with the (disabled) main window's title and
    class — the shell's second frame — or the main window the shell names is never the window that holds it."""
    desk = _Desk()
    main = _vs(desk, main_enabled=False)
    desk.top(90, WPF, "Project15 - TcXaeShell")                        # the shell's frame again, unowned, enabled
    desk.top(70, WPF, "Options", owner=main)
    desk.install(monkeypatch, uia={70: (["OK", "Cancel"], []), 90: ([], [])})
    assert [(d.hwnd, d.title) for d in D.list_dialogs(100)] == [(70, "Options")]
    del desk.w[70]                                                       # only the frame left: unidentified, not it
    desk.install(monkeypatch, uia={90: ([], [])})
    ds = D.list_dialogs(100)
    assert len(ds) == 1 and not ds[0].identified and ds[0].hwnd == main, ds
    # twelfth review of 0.8.16: the watchdog's log had that state as a 'seen' dialog titled like the shell — it is the
    # shell's own frame, disabled, said as that (the N176 entry: 'Project15 - TcXaeShell', buttons null)
    wd = D.DialogWatchdog([])
    wd.pid = 100
    wd.poll()
    e = wd.log[-1]
    assert e["event"] == "seen" and e["unidentified"] is True and "title" not in e and "buttons" not in e, e
    assert e["note"] == D.UNIDENTIFIED and e["main_window_class"], e


@pytest.mark.anyio
async def test_n181_a_read_call_a_dialog_refused_says_nothing_was_changed(monkeypatch, project_dir):
    """N181 (0.8.15 retest, live ×2): xae_tree_get / xae_status with the Options dialog open ended 'whether the action
    ran is unknown until the dialog is answered' — nothing was being changed. The refused COM call did not run; a
    read-only tool says nothing was changed (call again once it is answered); any other tool may have run steps before
    the refused one and says to check its result first. The dialog's fields stay in the text (blocked_by_dialog)."""
    from twincat_mcp import xae_backend as X
    from twincat_mcp.server import build_server
    from tests.fake_ads import install
    from tests.test_server_e2e import client_for, text
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    app = srv._tcmcp_app
    d = {"hwnd": 77, "title": "Options", "class": "#32770", "buttons": ["OK", "Cancel"], "text": "Environment"}

    def tree_item(path):
        raise X.XaeBusy.blocked(f"LookupTreeItem {path}", d)

    def tree_consume_xml(path, xml):
        raise X.XaeBusy.blocked(f"ConsumeXml {path}", d)
    monkeypatch.setattr(app.xae, "tree_item", tree_item)
    monkeypatch.setattr(app.xae, "tree_consume_xml", tree_consume_xml)

    async def nothing(*a, **kw):
        return None

    async def confirmed(action, details, cls, coro):
        return await coro
    monkeypatch.setattr(app, "permit", nothing)
    monkeypatch.setattr(app, "confirmed", confirmed)
    async with client_for(srv) as s:
        r = await s.call_tool("xae_tree_get", {"path": "TIPC"})
        w = await s.call_tool("xae_tree_consume_xml", {"path": "TIRT^PlcTask", "xml": "<TreeItem/>"})
    t, tw = text(r), text(w)
    assert r.is_error and "LookupTreeItem TIPC: BLOCKED BY DIALOG 'Options'" in t, t
    assert "it did not run. Nothing was changed (a read-only call): answer it (xae_dialog_resolve(button=...)), then " \
           "call again." in t and "whether the action ran" not in t and "may have run" not in t, t
    assert 'blocked_by_dialog: {"hwnd": 77, "title": "Options", "class": "#32770"' in t, t
    assert w.is_error and "ConsumeXml TIRT^PlcTask: BLOCKED BY DIALOG 'Options'" in tw, tw
    assert "Steps of this call before it may have run: answer it (xae_dialog_resolve(button=...)), then check the " \
           "result before calling again." in tw and "Nothing was changed" not in tw, tw
    # the backend alone does not know the tool: said as the latter; buttons it can't press: close it in XAE
    e = X.XaeBusy.blocked("LookupTreeItem TIPC", {**d, "buttons": None, "buttons_note": "not readable"})
    assert "may have run: close it in XAE (its buttons can't be pressed by the tool)" in str(e) and e.dialog["hwnd"] == 77


@pytest.mark.anyio
async def test_0817_busy_after_an_action_never_says_retry(project_dir):
    """Fourteenth review of 0.8.16: after an action of the same tool ran, the busy text withholds 'repeat it' — the
    dialog hint then said 'retry in a few seconds' (a second boot project / activation)."""
    from twincat_mcp import xae_backend as X
    from twincat_mcp.server import _tool_boundary
    app = App(mk_cfg(project_dir))
    app.dialogs_now = lambda: []

    def generate():
        app.xae.acted.append("GenerateBootProject")
        return {"generated": True}

    def read():
        raise X.XaeBusy(X.XaeBusy.busy_text("BootProjectAutostart", X.RPC_E_CALL_REJECTED, 0.3, []),
                        what="BootProjectAutostart", busy=(X.RPC_E_CALL_REJECTED, 0.3))

    async def two_calls():
        await app.xae_call(generate)
        await app.xae_call(read)
    with pytest.raises(ToolError) as ei:
        await _tool_boundary(two_calls)()
    t = str(ei.value)
    assert "earlier step(s) ran (GenerateBootProject)" in t and "retry" not in t and "repeat it" not in t, t
