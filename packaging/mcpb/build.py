"""Build the Claude Desktop bundle: dist/twincat-mcp-<version>.mcpb

An .mcpb is a zip with ``manifest.json`` at its root (spec: github.com/anthropics/mcpb).
Claude Desktop installs it by drag-and-drop (Settings → Extensions) and runs
``server.mcp_config`` — here ``cmd /d /c server\\launch.cmd``, which finds a Python 3.12+,
creates a private venv under %LOCALAPPDATA%\\twincat-mcp on first start, installs the
sources shipped in ``server/src`` and hands stdio to ``python -m twincat_mcp``.

Why not bundle the dependencies in server/lib as the spec suggests? pydantic-core and
pywin32 are compiled per Python minor version; a bundle pinned to one minor version breaks on the next.
The venv bootstrap costs one pip run per PC and works with the 64-bit Python 3.12 that is there.

Usage:  python packaging/mcpb/build.py [--out DIR] [--no-validate]
The tool/prompt lists in the manifest are generated from the live server registry so
they never drift from the code.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PKG = "twincat_mcp"

TOP_FILES = ["README.md", "LICENSE", "TOOLS.md", "twincat_mcp.toml.example"]
SRC_FILES = ["pyproject.toml", "requirements.txt", "README.md", "LICENSE"]


def version() -> str:
    for line in (ROOT / PKG / "__init__.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("no __version__")


def registry() -> tuple[list[dict], list[dict]]:
    """Tool and prompt lists straight from the server, so the manifest cannot drift."""
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("TCMCP_HOME", str(HERE / ".build-home"))
    from twincat_mcp.config import load
    from twincat_mcp.server import build_server

    server = build_server(load(None))
    tools = asyncio.run(server.list_tools())
    prompts = asyncio.run(server.list_prompts())
    def one_line(s: str | None, n: int = 200) -> str:
        s = " ".join((s or "").split())
        return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"

    t = [{"name": x.name, "description": one_line(x.description)} for x in tools]
    p = []
    for x in prompts:
        args = [a.name for a in (x.arguments or [])]
        entry = {"name": x.name, "description": one_line(x.description), "arguments": args,
                 # MCPB wants a static text for prompts; ours are server-rendered, so point at the server.
                 "text": f"Use the MCP prompt '{x.name}' from the twincat server" +
                         (" with " + ", ".join(f"{a}=${{arguments.{a}}}" for a in args) if args else "")}
        p.append(entry)
    shutil.rmtree(HERE / ".build-home", ignore_errors=True)
    return t, p


def render_manifest(ver: str, tools: list[dict], prompts: list[dict]) -> dict:
    tpl = json.loads((HERE / "manifest.template.json").read_text(encoding="utf-8"))
    tpl["version"] = ver
    tpl["tools"] = tools
    tpl["prompts"] = prompts
    missing = [k for k in ("manifest_version", "name", "version", "description", "author", "server") if k not in tpl]
    if missing:
        raise SystemExit(f"manifest template missing required keys: {missing}")
    return tpl


def bundle_members() -> list[tuple[Path, str]]:
    """(source path, archive name) pairs."""
    members: list[tuple[Path, str]] = []
    for f in TOP_FILES:
        if (ROOT / f).is_file():
            members.append((ROOT / f, f))
    members.append((ROOT / "scripts" / "launch.cmd", "server/launch.cmd"))
    members.append((ROOT / "scripts" / "launch.py", "server/launch.py"))
    for f in SRC_FILES:
        members.append((ROOT / f, f"server/src/{f}"))
    for py in sorted((ROOT / PKG).rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        members.append((py, "server/src/" + py.relative_to(ROOT).as_posix()))
    # native capture service (tcmcp-capture.exe, built by CI from native/capture) when present
    for exe in sorted((ROOT / PKG / "bin").glob("tcmcp-capture*")):
        members.append((exe, "server/src/" + exe.relative_to(ROOT).as_posix()))
    return members


def build(out_dir: Path, validate: bool = True) -> Path:
    ver = version()
    tools, prompts = registry()
    manifest = render_manifest(ver, tools, prompts)
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = out_dir / f"mcpb-stage-{ver}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for src, name in bundle_members():
        dst = stage / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    if validate:
        _validate(stage / "manifest.json")

    target = out_dir / f"twincat-mcp-{ver}.mcpb"
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(stage.rglob("*")):
            if p.is_file():
                zf.write(p, p.relative_to(stage).as_posix())
    shutil.rmtree(stage, ignore_errors=True)
    print(f"built {target} ({target.stat().st_size // 1024} KiB, {len(tools)} tools, {len(prompts)} prompts)", file=sys.stderr)
    return target


def _validate(manifest: Path) -> None:
    """Official validator when npx is around; silent skip otherwise (it is advisory)."""
    npx = shutil.which("npx")
    if not npx:
        print("npx not found — skipping `mcpb validate` (install Node to run it)", file=sys.stderr)
        return
    try:
        r = subprocess.run([npx, "-y", "@anthropic-ai/mcpb", "validate", str(manifest)],
                           capture_output=True, text=True, timeout=240)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"mcpb validate skipped: {e}", file=sys.stderr)
        return
    out = (r.stdout + r.stderr).strip()
    print(out or f"mcpb validate exit {r.returncode}", file=sys.stderr)
    if r.returncode != 0:
        raise SystemExit("manifest failed `mcpb validate`")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--no-validate", action="store_true", help="skip npx @anthropic-ai/mcpb validate")
    a = ap.parse_args(argv)
    build(Path(a.out), validate=not a.no_validate)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
