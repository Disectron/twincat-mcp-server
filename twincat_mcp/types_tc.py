"""TwinCAT data-type handling.

Maps the type string reported by the ADS symbol table (``INT``, ``LREAL``,
``STRING(80)``, ``ARRAY [0..9] OF REAL``, ``ARRAY [1..3, 0..1] OF INT``,
``TIME``, ``DT``, ``POINTER TO ST_X``, ``E_Mode``, ``ST_Recipe`` …) to

* a ctypes type for pyads (or ``None`` for opaque types → raw bytes),
* a decoder that turns the raw Python value into something readable
  (``T#1s500ms``, ISO dates, enum ints kept as ints),
* an encoder that accepts friendly input for writes.

This is where the "complex types unsupported / TIME shown as UDINT" gap of
the existing read-only servers is closed.
"""
from __future__ import annotations

import ctypes
import datetime as _dt
import re
from dataclasses import dataclass
from typing import Any, Callable

import pyads
from pyads import constants as C

# --------------------------------------------------------------------------
# scalar table: name -> (ctypes, kind)
# kind controls decode/encode: int, uint, float, bool, str, wstr, time, ltime,
# date, dt, tod, ldate, ldt, ltod
# --------------------------------------------------------------------------
_SCALARS: dict[str, tuple[Any, str]] = {
    "BOOL": (C.PLCTYPE_BOOL, "bool"),
    "BIT": (C.PLCTYPE_BOOL, "bool"),
    "BYTE": (C.PLCTYPE_BYTE, "uint"),
    "USINT": (C.PLCTYPE_USINT, "uint"),
    "SINT": (C.PLCTYPE_SINT, "int"),
    "WORD": (C.PLCTYPE_WORD, "uint"),
    "UINT": (C.PLCTYPE_UINT, "uint"),
    "INT": (C.PLCTYPE_INT, "int"),
    "DWORD": (C.PLCTYPE_DWORD, "uint"),
    "UDINT": (C.PLCTYPE_UDINT, "uint"),
    "DINT": (C.PLCTYPE_DINT, "int"),
    "LWORD": (C.PLCTYPE_ULINT, "uint"),
    "ULINT": (C.PLCTYPE_ULINT, "uint"),
    "LINT": (C.PLCTYPE_LINT, "int"),
    "REAL": (C.PLCTYPE_REAL, "float"),
    "LREAL": (C.PLCTYPE_LREAL, "float"),
    "TIME": (C.PLCTYPE_UDINT, "time"),
    "LTIME": (C.PLCTYPE_ULINT, "ltime"),
    "DATE": (C.PLCTYPE_UDINT, "date"),
    "LDATE": (C.PLCTYPE_ULINT, "ldate"),
    "DATE_AND_TIME": (C.PLCTYPE_UDINT, "dt"),
    "DT": (C.PLCTYPE_UDINT, "dt"),
    "LDATE_AND_TIME": (C.PLCTYPE_ULINT, "ldt"),
    "LDT": (C.PLCTYPE_ULINT, "ldt"),
    "TIME_OF_DAY": (C.PLCTYPE_UDINT, "tod"),
    "TOD": (C.PLCTYPE_UDINT, "tod"),
    "LTIME_OF_DAY": (C.PLCTYPE_ULINT, "ltod"),
    "LTOD": (C.PLCTYPE_ULINT, "ltod"),
}

_RE_STRING = re.compile(r"^STRING(?:\((\d+)\))?$", re.I)
_RE_WSTRING = re.compile(r"^WSTRING(?:\((\d+)\))?$", re.I)
_RE_ARRAY = re.compile(r"^ARRAY\s*\[(.+?)\]\s*OF\s+(.+)$", re.I)
_RE_POINTER = re.compile(r"^(POINTER|REFERENCE)\s+TO\s+(.+)$", re.I)


@dataclass
class TcType:
    name: str                 # original string
    ctype: Any                # ctypes type for pyads, or None (opaque)
    kind: str                 # scalar kind / 'string' / 'array' / 'struct' / 'pointer' / 'enum?'
    size: int | None = None   # byte size if known
    dims: list[tuple[int, int]] | None = None   # array bounds
    elem: "TcType | None" = None
    opaque: bool = False      # True -> read raw bytes
    guessed: bool = False     # N50: a name parse_type does not know, read by its size — not from the datatype table

    def describe(self) -> str:
        return self.name


# TwinCAT STRING is single-byte text in Windows-1252 (WSTRING: UTF-16). One decoder and one encoder for every path —
# ads_read, read_many, watch, the recorder, the dump, the writes (N91: pyads decoded STRING as UTF-8 and failed on 'ü',
# and wrote 'ü' as two bytes; the dump used Latin-1, which differs from 1252 in 0x80–0x9F: '€' is 0x80 in the PLC)
_CP1252_HOLES = frozenset({0x81, 0x8D, 0x8F, 0x90, 0x9D})   # bytes Windows-1252 leaves undefined: kept as Latin-1


def plc_string(raw: bytes) -> str:
    """A TwinCAT STRING's text: the bytes up to the first NUL, Windows-1252."""
    b = bytes(raw).split(b"\x00", 1)[0]
    try:
        return b.decode("cp1252")
    except UnicodeDecodeError:
        return "".join(chr(x) if x in _CP1252_HOLES else bytes([x]).decode("cp1252") for x in b)


def plc_string_bytes(text: str, size: int) -> bytes:
    """The bytes of a STRING variable of `size` bytes (capacity size - 1, NUL-terminated): Windows-1252; a character
    1252 cannot hold is refused, never replaced by '?'."""
    try:
        b = str(text).encode("cp1252")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{text!r}: {exc.object[exc.start:exc.end]!r} is not in the PLC's STRING character set "
                         "(Windows-1252) — use a WSTRING for it") from None
    if len(b) > size - 1:
        raise ValueError(f"{text!r} is {len(b)} bytes; the STRING holds {size - 1}")
    return b.ljust(size, b"\x00")


# ADS data type ids (ADST_*) → the ctype of a value of that id: what the runtime itself says a scalar is
ADST_CTYPE = {2: (C.PLCTYPE_INT, "int"), 3: (C.PLCTYPE_DINT, "int"), 16: (C.PLCTYPE_SINT, "int"),
              17: (C.PLCTYPE_USINT, "int"), 18: (C.PLCTYPE_UINT, "int"), 19: (C.PLCTYPE_UDINT, "int"),
              20: (C.PLCTYPE_LINT, "int"), 21: (C.PLCTYPE_ULINT, "int"), 4: (C.PLCTYPE_REAL, "float"),
              5: (C.PLCTYPE_LREAL, "float"), 33: (C.PLCTYPE_BOOL, "bool")}


def is_elementary(tt: TcType) -> bool:
    """A type parse_type decodes from its name alone: IEC elementary types, STRING/WSTRING, POINTER/REFERENCE TO,
    arrays of those. Everything else (enums, aliases, structs, FBs, a name it does not know) is the datatype
    table's to decode (N50: a size guess read TYPE PT_X : POINTER TO FB_X as a signed integer)."""
    if tt.kind == "array":
        return tt.elem is not None and is_elementary(tt.elem)
    return tt.kind not in ("enum", "struct") and not tt.guessed


def parse_type(type_str: str, size: int | None = None, data_type: int | None = None) -> TcType:
    """Resolve a TwinCAT type string. ``size`` (bytes, from symbol info) is
    used for enums and to size opaque structs. ``data_type``: the ADS data type id the runtime gives the symbol (or
    the datatype table its leaf) — for a name parse_type does not know, the value is read as THAT, not guessed."""
    t = type_str.strip()
    up = t.upper()
    if up in _SCALARS:
        ct, kind = _SCALARS[up]
        return TcType(t, ct, kind, ctypes.sizeof(ct))
    if m := _RE_STRING.match(t):
        n = int(m.group(1) or 80)
        return TcType(t, C.PLCTYPE_STRING, "string", n + 1)
    if m := _RE_WSTRING.match(t):
        n = int(m.group(1) or 80)
        return TcType(t, C.PLCTYPE_WSTRING, "wstring", (n + 1) * 2)
    if m := _RE_POINTER.match(t):
        # pointer size depends on target bitness; use symbol size if given
        ct = C.PLCTYPE_ULINT if (size or 8) == 8 else C.PLCTYPE_UDINT
        return TcType(t, ct, "pointer", ctypes.sizeof(ct))
    if m := _RE_ARRAY.match(t):
        bounds_txt, elem_txt = m.group(1), m.group(2)
        dims: list[tuple[int, int]] = []
        for part in bounds_txt.split(","):
            lo, hi = part.split("..")
            dims.append((int(lo.strip()), int(hi.strip())))
        count = 1
        for lo, hi in dims:
            count *= (hi - lo + 1)
        elem_size = (size // count) if (size and count) else None
        elem = parse_type(elem_txt, elem_size)
        if elem.ctype is None or elem.kind in ("string", "wstring"):
            # arrays of strings/structs: read raw and slice per element
            return TcType(t, None, "array", size, dims, elem, opaque=True)
        return TcType(t, elem.ctype * count, "array", (elem.size or 0) * count, dims, elem)
    # enum or struct/alias: the runtime's data type id when given, else by size (a guess, flagged)
    if data_type in ADST_CTYPE and size in (1, 2, 4, 8) and ctypes.sizeof(ADST_CTYPE[data_type][0]) == size:
        return TcType(t, ADST_CTYPE[data_type][0], "enum", size)
    if size in (1, 2, 4, 8) and not up.startswith("ST_") and not up.startswith("FB_"):
        # Most likely an enum (TwinCAT reports the enum type name). Base type
        # is INT unless declared otherwise; we read by size, signed.
        ct = {1: C.PLCTYPE_SINT, 2: C.PLCTYPE_INT, 4: C.PLCTYPE_DINT, 8: C.PLCTYPE_LINT}[size]
        return TcType(t, ct, "enum", size, guessed=True)
    return TcType(t, None, "struct", size, opaque=True)


# --------------------------------------------------------------------------
# decode
# --------------------------------------------------------------------------
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def _fmt_time_ms(ms: int) -> str:
    neg = ms < 0
    ms = abs(ms)
    d, ms = divmod(ms, 86_400_000)
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s: parts.append(f"{s}s")
    if ms or not parts: parts.append(f"{ms}ms")
    return ("-" if neg else "") + "T#" + "".join(parts)


def _fmt_ltime_ns(ns: int) -> str:
    us, rem_ns = divmod(ns, 1000)
    base = _fmt_time_ms(us // 1000).replace("T#", "LTIME#")
    frac = us % 1000
    if frac or rem_ns:
        base += f"{frac}us"
        if rem_ns:
            base += f"{rem_ns}ns"
    return base


def fmt_pointer(value: Any, size: int | None = 8) -> str | None:
    """An address as the PLC shows it: 0x-hex of the full width. A NULL pointer / unassigned interface is the address
    0 and prints as such (0x00000000 / 0x0000000000000000) — never None, which elsewhere means "no value" (an empty
    cell, an unreadable leaf): N51. None only when there is no integer to format."""
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return None
    width = (size or 8)
    iv &= (1 << (8 * width)) - 1          # a signed read of a high address must not print negative
    return f"0x{iv:0{2 * width}X}"


def decode(tt: TcType, value: Any) -> Any:
    """Turn the raw pyads value into a JSON-friendly readable value."""
    k = tt.kind
    if k == "pointer":
        return fmt_pointer(value, tt.size)
    if k == "array" and isinstance(value, (list, tuple)):
        vals = [decode(tt.elem, v) for v in value] if tt.elem else list(value)
        if tt.dims and len(tt.dims) > 1:
            return _reshape(vals, tt.dims)
        return vals
    if k == "time":
        return {"raw_ms": int(value), "text": _fmt_time_ms(int(value))}
    if k == "ltime":
        return {"raw_ns": int(value), "text": _fmt_ltime_ns(int(value))}
    if k == "date":
        return {"raw": int(value), "text": (_EPOCH + _dt.timedelta(seconds=int(value))).strftime("D#%Y-%m-%d")}
    if k == "dt":
        return {"raw": int(value), "text": (_EPOCH + _dt.timedelta(seconds=int(value))).strftime("DT#%Y-%m-%d-%H:%M:%S")}
    if k == "tod":
        return {"raw_ms": int(value), "text": "TOD#" + _fmt_time_ms(int(value))[2:]}
    if k == "ldate":
        return {"raw_ns": int(value), "text": (_EPOCH + _dt.timedelta(microseconds=int(value) // 1000)).strftime("LD#%Y-%m-%d")}
    if k == "ldt":
        return {"raw_ns": int(value), "text": (_EPOCH + _dt.timedelta(microseconds=int(value) // 1000)).strftime("LDT#%Y-%m-%d-%H:%M:%S.%f")}
    if k == "ltod":
        return {"raw_ns": int(value), "text": "LTOD#" + _fmt_ltime_ns(int(value))[6:]}
    if k == "bool":
        return bool(value)
    if k in ("string", "wstring"):
        return value if isinstance(value, str) else str(value)
    if k == "float":
        return float(value)
    return value


def _reshape(flat: list[Any], dims: list[tuple[int, int]]) -> Any:
    sizes = [hi - lo + 1 for lo, hi in dims]

    def build(level: int, offset: int) -> Any:
        if level == len(sizes) - 1:
            return flat[offset: offset + sizes[level]]
        stride = 1
        for s in sizes[level + 1:]:
            stride *= s
        return [build(level + 1, offset + i * stride) for i in range(sizes[level])]

    return build(0, 0)


# --------------------------------------------------------------------------
# encode (for writes)
# --------------------------------------------------------------------------
_RE_TIME = re.compile(
    r"^(?:L?T(?:IME)?#)?(-)?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?(?:(\d+)us)?(?:(\d+)ns)?$",
    re.I)


def parse_time_literal(text: str, nanoseconds: bool = False) -> int:
    """'T#1s500ms' -> 1500 (ms) ; with nanoseconds=True -> ns."""
    m = _RE_TIME.match(text.strip().replace("_", ""))
    if not m or not any(m.groups()[1:]):
        raise ValueError(f"not a TIME literal: {text!r}")
    neg, d, h, mi, s, ms, us, ns = m.groups()
    total_ns = 0
    total_ns += int(d or 0) * 86_400_000_000_000
    total_ns += int(h or 0) * 3_600_000_000_000
    total_ns += int(mi or 0) * 60_000_000_000
    total_ns += int(round(float(s or 0) * 1_000_000_000))
    total_ns += int(ms or 0) * 1_000_000
    total_ns += int(us or 0) * 1000
    total_ns += int(ns or 0)
    if neg:
        total_ns = -total_ns
    return total_ns if nanoseconds else total_ns // 1_000_000


def encode(tt: TcType, value: Any) -> Any:
    """Accept friendly input and return what pyads expects for tt.ctype."""
    k = tt.kind
    if k == "array":
        if not isinstance(value, (list, tuple)):
            raise ValueError("array write needs a JSON list")
        flat = _flatten(value)
        count = array_count(tt)
        if count is not None and len(flat) != count:
            raise ValueError(f"{tt.name} has {count} elements; got {len(flat)}. A short list would zero the rest of the array "
                             f"(ADS writes the whole variable) — pass all {count} values, or write single elements by index")
        if tt.elem:
            return [encode(tt.elem, v) for v in flat]
        return flat
    if k == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            # N22: exactly what the error text documents — a write to the PLC is no place to guess intent
            if low in ("1", "true"):
                return True
            if low in ("0", "false"):
                return False
            raise ValueError(f"not a BOOL: {value!r} (use true/false or 0/1; yes/no/on/off are refused)")
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        raise ValueError(f"not a BOOL: {value!r} (use true/false or 0/1; -1 and other numbers are refused rather than coerced)")
    if k == "pointer":
        raise ValueError(f"{tt.name} is a POINTER/REFERENCE: writing an address through ADS is refused (a wrong address is an "
                         "access violation on the PLC). Write the variable it points to instead")
    if k in ("int", "uint", "enum"):
        if isinstance(value, bool):
            iv = int(value)
        elif isinstance(value, str):
            v = value.strip().replace("16#", "0x").replace("2#", "0b")
            iv = int(v, 0)
        elif isinstance(value, float):
            if value != int(value):
                raise ValueError(f"{value!r} is not an integer; {tt.name} takes whole numbers")
            iv = int(value)
        else:
            iv = int(value)
        lo, hi = int_range(tt)
        if lo is not None and not (lo <= iv <= hi):
            raise ValueError(f"{iv} is out of range for {tt.name} ({lo}..{hi}); the PLC would silently wrap it")
        return iv
    if k == "float":
        fv = float(value)
        if tt.name.upper() == "REAL" and abs(fv) > 3.4028235e38:
            raise ValueError(f"{fv!r} is out of range for REAL")
        return fv
    if k in ("string", "wstring"):
        sv = str(value)
        cap = string_capacity(tt)
        if cap is not None and len(sv) > cap:
            raise ValueError(f"string is {len(sv)} chars, {tt.name} holds at most {cap}; shorten it (ADS would reject the write "
                             "with 'parameter size not correct' or the PLC would truncate it)")
        return sv
    if k == "time":
        return parse_time_literal(value) if isinstance(value, str) else int(value)
    if k == "tod":
        return parse_time_literal(value.split("#", 1)[-1]) if isinstance(value, str) else int(value)
    if k == "ltime":
        return parse_time_literal(value, nanoseconds=True) if isinstance(value, str) else int(value)
    if k == "ltod":
        return parse_time_literal(value.split("#", 1)[-1], nanoseconds=True) if isinstance(value, str) else int(value)
    if k in ("date", "dt"):
        if isinstance(value, str):
            # D#2026-10-15 / DT#2026-10-15-14:30:00
            parts = value.split("#", 1)[-1]
            if k == "date":
                d = _dt.datetime.strptime(parts, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc)
            else:
                d = _dt.datetime.strptime(parts, "%Y-%m-%d-%H:%M:%S").replace(tzinfo=_dt.timezone.utc)
            return int((d - _EPOCH).total_seconds())
        return int(value)
    if k in ("ldate", "ldt"):
        if isinstance(value, str):
            parts = value.split("#", 1)[-1]
            fmt = "%Y-%m-%d" if k == "ldate" else "%Y-%m-%d-%H:%M:%S"
            d = _dt.datetime.strptime(parts.split(".")[0], fmt).replace(tzinfo=_dt.timezone.utc)
            return int((d - _EPOCH).total_seconds()) * 1_000_000_000
        return int(value)
    raise ValueError(f"cannot encode value for type {tt.name!r} (kind {k})")


def array_count(tt: TcType) -> int | None:
    """Number of elements of an ARRAY type (None when the bounds are unknown)."""
    if not tt.dims:
        return None
    n = 1
    for lo, hi in tt.dims:
        n *= hi - lo + 1
    return n


def string_capacity(tt: TcType) -> int | None:
    """Declared character capacity of a STRING(n)/WSTRING(n) (the byte size minus the terminator)."""
    if tt.kind == "string" and tt.size:
        return tt.size - 1
    if tt.kind == "wstring" and tt.size:
        return tt.size // 2 - 1
    return None


def int_range(tt: TcType) -> tuple[int | None, int | None]:
    """Representable range of an integer-like TwinCAT type (None, None when the size is unknown)."""
    size = tt.size
    if size is None and tt.ctype is not None:
        try:
            size = ctypes.sizeof(tt.ctype)
        except TypeError:
            size = None
    if not size or size > 8:
        return None, None
    bits = 8 * size
    if tt.kind in ("uint", "pointer"):
        return 0, (1 << bits) - 1
    if tt.kind == "int":
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if tt.kind == "enum":
        # TwinCAT enums default to INT; a 2-byte enum is signed, larger ones follow their base type (unknown here)
        return (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if size == 2 else (-(1 << (bits - 1)), (1 << bits) - 1)
    return None, None


def _flatten(v: Any) -> list[Any]:
    out: list[Any] = []
    for x in v:
        if isinstance(x, (list, tuple)):
            out.extend(_flatten(x))
        else:
            out.append(x)
    return out


def bytes_type(n: int) -> Any:
    return ctypes.c_ubyte * n


def ads_state_name(code: int) -> str:
    names = {getattr(C, n): n.replace("ADSSTATE_", "") for n in dir(C) if n.startswith("ADSSTATE_") and not n.startswith("ADSSTATEFLAG")}
    return names.get(code, f"UNKNOWN({code})")
