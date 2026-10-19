import os
import shutil
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("TCMCP_HOME", str(tmp_path / "home"))
    for k in list(os.environ):
        if k.startswith("TCMCP_") and k != "TCMCP_HOME":
            monkeypatch.delenv(k)
    yield


@pytest.fixture
def project_dir(tmp_path):
    dst = tmp_path / "proj"
    shutil.copytree(FIX / "sample_project", dst)
    return dst


@pytest.fixture(scope="session")
def kb_fixture(tmp_path_factory):
    """A knowledge folder whose indexes were built by the real code (tests/fixtures/kb/make_fixtures.py): the plugin's
    Infosys extractor on a small Help Viewer package, the manuals builder on a small PDF. Built once per session —
    copy it (kb_copy) before changing anything."""
    pytest.importorskip("reportlab")
    pytest.importorskip("pypdf")
    import importlib.util
    spec = importlib.util.spec_from_file_location("tcmcp_make_fixtures", FIX / "kb" / "make_fixtures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.make_kb(tmp_path_factory.mktemp("kbfix") / "kb")


@pytest.fixture
def kb_copy(kb_fixture, tmp_path):
    d = tmp_path / "kb"
    shutil.copytree(kb_fixture, d)
    return d


SRC_SUFFIXES = (".tcpou", ".tcdut", ".tcgvl", ".tcio")


def register_sources(plcproj: Path) -> Path:
    """What XAE writes when objects are added to a PLC project: a <Compile Include> item for every source file below
    the .plcproj's folder (N106: the project index takes the .plcproj's compile items, never every .Tc* under the
    root). Run after a test adds a file to a PLC project folder."""
    import re as _re
    plcproj = Path(plcproj)
    base = plcproj.parent
    text = plcproj.read_text(encoding="utf-8")
    have = set(_re.findall(r'<Compile Include="([^"]+)"', text))
    items = "".join(f'<Compile Include="{rel}"><SubType>Code</SubType></Compile>'
                    for rel in sorted(str(f.relative_to(base)).replace("/", "\\") for f in base.rglob("*")
                                      if f.suffix.lower() in SRC_SUFFIXES and f.is_file())
                    if rel not in have)
    if items:
        text = text.replace("</Project>", f"<ItemGroup>{items}</ItemGroup></Project>", 1)
        plcproj.write_text(text, encoding="utf-8")
    return plcproj
