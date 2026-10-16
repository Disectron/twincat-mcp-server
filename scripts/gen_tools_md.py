"""Regenerate TOOLS.md from the live server registry.

Class letter per tool: the strongest OpClass the tool's body passes to ``app.permit``/``audit``
(found by parsing server.py), else from its annotations (read-only → R, otherwise E).

    python scripts/gen_tools_md.py            # writes TOOLS.md
    python scripts/gen_tools_md.py --check    # exit 1 if TOOLS.md is stale (CI)
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SECTIONS = [
    ("tc_", "Meta & safety"), ("wf_", "Workflows (one call, whole chain)"), ("ads_", "Runtime — ADS"),
    ("rec_", "Real-time recorder"), ("test_", "Test bench"), ("snap_", "Snapshots"),
    ("ipc_", "Target OS — System Service"), ("xae_", "XAE Automation Interface"),
    ("exp_", "Experiments (define → run → evaluate → report)"),
    ("proj_", "Project files (offline)"), ("hmi_", "HMI bindings"), ("tcunit_", "TcUnit"),
    ("infosys_", "Beckhoff Infosys reference (TwinCAT 3 docs)"),
]
RANK = {"DESTRUCTIVE": 4, "CONTROL": 3, "WRITE": 2, "EDIT": 1}
LETTER = {"DESTRUCTIVE": "D", "CONTROL": "C", "WRITE": "W", "EDIT": "E"}


def op_classes_from_source() -> dict[str, list[str]]:
    """tool name -> OpClass names used in its body, weakest first (a tool may pick at runtime)."""
    tree = ast.parse((ROOT / "twincat_mcp" / "server.py").read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if not any(isinstance(d, ast.Call) and getattr(getattr(d.func, "attr", None), "lower", lambda: "")() == "tool"
                   for d in node.decorator_list):
            continue
        used = {sub.attr for sub in ast.walk(node)
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "OpClass"
                and sub.attr in RANK}
        if used:
            out[node.name] = sorted(used, key=RANK.__getitem__)
    return out


def first_sentence(text: str) -> str:
    """First paragraph of the docstring, whitespace-collapsed, capped at 320 chars on a word boundary."""
    para = (text or "").strip().split("\n\n")[0]
    t = " ".join(para.split())
    return t if len(t) <= 320 else t[:319].rsplit(" ", 1)[0] + "…"


def render() -> str:
    os.environ.setdefault("TCMCP_HOME", str(ROOT / ".tools-md-home"))
    from twincat_mcp import __version__
    from twincat_mcp.config import load
    from twincat_mcp.server import build_server

    server = build_server(load(None))
    tools = asyncio.run(server.list_tools())
    prompts = asyncio.run(server.list_prompts())
    resources = asyncio.run(server.list_resources())
    ops = op_classes_from_source()

    def letter(t) -> str:
        if t.name in ops:
            return "/".join(LETTER[o] for o in ops[t.name])
        return "R" if (t.annotations and t.annotations.read_only_hint) else "E"

    lines = [f"# twincat-mcp {__version__} — tool reference", "",
             f"{len(tools)} tools, {len(prompts)} prompts, {len(resources)} resources. Generated from the server "
             "(`python scripts/gen_tools_md.py`); one line per tool. Class: R = read-only, E = edit (files/XAE project, "
             "no effect on the running machine), W = write to the PLC (allow/deny lists), C = control (confirm in guarded), "
             "D = destructive (confirm unless TCMCP_CONFIRM=0 in full).", ""]
    seen = set()
    for prefix, title in SECTIONS:
        group = [t for t in tools if t.name.startswith(prefix)]
        if not group:
            continue
        lines.append(f"## {title} ({len(group)})")
        lines.append("")
        for t in group:
            seen.add(t.name)
            lines.append(f"- `{t.name}` [{letter(t)}] — {first_sentence(t.description)}")
        lines.append("")
    rest = [t for t in tools if t.name not in seen]
    if rest:
        lines.append(f"## Other ({len(rest)})")
        lines.append("")
        lines += [f"- `{t.name}` [{letter(t)}] — {first_sentence(t.description)}" for t in rest]
        lines.append("")
    lines.append(f"## Prompts ({len(prompts)})")
    lines.append("")
    for p in prompts:
        args = ", ".join(a.name for a in (p.arguments or []))
        lines.append(f"- `{p.name}`" + (f"({args})" if args else "") + f" — {first_sentence(p.description)}")
    lines.append("")
    lines.append(f"## Resources ({len(resources)})")
    lines.append("")
    lines += [f"- `{r.uri}` — {first_sentence(r.description or r.name)}" for r in resources]
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    text = render()
    target = ROOT / "TOOLS.md"
    if a.check:
        if target.read_text(encoding="utf-8") != text:
            print("TOOLS.md is stale — run python scripts/gen_tools_md.py", file=sys.stderr)
            return 1
        return 0
    target.write_text(text, encoding="utf-8")
    print(f"wrote {target} ({text.count(chr(10))} lines)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
