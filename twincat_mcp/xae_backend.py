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
Common roots: TIID (I/O), TIPC (PLC), TIRT (tasks), TIRC (real-time settings), TIRR (routes),
TINC (NC), TIRS (safety).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any, Callable

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
}

TREEITEMTYPE_PLCAPP = 56          # the nested "<Name> Project" node of a PLC project (ITcPlcIECProject)
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


def sta_init() -> None:  # pragma: no cover
    if IS_WIN:
        pythoncom.CoInitialize()


def sta_teardown() -> None:  # pragma: no cover
    if IS_WIN:
        pythoncom.CoUninitialize()


class XaeBackend:
    def __init__(self, cfg: XaeConfig):
        self.cfg = cfg
        self.dte: Any = None
        self.sysman: Any = None
        self.progid: str | None = None
        self.attached_at: float | None = None
        self._sysman_project_name: str | None = None
        self.pid: int | None = None
        self.watchdog = DialogWatchdog(cfg.dialog_rules)

    # ------------------------------------------------------------ helpers
    def _require_win(self) -> None:
        if not IS_WIN:
            raise XaeError("XAE backend needs Windows with TwinCAT XAE and pywin32 installed "
                           "(pip install pywin32). This process is running on " + sys.platform)

    def _retry(self, fn: Callable[[], Any], what: str = "COM call") -> Any:
        """Retry COM calls while the shell is busy."""
        t_start = time.time()
        deadline = t_start + self.cfg.com_retry_s
        delay = 0.25
        while True:
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                hr = getattr(exc, "hresult", None)
                if hr in (RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER) and time.time() - t_start > min(1.0, self.cfg.com_retry_s / 2):
                    # busy for more than a second: if a modal dialog has the shell, retrying for a minute is pointless
                    dlg = self._modal_dialogs()
                    if dlg:
                        d = dlg[0]
                        raise XaeError(
                            f"{what}: BLOCKED BY DIALOG '{d.get('title')}' — buttons {d.get('buttons')}; text: "
                            f"{str(d.get('text', ''))[:300]!r}. The shell is waiting for an answer, so whether the action ran "
                            "is unknown until the dialog is answered (xae_dialog_resolve(button=...)), then check the result."
                        ) from exc
                if hr in (RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER) and time.time() < deadline:
                    time.sleep(delay)
                    delay = min(delay * 1.5, 2.0)
                    continue
                if hr in (RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE):
                    self.dte = self.sysman = None
                    if not getattr(self, "_reattaching", False):
                        # the solution was closed and reopened (or the shell restarted): find the live shell and retry once
                        self._reattaching = True
                        try:
                            self.attach()
                            log.warning("%s: DTE proxy was dead (0x%08X); re-attached to the running shell and retrying", what, hr & 0xFFFFFFFF)
                            return fn()
                        except Exception as exc2:  # noqa: BLE001
                            self.dte = self.sysman = None
                            raise XaeError(f"{what}: XAE shell disconnected (closed or restarted); re-attach did not help: "
                                           f"{_com_msg(exc2) if hasattr(exc2, 'hresult') else exc2}. Re-run xae_attach.") from exc2
                        finally:
                            self._reattaching = False
                    raise XaeError(f"{what}: XAE shell disconnected (closed or crashed). Re-run xae_attach.") from exc
                if self._stale_sysman(exc) and not getattr(self, "_refreshing_sm", False):
                    # N9: the solution was closed and reopened; the cached ITcSysManager belongs to the old one
                    self._refreshing_sm = True
                    try:
                        self.sysman = None
                        self._find_sysman()
                        log.warning("%s: System Manager object was stale (solution reopened); refreshed and retrying", what)
                        return fn()
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        self._refreshing_sm = False
                msg = _com_msg(exc)
                if "logged into" in msg.lower() or "logged in" in msg.lower():
                    msg += " — XAE is logged in to the PLC; the project cannot be changed online. Log out first: xae_plc('logout')."
                raise XaeError(f"{what}: {msg}") from exc

    def _modal_dialogs(self) -> list[dict[str, Any]]:
        try:
            from . import dialogs as _dlg
            return [d.to_dict() for d in _dlg.list_dialogs(self.pid)]
        except Exception:  # noqa: BLE001 — no dialog enumeration (non-Windows, no pid)
            return []

    def _need(self) -> Any:
        if self.dte is None:
            self.attach()
        return self.dte

    @staticmethod
    def _stale_sysman(exc: BaseException) -> bool:
        hr = getattr(exc, "hresult", None)
        info = getattr(exc, "excepinfo", None) or ()
        scode = info[5] if len(info) > 5 else None
        return hr == E_POINTER or (hr == DISP_E_EXCEPTION and (scode == E_POINTER or "pointer" in str(info).lower()))

    def _sm(self) -> Any:
        if self.sysman is None:
            self._need()
            self._find_sysman()
        else:
            # N9: a solution closed and reopened behind us leaves the cached System Manager stale — compare the
            # solution that is open now with the one the cached object came from
            try:
                cur = self.dte.Solution.FullName if self.dte is not None else None
            except Exception:  # noqa: BLE001
                cur = None
            if cur is not None and getattr(self, "_sysman_solution", None) not in (None, cur):
                self.sysman = None
                self._find_sysman()
        return self.sysman

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
                    sol = dte.Solution.FullName
                except Exception:  # noqa: BLE001
                    pass
                out.append({"progid": progid, "moniker": name, "dte": dte, "solution": sol})
            except Exception:  # noqa: BLE001
                continue
        return out

    def attach(self, progid: str | None = None, solution_path: str | None = None,
               launch: bool | None = None) -> dict[str, Any]:
        self._require_win()
        want_sol = (solution_path or self.cfg.solution_path or "")
        want_sol_norm = os.path.normcase(os.path.abspath(want_sol)) if want_sol else ""
        cands = [c for c in self._rot_candidates() if not progid or c["progid"] == progid]
        pick = None
        if want_sol_norm:
            for c in cands:
                if c["solution"] and os.path.normcase(os.path.abspath(c["solution"])) == want_sol_norm:
                    pick = c
                    break
        if pick is None and cands:
            # prefer the configured progid order
            order = {p: i for i, p in enumerate(self.cfg.progids)}
            cands.sort(key=lambda c: order.get(c["progid"], 99))
            pick = cands[0]
        launched = False
        if pick is None:
            do_launch = (not self.cfg.attach_only) if launch is None else launch
            if not do_launch:
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
        self.dte, self.progid = pick["dte"], pick["progid"]
        self.sysman = None
        self.attached_at = time.time()
        self.pid = None
        if pick.get("moniker") and ":" in pick["moniker"]:
            try:
                self.pid = int(pick["moniker"].rsplit(":", 1)[1])
            except ValueError:
                self.pid = None
        if self.pid is None and sys.platform == "win32":
            # launched via Dispatch (no ROT moniker): take the pid from the shell's main window, so the
            # dialog watchdog has a process scope — without one it deliberately watches nothing
            try:
                import win32process  # type: ignore
                hwnd = int(self.dte.MainWindow.HWnd)
                self.pid = int(win32process.GetWindowThreadProcessId(hwnd)[1]) or None
            except Exception as exc:  # noqa: BLE001
                log.warning("XAE pid unknown (%s); dialog watchdog disabled until re-attach", exc)
        if self.cfg.dialog_watchdog:
            self.watchdog.start(self.pid)
        if launched:
            try:
                self.dte.SuppressUI = False
                self.dte.MainWindow.Visible = bool(self.cfg.visible)
                self.dte.UserControl = True  # keep alive when we release
            except Exception:  # noqa: BLE001
                pass
            if want_sol:
                self.open_solution(want_sol)
        elif want_sol and pick["solution"] and os.path.normcase(os.path.abspath(pick["solution"])) != want_sol_norm:
            pass  # attached to a shell with a different solution; status() will show it
        info = self.status()
        info["launched"] = launched
        info["candidates"] = [{"progid": c["progid"], "solution": c["solution"], "moniker": c["moniker"]} for c in cands]
        return info

    def status(self) -> dict[str, Any]:
        if self.dte is None:
            return {"attached": False, "platform": sys.platform, "progids": list(self.cfg.progids)}
        d: dict[str, Any] = {"attached": True, "progid": self.progid}
        try:
            d["dte_version"] = self._retry(lambda: self.dte.Version, "DTE.Version")
            d["edition"] = self._retry(lambda: self.dte.Edition, "DTE.Edition")
        except XaeError as exc:
            d["dte_error"] = str(exc)
        try:
            sol = self.dte.Solution
            d["solution"] = sol.FullName
            d["solution_open"] = bool(sol.IsOpen)
            prjs = []
            for i in range(1, sol.Projects.Count + 1):
                p = sol.Projects.Item(i)
                prjs.append({"name": p.Name, "file": getattr(p, "FullName", ""), "kind": getattr(p, "Kind", "")})
            d["projects"] = prjs
        except Exception as exc:  # noqa: BLE001
            d["solution_error"] = _com_msg(exc)
        if not d.get("solution_open") or not str(d.get("solution") or "").strip():
            # no solution (IsOpen false, or the shell's empty start state with FullName ""): say so instead of a
            # stale project name and a raw COM exception (retest 0.7.1, 0.8.3)
            self.sysman = None
            d["solution_open"] = False
            d.pop("solution_error", None)
            d["note"] = "no solution open in the shell — xae_open_solution(path) or open one in XAE"
            return d
        try:
            sm = self._sm()
            d["twincat_project"] = self._sysman_project_name
            d["target_net_id"] = sm.GetTargetNetId()
            d["twincat_started"] = bool(sm.IsTwinCATStarted())
        except Exception as exc:  # noqa: BLE001
            if not any("tsproj" in str(p.get("file", "")).lower() or p.get("name") for p in d.get("projects") or []):
                d["note"] = "the open solution has no TwinCAT project"
            else:
                d["note"] = "the TwinCAT project of the open solution is not reachable through the Automation Interface"
            d["detail"] = _com_msg(exc)[:200]
        return d

    def _find_sysman(self) -> None:
        dte = self._need()
        sol = dte.Solution
        n = self._retry(lambda: sol.Projects.Count, "Projects.Count")
        for i in range(1, n + 1):
            p = sol.Projects.Item(i)
            try:
                obj = p.Object
                if obj is None:
                    continue
                obj.GetTargetNetId()  # proves it's an ITcSysManager
                self.sysman = obj
                self._sysman_project_name = p.Name
                try:
                    self._sysman_solution = sol.FullName
                except Exception:  # noqa: BLE001
                    self._sysman_solution = None
                return
            except Exception:  # noqa: BLE001
                continue
        raise XaeError("no TwinCAT (ITcSysManager) project found in the open solution")

    # ------------------------------------------------------------ solution
    def open_solution(self, path: str) -> dict[str, Any]:
        dte = self._need()
        if not os.path.isfile(path):
            raise XaeError(f"solution not found: {path}")
        self._retry(lambda: dte.Solution.Open(path), "Solution.Open")
        self.sysman = None
        # wait for projects to load
        for _ in range(120):
            try:
                if dte.Solution.Projects.Count > 0:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        return self.status()

    def _dirty(self, save: bool = False) -> list[str]:
        """Unsaved documents and projects the shell reports (Document.Saved / Project.Saved == False); with
        ``save`` each one is saved on its own (the second route when File.SaveAll left some, D44)."""
        dte = self._need()
        out: list[str] = []

        def name_of(x: Any) -> str:
            for a in ("FullName", "UniqueName", "Name"):
                try:
                    v = str(getattr(x, a))
                    if v:
                        return v
                except Exception:  # noqa: BLE001
                    continue
            return "?"

        def check(x: Any) -> None:
            try:
                if bool(x.Saved):
                    return
            except Exception:  # noqa: BLE001
                return
            if save:
                try:
                    x.Save()
                    if bool(x.Saved):
                        return
                except Exception:  # noqa: BLE001
                    pass
            out.append(name_of(x))
        try:
            docs = dte.Documents
            for i in range(1, int(docs.Count) + 1):
                try:
                    check(docs.Item(i))
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

        budget = [3000]                                   # COM calls are slow: cap the walk

        def walk(coll: Any, depth: int) -> None:
            try:
                n = int(coll.Count)
            except Exception:  # noqa: BLE001
                return
            for i in range(1, n + 1):
                if budget[0] <= 0:
                    return
                budget[0] -= 1
                try:
                    x = coll.Item(i)
                except Exception:  # noqa: BLE001
                    continue
                check(x)                                  # Project or ProjectItem (both have Saved)
                try:
                    sub = x.SubProject                    # a nested project (the PLC project in a .tsproj)
                except Exception:  # noqa: BLE001
                    sub = None
                if sub is not None:
                    check(sub)
                if depth < 6:
                    try:
                        walk((sub or x).ProjectItems, depth + 1)
                    except Exception:  # noqa: BLE001
                        pass
        try:
            walk(dte.Solution.Projects, 0)
        except Exception:  # noqa: BLE001
            pass
        return list(dict.fromkeys(out))

    def save_all(self) -> dict[str, Any]:
        """File.SaveAll, then whatever the shell still reports unsaved is saved one by one (D44: SaveAll from the
        Automation Interface did not always persist text set through DeclarationText/ImplementationText)."""
        dte = self._need()
        before = self._dirty()
        self._retry(lambda: dte.ExecuteCommand("File.SaveAll"), "File.SaveAll")
        after = self._dirty()
        if after:
            after = self._dirty(save=True)
        return {"saved": not after, "dirty_before": before, "dirty_after": after}

    def close_solution(self, save: bool = True) -> dict[str, Any]:
        dte = self._need()
        self._retry(lambda: dte.Solution.Close(save), "Solution.Close")
        self.sysman = None
        return {"closed": True}

    # ------------------------------------------------------------ build
    def build(self, clean: bool = False, rebuild: bool = False, timeout_s: float | None = None) -> dict[str, Any]:
        dte = self._need()
        sb = dte.Solution.SolutionBuild
        self._clear_error_list()
        t0 = time.time()
        if clean:
            self._retry(lambda: sb.Clean(True), "SolutionBuild.Clean")
        if rebuild:
            self._retry(lambda: sb.Clean(True), "SolutionBuild.Clean")
        self._retry(lambda: sb.Build(True), "SolutionBuild.Build")
        # Build(True) waits, but belt and braces: poll BuildState (3 = done)
        deadline = time.time() + (timeout_s or self.cfg.build_timeout_s)
        while time.time() < deadline:
            try:
                if int(sb.BuildState) != 2:  # vsBuildStateInProgress == 2 (NotStarted=1, Done=3)
                    break
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.5)
        failed = None
        try:
            failed = int(sb.LastBuildInfo)
        except Exception:  # noqa: BLE001
            pass
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
        try:
            self.dte.ExecuteCommand("OtherContextMenus.ErrorList.Clear")
        except Exception:  # noqa: BLE001
            pass

    def _dte2(self) -> Any:  # pragma: no cover
        """Best effort DTE -> DTE2 (needed for ToolWindows)."""
        try:
            mod = win32com.client.gencache.EnsureModule(*ENVDTE80_TYPELIB)
            if mod is not None:
                return win32com.client.CastTo(self.dte, "DTE2")
        except Exception:  # noqa: BLE001
            pass
        return None

    def _error_items(self, view_copy: bool = False) -> tuple[list[dict[str, Any]], str]:
        """Return (items, source). D20: the Error List is what the operator sees, so it is read first and by every
        route there is — DTE2.ToolWindows.ErrorList (late-bound: the cached 7.0 gen_py wrapper has no ToolWindows),
        the ErrorList window object, and when those report nothing a copy of the Error List view itself (its
        TwinCAT entries — Scope, Measurement, system messages — are not always in ErrorItems). The Build output
        pane is parsed as well and merged in, so a source that misses items does not hide them."""
        dte = self._need()
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
                          ("Windows.ErrorList", lambda: dte.Windows.Item(VS_WINDOW_ERRORLIST).Object.ErrorItems)):
            try:
                items = enum(get())
            except Exception:  # noqa: BLE001
                continue
            take(items, name)
            got = got or bool(items)
            if items:
                break
        if not got and view_copy:              # moves the focus to the Error List: only when diagnostics ask for it
            try:
                take(self._error_list_copy(), "ErrorList(view copy)")
            except Exception:  # noqa: BLE001
                pass
        try:
            out = _parse_build_output(self.build_output(tail_lines=2000)["text"])
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
        dte = self._need()
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
            dte.Windows.Item(VS_WINDOW_ERRORLIST).Activate()
            dte.ExecuteCommand("Edit.SelectAll")
            dte.ExecuteCommand("Edit.Copy")
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

    @staticmethod
    def _error_item(it: Any, lvl: dict[int, str]) -> dict[str, Any]:
        def g(name: str, default: Any = "") -> Any:
            try:
                return getattr(it, name)
            except Exception:  # noqa: BLE001
                return default
        return {
            "level": lvl.get(int(g("ErrorLevel", 3) or 3), "message"),
            "description": g("Description"),
            "file": g("FileName"),
            "line": g("Line", 0),
            "column": g("Column", 0),
            "project": g("Project"),
        }

    def errors(self, view_copy: bool = False) -> dict[str, Any]:
        items, source = self._error_items(view_copy)
        return {"count": len(items), "source": source, "items": items}

    def build_output(self, tail_lines: int = 200) -> dict[str, Any]:
        dte = self._need()
        text = ""
        try:
            win = dte.Windows.Item(VS_WINDOW_OUTPUT)
            ow = win.Object
            pane = ow.OutputWindowPanes.Item("Build")
            doc = pane.TextDocument
            sel = doc.Selection
            sel.StartOfDocument(False)
            sel.EndOfDocument(True)
            text = sel.Text or ""
        except Exception as exc:  # noqa: BLE001
            return {"text": "", "error": _com_msg(exc)}
        lines = text.splitlines()
        return {"text": "\n".join(lines[-tail_lines:]), "total_lines": len(lines)}

    # ------------------------------------------------------------ commands
    def command(self, name: str, args: str = "") -> dict[str, Any]:
        dte = self._need()
        self._retry(lambda: dte.ExecuteCommand(name, args), f"ExecuteCommand {name}")
        return {"executed": name, "args": args}

    def list_commands(self, contains: str, limit: int = 200) -> dict[str, Any]:
        dte = self._need()
        cmds = dte.Commands
        needle = contains.lower()
        out = []
        n = cmds.Count
        for i in range(1, n + 1):
            try:
                c = cmds.Item(i)
                nm = c.Name
            except Exception:  # noqa: BLE001
                continue
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

    def _select_plc_project_node(self) -> str | None:
        """Make a PLC project the active one by selecting its node in Solution Explorer (what a click does).
        Solution Explorer nests it as Solution → <TwinCAT project> → PLC → <PLC> → '<PLC> Project'; the node is
        found by walking the UI hierarchy (fixed paths missed the TwinCAT project level — retest #10)."""
        try:
            dte = self._need()
            projs = self.plc_projects().get("projects") or []
            if not projs:
                return None
            pname = str(projs[0].get("name") or "").removesuffix(" Project")
            want = {f"{pname} Project".lower(), pname.lower()}
            # #10: the cached 7.0 gen_py DTE wrapper has no ToolWindows (D20) — late-bound first, then the window object
            se = None
            for get in (lambda: self._dte2_late().ToolWindows.SolutionExplorer,
                        lambda: dte.ToolWindows.SolutionExplorer,
                        lambda: dte.Windows.Item(VS_WINDOW_SOLUTION_EXPLORER).Object):
                try:
                    se = get()
                    se.UIHierarchyItems.Count
                    break
                except Exception:  # noqa: BLE001
                    se = None
            if se is None:
                return None

            def walk(items, depth: int, trail: list[str]):
                if depth > 7:
                    return None
                for i in range(1, items.Count + 1):
                    it = items.Item(i)
                    name = str(it.Name)
                    if name.lower() == f"{pname} Project".lower():
                        return it, trail + [name]
                    try:
                        was = it.UIHierarchyItems.Expanded
                        it.UIHierarchyItems.Expanded = True
                        hit = walk(it.UIHierarchyItems, depth + 1, trail + [name])
                        if hit is None:
                            it.UIHierarchyItems.Expanded = was
                        if hit is not None:
                            return hit
                    except Exception:  # noqa: BLE001
                        continue
                return None
            hit = walk(se.UIHierarchyItems, 0, [])
            if hit is None:
                return None
            item, trail = hit
            item.Select(1)   # vsUISelectionTypeSelect
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
        except Exception:  # noqa: BLE001
            return None

    # ITcSmTreeItem.ConsumeXml on the nested PLC project (Automation Interface ≥ 3.1.4010, "Handling online
    # functionalities"): no active project, no Solution Explorer, no UI language involved
    _PLC_ONLINE_XML = {"login": "LoginCmd", "logout": "LogoutCmd", "start": "StartCmd", "stop": "StopCmd",
                       "reset_cold": "ResetColdCmd", "reset_origin": "ResetOriginCmd"}

    def plc_action(self, action: str, plc_project_path: str | None = None) -> dict[str, Any]:
        """login/logout/start/stop/reset_cold/reset_origin/download. #10: first the documented Automation Interface
        route — ConsumeXml(<IECProjectDef><OnlineSettings><Commands><LoginCmd>true…) on '<Name> Project' — which needs
        no active PLC project; then the DTE commands as before (4024: PLC.Login; 4026: PLC.Loginto<ActiveProject>),
        selecting the project node when the shell only offers '…none'."""
        cands = list(PLC_LOGIN_COMMANDS.get(action) or [])
        if not cands:
            raise XaeError(f"unknown PLC action {action!r}; valid: {list(PLC_LOGIN_COMMANDS)}")
        ai_note = ""
        tag = self._PLC_ONLINE_XML.get(action)
        if tag:
            try:
                nested = self._nested_projects(plc_project_path)
            except Exception as exc:  # noqa: BLE001
                nested, ai_note = [], f"PLC projects not listed: {str(exc)[:120]}"
            if len(nested) == 1:
                xml = (f"<TreeItem><IECProjectDef><OnlineSettings><Commands><{tag}>true</{tag}></Commands>"
                       "</OnlineSettings></IECProjectDef></TreeItem>")
                try:
                    it = self._item(nested[0])
                    self._retry(lambda: it.ConsumeXml(xml), f"ConsumeXml {tag}")
                    return {"action": action, "method": f"ConsumeXml {tag}", "project": nested[0], "ok": True}
                except XaeError as exc:
                    ai_note = f"ConsumeXml {tag} on {nested[0]}: {str(exc)[:200]}"
            elif len(nested) > 1:
                ai_note = f"{len(nested)} PLC projects — name one with plc_project_path: {nested}"
        tried: list[dict[str, str]] = []
        selected = None
        for attempt in range(2):
            dyn = [c for c in self._dynamic_plc_commands(action) if c not in cands]
            real = [c for c in dyn if not c.lower().endswith("none")]
            if not real and dyn and attempt == 0:
                selected = self._select_plc_project_node()     # only '…none' offered: no active project
                if selected:
                    continue
            for c in cands + real:
                try:
                    self.command(c)
                    out = {"action": action, "command": c, "ok": True, "tried": tried, "selected_node": selected}
                    if ai_note:
                        out["automation_interface"] = ai_note
                    return out
                except XaeError as exc:
                    tried.append({"command": c, "error": str(exc)[:200]})
            break
        offered = self._dynamic_plc_commands(action)
        raise XaeError(
            (f"Automation Interface route failed ({ai_note}); " if ai_note else "")
            + f"no working DTE command for {action!r}: tried {[t['command'] for t in tried]}; this shell offers {offered or 'nothing matching'}. "
            + ("A '…none' command means no PLC project is active: click the PLC project node once in Solution Explorer, then retry. "
               if any(o.lower().endswith("none") for o in offered) else "")
            + "ads_plc_state covers run/stop/reset via ADS without the shell."
        )

    # ------------------------------------------------------------ target / activate
    def target_get(self) -> dict[str, Any]:
        sm = self._sm()
        return {"target_net_id": self._retry(lambda: sm.GetTargetNetId(), "GetTargetNetId"),
                "twincat_started": bool(self._retry(lambda: sm.IsTwinCATStarted(), "IsTwinCATStarted"))}

    def target_set(self, net_id: str) -> dict[str, Any]:
        sm = self._sm()
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){5}", net_id):
            raise XaeError(f"not an AMS NetId: {net_id!r}")
        self._retry(lambda: sm.SetTargetNetId(net_id), "SetTargetNetId")
        return self.target_get()

    def activate_configuration(self) -> dict[str, Any]:
        sm = self._sm()
        t0 = time.time()
        self._retry(lambda: sm.ActivateConfiguration(), "ActivateConfiguration")
        return {"activated": True, "target": sm.GetTargetNetId(), "duration_s": round(time.time() - t0, 1),
                "note": "Configuration is on the target. Call xae_restart_twincat to switch it to RUN."}

    def restart_twincat(self) -> dict[str, Any]:
        sm = self._sm()
        t0 = time.time()
        self._retry(lambda: sm.StartRestartTwinCAT(), "StartRestartTwinCAT")
        started = None
        for _ in range(60):
            try:
                started = bool(sm.IsTwinCATStarted())
                if started:
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1.0)
        return {"restarted": True, "twincat_started": started, "duration_s": round(time.time() - t0, 1)}

    # ------------------------------------------------------------ tree
    def _item(self, path: str) -> Any:
        sm = self._sm()
        return self._retry(lambda: sm.LookupTreeItem(path), f"LookupTreeItem {path}")

    @staticmethod
    def _item_dict(it: Any) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for prop in ("Name", "PathName", "ItemType", "ItemSubType", "ChildCount", "Comment", "Disabled"):
            try:
                d[prop] = getattr(it, prop)
            except Exception:  # noqa: BLE001
                pass
        return d

    def tree_item(self, path: str) -> dict[str, Any]:
        return self._item_dict(self._item(path))

    def tree_children(self, path: str, depth: int = 1, max_items: int = 2000) -> dict[str, Any]:
        root = self._item(path)
        out: list[dict[str, Any]] = []
        unreadable: list[dict[str, Any]] = []

        def _name(x: Any) -> str:
            try:
                return str(x.Name)
            except Exception:  # noqa: BLE001
                return ""

        def walk(it: Any, lvl: int) -> None:
            if lvl > depth or len(out) >= max_items:
                return
            children: list[Any] = []
            try:
                ipath = str(it.PathName)
            except Exception:  # noqa: BLE001
                ipath = ""
            try:
                n = int(it.ChildCount)
                for i in range(1, n + 1):
                    try:
                        children.append(it.Child(i))
                    except Exception as exc:  # noqa: BLE001
                        unreadable.append({"parent": ipath, "index": i, "error": str(exc)[:160]})
            except Exception:  # noqa: BLE001
                n = 0
            # #17: the PLC container 'TIPC^<Name>' enumerates '<Name> Instance' but not its nested '<Name> Project'
            # (the item users need most); that one is reachable by path
            if ipath.upper().startswith("TIPC^") and ipath.count("^") == 1:
                want = f"{_name(it)} Project"
                if want.strip() != "Project" and want not in {_name(c) for c in children}:
                    try:
                        children.insert(0, self._sm().LookupTreeItem(f"{ipath}^{want}"))
                    except Exception:  # noqa: BLE001
                        pass
            if not children:
                # the nested PLC project node (ItemType 56) does not enumerate its children through ChildCount;
                # its folders are reachable by path
                try:
                    ptype = str(it.ItemType)
                    ppath = str(it.PathName)
                except Exception:  # noqa: BLE001
                    ptype, ppath = "", ""
                if ptype == str(TREEITEMTYPE_PLCAPP) or str(getattr(it, "Name", "")).endswith(" Project"):
                    for sub in PLC_PROJECT_FOLDERS:
                        try:
                            children.append(self._sm().LookupTreeItem(f"{ppath}^{sub}"))
                        except Exception:  # noqa: BLE001
                            continue
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
        it = self._item(path)
        xml = self._retry(lambda: it.ProduceXml(False), f"ProduceXml {path}")
        return {"path": path, "xml": xml}

    def tree_consume_xml(self, path: str, xml: str) -> dict[str, Any]:
        it = self._item(path)
        self._retry(lambda: it.ConsumeXml(xml), f"ConsumeXml {path}")
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
        if info is None:
            return self._retry(lambda: parent.CreateChild(name, subtype, before), "CreateChild")
        try:
            return self._retry(lambda: parent.CreateChild(name, subtype, before, info), "CreateChild")
        except XaeError:
            if isinstance(info, list) and IS_WIN:  # pragma: no cover - retry as SAFEARRAY of BSTR
                variant = win32com.client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_BSTR, [str(x) for x in info])
                return self._retry(lambda: parent.CreateChild(name, subtype, before, variant), "CreateChild(BSTR[])")
            raise

    def tree_create_child(self, parent_path: str, name: str, subtype: int, before: str = "",
                          info: Any = None) -> dict[str, Any]:
        parent = self._item(parent_path)
        return self._item_dict(self._create_child(parent, name, subtype, before, info))

    def tree_delete_child(self, parent_path: str, name: str) -> dict[str, Any]:
        parent = self._item(parent_path)
        self._retry(lambda: parent.DeleteChild(name), f"DeleteChild {name}")
        return {"deleted": f"{parent_path}^{name}"}

    # ------------------------------------------------------------ PLC
    def plc_projects(self) -> dict[str, Any]:
        """List PLC projects: TIPC children → each has one '<Name> Project' node."""
        res = self.tree_children("TIPC", depth=2)
        projs = []
        for it in res["items"]:
            name = str(it.get("Name", ""))
            if str(it.get("ItemType")) == str(TREEITEMTYPE_PLCAPP) or name.endswith(" Project"):
                outer, nested = _plc_split(str(it.get("PathName")))
                base = outer.split("^")[-1]
                projs.append({"name": name, "path": it.get("PathName"), "item_type": it.get("ItemType"),
                              "plc_project_path": it.get("PathName"),
                              "plc_path": outer, "iec_project_path": nested, "instance_path": f"{outer}^{base} Instance"})
        return {"count": len(projs), "projects": projs, "raw": res["items"],
                "note": ("every tool takes any of these paths (R14): boot project / autostart act on plc_path "
                         "(ITcPlcProject), PLCopen import/export and Check all objects on iec_project_path "
                         "(ITcPlcIECProject), links on instance_path; POUs live at <iec_project_path>^POUs")}

    def pou_get(self, path: str, include_subitems: bool = True) -> dict[str, Any]:
        it = self._item(path)
        d = self._item_dict(it)
        d["declaration"] = _try(lambda: it.DeclarationText)
        d["implementation"] = _try(lambda: it.ImplementationText)
        d["language"] = _try(lambda: it.Language)
        if include_subitems:
            subs = []
            try:
                for i in range(1, int(it.ChildCount) + 1):
                    ch = it.Child(i)
                    sd = self._item_dict(ch)
                    sd["declaration"] = _try(lambda: ch.DeclarationText)
                    sd["implementation"] = _try(lambda: ch.ImplementationText)
                    subs.append(sd)
            except Exception:  # noqa: BLE001
                pass
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
                   declaration: str | None = None, implementation: str | None = None) -> dict[str, Any]:
        kind_l = kind.lower()
        if kind_l not in POU_SUBTYPE:
            raise XaeError(f"kind must be one of {sorted(POU_SUBTYPE)}")
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
        elif kind_l in ("action", "transition", "property_get", "property_set"):
            info = lang
        elif kind_l in ("enum", "struct", "union", "alias", "gvl", "parameter_list"):
            info = declaration if declaration else None
        elif kind_l == "interface":
            info = extends if extends else None
        elif kind_l in ("itf_method", "itf_property"):
            info = return_type or "BOOL"
        else:
            info = None
        parent = self._item(parent_path)
        child = self._create_child(parent, name, sub, "", info)
        created = self._item_dict(child)
        path = created.get("PathName") or f"{parent_path}^{name}"
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

    def plcopen_import(self, plc_project_path: str, xml_file: str, option: str = "none") -> dict[str, Any]:
        opts = {"none": 0, "rename": 1, "replace": 2, "skip": 3}
        if option not in opts:
            raise XaeError(f"option must be one of {list(opts)}")
        if not os.path.isfile(xml_file):
            raise XaeError(f"PLCopen XML not found: {xml_file}")
        plc_project_path = _plc_split(plc_project_path)[1]          # R14: PLCopen lives on '<Name> Project'
        it = self._item(plc_project_path)
        names = _plcopen_names(xml_file)

        def snapshot() -> dict[str, str]:
            try:
                items = self.tree_children(plc_project_path, depth=8, max_items=20000)["items"]
            except Exception:  # noqa: BLE001
                return {}
            return {str(x.get("PathName")): str(x.get("Name")) for x in items if x.get("PathName")}
        before = snapshot()
        # all four arguments: the folder structure from the XML is kept (bFolderStructure, default TRUE in the docs)
        try:
            self._retry(lambda: it.PlcOpenImport(xml_file, opts[option], "", True), "PlcOpenImport")
        except XaeError:
            self._retry(lambda: it.PlcOpenImport(xml_file, opts[option]), "PlcOpenImport")
        after = snapshot()
        added = {p: n for p, n in after.items() if p not in before}
        res: dict[str, Any] = {"imported": xml_file, "into": plc_project_path, "option": option,
                               "objects_in_file": names, "added": sorted(added)[:200]}
        # R15: 'replace' replaces an object at the SAME place; when the XML carries no folder for it, TwinCAT puts it at
        # the project root and renames it (PRG_X_1) because the name exists elsewhere — a silent duplicate
        dup = [p for p, n in added.items() if any(re.fullmatch(re.escape(o) + r"_\d+", n, re.I) for o in names)]
        if dup:
            res["duplicates_created"] = dup
            if option == "replace":
                raise XaeError(f"PlcOpenImport(replace) did not replace — it created {dup} next to the existing object(s): "
                               "the XML has no folder for them, so TwinCAT imported them at the project root and renamed them. "
                               "Delete the copies (xae_tree_delete), then export the object together with its folder (or "
                               "import into the folder that holds it) and import again.")
        return res

    def plcopen_export(self, plc_project_path: str, items: list[str], xml_file: str) -> dict[str, Any]:
        plc_project_path = _plc_split(plc_project_path)[1]          # R14: 'TIPC^X' raised AttributeError
        it = self._item(plc_project_path)
        sel = ";".join(items)
        self._retry(lambda: it.PlcOpenExport(xml_file, sel), "PlcOpenExport")
        return {"exported": items, "file": xml_file}

    def boot_project(self, plc_project_path: str, autostart: bool | None, generate: bool,
                     activate_after_generate: bool = False) -> dict[str, Any]:
        plc_project_path = _plc_split(plc_project_path)[0]          # R14: boot project lives on 'TIPC^X'
        it = self._item(plc_project_path)
        res: dict[str, Any] = {"path": plc_project_path}
        if autostart is not None:
            def _set() -> None:
                it.BootProjectAutostart = bool(autostart)
            self._retry(_set, "BootProjectAutostart")
            res["autostart"] = bool(autostart)
        if generate:
            self._retry(lambda: it.GenerateBootProject(bool(activate_after_generate)), "GenerateBootProject")
            res["boot_project_generated"] = True
        res["current_autostart"] = _try(lambda: bool(it.BootProjectAutostart))
        return res

    def io_disable(self, disable: bool = True, only: list[str] | None = None) -> dict[str, Any]:
        """Set the Disabled flag on every I/O device under TIID (or the named ones) —
        lets a configuration activate on a PC/target without the hardware (TcUnit runs)."""
        root = self._item("TIID")
        changed = []
        n = int(root.ChildCount)
        for i in range(1, n + 1):
            dev = root.Child(i)
            name = _try(lambda: dev.Name) or f"device {i}"
            if only and name not in only:
                continue
            try:
                dev.Disabled = 1 if disable else 0   # SMDS_DISABLED / SMDS_NOT_DISABLED
                changed.append({"device": name, "disabled": bool(disable)})
            except Exception as exc:  # noqa: BLE001
                changed.append({"device": name, "error": _com_msg(exc)})
        return {"devices": changed, "note": "takes effect on the next ActivateConfiguration"}

    def links(self, plc_project_path: str) -> dict[str, Any]:
        """Variable links of the PLC instance (inputs/outputs groups) from their XML:
        name, type, link target; lists unlinked AT %I*/%Q* variables."""
        outer = _plc_split(plc_project_path)[0]
        inst_path = f"{outer}^{outer.split('^')[-1]} Instance"
        inst = self._item(inst_path)
        groups = []
        unlinked = []
        n = int(inst.ChildCount)
        for i in range(1, n + 1):
            grp = inst.Child(i)
            gname = _try(lambda: grp.Name) or f"group {i}"
            try:
                xml = grp.ProduceXml(False)
                root = ET.fromstring(xml)
            except Exception as exc:  # noqa: BLE001
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
        return [p["iec_project_path"] for p in (self.plc_projects().get("projects") or [])]

    def check_all_objects(self, plc_project_path: str | None = None) -> dict[str, Any]:
        """PLC 'Check all objects' (compile every POU incl. unused). #45: first the documented Automation Interface call
        ITcPlcIECProject2.CheckAllObjects() on the nested project — no UI, no active project needed — then the
        shell commands as before."""
        ai_err = ""
        try:
            nested = self._nested_projects(plc_project_path)
        except Exception as exc:  # noqa: BLE001
            nested, ai_err = [], str(exc)[:160]
        if len(nested) == 1:
            it = self._item(nested[0])
            try:
                self._retry(lambda: _com_call(it, "CheckAllObjects"), "ITcPlcIECProject2.CheckAllObjects")
                time.sleep(0.5)
                return {"method": "ITcPlcIECProject2.CheckAllObjects", "project": nested[0], **self.errors()}
            except XaeError as exc:
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
                    out = {"command": c, **self.errors()}
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
        dte = self._need()
        found: list[dict[str, Any]] = []

        def visit(p: Any, depth: int) -> None:
            if depth > 3:
                return
            fn = str(_try(lambda: p.FullName) or "")
            items = _try(lambda: p.ProjectItems)
            if items is None:
                return
            for j in range(1, (_try(lambda: items.Count) or 0) + 1):
                it = _try(lambda: items.Item(j))
                if it is None:
                    continue
                sub = _try(lambda: it.SubProject)          # projects inside solution folders
                if sub is not None:
                    visit(sub, depth + 1)
                    continue
                obj = _try(lambda: it.Object)
                ms = _measurement_scope(obj) if fn.lower().endswith(".tcmproj") and obj is not None else None
                if ms is not None:
                    found.append({"name": str(it.Name), "project": str(_try(lambda: p.Name) or ""), "file": fn,
                                  "_obj": ms})

        projs = self._retry(lambda: dte.Solution.Projects, "Solution.Projects")
        for i in range(1, projs.Count + 1):
            visit(projs.Item(i), 0)
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
        if path:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        rc = self._retry(lambda: _com_call(obj, meth, *args), f"Scope {meth}")
        out: dict[str, Any] = {"scope": f"{d['project']}\\{d['name']}", "action": action, "method": meth,
                               "result_code": rc, "elapsed_s": round(time.monotonic() - t0, 3)}
        if path:
            p = Path(path)
            out["path"] = str(p)
            out["written"] = p.is_file() and p.stat().st_size > 0
            if not out["written"]:
                raise XaeError(f"Scope {meth}({path!r}) returned {rc} but no file was written — is there recorded data "
                               "(stop the recording first)?")
        if rc not in (0, None):
            out["warning"] = f"{meth} returned {rc} (0 = success in the Automation Interface examples)"
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
    except Exception:  # noqa: BLE001 — not a Scope node (data pool, chart, …) or no pywin32
        return None
    return ms if _has_member(ms, "StartRecord") else None


def _has_member(obj: Any, name: str) -> bool:
    """Late-bound COM object exposes ``name`` (IDispatch::GetIDsOfNames)."""
    ole = getattr(obj, "_oleobj_", None)
    if ole is None:
        return callable(getattr(obj, name, None))      # plain Python objects (tests)
    try:
        ole.GetIDsOfNames(name)
        return True
    except Exception:  # noqa: BLE001
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
    """Names of the POUs / DUTs / GVLs a PLCopen XML file holds."""
    try:
        txt = Path(xml_file).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    found = re.findall(r"<(?:pou|dataType|globalVars)\b[^>]*\bname=\"([^\"]+)\"", txt)
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


def _try(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001
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
