"""The one search of the documentation indexes (twincat_mcp/docsearch.py, infosys.search_index): N99, N102, N105."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from twincat_mcp import docsearch as D
from twincat_mcp.infosys import InfosysIndex, html_to_doc
from tests.fake_infosys import FakeFetcher

ROOT = Path(__file__).resolve().parents[1]
C = "https://infosys.beckhoff.com/content/1033/"


@pytest.mark.parametrize("text, words", [
    ("FB_SocketConnect", ["fb", "socket", "connect"]),
    ("NT_GetTime", ["nt", "get", "time"]),
    ("sIPAddr", ["s", "ip", "addr"]),
    ("Tc3_EventLogger", ["tc3", "event", "logger"]),
    ("FB_EcCoESdoRead", ["fb", "ec", "co", "e", "sdo", "read"]),
    ("ADSLOGSTR TON EL3356", []),                      # one word each: the tokenizer has them already
    ("FB_AlarmList aTexts : ARRAY OF STRING", ["fb", "alarm", "list", "a", "texts"]),
])
def test_identifiers_are_words(text, words):
    assert D.ident_words(text) == words


def test_code_forms_and_where_a_code_is_defined():
    f = D.code_forms("ADS return codes 0x745")
    assert (f["value"], f["dec"], f["hex"]) == (1861, "1861", ["0x745", "0x0745", "0x00000745"])
    assert D.code_forms("ADS error 1861")["hex"][0] == "0x745" and D.code_forms("16#745")["value"] == 1861
    assert D.code_forms("error 7") is None and D.code_forms("0x745 1861") is None and D.code_forms("ADSREAD") is None
    # a decimal number is a code only when a word says so (review 0.8.12): a port or a build is none
    assert D.code_forms("ADS port 851") is None and D.code_forms("build 4024") is None
    # N122 (0.8.13 retest): a number that is the whole query is a code ('1861' listed Tc3_Vision pages first)
    assert D.code_forms("1861")["value"] == 1861 and D.code_forms(" 1861 ")["value"] == 1861
    assert D.code_forms("ADS error on port 851") is None                                 # N121: not next to 'error'
    assert D.code_forms("7") is None                                                     # one digit: no code
    table = "| Hex | Dec | Name |\n|---|---|---|\n| 0x745 | 1861 | ADSERR_CLIENT_SYNCTIMEOUT |"
    assert D.definition_score(table, 1861) == 2
    assert D.definition_score("| Code |\n| 0x00000745 |", 1861) == 1
    assert D.definition_score("returns 0x745 (1861) on a timeout", 1861) == 0


def test_titles_that_name_the_query_come_first():
    rows = [{"title": t} for t in ("Quick Start (C++ / TCP Client)", ".NET client", "FB_TlsSocketConnect",
                                   "FB_SocketConnect", "AdsReadDeviceInfo", "ADSREAD")]
    assert [r["title"] for r in D.title_order(rows, "tcp client connect socket")][:2] == ["FB_SocketConnect",
                                                                                          "FB_TlsSocketConnect"]
    assert D.title_order(rows, "ADSREAD xyzqq")[0]["title"] == "ADSREAD"      # exactly a query word, before a prefix



def test_kbv_the_title_that_is_the_query_comes_first():
    """kb_verify on the real index (0.8.13): 'Internal cycle process' listed ten pages titled 'Process (internal
    cycle)' before the page of that title — the same words, so nothing told them apart but bm25. The title that is the
    query (its words, in order, nothing else) comes first."""
    rows = [{"title": "Process (internal cycle)"}] * 3 + [{"title": "Internal cycle process"}, {"title": "Cycle"}]
    assert D.title_order(rows, "Internal cycle process")[0]["title"] == "Internal cycle process"
    assert D.title_order(rows, "internal  cycle process:")[0]["title"] == "Internal cycle process"
    rows = [{"title": "Parameters"}, {"title": "Description of parameters"}]
    assert D.title_order(rows, "Description of parameters")[0]["title"] == "Description of parameters"


def test_n105_the_library_names_query_words_too():
    """N105 (0.8.12 retest): 'EtherCAT slave state' — every candidate's title names 'slave state' only; 'EtherCAT' is
    the library's (Tc2_EtherCAT.FB_EcGetSlaveState). Title words first, then title and library together: the
    Tc2_EtherCAT symbols come before a manual page that only says 'slave' and 'state'."""
    rows = [{"title": "State Machine Slave Settings", "library": None},
            {"title": "FB_EcSetSlaveState", "library": "Tc2_EtherCAT"},
            {"title": "FB_EcGetSlaveState", "library": "Tc2_EtherCAT"}]
    got = [r["title"] for r in D.title_order(rows, "EtherCAT slave state", "title", "library")]
    assert got[-1] == "State Machine Slave Settings", got
    assert [r["title"] for r in D.title_order(rows, "EtherCAT slave state")][0] == "State Machine Slave Settings"
    # the title still counts first: a title with more of the words beats a library match
    rows.append({"title": "EtherCAT slave state machine", "library": None})
    assert D.title_order(rows, "EtherCAT slave state", "title", "library")[0]["title"] == "EtherCAT slave state machine"


def _page(title, body):
    return html_to_doc(f"<html><body><h1>{title}</h1>{body}</body></html>")


@pytest.fixture
def idx(tmp_path):
    i = InfosysIndex(tmp_path / "docs", fetcher=FakeFetcher(), books=["tcplclib_tc2_system"], max_workers=2)
    codes = ("<p>The ADS return codes.</p><table><tr><th>Hex</th><th>Dec</th><th>Name</th></tr>"
             "<tr><td>0x744</td><td>1860</td><td>ADSERR_CLIENT_DUPLINVOKEID</td></tr>"
             "<tr><td>0x745</td><td>1861</td><td>ADSERR_CLIENT_SYNCTIMEOUT</td></tr></table>")
    pages = [
        ("tf6310_tc3_tcpip/84195851", "FB_SocketConnect", "<p>The function block establishes a new TCP/IP "
         "connection to a remote server (client).</p><pre>VAR_INPUT\n    sRemoteHost : STRING;\nEND_VAR</pre>"),
        ("tf6311_tc3_tcpudp/1076850187", "Quick Start (C++ / TCP Client)", "<p>A TCP client: connect, socket, "
         "TCP client, connect the socket of the TCP client.</p>"),
        ("tf6310_tc3_tcpip/84214411", ".NET client", "<p>TCP client socket connect TCP client socket connect.</p>"),
        ("tc3_c/374277003", "ADS Return Codes", codes),
        ("tcplclib_tc2_system/374277003", "ADS Return Codes", codes),                   # the same page, another book
        ("tf7xxx_tc3_vision/16960508043", "ReadImage", "<p>Returns 0x745 on a timeout.</p>"),
        ("tf7xxx_tc3_vision/16960520843", "ReadMlModel", "<p>Returns 0x745 on a timeout.</p>"),
    ]
    with i._db() as con:
        for ref, title, body in pages:
            i._store(con, f"{C}{ref}.html", "", _page(title, body))
    return i


def test_n105_n99_one_search_for_both_readers(idx, tmp_path):
    """N105 (0.8.11 retest): 'tcp client connect socket' did not find FB_SocketConnect in the top 5 — the tokenizer
    keeps FB_SocketConnect as 'fb' + 'socketconnect', so neither 'socket' nor 'connect' was on the page; the index
    (format 5) stores a title's identifier words, and hits are ordered by how much of the query the title names.
    N99: '0x745' listed pages that mention the code before the ADS return-code table that defines it (and its 30
    copies). The plugin's infosys_search and the skill's docs_search answer from one implementation, in one order."""
    with idx._db() as con:
        assert con.execute("SELECT words FROM pages WHERE title = 'FB_SocketConnect'").fetchone()[0] == "fb socket connect"
    r = idx.search("tcp client connect socket")
    assert r["items"][0]["title"] == "FB_SocketConnect", [i["title"] for i in r["items"]]
    r = idx.search("0x745")
    assert [i["title"] for i in r["items"]][:2] == ["ADS Return Codes", "ReadImage"], r["items"]
    first = r["items"][0]
    assert first["url"] == f"{C}tcplclib_tc2_system/374277003.html" and first["same_page_in"] == ["tc3_c"]
    assert "define 0x745 / 1861 in a table" in r["code_note"]
    r = idx.search("ADS error 1861")                                           # the decimal form: the same table
    assert r["items"][0]["title"] == "ADS Return Codes"
    # the skill reads the same file with the same code
    kb = tmp_path / "kb"
    (kb / "index").mkdir(parents=True)
    shutil.copy(idx.db_file, kb / "index" / "infosys-1033.db")
    for q, top in (("tcp client connect socket", "FB_SocketConnect"), ("0x745", "ADS Return Codes")):
        p = subprocess.run([sys.executable, str(ROOT / "skills" / "twincat-docs" / "kb_docs.py"), "docs_search",
                            f"query={q}", "--kb", str(kb)], capture_output=True, timeout=60,
                           env={k: v for k, v in os.environ.items() if k != "TCMCP_KNOWLEDGE_DIR"})
        out = json.loads(p.stdout)
        plugin = [i["title"] for i in idx.search(q)["items"]]
        assert p.returncode == 0 and [i["title"] for i in out["items"]] == plugin and plugin[0] == top, (out, plugin)
    assert out["items"][0]["same_page_in"] == ["tc3_c/374277003"] and out["code"]["dec"] == "1861"


def test_review_0812_two_pages_of_one_book_are_never_one_document(idx):
    """Review 0.8.12: two FBs' methods of one name and signature (Beckhoff titles a method page with the bare name)
    were one 'document' — infosys_declaration answered one of them, the search dropped the other as a copy. A copy is
    in another book; two pages of one book are two documents."""
    from twincat_mcp.infosys import InfosysError
    body = "<pre>METHOD Reset : BOOL\nVAR_INPUT\n    bForce : BOOL;\nEND_VAR</pre>"
    with idx._db() as con:
        for ref in ("tcplclib_tc3_x/111", "tcplclib_tc3_x/222", "tf9999_other/111"):
            idx._store(con, f"{C}{ref}.html", "", _page("Reset", "<p>Resets the block.</p>" + body))
        con.execute("UPDATE pages SET library = 'Tc3_X' WHERE book = 'tcplclib_tc3_x'")
    with pytest.raises(InfosysError, match="ambiguous: Reset is documented by 2 different pages"):
        idx.declaration("Reset")
    r = idx.search("Resets the block")
    urls = sorted(i["url"] for i in r["items"])
    assert urls == [f"{C}tcplclib_tc3_x/111.html", f"{C}tcplclib_tc3_x/222.html"], r["items"]
    assert [i.get("same_page_in") for i in r["items"] if i["url"].endswith("/111.html")] == [["tf9999_other"]]


def test_review_0812_truncated_counts_documents_not_copies(idx, tmp_path):
    """Review 0.8.12: 'total' counts pages (a copy once per book) and 'truncated' was total > items — true when
    nothing was cut, only copies listed once."""
    r = idx.search("0x745", limit=10)
    assert r["total"] == 4 and r["count"] == 3 and r["truncated"] is False, r
    r = idx.search("0x745", limit=1)
    assert r["count"] == 1 and r["truncated"] is True, r


def test_second_review_0812_the_page_id_wins_whatever_the_order():
    """Second review 0.8.12: rows B, C, A (B and A two methods of one book with the same declaration, C the copy of
    A in another book): the declaration match took book Y for B before A's page id could — {B, C} and {A}."""
    from twincat_mcp.infosys import same_documents
    decl = json.dumps({"declaration": "METHOD Reset : BOOL"})

    def row(rid, book, pid):
        return {"rid": rid, "book": book, "pid": pid, "library": "Tc3_X", "decl": decl, "body": "", "title": "Reset"}
    a, b, c = row(1, "tcplclib_tc3_x", "111"), row(2, "tcplclib_tc3_x", "222"), row(3, "tf9999_y", "111")
    for order in ([a, b, c], [b, c, a], [c, b, a], [b, a, c]):
        groups = sorted(sorted(x["rid"] for x in g) for g in same_documents(order))
        assert groups == [[1, 3], [2]], (order, groups)
    assert D.code_forms("ErrorCode 1861")["value"] == 1861 and D.code_forms("nErrId 1861")["value"] == 1861


def test_kbv_a_number_is_a_code_only_next_to_a_code_word():
    """kb_verify full run on the real index (0.8.13): 'Number of errors per row (P-CHAN-00020)' listed the pages that
    define error code 20 before the page of that title — 'errors' anywhere in the query made 00020 a code, though the
    rule (review 0.8.12) is a number NEXT TO a word that says it is a code, and 00020 is part of a parameter name."""
    assert D.code_forms("Number of errors per row (P-CHAN-00020)") is None
    assert D.code_forms("errors in P-CHAN-00020") is None and D.code_forms("error in row 25 of the table") is None
    assert D.code_forms("ADS error 1861")["value"] == 1861 and D.code_forms("error code 1861")["value"] == 1861
    assert D.code_forms("nErrId = 1861")["value"] == 1861 and D.code_forms("1861 error")["value"] == 1861
    assert D.code_forms("ADS return codes 0x745")["value"] == 1861          # hex needs no word


def test_kbv_a_page_titled_as_the_query_is_found(idx):
    """kb_verify on the real index (0.8.13): 'Data type' did not find the page titled 'Data type' in the top 50 — the
    hits are ordered by their titles, but only the 150–300 best by bm25 were read, and 300 'Data types' pages full of
    'data type' came first (the page was bm25 rank 685). The pages whose title holds the query's words are read too."""
    with idx._db() as con:
        for n in range(320):
            idx._store(con, f"{C}tf{n:03d}_lib/{1000 + n}.html", "",
                       _page("Data types", f"<p>Library {n}: " + "the data type of the data type. " * 5 + "</p>"))
        idx._store(con, f"{C}tf5050_tc3_nc_camming/1003565323.html", "",
                   _page("Data type", "<p>" + " ".join(f"word{k}" for k in range(400)) + " The type of data.</p>"))
        idx._store(con, f"{C}tf5225_cycles/323079947.html", "", _page("Internal cycle process", "<p>"
                   + " ".join(f"word{k}" for k in range(400)) + " internal cycle process.</p>"))
        for n in range(12):
            idx._store(con, f"{C}tf52{n:02d}_cyc/{5000 + n}.html", "", _page(
                "Process (internal cycle)", f"<p>Cycle {n}: internal cycle process, internal cycle process.</p>"))
        # full run on the real index: a title of one common word ('TwinCAT': thousands of titles hold it) or of a
        # word shorter than 3 letters ('MC', no informative word at all)
        for n in range(320):
            idx._store(con, f"{C}tc3_x{n:03d}/{7000 + n}.html", "", _page(
                f"TwinCAT component {n}", f"<p>TwinCAT {n}: " + "TwinCAT runtime. " * 4 + "</p>"))
            idx._store(con, f"{C}tcplclib_tc2_mc2_{n:03d}/{8000 + n}.html", "", _page(
                f"MC_Function{n}", f"<p>MC function {n}: MC MC MC.</p>"))
        idx._store(con, f"{C}tc3_c/6778770443.html", "", _page(
            "TwinCAT", "<p>" + " ".join(f"word{k}" for k in range(400)) + " TwinCAT.</p>"))
        idx._store(con, f"{C}tc3_io_intro/1999768587.html", "", _page(
            "MC", "<p>" + " ".join(f"word{k}" for k in range(400)) + " MC.</p>"))
    for q in ("Data type", "Internal cycle process", "TwinCAT", "MC"):
        r = idx.search(q, limit=10)
        assert r["items"][0]["title"] == q, (q, [i["title"] for i in r["items"]])
    r = idx.search("Data type", limit=10)
    assert r["total"] == 321 and r["query_mode"] == "all words"          # the count is the tier's, unchanged

