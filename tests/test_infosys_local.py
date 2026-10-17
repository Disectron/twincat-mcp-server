"""infosys_* from the local Help Viewer package (BKINFOSYS3_*.mshc): no network, every book, web URLs as keys."""
from __future__ import annotations

import os
import re
import time
import zipfile
from pathlib import Path

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
    r = idx.index_all()
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


# Fingerprint of the code that turns a page into stored rows. When this test fails: the extraction logic changed —
# bump infosys.INDEXER_VERSION (every index is rebuilt once) and update the fingerprint below together.
INDEXER_FINGERPRINT = (2, "c062b9caaa65b3ad")


def _indexer_source_hash() -> str:
    import hashlib
    import inspect
    from twincat_mcp import helpviewer as hv
    from twincat_mcp import infosys as inf
    parts = [inspect.getsource(o) for o in (inf._MdWalker, inf.html_to_doc, inf.parse_declaration, inf.classify,
                                            inf._parse_vars, inf._descriptions, inf._library_from_tables,
                                            inf.InfosysIndex._store, inf._doc_version, hv.body_html, hv.page_meta)]
    norm = ["\n".join(ln.rstrip() for ln in p.splitlines() if ln.strip() and not ln.strip().startswith("#")) for p in parts]
    return hashlib.sha256("\n\n".join(norm).encode()).hexdigest()[:16]


def test_indexer_version_tracks_the_extraction_code():
    from twincat_mcp import infosys as inf
    got = (inf.INDEXER_VERSION, _indexer_source_hash())
    assert got == INDEXER_FINGERPRINT, (
        f"page extraction code changed: bump infosys.INDEXER_VERSION and set INDEXER_FINGERPRINT = {got!r}")


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
        d = payload(await s.call_tool("infosys_get_page", {"ref": "ADSWRITE", "max_chars": 500}))
        assert d["doc_date"]
        doc = payload(await s.call_tool("tc_doctor", {}))
        assert doc["infosys"]["source"]["kind"] == "helpviewer" and "helpviewer-1033" in doc["infosys"]["cache"]
        for _ in range(100):                                   # background index of the whole package
            st = payload(await s.call_tool("tc_doctor", {}))["infosys"]["background_index"]
            if st.get("finished"):
                break
            time.sleep(0.05)
        r = payload(await s.call_tool("infosys_search", {"query": "technology data"}))
        assert r["items"] and r["items"][0]["book"] == "tf5100_tc3_nc_i" and r["backend"].endswith("+helpviewer")


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
