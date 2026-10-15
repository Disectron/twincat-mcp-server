import struct

import pytest
import pyads

from twincat_mcp.config import AdsConfig
from twincat_mcp.ads_backend import AdsBackend, AdsError
from twincat_mcp import ads_files as F
from tests.fake_ads import install


class FakeFS:
    """Simulates the System Service file API behind _raw_rw."""

    def __init__(self):
        self.files = {r"C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml": b"<testsuites/>" * 10000}
        self.handles = {}
        self.next = 1
        self.calls = []
        self.find_entries = []

    def rw(self, conn, igrp, ioffs, read_len, write_bytes):
        self.calls.append((igrp, ioffs, read_len, len(write_bytes)))
        if igrp == F.IG_FOPEN:
            path = write_bytes.rstrip(b"\x00").decode()
            mode = ioffs & 0xFFFF
            assert ioffs >> 16 == F.PATH_GENERIC
            if mode & F.MODE_READ and path not in self.files:
                raise pyads.ADSError(1804)
            if mode & (F.MODE_WRITE | F.MODE_APPEND):
                if not (mode & F.MODE_APPEND):
                    self.files[path] = b""
                self.files.setdefault(path, b"")
            h = self.next; self.next += 1
            self.handles[h] = [path, 0]
            return struct.pack("<I", h)
        if igrp == F.IG_FREAD:
            path, pos = self.handles[ioffs]
            data = self.files[path][pos:pos + read_len]
            self.handles[ioffs][1] += len(data)
            return data
        if igrp == F.IG_FWRITE:
            path, pos = self.handles[ioffs]
            self.files[path] += write_bytes
            return struct.pack("<I", len(write_bytes))
        if igrp == F.IG_FCLOSE:
            self.handles.pop(ioffs)
            return b""
        if igrp == F.IG_FDELETE:
            path = write_bytes.rstrip(b"\x00").decode()
            if path not in self.files:
                raise pyads.ADSError(1804)
            del self.files[path]
            return b""
        if igrp == F.IG_FFIND:
            if ioffs == 0:
                self._iter = iter(self.find_entries)
                handle = 77
            else:
                handle = ioffs
            try:
                name, size, is_dir = next(self._iter)
            except StopIteration:
                raise pyads.ADSError(1804)
            fd = struct.pack("<I", 0x10 if is_dir else 0x20) + struct.pack("<QQQ", 0, 0, 133_000_000_000_000_000)
            fd += struct.pack("<II", 0, size) + b"\x00" * 8 + name.encode().ljust(260, b"\x00") + b"\x00" * 14
            return struct.pack("<I", handle) + fd
        raise pyads.ADSError(1793)


@pytest.fixture
def files(monkeypatch):
    install(monkeypatch)
    fs = FakeFS()
    monkeypatch.setattr(F, "_raw_rw", fs.rw)
    ads = AdsBackend(AdsConfig(ams_net_id="5.1.2.3.1.1"))
    return F.SystemServiceFiles(ads), fs


def test_read_file_chunked(files):
    svc, fs = files
    r = svc.read_file(r"C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml")
    assert r["bytes"] == 13 * 10000 and r["text"].startswith("<testsuites/>")
    assert not r["truncated"]
    assert not fs.handles  # closed
    r2 = svc.read_file(r"C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml", max_bytes=100)
    assert r2["bytes"] == 100 and r2["truncated"]


def test_read_missing(files):
    svc, fs = files
    with pytest.raises(AdsError, match="FOPEN"):
        svc.read_file(r"C:\nope.txt")


def test_write_append_delete(files):
    svc, fs = files
    svc.write_file(r"C:\log.txt", b"abc")
    svc.write_file(r"C:\log.txt", b"def", append=True)
    assert fs.files[r"C:\log.txt"] == b"abcdef"
    assert svc.delete_file(r"C:\log.txt")["deleted"]
    assert r"C:\log.txt" not in fs.files


def test_list_dir(files):
    svc, fs = files
    fs.find_entries = [("Boot", 0, True), ("CurrentConfig.xml", 1234, False)]
    r = svc.list_dir(r"C:\TwinCAT\3.1\*")
    assert [e["name"] for e in r["entries"]] == ["Boot", "CurrentConfig.xml"]
    assert r["entries"][0]["is_dir"] and r["entries"][1]["size"] == 1234
    assert r["entries"][1]["modified"].startswith("20")


def test_start_process_payload(files, monkeypatch):
    svc, fs = files
    from tests.fake_ads import FakeConnection
    r = svc.start_process(r"C:\Windows\System32\cmd.exe", "C:\\", "/c dir")
    conn = FakeConnection.instances[-1]
    tag, payload = conn.writes[-1]
    assert tag.startswith("raw:1F4/0")  # 500
    lp, ld, lc = struct.unpack("<III", payload[:12])
    assert (lp, ld, lc) == (len(r"C:\Windows\System32\cmd.exe"), 3, 6)
    assert payload[12:].split(b"\x00")[:3] == [b"C:\\Windows\\System32\\cmd.exe", b"C:\\", b"/c dir"]


def test_wait_for_file(files):
    svc, fs = files
    assert svc.wait_for_file(r"C:\TwinCAT\3.1\Boot\TcUnit_xUnit_testresults.xml", 1, 0.01)["exists"]
    assert not svc.wait_for_file(r"C:\never.xml", 0.05, 0.01)["exists"]
