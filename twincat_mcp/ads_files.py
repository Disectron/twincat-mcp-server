"""TwinCAT System Service (ADS port 10000) helpers.

The System Service exposes a small file system / OS API over ADS, which is
how ``Tc2_System`` ``FB_FileOpen``/``FB_FileRead``/``NT_StartProcess`` work
internally. Using it directly lets an agent fetch TcUnit results, logs,
boot-project archives or config files **from the target IPC** without a
share, SSH or RDP — and, in full mode, start a process or reboot the IPC.

Index groups (Tc2_System constants):
    SYSTEMSERVICE_FOPEN      120   SYSTEMSERVICE_FCLOSE   121
    SYSTEMSERVICE_FREAD      122   SYSTEMSERVICE_FWRITE   123
    SYSTEMSERVICE_FSEEK      124   SYSTEMSERVICE_FTELL    125
    SYSTEMSERVICE_FDELETE    131   SYSTEMSERVICE_FRENAME  132
    SYSTEMSERVICE_FFILEFIND  133   SYSTEMSERVICE_MKDIR    138
    SYSTEMSERVICE_RMDIR      139   SYSTEMSERVICE_STARTPROCESS 500
FOPEN index offset = mode flags | (ePath << 16), ePath 1 = PATH_GENERIC.
Mode flags: READ 0x1  WRITE 0x2  APPEND 0x4  PLUS 0x8  BINARY 0x10  TEXT 0x20.

``FFILEFIND`` returns a 4-byte handle followed by a WIN32_FIND_DATAA (320 B);
that layout is what Beckhoff's own .NET client uses. Verify once on a real
target — the directory listing is the least battle-tested function here.
"""
from __future__ import annotations

import ctypes
import re
import struct
import time
from typing import Any

import pyads
from pyads import constants as C
from pyads.pyads_ex import _adsDLL  # type: ignore[attr-defined]

from .ads_backend import AdsBackend, AdsError, SYSTEM_SERVICE_PORT, _fmt_exc

IG_FOPEN, IG_FCLOSE, IG_FREAD, IG_FWRITE = 120, 121, 122, 123
IG_FSEEK, IG_FTELL, IG_FDELETE, IG_FRENAME, IG_FFIND = 124, 125, 131, 132, 133
IG_MKDIR, IG_RMDIR, IG_STARTPROCESS = 138, 139, 500
PATH_GENERIC = 1
# ADS System Service path IDs (indexOffset high word of FOPEN/FFIND/FDELETE/FRENAME): a file name is then
# relative to a folder the target resolves itself, which is what makes one config work on Windows
# (C:\TwinCAT\3.1\Boot), CE7 (\Hard Disk\TwinCAT\3.1\Boot) and TC/BSD (/usr/local/etc/TwinCAT/3.1/Boot).
PATH_IDS = {"generic": 1, "bootprj": 2, "bootdata": 3, "boot": 4, "target": 5, "config": 6, "install": 7,
            **{f"user{i}": 10 + i for i in range(1, 10)}}


def split_path(path: str) -> tuple[int, str]:
    """``'boot:TcUnit_xUnit_testresults.xml'`` -> (PATH_BOOTPATH, 'TcUnit_xUnit_testresults.xml');
    anything without a known ``<id>:`` prefix (incl. a drive letter) is PATH_GENERIC with the string as given."""
    m = re.match(r"^([A-Za-z][A-Za-z0-9]*):(?![\\/])(.*)$", path or "")
    if m and m.group(1).lower() in PATH_IDS:
        return PATH_IDS[m.group(1).lower()], m.group(2)
    return PATH_GENERIC, path
MODE_READ, MODE_WRITE, MODE_APPEND, MODE_PLUS, MODE_BINARY, MODE_TEXT = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20

_CHUNK = 64 * 1024


def _raw_rw(conn: pyads.Connection, igrp: int, ioffs: int, read_len: int,
            write_bytes: bytes) -> bytes:
    """ReadWrite returning exactly the bytes the server sent (bytes_read)."""
    fn = _adsDLL.AdsSyncReadWriteReqEx2
    addr = ctypes.pointer(conn._adr.amsAddrStruct())  # type: ignore[attr-defined]
    rbuf = (ctypes.c_ubyte * max(read_len, 1))()
    wbuf = (ctypes.c_ubyte * max(len(write_bytes), 1))(*write_bytes) if write_bytes else None
    bytes_read = ctypes.c_ulong(0)
    err = fn(conn._port, addr, ctypes.c_ulong(igrp), ctypes.c_ulong(ioffs),  # type: ignore[attr-defined]
             ctypes.c_ulong(read_len), ctypes.pointer(rbuf) if read_len else None,
             ctypes.c_ulong(len(write_bytes)), ctypes.pointer(wbuf) if wbuf else None,
             ctypes.pointer(bytes_read))
    if err:
        raise pyads.ADSError(err)
    return bytes(rbuf[:bytes_read.value])


class SystemServiceFiles:
    def __init__(self, ads: AdsBackend):
        self.ads = ads

    def _c(self) -> pyads.Connection:
        return self.ads.conn(SYSTEM_SERVICE_PORT)

    # ------------------------------------------------------------ primitives
    def fopen(self, path: str, mode: int) -> int:
        c = self._c()
        pid, name = split_path(path)
        try:
            data = _raw_rw(c, IG_FOPEN, mode | (pid << 16), 4, name.encode("latin-1") + b"\x00")
        except pyads.ADSError as exc:
            raise AdsError(f"FOPEN {path!r}: {_fmt_exc(exc)}") from exc
        return struct.unpack("<I", data[:4])[0]

    def fclose(self, handle: int) -> None:
        c = self._c()
        try:
            _raw_rw(c, IG_FCLOSE, handle, 0, b"")
        except pyads.ADSError as exc:
            raise AdsError(f"FCLOSE: {_fmt_exc(exc)}") from exc

    def fread(self, handle: int, n: int) -> bytes:
        c = self._c()
        try:
            return _raw_rw(c, IG_FREAD, handle, n, b"")
        except pyads.ADSError as exc:
            raise AdsError(f"FREAD: {_fmt_exc(exc)}") from exc

    def fwrite(self, handle: int, data: bytes) -> int:
        c = self._c()
        try:
            out = _raw_rw(c, IG_FWRITE, handle, 4, data)
        except pyads.ADSError as exc:
            raise AdsError(f"FWRITE: {_fmt_exc(exc)}") from exc
        return struct.unpack("<I", out[:4])[0] if len(out) >= 4 else len(data)

    # ------------------------------------------------------------ high level
    def read_file(self, path: str, max_bytes: int | None = None, as_text: bool = True,
                  encoding: str = "utf-8") -> dict[str, Any]:
        limit = max_bytes or self.ads.cfg.max_file_bytes
        h = self.fopen(path, MODE_READ | MODE_BINARY)
        chunks: list[bytes] = []
        total = 0
        truncated = False
        try:
            while True:
                want = min(_CHUNK, limit - total)
                if want <= 0:
                    truncated = True
                    break
                part = self.fread(h, want)
                if not part:
                    break
                chunks.append(part)
                total += len(part)
                if len(part) < want:
                    break
        finally:
            self.fclose(h)
        data = b"".join(chunks)
        out: dict[str, Any] = {"path": path, "bytes": len(data), "truncated": truncated}
        if as_text:
            try:
                out["text"] = data.decode(encoding)
            except UnicodeDecodeError:
                out["text"] = data.decode("latin-1", "replace")
                out["note"] = f"not valid {encoding}; decoded as latin-1"
        else:
            import base64
            out["base64"] = base64.b64encode(data).decode()
        return out

    def write_file(self, path: str, data: bytes, append: bool = False) -> dict[str, Any]:
        mode = (MODE_APPEND if append else MODE_WRITE) | MODE_BINARY
        h = self.fopen(path, mode)
        written = 0
        try:
            for i in range(0, len(data), _CHUNK):
                written += self.fwrite(h, data[i:i + _CHUNK])
        finally:
            self.fclose(h)
        return {"path": path, "written": written}

    def delete_file(self, path: str) -> dict[str, Any]:
        c = self._c()
        try:
            pid, name = split_path(path)
            _raw_rw(c, IG_FDELETE, pid << 16, 0, name.encode("latin-1") + b"\x00")
        except pyads.ADSError as exc:
            raise AdsError(f"FDELETE {path!r}: {_fmt_exc(exc)}") from exc
        return {"deleted": path}

    # FFILEFIND ignores the path id in the index offset (observed on 3.1.4026: 'boot:*' listed System32, the
    # service's working directory). So a prefixed *listing* pattern is resolved to an absolute folder by
    # probing the known locations once per target; FOPEN honours the path id and needs no resolution.
    _PREFIX_DIRS: dict[str, list[str]] = {
        "boot": [r"C:\TwinCAT\3.1\Boot", r"\Hard Disk\TwinCAT\3.1\Boot", "/usr/local/etc/TwinCAT/3.1/Boot"],
        "bootprj": [r"C:\TwinCAT\3.1\Boot", r"\Hard Disk\TwinCAT\3.1\Boot", "/usr/local/etc/TwinCAT/3.1/Boot"],
        "bootdata": [r"C:\TwinCAT\3.1\Boot", r"\Hard Disk\TwinCAT\3.1\Boot", "/usr/local/etc/TwinCAT/3.1/Boot"],
        "config": [r"C:\TwinCAT\3.1\Config", r"\Hard Disk\TwinCAT\3.1\Config", "/usr/local/etc/TwinCAT/3.1/Config"],
        "target": [r"C:\TwinCAT\3.1\Target", r"\Hard Disk\TwinCAT\3.1\Target", "/usr/local/etc/TwinCAT/3.1/Target"],
        "install": [r"C:\TwinCAT\3.1", r"\Hard Disk\TwinCAT\3.1", "/usr/local/etc/TwinCAT/3.1"],
    }

    def resolve_prefix_dir(self, prefix: str) -> str:
        """Absolute folder behind 'boot:' etc. on the connected target, probed once and cached."""
        key = (prefix.lower(), self.ads.target_label())
        cache = getattr(self, "_prefix_cache", None)
        if cache is None:
            cache = self._prefix_cache = {}
        if key in cache:
            return cache[key]
        cands = self._PREFIX_DIRS.get(prefix.lower())
        if not cands:
            raise AdsError(f"no folder is known for the prefix {prefix!r}: ipc_list_dir needs an absolute path here "
                           f"(ipc_read_file accepts the prefix; the target resolves it for FOPEN)")
        for d in cands:
            sep = "/" if d.startswith("/") else "\\"
            try:
                probe = self._ffind_raw(d + sep + "*", 1)
            except AdsError:
                continue
            if probe:
                cache[key] = d
                return d
        raise AdsError(f"none of the known folders for {prefix!r} exist on the target: {cands}. Pass the absolute path.")

    def _ffind_raw(self, pattern: str, max_entries: int) -> list[dict[str, Any]]:
        c = self._c()
        entries: list[dict[str, Any]] = []
        try:
            data = _raw_rw(c, IG_FFIND, 0, 4 + 320, pattern.encode("latin-1") + b"\x00")
        except pyads.ADSError as exc:
            if exc.err_code in (1804, 0x70C):  # no more files / not found
                return []
            raise AdsError(f"FFILEFIND {pattern!r}: {_fmt_exc(exc)}") from exc
        while True:
            if len(data) < 4 + 44:
                break
            handle = struct.unpack_from("<I", data, 0)[0]
            fd = data[4:]
            attrs, = struct.unpack_from("<I", fd, 0)
            ctime, atime, wtime = struct.unpack_from("<QQQ", fd, 4)
            size_hi, size_lo = struct.unpack_from("<II", fd, 28)
            name = fd[44:44 + 260].split(b"\x00", 1)[0].decode("latin-1", "replace")
            if name not in (".", ".."):
                entries.append({"name": name, "is_dir": bool(attrs & 0x10), "size": (size_hi << 32) | size_lo,
                                "modified": _filetime_iso(wtime), "attributes": f"0x{attrs:X}"})
            if len(entries) >= max_entries:
                entries.append({"_truncated": True})
                break
            try:
                data = _raw_rw(c, IG_FFIND, handle, 4 + 320, b"")
            except pyads.ADSError as exc:
                if exc.err_code in (1804, 0x70C, 18, 1812):
                    break
                raise AdsError(f"FFILEFIND next: {_fmt_exc(exc)}") from exc
        return entries

    def list_dir(self, pattern: str, max_entries: int = 500) -> dict[str, Any]:
        """Directory listing via FFILEFIND. ``pattern`` like ``C:\\TwinCAT\\3.1\\Boot\\*`` or, OS-independent,
        ``boot:*`` — the prefix is resolved to the target's real folder (probed once), '.'/'..' are dropped,
        ``truncated`` says whether ``max_entries`` cut the list."""
        pid, name = split_path(pattern)
        resolved = None
        if pid != PATH_GENERIC:
            prefix = pattern.split(":", 1)[0]
            folder = self.resolve_prefix_dir(prefix)
            sep = "/" if folder.startswith("/") else "\\"
            resolved = folder + sep + (name or "*")
        raw = self._ffind_raw(resolved or pattern, max_entries)
        truncated = bool(raw and raw[-1].get("_truncated"))
        entries = [e for e in raw if not e.get("_truncated")]
        out = {"pattern": pattern, "entries": entries, "count": len(entries), "truncated": truncated}
        if resolved:
            out["resolved_pattern"] = resolved
        return out

    def start_process(self, path: str, directory: str = "", command_line: str = "") -> dict[str, Any]:
        """NT_StartProcess equivalent: launches ``path`` on the target."""
        c = self._c()
        p, d, cl = (s.encode("latin-1") for s in (path, directory, command_line))
        payload = struct.pack("<III", len(p), len(d), len(cl)) + p + b"\x00" + d + b"\x00" + cl + b"\x00"
        try:
            c.write(IG_STARTPROCESS, 0, (ctypes.c_ubyte * len(payload))(*payload), ctypes.c_ubyte * len(payload))
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"STARTPROCESS {path!r}: {_fmt_exc(exc)}") from exc
        return {"started": path, "directory": directory, "command_line": command_line}

    def reboot(self, delay_s: int = 0, reboot: bool = True) -> dict[str, Any]:
        """NT_Reboot / NT_Shutdown equivalent via ADSSTATE_SHUTDOWN on port 10000."""
        c = self._c()
        try:
            c.write_control(C.ADSSTATE_SHUTDOWN, 1 if reboot else 0, delay_s, C.PLCTYPE_UDINT)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"shutdown/reboot request failed: {_fmt_exc(exc)}") from exc
        self.ads.disconnect()
        return {"requested": "reboot" if reboot else "shutdown", "delay_s": delay_s}

    def wait_for_file(self, path: str, timeout_s: float, poll_s: float = 2.0) -> dict[str, Any]:
        t0 = time.time()
        last_err = ""
        while time.time() - t0 < timeout_s and not self.ads.cancel.is_set():
            try:
                h = self.fopen(path, MODE_READ | MODE_BINARY)
                self.fclose(h)
                return {"exists": True, "waited_s": round(time.time() - t0, 1)}
            except AdsError as exc:
                last_err = str(exc)
            time.sleep(poll_s)
        return {"exists": False, "waited_s": round(time.time() - t0, 1), "last_error": last_err}


def _filetime_iso(ft: int) -> str | None:
    if not ft:
        return None
    import datetime as dt
    try:
        return (dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=ft // 10)).isoformat()
    except (OverflowError, ValueError):
        return None
