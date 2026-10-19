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
import re
import shutil
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


# -- the Scope View's own log ------------------------------------------------------------------------------------------
# TE130x writes what goes wrong to TcScopeView.log (0.8.12 retest: "Server error on ordering data … insufficient Router
# memory", Ads-Error 0xA2) — nothing of it reached a tool result. A call marks where the log ends when it begins and
# reports the ERROR entries written after that mark.
_LEVEL = re.compile(r"\[(ERROR|FATAL|WARN(?:ING)?|INFO|DEBUG|TRACE|VERBOSE)\]", re.I)
_LOG_READ_MAX = 4 << 20


def view_log_path(configured: str | None = None) -> Path:
    """[scope] view_log, else %ProgramData%\\Beckhoff\\TwinCAT\\Functions\\TE130X-Scope-View\\TcScopeView.log."""
    if configured:
        return Path(configured)
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "Beckhoff" / "TwinCAT" / "Functions" / \
        "TE130X-Scope-View" / "TcScopeView.log"


def log_mark(path: Path | None) -> dict[str, Any]:
    """Where the Scope View log ends now ({"path", "size"}; size None: no log yet)."""
    if path is None:
        return {"path": None, "size": None}
    try:
        return {"path": str(path), "size": path.stat().st_size}
    except OSError:
        return {"path": str(path), "size": None}


def _decode(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or (len(raw) > 3 and raw[1] == 0 and raw[3] == 0 and raw[0] != 0):
        return raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    return raw.decode("utf-8", errors="replace").lstrip("\ufeff")


def log_errors_since(mark: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    """The ERROR / FATAL entries the Scope View log got after `mark` (an entry: its line and up to 5 following lines
    without a level tag). {"log", "errors": [...], "error_count", "new_bytes"}; "note" when the log is missing or was
    rotated (shorter than at the mark: read from its start)."""
    p = mark.get("path")
    if not p:
        return {"log": None, "errors": [], "note": "no Scope View log path"}
    path = Path(p)
    try:
        size = path.stat().st_size
    except OSError:
        return {"log": p, "errors": [], "note": "the Scope View log does not exist (TE130x not installed here, or "
                                                 "[scope] view_log points elsewhere)"}
    out: dict[str, Any] = {"log": p}
    start = mark.get("size") or 0
    if size < start:
        out["note"] = "the log is shorter than when the call began (rotated?) — read from its start"
        start = 0
    if size - start > _LOG_READ_MAX:
        out["note"] = f"{size - start} new bytes — only the last {_LOG_READ_MAX} read"
        start = size - _LOG_READ_MAX
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            raw = fh.read(size - start)
    except OSError as exc:
        return {**out, "errors": [], "note": f"the Scope View log cannot be read: {exc}"}
    entries: list[list[str]] = []
    cur: list[str] | None = None
    for ln in _decode(raw).splitlines():
        m = _LEVEL.search(ln)
        if m:
            cur = [ln.strip()] if m.group(1).upper() in ("ERROR", "FATAL") else None
            if cur is not None:
                entries.append(cur)
        elif cur is not None and ln.strip() and len(cur) < 6:
            cur.append(ln.strip())
    out.update({"new_bytes": size - (mark.get("size") or 0) if size >= (mark.get("size") or 0) else size,
                "error_count": len(entries), "errors": [" | ".join(e)[:800] for e in entries[-limit:]]})
    return out


def export(svd: str | Path, target: str | Path, *, tool: Path, overwrite: bool = False, timeout_s: float = 600,
           key: bytes | None = None, version: str = "?", view_log: Path | None = None) -> dict[str, Any]:
    """Run the export tool on `svd`. N118: a read-only source (rec_stop sets its .svdx read-only, like the recording)
    is exported from a byte-identical writable copy — the tool exited 0 without a CSV on such a file, while it
    exported a writable one; whether read-only is the cause is not proven, the copy rules it out. A run that writes no
    CSV says what is known: exit code, its output, whether the source is read-only, the Scope View log's new errors."""
    from .recorder import _manifest_sig, file_sha256, is_read_only, make_writable, provenance_path   # R10

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
    ssha, ssize = file_sha256(src)
    read_only = is_read_only(src)
    run_on, tmp, copy_note = src, None, None
    if read_only:
        tmp = out.parent / f".tcmcp-export-{os.getpid()}-{time.monotonic_ns()}"
        tmp.mkdir(parents=True)
        run_on = tmp / src.name
        shutil.copyfile(src, run_on)                   # copyfile: the copy is writable (no permission bits copied)
        if file_sha256(run_on)[0] != ssha:
            shutil.rmtree(tmp, ignore_errors=True)
            raise ScopeError(f"the writable copy of the read-only {src.name} differs from it — nothing was exported")
        copy_note = {"copy": str(run_on), "sha256_identical": True,
                     "why": "the source is read-only (rec_stop seals its Scope files like the recording): exported from "
                            "a byte-identical writable copy, deleted afterwards — the CSV's File line names the copy"}
    mark = log_mark(view_log)
    cmd = [str(tool), f"svd={run_on}", f"target={out}", "silent"]
    t0 = time.monotonic()
    try:
        try:
            r = subprocess.run(cmd, cwd=str(tool.parent), capture_output=True, text=True, timeout=timeout_s,
                               creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise ScopeError(f"{TOOL_NAME} did not finish within {timeout_s:.0f} s") from exc
        except OSError as exc:
            raise ScopeError(f"{TOOL_NAME} could not be started: {exc}") from exc
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    elapsed = round(time.monotonic() - t0, 2)
    log = log_errors_since(mark) if view_log is not None else None
    if not out.is_file() or out.stat().st_size == 0:
        said = (r.stderr or r.stdout or "").strip()
        raise ScopeError(f"{TOOL_NAME} exited with code {r.returncode} after {elapsed} s and wrote no CSV"
                         + (f"; its output: {said[:400]}" if said else "; it printed nothing")
                         + f"; source {src.name}: {ssize} bytes, "
                         + ("read-only — run on a writable copy" if read_only else "writable")
                         + (f"; Scope View log, new errors: {log['errors']}" if log and log.get("errors") else
                            f"; Scope View log: no new error ({log.get('note') or log.get('log')})" if log else "")
                         + " — open the file in XAE's Scope View to see whether Beckhoff can read it.")
    with open(out, "r", encoding="utf-8-sig", errors="replace") as fh:
        head = [fh.readline().rstrip("\r\n") for _ in range(3)]
        rows = 3 + sum(1 for _ in fh)
    esha, esize = file_sha256(out)
    tsha, _ = file_sha256(tool)
    prov: dict[str, Any] = {"_provenance": 1, "kind": "scope_export", "export_file": out.name, "export_sha256": esha,
                            "export_bytes": esize, "source_file": str(src), "source_sha256": ssha, "source_bytes": ssize,
                            "tool": str(tool), "tool_sha256": tsha, "command": cmd, "exit_code": r.returncode,
                            "exporter": f"twincat-mcp {version}", "created": dt.datetime.now(dt.timezone.utc).isoformat()}
    if copy_note:
        prov["exported_from_copy"] = copy_note
    if key:
        prov["hmac"] = _manifest_sig(key, prov)
    pp = provenance_path(out)
    make_writable(pp)
    pp.write_text(json.dumps(prov, indent=1) + "\n", encoding="utf-8")
    res: dict[str, Any] = {
        "file": str(out), "bytes": esize, "lines": rows, "first_lines": [h for h in head if h], "source": str(src),
        "source_sha256": ssha, "export_sha256": esha, "provenance": str(pp), "exit_code": r.returncode,
        "elapsed_s": elapsed,
        "note": ("CSV written by Beckhoff's export tool, unchanged. Channels, time column and the alignment of "
                 "channels with different sample times follow the export settings saved in the Scope project.")}
    if copy_note:
        res["exported_from_copy"] = copy_note
    if log is not None:
        res["scope_view_log"] = log
    return res
