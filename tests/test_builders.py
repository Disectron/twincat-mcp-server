"""The knowledge-folder builders shipped with the twincat-docs skill (skills/twincat-docs/builders): the Python 3.12
minimum, the atomic rebuild and its real state (infosys_local.py builds the plugin's one Infosys index with the plugin's
extractor, twincat_mcp.infosys_build), the permanent layout (index\\), the filter before LIMIT, ambiguous names
refused, links.

Everything runs on Python 3.12 (the only version the project targets); the refusal on an older Python is tested by
running each script with sys.version_info set to 3.11."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import signal
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
    k.mkdir(parents=True)
    return k


def live(kb: Path) -> Path:
    return kb / "index" / "infosys-1033.db"


def build(kb: Path, pkg: Path, *extra: str) -> subprocess.CompletedProcess:
    return run("infosys_local.py", "--kb", str(kb), "index", "--mshc", str(pkg), *extra)


def state(kb: Path) -> dict:
    p = run("infosys_local.py", "--kb", str(kb), "stats", "--json")
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


# ---------------------------------------------------------------------------------------------- 1. minimum Python
def _as_python_311(path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a script the way Python 3.11 would reach its version check: sys.version_info reads 3.11."""
    code = ("import sys; sys.version_info = (3, 11, 9, 'final', 0); "
            f"sys.argv = [{str(path)!r}, *{list(args)!r}]; "
            f"exec(compile(open({str(path)!r}, encoding='utf-8').read(), {str(path)!r}, 'exec'), "
            f"{{'__name__': '__main__', '__file__': {str(path)!r}}})")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", errors="replace", timeout=60)


@pytest.mark.parametrize("script", ["infosys_local.py", "manuals_db.py", "esi_db.py", "errors_db.py"])
def test_minimum_python_is_enforced_with_a_message(script):
    assert "needs Python 3.12" not in run(script, "stats").stderr                 # this Python (3.12+) runs it
    p = _as_python_311(B / script, "stats")
    assert p.returncode == 1 and f"{script} needs Python 3.12 or newer" in p.stderr and "Traceback" not in p.stderr


def test_kb_docs_launcher_and_server_refuse_an_older_python_with_a_message():
    p = _as_python_311(KBD, "status")
    assert p.returncode == 1 and "kb_docs.py needs Python 3.12 or newer" in p.stderr and "Traceback" not in p.stderr
    p = _as_python_311(ROOT / "scripts" / "launch.py", "--src", str(ROOT), "--no-run")
    assert p.returncode == 3 and "3.12+ required" in p.stderr and "Traceback" not in p.stderr
    code = ("import sys, runpy; sys.version_info = (3, 11, 9, 'final', 0); sys.argv = ['twincat_mcp']; "
            "runpy.run_module('twincat_mcp', run_name='__main__')")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", errors="replace", timeout=60,
                       cwd=str(ROOT))
    assert p.returncode == 1 and "twincat-mcp needs Python 3.12 or newer" in p.stderr and "Traceback" not in p.stderr


def test_builders_compile_on_this_python():
    """The version check only helps if an older Python can compile the file to reach it (no 3.12-only syntax above
    the check: checked with the 3.8 grammar)."""
    import ast
    for f in (B / "infosys_local.py", B / "manuals_db.py", B / "esi_db.py", B / "errors_db.py", KBD,
              ROOT / "scripts" / "launch.py",
              ROOT / "twincat_mcp" / "__main__.py"):
        ast.parse(f.read_text(encoding="utf-8"), str(f), feature_version=(3, 8))


# ---------------------------------------------------------------------------------------------- 2. atomic rebuild + state
def test_build_state_up_to_date_rebuild_needed_and_outdated(kb, tmp_path):
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    p = build(kb, pkg)
    assert p.returncode == 0, p.stderr
    s = state(kb)
    assert s["state"] == "complete" and s["pages"] == 9 and "rebuild" not in s, s
    assert s["links"] == 2 and s["links_resolved"] == 1          # ms-xhelp links (relative ones: resolved while parsed)
    assert "up to date" in build(kb, pkg).stdout
    # an index of an older format is used as it is and said to need a rebuild — never rebuilt by a reader
    c = sqlite3.connect(live(kb))
    c.execute("UPDATE meta SET value = '3' WHERE key = 'indexer_version'")
    c.commit()
    c.close()
    s = state(kb)
    assert s["state"] == "rebuild_needed" and "format 3" in s["message"] and "kb_rebuild" in s["message"]
    src = kbdocs(kb, "docs_books")["source"]
    assert src["state"] == "rebuild_needed" and "REBUILD" in src["state_note"]
    assert kbdocs(kb, "docs_page", "ref=tcplclib_tc2_system/30866572")["title"] == "ADSWRITE"     # still answers
    p = build(kb, pkg)
    assert "format 3, this plugin writes" in p.stdout and state(kb)["state"] == "complete"
    # the package changed since the build → outdated, and index rebuilds
    time.sleep(1.1)
    mshc(pkg, {**_fixture_pages(), "tcplclib_new/1033/1.html": "<html><article><h1>New</h1></article></html>"})
    s = state(kb)
    assert s["state"] == "outdated" and "package changed" in s["message"]
    assert kbdocs(kb, "docs_books")["source"]["state"] == "outdated"
    assert "the Help Viewer package changed" in build(kb, pkg).stdout and state(kb)["pages"] == 10


def _wait_progress(lock: Path, proc: subprocess.Popen, timeout: float = 120) -> dict:
    """The build's own progress (its lock file), read directly: stats in a subprocess could look too late."""
    t0 = time.time()
    while time.time() - t0 < timeout and proc.poll() is None:
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        if int(info.get("pages_done") or 0) > 0:
            return info
        time.sleep(0.02)
    return {}


def _hold(proc: subprocess.Popen) -> None:
    """Freeze a running build where it is (its lock's heartbeat then stays as it was): SIGSTOP, on Windows
    NtSuspendProcess — without it a fast runner finishes the build before the state is looked at."""
    if hasattr(signal, "SIGSTOP"):
        os.kill(proc.pid, signal.SIGSTOP)
    else:
        import ctypes
        assert ctypes.windll.ntdll.NtSuspendProcess(ctypes.c_void_p(int(proc._handle))) == 0


def test_interrupted_force_rebuild_keeps_the_complete_index_and_is_reported(kb, tmp_path):
    """Finding 2: an interrupted `index --force` left a half index that the next run called 'up to date'. The build
    writes index\\infosys-1033.db.building; the complete index stays in use; the next build resumes."""
    pages = _fixture_pages()
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", pages)
    assert build(kb, pkg).returncode == 0
    db = live(kb)
    big = dict(pages)
    filler = "<p>" + "lorem ipsum dolor sit amet " * 100 + "</p>"
    for i in range(4000):
        big[f"tcplclib_bulk/1033/{900000 + i}.html"] = f"<html><article><h1>Bulk {i}</h1>{filler}</article></html>"
    time.sleep(1.1)
    mshc(pkg, big)
    env = {k: v for k, v in os.environ.items() if k not in ("TCMCP_KNOWLEDGE_DIR", "TCMCP_HOME")}
    proc = subprocess.Popen([sys.executable, str(B / "infosys_local.py"), "--kb", str(kb), "index", "--mshc", str(pkg), "--force"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    building, lock = db.with_name(db.name + ".building"), db.with_name(db.name + ".lock")
    first = _wait_progress(lock, proc)
    assert first, "the rebuild never reported progress"
    _hold(proc)                              # its lock stays fresh for 120 s
    running = state(kb)
    assert running["rebuild"]["state"] == "running" and running["state"] == "outdated", running
    assert "RUNNING" in running["message"] and running["pages"] == 9          # the complete index is still the one in use
    # a second build while one runs is refused
    second = build(kb, pkg)
    assert second.returncode == 1 and "another build is running" in second.stderr
    proc.kill()
    proc.wait(30)
    assert building.is_file()
    # the reader keeps answering from the complete index; the stopped build shows as interrupted once its heartbeat
    # (the lock's modification time) is old — simulated
    if lock.exists():
        os.utime(lock, (time.time() - 600, time.time() - 600))
    st = state(kb)
    assert st["rebuild"]["state"] == "interrupted" and st["pages"] == 9 and "INTERRUPTED" in st["message"], st
    assert kbdocs(kb, "status")["infosys_state"]["rebuild"]["state"] == "interrupted"
    assert kbdocs(kb, "docs_declaration", "symbol=ADSWRITE")["ref"] == "tcplclib_tc2_system/30866572"
    # the next plain `index` builds again — resuming the interrupted build, never "up to date" over a half index
    p = run("infosys_local.py", "--kb", str(kb), "index", "--mshc", str(pkg), timeout=600)
    assert p.returncode == 0 and "up to date" not in p.stdout and "resuming" in p.stdout, (p.stdout, p.stderr)
    st = state(kb)
    assert st["state"] == "complete" and st["pages"] == len(big) and "rebuild" not in st and not building.exists()


def _as_building(db: Path, **meta: str) -> Path:
    """A copy of an index as the build's work file, with meta changed (a build left behind)."""
    import shutil
    building = db.with_name(db.name + ".building")
    shutil.copy(db, building)
    c = sqlite3.connect(building)
    c.executemany("INSERT OR REPLACE INTO meta VALUES (?, ?)", list(meta.items()))
    c.commit()
    c.close()
    return building


def test_an_interrupted_build_next_to_a_complete_index_is_neither_stuck_nor_nagging(kb, tmp_path):
    """Review: an interrupted `index --force` with the package unchanged said 'INTERRUPTED — the next build resumes it'
    forever, and `index` only printed 'up to date'. Now it is stale (the index in use is complete and current): not a
    problem for any reader; a plain build discards it, `index --force` resumes it."""
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    assert build(kb, pkg).returncode == 0
    db = live(kb)
    # every connection closed: on Windows an open one keeps the file, and the build could not swap it in (Windows CI
    # of 88a221c: "… infosys-1033.db is open in another program" — this test's own connection; Python ≥ 3.11
    # connections sit in a reference cycle, so dropping the last name does not close them)
    with contextlib.closing(sqlite3.connect(db)) as c:
        stamp = c.execute("SELECT value FROM meta WHERE key = 'source_stamp'").fetchone()[0]
    building = _as_building(db, state="building", target_stamp=stamp)
    with contextlib.closing(sqlite3.connect(building)) as c:
        c.execute("DELETE FROM meta WHERE key = 'source_stamp'")
        c.commit()
    st = state(kb)
    assert st["state"] == "complete" and st["rebuild"]["state"] == "stale", st
    assert "complete and current" in st["message"] and "kb_rebuild" not in st["message"]
    assert kbdocs(kb, "docs_books")["source"].get("state_note") is None            # answers carry no note
    p = build(kb, pkg, "--force")
    assert p.returncode == 0 and "resuming" in p.stdout and not building.exists(), p.stdout
    building = _as_building(db, state="building")
    p = build(kb, pkg)
    assert "discarded" in p.stdout and "up to date" in p.stdout and not building.exists(), p.stdout
    assert "rebuild" not in state(kb)


def test_a_finished_build_of_another_package_is_never_said_to_be_swapped_in(kb, tmp_path):
    """Review: a complete .building of another format or package read 'waiting to replace — swapped in at the next
    server start', which no start or build would ever do. It is stale, and the next build discards it."""
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    assert build(kb, pkg).returncode == 0
    building = _as_building(live(kb), source_stamp="content:another-package", package_file="x|1|1")
    st = state(kb)
    assert st["rebuild"]["state"] == "stale" and "another format or package" in st["message"], st
    building2 = _as_building(live(kb), indexer_version="3")
    assert state(kb)["rebuild"]["state"] == "stale"
    p = build(kb, pkg)
    assert "discarded" in p.stdout and not building.exists() and not building2.exists()


def test_a_reader_without_the_server_sees_a_replaced_package(kb, tmp_path):
    """Review: after the Help Viewer package was replaced (old file deleted, a newer one in the same folder) the skill
    and `stats` said complete while the server said outdated — two rules. One rule now: the newest package of the
    catalogs and of the folder the index was built from; the same file unchanged is the same package, anything else is
    decided by the content."""
    folder = tmp_path / "EN-US"
    folder.mkdir()
    old = mshc(folder / "BKINFOSYS3_VS_100_EN-US.1.mshc", _fixture_pages())
    assert build(kb, old).returncode == 0
    assert state(kb)["state"] == "complete"
    os.utime(old, (time.time() - 60, time.time() - 60))             # touched: the content decides — still the same
    assert state(kb)["state"] == "complete"
    old.unlink()
    time.sleep(1.1)
    mshc(folder / "BKINFOSYS3_VS_100_EN-US.2.mshc",
         {**_fixture_pages(), "tcplclib_new/1033/1.html": "<html><article><h1>New</h1></article></html>"})
    st = state(kb)
    assert st["state"] == "outdated" and "EN-US.2.mshc" in st["reason"], st
    assert kbdocs(kb, "docs_books")["source"]["state"] == "outdated"


def test_finished_rebuild_waiting_to_replace_is_swapped_in(kb, tmp_path):
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    assert build(kb, pkg).returncode == 0
    db = live(kb)
    building = db.with_name(db.name + ".building")
    import shutil
    shutil.copy(db, building)              # a complete rebuild that could not replace the live file (held open)
    assert state(kb)["rebuild"]["state"] == "waiting_to_replace"
    p = build(kb, pkg)
    assert "replaced" in p.stdout and not building.exists() and state(kb)["state"] == "complete"


def test_a_builder_copy_outside_the_plugin_says_so(tmp_path):
    """Version 3 / 5 run the plugin's own code (twincat_mcp, kb_docs.py beside them): a copy on its own — no plugin
    folder around it, nothing installed (-I -S) — stops with one line saying how to run it, not a traceback."""
    import shutil
    for n in ("infosys_local.py", "manuals_db.py", "esi_db.py", "errors_db.py"):
        d = tmp_path / n.split(".")[0] / "tools"
        d.mkdir(parents=True)
        shutil.copy(B / n, d / n)
        p = subprocess.run([sys.executable, "-I", "-S", str(d / n), "stats"], capture_output=True, text=True, timeout=60)
        assert p.returncode == 1 and "runs from the twincat plugin's folder" in p.stderr and "Traceback" not in p.stderr, p.stderr


def test_the_first_build_moves_an_old_layout_and_reports_the_second_index(kb, tmp_path):
    """The permanent layout (docs/KNOWLEDGE_BASE.md): an index in an old place is moved into index\\ once — a rename,
    never a copy — and the builder's old second index (helpviewer\\infosys_en.db) is reported, never deleted."""
    from twincat_mcp import kb_layout as L
    old = kb / "manualsdb" / "manuals.db"
    old.parent.mkdir(parents=True)
    sqlite3.connect(old).close()
    second = kb / "helpviewer" / "infosys_en.db"
    second.parent.mkdir(parents=True)
    second.write_bytes(b"x" * 2000)
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    p = build(kb, pkg)
    assert p.returncode == 0 and "layout:" in p.stdout, p.stdout
    assert (kb / "index" / "manuals.db").is_file() and not old.exists()
    obs = L.obsolete_files(kb)
    assert [Path(o["path"]).name for o in obs] == ["infosys_en.db"] and second.exists()


# ---------------------------------------------------------------------------------------------- 3. filter before LIMIT
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
def test_show_and_decl_refuse_ambiguous_names_and_show_links(kb, tmp_path):
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    assert build(kb, pkg).returncode == 0
    for cmd in ("show", "decl"):
        p = run("infosys_local.py", "--kb", str(kb), cmd, "MC_MoveAbsolute")
        assert p.returncode == 1 and "2 pages match" in p.stderr and "nothing was chosen" in p.stderr, p.stderr
        assert "tcplclib_tc2_mc2/70094731" in p.stderr and "tcplclib_tc3_drivemotioncontrol/8280601227" in p.stderr
        assert p.stdout == ""
    p = run("infosys_local.py", "--kb", str(kb), "decl", "MC_MoveAbsolute", "--book", "tcplclib_tc3_*")
    assert p.returncode == 0 and "Also in: tcplclib_tc2_mc2/70094731" in p.stdout
    p = run("infosys_local.py", "--kb", str(kb), "show", "tcplclib_tc2_system/30866572")
    assert "Links: ADSREAD → tcplclib_tc2_system/30866571; T_AmsNetID → tcplclib_tc2_system/31059723" in p.stdout
    p = run("infosys_local.py", "--kb", str(kb), "show", "ms-xhelp:///?Id=beckhoff-f599-48a7-92f2-48aa9133ca8a")
    assert p.stdout.startswith("T_AmsNetID | tcplclib_tc2_system/31059723")
    # links are stored as page addresses (rule 4): relative ones resolved while parsed, ms-xhelp ones after the
    # build; external links and anchors as they are; an id the package does not have stays as it is
    c = sqlite3.connect(live(kb))
    md = c.execute("SELECT markdown FROM pages WHERE page_id = '30866572'").fetchone()[0]
    C = "https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/"
    assert f"[ADSREAD]({C}30866571.html)" in md and f"[T_AmsNetID]({C}31059723.html)" in md
    assert "[Beckhoff](https://www.beckhoff.com/)" in md and "[top](#top)" in md
    assert "[gone](ms-xhelp:///?Id=beckhoff-not-in-package)" in md
    md = c.execute("SELECT markdown FROM pages WHERE page_id = '30866571'").fetchone()[0]
    assert f"[T_AmsNetID]({C}31059723.html)" in md and "ms-xhelp" not in md
    # the reader hands the links out as refs docs_page opens
    r = kbdocs(kb, "docs_page", "ref=tcplclib_tc2_system/30866572")
    assert r["links"] == [{"text": "ADSREAD", "ref": "tcplclib_tc2_system/30866571"},
                          {"text": "T_AmsNetID", "ref": "tcplclib_tc2_system/31059723"}]
    r = kbdocs(kb, "docs_declaration", "symbol=ADSREAD", "book=tcplclib_tc2_system")
    assert r["links"] == [{"text": "T_AmsNetID", "ref": "tcplclib_tc2_system/31059723"}]


# ---------------------------------------------------------------------------------------------- manuals
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
    assert st["state"] == "complete" and st["manuals"] == 1 and st["last_result"].startswith("1 added")
    assert Path(st["database"]) == kb / "index" / "manuals.db"
    # the reader computes the same state (one implementation)
    assert kbdocs(kb, "status")["manuals_state"]["state"] == "complete"
    # a complete index is left as it is: no copy, no swap
    db = kb / "index" / "manuals.db"
    before = (db.stat().st_ino, db.stat().st_mtime_ns)
    p = run("manuals_db.py", "update", "--kb", str(kb))
    assert p.returncode == 0 and p.stdout.startswith("up to date") and (db.stat().st_ino, db.stat().st_mtime_ns) == before
    # a second update while one runs is refused — "runs" is the operating-system lock held, not a file that exists:
    # an old lock file nobody holds blocks nothing
    from twincat_mcp import kb_lock
    lock = kb / "index" / "manuals.db.lock"
    lock.write_text('{"pid": 1, "started": "long ago"}', encoding="utf-8")
    os.utime(lock, (0, 0))
    assert kbdocs(kb, "status")["manuals_state"].get("updating") is None
    with kb_lock.IndexLock(lock) as lk:
        lk.update(files_done=3, files_total=9)
        assert kbdocs(kb, "status")["manuals_state"]["updating"]["state"] == "running"
        p = run("manuals_db.py", "update", "--kb", str(kb))
        assert p.returncode == 1 and "another update is running" in p.stderr, p.stderr
    assert lock.read_text() == "" and kbdocs(kb, "status")["manuals_state"].get("updating") is None
    # a new PDF is reported before anyone runs update
    mod.build_pdf(kb / "manuals" / "IPC" / "CX9021.pdf")
    st = json.loads(run("manuals_db.py", "stats", "--json", "--kb", str(kb)).stdout)
    assert st["state"] == "outdated" and st["not_indexed"] == ["CX9021.pdf"] and "manuals_db.py update" in st["message"]
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


@pytest.mark.parametrize("backend", ["pypdf", "pymupdf"])
def test_n37_text_at_size_zero_is_dropped_and_an_ocr_text_layer_is_kept(tmp_path, backend):
    """N37: under each heading Beckhoff PDFs carry the running header again at a vanishing size — not page text. Format 4
    dropped size 0 only; the real copy (EL3356-0010.pdf, the EtherCAT system manual) is drawn at 0.0028 pt and stayed
    (0.8.11 retest). Format 5: below 1 pt. Invisible text of a real size is page text: a scanned manual's OCR layer
    (render mode 3; PyMuPDF reports alpha 0) is the only text it has."""
    lib = pytest.importorskip(backend)
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    pdf = tmp_path / "n37.pdf"
    c = canvas.Canvas(str(pdf))
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 760, "4.2.2 Signal flow diagram")
    t = c.beginText(72, 760)
    t.setFont("Helvetica", 0)
    t.textLine("| Signal flow diagram")
    c.drawText(t)
    t = c.beginText(300, 760)
    t.setFont("Helvetica", 0.0028)                 # as Beckhoff draws it
    t.textLine("| Signal flow diagram")
    c.drawText(t)
    t = c.beginText(72, 730)
    t.setFont("Helvetica", 4.5)                    # the smallest real text of the four manuals
    t.textLine("footnote")
    c.drawText(t)
    t = c.beginText(72, 700)
    t.setFont("Helvetica", 11)
    t.setTextRenderMode(3)
    t.textLine("EL3356 OCR layer text")
    c.drawText(t)
    c.showPage()
    c.save()
    m = _manuals_module()
    page = lib.PdfReader(str(pdf)).pages[0] if backend == "pypdf" else lib.open(str(pdf))[0]
    spans, _h = (m._spans_pypdf if backend == "pypdf" else m._spans_pymupdf)(page)
    texts = [sp.text.strip() for sp in spans]
    assert texts == ["4.2.2 Signal flow diagram", "footnote", "EL3356 OCR layer text"], texts


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
    db = kb / "index" / "manuals.db"
    con = sqlite3.connect(db)
    texts = [r[0] for r in con.execute("SELECT text FROM page ORDER BY page")]
    assert texts and all("Calibration and compensation" not in t and "Version" not in t for t in texts), texts
    assert all("strain gauge bridge" in t for t in texts)
    assert con.execute("SELECT value FROM meta WHERE key='text_format'").fetchone()[0] == _manuals_module().TEXT_FORMAT
    # an index written by the old builder (no text_format) needs a rebuild — in use as it is until update runs
    con.execute("DELETE FROM meta WHERE key='text_format'")
    con.commit()
    con.close()
    st = json.loads(run("manuals_db.py", "stats", "--json", "--kb", str(kb)).stdout)
    assert st["state"] == "rebuild_needed" and "page text format 1" in st["message"] and "kb_rebuild" in st["message"]
    assert kbdocs(kb, "status")["manuals_state"]["state"] == "rebuild_needed"
    p = run("manuals_db.py", "update", "--kb", str(kb))
    assert "every manual is read again" in p.stdout and "1 updated" in p.stdout, p.stdout
    st = json.loads(run("manuals_db.py", "stats", "--json", "--kb", str(kb)).stdout)
    assert st["state"] == "complete" and not (kb / "index" / "manuals.db.building").exists()
    # review: a manual that fails during the re-read keeps its old rows — the format is not stamped, so the next
    # update reads every manual again instead of calling the old rows current
    con = sqlite3.connect(db)
    con.execute("DELETE FROM meta WHERE key='text_format'")
    con.commit()
    con.close()
    pdf.write_bytes(b"%PDF-1.4 broken")
    p = run("manuals_db.py", "update", "--kb", str(kb))
    assert "1 failed" in p.stdout, p.stdout
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='text_format'").fetchone() is None
    assert con.execute("SELECT COUNT(*) FROM page").fetchone()[0] == 8              # the old rows are still there
    con.close()


def test_n37_font_headings_wrapped_over_two_lines_are_one_entry(tmp_path):
    """PDFs without bookmarks (PyMuPDF font headings): the second line of a wrapped heading was a level-1 chapter."""
    pytest.importorskip("reportlab")
    pytest.importorskip("pymupdf", exc_type=ImportError)
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
    backend, _n, toc, _texts, _t, _running = m._open_pdf(pdf)
    assert backend == "pymupdf+font-headings"
    assert toc == [(2, "4.10 Notes on the measurement of the bridge capacity", 1), (2, "4.11 Settings", 2)], toc


def _beckhoff_like_pdf(path: Path, bookmarks: bool) -> None:
    """A manual laid out like the Beckhoff ones (EL3356 4.8.0): the running header is the current SECTION title
    (it changes), on some pages a section heading sits on the header's baseline; the footer alternates between
    '<page> Version: 4.8.0 EL3356-00x0' and 'EL3356-00x0 Version: 4.8.0 <page>'; a CoE table continues over six
    pages, each starting with the same table header row; every page starts its body on the same line."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    sections = ["Product overview", "Product overview", "Signal flow diagram", "Signal flow diagram", "Commissioning",
                "Commissioning", "Object description", "Object description", "Object description", "Object description",
                "Object description", "Object description"]
    c = canvas.Canvas(str(path), pagesize=A4)
    for i, sec in enumerate(sections):
        if bookmarks:
            c.bookmarkPage(f"p{i}")
            c.addOutlineEntry(f"{i + 1} {sec}", f"p{i}", level=0)
        c.setFont("Helvetica", 8)
        c.drawString(56, 812, f"| {sec}")                                  # running header: the section title
        if i in (0, 2, 4, 6):                                               # a heading on the header's baseline
            c.setFont("Helvetica-Bold", 16)
            c.drawString(200, 812, f"{i // 2 + 1}.{i % 2 + 1} {sec}")
        if 6 <= i:                                                          # the CoE table, continued
            c.setFont("Helvetica", 10)
            c.drawString(56, 760, "Index (hex) Name Meaning Data type Flags Default")
            for k in range(1, 20):
                c.drawString(56, 760 - 14 * k, f"8000:{k + i * 20:02X} Setting {k} of object {i} UINT16 RW 0x0000")
        else:
            c.setFont("Helvetica", 10)
            for k in range(20):
                c.drawString(56, 760 - 14 * k, f"Body of page {i + 1}, line {k}: strain gauge bridge value {k * 7 + i}")
        c.setFont("Helvetica", 8)
        if i % 2:
            c.drawString(56, 30, f"{i + 50} Version: 4.8.0 EL3356-00x0")    # the page number comes first …
        else:
            c.drawString(380, 30, f"EL3356-00x0 Version: 4.8.0 {i + 50}")   # … or last
        c.showPage()
    c.save()


@pytest.mark.parametrize("backend", ["pypdf", "pymupdf"])
def test_n37_n66_running_headers_by_position_not_by_repeated_text(tmp_path, monkeypatch, backend):
    """0.8.9 retest: N37 — the section-title header ('| Signal flow diagram4.2.2 Signal flow diagram', TOC '1.1
    Product overview| Product overview') and the footer whose page number comes first ('56 Version: 4.8.0
    EL3356-00x0') stayed in the text; N66 — real table header rows ('Index (hex) Name Meaning …') were removed.
    Both came from finding headers by repeated TEXT; they are found by their PLACE now."""
    pytest.importorskip("reportlab")
    if backend == "pypdf":
        pytest.importorskip("pypdf")
        monkeypatch.setitem(sys.modules, "pymupdf", None)               # import pymupdf -> ImportError
        monkeypatch.setitem(sys.modules, "fitz", None)
    else:
        pytest.importorskip("pymupdf", exc_type=ImportError)
    m = _manuals_module()
    pdf = tmp_path / "EL3356.pdf"
    _beckhoff_like_pdf(pdf, bookmarks=(backend == "pypdf"))
    used, n, toc, texts, _title, running = m._open_pdf(pdf)
    assert used.startswith(backend) and n == 12 and running is not None and running >= 24
    for i, t in enumerate(texts):
        assert "| " not in t and "Version: 4.8.0" not in t and "EL3356-00x0" not in t, (i, t[:200])
        assert ("Body of page" in t) or ("8000:" in t)
    assert "Body of page 1, line 0" in texts[0]                            # the body's first line stays
    assert "2.1 Signal flow diagram" in texts[2]                           # the heading on the header's baseline stays
    hdr = "Index (hex) Name Meaning Data type Flags Default"
    assert all(hdr in [ln.strip() for ln in texts[i].splitlines()] for i in range(6, 12)), texts[6][:300]
    if backend == "pymupdf":                                               # font headings: no header glued in
        titles = [t for _lvl, t, _pg in toc]
        assert "1.1 Product overview" in titles and "2.1 Signal flow diagram" in titles
        assert not any("|" in t for t in titles), titles
    # the old text-repetition rule removed the table header row (it repeats at the page top on 6 of 12 pages)
    old_out, _heads = m._strip_running([t for t in texts], set())
    assert not old_out[7].lstrip().startswith("Index (hex)")


def test_n37_fallback_page_number_may_come_first():
    """The text-repetition fallback (a backend without positions): a footer whose page number is the FIRST number
    ('56 Version: 4.8.0 …') is a page number too — only the last one counted before."""
    m = _manuals_module()
    body = lambda i: "\n".join(f"line {k} of page {i}: value {k * i + 7}" for k in range(8))  # noqa: E731
    pages = [f"Header text\n{body(i)}\n{i + 50} Version: 4.8.0 EL3356-00x0" for i in range(8)]
    out, heads = m._strip_running(pages, set())
    assert "# version: #.#.# el#-#x#" in heads and all("Version: 4.8.0" not in t for t in out)
    assert m._page_number_like([(0, (56, 4, 8, 0)), (1, (57, 4, 8, 0)), (2, (58, 4, 8, 0))], 1)
    assert not m._page_number_like([(0, (3, 9)), (1, (5, 9)), (2, (8, 9))], 1)          # 'Table 3 of 9' …


# ---------------------------------------------------------------------------------------------- builder stubs
def _stub_env(tmp_path: Path, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("TCMCP_KNOWLEDGE_DIR", "TCMCP_HOME", "TCMCP_BUILDERS")}
    home = tmp_path / "userhome"
    home.mkdir(exist_ok=True)
    env.update(HOME=str(home), USERPROFILE=str(home), TCMCP_HOME=str(tmp_path / "tchome"), PYTHONIOENCODING="utf-8")
    env.update(extra)
    return env


def test_stub_runs_the_plugins_builder_for_the_folder_above_it(tmp_path):
    """The knowledge folder keeps tools\\manuals_db.py / infosys_local.py as stubs that never change: they run the
    builder of the installed plugin (pointer written by the server, else the newest version in the plugin cache)."""
    import shutil as _sh
    kb = tmp_path / "kb"
    (kb / "tools").mkdir(parents=True)
    (kb / "index").mkdir()
    (kb / "manuals").mkdir()
    for n in ("infosys_local.py", "manuals_db.py"):
        _sh.copy(B / "stub.py", kb / "tools" / n)
    # 1. no pointer, no cache: a message, no traceback
    env = _stub_env(tmp_path)
    p = subprocess.run([sys.executable, str(kb / "tools" / "manuals_db.py"), "stats", "--json"], capture_output=True,
                       encoding="utf-8", env=env, timeout=60)
    assert p.returncode == 1 and "was not found" in p.stderr and "Traceback" not in p.stderr
    # 2. the plugin cache of Claude Code: the newest version wins
    cache = Path(env["HOME"]) / ".claude" / "plugins" / "cache" / "twincat-mcp" / "twincat-mcp"
    for ver in ("0.8.9", "0.8.10"):
        d = cache / ver / "skills" / "twincat-docs" / "builders"
        d.mkdir(parents=True)
        for n in ("infosys_local.py", "manuals_db.py"):
            _sh.copy(B / n, d / n)
        _sh.copy(KBD, d.parent / "kb_docs.py")                         # a plugin folder: the skill and the package
        _sh.copytree(ROOT / "twincat_mcp", cache / ver / "twincat_mcp", ignore=_sh.ignore_patterns("__pycache__", "bin"))
    p = subprocess.run([sys.executable, str(kb / "tools" / "manuals_db.py"), "stats", "--json"], capture_output=True,
                       encoding="utf-8", env={**env, "TCMCP_STUB_VERBOSE": "1"}, timeout=60)
    assert p.returncode == 0, p.stderr
    assert "0.8.10" in p.stderr and "plugin cache" in p.stderr
    st = json.loads(p.stdout)
    assert Path(st["database"]) == kb / "index" / "manuals.db" and Path(st["folder"]) == kb / "manuals"
    # 3. the pointer the server writes wins over the cache
    from twincat_mcp import knowledge as K
    tch = Path(env["TCMCP_HOME"])
    tch.mkdir()
    (tch / "plugin_location.json").write_text(json.dumps({"builders": str(B), "version": "repo"}), encoding="utf-8")
    p = subprocess.run([sys.executable, str(kb / "tools" / "infosys_local.py"), "stats", "--json"], capture_output=True,
                       encoding="utf-8", env={**env, "TCMCP_STUB_VERBOSE": "1"}, timeout=60)
    assert "plugin_location.json (twincat plugin repo)" in p.stderr and str(B / "infosys_local.py") in p.stderr
    assert K.builder_kind(kb / "tools" / "infosys_local.py", "infosys_local.py") == ("stub", "2")


def test_pymupdf_space_in_its_own_font_run_is_kept(tmp_path):
    """Review A: whitespace-only spans were dropped before the page text was built, so a space set in another font
    than the words around it ('Note' bold, ' ' regular, 'that' bold) glued them: 'Notethat' on every such line."""
    pytest.importorskip("reportlab")
    pytest.importorskip("pymupdf", exc_type=ImportError)
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    pdf = tmp_path / "space.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    for i in range(4):
        t = c.beginText(72, 700)
        for font, txt in (("Helvetica-Bold", "Note"), ("Helvetica", " "), ("Helvetica-Bold", "that"),
                          ("Helvetica", " the bridge is balanced")):
            t.setFont(font, 10)
            t.textOut(txt)
        c.drawText(t)
        c.setFont("Helvetica", 10)
        for k in range(8):
            c.drawString(72, 680 - 14 * k, f"body line {k} of page {i}")
        c.showPage()
    c.save()
    m = _manuals_module()
    spans, _h = m._spans_pymupdf(__import__("pymupdf").open(str(pdf))[0])
    assert any(s.blank for s in spans), [s.text for s in spans]          # the space is its own span here
    _used, _n, _toc, texts, _title, _running = m._open_pdf(pdf)
    assert all("Note that the bridge is balanced" in t for t in texts), texts[0][:200]


def test_finished_rebuild_is_running_while_its_builder_swaps_it_in(kb, tmp_path):
    """CI 0.8.10: a rebuild that had finished and was being put in place read as 'waiting_to_replace … run index
    again' — the state of a build whose builder gave up. With the builder's lock alive it is running (replacing)."""
    import shutil
    pkg = mshc(tmp_path / "BKINFOSYS3_T_EN-US.mshc", _fixture_pages())
    assert build(kb, pkg).returncode == 0
    db = live(kb)
    building, lock = db.with_name(db.name + ".building"), db.with_name(db.name + ".lock")
    shutil.copyfile(db, building)
    from twincat_mcp import kb_lock
    with kb_lock.IndexLock(lock) as lk:                    # a builder that holds the lock, putting the file in place
        lk.update(phase="replacing the live index")
        st = state(kb)
        assert st["rebuild"]["state"] == "running" and st["rebuild"]["phase"] == "replacing the live index", st
        assert kbdocs(kb, "status")["infosys_state"]["rebuild"]["state"] == "running"
    st = state(kb)                                         # the builder is gone: the finished file waits
    assert st["rebuild"]["state"] == "waiting_to_replace" and "swapped in at the next server start" in st["message"], st
    assert kbdocs(kb, "status")["infosys_state"]["rebuild"]["state"] == "waiting_to_replace"


def test_esi_builder_update_stats_and_queries(tmp_path):
    """esi_db.py: update builds index\\esi.db (the plugin's esi_index), a second update leaves it, stats tells the
    state, the queries answer as the server's hw_* tools; --esi-dir is remembered for the next update."""
    from tests.test_esi_index import EL33, TERMINALS, VENDOR
    kb = tmp_path / "kb"
    (kb / "manuals" / "Beckhoff_EtherCAT_XML").mkdir(parents=True)
    (kb / "manuals" / "Beckhoff_EtherCAT_XML" / "Beckhoff EL33xx.xml").write_text(EL33.format(vendor=VENDOR.format(fv=10)),
                                                                                encoding="utf-8")
    extra = tmp_path / "tc"
    extra.mkdir()
    (extra / "Beckhoff EtherCAT Terminals.xml").write_text(TERMINALS.format(vendor=VENDOR.format(fv=3)), encoding="utf-8")
    p = run("esi_db.py", "update", "--kb", str(kb), "--esi-dir", str(extra), "--no-twincat")
    assert p.returncode == 0 and "indexed 3 device revisions" in p.stdout, p.stdout + p.stderr
    p = run("esi_db.py", "update", "--kb", str(kb), "--no-twincat")          # the extra folder is remembered
    assert p.returncode == 0 and "up to date" in p.stdout, p.stdout
    p = run("esi_db.py", "stats", "--kb", str(kb), "--json")
    st = json.loads(p.stdout)
    assert st["state"] in ("complete", "outdated") and st["devices"] == 3
    p = run("esi_db.py", "lookup", "EL1002", "--kb", str(kb))
    assert json.loads(p.stdout)["devices"][0]["pdo"][1]["name"] == "Channel 2"
    p = run("esi_db.py", "object", "EL3356-0010", "#x8000", "--kb", str(kb))
    assert json.loads(p.stdout)["name"] == "RMB Settings"
    p = run("esi_db.py", "object", "EL3356-0010", "#x8010", "--kb", str(kb))
    assert p.returncode == 1 and "the nearest" in json.loads(p.stdout)["error"]
    p = run("esi_db.py", "update", "--kb", str(kb), "--no-esi-dirs", "--no-twincat")
    assert p.returncode == 0 and "indexed 2 device revisions" in p.stdout, p.stdout


def test_n125_errors_builder_update_stats_lookup_and_a_failed_check(tmp_path):
    """N125: errors_db.py update builds index\\errors.db from the knowledge folder's indexes (the plugin's
    errors_index, behind kb_lock, swapped in when complete and checked), a second update leaves it, stats tells the
    state, lookup answers as err_lookup; a build that fails the golden checks replaces nothing and exits non-zero."""
    import sqlite3 as _sq
    from tests.test_errors_index import make_kb
    kb = make_kb(tmp_path)
    p = run("errors_db.py", "update", "--kb", str(kb))
    assert p.returncode == 0 and "codes (" in p.stdout, p.stdout + p.stderr
    p = run("errors_db.py", "update", "--kb", str(kb))
    assert p.returncode == 0 and "up to date" in p.stdout, p.stdout
    st = json.loads(run("errors_db.py", "stats", "--kb", str(kb), "--json").stdout)
    assert st["state"] == "complete" and st["domains_detail"]["ads"]["codes"] == 139, st
    r = json.loads(run("errors_db.py", "lookup", "0x745", "--kb", str(kb)).stdout)
    assert r["matches"][0]["symbol"] == "ADSERR_CLIENT_SYNCTIMEOUT", r
    con = _sq.connect(kb / "index" / "infosys-1033.db")
    con.execute("DELETE FROM pages WHERE page_id = '391166347'")
    con.commit()
    con.close()
    before = (kb / "index" / "errors.db").read_bytes()
    p = run("errors_db.py", "update", "--kb", str(kb), "--force")
    assert p.returncode == 3 and "FAILED its checks" in p.stderr and "win32 0x6" in p.stderr, p.stdout + p.stderr
    assert (kb / "index" / "errors.db").read_bytes() == before


def test_n170_the_skill_and_the_cli_answer_symbols_documented_inside_a_page(kb, tmp_path):
    """N170 (0.8.14 retest): `decl RecipeManCommands`, `decl CreateRecipe` (CLI and skill) → 'no page for symbol' — the
    methods are sections of one page with tables and no code. Built by the builder the user runs, the skill and the
    CLI answer them like infosys_declaration does (C1–C5), docs_page opens a method's section — that section alone,
    not 'Method CreateRecipeNoSave' too — and the CLI prints the reconstructed declaration."""
    from tests.test_infosys_local import RECIPE_PAGES, _tbl, _VARS
    pages = dict(RECIPE_PAGES)
    page = pages["tc3_plc_intro/1033/2525821323.html"]
    extra = ("<h2>Method CreateRecipeNoSave</h2><p>Creates a recipe without saving it.</p><p><b>Inputs (VAR_INPUT)</b></p>"
             + _tbl(_VARS, ("RecipeName", "STRING", "Name of the recipe")) + "<h2>Return values</h2>")
    pages["tc3_plc_intro/1033/2525821323.html"] = page.replace("<h2>Return values</h2>", extra, 1)
    p = build(kb, mshc(tmp_path / "BKINFOSYS3_VS_100_EN-US.2.mshc", pages))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "symbols documented inside pages: 7 Constant, 1 FunctionBlock, 4 Method" in p.stdout + p.stderr, p.stdout
    r = kbdocs(kb, "docs_declaration", "symbol=RecipeManCommands")
    assert r["kind"] == "FunctionBlock" and r["ref"] == "tc3_plc_intro/2525821323", r
    assert [m["name"] for m in r["methods"]] == ["CreateRecipe", "GetRecipeNames", "GetRecipeValues", "CreateRecipeNoSave"]
    assert r["declaration_blocks"] == [] and "examples" in r["note"]             # the page's code is a sample
    for q in ("RecipeManCommands.CreateRecipe", "CreateRecipe", "createRecipe"):
        r = kbdocs(kb, "docs_declaration", f"symbol={q}")
        assert (r["kind"], r["return_type"], r["section"]) == ("Method", "DWORD", "Method CreateRecipe"), (q, r)
        assert [x["name"] for x in r["inputs"]] == ["RecipeDefinitionName", "RecipeName"]
        assert r["parent"] == {"title": "RecipeManCommands", "ref": "tc3_plc_intro/2525821323"}
        assert "section='Method CreateRecipe'" in r["note"]
    r = kbdocs(kb, "docs_declaration", "symbol=ERR_RECIPE_NOT_FOUND")
    assert (r["kind"], r["value"], r["type"], r["gvl"]) == ("Constant", "16#4003", "UDINT", "ReturnValues"), r
    # third review of 0.8.15 (S3-2): a constant's declaration is reconstructed too — 'the page has no code block' was
    # said of a page with code, and the section was lost
    assert "reconstructed from the page's tables" in r["note"] and "section='Return values'" in r["note"], r["note"]
    assert r["declaration_blocks"] == [], r
    r = kbdocs(kb, "docs_declaration", "symbol=AlarmCommands")               # only a title has the name
    assert (r["kind"], r["match"], r["ref"]) == ("Page", "title", "tc3_plc_intro/2525822000"), r
    r = kbdocs(kb, "docs_page", "ref=CreateRecipe")
    assert r["section"] == "Method CreateRecipe" and r["text"].startswith("## Method CreateRecipe\n"), r
    assert "CreateRecipeNoSave" not in r["text"] and "Return value" in r["text"]
    r = kbdocs(kb, "docs_page", "ref=tc3_plc_intro/2525821323", "section=Method CreateRecipe")
    assert "CreateRecipeNoSave" not in r["text"]
    r = kbdocs(kb, "docs_page", "ref=tc3_plc_intro/2525821323", "section=CreateRecipe")     # a part: every match
    assert "CreateRecipeNoSave" in r["text"] and "## Method CreateRecipe\n" in r["text"]
    p = run("infosys_local.py", "--kb", str(kb), "decl", "CreateRecipe")
    assert p.returncode == 0 and "METHOD CreateRecipe : DWORD" in p.stdout and "RecipeName           : STRING;" in p.stdout, p.stdout
    assert "Method — section 'Method CreateRecipe'" in p.stdout, p.stdout
    p = run("infosys_local.py", "--kb", str(kb), "decl", "ERR_RECIPE_NOT_FOUND")
    assert "ERR_RECIPE_NOT_FOUND : UDINT := 16#4003;" in p.stdout and "Value: 16#4003 (GVL ReturnValues)" in p.stdout, p.stdout


def test_rereview_0815_the_built_index_finds_a_symbol_by_the_name_its_page_declares(kb, tmp_path):
    """Re-review of 0.8.15 (S3): built by the builder the user runs, `decl FB_ActuatorActuals` finds the tf8560 page
    titled 'ActualsActuator' that declares it, and the CLI says under which name the page declares it."""
    from tests.test_infosys import store_declared_as
    from tests.test_infosys_local import _hv_page
    pages: dict[str, str] = {}
    store_declared_as(lambda ref, title, body, hid: pages.__setitem__(
        ref.replace("/", "/1033/", 1) + ".html", _hv_page(title, body, hid)))
    p = build(kb, mshc(tmp_path / "BKINFOSYS3_VS_100_EN-US.2.mshc", pages))
    assert p.returncode == 0, p.stdout + p.stderr
    r = kbdocs(kb, "docs_declaration", "symbol=FB_ActuatorActuals")
    assert (r["ref"], r["declared_as"]) == ("tf8560_plastic_technology_functions/16259000001", "FB_ActuatorActuals"), r
    c = run("infosys_local.py", "--kb", str(kb), "decl", "ActualsActuator")
    assert c.returncode == 0 and "Declared as FB_ActuatorActuals: the page is titled 'ActualsActuator'" in c.stdout
    c = run("infosys_local.py", "--kb", str(kb), "decl", "SendMessageEx2")
    assert c.returncode == 0 and "Syntax header: the header in its Syntax section declares SendMessageEx" in c.stdout
    assert "sJsonAttribute, which that header does not declare" in c.stdout, c.stdout


def test_n167_n168_n169_the_cli_pages_reads_on_and_shows_its_ranges(kb, tmp_path):
    """N167 / N168 / N169 (0.8.14 retest): `search … --limit 300` was refused and had no --offset; `show` had no
    --offset ('… [N more chars; --max or --section]', and --max 40000 was refused); -h showed `--limit LIMIT` /
    `--max MAX` with no range or default. The CLI pages like the skill, its cut marker and footer name the next
    --offset, and -h gives every range."""
    from tests.test_infosys_local import RECIPE_PAGES
    p = build(kb, mshc(tmp_path / "BKINFOSYS3_VS_100_EN-US.2.mshc", RECIPE_PAGES))
    assert p.returncode == 0, p.stdout + p.stderr
    a = run("infosys_local.py", "--kb", str(kb), "search", "recipe", "--limit", "1")
    assert a.returncode == 0 and "--offset 1 for the next ones" in a.stdout, a.stdout
    b = run("infosys_local.py", "--kb", str(kb), "search", "recipe", "--limit", "1", "--offset", "1")
    assert b.returncode == 0 and b.stdout.splitlines()[0] != a.stdout.splitlines()[0], (a.stdout, b.stdout)
    c = run("infosys_local.py", "--kb", str(kb), "search", "recipe", "--limit", "300")
    assert c.returncode == 0 and "limit 300 is above the maximum (1-50): 50 used" in c.stdout, c.stdout + c.stderr
    s = run("infosys_local.py", "--kb", str(kb), "show", "tc3_plc_intro/2525821323", "--max", "300")
    assert s.returncode == 0 and "more chars; --offset 300]" in s.stdout, s.stdout
    t = run("infosys_local.py", "--kb", str(kb), "show", "tc3_plc_intro/2525821323", "--max", "300", "--offset", "300")
    assert t.returncode == 0 and "--offset 600]" in t.stdout and t.stdout != s.stdout, t.stdout
    big = run("infosys_local.py", "--kb", str(kb), "show", "tc3_plc_intro/2525821323", "--max", "40000")
    assert big.returncode == 0 and "more chars" not in big.stdout, big.stdout + big.stderr
    for cmd, want in (("search", ["1-50", "default 10", "--offset"]), ("show", ["200-60000", "default 4000", "--offset"])):
        h = run("infosys_local.py", cmd, "-h")
        assert all(w in " ".join(h.stdout.split()) for w in want), (cmd, h.stdout)
