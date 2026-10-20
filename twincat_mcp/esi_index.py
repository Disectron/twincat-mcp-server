"""The ESI index of the knowledge folder: <kb>\\index\\esi.db — every EtherCAT device description on this PC, read
once, for hw_lookup / hw_search / hw_object / hw_diag and the twincat-docs skill (docs/KNOWLEDGE_BASE.md).

Sources (both, one index):
  * <kb>\\manuals\\** — every *.xml whose root element is an ESI document (the user's copy of Beckhoff's ESI folder,
    e.g. manuals\\Beckhoff_EtherCAT_XML, sub-folders included); the folder is the user's: read, never written;
  * TwinCAT's own ESI folders on this PC (Config\\Io\\EtherCAT of the installed TwinCAT) and knowledge.esi_dirs.
  A file found twice (same content) is read once. A device revision (type, product code, revision) described by
  several files is stored once: the description from the file with the highest vendor FileVersion (then the newer
  file) is kept, the others are named — also_in when they describe it identically, differs_in when not.

What is read — four document kinds, told apart by their root element:
  EtherCATInfo   devices: identity (type, product code, revision, names, group, vendor), E-bus current, URLs, the
                 terminal picture in RES\\ when there is one, mailbox protocols, sync managers, FMMUs, DC operation
                 modes, PDOs with every entry (gaps kept — they occupy bits; Beckhoff's PDO templates
                 <TxPdo Ref=… Chn=…> resolved per channel), the predefined PDO assignments (AlternativeSmMapping, with
                 their init commands), slots of modular devices, the CoE object dictionary (objects, sub-indices with
                 type, bit offset, access, PDO mapping, defaults decoded, limits, unit code, enumeration texts; array
                 elements one by one), inline diagnosis messages, and the DictionaryFile / DiagFile it names — of
                 every Profile of the device (several are joined; an object or message the profiles or files give
                 differently is named in dictionary_conflicts / diag_conflicts, the first one's is kept); the
                 modules of Descriptions\\Modules;
  EtherCATModule modules of modular devices (ModuleIdent, PDOs, dictionary, diagnosis messages);
  EtherCATDict   dictionaries in their own file (AX5000: the drive parameters);
  EtherCATDiag   diagnosis message catalogues (text id, message, description, causes and remedies, reaction, reset).
Identical objects, dictionaries and diagnosis messages (most revisions share them) are stored once; those only a
replaced description used are dropped before the index is complete (the counts are of what is kept). Devices,
objects, diagnosis messages and modules are full-text searchable — a module also by the devices whose slots take it
(their type, name and group: AX8000 modules are named DeviceMain, SynchronousMotor …). EtherCATInfo / EtherCATModule
files are read element by element (each device dropped once read); a dictionary or diagnosis file is read whole (the
AX5000 dictionaries: 1.6 MB).

Build discipline (as the other indexes): one build at a time (an OS lock on <db>.lock with the progress), written as
<db>.building and swapped in when complete — the index in use stays as it is until then; an interrupted build
replaces nothing; a finished build that could not replace the index (it was open) replaces it at the next build;
a work file left next to a complete, current index (stale) is discarded by the next build.
Readers open the file read-only. FORMAT is the stored layout and the reading rules: an index of another format stays
in use and is reported rebuild_needed until the user approves the rebuild. tests/test_formats.py fingerprints the
code behind it.

Standard library only: the server, the builder (skills/twincat-docs/builders/esi_db.py) and the docs skill import it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import struct
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from . import kb_lock

FORMAT = "2"            # 1: 0.8.12; 2: a module's search text holds the devices whose slots take it (N156, 0.8.14 retest)
ROOT_KINDS = {"EtherCATInfo": "info", "EtherCATModule": "module", "EtherCATDict": "dict", "EtherCATDiag": "diag"}
SIDE = ("-journal", "-wal", "-shm")
SWAP_WAIT_S = 60.0
_LCID_EN, _LCID_DE = "1033", "1031"


class EsiError(RuntimeError):
    """code: 'bad_param' (a value the call cannot use), 'empty_query', 'not_found', 'no_index'."""

    def __init__(self, message: str, code: str = "bad_param"):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------------------------- sources
def twincat_esi_folders(extra: Iterable[str | Path] = ()) -> list[Path]:
    """TwinCAT's ESI folders on this PC (3.1.4024: C:\\TwinCAT\\3.1\\Config\\Io\\EtherCAT; 4026: under Program Files
    (x86)\\Beckhoff), and the extra folders — the ones that exist."""
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    pd = os.environ.get("ProgramData") or r"C:\ProgramData"
    cands = [Path(d) for d in extra] + [
        Path(r"C:\TwinCAT\3.1\Config\Io\EtherCAT"),
        Path(pf86) / "Beckhoff" / "TwinCAT" / "3.1" / "Config" / "Io" / "EtherCAT",
        Path(pd) / "Beckhoff" / "TwinCAT" / "3.1" / "Config" / "Io" / "EtherCAT",
    ]
    seen: set[str] = set()
    out: list[Path] = []
    for c in cands:
        k = os.path.normcase(str(c))
        if k not in seen and c.is_dir():
            seen.add(k)
            out.append(c)
    return out


def source_folders(kb: Path | None, extra: Iterable[str | Path] = (), twincat: bool = True) -> list[tuple[Path, str]]:
    """(folder, source): the knowledge folder's manuals\\ first (the user's copy), then TwinCAT's folders and the
    extra ones (twincat=False: only the extra ones — tests, or a build from given folders)."""
    out: list[tuple[Path, str]] = []
    if kb is not None and (Path(kb) / "manuals").is_dir():
        out.append((Path(kb) / "manuals", "knowledge"))
    dirs = twincat_esi_folders(extra) if twincat else [Path(x) for x in extra if Path(x).is_dir()]
    out.extend((d, "twincat") for d in dirs)
    return out


def xml_files(folders: list[tuple[Path, str]]) -> list[tuple[Path, str]]:
    """Every *.xml below the folders, sub-folders included: (file, source) — sorted per folder, each file once."""
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    for folder, src in folders:
        for p in sorted(folder.rglob("*")):
            if p.suffix.lower() == ".xml" and p.is_file():
                k = os.path.normcase(str(p.resolve()))
                if k not in seen:
                    seen.add(k)
                    out.append((p, src))
    return out


def has_esi(files: list[tuple[Path, str]]) -> bool:
    """Is one of the files an ESI document (its root element; only the start of a file is read)? An .xml that is
    not (a project export, a config file) starts no first build (review 0.8.12)."""
    return any(root_kind(p) is not None for p, _s in files)


def sources_stamp(files: list[tuple[Path, str]]) -> str:
    """What the index is built from: every candidate file's path, size and modification time."""
    h = hashlib.sha256()
    for p, src in files:
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(f"{src}|{p}|{st.st_size}|{st.st_mtime_ns}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def root_kind(path: Path) -> str | None:
    """'info' / 'module' / 'dict' / 'diag' from the root element (only the start of the file is read), else None."""
    try:
        for _ev, el in ET.iterparse(path, events=("start",)):
            return ROOT_KINDS.get(_ln(el.tag))
    except (ET.ParseError, OSError, UnicodeDecodeError):
        return None
    return None


def _content_hash(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _has_templates(path: Path) -> bool:
    """Does the file use Beckhoff's PDO templates (Descriptions\\Pdos\\Pdo[@RefName])?"""
    with open(path, "rb") as fh:
        tail = b""
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            if b"RefName" in tail + chunk:
                return True
            tail = chunk[-16:]
    return False


# --------------------------------------------------------------------------------------------- XML helpers
def _ln(tag: Any) -> str:
    """Local name of a tag ('{ns}TxPdo' -> 'TxPdo'); ESI files rarely use a namespace, so that is the fast path."""
    if tag.__class__ is not str:
        return ""                                    # a comment or processing instruction
    return tag if tag[:1] != "{" else tag.rsplit("}", 1)[-1]


def _kids(el: Any, name: str) -> list[Any]:
    return [c for c in el if _ln(c.tag) == name] if el is not None else []


def _kid(el: Any, name: str) -> Any:
    return next((c for c in el if _ln(c.tag) == name), None) if el is not None else None


def _path(el: Any, *names: str) -> Any:
    for n in names:
        el = _kid(el, n)
        if el is None:
            return None
    return el


def _txt(el: Any) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _pick_lang(cs: list[Any], lcid: str) -> Any:
    """English: LcId 1033, else the one without LcId, else the first. Any other language: only that LcId."""
    if not cs:
        return None
    best = next((c for c in cs if c.get("LcId") == lcid), None)
    if best is None and lcid == _LCID_EN:
        best = next((c for c in cs if c.get("LcId") is None), cs[0])
    return best


def _lang(el: Any, name: str, lcid: str = _LCID_EN) -> str:
    return _txt(_pick_lang(_kids(el, name), lcid))


def _num(v: str | None) -> int | None:
    """#x1A00 / 0x1A00 / decimal (leading zeros allowed: '0410') -> int, else None."""
    if v is None:
        return None
    s = v.strip()
    try:
        if s[:2] in ("#x", "#X", "0x", "0X"):
            return int(s[2:], 16)
        neg = s.startswith("-")
        d = s[1:] if neg or s.startswith("+") else s
        if not d.isdigit():
            return None
        return -int(d) if neg else int(d)
    except ValueError:
        return None


def _hx(n: int | None, width: int = 4) -> str | None:
    return None if n is None else f"#x{n:0{width}X}"


def _chn(el: Any, chn: int) -> str:
    """Text with Beckhoff's channel factor applied: <Index ChnFac="1">#x1a00</Index> of channel n is #x1a00 +
    n·ChnFac; <Name ChnFac="1">Channel #1</Name> gives 'Channel {1 + n·ChnFac}' (a placeholder, also for channel 0)."""
    t = _txt(el)
    if el is None:
        return t
    fac = _num(el.get("ChnFac"))
    if fac is None:
        return t
    k = fac * chn
    n = _num(t)
    if n is not None:
        return f"#x{n + k:04x}" if t[:2] in ("#x", "#X") else str(n + k)
    return re.sub(r"#(\d+)", lambda m: str(int(m.group(1)) + k), t)


def _clang(el: Any, name: str, chn: int) -> str:
    best = _pick_lang(_kids(el, name), _LCID_EN)
    return _chn(best, chn) if best is not None else ""


def _flag(el: Any, name: str) -> bool:
    return _txt(_kid(el, name)) in ("1", "true")


def _pack(v: Any) -> bytes:
    return zlib.compress(json.dumps(v, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"), 6)


def _unpack(b: bytes | None) -> Any:
    return json.loads(zlib.decompress(b).decode("utf-8")) if b else None


def _hash(v: Any) -> str:
    return hashlib.sha1(json.dumps(v, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------------------- values
_INT_TYPES = {"BOOL": (1, False), "BIT": (1, False), "BYTE": (1, False), "USINT": (1, False), "UINT8": (1, False),
              "SINT": (1, True), "INT8": (1, True), "WORD": (2, False), "UINT": (2, False), "UINT16": (2, False),
              "INT": (2, True), "INT16": (2, True), "UINT24": (3, False), "INT24": (3, True), "DWORD": (4, False),
              "UDINT": (4, False), "UINT32": (4, False), "DINT": (4, True), "INT32": (4, True), "ULINT": (8, False),
              "LWORD": (8, False), "UINT64": (8, False), "LINT": (8, True), "INT64": (8, True)}
_FLOAT_TYPES = {"REAL": "<f", "FLOAT": "<f", "REAL32": "<f", "LREAL": "<d", "REAL64": "<d"}


def decode_data(hexdata: str, type_name: str | None, enum: dict[str, str] | None = None) -> Any:
    """An ESI DefaultData (the default's bytes, little-endian) as a value of its type — None when the type is not an
    elementary one (the bytes are kept beside it as default_raw)."""
    try:
        b = bytes.fromhex(hexdata.strip())
    except (ValueError, AttributeError):
        return None
    t = (type_name or "").strip().upper()
    if t.startswith("STRING") or t == "VISIBLE_STRING":
        return b.split(b"\x00", 1)[0].decode("cp1252", "replace")
    if t in _FLOAT_TYPES:
        size = struct.calcsize(_FLOAT_TYPES[t])
        return struct.unpack(_FLOAT_TYPES[t], b[:size])[0] if len(b) >= size else None
    m = re.fullmatch(r"BIT(\d+)", t)
    if m or t in _INT_TYPES:
        size, signed = (_INT_TYPES[t] if t in _INT_TYPES else ((int(m.group(1)) + 7) // 8, False))
        if len(b) < size:
            return None
        v = int.from_bytes(b[:size], "little", signed=signed)
        if t in ("BOOL", "BIT"):
            return bool(v & 1)
        return {"value": v, "text": enum[str(v)]} if enum and str(v) in enum else v
    return None


# --------------------------------------------------------------------------------------------- dictionary
def parse_dictionary(dic: Any) -> list[dict[str, Any]] | None:
    """A <Dictionary> (inline in a device or module, or an EtherCATDict file) -> its objects, every sub-index resolved
    through the object's data type: an array's elements one by one (sub-index, type, bit offset), the defaults of
    Info\\SubItem by name, enumeration types as their texts."""
    if dic is None:
        return None
    types: dict[str, dict[str, Any]] = {}
    for dt in _kids(_kid(dic, "DataTypes"), "DataType"):
        name = _txt(_kid(dt, "Name"))
        if not name:
            continue
        arr = _kid(dt, "ArrayInfo")
        types[name.upper()] = {
            "base": _txt(_kid(dt, "BaseType")) or None, "bits": _num(_txt(_kid(dt, "BitSize"))),
            "array": ({"lbound": _num(_txt(_kid(arr, "LBound"))) or 0, "elements": _num(_txt(_kid(arr, "Elements")))}
                      if arr is not None else None),
            "subs": [{"sub": _num(_txt(_kid(s, "SubIdx"))), "name": _lang(s, "Name") or _txt(_kid(s, "DisplayName")),
                      "type": _txt(_kid(s, "Type")), "bits": _num(_txt(_kid(s, "BitSize"))),
                      "bit_offs": _num(_txt(_kid(s, "BitOffs"))), **_flags(_kid(s, "Flags"))}
                     for s in _kids(dt, "SubItem")],
            "enum": {str(v): _lang(e, "Text") for e in _kids(dt, "EnumInfo")
                     if (v := _num(_txt(_kid(e, "Enum")))) is not None},
        }

    def tinfo(type_name: str | None) -> dict[str, Any]:
        return types.get((type_name or "").upper()) or {}

    def elementary(type_name: str | None) -> str | None:
        """The type DefaultData is decoded as: an enumeration's base type, else the type itself."""
        t = tinfo(type_name)
        return t["base"] if t.get("enum") and t.get("base") else type_name

    def values(info: Any, type_name: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if info is None:
            return out
        enum = tinfo(type_name).get("enum") or None
        dd = _txt(_kid(info, "DefaultData"))
        if dd:
            out["default_raw"] = dd
            v = decode_data(dd, elementary(type_name), enum)
            if v is not None:
                out["default"] = v
        for k, key in (("DefaultValue", "default"), ("MinValue", "min"), ("MaxValue", "max")):
            t = _txt(_kid(info, k))
            if t:
                n = _num(t)
                out[key] = n if n is not None else t
                if key == "default" and n is not None and enum and str(n) in enum:
                    out[key] = {"value": n, "text": enum[str(n)]}
        ds = _txt(_kid(info, "DefaultString"))
        if ds:
            out["default"] = ds
        u = _txt(_kid(info, "Unit"))
        if u:
            out["unit_code"] = u                     # ETG.1004 unit code, as written (not decoded here)
        return out

    def expand(subs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """An array sub-item ('Elements' of an array type) as its elements: sub-index LBound…, each with the base
        type and its own bit offset — the way the device and the Info\\SubItem defaults number them."""
        out = []
        for s in subs:
            at = tinfo(s["type"])
            arr = at.get("array")
            if not arr or not arr.get("elements"):
                out.append(s)
                continue
            n, lb = arr["elements"], arr["lbound"]
            eb = (at.get("bits") or s.get("bits") or 0) // n if n else None
            base = s.get("bit_offs")
            for i in range(n):
                sub = lb + i
                out.append({**{k: v for k, v in s.items() if k not in ("name", "type", "bits", "bit_offs", "sub")},
                            "sub": sub, "name": f"SubIndex {sub:03d}", "type": at.get("base") or s["type"],
                            "bits": eb, "bit_offs": (base + i * eb) if base is not None and eb is not None else None,
                            "array_of": s["name"]})
        return out

    objects = []
    for o in _kids(_kid(dic, "Objects"), "Object"):
        idx = _num(_txt(_kid(o, "Index")))
        if idx is None:
            continue
        tname = _txt(_kid(o, "Type"))
        t = tinfo(tname)
        info = _kid(o, "Info")
        obj: dict[str, Any] = {
            "index": idx, "name": _lang(o, "Name"), "type": tname, "bits": _num(_txt(_kid(o, "BitSize"))),
            **_flags(_kid(o, "Flags")), **({"comment": _lang(o, "Comment")} if _lang(o, "Comment") else {}),
            **values(info, tname)}
        if t.get("enum"):
            obj["enum"] = t["enum"]
        subs = expand(t.get("subs") or [])
        if not subs and t.get("array") and t["array"].get("elements"):
            subs = expand([{"sub": None, "name": "Elements", "type": tname, "bits": t.get("bits"), "bit_offs": 0}])
        if subs:
            sis = _kids(info, "SubItem")
            by_name: dict[str, Any] = {}
            for si in sis:
                by_name.setdefault(_lang(si, "Name").lower(), si)
            rows = []
            for s in subs:
                si = by_name.get((s["name"] or "").lower())
                row = {**s, **values(_kid(si, "Info") if si is not None else None, s["type"])}
                e = tinfo(s["type"]).get("enum")
                if e:
                    row["enum"] = e
                rows.append(row)
            obj["subitems"] = rows
        objects.append(obj)
    return objects


def _flags(fl: Any) -> dict[str, Any]:
    if fl is None:
        return {}
    out: dict[str, Any] = {}
    for k, key in (("Access", "access"), ("Category", "category"), ("PdoMapping", "pdo_mapping"),
                   ("SdoAccess", "sdo_access")):
        v = _txt(_kid(fl, k))
        if v:
            out[key] = v
    acc = _kid(fl, "Access")
    if acc is not None:
        for a in ("ReadRestrictions", "WriteRestrictions"):
            if acc.get(a):
                out[a[0].lower() + a[1:].replace("Restrictions", "_restrictions")] = acc.get(a)
    for k, key in (("Backup", "backup"), ("Setting", "setting")):
        if _flag(fl, k):
            out[key] = True
    return out


# --------------------------------------------------------------------------------------------- diagnosis messages
def parse_diag_messages(parent: Any) -> list[dict[str, Any]]:
    """<DiagMessages> (an EtherCATDiag file, or inline in a profile) -> messages, with the reset / reaction / type
    numbers given as their texts."""
    if parent is None:
        return []
    enums = {k: {_num(_txt(_kid(e, "Enum"))): _lang(e, "Text") for e in _kids(parent, k)}
             for k in ("DiagReset", "DiagReaction", "DiagType")}
    out = []
    for m in _kids(parent, "DiagMessage"):
        tid = _num(_txt(_kid(m, "TextId")))
        if tid is None:
            continue
        fl = _kid(m, "Flags")
        rec: dict[str, Any] = {"text_id": tid, "message": _lang(m, "MessageText")}
        for k, key in (("Description", "description"), ("Hint", "hint"), ("Info", "info")):
            v = _lang(m, k)
            if v:
                rec[key] = v
        crs = [{"cause": _lang(cr, "Cause"), "remedy": _lang(cr, "Remedy")} for cr in _kids(m, "CauseRemedy")]
        if crs:
            rec["causes"] = crs
        if _txt(_kid(fl, "DiagClass")):
            rec["class"] = _txt(_kid(fl, "DiagClass"))
        for k, key in (("DiagReset", "reset"), ("DiagReaction", "reaction"), ("DiagType", "type")):
            v = _num(_txt(_kid(fl, k)))
            if v is not None:
                rec[key] = enums[k].get(v) or v
        out.append(rec)
    return out


# --------------------------------------------------------------------------------------------- PDOs and devices
def _slot_dep(el: Any) -> str | None:
    if el is None:
        return None
    if el.get("DependOnSlotGroup") in ("1", "true"):
        return "slot group"
    if el.get("DependOnSlot") in ("1", "true"):
        return "slot"
    return None


def _pdo(kind: str, pdo: Any, src: Any, chn: int, ref: str | None) -> dict[str, Any]:
    """One PDO: its content from `src` (the element, or the template it names) for channel `chn`; its use (Fixed,
    Mandatory, Sm, Su, Virtual) from the element in the device first. Every entry is kept — a gap (index 0) occupies
    its bits in the process image."""
    entries = []
    for e in _kids(src, "Entry"):
        idx = _num(_clang(e, "Index", chn)) or 0
        ent: dict[str, Any] = {"index": _hx(idx), "sub": _num(_clang(e, "SubIndex", chn)) or 0,
                               "bits": _num(_clang(e, "BitLen", chn)), "name": _clang(e, "Name", chn),
                               "type": _clang(e, "DataType", chn)}
        if idx == 0:
            ent["gap"] = True
        dep = _slot_dep(_kid(e, "Index"))
        if dep:
            ent["index_depends_on"] = dep
        cm = _lang(e, "Comment")
        if cm:
            ent["comment"] = cm
        entries.append(ent)

    def attr(k: str) -> str | None:
        v = pdo.get(k)
        return v if v is not None else src.get(k)
    row: dict[str, Any] = {"dir": "out" if kind == "RxPdo" else "in", "index": _hx(_num(_clang(src, "Index", chn))),
                           "name": _clang(src, "Name", chn), "sm": _num(attr("Sm")),
                           "fixed": attr("Fixed") in ("1", "true"), "mandatory": attr("Mandatory") in ("1", "true"),
                           "bits": sum(x["bits"] or 0 for x in entries), "entries": entries}
    exc = [_hx(_num(_chn(x, chn))) for x in _kids(src, "Exclude")]
    if exc:
        row["excludes"] = exc
    if attr("Virtual") in ("1", "true"):
        row["virtual"] = True
    dep = _slot_dep(_kid(src, "Index"))
    if dep:
        row["index_depends_on"] = dep              # a module's PDO: its index moves with the slot (Slots increments)
    if attr("Su") is not None:
        row["sync_unit"] = _num(attr("Su"))
    if ref:
        row["template"] = ref if not chn else f"{ref} (channel {chn})"
    if not entries:
        row["entries_note"] = ("the ESI gives this PDO no entries" + (" — it is marked Virtual: its entries are "
                                                                      "defined in the configuration" if row.get("virtual")
                                                                      else ""))
    return row


def _pdos(el: Any, templates: dict[str, Any], file_name: str) -> list[dict[str, Any]]:
    out = []
    for kind in ("RxPdo", "TxPdo"):
        for p in _kids(el, kind):
            ref = p.get("Ref")
            src = templates.get(ref) if ref else p
            if src is None:
                out.append({"dir": "out" if kind == "RxPdo" else "in", "index": None, "name": "", "sm": _num(p.get("Sm")),
                            "bits": 0, "entries": [], "unresolved": f"template {ref!r} is not in {file_name}"})
                continue
            out.append(_pdo(kind, p, src, (_num(p.get("Chn")) or 0) if ref else 0, ref))
    return out


def _mappings(dev: Any) -> list[dict[str, Any]]:
    """The predefined PDO assignments (TwinCAT's AlternativeSmMapping: 'Standard', 'Compact' …) with the PDOs per sync
    manager, the DC mode and the init commands each one sets."""
    out = []
    for a in _kids(_path(dev, "Info", "VendorSpecific", "TwinCAT"), "AlternativeSmMapping"):
        sms = []
        for sm in _kids(a, "Sm"):
            r: dict[str, Any] = {"sm": _num(sm.get("No")), "pdos": [_txt(p) for p in _kids(sm, "Pdo")]}
            if _kids(sm, "ExcludedPdo"):
                r["excluded"] = [_txt(p) for p in _kids(sm, "ExcludedPdo")]
            sms.append(r)
        om = _kid(a, "OperationMode")
        rec: dict[str, Any] = {"name": _lang(a, "Name"), "default": a.get("Default") in ("1", "true"), "sm": sms}
        if _txt(_kid(om, "DcOpModeName")):
            rec["dc_mode"] = _txt(_kid(om, "DcOpModeName"))
        init = [{"transition": _txt(_kid(ic, "Transition")), "index": _txt(_kid(ic, "Index")),
                 "sub": _txt(_kid(ic, "SubIndex")), "data": _txt(_kid(ic, "Data")),
                 **({"comment": _txt(_kid(ic, "Comment"))} if _txt(_kid(ic, "Comment")) else {})}
                for ic in _kids(om, "InitCmds")]
        if init:
            rec["init_cmds"] = init
        out.append(rec)
    return out


def _slots(el: Any) -> dict[str, Any] | None:
    """Slots of a modular device: the modules (ModuleIdent) each slot takes and the default one, how many, and the
    increments that place a module's objects and PDOs per slot (as the ESI gives them)."""
    if el is None:
        return None
    out: dict[str, Any] = dict(el.attrib)
    out["slots"] = []
    for s in _kids(el, "Slot"):
        mods = []
        for m in _kids(s, "ModuleIdent") + _kids(s, "ModuleClass"):
            r: dict[str, Any] = ({"ident": _hx(_num(_txt(m)), 8)} if _ln(m.tag) == "ModuleIdent"
                                 else {"class": _txt(m)})
            if m.get("Default") in ("1", "true"):
                r["default"] = True
            mods.append(r)
        rec = {"name": _lang(s, "Name"), **dict(s.attrib), "modules": mods}
        if out["slots"] and {k: v for k, v in out["slots"][-1].items() if k != "count"} == rec:
            out["slots"][-1]["count"] = out["slots"][-1].get("count", 1) + 1    # 95 × 'Empty Slot' as one, counted
        else:
            out["slots"].append(rec)
    return out


_RES_CACHE: dict[str, dict[str, str]] = {}


def _picture(file: Path, typ: str) -> str | None:
    """The terminal's picture beside the ESI file: RES\\<type>_standard.* (any case), when there is one."""
    res = file.parent / "RES"
    key = os.path.normcase(str(res))
    if key not in _RES_CACHE:
        try:
            _RES_CACHE[key] = {p.stem.lower(): str(p) for p in res.iterdir() if p.is_file()} if res.is_dir() else {}
        except OSError:
            _RES_CACHE[key] = {}
    return _RES_CACHE[key].get(f"{typ}_standard".lower())


Vendor = tuple[str, "int | None", "int | None"]


def _vendor(el: Any) -> Vendor:
    """(name, vendor id, FileVersion — the vendor's version of this file: which of two descriptions is newer)."""
    return _lang(el, "Name"), _num(_txt(_kid(el, "Id"))), _num(el.get("FileVersion"))


def parse_device(dev: Any, templates: dict[str, Any], file: Path, vendor: Vendor,
                 groups: dict[str, str]) -> dict[str, Any] | None:
    t = _kid(dev, "Type")
    typ = _txt(t)
    if not typ:
        return None
    profs = _kids(dev, "Profile")
    prof = profs[0] if profs else None
    gt = _txt(_kid(dev, "GroupType"))
    dic, dconf = _join([parse_dictionary(_kid(x, "Dictionary")) for x in profs], "index")
    diag, gconf = _join([parse_diag_messages(_kid(x, "DiagMessages")) for x in profs], "text_id")
    dfiles = list(dict.fromkeys(f for f in (_txt(_kid(x, "DictionaryFile")) for x in profs) if f))  # each once
    gfiles = list(dict.fromkeys(f for f in (_txt(_kid(x, "DiagFile")) for x in profs) if f))
    rec = {
        "type": typ, "name": _lang(dev, "Name"), "name_de": _lang(dev, "Name", _LCID_DE) or None,
        "product_code": _num(t.get("ProductCode")), "revision": _num(t.get("RevisionNo")),
        "vendor": vendor[0], "vendor_id": vendor[1], "file_version": vendor[2],
        "group_type": gt or None, "group_name": groups.get(gt) or None, "physics": dev.get("Physics") or None,
        "ebus_ma": _num(_txt(_path(dev, "Info", "Electrical", "EBusCurrent"))),
        "urls": {(u.get("LcId") or ""): _txt(u) for u in _kids(dev, "URL")} or None,
        "image": _picture(file, typ),
        "mailbox": sorted(_ln(c.tag) for c in _kid(dev, "Mailbox")) if _kid(dev, "Mailbox") is not None else None,
        "sm": [{"no": i, "name": _txt(s), "start": s.get("StartAddress"), "size": _num(s.get("DefaultSize")),
                "control": s.get("ControlByte"), "enable": s.get("Enable")} for i, s in enumerate(_kids(dev, "Sm"))],
        "fmmu": [_txt(f) for f in _kids(dev, "Fmmu")],
        "dc": [{"name": _txt(_kid(m, "Name")), "desc": _txt(_kid(m, "Desc")),
                "assign_activate": _txt(_kid(m, "AssignActivate")),
                **({k: _txt(_kid(m, k)) for k in ("CycleTimeSync0", "ShiftTimeSync0", "CycleTimeSync1",
                                                   "ShiftTimeSync1") if _txt(_kid(m, k))})}
               for m in _kids(_kid(dev, "Dc"), "OpMode")] or None,
        "pdos": _pdos(dev, templates, file.name),
        "mappings": _mappings(dev) or None,
        "slots": _slots(_kid(dev, "Slots")),
        "hides": [{k: v for k, v in (("type", _txt(h)), ("product_code", h.get("ProductCode")),
                                     ("revision", h.get("RevisionNo"))) if v}
                  for h in _kids(dev, "HideType")] or None,      # the older descriptions this one replaces in XAE
        "profile_no": _num(_txt(_kid(prof, "ProfileNo"))), "channels": _num(_txt(_kid(prof, "ChannelCount"))),
        "dictionary": dic,
        "dictionary_file": dfiles[0] if dfiles else None,
        "diag_file": gfiles[0] if gfiles else None,
        "diag": diag or None,
    }
    if len(profs) > 1:          # the schema allows several (a channel each); Beckhoff's files have one
        rec["profiles"] = [{"profile_no": _num(_txt(_kid(x, "ProfileNo"))), "channel_no": _num(_txt(_kid(x, "ChannelNo"))),
                            "channels": _num(_txt(_kid(x, "ChannelCount")))} for x in profs]
    if len(dfiles) > 1:
        rec["dictionary_files"] = dfiles
    if len(gfiles) > 1:
        rec["diag_files"] = gfiles
    if dconf:
        rec["dictionary_conflicts"] = [_hx(i) for i in dconf]
    if gconf:
        rec["diag_conflicts"] = [_hx(i) for i in gconf]
    return rec


def _join(parts: list[list[dict[str, Any]] | None], key: str) -> tuple[list[dict[str, Any]] | None, list[int]]:
    """Objects (key 'index') or diagnosis messages (key 'text_id') of several profiles or files as one list: each key
    once, in the order given; a key two parts give differently is returned in conflicts (the first part's is kept)."""
    parts = [x for x in parts if x]
    if not parts:
        return None, []
    if len(parts) == 1:
        return parts[0], []
    out: list[dict[str, Any]] = []
    seen: dict[Any, str] = {}
    conflicts: list[int] = []
    for part in parts:
        for o in part:
            h = _hash(o)
            if o[key] in seen:
                if seen[o[key]] != h and o[key] not in conflicts:
                    conflicts.append(o[key])
                continue
            seen[o[key]] = h
            out.append(o)
    return out, conflicts


def parse_module(mod: Any, file: Path, vendor: Vendor, templates: dict[str, Any] | None = None) -> dict[str, Any] | None:
    t = _kid(mod, "Type")
    if t is None:
        return None
    profs = _kids(mod, "Profile")
    prof = profs[0] if profs else None
    dic, dconf = _join([parse_dictionary(_kid(x, "Dictionary")) for x in profs], "index")
    diag, gconf = _join([parse_diag_messages(_kid(x, "DiagMessages")) for x in profs], "text_id")
    rec = {"type": _txt(t), "ident": _num(t.get("ModuleIdent")), "name": _lang(mod, "Name"), "vendor": vendor[0],
           "file_version": vendor[2], "profile_no": _num(_txt(_kid(prof, "ProfileNo"))),
           "pdos": _pdos(mod, templates or {}, file.name), "dictionary": dic, "diag": diag or None}
    if dconf:
        rec["dictionary_conflicts"] = [_hx(i) for i in dconf]
    if gconf:
        rec["diag_conflicts"] = [_hx(i) for i in gconf]
    return rec


def _templates(path: Path) -> dict[str, Any]:
    """Beckhoff's PDO templates (Descriptions\\Pdos\\Pdo[@RefName]) — a first pass that keeps only them."""
    out: dict[str, Any] = {}
    stack: list[Any] = []
    for ev, el in ET.iterparse(path, events=("start", "end")):
        if ev == "start":
            stack.append(el)
            continue
        stack.pop()
        parent = stack[-1] if stack else None
        name, pname = _ln(el.tag), (_ln(parent.tag) if parent is not None else "")
        if name == "Pdo" and pname == "Pdos" and el.get("RefName"):
            out[el.get("RefName")] = el
            parent.remove(el)
        elif name in ("Device", "Module") and pname in ("Devices", "Modules"):
            parent.remove(el)
    return out


def iter_info(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """('device', …) and ('module', …) of an EtherCATInfo file (Descriptions\\Devices, Descriptions\\Modules), one at
    a time: the file is read element by element and each device dropped once read — memory stays bounded whatever
    the file's size. A file that uses PDO templates (the templates follow the devices) is read twice: templates
    first."""
    templates = _templates(path) if _has_templates(path) else {}
    vendor: Vendor = ("", None, None)
    groups: dict[str, str] = {}
    stack: list[Any] = []
    for ev, el in ET.iterparse(path, events=("start", "end")):
        if ev == "start":
            stack.append(el)
            continue
        stack.pop()
        name = _ln(el.tag)
        parent = stack[-1] if stack else None
        pname = _ln(parent.tag) if parent is not None else ""
        if name == "Vendor" and pname == "EtherCATInfo":
            vendor = _vendor(el)
        elif name == "Group" and pname == "Groups":
            groups[_txt(_kid(el, "Type"))] = _lang(el, "Name")
        elif name == "Device" and pname == "Devices":
            rec = parse_device(el, templates, path, vendor, groups)
            parent.remove(el)
            if rec:
                yield "device", rec
        elif name == "Module" and pname == "Modules":
            mrec = parse_module(el, path, vendor, templates)
            parent.remove(el)
            if mrec:
                yield "module", mrec
        elif name == "Pdo" and pname == "Pdos":
            parent.remove(el)                       # a template: read in the first pass


def iter_modules(path: Path) -> Iterator[dict[str, Any]]:
    """Modules of an EtherCATModule file (Modules\\Module), one at a time."""
    vendor: Vendor = ("", None, None)
    stack: list[Any] = []
    for ev, el in ET.iterparse(path, events=("start", "end")):
        if ev == "start":
            stack.append(el)
            continue
        stack.pop()
        name = _ln(el.tag)
        parent = stack[-1] if stack else None
        pname = _ln(parent.tag) if parent is not None else ""
        if name == "Vendor" and pname == "EtherCATModule":
            vendor = _vendor(el)
        elif name == "Module" and pname == "Modules":
            rec = parse_module(el, path, vendor)
            parent.remove(el)
            if rec:
                yield rec


# --------------------------------------------------------------------------------------------- the database
SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, source TEXT, kind TEXT, size INTEGER, mtime REAL, hash TEXT,
  same_as INTEGER, items INTEGER, error TEXT);
CREATE TABLE obj (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, idx INTEGER, name TEXT, type TEXT, access TEXT,
  pdo_mapping TEXT, data BLOB);
CREATE TABLE dicts (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, objects INTEGER, origin TEXT);
CREATE TABLE dict_obj (dict_id INTEGER, idx INTEGER, obj_id INTEGER, PRIMARY KEY (dict_id, idx)) WITHOUT ROWID;
CREATE INDEX dict_obj_o ON dict_obj (obj_id);
CREATE TABLE msg (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, text_id INTEGER, message TEXT, data BLOB);
CREATE TABLE diag_sets (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, messages INTEGER, origin TEXT);
CREATE TABLE diag_msg (set_id INTEGER, text_id INTEGER, msg_id INTEGER, PRIMARY KEY (set_id, text_id)) WITHOUT ROWID;
CREATE INDEX diag_msg_t ON diag_msg (text_id);
CREATE INDEX diag_msg_m ON diag_msg (msg_id);
CREATE TABLE devices (id INTEGER PRIMARY KEY, type TEXT, name TEXT, product_code INTEGER, revision INTEGER,
  vendor TEXT, group_type TEXT, file_version INTEGER, file_id INTEGER, dict_id INTEGER, diag_set INTEGER,
  pdo_count INTEGER, pdo_entries INTEGER, objects INTEGER, also_in TEXT, differs_in TEXT, data BLOB);
CREATE INDEX devices_type ON devices (type COLLATE NOCASE);
CREATE INDEX devices_pc ON devices (product_code, revision);
CREATE INDEX devices_dict ON devices (dict_id);
CREATE INDEX devices_diag ON devices (diag_set);
CREATE TABLE modules (id INTEGER PRIMARY KEY, ident INTEGER, type TEXT, name TEXT, vendor TEXT, file_id INTEGER,
  dict_id INTEGER, diag_set INTEGER, data BLOB);
CREATE INDEX modules_ident ON modules (ident);
"""
FTS = "CREATE VIRTUAL TABLE search USING fts5(kind UNINDEXED, ref UNINDEXED, title, body, tokenize='unicode61')"


def ro_uri(db: Path) -> str:
    return Path(db).resolve().as_uri() + "?mode=ro"


def connect_ro(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(ro_uri(db), uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def side_files(final: Path) -> tuple[Path, Path]:
    final = Path(final)
    return final.with_name(final.name + ".building"), final.with_name(final.name + ".lock")


def read_meta(db: Path) -> dict[str, str]:
    """meta of an index file, read-only; {} when it is missing or not readable."""
    if not Path(db).is_file():
        return {}
    try:
        con = sqlite3.connect(ro_uri(Path(db)), uri=True, timeout=5)
        try:
            return {str(r[0]): str(r[1]) for r in con.execute("SELECT k, v FROM meta")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class _Writer:
    """Objects, dictionaries, diagnosis messages and their sets — each stored once, by content."""

    def __init__(self, con: sqlite3.Connection, fts: bool):
        self.con, self.fts = con, fts
        self.objs: dict[str, int] = {}
        self.dicts: dict[str, int] = {}
        self.msgs: dict[str, int] = {}
        self.sets: dict[str, int] = {}
        self.search: list[tuple[str, str, str, str]] = []

    def dict_id(self, objects: list[dict[str, Any]] | None, origin: str) -> int | None:
        if not objects:
            return None
        hashes = [self._obj(o) for o in objects]
        h = _hash(hashes)
        if h in self.dicts:
            return self.dicts[h]
        did = self.con.execute("INSERT INTO dicts (hash, objects, origin) VALUES (?, ?, ?)",
                               (h, len(objects), origin)).lastrowid
        self.dicts[h] = did
        self.con.executemany("INSERT OR IGNORE INTO dict_obj VALUES (?, ?, ?)",
                             [(did, o["index"], self.objs[oh]) for o, oh in zip(objects, hashes)])
        return did

    def _obj(self, o: dict[str, Any]) -> str:
        h = _hash(o)
        if h not in self.objs:
            self.objs[h] = oid = self.con.execute(
                "INSERT INTO obj (hash, idx, name, type, access, pdo_mapping, data) VALUES (?,?,?,?,?,?,?)",
                (h, o["index"], o["name"], o["type"], o.get("access"), o.get("pdo_mapping"), _pack(o))).lastrowid
            if self.fts:
                body = " ".join([o.get("comment") or ""] + [s.get("name") or "" for s in o.get("subitems") or []
                                                            if not s.get("array_of")])
                self.search.append(("object", str(oid), f"{_hx(o['index'])} {o['name']}", body))
        return h

    def diag_set(self, msgs: list[dict[str, Any]] | None, origin: str) -> int | None:
        if not msgs:
            return None
        hashes = [self._msg(m) for m in msgs]
        h = _hash(hashes)
        if h in self.sets:
            return self.sets[h]
        sid = self.con.execute("INSERT INTO diag_sets (hash, messages, origin) VALUES (?, ?, ?)",
                               (h, len(msgs), origin)).lastrowid
        self.sets[h] = sid
        self.con.executemany("INSERT OR IGNORE INTO diag_msg VALUES (?, ?, ?)",
                             [(sid, m["text_id"], self.msgs[mh]) for m, mh in zip(msgs, hashes)])
        return sid

    def _msg(self, m: dict[str, Any]) -> str:
        h = _hash(m)
        if h not in self.msgs:
            self.msgs[h] = mid = self.con.execute("INSERT INTO msg (hash, text_id, message, data) VALUES (?,?,?,?)",
                                                  (h, m["text_id"], m["message"], _pack(m))).lastrowid
            if self.fts:
                body = " ".join([m.get("description") or "", m.get("hint") or ""]
                                + [f"{c['cause']} {c['remedy']}" for c in m.get("causes") or []])
                self.search.append(("diag", str(mid), f"{_hx(m['text_id'])} {m['message']}", body))
        return h


def _like(s: str) -> str:
    """A LIKE pattern part that matches `s` literally (ESCAPE '\\')."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _title(typ: str, name: str) -> str:
    """'EL3356-0010 1Ch. Ana. Input …' — the name, with the type in front when the name does not begin with it."""
    return name if (name or "").lower().startswith((typ or "").lower()) else f"{typ} {name}".strip()


def _resolve_ref(ref: str, file: Path, by_name: dict[str, list[Path]]) -> Path | None:
    """A DictionaryFile / DiagFile reference ('Beckhoff AX5xxx\\Beckhoff AX5101-xxxx-0216 Dict.xml'): beside the
    referencing file as written; else the one source file of that name (several: none is guessed)."""
    parts = [p for p in re.split(r"[\\/]", ref) if p]
    if not parts:
        return None
    cand = file.parent.joinpath(*parts)
    if cand.is_file():
        return cand
    same = by_name.get(parts[-1].lower()) or []
    return same[0] if len(same) == 1 else None


def module_candidates(con: sqlite3.Connection, vendor: str, dev_file: str, ident: int) -> tuple[list[Any], str]:
    """Which modules a device's slot means by a ModuleIdent, by rule — never a guess:
    1. a module of the device's own file (Descriptions\\Modules);
    2. a module of a module file paired with the device file by name: '<prefix> Modules….xml' where <prefix>
       begins the device file's name ('Beckhoff AX8yxx-zzzz-0107 Modules.xml' → 'Beckhoff AX8yxx-zzzz-0107.xml';
       'Beckhoff AX8yxx Modules Safety.xml' → every 'Beckhoff AX8yxx…'), the longest prefix first;
    3. the only module of the vendor with that ident (several: every one).
    -> (the modules of the first rule that applies: rows (id, type, name, data, file path), that rule). One rule for
    hw_lookup's slots, hw_module(device=…) and the modules' search text (N156) — the build reads it too."""
    rows = con.execute("SELECT m.id, m.type, m.name, m.data, f.path FROM modules m JOIN files f ON f.id = m.file_id "
                       "WHERE m.ident = ? AND m.vendor = ?", (ident, vendor)).fetchall()
    if not rows:
        return [], ""
    own = [c for c in rows if os.path.normcase(c[4]) == os.path.normcase(dev_file)]
    if own:
        return own, "the device's own file"
    dev = Path(dev_file).stem.lower()
    best, paired = -1, []
    for c in rows:
        stem = Path(c[4]).stem.lower()
        k = stem.find(" modules")
        if k <= 0 or not dev.startswith(stem[:k]):
            continue
        if k > best:
            best, paired = k, [c]
        elif k == best:
            paired.append(c)
    if paired:
        return paired, "the module file of this device file"
    return rows, "the only module with this ident" if len(rows) == 1 else "every module with this ident"


class _Build:
    """One pass over the files (see the module text for the rules on duplicates)."""

    def __init__(self, con: sqlite3.Connection, fts: bool, files: list[tuple[Path, str]]):
        self.con, self.fts = con, fts
        self.w = _Writer(con, fts)
        self.by_name: dict[str, list[Path]] = {}
        for p, _s in files:
            self.by_name.setdefault(p.name.lower(), []).append(p)
        self.seen_hash: dict[str, int] = {}
        self.kinds: dict[str, int] = {}
        self.errors: list[str] = []
        self.missing: list[str] = []
        self.not_esi: list[str] = []
        self.devs: dict[tuple, dict[str, Any]] = {}
        self.n_modules = 0
        self.file_keys: dict[int, list[tuple]] = {}
        self.ref_files: dict[str, int | None] = {}
        self.ref_dicts: set[int] = set()            # dictionaries / catalogues of a file of their own: always kept
        self.ref_sets: set[int] = set()

    def file(self, p: Path, src: str) -> None:
        try:
            st = p.stat()
        except OSError as exc:
            self.errors.append(f"{p}: {exc}")
            return
        kind = root_kind(p)
        if kind is None:                           # not an ESI document (or not well-formed XML): listed, not read
            self.not_esi.append(str(p))
            self.con.execute("INSERT INTO files (path, source, kind, size, mtime) VALUES (?,?,?,?,?)",
                             (str(p), src, None, st.st_size, st.st_mtime))
            return
        h = _content_hash(p)
        same = self.seen_hash.get(h)
        fid = self.con.execute("INSERT INTO files (path, source, kind, size, mtime, hash, same_as) VALUES "
                               "(?,?,?,?,?,?,?)", (str(p), src, kind, st.st_size, st.st_mtime, h, same)).lastrowid
        if same is not None:
            for k in self.file_keys.get(same, []):      # the same content in another folder: read once
                if self.devs[k]["fid"] == same:
                    self.devs[k]["also_in"].append(str(p))
            return
        self.seen_hash[h] = fid
        self.kinds[kind] = self.kinds.get(kind, 0) + 1
        n = 0
        try:
            if kind == "info":
                for what, rec in iter_info(p):
                    n += 1
                    (self.device if what == "device" else self.module)(rec, fid, p, st.st_mtime)
            elif kind == "module":
                for rec in iter_modules(p):
                    n += 1
                    self.module(rec, fid, p, st.st_mtime)
            elif kind == "dict":
                n = 1 if self._ref_file(p, "dict") is not None else 0
            else:
                sid = self._ref_file(p, "diag")
                n = self.con.execute("SELECT messages FROM diag_sets WHERE id = ?", (sid,)).fetchone()[0] if sid else 0
        except (ET.ParseError, UnicodeDecodeError, ValueError, OSError) as exc:
            self.errors.append(f"{p.name}: {exc}")
            self.con.execute("UPDATE files SET error = ? WHERE id = ?", (str(exc)[:300], fid))
        self.con.execute("UPDATE files SET items = ? WHERE id = ?", (n, fid))

    def _ref_file(self, p: Path, kind: str) -> int | None:
        """A dictionary / diagnosis file (by itself, or named by a device): read once -> its dict id / diag set."""
        k = os.path.normcase(str(p))
        if k not in self.ref_files:
            try:
                root = ET.parse(p).getroot()
            except (ET.ParseError, OSError) as exc:
                self.ref_files[k] = None
                self.errors.append(f"{p.name}: {exc}")
                return None
            if kind == "dict":
                self.ref_files[k] = x = self.w.dict_id(parse_dictionary(_kid(root, "Dictionary")), str(p))
                if x is not None:
                    self.ref_dicts.add(x)
            else:
                self.ref_files[k] = x = self.w.diag_set(parse_diag_messages(_kid(root, "DiagMessages")), str(p))
                if x is not None:
                    self.ref_sets.add(x)
        return self.ref_files[k]

    def _stored(self, kind: str, ref: int) -> list[dict[str, Any]]:
        if kind == "dict":
            q = "SELECT o.data FROM dict_obj d JOIN obj o ON o.id = d.obj_id WHERE d.dict_id = ? ORDER BY d.idx"
        else:
            q = "SELECT m.data FROM diag_msg d JOIN msg m ON m.id = d.msg_id WHERE d.set_id = ? ORDER BY d.text_id"
        return [_unpack(r[0]) for r in self.con.execute(q, (ref,))]

    def _content(self, rec: dict[str, Any], p: Path, kind: str) -> int | None:
        """The device's dictionary (kind 'dict') or diagnosis catalogue ('diag'): inline in its profiles, in the
        files they name, or both (joined; what they give differently in rec[…_conflicts]) -> its id."""
        inline = rec.pop("dictionary" if kind == "dict" else "diag", None)
        one = rec.get("dictionary_file" if kind == "dict" else "diag_file")
        names = rec.get("dictionary_files" if kind == "dict" else "diag_files") or ([one] if one else [])
        ids = []
        for name in names:
            ref = _resolve_ref(name, p, self.by_name)
            if ref is None:
                self.missing.append(f"{rec['type']}: {name}")
                note = "dictionary_note" if kind == "dict" else "diag_note"
                what = "dictionary" if kind == "dict" else "diagnosis"
                rec[note] = (rec[note] + "; " if rec.get(note) else "") + \
                    f"the {what} file {name!r} is not in the ESI folders"
                continue
            x = self._ref_file(ref, kind)
            if x is not None:
                ids.append(x)
        if not inline and len(ids) == 1:
            return ids[0]                              # the file's own dictionary / catalogue, stored once
        parts = ([inline] if inline else []) + [self._stored(kind, x) for x in ids]
        joined, conflicts = _join(parts, "index" if kind == "dict" else "text_id")
        if conflicts:
            key = "dictionary_conflicts" if kind == "dict" else "diag_conflicts"
            rec[key] = sorted(set(rec.get(key) or []) | {_hx(i) for i in conflicts})
        if kind == "dict":
            return self.w.dict_id(joined, str(p))
        return self.w.diag_set(joined, str(p))

    def device(self, rec: dict[str, Any], fid: int, p: Path, mtime: float) -> None:
        key = (rec["type"].lower(), rec["product_code"], rec["revision"])
        rh = _hash({k: v for k, v in rec.items() if k not in ("image", "file_version")})
        ver = (rec.get("file_version") or 0, mtime)
        old = self.devs.get(key)
        if old is not None:
            if old["hash"] == rh:
                old["also_in"].append(str(p))
                return
            if ver <= old["ver"]:
                old["differs_in"].append({"file": str(p), "file_version": rec.get("file_version")})
                return
        did = self._content(rec, p, "dict")
        sid = self._content(rec, p, "diag")
        n_obj = self.con.execute("SELECT objects FROM dicts WHERE id = ?", (did,)).fetchone()[0] if did else 0
        data = {k: v for k, v in rec.items() if k not in ("type", "name", "product_code", "revision", "vendor",
                                                          "group_type")}
        row = (rec["type"], rec["name"], rec["product_code"], rec["revision"], rec["vendor"], rec["group_type"],
               rec.get("file_version"), fid, did, sid, len(rec["pdos"]), sum(len(x["entries"]) for x in rec["pdos"]),
               n_obj, _pack(data))
        cols = ("type, name, product_code, revision, vendor, group_type, file_version, file_id, dict_id, diag_set, "
                "pdo_count, pdo_entries, objects, data")
        if old is None:
            dev_id = self.con.execute(f"INSERT INTO devices ({cols}) VALUES ({','.join('?' * len(row))})", row).lastrowid
            self.devs[key] = {"id": dev_id, "fid": fid, "path": str(p), "ver": ver, "hash": rh, "also_in": [],
                              "differs_in": []}
        else:
            self.con.execute(f"UPDATE devices SET ({cols}) = ({','.join('?' * len(row))}) WHERE id = ?",
                             (*row, old["id"]))
            # the replaced description, and the files that gave it identically, differ from the one kept
            old["differs_in"].extend({"file": f, "file_version": old["ver"][0] or None}
                                     for f in [old["path"], *old["also_in"]])
            old["also_in"] = []
            old.update(fid=fid, path=str(p), ver=ver, hash=rh)
        self.file_keys.setdefault(fid, []).append(key)

    def module(self, rec: dict[str, Any], fid: int, p: Path, mtime: float) -> None:
        """Modules are kept per file: the same ModuleIdent means different modules in different files (the AX8000
        module files per firmware revision); a device's slots are resolved against them when read (EsiIndex)."""
        did = self.w.dict_id(rec.pop("dictionary", None), str(p))
        sid = self.w.diag_set(rec.pop("diag", None), str(p))
        data = {"pdos": rec["pdos"], "profile_no": rec.get("profile_no"), "file_version": rec.get("file_version"),
                **{k: rec[k] for k in ("dictionary_conflicts", "diag_conflicts") if rec.get(k)}}
        self.con.execute("INSERT INTO modules (ident, type, name, vendor, file_id, dict_id, diag_set, data) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (rec["ident"], rec["type"], rec["name"], rec["vendor"], fid, did, sid, _pack(data)))
        self.n_modules += 1

    def _prune(self) -> None:
        """Review 0.8.12: a description replaced by a newer one (another file, higher FileVersion) left its
        dictionary, catalogue, objects and messages behind — found by hw_search, counted as stored. Kept: what a
        device or module uses, and the content of a dictionary / diagnosis file of its own."""
        con = self.con
        con.execute("CREATE TEMP TABLE keep_d (id INTEGER PRIMARY KEY)")
        con.execute("CREATE TEMP TABLE keep_s (id INTEGER PRIMARY KEY)")
        con.execute("INSERT OR IGNORE INTO keep_d SELECT dict_id FROM devices WHERE dict_id IS NOT NULL "
                    "UNION SELECT dict_id FROM modules WHERE dict_id IS NOT NULL")
        con.executemany("INSERT OR IGNORE INTO keep_d VALUES (?)", [(x,) for x in self.ref_dicts])
        con.execute("INSERT OR IGNORE INTO keep_s SELECT diag_set FROM devices WHERE diag_set IS NOT NULL "
                    "UNION SELECT diag_set FROM modules WHERE diag_set IS NOT NULL")
        con.executemany("INSERT OR IGNORE INTO keep_s VALUES (?)", [(x,) for x in self.ref_sets])
        con.execute("DELETE FROM dicts WHERE id NOT IN (SELECT id FROM keep_d)")
        con.execute("DELETE FROM dict_obj WHERE dict_id NOT IN (SELECT id FROM keep_d)")
        con.execute("DELETE FROM obj WHERE id NOT IN (SELECT obj_id FROM dict_obj)")
        con.execute("DELETE FROM diag_sets WHERE id NOT IN (SELECT id FROM keep_s)")
        con.execute("DELETE FROM diag_msg WHERE set_id NOT IN (SELECT id FROM keep_s)")
        con.execute("DELETE FROM msg WHERE id NOT IN (SELECT msg_id FROM diag_msg)")
        con.execute("DROP TABLE keep_d")
        con.execute("DROP TABLE keep_s")
        objs = {r[0] for r in con.execute("SELECT id FROM obj")}
        msgs = {r[0] for r in con.execute("SELECT id FROM msg")}
        self.w.search = [r for r in self.w.search if (int(r[1]) in objs if r[0] == "object" else
                                                      int(r[1]) in msgs if r[0] == "diag" else True)]

    def _module_hosts(self) -> dict[int, list[str]]:
        """N156 (0.8.14 retest): module id -> the devices whose slots take it (type and name as the device's search
        title, its group), by the rule of hw_lookup's slots (module_candidates; several at that rule: each of them).
        A module's own name is generic (the AX8000's DeviceMain, SynchronousMotor …): its search text holds these,
        so hw_search finds a module by its device."""
        con = self.con
        rule: dict[tuple[str, str, int], list[int]] = {}
        hosts: dict[int, dict[str, None]] = {}
        devs = con.execute("SELECT d.type, d.name, d.vendor, d.data, f.path FROM devices d JOIN files f ON "
                           "f.id = d.file_id ORDER BY d.type, d.revision DESC").fetchall()
        for typ, name, vendor, blob, path in devs:
            data = _unpack(blob) or {}
            if not data.get("slots"):
                continue
            text = " ".join(x for x in (_title(typ, name), data.get("group_name")) if x)
            for sl in data["slots"].get("slots") or []:
                for m in sl.get("modules") or []:
                    ident = _num(m.get("ident"))
                    if ident is None:
                        continue                   # a ModuleClass: not resolved (hw_lookup's slots neither)
                    key = (vendor, os.path.normcase(path), ident)
                    if key not in rule:
                        rule[key] = [c[0] for c in module_candidates(con, vendor, path, ident)[0]]
                    for mid in rule[key]:
                        hosts.setdefault(mid, {})[text] = None      # each device text once, in type order
        return {mid: list(t) for mid, t in hosts.items()}

    def finish(self) -> dict[str, Any]:
        con = self.con
        self._prune()
        for d in self.devs.values():
            if d["also_in"] or d["differs_in"]:
                con.execute("UPDATE devices SET also_in = ?, differs_in = ? WHERE id = ?",
                            (json.dumps(d["also_in"]) if d["also_in"] else None,
                             json.dumps(d["differs_in"]) if d["differs_in"] else None, d["id"]))
        if self.fts:
            rows = self.w.search
            for r in con.execute("SELECT id, type, name, data FROM devices"):
                data = _unpack(r[3])
                body = " ".join([data.get("name_de") or "", data.get("group_name") or ""]
                                + [f"{x['name']} " + " ".join(e["name"] for e in x["entries"]) for x in data["pdos"]]
                                + [m["name"] for m in data.get("mappings") or []])
                rows.append(("device", str(r[0]), _title(r[1], r[2]), body))
            hosts = self._module_hosts()
            for r in con.execute("SELECT id, type, name, data FROM modules"):
                data = _unpack(r[3])
                rows.append(("module", str(r[0]), _title(r[1], r[2]),
                             " ".join([f"{x['name']} " + " ".join(e["name"] for e in x["entries"]) for x in data["pdos"]]
                                      + hosts.get(r[0], []))))
            con.executemany("INSERT INTO search (kind, ref, title, body) VALUES (?, ?, ?, ?)", rows)
            con.execute("INSERT INTO search(search) VALUES ('optimize')")
        n = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("dicts", "obj", "msg")}
        return {"devices": len(self.devs), "types": len({k[0] for k in self.devs}), "modules": self.n_modules,
                "dictionaries": n["dicts"], "objects": n["obj"], "diag_messages": n["msg"],
                "files_read": len(self.seen_hash), "kinds": self.kinds}


def recorded_extra(final: Path) -> list[str]:
    """The extra ESI folders the index in use was built with (knowledge.esi_dirs of the server that built it)."""
    try:
        return [str(x) for x in json.loads(read_meta(final).get("extra_dirs") or "[]")]
    except ValueError:
        return []


def recorded_twincat(final: Path) -> bool:
    """Whether the index in use read TwinCAT's own ESI folders (esi_db.py update --no-twincat: not); True for none."""
    return read_meta(final).get("twincat", "1") != "0"


def build(final: Path, kb: Path | None, extra: Iterable[str | Path] | None = None, twincat: bool | None = None,
          force: bool = False, built_by: str = "", log: Callable[[str], None] = print) -> dict[str, Any]:
    """Build <final> from every ESI file of the sources, into <final>.building, swapped in when complete. Without
    force an index built from exactly these files (same paths, sizes, times) in this format is left as it is.
    extra=None / twincat=None: the extra folders, and whether TwinCAT's folders were read, as for the index in use
    (a rebuild from the command line or through the server keeps them)."""
    final = Path(final)
    building, lock = side_files(final)
    final.parent.mkdir(parents=True, exist_ok=True)
    extra = [str(x) for x in (recorded_extra(final) if extra is None else extra)]
    twincat = recorded_twincat(final) if twincat is None else bool(twincat)
    folders = source_folders(kb, extra, twincat)
    if not folders:
        raise EsiError("no ESI source: neither <kb>\\manuals nor a TwinCAT ESI folder (Config\\Io\\EtherCAT) exists "
                       "on this PC — set knowledge.esi_dirs, or copy the ESI files into the knowledge folder's manuals\\")
    lk = kb_lock.IndexLock(lock)
    try:
        lk.__enter__()
    except kb_lock.Held as h:
        raise EsiError(f"another ESI build is running (pid {h.info.get('pid')}, started {h.info.get('started')}, "
                       f"{h.info.get('phase') or 'starting'})") from None
    try:
        files = xml_files(folders)
        stamp = sources_stamp(files)
        meta = read_meta(final)
        # current: this format, complete, and read with TwinCAT's folders as asked (--twincat / --no-twincat change it)
        current = (meta.get("format") == FORMAT and meta.get("state") == "complete"
                   and meta.get("twincat", "1") == ("1" if twincat else "0"))
        if not force and current and meta.get("sources_stamp") == stamp:
            log(f"up to date: {final} ({meta.get('devices')} device revisions from {meta.get('files')} files)")
            out: dict[str, Any] = {"result": "up_to_date", "index": str(final), "devices": _int(meta.get("devices"))}
            if building.is_file():               # left by an interrupted or redundant build: the index is current
                _discard(building)
                log(f"discarded {building} (a build left next to the complete, current index)")
                out["discarded"] = str(building)
            return out
        bm = read_meta(building) if building.is_file() else {}
        if (not force and bm.get("state") == "complete" and bm.get("format") == FORMAT
                and bm.get("sources_stamp") == stamp):
            # a finished build of exactly these files that could not replace the index (it was open): swap it in
            why_not = _swap(building, final)
            if why_not is None:
                log(f"the finished build replaced {final} ({bm.get('devices')} device revisions)")
                return {"result": "replaced", "index": str(final), "devices": _int(bm.get("devices"))}
            log(f"a finished build is waiting to replace {final}, but {why_not}")
            return {"result": "not_replaced", "index": str(final), "building": str(building), "why": why_not}
        if building.is_file():
            _discard(building)                       # interrupted, or built from other files: start over
        why = ("forced" if force else "no index yet" if not final.is_file() else
               f"format {meta.get('format') or 'unknown'}, this plugin writes {FORMAT}" if meta.get("format") != FORMAT
               else "the index was never finished" if meta.get("state") != "complete" else "the ESI files changed")
        log(f"building {final} ({why}) from {len(files)} XML files in {[str(f) for f, _s in folders]}"
            + ("; the current index stays in use until the new one is complete" if final.is_file() else ""))
        t0 = time.time()
        res = _build_into(building, files, folders, stamp, built_by, lk, extra, twincat)
        lk.update(phase="replacing the index in use")
        why_not = _swap(building, final)
        took = round(time.time() - t0)
        if why_not is not None:
            log(f"the new index is complete ({building}) but {why_not} — it replaces the index at the next build")
            return {"result": "not_replaced", "index": str(final), "building": str(building), "why": why_not, **res}
        log(f"indexed {res['devices']} device revisions ({res['types']} types), {res['modules']} modules, "
            f"{res['dictionaries']} dictionaries ({res['objects']} distinct objects), {res['diag_messages']} distinct "
            f"diagnosis messages from {res['files_read']} ESI files ({len(files)} XML files) into {final} in {took} s "
            f"({final.stat().st_size / 1e6:.0f} MB)")
        if res["missing_refs"]:
            log(f"{len(res['missing_refs'])} dictionary / diagnosis file(s) named by a device are not in the folders "
                f"(first: {res['missing_refs'][:3]})")
        if res["errors"]:
            log(f"{len(res['errors'])} file(s) could not be read: {res['errors'][:5]}")
        return {"result": "built", "index": str(final), "seconds": took, **res}
    finally:
        lk.__exit__(None, None, None)


def _build_into(db: Path, files: list[tuple[Path, str]], folders: list[tuple[Path, str]], stamp: str,
                built_by: str, lk: kb_lock.IndexLock, extra: list[str], twincat: bool = True) -> dict[str, Any]:
    con = sqlite3.connect(str(db))
    try:
        con.execute("PRAGMA journal_mode=OFF")       # the work file: nothing reads it before it is complete
        con.execute("PRAGMA synchronous=OFF")
        con.executescript(SCHEMA)
        try:
            con.execute(FTS)
            fts = True
        except sqlite3.OperationalError:
            fts = False                              # an SQLite without FTS5: everything but `search`
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("format", FORMAT), ("state", "building"), ("started", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("built_by", built_by or "twincat-mcp"), ("python", sys.version.split()[0]), ("fts", str(int(fts))),
            ("sources", json.dumps([[str(f), s] for f, s in folders])), ("extra_dirs", json.dumps(extra)),
            ("twincat", "1" if twincat else "0")])
        b = _Build(con, fts, files)
        total = len(files)
        for i, (p, src) in enumerate(files):
            lk.update(phase=f"reading ESI files ({i + 1} of {total}): {p.name}", files_done=i, files_total=total)
            b.file(p, src)
        lk.update(phase="search index", files_done=total, files_total=total)
        res = b.finish()
        con.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", [
            ("sources_stamp", stamp), ("files", str(total)), ("files_read", str(res["files_read"])),
            ("devices", str(res["devices"])), ("types", str(res["types"])), ("modules", str(res["modules"])),
            ("dictionaries", str(res["dictionaries"])), ("objects", str(res["objects"])),
            ("diag_messages", str(res["diag_messages"])), ("kinds", json.dumps(res["kinds"])),
            ("errors", json.dumps(b.errors)), ("missing_refs", json.dumps(b.missing)),
            ("not_esi", json.dumps(b.not_esi)), ("built", time.strftime("%Y-%m-%d %H:%M:%S")), ("state", "complete")])
        con.commit()
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()
    return {**res, "files": total, "errors": b.errors[:20], "missing_refs": b.missing, "not_esi": b.not_esi[:20]}


def _discard(building: Path, wait_s: float = SWAP_WAIT_S) -> None:
    """The build's own work file and its SQLite side files — the only thing a build deletes. A file another program
    holds open (a reader, a virus scan) is retried as _swap does, then the build stops with the reason."""
    for p in [building] + [building.with_name(building.name + x) for x in SIDE]:
        t0 = time.time()
        while True:
            try:
                p.unlink()
                break
            except FileNotFoundError:
                break
            except PermissionError:
                if time.time() - t0 > wait_s:
                    raise EsiError(f"{p} is open in another program — it cannot be discarded; close it and build "
                                   "again", "busy") from None
                time.sleep(1.0)


def _swap(building: Path, final: Path, wait_s: float = SWAP_WAIT_S) -> str | None:
    """The finished build replaces the index in use (os.replace, retried while a reader has it open). -> None when
    replaced, else why not. Both files are in rollback-journal mode (the build sets it; readers never write)."""
    t0 = time.time()
    while True:
        try:
            os.replace(building, final)
            return None
        except PermissionError:
            if time.time() - t0 > wait_s:
                return f"{final} is open in another program"
            time.sleep(1.0)


# --------------------------------------------------------------------------------------------- state
def state(final: Path, kb: Path | None, extra: Iterable[str | Path] | None = None,
          twincat: bool | None = None) -> dict[str, Any]:
    """missing | complete | rebuild_needed (another format, or never finished) | outdated (ESI files added, changed or
    removed since, or other ESI folders), plus a build running / interrupted / waiting to replace it / stale (a work
    file left next to the complete, current index: the next build discards it). An index that exists is in use as it
    is until the user approves the rebuild. extra=None / twincat=None: the folders it was built with."""
    final = Path(final)
    extra = [str(x) for x in (recorded_extra(final) if extra is None else extra)]
    twincat = recorded_twincat(final) if twincat is None else bool(twincat)
    stamp_now: str | None = None
    building, lock = side_files(final)
    meta = read_meta(final)
    out: dict[str, Any] = {"database": str(final), "format_expected": FORMAT}
    if not final.is_file() or not meta:
        out["state"] = "missing"
    elif meta.get("format") != FORMAT:
        out["state"] = "rebuild_needed"
        out["reason"] = f"format {meta.get('format') or 'unknown'}, this plugin reads {FORMAT}"
    elif meta.get("state") != "complete":
        out["state"] = "rebuild_needed"
        out["reason"] = "the index was never finished"
    else:
        folders = source_folders(kb, extra, twincat)
        files = xml_files(folders)
        stamp_now = sources_stamp(files)
        out["state"] = "complete" if stamp_now == meta.get("sources_stamp") else "outdated"
        if out["state"] == "outdated":
            try:
                was = [f for f, _s in json.loads(meta.get("sources") or "[]")]
            except ValueError:
                was = []
            now = [str(f) for f, _s in folders]
            out["reason"] = (f"the ESI folders changed (built from {was}, now {now})" if
                             [os.path.normcase(x) for x in was] != [os.path.normcase(x) for x in now] else
                             "ESI files were added, changed or removed since the index was built")
            out["files_now"] = len(files)
    for k in ("devices", "types", "modules", "dictionaries", "objects", "diag_messages", "files"):
        if meta.get(k):
            out[k] = _int(meta[k])
    for k in ("built", "built_by", "format"):
        if meta.get(k):
            out[k] = meta[k]
    for k in ("missing_refs", "errors"):
        try:
            v = json.loads(meta.get(k) or "[]")
        except ValueError:
            v = []
        if v:
            out[k] = {"count": len(v), "first": v[:5]}
    running = kb_lock.held(lock)
    if running or building.is_file():
        info, beat = kb_lock.read(lock)
        if running:
            out["rebuild"] = {"state": "running", "phase": info.get("phase"), "pid": info.get("pid"),
                              "started": info.get("started"), "files_done": info.get("files_done"),
                              "files_total": info.get("files_total"),
                              "seconds_since_progress": round(time.time() - beat) if beat else None}
        else:
            bm = read_meta(building)
            if stamp_now is None:              # also with no index yet: a finished first build waits to replace it
                stamp_now = sources_stamp(xml_files(source_folders(kb, extra, twincat)))
            if out["state"] == "complete":
                rbs = "stale"                    # build() returns up_to_date and discards it
            elif bm.get("state") == "complete" and bm.get("format") == FORMAT and bm.get("sources_stamp") == stamp_now:
                rbs = "waiting_to_replace"       # build() swaps it in
            elif bm.get("state") == "complete":
                rbs = "stale"                    # built from other files or another format: build() starts over
            else:
                rbs = "interrupted"
            out["rebuild"] = {"state": rbs, "file": str(building)}
    out["message"] = message(out)
    return out


def message(s: dict[str, Any]) -> str:
    st = s["state"]
    rb = s.get("rebuild") or {}
    if rb.get("state") == "running":
        tail = (f"; a build is running ({rb.get('phase') or 'starting'}"
                + (f", {rb['files_done']} of {rb['files_total']} files" if rb.get("files_total") else "") + ")")
    elif rb.get("state") == "waiting_to_replace":
        tail = "; a finished build replaces it at the next build"
    elif rb.get("state") == "interrupted":
        tail = "; a build was interrupted — the next one starts over"
    elif rb.get("state") == "stale":
        tail = f"; a work file was left ({rb.get('file')}) — the next build discards it"
    else:
        tail = ""
    if st == "missing":
        return "no ESI index yet — its first build starts by itself" + tail
    base = (f"{s.get('devices', '?')} device revisions ({s.get('types', '?')} types) from {s.get('files', '?')} XML "
            f"files, built {s.get('built', '?')}")
    if st == "complete":
        return base + tail
    return (f"{base}; in use as it is, {'OUTDATED' if st == 'outdated' else 'a REBUILD is due'} — {s.get('reason')} — "
            "the rebuild runs once approved: kb_rebuild(index='esi')" + tail)


# --------------------------------------------------------------------------------------------- reading
DETAILS = ("newest", "summary", "full")
_KINDS = ("device", "object", "diag", "module")
# N152 / N154 (0.8.14 retest): the forms every hw_* reader takes a number in — '#x' / '0x' hex, else decimal; an
# object index, a text id and a ModuleIdent also as 4 hex digits alone (8000 = #x8000) — named when a value is none
FORMS = {
    "index": "'#x8000', '0x8000', 4 hex digits alone ('8000' = #x8000) or decimal (32768); one sub-index in CoE "
             "notation: '8000:11' (the sub-index in hex, 1–2 digits as Beckhoff's manuals and TwinCAT write it: "
             "8000:11 = sub-index 17; or '8000:#x11', '8000:0x11')",
    "code": "'#x4101', '0x4101', 4 hex digits alone ('4101' = #x4101) or decimal (16641)",
    "ident": "'#x006B0064' as hw_lookup's slots show it, '0x006B0064', 4 hex digits alone or decimal",
    "product code": "'#x0D1C3052', '0x0D1C3052' or decimal (219951186)",
    "revision": "'#x0019000A' (as hw_lookup shows it), '0x0019000A' or decimal (1638410)",
}
_NOT_IN_ESI = ("not in the ESI files of this PC — copy the vendor's ESI XML into the knowledge folder's manuals\\ (any "
               "sub-folder) and approve kb_rebuild(index='esi'), or update TwinCAT's device descriptions (XAE: TwinCAT "
               "→ EtherCAT Devices → Update Device Descriptions)")


def answer_source(db: Path, st: dict[str, Any]) -> dict[str, Any]:
    """N155 (0.8.14 retest): the index an hw_* answer came from, the same block in the server's hw_* tools and the
    skill's hw_* commands — database, state, built; and, when it is not the current one (rule 6), what it is
    (state_note: the answer comes from that index as it is)."""
    src: dict[str, Any] = {"database": str(db), "state": st["state"], "built": st.get("built")}
    if st["state"] != "complete" or st.get("rebuild"):
        src["state_note"] = st["message"]
    return src


class EsiIndex:
    """Read-only access to esi.db — the server's hw_* tools and the twincat-docs skill read it the same way."""

    def __init__(self, db: Path):
        self.db = Path(db)

    @contextlib.contextmanager
    def _con(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection, closed when the read is done — not left to the garbage collector: on Windows an
        open file cannot be replaced, and a build waited 60 s for a reader's forgotten connection (0.8.12 CI)."""
        if not self.db.is_file():
            raise EsiError(f"no ESI index yet ({self.db})", "no_index")
        con = connect_ro(self.db)
        try:
            yield con
        finally:
            con.close()

    @staticmethod
    def _range(n: int, what: str, bits: int, given: Any) -> int:
        # an ESI number is a UDINT (product code, revision, ModuleIdent, text id) or a UINT (object index)
        if not 0 <= n < (1 << bits):
            raise EsiError(f"{what} {given!r} is out of range (0 … 0x{(1 << bits) - 1:X})")
        return n

    @staticmethod
    def _rev(revision: int | str | None) -> int | None:
        if revision is None:
            return None
        n = revision if isinstance(revision, int) else _num(str(revision))     # _num: #x / 0x hex, else decimal
        if n is None or isinstance(n, bool):
            raise EsiError(f"revision must be {FORMS['revision']} (got {revision!r})")
        return EsiIndex._range(n, "revision", 32, revision)

    @staticmethod
    def _shown_rev(rev: int, given: int | str | None) -> str:
        """A revision as hw_lookup shows it, and the decimal number it was read from when it was given as one."""
        dec = isinstance(given, int) or bool(re.fullmatch(r"\s*\d+\s*", str(given)))
        return _hx(rev, 8) + (f" (decimal {rev})" if dec else "")

    @staticmethod
    def _index(v: str | int, what: str = "index") -> int:
        if isinstance(v, int):
            n: int | None = v
        else:
            s = str(v).strip()
            if ":" in s:
                # N152 (0.8.14 retest): the text after ':' was cut off silently ('8000:11' read as 8000); an object's
                # sub-index is read by _address, a diagnosis code or a ModuleIdent has none
                raise EsiError(f"{what} {v!r}: no sub-index here, a {what} is one number — {FORMS[what]}")
            n = _num(s if s[:2].lower() in ("#x", "0x") else ("#x" + s if re.fullmatch(r"[0-9A-Fa-f]{4}", s) else s))
        if n is None:
            raise EsiError(f"{what} must be {FORMS[what]} (got {v!r})")
        return EsiIndex._range(n, what, 16 if what == "index" else 32, v)

    @staticmethod
    def _address(v: str | int) -> tuple[int, int | None]:
        """An object's index, or one of its sub-indices in CoE notation -> (index, sub-index or None). N152 (0.8.14
        retest): '8000:11' was read as the whole object 0x8000. The sub-index is hex as the notation writes it
        (8000:11 = sub-index 17, as in Beckhoff's manuals and TwinCAT): 1–2 hex digits, or '#x…' / '0x…'."""
        if isinstance(v, int) or ":" not in str(v):
            return EsiIndex._index(v), None
        head, _c, tail = str(v).partition(":")
        idx = EsiIndex._index(head)
        t = tail.strip()
        m = re.fullmatch(r"(?:#x|0x)([0-9a-f]+)|([0-9a-f]{1,2})", t, re.I)
        if m is None:
            raise EsiError(f"sub-index {t!r} of {v!r} is none of the forms: {FORMS['index']}")
        sub = int(m.group(1) or m.group(2), 16)
        if sub > 0xFF:
            raise EsiError(f"sub-index {t!r} of {v!r} is out of range (0 … 0xFF) — {FORMS['index']}")
        return idx, sub

    def lookup(self, query: str, limit: int = 5, revision: int | str | None = None,
               detail: str = "newest") -> dict[str, Any]:
        """Devices by type / order code ('EL3356-0010'), product code ('#x0d1c3052', '0x0d1c3052'; a number is read
        as a decimal product code and as digits of a type or name: read_as) or part of a name. detail:
        'newest' — the full description of the newest revision of each type (or of `revision`), the other revisions
        as one line each; 'summary' — no PDOs or objects, their counts only; 'full' — every revision in full.
        Nothing found: `note` says what is not there — a revision of an indexed device (with revisions_available),
        or the device itself (then: add its ESI file)."""
        q = (query or "").strip()
        if not q:
            raise EsiError("empty query (e.g. 'EL3356-0010', 'EL3102', '#x0c1e3052')", "empty_query")
        if detail not in DETAILS:
            raise EsiError(f"detail must be one of {list(DETAILS)} (got {detail!r})")
        if not 1 <= int(limit) <= 30:
            raise EsiError(f"limit must be 1…30 (got {limit})")
        rev = self._rev(revision)
        rs, rp = (" AND d.revision = ?", [rev]) if rev is not None else ("", [])
        sel = "SELECT d.*, f.path AS file FROM devices d JOIN files f ON f.id = d.file_id WHERE "
        read_as = None
        with self._con() as con:
            # which devices the query means is decided by the query alone; the revision filters them after (N153)
            if q[:2].lower() in ("#x", "0x"):
                pc = _num(q)
                if pc is None:
                    # N154 (0.8.14 retest): '#x…' / '0x…' that is no hex number was searched as text and answered
                    # 'not in the ESI files … copy the ESI'
                    raise EsiError(f"product code {q!r} is not a hex number — a product code is "
                                   f"{FORMS['product code']}")
                self._range(pc, "product code", 32, q)
                base, bparams, order = "d.product_code = ?", [pc], "d.revision DESC"
            else:
                base, bparams, order = "d.type = ? COLLATE NOCASE", [q], "d.revision DESC"
                if not con.execute(f"SELECT 1 FROM devices d WHERE {base} LIMIT 1", bparams).fetchone():
                    pat = "%" + _like(q) + "%"           # the text as typed: '%' and '_' match only themselves
                    base = "d.type LIKE ? ESCAPE '\\' OR d.name LIKE ? ESCAPE '\\'"
                    bparams, order = [pat, pat], "d.type, d.revision DESC"
                    if re.fullmatch(r"[0-9]+", q) and int(q) < 1 << 32:
                        # N154 (0.8.14 retest): a decimal product code (219951186 = #x0D1C3052) was only searched as
                        # text — a number is both: the product code, and digits of a type or name ('3356')
                        base, bparams = f"d.product_code = ? OR {base}", [int(q), *bparams]
                        read_as = (f"the product code {_hx(int(q), 8)} (decimal {q}), and text in a type or name")
                    base = f"({base})"
            where, params = base + rs, bparams + rp
            rows = con.execute(sel + f"{where} ORDER BY {order} LIMIT 200", params).fetchall()
            # the counts are of every match (the rows read are the first 200 in that order)
            n_all, n_types = con.execute(f"SELECT COUNT(*), COUNT(DISTINCT lower(d.type)) FROM devices d WHERE {where}",
                                         params).fetchone()
            types = [x[0] for x in con.execute(f"SELECT MIN(d.type) FROM devices d WHERE {where} GROUP BY lower(d.type) "
                                               "ORDER BY lower(d.type) LIMIT 40", params)]
            # the newest revision of every type first, then the older ones (the older revisions of the first type
            # used to fill the limit before the other types showed)
            firsts, rest, seen = [], [], set()
            for r in rows:
                (rest if r["type"].lower() in seen else firsts).append(r)
                seen.add(r["type"].lower())
            rows = firsts + rest
            out_rows, trimmed, newest = [], 0, set()
            for r in rows[:int(limit)]:
                first = r["type"].lower() not in newest
                newest.add(r["type"].lower())
                full = detail == "full" or (detail == "newest" and (first or rev is not None))
                out_rows.append(self._device(con, r, full))
                trimmed += 0 if full else 1
            # N153 (0.8.14 retest): an empty answer said 'not in the ESI files … copy the ESI, kb_rebuild' whatever
            # emptied it — also for a device that is indexed, only not with the revision asked for
            avail: dict[str, list[str]] = {}
            if not rows and rev is not None:
                for t, rv in con.execute(f"SELECT d.type, d.revision FROM devices d WHERE {base} "
                                         "ORDER BY lower(d.type), d.revision DESC", bparams):
                    if t not in avail and len(avail) == 10:
                        break
                    avail.setdefault(t, []).append(_hx(rv, 8))
        res: dict[str, Any] = {"query": q, **({"read_as": read_as} if read_as else {}), "matches": n_all,
                               "types": types, "detail": detail, "devices": out_rows}
        if n_types > len(types):
            res["types_count"] = n_types              # 'types' lists the first 40 by name
        if n_all > int(limit):
            res["more"] = f"{n_all - len(out_rows)} more revision(s) match: limit=…, revision=…, or a longer query"
        if trimmed:
            res["detail_note"] = (f"{trimmed} device row(s) as one line (detail={detail!r}): detail='full', or "
                                  "revision='#x…' (as shown), gives them in full")
        if avail:
            shown = self._shown_rev(rev, revision)
            if len(avail) == 1:
                t, revs = next(iter(avail.items()))
                res["note"] = (f"{t} has no revision {shown} in the ESI index; its revisions (newest first): "
                               f"{', '.join(revs)}")
            else:
                res["note"] = (f"none of the types that match {q!r} has revision {shown} in the ESI index; their "
                               "revisions: revisions_available")
            res["revisions_available"] = avail
        elif not rows:
            if q[:2].lower() in ("#x", "0x"):
                what = f"no device has the product code {_hx(_num(q), 8)}"
            else:
                what = (f"no device type or name contains {q!r}" + (f", no device has the product code "
                                                                      f"{_hx(int(q), 8)} (decimal {q})" if read_as else "")
                        + f" (a product code is {FORMS['product code']})")
            res["note"] = f"{what} — {_NOT_IN_ESI}"
        return res

    def _device(self, con: sqlite3.Connection, r: sqlite3.Row, full: bool) -> dict[str, Any]:
        data = _unpack(r["data"]) or {}
        d: dict[str, Any] = {"type": r["type"], "name": r["name"], "vendor": r["vendor"],
                             "group": data.get("group_name") or r["group_type"],
                             "product_code": _hx(r["product_code"], 8), "revision": _hx(r["revision"], 8),
                             "pdo_count": r["pdo_count"], "pdo_entries": r["pdo_entries"], "coe_count": r["objects"],
                             "file": r["file"]}
        if data.get("ebus_ma") is not None:
            d["ebus_ma"] = data["ebus_ma"]
        if data.get("urls"):
            d["url"] = data["urls"].get(_LCID_EN) or data["urls"].get("") or next(iter(data["urls"].values()))
        if data.get("image"):
            d["image"] = data["image"]
        if r["diag_set"]:
            d["diag_messages"] = con.execute("SELECT messages FROM diag_sets WHERE id = ?", (r["diag_set"],)).fetchone()[0]
        for k in ("also_in", "differs_in"):
            if r[k]:
                d[k] = json.loads(r[k])
        for k in ("dictionary_note", "diag_note"):
            if data.get(k):
                d[k] = data[k]
        if not full:
            return d
        for k in ("name_de", "physics", "file_version", "mailbox", "dc", "sm", "fmmu", "slots", "profile_no",
                  "channels", "profiles", "dictionary_file", "dictionary_files", "diag_file", "diag_files", "hides",
                  "dictionary_conflicts", "diag_conflicts"):
            if data.get(k) not in (None, [], {}):
                d[k] = data[k]
        if data.get("slots"):
            d["slots"] = self._slots(con, r, data["slots"])
        d["pdo"] = data.get("pdos") or []
        if data.get("mappings"):
            d["pdo_assignments"] = data["mappings"]
        if r["dict_id"]:
            d["coe_objects"] = [
                {"index": _hx(o["idx"]), "name": o["name"], "type": o["type"],
                 **({"access": o["access"]} if o["access"] else {}),
                 **({"pdo_mapping": o["pdo_mapping"]} if o["pdo_mapping"] else {})}
                for o in con.execute("SELECT o.idx, o.name, o.type, o.access, o.pdo_mapping FROM dict_obj x JOIN obj o "
                                     "ON o.id = x.obj_id WHERE x.dict_id = ? ORDER BY x.idx", (r["dict_id"],))]
            d["coe_note"] = ("one object with its sub-indices, defaults, limits and enumeration texts: "
                             "hw_object(device, index)")
        return d

    def _slots(self, con: sqlite3.Connection, r: sqlite3.Row, slots: dict[str, Any]) -> dict[str, Any]:
        """The device's slots with each module resolved (type, name, the file it came from and how)."""
        cache: dict[int, dict[str, Any]] = {}
        out = {**slots, "slots": []}
        for sl in slots.get("slots") or []:
            mods = []
            for m in sl.get("modules") or []:
                if "ident" not in m:
                    mods.append(m)
                    continue
                i = _num(m["ident"])
                if i not in cache:
                    cache[i] = self._resolve_module(con, r, i)
                mods.append({**m, **cache[i]})
            out["slots"].append({**sl, "modules": mods})
        return out

    @staticmethod
    def _resolve_module(con: sqlite3.Connection, r: sqlite3.Row, ident: int) -> dict[str, Any]:
        """Which module a device's slot means by a ModuleIdent, by the rule of module_candidates — never a guess.
        Several different modules at the first rule that applies: all are named, none is chosen."""
        cands, how = module_candidates(con, r["vendor"], r["file"], ident)
        if not cands:
            return {"module": None, "note": "no module with this ident in the ESI files"}
        distinct = {(c["type"], c["name"], bytes(c["data"])): c for c in cands}
        if len(distinct) == 1:
            c = next(iter(distinct.values()))
            return {"module": c["type"], "name": c["name"], "from": Path(c["path"]).name, "how": how}
        return {"module": None, "how": how, "candidates": [{"module": c["type"], "name": c["name"],
                                                              "from": Path(c["path"]).name}
                                                             for c in distinct.values()][:8],
                "note": "several different modules have this ident at the same rule — none is chosen"}

    def _one(self, con: sqlite3.Connection, device: str, revision: int | str | None) -> sqlite3.Row:
        rev = self._rev(revision)
        q = "SELECT d.*, f.path AS file FROM devices d JOIN files f ON f.id = d.file_id WHERE d.type = ? COLLATE NOCASE"
        rows = con.execute(q + (" AND d.revision = ?" if rev is not None else "") + " ORDER BY d.revision DESC LIMIT 1",
                           [device.strip()] + ([rev] if rev is not None else [])).fetchall()
        if not rows and rev is not None:
            # N153 (0.8.14 retest): an indexed device without that revision was called 'not in the ESI index'
            have = con.execute("SELECT type, revision FROM devices WHERE type = ? COLLATE NOCASE ORDER BY revision "
                               "DESC", (device.strip(),)).fetchall()
            if have:
                raise EsiError(f"{have[0][0]} has no revision {self._shown_rev(rev, revision)} in the ESI index; its "
                               f"revisions (newest first): {', '.join(_hx(x[1], 8) for x in have)}", "not_found")
        if not rows:
            near = [x[0] for x in con.execute("SELECT DISTINCT type FROM devices WHERE type LIKE ? ESCAPE '\\' "
                                              "ORDER BY type LIMIT 8", ("%" + _like(device.strip()) + "%",))]
            raise EsiError(f"{device!r}{f' revision {_hx(rev, 8)}' if rev is not None else ''} is not in the ESI index"
                           + (f" — types with that text: {near}" if near else " (hw_lookup lists types and revisions)"),
                           "not_found")
        return rows[0]

    def object(self, device: str, index: str | int, revision: int | str | None = None) -> dict[str, Any]:
        """One CoE object of a device (its newest revision unless `revision`): every sub-index with type, bit offset,
        access, PDO mapping, default (decoded), limits, unit code and enumeration texts. index '8000:11' (CoE
        notation, the sub-index in hex): that sub-index only — `sub` names it, `subitems` holds just it."""
        idx, sub = self._address(index)
        with self._con() as con:
            r = self._one(con, device, revision)
            if not r["dict_id"]:
                raise EsiError(f"{r['type']} {_hx(r['revision'], 8)}: the ESI gives no object dictionary for it"
                               + (f" ({(_unpack(r['data']) or {}).get('dictionary_note')})"
                                  if (_unpack(r["data"]) or {}).get("dictionary_note") else ""), "not_found")
            o = con.execute("SELECT o.data FROM dict_obj x JOIN obj o ON o.id = x.obj_id WHERE x.dict_id = ? AND "
                            "x.idx = ?", (r["dict_id"], idx)).fetchone()
            if o is None:
                near = [_hx(x[0]) for x in con.execute(
                    "SELECT idx FROM dict_obj WHERE dict_id = ? ORDER BY ABS(idx - ?) LIMIT 8", (r["dict_id"], idx))]
                raise EsiError(f"{r['type']} has no object {_hx(idx)}; the nearest: {near}", "not_found")
        obj = _unpack(o["data"])
        obj["index"] = _hx(obj["index"])
        if sub is None:
            return {"device": r["type"], "revision": _hx(r["revision"], 8), **obj}
        # N152 (0.8.14 retest): the sub-index asked for, not the whole object
        what = f"{r['type']} {obj['index']} {obj['name']!r}"
        subs = obj.get("subitems")
        if not subs:
            if sub:
                raise EsiError(f"{what} is a single value, it has no sub-index {_hx(sub, 2)} ({sub}): its value is "
                               f"sub-index 0 (index={obj['index']} gives it)", "not_found")
            return {"device": r["type"], "revision": _hx(r["revision"], 8), "index": obj.pop("index"), "sub": 0, **obj}
        one = [s for s in subs if s.get("sub") == sub]
        if not one:
            have = ", ".join(f"{_hx(s['sub'], 2)} ({s['sub']})" for s in subs if s.get("sub") is not None)
            raise EsiError(f"{what} has no sub-index {_hx(sub, 2)} ({sub}); its sub-indices: {have}", "not_found")
        obj["subitems"] = one
        return {"device": r["type"], "revision": _hx(r["revision"], 8), "index": obj.pop("index"), "sub": sub, **obj}

    def diag(self, code: str | int, device: str | None = None, revision: int | str | None = None) -> dict[str, Any]:
        """A diagnosis message by its text id (#x4101, 0x4101 — as in the diagnosis history, object 0x10F3) — of one
        device's catalogue when `device` is given, else every distinct message with that id and who uses it."""
        tid = self._index(code, "code")
        with self._con() as con:
            if device:
                r = self._one(con, device, revision)
                if not r["diag_set"]:
                    raise EsiError(f"{r['type']} {_hx(r['revision'], 8)}: the ESI gives no diagnosis messages for it"
                                   + (f" ({(_unpack(r['data']) or {}).get('diag_note')})"
                                      if (_unpack(r["data"]) or {}).get("diag_note") else ""), "not_found")
                rows = con.execute("SELECT m.id, m.data FROM diag_msg x JOIN msg m ON m.id = x.msg_id WHERE "
                                   "x.set_id = ? AND x.text_id = ?", (r["diag_set"], tid)).fetchall()
            else:
                rows = con.execute("SELECT id, data FROM msg WHERE text_id = ? ORDER BY id", (tid,)).fetchall()
            msgs = []
            for x in rows[:20]:
                m = _unpack(x["data"])
                m["text_id"] = _hx(m["text_id"])
                if not device:
                    m["devices"] = self._users(con, "diag", x["id"])
                msgs.append(m)
        out: dict[str, Any] = {"code": _hx(tid), "found": len(rows), "messages": msgs}
        if device:
            out.update(device=r["type"], revision=_hx(r["revision"], 8))
        if not rows:
            out["note"] = (f"no diagnosis message {_hx(tid)}" + (f" in {r['type']}'s catalogue" if device else
                                                                 " in the ESI files of this PC"))
        elif len(rows) > 20:
            out["more"] = f"{len(rows) - 20} more — give device=… to see that device's one"
        return out

    def _users(self, con: sqlite3.Connection, kind: str, ref: int) -> dict[str, Any]:
        """The device types that use an object / a diagnosis message (by content), the first few and how many."""
        if kind == "object":
            q = ("SELECT DISTINCT d.type FROM dict_obj x JOIN devices d ON d.dict_id = x.dict_id WHERE x.obj_id = ? "
                 "ORDER BY d.type")
        else:
            q = ("SELECT DISTINCT d.type FROM diag_msg x JOIN devices d ON d.diag_set = x.set_id WHERE x.msg_id = ? "
                 "ORDER BY d.type")
        types = [t[0] for t in con.execute(q, (ref,))]
        out: dict[str, Any] = {"count": len(types), "first": types[:8]}
        mq = (q.replace("JOIN devices d ON d.dict_id", "JOIN modules d ON d.dict_id")
               .replace("JOIN devices d ON d.diag_set", "JOIN modules d ON d.diag_set"))
        mods = [t[0] for t in con.execute(mq, (ref,))]
        if mods:
            out["modules"] = {"count": len(mods), "first": mods[:8]}   # a module's catalogue (AX8000 …)
        return out

    def search(self, query: str, kinds: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
        """Full-text search over devices (names, PDOs and their entries, PDO assignments), CoE objects (names,
        descriptions, sub-indices), diagnosis messages (text, description, causes, remedies) and modules. FTS5:
        words are ANDed, "a phrase", prefix*."""
        q = (query or "").strip()
        if not q:
            raise EsiError("empty query", "empty_query")
        want = list(kinds or _KINDS)
        bad = [k for k in want if k not in _KINDS]
        if bad:
            raise EsiError(f"kinds must be of {list(_KINDS)} (got {bad})")
        if not 1 <= int(limit) <= 100:
            raise EsiError(f"limit must be 1…100 (got {limit})")
        if read_meta(self.db).get("fts") == "0":
            raise EsiError("this index was built by an SQLite without FTS5: no full-text search (hw_lookup works)",
                           "no_index")
        with self._con() as con:
            sql = (f"SELECT kind, ref, title, snippet(search, 3, '[', ']', '…', 12) AS snip FROM search WHERE search "
                   f"MATCH ? AND kind IN ({','.join('?' * len(want))}) ORDER BY rank LIMIT ?")
            fetch = int(limit) * 8                     # hits of one device type / one text are shown as one
            try:
                rows = con.execute(sql, [q, *want, fetch]).fetchall()
                how = "fts"
            except sqlite3.OperationalError:
                # not FTS5 syntax (an order code with '-', a '#x…'): the words as one phrase
                rows = con.execute(sql, ['"' + q.replace('"', '""') + '"', *want, fetch]).fetchall()
                how = "phrase"
            items: list[dict[str, Any]] = []
            groups: dict[tuple, dict[str, Any]] = {}
            for r in rows:
                key = (r["kind"], r["title"])
                if key in groups or len(items) >= int(limit):
                    continue
                ref = int(r["ref"])
                it: dict[str, Any] = {"kind": r["kind"], "title": r["title"]}
                groups[key] = it
                if r["snip"] and "[" in r["snip"]:
                    it["snippet"] = r["snip"]
                if r["kind"] == "object":
                    o = con.execute("SELECT idx FROM obj WHERE id = ?", (ref,)).fetchone()
                    it.update(index=_hx(o["idx"]), devices=self._users(con, "object", ref))
                elif r["kind"] == "diag":
                    m = con.execute("SELECT text_id FROM msg WHERE id = ?", (ref,)).fetchone()
                    it.update(code=_hx(m["text_id"]), devices=self._users(con, "diag", ref))
                elif r["kind"] == "device":
                    d = con.execute("SELECT type FROM devices WHERE id = ?", (ref,)).fetchone()
                    n = con.execute("SELECT MAX(revision), COUNT(*) FROM devices WHERE type = ?", (d["type"],)).fetchone()
                    it.update(device=d["type"], newest_revision=_hx(n[0], 8), revisions=n[1])
                else:
                    m = con.execute("SELECT ident, type FROM modules WHERE id = ?", (ref,)).fetchone()
                    it.update(module=m["type"], ident=_hx(m["ident"], 8))
                items.append(it)
            # variants: every hit with that title, counted over the whole index (not only the rows read)
            expr = q if how == "fts" else '"' + q.replace('"', '""') + '"'
            for (kind, title), it in groups.items():
                n = con.execute("SELECT COUNT(*) FROM search WHERE search MATCH ? AND kind = ? AND title = ?",
                                (expr, kind, title)).fetchone()[0]
                if n > 1:
                    it["variants"] = n
        out: dict[str, Any] = {"query": q, "kinds": want, "count": len(items), "items": items}
        if any(i.get("variants") for i in items):
            out["variants_note"] = ("variants: that many hits with the same title (revisions of one device type, or the "
                                    "same text in several catalogues) shown as one — hw_lookup / hw_diag(device=…) "
                                    "give the one of a device")
        if how == "phrase":
            out["match"] = "as one phrase (the query is not FTS5 syntax)"
        if not items:
            out["note"] = "nothing found — fewer words, a prefix* or another kind"
        return out

    def module(self, ident: str | int, device: str | None = None, revision: int | str | None = None) -> dict[str, Any]:
        """A module of a modular device by its ModuleIdent (as hw_lookup's slots give it): with `device`, the one that
        device's slot means (the rule of hw_lookup's slots), else every module with that ident."""
        mi = self._index(ident, "ident")
        with self._con() as con:
            how = None
            if device:
                r = self._one(con, device, revision)
                res = self._resolve_module(con, r, mi)
                how = res.get("how")
                if res.get("module") is None:
                    return {"ident": _hx(mi, 8), "device": r["type"], **res}
                rows = con.execute("SELECT m.*, f.path AS file FROM modules m JOIN files f ON f.id = m.file_id WHERE "
                                   "m.ident = ? AND m.type = ? AND m.vendor = ?", (mi, res["module"], r["vendor"])).fetchall()
                rows = [x for x in rows if Path(x["file"]).name == res["from"]][:1]
            else:
                rows = con.execute("SELECT m.*, f.path AS file FROM modules m JOIN files f ON f.id = m.file_id WHERE "
                                   "m.ident = ?", (mi,)).fetchall()
            out = []
            for r in rows[:10]:
                data = _unpack(r["data"]) or {}
                rec: dict[str, Any] = {"ident": _hx(r["ident"], 8), "type": r["type"], "name": r["name"],
                                       "vendor": r["vendor"], "file": r["file"], "pdo": data.get("pdos") or []}
                if r["dict_id"]:
                    rec["coe_objects"] = [{"index": _hx(o[0]), "name": o[1]} for o in con.execute(
                        "SELECT o.idx, o.name FROM dict_obj x JOIN obj o ON o.id = x.obj_id WHERE x.dict_id = ? "
                        "ORDER BY x.idx", (r["dict_id"],))]
                if r["diag_set"]:
                    rec["diag_messages"] = con.execute("SELECT messages FROM diag_sets WHERE id = ?",
                                                       (r["diag_set"],)).fetchone()[0]
                for k in ("dictionary_conflicts", "diag_conflicts"):     # what the module's profiles give differently
                    if data.get(k):
                        rec[k] = data[k]
                out.append(rec)
        res: dict[str, Any] = {"ident": _hx(mi, 8), "found": len(rows), "modules": out}
        if device:
            res.update(device=device, how=how)
        if not rows:
            res["note"] = f"no module {_hx(mi, 8)} in the ESI files of this PC"
        return res

    def stats(self) -> dict[str, Any]:
        meta = read_meta(self.db)
        out: dict[str, Any] = {k: meta.get(k) for k in ("format", "state", "built", "built_by")}
        for k in ("devices", "types", "modules", "dictionaries", "objects", "diag_messages", "files", "files_read"):
            out[k] = _int(meta.get(k))
        try:
            out["sources"] = json.loads(meta.get("sources") or "[]")
            out["kinds"] = json.loads(meta.get("kinds") or "{}")
        except ValueError:
            pass
        return out
