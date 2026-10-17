"""Local knowledge base (decisions of 2026-10-17: option (a) + user-provided locations only).

What it holds, and where each part comes from:

* known errors (K6)      — a seeded table in this file: error text → known cause → fix. Attached to tool errors.
                           Nothing is learned automatically.
* library versions (K3)  — the .plcproj of the project (what each placeholder resolves to) against the library
                           versions installed on this PC (folder names in TwinCAT's managed-library repository).
                           Encrypted .compiled-library files are never opened.
* hardware (K4)          — EtherCAT ESI device descriptions from TwinCAT's own install folders (PDOs, CoE objects).
* code patterns (K9)     — POUs/DUTs/GVLs of project folders the user adds explicitly (kb_add_project). Nothing is
                           indexed by default.

The database lives in a folder the user chooses (kb_set_location). Conditions: local fixed drive (SQLite corrupts
on network shares), not inside a cloud-synced folder (OneDrive/Dropbox/Google Drive lock and corrupt it), not inside
the plugin folder (replaced on every update), writable, ≥ 1 GB free. If the user names none, the agent asks; the
proposed fallback is D:\\TwinCAT-MCP\\knowledge (used only when the user agrees via kb_set_location(use_fallback=true)).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FALLBACK_LOCATION = r"D:\TwinCAT-MCP\knowledge"
MIN_FREE_BYTES = 1 << 30
DB_NAME = "knowledge.db"
POINTER_NAME = "knowledge_location.json"

LOCATION_CONDITIONS = [
    "a folder on a local fixed drive of this PC (not a network share, mapped drive, NAS or USB stick — SQLite "
    "corrupts databases on network file systems, and a removable drive takes the database with it)",
    "not inside a cloud-synced folder (OneDrive, Dropbox, Google Drive, iCloud, Box) — the sync client locks and "
    "corrupts open database files",
    "not inside the plugin / install folder (~/.claude/plugins/cache/…) — it is replaced on every update",
    "writable by your Windows user",
    "at least 1 GB free",
]

# --------------------------------------------------------------------------------------------- K6 known errors
# (pattern, cause, fix) — first match wins. Seeded from the field reports; extend here, never from tool output.
KNOWN_ERRORS: list[tuple[re.Pattern[str], str, str]] = [(re.compile(p, re.I), c, f) for p, c, f in [
    (r"0x98110724|2551252772|licen[cs]e",
     "TwinCAT license missing or expired on the target (TwinCAT falls back to CONFIG)",
     "XAE → SYSTEM → License → '7 Days Trial License…', activate the configuration again, restart TwinCAT"),
    (r"0x80010001|-2147418111|0x8001010a|-2147417846|call was rejected|retry later|blocked by dialog",
     "the XAE shell is busy or a modal dialog has it",
     "xae_dialogs shows the dialog; answer it with xae_dialog_resolve(button=…), then repeat the call"),
    (r"0x800706ba|-2147023174|rpc server is unavailable|disconnected \(closed",
     "the XAE shell was closed/reopened or crashed; the COM proxy is dead",
     "the next xae_* call re-attaches by itself; if not, xae_attach"),
    (r"logged into|logged in",
     "XAE is logged in to the PLC; the project cannot be changed online",
     "xae_plc('logout'), make the change, build, log in again"),
    (r"adserror 1797\b|0x705\b|parameter size not correct",
     "the value does not fit the variable (string too long, wrong array length or type size)",
     "check the declared type (ads_symbol_info); write elements by index for arrays"),
    (r"adserror 1808\b|0x710\b|symbol not found",
     "the symbol is not in the runtime: typo, not downloaded yet, or optimised away (unused, not referenced)",
     "ads_list_symbols pattern=…; after a code change: build + download / online change"),
    (r"adserror 1861\b|0x745\b|timeout elapsed",
     "the target did not answer in time: missing/broken route, firewall (TCP 48898), target busy or stopped",
     "ads_ping; ads_routes; check the route on both sides and the Windows firewall"),
    (r"adserror 6\b|0x6\b.*port|target port not found",
     "nothing listens on that AMS port: TwinCAT is in CONFIG (PLC runtimes exist only in RUN), no PLC project is "
     "activated on it, or the port number is wrong (851 = first PLC runtime, 852 = second …)",
     "ads_status port=10000 shows RUN/CONFIG; ads_system_mode run, or activate the PLC project"),
    (r"adserror 7\b|target machine not found",
     "no route to that AMS NetId",
     "ads_add_route_to_plc / XAE → Choose Target → Search; check ams_net_id in the config"),
    (r"adserror 1793\b|0x701\b|service is not supported",
     "the target/port does not offer this ADS service (e.g. sum commands, file access on this OS)",
     "use the non-sum variant or another port; for files check that the target OS supports the System Service file API"),
    (r"adserror 1799\b|0x707\b|device not ready",
     "the runtime is not running (TwinCAT in CONFIG, or the PLC stopped)",
     "ads_status; switch to RUN (ads_system_mode run) / start the PLC (ads_plc_state run)"),
    (r"adserror 1796\b|0x704\b|not permitted",
     "reading/writing this index group/offset is not permitted (read-only area or protected symbol)",
     "write a writable symbol instead; check READONLY flags with ads_symbol_info"),
]]


def ads6_context(port: int | None, mode: str | None) -> dict[str, str]:
    """N17: ADSError 6 (target port not found) explained from the actual port and TwinCAT mode."""
    p = port or 851
    if mode == "CONFIG" and 850 <= p <= 899:
        return {"cause": f"TwinCAT is in CONFIG mode — the PLC runtime on port {p} exists only in RUN",
                "fix": "ads_system_mode run (or activate the configuration and restart in RUN)"}
    if mode == "RUN" and 851 <= p <= 899:
        return {"cause": f"TwinCAT is in RUN but no PLC runtime listens on port {p}: no PLC project is activated on it"
                         + (" (851 is the first PLC runtime)" if p != 851 else "") + ", or there is no boot project",
                "fix": "activate the PLC project (xae_activate_configuration), or use the port it runs on"}
    if mode is None:
        return {"cause": f"nothing listens on AMS port {p} and the system service (port 10000) did not answer either — "
                         "is TwinCAT running on the target, and is the route right?",
                "fix": "ads_ping; ads_routes; tc_doctor"}
    return {"cause": f"nothing listens on AMS port {p} (TwinCAT is in {mode})", "fix": "check the port number"}


def known_error(msg: str) -> dict[str, str] | None:
    for pat, cause, fix in KNOWN_ERRORS:
        if pat.search(msg or ""):
            return {"cause": cause, "fix": fix}
    return None


def annotate(msg: str) -> str:
    """Append the known cause/fix to an error message unless the message already carries that fix."""
    k = known_error(msg)
    if not k:
        return msg
    low = msg.lower()
    tools = re.findall(r"\b[a-z]+_[a-z_]+\b", k["fix"])          # tool names the fix points at
    if any(t in low for t in tools) or k["fix"][:30].lower() in low:
        return msg                                                # the message already says what to do
    return f"{msg} [known cause: {k['cause']}. fix: {k['fix']}]"


class KnowledgeError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------- location checks
_SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "icloud drive", "iclouddrive", "box sync", "box drive")


def check_location(path: str | Path, plugin_dirs: list[Path] | None = None) -> list[str]:
    """Every condition the folder fails, as plain sentences (empty list = usable)."""
    problems: list[str] = []
    p = Path(path).expanduser()
    if not p.is_absolute():
        return [f"{path!r} is not an absolute path"]
    s = str(p)
    low = s.lower()
    if s.startswith("\\\\") or s.startswith("//"):
        problems.append("it is a network (UNC) path")
    elif os.name == "nt":
        try:
            import ctypes
            drive = os.path.splitdrive(s)[0] + "\\"
            kind = ctypes.windll.kernel32.GetDriveTypeW(drive)  # type: ignore[attr-defined]
            if kind == 4:
                problems.append(f"{drive} is a network/mapped drive")
            elif kind == 2:
                problems.append(f"{drive} is a removable drive")
            elif kind in (0, 1):
                problems.append(f"{drive} does not exist")
        except Exception:  # noqa: BLE001
            pass
    else:
        fs = _posix_fstype(p)
        if fs and any(x in fs for x in ("nfs", "cifs", "smb", "sshfs", "fuse.s3", "9p")):
            problems.append(f"it is on a network file system ({fs})")
    parts = {x.lower() for x in p.parts}
    if any(m in part for part in parts for m in _SYNC_MARKERS):
        problems.append("it is inside a cloud-synced folder")
    for env in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        od = os.environ.get(env)
        if od and low.startswith(od.lower().rstrip("\\/")):
            problems.append(f"it is inside the OneDrive folder ({od})")
    plugin_dirs = list(plugin_dirs or []) + [Path.home() / ".claude" / "plugins" / "cache", Path(__file__).resolve().parents[1]]
    for d in plugin_dirs:
        try:
            if p.resolve() == d.resolve() or d.resolve() in p.resolve().parents:
                problems.append(f"it is inside the plugin/install folder {d} (replaced on every update)")
                break
        except OSError:
            continue
    if not problems:
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".tcmcp_write_test"
            probe.write_text("x", encoding="ascii")
            probe.unlink()
        except OSError as exc:
            problems.append(f"it is not writable ({exc})")
        else:
            try:
                free = shutil.disk_usage(p).free
                if free < MIN_FREE_BYTES:
                    problems.append(f"only {free / (1 << 30):.2f} GB free (needs ≥ 1 GB)")
            except OSError:
                pass
    return problems


def _posix_fstype(p: Path) -> str | None:
    try:
        best, fstype = "", None
        with open("/proc/mounts", encoding="utf-8") as fh:
            for ln in fh:
                parts = ln.split()
                if len(parts) >= 3 and str(p).startswith(parts[1]) and len(parts[1]) > len(best):
                    best, fstype = parts[1], parts[2]
        return fstype
    except OSError:
        return None


def _hexint(v: str | None) -> int | None:
    if not v:
        return None
    v = v.strip()
    try:
        return int(v[2:], 16) if v[:2] in ("#x", "#X") else int(v, 0)
    except ValueError:
        return None


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)) or (0,)


# --------------------------------------------------------------------------------------------- store
class Knowledge:
    def __init__(self, home: Path, location: str | None = None, esi_dirs: list[str] | None = None,
                 library_dirs: list[str] | None = None):
        self.home = home
        self.configured = location
        self.esi_dirs = esi_dirs or []
        self.library_dirs = library_dirs or []
        self._fts: bool | None = None

    # -- location -------------------------------------------------------------------------
    def _pointer(self) -> Path:
        return self.home / POINTER_NAME

    def location(self) -> tuple[Path | None, str]:
        if self.configured:
            return Path(self.configured).expanduser(), "config (knowledge.location)"
        try:
            d = json.loads(self._pointer().read_text(encoding="utf-8"))
            if d.get("path"):
                return Path(d["path"]), f"chosen {d.get('chosen', '')}".strip()
        except (OSError, ValueError):
            pass
        return None, "not chosen yet"

    def require(self) -> Path:
        loc, _src = self.location()
        if loc is None:
            raise KnowledgeError(self.ask_text())
        problems = check_location(loc)
        if problems:
            raise KnowledgeError(f"the knowledge folder {loc} is no longer usable: {'; '.join(problems)}. "
                                 "Ask the user for another folder and call kb_set_location(path=…).")
        return loc

    def ask_text(self) -> str:
        fb = FALLBACK_LOCATION if (os.name == "nt" and Path("D:\\").exists()) else None
        return ("The knowledge base has no folder yet. ASK THE USER where to keep it (a folder path), then call "
                "kb_set_location(path=…). The folder must be: " + "; ".join(f"({i + 1}) {c}" for i, c in enumerate(LOCATION_CONDITIONS))
                + ". " + (f"If the user has no preference, propose {fb} and call kb_set_location(use_fallback=true) "
                          "once they agree." if fb else "This PC has no D: drive, so there is no fallback: a folder is required."))

    def set_location(self, path: str | None, use_fallback: bool = False) -> dict[str, Any]:
        if use_fallback:
            if not (os.name == "nt" and Path("D:\\").exists()):
                raise KnowledgeError("no D: drive on this PC — the fallback location does not exist; give a folder path")
            path = FALLBACK_LOCATION
        if not path:
            raise KnowledgeError(self.ask_text())
        p = Path(path).expanduser()
        problems = check_location(p)
        if problems:
            raise KnowledgeError(f"{p} cannot hold the knowledge base: {'; '.join(problems)}. Conditions: "
                                 + "; ".join(LOCATION_CONDITIONS))
        old, _ = self.location()
        self.home.mkdir(parents=True, exist_ok=True)
        self._pointer().write_text(json.dumps({"path": str(p), "chosen": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
        self._fts = None
        out: dict[str, Any] = {"location": str(p), "database": str(p / DB_NAME)}
        if old is not None and old != p and (old / DB_NAME).exists():
            out["note"] = (f"the previous database stays at {old / DB_NAME}; move it into the new folder yourself if you "
                           "want to keep its contents (nothing is copied or deleted automatically)")
        with self._db():
            pass
        return out

    # -- database -------------------------------------------------------------------------
    def _db(self) -> "_Conn":
        return _Conn(self.require() / DB_NAME, self)

    def _init(self, con: sqlite3.Connection) -> None:
        con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS projects (path TEXT PRIMARY KEY, label TEXT, added TEXT, indexed TEXT, units INTEGER, files TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS units (project TEXT, name TEXT, kind TEXT, file TEXT, line INTEGER, "
                    "declaration TEXT, implementation TEXT, comments TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS units_project ON units(project)")
        con.execute("CREATE TABLE IF NOT EXISTS esi (type TEXT, name TEXT, product_code INTEGER, revision INTEGER, vendor TEXT, "
                    "grp TEXT, file TEXT, pdo TEXT, coe TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS esi_type ON esi(type)")
        con.execute("CREATE TABLE IF NOT EXISTS esi_files (file TEXT PRIMARY KEY, mtime REAL, devices INTEGER)")
        if self._fts is None:
            try:
                con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS units_fts USING fts5(project UNINDEXED, name, kind UNINDEXED, "
                            "file UNINDEXED, declaration, implementation, comments, tokenize='unicode61')")
                self._fts = True
            except sqlite3.OperationalError:
                self._fts = False

    def status(self) -> dict[str, Any]:
        loc, src = self.location()
        out: dict[str, Any] = {"location": str(loc) if loc else None, "source": src}
        if loc is None:
            out["action"] = self.ask_text()
            return out
        problems = check_location(loc)
        if problems:
            out["problems"] = problems
            return out
        with self._db() as con:
            out["database"] = str(loc / DB_NAME)
            out["projects"] = [dict(r) for r in con.execute("SELECT path, label, added, indexed, units FROM projects ORDER BY path")]
            out["esi_devices"] = con.execute("SELECT COUNT(*) FROM esi").fetchone()[0]
            out["esi_files"] = con.execute("SELECT COUNT(*) FROM esi_files").fetchone()[0]
            out["fts5"] = bool(self._fts)
        out["known_errors"] = len(KNOWN_ERRORS)
        return out

    # -- K9 projects ---------------------------------------------------------------------
    def add_project(self, path: str, label: str | None = None) -> dict[str, Any]:
        p = Path(path).expanduser()
        if not p.is_absolute() or not p.is_dir():
            raise KnowledgeError(f"{path!r} is not an existing absolute folder")
        with self._db() as con:
            con.execute("INSERT OR IGNORE INTO projects(path, label, added) VALUES (?,?,?)",
                        (str(p), label or p.name, time.strftime("%Y-%m-%dT%H:%M:%S")))
            if label:
                con.execute("UPDATE projects SET label=? WHERE path=?", (label, str(p)))
        return self.reindex(str(p))

    def remove_project(self, path: str) -> dict[str, Any]:
        p = str(Path(path).expanduser())
        with self._db() as con:
            n = con.execute("DELETE FROM units WHERE project=?", (p,)).rowcount
            if self._fts:
                con.execute("DELETE FROM units_fts WHERE project=?", (p,))
            found = con.execute("DELETE FROM projects WHERE path=?", (p,)).rowcount
        if not found:
            raise KnowledgeError(f"{p} is not in the knowledge base (kb_status lists the projects)")
        return {"removed": p, "units_removed": n, "note": "only the index entries were removed; your files are untouched"}

    def reindex(self, path: str | None = None) -> dict[str, Any]:
        from .config import ProjectConfig
        from .project_backend import ProjectIndex
        with self._db() as con:
            rows = [dict(r) for r in con.execute("SELECT path, files FROM projects" + (" WHERE path=?" if path else ""),
                                                  ((str(Path(path).expanduser()),) if path else ()))]
        if path and not rows:
            raise KnowledgeError(f"{path} is not in the knowledge base; kb_add_project first")
        out = []
        for r in rows:
            root = Path(r["path"])
            if not root.is_dir():
                out.append({"project": r["path"], "error": "folder not found (moved or deleted?) — kb_remove_project or re-add"})
                continue
            stamp = _tree_stamp(root)
            if r.get("files") == stamp and path is None:
                out.append({"project": r["path"], "unchanged": True})
                continue
            idx = ProjectIndex(ProjectConfig(root=str(root)))
            try:
                scan = idx.scan()
            except Exception as exc:  # noqa: BLE001
                out.append({"project": r["path"], "error": str(exc)})
                continue
            units = [u for u in idx.units.values() if u.kind != "PARSE_ERROR"]
            with self._db() as con:
                con.execute("DELETE FROM units WHERE project=?", (r["path"],))
                if self._fts:
                    con.execute("DELETE FROM units_fts WHERE project=?", (r["path"],))
                for u in units:
                    txt = (u.declaration or "") + "\n" + (u.implementation or "")
                    comments = " ".join(a or b for a, b in re.findall(r"\(\*(.*?)\*\)|//([^\n]*)", txt, re.S))
                    row = (r["path"], u.name, u.kind, u.file, u.decl_line0, u.declaration or "", (u.implementation or "")[:20000], comments[:5000])
                    con.execute("INSERT INTO units VALUES (?,?,?,?,?,?,?,?)", row)
                    if self._fts:
                        con.execute("INSERT INTO units_fts(project, name, kind, file, declaration, implementation, comments) VALUES (?,?,?,?,?,?,?)",
                                    (row[0], row[1], row[2], row[3], row[5], row[6], row[7]))
                con.execute("UPDATE projects SET indexed=?, units=?, files=? WHERE path=?",
                            (time.strftime("%Y-%m-%dT%H:%M:%S"), len(units), stamp, r["path"]))
            out.append({"project": r["path"], "units": len(units), "files": scan.get("files"),
                        "parse_errors": sum(1 for u in idx.units.values() if u.kind == "PARSE_ERROR")})
        return {"reindexed": out}

    def search(self, query: str, kinds: list[str] | None = None, project: str | None = None, limit: int = 20) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            raise KnowledgeError("empty query")
        with self._db() as con:
            nproj = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            hits: list[dict[str, Any]] = []
            if nproj:
                if self._fts:
                    terms = " OR ".join(f'"{t}"' for t in re.findall(r"[\w]+", q))
                    sql = ("SELECT project, name, kind, file, snippet(units_fts, 4, '«', '»', ' … ', 12) AS decl, "
                           "snippet(units_fts, 5, '«', '»', ' … ', 12) AS impl, bm25(units_fts, 0, 10, 0, 0, 3, 1, 2) AS score "
                           "FROM units_fts WHERE units_fts MATCH ?")
                    params: list[Any] = [terms]
                else:
                    sql = ("SELECT project, name, kind, file, substr(declaration,1,200) AS decl, substr(implementation,1,200) AS impl, 0 AS score "
                           "FROM units WHERE (name LIKE ? OR declaration LIKE ? OR implementation LIKE ? OR comments LIKE ?)")
                    like = f"%{q}%"
                    params = [like, like, like, like]
                if project:
                    sql += " AND project = ?"
                    params.append(str(Path(project).expanduser()))
                if kinds:
                    sql += f" AND kind IN ({','.join('?' * len(kinds))})"
                    params += [k.upper() for k in kinds]
                sql += " ORDER BY score LIMIT ?"
                params.append(int(min(max(limit, 1), 200)))
                for r in con.execute(sql, params):
                    hits.append({"project": r["project"], "unit": r["name"], "kind": r["kind"], "file": r["file"],
                                 "declaration": r["decl"], "code": r["impl"]})
        err = known_error(q)
        out: dict[str, Any] = {"query": q, "hits": hits, "projects_indexed": nproj}
        if err:
            out["known_error"] = err
        if not nproj:
            out["note"] = "no project folders in the knowledge base yet — kb_add_project(path) with a folder the user names"
        return out

    # -- K4 hardware ---------------------------------------------------------------------
    def esi_folders(self) -> list[Path]:
        cands = [Path(d) for d in self.esi_dirs] + [
            Path(r"C:\TwinCAT\3.1\Config\Io\EtherCAT"),
            Path(r"C:\Program Files (x86)\Beckhoff\TwinCAT\3.1\Config\Io\EtherCAT"),
            Path(r"C:\ProgramData\Beckhoff\TwinCAT\3.1\Config\Io\EtherCAT"),
        ]
        seen, out = set(), []
        for c in cands:
            k = str(c).lower()
            if k not in seen and c.is_dir():
                seen.add(k)
                out.append(c)
        return out

    def esi_index(self, force: bool = False) -> dict[str, Any]:
        folders = self.esi_folders()
        if not folders:
            raise KnowledgeError("no ESI folder found (looked in TwinCAT's Config\\Io\\EtherCAT folders); set knowledge.esi_dirs")
        files = sorted({f for d in folders for f in d.glob("*.xml")})
        done = skipped = devices = 0
        errors: list[str] = []
        with self._db() as con:
            known = {r["file"]: r["mtime"] for r in con.execute("SELECT file, mtime FROM esi_files")}
        for f in files:
            mt = f.stat().st_mtime
            if not force and known.get(str(f)) == mt:
                skipped += 1
                continue
            try:
                rows = list(_parse_esi(f))
            except ET.ParseError as exc:
                errors.append(f"{f.name}: {exc}")
                continue
            with self._db() as con:
                con.execute("DELETE FROM esi WHERE file=?", (str(f),))
                con.executemany("INSERT INTO esi VALUES (?,?,?,?,?,?,?,?,?)", rows)
                con.execute("INSERT OR REPLACE INTO esi_files VALUES (?,?,?)", (str(f), mt, len(rows)))
            done += 1
            devices += len(rows)
        return {"folders": [str(d) for d in folders], "files": len(files), "indexed": done, "unchanged": skipped,
                "devices_added": devices, "errors": errors[:20]}

    def hw_lookup(self, query: str, limit: int = 5, revision: int | None = None) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            raise KnowledgeError("empty query (e.g. 'EL2008', 'EL3062', '#x0bf03052')")
        if self._esi_count() == 0:
            idx = self.esi_index()
        else:
            idx = None
        pc = _hexint(q) if q.startswith(("#x", "0x", "#X")) else None
        with self._db() as con:
            if pc is not None:
                rows = con.execute("SELECT * FROM esi WHERE product_code=? ORDER BY revision DESC LIMIT 50", (pc,)).fetchall()
            else:
                rows = con.execute("SELECT * FROM esi WHERE type = ? COLLATE NOCASE ORDER BY revision DESC LIMIT 50", (q,)).fetchall()
                if not rows:
                    rows = con.execute("SELECT * FROM esi WHERE type LIKE ? OR name LIKE ? ORDER BY type, revision DESC LIMIT 50",
                                       (f"%{q}%", f"%{q}%")).fetchall()
        if revision is not None:
            rows = [r for r in rows if r["revision"] == revision]
        types = sorted({r["type"] for r in rows})
        out_rows = []
        for r in rows[: max(1, min(limit, 20))]:
            out_rows.append({"type": r["type"], "name": r["name"], "vendor": r["vendor"], "group": r["grp"],
                             "product_code": f"#x{r['product_code']:08X}" if r["product_code"] is not None else None,
                             "revision": f"#x{r['revision']:08X}" if r["revision"] is not None else None,
                             "pdo": json.loads(r["pdo"] or "[]"), "coe_objects": json.loads(r["coe"] or "[]"),
                             "file": r["file"]})
        res: dict[str, Any] = {"query": q, "matches": len(rows), "types": types[:30], "devices": out_rows}
        if idx:
            res["indexed_now"] = idx
        if not rows:
            res["note"] = "not in the installed ESI files — update the ESI (XAE: TwinCAT → EtherCAT Devices → Update Device Descriptions)"
        return res

    def _esi_count(self) -> int:
        with self._db() as con:
            return con.execute("SELECT COUNT(*) FROM esi").fetchone()[0]

    # -- K3 libraries --------------------------------------------------------------------
    def library_folders(self) -> list[Path]:
        cands = [Path(d) for d in self.library_dirs] + [
            Path(r"C:\TwinCAT\3.1\Components\Plc\Managed Libraries"),
            Path(r"C:\Program Files (x86)\Beckhoff\TwinCAT\3.1\Components\Plc\Managed Libraries"),
            Path(r"C:\ProgramData\Beckhoff\TwinCAT\3.1\Components\Plc\Managed Libraries"),
        ]
        return [c for c in cands if c.is_dir()]

    def installed_libraries(self) -> dict[str, dict[str, Any]]:
        """{lib_name_lower: {"name", "vendor", "versions": [...]}} from folder names only (no file is opened)."""
        out: dict[str, dict[str, Any]] = {}
        for root in self.library_folders():
            for vendor in root.iterdir():
                if not vendor.is_dir():
                    continue
                for lib in vendor.iterdir():
                    if not lib.is_dir():
                        continue
                    vers = [v.name for v in lib.iterdir() if v.is_dir() and re.match(r"^\d+(\.\d+)+$", v.name)]
                    if vers:
                        d = out.setdefault(lib.name.lower(), {"name": lib.name, "vendor": vendor.name, "versions": []})
                        d["versions"] = sorted(set(d["versions"]) | set(vers), key=_vkey)
        return out


def library_report(plcproj_files: list[str], installed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """K3: per PLC project, every library reference with the version it asks for, the version it effectively
    resolves to on this PC ('*' = newest installed), and what is installed."""
    projects = []
    for f in plcproj_files:
        try:
            root = ET.parse(f).getroot()
        except (ET.ParseError, OSError) as exc:
            projects.append({"plcproj": f, "error": str(exc)})
            continue
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        resolutions = {}
        for el in root.iter(f"{ns}PlaceholderResolution"):
            r = el.find(f"{ns}Resolution")
            if el.get("Include") and r is not None and r.text:
                resolutions[el.get("Include").lower()] = r.text.strip()
        libs = []
        for el in root.iter(f"{ns}PlaceholderReference"):
            name = el.get("Include") or ""
            d = el.find(f"{ns}DefaultResolution")
            libs.append(_lib_row(name, resolutions.get(name.lower()) or (d.text.strip() if d is not None and d.text else ""),
                                 "placeholder", installed, overridden=name.lower() in resolutions))
        for el in root.iter(f"{ns}LibraryReference"):
            inc = el.get("Include") or ""
            libs.append(_lib_row(inc.split(",")[0].strip(), inc, "library", installed))
        projects.append({"plcproj": f, "libraries": libs})
    return {"projects": projects,
            "note": "versions from the .plcproj and from folder names in TwinCAT's library repository; '*' resolves to the "
                    "newest version installed on the PC that builds — pin a version for builds that must be reproducible"}


def _lib_row(name: str, resolution: str, kind: str, installed: dict[str, dict[str, Any]], overridden: bool = False) -> dict[str, Any]:
    m = re.match(r"^\s*([^,]+?)\s*,\s*([^\s(]+)\s*(?:\((.*)\))?", resolution or "")
    lib = (m.group(1) if m else name).strip()
    req = (m.group(2) if m else "?").strip()
    vendor = (m.group(3) or "").strip() if m else ""
    inst = installed.get(lib.lower(), {})
    versions = inst.get("versions", [])
    eff = (versions[-1] if versions else None) if req == "*" else req
    row: dict[str, Any] = {"name": name, "library": lib, "kind": kind, "requested": req, "vendor": vendor or inst.get("vendor"),
                           "effective": eff, "installed": versions}
    if overridden:
        row["pinned_by"] = "PlaceholderResolution"
    if (kind == "placeholder" and not overridden and req != "*" and versions and req not in versions
            and (vendor or inst.get("vendor") or "").lower() == "system"):
        # N19: a placeholder's DefaultResolution is only the fallback — TwinCAT resolves it through the compiler's
        # placeholder profile first; "(System)" libraries (VisuElems …) are always profile-managed
        row["effective"] = None
        row["info"] = (f"{lib} {req} (DefaultResolution) is not installed; TwinCAT resolves this placeholder through its "
                       f"placeholder profile first (installed: {versions}). Only a problem if the build reports it.")
    elif req != "*" and versions and req not in versions:
        row["warning"] = (f"{lib} {req} is not installed on this PC (installed: {versions}) — the build will fail to resolve it"
                          + (" unless the compiler's placeholder profile maps it" if kind == "placeholder" and not overridden else ""))
    elif req == "*" and not versions and installed:
        row["warning"] = f"{lib} is not installed on this PC"
    return row


def _tree_stamp(root: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.suffix.lower() in (".tcpou", ".tcdut", ".tcgvl", ".tcio") and f.is_file():
            st = f.stat()
            h.update(f"{f}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def _parse_esi(path: Path):
    """Rows for the esi table from one ESI XML (EtherCATInfo): one per Device/revision."""
    tree = ET.parse(path)
    root = tree.getroot()
    vendor = (root.findtext("Vendor/Name") or "").strip()
    for dev in root.iter("Device"):
        t = dev.find("Type")
        if t is None or not (t.text or "").strip():
            continue
        name = next((n.text.strip() for n in dev.findall("Name") if (n.get("LcId") in (None, "1033")) and n.text), "")
        pdos = []
        for kind in ("RxPdo", "TxPdo"):
            for pdo in dev.findall(kind):
                entries = []
                for e in pdo.findall("Entry"):
                    idx = _hexint(e.findtext("Index"))
                    if not idx:
                        continue                      # padding entry
                    entries.append({"index": f"#x{idx:04X}", "sub": _hexint(e.findtext("SubIndex")) or 0,
                                    "bits": _hexint(e.findtext("BitLen")), "name": (e.findtext("Name") or "").strip(),
                                    "type": (e.findtext("DataType") or "").strip()})
                pi = _hexint(pdo.findtext("Index"))
                pdos.append({"dir": "out (RxPdo)" if kind == "RxPdo" else "in (TxPdo)", "index": f"#x{pi:04X}" if pi else None,
                             "name": (pdo.findtext("Name") or "").strip(), "fixed": pdo.get("Fixed") in ("1", "true"),
                             "mandatory": pdo.get("Mandatory") in ("1", "true"), "sm": pdo.get("Sm"), "entries": entries[:64]})
        coe = []
        for obj in dev.iterfind("Profile/Dictionary/Objects/Object"):
            oi = _hexint(obj.findtext("Index"))
            if oi is None:
                continue
            coe.append({"index": f"#x{oi:04X}", "name": (obj.findtext("Name") or "").strip(), "type": (obj.findtext("Type") or "").strip()})
            if len(coe) >= 400:
                break
        yield ((t.text or "").strip(), name, _hexint(t.get("ProductCode")), _hexint(t.get("RevisionNo")), vendor,
               (dev.findtext("GroupType") or "").strip(), str(path), json.dumps(pdos), json.dumps(coe))


class _Conn:
    def __init__(self, path: Path, kb: Knowledge):
        self.path, self.kb = path, kb
        self.con: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self.con = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            self.con.execute("PRAGMA journal_mode=WAL")
            self.con.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            pass
        self.kb._init(self.con)
        return self.con

    def __exit__(self, et, ev, tb):
        assert self.con is not None
        try:
            if et is None:
                self.con.commit()
        finally:
            self.con.close()
