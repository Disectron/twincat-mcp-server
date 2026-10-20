"""The knowledge folder's layout — one place that says where everything is (docs/KNOWLEDGE_BASE.md, the rules).

    <kb>\\
      README.md            orientation
      manuals\\             yours: PDF manuals (any sub-folders) — read, never written
      sop\\                 yours: notes / SOPs (.md) — read, never written
      tools\\               builder stubs (replaced only when the file is one the plugin shipped)
      index\\               the plugin's: one index per source, rebuildable from it
        infosys-<lcid>.db  the Beckhoff Information System of the local Help Viewer package
        manuals.db         the PDFs in manuals\\
        esi.db             the EtherCAT device descriptions: ESI files in manuals\\ and TwinCAT's ESI folders (hw_*)
        knowledge.db       added project folders (kb_search)
        README.md          what each index is, its format, how it is rebuilt

Standard library only: the server, the docs skill script (kb_docs.py) and the builders all import it, so the three
can never disagree about a path again.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

INDEX_DIR = "index"
FILES = {"infosys": "infosys-{lcid}.db", "manuals": "manuals.db", "esi": "esi.db", "knowledge": "knowledge.db"}
# where an index lived before the layout was fixed (0.8.11): moved into index\ once, never copied
LEGACY = {"infosys": ["infosys/helpviewer-{lcid}/index.sqlite"], "manuals": ["manualsdb/manuals.db"], "esi": [],
          "knowledge": ["knowledge.db"]}
# files the plugin no longer reads — reported (with their size) for the user to delete; never deleted here
OBSOLETE = ["helpviewer/infosys_en.db", "helpviewer/infosys_en.db.building", "helpviewer/infosys_en.db.lock"]
SIDECARS = ("-wal", "-shm", "-journal")
DEFAULT_LCID = "1033"


def index_dir(kb: Path) -> Path:
    return Path(kb) / INDEX_DIR


def is_knowledge_folder(path: Path) -> bool:
    """A folder that holds a knowledge base: index\\, or (before the move into index\\) a file of an old place."""
    p = Path(path)
    tops = {INDEX_DIR, "helpviewer"} | {rel.split("/")[0] for rels in LEGACY.values() for rel in rels}
    return any((p / t).exists() for t in tops)


def index_file(kb: Path, kind: str, lcid: str = DEFAULT_LCID) -> Path:
    """The permanent path of an index."""
    return index_dir(kb) / FILES[kind].format(lcid=lcid)


def legacy_files(kb: Path, kind: str, lcid: str = DEFAULT_LCID) -> list[Path]:
    return [Path(kb) / rel.format(lcid=lcid) for rel in LEGACY[kind]]


def locate(kb: Path, kind: str, lcid: str = DEFAULT_LCID) -> Path:
    """The file to read: the permanent one; an old location only while the move has not happened yet (the file was
    in use when it was tried)."""
    new = index_file(kb, kind, lcid)
    if new.exists():
        return new
    for old in legacy_files(kb, kind, lcid):
        if old.exists():
            return old
    return new


def migrate(kb: Path, lcids: tuple[str, ...] = (DEFAULT_LCID,)) -> list[dict[str, Any]]:
    """Move every index from its old place into index\\ — once: a rename on the same drive (instant), with SQLite's
    side files; never a copy, never over an existing file. A file in use (another program has it open) stays where
    it is and is moved at a later start. -> what was moved / left, one entry per file found in an old place."""
    out: list[dict[str, Any]] = []
    kb = Path(kb)
    if not kb.is_dir():
        return out
    for kind in FILES:
        for lcid in (lcids if kind == "infosys" else (DEFAULT_LCID,)):
            new = index_file(kb, kind, lcid)
            for old in legacy_files(kb, kind, lcid):
                if not old.exists() or old.resolve() == new.resolve():
                    continue
                if new.exists():
                    out.append({"index": kind, "from": str(old), "state": "left",
                                "why": f"{new.name} already exists in index\\ — the old file is not used (delete it)"})
                    continue
                new.parent.mkdir(parents=True, exist_ok=True)
                out.append(_move_db(kind, old, new))
    return out


def _move_db(kind: str, old: Path, new: Path) -> dict[str, Any]:
    """One SQLite file with its side files, all or nothing: a WAL-mode database without its -wal loses its last
    commits, so the side files go first and the main file last; any rename that fails puts back what was moved. No
    destination may exist (os.rename replaces one on POSIX: checked before anything moves)."""
    pairs = [(old.with_name(old.name + s), new.with_name(new.name + s)) for s in SIDECARS
             if old.with_name(old.name + s).exists()] + [(old, new)]
    there = [str(d) for _s, d in pairs if d.exists()]
    if there:
        return {"index": kind, "from": str(old), "state": "left",
                "why": f"not moved: {', '.join(there)} already exists (nothing is moved over a file)"}
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in pairs:
            os.rename(src, dst)
            done.append((src, dst))
    except OSError as exc:
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError:
                pass
        return {"index": kind, "from": str(old), "state": "in use",
                "why": f"not moved now ({exc.strerror or exc}); moved at a later start"}
    return {"index": kind, "from": str(old), "to": str(new), "state": "moved", "files": [s.name for s, _d in done]}


def obsolete_files(kb: Path) -> list[dict[str, Any]]:
    """Files in the knowledge folder the plugin no longer reads, with their size — for the user to delete."""
    out = []
    for rel in OBSOLETE:
        p = Path(kb) / rel
        if p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            out.append({"path": str(p), "size_mb": round(size / 1e6, 1),
                        "why": "the Help Viewer package is indexed once, into index\\infosys-<lcid>.db (0.8.11)"})
    for kind in FILES:
        new = index_file(kb, kind)
        for old in legacy_files(kb, kind):
            if old.is_file() and new.is_file() and old.resolve() != new.resolve():
                out.append({"path": str(old), "size_mb": round(old.stat().st_size / 1e6, 1),
                            "why": f"an old copy: {new} is the one in use"})
    return out


INDEX_README = """# index\\ — the plugin's indexes (rules: docs/KNOWLEDGE_BASE.md in the plugin)

Every file here is built from a source on this PC and can be rebuilt from it at any time. Nothing here is yours to
edit; outside index\\ the plugin writes only the builder stubs in tools\\ (a copy it replaces is kept as .bak).

| File | Source | Built by |
|---|---|---|
| infosys-<lcid>.db | the Beckhoff Information System package of the local Help Viewer (BKINFOSYS3_*.mshc) | tools\\infosys_local.py index, or kb_rebuild('infosys') |
| manuals.db | the PDF manuals in ..\\manuals\\ | tools\\manuals_db.py update, or kb_rebuild('manuals') |
| esi.db | the EtherCAT device descriptions (ESI .xml) in ..\\manuals\\ and TwinCAT's Config\\Io\\EtherCAT | the plugin's esi_db.py update, or kb_rebuild('esi'); its first build starts by itself |
| knowledge.db | project folders you added (kb_add_project) | kb_add_project / kb_reindex |

Each index records its format version and the stamp of its source. When a plugin update changes a format, or the
source changes (a new Help Viewer package, new PDFs, new ESI files), the index is reported as needing a rebuild and
stays in use as it is until you approve the rebuild. A rebuild writes a new file beside the old one (.building) and
swaps it in only when it is complete; knowledge.db changes in one transaction per folder, one change at a time
(knowledge.db.lock). (knowledge.db of 0.8.11 and before also
holds an ESI table: no longer read — esi.db replaced it.) Apart from an index replaced by its
complete rebuild, the plugin never deletes a file here or anywhere in the knowledge folder; files it no longer reads
are listed by kb_status for you to delete.
"""


def write_index_readme(kb: Path) -> bool:
    """index\\README.md is the plugin's (index\\ is its folder): written when missing or different."""
    p = index_dir(kb) / "README.md"
    try:
        if p.is_file() and p.read_text(encoding="utf-8") == INDEX_README:
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(INDEX_README, encoding="utf-8")
        return True
    except OSError:
        return False
