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
    assert X.XaeBackend._non_st_language(tree, "POUs^FB_A") == "POUs^FB_A^Act (CFC)"
    assert X.XaeBackend._non_st_language({"_it": type("I", (), {"Language": 0})(), "kids": {}}, "GVL") is None


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
    be = X.XaeBackend.__new__(X.XaeBackend)
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
