"""Local knowledge base (decisions of 2026-10-17: option (a) + user-provided locations only).

What it holds, and where each part comes from:

* known errors (K6)      — a seeded table in this file: error text → known cause → fix. Attached to tool errors.
                           Nothing is learned automatically.
* library versions (K3)  — the .plcproj of the project (what each placeholder resolves to) against the library
                           versions installed on this PC (folder names in TwinCAT's managed-library repository).
                           Encrypted .compiled-library files are never opened.
* hardware (K4)          — EtherCAT ESI device descriptions from TwinCAT's own install folders (PDOs, CoE objects).
* code patterns (K9)     — POUs/DUTs/GVLs of project folders the user adds explicitly (kb_add_project). Nothing is
                           indexed by default.

The database lives in a folder the user chooses (kb_set_location). Conditions: local fixed drive (SQLite corrupts
on network shares), not inside a cloud-synced folder (OneDrive/Dropbox/Google Drive lock and corrupt it), not inside
the Claude plugins folder (cache/data are replaced or owned by the plugin manager), writable, ≥ 1 GB free. If the
user names none, the agent asks; the proposed fallback is D:\\TwinCAT-MCP\\knowledge (used only when the user agrees
via kb_set_location(use_fallback=true)).

The folder's layout and rules are permanent (docs/KNOWLEDGE_BASE.md; kb_layout names every path):
  index\\knowledge.db           this module (projects, ESI)
  index\\infosys-<lcid>.db      the one Infosys index (infosys_* tools and the twincat-docs skill), infosys_build
  index\\manuals.db             the PDFs in manuals\\, tools\\manuals_db.py
  manuals\\, sop\\               the user's (read, never written)
  tools\\                        builder stubs
kb_status reports all of it; hw_lookup adds the manuals that mention a product code (read-only). A rebuild of an
index that exists runs only when the user approves it (kb_rebuild) or runs the builder; the first build of one that
does not exist yet starts by itself.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import docsearch as DS
from . import esi_index as E
from . import kb_layout as L
from . import kb_lock

FALLBACK_LOCATION = r"D:\TwinCAT-MCP\knowledge"
MIN_FREE_BYTES = 1 << 30
POINTER_NAME = "knowledge_location.json"
_SKILL_REL = Path("skills") / "twincat-docs" / "kb_docs.py"


def docs_skill_path() -> Path | None:
    """kb_docs.py of the twincat-docs skill. The plugin server runs from its venv's site-packages, so the plugin folder
    comes from the launcher (TCMCP_PLUGIN_SRC) or Claude Code (CLAUDE_PLUGIN_ROOT); a source checkout has it beside
    the package."""
    roots = [os.environ.get("TCMCP_PLUGIN_SRC"), os.environ.get("CLAUDE_PLUGIN_ROOT"), str(Path(__file__).resolve().parents[1])]
    for r in roots:
        if r and (Path(r) / _SKILL_REL).is_file():
            return Path(r) / _SKILL_REL
    return None
# project unit kinds kb_search can filter on (aliases: DUT = every data type, FB = FUNCTION_BLOCK)
UNIT_KINDS = ("PROGRAM", "FUNCTION_BLOCK", "FUNCTION", "METHOD", "PROPERTY", "GET", "SET", "ACTION", "TRANSITION",
              "INTERFACE", "STRUCT", "UNION", "ENUM", "ALIAS", "GVL", "POU", "DUT")
# N134: 'POU' — the IEC program organisation units; the kind POU itself is only a POU file whose header did not parse
KIND_ALIASES = {"FB": ("FUNCTION_BLOCK",), "DUT": ("DUT", "STRUCT", "UNION", "ENUM", "ALIAS"), "ITF": ("INTERFACE",),
                "POU": ("PROGRAM", "FUNCTION_BLOCK", "FUNCTION", "POU")}
# N135: what a search engine's query language would read as operators — kb_search has none
_OPERATOR_WORDS = frozenset({"OR", "AND", "NOT", "NEAR"})
_OPERATOR_SYNTAX = re.compile(r"(?:^|\s)[-+^](?=\w)|(?<=\w)\*|\bNEAR\s*\(|(?<![\w])(?:OR|AND|NOT)(?![\w])")

LOCATION_CONDITIONS = [
    "a folder on a local fixed drive of this PC (not a network share, mapped drive, NAS or USB stick — SQLite "
    "corrupts databases on network file systems, and a removable drive takes the database with it), not the drive's "
    "root itself",
    "not inside a cloud-synced folder (OneDrive, Dropbox, Google Drive, iCloud, Box) — the sync client locks and "
    "corrupts open database files",
    "not inside the Claude plugins folder (~/.claude/plugins/…: cache, data, marketplaces) or the server's install "
    "folder — they are replaced or owned by the plugin manager",
    "writable by your Windows user",
    "at least 1 GB free",
]

# --------------------------------------------------------------------------------------------- K6 known errors
# (pattern, cause, fix) — first match wins. Seeded from the field reports; extend here, never from tool output.
_ELEMENTARY = frozenset("BOOL BYTE WORD DWORD LWORD SINT USINT INT UINT DINT UDINT LINT ULINT REAL LREAL TIME LTIME "
                        "DATE TOD TIME_OF_DAY DT DATE_AND_TIME STRING WSTRING".split())


def _convert_fix(a: str, b: str) -> str:
    """C0032: the conversion that exists — {A}_TO_{B} only between elementary types; INT → BOOL is a comparison."""
    ua, ub = a.upper(), b.upper()
    if ua in _ELEMENTARY and ub in _ELEMENTARY:
        if ub == "BOOL":
            return f"compare instead: (value <> 0) turns the {ua} into a BOOL (or {ua}_TO_BOOL(…) when that is meant)"
        return (f"convert explicitly: {ua}_TO_{ub}(…)" + (" — TRUE → 1, FALSE → 0" if ua == "BOOL" else "")
                + ", or declare both sides with the same type")
    return (f"{a} and {b} are not assignment-compatible: assign member by member / element by element, use a variable "
            f"of type {b}, or (pointer / reference) the referenced type")


KNOWN_ERRORS: list[tuple[re.Pattern[str], str, Any]] = [(re.compile(p, re.I), c, f) for p, c, f in [
    # N70: the TwinCAT compiler's messages, by their text (the C-numbers differ between builds; the text does not).
    # First: their quoted names may contain anything the broader patterns below look for ('bLicenseOk')
    (r"identifier '([^']+)' (?:is )?not defined|identifier (?!(?:is|was|not)\b)([A-Za-z_][\w.]*) (?:is )?not defined",
     "'{1}' is not declared where it is used: a typo, a variable declared in another POU or GVL without the "
     "qualifier, a library that is not referenced, or a namespace / qualified_only GVL ('GVL.{1}')",
     "proj_search {1} (this project) / kb_search {1} (added folders) show where it is declared; declare it, qualify "
     "it, or add the library (proj_libraries)"),
    (r"cannot convert type '([^']+)' to type '([^']+)'",
     "IEC 61131-3 has no implicit conversion from {1} to {2} here",
     _convert_fix),
    (r"unknown type:? '([^']+)'|unknown type: ([A-Za-z_][\w.]*)",
     "the type '{1}' is not known to the compiler: a typo, a DUT not in the project, or a library not referenced",
     "kb_search {1} / infosys_declaration {1} shows where it comes from; add the DUT or the library"),
    (r"0x98110724|2551252772|\blicen[cs]es?\b",
     "TwinCAT license missing or expired on the target (TwinCAT falls back to CONFIG)",
     "XAE → SYSTEM → License → '7 Days Trial License…', activate the configuration again, restart TwinCAT"),
    (r"0x80010001|-2147418111|0x8001010a|-2147417846|call was rejected|retry later|blocked by dialog",
     "the XAE shell is busy or a modal dialog has it",
     "xae_dialogs shows the dialog; answer it with xae_dialog_resolve(button=…), then repeat the call"),
    (r"0x800706ba|-2147023174|rpc server is unavailable|disconnected \(closed",
     "the XAE shell was closed/reopened or crashed; the COM proxy is dead",
     "the next xae_* call re-attaches by itself; if not, xae_attach"),
    (r"(?<!not )(?<!n't )logged in(?:to)?\b",
     "XAE is logged in to the PLC; the project cannot be changed online",
     "xae_plc('logout'), make the change, build, log in again"),
]]
# The ADS return codes the plugin has a note for. Symbol and Beckhoff's text (what pyads / the ADS router put in a
# message) as the local Infosys "ADS Return Codes" table has them (tcplclib_tc2_system/374277003, checked 2026-10-20);
# cause and fix are the plugin's own. A text counts only in a message about ADS (N130: 'not permitted' of any program
# was ADS 1796); the code in any notation: ADSError 1796, ADS error 1796, 0x704 next to ADS, 0x98110704, the symbol.
ADS_NOTES: dict[int, tuple[str, str, str, str]] = {
    6: ("ERR_TARGETPORTNOTFOUND", r"target port not found",
        "nothing listens on that AMS port: TwinCAT is in CONFIG (PLC runtimes exist only in RUN), no PLC project is "
        "activated on it, or the port number is wrong (851 = first PLC runtime, 852 = second …)",
        "ads_status port=10000 shows RUN/CONFIG; ads_system_mode run, or activate the PLC project"),
    7: ("ERR_TARGETMACHINENOTFOUND", r"target (?:machine|computer) not found",
        "no route to that AMS NetId",
        "ads_add_route_to_plc / XAE → Choose Target → Search; check ams_net_id in the config"),
    0x701: ("ADSERR_DEVICE_SRVNOTSUPP", r"service is not supported",
            "the target/port does not offer this ADS service (e.g. sum commands, file access on this OS)",
            "use the non-sum variant or another port; for files check that the target OS supports the System Service "
            "file API"),
    0x704: ("ADSERR_DEVICE_INVALIDACCESS", r"not permitted",
            "reading/writing this index group/offset is not permitted (read-only area or protected symbol)",
            "write a writable symbol instead; check READONLY flags with ads_symbol_info"),
    0x705: ("ADSERR_DEVICE_INVALIDSIZE", r"parameter size not correct",
            "the value does not fit the variable (string too long, wrong array length or type size)",
            "check the declared type (ads_symbol_info); write elements by index for arrays"),
    0x707: ("ADSERR_DEVICE_NOTREADY", r"device (?:is )?not ready",
            "the runtime is not running (TwinCAT in CONFIG, or the PLC stopped)",
            "ads_status; switch to RUN (ads_system_mode run) / start the PLC (ads_plc_state run)"),
    0x710: ("ADSERR_DEVICE_SYMBOLNOTFOUND", r"symbol not found",
            "the symbol is not in the runtime: typo, not downloaded yet, or optimised away (unused, not referenced)",
            "ads_list_symbols pattern=…; after a code change: build + download / online change"),
    0x745: ("ADSERR_CLIENT_SYNCTIMEOUT", r"timeout (?:elapsed|has occurred)",
            "the target did not answer in time: missing/broken route, firewall (TCP 48898), target busy or stopped",
            "ads_ping; ads_routes; check the route on both sides and the Windows firewall"),
}
# an RPC method call that fails with 0x705: the parameter block, not a variable's size
_RPC_SIZE = ("the RPC parameter block does not match the method's signature: an input missing or extra, a wrong base "
             "type, or outputs / return_type that differ from the declaration",
             "pass every VAR_INPUT in declaration order with its exact base type (inputs=[{type, value}, …]), every "
             "VAR_OUTPUT in outputs and the method's return type in return_type; check the method in proj_interface")
_ADS_SYMBOLS = {sym: code for code, (sym, *_r) in ADS_NOTES.items()}
KNOWN_ERRORS_COVERED = ("ADS return codes 6, 7, 1793, 1796, 1797, 1799, 1808, 1861 in any notation (ADSError 1861, "
                        "ADS error 1861, ADS-Error: 0x745, 0x98110745, ADSERR_CLIENT_SYNCTIMEOUT; a bare hex number "
                        "only in a message about ADS); license; XAE busy / modal dialog / disconnected; logged in; "
                        "compiler messages by their text: identifier '…' not defined, cannot convert type '…' to type "
                        "'…', unknown type '…' (a C-number alone is not documented locally)")
# N71: the words of a sentence around the names: the one list of every search (docsearch)
STOP_WORDS = DS.STOP_WORDS
COMMON_SHARE = 0.2          # some-words tier: a word in more than this share of the units (≥ 25 units) is not used
IEC_WORDS = frozenset("""
BOOL BYTE WORD DWORD LWORD SINT USINT INT UINT DINT UDINT LINT ULINT REAL LREAL TIME LTIME DATE TOD TIME_OF_DAY DT
DATE_AND_TIME STRING WSTRING POINTER REFERENCE ARRAY OF ANY TYPE END_TYPE STRUCT END_STRUCT VAR END_VAR VAR_INPUT
VAR_OUTPUT VAR_IN_OUT VAR_GLOBAL VAR_TEMP VAR_STAT CONSTANT IF THEN ELSE ELSIF END_IF CASE END_CASE FOR TO BY DO
END_FOR WHILE END_WHILE REPEAT UNTIL END_REPEAT RETURN EXIT TRUE FALSE AND OR XOR NOT MOD PROGRAM FUNCTION
FUNCTION_BLOCK METHOD PROPERTY INTERFACE EXTENDS IMPLEMENTS THIS SUPER
""".split())


_fold = DS.fold

# units_fts: what kb_search matches. name_words / idents / code (0.8.12, N102): the words of the unit's name and of
# the identifiers of its code ('FB_AlarmList' → alarm, list; 'aAlarmTexts' → alarm, texts) — the tokenizer keeps an
# identifier whole, so 'alarm text' never found FB_AlarmList —, and the code without its comments and strings (to tell
# a unit whose code holds the words from one that has them in a comment; not ranked: weight 0)
UNITS_FTS = ("CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(project UNINDEXED, name, kind UNINDEXED, "
             "file UNINDEXED, declaration, implementation, comments, name_words, idents, code, tokenize='unicode61')")
UNITS_FTS_COLS = "project, name, kind, file, declaration, implementation, comments, name_words, idents, code"
_COMMENT_OR_STRING = re.compile(r"\(\*.*?\*\)|//[^\n]*|'(?:\$.|[^'$])*'|\"(?:\$.|[^\"$])*\"", re.S)


def unit_words(name: str, declaration: str, implementation: str) -> tuple[str, str, str]:
    """(name_words, idents, code) of a unit for units_fts: the words of its name, the words of its code's identifiers
    made of several words (IEC keywords such as VAR_INPUT are none), its code without comments and strings."""
    code = _COMMENT_OR_STRING.sub(" ", (declaration or "") + "\n" + (implementation or ""))
    idents = re.sub(r"\b[A-Za-z_]\w*\b", lambda m: "" if m.group(0).upper() in IEC_WORDS else m.group(0), code)
    return " ".join(DS.ident_words(name)), " ".join(DS.ident_words(idents)), code


# a code word right before the number: 'error 1861', 'error code 7', 'nErrId := 1808', 'error number 1861'
_CODE_WORD = (r"(?:error|fehler|err|errid|nerrid|errorid|nerrorid|errorcode|nerrorcode)\b"
              r"(?:[\s_-]*(?:code|number|id|no|nr)\b\.?)?")      # 'error no. 1808' (the \b before the '.')
# a number as a message writes a code: 0x745, 16#745 (ST), 1861 — never the '16' of '16#745'
_CODE_NUM = r"(0x[0-9a-f]+|16#[0-9a-f_]+|\d+)(?![#\w])"
_ADS_CODE = re.compile(r"\bads[\s_-]*(?:error|fehler|err)\b[\s:#=-]*(?:code[\s:#=-]*)?" + _CODE_NUM +
                       r"|\b" + _CODE_WORD + r"[\s:#=-]*" + _CODE_NUM +
                       # pyads: 'ADSError: symbol not found (1808).', 'ADSError: invalid parameter value(s) (1798).'
                       r"|\badserror\b\s*:[^\n]{0,200}?\((\d+)\)", re.I)
_HEX = re.compile(r"\b0x([0-9a-f]{1,8})\b", re.I)
_ADS_WORD = re.compile(r"\bads(?:\b|error|err\b|fehler)", re.I)      # 'ADS', 'ADSError' — not 'ads_write_raw'
_ADS_HRESULT = re.compile(r"\b0x9811([0-9a-f]{4})\b", re.I)            # the ADS facility of an HRESULT
_SYMBOL = re.compile(r"\bADSERR_[A-Z0-9_]+\b|\bERR_[A-Z]+NOTFOUND\b")    # case as Beckhoff writes them
_NUMBER = r"(?:0x[0-9a-f]+|16#[0-9a-f_]+|\d+)(?![#\w])"
# N129 / N130: what makes a query an error message — a code in a notation that says so, a code word right next to a
# number, an HRESULT, an ADS symbol, a compiler message (its number or its text), or a 0x number that is the whole query.
# Not the word 'error' alone ('error handler' is a search in code), not a bare hex number anywhere ('nCycleCount
# 0x705'), not a decimal or 16# number alone ('851', '16#FF' are literals of the code), not an identifier that begins
# with err_ / ERR_ (review: 'ERR_TIMEOUT' of an enum, 'err_count'), not a Beckhoff model number ('C6015': compiler
# messages are C0xxx)
_ERROR_MESSAGE = re.compile(
    r"\bads[\s_-]*(?:error|err|fehler)\b[\s:#=-]*" + _NUMBER +
    r"|\b(?:" + _CODE_WORD + r"|code\b)[\s:#=-]*" + _NUMBER +
    r"|\badserror\b\s*:[^\n]{0,200}?\(\d+\)"
    r"|\b0x[89a-f][0-9a-f]{7}\b|\bC0\d{3}\b"
    r"|identifier\s+'[^']+'\s+(?:is\s+)?not\s+defined|identifier\s+(?!(?:is|was|not)\b)[A-Za-z_][\w.]*\s+(?:is\s+)?not\s+defined"
    r"|cannot convert type\s+'|unknown type:?\s*'|unknown type:\s*[A-Za-z_]"
    r"|^\s*0x[0-9a-f]+\s*$", re.I)


def message_names(q: str) -> list[str]:
    """N129: the names of the code an error message holds — quoted ('nFoo', 'MAIN.fbX'), and unquoted ones that can
    be no word of the message's sentence: a lower-case prefix before an upper-case letter (fbCounter, nCycleCount),
    '_' between letters or digits (FB_Valve, MAX_ALARMS), a dotted path (MAIN.fbCounter), letters with digits (Plc2).
    Not: IEC keywords and elementary types, ADS symbols (ADSERR_…, the code itself), compiler numbers, hex numbers."""
    out = [t for t in re.findall(r"'([A-Za-z_][\w.]*)'", q) if t.upper() not in IEC_WORDS]
    rest = re.sub(r"'[^']*'", " ", q)
    for t in re.findall(r"(?<![\w#.])[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", rest):
        if t.upper() in IEC_WORDS or _SYMBOL.fullmatch(t) or re.fullmatch(r"C0\d{3}|0x[0-9a-f]+", t, re.I):
            continue
        if ("." in t or re.match(r"[a-z]+[A-Z0-9]", t) or re.search(r"[A-Za-z0-9]_[A-Za-z0-9]", t)
                or (re.search(r"\d", t) and re.search(r"[A-Za-z]{2}", t))):
            out.append(t)
    return list(dict.fromkeys(out))


def is_error_message(q: str) -> bool:
    """kb_search: is the query an error message (its cause / fix in known_error, only its names searched in the code)
    rather than words of the code?"""
    return bool(_ERROR_MESSAGE.search(q or "") or _SYMBOL.search(q or "")) or _hresult_dec(q or "") is not None


def _hresult_dec(msg: str) -> int | None:
    """An ADS HRESULT written in decimal (2551252772 = 0x98110724) — its ADS code."""
    for m in re.finditer(r"\b(25512\d{5})\b", msg):
        v = int(m.group(1))
        if v >> 16 == 0x9811:
            return v & 0xFFFF
    return None


def ads6_context(port: int | None, mode: str | None) -> dict[str, str]:
    """N17: ADSError 6 (target port not found) explained from the actual port and TwinCAT mode."""
    p = port or 851
    if mode == "CONFIG" and 850 <= p <= 899:
        return {"cause": f"TwinCAT is in CONFIG mode — the PLC runtime on port {p} exists only in RUN",
                "fix": "ads_system_mode run (or activate the configuration and restart in RUN)"}
    if mode == "RUN" and 851 <= p <= 899:
        return {"cause": f"TwinCAT is in RUN but no PLC runtime listens on port {p}: no PLC project is activated on it"
                         + (" (851 is the first PLC runtime)" if p != 851 else "") + ", or there is no boot project",
                "fix": "activate the PLC project (xae_activate_configuration), or use the port it runs on"}
    if mode is None:
        return {"cause": f"nothing listens on AMS port {p} and the system service (port 10000) did not answer either — "
                         "is TwinCAT running on the target, and is the route right?",
                "fix": "ads_ping; ads_routes; tc_doctor"}
    return {"cause": f"nothing listens on AMS port {p} (TwinCAT is in {mode})", "fix": "check the port number"}


def _ads_codes(msg: str) -> list[int]:
    """The ADS return codes a message names, the most explicit first: 'ADSError 1861' (pyads), 'ADS error 1861',
    'ADS-Error: 0x745', pyads' 'ADSError: symbol not found (1808)', 'error 1861' / 'error code 1861' / 'nErrId :=
    1861' (about ADS, or an ADS device error 0x700–0x7FF), an ADS HRESULT (0x98110745, or in decimal), an ADS symbol
    (ADSERR_CLIENT_SYNCTIMEOUT), a 0x number that is the whole message; then
    — only in a message about ADS — Beckhoff's text of the code ('symbol not found') and bare hex numbers.
    N129: a bare hex number of 0x700–0x7FF in any text was an ADS code ('nCycleCount 0x705' → 1797)."""
    ads = _ADS_WORD.search(msg) is not None or _ADS_HRESULT.search(msg) is not None or \
        any(t in _ADS_SYMBOLS for t in _SYMBOL.findall(msg))
    codes: list[int] = []
    for m in _ADS_CODE.finditer(msg):
        v = (m.group(1) or m.group(2) or m.group(3)).lower().replace("_", "")
        n = int(v[2:], 16) if v.startswith("0x") else int(v[3:], 16) if v.startswith("16#") else int(v)
        if m.group(2) is not None and not ads and not 0x700 <= n <= 0x7FF:
            continue                                  # 'exited with error code 7' is not an ADS error; 1861 is
        codes.append(n)
    codes += [int(m.group(1), 16) for m in _ADS_HRESULT.finditer(msg)]
    dec = _hresult_dec(msg)
    if dec is not None:
        codes.append(dec)
    codes += [_ADS_SYMBOLS[t] for t in _SYMBOL.findall(msg) if t in _ADS_SYMBOLS]
    whole = re.fullmatch(r"\s*0x([0-9a-f]+)\s*", msg, re.I)          # 0x… alone (no ST literal is written so)
    if whole:
        codes.append(int(whole.group(1), 16))
    if ads:
        low = msg.lower()
        codes += [c for c, (_sym, text, *_r) in ADS_NOTES.items() if re.search(text, low)]
        codes += [int(m.group(1), 16) for m in _HEX.finditer(msg)]
    return list(dict.fromkeys(codes))


def _fill(t: str, groups: list[str]) -> str:
    """{1}, {2}: the pattern's groups (other braces are text: {type, value})."""
    return re.sub(r"\{(\d)\}", lambda g: groups[int(g.group(1))] if int(g.group(1)) < len(groups) else g.group(0), t)


def _match_known(msg: str) -> tuple[str, str, str, str | None] | None:
    """(cause, fix, fix template, the ADS code it is) of the first note that fits: the general table (compiler messages by their text,
    license, XAE), then the ADS return codes the message names (_ads_codes, most explicit first: 'ADSError 1796 at
    offset 0x7' is 1796, not 7)."""
    for pat, cause, fix in KNOWN_ERRORS:
        m = pat.search(msg)
        if not m:
            continue
        got = [g for g in m.groups() if g]
        groups = [""] + got + [""] * (pat.groups - len(got))      # alternatives: their filled groups, in order
        if callable(fix):
            return _fill(cause, groups), fix(*groups[1:]), "", None
        return _fill(cause, groups), _fill(fix, groups), fix, None
    for code in _ads_codes(msg):
        if code not in ADS_NOTES:
            continue
        sym, _text, cause, fix = ADS_NOTES[code]
        if code == 0x705 and re.search(r"\brpc call\b", msg, re.I):
            cause, fix = _RPC_SIZE
        return cause, fix, fix, f"ADS return code 0x{code:X} ({code}) {sym}"
    return None


def known_error(msg: str) -> dict[str, str] | None:
    """The plugin's note on an error message: cause and fix (its own words, not Beckhoff's), and for an ADS return code
    which one it is (code, symbol)."""
    k = _match_known(msg or "")
    if k is None:
        return None
    out = {"cause": k[0], "fix": k[1]}
    if k[3]:
        out["code"] = k[3]
    return out


def annotate(msg: str) -> str:
    """Append the known cause/fix to an error message unless the message already carries that fix."""
    m = _match_known(msg or "")
    if not m:
        return msg
    k = {"cause": m[0], "fix": m[1]}
    low = msg.lower()
    # tool names the fix points at — from the template: a filled-in name from the message ('my_var') is not a tool
    tools = re.findall(r"\b[a-z]+_[a-z_]+\b", m[2])
    if any(t in low for t in tools) or k["fix"][:30].lower() in low:
        return msg                                                # the message already says what to do
    return f"{msg} [known cause: {k['cause']}. fix: {k['fix']}]"


class KnowledgeError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------- location checks
_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "icloud drive", "iclouddrive", "box sync", "box drive")


def strip_long_prefix(path: str) -> str:
    """N136: a Windows extended-length / device path names the same folder without its prefix: '\\\\?\\C:\\x' is
    'C:\\x', '\\\\?\\UNC\\srv\\share' is the UNC path '\\\\srv\\share' ('\\\\?\\C:\\…' was reported as a network path)."""
    s = str(path)
    if s[:4] in ("\\\\?\\", "\\\\.\\", "//?/", "//./"):
        rest = s[4:]
        return "\\\\" + rest[4:] if rest[:4].upper() in ("UNC\\", "UNC/") else rest
    return s


def plugin_dirs(extra: list[Path] | None = None) -> list[Path]:
    """The plugin's folders: the whole ~/.claude/plugins tree (R33), this installation, the plugin root/data folders
    Claude Code names — replaced on every update, and holding the plugin's own test projects (N128)."""
    out = list(extra or []) + [Path.home() / ".claude" / "plugins", Path(__file__).resolve().parents[1]]
    out += [Path(os.environ[e]) for e in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA") if os.environ.get(e)]
    return out


def is_within(child: str | Path, parent: str | Path) -> bool:
    """child is parent or below it — by path components in the file system's spelling (norm_path), never by a
    string prefix ('C:\\OneDriveOld' is not inside 'C:\\OneDrive')."""
    c, p = norm_path(child), norm_path(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _sync_folder(part: str) -> bool:
    """A path component that is a cloud client's folder: 'OneDrive', 'OneDrive - Contoso', 'Dropbox (Personal)',
    'Google Drive' … — the whole name or the name followed by its suffix; N136: 'Toolbox Drive' is none."""
    low = part.lower()
    return any(low == m or low.startswith((m + " ", m + "(", m + "-")) for m in _SYNC_MARKERS)


def check_location(path: str | Path, plugin_dirs_: list[Path] | None = None, probe: bool = True,
                   space: bool = True) -> list[str]:
    """Every condition the folder fails, as plain sentences (empty list = usable). probe=True: the folder is being
    chosen — the write test and the drive-root rule; probe=False (R34: read-only calls must not write; parallel calls
    raced on one probe file) checks only what makes a folder in use unusable. space=False skips the free-space test."""
    problems: list[str] = []
    p = Path(strip_long_prefix(str(path))).expanduser()
    if not p.is_absolute():
        return [f"{path!r} is not an absolute path"]
    s = str(p)
    if s.startswith("\\\\") or s.startswith("//"):
        problems.append("it is a network (UNC) path")
    elif os.name == "nt":
        try:
            import ctypes
            drive = os.path.splitdrive(s)[0] + "\\"
            kind = ctypes.windll.kernel32.GetDriveTypeW(drive)  # type: ignore[attr-defined]
            if kind == 4:
                problems.append(f"{drive} is a network/mapped drive")
            elif kind == 2:
                problems.append(f"{drive} is a removable drive")
            elif kind in (0, 1):
                problems.append(f"{drive} does not exist")
        except Exception:  # noqa: BLE001
            pass
    else:
        fs = _posix_fstype(p)
        if fs and any(x in fs for x in ("nfs", "cifs", "smb", "sshfs", "fuse.s3", "9p")):
            problems.append(f"it is on a network file system ({fs})")
    if probe and p.parent == p:            # choosing only — a folder in use keeps working (R34: reads check less)
        problems.append(f"{p} is a drive root: the knowledge folder gets index\\, manuals\\, sop\\ and tools\\ of its "
                        f"own — give a folder on it (e.g. {Path(p, 'TwinCAT-MCP', 'knowledge')})")
    if any(_sync_folder(part) for part in p.parts):
        problems.append("it is inside a cloud-synced folder")
    for env in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        od = os.environ.get(env)
        if od and is_within(p, od):
            problems.append(f"it is inside the OneDrive folder ({od})")
    # R33: the whole plugins tree — the data folder (~/.claude/plugins/data/…) was accepted while only cache/ was blocked
    for d in plugin_dirs(plugin_dirs_):
        try:
            if p.resolve() == d.resolve() or d.resolve() in p.resolve().parents:
                problems.append(f"it is inside the plugin/install folder {d} (replaced on every update)")
                break
        except OSError:
            continue
    # N136: a file where the folder should be — said as such, not as 'not writable ([WinError 183])'
    for a in (p, *p.parents):
        if a.exists():
            if not a.is_dir():
                problems.append("it is a file, not a folder" if a == p else f"{a} is a file, not a folder")
            break
    if not problems and probe:
        try:
            p.mkdir(parents=True, exist_ok=True)
            # R34 / N20: a name of its own per call — two calls (or two servers) never share the probe file
            f = p / f".tcmcp_write_test_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
            f.write_text("x", encoding="ascii")
            f.unlink()
        except OSError as exc:
            problems.append(f"it is not writable ({exc})")
    elif not problems and not p.is_dir():
        problems.append("the folder does not exist (moved or deleted?)")
    if not problems and space:
        try:
            free = shutil.disk_usage(p).free
            if free < MIN_FREE_BYTES:
                problems.append(f"only {free / (1 << 30):.2f} GB free (needs ≥ 1 GB)")
        except OSError:
            pass
    return problems


def norm_path(path: str | Path) -> str:
    """R35: one spelling per folder — resolved, no trailing separator, and case-folded where the file system is
    case-insensitive (Windows), so 'C:\\P15\\' and 'c:\\p15' are the same project."""
    p = Path(strip_long_prefix(str(path))).expanduser()          # review: '\\\\?\\C:\\P15' is 'C:\\P15' too
    try:
        p = p.resolve()
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(strip_long_prefix(str(p))))


def display_path(path: str | Path) -> str:
    p = Path(strip_long_prefix(str(path))).expanduser()
    try:
        p = p.resolve()            # Windows: the on-disk spelling of an existing folder
    except OSError:
        pass
    return os.path.normpath(strip_long_prefix(str(p)))


def _posix_fstype(p: Path) -> str | None:
    try:
        best, fstype = "", None
        with open("/proc/mounts", encoding="utf-8") as fh:
            for ln in fh:
                parts = ln.split()
                if len(parts) >= 3 and str(p).startswith(parts[1]) and len(parts[1]) > len(best):
                    best, fstype = parts[1], parts[2]
        return fstype
    except OSError:
        return None


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


# --------------------------------------------------------------------------------------------- store
class Knowledge:
    def __init__(self, home: Path, location: str | None = None, esi_dirs: list[str] | None = None,
                 library_dirs: list[str] | None = None):
        self.home = home
        self.configured = location
        self.esi_dirs = esi_dirs or []
        self.docs_local: Any = None            # the Help Viewer package the server reads (set by the server)
        self.last_layout: dict[str, Any] | None = None
        self.library_dirs = library_dirs or []
        self._fts: bool | None = None
        self._fts_words = True              # units_fts has name_words / idents (set when it is opened)
        self.last_upkeep: dict[str, Any] | None = None

    # -- location -------------------------------------------------------------------------
    def _pointer(self) -> Path:
        return self.home / POINTER_NAME

    def location(self) -> tuple[Path | None, str]:
        if self.configured:
            return Path(self.configured).expanduser(), "config (knowledge.location)"
        try:
            d = json.loads(self._pointer().read_text(encoding="utf-8"))
            if d.get("path"):
                return Path(d["path"]), f"chosen {d.get('chosen', '')}".strip()
        except (OSError, ValueError):
            pass
        return None, "not chosen yet"

    def require(self) -> Path:
        loc, _src = self.location()
        if loc is None:
            raise KnowledgeError(self.ask_text())
        # R34: no write test on the way in (a read must not write; parallel calls raced on one probe file) and no
        # free-space test (a full disk is not a reason to move the knowledge base) — only a folder that is really
        # gone or became a network/sync/plugin path is reported as unusable
        problems = self._usable(loc)
        if problems:
            raise KnowledgeError(f"the knowledge folder {loc} is no longer usable: {'; '.join(problems)}. "
                                 "Ask the user for another folder and call kb_set_location(path=…).")
        return loc

    def _usable(self, loc: Path) -> list[str]:
        """Problems that make the folder unusable for reading — no write test, no free-space test (R34). A folder
        configured in the TOML / environment that does not exist yet is created, as the old write probe did; a folder
        chosen earlier with kb_set_location that has gone is reported (moved or deleted — never silently re-created)."""
        problems = check_location(loc, probe=False, space=False)
        if (problems == ["the folder does not exist (moved or deleted?)"] and loc.is_absolute()
                and self.configured and norm_path(self.configured) == norm_path(loc)):
            try:
                loc.mkdir(parents=True, exist_ok=True)
                problems = check_location(loc, probe=False, space=False)
            except OSError as exc:
                problems = [f"it cannot be created ({exc})"]
        return problems

    def ask_text(self) -> str:
        fb = FALLBACK_LOCATION if (os.name == "nt" and Path("D:\\").exists()) else None
        return ("The knowledge base has no folder yet. ASK THE USER where to keep it (a folder path), then call "
                "kb_set_location(path=…). The folder must be: " + "; ".join(f"({i + 1}) {c}" for i, c in enumerate(LOCATION_CONDITIONS))
                + ". " + (f"If the user has no preference, propose {fb} and call kb_set_location(use_fallback=true) "
                          "once they agree." if fb else "This PC has no D: drive, so there is no fallback: a folder is required."))

    def set_location(self, path: str | None, use_fallback: bool = False) -> dict[str, Any]:
        if use_fallback and path and norm_path(strip_long_prefix(path)) != norm_path(FALLBACK_LOCATION):
            # N136: use_fallback replaced a given path without a word
            raise KnowledgeError(f"give either path=… or use_fallback=true, not both: {path} is not the fallback "
                                 f"{FALLBACK_LOCATION} — nothing was changed")
        if use_fallback:
            if not (os.name == "nt" and Path("D:\\").exists()):
                raise KnowledgeError("no D: drive on this PC — the fallback location does not exist; give a folder path")
            path = FALLBACK_LOCATION
        if not path:
            cur, src = self.location()
            if cur is None:
                raise KnowledgeError(self.ask_text())
            # R38: asked without a folder while one is set — that is a question, not a reset
            return {"location": str(cur), "database": str(L.locate(cur, "knowledge")), "source": src, "changed": False,
                    "note": "the location is unchanged — pass path=… (a folder the user named) to choose another one",
                    "docs": self.docs_layout(cur, brief=True)}
        if not Path(strip_long_prefix(path)).expanduser().is_absolute():
            raise KnowledgeError(f"{path!r} is not an absolute path — give the full folder path the user named")
        p = Path(display_path(strip_long_prefix(path)))
        problems = check_location(p)
        if problems:
            raise KnowledgeError(f"{p} cannot hold the knowledge base: {'; '.join(problems)}. Conditions: "
                                 + "; ".join(LOCATION_CONDITIONS))
        old, _ = self.location()
        if old is not None and L.index_dir(old).is_dir() and \
                kb_lock.held(L.index_dir(old) / "knowledge.db.lock"):
            # review: a change still running (a kb_call that timed out) would go on writing into the folder it
            # began in — the location is not switched under it
            info = kb_lock.read(L.index_dir(old) / "knowledge.db.lock")[0] or {}
            raise KnowledgeError(f"a change of the knowledge base in {old} is still running ({info.get('phase')}, pid "
                                 f"{info.get('pid')}) — wait until it is finished, then choose the new folder")
        self.home.mkdir(parents=True, exist_ok=True)
        self._pointer().write_text(json.dumps({"path": str(p), "chosen": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        self.layout(p)                               # the permanent layout in the new folder (docs/KNOWLEDGE_BASE.md)
        out: dict[str, Any] = {"location": str(p), "database": str(L.locate(p, "knowledge"))}
        if old is not None and old != p and L.locate(old, "knowledge").exists():
            out["note"] = (f"the previous database stays at {L.locate(old, 'knowledge')}; move it into the new folder "
                           "yourself if you want to keep its contents (nothing is copied or deleted automatically)")
        with self._db():
            pass
        out["changed"] = old is None or norm_path(old) != norm_path(p)
        from . import __version__
        stubs = sync_builder_stubs(self.home, p, __version__).get("tools") or {}
        if any(isinstance(v, dict) and v.get("state") in ("replaced", "left alone", "error") for v in stubs.values()):
            out["builder_stubs"] = stubs
        out["docs"] = self.docs_layout(p, brief=True)
        return out

    def builder_upkeep(self, version: str) -> dict[str, Any]:
        """At every server start: record where this plugin's builders are (for the stubs) and turn builder copies in
        <kb>\\tools into stubs. File-system only; never raises."""
        res: dict[str, Any] = {}
        try:
            res = sync_builder_stubs(self.home, self.location()[0], version)
        except Exception as exc:  # noqa: BLE001 — a convenience; the tools work without it
            res["error"] = f"{type(exc).__name__}: {exc}"
        self.last_upkeep = res
        self.last_upkeep_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        return res

    def layout(self, loc: Path | None = None) -> dict[str, Any]:
        """The permanent layout (docs/KNOWLEDGE_BASE.md): every index moved from its old place into index\\ — once,
        a rename, never a copy, never over a file — and index\\README.md. Run before anything opens an index (server
        start, kb_set_location). Never raises; nothing is deleted."""
        loc = loc or self.location()[0]
        out: dict[str, Any] = {}
        if loc is None or not Path(loc).is_dir():
            return out
        try:
            out["moves"] = L.migrate(Path(loc))
            out["readme_written"] = L.write_index_readme(Path(loc))
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{type(exc).__name__}: {exc}"
        self.last_layout = out
        return out

    # -- database -------------------------------------------------------------------------
    def _db_path(self) -> Path:
        return L.locate(self.require(), "knowledge")

    def _db(self, path: Path | None = None) -> "_Conn":
        """A writer's connection — to `path`, the database an operation took when it began (review: a write after a
        kb_call timeout looked the location up again and could land in a folder chosen meanwhile)."""
        p = path or self._db_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        return _Conn(p, self)

    def _db_if_any(self, path: Path | None = None) -> "_Conn | None":
        """For reads: the database if it exists — a read never creates index\\ or knowledge.db (agent finding of the
        0.8.13 retest: kb_status / kb_search made both). None: no project folder was ever added here."""
        p = path or self._db_path()
        return _Conn(p, self, write=False) if p.is_file() else None

    def _db_read(self) -> "_Conn":
        """For a read that lists or searches: the database, or — when there is none yet — an empty one in memory with
        the tables a read uses (nothing is created on disk, and nothing of it is taken for the real database's state:
        review — its search table set the shared flag and the first real add then had none)."""
        return self._db_if_any() or _Conn(Path(":memory:"), self, write=False, scratch=True)

    def _write_lock(self, what: str) -> "_WriteLock":
        """One change of knowledge.db at a time (kb_lock on index\\knowledge.db.lock, as the builds): a second
        kb_add_project / kb_reindex / kb_remove_project — another thread of this server after a kb_call timeout, or
        another server — is refused with what is running, instead of racing (two adds of one new folder ended in an
        IntegrityError)."""
        loc = self.require()
        return _WriteLock(L.index_dir(loc) / "knowledge.db.lock", what, L.locate(loc, "knowledge"))

    def _init(self, con: sqlite3.Connection, write: bool = True) -> None:
        """A writer creates what is missing (tables, the problems column of 0.8.14, the search table); a reader only
        looks what is there — it never changes the database (agent finding of the 0.8.13 retest). Either one takes the
        search table's state from the database it opened, every time — never from an earlier connection's guess."""
        if not write:
            self._fts_state(con)
            return
        con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS projects (path TEXT PRIMARY KEY, label TEXT, added TEXT, indexed TEXT, units INTEGER, files TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS units (project TEXT, name TEXT, kind TEXT, file TEXT, line INTEGER, "
                    "declaration TEXT, implementation TEXT, comments TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS units_project ON units(project)")
        cols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
        if "problems" not in cols:               # N132 (0.8.14): what could not be read, kept for kb_status
            con.execute("ALTER TABLE projects ADD COLUMN problems TEXT")
        try:
            con.execute(UNITS_FTS)
        except sqlite3.OperationalError:
            pass                                 # no FTS5 in this SQLite: kb_search uses LIKE
        self._fts_state(con)

    def _fts_state(self, con: sqlite3.Connection) -> None:
        row = con.execute("SELECT sql FROM sqlite_master WHERE name = 'units_fts'").fetchone()
        self._fts = row is not None
        self._fts_words = bool(row and " code," in (row[0] or ""))           # False: the search table of 0.8.11

    def status(self) -> dict[str, Any]:
        loc, src = self.location()
        out: dict[str, Any] = {"location": str(loc) if loc else None, "source": src}
        if loc is None:
            out["action"] = self.ask_text()
            return out
        problems = self._usable(loc)          # R34: no write test on a status call
        if problems:
            out["problems"] = problems
            return out
        low = [x for x in check_location(loc, probe=False, space=True) if "GB free" in x]
        if low:
            out["warning"] = low[0] + " — reads keep working; indexing may fail"
        db_file = L.locate(loc, "knowledge")
        if not db_file.is_file():
            out["database_state"] = "not created yet — kb_add_project creates it"
        with self._db_read() as con:
            out["database"] = str(db_file)
            out["projects"] = self._project_rows(con)
            out["fts5"] = bool(self._fts) if db_file.is_file() else None      # not known before the database exists
            old_esi = con.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('esi', 'esi_files')").fetchall()
        dup: dict[str, list[str]] = {}
        for pr in out["projects"]:
            dup.setdefault(norm_path(pr["path"]), []).append(pr["path"])
        twice = [v for v in dup.values() if len(v) > 1]
        if twice:
            out["duplicate_projects"] = (f"{twice} are the same folder (R35, added before 0.8.8 with another spelling) — "
                                         "kb_add_project on it merges them into one")
        out["known_errors"] = len(KNOWN_ERRORS)
        out["esi"] = self.esi_state()
        out["docs"] = self.docs_layout(loc)
        lay: dict[str, Any] = {"index": str(L.index_dir(loc)), "rules": "docs/KNOWLEDGE_BASE.md in the plugin; "
                               "index\\README.md in the folder"}
        moves = (getattr(self, "last_layout", None) or {}).get("moves", [])
        if [m for m in moves if m.get("state") == "moved"]:
            lay["moved_into_index"] = [m for m in moves if m.get("state") == "moved"]
        if [m for m in moves if m.get("state") == "in use"]:
            lay["not_moved_yet"] = [m for m in moves if m.get("state") == "in use"]
        if old_esi:
            lay["obsolete_tables"] = {"file": out["database"], "tables": sorted(r[0] for r in old_esi),
                                      "why": "the ESI devices of 0.8.11 and before — index\\esi.db replaced them (0.8.12); "
                                             "no longer read, never deleted by the plugin"}
        obsolete = L.obsolete_files(loc)
        if obsolete:
            lay["obsolete_files"] = obsolete
            lay["obsolete_note"] = ("files the plugin no longer reads — it never deletes them (rule 7): delete them "
                                    "yourself when you like (" + ", ".join(f"{o['size_mb']} MB" for o in obsolete) + ")")
        out["layout"] = lay
        return out

    # -- the offline documentation next to knowledge.db (read-only) ------------------------
    def docs_layout(self, loc: Path | None = None, brief: bool = False) -> dict[str, Any]:
        """What the twincat-docs skill finds in the knowledge folder: the two databases built by the builders (the
        plugin's, run directly or through the stubs in tools\\), the manuals not indexed yet, the SOPs."""
        loc = loc or self.location()[0]
        if loc is None:
            return {"note": "no knowledge folder chosen yet"}
        script = docs_skill_path()
        inf, man, builders = _docs_parts(loc, getattr(self, "docs_local", None))
        out: dict[str, Any] = {"skill": "twincat-docs",
                               "call": (f'python "{script}" status --kb "{loc}"' if script else
                                        f'python "<twincat-docs skill folder>/kb_docs.py" status --kb "{loc}"'),
                               "infosys": inf, "manuals": man}
        if not brief:
            out["builders"] = builders
            up = getattr(self, "last_upkeep", None)
            if up:
                # N64 polish: this is what the server START did; the file may have changed since (an edited stub is
                # 'other' in `builders`, while the start's entry still said 'stub') — said as such, actions only
                view: dict[str, Any] = {"at_server_start": getattr(self, "last_upkeep_at", None),
                                        **{k: v for k, v in up.items() if k != "tools"}}
                tools = up.get("tools")
                if isinstance(tools, dict):
                    acted = {n: e for n, e in tools.items() if not (isinstance(e, dict) and e.get("state") == "stub")}
                    view["tools"] = acted or "nothing to do: the stubs were in place"
                elif tools is not None:
                    view["tools"] = tools
                view["note"] = "what the server did at its start — `builders` above is the state of the files now"
                out["builder_upkeep"] = view
            sop = loc / "sop"
            out["sop"] = sorted(str(f.relative_to(loc)) for f in sop.rglob("*.md"))[:50] if sop.is_dir() else []
        return out

    def docs_status(self) -> dict[str, Any]:
        """The real state of the offline documentation, short: for tc_doctor and the instructions the server sends
        at every (re)connection."""
        loc = self.location()[0]
        if loc is None:
            return {"summary": "no knowledge folder chosen yet (kb_set_location) — no offline documentation", "ok": False}
        inf, man, _b = _docs_parts(loc, getattr(self, "docs_local", None))
        from . import infosys_build as IB
        ok = not IB.problem(inf)
        esi = self.esi_state()
        out: dict[str, Any] = {"knowledge_folder": str(loc), "infosys": inf.get("message"), "manuals": man.get("message"),
                               "esi": esi.get("message"), "infosys_state": inf.get("state"),
                               "manuals_state": man.get("state"), "esi_state": esi.get("state"), "ok": ok,
                               "infosys_rebuild": (inf.get("rebuild") or {}).get("state"),
                               "esi_rebuild": (esi.get("rebuild") or {}).get("state"),
                               "summary": f"Infosys index: {inf.get('message')}. Manuals: {man.get('message')}. "
                                          f"ESI index: {esi.get('message')}."}
        if inf.get("command"):
            out["infosys_command"] = inf["command"]
        if man.get("command"):
            out["manuals_command"] = man["command"]
        return out

    # -- K9 projects ---------------------------------------------------------------------
    @staticmethod
    def _match_projects(con: sqlite3.Connection, ref: str) -> list[dict[str, Any]]:
        """Projects meant by `ref`: the same folder in any spelling (case, separators, trailing backslash), else the
        label (case-insensitive)."""
        rows = [dict(r) for r in con.execute("SELECT path, label, files FROM projects ORDER BY rowid")]
        key = norm_path(ref)
        hit = [r for r in rows if norm_path(r["path"]) == key]
        if not hit:
            hit = [r for r in rows if (r["label"] or "").strip().lower() == ref.strip().lower()]
        return hit

    def _drop_project(self, con: sqlite3.Connection, stored: str) -> int:
        n = con.execute("DELETE FROM units WHERE project=?", (stored,)).rowcount
        if self._fts:
            con.execute("DELETE FROM units_fts WHERE project=?", (stored,))
        con.execute("DELETE FROM projects WHERE path=?", (stored,))
        return n

    @staticmethod
    def _project_rows(con: sqlite3.Connection) -> list[dict[str, Any]]:
        """The indexed folders for kb_status, each with its state against the files now (N133: kb_status never looked
        at the folders) and what of it could not be indexed (N132)."""
        cols = {r[1] for r in con.execute("PRAGMA table_info(projects)")}
        sel = "path, label, added, indexed, units, files" + (", problems" if "problems" in cols else "")
        out = []
        for r in con.execute(f"SELECT {sel} FROM projects ORDER BY path"):
            pr = dict(r)
            pr["state"] = folder_state(pr["path"], pr.pop("files"))
            probs = json.loads(pr.pop("problems", None) or "null")
            if probs:
                pr.update(_problem_counts(probs))
                pr["problems"] = probs
            out.append(pr)
        return out

    @staticmethod
    def _project_list(con: sqlite3.Connection) -> list[str]:
        return [f"{r['label']} ({r['path']})" for r in con.execute("SELECT path, label FROM projects ORDER BY path")]

    def _refuse_project_folder(self, p: Path) -> None:
        """N128: folders that are no project of the user's — the plugin's folders (their test projects were indexed
        and outranked the real MAIN), and the knowledge folder itself."""
        for d in plugin_dirs():
            if is_within(p, d):
                raise KnowledgeError(f"{p} is inside the plugin folder {d}: its files are the plugin's (test projects), "
                                     "not a project of yours — give the folder of your TwinCAT project")
            if is_within(d, p) and d.exists():
                raise KnowledgeError(f"{p} contains the plugin folder {d}: the plugin's test projects would be indexed "
                                     "with yours — give the folder of your TwinCAT project")
        loc = self.location()[0]
        if loc is not None and is_within(p, loc):
            raise KnowledgeError(f"{p} is {'the knowledge folder' if norm_path(p) == norm_path(loc) else 'inside the knowledge folder'} "
                                 f"({loc}): it holds the indexes and manuals, not a project — give the folder of your "
                                 "TwinCAT project")

    def _refuse_overlap(self, con: sqlite3.Connection, p: Path) -> None:
        """N128: a folder inside, or around, one already indexed — every unit of the inner one would be there twice."""
        for r in con.execute("SELECT path, label FROM projects ORDER BY path"):
            if norm_path(r["path"]) == norm_path(p):
                continue                                   # the same folder: re-indexed, not added twice (R35)
            if is_within(p, r["path"]):
                raise KnowledgeError(f"{p} is inside the indexed folder {r['path']} ({r['label']}): its units are "
                                     f"already in the knowledge base — kb_reindex {r['label']!r} updates them")
            if is_within(r["path"], p):
                raise KnowledgeError(f"{p} contains the indexed folder {r['path']} ({r['label']}): its units would be "
                                     f"there twice — kb_remove_project {r['path']!r} first, then add {p}")

    def add_project(self, path: str, label: str | None = None) -> dict[str, Any]:
        path = strip_long_prefix(path) if path else path          # one spelling of the folder (review: '\\\\?\\C:\\…')
        if not path or not Path(path).expanduser().is_absolute():
            raise KnowledgeError(f"{path!r} is not an existing absolute folder")
        p = Path(display_path(path))
        if not p.is_dir():
            raise KnowledgeError(f"{path!r} is not an existing absolute folder")
        self._refuse_project_folder(p)
        with self._write_lock(f"kb_add_project {p}") as wl:
            db = self._db_if_any(wl.db)
            same: list[dict[str, Any]] = []
            if db is not None:
                with db as con:
                    self._refuse_overlap(con, p)
                    same = [r for r in self._match_projects(con, str(p)) if norm_path(r["path"]) == norm_path(p)]
            keep = same[0]["path"] if same else str(p)
            # agent finding (0.8.13): the folder was registered before it was indexed — a failed scan left it in the
            # list with 0 units. Now it is registered in the transaction that stores its units, or not at all
            res = self._reindex_rows([{"path": keep, "files": None,
                                       "register": {"label": label or p.name, "relabel": label,
                                                    "merge": [d["path"] for d in same[1:]]}}], force=True, db=wl.db)
        r0 = res["reindexed"][0]
        if r0.get("error"):
            r0["registered"] = bool(same)
            if not same:
                r0["note"] = "not added: nothing was stored for this folder"
        elif same:
            res["note"] = f"{keep} was already in the knowledge base — re-indexed it instead of adding it again"
        if same[1:] and not r0.get("error"):
            res["merged_duplicates"] = [d["path"] for d in same[1:]]                # R35: earlier double entries
        return res

    def remove_project(self, path: str) -> dict[str, Any]:
        with self._write_lock(f"kb_remove_project {path}") as wl:
            db = self._db_if_any(wl.db)
            if db is None:
                raise KnowledgeError(f"{path!r} is not in the knowledge base; indexed: none")
            with db as con:
                hit = self._match_projects(con, path or "")
                if not hit:
                    raise KnowledgeError(f"{path!r} is not in the knowledge base; indexed: {self._project_list(con) or 'none'}")
                by_folder = {norm_path(r["path"]) for r in hit}
                if len(by_folder) > 1:
                    raise KnowledgeError(f"the label {path!r} names {len(by_folder)} different folders: "
                                         f"{[r['path'] for r in hit]} — nothing was removed; pass the folder path")
            with self._db(wl.db) as con:
                n = sum(self._drop_project(con, r["path"]) for r in hit)
        return {"removed": [r["path"] for r in hit], "units_removed": n,
                "note": "only the index entries were removed; your files are untouched"}

    def reindex(self, path: str | None = None) -> dict[str, Any]:
        with self._write_lock(f"kb_reindex {path or '(every changed folder)'}") as wl:
            db = self._db_if_any(wl.db)
            if db is None:
                if path:
                    raise KnowledgeError(f"{path!r} is not in the knowledge base (indexed: none); kb_add_project first")
                return {"reindexed": [], "note": "no project folders in the knowledge base yet — kb_add_project(path)"}
            with db as con:
                if path:
                    rows = self._match_projects(con, path)
                    if not rows:
                        raise KnowledgeError(f"{path!r} is not in the knowledge base (indexed: "
                                             f"{self._project_list(con) or 'none'}); kb_add_project first")
                else:
                    rows = [dict(r) for r in con.execute("SELECT path, files FROM projects")]
            return self._reindex_rows(rows, force=bool(path), db=wl.db)

    def _reindex_rows(self, rows: list[dict[str, Any]], force: bool, db: Path) -> dict[str, Any]:
        """Read each folder, then store its units — and, for a folder being added (`register`), its entry — in one
        transaction. N132: the files that could not be read, the files no PLC project compiles and the files read
        in another encoding than they claim are named in the answer and kept with the entry (kb_status); the whole
        code and comments are stored (they were cut at 20,000 / 5,000 characters and not searchable after that)."""
        from .config import ProjectConfig
        from .project_backend import ProjectIndex
        out = []
        upgraded = self._upgrade_search_table(db)
        for r in rows:
            root = Path(r["path"])
            if not root.is_dir():
                out.append({"project": r["path"], "error": "folder not found (moved or deleted?) — kb_remove_project or re-add"})
                continue
            stamp = _tree_stamp(root)
            if r.get("files") == stamp and not force:
                out.append({"project": r["path"], "unchanged": True})
                continue
            idx = ProjectIndex(ProjectConfig(root=str(root)))
            try:
                scan = idx.scan()
            except Exception as exc:  # noqa: BLE001
                out.append({"project": r["path"], "error": str(exc)})
                continue
            units = [u for u in idx.all_units() if u.kind != "PARSE_ERROR"]     # both MAINs of two PLC projects
            problems = _problems(idx)
            reg = r.get("register")
            with self._db(db) as con:
                if reg is not None:
                    for d in reg["merge"]:
                        self._drop_project(con, d)
                    if con.execute("SELECT 1 FROM projects WHERE path=?", (r["path"],)).fetchone():
                        if reg["relabel"]:
                            con.execute("UPDATE projects SET label=? WHERE path=?", (reg["relabel"], r["path"]))
                    else:
                        con.execute("INSERT INTO projects(path, label, added) VALUES (?,?,?)",
                                    (r["path"], reg["label"], time.strftime("%Y-%m-%dT%H:%M:%S")))
                con.execute("DELETE FROM units WHERE project=?", (r["path"],))
                if self._fts:
                    con.execute("DELETE FROM units_fts WHERE project=?", (r["path"],))
                for u in units:
                    txt = (u.declaration or "") + "\n" + (u.implementation or "")
                    comments = " ".join(a or b for a, b in re.findall(r"\(\*(.*?)\*\)|//([^\n]*)", txt, re.S))
                    row = (r["path"], u.name, u.kind, u.file, u.decl_line0, u.declaration or "", u.implementation or "", comments)
                    con.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?)", row)
                    if self._fts:
                        con.execute(f"INSERT INTO units_fts({UNITS_FTS_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                    (row[0], row[1], row[2], row[3], row[5], row[6], row[7], *unit_words(row[1], row[5], row[6])))
                con.execute("UPDATE projects SET indexed=?, units=?, files=?, problems=? WHERE path=?",
                            (time.strftime("%Y-%m-%dT%H:%M:%S"), len(units), stamp,
                             json.dumps(problems) if problems else None, r["path"]))
            out.append({"project": r["path"], "units": len(units), "files": scan.get("files"), **_problem_counts(problems),
                        **problems})
        res: dict[str, Any] = {"reindexed": out}
        if upgraded is not None:
            res["search_table"] = upgraded
        return res

    def _upgrade_search_table(self, db: Path) -> dict[str, Any] | None:
        """N102: the search table of 0.8.11 has no identifier words. Replaced on the next kb_reindex / kb_add_project
        (the user's call): rebuilt from the stored units in one transaction — no file is read again, the units are
        unchanged; a reader sees the old table or the new one. Also when the search table does not hold the stored
        units (one made after them). -> what was done, None when nothing was to do."""
        with self._db(db) as con:
            if not self._fts:
                return None
            if self._fts_words and con.execute("SELECT COUNT(*) FROM units").fetchone()[0] == \
                    con.execute("SELECT COUNT(*) FROM units_fts").fetchone()[0]:
                return None
            t0 = time.time()
            if con.in_transaction:
                con.commit()
            con.execute("BEGIN IMMEDIATE")
            con.execute("DROP TABLE units_fts")
            con.execute(UNITS_FTS)
            n = 0
            for u in con.cursor().execute("SELECT project, name, kind, file, declaration, implementation, comments "
                                          "FROM units"):              # one unit at a time, not all in memory
                con.execute(f"INSERT INTO units_fts({UNITS_FTS_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (*tuple(u), *unit_words(u["name"], u["declaration"], u["implementation"])))
                n += 1
        self._fts_words = True
        return {"rebuilt": True, "units": n, "seconds": round(time.time() - t0, 2),
                "why": "identifier words (FB_AlarmList → alarm, list) for kb_search — from the stored units, no file "
                       "read again"}

    def search(self, query: str, kinds: list[str] | None = None, project: str | None = None, limit: int = 20) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            raise KnowledgeError("empty query — give words from the code, a unit name or an error message")
        phrases = [x for x in re.findall(r'"([^"]+)"', q) if re.search(r"[^\W_]", x)]
        bare = re.sub(r'"[^"]*"', " ", q)
        words = [w for w in re.findall(r"\w+", bare) if re.search(r"[^\W_]", w)]     # N135: '_' alone is no word
        if not phrases and not words:
            # R37: ':=' used to reach FTS as MATCH '' and fail with an empty message
            raise KnowledgeError(f"the query {q!r} has no searchable word: kb_search matches whole words and word "
                                 "prefixes in the code; punctuation such as ':=' is not indexed — search for the names "
                                 "around it (e.g. 'nCount')")
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise KnowledgeError(f"limit must be 1–200 (got {limit!r})")                          # R39
        want_kinds: list[str] = []
        for k in kinds or []:
            ku = str(k).strip().upper()
            if ku in KIND_ALIASES:
                want_kinds += KIND_ALIASES[ku]
            elif ku in UNIT_KINDS:
                want_kinds.append(ku)
            else:
                raise KnowledgeError(f"unknown kind {k!r}; kinds: {', '.join(UNIT_KINDS)} (FB = FUNCTION_BLOCK, "
                                     "DUT = every data type, POU = PROGRAM / FUNCTION_BLOCK / FUNCTION)")  # R39
        # N71: words of the sentence around the names ('how', 'to', 'the', 'cannot') pick nothing in code: they are
        # not searched for. A query of nothing else is refused (it matched every unit with 'to' in a comment)
        ignored: dict[str, str] = {}
        informative: list[str] = []
        for w in words:
            if w in _OPERATOR_WORDS:
                # N135: 'bEnable OR bReset' was searched as bEnable AND bReset with 'OR' listed as a stop word
                ignored[w] = ("not a search operator — kb_search lists the units with EVERY other word (in code "
                              f"{w} is an ST keyword); for either of two words, run one search per word")
            elif _fold(w) in STOP_WORDS:
                ignored[w] = "stop word"
            elif len(w) == 1:
                ignored[w] = "a single character — not searched in a query of several words (as a prefix it is in "\
                             "nearly every unit)"                                          # N135: 'x', 'ü'
            else:
                informative.append(w)
        if not informative and not phrases and len(words) == 1:
            informative, ignored = list(words), {}       # one word ('WHILE', 'On', 'i'): searched as written
        # N71: an error message: its words ('Cannot convert type … to type …') are not code words — they matched every
        # unit with TYPE or 'to' in it. N129 / N130: whether it is one is decided by the message (a code in a notation
        # that says so, an HRESULT, a compiler message …), not by whether the plugin has a note on it — 'Ads-Error 0xA2'
        # was searched as words ('Error' listed 16 units), 'License' got the licence note; and the names of the code it
        # holds are searched, quoted or not ('ADS error 1808 fbCounter' searched nothing)
        error_msg = is_error_message(q)
        err = known_error(q) if error_msg else None
        if error_msg:
            names = message_names(q)
            for w in informative:
                ignored.setdefault(w, "error message text (not a name of the code)")
            informative = list(dict.fromkeys(w for t in names for w in re.findall(r"\w+", t) if re.search(r"[^\W_]", w)))
            for w in informative:
                ignored.pop(w, None)
            phrases = []
        if not phrases and not informative and not error_msg:
            raise KnowledgeError(f"the query {q!r} has only stop words ({', '.join(ignored)}): kb_search looks for the "
                                 "names and words of the code — give a unit or variable name, or specific words")
        with self._db_read() as con:
            nproj = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            hits: list[dict[str, Any]] = []
            info: dict[str, Any] = {}
            scope: list[str] = []
            if project:
                m = self._match_projects(con, project)
                if not m:
                    raise KnowledgeError(f"no indexed project is {project!r} (path or label); indexed: "
                                         f"{self._project_list(con) or 'none'}")                    # R35 / R39
                scope = [r["path"] for r in m]
            # N133: a folder edited, renamed or deleted after indexing was answered from its old units, silently
            stale = [{"project": r["path"], "label": r["label"], "state": st}
                     for r in con.execute("SELECT path, label, files FROM projects ORDER BY path")
                     if (not scope or r["path"] in scope) and (st := folder_state(r["path"], r["files"])) != "current"]
            base_note = None
            if nproj and (phrases or informative):
                hits, info = self._search_tiers(con, phrases, informative, want_kinds, scope, limit)
                if not hits:
                    # N72: TwinCAT names a copy '<name>_<n>' (PRG_TcUnitRunner_9): no unit has that name — look for the
                    # name it was copied from, and say so
                    bases = {w: mm.group(1) for w in informative
                             if (mm := re.fullmatch(r"(\w*[A-Za-z]\w*?)_(\d+)", w)) and len(mm.group(1)) >= 3}
                    if bases:
                        alt = [bases.get(w, w) for w in informative]
                        hits, info = self._search_tiers(con, phrases, alt, want_kinds, scope, limit)
                        if hits:
                            for h in hits:
                                h["match"] = "base name: " + h["match"]
                            base_note = (f"no unit contains {', '.join(bases)}; these contain the name it is a copy "
                                         f"of ({', '.join(bases.values())}) — TwinCAT names copies <name>_<n>")
        out: dict[str, Any] = {"query": q, "hits": hits, "projects_indexed": nproj}
        if _OPERATOR_SYNTAX.search(bare):
            out["operators_note"] = ("kb_search has no query operators: OR / AND / NOT / NEAR(…), '-', '+', '^' and "
                                     "'*' are not read as such — every other word is searched, as a word and as a word "
                                     "prefix, and a unit must hold all of them (\"quoted text\" is a phrase)")
        if stale:
            out["stale"] = stale
        if ignored or info.get("ignored"):
            out["words_ignored"] = {**ignored, **info.get("ignored", {})}
        if info.get("used") is not None and info.get("tier") == "some words":
            out["words_used"] = info["used"]
        if info.get("too_common") and info.get("tier") == "some words":
            out["words_too_common"] = info["too_common"]
        if base_note:
            out["note"] = base_note
        elif hits and all(h.get("match") == "some words" for h in hits):
            out["note"] = ("no unit contains every word of the query; these contain some of them (best first, "
                           "'matched' names them) — fewer or more specific words help")
        elif nproj and not hits and (phrases or informative):
            out["note"] = (f"no unit contains {', '.join(phrases + informative)} (name, declaration, code, comments of "
                           f"the {nproj} indexed folder(s))")
        elif error_msg and not informative and not phrases:
            out["note"] = ("an error message without a name to look for in the code: known_error has its cause and fix"
                           if err else "an error message without a name to look for in the code, and not one the "
                           "plugin has a note on (known_errors_covered) — its words are not searched")
        if error_msg:
            out["known_error"] = err
            if err is None:
                out["known_errors_covered"] = KNOWN_ERRORS_COVERED
        if not nproj:
            out["note"] = "no project folders in the knowledge base yet — kb_add_project(path) with a folder the user names"
        elif self._fts and not self._fts_words:
            out["search_table_note"] = ("the search table is of 0.8.11: identifiers are not split into words "
                                        "('alarm' does not find FB_AlarmList) — kb_reindex() rebuilds it from the stored "
                                        "units (seconds; no file is read again)")
        return out

    def _search_tiers(self, con: sqlite3.Connection, phrases: list[str], words: list[str], kinds: list[str],
                      scope: list[str], limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """R36: every word first (exact, then as word prefixes: 'CounterMod' → CounterModule — both lists, the exact
        ones first); only when no unit has them all, units with some of them, marked match='some words' with the words
        each one holds. N71: in that tier a word on no unit, or on most units ('TYPE', 'BOOL', 'VAR' in a big project),
        picks nothing and is not used. N102: within that, a unit whose code (not only a comment) holds the words
        first, then the name that names most of the query (docsearch.title_order), then bm25."""
        def ph(t: str) -> str:
            return '"' + t.replace('"', '""') + '"'
        where_extra, params_extra = "", []
        if scope:
            where_extra += f" AND project IN ({','.join('?' * len(scope))})"
            params_extra += scope
        if kinds:
            where_extra += f" AND kind IN ({','.join('?' * len(kinds))})"
            params_extra += kinds
        def pre(w: str) -> str:            # N135: a one-character prefix is in nearly every unit — that word exact
            return ph(w) + ("*" if len(w) > 1 else "")
        info: dict[str, Any] = {}
        exact = [ph(t) for t in phrases + words]
        prefix = [ph(t) for t in phrases] + [pre(w) for w in words]
        tiers = [("all words", " AND ".join(exact), phrases + words)]
        if words:
            tiers.append(("all words (as prefixes)", " AND ".join(prefix), phrases + words))
        if len(exact) > 1:
            total = self._count(con, None, where_extra, params_extra)
            used, ignored, common = [], {}, []
            dfs: dict[str, int] = {}
            for t in phrases + words:
                df = dfs[t] = self._count(con, (ph(t) if t in phrases else pre(t)), where_extra, params_extra, t)
                if df == 0:
                    ignored[t] = "on no unit"
                elif total >= 25 and df > COMMON_SHARE * total:
                    common.append(t)
                else:
                    used.append(t)
            if not used and common:
                # every word is common: the least common still tells units apart better than nothing at all
                least = min(dfs[t] for t in common)
                used = [t for t in common if dfs[t] == least]
                common = [t for t in common if t not in used]
            info.update(ignored=ignored, too_common=common, used=used)
            if used:
                tiers.append(("some words", " OR ".join(ph(t) if t in phrases else pre(t) for t in used), used))
        hits: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        words_cols = self._fts and self._fts_words
        # N102: a word of the name (and of a name's words) weighs most, then the declaration, the comments, the code
        rank = "bm25(units_fts, 0, 10, 0, 0, 3, 1, 2" + (", 10, 1, 0)" if words_cols else ")")
        code_cols = "{name name_words idents code}" if words_cols else None
        fetch = min(max(limit * 3, 60), 600)

        def run(label: str, expr: str, terms: list[str], in_code_only: bool = False) -> list[sqlite3.Row]:
            if self._fts:
                sql = ("SELECT rowid AS rid, project, name, kind, file, snippet(units_fts, 4, '«', '»', ' … ', 12) AS decl, "
                       f"snippet(units_fts, 5, '«', '»', ' … ', 12) AS impl, {rank} AS score "
                       f"FROM units_fts WHERE units_fts MATCH ?{where_extra} ORDER BY score LIMIT ?")
                # in_code_only: the units whose code (not only a comment) holds the words — read first, so that a
                # window of `fetch` rows ranked by bm25 (comments weigh more than code) cannot leave them out
                params: list[Any] = [f"{code_cols} : ({expr})" if in_code_only else expr, *params_extra, fetch]
            else:
                conds = " AND " if label != "some words" else " OR "
                sql = ("SELECT rowid AS rid, project, name, kind, file, substr(declaration,1,200) AS decl, "
                       "substr(implementation,1,200) AS impl, "
                       f"0 AS score FROM units WHERE ({conds.join([self._LIKE_ONE] * len(terms))}){where_extra} LIMIT ?")
                params = []
                for t in terms:
                    params += [self._like(t)] * 4
                params += [*params_extra, fetch]
            try:
                return con.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                raise KnowledgeError(f"search failed for {expr!r}: {exc}") from exc

        # R36: units with every word (as written, or as word prefixes) — both, the exact ones first; only when no unit
        # has them all, units with some of them
        rows: list[sqlite3.Row] = []
        tier_of: dict[int, int] = {}
        used_expr = ""
        for ti, (label, expr, terms) in enumerate(tiers):
            if label == "some words" and rows:     # 'some words' only when no unit has every word
                break
            got = []
            if code_cols:
                try:
                    got = run(label, expr, terms, in_code_only=True)
                except KnowledgeError:
                    got = []
            got += run(label, expr, terms)
            for r in got:
                if r["rid"] not in tier_of:
                    tier_of[r["rid"]] = ti
                    rows.append(r)
            if got:
                used_expr = expr                   # the widest expression that matched: the code check uses it
        if not rows:
            info["tier"] = tiers[-1][0]
            return hits, info
        info["tier"] = tiers[min(tier_of.values())][0]
        in_code: set[int] | None = None
        in_comments: set[int] = set()
        if code_cols:
            # N102: a unit whose code holds the words before one that has them only in its comments
            try:
                in_code = {x[0] for x in con.execute(f"SELECT rowid FROM units_fts WHERE units_fts MATCH ?{where_extra}",
                                                     [f"{code_cols} : ({used_expr})", *params_extra])}
                in_comments = {x[0] for x in con.execute(f"SELECT rowid FROM units_fts WHERE units_fts MATCH ?{where_extra}",
                                                         [f"{{comments}} : ({used_expr})", *params_extra])}
            except sqlite3.OperationalError:
                in_code = None
        declares = self._declaring(con, rows, words)
        order = {id(r): k for k, r in enumerate(DS.title_order(rows, " ".join(phrases + words), "name"))}
        rows = sorted(rows, key=lambda r: (in_code is not None and r["rid"] not in in_code, -declares.get(r["rid"], 0),
                                           tier_of[r["rid"]], order[id(r)]))
        for r in rows:
            key = (r["project"], r["name"], r["file"])
            if key in seen:
                continue
            seen.add(key)
            label, _expr, terms = tiers[tier_of[r["rid"]]]
            h = {"project": r["project"], "unit": r["name"], "kind": r["kind"], "file": r["file"],
                 "declaration": r["decl"], "code": r["impl"], "match": label}
            if in_code is not None and r["rid"] not in in_code:
                # the words are there, not every one in the code — N135: in a string literal is not 'comments'
                h["match_in"] = "comments" if r["rid"] in in_comments else "string literals or comments"
            if declares.get(r["rid"]):
                h["declares"] = True
            if label == "some words":
                h["matched"] = [t for t in terms if self._row_has(con, r["rid"], t, t in phrases)]
            hits.append(h)
            if len(hits) >= limit:
                break
        return hits, info

    def _declaring(self, con: sqlite3.Connection, rows: list[sqlite3.Row], words: list[str]) -> dict[int, int]:
        """N131: how many of the query's identifiers each unit declares — its name, a variable / constant / member /
        parameter of its declaration (MAX_ALARMS : INT := 10), an enum value of an ENUM — so that the declaring unit
        comes first ('MAX_ALARMS' listed GVL_Param 6th of 7, 'nErrorCode' its struct last)."""
        idents = [w for w in words if re.fullmatch(r"[A-Za-z_]\w*", w)]
        if not idents or not rows:
            return {}
        tbl = "units_fts" if self._fts else "units"
        pats = [(w.lower(), re.compile(rf"(?<![\w.]){re.escape(w)}\s*(?:,\s*[A-Za-z_]\w*\s*)*(?:AT\s+%[\w.*]+\s*)?:(?!=)",
                                       re.I),
                 re.compile(rf"(?<![\w.]){re.escape(w)}\s*(?::=\s*[^,;)]+)?\s*[,)]", re.I)) for w in idents]
        out: dict[int, int] = {}
        rids = [r["rid"] for r in rows]
        for i in range(0, len(rids), 500):
            part = rids[i:i + 500]
            for rid, name, kind, decl in con.execute(f"SELECT rowid, name, kind, declaration FROM {tbl} "
                                                     f"WHERE rowid IN ({','.join('?' * len(part))})", part):
                body = _COMMENT_OR_STRING.sub(" ", decl or "")
                short = (name or "").rsplit(".", 1)[-1].lower()
                n = sum(1 for w, var, enum in pats
                        if short == w or var.search(body) or (kind == "ENUM" and enum.search(body)))
                if n:
                    out[rid] = n
        return out

    _LIKE_ONE = ("(name LIKE ? ESCAPE '\\' OR declaration LIKE ? ESCAPE '\\' OR implementation LIKE ? ESCAPE '\\' "
                 "OR comments LIKE ? ESCAPE '\\')")

    @staticmethod
    def _like(t: str) -> str:
        return "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    def _count(self, con: sqlite3.Connection, expr: str | None, where_extra: str, params_extra: list[Any],
               term: str | None = None) -> int:
        """Units (in scope) that hold the FTS expression (or the LIKE term without FTS); all units for None."""
        if expr is None:
            tbl = "units_fts" if self._fts else "units"
            return con.execute(f"SELECT COUNT(*) FROM {tbl} WHERE 1=1{where_extra}", params_extra).fetchone()[0]
        if self._fts:
            return con.execute(f"SELECT COUNT(*) FROM units_fts WHERE units_fts MATCH ?{where_extra}",
                               [expr, *params_extra]).fetchone()[0]
        return con.execute(f"SELECT COUNT(*) FROM units WHERE {self._LIKE_ONE}{where_extra}",
                           [self._like(term or "")] * 4 + params_extra).fetchone()[0]

    def _row_has(self, con: sqlite3.Connection, rid: int, term: str, phrase: bool) -> bool:
        if self._fts:
            e = '"' + term.replace('"', '""') + '"' + ("" if phrase else "*")
            return con.execute("SELECT 1 FROM units_fts WHERE rowid = ? AND units_fts MATCH ?", (rid, e)).fetchone() is not None
        return con.execute(f"SELECT 1 FROM units WHERE rowid = ? AND {self._LIKE_ONE}",
                           [rid] + [self._like(term)] * 4).fetchone() is not None

    # -- K4 hardware: the ESI index (index\\esi.db, twincat_mcp/esi_index.py) ---------------------------
    def esi_folders(self) -> list[Path]:
        """TwinCAT's ESI folders on this PC and knowledge.esi_dirs (the knowledge folder's manuals\\ is read too)."""
        return E.twincat_esi_folders(self.esi_dirs)

    def esi_db(self) -> Path:
        return L.index_file(self.require(), "esi")

    def esi_state(self) -> dict[str, Any]:
        """The ESI index's state (esi_index.state): missing | complete | rebuild_needed | outdated, with a build that
        runs, was interrupted or waits to replace it. An index that exists is used as it is until the user approves
        kb_rebuild(index='esi'); its first build starts by itself (the server)."""
        try:
            loc = self.require()
        except KnowledgeError as exc:
            return {"state": "unknown", "message": str(exc)}
        st = E.state(L.index_file(loc, "esi"), loc, self.esi_dirs)
        st["sources"] = [{"folder": str(f), "source": s} for f, s in
                         E.source_folders(loc, self.esi_dirs, E.recorded_twincat(L.index_file(loc, "esi")))]
        return st

    def esi_build(self, force: bool = False, log: Any = None) -> dict[str, Any]:
        """Build the ESI index in this process (the builder esi_db.py does the same in its own; the server runs that
        one). Raises KnowledgeError when no source exists or another build runs."""
        loc = self.require()
        try:
            return E.build(L.index_file(loc, "esi"), loc, self.esi_dirs, force=force, log=log or (lambda m: None),
                           built_by="twincat-mcp server")
        except E.EsiError as exc:
            raise KnowledgeError(str(exc)) from None

    HW_DETAILS = E.DETAILS

    def _hw(self, fn: str, *a: Any) -> dict[str, Any]:
        """One read of the ESI index, with its state: no index yet → what is happening (the first build), an index
        that is not current → esi_index_note (rule 6: an answer from an index that is not current says so)."""
        st = self.esi_state()
        if st["state"] == "missing":
            rb = st.get("rebuild") or {}
            loc = self.require()
            if rb.get("state") == "running":
                note = (f"the ESI index is being built (its first build: {rb.get('phase') or 'starting'}) — ask again "
                        "in a minute; kb_status shows the progress")
            elif not E.has_esi(E.xml_files(E.source_folders(loc, self.esi_dirs))):
                note = ("no ESI index: no ESI file (an .xml whose root is EtherCATInfo, EtherCATModule, EtherCATDict "
                        "or EtherCATDiag) in the knowledge folder's manuals\\ or TwinCAT's ESI folders — copy the "
                        "vendor's ESI files into manuals\\ (any sub-folder); the first build then starts by itself")
            else:
                note = ("no ESI index yet: its first build starts by itself (server start, or this call through the "
                        "server); or approve kb_rebuild(index='esi')")
            return {"esi_index": st, "note": note}
        try:
            res = getattr(E.EsiIndex(L.index_file(self.require(), "esi")), fn)(*a)
        except E.EsiError as exc:
            raise KnowledgeError(str(exc)) from None
        if st["state"] != "complete" or st.get("rebuild"):
            res["esi_index_note"] = st["message"]
        return res

    def hw_lookup(self, query: str, limit: int = 5, revision: int | str | None = None,
                  detail: str = "newest") -> dict[str, Any]:
        """EtherCAT devices from the ESI index (esi_index.EsiIndex.lookup: detail newest / summary / full, N73), with
        the PDF manuals of the knowledge folder that mention the product (manuals.db, read-only)."""
        q = (query or "").strip()
        if not q:
            raise KnowledgeError("empty query (e.g. 'EL2008', 'EL3356-0010', '#x0bf03052')")
        if detail not in self.HW_DETAILS:
            raise KnowledgeError(f"detail must be one of {list(self.HW_DETAILS)} (got {detail!r})")
        res = self._hw("lookup", q, limit, revision, detail)
        # the PDF manuals of the knowledge folder for the same product codes (N34: ESI type names carry text — 'CX9020
        # EtherCAT slave' — so the order codes are taken out of the query and the types)
        codes: list[str] = []
        for t in [q] + list(res.get("types") or [])[:5]:
            for c in _PRODUCT_IN.findall(t.upper()):
                if c not in codes:
                    codes.append(c)
        res.update(manuals_for(self.location()[0], codes))
        return res

    def hw_search(self, query: str, kinds: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        return self._hw("search", query, kinds, limit)

    def hw_object(self, device: str, index: str | int, revision: int | str | None = None) -> dict[str, Any]:
        return self._hw("object", device, index, revision)

    def hw_diag(self, code: str | int, device: str | None = None, revision: int | str | None = None) -> dict[str, Any]:
        return self._hw("diag", code, device, revision)

    def hw_module(self, ident: str | int, device: str | None = None, revision: int | str | None = None) -> dict[str, Any]:
        return self._hw("module", ident, device, revision)

    # -- K3 libraries --------------------------------------------------------------------
    def library_folders(self) -> list[Path]:
        pd = os.environ.get("ProgramData") or r"C:\ProgramData"
        cands = [Path(d) for d in self.library_dirs] + [
            # 3.1.4026 (XAE as its own package): the library repository moved to PlcEngineering (D38)
            Path(pd) / "Beckhoff" / "TwinCAT" / "PlcEngineering" / "Managed Libraries",
            Path(r"C:\ProgramData\Beckhoff\TwinCAT\PlcEngineering\Managed Libraries"),
            Path(r"C:\TwinCAT\3.1\Components\Plc\Managed Libraries"),
            Path(r"C:\Program Files (x86)\Beckhoff\TwinCAT\3.1\Components\Plc\Managed Libraries"),
            Path(r"C:\ProgramData\Beckhoff\TwinCAT\3.1\Components\Plc\Managed Libraries"),
        ]
        out: list[Path] = []
        for c in cands:
            if c.is_dir() and c not in out:
                out.append(c)
        return out

    def installed_libraries(self) -> dict[str, dict[str, Any]]:
        """{lib_name_lower: {"name", "vendor", "versions": [...]}} from folder names only (no file is opened)."""
        out: dict[str, dict[str, Any]] = {}
        for root in self.library_folders():
            for vendor in root.iterdir():
                if not vendor.is_dir():
                    continue
                for lib in vendor.iterdir():
                    if not lib.is_dir():
                        continue
                    vers = [v.name for v in lib.iterdir() if v.is_dir() and re.match(r"^\d+(\.\d+)+$", v.name)]
                    if vers:
                        d = out.setdefault(lib.name.lower(), {"name": lib.name, "vendor": vendor.name, "versions": []})
                        d["versions"] = sorted(set(d["versions"]) | set(vers), key=_vkey)
        return out


def library_report(plcproj_files: list[str], installed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """K3: per PLC project, every library reference with the version it asks for, the version it effectively
    resolves to on this PC ('*' = newest installed), and what is installed."""
    projects = []
    for f in plcproj_files:
        try:
            root = ET.parse(f).getroot()
        except (ET.ParseError, OSError) as exc:
            projects.append({"plcproj": f, "error": str(exc)})
            continue
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        resolutions = {}
        for el in root.iter(f"{ns}PlaceholderResolution"):
            r = el.find(f"{ns}Resolution")
            if el.get("Include") and r is not None and r.text:
                resolutions[el.get("Include").lower()] = r.text.strip()
        libs = []
        for el in root.iter(f"{ns}PlaceholderReference"):
            name = el.get("Include") or ""
            d = el.find(f"{ns}DefaultResolution")
            libs.append(_lib_row(name, resolutions.get(name.lower()) or (d.text.strip() if d is not None and d.text else ""),
                                 "placeholder", installed, overridden=name.lower() in resolutions))
        for el in root.iter(f"{ns}LibraryReference"):
            inc = el.get("Include") or ""
            libs.append(_lib_row(inc.split(",")[0].strip(), inc, "library", installed))
        projects.append({"plcproj": f, "libraries": libs})
    return {"projects": projects,
            "note": "versions from the .plcproj and from folder names in TwinCAT's library repository; '*' resolves to the "
                    "newest version installed on the PC that builds — pin a version for builds that must be reproducible"}


def _lib_row(name: str, resolution: str, kind: str, installed: dict[str, dict[str, Any]], overridden: bool = False) -> dict[str, Any]:
    m = re.match(r"^\s*([^,]+?)\s*,\s*([^\s(]+)\s*(?:\((.*)\))?", resolution or "")
    lib = (m.group(1) if m else name).strip()
    req = (m.group(2) if m else "?").strip()
    vendor = (m.group(3) or "").strip() if m else ""
    inst = installed.get(lib.lower(), {})
    versions = inst.get("versions", [])
    eff = (versions[-1] if versions else None) if req == "*" else req
    row: dict[str, Any] = {"name": name, "library": lib, "kind": kind, "requested": req, "vendor": vendor or inst.get("vendor"),
                           "effective": eff, "installed": versions}
    if overridden:
        row["pinned_by"] = "PlaceholderResolution"
    if (kind == "placeholder" and not overridden and req != "*" and versions and req not in versions
            and (vendor or inst.get("vendor") or "").lower() == "system"):
        # N19: a placeholder's DefaultResolution is only the fallback — TwinCAT resolves it through the compiler's
        # placeholder profile first; "(System)" libraries (VisuElems …) are always profile-managed
        # N19b: with one installed version the profile can only resolve to it; with several, the profile decides
        row["effective"] = versions[0] if len(versions) == 1 else "profile"
        row["info"] = (f"{lib} {req} (DefaultResolution) is not installed; TwinCAT resolves this placeholder through its "
                       f"placeholder profile first (installed: {versions}). Only a problem if the build reports it.")
    elif req != "*" and versions and req not in versions:
        row["warning"] = (f"{lib} {req} is not installed on this PC (installed: {versions}) — the build will fail to resolve it"
                          + (" unless the compiler's placeholder profile maps it" if kind == "placeholder" and not overridden else ""))
    elif req == "*" and not versions and installed:
        row["warning"] = f"{lib} is not installed on this PC"
    return row


_STAMP_EXT = (".tcpou", ".tcdut", ".tcgvl", ".tcio", ".plcproj", ".tsproj", ".xti", ".sln")
_STAMP_SKIP = {".git", ".vs", ".svn", "_boot", "_compileinfo", "_libraries"}


_STAMP_V = "2:"                          # 0.8.14: .tsproj / .xti / .sln in the stamp — older stamps compare as before


def _legacy_tree_stamp(root: Path) -> str:
    """The stamp 0.8.13 and before stored (sources and .plcproj only) — compared with a folder indexed by them, so an
    unchanged folder is not called changed after the update (review)."""
    import hashlib
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() in (".tcpou", ".tcdut", ".tcgvl", ".tcio", ".plcproj") and f.is_file():
            st = f.stat()
            h.update(f"{f}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _tree_stamp(root: Path) -> str:
    """What the index of a folder depends on: its sources, and what decides which of them are units — the .plcproj
    files (N106), the .tsproj / .xti files that name the PLC projects and the .sln that names the .tsproj (N133: an
    edit of those changed the units without changing the stamp). Folders the scan never reads are not walked."""
    import hashlib
    h = hashlib.sha256()
    found: list[tuple[str, int, int]] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.lower() not in _STAMP_SKIP]
        for name in files:
            if name.lower().endswith(_STAMP_EXT):
                f = os.path.join(dirpath, name)
                try:
                    st = os.stat(f)
                except OSError:
                    continue
                found.append((f, st.st_size, st.st_mtime_ns))
    for f, size, mt in sorted(found):
        h.update(f"{f}:{size}:{mt}\n".encode())
    return _STAMP_V + h.hexdigest()


def folder_state(path: str, stamp: str | None) -> str:
    """N133: an indexed folder against its files now — 'current', or what kb_search answers from (the units of the
    last indexing) and what to do."""
    if not Path(path).is_dir():
        return "folder not found (moved, renamed or deleted?) — its units are those of the last indexing: kb_remove_project, or kb_add_project the new place"
    # one walk of the folder (stat of its sources; .git, _Boot … not walked) — no cache: an answer about the files
    # now must not be seconds old
    cur = _tree_stamp(Path(path)) if (stamp or "").startswith(_STAMP_V) else _legacy_tree_stamp(Path(path))
    if stamp != cur:
        return "files changed since it was indexed — its units are those of the last indexing: kb_reindex"
    return "current"


def _problems(idx: Any) -> dict[str, Any]:
    """N132: what of a folder is not in the index, by file — the files that are no XML, the files read in another
    encoding than they claim, the files no PLC project compiles (a backup copy beside the project)."""
    out: dict[str, Any] = {}
    errs = idx.parse_errors()
    if errs:
        out["parse_error_files"] = errs
    if idx.read_notes:
        out["read_notes"] = dict(sorted(idx.read_notes.items()))
    if idx.not_in_project:
        out["not_in_project"] = {"count": len(idx.not_in_project), "files": idx.not_in_project[:50],
                                 "why": "no PLC project of the folder compiles them (.plcproj): not units"}
    return out


def _problem_counts(problems: dict[str, Any]) -> dict[str, int]:
    return {"parse_errors": len(problems.get("parse_error_files", [])),
            "not_in_project_count": (problems.get("not_in_project") or {}).get("count", 0)}


def ro_uri(db: Path) -> str:
    """SQLite read-only URI: 'file:/C:/x.db', 'file:////server/share/x.db' (Path.as_uri gives a UNC host that SQLite
    rejects as a URI authority)."""
    from urllib.parse import quote
    s = str(db.resolve()).replace("\\", "/")
    if s.startswith("//"):
        s = "//" + s
    elif not s.startswith("/"):
        s = "/" + s
    return "file:" + quote(s, safe="/:") + "?mode=ro"


def _ro(db: Path) -> sqlite3.Connection:
    """The documentation databases belong to the knowledge folder's own tools: opened read-only, never changed here."""
    con = sqlite3.connect(ro_uri(db), uri=True, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


_KBD: dict[str, Any] = {}


def kb_docs_module() -> Any | None:
    """The twincat-docs skill's kb_docs.py, loaded as a module: its manuals_state is the one implementation of the
    manuals index's state (manuals_db.py `stats --json` gives the same answer); the Infosys index's state is
    infosys_build.state, used by every reader directly."""
    p = docs_skill_path()
    if p is None:
        return None
    key = f"{p}|{p.stat().st_mtime_ns}"
    if _KBD.get("key") != key:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tcmcp_kb_docs", p)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _KBD.update(key=key, mod=mod)
    return _KBD["mod"]


def _builder_version(f: Path) -> str | None:
    """Version of a builder copy in <kb>\\tools (version 1 had no BUILDER_VERSION line)."""
    try:
        m = re.search(r'^BUILDER_VERSION = "(\d+)"', f.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError:
        return None
    return m.group(1) if m else "1"


BUILDER_NAMES = ("infosys_local.py", "manuals_db.py", "esi_db.py")
PLUGIN_POINTER = "plugin_location.json"
_STUB_MARK = "twincat-mcp builder stub"
# N64: a file in <kb>\tools is replaced by the stub only when it IS a file this plugin shipped — identified by its
# content (sha256 with CRLF read as LF: a git checkout on Windows converts line endings), never by how it looks.
# Every version of the builders that ever left this repository (releases, and copies handed out from main):
SHIPPED_BUILDERS: dict[str, dict[str, str]] = {
    "infosys_local.py": {
        "b59a40bc327b64171ca277bd313ae6994d08a76eebf9b2e6e3c0f9ee93783873": "2",   # e92f196
        "1682c747a8359454625d65eeb2c4831802e46e6a562848d1491d37049b49cf53": "2",   # e7e5b3f = release 0.8.9
        "864f9efc0bbebdde01ce54d6d47767f8d3c55eb11699a8971fb239550e8efdd0": "2",   # 1200fee (TCMCP_STUB_DIR)
        "fc62baf3e0ef001dff5ff7eee6f5d1ba8d07c9e654111092d81fe6c6bbee1cda": "2",   # release 0.8.10
        "dfbed120f63d7cac155a48a936796b3384beac2d3b6419f8e8af04db7213ddfb": "3",   # release 0.8.11
    },
    "manuals_db.py": {
        "530a0c81b2dab0e6580d183348017dfbb7c05d795b6ceeb3808af934da3ab33d": "2",   # e92f196
        "5d9cc66b174fc88bbb30dfa84114c0de367f1e3de7aba9b2f22375c2f0ae688b": "2",   # e7e5b3f
        "606fc4fc36cc0a86d79afaae59e6935311e0165246f5de975145b0fc94a772ea": "3",   # 570c533
        "217756a3d65af0755cce0100680bd1cc77934077a542e99ebfdd00a6e2de289c": "3",   # 08d3a44 = release 0.8.9
        "057677fad71344f574eaa97104d1e47ec8e556db5aa9341333abeab3e8990ebe": "3",   # 1200fee (TCMCP_STUB_DIR)
        "7f65bb454e0e57975ca5227fafce3b8db444dbcc334c990ab1da8359272fa341": "4",   # release 0.8.10
        "cb18f84dd247bd5451cabb10055879c5210702a739cb1719d90fe42e85de66f8": "5",   # release 0.8.11
        "fda439492e2533cbc075ea8451aa135deaf04342ef5782c703b3c203a8de3808": "6",   # release 0.8.12
    },
    "esi_db.py": {
        "724ab1d168c1ebcf8551b873335327b501153003d55ff12bfadc4f3287bbd49e": "1",   # release 0.8.12 (new)
    },
}
SHIPPED_STUBS: dict[str, str] = {
    "6eb88af57099b2610da38becf909609829ed9c69d015cb98800e0740a86c9adf": "1",       # 1200fee … release 0.8.11
    "220e8968ce5e629771024c610214d1849d1e1ee4d99543227c96089bff7bf511": "2",       # release 0.8.12
}


def builders_dir() -> Path | None:
    """The builders the plugin ships (skills/twincat-docs/builders), or None."""
    shipped = docs_skill_path()
    b = shipped.parent / "builders" if shipped else None
    return b if b is not None and b.is_dir() else None


def _content_sha(f: Path) -> str | None:
    """sha256 of a text file with CRLF read as LF — the identity of a shipped file on any checkout (a Windows checkout
    converts line endings). Nothing else is normalised: a file that differs in any other way is not one we shipped."""
    import hashlib
    try:
        return hashlib.sha256(f.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except OSError:
        return None


def builder_kind(f: Path, name: str) -> tuple[str, str | None]:
    """What a file in <kb>\\tools is, by its content:
    ('stub', version)  the stub the plugin ships now;
    ('copy', version)  a builder or an older stub this plugin shipped (sha256 known) — the server may replace it;
    ('other', None)    anything else: the user's own script, an edited copy, a newer one — never replaced."""
    sha = _content_sha(f)
    if sha is None:
        return "other", None
    b = builders_dir()
    current_stub = _content_sha(b / "stub.py") if b is not None and (b / "stub.py").is_file() else None
    if sha == current_stub:
        m = re.search(r'^STUB_VERSION = "(\d+)"', f.read_text(encoding="utf-8", errors="replace"), re.M)
        return "stub", m.group(1) if m else None
    if sha in SHIPPED_STUBS:
        return "copy", f"stub {SHIPPED_STUBS[sha]}"
    if sha in SHIPPED_BUILDERS.get(name, {}):
        return "copy", SHIPPED_BUILDERS[name][sha]
    shipped_now = b / name if b is not None else None
    if shipped_now is not None and shipped_now.is_file() and sha == _content_sha(shipped_now):
        return "copy", _builder_version(shipped_now)        # a copy of the builder this plugin version ships
    return "other", None


def record_plugin_location(home: Path, version: str) -> str:
    """Write <home>/plugin_location.json (the shipped builders' folder and the plugin version) when it changed. The
    builder stubs in <kb>\\tools find the current builders through it after every plugin update. -> 'written',
    'unchanged' (it already names these builders and this version) or why nothing was written."""
    b = builders_dir()
    if b is None:
        return "not written: the plugin's builders folder was not found"
    p = home / PLUGIN_POINTER
    data = {"builders": str(b), "version": version}
    try:
        cur = json.loads(p.read_text(encoding="utf-8"))
        if {k: cur.get(k) for k in data} == data:
            return "unchanged"
    except (OSError, ValueError):
        pass
    home.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps({**data, "written": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "note": "read by the builder stubs in <kb>\\tools — written by the twincat MCP server"},
                              indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return "written"


def install_builder_stubs(loc: Path) -> dict[str, Any]:
    """<kb>\\tools\\infosys_local.py / manuals_db.py that are a file this plugin shipped (a builder of any version, or
    an older stub — known by its sha256) are replaced by the current stub, which runs the plugin's builders. The
    replaced file is kept beside it as <name>.v<version>.bak. Anything else — the user's own script, an edited copy —
    is left alone and reported with the command that switches it to the stub. A missing file is not created."""
    b = builders_dir()
    out: dict[str, Any] = {}
    if b is None or not (b / "stub.py").is_file():
        return {"skipped": "the plugin's builders (skills/twincat-docs/builders/stub.py) were not found"}
    stub_text = (b / "stub.py").read_text(encoding="utf-8")
    m = re.search(r'^STUB_VERSION = "(\d+)"', stub_text, re.M)
    stub_ver = m.group(1) if m else None
    for n in BUILDER_NAMES:
        f = loc / "tools" / n
        if not f.is_file():
            continue
        kind, ver = builder_kind(f, n)
        if kind == "stub":
            out[n] = {"state": "stub", "stub_version": ver}
            continue
        if kind == "other":
            out[n] = {"state": "left alone", "sha256": _content_sha(f),
                      "reason": "not a file this plugin shipped (your own script, or an edited copy) — not replaced",
                      "to_use_the_plugin_builders": f'rename it, then: copy "{b / "stub.py"}" "{f}"'}
            continue
        tag = (ver or "unknown").replace(" ", "")
        backup = f.with_name(f"{n}.v{tag}.bak")
        if backup.exists():
            backup = f.with_name(f"{n}.v{tag}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
        try:
            shutil.copy2(f, backup)
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(stub_text, encoding="utf-8")
            os.replace(tmp, f)
        except OSError as exc:
            out[n] = {"state": "error", "error": str(exc)}
            continue
        out[n] = {"state": "replaced", "was": f"shipped version {ver}", "backup": str(backup), "stub_version": stub_ver}
    return out


def sync_builder_stubs(home: Path, loc: Path | None, version: str) -> dict[str, Any]:
    """N68: the pointer first, then the stubs — a stub installed before the pointer is written would run whatever an
    old pointer (or the plugin cache) names. Used at every server start and by kb_set_location."""
    # 'unchanged' when it already names this version's builders (an info line of the 0.8.10 retest read false as a fault)
    res: dict[str, Any] = {"pointer": record_plugin_location(home, version)}
    if loc is not None and (loc / "tools").is_dir():
        res["tools"] = install_builder_stubs(loc)
    return res


def _builders(loc: Path) -> dict[str, Any]:
    bdir = builders_dir()
    out: dict[str, Any] = {}
    for n in BUILDER_NAMES:
        plug = bdir / n if bdir and (bdir / n).is_file() else None
        copy = loc / "tools" / n
        e: dict[str, Any] = {"shipped": str(plug) if plug else None,
                             "shipped_version": _builder_version(plug) if plug else None,
                             "copy_in_tools": str(copy) if copy.is_file() else None}
        if copy.is_file():
            kind, ver = builder_kind(copy, n)
            e["copy_kind"] = kind
            if kind == "stub":
                e["note"] = (f"tools\\{n} is the builder stub: it runs the plugin's {n} (version {e['shipped_version']}) — "
                             "nothing to update")
            elif kind == "copy":
                e["copy_version"] = ver
                e["note"] = (f"tools\\{n} is a copy the plugin shipped ({ver}); the server replaces it with the builder "
                             "stub at its next start")
            else:
                e["note"] = (f"tools\\{n} is not a file the plugin shipped (your own script or an edited copy) — left "
                             f"alone; the commands below use the plugin's own builder")
        out[n] = e
    return out


def _cmd(builders: dict[str, Any], name: str, loc: Path, sub: str) -> str:
    b = builders.get(name) or {}
    if b.get("copy_kind") == "stub":                      # the stable path in the knowledge folder
        return f'<Python 3.12+> "{b["copy_in_tools"]}" {sub} --kb "{loc}"'
    script = b.get("shipped") or (b.get("copy_in_tools") if b.get("copy_kind") == "copy" else None) or name
    return f'<Python 3.12+> "{script}" {sub} --kb "{loc}"'


def _docs_parts(loc: Path, local: Any = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """The two documentation indexes of the knowledge folder, as every reader reports them: Infosys
    (infosys_build.state — local: the Help Viewer package the server reads) and manuals (the docs skill's
    manuals_state), with the command that rebuilds one that is not complete."""
    from . import infosys_build as IB
    idb, mdb = L.locate(loc, "infosys"), L.locate(loc, "manuals")
    builders = _builders(loc)
    try:
        inf = IB.state(idb, local=local)
    except Exception as exc:  # noqa: BLE001
        inf = {"database": str(idb), "state": "error", "message": f"not readable: {exc}"}
    kd = kb_docs_module()
    if kd is not None:
        try:
            man = kd.manuals_state(mdb, loc / "manuals")
        except Exception as exc:  # noqa: BLE001
            man = {"database": str(mdb), "state": "error", "message": f"not readable: {exc}"}
    else:
        man = {"database": str(mdb), "state": "unknown", "message": "the twincat-docs skill is not installed next to the server"}
    inf["exists"], man["exists"] = idb.is_file(), mdb.is_file()
    if idb.is_file():
        inf["size_mb"] = round(idb.stat().st_size / 1e6, 1)
    rb = (inf.get("rebuild") or {}).get("state")
    if rb != "running" and (inf.get("state") != "complete" or rb in ("interrupted", "waiting_to_replace")):
        inf["command"] = (_cmd(builders, "infosys_local.py", loc, "index")
                          + "   (or kb_rebuild(index='infosys'), which asks for approval)")
    if man.get("state") != "complete" and (man.get("updating") or {}).get("state") != "running":
        man["command"] = (_cmd(builders, "manuals_db.py", loc, "update") + "   (PDF text: pypdf, or PyMuPDF if you "
                          "installed it; or kb_rebuild(index='manuals'), which asks for approval)")
    return inf, man, builders


_PRODUCT = re.compile(r"^[A-Za-z]{1,4}\d{3,5}(?:-\d{4})?$")
_PRODUCT_IN = re.compile(r"\b([A-Z]{1,4}\d{3,5}(?:-\d{4})?)\b")


def _product_rows(con: sqlite3.Connection, code: str, limit: int) -> tuple[list[sqlite3.Row], str | None]:
    """N67: a product is its base order code plus an optional variant (CX9020-0130 is a CX9020; EL3102-0010 an
    EL3102). Look for the code itself (or its variants); a variant no manual names falls back to the base code — a
    manual is written for the base product — labelled 'base product CX9020'. -> (rows, label or None)."""
    def q(c: str) -> list[sqlite3.Row]:
        like = c.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "-%"
        return con.execute("SELECT d.name, d.id, GROUP_CONCAT(p.page) AS pages, SUM(p.n) AS hits FROM product p "
                           "JOIN doc d ON d.id = p.doc WHERE p.code = ? OR p.code LIKE ? ESCAPE '\\' "
                           "GROUP BY p.doc ORDER BY hits DESC LIMIT ?", (c, like, limit)).fetchall()
    rows = q(code)
    m = re.fullmatch(r"([A-Z]{1,4}\d{3,5})-\d{4}", code)
    if rows or not m:
        return rows, None
    return q(m.group(1)), f"base product {m.group(1)} (no manual names the variant {code})"


def manuals_for(loc: Path | None, codes: list[str]) -> dict[str, Any]:
    """Which indexed PDF manuals mention these product codes (manuals_db.py's product table: exact code or CODE-xxxx)."""
    if loc is None or not codes:
        return {}
    db = L.locate(loc, "manuals")
    if not db.is_file():
        return {"manuals_note": f"no manuals database at {db} — PDF manuals copied into {loc / 'manuals'} and indexed "
                                "with manuals_db.py update show up here (and in the twincat-docs skill)"}
    found: dict[str, list[dict[str, Any]]] = {}
    try:
        con = _ro(db)
        try:
            for code in codes[:3]:
                c = code.strip().upper()
                rows, via = _product_rows(con, c, 5)
                if rows:
                    found[c] = [{"doc": r["name"], "doc_id": r["id"], "mentions": r["hits"],
                                 "pages": sorted({int(x) for x in (r["pages"] or "").split(",") if x})[:20],
                                 **({"matched_as": via} if via else {})} for r in rows]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"manuals_note": f"manuals database not readable: {exc}"}
    if not found:
        return {"manuals_note": f"no indexed manual mentions {', '.join(codes[:3])} — copy its PDF into "
                                f"{loc / 'manuals'} and run manuals_db.py update"}
    return {"manuals": found, "manuals_more": "twincat-docs skill: manual_search query=… doc=… / manual_page"}


class _WriteLock:
    """with kb._write_lock(what): … — kb_lock's operating-system lock on index\\knowledge.db.lock; KnowledgeError
    naming the running change when another holds it."""

    def __init__(self, path: Path, what: str, db: Path):
        self.lk = kb_lock.IndexLock(path)
        self.what = what
        self.db = db                       # the database this change writes, whatever the location becomes

    def __enter__(self) -> "_WriteLock":
        try:
            self.lk.__enter__()
        except kb_lock.Held as h:
            i = h.info or {}
            raise KnowledgeError(f"another change of the knowledge base is running ({i.get('phase') or 'unknown'}, "
                                 f"pid {i.get('pid')}, started {i.get('started')}) — wait until it is finished, then "
                                 "repeat this call (kb_status shows the result)") from None
        self.lk.update(phase=self.what)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.lk.__exit__(*exc)


class _Conn:
    def __init__(self, path: Path, kb: Knowledge, write: bool = True, scratch: bool = False):
        self.path, self.kb, self.write, self.scratch = path, kb, write, scratch
        self.con: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        if self.write:
            try:
                self.con.execute("PRAGMA journal_mode=WAL")
                self.con.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.DatabaseError:
                pass
        if self.scratch:                   # an empty stand-in: the tables a read lists, no state taken from it
            self.con.execute("CREATE TABLE projects (path TEXT PRIMARY KEY, label TEXT, added TEXT, indexed TEXT, "
                             "units INTEGER, files TEXT, problems TEXT)")
            self.con.execute("CREATE TABLE units (project TEXT, name TEXT, kind TEXT, file TEXT, line INTEGER, "
                             "declaration TEXT, implementation TEXT, comments TEXT)")
            return self.con
        self.kb._init(self.con, self.write)
        return self.con

    def __exit__(self, et, ev, tb):
        assert self.con is not None
        try:
            if et is None:
                self.con.commit()
        finally:
            self.con.close()
