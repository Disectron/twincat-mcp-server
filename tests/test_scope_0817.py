"""First adversarial review of 0.8.17 (one owner per recording's Scope part): its HIGH and MEDIUM findings as tests.
H1 a call still stopping the recording labelled as one that had ended; H2 the link's 'started' before StartRecord ran;
H3 rec_delete while the part was finishing; H4 Scope recording with nothing to stop it; M1 rec_stop just after the
recording ended by itself."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time

import anyio
import pytest

from twincat_mcp.recorder import is_read_only
from twincat_mcp.server import build_server
from tests.fake_ads import install
from tests.test_scope_xae import fake_xae, _R13Busy  # noqa: F401  (fixture)
from tests.test_server_e2e import client_for, mk_cfg, payload, text


def _link(app, rid):
    return json.loads((app.rec.root / "scope" / f"{rid}.scope.json").read_text(encoding="utf-8"))


async def _until(cond, n=200, dt_s=0.05):
    for _ in range(n):
        if cond():
            return True
        await anyio.sleep(dt_s)
    return False


def _slow_status(monkeypatch, delay=1.0):
    """The owner's status call is slow (Recorder.status while reap() closes another recording's ADS connection)."""
    from twincat_mcp import recorder as R
    orig = R.Recorder.status
    arm = {"on": False, "in": threading.Event()}

    def status(self, rid):
        if arm["on"] and threading.current_thread().name != "MainThread":
            arm["on"] = False
            arm["in"].set()
            time.sleep(delay)
        return orig(self, rid)
    monkeypatch.setattr(R.Recorder, "status", status)
    return arm


def _busy_stop(monkeypatch, fake_xae):
    orig = fake_xae.StopRecord
    state = {"busy": True}

    def stop():
        if state["busy"]:
            fake_xae.calls.append("stop-rejected")
            raise _R13Busy("call rejected")
        return orig()
    monkeypatch.setattr(fake_xae, "StopRecord", stop)
    return state


def _slow_remove(monkeypatch, delay=1.5):
    from twincat_mcp import recorder as R
    orig_rm = R._remove_file

    def slow_remove(p, tries=10):
        time.sleep(delay)                      # Windows: a scanner holds the file, its removal retried
        return orig_rm(p, tries)
    monkeypatch.setattr(R, "_remove_file", slow_remove)


# ---- H1 --------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["rec_stop", "rec_delete"])
async def test_h1_a_call_arriving_during_the_owners_status_read_owns_the_end(monkeypatch, project_dir, fake_xae, tool):
    install(monkeypatch)
    arm = _slow_status(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        rid = r["id"]
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": rid}))
        arm["on"] = True
        assert await _until(arm["in"].is_set, dt_s=0.02)
        if tool == "rec_stop":
            out = payload(await s.call_tool("rec_stop", {"id": rid}))
        else:
            out = payload(await s.call_tool("rec_delete", {"id": rid, "confirm_token": tok["confirm_token"]}))
    assert out["scope"]["finished_by"] == tool, out["scope"]
    if tool == "rec_stop":
        assert _link(app, rid)["finished_by"] == "rec_stop"


@pytest.mark.anyio
async def test_h1_a_rec_stop_while_start_record_runs_finishes_the_part(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    orig = fake_xae.StartRecord

    def start():
        time.sleep(1.5)
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    outs: dict = {}
    async with client_for(srv) as s:
        async def start_():
            outs["start"] = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change",
                                                                     "also_scope": True}))

        async def stop_():
            assert await _until(lambda: bool(app.rec.recs), dt_s=0.02)
            rid = next(iter(app.rec.recs))
            assert await _until(lambda: rid in app.scope_parts, dt_s=0.02)
            st = payload(await s.call_tool("rec_status", {"id": rid}))
            outs["starting"] = st.get("scope")
            outs["stop"] = payload(await s.call_tool("rec_stop", {"id": rid}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(start_)
            tg.start_soon(stop_)
    rid = outs["start"]["id"]
    assert outs["starting"]["state"] == "starting", outs["starting"]
    assert outs["stop"]["scope"]["finished_by"] == "rec_stop", outs["stop"]
    assert _link(app, rid)["finished_by"] == "rec_stop" and not fake_xae.recording


@pytest.mark.anyio
async def test_h1_rec_profile_stop_is_not_a_call_that_ended(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True,
                                                     "name": "line1/manual"}))
        await s.call_tool("rec_profile_stop", {"name": "line1"})
        assert await _until(lambda: app.scope_parts[r["id"]].state == "finished")
    fb = _link(app, r["id"])["finished_by"]
    assert fb.startswith("rec_profile_stop") and "the call ended" not in fb, fb


# ---- H2 --------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_h2_started_is_when_start_record_ran(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    orig = fake_xae.StartRecord
    st: dict = {"t0": None, "ran": None}

    def start():
        if st["t0"] is None:
            st["t0"] = time.time()
        if time.time() - st["t0"] < 2.0:
            raise _R13Busy("call rejected")
        st["ran"] = time.time()
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 10
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        await s.call_tool("rec_stop", {"id": r["id"]})
    started = dt.datetime.fromisoformat(_link(app, r["id"])["started"]).timestamp()
    assert started >= st["ran"] - 0.05, (started, st)


@pytest.mark.anyio
async def test_h2_a_start_of_unknown_outcome_is_said_in_the_link(monkeypatch, project_dir, fake_xae):
    from twincat_mcp import dialogs as D
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    st = {"dlg": False}
    dlg = D.Dialog(90, 1, "TwinCAT Scope", "Overwrite?", [(1, "Yes"), (2, "No")]).to_dict()
    monkeypatch.setattr(app, "dialogs_now", lambda: [dlg] if st["dlg"] else [])
    orig = fake_xae.StartRecord

    def start():
        st["dlg"] = True
        time.sleep(3.5)
        st["dlg"] = False
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        await anyio.sleep(3.0)
        await s.call_tool("rec_stop", {"id": r["id"]})
    link = _link(app, r["id"])
    assert link["started"] is None and link["start_unknown"] and link["start_asked"], link
    assert link["scope"] == "Measurement\\Scope_Counter", link


# ---- H3 --------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_h3_rec_delete_while_the_part_finishes_waits_and_says_what_stays(monkeypatch, project_dir, fake_xae):
    """The recording ended by itself and its Scope part is being saved when rec_delete comes: the finish completes as
    it began (saved and linked while the recording existed), then the recording is deleted — rec_delete says which
    Scope files stay. Nothing is sealed into a link to a recording that is already gone."""
    install(monkeypatch)
    in_save = threading.Event()
    orig_save = fake_xae.SaveSVD

    def slow_save(path):
        in_save.set()
        time.sleep(2.0)
        return orig_save(path)
    monkeypatch.setattr(fake_xae, "SaveSVD", slow_save)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "duration_s": 1,
                                                     "also_scope": True}))
        rid = r["id"]
        assert await _until(in_save.is_set, n=400)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": rid}))
        d = payload(await s.call_tool("rec_delete", {"id": rid, "confirm_token": tok["confirm_token"]}))
    link = _link(app, rid)
    assert not (app.rec.root / f"{rid}.jsonl").exists()
    assert d["scope_files_kept"]["files"] == sorted(link["files"]) and link["recording"] == rid, d
    assert "ended by itself" in link["finished_by"], link


@pytest.mark.anyio
async def test_h3_no_link_for_a_recording_deleted_while_scope_was_saved(monkeypatch, project_dir, fake_xae):
    """A finish that decided to save, and the recording is deleted while SaveSVD runs (a delete that did not wait:
    the recorder removed it directly): Scope's files are kept, but no signed link to a recording that is gone."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    orig_save = fake_xae.SaveSVD

    def save_and_delete(path):
        rc = orig_save(path)
        app.rec.delete(rid_box["id"])            # the recording goes while Scope is saved
        return rc
    rid_box: dict = {}
    monkeypatch.setattr(fake_xae, "SaveSVD", save_and_delete)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        rid_box["id"] = r["id"]
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    sd = app.rec.root / "scope"
    assert not (sd / f"{r['id']}.scope.json").exists() and "link" not in out["scope"], out["scope"]
    assert "not linked to it nor sealed" in out["scope"]["note"], out["scope"]
    assert (sd / f"{r['id']}.svdx").exists() and not is_read_only(sd / f"{r['id']}.svdx")


# ---- H4 --------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_h4_a_refused_stop_of_a_deleted_recording_can_still_be_asked_again(monkeypatch, project_dir, fake_xae):
    """The recording ended, XAE refused StopRecord (stop_refused); rec_delete is cancelled while the file is removed.
    0.8.16 / 0.8.17 r0: Scope recorded on, rec_status and rec_stop said 'unknown', rec_list did not list it."""
    install(monkeypatch)
    busy = _busy_stop(monkeypatch, fake_xae)
    _slow_remove(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "poll", "cycle_ms": 20,
                                                     "max_events": 3, "also_scope": True}))
        rid = r["id"]
        assert await _until(lambda: app.scope_parts[rid].state == "stop_refused", n=100, dt_s=0.1)
        busy["busy"] = False                                 # XAE answers again
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": rid}))
        with anyio.move_on_after(0.4):
            await s.call_tool("rec_delete", {"id": rid, "confirm_token": tok["confirm_token"]})
        assert await _until(lambda: not (app.rec.root / f"{rid}.jsonl").exists() and rid not in app.rec.deleting)
        lst = payload(await s.call_tool("rec_list", {}))
        st = payload(await s.call_tool("rec_status", {"id": rid}))
        out = payload(await s.call_tool("rec_stop", {"id": rid}))
    assert [o["id"] for o in lst["scope_without_recording"]] == [rid], lst.get("scope_without_recording")
    assert st["scope"]["state"] == "recording" and st["scope"]["stop_refused"], st
    assert not fake_xae.recording and out["scope"]["stop"]["result_code"] == 0, (fake_xae.calls, out)
    assert "save" not in fake_xae.calls and app.scope_parts[rid].state == "finished"


# ---- M1 --------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_m1_rec_stop_right_after_an_end_of_its_own_says_that_end(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    seen = []
    async with client_for(srv) as s:
        for _ in range(3):
            r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "poll", "cycle_ms": 20,
                                                         "max_events": 3, "also_scope": True}))
            rid = r["id"]
            assert await _until(lambda: not app.rec.recs[rid].active, n=500, dt_s=0.005)
            out = payload(await s.call_tool("rec_stop", {"id": rid}))
            link = _link(app, rid)
            seen.append((out.get("stop_reason"), link.get("finished_by"), link.get("recording_ended")))
    for reason, fb, ended in seen:
        assert fb == f"the recording ended by itself ({reason})" and ended["stop_reason"] == reason, seen
