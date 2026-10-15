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
