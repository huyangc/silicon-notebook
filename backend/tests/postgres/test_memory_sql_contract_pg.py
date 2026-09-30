"""Memory SQL 片段共享定义点(`repositories/postgres/memory_sql.py`)的行为契约(PostgreSQL)。

`tests/test_memory_sql_contract.py`(SQLite)的镜像,**吃同一张判定表**
(`tests/memory_sql_cases.py`):本人 Memory 来源可读、别人的不可读、孤儿与无 memory_id 的
Memory 来源对所有人失败即关、空查看者与 NULL 查看者读不到任何 Memory 来源、Knowhow 与普通
来源人人可读;每个片段在语句文本里它所在的位置消费固定个数的 `%s`(readable / foreign
各 1,derived / cluster / seed 各 0);「一条 Memory 的簇」(`memory_cluster` 与其补集)同一张表;簇片段的两条相关条件(同笔记本、同代)各有用例;外层别名
校验不分大小写、只收裸标识符;嵌进更大查询结果一致;`query_store` 旧常量改为引用片段后,
真聚合给出硬编码的黄金结果。
"""
from __future__ import annotations

import pytest

from app.domain.knowledge_contracts import USABLE_STATUSES
from app.repositories.postgres import memory_sql, query_store as postgres_query_store
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.query_store import QueryStore
from tests import memory_sql_cases as cases

pytestmark = pytest.mark.postgres_integration

NOW = "2026-09-29T00:00:00+00:00"


@pytest.fixture
def world(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    with postgres_database.write() as db:
        for index, uid in enumerate(cases.USERS):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (uid, f"{uid}@example.test", uid, NOW, NOW, f"m{index:08d}"),
            )
        for notebook in (cases.NOTEBOOK, cases.NOTEBOOK2):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at,tier) "
                "VALUES (%s,'NB','','','ready',%s,%s,%s,'personal')",
                (notebook, cases.OWNER, NOW, NOW),
            )
        for memory_id, created_by in cases.MEMORY_ITEMS:
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                "content_md,created_at,updated_at) "
                "VALUES (%s,%s,%s,'ask_answer','confirmed',%s,'x',%s,%s)",
                (memory_id, cases.NOTEBOOK, created_by, memory_id, NOW, NOW),
            )
        for notebook, sources in (
            (cases.NOTEBOOK, cases.SOURCES),
            (cases.NOTEBOOK2, cases.SOURCES_NB2),
        ):
            for source_id, source_type, memory_id in sources:
                db.execute(
                    "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
                    "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (source_id, notebook, source_id, source_type, memory_id, NOW, NOW),
                )
        for notebook, objects in (
            (cases.NOTEBOOK, cases.OBJECTS),
            (cases.NOTEBOOK2, cases.OBJECTS_NB2),
        ):
            for object_id, source_id, object_type in objects:
                db.execute(
                    "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
                    "source_id,created_at,updated_at) "
                    "VALUES (%s,%s,%s,'approved',%s,%s,%s)",
                    (object_id, notebook, object_type, source_id, NOW, NOW),
                )
        for relation_id, source_id in cases.RELATIONS:
            db.execute(
                "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (relation_id, cases.NOTEBOOK, source_id, "ko-upload", "ko-knowhow",
                 "related_to", NOW),
            )
        for notebook, canonical_id, name, generation, members in cases.CLUSTERS:
            for member in members:
                db.execute(
                    "INSERT INTO concept_clusters(id,notebook_id,canonical_id,"
                    "member_object_id,canonical_name,object_type,created_at,generation) "
                    "VALUES (%s,%s,%s,%s,%s,'concept',%s,%s)",
                    (f"cc-{canonical_id}-{generation}-{member}", notebook, canonical_id,
                     member, name, NOW, generation),
                )
    return postgres_database


def _ids(database, sql: str, params: tuple = ()) -> set[str]:
    with database.connect() as db:
        return {row["id"] for row in db.execute(sql, params).fetchall()}


def _sources(database, viewer) -> set[str]:
    return _ids(
        database,
        f"SELECT s.id FROM sources s WHERE {memory_sql.memory_source_readable('s')}",
        (viewer,),
    )


def _objects_kept(database, viewer) -> set[str]:
    return _ids(
        database,
        "SELECT o.id FROM knowledge_objects o "
        f"WHERE {memory_sql.foreign_memory_object_excluded('o')}",
        (viewer,),
    )


def _relations_kept(database, viewer) -> set[str]:
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
        assert memory_sql.memory_source_readable(alias).count("%s") == 1
    for alias in ("o", "ko", "x1"):
        assert memory_sql.foreign_memory_object_excluded(alias).count("%s") == 1
        assert memory_sql.memory_derived_object(alias).count("%s") == 0
        assert memory_sql.memory_derived_in_notebook(alias).count("%s") == 0
    for alias in ("r", "kr", "x1"):
        assert memory_sql.foreign_memory_relation_excluded(alias).count("%s") == 1
        assert memory_sql.memory_derived_relation(alias).count("%s") == 0
    for alias in ("c", "cc", "x1"):
        assert memory_sql.no_memory_member_cluster(alias).count("%s") == 0
        assert memory_sql.memory_cluster(alias).count("%") == 0
        assert memory_sql.no_memory_cluster(alias).count("%") == 0
        assert memory_sql.cluster_seed_object_id(alias).count("%") == 0
    for fragment in (
        memory_sql.memory_source_readable("s"),
        memory_sql.foreign_memory_object_excluded("o"),
        memory_sql.memory_derived_object("o"),
        memory_sql.no_memory_member_cluster("c"),
    ):
        assert "?" not in fragment


# ------------------------------------------------------------------ 外层别名校验
def _alias_calls():
    return {
        "readable": (memory_sql.memory_source_readable,),
        "foreign": (
            memory_sql.foreign_memory_object_excluded,
            memory_sql.foreign_memory_relation_excluded,
        ),
        "derived": (
            memory_sql.memory_derived_object,
            memory_sql.memory_derived_relation,
            memory_sql.memory_derived_in_notebook,
        ),
        "cluster": (memory_sql.no_memory_member_cluster,),
        "memory_cluster": (
            memory_sql.memory_cluster, memory_sql.no_memory_cluster,
            memory_sql.memory_seed_cluster,
        ),
        "seed": (memory_sql.cluster_seed_object_id,),
    }


@pytest.mark.parametrize("family", sorted(cases.BAD_ALIASES))
def test_outer_alias_that_would_capture_an_inner_table_or_is_not_a_bare_identifier_is_refused(
    family,
):
    for fragment in _alias_calls()[family]:
        for bad in cases.BAD_ALIASES[family]:
            with pytest.raises(ValueError):
                fragment(bad)
        for good in ("o", "O1", "_x", "outer_1"):
            assert fragment(good)


# --------------------------------------------------------------------- 行为矩阵
@pytest.mark.parametrize("viewer", list(cases.READABLE_SOURCES))
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


@pytest.mark.parametrize("viewer", ["", None])
def test_empty_or_null_viewer_reads_no_memory_source(world, viewer):
    memory_ids = {s[0] for s in cases.SOURCES if s[1] == "memory"}
    assert _sources(world, viewer) & memory_ids == set()


@pytest.mark.parametrize("viewer", list(cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS))
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


_CLUSTER_KEPT_SQL = (
    "SELECT DISTINCT c.notebook_id || '/' || c.canonical_id || '/' || "
    "CAST(c.generation AS TEXT) AS id FROM concept_clusters c "
    f"WHERE {memory_sql.no_memory_member_cluster('c')}"
)


def test_no_memory_member_cluster_excludes_the_whole_cluster(world):
    assert _ids(world, _CLUSTER_KEPT_SQL) == cases.NO_MEMORY_MEMBER_CLUSTERS


def test_no_memory_member_cluster_is_scoped_to_its_own_notebook(world):
    kept = _ids(world, _CLUSTER_KEPT_SQL)
    assert f"{cases.NOTEBOOK}/can-mixed/0" not in kept
    assert f"{cases.NOTEBOOK2}/can-mixed/0" in kept, (
        "another notebook's Memory member hid this notebook's same-named cluster"
    )


def test_no_memory_member_cluster_is_scoped_to_its_own_generation(world):
    kept = _ids(world, _CLUSTER_KEPT_SQL)
    assert f"{cases.NOTEBOOK}/can-gen/1" not in kept
    assert f"{cases.NOTEBOOK}/can-gen/0" in kept, (
        "a Memory member in the building generation hid the published cluster"
    )


def _cluster_keys(database, predicate: str) -> set[str]:
    return _ids(
        database,
        "SELECT DISTINCT c.notebook_id || '/' || c.canonical_id || '/' || "
        f"CAST(c.generation AS TEXT) AS id FROM concept_clusters c WHERE {predicate}",
    )


def test_memory_cluster_is_the_one_definition_of_a_cluster_of_a_memory(world):
    memory = _cluster_keys(world, memory_sql.memory_cluster("c"))
    clean = _cluster_keys(world, memory_sql.no_memory_cluster("c"))
    assert memory == cases.MEMORY_CLUSTERS
    assert clean == cases.ALL_CLUSTERS - cases.MEMORY_CLUSTERS
    assert cases.ALL_CLUSTERS - cases.NO_MEMORY_MEMBER_CLUSTERS <= memory


def test_cluster_seed_object_id_reads_the_object_id_a_canonical_id_was_minted_from(world):
    for canonical, expected in cases.CLUSTER_SEED_OBJECT_IDS.items():
        with world.connect() as db:
            got = db.execute(
                f"SELECT {memory_sql.cluster_seed_object_id('c')} AS seed "
                "FROM (SELECT %s::text AS canonical_id) c",
                (canonical,),
            ).fetchone()["seed"]
        assert got == expected, canonical


def test_memory_derived_in_notebook_agrees_on_same_notebook_rows_and_stops_at_the_notebook(world):
    """合法数据(行与来源同笔记本)上与 `memory_derived_object` 逐行同义;来源在别的笔记本
    时不算(那条只用来把外层的笔记本条件传进内层)。"""
    got = _ids(
        world,
        "SELECT o.id AS id FROM knowledge_objects o "
        f"WHERE {memory_sql.memory_derived_in_notebook('o')}",
    )
    assert got == cases.MEMORY_DERIVED_OBJECTS
    with world.connect() as db:
        row = db.execute(
            f"SELECT {memory_sql.memory_derived_in_notebook('x')} AS here, "
            f"{memory_sql.memory_derived_object('x')} AS anywhere "
            "FROM (SELECT %s::text AS source_id, %s::text AS notebook_id) x",
            ("src-mem-alice", cases.NOTEBOOK2),
        ).fetchone()
    assert (bool(row["here"]), bool(row["anywhere"])) == (False, True)


def test_memory_member_cluster_keys_is_the_member_arm_as_a_set(world):
    """同一判据的集合形态:每个笔记本读出的 (canonical_id, generation) 恰是成员臂排除的那些簇,
    一个参数(笔记本 id)。"""
    member_clusters = cases.ALL_CLUSTERS - cases.NO_MEMORY_MEMBER_CLUSTERS
    for notebook in (cases.NOTEBOOK, cases.NOTEBOOK2):
        with world.connect() as db:
            rows = db.execute(memory_sql.memory_member_cluster_keys(), (notebook,)).fetchall()
        got = {f"{notebook}/{row['canonical_id']}/{row['generation']}" for row in rows}
        assert got == {key for key in member_clusters if key.startswith(notebook + "/")}
    assert memory_sql.memory_member_cluster_keys().count("%s") == 1


def test_memory_cluster_docstring_holds_seed_arm_only_minted_real_names_left_to_the_dirty_rule(
    world,
):
    """`memory_cluster` docstring 的两句话:canonical 臂只认按对象 id 铸的种子(``K-~ko-…``、
    ``Kx-~ko-…``);真名种子(哪怕长得像对象 id)在这里认不出来——Memory 被删后由拷贝的「脏源库
    不带簇」规则覆盖(见 test_notebook_share_copy / test_copy_memory_exclusion_pg 的
    test_a_cluster_seeded_by_a_since_deleted_memory)。"""
    doc = memory_sql.memory_cluster.__doc__
    assert "K-~ko-" in doc and "Kx-~ko-" in doc and "_source_clustering_current" in doc
    assert "minted_canonical_ids" in doc
    seeded = _cluster_keys(world, memory_sql.memory_seed_cluster("c"))
    assert seeded == cases.MEMORY_SEED_CLUSTERS
    assert f"{cases.NOTEBOOK}/K-ko-mem-alice/7" not in _cluster_keys(
        world, memory_sql.memory_cluster("c")
    )


# --------------------------------------------------------------- 嵌进更大的查询
def test_fragment_embedded_in_a_larger_query_agrees_with_the_standalone_result(world):
    for viewer, expected in cases.READABLE_SOURCES.items():
        got = _ids(
            world,
            "SELECT s.id FROM sources s JOIN notebooks n ON n.id = s.notebook_id "
            "WHERE n.id = ANY(%s) AND "
            f"{memory_sql.memory_source_readable('s')} "
            "AND s.status = %s AND s.id IN (SELECT source_id FROM knowledge_objects "
            "WHERE notebook_id = ANY(%s))",
            ([cases.NOTEBOOK, cases.NOTEBOOK2], viewer, "uploaded",
             [cases.NOTEBOOK, cases.NOTEBOOK2]),
        )
        objects_owned_by_a_source = {
            o[1] for o in cases.OBJECTS + cases.OBJECTS_NB2 if o[1]
        }
        assert got == expected & objects_owned_by_a_source, viewer

    for viewer, expected in cases.FOREIGN_EXCLUDED_KEEPS_OBJECTS.items():
        got = _ids(
            world,
            "SELECT o.id FROM knowledge_objects o "
            "LEFT JOIN concept_clusters c ON c.member_object_id = o.id "
            "WHERE o.status = %s AND "
            f"{memory_sql.foreign_memory_object_excluded('o')} "
            "AND o.object_type = ANY(%s)",
            ("approved", viewer, ["concept", "claim"]),
        )
        assert got == expected, viewer


# ------------------------------------------------------ query_store 旧常量归一
def test_query_store_constants_reference_the_shared_fragments():
    assert postgres_query_store._NOT_MEMORY_OWNED_SQL == (
        "NOT " + memory_sql.memory_derived_object("o")
    )
    assert postgres_query_store._NO_MEMORY_MEMBER_CLUSTER_SQL == (
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
