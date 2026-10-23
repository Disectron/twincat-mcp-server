"""Native capture service (tcmcp-capture) — the time-critical part of cycle-mode recordings in C++.

The executable is built on Beckhoff's AdsLib (C++ ``AdsDevice`` / ``AdsNotification``) over Beckhoff's
``TcAdsDll.dll`` on Windows, so samples go TwinCAT router → C++ callback → lock-free ring → file without ever
touching Python. This module only:

* finds the executable and checks it is the same version as this package (``tcmcp-capture --version``);
* turns a resolved recording (symbols → blocks, conditions → raw offsets) into a ``start`` command;
* runs one process per recording and talks JSON lines to it (start / status / stop, ``finished`` event);
* presents the process as a recording object with the same views the Python recorder offers.

The file it writes is recording format 2, byte-compatible with the Python recorder: ``rec_verify``,
``rec_read``, ``rec_export`` and ``rec_stats`` read native and Python recordings alike.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

PROTOCOL = 1
EXE_NAME = "tcmcp-capture.exe" if os.name == "nt" else "tcmcp-capture"
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW

_NATIVE_TYPES = {  # (kind, size) -> service type
    ("bool", 1): "bool",
    ("uint", 1): "u8", ("uint", 2): "u16", ("uint", 4): "u32", ("uint", 8): "u64",
    ("int", 1): "i8", ("int", 2): "i16", ("int", 4): "i32", ("int", 8): "i64",
    ("float", 4): "f32", ("float", 8): "f64",
    ("time", 4): "u32", ("ltime", 8): "u64", ("date", 4): "u32", ("ldate", 8): "u64", ("dt", 4): "u32",
    ("ldt", 8): "u64", ("tod", 4): "u32", ("ltod", 8): "u64", ("enum", 1): "u8", ("enum", 2): "u16",
    ("enum", 4): "u32", ("enum", 8): "u64",
}


class NativeUnavailable(RuntimeError):
    """The native engine cannot take this recording (reason in the message); the Python recorder can."""


class NativeStartError(RuntimeError):
    """The service ran and refused the start (e.g. a block could not be subscribed) — not a reason to fall back."""


# ------------------------------------------------------------------ executable
def default_exe() -> Path:
    return Path(__file__).resolve().parent / "bin" / EXE_NAME


_PROBE: dict[str, tuple[float, dict[str, Any] | None, str | None]] = {}


def probe(exe: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(version info, None) or (None, reason). Cached per path + mtime."""
    try:
        st = exe.stat()
    except OSError:
        return None, f"native capture service not installed ({exe} missing)"
    key = f"{exe}|{st.st_mtime_ns}"
    hit = _PROBE.get(key)
    if hit:
        return hit[1], hit[2]
    info: dict[str, Any] | None = None
    reason: str | None = None
    try:
        r = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=10,
                           creationflags=_NO_WINDOW)
        info = json.loads(r.stdout.strip().splitlines()[0])
    except Exception as exc:  # noqa: BLE001
        reason = f"{exe.name} --version failed: {type(exc).__name__}: {exc}"
    _PROBE[key] = (time.time(), info, reason)
    return info, reason


def pinned_sha256(exe: Path) -> str | None:
    """The SHA-256 the release workflow wrote next to the exe it built (``tcmcp-capture.exe.sha256``)."""
    f = exe.with_name(exe.name + ".sha256")
    try:
        tok = f.read_text(encoding="ascii", errors="replace").split()
    except OSError:
        return None
    return tok[0].lower() if tok and len(tok[0]) == 64 else None


_HASHED: dict[str, str] = {}


def exe_sha256(exe: Path) -> str:
    st = exe.stat()
    key = f"{exe}|{st.st_mtime_ns}|{st.st_size}"
    if key not in _HASHED:
        h = hashlib.sha256()
        with open(exe, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        _HASHED[key] = h.hexdigest()
    return _HASHED[key]


def availability(exe: Path, version: str) -> tuple[bool, str, dict[str, Any] | None]:
    pin = pinned_sha256(exe)
    if pin is not None and exe.is_file():
        try:
            got = exe_sha256(exe)
        except OSError as exc:
            return False, f"{exe.name} unreadable: {exc}", None
        if got != pin:
            # never run a capture binary that is not byte-for-byte the one the release built
            return False, (f"{exe.name} does not match the SHA-256 shipped with it ({got[:12]}… ≠ {pin[:12]}…): "
                           "replaced or corrupted — reinstall the plugin; recording on the Python engine"), None
    info, reason = probe(exe)
    if info is None:
        return False, reason or "unknown", None
    if pin is not None:
        info = {**info, "sha256": pin, "sha256_verified": True}
    if info.get("protocol") != PROTOCOL:
        return False, f"{exe.name} speaks protocol {info.get('protocol')}, this server {PROTOCOL}", info
    if info.get("version") != version:
        return False, (f"{exe.name} is version {info.get('version')}, the server {version} — install the matching "
                       "build (bin/ of the same release)"), info
    return True, "ok", info


# ------------------------------------------------------------------ command building
def native_condition(cond: Any, rec: Any) -> dict[str, Any] | None:
    """recorder.Condition → raw-byte condition for the service, or None when it cannot be expressed natively."""
    if cond is None:
        return None
    hit = rec.slot_of.get(cond.symbol)
    if hit is None:
        raise NativeUnavailable(f"condition symbol {cond.symbol} is not in a notification block")
    block, slot = hit
    tt, off = slot.tt, slot.off
    if cond.index is not None:
        if tt.kind != "array" or tt.elem is None or not tt.elem.size:
            raise NativeUnavailable(f"condition on {cond.symbol}[…]: element type unknown")
        tt, off = tt.elem, off + cond.index * tt.elem.size
    kind = tt.kind if tt.kind != "enum?" else "enum"
    ntype = _NATIVE_TYPES.get((kind, tt.size or 0))
    value = cond.value
    if isinstance(value, dict):
        value = value.get("raw_ms", value.get("raw_ns", value.get("raw")))
    if ntype is None or not isinstance(value, (int, float, bool)):
        raise NativeUnavailable(f"condition on {cond.symbol} ({tt.name}, value {cond.value!r}) needs the Python engine")
    return {"block": block.bid, "off": off, "type": ntype, "op": cond.op,
            "value": bool(value) if isinstance(value, bool) else value, "edge": cond.edge,
            "text": f"{cond.symbol}{'' if cond.index is None else f'[#{cond.index}]'} {cond.op} {cond.value}"}


# ------------------------------------------------------------------ process
class CaptureProcess:
    """One tcmcp-capture process: JSON lines on stdin/stdout, stderr kept (last 50 lines) for diagnostics."""

    def __init__(self, exe: Path):
        self.exe = exe
        self.proc = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", bufsize=1, creationflags=_NO_WINDOW)
        self.replies: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.finished = threading.Event()
        self.finished_mono: float | None = None          # when the finished event arrived (D15: not when someone looked)
        self.finished_at: float | None = None
        self.on_finished: Any = None                     # callback (N27: seal as soon as the service closed the file)
        self.footer: dict[str, Any] | None = None
        self.stderr: deque[str] = deque(maxlen=50)
        self.ready: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._rd = threading.Thread(target=self._read_out, name="tcmcp-capture-out", daemon=True)
        self._rd.start()
        threading.Thread(target=self._read_err, name="tcmcp-capture-err", daemon=True).start()
        first = self._wait_reply(timeout=15, want="ready")
        if first is None:
            self.kill()
            raise NativeUnavailable(f"{exe.name} did not start: {' | '.join(self.stderr) or 'no output'}")
        self.ready = first

    def _read_out(self) -> None:
        assert self.proc.stdout is not None
        for ln in self.proc.stdout:
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                self.stderr.append(f"(stdout) {ln[:200]}")
                continue
            if d.get("event") == "finished":
                self.footer = d.get("footer")
                self.finished_mono, self.finished_at = time.monotonic(), time.time()
                self.finished.set()
                cb = self.on_finished
                if cb is not None:
                    # not on this reader thread: the callback waits for the process to exit, which closes stdout
                    threading.Thread(target=cb, name="tcmcp-capture-finished", daemon=True).start()
            elif d.get("event") == "ready":
                self.replies.put(d)
            else:
                self.replies.put(d)
        if not self.finished.is_set():
            self.finished_mono, self.finished_at = time.monotonic(), time.time()
        self.finished.set()            # process gone
        self.replies.put({"reply": "eof", "ok": False, "error": "capture process ended"})

    def _read_err(self) -> None:
        assert self.proc.stderr is not None
        for ln in self.proc.stderr:
            if ln.strip():
                self.stderr.append(ln.rstrip()[:300])

    def _wait_reply(self, timeout: float, want: str | None = None) -> dict[str, Any] | None:
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return None
            try:
                d = self.replies.get(timeout=left)
            except queue.Empty:
                return None
            if want is None or d.get("reply") == want or d.get("event") == want or d.get("reply") == "eof":
                return d

    def call(self, cmd: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        with self._lock:
            if self.proc.poll() is not None:
                return {"ok": False, "error": f"capture process exited ({self.proc.returncode})"}
            try:
                assert self.proc.stdin is not None
                self.proc.stdin.write(json.dumps(cmd) + "\n")
                self.proc.stdin.flush()
            except OSError as exc:
                return {"ok": False, "error": f"capture process pipe: {exc}"}
            d = self._wait_reply(timeout, want=cmd["cmd"])
            return d if d is not None else {"ok": False, "error": f"no reply to {cmd['cmd']} within {timeout}s"}

    def close(self, timeout: float = 30.0) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()      # EOF: the service ends the recording cleanly and exits
        except OSError:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


# ------------------------------------------------------------------ recording facade
class NativeRecording:
    """A cycle-mode recording run by tcmcp-capture. Wraps the resolved recorder.Recording (symbols, blocks,
    header) and answers the same questions; values and statistics come from the file (rec_stats / rec_read)."""

    engine = "native"

    def __init__(self, rec: Any, proc: CaptureProcess, start_reply: dict[str, Any], header: dict[str, Any]):
        self.rec = rec
        self.proc = proc
        self.start_reply = start_reply
        self.header = header
        self.id, self.name, self.mode, self.path = rec.id, rec.name, rec.mode, rec.path
        self.symbols, self.blocks, self.cycle_ms = rec.symbols, rec.blocks, rec.cycle_ms
        self.stop_when = rec.stop_when
        self.created = rec.created
        self.t0_mono: float | None = time.monotonic()
        self.stopped_mono: float | None = None
        self.stopped_at: float | None = None
        self.live_stats = False
        self.samples = 0
        self.stats_samples = 0
        self.events = 0
        self.conn = None
        self._last: dict[str, Any] = {}
        self._closed = False
        self.manifest: dict[str, Any] | None = None
        self._reap_lock = threading.Lock()
        proc.on_finished = self.reap

    # -------------------------------------------------- state
    @property
    def active(self) -> bool:
        if self._closed:
            return False
        if self.proc.finished.is_set():
            self._mark_stopped()
            return False
        return True

    @property
    def stop_reason(self) -> str | None:
        f = self.proc.footer or {}
        return f.get("stop_reason") or self._last.get("stop_reason")

    def _mark_stopped(self) -> None:
        if self.stopped_mono is None:
            self.stopped_mono = self.proc.finished_mono or time.monotonic()
            self.stopped_at = self.proc.finished_at or time.time()

    # -------------------------------------------------- control
    def stop(self, reason: str) -> dict[str, Any]:
        with self._reap_lock:
            if not self._closed:
                if not self.proc.finished.is_set():
                    r = self.proc.call({"cmd": "stop", "reason": reason}, timeout=120)
                    if r.get("footer"):
                        self.proc.footer = r["footer"]
                self.proc.close()
                self._closed = True
                self._mark_stopped()
                self._seal()
        return self.status()

    def reap(self) -> None:
        """A recording that ended on its own (duration, stop_when…): let the process exit, then seal (N27). Called from
        the finished-event thread and from Recorder.reap(); idempotent."""
        with self._reap_lock:
            if not self._closed and self.proc.finished.is_set():
                self.proc.close()
                self._closed = True
                self._mark_stopped()
                self._seal()

    def _seal(self) -> None:
        """R9: sidecar manifest + read-only, once the service has closed the file."""
        from .recorder import seal_recording
        self.manifest = seal_recording(self.path, getattr(self.rec, "key", None))

    def request_stop(self, reason: str = "requested") -> None:
        self.stop(reason)

    # -------------------------------------------------- views
    def status(self) -> dict[str, Any]:
        live: dict[str, Any] = {}
        if not self._closed and not self.proc.finished.is_set():
            live = self.proc.call({"cmd": "status"}, timeout=10)
            if live.get("ok"):
                self._last = live
        src = self.proc.footer if (self._closed or self.proc.finished.is_set()) and self.proc.footer else (self._last or {})
        self.samples = int(src.get("samples") or 0)
        self.events = int(src.get("events") or 0)
        elapsed = ((self.stopped_mono or time.monotonic()) - self.t0_mono) if self.t0_mono else 0.0
        st: dict[str, Any] = {
            "id": self.id, "name": self.name, "active": self.active, "mode": self.mode, "cycle_ms": self.cycle_ms,
            "time_source": "plc", "engine": "native", "engine_version": self.start_reply.get("version"),
            "symbols": len(self.symbols), "notifications": len(self.blocks), "events": self.events,
            "samples": self.samples, "missing_samples": src.get("missing_samples"),
            "missing_head": src.get("missing_head"), "missing_gaps": src.get("missing_gaps"),
            "missing_tail": src.get("missing_tail"), "completeness": src.get("completeness"),
            "overflow_samples": src.get("overflow_samples"), "dropped": src.get("overflow_samples") or 0,
            "dropped_on_stop": 0, "after_limit": src.get("after_limit"),
            "backlog": self._last.get("backlog", 0), "max_backlog": self._last.get("max_backlog", 0),
            "stats_coverage": "from the file (native engine): rec_stats computes exact statistics",
            "events_per_s": round(self.events / elapsed, 1) if elapsed > 0 else 0.0,
            "elapsed_s": round(elapsed, 2), "plc_span_s": src.get("plc_span_s") or self._last.get("plc_span_s"),
            # N29: the last progress line may predate the trigger (or the stop) — never report armed once the
            # trigger fired or the recording ended
            "armed": bool(self.active and self._last.get("armed", False)
                          and src.get("triggered_at_ms", self._last.get("triggered_at_ms")) is None),
            "triggered_at_ms": src.get("triggered_at_ms", self._last.get("triggered_at_ms")),
            "stop_reason": self.stop_reason, "file": str(self.path),
            "file_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "ring": {"seconds": self.start_reply.get("ring", {}).get("seconds"), "bytes": self.start_reply.get("ring", {}).get("bytes")},
            "source": self.start_reply.get("source"),
            "last_values": self._last_values(), "errors": (src.get("errors") or [])[-5:],
            "blocks": (src.get("blocks") or [])[:20],
        }
        for k in ("stalls", "stalled_samples", "stalled", "stalled_since_ms", "missing_silent_est", "completeness_estimated",
                  "resubscriptions", "resubscribe_error", "resubscribe_failures", "port_reopens", "outage_reopens",
                  "reopen_error", "resubscribe_failures_total",
                  "label_reason", "stall_s",
                  # R43: the program the addresses belong to
                  "program_watch", "program_watch_error", "program_changed", "program_rebuilt_in_outage",
                  "program_watch_lost", "unverified_rows", "unverified_note", "verified_to_ms", "version_read_error",
                  "rows_before_window"):
            if k in src:
                st[k] = src[k]
        st.setdefault("warnings", []).extend(program_warnings(src))
        if src.get("stalled") and src.get("stalled_since_ms") is not None:
            st.setdefault("warnings", []).append(
                f"PLC STOPPED? the cycle counter has not advanced since {src['stalled_since_ms']} ms — samples since then are frozen values, not measurements")
        if src.get("resubscribe_error"):
            # N197: said while it lasts, not only in the footer — the runtime is away (the system service reads CONFIG),
            # or it is back and this connection is refused (the engine then opens a new one, at growing intervals)
            st.setdefault("warnings", []).append(
                f"no samples: re-registering the notifications failed {src.get('resubscribe_failures', '?')} time(s) "
                f"since the last samples, last: {src['resubscribe_error']}"
                + (f" — the ADS connection was opened again {src['outage_reopens']} time(s) since then"
                   if src.get("outage_reopens") else "")
                + (f" — opening it again failed: {src['reopen_error']}" if src.get("reopen_error") else ""))
        if src.get("missing_silent_est"):
            st.setdefault("warnings", []).append(
                f"≈{float(src['missing_silent_est']):.0f} samples missing so far from silent blocks (PC clock estimate)")
        if self.proc.footer:
            st["verdict_hint"] = self.proc.footer.get("verdict_hint")
            st["label"] = self.proc.footer.get("label")
            st["gap_markers"] = self.proc.footer.get("gap_markers")
            st["window_start_ms"] = self.proc.footer.get("window_start_ms")
            st["window_end_ms"] = self.proc.footer.get("window_end_ms")
        if not self.active and self.proc.proc.returncode not in (0, None) and not self.proc.footer:
            st["errors"] = st["errors"] + [f"capture process exited with {self.proc.proc.returncode}: {' | '.join(list(self.proc.stderr)[-3:])}"]
        warn = list(st.pop("warnings", []))
        if st["overflow_samples"]:
            warn.append(f"{st['overflow_samples']} samples LOST on this PC (capture ring full) — recording is not complete")
        if st["missing_samples"]:
            warn.append(f"{st['missing_samples']} samples missing in the PLC timeline (head {st['missing_head']}, gaps "
                        f"{st['missing_gaps']}, tail {st['missing_tail']})")
        if self._last.get("silent_blocks") and self.active:
            warn.append(f"{self._last['silent_blocks']} notification block(s) delivered nothing for a while (PLC stopped?)")
        if warn:
            st["warnings"] = warn
        return st

    def _last_values(self, limit: int = 20) -> dict[str, Any]:
        """Latest value per leaf, decoded from the last rows of the file (the service never decodes)."""
        from .recorder import trim_value
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 256 * 1024))
                tail = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return {}
        latest: dict[int, bytes] = {}
        for ln in reversed(tail):
            if not ln.startswith('[') or '"#' not in ln:
                continue
            try:
                row = json.loads(ln)
                bid = int(row[1][1:])
                if bid not in latest:
                    latest[bid] = base64.b64decode(row[2])
            except Exception:  # noqa: BLE001
                continue
            if len(latest) >= len(self.blocks):
                break
        vals: dict[str, Any] = {}
        items = [(sl.name, sl.decode(latest[b.bid])) for b in self.blocks if b.bid in latest for sl in b.slots]
        for k, v in items[:limit]:
            vals[k] = trim_value(v)
        if len(items) > limit:
            vals["…"] = f"{len(items) - limit} more leaves (rec_read for all)"
        return vals

    def snapshot(self, last_n: int, symbols: list[str] | None) -> list[dict[str, Any]]:
        from .recorder import iter_events
        want = {s.lower() for s in symbols} if symbols else None
        ring: deque[dict[str, Any]] = deque(maxlen=max(1, last_n))
        for t, n, v in iter_events(self.path, want):
            ring.append({"t_ms": round(t, 3), "symbol": n, "value": v})
        return list(ring)

    def stats_dict(self, symbols: list[str] | None, include_runs: bool, max_runs_out: int) -> dict[str, Any]:
        return {}


class ProgramMoved(Exception):
    """The PLC program changed after the variables were looked up: their addresses may belong to other variables."""

    def __init__(self, msg: str, now: Any) -> None:
        super().__init__(msg)
        self.now = now


def program_warnings(src: dict[str, Any]) -> list[str]:
    """R43, both engines: what a program change (or a version that could not be checked) did to the recording."""
    out: list[str] = []
    pc = src.get("program_changed")
    if isinstance(pc, dict) or pc is True:
        at = pc.get("after_ms") if isinstance(pc, dict) else src.get("verified_to_ms")
        out.append("the PLC program changed during the recording (download / online change): it ends at the last "
                   f"sample verified before the change ({at} ms); nothing of the new program is in the file")
    elif src.get("program_rebuilt_in_outage"):
        out.append(str(src["program_rebuilt_in_outage"]))
    if src.get("program_watch_lost"):
        out.append("the symbol version could not be read while samples kept coming: the recording ends at the last "
                   "verified sample")
    if src.get("program_watch_error"):
        out.append(str(src["program_watch_error"]))
    if src.get("unverified_rows"):
        out.append(f"{src['unverified_rows']} samples delivered but not written: " + str(src.get("unverified_note") or
                   "they could not be verified against a program change"))
    return out


def start_native(rec: Any, header: dict[str, Any], exe: Path, *, net_id: str, port: int, ip: str | None,
                 local_net_id: str | None, cycle_ms: float, max_delay_ms: float, duration_s: float | None,
                 post_trigger_s: float | None, pre_trigger_ms: float, max_events: int | None, key_path: Path,
                 ring_s: float, ring_cap_mb: float, timeout_ms: int, backend: str = "ads",
                 extra: dict[str, Any] | None = None) -> NativeRecording:
    """Launch tcmcp-capture for a resolved recording. Raises NativeUnavailable (fall back to Python) or
    NativeStartError (the service refused: do not fall back, the Python recorder would fail the same way)."""
    sw = native_condition(rec.start_when, rec)
    ew = native_condition(rec.stop_when, rec)
    blocks = [{"id": b.bid, "igrp": b.igrp, "ioffs": b.ioffs, "size": b.size, "leaves": max(1, len(b.slots))}
              for b in rec.blocks]
    cmd: dict[str, Any] = {
        "cmd": "start", "backend": backend, "ams_net_id": net_id, "port": port, "ip": ip or "",
        "local_ams_net_id": local_net_id or "", "timeout_ms": timeout_ms,
        "file": str(rec.path), "header": json.dumps(header, default=str), "blocks": blocks,
        "cycle_100ns": int(round(cycle_ms * 10_000)), "max_delay_100ns": int(round((max_delay_ms or 0) * 10_000)),
        "duration_s": duration_s or 0, "post_trigger_s": post_trigger_s or 0, "pre_trigger_ms": pre_trigger_ms or 0,
        "max_events": max_events or 0, "start_when": sw, "stop_when": ew, "hmac_key_path": str(key_path),
        "ring_s": ring_s, "ring_cap_mb": ring_cap_mb,
    }
    if extra:
        cmd.update(extra)
    rec.path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = CaptureProcess(exe)
    except NativeUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise NativeUnavailable(f"{exe.name} could not be launched: {type(exc).__name__}: {exc}") from exc
    r = proc.call(cmd, timeout=120)
    if not r.get("ok"):
        proc.close(timeout=5)
        err = str(r.get("error") or "start failed")
        if "symbol_version_now" in r:
            raise ProgramMoved(err, r["symbol_version_now"])
        if "block" in r:
            raise NativeStartError(err)
        if any(s in err for s in ("TcAdsDll.dll not found", "has no export", "could not open an ADS port", "standalone AdsLib")):
            raise NativeUnavailable(err)
        raise NativeStartError(err)
    return NativeRecording(rec, proc, r, header)

