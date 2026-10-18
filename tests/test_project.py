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
    assert r["summary"] == {"tests": 3, "failures": 1, "errors": 0, "skipped": 0}
    assert r["passed"] == 2 and not r["all_passed"]
    assert r["failed_tests"] == ["FB_Fill_Test::Test_CutoffApplied: EXP: 0.9, ACT: 0.85"]
    with pytest.raises(ProjectError):
        parse_tcunit_xml("<not xml")


def test_r3_tcunit_counts_cases_not_suite_attributes():
    """R3: a <failure> in a suite whose 'failures' attribute is missing (or 0) came out as all_passed: true."""
    xml = ('<testsuites><testsuite name="x" tests="2">'
           '<testcase name="a" classname="x"/><testcase name="b" classname="x"><failure message="boom"/></testcase>'
           '</testsuite></testsuites>')
    r = parse_tcunit_xml(xml)
    assert r["all_passed"] is False and r["passed"] == 1 and r["failed_tests"] == ["x::b: boom"]
    lie = xml.replace('tests="2"', 'tests="2" failures="0"')
    r = parse_tcunit_xml(lie)
    assert not r["all_passed"] and r["attribute_mismatch"] == ["x: attribute failures=0, counted 1"]
    r = parse_tcunit_xml('<testsuites><testsuite name="y" tests="3" failures="0"><testcase name="a"/></testsuite></testsuites>')
    assert not r["all_passed"] and "tests=3, counted 1" in r["attribute_mismatch"][0]
    err = parse_tcunit_xml('<testsuites><testsuite name="z"><testcase name="a"><error message="exc"/></testcase></testsuite></testsuites>')
    assert err["summary"]["errors"] == 1 and not err["all_passed"]
    ok = parse_tcunit_xml('<testsuites><testsuite name="w" tests="1" failures="0"><testcase name="a"/></testsuite></testsuites>')
    assert ok["all_passed"] and "attribute_mismatch" not in ok


def test_unknown_root(tmp_path):
    p = ProjectIndex(ProjectConfig(root=str(tmp_path / "nope")))
    with pytest.raises(ProjectError):
        p.scan()


def test_variables_flatten(project_dir):
    p = idx(project_dir)
    r = p.variables()
    paths = {it["path"]: it for it in r["items"]}
    assert "MAIN.fbFill.fbValve.bOpen" in paths and paths["MAIN.fbFill.fbValve.bOpen"]["declared_in"] == "FB_Valve"
    assert "MAIN.fbFill.stRecipe.rBulkCutoff" in paths and "MAIN.stRecipe.sName" in paths
    assert "MAIN.fbFill.arrWeights[9]" in paths and "MAIN.fbFill.arrWeights[10]" not in paths
    assert paths["GVL_Machine.bEStop"]["at"] == "%I*"
    assert "MAIN.fbFill.eState" in paths and paths["MAIN.fbFill.eState"]["type"] == "E_FillState"  # unknown type stays scalar
    assert set(r["roots"]) == {"MAIN", "GVL_Machine"}
    small = p.variables(max_array=4)
    assert any(it["path"] == "MAIN.fbFill.arrWeights[*]" for it in small["items"])
    one = p.variables("FB_Valve")
    assert [it["path"] for it in one["items"]] == ["FB_Valve.bOpen", "FB_Valve.tOpened"]


def test_incremental_refresh_and_git_changes(project_dir):
    import subprocess, time as _t
    p = idx(project_dir)
    f = project_dir / "Plc" / "POUs" / "FB_Valve.TcPOU"
    # incremental: touch one file, add one, remove one
    _t.sleep(0.01)
    f.write_text(f.read_text().replace("tOpened : TIME;", "tOpened : TIME;\n\ttClosed : TIME;"))
    new = project_dir / "Plc" / "POUs" / "FB_New.TcPOU"
    new.write_text((project_dir / "Plc" / "POUs" / "FB_Valve.TcPOU").read_text().replace('Name="FB_Valve"', 'Name="FB_New"').replace("FUNCTION_BLOCK FB_Valve", "FUNCTION_BLOCK FB_New"))
    (project_dir / "Plc" / "DUTs" / "ST_Recipe.TcDUT").unlink()
    r = p.refresh()
    assert f.as_posix() in [Path(x).as_posix() for x in r["changed"]] and len(r["added"]) == 1 and len(r["removed"]) == 1
    assert any(v.name == "tClosed" for v in p.get_unit("FB_Valve").vars)
    assert p.get_unit("FB_New").kind == "FUNCTION_BLOCK"
    with pytest.raises(ProjectError):
        p.get_unit("ST_Recipe")
    # git: init, commit, modify → changed_units
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], cwd=project_dir, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base"], cwd=project_dir, check=True)
    assert p.changed_units()["units"] == []
    (project_dir / "Plc" / "POUs" / "MAIN.TcPOU").write_text((project_dir / "Plc" / "POUs" / "MAIN.TcPOU").read_text().replace("nCycle + 1", "nCycle + 2"))
    (project_dir / "Plc" / "POUs" / "FB_Extra.TcPOU").write_text(new.read_text().replace("FB_New", "FB_Extra"))
    p.refresh()
    ch = p.changed_units()
    st = {u["name"]: u["status"] for u in ch["units"]}
    assert st == {"MAIN": "modified", "FB_Extra": "untracked"}


def test_read_file_confined_to_project(project_dir, tmp_path):
    idx = ProjectIndex(ProjectConfig(root=str(project_dir)))
    idx.scan()
    assert "MAIN" in idx.read_file("Plc/POUs/MAIN.TcPOU")["text"]
    outside = tmp_path / "secret.ini"
    outside.write_text("[x]\nkey=1\n")
    with pytest.raises(ProjectError, match="outside the project"):
        idx.read_file(str(outside))
    with pytest.raises(ProjectError, match="outside the project"):
        idx.read_file("../secret.ini")
    # a symlink pointing out of the tree is followed and refused
    link = project_dir / "Plc" / "escape.ini"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    with pytest.raises(ProjectError, match="outside the project"):
        idx.read_file("Plc/escape.ini")
