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
import re
import sqlite3
from datetime import datetime

import pytest

from app.core.config import Settings
from app.migration.shadow.postgres_catalog import (
    EXPECTED_CAPTURE_DDL,
    _capture_ddl_from,
)
from app.migration.shadow.postgres_catalog import _MIGRATIONS as _PACKAGED_MIGRATIONS
from app.migration.sync.capture import (
    expected_postgres_function_names,
    expected_postgres_trigger_names,
    expected_sqlite_trigger_names,
    key_columns,
    postgres_function_body,
    postgres_function_sql,
    postgres_trigger_sql,
    sqlite_trigger_sql,
)
from app.migration.sync.manifest import SYNC_MANIFEST, synced_tables
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.migrations import SCHEMA_VERSION
from app.services.sqlite_repository import SQLiteRepository


NOW = "2026-09-23T00:00:00+00:00"
# The shape ``SQLiteMigrator._now()`` produces:
# datetime.now(timezone.utc).isoformat() -> microseconds and an explicit offset.
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00")

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
    """0064 里的每个函数与每条触发器都必须与生成器输出逐字相同。"""
    text = _MIGRATION_0064.read_text(encoding="utf-8")

    missing = [
        name
        for name, (_table, sql) in (
            list(postgres_function_sql().items()) + list(postgres_trigger_sql().items())
        )
        if sql + ";" not in text
    ]
    assert missing == [], (
        f"0064 与生成器不一致, 缺失: {missing}。重新渲染: "
        "python3 scripts/generate_sync_capture_migration.py"
    )


def test_the_migration_parse_takes_the_last_definition_of_each_object():
    """一条后续迁移 ``CREATE OR REPLACE`` 掉某个捕获函数、或重建某条触发器时,
    期望值必须取**最后一次**定义 —— 线上 catalog 里留下的就是那一份。按出现
    顺序做字典赋值即可, 但前提是 ``CREATE OR REPLACE FUNCTION`` 也被认出来
    (只认 ``CREATE FUNCTION`` 的话, 覆盖后的期望会停留在旧文本, 守卫从此对着
    一份已经不存在的函数体报 drift)。"""
    later_migration = (
        "CREATE OR REPLACE FUNCTION sync_capture_sources() RETURNS trigger\n"
        "LANGUAGE plpgsql AS $later$\nBEGIN\n  RETURN NULL;\nEND;\n$later$;\n"
        "DROP TRIGGER sync_capture_sources ON sources;\n"
        "CREATE TRIGGER sync_capture_sources AFTER INSERT ON sources "
        "FOR EACH ROW EXECUTE FUNCTION sync_capture_sources();\n"
    )
    packaged = [
        migration.sql for migration in _PACKAGED_MIGRATIONS
    ]

    triggers, bodies = _capture_ddl_from([*packaged, later_migration])

    assert bodies["sync_capture_sources"] == "\nBEGIN\n  RETURN NULL;\nEND;\n"
    assert triggers["sync_capture_sources"] == (
        "CREATE TRIGGER sync_capture_sources AFTER INSERT ON sources "
        "FOR EACH ROW EXECUTE FUNCTION sync_capture_sources()"
    )
    # 其它 45 张表不受这条迁移影响。
    assert len(bodies) == 46 and len(triggers) == 46


def test_the_migration_parse_retires_a_dropped_capture_object():
    """对称的另一半: 后续迁移 DROP 掉捕获函数/触发器时, 期望集里也要消失 ——
    否则守卫会一直索要一个 schema 早已不该有的对象, 那张表永远升不上去。"""
    later_migration = (
        "DROP TRIGGER IF EXISTS sync_capture_sources ON sources;\n"
        "DROP FUNCTION IF EXISTS sync_capture_sources();\n"
    )
    packaged = [migration.sql for migration in _PACKAGED_MIGRATIONS]

    triggers, bodies = _capture_ddl_from([*packaged, later_migration])

    assert "sync_capture_sources" not in triggers
    assert "sync_capture_sources" not in bodies
    assert len(triggers) == 45 and len(bodies) == 45


def test_the_migration_parse_ignores_commented_out_ddl():
    """只认行首语句: 注释里出现同样的字眼不算 DDL。"""
    commented = (
        "-- CREATE TRIGGER sync_capture_ghost AFTER INSERT ON ghost "
        "FOR EACH ROW EXECUTE FUNCTION sync_capture_ghost();\n"
        "-- DROP FUNCTION sync_capture_sources();\n"
    )
    packaged = [migration.sql for migration in _PACKAGED_MIGRATIONS]

    triggers, bodies = _capture_ddl_from([*packaged, commented])

    assert "sync_capture_ghost" not in triggers
    assert "sync_capture_sources" in bodies


def test_the_catalog_guards_parse_of_the_migration_equals_the_generator():
    """守卫不导入生成器(会成环), 它自己解析 0064。这条把「解析结果 == 生成器
    输出」钉死, 链条才闭合: 生成器 ≡ 0064(上一条) ≡ 守卫的期望(这一条)。"""
    parsed_triggers, parsed_bodies = EXPECTED_CAPTURE_DDL

    assert parsed_triggers == {
        name: sql for name, (_table, sql) in postgres_trigger_sql().items()
    }
    assert parsed_bodies == {
        name: postgres_function_body(table)
        for name, (table, _sql) in postgres_function_sql().items()
    }


def test_the_postgres_migration_declares_nothing_beyond_the_generated_set():
    """反向: 文件里不许有生成器之外的 CREATE TRIGGER / CREATE FUNCTION。"""
    text = _MIGRATION_0064.read_text(encoding="utf-8")
    triggers = {
        line.split()[2]
        for line in text.splitlines()
        if line.startswith("CREATE TRIGGER ")
    }
    functions = {
        line.split()[2].split("(")[0]
        for line in text.splitlines()
        if line.startswith("CREATE FUNCTION ")
    }

    assert triggers == set(expected_postgres_trigger_names())
    assert functions == set(expected_postgres_function_names())
    assert text.count("CREATE FUNCTION ") == 46


def test_no_generated_statement_materializes_a_whole_row():
    """行级触发器绝不整行序列化: 捕获只要键列与 scope 列。``to_jsonb(NEW)``
    会把 chunk_embeddings/knowledge_embeddings 的 bytea 向量、chunks.text 每写
    一行都序列化一遍。"""
    rendered = [sql for _name, (_table, sql) in postgres_function_sql().items()]
    rendered += [sql for _name, (_table, sql) in sqlite_trigger_sql().items()]

    for sql in rendered:
        assert "to_jsonb(" not in sql
        assert "row_to_json(" not in sql
        assert "jsonb_object_agg(" not in sql
        assert "TG_ARGV" not in sql


def test_the_generated_trigger_sets_cover_every_synced_table():
    tables = synced_tables()

    assert len(tables) == 46
    assert len(expected_postgres_trigger_names()) == 46
    assert len(expected_postgres_function_names()) == 46
    assert len(expected_sqlite_trigger_names()) == 138
    assert {table for _name, (table, _sql) in sqlite_trigger_sql().items()} == set(
        tables
    )


# 四条**手写**的触发器全文, 每条覆盖一种 scope/形态。它们是独立于生成器的锚:
# 上面的用例都拿生成器的输出去比对别处, 所以生成器自己改错了(键列少一列、
# notebook 读成了 parent、门丢了)那些用例会跟着一起变、一起绿。这四条不会 ——
# 改生成器就必须回到这里逐字确认, 这正是要求的人类复核动作。
_HANDWRITTEN_SQLITE_TRIGGERS = {
    # NOTEBOOK scope, insert: notebook 直接读行上的 notebook_id, parent 为 NULL。
    "sync_capture_chunks_insert": (
        'CREATE TRIGGER "sync_capture_chunks_insert" AFTER INSERT ON "chunks" '
        "BEGIN INSERT INTO sync_change_log (table_name, key_json, operation, "
        "parent_key, notebook_id, changed_at) SELECT 'chunks', "
        "json_object('id', NEW.\"id\"), 'upsert', NULL, NEW.\"notebook_id\", "
        "strftime('%Y-%m-%dT%H:%M:%f','now') || '000+00:00' WHERE EXISTS "
        "(SELECT 1 FROM sync_capture_control WHERE singleton = 1 AND enabled = 1); END"
    ),
    # PARENT scope, delete: parent_key 记父行 id, notebook_id 留空(导出时再解析)。
    "sync_capture_source_elements_delete": (
        'CREATE TRIGGER "sync_capture_source_elements_delete" AFTER DELETE ON '
        '"source_elements" BEGIN INSERT INTO sync_change_log (table_name, '
        "key_json, operation, parent_key, notebook_id, changed_at) SELECT "
        "'source_elements', json_object('id', OLD.\"id\"), 'delete', "
        "OLD.\"source_id\", NULL, strftime('%Y-%m-%dT%H:%M:%f','now') || "
        "'000+00:00' WHERE EXISTS (SELECT 1 FROM sync_capture_control WHERE "
        "singleton = 1 AND enabled = 1); END"
    ),
    # GLOBAL scope, update: 两列都空; 键变化时先记 OLD 的 delete 再记 upsert。
    "sync_capture_groups_update": (
        'CREATE TRIGGER "sync_capture_groups_update" AFTER UPDATE ON "groups" '
        "BEGIN INSERT INTO sync_change_log (table_name, key_json, operation, "
        "parent_key, notebook_id, changed_at) SELECT 'groups', "
        "json_object('id', OLD.\"id\"), 'delete', NULL, NULL, "
        "strftime('%Y-%m-%dT%H:%M:%f','now') || '000+00:00' WHERE EXISTS "
        "(SELECT 1 FROM sync_capture_control WHERE singleton = 1 AND enabled = 1) "
        'AND (OLD."id" IS NOT NEW."id"); INSERT INTO sync_change_log (table_name, '
        "key_json, operation, parent_key, notebook_id, changed_at) SELECT "
        "'groups', json_object('id', NEW.\"id\"), 'upsert', NULL, NULL, "
        "strftime('%Y-%m-%dT%H:%M:%f','now') || '000+00:00' WHERE EXISTS "
        "(SELECT 1 FROM sync_capture_control WHERE singleton = 1 AND enabled = 1); END"
    ),
    # unified_kg_state update: 第三条语句是 kg_epoch, 只在 kg_reset_epoch 变化时记。
    "sync_capture_unified_kg_state_update": (
        'CREATE TRIGGER "sync_capture_unified_kg_state_update" AFTER UPDATE ON '
        '"unified_kg_state" BEGIN INSERT INTO sync_change_log (table_name, '
        "key_json, operation, parent_key, notebook_id, changed_at) SELECT "
        "'unified_kg_state', json_object('notebook_id', OLD.\"notebook_id\"), "
        "'delete', NULL, OLD.\"notebook_id\", "
        "strftime('%Y-%m-%dT%H:%M:%f','now') || '000+00:00' WHERE EXISTS "
        "(SELECT 1 FROM sync_capture_control WHERE singleton = 1 AND enabled = 1) "
        'AND (OLD."notebook_id" IS NOT NEW."notebook_id"); '
        "INSERT INTO sync_change_log (table_name, key_json, operation, "
        "parent_key, notebook_id, changed_at) SELECT 'unified_kg_state', "
        "json_object('notebook_id', NEW.\"notebook_id\"), 'upsert', NULL, "
        "NEW.\"notebook_id\", strftime('%Y-%m-%dT%H:%M:%f','now') || '000+00:00' "
        "WHERE EXISTS (SELECT 1 FROM sync_capture_control WHERE singleton = 1 "
        "AND enabled = 1); INSERT INTO sync_change_log (table_name, key_json, "
        "operation, parent_key, notebook_id, changed_at) SELECT "
        "'unified_kg_state', json_object('notebook_id', NEW.\"notebook_id\"), "
        "'kg_epoch', NULL, NEW.\"notebook_id\", "
        "strftime('%Y-%m-%dT%H:%M:%f','now') || '000+00:00' WHERE EXISTS "
        "(SELECT 1 FROM sync_capture_control WHERE singleton = 1 AND enabled = 1) "
        'AND (OLD."kg_reset_epoch" IS NOT NEW."kg_reset_epoch"); END'
    ),
}


@pytest.mark.parametrize("name", sorted(_HANDWRITTEN_SQLITE_TRIGGERS))
def test_the_generator_matches_the_handwritten_trigger_text(name):
    generated = sqlite_trigger_sql()[name][1]

    assert generated == _HANDWRITTEN_SQLITE_TRIGGERS[name]


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
    # changed_at 与库里其它时间戳同形 (_now(): 微秒 + "+00:00"), 不是自成一体的
    # "…Z"——同一张表里两种时间形态, 文本比较与排序就会在边界上骗人。
    for row in rows:
        assert _TIMESTAMP.fullmatch(row["changed_at"]), row["changed_at"]
        assert datetime.fromisoformat(row["changed_at"]).tzinfo is not None


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


def test_the_idempotent_write_still_refuses_a_broken_row(repo):
    """``ON CONFLICT(object_id, source_id) DO NOTHING`` 只吞那一个冲突。换成
    ``INSERT OR IGNORE`` 的话, NOT NULL 违例也会被静静吞掉 —— 那正是把坏行变成
    「什么都没发生」的写法。"""
    notebook_id = _seed_notebook(repo)

    with pytest.raises(sqlite3.IntegrityError):
        with repo._write() as db:
            KnowledgeStore.insert_object_source_rows(db, [(None, "s-1", notebook_id)])
