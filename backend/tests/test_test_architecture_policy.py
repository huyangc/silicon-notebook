import os
from pathlib import Path

from tests.architecture.policy import policy_offenders


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_LAYERS = {
    "scripts/check_backend.sh": ("backend/tests",),
    "scripts/check_contracts.sh": (
        "smoke_backend.py",
        "smoke_memory_mcp.py",
        "check_ask_modes_contract.py",
        "check_object_type_labels_contract.py",
        "check_ui_vocabulary.py",
        "fangan/testcases/harness/tests",
    ),
    "scripts/check_frontend.sh": (
        "npm run test",
        "npm run lint",
        "npm run build",
    ),
}


def _write(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "test_contract.py"
    path.write_text(source, encoding="utf-8")
    return path


def test_policy_detects_historical_task_allowlist_and_path_line_identity(tmp_path):
    path = _write(
        tmp_path,
        "TASK7_ALLOWED_CALLS = {'backend/app/a.py:17'}\n",
    )

    assert policy_offenders((path,)) == [
        "test_contract.py:1: historical-task-allowlist",
        "test_contract.py:1: path-line-identity",
    ]


def test_policy_detects_lineno_in_expected_identity(tmp_path):
    path = _write(
        tmp_path,
        "def collect(node):\n"
        "    expected = {(node.lineno, 'save')}\n"
        "    assert actual == expected\n",
    )

    assert policy_offenders((path,)) == [
        "test_contract.py:2: line-number-identity",
    ]


def test_policy_detects_lineno_added_to_manifest(tmp_path):
    path = _write(
        tmp_path,
        "def collect(node, rel, sites):\n"
        "    sites.add((rel, node.lineno, 'save'))\n",
    )

    assert policy_offenders((path,)) == [
        "test_contract.py:2: line-number-identity",
    ]


def test_policy_allows_lineno_only_as_diagnostic_metadata(tmp_path):
    path = _write(
        tmp_path,
        "def collect(node, rel):\n"
        "    finding = SemanticFinding(\n"
        "        key=key,\n"
        "        count=1,\n"
        "        diagnostic_lines=(node.lineno,),\n"
        "    )\n"
        "    assert ok, f'{rel}:{node.lineno}: forbidden import'\n",
    )

    assert policy_offenders((path,)) == []


def test_policy_allows_lineno_in_named_diagnostic_map(tmp_path):
    path = _write(
        tmp_path,
        "def collect(node, key, diagnostic_lines_by_key):\n"
        "    diagnostic_lines_by_key[key].append(node.lineno)\n",
    )

    assert policy_offenders((path,)) == []


def test_policy_detects_skip_and_xfail_markers(tmp_path):
    path = _write(
        tmp_path,
        "@pytest.mark.skip\n"
        "def test_one(): pass\n"
        "@pytest.mark.xfail(strict=True)\n"
        "def test_two(): pass\n",
    )

    assert policy_offenders((path,)) == [
        "test_contract.py:1: pytest-skip",
        "test_contract.py:3: pytest-xfail",
    ]


def test_repository_contracts_have_no_source_position_identity_or_markers():
    policy_implementation = {
        ROOT / "backend" / "tests" / "architecture" / "policy.py",
        ROOT / "backend" / "tests" / "test_test_architecture_policy.py",
    }
    paths = tuple(
        path
        for path in sorted((ROOT / "backend" / "tests").rglob("*.py"))
        if path not in policy_implementation
    ) + (
        ROOT / "scripts" / "generate_repository_contract_fixtures.py",
        ROOT / "backend" / "app" / "repositories" / "ownership_manifest.py",
    )

    assert policy_offenders(paths) == []


def test_complete_gate_delegates_every_required_verification_layer():
    coordinator = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    for relative, responsibilities in REQUIRED_LAYERS.items():
        lane = ROOT / relative
        assert lane.exists(), relative
        source = lane.read_text(encoding="utf-8")
        for responsibility in responsibilities:
            assert responsibility in source, f"{relative}: {responsibility}"

    for lane_name in ("backend", "contracts", "frontend"):
        assert f"check_${{lane}}.sh" in coordinator or (
            f"check_{lane_name}.sh" in coordinator
        )


def test_verification_lanes_do_not_disable_committed_tests():
    forbidden = ('-m "not slow"', "--ignore", "pytest.mark.skip", "xfail")
    for relative in REQUIRED_LAYERS:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert not any(value in source for value in forbidden), relative


def test_backend_parallelism_is_bounded_and_explicit():
    config = (ROOT / "backend" / "pytest.ini").read_text(encoding="utf-8")
    assert "addopts = -n 12 --dist loadgroup" in config
    assert "architecture_contract:" in config
    assert "graph_index_contract:" in config
    assert "-n auto" not in config
    assert "worksteal" not in config
    backend_lane = (ROOT / "scripts" / "check_backend.sh").read_text(encoding="utf-8")
    assert 'BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-9}"' in backend_lane


def test_parallel_graph_tests_share_repo_local_matplotlib_cache():
    cache_dir = Path(os.environ["MPLCONFIGDIR"]).resolve()
    assert cache_dir == (ROOT / ".local" / "matplotlib").resolve()
    assert tuple(cache_dir.glob("fontlist-v*.json"))
