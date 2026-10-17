import ctypes

import pytest
from pyads import constants as C

from twincat_mcp import types_tc as T


def test_scalars():
    assert T.parse_type("INT").ctype is C.PLCTYPE_INT
    assert T.parse_type("lreal").kind == "float"
    assert T.parse_type("BOOL").kind == "bool"
    assert T.parse_type("LWORD").size == 8


def test_strings():
    t = T.parse_type("STRING(40)")
    assert t.kind == "string" and t.size == 41
    assert T.parse_type("STRING").size == 81
    assert T.parse_type("WSTRING(10)").size == 22


def test_arrays_and_multidim():
    t = T.parse_type("ARRAY [0..9] OF LREAL", 80)
    assert t.kind == "array" and ctypes.sizeof(t.ctype) == 80 and t.elem.kind == "float"
    t2 = T.parse_type("ARRAY [1..2, 0..2] OF INT", 12)
    assert t2.dims == [(1, 2), (0, 2)]
    assert T.decode(t2, [1, 2, 3, 4, 5, 6]) == [[1, 2, 3], [4, 5, 6]]
    t3 = T.parse_type("ARRAY [0..1] OF STRING(5)", 12)
    assert t3.opaque and t3.elem.size == 6


def test_enum_vs_struct_by_size():
    assert T.parse_type("E_FillState", 2).kind == "enum"
    assert T.parse_type("E_FillState", 2).ctype is C.PLCTYPE_INT
    assert T.parse_type("ST_Recipe", 57).kind == "struct"
    assert T.parse_type("ST_Recipe", 57).opaque
    assert T.parse_type("FB_Valve", 8).kind == "struct"  # FB_ prefix never an enum


def test_time_decode_encode():
    t = T.parse_type("TIME")
    assert T.decode(t, 1500) == {"raw_ms": 1500, "text": "T#1s500ms"}
    assert T.decode(t, 90061001)["text"] == "T#1d1h1m1s1ms"
    assert T.encode(t, "T#1s500ms") == 1500
    assert T.encode(t, "2m") == 120000
    assert T.encode(t, 42) == 42
    lt = T.parse_type("LTIME")
    assert T.encode(lt, "LTIME#1us") == 1000
    assert T.decode(lt, 1_500_000_000)["text"].startswith("LTIME#1s500ms")


def test_date_decode_encode():
    d = T.parse_type("DATE")
    assert T.decode(d, 0)["text"] == "D#1970-01-01"
    assert T.encode(d, "D#2026-10-15") == 1792022400
    dt = T.parse_type("DT")
    assert T.decode(dt, 1792022400 + 3661)["text"] == "DT#2026-10-15-01:01:01"
    assert T.encode(dt, "DT#2026-10-15-01:01:01") == 1792022400 + 3661
    tod = T.parse_type("TOD")
    assert T.decode(tod, 3_600_000)["text"] == "TOD#1h"


def test_int_literals_and_bool():
    i = T.parse_type("UDINT")
    assert T.encode(i, "16#FF") == 255
    assert T.encode(i, "0x10") == 16
    assert T.encode(i, "2#101") == 5
    b = T.parse_type("BOOL")
    assert T.encode(b, "TRUE") is True and T.encode(b, "0") is False
    # N22: only what the error text documents — "yes" once reset a counter on the test PLC
    for bad in ("yes", "no", "on", "off", "maybe", 2, -1):
        with pytest.raises(ValueError):
            T.encode(b, bad)


def test_pointer_size():
    assert T.parse_type("POINTER TO ST_X", 8).ctype is C.PLCTYPE_ULINT
    assert T.parse_type("REFERENCE TO INT", 4).ctype is C.PLCTYPE_UDINT


def test_ads_state_names():
    assert T.ads_state_name(5) == "RUN"
    assert T.ads_state_name(6) == "STOP"
    assert T.ads_state_name(15) == "CONFIG"
