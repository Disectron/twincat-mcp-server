"""0.8.19 retest (Ryan, 2026-10-24, bugs.md §39 – §39d): N199 — xae_library_add(library='Tc2_Math') failed on XAE
4024 with "LibInfo display name 'Tc2_Math' is not valid" (the bare name was passed with version and distributor empty;
the display name 'Tc2_Math, * (Beckhoff Automation GmbH)' worked); N198 — Tc3_GlobalTypes, the virtual data-type
library TwinCAT adds itself (build 4026+), made saved_plcproj.same false even after a save."""
from __future__ import annotations

import pytest

from tests.test_libraries import PROJ, Lib, Placeholder, _libs, _plcproj, _server
from tests.test_server_e2e import client_for, payload
from twincat_mcp import xae_backend as X


# ---- N199 ------------------------------------------------------------------------------------------------------------

def test_n199_a_bare_name_is_added_by_its_display_name_completed_from_the_installed_libraries(monkeypatch):
    be, lm = _libs(monkeypatch)
    r = be.library_add(PROJ, "ex_math")                  # any case, as the retest typed it
    assert lm.calls == [("AddLibrary", "Ex_Math, * (Example Corp)", "", "")], lm.calls
    assert [x["name"] for x in r["added"]] == ["Ex_Math"] and "warning" not in r, r
    assert "named as 'Ex_Math, * (Example Corp)'" in r["named_as"] and "newest installed" in r["named_as"], r


def test_n199_a_version_without_distributor_is_completed_and_must_be_installed(monkeypatch):
    be, lm = _libs(monkeypatch)
    with pytest.raises(X.XaeError, match=r"Ex_Math 9\.0\.0\.0 is not installed \(installed: \['1\.4\.0\.0'\]\) — "
                                         "nothing was done"):
        be.library_add(PROJ, "Ex_Math", "9.0.0.0")
    with pytest.raises(X.XaeError, match=r"Ex_Base 9\.0\.0\.0 is not installed \(installed: \['1\.0\.0\.0', "
                                         r"'1\.2\.0\.0'\]\)"):
        be.placeholder_resolve(PROJ, "Ex_Base", "Ex_Base", "9.0.0.0")
    assert lm.calls == []
    r = be.library_add(PROJ, "Ex_Math", "1.4.0.0")
    assert lm.calls == [("AddLibrary", "Ex_Math, 1.4.0.0 (Example Corp)", "", "")] and r["added"], lm.calls


def test_n199_not_installed_or_several_distributors_is_refused_before_xae_is_asked(monkeypatch):
    be, lm = _libs(monkeypatch)
    with pytest.raises(X.XaeError, match="Ex_Missing is not installed .*nothing was done"):
        be.library_add(PROJ, "Ex_Missing")
    lm.installed.append(Lib("Ex_Math", "1.5.0.0", "Other GmbH"))
    with pytest.raises(X.XaeError, match=r"several distributors \['Example Corp', 'Other GmbH'\] — say which"):
        be.library_add(PROJ, "Ex_Math")
    with pytest.raises(X.XaeError, match=r"not installed from 'Nobody' — installed from: \['Example Corp', 'Other"):
        be.library_add(PROJ, "Ex_Math", distributor="Nobody")
    assert lm.calls == []
    be.library_add(PROJ, "Ex_Math", distributor="other gmbh")
    assert lm.calls == [("AddLibrary", "Ex_Math, * (Other GmbH)", "", "")], lm.calls


def test_n199_named_in_full_nothing_is_looked_up(monkeypatch):
    """The form that worked on the retest PC: version and distributor given — no ScanLibraries, no refusal on what it
    lists (a library from a repository it may not scan)."""
    be, lm = _libs(monkeypatch)
    r = be.library_add(PROJ, "Ex_Math", "1.4.0.0", "Example Corp")
    assert lm.scans == 0 and lm.calls == [("AddLibrary", "Ex_Math, 1.4.0.0 (Example Corp)", "", "")], lm.calls
    assert "named_as" not in r, r
    be.library_remove(PROJ, "Ex_Math")
    be.library_add(PROJ, "Ex_Math, * (Example Corp)")    # the display name as the retest passed it
    assert lm.scans == 0 and lm.calls[-1] == ("AddLibrary", "Ex_Math, * (Example Corp)", "", ""), lm.calls


def test_n199_scan_failure_says_what_to_give(monkeypatch):
    be, lm = _libs(monkeypatch)

    def broken():
        raise X.XaeError("ScanLibraries: catastrophic failure")
    monkeypatch.setattr(be, "_installed", lambda it, path: broken())
    with pytest.raises(X.XaeError, match=r"could not be read \(ScanLibraries failed: .*\) — give version \(or '\*'\) "
                                         "and distributor. Nothing was done"):
        be.library_add(PROJ, "Ex_Math")
    assert lm.calls == []


def test_n199_placeholders_get_the_distributor_of_their_library(monkeypatch):
    be, lm = _libs(monkeypatch)
    be.placeholder_add(PROJ, "Ex_Math", "Ex_Math")
    assert lm.calls[-1] == ("AddPlaceholder", "Ex_Math", "Ex_Math", "*", "Example Corp"), lm.calls
    be.placeholder_add(PROJ, "Ex_Dev")                    # no library: the device's placeholder, nothing looked up
    assert lm.calls[-1] == ("AddPlaceholder", "Ex_Dev", "", "", "") and lm.scans == 1, lm.calls
    be.placeholder_resolve(PROJ, "Ex_Base", "Ex_Base")
    assert lm.calls[-1] == ("SetEffectiveResolution", "Ex_Base", "Ex_Base", "*", "Example Corp"), lm.calls
    with pytest.raises(X.XaeError, match="Ex_Nope is not installed"):
        be.placeholder_resolve(PROJ, "Ex_Base", "Ex_Nope")


# ---- N198 ------------------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_n198_the_virtual_global_types_library_is_not_a_difference(monkeypatch, project_dir):
    """Beckhoff 'Using global data types': from build 4026 TwinCAT creates the virtual data-type library
    Tc3_GlobalTypes and adds it to the PLC project itself — the .plcproj never holds it."""
    srv, app, lm = _server(monkeypatch, project_dir)
    lm.refs = [Placeholder("Ex_Base", Lib("Ex_Base", "*"), Lib("Ex_Base", "1.2.0.0")),
               Placeholder("Tc3_GlobalTypes", Lib("Tc3_GlobalTypes", "*", "Beckhoff Automation GmbH"))]
    _plcproj(project_dir,
             '<PlaceholderReference Include="Ex_Base"><DefaultResolution>Ex_Base, * (Example Corp)</DefaultResolution>'
             '</PlaceholderReference>')
    async with client_for(srv) as s:
        sv = payload(await s.call_tool("xae_library_list", {}))["saved_plcproj"]
    assert sv["same"] is True and "only_in_xae" not in sv, sv
    assert sv["added_by_twincat"] == ["placeholder Tc3_GlobalTypes"] and "global data types" in \
        sv["added_by_twincat_note"], sv
    lm.refs.append(Lib("Ex_Math", "1.4.0.0"))           # a real difference still counts
    async with client_for(srv) as s:
        sv = payload(await s.call_tool("xae_library_list", {}))["saved_plcproj"]
    assert sv["same"] is False and sv["only_in_xae"] == ["library Ex_Math 1.4.0.0"], sv


# ---- proj_set_code while XAE has the project open (Ryan: warn only) -------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize("case", ["attached", "other_solution", "not_attached", "no_change"])
async def test_proj_set_code_says_when_the_attached_xae_has_the_project_open(monkeypatch, project_dir, tmp_path, case):
    from tests.fake_ads import install
    from tests.test_server_e2e import mk_cfg
    from twincat_mcp.server import build_server
    install(monkeypatch)
    srv = build_server(mk_cfg(project_dir, mode="full"))
    xae = srv._tcmcp_app.xae
    xae.dte = None if case == "not_attached" else object()      # nothing is called on it
    xae.bound_solution = str((tmp_path / "other" / "Other.sln") if case == "other_solution"
                             else (project_dir / "Machine.sln"))
    async with client_for(srv) as s:
        args = {"name": "MAIN", "implementation": "nCycle := nCycle + 2;"}
        if case == "no_change":
            xae.dte = None
            payload(await s.call_tool("proj_set_code", args))
            xae.dte = object()
        r = payload(await s.call_tool("proj_set_code", args))
    if case == "no_change":
        assert r["changed"] == [], r
    if case == "attached":
        assert r["changed"] and "XAE has" in r["xae_open"] and "Machine.sln open (attached)" in r["xae_open"], r
        assert "asks to reload" in r["xae_open"] and "xae_pou_set" in r["xae_open"], r
    else:
        assert "xae_open" not in r, r
