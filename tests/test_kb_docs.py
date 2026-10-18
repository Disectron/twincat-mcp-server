"""twincat-docs skill script (skills/twincat-docs/kb_docs.py) against databases built by the real knowledge-folder
tools (tests/fixtures/kb, see make_fixtures.py): the acceptance table of the docs-skill spec, the error contract, and
that nothing is ever written."""
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
def kbdir(tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(FIX, d, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    return d


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


def test_docs_page_refs_sections_and_paging(kbdir):
    rc, r = run(kbdir, "docs_page", "ref=tcplclib_tc2_system/30866571", "section=Outputs")
    assert rc == 0 and r["outline"] == ["ADSREAD", "Inputs", "Outputs", "Prerequisites"]
    assert r["text"].startswith("## Outputs") and "ERRID" in r["text"] and "READ    : BOOL" not in r["text"]
    assert r["breadcrumb"] == "PLC Lib: Tc2_System > ADS > ADSREAD"
    rc, r = run(kbdir, "docs_page", "ref=ms-xhelp:///?Id=beckhoff-f599-48a7-92f2-48aa9133ca8a")
    assert rc == 0 and r["ref"] == "tcplclib_tc2_system/31059723" and r["title"] == "T_AmsNetID"
    rc, r = run(kbdir, "docs_page", "ref=ADSREAD")
    assert rc == 1 and r["error"] == "ambiguous" and {c["book"] for c in r["candidates"]} == {"tcplclib_tc2_system", "tc3_c"}
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
    assert rc == 0 and r["books"] == [{"book": "tcplclib_tc2_system", "pages": 3}, {"book": "tcplclib_tc2_system_ex", "pages": 1}]


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
    assert len(r["commands"]) == 10
    rc, r = run(kbdir, "help")
    assert rc == 0 and set(r["commands"]) >= {"docs_search", "manual_page", "status"} and r["rules"]
    rc, r = run(kbdir, "status")
    assert rc == 0 and r["infosys"]["pages"] == 8 and r["infosys"]["books"] == 6 and r["manuals"]["manuals"] == 1
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


def test_fixtures_have_the_builders_schema():
    """The fixtures come from infosys_local.py / manuals_db.py (make_fixtures.py) — their tables are what the script reads."""
    import sqlite3
    c = sqlite3.connect(FIX / "helpviewer" / "infosys_en.db")
    cols = [r[1] for r in c.execute("PRAGMA table_info(page)")]
    assert cols == ["pid", "book", "title", "help_id", "toc_parent", "version", "crumb", "body"]
    assert {r[0] for r in c.execute("SELECT key FROM meta")} >= {"source", "lcid", "indexed", "schema", "state", "pages",
                                                                  "books", "links", "links_resolved", "mshc_path", "builder"}
    assert [r[1] for r in c.execute("PRAGMA table_info(link)")] == ["book", "pid", "n", "text", "href", "hid",
                                                                      "target_book", "target_pid"]
    m = sqlite3.connect(FIX / "manualsdb" / "manuals.db")
    assert [r[1] for r in m.execute("PRAGMA table_info(doc)")] == ["id", "path", "name", "size", "mtime", "sha256", "title",
                                                                     "pages", "text_pages", "indexed", "backend"]
    assert [r[1] for r in m.execute("PRAGMA table_info(product)")] == ["code", "doc", "page", "n"]


def test_review_help_slash_titles_code_headings_and_check_order(kbdir, tmp_path):
    rc, r = run(kbdir, "--help")
    assert rc == 0 and "commands" in r
    # a title with '/' is not mistaken for book/pageid; a '# ' line inside a code block is not a heading
    import sqlite3
    db = kbdir / "helpviewer" / "infosys_en.db"
    c = sqlite3.connect(db)
    body = "# Send/Receive\nIntro.\n\n## Sample\n```\n# not a heading\nx := 1;\n```\n\n## Notes\nend"
    c.execute("INSERT INTO page VALUES (?,?,?,?,?,?,?,?)", ("50000001", "tf6310_tc3_tcpip", "Send/Receive", "h-sr", "", "TF6310", "", body))
    c.execute("INSERT INTO fts(title, body, book, pid) VALUES (?,?,?,?)", ("Send/Receive", body, "tf6310_tc3_tcpip", "50000001"))
    c.commit()
    c.close()
    rc, r = run(kbdir, "docs_page", "ref=Send/Receive")
    assert rc == 0 and r["ref"] == "tf6310_tc3_tcpip/50000001" and r["outline"] == ["Send/Receive", "Sample", "Notes"]
    rc, r = run(kbdir, "docs_page", "ref=Send/Receive", "section=Sample")
    assert "# not a heading" in r["text"] and "end" not in r["text"]
    # pages are checked before the manual is looked up
    rc, r = run(kbdir, "manual_page", "doc=nope", "pages=5-40")
    assert rc == 1 and r["error"] == "bad_param"
