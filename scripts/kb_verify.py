"""Offline, read-only verification of the twincat-mcp knowledge base on this PC.

Runs every main kind of query of the plugin's documentation tools against the real indexes of the knowledge folder
(index\\infosys-<lcid>.db, index\\manuals.db, index\\esi.db, index\\knowledge.db), through the plugin's OWN code —
the readers the MCP tools use (twincat_mcp.infosys / esi_index / knowledge) and the twincat-docs skill's commands
(skills/twincat-docs/kb_docs.py) — and reports, for every book and page, every manual and page, every ESI device,
object, diagnosis message and module, and every unit of the indexed projects, whether the answer is right.

    python scripts\\kb_verify.py                       quick: a sample of every book / manual / device (minutes)
    python scripts\\kb_verify.py --mode full           every page, every object (tens of minutes)
    python scripts\\kb_verify.py --kb D:\\TwinCAT-MCP\\knowledge --only infosys,manuals

Nothing is changed: every database is opened read-only, nothing is built, nothing is fetched from the network, and
the run checks at its end that no file of index\\ and manuals\\ changed. The report goes to
%USERPROFILE%\\.twincat_mcp\\kb_verify\\<time>\\ (report.md, results.json, failures.csv) — never into the knowledge
folder. Exit code: 0 no failure, 1 failures found (or a file changed), 2 the run could not start, 130 interrupted
(Ctrl+C: the report of what ran is written).
Needs Python 3.12+ and nothing else: the plugin's own modules — of the plugin this script is part of, else the
newest installed twincat-mcp, or --plugin <folder>.
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:   # before anything else, so an older Python gets this message, not a traceback
    sys.exit(f"kb_verify.py needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
             f"{sys.version.split()[0]} ({sys.executable}).")

import argparse  # noqa: E402 — after the version check on purpose
import csv  # noqa: E402
import datetime as dt  # noqa: E402
import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import random  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from collections import Counter, defaultdict  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Callable, Iterable  # noqa: E402


def _version_key(p: Path) -> tuple[int, ...]:
    return tuple(int(x) if x.isdigit() else -1 for x in re.split(r"[.\-]", p.name))


def plugin_root(argv: list[str]) -> tuple[Path, str]:
    """The plugin whose code is tested: --plugin PATH; else the plugin this script is part of
    (<plugin>/scripts/kb_verify.py); else the newest twincat-mcp Claude Code installed
    (%USERPROFILE%\\.claude\\plugins\\cache\\<marketplace>\\twincat-mcp\\<version>)."""
    for i, a in enumerate(argv):
        if a == "--plugin" and i + 1 < len(argv):
            return Path(argv[i + 1]).expanduser().resolve(), "--plugin"
        if a.startswith("--plugin="):
            return Path(a.split("=", 1)[1]).expanduser().resolve(), "--plugin"
    here = Path(__file__).resolve().parents[1]
    if (here / "twincat_mcp" / "__init__.py").is_file():
        return here, "the plugin this script is part of"
    found = [p.parent.parent for p in (Path.home() / ".claude" / "plugins" / "cache").glob(
        "*/twincat-mcp/*/twincat_mcp/__init__.py")]
    if found:
        return max(found, key=_version_key), "the newest installed twincat-mcp"
    return here, "not found"


PLUGIN, PLUGIN_SOURCE = plugin_root(sys.argv[1:])
if not (PLUGIN / "twincat_mcp" / "__init__.py").is_file():
    sys.exit(f"the twincat-mcp plugin was not found ({PLUGIN}, {PLUGIN_SOURCE}) — run the script from the plugin's "
             "scripts folder or pass --plugin <plugin folder>")
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

from twincat_mcp import __version__ as PLUGIN_VERSION  # noqa: E402
from twincat_mcp import docsearch as DS  # noqa: E402
from twincat_mcp import esi_index as ESI  # noqa: E402
from twincat_mcp import helpviewer as HV  # noqa: E402
from twincat_mcp import infosys as INF  # noqa: E402
from twincat_mcp import infosys_build as IB  # noqa: E402
from twincat_mcp import kb_layout as L  # noqa: E402
from twincat_mcp import knowledge as K  # noqa: E402

SCRIPT_VERSION = "1"


def _load_kb_docs() -> Any:
    """The twincat-docs skill's script as a module: its commands are tested as the skill runs them."""
    path = PLUGIN / "skills" / "twincat-docs" / "kb_docs.py"
    spec = importlib.util.spec_from_file_location("tcmcp_kb_docs", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


KD = _load_kb_docs()

PASS, FAIL, WARN, SKIP = "pass", "fail", "warn", "not_judged"
STATUSES = (PASS, FAIL, WARN, SKIP)


# ====================================================================================================== results
class Check:
    """One kind of test: counts per status, every result other than a pass with its subject and detail, timing."""

    def __init__(self, cid: str, area: str, title: str, what: str):
        self.cid, self.area, self.title, self.what = cid, area, title, what
        self.counts: Counter[str] = Counter()
        self.items: dict[str, list[dict[str, Any]]] = {FAIL: [], WARN: [], SKIP: []}
        self.seconds = 0.0
        self.calls = 0
        self.slowest: list[tuple[float, str]] = []
        self.notes: list[str] = []
        self.planned: int | None = None

    def add(self, status: str, subject: str, detail: str = "", **data: Any) -> None:
        self.counts[status] += 1
        if status != PASS:
            self.items[status].append({"subject": subject, "detail": detail, **data})

    def timed(self, subject: str, fn: Callable[..., Any], *a: Any, **kw: Any) -> Any:
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            d = time.perf_counter() - t0
            self.seconds += d
            self.calls += 1
            if len(self.slowest) < 5 or d > self.slowest[-1][0]:
                self.slowest.append((d, subject))
                self.slowest.sort(key=lambda x: -x[0])
                del self.slowest[5:]

    def crash(self, subject: str, exc: BaseException) -> None:
        """An exception from the plugin's code: a failure of its own kind (the tool would answer with an error)."""
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        self.add(FAIL, subject, f"raised {type(exc).__name__}: {exc}"[:600], traceback="".join(tb[-4:])[-1500:])

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.cid, "area": self.area, "title": self.title, "what": self.what,
                "counts": {s: self.counts.get(s, 0) for s in STATUSES}, "planned": self.planned,
                "seconds": round(self.seconds, 2), "calls": self.calls,
                "ms_per_call": round(self.seconds / self.calls * 1000, 2) if self.calls else None,
                "slowest": [{"seconds": round(d, 3), "subject": s} for d, s in self.slowest],
                "notes": self.notes, "failures": self.items[FAIL], "warnings": self.items[WARN],
                "not_judged": self.items[SKIP]}


class Verifier:
    def __init__(self, args: argparse.Namespace, kb: Path, kb_source: str, out: Path):
        self.args, self.kb, self.kb_source, self.out = args, kb, kb_source, out
        self.full = args.mode == "full"
        self.top = args.top
        self.checks: dict[str, Check] = {}
        self.meta: dict[str, Any] = {}
        self.areas_done: list[str] = []
        self.untested: list[str] = []      # areas whose index does not exist
        self.esi_parity: Check | None = None
        self.ctx = KD.Ctx(kb, kb_source)
        self.docs_config, self.config_source = load_docs_config(args.config)
        self._last_progress = 0.0
        self.t0 = time.time()

    # -- bookkeeping ----------------------------------------------------------------------------------------------
    def check(self, cid: str, area: str, title: str, what: str) -> Check:
        c = Check(cid, area, title, what)
        self.checks[cid] = c
        return c

    def progress(self, c: Check, i: int, n: int) -> None:
        now = time.time()
        if now - self._last_progress >= 2.0 or i == n:
            self._last_progress = now
            bad = c.counts.get(FAIL, 0)
            sys.stdout.write(f"\r    {c.cid} {c.title[:48]:48} {i:>7}/{n:<7} failures {bad:<6}")
            sys.stdout.flush()
            if i == n:
                sys.stdout.write("\n")

    def say(self, text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def pick(self, items: list[Any], k: int, key: str) -> list[Any]:
        """Quick mode: k of the items, drawn by the seed and the group's key (a book, a manual, a check) — a re-run
        tests the same items, whichever areas run; full mode: all."""
        if self.full or len(items) <= k:
            return list(items)
        return random.Random(f"{self.args.seed}|{key}").sample(items, k)

    def skill(self, name: str, **kv: Any) -> dict[str, Any]:
        """A twincat-docs skill command, with its arguments bound and checked exactly as on the command line."""
        fn, spec, _doc = KD.COMMANDS[name]
        return fn(self.ctx, KD.bind(spec, {k: str(v) for k, v in kv.items() if v is not None}))


# ====================================================================================================== helpers
def load_docs_config(path: str | None) -> tuple[Any, str]:
    """The [docs] section of the server's twincat_mcp.toml (only mshc is used: the package to compare with) — the
    file as the server finds it (TCMCP_CONFIG, .\\twincat_mcp.toml, %USERPROFILE%\\.twincat_mcp\\config.toml) or
    --config. The rest of the file is not read or checked: it does not decide what the indexes hold."""
    import tomllib
    from twincat_mcp import config as C
    docs = C.DocsConfig()
    p = Path(path).expanduser() if path else C._find_config_file()
    if p is None:
        return docs, "no twincat_mcp.toml found (defaults)"
    try:
        with open(p, "rb") as fh:
            sub = tomllib.load(fh).get("docs") or {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return docs, f"{p} not readable ({exc}); defaults"
    for k, val in sub.items():
        if hasattr(docs, k):
            setattr(docs, k, val)
    return docs, str(p)


def snapshot(folders: Iterable[Path]) -> dict[str, tuple[int, int]]:
    """size and modification time (ns) of every file below the folders — compared after the run (read-only proof).
    Left out: SQLite's '<db>-shm', the shared-memory index of a WAL database that every reader maps and updates
    (also with mode=ro); its content is not the database's. The '-wal' file holds written pages: compared, except an
    empty one (a reader of a WAL database creates it when it is missing; it holds nothing)."""
    out: dict[str, tuple[int, int]] = {}
    for d in folders:
        if not d.is_dir():
            continue
        for f in d.rglob("*"):
            if f.name.endswith("-shm"):
                continue
            try:
                if f.is_file():
                    st = f.stat()
                    if f.name.endswith("-wal") and st.st_size == 0:
                        continue
                    out[str(f)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return out


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_text(s: str | None) -> str:
    return " ".join((s or "").split())


_TOKEN = re.compile(r"[^\W_]+")          # what FTS5's unicode61 tokenizer reads as one token


def distinctive_phrases(text: str, df: Callable[[str], int], length: int = 5, n: int = 3) -> list[str]:
    """Up to n runs of `length` consecutive tokens of the text, each around one of its rarest words (fewest pages hold
    it), rarest first — exact phrases the page holds and few other pages do (a footer's rare word is on every page of
    its manual: the next phrase decides). Empty when the text is shorter than one run."""
    toks = _TOKEN.findall(text or "")
    if len(toks) < length:
        return []
    cands = sorted((df(t.lower()), i) for i, t in enumerate(toks) if len(t) >= 4 and not t.isdigit())
    out: list[str] = []
    for _f, at in cands:
        lo = max(0, min(at - length // 2, len(toks) - length))
        ph = " ".join(toks[lo:lo + length])
        if ph not in out:
            out.append(ph)
        if len(out) >= n:
            break
    return out


def fts_df(con: sqlite3.Connection) -> Callable[[str], int]:
    """Document frequency of a term from the index's own statistics (temp.fts_vocab, made by the readers)."""
    cache: dict[str, int] = {}

    def df(term: str) -> int:
        if term not in cache:
            try:
                r = con.execute("SELECT doc FROM temp.fts_vocab WHERE term = ?", (term,)).fetchone()
            except sqlite3.Error:
                r = None
            cache[term] = int(r[0]) if r else 0
        return cache[term]
    return df


class _Offline(INF.Fetcher):
    """The network is never used: a reader that would fetch a page gets this error instead."""

    def get(self, url: str, allow_hosts: set[str] | None = None) -> bytes:   # noqa: ARG002
        raise INF.InfosysError(f"offline verification: no network access ({url})")


class _ROConn:
    """knowledge.db read-only. Knowledge._db opens it for writing (WAL, CREATE TABLE IF NOT EXISTS) — a test must not
    change it; the search itself is the plugin's code (Knowledge.search)."""

    def __init__(self, path: Path, kb: "_ROKnowledge"):
        self.path, self.kb = path, kb
        self.con: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        con = sqlite3.connect(KD.ro_uri(self.path), uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only = 1")
        row = con.execute("SELECT sql FROM sqlite_master WHERE name = 'units_fts'").fetchone()
        self.kb._fts = row is not None                        # what Knowledge._init finds out when it opens the db
        self.kb._fts_words = bool(row and " code," in (row[0] or ""))
        self.con = con
        return con

    def __exit__(self, et: Any, ev: Any, tb: Any) -> bool:
        if self.con is not None:
            self.con.close()
        return False


class _ROKnowledge(K.Knowledge):
    def __init__(self, kb: Path, home: Path):
        super().__init__(home=home, location=str(kb))
        self._kb_dir = kb

    def require(self) -> Path:          # the folder was checked by the run; nothing is created or probed
        return self._kb_dir

    def _db(self) -> Any:               # type: ignore[override]
        return _ROConn(L.locate(self._kb_dir, "knowledge"), self)


# ====================================================================================================== Infosys
# Known answers: the questions of the field retests whose right answer was checked against Beckhoff's pages.
# needs: the index format the answer depends on (an older index is said to need its rebuild, not failed).
GOLDEN: list[dict[str, Any]] = [
    {"id": "decl-ADSREAD", "kind": "declaration", "q": "ADSREAD", "library": "Tc2_System",
     "expect": {"kind": "FunctionBlock", "inputs": ["NETID", "PORT", "IDXGRP", "IDXOFFS", "LEN", "DESTADDR", "READ", "TMOUT"]}},
    {"id": "decl-MC_MoveAbsolute", "kind": "declaration", "q": "MC_MoveAbsolute",
     "expect": {"ambiguous": ["Tc2_MC2", "Tc3_DriveMotionControl"]}},
    {"id": "decl-MC_MoveAbsolute-Tc2_MC2", "kind": "declaration", "q": "MC_MoveAbsolute", "library": "Tc2_MC2",
     "expect": {"kind": "FunctionBlock", "library": "Tc2_MC2"}},
    {"id": "decl-T_MaxString", "kind": "declaration", "q": "T_MaxString", "expect": {"declaration_has": "MAX_STRING_LENGTH"}},
    {"id": "decl-FB_FileOpen", "kind": "declaration", "q": "FB_FileOpen", "expect": {"kind": "FunctionBlock", "inputs": ["sPathName", "nMode", "ePath"]}},
    {"id": "decl-TON", "kind": "declaration", "q": "TON", "expect": {"kind": "FunctionBlock", "inputs": ["IN", "PT"]}},
    {"id": "decl-MEMCPY", "kind": "declaration", "q": "MEMCPY", "needs": 5, "expect": {"kind": "Function", "return_type": "UDINT"}},
    {"id": "decl-MEMSET", "kind": "declaration", "q": "MEMSET", "needs": 5, "expect": {"kind": "Function", "return_type": "UDINT"}},
    {"id": "decl-LREAL_TO_FMTSTR", "kind": "declaration", "q": "LREAL_TO_FMTSTR", "needs": 5, "expect": {"kind": "Function", "return_type": "STRING(510)"}},
    {"id": "decl-SYSTEMTIME_TO_STRING", "kind": "declaration", "q": "SYSTEMTIME_TO_STRING", "needs": 5, "expect": {"kind": "Function", "return_type": "STRING(24)"}},
    {"id": "decl-F_ToASC", "kind": "declaration", "q": "F_ToASC", "needs": 5, "expect": {"kind": "Function", "return_type": "BYTE"}},
    {"id": "decl-F_GetTaskTime", "kind": "declaration", "q": "F_GetTaskTime", "expect": {"kind": "Function", "return_type": "ULINT"}},
    {"id": "page-linkalways", "kind": "page", "q": "Attribute 'linkalways'", "needs": 5, "expect": {"kind": "Page"}},
    {"id": "search-socket", "kind": "search", "q": "tcp client connect socket", "needs": 5, "expect": {"in_top": (["FB_SocketConnect"], 5)}},
    {"id": "search-file-lines", "kind": "search", "q": "read text file line by line", "needs": 5, "expect": {"in_top": (["FB_FileGets"], 5)}},
    {"id": "search-system-time", "kind": "search", "q": "system time of the PLC", "needs": 5, "expect": {"in_top": (["F_GetSystemTime", "NT_GetTime"], 5)}},
    {"id": "search-slave-state", "kind": "search", "q": "EtherCAT slave state", "needs": 5, "expect": {"in_top": (["FB_EcGetSlaveState"], 5)}},
    {"id": "search-ADSREAD", "kind": "search", "q": "ADSREAD", "expect": {"in_top": (["ADSREAD"], 3)}},
    {"id": "search-SMTP", "kind": "search", "q": "SMTP", "expect": {"in_top": (["FB_Smtp", "FB_SmtpV2", "FB_SmtpV3", "TwinCAT SMTP"], 5)}},
    {"id": "search-persistent", "kind": "search", "q": "persistent", "expect": {"in_top": (["Persistent Data"], 5)}},
    {"id": "search-0x745", "kind": "search", "q": "0x745", "needs": 5, "expect": {"code_first": 1861}},
    {"id": "search-error-1861", "kind": "search", "q": "error 1861", "needs": 5, "expect": {"code_first": 1861}},
    {"id": "symbols-F_GetTask", "kind": "symbols", "q": "F_GetTask", "expect": {"min_count": 3}},
    {"id": "code-fbRead", "kind": "code", "q": "fbRead(", "expect": {"min_count": 1}},
    {"id": "code-linkalways", "kind": "code", "q": "{attribute 'linkalways'}", "expect": {"min_count": 1}},
]


def infosys_checks(v: Verifier) -> None:
    db = L.locate(v.kb, "infosys")
    c0 = v.check("I0", "Infosys", "index state", "the index file, its format and state as every reader reports it")
    if not db.is_file():
        c0.add(WARN, str(db), "no Infosys index: nothing of it can be tested — its first build starts by itself in the "
                              "server; or tools\\infosys_local.py index")
        v.untested.append('infosys')
        return
    docs = v.docs_config
    local = None
    meta = IB.read_meta(db)
    lcid = str(meta.get("lcid") or "1033")
    try:
        if v.args.package or docs.mshc:
            pk = HV.find_packages(v.args.package or docs.mshc, lcid)
        else:                      # as a reader without the server finds it: the catalogs, the folder it was built from
            cur = IB.current_package(meta.get("package"), lcid)
            pk = [cur] if cur is not None else []
        if pk:
            local = HV.HelpViewerSource(pk[0], lcid)
    except Exception as exc:  # noqa: BLE001
        c0.notes.append(f"the Help Viewer package could not be opened ({exc}) — package checks are skipped")
    st = IB.state(db, local)
    v.meta["infosys"] = {k: st.get(k) for k in ("database", "state", "format", "format_expected", "pages", "books",
                                                "built", "built_by", "package", "package_date", "reason")}
    v.meta["infosys"]["package_found"] = str(local.path) if local is not None else None
    fmt = st.get("format") or 0
    if st["state"] == "complete":
        c0.add(PASS, str(db), st.get("message", ""))
    elif st["state"] in ("rebuild_needed", "outdated"):
        c0.add(WARN, str(db), f"{st['state']}: {st.get('reason') or st.get('message')} — the answers below come from "
                              "this index as it is; answers that need the current format are not judged")
    else:
        c0.add(FAIL, str(db), st.get("message", st["state"]))
        return
    con = v.ctx.con("infosys")
    pages = [dict(r) for r in con.execute(
        "SELECT rowid AS rid, url, book, page_id AS pid, title, kind, library, length(markdown) AS chars FROM pages")]
    n_meta = st.get("pages")
    if n_meta is not None and int(n_meta) != len(pages):
        c0.add(FAIL, "pages", f"the index's own count says {n_meta} pages, the pages table holds {len(pages)}")
    else:
        c0.add(PASS, "pages", f"{len(pages)} pages")
    books = sorted({p["book"] for p in pages})
    try:
        ix = INF.InfosysIndex(db_file=db, read_only=True, web_fallback=False, fetcher=_Offline(), local=local)
    except Exception as exc:  # noqa: BLE001
        c0.crash("InfosysIndex (the plugin's reader)", exc)
        return
    by_book: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pages:
        by_book[p["book"]].append(p)
    chosen = [p for b in books for p in v.pick(by_book[b], v.args.sample, b)]
    v.meta["infosys"]["pages_tested"] = len(chosen)
    v.meta["infosys"]["books_tested"] = len(books)
    url_set = {p["url"] for p in pages}
    sm: dict[str, str] = {}
    if local is not None:
        try:
            sm = ix.sitemap()
        except Exception as exc:  # noqa: BLE001
            c0.notes.append(f"the package's page list could not be read ({exc})")

    _i1_package(v, ix, local, sm, url_set, pages)
    _i2_pages(v, ix, chosen)
    _i3_skill_pages(v, chosen)
    _i4_helpids(v, ix, con, chosen)
    _i5_links(v, con, chosen, url_set, sm)
    _i6_title_search(v, con, chosen, pages)
    _i7_declarations(v, ix, con, chosen, fmt)
    _i8_symbol_search(v, ix, chosen)
    _i9_code_search(v, ix, con, chosen)
    _i10_books(v, ix, local, by_book)
    _i11_golden(v, ix, con, fmt)
    _i12_error_codes(v, con, fmt)


def _i1_package(v: Verifier, ix: Any, local: Any, sm: dict[str, str], url_set: set[str], pages: list[dict]) -> None:
    c = v.check("I1", "Infosys", "index vs Help Viewer package", "every page of the package (books the build takes) is "
                "in the index, and every indexed page is in the package")
    if local is None or not sm:
        c.add(SKIP, "package", "no Help Viewer package found on this PC (--package PATH names one)")
        return
    missing: Counter[str] = Counter()
    for url in sm:
        b = INF.book_of(url)
        if b and ix.book_allowed(b[0]) and url not in url_set:
            missing[b[0]] += 1
    for book, n in sorted(missing.items()):
        c.add(WARN, book, f"{n} page(s) of this book are in the package but not in the index")
    extra = [p["url"] for p in pages if p["url"] not in sm]
    for u in extra[:5000]:
        c.add(FAIL, u, "indexed, but not in the Help Viewer package (index built from another package?)")
    if not missing and not extra:
        c.add(PASS, "package", f"{len(sm)} package pages, {len(url_set)} indexed")
    try:
        c.notes.append(f"index_complete() = {ix.index_complete()}; index_note: {ix.index_note()}")
    except Exception as exc:  # noqa: BLE001
        c.crash("index_complete", exc)


def _i2_pages(v: Verifier, ix: Any, chosen: list[dict]) -> None:
    c = v.check("I2", "Infosys", "page by URL and book/id (infosys_get_page)", "the plugin's infosys_get_page returns "
                "the page by its URL (same title, kind and length as stored) and by 'book/id'")
    c.planned = len(chosen)
    for i, p in enumerate(chosen, 1):
        bad: list[str] = []
        try:
            r = c.timed(p["url"], ix.get_page, p["url"])
            if r.get("title") != p["title"]:
                bad.append(f"title {r.get('title')!r} ≠ {p['title']!r}")
            if r.get("kind") != p["kind"]:
                bad.append(f"kind {r.get('kind')!r} ≠ {p['kind']!r}")
            if r.get("chars") != (p["chars"] or 0):
                bad.append(f"chars {r.get('chars')} ≠ {p['chars']}")
            if r.get("indexed") is False:
                bad.append("answered from the package, not from the index")
        except INF.InfosysError as exc:
            bad.append(f"by URL refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c.crash(p["url"], exc)
            v.progress(c, i, len(chosen))
            continue
        ref = f"{p['book']}/{p['pid']}"
        try:
            r = c.timed(ref, ix.get_page, ref, max_chars=200)
            if r.get("url") != p["url"]:
                bad.append(f"'{ref}' opened {r.get('url')}")
        except INF.InfosysError as exc:
            bad.append(f"'{ref}' refused: {exc}"[:300])
        c.add(FAIL if bad else PASS, p["url"], "; ".join(bad))
        v.progress(c, i, len(chosen))


def _i3_skill_pages(v: Verifier, chosen: list[dict]) -> None:
    c = v.check("I3", "Infosys", "page by book/page id and URL (skill docs_page)", "the skill's docs_page opens the "
                "page by ref=<book>/<page id> (whole text) and by its Infosys URL")
    c.planned = len(chosen)
    for i, p in enumerate(chosen, 1):
        ref = f"{p['book']}/{p['pid']}"
        bad: list[str] = []
        for form, arg, size in (("ref=book/id", ref, 20000), ("ref=URL", p["url"], 200)):
            try:
                r = c.timed(arg, v.skill, "docs_page", ref=arg, max_chars=size)
                if r.get("ref") != ref:
                    bad.append(f"{form}: opened {r.get('ref')!r}")
                elif form == "ref=book/id":
                    if r.get("url") is None or not r["url"].endswith(f"/{p['book']}/{p['pid']}.html"):
                        bad.append(f"{form}: url {r.get('url')!r}")
                    if r.get("chars") != (p["chars"] or 0):
                        bad.append(f"{form}: chars {r.get('chars')} ≠ {p['chars']}")
            except KD.Err as exc:
                bad.append(f"{form}: {exc.code}: {exc.message}"[:300])
            except Exception as exc:  # noqa: BLE001
                bad.append(f"{form}: raised {type(exc).__name__}: {exc}"[:300])
        c.add(FAIL if bad else PASS, ref, "; ".join(bad))
        v.progress(c, i, len(chosen))


def _i4_helpids(v: Verifier, ix: Any, con: sqlite3.Connection, chosen: list[dict]) -> None:
    c = v.check("I4", "Infosys", "Help Viewer link (ms-xhelp)", "a page's ms-xhelp:///?Id=… link opens that page in "
                "infosys_get_page and in the skill's docs_page")
    try:
        ids: dict[str, list[str]] = defaultdict(list)
        for hid, url in con.execute("SELECT help_id, url FROM helpids"):
            ids[url].append(hid)
    except sqlite3.Error as exc:
        c.add(SKIP, "helpids", f"the index has no Help Viewer ids ({exc})")
        return
    todo = [(p, h) for p in chosen for h in ids.get(p["url"], [])[:1]]
    c.planned = len(todo)
    no_id = sum(1 for p in chosen if p["url"] not in ids)
    if no_id:
        c.notes.append(f"{no_id} tested page(s) have no Help Viewer id in the index")
    for i, (p, hid) in enumerate(todo, 1):
        link = f"ms-xhelp:///?Id={hid}"
        try:
            bad = []
            r = c.timed(link, ix.get_page, link, max_chars=200)
            if r.get("url") != p["url"]:
                bad.append(f"infosys_get_page → {r.get('url')}")
            r2 = c.timed(link, v.skill, "docs_page", ref=link, max_chars=200)
            if r2.get("url") != p["url"]:
                bad.append(f"docs_page → {r2.get('url')}")
            c.add(FAIL if bad else PASS, link, "; ".join(bad), page=p["url"])
        except (INF.InfosysError, KD.Err) as exc:
            c.add(FAIL, link, f"refused: {getattr(exc, 'message', exc)}"[:400], page=p["url"])
        except Exception as exc:  # noqa: BLE001
            c.crash(link, exc)
        v.progress(c, i, len(todo))


_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
_CONTENT = re.compile(r"^https://infosys\.beckhoff\.com/content/\d+/[^/]+/[^/#?]+\.html?$", re.I)


def _i5_links(v: Verifier, con: sqlite3.Connection, chosen: list[dict], url_set: set[str], sm: dict[str, str]) -> None:
    c = v.check("I5", "Infosys", "links inside pages", "every Infosys link in a page leads to a page of the index; a "
                "link to a page the index does not hold is a warning (Beckhoff links pages outside the Help Viewer "
                "package, and old page ids), split by whether its book is in the index; no unresolved ms-xhelp link")
    books = {u.split("/content/", 1)[1].split("/")[1] for u in url_set if "/content/" in u}
    c.planned = len(chosen)
    for i, p in enumerate(chosen, 1):
        md = con.execute("SELECT markdown FROM pages WHERE rowid = ?", (p["rid"],)).fetchone()[0] or ""
        missing: dict[str, list[str]] = defaultdict(list)
        for m in _LINK.finditer(md):
            u = m.group(1).split("#", 1)[0]
            if not _CONTENT.match(u) or u in url_set:
                continue                    # beckhoff.com product pages, images, other sites — or a page of the index
            book = u.split("/content/", 1)[1].split("/")[1].lower()
            if u in sm:
                missing["in the package, left out of the index"].append(u)
            elif book in books:
                missing["its book is indexed, the page is not" + ("" if sm else " (no package here to look in)")].append(u)
            else:
                missing["its book is not in the index" + (" nor in the package" if sm else "")].append(u)
        notes = [f"{len(set(us))} link(s) — {why}: {sorted(set(us))[:3]}" for why, us in missing.items()]
        if "ms-xhelp" in md.lower():
            notes.append("holds an ms-xhelp link the build could not resolve (its id is not in the package)")
        c.add(WARN if notes else PASS, p["url"], "; ".join(notes))
        v.progress(c, i, len(chosen))


def _i6_title_search(v: Verifier, con: sqlite3.Connection, chosen: list[dict], pages: list[dict]) -> None:
    c = v.check("I6", "Infosys", "search by the page's own title", f"infosys_search / docs_search (one code) with the "
                f"title as query: the page, a copy of it or a page of the same title in the top {v.top} — not judged "
                f"when more than {v.top} pages share the title")
    cp = v.check("I6b", "Infosys", "docs_search = infosys_search", "the skill's docs_search (its argument binding, "
                 "its output) lists the same pages in the same order as infosys_search — every 5th title of I6")
    same_title = Counter(title_key(p["title"]) for p in pages)
    c.planned = len(chosen)
    for i, p in enumerate(chosen, 1):
        q = norm_text(p["title"])
        if not q or not DS.has_words(q):
            c.add(SKIP, p["url"], "the title has no searchable word")
            v.progress(c, i, len(chosen))
            continue
        try:
            res = c.timed(q, INF.search_index, con, q, v.top)
            if i % 5 == 1:
                try:
                    sk = cp.timed(q, v.skill, "docs_search", query=q, limit=v.top)
                    a, b = [x["url"] for x in res["rows"]], [x["url"] for x in sk["items"]]
                    cp.add(PASS if a == b and sk["total"] == res["total"] else FAIL, q,
                           "" if a == b and sk["total"] == res["total"] else
                           f"infosys_search {len(a)} hit(s) of {res['total']}, docs_search {len(b)} of {sk['total']}; "
                           f"first differing: {next(((x, y) for x, y in zip(a, b) if x != y), None)}")
                except KD.Err as exc:
                    cp.add(FAIL, q, f"docs_search: {exc.code}: {exc.message}"[:300])
            rank = _rank_in(res, p)
            if rank is not None:
                c.add(PASS, p["url"])
            elif same_title[title_key(q)] > v.top:
                c.add(SKIP, p["url"], f"{same_title[title_key(q)]} pages have the title {q!r} (as the search reads "
                                      "it: words only)")
            else:
                wide = c.timed(q, INF.search_index, con, q, 50)
                r50 = _rank_in(wide, p)
                firsts = [x["title"] for x in res["rows"][:3]]
                c.add(FAIL, p["url"], f"title {q!r}: " + (f"rank {r50} (not in the top {v.top})" if r50 else
                                                         "not in the top 50") + f"; first: {firsts}",
                      query=q, rank=r50, mode=res.get("mode"))
        except DS.SearchError as exc:
            c.add(FAIL, p["url"], f"title {q!r} refused: {exc}")
        except Exception as exc:  # noqa: BLE001
            c.crash(p["url"], exc)
        v.progress(c, i, len(chosen))


def title_key(title: str | None) -> str:
    """A title as the search reads it: its words, lower case ('Example:' and 'Example' are one title)."""
    return " ".join(_TOKEN.findall((title or "").lower()))


def _rank_in(res: dict[str, Any], p: dict[str, Any]) -> int | None:
    """1-based position of the page in a search result: itself, a copy merged into a hit, or a hit of the same title."""
    want_title = title_key(p["title"])
    for n, r in enumerate(res["rows"], 1):
        if r["url"] == p["url"] or title_key(r["title"]) == want_title:
            return n
        for b, pid in res.get("copies", {}).get(r["rid"], []):
            if b == p["book"] and str(pid) == str(p["pid"]):
                return n
    return None


def _i7_declarations(v: Verifier, ix: Any, con: sqlite3.Connection, chosen: list[dict], fmt: int) -> None:
    c = v.check("I7", "Infosys", "symbol declaration (infosys_declaration + docs_declaration)",
                "every symbol page: the plugin's infosys_declaration and the skill's docs_declaration give the same "
                "answer — this page (or its copy), or 'ambiguous' with it among the candidates — with the same kind "
                "and return type")
    c2 = v.check("I7b", "Infosys", "declaration content", "a symbol page's stored declaration: header and members "
                 "parsed (function blocks, functions, structs, enums, interfaces); a function or property with its type "
                 "(format 5) — warnings: what Beckhoff's page gives the parser no hold on, or the parser misses")
    syms = [p for p in chosen if p["kind"] != "Page"]
    c.planned = c2.planned = len(syms)
    for i, p in enumerate(syms, 1):
        sym = INF.symbol_name(p["title"]) or norm_text(p["title"])
        try:
            docs = c.timed(sym, INF.symbol_documents, con, sym)
            mine = next((g for g in docs if any(r["rid"] == p["rid"] for r in g)), None)
            if mine is None:
                c.add(SKIP, p["url"], f"{p['title']!r}: the plugin's rule does not read this title as naming {sym!r}")
                v.progress(c, i, len(syms))
                continue
            urls = {r["url"] for r in mine}
            refs = {f"{r['book']}/{r['pid']}" for r in mine}
            bad: list[str] = []
            kinds: dict[str, Any] = {}
            try:
                d = c.timed(sym, ix.declaration, sym)
                if len(docs) > 1:
                    bad.append(f"infosys_declaration answered {d.get('url')} although {len(docs)} documents name {sym}")
                elif d.get("url") not in urls:
                    bad.append(f"infosys_declaration → {d.get('url')}, not this page or its copy")
                kinds["plugin"] = (d.get("kind"), d.get("return_type"))
            except INF.InfosysError as exc:
                if len(docs) > 1 and "ambiguous" in str(exc):
                    if not any(u in str(exc) for u in urls):
                        bad.append("infosys_declaration: ambiguous, but this page is not among its candidates")
                else:
                    bad.append(f"infosys_declaration refused: {exc}"[:300])
            try:
                s = c.timed(sym, v.skill, "docs_declaration", symbol=sym)
                if len(docs) > 1:
                    bad.append(f"docs_declaration answered {s.get('ref')} although {len(docs)} documents name {sym}")
                elif s.get("ref") not in refs:
                    bad.append(f"docs_declaration → {s.get('ref')}, not this page or its copy")
                kinds["skill"] = (s.get("kind"), s.get("return_type"))
            except KD.Err as exc:
                if len(docs) > 1 and exc.code == "ambiguous":
                    listed = exc.extra.get("candidates", [])
                    cands = {x.get("ref") for x in listed} | {y for x in listed for y in x.get("same_page_in", [])}
                    if not (cands & refs) and len(listed) >= len(docs):
                        bad.append("docs_declaration: ambiguous, but this page is not among its candidates")
                    elif not (cands & refs) and not exc.message.startswith(f"{len(docs)} pages"):
                        bad.append(f"docs_declaration: ambiguous, lists {len(listed)} candidates without this page "
                                   f"and does not say that {len(docs)} pages match: {exc.message[:120]}")
                else:
                    bad.append(f"docs_declaration refused: {exc.code}: {exc.message}"[:300])
            if len(kinds) == 2 and kinds["plugin"] != kinds["skill"]:
                bad.append(f"kind/return type differ: plugin {kinds['plugin']}, skill {kinds['skill']}")
            if bad:
                c.add(FAIL, p["url"], "; ".join(bad), symbol=sym)
            elif len(docs) > 1:
                c.add(PASS, p["url"])
                c.counts["ambiguous_names (pass)"] += 1
            else:
                c.add(PASS, p["url"])
            _declaration_content(c2, con, p, fmt)
        except Exception as exc:  # noqa: BLE001
            c.crash(p["url"], exc)
        v.progress(c, i, len(syms))


_NEEDS_DECL = {"FunctionBlock", "Function", "Struct", "Enum", "Interface", "Program", "Union", "Alias", "Method"}
_CALLABLE = {"Function", "Property"}         # a METHOD without a return type is common (METHOD Clear); a function's is not


def _declaration_content(c: Check, con: sqlite3.Connection, p: dict[str, Any], fmt: int) -> None:
    raw = con.execute("SELECT decl FROM pages WHERE rowid = ?", (p["rid"],)).fetchone()[0]
    try:
        d = json.loads(raw) if raw else None
    except ValueError:
        c.add(FAIL, p["url"], "the stored declaration is not valid JSON")
        return
    if not isinstance(d, dict):
        c.add(WARN, p["url"], f"{p['kind']} page without a stored declaration")
        return
    problems = []
    parsed = [k for k in ("declaration", "inputs", "outputs", "in_out", "members", "enum_values", "methods",
                          "properties", "description") if d.get(k)]
    if p["kind"] in _NEEDS_DECL and not parsed:
        problems.append("nothing of the declaration parsed (no header, members, methods or member table)")
    elif p["kind"] in _NEEDS_DECL and parsed == ["description"]:
        problems.append("only a member table (names and types) parsed, no declaration header")
    if p["kind"] in _CALLABLE and not d.get("return_type"):
        if fmt >= 5:
            problems.append("no return type" + (f" ({d['kind_note']})" if d.get("kind_note") else ""))
        else:
            c.add(SKIP, p["url"], f"no return type — this index is format {fmt}; format 5 parses headings and "
                                  "return-value tables")
            return
    if p["kind"] == "Enum" and not d.get("enum_values") and not d.get("declaration") and not d.get("description"):
        problems.append("enum without values")
    if problems and fmt < 5:
        problems.append(f"index format {fmt}")
    c.add(WARN if problems else PASS, p["url"], "; ".join(problems), kind=p["kind"])


def _i8_symbol_search(v: Verifier, ix: Any, chosen: list[dict]) -> None:
    c = v.check("I8", "Infosys", "symbol search (infosys_search_symbols)", f"the symbol's name finds its page in the "
                f"top {v.top} — not judged when more than {v.top} symbols contain the name")
    todo = []
    for p in chosen:
        if p["kind"] == "Page":
            continue
        m = INF._SYMBOL_TITLE.match(p["title"] or "")
        if m:
            todo.append((p, m.group(1)))
    c.planned = len(todo)
    for i, (p, name) in enumerate(todo, 1):
        try:
            r = c.timed(name, ix.search_symbols, name, None, None, v.top, False)
            urls = [x["url"] for x in r["items"]]
            if p["url"] in urls or any(x["name"].lower() == name.lower() and x["book"] != p["book"] and
                                       x["url"].rsplit("/", 1)[-1] == p["url"].rsplit("/", 1)[-1] for x in r["items"]):
                c.add(PASS, p["url"])
            elif r["count"] > v.top:
                c.add(SKIP, p["url"], f"{r['count']} symbols contain {name!r}")
            else:
                c.add(FAIL, p["url"], f"{name!r}: {r['count']} hit(s), this page not among them: "
                                      f"{[x['name'] for x in r['items'][:5]]}")
        except INF.InfosysError as exc:
            c.add(FAIL, p["url"], f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c.crash(p["url"], exc)
        v.progress(c, i, len(todo))


def _code_line(code: str) -> str | None:
    """The longest line of a code sample that is a useful search string (≥ 12 characters, has a word)."""
    lines = sorted({ln.replace("\xa0", " ").strip() for ln in (code or "").splitlines()}, key=len, reverse=True)
    for ln in lines:
        if 12 <= len(ln) <= 160 and re.search(r"[A-Za-z_]{3,}", ln):
            return ln
    return None


def _i9_code_search(v: Verifier, ix: Any, con: sqlite3.Connection, chosen: list[dict]) -> None:
    c = v.check("I9", "Infosys", "code search (infosys_search_code)", "a line of the page's first code sample finds "
                "the page — not judged when more than 50 samples hold the line")
    try:
        first: dict[int, str] = {}
        for rid, code in con.execute("SELECT page_rowid, code FROM code_blocks ORDER BY id"):
            first.setdefault(rid, code)
    except sqlite3.Error as exc:
        c.add(SKIP, "code_blocks", f"the index has no code sample table ({exc})")
        return
    todo = [(p, ln) for p in chosen if p["rid"] in first and (ln := _code_line(first[p["rid"]]))]
    c.planned = len(todo)
    for i, (p, ln) in enumerate(todo, 1):
        try:
            r = c.timed(ln, ix.search_code, ln, None, 50)
            if p["url"] in {x["url"] for x in r["items"]}:
                c.add(PASS, p["url"])
            elif isinstance(r["total_matches"], str) or r["total_matches"] > 50:
                c.add(SKIP, p["url"], f"{r['total_matches']} samples hold the line {ln[:60]!r}")
            else:
                c.add(FAIL, p["url"], f"line {ln[:80]!r}: {r['total_matches']} match(es), not this page", line=ln)
        except INF.InfosysError as exc:
            c.add(FAIL, p["url"], f"refused: {exc}"[:300], line=ln)
        except Exception as exc:  # noqa: BLE001
            c.crash(p["url"], exc)
        v.progress(c, i, len(todo))


def _i10_books(v: Verifier, ix: Any, local: Any, by_book: dict[str, list[dict]]) -> None:
    c = v.check("I10", "Infosys", "books (docs_books, infosys_books)", "every book: the skill lists it with its page "
                "count; infosys_books (with the package) lists it as indexed with the same count")
    try:
        r = c.timed("docs_books", v.skill, "docs_books")
        listed = {b["book"]: b["pages"] for b in r["books"]}
    except Exception as exc:  # noqa: BLE001
        c.crash("docs_books", exc)
        return
    pk: dict[str, dict[str, Any]] = {}
    if local is not None:
        try:
            pk = {b["book"]: b for b in c.timed("infosys_books", ix.books, None, None, 100000)["books"]}
        except Exception as exc:  # noqa: BLE001
            c.crash("infosys_books", exc)
    for book, rows in sorted(by_book.items()):
        bad = []
        if listed.get(book) != len(rows):
            bad.append(f"docs_books says {listed.get(book)} pages, the index holds {len(rows)}")
        if pk:
            b = pk.get(book)
            if b is None:
                bad.append("not listed by infosys_books (left out by the build's rules?)")
            elif b.get("indexed_pages") != len(rows) or not b.get("indexed"):
                bad.append(f"infosys_books: indexed={b.get('indexed')}, indexed_pages {b.get('indexed_pages')}")
        c.add(FAIL if bad else PASS, book, "; ".join(bad))


def _i11_golden(v: Verifier, ix: Any, con: sqlite3.Connection, fmt: int) -> None:
    c = v.check("I11", "Infosys", "known answers (field retests)", "questions whose right answer was checked against "
                "Beckhoff's pages in the retests — through the plugin's tools and the skill")
    c.planned = len(GOLDEN)
    for g in GOLDEN:
        if g.get("needs", 0) > fmt:
            c.add(SKIP, g["id"], f"needs Infosys format {g['needs']}; this index is format {fmt} (approve its rebuild)")
            continue
        try:
            absent = _golden_absent(con, g)
            if absent:
                c.add(SKIP, g["id"], absent, query=g["q"])
                continue
            problems = _golden_one(v, c, ix, con, g)
            c.add(FAIL if problems else PASS, g["id"], "; ".join(problems), query=g["q"])
        except Exception as exc:  # noqa: BLE001
            c.crash(g["id"], exc)


def _golden_absent(con: sqlite3.Connection, g: dict[str, Any]) -> str | None:
    """Why the index cannot answer this question at all (the book with the answer is not in it) — read straight from
    the pages, not through the code under test; None when the answer is there to be found."""
    e, q = g["expect"], g["q"]

    def titled(*names: str) -> int:
        n = 0
        for t in names:
            n += con.execute("SELECT COUNT(*) FROM pages WHERE title = ? COLLATE NOCASE OR title LIKE ? ESCAPE '\\'",
                             (t, t.replace("_", "\\_").replace("%", "\\%") + " %")).fetchone()[0]
        return n
    if g["kind"] in ("declaration", "page"):
        return None if titled(q) else f"no page of this index is titled {q!r} (its book is not indexed)"
    if g["kind"] == "search" and "in_top" in e:
        return None if titled(*e["in_top"][0]) else f"no page of this index is titled {e['in_top'][0]}"
    if g["kind"] == "search" and "code_first" in e:
        return None if titled("ADS Return Codes") else "no page of this index is titled 'ADS Return Codes'"
    if g["kind"] == "symbols":
        n = con.execute("SELECT COUNT(*) FROM pages WHERE kind != 'Page' AND title LIKE ? ESCAPE '\\'",
                        (q.replace("_", "\\_") + "%",)).fetchone()[0]
        return None if n >= e["min_count"] else f"only {n} symbol page(s) of this index begin with {q!r}"
    if g["kind"] == "code":
        n = con.execute("SELECT COUNT(*) FROM code_blocks WHERE instr(replace(code, char(160), ' '), ?) > 0",
                        (q,)).fetchone()[0]
        return None if n >= e["min_count"] else f"only {n} code sample(s) of this index hold {q!r}"
    return None


def _golden_one(v: Verifier, c: Check, ix: Any, con: sqlite3.Connection, g: dict[str, Any]) -> list[str]:
    e, q, out = g["expect"], g["q"], []
    if g["kind"] == "declaration":
        for reader in ("plugin", "skill"):
            try:
                if reader == "plugin":
                    d = c.timed(q, ix.declaration, q, g.get("library"))
                    lib, decl_text = d.get("library"), d.get("declaration") or ""
                    inputs = [x.get("name") for x in d.get("inputs") or []]
                else:
                    d = c.timed(q, v.skill, "docs_declaration", symbol=q,
                                book=("tcplclib_" + g["library"].lower()) if g.get("library") else None)
                    lib = None
                    decl_text = d.get("declaration") or "\n".join(d.get("declaration_blocks") or [])
                    inputs = re.findall(r"^\s*([A-Za-z_]\w*)\s*:", decl_text, re.M)
                if "ambiguous" in e:
                    out.append(f"{reader}: answered {d.get('url') or d.get('ref')} instead of 'ambiguous'")
                    continue
                if e.get("kind") and d.get("kind") != e["kind"]:
                    out.append(f"{reader}: kind {d.get('kind')!r}, expected {e['kind']!r}")
                if e.get("return_type") and str(d.get("return_type") or "").replace(" ", "").upper() != \
                        e["return_type"].replace(" ", "").upper():
                    out.append(f"{reader}: return type {d.get('return_type')!r}, expected {e['return_type']!r}")
                if e.get("library") and lib is not None and lib.lower() != e["library"].lower():  # IEC: no case
                    out.append(f"{reader}: library {lib!r}, expected {e['library']!r}")
                if e.get("declaration_has") and e["declaration_has"] not in decl_text:
                    out.append(f"{reader}: the declaration does not contain {e['declaration_has']!r}")
                missing = [x for x in e.get("inputs", []) if x.lower() not in {str(y).lower() for y in inputs}]
                if missing:
                    out.append(f"{reader}: inputs {missing} missing")
            except INF.InfosysError as exc:
                if "ambiguous" in e and "ambiguous" in str(exc):
                    gone = [x for x in e["ambiguous"] if x.lower() not in str(exc).lower()]
                    if not gone:
                        continue
                    out.append(f"{reader}: ambiguous, but {gone} not among the candidates: {exc}"[:400])
                    continue
                out.append(f"{reader}: refused: {exc}"[:300])
            except KD.Err as exc:
                if "ambiguous" in e and exc.code == "ambiguous":
                    text = json.dumps(exc.extra, default=str).lower()
                    gone = [x for x in e["ambiguous"] if x.lower() not in text]
                    if gone:
                        out.append(f"{reader}: ambiguous, but {gone} not among the candidates")
                    continue
                out.append(f"{reader}: {exc.code}: {exc.message}"[:300])
    elif g["kind"] == "page":
        urls = [r[0] for r in con.execute("SELECT url FROM pages WHERE title = ? COLLATE NOCASE", (q,))]
        for url in urls:
            r = c.timed(q, ix.get_page, url, max_chars=200)
            if e.get("kind") and r.get("kind") != e["kind"]:
                out.append(f"{url}: kind {r.get('kind')!r}, expected {e['kind']!r}")
    elif g["kind"] == "search":
        res = c.timed(q, INF.search_index, con, q, 10)
        titles = [r["title"] for r in res["rows"]]
        if "in_top" in e:
            want, n = e["in_top"]
            if not any(t in want for t in titles[:n]):
                out.append(f"none of {want} in the top {n}: {titles[:n]}")
        if "code_first" in e:
            body = con.execute("SELECT markdown FROM pages WHERE rowid = ?", (res["rows"][0]["rid"],)).fetchone()[0] \
                if res["rows"] else ""
            if not res.get("code") or res["code"].get("value") != e["code_first"]:
                out.append(f"the query was not read as code {e['code_first']}")
            elif not res["rows"] or DS.definition_score(body, e["code_first"]) <= 0:
                out.append(f"the first hit ({titles[:1]}) does not define code {e['code_first']} in a table row")
        sk = c.timed(q, v.skill, "docs_search", query=q, limit=10)
        if [x["title"] for x in sk["items"]] != titles:
            out.append("docs_search (skill) ranks differently from infosys_search: "
                       f"{[x['title'] for x in sk['items']][:5]} vs {titles[:5]}")
    elif g["kind"] == "symbols":
        r = c.timed(q, ix.search_symbols, q, None, None, 10, False)
        if r["count"] < e["min_count"]:
            out.append(f"{r['count']} symbol(s), expected at least {e['min_count']}")
    elif g["kind"] == "code":
        r = c.timed(q, ix.search_code, q, None, 10)
        n = r["total_matches"] if isinstance(r["total_matches"], int) else 2000
        if n < e["min_count"]:
            out.append(f"{n} sample(s), expected at least {e['min_count']}")
    return out


_HEX_CELL = re.compile(r"0x([0-9A-Fa-f]{1,8})")


def code_rows(md: str) -> Iterable[tuple[int, str]]:
    """(decimal, hex digits) of every table row of a page that writes one code in hex and decimal in adjacent cells,
    in either order — the rows an error-code table is made of."""
    for line in (md or "").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [x.strip().strip("`*").strip() for x in line.strip().strip("|").split("|")]
        for a, b in zip(cells, cells[1:]):
            for h, d in ((a, b), (b, a)):
                m = _HEX_CELL.fullmatch(h)
                if m and d.isdigit() and int(m.group(1), 16) == int(d):
                    yield int(d), m.group(1)


def _i12_error_codes(v: Verifier, con: sqlite3.Connection, fmt: int) -> None:
    c = v.check("I12", "Infosys", "error codes (ADS return codes)", "every code of the ADS return-code table: '0x…' and "
                "'error <decimal>' are read as that code and the first hit is a page whose table row defines it")
    if fmt < 5:
        c.add(SKIP, "codes", f"needs Infosys format 5 (code tables first); this index is format {fmt}")
        return
    rows = con.execute("SELECT rowid, url, markdown FROM pages WHERE title LIKE 'ADS Return Codes' ESCAPE '\\' "
                       "COLLATE NOCASE").fetchall()
    codes: dict[int, str] = {}
    for r in rows:
        for d, h in code_rows(r["markdown"]):
            if d >= 16:                       # 0x0–0xF: one digit, the same in hex and decimal, every page has those
                codes.setdefault(d, h)
    if not codes:
        c.add(SKIP, "codes", "no page titled 'ADS Return Codes' with hex/decimal rows")
        return
    todo = v.pick(sorted(codes.items()), max(20, v.args.sample * 3), "I12")
    c.planned = len(todo)
    for i, (dec, h) in enumerate(todo, 1):
        for q in (f"0x{h}", f"error {dec}"):
            try:
                res = c.timed(q, INF.search_index, con, q, 5)
                body = con.execute("SELECT markdown FROM pages WHERE rowid = ?", (res["rows"][0]["rid"],)).fetchone()[0] \
                    if res["rows"] else ""
                if not res.get("code") or res["code"].get("value") != dec:
                    c.add(FAIL, q, f"not read as code {dec}")
                elif not res["rows"] or DS.definition_score(body, dec) <= 0:
                    c.add(FAIL, q, f"first hit {[x['title'] for x in res['rows'][:1]]} does not define {dec}")
                else:
                    c.add(PASS, q)
            except Exception as exc:  # noqa: BLE001
                c.crash(q, exc)
        v.progress(c, i, len(todo))


# ====================================================================================================== manuals
def manuals_checks(v: Verifier) -> None:
    db = L.locate(v.kb, "manuals")
    folder = v.kb / "manuals"
    c0 = v.check("M0", "Manuals", "index state", "the manuals index, its page-text format and its PDFs as every reader "
                 "reports them")
    if not db.is_file():
        c0.add(WARN, str(db), "no manuals index: nothing of it can be tested — tools\\manuals_db.py update, or "
                              "kb_rebuild(index='manuals')")
        v.untested.append('manuals')
        return
    st = KD.manuals_state(db, folder)
    v.meta["manuals"] = {k: st.get(k) for k in ("database", "folder", "state", "manuals", "pages", "pdfs_in_folder",
                                                "text_format", "text_format_expected", "last_update", "last_result")}
    if st["state"] == "complete":
        c0.add(PASS, str(db), st.get("message", ""))
    else:
        c0.add(WARN, str(db), f"{st['state']}: {st.get('message')}")
    con = v.ctx.con("manuals")
    docs = [dict(r) for r in con.execute("SELECT * FROM doc ORDER BY name")]

    c1 = v.check("M1", "Manuals", "your PDFs vs the index", "every PDF in manuals\\ is indexed and unchanged since "
                 "(size, time, SHA-256), every indexed manual still exists")
    for name in st.get("not_indexed") or []:
        c1.add(WARN, name, "a PDF in manuals\\ that is not indexed — tools\\manuals_db.py update, or kb_rebuild(index='manuals')")
    for name in st.get("changed_since_index") or []:
        c1.add(WARN, name, "changed since it was indexed (size or time) — the index holds the old text")
    for name in st.get("indexed_but_gone") or []:
        c1.add(WARN, name, "indexed, but no longer in manuals\\")
    for d in docs:
        p = Path(d["path"])
        if not p.is_file():
            continue                                              # listed above
        if v.args.no_hash:
            c1.add(PASS, d["name"])
            continue
        try:
            same = c1.timed(d["name"], sha256_file, p) == d["sha256"]
            c1.add(PASS if same else FAIL, d["name"], "" if same else "the file's content differs from the indexed "
                                                                      "one (SHA-256) although its size and time match")
        except OSError as exc:
            c1.add(FAIL, d["name"], f"cannot be read: {exc}")
    for name in st.get("no_text_layer") or []:
        c1.add(WARN, name, "no text layer (a scanned PDF?): nothing of it can be searched")

    c2 = v.check("M2", "Manuals", "manual list and contents (manual_docs, manual_toc)", "every manual is listed with "
                 "its pages; its contents open by its file name, every chapter on a page that exists")
    try:
        listed = {m["doc"]: m for m in c2.timed("manual_docs", v.skill, "manual_docs")["manuals"]}
    except Exception as exc:  # noqa: BLE001
        c2.crash("manual_docs", exc)
        listed = {}
    page_rows = Counter(r[0] for r in con.execute("SELECT doc FROM page"))
    text_rows = Counter(r[0] for r in con.execute("SELECT doc FROM page WHERE text != ''"))
    toc_by: dict[int, set[str]] = defaultdict(set)
    for d in docs:
        bad, notes = [], []
        m = listed.get(d["name"])
        if m is None:
            bad.append("not listed by manual_docs")
        elif m["pages"] != d["pages"]:
            bad.append(f"manual_docs: {m['pages']} pages, index {d['pages']}")
        if page_rows[d["id"]] != (d["pages"] or 0):             # the builder stores every page, with or without text
            bad.append(f"{page_rows[d['id']]} page rows for {d['pages']} pages")
        if text_rows[d["id"]] != (d["text_pages"] or 0):
            bad.append(f"{text_rows[d['id']]} pages with text, the manual says {d['text_pages']}")
        try:
            t = c2.timed(d["name"], v.skill, "manual_toc", doc=d["name"], depth=6)
            off = [e for e in t["toc"] if not 1 <= int(e["page"] or 0) <= (d["pages"] or 0)]
            if off:
                bad.append(f"{len(off)} chapter(s) on pages outside 1–{d['pages']}: {[e['title'] for e in off[:3]]}")
            toc_by[d["id"]] = {norm_text(e["title"]).lower() for e in t["toc"]}
            if not t["toc"]:
                notes.append("no chapters (no bookmarks and no headings found)")
        except KD.Err as exc:
            bad.append(f"manual_toc doc={d['name']!r}: {exc.code}: {exc.message}"[:300])
        except Exception as exc:  # noqa: BLE001
            c2.crash(d["name"], exc)
            continue
        c2.add(FAIL if bad else WARN if notes else PASS, d["name"], "; ".join(bad + notes))

    rows = [dict(r) for r in con.execute("SELECT p.doc, p.page, p.section, p.text, d.name FROM page p JOIN doc d ON "
                                         "d.id = p.doc ORDER BY d.name, p.page")]
    by_doc: dict[int, list[dict]] = defaultdict(list)
    for r in rows:
        by_doc[r["doc"]].append(r)
    chosen = [r for d in docs for r in v.pick(by_doc[d["id"]], v.args.sample, d["name"])]
    v.meta["manuals"]["pages_tested"] = len(chosen)

    c3 = v.check("M3", "Manuals", "every page (manual_page)", "each page opens by the manual's name with the stored "
                 "text and section")
    c3.planned = len(chosen)
    for i, r in enumerate(chosen, 1):
        subj = f"{r['name']} p{r['page']}"
        try:
            out = c3.timed(subj, v.skill, "manual_page", doc=r["name"], pages=r["page"], max_chars=20000)
            pg = out["pages"][0] if out["pages"] else {}
            text = r["text"] or ""
            got = pg.get("text") or ""
            ok = got == text or (pg.get("cut") and text.startswith(got))
            bad = [] if ok else [f"text differs ({len(got)} vs {len(text)} chars)"]
            if (pg.get("section") or None) != (r["section"] or None):
                bad.append(f"section {pg.get('section')!r} ≠ {r['section']!r}")
            c3.add(FAIL if bad else PASS, subj, "; ".join(bad))
        except KD.Err as exc:
            c3.add(FAIL, subj, f"{exc.code}: {exc.message}"[:300])
        except Exception as exc:  # noqa: BLE001
            c3.crash(subj, exc)
        v.progress(c3, i, len(chosen))

    c4 = v.check("M4", "Manuals", "search finds each page (manual_search)", f"an exact phrase of the page around one "
                 f"of its rarest words finds that manual and page in the top {v.top} (up to 3 phrases are tried) — not "
                 f"judged when every phrase is on more pages than that")
    df = fts_df(con)
    c4.planned = len(chosen)
    for i, r in enumerate(chosen, 1):
        subj = f"{r['name']} p{r['page']}"
        phrases = distinctive_phrases(r["text"] or "", df)
        if not phrases:
            c4.add(SKIP, subj, "too little text for a phrase")
            v.progress(c4, i, len(chosen))
            continue
        tried: list[str] = []
        status, detail = SKIP, ""
        try:
            for phrase in phrases:
                q = f'"{phrase}"'
                out = c4.timed(q, v.skill, "manual_search", query=q, limit=v.top)
                if any(x["doc"] == r["name"] and x["page"] == r["page"] for x in out["items"]):
                    status, detail = PASS, ""
                    break
                if out["total"] <= v.top:
                    status = FAIL
                    detail = (f"{q}: {out['total']} hit(s), not this page: "
                              f"{[(x['doc'], x['page']) for x in out['items'][:3]]}")
                    break
                tried.append(f"{q} on {out['total']} pages")
            if status == SKIP:
                detail = "every phrase is on more pages than listed: " + "; ".join(tried)
            c4.add(status, subj, detail)
        except KD.Err as exc:
            c4.add(FAIL, subj, f"{exc.code}: {exc.message}"[:300])
        except Exception as exc:  # noqa: BLE001
            c4.crash(subj, exc)
        v.progress(c4, i, len(chosen))

    c5 = v.check("M5", "Manuals", "product codes (manual_product)", "every product code the index found names the "
                 "manuals and pages it is on")
    prod: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for code, doc, page in con.execute("SELECT p.code, d.name, p.page FROM product p JOIN doc d ON d.id = p.doc"):
        prod[code][doc].add(int(page))
    codes = v.pick(sorted(prod), max(30, v.args.sample * 5), "M5")
    c5.planned = len(codes)
    for i, code in enumerate(codes, 1):
        try:
            out = c5.timed(code, v.skill, "manual_product", code=code, limit=50)
            got = {x["doc"]: set(x["pages"]) for x in out["items"]}
            bad = []
            for doc, pgs in prod[code].items():
                if doc not in got:
                    bad.append(f"{doc} not listed")
                elif not pgs <= got[doc]:
                    bad.append(f"{doc}: pages {sorted(pgs - got[doc])[:5]} missing")
            c5.add(FAIL if bad else PASS, code, "; ".join(bad))
        except KD.Err as exc:
            c5.add(FAIL, code, f"{exc.code}: {exc.message}"[:300])
        except Exception as exc:  # noqa: BLE001
            c5.crash(code, exc)
        v.progress(c5, i, len(codes))

    c6 = v.check("M6", "Manuals", "page sections vs chapters", "a page's section is a chapter of the manual's contents "
                 "(the last part of its section path)")
    for r in chosen:
        subj = f"{r['name']} p{r['page']}"
        sec = norm_text((r["section"] or "").split(">")[-1]).lower()
        if not sec:
            c6.add(SKIP, subj, "no section (before the first chapter, or no chapters)")
        elif not toc_by.get(r["doc"]):
            c6.add(SKIP, subj, "the manual has no contents")
        elif sec in toc_by[r["doc"]]:
            c6.add(PASS, subj)
        else:
            c6.add(WARN, subj, f"section {r['section']!r} is not a chapter title of the contents")


# ====================================================================================================== ESI
def esi_checks(v: Verifier) -> None:
    db = L.index_file(v.kb, "esi")
    c0 = v.check("E0", "ESI", "index state", "the ESI index, its format and its source folders as every reader reports them")
    if not db.is_file():
        c0.add(WARN, str(db), "no ESI index: nothing of it can be tested — its first build starts by itself in the "
                              "server (or at the first hw_* call); or tools\\esi_db.py update")
        v.untested.append('esi')
        return
    st = ESI.state(db, v.kb)
    v.meta["esi"] = {k: st.get(k) for k in ("database", "state", "format", "format_expected", "devices", "types",
                                            "modules", "dictionaries", "objects", "diag_messages", "files", "built",
                                            "built_by", "reason")}
    if st["state"] == "complete":
        c0.add(PASS, str(db), st.get("message", ""))
    else:
        c0.add(WARN, str(db), f"{st['state']}: {st.get('reason') or st.get('message')}")
    for k in ("missing_refs", "errors"):
        if st.get(k):
            c0.add(WARN, k, f"{st[k]['count']}: {st[k]['first']}")
    E = ESI.EsiIndex(db)
    con = sqlite3.connect(ESI.ro_uri(db), uri=True)
    con.row_factory = sqlite3.Row
    try:
        _esi_files(v, con, db)
        _esi_devices(v, E, con)
        _esi_objects(v, E, con)
        _esi_diag(v, E, con)
        _esi_modules(v, E, con)
    finally:
        con.close()


def _esi_files(v: Verifier, con: sqlite3.Connection, db: Path) -> None:
    c = v.check("E1", "ESI", "ESI files vs the index", "every .xml the index was built from is unchanged (size, time), "
                "no ESI file was added since, none failed to parse")
    rows = [dict(r) for r in con.execute("SELECT path, source, kind, size, mtime, error FROM files")]
    known = {os.path.normcase(r["path"]): r for r in rows}
    for r in rows:
        p = Path(r["path"])
        if r["error"]:
            c.add(FAIL, r["path"], f"failed to parse when indexed: {r['error']}"[:300])
            continue
        try:
            stt = p.stat()
        except OSError:
            c.add(WARN, r["path"], "indexed, but no longer on disk")
            continue
        if stt.st_size != r["size"] or abs(stt.st_mtime - float(r["mtime"] or 0)) > 0.01:
            c.add(WARN, r["path"], "changed since it was indexed (size or time)")
        else:
            c.add(PASS, r["path"])
    try:
        folders = ESI.source_folders(v.kb, ESI.recorded_extra(db), ESI.recorded_twincat(db))
        for p, _src in ESI.xml_files(folders):
            if os.path.normcase(str(p)) not in known and ESI.root_kind(p) is not None:
                c.add(WARN, str(p), "an ESI file the index does not hold (added since it was built)")
    except Exception as exc:  # noqa: BLE001
        c.crash("ESI folders", exc)


def _same_answer(v: Verifier, c: Check, subject: str, plugin: dict[str, Any], skill: str, **kw: Any) -> None:
    """The skill's hw_* command answers as the plugin's reader (the same esi_index call; the skill adds 'source')."""
    try:
        out = c.timed(subject, v.skill, skill, **kw)
    except KD.Err as exc:
        c.add(FAIL, subject, f"{skill}: {exc.code}: {exc.message}"[:300])
        return
    out = {k: val for k, val in out.items() if k != "source"}
    a = json.dumps(plugin, sort_keys=True, default=str)
    b = json.dumps(out, sort_keys=True, default=str)
    c.add(PASS if a == b else FAIL, subject, "" if a == b else f"{skill} answers differently from the plugin's "
                                                               f"reader ({len(b)} vs {len(a)} characters of JSON)")


PARITY_EVERY = 10          # the skill's hw_* answers are compared for every 10th item of E2 / E3 / E5 / E6 / E7


def rev_int(value: Any) -> int | None:
    """A revision as hw_* write it ('#x00100000', a number, or None for a device without RevisionNo) as a number."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    t = str(value).strip().lower()
    return int(t[2:], 16) if t.startswith(("#x", "0x")) else int(t)


def rev_arg(value: int | None) -> str | None:
    return None if value is None else f"#x{value:08X}"


def _esi_devices(v: Verifier, E: Any, con: sqlite3.Connection) -> None:
    c = v.check("E2", "ESI", "device by type (hw_lookup)", "every device type: hw_lookup lists every revision the "
                "index holds; the newest in full has as many PDO entries and CoE objects as counted")
    c3 = v.check("E3", "ESI", "device by product code (hw_lookup #x…)", "every product code finds its device type(s)")
    c4 = v.check("E4", "ESI", "ESI full-text search (hw_search)", "the device type as a phrase finds the device")
    cp = v.check("E8", "ESI", "skill hw_* = plugin", f"the skill's hw_lookup / hw_search / hw_object / hw_diag / "
                 f"hw_module give the plugin reader's answer — every {PARITY_EVERY}th item of E2–E7")
    v.esi_parity = cp
    revs: dict[str, list[dict]] = defaultdict(list)
    for r in con.execute("SELECT type, revision, product_code, pdo_entries, pdo_count, objects, dict_id FROM devices"):
        revs[r["type"].lower()].append(dict(r))
    types = v.pick(sorted(revs), max(50, v.args.sample * 15), "E2")
    c.planned = len(types)
    for i, t in enumerate(types, 1):
        name = revs[t][0]["type"]
        try:
            r = c.timed(name, E.lookup, name, 30, None, "summary")
            if i % PARITY_EVERY == 1:
                _same_answer(v, cp, name, r, "hw_lookup", query=name, limit=30, detail="summary")
            bad = []
            want = {x["revision"] for x in revs[t]}
            got = {rev_int(d.get("revision")) for d in r["devices"] if d["type"].lower() == t}
            if r["matches"] != len(revs[t]):
                bad.append(f"matches {r['matches']}, the index holds {len(revs[t])} revision(s)")
            if len(want) <= 30 and got != want:
                bad.append(f"revisions listed {sorted(map(str, got))[:5]}, the index holds {sorted(map(str, want))[:5]}")
            full = c.timed(name, E.lookup, name, 1, None, "newest")["devices"][0]
            newest = next((x for x in revs[t] if x["revision"] == rev_int(full.get("revision"))), None)
            n_entries = sum(len(p.get("entries") or []) for p in full.get("pdo") or [])
            if newest is None:
                bad.append(f"'newest' answered revision {full.get('revision')}, which the index does not hold")
            else:
                if full.get("pdo") is not None and newest["pdo_count"] is not None and \
                        len(full["pdo"]) != newest["pdo_count"]:
                    bad.append(f"{len(full['pdo'])} PDOs listed, counted {newest['pdo_count']}")
                if newest["pdo_entries"] is not None and n_entries and n_entries != newest["pdo_entries"]:
                    bad.append(f"{n_entries} PDO entries listed, counted {newest['pdo_entries']}")
                if newest["dict_id"] and len(full.get("coe_objects") or []) != (newest["objects"] or 0):
                    bad.append(f"{len(full.get('coe_objects') or [])} CoE objects listed, counted {newest['objects']}")
            c.add(FAIL if bad else PASS, name, "; ".join(bad))
        except ESI.EsiError as exc:
            c.add(FAIL, name, f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c.crash(name, exc)
        try:
            q = '"' + " ".join(_TOKEN.findall(name)) + '"'
            s = c4.timed(q, E.search, q, ["device"], 50)
            if i % PARITY_EVERY == 1:
                _same_answer(v, cp, q, s, "hw_search", query=q, kinds="device", limit=50)
            refs = json.dumps(s, default=str).lower()
            c4.add(PASS if name.lower() in refs else FAIL, name, "" if name.lower() in refs else f"{q}: not found")
        except ESI.EsiError as exc:
            c4.add(FAIL, name, f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c4.crash(name, exc)
        v.progress(c, i, len(types))
    codes: dict[int, set[str]] = defaultdict(set)
    for row in con.execute("SELECT product_code, type FROM devices"):
        codes[row["product_code"]].add(row["type"].lower())
    pcs = v.pick(sorted(k for k in codes if k is not None), max(50, v.args.sample * 15), "E3")
    c3.planned = len(pcs)
    for i, pc in enumerate(pcs, 1):
        q = f"#x{pc:08X}"
        try:
            r = c3.timed(q, E.lookup, q, 30, None, "summary")
            if i % PARITY_EVERY == 1:
                _same_answer(v, v.esi_parity, q, r, "hw_lookup", query=q, limit=30, detail="summary")
            got = {d["type"].lower() for d in r["devices"]} | {x.lower() for x in r.get("types") or []}
            want = {x for x in codes[pc] if x}
            c3.add(PASS if want <= got or r["matches"] > 30 else FAIL, q,
                   "" if want <= got or r["matches"] > 30 else f"types {sorted(want - got)[:5]} missing")
        except ESI.EsiError as exc:
            c3.add(FAIL, q, f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c3.crash(q, exc)
        v.progress(c3, i, len(pcs))


def _esi_objects(v: Verifier, E: Any, con: sqlite3.Connection) -> None:
    c = v.check("E5", "ESI", "CoE object (hw_object)", "every distinct CoE object, through one device that has it: "
                "index and name as stored, decoded without error")
    rows = con.execute(
        "SELECT o.obj_id, o.idx, ob.name, (SELECT d.type FROM devices d WHERE d.dict_id = o.dict_id ORDER BY d.revision "
        "DESC LIMIT 1) AS type, (SELECT d.revision FROM devices d WHERE d.dict_id = o.dict_id ORDER BY d.revision DESC "
        "LIMIT 1) AS revision FROM dict_obj o JOIN obj ob ON ob.id = o.obj_id GROUP BY o.obj_id").fetchall()
    todo = v.pick([dict(r) for r in rows if r["type"]], max(200, v.args.sample * 50), "E5")
    c.planned = len(todo)
    for i, r in enumerate(todo, 1):
        idx = f"#x{r['idx']:04X}"
        subj = f"{r['type']} {idx}"
        try:
            out = c.timed(subj, E.object, r["type"], idx, rev_arg(r["revision"]))
            if i % PARITY_EVERY == 1:
                _same_answer(v, v.esi_parity, subj, out, "hw_object", device=r["type"], index=idx,
                             revision=rev_arg(r["revision"]))
            bad = []
            if str(out.get("index", "")).upper() != idx.upper():
                bad.append(f"index {out.get('index')}")
            if (out.get("name") or "") != (r["name"] or ""):
                bad.append(f"name {out.get('name')!r} ≠ {r['name']!r}")
            c.add(FAIL if bad else PASS, subj, "; ".join(bad))
        except ESI.EsiError as exc:
            c.add(FAIL, subj, f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c.crash(subj, exc)
        v.progress(c, i, len(todo))


def _esi_diag(v: Verifier, E: Any, con: sqlite3.Connection) -> None:
    c = v.check("E6", "ESI", "diagnosis message (hw_diag)", "every diagnosis text id of every catalogue, asked with a "
                "device that has the catalogue: found, with the stored message")
    rows = con.execute(
        "SELECT m.set_id, m.text_id, g.message, (SELECT d.type FROM devices d WHERE d.diag_set = m.set_id ORDER BY "
        "d.revision DESC LIMIT 1) AS type, (SELECT d.revision FROM devices d WHERE d.diag_set = m.set_id ORDER BY "
        "d.revision DESC LIMIT 1) AS revision FROM diag_msg m JOIN msg g ON g.id = m.msg_id").fetchall()
    todo = v.pick([dict(r) for r in rows if r["type"]], max(200, v.args.sample * 50), "E6")
    c.planned = len(todo)
    for i, r in enumerate(todo, 1):
        code = f"#x{r['text_id']:04X}"
        subj = f"{r['type']} {code}"
        try:
            out = c.timed(subj, E.diag, code, r["type"], rev_arg(r["revision"]))
            if i % PARITY_EVERY == 1:
                _same_answer(v, v.esi_parity, subj, out, "hw_diag", code=code, device=r["type"],
                             revision=rev_arg(r["revision"]))
            msgs = [m.get("message") for m in out.get("messages") or []]
            ok = out.get("found", 0) >= 1 and (r["message"] in msgs or not r["message"])
            c.add(PASS if ok else FAIL, subj, "" if ok else f"found {out.get('found')}, messages {msgs[:3]}, "
                                                            f"stored {r['message']!r}")
        except ESI.EsiError as exc:
            c.add(FAIL, subj, f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c.crash(subj, exc)
        v.progress(c, i, len(todo))


def _esi_modules(v: Verifier, E: Any, con: sqlite3.Connection) -> None:
    c = v.check("E7", "ESI", "module by ident (hw_module)", "every ModuleIdent finds its module(s)")
    idents: dict[int, set[str]] = defaultdict(set)
    for r in con.execute("SELECT ident, type FROM modules"):
        if r["ident"] is not None:
            idents[r["ident"]].add((r["type"] or "").lower())
    todo = v.pick(sorted(idents), max(100, v.args.sample * 10), "E7")
    c.planned = len(todo)
    for i, ident in enumerate(todo, 1):
        q = f"#x{ident:08X}"
        try:
            out = c.timed(q, E.module, q)
            if i % PARITY_EVERY == 1:
                _same_answer(v, v.esi_parity, q, out, "hw_module", ident=q)
            got = {(m.get("type") or "").lower() for m in out.get("modules") or []}
            ok = out.get("found", 0) >= 1 and idents[ident] <= got
            c.add(PASS if ok else FAIL, q, "" if ok else f"found {out.get('found')}, types {sorted(got)[:5]}, "
                                                         f"stored {sorted(idents[ident])[:5]}")
        except ESI.EsiError as exc:
            c.add(FAIL, q, f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c.crash(q, exc)
        v.progress(c, i, len(todo))


# ====================================================================================================== projects
KNOWN_ERROR_SAMPLES = ["ADS error 1861", "ADSError 1861", "0x745", "ADS-Error: 0x00000745", "error code 1861"]


def projects_checks(v: Verifier) -> None:
    db = L.locate(v.kb, "knowledge")
    c0 = v.check("P0", "Projects", "indexed projects (knowledge.db)", "every project folder still exists, its unit count "
                 "matches, and it has not changed since it was indexed")
    if not db.is_file():
        c0.add(SKIP, str(db), "no knowledge.db (no project added with kb_add_project)")
        v.untested.append("projects")
        return
    con = sqlite3.connect(KD.ro_uri(db), uri=True)
    con.row_factory = sqlite3.Row
    try:
        projects = [dict(r) for r in con.execute("SELECT path, label, units, indexed, files FROM projects")]
        units = [dict(r) for r in con.execute("SELECT project, name, kind, file FROM units")]
        has_fts = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'units_fts'").fetchone() is not None
    finally:
        con.close()
    v.meta["projects"] = {"database": str(db), "projects": len(projects), "units": len(units), "fts": has_fts}
    per = Counter(u["project"] for u in units)
    for p in projects:
        root = Path(p["path"])
        bad = []
        if per[p["path"]] != (p["units"] or 0):
            bad.append(f"{per[p['path']]} unit rows, the project says {p['units']}")
        if not root.is_dir():
            c0.add(WARN, p["path"], "the folder no longer exists (moved or deleted?) — kb_remove_project or add it again")
            continue
        try:
            if c0.timed(p["path"], K._tree_stamp, root) != p["files"]:
                c0.add(WARN, p["path"], "changed since it was indexed — kb_reindex() updates it")
        except Exception as exc:  # noqa: BLE001
            c0.crash(p["path"], exc)
        c0.add(FAIL if bad else PASS, p["path"], "; ".join(bad))

    kb = _ROKnowledge(v.kb, v.out)
    c1 = v.check("P1", "Projects", "unit by its name (kb_search)", f"every unit: kb_search with its name, limited to its "
                 f"project, lists it in the top {max(v.top, 20)}")
    todo = v.pick(units, max(100, v.args.sample * 10), "P1")
    c1.planned = len(todo)
    for i, u in enumerate(todo, 1):
        subj = f"{u['name']} ({Path(u['file']).name})"
        try:
            r = c1.timed(u["name"], kb.search, u["name"], None, u["project"], max(v.top, 20))
            hit = any(h["unit"] == u["name"] and h["file"] == u["file"] for h in r["hits"])
            c1.add(PASS if hit else FAIL, subj, "" if hit else
                   f"not in the hits: {[h['unit'] for h in r['hits'][:5]]} ({r.get('note') or ''})"[:300])
        except K.KnowledgeError as exc:
            c1.add(FAIL, subj, f"refused: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001
            c1.crash(subj, exc)
        v.progress(c1, i, len(todo))
    c2 = v.check("P2", "Projects", "known errors (kb_search / known_error)", "the ADS timeout in every notation is known")
    for q in KNOWN_ERROR_SAMPLES:
        k = K.known_error(q)
        c2.add(PASS if k else FAIL, q, "" if k else "not recognised")


def status_check(v: Verifier) -> None:
    """The skill's status command: it answers, and names each index's state as the checks above found it."""
    c = v.check("S0", "Skill", "status (skill)", "the skill's status command answers and reports each index in the "
                "state the readers report")
    try:
        r = c.timed("status", v.skill, "status")
    except Exception as exc:  # noqa: BLE001
        c.crash("status", exc)
        return
    for which in ("infosys", "manuals", "esi"):
        got = (r.get(which + "_state") or {}).get("state")
        want = (v.meta.get(which) or {}).get("state")
        if want is None:
            c.add(PASS if got else FAIL, which, "" if got else "no state reported")
        else:
            c.add(PASS if got == want else FAIL, which, "" if got == want else f"status says {got!r}, the index's "
                                                                            f"reader says {want!r}")


# ====================================================================================================== report
def write_report(v: Verifier, interrupted: bool, ro: dict[str, Any]) -> tuple[Path, Path, Path]:
    v.out.mkdir(parents=True, exist_ok=True)
    total = Counter()
    for c in v.checks.values():
        for s in STATUSES:
            total[s] += c.counts.get(s, 0)
    data = {"tool": f"kb_verify.py {SCRIPT_VERSION} (twincat-mcp {PLUGIN_VERSION})", "plugin": str(PLUGIN),
            "plugin_found_via": PLUGIN_SOURCE, "knowledge_folder": str(v.kb),
            "found_via": v.kb_source, "mode": v.args.mode, "sample_per_group": None if v.full else v.args.sample,
            "top": v.top, "seed": v.args.seed, "started": dt.datetime.fromtimestamp(v.t0).isoformat(timespec="seconds"),
            "seconds": round(time.time() - v.t0, 1), "interrupted": interrupted, "python": sys.version.split()[0],
            "areas": v.areas_done, "config": v.config_source, "not_tested": v.untested, "read_only": ro, "indexes": v.meta,
            "totals": {s: total.get(s, 0) for s in STATUSES},
            "checks": [c.to_dict() for c in v.checks.values()]}
    js = v.out / "results.json"
    js.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    fc = v.out / "failures.csv"
    with open(fc, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["area", "check", "title", "status", "subject", "detail"])
        for c in v.checks.values():
            for s in (FAIL, WARN, SKIP):
                for it in c.items[s]:
                    w.writerow([c.area, c.cid, c.title, s, it["subject"], it["detail"]])
    md = v.out / "report.md"
    md.write_text(_markdown(v, data), encoding="utf-8")
    return md, js, fc


def _markdown(v: Verifier, data: dict[str, Any]) -> str:
    L_: list[str] = []
    t = data["totals"]
    L_.append(f"# Knowledge base verification — {data['knowledge_folder']}")
    L_.append("")
    L_.append(f"Plugin code tested: {data['plugin']} ({data['plugin_found_via']}). Knowledge folder found via "
              f"{data['found_via']}.")
    L_.append("")
    L_.append(f"{data['tool']}, Python {data['python']}, mode **{data['mode']}**"
              + ("" if data["mode"] == "full" else f" (up to {data['sample_per_group']} items per book / manual)")
              + f", started {data['started']}, {data['seconds']} s" + (" — **INTERRUPTED (partial)**" if data["interrupted"] else ""))
    L_.append("")
    verdict = "no failure" if not t[FAIL] else f"{t[FAIL]} failure(s)"
    L_.append(f"**Result: {verdict}** — {t[PASS]} passed, {t[WARN]} warning(s), {t[SKIP]} not judged.")
    if data["not_tested"]:
        L_.append("")
        L_.append(f"**Not tested — no index:** {', '.join(data['not_tested'])}.")
    ro = data["read_only"]
    L_.append("")
    if ro.get("unchanged"):
        L_.append(f"Read-only: verified — none of the {ro.get('files_watched')} files of index\\ and manuals\\ changed.")
    else:
        L_.append(f"Read-only: **FILES CHANGED during the run** (by this run or another program): {ro.get('changed')}")
    L_.append("")
    L_.append(f"Config: {data.get('config')}. Not judged = the question cannot be decided on this index (the page "
              "with the answer is not in it, its format is older, or more items share the answer than are listed).")
    L_.append("")
    L_.append("## Indexes")
    L_.append("")
    for name, m in v.meta.items():
        if isinstance(m, dict):
            L_.append(f"- **{name}**: " + ", ".join(f"{k} {val}" for k, val in m.items() if val not in (None, "", [])))
    L_.append("")
    L_.append("## Summary")
    L_.append("")
    L_.append("| check | area | what | passed | failed | warnings | not judged | ms/call |")
    L_.append("|---|---|---|---:|---:|---:|---:|---:|")
    for c in v.checks.values():
        cnt = c.counts
        ms = f"{c.seconds / c.calls * 1000:.1f}" if c.calls else ""
        L_.append(f"| {c.cid} | {c.area} | {c.title} | {cnt.get(PASS, 0)} | {cnt.get(FAIL, 0)} | {cnt.get(WARN, 0)} | "
                  f"{cnt.get(SKIP, 0)} | {ms} |")
    L_.append("")
    shown = v.args.show
    for c in v.checks.values():
        L_.append(f"## {c.cid} — {c.title}")
        L_.append("")
        L_.append(c.what + ".")
        extra = {k: n for k, n in c.counts.items() if k not in STATUSES}
        if extra:
            L_.append("")
            L_.append("Also counted: " + ", ".join(f"{k} {n}" for k, n in extra.items()))
        for note in c.notes:
            L_.append("")
            L_.append(f"> {note}")
        for s, label in ((FAIL, "Failures"), (WARN, "Warnings"), (SKIP, "Not judged")):
            items = c.items[s]
            if not items:
                continue
            L_.append("")
            L_.append(f"**{label}** ({len(items)}" + (f", first {shown} — all in results.json / failures.csv" if len(items) > shown else "") + "):")
            L_.append("")
            for it in items[:shown]:
                L_.append(f"- `{it['subject']}` — {it['detail']}".replace("\n", " "))
        if c.slowest:
            L_.append("")
            L_.append("Slowest: " + ", ".join(f"{s[:60]} {d * 1000:.0f} ms" for d, s in c.slowest[:3]))
        L_.append("")
    return "\n".join(L_) + "\n"


# ====================================================================================================== main
AREAS: dict[str, Callable[[Verifier], None]] = {"infosys": infosys_checks, "manuals": manuals_checks,
                                                "esi": esi_checks, "projects": projects_checks}


def default_out() -> Path:
    home = Path(os.environ.get("TCMCP_HOME") or (Path.home() / ".twincat_mcp"))
    return home / "kb_verify" / time.strftime("%Y%m%d-%H%M%S")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="kb_verify.py", description="Offline, read-only verification of the twincat-mcp "
                                 "knowledge base (Infosys, manuals, ESI, projects) through the plugin's own readers.")
    ap.add_argument("--kb", help="the knowledge folder (default: as the twincat-docs skill finds it — "
                                 "TCMCP_KNOWLEDGE_DIR, kb_set_location, else D:\\TwinCAT-MCP\\knowledge)")
    ap.add_argument("--mode", choices=("quick", "full"), default="quick",
                    help="quick: a sample of every book / manual / device (default); full: everything")
    ap.add_argument("--only", help="comma list of areas: infosys, manuals, esi, projects (default: all)")
    ap.add_argument("--sample", type=int, default=20, help="quick mode: items per book / manual (default 20)")
    ap.add_argument("--top", type=int, default=10, help="a search passes when the item is in the first N hits (default 10)")
    ap.add_argument("--seed", type=int, default=1, help="quick mode sample seed (default 1: a re-run tests the same items)")
    ap.add_argument("--out", help="report folder (default %%USERPROFILE%%\\.twincat_mcp\\kb_verify\\<time>)")
    ap.add_argument("--package", help="the Infosys Help Viewer package (.mshc or its folder) — default: [docs] mshc "
                                      "of twincat_mcp.toml, else the Help Viewer catalogs and the folder the index was "
                                      "built from")
    ap.add_argument("--plugin", help="the twincat-mcp plugin folder whose code is tested — default: the plugin this "
                                     "script is part of, else the newest installed under %%USERPROFILE%%\\.claude\\plugins")
    ap.add_argument("--config", help="twincat_mcp.toml (only its [docs] mshc is read) — default: as the server finds it")
    ap.add_argument("--no-hash", action="store_true", help="do not compare the PDFs' SHA-256 with the index")
    ap.add_argument("--show", type=int, default=25, help="failures listed per check in report.md (default 25)")
    a = ap.parse_args(argv)
    if not 1 <= a.top <= 50:
        ap.error("--top must be 1–50 (the search tools' own limit)")
    if a.sample < 1:
        ap.error("--sample must be ≥ 1")
    return a


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    kb, how = KD.kb_root(args.kb)
    if not kb.is_dir():
        print(f"knowledge folder not found: {kb} (from {how}) — pass --kb PATH", file=sys.stderr)
        return 2
    areas = [a.strip().lower() for a in (args.only or ",".join(AREAS)).split(",") if a.strip()]
    unknown = [a for a in areas if a not in AREAS]
    if unknown:
        print(f"unknown area(s) {unknown}; areas: {', '.join(AREAS)}", file=sys.stderr)
        return 2
    kb = kb.resolve()
    out = Path(args.out).expanduser().resolve() if args.out else default_out()
    try:
        out.relative_to(kb)
        print(f"the report folder {out} is inside the knowledge folder — the plugin never writes there; choose "
              "another --out", file=sys.stderr)
        return 2
    except ValueError:
        pass
    v = Verifier(args, kb, how, out)
    v.say(f"kb_verify {SCRIPT_VERSION} — plugin twincat-mcp {PLUGIN_VERSION} at {PLUGIN} ({PLUGIN_SOURCE})")
    v.say(f"  knowledge folder {kb} ({how}), mode {args.mode}, report {out}")
    watched = [L.index_dir(kb), kb / "manuals"]
    before = snapshot(watched)
    interrupted = False
    try:
        for a in areas:
            v.say(f"  {a} …")
            t = time.time()
            try:
                AREAS[a](v)
            except KD.Err as exc:
                v.check(f"{a[:1].upper()}!", a, "could not run", "the area stopped").add(FAIL, a, f"{exc.code}: {exc.message}")
            except Exception as exc:  # noqa: BLE001
                v.check(f"{a[:1].upper()}!", a, "could not run", "the area stopped").crash(a, exc)
            v.areas_done.append(a)
            v.say(f"  {a} done in {time.time() - t:.0f} s")
        status_check(v)
    except KeyboardInterrupt:
        interrupted = True
        v.say("\n  interrupted — writing the partial report")
    after = snapshot(watched)
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    ro = {"unchanged": not changed, "changed": changed[:50], "files_watched": len(before)}
    md, js, fc = write_report(v, interrupted, ro)
    t = Counter()
    for c in v.checks.values():
        t.update({s: c.counts.get(s, 0) for s in STATUSES})
    v.say("")
    v.say(f"  {t[PASS]} passed, {t[FAIL]} failed, {t[WARN]} warnings, {t[SKIP]} not judged — "
          f"{time.time() - v.t0:.0f} s")
    if v.untested:
        v.say(f"  NOT TESTED (no index): {', '.join(v.untested)}")
    v.say(f"  read-only: {'verified' if not changed else 'FILES CHANGED: ' + ', '.join(changed[:5])}")
    v.say(f"  report: {md}")
    v.say(f"          {js}")
    v.say(f"          {fc}")
    return 130 if interrupted else 1 if t[FAIL] or changed else 0


if __name__ == "__main__":
    sys.exit(main())
