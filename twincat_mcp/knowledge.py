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
the Claude plugins folder (cache/data are replaced or owned by the plugin manager), writable, ≥ 1 GB free. If the
user names none, the agent asks; the proposed fallback is D:\\TwinCAT-MCP\\knowledge (used only when the user agrees
via kb_set_location(use_fallback=true)).

The same folder holds the offline documentation the twincat-docs skill reads (layout of the knowledge README):
  knowledge.db                 this module (projects, ESI, known errors)
  tools\\infosys_local.py       builds helpviewer\\infosys_en.db from the Help Viewer package
  tools\\manuals_db.py          builds manualsdb\\manuals.db from the PDFs in manuals\\
  sop\\                          operator SOPs (Markdown)
kb_status reports all of it; hw_lookup adds the manuals that mention a product code (read-only).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FALLBACK_LOCATION = r"D:\TwinCAT-MCP\knowledge"
MIN_FREE_BYTES = 1 << 30
DB_NAME = "knowledge.db"
POINTER_NAME = "knowledge_location.json"
# the offline documentation next to knowledge.db (built by the knowledge folder's own tools, read by twincat-docs)
INFOSYS_DB = ("helpviewer", "infosys_en.db")
MANUALS_DB = ("manualsdb", "manuals.db")
_SKILL_REL = Path("skills") / "twincat-docs" / "kb_docs.py"


def docs_skill_path() -> Path | None:
    """kb_docs.py of the twincat-docs skill. The plugin server runs from its venv's site-packages, so the plugin folder
    comes from the launcher (TCMCP_PLUGIN_SRC) or Claude Code (CLAUDE_PLUGIN_ROOT); a source checkout has it beside
    the package."""
    roots = [os.environ.get("TCMCP_PLUGIN_SRC"), os.environ.get("CLAUDE_PLUGIN_ROOT"), str(Path(__file__).resolve().parents[1])]
    for r in roots:
        if r and (Path(r) / _SKILL_REL).is_file():
            return Path(r) / _SKILL_REL
    return None
# project unit kinds kb_search can filter on (aliases: DUT = every data type, FB = FUNCTION_BLOCK)
UNIT_KINDS = ("PROGRAM", "FUNCTION_BLOCK", "FUNCTION", "METHOD", "PROPERTY", "GET", "SET", "ACTION", "TRANSITION",
              "INTERFACE", "STRUCT", "UNION", "ENUM", "ALIAS", "GVL", "POU", "DUT")
KIND_ALIASES = {"FB": ("FUNCTION_BLOCK",), "DUT": ("DUT", "STRUCT", "UNION", "ENUM", "ALIAS"), "ITF": ("INTERFACE",)}

LOCATION_CONDITIONS = [
    "a folder on a local fixed drive of this PC (not a network share, mapped drive, NAS or USB stick — SQLite "
    "corrupts databases on network file systems, and a removable drive takes the database with it)",
    "not inside a cloud-synced folder (OneDrive, Dropbox, Google Drive, iCloud, Box) — the sync client locks and "
    "corrupts open database files",
    "not inside the Claude plugins folder (~/.claude/plugins/…: cache, data, marketplaces) or the server's install "
    "folder — they are replaced or owned by the plugin manager",
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
    (r"rpc call .*(adserror 1797\b|0x705\b|parameter size not correct)",
     "the RPC parameter block does not match the method's signature: an input missing or extra, a wrong base type, "
     "or outputs / return_type that differ from the declaration",
     "pass every VAR_INPUT in declaration order with its exact base type (inputs=[{type, value}, …]), every "
     "VAR_OUTPUT in outputs and the method's return type in return_type; check the method in proj_interface"),
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


def check_location(path: str | Path, plugin_dirs: list[Path] | None = None, probe: bool = True,
                   space: bool = True) -> list[str]:
    """Every condition the folder fails, as plain sentences (empty list = usable). probe=False skips the write test
    (R34: read-only calls must not write; parallel calls raced on one probe file), space=False the free-space test."""
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
    # R33: the whole plugins tree — the data folder (~/.claude/plugins/data/…) was accepted while only cache/ was blocked
    plugin_dirs = list(plugin_dirs or []) + [Path.home() / ".claude" / "plugins", Path(__file__).resolve().parents[1]]
    plugin_dirs += [Path(os.environ[e]) for e in ("CLAUDE_PLUGIN_ROOT", "CLAUDE_PLUGIN_DATA") if os.environ.get(e)]
    for d in plugin_dirs:
        try:
            if p.resolve() == d.resolve() or d.resolve() in p.resolve().parents:
                problems.append(f"it is inside the plugin/install folder {d} (replaced on every update)")
                break
        except OSError:
            continue
    if not problems and probe:
        try:
            p.mkdir(parents=True, exist_ok=True)
            # R34 / N20: a name of its own per call — two calls (or two servers) never share the probe file
            f = p / f".tcmcp_write_test_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}"
            f.write_text("x", encoding="ascii")
            f.unlink()
        except OSError as exc:
            problems.append(f"it is not writable ({exc})")
    elif not problems and not p.is_dir():
        problems.append("the folder does not exist (moved or deleted?)")
    if not problems and space:
        try:
            free = shutil.disk_usage(p).free
            if free < MIN_FREE_BYTES:
                problems.append(f"only {free / (1 << 30):.2f} GB free (needs ≥ 1 GB)")
        except OSError:
            pass
    return problems


def norm_path(path: str | Path) -> str:
    """R35: one spelling per folder — resolved, no trailing separator, and case-folded where the file system is
    case-insensitive (Windows), so 'C:\\P15\\' and 'c:\\p15' are the same project."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    return os.path.normcase(os.path.normpath(str(p)))


def display_path(path: str | Path) -> str:
    p = Path(path).expanduser()
    try:
        p = p.resolve()            # Windows: the on-disk spelling of an existing folder
    except OSError:
        pass
    return os.path.normpath(str(p))


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
        # R34: no write test on the way in (a read must not write; parallel calls raced on one probe file) and no
        # free-space test (a full disk is not a reason to move the knowledge base) — only a folder that is really
        # gone or became a network/sync/plugin path is reported as unusable
        problems = self._usable(loc)
        if problems:
            raise KnowledgeError(f"the knowledge folder {loc} is no longer usable: {'; '.join(problems)}. "
                                 "Ask the user for another folder and call kb_set_location(path=…).")
        return loc

    def _usable(self, loc: Path) -> list[str]:
        """Problems that make the folder unusable for reading — no write test, no free-space test (R34). A folder
        configured in the TOML / environment that does not exist yet is created, as the old write probe did; a folder
        chosen earlier with kb_set_location that has gone is reported (moved or deleted — never silently re-created)."""
        problems = check_location(loc, probe=False, space=False)
        if (problems == ["the folder does not exist (moved or deleted?)"] and loc.is_absolute()
                and self.configured and norm_path(self.configured) == norm_path(loc)):
            try:
                loc.mkdir(parents=True, exist_ok=True)
                problems = check_location(loc, probe=False, space=False)
            except OSError as exc:
                problems = [f"it cannot be created ({exc})"]
        return problems

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
            cur, src = self.location()
            if cur is None:
                raise KnowledgeError(self.ask_text())
            # R38: asked without a folder while one is set — that is a question, not a reset
            return {"location": str(cur), "database": str(cur / DB_NAME), "source": src, "changed": False,
                    "note": "the location is unchanged — pass path=… (a folder the user named) to choose another one",
                    "docs": self.docs_layout(cur, brief=True)}
        if not Path(path).expanduser().is_absolute():
            raise KnowledgeError(f"{path!r} is not an absolute path — give the full folder path the user named")
        p = Path(display_path(path))
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
        out["changed"] = old is None or norm_path(old) != norm_path(p)
        out["docs"] = self.docs_layout(p, brief=True)
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
        problems = self._usable(loc)          # R34: no write test on a status call
        if problems:
            out["problems"] = problems
            return out
        low = [x for x in check_location(loc, probe=False, space=True) if "GB free" in x]
        if low:
            out["warning"] = low[0] + " — reads keep working; indexing may fail"
        with self._db() as con:
            out["database"] = str(loc / DB_NAME)
            out["projects"] = [dict(r) for r in con.execute("SELECT path, label, added, indexed, units FROM projects ORDER BY path")]
            out["esi_devices"] = con.execute("SELECT COUNT(*) FROM esi").fetchone()[0]
            out["esi_files"] = con.execute("SELECT COUNT(*) FROM esi_files").fetchone()[0]
            out["fts5"] = bool(self._fts)
        dup: dict[str, list[str]] = {}
        for pr in out["projects"]:
            dup.setdefault(norm_path(pr["path"]), []).append(pr["path"])
        twice = [v for v in dup.values() if len(v) > 1]
        if twice:
            out["duplicate_projects"] = (f"{twice} are the same folder (R35, added before 0.8.8 with another spelling) — "
                                         "kb_add_project on it merges them into one")
        out["known_errors"] = len(KNOWN_ERRORS)
        out["docs"] = self.docs_layout(loc)
        return out

    # -- the offline documentation next to knowledge.db (read-only) ------------------------
    def docs_layout(self, loc: Path | None = None, brief: bool = False) -> dict[str, Any]:
        """What the twincat-docs skill finds in the knowledge folder: the two databases built by the folder's own
        tools (tools\\infosys_local.py, tools\\manuals_db.py), the manuals not indexed yet, the SOPs."""
        loc = loc or self.location()[0]
        if loc is None:
            return {"note": "no knowledge folder chosen yet"}
        script = docs_skill_path()
        inf, man, builders = _docs_parts(loc)
        out: dict[str, Any] = {"skill": "twincat-docs",
                               "call": (f'python "{script}" status --kb "{loc}"' if script else
                                        f'python "<twincat-docs skill folder>/kb_docs.py" status --kb "{loc}"'),
                               "infosys": inf, "manuals": man}
        if not brief:
            out["builders"] = builders
            sop = loc / "sop"
            out["sop"] = sorted(str(f.relative_to(loc)) for f in sop.rglob("*.md"))[:50] if sop.is_dir() else []
        return out

    def docs_status(self) -> dict[str, Any]:
        """The real state of the offline documentation, short: for tc_doctor and the instructions the server sends
        at every (re)connection."""
        loc = self.location()[0]
        if loc is None:
            return {"summary": "no knowledge folder chosen yet (kb_set_location) — no offline documentation", "ok": False}
        inf, man, _b = _docs_parts(loc)
        ok = inf.get("state") == "complete" and not inf.get("rebuild")
        out: dict[str, Any] = {"knowledge_folder": str(loc), "infosys": inf.get("message"), "manuals": man.get("message"),
                               "infosys_state": inf.get("state"), "manuals_state": man.get("state"), "ok": ok,
                               "infosys_rebuild": (inf.get("rebuild") or {}).get("state"),
                               "summary": f"Infosys index: {inf.get('message')}. Manuals: {man.get('message')}."}
        if inf.get("command"):
            out["infosys_command"] = inf["command"]
        if man.get("command"):
            out["manuals_command"] = man["command"]
        return out

    # -- K9 projects ---------------------------------------------------------------------
    @staticmethod
    def _match_projects(con: sqlite3.Connection, ref: str) -> list[dict[str, Any]]:
        """Projects meant by `ref`: the same folder in any spelling (case, separators, trailing backslash), else the
        label (case-insensitive)."""
        rows = [dict(r) for r in con.execute("SELECT path, label, files FROM projects ORDER BY rowid")]
        key = norm_path(ref)
        hit = [r for r in rows if norm_path(r["path"]) == key]
        if not hit:
            hit = [r for r in rows if (r["label"] or "").strip().lower() == ref.strip().lower()]
        return hit

    def _drop_project(self, con: sqlite3.Connection, stored: str) -> int:
        n = con.execute("DELETE FROM units WHERE project=?", (stored,)).rowcount
        if self._fts:
            con.execute("DELETE FROM units_fts WHERE project=?", (stored,))
        con.execute("DELETE FROM projects WHERE path=?", (stored,))
        return n

    @staticmethod
    def _project_list(con: sqlite3.Connection) -> list[str]:
        return [f"{r['label']} ({r['path']})" for r in con.execute("SELECT path, label FROM projects ORDER BY path")]

    def add_project(self, path: str, label: str | None = None) -> dict[str, Any]:
        if not path or not Path(path).expanduser().is_absolute():
            raise KnowledgeError(f"{path!r} is not an existing absolute folder")
        p = Path(display_path(path))
        if not p.is_dir():
            raise KnowledgeError(f"{path!r} is not an existing absolute folder")
        merged: list[str] = []
        with self._db() as con:
            same = self._match_projects(con, str(p))
            same = [r for r in same if norm_path(r["path"]) == norm_path(p)]       # a label is no folder match here
            if same:
                keep = same[0]["path"]
                for d in same[1:]:                                                  # R35: earlier double entries
                    self._drop_project(con, d["path"])
                    merged.append(d["path"])
                if label:
                    con.execute("UPDATE projects SET label=? WHERE path=?", (label, keep))
            else:
                keep = str(p)
                con.execute("INSERT INTO projects(path, label, added) VALUES (?,?,?)",
                            (keep, label or p.name, time.strftime("%Y-%m-%dT%H:%M:%S")))
        res = self._reindex_rows([{"path": keep, "files": None}], force=True)
        if same:
            res["note"] = f"{keep} was already in the knowledge base — re-indexed it instead of adding it again"
        if merged:
            res["merged_duplicates"] = merged
        return res

    def remove_project(self, path: str) -> dict[str, Any]:
        with self._db() as con:
            hit = self._match_projects(con, path or "")
            if not hit:
                raise KnowledgeError(f"{path!r} is not in the knowledge base; indexed: {self._project_list(con) or 'none'}")
            by_folder = {norm_path(r["path"]) for r in hit}
            if len(by_folder) > 1:
                raise KnowledgeError(f"the label {path!r} names {len(by_folder)} different folders: "
                                     f"{[r['path'] for r in hit]} — nothing was removed; pass the folder path")
            n = sum(self._drop_project(con, r["path"]) for r in hit)
        return {"removed": [r["path"] for r in hit], "units_removed": n,
                "note": "only the index entries were removed; your files are untouched"}

    def reindex(self, path: str | None = None) -> dict[str, Any]:
        with self._db() as con:
            if path:
                rows = self._match_projects(con, path)
                if not rows:
                    raise KnowledgeError(f"{path!r} is not in the knowledge base (indexed: {self._project_list(con) or 'none'}); "
                                         "kb_add_project first")
            else:
                rows = [dict(r) for r in con.execute("SELECT path, files FROM projects")]
        return self._reindex_rows(rows, force=bool(path))

    def _reindex_rows(self, rows: list[dict[str, Any]], force: bool) -> dict[str, Any]:
        from .config import ProjectConfig
        from .project_backend import ProjectIndex
        out = []
        for r in rows:
            root = Path(r["path"])
            if not root.is_dir():
                out.append({"project": r["path"], "error": "folder not found (moved or deleted?) — kb_remove_project or re-add"})
                continue
            stamp = _tree_stamp(root)
            if r.get("files") == stamp and not force:
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
            raise KnowledgeError("empty query — give words from the code, a unit name or an error message")
        phrases = [x for x in re.findall(r'"([^"]+)"', q) if re.search(r"\w", x)]
        words = [w for w in re.findall(r"\w+", re.sub(r'"[^"]*"', " ", q))]
        if not phrases and not words:
            # R37: ':=' used to reach FTS as MATCH '' and fail with an empty message
            raise KnowledgeError(f"the query {q!r} has no searchable word: kb_search matches whole words and word "
                                 "prefixes in the code; punctuation such as ':=' is not indexed — search for the names "
                                 "around it (e.g. 'nCount')")
        if not isinstance(limit, int) or not 1 <= limit <= 200:
            raise KnowledgeError(f"limit must be 1–200 (got {limit!r})")                          # R39
        want_kinds: list[str] = []
        for k in kinds or []:
            ku = str(k).strip().upper()
            if ku in KIND_ALIASES:
                want_kinds += KIND_ALIASES[ku]
            elif ku in UNIT_KINDS:
                want_kinds.append(ku)
            else:
                raise KnowledgeError(f"unknown kind {k!r}; kinds: {', '.join(UNIT_KINDS)} (FB = FUNCTION_BLOCK, "
                                     "DUT = every data type)")                                       # R39
        with self._db() as con:
            nproj = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            hits: list[dict[str, Any]] = []
            scope: list[str] = []
            if project:
                m = self._match_projects(con, project)
                if not m:
                    raise KnowledgeError(f"no indexed project is {project!r} (path or label); indexed: "
                                         f"{self._project_list(con) or 'none'}")                    # R35 / R39
                scope = [r["path"] for r in m]
            if nproj:
                hits = self._search_tiers(con, phrases, words, want_kinds, scope, limit)
        err = known_error(q)
        out: dict[str, Any] = {"query": q, "hits": hits, "projects_indexed": nproj}
        if hits and all(h.get("match") == "some words" for h in hits):
            out["note"] = ("no unit contains every word of the query; these contain some of them (best first) — "
                           "fewer or more specific words help")
        if err:
            out["known_error"] = err
        if not nproj:
            out["note"] = "no project folders in the knowledge base yet — kb_add_project(path) with a folder the user names"
        return out

    def _search_tiers(self, con: sqlite3.Connection, phrases: list[str], words: list[str], kinds: list[str],
                      scope: list[str], limit: int) -> list[dict[str, Any]]:
        """R36: every word first (exact, then as word prefixes: 'CounterMod' → CounterModule); only when no unit has
        them all, units with some of them, marked match='some words'."""
        def ph(t: str) -> str:
            return '"' + t.replace('"', '""') + '"'
        exact = [ph(t) for t in phrases + words]
        prefix = [ph(t) for t in phrases] + [ph(w) + "*" for w in words]
        tiers = [("all words", " AND ".join(exact))]
        if words:
            tiers.append(("all words (as prefixes)", " AND ".join(prefix)))
        if len(exact) > 1:
            tiers.append(("some words", " OR ".join(prefix)))
        where_extra, params_extra = "", []
        if scope:
            where_extra += f" AND project IN ({','.join('?' * len(scope))})"
            params_extra += scope
        if kinds:
            where_extra += f" AND kind IN ({','.join('?' * len(kinds))})"
            params_extra += kinds
        hits: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for label, expr in tiers:
            if hits and label == "some words":
                break
            if self._fts:
                sql = ("SELECT project, name, kind, file, snippet(units_fts, 4, '«', '»', ' … ', 12) AS decl, "
                       "snippet(units_fts, 5, '«', '»', ' … ', 12) AS impl, bm25(units_fts, 0, 10, 0, 0, 3, 1, 2) AS score "
                       f"FROM units_fts WHERE units_fts MATCH ?{where_extra} ORDER BY score LIMIT ?")
                params: list[Any] = [expr, *params_extra, limit]
            else:
                terms = phrases + words
                conds = " AND " if label != "some words" else " OR "
                one = "(name LIKE ? ESCAPE '\\' OR declaration LIKE ? ESCAPE '\\' OR implementation LIKE ? ESCAPE '\\' OR comments LIKE ? ESCAPE '\\')"
                sql = ("SELECT project, name, kind, file, substr(declaration,1,200) AS decl, substr(implementation,1,200) AS impl, "
                       f"0 AS score FROM units WHERE ({conds.join([one] * len(terms))}){where_extra} LIMIT ?")
                params = []
                for t in terms:
                    like = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                    params += [like] * 4
                params += [*params_extra, limit]
            try:
                rows = con.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                raise KnowledgeError(f"search failed for {expr!r}: {exc}") from exc
            for r in rows:
                key = (r["project"], r["name"], r["file"])
                if key in seen:
                    continue
                seen.add(key)
                hits.append({"project": r["project"], "unit": r["name"], "kind": r["kind"], "file": r["file"],
                             "declaration": r["decl"], "code": r["impl"], "match": label})
                if len(hits) >= limit:
                    return hits
        return hits

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
            # an index built by an older ESI parser is re-read once (D19: PDOs/CoE were empty)
            if self._esi_parser_stale(con):
                force = True
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
        with self._db() as con:
            con.execute("INSERT OR REPLACE INTO meta VALUES ('esi_parser', ?)", (str(ESI_PARSER_VERSION),))
        return {"folders": [str(d) for d in folders], "files": len(files), "indexed": done, "unchanged": skipped,
                "devices_added": devices, "errors": errors[:20]}

    @staticmethod
    def _esi_parser_stale(con: sqlite3.Connection) -> bool:
        r = con.execute("SELECT v FROM meta WHERE k = 'esi_parser'").fetchone()
        return r is None or r[0] != str(ESI_PARSER_VERSION)

    def hw_lookup(self, query: str, limit: int = 5, revision: int | None = None) -> dict[str, Any]:
        q = (query or "").strip()
        if not q:
            raise KnowledgeError("empty query (e.g. 'EL2008', 'EL3062', '#x0bf03052')")
        with self._db() as con:
            stale = self._esi_parser_stale(con)
        if self._esi_count() == 0 or stale:
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
        bad = sum(1 for d in out_rows for p in d["pdo"] if p.get("unparsed"))
        if bad:
            res["parse_note"] = (f"{bad} PDO(s) could not be read from the ESI file (index or entries missing) — their raw "
                                 "layout is under 'unparsed'; send it (or the ESI file) to get the parser fixed")
        if idx:
            res["indexed_now"] = idx
        if not rows:
            res["note"] = "not in the installed ESI files — update the ESI (XAE: TwinCAT → EtherCAT Devices → Update Device Descriptions)"
        # the PDF manuals of the knowledge folder (read-only, built by manuals_db.py) for the same product codes.
        # N34: ESI type names carry text ('CX9020 EtherCAT slave') — take the order codes out of the query and the types
        codes: list[str] = []
        for t in [q] + types[:5]:
            for c in _PRODUCT_IN.findall(t.upper()):
                if c not in codes:
                    codes.append(c)
        res.update(manuals_for(self.location()[0], codes))
        return res

    def _esi_count(self) -> int:
        with self._db() as con:
            return con.execute("SELECT COUNT(*) FROM esi").fetchone()[0]

    # -- K3 libraries --------------------------------------------------------------------
    def library_folders(self) -> list[Path]:
        pd = os.environ.get("ProgramData") or r"C:\ProgramData"
        cands = [Path(d) for d in self.library_dirs] + [
            # 3.1.4026 (XAE as its own package): the library repository moved to PlcEngineering (D38)
            Path(pd) / "Beckhoff" / "TwinCAT" / "PlcEngineering" / "Managed Libraries",
            Path(r"C:\ProgramData\Beckhoff\TwinCAT\PlcEngineering\Managed Libraries"),
            Path(r"C:\TwinCAT\3.1\Components\Plc\Managed Libraries"),
            Path(r"C:\Program Files (x86)\Beckhoff\TwinCAT\3.1\Components\Plc\Managed Libraries"),
            Path(r"C:\ProgramData\Beckhoff\TwinCAT\3.1\Components\Plc\Managed Libraries"),
        ]
        out: list[Path] = []
        for c in cands:
            if c.is_dir() and c not in out:
                out.append(c)
        return out

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
        # N19b: with one installed version the profile can only resolve to it; with several, the profile decides
        row["effective"] = versions[0] if len(versions) == 1 else "profile"
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


def ro_uri(db: Path) -> str:
    """SQLite read-only URI: 'file:/C:/x.db', 'file:////server/share/x.db' (Path.as_uri gives a UNC host that SQLite
    rejects as a URI authority)."""
    from urllib.parse import quote
    s = str(db.resolve()).replace("\\", "/")
    if s.startswith("//"):
        s = "//" + s
    elif not s.startswith("/"):
        s = "/" + s
    return "file:" + quote(s, safe="/:") + "?mode=ro"


def _ro(db: Path) -> sqlite3.Connection:
    """The documentation databases belong to the knowledge folder's own tools: opened read-only, never changed here."""
    con = sqlite3.connect(ro_uri(db), uri=True, timeout=5, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


_KBD: dict[str, Any] = {}


def kb_docs_module() -> Any | None:
    """The twincat-docs skill's kb_docs.py, loaded as a module: its index_state / manuals_state / overview are the one
    implementation of the rules (the builders' `stats --json` give the same answer)."""
    p = docs_skill_path()
    if p is None:
        return None
    key = f"{p}|{p.stat().st_mtime_ns}"
    if _KBD.get("key") != key:
        import importlib.util
        spec = importlib.util.spec_from_file_location("tcmcp_kb_docs", p)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _KBD.update(key=key, mod=mod)
    return _KBD["mod"]


def _builder_version(f: Path) -> str | None:
    """Version of a builder copy in <kb>\\tools (version 1 had no BUILDER_VERSION line)."""
    try:
        m = re.search(r'^BUILDER_VERSION = "(\d+)"', f.read_text(encoding="utf-8", errors="replace"), re.M)
    except OSError:
        return None
    return m.group(1) if m else "1"


def _builders(loc: Path) -> dict[str, Any]:
    shipped = docs_skill_path()
    bdir = shipped.parent / "builders" if shipped else None
    out: dict[str, Any] = {}
    for n in ("infosys_local.py", "manuals_db.py"):
        plug = bdir / n if bdir and (bdir / n).is_file() else None
        copy = loc / "tools" / n
        e: dict[str, Any] = {"shipped": str(plug) if plug else None,
                             "shipped_version": _builder_version(plug) if plug else None,
                             "copy_in_tools": str(copy) if copy.is_file() else None}
        if copy.is_file():
            e["copy_version"] = _builder_version(copy)
            if plug and e["copy_version"] != e["shipped_version"]:
                e["note"] = (f"tools\\{n} is version {e['copy_version']}, the plugin ships version {e['shipped_version']} — "
                             f"run the shipped one, or copy it into tools\\")
        out[n] = e
    return out


def _cmd(builders: dict[str, Any], name: str, loc: Path, sub: str) -> str:
    b = builders.get(name) or {}
    script = b.get("shipped") or b.get("copy_in_tools") or name
    return f'<Python 3.12+> "{script}" {sub} --kb "{loc}"'


def _docs_parts(loc: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    idb, mdb = loc / INFOSYS_DB[0] / INFOSYS_DB[1], loc / MANUALS_DB[0] / MANUALS_DB[1]
    builders = _builders(loc)
    kd = kb_docs_module()
    if kd is not None:
        try:
            inf = kd.index_state(idb)
        except Exception as exc:  # noqa: BLE001
            inf = {"database": str(idb), "state": "error", "message": f"not readable: {exc}"}
        try:
            man = kd.manuals_state(mdb, loc / "manuals")
        except Exception as exc:  # noqa: BLE001
            man = {"database": str(mdb), "state": "error", "message": f"not readable: {exc}"}
    else:
        inf = {"database": str(idb), "state": "unknown", "message": "the twincat-docs skill is not installed next to the server"}
        man = {"database": str(mdb), "state": "unknown", "message": inf["message"]}
    inf["exists"], man["exists"] = idb.is_file(), mdb.is_file()
    if idb.is_file():
        inf["size_mb"] = round(idb.stat().st_size / 1e6, 1)
    if inf.get("state") != "complete" or inf.get("rebuild"):
        inf["command"] = _cmd(builders, "infosys_local.py", loc, "index")
    if man.get("state") != "up_to_date" or man.get("updating"):
        man["command"] = _cmd(builders, "manuals_db.py", loc, "update") + "   (PDF text: pypdf, or PyMuPDF if you installed it)"
    return inf, man, builders


_PRODUCT = re.compile(r"^[A-Za-z]{1,4}\d{3,5}(?:-\d{4})?$")
_PRODUCT_IN = re.compile(r"\b([A-Z]{1,4}\d{3,5}(?:-\d{4})?)\b")


def manuals_for(loc: Path | None, codes: list[str]) -> dict[str, Any]:
    """Which indexed PDF manuals mention these product codes (manuals_db.py's product table: exact code or CODE-xxxx)."""
    if loc is None or not codes:
        return {}
    db = loc / MANUALS_DB[0] / MANUALS_DB[1]
    if not db.is_file():
        return {"manuals_note": f"no manuals database at {db} — PDF manuals copied into {loc / 'manuals'} and indexed "
                                "with manuals_db.py update show up here (and in the twincat-docs skill)"}
    found: dict[str, list[dict[str, Any]]] = {}
    try:
        con = _ro(db)
        try:
            for code in codes[:3]:
                c = code.strip().upper()
                like = c.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "-%"
                rows = con.execute("SELECT d.name, d.id, GROUP_CONCAT(p.page) AS pages, SUM(p.n) AS hits FROM product p "
                                   "JOIN doc d ON d.id = p.doc WHERE p.code = ? OR p.code LIKE ? ESCAPE '\\' "
                                   "GROUP BY p.doc ORDER BY hits DESC LIMIT 5", (c, like)).fetchall()
                if rows:
                    found[c] = [{"doc": r["name"], "doc_id": r["id"], "mentions": r["hits"],
                                 "pages": sorted({int(x) for x in (r["pages"] or "").split(",") if x})[:20]} for r in rows]
        finally:
            con.close()
    except sqlite3.Error as exc:
        return {"manuals_note": f"manuals database not readable: {exc}"}
    if not found:
        return {"manuals_note": f"no indexed manual mentions {', '.join(codes[:3])} — copy its PDF into "
                                f"{loc / 'manuals'} and run manuals_db.py update"}
    return {"manuals": found, "manuals_more": "twincat-docs skill: manual_search query=… doc=… / manual_page"}


ESI_PARSER_VERSION = 3       # bump when _parse_esi's output changes: every ESI file is read again once


def _ln(tag: Any) -> str:
    """Local name of an element tag ('{ns}TxPdo' → 'TxPdo')."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _kids(el: Any, name: str) -> list[Any]:
    return [c for c in el if _ln(c.tag) == name]


def _text(el: Any, name: str, prefer_lcid: str = "1033") -> str:
    """Text of the child `name` — namespace-agnostic, English (LcId 1033) first when there are several."""
    cs = _kids(el, name)
    if not cs:
        return ""
    # (an Element without children is falsy: compare with None, never `or`)
    best = next((c for c in cs if c.get("LcId") == prefer_lcid), None)
    if best is None:
        best = next((c for c in cs if c.get("LcId") is None), cs[0])
    return " ".join("".join(best.itertext()).split())


def _deep_text(el: Any, name: str, skip: str = "") -> str:
    """First descendant `name` whose path does not run through a `skip` element (a PDO's own Index, not an Entry's)."""
    stack = [c for c in el]
    while stack:
        c = stack.pop(0)
        ln = _ln(c.tag)
        if skip and ln == skip:
            continue
        if ln == name:
            return " ".join("".join(c.itertext()).split())
        stack.extend(list(c))
    return ""


def _parse_esi(path: Path):
    """Rows for the esi table from one ESI XML (EtherCATInfo): one per Device/revision. D19: namespace-agnostic,
    PDO / entry indices from their element text (#x1A00), English names first, CoE objects from the dictionary."""
    tree = ET.parse(path)
    root = tree.getroot()
    vendor = next((_text(v, "Name") for v in root.iter() if _ln(v.tag) == "Vendor"), "")
    for dev in (d for d in root.iter() if _ln(d.tag) == "Device"):
        t = next(iter(_kids(dev, "Type")), None)
        if t is None or not (t.text or "").strip():
            continue
        name = _text(dev, "Name")
        pdos = []
        for kind in ("RxPdo", "TxPdo"):
            for pdo in _kids(dev, kind):
                entries = []
                for e in _kids(pdo, "Entry"):
                    idx = _hexint(_text(e, "Index"))
                    if not idx:
                        continue                      # padding entry
                    entries.append({"index": f"#x{idx:04X}", "sub": _hexint(_text(e, "SubIndex")) or 0,
                                    "bits": _hexint(_text(e, "BitLen")), "name": _text(e, "Name"),
                                    "type": _text(e, "DataType")})
                pi = _hexint(_text(pdo, "Index") or _deep_text(pdo, "Index", skip="Entry"))
                row = {"dir": "out (RxPdo)" if kind == "RxPdo" else "in (TxPdo)", "index": f"#x{pi:04X}" if pi else None,
                       "name": _text(pdo, "Name") or _deep_text(pdo, "Name", skip="Entry"),
                       "fixed": pdo.get("Fixed") in ("1", "true"),
                       "mandatory": pdo.get("Mandatory") in ("1", "true"), "sm": pdo.get("Sm"), "entries": entries[:64]}
                if pi is None or not entries:
                    # D19 (still empty on the real files): keep what the element holds so the layout can be fixed
                    row["unparsed"] = {"attrs": dict(pdo.attrib), "children": sorted({_ln(c.tag) for c in pdo})[:20],
                                       "text": " ".join("".join(pdo.itertext()).split())[:160]}
                pdos.append(row)
        coe = []
        objs = [o for prof in _kids(dev, "Profile") for dic in _kids(prof, "Dictionary")
                for ob in _kids(dic, "Objects") for o in _kids(ob, "Object")]
        for obj in objs:
            oi = _hexint(_text(obj, "Index"))
            if oi is None:
                continue
            coe.append({"index": f"#x{oi:04X}", "name": _text(obj, "Name"), "type": _text(obj, "Type")})
            if len(coe) >= 400:
                break
        yield ((t.text or "").strip(), name, _hexint(t.get("ProductCode")), _hexint(t.get("RevisionNo")), vendor,
               _text(dev, "GroupType"), str(path), json.dumps(pdos), json.dumps(coe))


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
