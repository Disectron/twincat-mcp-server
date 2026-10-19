"""The knowledge-base fixture (index/infosys-1033.db, index/manuals.db), built with the REAL code at test time.

The twincat-docs skill and the infosys_* tools read one Infosys index — the plugin's extractor
(twincat_mcp.infosys_build) on a Help Viewer package — and the manuals index of builders/manuals_db.py. The fixture is
made from the synthetic pages and the PDF below by exactly that code (tests/conftest.py: kb_fixture), so the tests
check the readers against the current format, never against a stored copy of an older one.

    python tests/fixtures/kb/make_fixtures.py --out <folder>        (to look at it)

Needs Python 3.12+, reportlab (to write the PDF) and pypdf or PyMuPDF (manuals_db.py reads it). Page ids, titles and
help ids are those the acceptance table of the docs-skill spec names, so the same calls work on fixture and real index.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

NB = " "


def page(title: str, help_id: str, version: str, crumb: list[str], article: str, toc_parent: str = "beckhoff-0") -> str:
    lis = "".join(f"<li><a href='#'>{c}</a></li>" for c in crumb)
    return (f"<!DOCTYPE html><html><head><meta name=\"Title\" content=\"{title}\">"
            f"<meta name=\"Microsoft.Help.Id\" content=\"{help_id}\">"
            f"<meta name=\"Microsoft.Help.TOCParent\" content=\"{toc_parent}\">"
            f"<meta name=\"Microsoft.Help.DisplayVersion\" content=\"{version}\">"
            f"<title>{title}</title></head><body><header>Beckhoff Information System</header>"
            f"<ol class=\"breadcrumb\">{lis}</ol><article>{article}</article><footer>© Beckhoff</footer></body></html>")


def pre(code: str) -> str:
    # the Help Viewer pages keep code indentation with non-breaking spaces (R17)
    return "<pre>" + code.replace("    ", NB * 4).replace(" := ", f"{NB}:={NB}") + "</pre>"


PAGES = {
    "tcplclib_tc2_system/1033/30866571.html": page(
        "ADSREAD", "beckhoff-adsread-0001", "Tc2_System (TwinCAT v3.1.0)", ["PLC Lib: Tc2_System", "ADS", "ADSREAD"],
        "<h1>ADSREAD</h1><p><img src='1107452.png'/>NETID T_AmsNetIdPORT T_AmsPortIDXGRP UDINTIDXOFFS UDINT</p>"
        "<p>The function block ADSREAD reads data from an ADS device. See also "
        "<a href='ms-xhelp:///?Id=beckhoff-f599-48a7-92f2-48aa9133ca8a'>T_AmsNetID</a>.</p>"
        "<h2>Inputs</h2>" + pre("VAR_INPUT\n    NETID   : T_AmsNetID;\n    PORT    : T_AmsPort;\n    READ    : BOOL;\nEND_VAR")
        + "<h2>Outputs</h2>" + pre("VAR_OUTPUT\n    BUSY    : BOOL;\n    ERR     : BOOL;\n    ERRID   : UDINT;\nEND_VAR")
        + "<h2>Prerequisites</h2><table><tr><th>Development environment</th><th>PLC libraries to include</th></tr>"
          "<tr><td>TwinCAT v3.0.0</td><td>Tc2_System</td></tr></table>"),
    "tc3_c/1033/743169291.html": page(
        "ADSREAD", "beckhoff-adsread-c", "TC3 C++", ["TwinCAT 3 C++", "Samples", "ADSREAD"],
        "<h1>ADSREAD</h1><p>The C++ sample shows ADSREAD and ADSWRITE from a TcCOM module.</p>"
        + pre("// ADSREAD from C++\nhr = m_spAds->AdsReadReq(addr, ADSIGRP_SYM_VALBYHND, hnd, len);")),
    "tcplclib_tc2_system/1033/31059723.html": page(
        "T_AmsNetID", "beckhoff-f599-48a7-92f2-48aa9133ca8a", "Tc2_System (TwinCAT v3.1.0)",
        ["PLC Lib: Tc2_System", "Data types", "T_AmsNetID"],
        "<h1>T_AmsNetID</h1><p>Type for the AMS network address (NetId) as a string.</p>"
        + pre("TYPE T_AmsNetID : STRING(23);\nEND_TYPE")),
    "tcplclib_tc2_mc2/1033/70094731.html": page(
        "MC_MoveAbsolute", "beckhoff-mc2-moveabs", "Tc2_MC2 (TwinCAT v3.1.0)", ["PLC Lib: Tc2_MC2", "MC_MoveAbsolute"],
        "<h1>MC_MoveAbsolute</h1><p>Execute BOOLPosition LREALVelocity LREALJerk LREAL</p>"
        "<p>The function block MC_MoveAbsolute starts the positioning to an absolute target position.</p>"
        + pre("VAR_INPUT\n    Execute    : BOOL;\n    Position   : LREAL;\n    Jerk       : LREAL;\n    BufferMode : MC_BufferMode;\nEND_VAR")),
    "tcplclib_tc3_drivemotioncontrol/1033/8280601227.html": page(
        "MC_MoveAbsolute", "beckhoff-dmc-moveabs", "Tc3_DriveMotionControl (TwinCAT v3.1.0)",
        ["PLC Lib: Tc3_DriveMotionControl", "MC_MoveAbsolute"],
        "<h1>MC_MoveAbsolute</h1><p>Execute BOOL ↔ Axis AXIS_REF</p>"
        "<p>The function block MC_MoveAbsolute starts the positioning to an absolute target position.</p>"
        + pre("VAR_INPUT\n    Execute  : BOOL;\n    Position : LREAL;\n    Velocity : LREAL;\nEND_VAR")
        + pre("VAR_OUTPUT\n    Done  : BOOL;\n    Busy  : BOOL;\nEND_VAR")
        + pre("VAR_IN_OUT\n    Axis : AXIS_REF;\nEND_VAR")),
    "tcplclib_tc2_utilities/1033/35409035.html": page(
        "Example: reading a variable", "beckhoff-util-ex", "Tc2_Utilities (TwinCAT v3.1.0)", ["PLC Lib: Tc2_Utilities", "Samples"],
        "<h1>Example: reading a variable</h1><p>Unbalanced ( parentheses in prose do not break the search.</p>"
        + pre("fbRead( READ := FALSE );\nfbRead( NETID := '', PORT := 851, READ := TRUE );")),
    "tcplclib_tc2_system/1033/30866572.html": page(
        "ADSWRITE", "beckhoff-adswrite", "Tc2_System (TwinCAT v3.1.0)", ["PLC Lib: Tc2_System", "ADS", "ADSWRITE"],
        "<h1>ADSWRITE</h1><p>The function block ADSWRITE writes data to an ADS device; it is the counterpart of "
        "<a href='30866571.html'>ADSREAD</a>. Address: <a href='../tcplclib_tc2_system/1033/31059723.html'>T_AmsNetID</a>, "
        "<a href='https://www.beckhoff.com/'>Beckhoff</a>, <a href='#top'>top</a>, "
        "<a href='ms-xhelp:///?Id=beckhoff-not-in-package'>gone</a>.</p>"
        + pre("VAR_INPUT\n    WRITE : BOOL;\nEND_VAR")),
    # N103b: the function header is written as a heading, the code block has the inputs only (as Beckhoff does)
    "tcplclib_tc2_system/1033/30868043.html": page(
        "MEMCPY", "beckhoff-memcpy", "Tc2_System (TwinCAT v3.1.0)", ["PLC Lib: Tc2_System", "Memory", "MEMCPY"],
        "<h1>MEMCPY</h1><p>The function MEMCPY can be used to copy the values of PLC variables from one memory area "
        "to another.</p><h2>FUNCTION MEMCPY : UDINT</h2>"
        + pre("VAR_INPUT\n    destAddr : PVOID;\n    srcAddr  : PVOID;\n    n        : UDINT;\nEND_VAR")),
    "tcplclib_tc2_system_ex/1033/40000001.html": page(
        "ADS return codes", "beckhoff-ads-codes", "Tc2_System (TwinCAT v3.1.0)", ["PLC Lib: Tc2_System", "ADS return codes"],
        "<h1>ADS return codes</h1><p>ADSREAD and ADSWRITE report ERRID. 1861 means timeout elapsed.</p>"
        "<table><tr><td>1861</td><td>0x745</td><td>timeout elapsed</td></tr></table>"),
}

MANUAL = [
    ("1 Foreword", 1, ["CX9020 Embedded PC", "Manual | EN", "Version 1.6"]),
    ("2 Product overview", 1, ["The CX9020 is a compact DIN-rail PC. Order codes CX9020-0110 and CX9020-0111.",
                               "Terminals EL3102 and EK1100 can be attached."]),
    ("10 Error handling and diagnostics", 1, ["The CX9020 shows its state on LEDs."]),
    ("10.1 Diagnostic LEDs", 2, ["The watchdog LED lights red when the watchdog of the CX9020 expired.",
                                 "EL3102 terminals report errors on their own LEDs."]),
]


def build_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle("")                                       # Beckhoff manuals carry no metadata title
    parents: dict[int, str] = {}
    for i, (head, level, lines) in enumerate(MANUAL):
        key = f"s{i}"
        c.bookmarkPage(key)
        c.addOutlineEntry(head, key, level=level - 1)
        parents[level] = key
        c.setFont("Helvetica-Bold", 18 if i == 0 else 14)
        c.drawString(72, 760, lines[0] if i == 0 else head)
        c.setFont("Helvetica", 10)
        for j, ln in enumerate(lines[1:] if i == 0 else lines):
            c.drawString(72, 730 - 16 * j, ln)
        c.showPage()
    c.save()


PACKAGE_NAME = "BKINFOSYS3_FIXTURE_EN-US.mshc"


def write_package(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, html in PAGES.items():
            z.writestr(name, html)
    return path


def make_kb(out: Path, tools: Path | None = None) -> Path:
    """A knowledge folder at `out`: the package in out/_package, index/infosys-1033.db built by the plugin's
    infosys_build, manuals/IPC/CX9020.pdf and index/manuals.db built by the manuals builder (its CLI)."""
    root = Path(__file__).resolve().parents[3]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from twincat_mcp import infosys_build, kb_layout
    tools = tools or root / "skills" / "twincat-docs" / "builders"
    out.mkdir(parents=True, exist_ok=True)
    pkg = write_package(out / "_package" / PACKAGE_NAME)
    infosys_build.build(kb_layout.index_file(out, "infosys"), pkg, "1033", built_by="fixture", log=lambda s: None)
    man = out / "manuals" / "IPC"
    man.mkdir(parents=True, exist_ok=True)
    build_pdf(man / "CX9020.pdf")
    subprocess.run([sys.executable, str(tools / "manuals_db.py"), "update", "--kb", str(out)], check=True,
                   capture_output=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    print(make_kb(Path(ap.parse_args().out)))


if __name__ == "__main__":
    main()
