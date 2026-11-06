"""Suite-wide isolation for pytest: grok children (forge_doctor.isolated_env) get their empty home folder under the
session's temporary folder, never the real %LOCALAPPDATA%/agent-sprite-forge/grok-home. A test that checks the
default location deletes FORGE_GROK_ISOLATED_HOME itself."""
from __future__ import annotations

import pytest


@pytest.fixture(scope="session")
def _grok_home_root(tmp_path_factory):
    return tmp_path_factory.mktemp("isolated-grok-home")


@pytest.fixture(autouse=True)
def _isolated_grok_home(_grok_home_root, monkeypatch):
    monkeypatch.setenv("FORGE_GROK_ISOLATED_HOME", str(_grok_home_root))
