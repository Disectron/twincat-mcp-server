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
    assert idx.search_code("IDXGRP:=16#F005", language="xml")["count"] == 0


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
