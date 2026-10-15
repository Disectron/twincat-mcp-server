from pathlib import Path

import pytest

from twincat_mcp.config import ProjectConfig
from twincat_mcp.project_backend import ProjectIndex, ProjectError, parse_tcunit_xml, parse_vars


def idx(project_dir):
    p = ProjectIndex(ProjectConfig(root=str(project_dir)))
    p.scan()
    return p


def test_scan_counts(project_dir):
    p = idx(project_dir)
    r = p.scan()
    assert r["files"] == 5
    assert r["by_kind"]["PROGRAM"] == 1
    assert r["by_kind"]["FUNCTION_BLOCK"] == 2
    assert r["by_kind"]["STRUCT"] == 1
    assert r["by_kind"]["GVL"] == 1
    assert r["by_kind"]["METHOD"] == 2
    assert r["by_kind"]["PROPERTY"] == 1
    assert r["plcproj"][0]["compile_items"] == 5
    assert len(r["solutions"]) == 1 and len(r["tsproj"]) == 1


def test_list_and_get_code(project_dir):
    p = idx(project_dir)
    fbs = p.list_units(kind="FUNCTION_BLOCK")
    assert [u["name"] for u in fbs["units"]] == ["FB_Fill", "FB_Valve"]
    assert fbs["units"][0]["extends"] == ["FB_Base"]
    code = p.get_code("FB_Fill")
    assert "CASE eState OF" in code["implementation"]
    assert any(v["name"] == "fbValve" and v["type"] == "FB_Valve" and v["comment"] == "bulk valve" for v in code["vars"])
    assert any(v["name"] == "tSettle" and v["init"] == "T#500MS" for v in code["vars"])
    assert {s["name"] for s in code["subitems"]} == {"FB_Fill.Start"}
    m = p.get_code("FB_Fill.Start")
    assert m["kind"] == "METHOD" and m["vars"][0]["name"] == "rTarget"
    assert m["declaration"].splitlines()[0].split("|")[1].strip().startswith("METHOD Start")


def test_line_numbers_point_into_file(project_dir):
    p = idx(project_dir)
    u = p.get_unit("MAIN")
    text = Path(u.file).read_text(encoding="utf-8-sig").splitlines()
    v = next(v for v in u.vars if v.name == "fbFill")
    assert "fbFill" in text[v.line - 1]
    v2 = next(v for v in u.vars if v.name == "bStart")
    assert v2.at == "%I*"


def test_resolve_symbol_chain(project_dir):
    p = idx(project_dir)
    r = p.resolve_symbol("MAIN.fbFill.fbValve.bOpen")
    assert r["resolved"]
    hops = r["hops"]
    assert [h["segment"] for h in hops] == ["MAIN", "fbFill", "fbValve", "bOpen"]
    assert hops[1]["declared_in"] == "MAIN" and hops[1]["type"] == "FB_Fill"
    assert hops[2]["declared_in"] == "FB_Fill" and hops[2]["file"].endswith("FB_Fill.TcPOU")
    assert hops[3]["declared_in"] == "FB_Valve" and hops[3]["section"] == "VAR_OUTPUT"
    # GVL symbol without prefix
    r2 = p.resolve_symbol("GVL_Machine.nDrumsFilled")
    assert r2["resolved"] and r2["hops"][1]["type"] == "UDINT"
    # array index stripped, struct member resolved
    r3 = p.resolve_symbol("MAIN.stRecipe.rBulkCutoff")
    assert r3["resolved"] and r3["hops"][2]["declared_in"] == "ST_Recipe"
    # unknown member reports error, no exception
    r4 = p.resolve_symbol("MAIN.fbFill.nope")
    assert not r4["resolved"] and "error" in r4["hops"][-1]


def test_search_and_references(project_dir):
    p = idx(project_dir)
    s = p.search(r"fbValve\.Open")
    assert s["hits"] and s["hits"][0]["unit"] == "FB_Fill"
    refs = p.references("FB_Valve")
    inst = {(i["unit"], i["var"]) for i in refs["instances"]}
    assert ("FB_Fill", "fbValve") in inst and ("FB_Fill", "fbFine") in inst


def test_set_code_roundtrip_and_lineids(project_dir):
    p = idx(project_dir)
    f = Path(p.get_unit("MAIN").file)
    before = f.read_text(encoding="utf-8")
    new_impl = "nCycle := nCycle + 2;\n// edited"
    r = p.set_code("MAIN", None, new_impl)
    assert "implementation" in r["changed"] and "lineids_removed(1)" in r["changed"]
    after = f.read_text(encoding="utf-8")
    assert new_impl in after
    assert '<LineIds Name="MAIN">' not in after
    # declaration untouched byte-for-byte
    assert before.split("<Declaration>")[1].split("</Declaration>")[0] == after.split("<Declaration>")[1].split("</Declaration>")[0]
    assert Path(r["backup"]).exists()
    # re-indexed
    assert p.get_code("MAIN", with_line_numbers=False)["implementation"] == new_impl
    # method edit only touches the method block
    r2 = p.set_code("FB_Fill.Start", "METHOD Start : BOOL\nVAR_INPUT\n\trTarget : LREAL;\n\trMax : LREAL;\nEND_VAR\n", None)
    assert "declaration" in r2["changed"]
    code = p.get_code("FB_Fill", with_line_numbers=False)
    assert "CASE eState OF" in code["implementation"]
    assert any(v["name"] == "rMax" for v in p.get_code("FB_Fill.Start")["vars"])


def test_cdata_terminator_escaped(project_dir):
    p = idx(project_dir)
    p.set_code("FB_Valve", None, "sTxt := ']]>';")
    assert p.get_code("FB_Valve", with_line_numbers=False)["implementation"] == "sTxt := ']]>';"


def test_parse_vars_multi_name_and_sections():
    v = parse_vars("VAR_INPUT\n a, b : INT; // two\nEND_VAR\nVAR\n c : ARRAY[0..1] OF BOOL := [TRUE, FALSE];\nEND_VAR")
    assert [(x.name, x.section) for x in v] == [("a", "VAR_INPUT"), ("b", "VAR_INPUT"), ("c", "VAR")]
    assert v[2].init == "[TRUE, FALSE]"


def test_tcunit_parse(project_dir):
    txt = (project_dir / "TcUnit_xUnit_testresults.xml").read_text()
    r = parse_tcunit_xml(txt)
    assert r["summary"] == {"tests": 3, "failures": 1, "skipped": 0}
    assert r["passed"] == 2 and not r["all_passed"]
    assert r["failed_tests"] == ["FB_Fill_Test::Test_CutoffApplied: EXP: 0.9, ACT: 0.85"]
    with pytest.raises(ProjectError):
        parse_tcunit_xml("<not xml")


def test_unknown_root(tmp_path):
    p = ProjectIndex(ProjectConfig(root=str(tmp_path / "nope")))
    with pytest.raises(ProjectError):
        p.scan()
