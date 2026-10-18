"""Static helpers over the project index: FB interface summary, lint, call
graph, TcUnit scaffold. Deterministic, regex-based; meant to catch the dull
mistakes before a build, not to replace the compiler or TE1200.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from .project_backend import ProjectIndex, CodeUnit, ProjectError

# Default Hungarian-style prefixes (Beckhoff / common TwinCAT practice). Override
# via ProjectConfig.naming_prefixes if the house style differs.
DEFAULT_PREFIXES: dict[str, list[str]] = {
    "BOOL": ["b"], "BYTE": ["by", "n"], "WORD": ["w", "n"], "DWORD": ["dw", "n"], "LWORD": ["lw", "n"],
    "SINT": ["n", "si"], "USINT": ["n", "usi"], "INT": ["n", "i"], "UINT": ["n", "ui"], "DINT": ["n", "di"],
    "UDINT": ["n", "udi"], "LINT": ["n", "li"], "ULINT": ["n", "uli"],
    "REAL": ["r", "f"], "LREAL": ["r", "lr", "f"], "STRING": ["s"], "WSTRING": ["ws", "s"],
    "TIME": ["t"], "LTIME": ["t", "lt"], "DATE": ["d"], "DT": ["dt"], "DATE_AND_TIME": ["dt"], "TOD": ["tod"],
    "TIME_OF_DAY": ["tod"], "POINTER": ["p"], "REFERENCE": ["ref", "r"], "ARRAY": ["a", "arr"],
    "FB_": ["fb"], "ST_": ["st"], "E_": ["e"], "I_": ["ip", "i"], "ITF": ["i"],
}

_RE_IDENT = re.compile(r"[A-Za-z_]\w*")
_RE_TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b", re.I)
_RE_REAL_EQ = re.compile(r"(\b[rf]\w*|\w*(?:Real|REAL)\w*)\s*(=|<>)\s*[\d\.]+|[\d]+\.\d+\s*(=|<>)", re.I)
_RE_CALL = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_RE_METHOD_CALL = re.compile(r"\b([A-Za-z_]\w*\^?)(?:\[[^\]]*\])?\.([A-Za-z_]\w*)\s*\(")
_RE_COMMENT = re.compile(r"\(\*.*?\*\)|//[^\n]*", re.S)
_RE_STRING = re.compile(r"'[^']*'")
_RE_FOR = re.compile(r"\bFOR\s+(?P<var>[A-Za-z_]\w*)\s*:=\s*(?P<start>.+?)\s+TO\s+(?P<end>.+?)(?:\s+BY\s+(?P<step>.+?))?\s+DO\b", re.I | re.S)
_RE_NEW = re.compile(r"__NEW\s*\(\s*([A-Za-z_]\w*)", re.I)
_RE_DELETE = re.compile(r"__DELETE\s*\(", re.I)
_RE_ABSTRACT = re.compile(r"^\s*(?:FUNCTION_BLOCK|METHOD|PROPERTY)\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|FINAL)\s+)*ABSTRACT\b", re.I | re.M)
UNSIGNED_TYPES = {"USINT", "UINT", "UDINT", "ULINT", "BYTE", "WORD", "DWORD", "LWORD"}


def strip_comments(code: str) -> str:
    return _RE_STRING.sub("''", _RE_COMMENT.sub("", code or ""))


# ----------------------------------------------------------------------------
def _method_sig(idx: ProjectIndex, s: CodeUnit) -> dict[str, Any]:
    m = re.search(r"METHOD\s+((?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|ABSTRACT|FINAL|\s)*)(\w+)(?:\s*:\s*([\w\.\(\)\[\] ]+?))?\s*$",
                  s.declaration, re.M | re.I)
    return {
        "name": s.name.split(".")[-1],
        "access": (m.group(1).strip().upper() or "PUBLIC") if m else "PUBLIC",
        "return_type": (m.group(3).strip() if m and m.group(3) else None),
        "inputs": [{"name": v.name, "type": v.type, "comment": v.comment} for v in s.vars if v.section == "VAR_INPUT"],
        "outputs": [{"name": v.name, "type": v.type} for v in s.vars if v.section == "VAR_OUTPUT"],
        "in_out": [{"name": v.name, "type": v.type} for v in s.vars if v.section == "VAR_IN_OUT"],
        "file": s.file, "line": s.decl_line0,
    }


def fb_interface(idx: ProjectIndex, name: str) -> dict[str, Any]:
    """Own members plus everything inherited through EXTENDS (tagged inherited_from); an override in the
    FB hides the base's member of the same name; implements lists the FB's own and inherited interfaces."""
    idx._ensure()
    u = idx.get_unit(name)
    chain = [u] + (_ancestors(idx, u) if u.kind == "FUNCTION_BLOCK" else [])
    methods: list[dict[str, Any]] = []
    props: list[dict[str, Any]] = []
    actions: list[str] = []
    seen_m: set[str] = set()
    seen_p: set[str] = set()
    for owner in chain:
        tag = None if owner is u else owner.name
        for s in [x for x in idx.units.values() if x.parent == owner.name]:
            short = s.name.split(".")[-1]
            if s.kind == "METHOD" and short.lower() not in seen_m:
                seen_m.add(short.lower())
                sig = _method_sig(idx, s)
                if tag:
                    sig["inherited_from"] = tag
                    if sig["access"].startswith("PRIVATE"):
                        continue          # not visible in the derived FB
                methods.append(sig)
            elif s.kind == "PROPERTY" and short.lower() not in seen_p:
                seen_p.add(short.lower())
                m = re.search(r"PROPERTY\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL)\s+)?(\w+)\s*:\s*([\w\.\(\)\[\] ]+)", s.declaration, re.I)
                acc = [x.kind for x in idx.units.values() if x.parent == s.name]
                d = {"name": short, "type": m.group(2).strip() if m else None,
                     "get": "GET" in acc, "set": "SET" in acc, "file": s.file, "line": s.decl_line0}
                if tag:
                    d["inherited_from"] = tag
                props.append(d)
            elif s.kind == "ACTION" and owner is u:
                actions.append(short)
    by_section: dict[str, list[dict[str, Any]]] = {}
    seen_v: set[str] = set()
    for owner in chain:
        for v in owner.vars:
            if v.name.lower() in seen_v:
                continue
            seen_v.add(v.name.lower())
            d = {"name": v.name, "type": v.type, "init": v.init, "at": v.at, "comment": v.comment, "line": v.line}
            if owner is not u:
                d["inherited_from"] = owner.name
                d["file"] = owner.file          # D16: the line number belongs to the base's file
            by_section.setdefault(v.section, []).append(d)
    implements: list[str] = []
    for owner in chain:
        for i in owner.implements:
            if i.lower() not in (x.lower() for x in implements):
                implements.append(i)
    lib_base = chain[-1].extends[0] if chain[-1].extends and idx.units.get(chain[-1].extends[0].lower()) is None else None
    out = {"name": u.name, "kind": u.kind, "file": u.file, "extends": u.extends,
           "extends_chain": [c.name for c in chain[1:]] + ([lib_base] if lib_base else []),
           "implements": implements, "implements_own": u.implements,
           "return_type": u.return_type, "inputs": by_section.get("VAR_INPUT", []),
           "outputs": by_section.get("VAR_OUTPUT", []), "in_out": by_section.get("VAR_IN_OUT", []),
           "locals": by_section.get("VAR", []) + by_section.get("VAR_STAT", []),
           "methods": methods, "properties": props, "actions": actions,
           "ads_symbol_hint": (f"{u.name}.<var>  e.g. {u.name}.{(by_section.get('VAR', []) or by_section.get('VAR_OUTPUT', []) or [{'name': 'x'}])[0]['name']}"
                               if u.kind == "PROGRAM" else
                               f"<instance>.{{{', '.join(v['name'] for v in by_section.get('VAR_OUTPUT', [])[:4])}}}")}
    if lib_base:
        out["note"] = f"the chain ends in {lib_base}, which the project does not define (library): its members are not listed"
    return out


# ----------------------------------------------------------------------------
def lint(idx: ProjectIndex, name: str | None = None, rules: list[str] | None = None,
         prefixes: dict[str, list[str]] | None = None) -> dict[str, Any]:
    idx._ensure()
    pref = {**DEFAULT_PREFIXES, **(prefixes or {})}
    enabled = set(rules) if rules else {"unused", "naming", "undocumented_io", "todo", "real_eq", "assign_input",
                                        "empty_method", "qualified_only", "magic_numbers", "missing_super",
                                        "interface_contract", "extends_cycle", "abstract_instantiation",
                                        "shadowed_member", "unsigned_loop", "new_delete"}
    units = [idx.get_unit(name)] if name else [u for u in idx.units.values() if u.kind != "PARSE_ERROR"]
    if name:
        units += [u for u in idx.units.values() if u.parent == units[0].name]
    findings: list[dict[str, Any]] = []

    def add(rule: str, u: CodeUnit, line: int, msg: str, sev: str = "warning") -> None:
        findings.append({"rule": rule, "severity": sev, "unit": u.name, "file": u.file, "line": line, "message": msg})

    # -- project-level OOP rules (need the whole index, not just the unit in hand) --
    abstract_fbs = {u.name.lower() for u in idx.units.values()
                    if u.kind == "FUNCTION_BLOCK" and _RE_ABSTRACT.search(u.declaration or "")}
    for u in units:
        if u.kind != "FUNCTION_BLOCK":
            continue
        if "extends_cycle" in enabled and u.extends:
            cyc = _extends_cycle(idx, u)
            if cyc:
                add("extends_cycle", u, u.decl_line0, "EXTENDS chain is circular: " + " -> ".join(cyc), "error")
        if "interface_contract" in enabled and u.implements:
            for msg, sev in _interface_contract(idx, u):
                add("interface_contract", u, u.decl_line0, msg, sev)
        if "abstract_instantiation" in enabled and abstract_fbs:
            for v in u.vars:
                if _base_type(v.type).lower() in abstract_fbs and v.section != "VAR_IN_OUT" \
                        and not re.match(r"^(POINTER|REFERENCE)\s+TO\b", v.type.strip(), re.I):
                    add("abstract_instantiation", u, v.line,
                        f"{v.name} : {v.type} instantiates an ABSTRACT function block (compiler error C0372-style); "
                        f"use a POINTER TO / REFERENCE TO or a concrete subclass", "error")
    if "abstract_instantiation" in enabled and abstract_fbs:
        for u in units:
            if u.kind in ("PROGRAM", "GVL", "FUNCTION", "METHOD", "ACTION", "DUT"):
                for v in u.vars:
                    if _base_type(v.type).lower() in abstract_fbs and v.section != "VAR_IN_OUT" \
                            and not re.match(r"^(POINTER|REFERENCE)\s+TO\b", v.type.strip(), re.I):
                        add("abstract_instantiation", u, v.line,
                            f"{v.name} : {v.type} instantiates an ABSTRACT function block", "error")

    for u in units:
        impl = strip_comments(u.implementation)
        decl = strip_comments(u.declaration)
        # code of sub-items counts as usage for the parent's locals
        sub_code = " ".join(strip_comments(x.implementation) + " " + strip_comments(x.declaration)
                            for x in idx.units.values() if x.parent == u.name)
        idents_impl = set(_RE_IDENT.findall(impl + " " + sub_code))
        idents_decl_other: set[str] = set()
        for v in u.vars:
            if v.init:
                idents_decl_other |= set(_RE_IDENT.findall(v.init))
        if "unused" in enabled and u.kind in ("FUNCTION_BLOCK", "PROGRAM", "FUNCTION", "METHOD", "ACTION", "GET", "SET"):
            for v in u.vars:
                if v.section in ("VAR", "VAR_TEMP", "VAR_STAT", "VAR_INST") and v.name not in idents_impl and v.name not in idents_decl_other \
                        and v.name.lower() != u.name.split(".")[-1].lower():
                    add("unused", u, v.line, f"{v.name} ({v.type}) declared in {v.section} but never used")
        if "naming" in enabled:
            for v in u.vars:
                exp = _expected_prefixes(v.type, pref)
                if exp and not any(v.name.startswith(p) for p in exp) and not v.name.isupper():
                    add("naming", u, v.line, f"{v.name} : {v.type} — expected prefix {'/'.join(exp)}", "info")
        if "undocumented_io" in enabled and u.kind in ("FUNCTION_BLOCK", "FUNCTION", "METHOD"):
            for v in u.vars:
                if v.section in ("VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT") and not v.comment:
                    add("undocumented_io", u, v.line, f"{v.section} {v.name} has no comment", "info")
        if "todo" in enabled:
            for part, text, base in (("declaration", u.declaration, u.decl_line0), ("implementation", u.implementation, u.impl_line0)):
                for i, ln in enumerate(text.splitlines()):
                    if _RE_TODO.search(ln):
                        add("todo", u, base + i, ln.strip()[:120], "info")
        if "real_eq" in enabled:
            real_vars = {v.name for v in u.vars if v.type.upper() in ("REAL", "LREAL")}
            for i, ln in enumerate(impl.splitlines()):
                # D17: an equality test is `=` or `<>` — not the `=` inside `:=`, `<=`, `>=` or the output binding `=>`
                if any(re.search(rf"\b{re.escape(rv)}\s*(?:(?<![:<>])=(?!>)|<>)", ln)
                       or re.search(rf"(?:(?<![:<>])=(?!>)|<>)\s*{re.escape(rv)}\b", ln) for rv in real_vars):
                    add("real_eq", u, u.impl_line0 + i, "REAL/LREAL compared with = / <> ; use a tolerance", "warning")
        if "assign_input" in enabled and u.kind in ("FUNCTION_BLOCK", "FUNCTION"):
            inputs = {v.name for v in u.vars if v.section == "VAR_INPUT"}
            for i, ln in enumerate(impl.splitlines()):
                for nm in inputs:
                    # a statement start (line start / ; / THEN / ELSE / DO) followed by `input :=`;
                    # `fb(input := x)` named-parameter calls do not match
                    if re.search(rf"(?:^|;|\bTHEN\b|\bELSE\b|\bDO\b)\s*{re.escape(nm)}\s*:=", ln, re.I):
                        add("assign_input", u, u.impl_line0 + i, f"VAR_INPUT {nm} is assigned inside the body", "warning")
        owner_u = idx.units.get((_fb_owner(u) or "").lower()) if u.parent else None
        abstract_m = bool(_RE_ABSTRACT.search(u.declaration or ""))
        if "empty_method" in enabled and u.kind in ("METHOD", "ACTION") and not impl.strip(" \t\r\n;") \
                and not (owner_u is not None and owner_u.kind == "INTERFACE") and not abstract_m:
            add("empty_method", u, u.impl_line0, "empty implementation", "info")
        if "qualified_only" in enabled and u.kind in ("GVL", "ENUM") and "qualified_only" not in u.declaration:
            add("qualified_only", u, u.decl_line0, "missing {attribute 'qualified_only'}", "info")
        if "magic_numbers" in enabled and u.kind in ("FUNCTION_BLOCK", "PROGRAM", "METHOD"):
            for i, ln in enumerate(impl.splitlines()):
                if re.search(r"(?<![\w\.#])(?!0\b|1\b)\d+(?:\.\d+)?(?![\w\.#])", ln) and ":=" in ln and "T#" not in ln.upper() and "[" not in ln:
                    add("magic_numbers", u, u.impl_line0 + i, "numeric literal in assignment; consider a constant", "info")
        if "missing_super" in enabled and u.kind == "METHOD" and u.parent:
            parent = idx.units.get(u.parent.lower())
            mname = u.name.split(".")[-1]
            if parent and parent.extends and mname.lower() in ("fb_init", "fb_exit", "fb_reinit") and "SUPER^" not in impl.upper():
                add("missing_super", u, u.impl_line0, f"{mname} in a derived FB does not call SUPER^.{mname}", "warning")
        if "shadowed_member" in enabled and u.kind in ("METHOD", "PROPERTY", "GET", "SET", "ACTION") and u.parent:
            owner = idx.units.get(_fb_owner(u).lower()) if _fb_owner(u) else None
            if owner is not None:
                for v in u.vars:
                    if v.section in ("VAR_INPUT", "VAR", "VAR_TEMP", "VAR_OUTPUT", "VAR_IN_OUT"):
                        hit = _member_lookup(idx, owner, v.name)
                        if hit is not None:
                            where = "" if hit.name.lower() == owner.name.lower() else f" (inherited from {hit.name})"
                            add("shadowed_member", u, v.line,
                                f"{v.section} {v.name} shadows member {owner.name}.{v.name}{where}; inside this body the bare "
                                f"name is the local — use THIS^.{v.name} for the member or rename", "warning")
        if "unsigned_loop" in enabled and u.kind in ("FUNCTION_BLOCK", "PROGRAM", "FUNCTION", "METHOD", "ACTION", "GET", "SET"):
            for m in _RE_FOR.finditer(impl):
                var = m.group("var")
                vt = _var_type_in_scope(idx, u, var)
                line = u.impl_line0 + impl[:m.start()].count("\n")
                if vt is None or vt.upper() not in UNSIGNED_TYPES:
                    continue
                step = (m.group("step") or "").strip()
                end = m.group("end").strip()
                if step.startswith("-"):
                    add("unsigned_loop", u, line,
                        f"FOR {var} ({vt}) counts down with BY {step}: an unsigned loop variable never goes below 0, "
                        f"so the loop wraps and runs ~{2 ** (8 * _type_bytes(vt))} times", "error")
                elif re.search(r"-\s*1\s*$", end) and not re.search(r"\d\s*-\s*1\s*$", end):
                    add("unsigned_loop", u, line,
                        f"FOR {var} ({vt}) TO {end}: when the bound is 0 the unsigned '-1' wraps to {2 ** (8 * _type_bytes(vt)) - 1} "
                        f"and the loop runs that many times — guard with IF n > 0 or use a signed loop variable", "warning")
        if "new_delete" in enabled and u.kind in ("FUNCTION_BLOCK", "PROGRAM"):
            tree_code = impl + " " + " ".join(strip_comments(x.implementation) for x in idx.units.values() if x.parent == u.name)
            news = _RE_NEW.findall(tree_code)
            if news and not _RE_DELETE.search(tree_code):
                add("new_delete", u, u.impl_line0,
                    f"__NEW({', '.join(sorted(set(news)))}) without any __DELETE in {u.name} or its methods — heap leak "
                    f"on every re-init unless ownership is handed elsewhere", "warning")
            for tname in set(news):
                tu = idx.units.get(tname.lower())
                if tu is not None and tu.kind == "FUNCTION_BLOCK" and "enable_dynamic_creation" not in (tu.declaration or ""):
                    add("new_delete", u, u.impl_line0,
                        f"__NEW({tname}): {tname} lacks {{attribute 'enable_dynamic_creation'}} — compiler rejects the allocation", "error")
    sev_rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (sev_rank[f["severity"]], f["file"], f["line"]))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    out: dict[str, Any] = {"units": len(units), "findings": len(findings), "by_rule": counts, "items": findings[:1000],
                           "root": str(idx.root) if idx.root else None, "files_indexed": len(idx.files)}
    parse_errors = [u.name for u in idx.units.values() if u.kind == "PARSE_ERROR"]
    if parse_errors:
        out["parse_errors"] = parse_errors[:20]
    if not units:
        out["note"] = (f"nothing to lint: the index under {idx.root} holds {len(idx.units)} units"
                       + (f", all PARSE_ERROR ({parse_errors[:3]}…)" if parse_errors and len(parse_errors) == len(idx.units) else "")
                       + ". proj_scan(root=<folder with the .plcproj>) if this is not the project you meant; the index is "
                         "per server process and starts from project.root / xae.solution_path.")
    return out


def _base_type(type_str: str) -> str:
    """Element type behind ARRAY[..] OF / POINTER TO / REFERENCE TO wrappers (one level each)."""
    t = (type_str or "").strip()
    m = re.match(r"^ARRAY\s*\[.*?\]\s*OF\s+(.+)$", t, re.I)
    if m:
        t = m.group(1).strip()
    return t


def _type_bytes(t: str) -> int:
    return {"USINT": 1, "BYTE": 1, "UINT": 2, "WORD": 2, "UDINT": 4, "DWORD": 4, "ULINT": 8, "LWORD": 8}.get(t.upper(), 2)


def _fb_owner(u: CodeUnit) -> str | None:
    """Name of the POU that owns a method / property accessor (GET/SET hang off PROPERTY, which hangs off the FB)."""
    return u.parent.split(".")[0] if u.parent else None


def _member_lookup(idx: ProjectIndex, owner: CodeUnit, name: str) -> CodeUnit | None:
    """Unit (owner or an ancestor within the project) that declares a member variable ``name``."""
    seen: set[str] = set()
    stack = [owner]
    while stack:
        cur = stack.pop()
        if cur.name.lower() in seen:
            continue
        seen.add(cur.name.lower())
        if any(v.name.lower() == name.lower() and v.section != "VAR_TEMP" for v in cur.vars):
            return cur
        for e in cur.extends:
            eu = idx.units.get(e.lower())
            if eu:
                stack.append(eu)
    return None


def _var_type_in_scope(idx: ProjectIndex, u: CodeUnit, name: str) -> str | None:
    """Declared type of ``name`` as seen from inside ``u``: own vars first, then the owning FB and its bases."""
    for v in u.vars:
        if v.name.lower() == name.lower():
            return v.type
    owner_name = _fb_owner(u)
    owner = idx.units.get(owner_name.lower()) if owner_name else None
    if owner is not None:
        hit = _member_lookup(idx, owner, name)
        if hit is not None:
            for v in hit.vars:
                if v.name.lower() == name.lower():
                    return v.type
    return None


def _extends_cycle(idx: ProjectIndex, u: CodeUnit) -> list[str] | None:
    path = [u.name]
    seen = {u.name.lower()}
    cur = u
    while cur.extends:
        nxt = idx.units.get(cur.extends[0].lower())
        if nxt is None:
            return None
        if nxt.name.lower() in seen:
            return path + [nxt.name]
        seen.add(nxt.name.lower())
        path.append(nxt.name)
        cur = nxt
    return None


def _sig(u: CodeUnit) -> dict[str, Any]:
    ins = [(v.name.lower(), re.sub(r"\s+", "", v.type).lower()) for v in u.vars if v.section == "VAR_INPUT"]
    outs = [(v.name.lower(), re.sub(r"\s+", "", v.type).lower()) for v in u.vars if v.section in ("VAR_OUTPUT", "VAR_IN_OUT")]
    return {"ret": re.sub(r"\s+", "", u.return_type or "").lower(), "in": ins, "out": outs}


def _members_of(idx: ProjectIndex, root: CodeUnit, follow_extends: bool = True) -> dict[str, CodeUnit]:
    """METHOD / PROPERTY sub-units reachable from ``root`` (own first, then bases), keyed by lower-case short name."""
    out: dict[str, CodeUnit] = {}
    seen: set[str] = set()
    stack = [root]
    while stack:
        cur = stack.pop(0)
        if cur.name.lower() in seen:
            continue
        seen.add(cur.name.lower())
        for x in idx.units.values():
            if x.parent == cur.name and x.kind in ("METHOD", "PROPERTY"):
                out.setdefault(x.name.split(".")[-1].lower(), x)
        if follow_extends:
            for e in cur.extends:
                eu = idx.units.get(e.lower())
                if eu:
                    stack.append(eu)
    return out


def _interface_contract(idx: ProjectIndex, fb: CodeUnit) -> list[tuple[str, str]]:
    """Every method/property of each implemented interface (and the interfaces it extends) must exist on the FB
    or one of its bases, with the same return type and VAR_INPUT list. Unresolvable interface or base → info only:
    the project index cannot see into libraries, so it does not claim a violation."""
    msgs: list[tuple[str, str]] = []
    # can we see the whole FB side?  (an unresolved base might supply the method)
    bases_complete = True
    cur = fb
    seen: set[str] = set()
    while cur.extends and cur.name.lower() not in seen:
        seen.add(cur.name.lower())
        nxt = idx.units.get(cur.extends[0].lower())
        if nxt is None:
            bases_complete = False
            break
        cur = nxt
    have = _members_of(idx, fb)
    for iname in fb.implements:
        itf = idx.units.get(iname.lower())
        if itf is None or itf.kind != "INTERFACE":
            msgs.append((f"IMPLEMENTS {iname}: interface not in the project (library?) — contract not checked", "info"))
            continue
        need = _members_of(idx, itf)
        for short, im in need.items():
            mine = have.get(short)
            if mine is None:
                if bases_complete:
                    msgs.append((f"IMPLEMENTS {itf.name}: {im.kind} {im.name.split('.')[-1]} is missing on {fb.name} "
                                 f"(compiler error; the FB must provide every interface member)", "error"))
                else:
                    msgs.append((f"IMPLEMENTS {itf.name}: {im.kind} {im.name.split('.')[-1]} not found on {fb.name} or its "
                                 f"in-project bases; base {cur.extends[0]} is outside the project so it may supply it", "info"))
                continue
            if mine.kind != im.kind:
                msgs.append((f"IMPLEMENTS {itf.name}: {short} is a {im.kind} on the interface but a {mine.kind} on {fb.name}", "error"))
                continue
            a, b = _sig(im), _sig(mine)
            if a["ret"] != b["ret"] or a["in"] != b["in"]:
                msgs.append((f"IMPLEMENTS {itf.name}: {im.kind} {short} signature differs — interface "
                             f"({', '.join(f'{n}:{t}' for n, t in a['in'])}) : {a['ret'] or '-'} vs FB "
                             f"({', '.join(f'{n}:{t}' for n, t in b['in'])}) : {b['ret'] or '-'}", "error"))
    return msgs


def _expected_prefixes(type_str: str, pref: dict[str, list[str]]) -> list[str]:
    t = type_str.strip()
    up = t.upper()
    if up.startswith("ARRAY"):
        return pref.get("ARRAY", [])
    if up.startswith("POINTER"):
        return pref.get("POINTER", [])
    if up.startswith("REFERENCE"):
        return pref.get("REFERENCE", [])
    base = up.split("(")[0].split(".")[-1]
    if base in pref:
        return pref[base]
    for key in ("FB_", "ST_", "E_", "I_"):
        if base.startswith(key):
            return pref.get(key, [])
    return []


# ----------------------------------------------------------------------------
def _method_in_chain(idx: ProjectIndex, fb: CodeUnit, meth: str) -> CodeUnit | None:
    """FB.meth or the nearest ancestor's (EXTENDS chain within the project)."""
    seen: set[str] = set()
    cur: CodeUnit | None = fb
    while cur is not None and cur.name.lower() not in seen:
        seen.add(cur.name.lower())
        mu = idx.units.get(f"{cur.name}.{meth}".lower())
        if mu:
            return mu
        cur = idx.units.get(cur.extends[0].lower()) if cur.extends else None
    return None


def _ancestors(idx: ProjectIndex, fb: CodeUnit) -> list[CodeUnit]:
    out, seen = [], {fb.name.lower()}
    cur = fb
    while cur.extends:
        nxt = idx.units.get(cur.extends[0].lower())
        if nxt is None or nxt.name.lower() in seen:
            break
        seen.add(nxt.name.lower())
        out.append(nxt)
        cur = nxt
    return out


def _implementers(idx: ProjectIndex, itf: str) -> list[CodeUnit]:
    """Concrete FBs implementing an interface directly or through a base FB (abstract bases left out when a
    derived FB exists — the call lands in the derived instance)."""
    want = itf.lower()

    def itf_closure(name: str, seen: set[str]) -> set[str]:
        """An interface and every interface it EXTENDS (I_Named EXTENDS I_Thing → implementing I_Named implements I_Thing)."""
        key = name.lower()
        if key in seen:
            return seen
        seen.add(key)
        iu = idx.units.get(key)
        for e in (iu.extends if iu is not None else []):
            itf_closure(e, seen)
        return seen
    out = []
    for u in idx.units.values():
        if u.kind != "FUNCTION_BLOCK":
            continue
        chain = [u] + _ancestors(idx, u)
        provided: set[str] = set()
        for c in chain:
            for i in c.implements:
                itf_closure(i, provided)
        if want in provided:
            out.append(u)
    bases = {a.name.lower() for u in out for a in _ancestors(idx, u)}
    return [u for u in out if not (u.name.lower() in bases and _RE_ABSTRACT.search(u.declaration or ""))] or out


def _subclasses(idx: ProjectIndex, fb: CodeUnit) -> list[CodeUnit]:
    want = fb.name.lower()
    return [u for u in idx.units.values() if u.kind == "FUNCTION_BLOCK" and any(a.name.lower() == want for a in _ancestors(idx, u))]


def _var_in_scope(idx: ProjectIndex, u: CodeUnit, owner: CodeUnit | None, name: str):
    """A variable visible in u: its own vars (method locals/inputs), then the owning FB and its EXTENDS chain."""
    v = next((x for x in u.vars if x.name.lower() == name.lower()), None)
    if v:
        return v
    if owner is not None:
        hit = idx._find_var(owner, name)
        if hit:
            return hit[1]
    return None


def call_graph(idx: ProjectIndex, root: str, depth: int = 4) -> dict[str, Any]:
    idx._ensure()
    start = idx.get_unit(root)
    seen: set[str] = set()
    nodes: list[dict[str, Any]] = []

    def visit(u: CodeUnit, lvl: int, via: str | None) -> None:
        key = u.name.lower()
        node = {"name": u.name, "kind": u.kind, "depth": lvl, "via": via, "file": u.file}
        if key in seen:
            node["repeat"] = True
            nodes.append(node)
            return
        seen.add(key)
        nodes.append(node)
        if lvl >= depth:
            return
        impl = strip_comments(u.implementation) + " " + " ".join(
            strip_comments(x.implementation) for x in idx.units.values() if x.parent == u.name)
        # FB instances (by declared type) that are actually invoked
        scalar = set(DEFAULT_PREFIXES) | {"BOOL", "STRING", "WSTRING", "TIME", "LTIME", "DATE", "DT", "TOD"}
        for v in u.vars:
            tu = idx._unit_for_type(v.type)
            called = re.search(rf"\b{re.escape(v.name)}\s*\(", impl) is not None
            if tu and tu.kind in ("FUNCTION_BLOCK", "PROGRAM") and re.search(rf"\b{re.escape(v.name)}\b", impl):
                visit(tu, lvl + 1, f"{u.name}.{v.name}")
            elif tu is None and called:
                # an FB from a library (Tc2_Standard.CTU, TON …): the project has no body to descend into,
                # but the call is part of what runs when this POU runs
                base = idx._base_type_name(v.type)
                if base.upper() not in scalar and not re.match(r"^(ARRAY|POINTER|REFERENCE)\b", v.type.strip(), re.I):
                    nodes.append({"name": base, "kind": "library", "depth": lvl + 1, "via": f"{u.name}.{v.name}",
                                  "file": None, "type": v.type, "note": "library FB — body not in the project; infosys_declaration for its interface"})
        # direct function / FB-type calls
        for m in _RE_CALL.finditer(impl):
            tu = idx.units.get(m.group(1).lower())
            if tu and tu.kind in ("FUNCTION", "FUNCTION_BLOCK", "PROGRAM") and tu.name.lower() != key:
                visit(tu, lvl + 1, f"{u.name} → {m.group(1)}()")
        owner = idx.units.get((u.parent or u.name).split(".")[0].lower()) if u.parent else u
        for m in _RE_METHOD_CALL.finditer(impl):
            inst, meth = m.group(1), m.group(2)
            if inst.upper() in ("THIS^", "SUPER^"):
                fb = owner if inst.upper() == "THIS^" else (idx.units.get(owner.extends[0].lower()) if owner and owner.extends else None)
                if fb is None:
                    continue
                mu = _method_in_chain(idx, fb, meth)
                if mu:
                    visit(mu, lvl + 1, f"{u.name} → {inst}.{meth}()")
                if inst.upper() == "THIS^":
                    # dynamic dispatch: a derived FB's override runs when the instance is of that type
                    for sub in _subclasses(idx, fb):
                        ov = idx.units.get(f"{sub.name}.{meth}".lower())
                        if ov and ov is not mu:
                            visit(ov, lvl + 1, f"{u.name} → THIS^.{meth}() (override in {sub.name})")
                continue
            hit = _var_in_scope(idx, u, owner, inst)
            if hit is None:
                continue
            tname = idx._base_type_name(hit.type)
            tu = idx.units.get(tname.lower())
            if tu is None:
                continue
            if tu.kind == "INTERFACE":
                impls = _implementers(idx, tu.name)
                for fb in impls:
                    mu = _method_in_chain(idx, fb, meth)
                    if mu:
                        visit(mu, lvl + 1, f"{u.name}.{inst}.{meth}() via {tu.name} → {fb.name}")
                    for ov in [idx.units.get(f"{sub.name}.{meth}".lower()) for sub in _subclasses(idx, fb)]:
                        if ov and ov is not mu:
                            visit(ov, lvl + 1, f"{u.name}.{inst}.{meth}() via {tu.name} (override)")
                if not impls:
                    nodes.append({"name": f"{tu.name}.{meth}", "kind": "interface_call", "depth": lvl + 1,
                                  "via": f"{u.name}.{inst}.{meth}()", "file": tu.file,
                                  "note": "no FB in the project implements this interface"})
            elif tu.kind == "FUNCTION_BLOCK":
                mu = _method_in_chain(idx, tu, meth)
                if mu:
                    visit(mu, lvl + 1, f"{u.name}.{inst}.{meth}()")

    visit(start, 0, None)
    return {"root": start.name, "depth": depth, "nodes": nodes, "unique": len(seen)}


# ----------------------------------------------------------------------------
_RE_GUID = re.compile(r"^\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$")
_RE_LINEIDS = re.compile(r"<LineIds Name=\"([^\"]+)\">(.*?)</LineIds>", re.S)
_RE_LINEID = re.compile(r"<LineId Id=\"(\d+)\" Count=\"(\d+)\"\s*/>")


def validate_xml(idx: ProjectIndex, name: str | None = None, fix: bool = False,
                 regenerate_lineids: bool = False) -> dict[str, Any]:
    """Structural checks of TwinCAT source files: well-formed XML, root/Name/Id, GUID format and
    project-wide uniqueness, Declaration/Implementation presence, property accessor VAR blocks,
    LineIds ↔ implementation line counts, file ending. fix=true applies safe fixes (missing final
    newline, GUID case, duplicate GUID → new GUID, and — only with regenerate_lineids — rebuilt
    LineIds); writes a .bak first."""
    import xml.etree.ElementTree as ET
    idx._ensure()
    files = sorted({u.file for u in idx.units.values() if u.kind != "PARSE_ERROR" and (not name or u.name.lower() == name.lower() or (u.parent or "").lower() == name.lower())})
    if name and not files:
        files = [idx.get_unit(name).file]
    guids: dict[str, list[str]] = {}
    for f in sorted({u.file for u in idx.units.values()}):
        try:
            txt = Path(f).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for g in re.findall(r'Id="(\{[^"]+\})"', txt):
            guids.setdefault(g.upper(), []).append(f)
    findings: list[dict[str, Any]] = []
    fixed: list[dict[str, Any]] = []
    for f in files:
        p = Path(f)
        raw = p.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        txt = raw[3:].decode("utf-8", "replace") if bom else raw.decode("utf-8", "replace")
        new = txt
        try:
            root = ET.fromstring(txt)
        except ET.ParseError as exc:
            findings.append({"file": f, "rule": "xml", "severity": "error", "message": f"not well-formed: {exc}"})
            continue
        if root.tag != "TcPlcObject":
            findings.append({"file": f, "rule": "root", "severity": "error", "message": f"root is <{root.tag}>, expected <TcPlcObject>"})
        obj = next((c for c in root), None)
        if obj is None or obj.tag not in ("POU", "DUT", "GVL", "Itf", "Task", "Visu"):
            findings.append({"file": f, "rule": "object", "severity": "error", "message": "no POU/DUT/GVL/Itf element"})
            continue
        if obj.get("Name") != p.stem:
            findings.append({"file": f, "rule": "name", "severity": "warning", "message": f"object Name '{obj.get('Name')}' differs from file name '{p.stem}'"})
        for el in root.iter():
            g = el.get("Id")
            if g is None or el.tag == "LineId":   # LineId Id="12" is a line number, not a GUID
                continue
            if not _RE_GUID.match(g):
                findings.append({"file": f, "rule": "guid", "severity": "error", "message": f"bad GUID {g!r} on <{el.tag} Name={el.get('Name')}>"})
            else:
                if g != g.upper() and g != g.lower():
                    findings.append({"file": f, "rule": "guid_case", "severity": "info", "message": f"mixed-case GUID {g}"})
                owners = set(guids.get(g.upper(), []))
                if len(owners) > 1 or guids.get(g.upper(), []).count(f) > 1:
                    findings.append({"file": f, "rule": "guid_duplicate", "severity": "error", "message": f"GUID {g} also used in {sorted(owners - {f}) or 'this file (twice)'}"})
                    if fix:
                        new = new.replace(g, "{" + str(uuid.uuid4()).upper() + "}", 1)
                        fixed.append({"file": f, "fix": "new GUID", "for": el.get("Name")})
        # declaration / implementation presence
        kind = obj.tag
        if kind in ("POU", "DUT", "GVL", "Itf") and obj.find("Declaration") is None:
            findings.append({"file": f, "rule": "declaration", "severity": "error", "message": "missing <Declaration>"})
        if kind == "POU" and obj.find("Implementation") is None:
            findings.append({"file": f, "rule": "implementation", "severity": "warning", "message": "POU without <Implementation>"})
        for prop in obj.findall("Property"):
            for acc in ("Get", "Set"):
                a = prop.find(acc)
                if a is not None:
                    d = a.find("Declaration")
                    if d is None or "VAR" not in (d.text or "").upper():
                        findings.append({"file": f, "rule": "property_var", "severity": "warning",
                                         "message": f"{prop.get('Name')}.{acc} declaration lacks a VAR…END_VAR block (XAE import rejects it)"})
        # LineIds vs implementation lines
        impl_lines: dict[str, int] = {}
        pou_name = obj.get("Name") or p.stem
        st = obj.find("Implementation/ST")
        if st is not None:
            impl_lines[pou_name] = len((st.text or "").split("\n"))
        for tag in ("Method", "Action", "Transition"):
            for m in obj.findall(tag):
                st2 = m.find("Implementation/ST")
                if st2 is not None:
                    impl_lines[f"{pou_name}.{m.get('Name')}"] = len((st2.text or "").split("\n"))
        for prop in obj.findall("Property"):
            for acc in ("Get", "Set"):
                a = prop.find(acc)
                if a is not None and a.find("Implementation/ST") is not None:
                    impl_lines[f"{pou_name}.{prop.get('Name')}.{acc}"] = len((a.find("Implementation/ST").text or "").split("\n"))
        for m in _RE_LINEIDS.finditer(txt):
            lname, body = m.group(1), m.group(2)
            have = sum(1 + int(c) for _i, c in _RE_LINEID.findall(body))
            want = impl_lines.get(lname)
            if want is None:
                findings.append({"file": f, "rule": "lineids_orphan", "severity": "info", "message": f"<LineIds Name=\"{lname}\"> has no implementation"})
            elif have != want:
                findings.append({"file": f, "rule": "lineids_count", "severity": "warning",
                                 "message": f"LineIds for {lname}: {have} ids vs {want} implementation lines (online-change line mapping will be off)"})
                if fix and regenerate_lineids:
                    new = new.replace(m.group(0), _lineids_block(lname, want), 1)
                    fixed.append({"file": f, "fix": "regenerated LineIds", "for": lname})
        missing_lineids = [k for k in impl_lines if f'<LineIds Name="{k}">' not in txt]
        # XAE itself writes files without LineIds for items that were never opened in the editor (and 4026 writes
        # none for properties/empty bodies): only an *inconsistent* file — some items with, some without — is worth a word
        if missing_lineids and len(missing_lineids) < len(impl_lines):
            for k in missing_lineids:
                findings.append({"file": f, "rule": "lineids_missing", "severity": "info",
                                 "message": f"no <LineIds> for {k} while other items in the file have them (XAE regenerates them)"})
        if fix and regenerate_lineids and missing_lineids:
            close = f"  </{kind}>"
            blocks = "".join("    " + _lineids_block(k, impl_lines[k]) + "\n" for k in missing_lineids)
            if close in new:
                new = new.replace(close, blocks + close, 1)
                fixed.append({"file": f, "fix": "added LineIds", "for": missing_lineids})
        # (no eof_newline rule: XAE writes its files without a trailing newline — flagging that flagged every file)
        if fix and new != txt:
            p.with_suffix(p.suffix + ".bak").write_bytes(raw)
            p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
            try:
                idx._index_file(p)
            except Exception:  # noqa: BLE001
                p.write_bytes(raw)
                fixed.append({"file": f, "fix": "REVERTED (result not parseable)"})
    by_rule: dict[str, int] = {}
    for fi in findings:
        by_rule[fi["rule"]] = by_rule.get(fi["rule"], 0) + 1
    return {"files": len(files), "findings": len(findings), "by_rule": by_rule, "items": findings[:1000], "fixed": fixed}


def _lineids_block(name: str, lines: int) -> str:
    inner = "\n".join(f'      <LineId Id="{i + 1}" Count="0" />' for i in range(max(lines, 1)))
    return f'<LineIds Name="{name}">\n{inner}\n    </LineIds>'


_TCPOU_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<TcPlcObject Version="1.1.0.1" ProductVersion="{product_version}">
  <POU Name="{name}" Id="{{{guid}}}" SpecialFunc="None">
    <Declaration><![CDATA[{decl}]]></Declaration>
    <Implementation>
      <ST><![CDATA[{impl}]]></ST>
    </Implementation>
{methods}  </POU>
</TcPlcObject>
"""
_METHOD_TMPL = """    <Method Name="{name}" Id="{{{guid}}}">
      <Declaration><![CDATA[{decl}]]></Declaration>
      <Implementation>
        <ST><![CDATA[{impl}]]></ST>
      </Implementation>
    </Method>
"""


def tcunit_scaffold(idx: ProjectIndex, fb_name: str, out_dir: str | None = None,
                    add_to_plcproj: bool = True, overwrite: bool = False) -> dict[str, Any]:
    """Generate FB_<fb>_Tests.TcPOU (TcUnit.FB_TestSuite) with one test method per
    public method plus an initial-state test, and register it in the .plcproj."""
    iface = fb_interface(idx, fb_name)
    if iface["kind"] != "FUNCTION_BLOCK":
        raise ProjectError(f"{fb_name} is a {iface['kind']}, scaffold targets function blocks")
    fb = iface["name"]
    suite = f"{fb}_Tests"
    src = Path(iface["file"])
    dest_dir = Path(out_dir) if out_dir else src.parent / "Tests"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{suite}.TcPOU"
    if dest.exists() and not overwrite:
        raise ProjectError(f"{dest} exists; pass overwrite=true")
    tests: list[tuple[str, str, str]] = []
    _enum_cache: dict[str, str] = {}

    def _default_for(type_str: str) -> str:            # noqa: F811 — the project-aware variant (D39)
        return _default_in(idx, type_str, _enum_cache)
    # Arrange restates each input's declared initial value (fbUut.nPreset := 10, not 0) — a test that silently
    # overrides the FB's defaults tests a different FB
    inputs_arrange = "\n".join(f"\tfbUut.{v['name']} := {v['init'] if v.get('init') else _default_for(v['type'])};  // "
                               f"{v['comment'] or v['type']}{' (declared default)' if v.get('init') else ''}"
                               f"{' — inherited from ' + v['inherited_from'] if v.get('inherited_from') else ''}"
                               for v in iface["inputs"])
    product_version = _product_version(src)
    out_asserts = "\n".join(f"\tAssertEquals_{_assert_suffix(v['type'])}(Expected := {_default_for(v['type'])}, Actual := fbUut.{v['name']}, Message := '{v['name']} initial');"
                            for v in iface["outputs"] if _assert_suffix(v["type"]))
    tests.append(("Test_InitialState",
                  f"METHOD PRIVATE Test_InitialState\nVAR\n\tfbUut : {fb};\nEND_VAR\n",
                  f"TEST('Test_InitialState');\n\n// Arrange\n{inputs_arrange}\n\n// Act\nfbUut();\n\n// Assert\n{out_asserts or chr(9) + 'AssertTrue(Condition := TRUE, Message := ''replace me'');'}\n\nTEST_FINISHED();"))
    for m in iface["methods"]:
        if m["access"] not in ("PUBLIC",) or m["name"].lower() in ("fb_init", "fb_exit", "fb_reinit"):
            continue
        args = ", ".join(f"{p['name']} := {_default_for(p['type'])}" for p in m["inputs"])
        call = f"fbUut.{m['name']}({args})"
        if m["return_type"]:
            decl_extra = f"\t{_var_name_for(m['return_type'])}Result : {m['return_type']};\n"
            act = f"{_var_name_for(m['return_type'])}Result := {call};"
        else:
            decl_extra = ""
            act = f"{call};"
        inh = f"  // inherited from {m['inherited_from']}" if m.get("inherited_from") else ""
        tests.append((f"Test_{m['name']}",
                      f"METHOD PRIVATE Test_{m['name']}\nVAR\n\tfbUut : {fb};\n{decl_extra}END_VAR\n",
                      f"TEST('Test_{m['name']}');{inh}\n\n// Arrange\n{inputs_arrange}\n\n// Act\n{act}\nfbUut();\n\n// Assert\n\tAssertTrue(Condition := FALSE, Message := 'Test_{m['name']}: write the assertion');\n\nTEST_FINISHED();"))
    decl = f"FUNCTION_BLOCK {suite} EXTENDS TcUnit.FB_TestSuite\n// Generated by twincat-mcp for {fb} — fill in the assertions.\nVAR\nEND_VAR\n"
    impl = "\n".join(f"{t[0]}();" for t in tests)
    methods_xml = "".join(_METHOD_TMPL.format(name=t[0], guid=uuid.uuid4(), decl=t[1], impl=t[2]) for t in tests)
    dest.write_text(_TCPOU_TMPL.format(name=suite, guid=uuid.uuid4(), decl=decl, impl=impl, methods=methods_xml,
                                       product_version=product_version), encoding="utf-8")
    result: dict[str, Any] = {"suite": suite, "file": str(dest), "tests": [t[0] for t in tests]}
    stubs = [t[0] for t in tests if "AssertTrue(Condition := FALSE" in t[2]]
    if stubs:
        result["stubs_fail_until_written"] = stubs
        result["stub_note"] = ("these tests FAIL on purpose until their assertion is written — a generated test that "
                               "passes without checking anything would report coverage that does not exist")
    if add_to_plcproj:
        result["plcproj"] = _register_in_plcproj(idx, dest)
    result["next"] = [f"Add `{suite}();` to the TcUnit runner program (and TcUnit.RUN();)",
                      "Reference the TcUnit library in the PLC project if not already",
                      "proj_scan to refresh the index, then xae_build / tcunit_run"]
    try:
        idx._index_file(dest)
    except Exception:  # noqa: BLE001
        pass
    return result


def _register_in_plcproj(idx: ProjectIndex, pou: Path) -> dict[str, Any]:
    for pp in idx.plcproj:
        proj = Path(pp["file"])
        try:
            rel = pou.relative_to(proj.parent)
        except ValueError:
            continue
        inc = str(rel).replace("/", "\\")
        text = proj.read_text(encoding="utf-8-sig")
        if f'Include="{inc}"' in text:
            return {"file": str(proj), "already": True}
        new = re.sub(r"(<Compile Include=\"[^\"]+\"[^>]*>(?:.*?</Compile>)?\s*)(</ItemGroup>)",
                     lambda m: m.group(1) + f'<Compile Include="{inc}">\n      <SubType>Code</SubType>\n    </Compile>\n  ' + m.group(2),
                     text, count=1, flags=re.S)
        if new == text:
            return {"file": str(proj), "added": False, "note": "no <Compile> ItemGroup found; add the file in XAE"}
        proj.write_text(new, encoding="utf-8")
        return {"file": str(proj), "added": inc}
    return {"added": False, "note": "POU is outside every known .plcproj folder"}


def _product_version(src: Path) -> str:
    """#66: the TwinCAT build the project is on — the newest of the source file's ProductVersion and the
    TcVersion of the .tsproj above it (a POU last saved by an older XAE still lives in a newer project)."""
    found: list[str] = []
    try:
        m = re.search(r'ProductVersion="([^"]+)"', src.read_text(encoding="utf-8-sig", errors="replace")[:2000])
        if m:
            found.append(m.group(1))
    except OSError:
        pass
    for d in list(src.parents)[:6]:
        for ts in d.glob("*.tsproj"):
            try:
                m = re.search(r'TcVersion="([\d.]+)"', ts.read_text(encoding="utf-8-sig", errors="replace")[:4000])
            except OSError:
                m = None
            if m:
                found.append(m.group(1))
        if found and len(found) > 1:
            break
    def key(v: str) -> tuple[int, ...]:
        return tuple(int(x) for x in re.findall(r"\d+", v))
    return max(found, key=key) if found else "3.1.4024.12"


_RE_ENUM_BODY = re.compile(r"TYPE\s+\w+\s*:\s*\((?P<body>.*?)\)\s*\w*\s*;?\s*END_TYPE", re.S | re.I)


def _enum_first(idx: ProjectIndex, type_str: str) -> str | None:
    """'E_Command.Idle' for an enum the project declares: the member with value 0, else the first member (D39: the
    scaffold wrote 'E_Command.0', which does not compile)."""
    try:
        u = idx.units.get(type_str.strip().lower())
    except Exception:  # noqa: BLE001
        u = None
    if u is None or not u.declaration:
        return None
    text = re.sub(r"\(\*.*?\*\)", "", re.sub(r"//[^\n]*", "", u.declaration), flags=re.S)
    m = _RE_ENUM_BODY.search(text)
    if not m:
        return None
    members = []
    for part in m.group("body").split(","):
        mm = re.match(r"\s*([A-Za-z_]\w*)\s*(?::=\s*([^,]+))?", part)
        if mm:
            members.append((mm.group(1), (mm.group(2) or "").strip()))
    if not members:
        return None
    zero = next((n for n, v in members if v in ("0", "16#0", "2#0", "8#0")), None)
    first_implicit = members[0][0] if not members[0][1] else None
    return f"{u.name}.{zero or first_implicit or members[0][0]}"


def _default_in(idx: ProjectIndex, type_str: str, cache: dict[str, str]) -> str:
    key = type_str.lower()
    if key not in cache:
        e = _enum_first(idx, type_str)
        cache[key] = e if e else _default_for(type_str)
    return cache[key]


def _default_for(type_str: str) -> str:
    up = type_str.upper().split("(")[0]
    if up == "BOOL":
        return "FALSE"
    if up in ("REAL", "LREAL"):
        return "0.0"
    if up.startswith("STRING") or up.startswith("WSTRING"):
        return "''"
    if up in ("TIME", "LTIME"):
        return "T#0S"
    if up.startswith(("INT", "UINT", "DINT", "UDINT", "SINT", "USINT", "LINT", "ULINT", "BYTE", "WORD", "DWORD", "LWORD")):
        return "0"
    if up.startswith("E_"):
        # an enum the project does not declare (library): its members are unknown here — 0 is accepted for a
        # non-strict enum; a {attribute 'strict'} enum needs a member name, which the comment asks for
        return f"0 (* {type_str}: set a member *)"
    return "0"


def _assert_suffix(type_str: str) -> str | None:
    up = type_str.upper().split("(")[0]
    return {"BOOL": "BOOL", "INT": "INT", "UINT": "UINT", "DINT": "DINT", "UDINT": "UDINT", "REAL": "REAL",
            "LREAL": "LREAL", "STRING": "STRING", "TIME": "TIME", "BYTE": "BYTE", "WORD": "WORD", "DWORD": "DWORD",
            "SINT": "SINT", "USINT": "USINT", "LINT": "LINT", "ULINT": "ULINT", "LWORD": "LWORD"}.get(up)


def _var_name_for(type_str: str) -> str:
    up = type_str.upper().split("(")[0]
    return {"BOOL": "b", "REAL": "r", "LREAL": "r", "STRING": "s", "TIME": "t"}.get(up, "n")
