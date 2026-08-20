"""测试进程默认开 auth_optional：无 token 的请求回退 seeded admin，
既有 HTTP 测试无需逐一登录即可继续以 admin 身份跑。"""
import os
from itertools import count
from pathlib import Path
import shutil
import time

os.environ.setdefault("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")

# 缓存默认开，但测试进程必须强制关闭：带真 .env 跑全量时，共享的缓存文件会让
# 断言读到上一次运行的响应，制造大规模假失败/假成功。
os.environ["LLM_CACHE_ENABLED"] = "false"

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
    "test_merge_dbs_taxonomy.py",
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
    """
    if request.path.name in _REAL_PASSWORD_HASH_MODULES:
        return

    from app.services import auth_utils

    real_hash_password = auth_utils.hash_password

    def fast_hash_password(
        password: str,
        *,
        salt: str | None = None,
        iterations: int = _TEST_PASSWORD_HASH_ITERATIONS,
    ) -> tuple[str, str, int]:
        return real_hash_password(password, salt=salt, iterations=iterations)

    monkeypatch.setattr(auth_utils, "hash_password", fast_hash_password)

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Keep repository-wide source scans on one worker.

    Their process-local indexes are deliberately shared. Spreading the modules
    across xdist workers reparses the whole repository in every process and
    increases both CPU cost and complete-gate latency.
    """
    for item in items:
        if Path(str(item.fspath)).name in _ARCHITECTURE_CONTRACT_MODULES:
            item.add_marker(
                pytest.mark.architecture_contract,
            )
            item.add_marker(
                pytest.mark.xdist_group(name="architecture_contract"),
            )
        elif Path(str(item.fspath)).name in _GRAPH_INDEX_CONTRACT_MODULES:
            item.add_marker(
                pytest.mark.graph_index_contract,
            )
            item.add_marker(
                pytest.mark.xdist_group(name="graph_index_contract"),
            )


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
