"""PostgreSQL allocation and launcher contracts without an ambient database."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.architecture import ci_partition
from tests.child_env_testkit import BYTECODE_ENV
from tests.postgres import ci_sharding, lane
from tests.postgres.targets import TARGET_SPECS, TARGETS_ENV

pytestmark = pytest.mark.postgres_lane_contract
ROOT = Path(__file__).resolve().parents[3]
PLUGIN = "tests.postgres.ci_sharding"


@pytest.mark.parametrize("arguments", [
    ["--shard-index", "0"], ["--shard-count", "2"], ["--preflight"], ["-k", "x"],
    ["--shard-index", "-1", "--shard-count", "2"],
    ["--shard-index", "2", "--shard-count", "2"],
    ["--shard-index", "0", "--shard-count", "1"],
    ["--shard-index", "0", "--shard-count", "3"],
    ["--shard-index", "00", "--shard-count", "2"],
    ["--shard-index", "0", "--shard-count", "2", "--shard-count", "2"],
    ["--shard-index", "0", "--shard-count", "2", "--shard-index", "1"],
    ["--shard-index", "0", "--shard-count", "2", "--maxfail=9"],
])
def test_invalid_launcher_arguments_fail_before_reading_targets(monkeypatch, arguments):
    def unexpected():
        pytest.fail("invalid invocation must fail before consulting database targets")
    monkeypatch.setattr(lane, "_configured_targets", unexpected)
    assert lane.main(arguments) == 2


@pytest.mark.parametrize("index", [None, 0, 1])
def test_launcher_selects_only_explicit_pair_and_preserves_full_gate(monkeypatch, index):
    seen = []
    targets = []
    monkeypatch.setattr(lane, "_configured_targets", lambda: targets)
    monkeypatch.setattr(lane, "_run_isolated_gate", lambda actual, **kwargs: seen.append((actual, kwargs)) or 17)
    monkeypatch.setattr(sys, "argv", ["caller", "--shard-index", "1", "--shard-count", "2"])
    args = None if index is None else ["--shard-count", "2", "--shard-index", str(index)]
    assert lane.main(args) == 17
    assert seen == [(targets, {} if index is None else {"shard": (index, 2)})]


@pytest.mark.parametrize("index", [None, 0, 1])
def test_pytest_command_preserves_markers_workers_and_report_identity(index):
    shard = None if index is None else (index, 2)
    command = lane._pytest_command(4, shard=shard)
    stem = "postgres" if index is None else f"postgres-shard{index}"
    assert command[command.index("-n") + 1] == "4"
    assert "--dist=loadgroup" in command and "--max-worker-restart=0" in command
    pytest_args = command[3:]
    assert pytest_args[pytest_args.index("-m") + 1] == "postgres_integration or postgres_lane_contract"
    assert command[-1] == "tests/postgres"
    assert f"--junitxml={lane.BACKEND_ROOT / '.local' / (stem + '-junit.xml')}" in command
    assert f"--ci-metrics={lane.BACKEND_ROOT / '.local' / (stem + '-metrics.json')}" in command
    if index is None:
        assert PLUGIN not in command and "--pg-shard-index" not in command
    else:
        assert command[command.index("--pg-shard-index") + 1] == str(index)
        assert command[command.index("--pg-shard-count") + 1] == "2"
        assert command[command.index(PLUGIN) - 1] == "-p"
    assert lane._preflight_command()[-1] == "--preflight"


def test_committed_pg_hints_are_positive_numeric_and_only_balance_ownership():
    costs, default = ci_partition.timings(ci_sharding._TIMINGS, lane="PostgreSQL")
    data = json.loads(ci_sharding._TIMINGS.read_text(encoding="utf-8"))
    assert costs and default == 0.1
    assert all(key.startswith("tests/postgres/") and key.endswith(".py") for key in costs)
    assert all(type(value) in (int, float) for key, value in data.items() if key != "seconds_per_test")
    assert costs.get("tests/postgres/test_new_unmeasured_module.py", default) == 0.1
    assert ci_sharding.BACKEND_PRIMARY_TEST_SECONDS == 9 * 60 * 4


@pytest.mark.parametrize("index,preflight_status", [(0, 0), (1, 0), (1, 7)])
def test_sharded_gate_keeps_preflight_credentials_and_fixed_parallel_topology(
    monkeypatch, index, preflight_status,
):
    for _, variable, _ in TARGET_SPECS:
        monkeypatch.delenv(variable, raising=False)
    groups = [{kind: f"postgresql://lane_user:sentinel-password@db.example/silicon_notebook_test_w{worker}_{kind}"
               for kind in ("primary", "non_c", "non_utf")} for worker in range(4)]
    monkeypatch.setenv(TARGETS_ENV, json.dumps(groups))
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only --pg-shard-index 0")
    monkeypatch.setenv("PYTEST_PLUGINS", PLUGIN)
    calls = []
    pgpass = []

    def child(command, environment, **kwargs):
        calls.append((command, environment))
        path = Path(environment["PGPASSFILE"])
        pgpass.append(path)
        assert path.exists() and path.stat().st_mode & 0o777 == 0o600
        assert len(path.read_text(encoding="utf-8").splitlines()) == 12
        assert "sentinel-password" not in repr(command) + repr(environment)
        assert "PYTEST_ADDOPTS" not in environment and "PYTEST_PLUGINS" not in environment
        assert not any("SHARD" in key for key in environment)
        if command[-1] == "--preflight":
            assert PLUGIN not in command
            return subprocess.CompletedProcess(command, preflight_status)
        assert command[command.index("-n") + 1] == "4"
        assert command[command.index("--pg-shard-index") + 1] == str(index)
        assert "--dist=loadgroup" in command and "--max-worker-restart=0" in command
        assert len(json.loads(environment[TARGETS_ENV])) == 4
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(lane, "_run_child", child)
    assert lane.main(["--shard-index", str(index), "--shard-count", "2"]) == preflight_status
    assert len(calls) == (1 if preflight_status else 2)
    if not preflight_status:
        assert calls[0][1] is calls[1][1]
    assert all(not path.exists() for path in pgpass)


def _environment():
    return {**os.environ, **BYTECODE_ENV, "PYTHONPATH": str(ROOT / "backend"),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": ""}


def _run(root, *args, environment=None):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(root / "pytest.ini"),
         "-p", "no:cacheprovider", "-q", *args], cwd=root,
        env=environment or _environment(), capture_output=True, text=True, timeout=40, check=False,
    )


def _suite(root):
    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    xdist_group: worker ownership\n    postgres_integration: PG lane\n",
        encoding="utf-8",
    )
    (root / "hints.json").write_text(json.dumps({
        "default_seconds_per_test": ci_sharding.BACKEND_PRIMARY_TEST_SECONDS * 2,
        "seconds_per_test": {"test_stale.py": 999999},
    }), encoding="utf-8")
    (root / "conftest.py").write_text(
        "import json, os\nfrom pathlib import Path\nimport pytest\n"
        "from tests.postgres import ci_sharding\n"
        "ci_sharding._TIMINGS = Path(__file__).parent / 'hints.json'\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item._native_id = item.nodeid\n"
        "        if item.name == 'test_dynamic':\n"
        "            item.add_marker(pytest.mark.xdist_group('shared'))\n"
        "    if os.environ.get('REVERSE_COLLECTION'): items.reverse()\n"
        "def pytest_collection_finish(session):\n"
        "    print('SELECTED=' + json.dumps([i._native_id for i in session.items]))\n"
        "@pytest.fixture(autouse=True)\n"
        "def identity(request, record_property):\n"
        "    record_property('native', request.node._native_id)\n"
        "    record_property('worker', os.environ.get('PYTEST_XDIST_WORKER', 'main'))\n",
        encoding="utf-8",
    )
    sources = {
        "test_a.py": "@pytest.mark.xdist_group('shared')\ndef test_shared(): pass\n"
        "@pytest.mark.parametrize('v', [1, 2], ids=['id@native', 'other'])\ndef test_plain(v): pass\n",
        "test_b.py": "def test_dynamic(): pass\ndef test_plain(): pass\n",
        "test_c.py": "@pytest.mark.xdist_group('a')\n@pytest.mark.xdist_group('b')\ndef test_combined(): pass\n",
        "test_d.py": "@pytest.mark.xdist_group('a_b')\ndef test_collision(): pass\n",
        "test_new.py": "def test_unmeasured(): pass\n",
    }
    for name, source in sources.items():
        (root / name).write_text("import pytest\npytestmark = pytest.mark.postgres_integration\n" + source,
                                 encoding="utf-8")
    (root / "test_other.py").write_text("def test_outside_lane(): pass\n", encoding="utf-8")


def _arguments(index):
    return ("-p", PLUGIN, "--pg-shard-index", str(index), "--pg-shard-count", "2")


def _collected(root, *args, environment=None):
    result = _run(root, "--collect-only", "-m", "postgres_integration", *args, environment=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    return set(json.loads(next(line.removeprefix("SELECTED=") for line in result.stdout.splitlines()
                               if line.startswith("SELECTED="))))


@pytest.mark.process_contract
def test_actual_pg_collection_is_exact_group_preserving_and_order_independent(tmp_path):
    _suite(tmp_path)
    whole = _collected(tmp_path)
    parts = [_collected(tmp_path, *_arguments(i)) for i in range(2)]
    assert len(whole) == 8 and whole == parts[0] | parts[1] and not parts[0] & parts[1]
    for group in ({"test_a.py::test_shared", "test_b.py::test_dynamic"},
                  {"test_c.py::test_combined", "test_d.py::test_collision"},
                  {"test_a.py::test_plain[id@native]", "test_a.py::test_plain[other]"}):
        assert sum(group <= part for part in parts) == 1
    assert [_collected(tmp_path, *_arguments(i), environment={**_environment(), "REVERSE_COLLECTION": "1"})
            for i in range(2)] == parts


@pytest.mark.process_contract
def test_real_pg_xdist_execution_preserves_identity_and_worker_groups(tmp_path):
    _suite(tmp_path)
    executed = {}
    for index in range(2):
        report = tmp_path / f"pg-{index}.xml"
        result = _run(tmp_path, *_arguments(index), "-p", "xdist.plugin", "-n", "2",
                      "--dist=loadgroup", "-m", "postgres_integration", f"--junitxml={report}")
        assert result.returncode == 0, result.stdout + result.stderr
        actual = {}
        for case in ET.parse(report).iter("testcase"):
            props = {p.get("name"): p.get("value") for p in case.iter("property")}
            assert props["native"] not in executed and props["native"] not in actual
            actual[props["native"]] = (index, props["worker"])
        assert set(actual) == _collected(tmp_path, *_arguments(index))
        executed.update(actual)
    assert set(executed) == _collected(tmp_path)
    assert executed["test_a.py::test_shared"] == executed["test_b.py::test_dynamic"]
    assert executed["test_c.py::test_combined"] == executed["test_d.py::test_collision"]


@pytest.mark.process_contract
def test_nested_pytest_does_not_inherit_pg_partition(tmp_path):
    _suite(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (nested / "test_inner.py").write_text("def test_a(): pass\ndef test_b(): pass\n", encoding="utf-8")
    (tmp_path / "test_nested.py").write_text(
        "import pytest, subprocess, sys\n@pytest.mark.postgres_integration\ndef test_nested():\n"
        "    result = subprocess.run([sys.executable, '-m', 'pytest', '-c', 'nested/pytest.ini',\n"
        "        '--noconftest', '--collect-only', '-q', 'nested'], capture_output=True, text=True, timeout=20)\n"
        "    assert result.returncode == 0, result.stdout + result.stderr\n"
        "    assert '2 tests collected' in result.stdout\n", encoding="utf-8",
    )
    owner = next(i for i in range(2) if "test_nested.py::test_nested" in _collected(tmp_path, *_arguments(i)))
    result = _run(tmp_path, *_arguments(owner), "-m", "postgres_integration")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.process_contract
def test_plugin_rejects_invalid_options_costs_and_empty_shards(tmp_path):
    _suite(tmp_path)
    for args in ((), ("--pg-shard-index", "0"), ("--pg-shard-count", "2"),
                 ("--pg-shard-index", "0", "--pg-shard-count", "1"),
                 (*_arguments(0)[2:], "--pg-shard-index", "1")):
        result = _run(tmp_path, "-p", PLUGIN, "--collect-only", *args)
        assert result.returncode != 0
    result = _run(tmp_path, *_arguments(0), "--collect-only", "test_new.py")
    assert result.returncode != 0 and "empty shard" in result.stderr
    (tmp_path / "hints.json").write_text('{"default_seconds_per_test": -1, "seconds_per_test": {}}', encoding="utf-8")
    result = _run(tmp_path, *_arguments(0), "--collect-only")
    assert result.returncode != 0 and "finite positive" in result.stderr


@pytest.mark.process_contract
def test_shell_wrapper_transports_only_explicit_pair_and_rejects_passthrough(tmp_path):
    capture = tmp_path / "capture.json"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'w') as target:\n"
        "    json.dump({'args': sys.argv[1:], 'env': dict(os.environ)}, target)\n", encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = {**_environment(), "PYTHON_BIN": str(fake_python), "CAPTURE": str(capture),
                   "TEST_POSTGRES_URL": "postgresql://u:p@localhost/silicon_notebook_test"}
    command = ["bash", str(ROOT / "scripts/check_postgres.sh")]
    for index in (None, 0, 1):
        args = [] if index is None else ["--shard-index", str(index), "--shard-count", "2"]
        result = subprocess.run(command + args, env=environment, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        observed = json.loads(capture.read_text(encoding="utf-8"))
        assert observed["args"] == ["-m", "tests.postgres.lane", *args]
        assert observed["env"].get("PYTEST_ADDOPTS", "") == ""
        assert observed["env"].get("PYTEST_PLUGINS", "") == ""
        assert not any("SHARD" in key for key in observed["env"])
    capture.unlink()
    for args in (["--shard-index", "0"], ["--shard-count", "2"], ["--maxfail=9"],
                 ["--shard-index", "2", "--shard-count", "2"],
                 ["--shard-index", "0", "--shard-index", "1", "--shard-count", "2"]):
        result = subprocess.run(command + args, env=environment, text=True, capture_output=True, timeout=10)
        assert result.returncode == 2 and not capture.exists()
