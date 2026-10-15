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
        try:
            data = _raw_rw(c, IG_FOPEN, mode | (PATH_GENERIC << 16), 4, path.encode("latin-1") + b"\x00")
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
            _raw_rw(c, IG_FDELETE, PATH_GENERIC << 16, 0, path.encode("latin-1") + b"\x00")
        except pyads.ADSError as exc:
            raise AdsError(f"FDELETE {path!r}: {_fmt_exc(exc)}") from exc
        return {"deleted": path}

    def list_dir(self, pattern: str, max_entries: int = 500) -> dict[str, Any]:
        """Directory listing via FFILEFIND. ``pattern`` like ``C:\\TwinCAT\\3.1\\Boot\\*``."""
        c = self._c()
        entries = []
        handle = 0
        try:
            data = _raw_rw(c, IG_FFIND, 0, 4 + 320, pattern.encode("latin-1") + b"\x00")
        except pyads.ADSError as exc:
            if exc.err_code in (1804, 0x70C):  # ERR_... no more files / not found
                return {"pattern": pattern, "entries": []}
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
            entries.append({
                "name": name,
                "is_dir": bool(attrs & 0x10),
                "size": (size_hi << 32) | size_lo,
                "modified": _filetime_iso(wtime),
                "attributes": f"0x{attrs:X}",
            })
            if len(entries) >= max_entries:
                break
            try:
                data = _raw_rw(c, IG_FFIND, handle, 4 + 320, b"")
            except pyads.ADSError as exc:
                if exc.err_code in (1804, 0x70C, 18, 1812):
                    break
                raise AdsError(f"FFILEFIND next: {_fmt_exc(exc)}") from exc
        return {"pattern": pattern, "entries": entries, "count": len(entries)}

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
