"""ADS datatype table: upload, parse, expand, decode.

The symbol upload (``ADSIGRP_SYM_UPLOAD``) lists top-level variables and the
members TwinCAT chooses to publish. It does **not** enumerate elements of
``ARRAY OF STRUCT``, nested arrays, or FB-internal instances beyond what the
runtime happens to flatten. The datatype table (``ADSIGRP_SYM_DT_UPLOAD``,
0xF00E) describes every type with member names, offsets, sizes, array
bounds and enum literals. With it a variable of *any* shape can be

* expanded into every scalar leaf with its exact byte offset,
* read in a single ADS read of the whole instance and decoded locally into a
  nested JSON tree (arrays → lists, structs/FBs → dicts, enums → names),
* dumped project-wide.

Layout (TcAdsDef.h ``AdsDatatypeEntry``):
  u32 entryLength, version, hashValue, typeHashValue, size, offs, dataType, flags
  u16 nameLength, typeLength, commentLength, arrayDim, subItems
  name\\0 type\\0 comment\\0
  arrayDim × { i32 lBound; u32 elements }
  subItems × AdsDatatypeEntry (member entries: name = member, type = type name, offs)
  [flag TYPEGUID]   16-byte GUID
  [flag COPYMASK]   size bytes
  [flag METHODINFOS] u16 n, n × { u32 entryLength … }
  [flag ATTRIBUTES]  u16 n, n × { u8 nameLen; u8 valueLen; name\\0; value\\0 }
  [flag ENUMINFOS]   u16 n, n × { u8 nameLen; name\\0; value[size] }
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Any, Callable

from . import types_tc as T

DT_FLAG = {
    0x1: "DATATYPE", 0x2: "DATAITEM", 0x4: "REFERENCETO", 0x8: "METHODDEREF", 0x10: "OVERSAMPLE",
    0x20: "BITVALUES", 0x40: "PROPITEM", 0x80: "TYPEGUID", 0x100: "PERSISTENT", 0x200: "COPYMASK",
    0x400: "TCCOMIFACEPTR", 0x800: "METHODINFOS", 0x1000: "ATTRIBUTES", 0x2000: "ENUMINFOS",
    0x10000: "ALIGNED", 0x20000: "STATIC", 0x40000: "SPLEVELS", 0x80000: "IGNOREPERSIST",
    0x100000: "ANYSIZEARRAY", 0x200000: "PERSIST_DT", 0x400000: "INITONRESULT",
}
F_REFERENCETO, F_TYPEGUID, F_COPYMASK, F_METHODINFOS, F_ATTRIBUTES, F_ENUMINFOS = 0x4, 0x80, 0x200, 0x800, 0x1000, 0x2000
F_PROPITEM, F_TCCOMIFACEPTR, F_STATIC = 0x40, 0x400, 0x20000

ADST_NAME = {0: "VOID", 2: "INT16", 3: "INT32", 4: "REAL32", 5: "REAL64", 16: "INT8", 17: "UINT8", 18: "UINT16",
             19: "UINT32", 20: "INT64", 21: "UINT64", 30: "STRING", 31: "WSTRING", 32: "REAL80", 33: "BIT", 65: "BIGTYPE"}

HDR = struct.Struct("<IIIIIIIIHHHHH")   # 42 bytes


def is_interface(e: "DtEntry | None") -> bool:
    """INTERFACE types appear as pointer-sized entries with a method table and no data members (TcCOM interface
    pointers carry TCCOMIFACEPTR). Their value is an address, never a number to show or write."""
    if e is None:
        return False
    return bool(e.flags & F_TCCOMIFACEPTR) or (bool(e.flags & F_METHODINFOS) and not e.subitems and not e.dims
                                               and not e.enum and e.size in (4, 8))
MAX_ARRAY_ELEMENTS = 4096                # per expansion of a single array (guard)
MAX_LEAF_BYTES = 65536                   # a single leaf larger than this is not decoded (N47): its head is shown
HEAD_BYTES = 64                          # how much of a value that is not expanded is shown (hex)
_RE_INDIRECT = re.compile(r"^\s*(POINTER|REFERENCE)\s+TO\b", re.I)


class WalkCancelled(Exception):
    """The caller's cancel callback fired during a leaf walk (N55: a timed-out dump stops inside one huge root)."""


def not_expanded_value(head: bytes, size: int, elements: int | None, reason: str) -> dict[str, Any]:
    """N48: the one representation of a value that is not decoded in full — an array beyond MAX_ARRAY_ELEMENTS or a
    leaf beyond MAX_LEAF_BYTES. decode_leaf (CSV, ads_leaves) and decode_tree (JSON of ads_read) both return this,
    so two outputs of the same bytes never disagree. Only the first HEAD_BYTES are needed (and read)."""
    out: dict[str, Any] = {"not_expanded": reason, "bytes": size}
    if elements is not None:
        out["elements"] = elements
    out["head_hex"] = bytes(head[:HEAD_BYTES]).hex()
    return out


@dataclass
class DtEntry:
    name: str
    type: str            # for members: the type name; for types: base type (alias) or ""
    size: int
    offs: int
    data_type: int
    flags: int
    comment: str
    dims: list[tuple[int, int]] = field(default_factory=list)   # (lbound, elements)
    subitems: list["DtEntry"] = field(default_factory=list)
    enum: dict[int, str] = field(default_factory=dict)
    attributes: dict[str, str] = field(default_factory=dict)
    guid: str = ""       # datatype GUID (F_TYPEGUID), registry format {xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx}

    @property
    def is_array(self) -> bool:
        return bool(self.dims)

    @property
    def element_count(self) -> int:
        n = 1
        for _, e in self.dims:
            n *= max(e, 0)
        return n

    def flag_names(self) -> list[str]:
        return [n for b, n in DT_FLAG.items() if self.flags & b]

    def summary(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "type": self.type, "size": self.size, "ads_type": ADST_NAME.get(self.data_type, self.data_type)}
        if not self.type:
            # the table leaves 'type' empty for composite entries; say what the entry is
            d["type"] = "ENUM" if self.enum else ("ARRAY" if self.dims else ("FUNCTION_BLOCK/STRUCT" if self.subitems else self.name))
            d["kind"] = "enum" if self.enum else ("array" if self.dims else ("composite" if self.subitems else "scalar"))
        if self.dims:
            d["array"] = [{"lbound": lb, "elements": e} for lb, e in self.dims]
        if self.subitems:
            d["members"] = [{"name": s.name, "type": s.type, "offset": s.offs, "size": s.size,
                             **({"array": [{"lbound": lb, "elements": e} for lb, e in s.dims]} if s.dims else {})} for s in self.subitems]
        if self.enum:
            d["enum"] = {str(k): v for k, v in sorted(self.enum.items())}
        if self.comment:
            d["comment"] = self.comment
        if self.attributes:
            d["attributes"] = self.attributes
        fl = self.flag_names()
        if fl:
            d["flags"] = fl
        if self.guid:
            d["guid"] = self.guid
        return d


def guid_str(b: bytes) -> str:
    """16 raw GUID bytes (Windows layout: Data1..3 little-endian) → {XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}."""
    d1, d2, d3 = struct.unpack_from("<IHH", b, 0)
    return "{%08X-%04X-%04X-%s-%s}" % (d1, d2, d3, b[8:10].hex().upper(), b[10:16].hex().upper())


def guid_bytes(g: str) -> bytes:
    h = g.strip("{}").replace("-", "")
    return struct.pack("<IHH", int(h[0:8], 16), int(h[8:12], 16), int(h[12:16], 16)) + bytes.fromhex(h[16:32])


def _cstr(buf: bytes, pos: int, n: int) -> tuple[str, int]:
    return buf[pos:pos + n].decode("latin-1", "replace"), pos + n + 1


def parse_entry(buf: bytes, pos: int) -> tuple[DtEntry, int]:
    """Parse one AdsDatatypeEntry at ``pos``; returns (entry, pos_after_entry)."""
    if pos + HDR.size > len(buf):
        raise ValueError("datatype table truncated")
    (entry_len, _ver, _hash, _thash, size, offs, dtype, flags,
     nlen, tlen, clen, adim, nsub) = HDR.unpack_from(buf, pos)
    end = pos + entry_len
    p = pos + HDR.size
    name, p = _cstr(buf, p, nlen)
    typ, p = _cstr(buf, p, tlen)
    com, p = _cstr(buf, p, clen)
    dims = []
    for _ in range(adim):
        lb, el = struct.unpack_from("<iI", buf, p)
        dims.append((lb, el))
        p += 8
    subs = []
    for _ in range(nsub):
        try:
            sub, p = parse_entry(buf, p)
            subs.append(sub)
        except ValueError:
            break
    e = DtEntry(name, typ, size, offs, dtype, flags, com, dims, subs)
    # optional trailing sections (best effort; bounded by entry_len)
    try:
        if flags & F_TYPEGUID:
            if p + 16 <= end:
                e.guid = guid_str(buf[p:p + 16])
            p += 16
        if flags & F_COPYMASK:
            p += size
        if flags & F_METHODINFOS and p + 2 <= end:
            n = struct.unpack_from("<H", buf, p)[0]; p += 2
            for _ in range(n):
                ml = struct.unpack_from("<I", buf, p)[0]
                p += ml
        if flags & F_ATTRIBUTES and p + 2 <= end:
            n = struct.unpack_from("<H", buf, p)[0]; p += 2
            for _ in range(n):
                al, vl = buf[p], buf[p + 1]; p += 2
                an, p = _cstr(buf, p, al)
                av, p = _cstr(buf, p, vl)
                e.attributes[an] = av
        if flags & F_ENUMINFOS and p + 2 <= end:
            n = struct.unpack_from("<H", buf, p)[0]; p += 2
            for _ in range(n):
                nl = buf[p]; p += 1
                en, p = _cstr(buf, p, nl)
                val = buf[p:p + size]; p += size
                e.enum[int.from_bytes(val, "little", signed=True) if size <= 8 else 0] = en
    except (IndexError, struct.error):
        pass
    return e, max(end, pos + HDR.size)


def parse_table(buf: bytes, count: int | None = None) -> dict[str, DtEntry]:
    out: dict[str, DtEntry] = {}
    pos = 0
    n = 0
    while pos + HDR.size <= len(buf) and (count is None or n < count):
        try:
            e, pos = parse_entry(buf, pos)
        except ValueError:
            break
        out[e.name.lower()] = e
        n += 1
    return out


# ----------------------------------------------------------------------------
@dataclass
class Leaf:
    path: str          # full symbol path, e.g. MAIN.arrSteps[2].stTimes.tDwell
    type: str
    offset: int        # byte offset relative to the root symbol
    size: int
    data_type: int
    enum: dict[int, str] | None = None
    note: str | None = None
    elements: int | None = None   # an array of this many elements that is not expanded (beyond MAX_ARRAY_ELEMENTS)

    @property
    def not_expanded(self) -> bool:
        return self.elements is not None or self.size > MAX_LEAF_BYTES

    @property
    def need(self) -> int:
        """Bytes needed to decode this leaf: the head only for a value that is not expanded."""
        return min(self.size, HEAD_BYTES) if self.not_expanded else self.size


class TypeSystem:
    def __init__(self, table: dict[str, DtEntry]):
        self.table = table

    def get(self, type_name: str) -> DtEntry | None:
        return self.table.get(type_name.strip().lower())

    def is_indirect(self, e: DtEntry | None, type_name: str | None = None) -> bool:
        """POINTER TO / REFERENCE TO / INTERFACE: the value is an address, never the pointee (R42: a POINTER TO
        FB_TestSuite entry carries the pointee as its base type and was expanded as the whole FB at the pointer's
        8 bytes — fabricated members, 4.7 GB of JSON for 1,000 pointers). N50: the alias chain is followed, so
        TYPE PT_X : POINTER TO FB_X (and an alias of that) is an indirection too."""
        return self.indirect_kind(e, type_name) is not None

    def indirect_kind(self, e: DtEntry | None, type_name: str | None = None) -> str | None:
        """'pointer' / 'reference' / 'interface' / 'tccom interface' when the value is an address (see is_indirect,
        the alias chain followed), else None. One rule for the dump, the decoder and the write guard."""
        def by_text(t: str | None) -> str | None:
            m = _RE_INDIRECT.match(t or "")
            return m.group(1).lower() if m else None
        k = by_text(type_name)
        if k:
            return k
        cur, hops = e, 0
        while cur is not None and hops <= 16:
            if cur.flags & F_TCCOMIFACEPTR:
                return "tccom interface"
            if cur.flags & F_REFERENCETO:
                return "reference"
            if is_interface(cur):
                return "interface"
            k = by_text(cur.name)
            if k:
                return k
            # a struct, an array (its type is the ELEMENT type), an enum or a base type: not an alias
            if cur.subitems or cur.dims or cur.enum or not cur.type or cur.type.lower() == cur.name.lower():
                return None
            k = by_text(cur.type)
            if k:
                return k                                       # TYPE PT_X : POINTER TO FB_X
            nxt = self.get(cur.type)
            if cur.size in (4, 8) and nxt is not None and (nxt.size or 0) > cur.size and (nxt.subitems or nxt.dims):
                return "pointer"     # a pointer-sized "alias" of a bigger structured type is an indirection
            cur, hops = nxt, hops + 1                          # follow the alias: TYPE A : B
        return None

    # ---------------------------------------------------------------- expand
    def leaves(self, root_path: str, type_name: str, max_leaves: int = 200_000,
               base_offset: int = 0, max_array: int = MAX_ARRAY_ELEMENTS, size: int | None = None,
               cancel: Callable[[], bool] | None = None) -> list[Leaf]:
        """size: the root's own size (the symbol's) — used where the table has no entry for its type (a POINTER TO
        is 4 bytes on a 32-bit runtime such as the CX9020, 8 on x64). cancel: polled during the walk; True raises
        WalkCancelled (N55)."""
        out: list[Leaf] = []
        self._expand(root_path, type_name, base_offset, out, max_leaves, max_array, None, 0, size, cancel)
        return out

    def _expand(self, path: str, type_name: str, off: int, out: list[Leaf], max_leaves: int, max_array: int,
                entry: DtEntry | None, depth: int, size_hint: int | None = None,
                cancel: Callable[[], bool] | None = None) -> None:
        if len(out) >= max_leaves or depth > 32:
            return
        if cancel is not None and len(out) % 1024 == 0 and cancel():
            raise WalkCancelled(f"cancelled while listing the leaves of {path} ({len(out)} so far)")
        e = entry or self.get(type_name)
        if e is None:
            # unknown to the table: treat as scalar by name; the declared size (member / element / symbol) wins over
            # the size the name implies (pointer width depends on the runtime)
            tt = T.parse_type(type_name, size_hint)
            note = "pointer/reference: the address, not the target" if _RE_INDIRECT.match(type_name or "") else "type not in datatype table"
            out.append(Leaf(path, type_name, off, size_hint or tt.size or 0, 0, None, note))
            return
        if self.is_indirect(e, type_name):
            out.append(Leaf(path, type_name if entry is None else e.type, off, e.size, e.data_type, None,
                            "pointer/reference: the address, not the target"))
            return
        if e.dims:
            if entry is not None:
                # member declared as an array: its `type` is the array type name; the element
                # type lives in that top-level entry (or in the name "ARRAY [..] OF X")
                top = self.get(e.type)
                if top is not None and top.dims:
                    self._expand(path, e.type, off, out, max_leaves, max_array, None, depth + 1, None, cancel)
                    return
                m = T._RE_ARRAY.match(e.type)
                elem_type = m.group(2).strip() if m else e.type
            else:
                elem_type = e.type
            count = e.element_count
            if count <= 0:
                return
            elem_size = e.size // count if count else 0
            if count > max_array:
                out.append(Leaf(path, e.name if entry is None else f"ARRAY OF {elem_type}", off, e.size, e.data_type, None,
                                f"array with {count} elements not expanded (limit {max_array}); read it directly",
                                elements=count))
                return
            # iterate multi-dim indices row-major
            idx = [lb for lb, _ in e.dims]
            lens = [n for _, n in e.dims]
            for linear in range(count):
                rem = linear
                coords = []
                for d in range(len(lens) - 1, -1, -1):
                    coords.append(rem % lens[d] + e.dims[d][0])
                    rem //= lens[d]
                coords.reverse()
                sub_path = f"{path}[{','.join(str(c) for c in coords)}]"
                self._expand(sub_path, elem_type, off + linear * elem_size, out, max_leaves, max_array, None, depth + 1,
                             elem_size or None, cancel)
                if len(out) >= max_leaves:
                    return
            return
        if e.subitems:
            for s in e.subitems:
                if s.flags & F_PROPITEM or s.flags & F_STATIC:
                    continue
                sub_entry = s if (s.subitems or s.dims) else None
                self._expand(f"{path}.{s.name}", s.type, off + s.offs, out, max_leaves, max_array, sub_entry, depth + 1,
                             s.size or None, cancel)
                if len(out) >= max_leaves:
                    return
            return
        # alias chain (e.g. T_MaxString -> STRING(255); on 4026 the table also carries BOOL -> BYTE / BIT)
        if e.type and e.type.lower() != e.name.lower() and self.get(e.type) is not None and not e.enum:
            tgt = self.get(e.type)
            if tgt is not None and not tgt.subitems and not tgt.dims and not tgt.enum:
                # scalar alias: keep the declared name (BOOL, T_MaxString), take the storage from the base type
                out.append(Leaf(path, e.name, off, e.size or tgt.size, tgt.data_type or e.data_type, None))
                return
            self._expand(path, e.type, off, out, max_leaves, max_array, None, depth + 1, None, cancel)
            return
        out.append(Leaf(path, e.name if entry is None else e.type, off, e.size, e.data_type, e.enum or None))

    # ---------------------------------------------------------------- decode
    def decode_tree(self, buf: bytes, type_name: str, off: int = 0, entry: DtEntry | None = None,
                    max_array: int = MAX_ARRAY_ELEMENTS, depth: int = 0, size: int | None = None) -> Any:
        """size: the declared size where the table has no entry (see leaves)."""
        e = entry or self.get(type_name)
        if e is None and _RE_INDIRECT.match(type_name or ""):
            n = size or T.parse_type(type_name, None).size or 8
            return T.fmt_pointer(int.from_bytes(buf[off:off + n], "little"), n) if n in (4, 8) and len(buf) >= off + n else None
        if e is None or depth > 32:
            return self._decode_scalar(buf, off, type_name, None, None)
        if self.is_indirect(e, type_name if entry is None else e.type):
            return T.fmt_pointer(int.from_bytes(buf[off:off + e.size], "little"), e.size) if e.size in (4, 8) else None
        if e.dims:
            if entry is not None:
                top = self.get(e.type)
                if top is not None and top.dims:
                    return self.decode_tree(buf, e.type, off, None, max_array, depth + 1)
                m = T._RE_ARRAY.match(e.type)
                elem_type = m.group(2).strip() if m else e.type
            else:
                elem_type = e.type
            count = e.element_count
            if count <= 0:
                return []
            if count > max_array:
                return not_expanded_value(buf[off:off + HEAD_BYTES], e.size, count,
                                          f"array with {count} elements (limit {max_array}); read it directly")
            elem_size = e.size // count
            flat = [self.decode_tree(buf, elem_type, off + i * elem_size, None, max_array, depth + 1, elem_size or None)
                    for i in range(count)]
            if len(e.dims) > 1:
                return T._reshape(flat, [(lb, lb + n - 1) for lb, n in e.dims])
            return flat
        if e.subitems:
            d: dict[str, Any] = {}
            for s in e.subitems:
                if s.flags & F_PROPITEM or s.flags & F_STATIC:
                    continue
                sub_entry = s if (s.subitems or s.dims) else None
                d[s.name] = self.decode_tree(buf, s.type, off + s.offs, sub_entry, max_array, depth + 1, s.size or None)
            return d
        if e.type and e.type.lower() != e.name.lower() and self.get(e.type) is not None and not e.enum:
            return self.decode_tree(buf, e.type, off, None, max_array, depth + 1)
        if (e.size or 0) > MAX_LEAF_BYTES:
            return not_expanded_value(buf[off:off + HEAD_BYTES], e.size, None, f"larger than {MAX_LEAF_BYTES} bytes")
        return self._decode_scalar(buf, off, e.name if entry is None else e.type, e, e.enum or None)

    def decode_leaf(self, buf: bytes, leaf: "Leaf") -> Any:
        """The value of one leaf from buf (buf holds at least leaf.offset + leaf.need bytes)."""
        if leaf.not_expanded:
            reason = (f"array with {leaf.elements} elements (limit {MAX_ARRAY_ELEMENTS}); read it directly"
                      if leaf.elements is not None else f"larger than {MAX_LEAF_BYTES} bytes")
            return not_expanded_value(buf[leaf.offset:leaf.offset + HEAD_BYTES], leaf.size, leaf.elements, reason)
        e = self.get(leaf.type)
        if self.is_indirect(e, leaf.type) and leaf.size in (4, 8):
            return T.fmt_pointer(int.from_bytes(buf[leaf.offset:leaf.offset + leaf.size], "little"), leaf.size)
        if e is None or e.size != leaf.size:
            e = DtEntry(leaf.type, "", leaf.size, 0, leaf.data_type or (e.data_type if e else 0), 0, "", enum=dict(leaf.enum or {}))
        return self._decode_scalar(buf, leaf.offset, leaf.type, e, leaf.enum or (e.enum or None))

    @staticmethod
    def _decode_scalar(buf: bytes, off: int, type_name: str, e: DtEntry | None, enum: dict[int, str] | None) -> Any:
        size = e.size if e else None
        tt = T.parse_type(type_name, size)
        if tt.ctype is None or tt.opaque:
            if e is not None and e.data_type in (2, 3, 16, 17, 18, 19, 20, 21, 4, 5, 30, 31, 33):
                return _decode_adst(buf, off, e.data_type, e.size, enum)
            return {"raw_hex": buf[off:off + (size or 0)].hex()}
        n = tt.size or 0
        chunk = buf[off:off + n]
        if len(chunk) < n:
            return None
        try:
            if tt.kind == "string":
                return chunk.split(b"\x00", 1)[0].decode("latin-1", "replace")
            if tt.kind == "wstring":
                return chunk.decode("utf-16-le", "replace").split("\x00", 1)[0]
            if tt.kind == "array":
                import ctypes
                vals = list(tt.ctype.from_buffer_copy(chunk))
                return T.decode(tt, vals)
            import ctypes
            val = tt.ctype.from_buffer_copy(chunk).value
            if tt.kind == "bool":
                val = bool(val)
            dec = T.decode(tt, val)
            if enum and isinstance(dec, int) and not isinstance(dec, bool):
                return {"value": dec, "name": enum.get(dec, f"?{dec}")}
            return dec
        except Exception:  # noqa: BLE001
            return {"raw_hex": chunk.hex()}


def _decode_adst(buf: bytes, off: int, adst: int, size: int, enum: dict[int, str] | None) -> Any:
    fmt = {2: "<h", 3: "<i", 16: "<b", 17: "<B", 18: "<H", 19: "<I", 20: "<q", 21: "<Q", 4: "<f", 5: "<d"}.get(adst)
    chunk = buf[off:off + size]
    if adst == 30:
        return chunk.split(b"\x00", 1)[0].decode("latin-1", "replace")
    if adst == 31:
        return chunk.decode("utf-16-le", "replace").split("\x00", 1)[0]
    if adst == 33:
        return bool(chunk[0]) if chunk else None
    if fmt and len(chunk) >= struct.calcsize(fmt):
        v = struct.unpack_from(fmt, chunk)[0]
        if enum:
            return {"value": v, "name": enum.get(v, f"?{v}")}
        return v
    return {"raw_hex": chunk.hex()}


# ----------------------------------------------------------------------------
def encode_entry(e: DtEntry) -> bytes:
    """Inverse of parse_entry — used by tests/fakes to build a table."""
    name, typ, com = e.name.encode(), e.type.encode(), e.comment.encode()
    body = name + b"\x00" + typ + b"\x00" + com + b"\x00"
    for lb, n in e.dims:
        body += struct.pack("<iI", lb, n)
    for s in e.subitems:
        body += encode_entry(s)
    flags = e.flags
    tail = b""
    if e.guid:
        flags |= F_TYPEGUID
        tail += guid_bytes(e.guid)
    if flags & F_METHODINFOS:           # method table: count only (parse_entry skips the entries by length)
        tail += struct.pack("<H", 0)
    if e.attributes:
        flags |= F_ATTRIBUTES
        tail += struct.pack("<H", len(e.attributes))
        for k, v in e.attributes.items():
            tail += bytes([len(k), len(v)]) + k.encode() + b"\x00" + v.encode() + b"\x00"
    if e.enum:
        flags |= F_ENUMINFOS
        tail += struct.pack("<H", len(e.enum))
        for val, nm in e.enum.items():
            tail += bytes([len(nm)]) + nm.encode() + b"\x00" + int(val).to_bytes(e.size, "little", signed=True)
    total = HDR.size + len(body) + len(tail)
    return HDR.pack(total, 1, 0, 0, e.size, e.offs, e.data_type, flags, len(name), len(typ), len(com),
                    len(e.dims), len(e.subitems)) + body + tail
