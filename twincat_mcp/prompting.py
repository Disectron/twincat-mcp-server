"""Asking the human during a tool call, on every MCP protocol revision the SDK (mcp 2.x) serves.

* 2026-07-28 and later: the server cannot call the client (ctx.elicit raises NoBackChannelError — N89). The tool
  RETURNS the question as an InputRequiredResult; the client shows it, then calls the same tool again with the
  answer in input_responses and the request_state echoed. The SDK seals request_state (bound to this tool, these
  arguments and a 600 s window), so what comes back is what this server minted.
* 2025-11-25 and earlier: elicitation/create is sent during the call (ctx.elicit).

Either way the client must have declared the elicitation capability (form mode); without it no prompt is possible
and the caller is told exactly that — it is the client's declaration, not something this server failed to send
(N90).

A tool that asks runs its body again on the answering round, up to the question: the question must be the same text
on every round (it is derived from the action, its target and the machine, never from a clock), and it is matched by
its digest — an answer to another question is never taken for this one. Several questions in one call are asked one
per round; answers already given ride the sealed request_state.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from mcp.server.elicitation import (AcceptedElicitation, CancelledElicitation, DeclinedElicitation,
                                    render_elicitation_schema)
from mcp_types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult
from mcp_types.version import is_version_at_least

INPUT_REQUIRED_VERSION = "2026-07-28"      # the first revision without server-to-client requests
_STATE_V = 1
_KEY_PREFIX = "tcmcp_ask_"


class AskUser(BaseException):
    """Raised inside a tool body: return this InputRequiredResult as the tool's result for this round. A
    BaseException so that no ``except Exception`` on the way up turns the question into an error."""

    def __init__(self, result: InputRequiredResult) -> None:
        super().__init__("input required")
        self.result = result


class NoPrompt(Exception):
    """No prompt can be shown in this session; the message says why."""


def _client_declares_form(ctx: Any) -> bool:
    try:
        caps = ctx.client_capabilities
    except Exception:  # noqa: BLE001 — no request context
        return False
    el = getattr(caps, "elicitation", None) if caps is not None else None
    # a bare "elicitation: {}" (before modes existed) means form; url-only does not (the SDK's own rule)
    return el is not None and (getattr(el, "form", None) is not None or getattr(el, "url", None) is None)


def route(ctx: Any) -> tuple[str | None, str]:
    """('input_required' | 'server_request' | None, why) for asking the user in this request."""
    proto = getattr(ctx, "protocol_version", None) if ctx is not None else None
    if proto is None:
        return None, "no MCP request context (the call did not come through a client session)"
    declared = _client_declares_form(ctx)
    if is_version_at_least(proto, INPUT_REQUIRED_VERSION):
        if not declared:
            return None, (f"MCP {proto}: the client did not declare the elicitation capability (form), so it cannot "
                          "show a prompt — a client setting or limitation, not a server failure")
        return "input_required", (f"MCP {proto}: the question is returned to the client (input_required); it asks the "
                                  "user and calls the tool again with the answer")
    if not declared:
        return None, (f"MCP {proto}: the client did not declare the elicitation capability, so it cannot show a prompt "
                      "— a client setting or limitation, not a server failure")
    session = getattr(ctx, "session", None)
    if session is not None and getattr(session, "can_send_request", True) is False:
        return None, f"MCP {proto}: this transport has no channel for a server request (stateless / JSON-response mode)"
    return "server_request", f"MCP {proto}: elicitation/create is sent to the client during the call"


def _digest(params: ElicitRequestFormParams) -> str:
    raw = json.dumps(params.model_dump(mode="json", by_alias=True, exclude_none=True), sort_keys=True,
                     separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()[:18]).decode().rstrip("=")


_CALL: contextvars.ContextVar[tuple[Any, dict[str, Any]] | None] = contextvars.ContextVar("tcmcp_ask", default=None)


def _state(ctx: Any) -> dict[str, Any]:
    """This call's question record: answers given in earlier rounds (sealed request_state) — once per request."""
    cur = _CALL.get()
    if cur is not None and cur[0] is ctx:
        return cur[1]
    st: dict[str, Any] = {"v": _STATE_V, "answers": {}, "asked": {}}
    raw = getattr(ctx, "request_state", None)
    if raw:
        try:
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("v") == _STATE_V:
                st["answers"] = dict(d.get("answers") or {})
                st["asked"] = dict(d.get("asked") or {})
        except ValueError:
            pass
    _CALL.set((ctx, st))
    return st


def _outcome(action: str, content: Any, schema: type[BaseModel]) -> Any:
    if action == "accept":
        return AcceptedElicitation(data=schema.model_validate(content))
    if action == "decline":
        return DeclinedElicitation()
    return CancelledElicitation()


async def ask(ctx: Any, message: str, schema: type[BaseModel]) -> tuple[Any, float | None]:
    """Ask the user; -> (AcceptedElicitation | DeclinedElicitation | CancelledElicitation, seconds the prompt was
    open or None). Raises AskUser (return its result: the question goes to the client this round) or NoPrompt."""
    how, why = route(ctx)
    if how is None:
        raise NoPrompt(why)
    if how == "server_request":
        t0 = time.monotonic()
        try:
            res = await ctx.elicit(message, schema)
        except Exception as exc:  # noqa: BLE001 — the client refused or failed the request
            raise NoPrompt(f"{why}, but the request failed: {type(exc).__name__}: {exc}") from exc
        return res, round(time.monotonic() - t0, 2)
    params = ElicitRequestFormParams(message=message, requested_schema=render_elicitation_schema(schema))
    q = _digest(params)
    key = _KEY_PREFIX + q[:16]
    st = _state(ctx)
    rec = st["answers"].get(key)
    if isinstance(rec, dict) and rec.get("q") == q:
        return _outcome(rec["action"], rec.get("content"), schema), rec.get("open_s")
    asked = st["asked"].get(key)
    ans = (getattr(ctx, "input_responses", None) or {}).get(key)
    if ans is not None and isinstance(asked, dict) and asked.get("q") == q:
        if not isinstance(ans, ElicitResult):
            raise NoPrompt(f"the client answered the question with a {type(ans).__name__}, not an elicitation result")
        open_s = round(max(0.0, time.time() - float(asked.get("at") or time.time())), 2)
        content = ans.content if ans.action == "accept" else None
        try:
            out = _outcome(ans.action, content, schema)
        except ValidationError as exc:
            raise NoPrompt("the client's answer does not match the question's form") from exc
        st["answers"][key] = {"q": q, "action": ans.action, "content": content, "open_s": open_s}
        return out, open_s
    # not answered yet: this round returns the question (with every answer given so far)
    st["asked"] = {key: {"q": q, "at": time.time()}}
    raise AskUser(InputRequiredResult(input_requests={key: ElicitRequest(params=params)},
                                      request_state=json.dumps(st, separators=(",", ":"))))


def report(ctx: Any, allowed: bool) -> dict[str, Any]:
    """tc_doctor: can a confirmation prompt reach the user in this session — from the negotiated protocol and the
    client's declared capability (what decides it), and the server setting."""
    how, why = route(ctx)
    out: dict[str, Any] = {"protocol": getattr(ctx, "protocol_version", None) if ctx is not None else None,
                           "client_declared_elicitation": _client_declares_form(ctx) if ctx is not None else False,
                           "route": how, "allowed_by_config": bool(allowed)}
    if not allowed:
        out["works"] = False
        out["why"] = "safety.allow_elicitation = false: confirmations go through tc_request_confirmation tokens"
    else:
        out["works"] = how is not None
        out["why"] = why
    return out
