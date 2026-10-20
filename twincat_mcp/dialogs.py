"""XAE modal-dialog watchdog (Windows).

A COM call into the XAE shell blocks for as long as a modal dialog is open
("Save changes?", "Activate configuration?", "Login: online change?"…).
Without eyes on the screen the agent only sees a timeout. This module

* enumerates the modal windows of the XAE process: top-level dialog windows (class ``#32770``) with their title,
  static text and buttons — and (N173) the window that holds the shell modal whatever its class (WPF
  ``HwndWrapper[…]``, WinForms ``WindowsForms10.*`` …): the shell is modal when its main window is disabled; such a
  window's buttons and text are read through UI Automation;
* clicks a button on request (``xae_dialog_resolve``) — a ``#32770`` button with BM_CLICK, another window's through
  UI Automation (Invoke); a window whose buttons cannot be read is never pressed (no WM_CLOSE, no Escape);
* optionally auto-answers dialogs that match configured rules
  (``xae.dialog_rules``) from a background thread, so an allowlisted prompt
  never stalls automation;
* never auto-answers prompts that change the machine (activate, run mode,
  restart, download, login/online change, delete, overwrite, safety) unless a
  rule says ``force: true`` — those are reported instead.

Everything is best effort and Windows-only (pywin32; UI Automation through ctypes). On other platforms the
functions return empty results.
"""
from __future__ import annotations

import concurrent.futures as cf
import ctypes
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

IS_WIN = sys.platform == "win32"
if IS_WIN:  # pragma: no cover
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore

DIALOG_CLASS = "#32770"
BS_TYPEMASK = 0x0F
# N86: what answers a dialog. BS_PUSHBUTTON, BS_DEFPUSHBUTTON, BS_OWNERDRAW (drawn by the dialog, pressed like a
# button), BS_SPLITBUTTON, BS_DEFSPLITBUTTON, BS_COMMANDLINK, BS_DEFCOMMANDLINK. Checkboxes, radio buttons and group
# boxes are class "Button" too — clicking "Show status bar" would toggle an IDE setting, not answer the dialog.
_PUSH_TYPES = frozenset({0x0, 0x1, 0xB, 0xC, 0xD, 0xE, 0xF})
_OPTION_TYPES = frozenset({0x2, 0x3, 0x4, 0x5, 0x6, 0x9})      # check boxes, 3-state, radio buttons
_NOT_AN_ANSWER = frozenset({"cancel", "abort", "stop", "close", "abbrechen"})
NEVER_AUTO = ("activate", "run mode", "restart", "download", "login", "online change", "logout",
              "delete", "remove", "overwrite", "safety", "twinsafe", "reset", "boot project", "license")

# N173: how a modal window is told from the other windows of the shell
GW_OWNER = 4                       # GetWindow(): the owner of a top-level window
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020     # click-through (the shell's glow windows around its main window)
WS_EX_NOACTIVATE = 0x08000000      # never activated: it cannot take an answer
BUTTONS_UNKNOWN = "not a standard dialog — buttons unknown"
UNIDENTIFIED = "XAE's main window is disabled (a modal window is open) but it could not be identified"
CANNOT_PRESS = "this window's buttons can't be pressed by the tool — close it in XAE"

# N173: the shell's main window per process — DTE.MainWindow.HWnd, recorded when the server attaches
# (XaeBackend._take). Without it, the visible unowned windows of the process stand for it.
_MAIN_WINDOWS: dict[int, int] = {}


def set_main_window(pid: int | None, hwnd: int | None) -> None:
    """Record (or forget, hwnd None) the main window of the XAE process ``pid``: the window whose being disabled
    means the shell is modal (N173)."""
    if pid is None:
        return
    if hwnd:
        _MAIN_WINDOWS[int(pid)] = int(hwnd)
    else:
        _MAIN_WINDOWS.pop(int(pid), None)


def button_kind(style: int) -> str:
    """'push' (answers the dialog), 'option' (check box / radio button) or 'other' (group box …), from the
    Button control's window style."""
    t = style & BS_TYPEMASK
    return "push" if t in _PUSH_TYPES else "option" if t in _OPTION_TYPES else "other"


@dataclass
class Dialog:
    hwnd: int
    pid: int
    title: str
    text: str
    # (hwnd, caption) — push buttons only (N86); hwnd 0 for a button read through UI Automation (not a window of
    # its own). None (N173): the window is not a standard dialog and its buttons could not be read.
    buttons: list[tuple[int, str]] | None = field(default_factory=list)
    options: list[str] = field(default_factory=list)               # check boxes / radio buttons: never clicked
    cls: str = DIALOG_CLASS
    via: str = "win32"            # how a button is pressed: 'win32' (BM_CLICK), 'uia' (UI Automation Invoke), 'none'
    identified: bool = True       # False (N173): the main window is disabled, no window holding it could be named —
    note: str | None = None       # hwnd / cls / title are then the main window's

    def captions(self) -> list[str] | None:
        return None if self.buttons is None else [c for _, c in self.buttons]

    def to_dict(self) -> dict[str, Any]:
        d = {"hwnd": self.hwnd, "title": self.title, "class": self.cls, "text": self.text[:1000],
             "buttons": self.captions(), "machine_affecting": self.machine_affecting()}
        if self.buttons is None:
            d["buttons_note"] = self.note or BUTTONS_UNKNOWN
        elif self.via == "uia":
            d["read_by"] = "UI Automation"
        if not self.identified:
            d["unidentified"] = True
        if self.options:
            d["options"] = self.options[:30]
        return d

    def machine_affecting(self) -> bool:
        blob = f"{self.title} {self.text}".lower()
        return any(k in blob for k in NEVER_AUTO)

    def awaits_answer(self) -> bool:
        """A question or a message the shell waits on: a push button other than Cancel / Abort (a progress window
        offers only those). N173: buttons unknown — a window that holds the shell modal — is taken as waiting."""
        return awaits(self.to_dict())


def awaits(d: dict[str, Any]) -> bool:
    """The rule of Dialog.awaits_answer on a dialog's dict (what the server keeps)."""
    b = d.get("buttons")
    if b is None:
        return True
    return any(str(c).lower() not in _NOT_AN_ANSWER for c in b)


def label(d: dict[str, Any]) -> str:
    """How a message names a dialog (from its dict): its title in quotes — or, for a modal state no window could be
    named for (N173), what is known about it."""
    if d.get("unidentified"):
        return (f"{UNIDENTIFIED} (main window hwnd {d.get('hwnd')}, class {d.get('class')!r}, title "
                f"{d.get('title')!r})")
    t = str(d.get("title") or "")
    return f"'{t}'" if t else f"an untitled window (hwnd {d.get('hwnd')}, class {d.get('class')!r})"


def buttons_text(d: dict[str, Any]) -> str:
    b = d.get("buttons")
    return f"buttons {b}" if b is not None else f"buttons unknown ({d.get('buttons_note') or BUTTONS_UNKNOWN})"


def _clean(caption: str) -> str:
    return caption.replace("&", "").strip()


def _safe(fn: Callable[..., Any], *a: Any, default: Any = None) -> Any:
    try:
        return fn(*a)
    except Exception:  # noqa: BLE001 — a window that went away between two calls
        return default


def list_dialogs(pid: int | None) -> list[Dialog]:
    """The modal windows of the XAE process ``pid``: every top-level #32770 dialog (with its static text and push
    buttons), and — N173 — while the shell's main window is disabled, the window that holds it, whatever its class
    (or one entry that says it could not be named). With ``pid`` None the answer is empty on purpose: without a known
    shell process the scan would list — and ``xae_dialog_resolve`` could click — any program's dialog on the desktop
    (Windows CI surfaced a 'System Properties' prompt)."""
    if not IS_WIN or pid is None:
        return []
    tops = _top_windows(pid)
    found = [d for d in (_std_dialog(h, pid) for h in tops if _safe(win32gui.GetClassName, h) == DIALOG_CLASS)
             if d is not None]
    try:
        found += _modal_windows(pid, tops, {d.hwnd for d in found})
    except Exception:  # noqa: BLE001 — best effort, as the #32770 scan
        pass
    _uia_forget(set(tops))
    return found


def _top_windows(pid: int) -> list[int]:
    """The visible top-level windows of the process, in Z order."""
    out: list[int] = []

    def on_top(hwnd: int, _: Any) -> bool:
        try:
            if win32gui.IsWindowVisible(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == pid:
                out.append(hwnd)
        except Exception:  # noqa: BLE001
            pass
        return True

    win32gui.EnumWindows(on_top, None)
    return out


def _std_dialog(hwnd: int, pid: int) -> Dialog | None:
    """A #32770 dialog read from its child controls."""
    try:
        title = win32gui.GetWindowText(hwnd)
        texts: list[str] = []
        buttons: list[tuple[int, str]] = []
        options: list[str] = []

        def on_child(ch: int, _a: Any) -> bool:
            cls = win32gui.GetClassName(ch)
            txt = win32gui.GetWindowText(ch)
            if cls == "Button" and txt:
                kind = button_kind(win32gui.GetWindowLong(ch, win32con.GWL_STYLE))
                if kind == "push":
                    buttons.append((ch, _clean(txt)))
                elif kind == "option":
                    options.append(_clean(txt))
            elif cls in ("Static", "RichEdit20W", "Edit", "SysLink") and txt:
                texts.append(txt)
            return True

        win32gui.EnumChildWindows(hwnd, on_child, None)
        return Dialog(hwnd, pid, title, "\n".join(texts), buttons, options)
    except Exception:  # noqa: BLE001
        return None


def _owner(hwnd: int) -> int:
    return int(_safe(win32gui.GetWindow, hwnd, GW_OWNER, default=0) or 0)


def _owned_by(hwnd: int, root: int) -> bool:
    """root is hwnd's owner, or its owner's owner … (a dialog opened from a dialog)."""
    seen: set[int] = set()
    o = _owner(hwnd)
    while o and o not in seen and len(seen) < 16:
        if o == root:
            return True
        seen.add(o)
        o = _owner(o)
    return False


def _takes_input(hwnd: int) -> bool:
    """Not a click-through or never-activated window (the glow windows around the shell's main window): those
    cannot take an answer, so they are never the modal window."""
    ex = int(_safe(win32gui.GetWindowLong, hwnd, GWL_EXSTYLE, default=0) or 0)
    return not ex & (WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)


def _modal_windows(pid: int, tops: list[int], listed: set[int]) -> list[Dialog]:
    """N173 (0.8.14 retest): XAE's About box and Options window are not #32770 dialogs and were never seen. The shell
    is modal when its main window (DTE.MainWindow.HWnd; without it, the visible unowned windows of the process) is
    DISABLED. The window that holds it is a visible, enabled window of the process that can take input and is owned
    by it — the one the owner activated last when there are several (a floating tool window that stays enabled is
    not it) — else an unowned one (a dialog shown without an owner); of any class. A disabled main window is the
    trigger: while it is enabled, a floating tool window, a torn-off Error List or a splash is no dialog."""
    in_pid = set(tops)
    main = _MAIN_WINDOWS.get(pid)
    # review of 0.8.15: with xae.visible=false the main window is hidden — not among the visible windows it was never
    # checked, and a modal window it owns went unseen. The recorded main window decides while it is a window of the
    # process, visible or not
    known = main is not None and (main in in_pid or (
        _safe(win32gui.IsWindow, main, default=False)
        and (_safe(win32process.GetWindowThreadProcessId, main, default=(0, 0)) or (0, 0))[1] == pid))
    roots = [main] if known else [h for h in tops if _owner(h) not in in_pid]
    held = [r for r in roots if not _safe(win32gui.IsWindowEnabled, r, default=True)]
    if not held:
        return []
    cands = [h for h in tops if h not in held and _safe(win32gui.IsWindowEnabled, h, default=False)
             and _takes_input(h)]
    owned = [h for h in cands if any(_owned_by(h, r) for r in held)]
    last = [p for p in (_safe(_last_active_popup, r) for r in held) if p in owned]
    picks = list(dict.fromkeys(last)) or owned or [h for h in cands if _owner(h) not in in_pid]
    if not picks:
        r = held[0]
        return [Dialog(r, pid, str(_safe(win32gui.GetWindowText, r, default="") or ""), "", None,
                       cls=str(_safe(win32gui.GetClassName, r, default="") or ""), via="none", identified=False,
                       note=UNIDENTIFIED)]
    return [_other_window(h, pid) for h in picks if h not in listed]


def _other_window(hwnd: int, pid: int) -> Dialog:
    """A modal window that is not a #32770 dialog: its buttons and texts as UI Automation reads them, or buttons
    unknown (never dropped)."""
    title = str(_safe(win32gui.GetWindowText, hwnd, default="") or "")
    cls = str(_safe(win32gui.GetClassName, hwnd, default="") or "")
    read = _safe(_uia_read, hwnd)
    if read is None:
        return Dialog(hwnd, pid, title, "", None, cls=cls, via="none", note=BUTTONS_UNKNOWN)
    buttons, texts = read
    return Dialog(hwnd, pid, title, "\n".join(t for t in texts if t),
                  [(0, c) for c in (_clean(b) for b in buttons) if c], cls=cls, via="uia")


def _last_active_popup(hwnd: int) -> int:  # pragma: no cover - needs Windows
    """GetLastActivePopup: the window the owner activated last (the modal one while it is disabled); hwnd itself
    when none."""
    f = _user32().GetLastActivePopup
    return int(f(hwnd) or 0)


_U32: Any = None


def _user32() -> Any:  # pragma: no cover - needs Windows
    global _U32
    if _U32 is None:
        u = ctypes.WinDLL("user32")              # own instance: argtypes set here do not touch ctypes.windll's
        u.GetLastActivePopup.argtypes = [ctypes.c_void_p]
        u.GetLastActivePopup.restype = ctypes.c_void_p
        _U32 = u
    return _U32


def click(dialog: Dialog, button: str) -> bool:
    """Press a button of the dialog: BM_CLICK on a #32770 button, UI Automation Invoke on another window's (N173).
    A window whose buttons are unknown is never pressed — nothing is sent to it."""
    if not IS_WIN or dialog.buttons is None:
        return False
    want = button.strip().lower()
    for hwnd, cap in dialog.buttons:
        if cap.lower() == want or cap.lower().startswith(want):
            try:
                if dialog.via == "uia":
                    return bool(_uia_invoke(dialog.hwnd, cap))
                win32gui.PostMessage(hwnd, win32con.BM_CLICK, 0, 0)
                return True
            except Exception:  # noqa: BLE001
                return False
    return False


def rule_matches(rule: dict[str, Any], d: Dialog) -> bool:
    t = (rule.get("title_contains") or "").lower()
    x = (rule.get("text_contains") or "").lower()
    if d.buttons is None or not d.identified:
        return False                   # N173: a rule never fires on a window whose buttons are unknown
    if t and t not in d.title.lower():
        return False
    if x and x not in d.text.lower():
        return False
    if not t and not x:
        return False
    if d.machine_affecting() and not rule.get("force"):
        return False
    return True


class DialogWatchdog:
    """Polls dialogs of one process; auto-answers by rules; keeps a log."""

    def __init__(self, rules: list[dict[str, Any]], interval_s: float = 0.5):
        self.rules = list(rules or [])
        self.interval = interval_s
        self.pid: int | None = None
        self.log: list[dict[str, Any]] = []
        self.pending: list[Dialog] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, pid: int | None) -> None:
        self.pid = pid
        if not IS_WIN or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="xae-dialog-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:  # pragma: no cover - needs Windows
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(self.interval)

    def poll(self, dialogs: list[Dialog] | None = None) -> list[Dialog]:
        """One pass: resolve by rules, keep the rest as pending. ``dialogs`` injectable for tests."""
        ds = dialogs if dialogs is not None else list_dialogs(self.pid)
        left: list[Dialog] = []
        for d in ds:
            handled = False
            for r in self.rules:
                if rule_matches(r, d):
                    ok = click(d, r.get("button", "OK")) if dialogs is None else True
                    self._record("auto", d, r.get("button", "OK"), ok)
                    handled = ok
                    if ok:
                        break
            if not handled:
                left.append(d)
        with self._lock:
            new = [d for d in left if all(d.hwnd != p.hwnd for p in self.pending)]
            for d in new:
                self._record("seen", d, None, None)
            self.pending = left
        return left

    def _record(self, kind: str, d: Dialog, button: str | None, ok: bool | None) -> None:
        self.log.append({"ts": time.strftime("%H:%M:%S"), "event": kind, "title": d.title, "class": d.cls,
                         "text": d.text[:200], "buttons": d.captions(), "button": button, "ok": ok})
        if len(self.log) > 200:
            del self.log[:50]

    def resolve(self, button: str, hwnd: int | None = None, remember: bool = False) -> dict[str, Any]:
        ds = list_dialogs(self.pid)
        if hwnd is not None:
            ds = [d for d in ds if d.hwnd == hwnd]
        if not ds:
            return {"resolved": False, "reason": "no dialog open" if hwnd is None else f"no dialog with hwnd {hwnd}"}
        d = ds[0]
        if d.buttons is None:
            # N173: a window whose buttons could not be read is refused — never closed (WM_CLOSE / Escape) unasked
            self._record("manual", d, button, False)
            return {"resolved": False, "dialog": d.to_dict(), "button": button, "remembered": False,
                    "reason": f"{label(d.to_dict())}: {CANNOT_PRESS}"}
        ok = click(d, button)
        self._record("manual", d, button, ok)
        if ok and remember and not d.machine_affecting():
            self.rules.append({"title_contains": d.title, "button": button})
        return {"resolved": ok, "dialog": d.to_dict(), "button": button,
                "remembered": bool(ok and remember and not d.machine_affecting()),
                "note": None if ok else f"button {button!r} not found; available: {d.captions()}"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            pend = [d.to_dict() for d in self.pending]
        return {"running": bool(self._thread and self._thread.is_alive()), "pid": self.pid,
                "scope": f"XAE process {self.pid}" if self.pid else "none — no XAE process known; dialogs of other programs are never listed",
                "rules": self.rules, "pending": pend, "recent": self.log[-20:]}


# ---------------------------------------------------------------------------------------------------- UI Automation
# N173: the buttons and text of a modal window that is not a #32770 dialog (WPF, WinForms). pywin32 has no
# IUIAutomation (it is not an IDispatch interface) and comtypes is not a dependency, so the few calls needed go through
# the COM vtables with ctypes. Slots after IUnknown's 0-2, in the order of UIAutomationClient.h:
#   IUIAutomation              6 ElementFromHandle, 23 CreatePropertyCondition
#   IUIAutomationElement       6 FindAll, 14 GetCurrentPatternAs, 23 get_CurrentName
#   IUIAutomationElementArray  3 get_Length, 4 GetElement
#   IUIAutomationInvokePattern 3 Invoke
CLSID_CUIAutomation = "ff48dba4-60ef-4201-aa87-54103eef594e"
IID_IUIAutomation = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
IID_IUIAutomationInvokePattern = "fb377fbe-8ea6-46d5-9c73-6499642d3059"
UIA_ControlTypePropertyId = 30003
UIA_ButtonControlTypeId = 50000
UIA_TextControlTypeId = 50020
UIA_InvokePatternId = 10000
TreeScope_Descendants = 4
VT_I4 = 3
UIA_WAIT_S = 1.0            # a scan waits this long for one window; a later answer is kept for the next scan
UIA_INVOKE_WAIT_S = 5.0
UIA_MAX_ELEMENTS = 200


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16),
                ("Data4", ctypes.c_ubyte * 8)]


def _guid(s: str) -> _GUID:
    return _GUID.from_buffer_copy(uuid.UUID(s).bytes_le)


class _VariantValue(ctypes.Union):
    _fields_ = [("lVal", ctypes.c_int32), ("pair", ctypes.c_void_p * 2)]     # the union is two pointers wide


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("r1", ctypes.c_uint16), ("r2", ctypes.c_uint16), ("r3", ctypes.c_uint16),
                ("v", _VariantValue)]


_PP = ctypes.POINTER(ctypes.c_void_p)


class _UiaCalls:
    """The UI Automation calls a scan and a button press need, made through COM vtables. The platform parts are
    passed in (function type, CoCreateInstance, BSTR text / free): tests drive it with fake vtables."""

    def __init__(self, functype: Any, create: Callable[[], int], bstr_text: Callable[[int], str],
                 bstr_free: Callable[[int], Any]) -> None:
        self._F = functype
        self._create = create
        self._bstr_text = bstr_text
        self._bstr_free = bstr_free
        self._auto: int | None = None

    def _call(self, this: int, slot: int, argtypes: tuple[Any, ...], *args: Any) -> None:
        vtbl = ctypes.cast(this, ctypes.POINTER(_PP)).contents
        hr = self._F(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[slot])(this, *args)
        if hr < 0:
            raise OSError(f"UI Automation call (vtable slot {slot}) failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")

    def _release(self, this: int | None) -> None:
        if this:
            vtbl = ctypes.cast(this, ctypes.POINTER(_PP)).contents
            self._F(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(this)

    def _automation(self) -> int:
        if self._auto is None:
            self._auto = self._create()
        return self._auto

    def _element(self, hwnd: int) -> int:
        out = ctypes.c_void_p()
        self._call(self._automation(), 6, (ctypes.c_void_p, _PP), hwnd, ctypes.byref(out))
        if not out.value:
            raise OSError(f"UI Automation has no element for window {hwnd}")
        return out.value

    def _each(self, el: int, control_type: int, visit: Callable[[int], bool]) -> None:
        """visit(element) for every descendant of el of that control type, until it returns True."""
        v = _VARIANT()
        v.vt = VT_I4
        v.v.lVal = control_type
        cond = ctypes.c_void_p()
        self._call(self._automation(), 23, (ctypes.c_int, _VARIANT, _PP), UIA_ControlTypePropertyId, v,
                   ctypes.byref(cond))
        arr = ctypes.c_void_p()
        try:
            self._call(el, 6, (ctypes.c_int, ctypes.c_void_p, _PP), TreeScope_Descendants, cond.value,
                       ctypes.byref(arr))
        finally:
            self._release(cond.value)
        if not arr.value:
            return
        try:
            n = ctypes.c_int()
            self._call(arr.value, 3, (ctypes.POINTER(ctypes.c_int),), ctypes.byref(n))
            for i in range(min(n.value, UIA_MAX_ELEMENTS)):
                e = ctypes.c_void_p()
                self._call(arr.value, 4, (ctypes.c_int, _PP), i, ctypes.byref(e))
                if not e.value:
                    continue
                try:
                    if visit(e.value):
                        return
                finally:
                    self._release(e.value)
        finally:
            self._release(arr.value)

    def _name(self, e: int) -> str:
        b = ctypes.c_void_p()
        self._call(e, 23, (_PP,), ctypes.byref(b))
        if not b.value:
            return ""
        try:
            return str(self._bstr_text(b.value) or "")
        finally:
            self._bstr_free(b.value)

    def read(self, hwnd: int) -> tuple[list[str], list[str]]:
        """(button names, text names) of the window."""
        el = self._element(hwnd)
        buttons: list[str] = []
        texts: list[str] = []
        try:
            self._each(el, UIA_ButtonControlTypeId, lambda e: bool(buttons.append(self._name(e))))
            self._each(el, UIA_TextControlTypeId, lambda e: bool(texts.append(self._name(e))))
        finally:
            self._release(el)
        return buttons, texts

    def invoke(self, hwnd: int, name: str) -> bool:
        """Invoke the window's button whose name (without '&') is `name`; False when there is none."""
        el = self._element(hwnd)
        done: list[bool] = []

        def visit(e: int) -> bool:
            if _clean(self._name(e)) != name:
                return False
            p = ctypes.c_void_p()
            self._call(e, 14, (ctypes.c_int, ctypes.POINTER(_GUID), _PP), UIA_InvokePatternId,
                       ctypes.byref(_guid(IID_IUIAutomationInvokePattern)), ctypes.byref(p))
            if not p.value:
                return False
            try:
                self._call(p.value, 3, ())
                done.append(True)
            finally:
                self._release(p.value)
            return True
        try:
            self._each(el, UIA_ButtonControlTypeId, visit)
        finally:
            self._release(el)
        return bool(done)


def _win_uia_calls() -> _UiaCalls:  # pragma: no cover - needs Windows
    ole32 = ctypes.WinDLL("ole32")
    oleaut = ctypes.WinDLL("oleaut32")
    ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, ctypes.c_uint32,
                                       ctypes.POINTER(_GUID), _PP]
    ole32.CoCreateInstance.restype = ctypes.c_long
    oleaut.SysStringLen.argtypes = [ctypes.c_void_p]
    oleaut.SysStringLen.restype = ctypes.c_uint
    oleaut.SysFreeString.argtypes = [ctypes.c_void_p]
    oleaut.SysFreeString.restype = None

    def create() -> int:
        p = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(ctypes.byref(_guid(CLSID_CUIAutomation)), None, 1,       # CLSCTX_INPROC_SERVER
                                    ctypes.byref(_guid(IID_IUIAutomation)), ctypes.byref(p))
        if hr < 0 or not p.value:
            raise OSError(f"CoCreateInstance(CUIAutomation) failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        return p.value
    return _UiaCalls(ctypes.WINFUNCTYPE, create, lambda b: ctypes.wstring_at(b, oleaut.SysStringLen(b)),
                     oleaut.SysFreeString)


class _UiaWorker:
    """One daemon thread (COM multithreaded apartment) makes every UI Automation call. A scan waits at most
    UIA_WAIT_S for a window and keeps a late answer for the next scan: a window that does not answer never stalls the
    watchdog, a call's watch or the event loop. Buttons once read are kept for that window; a window read without any
    (still building) is read again a second later."""

    def __init__(self, make: Callable[[], _UiaCalls]) -> None:
        self._make = make
        self._q: "queue.Queue[tuple[Callable[[_UiaCalls], Any], cf.Future]]" = queue.Queue()
        self._lock = threading.RLock()
        self._reads: dict[int, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None

    def _run(self) -> None:  # the worker thread
        try:
            ctypes.WinDLL("ole32").CoInitializeEx(None, 0)          # COINIT_MULTITHREADED
        except Exception:  # noqa: BLE001 — not Windows (tests), or already initialised
            pass
        calls: _UiaCalls | None = None
        while True:
            fn, fut = self._q.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                if calls is None:
                    calls = self._make()
                fut.set_result(fn(calls))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)

    def submit(self, fn: Callable[[_UiaCalls], Any]) -> cf.Future:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="xae-uia", daemon=True)
                self._thread.start()
        fut: cf.Future = cf.Future()
        self._q.put((fn, fut))
        return fut

    def read(self, hwnd: int, wait_s: float) -> tuple[list[str], list[str]] | None:
        with self._lock:
            ent = self._reads.get(hwnd)
            if ent is None or (ent["fut"].done() and not ent["keep"] and time.monotonic() - ent["t"] >= 1.0):
                ent = {"fut": self.submit(lambda c: c.read(hwnd)), "t": time.monotonic(), "keep": False}
                self._reads[hwnd] = ent
        try:
            res = ent["fut"].result(timeout=wait_s)
        except Exception:  # noqa: BLE001 — no answer in time, or UI Automation failed: buttons unknown
            return None
        ent["keep"] = bool(res and res[0])
        return res

    def invoke(self, hwnd: int, name: str, wait_s: float) -> bool:
        try:
            return bool(self.submit(lambda c: c.invoke(hwnd, name)).result(timeout=wait_s))
        except Exception:  # noqa: BLE001
            return False

    def forget(self, alive: set[int]) -> None:
        with self._lock:
            for h in [h for h in self._reads if h not in alive]:
                del self._reads[h]


_UIA: _UiaWorker | None = None
_UIA_LOCK = threading.Lock()


def _uia_worker() -> _UiaWorker:
    global _UIA
    with _UIA_LOCK:
        if _UIA is None:
            _UIA = _UiaWorker(_win_uia_calls)
        return _UIA


def _uia_read(hwnd: int) -> tuple[list[str], list[str]] | None:
    """(button names, texts) of a window through UI Automation; None when they cannot be read (in time)."""
    if not IS_WIN:
        return None
    return _uia_worker().read(hwnd, UIA_WAIT_S)


def _uia_invoke(hwnd: int, name: str) -> bool:
    """Press the window's button `name` through UI Automation (Invoke)."""
    if not IS_WIN:
        return False
    return _uia_worker().invoke(hwnd, name, UIA_INVOKE_WAIT_S)


def _uia_forget(alive: set[int]) -> None:
    if _UIA is not None:
        _UIA.forget(alive)
