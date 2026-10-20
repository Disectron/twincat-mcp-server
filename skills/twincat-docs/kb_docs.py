"""twincat-docs: offline Beckhoff documentation lookup, read-only, one JSON object per call.

Reads three indexes of the local TwinCAT knowledge base (`<kb>`, see `status`; the layout and rules:
docs/KNOWLEDGE_BASE.md of the twincat plugin):
  <kb>\\index\\infosys-<lcid>.db   Beckhoff Infosys from the Help Viewer package — the same index the plugin's infosys_*
                                 tools read, built by the plugin's extractor (builders\\infosys_local.py index)
  <kb>\\index\\manuals.db         PDF hardware manuals, built by builders\\manuals_db.py update
  <kb>\\index\\esi.db             EtherCAT device descriptions (ESI files in manuals\\ and TwinCAT's ESI folders), built by
                                 builders\\esi_db.py update — the same index the plugin's hw_* tools read
(the builders ship next to this script and need Python 3.12+; <kb>\\tools\\ holds stubs that run them)
Nothing is written: both are opened with SQLite's read-only mode, and what they hold is shown as stored (it is
normalised when indexed, never repaired here). Standard library only (and the plugin's own twincat_mcp package beside
this skill). Needs Python 3.12 or newer (checked at start, like the builders and the server), SQLite with FTS5 — the
python.org builds have it.

usage:
  python kb_docs.py <command> key=value ... [--kb PATH]
  python kb_docs.py help

Exit code 0 with the answer, 1 with {"error": code, "message": ..., [...]}. Values with spaces go in quotes:
  python kb_docs.py docs_search "query=fbRead( READ := FALSE )" limit=3

Deliberate behaviour: a name documented by different pages is never resolved silently (error 'ambiguous' with the
candidates; one page in several books is one page — the plugin's rule, twincat_mcp.infosys.symbol_documents),
filters are applied before the row limit so 'total' is exact ('total' counts pages: a page in several books once per
book; docs_search lists it once, with same_page_in, and 'truncated' says whether hits were not shown), out-of-range
parameters are refused instead of clamped, an invalid FTS5 query is retried once as a literal phrase, a query of
nothing but stop words is refused (the rule of the twincat server's kb_search).
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:   # before anything else, so an older Python gets this message, not a traceback
    sys.exit(f"kb_docs.py needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
             f"{sys.version.split()[0]} ({sys.executable}). Run it with a newer python.exe, e.g. "
             r"C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe")

import datetime as _dt  # noqa: E402 — after the version check on purpose
import difflib
import fnmatch
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

VERSION = "5"           # 2: index state in every answer, links as refs, builders shipped beside it; 3: Python 3.12+
                        # 4: the plugin's one Infosys index (index\\infosys-<lcid>.db), the permanent layout (0.8.11)
                        # 5: hw_* commands on the ESI index (index\\esi.db), its state in status; a name resolved by the
                        #    plugin's rule (one page in several books is one page; a symbol is documented by a symbol
                        #    page), as infosys_declaration does (0.8.12, N100); docs_search is the plugin's search
                        #    (twincat_mcp.infosys.search_index: identifier words, titles first, a code's table first,
                        #    copies once — N99, N105)
FALLBACK_KB = r"D:\TwinCAT-MCP\knowledge"
WEB = "https://infosys.beckhoff.com/content/{lcid}/{book}/{pid}.html"
BUILDERS = Path(__file__).resolve().parent / "builders"
_PLUGIN = Path(__file__).resolve().parents[2]          # <plugin>/skills/twincat-docs/kb_docs.py → <plugin>
# the plugin's own package beside this skill (this version's rules) — unless this runs inside the server, which has it
if "twincat_mcp" not in sys.modules and (_PLUGIN / "twincat_mcp" / "kb_layout.py").is_file() and str(_PLUGIN) not in sys.path:
    sys.path.insert(0, str(_PLUGIN))
from twincat_mcp import esi_index as ESI  # noqa: E402 — the ESI index: one implementation (the server's hw_* tools)
from twincat_mcp import infosys_build as IB  # noqa: E402 — the index's state: one implementation for every reader
from twincat_mcp import infosys as INF  # noqa: E402 — which pages document a name: the plugin's one rule (N100)
from twincat_mcp import docsearch as DS  # noqa: E402 — searching: the plugin's one ranking (N105)
from twincat_mcp import kb_layout as L  # noqa: E402 — where everything is: one place
from twincat_mcp import kb_lock as KL  # noqa: E402 — whether a build runs: the OS lock, never a guess
BUILD_INFOSYS = (f'<Python 3.12+> "{BUILDERS / "infosys_local.py"}" index --kb "<kb>"   (reads the Help Viewer package '
                 "BKINFOSYS3*.mshc with the plugin's extractor; or approve kb_rebuild(index='infosys') in the twincat "
                 "server)")
BUILD_MANUALS = (r"copy the PDF manuals into <kb>\manuals\ (sub-folders allowed), then: <Python 3.12+ with pypdf or "
                 f'PyMuPDF> "{BUILDERS / "manuals_db.py"}" update --kb "<kb>"')

RULES = [
    "Documentation questions use this skill; ads_* tools read the running PLC only and never return documentation.",
    "Known symbol -> docs_declaration; unknown -> docs_search -> ref -> docs_page section=...",
    "A name documented by different pages is never resolved silently: 'ambiguous' + candidates. Pick the library "
    "the project uses (proj_libraries), call again with book= or ref=, and say which one you used. One page in "
    "several books is one page (same_page_in); a symbol is documented by its symbol page, not by a C++/.NET page of "
    "that title — the rule of the plugin's infosys_declaration.",
    "also_in lists the other pages of that title or symbol (same_document: the same page or declaration).",
    "Every item has ref = book/pageid; docs_page accepts that ref, an Infosys URL, a bare page id, an exact title "
    "and ms-xhelp:///?Id=... links.",
    "Cite ref + url in the answer.",
    "Queries are FTS5: words ANDed, \"exact phrase\", OR, NOT, prefix*. Invalid syntax is retried once as a phrase "
    "(query_mode says so).",
    "Ranges are checked, not clamped: limit 1-50, max_chars 200-20000, offset >= 0, depth 1-6, at most 21 pages. "
    "manual_search's limit above 50 is used as 50 (clamped: true says so); its hits page with offset=next_offset.",
    "Long pages: section= first (outline lists them), then offset=next_offset. A manual's pages: manual_page "
    "pages=<next_pages> offset=<next_offset>, as `next` names them.",
    "manual_search: a hex number or CoE index (0x8000, #x8000, 16#8000, 8000:11) is found in any notation, a unit "
    "symbol (°C, mV/V, mA, µs) as written on the page; `terms` says how each was searched.",
    "Nothing found -> say so. A missing manual -> the user copies the PDF into <kb>\\manuals\\ and runs "
    "manuals_db.py update (or approves kb_rebuild(index='manuals')).",
    "source.state other than 'complete' (rebuild_needed, outdated) -> say so: the answer comes from that index as it "
    "is, and the rebuild runs only once the user approves it (kb_rebuild, or the command in source.state_note).",
    "Read-only: nothing here changes the databases.",
]


_KB_SHOWN = "<kb>"          # the knowledge folder as hints show it; main() sets the real one


def kbtext(s: str) -> str:
    return s.replace("<kb>", _KB_SHOWN)


class Err(Exception):
    def __init__(self, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.code, self.message = code, kbtext(message)
        self.extra = {k: kbtext(v) if isinstance(v, str) else v for k, v in extra.items()}


# ---------------------------------------------------------------------------------------------- arguments
class P:
    """One parameter: kind 'str' | 'int', required, default, (lo, hi) for ints. clamp: a value above hi is used as hi
    (bind records it in '_clamped'; the command says so) instead of being refused. route: how to read on, named in
    the refusal of an out-of-range value (N169)."""

    def __init__(self, kind: str = "str", required: bool = False, default: Any = None, lo: int | None = None,
                 hi: int | None = None, doc: str = "", clamp: bool = False, route: str = ""):
        self.kind, self.required, self.default, self.lo, self.hi, self.doc = kind, required, default, lo, hi, doc
        self.clamp, self.route = clamp, route

    def describe(self) -> str:
        s = self.doc or self.kind
        if self.lo is not None and self.clamp and self.hi is not None:
            s += f" ({self.lo}-{self.hi}; above {self.hi}: {self.hi} is used)"
        elif self.lo is not None:
            s += f" ({self.lo}-{self.hi})" if self.hi is not None else f" (>= {self.lo})"
        if self.required:
            s += ", required"
        elif self.default is not None:
            s += f", default {self.default}"
        return s


def parse_argv(argv: list[str]) -> tuple[str | None, dict[str, str], str | None]:
    """-> (command, {key: value}, --kb). Accepts key=value, --key=value and --key value."""
    cmd: str | None = None
    kv: dict[str, str] = {}
    kb: str | None = None
    last: str | None = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--help", "-h") and cmd is None:
            cmd = "help"
        elif a.startswith("--"):
            body = a[2:]
            if "=" in body:
                k, v = body.split("=", 1)
            else:
                if i + 1 >= len(argv):
                    raise Err("bad_param", f"{a} needs a value (key=value)")
                k, v = body, argv[i + 1]
                i += 1
            if k == "kb":
                kb = v
            else:
                _put(kv, k, v)
                last = k
        elif cmd is None and "=" not in a:
            cmd = a
        elif _KEY.match(a):
            k, v = a.split("=", 1)
            _put(kv, k, v)
            last = k
        elif last is not None:
            # an unquoted value with spaces arrives as several arguments: query=fbRead( READ := FALSE )
            kv[last] += " " + a
        else:
            raise Err("bad_param", f"unexpected argument {a!r}: parameters are key=value (quote a value with spaces "
                                   "together with its key: \"query=a b\")")
        i += 1
    return cmd, kv, kb


_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _put(kv: dict[str, str], k: str, v: str) -> None:
    k = k.strip()
    if not k:
        raise Err("bad_param", "a parameter has no name (key=value)")
    if k in kv:
        raise Err("bad_param", f"parameter {k!r} given twice")
    kv[k] = v


def bind(spec: dict[str, P], kv: dict[str, str]) -> dict[str, Any]:
    unknown = sorted(set(kv) - set(spec))
    if unknown:
        raise Err("bad_param", f"unknown parameter(s) {unknown}; allowed: {', '.join(spec) or 'none'}")
    out: dict[str, Any] = {}
    for name, p in spec.items():
        raw = kv.get(name)
        if raw is None or (raw.strip() == "" and p.kind == "int"):
            if p.required:
                raise Err("bad_param", f"{name} is required ({p.describe()})")
            out[name] = p.default
            continue
        if p.kind == "int":
            try:
                v = int(raw.strip())
            except ValueError:
                raise Err("bad_param", f"{name} must be a whole number, got {raw!r}") from None
            if p.clamp and p.hi is not None and v > p.hi:      # N167: used as the maximum, and said so
                out.setdefault("_clamped", {})[name] = (v, p.lo, p.hi)
                v = p.hi
            if (p.lo is not None and v < p.lo) or (p.hi is not None and v > p.hi):
                rng = f"{p.lo}-{p.hi}" if p.hi is not None else f">= {p.lo}"
                raise Err("bad_param", f"{name} must be {rng} (got {v}) — out-of-range values are refused, not clamped"
                          + (f"; {p.route}" if p.route else ""))
            out[name] = v
        else:
            if p.required and not raw.strip():
                raise Err("empty_query" if name == "query" else "bad_param", f"{name} is empty")
            out[name] = raw
    return out


# ---------------------------------------------------------------------------------------------- location / databases
def kb_root(arg: str | None) -> tuple[Path, str]:
    if arg:
        return Path(arg).expanduser(), "--kb"
    env = os.environ.get("TCMCP_KNOWLEDGE_DIR")
    if env:
        return Path(env).expanduser(), "environment TCMCP_KNOWLEDGE_DIR"
    home = Path(os.environ.get("TCMCP_HOME") or (Path.home() / ".twincat_mcp"))
    try:
        d = json.loads((home / "knowledge_location.json").read_text(encoding="utf-8"))
        if d.get("path"):
            return Path(d["path"]), f"kb_set_location ({home / 'knowledge_location.json'})"
    except (OSError, ValueError):
        pass
    return Path(FALLBACK_KB), "default (no kb_set_location yet)"


def ro_uri(db: Path) -> str:
    """'file:/C:/kb/x.db?mode=ro' — and 'file:////server/share/x.db' for UNC paths, which Path.as_uri() turns into a
    host part SQLite refuses."""
    from urllib.parse import quote
    s = str(db.resolve()).replace("\\", "/")
    s = "//" + s if s.startswith("//") else (s if s.startswith("/") else "/" + s)
    return "file:" + quote(s, safe="/:") + "?mode=ro"


# ---------------------------------------------------------------------------------------------- the real index state
# The Infosys index: twincat_mcp.infosys_build.state — the one implementation (the server's kb_status / tc_doctor, the
# instructions it sends at every (re)connection, the builder's `stats` and every answer here use it). The manuals
# index: manuals_state below (the builder's `stats --json` gives the same answer). Both follow the rules: a rebuild
# writes <db>.building and replaces the index only when complete, so the live file is always a whole index; a running
# build holds an operating-system lock on <db>.lock (twincat_mcp.kb_lock).


def _builder_text_format() -> str:
    """manuals_db.py's TEXT_FORMAT — read from the builder shipped beside this script, never a copy of the number."""
    try:
        m = re.search(r'^TEXT_FORMAT = "(\d+)"', (BUILDERS / "manuals_db.py").read_text(encoding="utf-8"), re.M)
    except OSError:
        return "?"
    return m.group(1) if m else "?"


MANUALS_TEXT_FORMAT = _builder_text_format()


def _meta_of(path: Path) -> dict[str, str]:
    return IB.read_meta(path)


def index_state(final: Path) -> dict[str, Any]:
    return IB.state(final)


def index_message(s: dict[str, Any]) -> str:
    return IB.message(s)


def manuals_state(db: Path, folder: Path) -> dict[str, Any]:
    """The manuals index: missing | complete | outdated (PDFs added, changed or removed since) | rebuild_needed (built
    by another page text format) — an index that is not complete stays in use as it is until an update runs
    (approved: kb_rebuild(index='manuals'), or tools\\manuals_db.py update) — plus an update in progress or abandoned
    (<db>.building, its lock the heartbeat)."""
    out: dict[str, Any] = {"database": str(db), "folder": str(folder)}
    try:
        on_disk = {os.path.normcase(str(f)): f for f in folder.rglob("*")
                   if f.suffix.lower() == ".pdf" and f.is_file()} if folder.is_dir() else {}
    except OSError:
        on_disk = {}
    out["pdfs_in_folder"] = len(on_disk)
    building, lock = db.with_name(db.name + ".building"), db.with_name(db.name + ".lock")
    upd: dict[str, Any] | None = None
    alive = KL.held(lock)
    if alive or building.is_file():
        bm = _meta_of(building)
        upd = {"state": "running" if alive else "interrupted", "files_done": int(bm.get("files_done") or 0),
               "files_total": int(bm.get("files_total") or 0)}
    if not db.is_file():
        out["state"] = "missing"
        out["message"] = (f"no manuals index yet ({len(on_disk)} PDFs in the folder) — tools\\manuals_db.py update, or "
                          "kb_rebuild(index='manuals')")
        if upd:
            out["updating"] = upd
            out["message"] += (f"; an update is RUNNING ({upd['files_done']} of {upd['files_total']} files)"
                               if upd["state"] == "running" else "; an update was INTERRUPTED")
        return out
    con = sqlite3.connect(ro_uri(db), uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        docs = [dict(r) for r in con.execute("SELECT path, name, size, mtime, pages, text_pages FROM doc")]
        try:
            meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
        except sqlite3.OperationalError:
            meta = {}
    finally:
        con.close()
    known = {os.path.normcase(d["path"]): d for d in docs}
    new = sorted(f.name for k, f in on_disk.items() if k not in known)
    changed = sorted(f.name for k, f in on_disk.items() if k in known and
                     (known[k]["size"] != f.stat().st_size or known[k]["mtime"] != int(f.stat().st_mtime)))
    gone = sorted(d["name"] for k, d in known.items() if k not in on_disk)
    out.update({"manuals": len(docs), "pages": sum(d["pages"] or 0 for d in docs), "not_indexed": new,
                "changed_since_index": changed, "indexed_but_gone": gone,
                "no_text_layer": sorted(d["name"] for d in docs if not d["text_pages"]),
                "last_update": meta.get("last_update"), "last_result": meta.get("last_result")})
    pending = len(new) + len(changed) + len(gone)
    fmt = meta.get("text_format") or "1"
    old_text = bool(docs) and fmt != MANUALS_TEXT_FORMAT
    out["text_format"] = fmt
    out["text_format_expected"] = MANUALS_TEXT_FORMAT
    out["state"] = "rebuild_needed" if old_text else "outdated" if pending else "complete"
    msg = f"{len(docs)} manuals, {out['pages']} pages indexed"
    if old_text:
        msg += (f"; in use as it is, a REBUILD is due — page text format {fmt}, the builder writes "
                f"{MANUALS_TEXT_FORMAT} (every manual is read again)")
    if pending:
        msg += (f"; in use as it is, OUTDATED — {len(new)} new, {len(changed)} changed, {len(gone)} removed PDF(s) "
                "not in it")
    if old_text or pending:
        msg += " — the update runs once approved: kb_rebuild(index='manuals'), or tools\\manuals_db.py update"
    if upd:
        out["updating"] = upd
        msg += (f"; an update is RUNNING ({upd['files_done']} of {upd['files_total']} files) — the index above stays "
                "in use until it is done" if upd["state"] == "running" else
                f"; an update was INTERRUPTED at {upd['files_done']} of {upd['files_total']} files — the index above "
                "is still the one in use")
    out["message"] = msg
    return out


def overview(kb: Path) -> dict[str, Any]:
    """Both indexes of a knowledge folder, as the server reports them at each connection."""
    i = index_state(L.locate(kb, "infosys"))
    try:
        m = manuals_state(L.locate(kb, "manuals"), kb / "manuals")
    except sqlite3.Error as exc:
        m = {"state": "error", "message": f"manuals database not readable: {exc}"}
    e = ESI.state(L.index_file(kb, "esi"), kb)
    return {"knowledge_base": str(kb), "infosys": i, "manuals": m, "esi": e,
            "summary": f"Infosys index: {i['message']}. Manuals: {m['message']}. ESI index: {e['message']}."}


class Ctx:
    def __init__(self, kb: Path, kb_source: str, infosys_db: Path | None = None):
        self.kb, self.kb_source = kb, kb_source
        self.infosys_db = infosys_db               # an explicit index file (the builder's --db)
        self._con: dict[str, sqlite3.Connection] = {}
        self._src: dict[str, dict[str, Any]] = {}

    def path(self, which: str) -> Path:
        if which == "infosys" and self.infosys_db is not None:
            return self.infosys_db
        return L.locate(self.kb, "infosys" if which == "infosys" else "manuals")

    def con(self, which: str) -> sqlite3.Connection:
        if which in self._con:
            return self._con[which]
        p = self.path(which)
        build = BUILD_INFOSYS if which == "infosys" else BUILD_MANUALS
        if not p.is_file():
            more = ""
            if which == "infosys":
                more = " — " + IB.state(p)["message"]          # the first build may be running
            raise Err("no_index", f"no {'Infosys' if which == 'infosys' else 'manuals'} index at {p} "
                      f"(knowledge base {self.kb}, from {self.kb_source}){more}",
                      hint=f"build it: {build}; another knowledge folder: --kb PATH "
                           "(the twincat server's kb_status shows the one it uses)")
        con = sqlite3.connect(ro_uri(p), uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        fts = "pages_fts" if which == "infosys" else "fts"
        try:   # N65: the index's own term statistics (in memory, temp schema) — before the connection turns query-only
            con.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_vocab USING fts5vocab(main, '{fts}', 'row')")
        except sqlite3.Error:
            pass
        con.execute("PRAGMA query_only = 1")
        con.create_function("fnm", 2, lambda s, pat: int(fnmatch.fnmatch((s or "").lower(), (pat or "").lower())),
                            deterministic=True)
        need = {"meta", "pages", "pages_fts"} if which == "infosys" else {"doc", "page", "toc", "product", "fts"}
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        if not need <= have:
            raise Err("no_index", f"{p} is not a{'n Infosys' if which == 'infosys' else ' manuals_db.py'} index "
                      f"(tables missing: {sorted(need - have)})", hint=f"rebuild it: {build}")
        self._con[which] = con
        return con

    def source(self, which: str) -> dict[str, Any]:
        """What every answer carries: the index it came from — and, when that index is not the current one, what it
        is (rule 6: an index of an older format or source is used as it is, and said so)."""
        if which in self._src:
            return self._src[which]
        con = self.con(which)
        if which == "infosys":
            meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
            src: dict[str, Any] = {"database": str(self.path(which)),
                                   "package": Path(meta["package"]).name if meta.get("package") else None,
                                   "package_date": meta.get("package_date"), "indexed": meta.get("built"),
                                   "lcid": meta.get("lcid") or "1033"}
            st = index_state(self.path(which))
        else:
            n, pages = con.execute("SELECT COUNT(*), COALESCE(SUM(pages), 0) FROM doc").fetchone()
            src = {"database": str(self.path(which)), "manuals": n, "pages": pages}
            st = manuals_state(self.path(which), self.kb / "manuals")
        src["state"] = st["state"]
        if (IB.problem(st) if which == "infosys" else st["state"] != "complete" or st.get("updating")):
            src["state_note"] = st["message"]          # the answer comes from this index — say what it is
        self._src[which] = src
        return src

    def lcid(self) -> str:
        return str(self.source("infosys").get("lcid") or "1033")


# ---------------------------------------------------------------------------------------------- shared helpers
_WS = re.compile(r"\s+")
_MSXHELP = re.compile(r"ms-xhelp:/*\?id=([^&#\s]+)", re.I)
# an Infosys page URL: /content/<lcid>/<book>/<page id>.htm[l] — the page id is the page's file name ('30866571',
# 'index', '6778772363_30967009'), as twincat_mcp.infosys.book_of reads it
_URL = re.compile(r"/content/\d+/([^/?#\s]+)/([^/?#.\s]+)\.html?", re.I)
_HEAD = re.compile(r"(?m)^(#{1,4}) (.*)$")
_TYPES = (r"(?:BOOL|BYTE|[DL]?WORD|U?[SDL]?INT|L?REAL|L?TIME|DATE|TOD|DT|W?STRING|PVOID|AXIS_REF|REFERENCE|POINTER|"
          r"T_[A-Za-z]|ST_[A-Za-z]|E_[A-Za-z]|MC_[A-Za-z]|I_[A-Za-z])")
_FBD_PAIR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s" + _TYPES)


def one_line(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


query_text = DS.query_text
has_words = DS.has_words
natural = DS.natural


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# searching: the plugin's one implementation (twincat_mcp.docsearch — the server's infosys_search ranks with it too)
_fold = DS.fold
STOP_WORDS = DS.STOP_WORDS
COMMON_SHARE = DS.COMMON_SHARE
matched_words = DS.matched_words


def _err(exc: "DS.SearchError") -> Err:
    return Err(exc.code, exc.message, **exc.extra)


def search_tiers(*a: Any, **kw: Any) -> tuple[list[sqlite3.Row], int, str, str, dict[str, Any]]:
    try:
        return DS.search_tiers(*a, **kw)
    except DS.SearchError as exc:
        raise _err(exc) from None


def run_fts(con: sqlite3.Connection, sql: str, query: str, extra: list[Any]) -> tuple[list[sqlite3.Row], str, str]:
    try:
        return DS.run_fts(con, sql, query, extra)
    except DS.SearchError as exc:
        raise _err(exc) from None


# ---------------------------------------------------------------------------------------------- Infosys
# the plugin's index: pages(url, book, page_id, title, version, crumb, markdown, kind, library, …), pages_fts
# (title, text, code — external content), helpids(help_id, url). A page row as the commands use it:
_PAGE = ("SELECT rowid AS rid, url, book, page_id AS pid, title, version, crumb, markdown AS body, kind, library, decl "
         "FROM pages")
_LINK_MD = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


def _books(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT book FROM pages ORDER BY book")]


def _check_book_glob(con: sqlite3.Connection, pattern: str | None) -> None:
    if not pattern:
        return
    books = _books(con)
    if not any(fnmatch.fnmatch(b.lower(), pattern.lower()) for b in books):
        near = difflib.get_close_matches(pattern.lower().strip("*?"), books, n=6, cutoff=0.5)
        raise Err("not_found", f"no book matches {pattern!r} (book is a glob on the book slug, e.g. tcplclib_tc2_system "
                  "or tcplclib_tc2_*)", near=near, hint="docs_books filter=... lists the books")


def _cand(ctx: Ctx, r: sqlite3.Row) -> dict[str, Any]:
    return {"ref": f"{r['book']}/{r['pid']}", "book": r["book"], "title": r["title"], "version": r["version"] or None}


def _order(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    # library pages (tcplclib_*) first, then the shortest book name
    return sorted(rows, key=lambda c: (not c["book"].startswith("tcplclib_"), len(c["book"]), c["book"], c["pid"]))


def resolve(ctx: Ctx, ref: str, book: str | None, what: str = "ref") -> tuple[sqlite3.Row, str]:
    """Page for a reference: ms-xhelp id → Infosys URL → book/pid → bare pid → exact title (case-insensitive).
    -> (row, how it was resolved). Several pages → Err('ambiguous') with candidates (rule 3)."""
    con = ctx.con("infosys")
    s = query_text(ref).strip()
    if not s:
        raise Err("bad_param", f"{what} is empty")
    m = _MSXHELP.search(s)
    if m:
        hid = m.group(1).strip().lower()
        try:
            urls = [r[0] for r in con.execute("SELECT url FROM helpids WHERE help_id = ?", (hid,))]
        except sqlite3.OperationalError:
            urls = []
        rows = [r for u in urls for r in con.execute(f"{_PAGE} WHERE url = ?", (u,)).fetchall()]
        return _pick(ctx, rows, ref, None, "ms-xhelp id", what)
    m = _URL.search(s)
    if m:
        rows = con.execute(f"{_PAGE} WHERE book = ? AND page_id = ?", (m.group(1).lower(), m.group(2))).fetchall()
        return _pick(ctx, rows, ref, None, "Infosys URL", what)
    if "/" in s and not s.startswith("/"):
        b, p = s.split("/", 1)
        rows = con.execute(f"{_PAGE} WHERE book = ? AND page_id = ?", (b, p)).fetchall()
        if not rows:
            rows = con.execute(f"{_PAGE} WHERE book = ? COLLATE NOCASE AND page_id = ?", (b, p)).fetchall()
        if rows or p.isdigit():
            return _pick(ctx, rows, ref, None, "book/pageid", what)
        # not book/pageid: a title with a slash ('Send/Receive') — go on to the title lookup
    if s.isdigit():
        rows = [r for r in con.execute(f"{_PAGE} WHERE page_id = ?", (s,)).fetchall()
                if not book or fnmatch.fnmatch(r["book"].lower(), book.lower())]
        return _pick(ctx, rows, ref, book, "page id", what)
    _check_book_glob(con, book)
    keep = (lambda r: fnmatch.fnmatch(r["book"].lower(), book.lower())) if book else None
    # N100: the plugin's rule — a symbol is documented by its symbol pages (a C++ 'AdsRead' page is no ADSREAD), one
    # page in several books is one page; a page asked for by name: the symbol's page, else the pages of that title
    docs = (INF.symbol_documents if what == "symbol" else INF.title_documents)(con, s, keep)
    rows = [con.execute(f"{_PAGE} WHERE rowid = ?", (r["rid"],)).fetchone() for g in docs for r in g]
    return _pick(ctx, [r for r in rows if r is not None], ref, book, "symbol" if what == "symbol" else "title", what)


def _pick(ctx: Ctx, rows: list[sqlite3.Row], ref: str, book: str | None, how: str, what: str) -> tuple[sqlite3.Row, str]:
    # N100: the same page in several books (T_MaxString) is one answer; kb_verify I13: a member only below one symbol
    docs = INF.same_documents(rows, ctx.con("infosys"))
    if len(docs) == 1:
        return docs[0][0], how
    if not rows:
        con = ctx.con("infosys")
        near = []
        word = query_text(ref).strip()
        if how in ("title", "symbol") and len(word) >= 3 and has_words(word):
            # N35: the FTS title column, not a LIKE scan over every page body
            expr = "title : " + " ".join('"' + w.replace('"', '""') + '"*' for w in re.findall(r"[^\W_]+", word))
            try:
                hits = [r[0] for r in con.execute("SELECT rowid FROM pages_fts WHERE pages_fts MATCH ? LIMIT 200", (expr,))]
            except sqlite3.OperationalError:
                hits = []
            rows_near = [con.execute(f"{_PAGE} WHERE rowid = ?", (h,)).fetchone() for h in hits]
            near = [_cand(ctx, r) for r in _order([r for r in rows_near if r is not None])
                    if not book or fnmatch.fnmatch(r["book"].lower(), book.lower())][:8]
        raise Err("not_found", f"no page for {what} {ref!r}" + (f" in books {book!r}" if book else "") + f" (looked up as {how})",
                  near=near, hint=f"docs_search query={word!r} finds pages by their text")
    cands = []
    for g in docs:
        c = _cand(ctx, g[0])
        if len(g) > 1:
            c["same_page_in"] = [f"{x['book']}/{x['pid']}" for x in g[1:]]
        cands.append(c)
    raise Err("ambiguous", f"{len(docs)} pages match {what} {ref!r} ({how}; one page in several books counts once); "
              "nothing was chosen — pick the library the project uses (proj_libraries) and call again with book=<book> "
              "or ref=<ref>, and say which one you used", candidates=cands[:20])


def _also_in(con: sqlite3.Connection, r: sqlite3.Row) -> list[dict[str, Any]]:
    """The other pages of this title or symbol; same_document: the same page (another book) or the same library's same
    declaration (N100)."""
    rows = {x["rid"]: x for x in con.execute(f"{_PAGE} WHERE title = ? COLLATE NOCASE", (r["title"],))}
    for g in INF.symbol_documents(con, INF.symbol_name(r["title"]) or r["title"]):
        for x in g:
            rows.setdefault(x["rid"], x)
    rows.setdefault(r["rid"], r)
    mine = next(g for g in INF.same_documents(list(rows.values()), con) if any(x["rid"] == r["rid"] for x in g))
    same = {x["rid"] for x in mine}
    return [{"ref": f"{x['book']}/{x['pid']}", "book": x["book"], "version": x["version"] or None,
             **({"same_document": True} if x["rid"] in same else {})}
            for x in _order([x for x in rows.values() if x["rid"] != r["rid"]])]


def _links(con: sqlite3.Connection, text: str, limit: int = 30) -> list[dict[str, Any]]:
    """The links in this text that lead to a page of the index, as refs docs_page opens (the stored text holds every
    link as a page URL — resolved when the index was built)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in _LINK_MD.finditer(text or ""):
        u = _URL.search(m.group(2))
        if not u:
            continue
        ref = f"{u.group(1).lower()}/{u.group(2)}"
        if ref in seen:
            continue
        seen.add(ref)
        if con.execute("SELECT 1 FROM pages WHERE book = ? AND page_id = ?", (u.group(1).lower(), u.group(2))).fetchone():
            out.append({"text": m.group(1), "ref": ref})
            if len(out) >= limit:
                break
    return out


def _page_head(ctx: Ctx, r: sqlite3.Row) -> dict[str, Any]:
    return {"ref": f"{r['book']}/{r['pid']}", "title": r["title"], "book": r["book"], "version": r["version"] or None,
            "url": WEB.format(lcid=ctx.lcid(), book=r["book"], pid=r["pid"])}


def cmd_docs_search(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("infosys")
    _check_book_glob(con, a["book"])
    try:   # the plugin's one search of this index (infosys_search answers with it too): N99, N105
        res = INF.search_index(con, a["query"], a["limit"], where="fnm(p.book, ?)" if a["book"] else "",
                               extra=[a["book"]] if a["book"] else [])
    except DS.SearchError as exc:
        raise _err(exc) from None
    rows, total, mode, info = res["rows"], res["total"], res["mode"], res["info"]
    items = []
    for r in rows:
        it = {"ref": f"{r['book']}/{r['pid']}", "title": r["title"], "book": r["book"],
              "version": r["version"] or None, "excerpt": one_line(r["s"]),
              "url": WEB.format(lcid=ctx.lcid(), book=r["book"], pid=r["pid"]), "score": float(f"{-r['r']:.4g}")}
        if r["rid"] in res["copies"]:
            it["same_page_in"] = [f"{b}/{pid}" for b, pid in res["copies"][r["rid"]]]
        if mode.startswith("some words"):
            it["matched"] = matched_words(con, r["rid"], info.get("words_used") or [], fts="pages_fts")
        items.append(it)
    # total: the pages that match (a page in several books once per book; copies are one item, same_page_in)
    out: dict[str, Any] = {"query": a["query"], "query_mode": mode, **info, "count": len(items), "total": total,
                           "truncated": res["more"], "items": items, "source": ctx.source("infosys")}
    if res["code"]:
        out["code"] = {k: res["code"][k] for k in ("value", "hex", "dec")}
        out["code_note"] = INF.code_note(res)
    if not items:
        out["hint"] = ("no page contains these words" + (f" in books {a['book']!r}" if a["book"] else "")
                       + " — try fewer or other words, OR, prefix*")
    return out


def _sections(body: str) -> list[tuple[str, str]]:
    """[(heading text, block text incl. the heading line)]; text before the first heading has heading ''. A '# '
    line inside a ``` block is code, not a heading."""
    out: list[tuple[str, list[str]]] = [("", [])]
    fence = False
    for ln in body.splitlines(keepends=True):
        if ln.startswith("```"):
            fence = not fence
        elif not fence and re.match(r"#{1,4} ", ln):
            out.append((ln.lstrip("#").strip(), []))
        out[-1][1].append(ln)
    return [(h, "".join(b)) for h, b in out if h or "".join(b).strip()]


def _intro(body: str) -> str:
    first = body.split("```")[0]
    keep = []
    for ln in first.splitlines():
        s = one_line(ln)
        if not s or s.startswith("#"):
            continue
        if "↔" in s or (len(_FBD_PAIR.findall(s)) >= 3 and ". " not in s):
            continue                                    # the run-together label line of the FBD graphic
        keep.append(s)
    t = " ".join(keep)
    return t[:600] + ("…" if len(t) > 600 else "")


def _parsed_declaration(r: sqlite3.Row) -> dict[str, Any]:
    """N103b: what the index stored for the page when it was built (the plugin's parse_declaration — the same answer
    infosys_declaration gives): the kind, the return type of a function / method / property, and the declaration
    with its header ('FUNCTION MEMCPY : UDINT' — Beckhoff often writes the header as a heading, so the page's code
    blocks alone never show the return type)."""
    try:
        d = json.loads(r["decl"]) if r["decl"] else None
    except ValueError:
        d = None
    if not isinstance(d, dict):
        return {"kind": r["kind"], "declaration_note": "the index holds no parsed declaration for this page — the "
                                                       "kind is the page's, the return type is not known"}
    out: dict[str, Any] = {"kind": d.get("kind") or r["kind"]}
    if out["kind"] in ("Function", "Method", "Property"):
        out["return_type"] = d.get("return_type")
        if out["return_type"] is None:
            out["return_type_note"] = "the page names no return type (no header, no return-value table) — not known"
    # N103b narrowed (0.8.13 retest): only kind / return type / extends / implements / declaration were passed — the
    # members (inputs, outputs, methods, enum values …) the index stores, and infosys_declaration gives, were not
    for k in ("kind_note", "extends", "implements", "declaration", "inputs", "outputs", "in_out", "members",
              "enum_values", "methods", "properties", "description"):
        if d.get(k):
            out[k] = d[k]
    return out


def _family(con: sqlite3.Connection, r: sqlite3.Row) -> dict[str, Any]:
    """N126: the page's parent and child pages in the Help Viewer contents (Infosys format 6), as refs docs_page
    opens — an FB's page lists its method and property pages, a method page names its FB."""
    fam = INF.page_family(con, r["url"])

    def ref(u: str) -> str:
        b = INF.book_of(u)
        return f"{b[0]}/{b[1]}" if b else u
    out: dict[str, Any] = {}
    if fam.get("parent"):
        out["parent"] = {"title": fam["parent"]["title"], "ref": ref(fam["parent"]["url"])}
    if fam.get("children"):
        out["children"] = [{"title": c["title"], "ref": ref(c["url"]), "kind": c["kind"]} for c in fam["children"]]
        if fam.get("children_count"):
            out["children_count"] = fam["children_count"]
    return out


def cmd_docs_declaration(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("infosys")
    r, how = resolve(ctx, a["symbol"], a["book"], what="symbol")
    body = r["body"] or ""
    blocks = [b.strip("\n") for b in re.findall(r"```[^\n`]*\n(.*?)\n```", body, re.S)]
    shown, used, rest = [], 0, 0
    for b in blocks:
        if used + len(b) > 12000 and shown:
            rest += 1
            continue
        shown.append(b)
        used += len(b)
    req = re.search(r"(?ms)^#{1,4} (?:Requirements|Prerequisites|Voraussetzungen)\s*\n(.*?)(?=^#{1,4} |\Z)", body)
    out: dict[str, Any] = _page_head(ctx, r)
    out.update({"resolved_by": how, **_parsed_declaration(r), "intro": _intro(body), "declaration_blocks": shown,
                "requirements": req.group(1).strip() if req else None, "also_in": _also_in(con, r)})
    fam = _family(con, r)
    if fam.get("parent"):
        out["parent"] = fam["parent"]
    lk = _links(con, body)
    if lk:
        out["links"] = lk
    if rest:
        out["blocks_not_shown"] = f"{rest} more code block(s) — docs_page ref={out['ref']} for the whole page"
    if not blocks:
        out["note"] = f"the page has no code block — docs_page ref={out['ref']} for its text"
    out["source"] = ctx.source("infosys")
    return out


def cmd_docs_page(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("infosys")
    r, how = resolve(ctx, a["ref"], a["book"])
    body = r["body"] or ""
    secs = _sections(body)
    outline = [h for h, _ in secs if h]
    text, section = body, None
    if a["section"]:
        want = a["section"].lower()
        on, keep = False, []
        for h, block in secs:
            if h:
                on = want in h.lower()
            if on:
                keep.append(block)
        if not keep:
            raise Err("not_found", f"no section heading contains {a['section']!r}", outline=outline,
                      hint="pick one of the outline headings, or leave section out")
        text, section = "".join(keep).strip(), a["section"]
    n = len(text)
    off, mx = a["offset"], a["max_chars"]
    if off and off >= n:
        raise Err("bad_param", f"offset {off} is past the end of the text ({n} chars)")
    part = text[off:off + mx]
    nxt = off + mx if off + mx < n else None
    out = _page_head(ctx, r)
    out.update({"resolved_by": how, "breadcrumb": r["crumb"] or None, "outline": outline, "section": section,
                "text": part, "chars": n, "offset": off, "next_offset": nxt, "truncated": nxt is not None})
    lk = _links(con, part)
    if lk:
        out["links"] = lk
    also = _also_in(con, r)
    if also:
        out["also_in"] = also
    out.update(_family(con, r))
    out["source"] = ctx.source("infosys")
    return out


def cmd_docs_books(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("infosys")
    books = [{"book": r["book"], "pages": r["n"]}
             for r in con.execute("SELECT book, COUNT(*) AS n FROM pages GROUP BY book ORDER BY book")
             if not a["filter"] or fnmatch.fnmatch(r["book"].lower(), a["filter"].lower())]
    out: dict[str, Any] = {"count": len(books), "books": books, "source": ctx.source("infosys")}
    if a["filter"] and not books:
        out["hint"] = "filter is a glob on the book slug, e.g. tcplclib_tc2_* or *ethercat*"
    return out


# ---------------------------------------------------------------------------------------------- manuals
def _docs(con: sqlite3.Connection, pattern: str | None) -> list[sqlite3.Row]:
    """manuals_db.py's matching: numeric id, glob (name or title), else a plain word = *word*."""
    rows = con.execute("SELECT * FROM doc ORDER BY name").fetchall()
    if not pattern:
        return rows
    pattern = pattern.strip()
    if pattern.isdigit():
        return [r for r in rows if r["id"] == int(pattern)]
    pat = pattern.lower() if any(c in pattern for c in "*?[") else f"*{pattern.lower()}*"
    return [r for r in rows if fnmatch.fnmatch(r["name"].lower(), pat) or fnmatch.fnmatch((r["title"] or "").lower(), pat)]


def _one_doc(con: sqlite3.Connection, pattern: str) -> sqlite3.Row:
    docs = _docs(con, pattern)
    if len(docs) == 1:
        return docs[0]
    if not docs:
        names = [r["name"] for r in con.execute("SELECT name FROM doc ORDER BY name")]
        raise Err("not_found", f"no indexed manual matches {pattern!r}",
                  near=difflib.get_close_matches(pattern, names, n=6, cutoff=0.3) or names[:10],
                  hint="manual_docs lists the manuals; a missing one: " + BUILD_MANUALS)
    raise Err("ambiguous", f"{pattern!r} matches {len(docs)} manuals — pass the file name or the id",
              candidates=[{"id": d["id"], "doc": d["name"], "title": d["title"]} for d in docs[:20]])


def _msource(ctx: Ctx) -> dict[str, Any]:
    return ctx.source("manuals")


# N164 / N165 (0.8.14 retest): terms of a manual query that the index cannot hold as written. Its tokenizer (unicode61)
# keeps letters and digits only, so every notation of one hex number is another term — '0x8000' is the token 0x8000,
# '#x8000' the token x8000, '16#8000' and the sub-index row '8000:11' the tokens 16 8000 / 8000 11 — and one notation
# never found the others ('8000:11' 1 hit, '0x8000:11' 4); the degree sign is no character of the index at all, so
# '°C' was the letter C (C++, C:\ first). Such a term is searched as every form the index holds (fts, ANDed with the
# query's other words in each step of the search) and the page text must hold the term itself in one of its
# notations (rx, checked on the stored text): the term is found, not its tokens. A query written in FTS5 syntax
# (quotes, operators, prefix*, parentheses) runs as written.
_FTS_WRITTEN = re.compile(r'["*()^]|\b(?:AND|OR|NOT|NEAR)\b')
_HEX_TERM = re.compile(r"(?<![\w#])(?:0x|#x|16#)([0-9a-f]{1,8})(?::(?:0x)?(0*[0-9a-f]{1,2}))?(?![\w#:])", re.I)
_SUB_TERM = re.compile(r"(?<![\w#:])([0-9a-f]{4}):(?:0x)?(0*[0-9a-f]{1,2})(?![\w#:])", re.I)
_MICRO = "\u00b5\u03bc"             # micro sign, Greek mu — PDFs use either
_OHM = "\u03a9\u2126"               # Greek capital omega, ohm sign
_DEG_C, _DEG_F = "\u2103", "\u2109"  # the one-character degree Celsius / Fahrenheit signs
_PREFIX = f"[pn{_MICRO}mkMG]"
_UNIT_ONE = f"{_PREFIX}?(?:Hz|Wh|VA|bar|Pa|min|V|A|W|[{_OHM}]|s|m|g|K|h)"
# a unit symbol: degrees, a unit per unit (mV/V), a prefixed unit (mA, µs, kΩ, MHz), Hz / Ω — a plain letter (V, A,
# s, m) is a word of the query, not a unit; digits may stand right before it (60°C), letters may not
_UNIT_TERM = re.compile(rf"(?<![^\W\d_])(?:°\s?[CF]|{_DEG_C}|{_DEG_F}|{_UNIT_ONE}/{_UNIT_ONE}"
                        rf"|{_PREFIX}(?:Hz|Wh|VA|bar|Pa|V|A|W|[{_OHM}]|s)|Hz|[{_OHM}])(?![^\W\d_])")


def _hex_term(text: str, digits: str, sub: str | None) -> dict[str, Any]:
    d = digits.lower()
    idx = len(d) == 4            # a CoE index: its sub-index rows are written without a prefix ('8000:11')
    alts = [f'"0x{d}"', f'"x{d}"'] + ([f'"{d}"'] if idx else [f'"16 {d}"'])
    pre = rf"(?<![0-9a-z])(?:0x|#x|16#){d}"
    D = d.upper()
    if sub is None:
        rx = pre + r"(?![0-9a-z])"
        forms = [f"0x{D}", f"#x{D}", f"16#{D}"]
        if idx:
            rx += rf"|(?<![0-9a-z#:]){d}:(?:0x)?[0-9a-f]{{1,2}}(?![0-9a-z])"
            forms.append(f"{D}:<sub-index>")
    else:
        s = int(sub, 16)
        tail = rf":(?:0x)?0*{s:x}(?![0-9a-z])"
        rx = (rf"(?:{pre}|(?<![0-9a-z#:]){d})" if idx else pre) + tail
        S = f"{s:02X}"
        forms = [f"0x{D}:{S}", f"#x{D}:{S}", f"16#{D}:{S}"] + ([f"{D}:{S}"] if idx else [])
    return {"term": text, "fts": "(" + " OR ".join(alts) + ")", "rx": "(?i)" + rx,
            "searched_as": f"{text} in any notation: {', '.join(forms)}"}


def _unit_term(text: str) -> dict[str, Any]:
    u = re.sub(r"\s+", "", text).replace(_DEG_C, "°C").replace(_DEG_F, "°F")
    parts = []
    for ch in u:
        parts.append(f"[{_MICRO}]" if ch in _MICRO else f"[{_OHM}]" if ch in _OHM else r"\s?/\s?" if ch == "/"
                     else r"°\s?" if ch == "°" else re.escape(ch))
    core = "".join(parts)
    if u in ("°C", "°F"):
        core = f"(?:{core}|{_DEG_C if u == '°C' else _DEG_F})"
    letters = " ".join(re.findall(r"[^\W_]+", u))
    return {"term": text, "fts": '"' + u + '"', "rx": rf"(?<![^\W\d_]){core}(?![^\W\d_])",
            "searched_as": f"the unit {u} as written on the page (the index keeps only its letters: {letters})"}


def _manual_terms(q: str) -> tuple[list[dict[str, Any]], str]:
    """-> (the hex numbers / CoE indexes and unit symbols of the query, in their order; the query without them)."""
    if _FTS_WRITTEN.search(q):
        return [], q
    found: list[tuple[int, dict[str, Any]]] = []
    for pat, make in ((_HEX_TERM, lambda m: _hex_term(m.group(0), m.group(1), m.group(2))),
                      (_SUB_TERM, lambda m: _hex_term(m.group(0), m.group(1), m.group(2))),
                      (_UNIT_TERM, lambda m: _unit_term(m.group(0)))):
        def take(m: re.Match, make=make) -> str:
            found.append((m.start(), make(m)))
            return " " * len(m.group(0))           # the places of the other terms stay where they are
        q = pat.sub(take, q)
    return [t for _pos, t in sorted(found, key=lambda x: x[0])], " ".join(q.split())


def _holds(text: str | None, rx: str) -> int:
    return 1 if _compiled(rx).search(text or "") else 0


_RX_CACHE: dict[str, re.Pattern] = {}


def _compiled(rx: str) -> re.Pattern:
    if rx not in _RX_CACHE:
        _RX_CACHE[rx] = re.compile(rx)
    return _RX_CACHE[rx]


def cmd_manual_search(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("manuals")
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM doc")}
    where, extra = "fts MATCH ?", []
    if a["doc"]:
        ids = [d["id"] for d in _docs(con, a["doc"])]
        if not ids:
            _one_doc(con, a["doc"])                    # raises not_found with the list
        where += f" AND doc IN ({','.join('?' * len(ids))})"
        extra = ids
    terms, rest = _manual_terms(query_text(a["query"]))
    if terms:                                          # N164 / N165: the page text holds each term as written
        con.create_function("manual_holds", 2, _holds, deterministic=True)
        for t in terms:
            where += " AND manual_holds(fts.text, ?)"
            extra = extra + [t["rx"]]
    exact = " AND ".join(t["fts"] for t in terms)
    # N167 (0.8.14 retest): paging — the hits up to offset + limit are read, the page of them is shown (the rowid
    # makes the order of equal scores fixed, so pages neither repeat nor skip a hit)
    off, fetch = a["offset"], a["offset"] + a["limit"]

    def sql(w: str) -> tuple[str, str]:
        return ("SELECT fts.rowid AS rid, doc, page, section, snippet(fts, 0, '[', ']', '…', 16) AS s, "
                f"bm25(fts, 1.0, 4.0) AS r FROM fts WHERE {w} ORDER BY r, fts.rowid LIMIT ?",
                f"SELECT COUNT(*) FROM fts WHERE {w}")
    if terms and not has_words(rest):                  # nothing but such terms
        s, c = sql(where)
        try:
            rows = con.execute(s, [exact, *extra, fetch]).fetchall()
            total = con.execute(c, [exact, *extra]).fetchone()[0]
        except sqlite3.OperationalError as exc:
            raise Err("internal", f"search of {[t['term'] for t in terms]} failed: {exc}") from None
        mode, info = "exact terms", {"words_used": [t["term"] for t in terms]}
    else:
        if terms:                                      # the other words fall back in steps; the terms stay in each
            where += " AND fts.rowid IN (SELECT rowid FROM fts WHERE fts MATCH ?)"
            extra = extra + [exact]
        s, c = sql(where)
        rows, total, mode, _expr, info = search_tiers(con, s, c, rest if terms else a["query"], extra, fetch)
    if off and off >= total and total:
        raise Err("bad_param", f"offset {off} is past the last hit ({total} hits) — offset=0 for the first ones")
    rows = rows[off:off + a["limit"]]
    nxt = off + len(rows) if off + len(rows) < total else None
    items = []
    for r in rows:
        it = {"doc": names.get(r["doc"], r["doc"]), "doc_id": r["doc"], "page": r["page"], "section": r["section"] or None,
              "excerpt": one_line(r["s"]), "score": float(f"{-r['r']:.4g}")}
        if mode.startswith("some words"):
            it["matched"] = matched_words(con, r["rid"], info.get("words_used") or [])
        items.append(it)
    out: dict[str, Any] = {"query": a["query"], "query_mode": mode, **info}
    if terms:
        out["terms"] = [{"term": t["term"], "searched_as": t["searched_as"]} for t in terms]
    out.update({"count": len(items), "total": total, "offset": off, "next_offset": nxt, "truncated": nxt is not None,
                "items": items})
    for name, (asked, lo, hi) in (a.get("_clamped") or {}).items():
        out["clamped"] = True
        out["clamped_note"] = (f"{name} {asked} is above the maximum ({lo}-{hi}): {hi} used"
                               + (f" — the next hits: offset={nxt}" if nxt is not None else ""))
    out["source"] = _msource(ctx)
    if not items:
        out["hint"] = ("no indexed manual page contains these words"
                       + (" with " + ", ".join(t["term"] for t in terms) + " as written" if terms else "")
                       + "; a manual that is not indexed yet: " + BUILD_MANUALS)
    return out


def cmd_manual_product(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("manuals")
    code = a["code"].strip().upper()
    if not code:
        raise Err("bad_param", "code is empty (e.g. EL3102, CX9020)")
    def q(c: str) -> list[sqlite3.Row]:
        return con.execute("SELECT d.name, p.doc, GROUP_CONCAT(p.page) AS pages, SUM(p.n) AS hits FROM product p "
                           "JOIN doc d ON d.id = p.doc WHERE p.code = ? OR p.code LIKE ? ESCAPE '\\' GROUP BY p.doc "
                           "ORDER BY hits DESC LIMIT ?", (c, like_escape(c) + "-%", a["limit"])).fetchall()
    rows = q(code)
    out: dict[str, Any] = {"code": code}
    base = re.fullmatch(r"([A-Z]{1,4}\d{3,5})-\d{4}", code)
    if not rows and base:            # N67: a variant no manual names -> the base product the manual is written for
        rows = q(base.group(1))
        if rows:
            out["matched_as"] = f"base product {base.group(1)} (no manual names the variant {code})"
    items = [{"doc": r["name"], "doc_id": r["doc"], "mentions": r["hits"],
              "pages": sorted({int(x) for x in (r["pages"] or "").split(",") if x})} for r in rows]
    if not items:
        # N163 (0.8.14 retest): the product table is what the builder took for a product code — an index built by an
        # older builder lacks whole families (ED2008: 'copy the PDF manuals' although its manual was indexed). The
        # page text is the evidence: a code it names is answered from it, and said so; only a code on no indexed
        # page sends the user to add a manual
        items = _code_in_text(con, code, a["limit"])
        via = None
        if not items and base:
            items, via = _code_in_text(con, base.group(1), a["limit"]), base.group(1)
        if items:
            out["matched_as"] = "page text" + (f": base product {via} (no page names the variant {code})" if via else "")
            fmt = (con.execute("SELECT value FROM meta WHERE key = 'text_format'").fetchone() or ["1"])[0]
            out["note"] = (f"{via or code} is not in this index's product-code table, but the page text names it — "
                           + (f"the index was written with page text format {fmt} by an older builder, whose "
                              f"product-code rule missed it; the rebuild (format {MANUALS_TEXT_FORMAT}) lists it once "
                              "approved: kb_rebuild(index='manuals'), or tools\\manuals_db.py update"
                              if fmt != MANUALS_TEXT_FORMAT else
                              "the product-code rule (a family's letters and four digits) does not take this form"))
    out.update({"count": len(items), "items": items, "source": _msource(ctx)})
    if not items:
        out["hint"] = f"{code} is in no indexed manual — " + BUILD_MANUALS
    return out


def _code_in_text(con: sqlite3.Connection, code: str, limit: int) -> list[dict[str, Any]]:
    """The manuals whose page text names the product code (or a variant of it, CODE-dddd), most mentions first."""
    toks = re.findall(r"[^\W_]+", code)
    if not toks:
        return []
    rx = re.compile(r"(?<![A-Za-z0-9])" + re.escape(code) + (r"(?:-\d{4})?" if "-" not in code else "")
                    + r"(?![A-Za-z0-9])", re.I)
    per: dict[int, dict[int, int]] = {}
    for r in con.execute("SELECT doc, page, text FROM fts WHERE fts MATCH ?", ('"' + " ".join(toks) + '"',)):
        n = len(rx.findall(r["text"] or ""))
        if n:
            per.setdefault(r["doc"], {})[r["page"]] = n
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM doc")}
    items = [{"doc": names.get(d, d), "doc_id": d, "mentions": sum(pg.values()), "pages": sorted(pg)}
             for d, pg in per.items()]
    return sorted(items, key=lambda it: (-it["mentions"], it["doc"]))[:limit]


def cmd_manual_toc(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("manuals")
    d = _one_doc(con, a["doc"])
    toc = [{"level": r["level"], "title": r["title"], "page": r["page"]} for r in
           con.execute("SELECT level, title, page FROM toc WHERE doc = ? AND level <= ? ORDER BY rowid", (d["id"], a["depth"]))]
    out: dict[str, Any] = {"doc": d["name"], "doc_id": d["id"], "title": d["title"], "pages": d["pages"], "toc": toc,
                           "source": _msource(ctx)}
    if not toc:
        out["note"] = "this manual has no chapters (no bookmarks and no detectable headings)"
    return out


def cmd_manual_page(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", a["pages"])
    if not m:
        raise Err("bad_param", f"pages must be N or N-M (got {a['pages']!r})")
    lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
    if lo < 1 or hi < lo:
        raise Err("bad_param", f"pages {a['pages']!r}: N >= 1 and M >= N")
    if hi - lo + 1 > 21:
        raise Err("bad_param", f"pages {a['pages']!r} is {hi - lo + 1} pages; at most 21 per call")
    con = ctx.con("manuals")
    d = _one_doc(con, a["doc"])
    if hi > (d["pages"] or 0):
        raise Err("bad_param", f"{d['name']} has {d['pages']} pages (asked for {lo}-{hi})")
    # N169 (0.8.14 retest): offset = the characters of page N (the first page asked) already read. `next` repeated the
    # same call when the first page alone was longer than max_chars (pages=N-M again, cut at the same place); it
    # names the offset now, so the route a max_chars refusal names exists
    out_pages, used, cut, cut_at = [], 0, None, 0
    for r in con.execute("SELECT page, section, text FROM page WHERE doc = ? AND page BETWEEN ? AND ? ORDER BY page",
                         (d["id"], lo, hi)):
        t = r["text"] or ""
        start = a["offset"] if r["page"] == lo else 0
        if start and start >= len(t):
            raise Err("bad_param", f"offset {start} is past the end of page {lo} ({len(t)} chars)")
        t = t[start:]
        at = {"offset": start} if start else {}
        if used + len(t) > a["max_chars"]:
            t = t[:max(0, a["max_chars"] - used)]
            out_pages.append({"page": r["page"], "section": r["section"] or None, **at, "text": t, "cut": True})
            cut, cut_at = r["page"], start + len(t)
            break
        out_pages.append({"page": r["page"], "section": r["section"] or None, **at, "text": t})
        used += len(t)
    out: dict[str, Any] = {"doc": d["name"], "doc_id": d["id"], "title": d["title"], "path": d["path"],
                           "pages": out_pages, "truncated": cut is not None, "source": _msource(ctx)}
    if cut is not None:
        out["next"] = (f"cut at max_chars {a['max_chars']} on page {cut}: manual_page doc={d['name']} "
                       f"pages={cut}-{hi} offset={cut_at}")
        out["next_pages"], out["next_offset"] = f"{cut}-{hi}", cut_at
    return out


def cmd_manual_docs(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("manuals")
    rows = _docs(con, a["filter"])
    out: dict[str, Any] = {"count": len(rows), "manuals": [
        {"id": r["id"], "doc": r["name"], "title": r["title"], "pages": r["pages"], "text_pages": r["text_pages"],
         **({"no_text": "no text layer (scanned?)"} if not r["text_pages"] else {})} for r in rows],
        "source": _msource(ctx)}
    return out


# ---------------------------------------------------------------------------------------------- status / help
def cmd_status(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"knowledge_base": str(ctx.kb), "found_via": ctx.kb_source, "script_version": VERSION}
    ov = overview(ctx.kb)
    out["summary"] = ov["summary"]
    for which in ("infosys", "manuals"):
        out[which + "_state"] = ov[which]
        try:
            src = dict(ctx.source(which))
            con = ctx.con(which)
            if which == "infosys":
                src["pages"] = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
                src["books"] = con.execute("SELECT COUNT(DISTINCT book) FROM pages").fetchone()[0]
            else:
                src["text_pages"] = con.execute("SELECT COALESCE(SUM(text_pages), 0) FROM doc").fetchone()[0]
                src["product_codes"] = con.execute("SELECT COUNT(DISTINCT code) FROM product").fetchone()[0]
            src["size_mb"] = round(ctx.path(which).stat().st_size / 1e6, 1)
            out[which] = src
        except Err as exc:
            out[which] = {"missing": exc.message, **exc.extra}
    out["esi_state"] = ov["esi"]
    out["rules"] = [kbtext(r) for r in RULES]
    return out


# ---------------------------------------------------------------------------------------------- hw_* (the ESI index)
def _esi(ctx: Ctx, fn: str, *a: Any) -> dict[str, Any]:
    """One read of index\\esi.db through the plugin's esi_index (the server's hw_* tools read it the same way), with
    the index it came from — and its state when it is not current (rule 6)."""
    db = L.index_file(ctx.kb, "esi")
    st = ESI.state(db, ctx.kb)
    if st["state"] == "missing":
        raise Err("no_index", f"no ESI index at {db} — {st['message']}",
                  hint=f'build it: <Python 3.12+> "{BUILDERS / "esi_db.py"}" update --kb "<kb>" (or approve '
                       "kb_rebuild(index='esi') in the twincat server; its first build starts by itself there)")
    try:
        out = getattr(ESI.EsiIndex(db), fn)(*a)
    except ESI.EsiError as exc:
        raise Err(exc.code, str(exc)) from None
    src: dict[str, Any] = {"database": str(db), "state": st["state"], "built": st.get("built")}
    if st["state"] != "complete" or st.get("rebuild"):
        src["state_note"] = st["message"]
    out["source"] = src
    return out


def cmd_hw_lookup(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    if a["detail"] not in ESI.DETAILS:
        raise Err("bad_param", f"detail must be one of {list(ESI.DETAILS)} (got {a['detail']!r})")
    return _esi(ctx, "lookup", a["query"], a["limit"], a["revision"], a["detail"])


def cmd_hw_search(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    kinds = [k.strip() for k in a["kinds"].split(",") if k.strip()] if a["kinds"] else None
    return _esi(ctx, "search", a["query"], kinds, a["limit"])


def cmd_hw_object(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    return _esi(ctx, "object", a["device"], a["index"], a["revision"])


def cmd_hw_diag(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    return _esi(ctx, "diag", a["code"], a["device"], a["revision"])


def cmd_hw_module(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    return _esi(ctx, "module", a["ident"], a["device"], a["revision"])


COMMANDS: dict[str, tuple[Callable[[Ctx, dict[str, Any]], dict[str, Any]], dict[str, P], str]] = {
    "docs_search": (cmd_docs_search, {"query": P(required=True, doc="FTS5 query"), "book": P(doc="glob on the book slug"),
                                      "limit": P("int", default=10, lo=1, hi=50)},
                    "Infosys full text: ranked pages with ref, title, book, version, excerpt, url"),
    "docs_declaration": (cmd_docs_declaration, {"symbol": P(required=True, doc="FB/function/struct/enum name or a ref"),
                                                "book": P(doc="glob on the book slug")},
                         "a library symbol's page: intro, every declaration block, requirements, also_in"),
    "docs_page": (cmd_docs_page, {"ref": P(required=True, doc="book/pageid, Infosys URL, page id, exact title or ms-xhelp link"),
                                  "book": P(doc="glob, narrows a title or page id"), "section": P(doc="heading text"),
                                  "offset": P("int", default=0, lo=0), "max_chars": P("int", default=4000, lo=200, hi=20000)},
                  "a page as text: outline, one section, paging with next_offset"),
    "docs_books": (cmd_docs_books, {"filter": P(doc="glob on the book slug")}, "Infosys books with page counts"),
    # N167 / N169 (0.8.14 retest): the search pages (offset / next_offset) and clamps its limit; each refusal of a
    # range names the way to read on
    "manual_search": (cmd_manual_search, {"query": P(required=True, doc="FTS5 query; 0x8000:11 / #x8000 / °C / mV/V are "
                                                                        "searched in any notation, as written"),
                                          "doc": P(doc="manual: file name, glob or id"),
                                          "limit": P("int", default=10, lo=1, hi=50, clamp=True, doc="hits shown"),
                                          "offset": P("int", default=0, lo=0, doc="hits skipped: the next ones are "
                                                                                   "offset=<next_offset>")},
                      "PDF manuals full text: doc, page, section, excerpt; paging with next_offset"),
    "manual_product": (cmd_manual_product, {"code": P(required=True, doc="product code, e.g. EL3102 (also EL3102-xxxx)"),
                                            "limit": P("int", default=20, lo=1, hi=50, doc="manuals shown")},
                       "which manuals mention a product code, on which pages"),
    "manual_toc": (cmd_manual_toc, {"doc": P(required=True, doc="file name, glob or id"),
                                    "depth": P("int", default=2, lo=1, hi=6, doc="bookmark levels")},
                   "a manual's chapters with page numbers"),
    "manual_page": (cmd_manual_page, {"doc": P(required=True, doc="file name, glob or id"), "pages": P(required=True, doc="N or N-M (<= 21 pages)"),
                                      "max_chars": P("int", default=4000, lo=200, hi=20000, doc="characters shown",
                                                     route="read a long text in parts: pages=N-M offset=<next_offset> "
                                                           "as `next` names them"),
                                      "offset": P("int", default=0, lo=0, doc="characters of page N already read: "
                                                                               "offset=<next_offset> with pages=<next_pages>")},
                    "page text of a manual with the section of each page; paging with next_pages / next_offset"),
    "manual_docs": (cmd_manual_docs, {"filter": P(doc="file name / title glob or word")}, "the indexed manuals"),
    "hw_lookup": (cmd_hw_lookup, {"query": P(required=True, doc="type / order code, part of a name, or product code #x…"),
                                  "limit": P("int", default=3, lo=1, hi=30), "revision": P(doc="#x… as shown, or a number"),
                                  "detail": P(default="newest", doc="newest | summary | full")},
                  "EtherCAT device from the ESI index: identity, PDOs and entries, PDO assignments, DC, slots, CoE list"),
    "hw_search": (cmd_hw_search, {"query": P(required=True, doc="FTS5 query"),
                                  "kinds": P(doc="comma list of device, object, diag, module"),
                                  "limit": P("int", default=20, lo=1, hi=100)},
                  "ESI full text: devices, CoE objects, diagnosis messages, modules"),
    "hw_object": (cmd_hw_object, {"device": P(required=True, doc="exact type, e.g. EL3356-0010"),
                                  "index": P(required=True, doc="#x8000 / 0x8000 / 8000"), "revision": P(doc="#x…")},
                  "one CoE object: sub-indices, types, bit offsets, defaults, limits, enum texts"),
    "hw_diag": (cmd_hw_diag, {"code": P(required=True, doc="text id, e.g. #x4101"), "device": P(doc="exact type"),
                              "revision": P(doc="#x…")},
                "a diagnosis message (diagnosis history text id): text, causes, remedies, reaction"),
    "hw_module": (cmd_hw_module, {"ident": P(required=True, doc="ModuleIdent #x…"), "device": P(doc="exact type"),
                                  "revision": P(doc="#x…")},
                  "a module of a modular device by its ModuleIdent"),
    "status": (cmd_status, {}, "where the knowledge base is, what is indexed, the rules"),
}


def cmd_help() -> dict[str, Any]:
    return {"usage": "python kb_docs.py <command> key=value ... [--kb PATH]  (quote values with spaces)",
            "commands": {name: {"does": doc, "parameters": {k: p.describe() for k, p in spec.items()}}
                         for name, (_fn, spec, doc) in COMMANDS.items()},
            "errors": ["ambiguous", "not_found", "bad_param", "bad_query", "empty_query", "no_index", "unknown_command",
                       "internal"],
            "rules": RULES}


def main(argv: list[str] | None = None) -> int:
    t0 = time.perf_counter()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    code = 0
    try:
        cmd, kv, kb = parse_argv(list(sys.argv[1:] if argv is None else argv))
        if cmd in (None, "help", "-h", "--help"):
            out = cmd_help()
        elif cmd not in COMMANDS:
            near = difflib.get_close_matches(cmd, list(COMMANDS), n=3, cutoff=0.4)
            why = ""
            if cmd.startswith("ads_"):
                why = (" (ads_* are tools of the twincat MCP server: they read the running PLC and never return "
                       "documentation)")
            elif cmd.startswith(("infosys_", "kb_")):
                why = " (that is a tool of the twincat MCP server, not a command of this script)"
            raise Err("unknown_command", f"{cmd!r} is not a command of this script{why}", commands=list(COMMANDS),
                      near=near)
        else:
            fn, spec, _doc = COMMANDS[cmd]
            args = bind(spec, kv)
            root, how = kb_root(kb)
            global _KB_SHOWN
            _KB_SHOWN = str(root)
            out = fn(Ctx(root, how), args)
            for k in ("hint", "note", "next"):
                if isinstance(out.get(k), str):
                    out[k] = kbtext(out[k])
    except Err as exc:
        out = {"error": exc.code, "message": exc.message or exc.code, **exc.extra}
        code = 1
    except Exception as exc:  # noqa: BLE001 — never an empty message
        out = {"error": "internal", "message": f"{type(exc).__name__}: {exc}" or type(exc).__name__}
        code = 1
    out["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
