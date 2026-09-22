"""守卫: 源端变更捕获 (app.migration.sync.capture) 的 PostgreSQL 泳道。

SQLite 泳道 (tests/test_sync_capture.py) 已经钉住生成器文本、行身份与触发器
语义。这里只覆盖**按后端会分叉**的那一段:

- 每张表一份 plpgsql 函数 ``sync_capture_<table>``, 键列写死在函数体里, 绝不
  ``to_jsonb(NEW)`` 整行物化;
- ``txid`` 真的被写进去 (SQLite 没有可记的事务 id, 那边恒为 NULL) —— 增量导出
  靠它区分「已提交」与「快照时仍在途」的变更;
- 0064 的去重 + ``ALTER TABLE ... ADD PRIMARY KEY`` 在已有重复行的库上必须先
  去重再加键;
- ``app.migration.shadow.postgres_catalog`` 对业务表非内部触发器的**恰好等于**
  判定: 多一条、少一条、换函数都必须报 drift(变异验证就在下面三条用例里)。
"""

from __future__ import annotations

import json

import pytest

from app.migration.sync.capture import (
    expected_postgres_functions,
    expected_postgres_trigger_names,
    expected_postgres_triggers,
    postgres_function_body,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_sync_capture"),
]

NOW = "2026-09-23T00:00:00+00:00"


@pytest.fixture
def migrated(postgres_database, postgres_scope):
    from app.repositories.postgres.migrator import PostgresMigrator

    PostgresMigrator(postgres_database).migrate()
    return postgres_database


def _enable_capture(database) -> None:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sync_capture_control (singleton, enabled, enabled_at) "
            "VALUES (1, true, now())"
        )


def _log(database) -> list[dict]:
    with database.connect() as conn:
        return [
            {
                "table_name": row["table_name"],
                "key": json.loads(row["key_json"]) if isinstance(row["key_json"], str)
                else row["key_json"],
                "operation": row["operation"],
                "parent_key": row["parent_key"],
                "notebook_id": row["notebook_id"],
                "txid": row["txid"],
            }
            for row in conn.execute(
                "SELECT table_name, key_json, operation, parent_key, notebook_id, "
                "txid FROM sync_change_log ORDER BY seq"
            ).fetchall()
        ]


def _seed_notebook(database, notebook_id: str = "nb-1") -> str:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES (%s,%s,%s,%s)",
            (notebook_id, "n", NOW, NOW),
        )
    return notebook_id


def _seed_source(database, notebook_id: str, source_id: str = "src-1") -> str:
    with database.write() as conn:
        conn.execute(
            "INSERT INTO sources (id, notebook_id, title, source_type, "
            "created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (source_id, notebook_id, "t", "md", NOW, NOW),
        )
    return source_id


# ------------------------------------------------------------- 安装结果


def test_the_migration_installs_the_generated_functions_and_triggers(migrated):
    with migrated.connect() as conn:
        functions = {
            row["proname"]: row
            for row in conn.execute(
                "SELECT p.proname, p.prokind, p.prosecdef, l.lanname, p.prosrc "
                "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "JOIN pg_language l ON l.oid=p.prolang "
                "WHERE n.nspname=current_schema() AND p.proname LIKE 'sync\\_capture%'"
            ).fetchall()
        }
        triggers = {
            row["tgname"]: row["table_name"]
            for row in conn.execute(
                "SELECT g.tgname, t.relname AS table_name FROM pg_trigger g "
                "JOIN pg_class t ON t.oid=g.tgrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace "
                "WHERE n.nspname=current_schema() AND NOT g.tgisinternal"
            ).fetchall()
        }

    expected_functions = expected_postgres_functions()
    assert set(functions) == set(expected_functions)
    assert len(functions) == 46
    assert triggers == expected_postgres_triggers()
    assert len(triggers) == 46
    for name, row in functions.items():
        assert row["prokind"] == "f"
        assert row["prosecdef"] is False
        assert row["lanname"] == "plpgsql"
        # prosrc 就是生成器的函数体, 一字不差 —— 守卫比对的也是这份文本。
        assert row["prosrc"] == postgres_function_body(expected_functions[name])


def test_no_installed_function_materializes_a_whole_row(migrated):
    """行级捕获绝不整行序列化: chunk_embeddings/knowledge_embeddings 的 bytea
    向量、chunks.text 不该因为「记了一次变更」就被读出来一遍。"""
    with migrated.connect() as conn:
        sources = [
            row["prosrc"]
            for row in conn.execute(
                "SELECT p.prosrc FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=current_schema() AND p.proname LIKE 'sync\\_capture%'"
            ).fetchall()
        ]

    assert len(sources) == 46
    for source in sources:
        assert "to_jsonb(" not in source
        assert "row_to_json(" not in source
        assert "TG_ARGV" not in source


def test_the_migration_takes_the_two_composite_primary_keys(migrated):
    with migrated.connect() as conn:
        keys = {
            row["table_name"]: list(row["columns"])
            for row in conn.execute(
                "SELECT tc.table_name, array_agg(kcu.column_name::text ORDER BY "
                "kcu.ordinal_position) AS columns "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "ON kcu.constraint_name=tc.constraint_name "
                "AND kcu.table_schema=tc.table_schema "
                "WHERE tc.constraint_type='PRIMARY KEY' "
                "AND tc.table_schema=current_schema() AND tc.table_name IN "
                "('knowledge_object_sources','community_members',"
                "'sync_capture_control','sync_change_log') "
                "GROUP BY tc.table_name"
            ).fetchall()
        }

    assert keys == {
        "knowledge_object_sources": ["object_id", "source_id"],
        "community_members": ["community_id", "canonical_id"],
        "sync_capture_control": ["singleton"],
        "sync_change_log": ["seq"],
    }


def test_the_migration_seeds_no_control_row(migrated):
    with migrated.connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM sync_capture_control"
        ).fetchone()["n"] == 0


def test_the_change_log_sequence_does_not_cache(migrated):
    """``sync_change_log.seq`` 的序列必须是 ``CACHE 1``(IDENTITY 的默认值)。

    **这条不是增量窗口的正确性前提**,别把它当成那个来读。窗口的正确性完全来自
    快照可见性:补偿查询捞的是「在 S_prev 里不可见」的日志行,而 PostgreSQL 的
    可见性规则保证这样的行必然满足 ``txid >= pg_snapshot_xmin(S_prev)``——
    ``xmin`` 按定义就是 S_prev 拍下那一刻最老的在途事务号,比它更老的事务当时
    要么已提交(于是可见)、要么已回滚。所以补偿窗口的下界与序列怎么发号无关。

    反过来也说明 ``CACHE 1`` 推不出「seq 序 ⇒ txid 序」:xid 是事务**首次写**时
    分配的,seq 是触发器**插入日志行**时分配的,两者本就可以任意交错——一个早就
    拿到小 xid 的长事务完全可以在很久以后才插日志、拿到大 seq。真要靠 seq 顺序
    去推提交顺序,在任何 CACHE 值下都是错的。

    那这条钉子钉的是什么:``CACHE n > 1`` 让每个会话预取一段号留在本地,于是发号
    顺序不再随时间单调,``from_seq``/``to_seq``、包目录名和 ``sync_change_log``
    按 seq 排出来的顺序对运维就不再可读(一段号会凭空跳过,较小的号会晚很多才
    出现)。保持 ``CACHE 1`` 让这些数字仍然是「按发放时间单调」的,是纵深防御与
    可运维性,不是算法依赖。

    序列名不写死:用 ``pg_get_serial_sequence`` 从列反查,这样即使 identity
    序列改名,这条断言仍然钉在同一个对象上。
    """
    with migrated.connect() as conn:
        row = conn.execute(
            "SELECT s.cache_size FROM pg_sequences s "
            "WHERE s.schemaname = current_schema() AND s.sequencename = ("
            "  SELECT c.relname FROM pg_class c "
            "  WHERE c.oid = pg_get_serial_sequence('sync_change_log', 'seq')::regclass"
            ")"
        ).fetchone()
    assert row is not None, "sync_change_log.seq 没有可反查的序列"
    assert row["cache_size"] == 1


# ------------------------------------------------------------ 触发器行为


def test_a_closed_gate_logs_nothing(migrated):
    notebook_id = _seed_notebook(migrated)
    _seed_source(migrated, notebook_id)
    with migrated.write() as conn:
        conn.execute("UPDATE sources SET title='t2' WHERE id='src-1'")
        conn.execute("DELETE FROM sources WHERE id='src-1'")

    assert _log(migrated) == []


def test_an_open_gate_logs_operations_with_a_transaction_id(migrated):
    notebook_id = _seed_notebook(migrated)
    _enable_capture(migrated)
    _seed_source(migrated, notebook_id)
    with migrated.write() as conn:
        conn.execute("UPDATE sources SET title='t2' WHERE id='src-1'")
        conn.execute("DELETE FROM sources WHERE id='src-1'")

    rows = [row for row in _log(migrated) if row["table_name"] == "sources"]
    assert [row["operation"] for row in rows] == ["upsert", "upsert", "delete"]
    assert all(row["key"] == {"id": "src-1"} for row in rows)
    assert all(row["notebook_id"] == notebook_id for row in rows)
    assert all(row["parent_key"] is None for row in rows)
    # 这一列正是 SQLite 端恒为 NULL 的那一列。
    assert all(isinstance(row["txid"], int) and row["txid"] > 0 for row in rows)


def test_a_key_change_logs_the_old_identity_as_a_delete(migrated):
    notebook_id = _seed_notebook(migrated)
    _seed_source(migrated, notebook_id)
    _enable_capture(migrated)
    with migrated.write() as conn:
        conn.execute("UPDATE sources SET id='src-2' WHERE id='src-1'")

    rows = [row for row in _log(migrated) if row["table_name"] == "sources"]
    assert [(row["operation"], row["key"]["id"]) for row in rows] == [
        ("delete", "src-1"),
        ("upsert", "src-2"),
    ]


def test_a_parent_scoped_table_logs_its_parent_not_a_notebook(migrated):
    notebook_id = _seed_notebook(migrated)
    source_id = _seed_source(migrated, notebook_id)
    _enable_capture(migrated)
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO source_elements (id, source_id, element_type, "
            "location_label, text, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            ("el-1", source_id, "formula", "p1", "x", NOW),
        )

    rows = [row for row in _log(migrated) if row["table_name"] == "source_elements"]
    assert len(rows) == 1
    assert rows[0]["parent_key"] == source_id
    assert rows[0]["notebook_id"] is None


def test_a_global_table_logs_neither_notebook_nor_parent(migrated):
    _enable_capture(migrated)
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO object_schemas (object_type, created_at, updated_at) "
            "VALUES (%s,%s,%s)",
            ("custom_type", NOW, NOW),
        )

    rows = [row for row in _log(migrated) if row["table_name"] == "object_schemas"]
    assert len(rows) == 1
    assert rows[0]["parent_key"] is None
    assert rows[0]["notebook_id"] is None
    assert rows[0]["key"] == {"object_type": "custom_type"}


def test_a_kg_reset_logs_an_extra_epoch_event(migrated):
    notebook_id = _seed_notebook(migrated)
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO unified_kg_state (notebook_id, updated_at) VALUES (%s,%s)",
            (notebook_id, NOW),
        )
    _enable_capture(migrated)
    with migrated.write() as conn:
        conn.execute(
            "UPDATE unified_kg_state SET kg_reset_epoch = 1 WHERE notebook_id = %s",
            (notebook_id,),
        )
        conn.execute(
            "UPDATE unified_kg_state SET updated_at = %s WHERE notebook_id = %s",
            ("2026-09-23T01:00:00+00:00", notebook_id),
        )

    rows = [row for row in _log(migrated) if row["table_name"] == "unified_kg_state"]
    assert [row["operation"] for row in rows] == ["upsert", "kg_epoch", "upsert"]


def test_a_keyless_table_logs_its_registered_composite_key(migrated):
    notebook_id = _seed_notebook(migrated)
    _enable_capture(migrated)
    with migrated.write() as conn:
        conn.execute(
            "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
            "VALUES (%s,%s,%s)",
            ("o-1", "s-1", notebook_id),
        )

    rows = [
        row for row in _log(migrated) if row["table_name"] == "knowledge_object_sources"
    ]
    assert len(rows) == 1
    assert rows[0]["key"] == {"object_id": "o-1", "source_id": "s-1"}


# -------------------------------------------------------------- 去重迁移


def test_the_migration_collapses_duplicate_rows_before_taking_the_key(
    postgres_database,
):
    """迁到 0063 (两张表还没有主键), 插重复行, 再迁到 0064: 必须先按复合键去重
    再 ADD PRIMARY KEY, 否则升级在加主键那一步硬失败。"""
    from app.repositories.postgres.migrator import PostgresMigrator

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate(63) == 63
    with postgres_database.write() as conn:
        conn.execute(
            "INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id) "
            "VALUES ('o-1','s-1','nb-1'), ('o-1','s-1','nb-1'), ('o-2','s-1','nb-1')"
        )
        conn.execute(
            "INSERT INTO community_members (canonical_id, notebook_id, level, "
            "community_id, canonical_name, centrality, generation) VALUES "
            "('c-1','nb-1',0,'com-1','a',0.0,1), "
            "('c-1','nb-1',0,'com-1','a',0.0,1), "
            "('c-2','nb-1',0,'com-1','b',0.0,1)"
        )

    assert migrator.migrate() == 65

    with postgres_database.connect() as conn:
        kos = conn.execute(
            "SELECT object_id, source_id FROM knowledge_object_sources "
            "ORDER BY object_id"
        ).fetchall()
        members = conn.execute(
            "SELECT community_id, canonical_id FROM community_members "
            "ORDER BY canonical_id"
        ).fetchall()
        keys = {
            row["conname"]
            for row in conn.execute(
                "SELECT c.conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid=c.conrelid "
                "JOIN pg_namespace n ON n.oid=t.relnamespace "
                "WHERE n.nspname=current_schema() AND c.contype='p' "
                "AND t.relname IN ('knowledge_object_sources','community_members')"
            ).fetchall()
        }

    assert [(row["object_id"], row["source_id"]) for row in kos] == [
        ("o-1", "s-1"), ("o-2", "s-1"),
    ]
    assert [(row["community_id"], row["canonical_id"]) for row in members] == [
        ("com-1", "c-1"), ("com-1", "c-2"),
    ]
    assert keys == {"pk_knowledge_object_sources", "pk_community_members"}


# ------------------------------------------- catalog 守卫的变异验证


def _catalog_tables() -> tuple[str, ...]:
    from app.migration.shadow.manifest import MANIFEST

    return ("silicon_schema_migrations", *MANIFEST.application_names)


def _validate(database) -> None:
    """只跑触发器那一段守卫。完整的 ``validate_postgres_business_catalog``
    还要求 shadow 自己的唯一索引 (``shadow_uq_*``) 已经装好, 那是 shadow 迁移
    运行期才有的东西, 一个刚跑完 packaged migrations 的 schema 上并不存在。"""
    from app.migration.shadow.postgres_catalog import _validate_capture_triggers

    with database.connect() as conn:
        schema = conn.execute(
            "SELECT current_schema() AS schema"
        ).fetchone()["schema"]
        _validate_capture_triggers(
            conn, business_schema=schema, catalog_tables=_catalog_tables()
        )


def test_the_catalog_guard_accepts_the_migrated_trigger_set(migrated):
    _validate(migrated)


def test_the_catalog_guard_rejects_a_foreign_trigger(migrated):
    """变异: 业务表上多一条不属于捕获集合的触发器。"""
    with migrated.write() as conn:
        conn.execute(
            "CREATE TRIGGER audit_hook AFTER INSERT ON sources "
            "FOR EACH ROW EXECUTE FUNCTION sync_capture_sources()"
        )

    with pytest.raises(ValueError, match="trigger set drifted"):
        _validate(migrated)


def test_the_catalog_guard_rejects_a_dropped_capture_trigger(migrated):
    """变异: 少一条 —— 那张表会悄悄停止被捕获, 下一次增量导出漏掉它的变更。"""
    name = sorted(expected_postgres_trigger_names())[0]
    table = expected_postgres_triggers()[name]
    with migrated.write() as conn:
        conn.execute(f"DROP TRIGGER {name} ON {table}")

    with pytest.raises(ValueError, match="trigger set drifted"):
        _validate(migrated)


def test_the_catalog_guard_rejects_a_repointed_capture_trigger(migrated):
    """变异: 名字与所在表都对, 但指向另一个函数。"""
    name = sorted(expected_postgres_trigger_names())[0]
    table = expected_postgres_triggers()[name]
    with migrated.write() as conn:
        conn.execute(
            "CREATE FUNCTION impostor_capture() RETURNS trigger LANGUAGE plpgsql "
            "AS $impostor$ BEGIN RETURN NULL; END; $impostor$"
        )
        conn.execute(f"DROP TRIGGER {name} ON {table}")
        conn.execute(
            f"CREATE TRIGGER {name} AFTER INSERT OR UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION impostor_capture()"
        )

    with pytest.raises(ValueError, match="trigger definition drifted"):
        _validate(migrated)


def test_the_catalog_guard_rejects_a_disabled_capture_trigger(migrated):
    """变异: 触发器还在, 但被 ALTER TABLE ... DISABLE TRIGGER 关掉 —— 对捕获
    来说与删掉等价, 所以也必须报 drift。"""
    name = sorted(expected_postgres_trigger_names())[0]
    table = expected_postgres_triggers()[name]
    with migrated.write() as conn:
        conn.execute(f"ALTER TABLE {table} DISABLE TRIGGER {name}")

    with pytest.raises(ValueError, match="trigger definition drifted"):
        _validate(migrated)


def test_the_catalog_guard_rejects_an_edited_function_body(migrated):
    """变异(内容维度): 名字、所在表、指向的函数全都不变, 只把函数体里记进
    key_json 的键列换掉。名字维度看不见这种改动 —— 但它会让日志记下一个不是
    行身份的东西, 增量导入据此匹配不到任何行。"""
    body = postgres_function_body("sources").replace(
        "jsonb_build_object('id', NEW.\"id\")",
        "jsonb_build_object('id', NEW.\"title\")",
    )
    assert "NEW.\"title\"" in body  # 变异真的落进去了
    with migrated.write() as conn:
        conn.execute(
            "CREATE OR REPLACE FUNCTION sync_capture_sources() RETURNS trigger "
            f"LANGUAGE plpgsql AS $edited${body}$edited$"
        )

    with pytest.raises(ValueError, match="capture function definition drifted"):
        _validate(migrated)


def test_the_catalog_guard_rejects_a_narrowed_trigger_event_set(migrated):
    """变异(内容维度): 触发器名字、所在表、函数都对, 但只在 INSERT 上触发 ——
    那张表的 update/delete 从此不进日志, 目标端永远看不到改名与删除。"""
    with migrated.write() as conn:
        conn.execute("DROP TRIGGER sync_capture_sources ON sources")
        conn.execute(
            "CREATE TRIGGER sync_capture_sources AFTER INSERT ON sources "
            "FOR EACH ROW EXECUTE FUNCTION sync_capture_sources()"
        )

    with pytest.raises(ValueError, match="trigger definition drifted"):
        _validate(migrated)


def test_the_catalog_guard_rejects_a_dropped_capture_function(migrated):
    """变异: 删掉一个捕获函数。DROP FUNCTION 必须 CASCADE(触发器依赖它), 所以
    先报的是触发器集合那条 —— 这里钉的是「函数消失不会被当成无事发生」, 两个
    维度里哪一个先开口不重要。"""
    with migrated.write() as conn:
        conn.execute("DROP FUNCTION sync_capture_sources() CASCADE")

    with pytest.raises(ValueError, match="drifted"):
        _validate(migrated)
