"""proj_lint OOP / safety rules: interface_contract, extends_cycle, abstract_instantiation,
shadowed_member, unsigned_loop, new_delete.

Each rule is exercised with one positive fixture and the whole set with a clean FB that must
produce no finding (false positives are the failure mode that matters for an offline linter)."""
from pathlib import Path

import pytest

from twincat_mcp.config import ProjectConfig
from twincat_mcp.project_backend import ProjectIndex
from twincat_mcp import codecheck


def _pou(name: str, header: str, body: str, members: str = "", decl_vars: str = "") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<TcPlcObject Version="1.1.0.1" ProductVersion="3.1.4024.12">
  <POU Name="{name}" Id="{{00000000-0000-0000-0000-{abs(hash(name)) % 10**12:012d}}}" SpecialFunc="None">
    <Declaration><![CDATA[{header}
{decl_vars}
]]></Declaration>
    <Implementation>
      <ST><![CDATA[{body}]]></ST>
    </Implementation>
{members}
  </POU>
</TcPlcObject>
"""


def _method(name: str, header: str, body: str, decl_vars: str = "") -> str:
    return f"""    <Method Name="{name}" Id="{{00000000-0000-0000-0001-{abs(hash(name)) % 10**12:012d}}}">
      <Declaration><![CDATA[{header}
{decl_vars}
]]></Declaration>
      <Implementation>
        <ST><![CDATA[{body}]]></ST>
      </Implementation>
    </Method>
"""


def _itf(name: str, methods: list[tuple[str, str, str]], extends: str = "") -> str:
    ms = "".join(f"""    <Method Name="{m}" Id="{{00000000-0000-0000-0002-{abs(hash(name + m)) % 10**12:012d}}}">
      <Declaration><![CDATA[{hdr}
{vars_}
]]></Declaration>
    </Method>
""" for m, hdr, vars_ in methods)
    ext = f" EXTENDS {extends}" if extends else ""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<TcPlcObject Version="1.1.0.1" ProductVersion="3.1.4024.12">
  <Itf Name="{name}" Id="{{00000000-0000-0000-0003-{abs(hash(name)) % 10**12:012d}}}">
    <Declaration><![CDATA[INTERFACE {name}{ext}
]]></Declaration>
{ms}
  </Itf>
</TcPlcObject>
"""


@pytest.fixture
def oop_dir(tmp_path: Path) -> Path:
    d = tmp_path / "oop" / "Plc" / "POUs"
    d.mkdir(parents=True)
    (tmp_path / "oop" / "Plc" / "Plc.plcproj").write_text('<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003"><PropertyGroup><Name>Plc</Name></PropertyGroup></Project>')
    (d / "I_Thing.TcIO").write_text(_itf("I_Thing", [
        ("Start", "METHOD Start : BOOL", "VAR_INPUT\n\trTarget : LREAL;\nEND_VAR"),
        ("Stop", "METHOD Stop : BOOL", ""),
    ]))
    (d / "I_Named.TcIO").write_text(_itf("I_Named", [("GetName", "METHOD GetName : STRING", "")], extends="I_Thing"))
    (d / "FB_Base.TcPOU").write_text(_pou(
        "FB_Base", "FUNCTION_BLOCK ABSTRACT FB_Base", ";",
        decl_vars="VAR\n\tnCount : INT;\nEND_VAR",
        members=_method("Reset", "METHOD Reset : BOOL", "nCount := 0;")))
    # child: implements I_Thing but misses Stop; Start shadows the member rTarget; FB_init without SUPER^
    (d / "FB_Child.TcPOU").write_text(_pou(
        "FB_Child", "FUNCTION_BLOCK FB_Child EXTENDS FB_Base IMPLEMENTS I_Thing", "fbTon(IN := TRUE, PT := T#1S);",
        decl_vars="VAR\n\tfbTon : TON;\n\trTarget : LREAL;\nEND_VAR",
        members=_method("Start", "METHOD Start : BOOL", "THIS^.rTarget := rTarget;\nStart := TRUE;",
                        "VAR_INPUT\n\trTarget : LREAL;\nEND_VAR")
        + _method("FB_init", "METHOD FB_init : BOOL", "nCount := 1;",
                  "VAR_INPUT\n\tbInitRetains : BOOL;\n\tbInCopyCode : BOOL;\nEND_VAR")))
    # good: implements I_Named (which extends I_Thing) completely with matching signatures, signed loop, THIS^ free
    (d / "FB_Good.TcPOU").write_text(_pou(
        "FB_Good", "FUNCTION_BLOCK FB_Good EXTENDS FB_Base IMPLEMENTS I_Named",
        "FOR i := 0 TO nMax - 1 DO\n\tarr[i] := 0;\nEND_FOR",
        decl_vars="VAR\n\ti : INT;\n\tnMax : INT := 4;\n\tarr : ARRAY[0..9] OF INT;\nEND_VAR",
        members=_method("Start", "METHOD Start : BOOL", "Start := TRUE;", "VAR_INPUT\n\trTarget : LREAL;\nEND_VAR")
        + _method("Stop", "METHOD Stop : BOOL", "Stop := TRUE;")
        + _method("GetName", "METHOD GetName : STRING", "GetName := 'good';")
        + _method("FB_init", "METHOD FB_init : BOOL", "SUPER^.FB_init(bInitRetains, bInCopyCode);",
                  "VAR_INPUT\n\tbInitRetains : BOOL;\n\tbInCopyCode : BOOL;\nEND_VAR")))
    # wrong signature: Start without the rTarget input
    (d / "FB_BadSig.TcPOU").write_text(_pou(
        "FB_BadSig", "FUNCTION_BLOCK FB_BadSig IMPLEMENTS I_Thing", ";",
        members=_method("Start", "METHOD Start : BOOL", "Start := TRUE;") + _method("Stop", "METHOD Stop : BOOL", "Stop := TRUE;")))
    # base from a library: contract cannot be proven -> info, never error
    (d / "FB_LibChild.TcPOU").write_text(_pou(
        "FB_LibChild", "FUNCTION_BLOCK FB_LibChild EXTENDS FB_FromLibrary IMPLEMENTS I_Thing, I_External", ";",
        members=_method("Start", "METHOD Start : BOOL", "Start := TRUE;", "VAR_INPUT\n\trTarget : LREAL;\nEND_VAR")))
    # unsigned loops
    (d / "FB_Loop.TcPOU").write_text(_pou(
        "FB_Loop", "FUNCTION_BLOCK FB_Loop",
        "FOR nIdx := nCount - 1 TO 0 BY -1 DO\n\t;\nEND_FOR\nFOR nIdx := 0 TO nCount - 1 DO\n\t;\nEND_FOR\nFOR nIdx := 0 TO 10 - 1 DO\n\t;\nEND_FOR",
        decl_vars="VAR\n\tnIdx : UINT;\n\tnCount : UINT;\nEND_VAR"))
    # dynamic memory
    (d / "FB_Pool.TcPOU").write_text(_pou("FB_Pool", "FUNCTION_BLOCK FB_Pool", ";", decl_vars="VAR\n\tn : INT;\nEND_VAR"))
    (d / "FB_Alloc.TcPOU").write_text(_pou(
        "FB_Alloc", "FUNCTION_BLOCK FB_Alloc", "pPool := __NEW(FB_Pool);",
        decl_vars="VAR\n\tpPool : POINTER TO FB_Pool;\nEND_VAR"))
    (d / "FB_AllocOk.TcPOU").write_text(_pou(
        "FB_AllocOk", "FUNCTION_BLOCK FB_AllocOk", "pBuf := __NEW(BYTE, 64);",
        decl_vars="VAR\n\tpBuf : POINTER TO BYTE;\nEND_VAR",
        members=_method("FB_exit", "METHOD FB_exit : BOOL", "__DELETE(pBuf);", "VAR_INPUT\n\tbInCopyCode : BOOL;\nEND_VAR")))
    # cycle
    (d / "FB_CycA.TcPOU").write_text(_pou("FB_CycA", "FUNCTION_BLOCK FB_CycA EXTENDS FB_CycB", ";"))
    (d / "FB_CycB.TcPOU").write_text(_pou("FB_CycB", "FUNCTION_BLOCK FB_CycB EXTENDS FB_CycA", ";"))
    # abstract instantiation in a program, pointer is fine
    (d / "MAIN.TcPOU").write_text(_pou(
        "MAIN", "PROGRAM MAIN", "fbGood();",
        decl_vars="VAR\n\tfbBase : FB_Base;\n\tpBase : POINTER TO FB_Base;\n\tfbGood : FB_Good;\n\taBases : ARRAY[0..1] OF FB_Base;\nEND_VAR"))
    return tmp_path / "oop"


@pytest.fixture
def lint(oop_dir):
    idx = ProjectIndex(ProjectConfig(root=str(oop_dir)))
    idx.scan()
    rep = codecheck.lint(idx, rules=["interface_contract", "extends_cycle", "abstract_instantiation",
                                     "shadowed_member", "unsigned_loop", "new_delete"])
    return rep["items"]


def _of(items, rule, unit_prefix=None):
    return [i for i in items if i["rule"] == rule and (unit_prefix is None or i["unit"].lower().startswith(unit_prefix.lower()))]


def test_interface_contract_missing_and_signature(lint):
    child = _of(lint, "interface_contract", "FB_Child")
    assert len(child) == 1 and child[0]["severity"] == "error" and "Stop" in child[0]["message"]
    bad = _of(lint, "interface_contract", "FB_BadSig")
    assert len(bad) == 1 and bad[0]["severity"] == "error" and "signature differs" in bad[0]["message"]


def test_interface_contract_through_extended_interface_and_bases_is_clean(lint):
    assert _of(lint, "interface_contract", "FB_Good") == []
    assert _of(lint, "shadowed_member", "FB_Good") == []
    assert _of(lint, "unsigned_loop", "FB_Good") == []


def test_interface_contract_never_claims_through_a_library(lint):
    lib = _of(lint, "interface_contract", "FB_LibChild")
    assert lib and all(i["severity"] == "info" for i in lib)
    assert any("I_External" in i["message"] for i in lib)
    assert any("Stop" in i["message"] and "outside the project" in i["message"] for i in lib)


def test_shadowed_member(lint):
    sh = _of(lint, "shadowed_member")
    assert len(sh) == 1 and sh[0]["unit"] == "FB_Child.Start" and "THIS^.rTarget" in sh[0]["message"]


def test_unsigned_loop(lint):
    ul = _of(lint, "unsigned_loop", "FB_Loop")
    sev = sorted(i["severity"] for i in ul)
    assert sev == ["error", "warning"], ul          # BY -1 -> error, TO n - 1 -> warning, literal 10 - 1 -> nothing
    assert "65536" in next(i["message"] for i in ul if i["severity"] == "error")


def test_new_delete(lint):
    nd = _of(lint, "new_delete")
    units = sorted(i["unit"] for i in nd)
    assert units == ["FB_Alloc", "FB_Alloc"], nd    # leak warning + missing attribute error; FB_AllocOk is clean
    assert {i["severity"] for i in nd} == {"warning", "error"}
    assert any("enable_dynamic_creation" in i["message"] for i in nd)


def test_extends_cycle(lint):
    cyc = _of(lint, "extends_cycle")
    assert {i["unit"] for i in cyc} == {"FB_CycA", "FB_CycB"} and all(i["severity"] == "error" for i in cyc)


def test_abstract_instantiation(lint):
    ab = _of(lint, "abstract_instantiation")
    names = sorted(i["message"].split(" :")[0] for i in ab)
    assert names == ["aBases", "fbBase"], ab          # pointer and the concrete FB_Good are fine


def test_default_rule_set_includes_new_rules(oop_dir):
    idx = ProjectIndex(ProjectConfig(root=str(oop_dir)))
    idx.scan()
    rep = codecheck.lint(idx)
    assert {"interface_contract", "unsigned_loop", "new_delete", "extends_cycle", "abstract_instantiation", "shadowed_member"} <= set(rep["by_rule"])


def test_round2_interface_dispatch_inheritance_scaffold(oop_dir):
    """Field report round 2: #58 interface calls reach the implementing FBs (incl. inherited / overridden methods),
    #59 proj_interface merges the EXTENDS chain and inherited IMPLEMENTS, #67 interface methods are not
    'empty_method', #66 scaffold keeps declared defaults, the project's ProductVersion and inherited methods."""
    d = oop_dir / "Plc" / "POUs"
    (d / "FB_Machine.TcPOU").write_text(_pou(
        "FB_Machine", "FUNCTION_BLOCK FB_Machine", "FOR i := 1 TO 2 DO\n\taModules[i].Start(rTarget := 1.0);\nEND_FOR",
        decl_vars="VAR\n\taModules : ARRAY[1..2] OF I_Thing;\n\ti : INT;\nEND_VAR"))
    (d / "FB_Hooked.TcPOU").write_text(_pou(
        "FB_Hooked", "FUNCTION_BLOCK FB_Hooked IMPLEMENTS I_Thing", ";",
        decl_vars="VAR_INPUT\n\tnPreset : INT := 10;\nEND_VAR",
        members=_method("Start", "METHOD Start : BOOL", "THIS^.OnRunning();\nStart := TRUE;", "VAR_INPUT\n\trTarget : LREAL;\nEND_VAR")
        + _method("Stop", "METHOD Stop : BOOL", "Stop := TRUE;")
        + _method("OnRunning", "METHOD PROTECTED OnRunning", ";")).replace("3.1.4024.12", "3.1.4026.12"))
    (d / "FB_Counter.TcPOU").write_text(_pou(
        "FB_Counter", "FUNCTION_BLOCK FB_Counter EXTENDS FB_Hooked", ";",
        decl_vars="VAR_OUTPUT\n\tnCount : INT;\nEND_VAR",
        members=_method("OnRunning", "METHOD PROTECTED OnRunning", "nCount := nCount + 1;")).replace("3.1.4024.12", "3.1.4026.12"))
    idx = ProjectIndex(ProjectConfig(root=str(oop_dir)))
    idx.scan()
    g = codecheck.call_graph(idx, "FB_Machine", 4)
    names = {n["name"] for n in g["nodes"]}
    assert {"FB_Child.Start", "FB_Good.Start", "FB_Hooked.Start"} <= names, names
    assert "FB_Counter.OnRunning" in names and "FB_Hooked.OnRunning" in names      # THIS^ dispatch reaches the override
    assert any("via I_Thing" in (n.get("via") or "") for n in g["nodes"])
    itf = codecheck.fb_interface(idx, "FB_Counter")
    assert itf["implements"] == ["I_Thing"] and itf["implements_own"] == []
    meths = {m["name"]: m for m in itf["methods"]}
    assert meths["Start"]["inherited_from"] == "FB_Hooked" and "inherited_from" not in meths["OnRunning"]
    assert any(v["name"] == "nPreset" and v["inherited_from"] == "FB_Hooked" for v in itf["inputs"])
    lint = codecheck.lint(idx, None, ["empty_method"])
    assert not any(str(f.get("unit") or f.get("name") or "").startswith("I_") for f in lint["items"]), lint["items"]
    sc = codecheck.tcunit_scaffold(idx, "FB_Counter", add_to_plcproj=False)
    body = Path(sc["file"]).read_text()
    assert 'ProductVersion="3.1.4026.12"' in body and "fbUut.nPreset := 10;" in body
    assert "Test_Start" in sc["tests"] and "Test_Stop" in sc["tests"] and "inherited from FB_Hooked" in body
