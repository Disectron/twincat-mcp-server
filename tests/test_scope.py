"""scope_export: runs Beckhoff's TC3ScopeExportTool.exe, never edits its CSV, writes a signed provenance file."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from twincat_mcp import scope as S
from twincat_mcp.recorder import file_sha256, provenance_path

FAKE = r'''import os, sys
args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
assert "silent" in sys.argv[1:]
src = open(args["svd"], "rb").read()
log = os.environ.get("TCMCP_FAKE_SCOPE_LOG")
if log:
    with open(log, "a", encoding="utf-8") as fh:
        fh.write("2026-10-19 15:24:24 [INFO] export started\n2026-10-19 15:24:25 [ERROR] Scope View: Server error on "
                 "ordering data.\nAds-Error 0xA2 (162)\n2026-10-19 15:24:26 [INFO] done\n")
if src.startswith(b"EMPTY"):
    sys.exit(3)
if not os.access(args["svd"], os.W_OK):     # what TC3ScopeExportTool did with rec_stop's read-only .svdx (N118)
    sys.exit(0)
with open(args["target"], "w", newline="") as fh:
    fh.write("Name;MAIN.nCycle\r\nTime;Value\r\n0;1\r\n1;2\r\n2;3\r\n")
'''


@pytest.fixture
def tool(tmp_path):
    d = tmp_path / "Functions" / "TE130X-Scope-View"
    d.mkdir(parents=True)
    script = d / "fake_export.py"
    script.write_text(FAKE)
    if os.name == "nt":
        exe = d / "TC3ScopeExportTool.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n')
    else:
        exe = d / S.TOOL_NAME
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
        exe.chmod(0o755)
    return exe


def test_export_runs_tool_and_records_provenance(tool, tmp_path):
    svd = tmp_path / "Scope Project.svdx"
    svd.write_bytes(b"SCOPEDATA" * 100)
    out = tmp_path / "out" / "scope.csv"
    r = S.export(svd, out, tool=tool, key=b"k" * 32, version="test")
    assert out.read_bytes() == b"Name;MAIN.nCycle\r\nTime;Value\r\n0;1\r\n1;2\r\n2;3\r\n"       # untouched
    assert r["lines"] == 5 and r["export_sha256"] == file_sha256(out)[0]
    p = json.loads(provenance_path(out).read_text(encoding="utf-8"))
    assert p["source_sha256"] == file_sha256(svd)[0] and p["tool_sha256"] == file_sha256(tool)[0]
    assert p["command"][1] == f"svd={svd}" and p["command"][3] == "silent" and p["hmac"]
    # the tool writes a work file beside the target, renamed when complete (an old CSV is never deleted first)
    assert p["command"][2] == f"target={out.with_name(p['written_as'])}" and p["written_as"].startswith(".scope.tcmcp-")
    assert sorted(x.name for x in out.parent.iterdir()) == sorted([out.name, provenance_path(out).name])
    with pytest.raises(S.ScopeError, match="exists"):
        S.export(svd, out, tool=tool)
    S.export(svd, out, tool=tool, overwrite=True)


def test_export_refuses_bad_input_and_reports_tool_failure(tool, tmp_path):
    with pytest.raises(S.ScopeError, match="not found"):
        S.export(tmp_path / "nope.svdx", tmp_path / "x.csv", tool=tool)
    other = tmp_path / "a.txt"
    other.write_text("x")
    with pytest.raises(S.ScopeError, match=r"\.svdx"):
        S.export(other, tmp_path / "x.csv", tool=tool)
    svd = tmp_path / "e.svdx"
    svd.write_bytes(b"EMPTY")
    with pytest.raises(S.ScopeError, match="exited with code 3 after .* and wrote no CSV; it printed nothing; source "
                                           "e.svdx: 5 bytes, writable"):
        S.export(svd, tmp_path / "e.csv", tool=tool)
    with pytest.raises(S.ScopeError, match="TDMS"):
        S.export(svd, tmp_path / "e.tdms", tool=tool)


def test_find_tool_in_twincat_functions(tool, monkeypatch, tmp_path):
    assert S.find_tool(str(tool)) == tool                                   # configured path
    monkeypatch.setenv("TWINCAT3DIR", str(tmp_path / "3.1"))
    if os.name != "nt":                                                     # found next to TwinCAT\3.1 by name
        assert S.find_tool() == tool
    monkeypatch.setenv("TWINCAT3DIR", str(tmp_path / "elsewhere" / "3.1"))
    st = S.status(str(tmp_path / "missing.exe"))
    if not st["available"]:
        assert "TE130x" in st["note"] and str(tmp_path / "missing.exe") in st["searched"]


def test_n118_a_read_only_scope_file_is_exported_from_a_writable_copy(tool, tmp_path):
    """N118 (0.8.12 retest): scope_export of the .svdx rec_stop saved — and set read-only like the recording — exited 0
    after 1 s without a CSV, while the tool exported a writable .svdx. A read-only source is exported from a
    byte-identical writable copy (deleted afterwards); the source stays read-only and is named in the provenance."""
    from twincat_mcp.recorder import is_read_only, make_writable
    import stat
    svd = tmp_path / "20261019-151850-10a4.svdx"
    svd.write_bytes(b"SCOPEDATA" * 100)
    os.chmod(svd, svd.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        out = tmp_path / "out" / "x.csv"
        r = S.export(svd, out, tool=tool, key=b"k" * 32, version="test")
        assert r["lines"] == 5 and r["exported_from_copy"]["sha256_identical"] is True
        assert is_read_only(svd) and r["source_sha256"] == file_sha256(svd)[0]
        assert sorted(p.name for p in out.parent.iterdir()) == sorted(["x.csv", provenance_path(out).name])  # copy gone
        prov = json.loads(provenance_path(out).read_text(encoding="utf-8"))
        assert prov["source_file"] == str(svd) and prov["exported_from_copy"]["copy"].endswith(svd.name)
    finally:
        make_writable(svd)


def test_scope_view_log_errors_are_reported(tool, tmp_path, monkeypatch):
    """The Scope View logs what goes wrong to TcScopeView.log ('Server error on ordering data … Router memory',
    Ads-Error 0xA2) — no tool result said so. The ERROR entries written during a call are reported with it."""
    log = tmp_path / "TcScopeView.log"
    log.write_text("2026-10-18 06:18:00 [ERROR] an old error, before the call\n", encoding="utf-8")
    monkeypatch.setenv("TCMCP_FAKE_SCOPE_LOG", str(log))
    svd = tmp_path / "a.svdx"
    svd.write_bytes(b"SCOPEDATA")
    r = S.export(svd, tmp_path / "a.csv", tool=tool, view_log=log)
    assert r["scope_view_log"]["errors"] == ["2026-10-19 15:24:25 [ERROR] Scope View: Server error on ordering data. "
                                             "| Ads-Error 0xA2 (162)"], r["scope_view_log"]
    bad = tmp_path / "e.svdx"
    bad.write_bytes(b"EMPTY")
    with pytest.raises(S.ScopeError, match=r"Scope View log, new errors: .*Ads-Error 0xA2"):
        S.export(bad, tmp_path / "e.csv", tool=tool, view_log=log)
    # rotated (shorter than at the mark), missing, UTF-16
    mark = S.log_mark(log)
    log.write_text("x [FATAL] restarted\n", encoding="utf-8")
    got = S.log_errors_since(mark)
    assert got["errors"] == ["x [FATAL] restarted"] and "rotated" in got["note"]
    assert "does not exist" in S.log_errors_since(S.log_mark(tmp_path / "none.log"))["note"]
    log.write_bytes("\ufeffa [ERROR] wide\r\n".encode("utf-16-le"))
    assert S.log_errors_since({"path": str(log), "size": 0})["errors"] == ["a [ERROR] wide"]


def test_review_scope_export_failures_are_said_never_raw(tool, tmp_path, monkeypatch):
    """Review of 0.8.13: a non-zero exit with a CSV was reported as plain success; a log without level tags gave
    'no new error'; a missing log at the start made the whole later log 'old'."""
    import subprocess as sp
    svd = tmp_path / "a.svdx"
    svd.write_bytes(b"SCOPEDATA")
    real = sp.run

    def exit_7(cmd, **kw):
        r = real(cmd, **kw)
        return sp.CompletedProcess(r.args, 7, r.stdout, "partial write")
    monkeypatch.setattr(S.subprocess, "run", exit_7)
    r = S.export(svd, tmp_path / "a.csv", tool=tool)
    assert "exited with code 7 although it wrote the CSV" in r["warning"] and "partial write" in r["warning"]
    monkeypatch.setattr(S.subprocess, "run", real)
    # a log in another format: its 'error' lines, and that the format was not recognised
    log = tmp_path / "TcScopeView.log"
    mark = S.log_mark(log)                                    # no log yet: everything written later is new
    assert mark["size"] == 0
    log.write_text("2026-10-19 15:24:25 Scope View: Server error on ordering data\nall fine\n", encoding="utf-8")
    got = S.log_errors_since(mark)
    assert got["errors"] == ["2026-10-19 15:24:25 Scope View: Server error on ordering data"]
    assert "format was not recognised" in got["note"]


REAL_LOG = (  # the shape of TcScopeView.log at 06:04 on 2026-10-20 (0.8.13 retest): padded tags, WARNINGs around
    "2026-10-20 06:04:31.112 [WARNING] TwinCAT State on local system changed: Run -> Config\n"
    "2026-10-20 06:04:49.870 [ INFO  ] Reply mode entered\n"
    "2026-10-20 06:04:50.201 [ ERROR ] Scope View: Server error on ordering data. This is often a hint at insufficient "
    "Router memory\n"
    "2026-10-20 06:04:50.202 [ ERROR ] Ads-Error 0xA2 (162)\n"
    "   at TwinCAT.Scope.Server.Order()\n"
    "2026-10-20 06:04:51.000 [WARNING] TwinCAT State on local system changed: Config -> Run\n"
    "2026-10-20 06:04:52.000 [ INFO  ] ErrorCount=0\n"
    "2026-10-20 06:04:53.000 [ FATAL ] view closed\n")


def test_n119_the_padded_tags_of_the_real_log_are_read(tmp_path):
    """N119 (0.8.13 retest, S2): the real log pads its tags ('[ ERROR ]', '[ INFO  ]') — the regex allowed no spaces,
    so the padded errors were never read, and the '[WARNING]' a mode change always writes turned the fallback off: a
    window with 2 router-memory / 0xA2 errors reported error_count 0. '[ INFO  ] ErrorCount=0' is no error; '[ FATAL ]'
    is one without the word; '[ERROR] a' then '[ ERROR ] b' are two entries."""
    log = tmp_path / "TcScopeView.log"
    log.write_text("2026-10-20 06:00:00.000 [ ERROR ] before the call\n", encoding="utf-8")
    mark = S.log_mark(log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(REAL_LOG)
    got = S.log_errors_since(mark)
    assert got["error_count"] == 3 and "note" not in got, got
    assert got["errors"] == [
        "2026-10-20 06:04:50.201 [ ERROR ] Scope View: Server error on ordering data. This is often a hint at "
        "insufficient Router memory",
        "2026-10-20 06:04:50.202 [ ERROR ] Ads-Error 0xA2 (162) | at TwinCAT.Scope.Server.Order()",
        "2026-10-20 06:04:53.000 [ FATAL ] view closed"]
    log.write_text("x [ERROR] a\ny [ ERROR ] b\n", encoding="utf-8")
    assert S.log_errors_since({"path": str(log), "size": 0})["errors"] == ["x [ERROR] a", "y [ ERROR ] b"]


def test_n140_the_logs_nul_padding_is_no_text(tmp_path):
    """N140 (0.8.14 retest, S4): each scope_view_log error string carried the log's NUL padding (hundreds of U+0000).
    NUL is no text of the log: an entry ends where its text ends, a line of NULs is no continuation line. The mark is
    where the text ends, so an entry written over trailing padding (in place, below the file's size) is still read."""
    log = tmp_path / "TcScopeView.log"
    pad = "\x00" * 600
    log.write_bytes(("t [ INFO  ] before\n" + pad).encode("utf-8"))          # bytes: no '\r\n' on Windows
    mark = S.log_mark(log)
    assert mark["size"] == len("t [ INFO  ] before\n")
    with open(log, "r+b") as fh:                              # written in place, over the padding
        fh.seek(mark["size"])
        fh.write(("t [ ERROR ] Invalid ScopeState!" + "\x00" * 300 + "\r\n" + pad[:200] + "\n"
                  "t [ ERROR ] second\n").encode("utf-8"))
    got = S.log_errors_since(mark)
    assert got["errors"] == ["t [ ERROR ] Invalid ScopeState!", "t [ ERROR ] second"], got
    assert all("\x00" not in e for e in got["errors"])
    wide = ("\ufeff" + "t [ ERROR ] wide" + "\x00" * 40 + "\n").encode("utf-16-le")
    log.write_bytes(wide + b"\x00" * 64)
    assert S.log_errors_since({"path": str(log), "size": 2})["errors"] == ["t [ ERROR ] wide"]
    assert S.log_mark(log)["size"] == len(wide) - 1          # after the '\n' byte, the last that is not NUL
    # review of N140: the mark is below the padded file's size — a log rotated to a new one that grew past the mark is
    # told by its beginning (else its first entries were skipped); new_bytes counts text, not padding
    log.write_text("2026-10-20 t [ INFO  ] old\n" + pad, encoding="utf-8")
    mark = S.log_mark(log)
    assert S.log_errors_since(mark)["new_bytes"] == 0
    log.write_text("2026-10-21 t [ ERROR ] in the new log\n" + "t [ INFO  ] filler\n" * 3, encoding="utf-8")
    got = S.log_errors_since(mark)
    assert got["errors"] == ["2026-10-21 t [ ERROR ] in the new log"] and "a new log" in got["note"], got


def test_review_0813_a_long_log_window_is_read_whole(tmp_path, monkeypatch):
    """Scope audit (0.8.13 retest): more than 4 MiB of new log → only the last 4 MiB were read; an error before them
    was lost. The window is read in chunks, all of it — also UTF-16 and a line cut by a chunk border."""
    monkeypatch.setattr(S, "_LOG_CHUNK", 64)
    log = tmp_path / "TcScopeView.log"
    body = "t [ ERROR ] first\n" + "t [ INFO  ] filler line number xx\n" * 50 + "t [ ERROR ] last\n"
    log.write_text(body, encoding="utf-8")
    assert S.log_errors_since({"path": str(log), "size": 0})["errors"] == ["t [ ERROR ] first", "t [ ERROR ] last"]
    log.write_bytes(("﻿" + body).encode("utf-16-le"))
    assert S.log_errors_since({"path": str(log), "size": 2})["errors"] == ["t [ ERROR ] first", "t [ ERROR ] last"]


def test_review_0813_a_failed_export_keeps_the_old_csv_and_a_sealed_one_is_never_replaced(tool, tmp_path):
    """Scope audit (0.8.13 retest): overwrite=true deleted the old CSV before the tool ran — a failed export lost it;
    a CSV rec_stop sealed (read-only, its SHA in the signed .scope.json) could be replaced, breaking the seal."""
    from twincat_mcp.recorder import make_writable
    import stat as _stat
    svd, bad = tmp_path / "a.svdx", tmp_path / "e.svdx"
    svd.write_bytes(b"SCOPEDATA")
    bad.write_bytes(b"EMPTY")
    out = tmp_path / "a.csv"
    S.export(svd, out, tool=tool)
    before = out.read_bytes()
    with pytest.raises(S.ScopeError, match="wrote no CSV.*a.csv is unchanged"):
        S.export(bad, out, tool=tool, overwrite=True)
    assert out.read_bytes() == before
    assert sorted(x.name for x in tmp_path.iterdir() if x.name.startswith(".a.tcmcp")) == []     # no work file left
    os.chmod(out, out.stat().st_mode & ~(_stat.S_IWUSR | _stat.S_IWGRP | _stat.S_IWOTH))      # sealed by rec_stop
    try:
        with pytest.raises(S.ScopeError, match="sealed .* never replaced"):
            S.export(svd, out, tool=tool, overwrite=True)
        assert out.read_bytes() == before
    finally:
        make_writable(out)


def _scope_line(text: str, pad: int = 120) -> bytes:
    """A line as TE130x writes it: the text, its newline, then NUL padding (N140)."""
    return ("\n" + text).encode("utf-8") + b"\x00" * pad


def test_n179_every_numbered_scope_view_log_is_read(tmp_path):
    """N179 (0.8.15 retest, S2): from the evening a second XAE shell opened, TE130x wrote every line to
    TcScopeView_1.log; the plugin read only TcScopeView.log and reported two recordings' Ads-Error 0xA2 lines as
    error_count 0. A1: the old log unchanged, _1 gains two padded ERROR lines → 2, each naming _1. A2: a _2 that
    appears after the mark counts from its start. A4: no log written → error_count 0 and said so."""
    folder = tmp_path / "TE130X-Scope-View"
    folder.mkdir()
    main, one = folder / "TcScopeView.log", folder / "TcScopeView_1.log"
    main.write_bytes(_scope_line("(2026-10-20-21:00:57:001)[ INFO  ] Scope View: closing"))
    one.write_bytes(_scope_line("(2026-10-21-15:15:31:000)[ INFO  ] Scope Client: Single Client State change from "
                                "'ROUTERSTOP' to 'AUTOSTOP' requested from '10.0.0.1.1.1'"))
    mark = S.log_mark(main)
    got = S.log_errors_since(mark)                                      # A4
    assert got["error_count"] == 0 and got["note"].startswith("no Scope View log was written"), got
    with open(one, "ab") as fh:
        fh.write(_scope_line("(2026-10-21-15:15:32:727)[ ERROR ] Scope View: Server error on ordering data. This is "
                             "often a hint at insufficient Router memory.TwinCAT.Scope2.ScopeClient.ReceiveMultiGroup"))
        fh.write(_scope_line("(2026-10-21-15:15:33:038)[ ERROR ] Scope Client: TwinCAT.Ads.AdsErrorException- "
                             "Ads-Error 0xA2 - A unknown Ads-Error has occurred. ('162' has occurred (AdsErrorCode- 162, "
                             "0xA2))"))
    got = S.log_errors_since(mark)                                      # A1
    assert got["error_count"] == 2 and got["error_logs"] == ["TcScopeView_1.log"] * 2, got
    assert got["log"] == str(one) and got["logs_written"] == ["TcScopeView_1.log"], got
    assert "\x00" not in "".join(got["errors"]) and "0xA2" in got["errors"][1], got
    assert set(got["logs_checked"]) == {"TcScopeView.log", "TcScopeView_1.log"}, got
    (folder / "TcScopeView_2.log").write_bytes(_scope_line("(2026-10-21-15:30:19:037)[ ERROR ] Scope Client: "
                                                           "Ads-Error 0xA2"))
    got = S.log_errors_since(mark)                                      # A2
    assert got["error_count"] == 3 and got["error_logs"][-1] == "TcScopeView_2.log", got
    # [scope] view_log set by the user: honoured, its numbered siblings read too
    custom = tmp_path / "mine" / "Scope.log"
    custom.parent.mkdir()
    m2 = S.log_mark(custom)
    (custom.parent / "Scope_1.log").write_bytes(_scope_line("(x)[ ERROR ] boom"))
    assert S.log_errors_since(m2)["error_logs"] == ["Scope_1.log"]


def _te130x_log(path, lines):
    """A TcScopeView.log as TE130x writes it (N185, 0.8.16 retest): a 12-byte binary header whose bytes 4–7 are the
    log's length (u32 LE), then the text."""
    import struct
    text = "\n" + "".join(ln + "\n" for ln in lines)
    body = text.encode("utf-8")
    size = 12 + len(body)
    path.write_bytes(b"\x00\x04\x00\x00" + struct.pack("<I", size) + b"\x00\x00\x00\x00" + body)


def test_n185_the_log_header_is_no_new_log(tmp_path):
    """N185: the header's length field changes with every write — the fingerprint of the first bytes did too, and
    every call read the whole log as new: 23 ERRORs of earlier days reported as this call's."""
    import re
    from twincat_mcp import scope as S
    log = tmp_path / "TcScopeView.log"
    old = [f"(2026-10-18-0{i}:00:00:000)[ ERROR ] Scope View: Ads-Error 0xA2 old {i}" for i in range(23)]
    _te130x_log(log, ["(2026-04-03-08:03:33:268)[ INFO ] Scope View: Scope Project created"] + old)
    mark = S.log_mark(log)
    assert mark["head_at"] == 13, mark
    new = ["(2026-10-21-22:43:32:332)[ ERROR ] Scope View: Invalid ScopeState! Config requested, actual- Reply"]
    _te130x_log(log, ["(2026-04-03-08:03:33:268)[ INFO ] Scope View: Scope Project created"] + old + new)
    got = S.log_errors_since(mark)
    assert got["error_count"] == 1 and "Invalid ScopeState" in got["errors"][0] and "new log" not in got.get("note", ""), got
    rx = re.compile(r"Invalid ScopeState", re.I)
    assert [ln for _f, ln in S.log_find_since(mark, rx)] == [new[0]]
    # a log that really is new (rotated: another first line) is still read from its start
    _te130x_log(log, ["(2026-10-21-23:00:00:000)[ INFO ] Scope View: a new log"] + new + ["x" * 3000])
    got = S.log_errors_since(mark)                    # longer than at the mark, another first line: a new log
    assert got["error_count"] == 1 and "a new log) — read from its start" in got["note"], got


def test_n185_more_errors_than_shown_say_truncated(tmp_path):
    from twincat_mcp import scope as S
    log = tmp_path / "TcScopeView.log"
    _te130x_log(log, ["(2026-04-03-08:03:33:268)[ INFO ] start"])
    mark = S.log_mark(log)
    _te130x_log(log, ["(2026-04-03-08:03:33:268)[ INFO ] start"] +
                [f"(2026-10-21-22:00:{i:02d}:000)[ ERROR ] e{i}" for i in range(23)])
    got = S.log_errors_since(mark)
    assert got["error_count"] == 23 and len(got["errors"]) == 20 and got["truncated"] is True, got
