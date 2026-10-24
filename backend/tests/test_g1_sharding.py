"""Partition semantics plus hermetic, real pytest/xdist process contracts."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.architecture import g1_sharding as shard
from tests.child_env_testkit import BYTECODE_ENV

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "tests.architecture.g1_sharding"


def _item(*marks, module="test_plain.py"):
    return SimpleNamespace(
        path=Path("/suite") / module,
        config=SimpleNamespace(rootpath=Path("/suite")),
        iter_markers=lambda _name: iter(marks),
    )


@pytest.mark.parametrize(("marks", "native", "expected"), [
    ((), "test_a.py::test_plain[id@native]", ("module", "test_plain.py")),
    ((), "test_a.py::test_plain@native", ("group", "native")),
    ((pytest.mark.xdist_group(),), "test_a.py::test_a", ("group", "default")),
    ((pytest.mark.xdist_group("b"), pytest.mark.xdist_group(name="a"),
      pytest.mark.xdist_group("a")), "test_a.py::test_a", ("group", "a_b")),
    ((pytest.mark.xdist_group("a_b"),), "test_b.py::test_b", ("group", "a_b")),
    ((pytest.mark.xdist_group("prefix@tail"),), "test_a.py::test_a", ("group", "tail")),
    ((pytest.mark.xdist_group("group]"),), "test_a.py::test_a",
     ("group", "test_a.py::test_a@group]")),
    ((pytest.mark.xdist_group("shared"),), "test_a.py::test_a[id@native]",
     ("group", "shared")),
])
def test_units_match_effective_loadgroup_keys(marks, native, expected):
    assert shard._unit(_item(*marks), native) == expected


def test_lpt_accounts_for_the_second_runners_later_work_and_is_order_independent():
    units = {("module", name): [name] for name in "abcd"}
    costs = dict(a=1000, b=800, c=600, d=400)
    expected = shard._owners(units, costs)
    assert expected == {
        ("module", "a"): 0, ("module", "b"): 1,
        ("module", "c"): 0, ("module", "d"): 0,
    }
    assert shard._owners(dict(reversed(list(units.items()))), costs) == expected
    assert shard._owners(units, costs, extra_seconds=0) != expected


@pytest.mark.parametrize("units,costs", [({}, {}), ({("module", "a"): ["a"]}, {"a": 1}),
    ({("module", "a"): ["a"], ("module", "b"): ["b"]}, {"a": 1, "b": 1})])
def test_empty_partitions_fail_closed(units, costs):
    with pytest.raises(pytest.UsageError, match="empty shard"):
        shard._owners(units, costs)


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan"), "1", None, 10**400])
def test_invalid_costs_fail_closed(tmp_path, value):
    path = tmp_path / "timings.json"
    for data in (
        {"default_seconds_per_test": value, "seconds_per_test": {}},
        {"default_seconds_per_test": 1, "seconds_per_test": {"tests/a.py": value}},
    ):
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(pytest.UsageError, match="finite positive"):
            shard._timings(path)


def test_timing_hints_do_not_define_collection_membership(tmp_path):
    path = tmp_path / "timings.json"
    path.write_text(json.dumps({
        "default_seconds_per_test": 0.25,
        "seconds_per_test": {"tests/stale.py": 999, "tests/known.py": 0.5},
    }), encoding="utf-8")
    hints, default = shard._timings(path)
    assert [hints.get(module, default) for module in ("tests/new.py", "tests/known.py")] == [0.25, 0.5]
    assert "tests/stale.py" in hints


def test_committed_hints_have_positive_costs_and_numeric_metadata():
    hints, default = shard._timings(shard._TIMINGS)
    data = json.loads(shard._TIMINGS.read_text(encoding="utf-8"))
    assert hints and default > 0
    assert all(type(value) in (int, float) for key, value in data.items() if key != "seconds_per_test")
    assert all(key.startswith("tests/") and key.endswith(".py") for key in hints)


def _environment():
    return {
        **os.environ, **BYTECODE_ENV,
        "PYTHONPATH": str(ROOT / "backend"), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "",
    }


def _run(root, *args, environment=None):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(root / "pytest.ini"),
         "-p", "no:cacheprovider", "-q", *args], cwd=root,
        env=environment or _environment(), text=True, capture_output=True,
        timeout=40, check=False,
    )


def _suite(root):
    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    xdist_group: shared worker\n    slow: extended\n",
        encoding="utf-8",
    )
    (root / "hints.json").write_text(json.dumps({
        "default_seconds_per_test": 1000,
        "seconds_per_test": {"test_stale.py": 20000, "test_a.py": 1200},
    }), encoding="utf-8")
    (root / "conftest.py").write_text(
        "import json, os\nfrom pathlib import Path\nimport pytest\n"
        "from tests.architecture import g1_sharding\n"
        "g1_sharding._TIMINGS = Path(__file__).parent / 'hints.json'\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        item._native_id = item.nodeid\n"
        "        if item.name == 'test_late_group':\n"
        "            item.add_marker(pytest.mark.xdist_group(name='shared'))\n"
        "    if os.environ.get('REVERSE_COLLECTION'):\n"
        "        items.reverse()\n"
        "def pytest_collection_finish(session):\n"
        "    print('SELECTED=' + json.dumps([item._native_id for item in session.items]))\n"
        "@pytest.fixture(autouse=True)\n"
        "def identity(request, record_property):\n"
        "    record_property('native', request.node._native_id)\n"
        "    record_property('worker', os.environ.get('PYTEST_XDIST_WORKER', 'main'))\n",
        encoding="utf-8",
    )
    sources = {
        "test_a.py": "@pytest.mark.xdist_group('shared')\ndef test_shared(): pass\n"
        "@pytest.mark.parametrize('value', [1, 2], ids=['id@native', 'second'])\n"
        "def test_plain(value): pass\n@pytest.mark.slow\ndef test_extended(): pass\n",
        "test_b.py": "def test_late_group(): pass\ndef test_other(): pass\n",
        "test_c.py": "@pytest.mark.xdist_group('a')\n@pytest.mark.xdist_group(name='b')\n"
        "def test_combined(): pass\n@pytest.mark.xdist_group()\ndef test_default(): pass\n",
        "test_d.py": "@pytest.mark.xdist_group('a_b')\ndef test_collision(): pass\n"
        "@pytest.mark.xdist_group(name='default')\ndef test_default_peer(): pass\n",
        "test_e.py": "@pytest.mark.xdist_group('prefix@tail')\ndef test_at(): pass\n"
        "@pytest.mark.xdist_group('group]')\ndef test_bracket(): pass\n",
        "test_f.py": "@pytest.mark.xdist_group('tail')\ndef test_at_peer(): pass\n"
        "@pytest.mark.xdist_group('group]')\ndef test_bracket_peer(): pass\n",
        "test_new.py": "def test_new_module(): pass\n",
    }
    for name, source in sources.items():
        (root / name).write_text("import pytest\n" + source, encoding="utf-8")


def _arguments(index):
    return ("-p", PLUGIN, "--g1-shard-index", str(index), "--g1-shard-count", "2")


def _collected(root, *args, environment=None):
    result = _run(root, "--collect-only", "-m", "not slow", *args, environment=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    return set(json.loads(next(line.removeprefix("SELECTED=")
                              for line in result.stdout.splitlines() if line.startswith("SELECTED="))))


@pytest.mark.process_contract
def test_actual_collection_preserves_coverage_groups_and_order_independence(tmp_path):
    _suite(tmp_path)
    whole = _collected(tmp_path)
    parts = [_collected(tmp_path, *_arguments(index)) for index in range(2)]
    assert len(whole) == 14
    assert whole == parts[0] | parts[1] and not parts[0] & parts[1]
    assert all("extended" not in node for node in whole)
    for group in (
        {"test_a.py::test_shared", "test_b.py::test_late_group"},
        {"test_c.py::test_combined", "test_d.py::test_collision"},
        {"test_c.py::test_default", "test_d.py::test_default_peer"},
        {"test_e.py::test_at", "test_f.py::test_at_peer"},
        {"test_a.py::test_plain[id@native]", "test_a.py::test_plain[second]"},
    ):
        assert sum(group <= part for part in parts) == 1
    environment = {**_environment(), "REVERSE_COLLECTION": "1"}
    assert [_collected(tmp_path, *_arguments(i), environment=environment) for i in range(2)] == parts


@pytest.mark.process_contract
def test_real_loadgroup_workers_execute_each_item_once_and_keep_effective_groups(tmp_path):
    _suite(tmp_path)
    actual = {}
    for index in range(2):
        expected = _collected(tmp_path, *_arguments(index))
        report = tmp_path / f"shard-{index}.xml"
        result = _run(tmp_path, *_arguments(index), "-p", "xdist.plugin", "-n", "2",
                      "--dist", "loadgroup", "-m", "not slow", f"--junitxml={report}")
        assert result.returncode == 0, result.stdout + result.stderr
        observed = {}
        for case in ET.parse(report).iter("testcase"):
            properties = {prop.get("name"): prop.get("value") for prop in case.iter("property")}
            native = properties["native"]
            assert native not in observed and native not in actual
            observed[native] = (index, properties["worker"])
        assert set(observed) == expected
        actual.update(observed)
    for left, right in (
        ("test_a.py::test_shared", "test_b.py::test_late_group"),
        ("test_c.py::test_combined", "test_d.py::test_collision"),
        ("test_c.py::test_default", "test_d.py::test_default_peer"),
        ("test_e.py::test_at", "test_f.py::test_at_peer"),
    ):
        assert actual[left] == actual[right]


@pytest.mark.process_contract
def test_nested_pytest_does_not_inherit_sharding(tmp_path):
    _suite(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (nested / "test_inner.py").write_text("def test_first(): pass\ndef test_second(): pass\n", encoding="utf-8")
    (tmp_path / "test_nested.py").write_text(
        "import subprocess, sys\ndef test_nested():\n"
        "    result = subprocess.run([sys.executable, '-m', 'pytest', '-c',\n"
        "        'nested/pytest.ini', '--noconftest', '--collect-only', '-q', 'nested'],\n"
        "        capture_output=True, text=True, timeout=20)\n"
        "    assert result.returncode == 0, result.stdout + result.stderr\n"
        "    assert '2 tests collected' in result.stdout\n", encoding="utf-8",
    )
    owner = next(i for i in range(2) if "test_nested.py::test_nested" in _collected(tmp_path, *_arguments(i)))
    result = _run(tmp_path, *_arguments(owner), "-m", "not slow")
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.process_contract
def test_partial_duplicate_invalid_and_empty_shard_options_fail(tmp_path):
    _suite(tmp_path)
    invalid = [(), ("--g1-shard-index", "0"), ("--g1-shard-count", "2"),
               ("--g1-shard-index", "oops", "--g1-shard-count", "2")]
    invalid += [("--g1-shard-index", str(i), "--g1-shard-count", str(c))
                for i, c in [(-1, 2), (2, 2), (0, 0), (0, 1), (0, 3)]]
    invalid += [(*_arguments(0)[2:], "--g1-shard-index", "1"),
                (*_arguments(0)[2:], "--g1-shard-count", "2")]
    for args in invalid:
        result = _run(tmp_path, "-p", PLUGIN, "--collect-only", *args)
        assert result.returncode != 0, (args, result.stdout, result.stderr)
    result = _run(tmp_path, *_arguments(0), "--collect-only", "test_new.py")
    assert result.returncode != 0 and "empty shard" in result.stderr


@pytest.mark.process_contract
def test_wrapper_pairs_arguments_names_artifacts_and_does_not_export_plugin(tmp_path):
    capture = tmp_path / "capture.json"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'w') as target:\n"
        "    json.dump({'args': sys.argv[1:], 'env': dict(os.environ)}, target)\n", encoding="utf-8",
    )
    fake_python.chmod(0o755)
    environment = {**_environment(), "ROOT_DIR": str(tmp_path), "PYTHON_BIN": str(fake_python),
                   "CHECK_TIMING_FILE": str(tmp_path / "time"), "CAPTURE": str(capture),
                   "MODEL_SERVICES_CONFIG": "ambient", "MINERU_MODE": "api"}
    command = ["bash", str(ROOT / "scripts/check_backend.sh")]
    for index in (None, 0, 1):
        args = [] if index is None else ["--shard-index", str(index), "--shard-count", "2"]
        result = subprocess.run(command + args, env=environment, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        observed = json.loads(capture.read_text(encoding="utf-8"))
        stem = "backend" if index is None else f"backend-shard{index}"
        assert f"--junitxml={tmp_path}/backend/.local/{stem}-junit.xml" in observed["args"]
        assert f"--ci-metrics={tmp_path}/backend/.local/{stem}-metrics.json" in observed["args"]
        assert (PLUGIN in observed["args"]) == (index is not None)
        assert observed["env"]["PYTEST_ADDOPTS"] == observed["env"]["PYTEST_PLUGINS"] == ""
        assert not any("SHARD" in key for key in observed["env"])
        assert observed["env"]["MODEL_SERVICES_CONFIG"] == ""
        assert observed["env"]["MINERU_MODE"] == "off"
    for args in (["--shard-index", "0"], ["--shard-count", "2"], ["--shard-index"],
                 ["--unknown"], ["--shard-index", "no", "--shard-count", "2"],
                 ["--shard-index", "2", "--shard-count", "2"],
                 ["--shard-index", "0", "--shard-count", "3"],
                 ["--shard-index", "0", "--shard-index", "1", "--shard-count", "2"],
                 ["--shard-index", "0", "--shard-count", "2", "--shard-count", "2"]):
        capture.unlink(missing_ok=True)
        result = subprocess.run(command + args, env=environment, text=True, capture_output=True, timeout=10)
        assert result.returncode == 2 and not capture.exists(), (args, result.stderr)
