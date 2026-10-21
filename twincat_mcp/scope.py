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

import codecs
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

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
# N119 (0.8.13 retest): TcScopeView.log pads its tags — '[ ERROR ]', '[ INFO  ]', '[WARNING]'. Without the spaces the
# padded ERROR lines were never read, and one '[WARNING]' (a TwinCAT state change always writes one) turned the
# fallback off: 2 real errors → error_count 0
_LEVEL = re.compile(r"\[\s*(ERROR|FATAL|WARN(?:ING)?|INFO|DEBUG|TRACE|VERBOSE)\s*\]", re.I)
_LOG_CHUNK = 4 << 20


def view_log_path(configured: str | None = None) -> Path:
    """[scope] view_log, else %ProgramData%\\Beckhoff\\TwinCAT\\Functions\\TE130X-Scope-View\\TcScopeView.log."""
    if configured:
        return Path(configured)
    return Path(os.environ.get("ProgramData") or r"C:\ProgramData") / "Beckhoff" / "TwinCAT" / "Functions" / \
        "TE130X-Scope-View" / "TcScopeView.log"


def _content_end(path: Path, size: int) -> int:
    """The byte offset after the log's last byte that is not NUL. N140 (0.8.14 retest): TcScopeView.log carries NUL
    padding; a writer that fills such padding in place writes below the file's size — the mark is where the text
    ends, so what is written there later is read."""
    with open(path, "rb") as fh:
        pos = size
        while pos > 0:
            n = min(1 << 16, pos)
            fh.seek(pos - n)
            kept = fh.read(n).rstrip(b"\x00")
            if kept:
                return pos - n + len(kept)
            pos -= n
    return 0


_HEAD = 4096


def _head(path: Path, n: int) -> str:
    """A fingerprint of the log's first n bytes — a new log (rotated) begins otherwise."""
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read(n)).hexdigest()[:16]


def _mark_one(path: Path | None) -> dict[str, Any]:
    """Where one Scope View log's text ends now ({"path", "size", "head"}; trailing NUL padding not counted, N140):
    size 0 when there is no log yet (all of a log written later is new); "error" when it exists but cannot be read
    (nothing can be compared then). head: the fingerprint of its beginning — a log rotated to one that has grown past
    the mark is told by it (review of N140: a padded log's text end is below its file size)."""
    if path is None:
        return {"path": None, "size": None}
    try:
        end = _content_end(path, path.stat().st_size)
        n = min(_HEAD, end)
        return {"path": str(path), "size": end, **({"head": _head(path, n), "head_bytes": n} if n else {})}
    except FileNotFoundError:
        return {"path": str(path), "size": 0}
    except OSError as exc:
        return {"path": str(path), "size": None, "error": str(exc)}


def _log_encoding(path: Path) -> str:
    """The log's encoding from its first bytes (the BOM / the UTF-16 zero bytes), not from a slice in its middle."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(4)
    except OSError:
        return "utf-8"
    if head.startswith(b"\xff\xfe") or (len(head) == 4 and head[1] == 0 and head[3] == 0 and head[0] != 0):
        return "utf-16-le"
    return "utf-8"


def _new_lines(path: Path, start: int, end: int) -> Iterator[str]:
    """The lines of the log between byte offsets start and end, read in chunks (no size limit: errors in a long
    window were dropped when only its last 4 MiB were read)."""
    enc = _log_encoding(path)
    if enc == "utf-16-le" and start % 2:
        start += 1
    dec = codecs.getincrementaldecoder(enc)(errors="replace")
    rest = ""
    with open(path, "rb") as fh:
        fh.seek(start)
        left = end - start
        while left > 0:
            raw = fh.read(min(_LOG_CHUNK, left))
            if not raw:
                break
            left -= len(raw)
            # N140 (0.8.14 retest): the log's NUL padding is no text — each error string carried hundreds of U+0000
            text = rest + dec.decode(raw).replace("\x00", "")
            lines = text.splitlines(keepends=True)
            rest = lines.pop() if lines and not lines[-1].endswith(("\n", "\r")) else ""
            for ln in lines:
                yield ln.lstrip("\ufeff").rstrip("\r\n")
    tail = rest + dec.decode(b"", final=True).replace("\x00", "")
    if tail:
        yield tail.lstrip("\ufeff")


def _errors_one(mark: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    """The ERROR / FATAL entries one Scope View log got after `mark` (an entry: its line and up to 5 following lines
    without a level tag). {"log", "errors": [...], "error_count", "new_bytes"}; "note" when the log is missing or was
    rotated (shorter than at the mark: read from its start)."""
    p = mark.get("path")
    if not p:
        return {"log": None, "errors": [], "note": "no Scope View log path"}
    if mark.get("error"):
        return {"log": p, "errors": [], "note": f"the log could not be read when the call began ({mark['error']}) — "
                                                "what is new in it is not known"}
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
    elif mark.get("head"):
        try:
            if _head(path, int(mark.get("head_bytes") or 0)) != mark["head"]:
                out["note"] = "the log begins otherwise than when the call began (a new log) — read from its start"
                start = 0
        except OSError as exc:
            return {**out, "errors": [], "note": f"the Scope View log cannot be read: {exc}"}
    entries: list[list[str]] = []
    said: list[str] = []                  # untagged lines that say error — used only when no line is tagged at all
    cur: list[str] | None = None
    tagged = False
    try:
        for ln in _new_lines(path, start, size):
            m = _LEVEL.search(ln)
            if m:
                tagged = True
                cur = [ln.strip()] if m.group(1).upper() in ("ERROR", "FATAL") else None
                if cur is not None:
                    entries.append(cur)
            elif cur is not None and ln.strip() and len(cur) < 6:
                cur.append(ln.strip())
            elif not tagged and re.search(r"error|fehler|exception|fatal", ln, re.I):
                said.append(ln.strip())
    except OSError as exc:
        return {**out, "errors": [], "note": f"the Scope View log cannot be read: {exc}"}
    if not tagged and said:
        # no level tag ([ ERROR ], [INFO] …) in the new lines at all: the log's format is not the one expected — its
        # lines that say 'error' are given, and that the format was not recognised
        entries = [[x] for x in said]
        out["note"] = ((out.get("note") + "; ") if out.get("note") else "") + (
            "no level tag ([ERROR], [INFO] …) in the new lines: the log's format was not recognised — the lines that "
            "say 'error' / 'exception' are listed")
    try:
        new = max(0, _content_end(path, size) - start)          # the text written, not the padding (review of N140)
    except OSError:
        new = size - start
    out.update({"new_bytes": new, "error_count": len(entries),
                "errors": [" | ".join(e)[:800] for e in entries[-limit:]]})
    return out


def log_family(path: Path) -> list[Path]:
    """The Scope View log and its numbered siblings in its folder — TcScopeView.log, TcScopeView_1.log, … (N179, 0.8.15
    retest: TE130x wrote every line to TcScopeView_1.log from the evening a second XAE shell opened, and the plugin
    read only TcScopeView.log: two recordings' Ads-Error 0xA2 lines were reported as error_count 0)."""
    rx = re.compile(rf"^{re.escape(path.stem)}(?:_\d+)?{re.escape(path.suffix)}$", re.I)
    try:
        found = [x for x in path.parent.iterdir() if x.is_file() and rx.match(x.name)]
    except OSError:
        found = []
    return sorted(set(found) | ({path} if path.exists() else set()), key=lambda x: (len(x.name), x.name.lower()))


def log_mark(path: Path | None) -> dict[str, Any]:
    """Where the Scope View logs' texts end now: the log `path` names and every numbered sibling (log_family) — each
    {"path", "size", "head"} (_mark_one). The top-level keys are the named log's, as before; "family" holds them all
    (N179: TE130x may write to TcScopeView_1.log; a file that appears later is read from its start)."""
    one = _mark_one(path)
    if path is None:
        return one
    return {**one, "family": [_mark_one(x) for x in log_family(path)], "family_of": str(path)}


def log_errors_since(mark: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    """The ERROR / FATAL entries the Scope View logs got after `mark` — in every log of the family (log_family, N179),
    one that did not exist at the mark read from its start. {"log": the log that got new text (the named one when
    none did), "logs_checked", "logs_written", "errors", "error_logs" (the log of each error, in order),
    "error_count", "new_bytes"}; "note" when no log was written since the mark, or one was missing / rotated. A mark
    of one file (no "family") reads that file only."""
    if "family" not in mark:
        return _errors_one(mark, limit)
    marked = {m["path"]: m for m in mark["family"] if m.get("path")}
    files = log_family(Path(mark["family_of"]))
    paths = list(dict.fromkeys([str(x) for x in files] + list(marked)))
    if not files:
        return _errors_one({"path": mark["family_of"], "size": 0}, limit)
    per = []
    for p in paths:
        r = _errors_one(marked.get(p) or {"path": p, "size": 0}, limit)
        per.append((p, r))
    written = [(p, r) for p, r in per if (r.get("new_bytes") or 0) > 0 or r.get("errors")]
    errors: list[str] = []
    logs: list[str] = []
    for p, r in written:
        errors += r.get("errors") or []
        logs += [Path(p).name] * len(r.get("errors") or [])
    out: dict[str, Any] = {
        "log": written[-1][0] if written else mark.get("path"),
        "logs_checked": [Path(p).name for p in paths], "logs_written": [Path(p).name for p, _ in written],
        "new_bytes": sum(int(r.get("new_bytes") or 0) for _p, r in per),
        "error_count": sum(int(r.get("error_count") or 0) for _p, r in per),
        "errors": errors[-limit:], "error_logs": logs[-limit:]}
    notes = [f"{Path(p).name}: {r['note']}" for p, r in per if r.get("note") and "does not exist" not in r["note"]]
    if not written:
        notes.insert(0, "no Scope View log was written since the call began (" + ", ".join(out["logs_checked"]) + ")")
    if notes:
        out["note"] = "; ".join(notes)
    return out


def log_find_since(mark: dict[str, Any] | None, rx: "re.Pattern[str]") -> list[tuple[str, str]] | None:
    """The lines the Scope View logs (the family of the mark) got after `mark` that match rx: [(file name, line)] in
    file order — e.g. the 'state changes … to Config' line of a TwinCAT restart (N180). None when that cannot be told:
    no mark, a mark taken when the log could not be read (read from its start, a line of days ago was taken for this
    run's — thirteenth review of 0.8.16), or a log of the family that cannot be read now. A log that begins otherwise
    than at the mark (a new log) or is shorter is read from its start, as log_errors_since reads it."""
    if not mark or not mark.get("path"):
        return None
    family = mark.get("family") or [mark]
    if any(m.get("error") or (m.get("path") and m.get("size") is None) for m in family):
        return None
    marked = {m["path"]: m for m in family if m.get("path")}
    paths = log_family(Path(mark["family_of"])) if mark.get("family_of") else [Path(mark["path"])]
    out: list[tuple[str, str]] = []
    if not any(p.is_file() for p in paths):
        return None                                    # no log at all (TE130x not here, or view_log elsewhere)
    for p in paths:
        m = marked.get(str(p)) or {"path": str(p), "size": 0}
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            continue                                   # not written (yet): nothing new in it
        except OSError:
            return None
        start = m.get("size") or 0
        try:
            if size < start or (start and m.get("head") and _head(p, int(m.get("head_bytes") or 0)) != m["head"]):
                start = 0
            out += [(p.name, ln.strip()) for ln in _new_lines(p, start, size) if rx.search(ln)]
        except OSError:
            return None
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
    if out.exists() and is_read_only(out):
        # a CSV rec_stop sealed (its SHA-256 is in the signed <id>.scope.json): replacing it would break the seal
        raise ScopeError(f"{out} is sealed (read-only, written by rec_stop with the recording) — it is never replaced; "
                         "export to another file name")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScopeError(f"the folder of {out} cannot be created: {exc} — nothing was exported") from exc
    # the tool writes a work file beside the target; the target is replaced only by a complete CSV — overwrite=true
    # deleted the old CSV before the tool ran, so a failed export lost it
    work = out.with_name(f".{out.stem}.tcmcp-{os.getpid()}-{time.monotonic_ns()}.csv")
    try:
        ssha, ssize = file_sha256(src)
    except OSError as exc:
        raise ScopeError(f"{src} cannot be read: {exc} — nothing was exported") from exc
    read_only = is_read_only(src)
    run_on, tmp, copy_note = src, None, None
    mark = log_mark(view_log)
    cmd: list[str] = []
    t0 = time.monotonic()
    try:
        if read_only:
            tmp = out.parent / f".tcmcp-export-{os.getpid()}-{time.monotonic_ns()}"
            try:
                tmp.mkdir(parents=True)
                run_on = tmp / src.name
                shutil.copyfile(src, run_on)           # copyfile: the copy is writable (no permission bits copied)
                same = file_sha256(run_on)[0] == ssha
            except OSError as exc:
                raise ScopeError(f"the read-only {src.name} could not be copied to export it ({exc}) — nothing was "
                                 "exported") from exc
            if not same:
                raise ScopeError(f"the writable copy of the read-only {src.name} differs from it — nothing was exported")
            copy_note = {"copy": str(run_on), "sha256_identical": True,
                         "why": "the source is read-only (rec_stop seals its Scope files like the recording): exported "
                                "from a byte-identical writable copy, deleted afterwards — the CSV's File line names the copy"}
        cmd = [str(tool), f"svd={run_on}", f"target={work}", "silent"]
        t0 = time.monotonic()
        try:
            r = subprocess.run(cmd, cwd=str(tool.parent), capture_output=True, text=True, errors="replace",
                               timeout=timeout_s, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired as exc:
            raise ScopeError(f"{TOOL_NAME} did not finish within {timeout_s:.0f} s") from exc
        except OSError as exc:
            raise ScopeError(f"{TOOL_NAME} could not be started: {exc}") from exc
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
            if tmp.exists():
                copy_note = {**(copy_note or {}), "left_behind": str(tmp)}   # Windows still held a handle: said, not hidden
    elapsed = round(time.monotonic() - t0, 2)
    log = log_errors_since(mark) if view_log is not None else None
    if not work.is_file() or work.stat().st_size == 0:
        said = (r.stderr or r.stdout or "").strip()
        try:
            work.unlink()
        except OSError:
            pass
        left = (copy_note or {}).get("left_behind")
        raise ScopeError(f"{TOOL_NAME} exited with code {r.returncode} after {elapsed} s and wrote no CSV"
                         + (f"; its output: {said[:400]}" if said else "; it printed nothing")
                         + f"; source {src.name}: {ssize} bytes, "
                         + ("read-only — run on a writable copy" if read_only else "writable")
                         + (f"; the copy's folder {left} could not be removed (delete it)" if left else "")
                         + (f"; Scope View log, new errors: {log['errors']}" if log and log.get("errors") else
                            f"; Scope View log: no new error ({log.get('note') or log.get('log')})" if log else "")
                         + (f"; {out.name} is unchanged" if out.exists() else "")
                         + " — open the file in XAE's Scope View to see whether Beckhoff can read it.")
    try:
        if out.exists():
            make_writable(out)
        os.replace(work, out)
    except OSError as exc:
        raise ScopeError(f"{TOOL_NAME} wrote {work.name}, but it could not replace {out} (open in another program?): "
                         f"{exc} — the new CSV is {work}") from exc
    try:
        with open(out, "r", encoding="utf-8-sig", errors="replace") as fh:
            head = [fh.readline().rstrip("\r\n") for _ in range(3)]
            rows = 3 + sum(1 for _ in fh)
        esha, esize = file_sha256(out)
        tsha, _ = file_sha256(tool)
    except OSError as exc:
        raise ScopeError(f"{TOOL_NAME} wrote {out}, but it cannot be read back: {exc}") from exc
    prov: dict[str, Any] = {"_provenance": 1, "kind": "scope_export", "export_file": out.name, "export_sha256": esha,
                            "export_bytes": esize, "source_file": str(src), "source_sha256": ssha, "source_bytes": ssize,
                            "tool": str(tool), "tool_sha256": tsha, "command": cmd, "exit_code": r.returncode,
                            "written_as": work.name,     # the tool's work file, renamed to export_file when complete
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
    if r.returncode != 0:
        # review: a tool that fails part-way may leave a partial CSV — it is the tool's output, unchanged, but not
        # called complete
        res["warning"] = (f"{TOOL_NAME} exited with code {r.returncode} although it wrote the CSV — it may be "
                          "incomplete" + (f"; its output: {(r.stderr or r.stdout).strip()[:300]}" if (r.stderr or r.stdout).strip() else ""))
    if log is not None:
        res["scope_view_log"] = log
    return res
