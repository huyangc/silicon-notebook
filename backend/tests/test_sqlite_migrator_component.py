from types import SimpleNamespace

import pytest

from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator


def test_initialize_orders_migrate_then_seed_and_never_recovers():
    """initialize() = migrate + seed only. Crash recovery is deliberately NOT
    part of it: every SQLiteRepository construction runs initialize(), and the
    ~20 offline CLI construction sites must not get to declare the server's
    in-progress rows dead. The server calls recover_interrupted_jobs()
    explicitly from startup_warmup.run_startup instead."""
    m = SqliteMigrator(SimpleNamespace(), SimpleNamespace())
    calls = []
    m.migrate = lambda: calls.append("migrate") or []
    m.recover_interrupted_jobs = lambda: calls.append("recover")
    m.seed = lambda: calls.append("seed")
    assert m.initialize() == []
    assert calls == ["migrate", "seed"]


def test_schema_version_constant_is_v33():
    # master v11/v12 hot-path indexes、Memory / Agent v13 migration、Task 1 的
    # sources.memory_id v14 migration、Task 5 的 parse_status/source_type 覆盖
    # 索引 v15 migration、knowhow-tables PR-1 Task 1 的五张新表 v16 migration、
    # paper-metadata Task 1 的 source_paper_meta/source_authors 两表 v17
    # migration、PR-2+3 Task 1 的 cell_code 表 + role 词表重映射 v18 migration、
    # source-asset-linking Task 2 的 notebook_assets.source_id 列 v19 migration
    # 与多领域基准库 Task 1 的 notebook_bases 挂载表 + promotion_candidates.
    # target_base_id 列 v20 migration、JS-trim anchor expression index v21、
    # KG 构建任务状态表 v22、每用户模型服务最新状态表 v23、写锁瘦身改造点 2 的
    # kg_canonical_scratch 表 v24、v25 清除用户模型凭据与旧状态并改为系统模型
    # 服务状态、knowhow 表版本管理的 knowhow_changes/knowhow_milestones 两表 v26、
    # v27 加源完成标记 sources.chunked_at 列、v28 每笔记本文档数量上限，
    # v29 收敛合并前两条 v24 迁移线、v30 加 sources(notebook_id, file_hash)
    # 内容哈希去重索引、v31 加 shadow capture 两张内部表、v32 持久化深度报告
    # 问题理解契约、v33 加关系端点 keyset 覆盖索引。
    assert SCHEMA_VERSION == 33


def test_v29_cluster_membership_migration_dedupes_before_unique_guard(tmp_path):
    import sqlite3

    from app.core.config import Settings
    from app.repositories.sqlite.database import SqliteDatabase

    settings = Settings(database_url=f"sqlite:///{tmp_path / 'cluster-v24.db'}")
    database = SqliteDatabase(settings, tmp_path)
    migrator = SqliteMigrator(database, settings)
    try:
        migrator.initialize()
        with database.connect() as db:
            db.execute("DROP INDEX uq_clusters_notebook_type_member")
            db.execute("PRAGMA user_version = 28")
            db.execute(
                "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                ("nb-cluster-migration", "clusters", "2026-01-01", "2026-01-01"),
            )
            db.executemany(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                "object_type,created_at) VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        "cc-legacy-old",
                        "nb-cluster-migration",
                        "canonical-old",
                        "ko-legacy-duplicate",
                        "old",
                        "concept",
                        "2026-01-01",
                    ),
                    (
                        "cc-legacy-new",
                        "nb-cluster-migration",
                        "canonical-new",
                        "ko-legacy-duplicate",
                        "new",
                        "concept",
                        "2026-01-02",
                    ),
                ],
            )

        assert migrator.migrate() == [29, 30, 31, 32, 33]
        with database.connect() as db:
            rows = db.execute(
                "SELECT id,canonical_id FROM concept_clusters "
                "WHERE notebook_id=? ORDER BY id",
                ("nb-cluster-migration",),
            ).fetchall()
        assert [dict(row) for row in rows] == [
            {"id": "cc-legacy-new", "canonical_id": "canonical-new"}
        ]

        with pytest.raises(sqlite3.IntegrityError):
            with database.connect() as db:
                db.execute(
                    "INSERT INTO concept_clusters "
                    "(id,notebook_id,canonical_id,member_object_id,canonical_name,"
                    "object_type,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        "cc-legacy-conflict",
                        "nb-cluster-migration",
                        "canonical-conflict",
                        "ko-legacy-duplicate",
                        "conflict",
                        "concept",
                        "2026-01-03",
                    ),
                )
    finally:
        database.close_local()


def test_add_column_guard_on_missing_table(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "x.db")
    SqliteMigrator.add_column_if_missing(db, "missing", "x", "TEXT")
    db.execute("create table t (id integer)")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    SqliteMigrator.add_column_if_missing(db, "t", "x", "TEXT")
    assert [r[1] for r in db.execute("pragma table_info(t)")] == ["id", "x"]
