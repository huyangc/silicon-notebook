# backend/tests/test_memory_sql_contract.py
"""Memory SQL 片段唯一定义点(`repositories/sqlite/memory_sql.py`)的行为契约(SQLite)。

E0 把「这条 Memory 派生行能不能给这个人看」与「这行知识算不算派生自 Memory」收进唯一
定义点。这份矩阵钉住的是**哪一格该翻、哪一格绝不许翻**(判定表在
`memory_sql_cases.py`,PostgreSQL 侧 `postgres/test_memory_sql_contract_pg.py` 吃同一张表):

* 本人的 Memory 来源可读,别人的不可读;孤儿 Memory 来源(``memory_id`` 指向不存在的行)
  与无 ``memory_id`` 的 Memory 来源对所有人失败即关;空查看者读不到任何 Memory 来源。
* Knowhow 与普通来源不受本谓词影响——**每个人**都可读。
* 每个片段消费**固定个数**的位置参数:readable / foreign 各 1 个,derived / cluster 各 0 个
  ——参数个数随别名或实现而变,调用方在更大语句里就会静默错绑。
* 嵌进更大的查询(参数夹在别的参数中间、与其他表 join)结果与独立执行一致。
* 旧 `query_store._NOT_MEMORY_OWNED_SQL` 改为引用 `memory_derived_object` 后,取代码里的
  真聚合在同一份数据上给出硬编码的黄金结果,且新片段的否定与 E0 之前的手写文本逐行同义。
"""
import pytest

from app.core.config import Settings
from app.domain.knowledge_contracts import USABLE_STATUSES
from app.repositories.sqlite import memory_sql, query_store as sqlite_query_store
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.query_store import QueryStore
from tests import memory_sql_cases as cases

NOW = "2026-09-29T00:00:00+00:00"


@pytest.fixture
def world(tmp_path):
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate()
    with database.write() as db:
        for uid in cases.USERS:
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, f"{uid}@example.test", uid, "user", "active", NOW, NOW),
            )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (cases.NOTEBOOK, "NB", "", "Semiconductor", "draft", cases.OWNER, NOW, NOW),
        )
        for memory_id, created_by in cases.MEMORY_ITEMS:
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                "content_md,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (memory_id, cases.NOTEBOOK, created_by, "ask_answer", "confirmed",
                 memory_id, "x", NOW, NOW),
            )
        for source_id, source_type, memory_id in cases.SOURCES:
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (source_id, cases.NOTEBOOK, source_id, source_type, memory_id, NOW, NOW),
            )
        for object_id, source_id, object_type in cases.OBJECTS:
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (object_id, cases.NOTEBOOK, object_type, "approved", source_id, NOW, NOW),
            )
        for relation_id, source_id in cases.RELATIONS:
            db.execute(
                "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,created_at) VALUES (?,?,?,?,?,?,?)",
                (relation_id, cases.NOTEBOOK, source_id, "ko-upload", "ko-knowhow",
                 "related_to", NOW),
            )
        for canonical_id, name, members in cases.CLUSTERS:
            for member in members:
                db.execute(
                    "INSERT INTO concept_clusters(id,notebook_id,canonical_id,"
                    "member_object_id,canonical_name,object_type,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"cc-{canonical_id}-{member}", cases.NOTEBOOK, canonical_id, member,
                     name, "concept", NOW),
                )
    return database


def _ids(database, sql: str, params: tuple = ()) -> set[str]:
    with database.connect() as db:
        return {row[0] for row in db.execute(sql, params).fetchall()}


def _sources(database, viewer: str) -> set[str]:
    return _ids(
        database,
        f"SELECT s.id FROM sources s WHERE {memory_sql.memory_source_readable('s')}",
        (viewer,),
    )


def _objects_kept(database, viewer: str) -> set[str]:
    return _ids(
        database,
        "SELECT o.id FROM knowledge_objects o "
        f"WHERE {memory_sql.foreign_memory_object_excluded('o')}",
        (viewer,),
    )


def _relations_kept(database, viewer: str) -> set[str]:
    return _ids(
        database,
        "SELECT r.id FROM knowledge_relations r "
        f"WHERE {memory_sql.foreign_memory_relation_excluded('r')}",
        (viewer,),
    )


# --------------------------------------------------------------------- 参数个数
def test_every_fragment_consumes_a_fixed_number_of_positional_parameters():
    """readable / foreign 恰好 1 个(查看者),derived / cluster 恰好 0 个;与别名无关。"""
    for alias in ("s", "src", "x1"):
        assert memory_sql.memory_source_readable(alias).count("?") == 1
    for alias in ("o", "ko", "x1"):
        assert memory_sql.foreign_memory_object_excluded(alias).count("?") == 1
        assert memory_sql.memory_derived_object(alias).count("?") == 0
    for alias in ("r", "kr", "x1"):
        assert memory_sql.foreign_memory_relation_excluded(alias).count("?") == 1
        assert memory_sql.memory_derived_relation(alias).count("?") == 0
    for alias in ("c", "cc", "x1"):
        assert memory_sql.no_memory_member_cluster(alias).count("?") == 0
    # 没有别的占位符方言混进来。
    for fragment in (
        memory_sql.memory_source_readable("s"),
        memory_sql.foreign_memory_object_excluded("o"),
        memory_sql.memory_derived_object("o"),
        memory_sql.no_memory_member_cluster("c"),
    ):
        assert "%s" not in fragment


def test_outer_alias_that_would_capture_an_inner_table_is_refused():
    """外层别名与内层子查询别名相同会把相关引用绑到内层表上,静默改变语义 —— 直接拒绝。"""
    for bad in ("rm", "1x", "a b", "s; DROP TABLE sources", ""):
        with pytest.raises(ValueError):
            memory_sql.memory_source_readable(bad)
    for bad in ("fs", "fm"):
        with pytest.raises(ValueError):
            memory_sql.foreign_memory_object_excluded(bad)
        with pytest.raises(ValueError):
            memory_sql.foreign_memory_relation_excluded(bad)
    for bad in ("ds", "d s"):
        with pytest.raises(ValueError):
            memory_sql.memory_derived_object(bad)
        with pytest.raises(ValueError):
            memory_sql.memory_derived_relation(bad)
    for bad in ("mc", "mo", "ms"):
        with pytest.raises(ValueError):
            memory_sql.no_memory_member_cluster(bad)


# --------------------------------------------------------------------- 行为矩阵
@pytest.mark.parametrize("viewer", sorted(cases.READABLE_SOURCES))
def test_source_readability_matrix(world, viewer):
    assert _sources(world, viewer) == cases.READABLE_SOURCES[viewer]


def test_own_memory_is_readable_and_anothers_is_not(world):
    assert "src-mem-alice" in _sources(world, "u-alice")
    assert "src-mem-bob" not in _sources(world, "u-alice"), "another user's Memory not readable"
    assert "src-mem-alice" not in _sources(world, "u-bob")


def test_orphan_and_memoryless_memory_sources_are_readable_by_nobody(world):
    for viewer in cases.READABLE_SOURCES:
        readable = _sources(world, viewer)
        assert "src-mem-orphan" not in readable, viewer
        assert "src-mem-null" not in readable, viewer


def test_knowhow_and_ordinary_sources_are_readable_by_everyone(world):
    for viewer in cases.READABLE_SOURCES:
        readable = _sources(world, viewer)
        assert {"src-knowhow", "src-upload"} <= readable, "ordinary source readable"


def test_empty_viewer_reads_no_memory_source(world):
    memory_ids = {s[0] for s in cases.SOURCES if s[1] == "memory"}
    assert _sources(world, "") & memory_ids == set()


@pytest.mark.parametrize("viewer", sorted(cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS))
def test_foreign_memory_exclusion_matrix(world, viewer):
    assert _objects_kept(world, viewer) == cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS[viewer]
    assert _relations_kept(world, viewer) == cases.FOREIGN_EXCLUDED_KEEPS_RELATIONS[viewer]


def test_rows_without_an_owning_source_are_never_excluded(world):
    for viewer in cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS:
        assert "ko-unowned" in _objects_kept(world, viewer)
        assert "kr-no-source" in _relations_kept(world, viewer)


def test_memory_derived_classifier_ignores_the_viewer_and_covers_orphans(world):
    derived_objects = _ids(
        world,
        f"SELECT o.id FROM knowledge_objects o WHERE {memory_sql.memory_derived_object('o')}",
    )
    derived_relations = _ids(
        world,
        "SELECT r.id FROM knowledge_relations r "
        f"WHERE {memory_sql.memory_derived_relation('r')}",
    )
    assert derived_objects == cases.MEMORY_DERIVED_OBJECTS
    assert derived_relations == cases.MEMORY_DERIVED_RELATIONS


def test_no_memory_member_cluster_excludes_the_whole_cluster(world):
    kept = _ids(
        world,
        "SELECT DISTINCT c.canonical_id FROM concept_clusters c "
        f"WHERE {memory_sql.no_memory_member_cluster('c')}",
    )
    assert kept == cases.NO_MEMORY_MEMBER_CLUSTERS


# --------------------------------------------------------------- 嵌进更大的查询
def test_fragment_embedded_in_a_larger_query_agrees_with_the_standalone_result(world):
    """参数夹在别的参数中间、与别的表 join,结果仍与独立执行一致。"""
    for viewer, expected in cases.READABLE_SOURCES.items():
        got = _ids(
            world,
            "SELECT s.id FROM sources s JOIN notebooks n ON n.id = s.notebook_id "
            "WHERE n.id = ? AND "
            f"{memory_sql.memory_source_readable('s')} "
            "AND s.status = ? AND s.id IN (SELECT source_id FROM knowledge_objects "
            "WHERE notebook_id = ?)",
            (cases.NOTEBOOK, viewer, "uploaded", cases.NOTEBOOK),
        )
        objects_owned_by_a_source = {o[1] for o in cases.OBJECTS if o[1]}
        assert got == expected & objects_owned_by_a_source, viewer

    for viewer, expected in cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS.items():
        got = _ids(
            world,
            "SELECT o.id FROM knowledge_objects o "
            "LEFT JOIN concept_clusters c ON c.member_object_id = o.id "
            "WHERE o.notebook_id = ? AND o.status = ? AND "
            f"{memory_sql.foreign_memory_object_excluded('o')} AND o.object_type IN (?, ?)",
            (cases.NOTEBOOK, "approved", viewer, "concept", "claim"),
        )
        assert got == expected, viewer


# ------------------------------------------------------ query_store 旧常量归一
def test_query_store_constants_reference_the_shared_fragments():
    assert sqlite_query_store._NOT_MEMORY_OWNED_SQL == (
        "NOT " + memory_sql.memory_derived_object("o")
    )
    assert sqlite_query_store._NO_MEMORY_MEMBER_CLUSTER_SQL == (
        memory_sql.no_memory_member_cluster("c")
    )


def test_derived_classifier_agrees_with_the_pre_e0_hand_written_predicate(world):
    old_kept = _ids(
        world,
        f"SELECT o.id FROM knowledge_objects o WHERE {cases.PRE_E0_NOT_MEMORY_OWNED}",
    )
    new_kept = _ids(
        world,
        "SELECT o.id FROM knowledge_objects o "
        f"WHERE NOT {memory_sql.memory_derived_object('o')}",
    )
    assert new_kept == old_kept == cases.ALL_OBJECT_IDS - cases.MEMORY_DERIVED_OBJECTS


def test_query_store_aggregates_give_the_golden_results(world):
    queries = QueryStore(world)
    with world.connect() as db:
        counts = {
            str(row["object_type"]): int(row["c"])
            for row in queries.knowledge_type_count_rows_excluding_memory(
                db, cases.NOTEBOOK, USABLE_STATUSES
            )
        }
        names = queries.top_concept_names(db, cases.NOTEBOOK, USABLE_STATUSES, 24)
    assert counts == cases.QUERY_STORE_TYPE_COUNTS
    assert names == cases.QUERY_STORE_TOP_CONCEPTS
