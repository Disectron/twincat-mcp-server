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
    assert p["command"][1:] == [f"svd={svd}", f"target={out}", "silent"] and p["hmac"]
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
