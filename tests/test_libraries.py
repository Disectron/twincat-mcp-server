"""0.8.19: PLC libraries through XAE (docs/PLAN-0.8.19.md §2) — a fake ITcPlcLibraryManager shaped like the TE1000 probe
of 2026-10-22 (References: libraries and placeholders; ScanLibraries with several versions of one library;
Repositories through Count / Item), synthetic names only."""
from __future__ import annotations

import pytest

from tests.fake_ads import install
from tests.test_server_e2e import client_for, mk_cfg, payload, text
from tests.test_xae_backend import ComError, FakeItem, FakeSysMan, _xae_with
from twincat_mcp import xae_backend as X
from twincat_mcp.server import build_server

PROJ = "TIPC^Plc^Plc Project"
REFS = f"{PROJ}^References"


class Lib:
    """_ITcPlcLibrary: Name, Version, Distributor, DisplayName."""

    def __init__(self, name, version, dist="Example Corp"):
        self.Name, self.Version, self.Distributor = name, version, dist
        self.DisplayName = f"{name}, {version} ({dist})"


class Placeholder:
    """_ITcPlcPlaceholderRef: PlaceholderName, DefaultResolution, EffectiveResolution (each an ITcPlcLibrary)."""

    def __init__(self, name, default: Lib, effective: Lib | None = None):
        self.Name, self.PlaceholderName = f"#{name}", name
        self.DefaultResolution, self.EffectiveResolution = default, effective or default


class Coll:
    """A TE1000 collection read through Count / Item(i), 1-based; iterating it fails like a dispatch without _NewEnum."""

    def __init__(self, items):
        self._items = list(items)

    @property
    def Count(self):
        return len(self._items)

    def Item(self, i):
        return self._items[i - 1]

    def __iter__(self):
        raise TypeError("no enumerator")


class Repo:
    def __init__(self, name, folder):
        self.Name, self.Folder = name, folder


class LibMan(FakeItem):
    """'<Plc> Project^References' as _ITcPlcLibraryManager."""

    def __init__(self, path):
        super().__init__("References", path)
        self.installed = [Lib("Ex_Base", "1.0.0.0"), Lib("Ex_Base", "1.2.0.0"), Lib("Ex_Util", "2.1.0.0"),
                          Lib("Ex_Visu", "3.5.10.0", "System"), Lib("Ex_Math", "1.4.0.0")]
        self.refs = [Placeholder("Ex_Base", Lib("Ex_Base", "*"), Lib("Ex_Base", "1.2.0.0")),
                     Placeholder("Ex_Visu", Lib("Ex_Visu", "*", "System"), Lib("Ex_Visu", "3.5.10.0", "System")),
                     Lib("Ex_Visu", "3.5.10.0", "System"),
                     Lib("Ex_Util", "2.0.0.0")]                    # pinned to a version that is not installed
        self.calls = []

    @property
    def References(self):
        return Coll(self.refs)

    @property
    def Repositories(self):
        return Coll([Repo("System", r"C:\TwinCAT\3.1\Components\Plc\Managed Libraries")])

    def ScanLibraries(self):
        return Coll(self.installed)

    def _find_inst(self, name, version):
        c = [x for x in self.installed if x.Name.lower() == name.lower() and (not version or version == "*"
                                                                             or x.Version == version)]
        return sorted(c, key=lambda x: x.Version)[-1] if c else None

    def AddLibrary(self, name, version="", company=""):
        self.calls.append(("AddLibrary", name, version, company))
        if "," in name:                                     # TE1000 takes a display name too
            p = X._lib_parts(name)
            name, version = p["library"], p["version"]
        lib = self._find_inst(name, version)
        if lib is None:
            raise ComError(-2147467259, "TSM_E_ITEMNOTFOUND: managed library not found")
        self.refs.append(Lib(lib.Name, version or lib.Version, lib.Distributor))

    def RemoveReference(self, name, version="", company=""):
        self.calls.append(("RemoveReference", name, version, company))
        for r in self.refs:
            nm = getattr(r, "PlaceholderName", None) or r.Name
            if nm.lower() == name.lower() and (not version or getattr(r, "Version", None) == version):
                self.refs.remove(r)
                return
        raise ComError(-2147467259, "Reference not found")

    def AddPlaceholder(self, name, lib="", version="", dist=""):
        self.calls.append(("AddPlaceholder", name, lib, version, dist))
        eff = self._find_inst(lib or name, version)
        self.refs.append(Placeholder(name, Lib(lib or name, version or "*", dist or "Example Corp"), eff))

    def SetEffectiveResolution(self, name, lib, version="", dist=""):
        self.calls.append(("SetEffectiveResolution", name, lib, version, dist))
        ph = next(r for r in self.refs if getattr(r, "PlaceholderName", "").lower() == name.lower())
        ph.EffectiveResolution = self._find_inst(lib, version)

    def FreezePlaceholder(self, name=""):
        self.calls.append(("FreezePlaceholder", name))


def _libs(monkeypatch, sm=None):
    sm = sm or FakeSysMan()
    lm = LibMan(REFS)
    sm.items[REFS] = lm
    be, dte, sm = _xae_with(monkeypatch, sm)
    monkeypatch.setattr(be, "_modal_dialogs", lambda: [])
    be.LIB_RELIST_S = 0
    return be, lm


# ---- reads -----------------------------------------------------------------------------------------------------------

def test_library_list_reads_libraries_and_placeholders_and_flags_what_is_wrong(monkeypatch):
    be, lm = _libs(monkeypatch)
    r = be.library_list("TIPC^Plc")                       # any path form (N193)
    assert r["plc_project"] == PROJ and r["count"] == 4, r
    ph = r["references"][0]
    assert ph == {"kind": "placeholder", "name": "Ex_Base",
                  "default": {"library": "Ex_Base", "version": "*", "distributor": "Example Corp",
                              "display": "Ex_Base, * (Example Corp)"},
                  "effective": {"library": "Ex_Base", "version": "1.2.0.0", "distributor": "Example Corp",
                                "display": "Ex_Base, 1.2.0.0 (Example Corp)"},
                  "resolves": "default '*', effective 1.2.0.0 (XAE's effective resolution)"}, ph
    assert r["references"][2] == {"kind": "library", "name": "Ex_Visu", "version": "3.5.10.0", "distributor": "System",
                                  "display": "Ex_Visu, 3.5.10.0 (System)"}
    assert r["referenced_twice"] == {"ex_visu": ["placeholder Ex_Visu", "library Ex_Visu"]}, r
    assert r["references"][3]["not_installed"] == "Ex_Util 2.0.0.0 is not installed (installed: ['2.1.0.0'])"
    assert r["installed_count"] == 5 and len(r["flags"]) == 2, r
    assert "installed_count" not in be.library_list(PROJ, check_installed=False)


def test_library_installed_lists_every_version_and_filters(monkeypatch):
    be, lm = _libs(monkeypatch)
    r = be.library_installed(PROJ, "ex_b")
    assert [(x["library"], x["version"]) for x in r["libraries"]] == [("Ex_Base", "1.0.0.0"), ("Ex_Base", "1.2.0.0")]
    assert r["scanned"] == 5 and r["filter"] == "ex_b"
    r = be.library_installed(PROJ, max_items=2)
    assert r["count"] == 5 and len(r["libraries"]) == 2 and "3 more" in r["truncated"], r


def test_library_repositories_through_count_and_item_and_said_when_not_readable(monkeypatch):
    be, lm = _libs(monkeypatch)
    assert be.library_repositories(PROJ)["repositories"] == [
        {"name": "System", "folder": r"C:\TwinCAT\3.1\Components\Plc\Managed Libraries"}]

    class NoIface(Coll):
        def Item(self, i):
            raise ComError(-2147467262, "No such interface supported")      # E_NOINTERFACE, as the probe saw it
    monkeypatch.setattr(LibMan, "Repositories", property(lambda self: NoIface([1])))
    with pytest.raises(X.XaeError, match="not readable through this XAE's Automation Interface"):
        be.library_repositories(PROJ)


def test_a_project_without_a_library_manager_is_said(monkeypatch):
    be, lm = _libs(monkeypatch)
    be.sysman.items[REFS] = FakeItem("References", REFS)
    with pytest.raises(X.XaeError, match="offers no library manager"):
        be.library_list(PROJ)


# ---- changes ---------------------------------------------------------------------------------------------------------

def test_library_add_runs_once_and_gives_the_difference(monkeypatch):
    be, lm = _libs(monkeypatch)
    r = be.library_add(PROJ, "Ex_Math")
    assert lm.calls == [("AddLibrary", "Ex_Math", "", "")] and be.acted == ["AddLibrary Ex_Math"], lm.calls
    assert [x["name"] for x in r["added"]] == ["Ex_Math"] and r["removed"] == [] and r["changed"] == [], r
    assert r["build_required"] is True and "not built and not saved" in r["note"] and "warning" not in r
    with pytest.raises(X.XaeError, match="Ex_Math is already referenced .* nothing was done"):
        be.library_add(PROJ, "Ex_Math, * (Example Corp)")                   # display-name form
    assert len(lm.calls) == 1


def test_library_add_xae_refusal_carries_its_message_and_nothing_is_said_done(monkeypatch):
    be, lm = _libs(monkeypatch)
    with pytest.raises(X.XaeError, match="TSM_E_ITEMNOTFOUND"):
        be.library_add(PROJ, "Ex_Missing")
    assert [x for x in lm.refs if getattr(x, "Name", "") == "Ex_Missing"] == []


def test_library_add_a_listing_missing_a_reference_right_after_is_read_again(monkeypatch):
    """N195's lesson: right after AddLibrary (a project reload) a reference was missing from one listing."""
    be, lm = _libs(monkeypatch)
    real, n = LibMan.References.fget, {"n": 0}

    def refs(self):
        c = real(self)
        if self.calls:                                     # after the action: the member check, then the listing
            n["n"] += 1
            if n["n"] == 2:                                # the first listing after it misses the first reference
                c._items = c._items[1:]
        return c
    monkeypatch.setattr(LibMan, "References", property(refs))
    r = be.library_add(PROJ, "Ex_Math")
    assert n["n"] == 4 and r["removed"] == [] and [x["name"] for x in r["added"]] == ["Ex_Math"], r


def test_library_remove_refuses_an_ambiguous_name_and_removes_the_one_named(monkeypatch):
    be, lm = _libs(monkeypatch)
    with pytest.raises(X.XaeError, match=r"Ex_Visu is referenced 2 times .* kind='placeholder' or kind='library'"):
        be.library_remove(PROJ, "Ex_Visu")
    with pytest.raises(X.XaeError, match="Ex_Nope is not referenced"):
        be.library_remove(PROJ, "Ex_Nope")
    assert lm.calls == []
    r = be.library_remove(PROJ, "Ex_Visu", kind="library")
    assert lm.calls == [("RemoveReference", "Ex_Visu", "3.5.10.0", "System")], lm.calls
    assert [x["kind"] for x in r["removed"]] == ["library"] and "warning" not in r, r
    assert "does not build any more" in r["consequence"]


def test_library_remove_says_when_xae_removed_the_other_reference(monkeypatch):
    be, lm = _libs(monkeypatch)

    def wrong(name, version="", company=""):
        lm.calls.append(("RemoveReference", name, version, company))
        lm.refs.pop(1)                                     # the placeholder of the same name went instead
    lm.RemoveReference = wrong
    r = be.library_remove(PROJ, "Ex_Visu", kind="library")
    assert "still referenced" in r["warning"], r


def test_placeholder_add_resolve_freeze(monkeypatch):
    be, lm = _libs(monkeypatch)
    r = be.placeholder_add(PROJ, "Ex_Math", "Ex_Math", "*")
    assert lm.calls[-1] == ("AddPlaceholder", "Ex_Math", "Ex_Math", "*", "") and r["added"][0]["kind"] == "placeholder"
    r = be.placeholder_resolve(PROJ, "#Ex_Base", "Ex_Base", "1.0.0.0")
    assert lm.calls[-1] == ("SetEffectiveResolution", "Ex_Base", "Ex_Base", "1.0.0.0", ""), lm.calls
    assert r["changed"][0]["after"]["effective"]["version"] == "1.0.0.0" and "warning" not in r, r
    r = be.placeholder_resolve(PROJ, "Ex_Base", "Ex_Base", "9.9.9.9")       # XAE took it, the resolution says otherwise
    assert "resolves to nothing after it, not to Ex_Base 9.9.9.9" in r["warning"], r
    with pytest.raises(X.XaeError, match="no placeholder Ex_Util"):
        be.placeholder_resolve(PROJ, "Ex_Util", "Ex_Util")                 # a library, not a placeholder
    r = be.placeholder_freeze(PROJ)
    assert lm.calls[-1] == ("FreezePlaceholder", "") and "every placeholder keeps" in r["frozen_note"], r
    be.placeholder_freeze(PROJ, "Ex_Visu")
    assert lm.calls[-1] == ("FreezePlaceholder", "Ex_Visu")


def test_a_busy_shell_refuses_the_change_before_it_runs(monkeypatch):
    be, lm = _libs(monkeypatch)

    def busy(*a):
        raise ComError(X.RPC_E_CALL_REJECTED, "Call was rejected by callee.")
    lm.AddLibrary = busy
    with pytest.raises(X.XaeBusy):
        be.library_add(PROJ, "Ex_Math")
    assert be.acted == []


# ---- through the server ------------------------------------------------------------------------------------------------

def _server(monkeypatch, project_dir, **safety):
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, **safety))
    app = srv._tcmcp_app
    app.dialogs_now = lambda: []
    be, lm = _libs(monkeypatch)
    _solution(monkeypatch, be, str(project_dir / "Machine.sln"))   # the open solution lies over the project root
    app.xae = be
    return srv, app, lm


def _solution(monkeypatch, be, path):
    dte = be.dte
    dte.Solution.FullName = path
    monkeypatch.setattr(be, "_rot_candidates", lambda: [{"progid": "TcXaeShell.DTE.17.0", "moniker": "!TcXaeShell.DTE.17.0:1",
                                                         "dte": dte, "solution": path}])
    be.bound_solution = path


@pytest.mark.anyio
async def test_server_library_list_compares_with_the_saved_plcproj(monkeypatch, project_dir):
    srv, app, lm = _server(monkeypatch, project_dir)
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_library_list", {}))
    sv = r["saved_plcproj"]
    assert sv["compared"] and sv["file"].endswith("Plc.plcproj") and sv["same"] is False, sv
    assert sv["only_in_saved_file"] == ["placeholder Tc2_Standard"] and "placeholder Ex_Base" in sv["only_in_xae"], sv


@pytest.mark.anyio
async def test_server_changes_need_the_users_ok_in_guarded_mode_bound_to_library_and_project(monkeypatch, project_dir):
    srv, app, lm = _server(monkeypatch, project_dir, mode="guarded", allow_elicitation=False)
    async with client_for(srv) as s:
        r = await s.call_tool("xae_library_add", {"library": "Ex_Math"})
        assert r.is_error and "tc_request_confirmation(action='xae_library_add', target='TIPC^Plc^Plc Project:Ex_Math')" \
            in text(r), text(r)
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_library_add",
                                                                    "args": {"library": "Ex_Util"}}))
        assert tok["target"] == f"{PROJ}:Ex_Util", tok
        r = await s.call_tool("xae_library_add", {"library": "Ex_Math", "confirm_token": tok["confirm_token"]})
        assert r.is_error and lm.calls == [], text(r)                        # a token for another library
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_library_add",
                                                                    "args": {"library": "Ex_Math"}}))
        r = payload(await s.call_tool("xae_library_add", {"library": "Ex_Math", "confirm_token": tok["confirm_token"]}))
        assert [x["name"] for x in r["added"]] == ["Ex_Math"] and lm.calls == [("AddLibrary", "Ex_Math", "", "")]
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_placeholder_resolve",
                                                                    "args": {"placeholder": "Ex_Base",
                                                                             "library": "Ex_Base"}}))
        assert tok["target"] == f"{PROJ}:Ex_Base=Ex_Base", tok


@pytest.mark.anyio
async def test_server_readonly_refuses_every_change(monkeypatch, project_dir):
    srv, app, lm = _server(monkeypatch, project_dir, mode="readonly")
    async with client_for(srv) as s:
        for tool, args in (("xae_library_add", {"library": "Ex_Math"}), ("xae_library_remove", {"library": "Ex_Util"}),
                           ("xae_placeholder_add", {"placeholder": "Ex_Math"}),
                           ("xae_placeholder_resolve", {"placeholder": "Ex_Base", "library": "Ex_Base"}),
                           ("xae_placeholder_freeze", {})):
            r = await s.call_tool(tool, args)
            assert r.is_error and "readonly" in text(r), (tool, text(r))
        assert payload(await s.call_tool("xae_library_list", {}))["count"] == 4
    assert lm.calls == []


@pytest.mark.anyio
async def test_server_full_mode_runs_and_audits_the_change(monkeypatch, project_dir):
    import json
    from pathlib import Path
    srv, app, lm = _server(monkeypatch, project_dir, mode="full")
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_placeholder_freeze", {"placeholder": "Ex_Base"}))
    assert lm.calls == [("FreezePlaceholder", "Ex_Base")] and r["action"] == "FreezePlaceholder Ex_Base"
    rows = [json.loads(x) for x in Path(app.cfg.safety.audit_log).read_text(encoding="utf-8").splitlines() if x.strip()]
    mine = [x for x in rows if x.get("tool") == "xae_placeholder_freeze"]
    assert mine and mine[-1]["outcome"] == "ok" and mine[-1]["args"]["target"] == f"{PROJ}:Ex_Base", mine


@pytest.mark.anyio
async def test_server_a_change_carries_xaes_messages(monkeypatch, project_dir):
    """§0 c of the plan: the library actions' messages come with the result (GetLastErrorMessages, 0.8.16)."""
    from tests.test_xae_backend import _MsgSysMan
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="full"))
    app = srv._tcmcp_app
    app.dialogs_now = lambda: []
    sm = _MsgSysMan()
    be, lm = _libs(monkeypatch, sm)
    app.xae = be
    real = lm.AddLibrary

    def add(*a):
        sm.buffer += "Warning: library 'Ex_Math' was added; the project was reloaded\n"
        return real(*a)
    lm.AddLibrary = add
    async with client_for(srv) as s:
        r = payload(await s.call_tool("xae_library_add", {"library": "Ex_Math"}))
    msgs = r.get("xae_messages") or []
    assert any(m.get("action") == "AddLibrary Ex_Math" and "project was reloaded" in m.get("messages", "")
               for m in msgs), r



# ---- review 1 of 0.8.19 ------------------------------------------------------------------------------------------------

def test_r1_repositories_listed_but_not_readable_are_not_rows_of_nothing(monkeypatch):
    be, lm = _libs(monkeypatch)

    def nope(self):
        raise ComError(-2147467262, "No such interface supported")

    class Bare:                                            # the probe's E_NOINTERFACE items: no Name, no Folder
        Name = property(nope)
        Folder = property(nope)
    monkeypatch.setattr(LibMan, "Repositories", property(lambda self: Coll([Bare(), Bare()])))
    with pytest.raises(X.XaeError, match="2 repositories listed, none of them readable"):
        be.library_repositories(PROJ)


def test_r1_xae_removing_the_other_reference_is_said_and_no_consequence_is_claimed(monkeypatch):
    be, lm = _libs(monkeypatch)

    def wrong(name, version="", company=""):
        lm.calls.append(("RemoveReference", name, version, company))
        lm.refs.pop(1)                                     # the placeholder of the same name went instead
    lm.RemoveReference = wrong
    r = be.library_remove(PROJ, "Ex_Visu", kind="library")
    assert "XAE removed ['placeholder ex_visu'] instead of library Ex_Visu, which is still referenced" in r["warning"], r
    assert "consequence" not in r, r


def test_r1_hash_means_placeholder_and_a_version_narrows_to_libraries(monkeypatch):
    be, lm = _libs(monkeypatch)
    with pytest.raises(X.XaeError, match="Ex_Base is referenced .* but not as a library of version 1.2.0.0"):
        be.library_remove(PROJ, "Ex_Base", version="1.2.0.0")       # only a placeholder of that name
    r = be.library_remove(PROJ, "#Ex_Visu")
    assert lm.calls == [("RemoveReference", "Ex_Visu", "", "")] and r["removed"][0]["kind"] == "placeholder", r


def test_r1_r2_a_name_that_is_not_a_display_name_is_refused_not_crashed(monkeypatch):
    be, lm = _libs(monkeypatch)
    for bad in ("Ex_Math, ", "Ex_Math, (Example Corp)", " , 1.0"):
        with pytest.raises(X.XaeError, match="not a display name|no library name"):
            be.library_add(PROJ, bad)
    with pytest.raises(X.XaeError, match="names version '1.4.0.0'.*give one of them"):
        be.library_add(PROJ, "Ex_Math, 1.4.0.0 (Example Corp)", version="1.0.0.0")
    assert lm.calls == []
    r = be.library_add(PROJ, "Ex_Math, 1.4.0.0 (Example Corp)")
    assert lm.calls == [("AddLibrary", "Ex_Math, 1.4.0.0 (Example Corp)", "", "")] and r["added"], r


def _plcproj(project_dir, body):
    (project_dir / "Plc" / "Plc.plcproj").write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<Project xmlns="http://schemas.microsoft.com/developer/msbuild/2003">'
        f'<ItemGroup>{body}</ItemGroup></Project>\n', encoding="utf-8")


@pytest.mark.anyio
async def test_r2_the_saved_file_is_compared_on_what_both_state(monkeypatch, project_dir):
    """Review 2: the round-1 'effective' comparison guessed what the saved file resolves to (profile-managed and
    '*' pinned placeholders gave false differences) and a freeze never showed — only stated facts are compared now,
    and what is not compared is said."""
    srv, app, lm = _server(monkeypatch, project_dir)
    lm.refs = [Placeholder("Ex_Base", Lib("Ex_Base", "*"), Lib("Ex_Base", "1.0.0.0")),
               Placeholder("Ex_Pin", Lib("Ex_Pin", "*"), Lib("Ex_Pin", "2.0.0.0")),
               Lib("Ex_Math", "1.3.0.0"), Lib("Ex_Math", "1.4.0.0")]
    _plcproj(project_dir,
             '<PlaceholderReference Include="Ex_Base"><DefaultResolution>Ex_Base, * (Example Corp)</DefaultResolution>'
             '</PlaceholderReference>'
             '<PlaceholderReference Include="Ex_Pin"><DefaultResolution>Ex_Pin, * (Example Corp)</DefaultResolution>'
             '</PlaceholderReference>'
             '<PlaceholderResolution Include="Ex_Pin"><Resolution>Ex_Pin, * (Example Corp)</Resolution>'
             '</PlaceholderResolution>'
             '<LibraryReference Include="Ex_Math, 1.4.0.0 (Example Corp)" />')
    async with client_for(srv) as s:
        sv = payload(await s.call_tool("xae_library_list", {}))["saved_plcproj"]
    assert sv["compared"] and sv["only_in_xae"] == ["library Ex_Math 1.3.0.0"], sv     # two versions stay two
    assert "version_differs" not in sv, sv                  # '*' pinned and an unpinned effective: not differences
    assert "freeze" in sv["not_compared"], sv


def test_r2_inside_a_drive_root():
    import ntpath
    from unittest import mock
    from twincat_mcp import server as S
    with mock.patch.object(S, "os") as fake_os:
        fake_os.path = ntpath
        fake_os.sep = "\\"
        assert S._inside(r"C:\Proj\Plc\Plc.plcproj", "C:\\")
        assert not S._inside(r"D:\Proj\Plc\Plc.plcproj", "C:\\")


@pytest.mark.anyio
async def test_r1_the_remove_token_names_which_reference(monkeypatch, project_dir):
    srv, app, lm = _server(monkeypatch, project_dir, mode="guarded", allow_elicitation=False)
    async with client_for(srv) as s:
        tok = payload(await s.call_tool("tc_request_confirmation", {"action": "xae_library_remove", "args": {
            "library": "Ex_Visu", "kind": "placeholder"}}))
        assert tok["target"] == f"{PROJ}:Ex_Visu (placeholder)", tok
        r = await s.call_tool("xae_library_remove", {"library": "Ex_Visu", "kind": "library",
                                                     "confirm_token": tok["confirm_token"]})
        assert r.is_error and lm.calls == [], text(r)



@pytest.mark.anyio
async def test_r1_a_plcproj_outside_the_open_solution_is_not_compared(monkeypatch, project_dir, tmp_path):
    srv, app, lm = _server(monkeypatch, project_dir)
    _solution(monkeypatch, app.xae, str(tmp_path / "other" / "Other.sln"))
    async with client_for(srv) as s:
        sv = payload(await s.call_tool("xae_library_list", {}))["saved_plcproj"]
    assert sv["compared"] is False and "is not in the folder of the solution XAE has open" in sv["why"], sv
