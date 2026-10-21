"""After a failed pytest step: the failures as annotations and the output as the job summary.

The job log is kept on a host that is not reachable from everywhere; annotations and the summary are read
through the GitHub API. Used by ci.yml and release.yml (pytest output in pytest-output.txt).
"""
import os, re, sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if not os.path.exists("pytest-output.txt"):
    print("::error title=ci::pytest did not run (an earlier step failed); see that step")
    raise SystemExit(0)
text = open("pytest-output.txt", encoding="utf-8", errors="replace").read()
summ = open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8")
summ.write("## pytest (" + os.environ.get("RUNNER_OS", "?") + ")\n\n```\n" + text[-60000:] + "\n```\n")
def ann(title, msg):
    msg = msg[:3800].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={title}::{msg}")
m = re.search(r"=+ short test summary info =+\n(.*?)(?:\n=+ .* in [\d.]+s)", text, re.S)
lines = [l for l in (m.group(1).splitlines() if m else []) if l.startswith(("FAILED", "ERROR"))]
for l in lines[:8]:
    ann("pytest", l)
# first two failure sections, verbatim, for a traceback
secs = re.split(r"\n_{5,} (.*?) _{5,}\n", text)
for name, body in list(zip(secs[1::2], secs[2::2]))[:2]:
    ann(f"traceback {name}", body)
if not lines:
    ann("pytest", text[-3800:])
