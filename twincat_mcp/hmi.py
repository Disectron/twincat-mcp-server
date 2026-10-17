"""TwinCAT HMI binding index.

TE2000 (TF2000 web HMI) projects are HTML/JSON: ``*.view``, ``*.content``,
``*.usercontrol`` carry control attributes like
``data-tchmi-value="%s%PLC1.MAIN.fbFill.bDone%/s%"``; ``*.json`` files
(symbol lists, configs) and themes may reference symbols the same way.
TF1800 ``.TcVIS`` (classic PLC HMI) is an XML archive: element properties are
``<VisualElemMember Id="…L">`` nodes whose text is an instance path (read
bindings, kind ``tcvis``), and ST input actions (``STSnippetInputAction``)
carry ``x := …`` assignments (write bindings, kind ``tcvis_write``).

Index → every symbol binding with file, control id/type and attribute, so the
agent can answer "which screens show this variable", "which bindings are
broken against the runtime", "which PLC symbols have no HMI".
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

HMI_EXT = {".view", ".content", ".usercontrol", ".tpl", ".html", ".htm", ".json", ".tcvis"}
_RE_SYM = re.compile(r"%s%\s*([^%]+?)\s*%/s%")
_RE_INTERNAL = re.compile(r"%i%\s*([^%]+?)\s*%/i%")
_RE_CTRL = re.compile(r"<(?P<tag>[a-zA-Z][\w-]*)\b(?P<attrs>[^>]*)>", re.S)
_RE_ATTR = re.compile(r"([\w:-]+)\s*=\s*\"([^\"]*)\"", re.S)
_RE_PATH = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[\d+\])+)\b")


@dataclass
class Binding:
    symbol: str          # as written, e.g. PLC1.MAIN.fbFill.bDone
    plc_symbol: str      # without the server prefix, e.g. MAIN.fbFill.bDone
    file: str
    line: int
    control_id: str | None
    control_type: str | None
    attribute: str | None
    kind: str            # symbol | internal | json | tcvis


class HmiIndex:
    def __init__(self) -> None:
        self.root: Path | None = None
        self.bindings: list[Binding] = []
        self.files: int = 0
        self.scanned_at: float | None = None
        self.servers: set[str] = set()

    def scan(self, root: str) -> dict[str, Any]:
        self.root = Path(root)
        if not self.root.is_dir():
            raise ValueError(f"HMI root not a directory: {root}")
        self.bindings.clear()
        self.servers.clear()
        n = 0
        for p in self.root.rglob("*"):
            if p.suffix.lower() not in HMI_EXT or not p.is_file():
                continue
            if any(part.lower() in ("node_modules", "bin", "obj", "packages", "lib", ".git", "framework") for part in p.parts):
                continue
            try:
                txt = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            n += 1
            if p.suffix.lower() == ".tcvis":
                self._scan_tcvis(p, txt)
            elif p.suffix.lower() == ".json":
                self._scan_json(p, txt)
            else:
                self._scan_html(p, txt)
        self.files = n
        self.scanned_at = time.time()
        syms = {b.plc_symbol.lower() for b in self.bindings}
        return {"root": str(self.root), "files": n, "bindings": len(self.bindings), "distinct_symbols": len(syms),
                "servers": sorted(self.servers),
                "by_file": _count(b.file for b in self.bindings)}

    # --------------------------------------------------------------- parsers
    def _add(self, sym: str, p: Path, line: int, cid: str | None, ctype: str | None, attr: str | None, kind: str) -> None:
        sym = sym.strip()
        parts = sym.split(".")
        plc = sym
        if len(parts) > 1 and (parts[0].upper().startswith("PLC") or parts[0].lower() in ("plc1", "plc2", "main_plc")):
            self.servers.add(parts[0])
            plc = ".".join(parts[1:])
        self.bindings.append(Binding(sym, plc, str(p), line, cid, ctype, attr, kind))

    def _scan_html(self, p: Path, txt: str) -> None:
        for m in _RE_CTRL.finditer(txt):
            attrs = dict(_RE_ATTR.findall(m.group("attrs")))
            cid = attrs.get("id")
            ctype = attrs.get("data-tchmi-type")
            line = txt[:m.start()].count("\n") + 1
            for name, val in attrs.items():
                for s in _RE_SYM.findall(val):
                    self._add(s, p, line, cid, ctype, name, "symbol")
                for s in _RE_INTERNAL.findall(val):
                    self._add(s, p, line, cid, ctype, name, "internal")
        # bindings in <script> or inline JS
        for m in _RE_SYM.finditer(txt):
            pass  # attribute pass already caught these; scripts rarely use %s% literally

    def _scan_json(self, p: Path, txt: str) -> None:
        for m in _RE_SYM.finditer(txt):
            self._add(m.group(1), p, txt[:m.start()].count("\n") + 1, None, None, None, "json")
        # symbol lists: {"symbols": {"MAIN.x": {...}}} or arrays of names
        try:
            doc = json.loads(txt)
        except json.JSONDecodeError:
            return
        for key in _walk_keys(doc):
            if _RE_PATH.fullmatch(key) and "." in key and not key.startswith("%"):
                self._add(key, p, 0, None, None, "json-key", "json")

    # TF1800 member ids seen in the wild (TcVIS stores element properties as <VisualElemMember Id="…L">)
    _TCVIS_MEMBER = {"2477733581": "text_variable", "1999528970": "toggle_color_variable", "743958181": "lamp_variable"}

    def _scan_tcvis(self, p: Path, txt: str) -> None:
        """TF1800 .TcVIS: every <VisualElemMember Id="…"> whose text (or a descendant's) is an instance path is a
        read binding, tagged with the member id (and a name when known); ST snippets of input actions
        (STSnippetInputAction) are scanned for ``x := …`` assignments, which are *write* bindings."""
        import xml.etree.ElementTree as ET
        found = 0
        try:
            root = ET.fromstring(txt.encode("utf-8") if isinstance(txt, str) else txt)
        except ET.ParseError:
            root = None
        if root is not None:
            # line numbers are not available from ET; locate by text search
            def line_of(needle: str) -> int:
                i = txt.find(needle)
                return txt[:i].count("\n") + 1 if i >= 0 else 0

            def is_snippet(e) -> bool:
                t = e.tag.split("}")[-1]
                return "STSnippet" in t or "InputAction" in t or t.endswith("Snippet")

            seen: set[tuple[str, str, str]] = set()

            def add_once(sym: str, ctype: str, attr: str, kind: str, line: int) -> None:
                key = (sym.lower(), attr, kind)
                if key in seen:
                    return
                seen.add(key)
                self._add(sym, p, line or line_of(sym), None, ctype, attr, kind)

            for el in root.iter():
                tag = el.tag.split("}")[-1]
                elem_id = el.get("Id") or el.get("id")
                if tag == "VisualElemMember":
                    mid = (elem_id or "").rstrip("L")
                    mname = self._TCVIS_MEMBER.get(mid, f"member:{mid}" if mid else "member")
                    texts = [t for t in el.itertext() if t and t.strip()]
                    for t in texts:
                        for cand in _RE_PATH.findall(t):
                            if "." in cand and not cand.replace(".", "").isdigit():
                                self._add(cand, p, line_of(cand), None, mname, "VisualElemMember", "tcvis")
                                found += 1
                elif is_snippet(el) and not any(is_snippet(c) for c in el.iter() if c is not el):
                    # innermost snippet element only (InputActions > STSnippetInputAction > Code all carry the same text)
                    code = " ".join(t for t in el.itertext() if t)
                    code_line = line_of(code.strip()[:60]) if code.strip() else 0
                    for stmt in code.split(";"):
                        lhs, sep, rhs = stmt.partition(":=")
                        if sep:
                            for m in re.finditer(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[\d+\])*)\s*$", lhs.strip()):
                                add_once(m.group(1), "st_input_action", "write", "tcvis_write", code_line)
                                found += 1
                            reads = rhs
                        else:
                            reads = stmt
                        for cand in _RE_PATH.findall(reads):
                            if "." in cand and not cand.replace(".", "").isdigit():
                                add_once(cand, "st_input_action", "read", "tcvis", code_line)
        if not found:
            # older/other layouts: fall back to the text scan
            for m in re.finditer(r"<(Variable|Expression|Text|Tooltip)[^>]*>([^<]{1,200})</\1>", txt):
                val = m.group(2).strip()
                if _RE_PATH.fullmatch(val):
                    self._add(val, p, txt[:m.start()].count("\n") + 1, None, m.group(1), None, "tcvis")

    # --------------------------------------------------------------- queries
    def _ensure(self) -> None:
        if self.scanned_at is None:
            raise ValueError("no HMI bindings indexed: hmi_scan has not run yet, or it scanned a folder with no .view/.content/.json/.TcVIS "
                             "binding files (hmi_scan reports files and bindings — check project.hmi_root points at the TE2000 project "
                             "or at the PLC project folder holding the .TcVIS visualizations)")

    def find(self, pattern: str | None, kind: str | None = None, limit: int = 500) -> dict[str, Any]:
        self._ensure()
        import fnmatch
        pat = (pattern or "*").lower()
        if "*" not in pat and "?" not in pat:
            pat = f"*{pat}*"
        sel = [b for b in self.bindings if fnmatch.fnmatchcase(b.plc_symbol.lower(), pat) or fnmatch.fnmatchcase(b.symbol.lower(), pat)]
        if kind:
            sel = [b for b in sel if b.kind == kind]
        return {"total": len(sel), "bindings": [asdict(b) for b in sel[:limit]]}

    def symbols(self) -> list[str]:
        self._ensure()
        return sorted({b.plc_symbol for b in self.bindings if b.kind in ("symbol", "json", "tcvis")}, key=str.lower)

    def compare(self, online: set[str], declared: set[str] | None = None) -> dict[str, Any]:
        """Bound symbols vs online leaves (lower-case paths)."""
        self._ensure()
        bound = {}
        for b in self.bindings:
            if b.kind in ("symbol", "json", "tcvis"):
                bound.setdefault(b.plc_symbol.lower(), b)
        broken = []
        for k, b in bound.items():
            base = re.sub(r"\[.*?\]", lambda m: m.group(0), k)
            if k in online or any(o.startswith(k + ".") or o.startswith(k + "[") for o in online):
                continue
            broken.append({"symbol": b.plc_symbol, "file": b.file, "line": b.line, "control_id": b.control_id,
                           "declared_in_source": bool(declared and (k in declared or any(d.startswith(k + ".") for d in declared)))})
        unbound = []
        if declared:
            unbound = sorted(d for d in declared if d not in bound and not any(d.startswith(k + ".") or d.startswith(k + "[") for k in bound))
        return {"bound_symbols": len(bound), "broken_bindings": broken[:500], "broken": len(broken),
                "declared_without_hmi": unbound[:500] if declared else None}


def _walk_keys(doc: Any):
    if isinstance(doc, dict):
        for k, v in doc.items():
            yield k
            yield from _walk_keys(v)
    elif isinstance(doc, list):
        for v in doc:
            yield from _walk_keys(v)


def _count(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1])[:50])
