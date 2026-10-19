"""EtherCAT device descriptions (ESI): index every ESI file of this PC into <kb>\\index\\esi.db and query it.

Sources (both, one index): every *.xml under <kb>\\manuals\\ whose root is an ESI document (EtherCATInfo,
EtherCATModule, EtherCATDict, EtherCATDiag — e.g. a copy of Beckhoff's ESI folder in manuals\\Beckhoff_EtherCAT_XML,
sub-folders included), and TwinCAT's ESI folders on this PC (Config\\Io\\EtherCAT), plus --esi-dir folders. What is
stored and the rules for duplicates: twincat_mcp/esi_index.py (the plugin's one implementation; the server's hw_*
tools and the twincat-docs skill read the same file).

usage:
  python esi_db.py [--kb PATH | --db PATH] <command> ...
  python esi_db.py update  [--force] [--esi-dir DIR ... | --no-esi-dirs] [--no-twincat | --twincat]   build
  python esi_db.py stats   [--json]                                      the state: complete / outdated / building …
  python esi_db.py lookup  QUERY [--revision REV] [--detail newest|summary|full] [--limit 5]
  python esi_db.py search  QUERY [--kinds device,object,diag,module] [--limit 20]
  python esi_db.py object  DEVICE INDEX [--revision REV]                one CoE object with its sub-indices
  python esi_db.py diag    CODE [--device DEVICE]                       a diagnosis message (text id, e.g. #x4101)
  python esi_db.py module  IDENT [--device DEVICE]                      a module of a modular device

Query commands print one JSON object. QUERY of search is FTS5 syntax (words ANDed, "phrase", prefix*); text that is
not FTS5 syntax is searched as one phrase.

`update` writes <db>.building and replaces the index only when it is complete (the index in use stays until then; an
interrupted build replaces nothing). Without --force an index built from exactly these files is left as it is.
--esi-dir: extra ESI folders (the server passes its knowledge.esi_dirs); without it, the extra folders the index in
use was built with; TwinCAT's own ESI folders as the index in use (--no-twincat / --twincat change it). Standard
library only; needs Python 3.12 or newer (checked at start).

The knowledge folder (<kb>) is --kb, else TCMCP_KNOWLEDGE_DIR, else the folder chosen with the twincat plugin's
kb_set_location, else the folder above this script when it is a knowledge folder, else D:\\TwinCAT-MCP\\knowledge.

Version 1 (0.8.12): first version — ESI format 1.
"""
from __future__ import annotations

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:   # before anything else, so an older Python gets this message, not a traceback
    sys.exit(f"esi_db.py needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
             f"{sys.version.split()[0]} ({sys.executable}). Run it with a newer python.exe, e.g. "
             r"C:\Users\<you>\AppData\Local\Programs\Python\Python312\python.exe")

import argparse  # noqa: E402 — after the version check on purpose
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parents[2]                 # <plugin>/skills/twincat-docs/builders → <plugin>
if "twincat_mcp" not in sys.modules and (PLUGIN_ROOT / "twincat_mcp" / "esi_index.py").is_file():
    sys.path.insert(0, str(PLUGIN_ROOT))
try:
    from twincat_mcp import __version__ as PLUGIN_VERSION, esi_index as E, kb_layout as L  # noqa: E402
except ImportError:                 # a copy of this file outside the plugin folder: it needs the plugin beside it
    sys.exit("esi_db.py runs from the twincat plugin's folder (it uses the plugin's twincat_mcp package): run "
             "<kb>\\tools\\esi_db.py (the stub, which finds the installed plugin) or "
             "<plugin>\\skills\\twincat-docs\\builders\\esi_db.py — not a copy of it")

BUILDER = "esi_db.py"
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
    return Path(args.db) if args.db else L.index_file(kb_root(args.kb)[0], "esi")


def _out(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


def cmd_update(args) -> int:
    kb, how = kb_root(args.kb)
    db = _db(args)
    print(f"knowledge folder: {kb} ({how}); index: {db}")
    try:
        extra = [] if args.no_esi_dirs else (args.esi_dir or None)      # None: the folders the index was built with
        twincat = False if args.no_twincat else True if args.twincat else None     # None: as the index in use
        res = E.build(db, kb, extra=extra, twincat=twincat,
                      force=args.force, built_by=f"{BUILDER} {BUILDER_VERSION} (twincat-mcp {PLUGIN_VERSION})")
    except E.EsiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if res.get("result") in ("built", "up_to_date", "replaced") else 2


def cmd_stats(args) -> int:
    kb = kb_root(args.kb)[0]
    st = E.state(_db(args), kb)
    if args.json:
        _out(st)
    else:
        print(f"{st['database']}: {st['state']} — {st['message']}")
        for k in ("devices", "types", "modules", "dictionaries", "objects", "diag_messages", "files", "built", "built_by"):
            if k in st:
                print(f"  {k}: {st[k]}")
        for k in ("missing_refs", "errors"):
            if k in st:
                print(f"  {k}: {st[k]['count']} (first: {st[k]['first']})")
    return 0


def _query(fn, *a, **kw) -> int:
    try:
        _out(fn(*a, **kw))
        return 0
    except E.EsiError as exc:
        _out({"error": str(exc)})
        return 1


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--kb", default=argparse.SUPPRESS, help="knowledge folder (default: see above)")
    common.add_argument("--db", default=argparse.SUPPRESS, help="database (default <kb>\\index\\esi.db)")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    sub = ap.add_subparsers(dest="cmd", required=True)
    _sub = sub.add_parser
    sub.add_parser = lambda *a, **kw: _sub(*a, parents=[common], **kw)  # type: ignore[method-assign]
    s = sub.add_parser("update")
    s.add_argument("--force", action="store_true")
    s.add_argument("--esi-dir", action="append", default=[], help="an extra ESI folder (repeatable)")
    s.add_argument("--no-esi-dirs", action="store_true", help="no extra ESI folders (not even those the index in use "
                   "was built with)")
    s.add_argument("--no-twincat", action="store_true", help="leave TwinCAT's own ESI folders out (recorded: later "
                   "updates keep it)")
    s.add_argument("--twincat", action="store_true", help="read TwinCAT's own ESI folders again (the default for a new "
                   "index)")
    s = sub.add_parser("stats")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("lookup")
    s.add_argument("query")
    s.add_argument("--revision")
    s.add_argument("--detail", default="newest", choices=E.DETAILS)
    s.add_argument("--limit", type=int, default=5)
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--kinds")
    s.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("object")
    s.add_argument("device")
    s.add_argument("index")
    s.add_argument("--revision")
    s = sub.add_parser("diag")
    s.add_argument("code")
    s.add_argument("--device")
    s = sub.add_parser("module")
    s.add_argument("ident")
    s.add_argument("--device")
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
    x = E.EsiIndex(_db(args))
    if args.cmd == "lookup":
        return _query(x.lookup, args.query, args.limit, args.revision, args.detail)
    if args.cmd == "search":
        return _query(x.search, args.query, args.kinds.split(",") if args.kinds else None, args.limit)
    if args.cmd == "object":
        return _query(x.object, args.device, args.index, args.revision)
    if args.cmd == "diag":
        return _query(x.diag, args.code, args.device)
    return _query(x.module, args.ident, args.device)


if __name__ == "__main__":
    sys.exit(main())
