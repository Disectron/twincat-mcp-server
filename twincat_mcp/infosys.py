"""Beckhoff Infosys reference lookup — the ``infosys_*`` tools.

Design goal: *never depend on somebody else's pre-built index*. Existing TwinCAT documentation
MCP servers ship an index that is missing whole libraries (Tc2_System is absent from one of
them), so ``ADSREAD`` comes back empty and ``get_page`` says "not found" for pages that exist.
This module works from two things that are always complete:

1. **The site's own sitemaps** (``/sitemap.xml`` → ``/content/1033/sitemapN.xml``): every
   English page URL with its ``<lastmod>``. That is the authoritative list of what exists,
   grouped by *book* (the path segment after ``/content/1033/``, e.g.
   ``tcplclib_tc2_system``). Cached for ``sitemap_ttl_days``.
2. **Live pages**, fetched on demand and converted to Markdown by our own HTML walker, then
   stored in a local SQLite index (FTS5 when the interpreter has it, LIKE otherwise). A book is
   indexed once (parallel fetch, polite worker count) and refreshed per page only when its
   ``lastmod`` in the sitemap changes.

On top of the index: symbol search by page title (FB/function/struct/enum pages are titled with
the symbol name), declaration extraction from the ``VAR_INPUT``/``VAR_OUTPUT`` code blocks plus
the description tables, full-text search with excerpts, and code-fragment search over the
``<pre>`` blocks. When the local index has no hit, an optional DuckDuckGo ``site:`` query finds
pages in books that were never indexed; every URL it yields is checked against the sitemap, so a
result is always a page that exists.

TwinCAT 3 only by default: TwinCAT 2 books (``tcplclib<name>`` without underscore, ``twincat2``
paths) are dropped. ``Tc2_System``, ``Tc2_Standard`` etc. are TwinCAT 3 libraries and stay.

Network: urllib only (no extra dependency), host-restricted to infosys.beckhoff.com (plus the
DuckDuckGo HTML endpoint for the fallback), honours HTTPS_PROXY. Nothing here writes to a PLC.
"""
from __future__ import annotations

import gzip
import html
import io
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

# Bump when the code that turns a page into stored rows changes (html_to_doc / _MdWalker, parse_declaration, classify,
# _store, the table layout). An index built by another INDEXER_VERSION is rebuilt once; otherwise nothing is
# re-parsed unless the page's content changed. tests/test_infosys_local.py fingerprints that code and fails when it
# changes without a bump.
INDEXER_VERSION = 4          # 3: code without non-breaking spaces; the return value parsed into the header (R24);
                             #    Methods / Properties tables as their own lists (N74)
                             # 4: everything a reader used to repair is stored repaired (docs/KNOWLEDGE_BASE.md rule 4):
                             #    no invisible marks, image placeholders or empty links; links resolved to page URLs
                             #    (relative ones while parsing, ms-xhelp ones after the build); table cells plain text
                             #    for the declaration (N77: a type is its name, its page in type_ref) and type names
                             #    split in narrow columns joined (R32); enum values numbered, comments as descriptions
                             #    (R25), no repeated description dict

INFOSYS = "https://infosys.beckhoff.com"
LANG = "1033"
CONTENT_PREFIX = f"{INFOSYS}/content/{LANG}/"
ALLOWED_HOSTS = {"infosys.beckhoff.com"}
DDG_HOSTS = {"html.duckduckgo.com", "duckduckgo.com"}
USER_AGENT = "twincat-mcp/0.6 (+https://github.com/; documentation lookup for an engineering tool)"
CODE_SEP = "\n\x1e\n"
DERIVED_VERSION = 1          # catalog / code_blocks layout; a bump rebuilds them from the stored pages (no re-parse)


BOOK_CATEGORIES = ("plc-library", "function-tf", "engineering-te", "twincat-system", "hardware", "other")
_SYMBOL_TITLE = re.compile(r"^(?:(?:FUNCTION_BLOCK|FUNCTION|PROGRAM|METHOD|PROPERTY|TYPE|INTERFACE|ACTION)\s+)?"
                           r"([A-Za-z_][\w.]*)\s*(?:\(\s*\))?$", re.I)


def _like(s: str) -> str:
    """A LIKE pattern matching ``s`` literally (R27: '%' matched everything, 'ADS_EAD' matched ADSREAD)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _check_limit(limit: Any, hi: int = 50, name: str = "limit") -> int:
    """R31: limit=-1 dropped the last item, limit=0 returned one, limit=1000 had no cap."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= hi:
        raise InfosysError(f"{name} {limit!r} must be 1..{hi}")
    return limit


_XHELP = re.compile(r"ms-xhelp:///?\?Id=([^)\s\"'&#]+)", re.I)
# a link while a page is converted: \ue001 text \ue002 href \ue003 (private-use marks: the text keeps its own
# brackets); _md_links turns them into [text](href), _plain into the text alone
_LINK = re.compile("\ue001([^\ue001\ue002]*)\ue002([^\ue003]*)\ue003")
_STRAY = re.compile("\ue002[^\ue003]*\ue003")


def _md_links(s: str) -> str:
    """Link marks → [text](href); a link without text (an image link whose image says nothing) is dropped."""
    prev = None
    while prev != s:
        prev = s
        s = _LINK.sub(lambda m: f"[{m.group(1).strip()}]({m.group(2)})" if m.group(1).replace("\ue000", "").strip()
                      else " ", s)
    s = _STRAY.sub("", s).replace("\ue001", "")         # a link split over two blocks: its text, without the link
    return s.replace("\ue004", "**").replace("\ue005", "`")


def _plain(s: str) -> str:
    """Link marks → the link text alone (table cells as data, N77)."""
    prev = None
    while prev != s:
        prev = s
        s = _LINK.sub(lambda m: m.group(1), s)
    return _STRAY.sub("", s).replace("\ue001", "").replace("\ue004", "").replace("\ue005", "")


def _first_href(s: str) -> str | None:
    """The target of the first link that has text (an image-only link says nothing)."""
    for m in _LINK.finditer(s):
        if m.group(1).replace("\ue000", "").strip():
            return m.group(2)
    return None


_PKG_REL = re.compile(r"^(?:\.\./)*(?:([^/?#:]+)/(?:\d{4}/)?)?(\d+)\.html?(?:[?#].*)?$", re.I)


def resolve_href(href: str, base_url: str | None, package: bool) -> str:
    """A link target as a page address, when it can be one: in a Help Viewer package page a relative link is
    '[../][<book>/][<lcid>/]<id>.htm' (the package layout) — the page's Infosys URL; on a web page the URL relative to
    the page. ms-xhelp links are resolved after the build (resolve_help_links); anchors, mail and other schemes stay."""
    h = html.unescape(href or "").strip()
    if not h or h.startswith("#") or _XHELP.search(h) or re.match(r"^[a-z][a-z0-9+.-]*:", h, re.I):
        return h
    if base_url is None:
        return h
    if package:
        b = book_of(base_url)
        m = _PKG_REL.match(h)
        if b and m:
            lcid = base_url[len(f"{INFOSYS}/content/"):].split("/", 1)[0]
            return f"{INFOSYS}/content/{lcid}/{(m.group(1) or b[0]).lower()}/{m.group(2)}.html"
        return h
    return urllib.parse.urljoin(base_url, h)


def _text_of(markdown: str) -> str:
    """The searchable text of a page: its Markdown without the code blocks (those are the code column)."""
    return re.sub(r"```.*?```", " ", markdown, flags=re.S)


def _bump_gen(con: Any) -> None:
    """The pages table changed: derived lookup tables are out of step until sync_derived()."""
    r = con.execute("SELECT value FROM meta WHERE key = 'pages_gen'").fetchone()
    n = int(r[0]) + 1 if r is not None and str(r[0]).isdigit() else 1
    con.execute("INSERT OR REPLACE INTO meta VALUES ('pages_gen', ?)", (str(n),))

KINDS = ("FunctionBlock", "Function", "Program", "Method", "Property", "Action", "Enum", "Struct",
         "Union", "Alias", "Interface", "GlobalVariable", "Library", "Page")

# TwinCAT 2 book slugs. TC3 PLC libraries are tcplclib_tc2_* / tcplclib_tc3_*; the TC2 ones have no underscore.
TC2_BOOK_RES = (re.compile(r"^tcplclib[a-z0-9]+$"), re.compile(r"twincat2"), re.compile(r"^tc2(?:[^_]|$)"),
                re.compile(r"^tcsystemmanager$"), re.compile(r"^tcplccontrol$"))


class InfosysError(Exception):
    pass


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
class Fetcher:
    """Minimal HTTPS client. Host allow-list, gzip, one retry. Injected into the index so tests
    can feed synthetic pages."""

    def __init__(self, timeout_s: float = 20.0):
        self.timeout_s = timeout_s

    def get(self, url: str, allow_hosts: set[str] | None = None) -> bytes:
        host = urllib.parse.urlparse(url).hostname or ""
        if host not in (allow_hosts or ALLOWED_HOSTS):
            raise InfosysError(f"refusing to fetch {host!r}: only {sorted(allow_hosts or ALLOWED_HOSTS)} are allowed")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip",
                                                   "Accept": "text/html,application/xml;q=0.9,*/*;q=0.8"})
        last: Exception | None = None
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310 - host checked above
                    data = resp.read()
                    if resp.headers.get("Content-Encoding", "").lower() == "gzip" or data[:2] == b"\x1f\x8b":
                        data = gzip.decompress(data)
                    return data
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise InfosysError(f"HTTP 404 for {url}") from exc
                last = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
            if attempt == 0:
                time.sleep(0.5)
        raise InfosysError(f"fetch failed for {url}: {last}")


# ---------------------------------------------------------------------------
# HTML → Markdown
# ---------------------------------------------------------------------------
_BLOCK = {"p", "div", "section", "article", "main", "body", "html", "br", "hr", "table", "ul", "ol", "li",
          "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dl", "dt", "dd", "tr", "figure", "figcaption"}
_SKIP = {"script", "style", "noscript", "nav", "header", "footer", "svg", "iframe", "button", "form"}


@dataclass
class HtmlDoc:
    title: str = ""
    markdown: str = ""
    code_blocks: list[tuple[str, str]] = field(default_factory=list)   # (language, code)
    tables: list[list[list[str]]] = field(default_factory=list)          # table -> rows -> cells (plain text, N77)
    headings: list[tuple[int, str]] = field(default_factory=list)
    table_titles: list[str] = field(default_factory=list)               # per table: the heading above it ('Methods')
    meta: dict[str, str] = field(default_factory=dict)                   # <meta name=…> (Help Viewer pages)
    crumb: str = ""                                                      # breadcrumb (Help Viewer pages)
    table_links: list[list[list[str | None]]] = field(default_factory=list)   # per cell: its first link target


_TABLE_MARK = "\ue010"          # placeholder of table k in the output until result() renders it: \ue010k\ue010


class _MdWalker(HTMLParser):
    def __init__(self, base_url: str | None = None, package: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url, self.package = base_url, package
        self.doc = HtmlDoc()
        self.out: list[str] = []
        self.inline: list[str] = []
        self.skip = 0
        self.pre = 0
        self.pre_buf: list[str] = []
        self.list_stack: list[tuple[str, int]] = []
        self.heading: int | None = None
        self.in_title = False
        self.title_buf: list[str] = []
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.cell_is_header = False
        self.href: list[str] = []
        self.code_inline = 0
        self.bold = 0

    # -- helpers --------------------------------------------------------------
    def _flush(self) -> None:
        raw = "".join(self.inline)
        self.inline = []
        if self.cell is not None:
            text = " ".join(raw.split())
            if _plain(text).replace("\ue000", "").strip():
                self.cell.append(text)           # links stay marked: a cell is rendered twice (result)
            return
        text = " ".join(_md_links(raw).split())
        if not text.replace("\ue000", "").strip():
            return
        if self.heading:
            text = _nobr(text)
            self.out.append("#" * self.heading + " " + text)
            plain = _nobr(_plain(" ".join(raw.split())))
            self.doc.headings.append((self.heading, plain))
            if self.heading == 1 and not self.doc.title:
                self.doc.title = plain
            return
        if self.list_stack:
            kind, n = self.list_stack[-1]
            indent = "  " * (len(self.list_stack) - 1)
            bullet = f"{n}." if kind == "ol" else "-"
            self.out.append(f"{indent}{bullet} {_nobr(text)}")
            return
        self.out.append(text)   # paragraph: keep the markers until result() decides code vs prose

    def _emit_table(self) -> None:
        rows = [r for r in (self.table or []) if any(_plain(c).strip() for c in r)]
        self.table = None
        if not rows:
            return
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        self.doc.tables.append(rows)            # marked cells; result() makes them plain and renders the table
        self.doc.table_titles.append(self.doc.headings[-1][1] if self.doc.headings else "")
        self.out.append(f"{_TABLE_MARK}{len(self.doc.tables) - 1}{_TABLE_MARK}")

    def _render_tables(self) -> None:
        """Tables once every code block of the page is known: the cells as plain text (the declaration's data, N77)
        with the first link of each cell aside, type names split in a narrow column joined (R32, on the evidence of
        the page's own code), and the Markdown table with its links."""
        idents = {w.lower() for _l, code in self.doc.code_blocks for w in re.findall(r"[A-Za-z_]\w*", code)}
        md: dict[int, str] = {}
        for k, rows in enumerate(self.doc.tables):
            plain_hdr = [_nobr(_plain(c)) for c in rows[0]]
            types = {j for j, h in enumerate(plain_hdr) if _TYPE_HEADER.match(h.strip("*` ").strip())}
            plain_rows, link_rows, md_rows = [], [], []
            for i, r in enumerate(rows):
                pr, lr, mr = [], [], []
                for j, c in enumerate(r):
                    p = _nobr(_plain(c))
                    rich = _nobr(_md_links(c))
                    if i:
                        mended = _mend_cell(p, idents, j in types)
                        if mended != p:
                            href = _first_href(c)
                            if rich == p:              # a cell without markup: the Markdown shows the joined name
                                rich = mended
                            elif href is not None and rich == f"[{p}]({href})":     # the cell is one link
                                rich = f"[{mended}]({href})"
                        p = mended
                    pr.append(p)
                    lr.append(_first_href(c))
                    mr.append(rich)
                plain_rows.append(pr)
                link_rows.append(lr)
                md_rows.append(mr)
            self.doc.tables[k] = plain_rows
            self.doc.table_links.append(link_rows)
            esc = lambda c: c.replace("|", "\\|")  # noqa: E731
            width = len(md_rows[0])
            lines = ["| " + " | ".join(esc(c) for c in md_rows[0]) + " |", "|" + "---|" * width]
            lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in md_rows[1:]]
            md[k] = "\n".join(lines)
        self.out = [md[int(b[1:-1])] if b.startswith(_TABLE_MARK) else b for b in self.out]

    # -- parser callbacks -------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = True
            return
        if self.pre:
            if tag == "br":
                self.pre_buf.append("\n")
            return
        if tag == "pre":
            self._flush()
            self.pre = 1
            self.pre_buf = []
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self.heading = int(tag[1])
            return
        if tag in ("ul", "ol"):
            self._flush()
            self.list_stack.append((tag, 0))
            return
        if tag == "li":
            self._flush()
            if self.list_stack:
                k, n = self.list_stack[-1]
                self.list_stack[-1] = (k, n + 1)
            return
        if tag == "table":
            self._flush()
            self.table = []
            return
        if tag == "tr" and self.table is not None:
            self.row = []
            return
        if tag in ("td", "th") and self.row is not None:
            self.cell = []
            self.cell_is_header = tag == "th"
            return
        if tag == "br":
            self.inline.append(" \ue000 ")   # line-break marker (private-use char: str.split() keeps it); becomes a newline if the block turns out to be code
            return
        if tag == "a" and a.get("href"):
            self.href.append(resolve_href(a["href"], self.base_url, self.package))
            self.inline.append("\ue001")
            return
        if tag == "img":
            # an image is its alt text when that says something; Help Viewer alts are image ids ('1107452'): nothing
            alt = (a.get("alt") or "").translate(_INVISIBLE).strip()
            if alt and not alt.isdigit():
                self.inline.append(f"![{alt}]")
            return
        if tag == "code":
            self.code_inline += 1
            self.inline.append("\ue005")         # '`' in Markdown, nothing in a plain table cell
            return
        if tag in ("b", "strong"):
            self.bold += 1
            self.inline.append("\ue004")         # '**' in Markdown, nothing in a plain table cell
            return
        if tag in _BLOCK:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "title":
            self.in_title = False
            return
        if self.pre:
            if tag == "pre":
                # R24 root: Help Viewer code uses non-breaking spaces for indentation; every consumer (the
                # declaration parser, code search, what an agent pastes into TwinCAT) needs plain spaces
                code = html.unescape("".join(self.pre_buf)).translate(_CODE_SPACES).strip("\n")
                self.pre = 0
                if code.strip():
                    lang = _guess_lang(code)
                    self.doc.code_blocks.append((lang, code))
                    self.out.append(f"```{lang}\n{code}\n```")
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self.heading = None
            return
        if tag in ("ul", "ol"):
            self._flush()
            if self.list_stack:
                self.list_stack.pop()
            return
        if tag == "li":
            self._flush()
            return
        if tag in ("td", "th") and self.cell is not None:
            self._flush()
            txt = " ".join(self.cell)
            if self.row is not None:
                self.row.append(txt)
            self.cell = None
            return
        if tag == "tr" and self.row is not None and self.table is not None:
            self.table.append(self.row)
            self.row = None
            return
        if tag == "table" and self.table is not None:
            self._emit_table()
            return
        if tag == "a" and self.href:
            href = self.href.pop()
            self.inline.append(f"\ue002{href}\ue003")
            return
        if tag == "code" and self.code_inline:
            self.code_inline -= 1
            self.inline.append("\ue005")
            return
        if tag in ("b", "strong") and self.bold:
            self.bold -= 1
            self.inline.append("\ue004")
            return
        if tag in _BLOCK:
            self._flush()

    def handle_data(self, data):
        if self.skip:
            return
        if self.in_title:
            self.title_buf.append(data.translate(_INVISIBLE))
            return
        if self.pre:
            self.pre_buf.append(data)
            return
        # zero-width spaces, joiners and soft hyphens are line-break hints of the page layout, not text
        self.inline.append(data.translate(_INVISIBLE))

    def result(self) -> HtmlDoc:
        self._flush()
        if self.table is not None:
            self._emit_table()
        self._render_tables()
        if not self.doc.title and self.doc.headings:
            self.doc.title = self.doc.headings[0][1]
        if not self.doc.title:
            t = " ".join("".join(self.title_buf).split())
            self.doc.title = re.split(r"\s+[-|–]\s+Beckhoff", t)[0].strip()
        if not self.doc.code_blocks:
            self.out = [self._reflow_inline_code(b) for b in self.out]
        self.out = [b if b.startswith(("```", "|")) else _nobr(b) for b in self.out]
        self.doc.markdown = "\n\n".join(b for b in self.out if b.strip())
        return self.doc

    _INLINE_DECL = re.compile(r"\b(FUNCTION_BLOCK|FUNCTION|PROGRAM|METHOD|INTERFACE|TYPE|VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_GLOBAL)\b"
                              r".*\bEND_(?:VAR|TYPE|STRUCT|FUNCTION_BLOCK|FUNCTION|PROGRAM|METHOD|INTERFACE)\b", re.S)

    def _reflow_inline_code(self, block: str) -> str:
        """Pages that ship declarations in <div>/<p> + <br> instead of <pre> arrive as one paragraph.
        Rebuild line structure so the declaration parser still sees them."""
        if block.startswith(("#", "|", "```", "-", "!")) or not self._INLINE_DECL.search(block):
            return block
        code = re.sub(r"\s*\ue000\s*", "\n", block)
        code = re.sub(r"[ \t]*;[ \t]*(?=[^\n/(])", ";\n", code)   # several declarations on one line
        code = re.sub(r"\s*\b(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_GLOBAL|VAR|END_VAR|STRUCT|END_STRUCT|END_TYPE|"
                      r"END_FUNCTION_BLOCK|END_FUNCTION|END_PROGRAM|END_METHOD|END_INTERFACE)\b\s*", r"\n\1\n", code)
        code = re.sub(r"\n{2,}", "\n", code).strip()
        self.doc.code_blocks.append(("st", code))
        return f"```st\n{code}\n```"


_VERSIONISH = re.compile(r"(TwinCAT|\bv?\d+\.\d+)", re.I)


def _doc_version(raw: str | None) -> str | None:
    """D2: Microsoft.Help.DisplayVersion, unescaped — and only when it reads like a version ('Tc2_Utilities (TwinCAT
    v3.1.0)'); some non-library pages carry API description text in that tag."""
    if not raw:
        return None
    v = " ".join(html.unescape(raw).split())
    if len(v) > 80 or not _VERSIONISH.search(v) or v.lower().startswith("type:"):
        return None
    return v


# blanks other than the space that Help Viewer pages put into code (indentation), and zero-width marks
_CODE_SPACES = str.maketrans({**{c: " " for c in "\u00a0\u2007\u202f\u2002\u2003\u2009\u200a"},
                              **{c: None for c in "\u200b\u200c\u200d\u2060\ufeff\u00ad"}})


def _nobr(text: str) -> str:
    return " ".join(text.replace("\ue000", " ").split())


_INVISIBLE = {0x200B: None, 0x200C: None, 0x200D: None, 0x2060: None, 0xFEFF: None, 0x00AD: None}
_TYPE_HEADER = re.compile(r"(?i)^(?:data\s*)?type$|^typ$|^datentyp$")
_IEC_WORDS = {"array", "of", "pointer", "reference", "to", "string", "wstring", "at", "constant", "retain", "persistent"}
_IDENT = re.compile(r"^[A-Za-z_]\w*$")


def _mend_cell(cell: str, idents: set[str], type_column: bool) -> str:
    toks = cell.replace("\ue000", " ").split()
    camel = lambda a, b: bool(re.search(r"[a-z0-9]$", a)) and b[:1].isupper()  # noqa: E731
    if 2 <= len(toks) <= 3 and all(_IDENT.match(t) for t in toks) \
            and all(camel(a, b) for a, b in zip(toks, toks[1:])):
        joined = "".join(toks)
        if joined.lower() in idents and toks[0].lower() not in idents:
            return joined
        if type_column and not any(t.lower() in _IEC_WORDS for t in toks):
            return joined
    out: list[str] = []                     # inside longer cells: 'T_Ams NetId' when the page's code declares it
    for t in toks:
        a = out[-1] if out else ""
        core, punct = re.match(r"^(.*?)([.,;:)]*)$", t).groups()
        if a and "_" in a and _IDENT.match(a) and _IDENT.match(core) and camel(a, core) \
                and (a + core).lower() in idents and a.lower() not in idents:
            out[-1] = a + core + punct
        else:
            out.append(t)
    return " ".join(out)


def _guess_lang(code: str) -> str:
    up = code.upper()
    if re.search(r"\b(VAR_INPUT|VAR_OUTPUT|END_VAR|FUNCTION_BLOCK|END_STRUCT|END_TYPE|END_IF|END_FOR|:=)", up) \
            and "<" not in code[:1]:
        return "st"
    if code.lstrip().startswith("<"):
        return "xml"
    if re.search(r"\b(#include|std::|HRESULT|void\s+\w+\()", code):
        return "cpp"
    if re.search(r"\b(using System|namespace\s+\w+|public\s+(class|void|static))", code):
        return "csharp"
    return ""


def html_to_doc(raw: bytes | str, base_url: str | None = None, package: bool = False) -> HtmlDoc:
    """A page as stored: Markdown with every link a page address where it can be one (base_url: the page's URL;
    package: a Help Viewer package page, whose relative links follow the package layout)."""
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    w = _MdWalker(base_url, package)
    w.feed(text)
    w.close()
    return w.result()


# ---------------------------------------------------------------------------
# IEC declaration extraction
# ---------------------------------------------------------------------------
_HEAD_RE = re.compile(
    r"^\s*(?:\{[^}]*\}\s*)*(FUNCTION_BLOCK|FUNCTION|PROGRAM|METHOD|PROPERTY|INTERFACE|TYPE|VAR_GLOBAL|ACTION)\b\s*"
    r"(?:(?:ABSTRACT|FINAL|INTERNAL|PUBLIC|PRIVATE|PROTECTED)\s+)*([A-Za-z_][\w]*)?(.*)$", re.I | re.M)
_SECTION_RE = re.compile(r"^\s*(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_GLOBAL|VAR)\b[^\n]*\n(.*?)^\s*END_VAR", re.I | re.M | re.S)
_STRUCT_RE = re.compile(r"STRUCT\b(.*?)END_STRUCT", re.I | re.S)
_ENUM_RE = re.compile(r":\s*\((.*?)\)\s*(?:[A-Za-z_]\w*\s*)?(?::=\s*\w+)?\s*;", re.S)
_VARLINE_RE = re.compile(r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*(?:AT\s+%[IQM][XBWDL]?\*?\s*)?:\s*([^;:=]+?)"
                         r"(?:\s*:=\s*([^;]+?))?\s*;\s*(?:\(\*\s*(.*?)\s*\*\)|//\s*(.*))?\s*$", re.M)

_PREFIX_KIND = (("FB_", "FunctionBlock"), ("F_", "Function"), ("ST_", "Struct"), ("E_", "Enum"), ("I_", "Interface"),
                ("T_", "Alias"), ("U_", "Union"), ("ITc", "Interface"), ("P_", "Program"), ("PRG_", "Program"))


def _ident_matches(ident: str, title: str) -> bool:
    t = title.strip().split()[0] if title.strip() else ""
    return ident.lower() in (t.lower(), t.rsplit(".", 1)[-1].lower())


def classify(title: str, code_blocks: list[tuple[str, str]]) -> str:
    name = title.strip()
    for _lang, code in code_blocks:
        m = _HEAD_RE.search(code)
        if not m:
            continue
        kw, ident = m.group(1).upper(), m.group(2) or ""
        if kw not in ("TYPE", "VAR_GLOBAL") and ident and name and not _ident_matches(ident, name):
            continue  # example code about another symbol; keep looking
        if kw == "TYPE":
            seg = code[m.start():m.start() + 400].upper()
            if re.search(r"\bSTRUCT\b", seg):
                return "Struct"
            if re.search(r"\bUNION\b", seg):
                return "Union"
            if re.search(r":\s*\(", seg):
                return "Enum"
            return "Alias"
        return {"FUNCTION_BLOCK": "FunctionBlock", "FUNCTION": "Function", "PROGRAM": "Program", "METHOD": "Method",
                "PROPERTY": "Property", "INTERFACE": "Interface", "ACTION": "Action", "VAR_GLOBAL": "GlobalVariable"}[kw]
    for pre, kind in _PREFIX_KIND:
        if name.startswith(pre):
            return kind
    if any(re.search(r"\bVAR_INPUT\b", c, re.I) for _l, c in code_blocks):
        return "FunctionBlock"
    if re.search(r"\bPLC\s+Lib(?:rary)?\b", name, re.I):
        return "Library"
    return "Page"


def _parse_vars(body: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _VARLINE_RE.finditer(body):
        names, typ, default, c1, c2 = m.groups()
        for n in [x.strip() for x in names.split(",")]:
            out.append({"name": n, "type": " ".join(typ.split()), "default": (default or "").strip() or None,
                        "comment": (c1 or c2 or "").strip() or None})
    return out


def _table_kind(rows: list[list[str]], title: str = "") -> str | None:
    """What a 'Name | …' table of a symbol page lists: 'methods' / 'properties' by the heading above it (an interface
    page has 'Name | Description' under 'Methods' — the header alone says nothing), else by its columns: Access, or
    Type with Definition location → properties; Definition location, or every name written as a call → methods;
    otherwise 'vars' (inputs, outputs, members, enum values). None: not a Name table."""
    if not rows:
        return None
    hdr = [c.strip().lower() for c in rows[0]]
    if not hdr or "name" not in hdr[0]:
        return None
    t = (title or "").lower()
    if "method" in t:
        return "methods"
    if "propert" in t:
        return "properties"
    if re.search(r"\b(?:inputs?|outputs?|in[ /_-]?outs?|parameters?|members?|values?|variables?)\b", t):
        return "vars"
    has = lambda key: any(key in h for h in hdr)  # noqa: E731
    if has("access") or (has("type") and has("definition location")):
        return "properties"
    names = [r[0].strip() for r in rows[1:] if r and r[0].strip()]
    if has("definition location") or (names and all(n.endswith(")") for n in names)):
        return "methods"
    return "vars"


def _member_tables(tables: list[list[list[str]]], titles: list[str] | None = None,
                   links: list[list[list[str | None]]] | None = None
                   ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """N74: the Methods and Properties tables of an FB / interface page as their own lists — they were read as
    variable descriptions, so 'description' carried the method list ('Configure()': …). N77: a property's type is its
    name (the cells are plain text); the page of the type, when the cell links to one, is type_ref."""
    methods: list[dict[str, Any]] = []
    props: list[dict[str, Any]] = []
    for k, rows in enumerate(tables):
        kind = _table_kind(rows, (titles or [])[k] if titles and k < len(titles) else "")
        if kind not in ("methods", "properties"):
            continue
        hdr = [c.strip().lower() for c in rows[0]]
        lk = links[k] if links and k < len(links) else None

        def idx(*keys: str) -> int | None:
            return next((i for i, h in enumerate(hdr) if any(key in h for key in keys)), None)

        def col(r: list[str], *keys: str) -> str | None:
            i = idx(*keys)
            return r[i].strip() if i is not None and i < len(r) and r[i].strip() else None
        for n, r in enumerate(rows[1:], 1):
            if not r or not r[0].strip():
                continue
            name = re.sub(r"\s*\(\s*\)\s*$", "", r[0].strip())
            if kind == "methods":
                methods.append({"name": name, "definition_location": col(r, "definition location"),
                                "description": col(r, "descr", "meaning", "comment")})
            else:
                p = {"name": name, "type": col(r, "type"), "access": col(r, "access"),
                     "definition_location": col(r, "definition location"),
                     "initial_value": col(r, "initial value", "default"),
                     "description": col(r, "descr", "meaning", "comment")}
                ti = idx("type")
                ref = lk[n][ti] if lk is not None and ti is not None and n < len(lk) and ti < len(lk[n]) else None
                if ref:
                    p["type_ref"] = ref
                props.append(p)
    return methods, props


def _type_refs(tables: list[list[list[str]]], titles: list[str] | None = None,
               links: list[list[list[str | None]]] | None = None) -> dict[str, str]:
    """N77: the page of a variable's type, when its row of a description table links the type: {name: url}."""
    out: dict[str, str] = {}
    for k, rows in enumerate(tables):
        if _table_kind(rows, (titles or [])[k] if titles and k < len(titles) else "") != "vars" or not links \
                or k >= len(links):
            continue
        ti = next((i for i, h in enumerate(c.strip().lower() for c in rows[0]) if _TYPE_HEADER.match(h)), None)
        if ti is None:
            continue
        for n, r in enumerate(rows[1:], 1):
            ref = links[k][n][ti] if n < len(links[k]) and ti < len(links[k][n]) else None
            if r and r[0].strip() and ref:
                out.setdefault(r[0].strip(), ref)
    return out


def _descriptions(tables: list[list[list[str]]], titles: list[str] | None = None) -> dict[str, str]:
    """Infosys description tables of variables: header has Name | Type | Description (sometimes without Type) —
    not the Methods / Properties tables (_member_tables)."""
    desc: dict[str, str] = {}
    for k, rows in enumerate(tables):
        if _table_kind(rows, (titles or [])[k] if titles and k < len(titles) else "") != "vars":
            continue
        hdr = [c.strip().lower() for c in rows[0]]
        di = next((i for i, h in enumerate(hdr) if "descr" in h or "meaning" in h or "comment" in h), len(hdr) - 1)
        for r in rows[1:]:
            if r and r[0].strip():
                desc.setdefault(r[0].strip(), r[di].strip() if di < len(r) else "")
    return desc


def _library_from_tables(tables: list[list[list[str]]]) -> str | None:
    for rows in tables:
        hdr = " ".join(rows[0]).lower()
        if "plc librar" in hdr or "library" in hdr and "target" in hdr:
            li = next((i for i, h in enumerate(rows[0]) if "librar" in h.lower()), len(rows[0]) - 1)
            for r in rows[1:]:
                if li < len(r) and r[li].strip():
                    m = re.match(r"\s*([A-Za-z][\w\.]*)", r[li])
                    if m:
                        return m.group(1)
    return None


def library_from_book(book: str) -> str | None:
    m = re.match(r"tcplclib_(tc[23])_(.+)", book)
    if not m:
        return None
    gen, rest = m.groups()
    return gen.capitalize() + "_" + "".join(p.capitalize() for p in rest.split("_"))


def parse_declaration(title: str, doc: HtmlDoc) -> dict[str, Any]:
    kind = classify(title, doc.code_blocks)
    decl: dict[str, Any] = {"name": title.strip(), "kind": kind, "declaration": None, "extends": None, "implements": None,
                            "return_type": None, "inputs": [], "outputs": [], "in_out": [], "members": [],
                            "enum_values": [], "description": {}}
    desc = _descriptions(doc.tables, doc.table_titles)
    fragments: list[str] = []   # header-less VAR_INPUT/VAR_OUTPUT blocks (how Infosys documents most FBs)
    for _lang, code in doc.code_blocks:
        m = _HEAD_RE.search(code)
        if not m:
            if not re.search(r"\bVAR_(?:INPUT|OUTPUT|IN_OUT)\b", code, re.I):
                continue
            fragments.append(code.strip())
            kw, ident, rest = "", "", ""
        else:
            kw, ident, rest = m.group(1).upper(), m.group(2) or "", m.group(3) or ""
            if kw not in ("TYPE", "VAR_GLOBAL") and ident and not _ident_matches(ident, decl["name"]):
                continue
            if decl["declaration"] is None:
                decl["declaration"] = code.strip()
        if kw in ("FUNCTION", "METHOD", "PROPERTY"):
            rm = re.search(r":\s*([A-Za-z_][\w\s\[\]\.\(\)]*?)\s*(?:$|\n)", rest + "\n")
            if rm:
                decl["return_type"] = rm.group(1).strip()
        em = re.search(r"\bEXTENDS\s+([\w\.]+)", rest, re.I)
        im = re.search(r"\bIMPLEMENTS\s+([\w\.,\s]+)", rest, re.I)
        if em:
            decl["extends"] = em.group(1)
        if im:
            decl["implements"] = [x.strip() for x in im.group(1).split(",") if x.strip()]
        for sm in _SECTION_RE.finditer(code):
            sec, body = sm.group(1).upper(), sm.group(2)
            items = _parse_vars(body)
            for it in items:
                it["description"] = desc.get(it["name"])
            if sec == "VAR_INPUT":
                decl["inputs"] += items
            elif sec == "VAR_OUTPUT":
                decl["outputs"] += items
            elif sec == "VAR_IN_OUT":
                decl["in_out"] += items
            elif sec == "VAR_GLOBAL":
                decl["members"] += items
        if kw == "TYPE":
            st = _STRUCT_RE.search(code)
            if st:
                items = _parse_vars(st.group(1))
                for it in items:
                    it["description"] = desc.get(it["name"])
                decl["members"] += items
            en = _ENUM_RE.search(code)
            if en and kind == "Enum":
                for part in en.group(1).split(","):
                    part = re.sub(r"\(\*.*?\*\)|//.*", "", part).strip()
                    if not part:
                        continue
                    nm, _, val = part.partition(":=")
                    decl["enum_values"].append({"name": nm.strip(), "value": val.strip() or None,
                                                "description": desc.get(nm.strip())})
    if decl["declaration"] is None and fragments:
        head = {"FunctionBlock": "FUNCTION_BLOCK", "Program": "PROGRAM", "Function": "FUNCTION"}.get(kind)
        decl["declaration"] = ((f"{head} {decl['name']}  (* header reconstructed *)\n" if head else "")
                               + "\n".join(fragments))
    decl["description"] = desc
    methods, props = _member_tables(doc.tables, doc.table_titles, doc.table_links)
    if methods:
        decl["methods"] = methods
    if props:
        decl["properties"] = props
    refs = _type_refs(doc.tables, doc.table_titles, doc.table_links)
    if refs:
        for key in ("inputs", "outputs", "in_out", "members"):
            for it in decl[key]:
                if refs.get(it["name"]):
                    it["type_ref"] = refs[it["name"]]
    short = decl["name"].split(".")[-1]
    if kind in ("Function", "Method"):
        # R24: Infosys documents the return value as a VAR_OUTPUT named like the function — it is the return type
        ret = next((o for o in decl["outputs"] if str(o.get("name", "")).lower() == short.lower()), None)
        if ret is not None:
            decl["outputs"] = [o for o in decl["outputs"] if o is not ret]
            decl["return_type"] = decl["return_type"] or ret.get("type")
            if ret.get("description"):
                decl["return_description"] = ret["description"]
        if decl["return_type"] and decl["declaration"]:
            decl["declaration"] = _return_in_header(decl["declaration"], short, decl["return_type"],
                                                    "FUNCTION" if kind == "Function" else "METHOD")
    _tidy_declaration(decl)                 # rule 4: stored as every reader needs it
    return decl


def _return_in_header(text: str, short: str, rtype: str, kw: str) -> str:
    """R24: Infosys documents a FUNCTION's return value as a VAR_OUTPUT line named like the function. Drop that line
    from every VAR_OUTPUT block (and the block once nothing but blanks is left), and give the header its ': type' —
    also when the header is already there (reconstructed '(* header reconstructed *)' or without the type)."""
    ret_line = re.compile(r"(?im)^[^\S\n]*" + re.escape(short) + r"[^\S\n]*:[^;\n]*;?[^\S\n]*(?:\(\*.*?\*\)|//[^\n]*)?[^\S\n]*\n?")

    def block(m: re.Match) -> str:
        body = ret_line.sub("", m.group(2))
        return "" if not body.strip() else m.group(1) + body + m.group(3)
    text = re.sub(r"(?is)(^[^\S\n]*VAR_OUTPUT\b[^\n]*\n)(.*?)(^[^\S\n]*END_VAR\b[^\n]*\n?)", block, text, flags=re.M)
    head = re.search(r"(?im)^([^\S\n]*)(FUNCTION|METHOD)([^\S\n]+(?:\w+[^\S\n]+)*?)" + re.escape(short) + r"\b([^\n]*)", text)
    if head:
        rest = head.group(4)
        if not re.match(r"\s*:", rest):                  # header without the return type
            note = rest.strip()
            text = (text[:head.start(4)] + f" : {rtype}" + (f"  {note}" if note else "") + text[head.end(4):])
        return re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return f"{kw} {short} : {rtype}\n" + re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def _tidy_declaration(decl: dict[str, Any]) -> None:
    """The last step of parse_declaration (index time, format 4): R25 implicit enum values are numbered and the
    declaration's (* … *) comments become descriptions; R32 the per-name 'description' dict loses what the items
    carry already."""
    name = decl.get("name") or ""
    if decl.get("kind") == "Enum" and decl.get("enum_values"):
        comments: dict[str, str] = {}
        body = decl.get("declaration") or ""
        for m in re.finditer(r"([A-Za-z_]\w*)\s*(?::=\s*[^,()\n]+)?\s*,?\s*(?:\(\*\s*(.*?)\s*\*\)|//\s*([^\n]*))", body, re.S):
            c = (m.group(2) or m.group(3) or "").strip()
            if c:
                comments.setdefault(m.group(1), c)
        nxt = 0
        for ev in decl["enum_values"]:
            v = ev.get("value")
            n = None
            if v not in (None, ""):
                vs = str(v).strip().replace("16#", "0x").replace("2#", "0b").replace("8#", "0o")
                try:
                    n = int(vs, 0)
                except ValueError:
                    n = None
            else:
                n = nxt
                ev["value"] = str(n)
                ev["implicit"] = True
            if n is not None:
                nxt = n + 1
            if not ev.get("description") and comments.get(ev.get("name", "")):
                ev["description"] = comments[ev["name"]]
    desc = decl.get("description")
    if isinstance(desc, dict) and not desc:
        decl.pop("description", None)                 # nothing to say: no empty field
    elif isinstance(desc, dict):
        carried = {it.get("name") for key in ("inputs", "outputs", "in_out", "members", "enum_values")
                   for it in (decl.get(key) or []) if it.get("description")}
        if decl.get("return_description"):
            carried.add(name.split(".")[-1])
        rest = {k: v for k, v in desc.items() if k not in carried}
        if rest:
            decl["description"] = rest
        else:
            decl.pop("description", None)


# ---------------------------------------------------------------------------
# book helpers
# ---------------------------------------------------------------------------
def book_of(url: str) -> tuple[str, str] | None:
    """('tcplclib_tc2_system', '30945803') for a content URL, else None."""
    if not url.startswith(CONTENT_PREFIX):
        return None
    rest = url[len(CONTENT_PREFIX):].split("?", 1)[0].split("#", 1)[0]
    parts = rest.split("/")
    if len(parts) < 2 or not parts[1].endswith(".html"):
        return None
    return parts[0], parts[1][:-5]


def is_tc2_book(book: str) -> bool:
    return any(r.search(book) for r in TC2_BOOK_RES)


def book_category(book: str) -> str:
    if book.startswith("tcplclib_"):
        return "plc-library"
    if re.match(r"^tf\d", book):
        return "function-tf"
    if re.match(r"^te\d", book):
        return "engineering-te"
    if re.match(r"^(tc3|tc1\d|tcinfosys|tcadsnet|tcadsdll|tcadsws|tcadsmqtt|tcadsserver)", book):
        return "twincat-system"
    if re.match(r"^(el|ek|ep|es|ej|epp|erp|elm|elx|kl|ks|bk|cx|c6|c9|cp|cu|ax|am|al|ag|ap|at|bc|bx|fc|il|ip|ps|ks)\d", book):
        return "hardware"
    return "other"


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------
class InfosysIndex:
    def __init__(self, cache_dir: str | Path | None = None, fetcher: Fetcher | None = None, books: list[str] | None = None,
                 exclude_books: list[str] | None = None, tc3_only: bool = True, max_workers: int = 6,
                 max_index_pages: int = 4000, sitemap_ttl_days: int = 7, web_fallback: bool = True,
                 fetch_timeout_s: float = 20.0, local: Any = None, db_file: str | Path | None = None,
                 read_only: bool = False):
        # local: a helpviewer.HelpViewerSource — the installed Help Viewer package replaces the website entirely
        # (page list, page content); nothing is fetched from the network and there is no web fallback.
        # db_file: the index file (default <cache_dir>/index.sqlite — the web cache).
        # read_only (docs/KNOWLEDGE_BASE.md): the knowledge folder's index of the local package is built as a whole by
        # infosys_build (written as <index>.building, swapped in when complete) and only READ here — never changed,
        # never rebuilt in place; an index of an older format is read as it is and said to need a rebuild.
        self.local = local
        if local is not None:
            web_fallback = False
        self.read_only = read_only
        if db_file is not None:
            self.db_file = Path(db_file)
            self.dir = self.db_file.parent
        elif cache_dir is not None:
            self.dir = Path(cache_dir)
            self.db_file = self.dir / "index.sqlite"
        else:
            raise ValueError("InfosysIndex needs cache_dir or db_file")
        if not read_only:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.fetch = fetcher or Fetcher(fetch_timeout_s)
        self.default_books = list(books or [])
        self.exclude = [re.compile(p, re.I) for p in (exclude_books or [])]
        self.tc3_only = tc3_only
        self.max_workers = max(1, min(16, max_workers))
        self.max_index_pages = max_index_pages
        self.sitemap_ttl = sitemap_ttl_days * 86400
        self.web_fallback = web_fallback
        self._sitemap: dict[str, str] | None = None     # url -> lastmod
        self._lock = threading.RLock()
        self._derived_lock = threading.RLock()
        self._index_lock = threading.RLock()   # one book indexes at a time: a background default index and an explicit
                                               # infosys_index of the same book must not both write (SQLite busy → 30 s stalls)
        self._fts: bool | None = None
        self._has_helpids = False
        self._probed_sig: tuple[int, int, int] | None = None
        self.format: int | None = None         # the index's format (INDEXER_VERSION it was built with)
        if read_only:
            self._probe()
        else:
            self._init_db()

    # -- storage ----------------------------------------------------------------
    # -- derived lookup tables (R17–R22): what is indexed, small enough to search cold -----------------------------
    # catalog:     one row per page (rowid = pages.rowid): url, title, kind, library, book — the symbol search reads
    #              this few-MB table instead of scanning the whole page store (R18: 16.9 s cold)
    # code_blocks: one row per code sample, \xa0 normalised (R17), language stored as is ('' = unlabelled, R22);
    #              searched in one pass over the samples alone, with the language filter in SQL (R19)
    # Built from the stored pages — no page is parsed again; kept in step after every index run.
    def _init_derived(self, con: Any) -> None:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS catalog(rowid INTEGER PRIMARY KEY, url TEXT, title TEXT, kind TEXT,
                                               library TEXT, book TEXT, fetched REAL);
            CREATE INDEX IF NOT EXISTS catalog_kind ON catalog(kind);
            CREATE TABLE IF NOT EXISTS code_blocks(id INTEGER PRIMARY KEY, page_rowid INTEGER, url TEXT, title TEXT,
                                                   library TEXT, lang TEXT, code TEXT);
            CREATE INDEX IF NOT EXISTS code_blocks_page ON code_blocks(page_rowid);
            CREATE INDEX IF NOT EXISTS code_blocks_lang ON code_blocks(lang COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS code_src(page_rowid INTEGER PRIMARY KEY, url TEXT, fetched REAL);
        """)

    def sync_derived(self) -> dict[str, Any]:
        """Bring catalog / code_blocks in step with the pages table (cheap when nothing changed). A read-only index
        has them from its build."""
        if self.read_only:
            return {"changed": False, "read_only": True}
        t0 = time.time()
        with self._derived_lock, self._db() as con:
            self._init_derived(con)
            g = con.execute("SELECT value FROM meta WHERE key = 'pages_gen'").fetchone()
            sig = g[0] if g is not None else "0"
            r = con.execute("SELECT value FROM meta WHERE key = 'derived_sig'").fetchone()
            if r is not None and r[0] == f"{DERIVED_VERSION}|{sig}":
                return {"changed": False, "seconds": round(time.time() - t0, 3)}
            # identity = rowid + url + fetched: a page stored again may get its old rowid back
            same = "EXISTS (SELECT 1 FROM pages p WHERE p.rowid = {t}.{k} AND p.url = {t}.url AND p.fetched IS {t}.fetched)"
            con.execute("DELETE FROM catalog WHERE NOT " + same.format(t="catalog", k="rowid"))
            con.execute("INSERT INTO catalog SELECT rowid, url, title, kind, library, book, fetched FROM pages "
                        "WHERE rowid NOT IN (SELECT rowid FROM catalog)")
            gone = [x[0] for x in con.execute("SELECT page_rowid FROM code_src WHERE NOT " + same.format(t="code_src", k="page_rowid"))]
            for pr in gone:
                con.execute("DELETE FROM code_blocks WHERE page_rowid = ?", (pr,))
                con.execute("DELETE FROM code_src WHERE page_rowid = ?", (pr,))
            added = 0
            cur = con.execute("SELECT rowid, url, title, library, code, fetched FROM pages WHERE rowid NOT IN "
                              "(SELECT page_rowid FROM code_src)")
            batch = cur.fetchall()
            for row in batch:
                for block in (row["code"] or "").split(CODE_SEP):
                    if not block.strip():
                        continue
                    lang, _, code = block.partition("\n")
                    con.execute("INSERT INTO code_blocks(page_rowid, url, title, library, lang, code) VALUES (?,?,?,?,?,?)",
                                (row["rowid"], row["url"], row["title"], row["library"], lang.strip(), code))
                    added += 1
                con.execute("INSERT OR REPLACE INTO code_src VALUES (?, ?, ?)", (row["rowid"], row["url"], row["fetched"]))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('derived_sig', ?)", (f"{DERIVED_VERSION}|{sig}",))
        return {"changed": True, "pages_added": len(batch), "pages_removed": len(gone), "code_blocks_added": added,
                "seconds": round(time.time() - t0, 2)}

    def warm(self) -> dict[str, Any]:
        """R18 at start: the first search reads only the derived lookup tables — read them once now (and, for the
        web cache, bring them up to date)."""
        if not self.read_only:
            return self.sync_derived()
        if not self.db_file.is_file():
            return {"index": "none yet"}
        t0 = time.time()
        with self._db() as con:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
            n = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("catalog", "code_blocks") if t in names}
        return {"read": n, "seconds": round(time.time() - t0, 2)}

    def _db(self):
        """Context manager: connection that commits and closes on exit (read-only: opened read-only)."""
        if self.read_only:
            sig = self._file_sig()
            if sig is None:
                raise InfosysError(self.missing_text())
            if sig != self._probed_sig:               # a build swapped a new index in (or the first one appeared)
                self._probe()
        return _Conn(self.db_file, read_only=self.read_only)

    def _file_sig(self) -> tuple[int, int, int] | None:
        try:
            st = self.db_file.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def missing_text(self) -> str:
        from . import infosys_build as B
        st = B.state(self.db_file)
        return f"no Infosys index yet ({self.db_file}): {st['message']}"

    def _probe(self) -> None:
        """Read-only: what the index file offers — nothing is created or changed."""
        self._probed_sig = self._file_sig()
        if self._probed_sig is None:
            self._fts, self.format = None, None
            return
        with _Conn(self.db_file, read_only=True) as con:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
            self._fts = "pages_fts" in names
            self._has_helpids = "helpids" in names
            r = con.execute("SELECT value FROM meta WHERE key = 'indexer_version'").fetchone() if "meta" in names else None
        self.format = int(r[0]) if r is not None and str(r[0]).isdigit() else None

    def _writable(self, what: str) -> None:
        if self.read_only:
            raise InfosysError(f"{what}: the index of the local Help Viewer package is built as a whole, outside the "
                               "tools that read it — kb_rebuild(index='infosys') (asks for approval), or "
                               "tools\\infosys_local.py index in the knowledge folder. Nothing was changed.")

    def _init_db(self) -> None:
        with self._db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS pages(
                    url TEXT PRIMARY KEY, book TEXT, page_id TEXT, lastmod TEXT, title TEXT, kind TEXT,
                    library TEXT, markdown TEXT, text TEXT, code TEXT, decl TEXT, fetched REAL);
                CREATE INDEX IF NOT EXISTS pages_book ON pages(book);
                CREATE INDEX IF NOT EXISTS pages_title ON pages(title COLLATE NOCASE);
                CREATE TABLE IF NOT EXISTS books(book TEXT PRIMARY KEY, indexed_at REAL, pages INTEGER);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            """)
            cols = {r["name"] for r in con.execute("PRAGMA table_info(pages)")}
            for c in ("version", "crumb"):          # added with the Help Viewer source
                if c not in cols:
                    con.execute(f"ALTER TABLE pages ADD COLUMN {c} TEXT")
            # external-content FTS: the index points at pages' own columns instead of keeping a second copy of every
            # page's text (≈ a third of the database). An older self-contained FTS table is replaced and rebuilt
            # from pages — no page has to be parsed again for that.
            try:
                row = con.execute("SELECT sql FROM sqlite_master WHERE name = 'pages_fts'").fetchone()
                if row is not None and "content='pages'" not in (row[0] or ""):
                    con.execute("DROP TABLE pages_fts")
                    row = None
                con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(url UNINDEXED, title, text, code, "
                            "content='pages', content_rowid='rowid', tokenize='unicode61')")
                self._fts = True
                if row is None:
                    con.execute("INSERT INTO pages_fts(pages_fts) VALUES ('rebuild')")
            except sqlite3.OperationalError:
                self._fts = False
            con.execute("CREATE TABLE IF NOT EXISTS helpids(help_id TEXT PRIMARY KEY, url TEXT)")
            self._has_helpids = True
            r = con.execute("SELECT value FROM meta WHERE key = 'indexer_version'").fetchone()
            have = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            if have and (r is None or r[0] != str(INDEXER_VERSION)):
                # a writable index is the web cache, or a .building file that infosys_build resumes (it never resumes
                # one of another format): rows built by other extraction logic are dropped once
                con.execute("DELETE FROM pages")
                con.execute("DELETE FROM books")
                con.execute("DELETE FROM meta WHERE key = 'source_stamp'")
                if self._fts:
                    con.execute("INSERT INTO pages_fts(pages_fts) VALUES ('delete-all')")
                _bump_gen(con)
            if r is None or r[0] != str(INDEXER_VERSION):
                con.execute("INSERT OR REPLACE INTO meta VALUES ('indexer_version', ?)", (str(INDEXER_VERSION),))
        self.format = INDEXER_VERSION

    @property
    def has_fts(self) -> bool:
        return bool(self._fts)

    # -- sitemap ------------------------------------------------------------------
    def sitemap(self, force: bool = False) -> dict[str, str]:
        """url -> lastmod for every English content page. Cached on disk for sitemap_ttl. With a local Help Viewer
        package: its page list, lastmod = the page's content identity (CRC-32 + size): only changed pages re-index."""
        with self._lock:
            if self._sitemap is not None and not force:
                return self._sitemap
            if self.local is not None:
                try:
                    self._sitemap = self.local.pages()
                except Exception as exc:  # noqa: BLE001 — HelpViewerError
                    raise InfosysError(str(exc)) from exc
                return self._sitemap
            cache = self.dir / "sitemap.json.gz"
            if cache.is_file() and not force and time.time() - cache.stat().st_mtime < self.sitemap_ttl:
                try:
                    with gzip.open(cache, "rt", encoding="utf-8") as fh:
                        self._sitemap = json.load(fh)
                    return self._sitemap
                except (OSError, ValueError):
                    pass
            urls: dict[str, str] = {}
            root = self.fetch.get(f"{INFOSYS}/sitemap.xml")
            for sm in _sitemap_locs(root):
                if f"/content/{LANG}/" not in sm:
                    continue
                for sub in _sitemap_locs(self.fetch.get(sm)):
                    if sub.endswith(".xml"):
                        urls.update(_sitemap_urlset(self.fetch.get(sub)))
                    else:
                        urls.setdefault(sub, "")
            if not urls:  # a flat urlset under /content/1033/sitemap.xml
                for sm in _sitemap_locs(root):
                    urls.update(_sitemap_urlset(self.fetch.get(sm)))
            if not urls:
                raise InfosysError("sitemap parsed to zero pages — infosys.beckhoff.com changed its sitemap layout")
            with gzip.open(cache, "wt", encoding="utf-8") as fh:
                json.dump(urls, fh)
            self._sitemap = urls
            return urls

    def book_allowed(self, book: str) -> bool:
        if self.tc3_only and is_tc2_book(book):
            return False
        return not any(r.search(book) for r in self.exclude)

    def books(self, pattern: str | None = None, category: str | None = None, limit: int = 500) -> dict[str, Any]:
        if category is not None and category not in BOOK_CATEGORIES:                       # R31
            raise InfosysError(f"category {category!r} unknown; one of {list(BOOK_CATEGORIES)}")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise InfosysError(f"limit {limit!r} must be ≥ 0")
        sm = self.sitemap()
        counts: dict[str, int] = {}
        for url in sm:
            b = book_of(url)
            if b:
                counts[b[0]] = counts.get(b[0], 0) + 1
        with self._db() as con:
            idx = {r["book"]: dict(r) for r in con.execute("SELECT book, indexed_at, pages FROM books")}
        rx = re.compile(_glob_to_re(pattern), re.I) if pattern else None
        items = []
        dropped = 0
        for book, n in sorted(counts.items()):
            if not self.book_allowed(book):
                dropped += 1
                continue
            cat = book_category(book)
            if rx and not rx.search(book):
                continue
            if category and cat != category:
                continue
            items.append({"book": book, "category": cat, "library": library_from_book(book), "pages": n,
                          "indexed_pages": (idx.get(book) or {}).get("pages", 0),
                          "indexed": book in idx, "url": f"{CONTENT_PREFIX}{book}/index.html"})
        return {"count": len(items), "excluded_tc2_or_rules": dropped, "total_pages": sum(counts.values()),
                "books": items[:limit]}

    # -- indexing -------------------------------------------------------------------
    def _store(self, con: sqlite3.Connection, url: str, lastmod: str, doc: HtmlDoc) -> dict[str, Any]:
        """One page → one pages row + one FTS row with the same rowid (D1: deleting by an UNINDEXED column was a full
        scan of the FTS table per page — quadratic over a 50k-page package)."""
        bk = book_of(url) or ("", "")
        title = doc.title or url
        decl = parse_declaration(title, doc)
        kind = decl["kind"]
        library = _library_from_tables(doc.tables) or library_from_book(bk[0])
        text = _text_of(doc.markdown)
        hid = (doc.meta.get("Microsoft.Help.Id") or "").strip().lower()
        if hid and self._has_helpids:
            # the page's Help Viewer id: ms-xhelp links to it are resolved once every page is in (resolve_help_links)
            con.execute("INSERT OR REPLACE INTO helpids VALUES (?, ?)", (hid, url))
        code = CODE_SEP.join(f"{lang}\n{c}" for lang, c in doc.code_blocks)
        has_decl = kind != "Page" and decl["declaration"] is not None
        version = _doc_version(doc.meta.get("Microsoft.Help.DisplayVersion"))
        old = con.execute("SELECT rowid, url, title, text, code FROM pages WHERE url = ?", (url,)).fetchone()
        if old is not None:
            if self._fts:   # external content: the index entry is removed with the values it was built from
                con.execute("INSERT INTO pages_fts(pages_fts, rowid, url, title, text, code) VALUES ('delete', ?, ?, ?, ?, ?)",
                            (old[0], old[1], old[2], old[3], old[4]))
            con.execute("DELETE FROM pages WHERE rowid = ?", (old[0],))
        cur = con.execute("INSERT INTO pages(url, book, page_id, lastmod, title, kind, library, markdown, text, code, "
                          "decl, fetched, version, crumb) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (url, bk[0], bk[1], lastmod, title, kind, library, doc.markdown, text, code,
                           json.dumps(decl) if has_decl or kind != "Page" else None, time.time(), version, doc.crumb or None))
        if self._fts:
            con.execute("INSERT INTO pages_fts(rowid, url, title, text, code) VALUES (?,?,?,?,?)",
                        (cur.lastrowid, url, title, text, code))
        return {"url": url, "title": title, "kind": kind, "library": library}

    def _fetch_doc(self, url: str) -> HtmlDoc:
        if self.local is None:
            return html_to_doc(self.fetch.get(url), base_url=url)
        from .helpviewer import body_html, page_meta
        try:
            raw = self.local.get(url)
        except Exception as exc:  # noqa: BLE001 — HelpViewerError
            raise InfosysError(str(exc)) from exc
        text, crumb = body_html(raw)
        doc = html_to_doc(text, base_url=url, package=True)
        doc.meta = page_meta(raw)
        doc.crumb = " ".join(crumb.translate(_INVISIBLE).split())
        if doc.meta.get("Title") and (not doc.title or doc.title.lower().startswith("beckhoff information system")):
            doc.title = " ".join(html.unescape(doc.meta["Title"]).translate(_INVISIBLE).split())
        return doc

    def _answer_meta(self) -> dict[str, Any]:
        """D3: what every answer carries — the date of the documentation it came from (a local package can be months
        old) and how much is indexed; the full book list and source block live in tc_doctor / infosys_books."""
        if self.local is not None:
            out = {"doc_date": self.local.describe()["modified"], "indexed_books_count": len(self.indexed_books())}
            note = self.index_note()
            if note:
                out["index_note"] = note          # rule 6: an answer from an index that is not the current one says so
            return out
        return {"indexed_books_count": len(self.indexed_books())}

    def source(self) -> dict[str, Any]:
        """Where the documentation comes from — every answer carries it (a local package can be months old)."""
        if self.local is not None:
            return self.local.describe()
        return {"kind": "web", "site": INFOSYS}

    def index_book(self, book: str, force: bool = False, max_pages: int | None = None,
                   progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
        """Fetch every page of a book that is new or whose sitemap lastmod changed. Serialised: a second
        caller for the same book waits and then finds the pages unchanged."""
        self._writable(f"indexing {book}")
        with self._index_lock:
            return self._index_book(book, force, max_pages, progress)

    def _index_book(self, book: str, force: bool, max_pages: int | None,
                    progress: Callable[[int, int], None] | None) -> dict[str, Any]:
        if max_pages is not None and (not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1):
            raise InfosysError(f"max_pages {max_pages!r} must be ≥ 1 (or omitted)")                # R31
        sm = self.sitemap()
        all_books = sorted({b[0] for u in sm if (b := book_of(u))})
        if book not in all_books:
            # R31: the library name the other tools accept ('Tc2_System' → tcplclib_tc2_system)
            by_lib = [b for b in all_books if (library_from_book(b) or "").lower() == book.lower() or b == book.lower()]
            if len(by_lib) == 1:
                book = by_lib[0]
        if not self.book_allowed(book):
            raise InfosysError(f"book {book!r} is excluded (TwinCAT 2 or docs.exclude_books)")
        pages = {u: lm for u, lm in sm.items() if (b := book_of(u)) and b[0] == book}
        if not pages:
            import difflib
            near = difflib.get_close_matches(book.lower(), all_books, n=6, cutoff=0.6)
            where = "local Help Viewer package" if self.local is not None else "Infosys sitemap"
            raise InfosysError(f"no book {book!r} in the {where}" + (f". Similar: {near}" if near else "")
                               + ". infosys_books(pattern=...) lists them")
        cap = (max_pages or len(pages)) if self.local is not None else min(max_pages or self.max_index_pages, self.max_index_pages)
        with self._db() as con:
            have = {r["url"]: r["lastmod"] for r in con.execute("SELECT url, lastmod FROM pages WHERE book = ?", (book,))}
        todo = [u for u, lm in pages.items() if force or u not in have or have[u] != lm]
        skipped = len(pages) - len(todo)
        todo = todo[:cap]
        done, failed = 0, []
        kinds: dict[str, int] = {}
        t0 = time.time()
        # local package: parsing is CPU-bound (one thread is as fast as many); the website: polite parallel fetches
        with ThreadPoolExecutor(max_workers=1 if self.local is not None else self.max_workers) as ex, self._db() as con:
            futs = {ex.submit(self._fetch_doc, u): u for u in todo}
            for fut in as_completed(futs):
                u = futs[fut]
                try:
                    doc = fut.result()
                except Exception as exc:  # noqa: BLE001
                    failed.append({"url": u, "error": str(exc)[:200]})
                    continue
                with self._lock:
                    info = self._store(con, u, pages[u], doc)
                kinds[info["kind"]] = kinds.get(info["kind"], 0) + 1
                done += 1
                if done % (2000 if self.local is not None else 25) == 0:
                    con.commit()
                if progress:
                    progress(done, len(todo))
            con.execute("INSERT OR REPLACE INTO books VALUES (?,?,?)",
                        (book, time.time(), con.execute("SELECT COUNT(*) FROM pages WHERE book=?", (book,)).fetchone()[0]))
            if done:
                _bump_gen(con)
        return {"book": book, "library": library_from_book(book), "pages_in_sitemap": len(pages), "fetched": done,
                "unchanged": skipped, "capped": len(pages) - skipped - len(todo) if len(todo) == cap else 0,
                "failed": failed[:20], "kinds": kinds, "seconds": round(time.time() - t0, 1), "fts": self.has_fts}

    def index_books(self, pattern: str, force: bool = False, max_pages: int | None = None) -> dict[str, Any]:
        rx = re.compile(_glob_to_re(pattern), re.I)
        sm = self.sitemap()
        books = sorted({b[0] for u in sm if (b := book_of(u)) and rx.search(b[0]) and self.book_allowed(b[0])})
        if not books:
            raise InfosysError(f"no TwinCAT 3 book matches {pattern!r}; infosys_books lists them")
        budget = max_pages or self.max_index_pages
        results = []
        for bk in books:
            if budget <= 0:
                results.append({"book": bk, "skipped": "page budget exhausted; call again"})
                continue
            r = self.index_book(bk, force=force, max_pages=budget)
            budget -= r["fetched"]
            results.append(r)
        return {"books": results, "remaining_budget": budget}

    def index_all(self, progress: Callable[[str, int, int], None] | None = None,
                  page_progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
        """Local package only: index every TwinCAT 3 book (unchanged pages are skipped, so a repeat is cheap).
        ``page_progress(book, pages_done, pages_todo)`` reports inside a book (D25: a large book looked stalled)."""
        if self.local is None:
            raise InfosysError("index_all needs the local Help Viewer package (docs.source = 'local')")
        self._writable("indexing the package")
        sm = self.sitemap()
        books = sorted({b[0] for u in sm if (b := book_of(u)) and self.book_allowed(b[0])})
        t0 = time.time()
        fetched = failed = 0
        book_errors: list[dict[str, str]] = []
        for i, bk in enumerate(books):
            if progress:
                progress(bk, i, len(books))
            try:
                r = self.index_book(bk, progress=(lambda d, t, _b=bk: page_progress(_b, d, t)) if page_progress else None)
            except Exception as exc:  # noqa: BLE001 — D25: one bad book must not end the run; it is reported
                book_errors.append({"book": bk, "error": f"{type(exc).__name__}: {exc}"[:300]})
                continue
            fetched += r["fetched"]
            failed += len(r["failed"])
        if progress:
            progress("", len(books), len(books))
        with self._db() as con:
            # pages of books that are no longer in the package (or no longer allowed) leave the index
            keep = set(books)
            gone = [r[0] for r in con.execute("SELECT DISTINCT book FROM pages") if r[0] not in keep]
            if gone:
                _bump_gen(con)
            for bk in gone:
                if self._fts:
                    con.execute("INSERT INTO pages_fts(pages_fts, rowid, url, title, text, code) "
                                "SELECT 'delete', rowid, url, title, text, code FROM pages WHERE book = ?", (bk,))
                con.execute("DELETE FROM pages WHERE book = ?", (bk,))
                con.execute("DELETE FROM books WHERE book = ?", (bk,))
            if self._fts and fetched:
                con.execute("INSERT INTO pages_fts(pages_fts) VALUES ('optimize')")
            if not book_errors:          # complete only when every book went in; otherwise the next start resumes
                con.execute("INSERT OR REPLACE INTO meta VALUES ('source_stamp', ?)", (self.local.content_stamp(),))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('indexer_version', ?)", (str(INDEXER_VERSION),))
        try:
            self.sync_derived()                 # the lookup tables follow the pages right away
        except sqlite3.Error:
            pass
        return {"books": len(books), "pages_indexed": fetched, "failed": failed, "book_errors": book_errors[:20],
                "seconds": round(time.time() - t0, 1), "removed_books": gone, "source": self.source()}

    def index_complete(self) -> bool:
        """The whole package is in the index, built by this indexer version."""
        if self.local is None:
            return False
        try:
            return self.indexed_stamp() == self.local.content_stamp() and self._meta("indexer_version") == str(INDEXER_VERSION)
        except Exception:  # noqa: BLE001
            return False

    def index_note(self) -> str | None:
        """Rule 6: what every answer from a read-only index says when that index is not the current one — an older
        format, another package — or not there yet. None when it is current."""
        if not self.read_only:
            return None
        from . import infosys_build as B
        try:
            st = B.state(self.db_file, local=self.local)
        except Exception as exc:  # noqa: BLE001
            return f"index state not readable: {exc}"
        return None if st["state"] == "complete" and not st.get("rebuild") else st["message"]

    def _meta(self, key: str) -> str | None:
        with self._db() as con:
            r = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def indexed_stamp(self) -> str | None:
        with self._db() as con:
            r = con.execute("SELECT value FROM meta WHERE key = 'source_stamp'").fetchone()
        return r["value"] if r else None

    def indexed_books(self) -> list[str]:
        with self._db() as con:
            return [r["book"] for r in con.execute("SELECT book FROM books ORDER BY book")]

    def ensure_defaults(self) -> list[dict[str, Any]]:
        """Index the configured default books if nothing is indexed yet. Returns what it did."""
        if self.indexed_books() or not self.default_books:
            return []
        out = []
        for b in self.default_books:
            try:
                out.append(self.index_book(b))
            except InfosysError as exc:
                out.append({"book": b, "error": str(exc)})
        return out

    # -- queries -------------------------------------------------------------------
    def search_symbols(self, query: str, kind: str | None = None, library: str | None = None, limit: int = 10,
                       auto_index: bool = True) -> dict[str, Any]:
        q = query.strip()
        if not q:
            raise InfosysError("empty query")
        if kind and kind not in KINDS:
            raise InfosysError(f"kind must be one of {KINDS}")
        did = self.ensure_defaults() if auto_index else []
        where = ["kind != 'Page'"]
        params: list[Any] = []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if library:
            where.append("(library = ? COLLATE NOCASE OR book = ? COLLATE NOCASE)")
            params += [library, library]
        _check_limit(limit)
        where.append("(title LIKE ? ESCAPE '\\' COLLATE NOCASE)")
        params.append(f"%{_like(q)}%")
        # R18: the catalog (a few MB) instead of the page store (hundreds of MB, cold: 16.9 s); markdown only for the hits
        self.sync_derived()
        with self._db() as con:
            rows = con.execute(f"SELECT url, title, kind, library, book FROM catalog WHERE {' AND '.join(where)}",
                               params).fetchall()
            ql = q.lower()

            def rank(r):
                t = r["title"].lower()
                return (0 if t == ql else 1 if t.startswith(ql) else 2 if re.search(rf"\b{re.escape(ql)}\b", t) else 3, len(t), t)

            # R28: a 'symbol' page whose title is a sentence ('Use of function blocks', 'Example: …') is a help topic
            rows = [r for r in rows if _SYMBOL_TITLE.match(r["title"] or "")]
            rows.sort(key=rank)
            top = rows[:limit]
            md = {r["url"]: r["markdown"] for r in con.execute(
                f"SELECT url, markdown FROM pages WHERE url IN ({','.join('?' * len(top))})", [r["url"] for r in top])} if top else {}
        items = [{"name": _SYMBOL_TITLE.match(r["title"]).group(1), "kind": r["kind"], "library": r["library"],
                  "book": r["book"], "url": r["url"], "summary": _summary(md.get(r["url"]) or "")} for r in top]
        res = {"query": q, "count": len(rows), "items": items, **self._answer_meta()}
        if did:
            res["auto_indexed"] = did
        if not rows:
            res["hint"] = ("no indexed symbol matches. infosys_books(pattern='tcplclib_*') lists libraries; "
                           + ("infosys_search(...) searches the page text." if self.local is not None else
                              "infosys_index(book=...) adds one; infosys_search(...) also tries the web fallback."))
        return res

    def search(self, query: str, library: str | None = None, limit: int = 10, web_fallback: bool | None = None) -> dict[str, Any]:
        q = query.strip()
        if not q:
            raise InfosysError("empty query")
        _check_limit(limit)
        items = self._fts_search(q, library, limit) if self._fts else self._like_search(q, library, limit)
        backend = "local-fts5" if self._fts else "local-like"
        if not items and self.local is None and (self.web_fallback if web_fallback is None else web_fallback):
            items = self._ddg(q, limit)
            backend = "duckduckgo+sitemap"
        if self.local is not None:
            backend += "+helpviewer"
        res = {"query": q, "backend": backend, "count": len(items), "items": items, **self._answer_meta()}
        if web_fallback and self.local is not None:
            res["note"] = "web_fallback is ignored with the local Help Viewer package (no network lookups)"   # R30
        if not items and library:
            with self._db() as con:
                known = con.execute("SELECT 1 FROM catalog WHERE library = ? COLLATE NOCASE OR book = ? COLLATE NOCASE "
                                    "LIMIT 1", (library, library)).fetchone() if self._has_catalog(con) else True
            if not known:
                res["hint"] = f"no indexed page belongs to library {library!r} — infosys_books lists the books"  # R32
        return res

    @staticmethod
    def _has_catalog(con: Any) -> bool:
        return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'catalog'").fetchone() is not None

    def search_code(self, query: str, language: str | None = None, limit: int = 10,
                    ignore_spacing: bool = False) -> dict[str, Any]:
        """Code samples containing ``query`` (case-insensitive substring of the code as written; non-breaking spaces
        count as spaces, R17). language filters in SQL before the limit (R19); '' / 'none' = unlabelled samples (R22).
        ignore_spacing=true also matches when only the spacing around punctuation differs ('fbRead(READ:=FALSE)')."""
        q = (query or "").replace("\xa0", " ").strip()
        if not q:
            raise InfosysError("empty query")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:      # R21
            raise InfosysError(f"limit {limit!r} must be 1..50")
        self.sync_derived()
        langs = self.code_languages()
        lang = None if language is None else language.strip()
        if lang is not None:
            low = lang.lower()
            if low in ("none", "unlabelled", "unlabeled"):
                lang = ""
            elif lang and lang.lower() not in {x.lower() for x in langs if x}:
                raise InfosysError(f"language {language!r} does not occur in the index; languages: "
                                   f"{sorted(x for x in langs if x)} ('' or 'none' = samples without a label: "
                                   f"{langs.get('', 0)})")                                        # R22
        if ignore_spacing:
            parts = re.findall(r"\w+|\S", q)
            rx = re.compile(r"\s*".join(re.escape(x) for x in parts), re.I)
            words = re.findall(r"\w{3,}", q)
            needle = max(words, key=len) if words else None
        else:
            rx = re.compile(re.escape(q), re.I)
            needle = q
        where, params = [], []
        if lang is not None:
            where.append("b.lang = ? COLLATE NOCASE")
            params.append(lang)
        with self._db() as con:
            # one pass over the code samples only (not the page store); the language filter is part of the query
            like = "%" + (needle or q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql = ("SELECT b.url, b.title, b.library, b.lang, b.code FROM code_blocks b WHERE b.code LIKE ? ESCAPE '\\'"
                   + "".join(f" AND {w}" for w in where))
            rows = con.execute(sql, [like] + params)
            out: list[dict[str, Any]] = []
            total = 0
            for r in rows:
                if not rx.search(r["code"]):
                    continue
                total += 1
                if len(out) < limit:
                    lines = r["code"].splitlines()
                    hit = next((n for n, ln in enumerate(lines) if rx.search(ln)), 0)
                    lo, hi = max(0, hit - 6), min(len(lines), hit + 10)
                    out.append({"url": r["url"], "title": r["title"], "library": r["library"],
                                "language": r["lang"] or None, "line": hit + 1, "snippet": "\n".join(lines[lo:hi])})
                if total >= 2000:
                    break
        res = {"query": q, "count": len(out), "total_matches": total if total < 2000 else "2000+", "items": out,
               **self._answer_meta()}
        if language is not None:
            res["language"] = lang or "(unlabelled)"
        if not out:
            complete = False
            try:
                complete = self.local is not None and self.index_complete()
            except Exception:  # noqa: BLE001
                complete = False
            res["hint"] = ((f"no code sample in the complete index contains {q!r}"                    # R20
                            + ("" if ignore_spacing else " — ignore_spacing=true also matches other spacing"))
                           if complete else "code search covers indexed books only — infosys_index(book=...) first, "
                           "or wait for the background index (tc_doctor → infosys)")
        return res

    def code_languages(self) -> dict[str, int]:
        with self._db() as con:
            return {r[0]: r[1] for r in con.execute("SELECT lang, COUNT(*) FROM code_blocks GROUP BY lang")}

    def get_page(self, ref: str, refresh: bool = False, max_chars: int = 60_000) -> dict[str, Any]:
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 1:          # R31
            raise InfosysError(f"max_chars {max_chars!r} must be ≥ 1")
        url = self.resolve(ref)
        try:
            sm = self.sitemap()
        except InfosysError:
            sm = {}   # the page itself may still be reachable; sitemap only drives cache invalidation
        lastmod = sm.get(url, "")
        row: Any = None
        if not (self.read_only and not self.db_file.is_file()):
            with self._db() as con:
                row = con.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        if self.local is not None and url not in sm:
            raise InfosysError(f"{url} is not in the local Help Viewer package ({self.local.path.name}, "
                               f"{self.local.describe()['modified']}) — no web lookup in local mode")
        indexed = True
        if self.read_only:
            if row is None or refresh:
                # a page the index does not hold (a book the build leaves out, or no index yet): converted from the
                # package as it is, the same way the build converts it — not stored (the index is built as a whole)
                doc = self._fetch_doc(url)
                bk = book_of(url) or ("", "")
                row = {"title": doc.title or url, "kind": parse_declaration(doc.title or url, doc)["kind"],
                       "library": _library_from_tables(doc.tables) or library_from_book(bk[0]), "book": bk[0],
                       "lastmod": "", "markdown": self._links_to_urls(doc.markdown),
                       "version": _doc_version(doc.meta.get("Microsoft.Help.DisplayVersion")), "crumb": doc.crumb or None}
                indexed = False
        elif row is None or refresh or (lastmod and row["lastmod"] != lastmod):
            doc = self._fetch_doc(url)
            with self._db() as con, self._lock:
                self._store(con, url, lastmod, doc)
                _bump_gen(con)
                row = con.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        md = row["markdown"] or ""
        out = {"url": url, "title": row["title"], "kind": row["kind"], "library": row["library"], "book": row["book"],
               "lastmod": row["lastmod"] or None, "in_sitemap": url in sm,
               "markdown": md[:max_chars], "truncated": len(md) > max_chars, "chars": len(md)}
        if not indexed:
            out["indexed"] = False
        if self.local is not None:
            out["lastmod"] = None
            out["version"] = row["version"]
            out["breadcrumb"] = row["crumb"]
            out.update(self._answer_meta())
        return out

    def declaration(self, symbol: str, library: str | None = None) -> dict[str, Any]:
        s = symbol.strip()
        with self._db() as con:
            where = "title = ? COLLATE NOCASE AND kind != 'Page'"
            params: list[Any] = [s]
            if library:
                where += " AND (library = ? COLLATE NOCASE OR book = ? COLLATE NOCASE)"
                params += [library, library]
            rows = con.execute(f"SELECT url, title, kind, library, decl FROM pages WHERE {where} ORDER BY book", params).fetchall()
        row = rows[0] if rows else None
        if row is None:
            found = self.search_symbols(s, library=library, limit=5)
            exact = [it for it in found["items"] if it["name"].lower() == s.lower()]
            if not exact:
                complete = self.local is not None and self.index_complete()
                raise InfosysError(f"no symbol page titled {s!r}" + (f" in {library}" if library else "") +
                                   (f"; near: {[it['name'] for it in found['items']]}" if found["items"] else
                                    (" in the complete index" if complete else
                                     "; index the library first (infosys_index) or check infosys_books")))
            with self._db() as con:
                row = con.execute("SELECT url, title, kind, library, decl FROM pages WHERE url = ?", (exact[0]["url"],)).fetchone()
            rows = [row]
        libs = sorted({r["library"] for r in rows if r["library"]})
        if len(libs) > 1 and not library:
            # R23 (0.8.8 retest): MC_MoveAbsolute exists in Tc2_MC2 and Tc3_DriveMotionControl with different
            # parameters — returning the first one let code be written against the wrong FB. Nothing is chosen.
            cands = [f"{r['library']} ({r['url']})" for r in rows if r["library"]]
            raise InfosysError(f"ambiguous: {s} is documented in {len(libs)} libraries: {cands} — nothing was chosen. "
                               "Pass library= with the one the project uses (proj_libraries lists them) and say which "
                               "one you used.")
        decl = json.loads(row["decl"]) if row["decl"] else {"name": row["title"], "kind": row["kind"], "declaration": None}
        decl.update({"url": row["url"], "library": row["library"]})
        with self._db() as con:
            same = con.execute("SELECT DISTINCT library FROM pages WHERE title = ? COLLATE NOCASE AND kind != 'Page' "
                               "AND library IS NOT NULL AND library != ?", (s, row["library"] or "")).fetchall()
        others = sorted(r["library"] for r in same if r["library"])
        if others:
            decl["also_in"] = others              # the other libraries with a symbol of this name
        if self.local is not None:
            with self._db() as con:
                v = con.execute("SELECT version FROM pages WHERE url = ?", (row["url"],)).fetchone()
            if v and v["version"]:
                decl["doc_version"] = v["version"]
            decl.update(self._answer_meta())
        return decl

    # -- ms-xhelp links (R26) ------------------------------------------------------
    # Help Viewer pages link to each other as ms-xhelp:///?Id=<Microsoft.Help.Id>. Every page's id goes into helpids
    # while it is stored; once all pages are in, resolve_help_links writes the page URLs into the stored text (rule 4).
    def resolve_help_links(self) -> dict[str, Any]:
        """ms-xhelp links → page URLs in the stored Markdown, text and declarations — once, at the end of a build.
        A link to an id the package does not have stays as it is. -> counts."""
        self._writable("resolving links")
        t0 = time.time()
        found = resolved = pages = 0
        with self._db() as con:
            ids = {r[0]: r[1] for r in con.execute("SELECT help_id, url FROM helpids")}

            def sub(m: re.Match) -> str:
                nonlocal found, resolved
                found += 1
                u = ids.get(urllib.parse.unquote(m.group(1)).strip().lower())
                if u is None:
                    return m.group(0)
                resolved += 1
                return u
            rows = con.execute("SELECT rowid, url, title, markdown, text, code, decl FROM pages WHERE "
                               "markdown LIKE '%ms-xhelp%' OR decl LIKE '%ms-xhelp%'").fetchall()
            for r in rows:
                md = _XHELP.sub(sub, r["markdown"] or "")
                decl = _XHELP.sub(sub, r["decl"]) if r["decl"] else r["decl"]
                if md == (r["markdown"] or "") and decl == r["decl"]:
                    continue
                text = _text_of(md)
                if self._fts:
                    con.execute("INSERT INTO pages_fts(pages_fts, rowid, url, title, text, code) VALUES "
                                "('delete', ?, ?, ?, ?, ?)", (r["rowid"], r["url"], r["title"], r["text"], r["code"]))
                con.execute("UPDATE pages SET markdown = ?, text = ?, decl = ? WHERE rowid = ?",
                            (md, text, decl, r["rowid"]))
                if self._fts:
                    con.execute("INSERT INTO pages_fts(rowid, url, title, text, code) VALUES (?,?,?,?,?)",
                                (r["rowid"], r["url"], r["title"], text, r["code"]))
                pages += 1
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            [("links", str(found)), ("links_resolved", str(resolved))])
        return {"pages": pages, "links": found, "resolved": resolved, "help_ids": len(ids),
                "seconds": round(time.time() - t0, 1)}

    def _xhelp_url(self, hid: str) -> str | None:
        hid = urllib.parse.unquote(hid).strip().lower()
        if not self.db_file.is_file():
            return None
        with self._db() as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'helpids'").fetchone() is None:
                return None
            r = con.execute("SELECT url FROM helpids WHERE help_id = ?", (hid,)).fetchone()
        return r[0] if r else None

    def _links_to_urls(self, md: str) -> str:
        """ms-xhelp links of a page converted now (one the index does not hold), through the index's help ids."""
        if "ms-xhelp" not in md or self.local is None:
            return md
        cache: dict[str, str | None] = {}

        def sub(m: re.Match) -> str:
            hid = m.group(1)
            if hid not in cache:
                cache[hid] = self._xhelp_url(hid)
            return cache[hid] or m.group(0)
        return _XHELP.sub(sub, md)

    # -- resolution ----------------------------------------------------------------
    def resolve(self, ref: str) -> str:
        r = ref.strip()
        if r.lower().startswith("ms-xhelp:"):
            m = _XHELP.search(r)
            u = self._xhelp_url(m.group(1)) if m else None
            if u is None:
                raise InfosysError(f"{ref!r}: no page with that Help Viewer Id in the local package")
            return u
        if r.startswith("http://") or r.startswith("https://"):
            u = r.split("#", 1)[0]
            u = re.sub(r"\?id=\d+$", "", u)
            u = u.replace("http://", "https://").replace("infosys.beckhoff.de", "infosys.beckhoff.com")
            host = urllib.parse.urlparse(u).hostname or ""
            if host not in ALLOWED_HOSTS:
                raise InfosysError(f"only infosys.beckhoff.com pages can be fetched, not {host!r}")
            return u
        if r.startswith("content/"):
            return f"{INFOSYS}/{r}"
        m = re.fullmatch(r"([a-z0-9_]+)/(\d+)(?:\.html)?", r, re.I)
        if m:
            return f"{CONTENT_PREFIX}{m.group(1)}/{m.group(2)}.html"
        if re.fullmatch(r"\d{6,}", r):
            hits = [u for u in self.sitemap() if u.endswith(f"/{r}.html")]
            if hits:
                return hits[0]
            raise InfosysError(f"no page id {r} in the " + ("local Help Viewer package" if self.local is not None
                                                             else "Infosys sitemap"))
        # a symbol name
        with self._db() as con:
            row = con.execute("SELECT url FROM pages WHERE title = ? COLLATE NOCASE AND kind != 'Page' ORDER BY book",
                              (r,)).fetchone()
        if row:
            return row["url"]
        found = self.search_symbols(r, limit=3)
        exact = [it for it in found["items"] if it["name"].lower() == r.lower()]
        if exact:
            return exact[0]["url"]
        raise InfosysError(f"{ref!r} is neither an Infosys URL, 'book/id', a page id nor an indexed symbol name"
                           + (f"; near: {[it['name'] for it in found['items']]}" if found["items"] else ""))

    # -- internals -------------------------------------------------------------------
    def _fts_search(self, q: str, library: str | None, limit: int) -> list[dict[str, Any]]:
        # R29: a quoted phrase stays one FTS phrase ("ADS error code" 1861 matched pages with 'error code' alone)
        terms: list[str] = []
        for phrase, bare in re.findall(r'"([^"]*)"|([^\s"]+)', q):
            words = re.findall(r"[\w\.]+", phrase if phrase else bare)
            if words:
                terms.append('"' + " ".join(w.replace('"', '""') for w in words) + '"')
        toks = terms
        if not toks:
            return []
        for joiner in (" AND ", " OR "):
            match = joiner.join(toks)
            sql = (f"SELECT p.url, p.title, p.kind, p.library, p.book, p.version, "
                   f"snippet(pages_fts, 2, '[', ']', '…', 24) AS snip, bm25(pages_fts, 0.0, 10.0, 1.0, 0.5) AS score "
                   f"FROM pages_fts JOIN pages p ON p.rowid = pages_fts.rowid WHERE pages_fts MATCH ?")
            params: list[Any] = [match]
            if library:
                sql += " AND (p.library = ? COLLATE NOCASE OR p.book = ? COLLATE NOCASE)"
                params += [library, library]
            sql += " ORDER BY score LIMIT ?"
            params.append(limit)
            try:
                with self._db() as con:
                    rows = con.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                raise InfosysError(f"search syntax: {exc}") from exc
            if rows:
                return [{"url": r["url"], "title": r["title"], "kind": r["kind"], "library": r["library"], "book": r["book"],
                         **({"version": r["version"]} if r["version"] else {}),
                         "excerpt": r["snip"], "score": round(-r["score"], 3)} for r in rows]
        return []

    def _like_search(self, q: str, library: str | None, limit: int) -> list[dict[str, Any]]:
        toks = [t.lower() for t in re.findall(r"[\w\.]+", q)]
        if not toks:
            return []
        sql = "SELECT url, title, kind, library, book, text FROM pages WHERE " + " AND ".join("text LIKE ? COLLATE NOCASE" for _ in toks)
        params: list[Any] = [f"%{t}%" for t in toks]
        if library:
            sql += " AND (library = ? COLLATE NOCASE OR book = ? COLLATE NOCASE)"
            params += [library, library]
        with self._db() as con:
            rows = con.execute(sql + " LIMIT ?", params + [limit * 4]).fetchall()
        out = []
        for r in rows:
            t = r["text"] or ""
            pos = min((t.lower().find(tok) for tok in toks if tok in t.lower()), default=0)
            title_hit = sum(tok in r["title"].lower() for tok in toks)
            out.append(({"url": r["url"], "title": r["title"], "kind": r["kind"], "library": r["library"], "book": r["book"],
                         "excerpt": t[max(0, pos - 100):pos + 160].strip()}, -title_hit))
        out.sort(key=lambda x: x[1])
        return [o for o, _ in out[:limit]]

    def _ddg(self, q: str, limit: int) -> list[dict[str, Any]]:
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(f"site:infosys.beckhoff.com {q}")
        try:
            raw = self.fetch.get(url, allow_hosts=DDG_HOSTS).decode("utf-8", errors="replace")
        except InfosysError as exc:
            return [{"error": f"web fallback unavailable: {exc}"}]
        sm = self.sitemap()
        seen: list[str] = []
        for href in re.findall(r'href="([^"]+)"', raw):
            href = html.unescape(href)
            if "uddg=" in href:
                href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [""])[0])
            if "infosys.beckhoff.com/content/" not in href:
                continue
            href = href.replace("http://", "https://").split("#", 1)[0]
            href = re.sub(r"\?.*$", "", href)
            if f"/content/{LANG}/" not in href:
                continue
            b = book_of(href)
            if not b or not self.book_allowed(b[0]):
                continue
            if href in sm and href not in seen:
                seen.append(href)
            if len(seen) >= limit:
                break
        items = []
        for u in seen:
            try:
                page = self.get_page(u, max_chars=400)
                items.append({"url": u, "title": page["title"], "kind": page["kind"], "library": page["library"],
                              "book": page["book"], "excerpt": _summary(page["markdown"], 240)})
            except InfosysError as exc:
                items.append({"url": u, "error": str(exc)[:160]})
        return items


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def ro_uri(db: Path) -> str:
    """SQLite read-only URI: 'file:/C:/x.db?mode=ro' ('file:////server/share/x.db' for UNC paths, which Path.as_uri
    turns into a host part SQLite refuses)."""
    s = str(Path(db).resolve()).replace("\\", "/")
    s = "//" + s if s.startswith("//") else (s if s.startswith("/") else "/" + s)
    return "file:" + urllib.parse.quote(s, safe="/:") + "?mode=ro"


class _Conn:
    def __init__(self, path: Path, read_only: bool = False):
        self.path, self.read_only = path, read_only
        self.con: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        if self.read_only:
            self.con = sqlite3.connect(ro_uri(self.path), uri=True, timeout=30, check_same_thread=False)
            self.con.row_factory = sqlite3.Row
            return self.con
        self.con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            # WAL: a background index (writer) never blocks a search (reader) and vice versa
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        return self.con

    def __exit__(self, et, ev, tb):
        assert self.con is not None
        try:
            if et is None:
                self.con.commit()
            else:
                self.con.rollback()
        finally:
            self.con.close()
        return False


def _sitemap_locs(data: bytes) -> list[str]:
    out = []
    try:
        for _ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
            if el.tag.rsplit("}", 1)[-1] == "loc" and el.text:
                out.append(el.text.strip())
    except ET.ParseError as exc:
        raise InfosysError(f"sitemap XML parse error: {exc}") from exc
    return out


def _sitemap_urlset(data: bytes) -> dict[str, str]:
    urls: dict[str, str] = {}
    loc = lastmod = None
    try:
        for _ev, el in ET.iterparse(io.BytesIO(data), events=("end",)):
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "loc":
                loc = (el.text or "").strip()
            elif tag == "lastmod":
                lastmod = (el.text or "").strip()
            elif tag == "url":
                if loc:
                    urls[loc] = lastmod or ""
                loc = lastmod = None
                el.clear()
    except ET.ParseError as exc:
        raise InfosysError(f"sitemap XML parse error: {exc}") from exc
    return urls


def _glob_to_re(pattern: str) -> str:
    return "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$" if any(c in pattern for c in "*?") \
        else re.escape(pattern)


def _summary(markdown: str | None, n: int = 220) -> str:
    if not markdown:
        return ""
    for block in markdown.split("\n\n"):
        b = block.strip()
        if not b or b.startswith("#") or b.startswith("```") or b.startswith("|") or b.startswith("!["):
            continue
        b = " ".join(b.split())
        return b if len(b) <= n else b[: n - 1].rsplit(" ", 1)[0] + "…"
    return ""
