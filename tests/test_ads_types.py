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


def test_r42_pointer_to_fb_is_an_address_not_the_fb():
    """R42: ARRAY[1..1000] OF POINTER TO FB_TestSuite (TcUnit) was expanded as 1,000 whole FBs at the pointers' 8 bytes."""
    from twincat_mcp.ads_types import TypeSystem, DtEntry
    fb = DtEntry("FB_TestSuite", "", 4000, 0, 65, 0, "", [], [
        DtEntry("InstancePath", "STRING(80)", 81, 0, 30, 0, ""),
        DtEntry("Tests", "ARRAY [1..100] OF ST_Test", 3900, 100, 65, 0, "", [(1, 100)]),
    ])
    st = DtEntry("ST_Test", "", 39, 0, 65, 0, "", [], [DtEntry("Name", "STRING(30)", 31, 0, 30, 0, ""),
                                                        DtEntry("Ok", "BOOL", 1, 32, 33, 0, "")])
    ptr = DtEntry("POINTER TO FB_TestSuite", "FB_TestSuite", 8, 0, 65, 1, "")
    arr = DtEntry("ARRAY [1..1000] OF POINTER TO FB_TestSuite", "POINTER TO FB_TestSuite", 8000, 0, 65, 1, "", [(1, 1000)])
    gvl_member = DtEntry("pCurrent", "POINTER TO FB_TestSuite", 8, 0, 65, 0, "")
    holder = DtEntry("ST_Holder", "", 16, 0, 65, 0, "", [], [gvl_member, DtEntry("pRaw", "POINTER TO BYTE", 8, 8, 65, 0, "")])
    ts = TypeSystem({e.name.lower(): e for e in (fb, st, ptr, arr, holder,
                                                  DtEntry("STRING(80)", "", 81, 0, 30, 0, ""),
                                                  DtEntry("STRING(30)", "", 31, 0, 30, 0, ""),
                                                  DtEntry("BOOL", "", 1, 0, 33, 0, ""))})
    lv = ts.leaves("GVL_TcUnit.TestSuiteAddresses", arr.name)
    assert len(lv) == 1000 and {l.type for l in lv} == {"POINTER TO FB_TestSuite"} and lv[1].offset == 8
    assert "address" in lv[0].note
    buf = (0x1F2E3D4C5B6A7980).to_bytes(8, "little") + bytes(8 * 999)
    tree = ts.decode_tree(buf, arr.name)
    assert len(tree) == 1000 and tree[0] == "0x1F2E3D4C5B6A7980" and tree[1] == "0x0000000000000000"   # N51: NULL is address 0
    assert ts.decode_leaf(buf, lv[0]) == "0x1F2E3D4C5B6A7980"
    # members declared POINTER TO …, with and without a table entry for the pointer type
    hl = ts.leaves("MAIN.st", "ST_Holder")
    assert [(l.path, l.type) for l in hl] == [("MAIN.st.pCurrent", "POINTER TO FB_TestSuite"), ("MAIN.st.pRaw", "POINTER TO BYTE")]
    d = ts.decode_tree(bytes(16), "ST_Holder")
    assert d == {"pCurrent": "0x0000000000000000", "pRaw": "0x0000000000000000"}
    # review: a 32-bit runtime (CX9020) has 4-byte pointers — without a table entry the declared size is used
    h32 = DtEntry("ST_H32", "", 8, 0, 65, 0, "", [], [DtEntry("pRaw", "POINTER TO BYTE", 4, 0, 65, 0, ""),
                                                      DtEntry("nNext", "UDINT", 4, 4, 19, 0, "")])
    ts.table["st_h32"] = h32
    ts.table["udint"] = DtEntry("UDINT", "", 4, 0, 19, 0, "")
    buf32 = (0x00C0FFEE).to_bytes(4, "little") + (7).to_bytes(4, "little")
    l32 = ts.leaves("MAIN.h", "ST_H32")
    assert [(l.path, l.size) for l in l32] == [("MAIN.h.pRaw", 4), ("MAIN.h.nNext", 4)]
    assert ts.decode_leaf(buf32, l32[0]) == "0x00C0FFEE" and ts.decode_leaf(buf32, l32[1]) == 7
    assert ts.decode_tree(buf32, "ST_H32") == {"pRaw": "0x00C0FFEE", "nNext": 7}
    root = ts.leaves("GVL.pX", "POINTER TO WORD", size=4)                       # a root symbol of 4 bytes
    assert root[0].size == 4 and ts.decode_tree(buf32[:4], "POINTER TO WORD", size=4) == "0x00C0FFEE"
    # an ordinary alias of a struct (same size) is still expanded
    alias = DtEntry("T_TestAlias", "ST_Test", 39, 0, 65, 0, "")
    ts.table["t_testalias"] = alias
    assert [l.path for l in ts.leaves("x", "T_TestAlias")] == ["x.Name", "x.Ok"]


def test_n50_alias_of_a_pointer_type_is_an_address_in_every_decoder():
    """N50: TYPE PT_X : POINTER TO FB_X was not seen as an indirection — CSV 4660, JSON '0x…1234'."""
    from twincat_mcp.ads_types import DtEntry, TypeSystem
    fb = DtEntry("FB_X", "", 400, 0, 65, 0, "", [], [DtEntry("n", "UDINT", 4, 0, 19, 0, "")])
    ptr = DtEntry("POINTER TO FB_X", "FB_X", 8, 0, 21, 1, "")
    alias = DtEntry("PT_X", "POINTER TO FB_X", 8, 0, 21, 0, "")
    alias2 = DtEntry("PT_Y", "PT_X", 8, 0, 21, 0, "")                     # an alias of the alias
    st = DtEntry("ST_H", "", 16, 0, 65, 0, "", [], [DtEntry("p", "PT_X", 8, 0, 21, 0, ""),
                                                     DtEntry("q", "PT_Y", 8, 8, 21, 0, "")])
    ts = TypeSystem({e.name.lower(): e for e in (fb, ptr, alias, alias2, st, DtEntry("UDINT", "", 4, 0, 19, 0, ""))})
    buf = (0x1234).to_bytes(8, "little") + bytes(8)
    lv = ts.leaves("MAIN.st", "ST_H")
    assert [(l.path, l.note) for l in lv] == [("MAIN.st.p", "pointer/reference: the address, not the target"),
                                              ("MAIN.st.q", "pointer/reference: the address, not the target")]
    assert [ts.decode_leaf(buf, l) for l in lv] == ["0x0000000000001234", "0x0000000000000000"]
    assert ts.decode_tree(buf, "ST_H") == {"p": "0x0000000000001234", "q": "0x0000000000000000"}
    assert ts.is_indirect(alias) and ts.is_indirect(alias2) and not ts.is_indirect(fb)


def test_n48_n55_one_not_expanded_form_and_a_cancellable_walk():
    import pytest as _p
    from twincat_mcp.ads_types import DtEntry, TypeSystem, WalkCancelled
    arr = DtEntry("ARRAY [1..5000] OF UDINT", "UDINT", 20000, 0, 19, 0, "", [(1, 5000)])
    big = DtEntry("ARRAY [1..2] OF ARRAY [1..4000] OF UDINT", "ARRAY [1..4000] OF UDINT", 32000, 0, 65, 0, "", [(1, 2)])
    inner = DtEntry("ARRAY [1..4000] OF UDINT", "UDINT", 16000, 0, 19, 0, "", [(1, 4000)])
    ts = TypeSystem({e.name.lower(): e for e in (arr, big, inner, DtEntry("UDINT", "", 4, 0, 19, 0, ""))})
    buf = bytes(range(64)) + bytes(20000 - 64)
    leaf = ts.leaves("GVL.a", arr.name)[0]
    assert leaf.not_expanded and leaf.need == 64
    assert ts.decode_leaf(buf[:64], leaf) == ts.decode_tree(buf, arr.name)                # one representation
    assert ts.decode_tree(buf, arr.name)["elements"] == 5000
    with _p.raises(WalkCancelled):
        ts.leaves("GVL.b", big.name, 10_000, cancel=lambda: True)


def test_nest_builds_the_decode_tree_shape_from_leaf_values():
    """The dump's JSON is nested from the same leaf values the CSV holds (N48/N50): structs → objects, arrays →
    lists in index order, multi-dimensional arrays → nested lists."""
    from twincat_mcp.ads_backend import _nest
    vals = {"G.a[1,1].x": 1, "G.a[1,2].x": 2, "G.a[2,1].x": 3, "G.a[2,2].x": 4, "G.s.t[-1]": "m", "G.s.t[0]": "z"}
    assert _nest("G", vals) == {"a": [[{"x": 1}, {"x": 2}], [{"x": 3}, {"x": 4}]], "s": {"t": ["m", "z"]}}
    assert _nest("G.n", {"G.n": 5}) == 5
