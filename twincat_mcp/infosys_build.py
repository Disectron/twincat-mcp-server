"""The Infosys index of the local Help Viewer package: its state and its build — one index (index\\infosys-<lcid>.db),
one extractor (twincat_mcp.infosys), the rules of docs/KNOWLEDGE_BASE.md.

* A build writes <index>.building and replaces the index only when the new one is complete; an interrupted build
  never replaces anything and is resumed (same format, same package) by the next one.
* One build at a time: an operating-system lock on <index>.lock (twincat_mcp.kb_lock), held for the life of the build
  — whether a build is running is known, never guessed. The file holds the build's progress as JSON.
* Which package an index is compared with is one rule for every reader (_Source): the server's own package; else the
  newest BKINFOSYS3*.mshc of the Help Viewer catalogs and of the folder the index was built from. The very file it was
  built from, unchanged, is the same package; anything else is decided by the content.
* The plugin's server (the first build of an index that does not exist), kb_rebuild (approved) and the builder command
  in the knowledge folder (tools\\infosys_local.py index) all run build(); every reader reports state().

Standard library only: the server, the docs skill script and the builder import it.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import helpviewer as HV
from . import infosys as I
from . import kb_lock

SWAP_WAIT_S = 60.0               # Windows: the live index cannot be replaced while a reader has it open
SIDE = ("-wal", "-shm", "-journal")


class BuildError(RuntimeError):
    pass


def side_files(final: Path) -> tuple[Path, Path]:
    final = Path(final)
    return final.with_name(final.name + ".building"), final.with_name(final.name + ".lock")


def read_meta(path: Path) -> dict[str, str]:
    """meta of an index file, read-only; {} when it is missing or not readable."""
    if not Path(path).is_file():
        return {}
    try:
        con = sqlite3.connect(I.ro_uri(Path(path)), uri=True, timeout=5)
        try:
            return {str(r[0]): str(r[1]) for r in con.execute("SELECT key, value FROM meta")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _count_pages(path: Path) -> int:
    try:
        con = sqlite3.connect(I.ro_uri(Path(path)), uri=True, timeout=5)
        try:
            return int(con.execute("SELECT COUNT(*) FROM pages").fetchone()[0])
        finally:
            con.close()
    except sqlite3.Error:
        return 0


def package_file_stamp(pkg: Path) -> str:
    st = Path(pkg).stat()
    return f"{Path(pkg).name}|{st.st_size}|{st.st_mtime_ns}"


def current_package(recorded: str | None, lcid: str = "1033") -> Path | None:
    """The package a reader without the server compares an index with: the newest BKINFOSYS3*.mshc of the Help
    Viewer catalogs and of the folder the index was built from (the recorded package itself, while it is there)."""
    cands: set[Path] = set(HV.find_packages(None, lcid))
    if recorded and Path(recorded).parent.is_dir():
        cands.update(HV.find_packages(str(Path(recorded).parent), lcid))
    cands = {c for c in cands if c.is_file()}
    return max(cands, key=lambda f: f.stat().st_mtime) if cands else None


class _Source:
    """The package that is here now — what an index (or a build) is compared with."""

    def __init__(self, path: Path, lcid: str, local: Any = None):
        self.path, self.lcid, self.local = Path(path), lcid, local
        self._stamp: str | None = None

    def content_stamp(self) -> str:
        if self._stamp is None:
            src = self.local if self.local is not None else HV.HelpViewerSource(self.path, self.lcid)
            self._stamp = src.content_stamp()
        return self._stamp

    def built_from(self, meta: dict[str, str]) -> bool:
        """Was the index (or build) with this meta made from this package? The same file, unchanged (path, name,
        size, modification time), was; otherwise the content stamp decides. Raises when the package is unreadable."""
        stamp = meta.get("source_stamp") or meta.get("target_stamp")
        if not stamp:
            return False
        try:
            same_file = (meta.get("package") == str(self.path.resolve())
                         and meta.get("package_file") == package_file_stamp(self.path))
        except OSError:
            same_file = False
        return same_file or stamp == self.content_stamp()


def _source(meta: dict[str, str], local: Any, package: Path | None) -> _Source | None:
    lcid = meta.get("lcid") or "1033"
    if local is not None:
        return _Source(local.path, getattr(local, "lcid", lcid), local)
    if package:
        return _Source(Path(package), lcid)
    p = current_package(meta.get("package"), lcid)
    return _Source(p, lcid) if p is not None else None


def state(final: Path, local: Any = None, package: Path | None = None) -> dict[str, Any]:
    """The real state of the index — the live file every reader uses, and a build in progress or left behind:
      missing         no index yet (the first build starts by itself)
      complete        built by this format from the package that is here
      rebuild_needed  built by another format, or never finished (an index made before 0.8.11): in use as it is
      outdated        built from another package: in use as it is
    plus 'rebuild': running (a build holds the lock) / interrupted (the next build resumes it) / waiting_to_replace
    (finished, swapped in at the next server start or build) / stale (of another format or package, or redundant
    next to a complete current index: the next build discards it). local: the HelpViewerSource the server reads;
    package: a package path; else the package that is here (_Source)."""
    final = Path(final)
    building, lock = side_files(final)
    meta = read_meta(final)
    bm = read_meta(building)
    src = _source(meta or bm, local, package)
    out: dict[str, Any] = {"database": str(final), "format_expected": I.INDEXER_VERSION}
    fmt = int(meta["indexer_version"]) if str(meta.get("indexer_version", "")).isdigit() else None
    if final.is_file():
        out["format"] = fmt
    if not final.is_file():
        out["state"] = "missing"
    elif fmt != I.INDEXER_VERSION:
        out["state"] = "rebuild_needed"
        out["reason"] = (f"format {fmt or 'unknown'}, this plugin writes {I.INDEXER_VERSION} — stored text, links and "
                         "declarations differ (rule 5)")
    elif not meta.get("source_stamp") or meta.get("state", "complete") != "complete":
        out["state"] = "rebuild_needed"
        out["reason"] = "the index was never finished"
    elif src is None:
        out["state"] = "complete"
        out["package_note"] = "no Help Viewer package found on this PC to compare it with"
    else:
        try:
            same = src.built_from(meta)
            out["state"] = "complete" if same else "outdated"
            if not same:
                out["reason"] = f"the Help Viewer package changed since the index was built (now {src.path})"
        except Exception as exc:  # noqa: BLE001 — an unreadable package: the index is what it is
            out["state"] = "complete"
            out["package_note"] = f"the package is not readable ({exc})"
    for k in ("pages", "books", "links", "links_resolved", "built", "built_by", "lcid", "package", "package_date"):
        if meta.get(k):
            out[k] = int(meta[k]) if k in ("pages", "books", "links", "links_resolved") and meta[k].isdigit() else meta[k]
    running = kb_lock.held(lock)
    if running or building.is_file():
        info, beat = kb_lock.read(lock)
        rb: dict[str, Any] = {"file": str(building), "started": bm.get("started") or info.get("started"),
                              "pages_done": int(info.get("pages_done") or 0) if running else _count_pages(building),
                              "pages_total": int(info.get("pages_total") or bm.get("pages_total") or 0)}
        if running:
            rb.update(state="running", phase=info.get("phase") or "starting", pid=info.get("pid"),
                      seconds_since_progress=round(time.time() - beat) if beat else None)
        else:
            try:
                same_pkg = src is None or src.built_from(bm)
            except Exception:  # noqa: BLE001
                same_pkg = True                       # cannot tell: judge by the format alone
            ours = bm.get("indexer_version") == str(I.INDEXER_VERSION) and same_pkg
            if not ours:
                rb.update(state="stale", why="a build of another format or package — the next build discards it")
            elif bm.get("state") == "complete":
                rb["state"] = "waiting_to_replace"
            elif out["state"] == "complete":
                rb.update(state="stale", why="the index in use is complete and current — the next build discards "
                                             "it (index --force resumes it)")
            else:
                rb["state"] = "interrupted"
        out["rebuild"] = rb
    out["message"] = message(out)
    return out


def message(s: dict[str, Any]) -> str:
    st = s["state"]
    rb = s.get("rebuild") or {}
    live = {"missing": "no index yet",
            "complete": "complete",
            "rebuild_needed": f"in use as it is, a REBUILD is due — {s.get('reason', '')}",
            "outdated": f"in use as it is, OUTDATED — {s.get('reason', '')}"}[st]
    if st != "missing" and s.get("pages"):
        live += f" ({s['pages']} pages, {s.get('books')} books, built {s.get('built') or '?'})"
    if st in ("rebuild_needed", "outdated") and rb.get("state") != "running":
        live += " — the rebuild runs once approved: kb_rebuild(index='infosys'), or tools\\infosys_local.py index"
    if rb:
        prog = f"{rb.get('pages_done', 0)} of {rb.get('pages_total') or '?'} pages"
        live += {"running": f"; a build is RUNNING ({rb.get('phase')}, {prog})"
                            + (" — the index above stays in use until it is done" if st != "missing" else ""),
                 "interrupted": f"; a build was INTERRUPTED at {prog} — the next build resumes it"
                                + (" (the index above is still the one in use)" if st != "missing" else ""),
                 "waiting_to_replace": "; a finished build waits to replace it (the file was open) — it is swapped in "
                                       "at the next server start or build",
                 "stale": f"; a build was left ({prog}): {rb.get('why')}"}[rb["state"]]
    return live


def problem(s: dict[str, Any]) -> bool:
    """Is this state worth the user's attention (not complete, or a build running / interrupted / waiting)? A stale
    leftover is not: the next build discards it, and it changes nothing about the index in use."""
    return s.get("state") != "complete" or (s.get("rebuild") or {}).get("state") in ("running", "interrupted",
                                                                                     "waiting_to_replace")


class BuildLock(kb_lock.IndexLock):
    """One build at a time (kb_lock): raises BuildError when another build holds <index>.lock."""

    def __init__(self, final: Path):
        super().__init__(side_files(final)[1])

    def __enter__(self) -> "BuildLock":
        try:
            super().__enter__()
        except kb_lock.Held as h:
            raise BuildError(f"another build is running (pid {h.info.get('pid')}, started {h.info.get('started')}, "
                             f"{h.info.get('phase') or 'starting'}) — its state: kb_status / infosys_local.py stats") from None
        return self


def _no_wal(db: Path, wait_s: float = 0.0) -> bool:
    """Rollback-journal mode, so readers open the file read-only with no side files and a swap leaves no -wal of
    the old file next to the new one (SQLite would apply it). Changes nothing stored. -> done?"""
    t0 = time.time()
    while True:
        try:
            con = sqlite3.connect(str(db), timeout=5)
            try:
                mode = con.execute("PRAGMA journal_mode").fetchone()[0]
                if str(mode).lower() == "wal":
                    mode = con.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if str(mode).lower() != "wal":
                    return True
            finally:
                con.close()
        except sqlite3.Error:
            pass
        if time.time() - t0 >= wait_s:
            return False
        time.sleep(1.0)


def prepare_for_readers(final: Path) -> bool:
    """An index written by an older plugin (WAL mode) in rollback-journal mode: read-only readers then need no side
    files. Best effort — a file another program holds open stays as it is. -> changed or already so."""
    final = Path(final)
    return final.is_file() and _no_wal(final)


def _discard(building: Path) -> None:
    """The build's own work file (and its SQLite side files) — the only thing a build deletes (rule 7)."""
    for p in [building] + [building.with_name(building.name + x) for x in SIDE]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def _swap(building: Path, final: Path, wait_s: float = SWAP_WAIT_S) -> str | None:
    """The finished build replaces the live index (os.replace, retried while a reader has the file open). Both files
    in rollback-journal mode first: a WAL frame not yet in the main file would stay behind in a side file of a name
    nobody opens. -> None when replaced, else why not."""
    t0 = time.time()
    if not _no_wal(building, wait_s):
        return f"the finished build {building} could not be closed out of WAL mode (open in another program?)"
    if final.is_file() and not _no_wal(final, max(0.0, wait_s - (time.time() - t0))):
        return f"{final} is open in another program"
    while True:
        try:
            os.replace(building, final)
            return None
        except PermissionError:
            if time.time() - t0 > wait_s:
                return f"{final} is open in another program"
            time.sleep(1.0)


def finish_pending(final: Path, local: Any = None) -> dict[str, Any] | None:
    """A finished build that could not replace the live index (it was open): swap it in now — that build was
    approved (or was the first). None when there is nothing to finish."""
    building, lock = side_files(final)
    bm = read_meta(building)
    if bm.get("state") != "complete" or bm.get("indexer_version") != str(I.INDEXER_VERSION):
        return None
    if kb_lock.held(lock):
        return None                                   # its builder is still at it
    src = _source(bm, local, None)
    try:
        if src is not None and not src.built_from(bm):
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        with BuildLock(final):
            why = _swap(building, final, 5.0)
    except BuildError:
        return None
    return {"replaced": why is None, "index": str(final), "pages": bm.get("pages"), **({"why": why} if why else {})}


def build(final: Path, package: Path, lcid: str = "1033", force: bool = False, built_by: str = "",
          log: Callable[[str], None] = print) -> dict[str, Any]:
    """Build the index of `package` into <final>.building and swap it in when complete. Resumes an interrupted
    build of the same format and package (also with force); discards a .building of another format or package, and
    one next to a complete, current index when nothing is forced. force: rebuild even when the index is complete and
    current. -> {'result': 'up_to_date' | 'built' | 'replaced' | 'incomplete' | 'not_replaced', ...}"""
    final = Path(final)
    building, _lock = side_files(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    with BuildLock(final) as lk:
        src = HV.HelpViewerSource(package, lcid)
        lk.update(phase="reading the package")
        stamp = src.content_stamp()
        bm = read_meta(building)
        ours = (building.is_file() and bm.get("indexer_version") == str(I.INDEXER_VERSION)
                and (bm.get("source_stamp") or bm.get("target_stamp")) == stamp)
        if ours and bm.get("state") == "complete" and not force:
            lk.update(phase="replacing the live index")
            why = _swap(building, final)
            if why is None:
                log(f"replaced {final} with the build finished earlier ({bm.get('pages')} pages)")
                return {"result": "replaced", "index": str(final), "pages": bm.get("pages")}
            raise BuildError(f"{why} — the finished build ({building}) replaces it at the next server start or build")
        st = state(final, local=src)
        if st["state"] == "complete" and not force:
            if building.is_file():
                _discard(building)
                log(f"discarded {building.name}: the index in use is complete and current")
            log(f"up to date: {final} ({st.get('pages', '?')} pages from {src.path.name})")
            return {"result": "up_to_date", "index": str(final), "state": st}
        resume = ours and bm.get("state") != "complete"
        if building.is_file() and not resume:
            _discard(building)                         # the build's own work file of another format or package
        why = ("forced" if force else "no index yet" if st["state"] == "missing" else st.get("reason") or st["state"])
        log(f"building {final} ({why}){' — resuming the interrupted build' if resume else ''}; "
            + ("the current index stays in use until the new one is complete" if final.is_file() else
               "the documentation tools answer once it is complete"))
        t0 = time.time()
        idx = I.InfosysIndex(db_file=building, local=src)
        pages_total = len(src.pages())
        with idx._db() as con:
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
                ("state", "building"), ("target_stamp", stamp), ("package", str(src.path.resolve())),
                ("package_file", package_file_stamp(src.path)),
                ("lcid", lcid), ("built_by", built_by or "twincat-mcp"), ("pages_total", str(pages_total)),
                ("python", sys.version.split()[0])] + ([] if resume else [("started", time.strftime("%Y-%m-%d %H:%M:%S"))]))
        done = {"n": _count_pages(building)}           # pages already in (a resumed build)

        def book_prog(book: str, i: int, n: int) -> None:
            lk.update(phase=f"books ({i} of {n})" + (f": {book}" if book else ""))

        def page_prog(book: str, d: int, t: int) -> None:
            if d == t or d % 200 == 0:
                lk.update(pages_done=done["n"] + d, pages_total=pages_total)
            if d == t:
                done["n"] += t
        lk.update(phase="pages", pages_total=pages_total, pages_done=done["n"])
        res = idx.index_all(book_prog, page_prog)
        if res["book_errors"]:
            log(f"INCOMPLETE: {len(res['book_errors'])} book(s) failed — {res['book_errors'][:3]}; the next build "
                "resumes; nothing was replaced")
            return {"result": "incomplete", "index": str(final), "building": str(building), **res}
        lk.update(phase="resolving links")
        links = idx.resolve_help_links()
        lk.update(phase="lookup tables")
        idx.sync_derived()
        with idx._db() as con:
            n_pages = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
            n_books = con.execute("SELECT COUNT(DISTINCT book) FROM pages").fetchone()[0]
            if idx.has_fts:
                con.execute("INSERT INTO pages_fts(pages_fts) VALUES ('optimize')")
            con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
                ("source_stamp", stamp), ("package_file", package_file_stamp(src.path)), ("pages", str(n_pages)),
                ("books", str(n_books)), ("package_date", src.describe()["modified"]),
                ("built", time.strftime("%Y-%m-%d %H:%M:%S")), ("state", "complete")])
        lk.update(phase="replacing the live index")
        took = round(time.time() - t0)
        why = _swap(building, final)
        if why is not None:
            log(f"the new index is complete ({building}) but {why} — it is swapped in at the next server start or "
                "build, without reading the package again")
            return {"result": "not_replaced", "index": str(final), "building": str(building), "pages": n_pages,
                    "why": why}
        log(f"indexed {n_pages} pages ({n_books} books, {links['resolved']} of {links['links']} help links resolved) "
            f"from {src.path} into {final} in {took} s ({final.stat().st_size / 1e6:.0f} MB)")
        return {"result": "built", "index": str(final), "pages": n_pages, "books": n_books, "seconds": took,
                "links": links}
