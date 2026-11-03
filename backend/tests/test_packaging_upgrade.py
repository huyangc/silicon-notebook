from __future__ import annotations

import importlib.util
import os
import platform
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("offline_package_upgrade", ROOT / "packaging/upgrade.py")
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)
REAL_INSPECT = upgrade.inspect_existing


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    source, target = tmp_path / "new release", tmp_path / "installed"
    for root in (source, target):
        root.mkdir()
        for name in upgrade.PAYLOAD:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name in {"backend/app", "frontend", "node", "scripts", "wheelhouse"}:
                path.mkdir()
                (path / "version.txt").write_text(root.name)
            else:
                path.write_text(root.name)
    arch = {"x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine().lower()]
    (source / "VERSION").write_text(f"platform={platform.system().lower()}-{arch}\n")
    (target / ".venv/bin").mkdir(parents=True)
    (target / ".venv/bin/python").write_text("old environment")
    preserved = {
        ".env": "DATABASE_URL=sqlite:///.local/customer.db\nSILICON_NOTEBOOK_STORAGE_DIR=uploads\n",
        ".local/customer.db": "existing database",
        ".local/model-services.toml": "operator model config",
        "custom-services.toml": "external configuration",
        "uploads/attachment.pdf": "existing attachment",
        "backend/customer-data.bin": "custom data outside app",
    }
    for name, content in preserved.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (target / ".env").chmod(0o600)
    monkeypatch.setattr(upgrade, "inspect_existing", lambda target: {
        "paths": [".local/customer.db", "uploads", ".local/model-services.toml", "custom-services.toml"],
        "packages": [],
    })
    return source, target, preserved


def fake_install(args, *, cwd, env, capture=False):
    if args[0] != "bash" or "-c" in args:
        return ""
    assert env["OFFLINE"] == "1"
    runtime = Path(env["VENV_DIR"])
    (runtime / "bin").mkdir(parents=True)
    (runtime / "bin/python").write_text("new environment")


def test_upgrade_keeps_config_data_paths_and_custom_files(deployment, monkeypatch):
    source, target, preserved = deployment
    monkeypatch.setattr(upgrade, "run_command", fake_install)
    upgrade.upgrade(source, target, target / ".local/offline-upgrade")
    for name, content in preserved.items():
        assert (target / name).read_text() == content
    assert (target / ".env").stat().st_mode & 0o777 == 0o600
    assert (target / "backend/app/version.txt").read_text() == source.name
    assert (target / "frontend/version.txt").read_text() == source.name
    assert (target / ".venv/bin/python").read_text() == "new environment"
    previous = list((target / ".local/upgrade-backups").glob("*/previous"))
    assert len(previous) == 1
    assert (previous[0] / ".venv/bin/python").read_text() == "old environment"
    assert not (target / ".local/offline-upgrade").exists()


def test_dependency_failure_does_not_change_existing_install(deployment, monkeypatch):
    source, target, preserved = deployment

    def fail(*args, **kwargs):
        fake_install(*args, **kwargs)
        raise subprocess.CalledProcessError(1, "offline-installer")

    monkeypatch.setattr(upgrade, "run_command", fail)
    with pytest.raises(subprocess.CalledProcessError):
        upgrade.upgrade(source, target, target / ".local/offline-upgrade")
    assert (target / "backend/app/version.txt").read_text() == target.name
    assert (target / ".venv/bin/python").read_text() == "old environment"
    assert not list((target / ".local/upgrade-venvs").iterdir())
    for name, content in preserved.items():
        assert (target / name).read_text() == content


def test_partial_replacement_rolls_back_and_can_be_retried(deployment, monkeypatch):
    source, target, _ = deployment
    monkeypatch.setattr(upgrade, "run_command", fake_install)
    replace = Path.replace

    def fail_once(path, destination):
        if path == target / ".local/offline-upgrade/next/frontend":
            raise OSError("simulated full disk")
        return replace(path, destination)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_once)
        with pytest.raises(OSError):
            upgrade.upgrade(source, target, target / ".local/offline-upgrade")
    for name in ("backend/app", "frontend", "node"):
        assert (target / name / "version.txt").read_text() == target.name
    upgrade.upgrade(source, target, target / ".local/offline-upgrade")
    assert (target / "frontend/version.txt").read_text() == source.name


def test_journal_creation_failure_leaves_original_startable(deployment, monkeypatch):
    source, target, _ = deployment

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(upgrade, "save_journal", fail)
    with pytest.raises(OSError):
        upgrade.upgrade(source, target, target / ".local/offline-upgrade")
    assert not (target / ".local/offline-upgrade").exists()
    assert (target / ".venv/bin/python").read_text() == "old environment"


def test_recover_handles_interruption_before_initial_journal(deployment, monkeypatch):
    _, target, _ = deployment
    work = target / ".local/offline-upgrade"
    work.mkdir()
    monkeypatch.setattr(sys, "argv", ["upgrade.py", "--target", str(target), "--recover"])
    assert upgrade.main() == 0
    assert not work.exists()


@pytest.mark.parametrize("after_incoming", [False, True])
def test_recover_after_interrupted_rename(deployment, after_incoming):
    source, target, _ = deployment
    work = target / ".local/offline-upgrade"
    (work / "previous").mkdir(parents=True)
    upgrade.save_journal(work, [{"name": "frontend", "existed": True}])
    (target / "frontend").replace(work / "previous/frontend")
    if after_incoming:
        shutil.copytree(source / "frontend", target / "frontend")
    upgrade.rollback(target, work)
    assert (target / "frontend/version.txt").read_text() == target.name


def test_running_service_and_symlinked_payload_are_rejected(deployment):
    source, target, _ = deployment
    (target / ".local/run").mkdir()
    pidfile = target / ".local/run/backend.pid"
    pidfile.write_text(str(os.getpid()))
    with pytest.raises(upgrade.UpgradeError, match="仍在运行"):
        upgrade.validate(source, target)
    pidfile.unlink()
    shutil.rmtree(target / "frontend")
    (target / "frontend").symlink_to(source / "frontend", target_is_directory=True)
    with pytest.raises(upgrade.UpgradeError, match="符号链接"):
        upgrade.validate(source, target)


@pytest.mark.parametrize("path", ["frontend/data.db", "scripts/storage", "model-services.example.toml"])
def test_upgrade_rejects_data_or_config_inside_release_files(deployment, monkeypatch, path):
    source, target, _ = deployment
    monkeypatch.setattr(upgrade, "inspect_existing", lambda target: {"paths": [path], "packages": []})
    with pytest.raises(upgrade.UpgradeError, match="重叠"):
        upgrade.upgrade(source, target, target / ".local/offline-upgrade")
    assert not (target / ".local/offline-upgrade").exists()


def test_extra_packages_require_explicit_offline_wheels(deployment, monkeypatch):
    source, target, _ = deployment
    monkeypatch.setattr(upgrade, "inspect_existing", lambda target: {"paths": [], "packages": [("corp-plugin", "1.2")]})
    with pytest.raises(upgrade.UpgradeError, match="extra-wheelhouse"):
        upgrade.upgrade(source, target, target / ".local/offline-upgrade")
    assert (target / ".venv/bin/python").read_text() == "old environment"


@pytest.mark.parametrize("protected", ["database", "env"])
def test_symlinked_protected_paths_cannot_hide_overlap(deployment, monkeypatch, tmp_path, protected):
    source, target, _ = deployment
    external = tmp_path / "database.db"
    external.touch()
    if protected == "database":
        (target / "frontend/customer.db").symlink_to(external)
        paths = ["frontend/customer.db"]
    else:
        (target / "frontend/settings.env").write_text("MODEL_SERVICES_CONFIG=\n")
        (target / ".env").unlink()
        (target / ".env").symlink_to("frontend/settings.env")
        paths = []
    monkeypatch.setattr(upgrade, "inspect_existing", lambda target: {"paths": paths, "packages": []})
    with pytest.raises(upgrade.UpgradeError, match="重叠"):
        upgrade.validate(source, target)


def test_extra_packages_are_installed_with_core_constraints(deployment, monkeypatch, tmp_path):
    source, target, _ = deployment
    monkeypatch.setattr(upgrade, "inspect_existing", lambda target: {"paths": [], "packages": [("corp-plugin", "1.2")]})
    calls = []

    def run(args, *, cwd, env, capture=False):
        calls.append(args)
        if args[0] == "bash" and "-c" not in args:
            fake_install(args, cwd=cwd, env=env)
        elif "-m" in args:
            assert env["PIP_CONFIG_FILE"] == "/dev/null"
            assert "PIP_FIND_LINKS" not in env
            if "install" in args:
                assert (cwd / "extra-requirements.txt").read_text() == "corp-plugin==1.2\n"
                assert "--no-index" in args and "--only-binary=:all:" in args
                assert str(tmp_path) in args

    monkeypatch.setattr(upgrade, "run_command", run)
    upgrade.upgrade(source, target, target / ".local/offline-upgrade", (tmp_path,))
    assert len(calls) == 4


@pytest.mark.process_contract
def test_preflight_uses_startup_shell_expansion_and_allows_core_pythonpath(deployment, monkeypatch):
    source, target, _ = deployment
    runtime = target / ".venv/bin/python"
    runtime.unlink()
    runtime.symlink_to(sys.executable)
    core = target / "backend/app/core"
    core.mkdir(parents=True)
    (core.parent / "__init__.py").touch()
    (core / "__init__.py").touch()
    (core / "config.py").write_text('''import os
class Settings:
    storage_dir = os.environ["SILICON_NOTEBOOK_STORAGE_DIR"]
    model_services_config = os.environ["MODEL_SERVICES_CONFIG"]
    extensions_config = ""
    database_url = os.environ["DATABASE_URL"]
    sqlite_path = database_url[len("sqlite:///"):]
''')
    (target / ".env").write_text('''DATABASE_URL=sqlite:///$PWD/frontend/customer.db
SILICON_NOTEBOOK_STORAGE_DIR=.local/storage
MODEL_SERVICES_CONFIG=
PYTHONPATH=backend
''')
    monkeypatch.setattr(upgrade, "inspect_existing", REAL_INSPECT)
    result = upgrade.inspect_existing(target)
    assert str(target / "frontend/customer.db") in result["paths"]
    assert "backend" not in result["paths"]
    with pytest.raises(upgrade.UpgradeError, match="重叠"):
        upgrade.validate(source, target)


@pytest.mark.process_contract
@pytest.mark.parametrize("selection", [".local/deployment.env", "frontend/deployment.env", "absolute", "symlink", ""])
def test_upgrade_preflight_preserves_selected_environment_file(deployment, monkeypatch, selection):
    source, target, _ = deployment
    runtime = target / ".venv/bin/python"
    runtime.unlink()
    runtime.symlink_to(sys.executable)
    core = target / "backend/app/core"
    core.mkdir(parents=True)
    (core.parent / "__init__.py").touch()
    (core / "__init__.py").touch()
    (core / "config.py").write_text('''import os
class Settings:
    storage_dir = os.environ["SILICON_NOTEBOOK_STORAGE_DIR"]
    model_services_config = os.environ["MODEL_SERVICES_CONFIG"]
    extensions_config = ""
    database_url = os.environ["DATABASE_URL"]
    sqlite_path = database_url[len("sqlite:///"):]
''')
    symlink_selection = selection == "symlink"
    if symlink_selection:
        selection = "frontend/deployment.env"
    elif selection == "absolute":
        selection = str(target / ".local/deployment.env")
    config = (target / selection) if selection else None
    database = ".local/customer.db" if not selection or selection == "frontend/deployment.env" else "frontend/customer.db"
    if config:
        if symlink_selection:
            (target / ".local/selected-real.env").touch()
            config.symlink_to("../.local/selected-real.env")
        config.write_text(f"DATABASE_URL=sqlite:///{database}\nPYTHONPATH=backend\n")
    (target / ".env").write_text(
        f"SILICON_NOTEBOOK_ENV_FILE='{selection}'\n"
        "SILICON_NOTEBOOK_STORAGE_DIR=.local/storage\nMODEL_SERVICES_CONFIG=\n"
        + ("DATABASE_URL=sqlite:///.local/customer.db\n" if not selection else "")
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(upgrade, "inspect_existing", REAL_INSPECT)
    result = upgrade.inspect_existing(target)
    assert database in result["paths"]
    if selection:
        assert str(config.resolve()) in result["paths"]
        with pytest.raises(upgrade.UpgradeError, match="重叠"):
            upgrade.validate(source, target)
    else:
        assert str(target / ".env") not in result["paths"]


@pytest.mark.process_contract
def test_config_check_merges_inherited_root_and_selected_pythonpaths(deployment, monkeypatch):
    _, target, _ = deployment
    (target / ".env").write_text("PYTHONPATH=config-plugin\nSILICON_NOTEBOOK_ENV_FILE=.local/deployment.env\n")
    (target / ".local/deployment.env").write_text("PYTHONPATH=selected-plugin\n")
    monkeypatch.setenv("PYTHONPATH", "inherited-plugin")
    output = upgrade.run_config_python(Path(sys.executable),
        "import os; print(os.environ['PYTHONPATH'])", target)
    assert output.strip().split(os.pathsep) == [
        str(target / "backend"), str(target / "inherited-plugin"),
        str(target / "config-plugin"), str(target / "selected-plugin"),
    ]


@pytest.mark.process_contract
def test_cancellation_reaps_installer_process_group(tmp_path):
    installer = tmp_path / "installer.py"
    installer.write_text("""import subprocess, sys, signal
child = subprocess.Popen([sys.executable, '-c', 'import signal; signal.pause()'])
print(child.pid, flush=True)
signal.pause()
""")
    runner = tmp_path / "runner.py"
    runner.write_text(f"""import runpy, signal, sys
from pathlib import Path
module = runpy.run_path({str(ROOT / 'packaging/upgrade.py')!r})
def cancel(signum, frame):
    raise KeyboardInterrupt
signal.signal(signal.SIGTERM, cancel)
try:
    module['run_command']([sys.executable, {str(installer)!r}], cwd=Path({str(tmp_path)!r}))
except KeyboardInterrupt:
    print('restored', flush=True)
""")
    process = subprocess.Popen([sys.executable, str(runner)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        child_pid = int(process.stdout.readline().strip())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert "restored" in stdout
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        process.kill() if process.poll() is None else None
        process.wait()


@pytest.mark.process_contract
@pytest.mark.parametrize("wheelhouse", [False, True])
def test_strict_offline_installer_never_falls_back_to_index(tmp_path, wheelhouse):
    bundle = tmp_path / "bundle"
    (bundle / "backend").mkdir(parents=True)
    shutil.copy2(ROOT / "packaging/install.sh", bundle / "install.sh")
    (bundle / "backend/requirements.txt").write_text("")
    runtime = bundle / ".venv/bin/python"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("""#!/bin/bash
if [[ ${1:-} == -m && ${2:-} == pip ]]; then
  printf '%s\\n' "$*" >> "$OFFLINE_PIP_EVENTS"
  [[ ${PIP_CONFIG_FILE:-} == /dev/null && -z ${PIP_FIND_LINKS:-} ]] || exit 9
  exit 1
fi
exit 0
""")
    runtime.chmod(0o755)
    if wheelhouse:
        (bundle / "wheelhouse").mkdir()
        (bundle / "wheelhouse/fake.whl").touch()
        (bundle / "wheelhouse/locked-wheels.txt").touch()
    events = tmp_path / "pip-events"
    result = subprocess.run(["bash", "install.sh"], cwd=bundle, capture_output=True, text=True,
                            env={**os.environ, "OFFLINE": "1", "PYTHON_BIN": str(runtime), "OFFLINE_PIP_EVENTS": str(events), "PIP_FIND_LINKS": "https://must-not-be-used.invalid/wheels"})
    assert result.returncode != 0
    assert "离线" in result.stderr
    calls = events.read_text().splitlines() if events.exists() else []
    assert len(calls) == int(wheelhouse)
    assert all("--no-index" in call for call in calls)
