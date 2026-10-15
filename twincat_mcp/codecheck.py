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
_RE_METHOD_CALL = re.compile(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")
_RE_COMMENT = re.compile(r"\(\*.*?\*\)|//[^\n]*", re.S)
_RE_STRING = re.compile(r"'[^']*'")


def strip_comments(code: str) -> str:
    return _RE_STRING.sub("''", _RE_COMMENT.sub("", code or ""))


# ----------------------------------------------------------------------------
def fb_interface(idx: ProjectIndex, name: str) -> dict[str, Any]:
    u = idx.get_unit(name)
    subs = [x for x in idx.units.values() if x.parent == u.name]
    methods, props = [], []
    for s in subs:
        if s.kind == "METHOD":
            m = re.search(r"METHOD\s+((?:PUBLIC|PRIVATE|PROTECTED|INTERNAL|ABSTRACT|FINAL|\s)*)(\w+)(?:\s*:\s*([\w\.\(\)\[\] ]+?))?\s*$",
                          s.declaration, re.M | re.I)
            methods.append({
                "name": s.name.split(".")[-1],
                "access": (m.group(1).strip().upper() or "PUBLIC") if m else "PUBLIC",
                "return_type": (m.group(3).strip() if m and m.group(3) else None),
                "inputs": [{"name": v.name, "type": v.type, "comment": v.comment} for v in s.vars if v.section == "VAR_INPUT"],
                "outputs": [{"name": v.name, "type": v.type} for v in s.vars if v.section == "VAR_OUTPUT"],
                "in_out": [{"name": v.name, "type": v.type} for v in s.vars if v.section == "VAR_IN_OUT"],
                "file": s.file, "line": s.decl_line0,
            })
        elif s.kind == "PROPERTY":
            m = re.search(r"PROPERTY\s+(?:(?:PUBLIC|PRIVATE|PROTECTED|INTERNAL)\s+)?(\w+)\s*:\s*([\w\.\(\)\[\] ]+)", s.declaration, re.I)
            acc = [x.kind for x in idx.units.values() if x.parent == s.name]
            props.append({"name": s.name.split(".")[-1], "type": m.group(2).strip() if m else None,
                          "get": "GET" in acc, "set": "SET" in acc, "file": s.file, "line": s.decl_line0})
    by_section: dict[str, list[dict[str, Any]]] = {}
    for v in u.vars:
        by_section.setdefault(v.section, []).append({"name": v.name, "type": v.type, "init": v.init, "at": v.at, "comment": v.comment, "line": v.line})
    return {"name": u.name, "kind": u.kind, "file": u.file, "extends": u.extends, "implements": u.implements,
            "return_type": u.return_type, "inputs": by_section.get("VAR_INPUT", []),
            "outputs": by_section.get("VAR_OUTPUT", []), "in_out": by_section.get("VAR_IN_OUT", []),
            "locals": by_section.get("VAR", []) + by_section.get("VAR_STAT", []),
            "methods": methods, "properties": props,
            "actions": [x.name.split(".")[-1] for x in subs if x.kind == "ACTION"],
            "ads_symbol_hint": f"<instance>.{{{', '.join(v['name'] for v in by_section.get('VAR_OUTPUT', [])[:4])}}}"}


# ----------------------------------------------------------------------------
def lint(idx: ProjectIndex, name: str | None = None, rules: list[str] | None = None,
         prefixes: dict[str, list[str]] | None = None) -> dict[str, Any]:
    idx._ensure()
    pref = {**DEFAULT_PREFIXES, **(prefixes or {})}
    enabled = set(rules) if rules else {"unused", "naming", "undocumented_io", "todo", "real_eq", "assign_input",
                                        "empty_method", "qualified_only", "magic_numbers", "missing_super"}
    units = [idx.get_unit(name)] if name else [u for u in idx.units.values() if u.kind != "PARSE_ERROR"]
    if name:
        units += [u for u in idx.units.values() if u.parent == units[0].name]
    findings: list[dict[str, Any]] = []

    def add(rule: str, u: CodeUnit, line: int, msg: str, sev: str = "warning") -> None:
        findings.append({"rule": rule, "severity": sev, "unit": u.name, "file": u.file, "line": line, "message": msg})

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
                if any(re.search(rf"\b{re.escape(rv)}\s*(=|<>)", ln) or re.search(rf"(=|<>)\s*{re.escape(rv)}\b", ln) for rv in real_vars):
                    add("real_eq", u, u.impl_line0 + i, "REAL/LREAL compared with = / <> ; use a tolerance", "warning")
        if "assign_input" in enabled and u.kind in ("FUNCTION_BLOCK", "FUNCTION"):
            inputs = {v.name for v in u.vars if v.section == "VAR_INPUT"}
            for i, ln in enumerate(impl.splitlines()):
                for nm in inputs:
                    # a statement start (line start / ; / THEN / ELSE / DO) followed by `input :=`;
                    # `fb(input := x)` named-parameter calls do not match
                    if re.search(rf"(?:^|;|\bTHEN\b|\bELSE\b|\bDO\b)\s*{re.escape(nm)}\s*:=", ln, re.I):
                        add("assign_input", u, u.impl_line0 + i, f"VAR_INPUT {nm} is assigned inside the body", "warning")
        if "empty_method" in enabled and u.kind in ("METHOD", "ACTION") and not impl.strip(" \t\r\n;"):
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
    sev_rank = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (sev_rank[f["severity"]], f["file"], f["line"]))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["rule"]] = counts.get(f["rule"], 0) + 1
    return {"units": len(units), "findings": len(findings), "by_rule": counts, "items": findings[:1000]}


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
        for v in u.vars:
            tu = idx._unit_for_type(v.type)
            if tu and tu.kind in ("FUNCTION_BLOCK", "PROGRAM") and re.search(rf"\b{re.escape(v.name)}\b", impl):
                visit(tu, lvl + 1, f"{u.name}.{v.name}")
        # direct function / FB-type calls
        for m in _RE_CALL.finditer(impl):
            tu = idx.units.get(m.group(1).lower())
            if tu and tu.kind in ("FUNCTION", "FUNCTION_BLOCK") and tu.name.lower() != key:
                visit(tu, lvl + 1, f"{u.name} → {m.group(1)}()")
        for m in _RE_METHOD_CALL.finditer(impl):
            inst, meth = m.group(1), m.group(2)
            v = next((x for x in u.vars if x.name.lower() == inst.lower()), None)
            if v:
                tu = idx._unit_for_type(v.type)
                if tu:
                    mu = idx.units.get(f"{tu.name}.{meth}".lower())
                    if mu:
                        visit(mu, lvl + 1, f"{u.name}.{inst}.{meth}()")
            elif inst.upper() == "THIS^":
                mu = idx.units.get(f"{u.parent or u.name}.{meth}".lower())
                if mu:
                    visit(mu, lvl + 1, f"{u.name} → THIS^.{meth}()")

    visit(start, 0, None)
    return {"root": start.name, "depth": depth, "nodes": nodes, "unique": len(seen)}


# ----------------------------------------------------------------------------
_TCPOU_TMPL = """<?xml version="1.0" encoding="utf-8"?>
<TcPlcObject Version="1.1.0.1" ProductVersion="3.1.4024.12">
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
    inputs_arrange = "\n".join(f"\tfbUut.{v['name']} := {_default_for(v['type'])};  // {v['comment'] or v['type']}" for v in iface["inputs"])
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
        tests.append((f"Test_{m['name']}",
                      f"METHOD PRIVATE Test_{m['name']}\nVAR\n\tfbUut : {fb};\n{decl_extra}END_VAR\n",
                      f"TEST('Test_{m['name']}');\n\n// Arrange\n{inputs_arrange}\n\n// Act\n{act}\nfbUut();\n\n// Assert\n\tAssertTrue(Condition := FALSE, Message := 'Test_{m['name']}: write the assertion');\n\nTEST_FINISHED();"))
    decl = f"FUNCTION_BLOCK {suite} EXTENDS TcUnit.FB_TestSuite\n// Generated by twincat-mcp for {fb} — fill in the assertions.\nVAR\nEND_VAR\n"
    impl = "\n".join(f"{t[0]}();" for t in tests)
    methods_xml = "".join(_METHOD_TMPL.format(name=t[0], guid=uuid.uuid4(), decl=t[1], impl=t[2]) for t in tests)
    dest.write_text(_TCPOU_TMPL.format(name=suite, guid=uuid.uuid4(), decl=decl, impl=impl, methods=methods_xml), encoding="utf-8")
    result: dict[str, Any] = {"suite": suite, "file": str(dest), "tests": [t[0] for t in tests]}
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
        return f"{type_str}.0"
    return "0"


def _assert_suffix(type_str: str) -> str | None:
    up = type_str.upper().split("(")[0]
    return {"BOOL": "BOOL", "INT": "INT", "UINT": "UINT", "DINT": "DINT", "UDINT": "UDINT", "REAL": "REAL",
            "LREAL": "LREAL", "STRING": "STRING", "TIME": "TIME", "BYTE": "BYTE", "WORD": "WORD", "DWORD": "DWORD",
            "SINT": "SINT", "USINT": "USINT", "LINT": "LINT", "ULINT": "ULINT", "LWORD": "LWORD"}.get(up)


def _var_name_for(type_str: str) -> str:
    up = type_str.upper().split("(")[0]
    return {"BOOL": "b", "REAL": "r", "LREAL": "r", "STRING": "s", "TIME": "t"}.get(up, "n")
