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


def test_d39_scaffold_uses_enum_members_and_names_failing_stubs(idx, project_dir):
    """D39: an enum input was arranged as 'E_Command.0' (does not compile)."""
    (project_dir / "Plc" / "DUTs" / "E_Command.TcDUT").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<TcPlcObject Version="1.1.0.1" ProductVersion="3.1.4026.12">\n'
        '  <DUT Name="E_Command" Id="{aaaaaaaa-0000-0000-0000-0000000000e1}">\n'
        "    <Declaration><![CDATA[{attribute 'qualified_only'}\n{attribute 'strict'}\nTYPE E_Command :\n(\n"
        "\tNone := 0,   // nothing\n\tStart,\n\tStop\n) UINT;\nEND_TYPE\n]]></Declaration>\n  </DUT>\n</TcPlcObject>\n",
        encoding="utf-8")
    from tests.conftest import register_sources
    register_sources(project_dir / "Plc" / "Plc.plcproj")
    pou = Path(idx.get_unit("FB_Fill").file)
    pou.write_text(pou.read_text().replace("\tbEnable", "\teCmd      : E_Command;\n\teOther    : E_Lib;\n\tbEnable", 1))
    idx.refresh()
    r = cc.tcunit_scaffold(idx, "FB_Fill", overwrite=True)
    txt = Path(r["file"]).read_text()
    assert "fbUut.eCmd := E_Command.None;" in txt and "E_Command.0" not in txt
    assert "fbUut.eOther := 0 (* E_Lib: set a member *);" in txt
    assert r["stubs_fail_until_written"] == ["Test_Start"] and "FAIL on purpose" in r["stub_note"]


def test_a_declaration_that_runs_fb_init_is_its_use(tmp_path):
    """0.8.11 retest (§30, info): proj_lint called the TcUnit suite instance fbCounterModuleTests 'never used' — a
    suite (EXTENDS FB_TestSuite) registers itself in FB_init, so declaring it is its use. A project FB with an FB_init
    along its chain is not reported; one that extends a library FB (whose FB_init the project cannot see) is reported
    as info that says so; an unused instance of an FB without either stays a warning."""
    from twincat_mcp.config import ProjectConfig
    from twincat_mcp.project_backend import ProjectIndex
    d = tmp_path / "p"
    d.mkdir()

    def pou(name, decl, impl="", methods=""):
        (d / f"{name}.TcPOU").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<TcPlcObject Version="1.1.0.1">\n'
            f'  <POU Name="{name}" Id="{{aaaaaaaa-0000-0000-0000-{sum(map(ord, name)):012d}}}">\n'
            f"    <Declaration><![CDATA[{decl}]]></Declaration>\n"
            f"    <Implementation><ST><![CDATA[{impl}]]></ST></Implementation>\n{methods}  </POU>\n</TcPlcObject>\n",
            encoding="utf-8")
    pou("FB_CounterModuleTests", "FUNCTION_BLOCK FB_CounterModuleTests EXTENDS TcUnit.FB_TestSuite\nVAR\nEND_VAR")
    init = ('    <Method Name="FB_init" Id="{aaaaaaaa-0000-0000-0000-000000009999}">\n      <Declaration><![CDATA['
            "METHOD FB_init : BOOL\nVAR_INPUT\n    bInitRetains : BOOL;\n    bInCopyCode : BOOL;\nEND_VAR]]></Declaration>\n"
            "      <Implementation><ST><![CDATA[Register(THIS);]]></ST></Implementation>\n    </Method>\n")
    pou("FB_Registered", "FUNCTION_BLOCK FB_Registered\nVAR\nEND_VAR", methods=init)
    pou("FB_Child", "FUNCTION_BLOCK FB_Child EXTENDS FB_Registered\nVAR\nEND_VAR")
    pou("FB_Plain", "FUNCTION_BLOCK FB_Plain\nVAR\nEND_VAR")
    pou("PRG_Tests", "PROGRAM PRG_Tests\nVAR\n    fbCounterModuleTests : FB_CounterModuleTests;\n    fbChild : FB_Child;\n"
        "    fbPlain : FB_Plain;\n    fbTimer : TON;\nEND_VAR", "")
    idx = ProjectIndex(ProjectConfig(root=str(d)))
    idx.scan()
    got = {f["message"].split(" ")[0]: f["severity"] for f in cc.lint(idx, "PRG_Tests", rules=["unused"])["items"]}
    assert got == {"fbCounterModuleTests": "info", "fbPlain": "warning", "fbTimer": "warning"}, got
