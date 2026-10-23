"""0.8.18 retest (Ryan, 2026-10-22, bugs.md §38): N195 — the first xae_plcopen_import(replace) of PRG_Machine said a
library reference was removed from its place (it was still in the tree), refused, and left PRG_Machine_1 at the
project root without naming it; N196 — tcunit_results(path=…) (no such parameter) was accepted and the boot result
file read instead; N180 — a TwinCAT restart while Scope recorded (Scope View log: RouterStop → AutoStop → Reply) and a
save that worked: nothing said it. (N194 is in test_recorder.py.)"""
from __future__ import annotations

import pytest

from tests.fake_ads import install
from tests.test_scope_0817 import _link
from tests.test_scope_0818_review import _log_line, _stateful, _view_log
from tests.test_scope_xae import fake_xae  # noqa: F401  (fixture)
from tests.test_server_e2e import client_for, mk_cfg, payload, text
from tests.test_xae_backend import FakeItem, _r15_setup, xae  # noqa: F401  (fixture)
from twincat_mcp import xae_backend as X
from twincat_mcp.server import build_server

REC = {"symbols": ["MAIN.nCycle"], "mode": "change"}


# ---- N196 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_n196_an_unknown_argument_is_refused_before_the_tool_runs(monkeypatch, project_dir):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        r = await s.call_tool("tcunit_results", {"path": r"C:\x\results.xml"})
        assert r.is_error and "tcunit_results: unknown argument(s) ['path']" in text(r) \
            and "'local_path'" in text(r) and "Nothing was done" in text(r), text(r)
        r = await s.call_tool("ads_read", {"name": "MAIN.nCycle", "symbol": "x"})
        assert r.is_error and "['symbol']" in text(r) and "'name'" in text(r), text(r)
        ok = await s.call_tool("tcunit_results", {"xml_text": "<testsuites tests='1' failures='0'><testsuite name='S' "
                                                             "tests='1'><testcase name='T'/></testsuite></testsuites>"})
        assert not ok.is_error, text(ok)                # the known arguments are not touched


# ---- N195 ------------------------------------------------------------------------------------------------------------

def _with_refs(sm, proj, names=("Tc2_Standard", "System_VisuElemMeter")):
    refs = FakeItem("References", "TIPC^Plc^Plc Project^References",
                    [FakeItem(n, f"TIPC^Plc^Plc Project^References^{n}") for n in names])
    proj._children.append(refs)
    sm.items[refs.PathName] = refs
    for c in refs._children:
        sm.items[c.PathName] = c
    return refs


def test_n195_a_reference_missing_from_the_first_listing_after_the_import_does_not_stop_the_replace(
        xae, tmp_path, monkeypatch):
    """The retest: '…^References^System_VisuElemMeter' was missing from the listing right after PlcOpenImport while it
    was in the tree — 'removed 1 existing object(s)', no replace, PRG_Machine_1 left at the root."""
    be, dte, sm = xae
    be.RELIST_SETTLE_S = 0
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    refs = _with_refs(sm, proj)
    meter = refs._children[1]

    def imp():
        dup()
        refs._children.remove(meter)          # the shell re-reads the project: the reference is not enumerated
    proj.on_import = imp
    real, calls = be.tree_children, []

    def listing(*a, **k):
        out = real(*a, **k)
        calls.append(1)
        if len(calls) == 2:                   # the listing right after the import: then the reference is back
            refs._children.append(meter)
        return out
    monkeypatch.setattr(be, "tree_children", listing)
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert len(calls) == 3 and "removed" not in r and "references_not_listed" not in r, r
    assert r["replaced"][0]["object"] == orig.PathName and proj.deleted == ["PRG_TcUnitRunner_1"], r


def test_n195_a_reference_still_not_listed_is_said_and_the_replace_goes_on(xae, tmp_path):
    be, dte, sm = xae
    be.RELIST_SETTLE_S = 0
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    refs = _with_refs(sm, proj)
    proj.on_import = lambda: (dup(), refs._children.pop())
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    gone = "TIPC^Plc^Plc Project^References^System_VisuElemMeter"
    assert r["references_not_listed"] == [gone] and "listed twice" in r["references_note"], r
    assert "removed" not in r and r["replaced"][0]["object"] == orig.PathName, r
    assert proj.deleted == ["PRG_TcUnitRunner_1"]


def test_n195_an_object_gone_from_both_listings_stops_the_replace_and_names_the_copy_left(xae, tmp_path):
    be, dte, sm = xae
    be.RELIST_SETTLE_S = 0
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    folder = sm.items["TIPC^Plc^Plc Project^95_UnitTests"]
    proj.on_import = lambda: (dup(), folder._children.remove(orig), sm.items.pop(orig.PathName),
                              sm.items.pop(meth.PathName))
    with pytest.raises(X.XaeError) as ei:
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    msg = str(ei.value)
    assert "removed 2 existing object(s) from their place (listed twice)" in msg, msg
    assert "renamed copies are still in the project: ['TIPC^Plc^Plc Project^PRG_TcUnitRunner_1']" in msg, msg
    assert proj.deleted == []                 # nothing deleted on its basis


# ---- N180 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("case", ["stopped", "again", "none"])
async def test_n180_a_twincat_restart_while_scope_recorded_is_said_when_the_save_works(monkeypatch, project_dir,
                                                                                      fake_xae, tmp_path, case):
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    log = _view_log(tmp_path, cfg)
    st = _stateful(monkeypatch, fake_xae, log)
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        rid = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        if case != "none":                    # TE130x's lines of a RUN → CONFIG → RUN (0.8.16 retest PC)
            for a, b in (("Record", "RouterStop"), ("RouterStop", "Reply"), ("Reply", "RouterStop"),
                         ("RouterStop", "AutoStop"), ("AutoStop", "Reply")):
                _log_line(log, f"Scope Project state changes from '{a}' to '{b}'")
            if case == "again":
                _log_line(log, "Scope Project state changes from 'Reply' to 'Record'")
            st["s"] = "Record" if case == "again" else "Reply"
        out = payload(await s.call_tool("rec_stop", {"id": rid}))
    sc = out["scope"]
    assert sc["save_svd"].get("result_code") == 0, sc
    if case == "none":
        assert "restart_seen" not in sc and "restart" not in _link(app, rid), sc
        return
    rs = sc["restart_seen"]
    assert rs["router_stop_at"] == "2026-10-21 23:00:00.000" and rs["auto_stop_at"] == "2026-10-21 23:00:00.000", rs
    if case == "stopped":
        assert "Scope stopped recording at the restart — its data ends there" in sc["note"], sc
    else:
        assert rs["recorded_again_at"] and "Scope recorded again after each — its data has a gap" in sc["note"], sc
    assert _link(app, rid)["restart"] == rs


# ---- review 1 of 0.8.19 ----------------------------------------------------------------------------------------------

def test_n195_r1_a_copy_missing_from_the_first_listing_is_read_again(xae, tmp_path, monkeypatch):
    """Review 1: the same transient listing could hide the renamed copy itself — the replace then said 'the import
    made no copy … unchanged' and PRG_TcUnitRunner_1 stayed at the root."""
    be, dte, sm = xae
    be.RELIST_SETTLE_S = 0
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    proj.on_import = dup
    real, calls = be.tree_children, []

    def listing(*a, **k):
        out = real(*a, **k)
        calls.append(1)
        if len(calls) == 2:                   # the listing right after the import misses the copy
            out = {**out, "items": [x for x in out["items"] if not str(x.get("Name")).endswith("_1")
                                    and "_1^" not in str(x.get("PathName"))]}
            out["count"] = len(out["items"])
        return out
    monkeypatch.setattr(be, "tree_children", listing)
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert len(calls) == 3 and r["replaced"][0]["object"] == orig.PathName, r
    assert proj.deleted == ["PRG_TcUnitRunner_1"]


@pytest.mark.anyio
@pytest.mark.parametrize("case", ["not_recording", "twice"])
async def test_n180_r1_the_state_scope_was_in_and_the_last_restart_decide(monkeypatch, project_dir, fake_xae,
                                                                           tmp_path, case):
    """Review 1: a router stop while Scope was in Reply (its capture had ended) was said to end its data; with two
    restarts the first one's Record-again was taken as the data running on."""
    install(monkeypatch)
    cfg = mk_cfg(project_dir, mode="guarded", allow_elicitation=False)
    log = _view_log(tmp_path, cfg)
    st = _stateful(monkeypatch, fake_xae, log)
    srv = build_server(cfg)
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        rid = payload(await s.call_tool("rec_start", {**REC, "also_scope": True}))["id"]
        if case == "not_recording":
            seq = [("Record", "Reply"), ("Reply", "RouterStop"), ("RouterStop", "AutoStop"), ("AutoStop", "Reply")]
        else:
            one = [("Record", "RouterStop"), ("RouterStop", "Reply"), ("Reply", "RouterStop"),
                   ("RouterStop", "AutoStop"), ("AutoStop", "Reply")]
            seq = one + [("Reply", "Record")] + one
        for a, b in seq:
            _log_line(log, f"Scope Project state changes from '{a}' to '{b}'")
        st["s"] = "Reply"
        out = payload(await s.call_tool("rec_stop", {"id": rid}))
    rs = out["scope"]["restart_seen"]
    if case == "not_recording":
        assert "restarts" not in rs and "while Scope was in Reply, not Record" in rs["note"], rs
        assert "its data ends there" not in out["scope"]["note"], out["scope"]
    else:
        assert len(rs["restarts"]) == 2 and "recorded_again_at" not in rs, rs
        assert "Scope stopped recording at the restart — its data ends there" in rs["note"] and "earlier gaps" in \
            rs["note"], rs["note"]
    assert _link(app, rid)["restart"] == rs
