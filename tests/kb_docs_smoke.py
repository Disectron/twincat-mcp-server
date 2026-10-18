"""Smoke test of the twincat-docs script as the skill runs it: kb_docs.py on a Python 3.12 with nothing installed
(-I -S: no site-packages, no PYTHONPATH — only the standard library and the plugin folder the script sits in), over a
knowledge folder whose indexes the real builders made (tests/fixtures/kb/make_fixtures.py; that step needs the dev
extras reportlab and pypdf). CI step "kb_docs.py without the plugin installed". python tests/kb_docs_smoke.py"""
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "twincat-docs" / "kb_docs.py"
_spec = importlib.util.spec_from_file_location("tcmcp_make_fixtures", ROOT / "tests" / "fixtures" / "kb" / "make_fixtures.py")
_mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mk)
KB = _mk.make_kb(Path(tempfile.mkdtemp(prefix="tcmcp-kb-")) / "kb")


def call(*args):
    p = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), *args, "--kb", str(KB)], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
    if p.stderr.strip():
        raise SystemExit(f"stderr from {args}: {p.stderr.decode('utf-8', 'replace')}")
    return p.returncode, json.loads(p.stdout.decode("utf-8"))


def check(cond, what):
    if not cond:
        raise SystemExit(f"FAILED: {what}")
    print(f"ok   {what}")


rc, r = call("docs_search", "query=ADSREAD", "limit=3")
check(rc == 0 and r["count"] == 3 and r["items"][0]["ref"] == "tc3_c/743169291", "docs_search ranking")
rc, r = call("docs_search", "query=fbRead( READ := FALSE )")
check(rc == 0 and r["query_mode"].startswith("phrase"), "phrase retry")
rc, r = call("docs_declaration", "symbol=MC_MoveAbsolute")
check(rc == 1 and r["error"] == "ambiguous" and len(r["candidates"]) == 2, "ambiguous refused")
rc, r = call("docs_page", "ref=tcplclib_tc2_system/30866572")
check(rc == 0 and r["links"][0]["ref"] == "tcplclib_tc2_system/30866571", "links as refs")
rc, r = call("docs_page", "ref=ms-xhelp:///?Id=beckhoff-f599-48a7-92f2-48aa9133ca8a")
check(rc == 0 and r["title"] == "T_AmsNetID", "ms-xhelp link")
rc, r = call("manual_search", "query=watchdog")
check(rc == 0 and r["items"][0]["page"] == 4, "manual_search")
rc, r = call("status")
check(rc == 0 and r["infosys_state"]["state"] == "complete" and "Infosys index: complete" in r["summary"], "status")
rc, r = call("docs_search", "query=x", "limit=0")
check(rc == 1 and r["error"] == "bad_param", "range refused")
print(f"kb_docs.py works on Python {sys.version.split()[0]}")
