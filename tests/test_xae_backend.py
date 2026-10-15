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
    assert r["error_source"] == "OutputPane"
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
    assert be.plc_projects()["projects"] == [{"name": "Plc Project", "path": "TIPC^Plc^Plc Project"}]
    r = be.tree_set_values("TIRC^Tasks^PlcTask", {"TaskDef/CycleTime": "50000"})
    assert r["changed"]["TaskDef/CycleTime"] == {"old": "100000", "new": "50000"}
    assert "<CycleTime>50000</CycleTime>" in sm.items["TIRC^Tasks^PlcTask"].consumed[0]
    with pytest.raises(X.XaeError, match="not found"):
        be.tree_set_values("TIRC^Tasks^PlcTask", {"Nope/X": "1"})
    with pytest.raises(X.XaeError, match="no item"):
        be.tree_item("TIPC^Missing")


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
