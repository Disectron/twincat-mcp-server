"""The Infosys index of the local Help Viewer package: its state and its build — one index (index\\infosys-<lcid>.db),
one extractor (twincat_mcp.infosys), the rules of docs/KNOWLEDGE_BASE.md.

* A build writes <index>.building and replaces the index only when the new one is complete; an interrupted build
  never replaces anything and is resumed (same format, same package) by the next one.
* One build at a time: <index>.lock, created exclusively, its modification time the heartbeat, its JSON the progress.
* The plugin's server (the first build of an index that does not exist), kb_rebuild (approved) and the builder command
  in the knowledge folder (tools\\infosys_local.py index) all run build(); every reader reports state().

Standard library only: the server, the docs skill script and the builder import it.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import infosys as I
from . import helpviewer as HV

HEARTBEAT_STALE_S = 120          # a build whose lock has not moved for this long is no longer running
SWAP_WAIT_S = 60.0               # Windows: the live index cannot be replaced while a reader has it open


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


def _lock_info(lock: Path) -> tuple[dict[str, Any], float | None]:
    try:
        info = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    try:
        return info, lock.stat().st_mtime
    except OSError:
        return info, None


def _source_state(meta: dict[str, str], local: Any, package: Path | None) -> tuple[bool | None, str]:
    """(is the index built from the package that is here now?, why not). None: cannot tell (no package found)."""
    built_from = meta.get("package")
    if local is not None:
        try:
            same = meta.get("source_stamp") == local.content_stamp()
        except Exception as exc:  # noqa: BLE001
            return None, f"the package is not readable ({exc})"
        if same:
            return True, ""
        return False, f"the Help Viewer package changed since the index was built (now {local.path.name})"
    pkg = Path(package) if package else (Path(built_from) if built_from else None)
    if pkg is None:
        return None, "no package recorded"
    if not pkg.is_file():
        return None, f"the package it was built from is gone: {pkg}"
    newer = [x for x in HV.find_packages(str(pkg.parent)) if x != pkg]
    if newer and newer[0].stat().st_mtime > pkg.stat().st_mtime:
        return False, f"a newer package exists: {newer[0]}"
    if built_from and str(pkg) == built_from and meta.get("package_file") == package_file_stamp(pkg):
        return True, ""
    try:                                              # touched or moved: the content decides
        same = meta.get("source_stamp") == HV.HelpViewerSource(pkg).content_stamp()
    except Exception as exc:  # noqa: BLE001
        return None, f"the package is not readable ({exc})"
    return (True, "") if same else (False, f"the Help Viewer package changed since the index was built ({pkg})")


def state(final: Path, local: Any = None, package: Path | None = None) -> dict[str, Any]:
    """The real state of the index — the live file every reader uses, and a build in progress or abandoned:
      missing         no index yet (the first build starts by itself)
      complete        built by this format from the package that is here
      rebuild_needed  built by another format, or never finished (an index made before 0.8.11): in use as it is
      outdated        built from another package: in use as it is
    plus 'rebuild': running / interrupted / waiting_to_replace. local: the HelpViewerSource the reader uses (its
    content decides); package: a package path (else the one the index was built from)."""
    final = Path(final)
    building, lock = side_files(final)
    meta = read_meta(final)
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
    else:
        same, why = _source_state(meta, local, package)
        out["state"] = "outdated" if same is False else "complete"
        if why:
            out["reason" if same is False else "package_note"] = why
    for k in ("pages", "books", "links", "links_resolved", "built", "built_by", "lcid", "package", "package_date"):
        if meta.get(k):
            out[k] = int(meta[k]) if k in ("pages", "books", "links", "links_resolved") and meta[k].isdigit() else meta[k]
    if building.is_file() or lock.is_file():
        bm = read_meta(building)
        info, beat = _lock_info(lock)
        now = time.time()
        alive = beat is not None and now - beat < HEARTBEAT_STALE_S
        rb: dict[str, Any] = {"file": str(building), "started": bm.get("started") or info.get("started"),
                              "pages_done": int(info.get("pages_done") or 0) if alive else _count_pages(building),
                              "pages_total": int(info.get("pages_total") or bm.get("pages_total") or 0),
                              "seconds_since_progress": round(now - beat) if beat else None}
        if alive:
            rb["state"] = "running"
            rb["phase"] = info.get("phase") or "pages"
            rb["pid"] = info.get("pid")
        elif bm.get("state") == "complete":
            rb["state"] = "waiting_to_replace"
        elif building.is_file():
            rb["state"] = "interrupted"
        if "state" in rb:
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
                                       "at the next server start or build"}[rb["state"]]
    return live


class BuildLock:
    """One build at a time: <index>.lock created exclusively; a lock whose heartbeat is older than HEARTBEAT_STALE_S
    belongs to a build that is gone and is taken over. update() writes the progress readers show."""

    def __init__(self, final: Path):
        self.path = side_files(final)[1]
        self.info: dict[str, Any] = {}

    def __enter__(self) -> "BuildLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                self.info = {"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": "starting"}
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(self.info))
                self._stop = threading.Event()
                threading.Thread(target=self._beat_loop, name="heartbeat", daemon=True).start()
                return self
            except FileExistsError:
                info, beat = _lock_info(self.path)
                if beat is not None and time.time() - beat < HEARTBEAT_STALE_S:
                    raise BuildError(f"another build is running (pid {info.get('pid')}, started {info.get('started')}, "
                                     f"last progress {time.time() - beat:.0f} s ago) — its state: kb_status / "
                                     "infosys_local.py stats") from None
                try:
                    self.path.unlink()                        # stale: that build is gone
                except FileNotFoundError:
                    pass
        raise BuildError(f"cannot take the build lock {self.path}")

    def update(self, **kv: Any) -> None:
        self.info.update(kv)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(self.info), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            self.beat()

    def beat(self) -> None:
        try:
            os.utime(self.path)
        except OSError:
            pass

    def _beat_loop(self) -> None:
        # long single steps (the FTS optimize, the link pass) must not make a live build look abandoned
        while not self._stop.wait(15):
            self.beat()

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


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
                    con.execute("PRAGMA journal_mode=DELETE")
                return True
            finally:
                con.close()
        except sqlite3.Error:
            if time.time() - t0 >= wait_s:
                return False
            time.sleep(1.0)


def prepare_for_readers(final: Path) -> bool:
    """An index written by an older plugin (WAL mode) in rollback-journal mode: read-only readers then need no side
    files. Best effort — a file another program holds open stays as it is. -> changed or already so."""
    final = Path(final)
    return final.is_file() and _no_wal(final)


def _swap(building: Path, final: Path, wait_s: float = SWAP_WAIT_S) -> bool:
    """The finished build replaces the live index (os.replace, retried while a reader has the file open)."""
    t0 = time.time()
    if final.is_file() and not _no_wal(final, wait_s):
        return False
    while True:
        try:
            os.replace(building, final)
            return True
        except PermissionError:
            if time.time() - t0 > wait_s:
                return False
            time.sleep(1.0)


def finish_pending(final: Path, local: Any = None) -> dict[str, Any] | None:
    """A finished build that could not replace the live index (it was open): swap it in now — that build was
    approved (or was the first). None when there is nothing to finish."""
    building, lock = side_files(final)
    bm = read_meta(building)
    if bm.get("state") != "complete" or bm.get("indexer_version") != str(I.INDEXER_VERSION):
        return None
    info, beat = _lock_info(lock)
    if beat is not None and time.time() - beat < HEARTBEAT_STALE_S:
        return None                                   # its builder is still at it
    if local is not None and bm.get("source_stamp") != local.content_stamp():
        return None
    try:
        with BuildLock(final):
            ok = _swap(building, final, 5.0)
    except BuildError:
        return None
    return {"replaced": ok, "index": str(final), "pages": bm.get("pages")}


def build(final: Path, package: Path, lcid: str = "1033", force: bool = False, built_by: str = "",
          log: Callable[[str], None] = print) -> dict[str, Any]:
    """Build the index of `package` into <final>.building and swap it in when complete. Resumes an interrupted
    build of the same format and package. force: rebuild even when the index is complete and current.
    -> {'result': 'up_to_date' | 'built' | 'replaced' | 'incomplete' | 'not_replaced', ...}"""
    final = Path(final)
    building, _lock = side_files(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    with BuildLock(final) as lk:
        src = HV.HelpViewerSource(package, lcid)
        lk.update(phase="reading the package")
        stamp = src.content_stamp()
        bm = read_meta(building)
        if bm.get("state") == "complete" and bm.get("source_stamp") == stamp \
                and bm.get("indexer_version") == str(I.INDEXER_VERSION) and not force:
            lk.update(phase="replacing the live index")
            if _swap(building, final):
                log(f"replaced {final} with the build finished earlier ({bm.get('pages')} pages)")
                return {"result": "replaced", "index": str(final), "pages": bm.get("pages")}
            raise BuildError(f"{final} is open in another program — the finished build ({building}) replaces it at "
                             "the next server start or build")
        st = state(final, local=src)
        if st["state"] == "complete" and not force:
            log(f"up to date: {final} ({st.get('pages', '?')} pages from {src.path.name})")
            return {"result": "up_to_date", "index": str(final), "state": st}
        resume = (building.is_file() and bm.get("indexer_version") == str(I.INDEXER_VERSION)
                  and bm.get("target_stamp") == stamp and not force)
        if building.is_file() and not resume:
            for p in (building, building.with_name(building.name + "-wal"), building.with_name(building.name + "-shm")):
                try:
                    p.unlink()                         # the build's own work file of another format or package
                except FileNotFoundError:
                    pass
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
        _no_wal(building, 30.0)
        lk.update(phase="replacing the live index")
        took = round(time.time() - t0)
        if not _swap(building, final):
            log(f"the new index is complete ({building}) but {final} is open in another program — it is swapped in at "
                "the next server start or build, without reading the package again")
            return {"result": "not_replaced", "index": str(final), "building": str(building), "pages": n_pages}
        log(f"indexed {n_pages} pages ({n_books} books, {links['resolved']} of {links['links']} help links resolved) "
            f"from {src.path} into {final} in {took} s ({final.stat().st_size / 1e6:.0f} MB)")
        return {"result": "built", "index": str(final), "pages": n_pages, "books": n_books, "seconds": took,
                "links": links}
