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

    def PlcOpenImport(self, f, opt):
        self.imported.append((f, opt))

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


def test_plcopen_and_boot(xae, tmp_path):
    be, dte, sm = xae
    f = tmp_path / "x.xml"; f.write_text("<project/>")
    be.plcopen_import("TIPC^Plc^Plc Project", str(f), "replace")
    assert sm.items["TIPC^Plc^Plc Project"].imported == [(str(f), 2)]
    with pytest.raises(X.XaeError, match="not found"):
        be.plcopen_import("TIPC^Plc^Plc Project", str(tmp_path / "missing.xml"))
    be.plcopen_export("TIPC^Plc^Plc Project", ["POUs.MAIN", "POUs.FB_Fill"], str(tmp_path / "out.xml"))
    assert sm.items["TIPC^Plc^Plc Project"].exported[1] == "POUs.MAIN;POUs.FB_Fill"
    r = be.boot_project("TIPC^Plc^Plc Project", True, True)
    assert r["autostart"] and sm.items["TIPC^Plc^Plc Project"].generated == [False]


def test_plc_action_fallback_and_commands(xae):
    be, dte, sm = xae
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
