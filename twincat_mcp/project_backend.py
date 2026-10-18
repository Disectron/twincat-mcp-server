"""Offline project backend.

Parses the TwinCAT source tree (.tsproj / .plcproj / .TcPOU / .TcDUT /
.TcGVL / .TcIO) into an index the agent can query without XAE running:

* list objects (programs, FBs, functions, DUTs, GVLs, interfaces, methods,
  properties, actions), with file + declared variables;
* get code with line numbers (declaration / implementation / method …);
* resolve an ADS symbol path (``MAIN.fbFill.fbValve.bOpen``) to the file
  and line where each hop is declared, following variable types;
* regex search across all ST with file:line hits;
* references: where is ``FB_X`` instantiated / called;
* write back declaration/implementation of an object (dropping the stale
  ``<LineIds>`` for that object so XAE regenerates them cleanly).

No tree-sitter, no vector DB: TwinCAT files are XML + ST text, and a
regex-based structural pass is sufficient for navigation and the LineIds
discipline. (Semantic search à la twincat-index-mcp is a different tool.)
"""
from __future__ import annotations

import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

from .config import ProjectConfig

SRC_EXT = {".tcpou": "POU", ".tcdut": "DUT", ".tcgvl": "GVL", ".tcio": "Itf"}

_RE_VAR_BLOCK = re.compile(
    r"^[ \t]*(VAR_INPUT|VAR_OUTPUT|VAR_IN_OUT|VAR_GLOBAL|VAR_STAT|VAR_TEMP|VAR_INST|VAR_EXTERNAL|VAR|STRUCT|UNION)\b"
    r"(?P<quals>(?:[ \t]+(?:CONSTANT|RETAIN|PERSISTENT|NON_RETAIN))*)[ \t]*$(.*?)^[ \t]*END_(?:VAR|STRUCT|UNION)",
    re.M | re.S | re.I)
_RE_VAR_DECL = re.compile(
    r"^[ \t]*(?P<names>[A-Za-z_][\w]*(?:[ \t]*,[ \t]*[A-Za-z_][\w]*)*)[ \t]*(?:AT[ \t]+(?P<at>%[IQM][XBWDL]?[\d\.\*]+))?[ \t]*:[ \t]*"
    r"(?P<type>[^:;=\n]+?)(?:[ \t]*:=[ \t]*(?P<init>[^;]*))?[ \t]*;[ \t]*(?:\(\*(?P<cmt>.*?)\*\)|//(?P<cmt2>.*))?",
    re.M)
_RE_HEADER = re.compile(
    r"^\s*(?P<kind>FUNCTION_BLOCK|PROGRAM|FUNCTION|INTERFACE|METHOD|PROPERTY|TYPE)\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|ABSTRACT|FINAL)\s+)*(?P<name>[A-Za-z_]\w*)"
    r"(?:\s*:\s*(?P<ret>[\w\.\(\)\[\] ]+?))?(?:\s+EXTENDS\s+(?P<ext>[\w\., ]+?))?(?:\s+IMPLEMENTS\s+(?P<impl>[\w\., ]+?))?\s*$",
    re.M | re.I)
_RE_ARRAY_OF = re.compile(r"^ARRAY\s*\[.*?\]\s*OF\s+(.+)$", re.I)
_RE_PTR = re.compile(r"^(?:POINTER|REFERENCE)\s+TO\s+(.+)$", re.I)


@dataclass
class VarDecl:
    name: str
    type: str
    section: str
    line: int
    init: str | None = None
    at: str | None = None
    comment: str | None = None
    attrs: dict[str, str] = field(default_factory=dict)   # {attribute 'name' := 'value'} pragmas above the line
    qualifiers: tuple[str, ...] = ()                       # CONSTANT / RETAIN / PERSISTENT / NON_RETAIN on the VAR block

    @property
    def constant(self) -> bool:
        return "CONSTANT" in self.qualifiers


def _section_text(v: "VarDecl") -> str:
    """D18: the section as declared, qualifiers included ('VAR_GLOBAL CONSTANT', 'VAR RETAIN')."""
    return " ".join((v.section, *v.qualifiers)) if v.qualifiers else v.section


_RE_ATTR = re.compile(r"\{\s*attribute\s+'([^']+)'\s*(?::=\s*'([^']*)')?\s*\}", re.I)


@dataclass
class CodeUnit:
    """A POU / DUT / GVL / interface or one of its sub-items."""
    name: str                  # e.g. FB_Valve or FB_Valve.Open
    kind: str                  # PROGRAM/FUNCTION_BLOCK/FUNCTION/DUT/GVL/INTERFACE/METHOD/PROPERTY/ACTION/GET/SET
    file: str
    xml_path: str              # element path inside the file, e.g. POU or POU/Method[@Name='Open']
    declaration: str = ""
    implementation: str = ""
    decl_line0: int = 0        # 1-based file line of the first declaration text line (0 = unknown)
    impl_line0: int = 0        # 1-based file line of the first implementation (ST) text line
    vars: list[VarDecl] = field(default_factory=list)
    extends: list[str] = field(default_factory=list)
    implements: list[str] = field(default_factory=list)
    return_type: str | None = None
    parent: str | None = None

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "file": self.file,
                "vars": len(self.vars), "extends": self.extends, "implements": self.implements,
                "return_type": self.return_type, "impl_lines": self.implementation.count("\n") + (1 if self.implementation else 0)}


_RE_LINE_COMMENT = re.compile(r"//.*$")
_RE_BLOCK_COMMENT = re.compile(r"\(\*.*?\*\)")
_RE_STR_LIT = re.compile(r"'[^']*'")


def _strip_comments_line(text: str) -> str:
    """Code part of one line: block comments, // comments and string literals removed."""
    return _RE_LINE_COMMENT.sub("", _RE_STR_LIT.sub("''", _RE_BLOCK_COMMENT.sub("", text or "")))


class ProjectError(RuntimeError):
    pass


class ProjectIndex:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.root: Path | None = Path(cfg.root) if cfg.root else None
        self.units: dict[str, CodeUnit] = {}      # lower-case name -> unit
        self.duplicates: dict[str, list[str]] = {}  # lower-case name -> files when the same name exists twice
        self.files: dict[str, float] = {}         # path -> mtime
        self.solutions: list[str] = []
        self.tsproj: list[str] = []
        self.plcproj: list[dict[str, Any]] = []
        self.scanned_at: float | None = None
        # N26: tools run in worker threads; parallel first calls must not scan concurrently (each scan clears
        # and re-appends → duplicated plcproj/units). Re-entrant: refresh() may fall back to scan().
        self._scan_lock = threading.RLock()

    # ------------------------------------------------------------ scan
    def scan(self, root: str | None = None) -> dict[str, Any]:
        with self._scan_lock:
            return self._scan(root)

    def _scan(self, root: str | None = None) -> dict[str, Any]:
        if root:
            self.root = Path(root)
        if self.root is None or not self.root.is_dir():
            raise ProjectError(f"project root not set or not a directory: {self.root}. "
                               "Pass root= or set TCMCP_PROJECT_ROOT / TCMCP_SOLUTION.")
        t0 = time.time()
        self.units.clear(); self.files.clear(); self.plcproj.clear(); self.duplicates.clear()
        self.solutions = [str(p) for p in self.root.rglob("*.sln")]
        self.tsproj = [str(p) for p in self.root.rglob("*.tsproj")]
        for pp in self.root.rglob("*.plcproj"):
            self.plcproj.append(self._parse_plcproj(pp))
        n_files = 0
        for p in self.root.rglob("*"):
            if p.suffix.lower() in SRC_EXT and p.is_file():
                if "_Boot" in p.parts or "_CompileInfo" in p.parts or "_Libraries" in p.parts:
                    continue
                try:
                    self._index_file(p)
                    n_files += 1
                except ET.ParseError as exc:
                    self.files[str(p)] = -1
                    self.units[f"__parse_error__{p.name}".lower()] = CodeUnit(p.name, "PARSE_ERROR", str(p), "", str(exc))
        self.scanned_at = time.time()
        kinds: dict[str, int] = {}
        for u in self.units.values():
            kinds[u.kind] = kinds.get(u.kind, 0) + 1
        return {"root": str(self.root), "files": n_files, "units": len(self.units), "by_kind": kinds,
                "duplicate_names": self.duplicates,
                "solutions": self.solutions, "tsproj": self.tsproj,
                "plcproj": [{"file": p["file"], "name": p["name"], "compile_items": len(p["compile"])} for p in self.plcproj],
                "duration_s": round(time.time() - t0, 2)}

    REFRESH_MIN_S = 2.0   # index-based tools re-check file mtimes at most this often

    def _ensure(self) -> None:
        """Scan on first use; afterwards an incremental mtime refresh (throttled) so objects XAE just created or
        edited are seen without a manual proj_scan — a stale index silently answering is worse than a slow one."""
        if self.scanned_at is None:
            with self._scan_lock:
                if self.scanned_at is None:          # another thread may have finished the scan meanwhile
                    self.scan()
            return
        if time.time() - self.scanned_at >= self.REFRESH_MIN_S and self.root is not None and self.root.is_dir():
            try:
                self.refresh()
            except OSError:
                pass

    def ensure_fresh(self) -> dict[str, Any] | None:
        """Public: scan or refresh now (no throttle); returns the refresh delta, or None when no root is set."""
        if self.root is None:
            return None
        if self.scanned_at is None:
            r = self.scan()
            return {"scanned": True, "units": r["units"]}
        return self.refresh()

    def refresh(self) -> dict[str, Any]:
        """Incremental rescan: re-index files whose mtime changed, pick up new files, drop deleted ones."""
        with self._scan_lock:
            return self._refresh()

    def _refresh(self) -> dict[str, Any]:
        if self.scanned_at is None or self.root is None:
            return self.scan()
        changed, added, removed = [], [], []
        seen: set[str] = set()
        for p in self.root.rglob("*"):
            if p.suffix.lower() in SRC_EXT and p.is_file():
                if "_Boot" in p.parts or "_CompileInfo" in p.parts or "_Libraries" in p.parts:
                    continue
                sp = str(p)
                seen.add(sp)
                mt = p.stat().st_mtime
                if sp not in self.files:
                    added.append(sp)
                elif self.files[sp] != mt:
                    changed.append(sp)
                else:
                    continue
                try:
                    self._drop_file(sp)
                    self._index_file(p)
                except ET.ParseError as exc:
                    self.files[sp] = -1
                    self.units[f"__parse_error__{p.name}".lower()] = CodeUnit(p.name, "PARSE_ERROR", sp, "", str(exc))
        for sp in list(self.files):
            if sp not in seen:
                removed.append(sp)
                self._drop_file(sp)
                self.files.pop(sp, None)
        self.scanned_at = time.time()
        return {"changed": changed, "added": added, "removed": removed, "units": len(self.units)}

    def _drop_file(self, path: str) -> None:
        for k in [k for k, u in self.units.items() if u.file == path]:
            self.units.pop(k, None)

    def changed_units(self, since: str = "HEAD") -> dict[str, Any]:
        """Units whose files differ from a git revision (default: uncommitted changes), plus
        untracked source files. Pairs with proj_vs_runtime: 'what did I change, is it deployed?'"""
        import subprocess
        self._ensure()
        if self.root is None:
            raise ProjectError("project root not set")
        try:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=self.root, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProjectError(f"git not available: {exc}") from exc
        if top.returncode != 0:
            raise ProjectError(f"{self.root} is not inside a git repository")
        repo = Path(top.stdout.strip())
        diff = subprocess.run(["git", "diff", "--name-status", since, "--", "."], cwd=self.root, capture_output=True, text=True, timeout=60)
        if diff.returncode != 0:
            raise ProjectError(f"git diff failed: {diff.stderr.strip()}")
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", "."], cwd=self.root, capture_output=True, text=True, timeout=60)
        files: dict[str, str] = {}
        for ln in diff.stdout.splitlines():
            parts = ln.split("\t")
            if len(parts) >= 2:
                files[str((repo / parts[-1]).resolve())] = parts[0][:1]
        for ln in untracked.stdout.splitlines():
            if ln.strip():
                files[str((repo / ln.strip()).resolve())] = "?"
        units = []
        for u in self.units.values():
            st = files.get(str(Path(u.file).resolve()))
            if st and not u.parent:
                units.append({"name": u.name, "kind": u.kind, "file": u.file, "status": {"M": "modified", "A": "added", "D": "deleted", "?": "untracked", "R": "renamed"}.get(st, st)})
        other = [f for f in files if f.lower().endswith((".tsproj", ".plcproj", ".xti", ".tmc", ".sln"))]
        return {"since": since, "repo": str(repo), "units": units, "project_files": other, "all_changed_files": len(files)}

    @staticmethod
    def _parse_plcproj(p: Path) -> dict[str, Any]:
        d: dict[str, Any] = {"file": str(p), "name": p.stem, "compile": [], "libraries": [], "project_guid": None}
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError as exc:
            d["error"] = str(exc)
            return d
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        for el in root.iter(f"{ns}Compile"):
            inc = el.get("Include")
            if inc:
                d["compile"].append(inc)
        for el in root.iter(f"{ns}PlaceholderReference"):
            d["libraries"].append(el.get("Include"))
        for el in root.iter(f"{ns}LibraryReference"):
            d["libraries"].append(el.get("Include"))
        g = root.find(f".//{ns}ProjectGuid")
        if g is not None:
            d["project_guid"] = g.text
        return d

    def _index_file(self, p: Path) -> None:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        self.files[str(p)] = p.stat().st_mtime
        root = ET.fromstring(text)
        kind_tag = SRC_EXT[p.suffix.lower()]
        obj = root.find(kind_tag)
        if obj is None:
            return
        name = obj.get("Name") or p.stem
        decl = _cdata(obj, "Declaration")
        impl = _cdata_impl(obj)
        kind = _kind_from_decl(decl, kind_tag)
        top_off = text.find(f"<{kind_tag} ")
        dl, il = _code_lines(text, top_off)
        unit = CodeUnit(name, kind, str(p), kind_tag, decl, impl, dl, il)
        self._fill_header(unit)
        unit.vars = parse_vars(decl, unit.decl_line0)
        prev = self.units.get(name.lower())
        if prev is not None and prev.file != str(p):
            self.duplicates.setdefault(name.lower(), [prev.file]).append(str(p))
        self.units[name.lower()] = unit
        # sub-items
        for tag, k in (("Method", "METHOD"), ("Property", "PROPERTY"), ("Action", "ACTION"), ("Transition", "TRANSITION")):
            for i, sub in enumerate(obj.findall(tag)):
                sname = sub.get("Name") or f"{tag}{i}"
                full = f"{name}.{sname}"
                sdecl = _cdata(sub, "Declaration")
                simpl = _cdata_impl(sub)
                sdl, sil = _code_lines(text, text.find(f'<{tag} Name="{sname}"'))
                su = CodeUnit(full, k, str(p), f"{kind_tag}/{tag}[@Name='{sname}']", sdecl, simpl,
                              sdl, sil, parent=name)
                self._fill_header(su)
                su.vars = parse_vars(sdecl, su.decl_line0)
                self.units[full.lower()] = su
                if tag == "Property":
                    for acc in ("Get", "Set"):
                        a = sub.find(acc)
                        if a is not None:
                            prop_off = text.find(f'<{tag} Name="{sname}"')
                            adl, ail = _code_lines(text, text.find(f"<{acc}", prop_off))
                            au = CodeUnit(f"{full}.{acc}", acc.upper(), str(p),
                                          f"{kind_tag}/{tag}[@Name='{sname}']/{acc}",
                                          _cdata(a, "Declaration"), _cdata_impl(a), adl, ail, parent=full)
                            self.units[au.name.lower()] = au

    def _fill_header(self, u: CodeUnit) -> None:
        m = _RE_HEADER.search(u.declaration or "")
        if not m:
            return
        if m.group("ext"):
            u.extends = [x.strip() for x in m.group("ext").split(",") if x.strip()]
        if m.group("impl"):
            u.implements = [x.strip() for x in m.group("impl").split(",") if x.strip()]
        if m.group("ret"):
            u.return_type = m.group("ret").strip()

    # ------------------------------------------------------------ queries
    def list_units(self, kind: str | None = None, pattern: str | None = None, limit: int = 500) -> dict[str, Any]:
        self._ensure()
        import fnmatch
        pat = (pattern or "*").lower()
        if "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        sel = [u for u in self.units.values()
               if (not kind or u.kind.lower() == kind.lower()) and fnmatch.fnmatchcase(u.name.lower(), pat)]
        sel.sort(key=lambda u: u.name.lower())
        return {"total": len(sel), "units": [u.summary() for u in sel[:limit]]}

    def get_unit(self, name: str, exact: bool = False) -> CodeUnit:
        """Look up by name; ``file@Name`` pins a unit when the same name exists in
        two PLC projects. ``exact`` disables the suffix match (used for writes)."""
        self._ensure()
        pin = None
        if "@" in name:
            pin, name = name.split("@", 1)
        key = name.lower()
        if key in self.duplicates and pin is None:
            raise ProjectError(f"{name!r} exists in several files {self.duplicates[key]}; "
                               f"qualify it as '<file-substring>@{name}'")
        if pin is not None:
            cands = [x for x in self.units.values() if x.name.lower() == key and pin.lower() in x.file.lower()]
            # duplicates overwrite each other in self.units; re-index the pinned file on demand
            if not cands and key in self.duplicates:
                for f in self.duplicates[key]:
                    if pin.lower() in f.lower():
                        self._index_file(Path(f))
                        cands = [x for x in self.units.values() if x.name.lower() == key and pin.lower() in x.file.lower()]
            if len(cands) != 1:
                raise ProjectError(f"'{pin}@{name}' matches {len(cands)} units")
            return cands[0]
        u = self.units.get(key)
        if u is None and not exact:
            # allow Method-name lookups ("Open" -> "FB_Valve.Open") when unique
            cands = [x for x in self.units.values() if x.name.lower().endswith("." + key)]
            if len(cands) == 1:
                return cands[0]
        if u is None:
            raise ProjectError(f"object {name!r} not found in index ({len(self.units)} units). proj_list to browse.")
        return u

    def get_code(self, name: str, part: str = "both", with_line_numbers: bool = True) -> dict[str, Any]:
        u = self.get_unit(name)
        out: dict[str, Any] = {"name": u.name, "kind": u.kind, "file": u.file, "xml_path": u.xml_path}
        if part in ("both", "declaration"):
            out["declaration"] = _numbered(u.declaration, u.decl_line0) if with_line_numbers else u.declaration
        if part in ("both", "implementation"):
            out["implementation"] = _numbered(u.implementation, u.impl_line0) if with_line_numbers else u.implementation
        out["vars"] = [asdict(v) for v in u.vars]
        subs = [x.summary() for x in self.units.values() if x.parent == u.name]
        if subs:
            out["subitems"] = subs
        return out

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        """MAIN.fbFill.fbValve.bOpen → hop list with declaring unit/file/line/type."""
        self._ensure()
        parts = [p for p in re.split(r"\.(?![^\[]*\])", symbol.strip()) if p]
        if not parts:
            raise ProjectError("empty symbol")
        hops: list[dict[str, Any]] = []
        # first hop: program / GVL / FB name
        cur = self.units.get(parts[0].lower())
        if cur is None:
            # maybe GVL without prefix: search all GVLs for the var
            hit = self._find_var_anywhere(parts[0], kinds=("GVL",))
            if hit:
                unit, var = hit
                hops.append({"segment": parts[0], "declared_in": unit.name, "file": unit.file, "line": var.line, "type": var.type, "section": var.section,
                             "constant": var.constant})
                cur = self._unit_for_type(var.type)
                parts = parts[1:]
            else:
                raise ProjectError(f"no program/GVL/FB named {parts[0]!r}; try proj_search")
        else:
            hops.append({"segment": parts[0], "declared_in": cur.name, "file": cur.file, "line": cur.decl_line0, "type": cur.kind})
            parts = parts[1:]
        for seg in parts:
            if cur is None:
                prev_type = hops[-1].get("type") if hops else None
                hops.append({"segment": seg, "library_member": True,
                             "note": f"inside {prev_type!r}, a type the project does not define (library FB/struct) — the "
                                     f"runtime datatype table knows its layout (ads_symbol_info / ads_types), infosys_declaration its meaning"})
                break
            base = re.sub(r"\[.*\]$", "", seg)
            var = self._find_var(cur, base)
            if var is None:
                # method/property/action?
                sub = self.units.get(f"{cur.name}.{base}".lower())
                if sub:
                    hops.append({"segment": seg, "declared_in": sub.name, "file": sub.file, "line": sub.decl_line0, "type": sub.kind})
                    cur = None
                    continue
                hops.append({"segment": seg, "error": f"{base!r} not declared in {cur.name} (or its EXTENDS chain)"})
                break
            unit_of_var, v = var
            hops.append({"segment": seg, "declared_in": unit_of_var.name, "file": unit_of_var.file, "line": v.line,
                         "type": v.type, "section": v.section, "comment": v.comment, "constant": v.constant})
            cur = self._unit_for_type(v.type)
        library_tail = [h["segment"] for h in hops if h.get("library_member")]
        return {"symbol": symbol, "hops": hops, "resolved": all("error" not in h for h in hops),
                "resolved_to_library_type": bool(library_tail),
                "library_type": next((h.get("type") for h in reversed(hops) if h.get("type") and not h.get("library_member")), None) if library_tail else None}

    def _find_var(self, u: CodeUnit, name: str) -> tuple[CodeUnit, VarDecl] | None:
        seen = set()
        stack = [u]
        while stack:
            cur = stack.pop(0)
            if cur.name.lower() in seen:
                continue
            seen.add(cur.name.lower())
            for v in cur.vars:
                if v.name.lower() == name.lower():
                    return cur, v
            for e in cur.extends:
                eu = self.units.get(e.lower())
                if eu:
                    stack.append(eu)
        return None

    def _find_var_anywhere(self, name: str, kinds: Iterable[str]) -> tuple[CodeUnit, VarDecl] | None:
        ks = {k.upper() for k in kinds}
        for u in self.units.values():
            if u.kind.upper() in ks:
                for v in u.vars:
                    if v.name.lower() == name.lower():
                        return u, v
        return None

    def _unit_for_type(self, type_str: str) -> CodeUnit | None:
        t = type_str.strip()
        if m := _RE_ARRAY_OF.match(t):
            t = m.group(1).strip()
        if m := _RE_PTR.match(t):
            t = m.group(1).strip()
        t = t.split("(")[0].strip()
        if "." in t:  # library-qualified
            t = t.split(".")[-1]
        return self.units.get(t.lower())

    def search(self, regex: str, kinds: list[str] | None = None, max_hits: int = 200,
               ignore_case: bool = True) -> dict[str, Any]:
        self._ensure()
        try:
            rx = re.compile(regex, re.I if ignore_case else 0)
        except re.error as exc:
            raise ProjectError(f"bad regex: {exc}") from exc
        hits: list[dict[str, Any]] = []
        ks = {k.upper() for k in kinds} if kinds else None
        for u in self.units.values():
            if ks and u.kind.upper() not in ks:
                continue
            for part, text, base in (("declaration", u.declaration, u.decl_line0), ("implementation", u.implementation, u.impl_line0)):
                for i, ln in enumerate(text.splitlines(), start=1):
                    if rx.search(ln):
                        hits.append({"unit": u.name, "part": part, "file": u.file, "line": base + i - 1, "text": ln.strip()[:200]})
                        if len(hits) >= max_hits:
                            return {"regex": regex, "hits": hits, "truncated": True}
        return {"regex": regex, "hits": hits, "truncated": False}

    def references(self, name: str, max_hits: int = 200) -> dict[str, Any]:
        self._ensure()
        rx = re.compile(rf"\b{re.escape(name)}\b", re.I)
        res = self.search(rx.pattern, None, max_hits, True)
        decl_hits = []
        for h in res["hits"]:
            if h["unit"].lower() == name.lower():
                continue
            # a hit inside a comment or string is a mention of the word, not a use of the type
            code_part = _strip_comments_line(h["text"])
            h = dict(h)
            h["in_comment"] = not rx.search(code_part)
            decl_hits.append(h)
        instances = []
        for u in self.units.values():
            for v in u.vars:
                base = self._base_type_name(v.type)
                tu = self._unit_for_type(v.type)
                if (tu is not None and tu.name.lower() == name.lower()) or (tu is None and base.lower() == name.lower()):
                    instances.append({"unit": u.name, "var": v.name, "type": v.type, "file": u.file, "line": v.line,
                                      "declared_in_project": tu is not None})   # False = library type (Tc2_Standard.CTU …)
        return {"name": name, "instances": instances,
                "mentions": [h for h in decl_hits if not h["in_comment"]],
                "mentions_in_comments": [h for h in decl_hits if h["in_comment"]][:50], "truncated": res["truncated"]}

    def _base_type_name(self, type_str: str) -> str:
        """The bare type name behind ARRAY OF / POINTER TO / STRING(n) / library qualification."""
        t = type_str.strip()
        if m := _RE_ARRAY_OF.match(t):
            t = m.group(1).strip()
        if m := _RE_PTR.match(t):
            t = m.group(1).strip()
        t = t.split("(")[0].strip()
        if "." in t:
            t = t.split(".")[-1]
        return t

    # ------------------------------------------------------------ instances
    def variables(self, scope: str | None = None, max_array: int = 64, max_items: int = 200_000,
                  include_temp: bool = False) -> dict[str, Any]:
        """Every variable the project declares, flattened to the instance paths the runtime
        will publish (MAIN.fbFill.fbValve.bOpen, GVL_X.arr[3].nId …), by walking programs and
        GVLs through FB/STRUCT definitions. Arrays longer than max_array are shown as [*]."""
        self._ensure()
        roots: list[CodeUnit]
        if scope:
            roots = [self.get_unit(scope)]
        else:
            roots = [u for u in self.units.values() if u.kind in ("PROGRAM", "GVL") and not u.parent]
        items: list[dict[str, Any]] = []
        truncated = False

        def walk(path: str, u: CodeUnit, depth: int, seen: tuple[str, ...], inherited: dict[str, str] | None = None) -> None:
            nonlocal truncated
            for v in u.vars:
                if len(items) >= max_items:
                    truncated = True
                    return
                if v.section == "VAR_TEMP" and not include_temp:
                    continue
                if v.section == "VAR_EXTERNAL":
                    continue
                attrs = {**(inherited or {}), **v.attrs}   # an instance's pragma applies to its whole subtree
                self._emit(f"{path}.{v.name}", v.type, u, v, depth, seen, items, max_array, walk, attrs)
            for e in u.extends:
                eu = self.units.get(e.lower())
                if eu and eu.name.lower() not in seen:
                    walk(path, eu, depth, seen + (eu.name.lower(),), inherited)

        for r in roots:
            walk(r.name, r, 0, (r.name.lower(),), dict(r.attrs) if hasattr(r, "attrs") else None)
        kinds: dict[str, int] = {}
        for it in items:
            kinds[it["type"]] = kinds.get(it["type"], 0) + 1
        return {"scope": scope or "all programs + GVLs", "count": len(items), "truncated": truncated,
                "roots": [r.name for r in roots], "items": items}

    def _emit(self, path: str, type_str: str, u: CodeUnit, v: VarDecl, depth: int, seen: tuple[str, ...],
              items: list[dict[str, Any]], max_array: int, walk, attrs: dict[str, str] | None = None) -> None:
        if depth > 24:
            return
        extra = {"attrs": attrs} if attrs else {}
        t = type_str.strip()
        m = _RE_ARRAY_OF.match(t)
        if m:
            bounds = re.search(r"\[(.+?)\]", t)
            dims = []
            if bounds:
                for part in bounds.group(1).split(","):
                    if ".." in part:
                        lo, hi = part.split("..")
                        try:
                            dims.append((int(lo.strip()), int(hi.strip())))
                        except ValueError:
                            dims = []
                            break
            elem = m.group(1).strip()
            count = 1
            for lo, hi in dims:
                count *= (hi - lo + 1)
            if dims and count <= max_array:
                # row-major enumeration
                import itertools
                for coords in itertools.product(*[range(lo, hi + 1) for lo, hi in dims]):
                    self._emit(f"{path}[{','.join(map(str, coords))}]", elem, u, v, depth + 1, seen, items, max_array, walk, attrs)
            else:
                self._emit(f"{path}[*]", elem, u, v, depth + 1, seen, items, max_array, walk, attrs)
            return
        if _RE_PTR.match(t):
            items.append({"path": path, "type": t, "declared_in": u.name, "section": _section_text(v), "line": v.line, **extra})
            return
        tu = self._unit_for_type(t)
        if tu and tu.kind in ("FUNCTION_BLOCK", "STRUCT", "UNION", "PROGRAM") and tu.name.lower() not in seen:
            walk(path, tu, depth + 1, seen + (tu.name.lower(),), attrs or None)
            return
        items.append({"path": path, "type": t, "declared_in": u.name, "section": _section_text(v), "line": v.line,
                      **({"at": v.at} if v.at else {}), **({"constant": True} if v.constant else {}), **extra})

    # ------------------------------------------------------------ write back
    def set_code(self, name: str, declaration: str | None, implementation: str | None,
                 strip_lineids: bool = True, regenerate_lineids: bool = False) -> dict[str, Any]:
        u = self.get_unit(name, exact=True)
        if u.kind == "PROPERTY" and implementation is not None:
            raise ProjectError(f"{u.name} is a PROPERTY; edit {u.name}.Get / {u.name}.Set implementations instead")
        p = Path(u.file)
        raw = p.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw[3:].decode("utf-8", errors="replace") if bom else raw.decode("utf-8", errors="replace")
        eol = "\r\n" if "\r\n" in text else "\n"
        if declaration is not None:
            declaration = declaration.replace("\r\n", "\n").replace("\n", eol)
        if implementation is not None:
            implementation = implementation.replace("\r\n", "\n").replace("\n", eol)
        new = text
        changed = []
        # Locate the element block for this unit in the raw text (keep formatting of the rest).
        elem_start, elem_end = _locate_element(text, u)
        if elem_start < 0 or elem_end <= elem_start:
            raise ProjectError(f"could not locate the XML element of {u.name} in {u.file}; run proj_scan (file changed on disk?)")
        block = text[elem_start:elem_end]
        if declaration is not None:
            block, ok = _replace_cdata(block, "Declaration", declaration)
            changed.append("declaration" if ok else "declaration(NOT FOUND)")
        if implementation is not None:
            block, ok = _replace_cdata(block, "ST", implementation)
            changed.append("implementation" if ok else "implementation(NOT FOUND)")
        new = text[:elem_start] + block + text[elem_end:]
        if implementation is not None and regenerate_lineids:
            lines = max(1, len(implementation.split(eol)))
            inner = eol.join(f'      <LineId Id="{i + 1}" Count="0" />' for i in range(lines))
            block = f'<LineIds Name="{u.name}">{eol}{inner}{eol}    </LineIds>'
            new, n = re.subn(rf'<LineIds Name="{re.escape(u.name)}">.*?</LineIds>', lambda _m: block, new, flags=re.S)
            if n:
                changed.append("lineids_regenerated")
            else:
                close = f"  </{u.xml_path.split('/')[0]}>"
                if close in new:
                    new = new.replace(close, "    " + block + eol + close, 1)
                    changed.append("lineids_added")
        elif strip_lineids:
            # remove the LineIds entry for this object so XAE regenerates it
            new, n = re.subn(rf'\s*<LineIds Name="{re.escape(u.name)}">.*?</LineIds>', "", new, flags=re.S)
            if not n and u.parent:  # some versions name sub-item LineIds by the short name
                new, n = re.subn(rf'\s*<LineIds Name="{re.escape(u.name.split(".")[-1])}">.*?</LineIds>', "", new, flags=re.S)
            if n:
                changed.append(f"lineids_removed({n})")
        if new == text:
            return {"file": u.file, "changed": [], "note": "no change"}
        bak = p.with_suffix(p.suffix + ".bak")
        bak.write_bytes(raw)
        out = (b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8")
        p.write_bytes(out)
        # re-index this file
        try:
            self._index_file(p)
        except ET.ParseError as exc:
            p.write_bytes(raw)
            raise ProjectError(f"edit produced invalid XML, reverted: {exc}") from exc
        return {"file": u.file, "changed": changed, "backup": str(bak), "eol": "CRLF" if eol == "\r\n" else "LF", "bom": bom}

    # -- tasks (N62) ------------------------------------------------------------------------------------------
    def _plc_of(self, path: str | Path) -> str | None:
        """The PLC project (its .plcproj file) a file belongs to: the nearest folder above it holding a .plcproj."""
        dirs = {Path(pp["file"]).parent.resolve(): pp["file"] for pp in self.plcproj if pp.get("file")}
        try:
            here = Path(path).resolve()
        except OSError:
            return None
        for d in here.parents:
            if d in dirs:
                return dirs[d]
        return None

    def task_config(self) -> dict[str, dict[str, Any]]:
        """The PLC tasks of the project from its *.TcTTO files: {file: {"name", "cycle_ms": float | None,
        "programs": [...], "plc_project": .plcproj | None, "file"}}. Keyed by file: two PLC projects can each have a
        task of the same name (PlcTask), and keying by name dropped one of them. CycleTime in a .TcTTO is in
        microseconds; the programs are the task's PouCall entries."""
        self._ensure()
        out: dict[str, dict[str, Any]] = {}
        if self.root is None:
            return out
        for f in sorted(self.root.rglob("*.TcTTO")) + sorted(self.root.rglob("*.tctto")):
            if "_Boot" in f.parts or str(f) in out:
                continue
            try:
                task = ET.fromstring(f.read_text(encoding="utf-8-sig", errors="replace")).find("Task")
            except (ET.ParseError, OSError):
                continue
            if task is None:
                continue
            ct = (task.findtext("CycleTime") or "").strip()
            progs = [(c.findtext("Name") or "").strip() for c in task.findall("PouCall")]
            out[str(f)] = {"name": task.get("Name") or f.stem, "cycle_ms": int(ct) / 1000.0 if ct.isdigit() else None,
                           "programs": [p for p in progs if p], "plc_project": self._plc_of(f), "file": str(f)}
        return out

    def tasks_of(self, unit_name: str) -> list[dict[str, Any]] | None:
        """The tasks that run a PROGRAM: it is in a task's PouCall, or a program of that task calls it (transitively,
        from its body, actions and methods) — only tasks and programs of the program's own PLC project. Each task:
        {"name", "cycle_ms", "plc_project"}. [] when the name is no program (a GVL, an FB type: no owner of its own);
        None when the project cannot tell: no task configuration, or the name exists in several files (MAIN in two
        PLC projects — which one the recording reads is not known here)."""
        tasks = self.task_config()
        if not tasks:
            return None
        key = unit_name.lower()
        if key in self.duplicates:
            return None
        u = self.units.get(key)
        if u is None or u.kind != "PROGRAM":
            return []
        plc = self._plc_of(u.file)
        progs = {k: v for k, v in self.units.items() if v.kind == "PROGRAM" and self._plc_of(v.file) == plc}
        calls: dict[str, set[str]] = {}       # program → programs it calls
        for k in progs:
            body = " ".join(x.implementation for x in self.units.values()
                            if x.name.lower() == k or (x.parent or "").lower() == k)
            code = "\n".join(_strip_comments_line(ln) for ln in body.splitlines())
            calls[k] = {o for o in progs if o != k and re.search(r"\b" + re.escape(progs[o].name) + r"\s*[(.]", code, re.I)}
        owners = []
        unsure = False
        for t in tasks.values():
            if t["plc_project"] != plc:
                continue
            seen: set[str] = set()
            dup_on_way = False
            todo = [p.lower() for p in t["programs"]]
            while todo:
                p = todo.pop()
                if p in seen:
                    continue
                seen.add(p)
                if p in self.duplicates:
                    # a program of this name exists in several files and the index holds only one of them: what it
                    # calls in THIS project is not known — not followed
                    dup_on_way = True
                    continue
                todo.extend(calls.get(p, ()))
            if key in seen:
                owners.append({"name": t["name"], "cycle_ms": t["cycle_ms"], "plc_project": t["plc_project"]})
            elif dup_on_way:
                unsure = True
        # a task whose call chain could not be followed may run the program too: then the answer is not known (an
        # incomplete list, or [] — read as 'a GVL, no program' — would compare with the wrong cycle)
        return None if unsure else owners

    def read_file(self, rel_or_abs: str, max_chars: int = 200_000) -> dict[str, Any]:
        """Read a file inside the project root (or an allowed extra root). Anything outside is refused: this
        tool exists to read .TcPOU/.plcproj/.tsproj/HMI files, not C:\\Windows\\win.ini."""
        p = Path(rel_or_abs)
        if not p.is_absolute() and self.root:
            p = self.root / p
        self.assert_inside(p)
        if not p.is_file():
            raise ProjectError(f"file not found: {p}")
        txt = p.read_text(encoding="utf-8-sig", errors="replace")
        return {"file": str(p), "chars": len(txt), "text": txt[:max_chars], "truncated": len(txt) > max_chars}

    def allowed_roots(self) -> list[Path]:
        roots: list[Path] = []
        for r in (self.root, getattr(self.cfg, "hmi_root", None), getattr(self.cfg, "experiments_dir", None)):
            if r:
                try:
                    roots.append(Path(r).resolve())
                except OSError:
                    pass
        return roots

    def assert_inside(self, p: Path) -> Path:
        """Resolve ``p`` (following links) and require it under one of the allowed roots."""
        roots = self.allowed_roots()
        if not roots:
            raise ProjectError("no project root configured (xae.solution_path / project.root); file access is refused without one")
        try:
            rp = p.resolve()
        except OSError as exc:
            raise ProjectError(f"cannot resolve {p}: {exc}") from exc
        for r in roots:
            try:
                rp.relative_to(r)
                return rp
            except ValueError:
                continue
        raise ProjectError(f"{p} is outside the project ({', '.join(str(r) for r in roots)}); refused. "
                           f"Project tools only read and write under the solution folder (plus hmi_root / experiments_dir).")


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
def parse_vars(decl: str, base_line: int = 0) -> list[VarDecl]:
    out: list[VarDecl] = []
    if not decl:
        return out
    for m in _RE_VAR_BLOCK.finditer(decl):
        section = m.group(1).upper()
        quals = tuple(q.upper() for q in (m.group("quals") or "").split())
        body = m.group(3)
        body_start_line = decl[:m.start(3)].count("\n")
        prev_end = 0
        for d in _RE_VAR_DECL.finditer(body):
            line = base_line + body_start_line + body[:d.start()].count("\n")
            cmt = (d.group("cmt") or d.group("cmt2") or "").strip() or None
            attrs = {a.group(1): (a.group(2) if a.group(2) is not None else "")
                     for a in _RE_ATTR.finditer(body, prev_end, d.start())}
            prev_end = d.end()
            for nm in d.group("names").split(","):
                out.append(VarDecl(nm.strip(), d.group("type").strip(), section, line,
                                   (d.group("init") or "").strip() or None, d.group("at"), cmt, dict(attrs), quals))
    return out


def _cdata(el: ET.Element, tag: str) -> str:
    d = el.find(tag)
    return (d.text or "") if d is not None else ""


def _cdata_impl(el: ET.Element) -> str:
    impl = el.find("Implementation")
    if impl is None:
        return ""
    st = impl.find("ST")
    if st is not None:
        return st.text or ""
    # non-ST languages: return the raw XML so the agent sees something
    return "".join(ET.tostring(c, encoding="unicode") for c in impl)


def _kind_from_decl(decl: str, tag: str) -> str:
    m = _RE_HEADER.search(decl or "")
    if m:
        k = m.group("kind").upper()
        if k == "TYPE":
            low = decl.upper()
            if "STRUCT" in low:
                return "STRUCT"
            if "UNION" in low:
                return "UNION"
            if re.search(r":\s*\(", decl):
                return "ENUM"
            return "ALIAS"
        return k
    return {"POU": "POU", "DUT": "DUT", "GVL": "GVL", "Itf": "INTERFACE"}[tag]


def _code_lines(text: str, elem_off: int) -> tuple[int, int]:
    """1-based file lines where the Declaration and ST CDATA text of the element
    starting at ``elem_off`` begin. TwinCAT writes ``<Declaration><![CDATA[`` and
    ``<ST><![CDATA[`` with the first code line on the tag line itself."""
    if elem_off < 0:
        return 0, 0
    sub_end = min([i for i in (text.find("<Method", elem_off + 1), text.find("<Property", elem_off + 1),
                               text.find("<Action", elem_off + 1), text.find("<Transition", elem_off + 1),
                               text.find("<LineIds", elem_off + 1)) if i > 0] or [len(text)])
    d = text.find("<Declaration>", elem_off, sub_end)
    st = text.find("<ST>", elem_off, sub_end)
    return (text[:d].count("\n") + 1 if d >= 0 else 0, text[:st].count("\n") + 1 if st >= 0 else 0)


def _numbered(text: str, start: int) -> str:
    if not text:
        return ""
    lines = text.split("\n")
    w = len(str(start + len(lines)))
    return "\n".join(f"{start + i:>{w}}| {ln}" for i, ln in enumerate(lines))


def _locate_element(text: str, u: CodeUnit) -> tuple[int, int]:
    """Find the raw text span of the XML element this unit lives in."""
    if u.parent:
        tag = {"METHOD": "Method", "PROPERTY": "Property", "ACTION": "Action", "TRANSITION": "Transition",
               "GET": "Get", "SET": "Set"}.get(u.kind, "Method")
        if u.kind in ("GET", "SET"):
            # locate the parent property first then the accessor within it
            prop_name = u.name.split(".")[-2]
            ps = text.find(f'<Property Name="{prop_name}"')
            pe = text.find("</Property>", ps)
            s = text.find(f"<{tag}", ps, pe)
            e = text.find(f"</{tag}>", s) + len(f"</{tag}>")
            return s, e
        sub_name = u.name.split(".")[-1]
        s = text.find(f'<{tag} Name="{sub_name}"')
        if s < 0:
            return -1, -1
        e = text.find(f"</{tag}>", s) + len(f"</{tag}>")
        return s, e
    tag = u.xml_path
    s = text.find(f"<{tag} ")
    if s < 0:
        s = text.find(f"<{tag}>")
    if s < 0:
        return -1, -1
    # top-level: declaration/implementation come before any Method/Property elements
    first_sub = min([i for i in (text.find("<Method", s), text.find("<Property", s), text.find("<Action", s),
                                 text.find("<Transition", s), text.find("<LineIds", s)) if i > 0] or [len(text)])
    return s, first_sub


def _replace_cdata(block: str, tag: str, new_text: str) -> tuple[str, bool]:
    rx = re.compile(rf"(<{tag}>\s*<!\[CDATA\[)(.*?)(\]\]>\s*</{tag}>)", re.S)
    m = rx.search(block)
    if not m:
        return block, False
    safe = new_text.replace("]]>", "]]]]><![CDATA[>")
    return block[:m.start(2)] + safe + block[m.end(2):], True


# ----------------------------------------------------------------------------
# TcUnit xUnit parsing
# ----------------------------------------------------------------------------
def parse_tcunit_xml(xml_text: str) -> dict[str, Any]:
    """Results counted from the <testcase> elements themselves (R3: a <failure> in a suite without a 'failures'
    attribute came out as all_passed). Suite attributes are only compared, never trusted: a disagreement is
    reported and blocks all_passed."""
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as exc:
        raise ProjectError(f"TcUnit result is not valid XML: {exc}") from exc

    def num(v: Any) -> int | None:
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return None
    suites = []
    tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    mismatches: list[str] = []
    for ts in root.iter("testsuite"):
        s: dict[str, Any] = {"name": ts.get("name"), "cases": []}
        for tc in ts.findall("testcase"):
            c: dict[str, Any] = {"name": tc.get("name"), "class": tc.get("classname")}
            st = (tc.get("status") or "").strip().upper()
            f = tc.find("failure")
            e = tc.find("error")
            if f is not None or e is not None:
                el = f if f is not None else e
                c["status"] = "FAIL" if f is not None else "ERROR"
                c["message"] = el.get("message")
                c["type"] = el.get("type")
                if el.text:
                    c["detail"] = el.text.strip()[:500]
            elif tc.find("skipped") is not None or st in ("SKIPPED", "SKIP", "IGNORED"):
                c["status"] = "SKIPPED"
            elif st in ("FAIL", "FAILED", "FAILURE", "ERROR"):
                c["status"] = "FAIL"
                c["message"] = "status attribute says " + st
            else:
                c["status"] = "PASS"
            s["cases"].append(c)
        counted = {"tests": len(s["cases"]),
                   "failures": sum(c["status"] == "FAIL" for c in s["cases"]),
                   "errors": sum(c["status"] == "ERROR" for c in s["cases"]),
                   "skipped": sum(c["status"] == "SKIPPED" for c in s["cases"])}
        s.update(counted)
        attrs = {k: num(ts.get(k)) for k in counted}
        s["attributes"] = {k: v for k, v in attrs.items() if v is not None}
        for k, v in s["attributes"].items():
            if v != counted[k]:
                mismatches.append(f"{s['name']}: attribute {k}={v}, counted {counted[k]}")
        suites.append(s)
        for k in tot:
            tot[k] += counted[k]
    failed = [f"{s['name']}::{c['name']}: {c.get('message') or ''}" for s in suites for c in s["cases"]
              if c["status"] in ("FAIL", "ERROR")]
    out: dict[str, Any] = {"summary": tot, "passed": tot["tests"] - tot["failures"] - tot["errors"] - tot["skipped"],
                           "all_passed": tot["failures"] == 0 and tot["errors"] == 0 and tot["tests"] > 0 and not mismatches,
                           "failed_tests": failed, "suites": suites}
    if mismatches:
        out["attribute_mismatch"] = mismatches
        out["note"] = "the suite attributes disagree with the test cases in the file — counts are from the cases; not a pass"
    return out
