"""The live-process diagnostics directory is a setting, and tests never use the checkout's.

The app lifespan used to hard-code ``<repo>/.local/diagnostics``, so every test
that entered it (``with TestClient(app)``) wrote ``runtime.json`` and
``thread-dumps.log`` into the checkout, with all xdist workers sharing one
heartbeat file. ``DIAGNOSTICS_DIR`` now carries the path; its default is the
historical one and conftest points the test process at a temporary directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core import diagnostics_runtime
from app.core.config import Settings
import app.main as main_module


ROOT = Path(__file__).resolve().parents[2]


def test_default_diagnostics_dir_is_the_historical_repo_local_path():
    # The literal the lifespan used before the setting existed.
    historical = Path(main_module.__file__).resolve().parents[2] / ".local" / "diagnostics"
    with patch.dict(os.environ, {}, clear=True):
        assert Settings(_env_file=None).diagnostics_dir == str(historical)
    with patch.dict(os.environ, {"DIAGNOSTICS_DIR": "  "}, clear=True):
        assert Settings(_env_file=None).diagnostics_dir == str(historical)
    with patch.dict(os.environ, {"DIAGNOSTICS_DIR": "var/diag"}, clear=True):
        assert Settings(_env_file=None).diagnostics_dir == str(ROOT / "var" / "diag")
    with patch.dict(os.environ, {"DIAGNOSTICS_DIR": "/srv/diag"}, clear=True):
        assert Settings(_env_file=None).diagnostics_dir == "/srv/diag"


def test_app_lifespan_writes_diagnostics_outside_the_checkout():
    # A fresh app: the module-level one's MCP session manager runs only once
    # per process, and other tests in this worker enter its lifespan.
    with TestClient(main_module.create_app()):
        runtime = diagnostics_runtime.current_runtime()
        assert runtime is not None
        target = Path(runtime.diagnostics_dir).resolve()
        assert not target.is_relative_to(ROOT.resolve()), target
        assert target == Path(os.environ.get("DIAGNOSTICS_DIR", "")).resolve()
        snapshot = target / "runtime.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not snapshot.exists():
            time.sleep(0.01)
        assert json.loads(snapshot.read_text(encoding="utf-8"))["pid"] == os.getpid()
