"""twincat-docs: offline Beckhoff documentation lookup, read-only, one JSON object per call.

Reads the two databases of the local TwinCAT knowledge base (`<kb>`, see `status`):
  <kb>\\helpviewer\\infosys_en.db   Beckhoff Infosys from the Help Viewer package, built by builders\\infosys_local.py index
  <kb>\\manualsdb\\manuals.db       PDF hardware manuals, built by builders\\manuals_db.py update
(the builders ship next to this script and need Python 3.12+; <kb>\\tools\\ holds stubs that run them)
Nothing is written: both are opened with SQLite's read-only mode. Standard library only. Needs Python 3.12 or newer
(checked at start, like the builders and the server), SQLite with FTS5 — the python.org builds have it.

usage:
  python kb_docs.py <command> key=value ... [--kb PATH]
  python kb_docs.py help

Exit code 0 with the answer, 1 with {"error": code, "message": ..., [...]}. Values with spaces go in quotes:
  python kb_docs.py docs_search "query=fbRead( READ := FALSE )" limit=3

The query and ranking logic is the one of infosys_local.py / manuals_db.py (same bm25 weights, snippet sizes, page
reference resolution, manual matching, product-code lookup); differences, all deliberate: a name that exists in
several books is never resolved silently (error 'ambiguous' with the candidates), filters are applied before the row
limit so 'total' is exact, out-of-range parameters are refused instead of clamped, an invalid FTS5 query is retried
once as a literal phrase.
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

VERSION = "3"           # 2: index state in every answer, links as refs, builders shipped beside it; 3: Python 3.12+
FALLBACK_KB = r"D:\TwinCAT-MCP\knowledge"
INFOSYS_DB = ("helpviewer", "infosys_en.db")
MANUALS_DB = ("manualsdb", "manuals.db")
WEB = "https://infosys.beckhoff.com/content/{lcid}/{book}/{pid}.html"
BUILDERS = Path(__file__).resolve().parent / "builders"
BUILD_INFOSYS = (f'<Python 3.12+> "{BUILDERS / "infosys_local.py"}" index --kb "<kb>"   (≈ 150 s, reads the Help Viewer '
                 'package BKINFOSYS3*.mshc)')
BUILD_MANUALS = (r"copy the PDF manuals into <kb>\manuals\ (sub-folders allowed), then: <Python 3.12+ with pypdf or "
                 f'PyMuPDF> "{BUILDERS / "manuals_db.py"}" update --kb "<kb>"')

RULES = [
    "Documentation questions use this skill; ads_* tools read the running PLC only and never return documentation.",
    "Known symbol -> docs_declaration; unknown -> docs_search -> ref -> docs_page section=...",
    "A name in several books is never resolved silently: 'ambiguous' + candidates. Pick the library the project "
    "uses (proj_libraries), call again with book= or ref=, and say which one you used.",
    "also_in lists the other books that have a page with the same title.",
    "Every item has ref = book/pageid; docs_page accepts that ref, an Infosys URL, a bare page id, an exact title "
    "and ms-xhelp:///?Id=... links.",
    "Cite ref + url in the answer.",
    "Queries are FTS5: words ANDed, \"exact phrase\", OR, NOT, prefix*. Invalid syntax is retried once as a phrase "
    "(query_mode says so).",
    "Ranges are checked, not clamped: limit 1-50, max_chars 200-20000, offset >= 0, depth 1-6, at most 21 pages.",
    "Long pages: section= first (outline lists them), then offset=next_offset.",
    "Nothing found -> say so. Then the twincat server's infosys_* tools; a missing manual -> the user copies the PDF "
    "into <kb>\\manuals\\ and runs manuals_db.py update.",
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
    """One parameter: kind 'str' | 'int', required, default, (lo, hi) for ints."""

    def __init__(self, kind: str = "str", required: bool = False, default: Any = None, lo: int | None = None,
                 hi: int | None = None, doc: str = ""):
        self.kind, self.required, self.default, self.lo, self.hi, self.doc = kind, required, default, lo, hi, doc

    def describe(self) -> str:
        s = self.doc or self.kind
        if self.lo is not None:
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
            if (p.lo is not None and v < p.lo) or (p.hi is not None and v > p.hi):
                rng = f"{p.lo}-{p.hi}" if p.hi is not None else f">= {p.lo}"
                raise Err("bad_param", f"{name} must be {rng} (got {v}) — out-of-range values are refused, not clamped")
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
# Same rules as the builders (builders/infosys_local.py and builders/manuals_db.py `stats --json`): a rebuild writes
# <db>.building and replaces the index only when complete, so the live file is always a whole index; a .building file
# whose heartbeat stopped is an interrupted rebuild. The twincat server's kb_status / tc_doctor and the instructions
# it sends at every (re)connection use these functions too.
SCHEMA_VERSION = "2"
HEARTBEAT_STALE_S = 120
MANUALS_HEARTBEAT_STALE_S = 300
MANUALS_TEXT_FORMAT = "3"          # manuals_db.py TEXT_FORMAT: page text / toc rules


def _meta_of(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        con = sqlite3.connect(ro_uri(path), uri=True, timeout=5)
        try:
            return {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _stamp(p: Path) -> str:
    st = p.stat()
    return f"{p.name}|{st.st_size}|{int(st.st_mtime)}"


def index_state(final: Path) -> dict[str, Any]:
    building, lock = final.with_name(final.name + ".building"), final.with_name(final.name + ".lock")
    now = time.time()
    meta = _meta_of(final)
    out: dict[str, Any] = {"database": str(final)}
    if not final.is_file():
        out["state"] = "missing"
    elif not meta.get("source"):
        out["state"] = "incomplete"
    elif meta.get("schema") != SCHEMA_VERSION:
        out["state"] = "old_format"
    else:
        out["state"] = "complete"
        pkg = Path(meta["mshc_path"]) if meta.get("mshc_path") else None
        if pkg is not None:
            try:
                if not pkg.is_file():
                    out["package_note"] = f"the package it was built from is gone: {pkg}"
                elif _stamp(pkg) != meta["source"]:
                    out["state"] = "outdated"
                    out["package_note"] = f"the Help Viewer package changed since the index was built: {pkg}"
                else:
                    newer = [x for x in pkg.parent.glob("BKINFOSYS3*.mshc")
                             if x != pkg and x.stat().st_mtime > pkg.stat().st_mtime]
                    if newer:
                        out["state"] = "outdated"
                        out["package_note"] = f"a newer package exists: {max(newer, key=lambda x: x.stat().st_mtime)}"
            except OSError:
                pass
    for k in ("pages", "books", "links", "links_resolved", "indexed", "lcid", "builder", "mshc_path"):
        if meta.get(k):
            out[k] = int(meta[k]) if k in ("pages", "books", "links", "links_resolved") and meta[k].isdigit() else meta[k]
    if meta.get("source"):
        out["package"] = meta["source"].split("|")[0]
    if building.is_file():
        bm = _meta_of(building)
        beat = max([float(bm.get("heartbeat") or 0)] + ([lock.stat().st_mtime] if lock.is_file() else []))
        rb: dict[str, Any] = {"file": str(building), "pages_done": int(bm.get("pages_done") or 0),
                              "pages_total": int(bm.get("pages_total") or 0), "started": bm.get("started"),
                              "seconds_since_progress": round(now - beat) if beat else None}
        if bm.get("state") == "complete":
            rb["state"] = "waiting_to_replace"
        elif beat and now - beat < HEARTBEAT_STALE_S:
            rb["state"] = "running"
        else:
            rb["state"] = "interrupted"
        out["rebuild"] = rb
    out["message"] = index_message(out)
    return out


def index_message(s: dict[str, Any]) -> str:
    live = {"missing": "no index yet", "incomplete": "INCOMPLETE (an earlier build stopped half-way) — run index",
            "old_format": "complete text, older format without links — run index once to upgrade",
            "complete": "complete", "outdated": "OUTDATED — " + s.get("package_note", "") + " — run index"}[s["state"]]
    if s["state"] in ("complete", "outdated") and s.get("pages"):
        live += f" ({s['pages']} pages, {s.get('books')} books, built {s.get('indexed')})"
    rb = s.get("rebuild")
    if rb:
        prog = f"{rb['pages_done']} of {rb['pages_total']} pages"
        live += {"running": f"; a rebuild is RUNNING ({prog}) — the index above stays in use until it is done",
                 "interrupted": f"; a rebuild was INTERRUPTED at {prog} — the index above is still the one in use; "
                                "run index to build again",
                 "waiting_to_replace": "; a finished rebuild waits to replace it (the file was open) — run index again"
                 }[rb["state"]]
    return live


def manuals_state(db: Path, folder: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"database": str(db), "folder": str(folder)}
    try:
        on_disk = {os.path.normcase(str(f)): f for f in folder.rglob("*")
                   if f.suffix.lower() == ".pdf" and f.is_file()} if folder.is_dir() else {}
    except OSError:
        on_disk = {}
    out["pdfs_in_folder"] = len(on_disk)
    if not db.is_file():
        out["state"] = "missing"
        out["message"] = f"no manuals database yet ({len(on_disk)} PDFs in the folder) — run update"
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
    lock = db.with_name(db.name + ".lock")
    beat = max(float(meta.get("heartbeat") or 0), lock.stat().st_mtime if lock.is_file() else 0)
    if meta.get("state") == "updating":
        out["updating"] = {"state": "running" if time.time() - beat < MANUALS_HEARTBEAT_STALE_S else "interrupted",
                           "files_done": int(meta.get("files_done") or 0), "files_total": int(meta.get("files_total") or 0)}
    pending = len(new) + len(changed) + len(gone)
    fmt = meta.get("text_format") or "1"
    old_text = bool(docs) and fmt != MANUALS_TEXT_FORMAT
    out["text_format"] = fmt
    out["state"] = "pending" if pending else "outdated" if old_text else "up_to_date"
    msg = f"{len(docs)} manuals, {out['pages']} pages indexed"
    if pending:
        msg += f"; {len(new)} new, {len(changed)} changed, {len(gone)} removed PDF(s) not reflected — run update"
    if old_text:
        msg += (f"; page text format {fmt}, the builder writes {MANUALS_TEXT_FORMAT} (running headers removed, "
                "wrapped headings joined) — run update, it reads every manual again")
    if out.get("updating"):
        u = out["updating"]
        msg += (f"; an update is RUNNING ({u['files_done']} of {u['files_total']} files)" if u["state"] == "running" else
                f"; the last update was INTERRUPTED at {u['files_done']} of {u['files_total']} files — run update")
    out["message"] = msg
    return out


def overview(kb: Path) -> dict[str, Any]:
    """Both databases of a knowledge folder, as the server reports them at each connection."""
    i = index_state(kb / INFOSYS_DB[0] / INFOSYS_DB[1])
    try:
        m = manuals_state(kb / MANUALS_DB[0] / MANUALS_DB[1], kb / "manuals")
    except sqlite3.Error as exc:
        m = {"state": "error", "message": f"manuals database not readable: {exc}"}
    return {"knowledge_base": str(kb), "infosys": i, "manuals": m,
            "summary": f"Infosys index: {i['message']}. Manuals: {m['message']}."}


class Ctx:
    def __init__(self, kb: Path, kb_source: str):
        self.kb, self.kb_source = kb, kb_source
        self._con: dict[str, sqlite3.Connection] = {}
        self._src: dict[str, dict[str, Any]] = {}

    def path(self, which: str) -> Path:
        sub, name = INFOSYS_DB if which == "infosys" else MANUALS_DB
        return self.kb / sub / name

    def con(self, which: str) -> sqlite3.Connection:
        if which in self._con:
            return self._con[which]
        p = self.path(which)
        build = BUILD_INFOSYS if which == "infosys" else BUILD_MANUALS
        if not p.is_file():
            raise Err("no_index", f"no {'Infosys' if which == 'infosys' else 'manuals'} database at {p} "
                      f"(knowledge base {self.kb}, from {self.kb_source})",
                      hint=f"build it: {build}; another knowledge folder: --kb PATH "
                           "(the twincat server's kb_status shows the one it uses)")
        con = sqlite3.connect(ro_uri(p), uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:   # N65: the index's own term statistics (in memory, temp schema) — before the connection turns query-only
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.fts_vocab USING fts5vocab(main, 'fts', 'row')")
        except sqlite3.Error:
            pass
        con.execute("PRAGMA query_only = 1")
        con.create_function("fnm", 2, lambda s, pat: int(fnmatch.fnmatch((s or "").lower(), (pat or "").lower())),
                            deterministic=True)
        need = {"meta", "page", "fts"} if which == "infosys" else {"doc", "page", "toc", "product", "fts"}
        have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        if not need <= have:
            raise Err("no_index", f"{p} is not a {'infosys_local.py' if which == 'infosys' else 'manuals_db.py'} database "
                      f"(tables missing: {sorted(need - have)})", hint=f"rebuild it: {build}")
        self._con[which] = con
        return con

    def source(self, which: str) -> dict[str, Any]:
        if which in self._src:
            return self._src[which]
        con = self.con(which)
        if which == "infosys":
            meta = {r[0]: r[1] for r in con.execute("SELECT key, value FROM meta")}
            stamp = meta.get("source") or ""
            parts = stamp.split("|")
            src: dict[str, Any] = {"database": str(self.path(which)), "package": parts[0] or None,
                                   "indexed": meta.get("indexed"), "lcid": meta.get("lcid") or "1033"}
            if len(parts) == 3 and parts[2].isdigit():
                src["package_date"] = _dt.datetime.fromtimestamp(int(parts[2])).strftime("%Y-%m-%d")
            st = index_state(self.path(which))
            src["state"] = st["state"]
            if st["state"] != "complete" or st.get("rebuild"):
                src["state_note"] = st["message"]          # the answer comes from this index — say what it is
        else:
            n, pages = con.execute("SELECT COUNT(*), COALESCE(SUM(pages), 0) FROM doc").fetchone()
            src = {"database": str(self.path(which)), "manuals": n, "pages": pages}
        self._src[which] = src
        return src

    def lcid(self) -> str:
        return str(self.source("infosys").get("lcid") or "1033")


# ---------------------------------------------------------------------------------------------- shared helpers
_WS = re.compile(r"\s+")
_MSXHELP = re.compile(r"ms-xhelp:/*\?id=([^&#\s]+)", re.I)
_URL = re.compile(r"/content/\d+/([^/]+)/(\d+)\.html?", re.I)
_HEAD = re.compile(r"(?m)^(#{1,4}) (.*)$")
_TYPES = (r"(?:BOOL|BYTE|[DL]?WORD|U?[SDL]?INT|L?REAL|L?TIME|DATE|TOD|DT|W?STRING|PVOID|AXIS_REF|REFERENCE|POINTER|"
          r"T_[A-Za-z]|ST_[A-Za-z]|E_[A-Za-z]|MC_[A-Za-z]|I_[A-Za-z])")
_FBD_PAIR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\s" + _TYPES)


def one_line(s: str | None) -> str:
    return _WS.sub(" ", s or "").strip()


def clean(s: str | None) -> str:
    return (s or "").replace("\xa0", " ")


def like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def has_words(q: str) -> bool:
    return bool(re.search(r"[^\W_]", q))


_FTS_SYNTAX = re.compile(r'["*:()^]|\b(?:AND|OR|NOT|NEAR)\b')


def natural(q: str) -> bool:
    """Words only (no FTS5 operators, quotes, prefixes, column filters, code punctuation): a question in words."""
    return not _FTS_SYNTAX.search(q) and bool(re.fullmatch(r"[\w\s\-./,?!']+", q))


# words that say nothing about a page: never searched alone, never a prefix (N65)
def _fold(w: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", w.lower()) if not unicodedata.combining(c))


# compared with _fold(word), so stored folded: 'für' was never matched (the query word folds to 'fur')
STOP_WORDS = frozenset(_fold(w) for w in """
a an and are as at be but by can do does for from has have how i if in into is it its no not of on or so such that
the their then there these this to was were what when where which while who why will with without you your
der die das den dem des ein eine einer eines einem und oder ist sind wie was wann wo warum mit von zu zum zur im in
am an auf aus bei für nicht kein keine ich es er sie wir ihr
""".split())
COMMON_SHARE = 0.2          # a word on more than this share of the pages does not pick a page in the some-words tier


def _term_df(con: sqlite3.Connection, word: str, prefix: bool) -> int | None:
    """Pages holding the word (all its tokens: 'T_AmsNetId' is 't' + 'amsnetid'), from the index's own vocabulary;
    prefix: whether any term starts with it. None when the vocabulary is not available."""
    toks = [t for t in re.split(r"[^\w]|_", _fold(word)) if t]
    if not toks:
        return 0
    try:
        dfs = []
        for i, t in enumerate(toks):
            if prefix and i == len(toks) - 1:
                r = con.execute("SELECT MAX(doc) FROM temp.fts_vocab WHERE term >= ? AND term < ?",
                                (t, t + "\U0010ffff")).fetchone()
            else:
                r = con.execute("SELECT doc FROM temp.fts_vocab WHERE term = ?", (t,)).fetchone()
            dfs.append(int(r[0] or 0) if r else 0)
        return min(dfs)
    except sqlite3.Error:
        return None


def _page_count(con: sqlite3.Connection) -> int:
    try:
        return int(con.execute("SELECT COUNT(*) FROM fts").fetchone()[0])
    except sqlite3.Error:
        return 0


def search_tiers(con: sqlite3.Connection, sql: str, count_sql: str, query: str, extra: list[Any],
                 limit: int, title_key: str | None = None) -> tuple[list[sqlite3.Row], int, str, str, dict[str, Any]]:
    """N36/N65: a question in words falls back in steps, and only on words that can pick a page:
      1. every word as written (stop words included: a precise query stays precise);
      2. every informative word that occurs in the index — stop words, 1-2 letter words and words on no page dropped;
      3. those words as prefixes (a word is kept when some term starts with it: 'Diagnos' -> diagnostics);
      4. some of them (OR) — without words that are on more than 20 % of the pages.
    Nothing informative left: no hits (never 49,096 'some words' hits for 'how to zzqxv a the'). A page whose title
    is one of the query words ranks first ('ADSREAD xyzqq' -> ADSREAD before AdsReadDeviceInfo). FTS5 syntax or code
    punctuation runs as written (run_fts, with the phrase retry).
    -> (rows, total, mode, expression, info: words used / ignored with the reason)."""
    q = clean(query).strip()
    if not natural(q):
        rows, mode, expr = run_fts(con, sql, query, extra + [limit])
        return rows, con.execute(count_sql, [expr, *extra]).fetchone()[0], mode, expr, {}
    words = [w for w in re.findall(r"[^\W_]+(?:_[^\W_]+)*", q)]
    if not words:
        raise Err("empty_query", f"the query {query!r} has no searchable word (punctuation is not indexed)")

    def ph(w: str) -> str:
        return '"' + w.replace('"', '""') + '"'
    ignored: dict[str, str] = {}
    informative = []
    for w in words:
        if _fold(w) in STOP_WORDS:
            ignored[w] = "stop word"
        elif len(w) < 3 and not w.isdigit():
            ignored[w] = "shorter than 3 letters"
        else:
            informative.append(w)
    pref = []                                  # informative words some term of the index starts with
    for w in informative:
        df = _term_df(con, w, True)
        if df is None or df > 0:
            pref.append(w)
        else:
            ignored[w] = "on no page"
    n_pages = _page_count(con)
    common = {w for w in pref if n_pages and (_term_df(con, w, False) or 0) > COMMON_SHARE * n_pages}
    tiers = [("all words", " ".join(ph(w) for w in words), words)]
    if pref and pref != words:                 # without the stop words / words on no page, exactly as written
        tiers.append(("all words found in the index", " ".join(ph(w) for w in pref), pref))
    if pref:
        tiers.append(("all words (as prefixes)", " ".join(ph(w) + "*" for w in pref), pref))
    if len(pref) > 1:
        some = [w for w in pref if w not in common] or pref     # a word on most pages picks nothing — unless all are
        tiers.append(("some words (no page has all of them)", " OR ".join(ph(w) + "*" for w in some), some))
    lw = {w.lower() for w in words}
    for label, expr, used in tiers:
        fetch = min(max(limit * 3, limit), 150) if title_key else limit
        rows = con.execute(sql, [expr, *extra, fetch]).fetchall()
        if rows:
            if title_key:     # a page titled exactly like a query word first, bm25 order otherwise
                rows = sorted(rows, key=lambda r: 0 if str(r[title_key] or "").lower() in lw else 1)
            info: dict[str, Any] = {"words_used": used}
            if ignored:
                info["words_ignored"] = ignored
            if label.startswith("some words") and common and set(used) != set(pref):
                info["words_too_common"] = sorted(common)
            return rows[:limit], con.execute(count_sql, [expr, *extra]).fetchone()[0], label, expr, info
    info = {"words_used": []}
    if ignored:
        info["words_ignored"] = ignored
    return [], 0, tiers[-1][0], tiers[-1][1], info


def matched_words(con: sqlite3.Connection, rowid: int, words: list[str]) -> list[str]:
    """Which of the words (as prefixes) the page with this fts rowid holds — shown per hit in the some-words tier."""
    out = []
    for w in words:
        try:
            if con.execute("SELECT 1 FROM fts WHERE rowid = ? AND fts MATCH ?", (rowid, '"' + w.replace('"', '""') + '"*')).fetchone():
                out.append(w)
        except sqlite3.Error:
            pass
    return out


def run_fts(con: sqlite3.Connection, sql: str, query: str, extra: list[Any]) -> tuple[list[sqlite3.Row], str, str]:
    """Run an FTS5 statement whose first parameter is the MATCH expression. -> (rows, mode, expression used).
    Invalid FTS5 syntax is retried once as a literal phrase (rule 7)."""
    q = clean(query).strip()
    if not q:
        raise Err("empty_query", "the query is empty")
    if not has_words(q):
        raise Err("empty_query", f"the query {query!r} has no searchable word (punctuation is not indexed)")
    try:
        return con.execute(sql, [q, *extra]).fetchall(), "fts5", q
    except sqlite3.OperationalError as exc:
        msg = str(exc)
        if "no such module" in msg:
            raise Err("internal", f"this Python's SQLite has no FTS5 ({msg}); use the python.org Python") from None
        if "locked" in msg or "disk i/o" in msg.lower():
            raise Err("internal", f"database busy ({msg}) — is the index being rebuilt right now?") from None
    phrase = '"' + q.replace('"', '""') + '"'
    try:
        return con.execute(sql, [phrase, *extra]).fetchall(), "phrase (input was not valid FTS5 syntax)", phrase
    except sqlite3.OperationalError as exc:
        raise Err("bad_query", f"query not usable even as a phrase: {exc}",
                  hint="FTS5 syntax: words, \"exact phrase\", OR, NOT, prefix*") from None


# ---------------------------------------------------------------------------------------------- Infosys
def _books(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT book FROM page ORDER BY book")]


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
    # infosys_local.py's preference: library pages (tcplclib_*) first, then the shortest book name
    return sorted(rows, key=lambda c: (not c["book"].startswith("tcplclib_"), len(c["book"]), c["book"], c["pid"]))


def resolve(ctx: Ctx, ref: str, book: str | None, what: str = "ref") -> tuple[sqlite3.Row, str]:
    """Page for a reference: ms-xhelp id → Infosys URL → book/pid → bare pid → exact title (case-insensitive).
    -> (row, how it was resolved). Several pages → Err('ambiguous') with candidates (rule 3)."""
    con = ctx.con("infosys")
    s = clean(ref).strip()
    if not s:
        raise Err("bad_param", f"{what} is empty")
    m = _MSXHELP.search(s)
    if m:
        hid = m.group(1)
        rows = con.execute("SELECT * FROM page WHERE help_id = ?", (hid,)).fetchall() or \
            con.execute("SELECT * FROM page WHERE help_id = ? COLLATE NOCASE", (hid,)).fetchall()
        return _pick(ctx, rows, ref, None, "ms-xhelp id", what)
    m = _URL.search(s)
    if m:
        rows = con.execute("SELECT * FROM page WHERE book = ? AND pid = ?", (m.group(1), m.group(2))).fetchall()
        return _pick(ctx, rows, ref, None, "Infosys URL", what)
    if "/" in s and not s.startswith("/"):
        b, p = s.split("/", 1)
        rows = con.execute("SELECT * FROM page WHERE book = ? AND pid = ?", (b, p)).fetchall()
        if not rows:
            rows = con.execute("SELECT * FROM page WHERE book = ? COLLATE NOCASE AND pid = ?", (b, p)).fetchall()
        if rows or p.isdigit():
            return _pick(ctx, rows, ref, None, "book/pageid", what)
        # not book/pageid: a title with a slash ('Send/Receive') — go on to the title lookup
    if s.isdigit():
        rows = [r for r in con.execute("SELECT * FROM page WHERE pid = ?", (s,)).fetchall()
                if not book or fnmatch.fnmatch(r["book"].lower(), book.lower())]
        return _pick(ctx, rows, ref, book, "page id", what)
    _check_book_glob(con, book)
    rows = [r for r in con.execute("SELECT * FROM page WHERE title = ? COLLATE NOCASE", (s,)).fetchall()
            if not book or fnmatch.fnmatch(r["book"].lower(), book.lower())]
    return _pick(ctx, rows, ref, book, "title", what)


def _pick(ctx: Ctx, rows: list[sqlite3.Row], ref: str, book: str | None, how: str, what: str) -> tuple[sqlite3.Row, str]:
    if len(rows) == 1:
        return rows[0], how
    if not rows:
        con = ctx.con("infosys")
        near = []
        word = clean(ref).strip()
        if how == "title" and len(word) >= 3 and has_words(word):
            # N35: the FTS title column, not a LIKE scan over every page body (9.6 s on the real package)
            expr = "title : " + " ".join('"' + w.replace('"', '""') + '"*' for w in re.findall(r"[^\W_]+", word))
            try:
                hits = con.execute("SELECT book, pid FROM fts WHERE fts MATCH ? LIMIT 200", (expr,)).fetchall()
            except sqlite3.OperationalError:
                hits = []
            rows_near = [con.execute("SELECT * FROM page WHERE book = ? AND pid = ?", (h["book"], h["pid"])).fetchone()
                         for h in hits[:200]]
            near = [_cand(ctx, r) for r in _order([r for r in rows_near if r is not None])
                    if not book or fnmatch.fnmatch(r["book"].lower(), book.lower())][:8]
        raise Err("not_found", f"no page for {what} {ref!r}" + (f" in books {book!r}" if book else "") + f" (looked up as {how})",
                  near=near, hint=f"docs_search query={word!r} finds pages by their text")
    cands = [_cand(ctx, r) for r in _order(rows)]
    raise Err("ambiguous", f"{len(rows)} pages match {what} {ref!r} ({how}); nothing was chosen — pick the library the "
              "project uses (proj_libraries) and call again with book=<book> or ref=<ref>, and say which one you used",
              candidates=cands[:20])


def _also_in(con: sqlite3.Connection, r: sqlite3.Row) -> list[dict[str, Any]]:
    rows = con.execute("SELECT book, pid, title, version FROM page WHERE title = ? COLLATE NOCASE AND NOT (book = ? AND pid = ?)",
                       (r["title"], r["book"], r["pid"])).fetchall()
    return [{"ref": f"{x['book']}/{x['pid']}", "book": x["book"], "version": x["version"] or None} for x in _order(rows)]


def _links(con: sqlite3.Connection, r: sqlite3.Row, within: str | None = None, limit: int = 30) -> list[dict[str, Any]] | None:
    """The page's links that resolve to a page of the package (builder version 2), as refs docs_page opens.
    within: only links whose text is in that part of the page. None: the index has no link table (version 1)."""
    try:
        rows = con.execute("SELECT text, target_book, target_pid FROM link WHERE book = ? AND pid = ? AND target_pid IS NOT NULL "
                           "ORDER BY n", (r["book"], r["pid"])).fetchall()
    except sqlite3.OperationalError:
        return None
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for x in rows:
        ref = f"{x['target_book']}/{x['target_pid']}"
        if ref in seen or (within is not None and x["text"] not in within):
            continue
        seen.add(ref)
        out.append({"text": x["text"], "ref": ref})
        if len(out) >= limit:
            break
    return out


def _page_head(ctx: Ctx, r: sqlite3.Row) -> dict[str, Any]:
    return {"ref": f"{r['book']}/{r['pid']}", "title": r["title"], "book": r["book"], "version": r["version"] or None,
            "url": WEB.format(lcid=ctx.lcid(), book=r["book"], pid=r["pid"])}


def cmd_docs_search(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("infosys")
    _check_book_glob(con, a["book"])
    where = "fts MATCH ?" + (" AND fnm(book, ?)" if a["book"] else "")
    extra = [a["book"]] if a["book"] else []
    rows, total, mode, expr, info = search_tiers(
        con, "SELECT fts.rowid AS rid, book, pid, title, snippet(fts, 1, '[', ']', '…', 18) AS s, "
             f"bm25(fts, 8.0, 1.0) AS r FROM fts WHERE {where} ORDER BY r LIMIT ?",
        f"SELECT COUNT(*) FROM fts WHERE {where}", a["query"], extra, a["limit"], title_key="title")
    items = []
    for r in rows:
        v = con.execute("SELECT version FROM page WHERE book = ? AND pid = ?", (r["book"], r["pid"])).fetchone()
        it = {"ref": f"{r['book']}/{r['pid']}", "title": r["title"], "book": r["book"],
              "version": (v[0] if v else None) or None, "excerpt": one_line(r["s"]),
              "url": WEB.format(lcid=ctx.lcid(), book=r["book"], pid=r["pid"]), "score": float(f"{-r['r']:.4g}")}
        if mode.startswith("some words"):
            it["matched"] = matched_words(con, r["rid"], info.get("words_used") or [])
        items.append(it)
    out: dict[str, Any] = {"query": a["query"], "query_mode": mode, **info, "count": len(items), "total": total,
                           "truncated": total > len(items), "items": items, "source": ctx.source("infosys")}
    if not items:
        out["hint"] = ("no page contains these words" + (f" in books {a['book']!r}" if a["book"] else "")
                       + " — try fewer or other words, OR, prefix*; then the twincat server's infosys_search")
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


def cmd_docs_declaration(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("infosys")
    r, how = resolve(ctx, a["symbol"], a["book"], what="symbol")
    body = clean(r["body"])
    blocks = [b.strip("\n") for b in re.findall(r"```\n(.*?)\n```", body, re.S)]
    shown, used, rest = [], 0, 0
    for b in blocks:
        if used + len(b) > 12000 and shown:
            rest += 1
            continue
        shown.append(b)
        used += len(b)
    req = re.search(r"(?ms)^#{1,4} (?:Requirements|Prerequisites|Voraussetzungen)\s*\n(.*?)(?=^#{1,4} |\Z)", body)
    out: dict[str, Any] = _page_head(ctx, r)
    out.update({"resolved_by": how, "intro": _intro(body), "declaration_blocks": shown,
                "requirements": req.group(1).strip() if req else None, "also_in": _also_in(con, r)})
    lk = _links(con, r)
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
    body = clean(r["body"])
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
    lk = _links(con, r, within=part)
    if lk is None:
        out["links_note"] = "this index has no link table (built by infosys_local.py version 1) — rebuild it to follow links"
    elif lk:
        out["links"] = lk
    also = _also_in(con, r)
    if also:
        out["also_in"] = also
    out["source"] = ctx.source("infosys")
    return out


def cmd_docs_books(ctx: Ctx, a: dict[str, Any]) -> dict[str, Any]:
    con = ctx.con("infosys")
    books = [{"book": r["book"], "pages": r["n"]}
             for r in con.execute("SELECT book, COUNT(*) AS n FROM page GROUP BY book ORDER BY book")
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
    rows, total, mode, expr, info = search_tiers(
        con, "SELECT fts.rowid AS rid, doc, page, section, snippet(fts, 0, '[', ']', '…', 16) AS s, "
             f"bm25(fts, 1.0, 4.0) AS r FROM fts WHERE {where} ORDER BY r LIMIT ?",
        f"SELECT COUNT(*) FROM fts WHERE {where}", a["query"], extra, a["limit"])
    items = []
    for r in rows:
        it = {"doc": names.get(r["doc"], r["doc"]), "doc_id": r["doc"], "page": r["page"], "section": r["section"] or None,
              "excerpt": one_line(r["s"]), "score": float(f"{-r['r']:.4g}")}
        if mode.startswith("some words"):
            it["matched"] = matched_words(con, r["rid"], info.get("words_used") or [])
        items.append(it)
    out: dict[str, Any] = {"query": a["query"], "query_mode": mode, **info, "count": len(items), "total": total,
                           "truncated": total > len(items), "items": items, "source": _msource(ctx)}
    if not items:
        out["hint"] = "no indexed manual page contains these words; a manual that is not indexed yet: " + BUILD_MANUALS
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
    out.update({"count": len(items), "items": items, "source": _msource(ctx)})
    if not items:
        out["hint"] = f"{code} is in no indexed manual — " + BUILD_MANUALS
    return out


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
    out_pages, used, cut = [], 0, None
    for r in con.execute("SELECT page, section, text FROM page WHERE doc = ? AND page BETWEEN ? AND ? ORDER BY page",
                         (d["id"], lo, hi)):
        t = r["text"] or ""
        if used + len(t) > a["max_chars"]:
            t = t[:max(0, a["max_chars"] - used)]
            out_pages.append({"page": r["page"], "section": r["section"] or None, "text": t, "cut": True})
            cut = r["page"]
            break
        out_pages.append({"page": r["page"], "section": r["section"] or None, "text": t})
        used += len(t)
    out: dict[str, Any] = {"doc": d["name"], "doc_id": d["id"], "title": d["title"], "path": d["path"],
                           "pages": out_pages, "truncated": cut is not None, "source": _msource(ctx)}
    if cut is not None:
        out["next"] = f"cut at max_chars {a['max_chars']} on page {cut}: manual_page doc={d['name']} pages={cut}-{hi}"
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
                src["pages"] = con.execute("SELECT COUNT(*) FROM page").fetchone()[0]
                src["books"] = con.execute("SELECT COUNT(DISTINCT book) FROM page").fetchone()[0]
            else:
                src["text_pages"] = con.execute("SELECT COALESCE(SUM(text_pages), 0) FROM doc").fetchone()[0]
                src["product_codes"] = con.execute("SELECT COUNT(DISTINCT code) FROM product").fetchone()[0]
            src["size_mb"] = round(ctx.path(which).stat().st_size / 1e6, 1)
            out[which] = src
        except Err as exc:
            out[which] = {"missing": exc.message, **exc.extra}
    out["rules"] = [kbtext(r) for r in RULES]
    return out


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
    "manual_search": (cmd_manual_search, {"query": P(required=True, doc="FTS5 query"), "doc": P(doc="manual: file name, glob or id"),
                                          "limit": P("int", default=10, lo=1, hi=50)},
                      "PDF manuals full text: doc, page, section, excerpt"),
    "manual_product": (cmd_manual_product, {"code": P(required=True, doc="product code, e.g. EL3102 (also EL3102-xxxx)"),
                                            "limit": P("int", default=20, lo=1, hi=50)},
                       "which manuals mention a product code, on which pages"),
    "manual_toc": (cmd_manual_toc, {"doc": P(required=True, doc="file name, glob or id"), "depth": P("int", default=2, lo=1, hi=6)},
                   "a manual's chapters with page numbers"),
    "manual_page": (cmd_manual_page, {"doc": P(required=True, doc="file name, glob or id"), "pages": P(required=True, doc="N or N-M (<= 21 pages)"),
                                      "max_chars": P("int", default=4000, lo=200, hi=20000)},
                    "page text of a manual with the section of each page"),
    "manual_docs": (cmd_manual_docs, {"filter": P(doc="file name / title glob or word")}, "the indexed manuals"),
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
            elif cmd.startswith(("infosys_", "kb_", "hw_")):
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
