"""The error-code index of the knowledge folder: <kb>\\index\\errors.db — for a number, a name or a whole error
message, which documented error(s) it is, in which domain, with Beckhoff's text and the page it comes from; or, said
plainly, that the local documentation does not have it (N125, 0.8.16). err_lookup — the server's tool and the
twincat-docs skill's command of the same name — answers from this one function; kb_search's known_error, the ads_*
error texts, the code_note of infosys_search / docs_search and the error texts of the other tools use it too.

Sources — the plugin's own local indexes, never the Help Viewer package or the PDFs again; each one's identity (path,
format, build stamp) is recorded, and a source rebuilt since makes this index rebuild_needed (it answers as it is):
  index\\infosys-<lcid>.db   the Markdown pipe tables of the pages that hold codes (ADS return codes, Win32 error
                            codes, NC errors, the error-code pages of libraries and functions …). A table-aware parser
                            finds each table's columns by what their headers — and, where a header says nothing, the
                            cells — mean (hex, decimal, HRESULT, symbol, text, kind), never by their position; the code
                            notations are docsearch's. Every candidate page is in source_report with what came out of it
                            (0 rows included, with why).
  index\\manuals.db          the EtherCAT AL status codes of the PDF manuals: rows where a page's table parses cleanly,
                            else a link to the page (manual, page) — every such page in source_report with the reason.
  index\\esi.db              the diagnosis text ids of the ESI files: linked (which ids exist, how many messages) — the
                            messages stay in esi.db (hw_diag reads them; err_lookup shows the first one from there).
  ANNOTATIONS (this module)  the plugin's own notes (cause / fix of a code or of a message's text): stored with the
                            build's check against the documented row (verified_on); always shown as the plugin's note,
                            never as Beckhoff's text.
Not documented locally (searched, answered 'documented: false', never guessed): CoE SDO abort codes, the compiler's
C0xxx messages.

Domains: a domain is one code space — 'ads' (the ADS return-code pages, in some 35 books), 'win32', 'hresult', 'nc'
(the NC error book), 'labview', 'ethercat_al', 'ethercat_diag', and one domain per other code page (its book/page id).
One document (one page id) in several books is one source: its copies with the same rows are one variant (variant:
the books that carry it); copies whose rows differ are other variants, kept with their books — a code whose text
differs answers with every text, the 'main' one being the text of the most books.

Build discipline (as esi.db): one build at a time (an OS lock on <db>.lock with the progress), written as
<db>.building and swapped in when complete and checked — the golden checks (GOLDEN, and no domain with fewer codes
than at the last build unless the rebuild was forced): a build that fails them is marked incomplete, its work file
kept for the report, and the index in use stays. Readers open the file read-only. FORMAT is the stored layout and the
reading rules; tests/test_formats.py fingerprints the code behind it.

Standard library only: the server, the builder (skills/twincat-docs/builders/errors_db.py) and the docs skill import it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from . import docsearch as DS
from . import kb_layout as L
from . import kb_lock

FORMAT = "1"            # 1: 0.8.16 (N125) — first version (code.detail: the labelled fields of a code's section)
SIDE = ("-journal", "-wal", "-shm")
SWAP_WAIT_S = 60.0
LIMIT_MAX = 20


class ErrError(RuntimeError):
    """code: 'bad_param', 'empty_query', 'no_index', 'unreadable', 'busy', 'no_source'."""

    def __init__(self, message: str, code: str = "bad_param"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------- the plugin's own notes
_ELEMENTARY = frozenset("BOOL BYTE WORD DWORD LWORD SINT USINT INT UINT DINT UDINT LINT ULINT REAL LREAL TIME LTIME "
                        "DATE TOD TIME_OF_DAY DT DATE_AND_TIME STRING WSTRING".split())


def convert_fix(a: str, b: str) -> str:
    """C0032: the conversion that exists — {A}_TO_{B} only between elementary types; INT → BOOL is a comparison."""
    ua, ub = a.upper(), b.upper()
    if ua in _ELEMENTARY and ub in _ELEMENTARY:
        if ub == "BOOL":
            return f"compare instead: (value <> 0) turns the {ua} into a BOOL (or {ua}_TO_BOOL(…) when that is meant)"
        return (f"convert explicitly: {ua}_TO_{ub}(…)" + (" — TRUE → 1, FALSE → 0" if ua == "BOOL" else "")
                + ", or declare both sides with the same type")
    return (f"{a} and {b} are not assignment-compatible: assign member by member / element by element, use a variable "
            f"of type {b}, or (pointer / reference) the referenced type")


FIX_FUNCTIONS: dict[str, Callable[..., str]] = {"convert": convert_fix}
PLUGIN_NOTE = "the plugin's own note — not Beckhoff's text"

# N125: the plugin's hand-written notes — before 0.8.16 three tables (knowledge.KNOWN_ERRORS, knowledge.ADS_NOTES,
# ads_backend._ADS_HINTS) that cited no page and could disagree. One table now; every row is the plugin's note on a
# code (domain, values) and / or on a message's text (pattern: tried in this order, the first that matches wins — the
# compiler's first: their quoted names may contain anything the later patterns look for, 'bLicenseOk'). symbol:
# Beckhoff's name of the code as the local table has it (checked by every build: verified_on). message_text: how
# pyads / the ADS router words the code — it counts only in a message about ADS (N130). context: the row applies only
# when the message also matches it (an RPC call's 0x705 is the parameter block, not a variable's size). fix_fn: a fix
# computed from the pattern's groups (FIX_FUNCTIONS). Extended here, never from tool output.
ANNOTATIONS: list[dict[str, Any]] = [
    # N70: the TwinCAT compiler's messages, by their text (the C-numbers differ between builds; the text does not)
    {"domain": "compiler",
     "pattern": r"identifier '([^']+)' (?:is )?not defined|identifier (?!(?:is|was|not)\b)([A-Za-z_][\w.]*) (?:is )?not defined",
     "cause": "'{1}' is not declared where it is used: a typo, a variable declared in another POU or GVL without the "
              "qualifier, a library that is not referenced, or a namespace / qualified_only GVL ('GVL.{1}')",
     "fix": "proj_search {1} (this project) / kb_search {1} (added folders) show where it is declared; declare it, "
            "qualify it, or add the library (proj_libraries)"},
    {"domain": "compiler", "pattern": r"cannot convert type '([^']+)' to type '([^']+)'",
     "cause": "IEC 61131-3 has no implicit conversion from {1} to {2} here",
     "fix": "an explicit conversion between {1} and {2} that exists ({A}_TO_{B} between elementary types; a comparison "
            "to get a BOOL)", "fix_fn": "convert"},
    {"domain": "compiler", "pattern": r"unknown type:? '([^']+)'|unknown type: ([A-Za-z_][\w.]*)",
     "cause": "the type '{1}' is not known to the compiler: a typo, a DUT not in the project, or a library not "
              "referenced",
     "fix": "kb_search {1} / infosys_declaration {1} shows where it comes from; add the DUT or the library"},
    {"domain": "ads", "values": (0x724,), "symbol": "ADSERR_DEVICE_LICENSENOTFOUND",
     "pattern": r"0x98110724|2551252772|\blicen[cs]es?\b",
     "cause": "TwinCAT license missing or expired on the target (TwinCAT falls back to CONFIG)",
     "fix": "XAE → SYSTEM → License → '7 Days Trial License…', activate the configuration again, restart TwinCAT"},
    {"domain": "hresult", "values": (0x80010001, 0x8001010A),
     "pattern": r"0x80010001|-2147418111|0x8001010a|-2147417846|call was rejected|retry later|blocked by dialog",
     "cause": "the XAE shell is busy or a modal dialog has it",
     "fix": "xae_dialogs shows the dialog; answer it with xae_dialog_resolve(button=…), then repeat the call"},
    {"domain": "hresult", "values": (0x800706BA,),
     "pattern": r"0x800706ba|-2147023174|rpc server is unavailable|disconnected \(closed",
     "cause": "the XAE shell was closed/reopened or crashed; the COM proxy is dead",
     "fix": "the next xae_* call re-attaches by itself; if not, xae_attach"},
    {"domain": "xae", "pattern": r"(?<!not )(?<!n't )logged in(?:to)?\b",
     "cause": "XAE is logged in to the PLC; the project cannot be changed online",
     "fix": "xae_plc('logout'), make the change, build, log in again"},
    # the ADS return codes the plugin has a note for (knowledge.ADS_NOTES and ads_backend._ADS_HINTS before 0.8.16)
    {"domain": "ads", "values": (6,), "symbol": "ERR_TARGETPORTNOTFOUND", "message_text": r"target port not found",
     "cause": "nothing listens on that AMS port: TwinCAT is in CONFIG (PLC runtimes exist only in RUN), no PLC project "
              "is activated on it, or the port number is wrong (851 = first PLC runtime, 852 = second …)",
     "fix": "ads_status port=10000 shows RUN/CONFIG; ads_system_mode run, or activate the PLC project"},
    {"domain": "ads", "values": (7,), "symbol": "ERR_TARGETMACHINENOTFOUND",
     "message_text": r"target (?:machine|computer) not found",
     "cause": "no route to that AMS NetId",
     "fix": "ads_add_route_to_plc / XAE → Choose Target → Search; check ams_net_id in the config"},
    {"domain": "ads", "values": (0x701,), "symbol": "ADSERR_DEVICE_SRVNOTSUPP", "message_text": r"service is not supported",
     "cause": "the target/port does not offer this ADS service (e.g. sum commands, file access on this OS)",
     "fix": "use the non-sum variant or another port; for files check that the target OS supports the System Service "
            "file API"},
    {"domain": "ads", "values": (0x704,), "symbol": "ADSERR_DEVICE_INVALIDACCESS", "message_text": r"not permitted",
     "cause": "reading/writing this index group/offset is not permitted (read-only area or protected symbol)",
     "fix": "write a writable symbol instead; check READONLY flags with ads_symbol_info"},
    {"domain": "ads", "values": (0x705,), "symbol": "ADSERR_DEVICE_INVALIDSIZE", "context": r"\brpc call\b",
     "cause": "the RPC parameter block does not match the method's signature: an input missing or extra, a wrong base "
              "type, or outputs / return_type that differ from the declaration",
     "fix": "pass every VAR_INPUT in declaration order with its exact base type (inputs=[{type, value}, …]), every "
            "VAR_OUTPUT in outputs and the method's return type in return_type; check the method in proj_interface"},
    {"domain": "ads", "values": (0x705,), "symbol": "ADSERR_DEVICE_INVALIDSIZE",
     "message_text": r"parameter size not correct",
     "cause": "the value does not fit the variable (string too long, wrong array length or type size)",
     "fix": "check the declared type (ads_symbol_info); write elements by index for arrays"},
    {"domain": "ads", "values": (0x707,), "symbol": "ADSERR_DEVICE_NOTREADY", "message_text": r"device (?:is )?not ready",
     "cause": "the runtime is not running (TwinCAT in CONFIG, or the PLC stopped)",
     "fix": "ads_status; switch to RUN (ads_system_mode run) / start the PLC (ads_plc_state run)"},
    {"domain": "ads", "values": (0x710,), "symbol": "ADSERR_DEVICE_SYMBOLNOTFOUND", "message_text": r"symbol not found",
     "cause": "the symbol is not in the runtime: typo, not downloaded yet, or optimised away (unused, not referenced)",
     "fix": "ads_list_symbols pattern=…; after a code change: build + download / online change"},
    {"domain": "ads", "values": (0x745,), "symbol": "ADSERR_CLIENT_SYNCTIMEOUT",
     "message_text": r"timeout (?:elapsed|has occurred)",
     "cause": "the target did not answer in time: missing/broken route, firewall (TCP 48898), target busy or stopped",
     "fix": "ads_ping; ads_routes; check the route on both sides and the Windows firewall"},
]


def _ann_sha(a: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([a.get("symbol"), a.get("pattern"), a.get("context"), a["cause"], a["fix"]],
                                     ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def notes_for(domain: str, value: int, message: str = "") -> dict[str, Any] | None:
    """The plugin's note on a code: the row whose context matches the message first, then the one without."""
    rows = [a for a in ANNOTATIONS if a["domain"] == domain and value in a.get("values", ())]
    for a in sorted(rows, key=lambda r: r.get("context") is None):
        if a.get("context") and not re.search(a["context"], message or "", re.I):
            continue
        return a
    return None


def pattern_note(message: str) -> tuple[dict[str, Any], list[str]] | None:
    """The first plugin note whose pattern matches the message's text -> (row, the pattern's groups [''] + filled)."""
    for a in ANNOTATIONS:
        if not a.get("pattern"):
            continue
        m = re.search(a["pattern"], message or "", re.I)
        if m:
            pat = re.compile(a["pattern"], re.I)
            got = [g for g in m.groups() if g]
            return a, [""] + got + [""] * (pat.groups - len(got))
    return None


def fill(t: str, groups: list[str]) -> str:
    """{1}, {2}: the pattern's groups (other braces are text: {type, value})."""
    return re.sub(r"\{(\d)\}", lambda g: groups[int(g.group(1))] if int(g.group(1)) < len(groups) else g.group(0), t)


def note_out(a: dict[str, Any], groups: list[str] | None = None, verified: dict[str, Any] | None = None) -> dict[str, Any]:
    """A plugin note as every answer shows it: labelled as the plugin's own (N125)."""
    g = groups or [""]
    fix = FIX_FUNCTIONS[a["fix_fn"]](*g[1:3]) if a.get("fix_fn") and len(g) >= 3 else fill(a["fix"], g)
    out: dict[str, Any] = {"origin": "plugin", "label": PLUGIN_NOTE, "domain": a["domain"], "cause": fill(a["cause"], g),
                           "fix": fix}
    if a.get("symbol"):
        out["symbol"] = a["symbol"]
    if verified is not None:
        out.update(verified)
    return out


# --------------------------------------------------------------------------------------------- domains
# The code spaces with a name of their own. pages: titles of the documents of this domain (in any book); books: books
# whose code pages are this domain (error_book: every page of it is a code page). names: words that name the domain in
# a message ('ADS error 6' is about ADS — the answer for its domain, the same number elsewhere listed apart); hints:
# words that rank it first. origin: where its rows come from (none: no local source — always answered 'not
# documented locally').
DOMAINS: dict[str, dict[str, Any]] = {
    "ads": {"title": "ADS return codes", "origin": "infosys", "priority": 0, "pages": (r"ADS return codes",),
            "names": ("ads", "adserror", "adserr", "adsexception", "adserrorcode", "pyads", "adserrorexception"),
            "hints": ("ams", "port", "route", "router", "netid", "plc", "target", "symbol", "handle", "notification",
                      "timeout", "twincat")},
    "win32": {"title": "Win32 error codes", "origin": "infosys", "priority": 1,
              "pages": (r"Win32 error codes", r"Windows Socket error codes"),
              "names": ("win32", "winerror", "getlasterror", "wsagetlasterror", "winsock", "windows"),
              "hints": ("socket", "file", "path", "system", "wsa", "access", "denied")},
    "hresult": {"title": "HRESULT codes", "origin": "infosys", "priority": 2, "names": (),
                "hints": ("hresult", "com", "xae", "visual", "studio", "shell")},
    # review of 0.8.16 (user decision): the HRESULTs an Automation Interface method fails with (TSM_E_ITEMNOTFOUND
    # 0x98510001 …) — an enum in a code block of the TE1000 book, read by parse_code_enums
    "tcsysmanager": {"title": "Automation Interface HRESULTs (TSM_E_…)", "origin": "infosys", "priority": 3,
                     "books": ("tc3_automationinterface",),
                     # re-review of 0.8.16: no names — 'Automation Interface' / 'System Manager' / 'ITcSysManager' are
                     # in nearly every XAE error and named this domain the only answer ('ITcSysManager::Activate…
                     # failed: HRESULT 0x98110745' lost ADS 0x745); its codes are found by value and by TSM_E_ symbol
                     "names": (),
                     # a hex number right after these is a code (third review: 'System Manager: 0x98110745') — of
                     # whichever domain has it
                     "hex_after": ("automation interface", "system manager", "itcsysmanager", "tcsysmanager"),
                     "hints": ("xae", "treeitem", "tree", "item", "consumexml", "lookuptreeitem")},
    "nc": {"title": "NC errors", "origin": "infosys", "priority": 3, "books": ("tc3ncerrcode",), "error_book": True,
           "names": ("nc", "ncerror", "ncerr"),
           "hints": ("axis", "axes", "drive", "encoder", "controller", "channel", "group", "motion", "mc", "bode",
                     "table", "kinematic")},
    "labview": {"title": "TwinCAT Interface for LabVIEW return codes", "origin": "infosys", "priority": 6,
                "books": ("tf3710_tc3_interface_for_labview",), "names": ("labview",), "hints": ()},
    "ethercat_al": {"title": "EtherCAT AL status codes", "origin": "manuals", "priority": 4,
                    "names": ("al status", "alstatus", "al status code", "al statuscode"),
                    "hints": ("ethercat", "state", "init", "preop", "safeop", "op", "esm", "slave", "terminal", "al")},
    "ethercat_diag": {"title": "EtherCAT diagnosis messages (ESI text ids)", "origin": "esi", "priority": 5,
                      "names": ("diag", "diagnosis", "10f3", "textid", "text id"),
                      "hints": ("ethercat", "terminal", "esi", "history")},
    "coe_sdo_abort": {"title": "CoE SDO abort codes", "origin": "none", "priority": 7,
                      "names": ("sdo", "sdo abort", "abort code", "abortcode"), "hints": ("coe", "canopen", "mailbox"),
                      "note": "no local source documents them: not in the Infosys index, the PDF manuals or the ESI "
                              "files of this PC"},
    # id_form: the number a message of the domain carries ('C0373') — read as that domain's message whatever words
    # stand around it (0.8.16 acceptance: 'C0373' was read as no code at all). C0 only: C5xxx-C9xxx are IPC models
    "compiler": {"title": "TwinCAT compiler messages (C0xxx)", "origin": "none", "priority": 8,
                 "names": ("compiler",), "hints": ("build", "compile"), "id_form": r"(?<![\w-])[Cc]0\d{3}(?![\w-])",
                 "note": "the compiler's messages are not in the local Infosys index (its pages only mention a few "
                         "C-numbers) — the plugin has notes on some of them by their text: give the whole message"},
    "xae": {"title": "XAE shell states", "origin": "plugin", "priority": 9, "names": (), "hints": ("xae", "logged"),
            "note": "the plugin's own notes on states of the XAE shell"},
}
_GENERIC_WORDS = frozenset(DS.fold(w) for w in """error errors err code codes return returns result results status
value values message messages number numbers list overview internal twincat tc3 tc2 tf te tcplclib the of and for in
on to a an id ids hex dec decimal description""".split())


def _norm_words(s: str) -> str:
    """' ads error 1861 port ' — lower case, every run of other characters one space (names and hints compare so)."""
    return " " + " ".join(re.findall(r"[^\W_]+", DS.fold(s or ""))) + " "


def _derived_hints(title: str, book: str) -> list[str]:
    words = [DS.fold(w) for w in re.findall(r"[^\W_]+", f"{title} {book}") if not w.isdigit()]
    return sorted({w for w in words if len(w) >= 3 and w not in _GENERIC_WORDS})


# --------------------------------------------------------------------------------------------- the table parser
# a page that holds codes: its title says so ('ADS Return Codes', 'Win32 Error Codes', 'Errorcodes', 'Error codes
# TF6280', 'Reason codes' …), or it is a page of an error-code book. Not .NET API pages (an enumeration's members are
# its source: ALIAS_PAGES).
CODE_PAGE = re.compile(r"(?:error|return|result|status|abort|reason|fault|alarm)\s*(?:codes?|numbers?|messages?|lists?)\b"
                       r"|\bfehler\s*(?:codes?|nummern?|meldungen?|liste)\b|r[üu]ckgabe\s*(?:codes?|werte)\b", re.I)
API_PAGE = re.compile(r"\b(?:Property|Properties|Method|Methods|Constructor|Constructors|Class|Enumeration|Interface|"
                      r"Field|Fields|Event|Events|Members|Namespace|Overload|Delegate|Structure)(?:\s*\([^)]*\)?)?\s*$|::")
# .NET enumerations whose members name codes of a domain (the alias of each code: 'TargetPortNotFound' → ads 6)
ALIAS_PAGES: dict[str, str] = {r"AdsErrorCode Enumeration": "ads"}

# what a column holds, from its header (normalised: camel case split, lower case): the first that matches (a tuple:
# tests/test_formats.py fingerprints it)
_HEADER = (
    ("hresult", re.compile(r"\bhresult\b")),
    ("hexdec", re.compile(r"\bhex(?:adecimal)?\b.*\b(?:dec(?:imal)?|dez)\b|\b(?:dec(?:imal)?|dez)\b.*\bhex(?:adecimal)?\b")),
    ("hex", re.compile(r"\bhex(?:adecimal)?\b")),
    ("dec", re.compile(r"\bdec(?:imal)?\b|\bdez(?:imal)?\b")),
    ("symbol", re.compile(r"\b(?:name|symbol|symbolic|constant|enum|enumeration|identifier|mnemonic|member)\b")),
    ("text", re.compile(r"\b(?:description|meaning|text|definition|explanation|message|remark|beschreibung|bedeutung)\b")),
    # IO-Link's ErrorTypes: 'Error Code | Additional Code' — the two bytes of one code (0x80 | 0x11 = 0x8011, the
    # page's own example: AdsReturnCode 0x80110700)
    ("subcode", re.compile(r"\badditional\s+code\b|\bsub\s*code\b")),
    ("kind", re.compile(r"\b(?:type|group|category|source|incident|class|range)\b")),
    ("code", re.compile(r"\b(?:code|codes|error|errors|err|errid|errno|id|value|number|return|result|status|"
                        r"fehler|wert|nummer)\b|^(?:no|nr)$")),
)
_VALUE_KINDS = ("hresult", "hexdec", "hex", "dec", "code")
_SYMBOL_CELL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_HEX_SPLIT = re.compile(r"(?<=[0-9A-Fa-f])[ _](?=[0-9A-Fa-f]{4}\b)")      # '0x9811 0745', '0x0000_0000'
_PREFIXED = re.compile(r"(?<![\w#])(?:0x|16#|#x)([0-9A-Fa-f_]+)(?![\w#])", re.I)
_BARE_HEX = re.compile(r"(?<![\w#.])([0-9A-Fa-f]*\d[0-9A-Fa-f]*)(?![\w#])")
_BARE_DEC = re.compile(r"(?<![\w#.])(-?\d+)(?![\w#.])")
_GLUED = re.compile(r"^([A-Z][A-Z_]{3,}?)\s*(-?\d+)$")                  # 'WSAEINTR10004', 'WSAEACCES 10013'
# a range of codes in one cell ('0x0000 ... 0x7FFF', '65537 - 131071', '0x8000 to 0x8FFF'): no code of its own
_RANGE = re.compile(r"[0-9a-f]\s*(?:\.\.\.?|…|\s[-–]\s|–|\bto\b|\bbis\b)\s*-?(?:0x|16#|#x)?[0-9a-f]", re.I)
# what a value cell may hold besides its numbers: a code word before them, separators — not a comparison ('<0',
# '>=0'), a unit, a bracket of other text ('Value range: [0, 0xFFFF]')
_RESIDUE = re.compile(r"^\s*(?:(?:id|no|nr|error|err|code)\b\.?)?[\s/(),:=+]*$", re.I)


def _hnorm(h: str) -> str:
    h = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", h or "")
    return " ".join(re.findall(r"[^\W_]+", h.lower()))


def header_meaning(h: str) -> str | None:
    n = _hnorm(h)
    for kind, rx in _HEADER:
        if rx.search(n):
            return kind
    return None


def _same_num(h: int, digits: int, d: int) -> bool:
    """Hex and decimal of one code: equal, or the decimal negative and the hex its two's complement in the width the
    hex is written in (FFF8 = -8 in 16 bits; 0xFFFFE504 = -6908 in 32)."""
    if h == d:
        return True
    if d < 0:
        for bits in (8, 16, 32, 64):
            if bits >= 4 * digits and h == d & ((1 << bits) - 1):
                return True
    return False


def cell_numbers(cell: str, meaning: str, strict: bool = False) -> dict[str, Any] | None:
    """The code a value cell holds, read as its column's header says: {'hex': int, 'hex_digits': n, 'decs': [int],
    'hresult': int, 'symbol': str} (the parts it has); None when it holds no code. A prefixed number (0x…, 16#…, #x…) is
    hex in any column; a bare one is hex in a hex column, decimal otherwise ('0x100 / (256)' in 'Code hex / (decimal)':
    both); a symbol glued to its number in a code column ('WSAEINTR10004', 'WSAEACCES 10013') is both (not when
    strict: a column found by its content). Not a code: a range ('0x0000 ... 0x7FFF'), a comparison ('<0'), a number
    in other text ('Value range: [0, 0xFFFF]', '4.000 s')."""
    c = _HEX_SPLIT.sub("", (cell or "").strip())
    if not c or _RANGE.search(c):
        return None
    out: dict[str, Any] = {}
    g = _GLUED.match(c)
    if g and not strict and meaning in ("code", "dec"):
        return {"symbol": g.group(1), "decs": [int(g.group(2))]}
    pre = [m for m in _PREFIXED.finditer(c) if m.group(1).replace("_", "")]
    rest = _PREFIXED.sub(" ", c)
    if meaning == "hresult":
        if pre:
            out["hresult"] = int(pre[0].group(1).replace("_", ""), 16)
        else:
            b = _BARE_HEX.fullmatch(rest.strip())
            if b:
                # '98110745' (hex digits, as HRESULTs are written) — a short decimal ('0' = S_OK) is that number
                t = b.group(1)
                out["hresult"] = int(t) if t.isdigit() and len(t) < 8 else int(t, 16)
                rest = ""
        return out if out and _RESIDUE.match(rest) else None
    if pre:
        out["hex"] = int(pre[0].group(1).replace("_", ""), 16)
        out["hex_digits"] = len(pre[0].group(1).replace("_", ""))
    if meaning in ("hex", "subcode") and not pre:
        b = _BARE_HEX.fullmatch(rest.strip())
        if b:
            out["hex"], out["hex_digits"] = int(b.group(1), 16), len(b.group(1))
            rest = ""
    else:
        decs = [int(x) for x in _BARE_DEC.findall(rest)]
        if decs:
            out["decs"] = decs
        rest = _BARE_DEC.sub(" ", rest)
    if not out or not _RESIDUE.match(rest):
        return None
    return out


def _chunks(md: str) -> list[tuple[str, str]]:
    """(heading, text up to the next heading of any level) of a page's Markdown — a '#' line in a code block is code;
    the text before the first heading has the heading ''."""
    out: list[tuple[str, str]] = []
    head, start, fence = "", 0, False
    for m in re.finditer(r"(?m)^(```.*|(#{1,6})\s+(.*))$", md or ""):
        if m.group(1).startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        out.append((head, md[start:m.start()]))
        head, start = m.group(3).strip(), m.end()
    out.append((head, (md or "")[start:]))
    return out


def page_tables(md: str) -> list[tuple[str, list[list[str]]]]:
    """(label, rows) of every pipe table of a page: the label is the line above the table, else its heading."""
    from .infosys import _md_tables          # one reader of the stored pipe tables (N170)
    out = []
    for head, text in _chunks(md):
        for label, rows in _md_tables(text):
            out.append((label or head, rows))
    return out


def parse_table(rows: list[list[str]], label: str = "") -> dict[str, Any]:
    """One pipe table -> {'columns': {index: meaning}, 'parser': 'header' | 'header+content' | 'content', 'rows':
    [{value, hex, dec, hresult, symbol, text, kind}], 'skipped', 'inconsistent': [why …], 'headers'}. The columns are
    found by what their headers mean; a header row that holds no meaning for a column (Win32: 'Error | Description |
    | ' over 'decimal | Hexadecimal | Name | ') is joined with the rows below it that hold no number; a value column
    whose cells are names is a symbol column (EtherNet/IP: 'Error' over CN_ORC_…); a table with no text column takes
    the last column of prose. No row is guessed: hex and decimal that disagree make the row inconsistent (counted, not
    stored)."""
    if not rows:
        return {"columns": {}, "parser": "header", "rows": [], "skipped": 0, "inconsistent": [], "headers": [],
                "rejected": []}
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head = [rows[0][:]]
    body = rows[1:]
    # a header row of a column that says nothing, and the row(s) below without any number: one header
    while body and len(head) < 3 and any(not header_meaning(h) for h in head[0]) and \
            not any(re.search(r"\d", c) for c in body[0]) and any(header_meaning(c) for c in body[0]):
        head.append(body.pop(0))
    joined = [" ".join(h[i] for h in head).strip() for i in range(width)]
    cols: dict[int, str] = {}
    for i, h in enumerate(joined):
        m = header_meaning(h)
        if m:
            cols[i] = m
    parser = "header"
    # a header that is a row of data ('ID 70329 | Actual value change …'): the table's first row
    if any(cols.get(i) in _VALUE_KINDS and re.search(r"\d", rows[0][i]) for i in cols):
        body = [rows[0]] + body
    data = [r for r in body if any(c.strip() for c in r)]

    def rate(i: int, fn: Callable[[str], bool]) -> float:
        cells = [r[i] for r in data if r[i].strip()]
        return sum(1 for c in cells if fn(c)) / len(cells) if cells else 0.0

    for i, m in list(cols.items()):
        if m in _VALUE_KINDS and data and rate(i, lambda c, m=m: cell_numbers(c, m) is not None) < 0.5:
            if rate(i, lambda c: bool(_SYMBOL_CELL.match(c.strip()))) >= 0.5:
                cols[i] = "symbol"
                parser = "header+content"
            else:
                del cols[i]
    # third review of 0.8.16: a header shifted against its cells (TF3710's 'error value | symbol | Error description |
    # Remedy option' over '16#00001414 | 5140 | TF3710_ERR_INVALID_REQUEST | The request is …') — 70 LabVIEW codes had
    # the symbol '5140' and their name as the text. A symbol column of numbers is a decimal one; a text column of names
    # is then the symbol column, and the text the prose column after it
    for i, m in list(cols.items()):
        if m == "symbol" and data and rate(i, lambda c: bool(re.fullmatch(r"-?\d+", c.strip()))) >= 0.8:
            cols[i] = "dec"
            parser = "header+content"
            for j, mj in list(cols.items()):
                if mj == "text" and rate(j, lambda c: bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", c.strip()))) >= 0.8:
                    cols[j] = "symbol"
    if not any(m in _VALUE_KINDS for m in cols.values()) and data:
        # no header says which column holds the code: the column whose cells are numbers (and only that one)
        nums = [i for i in range(width) if i not in cols
                and rate(i, lambda c: cell_numbers(c, "code", strict=True) is not None) >= 0.8]
        if len(nums) == 1:
            cols[nums[0]] = "code"
            parser = "content"
    if "text" not in cols.values() and data:
        prose = [i for i in range(width) if i not in cols and rate(i, lambda c: len(c.split()) >= 2) >= 0.5]
        if prose:
            cols[prose[-1]] = "text"
    value_cols = [i for i in sorted(cols) if cols[i] in _VALUE_KINDS]
    sub_col = next((i for i in sorted(cols) if cols[i] == "subcode"), None) if value_cols else None
    first = {m: next((i for i in sorted(cols) if cols[i] == m), None) for m in ("symbol", "text", "kind")}
    out_rows: list[dict[str, Any]] = []
    skipped = 0
    bad: list[str] = []
    rejected: list[str] = []
    for r in data:
        hexes: list[tuple[int, int]] = []
        decs: list[int] = []
        got: dict[str, Any] = {}
        for i in value_cols:
            v = cell_numbers(r[i], cols[i], strict=parser == "content")
            if not v:
                if re.search(r"(?<![A-Za-z])\d", r[i]) and len(rejected) < 3:
                    rejected.append(r[i][:80])        # a cell with a number that is no single code: said why
                continue
            if "hex" in v:
                hexes.append((v["hex"], v["hex_digits"]))
            decs += v.get("decs", [])
            for k in ("hresult", "symbol"):
                if k in v:
                    got.setdefault(k, v[k])
        value: int | None = None
        if hexes and decs:
            h, n = hexes[0]
            same = [d for d in decs if _same_num(h, n, d)]
            if not same:
                bad.append(f"{' | '.join(r)[:120]} (hex {h:#x} ≠ dec {', '.join(map(str, decs))})")
                continue
            value = same[0]
        elif hexes:
            value = hexes[0][0]
        elif decs:
            if len(set(decs)) > 1:                   # several numbers and nothing to tell which is the code
                skipped += 1
                continue
            value = decs[0]
        elif "hresult" in got:
            value = got["hresult"]
        if value is None:
            skipped += 1
            continue
        if sub_col is not None:
            s = cell_numbers(r[sub_col], "subcode")
            if s and "hex" in s and 0 <= value <= 0xFF and 0 <= s["hex"] <= 0xFF:
                value = (value << 8) | s["hex"]
        sym = (r[first["symbol"]].strip() if first["symbol"] is not None else "") or got.get("symbol") or ""
        text = r[first["text"]].strip() if first["text"] is not None else ""
        if not re.search(r"[^\W\d_]", sym + text):  # no word: '>0', '-' — not a documented meaning
            skipped += 1
            continue
        kind = (r[first["kind"]].strip() if first["kind"] is not None else "") or label
        # the hex form as the table writes it (FFF8 for -8); a composed code (IO-Link) or a decimal only: from the value
        hx = hexes[0][0] if hexes and sub_col is None else value if value >= 0 else value & 0xFFFFFFFF
        out_rows.append({"value": value, "hex": f"0x{hx:X}", "dec": str(value),
                         "hresult": f"0x{got['hresult']:08X}" if "hresult" in got else None,
                         "symbol": sym if sym and len(sym) <= 100 else None, "text": text or None,
                         "kind": kind or None, "hresult_only": set(cols[i] for i in value_cols) == {"hresult"}})
    return {"columns": cols, "parser": parser, "rows": out_rows, "skipped": skipped, "inconsistent": bad,
            "headers": joined, "rejected": rejected}


# --------------------------------------------------------------------------------------------- the AL status codes
AL_PAGE = re.compile(r"\bAL[\s-]?status(?:[\s-]?codes?)?\b", re.I)
_AL_ROW = re.compile(r"^\s*(0x[0-9A-Fa-f]{4})\b[\s:–\-]*(.*)$")
_AL_MIN_ROWS = 3


def parse_al_page(text: str) -> dict[str, Any]:
    """The AL status code table of a manual page (PDF text, one line per printed line): rows when it parses cleanly —
    at least three code lines (0x0000 … as the manuals write them), each with its text, codes ascending; a line between
    two code lines continues the text above it. -> {'rows': [...]} or {'rows': [], 'reason': why it is a link only}."""
    lines = (text or "").splitlines()
    rows: list[dict[str, Any]] = []
    tail: list[str] = []                       # the lines after the latest code line
    for ln in lines:
        m = _AL_ROW.match(ln)
        if m:
            if rows and tail:
                rows[-1]["more"] = " ".join(tail)          # between two code lines: the row above, wrapped
            tail = []
            rows.append({"value": int(m.group(1), 16), "hex": f"0x{int(m.group(1), 16):04X}", "text": m.group(2).strip()})
        elif rows and ln.strip():
            tail.append(ln.strip())
    # after the last code line the table's end is not marked in a PDF's text: a wrapped sentence goes on in lower case
    # ('the watchdog of the sync manager elapsed'), a footer or the next heading does not
    if rows and tail:
        cont = []
        for ln in tail:
            if not ln[:1].islower():
                break
            cont.append(ln)
        if cont:
            rows[-1]["more"] = " ".join(cont)
    if not rows:
        return {"rows": [], "reason": "mentions AL status codes but holds no table of them (no line begins with a "
                                      "code 0x….) — a reference to another document"}
    if len(rows) < _AL_MIN_ROWS:
        return {"rows": [], "reason": f"only {len(rows)} line(s) begin with a code — not a table that parses cleanly"}
    if any(not (r["text"] or r.get("more")) for r in rows):
        return {"rows": [], "reason": "a code line without its text (the columns are not on one line) — not a table "
                                      "that parses cleanly"}
    vals = [r["value"] for r in rows]
    if vals != sorted(set(vals)):
        return {"rows": [], "reason": "the codes are not in ascending order or repeat — the columns did not come out "
                                      "as rows (not a table that parses cleanly)"}
    return {"rows": [{"value": r["value"], "hex": r["hex"], "dec": str(r["value"]), "hresult": None, "symbol": None,
                      "text": " ".join(x for x in (r["text"], r.get("more", "")) if x), "kind": "AL status code"}
                     for r in rows]}


_AL_SECTION = re.compile(r"^\s*(?:[\d.]+\s+)?Error Code (0x[0-9A-Fa-f]{4})\s*$")
_AL_FIELDS = {"meaning": "meaning", "description": "description", "current state (or state change)": "current_state",
              "current state": "current_state", "resulting state": "resulting_state", "solution": "solution"}
# the fields in the order a section gives them: a line is a field's label only after the fields before it — the value
# of 'Resulting state' is often 'Current state', which is no new field (0x0000 lost its resulting state)
_AL_ORDER = ("meaning", "description", "current_state", "resulting_state", "solution")


def parse_al_sections(pages: list[tuple[int, str]]) -> dict[str, Any]:
    """The AL status codes a manual documents one section per code — the EtherCAT system documentation's '8.2.1 EtherCAT
    AL Status Codes': 'Error Code 0x0014' followed by labelled fields (Meaning, Description, Current State (or state
    change), Resulting state, Solution), sections running on over page breaks. Third review of 0.8.16's base: the table
    parser (parse_al_page) read none of its 52 codes — the document became 7 page links. pages: (page, text) of one
    document in page order -> {'rows': [...], 'pages': [pages the sections are on]} — a row's text is its Meaning, the
    other fields its detail; rows only when at least three sections parse, each with a Meaning, codes ascending."""
    rows: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    field: str | None = None
    on: list[int] = []
    for page, text in pages:
        for ln in (text or "").splitlines():
            m = _AL_SECTION.match(ln)
            if m:
                cur = {"value": int(m.group(1), 16), "page": page, "fields": {}}
                rows.append(cur)
                field = None
                if page not in on:
                    on.append(page)
                continue
            if cur is None:
                continue
            key = _AL_FIELDS.get(ln.strip().lower())
            if key is not None and (field is None or _AL_ORDER.index(key) > _AL_ORDER.index(field)):
                field = key
                cur["fields"].setdefault(key, [])
                if page not in on:
                    on.append(page)
                continue
            if field is not None and ln.strip():
                if re.match(r"^\s*\d+(?:\.\d+)+\s+\S", ln):     # the next numbered heading ends the section
                    cur, field = None, None
                    continue
                cur["fields"][field].append(ln.strip())
    out = []
    for r in rows:
        f = {k: " ".join(v).strip() for k, v in r["fields"].items() if " ".join(v).strip()}
        meaning = f.pop("meaning", "")
        out.append({"value": r["value"], "hex": f"0x{r['value']:04X}", "dec": str(r["value"]), "hresult": None,
                    "symbol": None, "text": meaning, "kind": "AL status code", "page": r["page"],
                    "detail": {k: v for k, v in f.items() if v.lower() not in ("n/a", "")} or None})
    vals = [r["value"] for r in out]
    if len(out) < _AL_MIN_ROWS or any(not r["text"] for r in out) or vals != sorted(set(vals)):
        return {"rows": [], "pages": on}
    return {"rows": out, "pages": on}


# a code page whose codes are an enumeration in a code block, not a table: the TE1000 HRESULTs (IDL 'typedef enum
# TCSYSMANAGERHRESULTS { [helpstring("ITcSmTreeItem not found!" …)] TSM_E_ITEMNOTFOUND = 0x98510001, … }') and the
# libraries' error enums (ST 'TYPE E_HomingErrorCodes : ( MC_HOMINGERROR_DRIVETYPE := 16#4B90, … ) UDINT; END_TYPE')
_CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_IDL_ENUM = re.compile(r"(?:\btypedef\s*enum|\benum)\b[^{;]*\{(.*?)\}", re.S)     # the page writes 'typedefenum'
_ST_ENUM = re.compile(r"\bTYPE\s+\w+\s*:\s*\((.*?)\)\s*\w*\s*;?\s*END_TYPE", re.S | re.I)
_ENUM_MEMBER = re.compile(r"^\s*([A-Za-z_]\w*)\s*:?=\s*(0x[0-9A-Fa-f]+|16#[0-9A-Fa-f_]+|-?\d+)\s*,?\s*"
                          r"(?:\(\*\s*(.*?)\s*\*\)|//\s*(.*?))?\s*$")
_HELPSTRING = re.compile(r'helpstring\(\s*"([^"]*)"')
_NUMBER_ONLY = re.compile(r"^\s*(?:0x|16#)?[0-9A-Fa-f_]+\s*(?:hex|dec|h)?\s*$", re.I)
_COMMENT_LINE = re.compile(r"^\s*(?:\(\*\s*(.*?)\s*\*\)|//\s*(.*?))\s*$")


ERROR_ENUM = re.compile(r"err|fault|fail|hresult|abort", re.I)


def parse_code_enums(md: str, title: str = "") -> list[dict[str, Any]]:
    """The members of the enumerations in a page's code blocks — symbol, value, and Beckhoff's text when the block gives
    one (an IDL helpstring's first, English, string; an ST comment on the member's line or the lines right above it). A
    block counts when it declares an enum (IDL 'enum … { }', ST 'TYPE x : ( … ) END_TYPE') of at least three members
    with distinct values — an assignment in a code example ('nErr := 16#1234;') is none."""
    out: list[dict[str, Any]] = []
    for block in _CODE_BLOCK.findall(md or ""):
        # re-review of 0.8.16: an enum is of codes on a page about errors (its title or the enum's name says so) —
        # 'E_DALIAlarmNumber', 'UAHAUpdateStatusCode' (a data value's quality) are none
        if not (ERROR_ENUM.search(title) or ERROR_ENUM.search(block[:200])):
            continue
        for body in [m.group(1) for m in _IDL_ENUM.finditer(block)] + [m.group(1) for m in _ST_ENUM.finditer(block)]:
            rows: list[dict[str, Any]] = []
            text: list[str] = []
            for ln in body.splitlines():
                h = _HELPSTRING.search(ln)
                if h:
                    text = [h.group(1).strip()]
                    continue
                c = _COMMENT_LINE.match(ln)
                if c and not _ENUM_MEMBER.match(ln):
                    # re-review of 0.8.16: a comment line above a member is a group heading as often as its meaning
                    # ('(* Cam Table Error Codes *)') — an ST member's text is its own line's comment only
                    text = []
                    continue
                m = _ENUM_MEMBER.match(ln)
                if not m:
                    if ln.strip():
                        text = []
                    continue
                raw = m.group(2)
                v = int(raw[3:].replace("_", ""), 16) if raw.startswith("16#") else int(raw, 0)
                own = (m.group(3) or m.group(4) or "").strip()
                t = own or " ".join(x for x in text if x).strip() or None
                if t and _NUMBER_ONLY.match(t):
                    t = None                   # '(* 20 hex *)', '// 102': the value again, no meaning
                hres = f"0x{v:08X}" if 0x80000000 <= v <= 0xFFFFFFFF else None
                rows.append({"value": v, "hex": f"0x{v:X}" if v >= 0 else str(v), "dec": str(v), "hresult": hres,
                             "symbol": m.group(1), "text": t, "kind": None})
                text = []
            if len(rows) >= 3 and len({r["value"] for r in rows}) == len(rows):
                out += rows
    return out


# --------------------------------------------------------------------------------------------- the database
SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE domain (id TEXT PRIMARY KEY, title TEXT, origin TEXT, priority INTEGER, names TEXT, hints TEXT,
  documents INTEGER, codes INTEGER, rows INTEGER, variants INTEGER, links INTEGER, note TEXT);
CREATE TABLE code (id INTEGER PRIMARY KEY, domain TEXT, value_int INTEGER, hex TEXT, dec TEXT, hresult TEXT,
  symbol TEXT, text TEXT, kind TEXT, source_ref TEXT, book TEXT, page_id TEXT, url TEXT, variant TEXT, document TEXT,
  detail TEXT);
CREATE INDEX code_value ON code (value_int);
CREATE INDEX code_domain ON code (domain, value_int);
CREATE TABLE variant (domain TEXT, variant TEXT, document TEXT, sha TEXT, books TEXT, books_n INTEGER, refs TEXT,
  row_count INTEGER, parser TEXT, main INTEGER, ref TEXT, PRIMARY KEY (domain, variant));
CREATE TABLE alias (domain TEXT, kind TEXT, form TEXT, value_int INTEGER, source_ref TEXT);
CREATE INDEX alias_form ON alias (form);
CREATE TABLE annotation (id INTEGER PRIMARY KEY, domain TEXT, value_int INTEGER, symbol TEXT, pattern TEXT, context TEXT,
  cause TEXT, fix TEXT, origin TEXT, sha TEXT, verified_on TEXT, documented_symbol TEXT, check_note TEXT);
CREATE TABLE link (domain TEXT, value_int INTEGER, target TEXT, ref TEXT, title TEXT, count INTEGER, reason TEXT);
CREATE INDEX link_value ON link (value_int);
CREATE TABLE source_report (id INTEGER PRIMARY KEY, source TEXT, ref TEXT, title TEXT, book TEXT, document TEXT,
  domain TEXT, tables INTEGER, code_tables INTEGER, rows_found INTEGER, skipped INTEGER, inconsistent INTEGER,
  parser TEXT, status TEXT, reason TEXT);
"""


def ro_uri(db: Path) -> str:
    from urllib.parse import quote
    s = str(Path(db).resolve()).replace("\\", "/")
    s = "//" + s if s.startswith("//") else (s if s.startswith("/") else "/" + s)
    return "file:" + quote(s, safe="/:") + "?mode=ro"


def connect_ro(db: Path, timeout: float = 10.0) -> sqlite3.Connection:
    con = sqlite3.connect(ro_uri(db), uri=True, timeout=timeout)
    con.row_factory = sqlite3.Row
    return con


def side_files(final: Path) -> tuple[Path, Path]:
    final = Path(final)
    return final.with_name(final.name + ".building"), final.with_name(final.name + ".lock")


def read_meta(db: Path) -> dict[str, str]:
    """meta of an errors index file, read-only; {} when it is missing or not readable."""
    if not Path(db).is_file():
        return {}
    try:
        con = sqlite3.connect(ro_uri(Path(db)), uri=True, timeout=READ_BUSY_S)
        try:
            return {str(r[0]): str(r[1]) for r in con.execute("SELECT k, v FROM meta")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


# how long a read of errors.db waits for another program's write lock (seventh review of 0.8.16: 5 + 5 + 10 s per lookup,
# 10 s of it in the ADS worker, while an SQLite browser held the file)
READ_BUSY_S = 2.0


def is_locked(reason: str | None) -> bool:
    """A read failed because another program holds the file (not because the file is damaged)."""
    return bool(reason) and bool(re.search(r"\b(?:locked|busy)\b", str(reason), re.I))


def read_meta_checked(db: Path) -> tuple[dict[str, str], str | None]:
    """meta of an errors index file and, when it gives none, why (meta_error's reasons) — one read: a locked file waits
    READ_BUSY_S once, not once per question."""
    if not Path(db).is_file():
        return {}, None
    try:
        con = sqlite3.connect(ro_uri(Path(db)), uri=True, timeout=READ_BUSY_S)
        try:
            meta = {str(r[0]): str(r[1]) for r in con.execute("SELECT k, v FROM meta")}
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {}, f"{type(exc).__name__}: {exc}"
    return meta, (None if meta else "its meta table is empty: not an index this plugin built")


def meta_error(db: Path) -> str | None:
    """Why an errors index file that exists cannot be read (an empty or cut copy, a file another program locks, a schema
    this plugin does not know) — None when its meta table reads. Sixth review of 0.8.16: such a file was 'missing', and
    a lookup raised sqlite's error out of a tool whose action had run."""
    try:
        con = sqlite3.connect(ro_uri(Path(db)), uri=True, timeout=READ_BUSY_S)
        try:
            if not con.execute("SELECT k, v FROM meta").fetchall():
                return "its meta table is empty: not an index this plugin built"
        finally:
            con.close()
    except sqlite3.Error as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _jl(s: str | None, default: Any) -> Any:
    try:
        v = json.loads(s) if s else default
    except ValueError:
        return default
    return v if isinstance(v, type(default)) else default


# --------------------------------------------------------------------------------------------- the sources
SOURCE_NAMES = {"infosys": "the Infosys index", "manuals": "manuals.db", "esi": "esi.db"}


def source_paths(kb: Path, lcid: str = L.DEFAULT_LCID, infosys_db: str | Path | None = None) -> dict[str, Path]:
    return {"infosys": Path(infosys_db) if infosys_db else L.locate(kb, "infosys", lcid),
            "manuals": L.locate(kb, "manuals"), "esi": L.index_file(kb, "esi")}


def identity(kind: str, p: Path) -> dict[str, Any]:
    """What a source index is right now: path, format, build stamp (and whether it is complete) — read-only."""
    p = Path(p)
    if not p.is_file():
        return {"path": str(p), "missing": True}
    try:
        con = sqlite3.connect(ro_uri(p), uri=True, timeout=5)
    except sqlite3.Error as exc:
        return {"path": str(p), "missing": True, "error": str(exc)}
    try:
        if kind == "esi":
            meta = {str(r[0]): str(r[1]) for r in con.execute("SELECT k, v FROM meta")}
            return {"path": str(p), "format": meta.get("format"), "built": meta.get("built"),
                    "stamp": meta.get("sources_stamp"), "state": meta.get("state")}
        meta = {str(r[0]): str(r[1]) for r in con.execute("SELECT key, value FROM meta")}
        if kind == "infosys":
            return {"path": str(p), "format": meta.get("indexer_version"), "built": meta.get("built"),
                    "stamp": meta.get("source_stamp") or meta.get("target_stamp"), "pages": meta.get("pages"),
                    "state": meta.get("state") or "complete"}
        docs = con.execute("SELECT path, sha256 FROM doc ORDER BY path").fetchall()
        h = hashlib.sha256(json.dumps([list(d) for d in docs]).encode("utf-8")).hexdigest()[:16]
        return {"path": str(p), "format": meta.get("text_format"), "built": meta.get("last_update"), "stamp": h,
                "docs": len(docs), "state": "complete" if meta.get("state", "idle") == "idle" else meta.get("state")}
    except sqlite3.Error as exc:
        return {"path": str(p), "missing": True, "error": f"not readable: {exc}"}
    finally:
        con.close()


def identities(kb: Path, lcid: str = L.DEFAULT_LCID, infosys_db: str | Path | None = None) -> dict[str, dict[str, Any]]:
    return {k: identity(k, p) for k, p in source_paths(kb, lcid, infosys_db).items()}


def _same_source(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return bool(a.get("missing")) == bool(b.get("missing")) and \
        all(a.get(k) == b.get(k) for k in ("format", "built", "stamp"))


def sources_stamp(ids: dict[str, dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps({k: [bool(v.get("missing")), v.get("format"), v.get("built"), v.get("stamp")]
                                      for k, v in sorted(ids.items())}).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------------------------- the golden checks
# N125 §5.4: what the index must hold after a build from the full local Infosys — a parser that loses them is a
# defect, and the index in use stays (the build is marked incomplete). (domain, value) → the symbol of the main
# variant; MIN_CODES: the codes a domain must have.
GOLDEN_SYMBOLS: tuple[tuple[str, int, str], ...] = (
    ("ads", 0x745, "ADSERR_CLIENT_SYNCTIMEOUT"), ("ads", 0x6, "ERR_TARGETPORTNOTFOUND"),
    ("ads", 0x710, "ADSERR_DEVICE_SYMBOLNOTFOUND"), ("win32", 6, "ERROR_INVALID_HANDLE"),
)
GOLDEN_FORMS: tuple[tuple[str, int, tuple[tuple[str, str], ...]], ...] = (
    ("ads", 0x745, (("dec", "1861"), ("hresult", "0x98110745"))),)
GOLDEN_MIN_CODES: dict[str, int] = {"ads": 139}
GOLDEN_CLEAN_TEXT: tuple[tuple[str, int], ...] = (("ads", 0x6),)     # no U+FFFD (N138: the decode upstream)
# the row that stands for a code of a domain: of the main variant of the document in the most books
MAIN_ORDER = "x.main DESC, x.books_n DESC, x.document"


def golden(con: sqlite3.Connection, previous: dict[str, int], force: bool, infosys: bool = True
           ) -> tuple[list[str], list[str]]:
    """-> (failures, accepted drops). The checks of the Infosys domains apply when the Infosys index was a source
    (without it there is nothing they could find). A domain with fewer codes than at the last build fails the build
    unless it was forced (then the drop is accepted and recorded)."""
    fails: list[str] = []
    counts = {r[0]: r[1] for r in con.execute("SELECT domain, COUNT(DISTINCT value_int) FROM code GROUP BY domain")}
    if not infosys:
        drops = [f"{d}: {n} codes at the last build, {counts.get(d, 0)} now" for d, n in sorted(previous.items())
                 if counts.get(d, 0) < n]
        if drops and not force:
            return [x + " (a drop is not accepted silently: rebuild with force to accept it)" for x in drops], []
        return [], drops
    for d, n in GOLDEN_MIN_CODES.items():
        if counts.get(d, 0) < n:
            fails.append(f"{d}: {counts.get(d, 0)} codes, at least {n} expected")

    def main_row(d: str, v: int) -> sqlite3.Row | None:
        return con.execute(f"SELECT c.* FROM code c JOIN variant x ON x.domain = c.domain AND x.variant = c.variant "
                           f"WHERE c.domain = ? AND c.value_int = ? ORDER BY {MAIN_ORDER} LIMIT 1", (d, v)).fetchone()
    for d, v, sym in GOLDEN_SYMBOLS:
        r = main_row(d, v)
        if r is None or r["symbol"] != sym:
            fails.append(f"{d} {v:#x}: {'not found' if r is None else repr(r['symbol'])}, {sym} expected")
    for d, v, forms in GOLDEN_FORMS:
        r = main_row(d, v)
        for k, want in forms:
            if r is None or (r[k] or "").lower() != want.lower():
                fails.append(f"{d} {v:#x}: {k} {None if r is None else r[k]!r}, {want} expected")
    for d, v in GOLDEN_CLEAN_TEXT:
        r = main_row(d, v)
        if r is not None and "\ufffd" in (r["text"] or ""):
            fails.append(f"{d} {v:#x}: its text holds U+FFFD ({r['text'][:80]!r}) — a decoding defect of the source")
    drops = [f"{d}: {n} codes at the last build, {counts.get(d, 0)} now" for d, n in sorted(previous.items())
             if counts.get(d, 0) < n]
    if drops and not force:
        fails += [x + " (a drop is not accepted silently: rebuild with force to accept it)" for x in drops]
        drops = []
    return fails, drops


# --------------------------------------------------------------------------------------------- building
class _Build:
    """One build: the rows of every source into the work file."""

    def __init__(self, con: sqlite3.Connection, lk: kb_lock.IndexLock | None):
        self.con, self.lk = con, lk
        self.report: list[tuple] = []
        self.domains: dict[str, dict[str, Any]] = {}
        self.variants = 0
        self.inconsistent: list[str] = []

    def _phase(self, **kv: Any) -> None:
        if self.lk is not None:
            self.lk.update(**kv)

    def _domain(self, did: str, title: str, origin: str, hints: list[str] | None = None) -> dict[str, Any]:
        if did not in self.domains:
            spec = DOMAINS.get(did, {})
            self.domains[did] = {"title": spec.get("title", title), "origin": spec.get("origin", origin),
                                 "priority": spec.get("priority", 10), "names": list(spec.get("names", ())),
                                 "hints": list(spec.get("hints", ())) or list(hints or []), "documents": set(),
                                 "note": spec.get("note")}
        return self.domains[did]

    # -- Infosys
    @staticmethod
    def _doc_domain(books: set[str], titles: set[str], pid: str) -> str:
        for did, spec in DOMAINS.items():
            if any(b in spec.get("books", ()) for b in books):
                return did
        for did, spec in DOMAINS.items():
            if any(re.fullmatch(t, x.strip(), re.I) for t in spec.get("pages", ()) for x in titles):
                return did
        return f"{sorted(books)[0]}/{pid}"

    def infosys(self, path: Path) -> dict[str, Any]:
        src = connect_ro(path)
        try:
            error_books = {b for s in DOMAINS.values() if s.get("error_book") for b in s.get("books", ())}
            pages = []
            alias_pages = []
            for r in src.execute("SELECT rowid, book, page_id, title, library, url FROM pages"):
                title = r["title"] or ""
                if any(re.fullmatch(t, title.strip(), re.I) for t in ALIAS_PAGES):
                    alias_pages.append(r)
                elif r["book"] in error_books or (CODE_PAGE.search(title) and not API_PAGE.search(title)):
                    pages.append(r)
            docs: dict[str, list[sqlite3.Row]] = {}
            for r in pages:
                docs.setdefault(str(r["page_id"]).split("_")[0], []).append(r)
            per: dict[tuple[str, str], list[tuple[sqlite3.Row, list[dict[str, Any]], str]]] = {}
            n_rows = 0
            for k, (pid, rs) in enumerate(sorted(docs.items())):
                self._phase(phase=f"Infosys code pages ({k + 1} of {len(docs)})", pages_done=k, pages_total=len(docs))
                did = self._doc_domain({r["book"] for r in rs}, {r["title"] or "" for r in rs}, pid)
                for r in rs:
                    md = src.execute("SELECT markdown FROM pages WHERE rowid = ?", (r["rowid"],)).fetchone()[0] or ""
                    by_dom: dict[str, list[dict[str, Any]]] = {}
                    tables = page_tables(md)
                    code_tables, skipped, bad, parsers, headers, rejected = 0, 0, [], set(), [], []
                    for label, trows in tables:
                        t = parse_table(trows, label)
                        headers.append(" | ".join(h for h in t["headers"] if h)[:80])
                        skipped += t["skipped"]
                        bad += t["inconsistent"]
                        rejected += t["rejected"]
                        if not t["rows"]:
                            continue
                        code_tables += 1
                        parsers.add(t["parser"])
                        # a table whose only code column is an HRESULT holds HRESULTs (the positive ones of the ADS
                        # page: S_OK …), not codes of the page's domain
                        tdom = "hresult" if t["rows"][0]["hresult_only"] and did == "ads" else did
                        by_dom.setdefault(tdom, []).extend(t["rows"])
                    found = sum(len(v) for v in by_dom.values())
                    if not found:
                        enum_rows = parse_code_enums(md, r["title"] or "")
                        if enum_rows:
                            by_dom[did] = enum_rows
                            parsers.add("enum")
                            code_tables += 1
                            found = len(enum_rows)
                    n_rows += found
                    self.inconsistent += [f"{r['book']}/{r['page_id']}: {x}" for x in bad[:3]]
                    for tdom, rows in by_dom.items():
                        per.setdefault((tdom, pid), []).append((r, rows, "+".join(sorted(parsers))))
                    if found:
                        status, reason = "rows", (f"value cells that hold no single code (a range, an offset, other "
                                                  f"text — not stored): {rejected[:2]}" if rejected else "")
                    elif not tables:
                        status, reason = "no table", "the page has no table"
                    else:
                        status, reason = "0 rows", (f"{len(tables)} table(s), none with a code column that parses "
                                                    f"(headers: {'; '.join(h for h in headers if h)[:200] or 'none'})"
                                                    + (f"; value cells that hold no single code (a range, an offset, "
                                                       f"other text): {rejected[:2]}" if rejected else ""))
                    self.report.append(("infosys", f"{r['book']}/{r['page_id']}", r["title"], r["book"], pid,
                                        ", ".join(sorted(by_dom)) or did, len(tables), code_tables, found, skipped,
                                        len(bad), "+".join(sorted(parsers)) or None, status, reason))
            for (did, pid), entries in per.items():
                self._variants(did, pid, [(f"{r['book']}/{r['page_id']}", r["book"], str(r["page_id"]), r["url"],
                                           r["library"], rows, parser, r["title"]) for r, rows, parser in entries])
            aliases = self._aliases(src, alias_pages)
            return {"code_pages": len(pages), "documents": len(docs), "rows_read": n_rows, "aliases": aliases}
        finally:
            src.close()

    def _variants(self, did: str, document: str, copies: list[tuple]) -> None:
        """The copies of one document in one domain: copies with the same rows are one variant (its books); the
        variant of the most books is the main one. Rows are stored once per variant."""
        groups: dict[str, list[tuple]] = {}
        for c in copies:
            key = sorted({(x["value"], x["symbol"] or "", x["text"] or "", x["kind"] or "", x["hresult"] or "")
                          for x in c[5]})
            groups.setdefault(hashlib.sha256(json.dumps(key, ensure_ascii=False).encode("utf-8")).hexdigest()[:16],
                              []).append(c)
        title = copies[0][7] or did
        dom = self._domain(did, title, "infosys", _derived_hints(title, copies[0][1]))
        dom["documents"].add(document)
        ordered = sorted(groups.items(), key=lambda kv: (-len({c[1] for c in kv[1]}), kv[0]))
        for n, (sha, cs) in enumerate(ordered):
            # the copy that stands for the variant: a library's own page first (it names its library), then by book
            rep = sorted(cs, key=lambda c: (c[4] is None, c[1], c[0]))[0]
            vid = f"{document}:v{n + 1}"
            books = sorted({c[1] for c in cs})
            self.con.execute("INSERT INTO variant VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (did, vid, document, sha, json.dumps(books), len(books),
                              json.dumps(sorted(c[0] for c in cs)), len(rep[5]), rep[6], int(n == 0), rep[0]))
            self.variants += 1
            seen: set[tuple] = set()
            for x in rep[5]:
                k = (x["value"], x["symbol"], x["text"])
                if k in seen:
                    continue
                seen.add(k)
                self.con.execute("INSERT INTO code (domain, value_int, hex, dec, hresult, symbol, text, kind, "
                                 "source_ref, book, page_id, url, variant, document, detail) "
                                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (did, x["value"], x["hex"], x["dec"], x["hresult"], x["symbol"], x["text"], x["kind"],
                                  x.get("ref") or rep[0], rep[1], rep[2], rep[3], vid, document,
                                  json.dumps(x["detail"], ensure_ascii=False) if x.get("detail") else None))

    def _aliases(self, src: sqlite3.Connection, alias_pages: list[sqlite3.Row]) -> int:
        """The names of every code (its symbol, its HRESULT) and the members of the .NET enumerations of ALIAS_PAGES
        — each member only where its value is a code of that domain (and its C++ name, when the page gives one, is the
        code's symbol: else reported)."""
        con = self.con
        con.execute("INSERT INTO alias SELECT DISTINCT domain, 'symbol', lower(symbol), value_int, source_ref FROM code "
                    "WHERE symbol IS NOT NULL AND symbol != ''")
        # the HRESULT of a code ('0x98110745' → ads 0x745): its 8 hex digits, lower case
        con.execute("INSERT INTO alias SELECT DISTINCT domain, 'hresult', lower(hresult), value_int, source_ref FROM code "
                    "WHERE hresult IS NOT NULL")
        n = 0
        for r in alias_pages:
            did = next(d for t, d in ALIAS_PAGES.items() if re.fullmatch(t, (r["title"] or "").strip(), re.I))
            md = src.execute("SELECT markdown FROM pages WHERE rowid = ?", (r["rowid"],)).fetchone()[0] or ""
            got, skipped, mism = 0, 0, []
            for label, trows in page_tables(md):
                t = parse_table(trows, label)
                for x in t["rows"]:
                    have = con.execute("SELECT symbol FROM code WHERE domain = ? AND value_int = ?",
                                       (did, x["value"])).fetchall()
                    if not have or not x["symbol"]:
                        skipped += 1
                        continue
                    cpp = re.search(r"C\+\+:\s*([A-Z][A-Z0-9_]+)", x["text"] or "")
                    if cpp and cpp.group(1) not in {h[0] for h in have}:
                        mism.append(f"{x['symbol']} = {x['value']}: C++ {cpp.group(1)}, the table {have[0][0]}")
                        continue
                    con.execute("INSERT INTO alias VALUES (?, 'dotnet', ?, ?, ?)",
                                (did, x["symbol"].lower(), x["value"], f"{r['book']}/{r['page_id']}"))
                    got += 1
            n += got
            self.report.append(("infosys", f"{r['book']}/{r['page_id']}", r["title"], r["book"],
                                str(r["page_id"]).split("_")[0], did, 1, 1 if got else 0, got, skipped, len(mism),
                                "alias", "alias" if got else "0 rows",
                                (f"names of {did} codes ({skipped} members not codes of {did})"
                                 + (f"; {len(mism)} whose C++ name is not the table's symbol: {mism[:3]}" if mism else ""))))
        return n

    # -- manuals
    def manuals(self, path: Path) -> dict[str, Any]:
        src = connect_ro(path)
        try:
            pages = src.execute("SELECT p.doc, p.page, p.section, p.text, d.name FROM page p JOIN doc d ON d.id = p.doc "
                                "ORDER BY d.name, p.page").fetchall()
            per: list[tuple] = []
            links = 0
            # a document that gives each code its own section (the EtherCAT system documentation): its sections, over
            # page breaks, before the page-by-page tables
            by_doc: dict[str, list[sqlite3.Row]] = {}
            for r in pages:
                by_doc.setdefault(r["name"], []).append(r)
            in_sections: set[tuple[str, int]] = set()
            for name, prs in by_doc.items():
                if not any(_AL_SECTION.match(ln) for r in prs for ln in (r["text"] or "").splitlines()):
                    continue
                res = parse_al_sections([(r["page"], r["text"]) for r in prs])
                if not res["rows"]:
                    continue
                first = res["pages"][0]
                sec = next((r["section"] for r in prs if r["page"] == first), None)
                for x in res["rows"]:
                    x["ref"] = f"{name}#p{x['page']}"
                per.append((f"{name}#p{first}", name, str(first), None, None, res["rows"], "al_sections", sec))
                self.report.append(("manuals", f"{name}#p{first}", sec, name, "al_status", "ethercat_al", 1, 1,
                                    len(res["rows"]), 0, 0, "al_sections", "rows",
                                    f"one section per code on pages {res['pages'][0]}-{res['pages'][-1]}"))
                in_sections |= {(name, pg) for pg in res["pages"]}
            for r in pages:
                if not AL_PAGE.search(r["text"] or "") or (r["name"], r["page"]) in in_sections:
                    continue
                ref = f"{r['name']}#p{r['page']}"
                res = parse_al_page(r["text"])
                if res["rows"]:
                    per.append((ref, r["name"], str(r["page"]), None, None, res["rows"], "al_lines", r["section"]))
                    self.report.append(("manuals", ref, r["section"], r["name"], "al_status", "ethercat_al", 1, 1,
                                        len(res["rows"]), 0, 0, "al_lines", "rows", ""))
                else:
                    links += 1
                    self.con.execute("INSERT INTO link VALUES ('ethercat_al', NULL, 'manuals', ?, ?, NULL, ?)",
                                     (ref, r["section"], res["reason"]))
                    self.report.append(("manuals", ref, r["section"], r["name"], "al_status", "ethercat_al", 0, 0, 0,
                                        0, 0, "al_lines", "link", res["reason"]))
            if per:
                self._variants("ethercat_al", "al_status", per)
            elif links:
                self._domain("ethercat_al", DOMAINS["ethercat_al"]["title"], "manuals")
            return {"al_pages": len(per), "al_links": links}
        finally:
            src.close()

    # -- ESI
    def esi(self, path: Path) -> dict[str, Any]:
        src = connect_ro(path)
        try:
            ids = src.execute("SELECT text_id, COUNT(*) FROM msg GROUP BY text_id").fetchall()
        finally:
            src.close()
        self.con.executemany("INSERT INTO link VALUES ('ethercat_diag', ?, 'esi', 'esi.db', NULL, ?, NULL)",
                             [(int(t), int(n)) for t, n in ids if t is not None])
        if ids:
            self._domain("ethercat_diag", DOMAINS["ethercat_diag"]["title"], "esi")
        return {"diag_text_ids": len(ids)}

    # -- the plugin's notes
    def annotations(self, built: str) -> list[str]:
        """Every note of ANNOTATIONS, with the build's check: a note on a code is verified when the documented row
        (main variant) has the note's symbol — else the mismatch is named (the note is still the plugin's)."""
        out = []
        for a in ANNOTATIONS:
            for v in a.get("values", ()) or (None,):
                doc = None if v is None else self.con.execute(
                    "SELECT c.symbol FROM code c JOIN variant x ON x.domain = c.domain AND x.variant = c.variant WHERE "
                    f"c.domain = ? AND c.value_int = ? ORDER BY {MAIN_ORDER} LIMIT 1", (a["domain"], v)).fetchone()
                if v is None:
                    on, note = None, "a note on the message's text, not on a documented code"
                elif doc is None:
                    on, note = None, f"{a['domain']} {v:#x} is not documented locally"
                elif a.get("symbol") and doc[0] != a["symbol"]:
                    on, note = None, f"the note names {a['symbol']}, the documented row {doc[0]}"
                    out.append(f"{a['domain']} {v:#x}: {note}")
                else:
                    on, note = built, "the documented row has the note's symbol"
                self.con.execute("INSERT INTO annotation (domain, value_int, symbol, pattern, context, cause, fix, "
                                 "origin, sha, verified_on, documented_symbol, check_note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (a["domain"], v, a.get("symbol"), a.get("pattern"), a.get("context"), a["cause"],
                                  a["fix"], "plugin", _ann_sha(a), on, doc[0] if doc else None, note))
        return out

    def finish(self) -> dict[str, Any]:
        con = self.con
        for did, d in self.domains.items():
            codes, rows = con.execute("SELECT COUNT(DISTINCT value_int), COUNT(*) FROM code WHERE domain = ?",
                                      (did,)).fetchone()
            variants = con.execute("SELECT COUNT(*) FROM variant WHERE domain = ?", (did,)).fetchone()[0]
            links = con.execute("SELECT COUNT(*) FROM link WHERE domain = ?", (did,)).fetchone()[0]
            con.execute("INSERT INTO domain VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (did, d["title"], d["origin"], d["priority"], json.dumps(d["names"]), json.dumps(d["hints"]),
                         len(d["documents"]), codes, rows, variants, links, d.get("note")))
        con.executemany("INSERT INTO source_report (source, ref, title, book, document, domain, tables, code_tables, "
                        "rows_found, skipped, inconsistent, parser, status, reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        self.report)
        counts = {r[0]: r[1] for r in con.execute("SELECT id, codes FROM domain WHERE codes > 0")}
        return {"domains": len(self.domains), "codes": sum(counts.values()),
                "rows": con.execute("SELECT COUNT(*) FROM code").fetchone()[0], "variants": self.variants,
                "domain_codes": counts,
                "zero_row_sources": con.execute("SELECT COUNT(*) FROM source_report WHERE rows_found = 0").fetchone()[0]}


def _build_into(db: Path, ids: dict[str, dict[str, Any]], stamp: str, built_by: str, lk: kb_lock.IndexLock | None,
                previous: dict[str, int], force: bool) -> dict[str, Any]:
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=OFF")       # the work file: nothing reads it before it is complete
        con.execute("PRAGMA synchronous=OFF")
        con.executescript(SCHEMA)
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("format", FORMAT), ("state", "building"), ("started", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("built_by", built_by or "twincat-mcp"), ("python", sys.version.split()[0]),
            ("sources", json.dumps(ids)), ("sources_stamp", stamp)])
        b = _Build(con, lk)
        res: dict[str, Any] = {}
        if not ids["infosys"].get("missing"):
            res["infosys"] = b.infosys(Path(ids["infosys"]["path"]))
        if not ids["manuals"].get("missing"):
            res["manuals"] = b.manuals(Path(ids["manuals"]["path"]))
        if not ids["esi"].get("missing"):
            res["esi"] = b.esi(Path(ids["esi"]["path"]))
        built = time.strftime("%Y-%m-%d %H:%M:%S")
        res["annotation_mismatches"] = b.annotations(built[:10])
        res.update(b.finish())
        res["inconsistent_rows"] = b.inconsistent[:20]
        fails, drops = golden(con, previous, force, infosys=not ids["infosys"].get("missing"))
        res["golden_failures"], res["accepted_drops"] = fails, drops
        res["missing_sources"] = [k for k, v in ids.items() if v.get("missing")]
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("domain_codes", json.dumps(res["domain_codes"])), ("codes", str(res["codes"])), ("rows", str(res["rows"])),
            ("variants", str(res["variants"])), ("domains", str(res["domains"])),
            ("zero_row_sources", str(res["zero_row_sources"])), ("golden", json.dumps(fails)),
            ("accepted_drops", json.dumps(drops)), ("missing_sources", json.dumps(res["missing_sources"])),
            ("annotation_mismatches", json.dumps(res["annotation_mismatches"])),
            ("inconsistent_rows", json.dumps(res["inconsistent_rows"])), ("built", built),
            ("state", "incomplete" if fails else "complete")])
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    return res


def _discard(building: Path, wait_s: float = SWAP_WAIT_S) -> None:
    """The build's own work file and its SQLite side files — the only thing a build deletes."""
    for p in [building] + [building.with_name(building.name + x) for x in SIDE]:
        t0 = time.time()
        while True:
            try:
                p.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if time.time() - t0 > wait_s:
                    raise ErrError(f"{p} is open in another program — it cannot be discarded; close it and build "
                                   "again", "busy") from None
                time.sleep(1.0)


def _swap(building: Path, final: Path, wait_s: float = SWAP_WAIT_S) -> str | None:
    t0 = time.time()
    while True:
        try:
            os.replace(building, final)
            return None
        except PermissionError:
            if time.time() - t0 > wait_s:
                return f"{final} is open in another program"
            time.sleep(1.0)


_INFOSYS_FILE = re.compile(r"infosys-(\d+)\.db$", re.I)


def recorded_infosys(final: Path, kb: Path, meta: dict[str, str] | None = None) -> Path | None:
    """The Infosys index a rebuild reads when none is named: this knowledge folder's, in the language the index in use
    was built from (infosys-<lcid>.db) — never the path it recorded. Review of 0.8.16: a copied folder rebuilt from the
    old folder's Infosys index (one index of two folders); a moved one found it 'missing' and a forced rebuild replaced
    5610 codes with 52. None: no record, or a file of no language (the folder's default is read)."""
    rec = (_jl((read_meta(final) if meta is None else meta).get("sources"), {}).get("infosys") or {}).get("path")
    m = _INFOSYS_FILE.search(str(rec or "").replace("\\", "/"))
    return L.locate(Path(kb), "infosys", m.group(1)) if m else None


def sources_building(ids: dict[str, dict[str, Any]]) -> list[str]:
    """The source indexes being built now (their build lock is held), by name."""
    from . import esi_index as E
    from . import infosys_build as IB
    locks = {"infosys": IB.side_files(Path(ids["infosys"]["path"]))[1],
             "esi": E.side_files(Path(ids["esi"]["path"]))[1],
             "manuals": Path(ids["manuals"]["path"]).with_name(Path(ids["manuals"]["path"]).name + ".lock")}
    return [SOURCE_NAMES[k] for k, p in locks.items() if kb_lock.held(p)]


def sources_ready(ids: dict[str, dict[str, Any]], kb: Path, lcid: str = L.DEFAULT_LCID) -> tuple[bool, str]:
    """Whether a build can start: a source exists, and no source index is being built (its first build, or a rebuild:
    the errors index would be built from the old one or none). -> (ready, why not)."""
    if all(v.get("missing") for v in ids.values()):
        return False, ("no source index: neither the Infosys index, manuals.db nor esi.db exists in the knowledge "
                       "folder")
    busy = sources_building(ids)
    if busy:
        return False, f"{', '.join(busy)} is being built — the errors index is built from it once that is complete"
    return True, ""


def build(final: Path, kb: Path, lcid: str | None = None, force: bool = False, built_by: str = "",
          log: Callable[[str], None] = print, infosys_db: str | Path | None = None) -> dict[str, Any]:
    """Build <final> from the source indexes of the knowledge folder, into <final>.building, checked (golden) and
    swapped in when complete. Without force an index built from exactly these sources in this format is left as it is.
    infosys_db=None: the Infosys index the index in use was built from, else the knowledge folder's."""
    final, kb = Path(final), Path(kb)
    building, lock = side_files(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    # re-review of 0.8.16: --lcid was ignored once an index existed — a language named is read; without one, the
    # language the index was built from (recorded_infosys), else the default
    if infosys_db is None and lcid is None:
        infosys_db = recorded_infosys(final, kb)
    lcid = lcid or L.DEFAULT_LCID
    ids = identities(kb, lcid, infosys_db)
    ok, why_not = sources_ready(ids, kb, lcid)
    if not ok:
        raise ErrError(why_not, "no_source")
    lk = kb_lock.IndexLock(lock)
    try:
        lk.__enter__()
    except kb_lock.Held as h:
        raise ErrError(f"another errors-index build is running (pid {h.info.get('pid')}, started "
                       f"{h.info.get('started')}, {h.info.get('phase') or 'starting'})", "busy") from None
    try:
        stamp = sources_stamp(ids)
        meta = read_meta(final)
        if final.is_file() and not meta:
            why = meta_error(final)
            if is_locked(why):
                # seventh review of 0.8.16: a complete, current index another program held was rebuilt as 'format
                # unknown' — a lock is not damage: nothing is built
                raise ErrError(f"the errors index {final} is locked by another program ({why}) — close it; nothing "
                               "was built", "busy")
        current = meta.get("format") == FORMAT and meta.get("state") == "complete"
        if not force and current and meta.get("sources_stamp") == stamp:
            log(f"up to date: {final} ({meta.get('codes')} codes in {meta.get('domains')} domains)")
            out: dict[str, Any] = {"result": "up_to_date", "index": str(final), "codes": _int(meta.get("codes"))}
            if building.is_file():               # left by an interrupted, failed or redundant build
                _discard(building)
                log(f"discarded {building} (a build left next to the complete, current index)")
                out["discarded"] = str(building)
            return out
        bm = read_meta(building) if building.is_file() else {}
        if (not force and bm.get("state") == "complete" and bm.get("format") == FORMAT
                and bm.get("sources_stamp") == stamp):
            why_not = _swap(building, final)
            if why_not is None:
                log(f"the finished build replaced {final} ({bm.get('codes')} codes)")
                return {"result": "replaced", "index": str(final), "codes": _int(bm.get("codes"))}
            log(f"a finished build is waiting to replace {final}, but {why_not}")
            return {"result": "not_replaced", "index": str(final), "building": str(building), "why": why_not}
        if building.is_file():
            _discard(building)                   # interrupted, failed its checks, or of other sources: start over
        why = ("forced" if force else "no index yet" if not final.is_file() else
               f"format {meta.get('format') or 'unknown'}, this plugin writes {FORMAT}" if meta.get("format") != FORMAT
               else "the index was never finished" if meta.get("state") != "complete" else "a source index changed")
        log(f"building {final} ({why}) from " + ", ".join(f"{SOURCE_NAMES[k]} ({v['path']})" for k, v in ids.items()
                                                          if not v.get("missing"))
            + ("; the current index stays in use until the new one is complete" if final.is_file() else ""))
        t0 = time.time()
        previous = {k: int(v) for k, v in _jl(meta.get("domain_codes"), {}).items()} if current else {}
        res = _build_into(building, ids, stamp, built_by, lk, previous, force)
        took = round(time.time() - t0, 1)
        if res["golden_failures"]:
            log(f"the new index FAILED its checks — {final} stays as it is; the work file {building} is kept for the "
                "report (the next build discards it): " + "; ".join(res["golden_failures"]))
            return {"result": "incomplete", "index": str(final), "building": str(building), "seconds": took, **res}
        lk.update(phase="replacing the index in use")
        why_not = _swap(building, final)
        if why_not is not None:
            log(f"the new index is complete ({building}) but {why_not} — it replaces the index at the next build")
            return {"result": "not_replaced", "index": str(final), "building": str(building), "why": why_not, **res}
        log(f"indexed {res['codes']} codes ({res['rows']} rows, {res['variants']} variants) in {res['domains']} domains "
            f"into {final} in {took} s; {res['zero_row_sources']} candidate page(s) gave 0 rows (source_report)"
            + (f"; accepted drops: {res['accepted_drops']}" if res["accepted_drops"] else "")
            + (f"; built without {res['missing_sources']}" if res["missing_sources"] else ""))
        return {"result": "built", "index": str(final), "seconds": took, **res}
    finally:
        lk.__exit__(None, None, None)


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------------------------- state
def state(final: Path, kb: Path | None, lcid: str | None = None, infosys_db: str | Path | None = None,
          detail: bool = False) -> dict[str, Any]:
    """missing | unreadable (the file is there and cannot be read — the reason names why; replaced by a rebuild once
    approved) | complete | rebuild_needed (another format, never finished, or a source index rebuilt / added since —
    the reason names it), plus a build running / interrupted / incomplete (failed its checks: the failures) / waiting
    to replace it / stale. An index that exists is in use as it is until the user approves the rebuild. detail: the
    domains (codes, rows, variants), the sources with 0 rows (kb_status)."""
    final = Path(final)
    building, lock = side_files(final)
    meta, why = read_meta_checked(final)
    out: dict[str, Any] = {"database": str(final), "format_expected": FORMAT}
    rec = _jl(meta.get("sources"), {})
    now: dict[str, dict[str, Any]] = {}
    if kb is not None:
        now = identities(Path(kb), lcid or L.DEFAULT_LCID,
                         infosys_db or (recorded_infosys(final, Path(kb), meta) if lcid is None else None))
    if not final.is_file():
        out["state"] = "missing"
    elif not meta:
        out["state"] = "unreadable"
        out["reason"] = why or "it could not be read"
        if is_locked(out["reason"]):
            out["locked"] = True
    elif meta.get("format") != FORMAT:
        out["state"] = "rebuild_needed"
        out["reason"] = f"format {meta.get('format') or 'unknown'}, this plugin reads {FORMAT}"
    elif meta.get("state") != "complete":
        out["state"] = "rebuild_needed"
        out["reason"] = "the index was never finished"
    else:
        out["state"] = "complete"
        changed, gone = [], []
        for k, was in rec.items():
            cur = now.get(k)
            if cur is None or _same_source(was, cur):
                continue
            if cur.get("missing") and not was.get("missing"):
                gone.append(f"{SOURCE_NAMES[k]} is missing now (built from it {was.get('built')}) — its codes are "
                            "those of then")
            elif was.get("missing"):
                changed.append(f"{SOURCE_NAMES[k]} exists now (it was missing at the build)")
            else:
                changed.append(f"{SOURCE_NAMES[k]} was rebuilt since (built {was.get('built')}, now {cur.get('built')}"
                               + (f", format {was.get('format')} → {cur.get('format')}" if was.get("format") !=
                                  cur.get("format") else "") + ")")
        if changed:
            out["state"] = "rebuild_needed"
            out["reason"] = "; ".join(changed)
        if gone:
            out["sources_gone"] = gone
    if rec:
        out["sources"] = {k: {kk: v.get(kk) for kk in ("path", "format", "built", "missing") if v.get(kk) is not None}
                          for k, v in rec.items()}
    for k in ("codes", "rows", "variants", "domains", "zero_row_sources"):
        if meta.get(k):
            out[k] = _int(meta[k])
    for k in ("built", "built_by", "format"):
        if meta.get(k):
            out[k] = meta[k]
    for k in ("missing_sources", "accepted_drops", "annotation_mismatches"):
        v = _jl(meta.get(k), [])
        if v:
            out[k] = v
    if detail and final.is_file() and meta:
        out.update(_detail(final))
    running = kb_lock.held(lock)
    if running or building.is_file():
        info, beat = kb_lock.read(lock)
        if running:
            out["rebuild"] = {"state": "running", "phase": info.get("phase"), "pid": info.get("pid"),
                              "started": info.get("started"),
                              "seconds_since_progress": round(time.time() - beat) if beat else None}
        else:
            bm = read_meta(building)
            stamp_now = sources_stamp(now) if now else None
            if bm.get("state") == "incomplete":
                rbs = "incomplete"
            elif out["state"] == "complete":
                rbs = "stale"
            elif bm.get("state") == "complete" and bm.get("format") == FORMAT and bm.get("sources_stamp") == stamp_now:
                rbs = "waiting_to_replace"
            elif bm.get("state") == "complete":
                rbs = "stale"
            else:
                rbs = "interrupted"
            out["rebuild"] = {"state": rbs, "file": str(building)}
            if rbs == "incomplete":
                out["rebuild"]["failures"] = _jl(bm.get("golden"), [])
                out["rebuild"]["built"] = bm.get("built")
    out["message"] = message(out)
    return out


def _detail(final: Path) -> dict[str, Any]:
    """kb_status: codes / rows / variants of every domain, the candidate sources that gave 0 rows (with why)."""
    out: dict[str, Any] = {}
    try:
        con = connect_ro(final)
    except sqlite3.Error:
        return out
    try:
        out["domains_detail"] = {
            r["id"]: {k: r[k] for k in ("title", "origin", "documents", "codes", "rows", "variants", "links") if r[k]}
            for r in con.execute("SELECT * FROM domain ORDER BY priority, id")}
        zero = con.execute("SELECT source, ref, title, status, reason FROM source_report WHERE rows_found = 0 "
                           "ORDER BY source, ref").fetchall()
        out["zero_row_sources_list"] = {"count": len(zero), "first": [dict(z) for z in zero[:40]],
                                        "note": "candidate pages that gave no code row, with why (all of them: the "
                                                "table source_report of errors.db)"}
        out["al_status_links"] = con.execute("SELECT COUNT(*) FROM link WHERE domain = 'ethercat_al'").fetchone()[0]
    finally:
        con.close()
    return out


def message(s: dict[str, Any]) -> str:
    st = s["state"]
    rb = s.get("rebuild") or {}
    if rb.get("state") == "running":
        tail = f"; a build is running ({rb.get('phase') or 'starting'})"
    elif rb.get("state") == "waiting_to_replace":
        tail = "; a finished build replaces it at the next build"
    elif rb.get("state") == "interrupted":
        tail = "; a build was interrupted — the next one starts over"
    elif rb.get("state") == "incomplete":
        tail = (f"; the last build ({rb.get('built')}) FAILED its checks and replaced nothing — "
                + "; ".join(rb.get("failures") or [])[:300])
    elif rb.get("state") == "stale":
        tail = f"; a work file was left ({rb.get('file')}) — the next build discards it"
    else:
        tail = ""
    if st == "missing":
        return "no errors index yet — its first build starts by itself once a source index is there" + tail
    if st == "unreadable" and s.get("locked"):
        return (f"the errors index is locked by another program ({s.get('reason')}) — close that program (an SQLite "
                "browser, a copy in progress); nothing documented can be named until then, and a rebuild does not help"
                + tail)
    if st == "unreadable":
        return (f"the errors index cannot be read ({s.get('reason')}) — nothing documented can be named from it until "
                "a rebuild replaces it, once approved: kb_rebuild(index='errors')" + tail)
    base = (f"{s.get('codes', '?')} codes in {s.get('domains', '?')} domains ({s.get('variants', '?')} table variants), "
            f"built {s.get('built', '?')}")
    if s.get("missing_sources"):
        base += f"; built without {', '.join(SOURCE_NAMES.get(k, k) for k in s['missing_sources'])}"
    if s.get("sources_gone"):
        base += "; " + "; ".join(s["sources_gone"])
    if st == "complete":
        return base + tail
    return (f"{base}; in use as it is, a REBUILD is due — {s.get('reason')} — the rebuild runs once approved: "
            "kb_rebuild(index='errors')" + tail)


def answer_source(db: Path, st: dict[str, Any]) -> dict[str, Any]:
    """The index an answer came from — the same block in the server's err_lookup and the skill's (as esi_index)."""
    src: dict[str, Any] = {"database": str(db), "state": st["state"], "built": st.get("built"),
                           "format": st.get("format")}
    if st["state"] != "complete" or st.get("rebuild") or st.get("sources_gone") or st.get("missing_sources"):
        src["state_note"] = st["message"]
    return src


# --------------------------------------------------------------------------------------------- reading
_NAME_TOKEN = re.compile(r"(?<![\w#])[A-Za-z_][A-Za-z0-9_]{2,}(?![\w#])")
_PYADS = re.compile(r"\badserror\b\s*:[^\n]{0,200}?\((-?\d+)\)", re.I)        # pyads: 'ADSError: symbol not found (1808).'


# an HRESULT written as one: 0x8…/0x9…/0xC… with 8 hex digits ('ConsumeXml failed (0x98510004)') — a code wherever it
# stands (third review of 0.8.16); an address or an index is written otherwise
_HRESULT_TOKEN = re.compile(r"0[xX](?:98[0-9A-Fa-f]{2}|800[0-7]|8004|[Cc]000)(?!0000)[0-9A-Fa-f]{4}")
# fourth review of 0.8.16: 'invalid index offset 0x80000000', 'at address 0x8C2A1F00', 'IG:0x80000001' were read as
# codes — an HRESULT is of a facility (0x9811 ADS, 0x9851 System Manager, 0x8007 Win32, 0x8000/0x8001/0x8004 COM,
# 0xC000 the TcCOM loader's NTSTATUS codes — fifth review: 'Loading … failed (0xC0000428)'), never 0x…0000, and never
# right after a word that makes it an index group, an offset, an address or a handle ('at' is none: 'failed at 0x…')
_NOT_HRESULT_BEFORE = re.compile(r"\b(?:ig|igrp|group|indexgroup|offset|ioffs|io|address|addr|adr|pointer|ptr|"
                                 r"handle|crc|checksum|mark)\W{0,3}$", re.I)


def _after_domain_name(q: str, start: int) -> bool:
    """The token at q[start:] stands right after a domain's name ('AL status 0x0011', 'ADS 0x745', 'NC 0x4221')."""
    pre = q[:start]
    if not re.search(r"[\s:=#('\"‘“]$", pre):         # seventh review of 0.8.16: "AL Status '0x001b'" too
        return False
    nw = _norm_words(pre)
    return any(nw.endswith(f" {n} ") for d in DOMAINS.values() for n in tuple(d.get("names", ()))
               + tuple(d.get("hex_after", ())))


def read_query(q: str) -> dict[str, Any]:
    """What a query names: codes by docsearch's code-form rules (a number is a code when it is the whole query or
    stands next to a word that says so — N121 / N122; a hex number too: not anywhere), a hex number right after a
    domain's name ('AL status 0x0011' — 0.8.16 acceptance: 'status' is no code word, the domain's name says it all the
    same; a decimal number needs a code word: 'ADS 851' stays no code), pyads' 'ADSError: … (1808)', the names that can
    be a code's symbol (identifiers with '_', inner capitals or digits: ADSERR_CLIENT_SYNCTIMEOUT, TargetPortNotFound)
    and the message ids of a domain's id_form ('C0373': a compiler message)."""
    qt = DS.query_text(q)
    toks = DS.code_tokens(qt, min_digits=1, hex_anywhere=True)
    codes = [{"token": t["token"], "value": t["value"], "how": t["how"]} for t in toks
             if t["how"] == "dec" or t["whole"] or t["next_to_code_word"] or _after_domain_name(qt, t["start"])
             or (_HRESULT_TOKEN.fullmatch(t["token"]) and not _NOT_HRESULT_BEFORE.search(qt[:t["start"]]))]
    for m in _PYADS.finditer(q):
        if all(c["value"] != int(m.group(1)) for c in codes):
            codes.append({"token": m.group(1), "value": int(m.group(1)), "how": "dec"})
    names, caps = [], []
    for m in _NAME_TOKEN.finditer(q):
        t = m.group(0)
        if DS._HEX.match(t) or t.upper() in ("ADSERROR",):
            continue
        if "_" in t.strip("_") or re.search(r"[a-z][A-Z]", t) or (re.search(r"\d", t) and re.search(r"[A-Za-z]{2}", t)):
            names.append(t)
        elif t.isupper() and sum(ch.isalpha() for ch in t) >= 4:
            # review of 0.8.16: a symbol in capitals without '_' (WSAETIMEDOUT, WSAEINTR) was read as a word and its
            # text search answered WSAEHOSTDOWN — such a word is a name when the alias table has it ('ERROR' in a log
            # line is none): err_lookup decides
            caps.append(t)
    ids = [{"token": m.group(0), "domain": did} for did, spec in DOMAINS.items() if spec.get("id_form")
           for m in re.finditer(spec["id_form"], qt)]
    return {"codes": codes, "names": list(dict.fromkeys(names)), "caps": list(dict.fromkeys(caps)), "ids": ids}


def _forms(v: int) -> set[int]:
    """The same number in 32 bits: -6908 is 0xFFFFE504, 0x80010001 is -2147418111; a 32-bit code printed as a 64-bit
    signed number (0xFFFFFFFF80004005) is that code. A number no 32-bit code can be has no form — nothing is looked up
    (seventh review of 0.8.16: 'ADSError 18446744073709551615' crashed sqlite's integer binding)."""
    if (1 << 64) - (1 << 31) <= v < (1 << 64):
        v -= 1 << 64
    if not -(1 << 31) <= v < (1 << 32):
        return set()
    out = {v}
    if v < 0 and v >= -(1 << 31):
        out.add(v & 0xFFFFFFFF)
    elif (1 << 31) <= v < (1 << 32):
        out.add(v - (1 << 32))
    return out


class ErrIndex:
    """Read-only access to errors.db."""

    def __init__(self, db: Path, busy_s: float = READ_BUSY_S):
        self.db = Path(db)
        self.busy_s = busy_s

    @contextlib.contextmanager
    def con(self) -> Iterator[sqlite3.Connection]:
        if not self.db.is_file():
            raise ErrError(f"no errors index yet ({self.db})", "no_index")
        # sixth review of 0.8.16: sqlite's own error (a locked or cut file) is this module's — every caller handles
        # ErrError; an sqlite3.Error escaped line_codes after an XAE action had run and made it look failed
        try:
            c = connect_ro(self.db, timeout=self.busy_s)
        except sqlite3.Error as exc:
            raise ErrError(f"the errors index cannot be read ({self.db}): {exc}", "unreadable") from exc
        try:
            yield c
        except (sqlite3.Error, OverflowError) as exc:
            raise ErrError(f"the errors index could not be read ({self.db}): {type(exc).__name__}: {exc}",
                           "unreadable") from exc
        finally:
            c.close()


def _domains(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    out = {}
    for r in con.execute("SELECT * FROM domain"):
        d = dict(r)
        d["names"], d["hints"] = _jl(d["names"], []), _jl(d["hints"], [])
        out[d["id"]] = d
    for did, spec in DOMAINS.items():              # the domains without rows: searched all the same, said so
        out.setdefault(did, {"id": did, "title": spec["title"], "origin": spec["origin"], "priority": spec["priority"],
                             "names": list(spec.get("names", ())), "hints": list(spec.get("hints", ())), "codes": 0,
                             "rows": 0, "variants": 0, "links": 0, "note": spec.get("note")})
    return out


def named_domains(q: str) -> list[str]:
    """The domains a message names by their names ('LabVIEW error 1808' → labview) — without an index."""
    qn = _norm_words(q)
    return [d for d, spec in DOMAINS.items() if any(f" {n} " in qn for n in spec.get("names", ()))]


def _named(qn: str, doms: dict[str, dict[str, Any]]) -> list[str]:
    return [d for d, x in doms.items() if any(f" {n} " in qn for n in x["names"])]


def _hint_hits(qn: str, d: dict[str, Any]) -> list[str]:
    return [h for h in d["hints"] if f" {h} " in qn]


def _domain_filter(domain: str | None, doms: dict[str, dict[str, Any]]) -> list[str] | None:
    if not domain:
        return None
    d = domain.strip()
    if d in doms:
        return [d]
    low = d.lower()
    hit = [k for k, x in doms.items() if low in k.lower() or low in (x.get("title") or "").lower()]
    if not hit:
        raise ErrError(f"domain {domain!r} is none of the domains of the errors index: "
                       f"{', '.join(sorted(k for k in doms if '/' not in k))} and the code pages of libraries "
                       "(their book/page id — any part of the id or title selects them)")
    return hit


CAPS_FAMILY_MIN = 10        # capitals-only symbols sharing their first three letters in one domain: a family (WSA…)


def _caps_symbol(con: sqlite3.Connection, token: str, query: str, named: list[str] | None = None) -> bool:
    """A word in capitals without '_' (WSAETIMEDOUT) is a code's name when it is the whole query (quotes and a final
    period aside), a symbol of a domain the message names ('labview TIMEOUT', 'NC: AXISNOTENABLED'), or a member of a
    family of such symbols in one domain (Winsock's 54 WSA… names: WSAETIMEDOUT, WSASYSNOTREADY) — not when it is a
    library's one-word symbol that is also a word of any log line (TIMEOUT, INTERNAL, NOMEMORY)."""
    if re.sub(r"[^\w]", "", query).upper() == token.upper():
        return True
    if named and con.execute("SELECT 1 FROM alias WHERE kind = 'symbol' AND form = ? AND domain IN (%s) LIMIT 1"
                             % ",".join("?" * len(named)), (token.lower(), *named)).fetchone():
        return True
    pre = token[:3].lower()
    for (n,) in con.execute("SELECT MAX(c) FROM (SELECT domain, COUNT(DISTINCT form) AS c FROM alias WHERE kind = "
                            "'symbol' AND form LIKE ? ESCAPE '\\' AND form NOT GLOB '*[^a-z]*' GROUP BY domain)",
                            (pre.replace("_", "\\_") + "%",)):
        return bool(n and n >= CAPS_FAMILY_MIN)
    return False


def _by_value(con: sqlite3.Connection, v: int) -> tuple[list[tuple[str, int, list[sqlite3.Row], str]],
                                                         list[tuple[str, int, int]]]:
    """Where a number is documented: in each of its 32-bit forms (-6908 = 0xFFFFE504), as the HRESULT of a code
    (0x98110745 → ads 0x745), and the ESI text ids it links -> ([(domain, value, rows, how)], [(domain, value, n)])."""
    found: list[tuple[str, int, list[sqlite3.Row], str]] = []
    links: list[tuple[str, int, int]] = []
    for f in sorted(_forms(v)):
        by: dict[str, list[sqlite3.Row]] = {}
        for r in con.execute("SELECT * FROM code WHERE value_int = ?", (f,)).fetchall():
            by.setdefault(r["domain"], []).append(r)
        for did, rs in by.items():
            found.append((did, f, rs, "value"))
    for f in sorted(_forms(v)):                # a code stored as its value is matched by value, not as an HRESULT
        for a in con.execute("SELECT DISTINCT domain, value_int FROM alias WHERE kind = 'hresult' AND form = ?",
                             (f"0x{f & 0xFFFFFFFF:08x}",)):
            rs = con.execute("SELECT * FROM code WHERE domain = ? AND value_int = ?", (a[0], a[1])).fetchall()
            if rs:
                found.append((a[0], a[1], rs, "hresult"))
        for lk in con.execute("SELECT domain, value_int, count FROM link WHERE value_int = ? AND target = 'esi'", (f,)):
            links.append((lk[0], lk[1], lk[2]))
    return found, links


def _row_out(con: sqlite3.Connection, did: str, value: int, rows: list[sqlite3.Row], doms: dict[str, dict[str, Any]],
             how: str, message: str) -> dict[str, Any]:
    """One documented code of one domain: its forms, symbol, Beckhoff's text — every text when the variants differ,
    with their books, the main one the text of the most books — the variants that lack it, the ref, the plugin note."""
    vinfo = {v["variant"]: v for v in con.execute(
        "SELECT * FROM variant WHERE domain = ? AND document IN (SELECT DISTINCT document FROM code WHERE domain = ? "
        "AND value_int = ?)", (did, did, value))}
    groups: dict[tuple, dict[str, Any]] = {}
    # the copy in the most books first: it stands for its text (its ref is the one shown)
    rows = sorted(rows, key=lambda r: (-(vinfo[r["variant"]]["books_n"] if r["variant"] in vinfo else 0),
                                       r["variant"] or ""))
    for r in rows:
        v = vinfo.get(r["variant"])
        g = groups.setdefault((r["symbol"], r["text"]), {"symbol": r["symbol"], "text": r["text"], "books": set(),
                                                        "refs": [], "row": r, "main": False})
        g["books"] |= set(_jl(v["books"], []) if v is not None else [r["book"]])
        # the row's own page (a code's section in a manual: the page it is on), the variant's otherwise
        g["refs"].append(r["source_ref"] or (v["ref"] if v is not None else None))
        g["main"] = g["main"] or bool(v is not None and v["main"])
    # the text of the most books first (said so); between equals, a main variant's
    ordered = sorted(groups.values(), key=lambda g: (-len(g["books"]), not g["main"], g["refs"][0]))
    main = ordered[0]["row"]
    out: dict[str, Any] = {"domain": did, "domain_title": doms.get(did, {}).get("title"), "value": value,
                           "hex": main["hex"], "dec": main["dec"]}
    if main["hresult"]:
        out["hresult"] = main["hresult"]
    if main["symbol"]:
        out["symbol"] = main["symbol"]
    out["text"] = main["text"]
    if main["kind"]:
        out["kind"] = main["kind"]
    detail = _jl(main["detail"], {})        # the labelled fields of the code's section (AL status codes)
    if detail:
        out["detail"] = detail
    out["matched_by"] = how
    out["ref"] = ordered[0]["refs"][0]
    if main["url"]:
        out["url"] = main["url"]
    out["books"] = len(ordered[0]["books"])
    if len(ordered) > 1:
        out["texts"] = [{"text": g["text"], **({"symbol": g["symbol"]} if g["symbol"] else {}),
                         "books": sorted(g["books"]), "main": i == 0} for i, g in enumerate(ordered)]
        out["texts_note"] = ("the copies of this table differ: every text with the books that carry it — the main "
                             "one is the text of the most books")
    have = {r["variant"] for r in rows}
    lack = [v for vid, v in vinfo.items() if vid not in have]
    if lack:
        out["missing_in"] = [{"books": _jl(v["books"], []), "ref": v["ref"], "rows": v["row_count"]} for v in lack]
        out["missing_note"] = (f"{sum(len(_jl(v['books'], [])) for v in lack)} cop(ies) of this table do not have "
                               f"{main['hex']}: missing_in names them")
    a = notes_for(did, value, message)
    if a is not None:
        out["plugin_note"] = note_out(a, verified=_verified(con, a, value))
    return out


def _verified(con: sqlite3.Connection | None, a: dict[str, Any], value: int | None) -> dict[str, Any]:
    if con is None:
        return {"verified_on": None, "check": "no errors index: not checked against the documentation"}
    r = con.execute("SELECT verified_on, check_note FROM annotation WHERE sha = ? AND domain = ? AND "
                    "(value_int = ? OR (value_int IS NULL AND ? IS NULL))", (_ann_sha(a), a["domain"], value, value)
                    ).fetchone()
    if r is None:
        return {"verified_on": None, "check": "a note newer than the errors index: not checked yet (next rebuild)"}
    return {"verified_on": r["verified_on"], "check": r["check_note"]}


def _diag_out(kb: Path | None, value: int, count: int) -> dict[str, Any]:
    # review of 0.8.16: every match has the fields of a documented code (value, hex, dec, text, ref) — doc_note read
    # r['ref'] and kb_search d0['dec'] of an ESI text id and raised KeyError instead of the error it annotated
    out: dict[str, Any] = {"domain": "ethercat_diag", "domain_title": DOMAINS["ethercat_diag"]["title"], "value": value,
                           "hex": f"#x{value:04X}", "dec": str(value), "ref": f"esi.db, text id #x{value:04X}",
                           "documented_in": "esi.db", "messages": count,
                           "see": f"hw_diag(code='#x{value:04X}') — the messages, causes and remedies (with device=…: "
                                  "that device's catalogue)", "matched_by": "value"}
    if kb is not None:
        p = L.index_file(Path(kb), "esi")
        if p.is_file():
            try:
                c = sqlite3.connect(ro_uri(p), uri=True, timeout=5)
                try:
                    r = c.execute("SELECT message FROM msg WHERE text_id = ? ORDER BY id LIMIT 1", (value,)).fetchone()
                    # texts that differ in case or a final period only are one (third review: 6 ids)
                    n = c.execute("SELECT COUNT(DISTINCT lower(rtrim(trim(message), '.'))) FROM msg WHERE text_id = ?",
                                  (value,)).fetchone()[0]
                finally:
                    c.close()
                if r and n == 1:
                    out["text"] = r[0]
                elif r:
                    # re-review of 0.8.16: 1473 of 3028 text ids have other texts on other devices (#x4101: 16) — the
                    # first one annotated another device's error as the documented meaning
                    out["device_texts"] = n
                    out["device_texts_note"] = (f"{n} different texts on different devices — the device's own: "
                                         f"hw_diag(code='#x{value:04X}', device=…)")
            except sqlite3.Error:
                pass
    return out


def _fulltext(kb: Path | None, infosys_db: str | Path | None, query: str) -> dict[str, Any] | None:
    """documented: false — the pages of the Infosys index that mention it (full text), marked as such: they do not
    define it."""
    if kb is None and not infosys_db:
        return None
    p = Path(infosys_db) if infosys_db else L.locate(Path(kb), "infosys")      # type: ignore[arg-type]
    if not p.is_file():
        return None
    from . import infosys as INF
    try:
        c = sqlite3.connect(ro_uri(p), uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            res = INF.search_index(c, query, 5)
        finally:
            c.close()
    except (sqlite3.Error, DS.SearchError):
        return None
    hits = [{"ref": f"{r['book']}/{r['pid']}", "title": r["title"], "excerpt": r["s"]} for r in res["rows"]]
    return {"note": "full-text hits of the Infosys index, as found: pages that mention it — no meaning is taken from "
                    "them (a code table among them is one of another domain: other_domains)", "query": query,
            "hits": hits}


def err_lookup(query: str, domain: str | None = None, kb: Path | str | None = None, limit: int = 5,
               infosys_db: str | Path | None = None, fulltext: bool = True) -> dict[str, Any]:
    """N125: what a code, a name or a whole error message is in the local Beckhoff documentation — the one lookup of
    the server's err_lookup tool and the twincat-docs skill's err_lookup command (one function: the same answer).

    A number is a code when it is the whole query or stands next to a word that says so (docsearch's rules); a symbol
    (ADSERR_…, ERR_…, ERROR_…, the .NET enum names) is looked up by name; with neither, the words are looked for in the
    documented texts. A number documented in several domains lists all, ranked by the words of the message — a message
    that names a domain ('ADS error 6') is answered for it (matches) and the same number elsewhere is listed apart
    (other_domains); `ranking` says why. Not documented: documented=false, the domains searched, the full-text hits
    marked as such — never a meaning. The plugin's own notes are labelled so (plugin_note / plugin_notes)."""
    q = DS.query_text(query).strip()
    if not q:
        raise ErrError("empty query — give a code (0x745, 1861, 16#745), a name (ADSERR_CLIENT_SYNCTIMEOUT) or the "
                       "error message", "empty_query")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= LIMIT_MAX:
        raise ErrError(f"limit must be 1-{LIMIT_MAX} (got {limit!r})")
    kbp = Path(kb) if kb is not None else None
    db = L.index_file(kbp, "errors") if kbp is not None else None
    st = state(db, kbp, infosys_db=infosys_db) if db is not None else {"state": "missing",
                                                                          "message": "no knowledge folder chosen yet"}
    read = read_query(q)
    qn = _norm_words(q)
    out: dict[str, Any] = {"query": q, "read_as": read["codes"]}
    if read["names"]:
        out["names"] = read["names"]
    if read["ids"]:
        out["message_ids"] = read["ids"]
    pn = pattern_note(q)
    notes: list[dict[str, Any]] = []
    if db is None or not db.is_file() or st["state"] == "unreadable":
        out["documented"] = None
        out["note"] = (("no errors index (index\\errors.db)" if st["state"] != "unreadable" else
                        "the errors index (index\\errors.db) cannot be read") + ": nothing documented can be named — "
                       + st["message"] + ". The plugin's own notes below are not Beckhoff's text.")
        for c in read["codes"]:
            for did in DOMAINS:
                for v in _forms(c["value"]):
                    a = notes_for(did, v, q)
                    if a is not None:
                        notes.append(note_out(a, verified=_verified(None, a, v)) | {"value": v})
        if pn:
            notes.append(note_out(pn[0], pn[1], _verified(None, pn[0], None)))
        if notes:
            out["plugin_notes"] = _uniq(notes)
        out["source"] = answer_source(db, st) if db is not None else {"state": "missing", "state_note": st["message"]}
        return out
    with ErrIndex(db).con() as con:
        doms = _domains(con)
        want = _domain_filter(domain, doms)
        named = list(dict.fromkeys(_named(qn, doms) + [i["domain"] for i in read["ids"] if i["domain"] in doms]))
        found: list[tuple[str, int, list[sqlite3.Row], str]] = []
        links: list[tuple[str, int, int]] = []
        for v in [c["value"] for c in read["codes"]]:
            f_found, f_links = _by_value(con, v)
            found += f_found
            links += f_links
        sym_domains: set[str] = set()
        known_caps = []
        code_names: list[str] = []
        for n in read["names"] + read["caps"]:
            if n in read["caps"] and not _caps_symbol(con, n, q, named):
                # re-review of 0.8.16: 'TIMEOUT', 'INTERNAL', 'NOMEMORY' are symbols of some library table and words of
                # every log line ('[ ERROR ] TIMEOUT while waiting' answered LabVIEW 0xFFFFDDEB) — not a code's name
                continue
            hit: list[str] = []
            for a in con.execute("SELECT DISTINCT domain, value_int, kind FROM alias WHERE form = ? AND kind IN "
                                 "('symbol', 'dotnet')", (n.lower(),)):
                rs = con.execute("SELECT * FROM code WHERE domain = ? AND value_int = ?", (a[0], a[1])).fetchall()
                if rs:
                    found.append((a[0], a[1], rs, "symbol" if a[2] == "symbol" else "dotnet name"))
                    hit.append(a[0])
            if hit and n in read["caps"]:
                known_caps.append(n)
            if hit and n not in code_names:
                code_names.append(n)
            if hit:
                # a symbol names the code space it belongs to: a domain the message names, else of the domains that have
                # it the general one (ADSERR_CLIENT_SYNCTIMEOUT is ads 0x745; the LabVIEW table numbers the same symbol
                # its own way — listed apart, in other_domains). Third review of 0.8.16: 'win32 error WSAETIMEDOUT'
                # was answered for ads too
                in_named = [d for d in hit if d in named]
                if not in_named:
                    sym_domains.add(min(hit, key=lambda d: (doms.get(d, {}).get("priority", 10), d)))
        if known_caps:                  # a capitalised word the alias table knows is a name of a code (WSAEINTR)
            read = {**read, "names": read["names"] + known_caps}
            out["names"] = read["names"]
        if code_names:
            # the names of the query the index knows as a code's name (symbol or .NET name) — as the query writes them
            out["code_names"] = code_names
        text_search = not read["codes"] and not found and not read["ids"]
        if text_search:
            # the words that can pick a text: not 'error' / 'code' / a domain's name ('ADS error on port 851' → port)
            dom_words = {w for x in doms.values() for n in x["names"] for w in n.split()}
            words = [w for w in DS.informative_words(q) if not w.isdigit() and w not in _GENERIC_WORDS
                     and w not in dom_words][:8]
            if words:
                sql = "SELECT * FROM code WHERE " + " AND ".join("lower(text) LIKE ? ESCAPE '\\'" for _ in words)
                rows = con.execute(sql + " LIMIT 400", ["%" + w.replace("\\", "\\\\").replace("%", "\\%")
                                                        .replace("_", "\\_") + "%" for w in words]).fetchall()
                by2: dict[tuple, list[sqlite3.Row]] = {}
                for r in rows:
                    by2.setdefault((r["domain"], r["value_int"]), []).append(r)
                for (did, v), rs in by2.items():
                    found.append((did, v, con.execute("SELECT * FROM code WHERE domain = ? AND value_int = ?",
                                                      (did, v)).fetchall(), "text"))
                out["searched_words"] = words
        # one entry per (domain, value)
        seen: dict[tuple, tuple] = {}
        for did, v, rs, how in found:
            if want is not None and did not in want:
                continue
            seen.setdefault((did, v), (did, v, rs, how))
        if want is not None:
            links = [x for x in links if x[0] in want]
        named_all = sorted(set(named) | sym_domains)
        if named_all:
            out["domain_named"] = named_all

        def rank(did: str) -> tuple:
            d = doms.get(did, {"priority": 10, "hints": []})
            return (did not in named_all, -len(_hint_hits(qn, d)), d.get("priority", 10), did)
        items = sorted(seen.values(), key=lambda x: (rank(x[0]), x[1]))
        entries = [_row_out(con, did, v, rs, doms, how, q) for did, v, rs, how in items]
        entries += [_diag_out(kbp, v, n) for _d, v, n in sorted(set(links))]
        entries.sort(key=lambda e: rank(e["domain"]))
        main = [e for e in entries if not named_all or e["domain"] in named_all]
        other = [e for e in entries if named_all and e["domain"] not in named_all]
        if text_search and entries:
            # review of 0.8.16 (user decision): a query without a code is not answered by a shared word — 'ADS error on
            # port 851' was documented=true with five 'port' codes, 'E_FAIL' with the texts that say 'failed'. The codes
            # whose text holds its words are listed apart, as what the words could mean (the server's err_lookup looks
            # for the code in the recent errors and the XAE Error List, then online)
            out["text_matches"] = [{k: e.get(k) for k in ("domain", "value", "hex", "symbol", "text", "ref")
                                    if e.get(k) is not None} for e in (main + other)[:limit]]
            out["text_matches_note"] = (f"no code in the query: the documented codes whose text holds its words "
                                        f"({', '.join(out.get('searched_words', []))}) — not what the message is "
                                        "unless it is one of them")
            main, other = [], []
        out["documented"] = bool(main)
        out["matches"] = main[:limit]
        if len(main) > limit:
            out["more"] = f"{len(main) - limit} more match(es): limit=… (1-{LIMIT_MAX}) or domain=…"
        if main:
            # sixth review of 0.8.16: a list cut to `limit` read as complete ('documented in 3 domains' of 16) — every
            # domain of the answer, whatever the limit
            out["match_domains"] = list(dict.fromkeys(e["domain"] for e in main))
        if other:
            out["other_domains"] = [{k: e.get(k) for k in ("domain", "domain_title", "value", "hex", "symbol", "text",
                                                           "ref") if e.get(k) is not None} for e in other[:10]]
            if named:
                out["other_domains_note"] = (f"the message names {', '.join(named)}"
                                             + (f" (and its symbol is of {', '.join(sorted(sym_domains))})"
                                                if sym_domains - set(named) else "")
                                             + ": the same number or symbol in other domains is listed here, not as "
                                               "the answer")
            else:
                # re-review of 0.8.16: 'WSAETIMEDOUT' said 'the message names ads' — the symbol chose it
                out["other_domains_note"] = (f"the symbol is answered in {', '.join(named_all)} (of the domains that "
                                             "have it, the general code space): the same symbol elsewhere is listed "
                                             "here")
        out["ranking"] = _ranking(qn, named_all, entries, doms, text_search)
        searched = [d for d in sorted(doms, key=lambda d: (doms[d]["priority"], d)) if want is None or d in want]
        have = [d for d in searched if doms[d].get("codes") or doms[d].get("links")]
        # the code spaces by name, and how many code pages of libraries / functions (domain = book/page id)
        out["searched"] = [d for d in have if "/" not in d]
        pages = [d for d in have if "/" in d]
        if pages:
            out["searched"].append(f"{len(pages)} code page(s) of libraries / functions" if len(pages) > 3
                                   else ", ".join(pages))
        gaps = {d: doms[d]["note"] for d in searched if not (doms[d].get("codes") or doms[d].get("links"))
                and doms[d].get("note") and doms[d].get("origin") != "plugin"}
        miss = set(_jl(read_meta(db).get("missing_sources"), []))
        gaps.update({d: f"not indexed: {SOURCE_NAMES[doms[d]['origin']]} was missing when the errors index was built"
                     for d in searched if doms[d].get("origin") in miss and not doms[d].get("codes")})
        if named_all:          # the message names its domains: their gaps, not every domain's ('ADS 851': none)
            gaps = {d: n for d, n in gaps.items() if d in named_all}
        if gaps and (not main or named_all):
            out["no_local_source"] = gaps
        if not main:
            given = [c["token"] for c in read["codes"]] + read["names"] + [i["token"] for i in read["ids"]]
            code_given = bool(read["codes"] or read["ids"]) or any(e["matched_by"] in ("symbol", "dotnet name")
                                                                   for e in entries)
            if code_given or read["names"] and not text_search:
                what = ", ".join(given) or q
                out["note"] = (f"{what}: not in the local Beckhoff documentation"
                               + (f" of {', '.join(named_all)}" if named_all else "")
                               + " — no meaning is given (the full-text hits, if any, only mention it)")
            elif out.get("text_matches") and read["names"]:
                out["note"] = (f"{', '.join(read['names'])}: no documented code has this name — the codes whose text "
                               "holds the query's words are in text_matches, not the answer")
            elif out.get("text_matches"):
                out["note"] = ("no code or code name in the query: nothing is documented for it — the codes whose "
                               "text holds its words are in text_matches. A number counts as a code when it is the "
                               "whole query or stands next to a word that says so (error 1861, nErrId := 1808, ADS "
                               "error 0x745), a name when it is a code's symbol")
            elif out.get("searched_words"):
                out["note"] = (f"no code or code name in the query, and no documented text has all of its words "
                               f"({', '.join(out['searched_words'])})")
            else:
                out["note"] = ("no code in the query: a number counts as a code when it is the whole query or stands "
                               "next to a word that says so (error 1861, nErrId := 1808, ADS error 0x745), a name when "
                               "it is a code's symbol")
            ft = None if not fulltext else _fulltext(
                kbp, infosys_db or (recorded_infosys(db, kbp) if kbp is not None else None), " ".join(given) or q)
            if ft is not None:
                out["fulltext"] = ft
        if pn and not any(e.get("plugin_note") for e in main):
            out["plugin_notes"] = [note_out(pn[0], pn[1], _verified(con, pn[0], None))]
        out["source"] = answer_source(db, st)
    return out


def _uniq(notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for n in notes:
        k = (n.get("domain"), n.get("cause"))
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


def _ranking(qn: str, named: list[str], entries: list[dict[str, Any]], doms: dict[str, dict[str, Any]],
             text: bool) -> str:
    ds = list(dict.fromkeys(e["domain"] for e in entries))
    if not ds:
        return "nothing found to rank"
    if text:
        why = "found by the words of the query in the documented texts"
    elif named:
        words = sorted({n for d in named for n in doms.get(d, {}).get("names", []) if f" {n} " in qn})
        why = (f"the message names {', '.join(named)}" + (f" ({', '.join(repr(w) for w in words)})" if words else
                                                          " (by the code's symbol)"))
    else:
        hits = {d: _hint_hits(qn, doms.get(d, {"hints": []})) for d in ds}
        if any(hits.values()):
            why = "ranked by the words of the query: " + "; ".join(f"{d} ({', '.join(h)})" for d, h in hits.items() if h)
        else:
            why = "no word of the query names a domain: the general code tables first (ads, win32, hresult, nc …)"
    return why + (f" — {len(ds)} domains: {', '.join(ds[:8])}" if len(ds) > 1 else "")


# --------------------------------------------------------------------------------------------- for the other tools
def describe_code(kb: Path | str | None, domain: str, value: int) -> dict[str, Any] | None:
    """The main documented row of one code (symbol, text, ref) — for the ads_* error texts; None without an index or a
    row."""
    if kb is None:
        return None
    db = L.index_file(Path(kb), "errors")
    if not db.is_file():
        return None
    try:
        # an annotation of an ADS error, in the ADS worker: it does not wait for another program's lock
        with ErrIndex(db, busy_s=0.5).con() as con:
            r = con.execute("SELECT c.symbol, c.text, COALESCE(c.source_ref, x.ref) AS ref, c.hex FROM code c JOIN "
                            "variant x ON x.domain = c.domain "
                            f"AND x.variant = c.variant WHERE c.domain = ? AND c.value_int = ? ORDER BY {MAIN_ORDER} "
                            "LIMIT 1", (domain, value)).fetchone()
    except (sqlite3.Error, ErrError):
        return None
    return dict(r) if r else None


def code_summary(kb: Path | str | None, value: int, infosys_db: str | Path | None = None) -> list[dict[str, Any]] | None:
    """infosys_search / docs_search: where a code of the query is documented — one entry per domain (symbol, main
    text, ref, its table variants and books), from the errors index (N99b: the defining table first, copies merged).
    None without an index."""
    if kb is None:
        return None
    db = L.index_file(Path(kb), "errors")
    if not db.is_file():
        return None
    out = []
    try:
        with ErrIndex(db).con() as con:
            doms = _domains(con)
            # review of 0.8.16: the forms err_lookup reads too (0x98110745 is ads 0x745, 0xFFFFE504 is LabVIEW -6908)
            seen: dict[tuple[str, int], tuple[list[sqlite3.Row], str]] = {}
            for did, v, rs, how in _by_value(con, value)[0]:
                seen.setdefault((did, v), (rs, how))
            for did, v in sorted(seen, key=lambda k: (doms.get(k[0], {}).get("priority", 10), k[0])):
                e = _row_out(con, did, v, seen[(did, v)][0], doms, seen[(did, v)][1], "")
                out.append({k: e[k] for k in ("domain", "domain_title", "value", "hex", "symbol", "text", "ref", "books",
                                              "texts", "missing_in", "matched_by") if k in e})
    except (sqlite3.Error, ErrError):
        return None
    return out
