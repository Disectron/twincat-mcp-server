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

First start and the client's connect timeout: an MCP client gives the server about 30 s to answer
the handshake, and ``pip install`` takes longer. So when the venv is missing or stale and this
script was started *as the server* (no ``--no-run``), it does not install inline: it starts the
install as a detached background process, prints where its log is, and exits with code 5. The
client reports a failed server; a reconnect (``/mcp``) after the install has finished starts in
about two seconds. ``--no-run`` installs synchronously (pre-install step, CI, the plugin's
SessionStart hook with ``--background`` spawns the same detached install and returns at once).
A lock file next to the venv keeps two concurrent installs apart.

Exit codes: 0 server exited cleanly; 2 bad arguments; 3 Python too old; 4 venv/pip failure;
5 install running in the background — reconnect later.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 12)
STAMP_NAME = ".twincat-mcp-stamp.json"
LOCK_NAME = ".twincat-mcp-install.lock"
INSTALL_LOG = "twincat-mcp-install.log"
LOCK_STALE_S = 20 * 60
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
    files += sorted((src / PACKAGE_DIR / "bin").glob("tcmcp-capture*"))   # native capture service ships as package data
    for f in files:
        if f.is_file():
            h.update(str(f.relative_to(src)).replace("\\", "/").encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def quick_fingerprint(src: Path) -> str:
    """Names, sizes and mtimes only — no file is read. Matches the stamp → nothing changed, skip hashing (P13)."""
    h = hashlib.sha256()
    files = [src / "pyproject.toml", src / "requirements.txt"]
    files += sorted((src / PACKAGE_DIR).rglob("*.py"))
    files += sorted((src / PACKAGE_DIR / "bin").glob("tcmcp-capture*"))   # native capture service ships as package data
    for f in files:
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.relative_to(src).as_posix()}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return h.hexdigest()


def shipped_version(src: Path) -> str:
    init = src / PACKAGE_DIR / "__init__.py"
    if init.is_file():
        for line in init.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip("\"'")
    return "unknown"


def make_stamp(src: Path, base_python: str) -> dict:
    return {"version": shipped_version(src), "digest": source_digest(src), "quick": quick_fingerprint(src),
            "src": str(src.resolve()), "base_python": base_python}


class LazyStamp(dict):
    """version + quick fingerprint up front; the content digest only when someone asks for it."""
    def __init__(self, src: Path, base_python: str):
        super().__init__(version=shipped_version(src), quick=quick_fingerprint(src), src=str(src.resolve()),
                         base_python=base_python)
        self._src = src

    def __getitem__(self, k):
        if k == "digest" and not dict.__contains__(self, "digest"):
            dict.__setitem__(self, "digest", source_digest(self._src))
        return dict.__getitem__(self, k)

    def full(self) -> dict:
        self["digest"]
        return dict(self)


def read_stamp(venv: Path) -> dict | None:
    p = venv / STAMP_NAME
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_stamp(venv: Path, stamp: dict) -> None:
    data = stamp.full() if hasattr(stamp, "full") else stamp
    (venv / STAMP_NAME).write_text(json.dumps(data, indent=1), encoding="utf-8")


def needs_install(venv: Path, stamp: dict) -> bool:
    old = read_stamp(venv)
    if old is None:
        return True
    if old.get("version") != stamp["version"]:
        return True
    if old.get("quick") and old.get("quick") == stamp.get("quick"):
        return False                      # same files, same sizes, same mtimes: no hashing needed
    return old.get("digest") != stamp["digest"]


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


def venv_health(venv: Path, timeout_s: float = 60) -> str:
    """'ok': the interpreter starts, has pip and is new enough. 'unusable': it is missing, has no pip, or is older than
    3.12 (a venv an earlier plugin built on 3.10 / 3.11) — a definite answer. 'unknown': it did not answer in time or
    could not be started (cold disk, virus scanner) — no reason to rebuild anything (N46)."""
    vp = venv_python(venv)
    if not vp.is_file():
        return "unusable"
    check = f"import sys, pip; sys.exit(0 if sys.version_info >= {MIN_PY!r} else 3)"
    try:
        rc = subprocess.call([str(vp), "-c", check], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return "unknown"
    except OSError:
        return "unknown"
    return "ok" if rc == 0 else "unusable"


def venv_ok(venv: Path) -> bool:
    return venv_health(venv) == "ok"


PENDING = ".twincat-mcp-swap-pending"


def _builds(venv: Path) -> list[Path]:
    return sorted(venv.parent.glob(venv.name + ".build-*"))


def _pending(venv: Path) -> list[Path]:
    """Finished builds marked pending, oldest install first by the stamp's 'built' time (the name's time is when the
    build STARTED: of two overlapping installs the later-started one can finish first) — whether one is newer than
    `venv` is _supersedes'."""
    pend = [b for b in _builds(venv) if (b / PENDING).is_file() and (b / STAMP_NAME).is_file() and venv_python(b).is_file()]
    return sorted(pend, key=lambda b: (_built(b), b.name))


def _built(v: Path) -> float:
    """When the venv's install finished (stamp 'built'); 0 for a venv without it (written by an older launcher)."""
    try:
        return float((read_stamp(v) or {}).get("built") or 0)
    except (TypeError, ValueError):
        return 0.0


def _supersedes(venv: Path, build: Path) -> bool:
    """A pending build replaces (and runs instead of) `venv` only when it is newer than what is in place, or nothing
    usable is there. Without this, a build left pending by an earlier install was put back over a newer install —
    the fresh venv retired and deleted, the old code running (review A)."""
    if not venv_python(venv).is_file() or read_stamp(venv) is None:
        return True
    return _built(build) > _built(venv)


def _live_pending(venv: Path) -> list[Path]:
    return [b for b in _pending(venv) if _supersedes(venv, b)]


def _unmark(build: Path) -> None:
    """No longer pending: never run, never swapped in (removable while another window runs from it — the build itself
    may not be renamable then, the marker is)."""
    try:
        (build / PENDING).unlink()
    except OSError:
        pass


def active_venv(venv: Path) -> Path:
    """The venv to run: normally `venv`; the newest finished build that is newer than `venv` while its swap is
    pending (another window still runs the old server from `venv`, so `venv` could not be renamed)."""
    pend = _live_pending(venv)
    return pend[-1] if pend else venv


def _retire(path: Path) -> Path | None:
    """Rename out of the way as `<venv>.old-…` (removed later) -> the new path; None when it is in use."""
    import time
    old = path.with_name(f"{path.name.split('.build-')[0]}.old-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-"
                         f"{abs(hash(path.name)) % 10000}")
    try:
        os.rename(path, old)
        return old
    except OSError:
        return None


def swap_in(venv: Path, build: Path) -> bool:
    """Put a finished build in place of `venv`: `venv` → `venv.old-…`, build → `venv`. N46: a live venv is never
    deleted or changed in place (rmtree of a venv another Claude window ran broke that server and the rebuild failed
    at every start). When `venv` cannot be renamed (files in use on Windows) the build stays beside it, marked
    pending, and is the one that runs until a later start can swap. When the build cannot be moved after `venv` was
    retired, `venv` is moved back — it is never left missing. -> swapped?"""
    retired = None
    if venv.exists():
        retired = _retire(venv)
        if retired is None:
            (build / PENDING).write_text("venv in use\n", encoding="utf-8")
            log(f"{venv} is in use (another window runs the server from it); the new build {build.name} is used until "
                "a later start can put it in place")
            return False
    try:
        os.rename(build, venv)
    except OSError as exc:
        if retired is not None:
            try:
                os.rename(retired, venv)
            except OSError as back:
                log(f"could not move {retired} back to {venv}: {back}")
        (build / PENDING).write_text(f"{exc}\n", encoding="utf-8")
        log(f"could not move {build} to {venv}: {exc}; {build.name} is used for now")
        return False
    _unmark(venv)
    return True


def tidy(venv: Path, lock_free: bool) -> None:
    """At every start: the newest pending build that is newer than `venv` goes in place when it can; every other
    pending build is superseded — unmarked and retired; unfinished builds (only when no install holds the lock) and
    replaced venvs (`.old-*`) are retired or removed — best effort, anything in use stays for a later start."""
    import shutil
    pend = _pending(venv)
    live = _live_pending(venv)
    keep = live[-1] if live and not swap_in(venv, live[-1]) else None
    for b in pend:
        if b != keep:
            _unmark(b)
    for b in _builds(venv):
        if b == keep:
            continue
        # superseded pending builds always; anything else only while no install runs (it may be that install's own)
        if b in pend or lock_free:
            _retire(b)
    for old in venv.parent.glob(venv.name + ".old-*"):
        shutil.rmtree(old, ignore_errors=True)


def build_new_venv(venv: Path, base_python: str) -> Path:
    """A fresh venv beside the live one (`venv.build-<time>-<pid>`); nothing that exists is touched. -> its python."""
    import time
    b = venv.with_name(f"{venv.name}.build-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    log(f"creating venv {b} with {base_python}")
    rc = _run_quiet([base_python, "-m", "venv", str(b)])
    if rc != 0 or not venv_python(b).is_file():
        log("venv creation failed. On Debian/Ubuntu: apt install python3-venv. On Windows: reinstall Python 64-bit from python.org.")
        raise SystemExit(4)
    return venv_python(b)


def install(vpython: Path, src: Path, stamp: dict, venv: Path) -> None:
    log(f"installing twincat-mcp {stamp['version']} from {src} (first start or sources changed; needs PyPI access once)")
    _run_quiet([str(vpython), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--quiet", "--upgrade", "pip"])
    rc = _run_quiet([str(vpython), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--quiet",
                     "--progress-bar", "off", "--upgrade", str(src)])
    if rc != 0:
        log("pip install failed. Offline PC? Pre-download wheels on a connected machine: "
            f"`pip download -d wheels -r {src / 'requirements.txt'}` then set PIP_FIND_LINKS=<wheels> PIP_NO_INDEX=1 and retry.")
        raise SystemExit(4)
    import time
    data = stamp.full() if hasattr(stamp, "full") else dict(stamp)
    write_stamp(venv, {**data, "built": time.time()})   # orders builds: an older pending build never replaces this


# ---------------------------------------------------------------------------
# install lock and detached background install
# ---------------------------------------------------------------------------
def lock_path(venv: Path) -> Path:
    return venv.parent / LOCK_NAME


def install_log_path(venv: Path) -> Path:
    return venv.parent / INSTALL_LOG


def lock_age_s(venv: Path, now: float | None = None) -> float | None:
    """Seconds since the lock was written, None when there is no (fresh) lock."""
    import time
    lp = lock_path(venv)
    if not lp.is_file():
        return None
    try:
        data = json.loads(lp.read_text(encoding="utf-8"))
        age = (time.time() if now is None else now) - float(data.get("ts", 0))
    except (OSError, ValueError, TypeError):
        return None
    if age > LOCK_STALE_S or age < 0:
        try:
            lp.unlink()
        except OSError:
            pass
        return None
    return age


def take_lock(venv: Path) -> bool:
    import time
    if lock_age_s(venv) is not None:
        return False
    venv.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_path(venv).write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}), encoding="utf-8")
    except OSError:
        return False
    return True


def release_lock(venv: Path) -> None:
    try:
        lock_path(venv).unlink()
    except OSError:
        pass


def spawn_background_install(src: Path, venv: Path, base_python: str) -> Path:
    """Start ``launch.py --no-run`` detached from this process; returns the log file it writes to."""
    logf = install_log_path(venv)
    venv.parent.mkdir(parents=True, exist_ok=True)
    cmd = [base_python, str(Path(__file__).resolve()), "--src", str(src), "--venv", str(venv), "--no-run", "--python", base_python,
           "--lock-held"]   # the caller took the lock for this child; the child releases it when done
    fh = open(logf, "ab")
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": fh, "stderr": fh, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    fh.close()
    return logf


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
    ap.add_argument("--background", action="store_true",
                    help="with --no-run: start the install detached and return at once (SessionStart hook)")
    ap.add_argument("--sync-install", action="store_true",
                    help="install inline even when started as the server (clients with a long connect timeout)")
    ap.add_argument("--lock-held", action="store_true", help=argparse.SUPPRESS)   # internal: spawned by a lock holder
    return ap.parse_known_args(argv)


def main(argv: list[str] | None = None) -> int:
    args, server_args = parse_args(sys.argv[1:] if argv is None else argv)
    if sys.version_info < MIN_PY:
        log(f"Python {sys.version.split()[0]} ({sys.executable}) is too old; 3.12+ required (64-bit on Windows for "
            "TcAdsDll). Point the plugin's 'python' setting (TCMCP_PYTHON for the .mcpb) at a Python 3.12 python.exe.")
        return 3
    src = Path(args.src).resolve() if args.src else default_src(Path(__file__))
    if not (src / "pyproject.toml").is_file() or not (src / PACKAGE_DIR).is_dir():
        log(f"no server sources at {src} (expected pyproject.toml and {PACKAGE_DIR}/). Pass --src.")
        return 2
    venv = Path(args.venv).resolve() if args.venv else default_venv()
    base_python = args.python or sys.executable
    stamp = LazyStamp(src, base_python)
    tidy(venv, lock_free=lock_age_s(venv) is None)
    run_from = active_venv(venv)
    health = venv_health(run_from)
    if health == "unknown":
        log(f"the venv at {run_from} did not answer the health check in time — using it as it is (no rebuild on a "
            "timeout)")
    must_install = args.reinstall or health == "unusable" or needs_install(run_from, stamp)

    if must_install and not args.no_run and not args.sync_install:
        # started as the MCP server: do not make the client wait through pip
        age = lock_age_s(venv)
        if age is not None:
            log(f"first-start install is running in the background for {age:.0f} s (log: {install_log_path(venv)}). "
                f"Reconnect in a minute (/mcp in Claude Code).")
            return 5
        if take_lock(venv):
            logf = spawn_background_install(src, venv, base_python)
            log(f"first start: building the private venv in the background (1-2 min, needs PyPI once). "
                f"Log: {logf}. Reconnect when it is done (/mcp in Claude Code); later starts take ~2 s.")
            return 5
        log("could not create the install lock; falling back to an inline install")

    if must_install and args.no_run and args.background:
        if lock_age_s(venv) is not None:
            log("install already running")
            return 0
        if take_lock(venv):
            logf = spawn_background_install(src, venv, base_python)
            log(f"install started in the background (log: {logf})")
        return 0

    if must_install:
        own_lock = True if args.lock_held else take_lock(venv)
        if not own_lock and not args.reinstall:
            # someone else is installing right now (the hook, or a previous start): wait for it
            import time
            log("another install is running; waiting for it")
            for _ in range(LOCK_STALE_S // 5):
                time.sleep(5)
                if lock_age_s(venv) is None:
                    break
            run_from = active_venv(venv)
            if not needs_install(run_from, make_stamp(src, base_python)) and venv_health(run_from) != "unusable":
                must_install = False
        if must_install:
            try:
                vpython = build_new_venv(venv, base_python)
                build = vpython.parent.parent
                install(vpython, src, stamp, build)
                swap_in(venv, build)
                tidy(venv, lock_free=False)
            finally:
                if own_lock:
                    release_lock(venv)
        run_from = active_venv(venv)
    vpython = venv_python(run_from)
    if not vpython.is_file():
        log(f"no interpreter at {vpython} after install")
        return 4
    if args.no_run:
        log(f"ready: {vpython}")
        return 0
    env = sanitize_env(dict(os.environ))
    env.pop("PYTHONHOME", None)  # a stray PYTHONHOME breaks the venv interpreter
    # the server runs from the venv's site-packages; files beside the package (skills/) are only found from here
    env["TCMCP_PLUGIN_SRC"] = str(src.resolve())
    return exec_server(vpython, server_args, env)


if __name__ == "__main__":
    raise SystemExit(main())
