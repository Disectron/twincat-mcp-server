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
    assert D.code_forms("1861")["hex"][0] == "0x745" and D.code_forms("16#745")["value"] == 1861
    assert D.code_forms("error 7") is None and D.code_forms("0x745 1861") is None and D.code_forms("ADSREAD") is None
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
    r = idx.search("1861")                                                     # the decimal form: the same table
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
