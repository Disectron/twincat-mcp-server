"""Capture profiles: a curated, named selection of PLC variables to record, with a mode per
variable, reusable across sessions and batches.

Why: ``rec_start`` takes symbols / a prefix / a pattern per call, so "which variables do we
sync" is re-decided every time, by whoever (or whatever) is typing. A profile fixes the
decision once — chosen from a scan of what exists — and ``rec_profile_start`` replays it.

Three ways to choose:

1. **Checklist.** ``rec_scan`` enumerates leaves (from the running PLC, or from the source via
   ``proj_variables``) and writes a CSV with one row per variable: ``select,symbol,type,size,
   suggested_mode,mode,cycle_ms,group``. Put an ``x`` in ``select`` (and optionally override
   ``mode``), then ``rec_profile_save name from_checklist=<file>``.
2. **In the source.** ``{attribute 'tcmcp.record' := 'cycle'}`` (or ``'change'``, ``'poll'``,
   ``'off'``) on a declaration selects that variable — on an FB/STRUCT instance it selects the
   whole subtree, and an inner ``'off'`` carves holes. ``{attribute 'tcmcp.cycle_ms' := '1'}`` sets
   the rate. ``rec_profile_save name from_attributes=true`` collects them through the project
   index, so the selection is versioned with the code. TwinCAT ignores unknown attributes.
3. **Explicit.** ``rec_profile_save name symbols=[...]``.

Suggested modes are a heuristic, not a standard: BOOL / integers / enums → ``change`` (edge-
accurate, cheap), REAL/LREAL/TIME → ``cycle`` (they change every cycle anyway, so a change
notification costs the same and loses nothing), STRING/WSTRING/opaque → ``poll``. The cost
column estimates bytes per second at the chosen rate; on a CX9020 that number is what you
compare against ``ads_task_stats`` before trusting a recording's timing.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

MODES = ("cycle", "change", "poll")
ATTR_RECORD = "tcmcp.record"
ATTR_CYCLE = "tcmcp.cycle_ms"
CHECKLIST_COLUMNS = ["select", "symbol", "type", "size", "suggested_mode", "mode", "cycle_ms", "group"]
_TRUE = {"x", "1", "y", "yes", "true", "on"}

_INT_RE = re.compile(r"^(BOOL|BIT|BYTE|WORD|DWORD|LWORD|U?S?INT|U?D?INT|U?LINT|E_\w+|DT|DATE|TOD|TIME_OF_DAY|DATE_AND_TIME)$", re.I)
_REAL_RE = re.compile(r"^(REAL|LREAL|TIME|LTIME)$", re.I)
_STR_RE = re.compile(r"^(W?STRING(\(.*\))?|POINTER TO.*|REFERENCE TO.*|__XINT|__UXINT|__XWORD)$", re.I)


class ProfileError(Exception):
    pass


@dataclass
class ProfileItem:
    symbol: str
    mode: str = "change"
    cycle_ms: float | None = None     # None → profile default
    type: str | None = None
    size: int | None = None


@dataclass
class CaptureProfile:
    name: str
    items: list[ProfileItem] = field(default_factory=list)
    description: str | None = None
    cycle_ms: float = 10.0
    start_when: str | None = None
    stop_when: str | None = None
    pre_trigger_ms: float = 0.0
    duration_s: float | None = None
    source: str = "explicit"          # explicit | checklist | attributes
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)

    def groups(self) -> dict[tuple[str, float], list[ProfileItem]]:
        """Items grouped by (mode, cycle_ms) — one recording per group."""
        out: dict[tuple[str, float], list[ProfileItem]] = {}
        for it in self.items:
            key = (it.mode, float(it.cycle_ms if it.cycle_ms is not None else self.cycle_ms))
            out.setdefault(key, []).append(it)
        return out

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["count"] = len(self.items)
        d["groups"] = [{"mode": m, "cycle_ms": c, "symbols": len(v), "bytes_per_s": sum(estimate_bytes_per_s(i.size or 8, m, c) for i in v)}
                       for (m, c), v in self.groups().items()]
        return d


# ---------------------------------------------------------------------------
# heuristics
# ---------------------------------------------------------------------------
def suggest_mode(type_str: str) -> str:
    t = (type_str or "").strip()
    if _STR_RE.match(t) or t.upper().startswith("ARRAY"):
        return "poll"
    if _REAL_RE.match(t):
        return "cycle"
    if _INT_RE.match(t):
        return "change"
    return "change"


def estimate_bytes_per_s(size: int, mode: str, cycle_ms: float) -> float:
    """Upper bound: cycle and change (worst case: changes every cycle) send size + ~16 B header
    per sample; poll reads the raw bytes once per interval."""
    rate = 1000.0 / max(cycle_ms, 0.1)
    if mode == "poll":
        return size * rate
    return (size + 16) * rate


def group_of(symbol: str) -> str:
    """Grouping key for the checklist: program + first instance ('MAIN.fbDispense')."""
    parts = re.split(r"[.\[]", symbol)
    return ".".join(p for p in parts[:2] if p)


# ---------------------------------------------------------------------------
# checklist
# ---------------------------------------------------------------------------
def build_checklist(leaves: list[dict[str, Any]], default_cycle_ms: float = 10.0,
                    preselect: list[str] | None = None) -> str:
    """CSV text for the user to mark up. leaves: [{path, type, size?}]. preselect: fnmatch
    patterns that get an 'x' already. mode/cycle_ms are left empty: empty mode = suggested_mode,
    empty cycle_ms = the profile's default, so a row only pins a rate when someone typed one."""
    import fnmatch
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CHECKLIST_COLUMNS)
    pre = [p.lower() for p in (preselect or [])]
    for lf in leaves:
        sym = lf["path"]
        sel = "x" if any(fnmatch.fnmatchcase(sym.lower(), p) for p in pre) else ""
        sm = suggest_mode(lf.get("type", ""))
        w.writerow([sel, sym, lf.get("type", ""), lf.get("size", ""), sm, "", "", group_of(sym)])   # empty = profile default
    return buf.getvalue()


def parse_checklist(text: str, default_cycle_ms: float = 10.0) -> list[ProfileItem]:
    rdr = csv.DictReader(io.StringIO(text))
    missing = {"select", "symbol"} - set(rdr.fieldnames or [])
    if missing:
        raise ProfileError(f"checklist lacks columns {sorted(missing)}; header must be {CHECKLIST_COLUMNS}")
    items: list[ProfileItem] = []
    seen: set[str] = set()
    for row in rdr:
        if (row.get("select") or "").strip().lower() not in _TRUE:
            continue
        sym = (row.get("symbol") or "").strip()
        if not sym or sym.lower() in seen:
            continue
        seen.add(sym.lower())
        mode = (row.get("mode") or "").strip().lower() or (row.get("suggested_mode") or "").strip().lower() or "change"
        if mode not in MODES:
            raise ProfileError(f"{sym}: mode must be one of {MODES}, got {mode!r}")
        cyc = (row.get("cycle_ms") or "").strip()
        try:
            cycle = float(cyc) if cyc else None
        except ValueError as exc:
            raise ProfileError(f"{sym}: cycle_ms {cyc!r} is not a number") from exc
        size = (row.get("size") or "").strip()
        items.append(ProfileItem(sym, mode, cycle, (row.get("type") or "").strip() or None, int(size) if size.isdigit() else None))
    if not items:
        raise ProfileError("no row has select=x")
    return items


# ---------------------------------------------------------------------------
# attributes in source
# ---------------------------------------------------------------------------
def items_from_attributes(proj_items: list[dict[str, Any]], default_cycle_ms: float = 10.0) -> list[ProfileItem]:
    """proj_variables items (with 'attrs' already merged down the instance tree) → selected leaves.
    'off' on an inner declaration overrides an outer 'on'/'cycle' because the merge is inner-wins."""
    out: list[ProfileItem] = []
    for it in proj_items:
        attrs = it.get("attrs") or {}
        if ATTR_RECORD not in attrs:
            continue
        val = (attrs.get(ATTR_RECORD) or "").strip().lower()
        if val in ("off", "false", "0", "no"):
            continue
        mode = val if val in MODES else suggest_mode(it.get("type", ""))
        cyc = attrs.get(ATTR_CYCLE)
        try:
            cycle = float(cyc) if cyc else None
        except ValueError as exc:
            raise ProfileError(f"{it['path']}: {ATTR_CYCLE}={cyc!r} is not a number") from exc
        if "[*]" in it["path"]:
            continue   # arrays beyond max_array are not enumerable here; select them explicitly
        out.append(ProfileItem(it["path"], mode, cycle, it.get("type")))
    return out


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------
class ProfileStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-\.]{1,64}", name):
            raise ProfileError("profile name: letters, digits, _ - . only (max 64)")
        return self.root / f"{name}.json"

    def checklist_path(self, name: str) -> Path:
        self._path(name)
        return self.root / f"{name}.checklist.csv"

    def list(self) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                out.append({"name": d["name"], "count": len(d.get("items", [])), "description": d.get("description"),
                            "source": d.get("source"), "updated": d.get("updated"),
                            "modes": sorted({i["mode"] for i in d.get("items", [])})})
            except (OSError, ValueError, KeyError):
                out.append({"name": p.stem, "error": "unreadable"})
        return out

    def get(self, name: str) -> CaptureProfile:
        p = self._path(name)
        if not p.is_file():
            raise ProfileError(f"no profile {name!r}; rec_profile_list shows what exists")
        d = json.loads(p.read_text(encoding="utf-8"))
        items = [ProfileItem(**{k: v for k, v in i.items() if k in ProfileItem.__dataclass_fields__}) for i in d.pop("items", [])]
        d.pop("count", None)
        d.pop("groups", None)
        return CaptureProfile(items=items, **{k: v for k, v in d.items() if k in CaptureProfile.__dataclass_fields__})

    def save(self, prof: CaptureProfile, replace: bool = True) -> Path:
        p = self._path(prof.name)
        if p.exists() and not replace:
            raise ProfileError(f"profile {prof.name!r} exists; pass replace=true")
        if not prof.items:
            raise ProfileError("a profile needs at least one symbol")
        for it in prof.items:
            if it.mode not in MODES:
                raise ProfileError(f"{it.symbol}: mode must be one of {MODES}")
        prof.updated = time.time()
        if p.exists():
            try:
                prof.created = json.loads(p.read_text(encoding="utf-8")).get("created", prof.created)
            except (OSError, ValueError):
                pass
        p.write_text(json.dumps(prof.to_dict(), indent=1), encoding="utf-8")
        return p

    def delete(self, name: str) -> bool:
        p = self._path(name)
        existed = p.exists()
        if existed:
            p.unlink()
        cl = self.checklist_path(name)
        if cl.exists():
            cl.unlink()
        return existed
