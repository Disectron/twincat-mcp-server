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
from typing import Any, Callable, Iterator

import pyads
from pyads import constants as C

from . import types_tc as T
from .ads_backend import AdsBackend, AdsError, SymbolEntry, _fmt_exc

FILETIME_EPOCH_OFFSET_100NS = 116444736000000000  # 1601 → 1970
MERGE_GAP_BYTES = 256          # leaves closer than this share one notification
MAX_BLOCK_BYTES = 16384        # one notification never carries more than this
QUEUE_BYTES = 256 << 20        # in-memory backlog cap per recording
DRAIN_TIMEOUT_S = 60.0         # a stop writes the backlog for at most this long
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
    def parse(cls, text: str, types: dict[str, T.TcType], edge: bool = False) -> "Condition":
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
        raw = m.group("val").strip().strip("'\"")
        try:
            val = T.encode(tt, raw) if tt is not None else raw
        except Exception:  # noqa: BLE001
            val = raw
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
            v = value["raw_ms"] if isinstance(value, dict) and "raw_ms" in value else value
            v = v["raw"] if isinstance(v, dict) and "raw" in v else v
            v = v["raw_ns"] if isinstance(v, dict) and "raw_ns" in v else v
            return _OPS[self.op](v, self.value)
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
            return chunk.split(b"\x00", 1)[0].decode("latin-1", "replace")
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

    def missing(self, end_t: float | None = None, start_t: float | None = None) -> int:
        """Holes between the first and last sample, plus — when the window end is known (duration / post-trigger
        stop, or the other blocks' last sample) — the samples this block should have delivered after its last one
        (field report N11: a recording that went quiet halfway must not look complete). One period of slack."""
        if self.samples < 2 or not self.min_step or self.first_t is None or self.last_t is None:
            return 0
        expected = round((self.last_t - self.first_t) / self.min_step) + 1
        inner = max(0, expected - self.samples)
        return inner + self.tail_missing(end_t) + self.head_missing(start_t)

    def head_missing(self, start_t: float | None) -> int:
        """Samples the window needed before this block's first one (a pre-trigger buffer that was not full yet)."""
        if start_t is None or not self.min_step or self.first_t is None or self.first_t <= start_t:
            return 0
        return int((self.first_t - start_t + 1e-6) / self.min_step)

    def tail_missing(self, end_t: float | None) -> int:
        if end_t is None or not self.min_step or self.last_t is None or end_t <= self.last_t:
            return 0
        return max(0, int((end_t - self.last_t) / self.min_step) - 1)


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
        self.types: dict[str, T.TcType] = {s.name: T.parse_type(s.type, s.size) for s in symbols}
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
        self.q: "queue.Queue[Any]" = queue.Queue(maxsize=200_000)
        self.events = 0             # leaf values represented in the file
        self.samples = 0            # rows written (block samples / poll cycles)
        self.stats_samples = 0      # rows whose leaves went into the live statistics
        self.dropped = 0            # samples lost because the backlog was full
        self.dropped_on_stop = 0    # samples still queued when the stop drain timed out
        self.after_limit = 0        # samples that arrived after a limit ended the recording (not part of it)
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
        self._cut_now = False                 # discard the backlog (limit reached at a known sample)
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

    def header_dict(self, **extra: Any) -> dict[str, Any]:
        return {"_header": True, "format": 2, "id": self.id, "name": self.name, "mode": self.mode,
                "cycle_ms": self.cycle_ms, "time_source": self.time_source,
                "symbols": [{"id": self.sids[s.name], "name": s.name, "type": s.type, "size": s.size,
                             "igrp": s.index_group, "ioffs": s.index_offset} for s in self.symbols],
                "blocks": [b.header() for b in self.blocks],
                "created": dt.datetime.fromtimestamp(self.created, dt.timezone.utc).isoformat(), **self.meta, **extra}

    def start_ingest(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "w", encoding="utf-8", newline="\n", buffering=1 << 16)
        self._write(json.dumps(self.header_dict(), default=str) + "\n")
        self._thread = threading.Thread(target=self._ingest_loop, name=f"rec-{self.id}", daemon=True)
        self._thread.start()

    def enqueue(self, ts: int | float, key: Any, payload: Any) -> None:
        """ADS callback thread (block notification: key = block id, payload = raw bytes) or poll thread
        (key = None, payload = {name: value}). Only enqueues — the callback must return at once."""
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
        released = False
        deadline = None
        while True:
            stopping = self._stop.is_set()
            if stopping and not released:
                released = True
                self._release()              # no new samples; what is queued was taken before the stop
                deadline = time.monotonic() + DRAIN_TIMEOUT_S
            if stopping and self._cut_now:
                self.after_limit += self.q.qsize()
                break
            try:
                item = self.q.get(timeout=0 if stopping else 0.25)
            except queue.Empty:
                if stopping:
                    break
                self._check_limits()
                continue
            if item is None:
                continue
            if stopping and deadline is not None and time.monotonic() > deadline:
                self.dropped_on_stop += 1 + self.q.qsize()
                break
            backlog = self.q.qsize()
            if backlog > self.max_backlog:
                self.max_backlog = backlog
            light = backlog > 2_000          # under load: raw samples first, statistics later (from the file)
            try:
                self._handle(item, light)
            except Exception as exc:  # noqa: BLE001 — a bad sample must not end the recording silently
                if len(self.errors) < 20:
                    self.errors.append(f"ingest: {type(exc).__name__}: {exc}")
            if not stopping:
                self._check_limits()
            if self._fh and time.monotonic() - self._last_flush > 1.0:
                self._write_chain()
                self._fh.flush()
                self._last_flush = time.monotonic()
        self._finish()

    def _handle(self, item: Any, light: bool) -> None:
        ts, key, payload = item
        t = self._rel_ms(ts)
        if self._cut_t is not None and t > self._cut_t:
            self.after_limit += 1
            return
        if self.t0_mono is None:
            self.t0_mono = time.monotonic()
        if key is None:
            self._handle_poll(t, payload, light)
        else:
            self._handle_block(t, self.blocks[key], payload, light)

    def _handle_block(self, t: float, b: Block, data: bytes, light: bool) -> None:
        if self.armed:
            self.pre.append((t, b, data))
            while self.pre and t - self.pre[0][0] > self.pre_trigger_ms:
                self.pre.popleft()
            sw = self.start_when
            if sw is not None and sw.symbol in self.slot_of and self.slot_of[sw.symbol][0] is b:
                if sw.fires(self.slot_of[sw.symbol][1].decode(data)):
                    self.armed = False
                    self.triggered_at = t
                    pre, self.pre = list(self.pre), deque()
                    for pt, pb, pd in pre:
                        self._commit_block(pt, pb, pd, light)
            return
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
        self._write(f'[{t:.4f},"#{b.bid}","{base64.b64encode(data).decode("ascii")}"]\n')
        self.samples += 1
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
        if ew is not None and ew.symbol in self.slot_of and self.slot_of[ew.symbol][0] is b:
            if ew.fires(self.slot_of[ew.symbol][1].decode(data)):
                self.stop_reason = f"stop_when {ew.symbol} {ew.op} {ew.value}"
                self._cut_now = True
                self._stop.set()
        if self.max_events and self.events >= self.max_events and not self._stop.is_set():
            self.stop_reason = f"max_events {self.max_events}"
            self._cut_now = True
            self._stop.set()

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
        self.samples += 1
        ew = self.stop_when
        with self._lock:
            for name, raw in row.items():
                dec = self._dec(name, raw)
                self._write(json.dumps([round(t, 4), self.sids.get(name, name), dec], default=str) + "\n")
                self.events += 1
                if not light:
                    self._ingest_value(t, name, dec)
                if ew is not None and name == ew.symbol and ew.fires(dec):
                    self.stop_reason = f"stop_when {ew.symbol} {ew.op} {ew.value}"
                    self._cut_now = True
                    self._stop.set()
        if not light:
            self.stats_samples += 1
        if self.max_events and self.events >= self.max_events and not self._stop.is_set():
            self.stop_reason = f"max_events {self.max_events}"
            self._cut_now = True
            self._stop.set()

    def _ingest_value(self, t: float, name: str, dec: Any) -> None:
        """Caller holds self._lock."""
        self.last_values[name] = dec
        self.ring.append((t, name, dec))
        self.stats[name].ingest(t, dec, _numeric_dec(dec), self.max_runs)

    def _check_limits(self) -> None:
        if self._stop.is_set():
            return
        mono = (time.monotonic() - self.t0_mono) if self.t0_mono is not None else None
        if self.post_trigger_s and self.triggered_at is not None and (self.last_t - self.triggered_at) / 1000.0 >= self.post_trigger_s:
            self.stop_reason = f"post_trigger {self.post_trigger_s}s after the start_when trigger"
            self._cut_t = self.triggered_at + self.post_trigger_s * 1000.0
            self._stop.set()
        elif self.duration_s and self.last_t / 1000.0 >= self.duration_s:
            self.stop_reason = f"duration {self.duration_s}s"
            self._cut_t = self.duration_s * 1000.0
            self._stop.set()
        elif self.duration_s and mono is not None and mono >= self.duration_s:
            # this PC's clock says the time is up; samples taken by the PLC within the duration may still be
            # queued (ingest behind): they are drained and written, later ones are not part of the recording
            self.stop_reason = f"duration {self.duration_s}s"
            self._cut_t = self.duration_s * 1000.0
            self._stop.set()
        elif self.duration_s and self.t0_mono is None and (time.time() - self.created) >= self.duration_s + 5:
            self.stop_reason = "duration elapsed with no data (symbols never updated?)"
            self._stop.set()

    def request_stop(self, reason: str = "requested") -> None:
        if not self._stop.is_set():
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
        self.stopped_mono = time.monotonic()
        self.stopped_at = time.time()
        with self._lock:
            for st in self.stats.values():
                if st.runs and st.last_t is not None:
                    st.runs[-1][1] = st.last_t
            if self._fh:
                miss = self.missing_samples()
                if self.mode == "cycle" and self.t0_wall is not None:
                    ws, we = self.window_start(), self.window_end()
                    for b in self.blocks:
                        h = b.head_missing(ws)
                        if h > 0 and ws is not None and b.first_t is not None:
                            self._write_gap(b, "head", ws, b.first_t, h)
                        tm = b.tail_missing(we)
                        if tm > 0 and we is not None:
                            self._write_gap(b, "tail", b.last_t, we, tm)
                digest = self._sha.hexdigest()
                footer = {"_footer": True, "events": self.events, "samples": self.samples,
                          "missing_samples": miss, "label": self.label(miss), "gap_markers": self.gap_markers,
                          "chain_checkpoints": self.chains, "dropped": self.dropped,
                          "dropped_on_stop": self.dropped_on_stop, "after_limit": self.after_limit,
                          "unsubscribed": self.unsubscribed, "stop_reason": self.stop_reason,
                          "window_end_ms": self._cut_t, "window_start_ms": self.window_start(), "engine": "python",
                          "stopped": dt.datetime.now(dt.timezone.utc).isoformat(), "lines": self._lines,
                          "sha256": digest}
                if self.key:
                    footer["hmac"] = _hmac.new(self.key, digest.encode("ascii"), hashlib.sha256).hexdigest()
                self._fh.write(json.dumps(footer, default=str) + "\n")
                self._fh.close()
                self._fh = None
                sealed = True
            else:
                sealed = False
        if sealed:
            self.manifest = seal_recording(self.path, self.key)

    def label(self, miss: int | None = None) -> str:
        """R5: COMPLETE only when the PLC timeline proves every sample is in the file (cycle mode); INCOMPLETE when
        anything is missing or was lost; NOT_PROVABLE for change/poll recordings (silence can't be told from loss)."""
        if self.mode != "cycle":
            return "NOT_PROVABLE"
        miss = self.missing_samples() if miss is None else miss
        ok = (self.samples > 0 and not miss and not self.dropped and not self.dropped_on_stop and not self.unsubscribed
              and (self.start_when is None or self.triggered_at is not None))
        return "COMPLETE" if ok else "INCOMPLETE"

    @property
    def active(self) -> bool:
        return self.stopped_at is None

    # ------------------------------------------------------------ views
    def window_start(self) -> float | None:
        """Start of the window that must be complete: trigger − pre_trigger_ms for triggered recordings (samples the
        pre-trigger buffer could not hold count as missing_head); None otherwise (each block from its first sample)."""
        if self.triggered_at is None or not self.pre_trigger_ms:
            return None
        return self.triggered_at - self.pre_trigger_ms

    def window_end(self) -> float | None:
        """End of the recorded window in PLC-relative ms: the duration / post-trigger cut, else the latest sample of
        any block (a block that stopped delivering before the others then shows its tail as missing)."""
        if self._cut_t is not None and not self.active:
            return self._cut_t
        lasts = [b.last_t for b in self.blocks if b.last_t is not None]
        return max(lasts) if lasts else None

    def missing_samples(self) -> int | None:
        if self.mode != "cycle":
            return None
        end, start = self.window_end(), self.window_start()
        return sum(b.missing(end, start) for b in self.blocks)

    def completeness(self) -> float | None:
        if self.mode != "cycle":
            return None
        got = sum(b.samples for b in self.blocks)
        miss = self.missing_samples() or 0
        return round(got / (got + miss), 6) if got + miss else None

    def block_report(self, limit: int = 20) -> list[dict[str, Any]]:
        out = []
        for b in self.blocks[:limit]:
            d: dict[str, Any] = {"id": b.bid, "bytes": b.size, "symbols": len(b.slots), "first": b.slots[0].name if b.slots else None,
                                 "samples": b.samples}
            if self.mode == "cycle":
                d.update({"missing_samples": b.missing(self.window_end(), self.window_start()),
                          "missing_head": b.head_missing(self.window_start()), "missing_tail": b.tail_missing(self.window_end()),
                          "period_ms": None if b.min_step is None else round(b.min_step, 4),
                          "duplicate_stamps": b.dup_stamps, "first_gaps": b.gaps[:5]})
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
            st: dict[str, Any] = {
                "id": self.id, "name": self.name, "active": self.active, "mode": self.mode, "cycle_ms": self.cycle_ms,
                "time_source": self.time_source, "symbols": len(self.symbols), "notifications": len(self.blocks) or None,
                "events": self.events, "samples": self.samples,
                "missing_samples": self.missing_samples(), "completeness": self.completeness(),
                "label": self.label() + ("" if not self.active else " (so far)"),
                "gap_markers": self.gap_markers,
                "dropped": self.dropped, "dropped_on_stop": self.dropped_on_stop, "backlog": backlog,
                "max_backlog": self.max_backlog,
                "stats_coverage": (round(self.stats_samples / self.samples, 4) if self.samples else 1.0) if self.live_stats
                                  else "deferred (high rate: computed from the file by rec_stats)",
                "events_per_s": round(self.events / elapsed, 1) if elapsed > 0 else 0.0,
                "elapsed_s": round(elapsed, 2), "plc_span_s": round(self.last_t / 1000.0, 4),
                "armed": self.armed,
                "triggered_at_ms": None if self.triggered_at is None else round(self.triggered_at, 3),
                "stop_reason": self.stop_reason, "file": str(self.path),
                "file_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "last_values": self._last_values_view(), "errors": self.errors[-5:],
            }
        if self.unsubscribed:
            st["unsubscribed_symbols"] = self.unsubscribed[:50]
        if self.blocks:
            st["blocks"] = self.block_report()
        warn = []
        if self.dropped or self.dropped_on_stop:
            warn.append(f"{self.dropped + self.dropped_on_stop} samples LOST on this PC (backlog full / stop timeout)")
        if st["missing_samples"]:
            warn.append(f"{st['missing_samples']} samples missing in the PLC timeline (gaps between timestamps)")
        if self.active and backlog > self.q.maxsize // 2:
            warn.append(f"ingest cannot keep up: backlog {backlog}/{self.q.maxsize} — fewer symbols, a larger cycle_ms or mode='change'")
        if self.unsubscribed:
            warn.append(f"{len(self.unsubscribed)} symbols could not be subscribed")
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


def _manifest_sig(key: bytes, m: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in m.items() if k != "hmac"}, sort_keys=True, separators=(",", ":"), default=str)
    return _hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def file_sha256(path: Path) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


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
        structs) are expanded through the datatype table — for explicit symbols, prefixes AND patterns
        (a pattern matching MAIN.fbFill records every leaf beneath it, the same set rec_scan lists)."""
        lst, idx = self.ads.symbols_cached(False, port)
        chosen: dict[str, SymbolEntry] = {}

        def add_leaves(root: SymbolEntry, leaves) -> None:
            for leaf in leaves:
                if leaf.size <= 0 or leaf.note in ("reference/pointer",) or leaf.note and "not expanded" in leaf.note:
                    continue
                tt = T.parse_type(leaf.type, leaf.size)
                if tt.ctype is None or tt.opaque:
                    continue
                chosen[leaf.path] = SymbolEntry(leaf.path, leaf.type, leaf.size, root.index_group,
                                                root.index_offset + leaf.offset, 0, "")

        def add(s: SymbolEntry) -> None:
            tt = T.parse_type(s.type, s.size)
            if tt.ctype is None or tt.opaque:
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
            import fnmatch
            pat = pattern.lower()
            for s in lst:
                if fnmatch.fnmatchcase(s.name.lower(), pat):
                    add(s)
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
            m["symbol_version"] = self.ads._symbol_version(self.ads.conn(port))
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
    def start(self, *, symbols: list[str] | None, prefix: str | None, pattern: str | None, mode: str,
              cycle_ms: float, duration_s: float | None, max_events: int | None, start_when: str | None,
              stop_when: str | None, pre_trigger_ms: float, name: str | None, max_symbols: int,
              port: int | None = None, post_trigger_s: float | None = None, allow_partial: bool = False,
              max_delay_ms: float = 0.0, cycle_counter: str | None = "auto", edge_triggers: bool = False,
              engine: str | None = None) -> dict[str, Any]:
        if mode not in ("cycle", "change", "poll"):
            raise RecorderError("mode must be cycle | change | poll")
        want = (engine or (getattr(self.rcfg, "engine", "auto") if self.rcfg is not None else "auto")).lower()
        if want not in ("auto", "native", "python"):
            raise RecorderError("engine must be auto | native | python")
        if mode not in ("cycle", "change", "poll"):
            raise RecorderError("mode must be cycle | change | poll")
        cycle_ms = max(float(cycle_ms), 0.1)
        syms = self.expand_symbols(symbols, prefix, pattern, max_symbols, port)
        manifest = self._manifest(port)
        counter = None
        if mode == "cycle" and cycle_counter:
            counter = self._pick_counter(cycle_counter, manifest, cycle_ms, port)
            if counter is not None and all(s.name.lower() != counter.name.lower() for s in syms):
                syms = syms + [counter]
        rid = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
        path = self.root / f"{rid}.jsonl"
        types = {s.name: T.parse_type(s.type, s.size) for s in syms}
        sw = Condition.parse(start_when, types, edge_triggers) if start_when else None
        ew = Condition.parse(stop_when, types, edge_triggers) if stop_when else None
        if post_trigger_s and not sw:
            raise RecorderError("post_trigger_s needs start_when (it counts from the trigger)")
        meta = {"ams_net_id": self.ads.net_id, "port": port or self.ads.port, "start_when": start_when,
                "stop_when": stop_when, "pre_trigger_ms": pre_trigger_ms, "post_trigger_s": post_trigger_s,
                "selection": {"symbols": symbols, "prefix": prefix, "pattern": pattern},
                "cycle_counter": counter.name if counter is not None else None, "max_delay_ms": max_delay_ms,
                "manifest": manifest, "recorder": f"twincat-mcp {_pkg_version()}"}
        rec = Recording(rid, name or rid, syms, mode, cycle_ms, path, self.ring_events, self.max_runs,
                        duration_s, max_events, sw, ew, pre_trigger_ms, meta, post_trigger_s, self.key)
        if mode != "poll":
            rec.set_blocks(build_blocks(syms, rec.sids, rec.types))
        chosen, engine_note = self._choose_engine(want, mode, allow_partial)
        if chosen == "native":
            try:
                return self._start_native(rec, port, cycle_ms, max_delay_ms, duration_s, post_trigger_s, pre_trigger_ms,
                                          max_events, syms, counter)
            except _NativeFallback as exc:
                if want == "native":
                    raise RecorderError(f"engine 'native' requested but not usable: {exc}") from exc
                engine_note = f"native engine not used: {exc}"
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
        st["values_per_s"] = round(len(syms) * 1000.0 / cycle_ms, 1) if mode == "cycle" else None
        if mode == "cycle" and rate > 20_000 and st.get("engine") != "native":
            st["warning"] = (f"{notifs} notifications every {cycle_ms} ms: above what this PC was measured to ingest. "
                             "Watch missing_samples / backlog in rec_status, or select fewer, contiguous variables.")
        return st

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
        names = [s.name for s in rec.symbols]
        types = rec.types
        interval = rec.cycle_ms / 1000.0

        def loop() -> None:
            use_sum = True
            while not rec._stop.is_set():
                t_cycle = time.perf_counter()
                ts = time.time()
                try:
                    if use_sum:
                        row = conn.read_list_by_name(names)
                        if any(isinstance(v, str) and v.startswith("error") for v in row.values()) and len(names) == 1:
                            raise RuntimeError("sum read error")
                    else:
                        row = {n: conn.read_by_name(n, types[n].ctype) for n in names}
                except Exception as exc:  # noqa: BLE001
                    if use_sum:
                        use_sum = False
                        continue
                    if len(rec.errors) < 20:
                        rec.errors.append(f"poll: {_fmt_exc(exc)}")
                    time.sleep(min(1.0, interval * 5))
                    continue
                rec.enqueue(ts, None, dict(row))
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
            rec.request_stop(reason)          # reason first: a limit check racing the release must not claim the stop
            if rec.mode != "poll":
                self._stop_notifications(rec)
            if rec._poll_thread:
                rec._poll_thread.join(timeout=5)
            if rec._thread:
                rec._thread.join(timeout=DRAIN_TIMEOUT_S + 10)
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
        """Release ADS handles of recordings that ended on their own (limits/stop_when)."""
        for rec in self.recs.values():
            if getattr(rec, "engine", "python") == "native":
                rec.reap()
            elif not rec.active and rec.conn is not None:
                self._stop_notifications(rec)

    # ------------------------------------------------------------ queries
    def _get(self, rid: str) -> Recording:
        rec = self.recs.get(rid)
        if rec is None:
            cands = [r for k, r in self.recs.items() if k.startswith(rid)]
            if len(cands) == 1:
                return cands[0]
            raise RecorderError(f"recording {rid!r} unknown in this session (rec_list shows files on disk)")
        return rec

    def list(self, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        self.reap()
        live = []
        for r in self.recs.values():
            s = r.status()
            live.append({k: s[k] for k in ("id", "name", "active", "mode", "symbols", "events", "samples", "missing_samples",
                                           "dropped", "stop_reason", "file", "file_bytes") if k in s})
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
        return {"session": live, "on_disk": out_files, "on_disk_total": total, "offset": offset, "root": str(self.root)}

    def status(self, rid: str) -> dict[str, Any]:
        self.reap()
        return self._get(rid).status()

    def snapshot(self, rid: str, last_n: int, symbols: list[str] | None) -> dict[str, Any]:
        rec = self._get(rid)
        return {"id": rec.id, "active": rec.active, "events": rec.events, "rows": rec.snapshot(last_n, symbols)}

    def stats(self, rid: str, symbols: list[str] | None, include_runs: bool, max_runs_out: int,
              exact: bool | None = None) -> dict[str, Any]:
        """Live statistics while they are complete; from the file when the ingest had to skip them under load
        (stats_coverage < 1), when the recording is from an earlier session, or with exact=true."""
        rec = self.recs.get(rid)
        if rec is None:
            try:
                rec = self._get(rid)
            except RecorderError:
                rec = None
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
        path = self._path_for(rid)
        want = {s.lower() for s in symbols} if symbols else None
        st: dict[str, SymStats] = {}
        for t, n, v in iter_events(path, want):
            st.setdefault(n, SymStats()).ingest(t, v, _numeric_dec(v), self.max_runs)
        return {"id": rid, "active": bool(rec and rec.active), "source": "file",
                "events": sum(s.count for s in st.values()), "symbols": _stats_out(st, symbols, include_runs, max_runs_out)}

    @staticmethod
    def _elapsed_ms(rec: Recording) -> float | None:
        if rec.t0_mono is None:
            return None
        end = rec.stopped_mono or time.monotonic()
        return round((end - rec.t0_mono) * 1000, 1)

    def read(self, rid: str, t_from_ms: float | None, t_to_ms: float | None, symbols: list[str] | None,
             max_rows: int, wide: bool) -> dict[str, Any]:
        """Stream the file; optionally pivot to wide rows with forward fill."""
        path = self._path_for(rid)
        want = {s.lower() for s in symbols} if symbols else None
        header = _read_header(path)
        cols = [s["name"] for s in header.get("symbols", []) if not want or s["name"].lower() in want]
        rows: list[Any] = []
        cur: dict[str, Any] = {}
        truncated = False
        for t, n, v in iter_events(path, want):
            if t_from_ms is not None and t < t_from_ms:
                if wide:
                    cur[n] = v
                continue
            if t_to_ms is not None and t > t_to_ms:
                break
            if wide:
                cur[n] = v
                rows.append({"t_ms": t, **{c: cur.get(c) for c in cols}})
            else:
                rows.append({"t_ms": t, "symbol": n, "value": v})
            if len(rows) >= max_rows:
                truncated = True
                break
        return {"id": rid, "file": str(path), "name": header.get("name"), "time_source": header.get("time_source", "plc"),
                "rows": rows, "count": len(rows), "truncated": truncated}

    def export(self, rid: str, fmt: str, out_path: str | None, symbols: list[str] | None,
               fill: bool = False) -> dict[str, Any]:
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
        with open(out, "w", newline="", encoding="utf-8") as fo:
            w = csv.writer(fo)
            plc_col = ["plc_filetime"] if t0 is not None else []
            w.writerow(["t_ms", *plc_col, *cols] if fmt == "csv_wide" else ["t_ms", *plc_col, "symbol", "value"])
            cur: dict[str, Any] = {}
            row_t: float | None = None

            def flush() -> None:
                nonlocal rows_written
                if row_t is None:
                    return
                ft = [t0 + int(round(row_t * 1e4))] if t0 is not None else []
                w.writerow([row_t, *ft, *[cur.get(c, "") for c in cols]])
                rows_written += 1

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
                    ft = [t0 + int(round(t * 1e4))] if t0 is not None else []
                    w.writerow([t, *ft, nme, _flat(v)])
                    rows_written += 1
                n += 1
            if fmt == "csv_wide":
                flush()
        footer = read_footer(path)
        out_d: dict[str, Any] = {"id": rid, "file": str(out), "format": fmt, "rows": rows_written, "values": n,
                                 "time_source": header.get("time_source", "plc"),
                                 "note": ("one row per sample time; empty cell = no sample of that variable at that time"
                                          if fmt == "csv_wide" and not fill else
                                          "one row per sample time; empty cells forward-filled from the last sample"
                                          if fmt == "csv_wide" else "one row per value")}
        if t0 is not None:
            out_d["note"] += "; plc_filetime = the PLC's own timestamp of the sample (100 ns since 1601-01-01 UTC)"
        if footer is not None:
            for k in ("events", "samples", "missing_samples", "dropped", "dropped_on_stop", "stop_reason"):
                if k in footer:
                    out_d[k if k != "events" else "events_in_file"] = footer.get(k)
        else:
            out_d["complete"] = False
            out_d["note"] += "; no footer: the recording is still running or the process died before it ended"
        return out_d

    def delete(self, rid: str) -> dict[str, Any]:
        rec = self.recs.get(rid)
        if rec and rec.active:
            self.stop(rid, "deleted")
        path = self._path_for(rid)
        removed = []
        for p in (path, manifest_path(path), path.with_suffix(".wide.csv"), path.with_suffix(".long.csv")):
            if p.exists():
                make_writable(p)                       # sealed recordings are read-only (R9)
                p.unlink()
                removed.append(str(p))
        self.recs.pop(rid, None)
        return {"id": rid, "removed": removed}

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
            for k in ("label", "gap_markers", "chain_checkpoints", "engine", "engine_version", "overflow_samples",
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
            for ev in _iter_raw_rows(path):
                t, k = ev[0], ev[1]
                if isinstance(k, str) and k.startswith("#"):
                    b = blocks.get(int(k[1:]))
                    if b is not None:
                        b.note_stamp(t)
            rep = []
            tot_s = tot_m = 0
            lasts = [b.last_t for b in blocks.values() if b.last_t is not None]
            end = (footer or {}).get("window_end_ms") or (max(lasts) if lasts else None)
            start = (footer or {}).get("window_start_ms")
            for b in blocks.values():
                m = b.missing(end, start)
                tot_s += b.samples
                tot_m += m
                rep.append({"block": b.bid, "first": b.slots[0].name if b.slots else None, "symbols": len(b.slots),
                            "samples": b.samples, "missing": m, "missing_head": b.head_missing(start),
                            "missing_tail": b.tail_missing(end),
                            "period_ms": None if b.min_step is None else round(b.min_step, 4),
                            "duplicate_stamps": b.dup_stamps, "first_gaps": b.gaps[:5]})
            out["timeline"] = {"samples": tot_s, "missing": tot_m,
                               "completeness": round(tot_s / (tot_s + tot_m), 6) if tot_s + tot_m else None,
                               "blocks": rep[:50]}
        elif header.get("mode") != "cycle":
            out["timeline"] = {"note": f"{header.get('mode')} mode samples only on change (or on this PC's clock for poll): "
                                       "gaps cannot be told from quiet periods — record in cycle mode to prove completeness"}
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
                for (ta, va), (tb, vb), d in zip(vals, vals[1:], diffs):
                    if d == 0:
                        dups += 1
                    elif d < 0:
                        back += 1
                    elif d != step:
                        miss = d // step - 1 if d % step == 0 else None
                        missing += miss or 0
                        if len(gaps) < max_gaps:
                            gaps.append({"t_ms": tb, "from": va, "to": vb, "missing_samples": miss})
                span = (vals[-1][1] - vals[0][1]) // step + 1 if step else None
                out["counter"] = {"symbol": counter, "samples": len(vals), "step": step, "expected_samples": span,
                                  "missing_samples": missing, "duplicates": dups, "backwards": back,
                                  "completeness": round(len(vals) / span, 6) if span else None,
                                  "lossless": missing == 0 and dups == 0 and back == 0, "first_gaps": gaps}
        ok = out.get("integrity", "").startswith("intact")
        tl = out.get("timeline") or {}
        cn = out.get("counter") or {}
        ft = footer or {}
        out["verdict"] = ("PASS" if ok and not tl.get("missing") and cn.get("lossless", True) and not ft.get("dropped")
                          and not ft.get("dropped_on_stop") and not ft.get("overflow_samples")
                          and ft.get("verdict_hint", "PASS") == "PASS" else "FAIL")
        return out

    # ------------------------------------------------------------ analysis
    def _events_for(self, rid: str, symbols: list[str]) -> list[dict[str, Any]]:
        return self.read(rid, None, None, symbols, 10_000_000, False)["rows"]

    def pulses(self, rid: str, symbol: str, max_list: int = 200) -> dict[str, Any]:
        """Pulse analysis of one symbol: for BOOL the high/low widths, period, duty;
        for any other type the value runs with dwell times (state machine timing)."""
        ev = self._events_for(rid, [symbol])
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
            highs = [(a, b) for a, b, v in runs[:-1] if v]        # last run has no known end
            lows = [(a, b) for a, b, v in runs[:-1] if not v]
            hw = [b - a for a, b in highs]
            lw = [b - a for a, b in lows]
            starts = [a for a, _ in highs]
            periods = [b - a for a, b in zip(starts, starts[1:])]
            out.update({
                "pulses": len(highs),
                "high_ms": _stat(hw), "low_ms": _stat(lw), "period_ms": _stat(periods),
                "frequency_hz": round(1000.0 / (sum(periods) / len(periods)), 3) if periods else None,
                "duty_percent": round(100 * sum(hw) / span, 2) if span > 0 and hw else None,
                "first_pulses": [{"t_on_ms": round(a, 3), "t_off_ms": round(b, 3), "width_ms": round(b - a, 3)} for a, b in highs[:max_list]],
                "note": "first/last runs are open-ended and excluded from width statistics",
            })
        else:
            dw = [b - a for a, b, _ in runs[:-1]]
            out.update({"dwell_ms": _stat(dw),
                        "timeline": [{"t_start_ms": round(a, 3), "t_end_ms": round(b, 3), "dwell_ms": round(b - a, 3), "value": v}
                                     for a, b, v in runs[:max_list]]})
        return out

    def latency(self, rid: str, from_symbol: str, from_op: str, from_value: Any, to_symbol: str, to_op: str,
                to_value: Any, max_pairs: int = 500) -> dict[str, Any]:
        """Time from each moment ``from_symbol <from_op> from_value`` becomes true to the next
        moment ``to_symbol <to_op> to_value`` becomes true (edge-to-edge, PLC timestamps)."""
        ev = self._events_for(rid, [from_symbol, to_symbol])
        f_prev = t_prev = False
        pending: list[float] = []
        absorbed = 0
        pairs: list[dict[str, Any]] = []
        for e in ev:
            v = _raw_val(e["value"])
            if e["symbol"].lower() == from_symbol.lower():
                now = _OPS.get(from_op, lambda a, b: False)(v, _coerce(from_value, v))
                if now and not f_prev:
                    pending.append(e["t_ms"])
                f_prev = now
            if e["symbol"].lower() == to_symbol.lower():
                now = _OPS.get(to_op, lambda a, b: False)(v, _coerce(to_value, v))
                if now and not t_prev and pending:
                    t_from = pending.pop(0)
                    pairs.append({"t_from_ms": round(t_from, 3), "t_to_ms": round(e["t_ms"], 3), "latency_ms": round(e["t_ms"] - t_from, 3)})
                    absorbed += len(pending)        # further from-edges before this to-edge: no response of their own
                    pending.clear()
                t_prev = now
            if len(pairs) >= max_pairs:
                break
        lat = [p["latency_ms"] for p in pairs]
        return {"id": rid, "from": f"{from_symbol} {from_op} {from_value}", "to": f"{to_symbol} {to_op} {to_value}",
                "pairs": len(pairs), "unmatched_from_edges": len(pending) + absorbed,
                "from_edges_total": len(pairs) + len(pending) + absorbed, "absorbed_from_edges": absorbed,
                "note": "a from-edge that occurs again before the to-condition fires is 'absorbed' — one response for several stimuli",
                "latency_ms": _stat(lat), "samples": pairs[:100]}

    def compare(self, rid_a: str, rid_b: str, symbols: list[str] | None = None) -> dict[str, Any]:
        """Before/after comparison of two recordings over their common symbols: stats deltas
        (count, min, max, mean, changes) and, for BOOL symbols, pulse width/period deltas."""
        def stats_from_file(rid: str) -> tuple[dict[str, SymStats], dict[str, str]]:
            st: dict[str, SymStats] = {}
            types: dict[str, str] = {}
            for t, n, v in iter_events(self._path_for(rid), None):
                v = _raw_val(v)
                st.setdefault(n, SymStats()).ingest(t, v, _numeric_dec(v), 100_000)
            return st, types
        if rid_a in self.recs and not self.recs[rid_a].active:
            sa = self.recs[rid_a].stats
        else:
            sa, _ = stats_from_file(rid_a)
        if rid_b in self.recs and not self.recs[rid_b].active:
            sb = self.recs[rid_b].stats
        else:
            sb, _ = stats_from_file(rid_b)
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
        return {"a": rid_a, "b": rid_b, "common_symbols": len(common), "only_in_a": sorted(set(sa) - set(sb))[:50],
                "only_in_b": sorted(set(sb) - set(sa))[:50], "rows": out}

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


class _NativeFallback(Exception):
    pass


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
