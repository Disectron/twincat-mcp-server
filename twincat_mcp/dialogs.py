"""XAE modal-dialog watchdog (Windows).

A COM call into the XAE shell blocks for as long as a modal dialog is open
("Save changes?", "Activate configuration?", "Login: online change?"…).
Without eyes on the screen the agent only sees a timeout. This module

* enumerates top-level dialog windows (class ``#32770``) of the XAE process,
  with their title, static text and buttons;
* clicks a button on request (``xae_dialog_resolve``);
* optionally auto-answers dialogs that match configured rules
  (``xae.dialog_rules``) from a background thread, so an allowlisted prompt
  never stalls automation;
* never auto-answers prompts that change the machine (activate, run mode,
  restart, download, login/online change, delete, overwrite, safety) unless a
  rule says ``force: true`` — those are reported instead.

Everything is best effort and Windows-only (pywin32). On other platforms the
functions return empty results.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

IS_WIN = sys.platform == "win32"
if IS_WIN:  # pragma: no cover
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32process  # type: ignore

DIALOG_CLASS = "#32770"
NEVER_AUTO = ("activate", "run mode", "restart", "download", "login", "online change", "logout",
              "delete", "remove", "overwrite", "safety", "twinsafe", "reset", "boot project", "license")


@dataclass
class Dialog:
    hwnd: int
    pid: int
    title: str
    text: str
    buttons: list[tuple[int, str]] = field(default_factory=list)   # (hwnd, caption)

    def to_dict(self) -> dict[str, Any]:
        return {"hwnd": self.hwnd, "title": self.title, "text": self.text[:1000],
                "buttons": [c for _, c in self.buttons], "machine_affecting": self.machine_affecting()}

    def machine_affecting(self) -> bool:
        blob = f"{self.title} {self.text}".lower()
        return any(k in blob for k in NEVER_AUTO)


def _clean(caption: str) -> str:
    return caption.replace("&", "").strip()


def list_dialogs(pid: int | None) -> list[Dialog]:  # pragma: no cover - needs Windows
    if not IS_WIN:
        return []
    found: list[Dialog] = []

    def on_top(hwnd: int, _: Any) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd) or win32gui.GetClassName(hwnd) != DIALOG_CLASS:
                return True
            _tid, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if pid is not None and wpid != pid:
                return True
            title = win32gui.GetWindowText(hwnd)
            texts: list[str] = []
            buttons: list[tuple[int, str]] = []

            def on_child(ch: int, _a: Any) -> bool:
                cls = win32gui.GetClassName(ch)
                txt = win32gui.GetWindowText(ch)
                if cls == "Button" and txt:
                    buttons.append((ch, _clean(txt)))
                elif cls in ("Static", "RichEdit20W", "Edit", "SysLink") and txt:
                    texts.append(txt)
                return True

            win32gui.EnumChildWindows(hwnd, on_child, None)
            found.append(Dialog(hwnd, wpid, title, "\n".join(texts), buttons))
        except Exception:  # noqa: BLE001
            pass
        return True

    win32gui.EnumWindows(on_top, None)
    return found


def click(dialog: Dialog, button: str) -> bool:  # pragma: no cover - needs Windows
    if not IS_WIN:
        return False
    want = button.strip().lower()
    for hwnd, cap in dialog.buttons:
        if cap.lower() == want or cap.lower().startswith(want):
            try:
                win32gui.PostMessage(hwnd, win32con.BM_CLICK, 0, 0)
                return True
            except Exception:  # noqa: BLE001
                return False
    return False


def rule_matches(rule: dict[str, Any], d: Dialog) -> bool:
    t = (rule.get("title_contains") or "").lower()
    x = (rule.get("text_contains") or "").lower()
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
        self.log.append({"ts": time.strftime("%H:%M:%S"), "event": kind, "title": d.title, "text": d.text[:200],
                         "buttons": [c for _, c in d.buttons], "button": button, "ok": ok})
        if len(self.log) > 200:
            del self.log[:50]

    def resolve(self, button: str, hwnd: int | None = None, remember: bool = False) -> dict[str, Any]:
        ds = list_dialogs(self.pid)
        if hwnd is not None:
            ds = [d for d in ds if d.hwnd == hwnd]
        if not ds:
            return {"resolved": False, "reason": "no dialog open" if hwnd is None else f"no dialog with hwnd {hwnd}"}
        d = ds[0]
        ok = click(d, button)
        self._record("manual", d, button, ok)
        if ok and remember and not d.machine_affecting():
            self.rules.append({"title_contains": d.title, "button": button})
        return {"resolved": ok, "dialog": d.to_dict(), "button": button,
                "remembered": bool(ok and remember and not d.machine_affecting()),
                "note": None if ok else f"button {button!r} not found; available: {[c for _, c in d.buttons]}"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            pend = [d.to_dict() for d in self.pending]
        return {"running": bool(self._thread and self._thread.is_alive()), "pid": self.pid, "rules": self.rules,
                "pending": pend, "recent": self.log[-20:]}
