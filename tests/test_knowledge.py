"""Knowledge base (decisions of 2026-10-17): location chosen by the user under stated conditions, known errors,
library versions from the .plcproj, ESI hardware lookup, pattern search over user-added folders only."""
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
        '<PlaceholderReference Include="Tc2_Standard">'))
    rep = K.library_report([str(pp)], k.installed_libraries())
    libs = {x["library"]: x for x in rep["projects"][0]["libraries"]}
    assert libs["Tc2_Standard"]["requested"] == "*" and libs["Tc2_Standard"]["effective"] == "3.4.5.0"
    assert libs["Tc2_Standard"]["installed"] == ["3.3.3.0", "3.4.5.0"]
    assert "not installed on this PC" in libs["Tc2_Smtp"]["warning"]
