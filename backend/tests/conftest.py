"""测试进程默认开 auth_optional：无 token 的请求回退 seeded admin，
既有 HTTP 测试无需逐一登录即可继续以 admin 身份跑。"""
import atexit
import gc
import os
import sys
import tempfile
from itertools import count
from pathlib import Path
import shutil
import time
from types import ModuleType

os.environ.setdefault("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")

# 缓存默认开，但测试进程必须强制关闭：带真 .env 跑全量时，共享的缓存文件会让
# 断言读到上一次运行的响应，制造大规模假失败/假成功。
os.environ["LLM_CACHE_ENABLED"] = "false"

# 部署插件配置同理必须硬清：留着开发者本机 .env 里的 EXTENSIONS_CONFIG 会让测试
# 进程装入真实部署插件，冻结拓扑与 openapi() 契约会随本机配置漂移。
os.environ["EXTENSIONS_CONFIG"] = ""

# 数据库与存储的进程级默认值，理由与下面日志目录那块相同：收集期、高作用域夹具和
# `monkeypatch.undo()` 之后都不在逐测试夹具的窗口里，读到「未设置」就回落到
# Settings 默认值——checkout 的 `.local/silicon_notebook.db` 与 `.local/storage`，在
# 开发者主 checkout 里那是真实的库和存储——或开发者 shell / `.env` 导出的部署值。
# 强制覆盖而不是 setdefault，形状沿用 Settings 默认值（SQLite 文件 + 存储目录），只换
# 根目录；`sqlite:///` 后接绝对路径被 `_anchor_relative_paths_to_repo_root` 原样保留。
# 显式传 `database_url=` 或在测试里 setenv 的（PG 泳道全是这样）照旧优先。
# tests/test_collection_time_log_isolation.py 同样守住这两个变量在收集期位于系统
# 临时目录之下。
_PROCESS_DATA_DIR = Path(tempfile.mkdtemp(prefix="silicon-notebook-test-data-"))
atexit.register(shutil.rmtree, _PROCESS_DATA_DIR, ignore_errors=True)
os.environ["DATABASE_URL"] = f"sqlite:///{_PROCESS_DATA_DIR / 'silicon_notebook.db'}"
os.environ["SILICON_NOTEBOOK_STORAGE_DIR"] = str(_PROCESS_DATA_DIR / "storage")

# 日志目录的进程级默认值必须在任何测试模块导入前就指向临时目录。下面的 autouse
# 夹具逐测试改 EVENT_LOG_DIR / LLM_LOG_PATH，但有三类代码不在它生效的窗口里读取
# 设置并把日志路径固化下来：
# - 收集期：测试模块在模块级 `from app.main import app`，模块级的
#   `app = create_app()` 就在收集期执行，requests 通道的 EventLogger 绑定到当时
#   解析出的目录；之后同一 worker 里任何测试经这个 app 发出的请求都写到那里。
# - 高作用域夹具：module/session 级夹具先于函数级 autouse 夹具实例化，在里面建的
#   仓库把 events 通道绑定到当时的目录。
# - 测试里调 `monkeypatch.undo()`：它连同 autouse 夹具的 setenv 一起撤销，之后新建
#   的 Settings 回落到这里的进程级值（test_incremental_fusion 的循环就这么做）。
#   所以 undo() 之后不必为日志目录再 setenv 一次。
# 没有这层默认值时那个目录就是 checkout 的 `.local/logs`（或开发者 `.env` 里配的
# 部署日志目录），并行 worker 写进去会让断言 checkout 日志不变的用例（推理循环
# 表征）间歇变红。强制覆盖而不是 setdefault：开发者 shell 里导出的值同样不该接收
# 测试日志。tests/test_collection_time_log_isolation.py 守住收集期这条：两个变量在
# 收集期必须位于系统临时目录之下。
_PROCESS_LOG_DIR = Path(tempfile.mkdtemp(prefix="silicon-notebook-test-logs-"))
atexit.register(shutil.rmtree, _PROCESS_LOG_DIR, ignore_errors=True)
os.environ["EVENT_LOG_DIR"] = str(_PROCESS_LOG_DIR)
os.environ["LLM_LOG_PATH"] = str(_PROCESS_LOG_DIR / "llm.jsonl")

# python-igraph imports its drawing adapters lazily on the first graph rebuild,
# which imports Matplotlib. On macOS, a missing Matplotlib cache invokes the
# system font enumerator (~8 s). Without a shared prewarm every xdist worker
# repeats that subprocess concurrently. Build one repo-local cache in the
# controller before workers are spawned; workers then only read the artifact.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_MPLCONFIGDIR = _REPO_ROOT / ".local" / "matplotlib"
os.environ["MPLCONFIGDIR"] = str(_MPLCONFIGDIR)
_MPLCONFIGDIR.mkdir(parents=True, exist_ok=True)
if "PYTEST_XDIST_WORKER" not in os.environ:
    import matplotlib.font_manager as _matplotlib_font_manager  # noqa: F401


import pytest

from tests.architecture.semantic_source import PythonSourceIndex


_ARCHITECTURE_CONTRACT_MODULES = {
    "test_architecture_documentation.py",
    "test_repository_dependency_contract.py",
    "test_repository_monkeypatch_owners.py",
    "test_repository_protocol_coverage.py",
    "test_semantic_source.py",
}
# Structural item B7: architecture_contract used to be one 64-test, G2-only
# group (~42s serial). Timing it (`pytest -m architecture_contract
# --durations=0 -n0`) showed only 8 of the 64 actually cost >2s per test —
# the rest are cheap, self-contained AST scans that were sitting in the daily
# lane purely because they lived in a file matched by
# `_ARCHITECTURE_CONTRACT_MODULES` above (module-name matching is all-or-
# nothing) or carried a blanket `@pytest.mark.architecture_contract`
# decorator. This explicit (file, test name) allowlist is the *only* thing
# that additionally tags `architecture_contract_heavy`; every other
# architecture_contract test keeps running (cheaply) in G1. It is deliberately
# a flat allowlist rather than a per-file cost policy: several of these files
# mix cheap and expensive tests (e.g. test_repository_protocol_coverage.py's
# two >2s tests share a module-level `@lru_cache` full-corpus AST parse with
# two <0.005s tests that never touch it), so the split has to be per-test, not
# per-file. Entries keep the historical `xdist_group("architecture_contract")`
# co-location (via `_ARCHITECTURE_CONTRACT_MODULES` membership, unchanged)
# wherever more than one heavy test in the same file benefits from sharing a
# worker-local parse cache; the three heavy tests from decorator-only files
# never shared that cache before this change either, so they stay ungrouped.
# Re-time with the command above before editing this set — it is a measured
# split, not a guess — and update `test_test_architecture_policy.py`'s pinned
# `-m` strings plus AGENTS.md/CLAUDE.md/README*.md/docs/development*.md
# together with it.
#
# `test_test_architecture_policy.py::test_verification_lane_markers_partition_
# every_architecture_contract_test` costs ≈2–4s and is a deliberate exception to
# this split: it stays in G1 on every PR because it is the guard that proves
# the 56/8 split above is actually correct, not one of the tests being split
# by it. Re-timing the split with the `-m architecture_contract` command above
# will not select this test — it carries no `architecture_contract` marker of
# its own — so it never shows up in that accounting and never needs adding to
# `_ARCHITECTURE_CONTRACT_HEAVY_TESTS`.
_ARCHITECTURE_CONTRACT_HEAVY_TESTS = {
    (
        "test_repository_monkeypatch_owners.py",
        "test_every_private_facade_patch_targets_a_manifest_marked_seam",
    ),
    (
        "test_architecture_hardening.py",
        "test_retired_repository_model_attributes_cannot_be_read_or_rebound",
    ),
    (
        "test_semantic_source.py",
        "test_session_index_contains_known_repository_import",
    ),
    (
        "test_collection_enumeration.py",
        "test_enumerable_element_kinds_have_exactly_one_literal_definition",
    ),
    (
        "test_repository_dependency_contract.py",
        "test_retired_retrieval_privates_have_no_production_callers",
    ),
    (
        "test_test_architecture_policy.py",
        "test_repository_contracts_have_no_source_position_identity_or_markers",
    ),
    (
        "test_repository_protocol_coverage.py",
        "test_retrieval_port_declares_every_production_retrieval_call",
    ),
    (
        "test_repository_protocol_coverage.py",
        "test_ask_ports_declare_the_executable_service_and_route_surface",
    ),
}
_GRAPH_INDEX_CONTRACT_MODULES = {
    "test_auto_scale_index.py",
    "test_autotune_wiring.py",
    "test_canonical_relations.py",
    "test_chunk_retrieval.py",
    "test_chunk_retrieval_characterization.py",
    "test_incremental_fuse_bounded.py",
    "test_incremental_fusion.py",
    "test_index_build_consolidation.py",
    "test_ppr.py",
    "test_ppr_fallback_guard.py",
    "test_ppr_retrieve.py",
    "test_scale_artifact_runtime.py",
    "test_scale_combined_splice_equivalence.py",
    "test_source_partitioned_ppr.py",
    "test_viz_bounded.py",
}

# ``tmp_path`` creates and later walks a directory for every test, including
# pure parser/policy tests that never touch the filesystem. Keep the same
# per-test isolation contract with cheap, unique paths below xdist's existing
# worker-specific base temp directory. The application creates storage/log
# directories lazily when a test actually uses them; SQLite only needs the
# already-existing base directory for its database file.
_TEST_ISOLATION_IDS = count()
_TEST_PASSWORD_HASH_ITERATIONS = 1
_REAL_PASSWORD_HASH_MODULES = {
    "test_auth_utils.py",
    "test_repository_snapshot_verifier.py",
}
# A test runs the real SQLite migration ladder from scratch only when it
# carries this marker, on the test itself or for the whole module through
# `pytestmark`. Every other test gets a copy of the per-worker template below
# whenever it migrates a database file that does not exist yet; a database
# that already exists (an old-version fixture, a rolled-back copy) is always
# migrated for real. `test_test_architecture_policy.py` requires the marker on
# every test whose own body drives the migrator in a module that owns one, and
# `_reuse_current_sqlite_schema` fails a template copy for a test that changed
# the ladder without it.
REAL_SQLITE_MIGRATION_MARKER = "real_sqlite_migration"


def _sqlite_ladder_identity(migrations_module, migrator=None) -> tuple:
    """What a from-scratch migration would run: the head version and its steps.

    Compared by identity, so a test that patches ``SCHEMA_VERSION`` or one
    ``_migration_N`` step -- on the class, on a subclass (the lookup follows the
    migrator's MRO) or on one migrator instance -- no longer matches the ladder
    the template was built from. It guards exactly those two things: a test
    that patches a helper the steps call (a frozen SQL constant, a module
    function) is not detected and must carry the marker itself.

    The refusal is ``pytest.fail``, raised in the thread that migrates; when
    that is not the test's own thread (a worker thread of the code under test)
    pytest only reports it as an unhandled-thread-exception warning, so a test
    that migrates a new database off its own thread gets no hard failure here.
    """
    version = migrations_module.SCHEMA_VERSION
    owner = migrations_module.SqliteMigrator if migrator is None else type(migrator)
    steps = tuple(
        getattr(owner, f"_migration_{step}", None) for step in range(1, version + 1)
    )
    patched_on_instance = tuple(
        sorted(name for name in vars(migrator) if name.startswith("_migration_"))
    ) if migrator is not None else ()
    return version, steps, patched_on_instance


# Ladder identity each worker's template was built from, keyed by template path.
_SQLITE_TEMPLATE_LADDER: dict[Path, tuple] = {}


@pytest.fixture(scope="session")
def _sqlite_schema_template(tmp_path_factory) -> Path:
    """Build the immutable current SQLite schema once in each xdist worker.

    This build is itself a real from-scratch run of the whole ladder, in every
    worker of every gate.
    """
    from app.core.config import Settings
    from app.repositories.sqlite import migrations as migrations_module
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.migrations import SqliteMigrator

    template_root = tmp_path_factory.getbasetemp() / "sqlite-schema-template"
    template_root.mkdir(parents=True, exist_ok=True)
    template_path = template_root / "template.db"
    settings = Settings(
        database_url=f"sqlite:///{template_path}",
        storage_dir=str(template_root / "storage"),
    )
    database = SqliteDatabase(settings, _REPO_ROOT)
    _SQLITE_TEMPLATE_LADDER[template_path] = _sqlite_ladder_identity(migrations_module)
    SqliteMigrator(database, settings).migrate()
    database.close_local()
    return template_path


@pytest.fixture(autouse=True)
def _reuse_current_sqlite_schema(monkeypatch, request, _sqlite_schema_template):
    """Copy current empty DDL unless the test owns a real from-scratch ladder."""
    if request.node.get_closest_marker(REAL_SQLITE_MIGRATION_MARKER) is not None:
        return

    from app.repositories.sqlite import migrations as migrations_module

    SqliteMigrator = migrations_module.SqliteMigrator
    real_migrate = SqliteMigrator.migrate
    template_ladder = _SQLITE_TEMPLATE_LADDER[_sqlite_schema_template]

    def migrate_from_template(migrator: SqliteMigrator) -> list[int]:
        database_path = migrator.database.db_path
        if database_path.exists():
            return real_migrate(migrator)
        if _sqlite_ladder_identity(migrations_module, migrator) != template_ladder:
            # pytest.fail raises a BaseException, so code under test that
            # catches Exception around initialization cannot swallow it.
            pytest.fail(
                f"{request.node.nodeid} changed the SQLite migration ladder "
                "(SCHEMA_VERSION or a _migration_N step) and then migrated a new "
                "database, which would get a copy of the unchanged current-schema "
                f"template. Mark the test @pytest.mark.{REAL_SQLITE_MIGRATION_MARKER} "
                "so it runs the real ladder.",
                pytrace=False,
            )
        database_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_sqlite_schema_template, database_path)
        return list(range(1, template_ladder[0] + 1))

    monkeypatch.setattr(SqliteMigrator, "migrate", migrate_from_template)


@pytest.fixture(autouse=True)
def _fast_default_password_hashing(monkeypatch, request):
    """Keep repository-heavy tests from benchmarking PBKDF2 thousands of times.

    ``test_auth_utils.py`` imports the real helpers during collection and still
    exercises the production default cost directly.  Repository stores import
    ``hash_password`` lazily, so this fixture only replaces their default call
    during a test.  Explicit iteration counts, random salts, persisted fields,
    and real ``verify_password`` behavior remain intact.

    ⚠ B3: patches ``app.domain.auth_utils`` (not ``app.services.auth_utils``,
    which now only re-exports the name at import time). Repository call
    sites do a per-call lazy ``from app.domain.auth_utils import
    hash_password``, which resolves the attribute on the DOMAIN module at
    call time — patching the services shim's own binding would silently stop
    taking effect there.
    """
    if request.path.name in _REAL_PASSWORD_HASH_MODULES:
        return

    from app.domain import auth_utils

    real_hash_password = auth_utils.hash_password

    def fast_hash_password(
        password: str,
        *,
        salt: str | None = None,
        iterations: int = _TEST_PASSWORD_HASH_ITERATIONS,
    ) -> tuple[str, str, int]:
        return real_hash_password(password, salt=salt, iterations=iterations)

    monkeypatch.setattr(auth_utils, "hash_password", fast_hash_password)

    # Self-assert the patch actually lands where repository call sites read
    # it from. Repository stores resolve `hash_password` via a per-call lazy
    # `from app.domain.auth_utils import hash_password`, i.e. they read the
    # attribute off the DOMAIN module object at call time. If this fixture
    # were ever pointed at `app.services.auth_utils` (the re-export shim)
    # instead, repository calls would silently keep using the slow real
    # implementation -- the suite would still pass, just ~25% slower, with
    # no failing assertion anywhere to say why. Assert identity here so that
    # regression is a loud fixture failure, not a quiet timing drift.
    from app.domain.auth_utils import hash_password as _resolved_hash_password

    assert _resolved_hash_password is fast_hash_password, (
        "fast-hash patch target must be app.domain.auth_utils (repository "
        "call sites import hash_password from there lazily per call); "
        "patching app.services.auth_utils would silently do nothing"
    )

# The lane-partition guard runs a ~6 s collect-only subprocess, and it is the
# one G1 test that must not move to G2. xdist's loadgroup scheduler orders its
# work units by group size with a stable sort, so an ungrouped test keeps its
# collection position among the single-test units; where it is collected (near
# the end of the alphabet) it used to be the last test of the gate. Moving it to
# the front of the collection dispatches it in the first wave instead. This
# changes no marker and no lane selection; `test_test_architecture_policy.py`
# pins it.
_DISPATCHED_FIRST = (
    "test_test_architecture_policy.py",
    "test_verification_lane_markers_partition_every_architecture_contract_test",
)


def _collect_first(items, key: tuple[str, str]) -> None:
    """Move the item named ``(file name, test name)`` to the front, if collected."""
    for index, item in enumerate(items):
        if (Path(str(item.fspath)).name, item.name) == key:
            if index:
                items.insert(0, items.pop(index))
            return


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Keep repository-wide source scans on one worker.

    Their process-local indexes are deliberately shared. Spreading the modules
    across xdist workers reparses the whole repository in every process and
    increases both CPU cost and complete-gate latency. It also collects the
    lane-partition guard first (see ``_DISPATCHED_FIRST``).
    """
    collected_file_names: set[str] = set()
    hit_heavy_tests: set[tuple[str, str]] = set()
    for item in items:
        file_name = Path(str(item.fspath)).name
        collected_file_names.add(file_name)
        if file_name in _ARCHITECTURE_CONTRACT_MODULES:
            item.add_marker(
                pytest.mark.architecture_contract,
            )
            item.add_marker(
                pytest.mark.xdist_group(name="architecture_contract"),
            )
        if (file_name, item.name) in _ARCHITECTURE_CONTRACT_HEAVY_TESTS:
            item.add_marker(
                pytest.mark.architecture_contract_heavy,
            )
            hit_heavy_tests.add((file_name, item.name))
        if file_name in _GRAPH_INDEX_CONTRACT_MODULES:
            item.add_marker(
                pytest.mark.graph_index_contract,
            )
            item.add_marker(
                pytest.mark.xdist_group(name="graph_index_contract"),
            )
    _collect_first(items, _DISPATCHED_FIRST)

    # Self-guard for the measured split above: only check allowlist entries
    # whose *file* was actually part of this collection run, so running a
    # narrow subset (e.g. a single test file) never falsely reports the rest
    # of the allowlist as stale. Within a collected file, every entry must be
    # matched by name — a rename, deletion, or reparametrization silently
    # drops a test out of the `architecture_contract_heavy` G2-only lane and
    # back into G1, which is a cost regression the split was built to avoid
    # (and the inverse — an entry that no longer exists at all — is exactly
    # as silent as a stale entry, since Python set membership just never
    # matches instead of raising).
    #
    # A file being present among the collected fspaths does not mean *every*
    # test in that file was collected: `pytest some_file.py::some_test` (a
    # routine way to iterate on a single test — including this very policy
    # file, which itself carries a heavy-allowlisted entry) or `-k` narrow
    # `items` to a subset before this hook ever sees it. Node-id or keyword
    # narrowing can't be validated against the allowlist either way, so skip
    # the check rather than report entries as stale just because a sibling
    # test in the same file wasn't asked for.
    node_id_narrowed = any("::" in arg for arg in config.args)
    keyword_narrowed = bool(config.option.keyword)
    if not node_id_narrowed and not keyword_narrowed:
        stale_heavy_entries = sorted(
            entry
            for entry in _ARCHITECTURE_CONTRACT_HEAVY_TESTS
            if entry[0] in collected_file_names and entry not in hit_heavy_tests
        )
        if stale_heavy_entries:
            message = (
                "backend/tests/conftest.py: _ARCHITECTURE_CONTRACT_HEAVY_TESTS "
                "names (file, test name) pairs that were not found among the "
                "tests collected from that file — the test was likely renamed, "
                "deleted, or reparametrized. Update the allowlist (re-time with "
                "`pytest -m architecture_contract --durations=0 -n0` first; see "
                "the comment above `_ARCHITECTURE_CONTRACT_HEAVY_TESTS`). Stale "
                f"entries: {stale_heavy_entries}"
            )
            # pytest.UsageError raised from a collection hook under xdist can be
            # swallowed by the controller before its message reaches the
            # terminal — write it to stderr directly so a worker process's
            # stderr (which does surface) still carries the actionable text.
            sys.stderr.write(f"\n[architecture_contract_heavy] {message}\n")
            raise pytest.UsageError(message)


def _prune_temporary_bytecode(prefix: Path, temp_dir: Path, repo_root: Path) -> None:
    """Drop the part of the gate's bytecode cache that mirrors the temp directory.

    The gate points ``PYTHONPYCACHEPREFIX`` at ``<repo>/.local/pycache`` and writes
    bytecode there (scripts/check.sh). That also caches every module a test
    writes under ``tmp_path`` or another temporary directory, and the prefix
    mirrors those absolute paths: each run's ``pytest-NNNN`` tree adds ~1 MB
    that no later run can reuse. Removing that one subtree when a session
    starts keeps the cache bounded while the repository's and the
    interpreter's own modules stay warm. Only the repository's own cache is
    touched, and nothing when the checkout itself lives under the temp
    directory.
    """
    prefix = prefix.resolve()
    temp_dir = temp_dir.resolve()
    if prefix != (repo_root / ".local" / "pycache").resolve():
        return
    if temp_dir == Path(temp_dir.anchor) or repo_root.resolve().is_relative_to(temp_dir):
        return
    shutil.rmtree(prefix / temp_dir.relative_to(temp_dir.anchor), ignore_errors=True)


_BYTECODE_PRUNED_ENV = "SN_TESTS_BYTECODE_PRUNED"


def pytest_configure(config):
    # The one pytest_configure in this file (a second definition would silently
    # replace the first). Install the test-process-only speedups before any
    # test module imports ``app.main`` (its module-level ``create_app()`` runs
    # during collection); each one is documented and pinned in
    # ``tests/speedups/``.
    from tests import speedups

    speedups.register(config)

    # Once per top-level session, in the controller (or the only process),
    # before any worker starts compiling. The marker is inherited by the
    # workers and by the pytest sessions some tests start in a subprocess, so
    # a nested session does not prune the outer run's cache mid-run.
    if (
        sys.pycache_prefix
        and not hasattr(config, "workerinput")
        and not os.environ.get(_BYTECODE_PRUNED_ENV)
    ):
        _prune_temporary_bytecode(
            Path(sys.pycache_prefix), Path(tempfile.gettempdir()), _REPO_ROOT
        )
        os.environ[_BYTECODE_PRUNED_ENV] = "1"


def pytest_collection_finish(session):
    """Move everything collection left alive out of the cyclic collector's reach.

    By the end of collection a worker holds the imported application, every
    test module and every collected item, over a million objects that stay
    alive for the whole session. The automatic collector's oldest generation
    walks all of them again on every full collection, and a worker runs tens of
    thousands of those, so their cost scales with that resident heap rather
    than with what each test allocates. ``gc.freeze()`` moves the survivors of
    one last collection into the permanent generation, which the collector
    never scans; objects created afterwards (every fixture, repository and
    response a test makes) are collected exactly as before. A frozen object
    that later becomes cyclic garbage stays allocated until the worker exits,
    which is test-process memory only.
    """
    gc.collect()
    gc.freeze()


@pytest.fixture(scope="session")
def python_source_index() -> PythonSourceIndex:
    root = _REPO_ROOT
    paths = tuple((root / "backend" / "app").rglob("*.py")) + tuple(
        (root / "backend" / "tests").rglob("*.py")
    )
    return PythonSourceIndex.from_paths(root, paths)


@pytest.fixture(autouse=True)
def _reset_singleton_caches(tmp_path_factory, monkeypatch):
    """Give every test an isolated default DB/storage/log root and clear singletons.

    Individual tests may override or delete these variables after this fixture
    starts. The default prevents a partial ``Settings(database_url=...)`` from
    touching the developer's real ``.local/storage`` and makes the suite safe
    in linked worktrees and CI sandboxes.
    """
    from app.api import deps
    from app.core.config import get_settings

    base_temp = tmp_path_factory.getbasetemp()
    isolation_id = next(_TEST_ISOLATION_IDS)
    repository_factory = deps.repository
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite:///{base_temp / f'default-{isolation_id}.db'}"
    )
    monkeypatch.setenv(
        "SILICON_NOTEBOOK_STORAGE_DIR",
        str(base_temp / f"storage-{isolation_id}"),
    )
    monkeypatch.setenv("EVENT_LOG_DIR", str(base_temp / f"logs-{isolation_id}"))
    monkeypatch.setenv(
        "LLM_LOG_PATH", str(base_temp / f"logs-{isolation_id}" / "llm.jsonl")
    )
    get_settings.cache_clear()
    repository_factory.cache_clear()
    yield
    get_settings.cache_clear()
    repository_factory.cache_clear()


@pytest.fixture(autouse=True)
def _reset_pending_bus():
    """pending_bus 是进程级单例,状态会跨测试串味——统一在这里重置。

    症状:任何测试只要触发 index_done / paper_meta_done 这类 emit,而当时没有
    活的 SSE 连接,事件就滞留进 _buffer;同一个 xdist worker 里后跑的
    test_me_pending_stream_first_frame_is_snapshot 读到的首帧于是变成这条事件
    而非 snapshot(端点契约就是「先补发离线缓冲、再发 snapshot」)。哪些测试同
    worker、谁先跑由 xdist 分配决定 → 表现为间歇失败。
    _loop 同理:端点 bind_loop 记下的 loop,在测试的 asyncio.run 结束后即关闭,
    留给后续测试就是个已关闭的 loop。

    ce8fa1e4 曾逐个给污染源打桩 emit,但 emit 点只会越来越多(彼时修掉
    test_paper_meta_service,如今 test_scale_index_repo / test_batch_ingest /
    test_relation_ann 仍在泄漏),逐点堵按下葫芦浮起瓢;故改为统一重置。
    """
    from app.services.pending_bus import pending_bus
    pending_bus.reset()
    yield
    pending_bus.reset()


@pytest.fixture(autouse=True)
def _reset_extension_admission():
    """插件运行时停用快照是进程级单例,和上面 pending_bus / 后台并发闸同一类问题。

    这份 frozenset 有两类写入者:测试自己 publish 的停用集,以及**任何**构造仓库
    的测试——组合根 `create_application_repository` 会按库里的开关行 prime 一次。
    两者都不自带回收,于是同一个 xdist worker 里先跑的测试可以把「某插件被停用」
    留给后跑的测试,而后者只会表现为 registry 闸莫名其妙地把贡献点判成
    `admin_disabled`——和 pending_bus 那条一样,谁先跑由 xdist 分配决定,所以是
    间歇失败而不是稳定红。统一在这里前后各清一次。

    注意这只复位快照,不停刷新线程:线程由 `startup_warmup` 的 lifecycle 成对
    管理(且默认零 deployment 插件时根本不起),不属于本 fixture 的职责。
    """
    from app.core import extension_admission
    extension_admission.reset_for_tests()
    yield
    extension_admission.reset_for_tests()


@pytest.fixture(autouse=True)
def _reset_background_job_gates():
    """后台并发闸是进程级单例,容量在首次用到时按当时的 Settings 定死。

    同一个 xdist worker 里,只要有测试改过 `BACKGROUND_*_CONCURRENCY`(或只是
    先跑了一个维护类 job),那份容量就会被后跑的测试继承——串味方向还不固定,
    取决于 xdist 的分配顺序。统一在这里前后各清一次。

    T5 修复轮追加:teardown 在丢弃/关闭池之前先对两个维护池(重活/轻活)做
    **有界等待收敛**——理由与下面 `_drain_knowhow_projection_schedulers` 完全
    同构。`_reset_maintenance_gate_for_tests` 只停 IDLE 的 worker:一个已经被
    某个 worker 从队列取出、正在跑的维护类 job(比如「AI 对这个库的理解」巡固,
    见 `_LIGHT_MAINTENANCE_OPERATIONS` 里的 `agentprofile`——一次有界 LLM 调用+
    几条聚合查询,故意进轻活池以免被小时级重建饿死)不会因为 executor 对象被
    丢弃而停止执行,它仍在自己的线程上跑到底:可能还在往这一测试已经拆掉的临时
    数据库写,也可能发出下一个测试完全没预期到的事件(例如
    `agent_profile_consolidated`,实测复现见 `test_report_engine.py` 的跨测试
    噪声——只关掉池、不等在飞任务收敛时,任何跑过维护类 job 的测试都可能把这条
    事件泄漏进随后按 xdist 调度顺序排到同一 worker 的下一个测试)。
    """
    from app.services import background_jobs
    background_jobs._reset_maintenance_gate_for_tests()
    yield
    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)
    background_jobs._reset_maintenance_gate_for_tests()


@pytest.fixture(autouse=True)
def _drain_knowhow_projection_schedulers():
    """Cancel pending projection timers and reap active runs after every test.

    Route tests and direct-service tests can construct different repository
    objects.  File-local cleanup that watches only one of them lets the other
    repository's delayed timer write into a closed test database during the
    next test, producing order-dependent foreign-key and projection failures.
    """
    yield
    from app.services.knowhow import api as knowhow_api

    schedulers = list(knowhow_api._SCHEDULERS.values())
    deadline = time.monotonic() + 10.0
    pending_timers = []
    for scheduler in schedulers:
        with scheduler._lock:
            pending = list(scheduler._timers.values())
            scheduler._timers.clear()
            scheduler._rerun.clear()
        pending_timers.extend(pending)
        for timer in pending:
            timer.cancel()
    for timer in pending_timers:
        timer.join(timeout=max(0.0, deadline - time.monotonic()))
    assert not any(timer.is_alive() for timer in pending_timers), (
        "knowhow projection timer did not stop during teardown"
    )

    remaining = schedulers
    while remaining and time.monotonic() < deadline:
        active = []
        for scheduler in remaining:
            with scheduler._lock:
                if scheduler._running or scheduler._timers or scheduler._rerun:
                    active.append(scheduler)
        remaining = active
        if remaining:
            time.sleep(0.01)
    assert not remaining, "knowhow projection scheduler did not quiesce during teardown"


def _split_app_module_namespace() -> list[tuple[ModuleType, str, ModuleType, object]]:
    """Every ``app`` package attribute that binds a submodule object which is
    NOT the one ``sys.modules`` holds under that name (or holds nothing)."""
    split = []
    for package_name, package in list(sys.modules.items()):
        # Name first: it is a string test, while ``hasattr`` on each of the
        # ~2500 registered modules (some with a lazy module ``__getattr__``)
        # is what made this per-test check cost milliseconds. Only ``app``
        # packages can ever be reported, so the order changes nothing else.
        if package_name != "app" and not package_name.startswith("app."):
            continue
        if package is None or not hasattr(package, "__path__"):
            continue
        prefix = package_name + "."
        for attribute, value in list(vars(package).items()):
            if not isinstance(value, ModuleType) or value.__name__ != prefix + attribute:
                continue
            registered = sys.modules.get(value.__name__)
            if registered is not value:
                split.append((package, attribute, value, registered))
    return split


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtest_teardown(item, nextitem):
    """`sys.modules` 与父包属性是同一个模块的两份登记,测试摘掉一份就会分叉。

    症状:某个测试 `sys.modules.pop("app.x.y")` 却没还原,父包 `app.x` 仍把旧的
    `y` 模块对象挂在属性上;下一次 `import app.x.y` 会建一个新对象登记进
    `sys.modules`,而 `from app.x import y` 走属性查找拿到的还是旧对象。此后
    对着其中一个 monkeypatch,另一条路径根本看不见——
    `test_scale_build_cli.py::test_the_composition_root_disowns_the_schema_at_the_call_site`
    就是这样在 2026-09-29 于四条互不相关的分支上各红一次:它 patch 的是属性那一份,
    被测 CLI 的 `from app.repositories.postgres.repository import PostgresRepository`
    读的是 `sys.modules` 那一份,于是真的 `PostgresRepository` 被构造出来。污染源是
    `test_repository_factory.py` 里的裸 `sys.modules.pop`;谁与受害者同 worker、中间
    有没有别的测试恰好重新 import 把属性重绑回来,全看 xdist 分配,所以是间歇失败。

    这里每个测试结束后核对一遍所有 `app` 包的模块属性与 `sys.modules` 是否指向同
    一个对象(约 20 个包,不到 0.1 ms),不一致就把 `sys.modules` 复位到父包属性那
    一份(即污染前的登记)并让**污染者**报错——不这样做,红的永远是后跑的无辜用例。

    做成 teardown 钩子的 wrapper 而不是 autouse fixture:本文件里更早注册的 autouse
    fixture 自己请求了 `monkeypatch`,于是 `monkeypatch` 比任何后注册的 fixture 都
    先建立、后拆除;fixture 形式的核对会跑在 `monkeypatch.undo()` 之前,把用
    `monkeypatch.delitem(sys.modules, ...)` 正确摘模块的测试也判成污染者。钩子在
    所有 fixture finalizer 之后才 yield 回来,看到的才是测试真正留下的状态。
    """
    result = yield
    split = _split_app_module_namespace()
    for _package, _attribute, value, _registered in split:
        sys.modules[value.__name__] = value
    assert not split, (
        "this test left an `app` package attribute and `sys.modules` pointing at "
        "different module objects (a bare `sys.modules.pop` without restore?): "
        + ", ".join(
            f"{package.__name__}.{attribute} is {value!r} but sys.modules holds "
            f"{registered!r}"
            for package, attribute, value, registered in split
        )
    )
    return result


@pytest.fixture(autouse=True)
def _mark_service_ready():
    """The readiness gate 503s every app route until startup warm-up flips ready.
    Tests drive the app via TestClient WITHOUT the lifespan (which is what runs
    warm-up), so mark ready up-front; test_readiness_gate toggles it explicitly."""
    from app.core import readiness
    readiness.mark_ready()
    yield
