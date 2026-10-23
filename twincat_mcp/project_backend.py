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

import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import ProjectConfig

SRC_EXT = {".tcpou": "POU", ".tcdut": "DUT", ".tcgvl": "GVL", ".tcio": "Itf"}


def _norm(p: Path) -> str:
    try:
        return os.path.normcase(str(Path(p).resolve()))
    except OSError:
        return os.path.normcase(str(p))


_BOMS = ((b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
         (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"))
_XML_ENCODING = re.compile(r"""^<\?xml[^>]*?\bencoding\s*=\s*["']([A-Za-z0-9._:-]+)["']""")


def decode_source(raw: bytes) -> tuple[str, str, bytes, str | None]:
    """(text, encoding, BOM, note) of a TwinCAT XML source file, read as an XML parser reads it: the byte-order mark,
    else UTF-16 by its first character, else the encoding the XML declaration names, else UTF-8. N132: a UTF-16
    .TcPOU (valid XML) was a parse error — every file was read as UTF-8 with bad bytes replaced. Nothing is replaced
    silently: a file that is not valid in its encoding gets a note (one that claims, or defaults to, UTF-8 and is not,
    is read as Windows-1252 — what a German Windows wrote — and the note says so)."""
    bom, enc = b"", None
    for b, e in _BOMS:
        if raw.startswith(b):
            bom, enc = b, e
            break
    if enc is None and raw[:4] in (b"<\x00?\x00", b"\x00<\x00?"):
        enc = "utf-16-le" if raw[0] == 0x3C else "utf-16-be"
    body = raw[len(bom):]
    named = None
    if enc is None:
        m = _XML_ENCODING.match(body[:200].decode("ascii", "replace"))
        named = m.group(1) if m else None
        enc = named or "utf-8"
    try:
        import codecs
        enc = codecs.lookup(enc).name
    except LookupError:
        note = f"the XML declaration names an unknown encoding {named!r} — read as UTF-8"
        enc = "utf-8"
        try:
            return body.decode("utf-8"), enc, bom, note
        except UnicodeDecodeError:
            return body.decode("cp1252", "replace"), "cp1252", bom, note + " (not valid UTF-8: read as Windows-1252)"
    try:
        return body.decode(enc), enc, bom, None
    except UnicodeDecodeError as exc:
        where = f"byte 0x{body[exc.start]:02X} at offset {exc.start + len(bom)}"
        # review of 0.8.14: a UTF-8 file with one bad byte was read whole as Windows-1252 (its good umlauts became
        # 'Ã¶'). Windows-1252 only when nothing says UTF-8: no BOM, and fewer valid UTF-8 characters than bad bytes
        from .helpviewer import utf8_majority
        if enc == "utf-8" and not bom and not utf8_majority(body):
            return (body.decode("cp1252", "replace"), "cp1252", bom,
                    f"not valid UTF-8 ({where}) — read as Windows-1252 (its bytes are not UTF-8); check its umlauts")
        return body.decode(enc, "replace"), enc, bom, f"not valid {enc} ({where}) — such bytes read as U+FFFD"


def read_source(p: Path) -> str:
    """The text of a TwinCAT XML file (decode_source), for reading."""
    return decode_source(Path(p).read_bytes())[0]


def _compiled_files(plcproj: list[dict[str, Any]]) -> dict[str, list[str]] | None:
    """N106: the files the PLC projects compile (<Compile Include="POUs\\MAIN.TcPOU"/>, relative to the .plcproj,
    MSBuild-escaped), each with the .plcproj files that compile it (review of 0.8.14: a file linked into two PLC
    projects — ..\\Common\\GVL_Common.TcGVL — had only the last one, and vanished from the other's port) — None
    when there is no .plcproj (a folder of loose sources: every file is a unit). A .plcproj that cannot be read
    contributes every source file below its folder (what it compiles is not known)."""
    if not plcproj:
        return None
    from urllib.parse import unquote
    out: dict[str, list[str]] = {}

    def add(k: str, owner: str) -> None:
        if owner not in out.setdefault(k, []):
            out[k].append(owner)
    for pp in plcproj:
        base = Path(pp["file"]).parent
        if pp.get("error"):
            for f in base.rglob("*"):
                if f.suffix.lower() in SRC_EXT:
                    add(_norm(f), pp["file"])
            continue
        for inc in pp.get("compile") or []:
            add(_norm(base.joinpath(*[x for x in unquote(inc).replace("\\", "/").split("/") if x])), pp["file"])
    return out


def _plcproj_stamp(root: Path) -> dict[str, float]:
    """What decides which files are the project: the .plcproj files, and the .tsproj / .xti files that name the PLC
    projects (N114: a PLC project removed from the tree changes the .tsproj, not its .plcproj)."""
    out: dict[str, float] = {}
    for pp in root.rglob("*"):                        # one walk for the three kinds (it runs at every refresh)
        if pp.suffix.lower() in (".plcproj", ".tsproj", ".xti"):
            try:
                out[str(pp)] = pp.stat().st_mtime
            except OSError:
                pass
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def not_restored_links(sln: str | os.PathLike[str], limit: int = 200) -> dict[str, Any]:
    """0.8.20: the links the SAVED TwinCAT project keeps but XAE could not restore when it loaded them — TwinCAT marks
    them with a RestoreInfo attribute (DrumFillingMachineV0: RestoreInfo="ANotFound", 5 links on Term 6 whose PLC
    variables had moved); ProduceMappingInfo does not list them. Read from the .tsproj files the .sln names and the
    .xti files below them, whatever element carries the attribute: {files, count, entries: [{file, restore_info,
    element, owner_a, owner_b, var_a, var_b, name}]} — the saved state, not what XAE holds now."""
    sln_p = Path(sln)
    base = sln_p.parent
    tsprojs: list[Path] = []
    try:
        txt = sln_p.read_text(encoding="utf-8-sig", errors="replace")
        for rel in _RE_SLN_PROJECT.findall(txt):
            t = (base / rel.replace("\\", "/")).resolve()
            if t.is_file():
                tsprojs.append(t)
    except OSError as exc:
        return {"read": False, "why": f"the solution file could not be read ({exc})"}
    if not tsprojs:
        return {"read": False, "why": f"{sln_p.name} names no .tsproj that exists"}
    files: list[Path] = []
    for t in tsprojs:
        files.append(t)
        files.extend(sorted(x for x in t.parent.rglob("*.xti") if x.is_file() and "_Boot" not in x.parts))
    entries: list[dict[str, Any]] = []
    unread: list[str] = []
    for f in dict.fromkeys(files):
        try:
            root = ET.parse(f).getroot()
        except (OSError, ET.ParseError) as exc:
            unread.append(f"{f}: {exc}")
            continue
        parent = {c: p for p in root.iter() for c in p}
        for el in root.iter():
            ri = el.get("RestoreInfo")
            if ri is None:
                continue
            row: dict[str, Any] = {"file": str(f), "restore_info": ri, "element": _local(el.tag)}
            up = parent.get(el)
            while up is not None:
                tag = _local(up.tag)
                if tag in ("OwnerA", "OwnerB") and f"owner_{tag[-1].lower()}" not in row:
                    row[f"owner_{tag[-1].lower()}"] = up.get("Name") or ""
                up = parent.get(up)
            for k in ("VarA", "VarB", "Name"):
                if el.get(k) is not None:
                    row[{"VarA": "var_a", "VarB": "var_b", "Name": "name"}[k]] = el.get(k)
            entries.append(row)
    out: dict[str, Any] = {"read": True, "files": [str(f) for f in dict.fromkeys(files)], "count": len(entries),
                           "entries": entries[:limit]}
    if len(entries) > limit:
        out["truncated"] = f"{len(entries)} entries, the first {limit} listed"
    if unread:
        out["unread_files"] = unread
    return out


def _find_xti(base: Path, name: str) -> tuple[Path | None, str | None]:
    """The independent project file a .tsproj names (<Project File="Plc2.xti"/>): XAE keeps it in _Config\\PLC\\;
    anywhere else below the .tsproj's folder only when exactly one file has that name."""
    first = base / "_Config" / "PLC" / name
    if first.is_file():
        return first, None
    hits = [p for p in base.rglob(name) if p.is_file() and "_Boot" not in p.parts]
    if len(hits) == 1:
        return hits[0], None
    return None, (f"its file {name!r} was not found below {str(base)!r}" if not hits else
                  f"{len(hits)} files named {name!r} below {str(base)!r} — which one is meant is not known")


def _parse_tsproj(p: Path) -> dict[str, Any]:
    """N113/N114: the PLC projects a TwinCAT project consists of — each <Plc><Project Name= PrjFilePath= AmsPort=> of
    the .tsproj, inline or in an independent file (<Project File="Plc2.xti"/>). {"file", "error", "plc": [{"name" (the
    PLC project's name in the XAE tree: TIPC^<name>), "plcproj" (the file, None when not found), "ams_port",
    "path_in_file", "why"?}]}. PrjFilePath is relative to the file that holds it; the .tsproj's folder is tried too."""
    d: dict[str, Any] = {"file": str(p), "plc": [], "error": None}
    try:
        root = ET.fromstring(read_source(p))
    except (ET.ParseError, OSError) as exc:
        d["error"] = f"{type(exc).__name__}: {exc}"
        return d
    for plc in (e for e in root.iter() if _local(e.tag) == "Plc"):
        for el in plc:
            if _local(el.tag) != "Project":
                continue
            src, holder = el, p
            if el.get("File") and not el.get("PrjFilePath"):
                xti, why = _find_xti(p.parent, el.get("File") or "")
                if xti is None:
                    d["plc"].append({"name": el.get("Name"), "plcproj": None, "ams_port": None, "why": why,
                                     "unresolved": True})
                    continue
                try:
                    xr = ET.fromstring(read_source(xti))
                except (ET.ParseError, OSError) as exc:
                    d["plc"].append({"name": el.get("Name"), "plcproj": None, "ams_port": None,
                                     "why": f"{str(xti)!r} cannot be read: {exc}", "unresolved": True})
                    continue
                found = next((e for e in xr.iter() if _local(e.tag) == "Project" and e.get("PrjFilePath")), None)
                if found is None:
                    d["plc"].append({"name": el.get("Name"), "plcproj": None, "ams_port": None,
                                     "why": f"{str(xti)!r} names no PrjFilePath", "unresolved": True})
                    continue
                src, holder = found, xti
            port = (src.get("AmsPort") or "").strip()
            row: dict[str, Any] = {"name": src.get("Name") or el.get("Name"), "plcproj": None,
                                   "ams_port": int(port) if port.isdigit() else None}
            rel = src.get("PrjFilePath")
            if not rel:
                row["why"] = "no PrjFilePath (a PLC project without sources in this project)"
                d["plc"].append(row)
                continue
            row["path_in_file"] = rel
            parts = [x for x in rel.replace("\\", "/").split("/") if x]
            cands = [holder.parent.joinpath(*parts)] + ([p.parent.joinpath(*parts)] if holder != p else [])
            hit = next((c for c in cands if c.is_file()), None)
            if hit is None:
                row["why"] = f"{rel!r} (relative to {str(holder.parent)!r}) does not exist"
                row["unresolved"] = True
            else:
                row["plcproj"] = str(hit)
            d["plc"].append(row)
    return d


_RE_SLN_PROJECT = re.compile(r'^\s*Project\("[^"]*"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+\.tsproj)"', re.I | re.M)


def _solution_tsproj(solutions: list[Path], found: list[Path]) -> tuple[list[Path], list[Path]]:
    """Multi-PLC audit (0.8.13): every .tsproj under the root counted — a backup copy of the .tsproj kept a PLC project
    removed from the tree 'in the project'. The .tsproj files a .sln under the root names are the project's; the
    others are ignored (and said). No .sln, or none naming an existing .tsproj: every .tsproj counts, as before."""
    named: set[str] = set()
    for sln in solutions:
        try:
            text = sln.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for rel in _RE_SLN_PROJECT.findall(text):
            parts = [x for x in rel.replace("\\", "/").split("/") if x]
            named.add(_norm(sln.parent.joinpath(*parts)))
    keep = [p for p in found if _norm(p) in named]
    if not keep:
        return found, []
    return keep, [p for p in found if _norm(p) not in named]


def _in_project(ts: list[dict[str, Any]], plcproj: list[dict[str, Any]]) -> tuple[list[dict[str, Any]],
                                                                                   list[dict[str, Any]], str | None]:
    """N114: which .plcproj files are the TwinCAT project's PLC projects — those a .tsproj under the root names. A
    .plcproj no .tsproj names (the folder of a PLC project removed from the tree stays on disk) is not part of the
    project. When that cannot be told — no .tsproj, one that cannot be read, or none naming a .plcproj that exists —
    every .plcproj counts, and the reason is returned. Each kept .plcproj gets "tree_name" and "ams_port"."""
    if not ts:
        return plcproj, [], None
    bad = [t["file"] for t in ts if t.get("error")]
    named: dict[str, dict[str, Any]] = {}
    for t in ts:
        for r in t["plc"]:
            if r.get("plcproj"):
                named.setdefault(_norm(Path(r["plcproj"])), {**r, "tsproj": t["file"]})
    def known(pps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # multi-PLC audit (0.8.13): when every .plcproj counts because one reference could not be followed, the ones
        # the .tsproj DID name keep their tree name and ADS port — one missing .xti turned the port mapping off for all
        out = []
        for pp in pps:
            r = named.get(_norm(Path(pp["file"])))
            out.append({**pp, "tree_name": r.get("name"), "ams_port": r.get("ams_port"), "tsproj": r["tsproj"]}
                       if r is not None else pp)
        return out
    if bad:
        return known(plcproj), [], (f"{bad} cannot be read: which PLC projects belong to the project is not known — "
                                    "every .plcproj counts")
    if not named:
        return plcproj, [], ("no .tsproj names a .plcproj that exists: which PLC projects belong to the project is not "
                             "known — every .plcproj counts")
    lost = [r.get("name") for t in ts for r in t["plc"] if r.get("unresolved")]
    if lost:
        # review: Plc2's .xti not found while Plc1 resolved — Plc2.plcproj on disk may be the one it names; dropping
        # it would be a guess. Every .plcproj counts, and the note says which reference could not be followed
        return known(plcproj), [], (f"the .tsproj names PLC project(s) {lost} whose .plcproj could not be found "
                                    "(plc_projects_not_found) — which .plcproj files belong to the project is not "
                                    "known, every .plcproj counts (the ones it names keep their ADS port)")
    keep, left = [], []
    for pp in plcproj:
        r = named.get(_norm(Path(pp["file"])))
        if r is None:
            left.append({"file": pp["file"], "why": "no .tsproj under the root names this PLC project (removed from "
                                                    "the tree? its folder stays on disk) — not part of the project"})
        else:
            keep.append({**pp, "tree_name": r.get("name"), "ams_port": r.get("ams_port"), "tsproj": r["tsproj"]})
    if not keep and plcproj:
        return plcproj, [], (f"the .tsproj names {sorted(named)}, none of them under the project root — which .plcproj "
                             "files under the root belong to the project is not known, every .plcproj counts")
    return keep, left, None

# N92: the variables of a PROGRAM / GVL that become symbols of the runtime (VAR_TEMP never does; CONSTANT is replaced)
_SYMBOL_SECTIONS = {"PROGRAM": {"VAR", "VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_STAT", "VAR_INST"},
                    "GVL": {"VAR_GLOBAL"}}
_RE_ALIAS = re.compile(r"\bTYPE\s+(?P<name>[A-Za-z_]\w*)\s*:\s*(?P<base>.+?)\s*(?::=.*?)?;?\s*END_TYPE", re.I | re.S)
_RE_LINKALWAYS = re.compile(r"\{\s*attribute\s+'linkalways'\s*\}", re.I)
# ST comments and string literals in ONE pass, strings first: a // or (* inside a literal ('//srv/share/') is text,
# a quote inside a comment is comment. Separate passes cut code at the // of a literal (review of N92)
_RE_ST_TOKENS = re.compile(r"(?P<s>'(?:\$.|[^'$\n])*'|\"(?:\$.|[^\"$\n])*\")|\(\*.*?\*\)|//[^\n]*", re.S)


def st_strip(code: str, *, blank_strings: bool = False, comment: str = " ") -> str:
    """ST without its comments (each replaced by `comment`); string literals kept — or emptied to '' / "" with
    blank_strings (their text is then never read as code)."""
    def one(m: re.Match[str]) -> str:
        lit = m.group("s")
        if lit is None:
            return comment
        return lit[0] * 2 if blank_strings else lit
    return _RE_ST_TOKENS.sub(one, code or "")


def _no_comments(code: str) -> str:
    """ST without comments; string literals kept (an attribute pragma is quoted: {attribute 'linkalways'})."""
    return st_strip(code)


def not_on_target(declared: dict[str, dict[str, Any]], on_target: set[str],
                  pattern: str | None = None) -> dict[str, Any]:
    """N92: source roots (declared_roots) that hold symbols but have none on the target (on_target: the lower-case
    first name segments of the runtime's symbol table) -> {roots, plc_project, not_judged}. pattern (ads_dump's)
    limits it to the roots it selects. The cause is named, never guessed past what is known: linkalways in the source
    rules out 'not linked'. A source with several PLC projects is judged only for the one the target's symbols show
    to be running there (which project runs on which port is not read from the files); a name declared in two files
    is never called missing."""
    import fnmatch
    head = None
    if pattern:
        pat = pattern.lower() if ("*" in pattern or "?" in pattern) else pattern.lower() + "*"
        head = pat.split(".", 1)[0]
    res: dict[str, Any] = {"roots": [], "plc_project": None, "not_judged": None}
    projects = {d["plc_project"] for d in declared.values() if d["symbol_vars"]}
    if len(projects) > 1:
        live = {d["plc_project"] for k, d in declared.items() if k in on_target and not d.get("ambiguous")}
        if len(live) != 1:
            res["not_judged"] = (f"the source holds {len(projects)} PLC projects and the target's symbols match "
                                 f"{len(live)} of them — which one runs on this port is not known, so no root is "
                                 "called missing")
            return res
        res["plc_project"] = next(iter(live))
        declared = {k: d for k, d in declared.items() if d["plc_project"] == res["plc_project"]}
    out: list[dict[str, Any]] = res["roots"]
    for key, d in sorted(declared.items()):
        if not d["symbol_vars"] or d.get("ambiguous") or key in on_target \
                or (head is not None and not fnmatch.fnmatchcase(key, head)):
            continue
        if d["linkalways"]:
            cause = ("declared with {attribute 'linkalways'} but not on the target: the target runs another program "
                     "than this source (an older download, or another project)")
        elif d["kind"] == "GVL":
            cause = ("not linked: TwinCAT links only what the program uses — a GVL nothing references builds with 0 "
                     "errors and gets no symbol. Reference it, or add {attribute 'linkalways'} on the line above "
                     "VAR_GLOBAL ('Always link' in its properties, Advanced; Infosys tc3_plc_intro/2529686923); or the "
                     "target runs an older program than this source (download it)")
        else:
            cause = ("not linked: a PROGRAM that no task and no code calls gets no symbol — or the target runs an older "
                     "program than this source (download it)")
        out.append({"root": d["name"], "kind": d["kind"], "file": d["file"], "symbol_vars": d["symbol_vars"],
                    "cause": cause})
    return res


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
    r"(?:[ \t]*:[ \t]*(?P<ret>[\w\.\(\)\[\] ]+?)?)?(?:\s+EXTENDS\s+(?P<ext>[\w\., ]+?))?(?:\s+IMPLEMENTS\s+(?P<impl>[\w\., ]+?))?[ \t]*$",
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


@dataclass
class _Index:
    """What a scan or refresh builds before it is published (N88: copy-on-write — readers outside the lock iterate
    the published dicts, which are never changed in place; a scan swaps in complete new ones)."""
    by_file: dict[str, dict[str, CodeUnit]]   # file -> its units by lower-case name: every file's units are kept
    files: dict[str, float]
    notes: dict[str, str] = field(default_factory=dict)     # file -> how it was read, when not as it claims (N132)

    def copy(self) -> "_Index":
        return _Index(dict(self.by_file), dict(self.files), dict(self.notes))

    def merged(self) -> tuple[dict[str, CodeUnit], dict[str, list[str]]]:
        """The name -> unit view (a name declared in two files: the file indexed last) and the names declared in
        several files. N113: the per-file units stay complete — MAIN of Plc2 does not replace MAIN of Plc1 there."""
        units: dict[str, CodeUnit] = {}
        dup: dict[str, list[str]] = {}
        for f, us in self.by_file.items():
            for k, u in us.items():
                prev = units.get(k)
                if prev is not None and prev.file != f and u.kind != "PARSE_ERROR":
                    # every name declared in several files, a method of FB_X in two PLC projects too (review: only
                    # top-level names were flagged — get_unit('MAIN.Act') picked one of two silently, for writes too)
                    files = dup.setdefault(k, [prev.file])
                    if f not in files:
                        files.append(f)
                units[k] = u
        return units, dup


class ProjectIndex:
    def __init__(self, cfg: ProjectConfig):
        self.cfg = cfg
        self.root: Path | None = Path(cfg.root) if cfg.root else None
        self.units: dict[str, CodeUnit] = {}      # lower-case name -> unit
        self.duplicates: dict[str, list[str]] = {}  # lower-case name -> files when the same name exists twice
        self._by_file: dict[str, dict[str, CodeUnit]] = {}   # N113: file -> its units (names in two files both kept)
        self._plc_cache: tuple[Any, Any, dict[Any, tuple[dict[str, CodeUnit], set[str]]], dict[str, str | None],
                               dict[str | None, ProjectIndex]] | None = None
        self.files: dict[str, float] = {}         # path -> mtime
        self.read_notes: dict[str, str] = {}      # file -> how it was read when not as it claims (N132)
        self.solutions: list[str] = []
        self.tsproj: list[str] = []
        self.tsproj_ignored: list[str] = []     # .tsproj files no .sln under the root names (a backup copy)
        # N114: the PLC projects of the TwinCAT project — the .plcproj files a .tsproj names (with "tree_name",
        # "ams_port"); every .plcproj under the root when that cannot be told (plc_projects_note says why)
        self.plcproj: list[dict[str, Any]] = []
        self.plcproj_not_in_project: list[dict[str, Any]] = []   # .plcproj files no .tsproj names
        self.plc_refs: list[dict[str, Any]] = []                 # what the .tsproj files name, found or not
        self.plc_projects_note: str | None = None
        # N106: the project is what its .plcproj files compile — a .Tc* file under the root that no .plcproj lists (a
        # backup copy, an export, a test leftover) is not part of it: not a unit, listed here
        self.not_in_project: list[str] = []
        self._members: dict[str, list[str]] | None = None   # normcase path of each compiled file -> the .plcproj files
        #                                                        that compile it (one, or several: a linked file); None: no .plcproj
        self._plcproj_stamp: dict[str, float] = {}
        self.scanned_at: float | None = None
        # N26: tools run in worker threads; parallel first calls must not scan concurrently (each scan clears
        # and re-appends → duplicated plcproj/units). Re-entrant: refresh() may fall back to scan(). N88: scans and
        # refreshes build new containers and publish them at the end (one assignment each): a reader that holds
        # self.units sees a complete index, never one being cleared or re-filled.
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
        ix = _Index({}, {}, {})
        stamp = _plcproj_stamp(self.root)            # before reading: a change made during the scan rescans next time
        solutions = [str(p) for p in self.root.rglob("*.sln")]
        tsproj_files, ts_ignored = _solution_tsproj([Path(x) for x in solutions], list(self.root.rglob("*.tsproj")))
        ts = [_parse_tsproj(p) for p in tsproj_files]
        # sorted: the order of a directory walk is the file system's (NTFS by name, ext4 not) — the PLC projects' order
        # decides the first owner of a linked file and the order names are given in, the same on every machine
        plcproj, left_out, plc_note = _in_project(ts, [self._parse_plcproj(pp) for pp in sorted(self.root.rglob("*.plcproj"))])
        members = _compiled_files(plcproj)
        n_files = 0
        outside: list[str] = []
        for p in sorted(self.root.rglob("*")):        # sorted: which of two files of one name is indexed last
            if p.suffix.lower() in SRC_EXT and p.is_file():
                if "_Boot" in p.parts or "_CompileInfo" in p.parts or "_Libraries" in p.parts:
                    continue
                if members is not None and _norm(p) not in members:
                    outside.append(str(p))
                    continue
                try:
                    self._index_file(p, ix)
                    n_files += 1
                except ET.ParseError as exc:
                    self._parse_error(p, exc, ix)
        # published whole (N88): the units first, so a reader that sees the new plcproj never pairs it with old units
        self._publish(ix)
        self.solutions, self.tsproj, self.plcproj = solutions, [str(p) for p in tsproj_files], plcproj
        self.tsproj_ignored = [str(p) for p in ts_ignored]
        self.plcproj_not_in_project, self.plc_projects_note = left_out, plc_note
        self.plc_refs = [{**r, "tsproj": t["file"]} for t in ts for r in t["plc"]] + \
                        [{"tsproj": t["file"], "error": t["error"]} for t in ts if t.get("error")]
        self._members, self.not_in_project = members, sorted(outside)
        self._plcproj_stamp = stamp
        self.scanned_at = time.time()
        kinds: dict[str, int] = {}
        every = self.all_units()                     # N113: a name in two PLC projects counts twice
        for u in every:
            kinds[u.kind] = kinds.get(u.kind, 0) + 1
        out = {"root": str(self.root), "files": n_files, "units": len(every), "by_kind": kinds,
               "duplicate_names": {k: v for k, v in self.duplicates.items() if "." not in k},
               **({"duplicate_members": sum(1 for k in self.duplicates if "." in k)}
                  if any("." in k for k in self.duplicates) else {}),
               "solutions": self.solutions, "tsproj": self.tsproj,
               **({"tsproj_ignored": {"files": self.tsproj_ignored,
                                      "why": "no .sln under the root names them (a backup copy?) — the PLC projects "
                                             "they name are not taken from them"}} if self.tsproj_ignored else {}),
               "plcproj": [{"file": p["file"], "name": p["name"], "compile_items": len(p["compile"]),
                            **({"tree_name": p["tree_name"], "ams_port": p["ams_port"]} if "tree_name" in p else {}),
                            **({"error": p["error"]} if p.get("error") else {})} for p in self.plcproj],
               "duration_s": round(time.time() - t0, 2)}
        errs = self.parse_errors()
        if errs:
            out["parse_errors"] = errs
        if self.read_notes:
            out["read_notes"] = dict(sorted(self.read_notes.items()))
        missing = [r for r in self.plc_refs if not r.get("plcproj")]
        if missing:
            out["plc_projects_not_found"] = missing
        if self.plc_projects_note:
            out["plc_projects_note"] = self.plc_projects_note
        out.update(self.outside_note())
        return out

    def _publish(self, ix: _Index) -> None:
        units, dup = ix.merged()
        self.units, self.duplicates, self._by_file, self.files = units, dup, ix.by_file, ix.files
        self.read_notes = ix.notes

    def parse_errors(self) -> list[dict[str, str]]:
        """N132: the files that could not be read as XML, each with the parser's message — named, not only counted."""
        return sorted(({"file": u.file, "error": u.declaration} for us in self._by_file.values() for u in us.values()
                       if u.kind == "PARSE_ERROR"), key=lambda d: d["file"])

    @staticmethod
    def _parse_error(p: Path, exc: Exception, ix: _Index) -> None:
        ix.files[str(p)] = -1
        # keyed by the path: two broken files of one name in two folders are two entries, neither hides the other
        ix.by_file[str(p)] = {f"__parse_error__{p}".lower(): CodeUnit(p.name, "PARSE_ERROR", str(p), "", str(exc))}

    def outside_note(self) -> dict[str, Any]:
        """N106: the source files under the root that no .plcproj compiles — not part of the project, so not units.
        N114: and the .plcproj files no .tsproj names (their files are among them)."""
        out: dict[str, Any] = {}
        if self.not_in_project:
            out["not_in_project"] = {
                "count": len(self.not_in_project), "files": self.not_in_project[:20],
                "why": "no PLC project of the TwinCAT project compiles these files (a backup copy, an export, the folder "
                       "of a PLC project removed from the tree, a leftover?): they are not part of the project and not "
                       "indexed as units — proj_read_file reads them; add them to the project in XAE to have them "
                       "indexed"}
        if self.plcproj_not_in_project:
            out["plcproj_not_in_project"] = self.plcproj_not_in_project
        return out

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
        """Public: scan or refresh now (no throttle); returns the refresh delta, or None when no root is set. N88: the
        first-scan check is made under the lock — two parallel first calls scanned twice, the second clearing the units
        while the first caller was reading them ('no project configured')."""
        if self.root is None:
            return None
        with self._scan_lock:
            if self.scanned_at is None:
                r = self._scan(None)
                return {"scanned": True, "units": r["units"]}
            return self._refresh()

    def unit_names(self, *kinds: str) -> set[str]:
        """Names (lower case) of the units of these kinds, read under the scan lock: never a half-built index."""
        with self._scan_lock:
            return {u.name.lower() for u in self.units.values() if not kinds or u.kind in kinds}

    def declared_roots(self) -> dict[str, dict[str, Any]]:
        """N92: the PROGRAMs and GVLs of the source with what the target's symbol table should hold for them —
        {lower name: {name, kind, file, symbol_vars, linkalways, io_vars}}. symbol_vars counts the variables that
        become symbols (not CONSTANT, not VAR_TEMP, not {attribute 'hide'}); a root without any has no symbol to
        expect. linkalways: {attribute 'linkalways'} in its declaration."""
        self._ensure()
        return self._declared(self.units)            # a published snapshot (N88): complete, never changed in place

    def _declared(self, units: dict[str, CodeUnit]) -> dict[str, dict[str, Any]]:
        """plc_project: the .plcproj the unit's file belongs to; ambiguous: the name is declared in two files."""
        dup = self.duplicates
        out: dict[str, dict[str, Any]] = {}
        for u in units.values():
            if u.parent or u.kind not in _SYMBOL_SECTIONS:
                continue
            syms = [v for v in u.vars if v.section in _SYMBOL_SECTIONS[u.kind] and not v.constant
                    and "hide" not in {k.lower() for k in v.attrs}]
            out[u.name.lower()] = {"name": u.name, "kind": u.kind, "file": u.file, "symbol_vars": len(syms),
                                   "linkalways": bool(_RE_LINKALWAYS.search(_no_comments(u.declaration))),
                                   "io_vars": sum(1 for v in syms if v.at and re.match(r"%[IQ]", v.at, re.I)),
                                   "plc_project": self._plc_of(u.file), "ambiguous": u.name.lower() in dup}
        return out

    def unreferenced_gvls(self) -> dict[str, Any]:
        """N92, before a download: GVLs that hold symbols, carry no {attribute 'linkalways'} and that nothing else in
        the source names — not the GVL, not (unless qualified_only) one of its variables. TwinCAT does not link an
        object nothing uses (live: build 0 errors, no symbol on the target). A name found anywhere counts as a
        reference (a local variable of the same name too), so this only names GVLs nothing can be using; GVLs with
        AT %I*/%Q* variables are listed apart — whether an I/O mapping keeps one linked is not checked here.
        Visualisations and task configurations are searched as text. Only the GVL's own PLC project is searched — code
        of another PLC project cannot reference it; a name declared in two files is not judged."""
        self._ensure()
        units = self.units                           # one snapshot for the roots and the texts searched (N88)
        decl = self._declared(units)
        texts: list[tuple[str, str | None, str]] = [
            (u.name.lower(), self._plc_of(u.file), _no_comments(u.declaration) + "\n" + _no_comments(u.implementation))
            for u in self.all_units() if u.kind != "PARSE_ERROR"]      # every file's: MAIN of Plc2 references too
        other: dict[str | None, str] = {}
        if self.root is not None and self.root.is_dir():
            for p in self.root.rglob("*"):
                if p.suffix.lower() in (".tcvis", ".tcvmo", ".tctto") and p.is_file():
                    try:
                        plc = self._plc_of(p)
                        other[plc] = other.get(plc, "") + p.read_text(encoding="utf-8-sig", errors="replace") + "\n"
                    except OSError:
                        pass
        found: list[dict[str, Any]] = []
        io: list[dict[str, Any]] = []
        for key, d in sorted(decl.items()):
            if d["kind"] != "GVL" or not d["symbol_vars"] or d["linkalways"] or d["ambiguous"]:
                continue
            u = units[key]
            names = [re.escape(u.name)]
            if not re.search(r"\{\s*attribute\s+'qualified_only'\s*\}", u.declaration, re.I):
                names += [re.escape(v.name) for v in u.vars if v.section == "VAR_GLOBAL"]
            rx = re.compile(r"(?<![\w.])(?:" + "|".join(names) + r")\b", re.I)
            own, plc = key, d["plc_project"]
            if any(rx.search(t) for k, kp, t in texts if kp == plc and k != own and not k.startswith(own + ".")) or \
                    re.search(r"\b" + re.escape(u.name) + r"\.", other.get(plc, ""), re.I):
                continue
            row = {"gvl": u.name, "file": u.file, "symbol_vars": d["symbol_vars"]}
            (io if d["io_vars"] else found).append(row if not d["io_vars"] else {**row, "io_vars": d["io_vars"]})
        return {"not_linked": found, "with_io_not_judged": io}

    def refresh(self) -> dict[str, Any]:
        """Incremental rescan: re-index files whose mtime changed, pick up new files, drop deleted ones."""
        with self._scan_lock:
            return self._refresh()

    def _refresh(self) -> dict[str, Any]:
        if self.scanned_at is None or self.root is None:
            return self.scan()
        if _plcproj_stamp(self.root) != self._plcproj_stamp:
            # a .plcproj added, removed or saved (XAE added or removed an object): what the project is changed —
            # scanned again, the delta reported as for any refresh
            before = dict(self.files)
            r = self._scan(None)
            after = self.files
            return {"changed": sorted(f for f in after if f in before and after[f] != before[f]),
                    "added": sorted(f for f in after if f not in before),
                    "removed": sorted(f for f in before if f not in after), "units": r["units"],
                    "rescanned": "a .plcproj, .tsproj or .xti changed"}
        changed, added, removed = [], [], []
        seen: set[str] = set()
        ix = _Index(dict(self._by_file), dict(self.files), dict(self.read_notes))
        for p in self.root.rglob("*"):
            if p.suffix.lower() in SRC_EXT and p.is_file():
                if "_Boot" in p.parts or "_CompileInfo" in p.parts or "_Libraries" in p.parts:
                    continue
                if self._members is not None and _norm(p) not in self._members:
                    continue                          # N106: not compiled by any .plcproj (not_in_project)
                sp = str(p)
                seen.add(sp)
                mt = p.stat().st_mtime
                if sp not in ix.files:
                    added.append(sp)
                elif ix.files[sp] != mt:
                    changed.append(sp)
                else:
                    continue
                try:
                    self._index_file(p, ix)           # replaces the file's units (its place in the order kept)
                except ET.ParseError as exc:
                    self._parse_error(p, exc, ix)
        for sp in list(ix.files):
            if sp not in seen:
                removed.append(sp)
                ix.by_file.pop(sp, None)
                ix.files.pop(sp, None)
                ix.notes.pop(sp, None)
        if changed or added or removed:
            self._publish(ix)                         # published whole (N88)
        self.scanned_at = time.time()
        return {"changed": changed, "added": added, "removed": removed, "units": len(self.all_units())}

    def reindex_file(self, p: Path) -> None:
        """Re-index one file now (after an edit): its units replaced, the index published whole (N88). Raises
        ET.ParseError — and publishes nothing — when the file does not parse."""
        with self._scan_lock:
            ix = _Index(dict(self._by_file), dict(self.files), dict(self.read_notes))
            self._index_file(p, ix)
            self._publish(ix)

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
        for u in self.all_units():
            st = files.get(str(Path(u.file).resolve()))
            if st and not u.parent:
                units.append({"name": u.name, "kind": u.kind, "file": u.file, "status": {"M": "modified", "A": "added", "D": "deleted", "?": "untracked", "R": "renamed"}.get(st, st)})
        other = [f for f in files if f.lower().endswith((".tsproj", ".plcproj", ".xti", ".tmc", ".sln"))]
        return {"since": since, "repo": str(repo), "units": units, "project_files": other, "all_changed_files": len(files)}

    def is_member(self, path: str | Path) -> bool:
        """Is this file compiled by a .plcproj under the root (True as well when the root has no .plcproj)? Read
        fresh (no refresh throttle): it decides whether an edit is an edit of the PLC project (N109)."""
        self.ensure_fresh()
        return self._members is None or _norm(Path(path)) in self._members

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

    def _index_file(self, p: Path, ix: _Index) -> None:
        """Index one file into ix.by_file[file] (replacing what the file held). Raises ET.ParseError before anything
        of ix is changed."""
        text, _enc, _bom, note = decode_source(p.read_bytes())
        mtime = p.stat().st_mtime
        root = ET.fromstring(text)
        mine: dict[str, CodeUnit] = {}
        ix.files[str(p)] = mtime
        ix.by_file[str(p)] = mine
        if note:
            ix.notes[str(p)] = note
        else:
            ix.notes.pop(str(p), None)
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
        mine[name.lower()] = unit
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
                mine[full.lower()] = su
                if tag == "Property":
                    for acc in ("Get", "Set"):
                        a = sub.find(acc)
                        if a is not None:
                            prop_off = text.find(f'<{tag} Name="{sname}"')
                            adl, ail = _code_lines(text, text.find(f"<{acc}", prop_off))
                            au = CodeUnit(f"{full}.{acc}", acc.upper(), str(p),
                                          f"{kind_tag}/{tag}[@Name='{sname}']/{acc}",
                                          _cdata(a, "Declaration"), _cdata_impl(a), adl, ail, parent=full)
                            mine[au.name.lower()] = au

    def _fill_header(self, u: CodeUnit) -> None:
        m = _RE_HEADER.search(u.declaration or "")
        if not m:
            return
        if m.group("ext"):
            u.extends = [x.strip() for x in m.group("ext").split(",") if x.strip()]
        if m.group("impl"):
            u.implements = [x.strip() for x in m.group("impl").split(",") if x.strip()]
        # N137 (0.8.13 retest): 'TYPE E_X :' + '(' on the next line gave every ENUM return_type '(' and every STRUCT
        # 'STRUCT' — the ':' part crossed the line end, and a TYPE has no return type (its kind says what it is)
        if m.group("ret") and m.group("kind").upper() in ("FUNCTION", "METHOD", "PROPERTY"):
            u.return_type = m.group("ret").strip()

    # ------------------------------------------------------------ queries
    def list_units(self, kind: str | None = None, pattern: str | None = None, limit: int = 500) -> dict[str, Any]:
        self._ensure()
        import fnmatch
        pat = (pattern or "*").lower()
        if "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        sel = [u for u in self.all_units()          # N113: a name in two PLC projects is listed twice (with its file)
               if (not kind or u.kind.lower() == kind.lower()) and fnmatch.fnmatchcase(u.name.lower(), pat)]
        sel.sort(key=lambda u: (u.name.lower(), u.file))
        return {"total": len(sel), "units": [{**u.summary(), **self.plc_tag(u.file)} for u in sel[:limit]]}

    def plc_tag(self, f: str) -> dict[str, Any]:
        """S4 (0.8.14 retest): with several PLC projects, the one a listed file is part of ({"plc_project": .plcproj};
        a file several compile: {"plc_projects": [...]}) — proj_list / proj_search / proj_references gave the file
        only. {} with one PLC project."""
        if len(self.plcproj) <= 1:
            return {}
        owners = self.plcs_of_file(f)
        return {"plc_project": owners[0]} if len(owners) == 1 else {"plc_projects": owners}

    def get_unit(self, name: str, exact: bool = False) -> CodeUnit:
        """Look up by name; ``<PLC project>@Name`` pins a unit when the same name exists in two PLC projects (also a
        folder or file of the project: pin_filter). ``exact`` disables the suffix match (used for writes)."""
        self._ensure()
        pin = None
        if "@" in name:
            pin, name = name.split("@", 1)
        key = name.lower()
        if key in self.duplicates and pin is None:
            raise ProjectError(f"{name!r} exists in several files {self.duplicates[key]}; "
                               f"pin it as '<PLC project>@{name}'")
        if pin is not None:
            # N113: every file's units are kept, so the unit of the pinned file is there even when the name exists twice
            match, what = self.pin_filter(pin)
            cands = [x for f, us in self._by_file.items() if match(f) for k, x in us.items() if k == key]
            if len(cands) != 1:
                raise ProjectError(f"'{pin}@{name}' ({what}) matches {len(cands)} units"
                                   + (f"; the PLC projects: {sorted(self.plc_names())}" if not cands else ""))
            return cands[0]
        u = self.units.get(key)
        if u is None and not exact:
            # allow Method-name lookups ("Open" -> "FB_Valve.Open") when unique — among every file's units: FB_X.Open in
            # two PLC projects is two candidates, never one picked silently (review of N113)
            cands = [x for x in self.all_units() if x.name.lower().endswith("." + key)]
            if len(cands) == 1:
                return cands[0]
            if len(cands) > 1:
                raise ProjectError(f"{name!r} matches {len(cands)} objects: "
                                   f"{sorted({x.name + ' (' + x.file + ')' for x in cands})[:10]} — give the full name, "
                                   "or '<PLC project>@<name>'")
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
        subs = [x.summary() for x in self._by_file.get(u.file, {}).values() if x.parent == u.name]
        if subs:
            out["subitems"] = subs
        return out

    def resolve_symbol(self, symbol: str, port: int | None = None, *, plc: str | None = None) -> dict[str, Any]:
        """MAIN.fbFill.fbValve.bOpen → hop list with declaring unit/file/line/type. port: the ADS port the symbol is
        read from — when its first name is declared in several PLC projects, the one the .tsproj gives that port
        (N113); without that mapping such a name is refused, never resolved in one of them by chance. plc: resolve it
        inside that PLC project (its .plcproj) — for a check that must look at each of them."""
        self._ensure()
        parts = [p for p in re.split(r"\.(?![^\[]*\])", symbol.strip()) if p]
        if not parts:
            raise ProjectError("empty symbol")
        hops: list[dict[str, Any]] = []
        # first hop: program / GVL / FB name
        head = parts[0].lower()
        if plc is not None:
            units, dup = self.plc_units(plc)
            cur = None if head in dup else units.get(head)
        else:
            cur = self.named(head, port)
        # S4 (0.8.14 retest): the types are those of the PLC project looked in (a file two compile: not the first's)
        ix = self.walk_view(plc if plc is not None else self.port_plc(port))
        # review: a name of two OTHER PLC projects read on a mapped port is not this port's (N141), not 'cannot be told'
        if cur is None and head in self.duplicates and plc is None and self.port_plc(port) is None:
            raise ProjectError(f"{parts[0]!r} is declared in several PLC projects {self.duplicates[head]} and "
                               + (f"the .tsproj gives no single PLC project ADS port {port}" if port is not None else
                                  "no ADS port was given") + " — which one this symbol is cannot be told")
        if cur is None:
            # maybe GVL without prefix: search the GVLs for the var
            hits = self._find_var_anywhere(parts[0], kinds=("GVL",), port=port, plc=plc)
            if len({u.file for u, _v in hits}) > 1:
                raise ProjectError(f"{parts[0]!r} is declared in several GVLs {sorted({u.name + ' (' + u.file + ')' for u, _v in hits})} "
                                   "— qualify it with its GVL")
            if hits:
                unit, var = hits[0]
                hops.append({"segment": parts[0], "declared_in": unit.name, "file": unit.file, "line": var.line, "type": var.type, "section": var.section,
                             "constant": var.constant})
                cur = ix._unit_for_type(var.type, near=unit)
                parts = parts[1:]
            else:
                raise ProjectError(self._not_in_plc(parts[0], port, plc))
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
            var = ix._find_var(cur, base)
            if var is None:
                # method/property/action?
                sub = ix._by_file.get(cur.file, {}).get(f"{cur.name}.{base}".lower())
                if sub:
                    hops.append({"segment": seg, "declared_in": sub.name, "file": sub.file, "line": sub.decl_line0, "type": sub.kind})
                    cur = None
                    continue
                hops.append({"segment": seg, "error": f"{base!r} not declared in {cur.name} (or its EXTENDS chain)"})
                break
            unit_of_var, v = var
            hops.append({"segment": seg, "declared_in": unit_of_var.name, "file": unit_of_var.file, "line": v.line,
                         "type": v.type, "section": v.section, "comment": v.comment, "constant": v.constant})
            cur = ix._unit_for_type(v.type, near=unit_of_var)
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
                eu = self._view(cur).get(e.lower())          # the base FB of this PLC project (N113)
                if eu:
                    stack.append(eu)
        return None

    def plc_label(self, plc: str | None) -> str:
        """A PLC project as the XAE tree names it (the .tsproj), else its .plcproj name; '(files of no PLC project)'."""
        if plc is None:
            return "(files of no PLC project)"
        pp = next((x for x in self.plcproj if x.get("file") == plc), None)
        return str((pp or {}).get("tree_name") or Path(plc).stem)

    def _not_in_plc(self, name: str, port: int | None, plc: str | None) -> str:
        """Why a symbol's first name was not found: N141 (0.8.14 retest) — PRG_B of Plc2, read on port 851, was 'no
        program/GVL/FB named PRG_B'. Looked up in one PLC project (the port's, or plc=) and declared in another: says
        which one was looked in and where the name is."""
        lim = plc if plc is not None else self.port_plc(port)
        if lim is None:
            return f"no program/GVL/FB named {name!r}; try proj_search"
        key = name.strip().lower()
        # N141b (0.8.15 retest): looked up through one PLC project's view (wf_debug_symbol: App.proj_for_port), the
        # other PLC projects are not in the view — where the name is declared is looked up in the whole index
        whole = getattr(self, "_parent", self)
        where = sorted({whole.plc_label(o) for u in whole.all_units() if u.name.lower() == key
                        for o in whole.plcs_of_file(u.file) if o != lim}
                       | {whole.plc_label(o) for u in whole.all_units() if u.kind.upper() == "GVL"
                          for v in u.vars if v.name.lower() == key for o in whole.plcs_of_file(u.file) if o != lim})
        here = f"PLC project {self.plc_label(lim)}" + (f" (ADS port {port})" if plc is None and port is not None else "")
        if not where:
            return f"no program/GVL/FB named {name!r} in {here}; try proj_search"
        return (f"{name!r} is not declared in {here} — it is in PLC project(s) {where}"
                + (": read from this port it is not that symbol" if plc is None else "")
                + f"; the PLC projects: {sorted(self.plc_names())}")

    def port_plc(self, port: int | None) -> str | None:
        """The PLC project (its .plcproj) the runtime on ADS `port` runs — when there are several and the .tsproj maps
        the port; None with one PLC project (every unit is its) or when it cannot be told."""
        return self.plc_project(port=port) if port is not None and len(self.plcproj) > 1 else None

    def _scope_units(self, port: int | None = None, plc: str | None = None) -> Iterable[CodeUnit]:
        """The units the runtime on `port` can have: that PLC project's when there are several and the .tsproj maps
        the port — also when no name is declared twice (review of 0.8.14: Plc2's PRG_B / GVL_B were listed and
        resolved on port 851); else every file's. plc: that PLC project's."""
        plc = plc if plc is not None else self.port_plc(port)
        if plc is not None:
            return self.plc_units(plc)[0].values()
        return self.units.values() if not self.duplicates else self.all_units()

    def _find_var_anywhere(self, name: str, kinds: Iterable[str], port: int | None = None,
                           plc: str | None = None) -> list[tuple[CodeUnit, VarDecl]]:
        """Every unit of these kinds that declares `name` (several: the caller refuses to pick)."""
        ks = {k.upper() for k in kinds}
        return [(u, v) for u in self._scope_units(port, plc) if u.kind.upper() in ks
                for v in u.vars if v.name.lower() == name.lower()]

    def _unit_for_type(self, type_str: str, near: "CodeUnit | str | None" = None) -> CodeUnit | None:
        """The unit a type name means where it is used (`near`: the unit or file using it — its PLC project's types).
        A name declared in several PLC projects, asked without `near`: None (not known which)."""
        t = type_str.strip()
        if m := _RE_ARRAY_OF.match(t):
            t = m.group(1).strip()
        if m := _RE_PTR.match(t):
            t = m.group(1).strip()
        t = t.split("(")[0].strip()
        if "." in t:  # library-qualified
            t = t.split(".")[-1]
        if near is None and t.lower() in self.duplicates:
            return None
        return self._view(near).get(t.lower())

    def search(self, regex: str, kinds: list[str] | None = None, max_hits: int = 200,
               ignore_case: bool = True) -> dict[str, Any]:
        self._ensure()
        try:
            rx = re.compile(regex, re.I if ignore_case else 0)
        except re.error as exc:
            raise ProjectError(f"bad regex: {exc}") from exc
        hits: list[dict[str, Any]] = []
        ks = {k.upper() for k in kinds} if kinds else None
        for u in self.all_units():                    # N113: both MAINs of two PLC projects are searched
            if ks and u.kind.upper() not in ks:
                continue
            for part, text, base in (("declaration", u.declaration, u.decl_line0), ("implementation", u.implementation, u.impl_line0)):
                for i, ln in enumerate(text.splitlines(), start=1):
                    if rx.search(ln):
                        hits.append({"unit": u.name, "part": part, "file": u.file, "line": base + i - 1,
                                     "text": ln.strip()[:200], **self.plc_tag(u.file)})
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
        for u in self.all_units():
            for v in u.vars:
                base = self._base_type_name(v.type)
                tu = self._unit_for_type(v.type, near=u)
                if (tu is not None and tu.name.lower() == name.lower()) or (tu is None and base.lower() == name.lower()):
                    instances.append({"unit": u.name, "var": v.name, "type": v.type, "file": u.file, "line": v.line,
                                      "declared_in_project": tu is not None,    # False = library type (Tc2_Standard.CTU …)
                                      **self.plc_tag(u.file)})
        return {"name": name, "instances": instances,
                "mentions": [h for h in decl_hits if not h["in_comment"]],
                "mentions_in_comments": [h for h in decl_hits if h["in_comment"]][:50], "truncated": res["truncated"]}

    def type_alias(self, name: str, port: int | None = None) -> str | None:
        """N50: the base an ALIAS DUT of the source declares — 'PT_X' -> 'POINTER TO FB_X' for TYPE PT_X : POINTER TO
        FB_X; END_TYPE (qualified or not; comments and pragmas are not part of it), else None. A name declared in
        several PLC projects: the one of ADS `port`'s PLC project, else None (not known which)."""
        self._ensure()
        u = self.named((name or "").strip().split(".")[-1], port)
        if u is None or u.kind not in ("ALIAS", "DUT"):     # 'TYPE X : POINTER TO Y;' is not a header line: DUT
            return None
        m = _RE_ALIAS.search(st_strip(u.declaration, comment=" "))
        if m is None or m.group("name").lower() != u.name.lower():
            return None
        base = " ".join(m.group("base").split())
        if re.match(r"(STRUCT|UNION)\b|\(", base, re.I):
            return None                                     # a structure or an enumeration, not an alias
        return base

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
                  include_temp: bool = False, port: int | None = None) -> dict[str, Any]:
        """Every variable the project declares, flattened to the instance paths the runtime
        will publish (MAIN.fbFill.fbValve.bOpen, GVL_X.arr[3].nId …), by walking programs and
        GVLs through FB/STRUCT definitions. Arrays longer than max_array are shown as [*]. port: the ADS port the
        paths are compared with — with names in several PLC projects, that port's PLC project (N113); without that
        mapping every PLC project's roots are walked and the result says so."""
        self._ensure()
        roots: list[CodeUnit]
        note = None
        twice: list[str] = []
        on_port = self.port_plc(port)
        walked: list[str | None] = [on_port] if on_port is not None else []     # the PLC project(s) walked
        if scope and "@" in scope:
            roots = [self.get_unit(scope)]             # a pin names the PLC project itself (review of 0.8.14)
            pinned = self.pin_plcs(scope.split("@", 1)[0])
            walked = [x for x in self.plcs_of_file(roots[0].file) if not pinned or x in pinned]
        elif scope:
            if on_port is not None:
                u0 = self.named(scope, port)
                if u0 is None:
                    raise ProjectError(f"{scope!r} is not declared in PLC project {self.plc_label(on_port)} (ADS port "
                                       f"{port}){' — or twice in it' if scope.strip().lower() in self.plc_units(on_port)[1] else ''}"
                                       f"; the PLC projects: {sorted(self.plc_names())}")
            else:
                u0 = self.named(scope, port) if scope.strip().lower() in self.duplicates else None
            roots = [u0 if u0 is not None else self.get_unit(scope)]
            if on_port is None:
                walked = self.plcs_of_file(roots[0].file)
        else:
            roots = [u for u in self._scope_units(port) if u.kind in ("PROGRAM", "GVL") and not u.parent]
            if len(self.plcproj) > 1 and on_port is None:
                twice = sorted({r.name for r in roots if r.name.lower() in self.duplicates})
                note = (f"{len(self.plcproj)} PLC projects and "
                        + ("no ADS port was given" if port is None else f"the .tsproj gives no PLC project port {port}")
                        + ": the programs and GVLs of every PLC project are listed"
                        + (f" — {twice} are declared in several PLC projects (same path, different files)" if twice else ""))
        items: list[dict[str, Any]] = []
        truncated = False
        # S4 (0.8.14 retest): each root is walked in a PLC project — with the types of that PLC project: a file compiled
        # by two PLC projects (GVL_Common) read on port 852 resolved its types in the first one's. The port's or the
        # pin's PLC project; else each PLC project the root's file is part of, its items tagged with it (review: a
        # shared file was labelled with both and walked with the first one's types)
        multi = len(self.plcproj) > 1 and not isinstance(self, PlcView)
        if not multi:
            plan: list[tuple[CodeUnit, list[str | None]]] = [(r, [None]) for r in roots]
        elif len(walked) == 1:
            plan = [(r, [walked[0]]) for r in roots]
        else:
            plan = [(r, self.plcs_of_file(r.file)) for r in roots]
        ix: list[ProjectIndex] = [self]
        sink: list[list[dict[str, Any]]] = [items]
        differ: list[str] = []

        def walk(path: str, u: CodeUnit, depth: int, seen: tuple[str, ...], inherited: dict[str, str] | None = None) -> None:
            nonlocal truncated
            for v in u.vars:
                if len(items) + (len(sink[0]) if sink[0] is not items else 0) >= max_items:
                    truncated = True
                    return
                if v.section == "VAR_TEMP" and not include_temp:
                    continue
                if v.section == "VAR_EXTERNAL":
                    continue
                attrs = {**(inherited or {}), **v.attrs}   # an instance's pragma applies to its whole subtree
                ix[0]._emit(f"{path}.{v.name}", v.type, u, v, depth, seen, sink[0], max_array, walk, attrs)
            for e in u.extends:
                eu = ix[0]._view(u).get(e.lower())
                if eu and eu.name.lower() not in seen:
                    walk(path, eu, depth, seen + (eu.name.lower(),), inherited)

        tag = multi and len(walked) != 1
        for r, plcs in plan:
            got: list[list[dict[str, Any]]] = []
            for plc in plcs:
                ix[0] = self.walk_view(plc) if multi else self
                sink[0] = [] if tag else items
                walk(r.name, r, 0, (r.name.lower(),), dict(r.attrs) if hasattr(r, "attrs") else None)
                got.append(sink[0])
            sink[0] = items
            if not tag:
                continue
            if len(got) > 1 and all([(i["path"], i["type"]) for i in g] == [(i["path"], i["type"]) for i in got[0]]
                                    for g in got[1:]):
                # one file several PLC projects compile, the same in each: listed once (review: rec_scan counted
                # it twice), named with all of them
                items.extend({**i, "plc_projects": list(plcs)} for i in got[0])
                continue
            if len(got) > 1:
                differ.append(r.name)          # the same file, other types in each: per PLC project, not judged as one
            for plc, g in zip(plcs, got):
                items.extend({**i, "plc_project": plc} for i in g)
        kinds: dict[str, int] = {}
        for it in items:
            kinds[it["type"]] = kinds.get(it["type"], 0) + 1
        out = {"scope": scope or "all programs + GVLs", "count": len(items), "truncated": truncated,
               "roots": [r.name for r in roots], "items": items}
        if multi:
            # S4 (0.8.14 retest): which PLC project was walked — 'GVL_Common' and 'Plc2@GVL_Common' looked identical
            if len(walked) == 1:
                out["plc_project"] = walked[0]
            else:
                out["plc_projects"] = (list(dict.fromkeys(plc for _r, plcs in plan for plc in plcs))
                                       or [pp["file"] for pp in self.plcproj])
                out["note_plc_projects"] = ("each item names the PLC project it was walked in (plc_project), or all of "
                                            "them (plc_projects: one file they compile, the same in each)")
        if differ:
            # review: a file two PLC projects compile, with other types in each — its paths depend on the PLC project
            # the runtime is; not judged against one runtime (proj_vs_runtime), like a name declared twice
            note = ((note + "; ") if note else "") + (
                f"{differ} are files several PLC projects compile, with other types in each: listed per PLC project "
                "(plc_project) — name the ADS port (AmsPort in the .tsproj) or pin '<PLC project>@<name>'")
            twice = sorted(set(twice) | set(differ))
        if note:
            out["note"] = note
            out["ambiguous_roots"] = twice
        return out

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
        tu = self._unit_for_type(t, near=u)
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
        text, enc, bom_bytes, note = decode_source(raw)
        if note:
            # N132: written back, the replaced / re-read bytes would change text the edit never touched
            raise ProjectError(f"{u.file} is {note.split(' — ')[0]}: not edited (it would be written back changed) — "
                               "fix the file's encoding in XAE first")
        bom = bool(bom_bytes)
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
        try:
            out = bom_bytes + new.encode(enc)                 # the file's own encoding (UTF-16 stays UTF-16)
        except UnicodeEncodeError as exc:
            raise ProjectError(f"{new[exc.start:exc.end]!r} cannot be written in {u.file}'s encoding ({enc}): "
                               "nothing was changed") from exc
        p.write_bytes(out)
        # re-index this file
        try:
            self.reindex_file(p)
        except ET.ParseError as exc:
            p.write_bytes(raw)
            raise ProjectError(f"edit produced invalid XML, reverted: {exc}") from exc
        return {"file": u.file, "changed": changed, "backup": str(bak), "eol": "CRLF" if eol == "\r\n" else "LF", "bom": bom}

    # -- tasks (N62) ------------------------------------------------------------------------------------------
    def _plc_of(self, path: str | Path) -> str | None:
        """The PLC project (its .plcproj file) a file belongs to: the .plcproj that compiles it; for a file no
        .plcproj lists (a .TcTTO, a visualisation), the nearest folder above it holding one of the project's .plcproj."""
        owners = (self._members or {}).get(_norm(Path(path)))
        if owners:
            return owners[0]
        return self._nearest(path, self.plcproj)

    def plcs_of_file(self, f: str) -> list[str | None]:
        """Every PLC project (its .plcproj) a file is part of — several for a file linked into several (review of
        0.8.14); else plc_of_file's one."""
        owners = (self._members or {}).get(_norm(Path(f)))
        return list(owners) if owners and len(owners) > 1 else [self.plc_of_file(f)]

    @staticmethod
    def _nearest(path: str | Path, plcproj: list[dict[str, Any]]) -> str | None:
        dirs = {Path(pp["file"]).parent.resolve(): pp["file"] for pp in plcproj if pp.get("file")}
        try:
            here = Path(path).resolve()
        except OSError:
            return None
        for d in here.parents:
            if d in dirs:
                return dirs[d]
        return None

    def plc_names(self) -> list[str]:
        """The PLC projects by the name the XAE tree gives them (the .tsproj), else their .plcproj name."""
        return [str(pp.get("tree_name") or Path(pp["file"]).stem) for pp in self.plcproj]

    def pin_plcs(self, pin: str) -> set[str]:
        """The PLC projects (their .plcproj files) a '<pin>@Name' pin names — by tree name or .plcproj name; empty
        when the pin is a folder or file of the project."""
        p = pin.strip().lower()
        return {pp["file"] for pp in self.plcproj
                if p in (str(pp.get("tree_name") or "").lower(), Path(pp["file"]).stem.lower())}

    def pin_filter(self, pin: str) -> tuple[Callable[[str], bool], str]:
        """'<pin>@Name': which files a pin selects, and what it was read as. A PLC project by its tree name (TIPC^<name>,
        from the .tsproj) or .plcproj name first; else a folder or file of the project — a whole path segment below the
        root, or the file's name. It was a substring of the absolute path (0.8.13 retest: 'Documents@MAIN' found MAIN
        through C:\\Users\\…\\Documents, and 'Plc2@MAIN' worked only because the folder was named Plc2)."""
        p = pin.strip().lower()
        plcs = self.pin_plcs(pin)
        if plcs:
            return (lambda f: bool(plcs.intersection(self.plcs_of_file(f)))), f"PLC project {pin!r}"
        root = self.root

        def seg(f: str) -> bool:
            fp = Path(f)
            try:
                rel = fp.relative_to(root) if root is not None else fp
            except ValueError:
                rel = Path(fp.name)
            return p in [x.lower() for x in rel.parts[:-1]] or p in (fp.name.lower(), fp.stem.lower())
        return seg, f"folder or file {pin!r} of the project"

    def plc_project(self, *, name: str | None = None, port: int | None = None) -> str | None:
        """N113/N114: the .plcproj of the PLC project the .tsproj names `name` (TIPC^<name> in the XAE tree) or gives
        ADS port `port` — None when no .tsproj says so, or more than one project matches."""
        hits = [pp["file"] for pp in self.plcproj
                if (name is not None and str(pp.get("tree_name") or "").lower() == name.lower())
                or (port is not None and pp.get("ams_port") == port)]
        return hits[0] if len(hits) == 1 else None

    def plc_units(self, plc: str | None) -> tuple[dict[str, CodeUnit], set[str]]:
        """N113: the units of ONE PLC project (its .plcproj file; None: the files of no PLC project), complete even when
        another PLC project declares the same names — and the names declared twice within it. Cached per published
        index (N88: a scan or refresh publishes a new _by_file, which drops the cache)."""
        by_file = self._by_file                       # a published snapshot (N88)
        cache = self._cache()
        hit = cache[2].get(plc)
        if hit is not None:
            return hit
        units: dict[str, CodeUnit] = {}
        dup: set[str] = set()
        for f, us in by_file.items():
            if plc not in self.plcs_of_file(f):
                continue
            for k, u in us.items():
                prev = units.get(k)
                if prev is not None and prev.file != f and u.kind != "PARSE_ERROR":
                    dup.add(k)
                units[k] = u
        cache[2][plc] = (units, dup)
        return units, dup

    def _cache(self) -> tuple[Any, Any, dict[Any, tuple[dict[str, CodeUnit], set[str]]], dict[str, str | None],
                              dict[str | None, "ProjectIndex"]]:
        """Memo of this published index (review: _plc_of resolves the path on every call — a variables() walk with
        names in two PLC projects made tens of thousands of resolve() calls). Keyed on the published _by_file and
        _members objects, so a scan's new index or new compile lists drop it."""
        by_file, members = self._by_file, self._members
        c = self._plc_cache
        if c is None or c[0] is not by_file or c[1] is not members:
            c = self._plc_cache = (by_file, members, {}, {}, {})
        return c

    def walk_view(self, plc: str | None) -> "ProjectIndex":
        """The index a walk inside PLC project `plc` resolves its types in (S4, 0.8.14 retest: a file two PLC projects
        compile took the first one's types on the other's port): that PLC project's view with several PLC projects,
        else the index itself. Memoised per published index."""
        if plc is None or len(self.plcproj) <= 1 or isinstance(self, PlcView):
            return self
        views = self._cache()[4]
        if plc not in views:
            views[plc] = PlcView(self, plc)
        return views[plc]

    def plc_of_file(self, f: str) -> str | None:
        """_plc_of for an indexed file, memoised per published index."""
        memo = self._cache()[3]
        if f not in memo:
            memo[f] = self._plc_of(f)
        return memo[f]

    def all_units(self) -> list[CodeUnit]:
        """Every unit of every file — a name declared in two PLC projects is listed twice (the name -> unit view holds
        one of them). For lists and searches: nothing is left out."""
        return [u for us in self._by_file.values() for u in us.values()]

    def _view(self, near: "CodeUnit | str | None") -> dict[str, CodeUnit]:
        """The name -> unit map a name used in `near` (a unit, or its file) resolves in: its own PLC project's units
        (code of one PLC project cannot use another's types). The project-wide view when no name is declared twice."""
        if not self.duplicates or near is None:
            return self.units
        f = near.file if isinstance(near, CodeUnit) else str(near)
        return self.plc_units(self.plc_of_file(f))[0]

    def named(self, name: str, port: int | None = None) -> CodeUnit | None:
        """The unit of this name as the runtime on ADS `port` has it: the PLC project the .tsproj gives that port, when
        the name is declared in several PLC projects; None when it is unknown or which one cannot be told."""
        key = (name or "").strip().lower()
        # multi-PLC audit (0.8.13): a name declared once was taken from any PLC project — PRG_B.x read on port 851
        # resolved into Plc2's source. When the .tsproj maps the port, the name is that PLC project's or none
        plc = self.plc_project(port=port) if port is not None and len(self.plcproj) > 1 else None
        if plc is not None:
            units, dup = self.plc_units(plc)
            return None if key in dup else units.get(key)
        if key not in self.duplicates:
            return self.units.get(key)
        return None

    def unit_at_tree_path(self, path: str) -> tuple[CodeUnit | None, str | None]:
        """N113: the source unit an XAE tree path names (TIPC^Plc2^Plc2 Project^POUs^MAIN, …^FB_X^M_Start) — looked up
        in the PLC project the path names (TIPC^<name>, from the .tsproj), so MAIN of Plc2 is never taken for MAIN of
        Plc1. (unit, None), or (None, why)."""
        segs = [x for x in path.split("^") if x]
        if not segs:
            return None, "empty tree path"
        # the unit's key is the object and its owners joined with '.': FB_X, FB_X.M_Start, MAIN.Act, and for a property
        # accessor FB_X.Value.Get (N123: only 'value.get' / 'get' were tried — every accessor edit was refused)
        keys = [".".join(segs[-n:]).lower() for n in (3, 2, 1) if len(segs) >= n]
        plc = self.plc_project(name=segs[1]) if len(segs) >= 3 and segs[0].upper() == "TIPC" else None
        if plc is not None:
            units, dup = self.plc_units(plc)
            where = f"PLC project {segs[1]!r} ({plc})"
        else:
            units, dup = self.units, set(self.duplicates)
            where = "the project"
        k = next((k for k in keys if k in units), None)
        if k is None:
            return None, f"not in any source file of {where}"
        if k in dup:
            if plc is not None:
                files = sorted({u.file for f, us in self._by_file.items() if self._plc_of(f) == plc
                                for kk, u in us.items() if kk == k})
                return None, f"{units[k].name!r} is declared in several files of {where}: {files}"
            named = (f"its PLC project {segs[1]!r} is not one a .tsproj under the root names"
                     if len(segs) >= 3 and segs[0].upper() == "TIPC" else "the path names no PLC project (TIPC^<name>^…)")
            return None, (f"{units[k].name!r} is declared in several files {self.duplicates.get(k)} and {named} — "
                          "which file holds this object is not known")
        return units[k], None

    def task_config(self) -> dict[str, dict[str, Any]]:
        """The PLC tasks of the project from its *.TcTTO files: {file: {"name", "cycle_ms": float | None,
        "programs": [...], "plc_project": .plcproj | None, "file"}}. Keyed by file: two PLC projects can each have a
        task of the same name (PlcTask), and keying by name dropped one of them. CycleTime in a .TcTTO is in
        microseconds; the programs are the task's PouCall entries."""
        self._ensure()
        out: dict[str, dict[str, Any]] = {}
        if self.root is None:
            return out
        left = self.plcproj_not_in_project
        for f in sorted(self.root.rglob("*.TcTTO")) + sorted(self.root.rglob("*.tctto")):
            if "_Boot" in f.parts or str(f) in out:
                continue
            if left and self._nearest(f, self.plcproj + left) in {pp["file"] for pp in left}:
                continue                      # N114: a task of a PLC project no .tsproj names is no task of the project
            try:
                task = ET.fromstring(read_source(f)).find("Task")
            except (ET.ParseError, OSError):
                continue
            if task is None:
                continue
            ct = (task.findtext("CycleTime") or "").strip()
            progs = [(c.findtext("Name") or "").strip() for c in task.findall("PouCall")]
            out[str(f)] = {"name": task.get("Name") or f.stem, "cycle_ms": int(ct) / 1000.0 if ct.isdigit() else None,
                           "programs": [p for p in progs if p], "plc_project": self._plc_of(f), "file": str(f)}
        return out

    def task_reason(self, unit_name: str, port: int | None = None) -> str | None:
        """Why tasks_of cannot tell a program's task, when the reason is the PLC project: the name is declared only in
        another PLC project than the one on `port` (multi-PLC audit, 0.8.13: such a program was labelled 'a GVL')."""
        if port is None:
            return None
        plc = self.plc_project(port=port)
        if plc is None:
            return None
        key = unit_name.lower()
        if key in self.plc_units(plc)[0]:
            return None
        others = sorted({Path(o or x.file).stem for x in self.all_units() if x.name.lower() == key
                         for o in self.plcs_of_file(x.file)})
        if not others:
            return None
        return (f"{unit_name} is not declared in PLC project {Path(plc).stem} (ADS port {port}) — it is in {others}: "
                "read from this port it is not that program; compared with the fastest task")

    def tasks_of(self, unit_name: str, port: int | None = None) -> list[dict[str, Any]] | None:
        """The tasks that run a PROGRAM: it is in a task's PouCall, or a program of that task calls it (transitively,
        from its body, actions and methods) — only tasks and programs of the program's own PLC project. Each task:
        {"name", "cycle_ms", "plc_project"}. port: the ADS port the program is read from — N114: the PLC project the
        .tsproj gives that port is the one meant (MAIN of Plc2 on 852, not MAIN of Plc1). [] when the name is no
        program (a GVL, an FB type: no owner of its own); None when the project cannot tell: no task configuration, or
        the name exists in several PLC projects and no .tsproj maps the port to one of them."""
        tasks = self.task_config()
        if not tasks:
            return None
        key = unit_name.lower()
        plc = self.plc_project(port=port) if port is not None else None
        if plc is None:
            if key in self.duplicates:
                return None
            u = self.units.get(key)
            if u is None or u.kind != "PROGRAM":
                return []
            plc = self._plc_of(u.file)
        units, dup = self.plc_units(plc)            # this PLC project's units, complete (N113)
        if key in dup:
            return None
        u = units.get(key)
        if u is None and any(x.name.lower() == key for x in self.all_units()):
            return None                            # declared in another PLC project only (task_reason says so)
        if u is None or u.kind != "PROGRAM":
            return []
        progs = {k: v for k, v in units.items() if v.kind == "PROGRAM"}
        calls: dict[str, set[str]] = {}       # program → programs it calls
        for k in progs:
            body = " ".join(x.implementation for x in units.values()
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
                if p in dup:
                    # a program of this name is declared twice in this PLC project: which one runs is not known —
                    # not followed
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
        txt, enc, _bom, note = decode_source(p.read_bytes())
        out = {"file": str(p), "chars": len(txt), "text": txt[:max_chars], "truncated": len(txt) > max_chars}
        if note or enc not in ("utf-8", "ascii"):
            out["encoding"] = enc
        if note:
            out["read_note"] = note
        return out

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
class PlcView(ProjectIndex):
    """N113: ONE PLC project of the index, as if it were the whole project — the analyses (lint, call graph, interface,
    XML checks, scaffold, symbol scope) resolve every name inside it, so MAIN of Plc2 never stands for MAIN of Plc1.
    Built from the index's published snapshot; reads only — a file re-indexed through it is re-indexed in the index."""

    def __init__(self, idx: ProjectIndex, plc: str | None):     # noqa: D107 — no ProjectIndex.__init__: shares its state
        self.__dict__.update(idx.__dict__)
        self._parent = idx
        self.plc = plc
        by_file = {f: us for f, us in idx._by_file.items() if plc in idx.plcs_of_file(f)}
        ix = _Index(by_file, {f: m for f, m in idx.files.items() if f in by_file})
        self.units, self.duplicates = ix.merged()
        self._by_file, self.files = by_file, ix.files
        self._plc_cache = None

    def _ensure(self) -> None:          # the index was brought up to date before the view was made
        return None

    def ensure_fresh(self) -> dict[str, Any] | None:
        return None

    def reindex_file(self, p: Path) -> None:
        self._parent.reindex_file(p)

    def refresh(self) -> dict[str, Any]:
        return self._parent.refresh()

    def scan(self, root: str | None = None) -> dict[str, Any]:
        return self._parent.scan(root)


def declaring(idx: ProjectIndex, name: str) -> tuple[list[str | None], list[str]]:
    """(the PLC projects — their .plcproj files — whose files declare `name`, those files). A '<PLC project>@Name' pin:
    that PLC project only — N142 (0.8.14 retest): a file compiled by two PLC projects gave both for 'PlcA@GVL_Common',
    and the pin the refusal asked for was refused the same way. A folder / file pin: its files."""
    pin = None
    if "@" in name:
        pin, name = name.split("@", 1)
    key = name.strip().lower()
    out: list[str | None] = []
    files: list[str] = []
    match = idx.pin_filter(pin)[0] if pin is not None else None
    only = idx.pin_plcs(pin) if pin is not None else set()
    for f, us in idx._by_file.items():
        if key in us and (match is None or match(f)):
            files.append(f)
            for plc in idx.plcs_of_file(f):
                if plc not in out and (not only or plc in only):
                    out.append(plc)
    return out, files


def plc_projects_declaring(idx: ProjectIndex, name: str) -> list[str | None]:
    """The PLC projects (their .plcproj files) whose files declare `name` (a '<PLC project>@Name' pin: that one)."""
    return declaring(idx, name)[0]


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
