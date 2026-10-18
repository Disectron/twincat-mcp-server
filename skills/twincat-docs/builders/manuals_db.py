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
D:\\TwinCAT-MCP\\knowledge. PDFs: <kb>\\manuals (--dir), database: <kb>\\index\\manuals.db (--db) — the layout of
docs/KNOWLEDGE_BASE.md in the twincat plugin. `update` writes <db>.building and replaces the index only when it is
complete (the old one stays in use until then; an interrupted update replaces nothing).

Version 2 (2026-10-18), changes against version 1:
  - refuses to run below Python 3.12 with a message (the file compiles on older versions, so the message is shown)
  - knowledge folder found like the other knowledge tools (--kb, TCMCP_KNOWLEDGE_DIR, kb_set_location)
  - one update at a time (<db>.lock with a heartbeat); state and progress in a meta table; `stats` shows it
  - `search --doc` filters before the row limit (it used to filter the first limit × 10 hits only)
Version 3 (2026-10-18): page text format 2 — running headers, footers and margin labels (lines at the top or bottom
  of a page that repeat on many pages) are removed from the page text, also where glued to a heading; a heading
  wrapped over two lines is one toc entry. An index of format 1 is reported as outdated and read again on update.
Version 4 (2026-10-18): page text format 3 — running headers, footers and margin labels are found by their place on
  the page (same baseline near the top or bottom edge on most pages, set apart from the body), not by repeated text:
  section titles in the header, footers whose page number comes first, and a header on a heading's baseline are
  removed; table header rows repeated at the top of continuation pages stay. The font-heading toc is read without
  them. The text-repetition rule remains only for a backend without text positions. An index of format 1 or 2 is
  reported as outdated and read again on update.
Version 5 (0.8.11): page text format 4 — invisible text (font size 0, fully transparent) is not page text: Beckhoff
  puts a size-0 copy of the running header under every heading, which doubled headings and toc titles (N37). The
  index lives in <kb>\\index\\manuals.db (moved there once from manualsdb\\); an update builds <db>.building from a copy
  of the index and swaps it in when complete. `stats` is the twincat-docs skill's state (one implementation).
Version 6 (0.8.12): page text format 5 — text drawn below 1 pt is not page text: the hidden copy of the running
  header under each heading is drawn at 0.003 pt, not at 0, so format 4 still doubled headings and toc titles (N37).
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
import math  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parents[2]                 # <plugin>/skills/twincat-docs/builders → <plugin>
if "twincat_mcp" not in sys.modules and (PLUGIN_ROOT / "twincat_mcp" / "kb_layout.py").is_file():
    sys.path.insert(0, str(PLUGIN_ROOT))
try:
    from twincat_mcp import __version__ as PLUGIN_VERSION, kb_layout as L, kb_lock as L_lock  # noqa: E402
except ImportError:                 # a copy of this file outside the plugin folder: it needs the plugin beside it
    sys.exit("manuals_db.py runs from the twincat plugin's folder (it uses the plugin's twincat_mcp package and the "
             "twincat-docs skill beside it): run <kb>\\tools\\manuals_db.py (the stub, which finds the installed plugin) or "
             "<plugin>\\skills\\twincat-docs\\builders\\manuals_db.py — not a copy of it")

BUILDER = "manuals_db.py"
BUILDER_VERSION = "6"
TEXT_FORMAT = "5"                # page text / toc rules; 5: text drawn below 1 pt is not page text (N37: Beckhoff's
                                 # hidden header copy is drawn at 0.003 pt, not 0 — format 4 kept it).
                                 # An index of another format stays in use, reported 'rebuild_needed', until `update`
                                 # (or kb_rebuild('manuals')) reads every manual again. tests/test_formats.py fingerprints
                                 # the code that makes the text and fails when it changes without a new format
FALLBACK_KB = Path(r"D:\TwinCAT-MCP\knowledge")


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
    # the folder above <kb>\\tools\\<this script> — or above the builder stub in <kb>\\tools\\ that runs this script
    up = Path(os.environ.get("TCMCP_STUB_DIR") or HERE).resolve().parent
    if L.is_knowledge_folder(up):
        return up, "folder above the script"
    return FALLBACK_KB, "default"


def _db(args) -> Path:
    return Path(args.db) if args.db else L.locate(kb_root(args.kb)[0], "manuals")


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
class Span:
    """One piece of text with its place on the page: baseline y from the TOP of the page (pt), x, font size, bold,
    and the PDF backend's line it belongs to (PyMuPDF block/line; pypdf: one text operation)."""
    __slots__ = ("y", "x", "size", "bold", "text", "line", "blank")

    def __init__(self, y: float, x: float, size: float, bold: bool, text: str, line: tuple):
        self.y, self.x, self.size, self.bold, self.text, self.line = y, x, size, bold, text, line
        self.blank = not text.strip()     # a space in its own font run: part of the text, not of the layout


MIN_TEXT_PT = 1.0       # below 1 pt (0.35 mm) no printed text can be read


def _visible(size) -> bool:
    """N37: Beckhoff PDFs carry under every heading a copy of the running header drawn at a vanishing size ('4.4
    Calibration and compensation' + '| Calibration and compensation' at 0.0028 pt in EL3356-0010.pdf), which doubled
    headings and TOC titles. Format 4 dropped size 0 only — the real copy is 0.003 pt, so it stayed. Text drawn below
    MIN_TEXT_PT is not page text (format 5). Invisible text of a real size stays: it is how a scanned PDF carries its
    OCR text layer (render mode 3 — PyMuPDF reports it with alpha 0), the only text such a manual has."""
    try:
        return size is None or float(size) >= MIN_TEXT_PT
    except (TypeError, ValueError):
        return True


def _spans_pymupdf(page) -> tuple[list[Span], float]:
    out: list[Span] = []
    for bi, b in enumerate(page.get_text("dict").get("blocks", [])):
        for li, ln in enumerate(b.get("lines", [])):
            for s in ln.get("spans", []):
                if not s["text"] or not _visible(s.get("size")):
                    continue
                # a whitespace-only span is kept: a space set in another font than the words around it ('bold
                # regular-space bold') is the only space between them — dropping it glued 'Note that' into 'Notethat'.
                # It takes no part in the layout statistics (Span.blank)
                ox, oy = s.get("origin") or (s["bbox"][0], s["bbox"][3])
                out.append(Span(round(oy, 1), round(ox, 1), round(s["size"], 1), bool(s["flags"] & 16), s["text"],
                                (bi, li)))
    return out, float(page.rect.height)


def _spans_pypdf(page) -> tuple[list[Span], float]:
    """pypdf reports every text operation to a visitor with its text matrix; joining the visited pieces gives exactly
    extract_text(), so the page text is rebuilt from the pieces that are not a running header or footer."""
    height = float(page.mediabox.height)
    out: list[Span] = []

    def visit(text, cm, tm, font_dict, font_size):
        if not text:
            return
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        # the drawn size: the font size times the length of the text space's vertical unit in device space (any
        # rotation; a matrix that collapses it draws nothing — size 0, N37)
        size = float(font_size or 0) * math.hypot(tm[2] * cm[0] + tm[3] * cm[2], tm[2] * cm[1] + tm[3] * cm[3])
        if not _visible(size):
            return
        try:
            base = str(font_dict.get("/BaseFont", "")) if font_dict is not None else ""
        except Exception:  # noqa: BLE001
            base = ""
        out.append(Span(round(height - y, 1), round(x, 1), round(size, 1),
                        "bold" in base.lower(), text, (len(out),)))
    page.extract_text(visitor_text=visit)
    return out, height


def _running(pages: list[tuple[list[Span], float]]) -> list[set[int]]:
    """N37/N66 (format 3): running headers, footers and margin labels are found by their PLACE, not by their text —
    Beckhoff prints the current section title at the top of every page ('| Signal flow diagram', a different text in
    every section) and a footer whose page number comes first on some pages ('56 Version: 4.8.0 EL3356-00x0').
    A span is running when, near the top or bottom edge (outer 15 % of the page), text of the same size sits on the
    same baseline (within 2 pt) on at least 40 % of the pages with text (and 3 pages), and that baseline is set apart
    from the body: the nearest line towards the middle of the page is at least 1.6 line pitches away on most of those
    pages. The body's first line and a table header row repeated at the top of continuation pages are followed by the
    next line at one pitch, so they stay. A heading that happens to share the header's baseline has another size and
    stays. -> per page, the indexes of the running spans."""
    import statistics
    text_pages = [i for i, (sp, _h) in enumerate(pages) if sp]
    need = max(3, -(-len(text_pages) * 2 // 5))
    drops: list[set[int]] = [set() for _ in pages]
    if len(text_pages) < 3:
        return drops
    pitches = []
    for sp, _h in pages:
        ys = sorted({round(s.y) for s in sp if not s.blank})
        pitches += [b - a for a, b in zip(ys, ys[1:]) if 2 <= b - a <= 40]
    pitch = statistics.median(pitches) if pitches else 12.0
    apart = max(1.6 * pitch, 6.0)
    # candidates: (side, size) -> baselines on every page, clustered within 2 pt
    cand: dict[tuple, list[tuple[float, int, int]]] = {}
    for pi, (sp, h) in enumerate(pages):
        for si, s in enumerate(sp):
            if s.blank:
                continue
            side = "top" if s.y < 0.15 * h else "bottom" if s.y > 0.85 * h else None
            if side:
                cand.setdefault((side, round(s.size * 2) / 2), []).append((s.y, pi, si))
    clusters: list[tuple[str, list[tuple[float, int, int]]]] = []
    for (side, _size), items in cand.items():
        items.sort()
        group: list[tuple[float, int, int]] = []
        for it in items:
            if group and it[0] - group[0][0] > 2.0:
                clusters.append((side, group))
                group = []
            group.append(it)
        if group:
            clusters.append((side, group))
    frequent = [c for c in clusters if len({pi for _y, pi, _si in c[1]}) >= need]
    # from the body outwards: a cluster that passed is transparent for the clusters further out (a header of two
    # lines); one that failed (the body's first lines) is not — the lines behind it stay too
    frequent.sort(key=lambda c: (c[0], -c[1][0][0] if c[0] == "top" else c[1][0][0]))
    passed: set[tuple[int, int]] = set()
    for side, group in frequent:
        pages_in = {pi for _y, pi, _si in group}
        mine = {(pi, si) for _y, pi, si in group}
        ok = 0
        for pi in pages_in:
            sp, _h = pages[pi]
            ys = [y for y, p_, _si in group if p_ == pi]
            yc = sum(ys) / len(ys)
            others = [s.y for si, s in enumerate(sp)
                      if not s.blank and (pi, si) not in passed and (pi, si) not in mine and abs(s.y - yc) > 2.0]
            towards = [y - yc for y in others if y > yc] if side == "top" else [yc - y for y in others if y < yc]
            if not towards or min(towards) >= apart:
                ok += 1
        if ok >= 0.6 * len(pages_in):
            passed |= mine
            for _y, pi, si in group:
                drops[pi].add(si)
    return drops


def _page_text(spans: list[Span], drop: set[int], by_line: bool) -> str:
    """The page text without the running spans. PyMuPDF (by_line): spans of one line joined as they are, lines on the
    same baseline side by side in x order (as get_text('text', sort=True) does), rows top-down. pypdf: the visited
    pieces in order (they carry the line breaks)."""
    keep = [s for i, s in enumerate(spans) if i not in drop]
    if not by_line:
        return "".join(s.text for s in keep)
    lines: dict[tuple, list[Span]] = {}
    for s in keep:
        lines.setdefault(s.line, []).append(s)
    # a line is kept for its words: the spaces left over from a running header's line are not a line
    lines = {k: ln for k, ln in lines.items() if any(not x.blank for x in ln)}
    items = sorted(((min(x.y for x in ln), min(x.x for x in ln), "".join(x.text for x in sorted(ln, key=lambda z: z.x)))
                    for ln in lines.values()), key=lambda t: (t[0], t[1]))
    rows: list[list[tuple[float, float, str]]] = []
    for it in items:
        if rows and abs(it[0] - rows[-1][0][0]) <= 2.0:
            rows[-1].append(it)
        else:
            rows.append([it])
    return "\n".join(" ".join(t for _y, _x, t in sorted(r, key=lambda z: z[1])) for r in rows)


def _open_pdf(path: Path):
    """-> (backend name, page count, toc [(level, title, page)], page texts without running headers/footers, title,
    number of running spans removed — None when the backend gave no positions)."""
    try:
        try:
            import pymupdf
        except ImportError:
            import fitz as pymupdf  # PyMuPDF < 1.24
        d = pymupdf.open(str(path))
        pages = [_spans_pymupdf(p) for p in d]
        drops = _running(pages)
        toc = [(lvl, title.strip(), pg) for lvl, title, pg in d.get_toc(simple=True)]
        backend = "pymupdf"
        if not toc:
            toc = _toc_from_fonts(pages, drops)
            backend = "pymupdf+font-headings"
        meta_title = (d.metadata or {}).get("title") or ""
        if not meta_title:   # first large line on page 1 (Beckhoff: "Manual | EN" … product name)
            meta_title = _first_title(d)
        texts = [_page_text(sp, dr, True) for (sp, _h), dr in zip(pages, drops)]
        return backend, d.page_count, toc, texts, meta_title, sum(len(x) for x in drops)
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
    try:
        pages = [_spans_pypdf(p) for p in r.pages]
    except Exception:  # noqa: BLE001 — no positions from this pypdf: the text-repetition fallback in index_pdf
        return "pypdf", len(r.pages), toc, [(p.extract_text() or "") for p in r.pages], meta_title, None
    drops = _running(pages)
    texts = [_page_text(sp, dr, False) for (sp, _h), dr in zip(pages, drops)]
    return "pypdf", len(r.pages), toc, texts, meta_title, sum(len(x) for x in drops)


_NUM = re.compile(r"^\d+(?:\.\d+)*\.?$")
_GLUED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s*([A-ZÄÖÜ].*)$")


def _toc_from_fonts(pages: list[tuple[list[Span], float]], drops: list[set[int]]) -> list[tuple[int, str, int]]:
    """Chapter headings for PDFs without bookmarks: bold lines >= 1.5 x the body font size, read without the running
    header/footer spans (a header on the heading's baseline was glued into the title: '1.1 Product overview| Product
    overview'). A line holding only a chapter number ('4.2') is joined with the heading that follows; the level comes
    from the number's depth, else from the font-size rank."""
    from collections import Counter
    sizes: Counter = Counter()
    lines_per_page = []
    for (sp, _h), dr in zip(pages, drops):
        by_line: dict[tuple, list[Span]] = {}
        for i, s in enumerate(sp):
            if i not in dr:
                by_line.setdefault(s.line, []).append(s)
        lines = []
        for key in sorted(by_line, key=lambda k: (min(x.y for x in by_line[k]), min(x.x for x in by_line[k]))):
            spans = sorted(by_line[key], key=lambda z: z.x)
            txt = "".join(s.text for s in spans).strip()
            if not txt:
                continue
            words = [s for s in spans if not s.blank]
            size = max(s.size for s in words)
            bold = any(s.bold for s in words)
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
        last = None                                           # index in raw of the heading on the line just above
        for size, bold, txt in lines:
            if size not in rank or not bold:
                pending = last = None
                continue
            if _NUM.match(txt):
                pending = txt.rstrip(".")
                last = None
                continue
            if len(txt) > 120:
                pending = last = None
                continue
            num = pending
            glued = _GLUED.match(txt)                         # '10Error handling' (number and title in one span)
            if num is None and glued:
                num, txt = glued.group(1), glued.group(2)
            if num is None and last is not None and raw[last][2] == rank[size]:
                n0, t0, r0, p0 = raw[last]                    # N37: the heading wrapped onto a second line
                raw[last] = (n0, _join_wrapped(t0, txt), r0, p0)
                continue
            raw.append((num, txt, rank[size], pg))
            last = len(raw) - 1
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


def _join_wrapped(head: str, tail: str) -> str:
    if head.endswith("-") and tail[:1].islower():             # 'Brücken-' + 'kapazität'
        return head[:-1] + tail
    return f"{head} {tail}"


_TOC_NUM = re.compile(r"^\d+(?:\.\d+)*\.?\s")


def _mend_toc(toc: list[tuple[int, str, int]]) -> list[tuple[int, str, int]]:
    """N37: a heading wrapped over two lines became two chapters — '4.10 Notes on the measurement of the bridge' and a
    level-1 'capacity' (also from bookmarks generated per line). In a numbered manual an unnumbered entry on the same
    page as the entry before it is that entry's second line when it starts in lower case or with a closing bracket,
    or when the entry before ends mid-phrase (hyphen, comma, '(', 'and', 'of' …)."""
    if not any(_TOC_NUM.match(t) for _, t, _ in toc):
        return toc
    out: list[tuple[int, str, int]] = []
    for lvl, t, pg in toc:
        if out and not _TOC_NUM.match(t) and t and pg == out[-1][2]:
            prev = out[-1][1].rstrip()
            if (t[:1].islower() or t[:1] in ")]}" or prev.endswith(("-", ",", "(", "/", "&"))
                    or re.search(r"\b(?:and|or|of|the|for|with|to|in|on|und|oder|der|die|das|des|für|mit|zur|zum|von)$", prev, re.I)):
                out[-1] = (out[-1][0], _join_wrapped(prev, t.strip()), pg)
                continue
        out.append((lvl, t, pg))
    return out


_SIGNAL_WORDS = {"note", "notice", "caution", "warning", "danger", "attention", "hinweis", "vorsicht", "warnung", "gefahr",
                 "achtung", "tip", "example", "beispiel"}
_HEAD_LINES = 3                       # lines at the top and at the bottom of a page that can be a running header/footer


def _norm_line(s: str) -> str:
    return re.sub(r"\d+", "#", " ".join(s.split())).strip().lower()


def _page_number_like(occ: list[tuple[int, tuple[int, ...]]], tolerance: int) -> bool:
    """occ = (page index, numbers in the line) per page: True when the numbers are the same on every page, or when
    exactly one position k changes with the page (number_k - page index constant) and the rest stay."""
    if len({nums for _pg, nums in occ}) <= tolerance:
        return True
    width = {len(nums) for _pg, nums in occ}
    if len(width) != 1:
        return False
    for k in range(width.pop()):
        if len({nums[:k] + (nums[k] - pg,) + nums[k + 1:] for pg, nums in occ}) <= tolerance:
            return True
    return False


def _strip_running(pages: list[str], chapter_titles: set[str],
                   headings: frozenset[str] = frozenset()) -> tuple[list[str], list[str]]:
    """FALLBACK only — used when the PDF backend gives no text positions (the rule by position is _running).
    N37: running headers, footers and margin labels ('Commissioning', 'EL3356 Version: 2.4  79') were part of the
    page text and glued to headings ('Calibration and compensation4.4 Calibration'). A line among the first or last
    three of a page whose text (digits as '#') repeats there on 3 pages and 5 % of the pages — or on 2 when it is a
    chapter title — is removed (signal words like NOTE / WARNING are never); such a header glued to the start of a line is cut off when
    no space separates it and the rest starts with a digit or a capital, or when a section number follows; glued to
    the end of a line without a space, after a digit or punctuation, it is cut off too. -> (pages, the header texts
    removed)"""
    from collections import Counter
    split = [[ln for ln in t.split("\n")] for t in pages]
    edge = lambda ls: [i for i, ln in enumerate(ls) if ln.strip()][:_HEAD_LINES] + \
        [i for i, ln in enumerate(ls) if ln.strip()][-_HEAD_LINES:]            # noqa: E731
    seen: Counter = Counter()
    nums: dict[str, list] = {}
    for pg, ls in enumerate(split):
        for h, raw in {_norm_line(ls[i]): ls[i] for i in edge(ls)}.items():
            seen[h] += 1
            nums.setdefault(h, []).append((pg, tuple(int(x) for x in re.findall(r"\d+", raw))))
    often = max(3, -(-len(pages) * 5 // 100))                                   # 3, or 5 % of the pages

    def running(h: str, n: int) -> bool:
        if not h or len(h) > 80 or h in _SIGNAL_WORDS or not (n >= often or (n >= 2 and h in chapter_titles)):
            return False
        if re.match(r"#(?:\.#)*\s", h) and any(t.startswith(h) for t in headings):
            return False                                  # a numbered heading of the toc (or its first line)
        if h not in chapter_titles and any(t.endswith(" " + h) for t in headings):
            return False                                  # the second line of a wrapped heading
        # numbers that differ from page to page: only a page number qualifies — ONE of the numbers (first, last or any:
        # '56 Version: 4.8.0 ...' or '... Version: 4.8.0 57') grows with the page, the others stay
        return _page_number_like(nums[h], max(1, n // 5))
    heads = {h for h, n in seen.items() if running(h, n)}
    if not heads:
        return pages, []
    glue = [h for h in heads if len(h) >= 8 and re.search(r"[a-zäöü]", h)]
    pats = [re.compile(r"\s*".join(r"\d+" if tok == "#" else re.escape(tok) for tok in re.split(r"(#)|\s+", h) if tok),
                       re.I) for h in glue]
    out = []
    for ls in split:
        for i in edge(ls):
            if _norm_line(ls[i]) in heads:
                ls[i] = ""
                continue
            for pat in pats:
                line = ls[i].lstrip()
                m = pat.match(line)
                rest = line[m.end():] if m else ""
                # glued without a space ('…compensation4.4 Calibration'), or followed by a section number: a header
                # in front of a heading. 'Commissioning EtherCAT slaves is …' (a sentence) is left alone.
                if m and rest and (re.match(r"[\dA-ZÄÖÜ]", rest) or re.match(r"\s+\d+(?:\.\d+)*\s+[A-ZÄÖÜ]", rest)):
                    ls[i] = rest.lstrip()
                    break
                m = re.search(r"(?<=[^\s])(?:" + pat.pattern + r")\s*$", ls[i], re.I)   # glued to the end, no space
                if m and re.match(r"[A-ZÄÖÜ]", ls[i][m.start():]) and not re.search(r"[A-Za-zÄÖÜäöü]$", ls[i][:m.start()]):
                    ls[i] = ls[i][:m.start()].rstrip()
                    break
        out.append("\n".join(ls))
    return out, sorted(heads)


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
    backend, n, toc, texts, meta_title, running = _open_pdf(path)
    st = path.stat()
    cur = con.execute("INSERT INTO doc(path, name, size, mtime, sha256, title, pages, text_pages, indexed, backend) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (str(path), path.name, st.st_size, int(st.st_mtime), sha, meta_title or path.stem, n, 0,
                       time.strftime("%Y-%m-%d %H:%M:%S"), backend))
    doc_id = cur.lastrowid
    toc = _mend_toc(toc)
    secs = _sections(toc, n)
    con.executemany("INSERT INTO toc VALUES(?,?,?,?)", [(doc_id, lvl, t, pg) for lvl, t, pg in toc])
    if running is None:      # no text positions from this backend: the text-repetition fallback
        chapters = {_norm_line(_TOC_NUM.sub("", t)) for lvl, t, _ in toc if lvl <= 2}
        pages, heads = _strip_running(list(texts), chapters, frozenset(_norm_line(t) for _, t, _ in toc))
        running = len(heads)
    else:
        pages = list(texts)
    text_pages = 0
    for pg, raw in enumerate(pages, 1):
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
    return {"id": doc_id, "pages": n, "text_pages": text_pages, "toc": len(toc), "backend": backend,
            "running_spans": running}


class UpdateLock(L_lock.IndexLock):
    """One update at a time: an operating-system lock on <db>.lock (twincat_mcp.kb_lock), held for the life of the
    update and released by the OS however the process ends — a running update is known, never guessed."""

    def __init__(self, db: Path):
        super().__init__(db.with_name(db.name + ".lock"))

    def __enter__(self) -> "UpdateLock":
        try:
            super().__enter__()
        except L_lock.Held as h:
            sys.exit(f"another update is running (pid {h.info.get('pid')}, started {h.info.get('started')}) — wait for "
                     "it; `stats` shows it")
        return self

    def beat(self) -> None:
        self.update(beat=time.strftime("%H:%M:%S"))


def _meta(con, **kv) -> None:
    con.executemany("INSERT OR REPLACE INTO meta VALUES(?, ?)", [(k, str(v)) for k, v in kv.items()])
    con.commit()


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


def cmd_update(args) -> None:
    folder = _dir(args)
    if not folder.is_dir():                             # the user's folder: read, never created or written (rule 1)
        sys.exit(f"no manuals folder {folder} — copy the PDF manuals there (sub-folders allowed), then run update")
    if not args.db:
        for m in L.migrate(kb_root(args.kb)[0]):         # the layout of docs/KNOWLEDGE_BASE.md, once
            print(f"layout: {m['from']} → {m.get('to') or m['state']} ({m.get('why') or m['state']})")
    db = _db(args)                                       # the file the readers read (L.locate)
    building = db.with_name(db.name + ".building")
    if db.is_file() and not args.force:
        st = manuals_state(db, folder)
        if st["state"] == "complete" and not st.get("updating"):
            print(f"up to date: {db} — {st['message']}")        # nothing copied, nothing replaced
            return
    with UpdateLock(db) as lock:
        for p in (building, building.with_name(building.name + "-journal")):
            p.unlink(missing_ok=True)                   # this update's own work file, left by one that was interrupted
        if db.is_file():                                # the update works on a copy: the index stays in use meanwhile
            src = sqlite3.connect(ro_uri(db), uri=True, timeout=30)
            dst = sqlite3.connect(str(building))
            try:
                src.backup(dst)
            finally:
                src.close()
                dst.close()
        _update(args, folder, building, lock)
        if not _replace(building, db):
            sys.exit(f"the update is complete ({building}) but {db} is open in another program — close it (MCP "
                     "tools, kb_docs.py) and run update again")


def _update(args, folder: Path, db: Path, lock: "UpdateLock") -> None:
    con = connect(db, create=True)
    known = {r["path"]: r for r in con.execute("SELECT * FROM doc")}
    found = {str(p): p for p in folder.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file()}
    fmt = (con.execute("SELECT value FROM meta WHERE key='text_format'").fetchone() or ["1"])[0]
    reread = bool(known) and fmt != TEXT_FORMAT
    if reread:
        print(f"page text format {fmt} -> {TEXT_FORMAT}: every manual is read again "
              "(running headers/footers removed, wrapped headings joined)")
    added = changed = unchanged = removed = failed = 0
    _meta(con, state="updating", pid=os.getpid(), started=time.strftime("%Y-%m-%d %H:%M:%S"), files_total=len(found),
          files_done=0, heartbeat=f"{time.time():.0f}", folder=str(folder), builder=f"{BUILDER} {BUILDER_VERSION} (twincat-mcp {PLUGIN_VERSION})")
    for path_s in sorted(set(known) - set(found)):
        _drop(con, known[path_s]["id"]); removed += 1
        print(f"removed  {Path(path_s).name}")
    con.commit()
    for done, (path_s, p) in enumerate(sorted(found.items())):
        _meta(con, files_done=done, heartbeat=f"{time.time():.0f}")
        lock.beat()
        st = p.stat()
        old = known.get(path_s)
        force = args.force or reread
        if old and not force and old["size"] == st.st_size and old["mtime"] == int(st.st_mtime):
            unchanged += 1
            continue
        t0 = time.time()
        sha = _sha256(p)
        if old and not force and old["sha256"] == sha:      # touched, same content
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
              f"{r['running_spans']} running header/footer pieces removed, {time.time() - t0:.1f} s{note}")
    con.commit()
    if added or changed or removed:
        con.execute("INSERT INTO fts(fts) VALUES('optimize')"); con.commit()
    if not (reread and failed):     # a manual that failed during a re-read keeps its old-format rows: read all again next time
        _meta(con, text_format=TEXT_FORMAT)
    _meta(con, state="idle", files_done=len(found), heartbeat=f"{time.time():.0f}",
          last_update=time.strftime("%Y-%m-%d %H:%M:%S"),
          last_result=f"{added} added, {changed} updated, {removed} removed, {unchanged} unchanged, {failed} failed")
    con.close()
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
    """The real state — the twincat-docs skill's implementation (kb_docs.manuals_state), the one every reader uses."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("tcmcp_kb_docs_manuals", HERE.parent / "kb_docs.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.manuals_state(db, folder)


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
    common.add_argument("--db", default=argparse.SUPPRESS, help="database (default <kb>\\index\\manuals.db)")
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
