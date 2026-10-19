"""Infosys reference lookup against a synthetic infosys.beckhoff.com (tests/fake_infosys.py)."""
from __future__ import annotations

import json

import pytest

from twincat_mcp import infosys as inf
from twincat_mcp.infosys import InfosysIndex, InfosysError, html_to_doc, parse_declaration, classify
from tests import fake_infosys as F
from tests.fake_infosys import FakeFetcher


@pytest.fixture
def idx(tmp_path):
    return InfosysIndex(tmp_path / "docs", fetcher=FakeFetcher(), books=["tcplclib_tc2_system"], max_workers=2)


# ---------------------------------------------------------------------------
# HTML → Markdown and declaration extraction
# ---------------------------------------------------------------------------
def test_html_to_markdown_structure():
    d = html_to_doc(F.PAGES[F.ADSREAD])
    assert d.title == "ADSREAD"
    assert d.headings[:3] == [(1, "ADSREAD"), (2, "Inputs"), (2, "Outputs")]
    assert "var x = 1" not in d.markdown and "footer junk" not in d.markdown and "Home" not in d.markdown
    assert "| NETID | T_AmsNetId | String containing the AMS network ID of the target device. |" in d.markdown
    assert d.markdown.count("```st") == 3
    assert "(see error codes \\| overview)" in d.markdown, "pipes inside cells are escaped"
    assert d.tables[0][0] == ["Name", "Type", "Description"]


def test_title_fallbacks():
    d = html_to_doc("<html><head><title>Foo - Beckhoff Information System</title></head><body><p>x</p></body></html>")
    assert d.title == "Foo"
    d = html_to_doc("<html><body><h2>Only H2</h2><p>x</p></body></html>")
    assert d.title == "Only H2"


def test_declaration_headerless_fb():
    d = html_to_doc(F.PAGES[F.ADSREAD])
    decl = parse_declaration(d.title, d)
    assert decl["kind"] == "FunctionBlock"
    names = [i["name"] for i in decl["inputs"]]
    assert names == ["NETID", "PORT", "IDXGRP", "IDXOFFS", "LEN", "DESTADDR", "READ", "TMOUT"]
    tm = decl["inputs"][-1]
    assert tm["type"] == "TIME" and tm["default"] == "DEFAULT_ADS_TIMEOUT" and tm["description"].startswith("Time-out")
    assert decl["inputs"][0]["comment"] == "the AmsNetId"
    assert [o["name"] for o in decl["outputs"]] == ["BUSY", "ERR", "ERRID"]
    assert decl["declaration"].startswith("FUNCTION_BLOCK ADSREAD")
    # the PROGRAM MAIN example block must not leak into the declaration
    assert "fbAdsRead" not in decl["declaration"]


@pytest.mark.parametrize("url,kind,check", [
    (F.ADSWRITE, "FunctionBlock", lambda d: [i["name"] for i in d["inputs"]] == ["NETID", "WRITE"]),
    (F.T_AMSNETID, "Alias", lambda d: "STRING(23)" in d["declaration"]),
    (F.ST_X, "Struct", lambda d: [m["name"] for m in d["members"]] == ["eState", "nAvailable"]
     and d["members"][0]["description"] == "Current router state." and d["members"][1]["comment"] == "free mailboxes"),
    (F.E_X, "Enum", lambda d: [(e["name"], e["value"]) for e in d["enum_values"]]
     == [("AMSROUTER_STOP", "0"), ("AMSROUTER_START", "1"), ("AMSROUTER_REMOVED", "2")]),
    (F.F_X, "Function", lambda d: d["return_type"] == "UINT" and d["inputs"][0]["name"] == "nVersionElement"),
    (F.LIB, "Library", lambda d: d["declaration"] is None),
    (F.TF, "Page", lambda d: d["inputs"] == []),
])
def test_kinds_and_declarations(url, kind, check):
    d = html_to_doc(F.PAGES[url])
    decl = parse_declaration(d.title, d)
    assert decl["kind"] == kind, decl
    assert check(decl), decl


def test_classify_by_prefix_without_code():
    assert classify("FB_Something", []) == "FunctionBlock"
    assert classify("ST_Something", []) == "Struct"
    assert classify("I_Thing", []) == "Interface"
    assert classify("Overview", []) == "Page"


def test_tc2_filter_and_categories():
    assert inf.is_tc2_book("tcplclibsystem") and inf.is_tc2_book("tcplclibstandard")
    assert not inf.is_tc2_book("tcplclib_tc2_system") and not inf.is_tc2_book("tcplclib_tc3_module")
    assert inf.book_category("tcplclib_tc3_jsonxml") == "plc-library"
    assert inf.book_category("tf6100_tc3_opcua") == "function-tf"
    assert inf.book_category("el1xxx") == "hardware" and inf.book_category("cx9020_hw") == "hardware"
    assert inf.library_from_book("tcplclib_tc3_json_xml") == "Tc3_JsonXml"
    assert inf.library_from_book("tcplclib_tc2_system") == "Tc2_System"
    assert inf.library_from_book("tf5100_tc3_nc_i") is None


# ---------------------------------------------------------------------------
# sitemap, books, indexing
# ---------------------------------------------------------------------------
def test_sitemap_and_books(idx):
    sm = idx.sitemap()
    assert F.ADSREAD in sm and sm[F.ADSREAD].startswith("2026-")
    assert F.TC2 in sm, "the raw sitemap keeps everything; filtering happens in books()/indexing"
    b = idx.books()
    slugs = [x["book"] for x in b["books"]]
    assert "tcplclib_tc2_system" in slugs and "tf5100_tc3_nc_i" in slugs and "tcplclibsystem" not in slugs
    assert b["excluded_tc2_or_rules"] == 1
    assert idx.books(pattern="tcplclib_*")["count"] == 1
    assert idx.books(category="function-tf")["books"][0]["book"] == "tf5100_tc3_nc_i"
    # second call served from the disk cache: no new sitemap fetches
    n = len(idx.fetch.calls)
    InfosysIndex(idx.dir, fetcher=idx.fetch).sitemap()
    assert len(idx.fetch.calls) == n


def test_index_book_incremental_and_refetch_on_lastmod(idx):
    r = idx.index_book("tcplclib_tc2_system")
    assert r["fetched"] == 6 and r["failed"] == [] and r["kinds"]["FunctionBlock"] == 2
    assert idx.indexed_books() == ["tcplclib_tc2_system"]
    r2 = idx.index_book("tcplclib_tc2_system")
    assert r2["fetched"] == 0 and r2["unchanged"] == 6
    # lastmod bump in the sitemap → that one page is refetched
    idx._sitemap[F.ADSREAD] = "2027-01-01T00:00:00+00:00"
    r3 = idx.index_book("tcplclib_tc2_system")
    assert r3["fetched"] == 1
    with pytest.raises(InfosysError, match="excluded"):
        idx.index_book("tcplclibsystem")
    with pytest.raises(InfosysError, match="no book"):
        idx.index_book("tcplclib_tc2_nonsense")


def test_index_glob_and_failure_reporting(idx):
    idx.fetch.fail.add(F.ADSWRITE)
    r = idx.index_books("tcplclib_*")
    bk = r["books"][0]
    assert bk["fetched"] == 5 and bk["failed"][0]["url"] == F.ADSWRITE
    idx.fetch.fail.clear()
    assert idx.index_book("tcplclib_tc2_system")["fetched"] == 1, "a failed page is retried next time"


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------
def test_symbol_search_autoindexes_defaults_and_ranks(idx):
    r = idx.search_symbols("ADSREAD")
    assert r["auto_indexed"][0]["book"] == "tcplclib_tc2_system"
    assert r["items"][0]["name"] == "ADSREAD" and r["items"][0]["library"] == "Tc2_System"
    r = idx.search_symbols("ADS", kind="FunctionBlock")
    assert [i["name"] for i in r["items"]] == ["ADSREAD", "ADSWRITE"]
    assert idx.search_symbols("Router", kind="Enum")["items"][0]["name"] == "E_AmsRouterState"
    assert idx.search_symbols("ams", library="Tc2_System")["count"] == 3
    assert idx.search_symbols("zzz")["count"] == 0 and "hint" in idx.search_symbols("zzz")
    with pytest.raises(InfosysError):
        idx.search_symbols("x", kind="Widget")


def test_declaration_lookup(idx):
    d = idx.declaration("adsread")
    assert d["kind"] == "FunctionBlock" and d["url"] == F.ADSREAD and len(d["inputs"]) == 8
    with pytest.raises(InfosysError, match="no symbol page"):
        idx.declaration("NotAThing")


def test_fulltext_search_local_then_web_fallback(idx):
    idx.index_book("tcplclib_tc2_system")
    r = idx.search("rising edge")
    assert r["backend"] in ("local-fts5", "local-like") and r["items"][0]["title"] == "ADSREAD"
    assert "[rising]" in r["items"][0]["excerpt"] or "rising" in r["items"][0]["excerpt"]
    # nothing local → DuckDuckGo path, TC2 result and non-sitemap result dropped, ?id= stripped
    assert idx.search("technology data SAF", web_fallback=False)["count"] == 0
    r = idx.search("technology data SAF")
    assert r["backend"] == "duckduckgo+sitemap"
    assert [i["url"] for i in r["items"]] == [F.TF]
    assert r["items"][0]["title"] == "Display of technology data"
    # the fallback cached the page, so the next search is answered locally
    r = idx.search("technology data SAF")
    assert r["backend"].startswith("local") and r["items"][0]["url"] == F.TF


def test_like_fallback_when_no_fts(idx, monkeypatch):
    idx.index_book("tcplclib_tc2_system")
    idx._fts = False
    r = idx.search("rising edge")
    assert r["backend"] == "local-like" and r["items"][0]["title"] == "ADSREAD"


def test_code_search(idx):
    idx.index_book("tcplclib_tc2_system")
    r = idx.search_code("IDXGRP:=16#F005", language="st")
    assert r["count"] == 1 and r["items"][0]["title"] == "ADSREAD" and "PROGRAM MAIN" in r["items"][0]["snippet"]
    with pytest.raises(InfosysError, match="language 'xml' does not occur in the index"):      # R22
        idx.search_code("IDXGRP:=16#F005", language="xml")


def test_get_page_resolution_forms(idx):
    idx.index_book("tcplclib_tc2_system")
    for ref in (F.ADSREAD, F.ADSREAD + "?id=123", "content/1033/tcplclib_tc2_system/30945803.html",
                "tcplclib_tc2_system/30945803", "30945803", "ADSREAD", "adsread"):
        p = idx.get_page(ref)
        assert p["url"] == F.ADSREAD and p["title"] == "ADSREAD" and p["in_sitemap"], ref
    # a live page that is not indexed and not even in the sitemap still works
    p = idx.get_page(F.LIB)
    assert p["kind"] == "Library" and p["in_sitemap"] is False
    with pytest.raises(InfosysError, match="only infosys.beckhoff.com"):
        idx.get_page("https://example.com/x.html")
    with pytest.raises(InfosysError, match="404"):
        idx.get_page("tcplclib_tc2_system/1")
    assert idx.get_page("ADSREAD", max_chars=50)["truncated"] is True


def test_fetcher_host_allowlist():
    f = inf.Fetcher()
    with pytest.raises(InfosysError, match="refusing"):
        f.get("https://example.com/")


# ---------------------------------------------------------------------------
# through the MCP server
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_infosys_tools_e2e(monkeypatch, tmp_path):
    from tests.test_server_e2e import client_for, payload
    from twincat_mcp.config import Config, validate
    from twincat_mcp.server import build_server

    monkeypatch.setattr(inf, "Fetcher", lambda *a, **k: FakeFetcher())
    cfg = Config()
    cfg.docs.source = "web"              # this test exercises the website path (sitemap + DuckDuckGo fallback)
    cfg.docs.cache_dir = str(tmp_path / "docs")
    cfg.docs.books = ["tcplclib_tc2_system"]
    validate(cfg)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        names = {t.name for t in (await s.list_tools()).tools}
        assert {"infosys_books", "infosys_index", "infosys_search_symbols", "infosys_declaration",
                "infosys_search", "infosys_search_code", "infosys_get_page"} <= names
        d = payload(await s.call_tool("infosys_declaration", {"symbol": "ADSREAD"}))
        assert d["inputs"][0]["name"] == "NETID"
        r = payload(await s.call_tool("infosys_books", {"pattern": "tcplclib_*"}))
        assert r["books"][0]["indexed"] is True
        r = payload(await s.call_tool("infosys_search", {"query": "technology data SAF"}))
        assert r["backend"] == "duckduckgo+sitemap"
        res = await s.call_tool("infosys_get_page", {"ref": "https://evil.example/x"})
        assert res.is_error and "only infosys.beckhoff.com" in res.content[0].text
        doc = payload(await s.call_tool("tc_doctor", {}))
        assert doc["infosys"]["indexed_books"] == ["tcplclib_tc2_system"]


def test_declaration_shipped_as_div_br_instead_of_pre():
    """If the real pages carry declarations in <div> + <br> rather than <pre>, the reflow pass must
    still recover line structure (and line comments must not swallow the next declaration)."""
    h = ("<html><body><h1>FB_Demo</h1><p>Does<br>things.</p>"
         '<div class="code">FUNCTION_BLOCK FB_Demo<br>VAR_INPUT<br>&nbsp;&nbsp;bStart : BOOL; // go<br>'
         "&nbsp;&nbsp;tDelay : TIME := T#1S; a, b : INT;<br>END_VAR<br>VAR_OUTPUT<br>&nbsp;&nbsp;bDone : BOOL;<br>END_VAR</div>"
         "<table><tr><th>Name</th><th>Type</th><th>Description</th></tr><tr><td>bStart</td><td>BOOL</td><td>Starts.<br>Really.</td></tr></table>"
         "</body></html>")
    d = html_to_doc(h)
    assert d.code_blocks and d.code_blocks[0][0] == "st"
    dec = parse_declaration(d.title, d)
    assert dec["kind"] == "FunctionBlock"
    assert [(i["name"], i["type"], i["default"], i["comment"]) for i in dec["inputs"]] == [
        ("bStart", "BOOL", None, "go"), ("tDelay", "TIME", "T#1S", None), ("a", "INT", None, None), ("b", "INT", None, None)]
    assert dec["inputs"][0]["description"] == "Starts. Really."
    assert "Does things." in d.markdown and d.markdown.count("\n|") >= 2, "tables stay multi-line"


def test_r17_to_r22_code_search(idx, tmp_path, monkeypatch):
    """R17: code stored with non-breaking spaces never matched a query with a space. R19: LIMIT before the language
    filter. R21: limit 0 / -1 / 1000. R22: unknown language → 0 and a wrong hint; unlabelled samples unreachable."""
    idx.index_book("tcplclib_tc2_system")
    assert idx.search_code("fbAdsRead(")["count"] == 1
    assert idx.search_code("fbAdsRead ( NETID := ''")["count"] == 0                    # exact by default
    assert idx.search_code("fbAdsRead ( NETID := ''", ignore_spacing=True)["count"] == 1
    for bad in (0, -1, 1000):
        with pytest.raises(InfosysError, match="limit"):
            idx.search_code("fbAdsRead", limit=bad)
    r = idx.search_code("NoSuchThing(")
    assert r["count"] == 0 and "hint" in r
    # R17 at the root: a page whose code is indented with non-breaking spaces (the Help Viewer pages: 21,166 of
    # 24,960) is stored with plain spaces — a query typed with spaces finds it, nothing is repaired when it is read
    url = next(u for u, h in F.PAGES.items() if "READ:=TRUE" in h)
    monkeypatch.setitem(F.PAGES, url, F.PAGES[url].replace("READ:=TRUE", "READ\u00a0:=\u00a0TRUE"))
    nb = InfosysIndex(tmp_path / "docs_nb", fetcher=FakeFetcher(), books=["tcplclib_tc2_system"], max_workers=2)
    nb.index_book("tcplclib_tc2_system")
    with nb._db() as con:
        assert not [r[0] for r in con.execute("SELECT code FROM pages") if "\xa0" in (r[0] or "")]
    assert nb.search_code("READ := TRUE")["count"] == 1
    langs = idx.code_languages()
    assert idx.search_code("fbAdsRead", language="ST")["count"] == 1 and set(langs) == {"st"}
    with idx._db() as con:                                         # the derived blocks were replaced, not duplicated
        assert con.execute("SELECT COUNT(*) FROM code_blocks").fetchone()[0] == langs["st"]
        assert con.execute("SELECT COUNT(*) FROM code_src").fetchone()[0] == con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]



def test_r24_return_value_is_the_header_from_real_help_viewer_markup():
    """R24 (0.8.9 retest, unchanged): F_GetTaskTime still showed 'VAR_OUTPUT F_GetTaskTime : ULINT'. Root: Help
    Viewer code is indented with non-breaking spaces, kept in the stored code; the 0.8.9 output-time fix matched
    ASCII blanks only. Now the code is stored with plain spaces and the parser puts the return value into the header."""
    from twincat_mcp.infosys import html_to_doc, parse_declaration, _tidy_declaration
    nb = "\u00a0"
    h = ("<html><body><h1>F_GetTaskTime</h1><p>Returns the task time.</p>"
         f"<pre>VAR_OUTPUT\n{nb * 4}F_GetTaskTime{nb}:{nb}ULINT;{nb}(*{nb}time{nb}*)\nEND_VAR</pre>"
         f"<pre>VAR_INPUT\n{nb * 4}nTask{nb}:{nb}UDINT;\nEND_VAR</pre>"
         "<table><tr><th>Name</th><th>Type</th><th>Description</th></tr>"
         "<tr><td>nTask</td><td>UDINT</td><td>task index</td></tr></table></body></html>")
    doc = html_to_doc(h)
    assert all(nb not in code for _l, code in doc.code_blocks)                  # stored without non-breaking spaces
    d = parse_declaration("F_GetTaskTime", doc)
    assert d["kind"] == "Function" and d["return_type"] == "ULINT" and d["outputs"] == []
    assert d["declaration"] == "FUNCTION F_GetTaskTime : ULINT  (* header reconstructed *)\nVAR_INPUT\n    nTask : UDINT;\nEND_VAR"
    assert d["inputs"][0]["name"] == "nTask" and d["inputs"][0]["description"] == "task index"
    # a page that has the header already: the return VAR_OUTPUT goes, other outputs stay
    h2 = ("<html><body><h1>M_Get</h1><pre>METHOD PUBLIC M_Get\nVAR_OUTPUT\n  M_Get : BOOL;\n  nErr : UDINT;\nEND_VAR</pre>"
          "</body></html>")
    d2 = parse_declaration("FB_X.M_Get", html_to_doc(h2))
    assert d2["declaration"] == "METHOD PUBLIC M_Get : BOOL\nVAR_OUTPUT\n  nErr : UDINT;\nEND_VAR"
    assert [o["name"] for o in d2["outputs"]] == ["nErr"] and d2["return_type"] == "BOOL"
    _tidy_declaration(d)
    assert "description" not in d                                              # every entry carried by an item


def test_r25_r32_declaration_tidy():
    from twincat_mcp.infosys import _tidy_declaration
    e = {"name": "E_OpenPath", "kind": "Enum",
         "declaration": "TYPE E_OpenPath :\n(\n  PATH_GENERIC := 1, (* search/open/create files in selected/generic folder *)\n"
                        "  PATH_BOOTPRJ, (* boot project folder *)\n  PATH_BOOTDATA,\n  PATH_MAX := 16#10\n);\nEND_TYPE",
         "enum_values": [{"name": "PATH_GENERIC", "value": "1"}, {"name": "PATH_BOOTPRJ", "value": None},
                         {"name": "PATH_BOOTDATA", "value": None}, {"name": "PATH_MAX", "value": "16#10"}]}
    _tidy_declaration(e)
    vals = {v["name"]: v for v in e["enum_values"]}
    assert vals["PATH_BOOTPRJ"]["value"] == "2" and vals["PATH_BOOTPRJ"]["implicit"] and vals["PATH_BOOTDATA"]["value"] == "3"
    assert vals["PATH_BOOTPRJ"]["description"] == "boot project folder" and vals["PATH_MAX"]["value"] == "16#10"


def test_r27_r28_r29_r31_infosys_queries(idx):
    idx.index_book("tcplclib_tc2_system")
    assert idx.search_symbols("%", auto_index=False)["count"] == 0                     # R27: no wildcard
    assert idx.search_symbols("ADS_EAD", auto_index=False)["count"] == 0
    assert idx.search_symbols("ADSREAD", auto_index=False)["count"] >= 1
    with idx._db() as con:                                                            # R28: help topic typed as FB
        con.execute("INSERT INTO pages(url, book, title, kind, library, markdown, text, code, fetched) VALUES "
                    "('https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/1.html', 'tcplclib_tc2_system', "
                    "'Use of function blocks', 'FunctionBlock', 'Tc2_System', '', '', '', 1)")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('pages_gen', '77')")
    assert idx.search_symbols("Use of", kind="FunctionBlock", auto_index=False)["count"] == 0
    for bad in (dict(limit=-1), dict(limit=0), dict(limit=1000)):
        with pytest.raises(InfosysError, match="limit"):
            idx.search_symbols("ADSREAD", auto_index=False, **bad)
        with pytest.raises(InfosysError, match="limit"):
            idx.search("ADSREAD", **bad)
    with pytest.raises(InfosysError, match="category 'bogus' unknown"):
        idx.books(category="bogus")
    with pytest.raises(InfosysError, match="max_pages"):
        idx.index_book("tcplclib_tc2_system", max_pages=-5)
    assert idx.index_book("Tc2_System")["book"] == "tcplclib_tc2_system"            # R31: library name accepted
    one = idx._fts_search('"read the value"', None, 10)                              # R29: phrase kept
    many = idx._fts_search("read the value", None, 10)
    assert len(one) <= len(many)


def test_r23_ambiguous_declaration_names_the_other_libraries(idx):
    """R23: MC_MoveAbsolute (Tc2_MC2 and Tc3_DriveMotionControl, different inputs) silently returned one of them."""
    import json as _j
    with idx._db() as con:
        for lib, book in (("Tc2_MC2", "tcplclib_tc2_mc2"), ("Tc3_DriveMotionControl", "tcplclib_tc3_drivemotioncontrol")):
            d = {"name": "MC_MoveAbsolute", "kind": "FunctionBlock", "declaration": "FUNCTION_BLOCK MC_MoveAbsolute",
                 "inputs": [], "outputs": [], "in_out": [], "members": [], "enum_values": [], "description": {}}
            con.execute("INSERT INTO pages(url, book, title, kind, library, markdown, text, code, decl, fetched) VALUES "
                        "(?,?,?,?,?,?,?,?,?,?)", (f"https://infosys.beckhoff.com/content/1033/{book}/1.html", book,
                                                  "MC_MoveAbsolute", "FunctionBlock", lib, "", "", "", _j.dumps(d), 1))
    # R23 (0.8.8 retest): no default — the call is refused with both libraries and their pages
    with pytest.raises(InfosysError, match=r"ambiguous: MC_MoveAbsolute is documented in 2 libraries.*Tc2_MC2.*"
                                             r"Tc3_DriveMotionControl.*nothing was chosen"):
        idx.declaration("MC_MoveAbsolute")
    r = idx.declaration("MC_MoveAbsolute", library="Tc3_DriveMotionControl")
    assert r["library"] == "Tc3_DriveMotionControl" and r["also_in"] == ["Tc2_MC2"]


def test_r32_retest_type_names_broken_across_lines_in_table_cells():
    """R32 (0.8.8 retest): 'T_Ams NetId' — a type wrapped in a narrow table column (<br>, two <p>, zero-width
    space) came out with a space. Joined when the page is converted (format 4; nothing is repaired when it is read):
    on evidence (the page's code declares the name) or in a Type column at a camel-case boundary; IEC keywords and
    prose keep their spaces. The Markdown and the table data (the declaration's source) agree."""
    from twincat_mcp.infosys import html_to_doc
    h = ("<html><body><h1>ADSREAD</h1><pre>VAR_INPUT\n NETID : T_AmsNetId;\n PORT : T_AmsPort;\nEND_VAR</pre>"
         "<table><tr><th>Name</th><th>Type</th><th>Description</th></tr>"
         "<tr><td>NETID</td><td>T_Ams<br/>NetId</td><td>AMS address as T_Ams NetId.</td></tr>"
         "<tr><td>PORT</td><td><p>T_Ams</p><p>Port</p></td><td>Input<br>Output</td></tr>"
         "<tr><td>X</td><td>ARRAY [1..2] OF<br/>BYTE</td><td>zero\u200bwidth soft\u00adhyphen</td></tr>"
         "<tr><td>Y</td><td>POINTER TO<br>BYTE</td><td>ST_My<br>Struct</td></tr>"
         "<tr><td>Z</td><td>ST_Axis<br>Ref</td><td>x a \\| b</td></tr></table></body></html>")
    doc = html_to_doc(h)
    md = doc.markdown
    rows = {r.split(" | ")[0].lstrip("| "): r for r in md.splitlines() if r.startswith("| ")}
    data = {r[0]: r for r in doc.tables[0]}
    assert data["NETID"][1] == "T_AmsNetId" and data["PORT"][1] == "T_AmsPort" and data["Z"][1] == "ST_AxisRef"
    assert rows["NETID"] == "| NETID | T_AmsNetId | AMS address as T_AmsNetId. |"
    assert rows["PORT"] == "| PORT | T_AmsPort | Input Output |"                  # prose keeps its break as a space
    assert rows["X"] == "| X | ARRAY [1..2] OF BYTE | zerowidth softhyphen |"
    assert rows["Y"] == "| Y | POINTER TO BYTE | ST_My Struct |"                  # no evidence outside a Type column
    assert rows["Z"].startswith("| Z | ST_AxisRef |") and "\\|" in rows["Z"]      # Type column; escaped pipe kept
    assert rows["Name"] == "| Name | Type | Description |"


def test_n74_methods_and_properties_are_their_own_lists_not_descriptions():
    """N74: FB_WG_ComboFilter's Methods (Name | Definition location | Description) and Properties (… | Access | …)
    tables were read as variable descriptions — 'description' carried 'Configure()', 'Call()', 'Reset()' and the
    properties. They are `methods` / `properties` now; `description` keeps only what no item carries."""
    from twincat_mcp.infosys import html_to_doc, parse_declaration, _tidy_declaration

    def table(head, *rows):
        return ("<table><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>"
                + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>")
    h = ("<html><body><h1>FB_WG_ComboFilter</h1><p>PTn, moving average and notch filter in series.</p>"
         "<pre>FUNCTION_BLOCK FB_WG_ComboFilter\nVAR_INPUT\n    stConfig : ST_WG_ComboFilter;\nEND_VAR\nVAR_OUTPUT\n"
         "    bError : BOOL;\n    bConfigured : BOOL;\nEND_VAR</pre>"
         + table(["Name", "Type", "Description"], ["stConfig", "ST_WG_ComboFilter", "Structure for configuring"])
         + table(["Name", "Type", "Description"], ["bError", "BOOL", "TRUE, if an error occurs."],
                 ["bConfigured", "BOOL", "TRUE if the configuration was successful."])
         + table(["Name", "Definition location", "Description"], ["Configure()", "Local", "Loads a new configuration."],
                 ["Call()", "Local", "Calculates the output signal."], ["Reset()", "Local", "Resets internal states."])
         + table(["Name", "Type", "Access", "Definition location", "Initial value", "Description"],
                 ["bTraceLevelDefault", "BOOL", "Get, Set", "Local", "TRUE", "TRUE, if eTraceLevel = Warning."])
         + "</body></html>")
    d = parse_declaration("FB_WG_ComboFilter", html_to_doc(h))
    assert [m["name"] for m in d["methods"]] == ["Configure", "Call", "Reset"], d.get("methods")
    assert d["methods"][2] == {"name": "Reset", "definition_location": "Local", "description": "Resets internal states."}
    assert d["properties"] == [{"name": "bTraceLevelDefault", "type": "BOOL", "access": "Get, Set",
                                "definition_location": "Local", "initial_value": "TRUE",
                                "description": "TRUE, if eTraceLevel = Warning."}]
    assert d["outputs"][0]["description"] == "TRUE, if an error occurs."
    _tidy_declaration(d)
    assert "description" not in d, d.get("description")          # no method or property list left in it


def test_n77_table_cells_are_plain_text_and_the_type_page_is_type_ref():
    """N77: a type cell that linked its type came out as '[ST_WG_Config](https://…/123.html)' in `type` — the name
    and the link in one string. A cell is plain text (rule 4: the index stores it so); the type's page, when the cell
    links one, is `type_ref` — for variables and for properties."""
    from twincat_mcp.infosys import html_to_doc, parse_declaration
    base = "https://infosys.beckhoff.com/content/1033/tcplclib_tc3_waveform/100.html"
    h = ("<html><body><h1>FB_WG_Filter</h1><pre>FUNCTION_BLOCK FB_WG_Filter\nVAR_INPUT\n    stConfig : ST_WG_Config;\n"
         "    nCount : UDINT;\nEND_VAR</pre>"
         "<table><tr><th>Name</th><th>Type</th><th>Description</th></tr>"
         "<tr><td>stConfig</td><td><a href='200.html'>ST_WG_Config</a></td><td>Configuration, see <a href='200.html'>"
         "ST_WG_Config</a>.</td></tr><tr><td>nCount</td><td>UDINT</td><td>Number of samples</td></tr></table>"
         "<h2>Properties</h2><table><tr><th>Name</th><th>Type</th><th>Access</th><th>Description</th></tr>"
         "<tr><td>eTraceLevel</td><td><a href='../tcplclib_tc3_eventlogger/1033/300.html'>TcEventSeverity</a></td>"
         "<td>Get, Set</td><td>Trace level</td></tr></table></body></html>")
    doc = html_to_doc(h, base, package=True)
    d = parse_declaration("FB_WG_Filter", doc)
    C = "https://infosys.beckhoff.com/content/1033/"
    st = next(i for i in d["inputs"] if i["name"] == "stConfig")
    assert st["type"] == "ST_WG_Config" and st["type_ref"] == C + "tcplclib_tc3_waveform/200.html", st
    assert st["description"] == "Configuration, see ST_WG_Config."                  # a cell: plain text
    assert "type_ref" not in next(i for i in d["inputs"] if i["name"] == "nCount")
    assert d["properties"] == [{"name": "eTraceLevel", "type": "TcEventSeverity", "access": "Get, Set",
                                "definition_location": None, "initial_value": None, "description": "Trace level",
                                "type_ref": C + "tcplclib_tc3_eventlogger/300.html"}]
    assert all("](" not in c for row in doc.tables[0] for c in row)
    # the page text keeps the links (Markdown), the cells do not
    assert f"[ST_WG_Config]({C}tcplclib_tc3_waveform/200.html)" in doc.markdown


def test_n74_interface_page_methods_by_their_heading():
    """Review: an interface page (I_TcEventBase) lists its methods as 'Name | Description' with no parentheses and no
    Definition location — the header alone looks like a variable table. The heading above it says 'Methods'."""
    from twincat_mcp.infosys import html_to_doc, parse_declaration, _tidy_declaration
    h = ("<html><body><h1>I_TcEventBase</h1><pre>INTERFACE I_TcEventBase</pre>"
         "<h2>Methods</h2><table><tr><th>Name</th><th>Description</th></tr>"
         "<tr><td>EqualsTo</td><td>Compares the instance with another.</td></tr>"
         "<tr><td>GetText</td><td>Returns the text of the event.</td></tr></table>"
         "<h2>Properties</h2><table><tr><th>Name</th><th>Type</th><th>Access</th><th>Description</th></tr>"
         "<tr><td>eSeverity</td><td>TcEventSeverity</td><td>Get</td><td>Severity</td></tr></table>"
         "</body></html>")
    doc = html_to_doc(h)
    assert doc.table_titles == ["Methods", "Properties"]
    d = parse_declaration("I_TcEventBase", doc)
    assert [m["name"] for m in d["methods"]] == ["EqualsTo", "GetText"], d
    assert d["properties"][0]["name"] == "eSeverity" and d["properties"][0]["access"] == "Get"
    _tidy_declaration(d)
    assert "description" not in d, d.get("description")


def _page(title, body):
    from twincat_mcp.infosys import html_to_doc
    return html_to_doc(f"<html><body><h1>{title}</h1>{body}</body></html>")


_TBL = ("<table><tr><th>Name</th><th>Type</th><th>Description</th></tr>"
        "<tr><td>{n}</td><td>{t}</td><td>{d}</td></tr></table>")


def test_n103_a_function_whose_header_is_a_heading(monkeypatch):
    """N103 (S2, 0.8.11 retest): Beckhoff writes many function headers as a heading — '## FUNCTION MEMCPY : UDINT',
    '## MEMSET FUNCTION: UDINT' — or tables the return value only ('Return value: LREAL_TO_FMTSTR | STRING(510)').
    They were indexed as function blocks with 'FUNCTION_BLOCK MEMCPY (* header reconstructed *)' and no return type:
    code written from the answer instantiated a function as an FB. (The bodies are those of the real pages.)"""
    from twincat_mcp.infosys import parse_declaration
    inputs = "<h2>Inputs</h2><pre>VAR_INPUT\n    destAddr : PVOID;\n    srcAddr  : PVOID;\n    n        : UDINT;\nEND_VAR</pre>"
    d = parse_declaration("MEMCPY", _page("MEMCPY", "<p>The function MEMCPY can be used to copy …</p>"
                                                  "<h2>FUNCTION MEMCPY : UDINT</h2>" + inputs))
    assert (d["kind"], d["return_type"]) == ("Function", "UDINT")
    assert d["declaration"].startswith("FUNCTION MEMCPY : UDINT  (* header reconstructed *)\nVAR_INPUT"), d["declaration"]
    d = parse_declaration("MEMSET", _page("MEMSET", "<h2>MEMSET FUNCTION: UDINT</h2>" + inputs))
    assert (d["kind"], d["return_type"]) == ("Function", "UDINT")
    d = parse_declaration("SYSTEMTIME_TO_STRING", _page("SYSTEMTIME_TO_STRING",
                          "<h2>FUNCTION SYSTEMTIME_TO_STRING : STRING(24)</h2>"
                          "<pre>VAR_INPUT\n    in : TIMESTRUCT;\nEND_VAR</pre>"))
    assert (d["kind"], d["return_type"]) == ("Function", "STRING(24)")
    d = parse_declaration("F_ToASC", _page("F_ToASC", "<h2>FUNCTION F_ToASC : BYTE</h2>"
                                                    "<pre>VAR_INPUT\n    str : STRING;\nEND_VAR</pre>"))
    assert (d["kind"], d["return_type"]) == ("Function", "BYTE")
    d = parse_declaration("LREAL_TO_FMTSTR", _page("LREAL_TO_FMTSTR",
                          "<p>The function converts and formats a floating-point number …</p><h2>Return value</h2>"
                          + _TBL.format(n="LREAL_TO_FMTSTR", t="STRING(510)", d="")
                          + "<h2>Inputs</h2><pre>VAR_INPUT\n    in : LREAL;\n    iPrecision : INT;\nEND_VAR</pre>"))
    assert (d["kind"], d["return_type"]) == ("Function", "STRING(510)") and "kind_note" not in d
    assert d["declaration"].startswith("FUNCTION LREAL_TO_FMTSTR : STRING(510)")
    # a return value and nothing that says function or method: a function, and the answer says it is not certain
    d = parse_declaration("LrealIsFinite", _page("LrealIsFinite", "<h2>Return value</h2>"
                          + _TBL.format(n="LrealIsFinite", t="BOOL", d="")
                          + "<h2>Inputs</h2><pre>VAR_INPUT\n    x : REFERENCE TO LREAL;\nEND_VAR</pre>"))
    assert d["kind"] == "Function" and d["return_type"] == "BOOL" and "does not say which" in d["kind_note"]
    # a method page that says so, and one whose own code names it differently (a doc error)
    d = parse_declaration("AddFilter", _page("AddFilter", "<p>Method for adding a filter.</p><h2>Return values</h2>"
                          + _TBL.format(n="AddFilter", t="HRESULT", d="S_OK")))
    assert (d["kind"], d["return_type"]) == ("Method", "HRESULT")
    d = parse_declaration("SendMessageEx2", _page("SendMessageEx2", "<p>This method sends a message.</p>"
                          "<pre>METHOD SendMessageEx : HRESULT\nVAR_INPUT\n    nTimeStamp : ULINT;\nEND_VAR</pre>"
                          "<h2>Return value</h2>" + _TBL.format(n="SendMessageEx2", t="HRESULT", d="")))
    assert d["kind"] == "Method" and d["return_type"] == "HRESULT"
    # a title with the keyword in it
    d = parse_declaration("FUNCTION F_GetVersionTcUtilities", _page("FUNCTION F_GetVersionTcUtilities",
                          "<pre>VAR_INPUT\n    nVersionElement : INT;\nEND_VAR</pre><h2>Return value</h2>"
                          + _TBL.format(n="F_GetVersionTcUtilities", t="UINT", d="")))
    assert (d["name"], d["kind"], d["return_type"]) == ("F_GetVersionTcUtilities", "Function", "UINT")
    # a real function block stays one
    d = parse_declaration("FB_FileOpen", _page("FB_FileOpen", "<pre>VAR_INPUT\n    sNetId : T_AmsNetId;\nEND_VAR</pre>"))
    assert d["kind"] == "FunctionBlock" and d["return_type"] is None


def test_n101_a_page_about_a_pragma_documents_no_symbol():
    """N101 (0.8.11 retest): infosys_get_page of "Attribute 'linkalways'" said kind GlobalVariable (its example is a
    VAR_GLOBAL list); manual pages ('Transmitting structures to the PLC') were Structs, .NET pages Aliases (C#
    'Type type' read as TYPE). A title that is a sentence documents no IEC symbol; IEC headers are in capitals."""
    from twincat_mcp.infosys import classify, page_symbols, parse_declaration
    d = parse_declaration("Attribute 'linkalways'", _page("Attribute 'linkalways'",
                          "<pre>{attribute 'linkalways'}\nVAR_GLOBAL\n    nVar : INT;\nEND_VAR</pre>"))
    assert d["kind"] == "Page" and d["members"] == [] and d["declaration"] is None
    assert classify("Transmitting structures to the PLC", [("st", "TYPE PLCStruct :\nSTRUCT\n  a : INT;\nEND_STRUCT\nEND_TYPE")]) == "Page"
    assert classify("AdsClient.ReadAny Method (UInt32, Type)", [("csharp", "public object ReadAny(uint indexGroup, Type type)\n{\n}")]) == "Page"
    assert classify("Global_Constants", [("st", "VAR_GLOBAL CONSTANT\n    nMax : INT := 10;\nEND_VAR")]) == "GlobalVariable"
    # the type of a type page, also under another name in its code (a doc error: T_DBConfig_Odbc shows TYPE T_DBConfig_NET_MySQL)
    assert classify("T_DBConfig_Odbc", [("st", "TYPE T_DBConfig_NET_MySQL\nSTRUCT\n  n : UINT;\nEND_STRUCT\nEND_TYPE")]) == "Struct"
    # IEC code marked cpp by the page is still IEC: the header is in capitals
    assert classify("Release", [("cpp", "METHOD Release : HRESULT")]) == "Method"
    assert page_symbols("F_GetCycleTime()") == {"f_getcycletime"} and page_symbols("GetHMI (4.1)") == {"gethmi"}
    assert page_symbols("Data type __SYSTEM.ExceptionCode") == {"exceptioncode"}
    assert page_symbols("KL6inData, KL6outData") == {"kl6indata", "kl6outdata"} and page_symbols("Static Analysis Light") == set()


def test_n100_one_page_in_two_books_is_one_answer_and_every_reader_agrees(idx):
    """N100 (0.8.11 retest): the skill's docs_declaration called T_MaxString ambiguous — page 31062795 is in
    tcplclib_tc2_system and in tf6510_tc3_iec61850 — and ADSREAD ambiguous with the C++ AdsRead page, while the plugin
    answered both; the plugin in turn took the first of two different pages of one library (two methods IsBase64) and
    counted Tc2_EtherCAT / Tc2_Ethercat as two libraries. One rule now (infosys.symbol_documents)."""
    import json as _j
    from twincat_mcp.infosys import symbol_documents
    C = "https://infosys.beckhoff.com/content/1033/"

    def put(book, pid, title, kind, lib, declaration):
        d = {"name": title, "kind": kind, "declaration": declaration, "inputs": [], "outputs": [], "in_out": [],
             "members": [], "enum_values": [], "description": {}}
        with idx._db() as con:
            con.execute("INSERT INTO pages(url, book, page_id, title, kind, library, markdown, text, code, decl, fetched) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (f"{C}{book}/{pid}.html", book, pid, title, kind, lib, "", "",
                                                         "", _j.dumps(d) if kind != "Page" else None, 1))
    put("tcplclib_tc2_system", "31062795", "T_MaxString", "Alias", "Tc2_System", "TYPE T_MaxString : STRING(MAX_STRING_LENGTH); END_TYPE")
    put("tf6510_tc3_iec61850", "31062795", "T_MaxString", "Alias", "Tc2_System", "TYPE T_MaxString : STRING(MAX_STRING_LENGTH); END_TYPE")
    d = idx.declaration("T_MaxString")
    assert d["url"] == f"{C}tcplclib_tc2_system/31062795.html" and d["same_page_in"] == ["tf6510_tc3_iec61850"]
    assert "also_in" not in d
    # the C++ API's AdsRead documents no IEC symbol: ADSREAD is the FB
    put("tcplclib_tc2_system", "30866571", "ADSREAD", "FunctionBlock", "Tc2_System", "FUNCTION_BLOCK ADSREAD")
    put("tc3_c", "743169291", "AdsRead", "Page", None, None)
    assert idx.declaration("ADSREAD")["url"] == f"{C}tcplclib_tc2_system/30866571.html"
    assert idx.get_page("ADSREAD")["url"] == f"{C}tcplclib_tc2_system/30866571.html"
    # one library, spelt two ways by its books, the same declaration: one answer
    put("tcplclib_tc2_ethercat", "1", "FB_EcGetSlaveState", "FunctionBlock", "Tc2_EtherCAT", "FUNCTION_BLOCK FB_EcGetSlaveState VAR_INPUT sNetId : T_AmsNetId; END_VAR")
    put("tf6250_tc3_modbus", "2", "FB_EcGetSlaveState", "FunctionBlock", "Tc2_Ethercat", "FUNCTION_BLOCK FB_EcGetSlaveState VAR_INPUT sNetId : T_AmsNetId; END_VAR")
    d = idx.declaration("FB_EcGetSlaveState")
    assert d["library"] == "Tc2_EtherCAT" and d["same_page_in"] == ["tf6250_tc3_modbus"] and "also_in" not in d
    # one library, two different methods of one name (two FBs): nothing chosen, even with library=
    put("tcplclib_tc3_jsonxml", "4220443531", "IsBase64", "Method", "Tc3_JsonXml", "METHOD IsBase64 : BOOL VAR_INPUT v : SJsonValue; END_VAR")
    put("tcplclib_tc3_jsonxml", "4220804235", "IsBase64", "Method", "Tc3_JsonXml", "METHOD IsBase64 : BOOL VAR_INPUT n : POINTER TO BYTE; END_VAR")
    for lib in (None, "Tc3_JsonXml"):
        with pytest.raises(InfosysError, match=r"ambiguous: IsBase64 is documented by 2 different pages.*infosys_get_page"):
            idx.declaration("IsBase64", library=lib)
    # a title that writes the keyword or the call: the same symbol
    put("tcplclib_tc2_utilities", "99", "FUNCTION F_GetVersionTcUtilities", "Function", "Tc2_Utilities", "FUNCTION F_GetVersionTcUtilities : UINT")
    assert idx.declaration("F_GetVersionTcUtilities")["url"] == f"{C}tcplclib_tc2_utilities/99.html"
    with idx._db() as con:
        assert [[r["book"] for r in g] for g in symbol_documents(con, "t_maxstring")] == [["tcplclib_tc2_system", "tf6510_tc3_iec61850"]]
