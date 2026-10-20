"""The ESI index (twincat_mcp/esi_index.py, index\\esi.db): what is read from each ESI document kind, the rules for
duplicates, the build discipline (rule 6/7 of docs/KNOWLEDGE_BASE.md) and the readers. The fixtures mirror the
structures of Beckhoff's real files (EL33xx with its dictionary, mappings and DC; the compact 'EtherCAT Terminals'
file with PDO templates; AX5000 with DictionaryFile / DiagFile; AX8000 module files per revision)."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from twincat_mcp import esi_index as E
from twincat_mcp import kb_lock

VENDOR = """<Vendor FileVersion="{fv}"><Id>2</Id><Name>Beckhoff Automation GmbH &amp; Co. KG</Name></Vendor>"""

EL33 = """<?xml version="1.0" encoding="iso-8859-1"?>
<EtherCATInfo Version="1.2">
  {vendor}
  <Descriptions>
    <Groups><Group><Type>AnaIn</Type><Name LcId="1033">Analog Input Terminals (EL3xxx)</Name></Group></Groups>
    <Devices>
      <Device Physics="KK">
        <Type ProductCode="#x0d1c3052" RevisionNo="#x0019000a">EL3356-0010</Type>
        <HideType RevisionNo="#x0018000a"/>
        <Name LcId="1031">EL3356-0010 1K. Ana. Eingang</Name>
        <Name LcId="1033">EL3356-0010 1Ch. Ana. Input Resistor Bridge</Name>
        <URL LcId="1033">http://www.beckhoff.com/EL3356</URL>
        <Info><Electrical><EBusCurrent>280</EBusCurrent></Electrical>
          <VendorSpecific><TwinCAT>
            <AlternativeSmMapping Default="1"><Name>Standard (INT32)</Name>
              <Sm No="2"><Pdo>#x1600</Pdo></Sm><Sm No="3"><Pdo>#x1a00</Pdo><Pdo>#x1a01</Pdo></Sm>
              <OperationMode><DcOpModeName>Synchron</DcOpModeName>
                <InitCmds><Transition>PS</Transition><Index>#x8000</Index><SubIndex>17</SubIndex><Data>0100</Data></InitCmds>
              </OperationMode>
            </AlternativeSmMapping>
            <AlternativeSmMapping><Name>Compact</Name><Sm No="3"><Pdo>#x1a02</Pdo><ExcludedPdo>#x1a00</ExcludedPdo></Sm>
            </AlternativeSmMapping>
          </TwinCAT></VendorSpecific>
        </Info>
        <GroupType>AnaIn</GroupType>
        <Profile><ProfileNo>5001</ProfileNo><ChannelCount>1</ChannelCount>
          <Dictionary>
            <DataTypes>
              <DataType><Name>DT0800EN16</Name><BaseType>UINT</BaseType><BitSize>16</BitSize>
                <EnumInfo><Text LcId="1033">FIR 50 Hz</Text><Enum>0</Enum></EnumInfo>
                <EnumInfo><Text LcId="1033">IIR1</Text><Enum>2</Enum></EnumInfo>
              </DataType>
              <DataType><Name>DT8000</Name><BitSize>80</BitSize>
                <SubItem><SubIdx>0</SubIdx><Name>SubIndex 000</Name><Type>USINT</Type><BitSize>8</BitSize><BitOffs>0</BitOffs>
                  <Flags><Access>ro</Access></Flags></SubItem>
                <SubItem><SubIdx>1</SubIdx><Name>Mode0 enable filter</Name><Type>BOOL</Type><BitSize>1</BitSize><BitOffs>16</BitOffs>
                  <Flags><Access>rw</Access><Backup>1</Backup><Setting>1</Setting></Flags></SubItem>
                <SubItem><SubIdx>17</SubIdx><Name>Mode0 filter settings</Name><Type>DT0800EN16</Type><BitSize>16</BitSize><BitOffs>32</BitOffs>
                  <Flags><Access>rw</Access></Flags></SubItem>
                <SubItem><SubIdx>20</SubIdx><Name>Dynamic filter delta</Name><Type>REAL</Type><BitSize>32</BitSize><BitOffs>48</BitOffs>
                  <Flags><Access>rw</Access></Flags></SubItem>
              </DataType>
              <DataType><Name>DT1C12ARR</Name><BaseType>UINT</BaseType><BitSize>32</BitSize>
                <ArrayInfo><LBound>1</LBound><Elements>2</Elements></ArrayInfo></DataType>
              <DataType><Name>DT1C12</Name><BitSize>48</BitSize>
                <SubItem><SubIdx>0</SubIdx><Name>SubIndex 000</Name><Type>USINT</Type><BitSize>8</BitSize><BitOffs>0</BitOffs>
                  <Flags><Access>rw</Access></Flags></SubItem>
                <SubItem><Name>Elements</Name><Type>DT1C12ARR</Type><BitSize>32</BitSize><BitOffs>16</BitOffs>
                  <Flags><Access>rw</Access></Flags></SubItem>
              </DataType>
            </DataTypes>
            <Objects>
              <Object><Index>#x1018</Index><Name>Identity</Name><Type>UDINT</Type><BitSize>32</BitSize>
                <Info><DefaultData>52301c0d</DefaultData></Info><Flags><Access>ro</Access></Flags></Object>
              <Object><Index>#x1c12</Index><Name>RxPDO assign</Name><Type>DT1C12</Type><BitSize>48</BitSize>
                <Info><SubItem><Name>SubIndex 000</Name><Info><DefaultData>02</DefaultData></Info></SubItem>
                  <SubItem><Name>SubIndex 001</Name><Info><DefaultData>0016</DefaultData></Info></SubItem>
                  <SubItem><Name>SubIndex 002</Name><Info><DefaultData>0116</DefaultData></Info></SubItem></Info>
                <Flags><Access>rw</Access></Flags></Object>
              <Object><Index>#x8000</Index><Name>RMB Settings</Name><Type>DT8000</Type><BitSize>80</BitSize>
                <Comment LcId="1033">Settings of the bridge input</Comment>
                <Info><SubItem><Name>SubIndex 000</Name><Info><DefaultData>14</DefaultData></Info></SubItem>
                  <SubItem><Name>Mode0 enable filter</Name><Info><DefaultData>01</DefaultData></Info></SubItem>
                  <SubItem><Name>Mode0 filter settings</Name><Info><DefaultData>0200</DefaultData></Info></SubItem>
                  <SubItem><Name>Dynamic filter delta</Name><Info><DefaultData>0000a041</DefaultData>
                    <MinValue>#x1</MinValue><MaxValue>#x64</MaxValue><Unit>#xfe030000</Unit></Info></SubItem></Info>
                <Flags><Access>rw</Access></Flags></Object>
            </Objects>
          </Dictionary>
        </Profile>
        <Fmmu>Outputs</Fmmu><Fmmu>Inputs</Fmmu>
        <Sm DefaultSize="128" StartAddress="#x1000" ControlByte="#x26" Enable="1">MBoxOut</Sm>
        <Sm StartAddress="#x1100" ControlByte="#x24" Enable="1">Outputs</Sm>
        <RxPdo Fixed="1" Sm="2"><Index>#x1600</Index><Name>RMB Control</Name>
          <Entry><Index>#x7000</Index><SubIndex>1</SubIndex><BitLen>1</BitLen><Name>Start calibration</Name><DataType>BOOL</DataType></Entry>
          <Entry><Index>#x0</Index><BitLen>15</BitLen></Entry>
        </RxPdo>
        <TxPdo Fixed="1" Sm="3"><Index>#x1a00</Index><Name>RMB Status</Name><Exclude>#x1a02</Exclude>
          <Entry><Index>#x6000</Index><SubIndex>1</SubIndex><BitLen>1</BitLen><Name>Underrange</Name><DataType>BOOL</DataType></Entry>
          <Entry><Index>#x0</Index><BitLen>15</BitLen></Entry>
        </TxPdo>
        <TxPdo Sm="3"><Index>#x1a01</Index><Name>RMB Value (INT32)</Name>
          <Entry><Index>#x6000</Index><SubIndex>17</SubIndex><BitLen>32</BitLen><Name>Value</Name><DataType>DINT</DataType></Entry>
        </TxPdo>
        <TxPdo Sm="3" Virtual="1"><Index>#x1a02</Index><Name>RMB Value (configured)</Name></TxPdo>
        <Mailbox DataLinkLayer="1"><CoE SdoInfo="1"/><FoE/></Mailbox>
        <Dc><OpMode><Name>Synchron</Name><Desc>SM-Synchron</Desc><AssignActivate>#x0</AssignActivate></OpMode>
          <OpMode><Name>DC</Name><Desc>DC-Synchron</Desc><AssignActivate>#x320</AssignActivate>
            <CycleTimeSync0 Factor="1">0</CycleTimeSync0></OpMode></Dc>
        <Slots MaxSlotCount="3" SlotIndexIncrement="16">
          <Slot MinInstances="0" MaxInstances="1"><Name>TSC Connection</Name><ModuleIdent Default="1">#x00000cf2</ModuleIdent></Slot>
          <Slot MinInstances="0" MaxInstances="1"><Name>Empty Slot</Name><ModuleIdent>#x0</ModuleIdent></Slot>
          <Slot MinInstances="0" MaxInstances="1"><Name>Empty Slot</Name><ModuleIdent>#x0</ModuleIdent></Slot>
        </Slots>
      </Device>
      <Device Physics="KK">
        <Type ProductCode="#x0d1c3052" RevisionNo="#x0018000a">EL3356-0010</Type>
        <Name LcId="1033">EL3356-0010 1Ch. Ana. Input Resistor Bridge</Name>
        <GroupType>AnaIn</GroupType>
        <TxPdo Fixed="1" Sm="3"><Index>#x1a00</Index><Name>RMB Status</Name>
          <Entry><Index>#x6000</Index><SubIndex>1</SubIndex><BitLen>1</BitLen><Name>Underrange</Name><DataType>BOOL</DataType></Entry>
        </TxPdo>
      </Device>
    </Devices>
    <Modules>
      <Module><Type ModuleIdent="#x00000cf2">EL3356-0010</Type><Name>TSC</Name>
        <TxPdo Sm="3"><Index DependOnSlot="1">#x1a10</Index><Name>TSC Inputs</Name>
          <Entry><Index>#x6040</Index><SubIndex>1</SubIndex><BitLen>8</BitLen><Name>Slave Cmd</Name><DataType>USINT</DataType></Entry>
        </TxPdo></Module>
      <Module><Type ModuleIdent="#x00000000">empty</Type><Name>Empty Module</Name></Module>
    </Modules>
  </Descriptions>
</EtherCATInfo>
"""

TERMINALS = """<?xml version="1.0" encoding="iso-8859-1"?>
<EtherCATInfo Version="1.2">
  {vendor}
  <Descriptions>
    <Groups><Group><Type>DigIn</Type><Name LcId="1033">Digital Input Terminals</Name></Group></Groups>
    <Devices>
      <Device Physics="YY">
        <Type ProductCode="#x03ea3052" RevisionNo="#x270b0000">EL1002</Type>
        <Name LcId="1033">EL1002 2Ch. Dig. Input 24V, 3ms</Name>
        <GroupType>DigIn</GroupType>
        <TxPdo Ref="DigInputBit" Chn="0" Fixed="1" Mandatory="1" Sm="0"/>
        <TxPdo Ref="DigInputBit" Chn="1" Fixed="1" Mandatory="1" Sm="0"/>
        <TxPdo Ref="NotThere" Sm="0"/>
      </Device>
    </Devices>
    <Pdos>
      <Pdo RefName="DigInputBit">
        <Index ChnFac="1">#x1a00</Index><Name ChnFac="1">Channel #1</Name>
        <Entry><Index>#x3101</Index><SubIndex ChnFac="1">1</SubIndex><BitLen>1</BitLen><Name>Input</Name><DataType>BOOL</DataType></Entry>
      </Pdo>
    </Pdos>
  </Descriptions>
</EtherCATInfo>
"""

AX5 = """<?xml version="1.0" encoding="iso-8859-1"?>
<EtherCATInfo Version="1.2">
  {vendor}
  <Descriptions><Groups/><Devices>
    <Device Physics="YY">
      <Type ProductCode="#x14366012" RevisionNo="#x00d80000">AX5206-0000-0216</Type>
      <Name LcId="1033">AX5206-0000-0216 EtherCAT Drive (SoE, 2 Ch.)</Name>
      <Profile><ProfileNo>1</ProfileNo>
        <DictionaryFile>Beckhoff AX5xxx\\Beckhoff AX5206-xxxx-0216 Dict.xml</DictionaryFile>
        <DiagFile>Beckhoff AX5xxx\\Beckhoff AX5xxx-xxxx-xxxx Diag.xml</DiagFile>
      </Profile>
      <Mailbox><SoE/></Mailbox>
    </Device>
    <Device Physics="YY">
      <Type ProductCode="#x14336012" RevisionNo="#x00d80000">AX5203-0000-0216</Type>
      <Name LcId="1033">AX5203-0000-0216 EtherCAT Drive (SoE, 2 Ch.)</Name>
      <Profile><DictionaryFile>Beckhoff AX5xxx\\Beckhoff AX5203-xxxx-0216 Dict.xml</DictionaryFile></Profile>
    </Device>
  </Devices></Descriptions>
</EtherCATInfo>
"""

AX5_DICT = """<?xml version="1.0" encoding="iso-8859-1"?>
<EtherCATDict Version="1.0">
  <Dictionary>
    <DataTypes><DataType><Name>UINT</Name><BitSize>16</BitSize></DataType></DataTypes>
    <Objects>
      <Object><Index>0001</Index><Name>Control unit cycle time (TNcyc)</Name><Type>UINT</Type><BitSize>16</BitSize>
        <Info><DefaultData>e803</DefaultData></Info><Flags><Access>ro</Access></Flags></Object>
      <Object><Index>0410</Index><Name>Communication cycle time</Name><Type>UINT</Type><BitSize>16</BitSize></Object>
    </Objects>
  </Dictionary>
</EtherCATDict>
"""

AX5_DIAG = """<?xml version="1.0" encoding="utf-8"?>
<EtherCATDiag Version="2.010">
  <DiagMessages>
    <DiagReset><Text LcId="1033">Execute Reset-Command (S-0-0099).</Text><Enum>2</Enum></DiagReset>
    <DiagReaction><Text LcId="1033">Torque off</Text><Enum>1</Enum></DiagReaction>
    <DiagType><Text LcId="1033">Hardware error</Text><Enum>3</Enum></DiagType>
    <DiagMessage><TextId>#xD196</TextId><MessageText LcId="1033"><![CDATA[%s SCI: Buffer overflow]]></MessageText>
      <Description LcId="1033">SCI communication Control&lt;->Front unit: Buffer overflow</Description>
      <CauseRemedy><Cause LcId="1033">EMC problems</Cause><Remedy LcId="1033">Check the shielding</Remedy></CauseRemedy>
      <Flags><DiagClass>error</DiagClass><DiagReset>2</DiagReset><DiagReaction>1</DiagReaction><DiagType>3</DiagType></Flags>
    </DiagMessage>
  </DiagMessages>
</EtherCATDiag>
"""

AX8_DEV = """<?xml version="1.0" encoding="iso-8859-1"?>
<EtherCATInfo Version="1.2">
  {vendor}
  <Descriptions><Groups/><Devices>
    <Device Physics="YY">
      <Type ProductCode="#x22066012" RevisionNo="#x006b0000">AX8206-0210-{rev}</Type>
      <Name LcId="1033">AX8206-0210-{rev} Servo drive</Name>
      <Slots MaxSlotCount="96">
        <Slot MinInstances="1" MaxInstances="1"><Name>DeviceMainSlot</Name><ModuleIdent Default="1">#x006b0064</ModuleIdent></Slot>
      </Slots>
    </Device>
  </Devices></Descriptions>
</EtherCATInfo>
"""

AX8_MODULES = """<?xml version="1.0" encoding="iso-8859-1"?>
<EtherCATModule Version="1.2">
  {vendor}
  <Modules>
    <Module><Type ModuleIdent="#x006b0064">DeviceMain</Type><Name>DeviceMain {rev}</Name>
      <RxPdo Sm="2"><Index DependOnSlot="1">#x1600</Index><Name>DeviceMain Outputs</Name>
        <Entry Fixed="1"><Index>#x2c00</Index><SubIndex>1</SubIndex><BitLen>16</BitLen><Name>Power supply controlword</Name>
          <Comment>linked to the Statusword of the power supply</Comment><DataType>UINT</DataType></Entry>
      </RxPdo>
      <Profile><DiagMessages><DiagMessage><TextId>#x4101</TextId><MessageText LcId="1033">Overtemperature {rev}</MessageText>
      </DiagMessage></DiagMessages></Profile>
    </Module>
  </Modules>
</EtherCATModule>
"""


def _write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture
def esi_kb(tmp_path):
    """A knowledge folder with a copy of Beckhoff's ESI folder in manuals\\, and a 'TwinCAT ESI folder' (extra)."""
    kb = tmp_path / "kb"
    m = kb / "manuals" / "Beckhoff_EtherCAT_XML"
    _write(m / "Beckhoff EL33xx.xml", EL33.format(vendor=VENDOR.format(fv=10)))
    _write(m / "Beckhoff EtherCAT Terminals.xml", TERMINALS.format(vendor=VENDOR.format(fv=3)))
    _write(m / "Beckhoff AX5xxx.xml", AX5.format(vendor=VENDOR.format(fv=4)))
    _write(m / "Beckhoff AX5xxx" / "Beckhoff AX5206-xxxx-0216 Dict.xml", AX5_DICT)
    _write(m / "Beckhoff AX5xxx" / "Beckhoff AX5xxx-xxxx-xxxx Diag.xml", AX5_DIAG)
    for rev in ("0106", "0107"):
        _write(m / f"Beckhoff AX8yxx-zzzz-{rev}.xml", AX8_DEV.format(vendor=VENDOR.format(fv=19), rev=rev))
        _write(m / "Beckhoff AX8yxx" / f"Beckhoff AX8yxx-zzzz-{rev} Modules.xml",
               AX8_MODULES.format(vendor=VENDOR.format(fv=19), rev=rev))
    (m / "RES").mkdir()
    (m / "RES" / "EL3356-0010_standard.GIF").write_bytes(b"GIF89a")
    _write(kb / "manuals" / "notes.xml", "<?xml version='1.0'?><Notes><n>not an ESI</n></Notes>")
    tc = tmp_path / "TwinCAT" / "EtherCAT"
    # TwinCAT's own copy: the same Terminals file (identical) and an older EL33xx (FileVersion 9, other content)
    _write(tc / "Beckhoff EtherCAT Terminals.xml", TERMINALS.format(vendor=VENDOR.format(fv=3)))
    _write(tc / "Beckhoff EL33xx.xml", EL33.format(vendor=VENDOR.format(fv=9)).replace("Resistor Bridge", "Bridge"))
    db = kb / "index" / "esi.db"
    return kb, tc, db


def _build(esi_kb, **kw):
    kb, tc, db = esi_kb
    return E.build(db, kb, [tc], twincat=False, log=lambda m: None, **kw)


def test_every_document_kind_is_read_and_counted(esi_kb):
    kb, tc, db = esi_kb
    r = _build(esi_kb)
    assert r["result"] == "built" and r["kinds"] == {"info": 6, "dict": 1, "diag": 1, "module": 2}, r
    # EL3356-0010 ×2, EL1002, AX5206, AX5203, AX8206-0210-0106/-0107
    assert r["devices"] == 7 and r["types"] == 6, r
    assert r["missing_refs"] == ["AX5203-0000-0216: Beckhoff AX5xxx\\Beckhoff AX5203-xxxx-0216 Dict.xml"]
    assert r["not_esi"] == [str(kb / "manuals" / "notes.xml")] and r["errors"] == []
    st = E.state(db, kb, [tc], twincat=False)
    assert st["state"] == "complete" and st["devices"] == 7 and st["missing_refs"]["count"] == 1, st


def test_a_device_is_read_whole(esi_kb):
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    r = x.lookup("EL3356-0010")
    assert r["matches"] == 2 and [d["revision"] for d in r["devices"]] == ["#x0019000A", "#x0018000A"]
    d = r["devices"][0]
    assert (d["product_code"], d["ebus_ma"], d["url"], d["group"]) == (
        "#x0D1C3052", 280, "http://www.beckhoff.com/EL3356", "Analog Input Terminals (EL3xxx)")
    assert d["name_de"] == "EL3356-0010 1K. Ana. Eingang" and d["physics"] == "KK" and d["file_version"] == 10
    assert d["image"].endswith("EL3356-0010_standard.GIF") and d["mailbox"] == ["CoE", "FoE"]
    assert d["hides"] == [{"revision": "#x0018000a"}]
    assert [m["name"] for m in d["dc"]] == ["Synchron", "DC"] and d["dc"][1]["assign_activate"] == "#x320"
    assert [(s["name"], s["start"], s["size"]) for s in d["sm"]] == [("MBoxOut", "#x1000", 128), ("Outputs", "#x1100", None)]
    # every PDO entry kept — a gap occupies its bits; an excluded PDO and an empty virtual one said as such
    rx = d["pdo"][0]
    assert rx["index"] == "#x1600" and rx["bits"] == 16 and [e.get("gap", False) for e in rx["entries"]] == [False, True]
    tx = {p["index"]: p for p in d["pdo"]}
    assert tx["#x1A00"]["excludes"] == ["#x1A02"] and tx["#x1A02"]["virtual"] is True
    assert "marked Virtual" in tx["#x1A02"]["entries_note"] and tx["#x1A01"]["entries"][0]["type"] == "DINT"
    # PDO assignments with the DC mode and init commands
    a = d["pdo_assignments"]
    assert [(m["name"], m["default"]) for m in a] == [("Standard (INT32)", True), ("Compact", False)]
    assert a[0]["sm"][1] == {"sm": 3, "pdos": ["#x1a00", "#x1a01"]} and a[0]["dc_mode"] == "Synchron"
    assert a[0]["init_cmds"] == [{"transition": "PS", "index": "#x8000", "sub": "17", "data": "0100"}]
    assert a[1]["sm"][0]["excluded"] == ["#x1a00"]
    # slots: repeated identical slots counted, modules resolved from the device's own file
    sl = d["slots"]["slots"]
    assert [(s["name"], s.get("count", 1)) for s in sl] == [("TSC Connection", 1), ("Empty Slot", 2)]
    assert sl[0]["modules"][0] == {"ident": "#x00000CF2", "default": True, "module": "EL3356-0010", "name": "TSC",
                                   "from": "Beckhoff EL33xx.xml", "how": "the device's own file"}
    assert [o["index"] for o in d["coe_objects"]] == ["#x1018", "#x1C12", "#x8000"] and d["coe_count"] == 3
    # the older revision: one line (detail newest)
    assert "pdo" not in r["devices"][1] and "detail='full'" in r["detail_note"]


def test_a_coe_object_with_its_subindices_defaults_and_enums(esi_kb):
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    o = x.object("EL3356-0010", "#x8000")
    assert o["comment"] == "Settings of the bridge input" and o["revision"] == "#x0019000A"
    s = {i["sub"]: i for i in o["subitems"]}
    assert s[0]["default"] == 20 and s[1]["default"] is True and s[1]["backup"] and s[1]["setting"]
    assert s[17]["default"] == {"value": 2, "text": "IIR1"} and s[17]["enum"] == {"0": "FIR 50 Hz", "2": "IIR1"}
    assert s[20]["default"] == 20.0 and (s[20]["min"], s[20]["max"], s[20]["unit_code"]) == (1, 100, "#xfe030000")
    assert s[20]["bit_offs"] == 48 and s[20]["bits"] == 32
    # an array: its elements one by one, with their own defaults and bit offsets
    a = x.object("EL3356-0010", "1c12")
    assert [(i["sub"], i["name"], i["type"], i["bits"], i["bit_offs"], i.get("default")) for i in a["subitems"]] == [
        (0, "SubIndex 000", "USINT", 8, 0, 2), (1, "SubIndex 001", "UINT", 16, 16, 0x1600),
        (2, "SubIndex 002", "UINT", 16, 32, 0x1601)]
    assert x.object("EL3356-0010", "0x1018")["default"] == 0x0D1C3052          # DefaultData is little-endian
    with pytest.raises(E.EsiError, match=r"has no object #x8010; the nearest") as ei:
        x.object("EL3356-0010", "#x8010")
    assert ei.value.code == "not_found"
    with pytest.raises(E.EsiError, match="is not in the ESI index — types with that text"):
        x.object("EL3356", "#x8000")
    assert x.object("EL3356-0010", "#x8000", revision="#x0019000a")["revision"] == "#x0019000A"


def test_pdo_templates_are_resolved_per_channel(esi_kb):
    kb, tc, db = esi_kb
    _build(esi_kb)
    d = E.EsiIndex(db).lookup("EL1002")["devices"][0]
    p = d["pdo"]
    assert [(x["index"], x["name"], x["entries"][0]["sub"], x.get("template")) for x in p[:2]] == [
        ("#x1A00", "Channel 1", 1, "DigInputBit"), ("#x1A01", "Channel 2", 2, "DigInputBit (channel 1)")]
    assert p[2]["unresolved"] == "template 'NotThere' is not in Beckhoff EtherCAT Terminals.xml"


def test_dictionary_and_diagnosis_files_named_by_a_device(esi_kb):
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    d = x.lookup("AX5206-0000-0216")["devices"][0]
    assert d["coe_count"] == 2 and d["diag_messages"] == 1 and d["mailbox"] == ["SoE"]
    assert x.object("AX5206-0000-0216", 1)["default"] == 1000          # index '0001' (decimal in the Dict file)
    assert x.object("AX5206-0000-0216", 410)["name"] == "Communication cycle time"
    g = x.diag("#xD196", "AX5206-0000-0216")["messages"][0]
    assert g["message"] == "%s SCI: Buffer overflow" and g["causes"] == [{"cause": "EMC problems",
                                                                          "remedy": "Check the shielding"}]
    assert (g["class"], g["reset"], g["reaction"], g["type"]) == ("error", "Execute Reset-Command (S-0-0099).",
                                                                  "Torque off", "Hardware error")
    miss = x.lookup("AX5203-0000-0216")["devices"][0]
    assert "is not in the ESI folders" in miss["dictionary_note"] and miss["coe_count"] == 0
    with pytest.raises(E.EsiError, match="no object dictionary for it .the dictionary file"):
        x.object("AX5203-0000-0216", 1)


def test_modules_are_kept_per_file_and_resolved_by_rule(esi_kb):
    """The AX8000 module files of revisions 0106 and 0107 hold the same ModuleIdent with different content: a device
    of each revision gets its own (the module file paired by name), never 'the newest'."""
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    for rev in ("0106", "0107"):
        m = x.lookup(f"AX8206-0210-{rev}")["devices"][0]["slots"]["slots"][0]["modules"][0]
        assert (m["module"], m["name"], m["from"], m["how"]) == (
            "DeviceMain", f"DeviceMain {rev}", f"Beckhoff AX8yxx-zzzz-{rev} Modules.xml",
            "the module file of this device file"), m
    one = x.module("#x006b0064", device="AX8206-0210-0107")
    assert one["found"] == 1 and one["modules"][0]["name"] == "DeviceMain 0107" and one["how"].startswith("the module")
    p = one["modules"][0]["pdo"][0]
    assert p["index_depends_on"] == "slot" and p["entries"][0]["comment"].startswith("linked to")
    both = x.module("#x006b0064")
    assert both["found"] == 2 and {m["name"] for m in both["modules"]} == {"DeviceMain 0106", "DeviceMain 0107"}
    # the module's diagnosis catalogue: who uses a message is named (modules too)
    d = x.diag("#x4101")
    assert d["found"] == 2 and all(m["devices"]["modules"]["first"] == ["DeviceMain"] for m in d["messages"])


def test_duplicates_across_folders(esi_kb):
    """The same file in two folders is read once (also_in); one revision described differently by two files: the
    higher vendor FileVersion is kept, the other is named (differs_in)."""
    kb, tc, db = esi_kb
    r = _build(esi_kb)
    assert r["files_read"] == 10 and r["files"] == 12, r            # 12 XML files: notes.xml is no ESI, Terminals once
    x = E.EsiIndex(db)
    el1002 = x.lookup("EL1002")["devices"][0]
    assert el1002["also_in"] == [str(tc / "Beckhoff EtherCAT Terminals.xml")]
    el = x.lookup("EL3356-0010")["devices"][0]
    assert el["file"].endswith(str(Path("Beckhoff_EtherCAT_XML") / "Beckhoff EL33xx.xml")) and el["file_version"] == 10
    assert el["differs_in"] == [{"file": str(tc / "Beckhoff EL33xx.xml"), "file_version": 9}]
    assert el["name"].endswith("Resistor Bridge")
    # the TwinCAT copy is newer: it wins, and the knowledge-folder copy is the one that differs
    _write(tc / "Beckhoff EL33xx.xml", EL33.format(vendor=VENDOR.format(fv=11)).replace("Resistor Bridge", "Bridge"))
    _build(esi_kb)
    el = E.EsiIndex(db).lookup("EL3356-0010")["devices"][0]
    assert el["file_version"] == 11 and el["name"].endswith(" Bridge") and el["differs_in"][0]["file_version"] == 10


def test_search_groups_and_names_the_users(esi_kb):
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    r = x.search("Underrange", kinds=["device"])
    assert r["count"] == 1 and r["items"][0]["device"] == "EL3356-0010" and r["items"][0]["variants"] == 2
    assert r["items"][0]["newest_revision"] == "#x0019000A" and r["items"][0]["revisions"] == 2
    o = x.search("filter", kinds=["object"])
    assert o["items"][0]["index"] == "#x8000" and o["items"][0]["devices"] == {"count": 1, "first": ["EL3356-0010"]}
    g = x.search("shielding")
    assert g["items"][0]["kind"] == "diag" and g["items"][0]["devices"]["first"] == ["AX5206-0000-0216"]
    assert x.search("EL3356-0010")["match"].startswith("as one phrase")       # not FTS5 syntax: one phrase
    with pytest.raises(E.EsiError, match="kinds must be of"):
        x.search("x", kinds=["devices"])
    with pytest.raises(E.EsiError, match="limit must be 1…100"):
        x.search("x", limit=0)
    assert x.search("Underrange", limit=500)["clamped"] is True            # N167: a larger limit is used as 100
    with pytest.raises(E.EsiError) as ei:
        x.search("  ")
    assert ei.value.code == "empty_query"


def test_lookup_rules(esi_kb):
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    assert [d["type"] for d in x.lookup("#x0D1C3052")["devices"]] == ["EL3356-0010", "EL3356-0010"]
    assert x.lookup("0x0d1c3052", revision="0x0018000a")["devices"][0]["revision"] == "#x0018000A"
    s = x.lookup("EL3356-0010", detail="summary")
    assert all("pdo" not in d and d["pdo_count"] >= 1 for d in s["devices"])
    f = x.lookup("EL3356-0010", detail="full")
    assert all("pdo" in d for d in f["devices"]) and "detail_note" not in f
    n = x.lookup("Resistor Bridge")
    assert n["types"] == ["EL3356-0010"]
    assert "copy the vendor's ESI XML" in x.lookup("EL9999")["note"]
    for bad, msg in ((dict(detail="all"), "detail must be one of"), (dict(limit=31), "limit must be 1…30"),
                     (dict(revision="banana"), "revision must be")):
        with pytest.raises(E.EsiError, match=msg):
            x.lookup("EL3356-0010", **bad)


def test_build_discipline(esi_kb, monkeypatch):
    """Rule 6/7: an index built from exactly these files is left alone; new files → outdated (used as it is, not
    rebuilt); another format → rebuild_needed; an interrupted build replaces nothing; one build at a time."""
    kb, tc, db = esi_kb
    assert E.state(db, kb, [tc], twincat=False)["state"] == "missing"
    _build(esi_kb)
    assert _build(esi_kb)["result"] == "up_to_date"
    _write(tc / "Beckhoff EL1xxx.xml", TERMINALS.format(vendor=VENDOR.format(fv=1)).replace("EL1002", "EL1012"))
    st = E.state(db, kb, [tc], twincat=False)
    assert st["state"] == "outdated" and "added, changed or removed" in st["reason"] and "kb_rebuild(index='esi')" in st["message"]
    assert E.EsiIndex(db).lookup("EL1012")["matches"] == 0                      # not read until the rebuild
    # other extra folders than the index was built with: said so
    assert "the ESI folders changed" in E.state(db, kb, [], twincat=False)["reason"]
    # an interrupted build: the index in use stays exactly as it was, the work file is reported, then discarded
    real = E.iter_info
    calls = []

    def flaky(p):
        calls.append(p)
        if len(calls) == 3:
            raise KeyboardInterrupt
        yield from real(p)
    monkeypatch.setattr(E, "iter_info", flaky)
    before = db.read_bytes()
    with pytest.raises(KeyboardInterrupt):
        _build(esi_kb)
    assert db.read_bytes() == before
    st = E.state(db, kb, [tc], twincat=False)
    assert st["rebuild"]["state"] == "interrupted" and "the next one starts over" in st["message"]
    monkeypatch.setattr(E, "iter_info", real)
    assert _build(esi_kb)["result"] == "built" and not db.with_name("esi.db.building").exists()
    assert E.EsiIndex(db).lookup("EL1012")["matches"] == 1
    # another format: rebuild_needed, the index stays in use
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET v = '0' WHERE k = 'format'")
    con.commit()
    con.close()
    st = E.state(db, kb, [tc], twincat=False)
    assert st["state"] == "rebuild_needed" and "format 0" in st["reason"]
    assert E.EsiIndex(db).lookup("EL1002")["matches"] == 1
    # one build at a time
    with kb_lock.IndexLock(E.side_files(db)[1]):
        with pytest.raises(E.EsiError, match="another ESI build is running"):
            _build(esi_kb, force=True)
        assert E.state(db, kb, [tc], twincat=False)["rebuild"]["state"] == "running"


def test_a_finished_build_that_could_not_replace_the_index_does_so_next_time(esi_kb, monkeypatch):
    kb, tc, db = esi_kb
    _build(esi_kb)
    _write(tc / "Beckhoff EL1xxx.xml", TERMINALS.format(vendor=VENDOR.format(fv=1)).replace("EL1002", "EL1012"))
    monkeypatch.setattr(E, "_swap", lambda b, f, wait_s=0: f"{f} is open in another program")
    r = _build(esi_kb)
    assert r["result"] == "not_replaced" and "open in another program" in r["why"]
    assert E.state(db, kb, [tc], twincat=False)["rebuild"]["state"] == "waiting_to_replace"
    monkeypatch.undo()
    r = _build(esi_kb)
    assert r["result"] == "replaced" and E.EsiIndex(db).lookup("EL1012")["matches"] == 1


def test_a_large_file_is_read_device_by_device(tmp_path):
    """Memory stays bounded whatever the file's size (Beckhoff's ELM310x.xml is 88 MB): each device is dropped from
    the tree once read — the peak while reading is a fraction of what the whole tree takes."""
    import tracemalloc
    import xml.etree.ElementTree as ET
    objs = "".join(f"<Object><Index>#x{0x8000 + k:04x}</Index><Name>Settings {k}</Name><Type>UINT</Type>"
                   f"<Info><DefaultData>0100</DefaultData></Info></Object>" for k in range(40))
    devs = "".join(f"""<Device><Type ProductCode="#x{i:08x}" RevisionNo="#x1">EL{i:04d}</Type><Name>T{i}</Name>
      <Profile><Dictionary><Objects>{objs}</Objects></Dictionary></Profile></Device>""" for i in range(600))
    p = _write(tmp_path / "big.xml", f"<EtherCATInfo>{VENDOR.format(fv=1)}<Descriptions><Devices>{devs}</Devices>"
                                     "</Descriptions></EtherCATInfo>")
    tracemalloc.start()
    tree = ET.parse(p)
    whole = tracemalloc.get_traced_memory()[1]
    del tree
    tracemalloc.stop()
    tracemalloc.start()
    n = sum(1 for _ in E.iter_info(p))
    streamed = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert n == 600 and streamed < whole / 4, (streamed, whole)


def test_root_kinds_and_non_esi(tmp_path):
    assert E.root_kind(_write(tmp_path / "a.xml", AX5_DICT)) == "dict"
    assert E.root_kind(_write(tmp_path / "b.xml", AX5_DIAG)) == "diag"
    assert E.root_kind(_write(tmp_path / "c.xml", "<Other/>")) is None
    assert E.root_kind(_write(tmp_path / "d.xml", "not xml at all")) is None


def test_values_are_decoded_by_their_type():
    assert E.decode_data("52301c0d", "UDINT") == 0x0D1C3052
    assert E.decode_data("ffff", "INT") == -1 and E.decode_data("ffff", "UINT") == 65535
    assert E.decode_data("0000a041", "REAL") == 20.0
    assert E.decode_data("01", "BOOL") is True and E.decode_data("0200", "UINT", {"2": "IIR1"}) == {"value": 2, "text": "IIR1"}
    assert E.decode_data("454c3333353600", "STRING(8)") == "EL3356"
    assert E.decode_data("01", "UDINT") is None and E.decode_data("zz", "UINT") is None
    assert E.decode_data("0102", "DT8000") is None                              # not elementary: default_raw only


# ---------------------------------------------------------------------------------------------- through the server
async def _until(c, fn, tries=600):
    import anyio
    last = None
    for _ in range(tries):
        last = await fn()
        if last:
            return last
        await anyio.sleep(0.05)
    raise AssertionError(last)


@pytest.mark.anyio
async def test_the_server_builds_it_once_by_itself_and_answers_from_it(monkeypatch, project_dir, esi_kb):
    """Rule 6: the first build starts by itself (server start, in its own process: esi_db.py with this Python); the
    hw_* tools answer from it; kb_rebuild(index='esi') of a current index says so without asking, a forced one needs
    the approval."""
    from mcp import Client
    from tests.fake_ads import install
    from tests.test_server_e2e import mk_cfg, payload, text
    from twincat_mcp.server import build_server
    install(monkeypatch)
    kb, tc, db = esi_kb
    cfg = mk_cfg(project_dir, mode="guarded")
    cfg.knowledge.location = str(kb)
    cfg.knowledge.esi_dirs = [str(tc)]
    async with Client(build_server(cfg)) as c:
        async def built():
            st = payload(await c.call_tool("kb_status", {}))["esi"]
            return st if st["state"] == "complete" and not st.get("rebuild") else None
        st = await _until(c, built)
        assert st["devices"] == 7 and st["database"] == str(db)
        r = payload(await c.call_tool("hw_lookup", {"query": "EL3356-0010"}))
        assert r["devices"][0]["pdo_assignments"][0]["name"] == "Standard (INT32)" and "esi_first_build" not in r
        o = payload(await c.call_tool("hw_object", {"device": "EL3356-0010", "index": "#x8000"}))
        assert {s["sub"]: s for s in o["subitems"]}[17]["default"] == {"value": 2, "text": "IIR1"}
        # N152 / N155 (0.8.14 retest): one sub-index through the server; its answers carry the index's source
        o = payload(await c.call_tool("hw_object", {"device": "EL3356-0010", "index": "8000:11"}))
        assert o["sub"] == 17 and [s["name"] for s in o["subitems"]] == ["Mode0 filter settings"], o
        assert o["source"]["database"] == str(db) and o["source"]["state"] == "complete" and r["source"] == o["source"]
        f = payload(await c.call_tool("hw_search", {"query": "shielding"}))
        assert f["items"][0]["code"] == "#xD196"
        g = payload(await c.call_tool("hw_diag", {"code": "0xD196", "device": "AX5206-0000-0216"}))
        assert g["messages"][0]["causes"][0]["remedy"] == "Check the shielding"
        m = payload(await c.call_tool("hw_module", {"ident": "#x006b0064", "device": "AX8206-0210-0106"}))
        assert m["modules"][0]["name"] == "DeviceMain 0106"
        r = payload(await c.call_tool("kb_rebuild", {"index": "esi"}))
        assert r["started"] is False and r["up_to_date"]
        # a work file left next to the current index: discarded on kb_rebuild without a rebuild or a question
        building = E.side_files(db)[0]
        building.write_bytes(db.read_bytes())
        r = payload(await c.call_tool("kb_rebuild", {"index": "esi"}))
        assert r["started"] is False and r["discarded"] == str(building) and not building.exists(), r
        r = await c.call_tool("kb_rebuild", {"index": "esi", "force": True})
        assert r.is_error and "tc_request_confirmation" in text(r)
        bad = await c.call_tool("hw_object", {"device": "EL3356-0010", "index": "#x8010"})
        assert bad.is_error and "the nearest" in text(bad)
    log = Path(cfg.safety.audit_log).parent / "logs" / "esi-build.log"
    assert "indexed 7 device revisions" in log.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_no_esi_files_no_build_and_said_before_asking(monkeypatch, project_dir, tmp_path):
    from mcp import Client
    from tests.fake_ads import install
    from tests.test_server_e2e import mk_cfg, payload
    from twincat_mcp.server import build_server
    install(monkeypatch)
    kb = tmp_path / "kb"
    (kb / "manuals").mkdir(parents=True)
    (kb / "index").mkdir()
    cfg = mk_cfg(project_dir, mode="guarded")
    cfg.knowledge.location = str(kb)
    async with Client(build_server(cfg)) as c:
        r = payload(await c.call_tool("kb_rebuild", {"index": "esi"}))
        assert r["started"] is False and "no ESI files" in r["error"]
        h = payload(await c.call_tool("hw_lookup", {"query": "EL2008"}))
        assert h["esi_index"]["state"] == "missing" and "esi_first_build" not in h
    assert not (kb / "index" / "esi.db").exists()


def test_review_0812_a_replaced_description_leaves_nothing_behind(esi_kb):
    """Review 0.8.12: a revision described by an older file and then replaced by a newer one (higher FileVersion,
    read later) left the older description's objects in the index — hw_search found objects no device has, and the
    counts included them."""
    kb, tc, db = esi_kb
    newer = EL33.format(vendor=VENDOR.format(fv=11)).replace("RMB Settings", "Bridge Settings")
    _write(tc / "Beckhoff EL33xx.xml", newer)                       # read after manuals\: it replaces the fv-10 one
    r = _build(esi_kb)
    x = E.EsiIndex(db)
    assert x.search("RMB", kinds=["object"])["items"] == []         # the replaced description's object is gone
    hit = x.search("Bridge Settings", kinds=["object"])["items"]
    assert hit and hit[0]["devices"]["count"] >= 1, hit
    con = sqlite3.connect(db)
    n_obj = con.execute("SELECT COUNT(*) FROM obj").fetchone()[0]
    unused = con.execute("SELECT COUNT(*) FROM obj WHERE id NOT IN (SELECT obj_id FROM dict_obj)").fetchone()[0]
    no_user = con.execute("SELECT COUNT(*) FROM dicts WHERE id NOT IN (SELECT dict_id FROM devices WHERE dict_id IS "
                          "NOT NULL UNION SELECT dict_id FROM modules WHERE dict_id IS NOT NULL) AND origin NOT LIKE "
                          "'%Dict.xml'").fetchone()[0]
    con.close()
    assert r["objects"] == n_obj and unused == 0 and no_user == 0, (r, n_obj, unused, no_user)


def test_review_0812_a_work_file_next_to_a_current_index_is_discarded(esi_kb):
    """Review 0.8.12: a .building left next to a complete, current index was reported 'interrupted — the next one
    starts over' but no build ever removed it (the build returned up_to_date first)."""
    kb, tc, db = esi_kb
    _build(esi_kb)
    building = E.side_files(db)[0]
    building.write_bytes(db.read_bytes())                           # a finished, redundant build of the same files
    st = E.state(db, kb, [tc], twincat=False)
    assert st["state"] == "complete" and st["rebuild"]["state"] == "stale" and "discards it" in st["message"], st
    r = _build(esi_kb)
    assert r["result"] == "up_to_date" and r["discarded"] == str(building) and not building.exists()


def test_review_0812_a_build_without_twincat_folders_stays_so(esi_kb, tmp_path, monkeypatch):
    """Review 0.8.12: an index built with --no-twincat was reported OUTDATED on every PC with TwinCAT's ESI folders
    (state() always counted them), and the next rebuild would have added them."""
    kb, tc, db = esi_kb
    fake_tc = tmp_path / "pd" / "Beckhoff" / "TwinCAT" / "3.1" / "Config" / "Io" / "EtherCAT"
    _write(fake_tc / "Beckhoff EL1xxx.xml", TERMINALS.format(vendor=VENDOR.format(fv=1)).replace("EL1002", "EL1012"))
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    assert fake_tc in E.twincat_esi_folders()
    E.build(db, kb, [tc], twincat=False, log=lambda m: None)
    assert E.recorded_twincat(db) is False
    assert E.state(db, kb, [tc])["state"] == "complete"             # as built: TwinCAT's folders are not counted
    assert E.build(db, kb, [tc], log=lambda m: None)["result"] == "up_to_date"
    assert E.EsiIndex(db).lookup("EL1012")["matches"] == 0
    E.build(db, kb, [tc], twincat=True, log=lambda m: None)         # asked for: read again
    assert E.recorded_twincat(db) is True and E.EsiIndex(db).lookup("EL1012")["matches"] == 1


def test_review_0812_counts_are_exact_and_input_is_literal(tmp_path):
    """Review 0.8.12: hw_lookup's matches / more were of the first 200 rows; hw_search's variants of the rows read;
    '%' and '_' in a query were LIKE wildcards; a hex number past 32 bits failed with an unmapped OverflowError."""
    devs = "".join(f'<Device><Type ProductCode="#x00001234" RevisionNo="#x{r:08x}">EL9{r % 3}00</Type>'
                   f'<Name LcId="1033">EL9{r % 3}00 Test terminal</Name></Device>' for r in range(1, 251))
    m = tmp_path / "kb" / "manuals"
    _write(m / "Beckhoff EL9xxx.xml", f'<?xml version="1.0"?><EtherCATInfo>{VENDOR.format(fv=1)}<Descriptions>'
           f'<Devices>{devs}</Devices></Descriptions></EtherCATInfo>')
    db = tmp_path / "kb" / "index" / "esi.db"
    E.build(db, tmp_path / "kb", [], twincat=False, log=lambda m: None)
    x = E.EsiIndex(db)
    r = x.lookup("Test terminal", limit=5)
    assert r["matches"] == 250 and r["types"] == ["EL9000", "EL9100", "EL9200"], r
    assert r["more"].startswith("245 more")
    assert x.lookup("#x00001234", limit=5)["matches"] == 250
    assert x.lookup("EL9_00")["matches"] == 0                       # '_' is the character, not any character
    s = x.search("Test terminal", kinds=["device"], limit=2)
    assert {i["variants"] for i in s["items"]} == {84, 83}, s        # 250 revisions over 3 types, all counted
    for bad in (lambda: x.lookup("#xFFFFFFFFFFFFFFFFFFF"), lambda: x.lookup("EL9000", revision="#x1FFFFFFFF"),
                lambda: x.object("EL9000", "#x123456")):
        with pytest.raises(E.EsiError, match="out of range"):
            bad()


def test_review_0812_every_profile_of_a_device_is_read(tmp_path):
    """Review 0.8.12: the ESI schema allows several Profile elements (one per channel); only the first was read.
    Their dictionaries are joined; an object two profiles give differently is named, the first one's kept."""
    def prof(ch, objs):
        o = "".join(f'<Object><Index>#x{i:04x}</Index><Name>{n}</Name><Type>UINT</Type><BitSize>16</BitSize>'
                    f'<Flags><Access>rw</Access></Flags></Object>' for i, n in objs)
        return (f"<Profile><ProfileNo>402</ProfileNo><ChannelNo>{ch}</ChannelNo><Dictionary><Objects>{o}</Objects>"
                "</Dictionary></Profile>")
    dev = ('<Device><Type ProductCode="#x1" RevisionNo="#x1">AX9002</Type><Name LcId="1033">AX9002 two axes</Name>'
           + prof(1, [(0x1018, "Identity"), (0x6040, "Controlword")])
           + prof(2, [(0x1018, "Identity"), (0x6840, "Controlword axis 2"), (0x6040, "Other")]) + "</Device>")
    m = tmp_path / "kb" / "manuals"
    _write(m / "Beckhoff AX9xxx.xml", f'<?xml version="1.0"?><EtherCATInfo>{VENDOR.format(fv=1)}<Descriptions>'
           f'<Devices>{dev}</Devices></Descriptions></EtherCATInfo>')
    db = tmp_path / "kb" / "index" / "esi.db"
    E.build(db, tmp_path / "kb", [], twincat=False, log=lambda m: None)
    d = E.EsiIndex(db).lookup("AX9002", detail="full")["devices"][0]
    assert d["coe_count"] == 3 and d["dictionary_conflicts"] == ["#x6040"], d
    assert [p["channel_no"] for p in d["profiles"]] == [1, 2]
    assert E.EsiIndex(db).object("AX9002", "#x6840")["name"] == "Controlword axis 2"


def test_review_0812_xml_that_is_no_esi_starts_no_first_build(tmp_path):
    files = [(_write(tmp_path / "manuals" / "export.xml", "<?xml version='1.0'?><PlcProjectExport/>"), "knowledge")]
    assert not E.has_esi(files)
    files.append((_write(tmp_path / "manuals" / "a.xml", f"<?xml version='1.0'?><EtherCATInfo>{VENDOR.format(fv=1)}"
                         "</EtherCATInfo>"), "knowledge"))
    assert E.has_esi(files)


def test_ci_0812_a_reader_leaves_no_connection_open(esi_kb):
    """0.8.12 Windows CI: a build waited 60 s and could not replace esi.db — a reader's connection was still open.
    sqlite3 connections sit in a reference cycle with their statement cache (an lru_cache that holds the
    connection), so the garbage collector closes them late, not when the read returns; on Windows an open file cannot
    be replaced. Every read closes its connection."""
    import gc
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    gc.disable()
    try:
        x.lookup("EL3356-0010", detail="full")
        x.search("RMB")
        x.object("EL3356-0010", "#x8000")
        open_ = []
        for o in gc.get_objects():
            if isinstance(o, sqlite3.Connection):
                try:
                    names = [r[2] for r in o.execute("PRAGMA database_list")]
                except sqlite3.ProgrammingError:
                    continue                                   # closed
                open_ += [n for n in names if n and Path(n).name == "esi.db"]
        assert open_ == [], open_
    finally:
        gc.enable()


def test_second_review_0812_state_and_flags(esi_kb, tmp_path, monkeypatch):
    """Second review 0.8.12: a finished first build that could not be swapped in was called 'stale' (the index did
    not exist yet, so its stamp was never compared); --twincat on an index built without TwinCAT's folders kept it
    up to date when the files had not changed; a file two profiles name was read twice."""
    kb, tc, db = esi_kb
    monkeypatch.setattr(E, "_swap", lambda b, f, wait_s=0: f"{f} is open in another program")
    r = _build(esi_kb)
    assert r["result"] == "not_replaced" and not db.exists()
    st = E.state(db, kb, [tc], twincat=False)
    assert st["state"] == "missing" and st["rebuild"]["state"] == "waiting_to_replace", st
    monkeypatch.undo()
    assert _build(esi_kb)["result"] == "replaced"
    fake_tc = tmp_path / "pd" / "Beckhoff" / "TwinCAT" / "3.1" / "Config" / "Io" / "EtherCAT"
    fake_tc.mkdir(parents=True)                                     # TwinCAT's folder exists, no file in it yet
    monkeypatch.setenv("ProgramData", str(tmp_path / "pd"))
    assert E.recorded_twincat(db) is False
    r = E.build(db, kb, [tc], twincat=True, log=lambda m: None)
    assert r["result"] == "built" and E.recorded_twincat(db) is True, r
    prof = ("<Profile><ChannelNo>{n}</ChannelNo><DictionaryFile>Beckhoff AX5xxx\\Beckhoff AX5206-xxxx-0216 Dict.xml"
            "</DictionaryFile></Profile>")
    dev = ('<Device><Type ProductCode="#x2" RevisionNo="#x1">AX9206</Type><Name LcId="1033">AX9206</Name>'
           + prof.format(n=1) + prof.format(n=2) + "</Device>")
    _write(kb / "manuals" / "Beckhoff_EtherCAT_XML" / "Beckhoff AX9xxx.xml",
           f'<?xml version="1.0"?><EtherCATInfo>{VENDOR.format(fv=1)}<Descriptions><Devices>{dev}</Devices>'
           "</Descriptions></EtherCATInfo>")
    r = _build(esi_kb)
    d = E.EsiIndex(db).lookup("AX9206", detail="full")["devices"][0]
    ax = E.EsiIndex(db).lookup("AX5206-0000-0216", detail="full")["devices"][0]
    assert "dictionary_files" not in d and d["coe_count"] == ax["coe_count"] > 0, (d, ax)
    con = sqlite3.connect(db)
    same = con.execute("SELECT COUNT(DISTINCT dict_id) FROM devices WHERE type IN ('AX9206', 'AX5206-0000-0216')").fetchone()[0]
    con.close()
    assert same == 1                                                 # the file's one dictionary, stored once


# ---------------------------------------------------------------------------------------------- 0.8.14 retest
def test_n152_one_sub_index_of_an_object(esi_kb):
    """N152 (0.8.14 retest, S3): hw_object index=8000:11 silently dropped ':11' and answered the whole object 0x8000
    (sub 0 … 0x14 here) — the index reader cut the text at ':'. The CoE notation index:sub-index (sub-index in hex,
    as Beckhoff's manuals and TwinCAT write it: 8000:11 = sub-index 17) gives that sub-index only; a sub-index the
    object does not have is named with the ones it has; a form that is none of the accepted ones is refused with
    them; a diagnosis code or a ModuleIdent has no sub-index and says so."""
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    for form in ("8000:11", "#x8000:11", "0x8000:0x11", "8000:#x11", " 8000 : 11 "):
        o = x.object("EL3356-0010", form)
        assert (o["index"], o["sub"], o["name"]) == ("#x8000", 17, "RMB Settings"), (form, o)
        assert [(s["sub"], s["name"]) for s in o["subitems"]] == [(17, "Mode0 filter settings")], (form, o)
        assert o["subitems"][0]["default"] == {"value": 2, "text": "IIR1"}
    assert len(x.object("EL3356-0010", "8000")["subitems"]) == 4 and "sub" not in x.object("EL3356-0010", "8000")
    assert x.object("EL3356-0010", "1c12:2")["subitems"][0]["default"] == 0x1601     # an array element
    with pytest.raises(E.EsiError, match=r"#x8000 'RMB Settings' has no sub-index #x12 \(18\); its sub-indices: "
                                         r"#x00 \(0\), #x01 \(1\), #x11 \(17\), #x14 \(20\)") as ei:
        x.object("EL3356-0010", "8000:12")
    assert ei.value.code == "not_found"
    # an object without sub-indices: its value is sub-index 0
    assert x.object("EL3356-0010", "#x1018:0")["default"] == 0x0D1C3052
    with pytest.raises(E.EsiError, match="is a single value, it has no sub-index #x01 .* its value is sub-index 0") as ei:
        x.object("EL3356-0010", "#x1018:1")
    assert ei.value.code == "not_found"
    for bad in ("8000:zz", "8000:017", "8000:", "8000:#x100", "8000:11:2"):
        with pytest.raises(E.EsiError, match="sub-index") as ei:
            x.object("EL3356-0010", bad)
        assert ei.value.code == "bad_param" and "8000:11" in str(ei.value), (bad, ei.value)
    with pytest.raises(E.EsiError, match="no sub-index") as ei:
        x.diag("#x4101:1")
    assert ei.value.code == "bad_param"
    with pytest.raises(E.EsiError, match="no sub-index"):
        x.module("#x006b0064:1")


def test_n153_a_revision_that_is_not_indexed_is_said_so_not_the_device(esi_kb):
    """N153 (0.8.14 retest, S2): hw_lookup EL3356-0010 revision=#x00190000 (and revision=9999) answered 'not in the ESI
    files of this PC — copy the vendor's ESI XML … kb_rebuild' although the device is indexed (with other
    revisions): the note was given for any empty answer, whatever filter emptied it. And hw_object / hw_diag /
    hw_module with that revision said the device was 'not in the ESI index' (listing itself as a type with that
    text). Now the answer names what is not there — the revision — and the revisions that are."""
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    r = x.lookup("EL3356-0010", revision="#x00190000")
    assert r["matches"] == 0 and r["devices"] == [], r
    assert "copy the vendor's ESI" not in r["note"] and "kb_rebuild" not in r["note"], r
    assert r["note"].startswith("EL3356-0010 has no revision #x00190000"), r
    assert r["revisions_available"] == {"EL3356-0010": ["#x0019000A", "#x0018000A"]}, r
    r = x.lookup("EL3356-0010", revision="9999")                        # a number: read as decimal, said so
    assert "no revision #x0000270F (decimal 9999)" in r["note"] and "copy the vendor's ESI" not in r["note"], r
    r = x.lookup("Resistor Bridge", revision=1)                          # a name fragment: its types' revisions
    assert "has no revision #x00000001" in r["note"] and r["revisions_available"]["EL3356-0010"][0] == "#x0019000A"
    # a device that is not indexed at all: still the hint to add its ESI file (with or without a revision)
    for rev in (None, "#x1"):
        assert "copy the vendor's ESI XML" in x.lookup("EL9999", revision=rev)["note"]
    for call in (lambda: x.object("EL3356-0010", "#x8000", revision="#x00190000"),
                 lambda: x.diag("#x4101", device="EL3356-0010", revision="#x00190000"),
                 lambda: x.module("#x00000cf2", device="EL3356-0010", revision="#x00190000")):
        with pytest.raises(E.EsiError, match=r"EL3356-0010 has no revision #x00190000 in the ESI index; its revisions "
                                             r"\(newest first\): #x0019000A, #x0018000A") as ei:
            call()
        assert ei.value.code == "not_found" and "is not in the ESI index" not in str(ei.value)


def test_n154_a_product_code_in_every_form(esi_kb):
    """N154 (0.8.14 retest, S3): hw_lookup query=219951186 (= #x0D1C3052, decimal) found nothing and answered 'copy the
    vendor's ESI XML' — only '#x…' / '0x…' were read as a product code. A number is now read as a decimal product
    code as well (a type or name containing the digits still matches: '3356'); '#x' / '0x' followed by no hex number
    is refused with the accepted forms; an answer that finds nothing names them; a revision is read the same way."""
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    for q in ("219951186", "#x0D1C3052", "#x0d1c3052", "0x0D1C3052", "0X0d1c3052"):
        r = x.lookup(q)
        assert r["matches"] == 2 and r["types"] == ["EL3356-0010"], (q, r)
    assert "product code #x0D1C3052" in x.lookup("219951186")["read_as"]
    assert x.lookup("3356")["types"] == ["EL3356-0010"]                 # digits of a type: found as before
    with pytest.raises(E.EsiError, match="is not a hex number") as ei:
        x.lookup("#x0D1C30G2")
    assert ei.value.code == "bad_param" and "decimal" in str(ei.value)
    n = x.lookup("0D1C3052")                                             # hex without #x: text, which matches nothing
    assert n["matches"] == 0 and "'#x0D1C3052', '0x0D1C3052' or decimal" in n["note"], n
    for rev in ("#x0018000a", "0x0018000A", "1572874", 1572874):         # 0x0018000A = 1572874
        assert x.lookup("EL3356-0010", revision=rev)["devices"][0]["revision"] == "#x0018000A", rev
    with pytest.raises(E.EsiError, match="revision must be .*0x0019000A.* or decimal"):
        x.lookup("EL3356-0010", revision="0018000A")


def test_n156_a_module_is_found_by_its_device(esi_kb):
    """N156 (0.8.14 retest, S4): hw_search AX8000 kinds=module found nothing although the AX8000 modules are indexed —
    they are named generically (DeviceMain, SynchronousMotor …) and their search text held only their own name and
    PDOs. The search text of a module now holds the devices whose slots take it (type, name, group — the rule of
    hw_lookup's slots), so a module is found by its device; the index format is 2."""
    kb, tc, db = esi_kb
    _build(esi_kb)
    x = E.EsiIndex(db)
    r = x.search("AX8206", kinds=["module"])
    assert {(i["module"], i["title"]) for i in r["items"]} == {("DeviceMain", "DeviceMain 0106"),
                                                               ("DeviceMain", "DeviceMain 0107")}, r
    assert "AX8206" in r["items"][0]["snippet"]
    r = x.search("Servo drive", kinds=["module"])                        # words of the device's name
    assert r["count"] == 2, r
    t = x.search("Resistor Bridge", kinds=["module"])                    # EL3356-0010's slot module (TSC), own file
    assert "TSC" in {i["title"].split()[-1] for i in t["items"]}, t
    # a module goes with the devices of its rule only: the 0106 module file is not the 0107 device's
    con = sqlite3.connect(db)
    body = dict(con.execute("SELECT title, body FROM search WHERE kind = 'module'").fetchall())
    con.close()
    assert "AX8206-0210-0107" in body["DeviceMain 0107"] and "AX8206-0210-0107" not in body["DeviceMain 0106"], body
    assert E.FORMAT == "2" and E.state(db, kb, [tc], twincat=False)["format"] == "2"


def test_n167_hw_search_pages_through_every_item_once(esi_kb):
    """N167 (S3, 0.8.14 retest): hw_search had a limit (1-100) and no offset — items past the limit could not be
    reached, and a larger limit was refused. offset / next_offset page through the items (one per title, in rank
    order, counted over the index: total); a limit above 100 is used as 100 and said so."""
    _build(esi_kb)
    x = E.EsiIndex(esi_kb[2])
    whole = x.search("S*", limit=100)
    assert whole["total"] == whole["count"] == 11 and whole["next_offset"] is None and whole["offset"] == 0, whole
    got, off = [], 0
    while off is not None:
        r = x.search("S*", limit=3, offset=off)
        assert r["count"] <= 3 and r["total"] == 11
        got += [(i["kind"], i["title"]) for i in r["items"]]
        off = r["next_offset"]
    assert got == [(i["kind"], i["title"]) for i in whole["items"]]           # one order, nothing twice or skipped
    r = x.search("S*", limit=1000)
    assert r["count"] == 11 and r["clamped"] is True and r["clamped_note"].startswith("limit 1000 is above the maximum")
    with pytest.raises(E.EsiError, match="past the last hit"):
        x.search("S*", offset=11)
    with pytest.raises(E.EsiError, match="offset must be >= 0"):
        x.search("S*", offset=-1)


def test_an_object_defined_twice_in_one_dictionary_is_one_object(esi_kb):
    """kb_verify E2 on the user's ESI set (0.8.14 full run): EL6653-0010 rev #x0010000A — '17 CoE objects listed,
    counted 19'. Beckhoff's EL66xx.xml defines #x1C32 and #x1C33 twice in that device's dictionary, byte-identical.
    The list kept one per index, the count took both. An index defined again identically is the same object, kept
    once and counted once; defined again differently, the first definition is the object and it says so
    (defined_again_differs) — nothing is dropped silently."""
    kb, tc, db = esi_kb
    p = kb / "manuals" / "Beckhoff_EtherCAT_XML" / "Beckhoff EL33xx.xml"
    s = p.read_text(encoding="utf-8")
    ident = ('<Object><Index>#x1018</Index><Name>Identity</Name><Type>UDINT</Type><BitSize>32</BitSize>\n'
             '                <Info><DefaultData>52301c0d</DefaultData></Info><Flags><Access>ro</Access></Flags></Object>')
    assert s.count(ident) == 1
    other = ('<Object><Index>#x1c12</Index><Name>RxPDO assign (2nd)</Name><Type>UDINT</Type><BitSize>32</BitSize>'
             '<Flags><Access>ro</Access></Flags></Object>')
    p.write_text(s.replace("</Objects>", ident + other + "</Objects>", 1), encoding="utf-8")
    _build(esi_kb)
    x = E.EsiIndex(db)
    d = x.lookup("EL3356-0010", 1, None, "newest")["devices"][0]
    idx = [o["index"] for o in d["coe_objects"]]
    assert idx == ["#x1018", "#x1C12", "#x8000"] and d["coe_count"] == 3, (idx, d["coe_count"])
    o = x.object("EL3356-0010", "#x1c12")
    assert o["name"] == "RxPDO assign" and o["defined_again_differs"] == 1, o
    assert "defined_again_differs" not in x.object("EL3356-0010", "#x1018")
