"""测试进程默认开 auth_optional：无 token 的请求回退 seeded admin，
既有 HTTP 测试无需逐一登录即可继续以 admin 身份跑。"""
import os
import sys
from itertools import count
from pathlib import Path
import shutil
import time

os.environ.setdefault("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")

# 缓存默认开，但测试进程必须强制关闭：带真 .env 跑全量时，共享的缓存文件会让
# 断言读到上一次运行的响应，制造大规模假失败/假成功。
os.environ["LLM_CACHE_ENABLED"] = "false"

# 部署插件配置同理必须硬清：留着开发者本机 .env 里的 EXTENSIONS_CONFIG 会让测试
# 进程装入真实部署插件，冻结拓扑与 openapi() 契约会随本机配置漂移。
os.environ["EXTENSIONS_CONFIG"] = ""

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
_REAL_SQLITE_MIGRATION_MODULES = {
    "test_agent_observation_store.py",
    "test_agent_profile_job_base.py",
    "test_agent_profile_job_observations.py",
    "test_agent_profile_job_overlay.py",
    "test_agent_profile_store.py",
    "test_catalog_store.py",
    "test_indexing_pipeline_identity.py",
    "test_merge_dbs_taxonomy.py",
    "test_readiness_gate.py",
    "test_repository_snapshot_verifier.py",
    "test_retrieval_experience_store.py",
    "test_search_profile_job.py",
    "test_shadow_sqlite_schema_validation.py",
    "test_ui_mode.py",
}


@pytest.fixture(scope="session")
def _sqlite_schema_template(tmp_path_factory) -> Path:
    """Build the immutable current SQLite schema once in each xdist worker."""
    from app.core.config import Settings
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
    SqliteMigrator(database, settings).migrate()
    database.close_local()
    return template_path


@pytest.fixture(autouse=True)
def _reuse_current_sqlite_schema(monkeypatch, request, _sqlite_schema_template):
    """Copy current empty DDL while migration contract tests run the real ladder."""
    if request.path.name in _REAL_SQLITE_MIGRATION_MODULES:
        return

    from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator

    real_migrate = SqliteMigrator.migrate

    def migrate_from_template(migrator: SqliteMigrator) -> list[int]:
        database_path = migrator.database.db_path
        if database_path.exists():
            return real_migrate(migrator)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_sqlite_schema_template, database_path)
        return list(range(1, SCHEMA_VERSION + 1))

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

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Keep repository-wide source scans on one worker.

    Their process-local indexes are deliberately shared. Spreading the modules
    across xdist workers reparses the whole repository in every process and
    increases both CPU cost and complete-gate latency.
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
def _reset_copy_stats_memo():
    """copy-stats memo 是进程级单例(R2-2 把它从共享 VectorCache 搬出来,形态
    照 knowledge_counts_cache),而 VectorCache 是**每个仓库实例自己的**——搬家
    之后「换一个 repo 就自动换一份缓存」这条隐式隔离没有了。用例里手写的
    notebook id(不是 uuid)因此可能跨用例串味,统一在这里前后各清一次。"""
    from app.services.notebook_scale import invalidate_copy_stats

    invalidate_copy_stats()
    yield
    invalidate_copy_stats()


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


@pytest.fixture(autouse=True)
def _mark_service_ready():
    """The readiness gate 503s every app route until startup warm-up flips ready.
    Tests drive the app via TestClient WITHOUT the lifespan (which is what runs
    warm-up), so mark ready up-front; test_readiness_gate toggles it explicitly."""
    from app.core import readiness
    readiness.mark_ready()
    yield
