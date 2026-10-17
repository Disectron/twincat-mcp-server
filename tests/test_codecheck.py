from pathlib import Path

import pytest

from twincat_mcp.config import ProjectConfig
from twincat_mcp.project_backend import ProjectIndex, ProjectError
from twincat_mcp import codecheck as cc


@pytest.fixture
def idx(project_dir):
    p = ProjectIndex(ProjectConfig(root=str(project_dir)))
    p.scan()
    return p


def test_fb_interface(idx):
    i = cc.fb_interface(idx, "FB_Fill")
    assert i["extends"] == ["FB_Base"]
    assert [v["name"] for v in i["inputs"]] == ["bEnable", "stRecipe"]
    assert [v["name"] for v in i["outputs"]] == ["bDone", "bError"]
    m = i["methods"][0]
    assert m["name"] == "Start" and m["return_type"] == "BOOL" and m["inputs"][0]["name"] == "rTarget" and m["access"] == "PUBLIC"
    v = cc.fb_interface(idx, "FB_Valve")
    assert v["properties"][0]["name"] == "IsOpen" and v["properties"][0]["get"] and not v["properties"][0]["set"]


def test_lint_rules(idx):
    r = cc.lint(idx)
    rules = r["by_rule"]
    unused = [f for f in r["items"] if f["rule"] == "unused"]
    assert any(f["unit"] == "FB_Valve" and "tOpened" in f["message"] for f in unused)
    assert any(f["unit"] == "FB_Fill" and "fbFine" in f["message"] for f in unused)
    assert not any("fbValve" in f["message"] for f in unused)  # used in Bulk state
    undoc = [f for f in r["items"] if f["rule"] == "undocumented_io"]
    assert {f["message"].split()[1] for f in undoc} >= {"bEnable", "stRecipe", "bDone", "bError"}
    assert all(f["line"] > 0 for f in r["items"])
    one = cc.lint(idx, "FB_Valve", rules=["unused"])
    assert one["by_rule"] == {"unused": 1}
    # naming override: require 'x' prefix for BOOL -> flags bOpen
    r2 = cc.lint(idx, "FB_Valve", rules=["naming"], prefixes={"BOOL": ["x"]})
    assert any("bOpen" in f["message"] for f in r2["items"])


def test_lint_detects_real_eq_assign_input_super(tmp_path, idx):
    pou = Path(idx.get_unit("FB_Fill").file)
    txt = pou.read_text()
    txt = txt.replace("fbValve.Open();", "fbValve.Open();\n\t\tIF stRecipe.rTargetKg = 5.0 THEN bEnable := FALSE; END_IF")
    pou.write_text(txt)
    idx.scan()
    r = cc.lint(idx, "FB_Fill")
    assert "real_eq" not in r["by_rule"]  # rTargetKg is a struct member, only direct REAL vars are checked
    assert r["by_rule"].get("assign_input") == 1
    # add FB_init without SUPER^
    txt = pou.read_text().replace("    <LineIds Name=\"FB_Fill\">",
        '    <Method Name="FB_init" Id="{aaaaaaaa-0000-0000-0000-0000000000f1}">\n      <Declaration><![CDATA[METHOD FB_init : BOOL\nVAR_INPUT\n\tbInitRetains : BOOL;\n\tbInCopyCode : BOOL;\nEND_VAR\n]]></Declaration>\n      <Implementation>\n        <ST><![CDATA[eState := 0;]]></ST>\n      </Implementation>\n    </Method>\n    <LineIds Name="FB_Fill">')
    pou.write_text(txt)
    idx.scan()
    r = cc.lint(idx, "FB_Fill", rules=["missing_super"])
    assert r["by_rule"] == {"missing_super": 1}


def test_call_graph(idx):
    g = cc.call_graph(idx, "MAIN", depth=5)
    names = [(n["name"], n["depth"]) for n in g["nodes"]]
    assert names[0] == ("MAIN", 0)
    assert ("FB_Fill", 1) in names and ("FB_Valve.Open", 2) in names
    assert g["unique"] == 4  # MAIN, FB_Fill, FB_Valve (instance body), FB_Valve.Open


def test_tcunit_scaffold(idx, project_dir):
    r = cc.tcunit_scaffold(idx, "FB_Fill")
    f = Path(r["file"])
    assert f.name == "FB_Fill_Tests.TcPOU" and f.parent.name == "Tests"
    txt = f.read_text()
    assert "FUNCTION_BLOCK FB_Fill_Tests EXTENDS TcUnit.FB_TestSuite" in txt
    assert r["tests"] == ["Test_InitialState", "Test_Start"]
    assert "fbUut.Start(rTarget := 0.0)" in txt
    assert "AssertEquals_BOOL(Expected := FALSE, Actual := fbUut.bDone" in txt
    plc = (project_dir / "Plc" / "Plc.plcproj").read_text()
    assert 'Include="POUs\\Tests\\FB_Fill_Tests.TcPOU"' in plc
    # indexed and well-formed
    import xml.etree.ElementTree as ET
    ET.fromstring(txt)
    assert idx.get_unit("FB_Fill_Tests").kind == "FUNCTION_BLOCK"
    with pytest.raises(ProjectError, match="exists"):
        cc.tcunit_scaffold(idx, "FB_Fill")
    with pytest.raises(ProjectError, match="function blocks"):
        cc.tcunit_scaffold(idx, "MAIN")
    r2 = cc.tcunit_scaffold(idx, "FB_Fill", overwrite=True)
    assert r2["plcproj"]["already"]


def test_d17_real_eq_ignores_assignments_and_bindings(idx):
    """D17: only `=` / `<>` comparisons of a REAL count; `:=`, `<=`, `>=` and `=>` are not equality tests."""
    pou = Path(idx.get_unit("FB_Fill").file)
    txt = pou.read_text().replace("\tarrWeights : ARRAY[0..9] OF LREAL;\n",
                                  "\tarrWeights : ARRAY[0..9] OF LREAL;\n\trA : LREAL;\n\trB : LREAL;\n\tbX : BOOL;\n")
    txt = txt.replace("fbValve.Open();", "fbValve.Open();\n\t\trA := rB;\n\t\tbX := rA >= rB;\n\t\tbX := rA <= 1.0;\n"
                                         "\t\tIF rA = rB THEN bX := TRUE; END_IF\n\t\tIF rB <> 0.0 THEN bX := FALSE; END_IF")
    pou.write_text(txt)
    idx.scan()
    r = cc.lint(idx, "FB_Fill", rules=["real_eq"])
    assert r["by_rule"] == {"real_eq": 2}, r["items"]
