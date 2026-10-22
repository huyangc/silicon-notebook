"""单元测试:`scripts/merge_dbs.py` 的表分类守卫(部署侧运维脚本,不属于 app 包)。

按文件路径直接 import(同 test_mineru_probe.py)。只测 `assert_taxonomy_complete`:
对当前 SCHEMA_VERSION 的全新迁移库跑一遍,钉住"库里每张业务表/FTS 虚表都必须被
显式归类"这条不变量——下次再加新表却忘了登记分类清单,这条测试就会红,而不是
让 merge_dbs.py 在生产合并时才发现漏拷数据。
"""
from __future__ import annotations

import importlib.util
import json
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


def _global_ask_record(conn, conversation_id, job_id, *, owner="u", request="request",
                       status="done", share_token=None):
    conn.execute(
        "INSERT OR IGNORE INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
        "VALUES (?,?,'User','user','active',?,'now','now')",
        (owner, f"{owner}@example.invalid", owner),
    )
    # Columns spelled out on purpose: v77 added the three share columns, and a
    # positional INSERT would break again on the next added column.
    conn.execute(
        "INSERT INTO global_ask_conversations"
        "(id,user_id,title,scope_json,submitted_via,created_at,updated_at,"
        "share_token,shared_through_at,shared_through_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (conversation_id, owner, "Original title", '{"mode":"all","notebook_ids":[]}',
         "web", "now", "now",
         share_token, "now" if share_token else None, job_id if share_token else None),
    )
    conn.execute(
        "INSERT INTO global_ask_jobs"
        "(id,conversation_id,user_id,client_request_id,request_json,status,payload_json,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (job_id, conversation_id, owner, request, '{"question":"original"}', status,
         json.dumps({"status": status, "question": "original", "response": {"answer": "preserved answer"}}), "now"),
    )
    conn.commit()


@pytest.mark.parametrize("conflict", ["none", "request", "owner", "missing_owner"])
def test_global_ask_merge_preserves_history_or_rejects_identity_conflicts(tmp_path, conflict):
    primary, secondary = tmp_path / "primary.db", tmp_path / "secondary.db"
    for path in (primary, secondary):
        merge_dbs.migrate_to_current(path)
    left, right = sqlite3.connect(primary), sqlite3.connect(secondary)
    try:
        _global_ask_record(left, "conv-main", "job-main", request="main-key")
        _global_ask_record(
            right, "conv-main" if conflict == "owner" else "conv-import", "job-import",
            owner="other" if conflict in {"owner", "missing_owner"} else "u",
            request="main-key" if conflict == "request" else "import-key", status="running",
            share_token="gshr-import",
        )
        right.close()
        left.execute("ATTACH DATABASE ? AS sec", (str(secondary),))
        if conflict != "none":
            with pytest.raises(SystemExit):
                with left:
                    merge_dbs._merge_global_ask(left)
            assert left.execute("SELECT count(*) FROM global_ask_jobs").fetchone()[0] == 1
        else:
            with left:
                merge_dbs._merge_global_ask(left)
            rows = left.execute("SELECT id,status,payload_json FROM global_ask_jobs ORDER BY id").fetchall()
            assert len(rows) == 2
            imported = next(row for row in rows if row[0] == "job-import")
            assert imported[1] == "interrupted"
            assert json.loads(imported[2])["response"]["answer"] == "preserved answer"
            # v77 share columns ride along with the conversation row, exactly
            # like conversations.share_token does — the merge reconciles two
            # deployments into one, it does not revoke one side's public links.
            assert left.execute(
                "SELECT share_token,shared_through_at,shared_through_id "
                "FROM global_ask_conversations WHERE id='conv-import'"
            ).fetchone() == ("gshr-import", "now", "job-import")
    finally:
        left.close()
        right.close()


def test_global_ask_merge_carries_an_imported_jobs_attribution_rows(tmp_path):
    """v98: an imported done job keeps its ``attributed`` flag and brings its
    ``global_ask_job_notebooks`` rows; a job id the primary already holds keeps
    the primary's rows (the primary wins), never the secondary's."""
    primary, secondary = tmp_path / "primary.db", tmp_path / "secondary.db"
    for path in (primary, secondary):
        merge_dbs.migrate_to_current(path)
    left, right = sqlite3.connect(primary), sqlite3.connect(secondary)
    try:
        _global_ask_record(left, "conv-main", "job-main", request="main-key")
        _global_ask_record(right, "conv-main", "job-main", request="main-key")
        _global_ask_record(right, "conv-import", "job-import", request="import-key")
        left.execute("UPDATE global_ask_jobs SET attributed=1 WHERE id='job-main'")
        left.execute("INSERT INTO global_ask_job_notebooks VALUES ('job-main','nb-left')")
        left.commit()
        right.execute("UPDATE global_ask_jobs SET attributed=1")
        right.executemany(
            "INSERT INTO global_ask_job_notebooks VALUES (?,?)",
            [("job-main", "nb-right"), ("job-import", "nb-a"), ("job-import", "nb-b")],
        )
        right.commit()
        right.close()
        left.execute("ATTACH DATABASE ? AS sec", (str(secondary),))
        with left:
            merge_dbs._merge_global_ask(left)
        assert left.execute(
            "SELECT job_id,notebook_id FROM global_ask_job_notebooks ORDER BY job_id,notebook_id"
        ).fetchall() == [("job-import", "nb-a"), ("job-import", "nb-b"), ("job-main", "nb-left")]
        assert left.execute(
            "SELECT attributed FROM global_ask_jobs WHERE id='job-import'"
        ).fetchone() == (1,)
    finally:
        left.close()
        right.close()


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


def test_post_union_eviction_recaps_each_partition_against_its_own_cap(fresh_db):
    """SQLite v79:收容必须**逐分区**各算各的,不是整表一刀切。

    这里刻意让每个笔记本分区的行看起来都比全局分区"更没价值"(全局那批
    adopted/support 都更高)。整表一刀切会把两个库的分区整个删光、全局分区
    一行不动,而返回的条数看上去完全合理——那正是这条用例要挡住的形态。
    """
    conn = fresh_db
    rows = (
        [("", index, 5, 5) for index in range(320)]
        + [("nb-a", index, 0, 0) for index in range(140)]
        + [("nb-b", index, 0, 0) for index in range(30)]
    )
    for partition, index, support, adopted in rows:
        conn.execute(
            "INSERT INTO retrieval_experiences"
            "(id, situation_json, action, polarity, rationale, support,"
            " adopted, provenance_json, notebook_id, created_at, updated_at)"
            " VALUES (?, '{}', 'ppr', 'bad', '', ?, ?, '[]', ?,"
            " '2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')",
            (f"rx-{partition}-{index:04d}", support, adopted, partition),
        )
    conn.commit()

    # 20 (全局 320→300) + 40 (nb-a 140→100) + 0 (nb-b 30 未超 100)
    assert merge_dbs._evict_experiences_to_limit(conn) == 60
    counts = dict(
        conn.execute(
            "SELECT notebook_id, COUNT(*) FROM retrieval_experiences "
            "GROUP BY notebook_id"
        ).fetchall()
    )
    assert counts == {"": 300, "nb-a": 100, "nb-b": 30}


def test_the_offline_notebook_cap_matches_the_runtime_protocol_constant():
    """与全局上限那条同款:离线脚本从 ports 直接取常量,这里钉住它取的确实是
    分区上限而不是又一份手抄数字。

    按**形参名**取默认值,不按 ``__defaults__`` 的下标:下标是位置,在两个同类型
    参数之间调换顺序、或在前面插一个新参数,都会让这条用例继续绿着去比另一个
    数——两个上限恰好都是 int,连类型都挡不住。
    """
    import inspect

    from app.repositories.ports import RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES

    signature = inspect.signature(merge_dbs._evict_experiences_to_limit)
    offline_default = signature.parameters["notebook_max_entries"].default
    assert offline_default == RETRIEVAL_EXPERIENCE_NOTEBOOK_MAX_ENTRIES


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
    import inspect

    from app.repositories.ports import RETRIEVAL_EXPERIENCE_MAX_ENTRIES

    signature = inspect.signature(merge_dbs._evict_experiences_to_limit)
    offline_default = signature.parameters["max_entries"].default
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
    # 内评 P1 场景:副库还躺着同一 member 的残代行(gen=6)——published-only
    # 筛选必须把它删掉;不筛选则塌 0 时撞四列唯一索引整体回滚。
    sec_conn = sqlite3.connect(str(secondary))
    with sec_conn:
        sec_conn.execute(
            "INSERT INTO concept_clusters (id,notebook_id,canonical_id,"
            "member_object_id,canonical_name,object_type,created_at,generation) "
            "VALUES ('cc-stale','nb-sec','can-old','mem','N','concept',"
            "'2026-01-01T00:00:00',6)")
    sec_conn.close()

    merge_dbs.merge_core(out, primary, secondary, shared_base="nb-none")

    conn = sqlite3.connect(str(out))
    try:
        for table in ("concept_clusters", "communities", "community_members"):
            rows = conn.execute(
                f"SELECT generation FROM {table} WHERE notebook_id='nb-sec'"
            ).fetchall()
            assert rows and all(r[0] == 0 for r in rows), (
                f"{table} 导入行必须归一到 0 代: {rows}")
        stale = conn.execute(
            "SELECT COUNT(*) FROM concept_clusters WHERE notebook_id='nb-sec'"
        ).fetchone()[0]
        assert stale == 1, "残代行(gen=6)必须被 published-only 筛选删掉"
        # 指针行已被 KG_STATE 清空 → COALESCE→0 → 归一行可见
        visible = conn.execute(
            "SELECT COUNT(*) FROM concept_clusters WHERE notebook_id='nb-sec' "
            "AND generation = COALESCE((SELECT cluster_generation FROM "
            "unified_kg_state u WHERE u.notebook_id='nb-sec'), 0)"
        ).fetchone()[0]
        assert visible == 1
    finally:
        conn.close()


def test_merge_drops_derived_layers_of_a_deleted_graph(tmp_path):
    """K1(裁决 13,规格评审 r4 P3-1):副库里删过知识图谱、还没重建的笔记本,
    状态行把 canonical_rel_seq / mention_seq 记成 -2,读者凭它挡住保留的旧
    派生行。合库清掉导入库的状态行(哨兵随之消失),所以这三张派生表的行
    必须按副库的 seq 直接丢掉;-2 以外的笔记本照常拷入。变异钉:去掉这段
    处理 → 旧行在合并库里可读,报红。社区行由既有的指针筛选处理(-1 代
    没有行,整层丢掉)。两层各看各的 seq(K1 第 6 轮,复审 P3-1):只有一层
    是 -2 的笔记本只丢那一层——列配错(提及两表按 canonical_rel_seq 判)
    报红。"""
    now = "2026-01-01T00:00:00"

    def _mk(path, notebooks):
        merge_dbs.migrate_to_current(path)
        conn = sqlite3.connect(str(path))
        with conn:
            conn.execute(
                "INSERT INTO users (id,email,display_name,role,created_at,"
                "updated_at) VALUES ('u','u@x','u','member',?,?)", (now, now))
            for nb, canonical_seq, mention_seq, community_generation in notebooks:
                conn.execute(
                    "INSERT INTO notebooks (id,name,purpose,primary_domain,status,"
                    "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (nb, nb, "", "Semiconductor", "active", "u", now, now))
                conn.execute(
                    "INSERT INTO unified_kg_state (notebook_id, canonical_rel_seq, "
                    "mention_seq, community_generation, kg_reset_epoch, updated_at) "
                    "VALUES (?,?,?,?,1,?)",
                    (nb, canonical_seq, mention_seq, community_generation, now))
                conn.execute(
                    "INSERT INTO canonical_relations (notebook_id,canonical_src,"
                    "edge_type,canonical_tgt,updated_at) VALUES (?,'K-a','supports','K-b',?)",
                    (nb, now))
                conn.execute(
                    "INSERT INTO mention_edges (notebook_id,claim_object_id,"
                    "concept_canonical_id) VALUES (?,'ko-c','K-a')", (nb,))
                conn.execute(
                    "INSERT INTO concept_comentions (notebook_id,canonical_a,"
                    "canonical_b) VALUES (?,'K-a','K-b')", (nb,))
                conn.execute(
                    "INSERT INTO communities (id,notebook_id,member_ids,size,"
                    "created_at,generation) VALUES (?,?,'[\"K-a\"]',1,?,0)",
                    (f"cm-{nb}", nb, now))
        conn.close()

    primary, secondary, out = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "out.db"
    _mk(primary, [("nb-main", 3, 3, 0)])
    _mk(secondary, [("nb-deleted", -2, -2, -1), ("nb-live", 4, 4, 0),
                    # one layer rebuilt since the delete, the other not
                    ("nb-canonical-only", 5, -2, 0),
                    ("nb-mention-only", -2, 5, 0)])
    merge_dbs.merge_core(out, primary, secondary, shared_base="nb-none")

    conn = sqlite3.connect(str(out))
    try:
        def _count(table, nb):
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE notebook_id=?", (nb,)
            ).fetchone()[0]

        for table in ("canonical_relations", "mention_edges",
                      "concept_comentions", "communities"):
            assert _count(table, "nb-deleted") == 0, table
            assert _count(table, "nb-live") == 1, table
            assert _count(table, "nb-main") == 1, table
        assert _count("canonical_relations", "nb-canonical-only") == 1
        assert _count("mention_edges", "nb-canonical-only") == 0
        assert _count("concept_comentions", "nb-canonical-only") == 0
        assert _count("canonical_relations", "nb-mention-only") == 0
        assert _count("mention_edges", "nb-mention-only") == 1
        assert _count("concept_comentions", "nb-mention-only") == 1
    finally:
        conn.close()


def test_merge_keeps_dismissals_and_drops_relink_marks_and_findings(tmp_path):
    """v97:导入库的「标为正常」随库合并(与合并决定同类);「补上关联已尝试」
    记的是副库的 kg_mutation_seq,导入库的状态行被清掉(seq 回到 0)之后它就
    不成立,必须删掉;问题清单明细与它的账本一样不导入。变异钉:去掉 relink
    标记的那条删除 → 报红;把 kg_analysis_findings 改回导入 → 报红。primary
    自己的标记一条不动。"""
    now = "2026-01-01T00:00:00"

    def _mk(path, nb):
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
                "INSERT INTO unified_kg_state (notebook_id, kg_mutation_seq, "
                "updated_at) VALUES (?, 9, ?)", (nb, now))
            for kind in ("dismissed", "relink_attempted"):
                conn.execute(
                    "INSERT INTO kg_analysis_marks (notebook_id, rule_id, subject_key, "
                    "kind, at_kg_seq, actor_user_id, created_at) "
                    "VALUES (?, ?, ?, ?, 9, 'u', ?)",
                    (nb, "F1" if kind == "dismissed" else "F5",
                     "src-1" if kind == "dismissed" else "notebook", kind, now))
            conn.execute(
                "INSERT INTO kg_analysis_findings (notebook_id, rule_id, subject_key, "
                "subject_kind, severity, first_seen_kg_seq, first_seen_cluster_seq) "
                "VALUES (?, 'F1', 'src-1', 'source', 'high', 9, 3)", (nb,))
        conn.close()

    primary, secondary, out = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "out.db"
    _mk(primary, "nb-main")
    _mk(secondary, "nb-sec")
    merge_dbs.merge_core(out, primary, secondary, shared_base="nb-none")

    conn = sqlite3.connect(str(out))
    try:
        marks = {
            (row[0], row[1])
            for row in conn.execute("SELECT notebook_id, kind FROM kg_analysis_marks")
        }
        findings = {
            row[0] for row in conn.execute("SELECT notebook_id FROM kg_analysis_findings")
        }
    finally:
        conn.close()
    assert marks == {
        ("nb-main", "dismissed"), ("nb-main", "relink_attempted"),
        ("nb-sec", "dismissed"),
    }
    assert findings == {"nb-main"}


def test_merge_carries_the_concept_seed_space_of_the_imported_clusters(tmp_path, monkeypatch):
    """Codex #988 R2 (S1, v102): the merge clears an imported notebook's state
    row but keeps its published concept clusters, built in the concept seed
    space of the source's PUBLISHED merge — and a missing row reads as the
    code's space 2. A library merged in before its first re-merge under the
    new rule (space 1) would mint "Q-Former" as ``K-qformer`` beside its
    ``K-q former``. Each imported notebook that keeps concept clusters gets a
    clean state row carrying the source's space (every other column its
    default: still reads as never built); one without clusters gets none.
    Built and merged by the real code; placement checked in the merged
    database. Mutation anchor: drop the INSERT from ``merge_core``."""
    from app.core.config import Settings
    from app.services.embedding import FakeEmbedder
    from app.services.sqlite_repository import SQLiteRepository
    from tests import kg_seed_space_world as world
    from tests.model_testkit import bind_all_embedding_clients

    def repository(name):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / name}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / f"{name}-storage"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        monkeypatch.setenv("EMBED_DIM", "16")
        repo = SQLiteRepository(Settings())
        bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
        return repo

    secondary = repository("b.db")
    old = world.notebook(secondary, "s1")
    world.concepts(secondary, old, "s1", "Q Former")
    with world.legacy_code(monkeypatch):
        world.rebuild(secondary, old)
    new = world.notebook(secondary, "t1")
    world.concepts(secondary, new, "t1", "Q Former")
    world.rebuild(secondary, new)
    bare = world.notebook(secondary)
    assert (world.published_space(secondary, old), world.published_space(secondary, new)) == (1, 2)
    secondary.close()

    primary, sec_db, out = tmp_path / "a.db", tmp_path / "b.db", tmp_path / "out.db"
    merge_dbs.migrate_to_current(primary)
    merge_dbs.migrate_to_current(sec_db)          # checkpoints the WAL, as main() does
    merge_dbs.merge_core(out, primary, sec_db, shared_base="nb-none")

    conn = sqlite3.connect(str(out))
    conn.row_factory = sqlite3.Row
    try:
        state = {r["notebook_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM unified_kg_state WHERE notebook_id IN (?, ?, ?)", (old, new, bare))}
    finally:
        conn.close()
    assert set(state) == {old, new}
    assert (state[old]["concept_seed_version"], state[new]["concept_seed_version"]) == (1, 2)
    for row in state.values():
        assert (row["dirty"], row["kg_mutation_seq"], row["cluster_mutation_seq"],
                row["cluster_generation"], row["community_generation"],
                row["cluster_input_version"], row["cluster_code_version"],
                row["last_rebuild_at"], row["cluster_count"]) == (0, 0, 0, 0, 0, "", "", "", 0), row

    merged = repository("out.db")
    try:
        world.fuse(merged, old, "s2", "Q-Former")
        world.fuse(merged, new, "t2", "Q-Former")
        assert world.canonical_of(merged, old)["Q-Former"] == "K-q former"
        assert world.canonical_of(merged, new)["Q-Former"] == "K-qformer"
    finally:
        merged.close()
