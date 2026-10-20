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
    assert d["domain"] == "ethercat_diag" and d["messages"] == 2 and d["text"] == "Terminal-Overtemperature", d
    assert "hw_diag" in d["see"]
    al = lookup(kb, "AL status code 0x001B")["matches"][0]
    assert al["domain"] == "ethercat_al" and al["ref"] == "EtherCAT_System_Documentation.pdf#p123", al
    t = lookup(kb, "remote terminal is not responding")
    assert t["matches"][0]["matched_by"] == "text" and "no code in the query" in t["note"], t
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
