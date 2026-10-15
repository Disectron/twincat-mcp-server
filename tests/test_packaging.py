"""Packaging: bootstrap launcher, Claude Code plugin manifests, Claude Desktop .mcpb bundle.

These guard the two one-click install paths against drift: a renamed script, a user_config
key referenced but not declared, a version bump forgotten in one of three places.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG_VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', (ROOT / "twincat_mcp" / "__init__.py").read_text()).group(1)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


launch = _load("tc_launch", ROOT / "scripts" / "launch.py")
mcpb_build = _load("tc_mcpb_build", ROOT / "packaging" / "mcpb" / "build.py")


# ---------------------------------------------------------------------------
# launcher
# ---------------------------------------------------------------------------
def _fake_src(tmp: Path, layout: str) -> tuple[Path, Path]:
    """Return (script_path, src_dir) for the plugin or bundle layout."""
    if layout == "plugin":
        src = tmp / "root"
        script = src / "scripts" / "launch.py"
    else:
        src = tmp / "root" / "server" / "src"
        script = tmp / "root" / "server" / "launch.py"
    (src / "twincat_mcp").mkdir(parents=True)
    (src / "pyproject.toml").write_text('[project]\nname = "twincat-mcp"\n')
    (src / "requirements.txt").write_text("mcp\n")
    (src / "twincat_mcp" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    (src / "twincat_mcp" / "a.py").write_text("x = 1\n")
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# launcher stub\n")
    return script, src


@pytest.mark.parametrize("layout", ["plugin", "bundle"])
def test_launch_default_src_finds_both_layouts(tmp_path, layout):
    script, src = _fake_src(tmp_path, layout)
    assert launch.default_src(script) == src.resolve()


def test_launch_default_venv_precedence(tmp_path):
    home = tmp_path / "home"
    assert launch.default_venv({"TCMCP_VENV": "/x/v"}, "win32", home) == Path("/x/v")
    assert launch.default_venv({"CLAUDE_PLUGIN_DATA": "/d"}, "linux", home) == Path("/d/venv")
    # an unsubstituted placeholder must not become a directory name
    assert launch.default_venv({"CLAUDE_PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}", "LOCALAPPDATA": "C:/la"}, "win32", home) \
        == Path("C:/la") / "twincat-mcp" / "venv"
    assert launch.default_venv({}, "linux", home) == home / ".twincat_mcp" / "venv"
    assert launch.venv_python(Path("v"), "win32") == Path("v/Scripts/python.exe")
    assert launch.venv_python(Path("v"), "linux") == Path("v/bin/python")


def test_launch_sanitize_env_drops_placeholders_and_empty_tcmcp():
    env = {"TCMCP_MODE": "${user_config.mode}", "TCMCP_AMS_NET_ID": "", "TCMCP_SOLUTION": "C:/a.sln",
           "PATH": "/bin", "OTHER": "", "X": "${__dirname}/y"}
    out = launch.sanitize_env(env)
    assert out == {"TCMCP_SOLUTION": "C:/a.sln", "PATH": "/bin", "OTHER": ""}


def test_launch_stamp_tracks_source_changes(tmp_path):
    _, src = _fake_src(tmp_path, "plugin")
    venv = tmp_path / "venv"
    venv.mkdir()
    stamp = launch.make_stamp(src, "python")
    assert stamp["version"] == "9.9.9"
    assert launch.needs_install(venv, stamp)
    launch.write_stamp(venv, stamp)
    assert not launch.needs_install(venv, launch.make_stamp(src, "python"))
    (src / "twincat_mcp" / "a.py").write_text("x = 2\n")
    assert launch.needs_install(venv, launch.make_stamp(src, "python"))


def test_launch_main_installs_once_then_skips(tmp_path, monkeypatch):
    script, src = _fake_src(tmp_path, "plugin")
    venv = tmp_path / "venv"
    calls: list[list[str]] = []

    def fake_run_quiet(cmd, **kw):
        calls.append(cmd)
        if cmd[1:3] == ["-m", "venv"]:
            vp = launch.venv_python(venv)
            vp.parent.mkdir(parents=True, exist_ok=True)
            vp.write_text("")
        return 0

    monkeypatch.setattr(launch, "_run_quiet", fake_run_quiet)
    monkeypatch.setattr(launch, "venv_ok", lambda v: launch.venv_python(v).exists())
    executed: list[tuple] = []
    monkeypatch.setattr(launch, "exec_server", lambda vp, args, env: executed.append((vp, args, env)) or 0)
    monkeypatch.setenv("TCMCP_MODE", "${user_config.mode}")

    assert launch.main(["--src", str(src), "--venv", str(venv), "--log", "DEBUG"]) == 0
    assert any(c[1:3] == ["-m", "venv"] for c in calls)
    assert any("install" in c and str(src) in c for c in calls)
    assert executed[0][1] == ["--log", "DEBUG"], "unknown args pass through to the server"
    assert "TCMCP_MODE" not in executed[0][2]
    assert (venv / launch.STAMP_NAME).is_file()

    calls.clear()
    assert launch.main(["--src", str(src), "--venv", str(venv), "--no-run"]) == 0
    assert not any("install" in c and str(src) in c for c in calls), "second start must not reinstall"
    assert launch.main(["--src", str(src), "--venv", str(venv), "--no-run", "--reinstall"]) == 0
    assert any("install" in c and str(src) in c for c in calls)


def test_launch_main_rejects_missing_sources(tmp_path):
    assert launch.main(["--src", str(tmp_path), "--no-run"]) == 2


# ---------------------------------------------------------------------------
# Claude Code plugin
# ---------------------------------------------------------------------------
PLUGIN = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
MARKET = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
_UC = re.compile(r"\$\{user_config\.([A-Za-z0-9_]+)\}")


def test_plugin_manifest_shape():
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", PLUGIN["name"])
    assert "claude" not in PLUGIN["name"] and "anthropic" not in PLUGIN["name"]
    assert PLUGIN["version"] == PKG_VERSION, "bump .claude-plugin/plugin.json with the package"
    assert PLUGIN["author"]["name"]
    assert set(PLUGIN["mcpServers"]) == {"twincat"}
    srv = PLUGIN["mcpServers"]["twincat"]
    assert srv["command"] == "${user_config.python_exe}"
    root_refs = [a for a in srv["args"] if a.startswith("${CLAUDE_PLUGIN_ROOT}")]
    for ref in root_refs:
        rel = ref[len("${CLAUDE_PLUGIN_ROOT}"):].lstrip("/")
        assert (ROOT / rel).exists() if rel else True, ref
    assert "--src" in srv["args"]


def test_plugin_user_config_references_are_declared():
    blob = json.dumps(PLUGIN["mcpServers"])
    referenced = set(_UC.findall(blob))
    declared = set(PLUGIN["userConfig"])
    assert referenced <= declared, referenced - declared
    for key, opt in PLUGIN["userConfig"].items():
        assert opt["type"] in {"string", "number", "boolean", "directory", "file"}
        assert opt["title"] and opt["description"]
        assert "options" not in opt, "options needs Claude Code ≥2.1.271 and breaks older loads"
        assert set(opt) <= {"type", "title", "description", "required", "default", "multiple", "sensitive", "min", "max"}, key
    # defaults so the server starts before the user visits /plugin configure
    assert PLUGIN["userConfig"]["python_exe"]["default"] == "python"
    assert PLUGIN["userConfig"]["mode"]["default"] == "guarded"


def test_plugin_root_layout():
    assert (ROOT / "skills" / "twincat-mcp" / "SKILL.md").is_file()
    head = (ROOT / "skills" / "twincat-mcp" / "SKILL.md").read_text(encoding="utf-8")[:400]
    assert head.startswith("---\nname: twincat-mcp\n")
    assert not (ROOT / ".mcp.json").exists(), ".mcp.json at the plugin root is auto-loaded as a plugin server"
    assert not (ROOT / "CLAUDE.md").exists(), "CLAUDE.md at plugin root is not loaded and trips validate"
    assert not (ROOT / "bin").exists(), "bin/ blocks install on claude.ai / Cowork"


def test_marketplace_manifest():
    assert MARKET["name"] and MARKET["owner"]["name"]
    assert len(MARKET["plugins"]) == 1
    entry = MARKET["plugins"][0]
    assert entry["name"] == PLUGIN["name"]
    assert entry["source"].startswith("./") and ".." not in entry["source"]
    assert "version" not in entry, "plugin.json owns the version; a second copy drifts"
    assert MARKET["metadata"]["version"] == PKG_VERSION


# ---------------------------------------------------------------------------
# .mcpb bundle
# ---------------------------------------------------------------------------
TEMPLATE = json.loads((ROOT / "packaging" / "mcpb" / "manifest.template.json").read_text(encoding="utf-8"))


def test_mcpb_template_shape():
    for k in ("manifest_version", "name", "version", "description", "author", "server"):
        assert k in TEMPLATE
    assert TEMPLATE["manifest_version"] == "0.3"
    srv = TEMPLATE["server"]
    assert srv["type"] == "python"
    cfg = srv["mcp_config"]
    assert cfg["command"] == "cmd" and cfg["args"][:2] == ["/d", "/c"]
    assert cfg["args"][2].endswith("launch.cmd") and "${__dirname}" in cfg["args"][2]
    assert TEMPLATE["compatibility"]["platforms"] == ["win32"]
    referenced = set(_UC.findall(json.dumps(cfg)))
    declared = set(TEMPLATE["user_config"])
    assert referenced <= declared, referenced - declared
    for key, opt in TEMPLATE["user_config"].items():
        assert opt["type"] in {"string", "number", "boolean", "directory", "file"}, key


def test_mcpb_bundle_members_exist_and_cover_package():
    members = mcpb_build.bundle_members()
    names = {n for _, n in members}
    for src, _ in members:
        assert src.is_file(), src
    assert {"manifest.json"} & names == set(), "manifest is rendered, not copied"
    assert {"server/launch.cmd", "server/launch.py", "server/src/pyproject.toml", "server/src/requirements.txt",
            "server/src/twincat_mcp/__init__.py", "server/src/twincat_mcp/server.py"} <= names
    on_disk = {"server/src/" + p.relative_to(ROOT).as_posix()
               for p in (ROOT / "twincat_mcp").rglob("*.py") if "__pycache__" not in p.parts}
    assert on_disk <= names
    assert not any("__pycache__" in n or n.endswith(".pyc") for n in names)


def test_mcpb_build_produces_installable_zip(tmp_path):
    out = mcpb_build.build(tmp_path, validate=False)
    assert out.name == f"twincat-mcp-{PKG_VERSION}.mcpb"
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "manifest.json" in names
        m = json.loads(zf.read("manifest.json"))
    assert m["version"] == PKG_VERSION
    assert m["tools_generated"] is True and len(m["tools"]) >= 100
    assert {t["name"] for t in m["tools"]} >= {"tc_doctor", "ads_read", "rec_start", "xae_build", "proj_get_code"}
    assert all(t["description"] and "\n" not in t["description"] for t in m["tools"])
    assert {p["name"] for p in m["prompts"]} == {"debug_symbol", "fb_validation", "deploy_safely", "performance_audit", "onboard_project"}
    for p in m["prompts"]:
        for a in p["arguments"]:
            assert f"${{arguments.{a}}}" in p["text"]
    # the launcher inside the bundle must find the sources where the manifest says they are
    stage = tmp_path / "x"
    with zipfile.ZipFile(out) as zf:
        zf.extractall(stage)
    assert launch.default_src(stage / "server" / "launch.py") == (stage / "server" / "src").resolve()
