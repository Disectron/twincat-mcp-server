"""The Infosys index of the locally installed Beckhoff Information System (Microsoft Help Viewer package), built by
the twincat plugin's own extractor (twincat_mcp.infosys) — the ONE index that the plugin's infosys_* tools and the
twincat-docs skill read (docs/KNOWLEDGE_BASE.md in the plugin):

    <kb>\\index\\infosys-<lcid>.db        (1033 = English, 1031 = German)

Standard library only. Needs Python 3.12 or newer (checked at start) with FTS5, which the python.org Windows builds
include. This file ships in the plugin's skills\\twincat-docs\\builders; <kb>\\tools\\infosys_local.py is a stub that
runs it.

usage:
  python infosys_local.py index  [--lang en|de] [--mshc PATH] [--kb PATH | --db PATH] [--force]
  python infosys_local.py stats  [--json]                                  the real state of the index
  python infosys_local.py search QUERY [--book GLOB] [--limit 10]          ranked hits, one line each
  python infosys_local.py show   REF [--max 4000] [--section NAME]         page as text (REF: id, book/id, title)
  python infosys_local.py decl   SYMBOL [--book GLOB]                      declaration code + requirements
  python infosys_local.py books  [--filter GLOB]                           books with page counts

search / show / decl / books are the twincat-docs skill's commands (kb_docs.py) printed as text. QUERY is FTS5:
words are ANDed, "exact phrase", OR, NOT, prefix*.

The knowledge folder (<kb>) is --kb, else the environment variable TCMCP_KNOWLEDGE_DIR, else the folder chosen with
the twincat plugin's kb_set_location, else the folder above this script (or above the stub that runs it) when it is a
knowledge folder, else D:\\TwinCAT-MCP\\knowledge. The package is the newest BKINFOSYS3_*.mshc of every Help Viewer
catalog on this PC (--mshc overrides).

`index` builds only what is due: no index yet, an index of another format (a plugin update changed what is stored),
or another package; otherwise it says 'up to date' (--force builds anyway). The build writes <index>.building and
replaces the index only when it is complete; until then the old index stays in use; an interrupted build is resumed
by the next `index`. The plugin starts the first build by itself; every later one runs when you run this command or
approve kb_rebuild(index='infosys').

Version 3 (0.8.11): one index — the plugin's extractor into index\\infosys-<lcid>.db (version 2 built a second,
different index, helpviewer\\infosys_<lang>.db, which nothing reads any more: delete it).
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:   # before anything else, so an older Python gets this message, not a traceback
    sys.exit(f"infosys_local.py needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
             f"{sys.version.split()[0]} ({sys.executable}). Run it with a newer python.exe, e.g. "
             r"C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe")

import argparse  # noqa: E402 — after the version check on purpose
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

BUILDER = "infosys_local.py"
BUILDER_VERSION = "3"
FALLBACK_KB = Path(r"D:\TwinCAT-MCP\knowledge")
LANGS = {"en": "1033", "de": "1031"}
HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parents[2]                 # <plugin>/skills/twincat-docs/builders → <plugin>

# the extractor is the plugin's own package — this builder's plugin version, not whatever else is installed
if "twincat_mcp" not in sys.modules and (PLUGIN_ROOT / "twincat_mcp" / "infosys.py").is_file():
    sys.path.insert(0, str(PLUGIN_ROOT))
try:
    from twincat_mcp import helpviewer as HV, infosys_build as B, kb_layout as L  # noqa: E402
except ImportError:                 # a copy of this file outside the plugin folder: it needs the plugin beside it
    sys.exit("infosys_local.py runs from the twincat plugin's folder (it uses the plugin's twincat_mcp package and the "
             "twincat-docs skill beside it): run <kb>\\tools\\infosys_local.py (the stub, which finds the installed plugin) or "
             "<plugin>\\skills\\twincat-docs\\builders\\infosys_local.py — not a copy of it")


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
    # the folder above <kb>\tools\<this script> — or above the builder stub in <kb>\tools\ that runs this script
    up = Path(os.environ.get("TCMCP_STUB_DIR") or HERE).resolve().parent
    if L.is_knowledge_folder(up):
        return up, "folder above the script"
    return FALLBACK_KB, "default"


def db_path(args) -> Path:
    if args.db:
        return Path(args.db)
    return L.index_file(kb_root(args.kb)[0], "infosys", LANGS[args.lang])


def cmd_index(args) -> int:
    lcid = LANGS[args.lang]
    if not args.db:
        kb = kb_root(args.kb)[0]
        for m in L.migrate(kb, (lcid,)):                # the layout of docs/KNOWLEDGE_BASE.md, once
            print(f"layout: {m['from']} → {m.get('to') or m['state']} ({m.get('why') or m['state']})")
    pk = HV.find_packages(args.mshc, lcid)
    if not pk:
        where = args.mshc or " / ".join(str(r) for r in HV.catalog_roots())
        print(f"no Beckhoff Infosys package (BKINFOSYS3*.mshc) under {where}; pass --mshc", file=sys.stderr)
        return 1
    try:
        res = B.build(db_path(args), pk[0], lcid, force=args.force,
                      built_by=args.built_by or f"{BUILDER} {BUILDER_VERSION}", log=lambda s: print(s, flush=True))
    except (B.BuildError, HV.HelpViewerError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0 if res["result"] in ("built", "replaced", "up_to_date") else 1


def cmd_stats(args) -> int:
    s = B.state(db_path(args))
    if args.json:
        print(json.dumps(s, ensure_ascii=False))
        return 0
    print(f"{s['database']}: {s['message']}")
    for k in ("format", "package", "links", "links_resolved", "built_by"):
        if s.get(k):
            print(f"  {k}: {s[k]}")
    if s.get("rebuild"):
        print(f"  build: {s['rebuild']}")
    return 0


# ---------------------------------------------------------------- the reader: the twincat-docs skill's commands
def _kb_docs():
    p = HERE.parent / "kb_docs.py"
    spec = importlib.util.spec_from_file_location("tcmcp_kb_docs_cli", p)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _reader(args):
    kd = _kb_docs()
    root, how = kb_root(args.kb)
    kd._KB_SHOWN = str(root)
    ctx = kd.Ctx(root, how, infosys_db=Path(args.db) if args.db else None)
    return kd, ctx


def _run(args, cmd: str, params: dict) -> tuple[object, dict] | None:
    kd, ctx = _reader(args)
    fn, spec, _doc = kd.COMMANDS[cmd]
    try:
        return kd, fn(ctx, kd.bind(spec, {k: str(v) for k, v in params.items() if v is not None}))
    except kd.Err as exc:
        print(exc.message + (f"\n  {exc.extra}" if exc.extra else ""), file=sys.stderr)
        return None


def _links(out: dict) -> str:
    lk = out.get("links") or []
    return ("\nLinks: " + "; ".join(f"{x['text']} → {x['ref']}" for x in lk)) if lk else ""


def _head(out: dict) -> str:
    ver = f" | {out['version']}" if out.get("version") else ""
    return f"{out['title']} | {out['ref']}{ver}\n{out['url']}\n"


def cmd_search(args) -> int:
    got = _run(args, "docs_search", {"query": args.query, "book": args.book, "limit": args.limit})
    if got is None:
        return 1
    _kd, out = got
    for it in out["items"]:
        print(f"{it['title']} | {it['ref']} | {it['excerpt']}")
    if not out["items"]:
        print("no hits" + (f" in books {args.book}" if args.book else ""))
    elif out["total"] > len(out["items"]):
        print(f"({len(out['items'])} of {out['total']} hits; --limit for more)")
    return 0


def cmd_show(args) -> int:
    got = _run(args, "docs_page", {"ref": args.ref, "book": args.book, "section": args.section, "max_chars": args.max})
    if got is None:
        return 1
    _kd, out = got
    tail = f"\n… [{out['chars'] - len(out['text'])} more chars; --max or --section]" if out.get("truncated") else ""
    print(_head(out) + "\n" + out["text"] + tail + _links(out))
    return 0


def cmd_decl(args) -> int:
    got = _run(args, "docs_declaration", {"symbol": args.symbol, "book": args.book})
    if got is None:
        return 1
    _kd, out = got
    parts = [_head(out)]
    if out.get("intro"):
        parts.append(out["intro"])
    parts += ["```\n" + b + "\n```" for b in out.get("declaration_blocks") or []]
    if out.get("requirements"):
        parts.append("Requirements:\n" + out["requirements"])
    if out.get("also_in"):
        parts.append("Also in: " + ", ".join(a["ref"] for a in out["also_in"]))
    lk = _links(out)
    if lk:
        parts.append(lk.strip())
    print("\n".join(parts))
    return 0


def cmd_books(args) -> int:
    got = _run(args, "docs_books", {"filter": args.filter})
    if got is None:
        return 1
    for b in got[1]["books"]:
        print(f"{b['book']}  {b['pages']}")
    return 0


def main() -> int:
    # --kb / --db / --lang work before or after the command (index --kb D:\\kb as well as --kb D:\\kb index)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--lang", default=argparse.SUPPRESS, choices=sorted(LANGS))
    common.add_argument("--kb", default=argparse.SUPPRESS, help="knowledge folder (default: see above)")
    common.add_argument("--db", default=argparse.SUPPRESS, help="index file (default <kb>\\index\\infosys-<lcid>.db)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    _sub = sub.add_parser
    sub.add_parser = lambda *a, **kw: _sub(*a, parents=[common], **kw)  # type: ignore[method-assign]
    s = sub.add_parser("index"); s.add_argument("--mshc"); s.add_argument("--force", action="store_true")
    s.add_argument("--built-by", dest="built_by", help=argparse.SUPPRESS)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--book"); s.add_argument("--limit", type=int, default=10)
    s = sub.add_parser("show"); s.add_argument("ref"); s.add_argument("--max", type=int, default=4000)
    s.add_argument("--section"); s.add_argument("--book")
    s = sub.add_parser("decl"); s.add_argument("symbol"); s.add_argument("--book")
    s = sub.add_parser("books"); s.add_argument("--filter")
    s = sub.add_parser("stats"); s.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for k, v in (("lang", "en"), ("kb", None), ("db", None), ("built_by", None)):
        if not hasattr(args, k):
            setattr(args, k, v)
    if getattr(args, "limit", 1) < 1:
        ap.error("--limit must be 1 or more")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return {"index": cmd_index, "search": cmd_search, "show": cmd_show, "decl": cmd_decl,
            "books": cmd_books, "stats": cmd_stats}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
