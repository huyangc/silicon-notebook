from pathlib import Path

from tests.architecture.policy import policy_offenders


ROOT = Path(__file__).resolve().parents[2]


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
