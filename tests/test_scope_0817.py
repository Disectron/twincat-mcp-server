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
    return json.loads((app.rec.root / "Scope Recordings" / f"{rid}.scope.json").read_text(encoding="utf-8"))


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
    sd = app.rec.root / "Scope Recordings"
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
                                                         "max_events": 3, "also_scope": True, "scope_disconnect": True}))
            rid = r["id"]
            assert await _until(lambda: not app.rec.recs[rid].active, n=500, dt_s=0.005)
            out = payload(await s.call_tool("rec_stop", {"id": rid}))
            link = _link(app, rid)
            seen.append((out.get("stop_reason"), link.get("finished_by"), link.get("recording_ended")))
    for reason, fb, ended in seen:
        assert fb == f"the recording ended by itself ({reason})" and ended["stop_reason"] == reason, seen


# ---- N186 (0.8.16 retest): Scope in Reply refuses StartRecord; an earlier capture never becomes this recording's ------

def _view_log(tmp_path, cfg):
    log = tmp_path / "TE130X" / "TcScopeView.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("(2026-10-21-22:40:00:000)[ INFO ] Scope View: start\n", encoding="utf-8")
    cfg.scope.view_log = str(log)
    return log


def _logs(log, line):
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


@pytest.mark.anyio
async def test_n186_a_start_scope_refused_in_reply_is_not_started(monkeypatch, project_dir, fake_xae, tmp_path):
    """StartRecord returned 1 and TE130x logged 'Invalid ScopeState! Config requested, actual- Reply' — 0.8.16 said
    'started: true', and at the end saved and signed the previous capture as this recording's."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    log = _view_log(tmp_path, cfg)

    def start():
        fake_xae.calls.append("start")
        _logs(log, "(2026-10-21-22:43:32:332)[ ERROR ] Scope View: Invalid ScopeState! Config requested, actual- Reply")
        return 1
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    e = r["scope"]["error"]
    assert e.startswith("Scope recording NOT started: Scope refused StartRecord — it is in state Reply") and \
        "do not repeat rec_start" in e and r["scope"]["scope_state"] == "Reply", r["scope"]
    assert st["scope"]["state"] == "finished" and "NOT started" in st["scope"]["error"], st["scope"]
    assert fake_xae.calls == ["start"] and "NOT started" in out["scope"]["error"], (fake_xae.calls, out["scope"])
    sd = app.rec.root / "Scope Recordings"
    assert not sd.is_dir() or not any(p.name.startswith(r["id"]) for p in sd.iterdir())


@pytest.mark.anyio
async def test_n186_a_start_the_log_confirms_is_said_so(monkeypatch, project_dir, fake_xae, tmp_path):
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    log = _view_log(tmp_path, cfg)
    orig = fake_xae.StartRecord

    def start():
        _logs(log, "(2026-10-21-22:50:00:000)[ INFO ] Scope View: Scope Project state changes from 'Config' to 'Record'")
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        await s.call_tool("rec_stop", {"id": r["id"]})
    assert r["scope"]["started"] is True and r["scope"]["start_check"].startswith("confirmed by the Scope View log"), \
        r["scope"]
    assert "result_note" not in r["scope"], r["scope"]


@pytest.mark.anyio
async def test_n186_scope_record_start_refused_is_an_error(monkeypatch, project_dir, fake_xae, tmp_path):
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    log = _view_log(tmp_path, cfg)

    def start():
        _logs(log, "(2026-10-21-22:45:43:000)[ ERROR ] Scope View: Invalid ScopeState! Config requested, actual- Reply")
        return 1
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = await s.call_tool("scope_record", {"action": "start"})
    assert r.is_error and "Scope refused StartRecord — it is in state Reply" in text(r) and "rec_start" not in text(r), \
        text(r)


@pytest.mark.anyio
@pytest.mark.parametrize("clock", ["pc", "plc", "fresh"])
async def test_n186_an_earlier_capture_is_never_linked_as_this_recordings(monkeypatch, project_dir, fake_xae, clock):
    """Defence behind the start check: the CSV Scope exports says when its data was recorded — data that ends minutes
    before the recording's first sample is an earlier capture: kept, not linked to the recording, not sealed."""
    install(monkeypatch)

    def export(path):
        fake_xae.calls.append("csv")
        now_ft = int((time.time() + 11_644_473_600) * 10_000_000)
        # 335 s before the recording began (the retest: 5.6 min); 'fresh': the data of this recording
        end = now_ft - (0 if clock == "fresh" else 335 * 10_000_000)
        start_ = end - 41_000_000
        Path(path).write_text(
            f"Name\t20261021-224543-7dd7\t\nFile\t{path}\t\n"
            f"Starttime of export\t{start_}\t21 October 2026\t22:40:04.001\n"
            f"Endtime of export\t{end}\t21 October 2026\t22:40:08.100\n\n\nName\tGVL.x\n1\t2\n", encoding="utf-8")
        return 0
    from pathlib import Path
    from twincat_mcp import recorder as R
    monkeypatch.setattr(fake_xae, "ExportCSV", export)
    if clock == "plc":                                     # a recording with PLC time (cycle / change mode)
        plc_t0 = int((time.time() + 11_644_473_600) * 10_000_000)
        monkeypatch.setattr(R.Recorder, "t0_filetime", lambda self, rid: plc_t0)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "poll", "cycle_ms": 20,
                                                     "max_events": 5, "also_scope": True}))
        assert await _until(lambda: app.scope_parts[r["id"]].state == "finished", n=200)
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    sc = out["scope"]
    sd = app.rec.root / "Scope Recordings"
    if clock == "fresh":
        assert sc["link"]["files"] and "not_this_recording" not in sc, sc
        return
    assert "link" not in sc and sc["not_this_recording"].endswith("an earlier capture Scope still held"), sc
    assert ("before StartRecord ran (this PC's clock" if clock == "pc" else
            "s before the recording's first sample") in sc["not_this_recording"], sc
    assert not (sd / f"{r['id']}.scope.json").exists() and not is_read_only(sd / f"{r['id']}.svdx")


# ---- second review of 0.8.17 ------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("users_run", [False, True])
async def test_r2_n1_a_start_withdrawn_from_the_xae_queue_is_not_started(monkeypatch, project_dir, fake_xae, users_run):
    """rec_start(also_scope) cancelled while its StartRecord waited behind another XAE job: the worker withdrew it — it
    never ran. The part was taken for 'whether it ran is not known' and stopped, saved and signed a Scope this
    recording never started — with users_run, the user's own Scope run."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        if users_run:
            payload(await s.call_tool("scope_record", {"action": "start"}))
            fake_xae.calls.clear()
        blocker = app.xae_worker.submit(lambda: time.sleep(2.0))          # another XAE job runs for 2 s
        with anyio.move_on_after(0.8):
            await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "duration_s": 1,
                                            "also_scope": True})
        assert await _until(lambda: bool(app.rec.recs))
        rid = next(iter(app.rec.recs))
        while not blocker.done():
            await anyio.sleep(0.1)
        await anyio.sleep(1.5)                            # the recording has ended by itself meanwhile
        out = payload(await s.call_tool("rec_stop", {"id": rid}))
    part = app.scope_parts[rid]
    assert part.state == "finished" and "before StartRecord ran" in part.result["error"], part.result
    assert "start" not in fake_xae.calls and "stop" not in fake_xae.calls, fake_xae.calls
    assert not (app.rec.root / "Scope Recordings" / f"{rid}.scope.json").exists() and "NOT started" in out["scope"]["error"]
    assert fake_xae.recording is users_run                 # the user's own run records on


@pytest.mark.anyio
async def test_r2_n2_rec_status_of_a_live_recording_is_never_deleted(monkeypatch, project_dir, fake_xae):
    from twincat_mcp import recorder as R
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    orig = R.Recorder.reap
    arm = {"on": False}

    def reap(self):
        if arm["on"]:          # sealing another recording whose file a virus scanner holds
            raise PermissionError(13, "The process cannot access the file because it is being used by another process")
        return orig(self)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        monkeypatch.setattr(R.Recorder, "reap", reap)
        arm["on"] = True
        st = await s.call_tool("rec_status", {"id": r["id"]})
        arm["on"] = False
        await s.call_tool("rec_stop", {"id": r["id"]})
    assert st.is_error and "being used by another process" in text(st) and "deleted" not in text(st), text(st)


@pytest.mark.anyio
async def test_r2_l1_a_left_scope_part_by_its_id_prefix(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    busy = _busy_stop(monkeypatch, fake_xae)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": r["id"]}))
        payload(await s.call_tool("rec_delete", {"id": r["id"], "confirm_token": tok["confirm_token"]}))
        pre = r["id"][:-2]
        st = payload(await s.call_tool("rec_status", {"id": pre}))
        busy["busy"] = False
        out = payload(await s.call_tool("rec_stop", {"id": pre}))
    assert st["id"] == r["id"] and st["scope"]["stop_refused"], st
    assert out["id"] == r["id"] and out["scope"]["stop"]["result_code"] == 0 and not fake_xae.recording, out


@pytest.mark.anyio
async def test_r2_gap_a_start_refused_answers_requests_that_waited(monkeypatch, project_dir, fake_xae):
    """rec_stop while the part is 'starting', then StartRecord is refused: not_started answers the waiting request —
    without it rec_stop hung."""
    install(monkeypatch)

    def start():
        time.sleep(1.0)
        fake_xae.calls.append("start-rejected")
        raise _R13Busy("call rejected")
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    cfg.xae.com_retry_s = 0.6
    srv = build_server(cfg)
    app = srv._tcmcp_app
    outs: dict = {}
    async with client_for(srv) as s:
        async def start_():
            outs["start"] = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change",
                                                                     "also_scope": True}))

        async def stop_():
            assert await _until(lambda: bool(app.scope_parts), dt_s=0.02)
            rid = next(iter(app.scope_parts))
            outs["state"] = app.scope_parts[rid].state
            with anyio.move_on_after(15) as cs:
                outs["stop"] = payload(await s.call_tool("rec_stop", {"id": rid}))
            outs["hung"] = cs.cancelled_caught
        async with anyio.create_task_group() as tg:
            tg.start_soon(start_)
            tg.start_soon(stop_)
    assert outs["state"] == "starting" and not outs["hung"], outs
    assert "NOT started" in outs["stop"]["scope"]["error"] and "stop" not in fake_xae.calls, (outs, fake_xae.calls)


@pytest.mark.anyio
async def test_r2_gap_a_part_whose_recording_is_being_deleted_is_not_listed_as_left(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    _slow_remove(monkeypatch, 1.5)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "rec_delete", "target": r["id"]}))
        seen: dict = {}

        async def delete():
            seen["d"] = payload(await s.call_tool("rec_delete", {"id": r["id"], "confirm_token": tok["confirm_token"]}))

        async def list_():
            assert await _until(lambda: r["id"] in app.rec.deleting, n=100, dt_s=0.02)
            app.rec.recs.pop(r["id"], None)                # the recorder no longer holds it, the delete still runs
            seen["l"] = payload(await s.call_tool("rec_list", {}))
        async with anyio.create_task_group() as tg:
            tg.start_soon(delete)
            tg.start_soon(list_)
    assert "scope_without_recording" not in seen["l"], seen["l"]


@pytest.mark.anyio
async def test_r2_gap_a_start_cancelled_while_it_runs_stays_with_the_recording(monkeypatch, project_dir, fake_xae):
    """The opposite of N1: rec_start cancelled while StartRecord RUNS — it may have started Scope: the part stays
    (start_unknown) and rec_stop sends StopRecord."""
    install(monkeypatch)
    orig = fake_xae.StartRecord

    def start():
        time.sleep(1.5)
        return orig()
    monkeypatch.setattr(fake_xae, "StartRecord", start)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        with anyio.move_on_after(0.6):
            await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True})
        assert await _until(lambda: bool(app.scope_parts))
        rid = next(iter(app.scope_parts))
        assert await _until(lambda: app.scope_parts[rid].state == "recording", n=100)
        out = payload(await s.call_tool("rec_stop", {"id": rid}))
    assert app.scope_parts[rid].run["start_unknown"].startswith("rec_start was cancelled while StartRecord ran")
    assert fake_xae.calls[:2] == ["start", "stop"] and not fake_xae.recording and out["scope"]["finished_by"] == "rec_stop"


# ---- Ryan, 2026-10-21: Scope's files in 'Scope Recordings'; after a recording the user chooses close / new / pause ----

def _answering(choice=None, disconnect=None):
    """An elicitation callback that answers the Scope questions (and records them)."""
    from mcp import types as _types
    asked: list[str] = []

    async def elicit(context, params):
        asked.append(params.message)
        props = (params.requested_schema or {}).get("properties", {})
        if "next" in props:
            return _types.ElicitResult(action="accept", content={"next": choice})
        if "disconnect" in props:
            return _types.ElicitResult(action="accept", content={"disconnect": disconnect})
        return _types.ElicitResult(action="decline")
    return elicit, asked


@pytest.mark.anyio
@pytest.mark.parametrize("choice", ["close", "new", "pause"])
async def test_ryan_after_rec_stop_scope_closes_starts_anew_or_pauses(monkeypatch, project_dir, fake_xae, choice):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app
    elicit, asked = _answering(choice)
    async with client_for(srv, elicitation_callback=elicit) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
    sd = app.rec.root / "Scope Recordings"
    assert (sd / f"{r['id']}.svdx").is_file() and (sd / f"{r['id']}.scope.json").is_file()   # the folder Ryan named
    assert len(asked) == 1 and "What should Scope do now?" in asked[0] and f"{r['id']}.svdx" in asked[0], asked
    nx = out["scope_next"]
    assert nx["choice"] == choice, nx
    if choice == "close":
        assert fake_xae.calls[-1] == "disconnect" and app.scope_left is None and not fake_xae.recording, fake_xae.calls
    elif choice == "new":
        assert fake_xae.calls[-2:] == ["disconnect", "start"] and nx["started"] and fake_xae.recording, fake_xae.calls
        assert "linked to no recording" in nx["note"] and app.scope_left is None
    else:
        assert "disconnect" not in fake_xae.calls and app.scope_left["choice"] == "pause", fake_xae.calls


@pytest.mark.anyio
async def test_ryan_paused_scope_the_next_recording_asks_to_disconnect(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app
    elicit, asked = _answering("pause", disconnect=True)
    async with client_for(srv, elicitation_callback=elicit) as s:
        r1 = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        payload(await s.call_tool("rec_stop", {"id": r1["id"]}))
        fake_xae.calls.clear()
        r2 = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        await s.call_tool("rec_stop", {"id": r2["id"]})
    assert any("Scope still holds the recording of " + r1["id"] in q and "Disconnect it" in q for q in asked), asked
    assert fake_xae.calls[:2] == ["disconnect", "start"] and r2["scope"]["started"], fake_xae.calls


@pytest.mark.anyio
async def test_ryan_without_a_prompt_the_agent_is_told_to_ask(monkeypatch, project_dir, fake_xae):
    """No prompt in the session: rec_stop's result and rec_status say what to ask; the next recording with Scope is
    refused until the answer comes as scope_disconnect — nothing started meanwhile."""
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        r = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "duration_s": 1,
                                                     "also_scope": True}))
        assert await _until(lambda: app.scope_parts[r["id"]].state == "finished")
        st = payload(await s.call_tool("rec_status", {"id": r["id"]}))
        out = payload(await s.call_tool("rec_stop", {"id": r["id"]}))
        n_recs = len(app.rec.recs)
        again = await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True})
        n_after = len(app.rec.recs)
        no_scope = payload(await s.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change",
                                                            "also_scope": True, "scope_disconnect": False}))
        await s.call_tool("rec_stop", {"id": no_scope["id"]})
    assert "What should Scope do now?" in st["scope"]["next"]["ask_user"], st["scope"]
    assert "scope_record(action='disconnect')" in out["scope_next"]["then"], out
    assert again.is_error and "scope_disconnect=true" in text(again) and n_after == n_recs, text(again)
    assert "scope" not in no_scope and "disconnect" not in fake_xae.calls, (no_scope, fake_xae.calls)


@pytest.mark.anyio
async def test_ryan_scope_record_disconnect_of_unsaved_data_needs_the_users_ok(monkeypatch, project_dir, fake_xae):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded", allow_elicitation=False))
    async with client_for(srv) as s:
        r = await s.call_tool("scope_record", {"action": "disconnect"})
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "scope_record", "target": "disconnect:"}))
        ok = payload(await s.call_tool("scope_record", {"action": "disconnect", "confirm_token": tok["confirm_token"]}))
    assert r.is_error and "confirm" in text(r).lower() and fake_xae.calls == ["disconnect"], (text(r), fake_xae.calls)
    assert ok["result_code"] == 0


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_ryan_the_scope_question_on_both_protocol_eras(monkeypatch, project_dir, fake_xae, mode):
    """2026-07-28 ('auto'): the question is returned as input_required and rec_stop runs again with the answer — the
    recording is not stopped twice, StopRecord / SaveSVD run once, Disconnect once."""
    from mcp import Client, types as _types
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="guarded"))
    app = srv._tcmcp_app
    asked: list[str] = []

    async def elicit(context, params):
        asked.append(params.message)
        return _types.ElicitResult(action="accept", content={"next": "close"})
    async with Client(srv, mode=mode, elicitation_callback=elicit) as c:
        r = payload(await c.call_tool("rec_start", {"symbols": ["MAIN.nCycle"], "mode": "change", "also_scope": True}))
        out = payload(await c.call_tool("rec_stop", {"id": r["id"]}))
    assert len(asked) == 1 and out["scope_next"]["choice"] == "close", (asked, out.get("scope_next"))
    assert fake_xae.calls == ["start", "stop", "save", "csv", "disconnect"], fake_xae.calls
    assert out["scope"]["finished_by"] == "rec_stop" and app.scope_left is None
