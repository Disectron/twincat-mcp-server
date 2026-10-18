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
