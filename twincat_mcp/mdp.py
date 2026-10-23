"""IPC diagnostics: the Beckhoff Device Manager's information model (MDP, Modular Device Profile) read over ADS.

The model (Infosys `devicemanager`, read 2026-10-23):
- a table index is 16 bits `0xAnnT` — area A (4 bits: 0x8 configuration area, 0xF device area), module ID nn (8 bits,
  made at start-up), table T (4 bits); subindex 0 of a table is its length (UNSIGNED16);
- table 0xF020 lists the modules: each row holds the module type in the high word and the module ID in the low word;
- every module has the header 0x8nn0 (1 Address, 2 Type, 3 Name, 4 Dev type); the tables decoded here are the ones
  the module pages document (CPU 0x000B, Memory 0x000C, TwinCAT 0x0008, OS 0x0018, Fan 0x001B, Mainboard 0x001C,
  Disk Management 0x001D). Module types: Tc3_IPCDiag's E_IPCDiag_ModuleType.

The ADS address is NOT confirmed (docs/PLAN-0.8.19.md, IPC diagnostics): AMS port 10000 (the system service) and index group 0xF302
with index offset `index << 16 | flags << 8 | subindex` — inferred from Tc3_IPCDiag's nIndex / nFlags / nSubIndex,
not read on a page. The first hardware check decides it; nothing here is released before it passes.
"""
from __future__ import annotations

import struct
from typing import Any, Callable

MDP_PORT = 10000
MDP_INDEX_GROUP = 0xF302
MODULE_LIST = 0xF020

MODULE_TYPES = {
    0x0002: "NIC", 0x0003: "Time", 0x0004: "UserManagement", 0x0005: "RAS", 0x0006: "FTP", 0x0007: "SMB",
    0x0008: "TwinCAT", 0x0009: "Datastore", 0x000A: "Software", 0x000B: "CPU", 0x000C: "Memory", 0x000E: "Firewall",
    0x0010: "FileSystemObject", 0x0012: "PLC", 0x0013: "DisplayDevice", 0x0014: "EWF", 0x0015: "FBWF",
    0x0017: "SiliconDrive", 0x0018: "OS", 0x0019: "Raid", 0x001B: "Fan", 0x001C: "Mainboard", 0x001D: "DiskManagement",
    0x001E: "UPS", 0x001F: "PhysicalDrive", 0x0020: "MassStorage", 0x0021: "UWF", 0x0100: "Misc",
}

# fixed size of each type; strings are read into a buffer of STRING_MAX bytes and cut at the first NUL
_FMT = {"u8": "<B", "u16": "<H", "u32": "<I", "u64": "<Q", "i16": "<h", "i32": "<i", "bool": "<B"}
STRING_MAX = 256

# module type → [(table, subindex, key, type, unit or None)] — the rows the Device Manager pages document
TABLES: dict[int, list[tuple[int, int, str, str, str | None]]] = {
    0x000B: [(1, 1, "cpu_frequency", "u32", None), (1, 2, "cpu_usage", "u16", "%"),
             (1, 3, "cpu_temperature", "i16", "°C")],
    0x000C: [(1, 1, "program_memory_allocated", "u32", "byte"), (1, 2, "program_memory_available", "u32", "byte"),
             (1, 6, "program_memory_allocated_64", "u64", "byte"),
             (1, 7, "program_memory_available_64", "u64", "byte")],
    0x0008: [(1, 1, "version_major", "u16", None), (1, 2, "version_minor", "u16", None), (1, 3, "build", "u16", None),
             (1, 12, "revision", "u16", None), (1, 4, "ams_net_id", "string", None),
             (1, 6, "twincat_status", "u16", None), (1, 13, "seconds_since_status_change", "u64", "s"),
             (1, 11, "system_id", "string", None),
             (9, 1, "router_memory_max", "u64", "byte"), (9, 2, "router_memory_available", "u64", "byte"),
             (9, 3, "registered_ports", "u32", None), (9, 4, "registered_drivers", "u32", None),
             (9, 7, "mailbox_size", "u32", None), (9, 8, "mailbox_used_entries", "u32", None)],
    0x0018: [(1, 1, "os_major", "u32", None), (1, 2, "os_minor", "u32", None), (1, 3, "os_build", "u32", None),
             (1, 4, "csd_version", "string", None), (1, 5, "os_version", "string", None),
             (2, 1, "uptime", "u64", "s")],
    0x001B: [(1, 1, "fan_speed", "i16", "rpm")],
    0x001C: [(1, 1, "mainboard_type", "string", None), (1, 2, "serial_number", "string", None),
             (1, 3, "production_date", "string", None), (1, 4, "boot_count", "u32", None),
             (1, 5, "operating_time", "u32", "min"), (1, 6, "board_temperature_min", "i32", "°C"),
             (1, 7, "board_temperature_max", "i32", "°C"), (1, 10, "board_temperature", "i16", "°C"),
             (2, 1, "mainboard_revision", "u8", None), (2, 4, "bios_version", "string", None)],
}
# list tables (subindex 0 = Len, 1..Len one entry each): Disk Management — the entries of its tables belong together
# by position (drive 1 is entry 1 of every table; the page shows them so, it does not say it in words)
LIST_TABLES: dict[int, list[tuple[int, str, str, str | None]]] = {
    0x001D: [(1, "drive", "string", None), (2, "label", "string", None), (3, "filesystem", "string", None),
             (4, "drive_type", "u32", None), (5, "size", "u64", "byte"), (6, "free", "u64", "byte")],
}
DRIVE_TYPES = {0: "unknown", 1: "fixed", 2: "removable", 4: "cdrom"}
MAX_LIST = 32                              # entries read of one list table
MAX_READS = 600                            # ADS reads of one ipc_info call


class MdpError(Exception):
    """One MDP read that failed (the ADS error as text)."""


class MdpStop(MdpError):
    """The read cannot go on: the call was cancelled, or the target stopped answering."""


def check_address(index: int, subindex: int) -> None:
    """An MDP address is a 16-bit index and an 8-bit subindex — anything else would be cut into another address."""
    if not (0 <= int(index) <= 0xFFFF and 0 <= int(subindex) <= 0xFF):
        raise MdpError(f"index 0x{int(index):X} / subindex {int(subindex)} is not an MDP address (index 0…0xFFFF, "
                       "subindex 0…0xFF) — nothing was read")


def index_offset(index: int, subindex: int, flags: int = 0) -> int:
    check_address(index, subindex)
    return ((index & 0xFFFF) << 16) | ((flags & 0xFF) << 8) | (subindex & 0xFF)


def table_index(module_id: int, table: int, area: int = 0x8) -> int:
    if not 0 <= module_id <= 0xFF:
        raise MdpError(f"module ID {module_id} does not fit the 8-bit module field of a table index")
    return ((area & 0xF) << 12) | ((module_id & 0xFF) << 4) | (table & 0xF)


def label(index: int, subindex: int) -> str:
    """'0x8011:03' — index and subindex both hex, as CoE / MDP tables write them."""
    return f"0x{index:04X}:{subindex:02X}"


def size_of(typ: str) -> int:
    return STRING_MAX if typ == "string" else struct.calcsize(_FMT[typ])


def decode(raw: bytes, typ: str) -> Any:
    """raw → value. A fixed-size type needs exactly its size (a shorter answer is an error, never padded to a value);
    a STRING ends at its first NUL (TwinCAT strings are Windows-1252)."""
    if typ == "string":
        return raw.split(b"\x00", 1)[0].decode("cp1252", errors="replace")
    if typ == "hex":
        return raw.hex()
    fmt = _FMT[typ]
    n = struct.calcsize(fmt)
    if len(raw) != n:
        raise MdpError(f"the target answered {len(raw)} byte(s), {typ} needs {n} — not decoded")
    v = struct.unpack(fmt, raw)[0]
    return bool(v) if typ == "bool" else v


# reader(index, subindex, type) → the decoded value; raises MdpError (MdpStop: stop reading)
Reader = Callable[[int, int, str], Any]
STOP_AFTER_TIMEOUTS = 3                    # consecutive reads that time out: the target is taken as not answering


class _Budget:
    def __init__(self, reader: Reader, limit: int):
        self.reader, self.left, self.used, self.timeouts = reader, limit, 0, 0

    def read(self, index: int, subindex: int, typ: str) -> Any:
        if self.left <= 0:
            raise MdpStop("read budget used up")
        self.left -= 1
        self.used += 1
        try:
            v = self.reader(index, subindex, typ)
        except MdpStop:
            raise
        except Exception as exc:  # noqa: BLE001
            if "1861" in str(exc) or "timeout" in str(exc).lower():
                self.timeouts += 1
                if self.timeouts >= STOP_AFTER_TIMEOUTS:
                    raise MdpStop(f"the target stopped answering ({self.timeouts} reads in a row timed out: {exc})") \
                        from exc
            else:
                self.timeouts = 0
            raise
        self.timeouts = 0
        return v


def read_info(reader: Reader, max_reads: int = MAX_READS, types: set[int] | None = None) -> dict[str, Any]:
    """The module list and, per module, its name and the documented values. A value that fails is listed with its
    error (never left out, never 0); a module type without documented tables gets its name only. A module list that
    cannot be one (more modules than the 8-bit module ID allows) is refused: a wrong ADS address answers something."""
    b = _Budget(reader, max_reads)
    try:
        count = int(b.read(MODULE_LIST, 0, "u16"))
    except Exception as exc:  # noqa: BLE001
        raise MdpError(f"the module list ({label(MODULE_LIST, 0)}) could not be read: {exc}") from exc
    if count > 0xFF:                       # subindex 0 is the count: rows 1…255 are all a table can address
        raise MdpError(f"the module list ({label(MODULE_LIST, 0)}) says {count} modules — more than the 8-bit module ID "
                       "allows: this is not a module list (a wrong ADS address?). Nothing is reported from it")
    modules: list[dict[str, Any]] = []
    skipped, stopped, row = 0, None, 0
    for row in range(1, count + 1):
        try:
            m = _read_module(b, row, types)
        except MdpStop as exc:
            stopped = str(exc)
            row -= 1
            break
        if m is None:
            skipped += 1
        else:
            modules.append(m)
    out: dict[str, Any] = {"module_count": count, "modules": modules, "reads": b.used}
    if skipped:
        out["modules_not_asked"] = skipped
    if stopped:
        out["stopped"] = (f"{stopped} — {count - row} of {count} module(s) not read"
                          + (" (raise max_reads or ask for fewer module types)" if "budget" in stopped else ""))
    return out


def _read_module(b: _Budget, row: int, types: set[int] | None) -> dict[str, Any] | None:
    """One row of the module list — None when its type was not asked for. MdpStop passes through, also in the
    middle of a module (its values read so far are dropped with it: the caller says which rows were not read)."""
    try:
        v = int(b.read(MODULE_LIST, row, "u32"))
    except MdpStop:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"row": row, "error": str(exc)[:200]}
    mtype, mid = v >> 16, v & 0xFFFF
    if types is not None and mtype not in types:
        return None
    m: dict[str, Any] = {"type": MODULE_TYPES.get(mtype, f"0x{mtype:04X}"), "type_id": f"0x{mtype:04X}",
                         "module_id": mid}
    if mid > 0xFF:
        m["error"] = f"module ID {mid} does not fit the 8-bit module field of a table index — its tables cannot be read"
        return m
    errors: dict[str, str] = {}
    try:
        m["name"] = b.read(table_index(mid, 0), 3, "string")
    except MdpStop:
        raise
    except Exception as exc:  # noqa: BLE001
        errors["name"] = str(exc)[:200]
    values: dict[str, Any] = {}
    for table, sub, key, typ, unit in TABLES.get(mtype, []):
        idx = table_index(mid, table)
        try:
            val = b.read(idx, sub, typ)
        except MdpStop:
            raise
        except Exception as exc:  # noqa: BLE001
            errors[key] = f"{label(idx, sub)}: {str(exc)[:160]}"
            continue
        values[key] = {"value": val, "unit": unit, "mdp": label(idx, sub)} if unit else \
            {"value": val, "mdp": label(idx, sub)}
    if mtype in LIST_TABLES:
        rows = _read_list(b, mid, LIST_TABLES[mtype], errors)
        if rows is not None:
            values["volumes"] = rows
    if values:
        m["values"] = values
    if errors:
        m["errors"] = errors
    if mtype not in TABLES and mtype not in LIST_TABLES:
        m["note"] = "no documented table decoded for this module type — ipc_mdp_read reads any address"
    return m


def _read_list(b: _Budget, mid: int, cols: list[tuple[int, str, str, str | None]],
               errors: dict[str, str]) -> list[dict[str, Any]] | None:
    lens = {}
    for table, key, _typ, _unit in cols:
        try:
            lens[key] = int(b.read(table_index(mid, table), 0, "u16"))
        except MdpStop:
            raise
        except Exception as exc:  # noqa: BLE001
            errors[f"{key}_count"] = str(exc)[:160]
    if not lens:
        return None
    longest = max(lens.values())
    n = min(longest, MAX_LIST)
    if longest > MAX_LIST:
        errors["volumes_cut"] = f"{longest} entries listed, the first {MAX_LIST} read"
    rows: list[dict[str, Any]] = [{} for _ in range(n)]
    for table, key, typ, _unit in cols:
        for i in range(1, min(lens.get(key, 0), n) + 1):
            try:
                v = b.read(table_index(mid, table), i, typ)
            except MdpStop:
                raise
            except Exception as exc:  # noqa: BLE001
                rows[i - 1][key] = {"error": str(exc)[:160]}
                continue
            if key == "drive_type":
                v = DRIVE_TYPES.get(v, v)
            rows[i - 1][key] = v
    if len(set(lens.values())) > 1:
        errors["volumes"] = f"the tables list different numbers of entries: {lens} — rows matched by position"
    return rows
