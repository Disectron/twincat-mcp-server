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
    spawned: list[tuple] = []
    monkeypatch.setattr(launch, "spawn_background_install", lambda s_, v_, py: spawned.append((s_, v_, py)) or (v_.parent / "log"))

    # 1. started as the server with no venv: must NOT install inline (the client would time out) —
    #    it takes the lock, spawns the detached install and exits 5
    assert launch.main(["--src", str(src), "--venv", str(venv), "--log", "DEBUG"]) == 5
    assert spawned and not executed and not any("install" in c for c in calls)
    assert launch.lock_age_s(venv) is not None, "lock held while the background install runs"
    # a second server start while the install runs: exit 5 again, nothing spawned twice
    assert launch.main(["--src", str(src), "--venv", str(venv)]) == 5
    assert len(spawned) == 1

    # 2. the spawned child (--no-run --lock-held) installs inline and releases the lock
    assert launch.main(["--src", str(src), "--venv", str(venv), "--no-run", "--lock-held"]) == 0
    assert any(c[1:3] == ["-m", "venv"] for c in calls)
    assert any("install" in c and str(src) in c for c in calls)
    assert (venv / launch.STAMP_NAME).is_file()
    assert launch.lock_age_s(venv) is None, "lock released after install"

    # 3. the reconnect: venv present and stamp current → straight to the server, env sanitised, args through
    calls.clear()
    assert launch.main(["--src", str(src), "--venv", str(venv), "--log", "DEBUG"]) == 0
    assert executed[0][1] == ["--log", "DEBUG"], "unknown args pass through to the server"
    assert "TCMCP_MODE" not in executed[0][2]
    # the server runs from site-packages: the plugin folder (skills/twincat-docs) comes from the launcher
    assert executed[0][2]["TCMCP_PLUGIN_SRC"] == str(src.resolve())
    assert not any("install" in c and str(src) in c for c in calls), "second start must not reinstall"

    # 4. --no-run bootstrap (pre-install step / CI) installs synchronously; --reinstall forces it
    assert launch.main(["--src", str(src), "--venv", str(venv), "--no-run", "--reinstall"]) == 0
    assert any("install" in c and str(src) in c for c in calls)
    # 5. --sync-install keeps the old inline behaviour for clients with a long connect timeout
    (venv / launch.STAMP_NAME).unlink()
    calls.clear(); executed.clear()
    assert launch.main(["--src", str(src), "--venv", str(venv), "--sync-install"]) == 0
    assert any("install" in c and str(src) in c for c in calls) and executed
    # 6. the hook form: --no-run --background spawns and returns at once
    (venv / launch.STAMP_NAME).unlink()
    spawned.clear()
    assert launch.main(["--src", str(src), "--venv", str(venv), "--no-run", "--background"]) == 0
    assert len(spawned) == 1 and launch.lock_age_s(venv) is not None
    launch.release_lock(venv)


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


def test_stamp_quick_path_skips_hashing(tmp_path, monkeypatch):
    """P13: unchanged names/sizes/mtimes → no content hashing on start; a touched file falls back to the digest,
    so an mtime-only change (git checkout) does not trigger a reinstall."""
    src = tmp_path / "src"
    (src / "twincat_mcp").mkdir(parents=True)
    (src / "pyproject.toml").write_text("[project]\nname='x'\n")
    (src / "twincat_mcp" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    venv = tmp_path / "venv"
    venv.mkdir()
    launch.write_stamp(venv, launch.LazyStamp(src, "py"))
    calls = {"n": 0}
    real = launch.source_digest

    def counting(s):
        calls["n"] += 1
        return real(s)
    monkeypatch.setattr(launch, "source_digest", counting)
    assert not launch.needs_install(venv, launch.LazyStamp(src, "py")) and calls["n"] == 0
    import os, time
    f = src / "twincat_mcp" / "__init__.py"
    os.utime(f, (time.time() + 5, time.time() + 5))          # touched, same content
    assert not launch.needs_install(venv, launch.LazyStamp(src, "py")) and calls["n"] == 1
    f.write_text('__version__ = "9.9.9"\nX = 1\n')            # real change
    assert launch.needs_install(venv, launch.LazyStamp(src, "py"))


@pytest.mark.skipif(sys.platform == "win32", reason="fake interpreters as shell scripts")
def test_launch_rebuilds_a_venv_made_with_an_older_python(tmp_path):
    """3.12 migration: a venv built by an earlier plugin on Python 3.10 / 3.11 still starts and has pip — it must
    count as unusable (pip would refuse requires-python >= 3.12 and the old server would keep running)."""
    import os
    import stat
    assert launch.MIN_PY == (3, 12)
    old = tmp_path / "old"
    new = tmp_path / "new"
    for venv, body in ((old, 'import sys; sys.version_info = (3, 11, 9, "final", 0)\n'), (new, "")):
        py = launch.venv_python(venv)
        py.parent.mkdir(parents=True)
        # a stand-in interpreter: the real one, with sys.version_info read as 3.11 for the "old" venv
        py.write_text(f"#!/bin/sh\nexec {sys.executable} -c '{body}exec(compile(sys.argv[2], \"-c\", \"exec\"))' \"$@\"\n"
                      if body else f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
        py.chmod(py.stat().st_mode | stat.S_IXUSR)
    assert launch.venv_ok(new) is True
    assert launch.venv_ok(old) is False


def test_python_minimum_is_312_everywhere():
    """One Python version for everything: pyproject, launchers, manifests, CI."""
    assert 'requires-python = ">=3.12"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "tomli" not in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "sys.version_info>=(3,12)" in (ROOT / "scripts" / "launch.cmd").read_text(encoding="utf-8")
    assert "sys.version_info>=(3,12)" in (ROOT / "scripts" / "install.cmd").read_text(encoding="utf-8")
    tpl = json.loads((ROOT / "packaging" / "mcpb" / "manifest.template.json").read_text(encoding="utf-8"))
    assert tpl["compatibility"]["runtimes"]["python"] == ">=3.12"
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'python: ["3.12"]' in ci and '"3.9"' not in ci and '"3.10"' not in ci


def test_mcpb_ships_the_docs_skill_builders_and_stub():
    names = {a for _src, a in mcpb_build.bundle_members()}
    for f in ("kb_docs.py", "builders/infosys_local.py", "builders/manuals_db.py", "builders/stub.py"):
        assert f"server/src/skills/twincat-docs/{f}" in names
