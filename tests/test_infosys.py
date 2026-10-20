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
    assert idx.get_page("ADSREAD", max_chars=200)["truncated"] is True          # N160: 200-60000, as the skill


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
    for bad in (dict(limit=-1), dict(limit=0)):
        with pytest.raises(InfosysError, match="limit"):
            idx.search("ADSREAD", **bad)
    # N167 (0.8.14 retest): a search's limit above 50 is used as 50 and said so — refusing it left hits 51… unreachable
    r = idx.search("ADSREAD", limit=1000)
    assert r["clamped"] is True and r["clamped_note"].startswith("limit 1000 is above the maximum (1-50): 50 used"), r
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
    # review of 0.8.14: two FBs' methods whose page ids share a prefix (5002755467_60885635 / _11099805) stay two
    put("tcplclib_tc3_eventlogger", "5002755467_60885635", "EqualsTo", "Method", "Tc3_EventLogger",
        "METHOD EqualsTo : BOOL VAR_INPUT ipOther : I_TcEventBase; END_VAR")
    put("tcplclib_tc3_eventlogger", "5002755467_11099805", "EqualsTo", "Method", "Tc3_EventLogger",
        "METHOD EqualsTo : BOOL VAR_INPUT ipOther : I_TcEventBase; END_VAR")
    with idx._db() as con:
        assert len(symbol_documents(con, "equalsto")) == 2
    # a title that writes the keyword or the call: the same symbol
    put("tcplclib_tc2_utilities", "99", "FUNCTION F_GetVersionTcUtilities", "Function", "Tc2_Utilities", "FUNCTION F_GetVersionTcUtilities : UINT")
    assert idx.declaration("F_GetVersionTcUtilities")["url"] == f"{C}tcplclib_tc2_utilities/99.html"
    with idx._db() as con:
        assert [[r["book"] for r in g] for g in symbol_documents(con, "t_maxstring")] == [["tcplclib_tc2_system", "tf6510_tc3_iec61850"]]


def test_review_0812_a_web_answer_carries_no_fts_counts(idx, monkeypatch):
    """Review 0.8.12: when the web fallback answered, total / query_mode described the empty local search."""
    r = idx.search("technology data SAF")
    assert r["backend"] == "duckduckgo+sitemap" and "total" not in r and "query_mode" not in r, r


def test_kbv_book_slash_id_for_ids_that_are_not_numbers(kb_copy):
    """kb_verify on the real index (0.8.13): infosys_get_page('aml_dataexchange/index') and
    ('tcplclib_tc3_eventlogger/13785931531_21345065') were refused ("neither an Infosys URL, 'book/id' …") — 'book/id'
    was read with digits only, and 1169 of the 51,655 pages have another id. A 'book/id' whose id is not a number is a
    page when the index has it; otherwise it is a title with a slash, as before."""
    import sqlite3
    from twincat_mcp.infosys import InfosysIndex
    db = kb_copy / "index" / "infosys-1033.db"
    old = "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31059723.html"
    new = "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/index.html"
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE pages SET url = ?, page_id = 'index' WHERE url = ?", (new, old))
        con.execute("UPDATE pages SET title = 'Send/Receive' WHERE title = 'ADSWRITE'")
        con.execute("UPDATE pages SET url = replace(url, '30868043', 'beckhoff-0cad-46d4'), page_id = 'beckhoff-0cad-46d4'"
                    " WHERE title = 'MEMCPY'")
    con.close()
    ix = InfosysIndex(db_file=db, read_only=True, web_fallback=False)
    assert ix.get_page("tcplclib_tc2_system/index", max_chars=200)["title"] == "T_AmsNetID"
    assert ix.get_page("tcplclib_tc2_system/index.html", max_chars=200)["url"] == new
    assert ix.get_page("Send/Receive", max_chars=200)["title"] == "Send/Receive"       # a title with a slash
    assert ix.get_page("tcplclib_tc2_system/beckhoff-0cad-46d4", max_chars=200)["title"] == "MEMCPY"
    # book slugs with '.' and '-' (tc3_ads.net, tf5110-tf5113_tc3_kinematic_transformation) were refused too
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE pages SET url = replace(url, '/tcplclib_tc2_utilities/', '/tc3_ads.net/'), "
                    "book = 'tc3_ads.net' WHERE book = 'tcplclib_tc2_utilities'")
    con.close()
    url = InfosysIndex(db_file=db, read_only=True, web_fallback=False).get_page("tc3_ads.net/35409035")["url"]
    assert url == "https://infosys.beckhoff.com/content/1033/tc3_ads.net/35409035.html"


def _store_with(idx, ref, title, body, help_id, parent=None):
    d = _page(title, body)
    d.meta = {"Microsoft.Help.Id": help_id, **({"Microsoft.Help.TOCParent": parent} if parent else {})}
    with idx._db() as con:
        idx._store(con, f"https://infosys.beckhoff.com/content/1033/{ref}.html", "", d)


_CALL = ("<p>Calls the filter.</p><pre>METHOD Call : BOOL\nVAR_INPUT\n    pIn : PVOID;\n    nSizeIn : UDINT;\n"
         "{more}END_VAR</pre>")


def test_n126_an_fbs_methods_are_tied_to_it_through_the_contents(idx):
    """N126 (0.8.13 retest, S3): Beckhoff titles a method page with its bare name — 'Call' of FB_WG_ComboFilter and
    'Call' of FB_WG_Weighing could not be told apart, and the FB's page linked only one of its methods. Format 6 stores
    each page's parent in the Help Viewer contents (Microsoft.Help.TOCParent): the FB lists its children, a method
    names its FB, and 'FB_WG_ComboFilter.Call' finds the Call below FB_WG_ComboFilter."""
    assert inf.INDEXER_VERSION >= 6                             # format 6 stores the parents (7 keeps them)
    W = "tf3685_tc3_weighing_library"
    _store_with(idx, f"{W}/15084337291", "FB_WG_ComboFilter", "<pre>FUNCTION_BLOCK FB_WG_ComboFilter\nVAR_INPUT\n"
                "    stConfig : ST_WG_ComboFilter;\nEND_VAR</pre>", "beckhoff-combo")
    _store_with(idx, f"{W}/15084337299", "FB_WG_Weighing", "<pre>FUNCTION_BLOCK FB_WG_Weighing\nVAR_INPUT\n"
                "    bEnable : BOOL;\nEND_VAR</pre>", "beckhoff-weigh")
    _store_with(idx, f"{W}/15098183435_25253189", "Call", _CALL.format(more="    pOut : PVOID;\n"), "beckhoff-call-1",
                parent="beckhoff-combo")
    _store_with(idx, f"{W}/15098183435_25253190", "Reset", "<pre>METHOD Reset : BOOL</pre>", "beckhoff-reset-1",
                parent="beckhoff-combo")
    _store_with(idx, f"{W}/15112951947", "Call", _CALL.format(more=""), "beckhoff-call-2", parent="beckhoff-weigh")
    r = idx.resolve_help_links()
    assert r["parents"] == 3
    C = "https://infosys.beckhoff.com/content/1033/"
    fb = idx.get_page(f"{W}/15084337291")
    assert [(c["title"], c["url"]) for c in fb["children"]] == [("Call", f"{C}{W}/15098183435_25253189.html"),
                                                                 ("Reset", f"{C}{W}/15098183435_25253190.html")]
    assert idx.get_page(f"{W}/15112951947")["parent"] == {"title": "FB_WG_Weighing", "url": f"{C}{W}/15084337299.html"}
    d = idx.declaration("FB_WG_ComboFilter.Call")
    assert d["url"] == f"{C}{W}/15098183435_25253189.html" and d["parent"]["title"] == "FB_WG_ComboFilter"
    assert [x["name"] for x in d["inputs"]] == ["pIn", "nSizeIn", "pOut"]
    assert idx.declaration("FB_WG_Weighing.Call")["url"] == f"{C}{W}/15112951947.html"
    with pytest.raises(InfosysError, match="ambiguous"):
        idx.declaration("Call")                                   # the bare name: still two answers, nothing picked


def test_kbv_i13_a_member_page_titled_with_brackets_or_a_version(idx):
    """kb_verify I13 (0.8.14, format 6): 'FB_CammingLookUp.LookUp', 'FB_AppMessage.SendAlarm', 'FB_TrendHmi.Append'
    were not found — Beckhoff titles those method pages 'LookUp()', 'SendAlarm()', 'Append()' and only a page titled
    exactly 'LookUp' was looked for below the FB. 'FB_Module.GetHMI' answered the 'GetHMI' page alone: the
    'GetHMI (4.1)' page below the other FB_Module page (tf3550) was never a candidate — two pages, nothing is chosen."""
    P = "tf8560_plastic_technology_functions"
    _store_with(idx, f"{P}/16232286603", "FB_CammingLookUp", "<pre>FUNCTION_BLOCK FB_CammingLookUp\nVAR_INPUT\n"
                "    bEnable : BOOL;\nEND_VAR</pre>", "camming")
    _store_with(idx, f"{P}/16233574155", "LookUp()", "<pre>METHOD LookUp : BOOL\nVAR_INPUT\n    fMaster : LREAL;\n"
                "END_VAR</pre>", "camming-lookup", parent="camming")
    A = "tf3550_tc3_analytics_runtime"
    gethmi = "<pre>METHOD GetHMI : BOOL\nVAR_INPUT\n    nContent : DINT;\n    pContent : PVOID;\nEND_VAR</pre>"
    _store_with(idx, f"{A}/10356579979", "FB_Module", "<pre>FUNCTION_BLOCK FB_Module\nVAR_INPUT\n    nOld : INT;\n"
                "END_VAR</pre>", "module-41")
    _store_with(idx, f"{A}/14385293579", "FB_Module", "<pre>FUNCTION_BLOCK FB_Module\nVAR_INPUT\n    nNew : INT;\n"
                "END_VAR</pre>", "module")
    _store_with(idx, f"{A}/14383837835", "GetHMI (4.1)", gethmi, "gethmi-41", parent="module-41")
    _store_with(idx, f"{A}/14385303307", "GetHMI", gethmi, "gethmi", parent="module")
    _store_with(idx, f"{A}/14385303399", "FB_Other.Run()", "<pre>METHOD Run : BOOL</pre>", "other-run")
    idx.resolve_help_links()
    C = "https://infosys.beckhoff.com/content/1033/"
    d = idx.declaration("FB_CammingLookUp.LookUp")
    assert d["url"] == f"{C}{P}/16233574155.html" and d["parent"]["title"] == "FB_CammingLookUp", d
    with pytest.raises(InfosysError, match="ambiguous") as e:
        idx.declaration("FB_Module.GetHMI")
    assert f"{A}/14383837835" in str(e.value) and f"{A}/14385303307" in str(e.value)
    assert idx.declaration("FB_Other.Run")["url"] == f"{C}{A}/14385303399.html"     # a dotted title with brackets


_ADDKEY = "<pre>METHOD AddKey\nVAR_IN_OUT CONSTANT\n    key : STRING;\nEND_VAR</pre>"
_JSON_MEMBERS = {   # book: (FB_JsonSaxWriter page, its AddKey, FB_JsonSaxPrettyWriter page, its AddKey) — real page ids
    "tf6701_tc3_iot_communication_mqtt": ("4220481035_1", "4220504971_62687", "4220481035_2", "4220504971_564191"),
    "tf6760_tc3_iot_https_rest": ("4220481035_3", "4220504971_2530563", "4220481035_4", "4220504971_22775067"),
    "tcplclib_tc3_jsonxml": ("4220481035", "4220504971_57616766", "4220481099", "4220504971_48788849"),
}


def store_json_members(store) -> None:
    """Two FBs with an AddKey method of one declaration, in 3 books (the 6 real 'AddKey' pages of the index): in the
    product books' order the PrettyWriter's tf6760 copy comes right after the SaxWriter's tf6701 copy."""
    order = [("tf6701_tc3_iot_communication_mqtt", 0), ("tf6760_tc3_iot_https_rest", 2), ("tcplclib_tc3_jsonxml", 0),
             ("tf6701_tc3_iot_communication_mqtt", 2), ("tf6760_tc3_iot_https_rest", 0), ("tcplclib_tc3_jsonxml", 2)]
    for book, i in order:
        fb = ("FB_JsonSaxWriter", "FB_JsonSaxPrettyWriter")[i // 2]
        pids = _JSON_MEMBERS[book]
        store(f"{book}/{pids[i]}", fb, f"<pre>FUNCTION_BLOCK {fb}\nVAR_INPUT\n    bX : BOOL;\nEND_VAR</pre>",
              f"{book}-{fb}")
        store(f"{book}/{pids[i + 1]}", "AddKey", _ADDKEY, f"{book}-{fb}-addkey", f"{book}-{fb}")


def test_kbv_i13_methods_of_two_fbs_are_never_one_document(idx):
    """kb_verify I13 (0.8.14, format 6): FB_JsonSaxWriter.AddKey and FB_JsonSaxPrettyWriter.AddKey have one
    declaration, and each is in 3 books. The copies were grouped by library and declaration only — the PrettyWriter's
    tf6760 page was taken as the SaxWriter's copy (also_in / same_document, the documents of 'AddKey'). Member pages
    below different symbols are two answers; a member is the same document only below the same symbol."""
    store_json_members(lambda ref, title, body, hid, parent=None: _store_with(idx, ref, title, body, hid, parent))
    idx.resolve_help_links()
    with idx._db() as con:
        docs = inf.symbol_documents(con, "AddKey")
        owners = [{con.execute("SELECT q.title FROM pages p JOIN pages q ON q.url = p.parent WHERE p.rowid = ?",
                               (r["rid"],)).fetchone()[0] for r in g} for g in docs]
    assert sorted(len(g) for g in docs) == [3, 3] and all(len(o) == 1 for o in owners), owners
    C = "https://infosys.beckhoff.com/content/1033/"
    d = idx.declaration("FB_JsonSaxPrettyWriter.AddKey")
    assert d["url"] == f"{C}tcplclib_tc3_jsonxml/4220504971_48788849.html"
    assert sorted(d["same_page_in"]) == ["tf6701_tc3_iot_communication_mqtt", "tf6760_tc3_iot_https_rest"]


def test_review_0815_a_members_owner_above_a_topic_page_and_by_its_own_name(idx):
    """Review: a member below a topic page ('Methods') had no owner and joined the first group of its declaration —
    FB_B's copy taken for FB_A's; an owner titled 'Tc3_Foo.FB_A' was another owner than 'FB_A' (copies split). The
    owner is the first symbol page above topic pages, by its own name; FB_B.Run finds the page below 'Methods'."""
    run = "<pre>METHOD Run : BOOL\nVAR_INPUT\n    bGo : BOOL;\nEND_VAR</pre>"
    _store_with(idx, "tf1000_book_one/100", "FB_A", "<pre>FUNCTION_BLOCK FB_A\nVAR_INPUT\n    a : INT;\nEND_VAR</pre>", "a1")
    _store_with(idx, "tf1000_book_one/101", "Run", run, "a1-run", parent="a1")
    _store_with(idx, "tf3000_book_three/300", "FB_B", "<pre>FUNCTION_BLOCK FB_B\nVAR_INPUT\n    b : INT;\nEND_VAR</pre>", "b3")
    _store_with(idx, "tf3000_book_three/301", "Run", run, "b3-run", parent="b3")
    _store_with(idx, "tf2000_book_two/200", "FB_B", "<pre>FUNCTION_BLOCK FB_B\nVAR_INPUT\n    b : INT;\nEND_VAR</pre>", "b2")
    _store_with(idx, "tf2000_book_two/201", "Methods", "<p>The methods of FB_B.</p>", "b2-methods", parent="b2")
    _store_with(idx, "tf2000_book_two/202", "Run", run, "b2-run", parent="b2-methods")
    _store_with(idx, "tf4000_book_four/400", "Tc3_Foo.FB_A", "<pre>FUNCTION_BLOCK FB_A\nVAR_INPUT\n    a : INT;\nEND_VAR</pre>",
                "a4")
    _store_with(idx, "tf4000_book_four/101", "Run", run, "a4-run", parent="a4")
    idx.resolve_help_links()
    with idx._db() as con:
        docs = inf.symbol_documents(con, "Run")
    got = sorted(sorted(f"{r['book']}/{r['pid']}" for r in g) for g in docs)
    assert got == [["tf1000_book_one/101", "tf4000_book_four/101"],
                   ["tf2000_book_two/202", "tf3000_book_three/301"]], got
    C = "https://infosys.beckhoff.com/content/1033/"
    d = idx.declaration("FB_B.Run")
    assert d["url"] in (f"{C}tf2000_book_two/202.html", f"{C}tf3000_book_three/301.html") and len(d["same_page_in"]) == 1
    assert idx.declaration("FB_A.Run")["url"] in (f"{C}tf1000_book_one/101.html", f"{C}tf4000_book_four/101.html")


def _store_pages(idx, pages):
    with idx._db() as con:
        for ref, title, body in pages:
            idx._store(con, f"https://infosys.beckhoff.com/content/1033/{ref}.html", "", _page(title, body))


def test_n148_n149_iec_keywords_and_operators_are_searchable(idx):
    """0.8.14 retest §34d. N148: IEC keywords were dropped as English stop words — 'THIS pointer' ignored THIS (the
    page THIS unreachable by search), 'FOR loop BY step' ranked the CNC '$FOR' page first. A word written in capitals
    is a keyword or an acronym of the documentation (FOR, BY, THIS, ST, AL), never a stop word or a too-short word.
    N149: 'THIS^ SUPER^' (a phrase) found nothing and nothing else was tried; 'REF=' found AXIS_REF … instead of the
    page of the operator REF=. A page whose title holds the operator comes first; a query whose punctuation finds
    nothing is searched as its words."""
    P = "tc3_plc_intro"
    _store_pages(idx, [
        (f"{P}/2528843147", "THIS", "<p>THIS is a special variable: a pointer to the own function block instance. "
                                    "Use THIS^ to dereference it.</p>"),
        (f"{P}/2528837771", "SUPER", "<p>SUPER is a pointer to the function block instance of the basic function block "
                                     "(SUPER^).</p>"),
        (f"{P}/1000000001", "POINTER TO", "<p>A pointer holds the address of a variable; this pointer type …</p>"),
        (f"{P}/2528897931", "ST instruction FOR", "<p>The FOR loop: FOR nCounter := 1 TO 10 BY 2 DO … END_FOR. The step "
                                                  "size is given BY the increment.</p>"),
        ("tf5200_cnc/1000000002", "Counting loop ($FOR)", "<p>The counting loop $FOR repeats a block; the loop counter "
                                                         "runs step by step up to the end value. FOR loop of the CNC.</p>"),
        (f"{P}/4978163979", "Assignment operator REF=", "<p>REF= assigns a reference to a variable.</p>"),
        ("tcplclib_tc2_mc2/1000000003", "AXIS_REF", "<p>AXIS_REF is the axis reference (REF) of every motion FB.</p>"),
        (f"{P}/1000000004", "Set operator S=", "<p>S= sets a BOOL variable: set and reset assignment.</p>"),
        (f"{P}/1000000005", "Reset operator R=", "<p>R= resets a BOOL variable: set and reset assignment.</p>"),
    ])
    idx.sync_derived()
    first = lambda q: [i["title"] for i in idx.search(q, limit=5)["items"]]           # noqa: E731
    assert first("THIS pointer")[0] == "THIS", first("THIS pointer")
    assert first("FOR loop BY step")[0] == "ST instruction FOR", first("FOR loop BY step")
    assert first("REF=")[0] == "Assignment operator REF=", first("REF=")
    got = first("THIS^ SUPER^")
    assert {"THIS", "SUPER"} <= set(got[:2]), got
    got = first("set reset S= R=")
    assert set(got[:2]) == {"Set operator S=", "Reset operator R="}, got
    # lower case is English: 'this' is a stop word once the words as written find nothing — said so, with the way out
    r = idx.search("this pointer xyzzyq", limit=5)
    assert "in capitals (THIS)" in r["words_ignored"]["this"] and "this" not in r["words_used"], r
    r = idx.search("THIS pointer", limit=5)                      # N166: a word the search used is not 'ignored'
    assert "words_ignored" not in r and r["words_used"] == ["THIS", "pointer"], r


def test_n161_symbol_search_takes_globs(idx):
    """N161 (0.8.14 retest): infosys_search_symbols 'FB_EcCoE*' → 0, the '*' taken literally (infosys_books takes
    globs); 'FB_EcCoE' → 7."""
    _store_pages(idx, [(f"tcplclib_tc2_ethercat/{i}", t, f"<pre>FUNCTION_BLOCK {t}\nVAR_INPUT\n    sNetId : STRING;\n"
                                                         "END_VAR</pre>")
                       for i, t in enumerate(["FB_EcCoESdoRead", "FB_EcCoESdoWrite", "FB_EcCoeReadBIC", "FB_EcGetSlaveState"])])
    r = idx.search_symbols("FB_EcCoE*", limit=10, auto_index=False)
    assert sorted(i["name"] for i in r["items"]) == ["FB_EcCoESdoRead", "FB_EcCoESdoWrite", "FB_EcCoeReadBIC"], r
    r = idx.search_symbols("FB_Ec*Write", limit=10, auto_index=False)
    assert [i["name"] for i in r["items"]] == ["FB_EcCoESdoWrite"], r
    assert idx.search_symbols("FB_EcCoE", limit=10, auto_index=False)["count"] == 3


_TESTANDSET = ("<p>You can use this function to check and set a flag. If the function call is successful, the function "
               "returns TRUE and the desired data can be accessed.</p><h2>Inputs/outputs</h2><pre>VAR_IN_OUT\n"
               "    Flag : BOOL; (* Flag to check if TRUE or FALSE *)\nEND_VAR</pre>"
               "<table><tr><th>Name</th><th>Type</th><th>Description</th></tr><tr><td>Flag</td><td>BOOL</td>"
               "<td>Boolean flag to be checked</td></tr></table><h2>Sample</h2><pre>VAR_GLOBAL\n    bGlobalTestFlag : BOOL;"
               "\nEND_VAR</pre><pre>VAR\n    nLocalBlockedCounter : DINT;\nEND_VAR</pre>"
               "<pre>IF TestAndSet(GVL.bGlobalTestFlag) THEN\n    GVL.bGlobalTestFlag := FALSE;\nEND_IF</pre>")
_NEW = ("<p>The operator is an extension of the IEC 61131-3 standard.</p><p>The __NEW operator dynamically reserves memory "
        "for function blocks.</p><p><b>Syntax</b>:</p><pre>&lt;pointer name&gt; := __NEW( &lt;type&gt; ( , &lt;size&gt; )? );"
        "\n__DELETE( &lt;pointer name&gt; );</pre><p><b>Sample:</b></p><pre>TYPE ST_Sample :\nSTRUCT\n    a,b,c,d,e,f : INT;"
        "\nEND_STRUCT\nEND_TYPE</pre><pre>pScalarType := __NEW(ScalarType, length);</pre>")
_DELETE = ("<p>The operator is an extension of the IEC 61131-3 standard.</p><p>The operator releases the memory of instances."
           "</p><p><b>Syntax:</b> __DELETE (&lt;Pointer&gt;)</p><p><b>Sample:</b></p><pre>FUNCTION_BLOCK FB_Dynamic\nVAR_INPUT"
           "\n    nIn1, nIn2 : INT;\nEND_VAR</pre><pre>__DELETE(_dut);</pre>")
_SIZEOF = ("<p>The operator is an extension of the IEC 61131-3 standard.</p><p>The operator is used to determine the number "
           "of bytes required by the specified variable x.</p><p><b>Syntax: </b></p><pre>&lt;return value&gt; := SIZEOF( "
           "&lt;variable name&gt; | &lt;date type name&gt; );</pre><p><b>Sample:</b></p><pre>PROGRAM MAIN\nVAR\n"
           "    aArr1 : ARRAY[0..4] OF INT;\nEND_VAR</pre>")
_MAXSTRING = ("<p>The variable of this type is PLC string with the maximal length.</p><pre>TYPE T_MaxString : "
              "STRING(MAX_STRING_LENGTH);\nEND_TYPE</pre><pre>VAR_GLOBAL CONSTANT\n    MAX_STRING_LENGTH : UDINT := 255;"
              "\nENd_VAR</pre>")


def test_n150_n151_n157_n172_kinds_from_the_page_not_its_samples(idx):
    """0.8.14 retest. N157: TestAndSet (a FUNCTION returning BOOL) was kind GlobalVariable, its declaration the sample's
    'VAR_GLOBAL bGlobalTestFlag : BOOL; END_VAR' — 73 pages were GlobalVariable through a sample's VAR_GLOBAL
    (FB_IecCriticalSection, Samples, __TRY …). A VAR_GLOBAL block makes a global variable list only on a page whose
    code is nothing else. N150: __NEW was a Struct (the sample's TYPE ST_Sample), __DELETE a FunctionBlock (a sample
    FB) — an operator page is kind Operator with its Syntax as the declaration. N151: SIZEOF (an operator page) was
    not found by docs_declaration. N172: T_MaxString listed the constant MAX_STRING_LENGTH it uses as a member."""
    P, S = "tc3_plc_intro", "tcplclib_tc2_system"
    with idx._db() as con:
        for ref, title, body in [
                (f"{S}/31023115", "TestAndSet", _TESTANDSET), (f"{P}/2529171083", "__NEW", _NEW),
                (f"{P}/2529160331", "__DELETE", _DELETE), (f"{P}/2528896907", "SIZEOF", _SIZEOF),
                (f"{S}/31062795", "T_MaxString", _MAXSTRING),
                (f"{S}/2326017163", "FB_IecCriticalSection",
                 "<p>The function block allows critical sections.</p><pre>VAR_GLOBAL\n    fbCriticalSection : "
                 "FB_IecCriticalSection;\nEND_VAR\n\nIF fbCriticalSection.Enter() THEN\n    ;\nEND_IF</pre>"),
                (f"{P}/12073949323", "Samples", "<p>Samples of the tutorial.</p><pre>VAR_GLOBAL\n    nSocketPort : INT;"
                                               "\nEND_VAR</pre><pre>PROGRAM MAIN\nVAR\nEND_VAR</pre>"),
                ("tcplclib_tc3_dali/5627593995", "Constants", "<pre>VAR_GLOBAL CONSTANT\n    cMaxDevices : INT := 64;"
                                                             "\nEND_VAR</pre>")]:
            idx._store(con, f"https://infosys.beckhoff.com/content/1033/{ref}.html", "", _page(title, body))
        kinds = dict(con.execute("SELECT title, kind FROM pages"))
    assert kinds == {"TestAndSet": "Function", "__NEW": "Operator", "__DELETE": "Operator", "SIZEOF": "Operator",
                     "T_MaxString": "Alias", "FB_IecCriticalSection": "FunctionBlock", "Samples": "Page",
                     "Constants": "GlobalVariable"}, kinds
    d = idx.declaration("TestAndSet")
    assert d["return_type"] == "BOOL" and "bGlobalTestFlag" not in (d["declaration"] or ""), d
    assert [x["name"] for x in d["in_out"]] == ["Flag"] and d["declaration"].startswith("FUNCTION TestAndSet : BOOL"), d
    d = idx.declaration("__NEW")
    assert d["kind"] == "Operator" and d["declaration"].startswith("<pointer name> := __NEW("), d
    assert "ST_Sample" not in json.dumps(d), d
    assert idx.declaration("__DELETE")["declaration"] == "__DELETE (<Pointer>)"
    assert idx.declaration("SIZEOF")["declaration"].startswith("<return value> := SIZEOF(")
    d = idx.declaration("T_MaxString")
    assert d["members"] == [] and [c["name"] for c in d["constants"]] == ["MAX_STRING_LENGTH"], d
    assert idx.declaration("Constants")["members"][0]["name"] == "cMaxDevices"


def test_n138_a_page_that_is_not_utf8_keeps_its_characters():
    """N138 (0.8.13 retest): the stored text of 4 tf5200 pages had U+FFFD for Beckhoff's curly quotes — the package
    page was Windows-1252, read as UTF-8 with errors replaced. (The ADS return-code table itself stores its dashes:
    '–', U+2013; a U+FFFD seen there comes from the console that printed it.)"""
    from twincat_mcp.helpviewer import decode_page
    raw = "<p>Use of the parameter “rt_cycles.enable” – as of V3.1</p>".encode("cp1252")
    assert decode_page(raw) == "<p>Use of the parameter “rt_cycles.enable” – as of V3.1</p>"
    assert decode_page("<p>– utf-8 ü</p>".encode("utf-8")) == "<p>– utf-8 ü</p>"
    latin = '<meta charset="iso-8859-1"><p>Müller</p>'.encode("latin-1")
    assert "Müller" in decode_page(latin)
    assert "�" not in html_to_doc(raw).markdown
    # review of 0.8.14: the usual <meta charset=utf-8> on a page that is not UTF-8 is not followed (it was decoded as
    # UTF-8 with replacement); a UTF-16 declaration on ASCII-compatible bytes neither; UTF-8 with one bad byte keeps
    # its good characters
    for meta in ('<meta charset="utf-8">', '<meta http-equiv="Content-Type" content="text/html; charset=utf-16">'):
        assert decode_page((meta + "<p>“x” – y</p>").encode("cp1252")) == meta + "<p>“x” – y</p>"
    assert decode_page("<p>Größe ".encode("utf-8") + b"\x96</p>") == "<p>Größe \ufffd</p>"
    # re-review of 0.8.14: a legacy charset the page declares is followed even with a byte it lacks (GB2312 + GBK)
    gb = '<meta charset="gb2312"><p>功能块的输入参数</p>'.encode("gbk") + "\u7881".encode("gbk")
    assert "功能块的输入参数" in decode_page(gb)
    assert decode_page("<p>Größe: „Maß“ – Öl</p>".encode("cp1252")) == "<p>Größe: „Maß“ – Öl</p>"


def test_n99b_one_source_page_placed_twice_in_a_book_is_one_hit_and_code_tables_come_first(idx):
    """N99b (0.8.13 retest, S4): '0x745' listed the 4 'ADS Return Codes' pages of tf6500 separately — ids
    374277003_30131813 / _2750866 …, Beckhoff's page 374277003 placed several times in one book — and an event-class
    struct that also lists the code ranked before the table. One source page is one hit (the others in same_page_in);
    of the defining pages a code table comes first; a merged hit's score is its best page's."""
    tbl = ("<p>Return codes of ADS.</p><table><tr><th>Hex</th><th>Dec</th><th>Name</th></tr>"
           "<tr><td>0x745</td><td>1861</td><td>ADSERR_CLIENT_SYNCTIMEOUT</td></tr></table>")
    with idx._db() as con:
        for ref in ("tf6500_tc3_iec60870_5_10x/374277003_30131813", "tf6500_tc3_iec60870_5_10x/374277003_2750866",
                    "tcplclib_tc2_system/374277003"):
            idx._store(con, f"https://infosys.beckhoff.com/content/1033/{ref}.html", "", _page("ADS Return Codes", tbl))
        idx._store(con, "https://infosys.beckhoff.com/content/1033/tf6510_tc3_iec61850/1.html", "",
                   _page("ST_TcGeneralAdsEventClass", "<table><tr><th>Hex</th><th>Dec</th><th>Name</th></tr>"
                         "<tr><td>0x745</td><td>1861</td><td>ClientSyncTimeout</td></tr></table>"))
    r = idx.search("0x745")
    titles = [i["title"] for i in r["items"]]
    assert titles == ["ADS Return Codes", "ST_TcGeneralAdsEventClass"], r["items"]
    first = r["items"][0]
    assert first["url"].endswith("/tcplclib_tc2_system/374277003.html")          # the library's own book shown
    assert first["same_page_in"] == ["tf6500_tc3_iec60870_5_10x"], first          # its two placements: one book


def _rows_table(head, *rows):
    return ("<table><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>"
            + "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows) + "</table>")


def store_eventlogger(store) -> None:
    """FB_TcMessage EXTENDS FB_TcEventBase IMPLEMENTS I_TcMessage, as Tc3_EventLogger documents it: FB_TcMessage's
    methods table says where each method is defined; 'Send' has a page below I_TcMessage only, 'RequestEventText'
    below FB_TcEventBase only. FB_Derived (no methods table) extends FB_TcEventBase. A DALI FB whose TEXT mentions
    TcMessage and Send."""
    E = "tcplclib_tc3_eventlogger"
    store(f"{E}/5003786635", "I_TcMessage", "<pre>INTERFACE I_TcMessage EXTENDS I_TcEventBase</pre><h2>Methods</h2>"
          + _rows_table(("Name", "Description"), ("Send", "Sends a message")), "i-tcmessage")
    store(f"{E}/5051076107", "Send", "<pre>METHOD Send : HRESULT\nVAR_INPUT\n    nTimeStamp : ULINT;\nEND_VAR</pre>",
          "i-tcmessage-send", "i-tcmessage")
    store(f"{E}/5002595467", "FB_TcEventBase", "<pre>FUNCTION_BLOCK FB_TcEventBase\nVAR_INPUT\n    bX : BOOL;\n"
          "END_VAR</pre>" + _rows_table(("Name", "Definition location", "Description"),
                                        ("RequestEventText()", "Local", "Returns the text for the event.")),
          "fb-tceventbase")
    store(f"{E}/5002603531", "RequestEventText", "<pre>METHOD RequestEventText : BOOL\nVAR_INPUT\n    nLangId : DINT;\n"
          "END_VAR</pre>", "fb-tceventbase-ret", "fb-tceventbase")
    store(f"{E}/5003041163", "FB_TcMessage", "<pre>FUNCTION_BLOCK FB_TcMessage EXTENDS FB_TcEventBase IMPLEMENTS "
          "I_TcMessage\nVAR_INPUT\n    bY : BOOL;\nEND_VAR</pre>"
          + _rows_table(("Name", "Definition location", "Description"),
                        ("RequestEventText()", "Inherited from FB_TcEventBase", "Returns the text for the event."),
                        ("Create()", "Local", "Creates a message instance in the EventLogger."),
                        ("Send()", "I_TcMessage", "Sends a message.")), "fb-tcmessage")
    store(f"{E}/5003041199", "FB_Derived", "<pre>FUNCTION_BLOCK FB_Derived EXTENDS FB_TcEventBase\nVAR_INPUT\n"
          "    bZ : BOOL;\nEND_VAR</pre>", "fb-derived")
    store("tcplclib_tc3_dali/10368229515", "FB_DALI_Osram_Coupler_HF_G2", "<pre>FUNCTION_BLOCK FB_DALI_Osram_Coupler_HF_G2"
          "\nVAR_INPUT\n    bStart : BOOL;\nEND_VAR</pre><p>Sends a TcMessage when the coupler reports an error; Send is "
          "called once.</p>", "dali-coupler")


def test_n158_n159_a_member_defined_elsewhere_and_what_is_near(idx):
    """N158 (0.8.14 retest): FB_TcMessage.Send → not found, though FB_TcMessage lists Send with definition location
    I_TcMessage and I_TcMessage.Send resolves. A member the owner does not document itself is looked up where the
    owner's page says it is defined, then up EXTENDS and IMPLEMENTS — and the answer says whose it is. N159: the near
    list for FB_TcMessage.Send was eight DALI FBs: the title filter of the near search held only for its first word
    (the others matched the page text). It offers the owner and the types the member comes from."""
    store_eventlogger(lambda ref, title, body, hid, parent=None: _store_with(idx, ref, title, body, hid, parent))
    idx.resolve_help_links()
    idx.sync_derived()
    E = "https://infosys.beckhoff.com/content/1033/tcplclib_tc3_eventlogger/"
    d = idx.declaration("FB_TcMessage.Send")
    assert d["url"] == E + "5051076107.html" and d["parent"]["title"] == "I_TcMessage", d
    assert (d["asked_as"], d["defined_in"]) == ("FB_TcMessage.Send", "I_TcMessage") and d["return_type"] == "HRESULT"
    d = idx.declaration("FB_TcMessage.RequestEventText")          # 'Inherited from FB_TcEventBase'
    assert d["url"] == E + "5002603531.html" and d["defined_in"] == "FB_TcEventBase", d
    d = idx.declaration("FB_Derived.RequestEventText")            # no methods table: EXTENDS
    assert d["url"] == E + "5002603531.html" and d["defined_in"] == "FB_TcEventBase", d
    assert "defined_in" not in idx.declaration("I_TcMessage.Send")          # its own member: nothing to say
    with pytest.raises(InfosysError, match="no symbol page") as e:
        idx.declaration("FB_TcMessage.Create")                    # 'Local', and no page: not found
    msg = str(e.value)
    assert msg.index("FB_TcMessage (") < msg.index("FB_TcEventBase (") and "I_TcMessage (" in msg, msg
    assert "DALI" not in msg, msg
    with idx._db() as con:
        near = [r["title"] for r in inf.near_symbols(con, "FB_TcMessage.Sendx")]
        assert near[:3] == ["FB_TcMessage", "FB_TcEventBase", "I_TcMessage"] and "FB_DALI_Osram_Coupler_HF_G2" not in near, near
        near = [r["title"] for r in inf.near_symbols(con, "TcMessage Send")]
        assert "FB_DALI_Osram_Coupler_HF_G2" not in near, near        # the title, not the text


def _visu_pages(n: int = 200):
    """n pages with 'Visu' — every 3rd in its title (the title order moves those up), the others in their text."""
    out = []
    for i in range(n):
        title = f"VisuElem{i:03d}" if i % 3 == 0 else f"Element {i:03d}"
        out.append((f"tc3_plc_intro/{9000000 + i}", title, f"<p>The Visu element number {i}. " + "Visu text. " * (i % 7)
                    + "</p>"))
    return out


def test_n167_n160_the_search_pages_through_every_hit_once(idx):
    """N167 (S3, 0.8.14 retest): 'search Visu* --limit 300' was refused and there was no offset — hits 51… could not
    be reached at all. A limit above 50 is used as 50 and said so (clamped); offset / next_offset page through the
    hits, every page cut from one order: no hit twice, none skipped — also past the hits the title order sees (150)."""
    _store_pages(idx, _visu_pages(200))
    idx.sync_derived()
    seen, off, pages = [], 0, 0
    while off is not None:
        r = idx.search("Visu", limit=50, offset=off)
        assert r["offset"] == off and r["total"] == 200 and r["count"] == len(r["items"]) <= 50, r
        seen += [i["url"] for i in r["items"]]
        off, pages = r["next_offset"], pages + 1
        assert pages <= 5
    assert len(seen) == 200 and len(set(seen)) == 200, (len(seen), len(set(seen)))
    first = idx.search("Visu", limit=10)
    assert [i["url"] for i in first["items"]] == seen[:10] and first["next_offset"] == 10       # one order
    assert first["items"][0]["title"].startswith("VisuElem")                                   # titles still first
    r = idx.search("Visu", limit=300)
    assert r["count"] == 50 and r["clamped"] is True and "offset=50" in r["clamped_note"], r
    with pytest.raises(InfosysError, match="past the last hit"):
        idx.search("Visu", offset=200)
    with pytest.raises(InfosysError, match="offset"):
        idx.search("Visu", offset=-1)


def test_n160_get_page_reads_a_part_as_the_skill_does_and_not_found_says_how_on(idx):
    """N160 (S3, 0.8.14 retest): infosys_get_page had no section and no offset (the skill's docs_page has both), took
    max_chars=50 where the skill refused below 200, and a name that is not found was a bare error — the skill names
    what is near and how to search. One reader of a page part (page_part), one range (200-60000) for both."""
    p = idx.get_page("ADSREAD", max_chars=200)
    assert p["truncated"] is True and p["next_offset"] == 200 and p["outline"] and p["offset"] == 0, p
    q = idx.get_page("ADSREAD", max_chars=200, offset=200)
    assert q["offset"] == 200 and q["markdown"] and q["markdown"] != p["markdown"]
    head = p["outline"][1] if len(p["outline"]) > 1 else p["outline"][0]
    s = idx.get_page("ADSREAD", section=head)
    assert s["section"] == head and s["markdown"].lstrip("#").strip().startswith(head), s["markdown"][:80]
    for bad in (dict(max_chars=199), dict(max_chars=60001), dict(offset=-1)):
        with pytest.raises(InfosysError, match="max_chars|offset") as e:
            idx.get_page("ADSREAD", **bad)
        assert "section=" in str(e.value) or "next_offset" in str(e.value)       # N169: the way to read on
    with pytest.raises(InfosysError, match="no section heading contains"):
        idx.get_page("ADSREAD", section="Bogus heading")
    with pytest.raises(InfosysError, match=r"infosys_search\(query='NoSuchSymbolXyz'\) finds pages by their text"):
        idx.declaration("NoSuchSymbolXyz")
    with pytest.raises(InfosysError, match=r"infosys_search\(query='NoSuchSymbolXyz'\)"):
        idx.get_page("NoSuchSymbolXyz")


def test_review_0815_an_acronym_in_capitals_does_not_outrank_the_words_of_the_query(idx):
    """Review of 0.8.15 (N148's title key): every word in capitals counted as a keyword the title should hold —
    'PLC persistent variables' ranked 'Resetting the PLC project' above 'Remanent Variables - PERSISTENT, RETAIN',
    'CX9020 LED' dropped 'Diagnostic LEDs'. An acronym is a word of the query like any other; only an IEC keyword
    (THIS, FOR) puts the page it titles first."""
    P = "tc3_plc_intro"
    _store_pages(idx, [
        (f"{P}/1000000101", "Remanent Variables - PERSISTENT, RETAIN", "<p>Persistent variables keep their values. "
                                                                       "PLC data.</p>"),
        (f"{P}/1000000102", "Resetting the PLC project", "<p>Reset the PLC project: persistent variables stay, other "
                                                         "variables are reset.</p>"),
        (f"{P}/1000000103", "THIS", "<p>THIS is a pointer to the own instance.</p>"),
        (f"{P}/1000000104", "Pointer", "<p>A pointer and THIS pointer: an address.</p>"),
    ])
    idx.sync_derived()
    first = lambda q: [i["title"] for i in idx.search(q, limit=5)["items"]]           # noqa: E731
    assert first("PLC persistent variables")[0] == "Remanent Variables - PERSISTENT, RETAIN", first("PLC persistent variables")
    assert first("THIS pointer")[0] == "THIS", first("THIS pointer")


def test_review_0815_a_member_through_an_owner_of_two_pages_is_not_guessed(idx):
    """Review of 0.8.15 (N158): FB_BA_SeqCtrl is documented by Tc3_BaCommon (no EXTENDS) and by tf8040 (EXTENDS
    FB_BA_Loop) — 'FB_BA_SeqCtrl.Calc' answered FB_BA_Loop.Calc ('it is FB_BA_Loop's') as if the owner were known,
    while 'FB_BA_SeqCtrl' alone is ambiguous. The sources of an owner of several pages that differ are not merged:
    not found, saying why, and library= picks the owner."""
    B, C = "tf8040_tc3_buildingautomation", "tcplclib_tc3_bacommon"
    _store_with(idx, f"{C}/1001", "FB_BA_SeqCtrl", "<pre>FUNCTION_BLOCK FB_BA_SeqCtrl\nVAR_INPUT\n    bA : BOOL;\nEND_VAR"
                "</pre>", "c-seq")
    _store_with(idx, f"{B}/2001", "FB_BA_SeqCtrl", "<pre>FUNCTION_BLOCK FB_BA_SeqCtrl EXTENDS FB_BA_Loop\nVAR_INPUT\n"
                "    bB : BOOL;\nEND_VAR</pre>", "b-seq")
    _store_with(idx, f"{B}/2002", "FB_BA_Loop", "<pre>FUNCTION_BLOCK FB_BA_Loop\nVAR_INPUT\n    bC : BOOL;\nEND_VAR</pre>",
                "b-loop")
    _store_with(idx, f"{B}/2003", "Calc", "<pre>METHOD Calc : BOOL\nVAR_INPUT\n    fIn : LREAL;\nEND_VAR</pre>", "b-calc",
                "b-loop")
    idx.resolve_help_links()
    idx.sync_derived()
    with pytest.raises(InfosysError, match="ambiguous"):
        idx.declaration("FB_BA_SeqCtrl")
    with pytest.raises(InfosysError, match=r"FB_BA_SeqCtrl is documented by 2 pages .* pass library=") as e:
        idx.declaration("FB_BA_SeqCtrl.Calc")
    assert "FB_BA_Loop's" not in str(e.value)
    d = idx.declaration("FB_BA_SeqCtrl.Calc", library=B)
    assert d["url"].endswith(f"{B}/2003.html") and d["defined_in"] == "FB_BA_Loop", d


def test_review_0815_syntax_sections_operator_names_and_the_page_own_header():
    """Review of 0.8.15 (N150 / N157 rules). An operator's syntax is what its 'Syntax' label gives — not the first
    code block within 2000 characters (CAL got its Example, __VARINFO 'in the declaration:'); an operator is named
    in capitals or as TwinCAT's __ ones (MathOperation's 'The operator can be selected' is a setting). The header in
    a page's Syntax section is its declaration, also under another name (FB_ExternalHydAxisLibRef on
    ExternalHydAxisLibRef, the copy-paste METHOD SendEqualMessage on SendHresult()); 'the Subscription method' names
    the page's kind as 'the method Subscription' does; 'FUNCTION BLOCK X' (44 pages write the space) is a header."""
    def page(title, body):
        return html_to_doc(f"<html><body><h1>{title}</h1>{body}</body></html>")

    def kind(title, body):
        return parse_declaration(title, page(title, body))
    cal = kind("CAL", "<p>The CAL operator is used to call a function block.</p><p><b>Syntax:</b></p>"
                      "<p>CAL &lt;function block&gt; (&lt;input&gt; := &lt;value&gt;)</p><p><b>Example:</b></p>"
                      "<pre>CAL fbInst(nVar1 := 0)</pre>")
    assert cal["kind"] == "Operator" and cal["declaration"] == "CAL <function block> (<input> := <value>)", cal
    vi = kind("__VARINFO", "<p>The operator __VARINFO returns information.</p><p><b>Syntax in the declaration:</b></p>"
                           "<pre>&lt;info&gt; : __SYSTEM.VAR_INFO;</pre><p><b>Syntax for the call:</b></p>"
                           "<pre>&lt;info&gt; := __VARINFO( &lt;variable&gt; );</pre><p><b>Sample:</b></p><pre>x := 1;</pre>")
    assert vi["declaration"] == "<info> : __SYSTEM.VAR_INFO;\n<info> := __VARINFO( <variable> );", vi
    mx = kind("MAX", "<p>The MAX operator returns the largest value.</p><p><b>Syntax:</b> <code>OUT := MAX(IN0, IN1)</code></p>")
    assert mx["declaration"] == "OUT := MAX(IN0, IN1)", mx
    assert kind("MathOperation", "<p>Symbols are calculated with the MathOperation block. The operator can be "
                                 "selected under MathOperator.</p>")["kind"] == "Page"
    hyd = kind("ExternalHydAxisLibRef", "<p>This class represents a standard hydraulic axis.</p><h2>Syntax:</h2>"
                                        "<pre>FUNCTION_BLOCK FB_ExternalHydAxisLibRef\nVAR_INPUT\n    stAxisRef : AXIS_REF;"
                                        "\nEND_VAR</pre><h2>Sample</h2><pre>METHOD Other</pre>")
    assert hyd["kind"] == "FunctionBlock", hyd
    sh = kind("SendHresult()", "<p>Logs a message in the EventLogger.</p><h2>Syntax:</h2><pre>METHOD SendEqualMessage : "
                               "HRESULT\nVAR_INPUT\n    hr : HRESULT;\nEND_VAR</pre>")
    assert sh["kind"] == "Method", sh
    sub = kind("Subscription", "<p>You can use the Subscription method to request OCA objects.</p><h2>Syntax</h2>"
                               "<pre>VAR_INPUT\n    bExecute : BOOL;\nEND_VAR</pre>")
    assert sub["kind"] == "Method", sub
    oc = kind("FB_OCPP1_Client", "<p>This function block represents an OCPP client.</p><h2>Syntax</h2>"
                                 "<pre>FUNCTION BLOCK FB_OCPP1_Client EXTENDS FB_Base\nVAR_OUTPUT\n    bValid : BOOL;\nEND_VAR</pre>")
    assert oc["kind"] == "FunctionBlock" and oc["declaration"].startswith("FUNCTION BLOCK FB_OCPP1_Client"), oc
    assert oc["extends"] == "FB_Base" and [x["name"] for x in oc["outputs"]] == ["bValid"], oc
