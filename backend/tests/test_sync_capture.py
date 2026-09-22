"""守卫: 源端变更捕获 (app.migration.sync.capture) 的生成器、SQLite v84 迁移
与触发器行为。设计见 docs/incremental-sync-design.md §7。

三段合同:

1. **生成器是唯一出处**——PostgreSQL 0064 文件里的函数与 46 条触发器必须与
   ``postgres_function_sql()`` / ``postgres_trigger_sql()`` 的输出逐字相同,
   SQLite 138 条触发器必须与 ``sqlite_trigger_sql()`` 逐字相同(迁移里先
   ``DROP TRIGGER IF EXISTS`` 再不带 ``IF NOT EXISTS`` 地建, 就是为了让
   ``sqlite_master`` 存下的文本等于生成器输出)。两端一旦各写一份, 增量导出就会
   在某个后端漏掉某张表的变更 —— 而且不报错。
2. **行身份**——登记了 ``TableSyncSpec.key`` 的表必须在两端都有覆盖该列集的唯一
   面; 没登记的表必须有 catalog 主键。
3. **触发器行为**——门关无日志; 门开时 insert/update/delete 各一条、键变化两条、
   ``unified_kg_state`` 的 ``kg_reset_epoch`` 变化额外一条 ``kg_epoch``;
   PARENT 表填 ``parent_key`` 不填 ``notebook_id``, GLOBAL 表两者都空。

本模块跑真实迁移阶梯(回滚到 v83 再开库), 因此登记在 conftest 的
``_REAL_SQLITE_MIGRATION_MODULES`` 里。
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from app.core.config import Settings
from app.migration.sync.capture import (
    POSTGRES_CAPTURE_FUNCTION,
    expected_postgres_trigger_names,
    expected_sqlite_trigger_names,
    key_columns,
    postgres_function_sql,
    postgres_trigger_sql,
    sqlite_trigger_sql,
)
from app.migration.sync.manifest import SYNC_MANIFEST, synced_tables
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.migrations import SCHEMA_VERSION
from app.services.sqlite_repository import SQLiteRepository


NOW = "2026-09-23T00:00:00+00:00"

_MIGRATION_0064 = (
    pathlib.Path(__file__).resolve().parents[1]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0064_sync_change_capture.sql"
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(_env_file=None))
    try:
        yield repository
    finally:
        repository.close_local()


def _enable_capture(repo) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, 1, ?)",
            (NOW,),
        )


def _log(repo) -> list[dict]:
    with repo._connect() as db:
        return [
            {
                "table_name": row["table_name"],
                "key": json.loads(row["key_json"]),
                "operation": row["operation"],
                "parent_key": row["parent_key"],
                "notebook_id": row["notebook_id"],
                "txid": row["txid"],
                "changed_at": row["changed_at"],
            }
            for row in db.execute(
                "SELECT table_name, key_json, operation, parent_key, notebook_id, "
                "txid, changed_at FROM sync_change_log ORDER BY seq"
            ).fetchall()
        ]


def _seed_notebook(repo, notebook_id: str = "nb-1") -> str:
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES (?,?,?,?)",
            (notebook_id, "n", NOW, NOW),
        )
    return notebook_id


def _seed_source(repo, notebook_id: str, source_id: str = "src-1") -> str:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?)",
            (source_id, notebook_id, "t", "md", NOW, NOW),
        )
    return source_id


# ------------------------------------------------- 1. 生成器是唯一出处


def test_the_postgres_migration_carries_the_generated_text_verbatim():
    """0064 里的函数与每条触发器都必须与生成器输出逐字相同。"""
    text = _MIGRATION_0064.read_text(encoding="utf-8")

    assert postgres_function_sql() + ";" in text
    missing = [
        name
        for name, (_table, sql) in postgres_trigger_sql().items()
        if sql + ";" not in text
    ]
    assert missing == [], missing


def test_the_postgres_migration_declares_no_trigger_beyond_the_generated_set():
    """反向: 文件里不许有生成器之外的 CREATE TRIGGER / CREATE FUNCTION。"""
    text = _MIGRATION_0064.read_text(encoding="utf-8")
    declared = {
        line.split()[2]
        for line in text.splitlines()
        if line.startswith("CREATE TRIGGER ")
    }

    assert declared == set(expected_postgres_trigger_names())
    assert text.count("CREATE FUNCTION ") == 1
    assert f"CREATE FUNCTION {POSTGRES_CAPTURE_FUNCTION}()" in text


def test_the_generated_trigger_sets_cover_every_synced_table():
    tables = synced_tables()

    assert len(tables) == 46
    assert len(expected_postgres_trigger_names()) == 46
    assert len(expected_sqlite_trigger_names()) == 138
    assert {table for _name, (table, _sql) in sqlite_trigger_sql().items()} == set(
        tables
    )


def test_the_sqlite_triggers_are_created_without_if_not_exists():
    """迁移靠 DROP + 无 IF NOT EXISTS 的 CREATE 保证 sqlite_master 文本与生成器
    逐字相等 —— 加上 IF NOT EXISTS 就会被 SQLite 吃掉, 两边文本分叉。"""
    for _name, (_table, sql) in sqlite_trigger_sql().items():
        assert sql.startswith("CREATE TRIGGER \"sync_capture_")
        assert "IF NOT EXISTS" not in sql


# ------------------------------------------------------------- 2. 行身份


def test_only_the_two_keyless_tables_register_a_manifest_key():
    registered = {spec.name: spec.key for spec in SYNC_MANIFEST if spec.key}

    assert registered == {
        "knowledge_object_sources": ("object_id", "source_id"),
        "community_members": ("community_id", "canonical_id"),
    }


def test_every_synced_table_resolves_a_row_key():
    for table in synced_tables():
        assert key_columns(table), table


def test_registered_keys_have_a_unique_index_in_the_sqlite_schema(
    _sqlite_schema_template,
):
    """登记了 key 的表必须在 SQLite 上有覆盖该列集的唯一索引 —— SQLite 不能给
    既有表加主键, 唯一面只能由索引承担, 所以这条守卫是 PG 主键在这一端的对偶。"""
    conn = sqlite3.connect(f"file:{_sqlite_schema_template}?mode=ro", uri=True)
    try:
        for spec in SYNC_MANIFEST:
            if not spec.key:
                continue
            unique_column_sets = set()
            for index in conn.execute(f"PRAGMA index_list({spec.name})").fetchall():
                if not index[2]:
                    continue
                unique_column_sets.add(
                    frozenset(
                        row[2]
                        for row in conn.execute(
                            f"PRAGMA index_info({index[1]})"
                        ).fetchall()
                    )
                )
            assert frozenset(spec.key) in unique_column_sets, spec.name
    finally:
        conn.close()


def test_tables_without_a_manifest_key_have_a_catalog_primary_key(
    _sqlite_schema_template,
):
    conn = sqlite3.connect(f"file:{_sqlite_schema_template}?mode=ro", uri=True)
    try:
        for table in synced_tables():
            from app.migration.sync.manifest import spec_for

            if spec_for(table).key:
                continue
            declared = tuple(
                row[1]
                for row in sorted(
                    conn.execute(f"PRAGMA table_info({table})").fetchall(),
                    key=lambda row: row[5],
                )
                if row[5]
            )
            assert declared, table
    finally:
        conn.close()


# --------------------------------------------------------- 3. 迁移安装结果


def test_a_current_database_installs_the_capture_objects(repo):
    with repo._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        installed = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND name LIKE 'sync_capture_%'"
            ).fetchall()
        }
        assert installed == set(expected_sqlite_trigger_names())
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
                "('idx_sync_change_log_table_seq',"
                "'uq_knowledge_object_sources_sync_key',"
                "'uq_community_members_sync_key')"
            ).fetchall()
        }
        assert len(indexes) == 3
        # 迁移不播种控制行: 没有行 == 门关着。
        assert db.execute("SELECT COUNT(*) FROM sync_capture_control").fetchone()[0] == 0


def _rollback_v84(db: sqlite3.Connection) -> None:
    for name in sorted(expected_sqlite_trigger_names()):
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute("DROP INDEX uq_community_members_sync_key")
    db.execute("DROP INDEX uq_knowledge_object_sources_sync_key")
    db.execute("DROP INDEX idx_sync_change_log_table_seq")
    db.execute("DROP TABLE sync_change_log")
    db.execute("DROP TABLE sync_capture_control")


def test_the_migration_collapses_duplicate_rows_before_taking_the_key(
    repo, tmp_path, monkeypatch
):
    """已部署库里可能有重复行 (两张表此前没有任何唯一面)。迁移必须先按复合键
    去重再建唯一索引, 否则升级会在 CREATE UNIQUE INDEX 上硬失败。"""
    database = tmp_path / "t.db"
    repo.close_local()
    with sqlite3.connect(database) as rollback:
        _rollback_v84(rollback)
        rollback.execute(
            "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
            "VALUES ('o-1','s-1','nb-1'), ('o-1','s-1','nb-1'), ('o-2','s-1','nb-1')"
        )
        rollback.execute(
            "INSERT INTO community_members (canonical_id, notebook_id, level, "
            "community_id, canonical_name, centrality, generation) VALUES "
            "('c-1','nb-1',0,'com-1','a',0.0,1), "
            "('c-1','nb-1',0,'com-1','a',0.0,1), "
            "('c-2','nb-1',0,'com-1','b',0.0,1)"
        )
        rollback.execute("PRAGMA user_version = 83")

    reopened = SQLiteRepository(Settings(_env_file=None))
    try:
        with reopened._connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            assert [
                (row[0], row[1])
                for row in db.execute(
                    "SELECT object_id, source_id FROM knowledge_object_sources "
                    "ORDER BY object_id"
                ).fetchall()
            ] == [("o-1", "s-1"), ("o-2", "s-1")]
            assert [
                (row[0], row[1])
                for row in db.execute(
                    "SELECT community_id, canonical_id FROM community_members "
                    "ORDER BY canonical_id"
                ).fetchall()
            ] == [("com-1", "c-1"), ("com-1", "c-2")]
    finally:
        reopened.close_local()


# ----------------------------------------------------------- 4. 触发器行为


def test_a_closed_gate_logs_nothing(repo):
    notebook_id = _seed_notebook(repo)
    _seed_source(repo, notebook_id)
    with repo._write() as db:
        db.execute("UPDATE sources SET title='t2' WHERE id='src-1'")
        db.execute("DELETE FROM sources WHERE id='src-1'")

    assert _log(repo) == []


def test_an_open_gate_logs_insert_update_and_delete(repo):
    notebook_id = _seed_notebook(repo)
    _enable_capture(repo)
    _seed_source(repo, notebook_id)
    with repo._write() as db:
        db.execute("UPDATE sources SET title='t2' WHERE id='src-1'")
        db.execute("DELETE FROM sources WHERE id='src-1'")

    rows = [row for row in _log(repo) if row["table_name"] == "sources"]
    assert [row["operation"] for row in rows] == ["upsert", "upsert", "delete"]
    assert all(row["key"] == {"id": "src-1"} for row in rows)
    assert all(row["notebook_id"] == notebook_id for row in rows)
    assert all(row["parent_key"] is None for row in rows)
    # SQLite has no transaction id to record; the column exists for parity.
    assert all(row["txid"] is None for row in rows)
    assert all(row["changed_at"].endswith("Z") for row in rows)


def test_a_key_change_logs_the_old_identity_as_a_delete(repo):
    notebook_id = _seed_notebook(repo)
    _seed_source(repo, notebook_id)
    _enable_capture(repo)
    with repo._write() as db:
        db.execute("UPDATE sources SET id='src-2' WHERE id='src-1'")

    rows = [row for row in _log(repo) if row["table_name"] == "sources"]
    assert [(row["operation"], row["key"]["id"]) for row in rows] == [
        ("delete", "src-1"),
        ("upsert", "src-2"),
    ]


def test_a_parent_scoped_table_logs_its_parent_not_a_notebook(repo):
    notebook_id = _seed_notebook(repo)
    source_id = _seed_source(repo, notebook_id)
    _enable_capture(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_elements (id, source_id, element_type, "
            "location_label, text, created_at) VALUES (?,?,?,?,?,?)",
            ("el-1", source_id, "formula", "p1", "x", NOW),
        )

    rows = [row for row in _log(repo) if row["table_name"] == "source_elements"]
    assert len(rows) == 1
    assert rows[0]["parent_key"] == source_id
    assert rows[0]["notebook_id"] is None
    assert rows[0]["key"] == {"id": "el-1"}


def test_a_global_table_logs_neither_notebook_nor_parent(repo):
    _enable_capture(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO object_schemas (object_type, created_at, updated_at) "
            "VALUES (?,?,?)",
            ("custom_type", NOW, NOW),
        )

    rows = [row for row in _log(repo) if row["table_name"] == "object_schemas"]
    assert len(rows) == 1
    assert rows[0]["parent_key"] is None
    assert rows[0]["notebook_id"] is None
    assert rows[0]["key"] == {"object_type": "custom_type"}


def test_a_kg_reset_logs_an_extra_epoch_event(repo):
    notebook_id = _seed_notebook(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO unified_kg_state (notebook_id, updated_at) VALUES (?,?)",
            (notebook_id, NOW),
        )
    _enable_capture(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET kg_reset_epoch = 1 WHERE notebook_id = ?",
            (notebook_id,),
        )
        db.execute(
            "UPDATE unified_kg_state SET updated_at = ? WHERE notebook_id = ?",
            ("2026-09-23T01:00:00+00:00", notebook_id),
        )

    rows = [row for row in _log(repo) if row["table_name"] == "unified_kg_state"]
    # 第一次 UPDATE 动了 kg_reset_epoch: upsert + kg_epoch; 第二次没动: 只 upsert。
    assert [row["operation"] for row in rows] == ["upsert", "kg_epoch", "upsert"]
    assert all(row["key"] == {"notebook_id": notebook_id} for row in rows)


def test_a_keyless_table_logs_its_registered_composite_key(repo):
    notebook_id = _seed_notebook(repo)
    _enable_capture(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
            "VALUES (?,?,?)",
            ("o-1", "s-1", notebook_id),
        )

    rows = [
        row for row in _log(repo) if row["table_name"] == "knowledge_object_sources"
    ]
    assert len(rows) == 1
    assert rows[0]["key"] == {"object_id": "o-1", "source_id": "s-1"}
    assert rows[0]["notebook_id"] == notebook_id


# ------------------------------------------------------- 5. 写点幂等


def test_writing_the_same_object_source_pair_twice_keeps_one_row(repo):
    notebook_id = _seed_notebook(repo)
    with repo._write() as db:
        KnowledgeStore.insert_object_source_rows(db, [("o-1", "s-1", notebook_id)])
        KnowledgeStore.insert_object_source_rows(db, [("o-1", "s-1", notebook_id)])

    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM knowledge_object_sources"
        ).fetchone()[0] == 1
