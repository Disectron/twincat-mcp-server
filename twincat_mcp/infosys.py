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
import fnmatch
import json
import os
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

from .docsearch import title_words

# Bump when the code that turns a page into stored rows changes (html_to_doc / _MdWalker, parse_declaration, classify,
# _store, the table layout). An index built by another INDEXER_VERSION is rebuilt once; otherwise nothing is
# re-parsed unless the page's content changed. tests/test_infosys_local.py fingerprints that code and fails when it
# changes without a bump.
INDEXER_VERSION = 7          # 3: code without non-breaking spaces; the return value parsed into the header (R24);
                             #    Methods / Properties tables as their own lists (N74)
                             # 4: everything a reader used to repair is stored repaired (docs/KNOWLEDGE_BASE.md rule 4):
                             #    no invisible marks, image placeholders or empty links; links resolved to page URLs
                             #    (relative ones while parsing, ms-xhelp ones after the build); table cells plain text
                             #    for the declaration (N77: a type is its name, its page in type_ref) and type names
                             #    split in narrow columns joined (R32); enum values numbered, comments as descriptions
                             #    (R25), no repeated description dict
                             # 5: a page's kind and return type from the evidence for its own symbol (N103: the header
                             #    as a heading 'FUNCTION MEMCPY : UDINT' / 'MEMSET FUNCTION: UDINT', the 'Return value'
                             #    table); IEC headers in capitals only; a sentence title documents no symbol (N101:
                             #    'Attribute 'linkalways'' is a Page); the declaration's name is the symbol
                             # 6: each page's place in the Help Viewer contents (Microsoft.Help.TOCParent → parent URL,
                             #    N126: an FB's method pages titled 'Call' could not be tied to their FB); a page that is
                             #    not UTF-8 decoded in its own charset (N138: Windows-1252 pages kept U+FFFD)
                             # 7: a page's kind from its own evidence, never from its samples (0.8.14 retest N150 N151
                             #    N157 N172: an FB whose sample declares a STRUCT is an FB, an operator page an Operator,
                             #    a method page a Method; a function's 'returns TRUE/FALSE' is its BOOL return; the
                             #    header in a page's Syntax section, 'FUNCTION BLOCK X' read as a header; an operator's
                             #    syntax only under its label); the symbols documented inside pages (N170, `symbols`)

INFOSYS = "https://infosys.beckhoff.com"
LANG = "1033"
CONTENT_PREFIX = f"{INFOSYS}/content/{LANG}/"
ALLOWED_HOSTS = {"infosys.beckhoff.com"}
DDG_HOSTS = {"html.duckduckgo.com", "duckduckgo.com"}
USER_AGENT = "twincat-mcp/0.6 (+https://github.com/; documentation lookup for an engineering tool)"
CODE_SEP = "\n\x1e\n"
DERIVED_VERSION = 3          # catalog / code_blocks / symbols layout and rules; a bump rebuilds them from the stored pages
                             # (3: the names pages declare under another title, declared_symbols)
                             # (no re-parse). 2: symbols — the methods and constants documented inside pages (N170)


BOOK_CATEGORIES = ("plc-library", "function-tf", "engineering-te", "twincat-system", "hardware", "other")
_SYMBOL_TITLE = re.compile(r"^(?:(?:FUNCTION_BLOCK|FUNCTION|PROGRAM|METHOD|PROPERTY|TYPE|INTERFACE|ACTION)\s+)?"
                           r"([A-Za-z_][\w.]*)\s*(?:\(\s*\))?$", re.I)


def _like(s: str) -> str:
    """A LIKE pattern matching ``s`` literally (R27: '%' matched everything, 'ADS_EAD' matched ADSREAD)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


SEARCH_LIMIT = 50             # hits per page of a search (N167: a larger limit is used as this, offset pages on)


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
         "Union", "Alias", "Interface", "GlobalVariable", "Library", "Operator", "Constant", "Page")

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
    if isinstance(raw, bytes):
        from .helpviewer import decode_page
        text = decode_page(raw)
    else:
        text = raw
    w = _MdWalker(base_url, package)
    w.feed(text)
    w.close()
    return w.result()


# ---------------------------------------------------------------------------
# IEC declaration extraction
# ---------------------------------------------------------------------------
# N103 / N101: an IEC header is written in capitals (Beckhoff's code, whatever language label the page gives it —
# 'METHOD Release : HRESULT' is marked cpp); a case-insensitive match read C# 'Type type' as TYPE and JavaScript
# 'function' as FUNCTION
# review of 0.8.15: 44 pages write the header 'FUNCTION BLOCK FB_OCPP1_Client' (a space) — read as FUNCTION 'BLOCK',
# their header was skipped (the declaration made of fragments) and a Syntax-section rule would have made them Functions
_HEAD_RE = re.compile(
    r"^\s*(?:\{[^}]*\}\s*)*(FUNCTION_BLOCK|FUNCTION BLOCK|FUNCTION|PROGRAM|METHOD|PROPERTY|INTERFACE|TYPE|VAR_GLOBAL|ACTION)\b\s*"
    r"(?:(?:ABSTRACT|FINAL|INTERNAL|PUBLIC|PRIVATE|PROTECTED)\s+)*([A-Za-z_][\w]*)?(.*)$", re.M)
_POU_KW = r"FUNCTION_BLOCK|FUNCTION|PROGRAM|METHOD|PROPERTY|INTERFACE|ACTION"
# the header as a heading or a title: 'FUNCTION MEMCPY : UDINT', 'FUNCTION DCTIME_TO_DCTIME64: T_DCTIME64' …
_HEAD_TEXT = re.compile(rf"^({_POU_KW})\s+(?:(?:ABSTRACT|FINAL|INTERNAL|PUBLIC|PRIVATE|PROTECTED)\s+)*"
                        r"([A-Za-z_][\w.]*)\s*(?::\s*(\S.*?))?\s*$")
# … and with the name first: 'MEMSET FUNCTION: UDINT', 'MEMCMP FUNCTION: DINT'
_HEAD_TEXT_AFTER = re.compile(r"^([A-Za-z_][\w.]*)\s+(FUNCTION_BLOCK|FUNCTION)\s*(?::\s*(\S.*?))?\s*$")
_RETURN_TITLES = ("return value", "return values", "rückgabewert", "return parameter")
_KIND_OF = {"FUNCTION_BLOCK": "FunctionBlock", "FUNCTION BLOCK": "FunctionBlock", "FUNCTION": "Function", "PROGRAM": "Program", "METHOD": "Method",
            "PROPERTY": "Property", "INTERFACE": "Interface", "ACTION": "Action", "VAR_GLOBAL": "GlobalVariable"}
_SECTION_RE = re.compile(r"^\s*(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_GLOBAL|VAR)\b[^\n]*\n(.*?)^\s*END_VAR", re.I | re.M | re.S)
_STRUCT_RE = re.compile(r"STRUCT\b(.*?)END_STRUCT", re.I | re.S)
_ENUM_RE = re.compile(r":\s*\((.*?)\)\s*(?:[A-Za-z_]\w*\s*)?(?::=\s*\w+)?\s*;", re.S)
_VARLINE_RE = re.compile(r"^\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*(?:AT\s+%[IQM][XBWDL]?\*?\s*)?:\s*([^;:=]+?)"
                         r"(?:\s*:=\s*([^;]+?))?\s*;\s*(?:\(\*\s*(.*?)\s*\*\)|//\s*(.*))?\s*$", re.M)

_PREFIX_KIND = (("FB_", "FunctionBlock"), ("F_", "Function"), ("ST_", "Struct"), ("E_", "Enum"), ("I_", "Interface"),
                ("T_", "Alias"), ("U_", "Union"), ("ITc", "Interface"), ("P_", "Program"), ("PRG_", "Program"))


def page_symbols(title: str) -> set[str]:
    """The symbol(s) a page documents, from its title (lower case, the last part of a dotted name): 'MEMCPY',
    'FB_X.Start', 'FUNCTION F_GetVersionTcUtilities', 'F_GetCycleTime()', 'GetHMI (4.1)', 'Data type
    __SYSTEM.ExceptionCode', 'KL6inData, KL6outData'. A title that is a sentence ('Attribute 'linkalways'',
    'Transmitting structures to the PLC', 'AdsClient Class') documents no IEC symbol: its code is an example (N101)."""
    t = " ".join((title or "").split())
    m = re.match(rf"^(?:{_POU_KW}|TYPE)\s+(.+)$", t)
    if m:
        t = m.group(1)
    t = re.sub(r"^(?:Data\s*type|Datentyp)\s+", "", t, flags=re.I)
    t = re.sub(r"\s*\(\s*\)\s*$", "", t)                       # F_GetCycleTime()
    t = re.sub(r"\s*\(\s*v?\d+(?:\.\d+)*\s*\)\s*$", "", t)        # GetHMI (4.1)
    parts = [x.strip() for x in t.split(",")]
    if parts and all(re.fullmatch(r"[A-Za-z_][\w.]*", x) for x in parts):
        return {x.rsplit(".", 1)[-1].lower() for x in parts}
    return set()


def symbol_name(title: str) -> str | None:
    """The one symbol a page documents, as its title writes it ('FUNCTION F_GetVersionTcUtilities' →
    'F_GetVersionTcUtilities', 'F_GetCycleTime()' → 'F_GetCycleTime', 'FB_X.Start'); None for a sentence title or
    one naming several symbols."""
    syms = page_symbols(title)
    if len(syms) != 1:
        return None
    want = next(iter(syms))
    return next((x for x in re.findall(r"[A-Za-z_][\w.]*", title or "") if x.rsplit(".", 1)[-1].lower() == want), None)


# -- which pages document a symbol: one rule for every reader (N100) ------------------------------------------------
DOC_COLS = "rowid AS rid, url, book, page_id AS pid, title, kind, library, decl, version, crumb, markdown AS body"


# N170: a symbol documented inside a page (the derived table `symbols`: a '## Method X' section with its tables, a row
# of a constants table) is a row with the DOC_COLS keys too — rid negative (-symbols.id), title its name
# ('RecipeManCommands.CreateRecipe', 'ERR_RECIPE_NOT_FOUND'), body its section of the page, url / book / pid the page's
# — plus section (the heading), owner (the FB of a method, the GVL of a constant) and page_title.
def is_symbol_row(r: Any) -> bool:
    return int(r["rid"] or 0) < 0


def has_symbols(con: Any) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'symbols'").fetchone() is not None


def declared_row(r: Any) -> bool:
    """A row of the symbols table for a name a page's Syntax section declares (declared_symbols)."""
    try:
        return is_symbol_row(r) and bool(json.loads(r["decl"] or "{}").get("declared_as"))
    except ValueError:
        return False


def _adopted(title: str, d: dict[str, Any], note: str) -> dict[str, Any]:
    h = d["syntax_header"]
    out = {k: v for k, v in d.items() if k not in ("syntax_header", "syntax_header_note")}
    out.update({k: h.get(k) for k in HEADER_FIELDS})
    out.update(declared_as=h["name"], declared_as_note=note)
    return out


def declared_symbols(row: Any) -> list[dict[str, Any]]:
    """Re-review of 0.8.15: the name a page's Syntax section declares ('ActualsActuator' declares FB_ActuatorActuals,
    the name code uses) finds that page — one entry {name, owner, kind, section, decl} for the symbols table. Not when
    the page's own tables document what the header does not declare, nor for a header that shows how to derive from
    the page's symbol (parse_declaration); a page of that name in the same library or book still wins, and several
    pages whose headers declare one name differently document none (symbol_documents)."""
    try:
        d = json.loads(row["decl"]) if row["decl"] else {}
    except ValueError:
        return []
    h = d.get("syntax_header") if isinstance(d, dict) else None
    if not isinstance(h, dict) or h.get("undeclared"):
        return []
    note = (f"no page is titled {h['name']}; the Syntax section of the page {str(row['title']).strip()!r} declares it")
    return [{"name": h["name"], "owner": None, "kind": row["kind"], "section": None,
             "decl": {**_adopted(str(row["title"]), d, note), "name": h["name"]}}]


def header_answer(con: Any, row: Any, d: dict[str, Any]) -> dict[str, Any]:
    """A page's declaration with its Syntax-section header (parse_declaration) decided — the one rule of
    infosys_declaration and the skill. The header is the page's declaration (declared_as) only when nothing
    contradicts it: no page of the page's library or book documents the name it declares (SendMessageEx2's copy of
    SendMessageEx's header; F_TimeToSeconds() showing FUNCTION F_SecondsToTime), and the page's tables document nothing
    it lacks. Otherwise the page shows no declaration of its own, and why is said (syntax_header_note)."""
    h = d.get("syntax_header")
    if not isinstance(h, dict) or is_symbol_row(row):
        return d
    title = str(row["title"]).strip()
    lib = str(row["library"] or row["book"] or "").lower()

    def same_place(r: Any) -> bool:
        return not is_symbol_row(r) and r["rid"] != row["rid"] and lib in (
            str(r["library"] or "").lower(), str(r["book"] or "").lower())
    other = symbol_documents(con, h["name"], same_place)
    why = ([f"{h['name']} has its own page ({other[0][0]['url']})"] if other else []) + (
        [f"the page's tables document {', '.join(h['undeclared'])}, which that header does not declare"]
        if h.get("undeclared") else [])
    if not why:
        return _adopted(title, d, f"the page is titled {title!r}; its Syntax section declares {h['name']}, a name no "
                                  "other page of its library documents")
    out = {k: v for k, v in d.items() if k != "syntax_header"}
    out["syntax_header"] = {"name": h["name"], "declaration": h.get("declaration")}
    out["syntax_header_note"] = (f"the header in its Syntax section declares {h['name']}, and is not taken as "
                                 f"{d.get('name') or title}'s declaration: " + "; ".join(why)
                                 + " — the page shows no declaration of its own")
    return out


def symbol_reconstructed(r: Any) -> bool:
    """A symbol row whose declaration the index reconstructed from tables (a '## Method X' section, a constants table)
    — not a name a page's Syntax section declares (declared_row: that page's code is its declaration)."""
    return is_symbol_row(r) and not declared_row(r)


def _section_text(markdown: str, heading: str | None) -> str:
    """The section of a page under this heading, the heading line included; the whole page without one."""
    if not heading:
        return markdown
    for lvl, head, text in _md_sections(markdown):
        if head == heading:
            return f"{'#' * lvl} {head}{text}"
    return markdown


def symbol_rows(con: Any, name: str, keep: Callable[[Any], bool] | None = None) -> list[dict[str, Any]]:
    """N170: the symbols documented inside pages with this name — 'Owner.Name' by owner and name, a bare name by name;
    without case, but an exact-case match wins (a 'createRecipe' never hides a 'CreateRecipe' of the same case as
    asked). [] for an index without the table (built before 0.8.15)."""
    s = " ".join(name.split())
    if not s or not has_symbols(con):
        return []
    owner, member = s.rsplit(".", 1) if "." in s else ("", s)
    sql = ("SELECT s.id, s.name, s.owner, s.kind, s.section, s.decl, p.url, p.book, p.page_id, p.title, p.library, "
           "p.version, p.crumb, p.markdown FROM symbols s JOIN pages p ON p.rowid = s.page_rowid "
           "WHERE s.name = ? COLLATE NOCASE" + (" AND s.owner = ? COLLATE NOCASE" if owner else ""))
    got = con.execute(sql, (member, owner) if owner else (member,)).fetchall()
    exact = [r for r in got if r[1] == member and (not owner or r[2] == owner)]
    out = []
    for r in exact or got:
        title = f"{r[2]}.{r[1]}" if r[2] and r[3] != "Constant" else r[1]
        row = {"rid": -int(r[0]), "url": r[6], "book": r[7], "pid": r[8], "title": title, "kind": r[3],
               "library": r[10], "decl": r[5], "version": r[11], "crumb": r[12],
               "body": _section_text(r[13] or "", r[4]), "section": r[4], "owner": r[2], "page_title": r[9]}
        if keep is None or keep(row):
            out.append(row)
    return out


def title_word_documents(con: Any, name: str, keep: Callable[[Any], bool] | None = None,
                         limit: int = 50) -> list[list[Any]]:
    """N170: the pages whose title has the name as a word ('Library Recipe Management - RecipeManCommands' for
    RecipeManCommands) — what answers a symbol no page and no section documents, as documents (same_documents)."""
    s = " ".join(name.split())
    if len(s) < 3 or not re.fullmatch(r"[A-Za-z_][\w.]*", s):
        return []
    src = "catalog" if con.execute("SELECT 1 FROM sqlite_master WHERE name = 'catalog'").fetchone() else "pages"
    word = re.compile(rf"(?<![\w.]){re.escape(s)}(?![\w])", re.I)
    rids = [rid for rid, title in con.execute(f"SELECT rowid, title FROM {src} WHERE title LIKE ? ESCAPE '\\' "
                                              "COLLATE NOCASE", ("%" + _like(s) + "%",)) if word.search(title or "")]
    rows = [con.execute(f"SELECT {DOC_COLS} FROM pages WHERE rowid = ?", (rid,)).fetchone() for rid in rids[:limit]]
    return same_documents([r for r in rows if r is not None and (keep is None or keep(r))], con)


def _decl_text(decl: str | None) -> str:
    try:
        d = json.loads(decl) if decl else {}
    except ValueError:
        d = {}
    return " ".join(str((d or {}).get("declaration") or "").split())


_MD_LINK_TARGET = re.compile(r"\]\([^)\s]*\)")


def _body_text(r: Any) -> str:
    # a page's text without its link targets (a copy in another book links to that book's pages)
    try:
        md = r["body"] or ""
    except (IndexError, KeyError):
        return ""
    return " ".join(_MD_LINK_TARGET.sub("]", md).split())


def _doc_order(r: Any) -> tuple:
    # the representative of one document: a page that names its library, a library's own book, then by book, page id
    return (not r["library"], not str(r["book"]).startswith(("tcplclib_", "tc3plclib_")), len(r["book"] or ""),
            r["book"] or "", r["pid"] or "")


_MEMBER_KINDS = frozenset({"Method", "Property", "Action", "Transition"})


def member_owners(con: Any, rows: list[Any]) -> dict[int, str]:
    """{rid: the symbol its parent page documents (lower case)} of the member pages (method, property, action,
    transition) among rows (DOC_COLS) whose parent in the Help Viewer contents is a symbol page — format 6 stores the
    parent; a member below a topic page ('Methods') or with no parent has no owner here."""
    if con is None or not (has_parents(con) or any(is_symbol_row(r) for r in rows)):
        return {}
    out: dict[int, str] = {}
    for r in rows:
        if r["kind"] in _MEMBER_KINDS and is_symbol_row(r):
            if r["owner"]:
                out[r["rid"]] = str(r["owner"]).lower()      # N170: a method section of its FB's page
        elif r["kind"] in _MEMBER_KINDS:
            up = _ancestors(con, r["rid"])
            name = symbol_name(up[-1][0]) if up and up[-1][1] != "Page" else None
            if name:
                # review: the symbol's own name — 'Tc3_Foo.FB_X' and 'FB_X' are one owner, as _owned_by reads them
                out[r["rid"]] = name.rsplit(".", 1)[-1].lower()
    return out


def _ancestors(con: Any, rid: int, depth: int = 3) -> list[tuple[str, str]]:
    """(title, kind) of a page's parents in the Help Viewer contents, nearest first: through topic pages ('Methods')
    up to the first symbol page, at most `depth` (format 6)."""
    out: list[tuple[str, str]] = []
    r = con.execute("SELECT parent FROM pages WHERE rowid = ?", (rid,)).fetchone()
    url = r[0] if r is not None else None
    while url and len(out) < depth:
        q = con.execute("SELECT title, kind, parent FROM pages WHERE url = ?", (url,)).fetchone()
        if q is None:
            break
        out.append((q[0], q[1]))
        if q[1] != "Page":
            break
        url = q[2]
    return out


def same_documents(rows: list[Any], con: Any = None) -> list[list[Any]]:
    """Rows (DOC_COLS) grouped into documents. Two pages are one document when they have the same page id (one
    Beckhoff page in several books: T_MaxString 31062795 in tcplclib_tc2_system and tf6510_tc3_iec61850), or the
    same library — compared without case, the books spell it both ways (Tc2_EtherCAT, Tc2_Ethercat) — and the same
    stored declaration (a page with none: the same text, link targets aside) — the answer is the same whichever is
    read; a page that names no library, with the declaration of exactly one library's page, is that page's copy. A
    document has at most one page per book (a copy is in another book). con (format 6): a member page is one document
    only with pages below the same symbol — the AddKey of FB_JsonSaxWriter and of FB_JsonSaxPrettyWriter have one
    declaration and are two answers (kb_verify I13, 0.8.14). Groups and their rows in _doc_order."""
    parent = list(range(len(rows)))
    books = [{str(r["book"] or "").lower()} for r in rows]       # per group: the books it has a page of
    of = member_owners(con, rows)
    owners = [{of[r["rid"]]} if r["rid"] in of else set() for r in rows]    # per group: the symbol its members are of

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int, same_source: bool = False) -> bool:
        # a copy is in another book: two pages of one book are never one document (review 0.8.12 — two FBs'
        # methods of one name and signature, both titled 'Reset', are two answers, not a page and its copy) — unless
        # they are one Beckhoff source page placed twice (same_source: the page id before '_', N99b)
        a, b = find(i), find(j)
        if a == b:
            return True
        if books[a] & books[b] and not same_source:
            return False
        if owners[a] and owners[b] and owners[a] != owners[b]:
            return False                        # members of two symbols (format 6): two answers, whatever the text
        parent[b] = a
        books[a] |= books[b]
        owners[a] |= owners[b]
        return True

    seen: dict[tuple, list[int]] = {}
    libs_of: dict[str, set[str]] = {}
    # the page id first, for every row: the same Beckhoff page is one document whatever the order of the rows (a
    # declaration or text match must not take a book a true copy needs — second review 0.8.12)
    for i, r in enumerate(rows):
        if r["pid"] and not is_symbol_row(r):          # a symbol in a page shares its page id with the page
            # N99b (0.8.13 retest): '374277003_30131813' and '374277003_2750866' are Beckhoff's page 374277003 placed
            # twice in one book (the 4 'ADS Return Codes' of tf6500, byte-identical) — one page, merged like its copies.
            # Review of 0.8.14: a shared id prefix alone is not that — two FBs' 'EqualsTo' (5002755467_60885635 /
            # _11099805) and 8 prefixes whose pages differ were merged. One source page: the same text, and no member
            # page (a method / property of two FBs is two answers, whatever its text)
            pid = str(r["pid"])
            base = pid.split("_", 1)[0]
            for j in seen.get(("pid", base), []):
                rj = rows[j]
                if str(rj["pid"]) == pid:
                    joined = union(j, i)                    # the same page id: a copy in another book
                elif (r["kind"] not in _MEMBER_KINDS and rj["kind"] not in _MEMBER_KINDS
                      and _body_text(r) and _body_text(r) == _body_text(rj)):
                    joined = union(j, i, same_source=True)
                else:
                    continue
                if joined:
                    break
            seen.setdefault(("pid", base), []).append(i)
    for i, r in enumerate(rows):
        keys = []
        d = _decl_text(r["decl"])
        if is_symbol_row(r):
            # N170: one symbol in sections of several pages — the PLCPRJ_ERROR_* constants of the four TCP/IP samples
            # of one book — is one answer when name and declaration are the same
            keys.append(("sym", str(r["title"]).lower(), d))
        elif d:
            keys.append(("decl", (r["library"] or "").lower(), d))
            if r["library"]:
                libs_of.setdefault(d, set()).add(r["library"].lower())
        else:
            b = _body_text(r)                     # no declaration stored: the same text is the same page
            if b:
                keys.append(("text", (r["library"] or "").lower(), b))
        for k in keys:
            for j in seen.get(k, []):
                if union(j, i, same_source=k[0] == "sym"):
                    break
            seen.setdefault(k, []).append(i)
    # a page that names no library joins the one library whose page shows the same declaration (a product's copy of a
    # library page: tc3_eventlogger next to tcplclib_tc3_eventlogger); with two such libraries it stays apart
    for i, r in enumerate(rows):
        d = _decl_text(r["decl"])
        if d and not r["library"] and not is_symbol_row(r) and len(libs_of.get(d, ())) == 1:
            for j in seen.get(("decl", next(iter(libs_of[d])), d), []):
                if union(j, i):
                    break
    groups: dict[int, list[Any]] = {}
    for i, r in enumerate(rows):
        groups.setdefault(find(i), []).append(r)
    out = [sorted(g, key=_doc_order) for g in groups.values()]
    return sorted(out, key=lambda g: _doc_order(g[0]))


def names_symbol(title: str, name: str) -> bool:
    """Does a page with this title document the symbol `name` (IEC: without case)? 'MEMCPY', 'FUNCTION MEMCPY',
    'MEMCPY()', 'FB_X.Start' for Start; a sentence title documents none (N101)."""
    n = name.strip().lower()
    t = " ".join((title or "").split()).lower()
    if not n:
        return False
    if t == n:
        return True
    sym = (symbol_name(title) or "").lower()
    return sym == n or ("." not in n and n in page_symbols(title))


def symbol_documents(con: Any, name: str, keep: Callable[[Any], bool] | None = None,
                     _seen: frozenset[str] = frozenset()) -> list[list[Any]]:
    """N100 (0.8.11 retest): the pages that document the IEC symbol `name`, as documents (same_documents) — the one
    rule of infosys_declaration, infosys_get_page by name and the twincat-docs skill's docs_declaration / docs_page
    (they disagreed: the skill called T_MaxString ambiguous — one page in two books — and ADSREAD — the C++ AdsRead
    page documents no IEC symbol; the plugin took the first page of one library). A page documents it when it is
    not a 'Page' and its title names it (names_symbol). keep: the caller's filter (library=, book=). More than one
    document: the name is ambiguous, nothing is chosen (R23)."""
    s = " ".join(name.split())
    if not s:
        return []
    rows = {r["rid"]: r for r in con.execute(f"SELECT {DOC_COLS} FROM pages WHERE title = ? COLLATE NOCASE "
                                             "AND kind != 'Page'", (s,))}
    has_catalog = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'catalog'").fetchone() is not None
    src = "catalog" if has_catalog else "pages"
    owner, member = s.rsplit(".", 1) if "." in s else ("", s)
    members = "." in s and has_parents(con)
    pat = "%" + _like(member) + "%"
    more, bare = [], []
    for rid, title in con.execute(f"SELECT rowid, title FROM {src} WHERE kind != 'Page' AND title LIKE ? "
                                  "ESCAPE '\\' COLLATE NOCASE", (pat,)):
        if rid not in rows and names_symbol(title, s):
            more.append(rid)
        elif members and names_symbol(title, member):
            bare.append(rid)

    def fetch(rids: list[int]) -> list[Any]:
        got = (con.execute(f"SELECT {DOC_COLS} FROM pages WHERE rowid = ? AND kind != 'Page'", (rid,)).fetchone()
               for rid in rids)
        return [r for r in got if r is not None]
    for r in fetch(more):
        rows[r["rid"]] = r
    cands = [r for r in rows.values() if keep is None or keep(r)]
    if members:
        # N126 (format 6): 'FB_X.Call' — Beckhoff titles a method page with its bare name; the pages below FB_X's page
        # in the contents are FB_X's members (a page titled 'FB_X.Call' itself stays a candidate). kb_verify I13
        # (0.8.14): the bare name as a title writes it — 'LookUp()', 'GetHMI (4.1)' (names_symbol), not only 'LookUp'
        # owner first, by rowid: a member name like 'Reset' has hundreds of pages; only the owner's are read (review)
        owned = [r for r in fetch([x["rid"] for x in _owned_by(con, [{"rid": rid} for rid in bare], owner)])
                 if keep is None or keep(r)]
        ids = {r["rid"] for r in cands}
        cands += [r for r in owned if r["rid"] not in ids]
    # N170 (0.8.14 retest): a symbol documented in a section or table of a page (RecipeManCommands' 22 methods, the
    # ERR_ codes of its GVL) — unless a page of the same library / book documents it (its own page is the answer)
    libs = {str(r["library"]).lower() for r in cands if r["library"]}
    books = {str(r["book"]).lower() for r in cands}
    syms = [r for r in symbol_rows(con, s, keep)
            if not ((r["library"] and r["library"].lower() in libs) or str(r["book"]).lower() in books)]
    declared = [r for r in syms if declared_row(r)]
    if declared and len(same_documents(declared, con)) > 1:
        # third review of 0.8.15: FB_BA_Settings is declared by the Syntax sections of FB_BA_Device and
        # FB_BA_WeatherParameter, differently — neither documents it ('ambiguous', and library= could not resolve it)
        syms = [r for r in syms if not declared_row(r)]
    cands += syms
    docs = same_documents(cands, con)
    if not docs and "." in s and len(_seen) < 4:
        # N158 (0.8.14 retest): FB_TcMessage.Send was not found — Send is I_TcMessage's (FB_TcMessage's methods table
        # says so: definition location I_TcMessage), and I_TcMessage.Send resolves. A member its owner does not
        # document itself is looked up where the owner's page says it is defined, then up EXTENDS and IMPLEMENTS
        vias = member_sources(con, owner, member, keep)
        if vias:
            # keep picks the owner (member_sources: one document); where the member comes from is looked up in the
            # owner's library or book (re-review of 0.8.15: library= filtered that too — a base class of another
            # library was never reached), in any other only when the owner's has no page of that class (third
            # review: FB_D of LibA EXTENDS FB_Base answered LibB's FB_Base.Calc although LibA documents its own)
            place = {str(x).lower() for g in symbol_documents(con, owner, keep, frozenset({owner.lower()}))
                     for x in (g[0]["library"], g[0]["book"]) if x}       # every document of the owner (same sources)

            def at_owner(r: Any) -> bool:
                return bool(place & {str(r["library"] or "").lower(), str(r["book"] or "").lower()})
        for via in vias:
            if via.lower() not in _seen:
                seen = _seen | {owner.lower(), via.lower()}
                docs = symbol_documents(con, f"{via}.{member}", at_owner, seen)
                if not docs and not symbol_documents(con, via, at_owner):
                    docs = symbol_documents(con, f"{via}.{member}", None, seen)
                if docs:
                    break
    return docs


def near_symbols(con: Any, name: str, keep: Callable[[Any], bool] | None = None, limit: int = 8) -> list[Any]:
    """N159 (0.8.14 retest): what a symbol that is not found is offered instead (each document once, DOC_COLS rows):
    the pages with the name as a word of their title; for 'Owner.Member' the owner's page, then the types the owner
    says the member comes from (member_sources) and their member; then the pages whose TITLE has all the name's words,
    then any of them (prefixes, words of three letters and more). The near list for FB_TcMessage.Send was eight DALI
    FBs: the title filter 'title : "FB"* "TcMessage"* "Send"*' applies to the first word only — the others matched
    the page text."""
    s = " ".join(name.split())
    out: list[Any] = []
    seen: set[int] = set()

    def add(groups: list[list[Any]]) -> None:
        for g in groups:
            if len(out) >= limit:
                return
            if not any(r["rid"] in seen for r in g):
                out.append(g[0])
            seen.update(r["rid"] for r in g)
    add(title_word_documents(con, s, keep))
    if "." in s:
        owner, member = s.rsplit(".", 1)
        add(symbol_documents(con, owner, keep))
        for via in member_sources(con, owner, member, keep):
            add(symbol_documents(con, via, keep))
            add(symbol_documents(con, f"{via}.{member}", keep))
    words = re.findall(r"[^\W_]+", s)
    words = [w for w in words if len(w) >= 3] or words
    if words and len(out) < limit and con.execute("SELECT 1 FROM sqlite_master WHERE name = 'pages_fts'").fetchone():
        terms = ['"' + w.replace('"', '""') + '"*' for w in words]
        for op in (" AND ", " OR "):
            try:
                rids = [r[0] for r in con.execute("SELECT rowid FROM pages_fts WHERE pages_fts MATCH ? ORDER BY rank "
                                                  "LIMIT 200", (f"title : ({op.join(terms)})",))]
            except sqlite3.Error:
                rids = []
            rows = [con.execute(f"SELECT {DOC_COLS} FROM pages WHERE rowid = ?", (rid,)).fetchone() for rid in rids]
            add(same_documents([r for r in rows if r is not None and (keep is None or keep(r))], con))
            if len(out) >= limit or len(words) == 1:
                break
    return out[:limit]


def defined_in(asked: str, parent_title: str | None) -> dict[str, str] | None:
    """N158: {"asked_as", "defined_in", "note"} when 'Owner.Member' was answered by a member of another symbol (an
    interface it implements, a base it extends) — None when the answer is Owner's own member or nothing says whose."""
    if "." not in asked or not parent_title:
        return None
    owner, member = asked.rsplit(".", 1)
    p = symbol_name(parent_title) or ""
    if not p or p.rsplit(".", 1)[-1].lower() == owner.rsplit(".", 1)[-1].lower():
        return None
    return {"asked_as": asked, "defined_in": p,
            "note": f"{owner} documents no {member} of its own; it is {p}'s (where {owner}'s page, EXTENDS or "
                    f"IMPLEMENTS says it is defined)"}


def member_sources(con: Any, owner: str, member: str, keep: Callable[[Any], bool] | None = None) -> list[str]:
    """N158: where a member of `owner` is defined when owner's pages do not document it — the definition location the
    owner's methods / properties table gives it ('I_TcMessage', 'Inherited from FB_TcEventBase'; 'Local' names
    none), else the types the owner EXTENDS, then those it IMPLEMENTS — read from the owner's stored declaration.
    Review of 0.8.15: an owner documented by several pages that say different things (FB_BA_SeqCtrl of Tc3_BaCommon,
    no EXTENDS, and of tf8040, EXTENDS FB_BA_Loop) names no source: which one is meant is not known (owner_sources
    says why; library= picks one)."""
    per = owner_sources(con, owner, member, keep)
    return per[0] if len(per) == 1 else []


def owner_sources(con: Any, owner: str, member: str, keep: Callable[[Any], bool] | None = None) -> list[list[str]]:
    """The sources of `member` (member_sources) per document of `owner` — the same list once."""
    out: list[list[str]] = []
    for g in symbol_documents(con, owner, keep, frozenset({owner.lower()})):
        r = g[0]
        try:
            d = json.loads(r["decl"]) if r["decl"] else {}
        except ValueError:
            d = {}
        here: list[str] = []
        for m in (d.get("methods") or []) + (d.get("properties") or []):
            if str(m.get("name") or "").lower() == member.lower():
                loc = re.sub(r"^(?:inherited\s+from|defined\s+in|from)\s+", "", str(m.get("definition_location") or "").strip(), flags=re.I)
                if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", loc) and loc.lower() not in ("local", owner.lower()):
                    here.append(loc)
        here += [x for x in [d.get("extends")] + list(d.get("implements") or []) if isinstance(x, str) and x]
        here = list(dict.fromkeys(here))
        if here not in out:
            out.append(here)
    return out


def owner_note(con: Any, name: str, keep: Callable[[Any], bool] | None = None) -> str | None:
    """Why 'Owner.Member' found nothing when Owner is documented by several pages that differ in where its members
    come from (review of 0.8.15), and the library= that finds it — None otherwise, also when no library= would find
    it (re-review: 'FB_BA_SeqCtrl.bEnable' got the advice, and library= still found nothing)."""
    if "." not in name:
        return None
    owner, member = name.rsplit(".", 1)
    docs = symbol_documents(con, owner, keep, frozenset({owner.lower()}))
    if len(docs) < 2 or len(owner_sources(con, owner, member, keep)) < 2:
        return None
    finds = []
    for g in docs:
        lib = str(g[0]["library"] or g[0]["book"])

        def pick(r: Any, lib: str = lib.lower()) -> bool:          # what library=<lib> keeps (declaration, the skill)
            return (keep is None or keep(r)) and lib in ((r["library"] or "").lower(), (r["book"] or "").lower())
        if symbol_documents(con, name, pick):
            finds.append(lib)
    if not finds:
        return None
    where = ", ".join(f"{g[0]['library'] or g[0]['book']}" for g in docs[:6])
    return (f"{owner} is documented by {len(docs)} pages ({where}) that differ in what it extends or implements, so "
            f"where its {member} is defined is not known — pass library= with the one the project uses (found with "
            + " / ".join(f"library={x}" for x in finds) + ")")


def title_documents(con: Any, title: str, keep: Callable[[Any], bool] | None = None) -> list[list[Any]]:
    """A page asked for by name: the symbol's documents (symbol_documents); when no page documents a symbol of that
    name, the pages of exactly that title (a topic: "Attribute 'linkalways'"), one document per page id."""
    docs = symbol_documents(con, title, keep)
    if docs:
        return docs
    rows = [r for r in con.execute(f"SELECT {DOC_COLS} FROM pages WHERE title = ? COLLATE NOCASE",
                                   (" ".join(title.split()),)) if keep is None or keep(r)]
    return same_documents(rows, con)


def has_parents(con: Any) -> bool:
    """Format 6: the pages know their parent in the Help Viewer contents."""
    try:
        return any(r[1] == "parent" for r in con.execute("PRAGMA table_info(pages)"))
    except sqlite3.Error:
        return False


def page_family(con: Any, url: str, limit: int = 60) -> dict[str, Any]:
    """N126: {"parent": {title, url}, "children": [{title, url, kind}], "children_count"} of a page — what the Help
    Viewer contents put above and below it (an FB → its methods and properties). {} without parent data."""
    if not has_parents(con):
        return {}
    out: dict[str, Any] = {}
    r = con.execute("SELECT q.title, q.url FROM pages p JOIN pages q ON q.url = p.parent WHERE p.url = ?",
                    (url,)).fetchone()
    if r is not None:
        out["parent"] = {"title": r[0], "url": r[1]}
    kids = con.execute("SELECT title, url, kind FROM pages WHERE parent = ? ORDER BY title COLLATE NOCASE, url",
                       (url,)).fetchall()
    if kids:
        out["children"] = [{"title": k[0], "url": k[1], "kind": k[2]} for k in kids[:limit]]
        if len(kids) > limit:
            out["children_count"] = len(kids)
    return out


# N160 (0.8.14 retest): infosys_get_page and the skill's docs_page read a page alike — one range for max_chars (the
# MCP took 50, the skill refused below 200), a section and an offset on both (the MCP had neither)
PAGE_CHARS = (200, 60_000)


class PageError(InfosysError):
    """A page part that cannot be given: code 'not_found' (no such section; outline lists them) or 'bad_param'."""

    def __init__(self, code: str, message: str, outline: list[str] | None = None):
        super().__init__(message)
        self.code, self.outline = code, outline or []


def page_sections(body: str) -> list[tuple[str, str]]:
    """[(heading text, block text incl. the heading line)] of a page's Markdown; text before the first heading has
    heading ''. A '#' line inside a ``` block is code, not a heading."""
    out: list[tuple[str, list[str]]] = [("", [])]
    fence = False
    for ln in body.splitlines(keepends=True):
        if ln.startswith("```"):
            fence = not fence
        elif not fence and re.match(r"#{1,4} ", ln):
            out.append((ln.lstrip("#").strip(), []))
        out[-1][1].append(ln)
    return [(h, "".join(b)) for h, b in out if h or "".join(b).strip()]


def part_ranges(offset: Any, max_chars: Any) -> None:
    """The ranges of a page part (N160 / N169: the refusal names the way to read on). Raises PageError."""
    lo, hi = PAGE_CHARS
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or not lo <= max_chars <= hi:
        raise PageError("bad_param", f"max_chars must be {lo}-{hi} (got {max_chars!r}) — read a long page in parts: "
                                     "section=<a heading of the outline> or offset=<next_offset>")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise PageError("bad_param", f"offset must be >= 0 (got {offset!r}) — 0 is the start, next_offset the next part")


def title_word_answer(name: str, body: str, read_with: str) -> dict[str, Any]:
    """N170: what answers a name no symbol page and no section documents, when one page has it as a word of its title —
    the fields every reader gives (review of 0.8.15: infosys_declaration and docs_declaration built them apart, with
    two outline rules): kind Page, match 'title', the page's outline (page_sections), and the note."""
    return {"kind": "Page", "match": "title", "outline": [h for h, _ in page_sections(body or "") if h],
            "note": f"no page or section documents a symbol named {name}; this page has the name in its title — "
                    f"{read_with} for its text"}


def page_part(body: str, section: str | None = None, offset: int = 0, max_chars: int = 4000) -> dict[str, Any]:
    """The part of a page a reader shows: its outline; one section (a heading that IS the one asked for wins — N170:
    'Method CreateRecipe' is not also 'Method CreateRecipeNoSave' — else every heading that contains it); max_chars
    of it from offset, with next_offset when more follows. -> {outline, section, text, chars, offset, next_offset,
    truncated}. Raises PageError."""
    part_ranges(offset, max_chars)
    secs = page_sections(body)
    outline = [h for h, _ in secs if h]
    text = body
    if section:
        want = section.lower()
        exact = any(h.lower() == want for h, _ in secs)
        on, keep = False, []
        for h, block in secs:
            if h:
                on = h.lower() == want if exact else want in h.lower()
            if on:
                keep.append(block)
        if not keep:
            raise PageError("not_found", f"no section heading contains {section!r} — pick one of the outline headings, "
                                         "or leave section out", outline)
        text = "".join(keep).strip()
    n = len(text)
    if offset and offset >= n:
        raise PageError("bad_param", f"offset {offset} is past the end of the text ({n} chars) — offset=0 for the start")
    nxt = offset + max_chars if offset + max_chars < n else None
    return {"outline": outline, "section": section or None, "text": text[offset:offset + max_chars], "chars": n,
            "offset": offset, "next_offset": nxt, "truncated": nxt is not None}


def _owned_by(con: Any, rows: list[Any], owner: str) -> list[Any]:
    """The candidate pages (each with "rid") whose parent in the contents documents `owner` (FB_WG_ComboFilter.Call →
    the 'Call' page below FB_WG_ComboFilter's page) — the parent itself, or the first symbol page above topic pages
    ('Methods'), as member_owners reads it."""
    out = []
    for r in rows:
        up = _ancestors(con, r["rid"])
        if up and (names_symbol(up[0][0], owner) or (up[-1][1] != "Page" and names_symbol(up[-1][0], owner))):
            out.append(r)
    return out


# -- searching the index: one search for every reader (N99, N105) ---------------------------------------------------
def search_index(con: Any, query: str, limit: int, where: str = "", extra: list[Any] | None = None,
                 snippet_tokens: int = 18, offset: int = 0) -> dict[str, Any]:
    """The Infosys search of infosys_search and of the twincat-docs skill's docs_search (they ranked differently):
    docsearch.search_tiers over pages_fts with the column weights of docsearch.infosys_rank, then
      - an error code (docsearch.code_forms): the pages whose table defines it (hex and decimal in one row) first;
      - the hits ordered by their titles (docsearch.title_order);
      - one page in several books once (same_documents): its copies are named in `copies` — the ADS return-code
        table is in some 30 books.
    where / extra: a filter on the pages (alias p) and its parameters. offset (N167): the documents skipped — the
    next page of hits is offset=next_offset; every page is cut from one order (search_tiers' skip). Raises
    docsearch.SearchError.
    -> {rows (rid, url, book, pid, title, version, kind, library, s = excerpt, r = bm25), total, mode, expr, info,
        code (code_forms or None), copies {rid: [(book, page id), …]}}"""
    from . import docsearch as DS
    extra = list(extra or [])
    DS.vocab(con, "pages_fts")
    cols = ("pages_fts.rowid AS rid, p.url, p.book, p.page_id AS pid, p.title, p.version, p.kind, p.library, "
            f"snippet(pages_fts, 2, '[', ']', '…', {int(snippet_tokens)}) AS s, {DS.infosys_rank(con)} AS r")
    w = "pages_fts MATCH ?" + (f" AND ({where})" if where else "")
    sql = f"SELECT {cols} FROM pages_fts JOIN pages p ON p.rowid = pages_fts.rowid WHERE {w} ORDER BY r LIMIT ?"
    count_sql = f"SELECT COUNT(*) FROM pages_fts JOIN pages p ON p.rowid = pages_fts.rowid WHERE {w}"
    title_sql = (f"SELECT {cols} FROM pages_fts JOIN pages p ON p.rowid = pages_fts.rowid WHERE {w} "
                 "ORDER BY length(p.title), r LIMIT ?")

    def body(r: Any) -> str:
        x = con.execute("SELECT markdown FROM pages WHERE rowid = ?", (r["rid"],)).fetchone()
        return (x[0] if x else "") or ""

    forms, defined = DS.code_definitions(con, query, sql, extra, body)
    copies: dict[int, list[tuple[str, str]]] = {}
    seen = {"read": 0, "documents": 0}

    def arrange(rows: list[Any], tail: list[Any] = ()) -> list[Any]:
        seen["read"] = len(rows) + len(tail)
        ordered = DS.title_order(rows, query, "title", "library") + list(tail)
        if defined:
            ids = {r["rid"] for r in defined}
            # N99b: of the pages whose table defines the code, those whose title says they are a code table ('ADS
            # Return Codes') first — an event-class struct that lists the codes too (ST_TcGeneralAdsEventClass) after
            tables = sorted(defined, key=lambda r: not _CODE_TITLE.search(str(r["title"] or "")))
            ordered = tables + [r for r in ordered if r["rid"] not in ids]
        out = _one_per_document(con, ordered, copies)
        seen["documents"] = len(out)
        return out

    rows, total, mode, expr, info = DS.search_tiers(con, sql, count_sql, query, extra, limit, fts="pages_fts",
                                                    arrange=arrange, title_col="title", title_sql=title_sql, skip=offset)
    outside = 0                                 # defining pages without the tier's words: listed, so counted
    if not rows and defined:                    # the code's table, though no tier found the query's other words
        rows, total, mode = arrange([])[:offset + limit], 0, "the code's table (no page has the other words)"
        outside = len(defined)
    elif defined and expr:
        for r in defined:
            try:
                hit = con.execute("SELECT 1 FROM pages_fts WHERE rowid = ? AND pages_fts MATCH ?", (r["rid"], expr)).fetchone()
            except sqlite3.Error:
                hit = True
            outside += 0 if hit else 1
    total += outside
    ids = {r["rid"] for r in defined}
    # more: hits not shown — more documents than shown up to this page among those read, or pages that were not
    # read. `total` counts pages: a page in several books once per book (copies are listed once, with same_page_in)
    more = seen["documents"] > len(rows) or total > seen["read"] + outside
    rows = rows[offset:offset + limit]                                     # N167: this page of the hits
    return {"rows": rows, "total": total, "more": more, "mode": mode, "expr": expr, "info": info, "code": forms,
            "offset": offset, "next_offset": offset + len(rows) if more and rows else None,
            "defined": sum(1 for r in rows if r["rid"] in ids),
            "copies": {r["rid"]: copies[r["rid"]] for r in rows if r["rid"] in copies}}


def code_note(res: dict[str, Any]) -> str | None:
    """What an answer to a code query says about where the code is defined (N99)."""
    if not res.get("code"):
        return None
    c = res["code"]
    if res.get("defined"):
        return (f"{res['defined']} of these pages define {c['hex'][0]} / {c['dec']} in a table (hex and decimal in one "
                "row) — they are listed first")
    return f"no indexed table holds {c['hex'][0]} and {c['dec']} in one row: these pages mention the code"


_CODE_TITLE = re.compile(r"\b(?:return|error|result|status)\s+codes?\b|\berror\s*(?:codes?|numbers?|messages?)\b|\bcodes\b",
                         re.I)


def _one_per_document(con: Any, rows: list[Any], copies: dict[int, list[tuple[str, str]]]) -> list[Any]:
    """The hits with each document once, at the place of its best hit (same_documents: the same page id, or the same
    library and declaration / text), shown as the document's representative (a page that names its library, the
    library's own book — as infosys_declaration answers); the others in copies[rid of the one shown]. Only hits that
    share a title are compared (a copy has its page's title)."""
    by_title: dict[str, list[Any]] = {}
    for r in rows:
        by_title.setdefault(" ".join(str(r["title"] or "").lower().split()), []).append(r)
    pos = {r["rid"]: i for i, r in enumerate(rows)}
    place: dict[int, Any] = {}                 # index in rows -> the row shown there
    drop: set[int] = set()
    for same in by_title.values():
        if len(same) < 2:
            continue
        full = [con.execute(f"SELECT {DOC_COLS} FROM pages WHERE rowid = ?", (r["rid"],)).fetchone() for r in same]
        for g in same_documents([f for f in full if f is not None], con):
            if len(g) < 2:
                continue
            best = min(pos[x["rid"]] for x in g)
            rep = g[0]["rid"]                  # same_documents puts the representative first
            shown = dict(rows[pos[rep]])
            if "r" in shown:
                shown["r"] = rows[best]["r"]   # N99b: the document's place and score are its best hit's
            place[best] = shown
            copies[rep] = [(x["book"], x["pid"]) for x in g[1:]]
            drop.update(pos[x["rid"]] for x in g if pos[x["rid"]] != best)
    return [place.get(i, r) for i, r in enumerate(rows) if i not in drop]


def _ident_matches(ident: str, title: str) -> bool:
    return bool(ident) and ident.lower() in page_symbols(title)


def _kind_of_type(code: str, at: int) -> str:
    seg = code[at:at + 400].upper()
    if re.search(r"\bSTRUCT\b", seg):
        return "Struct"
    if re.search(r"\bUNION\b", seg):
        return "Union"
    if re.search(r":\s*\(", seg):
        return "Enum"
    return "Alias"


def _intro_text(markdown: str) -> str:
    """The page's first paragraphs, before its first sub-heading (what it says it is)."""
    body = re.sub(r"(?m)^#\s[^\n]*\n", "", markdown or "", count=1)
    return body.split("\n#", 1)[0][:800]


def page_header(title: str, headings: list[tuple[int, str]] | None = None) -> tuple[str, str | None] | None:
    """N103: the header written as the page's title or as a heading — '## FUNCTION MEMCPY : UDINT', '## MEMSET
    FUNCTION: UDINT' — for this page's own symbol: (kind, return type or None)."""
    syms = page_symbols(title)
    if not syms:
        return None
    for text in [" ".join(title.split())] + [" ".join(h.split()) for _lvl, h in (headings or [])]:
        m = _HEAD_TEXT.match(text)
        if m and m.group(2).rsplit(".", 1)[-1].lower() in syms:
            return _KIND_OF[m.group(1)], (m.group(3) or None)
        m = _HEAD_TEXT_AFTER.match(text)
        if m and m.group(1).rsplit(".", 1)[-1].lower() in syms:
            return _KIND_OF[m.group(2)], (m.group(3) or None)
    return None


def return_from_table(title: str, tables: list[list[list[str]]] | None, titles: list[str] | None) -> str | None:
    """N103: the return type as Infosys tables it under 'Return value' — a row named like the function / method
    (LREAL_TO_FMTSTR | STRING(510)), with a Type column."""
    syms = page_symbols(title)
    if not syms:
        return None
    for i, rows in enumerate(tables or []):
        ttl = ((titles or [])[i] if i < len(titles or []) else "") or ""
        if ttl.strip().lower() not in _RETURN_TITLES or len(rows) < 2:
            continue
        head = [c.strip().lower() for c in rows[0]]
        if "name" not in head or not ({"type", "data type", "typ", "datentyp"} & set(head)):
            continue
        ni = head.index("name")
        ti = next(head.index(k) for k in ("type", "data type", "typ", "datentyp") if k in head)
        for r in rows[1:]:
            if len(r) > max(ni, ti) and r[ni].strip().lower() in syms and r[ti].strip():
                return r[ti].strip()
    return None


def _callable_kind(title: str, code_blocks: list[tuple[str, str]], intro: str) -> str | None:
    """A page with a return value is a function or a method (a function block has none) — which one, from the page:
    a dotted title (FB_X.Start), the kind its own code declares under another name (a doc error: 'METHOD SendMessageEx'
    on SendMessageEx2), or what its first sentences call it ('The function converts …', 'Method for adding …')."""
    if "." in title.split()[0]:
        return "Method"
    for _lang, code in code_blocks:
        m = _HEAD_RE.search(code)
        if m and m.group(1) in ("METHOD", "FUNCTION"):
            return _KIND_OF[m.group(1)]
    w = re.search(r"\b(method|function)\b", intro or "", re.I)
    if w:
        return "Method" if w.group(1).lower() == "method" else "Function"
    return None


_OPERATOR_INTRO = re.compile(r"(?:^|[.!?:]\s+)(?:The|This)\s+(?:\S+\s+)?operator\b")
_GVL_BLOCK = re.compile(r"\bVAR_GLOBAL(?:[_\s]+(?:CONSTANT|RETAIN|PERSISTENT))*\b.*?\bEND_VAR\b", re.S | re.I)
_TYPE_BLOCK = re.compile(r"\bTYPE\b.*?\bEND_TYPE\b", re.S | re.I)


def _code_only(code: str, block: re.Pattern) -> bool:
    """The code holds nothing but declarations of this kind (comments, pragmas and region marks aside)."""
    c = re.sub(r"\(\*.*?\*\)|//[^\n]*|\{[^}]*\}", " ", code, flags=re.S)
    return bool(block.search(c)) and not block.sub(" ", c).strip(" \n\t;.…")


def _gvl_page(code_blocks: list[tuple[str, str]], intro: str = "") -> bool:
    """N157 (0.8.14 retest): a page whose code is nothing but VAR_GLOBAL declarations — or whose first code is, when
    it says it documents a global variable list (TcMarkOption: '… in this global variable list …' + a usage sample) —
    documents a global variable list. A VAR_GLOBAL block among other code is a sample (TestAndSet,
    FB_IecCriticalSection, Samples, __TRY: 73 pages were kind GlobalVariable through one)."""
    if not code_blocks:
        return False
    if all(_code_only(c, _GVL_BLOCK) for _l, c in code_blocks):
        return True
    return (_code_only(code_blocks[0][1], _GVL_BLOCK)
            and bool(re.search(r"\bglobal\s+(?:variable\s+list|constants?|variables)\b|\bGVL\b", intro or "", re.I)))


def _operator_page(title: str, intro: str) -> bool:
    """N150 (0.8.14 retest): a page whose own sentences say it documents an operator ('The operator is an extension of
    the IEC 61131-3 standard.', 'The __NEW operator …') — not a page of an FB, a function … (a prefix says so: an FB
    page's 'The operator can acknowledge …' means a person)."""
    if any(title.startswith(pre) for pre, _k in _PREFIX_KIND):
        return False
    # review of 0.8.15: an operator is named in capitals (ADR, SEL, MAX) or as TwinCAT's own ones (__NEW) — the
    # MathOperation block's 'The operator can be selected under MathOperator' is about its setting
    sym = symbol_name(title) or ""
    if not re.fullmatch(r"__[A-Z][A-Z0-9_]*|[A-Z][A-Z0-9_]*", sym):
        return False
    return bool(_OPERATOR_INTRO.search(_prose(intro)))


def _similar_name(a: str, b: str) -> bool:
    """A doc error copies a sibling's header: 'METHOD OnAlarmConfirmed' on OnAlarmDisposed, 'METHOD ChildByName' on
    Child — the names share their start; a sample's FB_Base on SUPER does not."""
    a, b = a.lower(), b.lower()
    common = len(os.path.commonprefix([a, b]))
    return common >= 4 and common >= min(len(a), len(b)) // 2


def _param_kind(title: str, code_blocks: list[tuple[str, str]], tables: list[list[list[str]]] | None,
                table_titles: list[str] | None, intro: str) -> str | None:
    """The kind of a page that documents its symbol's parameters — on a page titled as an identifier, or one whose
    sentences name it ('The function block Profiler …'): 'Refactoring' / 'Operands' show such blocks as samples.
    VAR_INPUT blocks without a header: what the page calls itself, a function block else (as before 0.8.15);
    VAR_IN_OUT / VAR_OUTPUT blocks or Inputs / Outputs tables (Name | Type): only what the page calls itself (N157:
    TestAndSet, 'You can use this function …' with a VAR_IN_OUT block) — none: not told."""
    said = _callable_of_intro(intro, title)
    nm = re.escape(symbol_name(title) or title.strip())
    # review of 0.8.15: 'You can use the Subscription method …' names it as well as 'the method Subscription' does
    if not (_identifier_title(title) or re.search(rf"\b(?:{_CALL})\s+{nm}\b|\b{nm}\s+(?:{_CALL})\b", _prose(intro),
                                                re.I)):
        return None
    if any(not _HEAD_RE.search(c) and re.search(r"\bVAR_INPUT\b", c) for _l, c in code_blocks):
        return said or "FunctionBlock"
    if any(not _HEAD_RE.search(c) and re.search(r"\bVAR_(?:IN_OUT|OUTPUT)\b", c) for _l, c in code_blocks):
        return said
    if any(k < len(table_titles or []) and re.search(r"\b(?:inputs?|outputs?|in[ /_-]?outs?)\b", (table_titles or [])[k] or "",
                                                       re.I) and rows and {"name", "type"} <= {c.strip().lower() for c in rows[0]}
           for k, rows in enumerate(tables or [])):
        return said
    return None


def _prose(intro: str) -> str:
    """The page's first sentences without their code (a sample's 'FUNCTION F_X' is no word of the page)."""
    return " ".join(re.sub(r"```.*?(?:```|$)", " ", intro or "", flags=re.S).split())


_CALL = r"function\s+blocks?|FBs?|methods?|functions?"


def _callable_of_intro(intro: str, title: str = "") -> str | None:
    """What the page's own sentences call its symbol: next to its name ('The IOF_DeviceReset function block …', 'The
    function block Profiler …'), else at the start of one of its first two sentences or after 'this' ('This callback
    method …', 'Method reads …', 'You can use this function …'). A word elsewhere names nothing ('… structure,
    functions, and …', '… The function corresponds to the online reset …')."""
    prose = _prose(intro)
    word = None
    nm = symbol_name(title) or title.strip()
    if nm:
        w = re.search(rf"\b{re.escape(nm)}\s+({_CALL})\b|\b({_CALL})\s+{re.escape(nm)}\b", prose, re.I)
        if w:
            word = w.group(1) or w.group(2)
    if word is None:
        for sent in re.split(r"(?<=[.!?])\s+", prose)[:2]:
            w = (re.match(rf"\s*(?:(?:The|This|A|An)\s+(?:[\w-]+\s+)?)?({_CALL})\b", sent, re.I)
                 or re.search(rf"\bthis\s+(?:[\w-]+\s+)?({_CALL})\b", sent, re.I))
            if w:
                word = w.group(1)
                break
    if word is None:
        return None
    t = word.lower()
    return "Method" if t.startswith("method") else "FunctionBlock" if ("block" in t or t.startswith("fb")) else "Function"


def _identifier_title(title: str) -> bool:
    """A title written as an identifier (TestAndSet, ReceiveByte, MC_PATH_DATA_REF, ADSREAD) — not a word of a
    sentence ('Refactoring', 'Operands'): the parameter blocks of such a page are the symbol's, not a sample's."""
    return bool(re.search(r"[_\d]|[a-z][A-Z]", title)) or (title.isupper() and len(title) > 1)


def classify(title: str, code_blocks: list[tuple[str, str]], headings: list[tuple[int, str]] | None = None,
             tables: list[list[list[str]]] | None = None, table_titles: list[str] | None = None,
             intro: str = "", syntax_code: list[str] | None = None) -> str:
    """The kind of a page, from the evidence for the page's own symbol, strongest first: its header in the code;
    its header as the title or a heading (N103); an operator page (N150); a 'Return value' row named like it (a
    function); the name prefix (FB_, F_ …); a doc error — a header under another name of the kind the page's
    sentences name ('This method …' over 'METHOD OnAlarmConfirmed'), a type under another name on a page of nothing
    but types; parameter blocks without a header (VAR_INPUT / VAR_IN_OUT / VAR_OUTPUT) on a page titled as an
    identifier — what the page calls itself, a function block else; a page of VAR_GLOBAL code (N157). A sample's code
    is no evidence (0.8.14 retest: N150, N157). A page that documents no symbol (a sentence as title) is a Page,
    whatever code it shows (N101)."""
    name = title.strip()
    syms = page_symbols(name)
    if syms:
        other: list[tuple[str, str]] = []                         # headers under another name: (keyword, code)
        for _lang, code in code_blocks:
            m = _HEAD_RE.search(code)
            if not m:
                continue
            kw, ident = m.group(1), m.group(2) or ""
            if kw == "VAR_GLOBAL":
                continue                                          # N157: decided by _gvl_page, last
            if ident.lower() in syms:
                return _kind_of_type(code, m.start()) if kw == "TYPE" else _KIND_OF[kw]
            other.append((kw, code))
        head = page_header(name, headings)
        if head:
            return head[0]
        if _operator_page(name, intro):
            return "Operator"                                     # N150: its samples' TYPE / FB are not its kind
        if return_from_table(name, tables, table_titles):
            # a return value: not a function block. Which callable the page does not say: a function (parse_declaration
            # says so in kind_note) — a method page says 'method' or carries its FB's name
            return _callable_kind(name, code_blocks, intro) or "Function"
        for kw, code in other:
            if kw == "TYPE" and all(_code_only(c, _TYPE_BLOCK) for _l, c in code_blocks):
                return _kind_of_type(code, 0)     # the page's type under another name on a page of types (a doc error)
        if _identifier_title(name):
            # review of 0.8.15: the header in the page's Syntax section is its declaration, also under another name —
            # 'FUNCTION_BLOCK FB_ExternalHydAxisLibRef' on ExternalHydAxisLibRef, Beckhoff's copy-paste 'METHOD
            # SendEqualMessage' on SendHresult() (both had become Pages); a sample is under 'Sample', not 'Syntax'
            for code in syntax_code or []:
                hm = _HEAD_RE.search(code)
                if hm and hm.group(1) not in ("TYPE", "VAR_GLOBAL"):
                    return _KIND_OF[hm.group(1)]
        for pre, kind in _PREFIX_KIND:
            if name.startswith(pre):
                return kind
        said = _callable_of_intro(intro, name)
        for kw, code in other:
            hm = _HEAD_RE.search(code)
            if (kw not in ("TYPE", "VAR_GLOBAL") and hm and _similar_name(hm.group(2) or "", symbol_name(name) or name)
                    and (_KIND_OF.get(kw) == said or (said is None and _identifier_title(name)))):
                # a doc error: a sibling's header ('METHOD OnAlarmConfirmed' on OnAlarmDisposed, 'METHOD SetInitial' on
                # SetInital) — the page's sentences do not say otherwise
                return _KIND_OF[kw]
        pk = _param_kind(name, code_blocks, tables, table_titles, intro)
        if pk:
            return pk                                             # N157: TestAndSet was a GlobalVariable
        if _gvl_page(code_blocks, intro):
            return "GlobalVariable"
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


_SYNTAX_LABEL = re.compile(r"^[#*\t ]*Syntax\b([^:*\n]*)[*\t ]*:?[*\t ]*:?[\t ]*(.*)$", re.I)
_LABEL_LINE = re.compile(r"^\s*(?:#{1,6}\s|\*\*[^*]+\*\*\s*:?\s*$)")


_INTRO_LABEL = re.compile(r"^[*\s]*[A-Za-z][A-Za-z ,'\-/]*[*\s]*:[*\s]*$")     # 'Definition:', 'Declaration:'
_SENTENCE_END = re.compile(r"[\w)\]]\.\s*$")


def _syntax_parts(markdown: str) -> list[tuple[str, bool]]:
    """(text, is code) of what each 'Syntax' label of a page gives: the text on its line ('**Syntax:** `OUT := MAX(IN0,
    IN1, ...)`'), else the code blocks right below it, else the one line of text below it ('CAL <function block>
    (…)'); 'Syntax in the declaration:' / 'Syntax for the call:' are both read. Below the next label or heading nothing
    belongs to the syntax (review of 0.8.15: the first code block within 2000 characters was the sample under
    'Example' / 'Samples' — CAL, SEL — and 'in the declaration:' was read as __VARINFO's syntax).
    A line that ends with ':' introduces what follows (re-review): 'Definition:' under 3708 Syntax headings (the
    EventLogger's FB_TcAlarm …) was read as the syntax and the declaration below it was not; SEL's 'OUT := SEL(G, IN0,
    IN1) means:' lost the two lines it introduces. A label of words alone gives nothing itself; what follows is read up
    to the next label, heading, table, code block or sentence."""
    lines = (markdown or "").split("\n")
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(lines):
        m = _SYNTAX_LABEL.match(lines[i])
        i += 1
        if not m:
            continue
        inline = m.group(2).strip().strip("*").strip().strip("`").strip()
        read = 0                                   # what this label gave so far
        if inline and not _INTRO_LABEL.match(inline):
            out.append((inline, False))
            read = 1
        introduced = inline.endswith(":")          # a line that ends with ':' — what follows belongs to it
        more = not inline or introduced
        while more:
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines) and lines[i].startswith("```"):
                while i < len(lines) and lines[i].startswith("```"):          # the code blocks right below
                    j = i + 1
                    while j < len(lines) and not lines[j].startswith("```"):
                        j += 1
                    block = "\n".join(lines[i + 1:j]).strip()
                    if block:
                        out.append((block, True))
                    i = j + 1
                    while i < len(lines) and not lines[i].strip():
                        i += 1
                break
            if (i >= len(lines) or _LABEL_LINE.match(lines[i]) or _SYNTAX_LABEL.match(lines[i])
                    or lines[i].lstrip().startswith("|")):
                break
            t = lines[i].strip().strip("`").strip()
            if _INTRO_LABEL.match(t):
                if read:
                    break                                  # 'Permitted data types:' — the next label
                i += 1
                continue                                   # 'Definition:' — what it introduces is the syntax
            if read and _SENTENCE_END.search(t):
                break                                      # prose about the syntax, not the syntax
            out.append((t, False))                         # a line of text: the syntax as prose
            read += 1
            i += 1
            introduced = introduced or t.endswith(":")
            more = introduced
    return out


def _operator_syntax(markdown: str) -> str | None:
    """N150: an operator page's syntax — every part its 'Syntax' labels give (_syntax_parts)."""
    return "\n".join(t for t, _code in _syntax_parts(markdown)) or None


_TYPE_KINDS = ("Struct", "Union", "Enum", "Alias")


# the fields a Syntax-section header gives the page's declaration when it is adopted (header_answer)
HEADER_FIELDS = ("declaration", "extends", "implements", "return_type", "return_description", "inputs", "outputs",
                 "in_out", "members")


def _syntax_header(doc: HtmlDoc, name: str, kind: str) -> tuple[str, str] | None:
    """(block, the name it declares) — the POU header in the page's Syntax section that names another identifier than
    the page's symbol, of the page's kind, when no code block of the page declares the page's own symbol. A template's
    header ('FUNCTION_BLOCK FB_T[n]_DataSource', TE3500's generated code) names no symbol; nor does 'VAR_GLOBAL
    CONSTANT' (the word after VAR_GLOBAL is no name) or a TYPE (read by the type rules)."""
    if any((m := _HEAD_RE.search(c)) and m.group(1) not in ("TYPE", "VAR_GLOBAL") and _ident_matches(m.group(2) or "", name)
           for _l, c in doc.code_blocks):
        return None
    for c in (t for t, code in _syntax_parts(doc.markdown) if code):
        m = _HEAD_RE.search(c)
        if (m and m.group(2) and m.group(1) not in ("TYPE", "VAR_GLOBAL") and _KIND_OF.get(m.group(1)) == kind
                and not (m.group(3) or "").startswith(("[", "<")) and not _ident_matches(m.group(2), name)):
            return c.strip(), m.group(2)
    return None


def parse_declaration(title: str, doc: HtmlDoc) -> dict[str, Any]:
    """The page's declaration as the page gives it. Re-review of 0.8.15: a header in its Syntax section that names
    another identifier ('ActualsActuator' declares FUNCTION_BLOCK FB_ActuatorActuals; SendMessageEx2 shows Beckhoff's
    copy of METHOD SendMessageEx) is kept as a fact of the page (syntax_header) — whether it is this symbol's
    declaration needs the other pages (header_answer); a header that derives from the page's own symbol
    (FB_MTP_Procedure's 'FUNCTION_BLOCK FB_MTP_Procedure_1_Service EXTENDS FB_MTP_Procedure') shows how to derive."""
    decl = _parse_declaration(title, doc, None)
    hdr = None if decl["kind"] == "Operator" else _syntax_header(doc, decl["name"], decl["kind"])
    if hdr is None:
        return decl
    block, ident = hdr
    alt = _parse_declaration(title, doc, hdr)
    own = decl["name"].split(".")[-1].lower()
    if str(alt.get("extends") or "").split(".")[-1].lower() == own or own in {str(x).split(".")[-1].lower()
                                                                          for x in alt.get("implements") or []}:
        decl["syntax_header_note"] = (f"its Syntax section shows how to derive from it: "
                                      f"{block.splitlines()[0].strip()} — not a declaration of {decl['name']}")
        return decl
    text = alt["declaration"] or ""
    desc = _descriptions(doc.tables, doc.table_titles)
    decl["syntax_header"] = {"name": ident, **{k: alt.get(k) for k in HEADER_FIELDS},
                             # what the page's own tables document and the header does not declare
                             "undeclared": [n for n in desc if re.fullmatch(r"[A-Za-z_]\w*", n)
                                            and not re.search(rf"(?i)\b{n}\b", text) and n.lower() != own]}
    return decl


def _parse_declaration(title: str, doc: HtmlDoc, header: tuple[str, str] | None) -> dict[str, Any]:
    """parse_declaration with `header` (a Syntax-section header naming another identifier) read as the declaration."""
    intro = _intro_text(doc.markdown)
    syntax_code = [t for t, code in _syntax_parts(doc.markdown) if code]
    kind = classify(title, doc.code_blocks, doc.headings, doc.tables, doc.table_titles, intro, syntax_code)
    decl: dict[str, Any] = {"name": symbol_name(title) or title.strip(), "kind": kind, "declaration": None, "extends": None, "implements": None,
                            "return_type": None, "inputs": [], "outputs": [], "in_out": [], "members": [],
                            "enum_values": [], "description": {}}
    if kind == "Operator":
        # N150 (0.8.14 retest): __NEW was a Struct with the sample's 'TYPE ST_Sample …' as its declaration — an
        # operator's declaration is its syntax; the page's samples are not read
        decl["declaration"] = _operator_syntax(doc.markdown)
        decl["description"] = {}
        return decl
    desc = _descriptions(doc.tables, doc.table_titles)
    own_type = any((m := _HEAD_RE.search(c)) and m.group(1) == "TYPE" and _ident_matches(m.group(2) or "", decl["name"])
                   for _l, c in doc.code_blocks)
    fragments: list[str] = []   # header-less VAR_INPUT/VAR_OUTPUT blocks (how Infosys documents most FBs)
    syntax_decl = header[0] if header else None
    for _lang, code in doc.code_blocks:
        m = _HEAD_RE.search(code)
        if not m:
            if not re.search(r"\bVAR_(?:INPUT|OUTPUT|IN_OUT)\b", code, re.I):
                continue
            fragments.append(code.strip())
            kw, ident, rest = "", "", ""
        else:
            kw, ident, rest = m.group(1), m.group(2) or "", m.group(3) or ""
            if kw not in ("TYPE", "VAR_GLOBAL") and ident and not _ident_matches(ident, decl["name"]):
                if code.strip() != syntax_decl or decl["declaration"] is not None:
                    continue                              # another symbol's header (parse_declaration)
            if kw in ("TYPE", "VAR_GLOBAL") and not page_symbols(decl["name"]):
                continue                                  # a manual page's example (N101)
            if kw == "TYPE" and not _ident_matches(ident, decl["name"]) and (kind not in _TYPE_KINDS or own_type):
                continue                                  # N150: a sample's type (TYPE ST_Sample on __NEW)
            if kw == "VAR_GLOBAL" and kind != "GlobalVariable":
                # N157: a sample's global list (TestAndSet's bGlobalTestFlag) is not the page's declaration; N172: a
                # constant the symbol uses (T_MaxString: MAX_STRING_LENGTH) is named under constants, not members
                if re.match(r"\s*(?:\{[^}]*\}\s*)*VAR_GLOBAL\s+CONSTANT\b", code):
                    for sm in _SECTION_RE.finditer(code + ("\nEND_VAR" if not re.search(r"(?i)END_VAR", code) else "")):
                        decl.setdefault("constants", []).extend(_parse_vars(sm.group(2)))
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
    # N103: the header the page writes as a heading ('## FUNCTION MEMCPY : UDINT') or the 'Return value' table
    ph = page_header(title, doc.headings)
    if ph and ph[0] == kind and ph[1] and not decl["return_type"]:
        decl["return_type"] = ph[1]
    if kind in ("Function", "Method", "Property") and not decl["return_type"]:
        decl["return_type"] = return_from_table(title, doc.tables, doc.table_titles)
        if (decl["return_type"] and kind == "Function" and not ph
                and _callable_kind(title, doc.code_blocks, _intro_text(doc.markdown)) is None):
            decl["kind_note"] = ("the page gives a return value and no header: a function, or a method of a function "
                                 "block — it does not say which")
    if kind == "Function" and not decl["return_type"] and re.search(r"\breturns?\s+(?:the\s+value\s+)?(?:TRUE|FALSE)\b",
                                                                    intro):
        # N157: TestAndSet documents no header or 'Return value' table — 'the function returns TRUE …' does
        decl["return_type"] = "BOOL"
        decl["return_type_note"] = "from the page's text ('returns TRUE / FALSE'); the page gives no header"
    if decl["declaration"] is None and fragments:
        head = {"FunctionBlock": "FUNCTION_BLOCK", "Program": "PROGRAM", "Function": "FUNCTION",
                "Method": "METHOD"}.get(kind)
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
            # the header names the symbol it declares (a Syntax-section header: another name, parse_declaration)
            decl["declaration"] = _return_in_header(decl["declaration"], header[1] if header else short,
                                                    decl["return_type"], "FUNCTION" if kind == "Function" else "METHOD")
    _tidy_declaration(decl)                 # rule 4: stored as every reader needs it
    return decl


# -- symbols documented inside a page (N170) -------------------------------------------------------------------------
# 0.8.14 retest: 'RecipeManCommands', 'CreateRecipe' … were 'no page for symbol' — the 22 methods of RecipeManCommands
# are '## Method X' sections of ONE page (tc3_plc_intro/2525821323) with Inputs / Return value tables and no code, and
# its ERR_ codes a table. Such sections and tables are symbols of their own (derived table `symbols`), looked up when
# no page documents the name (symbol_documents).
_MEMBER_HEAD = re.compile(r"^(Method|Property|Function|Action)\s+([A-Za-z_]\w*)\s*(?:\(\s*\))?\s*$"
                          r"|^([A-Za-z_]\w*)\s*(?:\(\s*\))?\s*\((Method|Property|Function|Action)\)\s*$", re.I)
_CONST_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_IEC_VALUE = re.compile(r"^(?:16#[0-9A-Fa-f_]+|8#[0-7_]+|2#[01_]+|[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|TRUE|FALSE)$")
_IEC_BASED = re.compile(r"^(?:16|8|2)#")
_C_VALUE = re.compile(r"^[+-]?0[xX][0-9A-Fa-f]+[uUlL]*$")
_VALUE_HEAD = re.compile(r"\bvalue\b|\binitiali[sz]ation\b|\binitial\b", re.I)
_PLC_CONTEXT = re.compile(r"\bPLC\b|\bGVL\b|\bglobal\s+(?:variable\s+list|constants?)\b|\bVAR_GLOBAL\b", re.I)
_OWNER_KINDS = (("function block", "FunctionBlock"), ("interface", "Interface"), ("program", "Program"))


def _md_cell(c: str) -> str:
    c = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", c)
    return " ".join(c.replace("**", "").replace("`", "").split())


def _md_sections(md: str) -> list[tuple[int, str, str]]:
    """(level, heading, text up to the next heading of that level or higher) of a page's Markdown; a '#' line in a
    code block is code."""
    heads = []
    fence = False
    for m in re.finditer(r"(?m)^(```.*|(#{1,6})\s+(.*))$", md):
        if m.group(1).startswith("```"):
            fence = not fence
        elif not fence:
            heads.append((m.start(), m.end(), len(m.group(2)), m.group(3).strip()))
    out = []
    for i, (_s, e, lvl, head) in enumerate(heads):
        end = next((s2 for s2, _e2, l2, _h2 in heads[i + 1:] if l2 <= lvl), len(md))
        out.append((lvl, head, md[e:end]))
    return out


def _md_tables(text: str) -> list[tuple[str, list[list[str]]]]:
    """(label, rows) of the pipe tables in a text: the label is the line above the table ('**Inputs (VAR_INPUT)**');
    rows[0] is the header row."""
    out: list[tuple[str, list[list[str]]]] = []
    lines = text.split("\n")
    label = ""
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("|"):
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].startswith("|"):
                if not re.fullmatch(r"\|[\s\-|:]*\|?", lines[i].strip()):
                    rows.append([_md_cell(c) for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append((label, rows))
            label = ""
            continue
        if ln.strip():
            label = _md_cell(ln).strip(" :")
        i += 1
    return out


def _vars_of(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Name | (Data) type | Description rows → variables; [] when the table has no Name column."""
    if len(rows) < 2:
        return []
    hdr = [c.lower() for c in rows[0]]
    ni = next((k for k, h in enumerate(hdr) if "name" in h), None)
    ti = next((k for k, h in enumerate(hdr) if "type" in h), None)
    di = next((k for k, h in enumerate(hdr) if "description" in h or "comment" in h or "meaning" in h), None)
    if ni is None:
        return []
    out = []
    for r in rows[1:]:
        if ni < len(r) and re.fullmatch(r"[A-Za-z_]\w*", r[ni]):
            out.append({"name": r[ni], "type": (r[ti] if ti is not None and ti < len(r) else "") or None,
                        "default": None, "description": (r[di] if di is not None and di < len(r) else "") or None})
    return out


def _page_owner(title: str) -> str | None:
    """The symbol whose members a page documents in sections: 'Library Recipe Management - RecipeManCommands' →
    RecipeManCommands (the name after ' - '), else the page's own symbol."""
    m = re.search(r"\s[-–]\s*([A-Za-z_]\w*)\s*$", title or "")
    return m.group(1) if m else symbol_name(title or "")


def _var_lines(items: list[dict[str, Any]]) -> list[str]:
    w = max((len(x["name"]) for x in items), default=0)
    return [f"    {x['name'].ljust(w)} : {x['type'] or '(* type not given on the page *)'};"
            + (f"   // {x['description']}" if x.get("description") else "") for x in items]


def _prose_of(body: str) -> str:
    return " ".join(re.sub(r"(?m)^\|.*$|```.*?```", " ", body, flags=re.S).split())


def _constant_tables(markdown: str) -> list[tuple[str, str, list[list[str]]]]:
    """(heading, section text, rows) of the tables of IEC constants on a page: a header whose second column is a
    value ('Name | Initialization value | Comment', 'Port numbers | Value | Description'), at least 80 % of the rows
    NAME_X | IEC literal, and evidence that they are IEC — a value written 16# / 2# / 8# in such a table of the page,
    or the text naming the PLC / a GVL / global constants. Not a table in C notation (0x…: the ADS DLL, the CNC
    interfaces), not one the text calls an enumeration (the values of an enum type, not constants). The deepest
    section first: the nearest heading names the table."""
    found: list[tuple[str, str, list[list[str]], bool]] = []
    seen: set[tuple[str, ...]] = set()
    secs = sorted(_md_sections(markdown), key=lambda x: -x[0]) + [(0, "", markdown)]
    for _lvl, head, body in secs:
        part = body if head else re.split(r"(?m)^#{1,6}\s", markdown, maxsplit=1)[0]
        for label, rows in _md_tables(part):
            if len(rows) < 3 or len(rows[0]) < 2 or _CONST_NAME.match(rows[0][0]) or not _VALUE_HEAD.search(rows[0][1]):
                continue
            key = tuple(r[0] for r in rows)
            if key in seen:
                continue
            seen.add(key)
            data = rows[1:]
            if any(len(r) >= 2 and _C_VALUE.match(r[1]) for r in data):
                continue
            hits = [r for r in data if len(r) >= 2 and _CONST_NAME.match(r[0]) and _IEC_VALUE.match(r[1])]
            if len(hits) < 2 or len(hits) < 0.8 * len(data):
                continue
            text = _prose_of(body) + " " + label
            if re.search(r"\benumeration\b", text, re.I):
                continue
            found.append((head, text, hits, bool(_PLC_CONTEXT.search(text + " " + " ".join(rows[0])))))
    based = any(_IEC_BASED.match(r[1]) for _h, _t, hits, _c in found for r in hits)
    return [(h, t, hits) for h, t, hits, ctx in found if based or ctx]


def heading_symbols(title: str, markdown: str, kind: str | None = None) -> list[dict[str, Any]]:
    """N170: the symbols a page documents in its sections and tables, each {name, owner, kind, section, decl}:
      - a heading 'Method X' / 'Property X' / 'Function X' / 'Action X' / 'X (Method)' whose section tables its
        parameters (a table labelled VAR_INPUT / VAR_OUTPUT / VAR_IN_OUT / Inputs / Outputs) or its return value
        (a 'Return value' table): kind from the heading, the declaration reconstructed from the tables, owner the
        page's symbol (_page_owner). The owner itself when the page is not its page and its text says what it is
        ('the function block RecipeManCommands'): its methods, no VAR blocks (the page shows none);
      - a table of IEC constants (_constant_tables): kind Constant with its value, the type and the GVL when the
        text names them ('… in the GVL ReturnValues', '… of the data type UDINT'). Not on a type's page (kind Enum /
        Struct / Union / Alias: the rows are the type's values or components).
    Nothing is assumed: a type the table leaves empty stays unknown, an owner the text does not name a function
    block / interface / program gets no entry of its own."""
    md = markdown or ""
    if "|" not in md:
        return []
    own = (symbol_name(title or "") or "").lower()
    owner = _page_owner(title)
    out: list[dict[str, Any]] = []
    members: list[dict[str, Any]] = []
    for _lvl, head, body in _md_sections(md):
        m = _MEMBER_HEAD.match(head)
        if not m:
            continue
        kw = (m.group(1) or m.group(4)).capitalize()
        name = m.group(2) or m.group(3)
        if name.lower() == own:
            continue                                   # the page's own symbol ('FUNCTION F_GetVersionTcUtilities')
        ins: list[dict[str, Any]] = []
        outs: list[dict[str, Any]] = []
        inouts: list[dict[str, Any]] = []
        ret: list[dict[str, Any]] = []
        for label, rows in _md_tables(body):
            lab = label.lower()
            vs = _vars_of(rows)
            if not vs:
                continue
            if re.search(r"var_in_out|in[ /_-]?out|inputs?\s*/\s*outputs?", lab):
                inouts += vs
            elif re.search(r"var_output|\boutputs?\b", lab):
                outs += vs
            elif re.search(r"var_input|\binputs?\b", lab):
                ins += vs
            elif re.search(r"\breturn\s+values?\b", lab):
                ret += vs
        if not (ins or outs or inouts or ret):
            continue                                   # no IEC parameters tabled: not a symbol of the PLC
        rv = next((x for x in ret if x["name"].lower() == name.lower()), ret[0] if len(ret) == 1 else None)
        rtype = (rv or {}).get("type")
        hdr = {"Method": "METHOD", "Property": "PROPERTY", "Function": "FUNCTION", "Action": "ACTION"}[kw]
        text = [f"{hdr} {name}" + (f" : {rtype}" if rtype else "") + "   (* reconstructed from the page's tables *)"]
        for sec, items in (("VAR_INPUT", ins), ("VAR_OUTPUT", outs), ("VAR_IN_OUT", inouts)):
            if items:
                text += [sec, *_var_lines(items), "END_VAR"]
        prose = _prose_of(body)
        summary = re.split(r"(?<=[.!?])\s", prose, maxsplit=1)[0] if prose else None
        decl = {"name": name, "kind": kw, "declaration": "\n".join(text), "extends": None, "implements": None,
                "return_type": rtype, "inputs": ins, "outputs": outs, "in_out": inouts, "members": [], "enum_values": [],
                "description": {}, "section": head, "reconstructed": "from the page's tables (it shows no code)"}
        if rv and rv.get("description"):
            decl["return_description"] = rv["description"]
        if summary:
            decl["summary"] = summary
        out.append({"name": name, "owner": owner, "kind": kw, "section": head, "decl": decl})
        members.append({"name": name, "kind": kw, "description": summary})
    said = None
    if owner and members and own != owner.lower():
        words = " ".join(_text_of(md).split())
        said = next((k for w, k in _OWNER_KINDS
                     if re.search(rf"\b{w}\s+[\"'„“]?{re.escape(owner)}\b", words, re.I)), None)
    if said:
        out.append({"name": owner, "owner": None, "kind": said, "section": None, "decl": {
            "name": owner, "kind": said, "extends": None, "implements": None, "return_type": None,
            "declaration": f"{dict(_OWNER_KINDS_IEC)[said]} {owner}   (* methods only — the page shows no VAR blocks *)",
            "inputs": [], "outputs": [], "in_out": [], "members": [], "enum_values": [], "description": {},
            "methods": [{"name": x["name"], "definition_location": None, "description": x["description"]}
                        for x in members if x["kind"] == "Method"],
            "properties": [{"name": x["name"], "description": x["description"]} for x in members if x["kind"] == "Property"],
            "reconstructed": "from the page's method sections (it shows no code)"}})
    if kind not in ("Enum", "Struct", "Union", "Alias"):
        for head, text, hits in _constant_tables(md):
            gvl = re.search(r"\b(?:GVL|global variable list)\s+([A-Za-z_]\w*)", text)
            typ = re.search(r"\bdata\s+type\s+([A-Z][A-Z0-9_]*)\b", text)
            for r in hits:
                d = {"name": r[0], "kind": "Constant", "value": r[1], "type": typ.group(1) if typ else None,
                     "description": r[2] if len(r) > 2 and r[2] else None, "section": head or None,
                     "declaration": f"{r[0]}" + (f" : {typ.group(1)}" if typ else "") + f" := {r[1]};"}
                if gvl:
                    d["gvl"] = gvl.group(1)
                out.append({"name": r[0], "owner": gvl.group(1) if gvl else None, "kind": "Constant",
                            "section": head or None, "decl": d})
    seen: set[tuple[str, str]] = set()
    uniq = []
    for x in out:                              # a table under nested headings is read once
        k = (x["name"].lower(), (x["owner"] or "").lower())
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


_OWNER_KINDS_IEC = (("FunctionBlock", "FUNCTION_BLOCK"), ("Interface", "INTERFACE"), ("Program", "PROGRAM"))


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


# TwinCAT 2 supplement books the local package does not ship, so only the online search meets them (third review of
# 0.8.16: 'ts6100_tc2_opcua_configurator', 'ts6340_tc2_serial', 'tcdbserver', 'tcsmssmtpsrv'). Kept apart from
# TC2_BOOK_RES on purpose: that table is part of the Infosys index's format (tests/test_formats.py) and these books
# change nothing stored — they go into it with its next format change.
TC2_SUPPLEMENT_RE = re.compile(r"^ts\d+_tc2_|^tcdbserver$|^tcsmssmtpsrv$")


def is_tc2_supplement(book: str) -> bool:
    return bool(TC2_SUPPLEMENT_RE.search(book))


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
    # symbols:     the symbols a page documents in its sections and tables (heading_symbols, N170): a method of an FB
    #              whose page has no code, an FB known only by its methods, a constant of a constants table
    # Built from the stored pages — no page is parsed again; kept in step after every index run.
    _DERIVED_DDL = (
        "CREATE TABLE IF NOT EXISTS catalog(rowid INTEGER PRIMARY KEY, url TEXT, title TEXT, kind TEXT, library TEXT, "
        "book TEXT, fetched REAL)",
        "CREATE INDEX IF NOT EXISTS catalog_kind ON catalog(kind)",
        "CREATE TABLE IF NOT EXISTS code_blocks(id INTEGER PRIMARY KEY, page_rowid INTEGER, url TEXT, title TEXT, "
        "library TEXT, lang TEXT, code TEXT)",
        "CREATE INDEX IF NOT EXISTS code_blocks_page ON code_blocks(page_rowid)",
        "CREATE INDEX IF NOT EXISTS code_blocks_lang ON code_blocks(lang COLLATE NOCASE)",
        "CREATE TABLE IF NOT EXISTS code_src(page_rowid INTEGER PRIMARY KEY, url TEXT, fetched REAL)",
        "CREATE TABLE IF NOT EXISTS symbols(id INTEGER PRIMARY KEY, page_rowid INTEGER, name TEXT, owner TEXT, "
        "kind TEXT, section TEXT, decl TEXT)",
        "CREATE INDEX IF NOT EXISTS symbols_name ON symbols(name COLLATE NOCASE)",
        "CREATE INDEX IF NOT EXISTS symbols_page ON symbols(page_rowid)",
        "CREATE TABLE IF NOT EXISTS symbol_src(page_rowid INTEGER PRIMARY KEY, url TEXT, fetched REAL)",
    )

    def _init_derived(self, con: Any) -> None:
        for ddl in self._DERIVED_DDL:                  # con.execute: inside a transaction when one is open
            con.execute(ddl)

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
            if r is None or r[0].split("|", 1)[0] != str(DERIVED_VERSION):
                # another layout or rule: every derived row again — the passes below add the pages they have not seen
                # and kept every row the old rule made (a bump brought nothing in step). One transaction with the
                # rebuild (third review: executescript committed the DROP at once — a reader saw empty tables for
                # seconds and answered 'not found')
                con.execute("BEGIN IMMEDIATE")
                for t in ("catalog", "code_blocks", "code_src", "symbols", "symbol_src"):
                    con.execute(f"DROP TABLE IF EXISTS {t}")
                self._init_derived(con)
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
            # N170: the symbols documented inside the pages (a page with no table documents none)
            for (pr,) in con.execute("SELECT page_rowid FROM symbol_src WHERE NOT " + same.format(t="symbol_src", k="page_rowid")).fetchall():
                con.execute("DELETE FROM symbols WHERE page_rowid = ?", (pr,))
                con.execute("DELETE FROM symbol_src WHERE page_rowid = ?", (pr,))
            syms = 0
            for row in con.execute("SELECT rowid, url, title, kind, markdown, decl, fetched FROM pages WHERE rowid NOT IN "
                                   "(SELECT page_rowid FROM symbol_src)").fetchall():
                for x in heading_symbols(row["title"], row["markdown"] or "", row["kind"]) + declared_symbols(row):
                    con.execute("INSERT INTO symbols(page_rowid, name, owner, kind, section, decl) VALUES (?,?,?,?,?,?)",
                                (row["rowid"], x["name"], x["owner"], x["kind"], x["section"],
                                 json.dumps(x["decl"], ensure_ascii=False)))
                    syms += 1
                con.execute("INSERT OR REPLACE INTO symbol_src VALUES (?, ?, ?)", (row["rowid"], row["url"], row["fetched"]))
            con.execute("INSERT OR REPLACE INTO meta VALUES ('derived_sig', ?)", (f"{DERIVED_VERSION}|{sig}",))
        return {"changed": True, "pages_added": len(batch), "pages_removed": len(gone), "code_blocks_added": added,
                "symbols_added": syms, "seconds": round(time.time() - t0, 2)}

    def symbol_counts(self) -> dict[str, int]:
        """N170: {kind: count} of the symbols documented inside pages (the build log names them)."""
        with self._db() as con:
            if not has_symbols(con):
                return {}
            return {k: n for k, n in con.execute("SELECT kind, COUNT(*) FROM symbols GROUP BY kind ORDER BY kind")}

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
            # version, crumb: the Help Viewer source; words: format 5 (N105); toc_parent, parent: format 6 (N126)
            for c in ("version", "crumb", "words", "toc_parent", "parent"):
                if c not in cols:
                    con.execute(f"ALTER TABLE pages ADD COLUMN {c} TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS pages_parent ON pages(parent)")
            if "words" not in cols:
                con.executemany("UPDATE pages SET words = ? WHERE rowid = ?",
                                [(title_words(r[1]), r[0]) for r in con.execute("SELECT rowid, title FROM pages")])
            # external-content FTS: the index points at pages' own columns instead of keeping a second copy of every
            # page's text (≈ a third of the database). An older self-contained FTS table is replaced and rebuilt
            # from pages — no page has to be parsed again for that.
            try:
                row = con.execute("SELECT sql FROM sqlite_master WHERE name = 'pages_fts'").fetchone()
                if row is not None and ("content='pages'" not in (row[0] or "") or " words" not in (row[0] or "")):
                    con.execute("DROP TABLE pages_fts")     # self-contained, or without the words column (format 5)
                    row = None
                con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(url UNINDEXED, title, text, code, "
                            "words, content='pages', content_rowid='rowid', tokenize='unicode61')")
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
        words = title_words(title)              # the title's identifiers as words (format 5, N105)
        # the page's parent in the Help Viewer contents (format 6, N126): its help id now, its URL once every page is in
        toc_parent = (doc.meta.get("Microsoft.Help.TOCParent") or "").strip().lower() or None
        old = con.execute("SELECT rowid, url, title, text, code, words FROM pages WHERE url = ?", (url,)).fetchone()
        if old is not None:
            if self._fts:   # external content: the index entry is removed with the values it was built from
                con.execute("INSERT INTO pages_fts(pages_fts, rowid, url, title, text, code, words) "
                            "VALUES ('delete', ?, ?, ?, ?, ?, ?)", (old[0], old[1], old[2], old[3], old[4], old[5]))
            con.execute("DELETE FROM pages WHERE rowid = ?", (old[0],))
        cur = con.execute("INSERT INTO pages(url, book, page_id, lastmod, title, kind, library, markdown, text, code, "
                          "decl, fetched, version, crumb, words, toc_parent) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (url, bk[0], bk[1], lastmod, title, kind, library, doc.markdown, text, code,
                           json.dumps(decl) if has_decl or kind != "Page" else None, time.time(), version,
                           doc.crumb or None, words, toc_parent))
        if self._fts:
            con.execute("INSERT INTO pages_fts(rowid, url, title, text, code, words) VALUES (?,?,?,?,?,?)",
                        (cur.lastrowid, url, title, text, code, words))
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
            out = {"doc_date": self.local.describe()["modified"], "indexed_books_count": self._indexed_count()}
            note = self.index_note()
            if note:
                out["index_note"] = note          # rule 6: an answer from an index that is not the current one says so
            return out
        return {"indexed_books_count": self._indexed_count()}

    def _indexed_count(self) -> int:
        """Books in the index; 0 while a read-only index does not exist yet. A page converted from the package during
        the first build failed here with 'no Infosys index yet' after it had been converted (Windows CI of 88a221c,
        where the build had not finished when the first call came; on Linux it had)."""
        if self.read_only and self._file_sig() is None:
            return 0
        return len(self.indexed_books())

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
                    con.execute("INSERT INTO pages_fts(pages_fts, rowid, url, title, text, code, words) "
                                "SELECT 'delete', rowid, url, title, text, code, words FROM pages WHERE book = ?", (bk,))
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
        """The whole package is in the index, built by this indexer version. A reader (read-only, the server) asks
        infosys_build.state — the one rule every reader uses; the writer (a build's own .building) asks its progress."""
        if self.local is None:
            return False
        try:
            if self.read_only:
                from . import infosys_build as B
                return B.state(self.db_file, local=self.local)["state"] == "complete"
            return self.indexed_stamp() == self.local.content_stamp() and self._meta("indexer_version") == str(INDEXER_VERSION)
        except Exception:  # noqa: BLE001
            return False

    def _not_indexed_hint(self) -> str:
        """What to do when a lookup found nothing and the index may not hold everything."""
        if self.local is None:
            return "infosys_index(book=...) indexes a book first; infosys_books lists them"
        if self.read_only:
            note = self.index_note()
            return ("the index is not complete" + (f" ({note})" if note else "") + " — a rebuild runs only once the "
                    "user approves kb_rebuild(index='infosys')")
        return "the package is still being indexed"

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
        return st["message"] if B.problem(st) else None

    def _meta(self, key: str) -> str | None:
        with self._db() as con:
            r = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None

    def indexed_stamp(self) -> str | None:
        with self._db() as con:
            r = con.execute("SELECT value FROM meta WHERE key = 'source_stamp'").fetchone()
        return r["value"] if r else None

    def _pages_gen(self) -> str:
        """The pages table's generation (_bump_gen): a call that read the index and finds it changed reads again."""
        if self.read_only and not self.db_file.is_file():
            return ""
        with self._db() as con:
            r = con.execute("SELECT value FROM meta WHERE key = 'pages_gen'").fetchone()
        return str(r[0]) if r is not None else "0"

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
        # N161 (0.8.14 retest): 'FB_EcCoE*' found nothing — the '*' was a character of the name. A query with * or ?
        # is a glob on the symbol's name (as infosys_books takes them); without, a part of the name as before
        glob = bool(re.search(r"[*?]", q))
        like = "".join("%" if ch == "*" else "_" if ch == "?" else _like(ch) for ch in q) if glob else _like(q)
        where.append("(title LIKE ? ESCAPE '\\' COLLATE NOCASE)")
        params.append(f"%{like}%")
        # R18: the catalog (a few MB) instead of the page store (hundreds of MB, cold: 16.9 s); markdown only for the hits
        self.sync_derived()
        with self._db() as con:
            rows = con.execute(f"SELECT url, title, kind, library, book FROM catalog WHERE {' AND '.join(where)}",
                               params).fetchall()
            if glob:
                rows = [r for r in rows if fnmatch.fnmatchcase((symbol_name(r["title"]) or r["title"] or "").lower(),
                                                               q.lower())]
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

    def search(self, query: str, library: str | None = None, limit: int = 10, web_fallback: bool | None = None,
               offset: int = 0) -> dict[str, Any]:
        q = query.strip()
        if not q:
            raise InfosysError("empty query")
        # N167 (0.8.14 retest): hits 51… could not be reached — 'limit 300' was refused and there was no offset. A
        # limit above 50 is used as 50 and said so; offset pages through the hits (next_offset)
        clamped = None
        if isinstance(limit, int) and not isinstance(limit, bool) and limit > SEARCH_LIMIT:
            clamped, limit = limit, SEARCH_LIMIT
        _check_limit(limit, SEARCH_LIMIT)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise InfosysError(f"offset {offset!r} must be >= 0 — 0 is the first hit, next_offset the next page")
        last: dict[str, Any] | None = None
        if self._fts:
            items, last = self._fts_run(q, library, limit, offset)
            if not items and offset and last["total"]:
                raise InfosysError(f"offset {offset} is past the last hit ({last['total']} pages match) — offset=0 for "
                                   "the first ones")
        elif offset:
            raise InfosysError("offset needs the full-text index (FTS5), which this index has not")
        else:
            items = self._like_search(q, library, limit)
        backend = "local-fts5" if self._fts else "local-like"
        if not items and self.local is None and (self.web_fallback if web_fallback is None else web_fallback):
            items = self._ddg(q, limit)
            backend = "duckduckgo+sitemap"
        if self.local is not None:
            backend += "+helpviewer"
        res = {"query": q, "backend": backend, "count": len(items), "items": items, **self._answer_meta()}
        if last and backend.startswith("local"):     # (the web fallback's items are not of this search)
            res["query_mode"] = last["mode"]
            res["total"] = last["total"]              # pages; a page in several books once per book
            res["truncated"] = last["more"]
            res["offset"], res["next_offset"] = offset, last["next_offset"]
            if clamped is not None:
                res["clamped"] = True
                res["clamped_note"] = (f"limit {clamped} is above the maximum (1-{SEARCH_LIMIT}): {SEARCH_LIMIT} used"
                                       + (f" — the next hits: offset={last['next_offset']}" if last["next_offset"] else ""))
            for k in ("words_used", "words_ignored", "words_too_common"):
                if last["info"].get(k):
                    res[k] = last["info"][k]
            if last.get("code"):
                res["code_note"] = code_note(last)
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
                           if complete else "code search covers indexed pages only — " + self._not_indexed_hint())
        return res

    def code_languages(self) -> dict[str, int]:
        with self._db() as con:
            return {r[0]: r[1] for r in con.execute("SELECT lang, COUNT(*) FROM code_blocks GROUP BY lang")}

    def get_page(self, ref: str, refresh: bool = False, max_chars: int = 60_000, section: str | None = None,
                 offset: int = 0) -> dict[str, Any]:
        part_ranges(offset, max_chars)                    # R31 / N160: the ranges, before anything is read
        url, sym_section = self._resolve_part(ref)
        if section is None and sym_section:
            section = sym_section                          # N170: a method documented in a section of its FB's page
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
        # N160 (0.8.14 retest): the skill's docs_page reads the same part — an outline, one section, an offset
        got = page_part(row["markdown"] or "", section, offset, max_chars)
        out = {"url": url, "title": row["title"], "kind": row["kind"], "library": row["library"], "book": row["book"],
               "lastmod": row["lastmod"] or None, "in_sitemap": url in sm, "markdown": got["text"],
               **{k: got[k] for k in ("truncated", "chars", "offset", "next_offset", "section", "outline")}}
        if not indexed:
            out["indexed"] = False
        if indexed:
            out.update(self._family(url))
        if self.local is not None:
            out["lastmod"] = None
            out["version"] = row["version"]
            out["breadcrumb"] = row["crumb"]
            out.update(self._answer_meta())
        return out

    def _family(self, url: str, limit: int = 60) -> dict[str, Any]:
        """N126 (format 6): the page's parent in the Help Viewer contents and its child pages — an FB's page lists its
        method / property pages, a method page names its FB. {} for an index of an older format."""
        with self._db() as con:
            return page_family(con, url, limit)

    def declaration(self, symbol: str, library: str | None = None) -> dict[str, Any]:
        s = " ".join(symbol.split())
        lib = (library or "").strip().lower()

        def keep(r: Any) -> bool:
            return not lib or (r["library"] or "").lower() == lib or (r["book"] or "").lower() == lib

        def documents() -> tuple[list[list[Any]], list[list[Any]]]:
            with self._db() as con:
                every = symbol_documents(con, s)          # N100: the one rule of every reader
                docs = [g for g in ([r for r in g if keep(r)] for g in every) if g]
                if not docs and lib:
                    # review of 0.8.15: the library names the owner of a member it inherits (FB_BA_SeqCtrl.Calc of tf8040)
                    docs = symbol_documents(con, s, keep)
            return every, docs

        gen = self._pages_gen()
        every, docs = documents()
        if not docs:
            found = self.search_symbols(s, library=library, limit=5)    # indexes the default books first (web cache)
            if found.get("auto_indexed") or self._pages_gen() != gen:
                # Windows CI of 5c6f30f: the server's background indexer finished the default books between the first
                # read and search_symbols (which then indexed nothing) — the first, empty read was kept
                every, docs = documents()
        if not docs:
            # N170 (0.8.14 retest): no page and no section documents the symbol — a page with the name as a word of
            # its title ('Library Recipe Management - RecipeManCommands') answers as a page, with its outline
            with self._db() as con:
                titled = title_word_documents(con, s, keep)
                close = near_symbols(con, s, keep) if len(titled) != 1 else []
                onote = owner_note(con, s, keep)
            if len(titled) == 1:
                return self._title_answer(s, titled[0])
            complete = self.local is not None and self.index_complete()
            near = [f"{r['title']} ({r['url']})" for r in close]           # N159: the owner, where the member is from
            near += [it["name"] for it in found["items"] if not any(it["url"] == r["url"] for r in close)]
            raise InfosysError(f"no symbol page titled {s!r}" + (f" in {library}" if library else "") +
                               (f"; near: {near}" if near else
                                (" in the complete index" if complete else "; " + self._not_indexed_hint()))
                               # N160 (0.8.14 retest): the way on, as the skill's not_found gives it
                               + (f" — {onote}" if onote else f" — infosys_search(query={s!r}) finds pages by their text"))
        if len(docs) > 1:
            # R23 (0.8.8 retest): MC_MoveAbsolute exists in Tc2_MC2 and Tc3_DriveMotionControl with different
            # parameters — returning the first one let code be written against the wrong FB. Nothing is chosen. One
            # page in several books, or one library's declaration on several pages, is one document (N100).
            cands = [f"{g[0]['library'] or g[0]['book']} ({g[0]['url']})" for g in docs]
            libs = {(g[0]["library"] or "").lower() for g in docs}
            where = (f"in {len(docs)} libraries" if len(libs) == len(docs) and "" not in libs
                     else f"by {len(docs)} different pages")
            raise InfosysError(f"ambiguous: {s} is documented {where}: {cands} — nothing was chosen. Pass library= "
                               "with the one the project uses (proj_libraries lists them) and say which one you used"
                               + ("" if len(libs) == len(docs) else "; pages of one library: open the one meant with "
                                  "infosys_get_page(<url>)") + ".")
        row = docs[0][0]
        decl = json.loads(row["decl"]) if row["decl"] else {"name": row["title"], "kind": row["kind"], "declaration": None}
        if decl.get("syntax_header") and not is_symbol_row(row):
            with self._db() as con:
                decl = header_answer(con, row, decl)       # third review of 0.8.15: the one rule of every reader
        decl.update({"url": row["url"], "library": row["library"]})
        if is_symbol_row(row):
            # N170: documented in a section or table of a page — the page, the section, the FB of a method
            decl["page_title"] = row["page_title"]
            if row["owner"] and row["kind"] != "Constant":
                decl["parent"] = {"title": row["owner"], "url": row["url"]}
                decl.update(defined_in(s, row["owner"]) or {})
            if len(docs[0]) > 1:
                decl["documented_on"] = [{"title": r["page_title"], "url": r["url"]} for r in docs[0]]
        else:
            with self._db() as con:
                fam = page_family(con, row["url"], limit=0)
            if fam.get("parent"):
                decl["parent"] = fam["parent"]            # N126: a method's FB, from the contents (format 6)
                decl.update(defined_in(s, fam["parent"]["title"]) or {})
        chosen = {r["rid"] for r in docs[0]}
        mine = (row["library"] or "").lower()
        others: dict[str, str] = {}
        for g in every:
            for r in g:
                if r["rid"] not in chosen and r["library"] and r["library"].lower() != mine:
                    others.setdefault(r["library"].lower(), r["library"])
        if others:
            decl["also_in"] = sorted(others.values())     # the other libraries with a symbol of this name
        copies = [r["book"] for r in docs[0][1:] if r["book"] != row["book"] or not is_symbol_row(r)]
        if copies:
            decl["same_page_in"] = list(dict.fromkeys(copies))   # the same document in other books (N100)
        if self.local is not None:
            with self._db() as con:
                v = con.execute("SELECT version FROM pages WHERE url = ?", (row["url"],)).fetchone()
            if v and v["version"]:
                decl["doc_version"] = v["version"]
            decl.update(self._answer_meta())
        return decl

    def _title_answer(self, name: str, group: list[Any]) -> dict[str, Any]:
        """N170: the answer for a name no symbol page and no section documents, when one page has it as a word of its
        title: that page, kind Page, match 'title', its outline."""
        row = group[0]
        out: dict[str, Any] = {"name": name, "title": row["title"], "url": row["url"], "library": row["library"],
                               "declaration": None,
                               **title_word_answer(name, row["body"] or "", "infosys_get_page(<url>)")}
        if len(group) > 1:
            out["same_page_in"] = list(dict.fromkeys(r["book"] for r in group[1:]))
        if self.local is not None:
            if row["version"]:
                out["doc_version"] = row["version"]
            out.update(self._answer_meta())
        return out

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
            rows = con.execute("SELECT rowid, url, title, markdown, text, code, decl, words FROM pages WHERE "
                               "markdown LIKE '%ms-xhelp%' OR decl LIKE '%ms-xhelp%'").fetchall()
            for r in rows:
                md = _XHELP.sub(sub, r["markdown"] or "")
                decl = _XHELP.sub(sub, r["decl"]) if r["decl"] else r["decl"]
                if md == (r["markdown"] or "") and decl == r["decl"]:
                    continue
                text = _text_of(md)
                if self._fts:
                    con.execute("INSERT INTO pages_fts(pages_fts, rowid, url, title, text, code, words) VALUES "
                                "('delete', ?, ?, ?, ?, ?, ?)", (r["rowid"], r["url"], r["title"], r["text"], r["code"],
                                                                 r["words"]))
                con.execute("UPDATE pages SET markdown = ?, text = ?, decl = ? WHERE rowid = ?",
                            (md, text, decl, r["rowid"]))
                if self._fts:
                    con.execute("INSERT INTO pages_fts(rowid, url, title, text, code, words) VALUES (?,?,?,?,?,?)",
                                (r["rowid"], r["url"], r["title"], text, r["code"], r["words"]))
                pages += 1
            # N126 (format 6): each page's parent in the contents, by the help id its TOCParent names
            con.execute("UPDATE pages SET parent = (SELECT h.url FROM helpids h WHERE h.help_id = pages.toc_parent) "
                        "WHERE toc_parent IS NOT NULL")
            parents = con.execute("SELECT COUNT(*) FROM pages WHERE parent IS NOT NULL").fetchone()[0]
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)",
                            [("links", str(found)), ("links_resolved", str(resolved)), ("parents", str(parents))])
        return {"pages": pages, "links": found, "resolved": resolved, "help_ids": len(ids), "parents": parents,
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
    def _resolve_part(self, ref: str) -> tuple[str, str | None]:
        """resolve(), and the section of the page when the name is a symbol documented in one (N170)."""
        url = self.resolve(ref)
        r = " ".join(ref.split())
        if re.fullmatch(r"[A-Za-z_][\w.]*", r) and self.db_file.is_file():
            try:
                with self._db() as con:
                    if not con.execute("SELECT 1 FROM pages WHERE url = ? AND title = ? COLLATE NOCASE", (url, r)).fetchone():
                        for x in symbol_rows(con, r):
                            if x["url"] == url and x["section"]:
                                return url, x["section"]
            except (sqlite3.Error, InfosysError):
                pass
        return url, None

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
        # a book slug as the package names it ('tc3_ads.net', 'tf5110-tf5113_tc3_kinematic_transformation': 4437 pages)
        m = re.fullmatch(r"([a-z0-9_][a-z0-9_.\-]*)/([a-z0-9_][a-z0-9_\-]*)(?:\.html?)?", r, re.I)
        if m and m.group(2).isdigit():
            return f"{CONTENT_PREFIX}{m.group(1)}/{m.group(2)}.html"
        if m:
            # a page id is the page's file name — a number, 'index', '6778772363_30967009', 'beckhoff-0cad-46d4-…'
            # (1169 of 51,655 pages);
            # one that is not a number is a page when the index or the package has it, else a title ('Send/Receive')
            url = f"{CONTENT_PREFIX}{m.group(1).lower()}/{m.group(2)}.html"
            with self._db() as con:
                stored = con.execute("SELECT 1 FROM pages WHERE url = ?", (url,)).fetchone() is not None
            if stored or (self.local is not None and url in self.sitemap()):
                return url
        if re.fullmatch(r"\d{6,}", r):
            hits = [u for u in self.sitemap() if u.endswith(f"/{r}.html")]
            if hits:
                return hits[0]
            raise InfosysError(f"no page id {r} in the " + ("local Help Viewer package" if self.local is not None
                                                             else "Infosys sitemap"))
        # a symbol name, else a page title — the rule of every reader (N100)
        gen = self._pages_gen()
        with self._db() as con:
            docs = title_documents(con, r)
        if not docs and (self.search_symbols(r, limit=3).get("auto_indexed")     # the default books, indexed first
                         or self._pages_gen() != gen):                         # or by the background indexer meanwhile
            with self._db() as con:
                docs = title_documents(con, r)
        if len(docs) == 1:
            return docs[0][0]["url"]
        if docs:
            raise InfosysError(f"ambiguous: {len(docs)} different pages are titled {r!r}: "
                               f"{[g[0]['url'] for g in docs][:20]} — nothing was chosen; pass the URL or 'book/id' of "
                               "the one meant (for a symbol: the library the project uses, proj_libraries)")
        # N160 (0.8.14 retest): the skill's not_found names what is near and how to search; this said less
        with self._db() as con:
            close = near_symbols(con, r) if re.fullmatch(r"[A-Za-z_][\w.]*", r) else []
        found = self.search_symbols(r, limit=3)
        near = [f"{x['title']} ({x['url']})" for x in close]
        near += [it["name"] for it in found["items"] if not any(it["url"] == x["url"] for x in close)]
        raise InfosysError(f"{ref!r} is neither an Infosys URL, 'book/id', a page id nor an indexed symbol name or "
                           "page title" + (f"; near: {near}" if near else "")
                           + f" — infosys_search(query={r!r}) finds pages by their text")

    # -- internals -------------------------------------------------------------------
    def _fts_search(self, q: str, library: str | None, limit: int) -> list[dict[str, Any]]:
        return self._fts_run(q, library, limit)[0]

    def _fts_run(self, q: str, library: str | None, limit: int, offset: int = 0) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The index's one search (search_index — the twincat-docs skill's docs_search is the same): tiers for a
        question in words, "quoted phrases" kept (R29), identifiers as words, titles first, an error code's table
        first, a page in several books once. -> (items, search_index's answer)."""
        from . import docsearch as DS
        try:
            with self._db() as con:
                res = search_index(con, q, limit, where="p.library = ? COLLATE NOCASE OR p.book = ? COLLATE NOCASE"
                                   if library else "", extra=[library, library] if library else [], snippet_tokens=24,
                                   offset=offset)
        except DS.SearchError as exc:
            if exc.code == "empty_query":
                raise InfosysError(exc.message) from None
            raise InfosysError(f"search syntax: {exc.message}") from None
        out = []
        for r in res["rows"]:
            it = {"url": r["url"], "title": r["title"], "kind": r["kind"], "library": r["library"], "book": r["book"],
                  **({"version": r["version"]} if r["version"] else {}),
                  "excerpt": r["s"], "score": round(-r["r"], 3)}
            if r["rid"] in res["copies"]:
                it["same_page_in"] = list(dict.fromkeys(b for b, _pid in res["copies"][r["rid"]]))   # a book once
            out.append(it)
        return out, res

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

    def web_search(self, q: str, limit: int = 5) -> list[dict[str, Any]]:
        """A site search of infosys.beckhoff.com on the web (DuckDuckGo's HTML page) — err_lookup's online step (0.8.16,
        user decision: when the local documentation has nothing). The search engine's results as they are: url, title,
        its snippet, and whether the page is in the local Help Viewer package (then infosys_get_page opens it; otherwise
        only a browser does — no page is fetched from the web in local mode). TwinCAT 2 books are left out (tc3_only).
        [{'error': …}] when the search cannot be reached."""
        url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus(f"site:infosys.beckhoff.com {q}")
        try:
            # third review of 0.8.16: its own short timeout — the index's fetcher waits 20 s twice, and the thread a
            # caller gave up on kept running that long
            fetch = self.fetch if not isinstance(self.fetch, Fetcher) else Fetcher(min(self.fetch.timeout_s, 5.0))
            raw = fetch.get(url, allow_hosts=DDG_HOSTS).decode("utf-8", errors="replace")
        except InfosysError as exc:
            return [{"error": f"the online search is not reachable: {exc}"}]
        local: dict[str, str] = {}
        if self.local is not None:
            try:
                local = self.sitemap()
            except InfosysError:
                local = {}

        def plain(h: str) -> str:
            return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", h))).strip()
        # one result block each (title link and its own snippet) — re-review of 0.8.16: paired by position, a result
        # without a snippet gave every later one the snippet of the one before it
        blocks = re.split(r'(?=<div\b[^>]*class="[^"]*\bresult\b)', raw)
        out: list[dict[str, Any]] = []
        for blk in blocks:
            t = re.search(r'<a\b([^>]*class="[^"]*\bresult__a\b[^"]*"[^>]*)>(.*?)</a>', blk, re.S)
            if not t:
                continue
            m = re.search(r'href="([^"]+)"', t.group(1))
            if not m:
                continue
            href = html.unescape(m.group(1))
            if "uddg=" in href:
                href = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg", [""])[0])
            href = re.sub(r"\?.*$", "", href.replace("http://", "https://").split("#", 1)[0])
            # re-review of 0.8.16: a page of no known book (another language, TwinCAT 2's old '/html/…htm' form) passed —
            # only English content pages of a book this server allows (tc3_only), as the sitemap check of _ddg does
            if f"infosys.beckhoff.com/content/{LANG}/" not in href:
                continue
            b = book_of(href)
            if not b or not self.book_allowed(b[0]) or is_tc2_supplement(b[0]):
                continue
            if any(o["url"] == href for o in out):
                continue
            sn = re.search(r'<(a|td|div)\b[^>]*class="[^"]*\bresult__snippet\b[^"]*"[^>]*>(.*?)</\1>', blk, re.S)
            out.append({"url": href, "title": plain(t.group(2)), "snippet": plain(sn.group(2)) if sn else None,
                        "book": b[0], "in_local_package": href in local if self.local is not None else None})
            if len(out) >= limit:
                break
        return out

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
