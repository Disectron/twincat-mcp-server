"""Bootstrap launcher for twincat-mcp.

Used by the Claude Code plugin (``.claude-plugin/plugin.json``) and the Claude Desktop
bundle (``packaging/mcpb``). An MCP client only knows how to *start a command*; it does
not install Python packages. This script hides the install step:

1. first start: create a private virtualenv, ``pip install`` the server from the
   directory it was shipped in (``--src``), write a stamp;
2. every start: compare the stamp (version + content hash of the shipped sources) and
   reinstall only when the shipped code changed;
3. hand stdin/stdout over to ``python -m twincat_mcp`` unchanged.

Everything this script prints goes to **stderr** — stdout is the MCP transport.

Venv location, first match wins: ``--venv``, ``$TCMCP_VENV``,
``$CLAUDE_PLUGIN_DATA/venv`` (Claude Code keeps that directory across plugin updates),
``%LOCALAPPDATA%/twincat-mcp/venv`` on Windows, ``~/.twincat_mcp/venv`` elsewhere.

Exit codes: 0 server exited cleanly; 2 bad arguments; 3 Python too old; 4 venv/pip failure.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 10)
STAMP_NAME = ".twincat-mcp-stamp.json"
PACKAGE_DIR = "twincat_mcp"
UNRESOLVED_MARKERS = ("${user_config.", "${CLAUDE_PLUGIN_", "${__dirname}")


def log(msg: str) -> None:
    print(f"twincat-mcp launcher: {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# locations
# ---------------------------------------------------------------------------
def default_src(script_path: Path) -> Path:
    """Where the server sources are, relative to this script.

    Plugin layout:  <root>/scripts/launch.py  with <root>/pyproject.toml
    Bundle layout:  <root>/server/launch.py   with <root>/server/src/pyproject.toml
    """
    here = script_path.resolve().parent
    for cand in (here.parent, here / "src", here):
        if (cand / "pyproject.toml").is_file() and (cand / PACKAGE_DIR).is_dir():
            return cand
    return here.parent


def default_venv(env: dict[str, str] | None = None, platform: str | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    platform = platform or sys.platform
    home = home or Path.home()
    if env.get("TCMCP_VENV"):
        return Path(env["TCMCP_VENV"])
    if env.get("CLAUDE_PLUGIN_DATA") and not _unresolved(env["CLAUDE_PLUGIN_DATA"]):
        return Path(env["CLAUDE_PLUGIN_DATA"]) / "venv"
    if platform == "win32" and env.get("LOCALAPPDATA"):
        return Path(env["LOCALAPPDATA"]) / "twincat-mcp" / "venv"
    return home / ".twincat_mcp" / "venv"


def venv_python(venv: Path, platform: str | None = None) -> Path:
    platform = platform or sys.platform
    return venv / ("Scripts/python.exe" if platform == "win32" else "bin/python")


# ---------------------------------------------------------------------------
# stamp: what is installed vs what is shipped
# ---------------------------------------------------------------------------
def source_digest(src: Path) -> str:
    h = hashlib.sha256()
    files = [src / "pyproject.toml", src / "requirements.txt"]
    files += sorted((src / PACKAGE_DIR).rglob("*.py"))
    for f in files:
        if f.is_file():
            h.update(str(f.relative_to(src)).replace("\\", "/").encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def shipped_version(src: Path) -> str:
    init = src / PACKAGE_DIR / "__init__.py"
    if init.is_file():
        for line in init.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    return "unknown"


def make_stamp(src: Path, base_python: str) -> dict:
    return {"version": shipped_version(src), "digest": source_digest(src),
            "src": str(src.resolve()), "base_python": base_python}


def read_stamp(venv: Path) -> dict | None:
    p = venv / STAMP_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_stamp(venv: Path, stamp: dict) -> None:
    (venv / STAMP_NAME).write_text(json.dumps(stamp, indent=1), encoding="utf-8")


def needs_install(venv: Path, stamp: dict) -> bool:
    old = read_stamp(venv)
    if old is None:
        return True
    return old.get("digest") != stamp["digest"] or old.get("version") != stamp["version"]


# ---------------------------------------------------------------------------
# environment hygiene
# ---------------------------------------------------------------------------
def _unresolved(value: str) -> bool:
    return any(m in value for m in UNRESOLVED_MARKERS)


def sanitize_env(env: dict[str, str]) -> dict[str, str]:
    """Drop variables an MCP client left unsubstituted (``${user_config.x}`` when the user
    typed nothing) and empty ``TCMCP_*`` variables, so the server's own defaults apply
    instead of a literal placeholder string."""
    out = {}
    for k, v in env.items():
        if _unresolved(v):
            continue
        if k.startswith("TCMCP_") and v.strip() == "":
            continue
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# venv + install
# ---------------------------------------------------------------------------
def _run_quiet(cmd: list[str], **kw) -> int:
    """Run a child and forward whatever it printed to our stderr (stdout is the MCP pipe)."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, **kw)
    except OSError as e:
        log(f"cannot run {cmd[0]}: {e}")
        return 127
    out = (proc.stdout or "") + (proc.stderr or "")
    if out.strip():
        sys.stderr.write(out if out.endswith("\n") else out + "\n")
        sys.stderr.flush()
    return proc.returncode


def venv_ok(venv: Path) -> bool:
    vp = venv_python(venv)
    if not vp.is_file():
        return False
    try:
        return subprocess.call([str(vp), "-c", "import sys, pip"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=60) == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def ensure_venv(venv: Path, base_python: str) -> Path:
    if venv_ok(venv):
        return venv_python(venv)
    if venv.exists():
        log(f"venv at {venv} is unusable (Python moved or upgraded?) — recreating")
        import shutil
        shutil.rmtree(venv, ignore_errors=True)
    venv.parent.mkdir(parents=True, exist_ok=True)
    log(f"creating venv {venv} with {base_python}")
    rc = _run_quiet([base_python, "-m", "venv", str(venv)])
    if rc != 0 or not venv_python(venv).is_file():
        log("venv creation failed. On Debian/Ubuntu: apt install python3-venv. On Windows: reinstall Python 64-bit from python.org.")
        raise SystemExit(4)
    return venv_python(venv)


def install(vpython: Path, src: Path, stamp: dict, venv: Path) -> None:
    log(f"installing twincat-mcp {stamp['version']} from {src} (first start or sources changed; needs PyPI access once)")
    _run_quiet([str(vpython), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--quiet", "--upgrade", "pip"])
    rc = _run_quiet([str(vpython), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--quiet",
                     "--progress-bar", "off", "--upgrade", str(src)])
    if rc != 0:
        log("pip install failed. Offline PC? Pre-download wheels on a connected machine: "
            f"`pip download -d wheels -r {src / 'requirements.txt'}` then set PIP_FIND_LINKS=<wheels> PIP_NO_INDEX=1 and retry.")
        raise SystemExit(4)
    write_stamp(venv, stamp)


# ---------------------------------------------------------------------------
# hand-over
# ---------------------------------------------------------------------------
def exec_server(vpython: Path, server_args: list[str], env: dict[str, str]) -> int:
    cmd = [str(vpython), "-m", PACKAGE_DIR, *server_args]
    if os.name != "nt":
        os.execve(cmd[0], cmd, env)  # never returns
    # Windows: no exec; stay as parent, inherit stdio handles, forward the exit code.
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 130


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    ap = argparse.ArgumentParser(prog="launch.py", description=__doc__.split("\n\n")[0], add_help=True)
    ap.add_argument("--src", help="directory holding pyproject.toml and twincat_mcp/ (default: auto next to this script)")
    ap.add_argument("--venv", help="virtualenv to create/use (default: see module docstring)")
    ap.add_argument("--reinstall", action="store_true", help="force pip install even if the stamp matches")
    ap.add_argument("--no-run", action="store_true", help="bootstrap only; do not start the server")
    ap.add_argument("--python", help="base interpreter for venv creation (default: the one running this script)")
    return ap.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    args, server_args = parse_args(sys.argv[1:] if argv is None else argv)
    if sys.version_info < MIN_PY:
        log(f"Python {sys.version.split()[0]} is too old; 3.10+ required (64-bit on Windows for TcAdsDll).")
        return 3
    src = Path(args.src).resolve() if args.src else default_src(Path(__file__))
    if not (src / "pyproject.toml").is_file() or not (src / PACKAGE_DIR).is_dir():
        log(f"no server sources at {src} (expected pyproject.toml and {PACKAGE_DIR}/). Pass --src.")
        return 2
    venv = Path(args.venv).resolve() if args.venv else default_venv()
    base_python = args.python or sys.executable

    vpython = ensure_venv(venv, base_python)
    stamp = make_stamp(src, base_python)
    if args.reinstall or needs_install(venv, stamp):
        install(vpython, src, stamp, venv)
    if args.no_run:
        log(f"ready: {vpython}")
        return 0
    env = sanitize_env(dict(os.environ))
    env.pop("PYTHONHOME", None)  # a stray PYTHONHOME breaks the venv interpreter
    return exec_server(vpython, server_args, env)


if __name__ == "__main__":
    raise SystemExit(main())
