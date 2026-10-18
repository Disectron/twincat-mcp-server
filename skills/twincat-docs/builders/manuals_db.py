"""Hardware manual bank: index the PDF manuals dropped into a folder and search them with compact output.

Put PDFs (Beckhoff EL/EK/EP/CX/AX manuals, data sheets, any vendor) anywhere under the manuals folder, sub-folders
allowed. `update` finds new, changed and removed files (size + mtime, confirmed by SHA-256) and re-indexes only those;
run it whenever you add manuals (or from Task Scheduler). Text is stored per page with the section heading from the
PDF bookmarks, product codes found on each page are indexed for `product` lookups.

usage:
  python manuals_db.py [--kb PATH | --db PATH] <command> ...
  python manuals_db.py update  [--dir PATH] [--force]            index new/changed PDFs, drop removed ones
  python manuals_db.py search  QUERY [--doc GLOB] [--limit 10]    ranked hits: doc | page | section | snippet
  python manuals_db.py product CODE [--limit 20]                  where a product code (EL3102, CX5140 …) appears
  python manuals_db.py show    DOC PAGE[-PAGE] [--max 4000]       page text (DOC: file name, glob or doc id)
  python manuals_db.py toc     DOC [--depth 2]                    bookmarks with page numbers
  python manuals_db.py docs    [--filter GLOB]                    indexed manuals: id, pages, title
  python manuals_db.py stats   [--json]                         the real state: manuals, PDFs not indexed, a running update

QUERY is FTS5 syntax: words are ANDed, "exact phrase", OR, NOT, prefix*.
Backend: PyMuPDF (fitz) if installed, else pypdf. A PDF with no text layer (scanned) is listed with 0 text pages.
Needs Python 3.12 or newer (checked at start). PyMuPDF is not part of the twincat plugin: install it yourself
(pip install pymupdf) into the Python that runs this script, or pip install pypdf.

The knowledge folder (<kb>) is --kb, else the environment variable TCMCP_KNOWLEDGE_DIR, else the folder chosen with
the twincat plugin's kb_set_location, else the folder above this script when it is a knowledge folder, else
D:\\TwinCAT-MCP\\knowledge. PDFs: <kb>\\manuals (--dir), database: <kb>\\manualsdb\\manuals.db (--db).

Version 2 (2026-10-18), changes against version 1:
  - refuses to run below Python 3.12 with a message (the file compiles on older versions, so the message is shown)
  - knowledge folder found like the other knowledge tools (--kb, TCMCP_KNOWLEDGE_DIR, kb_set_location)
  - one update at a time (<db>.lock with a heartbeat); state and progress in a meta table; `stats` shows it
  - `search --doc` filters before the row limit (it used to filter the first limit × 10 hits only)
Stored paths stay absolute (moving the knowledge folder re-indexes every manual on the next update).
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:   # before anything else, so an older Python gets this message, not a traceback
    sys.exit(f"manuals_db.py needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
             f"{sys.version.split()[0]} ({sys.executable}). Run it with a newer python.exe, e.g. "
             r"C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe")

import argparse  # noqa: E402 — after the version check on purpose
import fnmatch  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

BUILDER = "manuals_db.py"
BUILDER_VERSION = "2"
FALLBACK_KB = Path(r"D:\TwinCAT-MCP\knowledge")
HEARTBEAT_STALE_S = 300          # one large PDF can take a while: an update silent for this long is gone


def kb_root(arg: str | None) -> tuple[Path, str]:
    if arg:
        return Path(arg).expanduser(), "--kb"
    env = os.environ.get("TCMCP_KNOWLEDGE_DIR")
    if env:
        return Path(env).expanduser(), "TCMCP_KNOWLEDGE_DIR"
    home = Path(os.environ.get("TCMCP_HOME") or (Path.home() / ".twincat_mcp"))
    try:
        d = json.loads((home / "knowledge_location.json").read_text(encoding="utf-8"))
        if d.get("path"):
            return Path(d["path"]), "kb_set_location"
    except (OSError, ValueError):
        pass
    up = Path(__file__).resolve().parent.parent           # <kb>\\tools\\manuals_db.py
    if (up / "knowledge.db").is_file() or (up / "manualsdb").is_dir():
        return up, "folder above the script"
    return FALLBACK_KB, "default"


def _db(args) -> Path:
    return Path(args.db) if args.db else kb_root(args.kb)[0] / "manualsdb" / "manuals.db"


def _dir(args) -> Path:
    return Path(args.dir) if getattr(args, "dir", None) else kb_root(args.kb)[0] / "manuals"

# Beckhoff order codes (EL3102, EL3102-0010, EK1100, EP2008, CX5140, C6030, AX5206, AM8121 …) and a few generic ones
PRODUCT_RE = re.compile(r"\b((?:EL|ELM|ELX|ES|EK|EP|EPP|EPI|ER|EQ|EM|EJ|EZ|EX|KL|KM|KS|BK|BC|BX|CX|CP|CU|C6|C7|AX|AMP|AM|AG|AT|"
                        r"EK|FC|IP|IL|IE|LC|TF|TE|TC|ZB|ZK|ZS)\d{3,5}(?:-\d{4})?)\b")

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc(id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, size INTEGER, mtime INTEGER,
    sha256 TEXT, title TEXT, pages INTEGER, text_pages INTEGER, indexed TEXT, backend TEXT);
CREATE TABLE IF NOT EXISTS page(doc INTEGER, page INTEGER, section TEXT, text TEXT, PRIMARY KEY(doc, page));
CREATE TABLE IF NOT EXISTS toc(doc INTEGER, level INTEGER, title TEXT, page INTEGER);
CREATE TABLE IF NOT EXISTS product(code TEXT, doc INTEGER, page INTEGER, n INTEGER);
CREATE INDEX IF NOT EXISTS product_code ON product(code);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, section, doc UNINDEXED, page UNINDEXED,
    tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


# ---------------------------------------------------------------- PDF backends
def _open_pdf(path: Path):
    """-> (backend name, page count, list of (level, title, page), iterator of page texts)."""
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf  # PyMuPDF < 1.24
        d = pymupdf.open(str(path))
        toc = [(lvl, title.strip(), pg) for lvl, title, pg in d.get_toc(simple=True)]
        backend = "pymupdf"
        if not toc:
            toc = _toc_from_fonts(d)
            backend = "pymupdf+font-headings"
        meta_title = (d.metadata or {}).get("title") or ""
        if not meta_title:   # first large line on page 1 (Beckhoff: "Manual | EN" … product name)
            meta_title = _first_title(d)
        return backend, d.page_count, toc, (p.get_text("text", sort=True) for p in d), meta_title
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
    except ImportError:
        sys.exit("no PDF library: pip install pymupdf  (or pypdf) into the Python that runs this script")
    r = PdfReader(str(path))
    toc: list[tuple[int, str, int]] = []

    def walk(items, lvl):
        for it in items:
            if isinstance(it, list):
                walk(it, lvl + 1)
            else:
                try:
                    toc.append((lvl, str(it.title).strip(), r.get_destination_page_number(it) + 1))
                except Exception:  # noqa: BLE001
                    pass
    try:
        walk(r.outline, 1)
    except Exception:  # noqa: BLE001
        pass
    meta_title = str((r.metadata or {}).get("/Title") or "")
    return "pypdf", len(r.pages), toc, ((p.extract_text() or "") for p in r.pages), meta_title


_NUM = re.compile(r"^\d+(?:\.\d+)*\.?$")
_GLUED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s*([A-ZÄÖÜ].*)$")


def _toc_from_fonts(d) -> list[tuple[int, str, int]]:
    """Chapter headings for PDFs without bookmarks: bold lines ≥ 1.5 × the body font size. A line holding only a
    chapter number ('4.2') is joined with the heading that follows; the level comes from the number's depth,
    else from the font-size rank."""
    from collections import Counter
    sizes: Counter = Counter()
    lines_per_page = []
    for p in d:
        lines = []
        for b in p.get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                spans = [s for s in ln.get("spans", []) if s["text"].strip()]
                if not spans:
                    continue
                txt = "".join(s["text"] for s in spans).strip()
                size = round(max(s["size"] for s in spans), 1)
                bold = any(s["flags"] & 16 for s in spans)
                sizes[size] += len(txt)
                lines.append((size, bold, txt))
        lines_per_page.append(lines)
    if not sizes:
        return []
    body = sizes.most_common(1)[0][0]
    head_sizes = sorted({s for s in sizes if s >= body * 1.5}, reverse=True)
    rank = {s: i + 1 for i, s in enumerate(head_sizes)}
    raw: list[tuple[str | None, str, int, int]] = []          # (number, title, size rank, page)
    for pg, lines in enumerate(lines_per_page, 1):
        pending = None
        for size, bold, txt in lines:
            if size not in rank or not bold:
                pending = None
                continue
            if _NUM.match(txt):
                pending = txt.rstrip(".")
                continue
            if len(txt) > 120:
                continue
            num = pending
            glued = _GLUED.match(txt)                         # '10Error handling' (number and title in one span)
            if num is None and glued:
                num, txt = glued.group(1), glued.group(2)
            raw.append((num, txt, rank[size], pg))
            pending = None
    numbered = any(n for n, *_ in raw)
    # numbered manuals: level = number depth, unnumbered headings (contents, lists) = level 1
    return [((n.count(".") + 1) if n else (1 if numbered else r), f"{n} {t}" if n else t, pg) for n, t, r, pg in raw]


def _first_title(d) -> str:
    try:
        best = ("", 0.0)
        for b in d[0].get_text("dict").get("blocks", []):
            for ln in b.get("lines", []):
                for s in ln.get("spans", []):
                    if s["text"].strip() and s["size"] > best[1]:
                        best = (s["text"].strip(), s["size"])
        return best[0]
    except Exception:  # noqa: BLE001
        return ""


def _clean(t: str) -> str:
    t = t.replace("\u00ad", "").replace("\ufb01", "fi").replace("\ufb02", "fl")
    t = re.sub(r"(\w)-\n(\w)", r"\1\2", t)          # hyphenated line breaks
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def _sections(toc: list[tuple[int, str, int]], pages: int) -> list[str]:
    """Section path ('3 Mounting > 3.2 Installation') valid on each page, from the bookmarks."""
    sec = [""] * (pages + 2)
    marks = sorted(((pg, lvl, title) for lvl, title, pg in toc if 1 <= pg <= pages), key=lambda x: (x[0], x[1]))
    stack: list[tuple[int, str]] = []
    i = 0
    for pg in range(1, pages + 1):
        while i < len(marks) and marks[i][0] == pg:
            _, lvl, title = marks[i]
            stack = [s for s in stack if s[0] < lvl] + [(lvl, title)]
            i += 1
        sec[pg] = " > ".join(t for _, t in stack[-2:])
    return sec


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- database
def connect(path: Path, create: bool = False) -> sqlite3.Connection:
    if not create and not path.exists():
        sys.exit(f"no database at {path} — run: python {Path(__file__).name} update")
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _drop(con, doc_id: int) -> None:
    con.execute("DELETE FROM page WHERE doc=?", (doc_id,))
    con.execute("DELETE FROM toc WHERE doc=?", (doc_id,))
    con.execute("DELETE FROM product WHERE doc=?", (doc_id,))
    con.execute("DELETE FROM fts WHERE doc=?", (doc_id,))
    con.execute("DELETE FROM doc WHERE id=?", (doc_id,))


def index_pdf(con, path: Path, sha: str) -> dict:
    backend, n, toc, texts, meta_title = _open_pdf(path)
    st = path.stat()
    cur = con.execute("INSERT INTO doc(path, name, size, mtime, sha256, title, pages, text_pages, indexed, backend) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (str(path), path.name, st.st_size, int(st.st_mtime), sha, meta_title or path.stem, n, 0,
                       time.strftime("%Y-%m-%d %H:%M:%S"), backend))
    doc_id = cur.lastrowid
    secs = _sections(toc, n)
    con.executemany("INSERT INTO toc VALUES(?,?,?,?)", [(doc_id, lvl, t, pg) for lvl, t, pg in toc])
    text_pages = 0
    for pg, raw in enumerate(texts, 1):
        t = _clean(raw)
        if t:
            text_pages += 1
        con.execute("INSERT INTO page VALUES(?,?,?,?)", (doc_id, pg, secs[pg] if pg < len(secs) else "", t))
        con.execute("INSERT INTO fts(text, section, doc, page) VALUES(?,?,?,?)", (t, secs[pg] if pg < len(secs) else "", doc_id, pg))
        counts: dict[str, int] = {}
        for code in PRODUCT_RE.findall(t):
            counts[code] = counts.get(code, 0) + 1
        con.executemany("INSERT INTO product VALUES(?,?,?,?)", [(c, doc_id, pg, k) for c, k in counts.items()])
    con.execute("UPDATE doc SET text_pages=? WHERE id=?", (text_pages, doc_id))
    return {"id": doc_id, "pages": n, "text_pages": text_pages, "toc": len(toc), "backend": backend}


class UpdateLock:
    """One update at a time: <db>.lock created exclusively; a lock silent for HEARTBEAT_STALE_S is taken over."""

    def __init__(self, db: Path):
        self.path = db.with_name(db.name + ".lock")

    def __enter__(self) -> "UpdateLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}))
                self._stop = threading.Event()
                threading.Thread(target=self._beat_loop, name="heartbeat", daemon=True).start()
                return self
            except FileExistsError:
                age = time.time() - self.path.stat().st_mtime
                if age < HEARTBEAT_STALE_S:
                    sys.exit(f"another update is running (last progress {age:.0f} s ago) — wait for it; `stats` shows it")
                self.path.unlink(missing_ok=True)
        sys.exit(f"cannot take the update lock {self.path}")

    def beat(self) -> None:
        try:
            os.utime(self.path)
        except OSError:
            pass

    def _beat_loop(self) -> None:
        # the heartbeat runs on its own: long single steps (fts optimize, link resolution, one huge PDF) must not
        # make a live build look abandoned to a second run
        while not self._stop.wait(15):
            self.beat()

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self.path.unlink(missing_ok=True)


def _meta(con, **kv) -> None:
    con.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", [(k, str(v)) for k, v in kv.items()])
    con.commit()


def cmd_update(args) -> None:
    folder = _dir(args)
    folder.mkdir(parents=True, exist_ok=True)
    db = _db(args)
    with UpdateLock(db) as lock:
        _update(args, folder, db, lock)


def _update(args, folder: Path, db: Path, lock: "UpdateLock") -> None:
    con = connect(db, create=True)
    known = {r["path"]: r for r in con.execute("SELECT * FROM doc")}
    found = {str(p): p for p in folder.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file()}
    added = changed = unchanged = removed = failed = 0
    _meta(con, state="updating", pid=os.getpid(), started=time.strftime("%Y-%m-%d %H:%M:%S"), files_total=len(found),
          files_done=0, heartbeat=f"{time.time():.0f}", folder=str(folder), builder=f"{BUILDER} {BUILDER_VERSION}")
    for path_s in sorted(set(known) - set(found)):
        _drop(con, known[path_s]["id"]); removed += 1
        print(f"removed  {Path(path_s).name}")
    con.commit()
    for done, (path_s, p) in enumerate(sorted(found.items())):
        _meta(con, files_done=done, heartbeat=f"{time.time():.0f}")
        lock.beat()
        st = p.stat()
        old = known.get(path_s)
        if old and not args.force and old["size"] == st.st_size and old["mtime"] == int(st.st_mtime):
            unchanged += 1
            continue
        t0 = time.time()
        sha = _sha256(p)
        if old and not args.force and old["sha256"] == sha:      # touched, same content
            con.execute("UPDATE doc SET mtime=? WHERE id=?", (int(st.st_mtime), old["id"])); unchanged += 1
            continue
        if old:
            _drop(con, old["id"])
        try:
            r = index_pdf(con, p, sha)
            con.commit()
        except Exception as exc:  # noqa: BLE001 — one broken PDF must not stop the run
            con.rollback(); failed += 1
            print(f"FAILED   {p.name}: {type(exc).__name__}: {exc}")
            continue
        if old:
            changed += 1
        else:
            added += 1
        note = "  (no text layer: scanned?)" if r["text_pages"] == 0 else ""
        print(f"{'updated ' if old else 'added   '} {p.name}: {r['pages']} pages, {r['toc']} bookmarks, "
              f"{time.time() - t0:.1f} s{note}")
    con.commit()
    if added or changed or removed:
        con.execute("INSERT INTO fts(fts) VALUES('optimize')"); con.commit()
    _meta(con, state="idle", files_done=len(found), heartbeat=f"{time.time():.0f}",
          last_update=time.strftime("%Y-%m-%d %H:%M:%S"),
          last_result=f"{added} added, {changed} updated, {removed} removed, {unchanged} unchanged, {failed} failed")
    print(f"done: {added} added, {changed} updated, {removed} removed, {unchanged} unchanged, {failed} failed "
          f"— folder {folder}")


def _docs(con, pattern: str | None) -> list[sqlite3.Row]:
    rows = con.execute("SELECT * FROM doc ORDER BY name").fetchall()
    if not pattern:
        return rows
    if pattern.isdigit():
        return [r for r in rows if r["id"] == int(pattern)]
    pat = pattern.lower() if any(c in pattern for c in "*?[") else f"*{pattern.lower()}*"
    return [r for r in rows if fnmatch.fnmatch(r["name"].lower(), pat) or fnmatch.fnmatch((r["title"] or "").lower(), pat)]


def cmd_search(args) -> None:
    con = connect(_db(args))
    names = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM doc")}
    where, params = "fts MATCH ?", [args.query]
    if args.doc:
        # the manual filter runs inside the query, before LIMIT (version 1 filtered the first limit × 10 hits only)
        ids = [r["id"] for r in _docs(con, args.doc)]
        if not ids:
            sys.exit(f"no indexed manual matches '{args.doc}' (docs lists them)")
        where += f" AND doc IN ({','.join('?' * len(ids))})"
        params += ids
    try:
        rows = con.execute("SELECT doc, page, section, snippet(fts, 0, '[', ']', '…', 16) AS s, bm25(fts, 1.0, 4.0) AS r "
                           f"FROM fts WHERE {where} ORDER BY r LIMIT ?", params + [args.limit]).fetchall()
        total = con.execute(f"SELECT COUNT(*) FROM fts WHERE {where}", params).fetchone()[0]
    except sqlite3.OperationalError as exc:
        sys.exit(f"bad query ({exc}); FTS5 syntax: words, \"phrase\", OR, NOT, prefix*")
    for r in rows:
        sec = f" | {r['section']}" if r["section"] else ""
        snip = re.sub(r"\s+", " ", r["s"])
        print(f"{names.get(r['doc'], r['doc'])} | p{r['page']}{sec} | {snip}")
    if not rows:
        print("no hits")
    elif total > len(rows):
        print(f"({len(rows)} of {total} hits; --limit for more)")


def cmd_product(args) -> None:
    con = connect(_db(args))
    code = args.code.upper()
    rows = con.execute("SELECT d.name, p.doc, GROUP_CONCAT(p.page) pages, SUM(p.n) hits FROM product p JOIN doc d ON d.id=p.doc "
                       "WHERE p.code=? OR p.code LIKE ? GROUP BY p.doc ORDER BY hits DESC LIMIT ?",
                       (code, code + "-%", args.limit)).fetchall()
    if not rows:
        print(f"{code}: not in any indexed manual")
    for r in rows:
        pages = sorted({int(x) for x in r["pages"].split(",")})
        txt = ",".join(map(str, pages[:15])) + (" …" if len(pages) > 15 else "")
        print(f"{r['name']} | {r['hits']} mentions | pages {txt}")


def cmd_show(args) -> None:
    con = connect(_db(args))
    docs = _docs(con, args.doc)
    if len(docs) != 1:
        sys.exit(f"'{args.doc}' matches {len(docs)} manuals" + (": " + ", ".join(d["name"] for d in docs[:8]) if docs else ""))
    d = docs[0]
    a, _, b = args.pages.partition("-")
    lo, hi = int(a), int(b or a)
    out, used = [f"{d['name']} | {d['title']} | {d['pages']} pages | {d['path']}"], 0
    for r in con.execute("SELECT page, section, text FROM page WHERE doc=? AND page BETWEEN ? AND ? ORDER BY page", (d["id"], lo, hi)):
        block = f"\n--- p{r['page']}" + (f" | {r['section']}" if r["section"] else "") + "\n" + r["text"]
        if used + len(block) > args.max:
            out.append(block[:max(0, args.max - used)] + f"\n… [cut at --max {args.max}]")
            break
        out.append(block); used += len(block)
    print("".join(out))


def cmd_toc(args) -> None:
    con = connect(_db(args))
    docs = _docs(con, args.doc)
    if len(docs) != 1:
        sys.exit(f"'{args.doc}' matches {len(docs)} manuals")
    d = docs[0]
    print(f"{d['name']} | {d['pages']} pages")
    for r in con.execute("SELECT level, title, page FROM toc WHERE doc=? AND level<=? ORDER BY rowid", (d["id"], args.depth)):
        print(f"{'  ' * (r['level'] - 1)}{r['title']}  p{r['page']}")


def cmd_docs(args) -> None:
    con = connect(_db(args))
    for r in _docs(con, args.filter):
        scan = "  (no text)" if not r["text_pages"] else ""
        print(f"{r['id']:>4} | {r['name']} | {r['pages']} p | {r['title']}{scan}")


def ro_uri(db: Path) -> str:
    """'file:/C:/kb/x.db?mode=ro'; UNC paths as 'file:////server/share/x.db' (Path.as_uri gives a host part SQLite refuses)."""
    from urllib.parse import quote
    s = str(db.resolve()).replace("\\", "/")
    s = "//" + s if s.startswith("//") else (s if s.startswith("/") else "/" + s)
    return "file:" + quote(s, safe="/:") + "?mode=ro"


def manuals_state(db: Path, folder: Path) -> dict:
    """The real state: what is indexed, what in the folder is not (new / changed / gone, by size + mtime like
    update), and an update that is running or was interrupted. Same rules as the twincat-docs skill's status."""
    out: dict = {"database": str(db), "folder": str(folder)}
    on_disk = {os.path.normcase(str(f)): f for f in folder.rglob("*") if f.suffix.lower() == ".pdf" and f.is_file()} if folder.is_dir() else {}
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
            meta = {}                                       # built by version 1
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
        out["updating"] = {"state": "running" if time.time() - beat < HEARTBEAT_STALE_S else "interrupted",
                         "files_done": int(meta.get("files_done") or 0), "files_total": int(meta.get("files_total") or 0)}
    pending = len(new) + len(changed) + len(gone)
    out["state"] = "up_to_date" if not pending else "pending"
    msg = f"{len(docs)} manuals, {out['pages']} pages indexed"
    if pending:
        msg += f"; {len(new)} new, {len(changed)} changed, {len(gone)} removed PDF(s) not reflected — run update"
    if out.get("updating"):
        u = out["updating"]
        msg += (f"; an update is RUNNING ({u['files_done']} of {u['files_total']} files)" if u["state"] == "running" else
                f"; the last update was INTERRUPTED at {u['files_done']} of {u['files_total']} files — run update")
    out["message"] = msg
    return out


def cmd_stats(args) -> None:
    p = _db(args)
    if args.json:
        print(json.dumps(manuals_state(p, _dir(args)), ensure_ascii=False))
        return
    con = connect(p)
    n = con.execute("SELECT COUNT(*), COALESCE(SUM(pages),0), COALESCE(SUM(text_pages),0) FROM doc").fetchone()
    codes = con.execute("SELECT COUNT(DISTINCT code) FROM product").fetchone()[0]
    print(f"{p}: {n[0]} manuals, {n[1]} pages ({n[2]} with text), {codes} product codes, "
          f"{p.stat().st_size / 1e6:.1f} MB")
    print("  " + manuals_state(p, _dir(args))["message"])


def main() -> None:
    # --kb / --db work before or after the command (update --kb D:\\kb as well as --kb D:\\kb update)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--kb", default=argparse.SUPPRESS, help="knowledge folder (default: see above)")
    common.add_argument("--db", default=argparse.SUPPRESS, help="database (default <kb>\\manualsdb\\manuals.db)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    _sub = sub.add_parser
    sub.add_parser = lambda *a, **kw: _sub(*a, parents=[common], **kw)  # type: ignore[method-assign]
    s = sub.add_parser("update"); s.add_argument("--dir"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--doc"); s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("product"); s.add_argument("code"); s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("show"); s.add_argument("doc"); s.add_argument("pages"); s.add_argument("--max", type=int, default=4000)
    s = sub.add_parser("toc"); s.add_argument("doc"); s.add_argument("--depth", type=int, default=2)
    s = sub.add_parser("docs"); s.add_argument("--filter")
    s = sub.add_parser("stats"); s.add_argument("--json", action="store_true"); s.add_argument("--dir")
    args = ap.parse_args()
    for k in ("kb", "db"):
        if not hasattr(args, k):
            setattr(args, k, None)
    if getattr(args, "limit", 1) < 1:
        ap.error("--limit must be 1 or more")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    {"update": cmd_update, "search": cmd_search, "product": cmd_product, "show": cmd_show, "toc": cmd_toc,
     "docs": cmd_docs, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
