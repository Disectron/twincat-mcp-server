"""TwinCAT Scope: export a saved Scope recording (.svdx / .svd) to CSV with Beckhoff's TC3ScopeExportTool.exe.

The export tool ships with TE130x Scope View and TF3300 Scope Server (``…\\TwinCAT\\Functions\\TE130X-Scope-View``
or ``…\\TF3300-Scope-Server``) and is driven from the command line::

    TC3ScopeExportTool.exe "svd=<scope file>" target=<file.csv> silent

CSV needs no extra licence (TDMS/DAT need a full View or Server licence). Which channels, which time column and
whether channels of different sample times are aligned is decided by the export settings stored in the Scope
project — not here. This module only runs the tool, never edits the CSV, and writes a signed
``<csv>.provenance.json`` (source file + SHA-256, tool path + SHA-256, command line) next to the export, the same
way rec_export does (R10).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

TOOL_NAME = "TC3ScopeExportTool.exe"
_FUNCTION_DIRS = ("TE130X-Scope-View", "TE130x-Scope-View", "TF3300-Scope-Server")
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class ScopeError(RuntimeError):
    pass


def candidate_tools(configured: str | None = None) -> list[Path]:
    out: list[Path] = []
    if configured:
        out.append(Path(configured))
    roots: list[Path] = []
    tc = os.environ.get("TWINCAT3DIR")                 # …\TwinCAT\3.1\ → Functions is a sibling of 3.1
    if tc:
        roots.append(Path(tc).parent / "Functions")
    for pf in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), r"C:\Program Files (x86)", r"C:\Program Files"):
        if pf:
            roots.append(Path(pf) / "Beckhoff" / "TwinCAT" / "Functions")
    roots.append(Path(r"C:\TwinCAT\Functions"))
    for r in roots:
        for d in _FUNCTION_DIRS:
            p = r / d / TOOL_NAME
            if p not in out:
                out.append(p)
    return out


def find_tool(configured: str | None = None) -> Path | None:
    for p in candidate_tools(configured):
        if p.is_file():
            return p
    return None


def status(configured: str | None = None) -> dict[str, Any]:
    t = find_tool(configured)
    d: dict[str, Any] = {"export_tool": str(t) if t else None, "available": t is not None}
    if t is None:
        d["searched"] = [str(p) for p in candidate_tools(configured)]
        d["note"] = ("TC3ScopeExportTool.exe not found — it is installed with TE130x Scope View / TF3300 Scope Server; "
                     "set [scope] export_tool to its path if it lives elsewhere")
    return d


def export(svd: str | Path, target: str | Path, *, tool: Path, overwrite: bool = False, timeout_s: float = 600,
           key: bytes | None = None, version: str = "?") -> dict[str, Any]:
    from .recorder import _manifest_sig, file_sha256, make_writable, provenance_path   # one implementation of R10

    src = Path(svd)
    if not src.is_file():
        raise ScopeError(f"Scope file not found: {src}")
    if src.suffix.lower() not in (".svdx", ".svd"):
        raise ScopeError(f"{src.name}: expected a saved Scope recording (.svdx / .svd)")
    out = Path(target)
    if out.suffix.lower() != ".csv":
        raise ScopeError("target must be a .csv file (TDMS/DAT need a full Scope licence and are not offered here)")
    if out.exists() and not overwrite:
        raise ScopeError(f"{out} exists; pass overwrite=true or another target")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        make_writable(out)
        out.unlink()
    cmd = [str(tool), f"svd={src}", f"target={out}", "silent"]
    t0 = time.monotonic()
    try:
        r = subprocess.run(cmd, cwd=str(tool.parent), capture_output=True, text=True, timeout=timeout_s,
                           creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired as exc:
        raise ScopeError(f"{TOOL_NAME} did not finish within {timeout_s:.0f} s") from exc
    except OSError as exc:
        raise ScopeError(f"{TOOL_NAME} could not be started: {exc}") from exc
    elapsed = round(time.monotonic() - t0, 2)
    if not out.is_file() or out.stat().st_size == 0:
        raise ScopeError(f"{TOOL_NAME} exited with code {r.returncode} and wrote no CSV"
                         + (f": {(r.stderr or r.stdout).strip()[:400]}" if (r.stderr or r.stdout).strip() else "")
                         + " — is the file a Scope recording with data, and is the Scope View installation complete?")
    with open(out, "r", encoding="utf-8-sig", errors="replace") as fh:
        head = [fh.readline().rstrip("\r\n") for _ in range(3)]
        rows = 3 + sum(1 for _ in fh)
    ssha, ssize = file_sha256(src)
    esha, esize = file_sha256(out)
    tsha, _ = file_sha256(tool)
    prov: dict[str, Any] = {"_provenance": 1, "kind": "scope_export", "export_file": out.name, "export_sha256": esha,
                            "export_bytes": esize, "source_file": str(src), "source_sha256": ssha, "source_bytes": ssize,
                            "tool": str(tool), "tool_sha256": tsha, "command": cmd, "exit_code": r.returncode,
                            "exporter": f"twincat-mcp {version}", "created": dt.datetime.now(dt.timezone.utc).isoformat()}
    if key:
        prov["hmac"] = _manifest_sig(key, prov)
    pp = provenance_path(out)
    make_writable(pp)
    pp.write_text(json.dumps(prov, indent=1) + "\n", encoding="utf-8")
    return {"file": str(out), "bytes": esize, "lines": rows, "first_lines": [h for h in head if h], "source": str(src),
            "source_sha256": ssha, "export_sha256": esha, "provenance": str(pp), "exit_code": r.returncode,
            "elapsed_s": elapsed,
            "note": ("CSV written by Beckhoff's export tool, unchanged. Channels, time column and the alignment of "
                     "channels with different sample times follow the export settings saved in the Scope project.")}
