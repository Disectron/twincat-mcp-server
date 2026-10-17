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
Common roots: TIID (I/O), TIPC (PLC), TIRC (real-time), TIRR (routes),
TINC (NC), TIRS (safety), TIRT (tasks under TIRC^Tasks).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import time
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

VS_WINDOW_ERRORLIST = "{D78612C7-9962-4B83-95D9-268046DAD23A}"
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

    def _sm(self) -> Any:
        if self.sysman is None:
            self._need()
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
        try:
            sm = self._sm()
            d["twincat_project"] = self._sysman_project_name
            d["target_net_id"] = sm.GetTargetNetId()
            d["twincat_started"] = bool(sm.IsTwinCATStarted())
        except Exception as exc:  # noqa: BLE001
            d["sysman_error"] = _com_msg(exc)
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

    def save_all(self) -> dict[str, Any]:
        dte = self._need()
        self._retry(lambda: dte.ExecuteCommand("File.SaveAll"), "File.SaveAll")
        return {"saved": True}

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

    def _error_items(self) -> tuple[list[dict[str, Any]], str]:
        """Return (items, source). Tries DTE2.ToolWindows.ErrorList, then the
        ErrorList tool window object, then parses the Build output pane."""
        dte = self._need()
        lvl = {1: "error", 2: "warning", 3: "message"}
        # route 1: DTE2 cast
        d2 = self._dte2()
        if d2 is not None:
            try:
                items = d2.ToolWindows.ErrorList.ErrorItems
                return [self._error_item(items.Item(i), lvl) for i in range(1, items.Count + 1)], "DTE2.ToolWindows"
            except Exception:  # noqa: BLE001
                pass
        # route 2: window object
        try:
            win = dte.Windows.Item(VS_WINDOW_ERRORLIST)
            el = win.Object
            items = el.ErrorItems
            return [self._error_item(items.Item(i), lvl) for i in range(1, items.Count + 1)], "Windows.ErrorList"
        except Exception:  # noqa: BLE001
            pass
        # route 3: parse output pane
        txt = self.build_output(tail_lines=2000)["text"]
        return _parse_build_output(txt), "OutputPane"

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

    def errors(self) -> dict[str, Any]:
        items, source = self._error_items()
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
        """Make a PLC project the active one by selecting its node in Solution Explorer (what a click does)."""
        try:
            dte = self._need()
            sol_name = Path(str(dte.Solution.FullName)).stem
            projs = self.plc_projects().get("projects") or []
            if not projs:
                return None
            pname = str(projs[0].get("name") or "").removesuffix(" Project")
            se = dte.ToolWindows.SolutionExplorer
            for cand in (f"{sol_name}\\{pname}\\{pname} Project", f"{sol_name}\\PLC\\{pname}\\{pname} Project",
                         f"{sol_name}\\{pname}"):
                try:
                    item = se.GetItem(cand)
                    item.Select(1)   # vsUISelectionTypeSelect
                    return cand
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            return None
        return None

    def plc_action(self, action: str) -> dict[str, Any]:
        """login/logout/start/stop/reset_cold/reset_origin/download via DTE commands. Command names
        differ between shells (4024: PLC.Login; 4026: PLC.Loginto<ActiveProject>): the static names are
        tried first, then the shell's own PLC.* list; a '…none' variant means no PLC project is active,
        so the project node is selected in Solution Explorer and the list re-read."""
        cands = list(PLC_LOGIN_COMMANDS.get(action) or [])
        if not cands:
            raise XaeError(f"unknown PLC action {action!r}; valid: {list(PLC_LOGIN_COMMANDS)}")
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
                    return {"action": action, "command": c, "ok": True, "tried": tried, "selected_node": selected}
                except XaeError as exc:
                    tried.append({"command": c, "error": str(exc)[:200]})
            break
        offered = self._dynamic_plc_commands(action)
        raise XaeError(
            f"no working DTE command for {action!r}: tried {[t['command'] for t in tried]}; this shell offers {offered or 'nothing matching'}. "
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

        def walk(it: Any, lvl: int) -> None:
            if lvl > depth or len(out) >= max_items:
                return
            children: list[Any] = []
            try:
                n = int(it.ChildCount)
                for i in range(1, n + 1):
                    try:
                        children.append(it.Child(i))
                    except Exception:  # noqa: BLE001
                        continue
            except Exception:  # noqa: BLE001
                n = 0
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
        return {"path": path, "count": len(out), "items": out}

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
                projs.append({"name": name, "path": it.get("PathName"), "item_type": it.get("ItemType"),
                              "plc_project_path": it.get("PathName")})
        return {"count": len(projs), "projects": projs, "raw": res["items"],
                "note": "plc_project_path is what xae_deploy / xae_links / tcunit_run take; POUs live at <path>^POUs"}

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
        it = self._item(plc_project_path)
        self._retry(lambda: it.PlcOpenImport(xml_file, opts[option]), "PlcOpenImport")
        return {"imported": xml_file, "into": plc_project_path, "option": option}

    def plcopen_export(self, plc_project_path: str, items: list[str], xml_file: str) -> dict[str, Any]:
        it = self._item(plc_project_path)
        sel = ";".join(items)
        self._retry(lambda: it.PlcOpenExport(xml_file, sel), "PlcOpenExport")
        return {"exported": items, "file": xml_file}

    def boot_project(self, plc_project_path: str, autostart: bool | None, generate: bool,
                     activate_after_generate: bool = False) -> dict[str, Any]:
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
        inst_path = plc_project_path.replace(" Project", " Instance")
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

    def check_all_objects(self) -> dict[str, Any]:
        """PLC 'Check all objects' (compile every POU incl. unused) via command."""
        cands = ["PLC.CheckAllObjects", "OtherContextMenus.PlcProject.CheckAllObjects"]
        # the name varies by shell and UI language (4026: PLC.CheckAllObjects<Project>, localised shells differ):
        # take whatever the shell lists under 'Check' that is not a plain syntax check
        try:
            listed = self.list_commands("Check", 200).get("commands", [])
            cands += [c for c in listed if "checkallobject" in c.lower().replace(" ", "") and c not in cands]
        except XaeError:
            listed = []
        tried = []
        for c in cands:
            try:
                self.command(c)
                time.sleep(1.0)
                return {"command": c, **self.errors()}
            except XaeError as exc:
                tried.append({"command": c, "error": str(exc)[:120]})
        raise XaeError(f"no CheckAllObjects command found in this shell (tried {[t['command'] for t in tried]}; "
                       f"shell lists {listed[:10]}). Unused POUs are then not compiled — xae_build still compiles everything "
                       f"referenced. In the IDE: right-click the PLC project → Check all objects.")


# ----------------------------------------------------------------------------
_RE_OUT_ERR = re.compile(
    r"(?:^|[\s:>\]\)])(?P<level>error|warning)(?!\(s\))\s*(?P<code>[A-Z]{1,2}\d{1,5}|\d{1,5})?\s*:?\s+(?P<msg>\S.*)$", re.I)
_RE_FILE_LINE = re.compile(r"(?P<file>[A-Za-z]:\\[^\(\[]+?\.(?:TcPOU|TcDUT|TcGVL|TcIO|tsproj|plcproj))\s*[\(\[]\s*(?P<line>\d+)")


def _parse_build_output(text: str) -> list[dict[str, Any]]:
    items = []
    for ln in text.splitlines():
        m = _RE_OUT_ERR.search(ln)
        if not m:
            continue
        f = _RE_FILE_LINE.search(ln)
        items.append({
            "level": m.group("level").lower(),
            "description": m.group("msg").strip(),
            "file": f.group("file") if f else "",
            "line": int(f.group("line")) if f else 0,
            "column": 0,
            "project": "",
            "raw": ln.strip(),
        })
    return items


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
