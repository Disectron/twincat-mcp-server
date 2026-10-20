"""twincat-docs skill script (skills/twincat-docs/kb_docs.py) against indexes built by the real code at test time
(tests/conftest.py kb_fixture, tests/fixtures/kb/make_fixtures.py): the plugin's one Infosys index and the manuals
index — the acceptance table of the docs-skill spec, the error contract, and that nothing is ever written."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "twincat-docs" / "kb_docs.py"
FIX = Path(__file__).parent / "fixtures" / "kb"


@pytest.fixture
def kbdir(kb_copy):
    return kb_copy


def run(kb: Path | None, *args: str, env: dict[str, str] | None = None) -> tuple[int, dict]:
    cmd = [sys.executable, str(SCRIPT), *args] + (["--kb", str(kb)] if kb is not None else [])
    e = {k: v for k, v in os.environ.items() if k not in ("TCMCP_KNOWLEDGE_DIR", "TCMCP_HOME")}
    e.update(env or {})
    p = subprocess.run(cmd, capture_output=True, timeout=60, env=e)
    out = json.loads(p.stdout.decode("utf-8"))
    assert "elapsed_ms" in out
    if p.returncode:
        assert out["error"] and out["message"], out                  # never an empty message
    return p.returncode, out


def _digest(d: Path) -> dict[str, str]:
    return {str(f.relative_to(d)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(d.rglob("*")) if f.is_file()}


def test_docs_search_ranking_total_and_phrase_retry(kbdir):
    rc, r = run(kbdir, "docs_search", "query=ADSREAD", "limit=3")
    assert rc == 0 and r["count"] == 3 and r["total"] == 4 and r["truncated"] is True
    assert r["items"][0]["ref"] == "tc3_c/743169291"                 # bm25(title 8, body 1) like infosys_local.py
    it = r["items"][1]
    assert it["url"] == "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/30866571.html"
    assert "[ADSREAD]" in it["excerpt"] and it["version"] == "Tc2_System (TwinCAT v3.1.0)"
    assert r["source"]["package"] == "BKINFOSYS3_FIXTURE_EN-US.mshc" and r["source"]["lcid"] == "1033"
    # code with punctuation (and \xa0 in the stored text): not FTS5 syntax → retried once as a phrase
    rc, r = run(kbdir, "docs_search", "query=fbRead( READ := FALSE )")
    assert rc == 0 and r["query_mode"].startswith("phrase") and [i["ref"] for i in r["items"]] == ["tcplclib_tc2_utilities/35409035"]
    rc, r = run(kbdir, "docs_search", "query=unbalanced (")
    assert rc == 0 and r["count"] == 1
    # an unquoted value with spaces arrives as several arguments
    rc, r = run(kbdir, "docs_search", "query=fbRead(", "READ", ":=", "FALSE", ")")
    assert rc == 0 and r["query"] == "fbRead( READ := FALSE )" and r["count"] == 1
    # the book filter is applied before the limit: total is exact
    rc, r = run(kbdir, "docs_search", "query=ADSREAD", "book=tcplclib_tc2_sys*")
    assert rc == 0 and r["total"] == 3 and {i["book"] for i in r["items"]} <= {"tcplclib_tc2_system", "tcplclib_tc2_system_ex"}


@pytest.mark.parametrize("args, code, text", [
    (["query=x", "limit=0"], "bad_param", "limit must be 1-50 (got 0)"),
    (["query=x", "limit=51"], "bad_param", "refused, not clamped"),
    (["query=  "], "empty_query", "empty"),
    (["query=:="], "empty_query", "no searchable word"),
    (["query=ADSREAD", "bogus=1"], "bad_param", "allowed: query, book, limit"),
    (["query=ADSREAD", "book=nosuch*"], "not_found", "no book matches"),
    ([], "bad_param", "query is required"),
])
def test_docs_search_errors(kbdir, args, code, text):
    rc, r = run(kbdir, "docs_search", *args)
    assert rc == 1 and r["error"] == code and text in r["message"], r


def test_docs_declaration_never_resolves_an_ambiguous_name(kbdir):
    rc, r = run(kbdir, "docs_declaration", "symbol=MC_MoveAbsolute")
    assert rc == 1 and r["error"] == "ambiguous"
    assert [c["ref"] for c in r["candidates"]] == ["tcplclib_tc2_mc2/70094731", "tcplclib_tc3_drivemotioncontrol/8280601227"]
    rc, r = run(kbdir, "docs_declaration", "symbol=MC_MoveAbsolute", "book=tcplclib_tc3_drivemotioncontrol")
    assert rc == 0 and len(r["declaration_blocks"]) == 3 and r["declaration_blocks"][2].startswith("VAR_IN_OUT")
    assert r["intro"] == "The function block MC_MoveAbsolute starts the positioning to an absolute target position."
    assert [a["book"] for a in r["also_in"]] == ["tcplclib_tc2_mc2"]
    assert "\xa0" not in "".join(r["declaration_blocks"])
    rc, r = run(kbdir, "docs_declaration", "symbol=MC_MoveAbsolute", "book=tcplclib_tc2_mc2")
    assert rc == 0 and "Execute BOOLPosition" not in r["intro"]       # the FBD label line is dropped
    rc, r = run(kbdir, "docs_declaration", "symbol=adsread", "book=tcplclib_tc2_system")
    assert rc == 0 and r["ref"] == "tcplclib_tc2_system/30866571" and r["version"] == "Tc2_System (TwinCAT v3.1.0)"
    assert r["requirements"].startswith("| Development environment") and "Tc2_System" in r["requirements"]
    assert r["intro"].startswith("The function block ADSREAD reads data") and "NETID T_AmsNetId" not in r["intro"]
    rc, r = run(kbdir, "docs_declaration", "symbol=NoSuchSymbol_42")
    assert rc == 1 and r["error"] == "not_found" and "docs_search" in r["hint"]
    rc, r = run(kbdir, "docs_declaration", "symbol=MC_MoveAbs")
    assert rc == 1 and {n["ref"] for n in r["near"]} == {"tcplclib_tc2_mc2/70094731", "tcplclib_tc3_drivemotioncontrol/8280601227"}


def test_n103b_docs_declaration_gives_kind_and_return_type(kbdir):
    """N103b (0.8.12 retest): docs_declaration had no kind or return type — MEMCPY's page writes 'FUNCTION MEMCPY :
    UDINT' as a heading, its code block holds the inputs only, so a reader of the skill never saw ': UDINT'. The skill
    answers with what the index stored for the page (the same as infosys_declaration)."""
    rc, r = run(kbdir, "docs_declaration", "symbol=MEMCPY")
    assert rc == 0 and r["kind"] == "Function" and r["return_type"] == "UDINT", r
    assert r["declaration"].startswith("FUNCTION MEMCPY : UDINT"), r["declaration"]
    assert r["declaration_blocks"][0].startswith("VAR_INPUT")          # the page's own code blocks, as before
    rc, r = run(kbdir, "docs_declaration", "symbol=ADSREAD", "book=tcplclib_tc2_system")
    assert rc == 0 and r["kind"] == "FunctionBlock" and "return_type" not in r


def test_docs_page_refs_sections_and_paging(kbdir):
    rc, r = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571", "section=Outputs")
    assert rc == 0 and r["outline"] == ["ADSREAD", "Inputs", "Outputs", "Prerequisites"]
    assert r["text"].startswith("## Outputs") and "ERRID" in r["text"] and "READ    : BOOL" not in r["text"]
    assert r["breadcrumb"] == "PLC Lib: Tc2_System > ADS > ADSREAD"
    rc, r = run(kbdir, "docs_page", "ref=ms-xhelp:///?Id=beckhoff-f599-48a7-92f2-48aa9133ca8a")
    assert rc == 0 and r["ref"] == "tcplclib_tc2_system/31059723" and r["title"] == "T_AmsNetID"
    # N100: two pages are titled ADSREAD — the Tc2_System FB and the C++ API's AdsRead, which documents no IEC
    # symbol: the name is the FB's (as infosys_get_page answers), the C++ page is named beside it
    rc, r = run(kbdir, "docs_page", "ref=ADSREAD")
    assert rc == 0 and r["ref"] == "tcplclib_tc2_system/30866571" and r["resolved_by"] == "title"
    assert [(a["ref"], a.get("same_document")) for a in r["also_in"]] == [("tc3_c/743169291", None)]
    for ref in ("30866571", "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/30866571.html", "ADSREAD"):
        extra = ["book=tcplclib_tc2_system"] if ref == "ADSREAD" else []
        rc, r = run(kbdir, "docs_page", f"ref={ref}", *extra)
        assert rc == 0 and r["ref"] == "tcplclib_tc2_system/30866571", ref
    rc, full = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571")
    n = full["chars"]
    rc, a = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571", "max_chars=200")
    assert a["next_offset"] == 200 and a["truncated"] is True and len(a["text"]) == 200
    rc, b = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571", "offset=200", "max_chars=20000")
    assert b["next_offset"] is None and a["text"] + b["text"] == full["text"] and len(full["text"]) == n
    rc, r = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571", f"offset={n + 10}")
    assert rc == 1 and r["error"] == "bad_param" and "past the end" in r["message"]
    rc, r = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571", "section=Bogus")
    assert rc == 1 and r["error"] == "not_found" and r["outline"][0] == "ADSREAD"
    for bad in ("max_chars=199", "max_chars=20001", "offset=-1"):
        rc, r = run(kbdir, "docs_page", "ref=30866571", bad)
        assert rc == 1 and r["error"] == "bad_param", bad


def test_docs_books(kbdir):
    rc, r = run(kbdir, "docs_books", "filter=tcplclib_tc2_sys*")
    assert rc == 0 and r["books"] == [{"book": "tcplclib_tc2_system", "pages": 4}, {"book": "tcplclib_tc2_system_ex", "pages": 1}]


def test_manual_commands(kbdir):
    rc, r = run(kbdir, "manual_search", "query=watchdog")
    assert rc == 0 and r["items"][0]["doc"] == "CX9020.pdf" and r["items"][0]["page"] == 4
    assert r["items"][0]["section"] == "10 Error handling and diagnostics > 10.1 Diagnostic LEDs"
    assert "[watchdog]" in r["items"][0]["excerpt"] and r["source"]["manuals"] == 1
    rc, r = run(kbdir, "manual_search", "query=watchdog", "doc=nope")
    assert rc == 1 and r["error"] == "not_found" and r["near"] == ["CX9020.pdf"]
    rc, r = run(kbdir, "manual_product", "code=cx9020")
    assert rc == 0 and r["code"] == "CX9020" and r["items"][0]["doc"] == "CX9020.pdf" and r["items"][0]["pages"] == [1, 2, 3, 4]
    rc, r = run(kbdir, "manual_product", "code=EL3102")
    assert rc == 0 and r["items"][0]["pages"] == [2, 4]
    # N67: a variant no manual names falls back to the base product the manual is written for
    rc, r = run(kbdir, "manual_product", "code=CX9020-0130")
    assert rc == 0 and r["items"][0]["doc"] == "CX9020.pdf" and r["matched_as"].startswith("base product CX9020")
    rc, r = run(kbdir, "manual_product", "code=EL9999-0010")
    assert rc == 0 and r["count"] == 0 and "matched_as" not in r
    rc, r = run(kbdir, "manual_product", "code=EL9999")
    assert rc == 0 and r["count"] == 0 and 'manuals_db.py" update --kb' in r["hint"] and str(kbdir) in r["hint"]
    rc, r = run(kbdir, "manual_page", "doc=CX9020", "pages=5-40")
    assert rc == 1 and r["error"] == "bad_param" and "at most 21" in r["message"]
    rc, r = run(kbdir, "manual_page", "doc=CX9020", "pages=4-9")
    assert rc == 1 and "has 4 pages" in r["message"]
    rc, r = run(kbdir, "manual_page", "doc=CX9020", "pages=3-4")
    assert rc == 0 and [p["page"] for p in r["pages"]] == [3, 4] and r["truncated"] is False
    assert r["pages"][1]["section"] == "10 Error handling and diagnostics > 10.1 Diagnostic LEDs"
    rc, r = run(kbdir, "manual_page", "doc=1", "pages=1-4", "max_chars=200")
    assert rc == 0 and r["truncated"] is True and sum(len(p["text"]) for p in r["pages"]) == 200 and "manual_page" in r["next"]
    rc, r = run(kbdir, "manual_toc", "doc=CX9020", "depth=1")
    assert rc == 0 and [t["title"] for t in r["toc"]] == ["1 Foreword", "2 Product overview", "10 Error handling and diagnostics"]
    rc, r = run(kbdir, "manual_toc", "doc=CX9020", "depth=7")
    assert rc == 1 and r["error"] == "bad_param"
    rc, r = run(kbdir, "manual_docs")
    assert rc == 0 and r["manuals"][0]["doc"] == "CX9020.pdf" and r["manuals"][0]["text_pages"] == 4


def test_commands_status_help_and_location(kbdir, tmp_path):
    rc, r = run(kbdir, "ads_read")
    assert rc == 1 and r["error"] == "unknown_command" and "never return documentation" in r["message"]
    assert len(r["commands"]) == 15
    rc, r = run(kbdir, "help")
    assert rc == 0 and set(r["commands"]) >= {"docs_search", "manual_page", "status"} and r["rules"]
    rc, r = run(kbdir, "status")
    assert rc == 0 and r["infosys"]["pages"] == 9 and r["infosys"]["books"] == 6 and r["manuals"]["manuals"] == 1
    # no database → no_index naming the build command, in the folder it looked at
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, r = run(empty, "docs_search", "query=ADSREAD")
    assert rc == 1 and r["error"] == "no_index" and 'builders' in r["hint"] and 'infosys_local.py" index --kb' in r["hint"]
    assert str(empty) in r["hint"]
    rc, r = run(empty, "status")
    assert rc == 0 and "missing" in r["infosys"] and "missing" in r["manuals"]
    # the folder chosen with kb_set_location (pointer file), else TCMCP_KNOWLEDGE_DIR
    home = tmp_path / "home"
    home.mkdir()
    (home / "knowledge_location.json").write_text(json.dumps({"path": str(kbdir)}), encoding="utf-8")
    rc, r = run(None, "status", env={"TCMCP_HOME": str(home)})
    assert rc == 0 and r["knowledge_base"] == str(kbdir) and "kb_set_location" in r["found_via"]
    rc, r = run(None, "status", env={"TCMCP_HOME": str(home), "TCMCP_KNOWLEDGE_DIR": str(empty)})
    assert r["knowledge_base"] == str(empty) and "TCMCP_KNOWLEDGE_DIR" in r["found_via"]


def test_read_only_nothing_changes(kbdir):
    before = _digest(kbdir)
    for args in (["docs_search", "query=ADSREAD"], ["docs_page", "ref=30866571"], ["manual_search", "query=watchdog"],
                 ["status"], ["docs_declaration", "symbol=MC_MoveAbsolute"]):
        run(kbdir, *args)
    assert _digest(kbdir) == before


def test_fixtures_have_the_indexes_schema(kb_fixture):
    """The fixture comes from the real code (make_fixtures.py): the plugin's Infosys index (the one the infosys_* tools
    read too) and manuals_db.py's — their tables are what the script reads, in the permanent layout."""
    import sqlite3
    from twincat_mcp import infosys as inf
    c = sqlite3.connect(kb_fixture / "index" / "infosys-1033.db")
    cols = [r[1] for r in c.execute("PRAGMA table_info(pages)")]
    assert cols[:12] == ["url", "book", "page_id", "lastmod", "title", "kind", "library", "markdown", "text", "code",
                         "decl", "fetched"] and {"version", "crumb"} <= set(cols)
    meta = {r[0]: r[1] for r in c.execute("SELECT key, value FROM meta")}
    assert meta["indexer_version"] == str(inf.INDEXER_VERSION) and meta["state"] == "complete"
    assert {"source_stamp", "package", "package_file", "pages", "books", "built", "built_by", "lcid", "links",
            "links_resolved"} <= set(meta)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    assert {"pages_fts", "helpids", "catalog", "code_blocks"} <= names
    assert str(c.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "wal"     # readers open it read-only
    m = sqlite3.connect(kb_fixture / "index" / "manuals.db")
    assert [r[1] for r in m.execute("PRAGMA table_info(doc)")] == ["id", "path", "name", "size", "mtime", "sha256", "title",
                                                                     "pages", "text_pages", "indexed", "backend"]
    assert [r[1] for r in m.execute("PRAGMA table_info(product)")] == ["code", "doc", "page", "n"]
    assert not (kb_fixture / "helpviewer").exists() and not (kb_fixture / "manualsdb").exists()


def test_review_help_slash_titles_code_headings_and_check_order(kbdir, tmp_path):
    rc, r = run(kbdir, "--help")
    assert rc == 0 and "commands" in r
    # a title with '/' is not mistaken for book/pageid; a '# ' line inside a code block is not a heading
    from twincat_mcp.infosys import InfosysIndex, html_to_doc
    url = "https://infosys.beckhoff.com/content/1033/tf6310_tc3_tcpip/50000001.html"
    ix = InfosysIndex(db_file=kbdir / "index" / "infosys-1033.db")       # the copy, written the way a build writes
    page = ("<html><body><h1>Send/Receive</h1><p>Intro.</p><h2>Sample</h2><pre># not a heading\nx := 1;</pre>"
            "<h2>Notes</h2><p>end</p></body></html>")
    with ix._db() as con:
        ix._store(con, url, "", html_to_doc(page, base_url=url))
    del ix
    rc, r = run(kbdir, "docs_page", "ref=Send/Receive")
    assert rc == 0 and r["ref"] == "tf6310_tc3_tcpip/50000001" and r["outline"] == ["Send/Receive", "Sample", "Notes"]
    rc, r = run(kbdir, "docs_page", "ref=Send/Receive", "section=Sample")
    assert "# not a heading" in r["text"] and "end" not in r["text"]
    # pages are checked before the manual is looked up
    rc, r = run(kbdir, "manual_page", "doc=nope", "pages=5-40")
    assert rc == 1 and r["error"] == "bad_param"


def test_n36_questions_in_words_fall_back_and_n35_near_is_quick(kbdir):
    """N36: `LED diagnostics` / `microSD boot card` gave 0 although each word finds the page — words were only ANDed.
    N35: not_found took 9.6 s on the real package (a LIKE scan of every page body for `near`)."""
    rc, r = run(kbdir, "manual_search", "query=watchdog diagnostics nonexistentword")
    # N65: a word on no page is dropped before anything else is relaxed
    assert rc == 0 and r["query_mode"] == "all words found in the index" and r["items"][0]["page"] == 4
    assert r["words_used"] == ["watchdog", "diagnostics"] and r["words_ignored"] == {"nonexistentword": "on no page"}
    rc, r = run(kbdir, "manual_search", "query=Diagnos LED")
    assert rc == 0 and r["query_mode"] == "all words (as prefixes)" and r["items"][0]["page"] in (3, 4)
    rc, r = run(kbdir, "docs_search", "query=reads ADS device")
    assert rc == 0 and r["query_mode"] == "all words" and r["items"][0]["ref"] == "tcplclib_tc2_system/30866571"
    # FTS syntax and code are not rewritten
    rc, r = run(kbdir, "docs_search", "query=ADSREAD NOT ADSWRITE")
    assert rc == 0 and r["query_mode"] == "fts5"
    rc, r = run(kbdir, "docs_search", "query=fbRead( READ := FALSE )")
    assert r["query_mode"].startswith("phrase")
    rc, r = run(kbdir, "docs_search", "query=zzqx yyqw")
    assert rc == 0 and r["count"] == 0 and "hint" in r
    rc, r = run(kbdir, "docs_declaration", "symbol=MC_MoveAbs")
    assert rc == 1 and len(r["near"]) == 2 and r["elapsed_ms"] < 2000


def _kbd_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("kb_docs_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_n65_fallback_drops_stop_words_absent_and_common_words_and_ranks_the_title_first(kbdir):
    """0.8.9 retest N65: the fallback tiers turned every word into a prefix, stop words included —
    'how to zzqxv a the' gave 'some words', 49,096 hits, top 'How To'; 'ADSREAD xyzqq' ranked AdsReadDeviceInfo
    above ADSREAD."""
    rc, r = run(kbdir, "docs_search", "query=how to zzqxv a the")
    assert rc == 0 and r["count"] == 0 and r["total"] == 0
    assert r["words_ignored"] == {"how": "stop word", "to": "stop word", "zzqxv": "on no page", "a": "stop word",
                                  "the": "stop word"}
    rc, r = run(kbdir, "docs_search", "query=ADSREAD xyzqq")
    assert rc == 0 and r["query_mode"] == "all words found in the index" and r["items"][0]["title"] == "ADSREAD"
    # the some-words tier: which words each hit holds; a word on most pages does not pick pages there
    rc, r = run(kbdir, "docs_search", "query=MC_MoveAbsolute ADSWRITE")
    assert rc == 0 and r["query_mode"].startswith("some words")
    assert all(it["matched"] and set(it["matched"]) <= {"MC_MoveAbsolute", "ADSWRITE"} for it in r["items"])
    # unit level: a page titled like a query word ranks first even when another page mentions the word more often
    import sqlite3
    m = _kbd_module()
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE VIRTUAL TABLE fts USING fts5(title, body, book UNINDEXED, pid UNINDEXED)")
    con.execute("INSERT INTO fts VALUES ('AdsReadDeviceInfo', ?, 'b', '1')", (" ".join(["ADSREAD"] * 30),))
    con.execute("INSERT INTO fts VALUES ('ADSREAD', 'reads data with ADSREAD and more words around it here', 'b', '2')")
    for i in range(8):
        con.execute("INSERT INTO fts VALUES (?, 'the common words of every page', 'b', ?)", (f"Page {i}", str(10 + i)))
    con.execute("CREATE VIRTUAL TABLE temp.fts_vocab USING fts5vocab(main, 'fts', 'row')")
    sql = "SELECT rowid AS rid, title, bm25(fts, 8.0, 1.0) AS r FROM fts WHERE fts MATCH ? ORDER BY r LIMIT ?"
    cnt = "SELECT COUNT(*) FROM fts WHERE fts MATCH ?"
    rows, total, mode, _expr, info = m.search_tiers(con, sql, cnt, "ADSREAD xyzqq", [], 5, title_key="title")
    assert mode == "all words found in the index" and [x["title"] for x in rows][:2] == ["ADSREAD", "AdsReadDeviceInfo"]
    rows, total, mode, _expr, info = m.search_tiers(con, sql, cnt, "common ADSREAD zzz", [], 5, title_key="title")
    assert mode.startswith("some words") and info["words_too_common"] == ["common"] and info["words_used"] == ["ADSREAD"]
    assert info["words_ignored"] == {"zzz": "on no page"} and total == 2 and rows[0]["title"] == "ADSREAD"
    rows, total, mode, _expr, info = m.search_tiers(con, sql, cnt, "every common", [], 5, title_key="title")
    assert rows and "words_too_common" not in info          # both words common: the AND tiers still answer


def test_n79_a_query_of_only_stop_words_is_refused_like_kb_search(kbdir):
    """N79: 'how to a the' ran as 'all words' (1,205 hits; 'the' 44,472) while kb_search refuses such a query — one
    rule in both: nothing but stop words is refused; one word alone is searched as written."""
    rc, r = run(kbdir, "docs_search", "query=how to a the")
    assert rc == 1 and r["error"] == "empty_query" and "only stop words" in r["message"]
    rc, r = run(kbdir, "manual_search", "query=the and")
    assert rc == 1 and r["error"] == "empty_query"
    rc, r = run(kbdir, "docs_search", "query=the")
    assert rc == 0 and r["query_mode"] == "all words"
    rc, r = run(kbdir, "docs_search", "query=how to zzqxv a the")            # one informative word: not refused
    assert rc == 0 and r["count"] == 0


def test_stop_words_are_compared_folded():
    """Review A: the query word is folded ('für' → 'fur') but the stop-word set held 'für' — never matched."""
    m = _kbd_module()
    assert m._fold("Für") in m.STOP_WORDS and "für" not in m.STOP_WORDS
    assert all(w == m._fold(w) for w in m.STOP_WORDS)


def test_hw_commands_read_the_esi_index_with_its_state(kbdir, tmp_path):
    """0.8.12: the skill's hw_* commands read index\\esi.db through the plugin's esi_index — the same answers as the
    server's hw_* tools — and say the index's state; nothing is written."""
    from tests.test_esi_index import EL33, VENDOR
    from twincat_mcp import esi_index as E
    rc, r = run(kbdir, "hw_lookup", "query=EL3356-0010")
    assert rc == 1 and r["error"] == "no_index" and "esi_db.py" in r["hint"]
    m = kbdir / "manuals" / "Beckhoff_EtherCAT_XML"
    m.mkdir(parents=True)
    (m / "Beckhoff EL33xx.xml").write_text(EL33.format(vendor=VENDOR.format(fv=10)), encoding="utf-8")
    E.build(kbdir / "index" / "esi.db", kbdir, [], twincat=False, log=lambda s: None)
    before = _digest(kbdir)
    rc, r = run(kbdir, "hw_lookup", "query=EL3356-0010", "limit=1")
    assert rc == 0 and r["devices"][0]["revision"] == "#x0019000A" and r["source"]["state"] == "complete"
    rc, r = run(kbdir, "hw_object", "device=EL3356-0010", "index=8000")
    assert rc == 0 and r["index"] == "#x8000" and r["subitems"][0]["default"] == 20
    rc, r = run(kbdir, "hw_search", "query=Underrange", "kinds=device")
    assert rc == 0 and r["items"][0]["device"] == "EL3356-0010"
    rc, r = run(kbdir, "hw_object", "device=EL3356-0010", "index=#x8010")
    assert rc == 1 and r["error"] == "not_found" and "the nearest" in r["message"]
    rc, r = run(kbdir, "hw_lookup", "query=EL3356-0010", "detail=all")
    assert rc == 1 and r["error"] == "bad_param"
    rc, r = run(kbdir, "status")
    assert rc == 0 and r["esi_state"]["state"] == "complete" and "ESI index: 2 device revisions" in r["summary"]
    (m / "Beckhoff EL34xx.xml").write_text(EL33.format(vendor=VENDOR.format(fv=1)).replace("EL3356", "EL3456"),
                                           encoding="utf-8")
    rc, r = run(kbdir, "hw_lookup", "query=EL3356-0010", "limit=1")
    assert rc == 0 and r["source"]["state"] == "outdated" and "OUTDATED" in r["source"]["state_note"]
    assert {k: v for k, v in _digest(kbdir).items() if "EL34xx" not in k} == before


def test_n100_docs_declaration_follows_the_plugins_rule(kbdir):
    """N100 (0.8.11 retest): docs_declaration T_MaxString was 'ambiguous' — the same page id in two books (a library's
    page copied into another product's book) — and ADSREAD ambiguous with the C++ AdsRead page, while infosys_declaration
    answered both. The skill resolves a name with the plugin's rule (infosys.symbol_documents)."""
    import shutil
    import sqlite3
    db = next((kbdir / "index").glob("infosys-*.db"))
    con = sqlite3.connect(db)
    row = con.execute("SELECT url, book, page_id, title, kind, library, markdown, text, code, decl, fetched, version, crumb "
                      "FROM pages WHERE book = 'tcplclib_tc2_system' AND page_id = '30866571'").fetchone()
    copy = list(row)
    copy[0] = row[0].replace("tcplclib_tc2_system", "tf6510_tc3_iec61850")
    copy[1] = "tf6510_tc3_iec61850"
    con.execute("INSERT INTO pages(url, book, page_id, title, kind, library, markdown, text, code, decl, fetched, version, "
                "crumb) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", copy)
    con.commit()
    con.close()
    for sym in ("ADSREAD", "adsread"):
        rc, r = run(kbdir, "docs_declaration", f"symbol={sym}")
        assert rc == 0 and r["ref"] == "tcplclib_tc2_system/30866571" and r["resolved_by"] == "symbol", r
        also = {a["ref"]: a.get("same_document") for a in r["also_in"]}
        assert also == {"tf6510_tc3_iec61850/30866571": True, "tc3_c/743169291": None}, also
    rc, r = run(kbdir, "docs_page", "ref=30866571")           # a bare page id of a copied page: one page
    assert rc == 0 and r["ref"] == "tcplclib_tc2_system/30866571"
    rc, r = run(kbdir, "docs_declaration", "symbol=MC_MoveAbsolute")    # two libraries: still refused
    assert rc == 1 and r["error"] == "ambiguous" and len(r["candidates"]) == 2


def test_kbv_pages_whose_id_is_not_a_number(kbdir):
    """kb_verify on the real index (0.8.13): docs_page with the Infosys URL of a book's index page ('…/el6851/
    index.html') or of a page id like '6778772363_30967009' — 1169 of the 51,655 pages — said not_found (looked up as
    title), and links to them were left out of `links`: the skill read only digits as a page id. A page id is what the
    page's file is named (twincat_mcp.infosys.book_of)."""
    import sqlite3
    db = kbdir / "index" / "infosys-1033.db"
    old = "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31059723.html"
    new = "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/index.html"
    mc = "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_mc2/70094731.html"
    mc_new = "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_mc2/6778772363_70094731.html"
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE pages SET url = ?, page_id = 'index' WHERE url = ?", (new, old))
        con.execute("UPDATE pages SET url = ?, page_id = '6778772363_70094731' WHERE url = ?", (mc_new, mc))
        con.execute("UPDATE helpids SET url = ? WHERE url = ?", (new, old))
        con.execute("UPDATE pages SET markdown = replace(markdown, ?, ?)", (old, new))
    con.close()
    rc, r = run(kbdir, "docs_page", f"ref={new}")
    assert rc == 0 and r["ref"] == "tcplclib_tc2_system/index" and r["title"] == "T_AmsNetID", r
    rc, r = run(kbdir, "docs_page", f"ref={mc_new}#section")
    assert rc == 0 and r["ref"] == "tcplclib_tc2_mc2/6778772363_70094731", r
    rc, r = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571")             # ADSREAD links T_AmsNetID
    assert rc == 0 and {"text": "T_AmsNetID", "ref": "tcplclib_tc2_system/index"} in r["links"], r.get("links")
