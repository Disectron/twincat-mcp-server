"""infosys_* from the local Help Viewer package (BKINFOSYS3_*.mshc): no network, every book, web URLs as keys."""
from __future__ import annotations

import os
import re
import time
import zipfile
from pathlib import Path

import anyio
import pytest

from twincat_mcp import helpviewer as HV
from twincat_mcp.infosys import InfosysError, InfosysIndex
from tests import fake_infosys as F

C = "https://infosys.beckhoff.com/content/1033/"


class NoNetwork:
    """Any fetch in local mode is a bug."""
    calls: list[str] = []

    def get(self, url, allow_hosts=None):
        raise AssertionError(f"network access in local mode: {url}")


def _as_package_page(url: str, html: str) -> str:
    """Wrap a fake web page the way the Help Viewer package ships it: <meta> tags, breadcrumb, <article>."""
    title = re.search(r"<h1>(.*?)</h1>", html).group(1)
    book = url[len(C):].split("/")[0]
    body = re.search(r'<div id="content">(.*)</div><footer>', html, re.S).group(1)
    ver = "Tc2_System (TwinCAT v3.1.0)" if book == "tcplclib_tc2_system" else "TwinCAT v3.1.4024"
    return (f'<!DOCTYPE html><html><head><meta name="Title" content="{title}" />'
            f'<meta name="Microsoft.Help.Id" content="{abs(hash(url))}" />'
            f'<meta name="Microsoft.Help.DisplayVersion" content="{ver}" />'
            f"<title>{title}</title></head><body>"
            f'<ol class="breadcrumb"><li>TwinCAT 3</li><li>PLC Lib</li><li>{book}</li></ol>'
            f"<article>{body}</article><footer>junk</footer></body></html>")


def make_mshc(path: Path, extra: dict[str, str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for url, html in F.PAGES.items():
            rest = url[len(C):]
            book, page = rest.split("/")
            z.writestr(f"{book}/1033/{page}", _as_package_page(url, html))
        for name, html in (extra or {}).items():
            z.writestr(name, html)
        z.writestr("tcplclib_tc2_system/1031/30945803.html", "<html>deutsch</html>")   # other language: ignored
        z.writestr("images/x.png", b"\x89PNG")
    return path


@pytest.fixture
def pkg(tmp_path):
    d = tmp_path / "Microsoft" / "HelpLibrary2" / "Catalogs" / "VisualStudio15" / "ContentStore" / "EN-US"
    d.mkdir(parents=True)
    return make_mshc(d / "BKINFOSYS3_VS_100_EN-US.2.mshc")


@pytest.fixture
def idx(tmp_path, pkg):
    return InfosysIndex(tmp_path / "idx", fetcher=NoNetwork(), books=["tcplclib_tc2_system"], local=HV.HelpViewerSource(pkg))


def test_package_maps_to_web_urls(pkg):
    src = HV.HelpViewerSource(pkg)
    pages = src.pages()
    assert F.ADSREAD in pages and all(u.startswith(C) for u in pages)
    assert not any("/1031/" in u for u in pages), "only the configured language"
    # identity per page = content (CRC-32 + size), not a file date: equal pages, equal identity
    assert all(v.startswith("crc:") for v in pages.values()) and len(set(pages.values())) == len(pages)
    assert src.content_stamp() == HV.HelpViewerSource(pkg).content_stamp()
    assert src.describe()["kind"] == "helpviewer" and src.describe()["books"] == 3


def test_find_packages_newest_first(pkg, tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramData", str(tmp_path))
    monkeypatch.setenv("ALLUSERSPROFILE", str(tmp_path))
    found = HV.find_packages()
    assert found and found[0] == pkg
    newer = make_mshc(pkg.with_name("BKINFOSYS3_VS_100_EN-US.3.mshc"))
    os.utime(newer, (time.time() + 60, time.time() + 60))
    assert HV.find_packages()[0] == newer
    assert HV.find_packages(str(pkg)) == [pkg] and HV.find_packages(str(pkg.parent))[0] == newer
    assert HV.find_packages(str(tmp_path / "missing")) == []


def test_index_search_declaration_without_network(idx):
    r = idx.index_book("tcplclib_tc2_system")
    assert r["fetched"] == 7 and r["failed"] == []          # the book index page too
    d = idx.declaration("ADSREAD")
    assert d["inputs"][0]["name"] == "NETID" and d["outputs"][0]["name"] == "BUSY"
    assert d["doc_version"] == "Tc2_System (TwinCAT v3.1.0)" and d["doc_date"]
    s = idx.search("ADS read command")
    assert s["backend"].endswith("+helpviewer") and s["items"][0]["url"] == F.ADSREAD and s["doc_date"]
    assert "indexed_books" not in s and "source" not in s and s["indexed_books_count"] == 1      # D3
    assert idx.search("zzqxvj")["count"] == 0          # and no web fallback (NoNetwork would raise)
    assert idx.search_symbols("ADSREAD")["items"][0]["url"] == F.ADSREAD


def test_get_page_any_page_in_package_no_breadcrumb_noise(idx):
    p = idx.get_page(F.TF)                                     # never indexed: read straight from the package
    assert p["title"] == "Display of technology data" and p["doc_date"]
    assert p["breadcrumb"] == "TwinCAT 3 > PLC Lib > tf5100_tc3_nc_i" and "PLC Lib" not in p["markdown"]
    assert "junk" not in p["markdown"] and p["version"] == "TwinCAT v3.1.4024"
    assert idx.get_page("tcplclib_tc2_system/30945803")["url"] == F.ADSREAD
    assert idx.get_page("30945803")["url"] == F.ADSREAD
    with pytest.raises(InfosysError, match="not in the local Help Viewer package"):
        idx.get_page(C + "tcplclib_tc2_system/99999999.html")


def test_books_and_index_all_and_the_reindex_rule(idx, pkg, tmp_path):
    """The rule: a page is parsed again only when its content changed or the indexer version changed. A copied
    or re-installed package (new file date, same content) costs nothing; the index is portable."""
    b = idx.books()
    assert {x["book"] for x in b["books"]} == {"tcplclib_tc2_system", "tf5100_tc3_nc_i"}     # TC2 book dropped
    seen: list[tuple[str, int, int]] = []
    r = idx.index_all(page_progress=lambda b, d, t: seen.append((b, d, t)))
    assert seen and all(0 < d <= t for _b, d, t in seen), "D25: progress inside a book"
    assert r["books"] == 2 and r["pages_indexed"] == 8 and r["book_errors"] == [] and idx.index_complete()
    assert idx.index_all()["pages_indexed"] == 0, "same package: nothing re-read"
    # same content, new file date (Help Viewer re-install, or the index copied to another PC): complete, nothing to do
    os.utime(pkg, (time.time() + 300, time.time() + 300))
    idx2 = InfosysIndex(idx.dir, fetcher=NoNetwork(), local=HV.HelpViewerSource(pkg))
    assert idx2.index_complete() and idx2.index_all()["pages_indexed"] == 0
    # a content update changes ONE page → exactly that page is parsed again
    page = F.PAGES[F.ADSWRITE]
    F.PAGES[F.ADSWRITE] = page.replace("ADS write command", "ADS write command (updated)")
    try:
        make_mshc(pkg)
    finally:
        F.PAGES[F.ADSWRITE] = page
    idx3 = InfosysIndex(idx.dir, fetcher=NoNetwork(), local=HV.HelpViewerSource(pkg))
    assert not idx3.index_complete()
    assert idx3.index_all()["pages_indexed"] == 1 and idx3.index_complete()
    # another indexer version: one full rebuild, then complete again
    from twincat_mcp import infosys as inf
    old = inf.INDEXER_VERSION
    try:
        inf.INDEXER_VERSION = old + 1000
        idx4 = InfosysIndex(idx.dir, fetcher=NoNetwork(), local=HV.HelpViewerSource(pkg))
        assert not idx4.index_complete() and idx4.indexed_books() == []
        assert idx4.index_all()["pages_indexed"] == 8 and idx4.index_complete()
    finally:
        inf.INDEXER_VERSION = old


def test_not_a_package(tmp_path):
    bad = tmp_path / "BKINFOSYS3_x.mshc"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("readme.txt", "x")
    with pytest.raises(HV.HelpViewerError, match="no <book>/1033"):
        HV.HelpViewerSource(bad).pages()


@pytest.mark.anyio
async def test_server_uses_local_package(monkeypatch, tmp_path, pkg):
    from tests.test_server_e2e import client_for, payload
    from twincat_mcp import infosys as inf
    from twincat_mcp.config import Config, validate
    from twincat_mcp.server import build_server

    monkeypatch.setattr(inf, "Fetcher", lambda *a, **k: NoNetwork())
    cfg = Config()
    cfg.docs.mshc = str(pkg)
    cfg.docs.cache_dir = str(tmp_path / "docs")
    cfg.docs.books = ["tcplclib_tc2_system"]
    validate(cfg)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        # a page by its address answers from the package while the first build runs (converted, not stored)
        d = payload(await s.call_tool("infosys_get_page", {"ref": F.ADSREAD, "max_chars": 500}))
        assert d["doc_date"] and d["title"] == "ADSREAD"
        doc = payload(await s.call_tool("tc_doctor", {}))
        assert doc["infosys"]["source"]["kind"] == "helpviewer" and "helpviewer-1033" in doc["infosys"]["index"]
        st = {}
        for _ in range(600):                                   # the first build: by itself, in its own process
            st = payload(await s.call_tool("tc_doctor", {}))["infosys"]["state"]
            if st["state"] == "complete" and not st.get("rebuild"):
                break
            time.sleep(0.05)
        assert st["state"] == "complete", st
        d = payload(await s.call_tool("infosys_get_page", {"ref": "ADSWRITE", "max_chars": 500}))
        assert d["title"] == "ADSWRITE" and "indexed" not in d
        r = payload(await s.call_tool("infosys_search", {"query": "technology data"}))
        assert r["items"] and r["items"][0]["book"] == "tf5100_tc3_nc_i" and r["backend"].endswith("+helpviewer")
        assert "index_note" not in r


@pytest.mark.anyio
async def test_kb_rebuild_only_with_approval_and_a_server_start_never_rebuilds(monkeypatch, tmp_path, pkg):
    """docs/KNOWLEDGE_BASE.md rule 6: the first build of a missing index starts by itself, into <kb>\\index\\; an index
    that exists is used as it is — an older format is reported, never rebuilt by a server start — until the user
    approves kb_rebuild (in-chat prompt, or the confirm_token of tc_request_confirmation)."""
    import sqlite3
    from tests.test_server_e2e import client_for, payload, text
    from twincat_mcp import infosys as inf
    from twincat_mcp.config import Config, validate
    from twincat_mcp.server import build_server

    monkeypatch.setattr(inf, "Fetcher", lambda *a, **k: NoNetwork())
    kb = tmp_path / "kb"
    kb.mkdir()
    cfg = Config()
    cfg.docs.mshc = str(pkg)
    cfg.docs.cache_dir = str(tmp_path / "docs")
    cfg.knowledge.location = str(kb)
    validate(cfg)
    live = kb / "index" / "infosys-1033.db"

    async def doctor(s) -> dict:
        for _ in range(600):
            d = payload(await s.call_tool("tc_doctor", {}))["infosys"]
            if d["warm"]["state"] != "warming":
                return d
            await anyio.sleep(0.05)
        raise AssertionError(d)

    async def until_complete(s) -> dict:
        st: dict = {}
        for _ in range(600):
            st = payload(await s.call_tool("tc_doctor", {}))["infosys"]["state"]
            if st["state"] == "complete" and not st.get("rebuild"):
                return st
            await anyio.sleep(0.05)
        raise AssertionError(st)

    async def token(s) -> str:
        return payload(await s.call_tool("tc_request_confirmation",
                                         {"action": "kb_rebuild", "args": {"index": "infosys"}}))["confirm_token"]

    async with client_for(build_server(cfg)) as s:
        st = await until_complete(s)                         # the first build: by itself
        assert Path(st["database"]) == live and st["format"] == inf.INDEXER_VERSION
        assert not (tmp_path / "docs" / "helpviewer-1033").exists()        # one index, in the knowledge folder
        r = await s.call_tool("kb_rebuild", {"index": "infosys"})           # no approval: nothing happens
        assert r.is_error and "tc_request_confirmation" in text(r)
        r = payload(await s.call_tool("kb_rebuild", {"index": "infosys", "confirm_token": await token(s)}))
        assert r["started"] is False and r["up_to_date"]                    # complete and current: left as it is
        r = await s.call_tool("kb_rebuild", {"index": "everything"})
        assert r.is_error and "'infosys', 'manuals' or 'esi'" in text(r)
    c = sqlite3.connect(live)
    c.execute("UPDATE meta SET value = '3' WHERE key = 'indexer_version'")
    c.commit()
    c.close()
    async with client_for(build_server(cfg)) as s:
        d = await doctor(s)                                  # the start's upkeep has run
        st = d["state"]
        assert st["state"] == "rebuild_needed" and "rebuild" not in st and "started_here" not in st, st
        assert "kb_rebuild(index='infosys')" in st["message"]
        from twincat_mcp import kb_lock
        assert not live.with_name(live.name + ".building").exists() and not kb_lock.held(live.with_name(live.name + ".lock"))
        p = payload(await s.call_tool("infosys_get_page", {"ref": F.ADSREAD, "max_chars": 300}))
        assert p["title"] == "ADSREAD"                       # in use as it is
        r = payload(await s.call_tool("kb_rebuild", {"index": "infosys", "confirm_token": await token(s)}))
        assert r["started"] and "format 3" in r["why"], r
        st = await until_complete(s)
        assert st["format"] == inf.INDEXER_VERSION and st["built_by"].startswith("twincat-mcp ")
        log = (tmp_path / "home").rglob("infosys-build.log")
        assert any("rebuild:" in f.read_text(encoding="utf-8") for f in log)


@pytest.mark.anyio
async def test_the_index_follows_the_knowledge_folder_chosen_while_the_server_runs(monkeypatch, tmp_path, pkg):
    """Review: with the folder chosen after the start, the server kept reading and building its own-folder index until
    a restart (and an index whose move into index\\ waited was read at one path and built at another). One path for
    reading and building, set again when the folder is chosen; a folder without an index gets its first build."""
    from tests.test_server_e2e import client_for, payload
    from twincat_mcp import infosys as inf
    from twincat_mcp.config import Config, validate
    from twincat_mcp.server import build_server

    monkeypatch.setattr(inf, "Fetcher", lambda *a, **k: NoNetwork())
    cfg = Config()
    cfg.docs.mshc = str(pkg)
    cfg.docs.cache_dir = str(tmp_path / "docs")
    validate(cfg)

    async def until_complete(s) -> dict:
        st: dict = {}
        for _ in range(600):
            st = payload(await s.call_tool("tc_doctor", {}))["infosys"]["state"]
            if st["state"] == "complete" and not st.get("rebuild"):
                return st
            await anyio.sleep(0.05)
        raise AssertionError(st)

    async with client_for(build_server(cfg)) as s:
        st = await until_complete(s)
        assert Path(st["database"]) == tmp_path / "docs" / "helpviewer-1033" / "index.sqlite"
        kb = tmp_path / "kb"
        kb.mkdir()
        r = payload(await s.call_tool("kb_set_location", {"path": str(kb)}))
        assert r["infosys_index"]["infosys_index"] == str(kb / "index" / "infosys-1033.db")
        assert r["infosys_index"]["first_build"]["started"], r["infosys_index"]
        st = await until_complete(s)
        assert Path(st["database"]) == kb / "index" / "infosys-1033.db"
        d = payload(await s.call_tool("tc_doctor", {}))["infosys"]
        assert d["index"] == str(kb / "index" / "infosys-1033.db")
        p = payload(await s.call_tool("infosys_get_page", {"ref": F.ADSREAD, "max_chars": 300}))
        assert p["title"] == "ADSREAD" and "index_note" not in p


@pytest.mark.anyio
async def test_server_without_package_says_how_to_fix(monkeypatch, tmp_path):
    from tests.test_server_e2e import client_for
    from twincat_mcp.config import Config, validate
    from twincat_mcp.server import build_server

    monkeypatch.setenv("ProgramData", str(tmp_path / "none"))
    monkeypatch.setenv("ALLUSERSPROFILE", str(tmp_path / "none"))
    monkeypatch.setattr(HV, "catalog_roots", lambda: [tmp_path / "none"])
    cfg = Config()
    cfg.docs.cache_dir = str(tmp_path / "docs")
    validate(cfg)
    srv = build_server(cfg)
    async with client_for(srv) as s:
        res = await s.call_tool("infosys_search", {"query": "ADSREAD"})
        assert res.is_error and "BKINFOSYS3" in res.content[0].text and "docs.source = 'web'" in res.content[0].text


def test_r26_ms_xhelp_links_resolve_to_pages(tmp_path):
    """R26: pages link to each other as ms-xhelp:///?Id=…; get_page refused those links from its own output. They
    are resolved once, when the index is built (rule 4): the stored page holds the page URL, the reader shows it as
    stored, and an ms-xhelp reference opens the page through the help ids collected while building."""
    from twincat_mcp import infosys_build as IB
    d = tmp_path / "Microsoft" / "HelpLibrary2" / "Catalogs" / "VisualStudio15" / "ContentStore" / "EN-US"
    d.mkdir(parents=True)
    target = F.ADSREAD
    link_page = ('<html><head><meta name="Title" content="Overview" /><meta name="Microsoft.Help.Id" content="ovw-1" />'
                 '</head><body><article><h1>Overview</h1><p>See <a href="ms-xhelp:///?Id=' + str(abs(hash(target))) +
                 '">ADSREAD</a> and <img src="1107452.png" alt="1107452"/> nothing.</p></article></body></html>')
    pkg = make_mshc(d / "BKINFOSYS3_VS_100_EN-US.2.mshc", {"tcplclib_tc2_system/1033/99999901.html": link_page})
    final = tmp_path / "index" / "infosys-1033.db"
    res = IB.build(final, pkg, log=lambda s: None)
    assert res["result"] == "built" and res["links"]["resolved"] >= 1, res
    ix = InfosysIndex(db_file=final, fetcher=NoNetwork(), local=HV.HelpViewerSource(pkg), read_only=True)
    url = C + "tcplclib_tc2_system/99999901.html"
    p = ix.get_page(url)
    assert target in p["markdown"] and "ms-xhelp" not in p["markdown"], p["markdown"]
    assert "![" not in p["markdown"]                                                    # R32: image placeholder gone
    with ix._db() as con:                                                              # shown exactly as stored
        assert con.execute("SELECT markdown FROM pages WHERE url = ?", (url,)).fetchone()[0] == p["markdown"]
    assert ix.get_page(f"ms-xhelp:///?Id={abs(hash(target))}")["url"] == target
    with pytest.raises(InfosysError, match="no page with that Help Viewer Id"):
        ix.get_page("ms-xhelp:///?Id=nope")
    with pytest.raises(InfosysError, match="max_chars"):
        ix.get_page(target, max_chars=-1)
