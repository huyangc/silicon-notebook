"""The public command catalog preserves each engine's process-level contract."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("unified_cli_under_test", ROOT / "scripts/cli.py")
cli = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(cli)


@pytest.mark.parametrize("argv", [[], ["--help"], ["maintain"], ["database", "--help"], ["kg", "--help"]])
def test_catalog_help_is_stdlib_only(argv, tmp_path):
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "scripts/cli.py"), *argv],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "EXTENSIONS_CONFIG": "/missing/plugin-config.toml"},
    )
    assert result.returncode == 0
    assert "用法:" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("argv", [["not-a-command"], ["maintain", "../batch_ingest.py"]])
def test_unknown_command_does_not_read_environment_or_dispatch(monkeypatch, capsys, argv):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid commands must not prepare an environment or execute an engine")

    monkeypatch.setattr(cli, "_exec_without_dotenv", forbidden)
    monkeypatch.setitem(sys.modules, "python_env", types.SimpleNamespace(exec_python=forbidden))
    assert cli.main(argv) == 2
    assert "未知" in capsys.readouterr().err


@pytest.mark.parametrize("alias", ["batch", "batch-ingest"])
def test_batch_preserves_every_argument_and_uses_shared_environment(monkeypatch, alias):
    calls = []
    monkeypatch.setitem(sys.modules, "python_env", types.SimpleNamespace(
        exec_python=lambda arguments, **kwargs: calls.append((arguments, kwargs)),
    ))
    rest = ["ingest", "path with spaces", "", "--", "literal;$(value)"]
    assert cli.main([alias, *rest]) == 0
    assert calls == [([str(ROOT / "scripts/batch_ingest.py"), *rest], {"root": ROOT})]


def test_module_dispatch_preserves_confirmation(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "python_env", types.SimpleNamespace(
        exec_python=lambda arguments, **kwargs: calls.append(arguments),
    ))
    assert cli.main(["kg", "build", "notebook id", "--confirm-service-stopped"]) == 0
    assert calls == [["-m", "app.scripts.build_kg", "notebook id", "--confirm-service-stopped"]]


def test_literal_help_after_option_terminator_does_not_change_environment(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "python_env", types.SimpleNamespace(
        exec_python=lambda arguments, **kwargs: calls.append(arguments),
    ))
    assert cli.main(["batch", "ingest", "--", "--help"]) == 0
    assert calls == [[str(ROOT / "scripts/batch_ingest.py"), "ingest", "--", "--help"]]


@pytest.mark.parametrize("path", [key for key, command in cli.COMMANDS.items() if command.positional_usage])
def test_legacy_positional_help_never_imports_app_or_opens_database(path, tmp_path):
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "scripts/cli.py"), *path, "--help"],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": "this-is-not-a-database", "EXTENSIONS_CONFIG": "/missing.toml"},
    )
    assert result.returncode == 0
    assert "<notebook_id>" in result.stdout
    assert "--confirm-service-stopped" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("path", [
    key for key, command in cli.COMMANDS.items()
    if not command.deployment_env and key not in cli.SELECTED_ENV_COMMANDS
])
def test_standalone_tools_keep_their_environment_policy(monkeypatch, path):
    calls = []
    monkeypatch.setattr(cli, "_exec_without_dotenv", calls.append)
    assert cli.main([*path, "--env-file", "chosen.env"]) == 0
    assert calls == [[str(ROOT / "scripts" / cli.COMMANDS[path].script), "--env-file", "chosen.env"]]


@pytest.mark.parametrize("path", [("maintain", "selected-source")])
@pytest.mark.parametrize("options", [["--env-file", "chosen.env"], ["--env-file=chosen.env"], ["--env", "chosen.env"]])
def test_selected_file_only_contributes_import_paths(monkeypatch, tmp_path, path, options):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", "exported-database")
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "ambient.env")
    calls = []
    monkeypatch.setitem(sys.modules, "python_env", types.SimpleNamespace(
        exec_python=lambda arguments, **kwargs: calls.append((arguments, kwargs)),
    ))
    rest = options
    assert cli.main([*path, *rest]) == 0
    assert calls == [(
        [str(ROOT / "scripts" / cli.COMMANDS[path].script), *rest],
        {"root": ROOT, "paths_only": True, "path_env_file": tmp_path / "chosen.env"},
    )]
    assert os.environ["DATABASE_URL"] == "exported-database"
    assert os.environ["SILICON_NOTEBOOK_ENV_FILE"] == "ambient.env"


def test_selected_source_default_ignores_ambient_file(monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "/ambient.env")
    assert cli._selected_path_env_file([]) == ROOT / ".env"


@pytest.mark.parametrize("path, rest", [
    (("eval", "shadow"), ["--dry-run", "--env-file", "/unreadable.env", "report"]),
    (("eval", "shadow"), ["--dry", "--env-file", "/unreadable.env", "report"]),
    (("eval", "shadow"), ["--help", "--env-file", "/unreadable.env"]),
    (("maintain", "selected-source"), ["--help", "--env-file", "/unreadable.env"]),
    (("maintain", "selected-source"), ["--env-file"]),
])
def test_selected_file_help_and_dry_run_do_not_read_deployment_file(monkeypatch, path, rest):
    calls = []
    monkeypatch.setattr(cli, "_exec_without_dotenv", calls.append)
    assert cli.main([*path, *rest]) == 0
    assert calls == [[str(ROOT / "scripts" / cli.COMMANDS[path].script), *rest]]


def test_selected_file_option_terminator_and_last_value(monkeypatch):
    monkeypatch.delenv("SILICON_NOTEBOOK_ENV_FILE", raising=False)
    assert cli._selected_path_env_file(
        ["--", "--env-file", "/ignored.env"],
    ) == ROOT / ".env"
    assert cli._selected_path_env_file(
        ["--env-file", "/first.env", "--env-file=/last.env"],
    ) == Path("/last.env")


@pytest.mark.parametrize("path, script", [
    (("maintain", "selected-source"), "prepare_selected_source_graph.py"),
])
def test_selected_file_plugin_import_preserves_settings_precedence(tmp_path, path, script):
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    plugins = repository / "plugins"
    plugins.mkdir()
    (plugins / "selected_plugin.py").write_text("VALUE = 'selected-plugin'\n")
    (repository / ".env").write_text("PYTHONPATH=wrong-plugin-directory\nDATABASE_URL=wrong-root-db\n")
    caller = tmp_path / "caller"
    caller.mkdir()
    (caller / "chosen.env").write_text(
        "PYTHONPATH=plugins\nDATABASE_URL=wrong-selected-db\n"
        "EXTENSIONS_CONFIG=wrong-selected-config\nSILICON_NOTEBOOK_ENV_FILE=wrong-file\n",
    )
    (scripts / script).write_text(
        "import json, os, sys, selected_plugin\n"
        "print(json.dumps({'plugin': selected_plugin.VALUE, 'argv': sys.argv[1:], "
        "'database': os.environ['DATABASE_URL'], 'extensions': os.environ['EXTENSIONS_CONFIG'], "
        "'env_file': os.environ['SILICON_NOTEBOOK_ENV_FILE']}))\n",
    )
    rest = ["--env-file", "chosen.env"]
    code = (
        "import importlib.util, pathlib, sys; "
        f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
        f"s=importlib.util.spec_from_file_location('cli', {str(ROOT / 'scripts/cli.py')!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"m.ROOT=pathlib.Path({str(repository)!r}); "
        f"m.main({[*path, *rest]!r})"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=caller, capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": "exported-db", "EXTENSIONS_CONFIG": "exported-config",
             "SILICON_NOTEBOOK_ENV_FILE": "ambient.env", "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "plugin": "selected-plugin", "argv": rest, "database": "exported-db",
        "extensions": "exported-config", "env_file": "ambient.env",
    }


@pytest.mark.parametrize("rest", [[], ["--since", "24"], ["db", "--help"], ["incident", "request-id"]])
def test_diag_preserves_default_and_subcommands_without_dotenv(monkeypatch, rest):
    calls = []
    monkeypatch.setattr(cli, "_exec_without_dotenv", calls.append)
    assert cli.main(["diag", *rest]) == 0
    assert calls == [[str(ROOT / "scripts/diag.py"), *rest]]


@pytest.mark.parametrize("exit_statement, expected_status", [
    ("raise SystemExit(23)", 23),
    ("os.kill(os.getpid(), 15)", -signal.SIGTERM),
])
def test_exec_preserves_cwd_arguments_and_exit_status(tmp_path, exit_statement, expected_status):
    scripts = tmp_path / "repository/scripts"
    scripts.mkdir(parents=True)
    (scripts / "diag.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}), flush=True)\n"
        f"{exit_statement}\n",
    )
    caller = tmp_path / "caller directory"
    caller.mkdir()
    code = (
        "import importlib.util, pathlib; "
        f"s=importlib.util.spec_from_file_location('cli', {str(ROOT / 'scripts/cli.py')!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"m.ROOT=pathlib.Path({str(scripts.parent)!r}); "
        "m.main(['diag', 'space value', '', '--', 'literal;$(value)'])"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=caller, capture_output=True, text=True)
    assert result.returncode == expected_status
    assert json.loads(result.stdout) == {
        "argv": ["space value", "", "--", "literal;$(value)"], "cwd": str(caller),
    }


def test_diag_help_runs_without_site_packages(tmp_path):
    result = subprocess.run(
        [sys.executable, "-S", str(ROOT / "scripts/cli.py"), "diag", "--help"],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "EXTENSIONS_CONFIG": "/missing.toml"},
    )
    assert result.returncode == 0
    assert "incident" in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("path", [("batch",), ("scale",), ("extensions", "parity")])
def test_application_leaf_help_does_not_load_plugins_or_open_database(path, tmp_path):
    invalid_plugins = tmp_path / "extensions.toml"
    invalid_plugins.write_text("this is deliberately invalid TOML\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/cli.py"), *path, "--help"],
        cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "DATABASE_URL": "invalid-database-url",
             "EXTENSIONS_CONFIG": str(invalid_plugins), "MODEL_SERVICES_CONFIG": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "--help" in result.stdout
    assert list(tmp_path.iterdir()) == [invalid_plugins]


@pytest.mark.parametrize("interpreter", ["", "missing-python"])
def test_shell_rejects_invalid_explicit_interpreter(tmp_path, interpreter):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/cli.sh"), "--help"], cwd=tmp_path,
        capture_output=True, text=True,
        env={**os.environ, "PYTHON_BIN": interpreter},
    )
    assert result.returncode == 127
    assert "PYTHON_BIN" in result.stderr


def test_shell_passes_arguments_and_preserves_cwd(tmp_path):
    interpreter = tmp_path / "python with spaces"
    interpreter.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$PWD\" \"$@\"\n"
        "exit 19\n",
    )
    interpreter.chmod(0o700)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/cli.sh"), "batch", "path with spaces", ""], cwd=tmp_path,
        capture_output=True, text=True,
        env={**os.environ, "PYTHON_BIN": str(interpreter)},
    )
    assert result.returncode == 19
    assert result.stdout.splitlines() == [str(tmp_path), str(ROOT / "scripts/cli.py"), "batch", "path with spaces", ""]
