"""Deployment preflight uses real isolated imports, without application composition."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_cli_extensions.py"


def _run(tmp_path: Path, config: str = "", *, extra_env=None, args=()):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.pathsep.join((str(tmp_path), str(ROOT / "backend"))),
        "SILICON_NOTEBOOK_ENV_FILE": "",
        "EXTENSIONS_CONFIG": config,
        "MODEL_SERVICES_CONFIG": "",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'must-not-exist.db'}",
        "STORAGE_DIR": str(tmp_path / "must-not-exist-storage"),
        **(extra_env or {}),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=30,
    )


def _config(tmp_path: Path, text: str) -> str:
    target = tmp_path / "private-config.toml"
    target.write_text(text, encoding="utf-8")
    return str(target)


PLUGIN = '''
from app.extension_sdk import EXTENSION_API_VERSION, ExtensionManifest
from pydantic import BaseModel
class Config(BaseModel):
    count: int
class Bundle:
    settings_model = Config
    manifest = ExtensionManifest(id="corp.test", version="1", api_version=EXTENSION_API_VERSION,
        display_name="Test", trust="deployment", contributions=())
    def configure(self, settings):
        assert settings.count == 3
    def register(self, registrar):
        raise AssertionError("must not register")
    def capability_decisions(self):
        raise AssertionError("must not probe")
BUNDLE = Bundle()
'''


def test_real_plugin_settings_checked_without_runtime_or_database(tmp_path):
    (tmp_path / "sample_plugin.py").write_text(PLUGIN, encoding="utf-8")
    config = _config(tmp_path, '''
[extensions."corp.test"]
bundle = "sample_plugin:BUNDLE"
[extensions."corp.test".settings]
count = 3
''')
    result = _run(tmp_path, config)
    assert result.returncode == 0, result.stderr
    assert "1 个" in result.stdout and "corp.test" in result.stdout
    assert not (tmp_path / "must-not-exist.db").exists()
    assert not (tmp_path / "must-not-exist-storage").exists()
    assert str(tmp_path) not in result.stdout + result.stderr


def test_unified_entry_loads_dotenv_plugin_even_with_backend_only_pythonpath(tmp_path):
    plugin_dir = tmp_path / "plugin source"
    plugin_dir.mkdir()
    (plugin_dir / "sample_plugin.py").write_text(PLUGIN, encoding="utf-8")
    config = _config(tmp_path, '''
[extensions."corp.test"]
bundle = "sample_plugin:BUNDLE"
[extensions."corp.test".settings]
count = 3
''')
    env_file = tmp_path / "selected.env"
    env_file.write_text(
        f'EXTENSIONS_CONFIG="{config}"\nPYTHONPATH="{plugin_dir}"\n', encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", ""), "PYTHON_BIN": sys.executable,
        "PYTHONPATH": "backend", "SILICON_NOTEBOOK_ENV_FILE": str(env_file),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'must-not-exist.db'}",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/cli.sh"), "extensions", "check"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "corp.test" in result.stdout
    assert not (tmp_path / "must-not-exist.db").exists()


def test_disabled_plugin_not_imported(tmp_path):
    marker = tmp_path / "imported"
    (tmp_path / "disabled_plugin.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('secret')\n",
        encoding="utf-8",
    )
    config = _config(tmp_path, '''
[extensions."corp.disabled"]
bundle = "disabled_plugin:BUNDLE"
enabled = false
''')
    result = _run(tmp_path, config)
    assert result.returncode == 0, result.stderr
    assert "0 个" in result.stdout
    assert not marker.exists()


def test_empty_configuration_does_not_use_ambient_env_file(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "0 个" in result.stdout


@pytest.mark.parametrize("body", [
    "import nonexistent_private_dependency_secret",
    "raise RuntimeError('/private/path raw-secret-token')",
])
def test_import_failure_is_sanitized_and_actionable(tmp_path, body):
    (tmp_path / "broken_plugin.py").write_text(body, encoding="utf-8")
    config = _config(tmp_path, '[extensions."corp.test"]\nbundle="broken_plugin:BUNDLE"\n')
    result = _run(tmp_path, config)
    assert result.returncode == 2
    assert "corp.test" in result.stderr and "plugin_module_import_failed" in result.stderr
    assert all(value not in result.stderr for value in (
        "raw-secret-token", "/private/path", "nonexistent_private_dependency_secret", str(tmp_path), "Traceback",
    ))
    if body.startswith("import"):
        assert "ModuleNotFoundError" in result.stderr and "PYTHONPATH" in result.stderr


def test_settings_failure_never_echoes_value(tmp_path):
    (tmp_path / "sample_plugin.py").write_text(PLUGIN, encoding="utf-8")
    config = _config(tmp_path, '''
[extensions."corp.test"]
bundle = "sample_plugin:BUNDLE"
[extensions."corp.test".settings]
count = "raw-secret-token"
''')
    result = _run(tmp_path, config)
    assert result.returncode == 2
    assert "plugin_settings_invalid" in result.stderr
    assert "raw-secret-token" not in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("config_text", ['[extensions."/private/raw-secret-token"]\n', '[extensions]\nsecret="unterminated'])
def test_bad_config_never_echoes_invalid_id_or_toml(tmp_path, config_text):
    result = _run(tmp_path, _config(tmp_path, config_text))
    assert result.returncode == 2
    assert all(value not in result.stderr for value in ("raw-secret-token", "unterminated", str(tmp_path), "Traceback"))


def test_application_settings_failure_is_sanitized(tmp_path):
    result = _run(tmp_path, extra_env={"NOTEBOOK_METADATA_BATCH_CHARS": "raw-secret-token"})
    assert result.returncode == 2
    assert "应用配置" in result.stderr
    assert "raw-secret-token" not in result.stderr and "Traceback" not in result.stderr


def test_help_needs_no_app_or_dependencies(tmp_path):
    # -S removes site packages, including pydantic; the blocker also catches app imports.
    code = '''
import sys, runpy
class BlockApp:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "app" or fullname.startswith("app."):
            raise AssertionError("help imported app")
sys.meta_path.insert(0, BlockApp())
sys.argv = [sys.argv[1], "--help"]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    result = subprocess.run(
        [sys.executable, "-S", "-c", code, str(SCRIPT)], cwd=tmp_path,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "不连接数据库" in result.stdout
