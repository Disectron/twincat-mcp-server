import struct

from twincat_mcp.ads_types import DtEntry, encode_entry, parse_entry, parse_table, TypeSystem
from tests.fake_ads import DATATYPES, DATATYPE_BLOB


def test_roundtrip_table():
    tbl = parse_table(DATATYPE_BLOB, len(DATATYPES))
    assert len(tbl) == len(DATATYPES)
    st = tbl["st_step"]
    assert [(m.name, m.type, m.offs, m.size) for m in st.subitems] == [("nId", "INT", 0, 2), ("tDwell", "TIME", 4, 4), ("bDone", "BOOL", 8, 1), ("eState", "E_FillState", 10, 2)]
    assert tbl["e_fillstate"].enum == {0: "Idle", 1: "Bulk", 2: "Fine", 3: "Done"}
    assert tbl["array [1..2] of st_step"].dims == [(1, 2)] and tbl["array [1..2] of st_step"].type == "ST_Step"


def test_attributes_and_entry_length_skip():
    e = DtEntry("ST_X", "", 4, 0, 0, 0, "a comment", [], [DtEntry("n", "DINT", 4, 0, 3, 0, "")], {}, {"pack_mode": "1"})
    blob = encode_entry(e) + b"\x00" * 10
    parsed, pos = parse_entry(blob, 0)
    assert parsed.attributes == {"pack_mode": "1"} and parsed.comment == "a comment" and pos == len(blob) - 10


def test_leaves_multidim_and_limits():
    tbl = {
        "int": DtEntry("INT", "", 2, 0, 2, 0, ""),
        "array [0..1,0..2] of int": DtEntry("ARRAY [0..1,0..2] OF INT", "INT", 12, 0, 2, 0, "", [(0, 2), (0, 3)]),
        "big": DtEntry("BIG", "INT", 20000, 0, 2, 0, "", [(0, 10000)]),
    }
    ts = TypeSystem(tbl)
    lv = ts.leaves("X", "ARRAY [0..1,0..2] OF INT")
    assert [l.path for l in lv] == ["X[0,0]", "X[0,1]", "X[0,2]", "X[1,0]", "X[1,1]", "X[1,2]"]
    assert [l.offset for l in lv] == [0, 2, 4, 6, 8, 10]
    big = ts.leaves("Y", "BIG")
    assert len(big) == 1 and "not expanded" in big[0].note
    tree = ts.decode_tree(struct.pack("<6h", 1, 2, 3, 4, 5, 6), "ARRAY [0..1,0..2] OF INT")
    assert tree == [[1, 2, 3], [4, 5, 6]]


def test_decode_unknown_type_falls_back_to_name():
    ts = TypeSystem({})
    assert ts.decode_tree(struct.pack("<d", 1.5), "LREAL") == 1.5
    assert ts.decode_tree(struct.pack("<I", 2500), "TIME")["text"] == "T#2s500ms"
    lv = ts.leaves("Z", "LREAL")
    assert lv[0].size == 8 and lv[0].note == "type not in datatype table"


def test_leaves_keep_declared_alias_name():
    """4026 datatype tables alias BOOL to BYTE/BIT; the leaf must still say BOOL (field item 19), and a
    STRING alias keeps its name while taking the base storage."""
    tbl = {
        "byte": DtEntry("BYTE", "", 1, 0, 16, 0, ""),
        "bool": DtEntry("BOOL", "BYTE", 1, 0, 16, 0, ""),
        "string(255)": DtEntry("STRING(255)", "", 256, 0, 30, 0, ""),
        "t_maxstring": DtEntry("T_MaxString", "STRING(255)", 256, 0, 30, 0, ""),
        "st_io": DtEntry("ST_IO", "", 258, 0, 65, 0, "", [], [
            DtEntry("bEnable", "BOOL", 1, 0, 16, 0, ""), DtEntry("sName", "T_MaxString", 256, 1, 30, 0, "")], {}, {}),
    }
    ts = TypeSystem(tbl)
    lv = ts.leaves("MAIN.stIo", "ST_IO")
    assert [(l.path, l.type, l.offset, l.size) for l in lv] == [("MAIN.stIo.bEnable", "BOOL", 0, 1), ("MAIN.stIo.sName", "T_MaxString", 1, 256)]
    assert ts.leaves("MAIN.bEnable", "BOOL")[0].type == "BOOL"
