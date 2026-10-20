"""scripts/kb_verify.py — the offline, read-only verification of a knowledge folder, run here on the fixture knowledge
folder (indexes built by the real code: tests/fixtures/kb, the ESI fixtures of test_esi_index, a project of
tests/fixtures/sample_project): every check runs and passes on correct indexes, a wrong answer is found, an exception
of the plugin's code is a failure and not the end of the run, and nothing in the knowledge folder changes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def KV():
    spec = importlib.util.spec_from_file_location("tcmcp_kb_verify", ROOT / "scripts" / "kb_verify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# the fixture's ADSREAD page declares three of the real page's inputs; the other known answers are judged as they are
# (or not judged: their page is not in the fixture)
def _golden(KV):
    out = []
    for g in KV.GOLDEN:
        g = json.loads(json.dumps(g))
        if g["id"] == "decl-ADSREAD":
            g["expect"]["inputs"] = ["NETID", "PORT", "READ"]
        if isinstance(g["expect"].get("in_top"), list):
            g["expect"]["in_top"] = tuple(g["expect"]["in_top"])
        out.append(g)
    return out


@pytest.fixture
def full_kb(kb_copy, tmp_path, project_dir):
    """The fixture knowledge folder with an ESI index and an indexed project as well."""
    from tests import test_esi_index as T
    from twincat_mcp import esi_index as E
    from twincat_mcp import knowledge as K
    m = kb_copy / "manuals" / "Beckhoff_EtherCAT_XML"
    T._write(m / "Beckhoff EL33xx.xml", T.EL33.format(vendor=T.VENDOR.format(fv=10)))
    T._write(m / "Beckhoff EtherCAT Terminals.xml", T.TERMINALS.format(vendor=T.VENDOR.format(fv=3)))
    T._write(m / "Beckhoff AX5xxx.xml", T.AX5.format(vendor=T.VENDOR.format(fv=4)))
    T._write(m / "Beckhoff AX5xxx" / "Beckhoff AX5206-xxxx-0216 Dict.xml", T.AX5_DICT)
    T._write(m / "Beckhoff AX5xxx" / "Beckhoff AX5xxx-xxxx-xxxx Diag.xml", T.AX5_DIAG)
    for rev in ("0106", "0107"):
        T._write(m / f"Beckhoff AX8yxx-zzzz-{rev}.xml", T.AX8_DEV.format(vendor=T.VENDOR.format(fv=19), rev=rev))
        T._write(m / "Beckhoff AX8yxx" / f"Beckhoff AX8yxx-zzzz-{rev} Modules.xml",
                 T.AX8_MODULES.format(vendor=T.VENDOR.format(fv=19), rev=rev))
    con = sqlite3.connect(kb_copy / "index" / "manuals.db")          # the manuals index names the PDFs by path:
    with con:                                                         # the copy's, as if it had been built here
        for doc_id, path in con.execute("SELECT id, path FROM doc").fetchall():
            rel = Path(path).as_posix().split("/manuals/", 1)[1]
            con.execute("UPDATE doc SET path = ? WHERE id = ?", (str(kb_copy / "manuals" / rel), doc_id))
    con.close()
    r = E.build(kb_copy / "index" / "esi.db", kb_copy, [], twincat=False, log=lambda _m: None)
    assert r["result"] == "built", r
    kn = K.Knowledge(home=tmp_path / "home", location=str(kb_copy))
    kn.add_project(str(project_dir))
    return kb_copy


def _digest(d: Path) -> dict[str, str]:
    return {str(f.relative_to(d)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(d.rglob("*")) if f.is_file() and not f.name.endswith("-shm")
            and not (f.name.endswith("-wal") and f.stat().st_size == 0)}


def _run(KV, kb: Path, out: Path, *extra: str) -> tuple[int, dict]:
    rc = KV.main(["--kb", str(kb), "--out", str(out), "--mode", "full", *extra])
    return rc, json.loads((out / "results.json").read_text(encoding="utf-8"))


def _checks(res: dict) -> dict[str, dict]:
    return {c["id"]: c for c in res["checks"]}


def test_every_check_runs_and_passes_on_correct_indexes(KV, full_kb, tmp_path, monkeypatch):
    monkeypatch.setattr(KV, "GOLDEN", _golden(KV))
    before = _digest(full_kb)
    rc, res = _run(KV, full_kb, tmp_path / "out")
    fails = {c["id"]: c["failures"] for c in res["checks"] if c["failures"]}
    assert rc == 0 and not fails, fails
    assert _digest(full_kb) == before and res["read_only"]["unchanged"] is True
    assert res["not_tested"] == [] and res["interrupted"] is False
    by = _checks(res)
    for cid in ("I0", "I1", "I2", "I3", "I4", "I5", "I6", "I7", "I7b", "I8", "I9", "I10", "I11", "M0", "M1", "M2",
                "M3", "M4", "M5", "E0", "E1", "E2", "E3", "E4", "E5", "E6", "E7", "P0", "P1", "P2", "I6b", "E8", "S0"):
        assert by[cid]["counts"]["pass"] > 0, (cid, by[cid])
    assert by["I2"]["counts"]["pass"] == 9 and by["M3"]["counts"]["pass"] == 4      # every page in full mode
    assert by["I11"]["counts"]["pass"] >= 5                    # ADSREAD, MC_MoveAbsolute ×2, MEMCPY, 0x745 …
    assert by["I12"]["counts"]["pass"] == 2                    # the fixture's table row 1861 / 0x745, both forms
    assert by["P1"]["counts"]["pass"] == res["indexes"]["projects"]["units"]
    report = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "**Result: no failure**" in report and "Read-only: verified" in report
    assert (tmp_path / "out" / "failures.csv").read_text(encoding="utf-8-sig").startswith("area,check,title,status")


def test_wrong_answers_are_found(KV, full_kb, tmp_path, monkeypatch):
    """A page the title search cannot find (its words gone from the search table), a manual page the phrase search
    cannot find, a CoE object list shorter than its count, a known answer that is wrong: each a failure, exit 1."""
    golden = _golden(KV)
    golden.append({"id": "decl-ADSWRITE-wrong", "kind": "declaration", "q": "ADSWRITE",
                   "expect": {"kind": "Function", "return_type": "BOOL"}})
    monkeypatch.setattr(KV, "GOLDEN", golden)
    con = sqlite3.connect(full_kb / "index" / "infosys-1033.db")
    with con:
        row = con.execute("SELECT rowid, url, title, text, code, words FROM pages WHERE title = 'T_AmsNetID'").fetchone()
        con.execute("INSERT INTO pages_fts(pages_fts, rowid, url, title, text, code, words) "
                    "VALUES ('delete', ?, ?, ?, ?, ?, ?)", row)
    con.close()
    con = sqlite3.connect(full_kb / "index" / "manuals.db")
    with con:
        con.execute("DELETE FROM fts WHERE page = 2")
    con.close()
    con = sqlite3.connect(full_kb / "index" / "esi.db")
    with con:
        con.execute("UPDATE devices SET objects = objects + 1 WHERE type = 'EL3356-0010'")
    con.close()
    rc, res = _run(KV, full_kb, tmp_path / "out")
    by = _checks(res)
    assert rc == 1
    assert [f["subject"] for f in by["I6"]["failures"]] == [
        "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/31059723.html"], by["I6"]
    assert [f["subject"] for f in by["M4"]["failures"]] == ["CX9020.pdf p2"], by["M4"]
    assert any("CoE objects listed" in f["detail"] for f in by["E2"]["failures"]), by["E2"]
    wrong = [f for f in by["I11"]["failures"] if f["subject"] == "decl-ADSWRITE-wrong"]
    assert wrong and "kind 'FunctionBlock', expected 'Function'" in wrong[0]["detail"], by["I11"]["failures"]


def test_an_exception_of_the_plugin_is_a_failure_and_the_run_goes_on(KV, kb_copy, tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("search exploded")
    monkeypatch.setattr(KV, "GOLDEN", [])
    monkeypatch.setattr(KV.INF, "search_index", boom)
    rc, res = _run(KV, kb_copy, tmp_path / "out", "--only", "infosys,manuals")
    by = _checks(res)
    assert rc == 1 and by["I6"]["counts"]["fail"] == 9
    assert "raised RuntimeError: search exploded" in by["I6"]["failures"][0]["detail"]
    assert by["I7"]["counts"]["pass"] > 0 and by["M3"]["counts"]["pass"] == 4        # the rest still ran
    assert res["areas"] == ["infosys", "manuals"]


def test_missing_indexes_are_named_not_failed(KV, kb_copy, tmp_path, monkeypatch):
    monkeypatch.setattr(KV, "GOLDEN", _golden(KV))
    rc, res = _run(KV, kb_copy, tmp_path / "out", "--only", "esi,projects")
    assert rc == 0 and res["not_tested"] == ["esi", "projects"], res["not_tested"]
    assert _checks(res)["E0"]["warnings"][0]["detail"].startswith("no ESI index")


def test_quick_mode_samples_with_a_seed(KV, kb_copy, tmp_path, monkeypatch):
    monkeypatch.setattr(KV, "GOLDEN", [])
    rc = KV.main(["--kb", str(kb_copy), "--out", str(tmp_path / "a"), "--only", "infosys", "--sample", "1"])
    res = json.loads((tmp_path / "a" / "results.json").read_text(encoding="utf-8"))
    assert rc == 0 and res["indexes"]["infosys"]["pages_tested"] == 6                 # one per book, 6 books
    KV.main(["--kb", str(kb_copy), "--out", str(tmp_path / "b"), "--only", "infosys", "--sample", "1"])
    res2 = json.loads((tmp_path / "b" / "results.json").read_text(encoding="utf-8"))
    assert [c["counts"] for c in res["checks"]] == [c["counts"] for c in res2["checks"]]
    v = KV.Verifier(KV.parse_args(["--seed", "7"]), kb_copy, "--kb", tmp_path / "c")
    w = KV.Verifier(KV.parse_args(["--seed", "7"]), kb_copy, "--kb", tmp_path / "c")
    assert v.pick(list(range(100)), 5, "book") == w.pick(list(range(100)), 5, "book") != list(range(5))
    assert v.pick(list(range(100)), 5, "book") != v.pick(list(range(100)), 5, "other book")


def test_refusals_before_anything_runs(KV, kb_copy, tmp_path, capsys):
    assert KV.main(["--kb", str(tmp_path / "nowhere")]) == 2
    assert KV.main(["--kb", str(kb_copy), "--out", str(kb_copy / "reports")]) == 2       # never into the knowledge folder
    assert not (kb_copy / "reports").exists()
    assert KV.main(["--kb", str(kb_copy), "--only", "infosys,ads"]) == 2
    with pytest.raises(SystemExit):
        KV.main(["--kb", str(kb_copy), "--top", "60"])
    err = capsys.readouterr().err
    assert "not found" in err and "inside the knowledge folder" in err and "unknown area" in err


def test_an_interrupted_run_writes_its_partial_report(KV, kb_copy, tmp_path, monkeypatch):
    def stop(_v):
        raise KeyboardInterrupt
    monkeypatch.setattr(KV, "GOLDEN", [])
    monkeypatch.setitem(KV.AREAS, "manuals", stop)
    rc, res = _run(KV, kb_copy, tmp_path / "out", "--only", "infosys,manuals")
    assert res["interrupted"] is True and res["areas"] == ["infosys"] and _checks(res)["I2"]["counts"]["pass"] == 9
    assert rc == 130 and "INTERRUPTED" in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")


def test_helpers(KV):
    assert list(KV.code_rows("| 0x745 | 1861 | x |\n| 1862 | 0x746 |\n| 0x10 | 17 |")) == [(1861, "745"), (1862, "746")]
    assert KV.title_key("Example:") == KV.title_key("example") == "example"
    df = {"rare": 1, "word": 50, "other": 40}.get
    ph = KV.distinctive_phrases("aa bb rare cc dd word ee ff other gg hh", lambda t: df(t, 100), n=2)
    assert ph == ["aa bb rare cc dd", "ee ff other gg hh"]
    assert KV.rev_int("#x00100000") == 0x100000 and KV.rev_int(None) is None and KV.rev_arg(16) == "#x00000010"
