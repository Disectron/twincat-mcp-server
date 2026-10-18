"""The mechanics behind docs/KNOWLEDGE_BASE.md: one build at a time (an operating-system lock, kb_lock), the move into
index\\ (all or nothing with SQLite's side files, kb_layout), and the swap of a finished build (infosys_build)."""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from twincat_mcp import infosys_build as IB
from twincat_mcp import kb_layout as L
from twincat_mcp import kb_lock

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------------------------- kb_lock
def _holder(lock: Path, ready: Path) -> subprocess.Popen:
    code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from twincat_mcp import kb_lock
        with kb_lock.IndexLock(__import__('pathlib').Path({str(lock)!r})) as lk:
            lk.update(phase="pages", pages_done=7)
            open({str(ready)!r}, "w").close()
            time.sleep(120)
    """)
    p = subprocess.Popen([sys.executable, "-c", code])
    t0 = time.time()
    while not ready.exists() and time.time() - t0 < 30:
        time.sleep(0.02)
    assert ready.exists()
    return p


def test_a_build_that_dies_releases_its_lock_at_once(tmp_path):
    """Review: a heartbeat lock could be taken over by two builds at once (both saw it stale), and a live build that
    paused looked dead. The operating system holds and releases the lock: killed → free, no waiting, no guessing."""
    lock = tmp_path / "x.db.lock"
    p = _holder(lock, tmp_path / "ready")
    try:
        assert kb_lock.held(lock) and kb_lock.read(lock)[0]["pages_done"] == 7
        with pytest.raises(kb_lock.Held) as e:
            with kb_lock.IndexLock(lock):
                pass
        assert e.value.info["pid"] == p.pid
        os.utime(lock, (0, 0))                       # an old modification time means nothing
        assert kb_lock.held(lock)
    finally:
        p.kill()
        p.wait(30)
    assert not kb_lock.held(lock)                    # killed: released by the OS, at once
    with kb_lock.IndexLock(lock) as lk:
        lk.update(phase="mine")
        assert kb_lock.read(lock)[0]["phase"] == "mine"
    assert lock.is_file() and lock.read_text() == ""     # kept, empty


def test_a_reader_in_the_same_process_never_releases_the_builders_lock(tmp_path):
    """POSIX fcntl locks belong to the process: a reader that opened and closed the file would drop the build's lock.
    flock / msvcrt locks belong to the open file: probing in the same process leaves the build's lock in place."""
    lock = tmp_path / "x.db.lock"
    with kb_lock.IndexLock(lock):
        assert kb_lock.held(lock) and kb_lock.held(lock)     # probed twice (open + close each time)
        with pytest.raises(kb_lock.Held):
            with kb_lock.IndexLock(lock):
                pass
        assert kb_lock.held(lock)
    assert not kb_lock.held(lock)


# ---------------------------------------------------------------------------------------------- kb_layout move
def _wal_db(path: Path) -> sqlite3.Connection:
    """A WAL-mode database whose last commit is only in the -wal (the connection stays open, no checkpoint)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE t(v)")
    con.execute("INSERT INTO t VALUES ('last commit')")
    con.commit()
    return con


@pytest.mark.skipif(os.name == "nt", reason="an open SQLite file cannot be renamed on Windows (moved at a later start)")
def test_an_old_index_moves_with_its_wal_and_keeps_its_last_commit(tmp_path):
    kb = tmp_path / "kb"
    con = _wal_db(kb / "knowledge.db")
    assert (kb / "knowledge.db-wal").stat().st_size > 0
    moves = L.migrate(kb)
    con.close()
    m = [x for x in moves if x["index"] == "knowledge"][0]
    assert m["state"] == "moved" and "knowledge.db-wal" in m["files"], m
    new = kb / "index" / "knowledge.db"
    assert not (kb / "knowledge.db").exists() and not (kb / "knowledge.db-wal").exists()
    assert sqlite3.connect(new).execute("SELECT v FROM t").fetchone()[0] == "last commit"


def test_a_move_is_all_or_nothing(tmp_path, monkeypatch):
    """Review: the main file moved first and a side file that failed was swallowed ('moved'), losing the last commits;
    on POSIX os.rename replaced an existing side file at the destination."""
    kb = tmp_path / "kb"
    (kb / "manualsdb").mkdir(parents=True)
    old = kb / "manualsdb" / "manuals.db"
    old.write_bytes(b"main")
    (kb / "manualsdb" / "manuals.db-wal").write_bytes(b"wal")
    # a side file already at the destination: nothing moves, nothing is replaced
    (kb / "index").mkdir()
    (kb / "index" / "manuals.db-wal").write_bytes(b"someone else's")
    m = [x for x in L.migrate(kb) if x["index"] == "manuals"][0]
    assert m["state"] == "left" and old.read_bytes() == b"main"
    assert (kb / "index" / "manuals.db-wal").read_bytes() == b"someone else's"
    (kb / "index" / "manuals.db-wal").unlink()
    # the last rename fails: what was moved goes back
    real = os.rename

    def flaky(src, dst):
        if str(src).endswith("manuals.db"):
            raise PermissionError(13, "in use")
        return real(src, dst)
    monkeypatch.setattr(os, "rename", flaky)
    m = [x for x in L.migrate(kb) if x["index"] == "manuals"][0]
    assert m["state"] == "in use"
    assert old.read_bytes() == b"main" and (kb / "manualsdb" / "manuals.db-wal").read_bytes() == b"wal"
    assert not (kb / "index" / "manuals.db").exists() and not (kb / "index" / "manuals.db-wal").exists()


# ---------------------------------------------------------------------------------------------- swap
def test_a_finished_build_in_wal_mode_is_swapped_in_whole(tmp_path):
    """Review: the switch out of WAL before the swap was not checked — a WAL frame (the 'complete' row) could stay in
    a -wal of a name nobody opens. The swap closes both files out of WAL first, and refuses when it cannot."""
    final = tmp_path / "index" / "infosys-1033.db"
    building = final.with_name(final.name + ".building")
    con = _wal_db(building)
    con.close()                                       # last close checkpoints; WAL mode stays recorded in the file
    c = sqlite3.connect(building)
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("INSERT INTO t VALUES ('complete')")
    c.commit()
    assert building.with_name(building.name + "-wal").exists()
    # still open by another connection: it cannot leave WAL mode — the swap refuses and nothing moves
    why = IB._swap(building, final, wait_s=0.0)
    assert why and "WAL" in why and building.exists() and not final.exists()
    c.close()
    assert IB._swap(building, final, wait_s=0.0) is None
    assert not building.exists() and not any(final.parent.glob("*-wal"))
    assert [r[0] for r in sqlite3.connect(final).execute("SELECT v FROM t")] == ["last commit", "complete"]
    assert sqlite3.connect(final).execute("PRAGMA journal_mode").fetchone()[0] == "delete"
