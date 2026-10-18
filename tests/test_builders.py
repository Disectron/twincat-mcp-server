"""The knowledge-folder builders shipped with the twincat-docs skill (skills/twincat-docs/builders): the Python 3.12
minimum, the atomic rebuild and its real state, the filter before LIMIT, ambiguous names refused, links.

On a Python older than 3.12 only the refusal is tested (CI runs 3.10 and 3.12, so both paths run on every push)."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
B = ROOT / "skills" / "twincat-docs" / "builders"
KBD = ROOT / "skills" / "twincat-docs" / "kb_docs.py"
NEW_ENOUGH = sys.version_info >= (3, 12)
needs312 = pytest.mark.skipif(not NEW_ENOUGH, reason="the builders need Python 3.12+ (refusal tested below)")


def run(script: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("TCMCP_KNOWLEDGE_DIR", "TCMCP_HOME")}
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run([sys.executable, str(B / script), *args], capture_output=True, timeout=timeout, env=env,
                          encoding="utf-8", errors="replace")


def kbdocs(kb: Path, *args: str) -> dict:
    p = subprocess.run([sys.executable, str(KBD), *args, "--kb", str(kb)], capture_output=True, timeout=60)
    return json.loads(p.stdout.decode("utf-8"))


def _fixture_pages() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("mkfix", ROOT / "tests" / "fixtures" / "kb" / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PAGES


def mshc(path: Path, pages: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, html in pages.items():
            z.writestr(name, html)
    return path


@pytest.fixture
def kb(tmp_path):
    k = tmp_path / "kb"
    (k / "helpviewer").mkdir(parents=True)
    return k


def build(kb: Path, pkg: Path, *extra: str) -> subprocess.CompletedProcess:
    return run("infosys_local.py", "--kb", str(kb), "index", "--mshc", str(pkg), *extra)


def state(kb: Path) -> dict:
    p = run("infosys_local.py", "--kb", str(kb), "stats", "--json")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# ---------------------------------------------------------------------------------------------- 1. minimum Python
@pytest.mark.parametrize("script", ["infosys_local.py", "manuals_db.py"])
def test_minimum_python_is_enforced_with_a_message(script):
    p = run(script, "stats")
    if NEW_ENOUGH:
        assert "needs Python 3.12" not in p.stderr
    else:
        assert p.returncode == 1 and f"{script} needs Python 3.12 or newer" in p.stderr and "Traceback" not in p.stderr


def test_builders_compile_on_this_python():
    """The version check only helps if an older Python can compile the file to reach it."""
    for f in ("infosys_local.py", "manuals_db.py"):
        compile((B / f).read_text(encoding="utf-8"), f, "exec")


# ---------------------------------------------------------------------------------------------- 2. atomic rebuild + state
@needs312
def test_build_state_up_to_date_and_old_format(kb, tmp_path):
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    p = build(kb, pkg)
    assert p.returncode == 0, p.stderr
    s = state(kb)
    assert s["state"] == "complete" and s["pages"] == 8 and s["links_resolved"] == 3 and "rebuild" not in s
    assert "up to date" in build(kb, pkg).stdout
    # a version-1 index (no schema, no link table) is never "up to date": index upgrades it
    db = kb / "helpviewer" / "infosys_en.db"
    c = sqlite3.connect(db)
    c.execute("DELETE FROM meta WHERE key = 'schema'")
    c.execute("DROP TABLE link")
    c.commit()
    c.close()
    assert state(kb)["state"] == "old_format"
    assert kbdocs(kb, "docs_page", "ref=tcplclib_tc2_system/30866572")["links_note"].startswith("this index has no link table")
    p = build(kb, pkg)
    assert "older index format" in p.stdout and state(kb)["state"] == "complete"
    # the package changed since the build → outdated, and index rebuilds
    time.sleep(1.1)
    mshc(pkg, {**_fixture_pages(), "tcplclib_new/1033/1.html": "<html><article><h1>New</h1></article></html>"})
    s = state(kb)
    assert s["state"] == "outdated" and "package changed" in s["message"]
    assert kbdocs(kb, "docs_books")["source"]["state"] == "outdated"
    assert "the Help Viewer package changed" in build(kb, pkg).stdout and state(kb)["pages"] == 9


@needs312
def test_interrupted_force_rebuild_keeps_the_complete_index_and_is_reported(kb, tmp_path):
    """Finding 2: an interrupted `index --force` left a half index that the next run called 'up to date'."""
    pages = _fixture_pages()
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", pages)
    assert build(kb, pkg).returncode == 0
    db = kb / "helpviewer" / "infosys_en.db"
    big = dict(pages)
    filler = "<p>" + "lorem ipsum dolor sit amet " * 400 + "</p>"
    for i in range(12000):
        big[f"tcplclib_bulk/1033/{900000 + i}.html"] = f"<html><article><h1>Bulk {i}</h1>{filler}</article></html>"
    time.sleep(1.1)
    mshc(pkg, big)
    env = {k: v for k, v in os.environ.items() if k not in ("TCMCP_KNOWLEDGE_DIR", "TCMCP_HOME")}
    proc = subprocess.Popen([sys.executable, str(B / "infosys_local.py"), "--kb", str(kb), "index", "--mshc", str(pkg), "--force"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    building = db.with_name(db.name + ".building")
    t0 = time.time()
    while time.time() - t0 < 60:
        st = state(kb)
        if st.get("rebuild", {}).get("pages_done", 0) >= 2000:
            break
        time.sleep(0.2)
    running = state(kb)
    assert running["rebuild"]["state"] == "running" and running["state"] == "outdated"
    assert "RUNNING" in running["message"] and running["pages"] == 8          # the complete index is still the one in use
    # a second build while one runs is refused
    second = build(kb, pkg)
    assert second.returncode == 1 and "another index build is running" in second.stderr
    proc.kill()
    proc.wait(30)
    assert building.is_file()
    # the reader keeps answering from the complete index; the stopped rebuild shows as interrupted once its heartbeat
    # is old (simulated: age the heartbeat and the lock)
    c = sqlite3.connect(building)
    c.execute("UPDATE meta SET value = ? WHERE key = 'heartbeat'", (f"{time.time() - 600:.0f}",))
    c.commit()
    c.close()
    lock = db.with_name(db.name + ".lock")
    if lock.exists():
        os.utime(lock, (time.time() - 600, time.time() - 600))
    st = state(kb)
    assert st["rebuild"]["state"] == "interrupted" and st["pages"] == 8 and "INTERRUPTED" in st["message"]
    assert kbdocs(kb, "status")["infosys_state"]["rebuild"]["state"] == "interrupted"
    assert kbdocs(kb, "docs_declaration", "symbol=ADSWRITE")["ref"] == "tcplclib_tc2_system/30866572"
    # the next plain `index` builds again — never "up to date" over a half index
    p = run("infosys_local.py", "--kb", str(kb), "index", "--mshc", str(pkg), timeout=600)
    assert p.returncode == 0 and "up to date" not in p.stdout, p.stderr
    st = state(kb)
    assert st["state"] == "complete" and st["pages"] == len(big) and "rebuild" not in st and not building.exists()


@needs312
def test_finished_rebuild_waiting_to_replace_is_swapped_in(kb, tmp_path):
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    assert build(kb, pkg).returncode == 0
    db = kb / "helpviewer" / "infosys_en.db"
    building = db.with_name(db.name + ".building")
    import shutil
    shutil.copy(db, building)              # a complete rebuild that could not replace the live file (held open)
    assert state(kb)["rebuild"]["state"] == "waiting_to_replace"
    p = build(kb, pkg)
    assert "replaced" in p.stdout and not building.exists() and state(kb)["state"] == "complete"


# ---------------------------------------------------------------------------------------------- 3. filter before LIMIT
@needs312
def test_book_filter_runs_before_the_limit(kb, tmp_path):
    pages = {f"aaa_other/1033/{100 + i}.html": f"<html><article><h1>Widget {i}</h1><p>Widget Widget</p></article></html>"
             for i in range(30)}
    pages["tcplclib_x/1033/7.html"] = "<html><article><h1>Gadget</h1><p>uses a Widget once</p></article></html>"
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", pages)
    assert build(kb, pkg).returncode == 0
    p = run("infosys_local.py", "--kb", str(kb), "search", "Widget", "--book", "tcplclib_*", "--limit", "1")
    assert "tcplclib_x/7" in p.stdout and "no hits" not in p.stdout
    p = run("infosys_local.py", "--kb", str(kb), "search", "Widget", "--limit", "2")
    assert "(2 of 31 hits" in p.stdout


# ---------------------------------------------------------------------------------------------- 4. ambiguity + 6. links
@needs312
def test_show_and_decl_refuse_ambiguous_names_and_show_links(kb, tmp_path):
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    assert build(kb, pkg).returncode == 0
    for cmd in ("show", "decl"):
        p = run("infosys_local.py", "--kb", str(kb), cmd, "MC_MoveAbsolute")
        assert p.returncode == 1 and "2 pages match 'MC_MoveAbsolute' — nothing shown" in p.stderr
        assert "tcplclib_tc2_mc2/70094731" in p.stderr and "tcplclib_tc3_drivemotioncontrol/8280601227" in p.stderr
        assert p.stdout == ""
    p = run("infosys_local.py", "--kb", str(kb), "decl", "MC_MoveAbsolute", "--book", "tcplclib_tc3_*")
    assert p.returncode == 0 and "Also in: tcplclib_tc2_mc2/70094731" in p.stdout
    p = run("infosys_local.py", "--kb", str(kb), "show", "tcplclib_tc2_system/30866572")
    assert "Links: ADSREAD → tcplclib_tc2_system/30866571; T_AmsNetID → tcplclib_tc2_system/31059723" in p.stdout
    p = run("infosys_local.py", "--kb", str(kb), "show", "ms-xhelp:///?Id=beckhoff-f599-48a7-92f2-48aa9133ca8a")
    assert p.stdout.startswith("T_AmsNetID | tcplclib_tc2_system/31059723")
    c = sqlite3.connect(kb / "helpviewer" / "infosys_en.db")
    rows = c.execute("SELECT text, href, target_book, target_pid FROM link WHERE pid = '30866572' ORDER BY n").fetchall()
    assert rows == [("ADSREAD", "30866571.html", "tcplclib_tc2_system", "30866571"),
                    ("T_AmsNetID", "../tcplclib_tc2_system/1033/31059723.html", "tcplclib_tc2_system", "31059723"),
                    ("Beckhoff", "https://www.beckhoff.com/", None, None), ("top", "#top", None, None),
                    ("gone", "ms-xhelp:///?Id=beckhoff-not-in-package", None, None)]
    # the reader hands the links out as refs docs_page opens
    r = kbdocs(kb, "docs_page", "ref=tcplclib_tc2_system/30866572")
    assert r["links"] == [{"text": "ADSREAD", "ref": "tcplclib_tc2_system/30866571"},
                          {"text": "T_AmsNetID", "ref": "tcplclib_tc2_system/31059723"}]
    r = kbdocs(kb, "docs_declaration", "symbol=ADSREAD", "book=tcplclib_tc2_system")
    assert r["links"] == [{"text": "T_AmsNetID", "ref": "tcplclib_tc2_system/31059723"}]


# ---------------------------------------------------------------------------------------------- manuals
@needs312
def test_manuals_update_lock_state_and_doc_filter(tmp_path):
    pytest.importorskip("reportlab")
    pytest.importorskip("pypdf")
    spec = importlib.util.spec_from_file_location("mkfix", ROOT / "tests" / "fixtures" / "kb" / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    kb = tmp_path / "kb"
    (kb / "manuals" / "IPC").mkdir(parents=True)
    mod.build_pdf(kb / "manuals" / "IPC" / "CX9020.pdf")
    p = run("manuals_db.py", "update", "--kb", str(kb))
    assert p.returncode == 0 and "1 added" in p.stdout, p.stderr
    st = json.loads(run("manuals_db.py", "stats", "--json", "--kb", str(kb)).stdout)
    assert st["state"] == "up_to_date" and st["manuals"] == 1 and st["last_result"].startswith("1 added")
    # the reader computes the same state
    assert kbdocs(kb, "status")["manuals_state"]["state"] == "up_to_date"
    # a second update while one runs is refused
    lock = kb / "manualsdb" / "manuals.db.lock"
    lock.write_text("{}", encoding="utf-8")
    p = run("manuals_db.py", "update", "--kb", str(kb))
    assert p.returncode == 1 and "another update is running" in p.stderr
    lock.unlink()
    # a new PDF is reported before anyone runs update
    mod.build_pdf(kb / "manuals" / "IPC" / "CX9021.pdf")
    st = json.loads(run("manuals_db.py", "stats", "--json", "--kb", str(kb)).stdout)
    assert st["state"] == "pending" and st["not_indexed"] == ["CX9021.pdf"] and "run update" in st["message"]
    assert run("manuals_db.py", "update", "--kb", str(kb)).returncode == 0
    p = run("manuals_db.py", "search", "watchdog", "--doc", "CX9021", "--kb", str(kb))
    assert "CX9021.pdf | p4" in p.stdout and "CX9020.pdf" not in p.stdout
    p = run("manuals_db.py", "search", "watchdog", "--doc", "nope", "--kb", str(kb))
    assert p.returncode == 1 and "no indexed manual matches 'nope'" in p.stderr


def _manuals_module():
    spec = importlib.util.spec_from_file_location("manuals_db_mod", B / "manuals_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_n37_running_headers_and_wrapped_headings(tmp_path):
    """N37: running headers / footers in the page text, glued to a heading ('Calibration and compensation4.4
    Calibration'); a heading wrapped over two lines became a level-1 chapter ('capacity')."""
    m = _manuals_module()
    body = lambda i: "\n".join(f"line {k} of page {i}: value {k * i + 7}" for k in range(8))  # noqa: E731
    pages = [f"Calibration and compensation\n{body(i)}\nEL3356-0010 Version: 2.4 {i + 40}" for i in range(8)]
    pages[3] = f"Calibration and compensation4.4 Calibration\n{body(3)}\nEL3356-0010 Version: 2.4 43"
    pages[4] = f"NOTICE\n{body(4)}\nEL3356-0010 Version: 2.4 44"
    pages[5] = f"NOTICE\n{body(5)}\nEL3356-0010 Version: 2.4 45"
    pages[6] = f"NOTICE\nTable 3 of 9\n{body(6)}\nEL3356-0010 Version: 2.4 46"
    pages[7] = f"Table 5 of 9\n{body(7)}\nEL3356-0010 Version: 2.4 47"
    pages.append(f"Table 8 of 9\n{body(8)}\nEL3356-0010 Version: 2.4 48")
    out, heads = m._strip_running(pages, set())
    assert heads == ["calibration and compensation", "el#-# version: #.# #"], heads
    assert out[0].split("\n")[0] == "" and out[0].split("\n")[-1] == "" and "line 0 of page 0" in out[0]
    assert out[3].split("\n")[0] == "4.4 Calibration"                 # the glued header cut off, the heading kept
    assert out[4].startswith("NOTICE") and out[6].startswith("NOTICE")  # signal words are never a running header
    assert "Table 5 of 9" in out[7]                                     # numbers that are no page number: kept
    # a page-number-only footer and a chapter title on two pages
    out, heads = m._strip_running(["Commissioning\nx\n1", "Commissioning\ny\n2", "Intro\nz\n3"], {"commissioning"})
    assert out == ["\nx\n", "\ny\n", "Intro\nz\n"] and heads == ["#", "commissioning"]
    toc = [(1, "4 Commissioning", 40), (2, "4.10 Notes on the measurement of the bridge", 60), (1, "capacity", 60),
           (2, "4.11 Filter (only", 61), (1, "only)", 61), (2, "4.12 Hinweise zur Brücken-", 62), (1, "kapazität", 62),
           (1, "Appendix", 90), (2, "4.13 Settings and", 63), (1, "Parameterization", 63)]
    assert m._mend_toc(toc) == [(1, "4 Commissioning", 40), (2, "4.10 Notes on the measurement of the bridge capacity", 60),
                                (2, "4.11 Filter (only only)", 61), (2, "4.12 Hinweise zur Brückenkapazität", 62),
                                (1, "Appendix", 90), (2, "4.13 Settings and Parameterization", 63)]
    unnumbered = [(1, "Intro", 1), (1, "details", 1)]
    assert m._mend_toc(unnumbered) == unnumbered                       # only manuals that number their chapters


def test_n37_pdf_running_header_removed_and_old_text_format_reread(tmp_path):
    pytest.importorskip("reportlab")
    pytest.importorskip("pypdf")
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    kb = tmp_path / "kb"
    (kb / "manuals").mkdir(parents=True)
    pdf = kb / "manuals" / "EL3356.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    for i in range(8):
        c.bookmarkPage(f"p{i}")
        c.addOutlineEntry(f"4.{i + 1} Topic {i}", f"p{i}", level=0)
        c.setFont("Helvetica", 9)
        c.drawString(400, 810, "Calibration and compensation")
        c.drawString(72, 30, f"EL3356-0010 Version: 2.4 {i + 1}")
        c.setFont("Helvetica", 10)
        for k in range(8):
            c.drawString(72, 720 - 14 * k, f"strain gauge bridge {i} measurement step {k * 3 + i * 7}")
        c.showPage()
    c.save()
    assert run("manuals_db.py", "update", "--kb", str(kb)).returncode == 0
    db = kb / "manualsdb" / "manuals.db"
    con = sqlite3.connect(db)
    texts = [r[0] for r in con.execute("SELECT text FROM page ORDER BY page")]
    assert texts and all("Calibration and compensation" not in t and "Version" not in t for t in texts), texts
    assert all("strain gauge bridge" in t for t in texts)
    assert con.execute("SELECT value FROM meta WHERE key='text_format'").fetchone()[0] == "2"
    # an index written by the old builder (no text_format) is outdated, and update reads every manual again
    con.execute("DELETE FROM meta WHERE key='text_format'")
    con.commit()
    con.close()
    st = json.loads(run("manuals_db.py", "stats", "--json", "--kb", str(kb)).stdout)
    assert st["state"] == "outdated" and "page text format 1" in st["message"]
    assert kbdocs(kb, "status")["manuals_state"]["state"] == "outdated"
    p = run("manuals_db.py", "update", "--kb", str(kb))
    assert "every manual is read again" in p.stdout and "1 updated" in p.stdout, p.stdout
    st = json.loads(run("manuals_db.py", "stats", "--json", "--kb", str(kb)).stdout)
    assert st["state"] == "up_to_date"


def test_n37_font_headings_wrapped_over_two_lines_are_one_entry(tmp_path):
    """PDFs without bookmarks (PyMuPDF font headings): the second line of a wrapped heading was a level-1 chapter."""
    pytest.importorskip("reportlab")
    pytest.importorskip("pymupdf")
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    pdf = tmp_path / "fonts.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    for i, (title, tail) in enumerate((("Notes on the measurement of the bridge", "capacity"), ("Settings", None))):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(72, 760, f"4.{i + 10}")
        c.drawString(110, 760, title)
        if tail:
            c.drawString(110, 740, tail)
        c.setFont("Helvetica", 10)
        for k in range(10):
            c.drawString(72, 700 - 14 * k, f"body text {i} {k * 5 + i * 3} words")
        c.showPage()
    c.save()
    m = _manuals_module()
    backend, _n, toc, _texts, _t = m._open_pdf(pdf)
    assert backend == "pymupdf+font-headings"
    assert toc == [(2, "4.10 Notes on the measurement of the bridge capacity", 1), (2, "4.11 Settings", 2)], toc
