"""TwinCAT XAE / Visual Studio Automation Interface (TE1000) backend.

Pure Python over COM (pywin32). No C# helper, no PowerShell, no daemon
build step — the two things that make the existing XAE servers painful to
install. The price: everything is late-bound IDispatch, so a handful of
VS-side objects (DTE2.ToolWindows) need the fallbacks implemented in
``_error_items``.

All methods are synchronous and run on an STA worker thread
(``pythoncom.CoInitialize`` in the worker init). COM "server busy" errors
(RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER) are retried for
``cfg.com_retry_s`` seconds — the XAE shell rejects calls while it builds,
shows a modal dialog or is logging in.

Attach strategy: enumerate the Running Object Table for ``!TcXaeShell.DTE.*``
/ ``!VisualStudio.DTE.*`` monikers, prefer the instance whose open solution
matches ``cfg.solution_path``; else the first one. Launching a new shell is
opt-in (``attach_only=False`` or ``xae_attach(launch=True)``).

Tree paths use Beckhoff syntax: ``TIPC^PlcName^PlcName Project^POUs^MAIN``.
Common roots (LookupTreeItem shortcuts): TIIC (I/O Configuration), TIID (I/O Devices), TIRC (Real-Time Configuration),
TIRT (its Additional Tasks), TIRS (its Real-Time Settings), TIRR (its Route Settings), TIPC (PLC Configuration),
TINC (NC Configuration).
"""
from __future__ import annotations

import logging
import contextlib
import json
import os
import re
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any, Callable, Iterator

from .config import XaeConfig
from .dialogs import DialogWatchdog

log = logging.getLogger(__name__)

IS_WIN = sys.platform == "win32"

if IS_WIN:  # pragma: no cover - not importable on the Linux CI box
    import pythoncom  # type: ignore
    import pywintypes  # type: ignore
    import win32com.client  # type: ignore
else:  # pragma: no cover
    pythoncom = pywintypes = win32com = None  # type: ignore

RPC_E_CALL_REJECTED = -2147418111        # 0x80010001
RPC_E_SERVERCALL_RETRYLATER = -2147417846  # 0x8001010A
RPC_E_DISCONNECTED = -2147417848        # 0x80010108
RPC_S_SERVER_UNAVAILABLE = -2147023174  # 0x800706BA — the shell was closed/reopened; our DTE proxy points at a dead process
DISP_E_EXCEPTION = -2147352567          # 0x80020009 — with scode E_POINTER after a solution reopen: stale System Manager
E_POINTER = -2147467261                 # 0x80004003
DISP_E_MEMBERNOTFOUND = -2147352573     # 0x80020003 — the item type has no such property (folder, property node)
E_NOTIMPL = -2147467263                 # 0x80004001

VS_WINDOW_ERRORLIST = "{D78612C7-9962-4B83-95D9-268046DAD23A}"
VS_WINDOW_SOLUTION_EXPLORER = "{3AE79031-E1BC-11D0-8F78-00A0C9110057}"   # vsWindowKindSolutionExplorer
VS_WINDOW_OUTPUT = "{34E76E81-EE4A-11D0-AE2E-00A0C90FFFC3}"
ENVDTE80_TYPELIB = ("{1A31287A-4D7D-413E-8E32-3B374931BD89}", 0, 8, 0)

IEC_LANG = {"none": 0, "st": 1, "il": 2, "sfc": 3, "fbd": 4, "cfc": 5, "ld": 6}

# CreateChild subtypes for PLC objects (Infosys "Accessing, creating and
# handling PLC POUs")
POU_SUBTYPE = {
    "folder": 601, "program": 602, "function": 603, "fb": 604, "function_block": 604,
    "enum": 605, "struct": 606, "union": 607, "action": 608, "method": 609,
    "itf_method": 610, "property": 611, "itf_property": 612, "property_get": 613,
    "property_set": 614, "gvl": 615, "transition": 616, "interface": 618,
    "visualization": 619, "alias": 623, "parameter_list": 629,
    "itf_property_get": 654, "itf_property_set": 655,
}
# N80: CreateChild(611 / 612) makes a property with no accessor (C0373 "defines neither a get nor a set accessor");
# the accessors are children of their own sub types (Infosys tc3_automationinterface "Creating PLC objects")
_ACCESSOR_SUBTYPE = {("property", "get"): 613, ("property", "set"): 614,
                     ("itf_property", "get"): 654, ("itf_property", "set"): 655}

TREEITEMTYPE_PLCPROJECTDEF = 56   # the nested "<Name> Project" node of a PLC project (ITcPlcIECProject); TE1000 name
TREEITEMTYPE_PLCAPP = TREEITEMTYPE_PLCPROJECTDEF   # the name this module used before (600 is TREEITEMTYPE_PLCAPP)
# TE1000 PLCIMPORTOPTIONS enum page (tc3_automationinterface/21842779147): REPLACE 1, RENAME 2. The older article
# (242732427) lists Rename 1 / Replace 2 — and 2 gave renamed copies at the project root in the field (R15 / N38),
# which is rename. The live type library decides when it is readable (_import_options).
_DOC_IMPORT_OPTIONS = {"none": 0, "replace": 1, "rename": 2, "skip": 3}
# ITcPlcOnline (TE1000 21841270795): PLC_LOGIN_FLAGS, PLC_RESET_TYPE, PLC_APPSTATE
_LOGIN_FLAGS = {"login": 1, "online_change": 2, "download": 4}      # REGULAR, FORCEONLINECHANGE, FORCEDOWNLOAD
_RESET_TYPE = {"reset_cold": 1, "reset_origin": 2}
_APP_STATE = {1: "run", 2: "stop", 3: "halted on breakpoint", -1: "unknown"}
PLC_PROJECT_FOLDERS = ("POUs", "DUTs", "GVLs", "VISUs", "References", "External Types", "PlcTask")

PLC_LOGIN_COMMANDS = {
    "login": ["PLC.Login", "OtherContextMenus.PlcProject.Login"],
    "logout": ["PLC.Logout", "OtherContextMenus.PlcProject.Logout"],
    "start": ["PLC.Start", "OtherContextMenus.PlcProject.Start"],
    "stop": ["PLC.Stop", "OtherContextMenus.PlcProject.Stop"],
    "reset_cold": ["PLC.ResetCold", "OtherContextMenus.PlcProject.ResetCold"],
    "reset_origin": ["PLC.ResetOrigin", "OtherContextMenus.PlcProject.ResetOrigin"],
    "download": ["PLC.Download", "OtherContextMenus.PlcProject.Download"],
    "online_change": ["PLC.Login"],  # TwinCAT offers online change inside Login
}


class XaeError(RuntimeError):
    pass


class XaeExplained(XaeError):
    """An error the plugin composed with its own cause and what to do — not annotated with a known-error note (review
    of 0.8.14: '… the project is logged in, application …' got 'XAE is logged in … log out first' appended)."""


class XaeBusy(XaeError):
    """N174: the shell rejected the call (RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER) for all of com_retry_s,
    or a dialog holds it — the call did not run. Never read as 'no value' by a read that has a fallback (_get).
    dialog: the modal window that holds the shell (BLOCKED BY DIALOG), else None."""

    def __init__(self, msg: str, dialog: dict[str, Any] | None = None, what: str = "",
                 busy: tuple[int, float] | None = None) -> None:
        super().__init__(msg)
        self.dialog = dialog
        self.what = what
        self.busy = busy                   # (HRESULT, seconds asked again) of a rejected call, else None

    @staticmethod
    def busy_text(what: str, hr: int, window: float, acted: list[str] | None = None) -> str:
        """N174: a rejected call ran nothing — said as that. Twelfth review of 0.8.16: 'repeat it' only when nothing of
        the call ran before it; after an action of the same call ran (boot_project's GenerateBootProject, then a
        rejected read), repeating the call runs that action again."""
        then = (f"this call's earlier step(s) ran ({', '.join(dict.fromkeys(acted))}): check the result before calling "
                "again (xae_status)" if acted else "repeat it")
        return (f"{what}: XAE is busy (call rejected, 0x{hr & 0xFFFFFFFF:08X}) and did not run this call — it was asked "
                f"again for {window:g} s; {then}. If it keeps happening, a modal window or a long build holds the shell "
                "(xae_dialogs)")

    @classmethod
    def blocked(cls, what: str, d: dict[str, Any]) -> "XaeBusy":
        return cls(cls.blocked_text(what, d, None), d, what)

    @staticmethod
    def blocked_text(what: str, d: dict[str, Any], read_only: bool | None, nothing_ran: bool = False) -> str:
        """N181 (0.8.15 retest): the refused COM call did not run — that is said as it is. What it means for the tool
        that made it depends on the tool: a read-only one changed nothing (call it again once the dialog is
        answered); any other may have run steps before the refused one, so its result is checked first. read_only
        None: not known here (the backend alone) — said as the latter."""
        from . import dialogs as _dlg
        answer = ("answer it (xae_dialog_resolve(button=...))" if d.get("buttons") is not None else
                  "close it in XAE (its buttons can't be pressed by the tool)")
        # N181b (0.8.16 retest): refused at the call's first COM call — nothing of it ran, also for a tool that edits
        then = ("Nothing was changed (a read-only call): " + answer + ", then call again." if read_only else
                "Nothing of this call ran (it was refused at its first step): " + answer + ", then call again."
                if nothing_ran else
                "Steps of this call before it may have run: " + answer + ", then check the result before calling "
                "again.")
        return (f"{what}: BLOCKED BY DIALOG {_dlg.label(d)} — {_dlg.buttons_text(d)}; text: "
                f"{str(d.get('text', ''))[:300]!r}. The shell is waiting for an answer and refused this call — it did "
                f"not run. {then} blocked_by_dialog: "
                + json.dumps({k: d.get(k) for k in ("hwnd", "title", "class", "buttons", "buttons_note") if k in d},
                             default=str))


class XaeGone(XaeError):
    """N174: the shell went away (closed, crashed) and could not be reached again."""


_BUSY_HR = (RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER)
_GONE_HR = (RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE)


class _ActedUnconfirmed(XaeExplained):
    """A PLC online command was accepted but its state was not read back: it may have acted — never retried elsewhere."""


class _NeedsLogin(XaeExplained):
    """Start / Stop / Reset asked of a project that is not logged in: refused before any route runs it."""


def sta_init() -> None:  # pragma: no cover
    if IS_WIN:
        pythoncom.CoInitialize()


def sta_teardown() -> None:  # pragma: no cover
    if IS_WIN:
        pythoncom.CoUninitialize()


def _norm_sol(p: str | None) -> str:
    """A solution path as compared — as Windows treats it (case and separators do not matter; XAE runs on Windows
    only, so the rule does not depend on the platform the tests run on); '' for none."""
    import ntpath
    p = str(p or "").strip()
    if p and os.name == "nt":
        p = os.path.abspath(p)              # a relative xae.solution_path: as the server's folder resolves it
    return ntpath.normcase(ntpath.normpath(p)) if p else ""


class XaeBackend:
    def __init__(self, cfg: XaeConfig):
        self.cfg = cfg
        self.dte: Any = None
        # what the System Manager logged during the actions of the current call (_sm_messages_of; the server empties it
        # before each XAE call and adds it to the result as xae_messages)
        self.messages: list[dict[str, Any]] = []
        # the actions (once=True) that ran in the current call — the server empties it before each XAE call (a
        # rejected step after them is no 'repeat it', twelfth review of 0.8.16)
        self.acted: list[str] = []
        # N181b (0.8.16 retest): COM calls that ran in the current server call — 0 when a dialog refused its first one
        self.calls_ok = 0
        # N188 (0.8.16 retest): each action's read of the message buffer — {"action", "read", "why"?} — so a result
        # without xae_messages says whether the buffer was read and empty or could not be read
        self.msg_reads: list[dict[str, Any]] = []
        self._msg_why = ""
        self.sysman: Any = None
        self.progid: str | None = None
        self.attached_at: float | None = None
        self._sysman_project_name: str | None = None
        self.pid: int | None = None
        self.watchdog = DialogWatchdog(cfg.dialog_rules)
        # N116 (0.8.12 retest, S1): the solution this server works on — xae.solution_path, else the one named in
        # xae_attach(solution_path=…) / xae_open_solution, else the one open when it attached. Every call checks that
        # the shell still has it: closed → refused (never another solution), reopened → found again in the ROT.
        self.bound_solution: str | None = cfg.solution_path or None
        self.reattach_note: str | None = None
        self.bound_check_failed: str | None = None

    # ------------------------------------------------------------ helpers
    def _require_win(self) -> None:
        if not IS_WIN:
            raise XaeError("XAE backend needs Windows with TwinCAT XAE and pywin32 installed "
                           "(pip install pywin32). This process is running on " + sys.platform)

    def _retry(self, fn: Callable[[], Any], what: str = "COM call", once: bool = False,
               retry_s: float | None = None) -> Any:
        """Retry COM calls while the shell is busy (a rejected call ran nothing). once=True: an action that must not
        run twice (activate, restart, an online command, create / delete / import, a shell command): when the shell
        goes away DURING it, whether it acted is unknown — the server re-attaches, but the call is not repeated on
        the new shell (review of 0.8.14: the retry, which now reaches the live shell, could run it a second time).
        retry_s: how long a busy shell is asked again (default xae.com_retry_s)."""
        window = self.cfg.com_retry_s if retry_s is None else retry_s
        t_start = time.time()
        deadline = t_start + window
        delay = 0.25
        reattached = refreshed = False
        while True:
            try:
                res = fn()
                if once and isinstance(getattr(self, "acted", None), list):
                    self.acted.append(what)            # it ran: a rejected step after it is no 'repeat it'
                self.calls_ok = getattr(self, "calls_ok", 0) + 1
                return res
            except Exception as exc:  # noqa: BLE001
                hr = getattr(exc, "hresult", None)
                if hr in _BUSY_HR and time.time() - t_start > min(1.0, window / 2):
                    # busy for more than a second: if a modal dialog has the shell, retrying for a minute is pointless
                    # (N173: a modal window of any class; one that waits for an answer — a progress window that
                    # offers only Cancel goes away by itself, so the call is asked again)
                    dlg = self._modal_dialogs()
                    if dlg:
                        raise XaeBusy.blocked(what, dlg[0]) from exc
                if hr in _BUSY_HR and time.time() < deadline:
                    time.sleep(delay)
                    delay = min(delay * 1.5, 2.0)
                    continue
                if hr in _BUSY_HR:
                    # N174: a rejected call ran nothing — said as that, never as a defect or a missing value
                    raise XaeBusy(XaeBusy.busy_text(what, hr, window, getattr(self, "acted", None)), what=what,
                                  busy=(hr, window)) from exc
                if hr in _GONE_HR and once:
                    self.dte = self.sysman = None
                    where = "the server could not re-attach yet"
                    if not getattr(self, "_reattaching", False):
                        self._reattaching = True
                        try:
                            self.attach(_internal=True)
                            where = f"re-attached to {self.bound_solution or 'the running shell'}"
                            self.reattach_note = (f"the XAE shell this server was attached to went away during {what}; "
                                                  f"{where}" + (f" (pid {self.pid})" if self.pid else ""))
                        except Exception:  # noqa: BLE001
                            self.dte = self.sysman = None
                        finally:
                            self._reattaching = False
                    raise XaeExplained(
                        f"{what}: the XAE shell went away during the call (closed or crashed, 0x{hr & 0xFFFFFFFF:08X}) — "
                        f"whether it acted is unknown, so it is not repeated ({where}). Check xae_status / ads_status "
                        "before you run it again.") from exc
                if hr in _GONE_HR:
                    self.dte = self.sysman = None
                    if reattached:
                        raise XaeGone(f"{what}: XAE shell disconnected (closed or restarted); re-attach did not help: "
                                      f"{_com_msg(exc)}. Re-run xae_attach.") from exc
                    if getattr(self, "_reattaching", False):
                        raise XaeGone(f"{what}: XAE shell disconnected (closed or crashed). Re-run xae_attach.") from exc
                    # the solution was closed and reopened (or the shell restarted): find the live shell and retry once
                    self._reattaching = True
                    try:
                        self.attach(_internal=True)
                    except Exception as exc2:  # noqa: BLE001
                        self.dte = self.sysman = None
                        raise XaeGone(f"{what}: XAE shell disconnected (closed or restarted); re-attach did not help: "
                                      f"{_com_msg(exc2) if hasattr(exc2, 'hresult') else exc2}. Re-run xae_attach.") from exc2
                    finally:
                        self._reattaching = False
                    log.warning("%s: DTE proxy was dead (0x%08X); re-attached to the running shell and retrying", what, hr & 0xFFFFFFFF)
                    self.reattach_note = (f"the XAE shell this server was attached to went away (closed or "
                                          f"restarted); re-attached to {self.bound_solution or 'the running shell'}"
                                          + (f" (pid {self.pid})" if self.pid else ""))
                    reattached = True
                    continue        # N174: the call on the live shell is retried here too (it rejected it: busy)
                if self._stale_sysman(exc) and once:
                    # re-review of 0.8.14: E_POINTER is also a NullReferenceException TwinCAT's managed code throws
                    # AFTER acting — an action is never repeated on it. (_on_sm / _on_item refresh a stale System
                    # Manager with a read before the action, so a reopened solution does not end up here.)
                    self.sysman = None
                    raise XaeExplained(
                        f"{what}: failed with {_com_msg(exc)} — whether it acted is unknown, so it is not repeated. "
                        "Check xae_status / the XAE tree before you run it again.") from exc
                if self._stale_sysman(exc) and not refreshed and not getattr(self, "_refreshing_sm", False):
                    # N9: the solution was closed and reopened; the cached ITcSysManager belongs to the old one
                    self._refreshing_sm = True
                    try:
                        self.sysman = None
                        self._find_sysman()
                        refreshed = True
                    except (XaeBusy, XaeGone):
                        raise
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        self._refreshing_sm = False
                    if refreshed:
                        log.warning("%s: System Manager object was stale (solution reopened); refreshed and retrying", what)
                        continue    # N174: through this loop (a busy shell is asked again)
                msg = _com_msg(exc)
                if re.search(r"(?<!not )(?<!n't )logged in(?:to)?\b", msg, re.I):     # not 'is not logged in'
                    msg += " — XAE is logged in to the PLC; the project cannot be changed online. Log out first: xae_plc('logout')."
                raise XaeError(f"{what}: {msg}") from exc

    def _get(self, fn: Callable[[], Any], what: str, default: Any = None) -> Any:
        """N174 (0.8.14 retest): a COM read that has a fallback. A member the object does not have (or another failure of
        that object) gives `default`, as the bare try/except around it did — but a busy shell is asked again (_retry),
        and a shell that stays busy, a dialog that holds it or a shell that went away raises: never read as 'no value'
        (a missing tree item, no solution, an empty Error List)."""
        try:
            return self._retry(fn, what)
        except (XaeBusy, XaeGone):
            raise
        except XaeError:
            return default

    def _modal_dialogs(self) -> list[dict[str, Any]]:
        """The shell's modal windows that wait for an answer (N173: of any class; a progress window that offers only
        Cancel goes away by itself — the call is asked again until com_retry_s)."""
        try:
            from . import dialogs as _dlg
            return [d for d in (x.to_dict() for x in _dlg.list_dialogs(self.pid)) if _dlg.awaits(d)]
        except Exception:  # noqa: BLE001 — no dialog enumeration (non-Windows, no pid)
            return []

    def _need(self) -> Any:
        if self.dte is None:
            self.attach(_internal=True)
        else:
            self._check_bound()
        return self.dte

    def _check_bound(self, raise_: bool = True) -> str | None:
        """N116: is the bound solution the one open in the attached shell? If not, the shell that has it now (the
        ROT) is attached instead; none has it → XaeError (raise_) / the reason — nothing acts on another solution."""
        self.bound_check_failed = None
        want = _norm_sol(self.bound_solution)
        if not want or self.dte is None:
            return None
        try:
            # through _retry: a busy shell (call rejected) is asked again, a dead proxy re-attaches to the bound one —
            # never skipped (review: any COM error passed the check, a solution switch in progress among them)
            cur = str(self._retry(lambda: self.dte.Solution.FullName, "Solution.FullName") or "")
        except XaeError as exc:
            if raise_:
                raise
            self.bound_check_failed = str(exc)           # review: a dead / busy shell is said, not read as 'open'
            return None
        if self.dte is None:
            return None
        if _norm_sol(cur) == want:
            return None
        try:
            cands = self._rot_candidates()
        except Exception:  # noqa: BLE001
            cands = []
        match = [c for c in cands if c.get("solution") and _norm_sol(c["solution"]) == want]
        if match:
            self._take(match[0])
            self.reattach_note = (f"{self.bound_solution} is open in another XAE shell now (or was reopened) — "
                                  f"re-attached to it ({match[0].get('moniker') or match[0].get('progid')})")
            log.warning("N116: %s", self.reattach_note)
            return None
        why = (f"the solution this server works on, {self.bound_solution}"
               + (" (xae.solution_path)" if _norm_sol(self.cfg.solution_path) == want else "")
               + f", is not open: the attached shell has {cur or 'no solution'}"
               + (f"; the XAE shells have: {[c.get('solution') or '(no solution)' for c in cands]}" if cands else "")
               + " — nothing was done. Open it in XAE again (the tools find it by themselves), or "
                 "xae_attach(solution_path=…) to work on another solution")
        if raise_:
            raise XaeError(why)
        return why

    def _take(self, pick: dict[str, Any]) -> None:
        """Attach to one ROT candidate (or a launched shell): DTE, progid, pid for the dialog watchdog."""
        self.dte, self.progid = pick["dte"], pick["progid"]
        self.sysman = None
        self.attached_at = time.time()
        self.pid = None
        if pick.get("moniker") and ":" in pick["moniker"]:
            try:
                self.pid = int(pick["moniker"].rsplit(":", 1)[1])
            except ValueError:
                self.pid = None
        # N173: the shell's main window — its being disabled is what says a modal window holds the shell (any class);
        # N174: read through _retry like every COM access (a hint only: a busy shell is asked for 5 s, not com_retry_s)
        try:
            hwnd: int | None = int(self._retry(lambda: self.dte.MainWindow.HWnd, "DTE.MainWindow.HWnd", retry_s=5)) or None
        except (XaeError, TypeError, ValueError) as exc:
            hwnd = None
            log.info("XAE main window unknown (%s): its visible unowned windows stand for it", exc)
        if self.pid is None and hwnd and sys.platform == "win32":
            # launched via Dispatch (no ROT moniker): take the pid from the shell's main window, so the
            # dialog watchdog has a process scope — without one it deliberately watches nothing
            try:
                import win32process  # type: ignore
                self.pid = int(win32process.GetWindowThreadProcessId(hwnd)[1]) or None
            except Exception as exc:  # noqa: BLE001
                log.warning("XAE pid unknown (%s); dialog watchdog disabled until re-attach", exc)
        elif self.pid is None and sys.platform == "win32":
            log.warning("XAE pid unknown; dialog watchdog disabled until re-attach")
        from . import dialogs as _dlg
        _dlg.set_main_window(self.pid, hwnd)
        if self.cfg.dialog_watchdog:
            self.watchdog.start(self.pid)

    @staticmethod
    def _stale_sysman(exc: BaseException) -> bool:
        hr = getattr(exc, "hresult", None)
        info = getattr(exc, "excepinfo", None) or ()
        scode = info[5] if len(info) > 5 else None
        return hr == E_POINTER or (hr == DISP_E_EXCEPTION and (scode == E_POINTER or "pointer" in str(info).lower()))

    def _sm(self) -> Any:
        self._need()                       # N116: the bound solution, checked on every call
        if self.sysman is None:
            self._find_sysman()
        else:
            # N9: a solution closed and reopened behind us leaves the cached System Manager stale — compare the
            # solution that is open now with the one the cached object came from (N174: through _retry)
            cur = self._get(lambda: self.dte.Solution.FullName, "Solution.FullName") if self.dte is not None else None
            if cur is not None and getattr(self, "_sysman_solution", None) not in (None, cur):
                self.sysman = None
                self._find_sysman()
        return self.sysman

    # N120: every retried COM call derives the object it calls from the live root AT CALL TIME. A call made on an
    # object fetched before _retry (sm = self._sm(); _retry(lambda: sm.X())) kept the stale ITcSysManager of a
    # solution closed and reopened in the same shell: _retry refreshed self.sysman, but the retry called the old object
    # again and failed — the first call after a reopen always failed, the second worked (0.8.13 retest, 18 sites).
    def _cur_sm(self) -> Any:
        """The System Manager an attempt uses: the cached one, or the one found now (after _retry dropped a stale one)."""
        if self.sysman is None:
            self._find_sysman()
        return self.sysman

    def _live_sm(self) -> None:
        """Before an action that must not run twice: a read through _retry, which replaces a stale System Manager (a
        solution reopened in the same shell, N120) — so the action itself is not the call that finds it stale."""
        self._retry(lambda: self._cur_sm().GetTargetNetId(), "ITcSysManager check")

    def _on_sm(self, op: Callable[[Any], Any], what: str, once: bool = False) -> Any:
        """op(System Manager) through _retry, the System Manager taken in every attempt. An action (once) is read for
        the System Manager's messages (_sm_messages_of)."""
        self._sm()                         # N116: the bound solution, checked once per call
        if not once:
            return self._retry(lambda: op(self._cur_sm()), what, once)
        self._live_sm()
        with self._sm_messages_of(what):
            return self._retry(lambda: op(self._cur_sm()), what, once)

    def _on_item(self, path: str, op: Callable[[Any], Any], what: str, once: bool = False) -> Any:
        """op(tree item at path) through _retry, the item looked up in every attempt — a refreshed System Manager or a
        re-attached shell gives a live item, never the stale one of the first attempt. An action (once) is read for
        the System Manager's messages (_sm_messages_of)."""
        self._sm()
        if not once:
            return self._retry(lambda: op(self._cur_sm().LookupTreeItem(path)), what, once)
        self._live_sm()
        with self._sm_messages_of(what):
            return self._retry(lambda: op(self._cur_sm().LookupTreeItem(path)), what, once)

    # TE1000 ITcSysManager2::GetLastErrorMessages: "Returns the error and warning messages accumulated in the internal
    # buffer and then clears the buffer. Many methods log additional diagnostic messages here, even if their HRESULT is
    # S_OK" (the current page; the TwinCAT 2.11 page says only "returns the last error messages"). Whether a build's
    # read clears it is not assumed: a buffer that does not clear starts with what the read before the action gave.
    MESSAGES_MAX = 4000                    # characters of one action's messages kept (the lines: 50)
    MESSAGES_READ_S = 2.0                  # how long a busy shell is asked for them (an addition: never a wait)

    def _sm_messages(self, sm: Any) -> str | None:
        """The message buffer of the System Manager `sm` ('' when empty); None when it can't be read: no ITcSysManager2,
        a shell that keeps rejecting the read for MESSAGES_READ_S (review of 0.8.16: the member check waited the whole
        com_retry_s), or `sm` is no longer the System Manager in use — the shell went away during the action and the
        server re-attached: the new shell's buffer is not this action's (review of 0.8.16, it was read and cleared)."""
        if sm is None:
            self._msg_why = "no System Manager"
            return None
        if self.sysman is not sm:
            self._msg_why = "the shell went away during the action and was re-attached — its buffer is not this action's"
            return None
        if getattr(self, "_msg_sm", None) is not sm:
            # what is known of this System Manager's buffer: whether it has one, whether a read clears it
            self._msg_sm, self._msg_has, self._msg_clears = sm, None, None
        try:
            if self._msg_has is None:
                self._msg_has = bool(self._retry(lambda: _has_prop(sm, "GetLastErrorMessages"), "GetLastErrorMessages?",
                                                 retry_s=self.MESSAGES_READ_S))
            if not self._msg_has:
                self._msg_why = "this System Manager has no GetLastErrorMessages (ITcSysManager2)"
                return None
            return str(self._retry(lambda: _com_call(sm, "GetLastErrorMessages"), "GetLastErrorMessages",
                                   retry_s=self.MESSAGES_READ_S) or "")
        except XaeError as exc:
            self._msg_why = f"the read failed ({str(exc)[:160]})"
            return None

    @contextlib.contextmanager
    def _sm_messages_of(self, what: str) -> Iterator[None]:
        """What the System Manager logged during one action (activate, restart, ConsumeXml, CreateChild, DeleteChild,
        PlcOpenImport, GenerateBootProject, an online command, the build): the buffer is read before the action — what
        it held came from earlier steps — and after it, on success as on failure, from the same System Manager. A success
        records them (self.messages → the result's xae_messages); a failure's error carries them. A call the shell
        rejected (XaeBusy) or a shell that went away (XaeGone) ran nothing that could be read.

        Whether a read clears the buffer (TE1000's current page: it does; the TwinCAT 2.11 page does not say) is read
        from the buffer itself, once per System Manager: the first time it holds something, a second read follows — empty
        means it clears. Review of 0.8.16: 'the after-read starts with the before-read, so it did not clear' dropped the
        messages of an action that logged the same text again."""
        sm = self.sysman
        before = self._sm_messages(sm)
        if before and getattr(self, "_msg_clears", None) is None:
            again = self._sm_messages(sm)
            if again is not None:
                self._msg_clears = again == ""
        try:
            yield
        except (XaeBusy, XaeGone):
            raise
        except XaeError as exc:
            rec = self._sm_messages_after(what, before, sm, failed=True)
            if rec and rec.get("messages"):
                raise type(exc)(f"{exc} — XAE messages ({what}): {rec['messages'][:1500]}") from exc
            # N188 (0.8.16 retest): an error without XAE messages did not say whether the buffer was read
            last = self.msg_reads[-1] if getattr(self, "msg_reads", None) and self.msg_reads[-1]["action"] == what else None
            if last is not None:
                raise type(exc)(f"{exc} — XAE messages ({what}): " + (
                    "none, the buffer was read and is empty" if last["read"] else f"not read: {last['why']}")) from exc
            raise
        self._sm_messages_after(what, before, sm)

    def _sm_messages_after(self, what: str, before: str | None, sm: Any, failed: bool = False) -> dict[str, Any] | None:
        after = self._sm_messages(sm)
        reads = getattr(self, "msg_reads", None)
        if after is None:
            if isinstance(reads, list):
                reads.append({"action": what, "read": False, "why": self._msg_why or "not known why"})
            return None
        rec: dict[str, Any] = {"action": what}
        if failed:
            rec["failed"] = True
        clears = getattr(self, "_msg_clears", None)
        if before and before.strip():
            # a buffer known not to clear: what the read before gave is history. Unknown (the second read was rejected):
            # taken as TE1000 documents it — cleared; re-review of 0.8.16: the old 'starts with it' guess lost an action
            # that logged the same text again
            if clears is False and after.startswith(before):
                after = after[len(before):]
            else:
                rec["earlier_messages"] = before.strip()[:self.MESSAGES_MAX]
                rec["earlier_note"] = "in the buffer before this action: logged by earlier steps, not by it"
        after = after.strip()
        if after:
            rec["messages"] = after[:self.MESSAGES_MAX]
            rec["lines"] = [ln.strip() for ln in after.splitlines() if ln.strip()][:50]
        if isinstance(reads, list):
            reads.append({"action": what, "read": True, "empty": not rec.get("messages")})
        if not rec.get("messages") and not rec.get("earlier_messages"):
            return None
        self.messages.append(rec)
        return rec

    def _on_dte(self, op: Callable[[Any], Any], what: str, once: bool = False) -> Any:
        """op(DTE) through _retry, the DTE taken in every attempt (a dead proxy is replaced by _retry's re-attach)."""
        self._need()
        return self._retry(lambda: op(self.dte), what, once)

    def mismatch_note(self) -> str | None:
        """N116: the solution the xae_* tools work on is not the configured xae.solution_path (xae_attach(solution_path=
        …) chose another) — said in every result, not only in xae_attach's."""
        if self.dte is None or not self.bound_solution or not self.cfg.solution_path:
            return None
        if _norm_sol(self.bound_solution) == _norm_sol(self.cfg.solution_path):
            return None
        return (f"working on {self.bound_solution}, not the configured xae.solution_path ({self.cfg.solution_path}) — "
                "until xae_attach() without a path")

    # ------------------------------------------------------------ attach
    def _rot_candidates(self) -> list[dict[str, Any]]:  # pragma: no cover
        rot = pythoncom.GetRunningObjectTable()
        ctx = pythoncom.CreateBindCtx(0)
        out = []
        for moniker in rot.EnumRunning():
            try:
                name = moniker.GetDisplayName(ctx, None)
            except Exception:  # noqa: BLE001
                continue
            if not (name.startswith("!TcXaeShell.DTE") or name.startswith("!VisualStudio.DTE")):
                continue
            progid = name[1:].split(":", 1)[0]
            if self.cfg.progids and progid not in self.cfg.progids:
                continue
            try:
                obj = rot.GetObject(moniker)
                dte = win32com.client.Dispatch(obj.QueryInterface(pythoncom.IID_IDispatch))
                sol = ""
                try:
                    # N174: a busy shell is asked again — its solution read as '' chose another shell or none
                    sol = self._retry(lambda: dte.Solution.FullName, f"{name}: Solution.FullName")
                except XaeError:
                    pass
                out.append({"progid": progid, "moniker": name, "dte": dte, "solution": sol})
            except Exception:  # noqa: BLE001
                continue
        return out

    def attach(self, progid: str | None = None, solution_path: str | None = None,
               launch: bool | None = None, _internal: bool = False, _for_open: bool = False) -> dict[str, Any]:
        """Attach to the shell that has the solution: solution_path (explicit — it becomes the solution this server
        works on), else xae.solution_path, else (an internal re-attach) the bound one, else the one open in the only
        shell. N116: a named or configured solution that no shell has is never replaced by another one — refused (or
        a shell launched with it, launch=true); several shells with different solutions and none named: refused."""
        self._require_win()
        explicit = bool(solution_path)
        # review: xae_status / xae_attach() without a path attached to whatever was open when the solution bound at
        # runtime (none configured) had been closed — the bound solution counts there too; xae_attach() without a path
        # goes back to xae.solution_path when one is configured
        want_sol = (solution_path or (self.bound_solution if _internal else None) or self.cfg.solution_path
                    or self.bound_solution or "")
        want_sol_norm = _norm_sol(want_sol)
        cands = [c for c in self._rot_candidates() if not progid or c["progid"] == progid]
        order = {p: i for i, p in enumerate(self.cfg.progids)}
        cands.sort(key=lambda c: order.get(c["progid"], 99))
        pick = None
        if want_sol_norm:
            for c in cands:
                if c["solution"] and _norm_sol(c["solution"]) == want_sol_norm:
                    pick = c
                    break
            if pick is None and _for_open:
                # xae_open_solution: a shell with no solution open takes it (the solution is opened into it next)
                pick = next((c for c in cands if not str(c["solution"] or "").strip()), None)
        else:
            sols = {_norm_sol(c["solution"]) for c in cands if c["solution"]}
            if len(sols) > 1:
                raise XaeError(f"{len(cands)} XAE shells with different solutions are open "
                               f"({[c['solution'] or '(none)' for c in cands]}) and xae.solution_path is not set — "
                               "nothing was chosen. xae_attach(solution_path=…) with the one to work on, or set "
                               "xae.solution_path in twincat_mcp.toml")
            if cands:
                pick = next((c for c in cands if c["solution"]), cands[0])    # the shell that has it, not an empty one
        launched = False
        if pick is None:
            do_launch = (not self.cfg.attach_only) if launch is None else launch
            if not do_launch:
                if want_sol_norm and cands and _for_open:
                    raise XaeError(
                        f"no XAE shell is free to open {want_sol}: the open shells have "
                        f"{[c['solution'] or '(no solution)' for c in cands]}, and opening it there would close that "
                        "solution — nothing was done. Open it in XAE yourself, close a solution first, or "
                        "xae_attach(launch=true) to start a shell for it.")
                if want_sol_norm and cands:
                    raise XaeError(
                        f"the solution {want_sol} is not open in any XAE shell (open: "
                        f"{[c['solution'] or '(no solution)' for c in cands]}) — nothing was attached: the tools would act "
                        "on another project and target. Open it in XAE, or xae_attach(launch=true) to start a shell with it, "
                        "or xae_attach(solution_path=…) to work on another solution.")
                raise XaeError(
                    "No running XAE shell / Visual Studio with TwinCAT found in the ROT "
                    f"(looked for {list(self.cfg.progids)}). Open the solution in XAE, or call "
                    "xae_attach(launch=true) to start a shell (slow, 20-60 s)."
                )
            last = None
            for pid in ([progid] if progid else self.cfg.progids):
                try:
                    dte = self._retry(lambda: win32com.client.Dispatch(pid), f"launch {pid}")
                    pick = {"progid": pid, "moniker": None, "dte": dte, "solution": ""}
                    launched = True
                    break
                except XaeError as exc:
                    last = exc
            if pick is None:
                raise XaeError(f"could not launch any of {list(self.cfg.progids)}: {last}")
        self._take(pick)
        if launched:
            # N174: through _retry — a shell that is still starting rejects calls, and these were then never set
            for what, op in (("SuppressUI", lambda: setattr(self.dte, "SuppressUI", False)),
                             ("MainWindow.Visible", lambda: setattr(self.dte.MainWindow, "Visible", bool(self.cfg.visible))),
                             ("UserControl", lambda: setattr(self.dte, "UserControl", True))):   # keep alive when we release
                try:
                    self._retry(op, what)
                except XaeError as exc:
                    log.warning("launched shell: %s not set (%s)", what, exc)
        if want_sol:
            self.bound_solution = want_sol
            if launched and not _for_open:
                self.open_solution(want_sol)
        elif str(pick.get("solution") or "").strip():
            self.bound_solution = pick["solution"]       # the only one open: from now on this one, no other
        info = self.status()
        info["launched"] = launched
        info["candidates"] = [{"progid": c["progid"], "solution": c["solution"], "moniker": c["moniker"]} for c in cands]
        if explicit and self.cfg.solution_path and _norm_sol(solution_path) != _norm_sol(self.cfg.solution_path):
            info["solution_mismatch"] = True
            info["solution_note"] = (f"attached to {solution_path}, not the configured xae.solution_path "
                                     f"({self.cfg.solution_path}) — every xae_* tool works on {solution_path} until "
                                     "xae_attach() without a path")
        return info

    def status(self) -> dict[str, Any]:
        if self.dte is None:
            return {"attached": False, "platform": sys.platform, "progids": list(self.cfg.progids)}
        not_open = self._check_bound(raise_=False)        # N116: re-attaches when it is open in a shell again
        failed = getattr(self, "bound_check_failed", None)
        if self.dte is None:                              # the shell went away and no re-attach was possible
            return {"attached": False, "platform": sys.platform, "progids": list(self.cfg.progids),
                    **({"solution_bound": self.bound_solution} if self.bound_solution else {}),
                    "reason": failed or "the XAE shell is not reachable"}
        d: dict[str, Any] = {"attached": True, "progid": self.progid}
        if failed:
            d["solution_check_error"] = failed           # which solution the shell has could not be read
        if self.bound_solution:
            d["solution_bound"] = self.bound_solution
        if self.reattach_note:
            d["reattached"] = self.reattach_note
            self.reattach_note = None
        if not_open:
            d["solution_mismatch"] = True
            d["bound_solution_open"] = False
            d["refused"] = not_open
        try:
            d["dte_version"] = self._retry(lambda: self.dte.Version, "DTE.Version")
            d["edition"] = self._retry(lambda: self.dte.Edition, "DTE.Edition")
        except XaeError as exc:
            d["dte_error"] = str(exc)
        try:
            # N174: every read through _retry — a rejected one gave solution_error and then 'no solution open'
            d["solution"] = self._retry(lambda: self.dte.Solution.FullName, "Solution.FullName")
            d["solution_open"] = bool(self._retry(lambda: self.dte.Solution.IsOpen, "Solution.IsOpen"))
            prjs = []
            n = int(self._retry(lambda: self.dte.Solution.Projects.Count, "Solution.Projects.Count"))
            for i in range(1, n + 1):
                p = self._retry(lambda i=i: self.dte.Solution.Projects.Item(i), f"Solution.Projects.Item({i})")
                prjs.append({"name": self._retry(lambda p=p: p.Name, f"Project {i} Name"),
                             "file": self._get(lambda p=p: p.FullName, f"Project {i} FullName", ""),
                             "kind": self._get(lambda p=p: p.Kind, f"Project {i} Kind", "")})
            d["projects"] = prjs
        except (XaeBusy, XaeGone) as exc:
            d["solution_error"] = str(exc)               # busy / gone: nothing is said about the solution
            return d
        except XaeError as exc:
            d["solution_error"] = str(exc)
        if not d.get("solution_open") or not str(d.get("solution") or "").strip():
            # no solution (IsOpen false, or the shell's empty start state with FullName ""): say so instead of a
            # stale project name and a raw COM exception (retest 0.7.1, 0.8.3)
            self.sysman = None
            d["solution_open"] = False
            d.pop("solution_error", None)
            d["note"] = "no solution open in the shell — xae_open_solution(path) or open one in XAE"
            return d
        if not_open:
            return d                                     # the other solution's TwinCAT project is not this server's
        try:
            # N120: through _retry with the System Manager taken at call time — a reopened solution is found again
            d["target_net_id"] = self._on_sm(lambda sm: sm.GetTargetNetId(), "GetTargetNetId")
            d["twincat_project"] = self._sysman_project_name
            d["twincat_started"] = bool(self._on_sm(lambda sm: sm.IsTwinCATStarted(), "IsTwinCATStarted"))
        except (XaeBusy, XaeGone) as exc:
            d["twincat_error"] = str(exc)[:400]          # N174: busy is not 'no TwinCAT project'
        except Exception as exc:  # noqa: BLE001
            if not any("tsproj" in str(p.get("file", "")).lower() or p.get("name") for p in d.get("projects") or []):
                d["note"] = "the open solution has no TwinCAT project"
            else:
                d["note"] = "the TwinCAT project of the open solution is not reachable through the Automation Interface"
            d["detail"] = _com_msg(exc)[:200]
        return d

    def _find_sysman(self) -> None:
        self._need()
        # N174: every read through _retry — a rejected p.Object was skipped as 'not a TwinCAT project', and the raw
        # Solution / Projects.Item reads let the rejection escape
        n = self._retry(lambda: self.dte.Solution.Projects.Count, "Projects.Count")
        _no = object()
        for i in range(1, n + 1):
            p = self._retry(lambda i=i: self.dte.Solution.Projects.Item(i), f"Projects.Item({i})")
            obj = self._get(lambda p=p: p.Object, f"Project {i} Object")
            if obj is None:
                continue
            if self._get(lambda o=obj: o.GetTargetNetId(), f"Project {i} GetTargetNetId", _no) is _no:
                continue                     # not an ITcSysManager
            self.sysman = obj
            self._sysman_project_name = self._get(lambda p=p: p.Name, f"Project {i} Name")
            self._sysman_solution = self._get(lambda: self.dte.Solution.FullName, "Solution.FullName")
            return
        raise XaeError("no TwinCAT (ITcSysManager) project found in the open solution")

    # ------------------------------------------------------------ solution
    def open_solution(self, path: str) -> dict[str, Any]:
        if not os.path.isfile(path):
            raise XaeError(f"solution not found: {path}")
        if self.dte is None:
            self.attach(solution_path=path, _for_open=True)
        # N174: through _retry (a raw read let a rejection escape)
        if _norm_sol(str(self._retry(lambda: self.dte.Solution.FullName, "Solution.FullName") or "")) != _norm_sol(path):
            self._retry(lambda: self.dte.Solution.Open(path), "Solution.Open")
        self.bound_solution = path                        # N116: the solution asked for is the one worked on
        self.sysman = None
        # wait for projects to load (a rejected read is a shell still loading: asked again in the next round)
        for _ in range(120):
            try:
                if self.dte.Solution.Projects.Count > 0:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        return self.status()

    def _dirty(self, save: bool = False) -> list[str]:
        """Unsaved documents and projects the shell reports (Document.Saved / Project.Saved == False); with
        ``save`` each one is saved on its own (the second route when File.SaveAll left some, D44)."""
        self._need()
        out: list[str] = []
        get = self._get          # N174: every read through _retry — a rejected Saved read counted as 'saved' (D44)

        def name_of(x: Any) -> str:
            for a in ("FullName", "UniqueName", "Name"):
                v = str(get(lambda a=a: getattr(x, a), f"unsaved item {a}", "") or "")
                if v:
                    return v
            return "?"

        def check(x: Any) -> None:
            saved = get(lambda: bool(x.Saved), "Saved")
            if saved is None or saved:
                return                                    # no Saved property (as before), or saved
            if save:
                try:
                    self._retry(lambda: x.Save(), "Save")
                    if get(lambda: bool(x.Saved), "Saved"):
                        return
                except (XaeBusy, XaeGone):
                    raise
                except XaeError:
                    pass
            out.append(name_of(x))
        docs = get(lambda: self.dte.Documents, "DTE.Documents")
        if docs is not None:
            for i in range(1, int(get(lambda: docs.Count, "Documents.Count", 0) or 0) + 1):
                d = get(lambda i=i: docs.Item(i), f"Documents.Item({i})")
                if d is not None:
                    check(d)

        budget = [3000]                                   # COM calls are slow: cap the walk

        def walk(coll: Any, depth: int) -> None:
            n = get(lambda: int(coll.Count), "Count")
            for i in range(1, (n or 0) + 1):
                if budget[0] <= 0:
                    return
                budget[0] -= 1
                x = get(lambda i=i: coll.Item(i), f"Item({i})")
                if x is None:
                    continue
                check(x)                                  # Project or ProjectItem (both have Saved)
                sub = get(lambda: x.SubProject, "SubProject")       # a nested project (the PLC project in a .tsproj)
                if sub is not None:
                    check(sub)
                if depth < 6:
                    items = get(lambda: (sub or x).ProjectItems, "ProjectItems")
                    if items is not None:
                        walk(items, depth + 1)
        projs = get(lambda: self.dte.Solution.Projects, "Solution.Projects")
        if projs is not None:
            walk(projs, 0)
        return list(dict.fromkeys(out))

    def save_all(self) -> dict[str, Any]:
        """File.SaveAll, then whatever the shell still reports unsaved is saved one by one (D44: SaveAll from the
        Automation Interface did not always persist text set through DeclarationText/ImplementationText)."""
        self._need()
        before = self._dirty()
        self._on_dte(lambda dte: dte.ExecuteCommand("File.SaveAll"), "File.SaveAll")
        after = self._dirty()
        if after:
            after = self._dirty(save=True)
        return {"saved": not after, "dirty_before": before, "dirty_after": after}

    def close_solution(self, save: bool = True) -> dict[str, Any]:
        self._on_dte(lambda dte: dte.Solution.Close(save), "Solution.Close", once=True)
        self.sysman = None
        return {"closed": True}

    # ------------------------------------------------------------ build
    def build(self, clean: bool = False, rebuild: bool = False, timeout_s: float | None = None) -> dict[str, Any]:
        self._need()
        self._clear_error_list()
        t0 = time.time()
        if clean:
            self._on_dte(lambda dte: dte.Solution.SolutionBuild.Clean(True), "SolutionBuild.Clean")
        if rebuild:
            self._on_dte(lambda dte: dte.Solution.SolutionBuild.Clean(True), "SolutionBuild.Clean")
        try:
            self._sm()                     # review of 0.8.16: the System Manager whose messages the build may log —
        except (XaeBusy, XaeGone):         # read only when it happened to be cached
            raise
        except XaeError:
            pass                           # no TwinCAT project in the solution: the build runs without them
        with self._sm_messages_of("SolutionBuild.Build"):
            self._on_dte(lambda dte: dte.Solution.SolutionBuild.Build(True), "SolutionBuild.Build")
        # N174 (0.8.14 retest): `self.dte.Solution.SolutionBuild` was read here outside _retry — right after a build the
        # shell rejects calls, and the rejection left xae_build / xae_deploy as an 'internal error' (crash 4ee44bfb,
        # 388c3b88). Every read goes through _retry; the build itself is not repeated.
        # Build(True) waits, but belt and braces: poll BuildState (3 = done)
        deadline = time.time() + (timeout_s or self.cfg.build_timeout_s)
        while time.time() < deadline:
            state = self._get(lambda: int(self.dte.Solution.SolutionBuild.BuildState), "SolutionBuild.BuildState")
            if state != 2:                   # vsBuildStateInProgress == 2 (NotStarted=1, Done=3); None: not readable
                break
            time.sleep(0.5)
        failed = self._get(lambda: int(self.dte.Solution.SolutionBuild.LastBuildInfo), "SolutionBuild.LastBuildInfo")
        errs = self.errors()
        out_tail = self.build_output(tail_lines=80)
        n_err = sum(1 for e in errs["items"] if e["level"] == "error")
        n_warn = sum(1 for e in errs["items"] if e["level"] == "warning")
        return {
            "success": (failed == 0) if failed is not None else (n_err == 0),
            "failed_projects": failed,
            "duration_s": round(time.time() - t0, 1),
            "errors": n_err,
            "warnings": n_warn,
            "items": errs["items"][:200],
            "error_source": errs["source"],
            "output_tail": out_tail["text"],
        }

    def _clear_error_list(self) -> None:
        # N174: through _retry — a rejected Clear left the last build's entries in this build's result (N82)
        self._get(lambda: self.dte.ExecuteCommand("OtherContextMenus.ErrorList.Clear"), "ErrorList.Clear")

    def _dte2(self) -> Any:  # pragma: no cover
        """Best effort DTE -> DTE2 (needed for ToolWindows)."""
        try:
            mod = win32com.client.gencache.EnsureModule(*ENVDTE80_TYPELIB)
            if mod is not None:
                return win32com.client.CastTo(self.dte, "DTE2")
        except Exception:  # noqa: BLE001
            pass
        return None

    def _error_items(self, view_copy: bool = False, pane_from: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """Return (items, source). D20: the Error List is what the operator sees, so it is read first and by every
        route there is — DTE2.ToolWindows.ErrorList (late-bound: the cached 7.0 gen_py wrapper has no ToolWindows),
        the ErrorList window object, and when those report nothing a copy of the Error List view itself (its
        TwinCAT entries — Scope, Measurement, system messages — are not always in ErrorItems). The Build output
        pane is parsed as well and merged in, so a source that misses items does not hide them."""
        self._need()
        lvl = {1: "error", 2: "warning", 3: "message", 4: "message"}
        found: list[dict[str, Any]] = []
        used: list[str] = []

        def take(items: list[dict[str, Any]], name: str) -> None:
            if items:
                used.append(f"{name}({len(items)})")
                found.extend(items)

        def enum(coll: Any) -> list[dict[str, Any]]:
            return [self._error_item(coll.Item(i), lvl) for i in range(1, int(coll.Count) + 1)]
        got = False
        for name, get in (("DTE2.ToolWindows", lambda: self._dte2_late().ToolWindows.ErrorList.ErrorItems),
                          ("DTE2.gen_py", lambda: self._dte2().ToolWindows.ErrorList.ErrorItems),
                          ("Windows.ErrorList", lambda: self.dte.Windows.Item(VS_WINDOW_ERRORLIST).Object.ErrorItems)):
            # N174: each route through _retry — a rejected read moved on to the next route (or to none)
            items = self._get(lambda get=get: enum(get()), f"Error List ({name})")
            if items is None:
                continue
            take(items, name)
            got = got or bool(items)
            if items:
                break
        if not got and view_copy:              # moves the focus to the Error List: only when diagnostics ask for it
            try:
                take(self._error_list_copy(), "ErrorList(view copy)")
            except (XaeBusy, XaeGone):
                raise
            except Exception:  # noqa: BLE001
                pass
        try:
            if pane_from is not None:
                # only what this command wrote: the pane's text after the marker (its text before the command) —
                # or all of it when the command cleared or rewrote the pane (review of N82: a line count kept from
                # before dropped the head of a pane that was cleared and refilled)
                text = self._pane_text() or ""
                text = text[len(pane_from):] if text.startswith(pane_from) else text
            else:
                text = self.build_output(tail_lines=2000)["text"]
            out = _parse_build_output(text)
        except (XaeBusy, XaeGone):
            raise
        except Exception:  # noqa: BLE001
            out = []
        seen = {(i.get("description", "").strip(), str(i.get("file", "")).lower(), int(i.get("line") or 0)) for i in found}
        extra = [i for i in out if (i["description"].strip(), i["file"].lower(), i["line"]) not in seen]
        take(extra, "OutputPane")
        return found, " + ".join(used) if used else "none (Error List empty, nothing in the Build pane)"

    def _dte2_late(self) -> Any:  # pragma: no cover
        """DTE as a late-bound dispatch: members of the newer object model (ToolWindows) resolve by name."""
        import win32com.client.dynamic  # type: ignore
        return win32com.client.dynamic.Dispatch(self.dte._oleobj_)

    def _error_list_copy(self) -> list[dict[str, Any]]:  # pragma: no cover — needs the shell and a clipboard
        """Select all in the Error List and copy it (tab-separated, header row); the user's clipboard text is put back."""
        import win32clipboard  # type: ignore
        self._need()
        saved = None
        try:
            win32clipboard.OpenClipboard()
            try:
                saved = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            except Exception:  # noqa: BLE001
                saved = None
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:  # noqa: BLE001
                pass
        try:
            # N174: through _retry
            self._retry(lambda: self.dte.Windows.Item(VS_WINDOW_ERRORLIST).Activate(), "Error List Activate")
            self._retry(lambda: self.dte.ExecuteCommand("Edit.SelectAll"), "Edit.SelectAll")
            self._retry(lambda: self.dte.ExecuteCommand("Edit.Copy"), "Edit.Copy")
            time.sleep(0.2)
            win32clipboard.OpenClipboard()
            try:
                text = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
        finally:
            if saved is not None:
                try:
                    win32clipboard.OpenClipboard()
                    win32clipboard.EmptyClipboard()
                    win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, saved)
                finally:
                    win32clipboard.CloseClipboard()
        return _parse_error_list_copy(text or "")

    def _error_item(self, it: Any, lvl: dict[int, str]) -> dict[str, Any]:
        def g(name: str, default: Any = "") -> Any:
            return self._get(lambda: getattr(it, name), f"ErrorItem.{name}", default)     # N174
        return {
            "level": lvl.get(int(g("ErrorLevel", 3) or 3), "message"),
            "description": g("Description"),
            "file": g("FileName"),
            "line": g("Line", 0),
            "column": g("Column", 0),
            "project": g("Project"),
        }

    def errors(self, view_copy: bool = False, pane_from: str | None = None) -> dict[str, Any]:
        items, source = self._error_items(view_copy, pane_from)
        return {"count": len(items), "source": source, "items": items}

    def _pane_text(self) -> str | None:
        """The Build output pane's whole text now (the marker: what a command adds comes after it); None unreadable."""
        try:
            return self._build_pane()
        except (XaeBusy, XaeGone):
            raise                          # N174: a busy shell is not 'no pane'
        except Exception:  # noqa: BLE001
            return None

    def _build_pane(self) -> str:
        self._need()

        def read() -> str:
            pane = self.dte.Windows.Item(VS_WINDOW_OUTPUT).Object.OutputWindowPanes.Item("Build")
            sel = pane.TextDocument.Selection
            sel.StartOfDocument(False)
            sel.EndOfDocument(True)
            return sel.Text or ""
        return self._retry(read, "Output window, Build pane")              # N174

    def build_output(self, tail_lines: int = 200) -> dict[str, Any]:
        self._need()                       # not attached: an error, as before — only the pane's own failure is a result
        try:
            text = self._build_pane()
        except (XaeBusy, XaeGone):
            raise                          # N174: a busy shell is said, not an empty pane
        except Exception as exc:  # noqa: BLE001
            return {"text": "", "error": _com_msg(exc.__cause__ or exc)}
        lines = text.splitlines()
        return {"text": "\n".join(lines[-tail_lines:]), "total_lines": len(lines)}

    # ------------------------------------------------------------ commands
    def command(self, name: str, args: str = "") -> dict[str, Any]:
        self._on_dte(lambda dte: dte.ExecuteCommand(name, args), f"ExecuteCommand {name}", once=True)
        return {"executed": name, "args": args}

    def list_commands(self, contains: str, limit: int = 200) -> dict[str, Any]:
        self._need()
        # N174: through _retry (Commands / Count were read raw; a rejected item was skipped as if it had no name)
        cmds = self._retry(lambda: self.dte.Commands, "DTE.Commands")
        needle = contains.lower()
        out = []
        n = int(self._retry(lambda: cmds.Count, "Commands.Count"))
        for i in range(1, n + 1):
            nm = self._get(lambda i=i: cmds.Item(i).Name, f"Commands.Item({i}).Name")
            if nm and needle in nm.lower():
                out.append(nm)
                if len(out) >= limit:
                    break
        return {"count": len(out), "commands": out}

    # 4026 shells name the PLC commands after the *active* PLC project: PLC.LogintoUntitled1,
    # PLC.LogoutfromUntitled1, PLC.StartUntitled1 … and PLC.Logintonone when no project is active.
    _PLC_VERB = {"login": "Login", "logout": "Logout", "start": "Start", "stop": "Stop", "reset_cold": "ResetCold",
                 "reset_origin": "ResetOrigin", "download": "Download", "online_change": "Login"}

    def _dynamic_plc_commands(self, action: str) -> list[str]:
        verb = self._PLC_VERB.get(action, "").lower()
        if not verb:
            return []
        names = self.list_commands("PLC.", 500).get("commands", [])
        out = []
        for nm in names:
            low = nm.lower()
            if not low.startswith("plc." + verb):
                continue
            if low.startswith(("plc.loginto", "plc.logoutfrom")) and verb in ("login", "logout") \
                    or low == "plc." + verb or (low.startswith("plc." + verb) and verb not in ("login", "logout")):
                out.append(nm)
        # the real project first, the 'none' placeholder last
        return sorted(out, key=lambda n: (n.lower().endswith("none"), n))

    def _select_plc_project_node(self, project_name: str | None = None) -> str | None:
        """Make the named PLC project the active one by selecting its node in Solution Explorer (what a click does).
        Solution Explorer nests it as Solution → <TwinCAT project> → PLC → <PLC> → '<PLC> Project'; the node is
        found by walking the UI hierarchy (fixed paths missed the TwinCAT project level — retest #10). N41: the
        project is the one the call is for, never simply the first one listed."""
        get, retry = self._get, self._retry        # N174: every UI hierarchy access through _retry
        try:
            self._need()
            if project_name:
                pname = project_name.removesuffix(" Project")
            else:
                projs = self.plc_projects().get("projects") or []
                if len(projs) != 1:
                    return None
                pname = str(projs[0].get("name") or "").removesuffix(" Project")
            # #10: the cached 7.0 gen_py DTE wrapper has no ToolWindows (D20) — late-bound first, then the window object
            se = None
            for route in (lambda: self._dte2_late().ToolWindows.SolutionExplorer,
                          lambda: self.dte.ToolWindows.SolutionExplorer,
                          lambda: self.dte.Windows.Item(VS_WINDOW_SOLUTION_EXPLORER).Object):
                se = get(route, "Solution Explorer")
                if se is not None and get(lambda: se.UIHierarchyItems.Count, "Solution Explorer items") is not None:
                    break
                se = None
            if se is None:
                return None

            def walk(items, depth: int, trail: list[str]):
                if depth > 7:
                    return None
                for i in range(1, int(retry(lambda: items.Count, "UIHierarchyItems.Count")) + 1):
                    it = retry(lambda i=i: items.Item(i), f"UIHierarchyItems.Item({i})")
                    name = str(retry(lambda: it.Name, "UIHierarchyItem.Name"))
                    if name.lower() == f"{pname} Project".lower():
                        return it, trail + [name]
                    sub = get(lambda: it.UIHierarchyItems, "UIHierarchyItems")
                    if sub is None:
                        continue
                    was = get(lambda: sub.Expanded, "UIHierarchyItems.Expanded")
                    try:
                        retry(lambda: setattr(sub, "Expanded", True), "UIHierarchyItems.Expanded")
                    except (XaeBusy, XaeGone):
                        raise
                    except XaeError:
                        continue
                    hit = walk(sub, depth + 1, trail + [name])
                    if hit is not None:
                        return hit
                    if was is not None:
                        get(lambda: setattr(sub, "Expanded", was), "UIHierarchyItems.Expanded")
                return None
            hit = walk(retry(lambda: se.UIHierarchyItems, "Solution Explorer items"), 0, [])
            if hit is None:
                return None
            item, trail = hit
            retry(lambda: item.Select(1), "UIHierarchyItem.Select")   # vsUISelectionTypeSelect
            # and, where the shell offers it, the explicit 'Set as Active PLC Project' on the selected node
            for c in self.list_commands("active", 100).get("commands", []):
                if re.search(r"set\s*as\s*active\s*plc\s*project|setactiveplcproject", c, re.I):
                    try:
                        self.command(c)
                        trail.append(f"[{c}]")
                    except XaeError:
                        pass
                    break
            return "\\".join(trail)
        except (XaeBusy, XaeGone):
            raise                              # N174: a busy shell is said, not 'the node could not be selected'
        except Exception:  # noqa: BLE001
            return None

    # ITcSmTreeItem.ConsumeXml on the nested PLC project (Automation Interface ≥ 3.1.4010, "Handling online
    # functionalities"): no active project, no Solution Explorer, no UI language involved
    _PLC_ONLINE_XML = {"login": "LoginCmd", "logout": "LogoutCmd", "start": "StartCmd", "stop": "StopCmd",
                       "reset_cold": "ResetColdCmd", "reset_origin": "ResetOriginCmd"}

    @contextlib.contextmanager
    def silent(self, what: str) -> Iterator[dict[str, Any]]:
        """xae.silent_mode: TcAutomationSettings.SilentMode on for one confirmed machine command, back after it."""
        note: dict[str, Any] = {}
        settings = prev = None
        if self.cfg.silent_mode and self.dte is not None:
            # N174: through _retry — a rejected read left silent mode off for the command (its dialogs then open);
            # a shell that stays busy stops here, before the command
            try:
                settings = self._retry(lambda: self.dte.GetObject("TcAutomationSettings"), "TcAutomationSettings")
                prev = bool(self._retry(lambda: settings.SilentMode, "SilentMode"))
                self._retry(lambda: setattr(settings, "SilentMode", True), "SilentMode")
                note["silent_mode"] = True
            except (XaeBusy, XaeGone):
                raise
            except XaeError as exc:
                settings = None
                cause = exc.__cause__ or exc
                note["silent_mode"] = f"not available ({_com_msg(cause) if hasattr(cause, 'hresult') else cause})"
        try:
            yield note
        finally:
            if settings is not None:
                try:
                    self._retry(lambda: setattr(settings, "SilentMode", prev), "SilentMode")
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s: SilentMode could not be switched back to %s: %s", what, prev, exc)

    ONLINE_STATE_WAIT_S = 5.0      # how long the state an ITcPlcOnline command leads to is awaited

    def _online_state(self, it: Any) -> dict[str, Any]:
        # N174: through _retry — a rejected read gave 'state unknown' (and Start / Stop went on as if logged in)
        st = self._get(lambda: int(it.OnlineApplicationState), "OnlineApplicationState")
        return {"logged_in": self._get(lambda: bool(it.IsLoggedIn), "IsLoggedIn"),
                "application_state": _APP_STATE.get(st, st) if st is not None else None}

    def _has(self, obj: Any, name: str) -> bool:
        """N174: does the late-bound object offer `name` — asked through _retry (a rejected GetIDsOfNames read as 'no
        such member', and the ITcPlcOnline route was skipped on a busy shell)."""
        return bool(self._retry(lambda: _has_prop(obj, name), f"{name}?"))

    def _plc_online(self, target: str, action: str) -> dict[str, Any] | None:
        """ITcPlcOnline on the nested project (TE1000): Login(flags) / Logoff / Start / Stop / Reset(type), the state
        read back after it. None when the project does not offer it (older XAE) — the other routes then apply."""
        it = self._item(target)
        if not self._has(it, "IsLoggedIn"):
            return None
        before = self._online_state(it)
        if before["logged_in"] is False and (action in ("start", "stop") or action in _RESET_TYPE):
            # 0.8.12 retest: Stop while logged out returned and did nothing; the error then said only that the state
            # did not change. Start / Stop / Reset act on the online session of the project
            raise _NeedsLogin(f"{target} is not logged in — {action} acts on the project's online session: xae_plc "
                           f"login first (ads_plc_state switches run/stop over ADS without a login). Nothing was done.")
        if action in _LOGIN_FLAGS:
            if before["logged_in"] and action == "download":
                return None              # logged in already: a download is the shell's PLC.Download (DTE route)
            self._on_item(target, lambda x: _com_call(x, "Login", _LOGIN_FLAGS[action]), f"ITcPlcOnline.Login({action})",
                          once=True)
        elif action == "logout":
            self._on_item(target, lambda x: _com_call(x, "Logoff"), "ITcPlcOnline.Logoff", once=True)
        elif action in ("start", "stop"):
            self._on_item(target, lambda x: _com_call(x, action.capitalize()), f"ITcPlcOnline.{action.capitalize()}",
                          once=True)
        elif action in _RESET_TYPE:
            self._on_item(target, lambda x: _com_call(x, "Reset", _RESET_TYPE[action]), f"ITcPlcOnline.Reset({action})",
                          once=True)
        else:
            return None
        want_in = {"login": True, "online_change": True, "download": True, "logout": False}.get(action)
        want_state = {"start": "run", "stop": "stop"}.get(action)

        def reached(st: dict[str, Any]) -> bool:
            return not ((want_in is not None and st["logged_in"] is not None and st["logged_in"] != want_in)
                        or (want_state and st["application_state"] not in (None, want_state)))
        # the state follows the call (Start is not yet RUN when it returns): read it again for a while. The command was
        # sent: whatever happens while reading it back, it is never sent again through another route (review of
        # 0.8.14: a busy shell during the read-back raised a plain error and plc_action went on to PLC.Download)
        t_end = time.monotonic() + self.ONLINE_STATE_WAIT_S
        try:
            after = self._online_state(self._item(target))
            while not reached(after) and time.monotonic() < t_end:
                time.sleep(0.25)
                after = self._online_state(self._item(target))
        except Exception as exc:  # noqa: BLE001
            raise _ActedUnconfirmed(
                f"ITcPlcOnline.{action} on {target} returned, but its state could not be read back ({exc}) — the "
                "command may have acted, so it is not sent again through another route; check xae_status / "
                "ads_status") from exc
        if not reached(after):
            # the call was accepted: it may have acted. No other route runs it again — a second reset or download
            # would act twice (review of the TE1000 route)
            raise _ActedUnconfirmed(
                f"ITcPlcOnline.{action} on {target} returned, but {self.ONLINE_STATE_WAIT_S:g} s later the project is "
                f"{'logged in' if after['logged_in'] else 'logged out' if after['logged_in'] is False else '(login unknown)'}"
                f", application {after['application_state']} (before: {before}) — the command may have acted, so it "
                "is not sent again through another route; check xae_status / ads_status")
        return {"action": action, "method": "ITcPlcOnline", "project": target, "ok": True, "before": before,
                "after": after}

    def plc_action(self, action: str, plc_project_path: str | None = None) -> dict[str, Any]:
        """login/logout/start/stop/reset_cold/reset_origin/download. N41: the PLC project is resolved ONCE — the
        named one, or the only one — and every route acts on exactly that project; with several and none named
        nothing is guessed. #10: first the documented Automation Interface route (ConsumeXml(<IECProjectDef>
        <OnlineSettings><Commands><LoginCmd>true…) on '<Name> Project', no active project needed); then the DTE
        commands, which act on the shell's ACTIVE project: a command that names the project (4026
        'PLC.Loginto<Name>') must name the target, a generic one ('PLC.Login') is used only when the solution has a
        single PLC project; the target's node is selected first when the shell names another project or none."""
        cands = list(PLC_LOGIN_COMMANDS.get(action) or [])
        if not cands:
            raise XaeError(f"unknown PLC action {action!r}; valid: {list(PLC_LOGIN_COMMANDS)}")
        try:
            all_projects = self._nested_projects(None)
            list_err = ""
        except Exception as exc:  # noqa: BLE001
            all_projects, list_err = [], str(exc)[:160]
        if plc_project_path:
            target = _plc_split(plc_project_path)[1]
        elif len(all_projects) == 1:
            target = all_projects[0]
        elif len(all_projects) > 1:
            raise XaeError(f"{len(all_projects)} PLC projects in the solution — name one with plc_project_path: {all_projects} "
                           "(nothing was done)")
        else:
            raise XaeError("the PLC projects of the solution could not be listed"
                           + (f" ({list_err})" if list_err else "") + " — pass plc_project_path (xae_plc_projects); nothing was done")
        target_name = target.split("^")[-1].removesuffix(" Project")
        # generic commands act on the ACTIVE project: only when the listing proves the target is the only one. An
        # empty or failed listing proves nothing (XAE busy) — 'single' was True then, and PLC.Download ran on PlcA
        # for a call (and a token) about PlcB
        single = len(all_projects) == 1 and all_projects[0].lower() == target.lower()
        ai_note = ""
        # TE1000 ITcPlcOnline first: documented for every action here, acts on this project, no shell command
        try:
            with self.silent(f"xae_plc {action}") as sn:
                r = self._plc_online(target, action)
            if r is not None:
                return {**r, **sn}
        except XaeExplained:
            raise                          # may have acted / needs a login / the shell went away during it
        except XaeError as exc:
            ai_note = f"ITcPlcOnline on {target}: {str(exc)[:200]}"
        tag = self._PLC_ONLINE_XML.get(action)
        if tag:
            xml = (f"<TreeItem><IECProjectDef><OnlineSettings><Commands><{tag}>true</{tag}></Commands>"
                   "</OnlineSettings></IECProjectDef></TreeItem>")
            try:
                self._consume(target, xml, f"ConsumeXml {tag}")
                out = {"action": action, "method": f"ConsumeXml {tag}", "project": target, "ok": True}
                if ai_note:
                    out["automation_interface"] = ai_note
                return out
            except XaeExplained:
                raise
            except XaeError as exc:
                ai_note = (ai_note + "; " if ai_note else "") + f"ConsumeXml {tag} on {target}: {str(exc)[:200]}"
        key = _cmd_key(target_name)
        tried: list[dict[str, str]] = []
        selected = None
        others: list[str] = []
        for attempt in range(2):
            dyn = self._dynamic_plc_commands(action)
            named = [(c, self._cmd_project(c, action)) for c in dyn]
            mine = [c for c, k in named if k and k not in ("", "none") and k == key]
            others = sorted({k for c, k in named if k and k not in ("", "none") and k != key})
            generic = [c for c in dict.fromkeys(cands + [c for c, k in named if k == ""])] if single else []
            if not mine and attempt == 0 and (others or not single or any(k == "none" for _c, k in named)):
                selected = self._select_plc_project_node(target_name)   # make the TARGET the active project
                if selected:
                    continue
            for c in mine + [g for g in generic if g not in mine]:
                try:
                    self.command(c)
                    out = {"action": action, "command": c, "ok": True, "project": target, "tried": tried,
                           "selected_node": selected}
                    if ai_note:
                        out["automation_interface"] = ai_note
                    return out
                except XaeExplained:
                    raise
                except XaeError as exc:
                    tried.append({"command": c, "error": str(exc)[:200]})
            break
        offered = self._dynamic_plc_commands(action)
        why = []
        if ai_note:
            why.append(f"Automation Interface route failed ({ai_note})")
        if others and not tried:
            why.append(f"the shell's PLC commands act on the active project {others}, not {target_name!r} — select "
                       f"'{target_name} Project' in Solution Explorer ('Set as Active PLC Project') and retry")
        elif not single and not tried:
            known = (f"the solution has {len(all_projects)} PLC projects" if all_projects else
                     "the solution's PLC projects could not be listed" + (f" ({list_err})" if list_err else ""))
            why.append(f"this shell's PLC commands do not name the project they act on and {known}, so "
                       f"{target_name!r} cannot be confirmed as the active one")
        else:
            why.append(f"no working DTE command for {action!r}: tried {[t['command'] for t in tried]}; this shell offers "
                       f"{offered or 'nothing matching'}")
        if any(o.lower().endswith("none") for o in offered):
            why.append("a '…none' command means no PLC project is active: click the PLC project node once in Solution "
                       "Explorer, then retry")
        raise XaeError("; ".join(why) + ". Nothing was done. ads_plc_state covers run/stop/reset via ADS without the shell.")

    def _cmd_project(self, cmd: str, action: str) -> str | None:
        """Which project a PLC shell command acts on: '' = the active one (generic 'PLC.Login', context menu),
        'none' = no active project, else the project key from 4026 names like 'PLC.LogintoUntitled1'; None = not a
        command for this action."""
        verb = self._PLC_VERB.get(action, "").lower()
        low = cmd.lower()
        if low.startswith("othercontextmenus.plcproject."):
            return ""
        for stem in (("plc.loginto", "plc.login") if verb == "login" else
                     ("plc.logoutfrom", "plc.logout") if verb == "logout" else ("plc." + verb,)):
            if low.startswith(stem):
                rest = low[len(stem):]
                return "none" if rest == "none" else _cmd_key(rest)
        return None

    # ------------------------------------------------------------ target / activate
    def target_get(self) -> dict[str, Any]:
        return {"target_net_id": self._on_sm(lambda sm: sm.GetTargetNetId(), "GetTargetNetId"),
                "twincat_started": bool(self._on_sm(lambda sm: sm.IsTwinCATStarted(), "IsTwinCATStarted"))}

    def target_set(self, net_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){5}", net_id):
            raise XaeError(f"not an AMS NetId: {net_id!r}")
        self._on_sm(lambda sm: sm.SetTargetNetId(net_id), "SetTargetNetId")
        return self.target_get()

    def activate_configuration(self) -> dict[str, Any]:
        self._sm()
        t0 = time.time()
        with self.silent("ActivateConfiguration") as sn:
            self._on_sm(lambda sm: sm.ActivateConfiguration(), "ActivateConfiguration", once=True)
        return {"activated": True, "target": self._on_sm(lambda sm: sm.GetTargetNetId(), "GetTargetNetId"), "duration_s": round(time.time() - t0, 1),
                "note": "Configuration is on the target. Call xae_restart_twincat to switch it to RUN.", **sn}

    def restart_twincat(self) -> dict[str, Any]:
        self._sm()
        t0 = time.time()
        with self.silent("StartRestartTwinCAT"):      # TE1000: "in UI builds a confirmation dialog appears first"
            self._on_sm(lambda sm: sm.StartRestartTwinCAT(), "StartRestartTwinCAT", once=True)
        started = None
        for _ in range(60):
            try:
                started = bool(self._cur_sm().IsTwinCATStarted())
                if started:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        # command_s: the command and the wait for IsTwinCATStarted — from RUN that is the OLD runtime, true at once
        # (N5: 0 s); how long the restart took is known only to who sees the target come back (verify_target_after)
        return {"restarted": True, "twincat_started": started, "command_s": round(time.time() - t0, 1)}

    # ------------------------------------------------------------ tree
    def _item(self, path: str) -> Any:
        return self._on_item(path, lambda it: it, f"LookupTreeItem {path}")

    def item_exists(self, path: str) -> bool | None:
        """N124: does the shell still have a tree item at path? True / False; None when the shell cannot say (busy,
        not attached) — a pending edit of an object that is gone can never reach the source files."""
        try:
            self._item(path)
            return True
        except XaeError as exc:
            low = str(exc).lower()
            if any(k in low for k in ("blocked by dialog", "disconnected", "busy", "rejected", "not open")):
                return None
            return False
        except Exception:  # noqa: BLE001
            return None

    def _item_dict(self, it: Any) -> dict[str, Any]:
        d: dict[str, Any] = {}
        _no = object()
        for prop in ("Name", "PathName", "ItemType", "ItemSubType", "ChildCount", "Comment", "Disabled"):
            # N174: through _retry — a rejected read left the property out (a tree item without its PathName)
            v = self._get(lambda prop=prop: getattr(it, prop), prop, _no)
            if v is not _no:
                d[prop] = v
        return d

    def tree_item(self, path: str) -> dict[str, Any]:
        return self._item_dict(self._item(path))

    def tree_children(self, path: str, depth: int = 1, max_items: int = 2000) -> dict[str, Any]:
        root = self._item(path)
        out: list[dict[str, Any]] = []
        unreadable: list[dict[str, Any]] = []
        get = self._get
        # N174 / N44 (0.8.14 retest): every read through _retry. A rejected ChildCount was read as 'no children' and a
        # rejected Name / lookup as 'nothing there', so the listing of a busy shell looked complete — the snapshot a
        # 'replace' import relies on (plcopen_import) missed objects instead of failing. A shell that stays busy, a
        # dialog or a shell that went away now raises: the listing failed, it is not a smaller tree.

        def _name(x: Any) -> str:
            return str(get(lambda: x.Name, "Name", "") or "")

        def walk(it: Any, lvl: int) -> None:
            if lvl > depth or len(out) >= max_items:
                return
            children: list[Any] = []
            ipath = str(get(lambda: it.PathName, "PathName", "") or "")
            n = get(lambda: int(it.ChildCount), f"{ipath or path} ChildCount", 0) or 0
            for i in range(1, n + 1):
                try:
                    children.append(self._retry(lambda i=i: it.Child(i), f"{ipath} Child({i})"))
                except (XaeBusy, XaeGone):
                    raise
                except XaeError as exc:
                    unreadable.append({"parent": ipath, "index": i, "error": str(exc)[:160]})
            # #17: the PLC container 'TIPC^<Name>' enumerates '<Name> Instance' but not its nested '<Name> Project'
            # (the item users need most); that one is reachable by path
            if ipath.upper().startswith("TIPC^") and ipath.count("^") == 1:
                want = f"{_name(it)} Project"
                if want.strip() != "Project" and want not in {_name(c) for c in children}:
                    x = get(lambda: self._cur_sm().LookupTreeItem(f"{ipath}^{want}"), f"LookupTreeItem {ipath}^{want}")
                    if x is not None:
                        children.insert(0, x)
            if not children:
                # the nested PLC project node (ItemType 56) does not enumerate its children through ChildCount;
                # its folders are reachable by path
                ptype = str(get(lambda: it.ItemType, "ItemType", "") or "")
                ppath = str(get(lambda: it.PathName, "PathName", "") or "")
                if ptype == str(TREEITEMTYPE_PLCAPP) or _name(it).endswith(" Project"):
                    for sub in PLC_PROJECT_FOLDERS:
                        x = get(lambda sub=sub: self._cur_sm().LookupTreeItem(f"{ppath}^{sub}"),
                                f"LookupTreeItem {ppath}^{sub}")
                        if x is not None:
                            children.append(x)
            for ch in children:
                d = self._item_dict(ch)
                d["depth"] = lvl
                out.append(d)
                if len(out) >= max_items:
                    return
                if lvl < depth:
                    walk(ch, lvl + 1)

        walk(root, 1)
        res: dict[str, Any] = {"path": path, "count": len(out), "items": out}
        if unreadable:
            res["unreadable_children"] = unreadable[:50]
        return res

    def tree_xml(self, path: str) -> dict[str, Any]:
        xml = self._on_item(path, lambda it: it.ProduceXml(False), f"ProduceXml {path}")
        return {"path": path, "xml": xml}

    def _consume(self, path: str, xml: str, what: str) -> None:
        """ConsumeXml; a refusal carries ITcSmTreeItem.GetLastXmlError (TE1000), which says what in the XML failed."""
        try:
            self._on_item(path, lambda it: it.ConsumeXml(xml), what, once=True)
        except XaeExplained:
            raise
        except XaeError as exc:
            # N174: the detail is read through _retry too (on a busy shell it was simply left out)
            try:
                it = self._item(path)
                detail = (self._get(lambda: str(_com_call(it, "GetLastXmlError") or ""), "GetLastXmlError")
                          if self._has(it, "GetLastXmlError") else None)
            except XaeError:
                detail = None
            if detail:
                raise XaeError(f"{exc} — GetLastXmlError: {detail[:600]}") from exc
            raise

    def tree_consume_xml(self, path: str, xml: str) -> dict[str, Any]:
        self._consume(path, xml, f"ConsumeXml {path}")
        return {"path": path, "consumed_bytes": len(xml)}

    def tree_set_values(self, path: str, assignments: dict[str, str]) -> dict[str, Any]:
        """ProduceXml → set text of elements addressed by XPath (relative to
        the <TreeItem> root) → ConsumeXml. Convenient for single settings
        (task cycle time, autostart, …) without hand-writing XML."""
        cur = self.tree_xml(path)["xml"]
        try:
            root = ET.fromstring(cur)
        except ET.ParseError as exc:
            raise XaeError(f"ProduceXml of {path} is not parseable: {exc}") from exc
        changed = {}
        for xp, val in assignments.items():
            el = root.find(xp)
            if el is None:
                raise XaeError(f"XPath {xp!r} not found in XML of {path}")
            changed[xp] = {"old": el.text, "new": val}
            el.text = val
        new_xml = ET.tostring(root, encoding="unicode")
        self.tree_consume_xml(path, new_xml)
        return {"path": path, "changed": changed}

    def _create_child(self, parent: Any, name: str, subtype: int, before: str, info: Any) -> Any:
        """CreateChild under parent: a tree path (looked up in every attempt, N120), or an item this same call has just
        created (a property's accessors: it is live — nothing can reopen the solution in between)."""
        def run(op: Callable[[Any], Any], what: str) -> Any:
            if isinstance(parent, str):
                return self._on_item(parent, op, what, once=True)
            with self._sm_messages_of(what):
                return self._retry(lambda: op(parent), what, once=True)
        if info is None:
            return run(lambda p: p.CreateChild(name, subtype, before), "CreateChild")
        try:
            return run(lambda p: p.CreateChild(name, subtype, before, info), "CreateChild")
        except (XaeExplained, XaeBusy):
            raise                          # the shell went away during it: not created a second time (N174: nor
                                           # with another argument form while it is busy or held by a dialog)
        except XaeError:
            if isinstance(info, list) and IS_WIN:  # pragma: no cover - retry as SAFEARRAY of BSTR
                variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, [str(x) for x in info])
                return run(lambda p: p.CreateChild(name, subtype, before, variant), "CreateChild(BSTR[])")
            raise

    def tree_create_child(self, parent_path: str, name: str, subtype: int, before: str = "",
                          info: Any = None) -> dict[str, Any]:
        return self._item_dict(self._create_child(parent_path, name, subtype, before, info))

    def tree_delete_child(self, parent_path: str, name: str) -> dict[str, Any]:
        self._on_item(parent_path, lambda p: p.DeleteChild(name), f"DeleteChild {name}", once=True)
        return {"deleted": f"{parent_path}^{name}"}

    # ------------------------------------------------------------ PLC
    def plc_projects(self) -> dict[str, Any]:
        """List PLC projects: TIPC children → each has one '<Name> Project' node."""
        res = self.tree_children("TIPC", depth=2)
        by_key: dict[str, dict[str, Any]] = {}
        for it in res["items"]:
            name = str(it.get("Name", ""))
            if str(it.get("ItemType")) == str(TREEITEMTYPE_PLCAPP) or name.endswith(" Project"):
                outer, nested = _plc_split(str(it.get("PathName")))
                # #10: the outer PLC node and its nested '<Name> Project' node are the SAME PLC project — listed once
                # (it was listed twice, so every Automation Interface route saw "2 PLC projects" and gave up); the
                # nested node's names are kept (the fields tools used before)
                key = nested.lower()
                if key in by_key and not name.endswith(" Project"):
                    continue
                base = outer.split("^")[-1]
                by_key[key] = {"name": name, "path": it.get("PathName"), "item_type": it.get("ItemType"),
                               "plc_project_path": it.get("PathName"),
                               "plc_path": outer, "iec_project_path": nested, "instance_path": f"{outer}^{base} Instance"}
        projs = list(by_key.values())
        return {"count": len(projs), "projects": projs, "raw": res["items"],
                "note": ("every tool takes any of these paths (R14): boot project / autostart act on plc_path "
                         "(ITcPlcProject), PLCopen import/export and Check all objects on iec_project_path "
                         "(ITcPlcIECProject), links on instance_path; POUs live at <iec_project_path>^POUs")}

    def pou_get(self, path: str, include_subitems: bool = True) -> dict[str, Any]:
        it = self._item(path)
        d = self._item_dict(it)
        get = self._get              # N174: through _retry — a rejected read gave declaration / implementation None
        d["declaration"] = get(lambda: it.DeclarationText, f"{path} DeclarationText")
        d["implementation"] = get(lambda: it.ImplementationText, f"{path} ImplementationText")
        d["language"] = get(lambda: it.Language, f"{path} Language")
        if include_subitems:
            subs = []
            for i in range(1, int(get(lambda: it.ChildCount, f"{path} ChildCount", 0) or 0) + 1):
                ch = get(lambda i=i: it.Child(i), f"{path} Child({i})")
                if ch is None:
                    break                    # as before: the sub items read up to the first that failed
                sd = self._item_dict(ch)
                sd["declaration"] = get(lambda: ch.DeclarationText, f"{path} Child({i}) DeclarationText")
                sd["implementation"] = get(lambda: ch.ImplementationText, f"{path} Child({i}) ImplementationText")
                subs.append(sd)
            d["subitems"] = subs
        return d

    def pou_set(self, path: str, declaration: str | None, implementation: str | None) -> dict[str, Any]:
        it = self._item(path)
        res: dict[str, Any] = {"path": path}
        self._set_text(it, declaration, implementation)
        if declaration is not None:
            res["declaration_chars"] = len(declaration)
        if implementation is not None:
            res["implementation_chars"] = len(implementation)
        return res

    def pou_create(self, parent_path: str, name: str, kind: str, language: str = "st",
                   return_type: str | None = None, extends: str | None = None,
                   implements: str | None = None, access: str | None = None,
                   declaration: str | None = None, implementation: str | None = None,
                   accessors: str = "get,set") -> dict[str, Any]:
        kind_l = kind.lower()
        if kind_l not in POU_SUBTYPE:
            raise XaeError(f"kind must be one of {sorted(POU_SUBTYPE)}")
        want_acc: list[str] = []
        if kind_l in ("property", "itf_property"):
            want_acc = [a.strip().lower() for a in (accessors or "").replace(";", ",").split(",") if a.strip()]
            bad = [a for a in want_acc if a not in ("get", "set")]
            if bad or not want_acc:
                raise XaeError(f"accessors must name get and/or set (got {accessors!r}): a property without an accessor "
                               "does not compile (C0373)")
        sub = POU_SUBTYPE[kind_l]
        lang = IEC_LANG.get(language.lower(), 1)
        info: Any
        if kind_l in ("program", "fb", "function_block"):
            v: list[Any] = [str(lang)]
            if extends and implements:
                v += ["Extends", extends, "Implements", implements]
            elif extends:
                v += ["Extends", extends]
            elif implements:
                v += ["Implements", implements]
            info = v if len(v) > 1 else lang
        elif kind_l == "function":
            info = [str(lang), return_type or "BOOL"]
        elif kind_l == "method":
            info = [str(lang), return_type or "", access or "PUBLIC"]
        elif kind_l == "property":
            info = [str(lang), return_type or "BOOL", access or "PUBLIC"]
        elif kind_l in ("action", "transition"):
            info = [str(lang)]                 # [0] IEC language as text, like every other vInfo array
        elif kind_l in ("property_get", "property_set"):
            # an Int32 here is refused: "vInfo (Type: Int32) is not supported for creating TREEITEMTYPE_PLCPROPGET"
            info = [str(lang)] + ([access] if access else [])
        elif kind_l in ("itf_property_get", "itf_property_set"):
            info = None
        elif kind_l in ("enum", "struct", "union", "alias", "gvl", "parameter_list"):
            info = declaration if declaration else None
        elif kind_l == "interface":
            info = extends if extends else None
        elif kind_l in ("itf_method", "itf_property"):
            info = return_type or "BOOL"
        else:
            info = None
        child = self._create_child(parent_path, name, sub, "", info)
        created = self._item_dict(child)
        path = created.get("PathName") or f"{parent_path}^{name}"
        if want_acc:
            made: list[str] = []
            try:
                for acc in want_acc:
                    acc_info = [str(lang)] if kind_l == "property" else None
                    ach = self._create_child(child, acc.capitalize(), _ACCESSOR_SUBTYPE[(kind_l, acc)], "", acc_info)
                    made.append(self._item_dict(ach).get("PathName") or f"{path}^{acc.capitalize()}")
            except XaeExplained as exc:
                # re-review of 0.8.14: the shell went away during an accessor (or it may have acted): no clean-up on a
                # re-attached shell, where the unsaved property is normally gone — say what is not known
                raise XaeExplained(f"{kind_l} {name}: creating its {acc.capitalize()} accessor — {exc} The {kind_l} "
                                   f"{path} this call created may be left without accessors: check the XAE tree.") from exc
            except XaeError as exc:
                # all or nothing: the property this call made goes again rather than stay without accessors
                try:
                    self._on_item(parent_path, lambda p: p.DeleteChild(name), f"DeleteChild {name}", once=True)
                    undo = f"the property {path} this call created was deleted again"
                except XaeError as exc2:
                    undo = f"the property {path} is left WITHOUT accessors (deleting it failed: {exc2}) — delete it"
                raise XaeError(f"{kind_l} {name}: creating its {acc.capitalize()} accessor failed ({exc}); {undo}") from exc
            created["accessors"] = made
        if declaration is not None and kind_l not in ("enum", "struct", "union", "alias", "gvl", "parameter_list"):
            self._set_text(child, declaration, None)
        if implementation is not None:
            self._set_text(child, None, implementation)
        created["path"] = path
        return created

    def _set_text(self, it: Any, declaration: str | None, implementation: str | None) -> None:
        if declaration is not None:
            def _sd() -> None:
                it.DeclarationText = declaration
            self._retry(_sd, "DeclarationText set")
        if implementation is not None:
            def _si() -> None:
                it.ImplementationText = implementation
            self._retry(_si, "ImplementationText set")

    def _import_options(self, it: Any) -> tuple[dict[str, int], str]:
        """PLCIMPORTOPTIONS as the live type library defines them; else the TE1000 enum page (see _DOC_IMPORT_OPTIONS)."""
        vals = self._get(lambda: _typelib_enum(it, "PLCIMPORTOPTIONS"), "PLCIMPORTOPTIONS")     # N174: through _retry
        if vals:
            m = {k.upper().removeprefix("PLCIMPORTOPTIONS_").lower(): int(v) for k, v in vals.items()}
            m = {k: v for k, v in m.items() if k in _DOC_IMPORT_OPTIONS}
            if len(m) == 4:
                return m, "type library (PLCIMPORTOPTIONS)"
        return dict(_DOC_IMPORT_OPTIONS), ("TE1000 enum page PLCIMPORTOPTIONS (REPLACE 1, RENAME 2; the type library "
                                           "was not readable)")

    def plcopen_import(self, plc_project_path: str, xml_file: str, option: str = "none") -> dict[str, Any]:
        if option not in _DOC_IMPORT_OPTIONS:
            raise XaeError(f"option must be one of {list(_DOC_IMPORT_OPTIONS)}")
        if not os.path.isfile(xml_file):
            raise XaeError(f"PLCopen XML not found: {xml_file}")
        plc_project_path = _plc_split(plc_project_path)[1]          # R14: PLCopen lives on '<Name> Project'
        it = self._item(plc_project_path)
        opts, opt_src = self._import_options(it)
        names = _plcopen_names(xml_file)

        def snapshot() -> dict[str, str] | None:
            """The project's objects by path — None when the listing cannot be trusted (it failed, or children were
            unreadable): then nothing is compared and nothing is deleted on its basis."""
            try:
                tc = self.tree_children(plc_project_path, depth=8, max_items=20000)
            except Exception:  # noqa: BLE001
                return None
            if tc.get("unreadable_children") or tc.get("count", 0) >= 20000:
                return None
            return {str(x.get("PathName")): str(x.get("Name")) for x in tc["items"] if x.get("PathName")}
        before = snapshot()
        # N44 (0.8.14 retest): the replace needs this listing (originals, copies, removed objects), so a listing that
        # failed refuses it before anything is imported. tree_children now fails on a shell that stays busy instead of
        # reading a rejected ChildCount as 'no children' (a listing that looked complete let the import run and left
        # its copies). The other options import what was asked and say what could not be checked (warning below).
        if before is None and option == "replace":
            raise XaeError(f"the objects of {plc_project_path} could not be listed completely, so the result of a 'replace' "
                           "import could not be checked for duplicate copies — nothing was imported. Retry when XAE is idle.")
        # N97: 'replace' here means the existing object gets the imported text IN PLACE (its folder, its GUID).
        # TwinCAT's PLCIMPORTOPTIONS_REPLACE deletes the existing object and creates the import at the import target —
        # foldered objects left their folders for the project root with new GUIDs (0.8.11 retest, value 1 from the type
        # library). So 'replace' imports as RENAME (copies <name>_1 beside the originals) and moves the checked text
        # of each copy into its original (_replace_from_copies), then deletes the copy
        value = opts["rename"] if option == "replace" else opts[option]
        # all four arguments: the folder structure from the XML is kept (bFolderStructure, default TRUE in the docs)
        try:
            self._on_item(plc_project_path, lambda x: x.PlcOpenImport(xml_file, value, "", True), "PlcOpenImport",
                          once=True)
        except (XaeExplained, XaeBusy):
            raise                          # the shell went away during it / N174: busy or held by a dialog — the
                                           # older two-argument signature is no answer to that: not imported again
        except XaeError:
            self._on_item(plc_project_path, lambda x: x.PlcOpenImport(xml_file, value), "PlcOpenImport", once=True)
        after = snapshot()
        ov: dict[str, Any] = {"value": value, "from": opt_src}
        if option == "replace":
            ov["imported_as"] = ("rename — then each copy's text, checked against the XML, is written into the existing "
                                 "object and the copy deleted (TwinCAT's own replace moves objects to the project root)")
        res: dict[str, Any] = {"imported": xml_file, "into": plc_project_path, "option": option, "objects_in_file": names,
                               "option_value": ov}
        if after is None and option == "replace":
            # N44: a normal result here read as success while renamed copies (PRG_X_1) may sit at the project root
            raise XaeError(f"PlcOpenImport(replace) ran, but the objects of {plc_project_path} could not be listed "
                           "afterwards, so it is not known whether TwinCAT put renamed copies (<name>_1) at the project "
                           "root instead of replacing — nothing was replaced or deleted. Check the tree (xae_tree) for "
                           f"{[n + '_1' for n in names][:5]} …")
        if before is None or after is None:
            res["warning"] = ("the project's objects could not be listed completely "
                              f"{'before' if before is None else 'after'} the import: the objects added or removed (and "
                              "any renamed copies such as PRG_X_1) are not known — check the tree (xae_tree)")
            return res
        added = {p: n for p, n in after.items() if p not in before}
        res["added"] = sorted(added)[:200]
        gone = sorted(p for p in before if p not in after)
        if gone:
            # N97: objects that were in the project before the import and are not now — never silent
            res["removed"] = gone[:200]
            moved = [{"from": p, "to": q} for p in gone for q, n in added.items() if n.lower() == before[p].lower()]
            if moved:
                res["moved"] = moved[:200]
            if option == "replace":
                raise XaeError(f"PlcOpenImport removed {len(gone)} existing object(s) from their place: {gone[:10]}"
                               + (f" — re-created elsewhere: {moved[:10]}" if moved else "")
                               + ". The in-place replace was not done; put them back (xae_tree) before importing again")
            res["warning"] = (f"{len(gone)} object(s) that were in the project before the import are not at their place "
                              f"any more: {gone[:10]}" + (f"; re-created at {[m['to'] for m in moved[:10]]}" if moved
                                                          else ""))
        # R15: 'replace' replaces an object at the SAME place; when the XML carries no folder for it, TwinCAT puts it at
        # the project root and renames it (PRG_X_1) because the name exists elsewhere — a silent duplicate. A name the
        # file itself holds (FB_Axis_2 imported on purpose) is never a copy.
        in_file = {o.lower() for o in names}
        dup = [p for p, n in added.items() if n.lower() not in in_file
               and any(re.fullmatch(re.escape(o) + r"_\d+", n, re.I) for o in names)]
        if dup:
            res["duplicates_created"] = dup
            if option == "replace":
                return self._replace_from_copies(res, dup, added, before, _plcopen_oracle(xml_file))
        if option == "replace":
            existing = sorted({n for n in before.values() if n.lower() in in_file})
            if existing:
                res["warning"] = (f"{existing[:10]} exist in the project, but the import made no copy of them, so "
                                  "nothing was written into them — they are unchanged")
        return res

    def _replace_from_copies(self, res: dict[str, Any], dup: list[str], added: dict[str, str],
                             before: dict[str, str], oracle: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """R15 / N38: PlcOpenImport(replace) put objects that live in a folder at the project root as renamed copies
        (PRG_X_1) — and renamed their declaration header too (PROGRAM PRG_X_1). The copy is TwinCAT's transformed
        version of the import, not the text the user imported, so it is never written as it is. Per object:
          1. the intended text = the copy's texts with the header name changed back to the original's name
             (PROGRAM/FUNCTION_BLOCK/FUNCTION/INTERFACE/TYPE, after pragmas and comments; nothing else changed);
          2. the intended text is checked against the XML file (TwinCAT's export carries every declaration as plain
             text, and the bodies): the user's input is the oracle, not the copy. A difference, a header that cannot
             be renamed back, or the copy's name anywhere else that the XML does not hold → nothing written;
          3. the original's texts are kept, the intended texts written, read back and compared, and the declared name
             must equal the original's name in the tree — an independent check, not a comparison with what was just
             written from the same source;
          4. any failure → the kept texts are written back (and checked);
          5. a copy (created by this import, it was not in the tree before) is deleted only when its original passed
             step 3. Every other copy is kept and named."""
        done: list[dict[str, Any]] = []
        failed: list[str] = []
        kept: list[str] = []
        for p in dup:
            copy_name = added[p]
            base = re.sub(r"_\d+$", "", copy_name)
            origs = [bp for bp, bn in before.items() if bn.lower() == base.lower()]
            if len(origs) != 1:
                failed.append(f"{copy_name}: {len(origs)} existing objects named {base} — nothing written")
                kept.append(p)
                continue
            orig_path, orig_name = origs[0], before[origs[0]]
            try:
                rec = self._replace_one(p, copy_name, orig_path, orig_name, (oracle or {}).get(base.lower()))
                done.append(rec)
            except _ReplaceError as exc:
                failed.append(f"{copy_name}: {exc}")
                kept.append(p)
        removed, left = [], []
        for p in dup:
            if p in kept:
                continue
            parent, name = p.rsplit("^", 1)
            try:
                self.tree_delete_child(parent, name)
                removed.append(p)
            except XaeError as exc:
                left.append(f"{p} ({exc})")
        res["copies_removed"] = removed
        if done:
            res["replaced"] = [{k: v for k, v in d.items() if k != "header_renamed"} for d in done]
            res["header_renamed"] = [d["header_renamed"] for d in done if d.get("header_renamed")]
            res["build_required"] = True
            res["objects_to_build"] = [d["object"] for d in done]
        if failed or left:
            raise XaeError("PlcOpenImport(replace) put the object(s) at the project root as renamed copies; "
                           f"not replaced: {failed}; "
                           + (f"copies kept (they hold the import, delete them with xae_tree_delete when done): {kept}; "
                              if kept else "")
                           + (f"copies that could NOT be removed: {left}; " if left else "")
                           + (f"replaced and verified: {[d['object'] for d in done]} (build required); " if done else "")
                           + "every original not listed as replaced is unchanged.")
        res.pop("duplicates_created", None)
        res["note"] = ("PlcOpenImport put the object(s) at the project root as renamed copies (they live in a folder); "
                       "the header names were changed back, the texts checked against the XML, written into the "
                       "existing object(s), read back with the declared name checked, and the copies deleted — "
                       "save + build (build_required)")
        return res

    def _replace_one(self, copy_path: str, copy_name: str, orig_path: str, orig_name: str,
                     xml: dict[str, Any] | None) -> dict[str, Any]:
        """One object of _replace_from_copies (steps 1–4). Raises _ReplaceError; the original is as before then,
        unless the message says it may be half-written."""
        try:
            src = self._text_tree(self._item(copy_path), copy_name)
            dst = self._text_tree(self._item(orig_path), orig_path)
        except XaeError as exc:
            raise _ReplaceError(f"could not read the texts ({exc}) — nothing written") from exc
        graphic = self._non_st_language(src, orig_path)
        if graphic:
            # N81: ITcPlcImplementation.Language (TE1000) says it before any text is compared: a graphical body is not
            # in ImplementationText (for those it returns the XML archive), so it cannot be moved as text
            raise _ReplaceError(f"{graphic}: a body that is not ST text cannot be moved from the copy as text, nor "
                                "checked — nothing written; the copy holds the imported version")
        diff = _tree_diff(src, dst, orig_path)
        if diff:
            raise _ReplaceError(f"{diff} — nothing written")
        # 1. intended text: the header renamed back
        want = _tree_copy(src)
        renamed = None
        if want["decl"] is not None and _header_name(want["decl"]) is not None:
            new_decl = _rename_header(want["decl"], copy_name, orig_name)
            if new_decl is None:
                kw_name = _header_name(want["decl"])
                raise _ReplaceError(f"the copy's declaration header names {kw_name[1] if kw_name else '?'!r}, not "
                                    f"{copy_name!r} — header not recognised, nothing written")
            want["decl"] = new_decl
            renamed = {"object": orig_path, "from": copy_name, "to": orig_name}
        elif _HAS_HEADER.match(_strip_trivia_head(want["decl"] or "")):
            raise _ReplaceError("the copy's declaration header could not be parsed — nothing written")
        # 2. the XML is the oracle
        checked = "name"
        not_in_xml: list[str] = []
        if xml is not None:
            graphic = _non_text_body(xml, orig_path)
            if graphic:
                raise _ReplaceError(f"{graphic}: a body that is not ST text cannot be moved from the copy as text, nor "
                                    "checked — nothing written; the copy holds the imported version")
            bad = _oracle_mismatch(want, xml, orig_path)
            if bad:
                raise _ReplaceError(f"the copy differs from the imported XML at {bad} — nothing written")
            # N175: 'xml' when texts were compared; the parts the XML holds no text for are named, not implied checked
            compared, not_in_xml = _oracle_coverage(want, xml, orig_path)
            checked = "xml" if compared else "name"
        stray = _name_occurrences(want, copy_name, xml)
        if stray:
            raise _ReplaceError(f"the copy's name {copy_name!r} also occurs in {', '.join(stray)}, which the XML does not hold — "
                                "TwinCAT may have renamed it there; nothing written")
        # 3. write, read back, independent name check
        try:
            self._write_tree(want, dst)
            got = self._text_tree(dst["_it"], orig_path)
            bad = _tree_mismatch(want, got, orig_path)
            if bad:
                raise XaeError(f"read back differs at {bad}")
            if got["decl"] is not None and _header_name(want["decl"] or "") is not None:
                h = _header_name(got["decl"])
                if h is None or h[1].lower() != orig_name.lower():
                    raise XaeError(f"after writing, the declaration names {h[1] if h else 'nothing'!r}, the object in "
                                   f"the tree is {orig_name!r}")
            rec = {"object": orig_path, "from_copy": copy_path, "checked_against": checked, "header_renamed": renamed}
            if not_in_xml:
                rec["not_in_xml"] = not_in_xml[:50]
                rec["not_in_xml_note"] = (
                    "the PLCopen file holds no plain text for these parts (TwinCAT exports an interface's methods and "
                    "properties with their declarations, but not the INTERFACE header), so they were not compared with "
                    "it — only the declared name was checked (header renamed back, read back, name equal to the "
                    "object's)")
            return rec
        except XaeError as exc:
            err = str(exc)
        # 4. back to the kept texts
        try:
            self._write_tree(dst, dst)
            bad = _tree_mismatch(dst, self._text_tree(dst["_it"], orig_path), orig_path)
            if bad:
                raise XaeError(f"read back differs at {bad}")
        except XaeError as exc:
            raise _ReplaceError(f"writing failed ({err}) and restoring {orig_path} failed too ({exc}) — it may be "
                                "half-written; its copy is kept") from exc
        raise _ReplaceError(f"writing failed ({err}); {orig_path} was restored to its earlier text")

    def _text_tree(self, it: Any, where: str, depth: int = 0) -> dict[str, Any]:
        """{'decl', 'impl', 'kids': {name: tree}, '_it'} of an object, every level (up to 6). Read through _retry: a
        busy or failing shell raises; a node type that has no such text property gives None."""
        def prop(name: str) -> Callable[[], Any]:
            def rd() -> Any:
                try:
                    v = getattr(it, name)
                except AttributeError:
                    return None
                except Exception as exc:  # noqa: BLE001
                    if getattr(exc, "hresult", None) in (DISP_E_MEMBERNOTFOUND, E_NOTIMPL):
                        return None
                    raise
                return None if v is None else str(v)
            return rd
        out: dict[str, Any] = {"_it": it, "decl": self._retry(prop("DeclarationText"), f"{where} DeclarationText"),
                               "impl": self._retry(prop("ImplementationText"), f"{where} ImplementationText"), "kids": {}}
        n = int(self._retry(lambda: it.ChildCount, f"{where} ChildCount"))
        if depth < 6:
            for i in range(1, n + 1):
                c = self._retry(lambda i=i: it.Child(i), f"{where} Child({i})")
                nm = str(self._retry(lambda c=c: c.Name, f"{where} Child({i}).Name"))
                out["kids"][nm] = self._text_tree(c, f"{where}^{nm}", depth + 1)
        return out

    def _non_st_language(self, tree: dict[str, Any], where: str) -> str | None:
        """'<path> (CFC)' for the first object of a text tree whose ITcPlcImplementation.Language is neither none nor
        ST, or None (also when the property is not offered)."""
        it = tree.get("_it")
        # N174: through _retry — on a busy shell the language read as 'not offered' and a graphical body passed
        lang = (self._get(lambda: int(it.Language), f"{where} Language")
                if it is not None and self._has(it, "Language") else None)
        if lang not in (None, 0, 1):
            names = {v: k.upper() for k, v in IEC_LANG.items()}
            return f"{where} ({names.get(lang, 'UML' if lang in (7, 8, 9) else f'language {lang}')})"
        for nm, sub in (tree.get("kids") or {}).items():
            m = self._non_st_language(sub, f"{where}^{nm}")
            if m:
                return m
        return None

    def _write_tree(self, src: dict[str, Any], dst: dict[str, Any]) -> None:
        """Write src's texts into dst's items, every level (same child names, checked before)."""
        self._set_text(dst["_it"], src["decl"], src["impl"])
        for nm, sub in src["kids"].items():
            self._write_tree(sub, dst["kids"][nm])

    def plcopen_export(self, plc_project_path: str, items: list[str], xml_file: str) -> dict[str, Any]:
        plc_project_path = _plc_split(plc_project_path)[1]          # R14: 'TIPC^X' raised AttributeError
        self._item(plc_project_path)                                  # it exists (a clear error otherwise)
        sel = ";".join(items)
        # TE1000 PlcOpenExport: an existing file or a missing folder is an ArgumentException — said here, plainly
        if os.path.exists(xml_file):
            raise XaeError(f"{xml_file} exists: PlcOpenExport does not overwrite — choose another file name")
        folder = os.path.dirname(os.path.abspath(xml_file))
        if not os.path.isdir(folder):
            raise XaeError(f"the folder {folder} does not exist: create it first (PlcOpenExport does not)")
        self._on_item(plc_project_path, lambda x: x.PlcOpenExport(xml_file, sel), "PlcOpenExport")
        return {"exported": items, "file": xml_file}

    def boot_project(self, plc_project_path: str, autostart: bool | None, generate: bool,
                     activate_after_generate: bool = False) -> dict[str, Any]:
        plc_project_path = _plc_split(plc_project_path)[0]          # R14: boot project lives on 'TIPC^X'
        self._item(plc_project_path)                                  # it exists (a clear error otherwise)
        res: dict[str, Any] = {"path": plc_project_path}
        if autostart is not None:
            def _set(x: Any) -> None:
                x.BootProjectAutostart = bool(autostart)
            self._on_item(plc_project_path, _set, "BootProjectAutostart")
            res["autostart"] = bool(autostart)
        if generate:
            self._on_item(plc_project_path, lambda x: x.GenerateBootProject(bool(activate_after_generate)),
                          "GenerateBootProject", once=True)
            res["boot_project_generated"] = True
        res["current_autostart"] = self._get(lambda: bool(self._cur_sm().LookupTreeItem(plc_project_path).BootProjectAutostart),
                                             "BootProjectAutostart")                       # N174: through _retry
        return res

    def io_disable(self, disable: bool = True, only: list[str] | None = None) -> dict[str, Any]:
        """Set the Disabled flag on every I/O device under TIID (or the named ones) —
        lets a configuration activate on a PC/target without the hardware (TcUnit runs)."""
        root = self._item("TIID")
        changed = []
        # N174: every access through _retry (ChildCount / Child were read raw: a rejection escaped)
        n = int(self._retry(lambda: root.ChildCount, "TIID ChildCount"))
        for i in range(1, n + 1):
            dev = self._retry(lambda i=i: root.Child(i), f"TIID Child({i})")
            name = self._get(lambda: dev.Name, f"TIID Child({i}) Name") or f"device {i}"
            if only and name not in only:
                continue
            try:
                self._retry(lambda: setattr(dev, "Disabled", 1 if disable else 0),   # SMDS_DISABLED / SMDS_NOT_DISABLED
                            f"{name} Disabled")
                changed.append({"device": name, "disabled": bool(disable)})
            except XaeError as exc:
                cause = exc.__cause__
                changed.append({"device": name, "error": _com_msg(cause) if cause is not None and not isinstance(exc, XaeBusy)
                                else str(exc)})
        return {"devices": changed, "note": "takes effect on the next ActivateConfiguration"}

    def links(self, plc_project_path: str) -> dict[str, Any]:
        """Variable links of the PLC instance (inputs/outputs groups) from their XML:
        name, type, link target; lists unlinked AT %I*/%Q* variables."""
        outer = _plc_split(plc_project_path)[0]
        inst_path = f"{outer}^{outer.split('^')[-1]} Instance"
        inst = self._item(inst_path)
        groups = []
        unlinked = []
        # N174: every access through _retry (ChildCount / Child were read raw: a rejection escaped)
        n = int(self._retry(lambda: inst.ChildCount, f"{inst_path} ChildCount"))
        for i in range(1, n + 1):
            grp = self._retry(lambda i=i: inst.Child(i), f"{inst_path} Child({i})")
            gname = self._get(lambda: grp.Name, f"{inst_path} Child({i}) Name") or f"group {i}"
            try:
                xml = self._retry(lambda: grp.ProduceXml(False), f"{gname} ProduceXml")
                root = ET.fromstring(xml)
            except (XaeBusy, XaeGone):
                raise
            except XaeError as exc:
                groups.append({"group": gname, "error": _com_msg(exc.__cause__ or exc)})
                continue
            except Exception as exc:  # noqa: BLE001 — XML the parser does not take
                groups.append({"group": gname, "error": _com_msg(exc)})
                continue
            vars_ = []
            for v in root.iter("Var"):
                name = (v.findtext("Name") or "").strip()
                vtype = (v.findtext("Type") or "").strip()
                linked = (v.findtext("LinkedTo") or "").strip()
                vars_.append({"name": name, "type": vtype, "linked_to": linked or None})
                if not linked:
                    unlinked.append({"group": gname, "name": name, "type": vtype})
            groups.append({"group": gname, "variables": vars_, "count": len(vars_)})
        return {"instance": inst_path, "groups": groups, "unlinked": unlinked, "unlinked_count": len(unlinked)}

    def _nested_projects(self, plc_project_path: str | None = None) -> list[str]:
        if plc_project_path:
            return [_plc_split(plc_project_path)[1]]
        out: list[str] = []
        for p in self.plc_projects().get("projects") or []:
            if p["iec_project_path"].lower() not in {x.lower() for x in out}:
                out.append(p["iec_project_path"])
        return out

    def check_all_objects(self, plc_project_path: str | None = None) -> dict[str, Any]:
        """PLC 'Check all objects' (compile every POU incl. unused). #45: first the documented Automation Interface call
        ITcPlcIECProject2.CheckAllObjects() on the nested project — no UI, no active project needed — then the
        shell commands as before."""
        ai_err = ""
        try:
            nested = self._nested_projects(plc_project_path)
        except Exception as exc:  # noqa: BLE001
            nested, ai_err = [], str(exc)[:160]
        # N82: the items came from the whole Output pane and an Error List never cleared — a deleted object's warning
        # was still reported, a recurring one counted once more per run. The Error List is cleared (as before a build)
        # and only the Build pane lines written after this point are read
        self._clear_error_list()
        mark = self._pane_text()
        if len(nested) == 1:
            try:
                ok = self._on_item(nested[0], lambda x: _com_call(x, "CheckAllObjects"),
                                   "ITcPlcIECProject2.CheckAllObjects")
                time.sleep(0.5)
                return {"method": "ITcPlcIECProject2.CheckAllObjects", "project": nested[0],
                        "passed": bool(ok) if ok is not None else None, **self.errors(pane_from=mark)}
            except XaeError as exc:
                if "0x80004005" in str(exc) or "E_FAIL" in str(exc):
                    # TE1000: the check reports a failed check as an exception (Error_CheckAllObjectsFailed, E_FAIL) —
                    # the check RAN: its findings are the result, not a reason to run it again through the shell
                    time.sleep(0.5)
                    return {"method": "ITcPlcIECProject2.CheckAllObjects", "project": nested[0], "passed": False,
                            "check_error": str(exc)[:300], **self.errors(pane_from=mark)}
                ai_err = str(exc)[:200]
        elif len(nested) > 1:
            ai_err = f"{len(nested)} PLC projects — name one with plc_project_path: {nested}"
        cands = ["PLC.CheckAllObjects", "OtherContextMenus.PlcProject.CheckAllObjects"]
        # the name varies by shell and UI language (4026: PLC.CheckAllObjects<Project>, localised shells differ):
        # take whatever the shell lists under 'Check' that is not a plain syntax check
        try:
            listed = self.list_commands("Check", 200).get("commands", [])
            cands += [c for c in listed if "checkallobject" in c.lower().replace(" ", "") and c not in cands]
        except XaeError:
            listed = []
        # the shell's own names first: a guessed name that does not exist only adds noise to the error
        cands = [c for c in cands if c in listed] + [c for c in cands if c not in listed]
        tried = []
        selected = None
        for attempt in range(2):
            for c in cands:
                try:
                    self.command(c)
                    time.sleep(1.0)
                    out = {"command": c, **self.errors(pane_from=mark)}
                    if selected:
                        out["selected_node"] = selected
                    return out
                except XaeError as exc:
                    tried.append({"command": c, "error": str(exc)[:240]})
            if attempt == 0:
                # 'command not available' usually means no PLC project node is selected (retest #45)
                selected = self._select_plc_project_node()
                if not selected:
                    break
        listed_hits = [t for t in tried if t["command"] in listed]
        raise XaeError("Check all objects did not run. "
                       + (f"Automation Interface: {ai_err}. " if ai_err else "")
                       + (f"The shell has {[t['command'] for t in listed_hits]} but refused it: {listed_hits[-1]['error']}. "
                          if listed_hits else f"No such command in this shell (looked for {cands}; it lists {listed[:10]}). ")
                       + ("Selecting the PLC project node did not help. " if selected else "The PLC project node could not be selected. ")
                       + "In the IDE: right-click the PLC project → Check all objects; xae_build still compiles everything referenced.")


    # ------------------------------------------------------------ TwinCAT Scope (Measurement projects)
    # Automation Interface for TwinCAT Measurement (TwinCAT.Measurement.AutomationInterface.dll, TE130x): a Scope
    # configuration is a ProjectItem of a Measurement project (.tcmproj) whose .Object implements IMeasurementScope —
    # StartRecord / StopRecord / SaveSVD(path) / ExportCSV(path), each returning int. Reached here by late binding.
    def _scope_objects(self) -> list[dict[str, Any]]:
        self._require_win()
        self._need()
        found: list[dict[str, Any]] = []

        def visit(p: Any, depth: int) -> None:
            if depth > 3:
                return
            # N174: every read through _retry — on a busy shell a Scope configuration was read as 'none there'
            fn = str(get(lambda: p.FullName, "Project FullName") or "")
            items = get(lambda: p.ProjectItems, "ProjectItems")
            if items is None:
                return
            for j in range(1, (get(lambda: items.Count, "ProjectItems.Count") or 0) + 1):
                it = get(lambda j=j: items.Item(j), f"ProjectItems.Item({j})")
                if it is None:
                    continue
                sub = get(lambda: it.SubProject, "SubProject")          # projects inside solution folders
                if sub is not None:
                    visit(sub, depth + 1)
                    continue
                obj = get(lambda: it.Object, "ProjectItem Object")
                ms = (get(lambda: _measurement_scope(obj), "IMeasurementScope")
                      if fn.lower().endswith(".tcmproj") and obj is not None else None)
                if ms is not None:
                    found.append({"name": str(self._retry(lambda: it.Name, "ProjectItem Name")),
                                  "project": str(get(lambda: p.Name, "Project Name") or ""), "file": fn, "_obj": ms})

        get = self._get
        projs = self._retry(lambda: self.dte.Solution.Projects, "Solution.Projects")
        for i in range(1, int(self._retry(lambda: projs.Count, "Solution.Projects.Count")) + 1):
            visit(self._retry(lambda i=i: projs.Item(i), f"Solution.Projects.Item({i})"), 0)
        return found

    def scope_list(self) -> dict[str, Any]:
        sc = self._scope_objects()
        return {"count": len(sc), "scopes": [{k: v for k, v in d.items() if not k.startswith("_")} for d in sc],
                "note": ("TwinCAT Scope configurations in the open solution (Measurement projects, .tcmproj). What is "
                         "recorded — channels, sample time, record time, trigger — is set in that configuration.")
                if sc else "no Scope configuration in the open solution (add a TwinCAT Measurement project → Scope)"}

    def _scope_pick(self, name: str | None) -> dict[str, Any]:
        sc = self._scope_objects()
        if not sc:
            raise XaeError("no TwinCAT Scope configuration in the open solution (TwinCAT Measurement project → Scope)")
        if name:
            low = name.lower()
            hit = [d for d in sc if low in (d["name"].lower(), f"{d['project']}\\{d['name']}".lower(), d["project"].lower())]
            if len(hit) != 1:
                raise XaeError(f"Scope {name!r} {'is ambiguous' if hit else 'not found'}; open: "
                               f"{[d['project'] + chr(92) + d['name'] for d in sc]}")
            return hit[0]
        if len(sc) > 1:
            raise XaeError(f"{len(sc)} Scope configurations are open — name one: {[d['project'] + chr(92) + d['name'] for d in sc]}")
        return sc[0]

    def scope_action(self, action: str, name: str | None = None, path: str | None = None) -> dict[str, Any]:
        """start | stop | save_svd(path) | export_csv(path) on one Scope configuration."""
        d = self._scope_pick(name)
        obj = d["_obj"]
        calls = {"start": ("StartRecord", ()), "stop": ("StopRecord", ()), "save_svd": ("SaveSVD", (path,)),
                 "export_csv": ("ExportCSV", (path,))}
        if action not in calls:
            raise XaeError(f"scope action must be one of {list(calls)}")
        meth, args = calls[action]
        if args and not path:
            raise XaeError(f"{action} needs a path")
        before = None
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            try:
                st0 = Path(path).stat()
                before = (st0.st_size, st0.st_mtime_ns)
            except OSError:
                before = None
        t0 = time.monotonic()
        tries = {"n": 0}

        def call() -> Any:
            # N120: a retry (busy shell, re-attached shell) asks the Scope object of the live shell, not the first one
            o = obj if tries["n"] == 0 else self._scope_pick(name)["_obj"]
            tries["n"] += 1
            return _com_call(o, meth, *args)
        rc = self._retry(call, f"Scope {meth}", once=True)
        out: dict[str, Any] = {"scope": f"{d['project']}\\{d['name']}", "action": action, "method": meth,
                               "result_code": rc, "elapsed_s": round(time.monotonic() - t0, 3)}
        if path:
            p = Path(path)
            out["path"] = str(p)
            st = p.stat() if p.is_file() else None
            # a file that was there before and is unchanged is not written by this call (scope audit, 0.8.13: a stale
            # CSV counted as written)
            out["written"] = st is not None and st.st_size > 0 and (before is None or (st.st_size, st.st_mtime_ns) != before)
            if not out["written"]:
                raise XaeError(f"Scope {meth}({path!r}) returned {rc} but "
                               + ("the file that was there is unchanged" if before is not None and st is not None else
                                  "no file was written")
                               + " — is there recorded data (stop the recording first)?")
        # R11: IMeasurementScope declares `int StartRecord()` etc. without saying what the int means (Beckhoff's own
        # sample ignores it); on 4026 StartRecord returns 1 and StopRecord 0 while both work. Only a negative value
        # (an HRESULT error) is a warning; success shows in the file written / scope_status.
        if isinstance(rc, int) and rc < 0:
            out["warning"] = f"{meth} returned {rc} (0x{rc & 0xFFFFFFFF:08X}) — an error code; check scope_status"
        elif rc not in (0, None):
            out["result_note"] = (f"{meth} returned {rc}: the Automation Interface does not document this value "
                                  "(StartRecord gives 1 while recording normally) — not an error")
        return out


# ----------------------------------------------------------------------------
# 4024: 'C:\P\MAIN.TcPOU(12,5): error C0046: …';  4026: 'C:\P\FB_X.TcPOU@M_Run(9) : error: C0185 …' (D41)
_RE_OUT_ERR = re.compile(
    r"(?:^|[\s:>\]\)])(?P<level>error|warning)(?!\(s\))\s*:?\s*(?P<code>[A-Z]{1,2}\d{1,5}|\d{1,5})?\s*:?\s+(?P<msg>\S.*)$", re.I)
_RE_FILE_LINE = re.compile(r"(?P<file>(?:[A-Za-z]:|\\\\)\\[^\(\[@]+?\.(?:TcPOU|TcDUT|TcGVL|TcIO|TcTTO|TcVIS|tsproj|plcproj))"
                           r"(?:@(?P<obj>[^\(\[\s]+))?\s*[\(\[]\s*(?P<line>\d+)(?:\s*,\s*(?P<col>\d+))?")


def _parse_error_list_copy(text: str) -> list[dict[str, Any]]:
    """The Error List copied as text: a header row (Severity, Code, Description, Project, File, Line, …) then one
    tab-separated row per entry; column order follows the header."""
    rows = [r for r in text.splitlines() if r.strip()]
    if not rows:
        return []
    head = [h.strip().lower() for h in rows[0].split("\t")]
    if "description" not in head:
        return []
    col = {h: i for i, h in enumerate(head)}

    def g(cells: list[str], name: str) -> str:
        i = col.get(name)
        return cells[i].strip() if i is not None and i < len(cells) else ""
    out = []
    for r in rows[1:]:
        c = r.split("\t")
        sev = g(c, "severity").lower()
        line = g(c, "line")
        out.append({"level": "error" if sev.startswith("err") else "warning" if sev.startswith("warn") else "message",
                    "description": g(c, "description"), "file": g(c, "file"), "line": int(line) if line.isdigit() else 0,
                    "column": 0, "project": g(c, "project"), **({"code": g(c, "code")} if g(c, "code") else {})})
    return out


def _parse_build_output(text: str) -> list[dict[str, Any]]:
    items = []
    for ln in text.splitlines():
        m = _RE_OUT_ERR.search(ln)
        if not m:
            continue
        f = _RE_FILE_LINE.search(ln)
        it = {
            "level": m.group("level").lower(),
            "description": m.group("msg").strip(),
            "file": f.group("file") if f else "",
            "line": int(f.group("line")) if f else 0,
            "column": int(f.group("col")) if f and f.group("col") else 0,
            "project": "",
            "raw": ln.strip(),
        }
        if m.group("code"):
            it["code"] = m.group("code").upper()
        if f and f.group("obj"):
            it["object"] = f.group("obj")           # method / action / property inside the file (4026)
        items.append(it)
    return items


# TwinCAT.Measurement.AutomationInterface.IMeasurementScope — [ComImport, Guid(...)], dual. The ProjectItem's Object
# is the .NET ScopeProjectNode whose default IDispatch (class interface _ScopeProjectNode) does NOT expose
# StartRecord/StopRecord/SaveSVD/ExportCSV; QueryInterface for this IID gives the dispatch that does (N28,
# verified on TC 3.1.4026: StartRecord dispid 1610743808, StopRecord …809, SaveSVD …811, ExportCSV …812).
IID_IMEASUREMENT_SCOPE = "{8FBF6B86-2B51-4B09-8045-D071F2663878}"


def _measurement_scope(obj: Any) -> Any:
    """The IMeasurementScope view of a Measurement project item, or None when the item is no Scope configuration."""
    if _has_member(obj, "StartRecord"):          # already the right dispatch (and the test doubles)
        return obj
    try:
        import pythoncom  # type: ignore
        import pywintypes  # type: ignore
        import win32com.client  # type: ignore
        disp = obj._oleobj_.QueryInterface(pywintypes.IID(IID_IMEASUREMENT_SCOPE), pythoncom.IID_IDispatch)
        ms = win32com.client.Dispatch(disp)
    except Exception as exc:  # noqa: BLE001 — not a Scope node (data pool, chart, …) or no pywin32
        if getattr(exc, "hresult", None) in _BUSY_HR + _GONE_HR:
            raise                  # N174: asked again by the caller's _retry
        return None
    return ms if _has_member(ms, "StartRecord") else None


_TKIND_ENUM = 0        # TYPEKIND: TKIND_ENUM = 0 (oaidl.h) — pythoncom.TKIND_ENUM, without importing pythoncom


def _typelib_enum(obj: Any, enum_name: str) -> dict[str, int] | None:
    """{name: value} of an enum in the type library that defines this late-bound COM object's interface (what makepy
    reads); None when it cannot be read (no type info, not Windows, a test double)."""
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:
        return None

    def field(o: Any, index: int, name: str) -> Any:
        # pywin32 gives TYPEATTR / VARDESC as tuples (genpy reads attr[7], vdesc[0], vdesc[1]); newer builds also name
        # the fields — either form is read
        try:
            return o[index]
        except (TypeError, IndexError, KeyError):
            return getattr(o, name)
    try:
        tlib, _ = ole.GetTypeInfo().GetContainingTypeLib()
        for i in range(tlib.GetTypeInfoCount()):
            if tlib.GetTypeInfoType(i) != _TKIND_ENUM:
                continue
            if str(tlib.GetDocumentation(i)[0]).upper() != enum_name.upper():
                continue
            ti = tlib.GetTypeInfo(i)
            out: dict[str, int] = {}
            for j in range(int(field(ti.GetTypeAttr(), 7, "cVars"))):        # TYPEATTR: cVars
                vd = ti.GetVarDesc(j)
                out[str(ti.GetNames(field(vd, 0, "memid"))[0])] = int(field(vd, 1, "value"))   # VARDESC: memid, value
            return out
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "hresult", None) in _BUSY_HR + _GONE_HR:
            raise                  # N174: asked again by the caller's _retry, not read as 'no type library'
        return None
    return None


def _has_prop(obj: Any, name: str) -> bool:
    """Late-bound COM object exposes the property ``name``; a plain Python object has the attribute (tests)."""
    if getattr(obj, "_oleobj_", None) is None:
        return hasattr(obj, name)
    return _has_member(obj, name)


def _has_member(obj: Any, name: str) -> bool:
    """Late-bound COM object exposes ``name`` (IDispatch::GetIDsOfNames)."""
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:
        return callable(getattr(obj, name, None))      # plain Python objects (tests)
    try:
        ole.GetIDsOfNames(name)
        return True
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "hresult", None) in _BUSY_HR + _GONE_HR:
            raise                  # N174: a busy (or gone) shell is not 'no such member' — the caller's _retry asks again
        return False


def _plc_split(path: str) -> tuple[str, str]:
    """('TIPC^X', 'TIPC^X^X Project') from either form (R14: tools took one form each and failed on the other).
    Anything else (a deeper path, another root) comes back unchanged in both places."""
    segs = [x for x in (path or "").split("^") if x]
    if len(segs) >= 2 and segs[0].upper() == "TIPC":
        outer = "^".join(segs[:2])
        nested = f"{outer}^{segs[1]} Project"
        if len(segs) == 2 or (len(segs) == 3 and segs[2].lower() == f"{segs[1]} project".lower()):
            return outer, nested
    return path, path


def _plcopen_names(xml_file: str) -> list[str]:
    """Names of the POUs / DUTs / GVLs / interfaces a PLCopen XML file holds."""
    try:
        txt = Path(xml_file).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    found = re.findall(r"<(?:pou|dataType|globalVars|Interface)\b[^>]*\bname=\"([^\"]+)\"", txt)
    return list(dict.fromkeys(found))


def _com_call(obj: Any, name: str, *args: Any) -> Any:
    """Call a COM *method*. On a late-bound dispatch ``getattr(obj, 'StartRecord')`` already invokes a parameterless
    member as a property get and returns its int result — calling that result raised "'int' object is not callable"
    (R11). IDispatch::Invoke with DISPATCH_METHOD calls the method exactly once."""
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:
        return getattr(obj, name)(*args)                  # plain Python objects (tests)
    try:
        import pythoncom  # type: ignore
        method_flag = pythoncom.DISPATCH_METHOD
    except ImportError:
        method_flag = 1                                   # DISPATCH_METHOD
    dispid = ole.GetIDsOfNames(name)
    return ole.Invoke(dispid, 0, method_flag, True, *args)


def _tree_diff(a: dict[str, Any], b: dict[str, Any], where: str) -> str | None:
    """Where two text trees have other children (names, case-insensitive like IEC), or None."""
    ka, kb = {k.lower() for k in a["kids"]}, {k.lower() for k in b["kids"]}
    if ka != kb:
        return f"{where} has other methods/properties/actions than the imported one (differ: {sorted(ka ^ kb)})"
    if set(a["kids"]) != set(b["kids"]):
        return f"{where}: child names differ in case ({sorted(set(a['kids']) ^ set(b['kids']))})"
    for k in a["kids"]:
        d = _tree_diff(a["kids"][k], b["kids"][k], f"{where}^{k}")
        if d:
            return d
    return None


def _norm_text(t: str | None) -> str | None:
    return None if t is None else "\n".join(ln.rstrip() for ln in t.replace("\r\n", "\n").split("\n")).strip("\n")


def _tree_mismatch(want: dict[str, Any], got: dict[str, Any], where: str) -> str | None:
    """First place where a read-back tree does not hold the wanted text (only where a text was wanted)."""
    for key in ("decl", "impl"):
        if want[key] is not None and _norm_text(want[key]) != _norm_text(got.get(key)):
            return f"{where} ({'declaration' if key == 'decl' else 'implementation'})"
    for k, sub in want["kids"].items():
        if k not in got["kids"]:
            return f"{where}^{k} (gone)"
        m = _tree_mismatch(sub, got["kids"][k], f"{where}^{k}")
        if m:
            return m
    return None


def _cmd_key(name: str) -> str:
    """A project name as it appears in a VS command name (letters and digits, lower case)."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


class _ReplaceError(Exception):
    pass


def _tree_copy(t: dict[str, Any]) -> dict[str, Any]:
    return {"_it": t.get("_it"), "decl": t.get("decl"), "impl": t.get("impl"),
            "kids": {k: _tree_copy(v) for k, v in (t.get("kids") or {}).items()}}


_HEAD_KW = {"PROGRAM", "FUNCTION_BLOCK", "FUNCTION", "INTERFACE", "TYPE"}
_HEAD_MODS = {"PUBLIC", "PRIVATE", "PROTECTED", "INTERNAL", "FINAL", "ABSTRACT"}
_IDENT_AT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HAS_HEADER = re.compile(r"(?i)(PROGRAM|FUNCTION_BLOCK|FUNCTION|INTERFACE|TYPE)\b")


def _skip_trivia(s: str, i: int) -> int:
    """Index after whitespace, // and /* */ comments, nested (* *) comments and {pragma} blocks."""
    n = len(s)
    while i < n:
        if s[i].isspace():
            i += 1
        elif s.startswith("//", i):
            j = s.find("\n", i)
            i = n if j < 0 else j + 1
        elif s.startswith("(*", i):
            depth = 0
            while i < n:
                if s.startswith("(*", i):
                    depth, i = depth + 1, i + 2
                elif s.startswith("*)", i):
                    depth, i = depth - 1, i + 2
                    if depth == 0:
                        break
                else:
                    i += 1
        elif s.startswith("/*", i):
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif s[i] == "{":
            j = s.find("}", i)
            i = n if j < 0 else j + 1
        else:
            break
    return i


def _strip_trivia_head(s: str) -> str:
    return s[_skip_trivia(s, 0):]


def _header_name(decl: str | None) -> tuple[str, str, int, int] | None:
    """(keyword, name, start, end) of the object's own name in its declaration header — PROGRAM / FUNCTION_BLOCK
    (with PUBLIC, FINAL, ABSTRACT …) / FUNCTION / INTERFACE / TYPE, after pragmas and comments — else None (a GVL
    has no header name)."""
    if not decl:
        return None
    i = _skip_trivia(decl, 0)
    m = _IDENT_AT.match(decl, i)
    if not m or m.group(0).upper() not in _HEAD_KW:
        return None
    kw = m.group(0).upper()
    i = m.end()
    while True:
        i = _skip_trivia(decl, i)
        m = _IDENT_AT.match(decl, i)
        if not m:
            return None
        if kw != "PROGRAM" and kw != "TYPE" and m.group(0).upper() in _HEAD_MODS:
            i = m.end()
            continue
        return kw, m.group(0), m.start(), m.end()


def _rename_header(decl: str, old: str, new: str) -> str | None:
    """The declaration with exactly the header's object name old → new (case-insensitive match, every other byte
    kept: EXTENDS, IMPLEMENTS, ': <return type>', comments, line endings); None when the header does not name old."""
    h = _header_name(decl)
    if h is None or h[1].lower() != old.lower():
        return None
    return decl[:h[2]] + new + decl[h[3]:]


def _name_occurrences(tree: dict[str, Any], name: str, xml: dict[str, Any] | None, where: str = "") -> list[str]:
    """Where `name` occurs as a whole word in the tree's texts and the XML (where it has that text) does not."""
    pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", re.I)
    out: list[str] = []
    for key in ("decl", "impl"):
        t = tree.get(key)
        if t and pat.search(t):
            x = (xml or {}).get(key)
            if not (x and pat.search(x)):
                out.append(f"{where or 'the object'} ({'declaration' if key == 'decl' else 'implementation'})")
    for k, sub in (tree.get("kids") or {}).items():
        out += _name_occurrences(sub, name, ((xml or {}).get("kids") or {}).get(k.lower()), f"{where}^{k}" if where else k)
    return out


def _oracle_mismatch(want: dict[str, Any], xml: dict[str, Any], where: str) -> str | None:
    """First place where the intended text differs from the XML's text (where the XML has one; line endings and
    trailing blanks ignored), with the first differing line."""
    for key in ("decl", "impl"):
        a, b = want.get(key), xml.get(key)
        if a is None or b is None:
            continue
        na, nb = _norm_text(a), _norm_text(b)
        if na != nb:
            la, lb = (na or "").split("\n"), (nb or "").split("\n")
            i = next((k for k in range(max(len(la), len(lb))) if (la[k] if k < len(la) else None) != (lb[k] if k < len(lb) else None)), 0)
            return (f"{where} ({'declaration' if key == 'decl' else 'implementation'}) line {i + 1}: "
                    f"{(la[i] if i < len(la) else '')!r} vs XML {(lb[i] if i < len(lb) else '')!r}")
    xk = xml.get("kids") or {}
    for k, sub in (want.get("kids") or {}).items():
        if k.lower() in xk:
            m = _oracle_mismatch(sub, xk[k.lower()], f"{where}^{k}")
            if m:
                return m
    return None


def _plcopen_oracle(xml_file: str) -> dict[str, dict[str, Any]]:
    """What the PLCopen XML says each object's texts are: {name.lower(): {'decl', 'impl', 'kids': {…}}} from
    TwinCAT's 'interfaceasplaintext' blocks and the ST bodies; methods, actions, properties (Get / Set) as kids.
    Objects without plain text (a foreign XML) are left out — the name check alone applies to them."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(xml_file).getroot()
    except (ET.ParseError, OSError):
        return {}
    for el in root.iter():                                  # namespaces off: tags and attributes by local name
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]

    def plain(el: Any) -> str | None:
        # N175 (0.8.15 retest): TwinCAT 3.1.4026 writes an interface's (and its methods' / properties') plain text as
        # a direct child <InterfaceAsPlainText><xhtml> — not under addData; looked for first
        x = el.find("./InterfaceAsPlainText/xhtml")
        if x is not None:
            return x.text or ""
        for d in el.findall("./interface/addData/data") + el.findall("./addData/data"):
            if str(d.get("name", "")).endswith("interfaceasplaintext"):
                x = d.find("./InterfaceAsPlainText/xhtml")
                if x is not None:
                    return x.text or ""
        return None

    def body(el: Any) -> str | None:
        x = el.find("./body/ST/xhtml")
        return (x.text or "") if x is not None else None

    def body_kind(el: Any) -> str | None:
        """'ST', the graphical / other language of the body (FBD, LD, SFC, IL, CFC …), or None (no body: a DUT, a
        GVL, an interface method). TwinCAT writes CFC as body/addData/data name='…/cfc'."""
        b = el.find("./body")
        if b is None:
            return None
        if b.find("./ST") is not None:
            return "ST"
        for c in b:
            if c.tag == "addData":
                for d in c.findall("./data"):
                    return str(d.get("name", "")).rsplit("/", 1)[-1].upper() or "addData"
                continue
            return str(c.tag)
        return None                     # <body/>: nothing in it that the texts could leave behind

    def node(el: Any) -> dict[str, Any]:
        kids: dict[str, Any] = {}
        # N175 (0.8.15 retest): TwinCAT 3.1.4026's interface export holds its members as direct children —
        # <Interface><Methods><Method>, <Properties><Property> — besides the addData/data[…/method] form of others
        groups = [(str(d.get("name", "")), d) for d in el.findall("./addData/data")]
        # eleventh review: each group by the member tag it holds ('Properties'[:-1] was 'Propertie' — never read)
        groups += [("/" + tag, d) for g, tag in (("Methods", "method"), ("Properties", "property"), ("Actions", "action"))
                   for d in el.findall("./" + g)]
        for nm, d in groups:
            for tag in ("Method", "Action", "Property", "Transition"):
                if nm.endswith("/" + tag.lower()):
                    for c in d.findall("./" + tag):
                        sub = node(c)
                        if tag == "Property":
                            # Get / Set: children of <Property> in TwinCAT / CODESYS exports; also looked for under
                            # its addData (seen in other writers) — not found meant never compared
                            for acc, kid in (("GetAccessor", "Get"), ("SetAccessor", "Set")):
                                a = c.find("./" + acc)
                                if a is None:
                                    a = next((x for dd in c.findall("./addData/data") for x in dd.findall("./" + acc)),
                                             None)
                                if a is not None:
                                    sub["kids"][kid.lower()] = node(a)
                        kids[str(c.get("name", "")).lower()] = sub
        return {"decl": plain(el), "impl": body(el), "body": body_kind(el), "kids": kids}

    out: dict[str, dict[str, Any]] = {}
    for el in root.iter():
        if el.tag in ("pou", "dataType", "globalVars", "Interface") and el.get("name"):
            n = node(el)
            # N175 (0.8.14 retest): an object counts when ANY part of it carries text — an interface's export holds its
            # methods' / properties' declarations but no plain text of the INTERFACE header, and was left out (its
            # replace then reported checked_against 'name' and its method texts were never compared)
            if _xml_has_text(n) or _non_text_body(n, ""):
                out.setdefault(str(el.get("name")).lower(), n)
    return out


def _xml_has_text(n: dict[str, Any]) -> bool:
    return (n.get("decl") is not None or n.get("impl") is not None
            or any(_xml_has_text(k) for k in (n.get("kids") or {}).values()))


def _oracle_coverage(want: dict[str, Any], xml: dict[str, Any] | None, where: str) -> tuple[int, list[str]]:
    """(texts compared with the XML, parts whose text the XML does not hold) of an intended text tree: what
    _oracle_mismatch could check and what it could not (N175). An empty text needs no check."""
    compared, missing = 0, []
    for key in ("decl", "impl"):
        if not (_norm_text(want.get(key)) or ""):
            continue
        if xml is not None and xml.get(key) is not None:
            compared += 1
        else:
            missing.append(f"{where} ({'declaration' if key == 'decl' else 'implementation'})")
    xk = (xml or {}).get("kids") or {}
    for k, sub in (want.get("kids") or {}).items():
        c, m = _oracle_coverage(sub, xk.get(k.lower()), f"{where}^{k}")
        compared += c
        missing += m
    return compared, missing


def _non_text_body(xml: dict[str, Any], where: str) -> str | None:
    """The first part of an XML object whose body is not ST text (FBD, LD, SFC, CFC, IL …): '<where> (FBD)', or None.
    Such a body is not in DeclarationText / ImplementationText — moving the copy's texts over would leave the imported
    graphics behind in the copy that is then deleted."""
    if xml.get("body") not in (None, "ST"):
        return f"{where or 'the object'} ({xml['body']})"
    for k, sub in (xml.get("kids") or {}).items():
        m = _non_text_body(sub, f"{where}^{k}" if where else k)
        if m:
            return m
    return None


def _com_msg(exc: BaseException) -> str:
    hr = getattr(exc, "hresult", None)
    excepinfo = getattr(exc, "excepinfo", None)
    desc = ""
    if excepinfo and len(excepinfo) > 2 and excepinfo[2]:
        desc = str(excepinfo[2])
    base = f"{type(exc).__name__}: {desc or exc}"
    if hr is not None:
        base += f" (HRESULT 0x{hr & 0xFFFFFFFF:08X})"
    return base
