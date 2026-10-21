"""Real-time recorder for online PLC variables.

Runs *alongside* the PLC program: start a recording, exercise the machine
(or let ``xae_deploy`` / ``tcunit_run`` do it), query it while it runs, stop,
export. Three capture modes:

``cycle``   ADS device notifications, ADSTRANS_SERVERCYCLE: one sample per
            ``cycle_ms``, time-stamped by the TwinCAT runtime when it took the
            sample (100 ns FILETIME) — cycle-accurate, independent of this PC.
``change``  ADS device notifications, ADSTRANS_SERVERONCHA: a sample only when
            the bytes change (checked every ``cycle_ms``).
``poll``    sum-read every ``cycle_ms`` from a background thread, timestamped
            on this PC (``time_source: "pc"`` — never presented as PLC time).

Block notifications (format 2)
------------------------------
The selected leaves are grouped by their memory address into contiguous
*blocks* (an FB, a struct, an array, neighbouring globals) and ONE notification
is registered per block, by index group/offset. The runtime sends the whole
block per sample; leaves are decoded here. Measured on a 1 ms task: 300 UDINTs
as one block were lossless, the same 300 as separate notifications lost ~95 %
(registration timeouts, router overflow). Per-symbol notifications are gone.

File: ``~/.twincat_mcp/recordings/<id>.jsonl``
  line 1  ``{"_header": true, "format": 2, "time_source": "plc"|"pc", "symbols": [...], "blocks": [...]}``
  ``{"_t0": {"filetime": <PLC FILETIME of the first sample>, "utc": "..."}}`` when the first sample arrives
  block sample  ``[t_ms, "#<block id>", "<base64 of the raw bytes the PLC sent>"]``
  poll sample   ``[t_ms, <symbol id>, value]``
  last line ``{"_footer": true, ..., "sha256": ..., "hmac": ...}``
``t_ms`` is relative to ``_t0`` with 0.1 µs resolution (4 decimals), so the PLC's
own FILETIME of every sample is ``filetime + round(t_ms * 1e4)``. The raw bytes
are what the runtime transmitted, not a decoded copy. ``sha256`` covers every line
before the footer and ``hmac`` signs it with a key kept in the recordings folder:
``rec_verify`` proves the file is unchanged since the recording ended.

Losses are counted, never hidden: queue overflow (``dropped``), samples still
queued when a stop timed out (``dropped_on_stop``), and — in cycle mode — gaps in
the PLC timestamps per block (``missing_samples``). Live statistics are computed
when the ingest thread keeps up; under load it writes the raw samples first and
skips the statistics (``stats_coverage`` < 1) — ``rec_stats`` then recomputes them
from the file, so the numbers stay exact.
"""
from __future__ import annotations

import base64
import ctypes
import datetime as dt
import hashlib
import heapq
import hmac as _hmac
import json
import math
import os
import queue
import re
import secrets
import stat
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import pyads
from pyads import constants as C

from . import types_tc as T
from .ads_backend import ADSIGRP_SYM_VERSION, AdsBackend, AdsError, ProgramChanged, SymbolEntry, _fmt_exc
from .ads_types import POINTER_NOTE

FILETIME_EPOCH_OFFSET_100NS = 116444736000000000  # 1601 → 1970
MERGE_GAP_BYTES = 256          # leaves closer than this share one notification
MAX_BLOCK_BYTES = 16384        # one notification never carries more than this
QUEUE_BYTES = 256 << 20        # in-memory backlog cap per recording
HOLD_MARGIN_MS = 50.0            # transport margin on top of max_delay_ms for a block's late delivery
VERSION_CHECK_MS = 20.0          # R43: how often the symbol version is read while block samples wait for it
DRAIN_TIMEOUT_S = 60.0         # a stop writes the backlog for at most this long
TAIL_WAIT_S = 1.0             # after the duration: wait at most this long for in-flight samples up to the cut


def _ticks(ms: float) -> int:
    """PLC-relative ms (4 decimals in the file) → integer 100 ns ticks, the native engine's unit."""
    return int(round(ms * 10_000))
LIVE_VALUES_PER_S = 20_000     # above this (leaves × samples/s) nothing is decoded while recording (see Recording)
_HDR_DATA_OFFSET = 16          # SAdsNotificationHeader: u32 handle, u64 timestamp, u32 size, data

_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "==": lambda a, b: a == b, "!=": lambda a, b: a != b, ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
}
_RE_COND = re.compile(r"^\s*(?P<sym>[A-Za-z_][\w\.\[\]]*)\s*(?P<op>==|!=|>=|<=|>|<)\s*(?P<val>.+?)\s*$")


class RecorderError(RuntimeError):
    pass


@dataclass
class Condition:
    symbol: str
    op: str
    value: Any
    index: int | None = None        # element of a recorded array (flat, row-major), e.g. GVL_Test.aCounter[1]
    edge: bool = False              # True: fires only when the condition BECOMES true (not if it already holds)
    _prev: bool | None = None

    @classmethod
    def parse(cls, text: str, types: dict[str, T.TcType], edge: bool = False,
              enum_resolver: Callable[[str, str], Any] | None = None) -> "Condition":
        m = _RE_COND.match(text)
        if not m:
            raise RecorderError(f"condition {text!r} must look like 'MAIN.bDone == TRUE' or 'MAIN.nStep >= 4'")
        sym = m.group("sym")
        key = next((k for k in types if k.lower() == sym.lower()), None)
        index = None
        tt = types.get(key) if key else None
        if key is None:
            # an element of an array that is recorded as a whole: MAIN.aCounter[3] / MAIN.aGrid[1,2]
            em = re.match(r"^(?P<base>.+)\[(?P<idx>[\d,\s]+)\]$", sym)
            base = next((k for k in types if em and k.lower() == em.group("base").lower()), None) if em else None
            if base is not None and types[base].kind == "array" and types[base].dims:
                tb = types[base]
                idx = [int(x) for x in em.group("idx").split(",")]
                if len(idx) != len(tb.dims) or any(not (lo <= i <= hi) for i, (lo, hi) in zip(idx, tb.dims)):
                    raise RecorderError(f"{sym!r}: index outside {tb.name}")
                flat = 0
                for i, (lo, hi) in zip(idx, tb.dims):
                    flat = flat * (hi - lo + 1) + (i - lo)
                key, index, tt = base, flat, tb.elem
        if key is None:
            raise RecorderError(f"condition symbol {sym!r} is not part of the recording; add it to symbols")
        rawv = m.group("val").strip()
        raw = rawv.strip("'\"")
        # R10: 'GVL_Sim.nCycle >>> import os' parsed as '>' against the text '>> import os' and silently never fired —
        # the value must be a literal of the symbol's type, else the condition is refused
        if re.match(r"^[=<>!]", rawv):
            raise RecorderError(f"condition {text!r}: operator not understood (use ==, !=, >, >=, <, <=)")
        if tt is None:
            raise RecorderError(f"condition {text!r}: the type of {sym!r} is unknown, so its value cannot be checked")
        if tt.ctype is None and tt.kind not in ("string", "wstring"):
            raise RecorderError(f"condition {text!r}: {sym!r} is a {tt.name} — conditions compare scalars")
        lit: Any = raw
        if tt.kind in ("enum", "int", "uint") and enum_resolver is not None \
                and not re.fullmatch(r"[-+]?(\d+|0x[0-9a-fA-F]+|16#[0-9a-fA-F_]+|2#[01_]+)", raw):
            try:
                lit = enum_resolver(tt.name, raw)               # 'E_State.Running' → its integer
            except Exception:  # noqa: BLE001
                lit = raw
        try:
            val = T.encode(tt, lit)
        except Exception as exc:  # noqa: BLE001
            raise RecorderError(f"condition {text!r}: {raw!r} is not a valid {tt.name} value ({exc})") from exc
        return cls(key, m.group("op"), val, index, edge)

    def fires(self, value: Any) -> bool:
        """test() for levels; with edge=True only the transition false → true (the first evaluation never fires)."""
        now = self.test(value)
        if not self.edge:
            return now
        prev, self._prev = self._prev, now
        return bool(now and prev is False)

    def test(self, value: Any) -> bool:
        if self.index is not None:
            flat = _flatten_values(value) if isinstance(value, list) else None
            if not flat or self.index >= len(flat):
                return False
            value = flat[self.index]
        try:
            # an enum read through the datatype table is {value, name}: compared by its value like a TIME by its count
            return _OPS[self.op](T.raw_value(value), self.value)
        except Exception:  # noqa: BLE001
            return False


@dataclass
class SymStats:
    count: int = 0
    first: Any = None
    last: Any = None
    last_t: float | None = None
    changes: int = 0
    num_min: float | None = None
    num_max: float | None = None
    num_sum: float = 0.0
    num_n: int = 0
    rising: int = 0
    falling: int = 0
    first_edges: list[dict[str, Any]] = field(default_factory=list)
    runs: list[list[Any]] = field(default_factory=list)   # [t_start, t_end, value]
    runs_truncated: bool = False

    def ingest(self, t: float, v: Any, raw_numeric: float | None, max_runs: int) -> None:
        self.count += 1
        if self.count == 1:
            self.first = v
        if raw_numeric is not None and not (isinstance(raw_numeric, float) and math.isnan(raw_numeric)):
            self.num_n += 1
            self.num_sum += raw_numeric
            self.num_min = raw_numeric if self.num_min is None else min(self.num_min, raw_numeric)
            self.num_max = raw_numeric if self.num_max is None else max(self.num_max, raw_numeric)
        if self.last_t is not None and v != self.last:
            self.changes += 1
            if isinstance(v, bool) and isinstance(self.last, bool):
                if v:
                    self.rising += 1
                else:
                    self.falling += 1
                if len(self.first_edges) < 50:
                    self.first_edges.append({"t_ms": round(t, 3), "edge": "rising" if v else "falling"})
        if not self.runs or self.runs[-1][2] != v:
            if self.runs:
                self.runs[-1][1] = t
            if len(self.runs) < max_runs:
                self.runs.append([t, t, v])
            else:
                self.runs_truncated = True
        else:
            self.runs[-1][1] = t
        self.last = v
        self.last_t = t

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"count": self.count, "first": self.first, "last": self.last,
                             "last_t_ms": None if self.last_t is None else round(self.last_t, 3), "changes": self.changes}
        if self.num_n:
            d.update({"min": self.num_min, "max": self.num_max, "mean": self.num_sum / self.num_n})
        if self.rising or self.falling:
            d.update({"rising_edges": self.rising, "falling_edges": self.falling, "first_edges": self.first_edges})
        return d



# ---------------------------------------------------------------------------- block codec
def _fmt_char(ct: Any) -> str | None:
    """struct format char for a ctypes simple type, by size and signedness (not by ctypes' _type_ letter,
    whose 'l'/'L' are 8 bytes on Linux but 4 in struct's standard sizes)."""
    try:
        code = ct._type_
        size = ctypes.sizeof(ct)
    except (AttributeError, TypeError):
        return None
    if not isinstance(code, str):
        return None
    if code == "?":
        return "?"
    if code == "f":
        return "f"
    if code == "d":
        return "d"
    signed = code in "bhilq"
    if code not in "bhilqBHILQ":
        return None
    return {1: "b", 2: "h", 4: "i", 8: "q"}[size] if signed else {1: "B", 2: "H", 4: "I", 8: "Q"}[size]


@dataclass
class Slot:
    """One recorded leaf inside a block."""
    sid: int
    name: str
    off: int
    size: int
    tt: T.TcType
    fmt: str | None = None
    count: int = 1

    def __post_init__(self) -> None:
        ct = self.tt.ctype
        if self.tt.kind in ("string", "wstring") or ct is None:
            return
        if self.tt.kind == "array":
            elem = getattr(ct, "_type_", None)
            n = getattr(ct, "_length_", 0)
            ch = _fmt_char(elem) if elem is not None else None
            if ch and n:
                self.fmt, self.count = f"<{n}{ch}", n
            return
        ch = _fmt_char(ct)
        if ch:
            self.fmt = "<" + ch

    def decode(self, data: bytes) -> Any:
        chunk = data[self.off:self.off + self.size]
        if len(chunk) < self.size:
            return None
        tt = self.tt
        if tt.kind == "string":
            return T.plc_string(chunk)
        if tt.kind == "wstring":
            return chunk.decode("utf-16-le", "replace").split("\x00", 1)[0]
        try:
            if self.fmt:
                vals = struct.unpack_from(self.fmt, chunk)
                raw: Any = list(vals) if tt.kind == "array" else vals[0]
            else:
                obj = tt.ctype.from_buffer_copy(chunk)
                raw = list(obj) if tt.kind == "array" else obj.value
        except Exception:  # noqa: BLE001
            return {"raw_hex": chunk.hex()}
        if tt.kind == "bool":
            raw = bool(raw)
        return T.decode(tt, raw)


@dataclass
class Block:
    """A contiguous memory range sampled by one notification."""
    bid: int
    igrp: int
    ioffs: int
    size: int
    slots: list[Slot]
    # timeline bookkeeping (cycle mode)
    samples: int = 0
    first_t: float | None = None
    last_t: float | None = None
    min_step: float | None = None
    dup_stamps: int = 0
    gaps: list[list[float]] = field(default_factory=list)   # [t_ms, est. missing] — first 20
    prev: bytes | None = None
    registered: bool = False
    error: str | None = None
    last_wall: float | None = None          # monotonic time the last sample arrived (D14: silence estimate)
    seen_t: float | None = None             # newest PLC time received, kept or not (an end is reached at or past it)
    late: int = 0                           # samples written before a row of another block with a later stamp

    def header(self) -> dict[str, Any]:
        return {"id": self.bid, "igrp": self.igrp, "ioffs": self.ioffs, "size": self.size,
                "slots": [[s.sid, s.off, s.size] for s in self.slots]}

    def note_stamp(self, t: float) -> int:
        """Record the sample time; returns how many samples are missing right before it (0: none)."""
        missing = 0
        if self.last_t is not None:
            step = t - self.last_t
            if step <= 0:
                self.dup_stamps += 1
            else:
                if self.min_step is None or step < self.min_step:
                    self.min_step = step
                if self.min_step and step > 1.5 * self.min_step:
                    missing = round(step / self.min_step) - 1
                    if len(self.gaps) < 20:
                        self.gaps.append([round(self.last_t, 4), missing])
        else:
            self.first_t = t
        self.last_t = t
        self.samples += 1
        return missing

    # N60: the counts below are integer arithmetic on 100 ns ticks — the native engine's units and formulas — so the
    # same file gives the same numbers in both (float ms made 3.0 periods read as 2.9999 → one sample less).
    #
    # A block with fewer than two samples has no period of its own. It is not complete for that: the other blocks'
    # smallest period (or the task cycle) stands in — the engine's step_of — and a block that never delivered misses the
    # whole window, from `origin` (the window start, or the moment every delivering block had delivered) to the end.
    # Returning 0 for such a block let a recording in which one block delivered once, or never, read as COMPLETE.
    def period(self, fallback: float | None = None) -> float | None:
        return self.min_step or fallback

    def missing(self, end_t: float | None = None, start_t: float | None = None, *,
                fallback: float | None = None, origin: float | None = None) -> int:
        """Holes between the first and last sample, plus the samples this block should have delivered before its first
        one (window start known: trigger − pre_trigger_ms) and after its last one (window end known: the stop's PLC
        time, duration / post-trigger cut, or the other blocks' last sample) — field report N11: a recording that went
        quiet halfway must not look complete. One period of allowance at the tail, none at the head."""
        return (self.inner_missing() + self.tail_missing(end_t, fallback, origin)
                + self.head_missing(start_t, fallback))

    def inner_missing(self) -> int:
        """Holes between the first and last sample (needs the block's own period: two samples or more)."""
        if self.samples < 2 or not self.min_step or self.first_t is None or self.last_t is None:
            return 0
        step = _ticks(self.min_step)
        if step <= 0:
            return 0
        expected = int(math.floor((_ticks(self.last_t) - _ticks(self.first_t)) / step + 0.5)) + 1   # llround
        return max(0, expected - self.samples)

    def head_missing(self, start_t: float | None, fallback: float | None = None) -> int:
        """Samples the window needed before this block's first one: floor((first − start) / period), as the engine."""
        p = self.period(fallback)
        if start_t is None or not p or self.first_t is None or self.first_t <= start_t:
            return 0
        step = _ticks(p)
        return (_ticks(self.first_t) - _ticks(start_t)) // step if step > 0 else 0

    def tail_missing(self, end_t: float | None, fallback: float | None = None, origin: float | None = None) -> int:
        """Samples missing after this block's last one up to the window end, one period of allowance:
        floor((end − last) / period) − 1, as the engine. No other allowance: a stop on request ends at a PLC time
        both engines wait for (N56), so a block that ends early has lost samples. A block that never delivered misses
        the whole window: floor((end − origin) / period) + 1, as the engine."""
        p = self.period(fallback)
        step = _ticks(p) if p else 0
        if end_t is None or step <= 0:
            return 0
        if self.last_t is None:
            if origin is None or end_t < origin:
                return 0
            return (_ticks(end_t) - _ticks(origin)) // step + 1
        if end_t <= self.last_t:
            return 0
        return max(0, (_ticks(end_t) - _ticks(self.last_t)) // step - 1)


def end_not_waited_for(footer: dict[str, Any]) -> bool:
    """True when the file's window end is not a PLC time every block was waited for, so each block ended where its
    notification happened to be and only the earliest block end is a time every block is known to have delivered:
    - ≤ 0.8.9 native stop on request: manual_stop, with the LATEST block end written as window_end_ms (so a missing end
      cannot be the test — that read every such file as a normal one and passed a short tail);
    - Python without a window end: ≤ 0.8.9 stop on request, stop_when or max_events cut the queue at once; today only a
      plain stop (start failure, fallback) or a file without a footer.
    The present engines end a stop on request at a PLC time and say so (stop_on_request); native stop_when / max_events
    end at the firing sample's time, which every block has reached (it writes in PLC order); Python waits for it."""
    if footer.get("stop_on_request") or footer.get("stop_at"):
        return False
    if footer.get("manual_stop"):
        return True
    return footer.get("window_end_ms") is None


def fallback_period(blocks: Iterable[Block], cycle_ms: float | None = None) -> float | None:
    """The period that stands in for a block with fewer than two samples: the smallest period of the other blocks, else
    the task cycle (the native engine's step_of)."""
    steps = [b.min_step for b in blocks if b.min_step]
    return min(steps) if steps else (cycle_ms or None)


def window_origin(blocks: Iterable[Block], start: float | None) -> float | None:
    """Where the window starts for a block that never delivered: the window start of a triggered recording, else the
    moment every delivering block had its first sample (the native engine's origin)."""
    if start is not None:
        return start
    firsts = [b.first_t for b in blocks if b.first_t is not None]
    return max(firsts) if firsts else None


def unprovable_blocks(blocks: Iterable[Block], fallback: float | None) -> list[int]:
    """Blocks whose completeness cannot be shown at all: never delivered, or one sample with no period to judge it by."""
    return [b.bid for b in blocks if b.samples == 0 or (b.samples < 2 and not b.period(fallback))]


# ------------------------------------------------------------------ selection patterns (A5)
_ANCESTOR_CUT = re.compile(r"(?=[.\[])")


def selection_regex(pattern: str) -> re.Pattern[str]:
    """One pattern language for rec_start and rec_scan: case-insensitive glob where '*' and '?' are the only
    wildcards — '[' and ']' are literal, so 'MAIN.arr[3].*' means element 3 (fnmatch read [3] as a character class)."""
    out = []
    for ch in pattern:
        out.append(".*" if ch == "*" else "." if ch == "?" else re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.I | re.S)


def _literal_prefix(pattern: str) -> str:
    m = re.search(r"[*?]", pattern)
    return (pattern[:m.start()] if m else pattern).lower()


def path_selected(rx: re.Pattern[str], path: str) -> bool:
    """A leaf is selected when the pattern matches the leaf or any of its ancestors (root, FB, struct member, array
    element): 'MAIN.fbFill' selects every leaf below it, '*bOpen' selects each bOpen however deep."""
    if rx.match(path):
        return True
    cuts = [m.start() for m in _ANCESTOR_CUT.finditer(path)]
    return any(rx.match(path[:c]) for c in cuts if c > 0)


def pattern_leaves(ads: AdsBackend, pattern: str, max_leaves: int, port: int | None = None,
                   prefix: str | None = None) -> list[tuple[SymbolEntry, list[Any]]]:
    """(root, selected leaves) for every root symbol that can hold a match. System symbols
    (TwinCAT_SystemInfoVarList …) are only searched when the pattern names them literally."""
    rx = selection_regex(pattern)
    lp = _literal_prefix(pattern)
    pre = (prefix or "").rstrip(".").lower()
    out: list[tuple[SymbolEntry, list[Any]]] = []
    n = 0
    for r in ads.roots(None, True, port):
        low = r.name.lower()
        if pre and not (low == pre or low.startswith(pre + ".") or low.startswith(pre + "[") or pre.startswith(low + ".")
                        or pre.startswith(low + "[")):
            continue
        if lp and not (low.startswith(lp) or lp.startswith(low)):
            continue
        if not lp and _is_system_root(r.name):
            continue
        root, leaves = ads.leaves(r.name, max_leaves + 1, port)
        sel = [lf for lf in leaves if path_selected(rx, lf.path)
               and (not pre or lf.path.lower() == pre or lf.path.lower().startswith((pre + ".", pre + "[")))]
        if sel:
            out.append((root, sel))
            n += len(sel)
            if n > max_leaves:
                break
    return out


def _is_system_root(name: str) -> bool:
    from .ads_backend import _is_system_symbol
    return _is_system_symbol(name)


def build_blocks(syms: list[SymbolEntry], sids: dict[str, int], types: dict[str, T.TcType],
                 merge_gap: int = MERGE_GAP_BYTES, max_bytes: int = MAX_BLOCK_BYTES) -> list[Block]:
    """Group leaves by index group and address into blocks; a leaf larger than max_bytes gets its own block."""
    by_grp: dict[int, list[SymbolEntry]] = {}
    for s in syms:
        by_grp.setdefault(s.index_group, []).append(s)
    blocks: list[Block] = []
    for igrp, lst in by_grp.items():
        lst.sort(key=lambda s: (s.index_offset, -s.size))
        cur: list[SymbolEntry] = []
        start = end = 0
        for s in lst:
            s_end = s.index_offset + max(s.size, 1)
            if cur and s.index_offset <= end + merge_gap and max(end, s_end) - start <= max_bytes:
                cur.append(s)
                end = max(end, s_end)
                continue
            if cur:
                blocks.append(_mk_block(len(blocks), igrp, start, end, cur, sids, types))
            cur, start, end = [s], s.index_offset, s_end
        if cur:
            blocks.append(_mk_block(len(blocks), igrp, start, end, cur, sids, types))
    return blocks


def _mk_block(bid: int, igrp: int, start: int, end: int, members: list[SymbolEntry],
              sids: dict[str, int], types: dict[str, T.TcType]) -> Block:
    slots = [Slot(sids[s.name], s.name, s.index_offset - start, s.size, types[s.name]) for s in members]
    return Block(bid, igrp, start, end - start, slots)


def blocks_from_header(header: dict[str, Any]) -> tuple[list[str], dict[int, Block]]:
    syms = header.get("symbols", [])
    names = [s["name"] for s in syms]
    types = [T.parse_type(s.get("type") or "", s.get("size")) for s in syms]
    blocks: dict[int, Block] = {}
    for b in header.get("blocks", []):
        slots = [Slot(sid, names[sid], off, size, types[sid]) for sid, off, size in b["slots"]]
        blocks[b["id"]] = Block(b["id"], b["igrp"], b["ioffs"], b["size"], slots)
    return names, blocks


def recording_key(root: Path) -> bytes:
    """Per-install HMAC key for recording files (created once, kept next to the recordings)."""
    p = root / ".recording.key"
    try:
        return bytes.fromhex(p.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        root.mkdir(parents=True, exist_ok=True)
        k = secrets.token_bytes(32)
        p.write_text(k.hex(), encoding="ascii")
        return k


# ---------------------------------------------------------------------------- recording
class Recording:
    def __init__(self, rid: str, name: str, symbols: list[SymbolEntry], mode: str, cycle_ms: float,
                 path: Path, ring_events: int, max_runs: int, duration_s: float | None,
                 max_events: int | None, start_when: Condition | None, stop_when: Condition | None,
                 pre_trigger_ms: float, meta: dict[str, Any], post_trigger_s: float | None = None,
                 key: bytes | None = None):
        self.id, self.name, self.mode, self.cycle_ms, self.path = rid, name, mode, cycle_ms, path
        self.symbols = symbols
        self.sids: dict[str, int] = {s.name: i for i, s in enumerate(symbols)}
        self.types: dict[str, T.TcType] = {s.name: T.parse_type(s.type, s.size, getattr(s, "data_type", 0))
                                           for s in symbols}
        self.blocks: list[Block] = []
        self.slot_of: dict[str, tuple[Block, Slot]] = {}
        self.time_source = "pc" if mode == "poll" else "plc"
        self.ring: deque[tuple[float, str, Any]] = deque(maxlen=ring_events)
        self.stats: dict[str, SymStats] = {s.name: SymStats() for s in symbols}
        self.max_runs = max_runs
        self.duration_s, self.max_events, self.post_trigger_s = duration_s, max_events, post_trigger_s
        self.start_when, self.stop_when, self.pre_trigger_ms = start_when, stop_when, pre_trigger_ms
        self.armed = start_when is not None
        self.pre: deque[tuple[float, Any, Any]] = deque()
        self.meta = meta
        self.key = key
        self.manifest: dict[str, Any] | None = None
        self.type_info: dict[str, dict[str, Any]] = {}   # R3: datatype GUID / layout per symbol
        # D11: the recorded cycle counter must advance; frozen values (PLC STOP) are counted as stalled, not as data
        self.stall_cfg: dict[str, Any] | None = None     # {"block", "off", "size", "after_100ns"} (also sent to the native engine)
        self.scratch = False                             # helper recording: not sealed (deleted right after)
        self._stall_block: Block | None = None
        self._stall_prev: bytes | None = None
        self._stall_adv_t: float | None = None
        self._stall_rep = 0
        self._stall_open = False
        self.stalls = 0
        self.stalled_samples = 0
        self.stall_ms = 0.0
        self.stall_list: list[list[float]] = []
        self.q: "queue.Queue[Any]" = queue.Queue(maxsize=200_000)
        self.events = 0             # leaf values represented in the file
        self.samples = 0            # rows written (block samples / poll cycles)
        self.stats_samples = 0      # rows whose leaves went into the live statistics
        self.dropped = 0            # samples lost because the backlog was full
        self.dropped_on_stop = 0    # samples still queued when the stop drain timed out
        self.after_limit = 0        # samples that arrived after a limit ended the recording (not part of it)
        self._written_t: deque[float] = deque(maxlen=200_000)   # PLC times of the newest rows in the file
        self._max_seen_t = 0.0                # the newest PLC time received from any block, and when it came
        self._max_seen_mono: float | None = None
        self.rows_after_window = 0  # rows in the file past the end, written before the end was known
        self.rows_after_window_at_least = False
        self.poll_refused: dict[str, dict[str, Any]] = {}   # poll mode: per symbol, reads the runtime refused
        self.unsubscribed: list[str] = []
        self.errors: list[str] = []
        self.created = time.time()
        self.t0_mono: float | None = None
        self.t0_wall: float | None = None
        self.t0_ft: int | None = None
        self.last_t: float = 0.0
        self.triggered_at: float | None = None
        self.stopped_at: float | None = None
        self.stopped_mono: float | None = None
        self.stop_reason: str | None = None
        self.last_values: dict[str, Any] = {}
        self.max_backlog = 0
        self.live_stats = True               # decode + statistics while recording (False: deferred to the file)
        self._cut_t: float | None = None     # drain only samples up to this PLC-relative time
        # N57: notifications are batched up to max_delay_ms; the pre-trigger buffer keeps that much skew as margin
        self.max_delay_ms = float(meta.get("max_delay_ms") or 0.0) if isinstance(meta, dict) else 0.0
        # R41 / N61: a level start_when fires once every block has delivered (cycle mode), as the native engine
        self._armed_seen: set[int] = set()
        self._armed_t0: float | None = None
        self.warmup_true_at: float | None = None   # start_when was true here, before every block had delivered
        self.warmup_lapsed = False                  # … and was false when every block had delivered (not fired)
        self.trigger_delayed_ms: float | None = None
        # N56: a stop on request ends at a PLC time — the newest sample received when it was asked for — and the
        # notifications stay until every block has delivered up to it (bounded), like the duration end (N13)
        self._recv_max_ts: int | None = None
        self.stop_at_cut = False
        self.cut_by: str | None = None        # what set the end the blocks are waited for: request / stop_when / max_events
        self._stop_cut_mono: float | None = None
        self.stop_wait_ms: float | None = None
        self._cut_lock = threading.Lock()     # the end (_cut_t, stop_at_cut, cut_by): ingest thread vs rec_stop
        # R43: block notifications sample memory addresses; a download / online change can put other variables there.
        # A block sample is released only once a symbol version read taken after its delivery returned the version
        # the recording started with (it was sampled before the read was answered): _verified_t. Poll mode reads the
        # version in each poll's own sum request instead. Same rule as the native engine.
        self.read_program: Callable[[], int] | None = None      # the symbol version, read now (raises if not)
        self.read_identity: Callable[[], str | None] | None = None
        self.check_ms = VERSION_CHECK_MS
        self._watch = False
        self._watch_error: str | None = None
        self._ver: int | None = None
        self._identity: str | None = None
        self._verified_t: float | None = None
        self._last_check = 0.0
        self._check_failing = False
        self._check_fail_since = 0.0
        self._check_fail_newest: float | None = None
        self._check_error: str | None = None
        self.version_reads = 0
        self.version_read_failures = 0
        self._final_check_done = False
        self.program_changed: dict[str, Any] | None = None
        self.program_watch_lost = False
        self.program_notes: list[str] = []
        self.unverified_rows = 0
        self.unverified_polls = 0
        # PLC-order release (_ingest_loop): per block, the samples that wait for the other blocks
        self._pending: dict[int, deque] = {}
        self._heads: list[tuple[float, int]] = []
        self._pending_n = 0
        self._last_out_t: float | None = None
        self._ingest_mono = time.monotonic()
        self._silent_s = max(0.5, (50.0 * float(self.cycle_ms or 0) + 3.0 * self.max_delay_ms) / 1000.0)
        self._sha = hashlib.sha256()
        self._lines = 0
        self._bytes = 0
        self._chain_lines = 0
        self.chains = 0
        self.gap_markers = 0
        self._fh = None
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.handles: list[tuple[int, int]] = []
        self.conn: pyads.Connection | None = None
        self.on_finish: Callable[[], None] | None = None   # releases the notifications (ingest thread, at stop)
        self._poll_thread: threading.Thread | None = None

    # ------------------------------------------------------------ setup
    def set_blocks(self, blocks: list[Block]) -> None:
        # Field retest 0.7.1: decoding 300 leaves per sample at 1 ms on the ingest thread competes with the ADS
        # callback for the GIL, and the runtime drops samples before they reach the queue. Above LIVE_VALUES_PER_S
        # the ingest only writes the raw bytes; statistics and values are decoded from the file afterwards.
        if self.mode == "cycle":
            leaves = sum(len(b.slots) for b in blocks)
            self.live_stats = leaves * (1000.0 / max(self.cycle_ms, 0.1)) <= LIVE_VALUES_PER_S
        self.blocks = blocks
        self.slot_of = {sl.name: (b, sl) for b in blocks for sl in b.slots}
        per_sample = max(1, sum(b.size for b in blocks))
        self.q = queue.Queue(maxsize=int(min(500_000, max(10_000, QUEUE_BYTES // (per_sample + 64)))))

    def _write(self, line: str) -> None:
        if self._fh:
            self._fh.write(line)
            raw = line.encode("utf-8")
            self._sha.update(raw)
            self._lines += 1
            self._bytes += len(raw)

    def _write_gap(self, b: "Block", kind: str, t_from: float | None, t_to: float, missing: int) -> None:
        """R5: a hole in the PLC timeline is written into the file where it happened (identical line format in the
        native engine)."""
        tf = "null" if t_from is None else f"{t_from:.4f}"
        self._write(f'{{"_gap": {{"block": {b.bid}, "kind": "{kind}", "t_from": {tf}, "t_to": {t_to:.4f}, "missing": {missing}}}}}\n')
        self.gap_markers += 1

    def _write_chain(self) -> None:
        """R9: checkpoint over every byte written so far (running SHA-256, HMAC-signed): a file whose footer never
        got written is still provably intact up to its last checkpoint."""
        if not self._fh or self._lines == self._chain_lines or self.t0_wall is None:
            return
        d = self._sha.hexdigest()
        line = f'{{"_chain": {{"lines": {self._lines}, "bytes": {self._bytes}, "sha256": "{d}"'
        if self.key:
            line += f', "hmac": "{_hmac.new(self.key, d.encode("ascii"), hashlib.sha256).hexdigest()}"'
        self._write(line + "}}\n")
        self._chain_lines = self._lines
        self.chains += 1

    def _note_counter(self, t: float, data: bytes) -> None:
        """D11: the cycle counter slot (block, off, size) must advance between samples. A PLC in STOP keeps sending its
        notifications with every value frozen; after stall_after the interval is a stall: marked in the file, counted
        against completeness, label INCOMPLETE. The bytes stay as the PLC sent them."""
        cfg = self.stall_cfg or {}
        off, size = int(cfg.get("off", 0)), int(cfg.get("size", 4))
        cur = data[off:off + size]
        if self._stall_prev is None:
            self._stall_prev, self._stall_adv_t = cur, t
            return
        if cur != self._stall_prev:
            if self._stall_open:
                self._close_stall(t)
            self._stall_prev, self._stall_adv_t, self._stall_rep = cur, t, 0
            return
        self._stall_rep += 1
        if not self._stall_open and self._stall_adv_t is not None and (t - self._stall_adv_t) * 10_000 > float(cfg.get("after_100ns", 0)):
            self._stall_open = True

    def _close_stall(self, t_to: float) -> None:
        b = self._stall_block
        if b is None or self._stall_adv_t is None:
            return
        self._write(f'{{"_stall": {{"block": {b.bid}, "t_from": {self._stall_adv_t:.4f}, "t_to": {t_to:.4f}, "samples": {self._stall_rep}}}}}\n')
        self.stalls += 1
        self.stalled_samples += self._stall_rep
        self.stall_ms += t_to - self._stall_adv_t
        if len(self.stall_list) < 20:
            self.stall_list.append([round(self._stall_adv_t, 4), round(t_to, 4)])
        self._stall_open = False
        self._stall_rep = 0

    def stalled_now(self) -> int:
        return self.stalled_samples + (self._stall_rep if self._stall_open else 0)

    def label_reason(self, miss: int | None = None) -> str | None:
        """Every cause of an INCOMPLETE label, in the conditions of label() (D11/D13 polish: only the STOP was named —
        a RUN→CONFIG→RUN gap counted against the label but was said only in `errors`)."""
        parts = []
        if self.stall_list:
            spans = ", ".join(f"{a:.4f}-{b:.4f} ms" for a, b in self.stall_list[:5])     # same text as the native engine
            more = f" (+{self.stalls - 5} more)" if self.stalls > 5 else ""
            parts.append(f"PLC stopped (cycle counter frozen) {spans}{more}")
        if self._stall_open and self._stall_adv_t is not None:
            parts.append(f"PLC stopped (cycle counter frozen) since {self._stall_adv_t:.4f} ms")
        if self.poll_refused:
            parts.append("reads refused by the runtime, those samples are missing: " + ", ".join(
                f"{n} ×{v['count']} ({v['first_error']})" for n, v in list(self.poll_refused.items())[:5]))
        if self.mode == "cycle":
            miss = self.missing_samples() if miss is None else miss
            if self.samples == 0:
                parts.append("no sample recorded")
            if miss and not self.poll_refused:
                parts.append(f"{miss} sample(s) missing from the PLC timeline (gap markers in the file)")
            if self.dropped or self.dropped_on_stop:
                parts.append(f"{self.dropped + self.dropped_on_stop} sample(s) dropped (queue overflow / at the stop)")
            if self.unsubscribed:
                parts.append(f"{len(self.unsubscribed)} variable(s) not subscribed")
            if self.start_when is not None and self.triggered_at is None:
                parts.append("start_when never fired")
            ub = unprovable_blocks(self.timeline_blocks(), self.fallback_period()) if self.samples else []
            if ub:
                parts.append(f"block(s) {ub[:10]} cannot be proven complete (no sample, or one without a period)")
        return "; ".join(parts) or None

    def silent_missing_estimate(self) -> float:
        """D14: silence so far on this PC's clock, in sample periods, for blocks that stopped delivering (live only)."""
        if self.mode != "cycle" or not self.active:
            return 0.0
        now = time.monotonic()
        est = 0.0
        for b in self.blocks:
            if b.last_wall is None or not b.min_step:
                continue
            silent_ms = (now - b.last_wall) * 1000.0
            if silent_ms > max(500.0, 50 * b.min_step):
                est += max(0.0, silent_ms / b.min_step - 1.0)
        return est

    def header_dict(self, **extra: Any) -> dict[str, Any]:
        return {"_header": True, "format": 2, "id": self.id, "name": self.name, "mode": self.mode,
                "cycle_ms": self.cycle_ms, "time_source": self.time_source,
                "symbols": [{"id": self.sids[s.name], "name": s.name, "type": s.type, "size": s.size,
                             "igrp": s.index_group, "ioffs": s.index_offset, **self.type_info.get(s.name, {})}
                            for s in self.symbols],
                "blocks": [b.header() for b in self.blocks],
                "created": dt.datetime.fromtimestamp(self.created, dt.timezone.utc).isoformat(), **self.meta, **extra}

    def start_ingest(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8", newline="\n", buffering=1 << 16)
        self._write(json.dumps(self.header_dict(), default=str) + "\n")
        self._ingest_mono = time.monotonic()
        self._thread = threading.Thread(target=self._ingest_loop, name=f"rec-{self.id}", daemon=True)
        self._thread.start()

    def enqueue(self, ts: int | float, key: Any, payload: Any) -> None:
        """ADS callback thread (block notification: key = block id, payload = raw bytes) or poll thread
        (key = None, payload = {name: value}). Only enqueues — the callback must return at once."""
        if key is not None and (self._recv_max_ts is None or ts > self._recv_max_ts):
            self._recv_max_ts = ts                   # newest PLC time received (a stop on request ends there)
        try:
            self.q.put_nowait((ts, key, payload))
        except queue.Full:
            self.dropped += 1

    def _rel_ms(self, ts: int | float) -> float:
        if self.time_source == "pc":
            if self.t0_wall is None:
                self.t0_wall = float(ts)
                self._write(json.dumps({"_t0": {"unix": float(ts), "source": "pc"}}) + "\n")
            return (float(ts) - self.t0_wall) * 1000.0
        if self.t0_ft is None:
            self.t0_ft = int(ts)
            self.t0_wall = (int(ts) - FILETIME_EPOCH_OFFSET_100NS) / 1e7
            utc = dt.datetime.fromtimestamp(self.t0_wall, dt.timezone.utc).isoformat()
            self._write(json.dumps({"_t0": {"filetime": self.t0_ft, "utc": utc, "source": "plc"}}) + "\n")
        return (int(ts) - self.t0_ft) / 1e4

    # ------------------------------------------------------------ ingest
    def _ingest_loop(self) -> None:
        """Samples arrive in ADS order: one block's in PLC order, but another block's can come up to max_delay_ms
        later (batching). Written in arrival order, a block that ran ahead had rows past an end that a batched block
        set later (stop_when at 500 ms with rows up to 600 ms already in the file), and the trigger saw the blocks
        out of step. So, like the native engine: every block sample waits in its block's queue and is released — to
        the trigger, the file, the statistics, stop_when / max_events, the limits — in PLC-time order, once every
        block that is delivering has been seen at or past it (_horizon). Poll rows (one source, this PC's clock)
        go straight through."""
        released = False
        released_last = False
        deadline = None
        while True:
            stopping = self._stop.is_set()
            if stopping and not released:
                released = True
                self._release()              # no new samples; what is queued was taken before the stop
                deadline = time.monotonic() + DRAIN_TIMEOUT_S
            got = 0
            # waiting samples that the horizon holds back move only with new input or when a block turns quiet: wait
            # for either (review: a zero timeout here spun one core at 100 % while the slowest block lagged)
            if stopping:
                timeout = 0.0
            elif self._pending_n and released_last:
                timeout = 0.0
            elif self._pending_n:
                timeout = self._next_quiet_s()
            else:
                timeout = 0.25
            try:
                item = self.q.get(timeout=timeout)
            except queue.Empty:
                item = None
            while item is not None:
                self._arrive(item)
                got += 1
                if got >= 4096:
                    break
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    item = None
            if stopping and deadline is not None and time.monotonic() > deadline:
                n_q = 0
                while True:                      # counted here as dropped_on_stop — taken out, so never counted twice
                    try:
                        self.q.get_nowait()
                    except queue.Empty:
                        break
                    n_q += 1
                self.dropped_on_stop += n_q + self._pending_n
                break
            final = stopping and self.q.empty()
            self._maybe_check(final)
            done = self._release_ordered(final)
            released_last = bool(done)
            if final and (not self._pending_n or self._only_unverified_left()):
                self._count_leftover()
                break
            if not stopping and not done:
                self._check_limits()
            if self._fh and time.monotonic() - self._last_flush > 1.0:
                self._write_chain()
                self._fh.flush()
                self._last_flush = time.monotonic()
        self._finish()

    def _arrive(self, item: Any) -> None:
        """A sample is here: its time, the block's newest time seen, this PC's clock of its arrival. Poll rows are
        handled at once; block samples wait in their block's queue (_release_ordered)."""
        if item is None:
            return
        ts, key, payload = item
        t = self._rel_ms(ts)
        if self.t0_mono is None:
            self.t0_mono = time.monotonic()
        backlog = self.q.qsize() + self._pending_n
        if backlog > self.max_backlog:
            self.max_backlog = backlog
        if key is None:
            self._release_one(t, None, payload, backlog > 2_000)
            return
        b = self.blocks[key]
        b.last_wall = time.monotonic()
        if b.seen_t is None or t > b.seen_t:
            b.seen_t = t
        if self._max_seen_mono is None or t >= self._max_seen_t:
            self._max_seen_t, self._max_seen_mono = t, b.last_wall
        q = self._pending.setdefault(b.bid, deque())
        if not q:
            heapq.heappush(self._heads, (t, b.bid))
        q.append((t, payload))
        self._pending_n += 1

    # ------------------------------------------------------------ R43: the program the addresses belong to
    def _gated(self) -> bool:
        return self._watch and self.read_program is not None and self.mode != "poll"

    def _check_period(self) -> float:
        return (max(self.check_ms, 500.0) if self._check_failing else self.check_ms) / 1000.0

    def _newest_t(self) -> float | None:
        """The newest PLC time delivered by any block (ms since t0): what a version read taken now can verify."""
        ts = self._recv_max_ts
        if ts is None or self.t0_ft is None:
            return None
        return (int(ts) - self.t0_ft) / 1e4

    def _flowing(self, now: float) -> bool:
        return any(now - (b.last_wall if b.last_wall is not None else self._ingest_mono) <= self._silent_s
                   for b in self.timeline_blocks())

    def _maybe_check(self, final: bool) -> None:
        if not self._gated():
            return
        newest = self._newest_t()
        if newest is not None and (self._verified_t is None or newest > self._verified_t):
            now = time.monotonic()
            if (final and not self._final_check_done) or now - self._last_check >= self._check_period():
                self._check_program(newest, now)
        if final:
            self._final_check_done = True

    def _check_program(self, newest: float, now: float) -> None:
        """Ingest thread. One read of the symbol version; after failed reads, and whenever it moved, the program's
        identity too (another build can come back after a restart with the same version byte)."""
        ok, v, err = True, None, None
        try:
            v = int(self.read_program())  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001
            ok, err = False, _fmt_exc(exc)
        use_id = ok and self._identity is not None and (v != self._ver or self._check_failing)
        id_now = None
        if use_id:
            try:
                id_now = self.read_identity() if self.read_identity is not None else None
            except Exception:  # noqa: BLE001
                id_now = None
            if id_now is None:
                ok, err = False, "the program identity (compile time, online changes, table sizes) could not be read"
        self._last_check = now
        self.version_reads += 1
        if not ok:
            self.version_read_failures += 1
            self._check_error = err
            if not self._check_failing:
                self._check_failing, self._check_fail_since, self._check_fail_newest = True, now, newest
            # samples keep coming while the version cannot be read: they cannot be verified — end at the last verified
            # one (an outage delivers nothing: waited for)
            if (not self.program_watch_lost and self._flowing(now)
                    and now - self._check_fail_since > 1.0 + self.max_delay_ms / 1000.0
                    and self._check_fail_newest is not None and newest > self._check_fail_newest):
                self.program_watch_lost = True
                self._end_program(f"the PLC program version could not be read for {now - self._check_fail_since:.1f} s "
                                  f"while samples kept coming ({err}): a download / online change could not be ruled "
                                  "out — the recording ends at the last verified sample")
            return
        self._check_failing = False
        same = (id_now == self._identity) if use_id else (v == self._ver)
        if same:
            if v != self._ver:
                self.program_notes.append(f"symbol version {self._ver} -> {v} with the same program ({id_now}): the "
                                          "recording continued")
                self._ver = v
            if self._verified_t is None or newest > self._verified_t:
                self._verified_t = newest
            return
        if self.program_changed is not None:
            return
        what = f"symbol version {self._ver} -> {v}" + (f", program {self._identity} -> {id_now}" if use_id else "")
        self.program_changed = {
            "after_ms": None if self._verified_t is None else round(self._verified_t, 4),
            "read_by_ms": round(newest, 4), "symbol_version": [self._ver, v],
            "rule": "the change happened after the last verified sample (after_ms) and before the version read taken "
                    "once the sample at read_by_ms had been delivered; samples between are not written"}
        self._end_program(f"the PLC program changed ({what}: download / online change / another build) - the "
                          "notifications sample memory addresses that may now hold other variables; the recording ends "
                          "at the last sample verified before the change")

    def _end_program(self, reason: str) -> None:
        if self._verified_t is None or self.armed:
            with self._cut_lock:
                if not self._stop.is_set():
                    self.stop_reason = reason
                    self._stop.set()        # no window to keep
            return
        self._end_at(self._verified_t, reason, "program_change")

    def _only_unverified_left(self) -> bool:
        if not self._gated() or not self._heads:
            return False
        v = self._verified_t
        return v is None or self._heads[0][0] > v

    def _count_leftover(self) -> None:
        """At the end: block samples still waiting — past the end (after_limit), or inside it but never verified
        against a program change (unverified_rows: delivered, not written, counted as missing where they fall)."""
        cut = self._cut_t
        for q in self._pending.values():
            for t, _p in q:
                if cut is not None and _ticks(t) > _ticks(cut):
                    self.after_limit += 1
                elif self._gated():
                    self.unverified_rows += 1
                else:
                    self.after_limit += 1
            q.clear()
        self._heads.clear()
        self._pending_n = 0

    def program_footer(self) -> dict[str, Any]:
        """R43: program_watch (and what a change did) — the native engine's footer fields."""
        out: dict[str, Any] = {}
        if self.mode == "poll":
            pw: dict[str, Any] = {"checked": self._ver is not None,
                                  "per_read": "each poll reads the symbol version in its own sum request: values of "
                                              "another program are never recorded"}
            if self._ver is None:
                pw["error"] = self._watch_error or "not readable"
            if self.unverified_polls:
                pw["unverified_polls"] = self.unverified_polls
        elif self._watch:
            pw = {"checked": True, "check_ms": self.check_ms, "reads": self.version_reads,
                  "failed_reads": self.version_read_failures}
            if self._check_error:
                pw["last_error"] = self._check_error
            pw["verified_to_ms"] = None if self._verified_t is None else round(self._verified_t, 4)
        else:
            pw = {"checked": False, "error": self._watch_error or "not readable"}
        if self.program_notes:
            pw["notes"] = list(self.program_notes)
        out["program_watch"] = pw
        if not pw["checked"]:
            out["program_watch_error"] = (f"the symbol version could not be read at the start ({pw['error']}): a "
                                          "download / online change during the recording would not be noticed")
        if self.program_changed is not None:
            out["program_changed"] = self.program_changed
        if self.program_watch_lost:
            out["program_watch_lost"] = True
        if self.unverified_rows:
            v = "none verified" if self._verified_t is None else f"{self._verified_t:.4f} ms"
            out["unverified_rows"] = self.unverified_rows
            out["unverified_note"] = (f"samples delivered after the last verified one ({v}) but not written: the symbol "
                                      f"version could not be read again ({self._check_error}), so a download / online "
                                      "change could not be ruled out — counted as missing where they fall")
        return out

    def backlog(self) -> int:
        """Samples received but not in the file yet: queued, or waiting for the other blocks (PLC-order release)."""
        return self.q.qsize() + self._pending_n

    @property
    def received_t(self) -> float:
        """The newest PLC time received (ms since t0) — kept or not, written or still waiting for the other blocks.
        What 'now' is for a client that compares PLC times (last_t is the newest one in the file)."""
        seen = [b.seen_t for b in self.blocks if b.seen_t is not None]
        return max(seen + [self.last_t])

    def _horizon(self) -> float | None:
        """The PLC time up to which every block has delivered: the smallest newest-seen time of the blocks that are
        delivering. A block not seen yet is waited for; one that has delivered nothing for silent_s (a stopped task,
        a change-mode block whose values do not change) no longer holds the others back — what it sends later is
        written as it comes and counted as late. None: wait."""
        now = time.monotonic()
        w = math.inf
        any_quiet = False
        for b in self.timeline_blocks():
            quiet = now - (b.last_wall if b.last_wall is not None else self._ingest_mono) > self._silent_s
            if quiet:
                any_quiet = True
                continue
            if b.seen_t is None:
                return None
            w = min(w, b.seen_t)
        if any_quiet and self._max_seen_mono is not None:
            # a quiet block can still deliver a sample stamped up to max_delay_ms (ADS batching, plus a margin for the
            # transport) before the PLC's 'now': rows after that are held back for it — a change-mode block's late
            # stop_when change is released in PLC order, before the other blocks' later rows (review). The PLC's now
            # is the newest time received plus the wall time since it came, so nothing waits longer than the bound.
            plc_now = self._max_seen_t + (now - self._max_seen_mono) * 1000.0
            w = min(w, plc_now - (self.max_delay_ms + HOLD_MARGIN_MS))
        if self._gated():
            if self._verified_t is None:
                return None
            w = min(w, self._verified_t)
        return w

    def _next_quiet_s(self) -> float:
        """Seconds until the horizon can move by time alone — a delivering block turning quiet, or the hold for a quiet
        block passing the oldest waiting sample — at most 0.25."""
        now = time.monotonic()
        nxt = 0.25
        for b in self.timeline_blocks():
            last = b.last_wall if b.last_wall is not None else self._ingest_mono
            left = last + self._silent_s - now
            if left > 0:
                nxt = min(nxt, left)
        if self._heads and self._max_seen_mono is not None:
            hold = self.max_delay_ms + HOLD_MARGIN_MS
            left = (self._heads[0][0] + hold - self._max_seen_t) / 1000.0 - (now - self._max_seen_mono)
            if left > 0:
                nxt = min(nxt, left)
        if self._heads and self._gated():
            nxt = min(nxt, self._last_check + self._check_period() - now)      # the next version read
        return max(0.001, nxt)

    def _release_ordered(self, final: bool) -> int:
        """Release the waiting block samples up to the horizon (everything when final), oldest PLC time first; the
        limits are checked after each one, so a sample past an end set by an earlier one is never written."""
        if not self._pending_n:
            return 0
        if final:
            w = math.inf
            if self._gated():          # R43: what no version read verified is never written, not even at the end
                w = self._verified_t if self._verified_t is not None else -math.inf
        else:
            w = self._horizon()
        if w is None:
            return 0
        n = 0
        while self._heads:
            t, bid = self._heads[0]
            if t > w:
                break
            heapq.heappop(self._heads)
            q = self._pending[bid]
            t, payload = q.popleft()
            self._pending_n -= 1
            if q:
                heapq.heappush(self._heads, (q[0][0], bid))
            self._release_one(t, self.blocks[bid], payload, self._pending_n + self.q.qsize() > 2_000)
            n += 1
            if not final:
                self._check_limits()
        return n

    def _release_one(self, t: float, b: Block | None, payload: Any, light: bool) -> None:
        try:
            if self._cut_t is not None and _ticks(t) > _ticks(self._cut_t):    # in ticks: 1.005 s × 1000 is 1004.99…
                self.after_limit += 1
                return
            if b is None:
                self._handle_poll(t, payload, light)
                return
            if self._last_out_t is not None and t < self._last_out_t:
                b.late += 1
            else:
                self._last_out_t = t
            self._handle_block(t, b, payload, light)
        except Exception as exc:  # noqa: BLE001 — a bad sample must not end the recording silently
            if len(self.errors) < 20:
                self.errors.append(f"ingest: {type(exc).__name__}: {exc}")

    WARMUP_MS = 200.0     # a level start_when waits at most this long (PLC time) for every block's first sample

    def _handle_block(self, t: float, b: Block, data: bytes, light: bool) -> None:
        if self.armed:
            self.pre.append((t, b, data))
            # R41 / N57: blocks arrive with a skew — a block's samples can come up to max_delay_ms (ADS batching) after
            # another block's newer ones. Purging against the newest stamp of any block with a fixed 50 ms dropped the
            # other blocks' samples of the trigger cycle when the trigger block was the late one. Keep
            # max_delay_ms + 50 ms as margin (the native engine's rule) and commit only what lies inside the window.
            margin = self.max_delay_ms + 50.0
            while self.pre and t - self.pre[0][0] > self.pre_trigger_ms + margin:
                self.pre.popleft()
            self._armed_seen.add(b.bid)
            if self._armed_t0 is None:
                self._armed_t0 = t
            sw = self.start_when
            if sw is not None and sw.symbol in self.slot_of and self.slot_of[sw.symbol][0] is b:
                fires = sw.fires(self.slot_of[sw.symbol][1].decode(data))
                # R41: in cycle mode every block delivers every cycle; a level condition true at the very start would
                # open the window before the other blocks' first samples. Fire on the first cycle all blocks deliver
                # (at most WARMUP_MS after arming). Change mode delivers on change only: no such wait there.
                all_live = (self.mode != "cycle" or sw.edge or len(self._armed_seen) >= len(self.timeline_blocks())
                            or t - self._armed_t0 > self.WARMUP_MS)
                if fires and not all_live:
                    if self.warmup_true_at is None:
                        self.warmup_true_at = t                  # N61: remembered, reported if it never fires
                    return
                if not fires and self.warmup_true_at is not None:
                    self.warmup_lapsed = True        # the warm-up truth is over (also within the warm-up): a later
                                                     # fire is a new one, not the warm-up's truth delayed
                if fires:
                    self.armed = False
                    self.triggered_at = t
                    if self.warmup_true_at is not None and not self.warmup_lapsed:
                        self.trigger_delayed_ms = round(t - self.warmup_true_at, 4)
                    pre, self.pre = list(self.pre), deque()
                    for pt, pb, pd in pre:
                        if pt >= t - self.pre_trigger_ms:
                            self._commit_block(pt, pb, pd, light)
            return
        if self.triggered_at is not None and t < self.triggered_at - self.pre_trigger_ms:
            return                      # a block's sample from before the window, delivered after the trigger
        self._commit_block(t, b, data, light)

    def _commit_block(self, t: float, b: Block, data: bytes, light: bool) -> None:
        # timeline of what is IN the file (pre-trigger samples that were discarded are not part of it)
        if self.mode == "cycle":
            prev = b.last_t
            gap = b.note_stamp(t)
            if gap and prev is not None:
                self._write_gap(b, "gap", prev, t, gap)
        else:
            b.samples += 1
        self.last_t = max(self.last_t, t)
        self._written_t.append(t)
        self._write(f'[{t:.4f},"#{b.bid}","{base64.b64encode(data).decode("ascii")}"]\n')
        self.samples += 1
        if b is self._stall_block:
            self._note_counter(t, data)
        prev, b.prev = b.prev, data
        if self.mode == "cycle" or prev is None:
            changed = b.slots
        else:
            changed = [s for s in b.slots if data[s.off:s.off + s.size] != prev[s.off:s.off + s.size]]
        self.events += len(changed)
        ew = self.stop_when
        if not light and self.live_stats:
            self.stats_samples += 1
            with self._lock:
                for s in changed:
                    self._ingest_value(t, s.name, s.decode(data))
        ending = self._stop.is_set() or self.cut_by in ("stop_when", "max_events")
        if not ending and ew is not None and ew.symbol in self.slot_of and self.slot_of[ew.symbol][0] is b:
            if ew.fires(self.slot_of[ew.symbol][1].decode(data)):
                self._end_at(t, f"stop_when {ew.symbol} {ew.op} {ew.value}", "stop_when")
                ending = True
        if not ending and self.max_events and self.events >= self.max_events:
            self._end_at(t, f"max_events {self.max_events}", "max_events")

    def _handle_poll(self, t: float, row: dict[str, Any], light: bool) -> None:
        if self.armed:
            self.pre.append((t, None, row))
            while self.pre and t - self.pre[0][0] > self.pre_trigger_ms:
                self.pre.popleft()
            sw = self.start_when
            if sw is not None and sw.symbol in row and sw.fires(self._dec(sw.symbol, row[sw.symbol])):
                self.armed = False
                self.triggered_at = t
                pre, self.pre = list(self.pre), deque()
                for pt, _b, prow in pre:
                    self._commit_poll(pt, prow, light)
            return
        self._commit_poll(t, row, light)

    def _dec(self, name: str, value: Any) -> Any:
        tt = self.types.get(name)
        if tt and tt.kind == "wstring" and isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-16-le", "replace").split("\x00", 1)[0]
        elif isinstance(value, (bytes, bytearray)):
            value = bytes(value).hex()
        return T.decode(tt, value) if tt else value

    def _commit_poll(self, t: float, row: dict[str, Any], light: bool) -> None:
        self.last_t = max(self.last_t, t)
        self._written_t.append(t)
        self.samples += 1
        ew = self.stop_when
        with self._lock:
            for name, raw in row.items():
                dec = self._dec(name, raw)
                self._write(json.dumps([round(t, 4), self.sids.get(name, name), dec], default=str) + "\n")
                self.events += 1
                if not light:
                    self._ingest_value(t, name, dec)
                if ew is not None and name == ew.symbol and ew.fires(dec) and not self._stop.is_set():
                    self._end_at(t, f"stop_when {ew.symbol} {ew.op} {ew.value}", "stop_when")
        if not light:
            self.stats_samples += 1
        if self.max_events and self.events >= self.max_events and not self._stop.is_set():
            self._end_at(t, f"max_events {self.max_events}", "max_events")

    def _ingest_value(self, t: float, name: str, dec: Any) -> None:
        """Caller holds self._lock."""
        self.last_values[name] = dec
        self.ring.append((t, name, dec))
        self.stats[name].ingest(t, dec, _numeric_dec(dec), self.max_runs)

    def _check_limits(self) -> None:
        if self._stop.is_set():
            return
        if self.stop_at_cut:
            # every end at a PLC time (duration, post_trigger, stop_when, max_events, rec_stop): the notifications stay
            # until every block has been seen at or past it, or the wait (max_delay + 1 s) ends and what is missing is
            # missing (tail, allowance 1)
            waited = time.monotonic() - (self._stop_cut_mono or time.monotonic())
            if self._tail_reached(self._cut_t) or waited >= TAIL_WAIT_S + self.max_delay_ms / 1000.0:
                self.stop_wait_ms = round(waited * 1000.0, 1)
                self._stop.set()
            return
        mono = (time.monotonic() - self.t0_mono) if self.t0_mono is not None else None
        if self.post_trigger_s and self.triggered_at is not None and (self.last_t - self.triggered_at) / 1000.0 >= self.post_trigger_s:
            self._end_at(self.triggered_at + self.post_trigger_s * 1000.0,
                         f"post_trigger {self.post_trigger_s}s after the start_when trigger", "post_trigger")
        elif self.duration_s and (self.last_t / 1000.0 >= self.duration_s or (mono is not None and mono >= self.duration_s)):
            # N13: the window is [0, duration]. Either the PLC time or this PC's clock says it is over — but samples
            # the PLC took inside the window may still be on their way (ADS → queue); samples after the cut are not
            # part of the recording (after_limit)
            self._end_at(self.duration_s * 1000.0, f"duration {self.duration_s}s", "duration")
        elif self.duration_s and self.t0_mono is None and (time.time() - self.created) >= self.duration_s + 5:
            self.stop_reason = "duration elapsed with no data (symbols never updated?)"
            self._stop.set()

    def timeline_blocks(self) -> list[Block]:
        """The blocks the PLC timeline is about: those whose notification was registered. A block that could not be
        registered (allow_partial) is not part of the recording — its symbols are in `unsubscribed`; counting it as a
        block that never delivered inflated the missing samples and kept every stop waiting out its timeout."""
        return [b for b in self.blocks if b.error is None]

    def _tail_reached(self, cut: float) -> bool:
        """Every block has been SEEN at or past the cut — a sample with a later PLC time, even one not kept (after
        the end): a block's samples arrive in order, so none up to the cut is still on its way. Judging by the last
        kept sample with one period of allowance ended the wait while the block's sample AT the cut was in flight,
        and lost it. A block that has not delivered is waited for like any other — until the caller's timeout."""
        if self.mode != "cycle" or not self.blocks:
            return True
        return all(b.seen_t is not None and _ticks(b.seen_t) >= _ticks(cut) for b in self.timeline_blocks())

    def _end_at(self, t: float, reason: str, by: str) -> None:
        """The recording ends at PLC time t — duration, post_trigger, stop_when, max_events or a stop on request (N56).
        Python receives samples in ADS order, not PLC order (another block's samples up to t can come max_delay_ms
        later), so in cycle mode the notifications stay until every block has reached t (_check_limits); cutting at
        once lost them and their blocks' tails read as missing. An earlier end narrows a pending one (its reason
        then stands); a later one does not move it. Change / poll mode: no regular delivery to wait for — stop now,
        what is queued is still written up to t. The native engine writes in PLC order: every block reached t."""
        with self._cut_lock:
            if self._stop.is_set():
                return
            if self.stop_at_cut and self._cut_t is not None and t >= self._cut_t:
                return
            self._cut_t = t if self._cut_t is None else min(self._cut_t, t)
            self.stop_reason = reason
            self.cut_by = by
            if self.mode == "cycle" and self.blocks:
                if not self.stop_at_cut:
                    self._stop_cut_mono = time.monotonic()
                self.stop_at_cut = True
            else:
                self._stop.set()

    def request_stop(self, reason: str = "requested", at_plc_time: bool = False) -> None:
        """at_plc_time (rec_stop): N56 — the recording ends at the newest PLC time received now; the notifications stay
        until every block has delivered up to it (at most max_delay_ms + 1 s), samples after it are not part of the
        recording (after_limit). Unsubscribing at once let each block end where its notification happened to be —
        max_delay_ms apart — so the end of the window could not be checked. An end already pending stands. Otherwise:
        stop now."""
        if self._stop.is_set():
            return
        if self.stop_at_cut:
            if not at_plc_time:
                with self._cut_lock:
                    self.stop_reason = self.stop_reason or reason
                    self._stop.set()      # a plain stop while waiting for the tail: stop now
            return
        if (at_plc_time and self.mode == "cycle" and self.blocks and not self.armed
                and self.t0_ft is not None and self._recv_max_ts is not None):
            self._end_at((int(self._recv_max_ts) - self.t0_ft) / 1e4, reason, "request")
            return
        with self._cut_lock:
            self.stop_reason = self.stop_reason or reason
            self._stop.set()

    def _release(self) -> None:
        cb = self.on_finish
        if cb is not None:
            try:
                cb()
            except Exception as exc:  # noqa: BLE001
                if len(self.errors) < 20:
                    self.errors.append(f"release: {exc}")

    def _finish(self) -> None:
        self._release()
        # a notification in flight while the subscriptions were deleted can land after the last drain: received, past
        # the end — counted, not lost from the accounting
        late = 0
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
            late += 1
        self.after_limit += late
        self.stopped_mono = time.monotonic()
        self.stopped_at = time.time()
        with self._lock:
            for st in self.stats.values():
                if st.runs and st.last_t is not None:
                    st.runs[-1][1] = st.last_t
            if self._fh:
                miss = self.missing_samples()
                if self._stall_open and self.t0_wall is not None:
                    we_ = self.window_end()
                    self._close_stall(max(we_ if we_ is not None else self.last_t, self._stall_adv_t or 0.0))
                if self.mode == "cycle" and self.t0_wall is not None:
                    ws, we = self.window_start(), self.window_end()
                    fb, org = self.fallback_period(), self.window_origin()
                    for b in self.timeline_blocks():
                        h = b.head_missing(ws, fb)
                        if h > 0 and ws is not None and b.first_t is not None:
                            self._write_gap(b, "head", ws, b.first_t, h)
                        tm = b.tail_missing(we, fb, org)
                        if tm > 0 and we is not None:
                            self._write_gap(b, "tail", b.last_t if b.last_t is not None else org, we, tm)
                digest = self._sha.hexdigest()
                footer = {"_footer": True, "events": self.events, "samples": self.samples,
                          "missing_samples": miss, "label": self.label(miss), "gap_markers": self.gap_markers,
                          "chain_checkpoints": self.chains, "stalls": self.stalls, "stalled_samples": self.stalled_samples,
                          "stall_s": round(self.stall_ms / 1000.0, 6), "dropped": self.dropped,
                          "dropped_on_stop": self.dropped_on_stop, "after_limit": self.after_limit,
                          "unsubscribed": self.unsubscribed, "stop_reason": self.stop_reason,
                          "window_end_ms": self._cut_t, "window_start_ms": self.window_start(), "engine": "python",
                          "max_delay_ms": self.max_delay_ms,
                          "stopped": dt.datetime.now(dt.timezone.utc).isoformat(), "lines": self._lines,
                          "sha256": digest}
                why = self.label_reason(miss)
                if why and footer["label"] != "COMPLETE":
                    footer["label_reason"] = why
                late = sum(b.late for b in self.blocks)
                if late:
                    footer["late_samples"] = late          # written after a later stamp of another block (see _horizon)
                if self._cut_t is not None:
                    # rows in the file past the end (a block delivered later than the ADS bound, before the end was
                    # known): counted here, in the ingest thread that wrote them; the readers skip them
                    cut_k = _ticks(self._cut_t)
                    self.rows_after_window = sum(1 for wt in self._written_t if _ticks(wt) > cut_k)
                    self.rows_after_window_at_least = (len(self._written_t) == self._written_t.maxlen
                                                       and _ticks(self._written_t[0]) > cut_k)
                if self.rows_after_window:
                    footer["rows_after_window"] = self.rows_after_window
                    if self.rows_after_window_at_least:
                        footer["rows_after_window_at_least"] = True
                if self.poll_refused:
                    footer["poll_refused"] = self.poll_refused
                footer.update(self.stop_and_trigger_notes())
                footer.update(self.program_footer())
                footer.update(self._native_like(footer, miss))     # after the stop's notes: whose end was waited for
                if self.stall_cfg:
                    footer["stall_counter_block"] = self.stall_cfg.get("block")
                if self.key:
                    footer["hmac"] = _hmac.new(self.key, digest.encode("ascii"), hashlib.sha256).hexdigest()
                self._fh.write(json.dumps(footer, default=str) + "\n")
                self._fh.close()
                self._fh = None
                sealed = True
            else:
                sealed = False
        if sealed and not self.scratch:
            self.manifest = seal_recording(self.path, self.key)

    def _native_like(self, footer: dict[str, Any], miss: int | None) -> dict[str, Any]:
        """0.8.15 retest (rec acea): the fields the native engine's footer has and this one had not — rec_capture and
        rec_verify showed verdict_hint / overflow_samples null. overflow_samples: the samples lost on this PC (here the
        backlog that was full: `dropped`); requested_s; for a cycle recording the missing samples at the head, in gaps
        and at the tail, and verdict_hint: PASS for a COMPLETE file, FAIL for an INCOMPLETE one whose end every block
        was waited for — none for a change / poll recording (no timeline to prove) or an end that was not waited for
        (rec_verify's tail rule decides)."""
        out: dict[str, Any] = {"overflow_samples": self.dropped,
                               "requested_s": self.duration_s if self.duration_s else None}
        if self.mode == "cycle" and self.t0_wall is not None:
            ws, we, fb, org = self.window_start(), self.window_end(), self.fallback_period(), self.window_origin()
            blocks = list(self.timeline_blocks())
            head = sum(max(0, b.head_missing(ws, fb)) for b in blocks)
            tail = sum(max(0, b.tail_missing(we, fb, org)) for b in blocks) if we is not None else 0
            out.update({"missing_head": head, "missing_tail": tail, "missing_gaps": max(0, (miss or 0) - head - tail)})
        label = footer.get("label")
        if label == "COMPLETE":
            out["verdict_hint"] = "PASS"
        elif label == "INCOMPLETE" and not end_not_waited_for(footer):
            out["verdict_hint"] = "FAIL"
        return out

    def label(self, miss: int | None = None) -> str:
        """R5: COMPLETE only when the PLC timeline proves every sample is in the file (cycle mode); INCOMPLETE when
        anything is missing or was lost; NOT_PROVABLE for change/poll recordings (silence can't be told from loss)."""
        if self.mode != "cycle":
            return "NOT_PROVABLE"
        miss = self.missing_samples() if miss is None else miss
        ok = (self.samples > 0 and not miss and not self.dropped and not self.dropped_on_stop and not self.unsubscribed
              and (self.start_when is None or self.triggered_at is not None) and not self.stalled_now()
              and not unprovable_blocks(self.timeline_blocks(), self.fallback_period()))
        return "COMPLETE" if ok else "INCOMPLETE"

    def stop_and_trigger_notes(self) -> dict[str, Any]:
        """N56 / N61: what the end of a stop on request and the start_when warm-up did — in the footer and rec_status."""
        out: dict[str, Any] = {}
        if self.stop_at_cut and self.cut_by == "request":
            out["stop_on_request"] = {"at_ms": self._cut_t, "waited_ms": self.stop_wait_ms,
                                      "rule": "ends at the newest PLC time received when the stop was asked for; every "
                                              "block must deliver up to it (allowance 1 period)"}
        elif self.stop_at_cut:
            what = {"duration": "the end of duration_s", "post_trigger": "the end of post_trigger_s",
                    "program_change": "the last sample verified before the PLC program changed (R43)"}.get(
                self.cut_by or "", f"the sample on which {self.cut_by} fired")
            out["stop_at"] = {"at_ms": self._cut_t, "waited_ms": self.stop_wait_ms, "by": self.cut_by,
                              "rule": f"ends at the PLC time of {what}; every block must deliver up to it (allowance "
                                      "1 period)"}
        bad = [b.bid for b in self.blocks if b.error is not None]
        if bad:
            out["unregistered_blocks"] = bad
        if self.trigger_delayed_ms is not None:
            out["trigger_delayed_ms"] = self.trigger_delayed_ms
            out["trigger_note"] = (f"start_when was already true at {self.warmup_true_at:.4f} ms, before every block had "
                                   f"delivered; it fired {self.trigger_delayed_ms:g} ms later, on the first cycle all "
                                   "blocks delivered (the window cannot start before a block's first sample)")
        elif self.start_when is not None and self.warmup_lapsed and self.warmup_true_at is not None:
            out["trigger_note"] = (f"start_when was true at {self.warmup_true_at:.4f} ms during the warm-up (before every "
                                   f"block had delivered, at most {self.WARMUP_MS:g} ms) and false afterwards — " +
                                   ("it did not fire" if self.triggered_at is None else
                                    f"it fired at {self.triggered_at:.4f} ms, when it was true again"))
            out["start_when_warmup_true_ms"] = self.warmup_true_at
        return out

    @property
    def active(self) -> bool:
        return self.stopped_at is None

    # ------------------------------------------------------------ views
    def window_start(self) -> float | None:
        """Start of the window that must be complete: trigger − pre_trigger_ms for triggered recordings — also with
        pre_trigger_ms 0 (N57: the window then starts at the trigger, and a block whose first sample comes later has
        lost its head); None for untriggered recordings (each block from its first sample)."""
        if self.triggered_at is None:
            return None
        return self.triggered_at - (self.pre_trigger_ms or 0.0)

    def window_end(self) -> float | None:
        """End of the recorded window in PLC-relative ms: the duration / post-trigger cut, else the latest sample of
        any block (a block that stopped delivering before the others then shows its tail as missing)."""
        if self._cut_t is not None and not self.active:
            return self._cut_t
        lasts = [b.last_t for b in self.timeline_blocks() if b.last_t is not None]
        return max(lasts) if lasts else None

    def fallback_period(self) -> float | None:
        return fallback_period(self.timeline_blocks(), self.cycle_ms)

    def window_origin(self) -> float | None:
        return window_origin(self.timeline_blocks(), self.window_start())

    def missing_samples(self) -> int | None:
        if self.mode != "cycle":
            return None
        end, start = self.window_end(), self.window_start()
        fb, org = self.fallback_period(), self.window_origin()
        return sum(b.missing(end, start, fallback=fb, origin=org) for b in self.timeline_blocks())

    def completeness(self, live_estimate: float = 0.0) -> float | None:
        """Share of the window that is in the file as samples of a running PLC: stalled samples don't count, and
        while a block is silent the estimated silence (live_estimate, D14) is added to the denominator."""
        if self.mode != "cycle":
            return None
        got = sum(b.samples for b in self.blocks)
        miss = self.missing_samples() or 0
        good = max(0, got - self.stalled_now())
        den = got + miss + live_estimate
        return round(good / den, 6) if den else None

    def block_report(self, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        for b in self.blocks[:limit]:
            d: dict[str, Any] = {"id": b.bid, "bytes": b.size, "symbols": len(b.slots), "first": b.slots[0].name if b.slots else None,
                                 "samples": b.samples}
            if self.mode == "cycle" and b.error is None:
                ws, we, fb, org = self.window_start(), self.window_end(), self.fallback_period(), self.window_origin()
                d.update({"missing_samples": b.missing(we, ws, fallback=fb, origin=org),
                          "missing_head": b.head_missing(ws, fb), "missing_tail": b.tail_missing(we, fb, org),
                          "period_ms": None if b.min_step is None else round(b.min_step, 4),
                          "duplicate_stamps": b.dup_stamps, "first_gaps": b.gaps[:5]})
                if b.samples < 2:
                    d["period_note"] = ("never delivered: the whole window counts as missing" if b.samples == 0 else
                                        "one sample: counted with the other blocks' period")
                if self.active and b.last_wall is not None and b.min_step and (time.monotonic() - b.last_wall) * 1000 > max(500.0, 50 * b.min_step):
                    d["silent"] = True
                    d["silent_s"] = round(time.monotonic() - b.last_wall, 1)
            if b.error:
                d["error"] = b.error
            out.append(d)
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self.t0_mono is None:
                elapsed = 0.0
            elif self.active:
                elapsed = time.monotonic() - self.t0_mono
            else:
                elapsed = max(0.0, (self.stopped_mono or time.monotonic()) - self.t0_mono)
            backlog = self.q.qsize()
            est = self.silent_missing_estimate()
            st: dict[str, Any] = {
                "id": self.id, "name": self.name, "active": self.active, "mode": self.mode, "cycle_ms": self.cycle_ms,
                "time_source": self.time_source, "symbols": len(self.symbols), "notifications": len(self.blocks) or None,
                "events": self.events, "samples": self.samples,
                "missing_samples": self.missing_samples(), "completeness": self.completeness(est),
                "label": self.label() + ("" if not self.active else " (so far)"),
                "gap_markers": self.gap_markers, "stalls": self.stalls, "stalled_samples": self.stalled_now(),
                "stalled": self._stall_open,
                "dropped": self.dropped, "dropped_on_stop": self.dropped_on_stop, "backlog": backlog,
                "max_backlog": self.max_backlog,
                "stats_coverage": (round(self.stats_samples / self.samples, 4) if self.samples else 1.0) if self.live_stats
                                  else "deferred (high rate: computed from the file by rec_stats)",
                "events_per_s": round(self.events / elapsed, 1) if elapsed > 0 else 0.0,
                "elapsed_s": round(elapsed, 2), "plc_span_s": round(self.last_t / 1000.0, 4),
                "received_ms": round(self.received_t, 4), "pending": self.backlog(),
                "armed": bool(self.active and self.armed and self.triggered_at is None),
                "triggered_at_ms": None if self.triggered_at is None else round(self.triggered_at, 3),
                "stop_reason": self.stop_reason, "file": str(self.path),
                "file_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "last_values": self._last_values_view(), "errors": self.errors[-5:],
            }
        if est > 0:
            st["missing_silent_est"] = round(est, 1)
            st["completeness_estimated"] = True
        if self._stall_open and self._stall_adv_t is not None:
            st["stalled_since_ms"] = round(self._stall_adv_t, 4)
        why = self.label_reason()
        if why and not st["label"].startswith("COMPLETE"):
            st["label_reason"] = why
        st.update(self.stop_and_trigger_notes())
        if self.unsubscribed:
            st["unsubscribed_symbols"] = self.unsubscribed[:50]
        if self.blocks:
            st["blocks"] = self.block_report()
        warn = []
        if self._stall_open:
            warn.append(f"PLC STOPPED? the cycle counter has not advanced since {self._stall_adv_t:.1f} ms — samples since then are frozen values, not measurements")
        if est > 0:
            warn.append(f"{[b.bid for b in self.blocks if b.last_wall is not None and (time.monotonic() - b.last_wall) * 1000 > max(500.0, 50 * (b.min_step or 0))]} silent: ≈{est:.0f} samples missing so far (PC clock estimate)")
        if self.dropped or self.dropped_on_stop:
            warn.append(f"{self.dropped + self.dropped_on_stop} samples LOST on this PC (backlog full / stop timeout)")
        if st["missing_samples"]:
            warn.append(f"{st['missing_samples']} samples missing in the PLC timeline (gaps between timestamps)")
        quiet = [b.bid for b in self.timeline_blocks() if b.samples == 0] if self.mode == "cycle" and self.samples else []
        if quiet:
            warn.append(f"block(s) {quiet} have not delivered a sample: their whole window counts as missing")
        if self.active and backlog > self.q.maxsize // 2:
            warn.append(f"ingest cannot keep up: backlog {backlog}/{self.q.maxsize} — fewer symbols, a larger cycle_ms or mode='change'")
        if self.unsubscribed:
            warn.append(f"{len(self.unsubscribed)} symbols could not be subscribed")
        pf = self.program_footer()
        st.update(pf)
        from .native_capture import program_warnings
        warn.extend(program_warnings(pf))
        if warn:
            st["warnings"] = warn
        return st

    def _last_values_view(self, limit: int = 20) -> dict[str, Any]:
        """Caller holds self._lock. Latest value per leaf (decoded on demand from the last raw sample when live
        decoding is off), at most `limit` leaves, long arrays summarised."""
        vals: dict[str, Any] = {}
        if self.live_stats and self.last_values:
            items = list(self.last_values.items())
        else:
            items = [(sl.name, sl.decode(b.prev)) for b in self.blocks if b.prev is not None for sl in b.slots]
        for k, v in items[:limit]:
            vals[k] = trim_value(v)
        if len(items) > limit:
            vals["…"] = f"{len(items) - limit} more leaves (rec_snapshot / rec_read for all)"
        return vals

    def snapshot(self, last_n: int, symbols: list[str] | None) -> list[dict[str, Any]]:
        with self._lock:
            ev = list(self.ring)
        if symbols:
            want = {s.lower() for s in symbols}
            ev = [e for e in ev if e[1].lower() in want]
        return [{"t_ms": round(t, 3), "symbol": n, "value": v} for t, n, v in ev[-last_n:]]

    def stats_dict(self, symbols: list[str] | None, include_runs: bool, max_runs_out: int) -> dict[str, Any]:
        with self._lock:
            return _stats_out(self.stats, symbols, include_runs, max_runs_out)


def trim_value(v: Any, max_len: int = 10) -> Any:
    """P1: a 300-element array as a value in a tool response is noise — summarise long lists."""
    if isinstance(v, list) and len(v) > max_len:
        return {"len": len(v), "first": v[:5], "last": v[-2:]}
    return v


def _stats_out(stats: dict[str, SymStats], symbols: list[str] | None, include_runs: bool, max_runs_out: int) -> dict[str, Any]:
    want = {s.lower() for s in symbols} if symbols else None
    out = {}
    for name, st in stats.items():
        if want and name.lower() not in want:
            continue
        d = st.to_dict()
        for k in ("first", "last"):
            d[k] = trim_value(d.get(k))
        if include_runs:
            runs = st.runs[-max_runs_out:] if max_runs_out else []
            d["runs"] = [{"t_start_ms": round(a, 3), "t_end_ms": round(b, 3), "dwell_ms": round(b - a, 3), "value": v} for a, b, v in runs]
            d["runs_truncated"] = st.runs_truncated or len(st.runs) > max_runs_out
        out[name] = d
    return out


def _numeric_dec(dec: Any) -> float | None:
    if isinstance(dec, bool):
        return 1.0 if dec else 0.0
    if isinstance(dec, (int, float)):
        return float(dec)
    if isinstance(dec, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in dec:
                return float(dec[k])
    return None


def iter_events(path: Path, want: set[str] | None = None) -> Iterator[tuple[float, str, Any]]:
    """(t_ms, symbol, value) from a recording file of either format. Block samples are decoded here; in change
    mode a leaf is emitted only when its bytes changed (the first sample emits every leaf)."""
    names: list[str] = []
    blocks: dict[int, Block] = {}
    mode = "change"
    prev: dict[tuple[int, int], bytes] = {}
    sel: dict[int, list[Slot]] = {}
    # the recording is the window the footer names: rows past its end (the Python recorder writes in ADS order, so a
    # block that ran ahead can have rows past an end set later) are in the file but not in the recording
    ft = read_footer(path)
    wend = _ticks(ft["window_end_ms"]) if ft and isinstance(ft.get("window_end_ms"), (int, float)) else None
    # N84: rows before the window (a block that delivered before every block had) are in the file, not the recording
    wstart = (_ticks(ft["window_start_ms"]) if ft and ft.get("rows_before_window")
              and isinstance(ft.get("window_start_ms"), (int, float)) else None)
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            if not ln.strip():
                continue
            if ln[0] == "{":
                try:
                    d = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if d.get("_header"):
                    mode = d.get("mode", "change")
                    if d.get("format") == 2:
                        names, blocks = blocks_from_header(d)
                        sel = {bid: [s for s in b.slots if want is None or s.name.lower() in want] for bid, b in blocks.items()}
                continue
            try:
                ev = json.loads(ln)
            except json.JSONDecodeError:
                continue
            t, k, v = ev[0], ev[1], ev[2]
            if wstart is not None and isinstance(t, (int, float)) and _ticks(t) < wstart:
                continue
            if wend is not None and isinstance(t, (int, float)) and _ticks(t) > wend:
                continue
            if isinstance(k, str) and k.startswith("#") and blocks:
                bid = int(k[1:])
                slots = sel.get(bid)
                if not slots:
                    continue
                data = base64.b64decode(v)
                for s in slots:
                    if mode != "cycle":
                        chunk = data[s.off:s.off + s.size]
                        key = (bid, s.sid)
                        if prev.get(key) == chunk:
                            continue
                        prev[key] = chunk
                    yield t, s.name, s.decode(data)
            elif isinstance(k, int) and names:
                n = names[k] if 0 <= k < len(names) else str(k)
                if want is None or n.lower() in want:
                    yield t, n, v
            elif isinstance(k, str):
                if want is None or k.lower() in want:
                    yield t, k, v


def read_footer(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "replace").strip().splitlines()
    except OSError:
        return None
    for ln in reversed(tail):
        if ln.startswith("{") and '"_footer"' in ln:
            try:
                return json.loads(ln)
            except json.JSONDecodeError:
                return None
    return None


MANIFEST_SUFFIX = ".manifest.json"
_MANIFEST_FIELDS = ("label", "samples", "events", "missing_samples", "gap_markers", "chain_checkpoints", "dropped",
                    "stop_reason", "engine", "engine_version", "window_start_ms", "window_end_ms", "stopped", "lines")


def manifest_path(path: Path) -> Path:
    return path.with_suffix(MANIFEST_SUFFIX)


def provenance_path(export: Path) -> Path:
    """<export>.provenance.json next to an exported file (x.wide.csv → x.wide.csv.provenance.json)."""
    return export.with_name(export.name + ".provenance.json")


def _export_target_refusal(out: Path, src: Path) -> str | None:
    """N58: an export writes a CSV; it never writes over a recording or a record about one — the source's own .jsonl,
    its manifest, any provenance file, or another recording (a .jsonl with a recording header). None = allowed."""
    try:
        o, s_ = out.resolve(), src.resolve()
    except OSError:
        o, s_ = out.absolute(), src.absolute()
    if o == s_:
        return "it is the recording itself"
    if o == manifest_path(s_) or o.name.endswith(MANIFEST_SUFFIX):
        return "it is a recording's manifest (sidecar)"
    if o.name.endswith(".provenance.json"):
        return "it is a provenance file (sidecar of an export)"
    if o.suffix.lower() == ".jsonl":
        if o.is_file():
            try:
                with open(o, encoding="utf-8") as fh:
                    first = fh.readline()
                if '"symbols"' in first or '"format"' in first:
                    return "it is a recording (.jsonl)"
            except OSError:
                return "it is a .jsonl file that cannot be read"
        return "a .jsonl name is reserved for recordings; exports are .csv"
    return None


_FT_EPOCH = dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)
_FT_SEC_CACHE: dict[int, str] = {}


def _ft_cols(ft: int) -> list[Any]:
    """[plc_filetime, time_utc] — time_utc is exact: whole seconds + the 7-digit 100 ns remainder, no float."""
    sec, frac = divmod(ft, 10_000_000)
    base = _FT_SEC_CACHE.get(sec)
    if base is None:
        if len(_FT_SEC_CACHE) > 4096:
            _FT_SEC_CACHE.clear()
        base = _FT_SEC_CACHE[sec] = (_FT_EPOCH + dt.timedelta(seconds=sec)).strftime("%Y-%m-%dT%H:%M:%S")
    return [ft, f"{base}.{frac:07d}Z"]


def _manifest_sig(key: bytes, m: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in m.items() if k != "hmac"}, sort_keys=True, separators=(",", ":"), default=str)
    return _hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def _earlier_export(out: Path, src: Path, params: dict[str, Any]) -> tuple[bool, str]:
    """(same, description) of the file already at an export path: same = this tool wrote it from the same source
    bytes with the same parameters and nobody changed it since (its provenance says so) — replacing it changes
    nothing."""
    try:
        pv = json.loads(provenance_path(out).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "it has no provenance file of this tool (not an export of this recording, or edited)"
    old = pv.get("params") or {}
    norm = lambda p: (p.get("fmt"), sorted(x.lower() for x in p["symbols"]) if p.get("symbols") else None,  # noqa: E731
                      bool(p.get("fill")))
    try:
        edited = file_sha256(out)[0] != pv.get("export_sha256")
        same_src = file_sha256(src)[0] == pv.get("source_sha256")
    except OSError:
        edited, same_src = True, False
    desc = (f"an earlier export ({old.get('fmt')}, symbols {old.get('symbols') or 'all'}, fill {bool(old.get('fill'))}, "
            f"{pv.get('created', '?')})" + ("; changed since it was written" if edited else "")
            + ("" if same_src else "; the recording has changed since"))
    return (not edited and same_src and norm(old) == norm(params)), desc


def file_sha256(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _remove_file(p: Path, tries: int = 10) -> None:
    """Delete a (possibly read-only, possibly just-closed) file; on Windows a handle closed a moment ago can still
    refuse the delete for a few ms — retry briefly, then fail with the OS reason as a RecorderError."""
    last: OSError | None = None
    for i in range(tries):
        try:
            make_writable(p)
            p.unlink()
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            last = exc
            time.sleep(0.05 * (i + 1))
    raise RecorderError(f"could not delete {p}: {last}")


def is_read_only(path: Path) -> bool:
    try:
        return not (path.stat().st_mode & stat.S_IWUSR)
    except OSError:
        return False


def make_writable(path: Path) -> None:
    try:
        if path.exists():
            os.chmod(path, path.stat().st_mode | stat.S_IWUSR)
    except OSError:
        pass


def seal_recording(path: Path, key: bytes | None) -> dict[str, Any] | None:
    """R9: after a recording has ended, write ``<id>.manifest.json`` (whole-file SHA-256 and size, the footer's
    checksum and signature, a summary) signed with this install's key, then mark both files read-only so an
    accidental edit fails loudly. The recording itself is never touched. Idempotent: an existing manifest is kept."""
    mp = manifest_path(path)
    if mp.exists() or not path.is_file():
        return None
    try:
        footer = read_footer(path) or {}
        sha, size = file_sha256(path)
        m: dict[str, Any] = {"_manifest": 1, "file": path.name, "size": size, "sha256_file": sha,
                             "footer_sha256": footer.get("sha256"), "footer_hmac": footer.get("hmac"),
                             "has_footer": bool(footer),
                             "sealed": dt.datetime.now(dt.timezone.utc).isoformat()}
        for k in _MANIFEST_FIELDS:
            if k in footer:
                m[k] = footer[k]
        if key:
            m["hmac"] = _manifest_sig(key, m)
        with open(mp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(m, indent=1, default=str) + "\n")
        for p in (path, mp):
            os.chmod(p, p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        return m
    except OSError:
        return None


class Recorder:
    """Owns all recordings. Methods are synchronous; the server calls the
    registration ones on its ADS worker."""

    def __init__(self, ads: AdsBackend, root: Path, ring_events: int = 200_000, max_runs: int = 20_000):
        self.ads = ads
        self.root = root
        self.ring_events = ring_events
        self.max_runs = max_runs
        self.recs: dict[str, Any] = {}      # Recording (Python engine) or NativeRecording
        self._key: bytes | None = None
        self.rcfg: Any = None                # config.RecorderConfig (engine, capture_exe, ring); None = defaults
        self.native_extra: dict[str, Any] | None = None   # tests: extra start fields (backend="sim", sim={...})

    # ------------------------------------------------------------ native engine
    def native_exe(self) -> Path:
        from . import native_capture as NC
        exe = getattr(self.rcfg, "capture_exe", None) if self.rcfg is not None else None
        return Path(exe) if exe else NC.default_exe()

    def engine_info(self) -> dict[str, Any]:
        """For tc_doctor / rec_start: which engine cycle recordings use and why."""
        from . import native_capture as NC
        want = getattr(self.rcfg, "engine", "auto") if self.rcfg is not None else "auto"
        exe = self.native_exe()
        ok, reason, info = NC.availability(exe, _pkg_version())
        return {"setting": want, "native_available": ok, "native_exe": str(exe), "native_reason": reason,
                "native_version": (info or {}).get("version"), "native_ads": (info or {}).get("ads"),
                "cycle_mode_uses": ("python" if want == "python" or not ok else "native")}

    @property
    def key(self) -> bytes:
        if self._key is None:
            self._key = recording_key(self.root)
        return self._key

    # ------------------------------------------------------------ symbol selection
    def expand_symbols(self, symbols: list[str] | None, prefix: str | None, pattern: str | None,
                       max_symbols: int, port: int | None = None) -> list[SymbolEntry]:
        """Resolve a selection to scalar/array leaves. Composite symbols (FB/struct instances, arrays of
        structs) are expanded through the datatype table — for explicit symbols, prefixes AND patterns.
        A pattern selects exactly the leaves rec_scan lists for it (A5): every leaf whose path, or one of
        whose ancestors, matches — 'MAIN.fbFill' → every leaf beneath it, '*bOpen' → each bOpen."""
        lst, idx = self.ads.symbols_cached(False, port)
        chosen: dict[str, SymbolEntry] = {}

        def add_leaves(root: SymbolEntry, leaves) -> None:
            for leaf in leaves:
                # pointers / references / interfaces are addresses, not values (the note was never matched: an alias
                # pointer was recorded as a signed integer, N50); values not expanded are not recorded either
                if leaf.size <= 0 or leaf.not_expanded or leaf.note == POINTER_NOTE:
                    continue
                tt = T.parse_type(leaf.type, leaf.size, leaf.data_type)
                if tt.ctype is None or tt.opaque or tt.kind == "pointer":
                    continue
                chosen[leaf.path] = SymbolEntry(leaf.path, leaf.type, leaf.size, root.index_group,
                                                root.index_offset + leaf.offset, 0, "", leaf.data_type)

        def add(s: SymbolEntry) -> None:
            tt = T.parse_type(s.type, s.size)
            if not T.is_elementary(tt):
                # the datatype table decides what it is (enum with its base type, pointer alias, struct …)
                root, leaves = self.ads.leaves(s.name, max_symbols + 1, port)
                add_leaves(root, leaves)
            elif tt.kind != "pointer":
                chosen[s.name] = s

        for n in symbols or []:
            s = idx.get(n.lower())
            if s is None:
                # nested element not in the table (e.g. MAIN.arr[2].x) → ask the runtime
                try:
                    s = self.ads.symbol_info(n, port)
                except AdsError:
                    raise AdsError(f"symbol {n!r} not found in the runtime symbol table")
            add(s)
        if prefix:
            groups = self.ads.leaves_under(prefix, max_symbols + 1, port)
            if not groups:
                raise AdsError(f"prefix {prefix!r} matches no symbol")
            for root, leaves in groups:
                add_leaves(root, leaves)
        if pattern:
            # A5: the same leaves rec_scan lists for this pattern (leaf or any ancestor matches; '[ ]' literal)
            for root, leaves in pattern_leaves(self.ads, pattern, max_symbols, port):
                add_leaves(root, leaves)
                if len(chosen) > max_symbols:
                    break
        out = list(chosen.values())
        if not out:
            raise AdsError("no recordable (scalar/array) symbols selected")
        if len(out) > max_symbols:
            raise AdsError(f"{len(out)} leaves selected, limit {max_symbols}; narrow the selection or raise max_symbols")
        return out


    # ------------------------------------------------------------ provenance
    def _manifest(self, port: int | None) -> dict[str, Any]:
        """What the recording was taken from: the PLC application (AppTimestamp / OnlineChangeCnt identify the
        exact download), its tasks and cycle times, the TwinCAT build and the symbol version. Best effort — a field
        the runtime does not offer is recorded as unavailable, never guessed."""
        m: dict[str, Any] = {}
        base = "TwinCAT_SystemInfoVarList._AppInfo"
        try:
            vals = self.ads.read_many([f"{base}.{k}" for k in ("AppTimestamp", "OnlineChangeCnt", "ProjectName", "AppName")], port)
            m["app"] = {k.rsplit(".", 1)[-1]: v for k, v in vals.items() if not (isinstance(v, dict) and "error" in v)} or "unavailable"
        except Exception as exc:  # noqa: BLE001
            m["app"] = f"unavailable: {exc}"
        tasks = []
        for i in range(1, 9):
            tb = f"TwinCAT_SystemInfoVarList._TaskInfo[{i}]"
            try:
                vals = self.ads.read_many([f"{tb}.CycleTime", f"{tb}.TaskName", f"{tb}.Priority"], port)
            except Exception:  # noqa: BLE001
                break
            ct = vals.get(f"{tb}.CycleTime")
            if isinstance(ct, dict) and "error" in ct or ct is None:
                break
            tasks.append({"index": i, "name": vals.get(f"{tb}.TaskName"), "cycle_time_ms": _raw_val(ct) / 10_000.0
                          if isinstance(_raw_val(ct), (int, float)) else ct, "priority": vals.get(f"{tb}.Priority")})
        m["tasks"] = tasks or "unavailable"
        try:
            m["device"] = self.ads.device_info(port)
        except Exception as exc:  # noqa: BLE001
            m["device"] = f"unavailable: {exc}"
        try:
            ident = self.ads._table_identity(self.ads.conn(port))
            m["symbol_version"] = ident[0] if ident else None
        except Exception:  # noqa: BLE001
            m["symbol_version"] = None
        return m

    def _pick_counter(self, choice: str, manifest: dict[str, Any], cycle_ms: float, port: int | None) -> SymbolEntry | None:
        """The per-cycle ground truth recorded with every cycle-mode recording: 'auto' = the task whose cycle time
        is the largest one not above cycle_ms (else task 1); or an explicit symbol (e.g. MAIN.nCycleCount)."""
        name = choice
        if choice == "auto":
            tasks = manifest.get("tasks") if isinstance(manifest.get("tasks"), list) else []
            fit = [t for t in tasks if isinstance(t.get("cycle_time_ms"), (int, float)) and 0 < t["cycle_time_ms"] <= cycle_ms + 1e-9]
            idx = max(fit, key=lambda t: t["cycle_time_ms"])["index"] if fit else 1
            name = f"TwinCAT_SystemInfoVarList._TaskInfo[{idx}].CycleCount"
        try:
            s = self.ads.symbol_info(name, port)
        except Exception:  # noqa: BLE001 — not offered on this runtime: the timestamps remain the evidence
            if choice != "auto":
                raise
            return None
        tt = T.parse_type(s.type, s.size)
        if tt.kind not in ("int", "uint"):
            raise RecorderError(f"cycle_counter {name!r} is {s.type}, not an integer counter")
        return s

    # ------------------------------------------------------------ start / stop
    def _type_info(self, syms: list[SymbolEntry], port: int | None) -> dict[str, dict[str, Any]]:
        """R3: what the PLC's datatype table says about each recorded symbol's type — its GUID (identifies the
        exact type version) and, for structs/FBs, the member layout (offset/size/type), so the raw bytes in the
        file can be decoded later without the PLC. Best effort: empty when the table can't be read."""
        try:
            ts = self.ads.typesystem(False, port)
        except Exception:  # noqa: BLE001
            ts = None
        if ts is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for s in syms:
            e = ts.get(s.type)
            if e is None:
                continue
            d: dict[str, Any] = {}
            if e.guid:
                d["type_guid"] = e.guid
            if e.type and e.type != s.type and not e.dims:
                d["base_type"] = e.type
            el = ts.get(e.type) if e.dims and e.type else None
            if el is not None and el.guid:
                d["element_type_guid"] = el.guid
            comp = e if e.subitems else el if (el is not None and el.subitems) else None
            if comp is not None:
                d["layout"] = [{"name": m.name, "type": m.type, "offset": m.offs, "size": m.size,
                                **({"array": [[lb, n] for lb, n in m.dims]} if m.dims else {})}
                               for m in comp.subitems[:256]]
                if len(comp.subitems) > 256:
                    d["layout_truncated"] = len(comp.subitems)
            if d:
                out[s.name] = d
        return out

    def start(self, **kw: Any) -> dict[str, Any]:
        """Start a recording (arguments: _start). R43: when the PLC program changed between looking the variables up
        and registering them (their addresses may belong to other variables), the symbol table is loaded again and
        the start repeated once — nothing is subscribed or written before that check."""
        try:
            return self._start(**kw)
        except _ProgramMoved:
            self.ads.refresh_symbols(kw.get("port"))
        try:
            return self._start(**kw)
        except _ProgramMoved as exc:
            raise AdsError(f"{exc} — twice in a row: nothing was recorded. Start again once the download / online "
                           "change is done.") from exc

    def _start(self, *, symbols: list[str] | None, prefix: str | None, pattern: str | None, mode: str,
              cycle_ms: float, duration_s: float | None, max_events: int | None, start_when: str | None,
              stop_when: str | None, pre_trigger_ms: float, name: str | None, max_symbols: int,
              port: int | None = None, post_trigger_s: float | None = None, allow_partial: bool = False,
              max_delay_ms: float = 0.0, cycle_counter: str | None = "auto", edge_triggers: bool = False,
              engine: str | None = None, scratch: bool = False) -> dict[str, Any]:
        # scratch: a helper recording (ads_wait_until) that is deleted right after — never sealed, never read-only
        if mode not in ("cycle", "change", "poll"):
            raise RecorderError("mode must be cycle | change | poll")
        want = (engine or (getattr(self.rcfg, "engine", "auto") if self.rcfg is not None else "auto")).lower()
        if want not in ("auto", "native", "python"):
            raise RecorderError("engine must be auto | native | python")
        # R9: cycle_ms=-5 became 0.1 ms silently (20,000 notifications/s) and duration_s=-1 meant "no end"
        bad = []
        try:
            cycle_ms = float(cycle_ms)
        except (TypeError, ValueError):
            bad.append(f"cycle_ms {cycle_ms!r} is not a number")
            cycle_ms = 0.0
        if not bad and not (0.05 <= cycle_ms <= 3_600_000):
            bad.append(f"cycle_ms {cycle_ms:g} must be between 0.05 and 3,600,000 ms (TwinCAT's finest task is 50 µs)")
        for nm, v, lo in (("duration_s", duration_s, 0.0), ("post_trigger_s", post_trigger_s, 0.0)):
            if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or not v > lo):
                bad.append(f"{nm} {v!r} must be > 0 (or omitted)")
        if max_events is not None and (not isinstance(max_events, int) or isinstance(max_events, bool) or max_events <= 0):
            bad.append(f"max_events {max_events!r} must be a positive integer (or omitted)")
        for nm, v in (("pre_trigger_ms", pre_trigger_ms), ("max_delay_ms", max_delay_ms)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                bad.append(f"{nm} {v!r} must be ≥ 0")
        if isinstance(max_symbols, bool) or not isinstance(max_symbols, int) or max_symbols <= 0:
            bad.append(f"max_symbols {max_symbols!r} must be a positive integer")
        if bad:
            raise RecorderError("; ".join(bad) + " — nothing was started")
        syms = self.expand_symbols(symbols, prefix, pattern, max_symbols, port)
        manifest = self._manifest(port)
        counter = None
        if mode == "cycle" and cycle_counter:
            counter = self._pick_counter(cycle_counter, manifest, cycle_ms, port)
            if counter is not None and all(s.name.lower() != counter.name.lower() for s in syms):
                syms = syms + [counter]
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        path = self.root / f"{rid}.jsonl"
        types = {s.name: T.parse_type(s.type, s.size, s.data_type) for s in syms}
        er = (lambda tname, raw: self.ads.enum_literal(tname, raw, port))
        sw = Condition.parse(start_when, types, edge_triggers, er) if start_when else None
        ew = Condition.parse(stop_when, types, edge_triggers, er) if stop_when else None
        if post_trigger_s and not sw:
            raise RecorderError("post_trigger_s needs start_when (it counts from the trigger)")
        meta = {"ams_net_id": self.ads.net_id, "port": port or self.ads.port, "start_when": start_when,
                "stop_when": stop_when, "pre_trigger_ms": pre_trigger_ms, "post_trigger_s": post_trigger_s,
                "edge_triggers": bool(edge_triggers),
                "selection": {"symbols": symbols, "prefix": prefix, "pattern": pattern},
                "cycle_counter": counter.name if counter is not None else None, "max_delay_ms": max_delay_ms,
                "manifest": manifest, "recorder": f"twincat-mcp {_pkg_version()}"}
        rec = Recording(rid, name or rid, syms, mode, cycle_ms, path, self.ring_events, self.max_runs,
                        duration_s, max_events, sw, ew, pre_trigger_ms, meta, post_trigger_s, self.key)
        rec.scratch = scratch
        rec.type_info = self._type_info(syms, port)
        if mode != "poll":
            rec.set_blocks(build_blocks(syms, rec.sids, rec.types))
        if mode == "cycle" and counter is not None and counter.name in rec.slot_of:
            blk, slot = rec.slot_of[counter.name]
            after_ms = 3.0 * max(cycle_ms, _task_cycle_ms(counter.name, manifest, cycle_ms))
            rec.stall_cfg = {"block": blk.bid, "off": slot.off, "size": slot.size, "after_100ns": int(round(after_ms * 10_000))}
            rec._stall_block = blk
            rec.meta["stall_detection"] = {"counter": counter.name, "block": blk.bid, "after_ms": round(after_ms, 3)}
        chosen, engine_note = self._choose_engine(want, mode, allow_partial)
        if chosen == "native":
            try:
                return self._start_native(rec, port, cycle_ms, max_delay_ms, duration_s, post_trigger_s, pre_trigger_ms,
                                          max_events, syms, counter)
            except _NativeFallback as exc:
                if want == "native":
                    raise RecorderError(f"engine 'native' requested but not usable: {exc}") from exc
                engine_note = f"native engine not used: {exc}"
        self._program_baseline(rec, port)
        rec.start_ingest()
        try:
            if mode == "poll":
                self._start_poll(rec, port)
            else:
                self._start_notifications(rec, port, allow_partial, max_delay_ms)
        except Exception:
            rec.request_stop("start failed")
            if rec._thread:
                rec._thread.join(timeout=10)
            raise
        self.recs[rid] = rec
        st = rec.status()
        st["engine"] = "python"
        if engine_note:
            st["engine_note"] = engine_note
        return self._start_summary(st, rec, syms, counter, mode, cycle_ms)

    def _program_baseline(self, rec: Recording, port: int | None) -> None:
        """R43: the symbol version the recording runs with, read before anything is registered; the addresses must be
        from the same program (the symbol table's version)."""
        c = self.ads.conn(port)
        want = self.ads._table_version(c)
        try:
            v = int(c.read(ADSIGRP_SYM_VERSION, 0, ctypes.c_ubyte))
        except Exception as exc:  # noqa: BLE001
            rec._watch_error = _fmt_exc(exc)
            return
        if want is not None and v != want:
            raise _ProgramMoved(f"the PLC program changed after the variables were looked up (symbol version {want}, "
                                f"now {v}): their addresses may belong to other variables")
        rec._watch, rec._ver, rec.version_reads = True, v, 1
        rec._identity = _program_identity(c)

    def _choose_engine(self, want: str, mode: str, allow_partial: bool) -> tuple[str, str | None]:
        if want == "python":
            return "python", None
        if mode != "cycle":
            if want == "native":
                raise RecorderError(f"engine 'native' records cycle mode only (mode={mode!r} uses the Python recorder)")
            return "python", None
        if allow_partial:
            if want == "native":
                raise RecorderError("engine 'native' subscribes all blocks or none: allow_partial needs the Python recorder")
            return "python", "allow_partial=true: Python recorder (the native engine is all-or-nothing)"
        from . import native_capture as NC
        ok, reason, _info = NC.availability(self.native_exe(), _pkg_version())
        if not ok:
            if want == "native":
                raise RecorderError(f"engine 'native' requested but not usable: {reason}")
            return "python", f"native engine not used: {reason}"
        return "native", None

    def _start_native(self, rec: Recording, port: int | None, cycle_ms: float, max_delay_ms: float,
                      duration_s: float | None, post_trigger_s: float | None, pre_trigger_ms: float,
                      max_events: int | None, syms: list[SymbolEntry], counter: SymbolEntry | None) -> dict[str, Any]:
        from . import native_capture as NC
        extra = dict(self.native_extra or {})
        backend = extra.pop("backend", "ads")
        net_id = extra.pop("ams_net_id", None)
        if net_id is None and backend == "ads":
            net_id = self.ads.conn(port).ams_net_id      # resolves the local NetId when none is configured
        info = NC.availability(self.native_exe(), _pkg_version())[2] or {}
        header = rec.header_dict(engine="native", engine_version=info.get("version"), engine_ads=info.get("ads"))
        _ = self.key                                      # make sure the HMAC key exists before the service reads it
        rc = self.rcfg
        if rec.stall_cfg:
            extra["stall"] = rec.stall_cfg
        if "symbol_version" not in extra:
            tv = self.ads._table_version(self.ads.conn(port))
            if tv is not None:
                extra["symbol_version"] = tv          # R43: the service refuses addresses of another program version
        try:
            nrec = NC.start_native(
                rec, header, self.native_exe(), net_id=net_id or "", port=port or self.ads.port, ip=self.ads.ip,
                local_net_id=getattr(rc, "local_ams_net_id", None), cycle_ms=cycle_ms, max_delay_ms=max_delay_ms,
                duration_s=duration_s, post_trigger_s=post_trigger_s, pre_trigger_ms=pre_trigger_ms,
                max_events=max_events, key_path=self.root / ".recording.key",
                ring_s=float(getattr(rc, "ring_s", 10.0)), ring_cap_mb=float(getattr(rc, "ring_cap_mb", 1024.0)),
                timeout_ms=int(getattr(self.ads.cfg, "timeout_ms", 5000)), backend=backend, extra=extra)
        except NC.NativeUnavailable as exc:
            raise _NativeFallback(str(exc)) from exc
        except NC.ProgramMoved as exc:
            raise _ProgramMoved(str(exc)) from exc
        except NC.NativeStartError as exc:
            raise AdsError(f"{exc}. Nothing is recorded rather than a recording with holes.") from exc
        self.recs[rec.id] = nrec
        st = nrec.status()
        return self._start_summary(st, rec, syms, counter, "cycle", cycle_ms)

    def _start_summary(self, st: dict[str, Any], rec: Any, syms: list[SymbolEntry], counter: SymbolEntry | None,
                       mode: str, cycle_ms: float) -> dict[str, Any]:
        notifs = len(rec.blocks) if mode != "poll" else 0
        rate = (notifs or 1) * 1000.0 / cycle_ms
        st["leaves"] = len(syms)
        vals = 0
        arrays = []
        for x in syms:
            tt = T.parse_type(x.type, x.size)
            n = T.array_count(tt) if tt.kind == "array" else None
            vals += n or 1
            if n:
                arrays.append(f"{x.name} ({n} values)")
        st["values_per_sample"] = vals
        if arrays:
            st["arrays"] = arrays[:20]
            st["note"] = ("arrays of scalars are recorded as one variable each (one block of bytes, elements decoded on "
                          "read/export as name[i]); rec_scan lists the same elements individually")
        if counter is not None:
            st["cycle_counter"] = counter.name
        st["notifications_per_s"] = round(rate, 1) if mode == "cycle" else f"≤{round(rate, 1)}"
        st["values_per_s"] = round(vals * 1000.0 / cycle_ms, 1) if mode == "cycle" else None
        warns = []
        if mode == "cycle" and rate > 20_000 and st.get("engine") != "native":
            warns.append(f"{notifs} notifications every {cycle_ms} ms: above what this PC was measured to ingest. "
                         "Watch missing_samples / backlog in rec_status, or select fewer, contiguous variables.")
        # N31 / N62: a variable changes only when the task that writes it runs. cycle_ms below that task's cycle samples
        # the same PLC cycle again (load, not resolution). Compared with the task that runs each recorded program
        # (from the project's task configuration); a GVL has no single owner — compared with the fastest task.
        chk = self._task_check(rec, syms, cycle_ms)
        if chk["checked"]:
            st["task_check"] = chk["checked"]
        if chk.get("fastest_task_ms") is not None:
            st["fastest_task_ms"] = chk["fastest_task_ms"]
        effect = "samples repeat the same PLC cycle" if mode != "change" else "the extra checks find nothing new"
        for w in chk["below"]:
            warns.append(f"cycle_ms {cycle_ms:g} is below the cycle of {w['what']} ({w['cycle_ms']:g} ms, {w['basis']}): "
                         f"{effect} and the rate above ({st['notifications_per_s']} notifications/s) is load, not "
                         f"resolution — use cycle_ms={w['cycle_ms']:g} or a multiple of it for these variables")
        if warns:
            st["warning"] = " | ".join(warns)
        return st

    # set by the server from the project index: (name of a program, ADS port it is read from) → the tasks that run it
    # ([] = no program, e.g. a GVL; None = the project cannot tell). N62; N114: the port picks the PLC project.
    task_of: Callable[[str, int | None], list[dict[str, Any]] | None] | None = None
    task_reason: Callable[[str, int | None], str | None] | None = None

    def _task_check(self, rec: Any, syms: list[SymbolEntry], cycle_ms: float) -> dict[str, Any]:
        man = (getattr(rec, "meta", None) or {}).get("manifest") or {}
        rt = {str(t.get("name") or "").lower(): float(t["cycle_time_ms"])
              for t in (man.get("tasks") if isinstance(man.get("tasks"), list) else [])
              if isinstance(t, dict) and isinstance(t.get("cycle_time_ms"), (int, float)) and t["cycle_time_ms"] > 0}
        fastest = min(rt.values()) if rt else None
        port = (getattr(rec, "meta", None) or {}).get("port")
        checked: dict[str, Any] = {}
        below: list[dict[str, Any]] = []
        roots = sorted({x.name.split(".", 1)[0] for x in syms if not _is_system_root(x.name)})
        for r in roots:
            owners = None
            if self.task_of is not None:
                try:
                    owners = self.task_of(r, port if isinstance(port, int) else None)
                except Exception:  # noqa: BLE001 — no project: the fastest task is the comparison
                    owners = None
            if owners:
                cyc = []
                for t in owners:             # {"name", "cycle_ms", "plc_project"} of the program's own PLC project
                    o = str(t.get("name") or "")
                    c = rt.get(o.lower())
                    if c is None:
                        c = t.get("cycle_ms")
                    if c:
                        cyc.append((c, o))
                if cyc:
                    c, o = min(cyc)
                    basis = "its task in the project, cycle from the runtime" if o.lower() in rt else "its task in the project"
                    checked[r] = {"task": o, "cycle_ms": c, "basis": basis}
                    if cycle_ms < c - 1e-9:
                        below.append({"what": f"{r} (task {o})", "cycle_ms": c, "basis": basis})
                    continue
            if fastest is None:
                continue
            other = None
            if owners is None and getattr(self, "task_reason", None) is not None:
                try:
                    other = self.task_reason(r, port if isinstance(port, int) else None)
                except Exception:  # noqa: BLE001
                    other = None
            why = ("a GVL / no program: written by any task — compared with the fastest task" if owners == [] else
                   other or "the project's task configuration is not available — compared with the fastest task")
            checked[r] = {"task": None, "cycle_ms": fastest, "basis": why}
            if cycle_ms < fastest - 1e-9:
                below.append({"what": f"{r} (no single task)", "cycle_ms": fastest, "basis": why})
        return {"checked": checked, "below": below, "fastest_task_ms": fastest if below else None}

    def _start_notifications(self, rec: Recording, port: int | None, allow_partial: bool, max_delay_ms: float) -> None:
        base = self.ads.conn(port)
        conn = pyads.Connection(base.ams_net_id, base.ams_net_port, base.ip_address)
        try:
            conn.open()
            conn.set_timeout(self.ads.cfg.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"recorder connection failed: {_fmt_exc(exc)}") from exc
        rec.conn = conn
        rec.on_finish = lambda: self._stop_notifications(rec)
        rec.read_program = lambda: int(conn.read(ADSIGRP_SYM_VERSION, 0, ctypes.c_ubyte))     # R43, ingest thread
        rec.read_identity = lambda: _program_identity(conn)
        trans = C.ADSTRANS_SERVERCYCLE if rec.mode == "cycle" else C.ADSTRANS_SERVERONCHA
        _fast_switch(+1)          # the ADS callback thread gets the GIL within ~0.5 ms instead of up to 5 ms
        rec._fast_switch = True

        def make_cb(bid: int) -> Callable:
            def cb(notification: Any, _data: Any) -> None:
                try:
                    hdr = notification.contents
                    n = hdr.cbSampleSize
                    buf = ctypes.string_at(ctypes.addressof(hdr) + _HDR_DATA_OFFSET, n)
                    rec.enqueue(hdr.nTimeStamp, bid, buf)
                except Exception as exc:  # noqa: BLE001
                    if len(rec.errors) < 20:
                        rec.errors.append(f"notification block {bid}: {exc}")
            return cb

        failed: list[Block] = []
        for b in rec.blocks:
            attr = pyads.NotificationAttrib(b.size, trans, max_delay=max_delay_ms or 0, cycle_time=rec.cycle_ms)
            try:
                h = conn.add_device_notification((b.igrp, b.ioffs), attr, make_cb(b.bid))
                if h:
                    rec.handles.append(h)
                b.registered = True
            except Exception as exc:  # noqa: BLE001
                b.error = _fmt_exc(exc)
                failed.append(b)
        if failed:
            rec.unsubscribed = [s.name for b in failed for s in b.slots]
            rec.errors.extend(f"block {b.bid} ({b.slots[0].name}…, {b.size} bytes): {b.error}" for b in failed[:20])
            if not allow_partial or len(failed) == len(rec.blocks):
                self._stop_notifications(rec)
                raise AdsError(f"{len(failed)} of {len(rec.blocks)} notifications could not be registered "
                               f"({len(rec.unsubscribed)} symbols, e.g. {rec.unsubscribed[:3]}): {failed[0].error}. "
                               "Nothing is recorded rather than a recording with holes; pass allow_partial=true to "
                               "record the rest (the missing symbols are then listed in unsubscribed_symbols).")

    def _stop_notifications(self, rec: Recording) -> None:
        with rec._lock:
            conn, rec.conn = rec.conn, None
            handles, rec.handles = list(rec.handles), []
            fs, rec._fast_switch = getattr(rec, "_fast_switch", False), False
        if fs:
            _fast_switch(-1)
        if conn is None:
            return
        for nh, uh in handles:
            try:
                conn.del_device_notification(nh, uh)
            except Exception:  # noqa: BLE001
                pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def _start_poll(self, rec: Recording, port: int | None) -> None:
        base = self.ads.conn(port)
        conn = pyads.Connection(base.ams_net_id, base.ams_net_port, base.ip_address)
        try:
            conn.open()
            conn.set_timeout(self.ads.cfg.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            raise AdsError(f"recorder connection failed: {_fmt_exc(exc)}") from exc
        rec.conn = conn
        types = rec.types
        # the addresses the recording resolved at its start (the same ones notification mode subscribes to): one sum
        # read per cycle, never pyads' per-connection name cache (stale after a download)
        items = [(s, types[s.name]) for s in rec.symbols]
        interval = rec.cycle_ms / 1000.0

        def loop() -> None:
            last_ok: float | None = None
            while not rec._stop.is_set():
                t_cycle = time.perf_counter()
                ts = time.time()
                try:
                    # R43: the symbol version is read in the same sum request; values of another program never come back
                    got = self.ads.read_values(conn, items, decode=False, expect_version=rec._ver)
                except ProgramChanged as exc:
                    if exc.now is None:            # not verified: not recorded (counted)
                        rec.unverified_polls += 1
                        if len(rec.errors) < 20:
                            rec.errors.append(f"poll: {exc}")
                        time.sleep(min(1.0, interval * 5))
                        continue
                    t_last = (None if last_ok is None or rec.t0_wall is None
                              else round((last_ok - rec.t0_wall) * 1000.0, 4))
                    rec.program_changed = {"after_ms": t_last, "symbol_version": [exc.expected, exc.now],
                                           "rule": "poll mode: every poll reads the symbol version with the values; "
                                                   "the first poll of the new program is not recorded"}
                    rec.request_stop(f"the PLC program changed (symbol version {exc.expected} -> {exc.now}: download / "
                                     "online change) - the recording ends at the last poll of the program it started "
                                     "with")
                    break
                except Exception as exc:  # noqa: BLE001
                    if len(rec.errors) < 20:
                        rec.errors.append(f"poll: {_fmt_exc(exc)}")
                    time.sleep(min(1.0, interval * 5))
                    continue
                bad = {n: v["error"] for n, v in got.items() if isinstance(v, dict) and set(v) == {"error"}}
                for n, e in bad.items():
                    # one symbol the runtime refuses no longer costs the others their rows (review): its samples are
                    # missing, counted per symbol (footer poll_refused) and the recording is labelled for it
                    r_ = rec.poll_refused.setdefault(n, {"count": 0, "first_error": e})
                    r_["count"] += 1
                good = {n: v for n, v in got.items() if n not in bad}
                if good:
                    rec.enqueue(ts, None, good)
                    last_ok = ts
                elif len(rec.errors) < 20:
                    rec.errors.append("poll: " + "; ".join(f"{n}: {e}" for n, e in list(bad.items())[:5]))
                time.sleep(max(0.0, interval - (time.perf_counter() - t_cycle)))
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        rec._poll_thread = threading.Thread(target=loop, name=f"rec-poll-{rec.id}", daemon=True)
        rec._poll_thread.start()

    def stop(self, rid: str, reason: str = "requested") -> dict[str, Any]:
        rec = self._get(rid)
        if getattr(rec, "engine", "python") == "native":
            st = rec.stop(reason)
            st["stats"] = None
            st["stats_note"] = "native engine: rec_stats(id, symbols=[…]) computes exact statistics from the file"
            return st
        if rec.active:
            # reason first: a limit check racing the release must not claim the stop. N56: in cycle mode the stop ends
            # at the newest PLC time received now; the ingest thread ends the recording once every block delivered up
            # to it (at most max_delay_ms + 1 s) — the notifications stay registered until then
            rec.request_stop(reason, at_plc_time=True)
            if rec.stop_at_cut:
                rec._stop.wait(timeout=TAIL_WAIT_S + rec.max_delay_ms / 1000.0 + 5.0)
                rec.request_stop(reason)      # the wait is over in any case
            if rec.mode != "poll":
                self._stop_notifications(rec)
            if rec._poll_thread:
                rec._poll_thread.join(timeout=5)
        if rec._thread:
            rec._thread.join(timeout=DRAIN_TIMEOUT_S + 10)   # also when it ended by itself: the footer/seal may be in progress (D7)
        st = rec.status()
        if rec.live_stats and rec.stats_samples == rec.samples and len(rec.symbols) <= 50:
            st["stats"] = rec.stats_dict(None, False, 0)
        else:
            st["stats"] = None
            st["stats_note"] = (f"{len(rec.symbols)} leaves" + ("" if rec.live_stats else ", high-rate recording") +
                                " — rec_stats(id, symbols=[…]) computes exact statistics from the file")
        return st

    def stop_all(self) -> None:
        for rid, rec in list(self.recs.items()):
            if rec.active:
                try:
                    self.stop(rid, "server shutdown")
                except Exception:  # noqa: BLE001
                    pass

    def reap(self) -> None:
        """Release ADS handles of recordings that ended on their own (limits/stop_when). Fourteenth review of 0.8.16:
        over a copy — a native recording's reap waits for its process and hashes its file (seconds), and a rec_start
        or rec_delete on another thread meanwhile made every status call fail ('dictionary changed size during
        iteration')."""
        for rec in list(self.recs.values()):
            if getattr(rec, "engine", "python") == "native":
                rec.reap()
            elif not rec.active and rec.conn is not None:
                self._stop_notifications(rec)

    # ------------------------------------------------------------ queries
    def _get(self, rid: str) -> Recording:
        rec = self.recs.get(rid)
        if rec is None:
            cands = [r for k, r in list(self.recs.items()) if k.startswith(rid)]
            if len(cands) == 1:
                return cands[0]
            raise RecorderError(f"recording {rid!r} unknown in this session (rec_list shows files on disk)")
        return rec

    def list(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.reap()
        live = []
        # D21: the session list is paged like the files (newest first); session_total says how many there are
        recs = sorted(list(self.recs.values()), key=lambda r: getattr(r, "id", ""), reverse=True)
        for r in recs[offset:offset + limit]:
            s = r.status()
            live.append({k: s[k] for k in ("id", "name", "active", "mode", "symbols", "events", "samples", "missing_samples",
                                           "dropped", "label", "stop_reason", "file", "file_bytes") if k in s})
        files = []
        if self.root.is_dir():
            for p in sorted(self.root.glob("*.jsonl"), reverse=True):
                rid = p.stem
                if rid in self.recs:
                    continue
                files.append(p)
        total = len(files)
        out_files = []
        for p in files[offset:offset + limit]:
            try:
                with open(p, encoding="utf-8") as fh:
                    head = json.loads(fh.readline())
            except Exception:  # noqa: BLE001
                head = {}
            out_files.append({"id": p.stem, "file": str(p), "bytes": p.stat().st_size, "name": head.get("name"),
                              "mode": head.get("mode"), "format": head.get("format", 1),
                              "symbols": len(head.get("symbols", [])), "created": head.get("created")})
        return {"session": live, "session_total": len(recs), "on_disk": out_files, "on_disk_total": total,
                "offset": offset, "limit": limit, "root": str(self.root)}

    def status(self, rid: str) -> dict[str, Any]:
        self.reap()
        return self._get(rid).status()

    def snapshot(self, rid: str, last_n: int, symbols: list[str] | None) -> dict[str, Any]:
        try:
            rec = self._get(rid)
        except RecorderError:
            # R13: a recording from an earlier server session — its tail comes from the file
            path = self._path_for(rid)
            want = {x.lower() for x in symbols} if symbols else None
            ring: deque[dict[str, Any]] = deque(maxlen=max(1, last_n))
            n = 0
            for t, nm, v in iter_events(path, want):
                n += 1
                ring.append({"t_ms": round(t, 3), "symbol": nm, "value": v})
            ft = read_footer(path) or {}
            return {"id": path.stem, "active": False, "events": ft.get("events", n), "rows": list(ring),
                    "source": "file (recording from an earlier server session)"}
        try:
            # D32: a native recording's counters are refreshed by status() (the service / its footer); a finished one
            # that nobody asked showed events: 0 next to its rows
            n_ev = rec.status().get("events", rec.events)
        except Exception:  # noqa: BLE001
            n_ev = rec.events
        return {"id": rec.id, "active": rec.active, "events": n_ev, "rows": rec.snapshot(last_n, symbols)}

    def stats(self, rid: str, symbols: list[str] | None, include_runs: bool, max_runs_out: int,
              exact: bool | None = None) -> dict[str, Any]:
        """Live statistics while they are complete; from the file when the ingest had to skip them under load
        (stats_coverage < 1), when the recording is from an earlier session, or with exact=true."""
        rec = self._rec_or_none(rid)
        if rec is not None and getattr(rec, "engine", "python") == "native":
            rec = None if not rec.active else rec
            if rec is not None:
                return {"id": rid, "active": True, "source": "none",
                        "note": "native engine recordings have no live statistics: rec_stop first, then rec_stats (exact, from the file)"}
        if rec is not None:
            coverage = rec.stats_samples / rec.samples if rec.samples else 1.0
            if not exact and (coverage >= 1.0 or rec.active):
                out = {"id": rec.id, "active": rec.active, "events": rec.events, "elapsed_ms": self._elapsed_ms(rec),
                       "triggered_at_ms": rec.triggered_at, "source": "live",
                       "symbols": rec.stats_dict(symbols, include_runs, max_runs_out)}
                if coverage < 1.0:
                    out["stats_coverage"] = round(coverage, 4)
                    out["note"] = "live statistics skipped samples under load; call again after rec_stop (or exact=true) for exact values"
                return out
        want = {s.lower() for s in symbols} if symbols else None
        st = self._file_stats(rid, want)
        return {"id": rid, "active": bool(rec and rec.active), "source": "file",
                "events": sum(s.count for s in st.values()), "symbols": _stats_out(st, symbols, include_runs, max_runs_out)}

    def _rec_or_none(self, rid: str) -> Any:
        rec = self.recs.get(rid)
        if rec is None:
            try:
                rec = self._get(rid)
            except RecorderError:
                rec = None
        return rec

    def _file_stats(self, rid: str, want: set[str] | None = None) -> dict[str, SymStats]:
        """Exact statistics from the recording's file — what rec_stats gives after the recording, for either engine."""
        st: dict[str, SymStats] = {}
        for t, n, v in iter_events(self._path_for(rid), want):
            st.setdefault(n, SymStats()).ingest(t, v, _numeric_dec(v), self.max_runs)
        return st

    def _final_stats(self, rid: str) -> tuple[dict[str, SymStats], str]:
        """The statistics of a recording as rec_stats gives them once it has stopped: the live ones of a Python-engine
        recording only when they are complete (nothing skipped under load), else — native engine (no live
        statistics, N107), a recording from an earlier session, live ones with gaps — exact, from the file.
        -> (stats, 'live' | 'file')."""
        rec = self._rec_or_none(rid)
        if (rec is not None and getattr(rec, "engine", "python") == "python" and not rec.active
                and (not rec.samples or rec.stats_samples >= rec.samples)):
            return rec.stats, "live"
        return self._file_stats(rid), "file"

    @staticmethod
    def _elapsed_ms(rec: Recording) -> float | None:
        if rec.t0_mono is None:
            return None
        end = rec.stopped_mono or time.monotonic()
        return round((end - rec.t0_mono) * 1000, 1)

    def read(self, rid: str, t_from_ms: float | None, t_to_ms: float | None, symbols: list[str] | None,
             max_rows: int, wide: bool, fill: bool = False) -> dict[str, Any]:
        """Stream the file. wide: one row per PLC timestamp, one column per symbol, null where that symbol has no sample
        at that time (D9: a row that never existed in the PLC is not produced). fill: repeat the last known value into
        the empty cells and name them in the row's "_filled" list."""
        path = self._path_for(rid)
        want = {s.lower() for s in symbols} if symbols else None
        header = _read_header(path)
        cols = [s["name"] for s in header.get("symbols", []) if not want or s["name"].lower() in want]
        rows: list[Any] = []
        last: dict[str, Any] = {}          # last known value per column (fill only)
        row_t: float | None = None
        row_vals: dict[str, Any] = {}
        truncated = False
        filled_cells = 0

        def flush() -> None:
            nonlocal filled_cells
            if row_t is None:
                return
            r: dict[str, Any] = {"t_ms": row_t}
            filled = []
            for c in cols:
                if c in row_vals:
                    r[c] = row_vals[c]
                elif fill and c in last:
                    r[c] = last[c]
                    filled.append(c)
                else:
                    r[c] = None
            if fill:
                r["_filled"] = filled
                filled_cells += len(filled)
            rows.append(r)

        for t, n, v in iter_events(path, want):
            if t_from_ms is not None and t < t_from_ms:
                if wide and fill:
                    last[n] = v
                continue
            if t_to_ms is not None and t > t_to_ms:
                break
            if wide:
                if row_t is not None and t != row_t:
                    flush()
                    if len(rows) >= max_rows:
                        truncated = True
                        row_t = None
                        break
                    row_vals = {}
                row_t = t
                row_vals[n] = v
                last[n] = v
            else:
                rows.append({"t_ms": t, "symbol": n, "value": v})
                if len(rows) >= max_rows:
                    truncated = True
                    break
        if wide and row_t is not None and len(rows) < max_rows:
            flush()
        out = {"id": rid, "file": str(path), "name": header.get("name"), "time_source": header.get("time_source", "plc"),
               "rows": rows, "count": len(rows), "truncated": truncated}
        if wide:
            out["note"] = ("one row per PLC timestamp; null = no sample of that symbol at that time" if not fill else
                           f"one row per PLC timestamp; {filled_cells} cells forward-filled from the last sample (listed per row in _filled)")
        return out

    def export(self, rid: str, fmt: str, out_path: str | None, symbols: list[str] | None,
               fill: bool = False, overwrite: bool = False) -> dict[str, Any]:
        """csv_wide: one row per PLC sample time, one column per variable and per array element (name[i]); a cell is
        empty when that variable has no sample at that time (fill=true repeats the last known value instead —
        convenient for charts, but then a cell no longer means "sampled at this time"). csv_long: one row per value.
        Both carry plc_filetime (the PLC's own timestamp of the sample) when the recording has one."""
        path = self._path_for(rid)
        if fmt not in ("csv_long", "csv_wide", "jsonl"):
            raise RecorderError("fmt must be csv_long | csv_wide | jsonl")
        if fmt == "jsonl":
            return {"id": rid, "file": str(path), "format": "jsonl", "note": "already on disk in this format"}
        out = Path(out_path) if out_path else path.with_suffix(f".{'wide' if fmt == 'csv_wide' else 'long'}.csv")
        bad = _export_target_refusal(out, path)
        if bad:
            raise RecorderError(f"{out} cannot be an export target: {bad} — whatever overwrite says (nothing was written)")
        params = {"fmt": fmt, "symbols": symbols, "fill": fill}
        replaced = None
        if out.exists():
            # N32: a re-export to the default path replaced the tool's own earlier CSV (other symbols, fill) silently
            same, earlier = _earlier_export(out, path, params)
            if not same and not overwrite:
                raise RecorderError(f"{out} exists — {earlier}; pass overwrite=true to replace it, or out_path for a "
                                    "second file (nothing was written)")
            replaced = "identical (same source bytes and parameters)" if same else earlier
        want = {s.lower() for s in symbols} if symbols else None
        header = _read_header(path)
        syms = [x for x in header.get("symbols", []) if not want or x["name"].lower() in want]
        cols: list[str] = []
        expand: dict[str, list[str]] = {}
        for x in syms:
            tt = T.parse_type(x.get("type") or "", x.get("size"))
            if tt.kind == "array" and tt.dims and not tt.opaque:
                names = _element_names(x["name"], tt.dims)
                expand[x["name"]] = names
                cols += names
            else:
                cols.append(x["name"])
        t0 = None
        t0d = _read_t0(path)
        if t0d and "filetime" in t0d:
            t0 = int(t0d["filetime"])
        import csv
        n = 0
        rows_written = 0
        # N59: written next to the target and moved over it only when complete — a failure halfway (disk full, a bad
        # value, a crash) leaves the previous export as it was, never a truncated file under its name
        tmp = out.with_name(f".{out.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        counts = {"rows": 0, "values": 0}
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as fo:
                self._write_export(fo, csv, fmt, path, want, cols, expand, t0, fill, counts)
            rows_written, n = counts["rows"], counts["values"]
            footer = read_footer(path)
            prov = self._provenance(path, out, footer, params,
                                    {"rows": rows_written, "values": n, "columns": len(cols) if fmt == "csv_wide" else 4},
                                    data_file=tmp)
            make_writable(out)
            os.replace(tmp, out)
            self._write_provenance(out, prov)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return self._export_result(rid, out, fmt, header, rows_written, n, t0, footer, fill, replaced, prov)

    def _write_export(self, fo: Any, csv: Any, fmt: str, path: Path, want: set[str] | None, cols: list[str],
                      expand: dict[str, list[str]], t0: int | None, fill: bool, counts: dict[str, int]) -> None:
        if True:
            w = csv.writer(fo)
            plc_col = ["plc_filetime", "time_utc"] if t0 is not None else []
            w.writerow(["t_ms", *plc_col, *cols] if fmt == "csv_wide" else ["t_ms", *plc_col, "symbol", "value"])
            cur: dict[str, Any] = {}
            row_t: float | None = None

            def flush() -> None:
                if row_t is None:
                    return
                ft = _ft_cols(t0 + int(round(row_t * 1e4))) if t0 is not None else []
                w.writerow([row_t, *ft, *[cur.get(c, "") for c in cols]])
                counts["rows"] += 1

            for t, nme, v in iter_events(path, want):
                if fmt == "csv_wide":
                    if row_t is not None and t != row_t:
                        flush()
                        if not fill:
                            cur = {}
                    row_t = t
                    names = expand.get(nme)
                    if names and isinstance(v, list):
                        for c, ev in zip(names, _flatten_values(v)):
                            cur[c] = _flat(ev)
                    else:
                        cur[nme] = _flat(v)
                else:
                    ft = _ft_cols(t0 + int(round(t * 1e4))) if t0 is not None else []
                    w.writerow([t, *ft, nme, _flat(v)])
                    counts["rows"] += 1
                counts["values"] += 1
            if fmt == "csv_wide":
                flush()

    def _export_result(self, rid: str, out: Path, fmt: str, header: dict[str, Any], rows_written: int, n: int,
                       t0: int | None, footer: dict[str, Any] | None, fill: bool, replaced: str | None,
                       prov: dict[str, Any]) -> dict[str, Any]:
        out_d: dict[str, Any] = {"id": rid, "file": str(out), "format": fmt, "rows": rows_written, "values": n,
                                 "time_source": header.get("time_source", "plc"),
                                 "note": ("one row per sample time; empty cell = no sample of that variable at that time"
                                          if fmt == "csv_wide" and not fill else
                                          "one row per sample time; empty cells forward-filled from the last sample"
                                          if fmt == "csv_wide" else "one row per value")}
        if t0 is not None:
            out_d["note"] += ("; plc_filetime = the PLC's own timestamp of the sample (100 ns since 1601-01-01 UTC), "
                              "time_utc = the same instant as ISO-8601 UTC, all 7 decimals (no rounding)")
        if footer is not None:
            for k in ("events", "samples", "missing_samples", "dropped", "dropped_on_stop", "stop_reason"):
                if k in footer:
                    out_d[k if k != "events" else "events_in_file"] = footer.get(k)
        else:
            out_d["complete"] = False
            out_d["note"] += "; no footer: the recording is still running or the process died before it ended"
        # R10: the export is a derived file — its provenance records exactly what it was derived from and how
        out_d["provenance"] = {k: prov[k] for k in ("file", "source_sha256", "source_label", "export_sha256")}
        if replaced:
            out_d["replaced"] = replaced
        if fill:
            out_d["note"] += "; fill=true: cells were forward-filled (recorded in the provenance file)"
        return out_d

    def _provenance(self, src: Path, out: Path, footer: dict[str, Any] | None, params: dict[str, Any],
                    counts: dict[str, Any], data_file: Path | None = None) -> dict[str, Any]:
        """The provenance record of an export (data_file: where its bytes are now, if not at out yet)."""
        pp = provenance_path(out)
        ssha, ssize = file_sha256(src)
        esha, esize = file_sha256(data_file or out)
        f = footer or {}
        p: dict[str, Any] = {"_provenance": 1, "export_file": out.name, "export_sha256": esha, "export_bytes": esize,
                             "source_file": str(src), "source_sha256": ssha, "source_bytes": ssize,
                             "source_footer_sha256": f.get("sha256"), "source_footer_hmac": f.get("hmac"),
                             "source_label": f.get("label"), "source_complete": bool(footer),
                             "source_seal": self._check_seal(src), "params": params, **counts,
                             "exporter": f"twincat-mcp {_pkg_version()}",
                             "created": dt.datetime.now(dt.timezone.utc).isoformat()}
        if self.key:
            p["hmac"] = _manifest_sig(self.key, p)
        p["file"] = str(pp)
        return p

    @staticmethod
    def _write_provenance(out: Path, prov: dict[str, Any]) -> None:
        pp = provenance_path(out)
        body = {k: v for k, v in prov.items() if k != "file"}
        tmp = pp.with_name(f".{pp.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(body, indent=1, default=str) + "\n")
        make_writable(pp)
        os.replace(tmp, pp)

    def _settle(self, rec: Any) -> None:
        """Wait until a recording's own threads are done with its file (footer written, sealed): a recording that ended
        by itself is 'inactive' while _finish is still hashing and chmod-ing — deleting then races (D7)."""
        if getattr(rec, "engine", "python") == "native":
            if rec.proc.finished.is_set():
                rec.reap()
            return
        th = getattr(rec, "_thread", None)
        if th is not None and th.is_alive():
            th.join(timeout=DRAIN_TIMEOUT_S + 10)

    def delete(self, rid: str) -> dict[str, Any]:
        rec = self.recs.get(rid)
        if rec and rec.active:
            self.stop(rid, "deleted")
        if rec is not None:
            self._settle(rec)
        path = self._path_for(rid)
        removed = []
        for p in (path, manifest_path(path), path.with_suffix(".wide.csv"), path.with_suffix(".long.csv"),
                  provenance_path(path.with_suffix(".wide.csv")), provenance_path(path.with_suffix(".long.csv"))):
            if p.exists():
                _remove_file(p)
                removed.append(str(p))
        self.recs.pop(rid, None)
        return {"id": rid, "removed": removed}

    def seal_unsealed(self, max_files: int = 500) -> dict[str, Any]:
        """N27 / server start: a recording whose footer is in the file but that has no manifest (the server went away
        before sealing) is sealed now. Only files with a footer: an unfinished file must not be sealed."""
        sealed, skipped = [], 0
        if not self.root.is_dir():
            return {"sealed": [], "checked": 0}
        files = sorted(self.root.glob("*.jsonl"))[-max_files:]
        for f in files:
            if manifest_path(f).exists() or self.recs.get(f.stem) is not None and self.recs[f.stem].active:
                continue
            if read_footer(f) is None:
                skipped += 1
                continue
            if seal_recording(f, self.key) is not None:
                sealed.append(f.stem)
        return {"sealed": sealed, "checked": len(files), "unfinished": skipped}

    # ------------------------------------------------------------ verification
    def _integrity(self, path: Path) -> dict[str, Any]:
        """Integrity of the file as written: running SHA-256 checked at every ``_chain`` checkpoint and at the
        footer, each signature checked with this install's key, then the sealed sidecar (whole-file hash)."""
        out: dict[str, Any] = {}
        sha = hashlib.sha256()
        nbytes = nlines = 0
        footer = None
        chk = {"checked": 0, "bad": None, "last_ok_line": 0, "last_ok_bytes": 0}
        unsigned = False
        with open(path, "rb") as fh:
            for raw in fh:
                if raw.startswith(b'{"_footer"'):
                    try:
                        footer = json.loads(raw)
                    except json.JSONDecodeError:
                        footer = None
                    break
                if raw.startswith(b'{"_chain"') and chk["bad"] is None:
                    try:
                        c = json.loads(raw)["_chain"]
                    except (json.JSONDecodeError, KeyError, TypeError):
                        c = None
                    d = sha.hexdigest()
                    if (c is None or c.get("sha256") != d or c.get("bytes") != nbytes or c.get("lines") != nlines):
                        chk["bad"] = f"checkpoint at line {nlines + 1} does not match the content before it"
                    elif "hmac" in c and _hmac.new(self.key, d.encode("ascii"), hashlib.sha256).hexdigest() != c["hmac"]:
                        chk["bad"] = f"checkpoint at line {nlines + 1} is not signed by this install's key"
                    else:
                        unsigned |= "hmac" not in c
                        chk["checked"] += 1
                        chk["last_ok_line"], chk["last_ok_bytes"] = nlines, nbytes
                sha.update(raw)
                nbytes += len(raw)
                nlines += 1
        out["checkpoints"] = {"verified": chk["checked"], "last_verified_line": chk["last_ok_line"]}
        if chk["bad"]:
            out["checkpoints"]["problem"] = chk["bad"]
        if footer is None:
            if chk["bad"]:
                out["integrity"] = f"MODIFIED — {chk['bad']} (and no footer)"
            elif chk["checked"]:
                after = nlines - chk["last_ok_line"]
                out["integrity"] = (f"NO FOOTER — intact up to the last checkpoint (line {chk['last_ok_line']}); "
                                    f"the {after} line(s) after it cannot be proven (process killed or still writing?)")
            else:
                out["integrity"] = "NO FOOTER — the recording did not end cleanly (process killed?); content cannot be proven"
        else:
            digest = sha.hexdigest()
            if digest != footer.get("sha256"):
                where = f"; {chk['bad']}" if chk["bad"] else (f"; intact up to line {chk['last_ok_line']}" if chk["checked"] else "")
                out["integrity"] = "MODIFIED — the content does not match the checksum written when the recording ended" + where
            elif chk["bad"]:
                out["integrity"] = f"MODIFIED — {chk['bad']} although the footer checksum matches (checksum re-written?)"
            elif "hmac" in footer and _hmac.new(self.key, digest.encode("ascii"), hashlib.sha256).hexdigest() != footer["hmac"]:
                out["integrity"] = "CHECKSUM RE-WRITTEN — content matches its checksum but the signature is not this install's"
            elif "hmac" not in footer or unsigned:
                out["integrity"] = "intact (unsigned: recorded without a signing key)"
            else:
                out["integrity"] = "intact"
            out["footer"] = {k: footer.get(k) for k in ("events", "samples", "missing_samples", "dropped", "dropped_on_stop",
                                                         "after_limit", "unsubscribed", "stop_reason")}
            for k in ("label", "label_reason", "stalls", "stalled_samples", "stall_s", "resubscriptions",
                      "gap_markers", "chain_checkpoints", "engine", "engine_version", "overflow_samples",
                      "missing_head", "missing_gaps", "missing_tail", "requested_s", "window_start_ms", "window_end_ms",
                      "verdict_hint"):
                if k in footer:
                    out["footer"][k] = footer[k]
        out["seal"] = self._check_seal(path)
        out["read_only"] = is_read_only(path)
        out["_footer_raw"] = footer
        return out

    def _check_seal(self, path: Path) -> str:
        mp = manifest_path(path)
        if not mp.is_file():
            return "no manifest (recorded before 0.8.1, or the server stopped before sealing)"
        try:
            m = json.loads(mp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "manifest unreadable"
        if "hmac" in m and _manifest_sig(self.key, m) != m["hmac"]:
            return "MANIFEST NOT SIGNED BY THIS INSTALL — the manifest was edited or comes from another install"
        sha, size = file_sha256(path)
        if sha != m.get("sha256_file") or size != m.get("size"):
            return f"FILE CHANGED SINCE SEALING — sealed {m.get('size')} bytes, now {size}; hashes differ"
        return "matches manifest" + ("" if "hmac" in m else " (unsigned)")

    def verify(self, rid: str, counter: str | None = None, step: int | None = None, max_gaps: int = 20) -> dict[str, Any]:
        """Prove a recording: (1) integrity — the file is byte-for-byte what the recorder wrote (sha256 over every
        line, signed with this install's key); (2) timeline — per notification block, PLC timestamps without
        holes; (3) optional ground truth — a counter that increments every PLC cycle (e.g. your own nCycleCount
        or TwinCAT_SystemInfoVarList._TaskInfo[1].CycleCount, recorded with the rest) must step by exactly
        `step` per sample (default: the step it shows most often)."""
        rec = self.recs.get(rid)
        if rec is not None and rec.active:
            raise RecorderError("recording is still running; rec_stop first (the footer with the checksum is written at the end)")
        path = self._path_for(rid)
        out: dict[str, Any] = {"id": rid, "file": str(path)}
        # (1) integrity
        out.update(self._integrity(path))
        footer = out.pop("_footer_raw")
        header = _read_header(path)
        out["time_source"] = header.get("time_source", "plc")
        if counter is None and header.get("cycle_counter"):
            counter = header["cycle_counter"]          # recorded automatically with the data (cycle mode)
            out["counter_source"] = "recorded automatically"
        if header.get("manifest"):
            out["manifest"] = header["manifest"]
        t0 = _read_t0(path)
        if t0:
            out["t0"] = t0
        # (2) timeline per block
        if header.get("format") == 2 and header.get("blocks") and header.get("mode") == "cycle":
            names, blocks = blocks_from_header(header)
            # a block whose notification could not be registered (allow_partial) is not part of the timeline: its
            # symbols are in the footer's unsubscribed list, and that alone fails the verdict
            unreg = sorted(set((footer or {}).get("unregistered_blocks") or []) & set(blocks))
            for k in unreg:
                blocks.pop(k, None)
            wend = (footer or {}).get("window_end_ms")
            wend_t = _ticks(wend) if isinstance(wend, (int, float)) else None
            ws0 = (footer or {}).get("window_start_ms")
            wstart_t = _ticks(ws0) if (footer or {}).get("rows_before_window") and isinstance(ws0, (int, float)) else None
            rows_after = rows_before = 0
            for ev in _iter_raw_rows(path):
                t, k = ev[0], ev[1]
                if wend_t is not None and isinstance(t, (int, float)) and _ticks(t) > wend_t:
                    rows_after += 1            # in the file, past the window: not part of the recording (readers skip)
                    continue
                if wstart_t is not None and isinstance(t, (int, float)) and _ticks(t) < wstart_t:
                    rows_before += 1           # N84: before every block had delivered: not part of the recording
                    continue
                if isinstance(k, str) and k.startswith("#"):
                    b = blocks.get(int(k[1:]))
                    if b is not None:
                        b.note_stamp(t)
            # N56: an independent check of the file — its own window, one allowance for every recording; the engine's
            # footer gives the window bounds (PLC times), never a tolerance
            rep = []
            tot_s = tot_m = 0
            ends = {b.bid: b.last_t for b in blocks.values() if b.last_t is not None}
            ft_ = footer or {}
            end = ft_.get("window_end_ms")
            start = ft_.get("window_start_ms")
            legacy_stop = end_not_waited_for(ft_)
            if legacy_stop and ends:
                end = min(ends.values())
            elif end is None and ends:
                end = max(ends.values())
            if ends:
                first_end = min(ends, key=lambda k: ends[k])
                spread = max(ends.values()) - min(ends.values())
                periods = [b.min_step for b in blocks.values() if b.min_step]
                out["block_ends"] = {"first_to_end": first_end, "spread_ms": round(spread, 4),
                                     "ends_ms": {str(k): round(v, 4) for k, v in sorted(ends.items())}}
                if legacy_stop:
                    out["tail_rule"] = (
                        "the footer has no window end every block was waited for (a stop that cut the notifications at "
                        "once — ≤ 0.8.9 stop on request, ≤ 0.8.9 Python stop_when / max_events, or no footer): the "
                        f"window is checked up to the earliest block end ({end:.4f} ms, block {first_end}); the "
                        f"{spread:g} ms after it differ per block and cannot be told from a loss")
                    if periods and spread > max(periods) + 1e-6:
                        out["inconclusive"] = (f"the blocks ended {spread:g} ms apart (more than one period): the last "
                                               f"{spread:g} ms of block(s) other than {first_end} are not verified")
            fb = fallback_period(blocks.values(), header.get("cycle_ms"))
            org = window_origin(blocks.values(), start)
            for b in blocks.values():
                m = b.missing(end, start, fallback=fb, origin=org)
                tot_s += b.samples
                tot_m += m
                row = {"block": b.bid, "first": b.slots[0].name if b.slots else None, "symbols": len(b.slots),
                       "samples": b.samples, "missing": m, "missing_head": b.head_missing(start, fb),
                       "missing_tail": b.tail_missing(end, fb, org),
                       "end_ms": None if b.last_t is None else round(b.last_t, 4),
                       "period_ms": None if b.min_step is None else round(b.min_step, 4),
                       "duplicate_stamps": b.dup_stamps, "first_gaps": b.gaps[:5]}
                if b.samples < 2:
                    row["period_note"] = ("never delivered: the whole window counts as missing" if b.samples == 0 else
                                          "one sample: counted with the other blocks' period")
                rep.append(row)
            unprovable = unprovable_blocks(blocks.values(), fb)
            out["timeline"] = {"samples": tot_s, "missing": tot_m, "window_start_ms": start, "window_end_ms": end,
                               "allowance": "1 period at the end of each block, none inside or at the start",
                               "completeness": round(tot_s / (tot_s + tot_m), 6) if tot_s + tot_m else None,
                               "blocks": rep[:50]}
            if unprovable:
                out["timeline"]["unprovable_blocks"] = unprovable[:50]
            if unreg:
                out["timeline"]["unregistered_blocks"] = unreg[:50]
            if rows_before:
                out["timeline"]["rows_before_window"] = rows_before
                out["timeline"]["rows_before_window_note"] = (
                    "rows before window_start_ms: a block delivered before every block had (the window starts when "
                    "all have); in the file as delivered, not part of the recording — rec_read / rec_stats / rec_export "
                    "skip them")
            if rows_after:
                out["timeline"]["rows_after_window"] = rows_after
                out["timeline"]["rows_after_window_note"] = (
                    "rows past window_end_ms, written before the end was known (a block ahead in ADS order — Python "
                    "recorder): not part of the recording; rec_read / rec_stats / rec_export skip them")
        elif header.get("mode") != "cycle":
            out["timeline"] = {"note": (
                "change mode: the PLC sends a sample only when a value changes, so a quiet period cannot be told from a "
                "lost sample" if header.get("mode") == "change" else
                "poll mode: samples are taken by this PC's clock (sum read every cycle_ms), not by the PLC, so the PLC "
                "timeline cannot be checked") + " — record in cycle mode to prove completeness"}
        # (3) counter
        if counter:
            vals: list[tuple[float, int]] = []
            for t, n, v in iter_events(path, {counter.lower()}):
                rv = _raw_val(v)
                if isinstance(rv, (int, float)) and not isinstance(rv, bool):
                    vals.append((t, int(rv)))
            if len(vals) < 2:
                out["counter"] = {"symbol": counter, "error": "fewer than 2 samples of this symbol in the recording"}
            else:
                diffs = [b[1] - a[1] for a, b in zip(vals, vals[1:])]
                if step is None:
                    pos = [d for d in diffs if d > 0]
                    step = max(set(pos), key=pos.count) if pos else 1
                gaps, dups, back, missing = [], 0, 0, 0
                resets: list[dict[str, Any]] = []
                dup_pairs: list[tuple[float, float]] = []
                seg_first = vals[0][1]
                span = 0
                for (ta, va), (tb, vb), d in zip(vals, vals[1:], diffs):
                    if d == 0:
                        dups += 1
                        dup_pairs.append((ta, tb))
                    elif d < 0:
                        # D46: the counter restarted (TwinCAT restart / RUN after CONFIG): a new segment. Across it the
                        # counter proves nothing — the restart time is checked by the timeline, not by the counter
                        back += 1
                        span += (va - seg_first) // step + 1
                        seg_first = vb
                        if len(resets) < max_gaps:
                            resets.append({"t_ms": tb, "from": va, "to": vb, "gap_ms": round(tb - ta, 4)})
                    elif d != step:
                        miss = d // step - 1 if d % step == 0 else None
                        missing += miss or 0
                        if len(gaps) < max_gaps:
                            gaps.append({"t_ms": tb, "from": va, "to": vb, "missing_samples": miss})
                span += (vals[-1][1] - seg_first) // step + 1
                # D12: completeness = distinct counter values present / expected — repeated values (a stopped PLC
                # keeps sending the same CycleCount) are duplicates, never more than 100 %
                distinct = len(vals) - dups
                out["counter"] = {"symbol": counter, "samples": len(vals), "step": step, "expected_samples": span,
                                  "missing_samples": missing, "duplicates": dups, "backwards": back,
                                  "segments": back + 1,
                                  "completeness": round(min(1.0, distinct / span), 6) if span > 0 else None,
                                  "lossless": missing == 0 and dups == 0 and back == 0, "first_gaps": gaps}
                # N111: why a counter went back or repeated is said only from evidence — the task's own cycle counter
                # (recorded with the data in cycle mode). A program variable reset by the program (MAIN.bReset) is not
                # a TwinCAT restart, and one the program leaves alone for a cycle is not a stopped PLC.
                task = _TASK_COUNTER.search(counter) is not None
                ref = None if task else self._task_counter_series(path, header)
                if resets:
                    out["counter"]["resets"] = resets
                    out["counter"]["lossless_within_segments"] = missing == 0 and dups == 0
                    if task:
                        why = "the task's cycle counter restarted (TwinCAT restart / RUN after CONFIG)"
                    elif ref is not None:
                        restarts = kept = 0
                        for r in resets:
                            k = _ref_delta(ref[1], ref[2], r["t_ms"] - r["gap_ms"], r["t_ms"])
                            r["task_cycle_counter"] = ("restarted too: a TwinCAT restart" if k is not None and k < 0 else
                                                       "kept counting: the program set it back" if k is not None and k > 0
                                                       else "no sample of it around this time")
                            restarts += 1 if k is not None and k < 0 else 0
                            kept += 1 if k is not None and k > 0 else 0
                        why = (f"{restarts} with a TwinCAT restart (the task's cycle counter {ref[0]} restarted too), "
                               f"{kept} set back by the program (the task's cycle counter went on), "
                               f"{len(resets) - restarts - kept} not decidable (no sample of it around them)"
                               + (f"; {back - len(resets)} more beyond the {len(resets)} listed were not assessed"
                                  if back > len(resets) else ""))
                    else:
                        why = ("set back by the program, or by a TwinCAT restart — a program variable does not tell "
                               "which (record in cycle mode: the task's cycle counter is recorded with the data and "
                               "decides it)")
                    out["counter"]["reset_note"] = (f"the counter went back {back} time(s): {why}; checked per segment — "
                                                    "what happened in between is in the timeline check")
                if dups:
                    if task:
                        out["counter"]["note"] = (f"{dups} samples repeat the previous counter value: the task did not "
                                                  "run between them (PLC stopped / breakpoint) — frozen values, not new data")
                    elif ref is not None:
                        ran = sum(1 for ta, tb in dup_pairs if (_ref_delta(ref[1], ref[2], ta, tb) or 0) > 0)
                        out["counter"]["note"] = (
                            f"{dups} samples repeat the previous counter value: in {ran} of them the task ran (its cycle "
                            f"counter {ref[0]} went on — the program did not change this counter in that cycle), in "
                            f"{dups - ran} it did not run or could not be told (PLC stopped / breakpoint)")
                    else:
                        out["counter"]["note"] = (f"{dups} samples repeat the previous counter value: the program did not "
                                                  "change it in those cycles, or the task did not run (PLC stopped / "
                                                  "breakpoint) — a program variable does not tell which")
        ok = out.get("integrity", "").startswith("intact")
        tl = out.get("timeline") or {}
        cn = out.get("counter") or {}
        ft = footer or {}
        passed = (ok and not tl.get("missing") and cn.get("lossless", True) and not ft.get("dropped")
                  and not ft.get("dropped_on_stop") and not ft.get("overflow_samples") and not ft.get("stalls")
                  and ft.get("verdict_hint", "PASS") == "PASS"          # the engine's findings can only add a FAIL
                  # Python writes only a label; a legacy stop's label counted the per-block stop skew as a loss
                  and (ft.get("label") != "INCOMPLETE" or "tail_rule" in out)
                  and not tl.get("unprovable_blocks")                   # a block that never delivered proves nothing
                  and not ft.get("unsubscribed")                        # requested symbols that were never recorded
                  and not (tl.get("samples") == 0 and "blocks" in tl))  # nothing recorded is not a pass
        if passed and cn.get("error") and not out.get("inconclusive"):
            # a check that was asked for and could not run is not a check that held (a counter name not in the file)
            out["inconclusive"] = f"the counter check could not run: {cn['error']} ({cn.get('symbol')})"
        if passed and header.get("mode") != "cycle" and not (cn and "error" not in cn) and not out.get("inconclusive"):
            # change / poll: the file is intact, but nothing showed that it is complete (no PLC timeline, no counter) —
            # PASS here meant only "not tampered with"
            out["inconclusive"] = ("the file is intact, but a change / poll recording has no PLC timeline to show it "
                                   "is complete — record in cycle mode, or pass counter= (a symbol that changes every "
                                   "cycle) to check it")
        out["verdict"] = "FAIL" if not passed else ("INCONCLUSIVE" if out.get("inconclusive") else "PASS")
        return out

    # ------------------------------------------------------------ analysis
    def _events_for(self, rid: str, symbols: list[str]) -> list[dict[str, Any]]:
        return self.read(rid, None, None, symbols, 10_000_000, False)["rows"]

    def pulses(self, rid: str, symbol: str, max_list: int = 200, t_to: float | None = None) -> dict[str, Any]:
        """Pulse analysis of one symbol: for BOOL the high/low widths, period, duty;
        for any other type the value runs with dwell times (state machine timing)."""
        ev = self._events_for(rid, [symbol])
        if t_to is not None:
            ev = [e for e in ev if e["t_ms"] <= t_to]
        if not ev:
            raise RecorderError(f"no events for {symbol!r} in {rid}")
        runs: list[list[Any]] = []
        for e in ev:
            v = _raw_val(e["value"])
            if not runs or runs[-1][2] != v:
                if runs:
                    runs[-1][1] = e["t_ms"]
                runs.append([e["t_ms"], e["t_ms"], v])
            else:
                runs[-1][1] = e["t_ms"]
        span = ev[-1]["t_ms"] - ev[0]["t_ms"]
        is_bool = all(isinstance(r[2], bool) for r in runs)
        out: dict[str, Any] = {"id": rid, "symbol": symbol, "events": len(ev), "span_ms": round(span, 3), "runs": len(runs)}
        if is_bool:
            # the first run starts at the recording's first sample, the last has no end: neither is a measured pulse
            inner = runs[1:-1]
            highs = [(a, b) for a, b, v in inner if v]
            lows = [(a, b) for a, b, v in inner if not v]
            hw = [b - a for a, b in highs]
            lw = [b - a for a, b in lows]
            starts = [a for a, _ in highs]
            periods = [b - a for a, b in zip(starts, starts[1:])]
            # D26: duty over the pulse train (complete periods, rising edge to rising edge), not over the whole
            # recording — the idle time before the first pulse made 54 % read as 30.6 %
            duty = round(100 * sum(hw[:len(periods)]) / sum(periods), 2) if periods and sum(periods) > 0 else None
            out.update({
                "pulses": len(highs),
                "high_ms": _stat(hw), "low_ms": _stat(lw), "period_ms": _stat(periods),
                "frequency_hz": round(1000.0 / (sum(periods) / len(periods)), 3) if periods else None,
                "duty_percent": duty,
                "high_share_of_recording_percent": round(100 * sum(hw) / span, 2) if span > 0 and hw else None,
                "first_pulses": [{"t_on_ms": round(a, 3), "t_off_ms": round(b, 3), "width_ms": round(b - a, 3)} for a, b in highs[:max_list]],
                "note": ("the first run (from the recording's first sample) and the last (no end) are open-ended and not "
                         "counted; duty_percent = high time / period over complete periods"),
            })
        else:
            dw = [b - a for a, b, _ in runs[:-1]]
            out.update({"dwell_ms": _stat(dw),
                        "timeline": [{"t_start_ms": round(a, 3), "t_end_ms": round(b, 3), "dwell_ms": round(b - a, 3), "value": v}
                                     for a, b, v in runs[:max_list]]})
        return out

    def start_offsets_ms(self, rids: list[str], timeout_s: float = 3.0) -> dict[str, float]:
        """PLC time of each recording's first sample, relative to the earliest (D29); waits up to ``timeout_s``."""
        deadline = time.monotonic() + timeout_s
        ft: dict[str, int] = {}
        while True:
            for rid in rids:
                if rid in ft:
                    continue
                rec = self.recs.get(rid)
                v = getattr(rec, "t0_ft", None)
                if not isinstance(v, int):
                    try:
                        t0 = _read_t0(self._path_for(rid)) or {}
                    except RecorderError:
                        t0 = {}
                    v = t0.get("filetime")
                if isinstance(v, int):
                    ft[rid] = v
            if len(ft) == len(rids) or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if len(ft) < 2:
            return {}
        lo = min(ft.values())
        return {rid: round((v - lo) / 1e4, 3) for rid, v in ft.items()}

    def timing_of(self, rid: str, symbols: list[str]) -> dict[str, Any]:
        """How far the timestamps of ``symbols`` in this recording can be trusted relative to each other (D35).

        cycle mode: every sample carries the PLC cycle it was taken in → order exact, resolution = cycle_ms.
        change mode: the runtime checks each notification every cycle_ms, every notification at its own phase →
        inside one notification block the order is exact (resolution cycle_ms); across blocks a later change can
        carry an earlier timestamp (up to cycle_ms off) → order and differences unreliable.
        poll mode: PC-side sum reads → resolution cycle_ms plus PC jitter, one timestamp per read."""
        try:
            with open(self._path_for(rid), encoding="utf-8") as fh:
                head = json.loads(fh.readline())
        except Exception as exc:  # noqa: BLE001
            return {"trust": "unknown", "why": f"header unreadable: {exc}"}
        mode, cyc = head.get("mode"), float(head.get("cycle_ms") or 0)
        names = [x.get("name", "") for x in head.get("symbols", [])]

        def sid_of(sym: str) -> int | None:
            low = sym.lower()
            for i, n in enumerate(names):
                nl = n.lower()
                if nl == low or low.startswith(nl + "[") or low.startswith(nl + "."):
                    return i
            return None
        block_of: dict[str, Any] = {}
        for sym in symbols:
            sid = sid_of(sym)
            block_of[sym] = next((b.get("id") for b in head.get("blocks", []) if sid is not None
                                  and any(sl[0] == sid for sl in b.get("slots", []))), None)
        same_block = len({v for v in block_of.values()}) == 1 and None not in block_of.values()
        if mode == "cycle":
            trust, why = "exact", f"cycle mode: PLC cycle timestamps, resolution {cyc:g} ms"
        elif mode == "change":
            if same_block:
                trust, why = "quantised", f"change mode, one notification: order exact, timestamps on a {cyc:g} ms grid"
            else:
                trust, why = ("unreliable", f"change mode, separate notifications checked every {cyc:g} ms at different "
                              f"phases: timestamps up to {cyc:g} ms off, events between them can be reordered")
        else:
            trust, why = "quantised", f"{mode} mode: PC-side reads every {cyc:g} ms"
        return {"mode": mode, "cycle_ms": cyc, "blocks": block_of, "same_block": same_block, "trust": trust,
                "resolution_ms": cyc, "why": why}

    def latency(self, rid: str, from_symbol: str, from_op: str, from_value: Any, to_symbol: str, to_op: str,
                to_value: Any, max_pairs: int = 500, t_to: float | None = None) -> dict[str, Any]:
        """Time from each moment ``from_symbol <from_op> from_value`` becomes true to the next
        moment ``to_symbol <to_op> to_value`` becomes true (edge-to-edge, PLC timestamps).

        A symbol's FIRST sample is its initial state, not an edge (D34): a condition that already holds there
        became true before the recording started, at an unknown time — it opens no pair and closes none."""
        ev = self._events_for(rid, [from_symbol, to_symbol])
        if t_to is not None:
            ev = [e for e in ev if e["t_ms"] <= t_to]
        f_prev = t_prev = False
        f_seen = t_seen = False
        from_at_start = to_at_start = False
        pending: list[float] = []
        absorbed = 0
        pairs: list[dict[str, Any]] = []
        for e in ev:
            v = _raw_val(e["value"])
            if e["symbol"].lower() == from_symbol.lower():
                now = _OPS.get(from_op, lambda a, b: False)(v, _coerce(from_value, v))
                if not f_seen:
                    f_seen, from_at_start = True, bool(now)
                elif now and not f_prev:
                    pending.append(e["t_ms"])
                f_prev = now
            if e["symbol"].lower() == to_symbol.lower():
                now = _OPS.get(to_op, lambda a, b: False)(v, _coerce(to_value, v))
                if not t_seen:
                    t_seen, to_at_start = True, bool(now)
                    t_prev = now
                    continue
                if now and not t_prev and pending:
                    t_from = pending.pop(0)
                    pairs.append({"t_from_ms": round(t_from, 3), "t_to_ms": round(e["t_ms"], 3), "latency_ms": round(e["t_ms"] - t_from, 3)})
                    absorbed += len(pending)        # further from-edges before this to-edge: no response of their own
                    pending.clear()
                t_prev = now
            if len(pairs) >= max_pairs:
                break
        lat = [p["latency_ms"] for p in pairs]
        out = {"id": rid, "from": f"{from_symbol} {from_op} {from_value}", "to": f"{to_symbol} {to_op} {to_value}",
               "pairs": len(pairs), "unmatched_from_edges": len(pending) + absorbed,
               "from_edges_total": len(pairs) + len(pending) + absorbed, "absorbed_from_edges": absorbed,
               "note": "a from-edge that occurs again before the to-condition fires is 'absorbed' — one response for several stimuli",
               "latency_ms": _stat(lat), "samples": pairs[:100],
               "from_true_at_start": from_at_start, "to_true_at_start": to_at_start,
               "timing": self.timing_of(rid, [from_symbol, to_symbol])}
        if from_at_start:
            out["start_note"] = ("the from-condition already held at the recording's first sample: that edge happened before "
                                 "the recording started and is not measured")
        return out

    def compare(self, rid_a: str, rid_b: str, symbols: list[str] | None = None) -> dict[str, Any]:
        """Before/after comparison of two recordings over their common symbols: stats deltas
        (count, min, max, mean, changes) and, for BOOL symbols, pulse width/period deltas."""
        # N107: the statistics rec_stats gives — the same for both engines (a native recording has no live ones; the
        # compare read `.stats` of whatever object it was and crashed on it)
        running = [r for r in (rid_a, rid_b) if getattr(self._rec_or_none(r), "active", False)]
        sa, src_a = self._final_stats(rid_a)
        sb, src_b = self._final_stats(rid_b)
        common = sorted(set(sa) & set(sb), key=str.lower)
        if symbols:
            want = {s.lower() for s in symbols}
            common = [c for c in common if c.lower() in want]
        out = []
        for n in common:
            a, b = sa[n].to_dict(), sb[n].to_dict()
            row: dict[str, Any] = {"symbol": n, "a": a, "b": b, "delta": {}}
            for k in ("count", "changes", "min", "max", "mean", "rising_edges", "falling_edges"):
                if isinstance(a.get(k), (int, float)) and isinstance(b.get(k), (int, float)):
                    row["delta"][k] = round(b[k] - a[k], 6)
            if isinstance(a.get("last"), bool) and isinstance(b.get("last"), bool):
                try:
                    pa, pb = self.pulses(rid_a, n), self.pulses(rid_b, n)
                    row["pulses"] = {"a": {k: pa.get(k) for k in ("pulses", "high_ms", "period_ms", "duty_percent")},
                                     "b": {k: pb.get(k) for k in ("pulses", "high_ms", "period_ms", "duty_percent")}}
                except RecorderError:
                    pass
            out.append(row)
        res: dict[str, Any] = {"a": rid_a, "b": rid_b, "source": {"a": src_a, "b": src_b},
                               "common_symbols": len(common), "only_in_a": sorted(set(sa) - set(sb))[:50],
                               "only_in_b": sorted(set(sb) - set(sa))[:50], "rows": out}
        if running:
            res["note"] = (f"{', '.join(running)} still recording: compared as recorded so far (the file up to now) — "
                           "compare again after rec_stop for the whole recording")
        return res

    @staticmethod
    def _task_counter_series(path: Path, header: dict[str, Any]) -> tuple[str, list[tuple[float, int]], list[float]] | None:
        """The task's own cycle counter recorded with the data (cycle mode, cycle_counter auto) as (name, [(t, v)],
        [t] — the times once, for _ref_delta)."""
        name = header.get("cycle_counter")
        if not name or not _TASK_COUNTER.search(name):
            return None
        series = [(t, int(rv)) for t, _n, v in iter_events(path, {name.lower()})
                  if isinstance(rv := _raw_val(v), (int, float)) and not isinstance(rv, bool)]
        return (name, series, [t for t, _v in series]) if len(series) >= 2 else None

    def _path_for(self, rid: str) -> Path:
        rec = self.recs.get(rid)
        if rec:
            return rec.path
        p = self.root / f"{rid}.jsonl"
        if not p.is_file():
            cands = list(self.root.glob(f"{rid}*.jsonl")) if self.root.is_dir() else []
            if len(cands) == 1:
                return cands[0]
            raise RecorderError(f"no recording file for {rid!r} under {self.root}")
        return p


_TASK_COUNTER = re.compile(r"_TaskInfo\[\d+\]\.CycleCount$", re.I)


def _ref_delta(series: list[tuple[float, int]], ts: list[float], ta: float, tb: float) -> int | None:
    """How a reference counter changed from the last sample at or before ta to the first at or after tb (None: no
    sample there). ts: the series' times (built once by the caller: one call per duplicate, O(log n) each)."""
    import bisect
    i = bisect.bisect_right(ts, ta) - 1
    j = bisect.bisect_left(ts, tb)
    if i < 0 or j >= len(series):
        return None
    return series[j][1] - series[i][1]


def _raw_val(v: Any) -> Any:
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in v:
                return v[k]
    return v


def _coerce(expected: Any, like: Any) -> Any:
    if isinstance(like, bool) and isinstance(expected, str):
        return expected.strip().lower() in ("1", "true", "on")
    if isinstance(like, (int, float)) and isinstance(expected, str):
        try:
            return type(like)(float(expected)) if isinstance(like, float) else int(float(expected))
        except ValueError:
            return expected
    return expected


def _stat(vals: list[float]) -> dict[str, Any] | None:
    if not vals:
        return None
    s = sorted(vals)
    return {"count": len(s), "min": round(s[0], 3), "max": round(s[-1], 3), "mean": round(sum(s) / len(s), 3),
            "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 3)}


def _flat(v: Any) -> Any:
    if isinstance(v, dict):
        for k in ("raw_ms", "raw", "raw_ns"):
            if k in v:
                return v[k]
        return json.dumps(v)
    if isinstance(v, list):
        return json.dumps(v)
    return v


def _element_names(name: str, dims: list[tuple[int, int]]) -> list[str]:
    import itertools
    ranges = [range(lo, hi + 1) for lo, hi in dims]
    return [f"{name}[{','.join(str(i) for i in idx)}]" for idx in itertools.product(*ranges)]


def _flatten_values(v: Any) -> list[Any]:
    out: list[Any] = []
    for x in v:
        if isinstance(x, list):
            out.extend(_flatten_values(x))
        else:
            out.append(x)
    return out


_SWITCH_LOCK = threading.Lock()
_SWITCH_USERS = [0, None]


def _fast_switch(delta: int) -> None:
    import sys
    with _SWITCH_LOCK:
        if _SWITCH_USERS[0] == 0 and delta > 0:
            _SWITCH_USERS[1] = sys.getswitchinterval()
            sys.setswitchinterval(0.0005)
        _SWITCH_USERS[0] = max(0, _SWITCH_USERS[0] + delta)
        if _SWITCH_USERS[0] == 0 and _SWITCH_USERS[1] is not None:
            sys.setswitchinterval(_SWITCH_USERS[1])
            _SWITCH_USERS[1] = None


class _ProgramMoved(Exception):
    """R43: the PLC program changed after the variables were looked up (Recorder.start loads the table again once)."""


def _program_identity(conn: Any) -> str | None:
    """R43: "t<compile time>:c<online changes>:u<symbols>,<symbol bytes>,<datatypes>,<datatype bytes>" of the PLC
    application (the native engine's identity); None when any part is not readable."""
    try:
        ts = int(conn.read_by_name("TwinCAT_SystemInfoVarList._AppInfo.AppTimestamp", pyads.PLCTYPE_UDINT))
        oc = int(conn.read_by_name("TwinCAT_SystemInfoVarList._AppInfo.OnlineChangeCnt", pyads.PLCTYPE_UDINT))
        info = bytes(conn.read(C.ADSIGRP_SYM_UPLOADINFO2, 0, ctypes.c_ubyte * 64, return_ctypes=True,
                               check_length=False))
        u = struct.unpack_from("<IIII", info, 0)
    except Exception:  # noqa: BLE001
        return None
    return f"t{ts}:c{oc}:u{u[0]},{u[1]},{u[2]},{u[3]}"


class _NativeFallback(Exception):
    pass


def _task_cycle_ms(counter: str, manifest: dict[str, Any], cycle_ms: float) -> float:
    """Cycle time of the task that increments the recorded counter: _TaskInfo[n] → task n from the manifest; an own
    counter → the slowest task not above the notification cycle (else the notification cycle itself)."""
    tasks = manifest.get("tasks") if isinstance(manifest, dict) and isinstance(manifest.get("tasks"), list) else []
    m = re.search(r"_TaskInfo\[(\d+)\]", counter)
    if m:
        for t in tasks:
            if t.get("index") == int(m.group(1)) and isinstance(t.get("cycle_time_ms"), (int, float)) and t["cycle_time_ms"] > 0:
                return float(t["cycle_time_ms"])
    fit = [float(t["cycle_time_ms"]) for t in tasks
           if isinstance(t.get("cycle_time_ms"), (int, float)) and 0 < t["cycle_time_ms"] <= cycle_ms + 1e-9]
    return max(fit) if fit else float(cycle_ms)


def _pkg_version() -> str:
    try:
        from . import __version__
        return __version__
    except Exception:  # noqa: BLE001
        return "?"


def _read_header(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.loads(fh.readline())
            return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_t0(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            for i, ln in enumerate(fh):
                if ln.startswith('{"_t0"'):
                    return json.loads(ln)["_t0"]
                if i > 5000:
                    break
    except (OSError, json.JSONDecodeError, KeyError):
        return None
    return None


def _iter_raw_rows(path: Path) -> Iterator[list[Any]]:
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            if ln.startswith("["):
                try:
                    yield json.loads(ln)
                except json.JSONDecodeError:
                    continue
