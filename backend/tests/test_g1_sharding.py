"""Exercise the shard boundary through hermetic, real pytest subprocesses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = "tests.architecture.g1_sharding"
pytestmark = pytest.mark.xdist_group("g1_sharding_contract")


def _environment() -> dict[str, str]:
    return {
        **os.environ,
        "PYTHONPATH": str(ROOT / "backend"),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
    }


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(root / "pytest.ini"),
         "-p", "no:cacheprovider", "-q", *args],
        cwd=root, env=_environment(), text=True, capture_output=True,
        timeout=30, check=False,
    )


def _suite(root: Path) -> None:
    (root / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    xdist_group: shared worker\n    slow: extended\n",
        encoding="utf-8",
    )
    (root / "conftest.py").write_text(
        "import json, os\nimport pytest\n"
        "def pytest_collection_modifyitems(items):\n"
        "    for item in items:\n"
        "        if item.name == 'test_late_group':\n"
        "            item.add_marker(pytest.mark.xdist_group(name='shared'))\n"
        "    if os.environ.get('REVERSE_COLLECTION'):\n"
        "        items.reverse()\n"
        "def pytest_collection_finish(session):\n"
        "    print('SELECTED=' + json.dumps([item.nodeid for item in session.items]))\n",
        encoding="utf-8",
    )
    (root / "test_a.py").write_text(
        "import pytest\n"
        "@pytest.mark.xdist_group('shared')\ndef test_shared(): pass\n"
        "@pytest.mark.parametrize('value', range(3))\ndef test_plain(value): pass\n"
        "@pytest.mark.slow\ndef test_extended(): pass\n",
        encoding="utf-8",
    )
    (root / "test_b.py").write_text(
        "def test_late_group(): pass\ndef test_other(): pass\n",
        encoding="utf-8",
    )
    (root / "test_c.py").write_text(
        "import pytest\n@pytest.mark.xdist_group('one')\n"
        "@pytest.mark.xdist_group(name='two')\ndef test_combined(): pass\n",
        encoding="utf-8",
    )
    (root / "test_d.py").write_text(
        "import pytest\n@pytest.mark.xdist_group('two')\n"
        "@pytest.mark.xdist_group(name='one')\ndef test_combined_peer(): pass\n",
        encoding="utf-8",
    )


def _collected(root: Path, *args: str) -> set[str]:
    result = _run(root, "--collect-only", "-m", "not slow", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    return set(json.loads(next(
        line.removeprefix("SELECTED=")
        for line in result.stdout.splitlines() if line.startswith("SELECTED=")
    )))


def _shard(index: int, count: int = 2) -> tuple[str, ...]:
    return ("-p", PLUGIN, "--g1-shard-index", str(index), "--g1-shard-count", str(count))


def test_shards_are_a_complete_disjoint_order_independent_partition(tmp_path, monkeypatch):
    _suite(tmp_path)
    whole = _collected(tmp_path)
    shards = [_collected(tmp_path, *_shard(index)) for index in range(2)]
    assert whole == shards[0] | shards[1]
    assert not shards[0] & shards[1]
    assert len(whole) == 8
    assert all("extended" not in node for node in whole)
    for group in (
        {"test_a.py::test_shared", "test_b.py::test_late_group"},
        {"test_c.py::test_combined", "test_d.py::test_combined_peer"},
        {f"test_a.py::test_plain[{index}]" for index in range(3)},
    ):
        assert sum(group <= shard for shard in shards) == 1
    monkeypatch.setenv("REVERSE_COLLECTION", "1")
    assert [_collected(tmp_path, *_shard(index)) for index in range(2)] == shards


def test_shard_options_reach_real_xdist_workers(tmp_path):
    _suite(tmp_path)
    expected = _collected(tmp_path, *_shard(0))
    report = tmp_path / "result.xml"
    result = _run(
        tmp_path, *_shard(0), "-p", "xdist.plugin", "-n", "2", "--dist", "loadgroup",
        "-m", "not slow", f"--junitxml={report}",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    actual = {
        f"{case.attrib['classname']}.py::{case.attrib['name']}"
        for case in ET.parse(report).iter("testcase")
    }
    assert actual == expected


def test_nested_pytest_keeps_its_complete_collection(tmp_path):
    _suite(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (nested / "test_inner.py").write_text(
        "def test_first(): pass\ndef test_second(): pass\n", encoding="utf-8",
    )
    outer = tmp_path / "test_outer.py"
    outer.write_text(
        "import subprocess, sys\n"
        "def test_nested_collection():\n"
        "    result = subprocess.run([sys.executable, '-m', 'pytest', '-c',\n"
        "        'nested/pytest.ini', '--noconftest', '--collect-only', '-q', 'nested'],\n"
        "        capture_output=True, text=True, timeout=15)\n"
        "    assert result.returncode == 0, result.stdout + result.stderr\n"
        "    assert '2 tests collected' in result.stdout\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, *_shard(0, 1), str(outer))
    assert result.returncode == 0, result.stdout + result.stderr


def test_invalid_partial_and_empty_shards_fail_closed(tmp_path):
    _suite(tmp_path)
    invalid = (
        ("-p", PLUGIN),
        ("-p", PLUGIN, "--g1-shard-index", "0"),
        ("-p", PLUGIN, "--g1-shard-count", "2"),
        _shard(-1), _shard(2), _shard(0, 0), _shard(0, 99),
    )
    for args in invalid:
        result = _run(tmp_path, "--collect-only", *args)
        assert result.returncode != 0, (args, result.stdout)
        assert "G1" in result.stderr, (args, result.stdout, result.stderr)


def test_backend_wrapper_requires_explicit_paired_arguments_and_isolates_environment(tmp_path):
    capture = tmp_path / "capture.json"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        "with open(os.environ['CAPTURE'], 'w') as target:\n"
        "    json.dump({'args': sys.argv[1:], 'env': {key: os.environ.get(key)\n"
        "        for key in ['SILICON_NOTEBOOK_ENV_FILE', 'MODEL_SERVICES_CONFIG',\n"
        "        'EXTENSIONS_CONFIG', 'MINERU_MODE', 'MINERU_API_TOKEN']}}, target)\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    env = {
        **os.environ, "ROOT_DIR": str(tmp_path), "PYTHON_BIN": str(fake_python),
        "CHECK_TIMING_FILE": str(tmp_path / "wrapper.time"),
        "CAPTURE": str(capture), "SILICON_NOTEBOOK_ENV_FILE": "/ambient/config",
        "MODEL_SERVICES_CONFIG": "ambient", "EXTENSIONS_CONFIG": "ambient",
        "MINERU_MODE": "api", "MINERU_API_TOKEN": "not-a-real-token",
    }
    command = ["bash", str(ROOT / "scripts/check_backend.sh")]
    for args in ([], ["--shard-index", "0", "--shard-count", "2"]):
        result = subprocess.run(command + args, env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode == 0, result.stderr
        observed = json.loads(capture.read_text(encoding="utf-8"))
        assert observed["env"] == {
            "SILICON_NOTEBOOK_ENV_FILE": "", "MODEL_SERVICES_CONFIG": "",
            "EXTENSIONS_CONFIG": "", "MINERU_MODE": "off", "MINERU_API_TOKEN": "",
        }
        assert (PLUGIN in observed["args"]) == bool(args)
        report = "backend-junit-shard-0.xml" if args else "backend-junit.xml"
        assert f"--junitxml={tmp_path}/backend/.local/{report}" in observed["args"]
    for args in (
        ["--shard-index", "0"], ["--shard-count", "2"], ["--shard-index", "oops"],
        ["--shard-index"], ["--other"],
        ["--shard-index", "0", "--shard-index", "1", "--shard-count", "2"],
    ):
        capture.unlink(missing_ok=True)
        result = subprocess.run(command + args, env=env, text=True, capture_output=True, timeout=10)
        assert result.returncode != 0, args
        assert not capture.exists()
