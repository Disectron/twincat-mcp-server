"""XAE backend tests with fake COM objects (the real thing needs Windows + XAE)."""
import pytest

from twincat_mcp.config import XaeConfig
from twincat_mcp import xae_backend as X


class ComError(Exception):
    def __init__(self, hresult, desc="boom"):
        super().__init__(desc)
        self.hresult = hresult
        self.excepinfo = (0, "src", desc)


class FakeItem:
    def __init__(self, name, path, children=None, item_type=0, xml="<TreeItem/>"):
        self.Name, self.PathName, self.ItemType, self.ItemSubType = name, path, item_type, 0
        self._children = children or []
        self._xml = xml
        self.DeclarationText = f"// decl of {name}"
        self.ImplementationText = f"// impl of {name}"
        self.consumed = []
        self.created = []
        self.deleted = []
        self.BootProjectAutostart = False
        self.generated = []
        self.imported = []

    @property
    def ChildCount(self):
        return len(self._children)

    def Child(self, i):
        return self._children[i - 1]

    def ProduceXml(self, _=False):
        return self._xml

    def ConsumeXml(self, xml):
        self.consumed.append(xml)

    def CreateChild(self, name, subtype, before="", info=None):
        it = FakeItem(name, f"{self.PathName}^{name}")
        self.created.append((name, subtype, before, info))
        self._children.append(it)
        return it

    def DeleteChild(self, name):
        self.deleted.append(name)

    def GenerateBootProject(self, activate):
        self.generated.append(activate)

    def PlcOpenImport(self, f, opt, selection="", folders=True):
        self.imported.append((f, opt))
        if getattr(self, "on_import", None):
            self.on_import()

    def PlcOpenExport(self, f, items):
        self.exported = (f, items)


class FakeSysMan:
    def __init__(self):
        self.net = "5.1.2.3.1.1"
        self.started = True
        self.activated = 0
        self.restarted = 0
        mainpou = FakeItem("MAIN", "TIPC^Plc^Plc Project^POUs^MAIN")
        pous = FakeItem("POUs", "TIPC^Plc^Plc Project^POUs", [mainpou])
        proj = FakeItem("Plc Project", "TIPC^Plc^Plc Project", [pous])
        plc = FakeItem("Plc", "TIPC^Plc", [proj])
        self.tipc = FakeItem("TIPC", "TIPC", [plc])
        task = FakeItem("PlcTask", "TIRC^Tasks^PlcTask", xml='<TreeItem><TaskDef><CycleTime>100000</CycleTime></TaskDef></TreeItem>')
        self.items = {it.PathName: it for it in (self.tipc, plc, proj, pous, mainpou, task)}

    def GetTargetNetId(self): return self.net
    def SetTargetNetId(self, n): self.net = n
    def IsTwinCATStarted(self): return self.started
    def ActivateConfiguration(self): self.activated += 1
    def StartRestartTwinCAT(self): self.restarted += 1; self.started = True
    def LookupTreeItem(self, path):
        if path not in self.items:
            raise ComError(-2147467259, f"no item {path}")
        return self.items[path]


class FakeProject:
    def __init__(self, name, obj):
        self.Name, self.Object, self.FullName, self.Kind = name, obj, f"C:\\P\\{name}.tsproj", "{TC}"


class FakeProjects:
    def __init__(self, lst): self._l = lst
    @property
    def Count(self): return len(self._l)
    def Item(self, i): return self._l[i - 1]


class FakeSolutionBuild:
    def __init__(self, fail=0): self.LastBuildInfo = fail; self.BuildState = 3; self.calls = []
    def Build(self, wait): self.calls.append(("build", wait))
    def Clean(self, wait): self.calls.append(("clean", wait))


class FakeDTE:
    def __init__(self, sysman, build_fail=0, output=""):
        self.Version, self.Edition = "17.0", "TcXaeShell"
        self.Solution = type("Sol", (), {})()
        self.Solution.FullName = r"C:\P\Machine.sln"
        self.Solution.IsOpen = True
        self.Solution.Projects = FakeProjects([FakeProject("Other", None), FakeProject("Machine", sysman)])
        self.Solution.SolutionBuild = FakeSolutionBuild(build_fail)
        self.commands = []
        self._output = output
        self.Commands = FakeProjects([type("C", (), {"Name": n})() for n in ("PLC.Login", "PLC.Logout", "File.SaveAll", "Build.BuildSolution")])
        self.Windows = self

    def ExecuteCommand(self, name, args=""):
        if name.startswith("OtherContextMenus.ErrorList"):
            return
        if name not in [c.Name for c in self.Commands._l]:
            raise ComError(-2147352573, f"Command \"{name}\" is not available.")
        self.commands.append((name, args))

    # Windows.Item(guid).Object emulation
    def Item(self, guid):
        if guid == X.VS_WINDOW_OUTPUT:
            pane = type("Pane", (), {})()
            sel = type("Sel", (), {"Text": self._output, "StartOfDocument": lambda s, b: None, "EndOfDocument": lambda s, b: None})()
            pane.TextDocument = type("Doc", (), {"Selection": sel})()
            panes = type("Panes", (), {"Item": lambda s, n: pane})()
            return type("Win", (), {"Object": type("OW", (), {"OutputWindowPanes": panes})()})()
        raise ComError(-2147352567, "no window")


@pytest.fixture
def xae(monkeypatch):
    cfg = XaeConfig(com_retry_s=0.3)
    be = X.XaeBackend(cfg)
    sm = FakeSysMan()
    dte = FakeDTE(sm)
    monkeypatch.setattr(be, "_require_win", lambda: None)
    monkeypatch.setattr(be, "_rot_candidates", lambda: [{"progid": "TcXaeShell.DTE.17.0", "moniker": "!TcXaeShell.DTE.17.0:1", "dte": dte, "solution": dte.Solution.FullName}])
    monkeypatch.setattr(be, "_dte2", lambda: None)
    return be, dte, sm


def test_attach_and_status(xae):
    be, dte, sm = xae
    info = be.attach()
    assert info["attached"] and info["progid"] == "TcXaeShell.DTE.17.0"
    assert info["twincat_project"] == "Machine" and info["target_net_id"] == "5.1.2.3.1.1"
    assert not info["launched"]


def test_attach_prefers_matching_solution(monkeypatch):
    be = X.XaeBackend(XaeConfig(solution_path=r"C:\P\Other.sln"))
    monkeypatch.setattr(be, "_require_win", lambda: None)
    d1 = FakeDTE(FakeSysMan()); d2 = FakeDTE(FakeSysMan()); d2.Solution.FullName = r"C:\P\Other.sln"
    monkeypatch.setattr(be, "_rot_candidates", lambda: [
        {"progid": "TcXaeShell.DTE.17.0", "moniker": "a", "dte": d1, "solution": d1.Solution.FullName},
        {"progid": "TcXaeShell.DTE.15.0", "moniker": "b", "dte": d2, "solution": d2.Solution.FullName}])
    be.attach()
    assert be.dte is d2


def test_attach_refuses_launch_by_default(monkeypatch):
    be = X.XaeBackend(XaeConfig())
    monkeypatch.setattr(be, "_require_win", lambda: None)
    monkeypatch.setattr(be, "_rot_candidates", lambda: [])
    with pytest.raises(X.XaeError, match="No running XAE shell"):
        be.attach()


def test_build_parses_output_pane_when_error_list_unavailable(xae):
    be, dte, sm = xae
    dte._output = ("1>------ Build started ------\n"
                   r"1>C:\P\Plc\POUs\MAIN.TcPOU(12,5): error C0046: Identifier 'nFoo' not defined" "\n"
                   r"1>C:\P\Plc\POUs\FB_Fill.TcPOU [3] : warning C0371: unused variable" "\n"
                   "========== Build: 0 succeeded, 1 failed ==========")
    dte.Solution.SolutionBuild.LastBuildInfo = 1
    r = be.build()
    assert not r["success"] and r["errors"] == 1 and r["warnings"] == 1
    assert r["items"][0]["file"].endswith("MAIN.TcPOU") and r["items"][0]["line"] == 12
    assert r["error_source"] == "OutputPane(2)"
    assert ("build", True) in dte.Solution.SolutionBuild.calls


def test_build_success_and_clean(xae):
    be, dte, sm = xae
    r = be.build(clean=True)
    assert r["success"] and dte.Solution.SolutionBuild.calls[0] == ("clean", True)


def test_com_retry_on_busy(xae):
    be, dte, sm = xae
    be.attach()
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ComError(X.RPC_E_CALL_REJECTED)
        return "ok"
    assert be._retry(flaky) == "ok" and calls["n"] == 3

    def dead():
        raise ComError(X.RPC_E_DISCONNECTED)
    with pytest.raises(X.XaeError, match="disconnected"):
        be._retry(dead)
    assert be.dte is None


def test_com_rejected_by_modal_dialog_fails_fast(xae, monkeypatch):
    """#79 / P15: a rejected call while a modal dialog is open names the dialog after ~1 s instead of retrying
    for com_retry_s (60 s) and ending in a raw com_error."""
    import time as _t
    be, dte, sm = xae
    be.attach()
    monkeypatch.setattr(be, "_modal_dialogs", lambda: [{"title": "Microsoft Visual Studio", "buttons": ["Save", "Don't Save", "Cancel"],
                                                        "text": "Save changes to the following items?"}])

    def rejected():
        raise ComError(X.RPC_E_CALL_REJECTED)
    t0 = _t.time()
    with pytest.raises(X.XaeError, match="BLOCKED BY DIALOG 'Microsoft Visual Studio'.*Don't Save"):
        be._retry(rejected, "ExecuteCommand File.CloseSolution")
    assert _t.time() - t0 < 5


def test_target_activate_restart(xae):
    be, dte, sm = xae
    assert be.target_get()["target_net_id"] == "5.1.2.3.1.1"
    with pytest.raises(X.XaeError):
        be.target_set("not-a-netid")
    be.target_set("5.9.9.9.1.1")
    assert sm.net == "5.9.9.9.1.1"
    assert be.activate_configuration()["activated"] and sm.activated == 1
    assert be.restart_twincat()["twincat_started"] and sm.restarted == 1


def test_tree_ops(xae):
    be, dte, sm = xae
    ch = be.tree_children("TIPC", depth=3)
    assert [c["Name"] for c in ch["items"]] == ["Plc", "Plc Project", "POUs"]
    pj = be.plc_projects()["projects"]
    assert [(x["name"], x["path"], x["plc_project_path"]) for x in pj] == [("Plc Project", "TIPC^Plc^Plc Project", "TIPC^Plc^Plc Project")]
    r = be.tree_set_values("TIRC^Tasks^PlcTask", {"TaskDef/CycleTime": "50000"})
    assert r["changed"]["TaskDef/CycleTime"] == {"old": "100000", "new": "50000"}
    assert "<CycleTime>50000</CycleTime>" in sm.items["TIRC^Tasks^PlcTask"].consumed[0]
    with pytest.raises(X.XaeError, match="not found"):
        be.tree_set_values("TIRC^Tasks^PlcTask", {"Nope/X": "1"})
    with pytest.raises(X.XaeError, match="no item"):
        be.tree_item("TIPC^Missing")


def test_n17_tree_descends_into_nested_plc_project(xae):
    """#17: TIPC^Untitled1 enumerated only 'Untitled1 Instance'; the nested 'Untitled1 Project' (POUs …) was never
    walked because Child(i) did not return it."""
    be, dte, sm = xae
    proj = sm.items["TIPC^Plc^Plc Project"]
    inst = FakeItem("Plc Instance", "TIPC^Plc^Plc Instance")

    class Container(FakeItem):
        def Child(self, i):
            if i == 1:
                raise ComError(-2147467259, "nested project not enumerable")
            return inst
    c = Container("Plc", "TIPC^Plc", [proj, inst])
    sm.tipc._children[0] = c
    sm.items["TIPC^Plc"] = c
    ch = be.tree_children("TIPC", depth=3)
    names = [x["Name"] for x in ch["items"]]
    assert names == ["Plc", "Plc Project", "POUs", "Plc Instance"], names
    assert ch["unreadable_children"][0]["parent"] == "TIPC^Plc" and ch["unreadable_children"][0]["index"] == 1
    assert [p["plc_project_path"] for p in be.plc_projects()["projects"]] == ["TIPC^Plc^Plc Project"]


def test_pou_get_set_create(xae):
    be, dte, sm = xae
    g = be.pou_get("TIPC^Plc^Plc Project^POUs^MAIN")
    assert g["declaration"] == "// decl of MAIN"
    be.pou_set("TIPC^Plc^Plc Project^POUs^MAIN", "PROGRAM MAIN\nVAR\nEND_VAR", None)
    assert sm.items["TIPC^Plc^Plc Project^POUs^MAIN"].DeclarationText.startswith("PROGRAM MAIN")
    pous = sm.items["TIPC^Plc^Plc Project^POUs"]
    be.pou_create("TIPC^Plc^Plc Project^POUs", "FB_New", "fb", extends="FB_Base", implementation="; // body")
    name, sub, before, info = pous.created[-1]
    assert (name, sub, info) == ("FB_New", 604, ["1", "Extends", "FB_Base"])
    assert pous._children[-1].ImplementationText == "; // body"
    be.pou_create("TIPC^Plc^Plc Project^POUs", "FB_Plain", "fb")
    assert pous.created[-1][3] == 1  # single value → plain language code
    be.pou_create("TIPC^Plc^Plc Project^POUs", "F_Add", "function", return_type="LREAL")
    assert pous.created[-1][1:] == (603, "", ["1", "LREAL"])
    be.pou_create("TIPC^Plc^Plc Project^POUs", "ST_New", "struct", declaration="TYPE ST_New : STRUCT END_STRUCT END_TYPE")
    assert pous.created[-1][1] == 606 and pous.created[-1][3].startswith("TYPE ST_New")
    with pytest.raises(X.XaeError, match="kind must be"):
        be.pou_create("TIPC^Plc^Plc Project^POUs", "X", "thing")


def test_n80_a_property_is_created_with_its_accessors(xae):
    """N80: kind='property' made a property without Get/Set (C0373); property_get with vInfo 1 (Int32) was refused
    ("vInfo (Type: Int32) is not supported for creating TREEITEMTYPE_PLCPROPGET"): the language goes as text."""
    be, dte, sm = xae
    fb = sm.items["TIPC^Plc^Plc Project^POUs"]
    r = be.pou_create("TIPC^Plc^Plc Project^POUs", "Speed", "property", return_type="LREAL", access="PUBLIC")
    assert fb.created[-1][1:] == (611, "", ["1", "LREAL", "PUBLIC"])
    prop = fb._children[-1]
    assert [(c[0], c[1], c[3]) for c in prop.created] == [("Get", 613, ["1"]), ("Set", 614, ["1"])]
    assert r["accessors"] == ["TIPC^Plc^Plc Project^POUs^Speed^Get", "TIPC^Plc^Plc Project^POUs^Speed^Set"]
    be.pou_create("TIPC^Plc^Plc Project^POUs", "Ro", "property", return_type="INT", accessors="get")
    assert [c[1] for c in fb._children[-1].created] == [613]
    sm.items["TIPC^Plc^Plc Project^POUs^Speed"] = prop
    be.pou_create("TIPC^Plc^Plc Project^POUs^Speed", "Set", "property_set", access="PROTECTED")
    assert prop.created[-1][1:] == (614, "", ["1", "PROTECTED"])
    be.pou_create("TIPC^Plc^Plc Project^POUs", "Pos", "itf_property", return_type="LREAL")
    assert [(c[1], c[3]) for c in fb._children[-1].created] == [(654, None), (655, None)]
    with pytest.raises(X.XaeError, match="accessors must name get and/or set"):
        be.pou_create("TIPC^Plc^Plc Project^POUs", "Bad", "property", accessors="")
    # an accessor that cannot be created: the half-made property goes again (all or nothing)
    real = X.XaeBackend._create_child

    def flaky(self, parent, name, sub, before, info):
        if sub == 614:
            raise X.XaeError("CreateChild Set: refused")
        return real(self, parent, name, sub, before, info)
    import types as _t
    be._create_child = _t.MethodType(flaky, be)
    with pytest.raises(X.XaeError, match="creating its Set accessor failed.*was deleted again"):
        be.pou_create("TIPC^Plc^Plc Project^POUs", "Half", "property", return_type="BOOL")
    assert fb.deleted[-1] == "Half"


def test_plcopen_and_boot(xae, tmp_path):
    be, dte, sm = xae
    f = tmp_path / "x.xml"; f.write_text("<project/>")
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    # TE1000 PLCIMPORTOPTIONS: REPLACE 1, RENAME 2; a test double has no type library, so the enum page decides and the
    # result says so. N97: 'replace' (in place) imports as RENAME — TwinCAT's own REPLACE moves objects to the root
    assert sm.items["TIPC^Plc^Plc Project"].imported == [(str(f), 2)]
    assert r["option_value"]["value"] == 2 and "enum page" in r["option_value"]["from"]
    be.plcopen_import("TIPC^Plc^Plc Project", str(f), "rename")
    assert sm.items["TIPC^Plc^Plc Project"].imported[-1] == (str(f), 2)
    be.plcopen_import("TIPC^Plc^Plc Project", str(f), "skip")
    assert sm.items["TIPC^Plc^Plc Project"].imported[-1] == (str(f), 3)
    with pytest.raises(X.XaeError, match="not found"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(tmp_path / "missing.xml"))
    be.plcopen_export("TIPC^Plc^Plc Project", ["POUs.MAIN", "POUs.FB_Fill"], str(tmp_path / "out.xml"))
    assert sm.items["TIPC^Plc^Plc Project"].exported[1] == "POUs.MAIN;POUs.FB_Fill"
    (tmp_path / "exists.xml").write_text("x")           # TE1000: PlcOpenExport refuses an existing file / missing folder
    with pytest.raises(X.XaeError, match="does not overwrite"):
        be.plcopen_export("TIPC^Plc^Plc Project", ["POUs.MAIN"], str(tmp_path / "exists.xml"))
    with pytest.raises(X.XaeError, match="does not exist"):
        be.plcopen_export("TIPC^Plc^Plc Project", ["POUs.MAIN"], str(tmp_path / "nope" / "o.xml"))
    # R14: the boot project belongs to ITcPlcProject = 'TIPC^Plc' (Beckhoff AI docs); either path form is accepted
    r = be.boot_project("TIPC^Plc^Plc Project", True, True)
    assert r["path"] == "TIPC^Plc" and r["autostart"] and sm.items["TIPC^Plc"].generated == [False]
    assert X._plc_split("TIPC^Plc") == ("TIPC^Plc", "TIPC^Plc^Plc Project") == X._plc_split("TIPC^Plc^Plc Project")
    assert X._plc_split("TIPC^Plc^Plc Project^POUs") == ("TIPC^Plc^Plc Project^POUs",) * 2
    be.plcopen_export("TIPC^Plc", ["POUs.MAIN"], str(tmp_path / "o2.xml"))          # was AttributeError
    assert sm.items["TIPC^Plc^Plc Project"].exported[0].endswith("o2.xml")


def _no_consume_xml(sm):
    def refuse(xml):
        raise ComError(-2147467259, "ConsumeXml not supported here")
    sm.items["TIPC^Plc^Plc Project"].ConsumeXml = refuse


def test_plc_action_fallback_and_commands(xae):
    be, dte, sm = xae
    _no_consume_xml(sm)                                    # the DTE-command route (fallback)
    r = be.plc_action("login")
    assert r["command"] == "PLC.Login" and dte.commands[-1][0] == "PLC.Login"
    with pytest.raises(X.XaeError, match="no working DTE command"):
        be.plc_action("start")
    assert be.list_commands("plc")["commands"] == ["PLC.Login", "PLC.Logout"]


def test_parse_build_output_formats():
    txt = ("C:\\P\\Plc\\POUs\\MAIN.TcPOU(12,5): error C0046: Identifier 'x' not defined\n"
           "Error 3 Some generic message\n"
           "  warning : library not found\n"
           "nothing here")
    items = X._parse_build_output(txt)
    assert len(items) == 3
    assert items[0]["line"] == 12 and items[0]["level"] == "error"
    assert items[2]["level"] == "warning"


def test_io_disable_and_links(xae):
    be, dte, sm = xae
    dev1 = FakeItem("Device 1 (EtherCAT)", "TIID^Device 1 (EtherCAT)")
    dev2 = FakeItem("Device 2 (USB)", "TIID^Device 2 (USB)")
    tiid = FakeItem("TIID", "TIID", [dev1, dev2])
    sm.items["TIID"] = tiid
    r = be.io_disable(True, only=["Device 1 (EtherCAT)"])
    assert r["devices"] == [{"device": "Device 1 (EtherCAT)", "disabled": True}] and dev1.Disabled == 1
    r = be.io_disable(False)
    assert all(d["disabled"] is False for d in r["devices"]) and dev2.Disabled == 0
    inputs = FakeItem("PlcTask Inputs", "TIPC^Plc^Plc Instance^PlcTask Inputs", xml=
        '<TreeItem><VarGrp><Var><Name>MAIN.bStart</Name><Type>BOOL</Type><LinkedTo>TIID^Device 1 (EtherCAT)^Term 2 (EL1008)^Channel 1^Input</LinkedTo></Var>'
        '<Var><Name>GVL_Machine.bEStop</Name><Type>BOOL</Type></Var></VarGrp></TreeItem>')
    outputs = FakeItem("PlcTask Outputs", "TIPC^Plc^Plc Instance^PlcTask Outputs", xml='<TreeItem><VarGrp><Var><Name>MAIN.fbFill.fbValve.bOpen</Name><Type>BOOL</Type><LinkedTo>TIID^X^Y</LinkedTo></Var></VarGrp></TreeItem>')
    sm.items["TIPC^Plc^Plc Instance"] = FakeItem("Plc Instance", "TIPC^Plc^Plc Instance", [inputs, outputs])
    r = be.links("TIPC^Plc^Plc Project")
    assert r["unlinked"] == [{"group": "PlcTask Inputs", "name": "GVL_Machine.bEStop", "type": "BOOL"}]
    assert r["groups"][1]["variables"][0]["linked_to"] == "TIID^X^Y"


def test_stale_system_manager_refreshed(xae, monkeypatch):
    """N9: after a solution close/reopen, System Manager calls fail with DISP_E_EXCEPTION / E_POINTER until the
    cached object is re-found — the retry does that once by itself."""
    be, dte, sm = xae
    be.attach()
    calls = {"n": 0, "found": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            e = ComError(X.DISP_E_EXCEPTION, "Invalid pointer")
            e.excepinfo = (0, "TcSysManager", "Invalid pointer", None, 0, X.E_POINTER)
            raise e
        return "ok"
    orig = be._find_sysman

    def counting():
        calls["found"] += 1
        return orig()
    monkeypatch.setattr(be, "_find_sysman", counting)
    assert be._retry(flaky, "LookupTreeItem") == "ok" and calls["found"] == 1


def test_d44_save_all_saves_what_saveall_left_and_reports_it(xae):
    be, dte, sm = xae

    class Doc:
        def __init__(self, name, stuck=False):
            self.FullName, self.Saved, self.stuck = name, False, stuck

        def Save(self):
            if not self.stuck:
                self.Saved = True
    d1, d2 = Doc(r"C:\P\Plc\POUs\MAIN.TcPOU"), Doc(r"C:\P\Plc\POUs\FB_Test.TcPOU", stuck=True)
    dte.Documents = FakeProjects([d1, d2])
    r = be.save_all()
    assert ("File.SaveAll", "") in dte.commands
    assert r["dirty_before"] == [d1.FullName, d2.FullName] and r["dirty_after"] == [d2.FullName] and r["saved"] is False
    d2.stuck = False
    assert be.save_all() == {"saved": True, "dirty_before": [d2.FullName], "dirty_after": []}


def test_d41_4026_compiler_lines_keep_file_object_and_line():
    """D41: 4026 writes 'File.TcPOU@Method(9) : error: C0185 …' — file/line came back empty."""
    txt = (r"1>C:\Prj\Untitled1\95_UnitTests\FB_CounterModule_Tests.TcPOU@Test_CountsRisingEdges(9) : error: C0185: "
           r"It is not possible to perform component access '.' on 'fbUut'" "\n"
           r"1>C:\Prj\Untitled1\POUs\MAIN.TcPOU(14) : warning: C0373: Unused variable 'x'" "\n"
           r"1>C:\Prj\Untitled1\POUs\FB_A.TcPOU(3,7): error C0046: Identifier 'y' not defined" "\n")
    a, b, c = X._parse_build_output(txt)
    assert a["file"].endswith("FB_CounterModule_Tests.TcPOU") and a["object"] == "Test_CountsRisingEdges" and a["line"] == 9
    assert a["level"] == "error" and a["code"] == "C0185" and a["description"].startswith("It is not possible")
    assert b["level"] == "warning" and b["line"] == 14 and b["code"] == "C0373" and "object" not in b
    assert c["line"] == 3 and c["column"] == 7 and c["code"] == "C0046"


def test_d20_diagnostics_reads_the_error_list_view_and_merges_the_build_pane(xae, monkeypatch):
    """D20: ErrorItems returned 0 while XAE showed TwinCAT warnings (Scope View 'Server Reset or a lost connection');
    xae_diagnostics then reported only the Output pane."""
    be, dte, sm = xae
    empty = type("C", (), {"Count": 0, "Item": lambda self, i: None})()
    tw = type("TW", (), {"ErrorList": type("EL", (), {"ErrorItems": empty})()})()
    monkeypatch.setattr(be, "_dte2_late", lambda: type("D2", (), {"ToolWindows": tw})())
    copied = ("Severity\tCode\tDescription\tProject\tFile\tLine\tSuppression State\n"
              "Warning\t\tTwinCAT Measurement: Server Reset or a lost connection\tScope_Counter\t\t\t\n"
              "Warning\t\tScope View: system changed\tScope_Counter\t\t\t\n"
              "Error\tC0046\tIdentifier 'nFoo' not defined\tUntitled1\tC:\\P\\Plc\\POUs\\MAIN.TcPOU\t12\t\n")
    monkeypatch.setattr(be, "_error_list_copy", lambda: X._parse_error_list_copy(copied))
    dte._output = (r"1>C:\P\Plc\POUs\MAIN.TcPOU(12,5): error C0046: Identifier 'nFoo' not defined" "\n"
                   r"1>C:\P\Plc\POUs\FB_A.TcPOU(3) : warning: C0373: Unused variable 'x'" "\n")
    assert "view copy" not in be.errors()["source"]                             # a build does not grab the focus
    e = be.errors(view_copy=True)
    descs = [i["description"] for i in e["items"]]
    assert e["count"] == 4 and "TwinCAT Measurement: Server Reset or a lost connection" in descs
    assert descs.count("Identifier 'nFoo' not defined") == 1                     # the same entry from both: once
    assert e["source"] == "ErrorList(view copy)(3) + OutputPane(1)", e["source"]
    assert X._parse_error_list_copy("") == [] and X._parse_error_list_copy("no header\nrow") == []


def test_n10_logout_selects_the_plc_project_without_toolwindows(xae, monkeypatch):
    """#10: 4026 offers only PLC.Logoutfromnone until a PLC project is active; the node selection went through
    dte.ToolWindows, which the cached 7.0 DTE wrapper does not have — so it never happened."""
    be, dte, sm = xae
    _no_consume_xml(sm)                                    # the Solution Explorer route (fallback since 0.8.7)
    Cmd = lambda n: type("C", (), {"Name": n})()                                   # noqa: E731
    dte.Commands = FakeProjects([Cmd("PLC.Logoutfromnone"), Cmd("File.SaveAll")])

    class Node:
        def __init__(self, name, kids=()):
            self.Name, self.kids, self.selected = name, list(kids), False
            self.UIHierarchyItems = self

        Expanded = False
        @property
        def Count(self): return len(self.kids)
        def Item(self, i): return self.kids[i - 1]

        def Select(self, how):
            self.selected = True
            dte.Commands = FakeProjects([Cmd("PLC.LogoutfromPlc"), Cmd("File.SaveAll")])
    proj = Node("Plc Project")
    root = Node("root", [Node("Solution 'Machine'", [Node("Machine", [Node("PLC", [Node("Plc", [proj, Node("Plc Instance")])])])])])
    orig_item = dte.Item

    def item(guid):
        if guid == X.VS_WINDOW_SOLUTION_EXPLORER:
            return type("W", (), {"Object": root})()
        return orig_item(guid)
    monkeypatch.setattr(dte, "Item", item)
    monkeypatch.setattr(be, "_dte2_late", lambda: (_ for _ in ()).throw(AttributeError("no ToolWindows")))
    r = be.plc_action("logout")
    assert proj.selected and r["command"] == "PLC.LogoutfromPlc" and r["selected_node"].endswith("Plc\\Plc Project"), r


def test_xae_status_without_a_solution_says_so(xae):
    """0.8.3: xae_status with no solution open returned the raw sysman_error."""
    be, dte, sm = xae
    be.attach()
    dte.Solution.FullName = ""                     # the shell's empty start state: IsOpen may still say True
    info = be.status()
    assert info["solution_open"] is False and info["note"].startswith("no solution open") and "sysman_error" not in info


def test_r15_plcopen_replace_that_duplicates_is_an_error(xae, tmp_path):
    """R15: import(option='replace') of PRG_TcUnitRunner (living in 95_UnitTests) created PRG_TcUnitRunner_1 at the root."""
    be, dte, sm = xae
    proj = sm.items["TIPC^Plc^Plc Project"]
    f = tmp_path / "r.xml"
    f.write_text('<project><types><pous><pou name="PRG_TcUnitRunner" pouType="program"/></pous></types></project>')

    def dup():
        it = FakeItem("PRG_TcUnitRunner_1", "TIPC^Plc^Plc Project^PRG_TcUnitRunner_1")
        proj._children.append(it)
        sm.items[it.PathName] = it
    proj.on_import = dup
    # no original to replace: nothing written, the copy (the import) is kept and named
    with pytest.raises(X.XaeError, match=r"not replaced: .*0 existing objects named PRG_TcUnitRunner.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert proj.deleted == []
    proj.on_import = None
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert r["objects_in_file"] == ["PRG_TcUnitRunner"] and "duplicates_created" not in r


def _r15_setup(sm, tmp_path, file_names=("PRG_TcUnitRunner",), xml_body=None):
    proj = sm.items["TIPC^Plc^Plc Project"]
    meth = FakeItem("Run", "TIPC^Plc^Plc Project^95_UnitTests^PRG_TcUnitRunner^Run")
    orig = FakeItem("PRG_TcUnitRunner", "TIPC^Plc^Plc Project^95_UnitTests^PRG_TcUnitRunner", [meth])
    orig.DeclarationText = "PROGRAM PRG_TcUnitRunner\nVAR\nEND_VAR"
    folder = FakeItem("95_UnitTests", "TIPC^Plc^Plc Project^95_UnitTests", [orig])
    proj._children.append(folder)
    for it in (folder, orig, meth):
        sm.items[it.PathName] = it
    f = tmp_path / "r.xml"
    f.write_text(xml_body or ("<project><types><pous>" + "".join(f'<pou name="{n}" pouType="program"/>' for n in file_names)
                              + "</pous></types></project>"))

    def dup(name="PRG_TcUnitRunner_1", decl=None, methods=("Run",)):
        """What the real shell does: the copy at the root, its header renamed to the copy's name (N38)."""
        kids = []
        for m in methods:
            k = FakeItem(m, f"TIPC^Plc^Plc Project^{name}^{m}")
            k.ImplementationText = f"// new {m}"
            kids.append(k)
        it = FakeItem(name, f"TIPC^Plc^Plc Project^{name}", kids)
        it.DeclarationText = decl if decl is not None else (
            "{attribute 'qualified_only'}\n// runner (* of the tests *)\nPROGRAM " + name + "\r\nVAR\r\n  n : INT;\r\nEND_VAR\r\n")
        it.ImplementationText = "// new body"
        proj._children.append(it)
        sm.items[it.PathName] = it
        for k in kids:
            sm.items[k.PathName] = k
        return it
    return proj, orig, meth, f, dup


def test_r15_n38_replace_renames_the_header_back_and_checks_the_declared_name(xae, tmp_path):
    """N38 (0.8.9 retest): the copy's header reads PROGRAM PRG_TcUnitRunner_1 (TwinCAT renames it); 0.8.9 wrote it
    into the original unchanged, compared the read-back with the same text, deleted the copy → the original declared
    PROGRAM PRG_TcUnitRunner_1 and the build failed. A1: header renamed back, every other byte kept."""
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    proj.on_import = dup
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert orig.DeclarationText == ("{attribute 'qualified_only'}\n// runner (* of the tests *)\nPROGRAM PRG_TcUnitRunner"
                                    "\r\nVAR\r\n  n : INT;\r\nEND_VAR\r\n")
    assert orig.ImplementationText == "// new body" and meth.ImplementationText == "// new Run"
    assert r["header_renamed"] == [{"object": orig.PathName, "from": "PRG_TcUnitRunner_1", "to": "PRG_TcUnitRunner"}]
    assert r["replaced"] == [{"object": orig.PathName, "from_copy": "TIPC^Plc^Plc Project^PRG_TcUnitRunner_1",
                              "checked_against": "name"}]
    assert r["build_required"] is True and r["objects_to_build"] == [orig.PathName]
    assert proj.deleted == ["PRG_TcUnitRunner_1"]


def test_r15_n38_header_shapes():
    """A2–A4: FUNCTION_BLOCK with modifiers / EXTENDS / IMPLEMENTS, FUNCTION with a return type, DUT, interface."""
    rh = X._rename_header
    assert rh("FUNCTION_BLOCK PUBLIC FB_Axis_1 EXTENDS FB_Base IMPLEMENTS I_Axis\nVAR END_VAR", "FB_Axis_1", "FB_Axis") == \
        "FUNCTION_BLOCK PUBLIC FB_Axis EXTENDS FB_Base IMPLEMENTS I_Axis\nVAR END_VAR"
    assert rh("FUNCTION F_Calc_2 : LREAL", "F_Calc_2", "F_Calc") == "FUNCTION F_Calc : LREAL"
    assert rh("TYPE ST_Data_1 :\nSTRUCT\n  x : INT;\nEND_STRUCT\nEND_TYPE", "ST_Data_1", "ST_Data").startswith("TYPE ST_Data :\n")
    assert rh("INTERFACE I_Axis_1 EXTENDS I_Base", "I_Axis_1", "I_Axis") == "INTERFACE I_Axis EXTENDS I_Base"
    assert rh("(* c *) {attribute 'x'}\r\nprogram prg_x_3\r\n", "PRG_X_3", "PRG_X") == "(* c *) {attribute 'x'}\r\nprogram PRG_X\r\n"
    assert rh("PROGRAM PRG_Other", "PRG_X_1", "PRG_X") is None                 # does not name the copy
    assert X._header_name("VAR_GLOBAL\n x : INT;\nEND_VAR") is None               # a GVL has no header name


def test_r15_n38_unrecognised_header_or_stray_copy_name_writes_nothing(xae, tmp_path):
    """A5: a header that does not name the copy → nothing written, copy kept, error names the object. A copy name
    elsewhere (e.g. a method naming the type) that the XML does not hold → nothing written either."""
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    before = (orig.DeclarationText, orig.ImplementationText, meth.ImplementationText)
    proj.on_import = lambda: dup(decl="PROGRAM PRG_TcUnitRunner // header not renamed — an unknown shape")
    with pytest.raises(X.XaeError, match=r"PRG_TcUnitRunner_1: the copy's declaration header names 'PRG_TcUnitRunner'.*"
                                         r"header not recognised, nothing written.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert (orig.DeclarationText, orig.ImplementationText, meth.ImplementationText) == before and proj.deleted == []
    proj._children = [c for c in proj._children if c.Name != "PRG_TcUnitRunner_1"]

    def dup_ref():
        it = dup()
        it.ImplementationText = "PRG_TcUnitRunner_1.n := 1;"
    proj.on_import = dup_ref
    with pytest.raises(X.XaeError, match=r"also occurs in the object \(implementation\).*nothing written"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert (orig.DeclarationText, orig.ImplementationText, meth.ImplementationText) == before and proj.deleted == []


class _StuckDecl(FakeItem):
    """An original whose DeclarationText does not take a write (the shell keeps the old text)."""
    def __setattr__(self, k, v):
        if k == "DeclarationText" and getattr(self, "_ready", False):
            return
        object.__setattr__(self, k, v)


def test_r15_n38_name_check_after_writing_restores_and_keeps_the_copy(xae, tmp_path):
    """A6: the declared name after writing is not the object's tree name → restored, copy kept, error."""
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    stuck = _StuckDecl("PRG_TcUnitRunner", orig.PathName, [meth])
    stuck.DeclarationText = "PROGRAM PRG_TcUnitRunner_9\nVAR\nEND_VAR"            # what the shell keeps returning
    object.__setattr__(stuck, "_ready", True)
    folder = sm.items["TIPC^Plc^Plc Project^95_UnitTests"]
    folder._children = [stuck]
    sm.items[stuck.PathName] = stuck
    proj.on_import = dup
    with pytest.raises(X.XaeError, match=r"read back differs at .*declaration.*restored to its earlier text.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert stuck.ImplementationText == "// impl of PRG_TcUnitRunner" and proj.deleted == []


def test_r15_n38_the_imported_xml_is_the_oracle(xae, tmp_path):
    """TwinCAT's export carries every declaration as plain text and the bodies: the intended text is checked against
    the user's XML, not against the copy TwinCAT made of it."""
    be, dte, sm = xae
    xml = """<?xml version="1.0" encoding="utf-8"?>
<project xmlns="http://www.plcopen.org/xml/tc6_0200"><types><pous>
<pou name="PRG_TcUnitRunner" pouType="program"><interface><addData>
<data name="http://www.3s-software.com/plcopenxml/interfaceasplaintext" handleUnknown="implementation">
<InterfaceAsPlainText><xhtml xmlns="http://www.w3.org/1999/xhtml">{attribute 'qualified_only'}
// runner (* of the tests *)
PROGRAM PRG_TcUnitRunner
VAR
  n : INT;
END_VAR
</xhtml></InterfaceAsPlainText></data></addData></interface>
<body><ST><xhtml xmlns="http://www.w3.org/1999/xhtml">// new body</xhtml></ST></body>
<addData><data name="http://www.3s-software.com/plcopenxml/method" handleUnknown="implementation">
<Method name="Run"><body><ST><xhtml xmlns="http://www.w3.org/1999/xhtml">// new Run</xhtml></ST></body></Method>
</data></addData></pou></pous></types></project>"""
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path, xml_body=xml)
    proj.on_import = dup
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert r["replaced"][0]["checked_against"] == "xml" and proj.deleted == ["PRG_TcUnitRunner_1"]
    # the copy differs from the XML somewhere else than the header (an unknown transformation): nothing written
    proj._children = [c for c in proj._children if c.Name != "PRG_TcUnitRunner_1"]
    orig.ImplementationText = "// old body"

    def dup_changed():
        it = dup()
        it._children[0].ImplementationText = "// Run as TwinCAT changed it"
    proj.on_import = dup_changed
    with pytest.raises(X.XaeError, match=r"differs from the imported XML at .*\^Run \(implementation\) line 1: "
                                         r"'// Run as TwinCAT changed it' vs XML '// new Run'.*nothing written.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert orig.ImplementationText == "// old body" and proj.deleted == ["PRG_TcUnitRunner_1"]


def test_r15_n38_graphical_bodies_and_property_accessors_in_the_oracle(xae, tmp_path):
    """Review A: (1) a CFC / FBD / LD / SFC body is not in ImplementationText — only the declaration was moved over,
    the copy holding the imported graphics was deleted and the object reported 'checked against xml'. Refused now,
    the copy kept. (2) Get / Set are children of <Property> in TwinCAT's export; the oracle looked only under the
    property's addData, so accessor bodies were never compared."""
    be, dte, sm = xae
    ns = 'xmlns="http://www.w3.org/1999/xhtml"'
    plain = ('<interface><addData><data name="http://www.3s-software.com/plcopenxml/interfaceasplaintext" '
             'handleUnknown="implementation"><InterfaceAsPlainText><xhtml ' + ns + '>{decl}</xhtml></InterfaceAsPlainText>'
             '</data></addData></interface>')
    prop = ('<addData><data name="http://www.3s-software.com/plcopenxml/property" handleUnknown="implementation">'
            '<Property name="P">' + plain.format(decl="PROPERTY P : INT") +
            '<GetAccessor><body><ST><xhtml ' + ns + '>P := 7;</xhtml></ST></body></GetAccessor>'
            '<SetAccessor><body><ST><xhtml ' + ns + '>;</xhtml></ST></body></SetAccessor></Property></data></addData>')
    xml = ('<?xml version="1.0" encoding="utf-8"?><project xmlns="http://www.plcopen.org/xml/tc6_0200"><types><pous>'
           '<pou name="FB_A" pouType="functionBlock">' + plain.format(decl="FUNCTION_BLOCK FB_A") +
           '<body><ST><xhtml ' + ns + '>;</xhtml></ST></body>' + prop + '</pou>'
           '<pou name="PRG_Cfc" pouType="program">' + plain.format(decl="PROGRAM PRG_Cfc") +
           '<body><addData><data name="http://www.3s-software.com/plcopenxml/cfc" handleUnknown="implementation">'
           '<CFC /></data></addData></body></pou>'
           '<pou name="PRG_Fbd" pouType="program"><body><FBD /></body></pou>'
           '</pous></types></project>')
    f = tmp_path / "o.xml"
    f.write_text(xml, encoding="utf-8")
    o = X._plcopen_oracle(str(f))
    assert o["fb_a"]["kids"]["p"]["kids"]["get"]["impl"] == "P := 7;" and "set" in o["fb_a"]["kids"]["p"]["kids"]
    assert o["prg_cfc"]["body"] == "CFC" and o["prg_fbd"]["body"] == "FBD" and o["fb_a"]["body"] == "ST"
    assert X._non_text_body(o["prg_cfc"], "X") == "X (CFC)" and X._non_text_body(o["fb_a"], "X") is None
    # an accessor that differs from the XML is caught
    want = {"decl": "FUNCTION_BLOCK FB_A", "impl": ";", "kids": {"P": {"decl": "PROPERTY P : INT", "impl": None,
            "kids": {"Get": {"decl": None, "impl": "P := 8;", "kids": {}}}}}}
    assert "P^Get (implementation)" in (X._oracle_mismatch(want, o["fb_a"], "FB_A") or "")
    # a replace of a graphical POU: refused, the original untouched, the copy kept
    proj, orig, meth, _f, dup = _r15_setup(sm, tmp_path, xml_body=xml.replace("PRG_Cfc", "PRG_TcUnitRunner"))
    orig.ImplementationText = "// old"
    proj.on_import = lambda: dup(methods=())
    orig._children = []
    with pytest.raises(X.XaeError, match=r"\(CFC\): a body that is not ST text cannot be moved.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(_f), "replace")
    assert orig.ImplementationText == "// old" and orig.DeclarationText == "PROGRAM PRG_TcUnitRunner\nVAR\nEND_VAR"
    assert proj.deleted == []


class _FailingText(FakeItem):
    """A method item whose ImplementationText cannot be written (fails n times, then works)."""
    def __init__(self, base, fails):
        super().__init__(base.Name, base.PathName)
        object.__setattr__(self, "_impl", base.ImplementationText)
        object.__setattr__(self, "_fails", fails)

    def __setattr__(self, k, v):
        if k == "ImplementationText" and getattr(self, "_fails", 0):
            object.__setattr__(self, "_fails", self._fails - 1)
            raise ComError(-2147467259, "write refused")
        object.__setattr__(self, "_impl" if k == "ImplementationText" else k, v)

    def __getattribute__(self, k):
        if k == "ImplementationText":
            return object.__getattribute__(self, "_impl")
        return object.__getattribute__(self, k)


def test_r15_review_a_failed_write_restores_the_original_and_a_failed_restore_keeps_the_copy(xae, tmp_path):
    """Review of the 0.8.9 fix: a write that fails half-way left the original half-updated and deleted the copy."""
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    bad = _FailingText(meth, fails=1)                     # the method write fails once: restore works
    orig._children = [bad]
    sm.items[bad.PathName] = bad
    proj.on_import = dup
    with pytest.raises(X.XaeError, match=r"writing failed .*was restored to its earlier text.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert orig.DeclarationText == "PROGRAM PRG_TcUnitRunner\nVAR\nEND_VAR" and orig.ImplementationText == "// impl of PRG_TcUnitRunner"
    assert bad.ImplementationText == "// impl of Run" and proj.deleted == []
    # the write and the restore both fail: the copy is kept and the original is named as possibly half-written
    proj._children = [c for c in proj._children if c.Name != "PRG_TcUnitRunner_1"]
    object.__setattr__(bad, "_fails", 5)
    with pytest.raises(X.XaeError, match=r"restoring .*PRG_TcUnitRunner failed too.*half-written.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert proj.deleted == []


def test_r15_review_names_in_the_file_and_an_unreadable_tree(xae, tmp_path):
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path, ("PRG_TcUnitRunner", "PRG_TcUnitRunner_2"))
    # A8: PRG_TcUnitRunner_2 is in the file: imported on purpose, never treated as a copy of PRG_TcUnitRunner
    proj.on_import = lambda: dup("PRG_TcUnitRunner_2")
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert "duplicates_created" not in r and proj.deleted == [] and orig.ImplementationText == "// impl of PRG_TcUnitRunner"
    # the tree cannot be listed completely: a 'replace' import is refused before anything happens
    real = be.tree_children
    be.tree_children = lambda *a, **k: {**real(*a, **k), "unreadable_children": [{"parent": "x", "index": 1}]}
    n = len(proj.imported)
    with pytest.raises(X.XaeError, match="could not be listed completely.*nothing was imported"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert len(proj.imported) == n
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "none")               # other options: imported, warned
    assert "not known" in r["warning"] and "added" not in r


def test_r15_n44_listing_fails_after_a_replace_import(xae, tmp_path):
    """N44: the listing after the import failed → 0.8.9 returned a normal result with a warning (copies stay,
    nothing replaced, reads as success). Now an error that says the import ran and what to look for."""
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    real = be.tree_children
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        res = real(*a, **k)
        return res if calls["n"] == 1 else {**res, "unreadable_children": [{"parent": "x", "index": 2}]}
    be.tree_children = flaky
    proj.on_import = dup
    with pytest.raises(X.XaeError, match=r"PlcOpenImport\(replace\) ran, but .*could not be listed afterwards.*"
                                         r"nothing was replaced or deleted.*PRG_TcUnitRunner_1"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert proj.deleted == [] and orig.ImplementationText == "// impl of PRG_TcUnitRunner"


class _BusyFolder(FakeItem):
    """A folder whose ChildCount the shell rejects (RPC_E_CALL_REJECTED) while `busy` is set."""
    busy = True

    @property
    def ChildCount(self):
        if self.busy:
            raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
        return len(self._children)


def test_n44_a_listing_that_fails_before_a_replace_import_imports_nothing(xae, tmp_path):
    """N44 (0.8.14 retest, S4): a listing failure BEFORE the import must not end in an import. 'replace' already
    refused an unreadable or raising listing, but tree_children read a rejected ChildCount (XAE busy, right after an
    import or a build) as 'no children': the folder looked empty, the snapshot complete, the import ran — and with
    the original missing from the snapshot its copy (PRG_TcUnitRunner_1) was left at the project root. A shell that
    stays busy now fails the listing, and the replace is refused before anything is imported."""
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    folder = sm.items["TIPC^Plc^Plc Project^95_UnitTests"]
    busy = _BusyFolder(folder.Name, folder.PathName, [orig])
    proj._children = [busy if c is folder else c for c in proj._children]
    sm.items[busy.PathName] = busy
    proj.on_import = dup
    with pytest.raises(X.XaeError, match=r"could not be listed completely.*nothing was imported"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert proj.imported == [] and not any(c.Name == "PRG_TcUnitRunner_1" for c in proj._children)
    # the shell answers again: the same import replaces in place
    busy.busy = False
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert r["copies_removed"] == ["TIPC^Plc^Plc Project^PRG_TcUnitRunner_1"] and len(proj.imported) == 1, r


def _itf_xml(methods: dict[str, str]) -> str:
    """An interface as TwinCAT exports it to PLCopen XML: <Interface> under the project's addData, each method with
    its declaration as plain text — and no plain text of the INTERFACE header itself."""
    ns = 'xmlns="http://www.w3.org/1999/xhtml"'
    meth = "".join(
        f'<Method name="{n}" ObjectId="{{0000000{i}-0000-0000-0000-000000000000}}"><interface><returnType><BOOL />'
        '</returnType><addData><data name="http://www.3s-software.com/plcopenxml/interfaceasplaintext" '
        f'handleUnknown="implementation"><InterfaceAsPlainText><xhtml {ns}>{d}</xhtml></InterfaceAsPlainText></data>'
        '</addData></interface><addData /></Method>' for i, (n, d) in enumerate(methods.items()))
    return ('<?xml version="1.0" encoding="utf-8"?><project xmlns="http://www.plcopen.org/xml/tc6_0200">'
            '<types><dataTypes /><pous /></types><instances><configurations /></instances><addData>'
            '<data name="http://www.3s-software.com/plcopenxml/interface" handleUnknown="implementation">'
            '<Interface name="I_Module" Id="{11111111-0000-0000-0000-000000000000}"><addData>'
            '<data name="http://www.3s-software.com/plcopenxml/method" handleUnknown="implementation">' + meth +
            '</data></addData></Interface></data></addData></project>')


def test_n175_an_interface_is_checked_against_the_xml_like_a_function_block(xae, tmp_path):
    """N175 (0.8.14 retest, S4): xae_plcopen_import(replace) reported checked_against 'name' for I_Module and 'xml' for
    the FB and the struct: the oracle kept only objects that carry a text of their own, and TwinCAT's export carries
    an interface's methods with their declarations but no plain text of the INTERFACE header — so the interface was
    left out and its method texts never compared. Now its methods are compared with the XML (a difference refuses
    the replace), and what the XML has no text for — the header — is named in not_in_xml (checked by name only)."""
    be, dte, sm = xae
    proj = sm.items["TIPC^Plc^Plc Project"]
    decl = {"M_Init": "METHOD M_Init : BOOL\nVAR_INPUT\n\tbReset : BOOL;\nEND_VAR\n", "M_Cyclic": "METHOD M_Cyclic : BOOL\n"}
    base = "TIPC^Plc^Plc Project^10_Interfaces^I_Module"
    kids = []
    for n in decl:
        k = FakeItem(n, f"{base}^{n}")
        k.DeclarationText, k.ImplementationText = f"METHOD {n} : BOOL // old\n", None
        kids.append(k)
    orig = FakeItem("I_Module", base, kids)
    orig.DeclarationText, orig.ImplementationText = "INTERFACE I_Module EXTENDS __SYSTEM.IQueryInterface\n", None
    folder = FakeItem("10_Interfaces", "TIPC^Plc^Plc Project^10_Interfaces", [orig])
    proj._children.append(folder)
    for it in [folder, orig, *kids]:
        sm.items[it.PathName] = it
    f = tmp_path / "itf.xml"
    f.write_text(_itf_xml(decl), encoding="utf-8")

    def dup(changed=None):
        cbase = "TIPC^Plc^Plc Project^I_Module_1"
        ck = []
        for n, d in decl.items():
            k = FakeItem(n, f"{cbase}^{n}")
            k.DeclarationText, k.ImplementationText = (changed or {}).get(n, d), None
            ck.append(k)
        c = FakeItem("I_Module_1", cbase, ck)
        c.DeclarationText, c.ImplementationText = "INTERFACE I_Module_1 EXTENDS __SYSTEM.IQueryInterface\n", None
        proj._children.append(c)
        for it in [c, *ck]:
            sm.items[it.PathName] = it
    # a method that differs from the XML (an unknown transformation by TwinCAT): nothing written, the copy kept
    proj.on_import = lambda: dup({"M_Init": "METHOD M_Init : INT\n"})
    with pytest.raises(X.XaeError, match=r"differs from the imported XML at .*I_Module\^M_Init \(declaration\).*"
                                         r"nothing written.*copies kept"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert kids[0].DeclarationText == "METHOD M_Init : BOOL // old\n" and proj.deleted == []
    proj._children = [c for c in proj._children if c.Name != "I_Module_1"]
    # the copy as the XML has it: checked against the XML; the header, which the XML has no text for, is named
    proj.on_import = dup
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    rec = r["replaced"][0]
    assert rec["checked_against"] == "xml" and rec["not_in_xml"] == [f"{base} (declaration)"], rec
    assert "no plain text" in rec["not_in_xml_note"] and "name" in rec["not_in_xml_note"], rec
    assert orig.DeclarationText == "INTERFACE I_Module EXTENDS __SYSTEM.IQueryInterface\n"
    assert kids[0].DeclarationText == decl["M_Init"] and proj.deleted == ["I_Module_1"]


def test_n10_n45_online_commands_and_check_all_objects_via_the_automation_interface(xae):
    """#10: login/logout needed an active PLC project (UI selection never took); #45 Check all objects never ran.
    Both now go through the documented Automation Interface first: ConsumeXml online commands and
    ITcPlcIECProject2.CheckAllObjects on the nested project."""
    be, dte, sm = xae
    proj = sm.items["TIPC^Plc^Plc Project"]
    checked = []
    proj.CheckAllObjects = lambda: checked.append(1) or 0
    r = be.plc_action("logout")
    assert r["method"] == "ConsumeXml LogoutCmd" and r["project"] == "TIPC^Plc^Plc Project"
    assert "<LogoutCmd>true</LogoutCmd>" in proj.consumed[-1] and dte.commands == []
    r = be.check_all_objects()
    assert r["method"] == "ITcPlcIECProject2.CheckAllObjects" and checked == [1]

    def refuse(xml):
        raise ComError(-2147467259, "online command refused")
    proj.ConsumeXml = refuse
    r = be.plc_action("login")                                  # falls back to the shell command, says why
    assert r["command"] == "PLC.Login" and "online command refused" in r["automation_interface"]


def test_n41_xae_plc_acts_on_the_named_project_on_every_route(xae, monkeypatch):
    """N41: when ConsumeXml failed (or for download, which has no ConsumeXml command), the DTE fallback acted on the
    shell's ACTIVE project, ignoring plc_project_path; with two projects and none named it used whichever was active.
    Now the project is resolved once and every route acts on exactly that one."""
    be, dte, sm = xae
    a, b = "TIPC^PlcA^PlcA Project", "TIPC^PlcB^PlcB Project"
    monkeypatch.setattr(be, "_nested_projects", lambda p=None: [X._plc_split(p)[1]] if p else [a, b])
    shell = {"active": "PlcA", "selected": []}

    def cmds(prefix, limit=500):
        return {"commands": [f"PLC.Loginto{shell['active']}", f"PLC.Logoutfrom{shell['active']}", "PLC.Download"]}
    monkeypatch.setattr(be, "list_commands", cmds)

    def select(name=None):
        shell["selected"].append(name)
        if shell.get("selectable", True):
            shell["active"] = name
            return f"Solution\\{name} Project"
        return None
    monkeypatch.setattr(be, "_select_plc_project_node", select)
    issued = []
    monkeypatch.setattr(be, "command", lambda c, *a, **k: issued.append(c) or {"command": c})
    monkeypatch.setattr(be, "_item", lambda path: (_ for _ in ()).throw(X.XaeError(f"ConsumeXml not here: {path}")))
    # two projects, none named: nothing is guessed, nothing issued
    with pytest.raises(X.XaeError, match=r"2 PLC projects in the solution — name one.*nothing was done"):
        be.plc_action("login")
    assert issued == []
    # B named while A is active: B's node is selected first, then B's command — never A's
    r = be.plc_action("login", b)
    assert issued == ["PLC.LogintoPlcB"] and shell["selected"] == ["PlcB"] and r["project"] == b
    # B named, B cannot be made active: refused, A's command is not used
    shell.update(active="PlcA", selectable=False)
    issued.clear()
    with pytest.raises(X.XaeError, match=r"act on the active project \['plca'\], not 'PlcB'.*Nothing was done"):
        be.plc_action("logout", b)
    assert issued == []
    # download (no ConsumeXml command) on a shell whose generic PLC.Download does not name the project: refused
    # with two projects in the solution
    with pytest.raises(X.XaeError, match=r"do not name the project they act on and the solution has 2 PLC projects"):
        be.plc_action("download", b)
    assert issued == []
    # review A: the listing fails (XAE busy) — an unknown list is not 'one project': the generic PLC.Download (which
    # acts on the active PlcA) is never issued for B
    def busy(p=None):
        raise X.XaeError("XAE busy")
    monkeypatch.setattr(be, "_nested_projects", busy)
    with pytest.raises(X.XaeError, match=r"PLC projects could not be listed \(XAE busy\).*Nothing was done"):
        be.plc_action("download", b)
    assert issued == []
    monkeypatch.setattr(be, "_nested_projects", lambda p=None: [])
    with pytest.raises(X.XaeError, match=r"Nothing was done"):
        be.plc_action("download", b)
    assert issued == []


def test_n10_retest_outer_and_nested_node_are_one_plc_project(xae):
    """#10 (0.8.8 retest): the outer PLC node (ItemType PLCAPP on the real tree) and its nested '<Name> Project' were
    both listed → 'TIPC^Untitled1^Untitled1 Project' ×2 → 'ambiguous' → the DTE fallback and > 120 s."""
    be, dte, sm = xae
    real_children = be.tree_children

    def with_outer(path, depth=1, **kw):
        res = real_children(path, depth, **kw)
        if path == "TIPC":
            res["items"] = [{"Name": "Plc", "PathName": "TIPC^Plc", "ItemType": X.TREEITEMTYPE_PLCAPP}] + res["items"]
        return res
    be.tree_children = with_outer
    pj = be.plc_projects()
    assert pj["count"] == 1 and pj["projects"][0]["iec_project_path"] == "TIPC^Plc^Plc Project"
    assert pj["projects"][0]["plc_project_path"] == "TIPC^Plc^Plc Project"           # the nested node's fields kept
    r = be.plc_action("login")
    assert r["method"] == "ConsumeXml LoginCmd" and dte.commands == []               # no DTE fallback, no wait


def test_te1000_itcplconline_is_the_first_route_and_its_state_is_read_back(xae):
    """TE1000 ITcPlcOnline: Login(PLC_LOGIN_FLAGS) / Logoff / Start / Stop / Reset(PLC_RESET_TYPE) on the nested
    project — documented, acts on this project, no shell command; IsLoggedIn / OnlineApplicationState read back."""
    be, dte, sm = xae
    proj = sm.items["TIPC^Plc^Plc Project"]
    calls = []
    proj.IsLoggedIn, proj.OnlineApplicationState = False, 2

    def login(flags):
        calls.append(("Login", flags))
        proj.IsLoggedIn = True
    proj.Login = login
    proj.Logoff = lambda: calls.append(("Logoff",)) or setattr(proj, "IsLoggedIn", False)
    proj.Start = lambda: calls.append(("Start",)) or setattr(proj, "OnlineApplicationState", 1)
    proj.Stop = lambda: calls.append(("Stop",))                 # returns, but the application keeps running
    proj.Reset = lambda t: calls.append(("Reset", t))
    be.ONLINE_STATE_WAIT_S = 0.6
    # 0.8.12 retest: Stop while logged out returned and did nothing — refused first, through no route at all
    consumed0 = []
    proj.ConsumeXml = lambda xml: consumed0.append(xml)
    for act in ("stop", "start", "reset_cold"):
        with pytest.raises(X.XaeError, match=rf"not logged in — {act} acts on the project's online session: xae_plc "
                                             "login first.*Nothing was done"):
            be.plc_action(act)
    assert calls == [] and consumed0 == [] and dte.commands == []
    del proj.ConsumeXml
    r = be.plc_action("download")
    assert r["method"] == "ITcPlcOnline" and calls[-1] == ("Login", 4) and r["after"]["logged_in"] is True
    assert be.plc_action("start")["after"]["application_state"] == "run"
    be.plc_action("reset_origin")
    assert calls[-1] == ("Reset", 2)
    # review: a command that was accepted but whose state never came is NOT sent again through ConsumeXml / the shell
    # (a second reset or download would act twice) — it is reported as possibly done
    consumed = []
    proj.ConsumeXml = lambda xml: consumed.append(xml)
    n_cmd = len(dte.commands)
    with pytest.raises(X.XaeError, match=r"ITcPlcOnline.stop on .* returned, but 0.6 s later .* application run"
                                         r".*not sent again"):
        be.plc_action("stop")
    assert calls[-1] == ("Stop",) and consumed == [] and len(dte.commands) == n_cmd
    # a state that follows the call a moment later is awaited, not taken for a failure
    import threading
    proj.Stop = lambda: calls.append(("Stop",)) or threading.Timer(0.2, setattr, (proj, "OnlineApplicationState", 2)).start()
    assert be.plc_action("stop")["after"]["application_state"] == "stop"
    assert be.plc_action("logout")["after"]["logged_in"] is False


def test_n82_check_all_objects_reads_only_its_own_messages_and_its_result(xae):
    """N82: the items came from the whole Output pane (2000 lines) and an Error List never cleared: a deleted
    property's warning stayed, a recurring warning grew by one per run. Now: Error List cleared, only the Build pane
    lines after a marker, CheckAllObjects' own result; a failed check (E_FAIL) is a result, not a reason to rerun."""
    be, dte, sm = xae
    proj = sm.items["TIPC^Plc^Plc Project"]
    old = "1>C:\\P\\Plc\\POUs\\FB_X.TcPOU(12,3): warning C0373: Property 'Position' defines neither a get nor a set accessor"
    dte._output = old
    proj.CheckAllObjects = lambda: (setattr(dte, "_output", old + "\nnew line: no issues") or True)
    r = be.check_all_objects()
    assert r["passed"] is True and not any("Position" in str(i) for i in r["items"]), r

    def fail():
        dte._output += "\n1>C:\\P\\Plc\\POUs\\FB_Y.TcPOU(3,1): error C0046: Identifier 'x' not defined"
        raise ComError(-2147467259, "Check all objects failed")
    proj.CheckAllObjects = fail
    dte.commands.clear()
    r = be.check_all_objects()
    assert r["passed"] is False and dte.commands == [] and [i["description"][:5] for i in r["items"]] == ["Ident"]
    # review: a check that CLEARS the pane and writes fewer (or more) lines than were there keeps all of its own
    many = "\n".join(f"line {i}" for i in range(40))
    dte._output = many
    proj.CheckAllObjects = lambda: setattr(
        dte, "_output", "1>C:\\P\\Plc\\POUs\\FB_Z.TcPOU(7,1): error C0046: Identifier 'z' not defined") or False
    r = be.check_all_objects()
    assert [i["description"][:16] for i in r["items"]] == ["Identifier 'z' n"], r


def test_n81_a_graphical_body_is_named_from_the_language_before_any_text(xae):
    be, dte, sm = xae
    tree = {"_it": type("I", (), {"Language": 1})(), "kids": {
        "Act": {"_it": type("I", (), {"Language": 5})(), "kids": {}}}}
    # (a method since N174: the Language read goes through the backend's _retry)
    assert be._non_st_language(tree, "POUs^FB_A") == "POUs^FB_A^Act (CFC)"
    assert be._non_st_language({"_it": type("I", (), {"Language": 0})(), "kids": {}}, "GVL") is None


def test_silent_mode_is_on_only_during_a_confirmed_command(xae):
    be, dte, sm = xae
    settings = type("S", (), {"SilentMode": False})()
    seen = []
    dte.GetObject = lambda name: settings if name == "TcAutomationSettings" else None
    be.cfg.silent_mode = True
    real = sm.ActivateConfiguration
    sm.ActivateConfiguration = lambda: seen.append(settings.SilentMode) or real()
    r = be.activate_configuration()
    assert seen == [True] and settings.SilentMode is False and r["silent_mode"] is True
    be.cfg.silent_mode = False
    seen.clear()
    be.activate_configuration()
    assert seen == [False]


def test_review_plcimportoptions_are_read_from_the_type_library_as_pywin32_gives_them():
    """Review: GetTypeAttr() / GetVarDesc() are tuples in pywin32 (genpy reads attr[7], vdesc[0], vdesc[1]); reading
    .cVars / .memid / .value raised AttributeError, which was taken for 'no type library' — the branch never ran."""
    names = {10: "PLCIMPORTOPTIONS_NONE", 11: "PLCIMPORTOPTIONS_REPLACE", 12: "PLCIMPORTOPTIONS_RENAME",
             13: "PLCIMPORTOPTIONS_SKIP"}
    vals = {10: 0, 11: 1, 12: 2, 13: 3}

    class TI:
        def GetTypeAttr(self):
            return (None, 0, -1, -1, 4, 0, 0, 4, 0, 0, 4, 0, 1, 0, None, None)      # tuple: cVars at [7]

        def GetVarDesc(self, j):
            m = 10 + j
            return (m, vals[m], None, 0, 2)                                         # (memid, value, elemdesc, flags, kind)

        def GetNames(self, memid):
            return (names[memid],)

    class TL:
        def GetTypeInfoCount(self):
            return 2

        def GetTypeInfoType(self, i):
            return 3 if i == 0 else 0                                               # an interface, then the enum

        def GetDocumentation(self, i):
            return ("ITcSmTreeItem", "", 0, None) if i == 0 else ("PLCIMPORTOPTIONS", "", 0, None)

        def GetTypeInfo(self, i):
            return TI()

    class Ole:
        def GetTypeInfo(self):
            return type("T", (), {"GetContainingTypeLib": lambda self: (TL(), 0)})()

    obj = type("Item", (), {"_oleobj_": Ole()})()
    assert X._typelib_enum(obj, "PLCIMPORTOPTIONS") == {"PLCIMPORTOPTIONS_NONE": 0, "PLCIMPORTOPTIONS_REPLACE": 1,
                                                        "PLCIMPORTOPTIONS_RENAME": 2, "PLCIMPORTOPTIONS_SKIP": 3}
    be = X.XaeBackend(XaeConfig())               # (N174: the type library is read through the backend's _retry)
    m, src = be._import_options(obj)
    assert m == {"none": 0, "replace": 1, "rename": 2, "skip": 3} and src.startswith("type library"), (m, src)


def test_n97_replace_is_done_in_place_and_a_removed_object_is_never_silent(xae, tmp_path):
    """N97 (0.8.11 retest): with PLCIMPORTOPTIONS_REPLACE (1, from the type library) TwinCAT deleted the foldered
    originals and re-created them at the project root with new GUIDs; the result said only 'added'. 'replace' now
    imports as RENAME (copies beside the originals) and moves the checked text into each original in its folder; an
    object that leaves its place is named, and stops a replace."""
    be, dte, sm = xae
    proj, orig, meth, f, dup = _r15_setup(sm, tmp_path)
    folder = sm.items["TIPC^Plc^Plc Project^95_UnitTests"]

    def by_value():
        v = proj.imported[-1][1]
        if v == X._DOC_IMPORT_OPTIONS["rename"]:
            dup()                                    # TwinCAT's rename: a copy PRG_TcUnitRunner_1 at the root
        elif v == X._DOC_IMPORT_OPTIONS["replace"]:  # TwinCAT's replace: the original leaves its folder
            folder._children.remove(orig)
            sm.items.pop(orig.PathName)
            sm.items.pop(meth.PathName)
            dup("PRG_TcUnitRunner")
    proj.on_import = by_value
    r = be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert proj.imported[-1][1] == X._DOC_IMPORT_OPTIONS["rename"] and "rename" in r["option_value"]["imported_as"]
    assert orig in folder._children and orig.DeclarationText.startswith("{attribute 'qualified_only'}")
    assert "PROGRAM PRG_TcUnitRunner\r\n" in orig.DeclarationText and r["replaced"][0]["object"] == orig.PathName
    assert proj.deleted == ["PRG_TcUnitRunner_1"] and "removed" not in r
    # if the shell still moves an object (whatever the option), a replace stops and says where it went
    proj.on_import = lambda: (folder._children.remove(orig), sm.items.pop(orig.PathName), sm.items.pop(meth.PathName),
                              dup("PRG_TcUnitRunner"))
    with pytest.raises(X.XaeError, match=r"removed 2 existing object\(s\) from their place.*re-created elsewhere"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")


def _two_shells(monkeypatch, cfg):
    """Two XAE shells: Project15.sln (pid 1) and DrumFillingMachine.sln (pid 2), as in the 0.8.12 retest."""
    be = X.XaeBackend(cfg)
    monkeypatch.setattr(be, "_require_win", lambda: None)
    monkeypatch.setattr(be, "_dte2", lambda: None)
    sm1, sm2 = FakeSysMan(), FakeSysMan()
    sm2.net = "5.166.17.133.1.1"
    d1, d2 = FakeDTE(sm1), FakeDTE(sm2)
    d1.Solution.FullName, d2.Solution.FullName = r"C:\P\Project15.sln", r"C:\D\DrumFillingMachine.sln"
    shells = [("!TcXaeShell.DTE.17.0:1", d1), ("!TcXaeShell.DTE.17.0:2", d2)]
    monkeypatch.setattr(be, "_rot_candidates", lambda: [
        {"progid": "TcXaeShell.DTE.17.0", "moniker": m, "dte": d, "solution": d.Solution.FullName} for m, d in shells])
    return be, d1, d2, shells


def test_n116_a_closed_solution_is_never_replaced_by_another(monkeypatch):
    """N116 (0.8.12 retest, S1): two shells; the configured Project15.sln was closed — the next xae_status answered
    for DrumFillingMachine.sln and another target, without a word, and stayed there after Project15 was reopened.
    Now: closed → every call refused, status says so; reopened (in the same or another shell) → found again."""
    be, d1, d2, shells = _two_shells(monkeypatch, XaeConfig(com_retry_s=0.3, solution_path=r"C:\P\Project15.sln"))
    assert be.attach()["target_net_id"] == "5.1.2.3.1.1" and be.dte is d1
    d1.Solution.FullName = ""                                       # File → Close Solution in shell 1
    st = be.status()
    assert st["solution_mismatch"] and st["bound_solution_open"] is False and be.dte is d1, st
    assert "Project15.sln" in st["refused"] and "DrumFillingMachine" in st["refused"]
    with pytest.raises(X.XaeError, match="is not open: the attached shell has no solution; the XAE shells have: .*Drum.* — nothing was done"):
        be.tree_item("TIPC")
    assert be.dte is d1                                             # never DrumFillingMachine
    d1.Solution.FullName = r"C:\P\Project15.sln"                    # reopened in the same shell
    assert be.tree_item("TIPC") and be.status().get("solution_mismatch") is None
    d1.Solution.FullName = ""                                       # closed, then opened in a third shell
    sm3 = FakeSysMan()
    sm3.net = "5.1.2.3.1.1"
    d3 = FakeDTE(sm3)
    d3.Solution.FullName = r"c:\p\PROJECT15.sln"                    # Windows: case does not matter
    shells.append(("!TcXaeShell.DTE.17.0:3", d3))
    st = be.status()
    assert be.dte is d3 and "re-attached" in st["reattached"] and st["target_net_id"] == "5.1.2.3.1.1", st


def test_n116_attach_never_falls_back_and_unconfigured_never_guesses(monkeypatch):
    be, d1, d2, shells = _two_shells(monkeypatch, XaeConfig(com_retry_s=0.3, solution_path=r"C:\P\Elsewhere.sln"))
    with pytest.raises(X.XaeError, match=r"Elsewhere.sln is not open in any XAE shell .*nothing was attached"):
        be.attach()
    assert be.dte is None
    # explicitly another solution: allowed, flagged, and from then on that one
    info = be.attach(solution_path=r"C:\D\DrumFillingMachine.sln")
    assert be.dte is d2 and info["solution_mismatch"] and "not the configured" in info["solution_note"]
    assert be.bound_solution == r"C:\D\DrumFillingMachine.sln"
    # nothing configured, two solutions open: nothing chosen
    be2, *_ = _two_shells(monkeypatch, XaeConfig(com_retry_s=0.3))
    with pytest.raises(X.XaeError, match="2 XAE shells with different solutions are open .*nothing was chosen"):
        be2.attach()
    be2.attach(solution_path=r"C:\P\Project15.sln")
    assert be2.bound_solution == r"C:\P\Project15.sln"


def test_n116_a_dead_proxy_reattaches_to_the_bound_solution_only(monkeypatch):
    be, d1, d2, shells = _two_shells(monkeypatch, XaeConfig(com_retry_s=0.3))
    be.attach(solution_path=r"C:\D\DrumFillingMachine.sln")
    shells[:] = [shells[0]]                                         # shell 2 exits: only Project15 is left
    be.dte = None                                                   # (what _retry does on RPC_E_DISCONNECTED)
    with pytest.raises(X.XaeError, match=r"DrumFillingMachine.sln is not open in any XAE shell"):
        be._need()


def test_n116_review_a_runtime_bound_solution_is_kept_by_status_and_attach(monkeypatch):
    """Review of N116: with no xae.solution_path, the solution bound at runtime (the only one open) was ignored by
    xae_status' own attach (and xae_attach() without a path) once the shell had gone: they attached to whatever was open
    then. The bound solution counts there too; an empty shell is never taken for the one that has the solution."""
    be, d1, d2, shells = _two_shells(monkeypatch, XaeConfig(com_retry_s=0.3))
    both = list(shells)
    shells[:] = [both[0]]                                           # only Project15 open: bound at runtime
    be.attach()
    assert be.bound_solution == r"C:\P\Project15.sln"
    shells[:] = [both[1]]                                           # Project15's shell gone, DrumFillingMachine open
    be.dte = None
    with pytest.raises(X.XaeError, match=r"Project15.sln is not open in any XAE shell .*nothing was attached"):
        be.attach()                                                 # what xae_status does when not attached
    assert be.dte is None and be.bound_solution == r"C:\P\Project15.sln"
    # an empty shell listed first is not taken for the one with the (only) solution
    be2, e1, e2, shells2 = _two_shells(monkeypatch, XaeConfig(com_retry_s=0.3))
    e1.Solution.FullName = ""
    be2.attach()
    assert be2.dte is e2 and be2.bound_solution == r"C:\D\DrumFillingMachine.sln"


def test_n116_second_review_status_keeps_an_explicit_solution_over_the_configured_one(monkeypatch):
    """Second review: xae_status attached without the bound solution's precedence — configured A, xae_attach(B), B's
    shell gone, A open: xae_status silently rebound to A. Its attach is internal now: B is refused, nothing rebound."""
    be, d1, d2, shells = _two_shells(monkeypatch, XaeConfig(com_retry_s=0.3, solution_path=r"C:\P\Project15.sln"))
    be.attach(solution_path=r"C:\D\DrumFillingMachine.sln")
    shells[:] = [shells[0]]                                         # DrumFillingMachine's shell gone, Project15 open
    be.dte = None
    with pytest.raises(X.XaeError, match=r"DrumFillingMachine.sln is not open in any XAE shell"):
        be.attach(_internal=True)                                   # what xae_status does now
    assert be.bound_solution == r"C:\D\DrumFillingMachine.sln"
    assert be.attach()["solution_bound"] == r"C:\P\Project15.sln"  # xae_attach() without a path: back to the config


class _DeadSysMan:
    """The ITcSysManager of a solution closed and reopened in the same shell: every call errors (E_POINTER), it
    never acts (0.8.13 retest N120: 'LookupTreeItem TIPC: com_error … E_POINTER')."""
    def __getattr__(self, name):
        def dead(*_a, **_k):
            e = ComError(X.DISP_E_EXCEPTION, "Invalid pointer")
            e.excepinfo = (0, "TcSysManager", "Invalid pointer", None, 0, X.E_POINTER)
            raise e
        return dead


def test_n120_the_first_call_after_a_same_shell_reopen_works(xae):
    """N120 (0.8.13 retest): close + reopen of the solution in the SAME shell — the first xae call failed, the second
    worked. _retry refreshed the System Manager, but the retried lambda called the object captured before (sm = …;
    _retry(lambda: sm.X())), so the retry hit the stale one again. Every retried call takes its object at call time."""
    be, dte, sm = xae
    be.attach()
    fresh = FakeSysMan()
    fresh.net = "5.1.2.3.1.1"

    def reopen():
        be.sysman = _DeadSysMan()                        # what the server still holds after the reopen …
        dte.Solution.Projects = FakeProjects([FakeProject("Other", None), FakeProject("Machine", fresh)])
    for call, check in (
            (lambda: be.target_get(), lambda r: r["target_net_id"] == "5.1.2.3.1.1"),
            (lambda: be.tree_xml("TIPC^Plc"), lambda r: r["xml"] == "<TreeItem/>"),
            (lambda: be.pou_get("TIPC^Plc^Plc Project^POUs^MAIN", include_subitems=False), lambda r: r["Name"] == "MAIN"),
            (lambda: be.target_set("5.1.2.3.1.1"), lambda r: fresh.net == "5.1.2.3.1.1"),
            (lambda: be.activate_configuration(), lambda r: fresh.activated == 1 and r["target"] == "5.1.2.3.1.1"),
            (lambda: be.restart_twincat(), lambda r: fresh.restarted == 1),
            (lambda: be.tree_delete_child("TIPC^Plc^Plc Project^POUs", "MAIN"),
             lambda r: fresh.items["TIPC^Plc^Plc Project^POUs"].deleted[-1] == "MAIN"),
            (lambda: be.status(), lambda r: r.get("target_net_id") == "5.1.2.3.1.1")):
        reopen()
        r = call()                                       # … the FIRST call works
        assert check(r), r
        assert be.sysman is fresh


def test_n116_every_result_says_when_another_solution_is_worked_on(monkeypatch):
    """N116 (0.8.13 retest, S3): after xae_attach(solution_path=<other>) only that call's result had solution_mismatch;
    xae_plc_projects / xae_status while bound to the other solution said nothing. mismatch_note() says it every time
    (the server adds it to each XAE result); the configured solution bound again → no note."""
    be = X.XaeBackend(XaeConfig(solution_path=r"C:\P\Machine.sln", com_retry_s=0.3))
    sm = FakeSysMan()
    dte = FakeDTE(sm)
    dte.Solution.FullName = r"C:\P\Other.sln"
    monkeypatch.setattr(be, "_require_win", lambda: None)
    monkeypatch.setattr(be, "_rot_candidates", lambda: [{"progid": "TcXaeShell.DTE.17.0", "moniker": "!TcXaeShell.DTE.17.0:1",
                                                         "dte": dte, "solution": dte.Solution.FullName}])
    monkeypatch.setattr(be, "_dte2", lambda: None)
    assert be.mismatch_note() is None                     # not attached
    be.attach(solution_path=r"C:\P\Other.sln")
    assert "C:\\P\\Other.sln" in be.mismatch_note() and "xae.solution_path" in be.mismatch_note()
    be.bound_solution = r"c:\p\machine.SLN"               # the configured one, any spelling
    assert be.mismatch_note() is None


def test_n116_a_reattach_after_the_shell_went_away_is_said(xae):
    """N116 (0.8.13 retest, S4): the shell exited and the solution was opened in a new one — the tools re-attached
    silently (reattach_note was only set when the bound solution was found in another running shell)."""
    be, dte, sm = xae
    be.attach()
    calls = {"n": 0}

    def dead_then_ok():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ComError(X.RPC_S_SERVER_UNAVAILABLE, "The RPC server is unavailable")
        return "ok"
    assert be._retry(dead_then_ok, "DTE.Version") == "ok"
    assert "went away" in be.reattach_note
    assert "went away" in be.status()["reattached"] and be.reattach_note is None


def test_review_0814_an_online_command_or_action_is_never_sent_twice(xae, monkeypatch):
    """Review of 0.8.14 (S1): after ITcPlcOnline.Login/Reset/Stop was sent, a busy shell during the state read-back
    raised a plain XaeError and plc_action went on to ConsumeXml and PLC.Download — the command acted twice. And a
    shell that went away DURING an action (activate, restart, delete …) had the action run again on the re-attached
    shell, since the retry now reaches the live one."""
    be, dte, sm = xae
    proj = sm.items["TIPC^Plc^Plc Project"]
    calls = []
    proj.IsLoggedIn, proj.OnlineApplicationState = False, 2
    proj.Login = lambda flags: calls.append(("Login", flags))
    consumed = []
    proj.ConsumeXml = lambda xml: consumed.append(xml)
    real = be._item
    sent = {"n": 0}

    def busy_after_login(path):
        if calls:
            raise X.XaeError("LookupTreeItem: the shell is busy")
        return real(path)
    monkeypatch.setattr(be, "_item", busy_after_login)
    with pytest.raises(X.XaeExplained, match=r"ITcPlcOnline.download on .* could not be read back .*may have acted.*not sent again"):
        be.plc_action("download")
    assert calls == [("Login", 4)] and consumed == [] and dte.commands == []
    monkeypatch.setattr(be, "_item", real)
    # an action during which the shell went away: re-attached, not repeated
    be.attach()

    def dies_during_activate():
        sent["n"] += 1
        raise ComError(X.RPC_E_DISCONNECTED)
    monkeypatch.setattr(sm, "ActivateConfiguration", dies_during_activate)
    with pytest.raises(X.XaeExplained, match=r"ActivateConfiguration: the XAE shell went away during the call.*"
                                             r"not repeated \(re-attached"):
        be.activate_configuration()
    assert sent["n"] == 1 and "went away during ActivateConfiguration" in be.reattach_note
    # a read is still retried on the re-attached shell
    n = {"k": 0}

    def dead_once():
        n["k"] += 1
        if n["k"] == 1:
            raise ComError(X.RPC_E_DISCONNECTED)
        return "ok"
    assert be._retry(dead_once, "GetTargetNetId") == "ok" and n["k"] == 2


def test_rereview_0814_a_stale_object_error_never_repeats_an_action(xae, monkeypatch):
    """Re-review of 0.8.14: the stale-System-Manager branch retried every call — E_POINTER is also the .NET
    NullReferenceException TwinCAT's managed code throws after acting, so a ConsumeXml / import ran twice. An action is
    not repeated on it; before an action a read refreshes a stale System Manager (a reopened solution, N120)."""
    be, dte, sm = xae
    be.attach()
    n = {"k": 0}

    def act():
        n["k"] += 1
        raise ComError(X.E_POINTER)
    with pytest.raises(X.XaeExplained, match="whether it acted is unknown, so it is not repeated"):
        be._retry(act, "ConsumeXml StartCmd", once=True)
    assert n["k"] == 1
    reads = {"k": 0}

    def read():
        reads["k"] += 1
        if reads["k"] == 1:
            raise ComError(X.E_POINTER)
        return "5.1.2.3.1.1"
    assert be._retry(read, "GetTargetNetId") == "5.1.2.3.1.1" and reads["k"] == 2      # a read still retries


# ---------------------------------------------------------------------------------------------------------------------
# N174 (0.8.14 retest, S3): xae_build and xae_deploy right after a PLCopen import failed with "internal error …
# com_error (-2147418111, 'Call was rejected by callee.') — a defect in twincat-mcp"; repeating them worked. A COM
# access outside _retry let the busy shell's rejection escape (and accesses inside a bare try/except read a busy shell
# as "no value"). Every public XAE method runs below against a shell that rejects each COM access — every property
# read, property write and method call, at each line of xae_backend.py — the first times it is made, and against one
# that rejects for ever.
# ---------------------------------------------------------------------------------------------------------------------
import sys as _sys
import types as _types


class _BusyShell:
    """Counts what a busy shell rejected: per (line of xae_backend.py, object type, member, get/set/call)."""

    def __init__(self, times):
        self.times = times                 # None: rejected for ever
        self.count: dict = {}
        self.rejected = 0

    @staticmethod
    def _site() -> int:
        f = _sys._getframe(3)
        while f is not None and not f.f_code.co_filename.replace("\\", "/").endswith("twincat_mcp/xae_backend.py"):
            f = f.f_back
        return f.f_lineno if f is not None else 0

    def hit(self, target, member, kind):
        key = (self._site(), type(target).__name__, member, kind)
        n = self.count.get(key, 0)
        if self.times is None or n < self.times:
            self.count[key] = n + 1
            self.rejected += 1
            raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")

    def wrap(self, v):
        if v is None or isinstance(v, (str, bytes, int, float, bool, list, tuple, dict, set)):
            return v
        return _Busy(v, self)


class _Busy:
    """A fake COM object served by a busy shell: what it returns is served the same way."""

    def __init__(self, target, shell):
        object.__setattr__(self, "_busy_t", target)
        object.__setattr__(self, "_busy_s", shell)

    def __getattr__(self, name):
        t, s = object.__getattribute__(self, "_busy_t"), object.__getattribute__(self, "_busy_s")
        if name.startswith("_"):
            return getattr(t, name)          # Python-side attributes (_oleobj_ …): no COM call
        s.hit(t, name, "get")
        v = getattr(t, name)
        if callable(v) and not isinstance(v, type):
            def call(*a, **k):
                s.hit(t, name, "call")
                return s.wrap(v(*a, **k))
            return call
        return s.wrap(v)

    def __setattr__(self, name, value):
        t, s = object.__getattribute__(self, "_busy_t"), object.__getattribute__(self, "_busy_s")
        s.hit(t, name, "set")
        setattr(t, name, value)


class _Clock:
    """xae_backend's `time` on a virtual clock: a retry window of 60 s passes without waiting. step: what one sleep
    advances it by (None: the time asked for) — a small step keeps a shell that rejects each access a few times from
    adding up to a minute of back-off over a call of many accesses (a busy shell rejects for a while, not per access)."""

    def __init__(self, step=None):
        import time as _t
        self.now = 1_000_000.0
        self.step = step
        self.strftime = _t.strftime

    def time(self):
        return self.now

    monotonic = time

    def sleep(self, s):
        self.now += self.step if self.step is not None else max(float(s), 0.001)


class _Ms:
    """The Scope configuration of a TwinCAT Measurement project (IMeasurementScope)."""
    def __init__(self):
        self.calls = []

    def StartRecord(self):
        self.calls.append("start")
        return 0

    def StopRecord(self):
        self.calls.append("stop")
        return 0


def _world(times, tmp):
    """The fixture's fake XAE (plus I/O devices, the PLC instance, a Measurement project and a Scope) whose DTE is
    served by a shell that rejects each COM access `times` times (None: always)."""
    be = X.XaeBackend(XaeConfig(com_retry_s=60))
    sm = FakeSysMan()
    tiid = FakeItem("TIID", "TIID", [FakeItem("Device 1 (EtherCAT)", "TIID^Device 1 (EtherCAT)")])
    grp = FakeItem("PlcTask Inputs", "TIPC^Plc^Plc Instance^PlcTask Inputs",
                   xml="<TreeItem><Vars><Var><Name>bIn</Name><Type>BOOL</Type></Var></Vars></TreeItem>")
    inst = FakeItem("Plc Instance", "TIPC^Plc^Plc Instance", [grp])
    for it in (tiid, tiid._children[0], grp, inst):
        sm.items[it.PathName] = it
    sm.items["TIPC^Plc^Plc Project"].CheckAllObjects = lambda: True
    dte = FakeDTE(sm)
    scope_item = _types.SimpleNamespace(Name="Scope_Counter", Object=_Ms(), SubProject=None)
    meas = _types.SimpleNamespace(Name="Measurement", FullName=r"C:\P\M.tcmproj", ProjectItems=FakeProjects([scope_item]))
    dte.Solution.Projects._l.append(meas)
    dte.Solution.Open = lambda p: None
    dte.Solution.Close = lambda save: None
    shell = _BusyShell(times)
    proxy = shell.wrap(dte)
    be._require_win = lambda: None
    be._rot_candidates = lambda: [{"progid": "TcXaeShell.DTE.17.0", "moniker": "!TcXaeShell.DTE.17.0:1",
                                   "dte": proxy, "solution": r"C:\P\Machine.sln"}]
    be._dte2 = lambda: None
    return be, sm, shell


def _replace_import(be, sm, tmp):
    proj, orig, meth, f, dup = _r15_setup(sm, tmp)
    proj.on_import = dup
    return be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")


_MAIN = "TIPC^Plc^Plc Project^POUs^MAIN"
_POUS = "TIPC^Plc^Plc Project^POUs"
# every public method of XaeBackend that touches COM
_PUBLIC_XAE = {
    "attach": lambda be, sm, tmp: be.attach(),
    "status": lambda be, sm, tmp: (be.attach(), be.status())[1],
    "open_solution": lambda be, sm, tmp: (be.attach(), be.open_solution(str(tmp / "Machine.sln")))[1],
    "save_all": lambda be, sm, tmp: be.save_all(),
    "close_solution": lambda be, sm, tmp: be.close_solution(),
    "build": lambda be, sm, tmp: be.build(),
    "build(clean)": lambda be, sm, tmp: be.build(clean=True),
    "errors": lambda be, sm, tmp: be.errors(),
    "build_output": lambda be, sm, tmp: be.build_output(),
    "command": lambda be, sm, tmp: be.command("File.SaveAll"),
    "list_commands": lambda be, sm, tmp: be.list_commands("PLC"),
    "plc_action": lambda be, sm, tmp: be.plc_action("logout"),
    "target_get": lambda be, sm, tmp: be.target_get(),
    "target_set": lambda be, sm, tmp: be.target_set("5.9.9.9.1.1"),
    "activate_configuration": lambda be, sm, tmp: be.activate_configuration(),
    "restart_twincat": lambda be, sm, tmp: be.restart_twincat(),
    "item_exists": lambda be, sm, tmp: be.item_exists(_MAIN),
    "tree_item": lambda be, sm, tmp: be.tree_item(_MAIN),
    "tree_children": lambda be, sm, tmp: be.tree_children("TIPC", depth=4),
    "tree_xml": lambda be, sm, tmp: be.tree_xml("TIRC^Tasks^PlcTask"),
    "tree_consume_xml": lambda be, sm, tmp: be.tree_consume_xml("TIRC^Tasks^PlcTask", "<TreeItem/>"),
    "tree_set_values": lambda be, sm, tmp: be.tree_set_values("TIRC^Tasks^PlcTask", {"TaskDef/CycleTime": "50000"}),
    "tree_create_child": lambda be, sm, tmp: be.tree_create_child(_POUS, "FB_New", 604),
    "tree_delete_child": lambda be, sm, tmp: be.tree_delete_child(_POUS, "MAIN"),
    "plc_projects": lambda be, sm, tmp: be.plc_projects(),
    "pou_get": lambda be, sm, tmp: be.pou_get(_MAIN),
    "pou_set": lambda be, sm, tmp: be.pou_set(_MAIN, "PROGRAM MAIN\nVAR\nEND_VAR", "x := 1;"),
    "pou_create": lambda be, sm, tmp: be.pou_create(_POUS, "FB_X", "fb", declaration="FUNCTION_BLOCK FB_X"),
    "pou_create(property)": lambda be, sm, tmp: be.pou_create(_MAIN, "P", "property", return_type="INT"),
    "plcopen_import(replace)": _replace_import,
    "plcopen_export": lambda be, sm, tmp: be.plcopen_export("TIPC^Plc", ["MAIN"], str(tmp / f"x{id(be)}.xml")),
    "boot_project": lambda be, sm, tmp: be.boot_project("TIPC^Plc", True, True),
    "io_disable": lambda be, sm, tmp: be.io_disable(True),
    "links": lambda be, sm, tmp: be.links("TIPC^Plc"),
    "check_all_objects": lambda be, sm, tmp: be.check_all_objects(),
    "scope_list": lambda be, sm, tmp: be.scope_list(),
    "scope_action": lambda be, sm, tmp: be.scope_action("start"),
}
_VOLATILE = {"duration_s", "command_s", "elapsed_s", "file"}


def _stable(v, run_dir):
    """A result without its timings, the run's own folder written as <run>."""
    if isinstance(v, dict):
        return {k: _stable(x, run_dir) for k, x in v.items() if k not in _VOLATILE}
    if isinstance(v, list):
        return [_stable(x, run_dir) for x in v]
    return v.replace(str(run_dir), "<run>") if isinstance(v, str) else v


@pytest.mark.parametrize("name", list(_PUBLIC_XAE))
def test_n174_a_busy_shell_is_asked_again_by_every_public_method(name, monkeypatch, tmp_path):
    """B1: each COM access is rejected (RPC_E_CALL_REJECTED) three times, then served: the result is the one an idle
    shell gives — no raw com_error escapes (the 'internal error' of xae_build / xae_deploy), and no access inside a
    try/except reads the busy shell as 'nothing there' (a missing tree item, no solution, an empty Error List)."""
    monkeypatch.setattr(X, "time", _Clock(step=0.01))
    calm = tmp_path / "calm"
    busy = tmp_path / "busy"
    for d in (calm, busy):
        d.mkdir()
        (d / "Machine.sln").write_text("")
    be, sm, shell = _world(0, calm)
    want = _stable(_PUBLIC_XAE[name](be, sm, calm), calm)
    be, sm, shell = _world(3, busy)
    try:
        got = _stable(_PUBLIC_XAE[name](be, sm, busy), busy)
    except ComError as exc:
        pytest.fail(f"{name}: a raw com_error escaped ({exc}) after {shell.rejected} rejections")
    assert shell.rejected > 0
    assert got == want, f"{name} on a busy shell:\n{got}\nidle:\n{want}"


@pytest.mark.parametrize("name", list(_PUBLIC_XAE))
def test_n174_a_shell_that_stays_busy_is_said_never_a_raw_error(name, monkeypatch, tmp_path):
    """B2: every COM access rejected for ever: after com_retry_s the method raises an XaeError that says XAE is busy
    and did not run the call — never a raw com_error, never a result read from 'nothing'."""
    monkeypatch.setattr(X, "time", _Clock())
    (tmp_path / "Machine.sln").write_text("")
    be, sm, shell = _world(None, tmp_path)
    if name in ("attach", "status"):
        # status reports instead of raising: what could not be read says the shell is busy, and nothing is claimed
        # about the solution (it said 'no solution open' when the read was rejected)
        try:
            r = _PUBLIC_XAE[name](be, sm, tmp_path)
        except ComError as exc:
            pytest.fail(f"{name}: a raw com_error escaped ({exc})")
        blob = str(r)
        assert "XAE is busy" in blob and "no solution open" not in blob, r
        return
    if name == "item_exists":
        assert _PUBLIC_XAE[name](be, sm, tmp_path) is None     # N124: 'the shell cannot say', never 'gone'
        return
    with pytest.raises(X.XaeError) as ei:
        _PUBLIC_XAE[name](be, sm, tmp_path)
    assert "XAE is busy (call rejected, 0x80010001)" in str(ei.value) and "did not run" in str(ei.value), str(ei.value)


# ------------------------------------------------------------------------------------------------ GetLastErrorMessages
class _MsgSysMan(FakeSysMan):
    """ITcSysManager2::GetLastErrorMessages as TE1000's current page describes it: the error and warning messages of the
    internal buffer, the buffer cleared by the read (clears=False: a build where the read leaves it)."""

    def __init__(self, clears=True):
        super().__init__()
        self.buffer, self.clears, self.reads = "", clears, 0

    def GetLastErrorMessages(self):
        self.reads += 1
        b = self.buffer
        if self.clears:
            self.buffer = ""
        return b

    def ActivateConfiguration(self):
        self.buffer += "Warning: Init4\\Activate Configuration: the license expires in 3 days\n"
        super().ActivateConfiguration()


def _xae_with(monkeypatch, sm):
    be = X.XaeBackend(XaeConfig(com_retry_s=0.3))
    dte = FakeDTE(sm)
    monkeypatch.setattr(be, "_require_win", lambda: None)
    monkeypatch.setattr(be, "_rot_candidates", lambda: [{"progid": "TcXaeShell.DTE.17.0", "moniker": "!TcXaeShell.DTE.17.0:1",
                                                         "dte": dte, "solution": dte.Solution.FullName}])
    monkeypatch.setattr(be, "_dte2", lambda: None)
    be.attach()
    return be, dte, sm


WARN = "Warning: Init4\\Activate Configuration: the license expires in 3 days"


def test_glem_an_action_records_what_the_system_manager_logged_apart_from_earlier_steps(monkeypatch):
    """0.8.16: GetLastErrorMessages — 'many methods log additional diagnostic messages here, even if their HRESULT is
    S_OK' (TE1000). A successful action records its messages; what the buffer held before it came from earlier steps
    and is kept apart, labelled so."""
    be, _dte, sm = _xae_with(monkeypatch, _MsgSysMan())
    sm.buffer = "Error: an earlier step\n"
    assert be.activate_configuration()["activated"] is True
    assert be.messages == [{"action": "ActivateConfiguration", "earlier_messages": "Error: an earlier step",
                            "earlier_note": "in the buffer before this action: logged by earlier steps, not by it",
                            "messages": WARN, "lines": [WARN]}], be.messages
    be.messages = []
    be.tree_delete_child("TIPC^Plc", "Old")                  # an action that logs nothing: no record
    assert be.messages == [] and sm.reads >= 4


def test_glem_a_failed_action_carries_its_messages(monkeypatch):
    """A failure's error carries what the System Manager logged during the action (the HRESULT alone says 'E_FAIL');
    the record is there too."""
    be, _dte, sm = _xae_with(monkeypatch, _MsgSysMan())

    def fail():
        sm.buffer += "Error: Init4\\Activate Configuration: ADS error 0x745 (1861)\nError: target not reachable\n"
        raise ComError(-2147467259, "Unspecified error")
    sm.ActivateConfiguration = fail
    with pytest.raises(X.XaeError, match=r"Unspecified error \(HRESULT 0x80004005\) — XAE messages \(ActivateConfiguration\): Error: Init4"
                                         r"\\Activate Configuration: ADS error 0x745 \(1861\)\nError: target not"):
        be.activate_configuration()
    assert be.messages[0]["lines"] == ["Error: Init4\\Activate Configuration: ADS error 0x745 (1861)",
                                       "Error: target not reachable"], be.messages


def test_glem_a_buffer_the_read_does_not_clear_gives_only_the_new_part(monkeypatch):
    """The TwinCAT 2.11 page says only 'returns the last error messages': on a build whose read leaves the buffer, what
    it held before the action is history — only what the action added is its messages, and nothing is 'earlier'."""
    be, _dte, sm = _xae_with(monkeypatch, _MsgSysMan(clears=False))
    sm.buffer = "Error: an old step\n"
    be.activate_configuration()
    assert be.messages == [{"action": "ActivateConfiguration", "messages": WARN, "lines": [WARN]}], be.messages


def test_glem_without_the_member_or_on_a_busy_read_the_action_runs_as_before(monkeypatch):
    """No ITcSysManager2 (FakeSysMan has no GetLastErrorMessages): no messages, the action unchanged. A read the shell
    keeps rejecting is left out — never the reason an action fails."""
    be, _dte, sm = _xae_with(monkeypatch, FakeSysMan())
    assert be.activate_configuration()["activated"] is True and be.messages == [] and sm.activated == 1
    be, _dte, sm = _xae_with(monkeypatch, _MsgSysMan())

    def busy():
        raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
    sm.GetLastErrorMessages = busy
    monkeypatch.setattr(be, "_modal_dialogs", lambda: [])
    assert be.activate_configuration()["activated"] is True and be.messages == [] and sm.activated == 1


def test_glem_create_child_on_a_new_item_and_an_import_are_read_too(monkeypatch):
    """Every action that must not run twice is read: CreateChild (also on an item this call just created), DeleteChild,
    PlcOpenImport, GenerateBootProject — through the one place they run (_on_item / _on_sm / _create_child)."""
    be, _dte, sm = _xae_with(monkeypatch, _MsgSysMan())
    parent = sm.items["TIPC^Plc^Plc Project^POUs"]
    orig = parent.CreateChild

    def create(name, subtype, before="", info=None):
        sm.buffer += f"Info: {name} created\n"
        return orig(name, subtype, before, info)
    parent.CreateChild = create
    be.tree_create_child("TIPC^Plc^Plc Project^POUs", "FB_New", 604)
    child = be._create_child(parent, "Sub", 604, "", None)
    assert child.Name == "Sub"
    assert [(r["action"], r["messages"]) for r in be.messages] == [("CreateChild", "Info: FB_New created"),
                                                                    ("CreateChild", "Info: Sub created")], be.messages


# ------------------------------------------------------------------------------------------------ review of 0.8.16
def _be_on(monkeypatch, cands, com_retry_s=0.3):
    be = X.XaeBackend(XaeConfig(com_retry_s=com_retry_s))
    monkeypatch.setattr(be, "_require_win", lambda: None)
    monkeypatch.setattr(be, "_rot_candidates", lambda: cands[0])
    monkeypatch.setattr(be, "_dte2", lambda: None)
    monkeypatch.setattr(be, "_modal_dialogs", lambda: [])
    be.attach()
    return be


def _cand(dte, n=1):
    return {"progid": "TcXaeShell.DTE.17.0", "moniker": f"!TcXaeShell.DTE.17.0:{n}", "dte": dte,
            "solution": dte.Solution.FullName}


def test_review_0816_a_shell_that_went_away_is_not_read_for_the_action_s_messages(monkeypatch):
    """Review of 0.8.16 (5): the shell died during ActivateConfiguration; the server re-attached, and the messages were
    then read — and cleared — from the NEW shell's buffer, attributed to the action that ran on the old one. Only the
    System Manager the action ran on is read."""
    old, new = _MsgSysMan(), _MsgSysMan()
    old_dte, new_dte = FakeDTE(old), FakeDTE(new)
    cands = [[_cand(old_dte)]]
    be = _be_on(monkeypatch, cands)
    new.buffer = "Info: solution Machine.sln loaded by the new shell\n"

    def die():
        cands[0] = [_cand(new_dte, 2)]
        raise ComError(X.RPC_E_DISCONNECTED, "The object invoked has disconnected from its clients.")
    old.ActivateConfiguration = die
    with pytest.raises(X.XaeExplained) as ei:
        be.activate_configuration()
    assert "went away during the call" in str(ei.value) and "new shell" not in str(ei.value), str(ei.value)
    assert new.reads == 0 and new.buffer.startswith("Info: solution") and be.messages == []


def test_review_0816_a_clearing_buffer_keeps_an_action_that_logs_the_same_text_again(monkeypatch):
    """Review of 0.8.16 (7): 'the after-read starts with the before-read, so the buffer did not clear' dropped the
    messages of an action that logged the text the buffer already held (a warning of the user's own activation, logged
    again). Whether a read clears the buffer is read from the buffer itself, once: the second read is empty."""
    sm = _MsgSysMan(clears=True)
    be = _be_on(monkeypatch, [[_cand(FakeDTE(sm))]])
    sm.buffer = WARN + "\n"                          # left by an activation the user made in XAE
    assert be.activate_configuration()["activated"] is True
    assert be.messages == [{"action": "ActivateConfiguration", "earlier_messages": WARN,
                            "earlier_note": "in the buffer before this action: logged by earlier steps, not by it",
                            "messages": WARN, "lines": [WARN]}], be.messages
    be.messages = []
    sm.buffer = "Error: license missing\n"

    def fail():
        sm.buffer += "Error: license missing\nError: activation aborted\n"
        raise ComError(-2147467259, "Unspecified error")
    sm.ActivateConfiguration = fail
    with pytest.raises(X.XaeError) as ei:
        be.activate_configuration()
    assert "XAE messages (ActivateConfiguration): Error: license missing\nError: activation aborted" in str(ei.value)
    # a buffer the read does not clear: learnt the same way, the old text is history
    sm2 = _MsgSysMan(clears=False)
    be2 = _be_on(monkeypatch, [[_cand(FakeDTE(sm2))]])
    sm2.buffer = WARN + "\n"
    be2.activate_configuration()
    assert be2.messages == [{"action": "ActivateConfiguration", "messages": WARN, "lines": [WARN]}], be2.messages
    assert be2._msg_clears is False and be._msg_clears is True


def test_review_0816_a_busy_shell_is_asked_for_the_messages_two_seconds_not_com_retry_s(monkeypatch):
    """Review of 0.8.16 (9): the read meant to give up after 2 s first asked whether the member exists with the whole
    com_retry_s — a busy shell after an activation made it wait (60 s by default). The member is asked once per System
    Manager, and every read of the messages gives up after MESSAGES_READ_S."""
    import time as _t
    sm = _MsgSysMan()
    be = _be_on(monkeypatch, [[_cand(FakeDTE(sm))]], com_retry_s=8.0)
    busy = {"on": False}
    orig_read, orig_act = sm.GetLastErrorMessages, sm.ActivateConfiguration

    def read():
        if busy["on"]:
            raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
        return orig_read()

    def act():
        orig_act()
        busy["on"] = True                            # the shell is busy right after the activation
    sm.GetLastErrorMessages, sm.ActivateConfiguration = read, act
    real_has = X._has_prop

    def has_prop(obj, name):                         # the member check is rejected too while the shell is busy
        if busy["on"] and name == "GetLastErrorMessages":
            raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
        return real_has(obj, name)
    monkeypatch.setattr(X, "_has_prop", has_prop)
    t0 = _t.time()
    assert be.activate_configuration()["activated"] is True
    assert _t.time() - t0 < 4.0, _t.time() - t0      # MESSAGES_READ_S (2 s), not com_retry_s (8 s)
    assert be.messages == []


def test_review_0816_the_build_finds_the_system_manager_to_read(monkeypatch):
    """Review of 0.8.16 (13): the build read the messages only when a System Manager happened to be cached — right
    after attach or a solution reopen none is, and the build's messages were never read."""
    sm = _MsgSysMan()
    dte = FakeDTE(sm)
    be = _be_on(monkeypatch, [[_cand(dte)]])
    be.sysman = None
    orig = dte.Solution.SolutionBuild.Build

    def build(wait):
        sm.buffer += "Warning: the build logged this\n"
        return orig(wait)
    dte.Solution.SolutionBuild.Build = build
    be.build()
    assert [(r["action"], r["messages"]) for r in be.messages] == [("SolutionBuild.Build",
                                                                     "Warning: the build logged this")], be.messages


def test_rereview_0816_a_rejected_second_read_keeps_the_action_s_messages(monkeypatch):
    """Re-review of 0.8.16 (12): when the read that learns whether the buffer clears was rejected (busy), the old
    'starts with it' guess returned and an action that logged the same text again lost its messages. Unknown is taken
    as TE1000 documents it: the read clears."""
    sm = _MsgSysMan(clears=True)
    be = _be_on(monkeypatch, [[_cand(FakeDTE(sm))]])
    be.MESSAGES_READ_S = 0.2
    sm.buffer = WARN + "\n"
    calls = {"n": 0}
    orig = sm.GetLastErrorMessages

    def read():
        calls["n"] += 1
        if calls["n"] == 2:
            raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
        return orig()
    sm.GetLastErrorMessages = read
    monkeypatch.setattr(be, "_retry", lambda fn, what="", once=False, retry_s=None: fn() if "GetLastErrorMessages"
                        not in what or calls["n"] != 1 else _raise_busy(fn))
    be.activate_configuration()
    assert be._msg_clears is None
    assert [(r.get("earlier_messages"), r.get("messages")) for r in be.messages] == [(WARN, WARN)], be.messages


def _raise_busy(fn):
    try:
        return fn()
    except ComError as exc:
        raise X.XaeBusy("busy") from exc
