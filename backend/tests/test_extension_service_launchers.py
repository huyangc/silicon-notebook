"""Real shell launchers with fake services/processes; never bind host ports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tarfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]


def executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def wait_until(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("fake process did not reach expected lifecycle state")
        time.sleep(0.02)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def launcher(tmp_path):
    root = tmp_path / "checkout"
    for directory in ("scripts", "backend", "frontend/node_modules/.bin", "fake-bin", ".venv/bin", "node/bin"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in ("dev.sh", "prod.sh", "backend.sh", "stop.sh", "extension_services.sh"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    for name in ("start.sh", "stop.sh"):
        shutil.copy2(ROOT / "packaging" / name, root / name)
    (root / "scripts/autotune.sh").write_text("# no host configuration\n")
    (root / ".env").write_text("# isolated deployment\n")
    (root / "frontend/server.js").write_text("// fake node does not evaluate this\n")
    (root / "scripts/extension_services.py").write_text('''
import json, os, pathlib, sys
action = sys.argv[1]
receipt = pathlib.Path(sys.argv[sys.argv.index("--receipt") + 1]) if "--receipt" in sys.argv else None
if action == "start" and receipt:
    receipt.write_text(json.dumps({"run_id": "owned-test-run", "owned": True}))
record = {"action": action, "receipt": str(receipt) if receipt else None}
if receipt and receipt.exists() and receipt.read_text():
    record["ownership"] = json.loads(receipt.read_text())
with open(os.environ["SERVICE_EVENTS"], "a") as stream:
    stream.write(json.dumps(record) + "\\n")
sys.exit(7 if action == "start" and os.environ.get("FAIL_SERVICE") == "1" else 0)
''')
    executable(root / "fake_child.sh", '''#!/bin/bash
echo "$$" > "$FAKE_ROOT/$1.pid"
[[ "${FAIL_MAIN:-}" == "$1" ]] && exit 8
exec sleep 60
''')
    python_wrapper = '''#!/bin/bash
case "${1:-}" in
  */extension_services.py) exec "$REAL_PYTHON" "$@" ;;
  */python_env.py)
    if [[ "${2:-}" == "-c" ]]; then
      [[ "${FAIL_DATABASE:-0}" == 1 ]] && exit 2
      echo "sqlite test deployment"; exit 0
    fi
    exec /bin/bash "$FAKE_ROOT/fake_child.sh" backend ;;
  -c) exec "$REAL_PYTHON" "$@" ;;
esac
exit 0
'''
    executable(root / "fake-bin/python", python_wrapper)
    executable(root / ".venv/bin/python", python_wrapper)
    frontend_wrapper = '#!/bin/bash\nexec /bin/bash "$FAKE_ROOT/fake_child.sh" frontend\n'
    for path in ("fake-bin/npm", "frontend/node_modules/.bin/next", "node/bin/node"):
        executable(root / path, frontend_wrapper)
    for name in ("ss", "lsof", "fuser"):
        executable(root / "fake-bin" / name, "#!/bin/sh\nexit 0\n")
    executable(root / "fake-bin/curl", '''#!/bin/bash
case "$*" in
  */openapi.json*) echo '{"info":{"title":"silicon-notebook API"}}' ;;
  */api/ready*)
    if [[ -f "$FAKE_ROOT/backend.pid" ]]; then echo '{"ready":true}'
    else echo '{"ready":false}'; fi ;;
esac
''')
    env = {
        "PATH": f"{root / 'fake-bin'}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "REAL_PYTHON": sys.executable, "PYTHON_BIN": str(root / "fake-bin/python"),
        "FAKE_ROOT": str(root), "SERVICE_EVENTS": str(root / "service-events.jsonl"),
        "SKIP_INSTALL": "1", "SKIP_BUILD": "1", "START_TIMEOUT_SECONDS": "3",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    yield root, env
    for name in ("backend", "frontend"):
        path = root / f"{name}.pid"
        if path.exists():
            try:
                os.kill(int(path.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass


def run(root, env, path, *args):
    return subprocess.run(["bash", str(root / path), *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=15)


def events(root):
    return [json.loads(line) for line in (root / "service-events.jsonl").read_text().splitlines()]


def assert_owned_cleanup(root):
    records = events(root)
    assert [record["action"] for record in records] == ["start", "stop"]
    assert records[0]["receipt"] == records[1]["receipt"]
    assert records[1]["ownership"] == {"run_id": "owned-test-run", "owned": True}
    assert not Path(records[0]["receipt"]).exists()


@pytest.mark.parametrize("path,args", [
    ("scripts/dev.sh", ()), ("scripts/prod.sh", ()),
    ("scripts/backend.sh", ("start",)), ("start.sh", ()),
])
def test_service_start_failure_prevents_application_start(launcher, path, args):
    root, env = launcher
    result = run(root, {**env, "FAIL_SERVICE": "1"}, path, *args)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (root / "backend.pid").exists()
    assert not (root / "frontend.pid").exists()
    assert_owned_cleanup(root)


def test_development_signal_exit_cleans_owned_receipt(launcher):
    root, env = launcher
    with subprocess.Popen(["bash", str(root / "scripts/dev.sh")], cwd=root,
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
        try:
            wait_until(lambda: (root / "backend.pid").exists() and (root / "frontend.pid").exists())
            process.terminate()
            assert process.wait(timeout=8) != 0
        finally:
            if process.poll() is None:
                process.kill()
    assert_owned_cleanup(root)
    for name in ("backend", "frontend"):
        pid = int((root / f"{name}.pid").read_text())
        wait_until(lambda: not alive(pid))


@pytest.mark.parametrize("path,args", [
    ("scripts/prod.sh", ()), ("scripts/backend.sh", ("start",)), ("start.sh", ()),
])
def test_background_success_hands_off_without_stopping_services(launcher, path, args):
    root, env = launcher
    result = run(root, env, path, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [record["action"] for record in events(root)] == ["start"]
    assert not Path(events(root)[0]["receipt"]).exists()
    wait_until(lambda: (root / "backend.pid").exists())
    assert alive(int((root / "backend.pid").read_text()))


@pytest.mark.parametrize("failed", ["backend", "frontend"])
def test_packaged_application_failure_rolls_back_other_child_and_services(launcher, failed):
    root, env = launcher
    # The real launcher has a fixed startup observation interval. Synchronize
    # that observation with fake-child entry, so heavily loaded xdist workers
    # test a process that failed, rather than one not scheduled to execute yet.
    executable(root / "fake-bin/sleep", '''#!/bin/bash
if [[ "$1" == "2" ]]; then
  for ((attempt=0; attempt<500; attempt++)); do
    if [[ -f "$FAKE_ROOT/backend.pid" && -f "$FAKE_ROOT/frontend.pid" ]]; then
      exec /bin/sleep 0.1
    fi
    /bin/sleep 0.02
  done
  exit 1
fi
exec /bin/sleep "$@"
''')
    result = run(root, {**env, "FAIL_MAIN": failed}, "start.sh")
    assert result.returncode != 0, result.stdout + result.stderr
    assert_owned_cleanup(root)
    for name in ("backend", "frontend"):
        pid = int((root / f"{name}.pid").read_text())
        wait_until(lambda: not alive(pid))
        assert not (root / ".local/run" / f"{name}.pid").exists()


@pytest.mark.parametrize("path,args", [
    ("scripts/stop.sh", ()), ("scripts/backend.sh", ("stop",)), ("stop.sh", ()),
])
def test_stop_without_host_process_still_stops_companion_services(launcher, path, args):
    root, env = launcher
    result = run(root, env, path, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events(root) == [{"action": "stop", "receipt": None}]


@pytest.mark.parametrize("action", ["stop", "status"])
def test_backend_diagnostics_failure_does_not_block_saved_service_controls(launcher, action):
    root, env = launcher
    result = run(root, {**env, "FAIL_DATABASE": "1"}, "scripts/backend.sh", action)
    assert result.returncode != 0
    assert events(root) == [{"action": action, "receipt": None}]


def test_offline_archive_ships_executable_service_runtime_without_a_real_build(launcher):
    root, env = launcher
    helpers = (
        "extension_services.sh", "extension_services.py", "extension_service_runtime.py",
        "extension_service_worker.py", "check_cli_extensions.py", "python_env.py",
    )
    for name in (*helpers, "pack.sh", "migrate_legacy_model_env.py"):
        shutil.copy2(ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copytree(ROOT / "packaging", root / "packaging")
    for name in (".env.example", "model-services.example.toml"):
        shutil.copy2(ROOT / name, root / name)
    (root / "backend/app").mkdir()
    (root / "backend/app/__init__.py").write_text("")
    (root / "backend/requirements.txt").write_text("")
    for directory in ("standalone", "static"):
        (root / "frontend/.next" / directory).mkdir(parents=True)
    (root / "frontend/.next/standalone/server.js").write_text("// prebuilt fixture\n")
    executable(root / "fake-bin/npm", "#!/bin/sh\nexit 0\n")
    executable(root / "fake-bin/node", "#!/bin/sh\necho v22.0.0\n")
    node_tar = root / "node-fixture.tar"
    with tarfile.open(node_tar, "w") as archive:
        archive.add(root / "fake-bin/node", arcname="node-fixture/bin/node")
    result = run(root, {
        **env, "PACK_PYTHON": sys.executable, "VERSION": "fixture",
        "NODE_TARBALL": str(node_tar), "NODE_VERSION": "v22.0.0", "SKIP_WHEELHOUSE": "1",
    }, "scripts/pack.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    [release] = (root / "dist").glob("silicon_notebook_*.tar.gz")
    with tarfile.open(release) as archive:
        for helper in helpers:
            [member] = [entry for entry in archive.getmembers() if entry.name.endswith("/scripts/" + helper)]
            assert archive.extractfile(member).read() == (ROOT / "scripts" / helper).read_bytes()
    [stage] = (root / "dist/stage").iterdir()
    help_result = subprocess.run(
        [sys.executable, str(stage / "scripts/extension_services.py"), "--help"],
        cwd=stage, env=env, capture_output=True, text=True, timeout=5,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "validate" in help_result.stdout
