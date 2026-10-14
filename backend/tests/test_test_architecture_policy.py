import json
import os
import sys
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
        "npm run test:node",
        "npm run test:component",
        "npm run build",
        "npm run lint",
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


def test_frontend_lane_recovers_the_test_file_diagnostics_next_drops():
    frontend_lane = (ROOT / "scripts" / "check_frontend.sh").read_text(
        encoding="utf-8"
    )
    next_config = (ROOT / "frontend" / "next.config.mjs").read_text(
        encoding="utf-8"
    )

    # `next build` stays the fail-closed typecheck for production code.
    assert "npm run build" in frontend_lane
    assert "ignoreBuildErrors" not in next_config

    # But Next's build-time type checker filters out every diagnostic in
    # `*.test.*`/`*.spec.*` files and `__tests__`/`__mocks__` directories
    # (the ignoreRegex in next/dist/lib/typescript/runTypeCheck.js), so the
    # lane must also run the package typecheck — and after the build, so it
    # sees the freshly regenerated `.next/types` instead of a stale tree.
    # Compare actual commands, not raw text: comments mention both spellings.
    commands = [
        line.strip()
        for line in frontend_lane.splitlines()
        if not line.lstrip().startswith("#")
    ]
    sync_command = "npm run sync:ui-plugins"
    build_command = (
        "run_step build env npm_config_ignore_scripts=true npm run build \\"
    )
    lint_command = (
        "&& run_step typecheck env npm_config_ignore_scripts=true npm run lint"
    )
    assert commands.count(sync_command) == 1
    # The typecheck is chained on the build's success, in the one function both
    # the sequential and the parallel path call.
    assert commands.index(lint_command) == commands.index(build_command) + 1
    assert commands.count(build_command) == 1
    assert commands.count("build_then_typecheck") == 1
    assert commands.count("build_then_typecheck &") == 1

    # The lane runs `npm run test` as its two halves, once per path; that is
    # only complete while the package's test script is exactly those halves.
    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    assert package["scripts"]["test"] == (
        "npm run test:node && npm run test:component"
    )
    for half in ("node-tests npm run test:node", "component-tests npm run test:component"):
        assert commands.count(f"run_step {half}") == 1
        assert commands.count(f"run_step {half} &") == 1


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

    This test itself costs ≈2–4s (dominated by one `--collect-only`
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

    tests_dir = ROOT / "backend" / "tests"
    # conftest.py's three constants above only ever name modules that live
    # directly in backend/tests/ — its own matching is `Path(fspath).name`
    # against these bare basenames (see pytest_collection_modifyitems) — so
    # resolving them against tests_dir is correct for this source.
    candidate_relpaths = {
        str((tests_dir / name).relative_to(ROOT)) for name in module_sets
    }

    decorator_pattern = re.compile(
        r"@pytest\.mark\.(?:slow|architecture_contract(?:_heavy)?|graph_index_contract)\b"
        r"|pytestmark\s*=\s*.*mark\.(?:slow|architecture_contract(?:_heavy)?|graph_index_contract)"
    )
    for path in tests_dir.rglob("*.py"):
        if "postgres" in path.relative_to(tests_dir).parts:
            continue
        if path.name == "conftest.py":
            continue
        if decorator_pattern.search(path.read_text(encoding="utf-8")):
            # Unlike the conftest-derived names above, this scan walks
            # subdirectories (e.g. backend/tests/kg/). Keep the *full*
            # relative path here — collapsing to `path.name` would silently
            # rewrite a decorated file below tests_dir into a same-named
            # sibling directly under tests_dir (or a nonexistent path when
            # no such sibling exists), dropping the real file from the
            # candidate set the collect-only subprocess below is given.
            candidate_relpaths.add(str(path.relative_to(ROOT)))

    candidate_paths = sorted(candidate_relpaths)
    assert candidate_paths, "expected at least one architecture_contract-bearing file"
    missing_candidates = [
        rel for rel in candidate_paths if not (ROOT / rel).is_file()
    ]
    assert not missing_candidates, (
        "candidate files resolved from conftest.py's marker-driving module "
        "names do not exist on disk (a name likely collided across "
        f"subdirectories of backend/tests/): {missing_candidates}"
    )

    def marker_expr(layer_index: int) -> str:
        path = EXTENDED_BACKEND_LAYERS[layer_index]
        source = (ROOT / path).read_text(encoding="utf-8")
        # Scan line by line and skip comments — a `-m "..."` example sitting
        # in a comment (e.g. the re-timing hint above the real invocation)
        # must not be picked up ahead of the actual pytest invocation.
        for line in source.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = re.search(r'-m\s+"([^"]+)"', line)
            if match:
                return match.group(1)
        raise AssertionError(f"no -m \"...\" expression found in {path}")

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
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"collect-only subprocess failed (exit {result.returncode}):\n"
        f"stdout (tail):\n{result.stdout[-4000:]}\n"
        f"stderr (tail):\n{result.stderr[-4000:]}"
    )
    assert START_SENTINEL in result.stdout and END_SENTINEL in result.stdout, (
        "lane_partition_plugin dump not found in collect-only output:\n"
        + result.stdout
    )
    dump = result.stdout.split(START_SENTINEL, 1)[1].split(END_SENTINEL, 1)[0]
    items = json.loads(dump)

    universe = {item["nodeid"] for item in items}
    assert universe, "candidate discovery found files but collected zero tests"

    all_markers: set[str] = set()
    for item in items:
        all_markers.update(item["markers"])
    for marker_name in ("slow", "architecture_contract_heavy", "graph_index_contract"):
        assert marker_name in all_markers, (
            f"no collected item carries @pytest.mark.{marker_name} — the "
            "partition assertions below would pass vacuously (every item "
            "trivially satisfies 'not <marker>') if this marker name were "
            "typo'd upstream or its only test dropped out of the candidate set"
        )

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
    assert 'BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-16}"' in backend_lane


def test_the_collected_heap_is_frozen_out_of_the_cyclic_collector():
    """backend/tests/conftest.py freezes what collection left alive.

    Without it every full collection in a worker re-walks the imported app and
    every collected test module, which the 2026-10 gate diagnosis measured as a
    large share of the backend lane's busy time.
    """
    import gc

    from tests import conftest

    assert gc.get_freeze_count() > 0
    assert conftest.pytest_collection_finish.__module__ == conftest.__name__


def test_session_start_drops_only_the_bytecode_cached_for_temporary_modules(tmp_path):
    """conftest prunes the prefix subtree that mirrors the temp directory.

    The gate's bytecode cache mirrors absolute source paths, so modules tests
    write under tmp_path pile up there run after run; the repository's and the
    interpreter's own bytecode must stay.
    """
    from tests import conftest

    repo = tmp_path / "repo"
    temp = tmp_path / "var" / "tmp"
    prefix = repo / ".local" / "pycache"

    def cached(source_dir: Path) -> Path:
        directory = prefix / source_dir.resolve().relative_to(source_dir.resolve().anchor)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "m.cpython-314.pyc").write_bytes(b"pyc")
        return directory

    temporary = cached(temp / "pytest-of-user" / "pytest-1" / "test_x0")
    own = cached(repo / "backend" / "app")
    interpreter = cached(tmp_path / "python" / "lib")

    # Not the repository's own cache, or the checkout under the temp dir: no-op.
    elsewhere = tmp_path / "elsewhere"
    foreign = elsewhere / temp.resolve().relative_to(temp.resolve().anchor)
    foreign.mkdir(parents=True)
    conftest._prune_temporary_bytecode(elsewhere, temp, repo)
    conftest._prune_temporary_bytecode(prefix, repo.parent, repo)
    assert foreign.exists()
    assert temporary.exists() and own.exists() and interpreter.exists()

    conftest._prune_temporary_bytecode(prefix, temp, repo)
    assert not (prefix / temp.resolve().relative_to(temp.resolve().anchor)).exists()
    assert own.exists() and interpreter.exists()


def test_only_the_top_level_session_prunes(monkeypatch):
    """Workers and pytest sessions that tests start in a subprocess inherit the
    marker, so they never prune the outer run's cache mid-run."""
    from types import SimpleNamespace

    from tests import conftest

    calls: list[tuple] = []
    monkeypatch.setattr(conftest, "_prune_temporary_bytecode", lambda *a: calls.append(a))
    monkeypatch.setattr(sys, "pycache_prefix", "/some/prefix")
    monkeypatch.delenv(conftest._BYTECODE_PRUNED_ENV, raising=False)

    conftest.pytest_configure(SimpleNamespace(workerinput={}))
    assert calls == []
    conftest.pytest_configure(SimpleNamespace())
    assert len(calls) == 1
    assert os.environ[conftest._BYTECODE_PRUNED_ENV] == "1"
    conftest.pytest_configure(SimpleNamespace())
    assert len(calls) == 1


def test_the_module_namespace_check_reports_app_splits_and_never_probes_other_modules(
    monkeypatch,
):
    """conftest's per-test ``sys.modules`` / package-attribute check.

    It must still report an ``app`` submodule whose ``sys.modules`` entry is
    gone while the parent package keeps the attribute (a bare
    ``sys.modules.pop``), and it must decide by name before touching a non-app
    module at all: probing every registered module was its per-test cost.
    """
    import types

    import app.services.scale_index_builder as module

    from tests import conftest

    class _Untouchable(types.ModuleType):
        def __getattribute__(self, name):
            raise AssertionError(f"probed a non-app module for {name!r}")

    monkeypatch.setitem(sys.modules, "not_app_probe", _Untouchable("not_app_probe"))
    assert conftest._split_app_module_namespace() == []

    monkeypatch.delitem(sys.modules, module.__name__)
    package = sys.modules["app.services"]
    assert conftest._split_app_module_namespace() == [
        (package, "scale_index_builder", module, None)
    ]


def test_child_environment_scan_flags_only_hand_built_environments_without_a_policy():
    from tests.architecture.child_environments import offenders

    source = (
        "import os, subprocess\n"
        "from tests.child_env_testkit import BYTECODE_ENV\n"
        "BASE = {k: v for k, v in os.environ.items()}\n"
        "def scrubbed():\n"
        "    return {'PATH': os.defpath}\n"
        "def scrubbed_call():\n"
        "    subprocess.run(['python'], env={'PYTHONPATH': 'backend'})\n"
        "def with_kit():\n"
        "    subprocess.run(['python'], env={'PATH': os.defpath, **BYTECODE_ENV})\n"
        "def with_key():\n"
        "    return {'PATH': '/bin', 'PYTHONDONTWRITEBYTECODE': '1'}\n"
        "def inherits():\n"
        "    subprocess.run(['python'], env={**os.environ, 'PATH': '/bin'})\n"
        "def inherits_by_name():\n"
        "    return {**BASE, 'PATH': '/bin'}\n"
        "def extends_a_checked_base(env):\n"
        "    subprocess.run(['python'], env={**env, 'X': '1'})\n"
        "def not_a_child(client):\n"
        "    client(env={'TOKEN': 't'})\n"
    )

    found, checked = offenders(source, "sample.py")

    assert found == [
        "sample.py: <module>.scrubbed",
        "sample.py: <module>.scrubbed_call",
    ]
    assert checked == 5


def test_hand_built_child_environments_keep_bytecode_out_of_the_source_tree():
    """Every scrubbed child environment under backend/tests carries BYTECODE_ENV.

    Without it the child drops the gate's PYTHONPYCACHEPREFIX and any Python it
    runs writes .pyc files next to the repository sources (three such tests
    wrote ~430 files into backend/app and scripts/ per gate run).
    """
    from tests.architecture.child_environments import offenders

    found: list[str] = []
    checked = 0
    tests_root = ROOT / "backend" / "tests"
    for path in sorted(tests_root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "subprocess" not in source and "PATH" not in source:
            continue
        file_found, file_checked = offenders(
            source, path.relative_to(ROOT).as_posix()
        )
        found += file_found
        checked += file_checked
    assert found == []
    # Non-vacuity: the known hand-built environments (model-bindings preflight,
    # the deployment-plugin check, the launcher fixtures, ...) are all seen.
    assert checked >= 8


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
    import ast

    from tests.conftest import _REAL_SQLITE_MIGRATION_MODULES

    # These service protocols only initialize an empty database in their
    # harness. Their store's upgrade cases remain real migration contracts.
    setup_only_modules = {
        "test_agent_profile_job_base.py",
        "test_agent_profile_job_overlay.py",
    }
    reviewed_setup_modules = set()
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
        if path.name in setup_only_modules:
            tree = ast.parse(source, filename=str(path))
            harness = next(
                node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "harness"
            )
            assert any(
                isinstance(node, ast.Attribute) and node.attr == "fixture"
                and isinstance(node.value, ast.Name) and node.value.id == "pytest"
                for node in harness.decorator_list
            ), path.name
            # A later migration test must not inherit the setup exception.
            references = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "SqliteMigrator"
            ]
            assert len(references) == 1 and references[0] in ast.walk(harness), path.name
            assert path.name not in _REAL_SQLITE_MIGRATION_MODULES, path.name
            reviewed_setup_modules.add(path.name)
            continue
        if path.name not in _REAL_SQLITE_MIGRATION_MODULES:
            missing.append(path.name)
    assert reviewed_setup_modules == setup_only_modules
    assert missing == []
