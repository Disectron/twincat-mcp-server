"""The PDF-manuals index (skills/twincat-docs/builders/manuals_db.py) and the twincat-docs skill's manual_* commands
(skills/twincat-docs/kb_docs.py) on a manual built at test time — the findings of the 0.8.14 retest, §34f / §34h:
product codes of every Beckhoff family (N163), hex index forms (N164), unit symbols (N165), paging of the search
(N167), `show --offset` (N168), ranges in the help and in the refusals (N169).

The manual is written with reportlab and indexed by the builder's own CLI (whatever PDF library this Python has), so
what is tested is what the user's update stores."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
B = ROOT / "skills" / "twincat-docs" / "builders"
KBD = ROOT / "skills" / "twincat-docs" / "kb_docs.py"

# (bookmark, lines) — one PDF page each; page numbers below are 1-based
PAGES: list[tuple[str, list[str]]] = [
    ("1 Foreword", ["ED2008, ED280x EtherCAT Terminals", "Manual | EN"]),                                     # 1
    ("2.1 ED2008", ["2.1 ED2008 EtherCAT Terminal, 8-channel digital input 24 V DC",
                    "The variant ED2008-0022 has a second connector."]),                                       # 2
    ("2.2 ED2809", ["2.2 ED2809 EtherCAT Terminal, 16-channel digital output",
                    "Related: AL2006 linear motor, AA3023 electric cylinder, PS2001 power supply, ERI2008 box."]),  # 3
    ("5.1 Index 8000", ["Index 8000 Bridge settings", "8000:0 Bridge settings UINT8", "8000:0A Enable filter BOOL",
                        "8000:11 Filter settings UINT16"]),                                                    # 4
    ("5.2 Filter", ["Set the filter with object 0x8000:11 (sub-index 17)."]),                                  # 5
    ("5.3 ESI", ["In the ESI file the object is #x8000."]),                                                     # 6
    ("5.4 Settings", ["The object 0x8000 holds the settings."]),                                               # 7
    ("6 Motor", ["Motor speed 8000 rpm and 11 pole pairs."]),                                                  # 8
    ("7 Technical data", ["Operating temperature -25 °C ... +60 °C", "Storage temperature -40 °C ... +85 °C"]),  # 9
    ("8 Programming", ["Programming in C++ with the C compiler, files in C:\\Program Files"]),                  # 10
    ("9 Bridge", ["Bridge signal 2 mV/V nominal sensitivity"]),                                                # 11
    ("10 Ranges", ["Input range in mV, supply in V, the ambient temperature matters"]),                        # 12
] + [(f"11.{k} Widget {k}", [f"Widget number {k} of the paging test, {'with a longer line of text ' * 3}"])
     for k in range(1, 61)] + [                                                                                # 13-72
    ("12 Index h", ["Index 8000h:11h holds the filter settings (h notation)."]),                              # 73
    ("13 Appendix", ["Chapter 11 of the appendix lists 8000 words."]),                                         # 74
]
WIDGET_PAGES = list(range(13, 73))


def build_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    for i, (head, lines) in enumerate(PAGES):
        c.bookmarkPage(f"p{i}")
        c.addOutlineEntry(head, f"p{i}", level=0)
        c.setFont("Helvetica", 10)
        for j, ln in enumerate(lines):                   # the body only, away from the edges (no running header)
            c.drawString(72, 600 - 16 * j, ln)
        c.showPage()
    c.save()


def builder(*args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("TCMCP_KNOWLEDGE_DIR", "TCMCP_HOME")}
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(B / "manuals_db.py"), *args], capture_output=True, timeout=120,
                          env=env, encoding="utf-8", errors="replace")


def kbdocs(kb: Path, *args: str) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k not in ("TCMCP_KNOWLEDGE_DIR", "TCMCP_HOME")}
    p = subprocess.run([sys.executable, str(KBD), *args, "--kb", str(kb)], capture_output=True, timeout=60, env=env)
    out = json.loads(p.stdout.decode("utf-8"))
    if p.returncode:
        assert out["error"] and out["message"], out
    return p.returncode, out


@pytest.fixture(scope="module")
def mkb(tmp_path_factory):
    pytest.importorskip("reportlab")
    pytest.importorskip("pypdf")
    kb = tmp_path_factory.mktemp("manuals") / "kb"
    (kb / "manuals").mkdir(parents=True)
    build_pdf(kb / "manuals" / "ED2008.pdf")
    p = builder("update", "--kb", str(kb))
    assert p.returncode == 0 and "1 added" in p.stdout, (p.stdout, p.stderr)
    return kb


@pytest.fixture
def mkb_copy(mkb, tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(mkb, d)
    return d


def pages_of(r: dict) -> list[int]:
    return sorted(it["page"] for it in r["items"])


# ---------------------------------------------------------------------------------------------- N163
def test_n163_product_codes_of_every_beckhoff_family_are_indexed(mkb):
    """N163 (0.8.14 retest): `manual_product code=ED2008` (and ED2809) answered count 0 and 'copy the PDF manuals
    into …' although `ED2008, ED280x.pdf` is indexed — the builder took product codes only with the prefixes it
    listed (EL, EK, EP …), and ED (like AL, AA, PS, ERI …) was not among them. A Beckhoff order code is the family's
    letters and four digits, whatever the family."""
    want = {"ED2008": [1, 2], "ED2809": [3], "AL2006": [3], "AA3023": [3], "PS2001": [3], "ERI2008": [3]}
    for code, pages in want.items():
        rc, r = kbdocs(mkb, "manual_product", f"code={code}")
        assert rc == 0 and r["count"] == 1 and r["items"][0]["pages"] == pages, (code, r)
        assert "hint" not in r and "matched_as" not in r
    # idea of the 0.8.15 retest (EL3102 → the EL28xx manual's commissioning example): the manual's title page names
    # ED2008 and the family ED280x — its manual; AL2006 / PS2001 … appear only on page 3 — it mentions them
    for code in ("ED2008", "ED2809"):
        rc, r = kbdocs(mkb, "manual_product", f"code={code}")
        assert r["items"][0]["kind"] == "product manual" and "kind_note" not in r, (code, r)
    for code in ("AL2006", "AA3023", "PS2001", "ERI2008"):
        rc, r = kbdocs(mkb, "manual_product", f"code={code}")
        assert r["items"][0]["kind"] == "mentions" and "e.g. as an example" in r["kind_note"], (code, r)
    from twincat_mcp.knowledge import manuals_for
    m = manuals_for(mkb, ["AL2006"])
    assert m["manuals"]["AL2006"][0]["kind"] == "mentions" and "not written for this product" in m["manuals_note"], m
    rc, r = kbdocs(mkb, "manual_product", "code=ED2008-0022")                 # the variant, as written in the text
    assert rc == 0 and r["items"][0]["pages"] == [2] and "matched_as" not in r
    p = builder("product", "ED2809", "--kb", str(mkb))
    assert p.returncode == 0 and "ED2008.pdf | 1 mentions | pages 3" in p.stdout, p.stdout
    # what the builder stores changed: a new page text format, so an index of the old one asks for a rebuild
    con = sqlite3.connect(mkb / "index" / "manuals.db")
    fmt = con.execute("SELECT value FROM meta WHERE key = 'text_format'").fetchone()[0]
    con.close()
    assert int(fmt) >= 6
    rc, st = kbdocs(mkb, "status")
    assert st["manuals_state"]["state"] == "complete" and st["manuals_state"]["text_format_expected"] == fmt


def test_n163_a_code_in_the_text_is_never_sent_to_copy_an_indexed_manual(mkb_copy):
    """N163 (0.8.14 retest): the not-found hint told the user to copy the PDF of a manual that is indexed — the
    product table of an index built by the older builder has no ED codes, but the page text has them
    (`manual_search ED2008` gave 10 hits). A code the product table lacks is looked up in the page text; the answer
    says so, and why (an index of an older format: the approved rebuild lists it). Only a code on no page is sent
    to copy a PDF."""
    db = mkb_copy / "index" / "manuals.db"
    con = sqlite3.connect(db)
    con.execute("DELETE FROM product WHERE code LIKE 'ED%'")                 # as the format-5 builder left it
    con.execute("UPDATE meta SET value = '5' WHERE key = 'text_format'")
    con.commit()
    con.close()
    rc, r = kbdocs(mkb_copy, "manual_product", "code=ED2008")
    assert rc == 0 and r["count"] == 1 and r["items"][0]["doc"] == "ED2008.pdf" and r["items"][0]["pages"] == [1, 2]
    assert r["items"][0]["mentions"] == 3                                     # ED2008 twice, ED2008-0022 once
    assert r["matched_as"].startswith("page text") and "hint" not in r
    assert "rebuild" in r["note"] and "format 5" in r["note"] and "copy" not in json.dumps(r).lower()
    assert r["source"]["state"] == "rebuild_needed"
    rc, r = kbdocs(mkb_copy, "manual_product", "code=ED2809-0005")           # a variant no page names: the base
    assert rc == 0 and r["items"][0]["pages"] == [3] and "ED2809" in r["matched_as"]
    rc, r = kbdocs(mkb_copy, "manual_product", "code=EL9999")
    assert rc == 0 and r["count"] == 0 and "copy the PDF manuals" in r["hint"]
    # hw_lookup's manuals block (knowledge.manuals_for) said 'copy its PDF' for ED2008 too: one rule for both
    from twincat_mcp.knowledge import manuals_for
    m = manuals_for(mkb_copy, ["ED2008"])
    assert m["manuals"]["ED2008"][0]["doc"] == "ED2008.pdf" and m["manuals"]["ED2008"][0]["pages"] == [1, 2], m
    assert m["manuals"]["ED2008"][0]["matched_as"].startswith("page text") and "rebuild" in m["manuals_note"], m
    assert "copy" not in json.dumps(m).lower()
    assert "copy its PDF" in manuals_for(mkb_copy, ["EL9999"])["manuals_note"]


# ---------------------------------------------------------------------------------------------- N164
def test_n164_a_hex_index_is_searched_in_all_its_notations(mkb):
    """N164 (0.8.14 retest): `manual_search "8000:11" doc=EL3356` 1 hit, `0x8000:11` 4; `#x8000` 1, `0x8000` 15 —
    each notation is another token of the index (0x8000 / x8000 / 8000), so one never found the others. 0x…, #x…,
    16#… and, for a CoE index, its sub-index rows (8000:11) are one term: found in any notation in one search."""
    for q in ("8000:11", "0x8000:11", "#x8000:11", "16#8000:11", "0x8000:0x11", "8000:011", "8000h:11", "8000h:11h"):
        rc, r = kbdocs(mkb, "manual_search", f"query={q}")
        assert rc == 0 and pages_of(r) == [4, 5, 73] and r["total"] == 3, (q, r)      # N177: p73 '8000h:11h'
        assert r["terms"][0]["term"] == q and "8000:11" in r["terms"][0]["searched_as"]
        assert "8000h:11h" in r["terms"][0]["searched_as"], r["terms"]
    for q in ("0x8000", "#x8000", "16#8000", "8000h"):
        rc, r = kbdocs(mkb, "manual_search", f"query={q}")
        assert rc == 0 and pages_of(r) == [4, 5, 6, 7, 73], (q, r)             # not 'Motor speed 8000 rpm' (p8)
    # N177 (0.8.15 retest): '8000h:11' was the words 8000h and 11 — p74 ('Chapter 11 … 8000 words') is no hit
    rc, r = kbdocs(mkb, "manual_search", "query=8000h:11")
    assert 74 not in pages_of(r) and r["terms"][0]["term"] == "8000h:11", r
    # review of 0.8.15: the builder's CLI search was a second, raw FTS search ('8000:11' an FTS syntax error) — it
    # prints the skill's manual_search now
    p = builder("search", "8000:11", "--kb", str(mkb))
    hits = [ln for ln in p.stdout.splitlines() if " | p" in ln]
    assert p.returncode == 0 and sorted(int(ln.split(" | p")[1].split(" ")[0].split("|")[0]) for ln in hits) == [4, 5, 73], p.stdout + p.stderr
    rc, r = kbdocs(mkb, "manual_search", "query=0x8000 filter")               # with words: the index is one of them
    assert rc == 0 and pages_of(r) == [4, 5, 73] and r["words_used"] == ["filter"]
    # FTS5 syntax written by the user runs as written
    rc, r = kbdocs(mkb, "manual_search", 'query="0x8000" OR widget')
    assert rc == 0 and r["query_mode"] == "fts5" and "terms" not in r


# ---------------------------------------------------------------------------------------------- N165
def test_n165_a_unit_symbol_is_searched_as_the_unit_not_as_its_letter(mkb):
    """N165 (0.8.14 retest): `manual_search "°C"` ran as FTS5 as written (the '°' is no word character, so the query
    was not taken for words) and the index keeps only the letter of '°C' — the one-letter token C was searched: C++
    and C:\\ pages first, the temperature pages buried. A unit symbol is searched as the unit: the page text must
    hold it as written. `mV/V` is the unit, not mV and V anywhere on the page."""
    rc, r = kbdocs(mkb, "manual_search", "query=°C")
    assert rc == 0 and pages_of(r) == [9] and r["query_mode"] != "fts5", r
    assert r["terms"] == [{"term": "°C", "searched_as": r["terms"][0]["searched_as"]}] and "°C" in r["terms"][0]["searched_as"]
    rc, r = kbdocs(mkb, "manual_search", "query=temperature °C")
    assert rc == 0 and pages_of(r) == [9], r                                  # p12 has temperature, p10 the C
    rc, r = kbdocs(mkb, "manual_search", "query=mV/V")
    assert rc == 0 and pages_of(r) == [11], r                                 # p12: 'in mV, supply in V'
    rc, r = kbdocs(mkb, "manual_search", "query=C++")                         # not a unit: as before
    assert rc == 0 and "terms" not in r


def test_n165_a_unit_is_one_symbol_whichever_code_point_the_pdf_used():
    """N165 (0.8.14 retest), unit level: PDFs write micro as U+00B5 or U+03BC, ohm as U+03A9 or U+2126, and degrees
    Celsius as '°C', '° C' or U+2103 — a unit typed one way finds it written the other way; a plain letter (V, s) is a
    word of the query, not a unit; letters around a unit make it part of a word ('Terms' holds no 'ms')."""
    import importlib.util
    import re
    spec = importlib.util.spec_from_file_location("kb_docs_units", KBD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)

    def holds(query: str, text: str) -> bool:
        terms, _rest = m._manual_terms(query)
        assert len(terms) == 1, (query, terms)
        return bool(re.search(terms[0]["rx"], text))
    assert holds("µs", "cycle 50 μs") and holds("μs", "cycle 50 µs")
    assert holds("kΩ", "R = 10 kΩ") and holds("kΩ", "R = 10 kΩ")
    assert holds("°C", "-25 ° C") and holds("°C", "+60℃") and holds("℃", "+60 °C")
    assert holds("mV/V", "2 mV / V") and not holds("mV/V", "2 mV and V")
    assert not holds("ms", "Terms and conditions") and holds("ms", "every 10 ms")
    assert not holds("°C", "°Celsius")
    assert m._manual_terms("V A s m")[0] == [] and m._manual_terms("version 3 V")[0] == []


# ---------------------------------------------------------------------------------------------- N167
def test_n167_manual_search_pages_with_offset_and_clamps_the_limit(mkb):
    """N167 (0.8.14 retest): `manual_search` had a limit of 1-50 and no offset — hits 51… could not be reached at all,
    and a limit above 50 was refused. The search pages like docs_page (offset / next_offset); a limit above the
    maximum is used as the maximum and the answer says so (clamped)."""
    rc, r = kbdocs(mkb, "manual_search", "query=widget", "limit=50")
    assert rc == 0 and r["count"] == 50 and r["total"] == 60 and r["offset"] == 0 and r["next_offset"] == 50
    assert r["truncated"] is True and "clamped" not in r
    first = pages_of(r)
    rc, r = kbdocs(mkb, "manual_search", "query=widget", "limit=50", "offset=50")
    assert rc == 0 and r["count"] == 10 and r["offset"] == 50 and r["next_offset"] is None and r["truncated"] is False
    assert sorted(first + pages_of(r)) == WIDGET_PAGES                        # every hit once, none twice
    rc, r = kbdocs(mkb, "manual_search", "query=widget", "limit=300")
    assert rc == 0 and r["count"] == 50 and r["clamped"] is True and r["next_offset"] == 50
    assert "limit 300" in r["clamped_note"] and "1-50" in r["clamped_note"] and "offset=50" in r["clamped_note"]
    rc, r = kbdocs(mkb, "manual_search", "query=widget", "limit=20", "offset=40")
    assert rc == 0 and r["count"] == 20 and r["next_offset"] is None
    rc, r = kbdocs(mkb, "manual_search", "query=widget", "offset=60")
    assert rc == 1 and r["error"] == "bad_param" and "60 hits" in r["message"]
    rc, r = kbdocs(mkb, "manual_search", "query=widget", "offset=-1")
    assert rc == 1 and r["error"] == "bad_param" and ">= 0" in r["message"]
    rc, r = kbdocs(mkb, "manual_search", "query=widget", "limit=0")         # below the range: refused, as before
    assert rc == 1 and r["error"] == "bad_param" and "1-50" in r["message"]
    # the builder's CLI search pages the same way
    p = builder("search", "widget", "--limit", "50", "--kb", str(mkb))
    assert p.returncode == 0 and "(50 of 60 hits; --offset 50 for the next ones)" in p.stdout, p.stdout
    p2 = builder("search", "widget", "--limit", "50", "--offset", "50", "--kb", str(mkb))
    assert p2.returncode == 0 and len([ln for ln in p2.stdout.splitlines() if " | p" in ln]) == 10
    assert "of 60 hits" not in p2.stdout


# ---------------------------------------------------------------------------------------------- N168
def test_n168_show_reads_on_with_offset(mkb):
    """N168 (0.8.14 retest): `manuals_db.py show` had no --offset — text past --max could only be reached by a larger
    --max; the cut marker did not say where to go on. --offset reads on, and the cut marker names it."""
    full = builder("show", "ED2008", "13-30", "--max", "100000", "--kb", str(mkb))
    assert full.returncode == 0 and "more chars" not in full.stdout
    head, _, body = full.stdout.rstrip("\n").partition("\n")
    assert head.startswith("ED2008.pdf |") and len(body) > 900
    got, off = "", 0
    while True:
        p = builder("show", "ED2008", "13-30", "--max", "300", "--offset", str(off), "--kb", str(mkb))
        assert p.returncode == 0, p.stderr
        h, _, part = p.stdout.rstrip("\n").partition("\n")
        assert h == head
        if "more chars; --offset " in part:
            text, _, marker = part.rpartition("\n… [")
            n_more, nxt = marker.split(" more chars; --offset ")
            nxt = int(nxt.rstrip("]"))
            assert nxt == off + 300 and int(n_more) == len(body) - nxt
            got += text
            off = nxt
        else:
            got += part
            break
    assert got == body
    p = builder("show", "ED2008", "13-30", "--offset", str(len(body) + 5), "--kb", str(mkb))
    assert p.returncode != 0 and "past the end" in p.stderr


# ---------------------------------------------------------------------------------------------- N169
def test_n169_ranges_and_defaults_are_in_the_help_and_refusals_name_the_way_on(mkb):
    """N169 (0.8.14 retest): `-h` of search / show / toc showed `--limit LIMIT` / `--max MAX` with no range and no
    default — the ranges were invisible until a call was refused, and the refusal did not say how to read on. The
    help gives range and default; a refusal names the paging route. The skill's help and refusals do the same for
    the manual_* commands."""
    # review of 0.8.15: search / product / toc read through the skill's commands — their ranges are the skill's
    for cmd, texts in (("search", ["--limit N", "1-50", "used as 50", "default 10", "--offset N", "default 0"]),
                       ("show", ["--max N", "1 or more", "default 4000", "--offset N", "default 0"]),
                       ("toc", ["--depth N", "1-6", "default 2"]),
                       ("product", ["--limit N", "1-50", "default 20"])):
        p = builder(cmd, "-h")
        assert p.returncode == 0 and all(t in " ".join(p.stdout.split()) for t in texts), (cmd, p.stdout)
    p = builder("show", "ED2008", "13", "--max", "0", "--kb", str(mkb))
    assert p.returncode != 0 and "--max must be 1 or more (got 0)" in p.stderr and "--offset" in p.stderr
    p = builder("search", "widget", "--offset", "-1", "--kb", str(mkb))
    assert p.returncode != 0 and "--offset must be 0 or more" in p.stderr
    p = builder("toc", "ED2008", "--depth", "0", "--kb", str(mkb))
    assert p.returncode != 0 and "--depth must be 1 or more" in p.stderr
    # the skill: ranges and defaults in help, the way on in a refusal
    rc, h = kbdocs(mkb, "help")
    ms = h["commands"]["manual_search"]["parameters"]
    assert "1-50" in ms["limit"] and "default 10" in ms["limit"] and "50 is used" in ms["limit"]
    assert ">= 0" in ms["offset"] and "default 0" in ms["offset"]
    assert "200-20000" in h["commands"]["manual_page"]["parameters"]["max_chars"]
    rc, r = kbdocs(mkb, "manual_page", "doc=ED2008", "pages=13-14", "max_chars=40000")
    assert rc == 1 and r["error"] == "bad_param" and "200-20000" in r["message"] and "offset=" in r["message"]


def test_n169_manual_page_reads_on_inside_a_page_with_offset(mkb):
    """N169 (0.8.14 retest) — the route a max_chars refusal names must exist: manual_page's `next` repeated the same
    call when the first page alone was longer than max_chars (pages=N-M again: cut at the same place). The page
    command reads on inside a page with offset, and `next` names it."""
    rc, whole = kbdocs(mkb, "manual_page", "doc=ED2008", "pages=13-15", "max_chars=20000")
    text = "".join(p["text"] for p in whole["pages"])
    got, args = "", ["pages=13-15", "max_chars=200"]
    for _ in range(20):
        rc, r = kbdocs(mkb, "manual_page", "doc=ED2008", *args)
        assert rc == 0, r
        got += "".join(p["text"] for p in r["pages"])
        if not r["truncated"]:
            break
        assert r["next_pages"] and r["next_offset"] is not None and f"offset={r['next_offset']}" in r["next"]
        args = [f"pages={r['next_pages']}", "max_chars=200", f"offset={r['next_offset']}"]
    assert got == text and not r["truncated"]
