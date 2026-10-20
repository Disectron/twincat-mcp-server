"""Knowledge base (decisions of 2026-10-17): location chosen by the user under stated conditions, known errors,
library versions from the .plcproj, ESI hardware lookup, pattern search over user-added folders only."""
import json
from pathlib import Path

import pytest

from twincat_mcp import knowledge as K

ESI = """<?xml version="1.0" encoding="utf-8"?>
<EtherCATInfo>
  <Vendor><Id>#x00000002</Id><Name>Beckhoff Automation GmbH &amp; Co. KG</Name></Vendor>
  <Descriptions><Devices>
    <Device Physics="YY">
      <Type ProductCode="#x07d83052" RevisionNo="#x00100000">EL2008</Type>
      <Name LcId="1033">EL2008 8Ch. Dig. Output 24V, 0.5A</Name>
      <GroupType>DigOut</GroupType>
      <RxPdo Fixed="1" Mandatory="1" Sm="0"><Index>#x1600</Index><Name>Channel 1</Name>
        <Entry><Index>#x7000</Index><SubIndex>1</SubIndex><BitLen>1</BitLen><Name>Output</Name><DataType>BOOL</DataType></Entry>
      </RxPdo>
      <RxPdo Fixed="1" Sm="0"><Index>#x1601</Index><Name>Channel 2</Name>
        <Entry><Index>#x0</Index><BitLen>7</BitLen></Entry>
        <Entry><Index>#x7010</Index><SubIndex>1</SubIndex><BitLen>1</BitLen><Name>Output</Name><DataType>BOOL</DataType></Entry>
      </RxPdo>
      <Profile><Dictionary><Objects>
        <Object><Index>#x1018</Index><Name>Identity</Name><Type>DT1018</Type></Object>
        <Object><Index>#x7000</Index><Name>DOX Outputs Ch.1</Name><Type>DT7000</Type></Object>
      </Objects></Dictionary></Profile>
    </Device>
    <Device Physics="YY">
      <Type ProductCode="#x0bf03052" RevisionNo="#x00110000">EL3062</Type>
      <Name LcId="1033">EL3062 2Ch. Ana. Input 0-10V</Name>
      <TxPdo Sm="3"><Index>#x1A00</Index><Name>AI Standard Channel 1</Name>
        <Entry><Index>#x6000</Index><SubIndex>17</SubIndex><BitLen>16</BitLen><Name>Value</Name><DataType>INT</DataType></Entry>
      </TxPdo>
    </Device>
  </Devices></Descriptions>
</EtherCATInfo>
"""


@pytest.fixture
def kb(tmp_path):
    esi = tmp_path / "esi"
    esi.mkdir()
    (esi / "Beckhoff EL2xxx.xml").write_text(ESI, encoding="utf-8")
    libs = tmp_path / "libs" / "Beckhoff Automation GmbH"
    for lib, vers in (("Tc2_Standard", ["3.3.3.0", "3.4.5.0"]), ("Tc2_Smtp", ["3.3.4.0"])):
        for v in vers:
            (libs / lib / v).mkdir(parents=True)
    (tmp_path / "libs" / "System" / "VisuElems" / "3.5.13.40").mkdir(parents=True)
    k = K.Knowledge(tmp_path / "home", None, [str(esi)], [str(tmp_path / "libs")])
    return k, tmp_path


def test_known_errors_annotate_without_repeating():
    m = K.annotate("ADSError 1808 (0x710): symbol not found")
    assert "known cause: the symbol is not in the runtime" in m and "ads_list_symbols" in m
    already = "XAE refused the COM call … answer it with xae_dialog_resolve(button=...)"
    assert K.annotate("ExecuteCommand: (HRESULT 0x80010001) " + already).count("xae_dialog_resolve") == 1
    assert K.known_error("Unknown error 2551252772 checking TwinCAT Licenses")["fix"].startswith("XAE → SYSTEM → License")
    assert K.known_error("everything fine") is None


def test_location_must_be_chosen_and_checked(kb, monkeypatch):
    k, tmp = kb
    st = k.status()
    assert st["location"] is None and "ASK THE USER" in st["action"] and "local fixed drive" in st["action"]
    with pytest.raises(K.KnowledgeError, match="ASK THE USER"):
        k.search("valve")
    # conditions: synced folder, plugin folder, relative path
    assert any("cloud-synced" in p for p in K.check_location(tmp / "OneDrive" / "kb"))
    assert any("plugin/install folder" in p for p in K.check_location(Path(K.__file__).resolve().parents[1] / "kbdata"))
    assert K.check_location("relative/kb") == ["'relative/kb' is not an absolute path"]
    with pytest.raises(K.KnowledgeError, match="cannot hold the knowledge base: it is inside a cloud-synced folder"):
        k.set_location(str(tmp / "Dropbox" / "kb"))
    if not Path("D:\\").exists():
        with pytest.raises(K.KnowledgeError, match="no D: drive"):
            k.set_location(None, use_fallback=True)
    r = k.set_location(str(tmp / "kbdata"))
    assert Path(r["database"]).exists() and k.status()["source"].startswith("chosen")
    # a second choice never moves or deletes the first database
    r2 = k.set_location(str(tmp / "kbdata2"))
    assert "previous database stays" in r2["note"] and (tmp / "kbdata" / "index" / "knowledge.db").exists()


def test_pattern_search_only_over_added_projects(kb, project_dir):
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    s = k.search("valve")
    assert s["hits"] == [] and "kb_add_project" in s["note"]
    r = k.add_project(str(project_dir), "sample")
    assert r["reindexed"][0]["units"] >= 3
    hits = k.search("FB_Valve")["hits"]
    assert hits and hits[0]["unit"].startswith("FB_Valve") and hits[0]["project"] == str(project_dir)
    assert all(h["kind"] == "PROGRAM" for h in k.search("nCycle", kinds=["PROGRAM"])["hits"])
    assert k.search("ADSError 1861 timeout elapsed")["known_error"]["cause"].startswith("the target did not answer")
    assert k.reindex()["reindexed"][0]["unchanged"]
    assert k.remove_project(str(project_dir))["units_removed"] >= 3 and k.search("FB_Valve")["hits"] == []
    with pytest.raises(K.KnowledgeError):
        k.add_project(str(tmp / "nope"))


def test_hw_lookup_from_esi(kb):
    """0.8.12: hw_lookup reads index\\esi.db (esi_index); the first build starts by itself in the server — here, a
    lookup before it says so and builds nothing."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    r = k.hw_lookup("EL2008")
    assert r["esi_index"]["state"] == "missing" and "first build" in r["note"]
    assert k.esi_build()["devices"] == 2
    r = k.hw_lookup("EL2008")
    d = r["devices"][0]
    assert d["product_code"] == "#x07D83052" and d["group"] == "DigOut" and "state_note" not in r["source"]
    rx = [p for p in d["pdo"] if p["index"] == "#x1601"][0]
    assert [(e["index"], e.get("gap", False), e["bits"]) for e in rx["entries"]] == [("#x0000", True, 7),
                                                                                  ("#x7010", False, 1)]
    assert d["coe_objects"][0] == {"index": "#x1018", "name": "Identity", "type": "DT1018"}
    assert k.hw_lookup("#x0bf03052")["devices"][0]["type"] == "EL3062"
    assert k.hw_lookup("Ana. Input")["types"] == ["EL3062"]
    with pytest.raises(K.KnowledgeError, match="empty query"):
        k.hw_lookup(" ")


def test_library_versions(kb, project_dir):
    k, tmp = kb
    pp = project_dir / "Plc" / "Plc.plcproj"
    pp.write_text(pp.read_text().replace(
        '<PlaceholderReference Include="Tc2_Standard">',
        '<PlaceholderReference Include="Tc2_Smtp"><DefaultResolution>Tc2_Smtp, 3.3.1.0 (Beckhoff Automation GmbH)</DefaultResolution></PlaceholderReference>'
        '<PlaceholderReference Include="VisuElems"><DefaultResolution>VisuElems, 4.10.0.0 (System)</DefaultResolution></PlaceholderReference>'
        '<PlaceholderReference Include="Tc2_Standard">'))
    rep = K.library_report([str(pp)], k.installed_libraries())
    libs = {x["library"]: x for x in rep["projects"][0]["libraries"]}
    assert libs["Tc2_Standard"]["requested"] == "*" and libs["Tc2_Standard"]["effective"] == "3.4.5.0"
    assert libs["Tc2_Standard"]["installed"] == ["3.3.3.0", "3.4.5.0"]
    assert "not installed on this PC" in libs["Tc2_Smtp"]["warning"]
    # N19: a (System) placeholder is resolved by the placeholder profile — information, not a build failure
    assert "warning" not in libs["VisuElems"] and "placeholder profile" in libs["VisuElems"]["info"]
    # N19b: not null — the only installed version, or 'profile' when several could be chosen
    inst = libs["VisuElems"]["installed"]
    assert libs["VisuElems"]["effective"] == (inst[0] if len(inst) == 1 else "profile")


def test_d38_library_repository_of_4026_is_scanned(kb, tmp_path, monkeypatch):
    r"""D38: 4026 installs libraries to ProgramData\Beckhoff\TwinCAT\PlcEngineering\Managed Libraries."""
    k, _ = kb
    pd = tmp_path / "pd"
    (pd / "Beckhoff" / "TwinCAT" / "PlcEngineering" / "Managed Libraries" / "www.tcunit.org" / "TcUnit" / "1.3.1").mkdir(parents=True)
    monkeypatch.setenv("ProgramData", str(pd))
    inst = k.installed_libraries()
    assert inst["tcunit"]["versions"] == ["1.3.1"] and inst["tcunit"]["vendor"] == "www.tcunit.org"


def test_n17_ads_error_6_explained_from_port_and_mode():
    assert "CONFIG" in K.ads6_context(851, "CONFIG")["cause"] and "ads_system_mode run" in K.ads6_context(851, "CONFIG")["fix"]
    assert "852" not in K.ads6_context(851, "RUN")["cause"] and "no PLC project is activated" in K.ads6_context(851, "RUN")["cause"]
    assert "851 is the first" in K.ads6_context(852, "RUN")["cause"]
    assert "ads_ping" in K.ads6_context(851, None)["fix"]


def test_d19_esi_namespaced_pdos_entries_and_coe():
    """D19: an ESI with a default namespace (and German names first) still yields PDOs, entries and CoE objects."""
    from twincat_mcp import esi_index as E
    rows = list(E.iter_info(Path(__file__).parent / "fixtures" / "esi_namespaced.xml"))
    assert [w for w, _r in rows] == ["device"]
    d = rows[0][1]
    assert d["type"] == "EL3102" and d["name"].startswith("EL3102 2Ch.") and d["vendor"].startswith("Beckhoff")
    assert d["group_type"] == "AnaIn" and d["name_de"] == "EL3102 2K. Ana. Eingang"
    p = d["pdos"][0]
    assert p["index"] == "#x1A00" and p["name"] == "AI Standard Channel 1" and p["bits"] == 22
    assert [(e["index"], e["sub"], e["bits"], e.get("gap", False)) for e in p["entries"]] == [
        ("#x6000", 1, 1, False), ("#x0000", 0, 5, True), ("#x6000", 17, 16, False)]
    assert [(o["index"], o["name"], o["type"]) for o in d["dictionary"]] == [(0x8000, "AI Settings Ch.1", "DT8000")]


def test_r16_rpc_size_error_points_at_the_argument_list():
    """R16: ads_call_method with one input too few → ADS 0x705 with a hint about variable writes / arrays."""
    m = K.annotate("RPC call 'P.fb#M' failed: ADSError 1797 (0x705) parameter size not correct (sent 1 input(s) = 4 bytes)")
    assert "VAR_INPUT in declaration order" in m and "write elements by index" not in m
    assert "write elements by index" in K.annotate("write MAIN.a failed: ADSError 1797 (0x705)")


# ---------------------------------------------------------------------------------------------- 0.8.8: R33–R39
def test_r33_whole_plugins_tree_is_refused(tmp_path, monkeypatch):
    """R33: ~/.claude/plugins/data/… was accepted (only plugins/cache was blocked)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for sub in ("cache", "data/twincat-mcp-twincat-mcp/kb", "marketplaces/x"):
        probs = K.check_location(tmp_path / ".claude" / "plugins" / sub)
        assert any("plugin/install folder" in p for p in probs), (sub, probs)
    k = K.Knowledge(tmp_path / "home")
    with pytest.raises(K.KnowledgeError, match="plugin"):
        k.set_location(str(tmp_path / ".claude" / "plugins" / "data" / "kb"))
    assert k.location()[0] is None                                   # nothing was switched


def test_r34_reads_never_write_probe_and_parallel_searches_work(kb, project_dir, monkeypatch):
    """R34: parallel kb_search calls raced on the write probe → 'no longer usable … kb_set_location'."""
    import concurrent.futures as cf
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    k.add_project(str(project_dir), "sample")
    writes: list[str] = []
    real = Path.write_text

    def spy(self, *a, **kw):
        if self.name.startswith(".tcmcp_write_test"):
            writes.append(self.name)
        return real(self, *a, **kw)
    monkeypatch.setattr(Path, "write_text", spy)
    with cf.ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda _i: k.search("FB_Valve"), range(16)))
    assert all(r["hits"] for r in res) and writes == []
    k.status()
    assert writes == []
    k.set_location(str(tmp / "kbdata"))                             # a choice does test writing, with a name of its own
    assert len(writes) == 1 and writes[0] != ".tcmcp_write_test"


def test_r35_one_project_per_folder_any_spelling_and_label(kb, project_dir, monkeypatch):
    """R35: the same folder in another spelling was added twice; reindex/remove/search(project=) needed the exact
    stored string and ignored the label. Windows folds case: simulated with a case-folding normcase."""
    monkeypatch.setattr(K.os.path, "normcase", lambda s: s.lower())
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    first = k.add_project(str(project_dir), "P15")
    n = first["reindexed"][0]["units"]
    again = k.add_project(str(project_dir) + "/", None)
    assert "already in the knowledge base" in again["note"]
    st = k.status()
    assert len(st["projects"]) == 1 and st["projects"][0]["label"] == "P15"
    hits = k.search("FB_Valve")["hits"]
    assert len({(h["unit"], h["file"]) for h in hits}) == len(hits)            # no hit twice
    assert k.search("FB_Valve", project="p15")["hits"]                          # label, any case
    assert k.search("FB_Valve", project=str(project_dir) + "/")["hits"]         # path, trailing separator
    assert k.reindex(str(project_dir) + "/.")["reindexed"][0]["units"] == n
    # a double entry from before 0.8.8 (another spelling stored) is merged by the next add
    with k._db() as con:
        con.execute("INSERT INTO projects(path, label, added) VALUES (?,?,?)", (str(project_dir).upper(), "dup", "x"))
    assert "duplicate_projects" in k.status()
    merged = k.add_project(str(project_dir))
    assert merged["merged_duplicates"] == [str(project_dir).upper()] and len(k.status()["projects"]) == 1
    rm = k.remove_project("P15")
    assert rm["removed"] == [str(project_dir)] and k.status()["projects"] == []


def test_r36_r37_r39_search_semantics_and_validation(kb, project_dir):
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    k.add_project(str(project_dir), "sample")
    # every word first; a word that is only a prefix still finds the unit
    allw = k.search("FB_Valve tOpen")["hits"]
    assert allw and all(h["match"].startswith("all words") for h in allw)
    pre = k.search("FB_Val")["hits"]
    assert pre and pre[0]["match"] == "all words (as prefixes)" and pre[0]["unit"].startswith("FB_Valve")
    some = k.search("FB_Valve zzqqxx_nothing")
    assert some["hits"] and all(h["match"] == "some words" for h in some["hits"]) and "every word" in some["note"]
    # R37: punctuation only
    with pytest.raises(K.KnowledgeError, match="no searchable word"):
        k.search(":=")
    # R39: kinds, project, limit
    with pytest.raises(K.KnowledgeError, match="unknown kind 'BANANA'"):
        k.search("valve", kinds=["BANANA"])
    assert k.search("FB_Valve", kinds=["fb"])["hits"][0]["kind"] == "FUNCTION_BLOCK"
    with pytest.raises(K.KnowledgeError, match="no indexed project is 'NoSuchProject'.*sample"):
        k.search("valve", project="NoSuchProject")
    for bad in (0, -1, 201):
        with pytest.raises(K.KnowledgeError, match="limit must be 1–200"):
            k.search("valve", limit=bad)


def test_r38_set_location_without_arguments_returns_the_current_one(kb):
    k, tmp = kb
    with pytest.raises(K.KnowledgeError, match="ASK THE USER"):
        k.set_location(None)                                         # nothing chosen yet: still the question
    k.set_location(str(tmp / "kbdata"))
    r = k.set_location(None)
    assert r["changed"] is False and r["location"] == str(tmp / "kbdata") and "unchanged" in r["note"]


def _kb_with_docs(tmp: Path, kb_fixture: Path) -> Path:
    """The knowledge folder of tests/fixtures/kb (indexes built by the real code, conftest.kb_fixture) plus a PDF
    not indexed yet, an SOP and an old builder copy in tools\\."""
    import shutil
    loc = tmp / "kbdocs"
    shutil.copytree(kb_fixture, loc, ignore=shutil.ignore_patterns("_package"))
    shutil.rmtree(loc / "manuals")                    # the indexed PDF is not in this folder: reported as gone
    (loc / "manuals" / "IO").mkdir(parents=True)
    (loc / "manuals" / "IO" / "EL3102.pdf").write_bytes(b"%PDF-1.4 not indexed yet")
    (loc / "sop" / "scope").mkdir(parents=True)
    (loc / "sop" / "scope" / "SOP-scope-display.md").write_text("# Scope", encoding="utf-8")
    (loc / "tools").mkdir()
    shipped = K.builders_dir() / "manuals_db.py"              # a copy of the builder the plugin ships, CRLF on disk
    # (the checkout itself is CRLF on Windows: LF first, or the copy would be CR CR LF — a CI failure on Windows only)
    (loc / "tools" / "manuals_db.py").write_bytes(shipped.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    return loc


def test_status_reports_the_offline_documentation_layout(kb, kb_fixture):
    """kb_status names what the twincat-docs skill reads in the same folder (the plugin's builders, run directly or
    through the stubs in tools\\) — and every index in index\\ (docs/KNOWLEDGE_BASE.md)."""
    k, tmp = kb
    loc = _kb_with_docs(tmp, kb_fixture)
    k.set_location(str(loc))
    d = k.status()["docs"]
    assert d["skill"] == "twincat-docs" and str(K.docs_skill_path()) in d["call"] and str(loc) in d["call"]
    assert d["infosys"]["exists"] and d["infosys"]["pages"] == 9 and d["infosys"]["books"] == 6
    assert Path(d["infosys"]["package"]).name == "BKINFOSYS3_FIXTURE_EN-US.mshc" and d["infosys"]["lcid"] == "1033"
    assert d["infosys"]["state"] == "complete" and d["infosys"]["format"] == d["infosys"]["format_expected"]
    assert d["infosys"]["links"] == 2 and d["infosys"]["links_resolved"] == 1 and "command" not in d["infosys"]
    assert Path(d["infosys"]["database"]) == loc / "index" / "infosys-1033.db"
    m = d["manuals"]
    assert m["manuals"] == 1 and m["pdfs_in_folder"] == 1 and m["not_indexed"] == ["EL3102.pdf"]
    assert m["indexed_but_gone"] == ["CX9020.pdf"]                   # the fixture's PDF path is not in this folder
    # the old copy in tools\ became the stub when the folder was chosen: the command runs it from the knowledge folder
    assert m["state"] == "outdated" and m["command"].startswith(f'<Python 3.12+> "{loc / "tools" / "manuals_db.py"}" update --kb')
    assert Path(m["database"]) == loc / "index" / "manuals.db"
    b = d["builders"]
    assert b["infosys_local.py"]["shipped_version"] == "3" and b["infosys_local.py"]["copy_in_tools"] is None
    assert b["manuals_db.py"]["copy_kind"] == "stub" and "nothing to update" in b["manuals_db.py"]["note"]
    assert (loc / "tools" / f"manuals_db.py.v{K._builder_version(K.builders_dir() / 'manuals_db.py')}.bak").is_file()
    assert d["sop"] == [str(Path("sop") / "scope" / "SOP-scope-display.md")]
    # read-only: nothing next to the indexes was created (kb_set_location wrote index\\README.md and knowledge.db)
    assert sorted(p.name for p in (loc / "index").iterdir()) == ["README.md", "infosys-1033.db", "infosys-1033.db.lock",
                                                                 "knowledge.db", "manuals.db", "manuals.db.lock"]
    assert (loc / "index" / "manuals.db.lock").read_text() == ""          # the builds' locks: kept, empty
    assert k.status()["layout"]["index"] == str(loc / "index") and "obsolete_files" not in k.status()["layout"]
    assert K.docs_skill_path().is_file()


def test_set_location_names_the_docs_it_finds(kb, kb_fixture):
    k, tmp = kb
    r = k.set_location(str(tmp / "empty_kb"))
    assert r["docs"]["infosys"]["exists"] is False and r["docs"]["infosys"]["state"] == "missing"
    assert 'infosys_local.py" index --kb' in r["docs"]["infosys"]["command"]
    r2 = k.set_location(str(_kb_with_docs(tmp, kb_fixture)))
    assert r2["docs"]["infosys"]["exists"] and r2["docs"]["manuals"]["exists"]


def test_hw_lookup_adds_the_manuals_that_mention_the_product(kb, kb_fixture):
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    k.esi_build()
    r = k.hw_lookup("EL2008")
    assert "manuals" not in r and "no manuals database" in r["manuals_note"]
    assert str(tmp / "kbdata" / "index" / "manuals.db") in r["manuals_note"]
    k.set_location(str(_kb_with_docs(tmp, kb_fixture)))
    k.esi_build()
    r = k.hw_lookup("EL3102")                                  # not in this ESI, but a product code in the manual
    assert r["matches"] == 0 and r["manuals"]["EL3102"][0]["doc"] == "CX9020.pdf"
    assert r["manuals"]["EL3102"][0]["pages"] == [2, 4]
    assert "no indexed manual mentions EL2008" in k.hw_lookup("EL2008")["manuals_note"]


def test_docs_skill_path_from_the_launcher_not_site_packages(tmp_path, monkeypatch):
    """Review: the plugin server imports twincat_mcp from its venv's site-packages — skills/ is not beside it there."""
    plug = tmp_path / "plugin"
    (plug / "skills" / "twincat-docs").mkdir(parents=True)
    (plug / "skills" / "twincat-docs" / "kb_docs.py").write_text("#", encoding="utf-8")
    monkeypatch.setenv("TCMCP_PLUGIN_SRC", str(plug))
    assert K.docs_skill_path() == plug / "skills" / "twincat-docs" / "kb_docs.py"
    monkeypatch.setenv("TCMCP_PLUGIN_SRC", str(tmp_path / "nothing"))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(K, "_SKILL_REL", Path("no") / "such.py")
    assert K.docs_skill_path() is None
    k = K.Knowledge(tmp_path / "home")
    k.set_location(str(tmp_path / "kb"))
    assert "<twincat-docs skill folder>" in k.docs_layout()["call"]


def test_review_relative_paths_shared_labels_low_space_and_configured_folder(kb, project_dir, tmp_path, monkeypatch):
    k, tmp = kb
    with pytest.raises(K.KnowledgeError, match="not an absolute path"):
        k.set_location("kb")
    k.set_location(str(tmp / "kbdata"))
    with pytest.raises(K.KnowledgeError, match="not an existing absolute folder"):
        k.add_project(".")
    # one label on two folders: remove by label refuses instead of dropping both
    other = tmp / "other" / "proj"
    import shutil as _sh
    _sh.copytree(project_dir, other)
    k.add_project(str(project_dir))
    k.add_project(str(other))
    with pytest.raises(K.KnowledgeError, match="names 2 different folders.*nothing was removed"):
        k.remove_project("proj")
    assert len(k.status()["projects"]) == 2
    # low free space: a warning, the status (and docs) still there
    real = K.shutil.disk_usage
    monkeypatch.setattr(K.shutil, "disk_usage", lambda p: type("U", (), {"free": 10 << 20, "total": 0, "used": 0})())
    st = k.status()
    assert "GB free" in st["warning"] and "docs" in st and len(st["projects"]) == 2
    assert k.search("FB_Valve")["hits"]
    monkeypatch.setattr(K.shutil, "disk_usage", real)
    # a location configured in the TOML that does not exist yet is created on first use
    k2 = K.Knowledge(tmp_path / "home2", str(tmp_path / "configured_kb"))
    assert Path(k2.status()["database"]) == tmp_path / "configured_kb" / "index" / "knowledge.db"
    assert (tmp_path / "configured_kb").is_dir()


def test_ro_uri_quotes_and_handles_posix_paths(tmp_path):
    d = tmp_path / "a b#c"
    d.mkdir()
    u = K.ro_uri(d / "x.db")
    assert u.startswith("file:/") and u.endswith("?mode=ro") and "a%20b%23c" in u and not u.startswith("file://")


def test_a_chosen_folder_that_was_deleted_is_reported_not_recreated(kb):
    import shutil as _sh
    k, tmp = kb
    k.set_location(str(tmp / "kbgone"))
    _sh.rmtree(tmp / "kbgone")
    with pytest.raises(K.KnowledgeError, match="does not exist"):
        k.search("x")
    assert not (tmp / "kbgone").exists()


def test_n34_hw_lookup_takes_the_order_code_out_of_the_esi_type(kb, kb_fixture, monkeypatch):
    """N34: hw_lookup CX9020 looked up the ESI device name 'CX9020 EtherCAT slave' in the manuals (no match)."""
    k, tmp = kb
    (tmp / "esi" / "Beckhoff CX.xml").write_text(
        '<?xml version="1.0"?><EtherCATInfo><Vendor><Id>#x2</Id><Name>Beckhoff</Name></Vendor><Descriptions><Devices>'
        '<Device><Type ProductCode="#x23446052" RevisionNo="#x1">CX9020 EtherCAT slave</Type>'
        '<Name LcId="1033">CX9020 EtherCAT slave</Name></Device></Devices></Descriptions></EtherCATInfo>', encoding="utf-8")
    k.set_location(str(_kb_with_docs(tmp, kb_fixture)))
    k.esi_build()
    r = k.hw_lookup("CX9020")
    assert r["types"] == ["CX9020 EtherCAT slave"]
    assert r["manuals"]["CX9020"][0]["doc"] == "CX9020.pdf" and "manuals_note" not in r
    # N67 (0.8.9 retest): CX9020-0130 found nothing although CX9020.pdf names CX9020 94 times
    m = K.manuals_for(k.location()[0], ["CX9020-0130"])
    assert m["manuals"]["CX9020-0130"][0]["doc"] == "CX9020.pdf"
    assert m["manuals"]["CX9020-0130"][0]["matched_as"].startswith("base product CX9020")


def test_builder_copies_become_stubs_once_and_are_reported(kb, tmp_path, monkeypatch):
    """N64 (0.8.9 retest): a file counted as a plugin copy when its first docstring line matched — the user's own
    14 kB infosys_local.py / manuals_db.py (0.8.8 shipped no builders) were replaced. Now only a file this plugin
    shipped (known sha256, CRLF read as LF) is replaced; everything else is left alone and reported.
    N68: kb_set_location writes the pointer the stubs follow before it installs them."""
    import hashlib
    k, tmp = kb
    loc = tmp / "kbfolder"
    (loc / "tools").mkdir(parents=True)
    own = ('"""Search the locally installed Beckhoff Infosys (Microsoft Help Viewer package) with compact, low-token '
           'output.\n"""\nprint("my own version 1")\n')
    (loc / "tools" / "infosys_local.py").write_text(own, encoding="utf-8")
    old_shipped = b'"""Hardware manual bank: an older release of the builder"""\r\nBUILDER_VERSION = "2"\r\n'
    (loc / "tools" / "manuals_db.py").write_bytes(old_shipped)
    monkeypatch.setitem(K.SHIPPED_BUILDERS, "manuals_db.py",
                        {**K.SHIPPED_BUILDERS["manuals_db.py"],
                         hashlib.sha256(old_shipped.replace(b"\r\n", b"\n")).hexdigest(): "2"})
    assert not (k.home / "plugin_location.json").exists()
    r = k.set_location(str(loc))
    st = r["builder_stubs"]
    assert (k.home / "plugin_location.json").is_file()                   # N68: pointer first
    assert st["infosys_local.py"]["state"] == "left alone" and "not a file this plugin shipped" in st["infosys_local.py"]["reason"]
    assert (loc / "tools" / "infosys_local.py").read_text(encoding="utf-8") == own          # the user's file is untouched
    assert "stub.py" in st["infosys_local.py"]["to_use_the_plugin_builders"]
    assert st["manuals_db.py"]["state"] == "replaced" and st["manuals_db.py"]["was"] == "shipped version 2"
    assert Path(st["manuals_db.py"]["backup"]).read_bytes() == old_shipped
    assert K.builder_kind(loc / "tools" / "manuals_db.py", "manuals_db.py") == ("stub", "2")
    # an edited copy of the shipped builder is the user's file now: left alone
    edited = (K.builders_dir() / "infosys_local.py").read_text(encoding="utf-8") + "\n# my change\n"
    (loc / "tools" / "infosys_local.py").write_text(edited, encoding="utf-8")
    assert K.builder_kind(loc / "tools" / "infosys_local.py", "infosys_local.py") == ("other", None)
    # every later start: nothing to do; the pointer for the stubs is written once per plugin location/version
    up = k.builder_upkeep("9.9.9")
    assert up["tools"]["manuals_db.py"] == {"state": "stub", "stub_version": "2"} and up["pointer"] == "written"
    assert up["tools"]["infosys_local.py"]["state"] == "left alone"
    assert k.builder_upkeep("9.9.9")["pointer"] == "unchanged"
    ptr = json.loads((k.home / "plugin_location.json").read_text(encoding="utf-8"))
    assert Path(ptr["builders"]) == K.builders_dir() and ptr["version"] == "9.9.9"
    d = k.docs_layout(loc)
    b = d["builders"]["manuals_db.py"]
    assert b["copy_kind"] == "stub" and "nothing to update" in b["note"]
    assert d["builders"]["infosys_local.py"]["copy_kind"] == "other"
    assert "left alone" in d["builders"]["infosys_local.py"]["note"]
    # the user's own script is never offered as the command — the plugin's builder is
    assert str(K.builders_dir() / "infosys_local.py") in d["infosys"]["command"]
    # N64 polish: the start's upkeep is reported as such, with what it did — an unchanged stub is no action
    u = d["builder_upkeep"]
    assert u["at_server_start"] and "state of the files now" in u["note"]
    assert "manuals_db.py" not in u["tools"] and u["tools"]["infosys_local.py"]["state"] == "left alone"
    (loc / "tools" / "manuals_db.py").write_text((loc / "tools" / "manuals_db.py").read_text(encoding="utf-8") + "\n# edit\n",
                                                encoding="utf-8")
    d = k.docs_layout(loc)                                   # edited after the start: `builders` says so, upkeep unchanged
    assert d["builders"]["manuals_db.py"]["copy_kind"] == "other" and "manuals_db.py" not in d["builder_upkeep"]["tools"]


def test_content_sha_reads_crlf_as_lf_and_nothing_else(tmp_path):
    """A shipped builder is recognised with LF or CRLF line ends (a Windows checkout); any other difference — CR CR LF
    included — is a file we did not ship, left alone."""
    src = (K.builders_dir() / "manuals_db.py").read_bytes().replace(b"\r\n", b"\n")
    want = K._content_sha(K.builders_dir() / "manuals_db.py")
    for i, (data, same) in enumerate(((src, True), (src.replace(b"\n", b"\r\n"), True),
                                     (src.replace(b"\n", b"\r\r\n"), False))):
        f = tmp_path / f"m{i}.py"
        f.write_bytes(data)
        assert (K._content_sha(f) == want) is same, i


def test_n70_known_errors_in_every_notation_and_the_compiler_messages():
    """N70: 'ADS error 1861' (a space) was not known, only 'ADSError 1861' and '0x745'; the compiler messages C0046 /
    C0032 were not known at all. ADS codes are read in any notation; the compiler messages by their text."""
    for q in ("ADS error 1861", "ADSError 1861", "0x745", "ADS-Error: 0x00000745", "error code 1861"):
        k = K.known_error(q)
        assert k and k["cause"].startswith("the target did not answer"), q
    c46 = K.known_error("C0046: Identifier 'nFoo' not defined")
    assert c46 and "'nFoo' is not declared" in c46["cause"] and "proj_search nFoo" in c46["fix"], c46
    c32 = K.known_error("C0032: Cannot convert type 'BOOL' to type 'INT'")
    assert c32 and "from BOOL to INT" in c32["cause"] and "BOOL_TO_INT" in c32["fix"], c32
    assert K.known_error("HRESULT 0x6") is None                        # not every hex number is an ADS code
    assert "{type, value}" in K.known_error("RPC call 'P.fb#M' failed: ADSError 1797 (0x705)")["fix"]   # braces kept


def test_n71_n72_kb_search_stop_words_error_text_and_copy_names(kb, project_dir):
    """N71: 'a the' searched the code for 'a' and 'the'; the C0032 text found units by 'to' / 'TYPE' alone.
    N72: PRG_TcUnitRunner_9 (a copy name) found nothing and did not say why."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    k.add_project(str(project_dir), "sample")
    with pytest.raises(K.KnowledgeError, match="only stop words"):
        k.search("a the")
    r = k.search("C0032: Cannot convert type 'BOOL' to type 'INT'")
    assert r["known_error"] and r["hits"] == [] and "known_error has its cause" in r["note"], r
    assert r["words_ignored"]["to"] == "stop word" and "error message" in r["words_ignored"]["type"], r
    r = k.search("C0046: Identifier 'FB_Valve' not defined")              # the quoted name is looked for
    assert r["known_error"] and r["hits"] and r["hits"][0]["unit"].startswith("FB_Valve"), r
    r = k.search("how to open the valve")
    assert {"how", "to", "the"} <= set(r["words_ignored"]) and r["hits"], r
    r = k.search("FB_Valve_9")
    assert r["hits"] and r["hits"][0]["unit"].startswith("FB_Valve") and r["hits"][0]["match"].startswith("base name")
    assert "copy of (FB_Valve)" in r["note"], r
    r = k.search("FB_Nothing_9")
    assert r["hits"] == [] and "no unit contains FB_Nothing_9" in r["note"], r
    r = k.search("ADS error 9999 zzqqxx")
    assert r["known_error"] is None and "1861" in r["known_errors_covered"], r


def _older_revisions(tmp):
    """Two older revisions of EL2008 (as Beckhoff's files hold them: every revision a Device of its own)."""
    old = ESI.replace('RevisionNo="#x00100000">EL2008', 'RevisionNo="#x000F0000">EL2008')
    (tmp / "esi" / "Beckhoff EL2xxx old.xml").write_text(old, encoding="utf-8")
    older = ESI.replace('RevisionNo="#x00100000">EL2008', 'RevisionNo="#x000E0000">EL2008')
    (tmp / "esi" / "Beckhoff EL2xxx older.xml").write_text(older, encoding="utf-8")


def test_n73_hw_lookup_output_size_is_controllable(kb):
    """N73: every revision came with all its PDOs and CoE objects — EL3356-0010 / EL2407 were walls of repeats."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    _older_revisions(tmp)
    k.esi_build()
    r = k.hw_lookup("EL2008", limit=5)
    assert r["detail"] == "newest" and len(r["devices"]) == 3
    assert "pdo" in r["devices"][0] and all("pdo" not in d and "coe_objects" not in d for d in r["devices"][1:])
    assert all(d["pdo_count"] >= 1 and d["coe_count"] >= 1 for d in r["devices"]) and "detail='full'" in r["detail_note"]
    s = k.hw_lookup("EL2008", limit=5, detail="summary")
    assert all("pdo" not in d for d in s["devices"]) and len(json.dumps(s)) < len(json.dumps(r))
    f = k.hw_lookup("EL2008", limit=5, detail="full")
    assert all("pdo" in d and "coe_objects" in d for d in f["devices"]) and "detail_note" not in f
    old = f["devices"][2]["revision"]
    o = k.hw_lookup("EL2008", revision=int(old[2:], 16))
    assert len(o["devices"]) == 1 and "pdo" in o["devices"][0]
    with pytest.raises(K.KnowledgeError, match="detail must be one of"):
        k.hw_lookup("EL2008", detail="all")


def test_review_known_errors_and_kb_search_edge_cases(kb, project_dir, tmp_path):
    """Review of N70–N74: the first table match shadowed the compiler patterns ('bLicenseOk' read as a license
    error); an offset '0x7' turned ADSError 1796 into 7; 'error code 7' of any program was an ADS error; annotate()
    took a filled-in name for a tool name and dropped the annotation; a code query that only CONTAINED a known-error
    text ('Valve license') was not searched; a one-word query of a keyword ('WHILE') was refused; INT → BOOL
    suggested BOOL_TO_BOOL."""
    assert "'bLicenseOk' is not declared" in K.known_error("C0046: Identifier 'bLicenseOk' not defined")["cause"]
    assert K.known_error("FB_LicenseCheck") is None
    assert K.known_error("ADSError 1796 at offset 0x7")["cause"].startswith("reading/writing this index group")
    assert K.known_error("exited with error code 7") is None
    assert "[known cause:" in K.annotate("C0046: Identifier 'my_var' not defined")
    f = K.known_error("C0032: Cannot convert type 'INT' to type 'BOOL'")["fix"]
    assert "(value <> 0)" in f and "BOOL_TO_BOOL" not in f, f
    assert "not assignment-compatible" in K.known_error("C0032: Cannot convert type 'ST_Foo' to type 'INT'")["fix"]
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    k.add_project(str(project_dir), "sample")
    r = k.search("Valve license")
    # N130 (0.8.13 retest): searched — and no licence note: words of the code are no error message
    assert "known_error" not in r and r["hits"] and r["hits"][0]["unit"].startswith("FB_Valve"), r
    assert isinstance(k.search("WHILE")["hits"], list)                   # searched as written, not refused
    # every word common in a big project: the least common one still picks units (the tier was empty)
    big = tmp_path / "big" / "POUs"
    big.mkdir(parents=True)
    for i in range(30):
        words = "Valve" if i < 18 else "Pump"           # no unit has both; both are on more than 20 % of them
        (big / f"PRG_{i}.TcPOU").write_text(
            f'<?xml version="1.0" encoding="utf-8"?><TcPlcObject><POU Name="PRG_{i}"><Declaration><![CDATA[PROGRAM '
            f'PRG_{i}\nVAR\nEND_VAR]]></Declaration><Implementation><ST><![CDATA[// {words}\n;]]></ST></Implementation>'
            '</POU></TcPlcObject>', encoding="utf-8")
    k.add_project(str(tmp_path / "big"), "big")
    r = k.search("Valve Pump", project="big", limit=50)
    assert r["hits"] and r.get("words_used") == ["Pump"] and r.get("words_too_common") == ["Valve"], r
    assert len(r["hits"]) == 12 and all(h["matched"] == ["Pump"] for h in r["hits"]), r


def test_review_hw_lookup_revision_text_and_newest_of_every_type_first(kb):
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    _older_revisions(tmp)
    k.esi_build()
    r = k.hw_lookup("EL", limit=2)                       # name fragment: EL2008 ×3 revisions and EL3062
    assert [d["type"] for d in r["devices"]] == ["EL2008", "EL3062"] and all("pdo" in d for d in r["devices"]), r
    old = "#x000E0000"
    o = k.hw_lookup("EL2008", revision=old)              # the text form the output shows
    assert [d["revision"] for d in o["devices"]] == [old] and "pdo" in o["devices"][0]
    with pytest.raises(K.KnowledgeError, match="revision must be"):
        k.hw_lookup("EL2008", revision="banana")


def test_esi_index_is_built_once_by_itself_and_rebuilt_only_when_asked(kb):
    """docs/KNOWLEDGE_BASE.md rule 6 for index\\esi.db: its first build starts by itself (the server); an index that
    exists is used as it is — new ESI files or another format are reported (esi_state, the answer's
    source.state_note — N155, 0.8.14 retest: it was esi_index_note, the skill's hw_* commands said it in
    source.state_note; one envelope now) until kb_rebuild(index='esi') runs the builder. Nothing of the old
    knowledge.db ESI table is read."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    assert k.esi_state()["state"] == "missing"
    assert k.esi_build()["devices"] == 2
    st = k.esi_state()
    assert st["state"] == "complete" and st["devices"] == 2 and st["files"] == 1
    assert st["sources"] == [{"folder": str(tmp / "esi"), "source": "twincat"}]
    (tmp / "esi" / "Beckhoff EL1xxx.xml").write_text(ESI.replace("EL2008", "EL1008").replace("EL3062", "EL1018")
                                                     .replace("#x07d83052", "#x03f03052").replace("#x0bf03052", "#x03fa3052"),
                                                     encoding="utf-8")
    st = k.esi_state()
    assert st["state"] == "outdated" and "kb_rebuild(index='esi')" in st["message"]
    r = k.hw_lookup("EL1008")
    assert r["matches"] == 0 and r["source"]["state"] == "outdated" and "OUTDATED" in r["source"]["state_note"]
    assert k.esi_build()["result"] == "built" and k.hw_lookup("EL1008")["matches"] == 1
    assert k.esi_state()["state"] == "complete" and k.esi_build()["result"] == "up_to_date"
    # the ESI files in the knowledge folder's manuals\\ are read too
    m = tmp / "kbdata" / "manuals" / "Beckhoff_EtherCAT_XML"
    m.mkdir(parents=True)
    (m / "Beckhoff EL9xxx.xml").write_text(ESI.replace("EL2008", "EL9008").replace("EL3062", "EL9062")
                                           .replace("#x07d83052", "#x23303052").replace("#x0bf03052", "#x2336052"),
                                           encoding="utf-8")
    assert k.esi_state()["state"] == "outdated"
    k.esi_build()
    assert k.hw_lookup("EL9008")["devices"][0]["file"] == str(m / "Beckhoff EL9xxx.xml")
    assert k.status()["esi"]["state"] == "complete"


def test_a_stub_of_0811_becomes_the_current_one(kb, monkeypatch):
    """0.8.12 stub 2 also runs esi_db.py: the stub 1 every knowledge folder has (sha in SHIPPED_STUBS) is replaced,
    kept as .bak."""
    k, tmp = kb
    loc = tmp / "kbfolder"
    (loc / "tools").mkdir(parents=True)
    import subprocess
    old = subprocess.run(["git", "show", "1200fee:skills/twincat-docs/builders/stub.py"], capture_output=True,
                         cwd=Path(__file__).resolve().parents[1])
    if old.returncode != 0:
        pytest.skip("git history not available")
    (loc / "tools" / "manuals_db.py").write_bytes(old.stdout)
    assert K.builder_kind(loc / "tools" / "manuals_db.py", "manuals_db.py") == ("copy", "stub 1")
    st = k.set_location(str(loc))["builder_stubs"]
    assert st["manuals_db.py"]["state"] == "replaced"
    assert K.builder_kind(loc / "tools" / "manuals_db.py", "manuals_db.py") == ("stub", "2")
    assert "esi_db.py" in (loc / "tools" / "manuals_db.py").read_text(encoding="utf-8")


def _pou(folder: Path, name: str, decl: str, impl: str = "") -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.TcPOU").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<TcPlcObject Version="1.1.0.1" ProductVersion="3.1.4024.12">\n'
        f'  <POU Name="{name}" Id="{{aaaaaaaa-0000-0000-0000-0000000000{len(name):02d}}}" SpecialFunc="None">\n'
        f"    <Declaration><![CDATA[{decl}]]></Declaration>\n"
        f"    <Implementation><ST><![CDATA[{impl}]]></ST></Implementation>\n  </POU>\n</TcPlcObject>\n",
        encoding="utf-8")


def test_n102_identifier_words_and_code_before_comments(kb, tmp_path):
    """N102 (0.8.11 retest): kb_search 'alarm text' found only GVL_Machine (a comment word), not FB_AlarmList — the
    tokenizer keeps FB_AlarmList / sAlarmText whole; 'EXTENDS FB_ModuleBase' ranked a unit with the words in a
    comment above the real subclass. The search table holds the words of names and identifiers; a unit whose code
    holds every word comes before one that has some of them only in comments."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    src = tmp_path / "alarms"
    _pou(src, "FB_AlarmList", "FUNCTION_BLOCK FB_AlarmList\nVAR\n    aAlarmTexts : ARRAY[1..10] OF STRING;\nEND_VAR",
         "aAlarmTexts[1] := 'Overpressure';")
    _pou(src, "PRG_Main", "PROGRAM PRG_Main\nVAR\n    nCycle : UDINT;\nEND_VAR",
         "// the alarm text is shown by the HMI\nnCycle := nCycle + 1;")
    _pou(src, "FB_ModuleBase", "FUNCTION_BLOCK FB_ModuleBase\nVAR\n    bBusy : BOOL;\nEND_VAR")
    _pou(src, "FB_CounterModule", "FUNCTION_BLOCK FB_CounterModule EXTENDS FB_ModuleBase\nVAR\n    nCount : INT;\nEND_VAR")
    _pou(src, "FB_Notes", "FUNCTION_BLOCK FB_Notes\nVAR\n    bDummy : BOOL; (* later: EXTENDS FB_ModuleBase *)\nEND_VAR")
    k.add_project(str(src), "alarms")
    r = k.search("alarm text")
    units = [h["unit"] for h in r["hits"]]
    assert units[0] == "FB_AlarmList" and "match_in" not in r["hits"][0], r
    assert dict((h["unit"], h.get("match_in")) for h in r["hits"]).get("PRG_Main") == "comments", r
    r = k.search("EXTENDS FB_ModuleBase")
    assert [h["unit"] for h in r["hits"]][0] == "FB_CounterModule", r
    assert {h["unit"]: h.get("match_in") for h in r["hits"]}["FB_Notes"] == "comments", r
    assert "search_table_note" not in r


def test_n102_a_search_table_of_0811_is_rebuilt_on_the_next_reindex(kb, tmp_path):
    """The search table of 0.8.11 has no identifier words: kb_search says so and uses it as it is; the next
    kb_reindex (the user's call) rebuilds it from the stored units in one transaction — no file is read again."""
    import sqlite3
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    src = tmp_path / "alarms"
    _pou(src, "FB_AlarmList", "FUNCTION_BLOCK FB_AlarmList\nVAR\n    aAlarmTexts : ARRAY[1..10] OF STRING;\nEND_VAR")
    k.add_project(str(src), "alarms")
    db = tmp / "kbdata" / "index" / "knowledge.db"
    con = sqlite3.connect(db)
    con.execute("DROP TABLE units_fts")             # the table as 0.8.11 made it, filled from the units
    con.execute("CREATE VIRTUAL TABLE units_fts USING fts5(project UNINDEXED, name, kind UNINDEXED, file UNINDEXED, "
                "declaration, implementation, comments, tokenize='unicode61')")
    con.execute("INSERT INTO units_fts SELECT project, name, kind, file, declaration, implementation, comments FROM units")
    con.execute("PRAGMA user_version = 0")
    con.commit()
    con.close()
    k2 = K.Knowledge(tmp / "home", str(tmp / "kbdata"))
    r = k2.search("alarm text")                     # 'text' is in no token of the 0.8.11 table
    assert r["words_ignored"]["text"] == "on no unit" and "kb_reindex() rebuilds it" in r["search_table_note"], r
    (src / "FB_AlarmList.TcPOU").unlink()                       # the rebuild reads no file
    res = k2.reindex(str(src))
    assert res["search_table"]["units"] == 1, res
    r = k2.search("alarm list")
    # the unit is gone from the folder, so the folder's re-index removed it — but the table was rebuilt first
    assert "search_table_note" not in r
    _pou(src, "FB_AlarmList", "FUNCTION_BLOCK FB_AlarmList\nVAR\n    aAlarmTexts : ARRAY[1..10] OF STRING;\nEND_VAR")
    k2.reindex(str(src))
    assert [h["unit"] for h in k2.search("alarm text")["hits"]] == ["FB_AlarmList"]


def test_review_0812_phrases_only_and_code_hits_beyond_the_window(kb, tmp_path):
    """Review 0.8.12: with quoted phrases only, units with one phrase were added to those with both (the 'some words'
    tier is the second one there); with many comment matches, code matches ranked below the bm25 window were lost."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    src = tmp_path / "p"
    _pou(src, "FB_Both", "FUNCTION_BLOCK FB_Both\nVAR\n    bAlarmList : BOOL; bResetValve : BOOL;\nEND_VAR",
         "// alarm list and reset valve\nbAlarmList := bResetValve;")
    _pou(src, "FB_One", "FUNCTION_BLOCK FB_One\nVAR\n    nX : INT;\nEND_VAR", "// alarm list only\nnX := 1;")
    for i in range(70):                                            # comment-only matches outrank code in bm25
        _pou(src, f"FB_Note{i:02d}", f"FUNCTION_BLOCK FB_Note{i:02d}\nVAR\n    nY : INT;\nEND_VAR",
             "// valve valve valve\nnY := 1;")
    _pou(src, "FB_Valve", "FUNCTION_BLOCK FB_Valve\nVAR\n    bOpen : BOOL;\nEND_VAR", "bOpen := TRUE;")
    k.add_project(str(src), "p")
    r = k.search('"alarm list" "reset valve"')
    assert [h["unit"] for h in r["hits"]] == ["FB_Both"], r
    r = k.search("valve", limit=5)
    assert {h["unit"] for h in r["hits"][:2]} == {"FB_Valve", "FB_Both"}, r      # name / identifier bResetValve
    assert r["hits"][0]["unit"] == "FB_Valve"                                   # the name names it
    assert all(h.get("match_in") == "comments" for h in r["hits"][2:]) and len(r["hits"]) == 5, r


def test_n128_project_folders_that_are_not_projects_or_overlap_are_refused(kb, project_dir, tmp_path, monkeypatch):
    """N128 (0.8.13 retest): kb_add_project took the plugin folder (its test fixture's MAIN then ranked above P15's
    MAIN), the knowledge folder itself (0 units, no warning) and a child of an indexed folder (every unit twice)."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    plugin = Path(K.__file__).resolve().parents[1]
    with pytest.raises(K.KnowledgeError, match="inside the plugin folder"):
        k.add_project(str(plugin / "tests"))
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "around" / "plugin-data"))
    (tmp_path / "around" / "plugin-data").mkdir(parents=True)
    with pytest.raises(K.KnowledgeError, match="contains the plugin folder"):
        k.add_project(str(tmp_path / "around"))
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA")
    with pytest.raises(K.KnowledgeError, match="is the knowledge folder"):
        k.add_project(str(tmp / "kbdata"))
    with pytest.raises(K.KnowledgeError, match="inside the knowledge folder"):
        k.add_project(str(tmp / "kbdata" / "index"))
    k.add_project(str(project_dir), "p")
    with pytest.raises(K.KnowledgeError, match=r"inside the indexed folder .*kb_reindex 'p'"):
        k.add_project(str(project_dir / "Plc"))
    with pytest.raises(K.KnowledgeError, match=r"inside the indexed folder"):         # review: '\\?\' spelling
        k.add_project("\\\\?\\" + str(project_dir / "Plc"))
    with pytest.raises(K.KnowledgeError, match="contains the indexed folder .*kb_remove_project"):
        k.add_project(str(project_dir.parent))
    assert [p["path"] for p in k.status()["projects"]] == [str(project_dir)]
    assert k.add_project(str(project_dir) + "/")["note"].startswith(str(project_dir))      # same folder: re-indexed


def test_n136_location_messages_long_paths_sync_names_drive_root_and_fallback(kb, tmp_path):
    """N136 (0.8.13 retest): a file → 'not writable ([WinError 183])'; '\\\\?\\C:\\…' treated as UNC; 'D:\\Toolbox
    Drive' refused as Box Drive (substring); a drive root accepted; use_fallback silently replaced a given path."""
    k, tmp = kb
    f = tmp / "a_file.txt"
    f.write_text("x")
    assert K.check_location(f) == ["it is a file, not a folder"]
    assert K.check_location(f / "kb") == [f"{f} is a file, not a folder"]
    assert K.strip_long_prefix("\\\\?\\C:\\TwinCAT-MCP\\knowledge") == "C:\\TwinCAT-MCP\\knowledge"
    assert K.strip_long_prefix("\\\\?\\UNC\\srv\\share\\kb") == "\\\\srv\\share\\kb"
    assert K.strip_long_prefix("D:\\kb") == "D:\\kb"
    assert K.check_location(tmp / "Toolbox Drive" / "kb") == []
    for synced in ("OneDrive - Contoso", "Dropbox (Personal)", "Google Drive", "iCloudDrive", "Box Sync"):
        assert "it is inside a cloud-synced folder" in K.check_location(tmp / synced / "kb"), synced
    assert any("drive root" in p for p in K.check_location(Path(tmp.anchor)))
    with pytest.raises(K.KnowledgeError, match="either path=… or use_fallback=true, not both"):
        k.set_location(str(tmp / "kbdata"), use_fallback=True)
    assert k.location()[0] is None


def test_agent_findings_registration_write_lock_and_reads_that_create_nothing(kb, project_dir, tmp_path, monkeypatch):
    """Agent findings of the 0.8.13 retest (code): kb_add_project registered the folder before indexing it (a crash
    left it with 0 units); knowledge.db had no lock — two adds of one new folder ended in an IntegrityError; kb_status
    and kb_search (READ) created index\\ and the tables."""
    k, tmp = kb
    kbf = tmp_path / "kb_ro"
    kbf.mkdir()
    k2 = K.Knowledge(tmp_path / "home_ro", str(kbf))
    st = k2.status()
    assert st["projects"] == [] and "not created yet" in st["database_state"]
    assert k2.search("FB_Valve")["projects_indexed"] == 0
    assert not (kbf / "index" / "knowledge.db").exists()
    with pytest.raises(K.KnowledgeError, match="not in the knowledge base"):
        k2.remove_project(str(project_dir))
    # review of 0.8.14: the empty stand-in of those reads must not be taken for the database — the first add then had
    # no search table ('no such table: units_fts') and the folder stayed on the LIKE fallback
    other = tmp_path / "other_proj"
    import shutil as _sh
    _sh.copytree(project_dir, other)
    assert k2.add_project(str(other), "o")["reindexed"][0]["units"] >= 8
    import sqlite3 as _sq
    assert _sq.connect(kbf / "index" / "knowledge.db").execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'units_fts'").fetchone()
    assert K.Knowledge(tmp_path / "home_ro", str(kbf)).search("Valve")["hits"][0]["unit"].startswith("FB_Valve")
    # a knowledge folder in use at a drive root keeps working (only choosing one is refused)
    assert not any("drive root" in x for x in K.check_location(Path(tmp_path.anchor), probe=False, space=False))
    # a folder whose scan fails is not registered
    k.set_location(str(tmp / "kbdata"))
    from twincat_mcp import project_backend as PB

    def broken(self, root=None):
        raise RuntimeError("disk read error")
    monkeypatch.setattr(PB.ProjectIndex, "scan", broken)
    r = k.add_project(str(project_dir), "p")
    assert r["reindexed"][0]["error"] == "disk read error" and r["reindexed"][0]["note"].startswith("not added"), r
    assert k.status()["projects"] == []
    monkeypatch.undo()
    # one change at a time: a second writer is refused with what runs
    from twincat_mcp import kb_lock
    with kb_lock.IndexLock(tmp / "kbdata" / "index" / "knowledge.db.lock") as lk:
        lk.update(phase="kb_reindex (every changed folder)")
        for call in (lambda: k.add_project(str(project_dir)), lambda: k.reindex(), lambda: k.remove_project("p")):
            with pytest.raises(K.KnowledgeError, match=r"another change of the knowledge base is running \(kb_reindex"):
                call()
        assert k.search("FB_Valve")["projects_indexed"] == 0               # reads are not blocked
        with pytest.raises(K.KnowledgeError, match="still running"):       # nor is the location switched under it
            k.set_location(str(tmp_path / "kb_elsewhere"))
    assert k.add_project(str(project_dir), "p")["reindexed"][0]["units"] >= 8


def test_n132_unreadable_files_are_named_utf16_read_and_no_text_cut(kb, project_dir, tmp_path):
    """N132 (0.8.13 retest): parse errors were only counted (no file); a UTF-16 .TcPOU (valid XML) was a parse error,
    cp1252 umlauts became U+FFFD; a source no PLC project compiles was skipped without a word; code after 20,000
    characters and comments after 5,000 were not searchable."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    pous = project_dir / "Plc" / "POUs"
    _pou(pous, "FB_Utf16", "FUNCTION_BLOCK FB_Utf16\nVAR\n    nDurchfluss : INT; // Durchfluß\nEND_VAR")
    f16 = pous / "FB_Utf16.TcPOU"
    f16.write_bytes(f16.read_text(encoding="utf-8").replace('encoding="utf-8"', 'encoding="utf-16"').encode("utf-16"))
    _pou(pous, "FB_Ansi", "FUNCTION_BLOCK FB_Ansi\nVAR\n    nMenge : INT; // Größe\nEND_VAR")
    fa = pous / "FB_Ansi.TcPOU"
    fa.write_bytes(fa.read_text(encoding="utf-8").encode("cp1252"))          # claims UTF-8, holds cp1252 bytes
    (pous / "FB_Broken.TcPOU").write_text("<?xml version='1.0'?><TcPlcObject><POU Name='FB_Broken'>", encoding="utf-8")
    _pou(pous, "FB_Long", "FUNCTION_BLOCK FB_Long\nVAR\nEND_VAR", ";\n" * 15000 + "nZzqFinalWord := 1; // zzqlastcomment")
    from tests.conftest import register_sources
    register_sources(project_dir / "Plc" / "Plc.plcproj")
    _pou(project_dir / "Plc" / "Backup", "FB_KbDup", "FUNCTION_BLOCK FB_KbDup\nVAR\nEND_VAR")   # not compiled
    r = k.add_project(str(project_dir), "p")["reindexed"][0]
    assert r["parse_errors"] == 1 and [e["file"] for e in r["parse_error_files"]] == [str(pous / "FB_Broken.TcPOU")]
    assert list(r["read_notes"]) == [str(fa)] and "Windows-1252" in r["read_notes"][str(fa)], r
    assert r["not_in_project"]["count"] == 1 and r["not_in_project"]["files"][0].endswith("FB_KbDup.TcPOU"), r
    assert k.search("nDurchfluss")["hits"][0]["unit"] == "FB_Utf16"
    assert "Größe" in k.search("nMenge")["hits"][0]["declaration"]
    assert k.search("nZzqFinalWord")["hits"][0]["unit"] == "FB_Long"
    assert k.search("zzqlastcomment")["hits"][0]["unit"] == "FB_Long"
    pr = k.status()["projects"][0]
    assert pr["parse_errors"] == 1 and pr["not_in_project_count"] == 1 and pr["problems"]["read_notes"], pr
    # the project index reads it the same way; an edit keeps UTF-16 UTF-16 and refuses the mis-encoded file
    from twincat_mcp.config import ProjectConfig
    from twincat_mcp.project_backend import ProjectIndex, ProjectError
    idx = ProjectIndex(ProjectConfig(root=str(project_dir)))
    sc = idx.scan()
    assert sc["parse_errors"][0]["file"].endswith("FB_Broken.TcPOU") and str(fa) in sc["read_notes"]
    idx.set_code("FB_Utf16", None, "nDurchfluss := 1; // ß")
    assert f16.read_bytes()[:2] == b"\xff\xfe" and "nDurchfluss := 1; // ß" in f16.read_text(encoding="utf-16")
    with pytest.raises(ProjectError, match="not valid UTF-8 .*not edited"):
        idx.set_code("FB_Ansi", None, "nMenge := 1;")
    # review of 0.8.14: proj_validate_xml reads and writes the files as the index does
    from twincat_mcp import codecheck
    v = codecheck.validate_xml(idx)
    rules = {(Path(i["file"]).name, i["rule"]) for i in v["items"]}
    assert ("FB_Utf16.TcPOU", "xml") not in rules and ("FB_Ansi.TcPOU", "encoding") in rules, v["items"]
    # a UTF-8 file with a BOM and one bad byte stays UTF-8 (its good umlauts are kept); bytes alone decide otherwise
    from twincat_mcp.project_backend import decode_source
    t, enc, bom, note = decode_source(b"\xef\xbb\xbf<a>Gr\xc3\xb6\xc3\x9fe \x96</a>")
    assert t == "<a>Größe \ufffd</a>" and enc == "utf-8" and "U+FFFD" in note
    t, enc, bom, note = decode_source(b"<a>Gr\xc3\xb6\xc3\x9fe \x96</a>")
    assert t.startswith("<a>Größe") and enc == "utf-8"
    t, enc, bom, note = decode_source(b"<a>Gr\xf6\xdfe \x96</a>")
    assert t == "<a>Größe –</a>" and enc == "cp1252" and "Windows-1252" in note
    # re-review: one valid-looking pair ('ß“' = DF 93) does not make a Windows-1252 file UTF-8
    t, enc, bom, note = decode_source("<a>Größe: „Maß“ – Öl</a>".encode("cp1252"))
    assert t == "<a>Größe: „Maß“ – Öl</a>" and enc == "cp1252", t


def test_n133_a_changed_renamed_or_deleted_folder_is_reported(kb, project_dir, tmp_path):
    """N133 (0.8.13 retest): after editing / deleting files and renaming the folder kb_search answered from the old
    units without a note; kb_status never looked at the folders; the change stamp ignored .tsproj / .xti."""
    import os
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    k.add_project(str(project_dir), "p")
    assert "stale" not in k.search("FB_Valve") and k.status()["projects"][0]["state"] == "current"
    # review of 0.8.14: a folder indexed by 0.8.13 (its stamp of sources only) is not 'changed' after the update
    import sqlite3 as _sq
    db = _sq.connect(tmp / "kbdata" / "index" / "knowledge.db")
    db.execute("UPDATE projects SET files=?", (K._legacy_tree_stamp(project_dir),))
    db.commit()
    assert k.status()["projects"][0]["state"] == "current" and "stale" not in k.search("FB_Valve")
    (project_dir / "Plc" / "POUs" / "MAIN.TcPOU").write_text(
        (project_dir / "Plc" / "POUs" / "MAIN.TcPOU").read_text() + "\n")
    assert k.status()["projects"][0]["state"].startswith("files changed")
    k.reindex("p")
    ts = project_dir / "Machine.tsproj"
    os.utime(ts, ns=(ts.stat().st_atime_ns, ts.stat().st_mtime_ns + 10**9))
    st = k.status()["projects"][0]["state"]
    assert st.startswith("files changed since it was indexed") and "kb_reindex" in st
    r = k.search("FB_Valve")
    assert r["hits"] and r["stale"][0]["project"] == str(project_dir) and r["stale"][0]["label"] == "p", r
    assert k.reindex()["reindexed"][0]["units"] >= 8 and "stale" not in k.search("FB_Valve")
    project_dir.rename(project_dir.with_name("renamed"))
    r = k.search("FB_Valve")
    assert r["hits"] and r["stale"][0]["state"].startswith("folder not found"), r
    assert k.status()["projects"][0]["state"].startswith("folder not found")


def test_n131_n134_n135_declaring_unit_first_kind_pou_and_query_handling(kb, tmp_path):
    """N131: the declaring unit was not preferred (MAX_ALARMS → GVL_Param 6th of 7, nErrorCode → its struct last).
    N134: kinds=['POU'] never matched a parsed unit. N135: 'OR' silently meant AND; 'alarm OR NEAR(state) -reset* ^x'
    searched only 'x'; single letters were search words; '_' was 'no unit contains _'; a phrase in a string literal
    was labelled match_in 'comments'."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    src = tmp_path / "decl"
    gvl = src / "GVL_Param.TcGVL"
    src.mkdir(parents=True)
    gvl.write_text('<?xml version="1.0" encoding="utf-8"?><TcPlcObject><GVL Name="GVL_Param"><Declaration><![CDATA['
                   "VAR_GLOBAL CONSTANT\n    MAX_ALARMS : INT := 10;\nEND_VAR]]></Declaration></GVL></TcPlcObject>",
                   encoding="utf-8")
    for i in range(6):
        _pou(src, f"FB_User{i}", f"FUNCTION_BLOCK FB_User{i}\nVAR\n    aList : ARRAY[1..GVL_Param.MAX_ALARMS] OF INT;\n"
             "    stStatus : ST_ModuleStatus;\nEND_VAR",
             "IF stStatus.nErrorCode > MAX_ALARMS THEN stStatus.nErrorCode := 0; END_IF")
    (src / "ST_ModuleStatus.TcDUT").write_text(
        '<?xml version="1.0" encoding="utf-8"?><TcPlcObject><DUT Name="ST_ModuleStatus"><Declaration><![CDATA['
        "TYPE ST_ModuleStatus :\nSTRUCT\n    bBusy : BOOL;\n    nErrorCode : DINT;\nEND_STRUCT\nEND_TYPE]]>"
        "</Declaration></DUT></TcPlcObject>", encoding="utf-8")
    _pou(src, "FB_Edge", "FUNCTION_BLOCK FB_Edge\nVAR\n    sMsg : STRING := 'rising edge seen';\nEND_VAR",
         "bReset := bEnable;")
    _pou(src, "FB_Edge2", "FUNCTION_BLOCK FB_Edge2\nVAR\nEND_VAR", "// on a rising edge\n;")
    k.add_project(str(src), "decl")
    r = k.search("MAX_ALARMS")
    assert r["hits"][0]["unit"] == "GVL_Param" and r["hits"][0]["declares"], [h["unit"] for h in r["hits"]]
    r = k.search("nErrorCode")
    assert r["hits"][0]["unit"] == "ST_ModuleStatus" and r["hits"][0]["declares"], [h["unit"] for h in r["hits"]]
    assert len(r["hits"]) == 7 and not any(h.get("declares") for h in r["hits"][1:]), r
    # N134
    assert {h["unit"] for h in k.search("nErrorCode", kinds=["POU"])["hits"]} == {f"FB_User{i}" for i in range(6)}
    assert [h["unit"] for h in k.search("nErrorCode", kinds=["STRUCT"])["hits"]] == ["ST_ModuleStatus"]
    # N135
    r = k.search("bEnable OR bReset")
    assert "not a search operator" in r["words_ignored"]["OR"] and "operators_note" in r, r
    assert [h["unit"] for h in r["hits"]] == ["FB_Edge"], r
    r = k.search("MAX_ALARMS OR NEAR(nErrorCode) -bBusy* ^x")
    assert "single character" in r["words_ignored"]["x"] and "x" not in r.get("words_used", []), r
    assert r["operators_note"].startswith("kb_search has no query operators"), r
    with pytest.raises(K.KnowledgeError, match="no searchable word"):
        k.search("_")
    r = k.search('"rising edge"')
    labels = {h["unit"]: h.get("match_in") for h in r["hits"]}
    assert labels == {"FB_Edge": "string literals or comments", "FB_Edge2": "comments"}, r


def test_n129_n130_error_messages_keep_their_names_and_known_errors_need_their_context(kb, tmp_path):
    """N129 (0.8.13 retest): 'ADS error 1808 fbCounter' searched nothing ('fbCounter' was "error message text");
    'nCycleCount 0x705' got the ADS 1797 note (any bare 0x700–0x7FF was ADS) and 0 hits. N130: 'License' got the
    licence note; 'Ads-Error 0xA2' (no note) was searched as words — 'Error' listed 16 units; '0x6 port', 'not
    permitted', 'timeout elapsed', 'logged in' in plain text matched; ADSERR_* names and unquoted identifiers were
    not recognised; a bare C0046 was claimed as covered."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    src = tmp_path / "err"
    _pou(src, "PRG_Machine", "PROGRAM PRG_Machine\nVAR\n    fbCounter : FB_Counter;\n    nCycleCount : UDINT;\nEND_VAR",
         "fbCounter(bEnable := TRUE);\nnCycleCount := nCycleCount + 1;\n// error handling below")
    _pou(src, "FB_Counter", "FUNCTION_BLOCK FB_Counter\nVAR_INPUT\n    bEnable : BOOL;\nEND_VAR")
    _pou(src, "FB_ErrorHandler", "FUNCTION_BLOCK FB_ErrorHandler\nVAR\n    nError : UDINT;\nEND_VAR")
    k.add_project(str(src), "err")
    r = k.search("ADS error 1808 fbCounter")
    assert r["known_error"]["code"] == "ADS return code 0x710 (1808) ADSERR_DEVICE_SYMBOLNOTFOUND", r
    assert [h["unit"] for h in r["hits"]] == ["PRG_Machine"], r
    r = k.search("nCycleCount 0x705")
    assert "known_error" not in r and r["hits"][0]["unit"] == "PRG_Machine", r
    assert K.known_error("nCycleCount 0x705") is None
    assert "known_error" not in k.search("License")
    r = k.search("Ads-Error 0xA2")
    assert r["known_error"] is None and r["hits"] == [] and "not searched" in r["note"], r
    assert "1861" in r["known_errors_covered"], r
    r = k.search("error handler")                                          # words of the code: searched as such
    assert "known_error" not in r and r["hits"][0]["unit"] == "FB_ErrorHandler", r
    for plain in ("0x6 port 3", "Operation not permitted", "timeout elapsed while waiting"):
        assert K.known_error(plain) is None, plain
    assert "known_error" not in k.search("logged in") and "known_error" not in k.search("timeout elapsed")
    assert K.annotate("PermissionError: [Errno 1] Operation not permitted") == \
        "PermissionError: [Errno 1] Operation not permitted"
    # the code in every notation the local ADS return-code table gives — and pyads' text, in a message about ADS
    for q in ("ADSERR_CLIENT_SYNCTIMEOUT", "0x98110745", "ADS-Error: 0x745", "ADSError 1861", "0x745", "error 1861",
              "nErrId := 1861", "ADSError: timeout elapsed", "ADSError: Timeout elapsed (1861). "):
        assert K.known_error(q)["code"].startswith("ADS return code 0x745 (1861)"), q
        assert K.is_error_message(q) or q == "ADSError: timeout elapsed", q
    # review of 0.8.14: pyads' own text is an error message for kb_search (it was searched as words)
    r = k.search("ADSError: symbol not found (1808). ")
    assert r["known_error"]["code"].endswith("ADSERR_DEVICE_SYMBOLNOTFOUND") and r["hits"] == [], r
    # … and words of the code are not: identifiers beginning with ERR_ / err_, numbers, model numbers, sentences
    for q in ("ERR_TIMEOUT", "err_count reset", "851", "16#FF", "C6015", "the unknown type of data", "1861"):
        assert not K.is_error_message(q), q
    # re-review of 0.8.14: pyads' text with parentheses of its own, 'error no. N', and ST's 16# numbers
    assert K._ads_codes("ADSError: invalid parameter value(s) (1798). ") == [1798]
    assert K.is_error_message("ADSError: invalid parameter value(s) (1798). ")
    assert K.known_error("Error No.: 1808")["code"].endswith("ADSERR_DEVICE_SYMBOLNOTFOUND")
    assert K._ads_codes("ADS: nErrId := 16#745") == [1861] and K._ads_codes("error 16#745 at offset 3") == [1861]
    assert K.known_error("the unknown type of data") is None and K.known_error("identifier is not defined") is None
    assert K.known_error("TIPC^Plc1^Plc1 Project is not logged in — start acts on the online session") is None
    assert K.known_error("ADSError 1796: reading or writing not permitted")["code"].endswith("ADSERR_DEVICE_INVALIDACCESS")
    assert K.known_error("Target port not found – ADS server is not started (ADS)")["code"].endswith("ERR_TARGETPORTNOTFOUND")
    assert "'nFoo' is not declared" in K.known_error("identifier nFoo not defined")["cause"]
    r = k.search("C0046: identifier fbCounter not defined")
    assert r["known_error"] and r["hits"][0]["unit"] == "PRG_Machine", r
    r = k.search("C0046")
    assert r["known_error"] is None and "C-number alone is not documented locally" in r["known_errors_covered"], r


def _pou_m(folder: Path, name: str, decl: str, impl: str = "", methods=()) -> Path:
    """A POU with methods ((name, declaration, implementation), …)."""
    folder.mkdir(parents=True, exist_ok=True)
    ms = "".join(f'<Method Name="{m}" Id="{{bbbbbbbb-0000-0000-0000-0000000000{i:02d}}}"><Declaration><![CDATA[{d}]]>'
                 f'</Declaration><Implementation><ST><![CDATA[{b}]]></ST></Implementation></Method>'
                 for i, (m, d, b) in enumerate(methods))
    f = folder / f"{name}.TcPOU"
    f.write_text('<?xml version="1.0" encoding="utf-8"?>\n<TcPlcObject Version="1.1.0.1" ProductVersion="3.1.4024.12">\n'
                 f'  <POU Name="{name}" Id="{{aaaaaaaa-0000-0000-0000-0000000000{len(name):02d}}}" SpecialFunc="None">\n'
                 f"    <Declaration><![CDATA[{decl}]]></Declaration>\n"
                 f"    <Implementation><ST><![CDATA[{impl}]]></ST></Implementation>{ms}\n  </POU>\n</TcPlcObject>\n",
                 encoding="utf-8")
    return f


def _p15_like(src: Path) -> None:
    _pou_m(src, "FB_ModuleBase", "FUNCTION_BLOCK ABSTRACT FB_ModuleBase IMPLEMENTS I_Module\nVAR\n    bBusy : BOOL;\nEND_VAR",
           "", [("Execute", "METHOD Execute : BOOL", "Execute := TRUE;")])
    _pou_m(src, "FB_CounterModule", "FUNCTION_BLOCK FB_CounterModule EXTENDS FB_ModuleBase\nVAR\n    nCount : INT;\n"
           "    sName : STRING(40);\nEND_VAR", "",
           [("OnReset", "METHOD OnReset : BOOL", "nCount := 0;"), ("OnRunning", "METHOD OnRunning : BOOL", "nCount := nCount + 1;")])
    _pou_m(src, "FB_CounterModule_Tests", "FUNCTION_BLOCK FB_CounterModule_Tests\nVAR\n    fbUut : FB_CounterModule;\n"
           "END_VAR", "fbUut();")
    (src / "I_Module.TcIO").write_text(
        '<?xml version="1.0" encoding="utf-8"?><TcPlcObject><Itf Name="I_Module" Id="{cccccccc-0000-0000-0000-000000000001}">'
        "<Declaration><![CDATA[INTERFACE I_Module]]></Declaration><Method Name=\"Execute\" "
        'Id="{cccccccc-0000-0000-0000-000000000002}"><Declaration><![CDATA[METHOD Execute : BOOL]]></Declaration></Method>'
        "</Itf></TcPlcObject>", encoding="utf-8")
    (src / "E_State.TcDUT").write_text(
        '<?xml version="1.0" encoding="utf-8"?><TcPlcObject><DUT Name="E_State"><Declaration><![CDATA['
        "{attribute 'to_string'}\nTYPE E_State :\n(\n    Idle := 0,\n    Running := 40\n);\nEND_TYPE]]></Declaration>"
        "</DUT></TcPlcObject>", encoding="utf-8")


def test_n143_n144_n145_n146_kinds_members_own_name_and_sequences(kb, tmp_path):
    """0.8.14 retest §34c. N143: with kinds, the empty answer said 'no unit contains nCount' — units of other kinds
    do. N144: kinds did not see members: 'Execute kinds=[INTERFACE]' found nothing though I_Module declares the method
    Execute (unit I_Module.Execute, kind METHOD); kinds=[FB] found FB_ModuleBase only through a comment. N145:
    'CounterMod' listed FB_CounterModule's methods before FB_CounterModule. N146: 'STRING(40)' matched E_State
    ({attribute 'to_string'} + Running := 40) — tokens joined by punctuation are one sequence of the code."""
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    src = tmp_path / "p15"
    _p15_like(src)
    k.add_project(str(src), "p15")
    r = k.search("nCount", ["DUT"])
    assert r["hits"] == [] and "no DUT unit contains nCount" in r["note"], r.get("note")
    assert "of other kinds" in r["note"] and "FUNCTION_BLOCK" in r["note"] and "METHOD" in r["note"], r["note"]
    r = k.search("Execute", ["INTERFACE"])
    assert [(h["unit"], h["kind"], h.get("via_member"), h.get("declares")) for h in r["hits"]] == [
        ("I_Module", "INTERFACE", "I_Module.Execute", True)], r["hits"]
    r = k.search("Execute", ["FB"])
    assert [(h["unit"], h.get("via_member")) for h in r["hits"]] == [("FB_ModuleBase", "FB_ModuleBase.Execute")], r
    r = k.search("CounterMod")
    assert [h["unit"] for h in r["hits"]][:2] == ["FB_CounterModule", "FB_CounterModule_Tests"], [h["unit"] for h in r["hits"]]
    r = k.search("STRING(40)")
    assert [h["unit"] for h in r["hits"]] == ["FB_CounterModule"] and r["searched_as_sequence"] == ["STRING 40"], r
    r = k.search("FB_CounterModule.nCount")          # a qualified name stays two names: the FB declares the second
    assert r["hits"][0]["unit"] == "FB_CounterModule", r["hits"]


def test_n146_n147_n171_label_changed_files_and_placeholders(kb, tmp_path):
    """N146: the label said 'string literals or comments' for a unit whose code held one of the words — the words not
    in its code are named. N147: 'stale: files changed since it was indexed' named no file. N171: the placeholder 'X' of
    'C0046: Identifier 'X' not defined' was searched as a name (noise)."""
    import os
    import time as _t
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    src = tmp_path / "p15"
    _p15_like(src)
    _pou_m(src, "FB_Note", "FUNCTION_BLOCK FB_Note\nVAR\n    nLimit : INT := 40;\nEND_VAR", "// alarm text\n;")
    k.add_project(str(src), "p15")
    r = k.search("alarm nLimit")
    h = next(h for h in r["hits"] if h["unit"] == "FB_Note")
    assert h["words_not_in_code"] == ["alarm"], h
    f = src / "FB_Note.TcPOU"
    _t.sleep(0.01)
    f.write_text(f.read_text().replace("40", "41"), encoding="utf-8")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 5))
    (src / "FB_New.TcPOU").write_text((src / "FB_Note.TcPOU").read_text().replace("FB_Note", "FB_New"), encoding="utf-8")
    (src / "E_State.TcDUT").unlink()
    r = k.search("nCount")
    st = r["stale"][0]
    assert st["changed_files"] == [str(f)] and st["added_files"] == [str(src / "FB_New.TcPOU")], st
    assert st["removed_files"] == [str(src / "E_State.TcDUT")], st
    r = k.search("C0046: Identifier 'X' not defined")
    assert r["hits"] == [] and "X" in r["words_ignored"] and "single character" in r["words_ignored"]["X"], r
    assert r["known_error"], r
