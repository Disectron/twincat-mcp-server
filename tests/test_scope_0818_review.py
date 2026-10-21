"""Review 1 of 0.8.18 — the in-depth review (reviewers A and B) of 0.8.17's Scope and recorder code: its probes as
regression tests of the fixes (eaa6b6b, 4877d65). Each test names its probe (A-pN / B-pN) and what 0.8.17 did; the
behaviour asserted is the fixed one. Already in test_scope_0818.py (not repeated here): scope_record start / stop / save /
new / disconnect refused while a recording's part owns Scope (A-p4), and the disconnect question after scope_record stop
(B-p4, second half). A-p8 and A-p9 stay LOW (tracker)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import anyio
import pytest
from mcp import types as _types

from tests.fake_ads import install
from tests.test_scope_0817 import _answering, _until
from tests.test_scope_xae import _R13Busy, fake_xae  # noqa: F401  (fixture)
from tests.test_server_e2e import client_for, mk_cfg, payload, text
from twincat_mcp import xae_backend as X
from twincat_mcp.recorder import is_read_only
from twincat_mcp.server import build_server

REC = {"symbols": ["MAIN.nCycle"], "mode": "change"}
FT = lambda t: int((t + 11_644_473_600) * 10_000_000)  # noqa: E731  (Unix seconds -> FILETIME)
STOPPED = ["start", "stop", "save", "csv"]               # a recording with Scope, stopped: XAE's calls


def _view_log(tmp_path: Path, cfg) -> Path:
    """A Scope View log (TcScopeView.log) that exists — the start check reads it."""
    log = tmp_path / "TE130X" / "TcScopeView.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("(2026-10-21-22:40:00:000)[ INFO ] Scope View: start\n", encoding="utf-8")
    cfg.scope.view_log = str(log)
    return log


def _log_line(log: Path, line: str, level: str = "INFO") -> None:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"(2026-10-21-23:00:00:000)[ {level} ] Scope View: {line}\n")


def _stateful(monkeypatch, fake_xae, log: Path) -> dict:
    """A Scope with TE130x's states (Config -> Record -> Reply; Disconnect -> Config) and its Scope View log lines."""
    st = {"s": "Config"}

    def move(to):
        _log_line(log, f"Scope Project state changes from '{st['s']}' to '{to}'")
        st["s"] = to

    def start():
        if st["s"] != "Config":
            fake_xae.calls.append("start-refused")
            _log_line(log, f"Invalid ScopeState! Config requested, actual- {st['s']}", "ERROR")
            return 1
        fake_xae.calls.append("start")
        move("Record")
        return 1

    def stop():
        fake_xae.calls.append("stop")
        if st["s"] == "Record":
            move("Reply")
        return 0

    def disconnect():
        fake_xae.calls.append("disconnect")
        move("Config")
        return 0

    def save(path):
        if st["s"] != "Reply":
            raise RuntimeError(f"Invalid ScopeState! Reply requested, actual: {st['s']}")
        fake_xae.calls.append("save")
        Path(path).write_bytes(b"SVDX" * 64)
        return 0

    def csv(path):
        fake_xae.calls.append("csv")
        Path(path).write_text("Name\tx\n" + ("0\t1\n" if st["s"] == "Reply" else ""), encoding="utf-8")
        return 0
    for k, f in (("StartRecord", start), ("StopRecord", stop), ("Disconnect", disconnect), ("SaveSVD", save),
                 ("ExportCSV", csv)):
        monkeypatch.setattr(fake_xae, k, f)
    return st


def _next(params) -> bool:
    return "next" in (params.requested_schema or {}).get("properties", {})


# ---- A-p1 / B-p1 -----------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_r1_a1_b1_rec_start_cancelled_during_the_start_check_leaves_scope_with_its_recording(
        monkeypatch, project_dir, fake_xae, tmp_path):
    """review 1 of 0.8.18, A-p1 / B-p1: rec_start cancelled while its start check read the Scope View log (StartRecord had
    run) — 0.8.17 left the part 'starting' with no owner: rec_stop / rec_delete waited forever, Scope recorded on."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    _view_log(tmp_path, cfg)                   # readable; StartRecord writes no line: the check waits its 2 s
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        with anyio.move_on_after(1.0):         # the client gives up on rec_start during the check
            await s.call_tool("rec_start", {**REC, "also_scope": True})
        rid = next(iter(app.rec.recs))
        part = app.scope_parts[rid]
        assert await _until(lambda: part.state == "recording", n=60), part.state     # its owner took it over
        assert part.task is not None and fake_xae.recording and fake_xae.calls == ["start"]
        assert part.run["start_check"].startswith("not checked: rec_start was cancelled"), part.run
        with anyio.move_on_after(5.0) as cs:
            out = payload(await s.call_tool("rec_stop", {"id": rid}))
        assert not cs.cancelled_caught, "rec_stop waited for a Scope part nobody owned"
        assert fake_xae.calls == STOPPED and not fake_xae.recording, fake_xae.calls
        assert out["scope"]["stop"] == {"result_code": 0}, out["scope"]
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": rid}))
        with anyio.move_on_after(5.0) as cs:
            await s.call_tool("rec_delete", {"id": rid, "confirm_token": tok["confirm_token"]})
        assert not cs.cancelled_caught, "rec_delete waited for a Scope part nobody owned"


# ---- A-p2 / A-p2c ----------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("choice", ["close", "new"])
async def test_r1_a2_a_late_answer_after_a_recording_leaves_the_next_recordings_scope_alone(
        monkeypatch, project_dir, fake_xae, tmp_path, choice):
    """review 1 of 0.8.18, A-p2 / A-p2c: while A's 'What should Scope do now?' was open, B started with Scope
    (scope_disconnect=true) — 0.8.17 applied A's close / new to B's live capture: B's save failed (Scope in Config,
    'TwinCAT restart'), or B saved and signed the run A's 'new' had started."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded")
    _stateful(monkeypatch, fake_xae, _view_log(tmp_path, cfg))
    srv = build_server(cfg)
    app = srv._tcmcp_app
    asked, b_started = anyio.Event(), anyio.Event()

    async def elicit(context, params):
        asked.set()
        with anyio.move_on_after(5.0):         # the user reads the question; meanwhile B starts with Scope
            await b_started.wait()
        return _types.ElicitResult(action="accept", content={"next": choice})
    out: dict = {}
    async with client_for(srv, elicitation_callback=elicit) as s2, client_for(srv) as s1:
        a = payload(await s2.call_tool("rec_start", {**REC, "also_scope": True}))["id"]

        async def stop_a():
            out["a"] = payload(await s2.call_tool("rec_stop", {"id": a}))

        async def start_b():
            await asked.wait()
            out["b"] = payload(await s1.call_tool("rec_start", {**REC, "also_scope": True, "scope_disconnect": True}))
            out["calls_b"] = list(fake_xae.calls)
            b_started.set()
        async with anyio.create_task_group() as tg:
            tg.start_soon(stop_a)
            tg.start_soon(start_b)
        b = out["b"]["id"]
        assert out["b"]["scope"]["started"] is True
        assert out["calls_b"] == STOPPED + ["disconnect", "start"], out["calls_b"]
        assert fake_xae.calls == out["calls_b"], fake_xae.calls      # A's answer sent nothing to B's Scope
        nx = out["a"]["scope_next"]
        assert nx["choice"] == choice and f"Scope no longer holds the data of {a}" in nx["error"] \
            and "nothing was done" in nx["error"], nx
        assert app.scope_parts[b].state == "recording"
        sb = payload(await s1.call_tool("rec_stop", {"id": b}))["scope"]
    assert "error" not in sb["save_svd"] and sorted(sb["link"]["files"]) == [f"{b}.scope.csv", f"{b}.svdx"], sb
    assert fake_xae.calls == STOPPED + ["disconnect", "start"] + ["stop", "save", "csv"], fake_xae.calls


# ---- A-p2b -----------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_r1_a2b_no_rec_start_takes_scope_while_another_ones_disconnect_question_is_open(
        monkeypatch, project_dir, fake_xae):
    """review 1 of 0.8.18, A-p2b: B's rec_start asked 'Disconnect it and record with Scope now?'; meanwhile C started
    with scope_disconnect=true and recorded — 0.8.17 then applied B's yes to C's live capture (a second Disconnect)."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app
    asked, c_done = anyio.Event(), anyio.Event()

    async def elicit(context, params):
        if _next(params):
            return _types.ElicitResult(action="accept", content={"next": "pause"})
        asked.set()
        with anyio.move_on_after(5.0):
            await c_done.wait()
        return _types.ElicitResult(action="accept", content={"disconnect": True})
    out: dict = {}
    async with client_for(srv, elicitation_callback=elicit) as s2, client_for(srv) as s1:
        a = payload(await s2.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        payload(await s2.call_tool("rec_stop", {"id": a}))                   # pause: Scope keeps A's data
        n_recs = len(app.rec.recs)

        async def start_b():
            out["b"] = payload(await s2.call_tool("rec_start", {**REC, "also_scope": True}))

        async def start_c():
            await asked.wait()
            out["c"] = await s1.call_tool("rec_start", {**REC, "also_scope": True, "scope_disconnect": True})
            out["recs_c"], out["calls_c"] = len(app.rec.recs), list(fake_xae.calls)
            c_done.set()
        async with anyio.create_task_group() as tg:
            tg.start_soon(start_b)
            tg.start_soon(start_c)
        c = out["c"]
        assert c.is_error and "Scope is being started by another call (rec_start)" in text(c) \
            and "This recording was not started" in text(c), text(c)
        assert out["recs_c"] == n_recs and out["calls_c"] == STOPPED, out
        assert out["b"]["scope"]["started"] is True
        assert fake_xae.calls == STOPPED + ["disconnect", "start"], fake_xae.calls    # B's yes: one Disconnect
        await s2.call_tool("rec_stop", {"id": out["b"]["id"]})


@pytest.mark.anyio
@pytest.mark.parametrize("meanwhile", ["changed", "disconnected"])
async def test_r1_a2b_the_disconnect_answer_applies_to_what_it_was_asked_about(monkeypatch, project_dir, fake_xae,
                                                                               meanwhile):
    """review 1 of 0.8.18, A-p2b (4877d65): 0.8.17 applied the yes to 'Disconnect it and record with Scope now?' to
    whatever Scope held when it came — also when that had changed or was disconnected while the question was open
    (simulated: app.scope_left replaced / cleared from inside the prompt)."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app

    async def elicit(context, params):
        if _next(params):
            return _types.ElicitResult(action="accept", content={"next": "pause"})
        app.scope_left = dict(app.scope_left, rid=None, saved=None) if meanwhile == "changed" else None
        return _types.ElicitResult(action="accept", content={"disconnect": True})
    async with client_for(srv, elicitation_callback=elicit) as s:
        a = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        payload(await s.call_tool("rec_stop", {"id": a}))                    # pause: Scope keeps A's data
        n_recs = len(app.rec.recs)
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
        if meanwhile == "changed":
            assert r.is_error and "What Scope holds changed while the question was open" in text(r) \
                and "Nothing was started" in text(r), text(r)
            assert len(app.rec.recs) == n_recs and fake_xae.calls == STOPPED, fake_xae.calls
            return
        r = payload(r)
        assert r["scope"]["started"] is True
        assert fake_xae.calls == STOPPED + ["start"], fake_xae.calls        # nothing left to discard: no Disconnect
        await s.call_tool("rec_stop", {"id": r["id"]})


# ---- A-p2d / B-p6 ----------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("answers", [("close", "close"), ("new", "new"), ("new", "close")])
async def test_r1_a2d_b6_two_rec_stop_calls_apply_one_answer(monkeypatch, project_dir, fake_xae, answers):
    """review 1 of 0.8.18, A-p2d / B-p6: two rec_stop calls of one recording at once both asked 'What should Scope do
    now?' and 0.8.17 applied both answers (Disconnect twice; the second discarded the run 'new' started). Two questions
    may still be asked (by design): one answer is applied, the other says it was not."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    it = iter(answers)
    n = {"asked": 0}
    both = anyio.Event()

    async def elicit(context, params):
        if not _next(params):
            return _types.ElicitResult(action="decline")
        n["asked"] += 1
        if n["asked"] == 2:
            both.set()
        with anyio.move_on_after(2.0):         # the two questions are open together
            await both.wait()
        return _types.ElicitResult(action="accept", content={"next": next(it)})
    outs: list = []
    async with client_for(srv, elicitation_callback=elicit) as s1, client_for(srv, elicitation_callback=elicit) as s2:
        rid = payload(await s1.call_tool("rec_start", {**REC, "also_scope": True}))["id"]

        async def stop(s):
            outs.append(payload(await s.call_tool("rec_stop", {"id": rid})).get("scope_next"))
        async with anyio.create_task_group() as tg:
            tg.start_soon(stop, s1)
            tg.start_soon(stop, s2)
    assert 1 <= n["asked"] <= 2
    nexts = [o for o in outs if o]
    applied = [o for o in nexts if "error" not in o]
    assert len(applied) == 1, nexts
    for o in nexts:
        if o is not applied[0]:
            assert ("already answered in another call" in o["error"] or f"Scope no longer holds the data of {rid}"
                    in o["error"]) and "nothing was done" in o["error"], o
    new = applied[0]["choice"] == "new"
    assert fake_xae.calls == STOPPED + ["disconnect"] + (["start"] if new else []), fake_xae.calls
    assert fake_xae.recording is new


# ---- A-p3 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_r1_a3_the_start_check_reads_only_the_log_after_its_own_start_record(monkeypatch, project_dir, fake_xae,
                                                                                   tmp_path):
    """review 1 of 0.8.18, A-p3: the Scope View log's mark was taken before StartRecord queued on the XAE worker — the
    'Config -> Record' line of another start meanwhile confirmed a StartRecord Scope refused; 0.8.17 said 'started'."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    log = _view_log(tmp_path, cfg)

    def start():                               # TE130x (N186): refused unless in Config, and logged either way
        if fake_xae.recording:
            fake_xae.calls.append("start-refused")
            _log_line(log, "Invalid ScopeState! Config requested, actual- Record", "ERROR")
            return 1
        fake_xae.calls.append("start")
        fake_xae.recording = True
        _log_line(log, "Scope Project state changes from 'Config' to 'Record'")
        return 1
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    srv = build_server(cfg)
    app = srv._tcmcp_app

    def other_start():
        # an XAE job ahead of rec_start's StartRecord; once rec_start has its part (the 0.8.17 mark taken), Scope is
        # started meanwhile — the user's Start in XAE's Scope View, another caller's StartRecord
        t_end = time.monotonic() + 5.0
        while not app.scope_parts and time.monotonic() < t_end:
            time.sleep(0.02)
        time.sleep(0.1)
        fake_xae.recording = True
        _log_line(log, "Scope Project state changes from 'Config' to 'Record'")
    async with client_for(srv) as s:
        blocker = app.xae_worker.submit(other_start)
        r = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))
        blocker.result(timeout=5)
        sc = r["scope"]
        assert "started" not in sc and sc.get("scope_state") == "Record" \
            and "Scope refused StartRecord" in sc["error"], sc
        assert fake_xae.calls == ["start-refused"]
        await s.call_tool("rec_stop", {"id": r["id"]})
    assert fake_xae.calls == ["start-refused"]                 # the run this recording never started is not stopped


# ---- A-p4 (beyond test_scope_0818) -----------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_r1_a4_scope_record_disconnect_rechecks_the_owner_after_its_confirmation(monkeypatch, project_dir,
                                                                                       fake_xae):
    """review 1 of 0.8.18, A-p4: a recording took Scope while scope_record(disconnect)'s confirmation prompt was open —
    0.8.17 disconnected its live capture once confirmed; the prompt did not say what a Scope disconnect does."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    prompts: list[str] = []
    asked, a_started = anyio.Event(), anyio.Event()

    async def elicit(context, params):
        if "confirm" not in (params.requested_schema or {}).get("properties", {}):
            return _types.ElicitResult(action="decline")
        prompts.append(params.message)
        asked.set()
        with anyio.move_on_after(5.0):         # the user reads the confirmation; meanwhile A starts with Scope
            await a_started.wait()
        return _types.ElicitResult(action="accept", content={"confirm": True})
    out: dict = {}
    async with client_for(srv, elicitation_callback=elicit) as s2, client_for(srv) as s1:
        async def disconnect():
            out["d"] = await s2.call_tool("scope_record", {"action": "disconnect"})

        async def start_a():
            await asked.wait()
            out["a"] = payload(await s1.call_tool("rec_start", {**REC, "also_scope": True}))
            a_started.set()
        async with anyio.create_task_group() as tg:
            tg.start_soon(disconnect)
            tg.start_soon(start_a)
        a, d = out["a"]["id"], out["d"]
        assert d.is_error and f"Scope belongs to recording {a}" in text(d) \
            and "would discard what Scope records for its owner: nothing was done" in text(d), text(d)
        assert fake_xae.calls == ["start"] and fake_xae.recording, fake_xae.calls
        assert "Effect: disconnects the TwinCAT Scope configuration" in prompts[0], prompts
        sa = payload(await s1.call_tool("rec_stop", {"id": a}))["scope"]
    assert sorted(sa["link"]["files"]) == [f"{a}.scope.csv", f"{a}.svdx"] and fake_xae.calls == STOPPED, fake_xae.calls


# ---- A-p5 / B-p4 -----------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_r1_a5_b4_after_new_the_next_recording_with_scope_is_refused(monkeypatch, project_dir, fake_xae):
    """review 1 of 0.8.18, A-p5 / B-p4: after the 'new' choice Scope recorded on its own with no owner — 0.8.17's next
    rec_start(also_scope) asked nothing, sent StartRecord to the recording Scope and at its end stopped the user's
    'new' run and signed it as its own."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app
    elicit, asked = _answering("new", disconnect=True)
    async with client_for(srv, elicitation_callback=elicit) as s:
        a = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        nx = payload(await s.call_tool("rec_stop", {"id": a}))["scope_next"]
        assert nx["choice"] == "new" and nx["started"] is True, nx
        n_recs, n_asked = len(app.rec.recs), len(asked)
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
        assert r.is_error and "Scope records on its own (scope_record new," in text(r) \
            and "This recording was not started" in text(r), text(r)
        assert len(app.rec.recs) == n_recs and len(asked) == n_asked                # nothing started, nothing asked
        assert fake_xae.calls == STOPPED + ["disconnect", "start"] and fake_xae.recording, fake_xae.calls
        payload(await s.call_tool("scope_record", {"action": "stop"}))             # its owner stops the 'new' run
    assert fake_xae.calls[-1] == "stop" and not fake_xae.recording, fake_xae.calls


# ---- A-p6 / B-p5 -----------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("case", ["ahead_old_capture", "behind_poll_unconfirmed", "behind_poll_confirmed"])
async def test_r1_a6_b5_the_stale_capture_check_compares_one_clock_at_a_time(monkeypatch, project_dir, fake_xae,
                                                                             tmp_path, case):
    """review 1 of 0.8.18, A-p6 / B-p5: target clock 10 min ahead — 0.8.17's PC-clock test passed a 5-minute-old capture
    (linked, sealed). Poll recording, target clock 60 s behind — 0.8.17 called its own capture 'an earlier capture'; by
    design it stays unlinked ('cannot be told') unless the Scope View log confirmed the start, then it is linked."""
    install(monkeypatch)
    from twincat_mcp import recorder as R
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    offset = 600.0 if case == "ahead_old_capture" else -60.0           # the target's clock minus this PC's, in s
    if case == "behind_poll_confirmed":
        log = _view_log(tmp_path, cfg)
        orig = fake_xae.StartRecord

        def start():
            _log_line(log, "Scope Project state changes from 'Config' to 'Record'")
            return orig()
        monkeypatch.setattr(fake_xae, "StartRecord", start)

    def export(path):
        fake_xae.calls.append("csv")
        now = time.time() + offset
        end = FT(now - 300.0) if case == "ahead_old_capture" else FT(now)   # 5 min old / this recording's data
        Path(path).write_text(f"Name\tx\t\nFile\t{path}\t\nStarttime of export\t{end - 40_000_000}\t\n"
                              f"Endtime of export\t{end}\t\n\n\nName\tGVL.x\n1\t2\n", encoding="utf-8")
        return 0
    monkeypatch.setattr(fake_xae, "ExportCSV", export)
    if case == "ahead_old_capture":
        t0 = FT(time.time() + offset)                                   # the first sample, the target's clock
        monkeypatch.setattr(R.Recorder, "t0_filetime", lambda self, rid: t0)
    srv = build_server(cfg)
    app = srv._tcmcp_app
    mode = "change" if case == "ahead_old_capture" else "poll"
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {**REC, "mode": mode, "cycle_ms": 20, "also_scope": True}))
        await anyio.sleep(0.3)
        sc = payload(await s.call_tool("rec_stop", {"id": r["id"]}))["scope"]
    rid, sd = r["id"], app.rec.root / "Scope Recordings"
    if case == "behind_poll_confirmed":
        assert "not_this_recording" not in sc and sorted(sc["link"]["files"]) == [f"{rid}.scope.csv", f"{rid}.svdx"], sc
        assert r["scope"]["start_check"].startswith("confirmed by the Scope View log")
        return
    assert "link" not in sc and not (sd / f"{rid}.scope.json").exists(), sc
    assert (sd / f"{rid}.svdx").is_file() and not is_read_only(sd / f"{rid}.scope.csv")    # kept, not sealed
    stale = sc["not_this_recording"]
    if case == "ahead_old_capture":
        assert "s before the recording's first sample" in stale and stale.endswith("an earlier capture Scope still held")
        assert sc["note"].startswith("the Scope data saved is not this recording's"), sc["note"]
    else:
        assert "before StartRecord ran (this PC's clock" in stale and stale.endswith("cannot be told"), stale
        assert sc["note"].startswith("the Scope data saved could not be shown to be this recording's") \
            and "is not this recording's" not in sc["note"], sc["note"]


# ---- A-p7 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("how", ["refused", "pending"])
async def test_r1_a7_new_says_what_ran_when_its_start_record_did_not(monkeypatch, project_dir, fake_xae, how):
    """review 1 of 0.8.18, A-p7: the 'new' choice — its Disconnect ran, then XAE refused StartRecord (or its outcome
    was not known): 0.8.17 said 'not done' and advised the choice again; a pending StartRecord had no owner."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded")
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    app = srv._tcmcp_app
    orig = fake_xae.StartRecord
    n = {"start": 0}

    def start():
        n["start"] += 1
        if n["start"] > 1 and how == "refused":
            fake_xae.calls.append("start-rejected")
            raise _R13Busy("call rejected")
        if n["start"] > 1:
            time.sleep(1.5)                    # longer than the call may take (below): it runs on, and starts Scope
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    elicit, _asked = _answering("new")
    async with client_for(srv, elicitation_callback=elicit) as s:
        a = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        if how == "pending":
            real = app.xae_call

            async def short(fn, *aa, timeout=None, **kw):
                if getattr(fn, "__name__", "") == "scope_action" and (not aa or aa[0] == "start"):
                    timeout = 0.7              # StartRecord times out while it runs: its outcome is not known
                return await real(fn, *aa, timeout=timeout, **kw)
            monkeypatch.setattr(app, "xae_call", short)
        nx = payload(await s.call_tool("rec_stop", {"id": a}))["scope_next"]
        assert nx["choice"] == "new" and "not done" not in str(nx.get("note")), nx
        assert nx["error"].startswith("Scope was disconnected (what it held is discarded); StartRecord"), nx
        assert fake_xae.calls[:5] == STOPPED + ["disconnect"], fake_xae.calls
        if how == "refused":
            assert "StartRecord was refused and did not run" in nx["error"] \
                and "Scope is in Config: scope_record(action='start') starts it" in nx["error"], nx
            assert not fake_xae.recording and app.scope_left is None and app.scope_standalone is None
            return
        assert "StartRecord's outcome is not known" in nx["error"] \
            and "held as a recording of its own until scope_record(action='stop')" in nx["error"], nx
        assert await _until(lambda: fake_xae.recording, n=80)            # it ran after all
        r = await s.call_tool("rec_start", {**REC, "also_scope": True})
        assert r.is_error and "Scope records on its own (scope_record new" in text(r), text(r)
        payload(await s.call_tool("scope_record", {"action": "stop"}))
    assert not fake_xae.recording and fake_xae.calls[-1] == "stop", fake_xae.calls


# ---- A-p10 -----------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_r1_a10_a_start_of_unknown_outcome_is_checked_against_when_it_was_asked(monkeypatch, project_dir,
                                                                                      fake_xae):
    """review 1 of 0.8.18, A-p10: StartRecord held by a dialog (outcome not known), poll recording (no PLC time) —
    0.8.17 ran no stale check at all and linked and sealed a 5-minute-old capture Scope still held."""
    install(monkeypatch)
    from twincat_mcp import dialogs as D
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    st = {"dlg": False}
    dlg = D.Dialog(90, 1, "TwinCAT Scope", "Overwrite?", [(1, "Yes"), (2, "No")]).to_dict()
    monkeypatch.setattr(app, "dialogs_now", lambda: [dlg] if st["dlg"] else [])

    def start():                    # a dialog holds it; once answered, Scope (in Reply with an earlier capture) refuses
        st["dlg"] = True
        time.sleep(3.5)
        st["dlg"] = False
        fake_xae.calls.append("start-refused")
        return 1

    def export(path):
        fake_xae.calls.append("csv")
        end = FT(time.time() - 300)                   # the earlier capture ended 5 min ago
        Path(path).write_text(f"Name\tx\t\nFile\t{path}\t\nStarttime of export\t{end - 40_000_000}\t\n"
                              f"Endtime of export\t{end}\t\n\n\nName\tGVL.x\n1\t2\n", encoding="utf-8")
        return 0
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    monkeypatch.setattr(fake_xae, "ExportCSV", export)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {**REC, "mode": "poll", "cycle_ms": 20, "also_scope": True}))
        assert r["scope"]["start_unknown"] is True, r["scope"]
        assert await _until(lambda: "start-refused" in fake_xae.calls and getattr(app, "xae_held", None) is None, n=100)
        sc = payload(await s.call_tool("rec_stop", {"id": r["id"]}))["scope"]
    rid, sd = r["id"], app.rec.root / "Scope Recordings"
    assert "link" not in sc and not (sd / f"{rid}.scope.json").exists(), sc
    assert not is_read_only(sd / f"{rid}.scope.csv")
    stale = sc["not_this_recording"]
    assert "before StartRecord ran (this PC's clock" in stale and stale.endswith("cannot be told"), stale


# ---- A-p11 / B-p7 ----------------------------------------------------------------------------------------------------

class _Gone(Exception):
    hresult = X.RPC_E_DISCONNECTED


class _EPointer(Exception):
    hresult = X.E_POINTER


@pytest.mark.anyio
@pytest.mark.parametrize("how", ["shell_gone", "e_pointer"])
async def test_r1_a11_b7_a_start_record_that_may_have_acted_stays_with_its_recording(monkeypatch, project_dir,
                                                                                    fake_xae, how):
    """review 1 of 0.8.18, A-p11 / B-p7: StartRecord acted, then the shell went away (RPC_E_DISCONNECTED) or TwinCAT's
    managed code threw E_POINTER ('whether it acted is unknown') — 0.8.17 said 'Scope recording NOT started' and nothing
    ever sent StopRecord to the Scope that recorded."""
    install(monkeypatch)
    orig = fake_xae.StartRecord

    def start():
        orig()                                 # it acted (Scope records) ...
        raise (_Gone("The object invoked has disconnected from its clients") if how == "shell_gone" else
               _EPointer("Object reference not set to an instance of an object."))       # ... then it failed
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    if how == "shell_gone":
        monkeypatch.setattr(X.XaeBackend, "attach", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("no shell")))
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))
        sc = r["scope"]
        assert "error" not in sc and "NOT started" not in json.dumps(sc), sc
        assert sc["started"] is None and sc["start_unknown"] is True and "outcome is not known" in sc["warning"], sc
        assert app.scope_parts[r["id"]].state == "recording" and fake_xae.recording
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))["scope"]
    assert fake_xae.calls == STOPPED and not fake_xae.recording, fake_xae.calls
    assert out["stop"] == {"result_code": 0} and out["start_unknown"], out


# ---- B-p2 ------------------------------------------------------------------------------------------------------------

def _busy_first_reads(sm, n: int) -> None:
    """The first n reads of the message buffer are rejected (the read before the action gives up), later ones answer."""
    from tests.test_xae_backend import ComError
    orig = sm.GetLastErrorMessages
    t = {"n": 0}

    def read():
        t["n"] += 1
        if t["n"] <= n:
            raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
        return orig()
    sm.GetLastErrorMessages = read


@pytest.mark.parametrize("case", ["clearing", "keeps_history", "failed"])
def test_r1_b2_messages_read_after_a_failed_before_read_are_marked_as_possibly_earlier_calls(monkeypatch, case):
    """review 1 of 0.8.18, B-p2: the read of XAE's message buffer before an action was rejected (busy), the read after
    it answered — 0.8.17 gave what the buffer already held (an earlier call's error; a non-clearing buffer's history)
    as this action's own messages, unmarked — also in a failed action's error."""
    from tests.test_xae_backend import WARN, ComError, _MsgSysMan, _xae_with
    be, _dte, sm = _xae_with(monkeypatch, _MsgSysMan(clears=case != "keeps_history"))
    monkeypatch.setattr(be, "_modal_dialogs", lambda: [])
    be.MESSAGES_READ_S = 0.3
    be.cfg.com_retry_s = 3.0
    earlier = "Error: Init4\\Activate Configuration: ADS error 0x745 (1861)"
    sm.buffer = earlier + "\n"                 # an earlier call's (or the user's own) activation
    if case == "keeps_history":
        be.activate_configuration()            # learns that a read leaves this buffer as it is
        assert be._msg_clears is False and be.messages[-1]["messages"] == WARN, be.messages
        be.messages, be.msg_reads = [], []
    if case == "failed":
        def fail():
            sm.buffer += "Error: license missing\n"
            raise ComError(-2147467259, "Unspecified error")
        sm.ActivateConfiguration = fail
    _busy_first_reads(sm, 3)
    if case == "failed":
        with pytest.raises(X.XaeError) as ei:
            be.activate_configuration()
        assert ("XAE messages (ActivateConfiguration) (may include earlier calls' — the read before it failed): "
                + earlier) in str(ei.value), str(ei.value)
        return
    be.activate_configuration()
    rec = be.messages[-1]
    assert earlier in rec["messages"] and WARN in rec["messages"], rec
    assert rec.get("messages_note") == ("the read before this action failed: these may include messages of earlier calls"
                                    + (" — this buffer keeps its history" if case == "keeps_history" else "")), rec


# ---- B-p3 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_r1_b3_rec_delete_during_a_finish_leaves_no_signed_link_to_a_deleted_recording(monkeypatch, project_dir,
                                                                                            fake_xae):
    """review 1 of 0.8.18, B-p3: rec_stop and rec_delete of one recording at once, another job on the ADS worker
    between them — 0.8.17's finish found the recording on disk, the delete ran while the link hashed the Scope files:
    a signed .scope.json for a recording that is gone (and rec_delete said its Scope files were kept)."""
    install(monkeypatch)
    from twincat_mcp import recorder as R
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    orig_stop, orig_sha = R.Recorder.stop, R.file_sha256

    def slow_stop(self, rid, reason="requested"):
        time.sleep(0.5)
        return orig_stop(self, rid, reason)

    def slow_sha(p):
        if str(p).endswith((".svdx", ".csv")) and "Scope Recordings" in str(p):
            time.sleep(0.6)                    # a large .svdx / CSV being hashed
        return orig_sha(p)
    out: dict = {}
    async with client_for(srv) as s:
        rid = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": rid}))
        monkeypatch.setattr(R.Recorder, "stop", slow_stop)
        monkeypatch.setattr(R, "file_sha256", slow_sha)

        async def stop_():
            out["stop"] = payload(await s.call_tool("rec_stop", {"id": rid}))

        async def other_and_delete():
            await anyio.sleep(0.1)
            app.ads_slow_worker.submit(lambda: time.sleep(1.2))      # another recording's rec_stop, an ads watch …
            await anyio.sleep(0.1)
            out["delete"] = payload(await s.call_tool("rec_delete", {"id": rid, "confirm_token": tok["confirm_token"]}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(stop_)
            tg.start_soon(other_and_delete)
    sd = app.rec.root / "Scope Recordings"
    assert rid not in app.rec.recs and not (app.rec.root / f"{rid}.jsonl").exists()
    assert not (sd / f"{rid}.scope.json").exists(), "a signed link to a recording that is gone"
    sc = out["stop"].get("scope") or {}
    assert "link" not in sc and "scope_files_kept" not in out["delete"], (sc, out["delete"])
    assert "the recording was deleted while Scope was saved" in sc.get("note", ""), sc
