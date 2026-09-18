"""Process-level import/environment contracts for service and CLI launchers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
LAUNCH = (
    "import sys; from pathlib import Path; "
    f"sys.path.insert(0, {str(SCRIPTS)!r}); "
    "from python_env import exec_python; "
    "exec_python(sys.argv[2:], root=Path(sys.argv[1]))"
)
PATHS_ONLY_LAUNCH = (
    "import sys; from pathlib import Path; "
    f"sys.path.insert(0, {str(SCRIPTS)!r}); "
    "from python_env import exec_python; "
    "exec_python(sys.argv[3:], root=Path(sys.argv[1]), paths_only=True, "
    "path_env_file=Path(sys.argv[2]) if sys.argv[2] else None)"
)
PROBE = """
import json, os, sys
import sample_plugin
print(json.dumps({
    'plugin': sample_plugin.VALUE,
    'arguments': sys.argv[1:],
    'pythonpath': os.environ['PYTHONPATH'].split(os.pathsep),
    'secret': os.environ.get('TEST_LAUNCH_SECRET'),
    'literal': os.environ.get('TEST_LAUNCH_LITERAL'),
    'expanded': os.environ.get('TEST_LAUNCH_EXPANDED'),
    'env_file': os.environ.get('SILICON_NOTEBOOK_ENV_FILE'),
    'cwd': os.getcwd(),
    'pid': os.getpid(),
}))
"""


@pytest.fixture
def launch_project(tmp_path):
    root = tmp_path / "project with spaces"
    backend = root / "backend"
    backend.mkdir(parents=True)
    (backend / "probe.py").write_text(PROBE, encoding="utf-8")
    plugin = root / "plugin source"
    plugin.mkdir()
    (plugin / "sample_plugin.py").write_text("VALUE = 'loaded'\n", encoding="utf-8")
    return root


def _launch(root, arguments, *, cwd=None, variables=None, paths_only=False, path_env_file=None):
    environ = dict(os.environ)
    for key in (
        "PYTHONPATH", "SILICON_NOTEBOOK_ENV_FILE", "PYTHON_DOTENV_DISABLED",
        "TEST_LAUNCH_SECRET", "TEST_LAUNCH_LITERAL", "TEST_LAUNCH_EXPANDED",
    ):
        environ.pop(key, None)
    environ.update(variables or {})
    command = (
        [sys.executable, "-c", PATHS_ONLY_LAUNCH, str(root), str(path_env_file or "")]
        if paths_only else [sys.executable, "-c", LAUNCH, str(root)]
    )
    return subprocess.run(
        [*command, *arguments],
        cwd=cwd or root,
        env=environ,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_dotenv_only_plugin_path_and_child_environment(launch_project):
    root = launch_project
    (root / ".env").write_text(
        "PYTHONPATH='plugin source'\n"
        "TEST_LAUNCH_SECRET='a secret with spaces'\n"
        "TEST_LAUNCH_EXPANDED=${TEST_LAUNCH_SECRET}\n",
        encoding="utf-8",
    )
    result = _launch(root, ["-m", "probe", "one argument", "--flag=a b"])
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["plugin"] == "loaded"
    assert data["arguments"] == ["one argument", "--flag=a b"]
    assert data["secret"] == data["expanded"] == "a secret with spaces"
    assert data["pythonpath"] == [str(root / "backend"), str(root / "plugin source")]
    assert result.stderr == ""


def test_process_path_merges_dotenv_and_anchors_to_root(launch_project, tmp_path):
    root = launch_project
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (root / ".env").write_text("PYTHONPATH='plugin source:backend::'\nTEST_LAUNCH_SECRET=file\n")
    result = _launch(
        root, ["-m", "probe"], cwd=elsewhere,
        variables={"PYTHONPATH": "backend:additional::", "TEST_LAUNCH_SECRET": "process"},
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["pythonpath"] == [
        str(root / "backend"), str(root / "additional"), str(root / "plugin source"),
    ]
    assert data["secret"] == "process"
    assert data["cwd"] == str(elsewhere)


def test_explicit_env_file_is_root_relative_and_forwarded(launch_project, tmp_path):
    root = launch_project
    (root / ".env").write_text("TEST_LAUNCH_SECRET=wrong\n")
    override = root / "alternate env"
    override.write_text("PYTHONPATH='plugin source'\nTEST_LAUNCH_SECRET=selected\n")
    result = _launch(
        root, ["-m", "probe"], cwd=tmp_path,
        variables={"SILICON_NOTEBOOK_ENV_FILE": "alternate env"},
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["secret"] == "selected"
    assert data["env_file"] == str(override)


@pytest.mark.parametrize("selection", ["", "   "])
def test_empty_override_disables_dotenv(launch_project, selection):
    root = launch_project
    (root / ".env").write_text("TEST_LAUNCH_SECRET=unwanted\nPYTHONPATH=unwanted\n")
    result = _launch(
        root, ["-m", "probe"],
        variables={"SILICON_NOTEBOOK_ENV_FILE": selection, "PYTHONPATH": "plugin source"},
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["secret"] is None
    assert data["env_file"] == ""
    assert data["pythonpath"] == [str(root / "backend"), str(root / "plugin source")]


def test_missing_default_is_allowed_but_explicit_missing_is_sanitized(launch_project):
    root = launch_project
    result = _launch(root, ["-c", "raise SystemExit(17)"])
    assert result.returncode == 17
    result = _launch(
        root, ["-c", "raise AssertionError('must not start')"],
        variables={"SILICON_NOTEBOOK_ENV_FILE": "private-secret-file"},
    )
    assert result.returncode == 2
    assert "SILICON_NOTEBOOK_ENV_FILE" in result.stderr
    assert "private-secret-file" not in result.stderr
    assert "Traceback" not in result.stderr
    assert str(root) not in result.stderr


def test_dotenv_does_not_execute_shell_and_ignores_bare_keys(launch_project):
    root = launch_project
    marker = root / "must not exist"
    literal = f'$(touch "{marker}")'
    (root / ".env").write_text(
        f"PYTHONPATH='plugin source'\nTEST_LAUNCH_LITERAL='{literal}'\nBARE_KEY\n",
    )
    result = _launch(root, ["-m", "probe"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["literal"] == literal
    assert not marker.exists()


def test_exec_keeps_pid_and_script_arguments(launch_project):
    root = launch_project
    script = root / "standalone script.py"
    script.write_text("import json, os, sys; print(json.dumps([os.getpid(), sys.argv[1:]]))")
    environ = dict(os.environ, SILICON_NOTEBOOK_ENV_FILE="", PYTHONPATH="")
    with subprocess.Popen(
        [sys.executable, "-c", LAUNCH, str(root), str(script), "argument with spaces"],
        env=environ, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as process:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr
        assert json.loads(stdout) == [process.pid, ["argument with spaces"]]


def test_empty_search_entries_do_not_import_callers_directory(launch_project, tmp_path):
    root = launch_project
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "ambient_plugin.py").write_text("raise AssertionError('ambient import')")
    script = root / "isolated.py"
    script.write_text(
        "import importlib.util; assert importlib.util.find_spec('ambient_plugin') is None"
    )
    result = _launch(
        root, [str(script)], cwd=elsewhere, variables={"PYTHONPATH": "::backend:"},
    )
    assert result.returncode == 0, result.stderr


def test_paths_only_uses_selected_plugin_without_injecting_configuration(launch_project):
    root = launch_project
    (root / ".env").write_text("PYTHONPATH=wrong-default-plugin\n")
    selected = root / "command configuration"
    selected.write_text(
        "PYTHONPATH='plugin source'\n"
        "DATABASE_URL=sqlite:///wrong.db\n"
        "EXTENSIONS_CONFIG=wrong.toml\n"
        "TEST_LAUNCH_SECRET=private-file-credential\n"
        "SILICON_NOTEBOOK_ENV_FILE=wrong-selection\n"
    )
    command = (
        "import json, os, sys, sample_plugin; "
        "print(json.dumps({'environment': dict(os.environ), 'args': sys.argv[1:]}))"
    )
    expected = {
        "DATABASE_URL": "sqlite:///caller.db",
        "EXTENSIONS_CONFIG": "caller.toml",
        "SILICON_NOTEBOOK_ENV_FILE": "  existing selection  ",
        "TEST_LAUNCH_LITERAL": "existing-credential",
    }
    result = _launch(
        root, ["-c", command, "--env", str(selected), "value with spaces"],
        variables={**expected, "PYTHONPATH": "backend:additional"},
        paths_only=True, path_env_file=selected,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["args"] == ["--env", str(selected), "value with spaces"]
    assert {key: data["environment"][key] for key in expected} == expected
    assert "TEST_LAUNCH_SECRET" not in data["environment"]
    assert data["environment"]["PYTHONPATH"].split(os.pathsep) == [
        str(root / "backend"), str(root / "additional"), str(root / "plugin source"),
    ]
    assert result.stderr == ""


@pytest.mark.parametrize("selection", ["none", "missing", "directory", "invalid-encoding"])
def test_paths_only_defers_file_errors_and_never_falls_back(launch_project, selection):
    root = launch_project
    (root / ".env").write_text("PYTHONPATH=wrong-plugin\nTEST_LAUNCH_SECRET=wrong-secret\n")
    selected = None if selection == "none" else root / selection
    if selection == "directory":
        selected.mkdir()
    elif selection == "invalid-encoding":
        selected.write_bytes(b"\xff")
    result = _launch(
        root,
        ["-c", "import os; assert os.environ['PYTHONPATH'].endswith('/backend'); "
         "assert 'TEST_LAUNCH_SECRET' not in os.environ; raise SystemExit(19)"],
        paths_only=True, path_env_file=selected,
    )
    assert result.returncode == 19, result.stderr
    assert result.stderr == ""


def test_malformed_search_path_is_sanitized(launch_project):
    root = launch_project
    selected = root / "configuration"
    selected.write_bytes(b"PYTHONPATH=private-path\x00suffix\n")
    result = _launch(
        root, ["-c", "raise AssertionError('must not run')"],
        paths_only=True, path_env_file=selected,
    )
    assert result.returncode == 2
    assert "模块搜索路径" in result.stderr
    assert "private-path" not in result.stderr
    assert "Traceback" not in result.stderr
    assert str(root) not in result.stderr


@pytest.mark.parametrize("select_file", [False, True])
def test_prepare_python_path_only_changes_paths_in_current_process_and_children(
    launch_project, tmp_path, select_file,
):
    root = launch_project
    (root / ".env").write_text("PYTHONPATH=wrong-default\nTEST_LAUNCH_SECRET=wrong\n")
    selected = root / "configuration"
    selected.write_text(
        "PYTHONPATH='plugin source:backend::'\n"
        "DATABASE_URL=sqlite:///wrong.db\n"
        "EXTENSIONS_CONFIG=wrong.toml\n"
        "TEST_LAUNCH_SECRET=wrong\n"
        "SILICON_NOTEBOOK_ENV_FILE=wrong\n"
    )
    bootstrap = """
import importlib.util, json, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from python_env import prepare_python_path
original_environment = dict(os.environ)
original_arguments = list(sys.argv)
original_pid = os.getpid()
root = Path(sys.argv[2])
selected = Path(sys.argv[3]) if sys.argv[3] else None
prepare_python_path(root=root, path_env_file=selected)
expected_environment = {key: value for key, value in original_environment.items() if key != 'PYTHONPATH'}
assert {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'} == expected_environment
assert sys.argv == original_arguments
assert os.getpid() == original_pid
expected_paths = [str(root / 'backend'), str(root / 'additional')]
if selected is not None:
    import sample_plugin
    assert sample_plugin.VALUE == 'loaded'
    expected_paths.append(str(root / 'plugin source'))
    subprocess.run([sys.executable, '-c', 'import sample_plugin; assert sample_plugin.VALUE == "loaded"'], check=True)
else:
    assert importlib.util.find_spec('sample_plugin') is None
assert os.environ['PYTHONPATH'].split(os.pathsep) == expected_paths
assert sys.path[:len(expected_paths)] == expected_paths
print('prepared')
"""
    environment = dict(
        os.environ,
        PYTHONPATH="backend:additional::",
        SILICON_NOTEBOOK_ENV_FILE="  existing selection  ",
        DATABASE_URL="sqlite:///caller.db",
        EXTENSIONS_CONFIG="caller.toml",
        TEST_LAUNCH_SECRET="caller-credential",
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, str(SCRIPTS), str(root), str(selected) if select_file else ""],
        cwd=tmp_path, env=environment, text=True, capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "prepared"
    assert result.stderr == ""
