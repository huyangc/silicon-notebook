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
        '-m "not slow and not architecture_contract_heavy and not graph_index_contract"'
        in standard
    )
    assert (
        '-m "slow or architecture_contract_heavy or graph_index_contract"'
        in extended
    )
    assert "backend/tests/postgres" in standard
    assert "backend/tests/postgres" in extended
    assert "check_backend_extended.sh" in (
        ROOT / "scripts/check_extended.sh"
    ).read_text(encoding="utf-8")


def test_verification_lane_markers_partition_every_architecture_contract_test():
    """Empirically prove the G1/G2 backend marker split has no gap or overlap.

    The pinned literal strings above catch textual drift between the two `-m`
    expressions, but they cannot catch a *consistent* mistake: someone edits
    both `check_backend.sh` and this pinned string together (e.g. copies the
    NOT/AND form into check_backend_extended.sh instead of writing the dual
    OR form), which would keep both hardcoded checks green while some
    architecture_contract_heavy test silently ran in neither lane. Only
    running the two real `-m` expressions through pytest's own collector,
    against the real test files, proves the actual split.

    Candidate files are discovered dynamically (conftest.py's marker-driving
    module sets, plus a source-text scan for the marker decorators) rather
    than hardcoded, so this stays valid as tests gain or lose these markers.

    This test itself costs ~5s (dominated by one `--collect-only`
    subprocess) and is a deliberate exception to the G1/G2 split it proves:
    it stays in G1 on every PR because it *is* the guard that makes the
    56/8 `architecture_contract`/`architecture_contract_heavy` split
    trustworthy, not one of the things being split by it. Re-timing that
    split with `pytest -m architecture_contract --durations=0` will not
    select this test (it carries no `architecture_contract` marker of its
    own), so its cost is intentionally outside that accounting.

    Collection now runs once, not once per `-m` expression: a single
    `--collect-only` subprocess loads `tests.architecture.lane_partition_plugin`,
    which dumps every collected item's nodeid and own marker names as JSON
    (unfiltered — no `-m` passed to the subprocess). The two real `-m`
    expressions are then evaluated in-process, per item, against that
    marker set using pytest's own `_pytest.mark.expression` compiler — the
    same engine `-m` itself uses (see `_pytest.mark.deselect_by_mark`) — so
    this stays an empirical collection proof rather than a hand-rolled
    re-implementation of marker matching.
    """
    import ast
    import json
    import re
    import subprocess
    import sys

    from _pytest.mark.expression import Expression

    from tests.architecture.lane_partition_plugin import END_SENTINEL, START_SENTINEL

    conftest_source = (ROOT / "backend/tests/conftest.py").read_text(
        encoding="utf-8"
    )
    module_sets: set[str] = set()
    for constant in (
        "_ARCHITECTURE_CONTRACT_MODULES",
        "_GRAPH_INDEX_CONTRACT_MODULES",
    ):
        match = re.search(
            rf"{constant}\s*=\s*(\{{.*?\}})", conftest_source, re.DOTALL
        )
        assert match, f"conftest.py must still define {constant}"
        module_sets |= ast.literal_eval(match.group(1))
    heavy_match = re.search(
        r"_ARCHITECTURE_CONTRACT_HEAVY_TESTS\s*=\s*(\{.*?\})",
        conftest_source,
        re.DOTALL,
    )
    assert heavy_match, "conftest.py must still define _ARCHITECTURE_CONTRACT_HEAVY_TESTS"
    heavy_tests = ast.literal_eval(heavy_match.group(1))
    module_sets |= {file_name for file_name, _ in heavy_tests}

    decorator_pattern = re.compile(
        r"@pytest\.mark\.(?:slow|architecture_contract(?:_heavy)?|graph_index_contract)\b"
    )
    tests_dir = ROOT / "backend" / "tests"
    for path in tests_dir.rglob("*.py"):
        if "postgres" in path.relative_to(tests_dir).parts:
            continue
        if path.name == "conftest.py":
            continue
        if decorator_pattern.search(path.read_text(encoding="utf-8")):
            module_sets.add(path.name)

    candidate_paths = sorted(
        str((tests_dir / name).relative_to(ROOT)) for name in module_sets
    )
    assert candidate_paths, "expected at least one architecture_contract-bearing file"

    def marker_expr(layer_index: int) -> str:
        source = (ROOT / EXTENDED_BACKEND_LAYERS[layer_index]).read_text(
            encoding="utf-8"
        )
        match = re.search(r'-m\s+"([^"]+)"', source)
        assert match, EXTENDED_BACKEND_LAYERS[layer_index]
        return match.group(1)

    args = [
        sys.executable,
        "-m",
        "pytest",
        *candidate_paths,
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "tests.architecture.lane_partition_plugin",
    ]
    result = subprocess.run(
        args,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert START_SENTINEL in result.stdout and END_SENTINEL in result.stdout, (
        "lane_partition_plugin dump not found in collect-only output:\n"
        + result.stdout
    )
    dump = result.stdout.split(START_SENTINEL, 1)[1].split(END_SENTINEL, 1)[0]
    items = json.loads(dump)

    universe = {item["nodeid"] for item in items}
    assert universe, "candidate discovery found files but collected zero tests"

    def select(expression_text: str) -> set[str]:
        compiled = Expression.compile(expression_text)
        selected: set[str] = set()
        for item in items:
            markers = set(item["markers"])
            if compiled.evaluate(lambda name, **_: name in markers):
                selected.add(item["nodeid"])
        return selected

    g1_ids = select(marker_expr(0))
    g2_ids = select(marker_expr(1))

    assert g1_ids | g2_ids == universe
    assert g1_ids & g2_ids == set()


def test_backend_parallelism_is_bounded_and_explicit():
    config = (ROOT / "backend" / "pytest.ini").read_text(encoding="utf-8")
    assert "addopts = -n 12 --dist loadgroup" in config
    assert "architecture_contract:" in config
    assert "architecture_contract_heavy:" in config
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


def test_sqlite_migration_contracts_bypass_the_schema_template():
    conftest_source = (ROOT / "backend" / "tests" / "conftest.py").read_text(
        encoding="utf-8"
    )
    missing = []
    for path in sorted((ROOT / "backend" / "tests").glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        directly_owns_migrator = (
            "SqliteMigrator(" in source and ".migrate(" in source
        )
        indirectly_owns_startup_migration = (
            "def test_run_startup_migrates_warms_and_flips_ready(" in source
            and "startup_warmup.run_startup(" in source
        )
        if not directly_owns_migrator and not indirectly_owns_startup_migration:
            continue
        if f'"{path.name}"' not in conftest_source:
            missing.append(path.name)
    assert missing == []
