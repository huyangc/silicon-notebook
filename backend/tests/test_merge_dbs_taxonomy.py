"""单元测试:`scripts/merge_dbs.py` 的表分类守卫(部署侧运维脚本,不属于 app 包)。

按文件路径直接 import(同 test_mineru_probe.py)。只测 `assert_taxonomy_complete`:
对当前 SCHEMA_VERSION 的全新迁移库跑一遍,钉住"库里每张业务表/FTS 虚表都必须被
显式归类"这条不变量——下次再加新表却忘了登记分类清单,这条测试就会红,而不是
让 merge_dbs.py 在生产合并时才发现漏拷数据。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_SCRIPT_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "merge_dbs.py"
)
_spec = importlib.util.spec_from_file_location("merge_dbs", _SCRIPT_PATH)
merge_dbs = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["merge_dbs"] = merge_dbs
_spec.loader.exec_module(merge_dbs)


@pytest.fixture
def fresh_db(tmp_path):
    """迁到当前 SCHEMA_VERSION 的全新 SQLite 库(只 migrate, 不 seed)。"""
    db_path = tmp_path / "fresh.db"
    merge_dbs.migrate_to_current(db_path)
    conn = sqlite3.connect(str(db_path))
    try:
        yield conn
    finally:
        conn.close()


def test_assert_taxonomy_complete_passes_for_fresh_schema(fresh_db):
    """全新迁移库的每张表都已被某个分类清单收纳 —— 不应该抛。"""
    merge_dbs.assert_taxonomy_complete(fresh_db)  # 不抛即通过


def test_assert_taxonomy_complete_flags_unclassified_command_catalog_tables(
    fresh_db, monkeypatch
):
    """变异验证:把 catalog_jobs / catalog_candidates 从分类清单里删掉,
    守卫必须 fail-loud(SystemExit),而不是静默放行未归类的表。"""
    monkeypatch.setattr(
        merge_dbs,
        "SKIP_SECONDARY_TABLES",
        [
            t
            for t in merge_dbs.SKIP_SECONDARY_TABLES
            if t not in ("catalog_jobs", "catalog_candidates")
        ],
    )
    with pytest.raises(SystemExit) as exc_info:
        merge_dbs.assert_taxonomy_complete(fresh_db)
    message = str(exc_info.value)
    assert "catalog_jobs" in message
    assert "catalog_candidates" in message


def _schema_db(definition: tuple[str, ...]) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE object_schemas ("
        "object_type TEXT PRIMARY KEY, notebook_id TEXT NOT NULL DEFAULT '', "
        "plural TEXT, fields TEXT, primary_field TEXT, description TEXT, "
        "label TEXT, list_fields TEXT, source TEXT, status TEXT, rationale TEXT)"
    )
    db.execute(
        "INSERT INTO object_schemas VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        definition,
    )
    return db


def test_global_schema_union_rejects_same_name_with_different_semantics():
    base = (
        "claim", "", "claims", '["statement"]', "statement", "desc",
        "Claim", "[]", "builtin", "active", "",
    )
    left = _schema_db(base)
    right = _schema_db((*base[:6], "Different", *base[7:]))
    try:
        with pytest.raises(SystemExit, match="claim"):
            merge_dbs._assert_global_schema_compatibility(left, right)
    finally:
        left.close()
        right.close()


def test_global_schema_union_accepts_semantically_identical_rows():
    definition = (
        "claim", "", "claims", '["statement"]', "statement", "desc",
        "Claim", "[]", "builtin", "active", "",
    )
    left = _schema_db(definition)
    right = _schema_db(definition)
    try:
        merge_dbs._assert_global_schema_compatibility(left, right)
    finally:
        left.close()
        right.close()


# ---------------------------------------------------------------------------
# 孤儿群组授权边清扫(群组知识共享 P1,已定裁决 1c 的审计承诺)
# ---------------------------------------------------------------------------


def _seed_grant_world(conn: sqlite3.Connection) -> None:
    """一个用户 + 一本笔记本,供下面的授权边用(两张表都有外键指向它们)。"""
    now = "2026-08-18T00:00:00+00:00"
    conn.execute(
        "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
        "VALUES ('u1','u1@t','U1','user','active','u00000001',?,?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at) VALUES ('nb1','NB','','Semiconductor','draft','u1',?,?)",
        (now, now),
    )
    conn.commit()


def _add_group(conn: sqlite3.Connection, group_id: str) -> None:
    now = "2026-08-18T00:00:00+00:00"
    conn.execute(
        "INSERT INTO groups (id,name,kind,description,created_by,created_at,updated_at) "
        "VALUES (?,?, 'project','', 'u1', ?, ?)",
        (group_id, group_id, now, now),
    )
    conn.commit()


def _add_grant(
    conn: sqlite3.Connection,
    grant_id: str,
    principal_type: str,
    principal_id: str,
) -> None:
    conn.execute(
        "INSERT INTO notebook_grants "
        "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
        "VALUES (?, 'nb1', ?, ?, 'viewer', 'u1', '2026-08-18T00:00:00+00:00')",
        (grant_id, principal_type, principal_id),
    )
    conn.commit()


def test_orphan_group_grants_are_swept_but_live_ones_survive(fresh_db):
    """合并后清扫:指向已不存在群组的边被清掉,组还在的边一条不动。

    合并是这类孤儿边**唯一**的来源:平时删组走的是同一个写事务(先清边、再删组),
    而合并把 `notebook_grants` 按 notebook 范围导入、把 `groups` 按并集导入,两者
    口径不同就会对不上。数据库替不了这件事——`principal_id` 是无外键的多态列,
    `PRAGMA foreign_key_check` 永远看不见这类行。
    """
    _seed_grant_world(fresh_db)
    _add_group(fresh_db, "grp-alive")
    _add_grant(fresh_db, "g-live-viewer", "group", "grp-alive")
    _add_grant(fresh_db, "g-live-admins", "group_admins", "grp-alive")
    _add_grant(fresh_db, "g-orphan-viewer", "group", "grp-gone")
    _add_grant(fresh_db, "g-orphan-admins", "group_admins", "grp-gone")

    assert merge_dbs.sweep_orphan_group_grants(fresh_db) == 2
    fresh_db.commit()
    survivors = {
        row[0]
        for row in fresh_db.execute("SELECT id FROM notebook_grants").fetchall()
    }
    assert survivors == {"g-live-viewer", "g-live-admins"}
    # 幂等:再跑一次没有可清的行。
    assert merge_dbs.sweep_orphan_group_grants(fresh_db) == 0


def test_orphan_sweep_never_touches_user_or_everyone_principals(fresh_db):
    """`user` / `everyone` 主体的 `principal_id` 根本不指向 `groups`。

    把它们一起扫掉就是**删掉两类完全正常的授权**:`everyone` 存的是空串,`user` 存
    的是用户 id,两者拿去 `groups` 里查当然都查不到。判据必须只认两个群组主体。
    """
    _seed_grant_world(fresh_db)
    _add_grant(fresh_db, "g-user", "user", "u1")
    _add_grant(fresh_db, "g-everyone", "everyone", "")
    _add_grant(fresh_db, "g-orphan", "group", "grp-gone")

    assert merge_dbs.sweep_orphan_group_grants(fresh_db) == 1
    fresh_db.commit()
    survivors = {
        row[0]
        for row in fresh_db.execute("SELECT id FROM notebook_grants").fetchall()
    }
    assert survivors == {"g-user", "g-everyone"}


def test_orphan_sweep_keeps_edges_whose_group_survived_the_union(fresh_db):
    """合库语义的正例:副库那本笔记本的边导进来了,而它指向的组也在并集里 → 保留。

    这条与上面那条一起构成裁决 1c 要的两种情形:「主库孤儿边 + 副库同组存活 → 保留」
    与「两侧都没组 → 清掉」。`groups` 走 GLOBAL_UNION(主库优先并集),所以「组从副库
    来、边从主库来」是完全正常的终态,绝不能被扫掉。
    """
    _seed_grant_world(fresh_db)
    _add_grant(fresh_db, "g-from-primary", "group", "grp-from-secondary")
    assert merge_dbs.sweep_orphan_group_grants(fresh_db) == 1  # 组还没并进来:是孤儿

    fresh_db.execute("DELETE FROM notebook_grants")
    _add_group(fresh_db, "grp-from-secondary")  # GLOBAL_UNION 把副库的组并了进来
    _add_grant(fresh_db, "g-from-primary", "group", "grp-from-secondary")
    assert merge_dbs.sweep_orphan_group_grants(fresh_db) == 0
    fresh_db.commit()
    assert fresh_db.execute(
        "SELECT COUNT(*) FROM notebook_grants"
    ).fetchone()[0] == 1


def test_post_union_eviction_recaps_the_experience_library(fresh_db):
    """codex #524 R1 P2:两个各自合法 300 行的库并出最多 600 行,普通读者只按
    id 序取前 300、下一次蒸馏(可能没配模型)之前无人淘汰——并集后必须立刻按
    运行时同一淘汰序收容,且删的是 (adopted, support, updated_at, id) 升序的
    最低价值行。"""
    conn = fresh_db
    for i in range(310):
        conn.execute(
            "INSERT INTO retrieval_experiences"
            "(id, situation_json, action, polarity, rationale, support,"
            " adopted, provenance_json, created_at, updated_at)"
            " VALUES (?, '{}', 'ppr', 'bad', '', ?, ?, '[]',"
            " '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
            (f"rx-{i:04d}", i, i % 3),
        )
    conn.commit()
    evicted = merge_dbs._evict_experiences_to_limit(conn)
    assert evicted == 10
    kept = conn.execute(
        "SELECT COUNT(*) FROM retrieval_experiences"
    ).fetchone()[0]
    assert kept == 300
    # 淘汰序:adopted 升序优先——adopted=0 且 support 最低的先走
    survivors_min = conn.execute(
        "SELECT MIN(support) FROM retrieval_experiences WHERE adopted = 0"
    ).fetchone()[0]
    dropped_probe = conn.execute(
        "SELECT COUNT(*) FROM retrieval_experiences WHERE id = 'rx-0000'"
    ).fetchone()[0]
    assert dropped_probe == 0, "adopted=0/support=0 的最低价值行必须被删"
    assert survivors_min is not None


def test_merge_core_actually_wires_the_post_union_eviction():
    """接线守卫:上面那条测的是 helper 本身,直接调用绕过了 merge_core——把
    调用点删掉它照样绿(变异实测)。这里按源码钉住 merge_core 真的在 GLOBAL_UNION
    之后调 _evict_experiences_to_limit。"""
    source = _SCRIPT_PATH.read_text(encoding="utf-8")
    core = source[source.index("def merge_core"):]
    union_at = core.index("GLOBAL_UNION_TABLES:")
    evict_at = core.index("_evict_experiences_to_limit(conn)")
    assert evict_at > union_at, "收容必须发生在 GLOBAL_UNION 并集之后"


def test_the_offline_eviction_cap_matches_the_runtime_protocol_constant():
    """codex #524 R2 P2:merge_dbs 是纯 stdlib 离线脚本、刻意不 import 后端包,
    上限因此是第二份拼写——本测试把两份钉成相等,任一侧漂移即红(单一真源的
    测试化替代)。"""
    from app.repositories.ports import RETRIEVAL_EXPERIENCE_MAX_ENTRIES

    offline_default = merge_dbs._evict_experiences_to_limit.__defaults__[0]
    assert offline_default == RETRIEVAL_EXPERIENCE_MAX_ENTRIES


def test_merge_normalizes_cluster_generation_for_imported_notebooks(tmp_path):
    """批 3·W2 §1.6 pin:合库把副库簇行的 generation 原样拷入,而 KG_STATE
    清空让代次指针消失——三张派生表的导入行必须归一到 0 代,否则合并库的
    簇图对 COALESCE(指针,0) 读者整体不可见。"""
    now = "2026-01-01T00:00:00"

    def _mk(path, nb, gen):
        merge_dbs.migrate_to_current(path)
        conn = sqlite3.connect(str(path))
        with conn:
            conn.execute(
                "INSERT INTO users (id,email,display_name,role,created_at,"
                "updated_at) VALUES ('u','u@x','u','member',?,?)", (now, now))
            conn.execute(
                "INSERT INTO notebooks (id,name,purpose,primary_domain,status,"
                "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (nb, nb, "", "Semiconductor", "active", "u", now, now))
            conn.execute(
                "INSERT INTO unified_kg_state (notebook_id, cluster_generation, "
                "community_generation, updated_at) VALUES (?,?,?,?)",
                (nb, gen, gen, now))
            conn.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
                "member_object_id,canonical_name,object_type,created_at,generation) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"cc-{nb}", nb, "can", "mem", "N", "concept", now, gen))
            conn.execute(
                "INSERT INTO communities (id,notebook_id,member_ids,size,"
                "created_at,generation) VALUES (?,?,?,?,?,?)",
                (f"cm-{nb}", nb, '["mem"]', 1, now, gen))
            conn.execute(
                "INSERT INTO community_members (canonical_id,notebook_id,"
                "community_id,generation) VALUES (?,?,?,?)",
                ("can", nb, f"cm-{nb}", gen))
        conn.close()

    primary, secondary, out = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "out.db"
    _mk(primary, "nb-main", 0)
    _mk(secondary, "nb-sec", 7)  # 副库指针/行都在第 7 代

    merge_dbs.merge_core(out, primary, secondary, shared_base="nb-none")

    conn = sqlite3.connect(str(out))
    try:
        for table in ("concept_clusters", "communities", "community_members"):
            rows = conn.execute(
                f"SELECT generation FROM {table} WHERE notebook_id='nb-sec'"
            ).fetchall()
            assert rows and all(r[0] == 0 for r in rows), (
                f"{table} 导入行必须归一到 0 代: {rows}")
        # 指针行已被 KG_STATE 清空 → COALESCE→0 → 归一行可见
        visible = conn.execute(
            "SELECT COUNT(*) FROM concept_clusters WHERE notebook_id='nb-sec' "
            "AND generation = COALESCE((SELECT cluster_generation FROM "
            "unified_kg_state u WHERE u.notebook_id='nb-sec'), 0)"
        ).fetchone()[0]
        assert visible == 1
    finally:
        conn.close()
