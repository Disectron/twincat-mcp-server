"""Error codes (N125): index the codes the knowledge folder's own indexes document into <kb>\\index\\errors.db and look
them up.

Sources — the plugin's indexes in <kb>\\index\\, never the Help Viewer package or the PDFs again: the code tables of
infosys-<lcid>.db (ADS return codes, Win32 error codes, NC errors, the error-code pages of the libraries …), the AL
status codes of manuals.db (rows where a page's table parses cleanly, else a link to the page) and the diagnosis text
ids of esi.db (linked, not copied). What is stored and the rules: twincat_mcp/errors_index.py (the plugin's one
implementation; the server's err_lookup tool and the twincat-docs skill's err_lookup command read the same file with
the same function).

usage:
  python errors_db.py [--kb PATH | --db PATH] <command> ...
  python errors_db.py update  [--force] [--lcid 1033] [--infosys-db PATH]    build (checked: the golden checks)
  python errors_db.py stats   [--json]                                      the state: complete / rebuild_needed …
  python errors_db.py lookup  QUERY [--domain DOMAIN] [--limit 5]           what a code / name / message is

`update` writes <db>.building and replaces the index only when it is complete and passed its checks (the index in use
stays until then; a build that fails them replaces nothing and says why). Without --force an index built from exactly
these source indexes is left as it is; --force also accepts a domain with fewer codes than at the last build (reported).
Standard library only; needs Python 3.12 or newer (checked at start).

The knowledge folder (<kb>) is --kb, else TCMCP_KNOWLEDGE_DIR, else the folder chosen with the twincat plugin's
kb_set_location, else the folder above this script when it is a knowledge folder, else D:\\TwinCAT-MCP\\knowledge.

Version 1 (0.8.16): first version — errors format 1.
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:   # before anything else, so an older Python gets this message, not a traceback
    sys.exit(f"errors_db.py needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
             f"{sys.version.split()[0]} ({sys.executable}). Run it with a newer python.exe, e.g. "
             r"C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe")

import argparse  # noqa: E402 — after the version check on purpose
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parents[2]                 # <plugin>/skills/twincat-docs/builders → <plugin>
if "twincat_mcp" not in sys.modules and (PLUGIN_ROOT / "twincat_mcp" / "errors_index.py").is_file():
    sys.path.insert(0, str(PLUGIN_ROOT))
try:
    from twincat_mcp import __version__ as PLUGIN_VERSION, errors_index as X, kb_layout as L  # noqa: E402
except ImportError:                 # a copy of this file outside the plugin folder: it needs the plugin beside it
    sys.exit("errors_db.py runs from the twincat plugin's folder (it uses the plugin's twincat_mcp package): run "
             "<plugin>\\skills\\twincat-docs\\builders\\errors_db.py — not a copy of it")

BUILDER = "errors_db.py"
BUILDER_VERSION = "1"
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
    up = Path(os.environ.get("TCMCP_STUB_DIR") or HERE).resolve().parent
    if L.is_knowledge_folder(up):
        return up, "folder above the script"
    return FALLBACK_KB, "default"


def _db(args) -> Path:
    return Path(args.db) if args.db else L.index_file(kb_root(args.kb)[0], "errors")


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def cmd_update(args) -> int:
    kb, how = kb_root(args.kb)
    db = _db(args)
    print(f"knowledge folder: {kb} ({how}); index: {db}")
    try:
        res = X.build(db, kb, lcid=args.lcid, force=args.force, infosys_db=args.infosys_db,
                      built_by=f"{BUILDER} {BUILDER_VERSION} (twincat-mcp {PLUGIN_VERSION})")
    except X.ErrError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if res.get("result") == "incomplete":
        print("the build FAILED its checks — nothing was replaced: " + "; ".join(res.get("golden_failures") or []),
              file=sys.stderr)
        return 3
    return 0 if res.get("result") in ("built", "up_to_date", "replaced") else 2


def cmd_stats(args) -> int:
    kb = kb_root(args.kb)[0]
    st = X.state(_db(args), kb, detail=True)
    if args.json:
        _out(st)
    else:
        print(f"{st['database']}: {st['state']} — {st['message']}")
        for k in ("codes", "rows", "variants", "domains", "zero_row_sources", "built", "built_by"):
            if k in st:
                print(f"  {k}: {st[k]}")
        for d, x in (st.get("domains_detail") or {}).items():
            if "/" not in d:
                print(f"  {d}: {x}")
    return 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--kb", default=argparse.SUPPRESS, help="knowledge folder (default: see above)")
    common.add_argument("--db", default=argparse.SUPPRESS, help="database (default <kb>\\index\\errors.db)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    _sub = sub.add_parser
    sub.add_parser = lambda *a, **kw: _sub(*a, parents=[common], **kw)  # type: ignore[method-assign]
    s = sub.add_parser("update")
    s.add_argument("--force", action="store_true", help="build even when current; accept a domain with fewer codes")
    s.add_argument("--lcid", default=L.DEFAULT_LCID, help="the Infosys index's language (default 1033)")
    s.add_argument("--infosys-db", default=None, help="the Infosys index to read (default: the one the index in use "
                   "was built from, else <kb>\\index\\infosys-<lcid>.db)")
    s = sub.add_parser("stats")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("lookup")
    s.add_argument("query")
    s.add_argument("--domain")
    s.add_argument("--limit", type=int, default=5, help=f"matches shown (1-{X.LIMIT_MAX}, default 5)")
    args = ap.parse_args()
    for k in ("kb", "db"):
        if not hasattr(args, k):
            setattr(args, k, None)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if args.cmd == "update":
        return cmd_update(args)
    if args.cmd == "stats":
        return cmd_stats(args)
    try:
        _out(X.err_lookup(args.query, args.domain, kb=kb_root(args.kb)[0], limit=args.limit))
        return 0
    except X.ErrError as exc:
        _out({"error": exc.code, "message": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
