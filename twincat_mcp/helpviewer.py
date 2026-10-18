"""Beckhoff Infosys from the locally installed Microsoft Help Viewer package — the offline source for infosys_*.

TwinCAT XAE installs the complete Beckhoff Information System as a Help Viewer package::

    %ProgramData%\\Microsoft\\HelpLibrary2\\Catalogs\\<catalog>\\ContentStore\\EN-US\\BKINFOSYS3_VS_100_EN-US.<n>.mshc

An ``.mshc`` is a zip of HTML pages stored as ``<book>/<lcid>/<pageid>.html``; ``pageid`` is the same id as on
infosys.beckhoff.com, so every page maps to ``https://infosys.beckhoff.com/content/<lcid>/<book>/<pageid>.html``.
The index keys pages by that URL: answers cite the real web page, and nothing is fetched from the network.

Each page carries ``<meta>`` tags (``Title``, ``Microsoft.Help.Id``, ``Microsoft.Help.TOCParent``,
``Microsoft.Help.DisplayVersion`` e.g. ``Tc2_Utilities (TwinCAT v3.1.0)``) and a breadcrumb; the body is in
``<article>`` (else ``<body>``).

The package is as old as the last Help Viewer content update on this PC — every answer carries its date
(``source.modified``) so a stale manual is visible, not silent.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import threading
import zipfile
from pathlib import Path
from typing import Any

PREFIX = "https://infosys.beckhoff.com/content/"
LANG_DIRS = {"1033": "EN-US", "1031": "DE-DE"}
_ENTRY = re.compile(r"^([^/]+)/(\d{4})/([^/]+?)\.html?$", re.I)
_META = re.compile(r"""<meta\s+name\s*=\s*["']([^"']+)["']\s+content\s*=\s*["']([^"']*)["']""", re.I)


class HelpViewerError(RuntimeError):
    pass


def catalog_roots() -> list[Path]:
    roots = []
    for env in ("ProgramData", "ALLUSERSPROFILE"):
        v = os.environ.get(env)
        if v:
            roots.append(Path(v) / "Microsoft" / "HelpLibrary2" / "Catalogs")
    roots.append(Path(r"C:\ProgramData\Microsoft\HelpLibrary2\Catalogs"))
    out: list[Path] = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def find_packages(configured: str | None = None, lcid: str = "1033") -> list[Path]:
    """Candidate packages, newest first. ``configured`` may be the .mshc itself or a folder holding it."""
    lang = LANG_DIRS.get(lcid, "EN-US")
    found: list[Path] = []
    if configured:
        p = Path(configured)
        if p.is_file():
            return [p]
        if p.is_dir():
            found += [f for f in p.glob("BKINFOSYS3*.mshc") if f.is_file()]
            return sorted(found, key=lambda f: f.stat().st_mtime, reverse=True)
        return []
    for root in catalog_roots():
        if not root.is_dir():
            continue
        # every catalog (VisualStudio15 for the 4024 shell, others for newer shells); language folder case varies
        for cat in root.iterdir():
            store = cat / "ContentStore"
            if not store.is_dir():
                continue
            for d in store.iterdir():
                if d.is_dir() and d.name.upper() == lang:
                    found += [f for f in d.glob("BKINFOSYS3*.mshc") if f.is_file()]
                    found += [f for f in d.glob("bkinfosys3*.mshc") if f.is_file() and f not in found]
    return sorted(set(found), key=lambda f: f.stat().st_mtime, reverse=True)


class HelpViewerSource:
    """Read-only view of one .mshc: page list (as Infosys URLs) and page HTML."""

    kind = "helpviewer"

    def __init__(self, path: str | Path, lcid: str = "1033"):
        self.path = Path(path)
        self.lcid = lcid
        if not self.path.is_file():
            raise HelpViewerError(f"Help Viewer package not found: {self.path}")
        st = self.path.stat()
        self.size, self.mtime = st.st_size, st.st_mtime
        self.stamp = f"{self.path.name}|{self.size}|{int(st.st_mtime_ns)}"
        # the per-page 'lastmod': changes for every page when the package is replaced → full re-index
        self.lastmod = (dt.datetime.fromtimestamp(self.mtime, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        + "#" + hashlib.sha256(self.stamp.encode()).hexdigest()[:10])
        self._entries: dict[str, str] | None = None    # url -> zip entry name
        self._crc: dict[str, str] = {}                  # url -> "crc32:size" of the entry (content identity)
        self._tls = threading.local()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- listing
    def _zip(self) -> zipfile.ZipFile:
        z = getattr(self._tls, "zip", None)
        if z is None:
            try:
                z = zipfile.ZipFile(self.path)
            except (OSError, zipfile.BadZipFile) as exc:
                raise HelpViewerError(f"{self.path.name} is not a readable Help Viewer package: {exc}") from exc
            self._tls.zip = z
        return z

    def entries(self) -> dict[str, str]:
        with self._lock:
            if self._entries is None:
                out: dict[str, str] = {}
                crc: dict[str, str] = {}
                for zi in self._zip().infolist():
                    name = zi.filename
                    m = _ENTRY.match(name.replace("\\", "/"))
                    if not m or m.group(2) != self.lcid:
                        continue
                    url = f"{PREFIX}{self.lcid}/{m.group(1).lower()}/{m.group(3)}.html"
                    if url not in out:
                        out[url] = name
                        crc[url] = f"crc:{zi.CRC:08x}:{zi.file_size}"
                self._crc = crc
                if not out:
                    raise HelpViewerError(f"{self.path.name}: no <book>/{self.lcid}/<id>.html pages inside — not a Beckhoff "
                                          "Infosys package?")
                self._entries = out
            return self._entries

    def pages(self) -> dict[str, str]:
        """url -> identity of the page's content (CRC-32 + size from the zip directory), the same shape as the web
        sitemap's url -> lastmod. A Help Viewer update re-parses only the pages whose content changed; the index is
        portable to another PC with the same package (no file dates involved)."""
        self.entries()
        return dict(self._crc)

    def content_stamp(self) -> str:
        """Identity of the whole package content, independent of file name and modification time (computed once)."""
        if getattr(self, "_stamp_cache", None):
            return self._stamp_cache
        self.entries()
        h = hashlib.sha256()
        for u in sorted(self._crc):
            h.update(f"{u}={self._crc[u]}\n".encode())
        self._stamp_cache = "content:" + h.hexdigest()[:24]
        return self._stamp_cache

    def has(self, url: str) -> bool:
        return url in self.entries()

    def get(self, url: str) -> bytes:
        name = self.entries().get(url)
        if name is None:
            raise HelpViewerError(f"page not in the local Help Viewer package ({self.path.name}): {url}")
        try:
            return self._zip().read(name)
        except (KeyError, OSError, zipfile.BadZipFile) as exc:
            raise HelpViewerError(f"cannot read {name} from {self.path.name}: {exc}") from exc

    def describe(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "file": str(self.path), "bytes": self.size,
                             "modified": dt.datetime.fromtimestamp(self.mtime, dt.timezone.utc).date().isoformat(),
                             "lcid": self.lcid}
        if self._entries is not None:
            d["pages"] = len(self._entries)
            d["books"] = len({u[len(PREFIX) + 5:].split("/", 1)[0] for u in self._entries})
        return d


def page_meta(raw: bytes | str, limit: int = 8000) -> dict[str, str]:
    """<meta name=… content=…> of a package page (first occurrence wins)."""
    text = raw[:limit].decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw[:limit]
    out: dict[str, str] = {}
    for k, v in _META.findall(text):
        out.setdefault(k, v)
    return out


_ARTICLE = re.compile(r"<article\b.*?</article>", re.I | re.S)
_CRUMB = re.compile(r"<ol[^>]*class\s*=\s*[\"'][^\"']*breadcrumb[^\"']*[\"'][^>]*>.*?</ol>", re.I | re.S)


def body_html(raw: bytes) -> tuple[str, str]:
    """(html to convert, breadcrumb text). Keeps <head> (title/meta) + the <article> when there is one, drops the
    breadcrumb list so it does not end up as the first lines of every page."""
    text = raw.decode("utf-8", errors="replace")
    crumb = ""
    m = _CRUMB.search(text)
    if m:
        crumb = " > ".join(x.strip() for x in re.findall(r">([^<>]+)<", m.group(0)) if x.strip())
        text = text[:m.start()] + text[m.end():]
    a = _ARTICLE.search(text)
    if a:
        head = re.search(r"<head\b.*?</head>", text, re.I | re.S)
        text = (head.group(0) if head else "") + "<body>" + a.group(0) + "</body>"
    return text, crumb
