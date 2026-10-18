"""twincat-mcp builder stub: runs the knowledge-folder builder that the installed twincat plugin ships.

This file sits in <kb>\\tools\\ under the builder's own name (infosys_local.py, manuals_db.py or esi_db.py) and never
needs updating: it finds the plugin's current builders and runs the one with its own file name, with the same
arguments and the same Python. Commands and output are those of the builder (python infosys_local.py stats, ...).

Where the builders are found, first match wins:
  1. TCMCP_BUILDERS (a folder holding infosys_local.py / manuals_db.py / esi_db.py)
  2. the location the twincat MCP server records at every start: <TCMCP_HOME or ~/.twincat_mcp>/plugin_location.json
  3. the newest twincat-mcp version in the Claude Code plugin cache (~/.claude/plugins/cache/*/twincat-mcp/<version>)
The knowledge folder is the builder's usual one (--kb, TCMCP_KNOWLEDGE_DIR, kb_set_location, ...) with "the folder above
the script" meaning the folder above this stub.
"""
import json
import os
import re
import runpy
import sys
from pathlib import Path

STUB = "twincat-mcp builder stub"
STUB_VERSION = "2"
NAMES = ("infosys_local.py", "manuals_db.py", "esi_db.py")


def _version_key(p):
    parts = re.findall(r"\d+", p.name)
    return tuple(int(x) for x in parts) if parts else (-1,)


def find_builders(name):
    """-> (folder, how it was found) or (None, what was tried)."""
    tried = []
    env = os.environ.get("TCMCP_BUILDERS")
    if env:
        if (Path(env) / name).is_file():
            return Path(env), "TCMCP_BUILDERS"
        tried.append("TCMCP_BUILDERS=" + env)
    home = Path(os.environ.get("TCMCP_HOME") or (Path.home() / ".twincat_mcp"))
    pointer = home / "plugin_location.json"
    try:
        d = json.loads(pointer.read_text(encoding="utf-8"))
        b = Path(d.get("builders") or "")
        if d.get("builders") and (b / name).is_file():
            return b, "plugin_location.json (twincat plugin " + str(d.get("version") or "?") + ")"
        tried.append(str(pointer) + " (points to " + str(d.get("builders")) + ")")
    except (OSError, ValueError):
        tried.append(str(pointer) + " (none)")
    cache = Path.home() / ".claude" / "plugins" / "cache"
    found = sorted((p for p in cache.glob("*/twincat-mcp/*") if (p / "skills" / "twincat-docs" / "builders" / name).is_file()),
                   key=_version_key)
    if found:
        return found[-1] / "skills" / "twincat-docs" / "builders", "plugin cache (" + found[-1].name + ")"
    tried.append(str(cache) + "/*/twincat-mcp/*")
    return None, "; ".join(tried)


def main():
    me = Path(__file__).resolve()
    name = me.name if me.name in NAMES else None
    if name is None:
        sys.exit("twincat-mcp builder stub: name this file infosys_local.py, manuals_db.py or esi_db.py (it is "
                 + me.name + ")")
    folder, how = find_builders(name)
    if folder is None:
        sys.exit("twincat-mcp builder stub: the twincat plugin's " + name + " was not found (tried: " + how + "). "
                 "Install or update the twincat plugin, start it once, or set TCMCP_BUILDERS to its "
                 "skills\\twincat-docs\\builders folder.")
    target = (folder / name).resolve()
    if target == me:
        sys.exit("twincat-mcp builder stub: " + str(target) + " is this stub itself, not the plugin's builder")
    os.environ["TCMCP_STUB_DIR"] = str(me.parent)          # the builder's "folder above the script" = above this stub
    if os.environ.get("TCMCP_STUB_VERBOSE"):
        sys.stderr.write("twincat-mcp builder stub: running " + str(target) + " (" + how + ")\n")
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
