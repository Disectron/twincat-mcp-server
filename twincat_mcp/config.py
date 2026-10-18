"""Configuration for twincat-mcp.

Precedence (highest first):
  1. Environment variables ``TCMCP_*``
  2. TOML file given by ``TCMCP_CONFIG`` or ``./twincat_mcp.toml`` or
     ``~/.twincat_mcp/config.toml``
  3. Built-in defaults below

Everything is plain dataclasses so the config can be printed back to the
agent by ``tc_config`` without surprises.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import tomllib

MODES = ("readonly", "guarded", "full")
MODE_RANK = {m: i for i, m in enumerate(MODES)}

# Default ProgIDs, most recent first. Beckhoff:
#   TwinCAT XAE Shell (4024, 32-bit)   -> TcXaeShell.DTE.15.0
#   TwinCAT XAE Shell 64-bit (4026)    -> TcXaeShell.DTE.17.0
#   Visual Studio 2019 / 2022 / 2026   -> VisualStudio.DTE.16.0 / 17.0 / 18.0
DEFAULT_PROGIDS = (
    "TcXaeShell.DTE.17.0",
    "TcXaeShell.DTE.15.0",
    "VisualStudio.DTE.18.0",
    "VisualStudio.DTE.17.0",
    "VisualStudio.DTE.16.0",
)


def _home() -> Path:
    return Path(os.environ.get("TCMCP_HOME") or (Path.home() / ".twincat_mcp"))


@dataclass
class AdsConfig:
    ams_net_id: str | None = None          # e.g. "5.12.34.56.1.1"; None = local
    ams_port: int = 851                    # PLC runtime 1
    ip_address: str | None = None          # only needed for pyads on Linux / direct
    timeout_ms: int = 5000
    symbol_cache_ttl_s: int = 300
    static_routes_xml: str = r"C:\TwinCAT\3.1\Target\StaticRoutes.xml"
    ethercat_master_port: int = 0xFFFF     # ADS port of first EtherCAT master
    max_watch_duration_ms: int = 60_000
    min_watch_interval_ms: int = 10
    max_watch_points: int = 5000
    max_file_bytes: int = 4 * 1024 * 1024


@dataclass
class XaeConfig:
    progids: tuple[str, ...] = DEFAULT_PROGIDS
    attach_only: bool = True               # never launch a new shell unless told
    solution_path: str | None = None       # pick ROT instance holding this .sln
    visible: bool = True
    com_retry_s: float = 60.0              # retry window for RPC_E_CALL_REJECTED
    build_timeout_s: float = 900.0
    dialog_watchdog: bool = True           # poll XAE modal dialogs while attached
    dialog_rules: list[dict] = field(default_factory=list)   # [{title_contains|text_contains, button, force?}]
    allow_safety: bool = False             # permit edits under TwinSAFE (TISC) paths


@dataclass
class ProjectConfig:
    root: str | None = None                # folder to scan for .tsproj/.plcproj
    tcunit_remote_result: str = "boot:TcUnit_xUnit_testresults.xml"   # PATH_BOOTPATH-relative: Windows, CE7 and TC/BSD alike
    hmi_root: str | None = None            # TE2000 project folder (defaults to project root)
    naming_prefixes: dict = field(default_factory=dict)      # overrides for proj_lint naming rule
    experiments_dir: str | None = None     # exp_* specs/runs/reports; None -> <root>/experiments or ~/.twincat_mcp/experiments


@dataclass
class SafetyConfig:
    mode: str = "guarded"
    max_mode: str | None = None            # ceiling for runtime escalation; None = mode
    confirm_destructive: bool = True       # confirm token for activate/restart/exec/...
    confirm_ttl_s: int = 180
    write_allowlist: list[str] = field(default_factory=list)   # fnmatch, case-insens.
    write_denylist: list[str] = field(default_factory=list)    # always wins
    audit_log: str | None = None           # None -> ~/.twincat_mcp/audit.jsonl
    audit_reads: bool = False
    # false → no in-chat confirmation prompts: every control/destructive call needs a tc_request_confirmation token,
    # so each approval is a separate, visible, audited step (prompts are easy to click through without reading)
    allow_elicitation: bool = True
    # true → PLC code/project edits (xae_pou_*, proj_set_code, xae_tree_*, tcunit_scaffold, PLCopen import) are refused
    # unless the ADS target's TwinCAT system is in CONFIG mode (read from port 10000 before every edit)
    edit_requires_config_mode: bool = False


# Infosys books indexed on the first symbol lookup when nothing else is configured. Slugs are
# the path segment after /content/1033/ on infosys.beckhoff.com; unknown slugs are reported, not fatal.
DEFAULT_DOCS_BOOKS = (
    "tcplclib_tc2_system", "tcplclib_tc2_standard", "tcplclib_tc2_utilities", "tcplclib_tc3_module",
    "tcplclib_tc2_math", "tcplclib_tc2_iofunctions", "tcplclib_tc3_eventlogger", "tcplclib_tc3_jsonxml",
)


@dataclass
class DocsConfig:
    """Beckhoff Infosys reference lookup (infosys_* tools)."""
    enabled: bool = True
    # "local" = the Help Viewer package TwinCAT XAE installs (BKINFOSYS3_*.mshc, every book, offline; no network
    # call at all); "web" = infosys.beckhoff.com (sitemap + page fetches + optional DuckDuckGo fallback)
    source: str = "local"
    mshc: str | None = None                # the .mshc (or its folder); None = newest under %ProgramData%\Microsoft\HelpLibrary2
    cache_dir: str | None = None           # None -> ~/.twincat_mcp/infosys
    books: list[str] = field(default_factory=lambda: list(DEFAULT_DOCS_BOOKS))
    exclude_books: list[str] = field(default_factory=list)   # extra regexes on the book slug
    tc3_only: bool = True                  # drop TwinCAT 2 books (tcplclib<name> without underscore, twincat2/)
    max_workers: int = 6                   # parallel page fetches while indexing a book
    max_index_pages: int = 4000            # hard cap per infosys_index call
    sitemap_ttl_days: int = 7
    web_fallback: bool = True              # DuckDuckGo site: search when the local index has no hit
    fetch_timeout_s: float = 20.0


TOOL_GROUPS = ("tc", "ads", "rec", "snap", "test", "exp", "proj", "xae", "ipc", "hmi", "infosys", "wf", "tcunit", "kb", "hw", "scope")


@dataclass
class KnowledgeConfig:
    # folder for knowledge.db; None = ask the user on first use (kb_set_location). Conditions: local fixed drive,
    # not cloud-synced, not inside the plugin folder, writable, >= 1 GB free.
    location: str | None = None
    esi_dirs: list[str] = field(default_factory=list)       # extra ESI folders (TwinCAT's own are probed)
    library_dirs: list[str] = field(default_factory=list)   # extra managed-library repositories


@dataclass
class RecorderConfig:
    # engine for cycle-mode recordings: "auto" = native capture service (tcmcp-capture, Beckhoff AdsLib on TcAdsDll)
    # when its executable is present and matches this version, else the Python recorder; "native" = native or fail;
    # "python" = always the Python recorder. change/poll modes always use the Python recorder.
    engine: str = "auto"
    capture_exe: str | None = None          # explicit path to tcmcp-capture(.exe); None = the one shipped in the package
    ring_s: float = 10.0                    # native: seconds of samples buffered between the ADS callback and the file
    ring_cap_mb: float = 1024.0             # native: upper limit for that buffer
    local_ams_net_id: str | None = None     # native on a non-TwinCAT host (standalone AdsLib): this PC's AMS Net ID
    # also record with TwinCAT Scope (a Scope configuration open in XAE) when rec_start runs: "ask" = rec_start asks
    # the user (elicitation, or a question back to the agent) whenever XAE has a Scope configuration open;
    # "always" = record it without asking; "never" = don't offer it
    also_scope: str = "ask"


@dataclass
class ScopeConfig:
    # TC3ScopeExportTool.exe (TE130x Scope View / TF3300 Scope Server); None = searched in TwinCAT's Functions folders
    export_tool: str | None = None
    # where scope_export writes CSVs when no target is given; None = <recordings>/scope
    export_dir: str | None = None
    # the Scope configuration rec_start records alongside (name in the XAE solution); None = the only one open
    project: str | None = None


@dataclass
class ServerConfig:
    # tool groups exposed to the agent (by name prefix); None = all. 'tc' (doctor, safety, audit) is always on.
    # e.g. ["ads", "rec", "proj"] on a machine where XAE is never driven — fewer tool descriptions to load.
    tool_groups: list[str] | None = None
    # R4: extra folders tools may write exports / dumps / reports into (besides the plugin's data folder, the
    # recordings, the project and the solution folder) — e.g. ["D:/TwinCAT-MCP/exports"]
    output_dirs: list[str] | None = None


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    knowledge: KnowledgeConfig = field(default_factory=KnowledgeConfig)
    recorder: RecorderConfig = field(default_factory=RecorderConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    ads: AdsConfig = field(default_factory=AdsConfig)
    xae: XaeConfig = field(default_factory=XaeConfig)
    project: ProjectConfig = field(default_factory=ProjectConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    docs: DocsConfig = field(default_factory=DocsConfig)
    config_file: str | None = None
    warnings: list[str] = field(default_factory=list)   # values that were ignored or corrected at load (tc_doctor shows them)
    overrides: list[dict[str, Any]] = field(default_factory=list)   # env var beat a TOML value (N10)
    toml_keys: list[str] = field(default_factory=list)              # "section.key" set by the TOML file
    docs_cache_explicit: bool = False      # docs.cache_dir came from the TOML / env (not the default)

    # ------------------------------------------------------------------ dump
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["xae"]["progids"] = list(self.xae.progids)
        return d


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def _find_config_file() -> Path | None:
    cands = []
    if os.environ.get("TCMCP_CONFIG"):
        cands.append(Path(os.environ["TCMCP_CONFIG"]))
    cands.append(Path.cwd() / "twincat_mcp.toml")
    cands.append(_home() / "config.toml")
    for c in cands:
        if c.is_file():
            return c
    return None


def _apply_toml(cfg: Config, data: dict[str, Any]) -> None:
    for section in ("server", "knowledge", "recorder", "scope", "ads", "xae", "project", "safety", "docs"):
        sub = data.get(section) or {}
        target = getattr(cfg, section)
        for k, v in sub.items():
            if hasattr(target, k):
                if k == "progids":
                    v = tuple(v)
                setattr(target, k, v)
                cfg.toml_keys.append(f"{section}.{k}")


def _env(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else None


def _env_bool(name: str) -> bool | None:
    v = _env(name)
    if v is None:
        return None
    return v.strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str) -> list[str] | None:
    v = _env(name)
    if v is None:
        return None
    return [p.strip() for p in v.split(";") if p.strip()]


def _env_override(cfg: Config, key: str, env: str, new: Any) -> None:
    """Record when an environment variable (e.g. a plugin install setting) replaces a value the TOML set."""
    section, attr = key.split(".")
    old = getattr(getattr(cfg, section), attr)
    if cfg.config_file and key in cfg.toml_keys and old != new:
        cfg.overrides.append({"setting": key, "toml": old, "env": env, "value": new,
                              "note": f"{key}: {cfg.config_file} says {old!r}, environment {env} says {new!r} — the environment wins "
                                      "(plugin settings are passed as TCMCP_* variables; change it there or remove it)"})


def _apply_env(cfg: Config) -> None:
    if v := _env("TCMCP_KNOWLEDGE_DIR"):
        cfg.knowledge.location = v
    if v := _env("TCMCP_REC_ENGINE"):
        _env_override(cfg, "recorder.engine", "TCMCP_REC_ENGINE", v.lower())
        cfg.recorder.engine = v.lower()
    if v := _env("TCMCP_CAPTURE_EXE"):
        cfg.recorder.capture_exe = v
    if (lst := _env_list("TCMCP_TOOL_GROUPS")) is not None:
        cfg.server.tool_groups = [g.strip().lower() for g in lst]
    # safety
    if m := _env("TCMCP_MODE"):
        _env_override(cfg, "safety.mode", "TCMCP_MODE", m.lower())
        cfg.safety.mode = m.lower()
    if m := _env("TCMCP_MAX_MODE"):
        _env_override(cfg, "safety.max_mode", "TCMCP_MAX_MODE", m.lower())
        cfg.safety.max_mode = m.lower()
    if (b := _env_bool("TCMCP_CONFIRM")) is not None:
        cfg.safety.confirm_destructive = b
    if (lst := _env_list("TCMCP_WRITE_ALLOW")) is not None:
        _env_override(cfg, "safety.write_allowlist", "TCMCP_WRITE_ALLOW", lst)
        cfg.safety.write_allowlist = lst
    if (lst := _env_list("TCMCP_WRITE_DENY")) is not None:
        cfg.safety.write_denylist = lst
    if v := _env("TCMCP_AUDIT_LOG"):
        cfg.safety.audit_log = v
    if (b := _env_bool("TCMCP_AUDIT_READS")) is not None:
        cfg.safety.audit_reads = b
    if (b := _env_bool("TCMCP_ALLOW_ELICITATION")) is not None:
        cfg.safety.allow_elicitation = b
    if (b := _env_bool("TCMCP_EDIT_REQUIRES_CONFIG")) is not None:
        cfg.safety.edit_requires_config_mode = b
    # ads
    if v := _env("TCMCP_AMS_NET_ID"):
        _env_override(cfg, "ads.ams_net_id", "TCMCP_AMS_NET_ID", v)
        cfg.ads.ams_net_id = v
    if v := _env("TCMCP_AMS_PORT"):
        cfg.ads.ams_port = int(v)
    if v := _env("TCMCP_IP"):
        cfg.ads.ip_address = v
    if v := _env("TCMCP_ADS_TIMEOUT_MS"):
        cfg.ads.timeout_ms = int(v)
    if v := _env("TCMCP_STATIC_ROUTES_XML"):
        cfg.ads.static_routes_xml = v
    # xae
    if v := _env("TCMCP_PROGIDS"):
        cfg.xae.progids = tuple(p.strip() for p in v.split(";") if p.strip())
    if v := _env("TCMCP_SOLUTION"):
        cfg.xae.solution_path = v
    if (b := _env_bool("TCMCP_XAE_ATTACH_ONLY")) is not None:
        cfg.xae.attach_only = b
    if (b := _env_bool("TCMCP_DIALOG_WATCHDOG")) is not None:
        cfg.xae.dialog_watchdog = b
    if (b := _env_bool("TCMCP_ALLOW_SAFETY")) is not None:
        cfg.xae.allow_safety = b
    if v := _env("TCMCP_HMI_ROOT"):
        cfg.project.hmi_root = v
    # project
    if v := _env("TCMCP_PROJECT_ROOT"):
        cfg.project.root = v
    # docs
    if (b := _env_bool("TCMCP_DOCS")) is not None:
        cfg.docs.enabled = b
    if (lst := _env_list("TCMCP_DOCS_BOOKS")) is not None:
        cfg.docs.books = lst
    if (lst := _env_list("TCMCP_DOCS_EXCLUDE")) is not None:
        cfg.docs.exclude_books = lst
    if (b := _env_bool("TCMCP_DOCS_WEB_FALLBACK")) is not None:
        cfg.docs.web_fallback = b
    if v := _env("TCMCP_DOCS_CACHE"):
        cfg.docs.cache_dir = v
    if v := _env("TCMCP_DOCS_SOURCE"):
        cfg.docs.source = v.lower()
    if v := _env("TCMCP_DOCS_MSHC"):
        cfg.docs.mshc = v
    if v := _env("TCMCP_EXPERIMENTS"):
        cfg.project.experiments_dir = v
    if v := _env("TCMCP_TCUNIT_RESULT"):
        cfg.project.tcunit_remote_result = v


_RE_NETID = re.compile(r"^\d{1,3}(\.\d{1,3}){5}$")
_PLACEHOLDER_MARKS = ("<", ">", "path\\to", "path/to", "yoursolution", "your.sln", "x.x.x.x")


def looks_like_placeholder(value: str | None) -> bool:
    """A template value that was copied without being edited: '<CX_NETID>', 'C:\\path\\to\\YourSolution.sln', 'x.x.x.x.1.1'."""
    if not value:
        return False
    low = str(value).lower()
    return any(m in low for m in _PLACEHOLDER_MARKS)


def validate(cfg: Config) -> list[str]:
    """Return a list of human-readable problems (empty = fine). Placeholder or malformed values that
    have a safe fallback are *not* problems: they are dropped and recorded in ``cfg.warnings`` so the
    server still starts and tc_doctor can point at them."""
    problems: list[str] = []
    # --- values copied from a template without editing -----------------------------------------
    if looks_like_placeholder(cfg.ads.ams_net_id):
        cfg.warnings.append(f"ads.ams_net_id = {cfg.ads.ams_net_id!r} is a template placeholder — ignored; talking to the LOCAL runtime. "
                            "Set the CX NetId (XAE → System → Routes, e.g. 5.12.34.56.1.1) in twincat_mcp.toml or the plugin option ams_net_id.")
        cfg.ads.ams_net_id = None
    elif cfg.ads.ams_net_id and not _RE_NETID.match(cfg.ads.ams_net_id.strip()):
        cfg.warnings.append(f"ads.ams_net_id = {cfg.ads.ams_net_id!r} is not an AmsNetId (six dotted numbers) — ignored; talking to the LOCAL runtime.")
        cfg.ads.ams_net_id = None
    elif cfg.ads.ams_net_id:
        cfg.ads.ams_net_id = cfg.ads.ams_net_id.strip()
    if looks_like_placeholder(cfg.xae.solution_path):
        cfg.warnings.append(f"xae.solution_path = {cfg.xae.solution_path!r} is a template placeholder — ignored. Set the real .sln path.")
        cfg.xae.solution_path = None
    elif cfg.xae.solution_path and not Path(cfg.xae.solution_path).is_file():
        cfg.warnings.append(f"xae.solution_path {cfg.xae.solution_path!r} does not exist on this PC (xae_attach will still try by name).")
    for attr in ("root", "hmi_root", "experiments_dir"):
        v = getattr(cfg.project, attr)
        if looks_like_placeholder(v):
            cfg.warnings.append(f"project.{attr} = {v!r} is a template placeholder — ignored.")
            setattr(cfg.project, attr, None)
    if cfg.project.root and not Path(cfg.project.root).is_dir():
        cfg.warnings.append(f"project.root {cfg.project.root!r} is not a folder on this PC — project tools will find nothing.")
    if cfg.recorder.engine not in ("auto", "native", "python"):
        cfg.warnings.append(f"recorder.engine = {cfg.recorder.engine!r} is not auto | native | python — using auto.")
        cfg.recorder.engine = "auto"
    cfg.recorder.also_scope = str(cfg.recorder.also_scope or "ask").lower()
    if cfg.recorder.also_scope not in ("ask", "always", "never"):
        cfg.warnings.append(f"recorder.also_scope = {cfg.recorder.also_scope!r} is not ask | always | never — using ask.")
        cfg.recorder.also_scope = "ask"
    if looks_like_placeholder(cfg.recorder.capture_exe):
        cfg.recorder.capture_exe = None
    # --- hard errors ------------------------------------------------------------------------------
    if cfg.safety.mode not in MODES:
        problems.append(f"safety.mode must be one of {MODES}, got {cfg.safety.mode!r}")
    if cfg.safety.max_mode is None:
        cfg.safety.max_mode = cfg.safety.mode
    elif cfg.safety.max_mode not in MODES:
        problems.append(f"safety.max_mode must be one of {MODES}, got {cfg.safety.max_mode!r}")
    elif MODE_RANK[cfg.safety.max_mode] < MODE_RANK.get(cfg.safety.mode, 0):
        problems.append("safety.max_mode is lower than safety.mode")
    if cfg.safety.audit_log is None:
        cfg.safety.audit_log = str(_home() / "audit.jsonl")
    if not (1 <= cfg.ads.ams_port <= 65535):
        problems.append("ads.ams_port out of range")
    if cfg.project.root is None and cfg.xae.solution_path:
        cfg.project.root = str(Path(cfg.xae.solution_path).parent)
    cfg.docs.source = str(cfg.docs.source or "local").lower()
    if cfg.docs.source not in ("local", "web"):
        cfg.warnings.append(f"docs.source = {cfg.docs.source!r} is not local | web — using local.")
        cfg.docs.source = "local"
    cfg.docs_cache_explicit = cfg.docs.cache_dir is not None
    if cfg.docs.cache_dir is None:
        cfg.docs.cache_dir = str(_home() / "infosys")
    if cfg.project.experiments_dir is None:
        cfg.project.experiments_dir = str(Path(cfg.project.root) / "experiments") if cfg.project.root else str(_home() / "experiments")
    if cfg.docs.max_workers < 1 or cfg.docs.max_workers > 16:
        problems.append("docs.max_workers must be 1..16 (be polite to infosys.beckhoff.com)")
    return problems


def load(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    p = Path(path) if path else _find_config_file()
    if p and p.is_file():
        with open(p, "rb") as fh:
            _apply_toml(cfg, tomllib.load(fh))
        cfg.config_file = str(p)
    _apply_env(cfg)
    problems = validate(cfg)
    if problems:
        raise ValueError("twincat-mcp config invalid: " + "; ".join(problems))
    return cfg


def is_windows() -> bool:
    return sys.platform == "win32"
