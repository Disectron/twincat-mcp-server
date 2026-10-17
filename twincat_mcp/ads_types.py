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

import struct
from dataclasses import dataclass, field
from typing import Any

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
        return d


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


class TypeSystem:
    def __init__(self, table: dict[str, DtEntry]):
        self.table = table

    def get(self, type_name: str) -> DtEntry | None:
        return self.table.get(type_name.strip().lower())

    # ---------------------------------------------------------------- expand
    def leaves(self, root_path: str, type_name: str, max_leaves: int = 200_000,
               base_offset: int = 0, max_array: int = MAX_ARRAY_ELEMENTS) -> list[Leaf]:
        out: list[Leaf] = []
        self._expand(root_path, type_name, base_offset, out, max_leaves, max_array, None, 0)
        return out

    def _expand(self, path: str, type_name: str, off: int, out: list[Leaf], max_leaves: int, max_array: int,
                entry: DtEntry | None, depth: int) -> None:
        if len(out) >= max_leaves or depth > 32:
            return
        e = entry or self.get(type_name)
        if e is None:
            # unknown to the table: treat as scalar by name
            tt = T.parse_type(type_name, None)
            out.append(Leaf(path, type_name, off, tt.size or 0, 0, None, "type not in datatype table"))
            return
        if e.flags & F_REFERENCETO:
            out.append(Leaf(path, e.name if entry is None else e.type, off, e.size, e.data_type, None, "reference/pointer"))
            return
        if e.dims:
            if entry is not None:
                # member declared as an array: its `type` is the array type name; the element
                # type lives in that top-level entry (or in the name "ARRAY [..] OF X")
                top = self.get(e.type)
                if top is not None and top.dims:
                    self._expand(path, e.type, off, out, max_leaves, max_array, None, depth + 1)
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
                                f"array with {count} elements not expanded (limit {max_array}); read it directly"))
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
                self._expand(sub_path, elem_type, off + linear * elem_size, out, max_leaves, max_array, None, depth + 1)
                if len(out) >= max_leaves:
                    return
            return
        if e.subitems:
            for s in e.subitems:
                if s.flags & F_PROPITEM or s.flags & F_STATIC:
                    continue
                sub_entry = s if (s.subitems or s.dims) else None
                self._expand(f"{path}.{s.name}", s.type, off + s.offs, out, max_leaves, max_array, sub_entry, depth + 1)
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
            self._expand(path, e.type, off, out, max_leaves, max_array, None, depth + 1)
            return
        out.append(Leaf(path, e.name if entry is None else e.type, off, e.size, e.data_type, e.enum or None))

    # ---------------------------------------------------------------- decode
    def decode_tree(self, buf: bytes, type_name: str, off: int = 0, entry: DtEntry | None = None,
                    max_array: int = MAX_ARRAY_ELEMENTS, depth: int = 0) -> Any:
        e = entry or self.get(type_name)
        if e is None or depth > 32:
            return self._decode_scalar(buf, off, type_name, None, None)
        if e.flags & F_REFERENCETO or is_interface(e):
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
                return {"array_elements": count, "note": "not expanded", "raw_hex": buf[off:off + e.size].hex()[:512]}
            elem_size = e.size // count
            flat = [self.decode_tree(buf, elem_type, off + i * elem_size, None, max_array, depth + 1) for i in range(count)]
            if len(e.dims) > 1:
                return T._reshape(flat, [(lb, lb + n - 1) for lb, n in e.dims])
            return flat
        if e.subitems:
            d: dict[str, Any] = {}
            for s in e.subitems:
                if s.flags & F_PROPITEM or s.flags & F_STATIC:
                    continue
                sub_entry = s if (s.subitems or s.dims) else None
                d[s.name] = self.decode_tree(buf, s.type, off + s.offs, sub_entry, max_array, depth + 1)
            return d
        if e.type and e.type.lower() != e.name.lower() and self.get(e.type) is not None and not e.enum:
            return self.decode_tree(buf, e.type, off, None, max_array, depth + 1)
        return self._decode_scalar(buf, off, e.name if entry is None else e.type, e, e.enum or None)

    def decode_leaf(self, buf: bytes, leaf: "Leaf") -> Any:
        e = self.get(leaf.type)
        if e is not None and (is_interface(e) or e.flags & F_REFERENCETO) and e.size == leaf.size:
            return T.fmt_pointer(int.from_bytes(buf[leaf.offset:leaf.offset + e.size], "little"), e.size)
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
