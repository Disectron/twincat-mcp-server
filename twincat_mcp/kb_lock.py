"""One build of an index at a time (docs/KNOWLEDGE_BASE.md): an operating-system lock on <index>.lock, held for the
life of the build. The operating system releases it when the process ends in any way — finished, killed, crashed, the
PC switched off — so whether a build is running is known, not guessed from a heartbeat: no stale lock to take over, no
two builds that both believe they hold it, no live build that looks dead because one step took long or the PC slept.

<index>.lock also holds the build's progress as JSON, readable at any time. The lock is on one byte far past that text
(LOCK_AT), so reading the progress never meets it (Windows locks are mandatory). The file is never deleted — deleting
an open lock file lets a second build lock a new file of the same name — and is empty when no build runs.

POSIX: flock (per open file, so a reader in the same process that opens and closes the file never releases the
build's lock, as fcntl/lockf locks would). Windows: msvcrt.locking. Standard library only: the server, the docs skill
script and both builders import it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

LOCK_AT = 1 << 30                 # the locked byte (Windows): far past any progress text
_TAKE_TRIES = 20                  # a reader's probe holds the lock for microseconds: a builder retries for ~1 s

if os.name == "nt":
    import msvcrt

    def _try(fd: int) -> bool:
        os.lseek(fd, LOCK_AT, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _release(fd: int) -> None:
        os.lseek(fd, LOCK_AT, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _try(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _release(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass


class Held(RuntimeError):
    """Another build holds the lock."""

    def __init__(self, info: dict[str, Any]):
        super().__init__(f"another build is running (pid {info.get('pid')}, started {info.get('started')})")
        self.info = info


class IndexLock:
    """with IndexLock(path) as lk: … lk.update(phase=…, pages_done=…). Raises Held when another build holds it."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None
        self.info: dict[str, Any] = {}

    def __enter__(self) -> "IndexLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o666)
        for i in range(_TAKE_TRIES):
            if _try(fd):
                break
            time.sleep(0.05)
        else:
            os.close(fd)
            raise Held(read(self.path)[0])
        self.fd = fd
        self.info = {"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S"), "phase": "starting"}
        self._write()
        return self

    def update(self, **kv: Any) -> None:
        self.info.update(kv)
        self._write()

    def _write(self) -> None:
        assert self.fd is not None
        data = json.dumps(self.info).encode("utf-8")
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.ftruncate(self.fd, 0)
            os.write(self.fd, data)
        except OSError:
            pass                                  # progress text only; the lock is what counts

    def __exit__(self, *exc: Any) -> None:
        if self.fd is None:
            return
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.ftruncate(self.fd, 0)              # no build: no progress text
        except OSError:
            pass
        _release(self.fd)
        os.close(self.fd)
        self.fd = None


def held(path: Path) -> bool:
    """Is a build holding this lock right now? (A missing file: no.) Takes and drops the lock to find out."""
    p = Path(path)
    if not p.is_file():
        return False
    try:
        fd = os.open(p, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    try:
        if _try(fd):
            _release(fd)
            return False
        return True
    finally:
        os.close(fd)


def read(path: Path) -> tuple[dict[str, Any], float | None]:
    """The progress a build wrote ({} when none) and when it last wrote it (the file's modification time)."""
    p = Path(path)
    try:
        info = json.loads(p.read_bytes().decode("utf-8") or "{}")
    except (OSError, ValueError):
        info = {}
    try:
        return (info if isinstance(info, dict) else {}), p.stat().st_mtime
    except OSError:
        return {}, None
