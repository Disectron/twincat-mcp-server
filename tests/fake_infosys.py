"""A synthetic infosys.beckhoff.com: sitemap index → language index → urlset, plus pages shaped
like the real FB/function/struct/enum/alias documentation pages (H1 title, ST declaration blocks,
Name|Type|Description tables, a Prerequisites table naming the library)."""
from __future__ import annotations

import urllib.parse

from twincat_mcp.infosys import InfosysError

C = "https://infosys.beckhoff.com/content/1033/"

PAGES: dict[str, str] = {}
LASTMOD: dict[str, str] = {}


def _page(book: str, pid: str, title: str, body: str, lastmod: str = "2026-01-01T00:00:00+00:00") -> str:
    url = f"{C}{book}/{pid}.html"
    PAGES[url] = f"""<!DOCTYPE html><html><head><title>{title} - Beckhoff Information System</title>
<script>var x = 1;</script><link rel="stylesheet" href="x.css"></head>
<body><nav><a href="/">Home</a> &gt; TwinCAT 3</nav>
<div id="content"><h1>{title}</h1>{body}
<p class="footer">© Beckhoff Automation</p></div><footer>footer junk</footer></body></html>"""
    LASTMOD[url] = lastmod
    return url


PREREQ = """<h2>Prerequisites</h2>
<table><tr><th>Development environment</th><th>Target platform</th><th>PLC libraries to be integrated (category group)</th></tr>
<tr><td>TwinCAT v3.1.0</td><td>PC or CX (x86, x64, Arm®)</td><td>Tc2_System (system)</td></tr></table>"""

ADSREAD = _page("tcplclib_tc2_system", "30945803", "ADSREAD", """
<p>The function block <b>ADSREAD</b> enables an ADS read command to be sent to an ADS device.</p>
<img src="adsread.png" alt="ADSREAD 1:">
<h2>Inputs</h2>
<pre>VAR_INPUT
    NETID   : T_AmsNetId;    (* the AmsNetId *)
    PORT    : T_AmsPort;
    IDXGRP  : UDINT;
    IDXOFFS : UDINT;
    LEN     : UDINT;
    DESTADDR: PVOID;
    READ    : BOOL;
    TMOUT   : TIME := DEFAULT_ADS_TIMEOUT;
END_VAR</pre>
<table><tr><th>Name</th><th>Type</th><th>Description</th></tr>
<tr><td>NETID</td><td>T_AmsNetId</td><td>String containing the AMS network ID of the target device.</td></tr>
<tr><td>PORT</td><td>T_AmsPort</td><td>Port number of the ADS device.</td></tr>
<tr><td>READ</td><td>BOOL</td><td>The command is triggered by a rising edge at this input.</td></tr>
<tr><td>TMOUT</td><td>TIME</td><td>Time-out for the ADS command.</td></tr></table>
<h2>Outputs</h2>
<pre>VAR_OUTPUT
    BUSY  : BOOL;
    ERR   : BOOL;
    ERRID : UDINT;
END_VAR</pre>
<table><tr><th>Name</th><th>Type</th><th>Description</th></tr>
<tr><td>BUSY</td><td>BOOL</td><td>TRUE while the command is being processed.</td></tr>
<tr><td>ERR</td><td>BOOL</td><td>TRUE if an error occurred.</td></tr>
<tr><td>ERRID</td><td>UDINT</td><td>ADS error code (see error codes | overview).</td></tr></table>
<h2>Example</h2>
<pre>PROGRAM MAIN
VAR
    fbAdsRead : ADSREAD;
    nState    : INT;
END_VAR
fbAdsRead(NETID:='', PORT:=851, IDXGRP:=16#F005, IDXOFFS:=0, LEN:=4, DESTADDR:=ADR(nState), READ:=TRUE);
IF NOT fbAdsRead.BUSY THEN
    fbAdsRead(READ:=FALSE);
END_IF</pre>
""" + PREREQ)

ADSWRITE = _page("tcplclib_tc2_system", "30946059", "ADSWRITE", """
<p>Sends an ADS write command.</p>
<pre>FUNCTION_BLOCK ADSWRITE
VAR_INPUT
    NETID : T_AmsNetId;
    WRITE : BOOL;
END_VAR
VAR_OUTPUT
    BUSY : BOOL;
    ERR  : BOOL;
END_VAR</pre>""" + PREREQ)

T_AMSNETID = _page("tcplclib_tc2_system", "31020939", "T_AmsNetId", """
<p>String containing the AMS network ID.</p>
<pre>TYPE T_AmsNetId : STRING(23);
END_TYPE</pre>""" + PREREQ)

ST_X = _page("tcplclib_tc2_system", "31021000", "ST_AmsRouterInfo", """
<p>Router status information.</p>
<pre>TYPE ST_AmsRouterInfo :
STRUCT
    eState     : E_AmsRouterState;
    nAvailable : UDINT;   // free mailboxes
END_STRUCT
END_TYPE</pre>
<table><tr><th>Name</th><th>Type</th><th>Description</th></tr>
<tr><td>eState</td><td>E_AmsRouterState</td><td>Current router state.</td></tr></table>""" + PREREQ)

E_X = _page("tcplclib_tc2_system", "31021100", "E_AmsRouterState", """
<p>Router states.</p>
<pre>TYPE E_AmsRouterState : (
    AMSROUTER_STOP := 0,
    AMSROUTER_START := 1,
    AMSROUTER_REMOVED := 2
) UDINT;
END_TYPE</pre>""" + PREREQ)

F_X = _page("tcplclib_tc2_system", "31021200", "F_GetVersionTcSystem", """
<p>Returns the library version.</p>
<pre>FUNCTION F_GetVersionTcSystem : UINT
VAR_INPUT
    nVersionElement : INT;
END_VAR</pre>""" + PREREQ)

LIB = _page("tcplclib_tc2_system", "index", "TwinCAT 3 PLC Lib: Tc2_System", "<p>Manual</p>")

TF = _page("tf5100_tc3_nc_i", "3288748555", "Display of technology data", """
<p>Technology data of the segment currently in block execution (SAF) can be read with ADS-Read.</p>
<table><tr><th>Function</th><th>ADS-Read</th></tr><tr><td>Port</td><td>500 (dec)</td></tr></table>""")

TC2 = _page("tcplclibsystem", "11827814795", "ADSREAD", "<p>TwinCAT 2 ADSREAD. Must never surface.</p>")

_URLSET = "\n".join(f"<url><loc>{u}</loc><lastmod>{lm}</lastmod></url>" for u, lm in LASTMOD.items() if not u.endswith("/index.html"))
SITEMAPS = {
    "https://infosys.beckhoff.com/sitemap.xml":
        '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<sitemap><loc>https://infosys.beckhoff.com/content/1031/sitemap.xml</loc></sitemap>'
        '<sitemap><loc>https://infosys.beckhoff.com/content/1033/sitemap.xml</loc></sitemap></sitemapindex>',
    "https://infosys.beckhoff.com/content/1033/sitemap.xml":
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        '<sitemap><loc>https://infosys.beckhoff.com/content/1033/sitemap1.xml</loc>'
        '<lastmod>2026-08-20T18:56:16+00:00</lastmod></sitemap></sitemapindex>',
    "https://infosys.beckhoff.com/content/1033/sitemap1.xml":
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{_URLSET}</urlset>',
}

DDG = ("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote_plus("site:infosys.beckhoff.com technology data SAF"))
DDG_HTML = f"""<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg={urllib.parse.quote(TF + '?id=1175209852827580240', safe='')}&amp;rut=abc">Display of technology data</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg={urllib.parse.quote(TC2, safe='')}">TwinCAT 2 page</a>
<a class="result__a" href="https://infosys.beckhoff.com/content/1033/tcplclib_tc2_system/99999999.html">not in sitemap</a>
</body></html>"""


class FakeFetcher:
    def __init__(self):
        self.calls: list[str] = []
        self.fail: set[str] = set()

    def get(self, url: str, allow_hosts=None) -> bytes:
        self.calls.append(url)
        host = urllib.parse.urlparse(url).hostname
        if allow_hosts is not None and host not in allow_hosts:
            raise InfosysError(f"host {host} not allowed")
        if url in self.fail:
            raise InfosysError(f"fetch failed for {url}: simulated")
        if url in SITEMAPS:
            return SITEMAPS[url].encode()
        if url.startswith("https://html.duckduckgo.com/"):
            return DDG_HTML.encode()
        if url in PAGES:
            return PAGES[url].encode()
        raise InfosysError(f"HTTP 404 for {url}")
