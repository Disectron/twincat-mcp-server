"""Capture profiles: scan → checklist / attributes → save → start, unit and through the MCP server."""
from __future__ import annotations

import csv
import io
import json

import time

import pytest

from twincat_mcp import profiles as P
from twincat_mcp.project_backend import ProjectIndex
from twincat_mcp.config import ProjectConfig


# ---------------------------------------------------------------------------
# heuristics, checklist, attributes, store
# ---------------------------------------------------------------------------
def test_suggest_mode_and_cost():
    assert P.suggest_mode("BOOL") == "change" and P.suggest_mode("E_FillState") == "change" and P.suggest_mode("UDINT") == "change"
    assert P.suggest_mode("LREAL") == "cycle" and P.suggest_mode("TIME") == "cycle"
    assert P.suggest_mode("STRING(80)") == "poll" and P.suggest_mode("ARRAY [0..3] OF LREAL") == "poll"
    assert P.estimate_bytes_per_s(8, "cycle", 1) == (8 + 16) * 1000
    assert P.estimate_bytes_per_s(80, "poll", 100) == 800
    assert P.group_of("MAIN.fbFill.fbValve.bOpen") == "MAIN.fbFill" and P.group_of("GVL_X.arr[3].n") == "GVL_X.arr"


def test_checklist_roundtrip():
    leaves = [{"path": "MAIN.fbFill.bDone", "type": "BOOL", "size": 1},
              {"path": "MAIN.fbFill.tSettle", "type": "TIME", "size": 4},
              {"path": "MAIN.sName", "type": "STRING(80)", "size": 81}]
    text = P.build_checklist(leaves, 1.0, preselect=["MAIN.fbFill.*"])
    rows = list(csv.DictReader(io.StringIO(text)))
    assert [r["select"] for r in rows] == ["x", "x", ""]
    assert rows[1]["suggested_mode"] == "cycle" and rows[2]["cycle_ms"] == ""
    # user edits: deselect bDone, override tSettle to change at 5 ms, select the string
    rows[0]["select"] = ""
    rows[1]["mode"], rows[1]["cycle_ms"] = "change", "5"
    rows[2]["select"] = "X"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=P.CHECKLIST_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    items = P.parse_checklist(buf.getvalue())
    assert [(i.symbol, i.mode, i.cycle_ms) for i in items] == [("MAIN.fbFill.tSettle", "change", 5.0), ("MAIN.sName", "poll", None)]
    with pytest.raises(P.ProfileError, match="select=x"):
        P.parse_checklist(P.build_checklist(leaves))
    with pytest.raises(P.ProfileError, match="columns"):
        P.parse_checklist("a,b\n1,2\n")
    with pytest.raises(P.ProfileError, match="mode"):
        P.parse_checklist("select,symbol,mode\nx,MAIN.x,fast\n")


def test_items_from_attributes_inner_off_wins():
    items = [
        {"path": "MAIN.fbPreact.rTau", "type": "LREAL", "attrs": {"tcmcp.record": "change"}},
        {"path": "MAIN.fbPreact.P[1]", "type": "LREAL", "attrs": {"tcmcp.record": "off"}},
        {"path": "MAIN.fbPreact.bTrip", "type": "BOOL", "attrs": {"tcmcp.record": "on", "tcmcp.cycle_ms": "1"}},
        {"path": "MAIN.nCycle", "type": "UDINT"},
        {"path": "MAIN.arr[*]", "type": "LREAL", "attrs": {"tcmcp.record": "cycle"}},
    ]
    sel = P.items_from_attributes(items)
    assert [(i.symbol, i.mode, i.cycle_ms) for i in sel] == [("MAIN.fbPreact.rTau", "change", None), ("MAIN.fbPreact.bTrip", "change", 1.0)]
    with pytest.raises(P.ProfileError, match="not a number"):
        P.items_from_attributes([{"path": "X", "type": "BOOL", "attrs": {"tcmcp.record": "on", "tcmcp.cycle_ms": "fast"}}])


def test_store_save_get_list_delete(tmp_path):
    st = P.ProfileStore(tmp_path / "profiles")
    prof = P.CaptureProfile("batch", [P.ProfileItem("MAIN.a", "cycle", 1, "LREAL", 8), P.ProfileItem("MAIN.b", "change"),
                                      P.ProfileItem("MAIN.c", "cycle", 1)], cycle_ms=10, stop_when="MAIN.bDone == TRUE")
    st.save(prof)
    got = st.get("batch")
    assert [i.symbol for i in got.items] == ["MAIN.a", "MAIN.b", "MAIN.c"] and got.stop_when == "MAIN.bDone == TRUE"
    groups = got.groups()
    assert set(groups) == {("cycle", 1.0), ("change", 10.0)} and len(groups[("cycle", 1.0)]) == 2
    d = got.to_dict()
    assert d["count"] == 3 and {g["mode"] for g in d["groups"]} == {"cycle", "change"}
    assert st.list()[0]["name"] == "batch" and st.list()[0]["modes"] == ["change", "cycle"]
    with pytest.raises(P.ProfileError, match="exists"):
        st.save(prof, replace=False)
    with pytest.raises(P.ProfileError, match="at least one"):
        st.save(P.CaptureProfile("empty"))
    with pytest.raises(P.ProfileError, match="name"):
        st.get("../x")
    assert st.delete("batch") is True and st.delete("batch") is False and st.list() == []


# ---------------------------------------------------------------------------
# pragmas through the project index
# ---------------------------------------------------------------------------
def test_proj_variables_carries_attrs_down_the_instance_tree(project_dir):
    main = project_dir / "Plc" / "POUs" / "MAIN.TcPOU"
    txt = main.read_text(encoding="utf-8")
    txt = txt.replace("\tfbFill      : FB_Fill;", "\t{attribute 'tcmcp.record' := 'change'}\n\tfbFill      : FB_Fill;")
    main.write_text(txt, encoding="utf-8")
    fill = project_dir / "Plc" / "POUs" / "FB_Fill.TcPOU"
    t2 = fill.read_text(encoding="utf-8").replace("\ttSettle   : TIME := T#500MS;",
                                                  "\t{attribute 'tcmcp.record' := 'off'}\n\ttSettle   : TIME := T#500MS;")
    fill.write_text(t2, encoding="utf-8")
    idx = ProjectIndex(ProjectConfig(root=str(project_dir)))
    idx.scan()
    items = {it["path"]: it for it in idx.variables()["items"]}
    assert items["MAIN.fbFill.bDone"]["attrs"] == {"tcmcp.record": "change"}, "instance pragma reaches the leaves"
    assert items["MAIN.fbFill.fbValve.bOpen"]["attrs"] == {"tcmcp.record": "change"}, "and nested instances"
    assert items["MAIN.fbFill.tSettle"]["attrs"] == {"tcmcp.record": "off"}, "inner pragma overrides"
    assert "attrs" not in items["MAIN.nCycle"]
    sel = {i.symbol for i in P.items_from_attributes(list(items.values()))}
    assert "MAIN.fbFill.bDone" in sel and "MAIN.fbFill.tSettle" not in sel and "MAIN.nCycle" not in sel


# ---------------------------------------------------------------------------
# through the MCP server with the fake ADS
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_profile_tools_e2e(monkeypatch, project_dir, tmp_path):
    from tests.fake_ads import install
    from tests.test_server_e2e import client_for, mk_cfg, payload
    from twincat_mcp.server import build_server

    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    async with client_for(srv) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"rec_scan", "rec_profile_save", "rec_profile_list", "rec_profile_get", "rec_profile_delete",
                "rec_profile_start", "rec_profile_stop"} <= names

        scan = payload(await s.call_tool("rec_scan", {"prefix": "MAIN.fbFill", "checklist": "batch", "preselect": ["*.bDone", "*.tSettle"]}))
        assert scan["count"] >= 4 and "MAIN.fbFill" in scan["groups"]
        assert {"cycle", "change"} & set(scan["suggested_modes"])
        cl = scan["checklist"]
        rows = list(csv.DictReader(open(cl, encoding="utf-8")))
        assert sum(r["select"] == "x" for r in rows) == 2

        prof = payload(await s.call_tool("rec_profile_save", {"name": "batch", "from_checklist": "batch", "symbols": ["MAIN.nCycle"],
                                                              "cycle_ms": 2, "stop_when": "MAIN.fbFill.bDone == TRUE"}))
        syms = {i["symbol"]: i for i in prof["items"]}
        assert set(syms) == {"MAIN.fbFill.bDone", "MAIN.fbFill.tSettle", "MAIN.nCycle"}
        assert syms["MAIN.fbFill.tSettle"]["mode"] == "cycle" and syms["MAIN.nCycle"]["mode"] == "change", "auto mode from runtime types"
        assert prof["source"] == "explicit+checklist"

        lst = payload(await s.call_tool("rec_profile_list", {}))
        assert lst["profiles"][0]["name"] == "batch"
        assert isinstance(lst["profiles"][0]["updated"], str) and "T" in lst["profiles"][0]["updated"]     # D28

        started = payload(await s.call_tool("rec_profile_start", {"name": "batch"}))
        assert started["groups"] == 2 and {r["mode"] for r in started["recordings"]} == {"cycle", "change"}
        # D29: the profile has a stop_when but no start_when → the groups start apart; the response says by how much
        offs = started.get("start_offsets_ms")
        assert offs is None or (min(offs.values()) == 0 and "start_when" in started["note"]), started
        live = payload(await s.call_tool("rec_list", {}))
        assert sum(1 for r in live["session"] if (r.get("name") or "").startswith("batch/")) == 2

        stopped = payload(await s.call_tool("rec_profile_stop", {"name": "batch"}))
        assert len(stopped["stopped"]) == 2
        # D27: stopping again (or after the profile ended by itself) gives the final status, not "no live recording"
        again = payload(await s.call_tool("rec_profile_stop", {"name": "batch"}))
        assert again["stopped"] == [] and len(again["already_ended"]) == 2 and not any(x["active"] for x in again["already_ended"])
        res = await s.call_tool("rec_profile_stop", {"name": "never-started"})
        assert res.is_error

        # project-side scan sees declarations without a PLC
        ps = payload(await s.call_tool("rec_scan", {"source": "project", "prefix": "MAIN", "pattern": "*fbFill*"}))
        assert ps["count"] >= 4 and all(i["path"].startswith("MAIN.fbFill") for i in ps["items"])

        assert payload(await s.call_tool("rec_profile_delete", {"name": "batch"}))["deleted"] is True
        gone = await s.call_tool("rec_profile_delete", {"name": "does_not_exist"})      # #70: say why, not {"deleted": false}
        assert gone.is_error and "not found" in gone.content[0].text
        res = await s.call_tool("rec_profile_get", {"name": "batch"})
        assert res.is_error and "no profile" in res.content[0].text


def test_d28_updated_is_iso_for_old_epoch_profiles():
    from twincat_mcp.profiles import _iso
    assert _iso(0).startswith("1970-01-01T00:00:00") and _iso("2026-10-18T04:00:00+00:00") == "2026-10-18T04:00:00+00:00"
    assert _iso(None) is None


@pytest.mark.anyio
async def test_d29_profile_groups_start_on_the_same_plc_cycle(monkeypatch, project_dir):
    """D29: the change and cycle groups of a profile started ≈ 163 ms apart; files covered different windows. Now every
    group gets the same start trigger on the task's cycle counter (a moment ahead) and the duration counts from it."""
    import json as _j
    from pathlib import Path as _P
    from tests.fake_ads import install
    from tests.test_server_e2e import client_for, mk_cfg, payload
    from twincat_mcp.server import build_server
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir))
    app = srv._tcmcp_app
    async with client_for(srv) as s:
        payload(await s.call_tool("rec_profile_save", {"name": "mix", "symbols": ["MAIN.nCycle", "MAIN.fbFill.tSettle"],
                                                       "mode": "auto", "cycle_ms": 10}))
        st = payload(await s.call_tool("rec_profile_start", {"name": "mix", "duration_s": 1.0}))
        al = st["aligned"]
        assert st["groups"] == 2 and al["trigger"] == "TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount >= 123586", st
        assert al["cycles_ahead"] == 130 and "start_offsets_ms" not in st
        recs = [app.rec.recs[r["id"]] for r in st["recordings"]]
        assert {r.meta.get("start_when") for r in recs} == {al["trigger"]} and {r.post_trigger_s for r in recs} == {1.0}
        payload(await s.call_tool("rec_profile_stop", {"name": "mix"}))
        un = payload(await s.call_tool("rec_profile_start", {"name": "mix", "align": False}))
        assert "aligned" not in un
        payload(await s.call_tool("rec_profile_stop", {"name": "mix"}))
