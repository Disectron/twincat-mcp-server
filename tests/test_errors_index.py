"""N125 (0.8.16): the error-code index index\\errors.db and err_lookup — built from the knowledge folder's own indexes
(the Infosys index's pipe tables, manuals.db's AL status pages, esi.db's diagnosis text ids), the acceptance cases of
twincat-mcp-error-index-spec.md §8 on fixture data, the integrations (kb_search, ads_* error texts, docs search,
Scope log), the server tool and the skill command (one function, one answer).

The fixture indexes are written in the formats the plugin's builders write (the tables the errors builder reads):
the ADS return-code page in several books — three different tables among the copies (the variants of the real
index: a copy without 0x756, texts that differ, HRESULTs written '0x9811 0006' by one layout), the Win32 page with its
two-row header, a .NET enumeration, library code pages, the NC book, the LabVIEW numbering, a guideline page titled
'Error Codes' that holds no code."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from twincat_mcp import errors_index as X
from twincat_mcp import kb_layout as L
from twincat_mcp import kb_lock

# ------------------------------------------------------------------------------------------------ the fixture
# the rows the checks, the notes and the tests name — symbol and text as the local ADS table has them
ADS_REAL = {
    0x0: ("ERR_NOERROR", "No error."),
    0x6: ("ERR_TARGETPORTNOTFOUND", "Target port not found – ADS server is not started, not reachable or not "
                                    "installed."),
    0x7: ("ERR_TARGETMACHINENOTFOUND", "Target computer not found – AMS route was not found."),
    0x701: ("ADSERR_DEVICE_SRVNOTSUPP", "Service is not supported by the server."),
    0x704: ("ADSERR_DEVICE_INVALIDACCESS", "Reading or writing not permitted."),
    0x705: ("ADSERR_DEVICE_INVALIDSIZE", "Parameter size not correct."),
    0x707: ("ADSERR_DEVICE_NOTREADY", "Device is not ready to operate."),
    0x710: ("ADSERR_DEVICE_SYMBOLNOTFOUND", "Symbol not found."),
    0x724: ("ADSERR_DEVICE_LICENSENOTFOUND", "Missing license."),
    0x745: ("ADSERR_CLIENT_SYNCTIMEOUT", "Timeout has occurred – the remote terminal is not responding in the "
                                         "specified ADS timeout."),
    0x756: ("ADSERR_CLIENT_REQUESTCANCELLED", "The request was canceled."),
}
ADS_GROUPS = [("Global error codes", list(range(0x0, 0x1F))), ("Router error codes", list(range(0x500, 0x50E))),
              ("General ADS error codes", list(range(0x700, 0x73A)) + list(range(0x740, 0x74A)) + list(range(0x750, 0x757))),
              ("RTime error codes", list(range(0x1000, 0x1008)) + list(range(0x100D, 0x1011)) + list(range(0x1017, 0x101B)))]


def ads_page(spaced: bool = False, drop: tuple[int, ...] = (), texts: dict[int, str] | None = None) -> str:
    """The ADS return-code page as the Infosys index stores it (136 ADS codes in four tables, the positive HRESULTs,
    the Winsock codes = 139 codes of the ADS domain)."""
    texts = texts or {}
    out = ["# ADS Return Codes", "", "Grouping of error codes:", ""]
    for label, vals in ADS_GROUPS:
        out += [f"**{label}**", "", "| Hex | Dec | HRESULT | Name | Description |", "|---|---|---|---|---|"]
        for v in vals:
            if v in drop:
                continue
            sym, txt = ADS_REAL.get(v, (f"ADSERR_FIXTURE_{v:X}", f"Fixture text of {v:#x}."))
            hres = f"0x9811 {v:04X}" if spaced else f"0x9811{v:04X}"
            out.append(f"| 0x{v:X} | {v} | {hres} | {sym} | {texts.get(v, txt)} |")
        out.append("")
    out += ["**Specific positive HRESULT Return Codes:**", "", "| HRESULT | Name | Description |", "|---|---|---|",
            "| 0x0000_0000 | S_OK | No error. |", "| 0x0000_0001 | S_FALSE | No error. Example: incomplete result. |",
            "| 0x0000_0203 | S_PENDING | No error. Example: no result yet. |",
            "| 0x0000_0256 | S_WATCHDOG_TIMEOUT | No error. Example: a timeout occurred. |", "",
            "**TCP Winsock error codes**", "", "| Hex | Dec | Name | Description |", "|---|---|---|---|",
            "| 0x274C | 10060 | WSAETIMEDOUT | A connection timeout has occurred - the remote terminal did not respond. |",
            "| 0x274D | 10061 | WSAECONNREFUSED | Connection refused - the target computer rejected it. |",
            "| 0x2751 | 10065 | WSAEHOSTUNREACH | No route to host. |",
            "| More Winsock error codes: Win32 error codes |  |  |  |"]
    return "\n".join(out)


WIN32 = """# Win32 Error Codes

The following table provides a list of Win32 error codes.

| Error | Description |  |  |
|---|---|---|---|
| decimal | Hexadecimal | Name |  |
| 0 | 0x00000000 | ERROR_SUCCESS | The operation completed successfully. |
| 5 | 0x00000005 | ERROR_ACCESS_DENIED | Access is denied. |
| 6 | 0x00000006 | ERROR_INVALID_HANDLE | The handle is invalid. |
| 162 | 0x000000A2 | ERROR_SIGNAL_PENDING | A signal is already pending. |
| 1808 | 0x00000710 | ERROR_NOLOGON_WORKSTATION_TRUST_ACCOUNT | The account used is a computer account. |
| 10060 | 0x0000274C | WSAETIMEDOUT | A connection attempt failed. |
"""
DOTNET = """# AdsErrorCode Enumeration

ADS request return codes.

## Members

|  | Member name | Value | Description |
|---|---|---|---|
|  | NoError | 0 | No Error / Succeeded. Category: Global C++: ERR_NOERROR |
|  | TargetPortNotFound | 6 | The Target port is not found. Error code: 6 (0x006). Category: Global C++: ERR_TARGETPORTNOTFOUND |
|  | ClientSyncTimeOut | 1861 | Timeout. Category: Client C++: ADSERR_CLIENT_SYNCTIMEOUT |
|  | WrongMember | 7 | A member whose C++ name is not the table's. C++: ERR_SOMETHINGELSE |
"""
TCPIP = """# Internal error codes of the TwinCAT TCP/IP Connection Server

| Code (hex) | Code (dec) | Symbolic constant | Description |
|---|---|---|---|
| 0x00008001 | 32769 | TCPADSERROR_NOMOREENTRIES | No new sockets can be created (per FB_SocketListen and FB_SocketConnect). |
| 0x00000006 | 6 | TCPADSERROR_FIXTURE | A library code 6 (fixture: a number of several domains). |
"""
MBUS = """# Error codes

| Value (hex) | Value (dec) | Name | Description |
|---|---|---|---|
| 0x1001 | 4097 | eMBUSERROR_OK_FIXTURE | A good row. |
| 0x1008 | 4109 | eMBUSERROR_COM_TIMEOUT | COM timeout (hex and dec disagree in the source). |
"""
GUIDELINE = """# Error Codes

## Data type for error codes

| Declaration | Error range | No error | Message/info | Check functions |
|---|---|---|---|---|
| hrErrorCode : HRESULT; | <0 | >=0 | >0 | IF SUCCEEDED(hrErrorCode) THEN ... END_IF |
| nErrorID : UDINT; | >0 | 0 |  | IF nErrorID = 0 THEN ... END_IF |
"""
NC_GENERAL = """# General NC Errors

| Error(Hex) | Error(Dec) | Error type | Description |
|---|---|---|---|
| **4000** | **16384** | Internal | **Internal error** Internal system error in the NC on ring 0, no further details. |
| **4002 ** | **16386 ** | Internal | **NC Retain data error (persistent data)** An error occurred when loading the NC Retain data. |
"""
NC_OVERVIEW = """# Overview of NC Errors

| Error code ( hex ) | Description |
|---|---|
| **0x4000 - 0x4FFF: NC error code range** |  |
| 0x40nn | [General errors](https://infosys.beckhoff.com/content/1033/tc3ncerrcode/513122571.html) |
"""
LABVIEW = """# ADS Return Codes

**Global error codes**

| Hex | Dec | Name | Description |
|---|---|---|---|
| 0xFFFFE504 | -6908 | ERR_NOERROR | No error. |
| 0xFFFFDDBF | -8769 | ADSERR_CLIENT_SYNCTIMEOUT | Timeout has occurred – the remote terminal is not responding in the specified ADS timeout. |
"""
SCOPE_NOTE = """# Notes on recording

When the router runs out of memory the Scope View log shows Ads-Error 0xA2 and the recording stops."""

ADS_BOOKS_MAIN = ["tcplclib_tc2_system", "tcplclib_tc2_utilities", "tc3_ads_intro"]


def _pages() -> list[tuple[str, str, str, str | None, str]]:
    """(book, page id, title, library, markdown) of the fixture's Infosys index."""
    p: list[tuple[str, str, str, str | None, str]] = []
    for b in ADS_BOOKS_MAIN:
        p.append((b, "374277003", "ADS Return Codes", "Tc2_System" if b == "tcplclib_tc2_system" else None, ads_page()))
    # another layout of the same table (HRESULT '0x9811 0006'): the same rows — the copy is of the main variant
    p.append(("tcplclib_tc3_ipcdiag", "374277003", "ADS Return Codes", None, ads_page(spaced=True)))
    # one document placed four times in one book (tf6500): one copy
    for suffix in ("_30131813", "_2750866", "_24757801", "_21493617"):
        p.append(("tf6500_tc3_iec60870_5_10x", "374277003" + suffix, "ADS Return Codes", None, ads_page()))
    # the copies whose rows differ: tc3_c lacks 0x756 and words 0x6 otherwise; tc3_module writes 0x756 'cancelled'
    p.append(("tc3_c", "374277003", "ADS Return Codes", None, ads_page(
        spaced=True, drop=(0x756,), texts={0x6: "Target port not found – ADS server is not started or is not reachable."})))
    p.append(("tcplclib_tc3_module", "374277003", "ADS Return Codes", None, ads_page(
        spaced=True, texts={0x756: "The request was cancelled."})))
    for b in ("tcplclib_tc2_utilities", "tf6340_tc3_serial_communication"):
        p.append((b, "391166347", "Win32 Error Codes", None, WIN32))
    p += [("tc3_ads.net", "9408165643", "AdsErrorCode Enumeration", None, DOTNET),
          ("tf6310_tc3_tcpip", "84252939", "Internal error codes of the TwinCAT TCP/IP Connection Server", None, TCPIP),
          ("tcplclib_tc2_mbus", "3104318603", "Error codes", "Tc2_MBus", MBUS),
          ("tc3_plc_intro", "12049349259", "Error Codes", None, GUIDELINE),
          ("tc3ncerrcode", "513122571", "General NC Errors", None, NC_GENERAL),
          ("tc3ncerrcode", "1521556875", "Overview of NC Errors", None, NC_OVERVIEW),
          ("tf3710_tc3_interface_for_labview", "10041696011", "ADS Return Codes", None, LABVIEW),
          ("te13xx_tc3_scope", "4711", "Notes on recording", None, SCOPE_NOTE),
          ("tcplclib_tc2_system", "30866571", "ADSREAD", "Tc2_System", "# ADSREAD\n\nReads data from an ADS device.")]
    return p


def write_infosys(db: Path, pages: list[tuple] | None = None, built: str = "2026-10-20 16:11:47") -> Path:
    """An Infosys index with the tables the readers use (pages, meta, pages_fts), as the plugin's extractor writes
    them."""
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE pages(url TEXT PRIMARY KEY, book TEXT, page_id TEXT, lastmod TEXT, title TEXT, kind TEXT, library TEXT,
        markdown TEXT, text TEXT, code TEXT, decl TEXT, fetched REAL, version TEXT, crumb TEXT, words TEXT);
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE VIRTUAL TABLE pages_fts USING fts5(url UNINDEXED, title, text, code, words, content='pages',
        content_rowid='rowid', tokenize='unicode61');
    """)
    rows = pages if pages is not None else _pages()
    for b, pid, title, lib, md in rows:
        con.execute("INSERT INTO pages (url, book, page_id, title, kind, library, markdown, text, code, words) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?)", (f"https://infosys.beckhoff.com/content/1033/{b}/{pid}.html", b, pid,
                                              title, "Page", lib, md, md, "", ""))
    con.execute("INSERT INTO pages_fts(pages_fts) VALUES('rebuild')")
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("indexer_version", "7"), ("built", built), ("source_stamp", "content:fixture"), ("pages", str(len(rows))),
        ("state", "complete"), ("lcid", "1033"), ("package", "BKINFOSYS3_FIXTURE_EN-US.mshc"),
        ("package_date", "2025-07-24")])
    con.commit()
    con.close()
    return db


AL_TABLE = """5.3 AL Status Codes
For detailed information please refer to the EtherCAT system description.
0x0000 No error
0x0001 Unspecified error
0x0011 Invalid requested state change
0x0012 Unknown requested state
0x001B Sync manager watchdog
the watchdog of the sync manager elapsed
EtherCAT System Documentation Version 5.6 123"""
AL_REFERENCE = """6.2 Diagnostics
AL status codes: for detailed information please refer to the EtherCAT system description."""
AL_BROKEN = """4.1 AL status codes
0x0011 Invalid requested state change
0x0001 Unspecified error
0x001B Sync manager watchdog"""


def write_manuals(db: Path) -> Path:
    con = sqlite3.connect(db)
    con.executescript("""
    CREATE TABLE doc(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, size INTEGER, mtime INTEGER, sha256 TEXT,
        title TEXT, pages INTEGER, text_pages INTEGER, indexed TEXT, backend TEXT);
    CREATE TABLE page(doc INTEGER, page INTEGER, section TEXT, text TEXT, PRIMARY KEY(doc, page));
    CREATE TABLE toc(doc INTEGER, level INTEGER, title TEXT, page INTEGER);
    CREATE TABLE product(code TEXT, doc INTEGER, page INTEGER, n INTEGER);
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    for i, (name, page, section, text) in enumerate([
            ("EtherCAT_System_Documentation.pdf", 123, "5 Diagnosis > 5.3 AL Status Codes", AL_TABLE),
            ("EL3356-0010.pdf", 88, "6 Commissioning > 6.2 Diagnostics", AL_REFERENCE),
            ("EL2407.pdf", 41, "4 Diagnosis > 4.1 AL status codes", AL_BROKEN)], start=1):
        con.execute("INSERT INTO doc VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (i, f"C:/kb/manuals/{name}", name, 1000, 1, f"sha{i}", None, 200, 200, "2026-10-20", "pymupdf"))
        con.execute("INSERT INTO page VALUES (?,?,?,?)", (i, page, section, text))
        con.execute("INSERT INTO page VALUES (?,?,?,?)", (i, 1, "1 Foreword", "Foreword of the manual."))
    con.executemany("INSERT INTO meta VALUES (?, ?)", [("text_format", "6"), ("last_update", "2026-10-20 16:20:00"),
                                                       ("state", "idle")])
    con.commit()
    con.close()
    return db


def write_esi(db: Path) -> Path:
    con = sqlite3.connect(db)
    con.executescript("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT); CREATE TABLE msg (id INTEGER PRIMARY KEY, "
                      "hash TEXT UNIQUE, text_id INTEGER, message TEXT, data BLOB);")
    con.executemany("INSERT INTO msg (hash, text_id, message) VALUES (?,?,?)",
                    [("h1", 0x4101, "Terminal-Overtemperature"), ("h2", 0x4101, "Overtemperature (variant)"),
                     ("h3", 0x8001, "Supply voltage too low")])
    con.executemany("INSERT INTO meta VALUES (?, ?)", [("format", "2"), ("state", "complete"),
                                                       ("built", "2026-10-20 16:11:13"), ("sources_stamp", "esi1")])
    con.commit()
    con.close()
    return db


def make_kb(root: Path, manuals: bool = True, esi: bool = True) -> Path:
    kb = root / "kb"
    (kb / "manuals").mkdir(parents=True, exist_ok=True)
    write_infosys(L.index_file(kb, "infosys"))
    if manuals:
        write_manuals(L.index_file(kb, "manuals"))
    if esi:
        write_esi(L.index_file(kb, "esi"))
    return kb


def build(kb: Path, **kw) -> dict:
    return X.build(L.index_file(kb, "errors"), kb, log=lambda m: None, **kw)


@pytest.fixture
def kb(tmp_path):
    k = make_kb(tmp_path)
    r = build(k)
    assert r["result"] == "built", r
    return k


def lookup(kb: Path, q: str, **kw) -> dict:
    return X.err_lookup(q, kb=kb, **kw)


# ------------------------------------------------------------------------------------------------ §3–§5 the build
def test_build_from_the_local_indexes_domains_variants_aliases_and_the_report(kb):
    """N125 §4/§5 (correction 2: built from the plugin's own indexes, each one's identity recorded): one row per code
    per variant, the variants with their books (the copies of one document with the same rows — a copy in another
    layout among them — are one variant), every candidate page in source_report (0 rows with why), the AL status
    links, the diagnosis text ids linked, the aliases (symbol, HRESULT, .NET name)."""
    st = X.state(L.index_file(kb, "errors"), kb, detail=True)
    assert st["state"] == "complete" and st["format"] == X.FORMAT, st
    assert set(st["sources"]) == {"infosys", "manuals", "esi"} and st["sources"]["infosys"]["format"] == "7", st
    dd = st["domains_detail"]
    assert dd["ads"]["codes"] == 139 and dd["ads"]["variants"] == 3, dd["ads"]
    assert dd["win32"]["codes"] == 6 and dd["hresult"]["codes"] == 4 and dd["nc"]["codes"] == 2, dd
    assert dd["labview"]["codes"] == 2 and dd["ethercat_al"]["codes"] == 5 and dd["ethercat_diag"]["links"] == 2, dd
    assert dd["tf6310_tc3_tcpip/84252939"]["codes"] == 2, dd
    con = X.connect_ro(L.index_file(kb, "errors"))
    try:
        v = {r["variant"]: (json.loads(r["books"]), r["main"]) for r in con.execute(
            "SELECT * FROM variant WHERE domain = 'ads'")}
        assert v["374277003:v1"] == (sorted(ADS_BOOKS_MAIN + ["tcplclib_tc3_ipcdiag", "tf6500_tc3_iec60870_5_10x"]), 1)
        assert {tuple(b) for b, _m in v.values()} >= {("tc3_c",), ("tcplclib_tc3_module",)}
        rep = {r["ref"]: dict(r) for r in con.execute("SELECT * FROM source_report")}
        g = rep["tc3_plc_intro/12049349259"]
        assert g["rows_found"] == 0 and g["status"] == "0 rows" and "Declaration | Error range" in g["reason"], g
        assert rep["tc3ncerrcode/1521556875"]["rows_found"] == 0                       # '0x40nn' is no code
        assert rep["tcplclib_tc2_mbus/3104318603"]["inconsistent"] == 1                  # hex ≠ dec: not stored
        assert rep["tc3_ads.net/9408165643"]["status"] == "alias" and "C++ name" in rep["tc3_ads.net/9408165643"]["reason"]
        assert rep["EL3356-0010.pdf#p88"]["status"] == "link" and "no table" in rep["EL3356-0010.pdf#p88"]["reason"]
        assert rep["EL2407.pdf#p41"]["status"] == "link" and "ascending" in rep["EL2407.pdf#p41"]["reason"]
        al = con.execute("SELECT text FROM code WHERE domain = 'ethercat_al' AND value_int = 0x1B").fetchone()[0]
        assert al == "Sync manager watchdog the watchdog of the sync manager elapsed", al
        al = {r[0] for r in con.execute("SELECT ref FROM link WHERE domain = 'ethercat_al'")}
        assert al == {"EL3356-0010.pdf#p88", "EL2407.pdf#p41"}, al
        alias = {(r["kind"], r["form"], r["domain"], r["value_int"]) for r in con.execute("SELECT * FROM alias")}
        assert {("symbol", "adserr_client_synctimeout", "ads", 0x745), ("hresult", "0x98110745", "ads", 0x745),
                ("dotnet", "targetportnotfound", "ads", 6),
                ("symbol", "adserr_client_synctimeout", "labview", -8769)} <= alias, alias
        assert not [a for a in alias if a[1] == "wrongmember"]                  # its C++ name is not the table's
    finally:
        con.close()


def test_the_table_parser_finds_columns_by_what_they_mean():
    """N125 correction 3: columns found by meaning (hex / dec / HRESULT / symbol / text), never by position; a value is a
    single code — a range, a comparison, an offset or a number in other text is none; hex and decimal that disagree
    make the row inconsistent (counted, not stored)."""
    def rows(md: str) -> list[dict]:
        (label, t), = X.page_tables(md)
        return X.parse_table(t, label)["rows"]
    # Win32: a header row with empty columns over a row that names them ('decimal | Hexadecimal | Name | ')
    w = rows(WIN32)
    assert [(r["value"], r["symbol"], r["text"]) for r in w][2] == (6, "ERROR_INVALID_HANDLE", "The handle is invalid.")
    # EtherNet/IP: the 'Error' column holds the names, 'Code hex / (decimal)' both numbers
    e = rows("| Error | Code hex / (decimal) | Description | Remedy/meaning |\n|---|---|---|---|\n"
             "| CN_ORC_ALREADY_USED | 0x100 / (256) | Connection already in use | close it |\n"
             "| CN_ORC_BAD_TRANSPORT | 0x103 / (259) | Transport type not supported | - |")
    assert [(r["value"], r["symbol"], r["text"]) for r in e] == [
        (256, "CN_ORC_ALREADY_USED", "Connection already in use"), (259, "CN_ORC_BAD_TRANSPORT", "Transport type not supported")]
    # Windows sockets: the name glued to its number in a 'Return Value' column
    s = rows("| Return Value | Description |\n|---|---|\n| WSAEINTR10004 | Interrupted function call. |\n"
             "| WSAEACCES 10013 | Permission denied. |")
    assert [(r["value"], r["symbol"]) for r in s] == [(10004, "WSAEINTR"), (10013, "WSAEACCES")]
    # NC: bare hex digits in a 'Error(Hex)' column, bold cells; the error type is the kind
    n = rows(NC_GENERAL)
    assert [(r["value"], r["hex"], r["kind"]) for r in n] == [(0x4000, "0x4000", "Internal"), (0x4002, "0x4002", "Internal")]
    # LabVIEW and a 16-bit table: a negative decimal and its two's complement in the hex's width
    lv = rows(LABVIEW)
    assert [(r["value"], r["hex"]) for r in lv] == [(-6908, "0xFFFFE504"), (-8769, "0xFFFFDDBF")]
    ml = rows("| Error code | Hex | Description |\n|---|---|---|\n| -8 | FFF8 | Invalid AMS Net ID |")
    assert [(r["value"], r["hex"]) for r in ml] == [(-8, "0xFFF8")]
    # IO-Link: 'Error Code | Additional Code' — two bytes of one code (the page's own example: 0x80 0x11 = 0x8011)
    io = rows("| Incident | Error Code | Additional Code | Name | Definition |\n|---|---|---|---|---|\n"
              "| Index not available | 0x80 | 0x11 | IDX_NOTAVAIL | A read or write access to a not existing Index. |")
    assert [(r["value"], r["symbol"], r["kind"]) for r in io] == [(0x8011, "IDX_NOTAVAIL", "Index not available")]
    # the other ADS layout: HRESULT '0x9811 0006'; an HRESULT written '0' is 0
    a = rows("| Hex | Dec | HRESULT | Name | Description |\n|---|---|---|---|---|\n| 0x6 | 6 | 0x9811 0006 | E | T x |")
    assert a[0]["hresult"] == "0x98110006"
    h = rows("| code (HRESULT) | symbol | description / solution |\n|---|---|---|\n| 0 | eCMA_OK | No Error, all OK |\n"
             "| 16#9852_0101 | eCMA_ErrConfig_InvalidOwnID | invalid transfer own ID |")
    assert [r["value"] for r in h] == [0, 0x98520101]
    # not single codes: ranges, comparisons, offsets, other text — and no row is guessed
    for cell in ("0x0000 ... 0x7FFF", "65537 - 131071", "< 0x8000", ">=0", "16#00000010 + Argument number (1..9)",
                 "Value range: [0, 0xFFFF]", "4.000 s", "0x40nn"):
        assert X.cell_numbers(cell, "code") is None and X.cell_numbers(cell, "hex") is None, cell
    t = X.parse_table([["Code (hex)", "Code (dec)", "Description"], ["0x1008", "4109", "COM timeout"],
                       ["0x00008001", "32768 + 1 := 32769", "Internal error"]])
    assert [r["value"] for r in t["rows"]] == [32769] and len(t["inconsistent"]) == 1, t
    # a header that is a row of data, a column found by its numbers
    c = X.parse_table([["ID 70329", "Actual value change of sensor signal greater than limit"],
                       ["ID 70330", "Sensor signal lost"]])
    assert [(r["value"], r["text"]) for r in c["rows"]] == [
        (70329, "Actual value change of sensor signal greater than limit"), (70330, "Sensor signal lost")], c


def test_al_status_codes_rows_where_the_table_parses_else_page_links():
    """N125, user decision: AL status codes as rows where the PDF manual's table parses cleanly, otherwise a page link —
    with the reason."""
    ok = X.parse_al_page(AL_TABLE)
    assert [r["value"] for r in ok["rows"]] == [0, 1, 0x11, 0x12, 0x1B] and "reason" not in ok
    assert ok["rows"][-1]["text"].endswith("elapsed") and "Version 5.6" not in ok["rows"][-1]["text"]
    assert "no table" in X.parse_al_page(AL_REFERENCE)["reason"]
    assert "ascending" in X.parse_al_page(AL_BROKEN)["reason"]
    assert "only 1 line" in X.parse_al_page("AL status\n0x0011 Invalid requested state change")["reason"]


# the EtherCAT system documentation's layout (8.2.1 EtherCAT AL Status Codes, its real text): one section per code with
# labelled fields, sections running on over page breaks, the next numbered heading ending the last one
AL_SECTIONS = [
    (307, """8.2 EtherCAT Diagnosis
8.2.1 EtherCAT AL Status Codes
8.2.1.1 Error Code 0x0000
Meaning
No error
Description
No error
Current State (or state change)
Any
Resulting state
Current state
Solution
n/a
8.2.1.2 Error Code 0x0001
Meaning
Unspecified error
Description
No error code is defined for occurred error"""),
    (308, """Current State (or state change)
Any
Resulting state
Any + E
Solution
Read user manual or contact device manufacturer
8.2.1.5 Error Code 0x0011
Meaning
Invalid requested state change
Description
The EtherCAT State Machine (ESM) defines which state changes are allowed. All other state changes are
not allowed
Current State (or state change)
P→S, I→O, P→O
Resulting state
Current State + E"""),
    (309, """Solution
Go step-by-step from the original state to the desired state.
8.3 Training
Beckhoff offers trainings.""")]
AL_SYSTEM_DOC = "Documentation Introduction, Mounting, Wiring, Configuration, Operation, Diagnosis.pdf"


def test_al_status_codes_one_section_per_code_over_page_breaks():
    """Real data (0.8.16): the EtherCAT system documentation gives each AL status code a section of labelled fields —
    the table parser read none of its 52 codes. Each code: its Meaning as the text, the other fields as detail (n/a
    left out), the page its section starts on; a field running on over a page break is one field; the next numbered
    heading ends the last section. Fewer than three sections, a section without a Meaning or codes out of order: none."""
    r = X.parse_al_sections(AL_SECTIONS)
    assert [(x["value"], x["text"], x["page"]) for x in r["rows"]] == [
        (0, "No error", 307), (1, "Unspecified error", 307), (0x11, "Invalid requested state change", 308)], r
    assert r["rows"][0]["detail"] == {"description": "No error", "current_state": "Any",
                                      "resulting_state": "Current state"}, r["rows"][0]
    assert r["rows"][1]["detail"] == {"description": "No error code is defined for occurred error",
                                      "current_state": "Any", "resulting_state": "Any + E",
                                      "solution": "Read user manual or contact device manufacturer"}, r["rows"][1]
    d = r["rows"][2]["detail"]
    assert d["description"].endswith("All other state changes are not allowed"), d
    assert d["solution"] == "Go step-by-step from the original state to the desired state.", d
    assert r["rows"][2]["hex"] == "0x0011" and r["rows"][2]["kind"] == "AL status code"
    assert r["pages"] == [307, 308, 309]
    assert X.parse_al_sections(AL_SECTIONS[:1])["rows"] == []                       # two sections only
    swapped = [(307, AL_SECTIONS[0][1].replace("0x0001", "0x0021"))] + AL_SECTIONS[1:]
    assert X.parse_al_sections(swapped)["rows"] == []                               # 0x0021 before 0x0011
    no_meaning = [(307, AL_SECTIONS[0][1].replace("Meaning\nUnspecified error\n", ""))] + AL_SECTIONS[1:]
    assert X.parse_al_sections(no_meaning)["rows"] == []
    assert X.parse_al_page(AL_SECTIONS[1][1])["rows"] == []                         # what the table parser made of it


def test_the_section_layout_is_indexed_with_its_fields_and_pages(tmp_path):
    """The system documentation's AL codes in errors.db: one row per code with its own page as ref and its fields as
    detail — and its pages are not also turned into page links."""
    k = make_kb(tmp_path, manuals=False)
    db = L.index_file(k, "manuals")
    write_manuals(db)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM page WHERE doc IN (SELECT id FROM doc WHERE name = 'EtherCAT_System_Documentation.pdf')")
    con.execute("DELETE FROM doc WHERE name = 'EtherCAT_System_Documentation.pdf'")
    con.execute("INSERT INTO doc VALUES (9,?,?,?,?,?,?,?,?,?,?)", (f"C:/kb/manuals/{AL_SYSTEM_DOC}", AL_SYSTEM_DOC,
                                                                 1000, 1, "sha9", None, 400, 400, "2026-10-20", "pymupdf"))
    for page, text in AL_SECTIONS:
        con.execute("INSERT INTO page VALUES (9,?,?,?)", (page, "8 Appendix > 8.2 EtherCAT Diagnosis", text))
    con.commit()
    con.close()
    assert build(k)["result"] == "built"
    r = lookup(k, "AL status 0x0011")
    m = r["matches"][0]
    assert r["documented"] is True and m["domain"] == "ethercat_al" and m["text"] == "Invalid requested state change"
    assert m["ref"] == f"{AL_SYSTEM_DOC}#p308" and m["detail"]["resulting_state"] == "Current State + E", m
    assert m["detail"]["solution"].startswith("Go step-by-step"), m
    m = lookup(k, "AL status code 0x0001")["matches"][0]
    assert m["ref"] == f"{AL_SYSTEM_DOC}#p307" and m["detail"]["solution"].startswith("Read user manual"), m
    assert "detail" not in lookup(k, "0x745")["matches"][0]
    con = X.connect_ro(L.index_file(k, "errors"))
    try:
        assert con.execute("SELECT COUNT(*) FROM link WHERE domain = 'ethercat_al' AND ref LIKE ?",
                           (f"{AL_SYSTEM_DOC}#%",)).fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM code WHERE domain = 'ethercat_al' AND book = ?",
                           (AL_SYSTEM_DOC,)).fetchone()[0] == 3
    finally:
        con.close()


def test_a_hex_number_after_a_domain_name_is_a_code_and_c0xxx_a_compiler_message(kb):
    """0.8.16 acceptance on real data: 'AL status 0x0011' was read as no code ('status' is no code word) and 'C0373' as
    no code at all. A hex number right after a domain's name is that domain's code; a decimal one still needs a code
    word ('ADS 851' is no code — N121). C0xxx is a compiler message: not documented locally, said so for the compiler
    alone; C6015 (an IPC) is none."""
    assert [c["token"] for c in X.read_query("AL status 0x0011")["codes"]] == ["0x0011"]
    assert [c["token"] for c in X.read_query("ADS 0x745")["codes"]] == ["0x745"]
    assert X.read_query("ADS 851")["codes"] == [] and X.read_query("CoE index 0x1018")["codes"] == []
    assert X.read_query("Error C0373: …")["ids"] == [{"token": "C0373", "domain": "compiler"}]
    assert X.read_query("C6015 fan error")["ids"] == []
    r = lookup(kb, "AlStatus 0x0011")
    assert r["documented"] is True and r["matches"][0]["domain"] == "ethercat_al", r
    r = lookup(kb, "C0373")
    assert r["documented"] is False and r["domain_named"] == ["compiler"] and r["matches"] == [], r
    assert list(r["no_local_source"]) == ["compiler"] and r["note"].startswith("C0373: not in the local"), r
    assert "text" not in r["ranking"] and "searched_words" not in r, r
    r = lookup(kb, "ADS 851")
    assert r["documented"] is False and "no_local_source" not in r, r      # the compiler's gap is not the answer


# ------------------------------------------------------------------------------------------------ §8 acceptance
def test_acceptance_1_every_notation_is_one_ads_row_with_its_page(kb):
    """N125 §8.1: 0x745, 1861, 16#745, 0x00000745, 0x98110745, ADSERR_CLIENT_SYNCTIMEOUT, 'ADSError 1861 (0x745): Timeout'
    → one ads row, the same text, ref …/374277003 (the LabVIEW table's own number for the symbol is listed apart)."""
    texts = set()
    for q in ("0x745", "1861", "16#745", "0x00000745", "0x98110745", "ADSERR_CLIENT_SYNCTIMEOUT",
              "ADSError 1861 (0x745): Timeout", "ClientSyncTimeOut"):
        r = lookup(kb, q)
        ads = [m for m in r["matches"] if m["domain"] == "ads"]
        assert r["documented"] is True and len(ads) == 1 and r["matches"][0]["domain"] == "ads", (q, r)
        m = ads[0]
        assert m["symbol"] == "ADSERR_CLIENT_SYNCTIMEOUT" and m["hex"] == "0x745" and m["dec"] == "1861", (q, m)
        assert m["hresult"] == "0x98110745" and m["ref"].endswith("/374277003") and m["url"].endswith(".html"), m
        texts.add(m["text"])
    assert len(texts) == 1
    r = lookup(kb, "ADSERR_CLIENT_SYNCTIMEOUT")
    assert r["domain_named"] == ["ads"] and [o["domain"] for o in r["other_domains"]] == ["labview"], r


def test_acceptance_2_a_number_of_two_domains_lists_both_the_message_ranks(kb):
    """N125 §8.2: '6' → ads ERR_TARGETPORTNOTFOUND and win32 ERROR_INVALID_HANDLE, both listed (never one picked
    silently); 'ADS error 6' → ads first, the others listed apart, and `ranking` says why."""
    r = lookup(kb, "6")
    assert [(m["domain"], m.get("symbol")) for m in r["matches"]][:2] == [("ads", "ERR_TARGETPORTNOTFOUND"),
                                                                         ("win32", "ERROR_INVALID_HANDLE")], r
    assert "tf6310_tc3_tcpip/84252939" in [m["domain"] for m in r["matches"]] and "no word" in r["ranking"], r
    r = lookup(kb, "ADS error 6")
    assert [m["domain"] for m in r["matches"]] == ["ads"] and r["domain_named"] == ["ads"], r
    assert [o["domain"] for o in r["other_domains"]][:1] == ["win32"] and "names ads" in r["ranking"], r
    r = lookup(kb, "6", domain="win32")
    assert [m["domain"] for m in r["matches"]] == ["win32"], r


def test_acceptance_3_a_number_beside_port_is_no_code(kb):
    """N125 §8.3 (N121): 'ADS error on port 851' and 'port 851' hold no code 851 — a number is a code only as the whole
    query or next to a word that says so."""
    for q in ("ADS error on port 851", "port 851"):
        r = lookup(kb, q)
        assert r["read_as"] == [] and all(m["value"] != 851 for m in r.get("matches", [])), (q, r)
        # review of 0.8.16 (user decision): no code, so nothing documented — the 'port' codes are text matches only
        assert r["documented"] is False and r["matches"] == [], (q, r)
    assert lookup(kb, "851")["read_as"][0]["value"] == 851                       # the whole query: a code (N122)


def test_acceptance_4_ads_error_0xA2_is_not_documented_nothing_invented(kb):
    """N125 §8.4: 'Ads-Error 0xA2' → documented: false for the domain the message names (ads has no 0xA2); no text is
    invented. The same number is Win32 ERROR_SIGNAL_PENDING in the local Win32 table — listed apart as another
    domain (the spec's 'not in any local table' is not so in the real index), and the full-text hits only mention it."""
    r = lookup(kb, "Ads-Error 0xA2")
    assert r["documented"] is False and r["matches"] == [] and r["domain_named"] == ["ads"], r
    assert [(o["domain"], o["symbol"]) for o in r["other_domains"]] == [("win32", "ERROR_SIGNAL_PENDING")], r
    assert "not in the local Beckhoff documentation of ads" in r["note"] and "no meaning" in r["note"], r
    assert "te13xx_tc3_scope/4711" in [h["ref"] for h in r["fulltext"]["hits"]], r
    assert "no meaning is taken from them" in r["fulltext"]["note"], r


def test_acceptance_5_sdo_abort_codes_are_not_documented_locally(kb):
    """N125 §8.5 (user decision): 0x06020000 (CoE SDO abort) → documented: false, the domains searched listed, never a
    meaning — no local source documents SDO abort codes."""
    r = lookup(kb, "0x06020000")
    assert r["documented"] is False and r["matches"] == [] and "ads" in r["searched"], r
    assert "no local source" in r["no_local_source"]["coe_sdo_abort"], r
    r = lookup(kb, "SDO abort code 0x06020000")
    assert r["documented"] is False and r["domain_named"] == ["coe_sdo_abort"] and "coe_sdo_abort" in r["no_local_source"]


def test_acceptance_6_variants_every_text_with_its_books_and_the_copies_that_lack_it(kb):
    """N125 §8.6: 0x756 is found, with the copies that lack it (tc3_c); a code whose text differs between the copies
    answers with every text and the books of each — the main one the text of the most books."""
    m = lookup(kb, "0x756")["matches"][0]
    assert m["symbol"] == "ADSERR_CLIENT_REQUESTCANCELLED" and m["text"] == "The request was canceled.", m
    assert [x["books"] for x in m["missing_in"]] == [["tc3_c"]], m
    assert [(t["text"], t["main"]) for t in m["texts"]] == [("The request was canceled.", True),
                                                            ("The request was cancelled.", False)], m
    assert m["texts"][1]["books"] == ["tcplclib_tc3_module"], m
    six = lookup(kb, "ADS error 6")["matches"][0]
    assert len(six["texts"]) == 2 and six["texts"][1]["books"] == ["tc3_c"], six


def test_acceptance_7_8_kb_search_known_error_names_and_no_licence_note(kb, tmp_path):
    """N125 §8.7: kb_search 'ADS error 1808 fbCounter' → known_error 1808 (the plugin's note, labelled, and what the
    errors index documents) and the hits for fbCounter. §8.8: 'License' → no licence known_error (N129 / N130 hold)."""
    from tests.test_knowledge import _pou
    from twincat_mcp import knowledge as K
    k = K.Knowledge(tmp_path / "home", str(kb))
    src = tmp_path / "err"
    _pou(src, "PRG_Machine", "PROGRAM PRG_Machine\nVAR\n    fbCounter : FB_Counter;\nEND_VAR", "fbCounter();")
    k.add_project(str(src), "err")
    r = k.search("ADS error 1808 fbCounter")
    ke = r["known_error"]
    assert ke["code"] == "ADS return code 0x710 (1808) ADSERR_DEVICE_SYMBOLNOTFOUND" and ke["origin"] == "plugin", ke
    assert ke["documented"][0]["symbol"] == "ADSERR_DEVICE_SYMBOLNOTFOUND" and ke["documented"][0]["text"] == \
        "Symbol not found." and ke["documented"][0]["ref"].endswith("/374277003"), ke
    assert [h["unit"] for h in r["hits"]] == ["PRG_Machine"], r
    assert "known_error" not in k.search("License")
    r = k.search("Ads-Error 0xA2")                     # documented in Win32 only — not for a message about ADS
    assert r["known_error"] is None, r
    # a code the plugin has no note on, documented: known_error names it from the documentation
    r = k.search("ADS error 1801")
    assert "cause" not in r["known_error"] and r["known_error"]["documented"][0]["symbol"] == "ADSERR_FIXTURE_709", r


def test_acceptance_9_the_stored_text_has_no_replacement_character(kb, tmp_path):
    """N125 §8.9 (correction 1): the text of 0x6 is stored as the Infosys index has it — with its en dash, no U+FFFD;
    a source whose 0x6 text holds U+FFFD fails the golden check (the decode defect is named, nothing replaced)."""
    m = lookup(kb, "0x6")["matches"][0]
    assert "\ufffd" not in m["text"] and "\u2013" in m["text"], m
    con = X.connect_ro(L.index_file(kb, "errors"))
    try:
        assert con.execute("SELECT COUNT(*) FROM code WHERE text LIKE ?", ("%\ufffd%",)).fetchone()[0] == 0
    finally:
        con.close()
    bad = make_kb(tmp_path / "bad", manuals=False, esi=False)
    pages = [(b, p, t, lib, md.replace("not started, not reachable", "not started \ufffd not reachable"))
             for b, p, t, lib, md in _pages()]
    L.index_file(bad, "infosys").unlink()
    write_infosys(L.index_file(bad, "infosys"), pages)
    r = build(bad)
    assert r["result"] == "incomplete" and any("U+FFFD" in f for f in r["golden_failures"]), r


def test_acceptance_10_kb_status_shows_the_errors_index(kb, tmp_path):
    """N125 §8.10: kb_status → the errors index: rows per domain, variants, the sources with 0 rows, build date, format."""
    from twincat_mcp import knowledge as K
    st = K.Knowledge(tmp_path / "home", str(kb)).status()["errors"]
    assert st["state"] == "complete" and st["format"] == "1" and st["built"], st
    assert st["domains_detail"]["ads"] == {"title": "ADS return codes", "origin": "infosys", "documents": 1,
                                           "codes": 139, "rows": 139 * 3 - 1, "variants": 3}, st["domains_detail"]["ads"]
    zero = {z["ref"]: z for z in st["zero_row_sources_list"]["first"]}
    assert "tc3_plc_intro/12049349259" in zero and zero["EL2407.pdf#p41"]["status"] == "link", zero
    assert st["al_status_links"] == 2 and "codes in" in st["message"], st


def test_acceptance_11_a_rebuild_that_loses_a_source_page_fails_and_keeps_the_index(kb):
    """N125 §8.11 / §5.4: rebuilt with a source page removed → the golden check fails, the index in use stays as it was,
    the failure is reported (state, kb_status); a domain with fewer codes than at the last build is never accepted
    silently — only a forced rebuild accepts it, and records it."""
    db = L.index_file(kb, "errors")
    before = db.read_bytes()
    inf = L.index_file(kb, "infosys")
    con = sqlite3.connect(inf)
    con.execute("DELETE FROM pages WHERE page_id = '84252939'")                      # a library page
    con.execute("UPDATE meta SET value = '2026-10-21 09:00:00' WHERE key = 'built'")
    con.commit()
    con.close()
    st = X.state(db, kb)
    assert st["state"] == "rebuild_needed" and "Infosys index was rebuilt" in st["reason"], st
    r = build(kb)
    assert r["result"] == "incomplete" and any("tf6310_tc3_tcpip/84252939: 2 codes at the last build, 0 now" in f
                                               for f in r["golden_failures"]), r
    assert db.read_bytes() == before
    st = X.state(db, kb)
    assert st["rebuild"]["state"] == "incomplete" and "FAILED its checks" in st["message"], st
    assert "tf6310_tc3_tcpip/84252939" in [m["domain"] for m in lookup(kb, "0x00008001")["matches"]]   # as it is
    r = build(kb, force=True)
    assert r["result"] == "built" and r["accepted_drops"] == ["tf6310_tc3_tcpip/84252939: 2 codes at the last "
                                                              "build, 0 now"], r
    assert X.state(db, kb)["accepted_drops"]
    assert [m["domain"] for m in lookup(kb, "0x00008001")["matches"]] == ["ethercat_diag"]     # the page's row is gone
    # a page a golden check needs: no rebuild accepts its loss, forced or not
    con = sqlite3.connect(inf)
    con.execute("DELETE FROM pages WHERE page_id = '391166347'")
    con.execute("UPDATE meta SET value = '2026-10-21 10:00:00' WHERE key = 'built'")
    con.commit()
    con.close()
    before = db.read_bytes()
    r = build(kb, force=True)
    assert r["result"] == "incomplete" and any("win32 0x6: not found, ERROR_INVALID_HANDLE expected" in f
                                               for f in r["golden_failures"]), r
    assert db.read_bytes() == before and not any(f.startswith("win32: ") for f in r["golden_failures"])


# ------------------------------------------------------------------------------------------------ rules of the build
def test_sources_identity_rebuilt_missing_and_appearing(kb):
    """N125 correction 2: each source's identity is recorded; one rebuilt or added since → rebuild_needed with the reason;
    one gone → said, and the index answers from what it has; no source at all → no build."""
    db = L.index_file(kb, "errors")
    con = sqlite3.connect(L.index_file(kb, "esi"))
    con.execute("UPDATE meta SET v = '2026-10-21 08:00:00' WHERE k = 'built'")
    con.commit()
    con.close()
    st = X.state(db, kb)
    assert st["state"] == "rebuild_needed" and "esi.db was rebuilt since" in st["reason"], st
    assert "kb_rebuild(index='errors')" in st["message"]
    L.index_file(kb, "esi").unlink()
    L.index_file(kb, "manuals").unlink()
    st = X.state(db, kb)
    assert st["state"] == "complete" and "manuals.db is missing now" in st["message"], st
    assert lookup(kb, "AL status code 0x0011")["matches"][0]["domain"] == "ethercat_al"     # from then
    k2 = make_kb(db.parent.parent.parent / "k2", manuals=False, esi=False)
    r = build(k2)
    assert r["result"] == "built" and r["missing_sources"] == ["manuals", "esi"], r
    st = X.state(L.index_file(k2, "errors"), k2)
    assert "built without manuals.db, esi.db" in st["message"], st
    write_manuals(L.index_file(k2, "manuals"))
    st = X.state(L.index_file(k2, "errors"), k2)
    assert st["state"] == "rebuild_needed" and "manuals.db exists now" in st["reason"], st
    r = lookup(k2, "AL status code 0x0011")
    assert "not indexed: manuals.db was missing" in r["no_local_source"]["ethercat_al"], r
    empty = db.parent.parent.parent / "empty"
    (empty / "index").mkdir(parents=True)
    with pytest.raises(X.ErrError, match="no source index"):
        build(empty)


def test_one_build_at_a_time_and_a_build_of_a_source_waits(kb):
    """N125 §5.5: behind kb_lock as esi.db: a second build is refused with what runs; while a source index is being built the
    errors index is not built from it."""
    db = L.index_file(kb, "errors")
    with kb_lock.IndexLock(X.side_files(db)[1]):
        with pytest.raises(X.ErrError, match="another errors-index build is running"):
            build(kb, force=True)
        assert X.state(db, kb)["rebuild"]["state"] == "running"
    from twincat_mcp import infosys_build as IB
    with kb_lock.IndexLock(IB.side_files(L.index_file(kb, "infosys"))[1]):
        ok, why = X.sources_ready(X.identities(kb), kb)
        assert not ok and "the Infosys index is being built" in why
        with pytest.raises(X.ErrError, match="being built"):
            build(kb, force=True)


def test_an_interrupted_build_replaces_nothing_and_the_next_starts_over(kb, monkeypatch):
    """N125 §5.5 / rule 6 (as esi.db): a build stopped half-way leaves the index in use untouched and its work file
    reported as stale; the next build discards the work file and starts over."""
    db = L.index_file(kb, "errors")
    before = db.read_bytes()

    def boom(self, path):
        raise KeyboardInterrupt
    monkeypatch.setattr(X._Build, "esi", boom)
    with pytest.raises(KeyboardInterrupt):
        build(kb, force=True)
    assert db.read_bytes() == before
    st = X.state(db, kb)
    assert st["rebuild"]["state"] == "stale" and "the next build discards it" in st["message"], st
    monkeypatch.undo()
    r = build(kb)
    assert r["result"] == "up_to_date" and r["discarded"] and not X.side_files(db)[0].exists(), r


# ------------------------------------------------------------------------------------------------ §6 lookup rules
def test_plugin_notes_are_labelled_and_checked_against_the_documentation(kb, tmp_path):
    """N125 / user rule: KNOWN_ERRORS and _ADS_HINTS are annotation rows — shown as the plugin's own note, never as
    Beckhoff's text, with the build's check (verified_on); a note on an undocumented code says so. Without an index:
    the notes only, said so (documented null)."""
    m = lookup(kb, "0x745")["matches"][0]
    pn = m["plugin_note"]
    assert pn["origin"] == "plugin" and pn["label"] == X.PLUGIN_NOTE and pn["verified_on"], pn
    assert pn["cause"].startswith("the target did not answer") and pn["cause"] != m["text"], pn
    con = X.connect_ro(L.index_file(kb, "errors"))
    try:
        rows = {(r["domain"], r["value_int"]): r["check_note"] for r in con.execute("SELECT * FROM annotation")}
    finally:
        con.close()
    assert rows[("hresult", 0x80010001)] == "hresult 0x80010001 is not documented locally", rows
    assert rows[("ads", 0x724)] == "the documented row has the note's symbol"
    r = lookup(kb, "ExecuteCommand: (HRESULT 0x80010001) call was rejected")
    assert r["documented"] is False and r["plugin_notes"][0]["cause"].startswith("the XAE shell is busy"), r
    r = lookup(kb, "C0046: Identifier 'nFoo' not defined")
    assert r["plugin_notes"][0]["cause"].startswith("'nFoo' is not declared") and r["plugin_notes"][0]["origin"] == "plugin"
    r = X.err_lookup("ADSError 1861", kb=tmp_path / "nokb")
    assert r["documented"] is None and "no errors index" in r["note"], r
    assert r["plugin_notes"][0]["cause"].startswith("the target did not answer") and \
        r["plugin_notes"][0]["verified_on"] is None, r
    # the notes are one table: knowledge's views are made of it
    from twincat_mcp import knowledge as K
    assert K.ADS_NOTES[0x710][0] == "ADSERR_DEVICE_SYMBOLNOTFOUND" and len(K.KNOWN_ERRORS) == 7
    assert not hasattr(__import__("twincat_mcp.ads_backend", fromlist=["x"]), "_ADS_HINTS")


def test_lookup_reads_codes_names_and_text_and_refuses_bad_input(kb):
    """N125 §6 (N121/N122): a whole message ('ADSError: symbol not found (1808).', 'nErrId := 1808'), a .NET name, a 32-bit form
    (-6908 = 0xFFFFE504), the ESI diagnosis link (its message read from esi.db, not copied), the AL rows, a text
    query; empty / bad limit / unknown domain refused."""
    assert lookup(kb, "ADSError: symbol not found (1808).")["matches"][0]["symbol"] == "ADSERR_DEVICE_SYMBOLNOTFOUND"
    assert lookup(kb, "nErrId := 1808")["matches"][0]["domain"] == "ads"
    assert lookup(kb, "TargetPortNotFound")["matches"][0]["matched_by"] == "dotnet name"
    assert lookup(kb, "LabVIEW error 0xFFFFE504")["matches"][0]["value"] == -6908
    d = lookup(kb, "diag code #x4101")["matches"][0]
    # re-review of 0.8.16: two devices word #x4101 differently — no one text is its meaning (hw_diag with device=)
    assert d["domain"] == "ethercat_diag" and d["messages"] == 2 and "text" not in d and d["device_texts"] == 2, d
    assert "hw_diag" in d["see"]
    al = lookup(kb, "AL status code 0x001B")["matches"][0]
    assert al["domain"] == "ethercat_al" and al["ref"] == "EtherCAT_System_Documentation.pdf#p123", al
    t = lookup(kb, "remote terminal is not responding")
    assert t["documented"] is False and t["matches"] == [] and t["text_matches"][0]["hex"] == "0x745", t
    assert "text_matches" in t["note"] and "no code in the query" in t["text_matches_note"], t
    with pytest.raises(X.ErrError, match="empty query"):
        lookup(kb, "  ")
    with pytest.raises(X.ErrError, match="limit must be"):
        lookup(kb, "6", limit=0)
    with pytest.raises(X.ErrError, match="none of the domains"):
        lookup(kb, "6", domain="banana")
    assert lookup(kb, "6", domain="tcpip")["matches"][0]["symbol"] == "TCPADSERROR_FIXTURE"


# ------------------------------------------------------------------------------------------------ §7 integrations
def test_ads_error_text_names_beckhoffs_text_and_the_plugin_note_is_labelled(kb):
    """N125 §7 ads_*: _fmt_exc adds the documented symbol, text and page (the describer the server installs); the server's
    note on top is labelled as the plugin's."""
    import pyads
    from twincat_mcp import ads_backend as AB
    from twincat_mcp import knowledge as K
    AB.set_error_describer(lambda c: (lambda d: f"{d['symbol']}: {d['text']} (Beckhoff, {d['ref']})" if d else None)(
        X.describe_code(kb, "ads", c)))
    try:
        msg = AB._fmt_exc(pyads.ADSError(1808))
    finally:
        AB.set_error_describer(None)
    assert msg.startswith("ADSError 1808 (0x710): ") and "ADSERR_DEVICE_SYMBOLNOTFOUND: Symbol not found." in msg, msg
    assert "/374277003" in msg and "check the spelling" not in msg, msg
    plain = AB._fmt_exc(pyads.ADSError(1808))                                    # no describer: pyads' text only
    assert plain == f"ADSError 1808 (0x710): {pyads.ADSError(1808).msg}" and "ADSERR_" not in plain, plain
    assert "(plugin's note)]" in K.annotate("ADSError 1808 (0x710): symbol not found")


def test_docs_search_code_lookup_and_the_scope_log_codes(kb, project_dir):
    """N125 §7 (N99b): infosys_search / docs_search name where the code is documented (code_lookup, copies merged, variants
    named) — the skill and the server by one function; the Scope View log's codes are looked up."""
    from tests.test_server_e2e import mk_cfg
    from twincat_mcp.server import App
    from twincat_mcp import kb_layout
    import importlib.util
    spec = importlib.util.spec_from_file_location("tcmcp_kb_docs_err", Path(__file__).resolve().parents[1] / "skills"
                                                  / "twincat-docs" / "kb_docs.py")
    kd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kd)
    out = kd.cmd_docs_search(kd.Ctx(kb, "--kb"), {"query": "0x745", "book": None, "library": None, "limit": 5, "offset": 0})
    cl = out["code_lookup"]
    # the text of 0x745 is the same in every copy: one entry, seven books (the three variants differ elsewhere)
    assert cl[0]["domain"] == "ads" and cl[0]["symbol"] == "ADSERR_CLIENT_SYNCTIMEOUT" and cl[0]["books"] == 7, cl
    assert "texts" not in cl[0] and "missing_in" not in cl[0], cl
    cfg = mk_cfg(project_dir)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    app = App(cfg)
    assert app.code_lookup("0x745") == cl
    log = app.log_codes({"errors": ["[ ERROR ] Scope: Ads-Error 0xA2 router memory", "[ ERROR ] nothing coded"]})
    assert log["codes"] == [{"error": 0, "read_as": ["0xA2"], "documented": False, "matches": [],
                             "note": "0xA2: not in the local Beckhoff documentation of ads — no meaning is given (the "
                                     "full-text hits, if any, only mention it)"}], log
    assert "ADSERR_CLIENT_SYNCTIMEOUT: Timeout has occurred" in app.ads_error_text(1861)
    assert app.doc_note("XAE: (HRESULT 0x98110745) the call failed").endswith(
        "[documented: ads 0x745 ADSERR_CLIENT_SYNCTIMEOUT: " + ADS_REAL[0x745][1] + " (tcplclib_tc2_system/374277003)]")
    assert kb_layout.index_file(kb, "errors").is_file()


@pytest.mark.anyio
async def test_the_server_tool_and_the_skill_command_give_the_same_answer_first_build_and_rebuild(monkeypatch,
                                                                                                   project_dir, tmp_path):
    """N125 §6: one function for the plugin's err_lookup tool and the skill's err_lookup command — identical answers;
    the first build of the missing index starts by itself (its own process, behind kb_lock); a rebuild needs the
    user's approval (kb_rebuild index='errors')."""
    import subprocess
    import sys
    from mcp import Client
    from tests.fake_ads import install
    from tests.test_esi_index import _until
    from tests.test_server_e2e import mk_cfg, payload, text
    from twincat_mcp.server import build_server
    install(monkeypatch)
    kbp = make_kb(tmp_path)
    cfg = mk_cfg(project_dir, mode="guarded")
    cfg.knowledge.location = str(kbp)
    cfg.docs.enabled = False
    async with Client(build_server(cfg)) as c:
        async def built():
            st = payload(await c.call_tool("kb_status", {}))["errors"]
            return st if st["state"] == "complete" and not st.get("rebuild") else None
        st = await _until(c, built)
        assert st["domains_detail"]["ads"]["codes"] == 139, st
        tool = payload(await c.call_tool("err_lookup", {"query": "ADS error 6"}))
        r = payload(await c.call_tool("kb_rebuild", {"index": "errors"}))
        assert r["started"] is False and r["up_to_date"], r
        r = await c.call_tool("kb_rebuild", {"index": "errors", "force": True})
        assert r.is_error and "tc_request_confirmation" in text(r)
    script = Path(__file__).resolve().parents[1] / "skills" / "twincat-docs" / "kb_docs.py"
    p = subprocess.run([sys.executable, str(script), "err_lookup", "query=ADS error 6", "--kb", str(kbp)],
                       capture_output=True, timeout=60)
    skill = json.loads(p.stdout.decode("utf-8"))
    skill.pop("elapsed_ms")
    assert p.returncode == 0 and skill == tool, (skill, tool)
    log = Path(cfg.safety.audit_log).parent / "logs" / "errors-build.log"
    assert "errors_db.py update" in log.read_text(encoding="utf-8") and "codes (" in log.read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ review of 0.8.16
def _app(tmp_path, kb):
    from tests.test_server_e2e import mk_cfg
    from twincat_mcp.server import App
    cfg = mk_cfg(tmp_path)
    cfg.knowledge.location = str(kb)
    cfg.docs.enabled = False
    return App(cfg)


def test_review_0816_an_esi_text_id_has_the_fields_of_any_documented_code(kb, tmp_path):
    """Review of 0.8.16 (1): the ESI match had no 'dec' and no 'ref' — doc_note (every XAE / ADS / rec error text) and
    kb_search raised KeyError instead of giving the error. Every match has the fields of a documented code."""
    from twincat_mcp import knowledge as K
    m = lookup(kb, "EtherCAT diagnosis 0x8001")["matches"][0]
    assert m["domain"] == "ethercat_diag" and {"value", "hex", "dec", "text", "ref"} <= set(m), m
    app = _app(tmp_path, kb)
    note = app.doc_note("Init4: EtherCAT diagnosis 0x8001 on Term 3")
    assert note.endswith("[documented: ethercat_diag #x8001: Supply voltage too low (esi.db, text id #x8001)]"), note
    # re-review of 0.8.16: a text id whose devices word it differently is not annotated with one device's text
    m = "Term 5 (EL3102): diag 0x4101 Overheated"
    assert app.doc_note(m) == m
    r = K.Knowledge(tmp_path / "home", str(kb)).search("EtherCAT diagnosis code 0x4101")
    assert r["known_error"]["documented"][0]["domain"] == "ethercat_diag", r


WINSOCK = "# Windows Socket Error Codes\n\n| Error code | Hex | Symbol | Description |\n|---|---|---|---|\n" + "\n".join(
    f"| {10000 + i} | 0x{10000 + i:X} | WSAE{n} | Winsock fixture {n.lower()}. |" for i, n in enumerate(
        ("INTR", "BADF", "ACCES", "FAULT", "INVAL", "MFILE", "WOULDBLOCK", "INPROGRESS", "ALREADY", "NOTSOCK",
         "DESTADDRREQ", "MSGSIZE"), start=4)) + "\n"


def winsock_kb(tmp_path: Path) -> Path:
    """The fixture with Winsock's family of capitals-only symbols (WSAE…, as the real index has 54 of them)."""
    k = tmp_path / "kbw"
    write_infosys(L.index_file(k, "infosys"), _pages() + [("tcplclib_tc2_utilities", "391170000",
                                                          "Windows Socket error codes", None, WINSOCK)])
    write_manuals(L.index_file(k, "manuals"))
    write_esi(L.index_file(k, "esi"))
    assert build(k)["result"] == "built"
    return k


def test_review_0816_a_symbol_in_capitals_is_looked_up_by_its_name(kb, tmp_path):
    """Review of 0.8.16 (2): WSAETIMEDOUT / WSAECONNREFUSED (capitals, no '_') were read as words — the text search
    answered another code (WSAEHOSTDOWN) or nothing. A capitalised word is a name when the alias table has it; 'ERROR' in
    a log line is none."""
    r = lookup(kb, "WSAECONNREFUSED")                          # the whole query: a name
    m = r["matches"][0]
    assert r["documented"] is True and (m["symbol"], m["matched_by"], m["value"]) == ("WSAECONNREFUSED", "symbol",
                                                                                       10061), r
    kw = winsock_kb(tmp_path)                                    # inside a message: a member of a family of names
    m = lookup(kw, "recv failed: WSAEINTR")["matches"][0]
    assert (m["symbol"], m["matched_by"], m["value"]) == ("WSAEINTR", "symbol", 10004), m
    r = lookup(kb, "WSAETIMEDOUT")
    assert (r["matches"][0]["domain"], r["matches"][0]["value"], r["matches"][0]["matched_by"]) == ("ads", 10060,
                                                                                                    "symbol"), r
    r = lookup(kb, "[ ERROR ] TIMEOUT while waiting")
    assert "names" not in r and r["documented"] is False, r


def test_review_0816_a_copied_or_moved_folder_rebuilds_from_its_own_infosys_index(kb, tmp_path):
    """Review of 0.8.16 (3): a rebuild read the Infosys index path the old index recorded — after a copy the old folder's
    (one index of two folders), after a move a missing one (a forced rebuild replaced 139 ADS codes with none). The
    folder's own Infosys index of the recorded language is read."""
    import shutil
    kb2 = tmp_path / "moved" / "kb"
    shutil.copytree(kb, kb2)
    db2 = L.index_file(kb2, "errors")
    assert X.build(db2, kb2, force=True, log=lambda m: None)["result"] == "built"
    src = Path(json.loads(X.read_meta(db2)["sources"])["infosys"]["path"])
    assert src.resolve().is_relative_to(kb2.resolve()), src            # the copy's own, not the old folder's
    shutil.rmtree(kb)                                                   # moved: the old folder is gone
    st = X.state(db2, kb2)
    assert st["state"] == "complete", st
    assert X.build(db2, kb2, force=True, log=lambda m: None)["result"] == "built"
    assert X.state(db2, kb2, detail=True)["domains_detail"]["ads"]["codes"] == 139


def test_review_0816_the_first_build_waits_while_a_source_is_built(tmp_path, monkeypatch):
    """Review of 0.8.16 (8): at the server start an index being built (here esi.db) kept the errors index's first build
    from starting, and only an err_lookup call tried again. The start's thread waits and starts it once the source is
    complete."""
    import time as _t
    from twincat_mcp import esi_index as E
    from twincat_mcp import kb_lock
    from twincat_mcp.server import App
    k = make_kb(tmp_path)
    held = kb_lock.IndexLock(E.side_files(L.index_file(k, "esi"))[1])
    held.__enter__()
    started: list[int] = []
    monkeypatch.setattr(App, "ERRORS_FIRST_WAIT_S", 0.05)
    monkeypatch.setattr(App, "_spawn_errors_build", lambda self, force=False, dry_run=False:
                        started.append(1) or {"started": True})
    _app(tmp_path, k)
    _t.sleep(0.5)
    assert started == []                                                # esi.db is being built: it waits
    held.__exit__(None, None, None)
    t0 = _t.time()
    while not started and _t.time() - t0 < 10:
        _t.sleep(0.05)
    assert started == [1]


def test_review_0816_one_errors_build_at_a_time(kb, tmp_path, monkeypatch):
    """Review of 0.8.16 (17): two callers at once (an err_lookup and the first build's wait) both started a builder;
    the second failed on the lock and its caller was told started=True."""
    import threading
    import time as _t
    from twincat_mcp.server import App
    app = _app(tmp_path, kb)
    calls: list[int] = []

    class Running:
        pid = 4242

        def poll(self):
            return None

    def spawn(self, force=False, dry_run=False):
        calls.append(1)
        _t.sleep(0.2)
        self.errors_build_proc = Running()
        return {"started": True}
    monkeypatch.setattr(App, "_spawn_errors_build", spawn)
    res: list[dict] = []
    ts = [threading.Thread(target=lambda: res.append(app.spawn_errors_build(force=True))) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert calls == [1] and sorted(r["started"] for r in res) == [False, True], res
    assert [r for r in res if not r["started"]][0]["already_running"] == {"pid": 4242}


def test_review_0816_line_codes_only_for_lines_that_name_a_code(kb, tmp_path):
    """Review of 0.8.16 (10): a capitalised word (EtherCAT, Init4, EK1100) made a line 'documented: false' — no code was
    in it. A line counts when it names a code: a number read as one, a message id, a code's symbol."""
    app = _app(tmp_path, winsock_kb(tmp_path))
    codes = app.line_codes(["Init4: EtherCAT state change aborted on EK1100", "Error: ADS error 0x745",
                            "[ ERROR ] TIMEOUT", "socket: WSAEINTR"], "line")
    assert [c["line"] for c in codes] == [1, 3], codes
    assert codes[0]["matches"][0]["symbol"] == "ADSERR_CLIENT_SYNCTIMEOUT" and codes[1]["read_as"] == ["WSAEINTR"]


def test_review_0816_a_count_of_errors_is_no_code(kb):
    """Review of 0.8.16 (11): 'Build: 3 errors, 0 warnings' was read as code 3, 'found 0 errors' as 0. A plural after a
    number counts; '1861 error' names one."""
    from twincat_mcp import docsearch as DS
    for q in ("Build: 3 errors, 0 warnings", "found 0 errors", "PLC: 7 errors found", "17 codes"):
        assert X.read_query(q)["codes"] == [], q
    assert DS.code_forms("PLC: 17 errors found") is None
    assert [c["value"] for c in X.read_query("1861 error")["codes"]] == [1861]


def test_review_0816_code_lookup_reads_the_hresult_and_32_bit_forms(kb):
    """Review of 0.8.16 (14): infosys_search's code_lookup took the plain value only — 0x98110745 (the HRESULT of ads
    0x745) and 0xFFFFE504 (LabVIEW's -6908) found nothing there, though err_lookup finds both."""
    a = X.code_summary(kb, 0x98110745)
    assert [(e["domain"], e["symbol"], e["matched_by"]) for e in a] == [("ads", "ADSERR_CLIENT_SYNCTIMEOUT", "hresult")], a
    b = X.code_summary(kb, 0xFFFFE504)
    assert [(e["domain"], e["value"]) for e in b] == [("labview", -6908)], b


def test_review_0816_doc_note_never_picks_one_of_several_domains(kb, tmp_path):
    """Review of 0.8.16 (6), user decision: an error text is annotated only when the code's domain is clear — the
    message names it ('ADS error 6') or only one domain has the code; a message that quotes one domain's text needs
    nothing. 'CreateFile failed: error 6: The handle is invalid.' got ADS's 0x6 appended."""
    app = _app(tmp_path, kb)
    m = "CreateFile failed: error 6: The handle is invalid."
    assert app.doc_note(m) == m
    assert app.doc_note("FB_Socket: error 6") == "FB_Socket: error 6"           # ads, win32, a library: none picked
    n = app.doc_note("ADS error 6 on read")
    assert n.startswith("ADS error 6 on read [documented: ads 0x6 ERR_TARGETPORTNOTFOUND"), n


TSM_PAGE = """# Miscallaneous error codes

The following error codes represent the HRESULT values of Automation Interface methods.

```
typedefenum TCSYSMANAGERHRESULTS
{
    [helpstring("ITcSmTreeItem not found!" (ITcSmTreeItem nicht gefunden!))]
    TSM_E_ITEMNOTFOUND         = 0x98510001,
    [helpstring("Invalid Item Type!" (Ungültiger Elementtyp!))]
    TSM_E_INVALIDITEMTYPE         = 0x98510002,
    [helpstring("XML Error" (XML-Fehler))]
    TSM_E_XMLERROR             = 0x98510008,
} TCSYSMANAGERHRESULTS;
```

| Error | Description |
|---|---|
| RPC_E_CALL_REJECTED | The COM-Server has rejected the call. |
"""
ST_ENUM_PAGE = """# E_HomingErrorCodes

```st
TYPE MC_Switch_Mode :
(
    MC_HOMINGERROR_DRIVETYPE               := 16#4B90, (* the drive type is not supported *)
    // the direction is invalid
    MC_HOMINGERROR_DIRECTION               := 16#4B91,
    MC_HOMINGERROR_SWITCHMODE              := 16#4B92  (* 4B92 hex *)
) UDINT;
END_TYPE
```

Example:

```st
nErr := 16#4B90;
nState := 16#0001;
nOther := 16#0002;
```
"""


def test_review_0816_codes_in_an_enum_code_block_are_indexed():
    """Review of 0.8.16 (15), user decision: the Automation Interface's HRESULTs (TSM_E_ITEMNOTFOUND 0x98510001 …) are
    an IDL enum in a code block, a library's error enum an ST TYPE — the table parser read neither. Members with their
    value and Beckhoff's text (an IDL helpstring's English string, an ST comment); a number-only comment is no text; an
    assignment in a code example is no enum."""
    r = X.parse_code_enums(TSM_PAGE)
    assert [(x["symbol"], x["value"], x["hresult"], x["text"]) for x in r] == [
        ("TSM_E_ITEMNOTFOUND", 0x98510001, "0x98510001", "ITcSmTreeItem not found!"),
        ("TSM_E_INVALIDITEMTYPE", 0x98510002, "0x98510002", "Invalid Item Type!"),
        ("TSM_E_XMLERROR", 0x98510008, "0x98510008", "XML Error")], r
    r = X.parse_code_enums(ST_ENUM_PAGE)
    assert [(x["symbol"], x["value"], x["text"]) for x in r] == [
        ("MC_HOMINGERROR_DRIVETYPE", 0x4B90, "the drive type is not supported"),
        ("MC_HOMINGERROR_DIRECTION", 0x4B91, None),        # re-review: a comment line above is a heading, not its text
        ("MC_HOMINGERROR_SWITCHMODE", 0x4B92, None)], r
    assert X.parse_code_enums("```st\nnErr := 16#4B90;\nnA := 1;\nnB := 2;\n```") == []


def test_review_0816_an_automation_interface_hresult_is_documented(tmp_path):
    """The XAE failure codes: 0x98510001 in every form (the hex, its negative 32-bit form, the symbol) answers
    TSM_E_ITEMNOTFOUND 'ITcSmTreeItem not found!' of the TE1000 book; a library enum's member by its symbol."""
    k = tmp_path / "kb"
    write_infosys(L.index_file(k, "infosys"), _pages() + [
        ("tc3_automationinterface", "242957195", "Miscallaneous error codes", None, TSM_PAGE),
        ("tcplclib_tc3_mc2_advancedhoming", "645493643", "E_HomingErrorCodes", "Tc3_MC2_AdvancedHoming", ST_ENUM_PAGE)])
    write_manuals(L.index_file(k, "manuals"))
    write_esi(L.index_file(k, "esi"))
    assert build(k)["result"] == "built"
    for q in ("0x98510001", "-1739522047", "TSM_E_ITEMNOTFOUND", "LookupTreeItem failed (HRESULT 0x98510001)"):
        m = lookup(k, q)["matches"][0]
        assert (m["domain"], m["symbol"], m["text"]) == ("tcsysmanager", "TSM_E_ITEMNOTFOUND",
                                                         "ITcSmTreeItem not found!"), (q, m)
    m = lookup(k, "MC_HOMINGERROR_DRIVETYPE")["matches"][0]
    assert (m["value"], m["text"], m["ref"]) == (0x4B90, "the drive type is not supported",
                                                 "tcplclib_tc3_mc2_advancedhoming/645493643"), m


# ------------------------------------------------------------------------------------------------ re-review of 0.8.16
LIB_TIMEOUT = """# Error codes

| Hex | Dec | Symbol | Description |
|---|---|---|---|
| 0x0001 | 1 | TIMEOUT | Device has a timeout. |
| 0x0002 | 2 | INTERNAL | Internal error of the library. |
| 0x0003 | 3 | NOMEMORY | No memory. |
"""


def test_rereview_0816_the_automation_interface_words_name_no_domain(kb, tmp_path):
    """Re-review of 0.8.16 (1): the TE1000 HRESULT domain had the names 'automation interface' / 'system manager' /
    'itcsysmanager' — words of nearly every XAE error — so such a message named it the only answer: 'ITcSysManager::…
    failed: HRESULT 0x98110745' lost ADS 0x745, and the plugin's own XAE error texts lost their annotation."""
    for q in ("ITcSysManager::ActivateConfiguration failed: HRESULT 0x98110745",
              "the System Manager returned error 0x745", "Automation Interface error 0x98110745"):
        r = lookup(kb, q)
        assert r["documented"] is True and r["matches"][0]["symbol"] == "ADSERR_CLIENT_SYNCTIMEOUT", (q, r)
    app = _app(tmp_path, kb)
    m = "Check all objects did not run. Automation Interface: ExecuteCommand: com_error: failed (HRESULT 0x98110706)."
    assert "[documented: ads 0x706" in app.doc_note(m), app.doc_note(m)


def test_rereview_0816_a_one_word_library_symbol_in_a_log_line_is_no_code(tmp_path):
    """Re-review of 0.8.16 (2): TIMEOUT / INTERNAL / NOMEMORY are symbols of library tables and words of every log line
    — '[ ERROR ] TIMEOUT while waiting' answered a library's 0x0001. A capitals-only word is a name as the whole query
    or as a member of a family of such names (Winsock's WSAE…)."""
    k = tmp_path / "kbt"
    write_infosys(L.index_file(k, "infosys"), _pages() + [("tf3600_tc3_condition_monitoring", "1162595595",
                                                          "Error codes", None, LIB_TIMEOUT)])
    write_manuals(L.index_file(k, "manuals"))
    write_esi(L.index_file(k, "esi"))
    assert build(k)["result"] == "built"
    for q in ("[ ERROR ] TIMEOUT while waiting for the target", "ADS read: TIMEOUT", "INTERNAL ERROR in the compiler"):
        r = lookup(k, q)
        assert r["documented"] is False and not any(m.get("matched_by") == "symbol" for m in r["matches"]), (q, r)
    r = lookup(k, "TIMEOUT")
    assert r["documented"] is True and r["matches"][0]["symbol"] == "TIMEOUT", r


def test_rereview_0816_kb_search_names_every_domain_of_a_code(kb, tmp_path):
    """Re-review of 0.8.16 (4): kb_search's known_error.code named the first of several domains ('CreateFile failed:
    error 6' → ads ERR_TARGETPORTNOTFOUND)."""
    from twincat_mcp import knowledge as K
    r = K.Knowledge(tmp_path / "home", str(kb)).search("CreateFile failed: error 6")
    code = r["known_error"]["code"]
    assert "documented in" in code and "ads" in code and "win32" in code, r["known_error"]


def test_rereview_0816_the_first_build_waits_for_a_builder_this_server_started(tmp_path, monkeypatch):
    """Re-review of 0.8.16 (5): a builder counts as being built only once its process holds the lock — the errors
    index's first build, right after the ESI one was started, was built without esi.db (or gave up). A builder this
    server started and that still runs is waited for."""
    from twincat_mcp.server import App
    k = make_kb(tmp_path)
    started: list[int] = []
    monkeypatch.setattr(App, "_errors_first_build_when_ready", lambda self: None)
    monkeypatch.setattr(App, "_spawn_errors_build", lambda self, force=False, dry_run=False:
                        started.append(1) or {"started": True})
    app = _app(tmp_path, k)

    class Proc:
        def __init__(self, rc): self.rc = rc
        def poll(self): return self.rc
    app.esi_build_proc = Proc(None)                       # spawned, its lock not taken yet
    assert app.errors_first_build() is None and started == [] and app.source_builders_running() == ["esi.db"]
    app.esi_build_proc = Proc(0)                          # done
    assert app.errors_first_build() == {"started": True} and started == [1]


def test_rereview_0816_the_full_text_step_reads_the_folder_s_own_infosys_index(kb, tmp_path):
    """Re-review of 0.8.16 (10): the full-text hits of an undocumented code read the Infosys index path the index
    recorded — after the folder moved, none."""
    import shutil
    kb2 = tmp_path / "moved" / "kb"
    shutil.copytree(kb, kb2)
    shutil.rmtree(kb)
    r = lookup(kb2, "Ads-Error 0xA2")
    assert r["documented"] is False and r.get("fulltext") and r["fulltext"]["hits"], r


def test_rereview_0816_more_counts_are_no_codes():
    """Re-review of 0.8.16 (11): '1 error(s), 0 warning(s)', 'errors: 3', '3 Fehler' were read as codes."""
    for q in ("1 error(s), 0 warning(s)", "Build errors: 3", "3 Fehler", "found 2 warnings"):
        assert X.read_query(q)["codes"] == [], q
    assert [c["value"] for c in X.read_query("Fehler 1861")["codes"]] == [1861]


def test_rereview_0816_a_symbol_s_domain_is_said_as_chosen_by_the_symbol(kb):
    """Re-review of 0.8.16 (14): 'WSAETIMEDOUT' said 'the message names ads' — the symbol chose it."""
    r = lookup(kb, "WSAETIMEDOUT")
    assert r["other_domains"] and r["other_domains_note"].startswith("the symbol is answered in ads"), r


def test_rereview_0816_doc_note_reads_the_message_s_words(kb, tmp_path):
    """Re-review of 0.8.16 (15): 'ADSError: symbol not found (1808)' got 'Symbol not found.' appended — the message says
    it already, in other case and punctuation."""
    app = _app(tmp_path, kb)
    m = "ADSError: symbol not found (1808)"
    assert app.doc_note(m) == m


def test_rereview_0816_an_explicit_language_is_read(kb, tmp_path):
    """Re-review of 0.8.16 (16): errors_db.py update --lcid was ignored once an index existed."""
    import shutil
    shutil.copy(L.index_file(kb, "infosys"), L.index_file(kb, "infosys", "1031"))
    db = L.index_file(kb, "errors")
    assert X.build(db, kb, lcid="1031", force=True, log=lambda m: None)["result"] == "built"
    assert json.loads(X.read_meta(db)["sources"])["infosys"]["path"].endswith("infosys-1031.db")
    assert X.build(db, kb, force=True, log=lambda m: None)["result"] == "built"          # none named: the recorded
    assert json.loads(X.read_meta(db)["sources"])["infosys"]["path"].endswith("infosys-1031.db")


def test_rereview_0816_a_negative_code_is_a_code_of_infosys_search(kb, tmp_path):
    """Re-review of 0.8.16 (17): '-6908' (LabVIEW's 0xFFFFE504) gave no code_lookup."""
    from twincat_mcp import docsearch as DS
    f = DS.code_forms("-6908")
    assert f is not None and f["value"] == -6908 and "0xffffe504" in f["hex"], f
    assert [e["domain"] for e in _app(tmp_path, kb).code_lookup("-6908")] == ["labview"]


def test_rereview_0816_an_enum_on_a_page_about_something_else_is_no_code_table():
    """Re-review of 0.8.16 (18): a comment line above a member is a heading as often as its text, and an enum of a page
    that is not about errors (UAHAUpdateStatusCode: a data value's quality) is no code table."""
    md = "```st\nTYPE UAHAUpdateStatusCode :\n(\n    HistorianRaw := 0,\n    HistorianCalculated := 1,\n" \
         "    HistorianInterpolated := 2\n) UDINT;\nEND_TYPE\n```"
    assert X.parse_code_enums(md, "UAHAUpdateStatusCode") == []
    md2 = "```st\nTYPE E_CamErr :\n(\n    (* Cam Table Error Codes *)\n    ERR_A := 16#4A00,\n" \
          "    ERR_B := 16#4A01, (* table not found *)\n    ERR_C := 16#4A02\n) UDINT;\nEND_TYPE\n```"
    assert [(r["symbol"], r["text"]) for r in X.parse_code_enums(md2, "E_CamErr")] == [
        ("ERR_A", None), ("ERR_B", "table not found"), ("ERR_C", None)]


# ------------------------------------------------------------------------------------------------ third review of 0.8.16
SHIFTED = """# Support Return Codes

| error value | symbol | Error description | Remedy option |
|---|---|---|---|
| 16#00001414 | 5140 | TF3710_ERR_INVALID_REQUEST | The request is not currently supported |
| 16#00001415 | 5141 | TF3710_ERR_BETA_TRIAL_EXPIRED | Beta trial period has expired |
| 16#00001416 | 5142 | TF3710_ERR_LICENSE_STATE_ISSUE | The license state is not valid |
"""


def test_review3_0816_a_header_shifted_against_its_cells_is_read_by_the_cells():
    """Third review of 0.8.16: TF3710's 'error value | symbol | Error description | Remedy option' sits over hex |
    decimal | symbol | text — 70 LabVIEW codes had the symbol '5140' and their name as the text."""
    t = X.parse_table([[c.strip() for c in ln.strip().strip("|").split("|")] for ln in SHIFTED.splitlines()
                       if ln.startswith("|") and not ln.startswith("|---")])
    r = t["rows"][0]
    assert (r["value"], r["symbol"], r["text"]) == (0x1414, "TF3710_ERR_INVALID_REQUEST",
                                                    "The request is not currently supported"), t


def test_review3_0816_doc_note_counts_every_domain_before_it_drops_a_textless_one(kb, tmp_path):
    """Third review of 0.8.16 (1): the ESI row of a text id with several texts was dropped BEFORE the several-domains
    check — doc_note then picked the other domain ('error 0x100F': ads appended though ESI has it too)."""
    app = _app(tmp_path, kb)
    rows = [{"domain": "ads", "hex": "0x6", "symbol": "ERR_TARGETPORTNOTFOUND", "text": "Target port not found."},
            {"domain": "ethercat_diag", "hex": "#x0006", "device_texts": 4}]
    app.kb.documented = lambda msg: {"rows": rows, "domains": ["ads", "ethercat_diag"]}
    assert app.doc_note("error 0x0006") == "error 0x0006"
    # sixth review of 0.8.16: the domains beyond the first three rows count too
    app.kb.documented = lambda msg: {"rows": rows[:1], "domains": ["ads", "win32", "ethercat_diag", "nc"]}
    assert app.doc_note("error 0x0006") == "error 0x0006"


def test_review3_0816_names_reads_and_counts(kb, tmp_path):
    """Third review of 0.8.16: (3) the Winsock family is WSA… (WSASYSNOTREADY), a capitals word of a domain the message
    names is its symbol, the whole query may end in a period; (6) a hex number after 'errors' is a code (a count is
    decimal); (7) 'win32 error WSAETIMEDOUT' is answered for win32 only; (11) a hex number after 'System Manager' is a
    code; (older) an HRESULT-shaped number is a code anywhere."""
    kw = winsock_kb(tmp_path)
    assert lookup(kw, "WSAEINTR.")["matches"][0]["symbol"] == "WSAEINTR"
    r = lookup(kb, "errors: 0x745")
    assert r["documented"] is True and r["matches"][0]["symbol"] == "ADSERR_CLIENT_SYNCTIMEOUT", r
    r = lookup(kb, "win32 error WSAETIMEDOUT")
    assert [m["domain"] for m in r["matches"]] == ["win32"] and r["other_domains_note"].startswith(
        "the message names win32"), r
    r = lookup(kb, "System Manager: 0x98110745")
    assert r["documented"] is True and r["matches"][0]["symbol"] == "ADSERR_CLIENT_SYNCTIMEOUT", r
    r = lookup(kb, "ConsumeXml failed (0x98110745)")
    assert r["documented"] is True and r["matches"][0]["hex"] == "0x745", r


def test_review3_0816_infosys_search_reads_negative_codes(kb, tmp_path):
    """Third review of 0.8.16 (8): 'error -6908' gave no code_lookup, and a table row '| 0xFFFFE504 | -6908 |' was not
    the code's definition."""
    from twincat_mcp import docsearch as DS
    assert DS.code_forms("error -6908")["value"] == -6908
    assert DS.definition_score("| 0xFFFFE504 | -6908 | ERR_NOERROR | No error. |", -6908) == 2
    assert [e["domain"] for e in _app(tmp_path, kb).code_lookup("LabVIEW error -6908")] == ["labview"]


def test_review3_0816_kb_search_keeps_the_device_pointer_and_says_the_plugin_note_s_space(kb, tmp_path):
    """Third review of 0.8.16: (9) an ESI text id of several texts lost its pointer (texts_note) in kb_search; (10) the
    plugin's ADS note named the code although the documentation has it in several domains."""
    from twincat_mcp import knowledge as K
    k = K.Knowledge(tmp_path / "home", str(kb))
    r = k.search("EtherCAT diagnosis code 0x4101")
    d = r["known_error"]["documented"][0]
    assert d["device_texts"] == 2 and "hw_diag" in d["device_texts_note"], d
    r = k.search("read failed: error 1808")
    assert "documented in" in r["known_error"].get("code_note", ""), r["known_error"]


def test_review4_0816_an_hresult_is_of_a_facility_and_not_an_index_group_or_address(kb, tmp_path):
    """Fourth review of 0.8.16 (3): 'an HRESULT-shaped number is a code anywhere' read index offsets, addresses and
    index groups ('invalid index offset 0x80000000' answered an OPC UA status). An HRESULT is of a facility (ADS,
    System Manager, Win32, COM), never 0x…0000, never right after an index-group / offset / address word."""
    for q in ("invalid index offset 0x80000000", "Exception (Access violation) at address 0x8C2A1F00",
              "read IG:0x80000001 failed", "TCMARK_STOP 0xC0000000"):
        assert X.read_query(q)["codes"] == [], q
    for q in ("ConsumeXml failed (0x98510004)", "call failed 0X98110745", "Activate failed with 0x80004005"):
        assert X.read_query(q)["codes"], q


def test_review4_0816_kb_search_says_when_the_plugin_note_is_of_another_code_space(kb, tmp_path):
    """Fourth review of 0.8.16 (9): 'win32 error 1808' got the plugin's ADS note as if it were the code; and (6) the
    table-copies note is not passed on without its list."""
    from twincat_mcp import knowledge as K
    k = K.Knowledge(tmp_path / "home", str(kb))
    r = k.search("win32 error 1808")
    assert "does not apply" in r["known_error"].get("code_note", ""), r["known_error"]
    for d in k.search("ADS error 6 on port 851")["known_error"]["documented"]:
        assert "texts_note" not in d, d


def test_review5_0816_tccom_loader_codes_and_failed_at(kb, tmp_path):
    """Fifth review of 0.8.16: (3) the TcCOM loader's 0xC000xxxx codes (documented in tc3_c) and 'failed at 0x…' were
    no longer read; (7) 'LabVIEW error 1808' got the plugin's ADS note though the message names another code space."""
    from twincat_mcp import knowledge as K
    for q in ("Loading TcCOM module 'Untitled1.tmx' failed (0xC0000428)", "failed at 0x98110745"):
        assert X.read_query(q)["codes"], q
    assert X.read_query("TCMARK_STOP 0xC0000000")["codes"] == []
    r = K.Knowledge(tmp_path / "home", str(kb)).search("LabVIEW error 1808")
    ke = r.get("known_error") or {}
    assert ke["code"].startswith("ADS return code 0x710") and "may not apply" in ke["code_note"], ke


def test_review7_0816_item_numbers_quoted_codes_overflow_and_a_locked_index(kb, tmp_path):
    """Seventh review of 0.8.16: (1) an item / line number before 'error' was read as the code ('Task 1: ADSError
    1861' got ads 0x1); (2) a quoted AL status code was not read; (5) a number ≥ 2^63 crashed the lookup; (8) a locked
    index waited 15 s per lookup, said to rebuild, and the builder rebuilt a current index."""
    import sqlite3
    import time as _t
    for q, want in (("Task 1: ADSError 1861", ["1861"]), ("Drive 2: error 1861", ["1861"]),
                    ("notification block 3: ADSError 1861 (0x745): timeout elapsed", ["1861"]),
                    ("Axis 2: error 0x4260", ["0x4260"]), ("'Term 2 (EL1008)' (1002): error 0x001B", ["0x001B"]),
                    ("MAIN.TcPOU(34): error C0046", []), ("Port 851: error 1808", ["1808"]),
                    ("Task 1 error 1861", ["1861"]), ("1861 error", ["1861"]), ("error '1861'", ["1861"]),
                    ("AL Status '0x001b' read and 'SAFEOP ERR' expected", ["0x001b"]),
                    ("AL Status Code: '0x001b' - Invalid output configuration", ["0x001b"])):
        assert [c["token"] for c in X.read_query(q)["codes"]] == want, q
    app = _app(tmp_path, kb)
    assert "ERR_INTERNAL" not in app.doc_note("Task 1: ADSError 1861")
    assert lookup(kb, "AL Status Code: '0x001b' - Invalid output configuration")["matches"][0]["domain"] == "ethercat_al"
    for q in ("TcCOM init failed, error 0xFFFFFFFF80004005", "ADSError 18446744073709551615", "error 0x1" + "0" * 30):
        assert lookup(kb, q)["documented"] in (True, False), q
    assert app.line_codes(["ADSError 18446744073709551615"], "line")[0]["documented"] is False
    db = L.index_file(kb, "errors")
    con = sqlite3.connect(db)
    con.execute("BEGIN EXCLUSIVE")
    try:
        t0 = _t.monotonic()
        st = X.state(db, kb)
        assert st["state"] == "unreadable" and st["locked"] and "rebuild does not help" in st["message"], st
        assert _t.monotonic() - t0 < 3.5, _t.monotonic() - t0
        with pytest.raises(X.ErrError) as ei:
            build(kb)
        assert ei.value.code == "busy" and "nothing was built" in str(ei.value)
        assert X.describe_code(kb, "ads", 1861) is None
    finally:
        con.rollback()
        con.close()
    assert X.state(db, kb)["state"] == "complete"
    # damaged inside (its meta readable, a table gone): said, not silently nothing
    bad = tmp_path / "bad"
    import shutil
    shutil.copytree(kb, bad / "kb")
    c2 = sqlite3.connect(L.index_file(bad / "kb", "errors"))
    c2.execute("DROP TABLE code")
    c2.commit()
    c2.close()
    app2 = _app(tmp_path / "b", bad / "kb")
    e = app2.line_codes(["Error: ADS error 0x745 (1861)"], "line")[0]
    assert e["documented"] is None and "could not be read" in e["note"], e
