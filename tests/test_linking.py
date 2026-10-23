"""0.8.20: linking through XAE (docs/PLAN-0.8.19.md, 0.8.20 row) — TE1000 "Managing variable mappings": LinkVariables /
UnlinkVariables on ITcSysManager, ProduceMappingInfo / ConsumeMappingInfo on ITcSysManager2 (ConsumeMappingInfo
replaces the whole table). A fake System Manager keeps the table as <VarLinks>, synthetic names only."""
from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET

import pytest

from tests.fake_ads import install
from tests.test_libraries import _solution
from tests.test_server_e2e import client_for, mk_cfg, payload, text
from tests.test_xae_backend import _INST_4026, ComError, FakeItem, FakeSysMan, _xae_with
from twincat_mcp import xae_backend as X
from twincat_mcp.server import build_server

PLC = "TIPC^Plc^Plc Instance"
T2 = "TIID^Device 1 (EtherCAT)^Term 1 (EK1100)^Term 2 (EL1008)"
T3 = "TIID^Device 1 (EtherCAT)^Term 1 (EK1100)^Term 3 (EL2008)"
IN_PROX2 = f"{PLC}^PlcTask Inputs^GVL_IO.abProx[2]"
IN_MANUAL = f"{PLC}^PlcTask Inputs^GVL_IO.bManual"
OUT_PUMP = f"{PLC}^PlcTask Outputs^GVL_IO.bPump"


class LinkSysMan(FakeSysMan):
    """The mapping table as XAE keeps it: ProduceMappingInfo writes it, LinkVariables / UnlinkVariables /
    ConsumeMappingInfo change it. A path splits into owner (the PLC instance or a box) and variable."""

    OWNERS = (PLC, T2, T3)

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str, str, str, dict]] = [
            (PLC, "PlcTask Inputs^GVL_IO.abProx[2]", T2, "Channel 3^Input", {}),
            (T3, "Channel 1^Output", PLC, "PlcTask Outputs^GVL_IO.bPump", {}),
        ]
        self.calls: list[tuple] = []
        self.items[PLC] = FakeItem("Plc Instance", PLC, xml=_INST_4026)

    def _split(self, path):
        for o in sorted(self.OWNERS, key=len, reverse=True):
            if path.casefold().startswith(o.casefold() + "^"):
                return o, path[len(o) + 1:]
        raise ComError(-1739587583, f"TSM_E_ITEMNOTFOUND: {path}")   # 0x98510001

    def ProduceMappingInfo(self):
        root = ET.Element("VarLinks")
        for oa, va, ob, vb, attrs in self.links:
            a = next((e for e in root.findall("OwnerA") if e.get("Name") == oa), None)
            if a is None:
                a = ET.SubElement(root, "OwnerA", {"Name": oa})
            b = next((e for e in a.findall("OwnerB") if e.get("Name") == ob), None)
            if b is None:
                b = ET.SubElement(a, "OwnerB", {"Name": ob})
            ET.SubElement(b, "Link", {"VarA": va, "VarB": vb, **attrs})
        return '<?xml version="1.0"?>' + ET.tostring(root, encoding="unicode")

    def LinkVariables(self, v1, v2, o1=0, o2=0, size=0):
        self.calls.append(("LinkVariables", v1, v2, o1, o2, size))
        oa, va = self._split(v1)
        ob, vb = self._split(v2)
        if va.endswith("Input") and vb.endswith("Output"):
            raise ComError(-1739587580, "TSM_E_MISMATCHINGITEMS")
        self.links.append((oa, va, ob, vb, {"Size": str(size)} if size else {}))

    def UnlinkVariables(self, v1, v2=""):
        self.calls.append(("UnlinkVariables", v1, v2))
        f1, f2 = v1.casefold(), v2.casefold()
        keep = []
        for lk in self.links:
            ends = {f"{lk[0]}^{lk[1]}".casefold(), f"{lk[2]}^{lk[3]}".casefold()}
            if f1 in ends and (not v2 or f2 in ends):
                continue
            keep.append(lk)
        self.links = keep                       # nothing matched: S_FALSE, no error

    def ConsumeMappingInfo(self, xml):
        """Reads only <VarLinks><OwnerA><OwnerB><Link> directly below each other — its own reading, not the plugin's
        (review 1 of 0.8.20); resets the table first, as TE1000 says."""
        self.calls.append(("ConsumeMappingInfo", xml))
        self.links = []
        root = ET.fromstring(xml)
        for a in root.findall("OwnerA"):
            for b in a.findall("OwnerB"):
                for lk in b.findall("Link"):
                    attrs = {k: v for k, v in lk.attrib.items() if k not in ("VarA", "VarB")}
                    self.links.append((a.get("Name"), lk.get("VarA"), b.get("Name"), lk.get("VarB"), attrs))


def _be(monkeypatch):
    be, _dte, sm = _xae_with(monkeypatch, LinkSysMan())
    monkeypatch.setattr(be, "_modal_dialogs", lambda: [])
    return be, sm


# ---- xae_link ---------------------------------------------------------------------------------------------------------

def test_link_runs_once_gives_the_change_and_refuses_a_second_time(monkeypatch):
    be, sm = _be(monkeypatch)
    r = be.link(IN_MANUAL, f"{T2}\tChannel 4^Input")             # a tab separates branches too (TE1000)
    assert sm.calls == [("LinkVariables", IN_MANUAL, f"{T2}^Channel 4^Input", 0, 0, 0)], sm.calls
    assert r["added"] == [{"a": IN_MANUAL, "b": f"{T2}^Channel 4^Input"}] and r["removed"] == [], r
    assert r["activation_required"] is True and "not saved and not activated" in r["note"] and "warning" not in r
    assert r["links_before"] == 2 and r["links_after"] == 3
    with pytest.raises(X.XaeError, match="are linked already .* nothing was done"):
        be.link(f"{T2}^channel 4^input", IN_MANUAL)              # either direction, any case
    assert len(sm.calls) == 1


def test_link_says_what_else_the_target_is_linked_to_and_passes_a_part(monkeypatch):
    be, sm = _be(monkeypatch)
    r = be.link(IN_MANUAL, f"{T2}^Channel 3^Input", 0, 0, 1)
    assert sm.calls[-1] == ("LinkVariables", IN_MANUAL, f"{T2}^Channel 3^Input", 0, 0, 1)
    assert r["target_also_linked_to"] == [IN_PROX2] and r["added"][0]["attrs"] == {"Size": "1"}, r
    assert "(offsets 0/0, 1 bit)" in r["action"]


def test_link_refusal_by_xae_carries_its_message_and_changes_nothing(monkeypatch):
    be, sm = _be(monkeypatch)
    with pytest.raises(X.XaeError, match="TSM_E_MISMATCHINGITEMS"):
        be.link(f"{T2}^Channel 5^Input", f"{T3}^Channel 2^Output")
    assert len(sm.links) == 2


def test_link_xae_reports_success_but_the_table_does_not_have_it(monkeypatch):
    be, sm = _be(monkeypatch)
    sm.LinkVariables = lambda *a: sm.calls.append(("LinkVariables",) + a)   # S_OK, nothing linked
    r = be.link(IN_MANUAL, f"{T2}^Channel 4^Input")
    assert "no new link" in r["warning"] and r["added"] == [], r
    with pytest.raises(X.XaeError, match="two different full tree paths"):
        be.link(IN_MANUAL, IN_MANUAL)


# ---- xae_unlink -------------------------------------------------------------------------------------------------------

def test_unlink_one_link_all_links_and_refuses_a_missing_one(monkeypatch):
    be, sm = _be(monkeypatch)
    sm.links.append((PLC, "PlcTask Inputs^GVL_IO.abProx[2]", T2, "Channel 4^Input", {}))
    r = be.unlink(IN_PROX2, f"{T2}^Channel 4^Input")
    assert sm.calls[-1] == ("UnlinkVariables", IN_PROX2, f"{T2}^Channel 4^Input")
    assert r["removed"] == [{"a": IN_PROX2, "b": f"{T2}^Channel 4^Input"}] and "warning" not in r, r
    r = be.unlink(OUT_PUMP)                                       # every link of the variable
    assert sm.calls[-1] == ("UnlinkVariables", OUT_PUMP, "") and len(r["removed"]) == 1, r
    n = len(sm.calls)
    with pytest.raises(X.XaeError, match=r"no link of .*GVL_IO.bManual in the mapping table — nothing was done"):
        be.unlink(IN_MANUAL)
    with pytest.raises(X.XaeError, match=r"no link of .*abProx\[2\] to .*Channel 9.* \(links naming it: "):
        be.unlink(IN_PROX2, f"{T2}^Channel 9^Input")
    assert len(sm.calls) == n


def test_unlink_xae_answers_s_false_and_the_link_stays(monkeypatch):
    be, sm = _be(monkeypatch)
    sm.UnlinkVariables = lambda *a: sm.calls.append(("UnlinkVariables",) + a)
    r = be.unlink(IN_PROX2)
    assert "still in the mapping table" in r["warning"], r


# ---- xae_links_import -------------------------------------------------------------------------------------------------

NEW = (f'<VarLinks><OwnerA Name="{PLC}"><OwnerB Name="{T2}">'
       '<Link VarA="PlcTask Inputs^GVL_IO.bManual" VarB="Channel 4^Input"/></OwnerB></OwnerA></VarLinks>')


def test_import_dry_run_add_and_replace_change_nothing(monkeypatch):
    be, sm = _be(monkeypatch)
    r = be.links_import(NEW, "add")
    assert r["dry_run"] and r["added"] == [{"a": IN_MANUAL, "b": f"{T2}^Channel 4^Input"}] and r["removed"] == [], r
    assert r["links_now"] == 2 and r["links_after"] == 3
    r = be.links_import(NEW, "replace")
    assert len(r["removed"]) == 2 and len(r["added"]) == 1, r
    r = be.links_import("<VarLinks/>", "replace")
    assert "every link of the project (2) would be removed" in r["warning"], r
    assert sm.calls == [] and len(sm.links) == 2


@pytest.mark.parametrize("bad,why", [("<VarLinks><OwnerA", "not a <VarLinks> table"),
                                     ("<Links/>", "the root element is <Links>"),
                                     (f'<VarLinks><OwnerA Name="{PLC}"><OwnerB Name="{T2}"><Link VarA="x"/></OwnerB>'
                                      '</OwnerA></VarLinks>', "a Link without both")])
def test_import_refuses_what_is_no_mapping_table(monkeypatch, bad, why):
    be, sm = _be(monkeypatch)
    with pytest.raises(X.XaeError, match=why):
        be.links_import(bad, "add")
    with pytest.raises(X.XaeError, match="mode is 'add'"):
        be.links_import(NEW, "merge")


def test_import_apply_writes_the_backup_first_and_checks_the_table_after(monkeypatch, tmp_path):
    be, sm = _be(monkeypatch)
    before = sm.ProduceMappingInfo()
    bk = tmp_path / "b" / "links.xml"
    r = be.links_import(NEW, "add", True, str(bk))
    assert bk.read_text(encoding="utf-8") == before and r["backup"] == str(bk), r
    consumed = [c for c in sm.calls if c[0] == "ConsumeMappingInfo"]
    assert len(consumed) == 1 and len(X._links_of(consumed[0][1])) == 3          # the whole table: old + new
    assert "warning" not in r and r["activation_required"] and "restore with xae_links_import" in r["note"], r
    r = be.links_import(NEW, "add", True, str(tmp_path / "b2.xml"))              # nothing would change
    assert "ConsumeMappingInfo was not called" in r["note"] and not (tmp_path / "b2.xml").exists()
    assert len([c for c in sm.calls if c[0] == "ConsumeMappingInfo"]) == 1


def test_import_apply_says_what_xae_did_not_take(monkeypatch, tmp_path):
    be, sm = _be(monkeypatch)
    real = sm.ConsumeMappingInfo

    def partly(xml):
        real(xml)
        sm.links = [lk for lk in sm.links if "bManual" not in lk[1]]   # the new link was not taken
    sm.ConsumeMappingInfo = partly
    r = be.links_import(NEW, "add", True, str(tmp_path / "bk.xml"))
    assert "1 link(s) missing" in r["warning"] and r["not_applied"] == [f"{IN_MANUAL} <-> {T2}^Channel 4^Input"], r


def test_merge_keeps_the_table_element_for_element():
    cur = (f'<VarLinks><OwnerA Name="{PLC}" Extra="1"><OwnerB Name="{T2}"><Link VarA="PlcTask Inputs^a" '
           'VarB="Channel 1^Input" Size="1"/></OwnerB></OwnerA></VarLinks>')
    out = X._merge_links(cur, X._links_of(NEW) + X._links_of(cur))
    root = ET.fromstring(out)
    assert root.find("OwnerA").get("Extra") == "1" and len(root.find("OwnerA").find("OwnerB").findall("Link")) == 2
    assert X._links_of(out)[0]["attrs"] == {"Size": "1"}


# ---- through the server -----------------------------------------------------------------------------------------------

def _server(monkeypatch, project_dir, **safety):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, **safety))
    app = srv._tcmcp_app
    app.dialogs_now = lambda: []
    be, sm = _be(monkeypatch)
    _solution(monkeypatch, be, str(project_dir / "Machine.sln"))
    app.xae = be
    return srv, app, sm


@pytest.mark.anyio
async def test_server_guarded_link_needs_the_token_of_that_link(monkeypatch, project_dir):
    srv, app, sm = _server(monkeypatch, project_dir, mode="guarded", allow_elicitation=False)
    tgt = f"{T2}^Channel 4^Input"
    async with client_for(srv) as s:
        r = await s.call_tool("xae_link", {"variable": IN_MANUAL, "target": tgt})
        assert r.is_error and sm.calls == [], text(r)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_link", "args": {
            "variable": IN_MANUAL, "target": f"{T2}^Channel 5^Input"}}))
        r = await s.call_tool("xae_link", {"variable": IN_MANUAL, "target": tgt, "confirm_token": tok["confirm_token"]})
        assert r.is_error and sm.calls == [], text(r)                     # a token for another channel
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_link", "args": {
            "variable": IN_MANUAL, "target": tgt}}))
        assert tok["target"] == f"{IN_MANUAL} -> {tgt}", tok
        r = payload(await s.call_tool("xae_link", {"variable": IN_MANUAL, "target": tgt,
                                                   "confirm_token": tok["confirm_token"]}))
        assert r["added"] and len(sm.calls) == 1, r
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_unlink", "args": {
            "variable": IN_MANUAL}}))
        assert tok["target"] == f"{IN_MANUAL} -> (all 1)", tok          # how many links it removes (review 2)


@pytest.mark.anyio
async def test_server_import_dry_run_is_free_apply_needs_the_token_of_that_content(monkeypatch, project_dir, tmp_path):
    srv, app, sm = _server(monkeypatch, project_dir, mode="guarded", allow_elicitation=False)
    monkeypatch.setenv("TCMCP_HOME", str(tmp_path / "home"))
    f = tmp_path / "new.xml"
    f.write_text(NEW, encoding="utf-8")
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_links_import", {"file": str(f)}))
        assert r["dry_run"] and len(r["added"]) == 1 and sm.calls == [], r
        dry = r
        r = await s.call_tool("xae_links_import", {"file": str(f), "xml": NEW})
        assert r.is_error and "one of them" in text(r), text(r)
        r = await s.call_tool("xae_links_import", {"file": str(f), "dry_run": False})
        assert r.is_error and sm.calls == [], text(r)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_links_import", "args": {
            "file": str(f), "mode": "add"}}))
        assert tok["target"] == dry["confirm_target"] == ("add:" + hashlib.sha256(NEW.encode()).hexdigest()[:16] +
                                                          f" over {dry['table_sha']} (+1 -0)"), (tok, dry)
        r = await s.call_tool("xae_links_import", {"xml": NEW.replace("Channel 4", "Channel 6"), "dry_run": False,
                                                   "confirm_token": tok["confirm_token"]})
        assert r.is_error and sm.calls == [], text(r)                     # other content, other token
        r = payload(await s.call_tool("xae_links_import", {"file": str(f), "dry_run": False,
                                                           "confirm_token": tok["confirm_token"]}))
    assert r["backup"].startswith(str(tmp_path / "home" / "backups" / "links")) and "links-Machine-" in r["backup"], r
    assert [c[0] for c in sm.calls] == ["ConsumeMappingInfo"] and "warning" not in r, r


@pytest.mark.anyio
async def test_server_readonly_refuses_the_changes_not_the_dry_run(monkeypatch, project_dir):
    srv, app, sm = _server(monkeypatch, project_dir, mode="readonly")
    async with client_for(srv) as s:
        for tool, args in (("xae_link", {"variable": IN_MANUAL, "target": f"{T2}^Channel 4^Input"}),
                           ("xae_unlink", {"variable": IN_PROX2}),
                           ("xae_links_import", {"xml": NEW, "dry_run": False})):
            r = await s.call_tool(tool, args)
            assert r.is_error and "readonly" in text(r).lower(), (tool, text(r))
        assert not (await s.call_tool("xae_links_import", {"xml": NEW})).is_error
    assert sm.calls == []


@pytest.mark.anyio
async def test_server_links_lists_what_the_saved_project_keeps_but_xae_could_not_restore(monkeypatch, project_dir):
    """DrumFillingMachineV0 (0.8.18 probe): 5 links on Term 6 marked RestoreInfo="ANotFound" in the .tsproj — their
    variables had moved to GVL_WeighingScale; ProduceMappingInfo does not list them."""
    srv, app, sm = _server(monkeypatch, project_dir)
    (project_dir / "Machine.sln").write_text(
        'Microsoft Visual Studio Solution File, Format Version 12.00\n'
        'Project("{B1E792BE-AA5F-4E3C-8C82-674BF9C0715B}") = "Machine", "Machine.tsproj", "{00000000-0000-0000-0000-'
        '000000000001}"\nEndProject\n', encoding="utf-8")
    (project_dir / "Machine.tsproj").write_text(
        '<?xml version="1.0"?><TcSmProject TcVersion="3.1.4026.12"><Project/><Mappings>'
        f'<OwnerA Name="{PLC}"><OwnerB Name="TIID^Device 1 (EtherCAT)^Term 6 (EL3356)">'
        '<Link VarA="PlcTask Inputs^GVL_Old.nLoad" VarB="RMB Value (INT32)^Value" RestoreInfo="ANotFound"/>'
        '<Link VarA="PlcTask Inputs^GVL_IO.nOk" VarB="Status^Ok"/>'
        '</OwnerB></OwnerA></Mappings></TcSmProject>', encoding="utf-8")
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_links", {"plc_project_path": "TIPC^Plc"}))
    nr = r["not_restored"]
    assert nr["count"] == 1 and nr["entries"][0] == {
        "file": str((project_dir / "Machine.tsproj").resolve()), "restore_info": "ANotFound", "element": "Link",
        "owner_a": PLC, "owner_b": "TIID^Device 1 (EtherCAT)^Term 6 (EL3356)", "var_a": "PlcTask Inputs^GVL_Old.nLoad",
        "var_b": "RMB Value (INT32)^Value"}, nr
    assert "they move nothing" in nr["note"], nr


# ---- review 1 of 0.8.20 -----------------------------------------------------------------------------------------------

SAFE = "TISC^Safe^Safe Instance"


@pytest.mark.anyio
async def test_r1_twinsafe_ends_are_refused_however_they_are_reached(monkeypatch, project_dir):
    srv, app, sm = _server(monkeypatch, project_dir, mode="full", confirm_destructive=False)
    monkeypatch.setattr(LinkSysMan, "OWNERS", (PLC, T2, T3, SAFE))
    sm.links.append((PLC, "PlcTask Outputs^GVL_IO.bEStop", SAFE, "Inputs^bIn", {}))
    async with client_for(srv) as s:
        r = await s.call_tool("xae_link", {"variable": f" {SAFE}^Inputs^b1", "target": f"{T2}^Channel 4^Input"})
        assert r.is_error and "TwinSAFE" in text(r), text(r)                 # a leading space hid it
        r = await s.call_tool("xae_unlink", {"variable": f"{PLC}^PlcTask Outputs^GVL_IO.bEStop"})
        assert r.is_error and "TwinSAFE" in text(r) and "nothing was done" in text(r), text(r)   # the other end
        safe_add = (f'<VarLinks><OwnerA Name="{SAFE}"><OwnerB Name="{T2}"><Link VarA="Inputs^b2" '
                    'VarB="Channel 5^Input"/></OwnerB></OwnerA></VarLinks>')
        r = await s.call_tool("xae_links_import", {"xml": safe_add})
        assert r.is_error and "TwinSAFE" in text(r), text(r)                 # even the dry run says it
        r = await s.call_tool("xae_links_import", {"xml": NEW, "mode": "replace"})
        assert r.is_error and "TwinSAFE" in text(r), text(r)                 # a replace would remove the safe link
    assert sm.calls == [], sm.calls


def test_r1_a_failed_import_says_what_the_table_holds_now_and_where_the_backup_is(monkeypatch, tmp_path):
    be, sm = _be(monkeypatch)

    def broken(xml):
        sm.calls.append(("ConsumeMappingInfo", xml))
        sm.links = []                                             # reset first, then failed
        raise ComError(-1739587580, "TSM_E_MISMATCHINGITEMS")
    sm.ConsumeMappingInfo = broken
    bk = tmp_path / "bk.xml"
    with pytest.raises(X.XaeError) as ei:
        be.links_import(NEW, "add", True, str(bk))
    msg = str(ei.value)
    assert "TSM_E_MISMATCHINGITEMS" in msg and "the mapping table now has 0 link(s), it had 2" in msg, msg
    assert f"xae_links_import(file={str(bk)!r}, mode='replace')" in msg and bk.exists(), msg
    with pytest.raises(FileExistsError):
        be.links_import(NEW.replace("Channel 4", "Channel 7"), "add", True, str(bk))   # never over a backup


@pytest.mark.parametrize("xml", [
    f'<VarLinks><Mappings><OwnerA Name="{PLC}"><OwnerB Name="{T2}"><Link VarA="PlcTask Inputs^a" VarB="Channel 1^Input"/>'
    '</OwnerB></OwnerA></Mappings></VarLinks>',
    f'<VarLinks><OwnerA Name="{PLC}"><OwnerB Name="{T2}"><X><Link VarA="PlcTask Inputs^a" VarB="Channel 1^Input"/></X>'
    '</OwnerB></OwnerA></VarLinks>'])
def test_r1_links_xae_would_not_read_are_refused_not_counted(monkeypatch, xml):
    be, sm = _be(monkeypatch)
    with pytest.raises(X.XaeError, match="outside <VarLinks><OwnerA><OwnerB>"):
        be.links_import(xml, "replace")


def test_r1_two_parts_of_one_pair_are_two_links(monkeypatch, tmp_path):
    be, sm = _be(monkeypatch)
    sm.links = [(PLC, "PlcTask Inputs^GVL_IO.nWord", T2, "Channel 1^Input", {"OffsA": "0", "Size": "1"}),
                (PLC, "PlcTask Inputs^GVL_IO.nWord", T2, "Channel 2^Input", {"OffsA": "1", "Size": "1"}),
                (PLC, "PlcTask Inputs^GVL_IO.nByte", T2, "Channel 3^Input", {"OffsA": "0", "Size": "1"}),
                (PLC, "PlcTask Inputs^GVL_IO.nByte", T2, "Channel 3^Input", {"OffsA": "1", "Size": "1"})]
    one = (f'<VarLinks><OwnerA Name="{PLC}"><OwnerB Name="{T2}"><Link VarA="PlcTask Inputs^GVL_IO.nByte" '
           'VarB="Channel 3^Input" OffsA="0" Size="1"/></OwnerB></OwnerA></VarLinks>')
    r = be.links_import(one, "replace")
    assert len(r["removed"]) == 3 and r["added"] == [], r              # the second part of nByte is a removal too
    other = one.replace('OffsA="0"', 'OffsA="2"')
    r = be.links_import(other, "add")
    assert r["added"] == [{"a": f"{PLC}^PlcTask Inputs^GVL_IO.nByte", "b": f"{T2}^Channel 3^Input",
                           "attrs": {"OffsA": "2", "Size": "1"}}] and r["removed"] == [], r
    r = be.links_import(other, "add", True, str(tmp_path / "bk.xml"))
    assert "warning" not in r and len(sm.links) == 5, r
    r = be.link(f"{PLC}^PlcTask Inputs^GVL_IO.nByte", f"{T2}^Channel 3^Input", 3, 0, 1)    # a part besides others
    assert r["added"] and len(r["pair_links_before"]) == 3, r


@pytest.mark.anyio
async def test_r1_a_replace_token_binds_the_table_it_replaces_and_a_link_token_its_part(monkeypatch, project_dir,
                                                                                         tmp_path):
    srv, app, sm = _server(monkeypatch, project_dir, mode="guarded", allow_elicitation=False)
    monkeypatch.setenv("TCMCP_HOME", str(tmp_path / "home"))
    async with client_for(srv) as s:
        dry = payload(await s.call_tool("xae_links_import", {"xml": NEW, "mode": "replace"}))
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_links_import", "args": {
            "xml": NEW, "mode": "replace"}}))
        assert tok["target"] == dry["confirm_target"] and " over " in tok["target"], (tok, dry)
        sm.links.append((PLC, "PlcTask Inputs^GVL_IO.bLate", T2, "Channel 8^Input", {}))   # made after the dry run
        r = await s.call_tool("xae_links_import", {"xml": NEW, "mode": "replace", "dry_run": False,
                                                   "confirm_token": tok["confirm_token"]})
        assert r.is_error and not [c for c in sm.calls if c[0] == "ConsumeMappingInfo"], text(r)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_link", "args": {
            "variable": IN_MANUAL, "target": f"{T2}^Channel 4^Input", "size_bits": 1}}))
        assert tok["target"].endswith("[offsets 0/0, 1 bit]"), tok
        r = await s.call_tool("xae_link", {"variable": IN_MANUAL, "target": f"{T2}^Channel 4^Input",
                                           "confirm_token": tok["confirm_token"]})
        assert r.is_error and not [c for c in sm.calls if c[0] == "LinkVariables"], text(r)   # the whole variable
    be, _sm = _be(monkeypatch)
    with pytest.raises(X.XaeError, match="the mapping table changed since the confirmation"):
        be.links_import(NEW, "replace", True, str(tmp_path / "b.xml"), None, "0000000000000000")


def _project_with_restore_info(project_dir, xti_restore=True):
    (project_dir / "Machine.sln").write_text(
        'Project("{B1E792BE-AA5F-4E3C-8C82-674BF9C0715B}") = "Machine", "Machine.tsproj", "{0}"\nEndProject\n',
        encoding="utf-8")
    (project_dir / "Machine.tsproj").write_text(
        '<?xml version="1.0"?><TcSmProject><Project><Io><Device File="Device 1 (EtherCAT).xti"/></Io></Project>'
        f'<Mappings><OwnerA Name="TIPC^Other^Other Instance"><OwnerB Name="TIID^X"><Link VarA="a" VarB="b" '
        'RestoreInfo="ANotFound"/></OwnerB></OwnerA></Mappings></TcSmProject>', encoding="utf-8")
    io = project_dir / "_Config" / "IO"
    io.mkdir(parents=True)
    (io / "Device 1 (EtherCAT).xti").write_text(
        '<TcSmItem><Device><Box File="Term 6 (EL3356).xti"/></Device></TcSmItem>', encoding="utf-8")
    (io / "Device 1 (EtherCAT)").mkdir()
    (io / "Device 1 (EtherCAT)" / "Term 6 (EL3356).xti").write_text(
        f'<TcSmItem><Mappings><OwnerA Name="{PLC}"><OwnerB Name="TIID^Device 1 (EtherCAT)^Term 6 (EL3356)">'
        '<Link VarA="PlcTask Inputs^GVL_Old.nLoad" VarB="RMB Value (INT32)^Value"'
        + (' RestoreInfo="ANotFound"' if xti_restore else '') + '/></OwnerB></OwnerA></Mappings></TcSmItem>',
        encoding="utf-8")
    old = project_dir / "old copy"
    old.mkdir()
    (old / "Term 9 (EL3356).xti").write_text(
        f'<TcSmItem><OwnerA Name="{PLC}"><OwnerB Name="TIID^Y"><Link VarA="x" VarB="y" RestoreInfo="ANotFound"/>'
        '</OwnerB></OwnerA></TcSmItem>', encoding="utf-8")


def test_r1_not_restored_reads_the_files_the_project_names_and_one_plc(project_dir):
    from twincat_mcp.project_backend import not_restored_links
    _project_with_restore_info(project_dir)
    nr = not_restored_links(project_dir / "Machine.sln", "TIPC^Plc")
    assert nr["count"] == 1 and nr["entries"][0]["var_a"] == "PlcTask Inputs^GVL_Old.nLoad", nr   # not the old copy,
    assert nr["entries"][0]["file"].endswith("Term 6 (EL3356).xti")                               # not the other PLC
    assert len(nr["files"]) == 3 and not any("old copy" in f for f in nr["files"]), nr
    assert not_restored_links(project_dir / "Machine.sln")["count"] == 2


@pytest.mark.anyio
async def test_r1_an_import_says_the_links_the_saved_project_could_not_restore(monkeypatch, project_dir):
    srv, app, sm = _server(monkeypatch, project_dir)
    _project_with_restore_info(project_dir)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_links_import", {"xml": NEW}))
    assert "the saved project has 2 link(s) TwinCAT could not restore" in r["not_restored_warning"], r
    assert "dry_run=false" in r["note"], r


def test_r1_the_long_io_path_and_a_namespaced_table(monkeypatch):
    be, sm = _be(monkeypatch)
    with pytest.raises(X.XaeError, match="are linked already"):
        be.link(IN_PROX2, "TIIC^I/O Devices^Device 1 (EtherCAT)^Term 1 (EK1100)^Term 2 (EL1008)^Channel 3^Input")
    sm.ProduceMappingInfo = lambda: '<VarLinks xmlns="urn:x"><OwnerA Name="a"/></VarLinks>'
    with pytest.raises(X.XaeError, match=r"the root element is <\{urn:x\}VarLinks>"):
        be.links_import(NEW, "add")


@pytest.mark.anyio
async def test_r1_a_utf16_file_is_read_and_unlink_is_destructive(monkeypatch, project_dir, tmp_path):
    srv, app, sm = _server(monkeypatch, project_dir, mode="full")
    f = tmp_path / "ps.xml"
    f.write_bytes(NEW.encode("utf-16"))                              # PowerShell 5.1 '>'
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_links_import", {"file": str(f)}))
        assert len(r["added"]) == 1, r
        f.write_bytes(b"\x80\x81 not text")
        r = await s.call_tool("xae_links_import", {"file": str(f)})
        assert r.is_error and "neither UTF-8 nor UTF-16" in text(r), text(r)
        r = await s.call_tool("xae_unlink", {"variable": IN_PROX2})
        assert r.is_error and "confirm" in text(r).lower(), text(r)   # full mode still confirms a destructive action
    assert sm.calls == []


# ---- review 2 of 0.8.20 -----------------------------------------------------------------------------------------------

def test_r2_no_import_while_the_table_has_a_twinsafe_end(monkeypatch, tmp_path):
    """ConsumeMappingInfo resets and rewrites every link: an add of one PLC link reset the TwinSAFE links too, and after
    a failure the restore it named was refused — such a table is not given to it at all."""
    monkeypatch.setattr(LinkSysMan, "OWNERS", (PLC, T2, T3, SAFE))
    be, sm = _be(monkeypatch)
    sm.links.append((SAFE, "Outputs^bLamp", T3, "Channel 2^Output", {}))
    guard = (lambda p: "'TISC…' is under the TwinSAFE/safety project" if p.lower().startswith("tisc") else None)
    for mode, xml in (("add", NEW), ("replace", sm.ProduceMappingInfo())):
        with pytest.raises(X.XaeError, match="ConsumeMappingInfo rewrites every link .* TwinSAFE"):
            be.links_import(xml, mode, False, None, guard)
        with pytest.raises(X.XaeError, match="rewrites every link"):
            be.links_import(xml, mode, True, str(tmp_path / f"{mode}.xml"), guard)
    assert not [c for c in sm.calls if c[0] == "ConsumeMappingInfo"] and not list(tmp_path.iterdir())


def test_r2_a_link_written_the_other_way_round_is_the_same_link(monkeypatch, tmp_path):
    be, sm = _be(monkeypatch)
    sm.links = [(PLC, "PlcTask Inputs^GVL_IO.nWord", T2, "Value", {"OffsA": "3", "Size": "08"})]
    same = (f'<VarLinks><OwnerA Name="{T2}"><OwnerB Name="{PLC}"><Link VarA="Value" VarB="PlcTask Inputs^GVL_IO.nWord" '
            'OffsB="3" Size="8" OffsA="0"/></OwnerB></OwnerA></VarLinks>')
    r = be.links_import(same, "add")
    assert r["added"] == [] and r["removed"] == [], r                      # reversed, '08' = '8', OffsA 0 = none
    other = same.replace('OffsB="3"', 'OffsB="0"').replace('OffsA="0"', 'OffsA="3"')   # the offset on the other end
    r = be.links_import(other, "add")
    assert len(r["added"]) == 1, r
    r = be.links_import(other, "replace")
    assert len(r["added"]) == 1 and len(r["removed"]) == 1, r


def test_r2_link_says_and_guards_what_linkvariables_replaces(monkeypatch):
    monkeypatch.setattr(LinkSysMan, "OWNERS", (PLC, T2, T3, SAFE))
    be, sm = _be(monkeypatch)
    real = sm.LinkVariables

    def one_source(v1, v2, *a):
        sm.links = [lk for lk in sm.links if f"{lk[2]}^{lk[3]}" != v2 and f"{lk[0]}^{lk[1]}" != v2]   # replaces
        real(v1, v2, *a)
    sm.LinkVariables = one_source
    r = be.link(f"{PLC}^PlcTask Outputs^GVL_IO.bHorn", f"{T3}^Channel 1^Output")
    assert "LinkVariables also removed 1 link(s)" in r["warning"] and r["target_also_linked_to"] == [OUT_PUMP], r
    sm.links.append((SAFE, "Outputs^bLamp", T3, "Channel 2^Output", {}))
    guard = (lambda p: "'TISC…' is under the TwinSAFE/safety project" if p.lower().startswith("tisc") else None)
    n = len(sm.calls)
    with pytest.raises(X.XaeError, match="TwinSAFE"):
        be.link(f"{PLC}^PlcTask Outputs^GVL_IO.bHorn2", f"{T3}^Channel 2^Output", protect=guard)
    assert len(sm.calls) == n


def test_r2_a_failed_import_keeps_the_class_of_its_error(monkeypatch, tmp_path):
    be, sm = _be(monkeypatch)

    def rejected(xml):
        raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
    sm.ConsumeMappingInfo = rejected
    with pytest.raises(X.XaeBusy):
        be.links_import(NEW, "add", True, str(tmp_path / "a.xml"))
    assert len(sm.links) == 2


def test_r2_texts_after_an_action_and_an_empty_dry_run(monkeypatch):
    be, sm = _be(monkeypatch)
    real = sm.ProduceMappingInfo
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise ComError(-2147467259, "E_FAIL")
        return real()
    sm.ProduceMappingInfo = flaky
    r = be.link(IN_MANUAL, f"{T2}^Channel 4^Input")
    assert "could not be read after it" in r["after_unread"] and "nothing was done" not in r["after_unread"], r
    sm.ProduceMappingInfo = real
    r = be.links_import(sm.ProduceMappingInfo(), "replace")
    assert "the table would not change" in r["note"], r


def test_r2_the_long_io_form_duplicates_and_an_empty_unlink_target(monkeypatch):
    be, sm = _be(monkeypatch)
    long_form = (f'<VarLinks><OwnerA Name="{PLC}"><OwnerB Name="TIIC^I/O Devices^Device 1 (EtherCAT)^Term 1 (EK1100)^'
                 'Term 2 (EL1008)"><Link VarA="PlcTask Inputs^GVL_IO.abProx[2]" VarB="Channel 3^Input"/></OwnerB>'
                 '</OwnerA></VarLinks>')
    assert be.links_import(long_form, "add")["added"] == []
    twice = NEW.replace("</OwnerB>", '<Link VarA="PlcTask Inputs^GVL_IO.bManual" VarB="Channel 4^Input"/></OwnerB>')
    with pytest.raises(X.XaeError, match="is in it twice"):
        be.links_import(twice, "replace")
    with pytest.raises(X.XaeError, match="is no tree path"):
        be.unlink(IN_PROX2, " ^ ")
    assert sm.calls == []


@pytest.mark.anyio
async def test_r2_an_unlink_all_token_binds_how_many_links(monkeypatch, project_dir):
    srv, app, sm = _server(monkeypatch, project_dir, mode="guarded", allow_elicitation=False)
    async with client_for(srv) as s:
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_unlink", "args": {
            "variable": IN_PROX2}}))
        assert tok["target"] == f"{IN_PROX2} -> (all 1)", tok
        sm.links.append((PLC, "PlcTask Inputs^GVL_IO.abProx[2]", T2, "Channel 6^Input", {}))   # after the token
        r = await s.call_tool("xae_unlink", {"variable": IN_PROX2, "confirm_token": tok["confirm_token"]})
        assert r.is_error and sm.calls == [], text(r)


@pytest.mark.anyio
async def test_r2_xae_links_lists_one_plcs_not_restored_and_what_it_could_not_place(monkeypatch, project_dir):
    srv, app, sm = _server(monkeypatch, project_dir)
    _project_with_restore_info(project_dir)
    io = project_dir / "_Config" / "IO"
    bk = project_dir / "Backup" / "_Config" / "IO" / "Device 1 (EtherCAT)"
    bk.mkdir(parents=True)
    (bk / "Term 6 (EL3356).xti").write_text("<TcSmItem/>", encoding="utf-8")       # a copy elsewhere: not read
    (io / "Device 1 (EtherCAT).xti").write_text(
        '<TcSmItem><Device><Box File="Term 6 (EL3356).xti"/><Box File="Term 7 (EL1008).xti"/></Device></TcSmItem>',
        encoding="utf-8")
    (io / "Device 1 (EtherCAT)" / "Odd.xti").write_text('<?xml version="1.0" encoding="no-such"?><X RestoreInfo="a"/>',
                                                         encoding="utf-8")
    (io / "Device 1 (EtherCAT).xti").write_text(
        '<TcSmItem><Device><Box File="Term 6 (EL3356).xti"/><Box File="Term 7 (EL1008).xti"/><Box File="Odd.xti"/>'
        '<Box RestoreInfo="BNotFound" Name="Term 8"/></Device></TcSmItem>', encoding="utf-8")
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_links", {"plc_project_path": "TIPC^Plc"}))
    nr = r["not_restored"]
    assert nr["count"] == 1 and nr["entries"][0]["var_a"] == "PlcTask Inputs^GVL_Old.nLoad", nr   # not TIPC^Other
    assert any("Term 7 (EL1008).xti" in u and "not found" in u for u in nr["unresolved"]), nr
    assert nr["other_marked"][0]["name"] == "Term 8" and any("Odd.xti" in u for u in nr["unread_files"]), nr
