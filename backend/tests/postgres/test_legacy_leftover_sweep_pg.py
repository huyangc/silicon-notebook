"""批 3·W1 PR-4 PostgreSQL twin:孤儿行 anti-join 的 ctid 分页删除 + 盘半在
真 advisory lock 下的清扫/跳过。SQLite 侧行为 pin 在
``backend/tests/test_legacy_leftover_sweep.py``。"""
from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import normalize_timestamp
from app.repositories.scale_build_lock import SCALE_BUILD_LOCK_UNAVAILABLE
from app.migration.legacy_leftover_sweep import (
    DIRECT_DISK_ROOTS,
    ORPHAN_ROW_TABLES,
    SCALE_DISK_ROOTS,
    count_orphan_rows,
    find_orphan_disk,
    sweep_orphan_disk,
    sweep_orphan_rows,
)

pytestmark = pytest.mark.xdist_group(name="postgres_legacy_leftover_sweep")


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _seed_rows(runtime, notebook_id, per_table):
    now = normalize_timestamp(runtime.seams.now())
    with runtime.database.write() as db:
        for i in range(per_table):
            db.execute(
                "INSERT INTO community_members (canonical_id,notebook_id,community_id)"
                " VALUES (%s,%s,%s)",
                (f"can-{i}", notebook_id, f"comm-{i}"),
            )
            db.execute(
                "INSERT INTO conversations (id,notebook_id,created_at,updated_at)"
                " VALUES (%s,%s,%s,%s)",
                (f"conv-{notebook_id}-{i}", notebook_id, now, now),
            )
            db.execute(
                "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id)"
                " VALUES (%s,%s,%s)",
                (f"ko-{i}", f"src-{i}", notebook_id),
            )
            db.execute(
                "INSERT INTO kg_cluster_scratch (notebook_id,run_id,object_id,seed)"
                " VALUES (%s,%s,%s,%s)",
                (notebook_id, "run-1", f"ko-{i}", f"seed-{i}"),
            )
            db.execute(
                "INSERT INTO kg_canonical_scratch (notebook_id,run_id,seed,canonical_id)"
                " VALUES (%s,%s,%s,%s)",
                (notebook_id, "run-1", f"seed-{i}", f"can-{i}"),
            )


@pytest.mark.postgres_integration
def test_orphan_row_sweep_ctid_paging_keeps_live(postgres_repository):
    """ctid IN (…LIMIT n) 形态在真 PG 上:只删孤儿、活本不动、小批量分页收敛。"""
    runtime = postgres_repository._runtime
    live = postgres_repository.create_notebook(NotebookCreate(name="sweep-live")).id
    _seed_rows(runtime, live, 2)
    _seed_rows(runtime, "ghost-pg-a", 3)

    assert count_orphan_rows(runtime.database) == {
        table: 3 for table in ORPHAN_ROW_TABLES
    }
    deleted = sweep_orphan_rows(runtime.database, "postgresql", batch_size=2)
    assert deleted == {table: 3 for table in ORPHAN_ROW_TABLES}
    assert count_orphan_rows(runtime.database) == {
        table: 0 for table in ORPHAN_ROW_TABLES
    }
    with runtime.database.connect() as db:
        for table in ORPHAN_ROW_TABLES:
            row = db.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE notebook_id=%s", (live,)
            ).fetchone()
            assert int(row["n"]) == 2


@pytest.mark.postgres_integration
def test_disk_sweep_under_real_advisory_lock_and_busy_skip(
    postgres_repository, tmp_path
):
    """盘半在真 advisory lock 下:可取锁的孤儿全清(含 scratch 兄弟);同库
    另一把已持有的 claim 让该本 scale 根整体跳过,notebooks/assets 照清。"""
    runtime = postgres_repository._runtime
    live = postgres_repository.create_notebook(NotebookCreate(name="sweep-disk")).id
    storage = tmp_path / "s"
    ghost, busy = "nb-pg-ghost", "nb-pg-busy"
    for root in DIRECT_DISK_ROOTS:
        for nb in (ghost, busy, live):
            (storage / root / nb).mkdir(parents=True)
    for root in SCALE_DISK_ROOTS:
        for name in (ghost, f"{ghost}.tmp-tok", busy, f"{busy}.old", live):
            (storage / root / name).mkdir(parents=True)

    held = runtime.database.try_scale_build_lock(busy)
    assert held is not None and held is not SCALE_BUILD_LOCK_UNAVAILABLE
    try:
        report = sweep_orphan_disk(
            runtime.database, "postgresql", storage, min_age_seconds=0
        )
    finally:
        held.release()

    assert (busy, "lock_held_elsewhere") in report.skipped
    assert not report.failed_paths
    for root in DIRECT_DISK_ROOTS:
        assert not (storage / root / ghost).exists()
        assert not (storage / root / busy).exists()
        assert (storage / root / live).is_dir()
    for root in SCALE_DISK_ROOTS:
        assert not (storage / root / ghost).exists()
        assert not (storage / root / f"{ghost}.tmp-tok").exists()
        assert (storage / root / busy).is_dir()
        assert (storage / root / f"{busy}.old").is_dir()
        assert (storage / root / live).is_dir()

    orphans = find_orphan_disk(runtime.database, "postgresql", storage)
    for root in SCALE_DISK_ROOTS:
        assert orphans[root] == [busy]
