from pathlib import Path

import pytest

from tests.conftest import register_sources

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
    register_sources(project_dir / "Plc" / "Plc.plcproj")              # N106: added to the project, as XAE does
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
    register_sources(project_dir / "Plc" / "Plc.plcproj")
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


def test_n88_parallel_first_calls_see_one_complete_index(tmp_path, project_dir):
    """N88: two ads_dump calls started together — one said 'no project configured' (alphabetical order): both scanned,
    the second clearing the units while the first read them. One scan, read under the lock."""
    import threading
    from twincat_mcp.config import ProjectConfig
    from twincat_mcp.project_backend import ProjectIndex
    idx = ProjectIndex(ProjectConfig(root=str(project_dir)))
    scans = []
    real = idx._scan
    idx._scan = lambda root=None: (scans.append(1), real(root))[1]
    got: list[set] = []

    def call():
        idx.ensure_fresh()
        got.append(idx.unit_names("PROGRAM", "GVL"))
    ts = [threading.Thread(target=call) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert len(scans) == 1 and len(got) == 6 and all(g == got[0] and g for g in got), (len(scans), got)


def test_n88_readers_never_see_an_index_being_rebuilt(tmp_path, project_dir):
    """N88 root: a scan cleared the published dict and re-filled it; refreshes changed it in place. A reader outside
    the lock (codecheck, ads_dump, every proj_* tool) saw it empty, partial, or 'changed size during iteration'.
    Scans and refreshes now build new containers and publish them whole."""
    import os
    import threading
    import time as _t
    from twincat_mcp.config import ProjectConfig
    from twincat_mcp.project_backend import ProjectIndex
    idx = ProjectIndex(ProjectConfig(root=str(project_dir)))
    full = idx.scan()["units"]
    src = next(p for p in project_dir.rglob("*.TcPOU"))
    stop = threading.Event()
    bad: list[str] = []

    def rescan():
        n = 0
        while not stop.is_set():
            idx.scan() if n % 2 else (os.utime(src, (_t.time(), _t.time() + n)), idx.refresh())
            n += 1

    def read():
        while not stop.is_set():
            try:
                k = sum(1 for _ in idx.units.values())
            except RuntimeError as exc:
                bad.append(str(exc))
                return
            if k != full:
                bad.append(f"{k} of {full} units")
                return
    ts = [threading.Thread(target=rescan)] + [threading.Thread(target=read) for _ in range(3)]
    for t in ts:
        t.start()
    _t.sleep(1.5)
    stop.set()
    for t in ts:
        t.join()
    assert not bad, bad[:3]


def _obj(path, tag: str, name: str, decl: str, impl: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"<Implementation><ST><![CDATA[{impl}]]></ST></Implementation>" if impl is not None else ""
    path.write_text('<?xml version="1.0" encoding="utf-8"?>\n<TcPlcObject Version="1.1.0.1">\n'
                    f'  <{tag} Name="{name}" Id="{{aaaaaaaa-0000-0000-0000-0000000000{len(name):02d}}}">'
                    f"<Declaration><![CDATA[{decl}]]></Declaration>{body}</{tag}>\n</TcPlcObject>\n", encoding="utf-8")


def test_n92_review_strings_projects_and_duplicates(tmp_path, project_dir):
    """Review of N92: (1) the comment pass cut code at a // inside a literal, so a GVL used as
    CONCAT('//srv/share/', GVL_Paths.sShare) read as unreferenced; (2) with two PLC projects every root of the other
    one read as 'not linked' on this port; (3) a name declared twice is never called missing."""
    from twincat_mcp.config import ProjectConfig
    from twincat_mcp.project_backend import ProjectIndex, not_on_target, st_strip
    plc = project_dir / "Plc"
    _obj(plc / "GVLs" / "GVL_Paths.TcGVL", "GVL", "GVL_Paths", "VAR_GLOBAL\n\tsShare : STRING;\nEND_VAR\n")
    _obj(plc / "POUs" / "PRG_Paths.TcPOU", "POU", "PRG_Paths", "PROGRAM PRG_Paths\nVAR\n\tsPath : STRING;\nEND_VAR\n",
         "sPath := CONCAT('//srv/share/', GVL_Paths.sShare); (* a ' in a comment *) sPath := '(*';")
    two = project_dir / "Plc2"
    (two / "Plc2.plcproj").parent.mkdir(parents=True, exist_ok=True)
    (two / "Plc2.plcproj").write_text((plc / "Plc.plcproj").read_text(encoding="utf-8"), encoding="utf-8")
    _obj(two / "POUs" / "MAIN2.TcPOU", "POU", "MAIN2", "PROGRAM MAIN2\nVAR\n\tn : INT;\nEND_VAR\n", "n := n + 1;")
    _obj(two / "GVLs" / "GVL_Two.TcGVL", "GVL", "GVL_Two", "VAR_GLOBAL\n\tnX : INT;\nEND_VAR\n")
    register_sources(plc / "Plc.plcproj")
    register_sources(two / "Plc2.plcproj")
    idx = ProjectIndex(ProjectConfig(root=str(project_dir)))
    assert st_strip("a := '//x'; // c\nb := '(*'; (* ' *) c") == "a := '//x';  \nb := '(*';   c"
    assert st_strip("s := 'it''s'; t := '$'x'", blank_strings=True) == "s := ''''; t := ''"
    ul = idx.unreferenced_gvls()
    assert [x["gvl"] for x in ul["not_linked"]] == ["GVL_Two"], ul        # GVL_Paths: used inside the CONCAT line
    decl = idx.declared_roots()
    # the target on this port runs Plc (MAIN, GVL_Machine, PRG_Paths, GVL_Paths): Plc2's roots are not "missing"
    r = not_on_target(decl, {"main", "gvl_machine", "prg_paths", "gvl_paths"})
    assert r["roots"] == [] and r["plc_project"].endswith("Plc.plcproj") and r["not_judged"] is None, r
    # a target whose symbols match both projects (or neither) is not judged at all
    r2 = not_on_target(decl, {"main", "main2"})
    assert r2["roots"] == [] and "2 PLC projects" in r2["not_judged"], r2
    # Plc2 on its own port: GVL_Two is missing there
    r3 = not_on_target(decl, {"main2"})
    assert [m["root"] for m in r3["roots"]] == ["GVL_Two"] and r3["plc_project"].endswith("Plc2.plcproj")
    # a duplicate name (two files) is never called missing
    _obj(two / "GVLs" / "GVL_Machine.TcGVL", "GVL", "GVL_Machine", "VAR_GLOBAL\n\tnY : INT;\nEND_VAR\n")
    register_sources(two / "Plc2.plcproj")
    idx.refresh()
    r4 = not_on_target(idx.declared_roots(), {"main2"})
    assert "GVL_Machine" not in [m["root"] for m in r4["roots"]], r4



def test_n106_the_project_is_what_its_plcproj_compiles(project_dir):
    """N106 (0.8.11 retest): a backup copy of a POU under the root (retest_plcopen\\before_FB_RetestX.TcPOU) was a
    unit — the call graph, the overview and lint showed it beside the real one. Only the .plcproj's compile items are
    units; the others are named (not_in_project). Added to the project (the .plcproj saved), a file is a unit."""
    backup = project_dir / "retest_plcopen" / "before_FB_Valve.TcPOU"
    backup.parent.mkdir()
    backup.write_text((project_dir / "Plc" / "POUs" / "FB_Valve.TcPOU").read_text().replace('Name="FB_Valve"', 'Name="FB_ValveOld"')
                      .replace("FUNCTION_BLOCK FB_Valve", "FUNCTION_BLOCK FB_ValveOld"))
    stray = project_dir / "Plc" / "POUs" / "Copy of MAIN.TcPOU"
    stray.write_text((project_dir / "Plc" / "POUs" / "MAIN.TcPOU").read_text())
    p = ProjectIndex(ProjectConfig(root=str(project_dir)))
    r = p.scan()
    assert "fb_valveold" not in p.units and p.duplicates == {}, (p.duplicates, list(p.units))
    assert r["not_in_project"]["count"] == 2 and str(backup) in r["not_in_project"]["files"]
    assert not p.is_member(backup) and p.is_member(project_dir / "Plc" / "POUs" / "MAIN.TcPOU")
    # added to the PLC project in XAE (the .plcproj lists it): a unit, found by the next refresh
    pp = project_dir / "Plc" / "Plc.plcproj"
    pp.write_text(pp.read_text().replace("</ItemGroup>", '<Compile Include="..\\retest_plcopen\\before_FB_Valve.TcPOU"/>'
                                         "</ItemGroup>", 1))
    d = p.refresh()
    assert d["rescanned"] and str(backup) in d["added"] and p.get_unit("FB_ValveOld").kind == "FUNCTION_BLOCK"
    assert [str(stray)] == p.not_in_project
    # a folder of loose sources (no .plcproj at all): every file is a unit
    loose = project_dir.parent / "loose"
    loose.mkdir()
    (loose / "FB_X.TcPOU").write_text(backup.read_text().replace("FB_ValveOld", "FB_X"))
    q = ProjectIndex(ProjectConfig(root=str(loose)))
    assert q.scan()["units"] >= 1 and q.get_unit("FB_X") and "not_in_project" not in q.scan()


# ------------------------------------------------------------------ N113 / N114: PLC projects named by the .tsproj
def _plc(root, name, impl, cycle_us, prog="MAIN"):
    d = root / name
    (d / "POUs").mkdir(parents=True)
    (d / f"{name}.plcproj").write_text('<?xml version="1.0" encoding="utf-8"?><Project><PropertyGroup><Name>'
                                       f'{name}</Name></PropertyGroup><ItemGroup /></Project>', encoding="utf-8")
    (d / "POUs" / f"{prog}.TcPOU").write_text(
        f'<?xml version="1.0" encoding="utf-8"?><TcPlcObject><POU Name="{prog}"><Declaration><![CDATA[PROGRAM {prog}\n'
        f'VAR\n  n : INT;\nEND_VAR]]></Declaration><Implementation><ST><![CDATA[{impl}]]></ST></Implementation></POU>'
        '</TcPlcObject>', encoding="utf-8")
    (d / "PlcTask.TcTTO").write_text(
        f'<?xml version="1.0" encoding="utf-8"?><TcPlcObject><Task Name="PlcTask"><CycleTime>{cycle_us}</CycleTime>'
        f'<PouCall><Name>{prog}</Name></PouCall></Task></TcPlcObject>', encoding="utf-8")
    register_sources(d / f"{name}.plcproj")
    return d


def _tsproj(root, *plcs, xti=()):
    """plcs: (name, port) named inline; xti: (name, port) in _Config\\PLC\\<name>.xti (independent project files)."""
    inline = "".join(f'<Project GUID="{{0000000{i}-0000-0000-0000-000000000000}}" Name="{n}" '
                     f'PrjFilePath="{n}\\{n}.plcproj" TmcFilePath="{n}\\{n}.tmc" AmsPort="{port}"/>'
                     for i, (n, port) in enumerate(plcs))
    files = "".join(f'<Project File="{n}.xti"/>' for n, _ in xti)
    for n, port in xti:
        (root / "_Config" / "PLC").mkdir(parents=True, exist_ok=True)
        (root / "_Config" / "PLC" / f"{n}.xti").write_text(
            '<?xml version="1.0"?><TcSmItem ClassName="CNestedPlcProjDef"><Project GUID="{1}" '
            f'Name="{n}" PrjFilePath="..\\..\\{n}\\{n}.plcproj" AmsPort="{port}"/></TcSmItem>', encoding="utf-8")
    (root / "Machine.tsproj").write_text(
        '<?xml version="1.0"?><TcSmProject TcVersion="3.1.4026.14"><Project><System/>'
        f'<Plc>{inline}{files}</Plc></Project></TcSmProject>', encoding="utf-8")


def test_n113_two_plc_projects_with_main_each_keep_their_own_unit(tmp_path):
    """N113: units were keyed by name only — MAIN of Plc2 replaced MAIN of Plc1, so an edit of one was compared with the
    other's file. Every file's units are kept; a tree path is looked up in the PLC project it names (the .tsproj)."""
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _plc(tmp_path, "Plc2", "n := n + 2;", 2000)
    _tsproj(tmp_path, ("Plc1", 851), ("Plc2", 852))
    p = idx(tmp_path)
    r = p.scan()
    assert sorted((x["tree_name"], x["ams_port"]) for x in r["plcproj"]) == [("Plc1", 851), ("Plc2", 852)]
    assert "main" in p.duplicates
    u1, why1 = p.unit_at_tree_path("TIPC^Plc1^Plc1 Project^POUs^MAIN")
    u2, why2 = p.unit_at_tree_path("TIPC^Plc2^Plc2 Project^POUs^MAIN")
    assert why1 is None and why2 is None
    assert "Plc1" in u1.file and "n + 1" in u1.implementation
    assert "Plc2" in u2.file and "n + 2" in u2.implementation
    u, why = p.unit_at_tree_path("TIPC^Plc9^Plc9 Project^POUs^MAIN")
    assert u is None and "declared in several files" in why and "'Plc9' is not one a .tsproj" in why, why
    # the pinned lookup finds the pinned file's unit without re-indexing (the name -> unit view holds one of them)
    assert "n + 1" in p.get_unit("Plc1@MAIN").implementation and "n + 2" in p.get_unit("Plc2@MAIN").implementation
    with pytest.raises(ProjectError, match="exists in several files"):
        p.get_unit("MAIN")


def test_n114_task_of_main_follows_the_port_to_its_plc_project(tmp_path):
    """N114: with MAIN in two PLC projects the task check said 'task configuration not available'. The ADS port the
    recording reads names the PLC project (AmsPort in the .tsproj)."""
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _plc(tmp_path, "Plc2", "n := n + 2;", 2000)
    _tsproj(tmp_path, ("Plc1", 851), ("Plc2", 852))
    p = idx(tmp_path)
    assert [(t["name"], t["cycle_ms"]) for t in p.tasks_of("MAIN", 852)] == [("PlcTask", 2.0)]
    assert [(t["name"], t["cycle_ms"]) for t in p.tasks_of("MAIN", 851)] == [("PlcTask", 10.0)]
    assert p.tasks_of("MAIN") is None                       # no port: which MAIN is not known
    assert p.tasks_of("MAIN", 853) is None                  # a port no .tsproj names: not known either


def test_n114_a_plc_project_removed_from_the_tree_is_not_part_of_the_project(tmp_path):
    """N114 (corrected): after Plc2 was removed from the tree its folder stayed on disk with MAIN.TcPOU; MAIN counted
    twice and the task check of the ONE remaining PLC said 'task configuration not available'."""
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _plc(tmp_path, "Plc2", "n := n + 2;", 2000)
    _tsproj(tmp_path, ("Plc1", 851), ("Plc2", 852))
    p = idx(tmp_path)
    assert p.tasks_of("MAIN") is None
    _tsproj(tmp_path, ("Plc1", 851))                        # Plc2 removed from the tree: only the .tsproj changes
    p.REFRESH_MIN_S = 0
    p.refresh()
    assert p.duplicates == {}, p.duplicates
    assert [x["file"] for x in p.plcproj] == [str(tmp_path / "Plc1" / "Plc1.plcproj")]
    assert [x["file"] for x in p.plcproj_not_in_project] == [str(tmp_path / "Plc2" / "Plc2.plcproj")]
    assert str(tmp_path / "Plc2" / "POUs" / "MAIN.TcPOU") in p.not_in_project
    assert [(t["name"], t["cycle_ms"]) for t in p.tasks_of("MAIN")] == [("PlcTask", 10.0)]
    assert [t["cycle_ms"] for t in p.task_config().values()] == [10.0]    # Plc2's task is no task of the project
    note = p.outside_note()
    assert note["plcproj_not_in_project"][0]["why"].startswith("no .tsproj under the root names this PLC project")


def test_n114_independent_project_files_name_the_plc_projects(tmp_path):
    """The .tsproj may keep a PLC project in an independent file (<Project File="Plc1.xti"/>, _Config\\PLC\\Plc1.xti,
    PrjFilePath relative to the .xti)."""
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _plc(tmp_path, "Old", "n := n + 2;", 2000)
    _tsproj(tmp_path, xti=[("Plc1", 851)])
    p = idx(tmp_path)
    assert [(x["tree_name"], x["ams_port"]) for x in p.plcproj] == [("Plc1", 851)]
    assert p.plc_project(port=851) == str(tmp_path / "Plc1" / "Plc1.plcproj")
    assert [x["file"] for x in p.plcproj_not_in_project] == [str(tmp_path / "Old" / "Old.plcproj")]


def test_n114_unknown_tsproj_contents_keep_every_plc_project(tmp_path):
    """A .tsproj that names no .plcproj that exists (another format, a broken path) cannot tell which PLC projects
    belong to the project: every .plcproj counts and the scan says why — nothing is dropped on a guess."""
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    (tmp_path / "Machine.tsproj").write_text('<?xml version="1.0"?><TcSmProject><Project><Plc><Project Name="X" '
                                             'PrjFilePath="Gone\\Gone.plcproj" AmsPort="851"/></Plc></Project>'
                                             '</TcSmProject>', encoding="utf-8")
    p = idx(tmp_path)
    r = p.scan()
    assert len(p.plcproj) == 1 and p.plcproj_not_in_project == []
    assert "every .plcproj counts" in r["plc_projects_note"]
    assert r["plc_projects_not_found"][0]["name"] == "X" and "does not exist" in r["plc_projects_not_found"][0]["why"]
    (tmp_path / "Machine.tsproj").write_text("<TcSmProject><Plc>", encoding="utf-8")       # unreadable
    r = p.scan()
    assert len(p.plcproj) == 1 and "cannot be read" in r["plc_projects_note"]


def _plc_with_action(root, name, impl, cycle_us, var_type="INT"):
    d = _plc(root, name, impl, cycle_us)
    main = d / "POUs" / "MAIN.TcPOU"
    main.write_text(main.read_text(encoding="utf-8").replace(
        "  n : INT;", f"  n : INT;\n  x : {var_type};").replace(
        "</POU>", f'<Action Name="Act"><Implementation><ST><![CDATA[n := 0; // {name}]]></ST></Implementation></Action></POU>'),
        encoding="utf-8")
    return d


def test_n113_review_readers_never_pick_one_of_two_silently(tmp_path):
    """Review of N113: lookups were per PLC project, but lists, searches, symbol resolution and sub-items still read the
    name -> unit view, where MAIN of one PLC project hides the other's. Lists and searches show both; resolution is
    done in the PLC project of the ADS port; an ambiguous name (MAIN.Act, 'Act') is refused, for writes too."""
    for n, port in (("Plc1", 851), ("Plc2", 852)):
        d = _plc_with_action(tmp_path, n, "n := n + 1;", 10000, var_type=f"ST_{n}")
        (d / "DUTs").mkdir()
        (d / "DUTs" / f"ST_{n}.TcDUT").write_text(
            f'<?xml version="1.0" encoding="utf-8"?><TcPlcObject><DUT Name="ST_{n}"><Declaration><![CDATA[TYPE ST_{n} :\n'
            f'STRUCT\n  v{n} : INT;\nEND_STRUCT\nEND_TYPE]]></Declaration></DUT></TcPlcObject>', encoding="utf-8")
        register_sources(d / f"{n}.plcproj")
    _tsproj(tmp_path, ("Plc1", 851), ("Plc2", 852))
    p = idx(tmp_path)
    assert "main.act" in p.duplicates
    assert len(p.list_units("PROGRAM")["units"]) == 2
    assert {h["file"] for h in p.search(r"// Plc")["hits"]} == {str(tmp_path / n / "POUs" / "MAIN.TcPOU") for n in ("Plc1", "Plc2")}
    with pytest.raises(ProjectError, match="exists in several files"):
        p.get_unit("MAIN.Act")
    with pytest.raises(ProjectError, match="matches 2 objects"):
        p.get_unit("Act")
    assert "Plc2" in p.get_unit("Plc2@MAIN.Act").implementation
    # resolution in the PLC project of the ADS port
    r = p.resolve_symbol("MAIN.x.vPlc2", 852)
    assert r["resolved"] and r["hops"][1]["type"] == "ST_Plc2" and r["hops"][2]["declared_in"] == "ST_Plc2", r
    assert p.resolve_symbol("MAIN.x.vPlc1", 851)["resolved"]
    with pytest.raises(ProjectError, match="several PLC projects .* no ADS port was given"):
        p.resolve_symbol("MAIN.x")
    v = p.variables(port=852)
    assert {it["path"] for it in v["items"]} == {"MAIN.n", "MAIN.x.vPlc2"} and "note" not in v
    v = p.variables()
    assert {"MAIN.x.vPlc1", "MAIN.x.vPlc2"} <= {it["path"] for it in v["items"]} and "several PLC projects" in v["note"]
    assert v["ambiguous_roots"] == ["MAIN"]
    # the file -> PLC project memo is dropped with a new published index
    c1 = p._cache()
    p.scan()
    assert p._cache() is not c1


def test_n113_review_an_unresolved_tsproj_reference_keeps_every_plc_project(tmp_path):
    """Review: Plc1 resolved and Plc2's .xti missing — Plc2.plcproj on disk may be the one the .tsproj names; it was
    dropped as 'removed from the tree'. Every .plcproj counts, and the scan says which reference was not found."""
    _plc(tmp_path, "Plc1", "n := n + 1;", 10000)
    _plc(tmp_path, "Plc2", "n := n + 2;", 2000)
    _tsproj(tmp_path, ("Plc1", 851))
    ts = (tmp_path / "Machine.tsproj").read_text(encoding="utf-8").replace("</Plc>", '<Project File="Plc2.xti"/></Plc>')
    (tmp_path / "Machine.tsproj").write_text(ts, encoding="utf-8")
    p = idx(tmp_path)
    r = p.scan()
    assert len(p.plcproj) == 2 and p.plcproj_not_in_project == []
    assert "could not be found" in r["plc_projects_note"] and r["plc_projects_not_found"][0]["unresolved"] is True
