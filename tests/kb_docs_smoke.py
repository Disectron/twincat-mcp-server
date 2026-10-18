"""Standard-library smoke test of the twincat-docs script on its minimum Python (3.9; CI job docs-skill-min-python).
No pytest and no plugin install needed: python tests/kb_docs_smoke.py"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "tests" / "fixtures" / "kb"
SCRIPT = ROOT / "skills" / "twincat-docs" / "kb_docs.py"


def call(*args):
    p = subprocess.run([sys.executable, str(SCRIPT), *args, "--kb", str(KB)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
