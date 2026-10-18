"""Search the locally installed Beckhoff Infosys (Microsoft Help Viewer package) with compact, low-token output.

The Help Viewer content is one zip (.mshc) of HTML pages stored as <book>/<lcid>/<pageid>.html; the page id is the
same as on infosys.beckhoff.com. `index` reads it once into an SQLite FTS5 database; the other commands only query
that database. Standard library only. Needs Python 3.12 or newer (checked at start) with FTS5, which the python.org
Windows builds include.

usage:
  python infosys_local.py index  [--lang en|de] [--mshc PATH] [--kb PATH | --db PATH] [--force]
  python infosys_local.py search QUERY [--book GLOB] [--limit 10]          ranked hits, one line each
  python infosys_local.py show   REF [--max 4000] [--section NAME]         page as text (REF: id, book/id, title)
  python infosys_local.py decl   SYMBOL [--book GLOB]                      only declaration code + requirements
  python infosys_local.py books  [--filter GLOB]                           books with page counts
  python infosys_local.py stats                                            the real state of the index

QUERY is FTS5 syntax: words are ANDed, "exact phrase", OR, NOT, prefix*  (e.g.  profiler OR GETCPUACCOUNT).

The knowledge folder (<kb>) is --kb, else the environment variable TCMCP_KNOWLEDGE_DIR, else the folder chosen with
the twincat plugin's kb_set_location, else the folder above this script when it is a knowledge folder, else
D:\\TwinCAT-MCP\\knowledge. The index is <kb>\\helpviewer\\infosys_<lang>.db (--db overrides).

Version 2 (2026-10-18), changes against version 1:
  - refuses to run below Python 3.12 with a message (the file compiles on older versions, so the message is shown)
  - a rebuild writes <db>.building and replaces the index only when it is complete: an interrupted build (also with
    --force) never leaves a half index that the next run calls "up to date"; the last complete index stays in use
  - one build at a time (<db>.lock with a heartbeat); progress, page/book/link counts and the package path in meta
  - `stats` reports the real state: complete / building / interrupted / outdated (the package changed) / old format
  - `search --book` filters before the row limit (it used to filter the first limit × 8 hits only)
  - `show` / `decl` refuse a name that several pages share and list them, instead of showing the first one
  - links are kept: table `link` (text, href, target book/page; ms-xhelp ids resolved through the help ids)
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:   # before anything else, so an older Python gets this message, not a traceback
    sys.exit(f"infosys_local.py needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
             f"{sys.version.split()[0]} ({sys.executable}). Run it with a newer python.exe, e.g. "
             r"C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe")

import argparse  # noqa: E402 — after the version check on purpose
import fnmatch  # noqa: E402
import html  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402
from html.parser import HTMLParser  # noqa: E402
from pathlib import Path  # noqa: E402

BUILDER = "infosys_local.py"
BUILDER_VERSION = "2"
SCHEMA_VERSION = "2"
CATALOG = Path(r"C:\ProgramData\Microsoft\HelpLibrary2\Catalogs\VisualStudio15\ContentStore")
FALLBACK_KB = Path(r"D:\TwinCAT-MCP\knowledge")
LANGS = {"en": ("EN-US", "1033"), "de": ("DE-DE", "1031")}
WEB = "https://infosys.beckhoff.com/content/{lcid}/{book}/{pid}.html"
HEARTBEAT_STALE_S = 120          # a build that has not written progress for this long is no longer running

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS page(
    pid TEXT, book TEXT, title TEXT, help_id TEXT, toc_parent TEXT, version TEXT, crumb TEXT, body TEXT,
    PRIMARY KEY(book, pid));
CREATE INDEX IF NOT EXISTS page_title ON page(title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS page_help ON page(help_id);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(title, body, book UNINDEXED, pid UNINDEXED,
    tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS link(book TEXT, pid TEXT, n INTEGER, text TEXT, href TEXT, hid TEXT,
    target_book TEXT, target_pid TEXT);
CREATE INDEX IF NOT EXISTS link_page ON link(book, pid);
"""


# ---------------------------------------------------------------- HTML -> compact text
class _Text(HTMLParser):
    """Article body to plain text: '#' headings, pipe tables, fenced <pre>, links as 'text', images dropped.
    Links are also collected (href + text) for the link table; the text itself is unchanged by that."""
    SKIP = {"script", "style", "header", "nav", "footer", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.rows_in_table = 0
        self.links: list[tuple[str, str]] = []
        self._a: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if self.skip:
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._a = (href, []) if href else None
        if tag in ("h1", "h2", "h3", "h4"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in ("p", "div", "li", "br", "dt", "dd"):
            self._nl("- " if tag == "li" else "")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n```\n")
        elif tag == "table":
            self.rows_in_table = 0
            self.out.append("\n")
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a" and self._a is not None:
            text = re.sub(r"\s+", " ", "".join(self._a[1])).strip()
            if text:
                self.links.append((self._a[0], text))
            self._a = None
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n")
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "/"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.out.append("\n| " + " | ".join(self.row) + " |")
                self.rows_in_table += 1
            self.row = None
        elif tag == "table":
            self.out.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self._a is not None:
            self._a[1].append(data)
        if self.cell is not None:
            self.cell.append(data)
        elif self.pre:
            self.out.append(data)
        else:
            self.out.append(re.sub(r"\s+", " ", data))

    def _nl(self, prefix: str = "") -> None:
        if self.cell is not None:
            self.cell.append(" ")
        else:
            self.out.append("\n" + prefix)

    def text(self) -> str:
        t = "".join(self.out)
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()


def page_text_links(raw: str) -> tuple[str, list[tuple[str, str]]]:
    body = raw[raw.find("<article"):] if "<article" in raw else raw[raw.find("<body"):]
    p = _Text()
    try:
        p.feed(body)
    except Exception:  # noqa: BLE001 — malformed page: keep what was parsed
        pass
    return p.text(), p.links


def page_text(raw: str) -> str:
    return page_text_links(raw)[0]


_META = re.compile(r'<meta name="([^"]+)" content="([^"]*)"')
_CRUMB = re.compile(r'<ol class="breadcrumb[^"]*">(.*?)</ol>', re.S)


def page_meta(raw: str) -> dict[str, str]:
    m: dict[str, str] = {}
    for k, v in _META.findall(raw[:6000]):
        m.setdefault(k, html.unescape(v))
    c = _CRUMB.search(raw[:12000])
    m["crumb"] = " > ".join(html.unescape(re.sub(r"<[^>]+>", "", x)).strip()
                            for x in re.findall(r"<li>(.*?)</li>", c.group(1), re.S) if re.sub(r"<[^>]+>", "", x).strip()) if c else ""
    return m


_XHELP = re.compile(r"ms-xhelp:/*\?id=([^&#\s]+)", re.I)
_CONTENT = re.compile(r"/content/\d+/([^/]+)/(\d+)\.html?", re.I)
_REL = re.compile(r"^(?:\.\./)*(?:([^/?#]+)/(?:\d{4}/)?)?(\d+)\.html?(?:[?#].*)?$", re.I)


def link_target(href: str, book: str) -> tuple[str | None, str | None, str | None]:
    """-> (help id, target book, target page id) for a link on a page of `book`. External links: (None, None, None)."""
    h = html.unescape(href or "").strip()
    m = _XHELP.search(h)
    if m:
        return m.group(1), None, None
    m = _CONTENT.search(h)
    if m:
        return None, m.group(1), m.group(2)
    if h.startswith(("#", "mailto:")) or re.match(r"^[a-z]+:", h, re.I):
        return None, None, None
    m = _REL.match(h)
    if m:
        return None, m.group(1) or book, m.group(2)
    return None, None, None


# ---------------------------------------------------------------- location / database
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
    up = Path(__file__).resolve().parent.parent           # <kb>\tools\infosys_local.py
    if (up / "knowledge.db").is_file() or (up / "helpviewer").is_dir():
        return up, "folder above the script"
    return FALLBACK_KB, "default"


def db_path(args) -> Path:
    if args.db:
        return Path(args.db)
    return kb_root(args.kb)[0] / "helpviewer" / f"infosys_{args.lang}.db"


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no index at {path} — run: python {Path(__file__).name} index --lang <en|de>")
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.create_function("fnm", 2, lambda s, pat: int(_book_ok(s or "", pat)), deterministic=True)
    return con


def find_mshc(lang: str) -> Path:
    folder = CATALOG / LANGS[lang][0]
    hits = sorted(folder.glob("BKINFOSYS3*.mshc"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not hits:
        sys.exit(f"no Beckhoff Infosys package (BKINFOSYS3*.mshc) in {folder}; pass --mshc")
    return hits[0]


def package_stamp(mshc: Path) -> str:
    st = mshc.stat()
    return f"{mshc.name}|{st.st_size}|{int(st.st_mtime)}"


def ro_uri(db: Path) -> str:
    """'file:/C:/kb/x.db?mode=ro'; UNC paths as 'file:////server/share/x.db' (Path.as_uri gives a host part SQLite refuses)."""
    from urllib.parse import quote
    s = str(db.resolve()).replace("\\", "/")
    s = "//" + s if s.startswith("//") else (s if s.startswith("/") else "/" + s)
    return "file:" + quote(s, safe="/:") + "?mode=ro"


def read_meta(path: Path) -> dict[str, str]:
    """meta of an index file, read-only; {} when it is missing or not readable."""
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


def side_files(final: Path) -> tuple[Path, Path]:
    return final.with_name(final.name + ".building"), final.with_name(final.name + ".lock")


def index_state(final: Path) -> dict:
    """The real state of the index: the live file (what every reader uses) and a rebuild in progress or abandoned.
    The same rules as the twincat-docs skill's kb_docs.py status."""
    building, lock = side_files(final)
    now = time.time()
    meta = read_meta(final)
    out: dict = {"database": str(final)}
    if not final.is_file():
        out["state"] = "missing"
    elif not meta.get("source"):
        out["state"] = "incomplete"          # a version-1 build that did not finish
    elif meta.get("schema") != SCHEMA_VERSION:
        out["state"] = "old_format"          # built by version 1: complete text, no link table
    else:
        out["state"] = "complete"
        pkg = Path(meta["mshc_path"]) if meta.get("mshc_path") else None
        if pkg is not None:
            if not pkg.is_file():
                out["package_note"] = f"the package it was built from is gone: {pkg}"
            elif package_stamp(pkg) != meta["source"]:
                out["state"] = "outdated"
                out["package_note"] = f"the Help Viewer package changed since the index was built: {pkg}"
            else:
                newer = [p for p in pkg.parent.glob("BKINFOSYS3*.mshc") if p != pkg and p.stat().st_mtime > pkg.stat().st_mtime]
                if newer:
                    out["state"] = "outdated"
                    out["package_note"] = f"a newer package exists: {max(newer, key=lambda p: p.stat().st_mtime)}"
    for k in ("pages", "books", "links", "links_resolved", "indexed", "lcid", "builder", "mshc_path"):
        if meta.get(k):
            out[k] = int(meta[k]) if k in ("pages", "books", "links", "links_resolved") and meta[k].isdigit() else meta[k]
    if meta.get("source"):
        out["package"] = meta["source"].split("|")[0]
    if building.is_file():
        bm = read_meta(building)
        beat = max([float(bm.get("heartbeat") or 0)] + ([lock.stat().st_mtime] if lock.is_file() else []))
        rb = {"file": str(building), "pages_done": int(bm.get("pages_done") or 0), "pages_total": int(bm.get("pages_total") or 0),
              "started": bm.get("started"), "seconds_since_progress": round(now - beat) if beat else None}
        if bm.get("state") == "complete":
            rb["state"] = "waiting_to_replace"   # finished, but the live file was held open by a reader
        elif beat and now - beat < HEARTBEAT_STALE_S:
            rb["state"] = "running"
        else:
            rb["state"] = "interrupted"
        out["rebuild"] = rb
    out["message"] = state_message(out)
    return out


def state_message(s: dict) -> str:
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


class BuildLock:
    """One build at a time: <db>.lock created exclusively; a lock whose heartbeat is older than
    HEARTBEAT_STALE_S belongs to a build that is no longer running and is taken over."""

    def __init__(self, final: Path):
        self.path = side_files(final)[1]

    def __enter__(self) -> "BuildLock":
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
                    try:
                        who = json.loads(self.path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        who = {}
                    sys.exit(f"another index build is running (pid {who.get('pid')}, started {who.get('started')}, "
                             f"last progress {age:.0f} s ago) — wait for it, or `stats` to see its progress")
                self.path.unlink(missing_ok=True)          # stale: that build is gone
        sys.exit(f"cannot take the build lock {self.path}")

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


def _replace(src: Path, dst: Path, wait_s: float = 60.0) -> bool:
    """os.replace with retries: on Windows the live index cannot be replaced while a reader has it open."""
    t0 = time.time()
    while True:
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            if time.time() - t0 > wait_s:
                return False
            time.sleep(1.0)


def cmd_index(args) -> None:
    mshc = Path(args.mshc) if args.mshc else find_mshc(args.lang)
    final = db_path(args)
    final.parent.mkdir(parents=True, exist_ok=True)
    stamp = package_stamp(mshc)
    building, _lock = side_files(final)
    with BuildLock(final) as lock:
        # a finished rebuild that could not replace the live file last time: just swap it in
        bm = read_meta(building)
        if bm.get("state") == "complete" and bm.get("source") == stamp and not args.force:
            if _replace(building, final):
                print(f"replaced {final} with the rebuild finished earlier ({bm.get('pages')} pages)")
                return
            sys.exit(f"{final} is still open in another program — close it (MCP tools, kb_docs.py) and run index again")
        meta = read_meta(final)
        if (meta.get("source") == stamp and meta.get("schema") == SCHEMA_VERSION and meta.get("state", "complete") == "complete"
                and not args.force):
            print(f"up to date: {final} ({meta.get('pages', '?')} pages from {mshc.name})")
            return
        why = ("--force" if args.force else "no index yet" if not meta else
               "older index format (no link table)" if meta.get("schema") != SCHEMA_VERSION and meta.get("source") else
               "the last build did not finish" if not meta.get("source") else "the Help Viewer package changed")
        print(f"building {final} ({why}); the current index stays in use until the new one is complete")
        building.unlink(missing_ok=True)                  # leftover of an interrupted build
        con = sqlite3.connect(str(building))
        con.executescript(SCHEMA)
        t0, n = time.time(), 0
        with zipfile.ZipFile(mshc) as z:
            names = [x for x in z.namelist() if x.lower().endswith((".html", ".htm"))]

            def progress(done: int, state: str = "building") -> None:
                con.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", [
                    ("state", state), ("pid", str(os.getpid())), ("pages_total", str(len(names))),
                    ("pages_done", str(done)), ("heartbeat", f"{time.time():.0f}")])
                con.commit()
                lock.beat()

            con.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", [
                ("started", time.strftime("%Y-%m-%d %H:%M:%S")), ("builder", f"{BUILDER} {BUILDER_VERSION}"),
                ("python", sys.version.split()[0])])
            progress(0)
            for i, name in enumerate(names, 1):
                parts = name.split("/")
                book = parts[0] if len(parts) >= 3 else ""
                pid = Path(name).stem
                raw = z.read(name).decode("utf-8", "replace")
                m = page_meta(raw)
                title = m.get("Title") or pid
                body, links = page_text_links(raw)
                con.execute("INSERT OR REPLACE INTO page VALUES(?,?,?,?,?,?,?,?)",
                            (pid, book, title, m.get("Microsoft.Help.Id", ""), m.get("Microsoft.Help.TOCParent", ""),
                             m.get("Microsoft.Help.DisplayVersion", ""), m.get("crumb", ""), body))
                con.execute("INSERT INTO fts(title, body, book, pid) VALUES(?,?,?,?)", (title, body, book, pid))
                rows = []
                for k, (href, text) in enumerate(links):
                    hid, tb, tp = link_target(href, book)
                    rows.append((book, pid, k, text, href, hid, tb, tp))
                con.executemany("INSERT INTO link VALUES(?,?,?,?,?,?,?,?)", rows)
                n += 1
                if i % 2000 == 0:
                    progress(i)
                    print(f"  {i}/{len(names)} pages, {time.time() - t0:.0f} s", flush=True)
        progress(n)
        # ms-xhelp links → pages through their help id; links to pages not in the package keep only the href
        con.execute("UPDATE link SET target_book = (SELECT p.book FROM page p WHERE p.help_id = link.hid LIMIT 1), "
                    "target_pid = (SELECT p.pid FROM page p WHERE p.help_id = link.hid LIMIT 1) WHERE hid IS NOT NULL")
        con.execute("UPDATE link SET target_book = NULL, target_pid = NULL WHERE target_pid IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM page p WHERE p.book = link.target_book AND p.pid = link.target_pid)")
        books = con.execute("SELECT COUNT(DISTINCT book) FROM page").fetchone()[0]
        nl = con.execute("SELECT COUNT(*) FROM link").fetchone()[0]
        nr = con.execute("SELECT COUNT(*) FROM link WHERE target_pid IS NOT NULL").fetchone()[0]
        con.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", [
            ("source", stamp), ("mshc_path", str(mshc.resolve())), ("lcid", LANGS[args.lang][1]),
            ("indexed", time.strftime("%Y-%m-%d %H:%M:%S")), ("schema", SCHEMA_VERSION), ("pages", str(n)),
            ("books", str(books)), ("links", str(nl)), ("links_resolved", str(nr))])
        progress(n)
        con.execute("INSERT INTO fts(fts) VALUES('optimize')")
        progress(n, "complete")
        con.close()
        if not _replace(building, final):
            sys.exit(f"the new index is complete ({building}) but {final} is open in another program — close it "
                     "(MCP tools, kb_docs.py) and run index again; it is swapped in without re-reading the package")
    print(f"indexed {n} pages ({books} books, {nr} of {nl} links resolved) from {mshc} into {final} in "
          f"{time.time() - t0:.0f} s ({final.stat().st_size / 1e6:.0f} MB)")


def _lcid(con) -> str:
    r = con.execute("SELECT value FROM meta WHERE key='lcid'").fetchone()
    return r[0] if r else "1033"


def _book_ok(book: str, pattern: str | None) -> bool:
    return not pattern or fnmatch.fnmatch(book.lower(), pattern.lower())


def cmd_search(args) -> None:
    con = connect(db_path(args))
    # the book filter runs inside the query, before LIMIT (version 1 filtered the first limit × 8 hits only)
    where = "fts MATCH ?" + (" AND fnm(book, ?)" if args.book else "")
    params = [args.query] + ([args.book] if args.book else [])
    try:
        rows = con.execute("SELECT book, pid, title, snippet(fts, 1, '[', ']', '…', 18) AS s, bm25(fts, 8.0, 1.0) AS r "
                           f"FROM fts WHERE {where} ORDER BY r LIMIT ?", params + [args.limit]).fetchall()
        total = con.execute(f"SELECT COUNT(*) FROM fts WHERE {where}", params).fetchone()[0]
    except sqlite3.OperationalError as exc:
        sys.exit(f"bad query ({exc}); FTS5 syntax: words, \"phrase\", OR, NOT, prefix*")
    for r in rows:
        snip = re.sub(r"\s+", " ", r["s"])
        print(f"{r['title']} | {r['book']}/{r['pid']} | {snip}")
    if not rows:
        print("no hits" + (f" in books {args.book}" if args.book else ""))
    elif total > len(rows):
        print(f"({len(rows)} of {total} hits; --limit for more)")


def _candidates(rows) -> list:
    # library pages (tcplclib_*) first, then the shortest book name — the order the list is shown in
    return sorted(rows, key=lambda c: (not c["book"].startswith("tcplclib_"), len(c["book"]), c["book"]))


def _resolve(con, ref: str, book_glob: str | None = None):
    """Page for REF: Infosys URL → book/id → bare id → exact title (case-insensitive). Several pages → the list is
    printed and the command stops (version 1 showed the first one): pass book/id or --book."""
    ref = ref.strip()
    m = re.search(r"/content/\d+/([^/]+)/(\d+)\.html?", ref)
    if m:
        return con.execute("SELECT * FROM page WHERE book=? AND pid=?", (m.group(1), m.group(2))).fetchone()
    m = _XHELP.search(ref)
    if m:
        return con.execute("SELECT * FROM page WHERE help_id=?", (m.group(1),)).fetchone()
    if "/" in ref:
        b, p = ref.split("/", 1)
        r = con.execute("SELECT * FROM page WHERE book=? AND pid=?", (b, p)).fetchone()
        if r is not None or p.isdigit():
            return r
    if ref.isdigit():
        cands = con.execute("SELECT * FROM page WHERE pid=?", (ref,)).fetchall()
    else:
        cands = con.execute("SELECT * FROM page WHERE title=? COLLATE NOCASE", (ref,)).fetchall()
    cands = _candidates([c for c in cands if _book_ok(c["book"], book_glob)])
    if not cands:
        return None
    if len(cands) > 1:
        lines = "\n".join(f"  {c['book']}/{c['pid']}  {c['title']}" + (f"  ({c['version']})" if c["version"] else "")
                          for c in cands[:20])
        sys.exit(f"{len(cands)} pages match '{ref}' — nothing shown; pick one (the library your project uses) and pass "
                 f"book/id or --book:\n{lines}" + ("\n  …" if len(cands) > 20 else ""))
    return cands[0]


def _header(con, r) -> str:
    url = WEB.format(lcid=_lcid(con), book=r["book"], pid=r["pid"])
    ver = f" | {r['version']}" if r["version"] else ""
    return f"{r['title']} | {r['book']}/{r['pid']}{ver}\n{url}\n"


def _links(con, r, within: str | None = None, limit: int = 30) -> str:
    """'Links:' line for a page (the resolved ones; within = only those whose text is in that part of the page)."""
    try:
        rows = con.execute("SELECT text, target_book, target_pid FROM link WHERE book=? AND pid=? AND target_pid IS NOT NULL "
                           "ORDER BY n", (r["book"], r["pid"])).fetchall()
    except sqlite3.OperationalError:
        return ""                                     # version-1 index: no link table
    seen, out = set(), []
    for x in rows:
        ref = f"{x['target_book']}/{x['target_pid']}"
        if ref in seen or (within is not None and x["text"] not in within):
            continue
        seen.add(ref)
        out.append(f"{x['text']} → {ref}")
        if len(out) >= limit:
            break
    return ("\nLinks: " + "; ".join(out)) if out else ""


def cmd_show(args) -> None:
    con = connect(db_path(args))
    r = _resolve(con, args.ref, args.book)
    if r is None:
        sys.exit(f"not found: {args.ref} (try: search \"{args.ref}\")")
    body = r["body"]
    if args.section:
        parts = re.split(r"(?m)^(#{1,4} .*)$", body)
        keep, on = [], False
        for p in parts:
            if re.match(r"#{1,4} ", p):
                on = args.section.lower() in p.lower()
            if on:
                keep.append(p)
        body = "".join(keep).strip() or f"(no section matching '{args.section}')"
    shown = body
    if len(body) > args.max:
        body = body[:args.max] + f"\n… [{len(r['body']) - args.max} more chars; --max or --section]"
        shown = shown[:args.max]
    print(_header(con, r) + "\n" + body + _links(con, r, within=shown))


def cmd_decl(args) -> None:
    con = connect(db_path(args))
    r = _resolve(con, args.symbol, args.book)
    if r is None:
        sys.exit(f"not found: {args.symbol}")
    body = r["body"]
    code = re.findall(r"```\n(.*?)\n```", body, re.S)
    req = re.search(r"(?ms)^#{1,4} (?:Requirements|Prerequisites|Voraussetzungen)\s*\n(.*?)(?=^#{1,4} |\Z)", body)
    out = [_header(con, r)]
    first = re.sub(r"(?m)^#.*$", "", body.split("```")[0]).strip()
    if first:
        out.append(first[:400] + ("…" if len(first) > 400 else ""))
    for c in code:
        out.append("```\n" + c.strip() + "\n```")
    if req:
        out.append("Requirements:\n" + req.group(1).strip())
    also = con.execute("SELECT book, pid FROM page WHERE title=? COLLATE NOCASE AND NOT (book=? AND pid=?)",
                       (r["title"], r["book"], r["pid"])).fetchall()
    if also:
        out.append("Also in: " + ", ".join(f"{a['book']}/{a['pid']}" for a in _candidates(also)))
    lk = _links(con, r)
    if lk:
        out.append(lk.strip())
    print("\n".join(out))


def cmd_books(args) -> None:
    con = connect(db_path(args))
    for r in con.execute("SELECT book, COUNT(*) n FROM page GROUP BY book ORDER BY book"):
        if _book_ok(r["book"], args.filter):
            print(f"{r['book']}  {r['n']}")


def cmd_stats(args) -> None:
    s = index_state(db_path(args))
    if args.json:
        print(json.dumps(s, ensure_ascii=False))
        return
    print(f"{s['database']}: {s['message']}")
    for k in ("package", "mshc_path", "links", "links_resolved", "builder"):
        if s.get(k):
            print(f"  {k}: {s[k]}")
    if s.get("rebuild"):
        print(f"  rebuild: {s['rebuild']}")


def main() -> None:
    # --kb / --db / --lang work before or after the command (index --kb D:\\kb as well as --kb D:\\kb index)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lang", default=argparse.SUPPRESS, choices=sorted(LANGS))
    common.add_argument("--kb", default=argparse.SUPPRESS, help="knowledge folder (default: see above)")
    common.add_argument("--db", default=argparse.SUPPRESS, help="database file (default <kb>\\helpviewer\\infosys_<lang>.db)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    _sub = sub.add_parser
    sub.add_parser = lambda *a, **kw: _sub(*a, parents=[common], **kw)  # type: ignore[method-assign]
    s = sub.add_parser("index"); s.add_argument("--mshc"); s.add_argument("--force", action="store_true")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--book"); s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("show"); s.add_argument("ref"); s.add_argument("--max", type=int, default=4000)
    s.add_argument("--section"); s.add_argument("--book")
    s = sub.add_parser("decl"); s.add_argument("symbol"); s.add_argument("--book")
    s = sub.add_parser("books"); s.add_argument("--filter")
    s = sub.add_parser("stats"); s.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for k, v in (("lang", "en"), ("kb", None), ("db", None)):
        if not hasattr(args, k):
            setattr(args, k, v)
    if getattr(args, "limit", 1) < 1:
        ap.error("--limit must be 1 or more")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    {"index": cmd_index, "search": cmd_search, "show": cmd_show, "decl": cmd_decl,
     "books": cmd_books, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    main()
