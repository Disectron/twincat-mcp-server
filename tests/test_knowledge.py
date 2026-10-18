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
    assert "previous database stays" in r2["note"] and (tmp / "kbdata" / K.DB_NAME).exists()


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
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    r = k.hw_lookup("EL2008")
    d = r["devices"][0]
    assert r["indexed_now"]["devices_added"] == 2 and d["product_code"] == "#x07D83052" and d["group"] == "DigOut"
    rx = [p for p in d["pdo"] if p["index"] == "#x1601"][0]
    assert [e["index"] for e in rx["entries"]] == ["#x7010"]          # padding entries dropped
    assert d["coe_objects"][0] == {"index": "#x1018", "name": "Identity", "type": "DT1018"}
    assert k.hw_lookup("#x0bf03052")["devices"][0]["type"] == "EL3062"
    assert k.hw_lookup("Ana. Input")["types"] == ["EL3062"]
    assert "indexed_now" not in k.hw_lookup("EL2008")                   # second call: from the database


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
    rows = list(K._parse_esi(Path(__file__).parent / "fixtures" / "esi_namespaced.xml"))
    assert len(rows) == 1
    typ, name, pc, rev, vendor, group, _f, pdos, coe = rows[0]
    pdos, coe = json.loads(pdos), json.loads(coe)
    assert typ == "EL3102" and name.startswith("EL3102 2Ch.") and vendor.startswith("Beckhoff") and group == "AnaIn"
    assert pdos[0]["index"] == "#x1A00" and pdos[0]["name"] == "AI Standard Channel 1"
    assert [(e["index"], e["sub"], e["bits"]) for e in pdos[0]["entries"]] == [("#x6000", 1, 1), ("#x6000", 17, 16)]
    assert coe == [{"index": "#x8000", "name": "AI Settings Ch.1", "type": "DT8000"}]


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


def _kb_with_docs(tmp: Path) -> Path:
    import shutil
    loc = tmp / "kbdocs"
    shutil.copytree(Path(__file__).parent / "fixtures" / "kb", loc, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    (loc / "manuals" / "IO").mkdir(parents=True)
    (loc / "manuals" / "IO" / "EL3102.pdf").write_bytes(b"%PDF-1.4 not indexed yet")
    (loc / "sop" / "scope").mkdir(parents=True)
    (loc / "sop" / "scope" / "SOP-scope-display.md").write_text("# Scope", encoding="utf-8")
    (loc / "tools").mkdir()
    shipped = K.builders_dir() / "manuals_db.py"              # a copy of the builder the plugin ships, CRLF on disk
    (loc / "tools" / "manuals_db.py").write_bytes(shipped.read_bytes().replace(b"\n", b"\r\n"))
    return loc


def test_status_reports_the_offline_documentation_layout(kb):
    """kb_status names what the twincat-docs skill reads in the same folder (built by the folder's own tools)."""
    k, tmp = kb
    loc = _kb_with_docs(tmp)
    k.set_location(str(loc))
    d = k.status()["docs"]
    assert d["skill"] == "twincat-docs" and str(K.docs_skill_path()) in d["call"] and str(loc) in d["call"]
    assert d["infosys"]["exists"] and d["infosys"]["pages"] == 8 and d["infosys"]["books"] == 6
    assert d["infosys"]["package"] == "BKINFOSYS3_FIXTURE_EN-US.mshc" and d["infosys"]["lcid"] == "1033"
    assert d["infosys"]["state"] == "complete" and d["infosys"]["links_resolved"] == 3 and "command" not in d["infosys"]
    m = d["manuals"]
    assert m["manuals"] == 1 and m["pdfs_in_folder"] == 1 and m["not_indexed"] == ["EL3102.pdf"]
    assert m["indexed_but_gone"] == ["CX9020.pdf"]                   # the fixture's PDF path is not in this folder
    # the old copy in tools\ became the stub when the folder was chosen: the command runs it from the knowledge folder
    assert m["state"] == "pending" and m["command"].startswith(f'<Python 3.12+> "{loc / "tools" / "manuals_db.py"}" update --kb')
    b = d["builders"]
    assert b["infosys_local.py"]["shipped_version"] == "2" and b["infosys_local.py"]["copy_in_tools"] is None
    assert b["manuals_db.py"]["copy_kind"] == "stub" and "nothing to update" in b["manuals_db.py"]["note"]
    assert (loc / "tools" / f"manuals_db.py.v{K._builder_version(K.builders_dir() / 'manuals_db.py')}.bak").is_file()
    assert d["sop"] == [str(Path("sop") / "scope" / "SOP-scope-display.md")]
    # read-only: nothing next to the databases was created
    assert sorted(p.name for p in (loc / "helpviewer").iterdir()) == ["infosys_en.db"]
    assert K.docs_skill_path().is_file()


def test_set_location_names_the_docs_it_finds(kb):
    k, tmp = kb
    r = k.set_location(str(tmp / "empty_kb"))
    assert r["docs"]["infosys"]["exists"] is False and r["docs"]["infosys"]["state"] == "missing"
    assert 'infosys_local.py" index --kb' in r["docs"]["infosys"]["command"]
    r2 = k.set_location(str(_kb_with_docs(tmp)))
    assert r2["docs"]["infosys"]["exists"] and r2["docs"]["manuals"]["exists"]


def test_hw_lookup_adds_the_manuals_that_mention_the_product(kb):
    k, tmp = kb
    k.set_location(str(tmp / "kbdata"))
    r = k.hw_lookup("EL2008")
    assert "manuals" not in r and "no manuals database" in r["manuals_note"]
    k.set_location(str(_kb_with_docs(tmp)))
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
    assert k2.status()["database"].endswith(K.DB_NAME) and (tmp_path / "configured_kb").is_dir()


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


def test_n34_hw_lookup_takes_the_order_code_out_of_the_esi_type(kb, monkeypatch):
    """N34: hw_lookup CX9020 looked up the ESI device name 'CX9020 EtherCAT slave' in the manuals (no match)."""
    k, tmp = kb
    (tmp / "esi" / "Beckhoff CX.xml").write_text(
        '<?xml version="1.0"?><EtherCATInfo><Vendor><Id>#x2</Id><Name>Beckhoff</Name></Vendor><Descriptions><Devices>'
        '<Device><Type ProductCode="#x23446052" RevisionNo="#x1">CX9020 EtherCAT slave</Type>'
        '<Name LcId="1033">CX9020 EtherCAT slave</Name></Device></Devices></Descriptions></EtherCATInfo>', encoding="utf-8")
    k.set_location(str(_kb_with_docs(tmp)))
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
    assert K.builder_kind(loc / "tools" / "manuals_db.py", "manuals_db.py") == ("stub", "1")
    # an edited copy of the shipped builder is the user's file now: left alone
    edited = (K.builders_dir() / "infosys_local.py").read_text(encoding="utf-8") + "\n# my change\n"
    (loc / "tools" / "infosys_local.py").write_text(edited, encoding="utf-8")
    assert K.builder_kind(loc / "tools" / "infosys_local.py", "infosys_local.py") == ("other", None)
    # every later start: nothing to do; the pointer for the stubs is written once per plugin location/version
    up = k.builder_upkeep("9.9.9")
    assert up["tools"]["manuals_db.py"] == {"state": "stub", "stub_version": "1"} and up["pointer_written"] is True
    assert up["tools"]["infosys_local.py"]["state"] == "left alone"
    assert k.builder_upkeep("9.9.9")["pointer_written"] is False
    ptr = json.loads((k.home / "plugin_location.json").read_text(encoding="utf-8"))
    assert Path(ptr["builders"]) == K.builders_dir() and ptr["version"] == "9.9.9"
    d = k.docs_layout(loc)
    b = d["builders"]["manuals_db.py"]
    assert b["copy_kind"] == "stub" and "nothing to update" in b["note"]
    assert d["builders"]["infosys_local.py"]["copy_kind"] == "other"
    assert "left alone" in d["builders"]["infosys_local.py"]["note"]
    # the user's own script is never offered as the command — the plugin's builder is
    assert str(K.builders_dir() / "infosys_local.py") in d["infosys"]["command"]
