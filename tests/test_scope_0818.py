"""0.8.17 retest (Ryan, 2026-10-22, bugs.md §37): N189 — two recordings with Scope at once, one Scope: B's part
stopped Scope at B's end and saved A's capture, A saved the same capture again, both signed. N190 — the disconnect
question and its answer were not audited. N191 — a Scope capture that ran past (or fell short of) its recording was
saved without a word."""
from __future__ import annotations

import json
import time
from pathlib import Path

import anyio
import pytest

from tests.fake_ads import install
from tests.test_scope_0817 import _answering, _link, _until
from tests.test_scope_xae import fake_xae  # noqa: F401  (fixture)
from tests.test_server_e2e import client_for, mk_cfg, payload, text
from twincat_mcp.server import build_server

REC = {"symbols": ["MAIN.nCycle"], "mode": "change"}


# ---- N189 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_n189_two_recordings_with_scope_started_together_one_gets_scope(monkeypatch, project_dir, fake_xae):
    """The retest's batch: A (10 s) and B (3 s) with also_scope in one go. One owns Scope; the other is refused before
    anything starts — never two parts of the one Scope."""
    install(monkeypatch)
    orig = fake_xae.StartRecord

    def slow_start():
        time.sleep(0.5)                        # StartRecord takes a moment: the second call arrives meanwhile
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", slow_start)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    outs: list = []
    async with client_for(srv) as s:
        async def one(d):
            outs.append(await s.call_tool("rec_start", {**REC, "duration_s": d, "also_scope": True}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(one, 10)
            tg.start_soon(one, 3)
        ok = [payload(r) for r in outs if not r.is_error]
        bad = [text(r) for r in outs if r.is_error]
        assert len(ok) == 1 and len(bad) == 1, (ok, bad)
        assert ("being started by another call (rec_start)" in bad[0] or "Scope belongs to recording" in bad[0]) \
            and "This recording was not started" in bad[0] and "also_scope=false" in bad[0], bad[0]
        assert list(app.rec.recs) == [ok[0]["id"]] and list(app.scope_parts) == [ok[0]["id"]]
        await s.call_tool("rec_stop", {"id": ok[0]["id"]})
    assert fake_xae.calls.count("start") == 1 and fake_xae.calls.count("stop") == 1, fake_xae.calls


@pytest.mark.anyio
async def test_n189_scope_has_one_owner_while_a_recording_records_with_it(monkeypatch, project_dir, fake_xae):
    """While A's Scope part has Scope: another rec_start with Scope is refused (without Scope it records; with the
    'ask' policy Scope is not offered), and every scope_record action that would start, stop, save or discard Scope
    is refused — A's rec_stop stops and saves it as before."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        a = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
        assert r.is_error and f"Scope belongs to recording {a} (its Scope part: recording)" in text(r), text(r)
        assert list(app.rec.recs) == [a]                                  # nothing of B was started
        b = payload(await s.call_tool("rec_start", {**REC, "also_scope": False}))
        assert "scope" not in b
        c = payload(await s.call_tool("rec_start", REC))                 # policy 'ask': not offered, said why
        assert "scope" not in c and f"Scope belongs to recording {a}" in c["scope_note"], c
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "scope_record", "target": "disconnect:"}))
        for act, extra in (("start", {}), ("stop", {}), ("save", {}), ("new", {"confirm_token": tok["confirm_token"]}),
                           ("disconnect", {"confirm_token": tok["confirm_token"]})):
            r = await s.call_tool("scope_record", {"action": act, **extra})
            assert r.is_error and f"Scope belongs to recording {a}" in text(r) and "nothing was done" in text(r), \
                (act, text(r))
        assert fake_xae.calls == ["start"] and fake_xae.recording, fake_xae.calls
        out = payload(await s.call_tool("rec_stop", {"id": a}))
        for rid in (b["id"], c["id"]):
            await s.call_tool("rec_stop", {"id": rid})
    assert out["scope"]["link"]["files"] and fake_xae.calls[:3] == ["start", "stop", "save"], fake_xae.calls


@pytest.mark.anyio
async def test_n189_a_scope_recording_of_its_own_owns_scope_until_it_is_stopped(monkeypatch, project_dir, fake_xae):
    """scope_record start: a recording with Scope is refused (0.8.17 gave it a part of the user's own run, which its
    end then stopped and saved). After scope_record stop Scope holds that data: the next recording with Scope asks to
    disconnect it first, as after a recording."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        payload(await s.call_tool("scope_record", {"action": "start"}))
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
        assert r.is_error and "Scope records on its own (scope_record start" in text(r) and \
            "scope_record(action='stop') stops it first" in text(r), text(r)
        r = await s.call_tool("scope_record", {"action": "start"})
        assert r.is_error and "would start Scope a second time" in text(r), text(r)
        assert app.rec.recs == {} and fake_xae.calls == ["start"]
        payload(await s.call_tool("scope_record", {"action": "stop"}))
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
        assert r.is_error and "Scope still holds a Scope recording of its own (scope_record)" in text(r) and \
            "NOT saved" in text(r) and "Nothing was started" in text(r), text(r)
        payload(await s.call_tool("scope_record", {"action": "save"}))           # saved: disconnecting loses nothing
        r = payload(await s.call_tool("rec_start", {**REC, "also_scope": True, "scope_disconnect": True}))
        assert r["scope"]["started"] and fake_xae.calls[-2:] == ["disconnect", "start"], fake_xae.calls
        await s.call_tool("rec_stop", {"id": r["id"]})


@pytest.mark.anyio
async def test_n189_a_late_answer_to_the_scope_question_does_nothing_when_scope_moved_on(monkeypatch, project_dir,
                                                                                        fake_xae):
    """The choice after a recording can come minutes after the question: Scope may meanwhile have been disconnected
    and record for another owner — 'new' then disconnected that owner's recording."""
    install(monkeypatch)
    from mcp import types as _types
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app

    async def elicit(context, params):
        if "next" in (params.requested_schema or {}).get("properties", {}):
            app.scope_left = None                  # meanwhile: disconnected in another session / by the user's close
            return _types.ElicitResult(action="accept", content={"next": "new"})
        return _types.ElicitResult(action="decline")
    async with client_for(srv, elicitation_callback=elicit) as s:
        r = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    nx = out["scope_next"]
    assert nx["choice"] == "new" and "Scope no longer holds the data of" in nx["error"], nx
    assert "disconnect" not in fake_xae.calls and fake_xae.calls.count("start") == 1, fake_xae.calls


# ---- N190 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_n190_the_disconnect_question_and_the_choice_are_audited(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded")
    srv = build_server(cfg)
    elicit, asked = _answering("pause", disconnect=True)
    async with client_for(srv, elicitation_callback=elicit) as s:
        r1 = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))
        payload(await s.call_tool("rec_stop", {"id": r1["id"]}))
        r2 = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))
        await s.call_tool("rec_stop", {"id": r2["id"]})
    rows = [json.loads(ln) for ln in Path(cfg.safety.audit_log).read_text(encoding="utf-8").splitlines() if ln.strip()]
    tools = [(x.get("tool"), x.get("args")) for x in rows]
    assert ("scope_next", {"id": r1["id"], "answer": "pause"}) in tools, tools
    asked_rows = [a for t, a in tools if t == "scope_disconnect_asked"]
    assert asked_rows and asked_rows[0]["held"] == r1["id"] and asked_rows[0]["answer"] is True \
        and asked_rows[0]["via"] == "prompt", asked_rows


# ---- N191 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("case", ["past", "short", "match"])
async def test_n191_a_scope_capture_not_matching_its_recording_is_said(monkeypatch, project_dir, fake_xae, case):
    """The retest: Scope ran 28 s past its recording (StopRecord refused while XAE was blocked); N189: a capture that
    covered 3.5 s of 10 s. Both are said in the result and in the signed link; a matching capture says nothing."""
    install(monkeypatch)
    from twincat_mcp import recorder as R
    from twincat_mcp import server as srvmod
    t0 = int((time.time() + 11_644_473_600) * 10_000_000)
    monkeypatch.setattr(R.Recorder, "t0_filetime", lambda self, rid: t0)
    orig_footer = srvmod.K_footer
    monkeypatch.setattr(srvmod, "K_footer", lambda p: {**(orig_footer(p) or {}), "window_end_ms": 10_000.0})
    end_s = {"past": 38.0, "short": 3.5, "match": 10.8}[case]

    def export(path):
        fake_xae.calls.append("csv")
        Path(path).write_text(f"Name\tx\nStarttime of export\t{t0 + 5_000_000}\nEndtime of export\t"
                              f"{t0 + int(end_s * 1e7)}\n\nName\tGVL.x\n1\t2\n", encoding="utf-8")
        return 0
    monkeypatch.setattr(fake_xae, "ExportCSV", export)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    sc = out["scope"]
    if case == "match":
        assert "scope_span" not in sc and "span" not in _link(app, r["id"]), sc
        return
    span = sc["scope_span"]
    if case == "past":
        assert span["runs_past_s"] == 28.0 and "ran 28.0 s past its end" in sc["note"], sc
    else:
        assert span["ends_before_s"] == 6.5 and span["covers_s"] == 3.0 and span["recording_s"] == 10.0, span
        assert "covers 3.0 s of the recording's 10.0 s" in sc["note"], sc
    assert _link(app, r["id"])["span"] == span
