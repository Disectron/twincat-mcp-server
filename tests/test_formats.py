"""Rule 5 of docs/KNOWLEDGE_BASE.md: every index records its format, and a test fingerprints the code that produces
each format — it fails when that code changes without a new format version, so a change to what is stored can never
reach an index silently (and an index of an older format is reported as needing a rebuild, never rebuilt unasked).

When one of these fails: the stored content changed. Bump the format named in the message (at most once per index per
release, rule 6) and set the new fingerprint printed by the failure."""
from __future__ import annotations

import hashlib
import importlib.util
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (format version, fingerprint of the code that produces it)
INFOSYS_FINGERPRINT = (4, "9e39afc8bf71b1c3")
MANUALS_FINGERPRINT = ("4", "8d9ac4107c60f0d8")
ESI_FINGERPRINT = (3, "5bc82867a5b9433d")


def _digest(objs) -> str:
    parts = [inspect.getsource(o) if not isinstance(o, str) else o for o in objs]
    norm = ["\n".join(ln.rstrip() for ln in p.splitlines() if ln.strip() and not ln.strip().startswith("#"))
            for p in parts]
    return hashlib.sha256("\n\n".join(norm).encode()).hexdigest()[:16]


def _manuals_module():
    p = ROOT / "skills" / "twincat-docs" / "builders" / "manuals_db.py"
    spec = importlib.util.spec_from_file_location("tcmcp_manuals_db_fp", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                  # inspect finds a class's source through its module
    spec.loader.exec_module(mod)
    return mod


def test_infosys_format_tracks_the_extraction_code():
    """Everything that decides what a page becomes in index\\infosys-<lcid>.db: the HTML walker, the link, cell and
    declaration rules, what is stored and the help-link pass of the build."""
    from twincat_mcp import helpviewer as hv
    from twincat_mcp import infosys as inf
    code = [inf._MdWalker, inf.html_to_doc, inf.HtmlDoc, inf._md_links, inf._plain, inf._first_href, inf.resolve_href,
            inf._nobr, inf._mend_cell, inf._guess_lang, inf.parse_declaration, inf.classify, inf._parse_vars,
            inf._descriptions, inf._table_kind, inf._member_tables, inf._type_refs, inf._library_from_tables,
            inf._return_in_header, inf._tidy_declaration, inf._doc_version, inf._text_of, inf.InfosysIndex._store,
            inf.InfosysIndex._fetch_doc, inf.InfosysIndex.resolve_help_links, hv.body_html, hv.page_meta,
            inf._LINK.pattern, inf._STRAY.pattern, inf._PKG_REL.pattern, inf._XHELP.pattern, inf._TYPE_HEADER.pattern,
            repr(sorted(inf._INVISIBLE.items())), repr(sorted(inf._CODE_SPACES.items())), repr(sorted(inf._IEC_WORDS))]
    got = (inf.INDEXER_VERSION, _digest(code))
    assert got == INFOSYS_FINGERPRINT, (
        f"Infosys extraction code changed: bump infosys.INDEXER_VERSION and set INFOSYS_FINGERPRINT = {got!r}")


def test_manuals_text_format_tracks_the_text_code():
    m = _manuals_module()
    code = [m.Span, m._visible, m._spans_pymupdf, m._spans_pypdf, m._running, m._page_text, m._open_pdf,
            m._toc_from_fonts, m._first_title, m._join_wrapped, m._mend_toc, m._norm_line, m._page_number_like,
            m._strip_running, m._clean, m._sections, m.index_pdf, m.PRODUCT_RE.pattern, m._NUM.pattern,
            m._GLUED.pattern, m._TOC_NUM.pattern]
    got = (m.TEXT_FORMAT, _digest(code))
    assert got == MANUALS_FINGERPRINT, (
        f"manuals page text code changed: bump manuals_db.TEXT_FORMAT and set MANUALS_FINGERPRINT = {got!r}")


def test_esi_parser_version_tracks_the_parser():
    from twincat_mcp import knowledge as K
    got = (K.ESI_PARSER_VERSION, _digest([K._parse_esi, K._text, K._deep_text, K._kids, K._ln, K._hexint]))
    assert got == ESI_FINGERPRINT, (
        f"ESI parser changed: bump knowledge.ESI_PARSER_VERSION and set ESI_FINGERPRINT = {got!r}")
