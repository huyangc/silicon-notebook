"""Schema test for the community_members reverse-index table (Task 2).

SQLiteRepository's real ctor is SQLiteRepository(settings) (db_path resolved from
settings.sqlite_path), so we point DATABASE_URL at tmp_path like the existing repo
fixtures (see test_ppr_retrieve.py) rather than the (path, settings) signature the
plan sketched.
"""
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_community_members_table(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(community_members)")}
    assert {
        "canonical_id",
        "notebook_id",
        "level",
        "community_id",
        "canonical_name",
        "centrality",
    } <= cols


def test_community_members_indexes(repo):
    with repo._connect() as db:
        idx = {r["name"] for r in db.execute("PRAGMA index_list(community_members)")}
    assert "idx_commmem_nb_can" in idx
    assert "idx_commmem_nb_comm" in idx


def test_migration_3_backfills_on_deployed_db(repo):
    """回归生产 bug:已部署库(user_version 已达标,当年跑 _migration_1 时 baseline
    还没 community_members)重启后应由 _migration_3 补建该表。

    community 层把 community_members 塞进 _migration_1 却未 bump SCHEMA_VERSION,
    导致 `_migrate` 的 `if current >= SCHEMA_VERSION: return []` 版本闸对旧库短路、
    不重跑 _migration_1 → 缺表 → community_peers()/expand_community 与报告社区节
    `no such table: community_members` 崩。此测试模拟旧库并断言重启即补建。
    (顺延为 _migration_3:master 的 _migration_2 已被 admin created_by 索引占用。)"""
    from app.services.sqlite_repository import SCHEMA_VERSION

    # 模拟旧部署库:删掉 community_members + 版本戳回退到 2(已跑过 admin 的
    # _migration_2 却仍缺 community_members —— 贴近真实部署)。communities 表故意
    # 保留 —— 复现"communities 在但反向索引缺"。
    with repo._connect() as db:
        db.executescript(
            "DROP INDEX IF EXISTS idx_commmem_nb_can;"
            "DROP INDEX IF EXISTS idx_commmem_nb_comm;"
            "DROP TABLE IF EXISTS community_members;"
        )
        db.execute("PRAGMA user_version = 2")
    with repo._connect() as db:  # 确认已成缺表状态
        assert not db.execute("PRAGMA table_info(community_members)").fetchall()

    applied = repo._migrate()  # 等价于后端重启时的迁移

    assert 3 in applied, "应用了 _migration_3"
    assert SCHEMA_VERSION == 3
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == 3
        cols = {r["name"] for r in db.execute("PRAGMA table_info(community_members)")}
        idx = {r["name"] for r in db.execute("PRAGMA index_list(community_members)")}
    assert {"canonical_id", "notebook_id", "community_id", "centrality"} <= cols
    assert {"idx_commmem_nb_can", "idx_commmem_nb_comm"} <= idx
