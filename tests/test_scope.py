"""scope_export: runs Beckhoff's TC3ScopeExportTool.exe, never edits its CSV, writes a signed provenance file."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from twincat_mcp import scope as S
from twincat_mcp.recorder import file_sha256, provenance_path

FAKE = r'''import sys
args = dict(a.split("=", 1) for a in sys.argv[1:] if "=" in a)
assert "silent" in sys.argv[1:]
src = open(args["svd"], "rb").read()
if src.startswith(b"EMPTY"):
    sys.exit(3)
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
    with pytest.raises(S.ScopeError, match="exited with code 3 and wrote no CSV"):
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
