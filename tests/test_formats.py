"""Rule 5 of docs/KNOWLEDGE_BASE.md: every index records its format, and a test fingerprints the code that produces
each format — it fails when that code changes without a new format version, so a change to what is stored can never
reach an index silently (and an index of an older format is reported as needing a rebuild, never rebuilt unasked).

What is fingerprinted is found, not listed: starting from the functions that write an index, every module-level
function, class, method (self.x) and constant they reach — through any depth — is part of it (_closure). A helper or
a table added to the path tomorrow is covered without editing this file. EXCLUDE names the few things on that path
that decide nothing stored: connection handling, error types, the web sitemap parser.

When one of these fails: the stored content changed. Bump the format named in the message (at most once per index per
release, rule 6) and set the new fingerprint printed by the failure."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import inspect
import re
import sys
import textwrap
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (format version, fingerprint of the code that produces it)
INFOSYS_FINGERPRINT = (7, "82482f0a39e6a29a")   # 0.8.15: kinds from the page's own evidence (N150 N151 N157 N172),
#                                                    symbols inside pages (N170)
MANUALS_FINGERPRINT = ("6", "481f937f2d3d0a9d")   # 0.8.14 retest: product codes of every family (N163)
ESI_FINGERPRINT = ("2", "90374d1987a8c788")    # index\\esi.db (esi_index.py): format 2 — modules found by their
#                                                 devices (N156, 0.8.14 retest), one object per index of a dictionary
#                                                 (EL6653, kb_verify E2; format 2 is not released yet); 1 since 0.8.12

EXCLUDE = frozenset({
    "_db", "_Conn", "_file_sig", "_probe", "_writable", "missing_text", "ro_uri", "source", "has_fts", "_bump_gen",
    "InfosysError", "HelpViewerError", "_sitemap_locs", "_sitemap_urlset",
    "connect", "_meta",
    "_RES_CACHE",                 # esi_index: the RES\\ folder listings of this process (runtime state, not a rule)
})


def _closure(roots: list, modules: list) -> dict[str, str]:
    """{name: source or constant} of everything the roots reach through module-level names of `modules` and the
    methods of their own classes."""
    mods = {m.__name__ for m in modules}
    found: dict[str, str] = {}
    todo = list(roots)

    def owner(fn):
        q = getattr(fn, "__qualname__", "")
        cls = sys.modules[fn.__module__].__dict__.get(q.split(".")[0]) if "." in q else None
        return cls if inspect.isclass(cls) else None

    def value(name: str, val) -> None:
        if name in EXCLUDE or isinstance(val, types.ModuleType):
            return
        if inspect.isfunction(val) or inspect.isclass(val):
            if getattr(val, "__module__", None) in mods:
                todo.append(val)
        elif isinstance(val, re.Pattern):
            found.setdefault(name, repr((val.pattern, val.flags)))
        elif isinstance(val, (str, bytes, int, float, bool, tuple)):
            found.setdefault(name, repr(val))
        elif isinstance(val, (set, frozenset)):
            found.setdefault(name, repr(sorted(map(repr, val))))
        elif isinstance(val, dict):
            found.setdefault(name, repr(sorted((repr(k), repr(v)) for k, v in val.items())))

    while todo:
        obj = todo.pop()
        key = obj.__qualname__
        if key in found or obj.__name__ in EXCLUDE:
            continue
        src = textwrap.dedent(inspect.getsource(obj))
        found[key] = "\n".join(ln.rstrip() for ln in src.splitlines() if ln.strip() and not ln.strip().startswith("#"))
        g = obj.__globals__ if inspect.isfunction(obj) else sys.modules[obj.__module__].__dict__
        cls = owner(obj) if inspect.isfunction(obj) else None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.ImportFrom):
                # review of 0.8.14: a name imported inside a function (html_to_doc's `from .helpviewer import
                # decode_page`) is on the path too — the closure did not see it
                try:
                    modname = importlib.util.resolve_name("." * node.level + (node.module or ""), obj.__module__
                                                          .rpartition(".")[0]) if node.level else node.module
                except (ImportError, ValueError):
                    modname = None
                if modname in mods:
                    for a in node.names:
                        value(a.asname or a.name, getattr(sys.modules[modname], a.name, None))
            if isinstance(node, ast.Name) and node.id in g:
                value(node.id, g[node.id])
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                base = node.value.id
                if base in ("self", "cls") and cls is not None and node.attr not in EXCLUDE:
                    m = inspect.getattr_static(cls, node.attr, None)
                    m = m.__func__ if isinstance(m, (staticmethod, classmethod)) else m
                    m = m.fget if isinstance(m, property) else m
                    if inspect.isfunction(m):
                        todo.append(m)
                elif base in g and isinstance(g[base], types.ModuleType) and g[base].__name__ in mods:
                    value(node.attr, getattr(g[base], node.attr, None))
    return found


def _digest(found: dict[str, str]) -> str:
    return hashlib.sha256("\n\n".join(f"{k}\n{v}" for k, v in sorted(found.items())).encode()).hexdigest()[:16]


def _manuals_module():
    p = ROOT / "skills" / "twincat-docs" / "builders" / "manuals_db.py"
    spec = importlib.util.spec_from_file_location("tcmcp_manuals_db_fp", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                  # inspect finds a class's source through its module
    spec.loader.exec_module(mod)
    return mod


def infosys_code() -> dict[str, str]:
    """What a page of the local package becomes in index\\infosys-<lcid>.db: the package reader, the books chosen,
    the HTML walker, links, cells, declarations, the title words (docsearch.ident_words), the schema, the help-link
    pass and the lookup tables."""
    from twincat_mcp import docsearch as ds
    from twincat_mcp import helpviewer as hv
    from twincat_mcp import infosys as inf
    return _closure([inf.InfosysIndex.index_all, inf.InfosysIndex._store, inf.InfosysIndex.resolve_help_links,
                     inf.InfosysIndex.sync_derived, inf.InfosysIndex._init_db, inf.html_to_doc, inf.parse_declaration,
                     inf.classify, hv.HelpViewerSource], [inf, hv, ds])


def test_infosys_format_tracks_the_extraction_code():
    from twincat_mcp import infosys as inf
    got = (inf.INDEXER_VERSION, _digest(infosys_code()))
    assert got == INFOSYS_FINGERPRINT, (
        f"Infosys extraction code changed: bump infosys.INDEXER_VERSION and set INFOSYS_FINGERPRINT = {got!r}")


def test_manuals_text_format_tracks_the_text_code():
    m = _manuals_module()
    got = (m.TEXT_FORMAT, _digest(_closure([m.index_pdf], [m])))
    assert got == MANUALS_FINGERPRINT, (
        f"manuals page text code changed: bump manuals_db.TEXT_FORMAT and set MANUALS_FINGERPRINT = {got!r}")


def esi_code() -> dict[str, str]:
    """What the ESI files become in index\\esi.db: the readers of the four document kinds, the dictionary, PDO,
    template, slot and diagnosis rules, the content store and the schema (esi_index._build_into reaches them all)."""
    from twincat_mcp import esi_index as E
    return _closure([E._build_into], [E])


def test_esi_format_tracks_the_index_code():
    from twincat_mcp import esi_index as E
    got = (E.FORMAT, _digest(esi_code()))
    assert got == ESI_FINGERPRINT, (
        f"ESI index code changed: bump esi_index.FORMAT and set ESI_FINGERPRINT = {got!r}")


def test_the_esi_closure_reaches_the_parsers():
    found = esi_code()
    for name in ("parse_device", "parse_dictionary", "parse_diag_messages", "parse_module", "iter_info",
                 "iter_modules", "_templates", "_pdo", "_chn", "_slots", "_mappings", "decode_data", "_INT_TYPES",
                 "_Build", "_Writer", "SCHEMA", "FTS", "_resolve_ref", "_title", "_hash", "_pack",
                 "module_candidates"):           # N156: the slot rule decides a module's search text
        assert name in found, name


def test_the_closure_reaches_what_decides_stored_content():
    """Review: a fixed list missed _SKIP, _PREFIX_KIND, book selection … — changing any of them changes what is stored.
    The closure must reach them (and a new helper on the path without anyone listing it)."""
    found = infosys_code()
    for name in ("_SKIP", "_PREFIX_KIND", "_BLOCK", "_HEAD_RE", "_SECTION_RE", "_VARLINE_RE", "_STRUCT_RE", "_ENUM_RE",
                 "_IDENT", "_TABLE_MARK", "_VERSIONISH", "CODE_SEP", "_ident_matches", "book_of", "library_from_book",
                 "InfosysIndex.book_allowed", "is_tc2_book", "TC2_BOOK_RES", "_MdWalker", "_type_refs",
                 "InfosysIndex.sitemap", "HelpViewerSource", "decode_page", "_CHARSET", "utf8_majority"):
        assert name in found, name
    for name in EXCLUDE:
        assert name not in found and f"InfosysIndex.{name}" not in found, name
