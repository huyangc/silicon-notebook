import os
from pathlib import Path

import pytest

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
        "npm run build",
    ),
}

EXTENDED_BACKEND_LAYERS = (
    "scripts/check_backend.sh",
    "scripts/check_backend_extended.sh",
)


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


@pytest.mark.architecture_contract
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


def test_frontend_production_build_owns_the_standard_gate_typecheck():
    frontend_lane = (ROOT / "scripts" / "check_frontend.sh").read_text(
        encoding="utf-8"
    )
    next_config = (ROOT / "frontend" / "next.config.mjs").read_text(
        encoding="utf-8"
    )

    assert "npm run build" in frontend_lane
    assert "npm run lint" not in frontend_lane
    assert "ignoreBuildErrors" not in next_config


def test_openapi_framework_versions_are_exactly_pinned():
    requirements = {
        line.split("#", 1)[0].strip()
        for line in (ROOT / "backend" / "requirements.txt").read_text(
            encoding="utf-8"
        ).splitlines()
        if line.split("#", 1)[0].strip()
    }

    assert "fastapi==0.135.3" in requirements
    assert "pydantic==2.12.4" in requirements


def test_verification_lanes_do_not_hide_committed_tests():
    forbidden = ("pytest.mark.skip", "xfail")
    for relative in REQUIRED_LAYERS:
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert not any(value in source for value in forbidden), relative

    standard = (ROOT / EXTENDED_BACKEND_LAYERS[0]).read_text(encoding="utf-8")
    extended = (ROOT / EXTENDED_BACKEND_LAYERS[1]).read_text(encoding="utf-8")
    assert (
        '-m "not slow and not architecture_contract and not graph_index_contract"'
        in standard
    )
    assert (
        '-m "slow or architecture_contract or graph_index_contract"' in extended
    )
    assert "backend/tests/postgres" in standard
    assert "backend/tests/postgres" in extended
    assert "check_backend_extended.sh" in (
        ROOT / "scripts/check_extended.sh"
    ).read_text(encoding="utf-8")


def test_backend_parallelism_is_bounded_and_explicit():
    config = (ROOT / "backend" / "pytest.ini").read_text(encoding="utf-8")
    assert "addopts = -n 12 --dist loadgroup" in config
    assert "architecture_contract:" in config
    assert "graph_index_contract:" in config
    assert "-n auto" not in config
    assert "worksteal" not in config
    backend_lane = (ROOT / "scripts" / "check_backend.sh").read_text(encoding="utf-8")
    assert 'BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-12}"' in backend_lane


def test_parallel_graph_tests_share_repo_local_matplotlib_cache():
    cache_dir = Path(os.environ["MPLCONFIGDIR"]).resolve()
    assert cache_dir == (ROOT / ".local" / "matplotlib").resolve()
    assert tuple(cache_dir.glob("fontlist-v*.json"))


def test_reused_sqlite_schema_keeps_each_test_database_mutably_isolated(tmp_path):
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.repositories.sqlite.migrations import SCHEMA_VERSION
    from app.services.sqlite_repository import SQLiteRepository

    first = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'first.db'}",
            storage_dir=str(tmp_path / "first-storage"),
        )
    )
    second = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'second.db'}",
            storage_dir=str(tmp_path / "second-storage"),
        )
    )
    created = first.create_notebook(NotebookCreate(name="template isolation"))

    with first._connect() as first_db, second._connect() as second_db:
        assert first_db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert second_db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert second_db.execute(
            "SELECT 1 FROM notebooks WHERE id=?", (created.id,)
        ).fetchone() is None
    first.close_local()
    second.close_local()


def test_direct_sqlite_migrator_contracts_bypass_the_schema_template():
    conftest_source = (ROOT / "backend" / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )
    missing = []
    for path in sorted((ROOT / "backend" / "tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        if "SqliteMigrator(" not in source or ".migrate(" not in source:
            continue
        if f'"{path.name}"' not in conftest_source:
            missing.append(path.name)
    assert missing == []
