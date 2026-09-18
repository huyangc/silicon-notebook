"""Exercise the real development launcher without binding host ports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("dotenv_path", ['"plugin source"', '"$PLUGIN_SOURCE"'])
def test_service_launcher_merges_dotenv_and_exported_plugin_paths(tmp_path, dotenv_path):
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    backend = root / "backend"
    frontend = root / "frontend"
    plugin = root / "plugin source"
    exported = root / "exported source"
    fake_bin = root / "bin"
    for directory in (scripts, backend, frontend / "node_modules", plugin, exported, fake_bin):
        directory.mkdir(parents=True)
    for name in ("dev.sh", "python_env.py", "extension_services.sh", "extension_services.py", "extension_service_runtime.py", "extension_service_worker.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (scripts / "autotune.sh").write_text("# No host tuning in this test.\n")
    (root / ".env").write_text(
        f'PLUGIN_SOURCE="plugin source"\nPYTHONPATH={dotenv_path}\nPLUGIN_TOKEN="fixture-token"\n'
    )
    (plugin / "dotenv_plugin.py").write_text('VALUE = "dotenv"\n')
    (exported / "exported_plugin.py").write_text('VALUE = "exported"\n')
    # A module-level stand-in exercises the actual -m boundary and exits; no server.
    (backend / "uvicorn.py").write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "import dotenv_plugin, exported_plugin\n"
        "Path(os.environ['RESULT_FILE']).write_text(json.dumps(["
        "dotenv_plugin.VALUE, exported_plugin.VALUE, os.environ['PLUGIN_TOKEN']]))\n"
    )
    npm = fake_bin / "npm"
    npm.write_text('#!/bin/sh\nwhile [ ! -f "$RESULT_FILE" ]; do sleep 0.1; done\n')
    npm.chmod(0o755)
    result_file = tmp_path / "result.json"
    env = dict(os.environ)
    env.pop("SILICON_NOTEBOOK_ENV_FILE", None)
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}",
        PYTHON_BIN=sys.executable,
        PYTHONPATH="exported source",
        RESULT_FILE=str(result_file),
    )
    result = subprocess.run(
        ["bash", str(scripts / "dev.sh")], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result_file.read_text()) == ["dotenv", "exported", "fixture-token"]


def test_backend_status_uses_root_relative_selected_environment(tmp_path):
    root = tmp_path / "checkout"
    scripts = root / "scripts"
    config = root / "config"
    core = root / "backend/app/core"
    fake_bin = root / "bin"
    for directory in (scripts, config, core, fake_bin):
        directory.mkdir(parents=True)
    for name in ("backend.sh", "python_env.py", "extension_services.sh", "extension_services.py", "extension_service_runtime.py", "extension_service_worker.py"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    (config / "alternate.env").write_text('DATABASE_URL="selected-database"\n')
    (core / "config.py").write_text(
        'import os\nclass Settings:\n    database_url = os.environ.get("DATABASE_URL", "wrong")\n'
    )
    (core / "database_url.py").write_text('def database_status(value):\n    return value\n')
    lsof = fake_bin / "lsof"
    lsof.write_text("#!/bin/sh\nexit 0\n")
    lsof.chmod(0o755)
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.update(
        PATH=f"{fake_bin}{os.pathsep}{env['PATH']}", PYTHON_BIN=sys.executable,
        SILICON_NOTEBOOK_ENV_FILE="config/alternate.env", PYTHONPATH="",
    )
    result = subprocess.run(
        ["bash", str(scripts / "backend.sh"), "status"], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert "selected-database" in result.stdout
    assert "wrong" not in result.stdout
