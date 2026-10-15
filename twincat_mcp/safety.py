"""Safety guard + audit log.

Operation classes
-----------------
READ         symbol/state/file reads, tree ProduceXml, project parsing
EDIT         offline project edits (POU text, ConsumeXml, PLCopen import,
             boot-project flags). Undoable via git, no effect on the running
             machine until a build+activate happens.
WRITE        ADS write to a runtime symbol on the target.
CONTROL      runtime state changes that do not re-deploy: PLC run/stop/reset,
             PLC login/logout/start/stop, local-route changes.
DESTRUCTIVE  anything that drops the machine or the IPC: Activate
             Configuration, Restart TwinCAT, switch to Config mode, download,
             delete tree items / files on the target, start a process on the
             IPC, reboot the IPC, TcUnit run (it activates).

Mode matrix
-----------
             readonly   guarded                 full
READ         ok         ok                      ok
EDIT         deny       ok                      ok
WRITE        deny       allowlist && !denylist  !denylist
CONTROL      deny       confirm token           ok
DESTRUCTIVE  deny       confirm token           confirm token *

* in ``full`` the token is still required unless ``safety.confirm_destructive``
  is false (``TCMCP_CONFIRM=0``). That is the only switch that makes a single
  tool call capable of taking a machine down.

Confirmation tokens are a two-step guard against a *single* mis-aimed call,
not human approval: the agent requests a token for a named action/target and
must echo it back within ``confirm_ttl_s``. Human approval belongs to the MCP
client's own tool-permission prompt.

Runtime escalation (``tc_set_mode``) is capped at ``safety.max_mode`` from the
configuration. The agent cannot raise its own ceiling.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .config import SafetyConfig, MODES, MODE_RANK


class OpClass(str, Enum):
    READ = "read"
    EDIT = "edit"
    WRITE = "write"
    CONTROL = "control"
    DESTRUCTIVE = "destructive"


class SafetyError(PermissionError):
    """Raised when the guard refuses an operation. Message says how to unblock."""


@dataclass
class _Token:
    token: str
    action: str
    target: str
    scope: str
    expires: float
    used: bool = False


class Guard:
    def __init__(self, cfg: SafetyConfig):
        self.cfg = cfg
        self.mode = cfg.mode
        self.max_mode = cfg.max_mode or cfg.mode
        self._tokens: dict[str, _Token] = {}
        self._lock = threading.Lock()
        self._audit_path = Path(cfg.audit_log) if cfg.audit_log else None
        if self._audit_path:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ mode
    def set_mode(self, mode: str) -> str:
        mode = mode.lower()
        if mode not in MODES:
            raise SafetyError(f"unknown mode {mode!r}; valid: {MODES}")
        if MODE_RANK[mode] > MODE_RANK[self.max_mode]:
            raise SafetyError(
                f"mode {mode!r} exceeds configured ceiling {self.max_mode!r}. "
                "Raise TCMCP_MAX_MODE (or safety.max_mode in the TOML) and restart the server."
            )
        old, self.mode = self.mode, mode
        self.audit("tc_set_mode", {"from": old, "to": mode}, OpClass.CONTROL, "ok")
        return old

    def status(self) -> dict[str, Any]:
        with self._lock:
            live = [t for t in self._tokens.values() if not t.used and t.expires > time.time()]
        return {
            "mode": self.mode,
            "max_mode": self.max_mode,
            "confirm_destructive": self.cfg.confirm_destructive,
            "confirm_ttl_s": self.cfg.confirm_ttl_s,
            "write_allowlist": self.cfg.write_allowlist,
            "write_denylist": self.cfg.write_denylist,
            "pending_tokens": [{"action": t.action, "target": t.target, "scope": t.scope,
                                "expires_in_s": round(t.expires - time.time(), 1)} for t in live],
            "audit_log": str(self._audit_path) if self._audit_path else None,
        }

    # ------------------------------------------------------------ tokens
    def request_token(self, action: str, target: str, scope: str = "") -> dict[str, Any]:
        """``scope`` names the machine the token is valid for (e.g. '5.1.2.3.1.1:851');
        it is captured at issue time so a token cannot be carried to another target."""
        nonce = secrets.token_hex(4)
        raw = f"{action}|{target}|{scope}|{nonce}".encode()
        token = hashlib.sha256(raw).hexdigest()[:12].upper()
        exp = time.time() + self.cfg.confirm_ttl_s
        with self._lock:
            # purge expired
            for k in [k for k, t in self._tokens.items() if t.expires < time.time()]:
                self._tokens.pop(k, None)
            self._tokens[token] = _Token(token, action, target, scope, exp)
        self.audit("tc_request_confirmation", {"action": action, "target": target, "scope": scope},
                   OpClass.READ, "issued")
        return {
            "confirm_token": token,
            "action": action,
            "target": target,
            "scope": scope,
            "expires_in_s": self.cfg.confirm_ttl_s,
            "note": f"Pass confirm_token={token!r} to the tool performing {action!r} on {target!r} "
                    f"(machine {scope or 'n/a'}). Single use. Re-read the target before you use it.",
        }

    def _consume_token(self, token: str | None, action: str, target: str, scope: str = "") -> None:
        if not token:
            raise SafetyError(
                f"{action} on {target!r} (machine {scope or 'n/a'}) needs a confirm_token. Call "
                f"tc_request_confirmation(action={action!r}, target={target!r}) first."
            )
        with self._lock:
            t = self._tokens.get(token.strip().upper())
            if t is None:
                raise SafetyError("confirm_token unknown (typo, or server restarted). Request a new one.")
            if t.used:
                raise SafetyError("confirm_token already used. Request a new one.")
            if t.expires < time.time():
                raise SafetyError("confirm_token expired. Request a new one.")
            if t.action != action or t.target != target:
                raise SafetyError(
                    f"confirm_token was issued for {t.action!r} on {t.target!r}, "
                    f"not {action!r} on {target!r}."
                )
            if t.scope != scope:
                raise SafetyError(
                    f"confirm_token was issued while connected to {t.scope or 'n/a'}, but the server now "
                    f"targets {scope or 'n/a'}. Request a new one for this machine."
                )
            t.used = True

    # ------------------------------------------------------------ checks
    def _match_any(self, name: str, patterns: list[str]) -> str | None:
        low = name.lower()
        for p in patterns:
            if fnmatch.fnmatchcase(low, p.lower()):
                return p
        return None

    def check(self, op: OpClass, *, action: str, target: str = "",
              confirm_token: str | None = None, scope: str = "") -> None:
        """Raise SafetyError if ``op`` is not permitted right now."""
        mode = self.mode
        if op is OpClass.READ:
            return
        if mode == "readonly":
            raise SafetyError(
                f"server is in readonly mode; {action} ({op.value}) refused. "
                "tc_set_mode('guarded'|'full') if the ceiling allows, or restart with TCMCP_MODE."
            )
        if op is OpClass.EDIT:
            return
        if op is OpClass.WRITE:
            if deny := self._match_any(target, self.cfg.write_denylist):
                raise SafetyError(f"write to {target!r} refused: matches denylist pattern {deny!r}")
            if mode == "guarded":
                if not self.cfg.write_allowlist:
                    raise SafetyError(
                        "guarded mode: write_allowlist is empty, so no symbol is writable. "
                        "Set TCMCP_WRITE_ALLOW='MAIN.fb*;GVL_Test.*' (fnmatch, ';'-separated) "
                        "or tc_set_mode('full') if max_mode permits."
                    )
                if not self._match_any(target, self.cfg.write_allowlist):
                    raise SafetyError(
                        f"guarded mode: {target!r} is not in write_allowlist "
                        f"{self.cfg.write_allowlist}."
                    )
            return
        if op is OpClass.CONTROL:
            if mode == "guarded":
                self._consume_token(confirm_token, action, target, scope)
            return
        if op is OpClass.DESTRUCTIVE:
            if mode == "guarded" or self.cfg.confirm_destructive:
                self._consume_token(confirm_token, action, target, scope)
            return
        raise SafetyError(f"unknown op class {op!r}")  # pragma: no cover

    # ------------------------------------------------------------ audit
    def audit(self, tool: str, args: dict[str, Any], op: OpClass, outcome: str,
              detail: Any = None) -> None:
        if op is OpClass.READ and not self.cfg.audit_reads and tool not in (
                "tc_request_confirmation",):
            return
        if not self._audit_path:
            return
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "pid": os.getpid(),
            "tool": tool,
            "op": op.value,
            "mode": self.mode,
            "args": _shrink(args),
            "outcome": outcome,
        }
        if detail is not None:
            rec["detail"] = _shrink(detail)
        try:
            with open(self._audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        except OSError:
            pass  # audit must never break the tool

    def tail_audit(self, n: int = 50) -> list[dict[str, Any]]:
        if not self._audit_path or not self._audit_path.is_file():
            return []
        try:
            lines = self._audit_path.read_text(encoding="utf-8").splitlines()[-n:]
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
        return out


def _shrink(obj: Any, limit: int = 2000) -> Any:
    s = json.dumps(obj, default=str)
    if len(s) <= limit:
        return obj
    return {"_truncated": s[:limit] + "…"}
