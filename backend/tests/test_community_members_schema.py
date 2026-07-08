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


def test_migration_3_and_4_backfill_community_schema(repo):
    """回归生产 bug:已部署库(user_version 已达标)重启后应由 _migration_3 补建
    community_members 表、_migration_4 补建 unified_kg_state.community_seq 列。

    community 层把这两样 schema 塞进 _migration_1 却未 bump SCHEMA_VERSION → `_migrate`
    版本闸 `if current >= SCHEMA_VERSION: return []` 对旧库短路、不重跑 _migration_1 →
    缺表 → community_peers()/expand_community 与报告社区节 `no such table` 崩;缺列 →
    rebuild_communities 查 community_seq `no such column` → 社区建不了。此测试模拟旧库
    并断言两步都补齐。(表=_migration_3/PR#210;列=_migration_4:_migration_3 只补了表、
    漏了列,且 master 的 _migration_2 已被 admin created_by 索引占用,故一路顺延。)"""
    from app.services.sqlite_repository import SCHEMA_VERSION

    # 模拟旧部署库:删掉 community_members 表 + community_seq 列 + 版本戳回退。
    # communities 表故意保留 —— 复现"communities 在但反向索引/版本闸缺"。
    with repo._connect() as db:
        db.executescript(
            "DROP INDEX IF EXISTS idx_commmem_nb_can;"
            "DROP INDEX IF EXISTS idx_commmem_nb_comm;"
            "DROP TABLE IF EXISTS community_members;"
        )
        has_uks = bool(db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='unified_kg_state'"
        ).fetchone())
        if has_uks:
            db.execute("ALTER TABLE unified_kg_state DROP COLUMN community_seq")
        db.execute("PRAGMA user_version = 2")
    with repo._connect() as db:  # 确认已成缺表 + 缺列状态
        assert not db.execute("PRAGMA table_info(community_members)").fetchall()
        if has_uks:
            assert "community_seq" not in {
                r["name"] for r in db.execute("PRAGMA table_info(unified_kg_state)")}

    applied = repo._migrate()  # 等价于后端重启时的迁移

    assert 3 in applied and 4 in applied, "应用了 _migration_3(表)与 _migration_4(列)"
    # 版本闸从旧库 user_version=2 迁到当前 SCHEMA_VERSION(非硬编码字面量——每次新增
    # _migration_N 该常量都会 +1,断言与之绑定才不会随后续 schema 变更假红)。
    with repo._connect() as db:
        assert int(db.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_VERSION
        cols = {r["name"] for r in db.execute("PRAGMA table_info(community_members)")}
        idx = {r["name"] for r in db.execute("PRAGMA index_list(community_members)")}
        uks_cols = ({r["name"] for r in db.execute("PRAGMA table_info(unified_kg_state)")}
                    if has_uks else set())
    assert {"canonical_id", "notebook_id", "community_id", "centrality"} <= cols
    assert {"idx_commmem_nb_can", "idx_commmem_nb_comm"} <= idx
    if has_uks:
        assert "community_seq" in uks_cols
