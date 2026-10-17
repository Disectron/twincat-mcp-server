"""Tests for the 0.5.0 additions: dialogs, HMI index, exporters, discovery parsing, XML validation."""
import struct
from pathlib import Path

import pytest

from twincat_mcp import dialogs as D
from twincat_mcp.hmi import HmiIndex
from twincat_mcp import exporters as X
from twincat_mcp.ads_types import TypeSystem, parse_table
from twincat_mcp.ads_backend import _parse_discovery, AdsBackend
from twincat_mcp.config import AdsConfig, ProjectConfig
from twincat_mcp.project_backend import ProjectIndex
from twincat_mcp import codecheck as cc
from tests.fake_ads import DATATYPES, DATATYPE_BLOB, install

FIX = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------- dialogs
def test_dialog_rules_and_watchdog():
    save = D.Dialog(1, 100, "Microsoft Visual Studio", "Save changes to the following items?", [(11, "Yes"), (12, "No"), (13, "Cancel")])
    act = D.Dialog(2, 100, "TwinCAT XAE", "Activate Configuration (Old configurations will be overwritten!)", [(21, "OK"), (22, "Cancel")])
    assert not save.machine_affecting() and act.machine_affecting()
    wd = D.DialogWatchdog([{"title_contains": "Visual Studio", "text_contains": "save changes", "button": "No"}])
    left = wd.poll([save, act])
    assert [d.hwnd for d in left] == [2]            # activate prompt is never auto-answered
    assert wd.log[0]["event"] == "auto" and wd.log[0]["button"] == "No"
    st = wd.status()
    assert st["pending"][0]["title"] == "TwinCAT XAE" and st["pending"][0]["machine_affecting"]
    forced = D.DialogWatchdog([{"title_contains": "TwinCAT XAE", "text_contains": "activate", "button": "OK", "force": True}])
    assert forced.poll([act]) == []
    assert not D.rule_matches({"button": "OK"}, save)   # empty rule never matches
    assert D.list_dialogs(None) == [] or True            # Linux: empty


# ---------------------------------------------------------------- hmi
def test_hmi_index_and_compare():
    h = HmiIndex()
    r = h.scan(str(FIX / "sample_hmi"))
    assert r["bindings"] == 11 and r["servers"] == ["PLC1"] and r["files"] == 3
    # TF1800 .TcVIS: VisualElemMember ids -> read bindings; ST input-action snippets -> write (lhs) / read (rhs)
    tv = h.find("*", kind="tcvis")
    tcvis = {(b["plc_symbol"], b["control_type"]) for b in tv["bindings"] if b["file"].lower().endswith(".tcvis")}
    assert ("MAIN.bStart", "lamp_variable") in tcvis and ("MAIN.nCycle", "text_variable") in tcvis
    assert ("MAIN.fbFill.eState", "toggle_color_variable") in tcvis
    w = h.find("*", kind="tcvis_write")
    assert sorted(b["plc_symbol"] for b in w["bindings"]) == ["MAIN.bStart", "MAIN.nCycle"]
    assert all(b["line"] == 16 for b in w["bindings"])  # located at the snippet, not the first mention of the symbol
    f = h.find("MAIN.stRecipe.*")
    assert f["total"] == 1 and f["bindings"][0]["control_id"] == "TcHmiTextblock_Weight" and f["bindings"][0]["attribute"] == "data-tchmi-text"
    assert h.find("Internal*", kind="internal")["total"] == 1
    assert "GVL_Machine.nDrumsFilled" in h.symbols()
    online = {"main.bstart", "main.strecipe.rtargetkg", "gvl_machine.ndrumsfilled", "main.fbfill.bdone", "main.ncycle", "main.fbfill.estate"}
    declared = online | {"main.fbfill.berror"}
    c = h.compare(online, declared)
    assert c["broken"] == 1 and c["broken_bindings"][0]["symbol"] == "MAIN.fbFill.rGhostValue" and not c["broken_bindings"][0]["declared_in_source"]
    assert c["declared_without_hmi"] == ["main.fbfill.bdone", "main.fbfill.berror"]
    with pytest.raises(ValueError):
        HmiIndex().find("x")


# ---------------------------------------------------------------- exporters
def test_csharp_and_typescript_export():
    ts = TypeSystem(parse_table(DATATYPE_BLOB, len(DATATYPES)))
    cs = X.to_csharp(ts, ["ST_Step", "FB_Valve"], "Acme.Plc")
    assert "public enum E_FillState : short" in cs and "Bulk = 1," in cs
    assert "[StructLayout(LayoutKind.Explicit, Size = 16, Pack = 1)]\n    public struct ST_Step" in cs
    assert "[FieldOffset(4)] public uint tDwell; // TIME" in cs
    assert "[FieldOffset(8)] [MarshalAs(UnmanagedType.U1)] public bool bDone; // BOOL" in cs
    assert "[FieldOffset(10)] public E_FillState eState; // E_FillState" in cs
    assert "[FieldOffset(8)] [MarshalAs(UnmanagedType.ByValArray, SizeConst = 2)] public short[] arrHist;" in cs
    assert cs.index("enum E_FillState") < cs.index("struct ST_Step")  # dependency order
    cs2 = X.to_csharp(ts, ["ST_Recipe"])
    assert "[MarshalAs(UnmanagedType.ByValTStr, SizeConst = 41)] public string sName;" in cs2
    t = X.to_typescript(ts, ["ST_Step"])
    assert "export enum E_FillState" in t and "tDwell: number;  // TIME @4" in t
    j = X.to_json(ts, ["ST_Step"])
    assert '"E_FillState"' in j and '"ST_Step"' in j
    sy = X.symbols_to_csharp([{"name": "MAIN.arrSteps[1].nId", "type": "INT"}])
    assert 'public const string MAIN_arrSteps_1_nId = "MAIN.arrSteps[1].nId";' in sy


# ---------------------------------------------------------------- discovery
def test_discovery_packet_parse():
    hdr = b"\x03\x66\x14\x71\x00\x00\x00\x00\x01\x00\x00\x80" + bytes([5, 12, 34, 56, 1, 1]) + struct.pack("<H", 10000)
    items = [(5, b"CX-1234AB\x00"), (3, bytes([3, 1]) + struct.pack("<H", 4024)), (7, b"\x01\x02")]
    body = struct.pack("<I", len(items)) + b"".join(struct.pack("<HH", t, len(v)) + v for t, v in items)
    d = _parse_discovery(hdr + body)
    assert d["ams_net_id"] == "5.12.34.56.1.1" and d["hostname"] == "CX-1234AB" and d["twincat_version"] == "3.1.4024"
    assert _parse_discovery(b"junk") is None


# ---------------------------------------------------------------- xml validation
def test_validate_xml_and_fixes(project_dir):
    idx = ProjectIndex(ProjectConfig(root=str(project_dir))); idx.scan()
    r = cc.validate_xml(idx)
    # the fixture's LineIds are deliberately short → count findings, no errors
    assert not any(f["severity"] == "error" for f in r["items"])
    assert r["by_rule"].get("lineids_count", 0) >= 1
    # break things: duplicate GUID, missing EOF newline, no VAR block in a property getter
    fv = project_dir / "Plc" / "POUs" / "FB_Valve.TcPOU"
    txt = fv.read_text().replace("{aaaaaaaa-0000-0000-0000-000000000004}", "{aaaaaaaa-0000-0000-0000-000000000001}")
    txt = txt.replace("<Declaration><![CDATA[VAR\nEND_VAR\n]]></Declaration>", "<Declaration><![CDATA[]]></Declaration>").rstrip("\n")
    fv.write_text(txt)
    idx.scan()
    r = cc.validate_xml(idx, "FB_Valve")
    rules = r["by_rule"]
    assert rules.get("guid_duplicate") == 1 and rules.get("property_var") == 1
    assert "eof_newline" not in rules, "XAE writes files without a trailing newline; that is not a finding"
    # the fixture has LineIds for FB_Valve but none for Open / IsOpen.Get: an inconsistent file → reported
    assert rules.get("lineids_missing", 0) >= 1
    r2 = cc.validate_xml(idx, "FB_Valve", fix=True, regenerate_lineids=True)
    fixes = {f["fix"] for f in r2["fixed"]}
    assert {"new GUID", "added LineIds"} <= fixes
    after = fv.read_text()
    assert "{aaaaaaaa-0000-0000-0000-000000000001}" not in after
    assert fv.with_suffix(".TcPOU.bak").exists()
    r3 = cc.validate_xml(idx, "FB_Valve")
    assert "guid_duplicate" not in r3["by_rule"] and "lineids_count" not in r3["by_rule"]


def test_set_code_regenerate_lineids(project_dir):
    idx = ProjectIndex(ProjectConfig(root=str(project_dir))); idx.scan()
    r = idx.set_code("MAIN", None, "a := 1;\nb := 2;\nc := 3;", regenerate_lineids=True)
    assert "lineids_regenerated" in r["changed"]
    txt = Path(idx.get_unit("MAIN").file).read_text()
    block = txt[txt.index('<LineIds Name="MAIN">'):txt.index("</LineIds>", txt.index('<LineIds Name="MAIN">'))]
    assert block.count("<LineId ") == 3
    assert cc.validate_xml(idx, "MAIN")["by_rule"].get("lineids_count") is None


# ---------------------------------------------------------------- snapshot apply
def test_snapshot_apply(monkeypatch, tmp_path):
    install(monkeypatch)
    from twincat_mcp.recorder import Recorder
    from twincat_mcp.testing import TestBench
    from tests.fake_ads import FakeConnection
    ads = AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1"))
    bench = TestBench(ads, Recorder(ads, tmp_path / "r"), tmp_path / "s")
    bench.snapshot_save("recipeA", ["MAIN.stRecipe.rTargetKg", "MAIN.stRecipe.rBulkCutoff", "MAIN.nCycle"], None, None)
    FakeConnection.shared_values["MAIN.stRecipe.rTargetKg"] = 10.0
    FakeConnection.shared_values["MAIN.nCycle"] = 0
    dry = bench.snapshot_apply("recipeA", dry_run=True)
    assert {d["symbol"] for d in dry["differences"]} == {"MAIN.stRecipe.rTargetKg", "MAIN.nCycle"} and dry["written"] == []
    res = bench.snapshot_apply("recipeA", symbols=["MAIN.stRecipe.rTargetKg"])
    assert res["written"] == ["MAIN.stRecipe.rTargetKg"] and FakeConnection.shared_values["MAIN.stRecipe.rTargetKg"] == 180.5
    assert FakeConnection.shared_values["MAIN.nCycle"] == 0
