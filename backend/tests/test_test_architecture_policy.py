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
        "check_backend_lint.py",
        "audit_component_callers.py\" --check",
        "fangan/testcases/harness/tests",
    ),
    "scripts/check_frontend.sh": (
        "npm run test:node",
        "npm run test:component",
        "npm run build",
        "npm run typecheck",
        "npm run lint",
    ),
}

EXTENDED_BACKEND_LAYERS = (
    "scripts/check_backend.sh",
    "scripts/check_backend_extended.sh",
)
# Every marker that moves a test from G1 to the G2 complement.
G2_LANE_MARKERS = (
    "slow",
    "architecture_contract_heavy",
    "graph_index_contract",
    "process_contract",
    "legacy_upgrade_contract",
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
        assert "check_${lane}.sh" in coordinator or (
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
    typecheck_command = (
        "&& run_step typecheck env npm_config_ignore_scripts=true npm run typecheck"
    )
    assert commands.count(sync_command) == 1
    # The typecheck is chained on the build's success, in the one function both
    # the sequential and the parallel path call.
    assert commands.index(typecheck_command) == commands.index(build_command) + 1
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
    # ESLint runs once per path, and the package scripts keep the two passes
    # apart: `typecheck` is tsc, `lint` is ESLint against its baseline.
    assert commands.count("run_step lint npm run lint") == 1
    assert commands.count("run_step lint npm run lint &") == 1
    assert package["scripts"]["typecheck"] == "tsc --noEmit"
    assert package["scripts"]["lint"] == "eslint --max-warnings 0 ."
    # Next's own build-time lint would run without the baseline.
    assert "ignoreDuringBuilds: true" in next_config


def test_openapi_framework_versions_are_exactly_pinned():
    requirements = {
        line.split("#", 1)[0].strip()
        for line in (ROOT / "backend" / "requirements.in").read_text(
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
        '-m "not slow and not architecture_contract_heavy and not graph_index_contract'
        ' and not process_contract and not legacy_upgrade_contract"'
        in standard
    )
    assert (
        '-m "slow or architecture_contract_heavy or graph_index_contract'
        ' or process_contract or legacy_upgrade_contract"'
        in extended
    )
    assert "backend/tests/postgres" in standard
    assert "backend/tests/postgres" in extended
    assert "check_backend_extended.sh" in (
        ROOT / "scripts/check_extended.sh"
    ).read_text(encoding="utf-8")


# conftest collects this test first (`_DISPATCHED_FIRST`) so xdist dispatches
# it in the first wave rather than at the end of the run, where its ~6 s
# collect-only subprocess would become the gate's tail. It changes no lane
# selection; `test_the_lane_partition_guard_is_collected_first` pins it.
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
    own), so its cost is intentionally outside that accounting. For the same
    reason it carries no `process_contract` marker although it starts a real
    child process: a G2-only copy of this guard would stop running the moment
    a broken expression deselected it, so the guard stays in G1.

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

    lane_markers = (
        r"(?:slow|architecture_contract(?:_heavy)?|graph_index_contract"
        r"|process_contract|legacy_upgrade_contract)"
    )
    decorator_pattern = re.compile(
        rf"@pytest\.mark\.{lane_markers}\b"
        rf"|pytestmark\s*=\s*.*mark\.{lane_markers}"
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
    for marker_name in G2_LANE_MARKERS:
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


def test_the_process_scan_finds_every_way_a_g1_test_starts_a_process():
    from tests.architecture.process_tests import g1_process_tests

    source = (
        "import asyncio, os, subprocess, pytest\n"
        "import multiprocessing as mp\n"
        "import concurrent.futures\n"
        "from subprocess import Popen as P\n"
        "from concurrent.futures import ProcessPoolExecutor\n"
        "def _helper():\n"
        "    return subprocess.run(['true'])\n"
        "@pytest.fixture\n"
        "def child():\n"
        "    return P(['true'])\n"
        "def test_run():\n"
        "    subprocess.check_output(['true'])\n"
        "def test_imported_popen():\n"
        "    P(['true'])\n"
        "def test_through_a_helper():\n"
        "    _helper()\n"
        "def test_through_a_fixture(child):\n"
        "    child.wait()\n"
        "def test_multiprocessing():\n"
        "    mp.get_context('spawn')\n"
        "def test_pool():\n"
        "    ProcessPoolExecutor(2)\n"
        "def test_pool_by_module():\n"
        "    concurrent.futures.ProcessPoolExecutor(2)\n"
        "def test_fork():\n"
        "    os.fork()\n"
        "async def test_asyncio():\n"
        "    await asyncio.create_subprocess_exec('true')\n"
        "def test_a_completed_process_is_not_a_process():\n"
        "    subprocess.CompletedProcess(['true'], 0)\n"
        "def test_threads_are_not_processes():\n"
        "    import threading\n"
        "    threading.Thread(target=print).start()\n"
        "@pytest.mark.process_contract\n"
        "def test_marked():\n"
        "    subprocess.run(['true'])\n"
        "@pytest.mark.slow\n"
        "def test_in_g2_for_another_reason():\n"
        "    subprocess.run(['true'])\n"
        "def test_moved_by_conftest():\n"
        "    subprocess.run(['true'])\n"
        "@pytest.mark.process_contract\n"
        "class TestMarked:\n"
        "    def test_in_a_marked_class(self):\n"
        "        subprocess.run(['true'])\n"
        "class TestPlain:\n"
        "    def test_in_a_class(self):\n"
        "        _helper()\n"
    )

    assert g1_process_tests(
        source, "test_x.py", g2_markers=G2_LANE_MARKERS,
        g2_tests={"test_moved_by_conftest"},
    ) == [
        "test_run", "test_imported_popen", "test_through_a_helper",
        "test_through_a_fixture", "test_multiprocessing", "test_pool",
        "test_pool_by_module", "test_fork", "test_asyncio", "TestPlain.test_in_a_class",
    ]
    for module_mark in (
        "pytestmark = pytest.mark.process_contract\n",
        "pytestmark: list = [pytest.mark.process_contract]\n",
    ):
        marked = source.replace("def _helper():\n", module_mark + "def _helper():\n", 1)
        assert g1_process_tests(marked, "test_x.py", g2_markers=G2_LANE_MARKERS) == []


def test_g1_tests_that_start_a_process_carry_process_contract():
    """docs/development.md: a test that starts a real child process carries
    `process_contract`, so it runs in G2 only. The lane-partition guard is the
    one reviewed exception (see its docstring)."""
    from tests.architecture.process_tests import g1_process_tests
    from tests.conftest import _ARCHITECTURE_CONTRACT_HEAVY_TESTS, _GRAPH_INDEX_CONTRACT_MODULES

    allowed = {
        "backend/tests/test_test_architecture_policy.py::"
        "test_verification_lane_markers_partition_every_architecture_contract_test",
    }
    tests_root = ROOT / "backend" / "tests"
    found: set[str] = set()
    for path in sorted(tests_root.rglob("test_*.py")):
        if "postgres" in path.relative_to(tests_root).parts:
            continue
        if path.name in _GRAPH_INDEX_CONTRACT_MODULES:
            continue
        source = path.read_text(encoding="utf-8")
        # Every form the scan reports names one of these words; skipping the
        # other files keeps this from parsing the whole test tree.
        if not any(word in source for word in (
            "subprocess", "multiprocessing", "ProcessPoolExecutor", "fork", "spawn",
            "Popen",
        )):
            continue
        heavy = {name for file, name in _ARCHITECTURE_CONTRACT_HEAVY_TESTS if file == path.name}
        relative = path.relative_to(ROOT).as_posix()
        found |= {
            f"{relative}::{name}"
            for name in g1_process_tests(
                source, relative,
                g2_markers=G2_LANE_MARKERS, g2_tests=heavy,
            )
        }
    assert found == allowed, (
        "these G1 tests start a real child process; mark them "
        "@pytest.mark.process_contract (G2 only)"
    )


def test_the_lane_partition_guard_is_collected_first(request):
    from types import SimpleNamespace

    from tests.conftest import _DISPATCHED_FIRST, _collect_first

    file_name, test_name = _DISPATCHED_FIRST
    assert file_name == Path(__file__).name
    assert callable(globals()[test_name])
    # A group of its own would not help: xdist's stable sort by group size
    # ties a one-test group with every ungrouped test.
    assert not [
        mark for mark in getattr(globals()[test_name], "pytestmark", [])
        if mark.name == "xdist_group"
    ]

    def item(file, name):
        return SimpleNamespace(fspath=f"/tests/{file}", name=name)

    items = [item("test_a.py", "test_one"), item("test_b.py", "test_two"),
             item(*_DISPATCHED_FIRST), item("test_c.py", "test_three")]
    _collect_first(items, _DISPATCHED_FIRST)
    assert [(Path(i.fspath).name, i.name) for i in items] == [
        _DISPATCHED_FIRST, ("test_a.py", "test_one"), ("test_b.py", "test_two"),
        ("test_c.py", "test_three"),
    ]
    absent = items[1:]
    _collect_first(absent, _DISPATCHED_FIRST)
    assert [i.name for i in absent] == ["test_one", "test_two", "test_three"]

    # This session's own collection, whenever it holds the guard (a whole-file
    # or whole-suite run; a node-id run of this test alone does not).
    collected = [(Path(str(i.fspath)).name, i.name) for i in request.session.items]
    if _DISPATCHED_FIRST in collected:
        assert collected[0] == _DISPATCHED_FIRST


def test_backend_parallelism_is_bounded_and_explicit():
    config = (ROOT / "backend" / "pytest.ini").read_text(encoding="utf-8")
    assert "addopts = -n 12 --dist loadgroup" in config
    assert "architecture_contract:" in config
    assert "architecture_contract_heavy:" in config
    for marker_name in (*G2_LANE_MARKERS, "real_sqlite_migration"):
        assert f"\n    {marker_name}:" in config, marker_name
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


def _marks_real_sqlite_migration(decorators) -> bool:
    import ast

    return any(
        isinstance(node, ast.Attribute) and node.attr == "real_sqlite_migration"
        for decorator in decorators
        for node in ast.walk(decorator)
    )


def _tests_that_drive_the_sqlite_migrator(source: str, filename: str) -> dict[str, bool]:
    """Map each test whose own body drives ``SqliteMigrator`` to "is it marked".

    A function drives the migrator when its body names ``SqliteMigrator`` (as
    a bare name, through an ``import ... as`` alias, or as an attribute such
    as ``migrations.SqliteMigrator``) or calls, by name, a module-level helper
    that does (transitively); test methods of a class follow the same helpers.
    Fixtures (``@fixture``/``@pytest.fixture``, bare or called) are setup: a
    fixture that only creates an empty database at the current schema gets
    the template copy, which the real ladder built. A module ``pytestmark``
    (plain or annotated assignment), a class ``pytestmark`` and a class
    decorator count as marking every test under them.
    """
    import ast

    tree = ast.parse(source, filename=filename)
    aliases = {"SqliteMigrator"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "SqliteMigrator" and alias.asname
    }

    def is_fixture(node) -> bool:
        def plain(expr) -> bool:
            return (isinstance(expr, ast.Name) and expr.id == "fixture") or (
                isinstance(expr, ast.Attribute)
                and expr.attr == "fixture"
                and isinstance(expr.value, ast.Name)
                and expr.value.id == "pytest"
            )

        return any(
            plain(decorator)
            or (isinstance(decorator, ast.Call) and plain(decorator.func))
            for decorator in node.decorator_list
        )

    def module_marked(body) -> bool:
        for node in body:
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            if any(getattr(target, "id", "") == "pytestmark" for target in targets) and (
                _marks_real_sqlite_migration([value])
            ):
                return True
        return False

    def names_the_migrator(node) -> bool:
        return any(
            (isinstance(sub, ast.Name) and sub.id in aliases)
            or (isinstance(sub, ast.Attribute) and sub.attr == "SqliteMigrator")
            for sub in ast.walk(node)
        )

    def calls(node) -> set[str]:
        return {
            sub.func.id for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
        }

    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    helpers = {name for name, node in functions.items() if not is_fixture(node)}
    drivers = {name for name in helpers if names_the_migrator(functions[name])}
    changed = True
    while changed:
        changed = False
        for name in helpers - drivers:
            if calls(functions[name]) & drivers:
                drivers.add(name)
                changed = True

    def drives(node) -> bool:
        return names_the_migrator(node) or bool(calls(node) & drivers)

    whole_module = module_marked(tree.body)
    result: dict[str, bool] = {}
    for name in sorted(drivers):
        if name.startswith("test_"):
            result[name] = whole_module or _marks_real_sqlite_migration(
                functions[name].decorator_list
            )
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        class_marked = whole_module or module_marked(cls.body) or (
            _marks_real_sqlite_migration(cls.decorator_list)
        )
        for node in cls.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
                and not is_fixture(node)
                and drives(node)
            ):
                result[f"{cls.name}.{node.name}"] = class_marked or (
                    _marks_real_sqlite_migration(node.decorator_list)
                )
    return result


def test_the_migrator_driver_scan_follows_helpers_and_exempts_fixtures():
    source = (
        "import pytest\n"
        "from app.repositories.sqlite import migrations\n"
        "from app.repositories.sqlite.migrations import SqliteMigrator\n"
        "from app.repositories.sqlite.migrations import SqliteMigrator as Ladder\n"
        "def _ladder(db, settings):\n"
        "    return SqliteMigrator(db, settings).migrate()\n"
        "def _upgraded(db, settings):\n"
        "    return _ladder(db, settings)\n"
        "@pytest.fixture\n"
        "def store(tmp_path):\n"
        "    return _ladder(tmp_path, None)\n"
        "@pytest.fixture(scope='module')\n"
        "def module_store(tmp_path_factory):\n"
        "    return SqliteMigrator(tmp_path_factory, None).migrate()\n"
        "def test_uses_the_fixture(store, module_store):\n"
        "    assert store\n"
        "def test_through_two_helpers(tmp_path):\n"
        "    assert _upgraded(tmp_path, None)\n"
        "@pytest.mark.real_sqlite_migration\n"
        "def test_marked(tmp_path):\n"
        "    assert SqliteMigrator(tmp_path, None).migrate()\n"
        "@pytest.mark.usefixtures('store')\n"
        "def test_usefixtures_is_not_a_fixture(tmp_path):\n"
        "    assert SqliteMigrator(tmp_path, None).migrate()\n"
        "@pytest.mark.parametrize('fixture_path', ['a'])\n"
        "def test_a_fixture_named_parameter(fixture_path):\n"
        "    assert SqliteMigrator(fixture_path, None).migrate()\n"
        "def test_an_import_alias(tmp_path):\n"
        "    assert Ladder(tmp_path, None).migrate()\n"
        "def test_a_module_attribute(tmp_path):\n"
        "    assert migrations.SqliteMigrator(tmp_path, None).migrate()\n"
        "class TestGroup:\n"
        "    def test_in_a_class(self, tmp_path):\n"
        "        assert SqliteMigrator(tmp_path, None).migrate()\n"
        "    def test_a_class_method_through_a_helper(self, tmp_path):\n"
        "        assert _upgraded(tmp_path, None)\n"
        "    def test_a_class_method_without_the_migrator(self, store):\n"
        "        assert store\n"
    )

    assert _tests_that_drive_the_sqlite_migrator(source, "test_x.py") == {
        "test_through_two_helpers": False,
        "test_marked": True,
        "test_usefixtures_is_not_a_fixture": False,
        "test_a_fixture_named_parameter": False,
        "test_an_import_alias": False,
        "test_a_module_attribute": False,
        "TestGroup.test_in_a_class": False,
        "TestGroup.test_a_class_method_through_a_helper": False,
    }
    for pytestmark in (
        "pytestmark = [pytest.mark.slow, pytest.mark.real_sqlite_migration]\n",
        "pytestmark: list = [pytest.mark.real_sqlite_migration]\n",
    ):
        whole_module = source.replace(
            "def _ladder(db, settings):\n", pytestmark + "def _ladder(db, settings):\n", 1
        )
        assert set(
            _tests_that_drive_the_sqlite_migrator(whole_module, "test_x.py").values()
        ) == {True}


def test_sqlite_migration_contracts_bypass_the_schema_template():
    """A test that drives the migrator itself runs the real ladder (D3).

    The real from-scratch ladder is chosen per test by
    ``@pytest.mark.real_sqlite_migration`` (or a module ``pytestmark``), not
    per module: in a module that owns a migrator, every test whose own body
    drives ``SqliteMigrator`` carries the marker, and so does the startup
    wiring test that asserts the startup path migrates. Fixtures that only
    create an empty current-schema database copy the template instead; the
    autouse fixture fails such a copy when the test changed the ladder.
    """
    import ast

    tests_dir = ROOT / "backend" / "tests"
    unmarked: list[str] = []
    marked: list[str] = []
    template_only_modules: list[str] = []
    for path in sorted(tests_dir.glob("test_*.py")):
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
        drivers = _tests_that_drive_the_sqlite_migrator(source, str(path))
        if indirectly_owns_startup_migration:
            tree = ast.parse(source, filename=str(path))
            [startup_test] = [
                node for node in tree.body
                if isinstance(node, ast.FunctionDef)
                and node.name == "test_run_startup_migrates_warms_and_flips_ready"
            ]
            drivers[startup_test.name] = _marks_real_sqlite_migration(
                startup_test.decorator_list
            )
        if not drivers:
            template_only_modules.append(path.name)
        for name, is_marked in drivers.items():
            (marked if is_marked else unmarked).append(f"{path.name}::{name}")
    assert unmarked == [], (
        "these tests drive the SQLite migrator in their own body but would get "
        "the current-schema template copy; mark them "
        "@pytest.mark.real_sqlite_migration"
    )
    # Non-vacuity: both sides of the per-test split exist.
    assert "test_ui_mode.py::test_sqlite_migration_v44_to_v45_adds_ui_mode_column" in marked
    assert "test_readiness_gate.py::test_run_startup_migrates_warms_and_flips_ready" in marked
    assert {
        "test_agent_profile_job_base.py",
        "test_catalog_store.py",
        "test_retrieval_experience_store.py",
    } <= set(template_only_modules)


def test_the_template_copy_refuses_a_ladder_the_test_changed(tmp_path, monkeypatch):
    """Unmarked tests get the template copy, unless they changed the ladder."""
    from app.core.config import Settings
    from app.repositories.sqlite import migrations as migrations_module
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator

    def migrator(name: str) -> SqliteMigrator:
        settings = Settings(database_url=f"sqlite:///{tmp_path / name}")
        return SqliteMigrator(SqliteDatabase(settings, tmp_path), settings)

    copied = migrator("unchanged.db")
    assert copied.migrate() == list(range(1, SCHEMA_VERSION + 1))
    with copied.database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    copied.database.close_local()

    on_instance = migrator("instance.db")
    on_instance._migration_5 = lambda: None
    with pytest.raises(pytest.fail.Exception, match="real_sqlite_migration"):
        on_instance.migrate()
    assert not on_instance.database.db_path.exists()

    with monkeypatch.context() as patch:
        patch.setattr(SqliteMigrator, "_migration_7", lambda self: None)
        with pytest.raises(pytest.fail.Exception, match="real_sqlite_migration"):
            migrator("class-step.db").migrate()

    with monkeypatch.context() as patch:
        patch.setattr(migrations_module, "SCHEMA_VERSION", 3)
        with pytest.raises(pytest.fail.Exception, match="real_sqlite_migration"):
            migrator("version.db").migrate()

    class SkipsAStep(SqliteMigrator):
        def _migration_9(self) -> None:
            return None

    base = migrator("subclass.db")
    subclassed = SkipsAStep(base.database, base.settings)
    with pytest.raises(pytest.fail.Exception, match="real_sqlite_migration"):
        subclassed.migrate()
    assert not subclassed.database.db_path.exists()


@pytest.mark.real_sqlite_migration
def test_the_marker_runs_the_real_ladder_from_scratch(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.repositories.sqlite import migrations as migrations_module
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.migrations import SqliteMigrator

    monkeypatch.setattr(migrations_module, "SCHEMA_VERSION", 3)
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'real.db'}")
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate() == [1, 2, 3]
    with database.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    database.close_local()
