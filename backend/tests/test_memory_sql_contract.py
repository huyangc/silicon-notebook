"""Memory SQL 片段唯一定义点(`repositories/sqlite/memory_sql.py`)的行为契约(SQLite)。

E0 把「这条 Memory 派生行能不能给这个人看」与「这行知识算不算派生自 Memory」收进共享
定义点。这份矩阵钉住的是**哪一格该翻、哪一格绝不许翻**(判定表在
`memory_sql_cases.py`,PostgreSQL 侧 `postgres/test_memory_sql_contract_pg.py` 吃同一张表):

* 本人的 Memory 来源可读,别人的不可读;孤儿 Memory 来源(``memory_id`` 指向不存在的行)
  与无 ``memory_id`` 的 Memory 来源对所有人失败即关;空查看者与 NULL 查看者读不到任何
  Memory 来源。
* Knowhow 与普通来源不受本谓词影响——**每个人**都可读。
* **参数契约**:每个片段在语句文本里**它所在的位置**恰好消费固定个数的位置参数——
  readable / foreign 各 1 个(查看者),derived / cluster 各 0 个。参数个数随别名或实现而
  变、或片段被放到语句里别的位置,调用方在更大语句里就会静默错绑;所以这里既断言个数,
  也断言「参数夹在别的参数中间」的嵌入结果与独立执行一致。
* 簇片段的两条相关条件各有用例:不同笔记本可以撞同一个 canonical_id(簇 id 是
  ``K-<规范化种子名>``),笔记本一里的 Memory 成员不得让笔记本二的同名簇消失;building 代
  里的 Memory 成员不得让 published 代的簇消失。
* 外层别名校验:不分大小写地拒绝撞内层别名(``MC`` 会让簇片段退化成恒真的
  ``mc.x = mc.x``,静默排除所有笔记本的所有簇)、拒绝尾部换行、带引号与带 schema 的写法。
* 两个后端的片段文本在把 ``%s`` 换成 ``?`` 后逐字相同;``'memory'`` 字面量与
  ``source_store.MEMORY_SOURCE_TYPE_PREDICATE`` 不漂移。
* 旧 `query_store._NOT_MEMORY_OWNED_SQL` 改为引用 `memory_derived_object` 后,取代码里的
  真聚合在同一份数据上给出硬编码的黄金结果,且新片段的否定与 E0 之前的手写文本逐行同义。
"""
import re

import pytest

from app.core.config import Settings
from app.domain.knowledge_contracts import USABLE_STATUSES
from app.repositories.postgres import (
    memory_sql as pg_memory_sql,
    source_store as pg_source_store,
)
from app.repositories.sqlite import (
    memory_sql,
    query_store as sqlite_query_store,
    source_store as sqlite_source_store,
)
from app.repositories.sqlite.query_store import QueryStore
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_sql_cases as cases

NOW = "2026-09-29T00:00:00+00:00"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    database = repo._runtime.database
    with database.write() as db:
        for uid in cases.USERS:
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, f"{uid}@example.test", uid, "user", "active", NOW, NOW),
            )
        for notebook in (cases.NOTEBOOK, cases.NOTEBOOK2):
            db.execute(
                "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (notebook, "NB", "", "Semiconductor", "draft", cases.OWNER, NOW, NOW),
            )
        for memory_id, created_by in cases.MEMORY_ITEMS:
            db.execute(
                "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                "content_md,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (memory_id, cases.NOTEBOOK, created_by, "ask_answer", "confirmed",
                 memory_id, "x", NOW, NOW),
            )
        for notebook, sources in (
            (cases.NOTEBOOK, cases.SOURCES),
            (cases.NOTEBOOK2, cases.SOURCES_NB2),
        ):
            for source_id, source_type, memory_id in sources:
                db.execute(
                    "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (source_id, notebook, source_id, source_type, memory_id, NOW, NOW),
                )
        for notebook, objects in (
            (cases.NOTEBOOK, cases.OBJECTS),
            (cases.NOTEBOOK2, cases.OBJECTS_NB2),
        ):
            for object_id, source_id, object_type in objects:
                db.execute(
                    "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
                    "source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                    (object_id, notebook, object_type, "approved", source_id, NOW, NOW),
                )
        for relation_id, source_id in cases.RELATIONS:
            db.execute(
                "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,created_at) VALUES (?,?,?,?,?,?,?)",
                (relation_id, cases.NOTEBOOK, source_id, "ko-upload", "ko-knowhow",
                 "related_to", NOW),
            )
        for notebook, canonical_id, name, generation, members in cases.CLUSTERS:
            for member in members:
                db.execute(
                    "INSERT INTO concept_clusters(id,notebook_id,canonical_id,"
                    "member_object_id,canonical_name,object_type,created_at,generation) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f"cc-{canonical_id}-{generation}-{member}", notebook, canonical_id,
                     member, name, "concept", NOW, generation),
                )
    return database


def _ids(database, sql: str, params: tuple = ()) -> set[str]:
    with database.connect() as db:
        return {row[0] for row in db.execute(sql, params).fetchall()}


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
        assert memory_sql.memory_source_readable(alias).count("?") == 1
    for alias in ("o", "ko", "x1"):
        assert memory_sql.foreign_memory_object_excluded(alias).count("?") == 1
        assert memory_sql.memory_derived_object(alias).count("?") == 0
        assert memory_sql.memory_derived_in_notebook(alias).count("?") == 0
    for alias in ("r", "kr", "x1"):
        assert memory_sql.foreign_memory_relation_excluded(alias).count("?") == 1
        assert memory_sql.memory_derived_relation(alias).count("?") == 0
    for alias in ("c", "cc", "x1"):
        assert memory_sql.no_memory_member_cluster(alias).count("?") == 0
        assert memory_sql.memory_cluster(alias).count("?") == 0
        assert memory_sql.no_memory_cluster(alias).count("?") == 0
        assert memory_sql.cluster_seed_object_id(alias).count("?") == 0
    # 没有别的占位符方言混进来。
    for fragment in (
        memory_sql.memory_source_readable("s"),
        memory_sql.foreign_memory_object_excluded("o"),
        memory_sql.memory_derived_object("o"),
        memory_sql.no_memory_member_cluster("c"),
        memory_sql.memory_cluster("c"),
        memory_sql.no_memory_cluster("c"),
        memory_sql.cluster_seed_object_id("c"),
    ):
        assert "%s" not in fragment and "%" not in fragment


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
    """SQL 标识符不区分大小写:``MC`` 会绑到内层表上、``mc.x = mc.x`` 恒真;``re.match`` 的
    ``$`` 还会放过尾部换行。带引号与带 schema 的写法也不是裸标识符。"""
    for fragment in _alias_calls()[family]:
        for bad in cases.BAD_ALIASES[family]:
            with pytest.raises(ValueError):
                fragment(bad)
        # 合法别名(含大写、下划线、数字)照常通过。
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
    "CAST(c.generation AS TEXT) FROM concept_clusters c "
    f"WHERE {memory_sql.no_memory_member_cluster('c')}"
)


def test_no_memory_member_cluster_excludes_the_whole_cluster(world):
    assert _ids(world, _CLUSTER_KEPT_SQL) == cases.NO_MEMORY_MEMBER_CLUSTERS


def test_no_memory_member_cluster_is_scoped_to_its_own_notebook(world):
    """簇 id 是 ``K-<规范化种子名>``:两个笔记本可以有同一个 canonical_id。笔记本一的
    ``can-mixed`` 含 alice 的 Memory 成员,笔记本二的同名簇只含普通对象,必须保留。"""
    kept = _ids(world, _CLUSTER_KEPT_SQL)
    assert f"{cases.NOTEBOOK}/can-mixed/0" not in kept
    assert f"{cases.NOTEBOOK2}/can-mixed/0" in kept, (
        "another notebook's Memory member hid this notebook's same-named cluster"
    )


def test_no_memory_member_cluster_is_scoped_to_its_own_generation(world):
    """building 代(generation 1)里的 Memory 成员只让**这一代**的簇被排除,published 代
    (generation 0)的同名簇必须保留。"""
    kept = _ids(world, _CLUSTER_KEPT_SQL)
    assert f"{cases.NOTEBOOK}/can-gen/1" not in kept
    assert f"{cases.NOTEBOOK}/can-gen/0" in kept, (
        "a Memory member in the building generation hid the published cluster"
    )


def _cluster_keys(database, predicate: str) -> set[str]:
    return _ids(
        database,
        "SELECT DISTINCT c.notebook_id || '/' || c.canonical_id || '/' || "
        f"CAST(c.generation AS TEXT) FROM concept_clusters c WHERE {predicate}",
    )


def test_memory_cluster_is_the_one_definition_of_a_cluster_of_a_memory(world):
    """「一条 Memory 的簇」:有 Memory 派生成员(按笔记本、代),或 canonical id 就是 / 铸自
    一个 Memory 派生对象。铸自普通对象、铸自已删对象(判不出)、真名种子恰好长得像对象 id
    都不算。`no_memory_cluster` 恰是它的补集。"""
    memory = _cluster_keys(world, memory_sql.memory_cluster("c"))
    clean = _cluster_keys(world, memory_sql.no_memory_cluster("c"))
    assert memory == cases.MEMORY_CLUSTERS
    assert clean == cases.ALL_CLUSTERS - cases.MEMORY_CLUSTERS
    # 成员臂与 no_memory_member_cluster 同义:凡被它排除的簇都是 Memory 的簇。
    assert cases.ALL_CLUSTERS - cases.NO_MEMORY_MEMBER_CLUSTERS <= memory


def test_cluster_seed_object_id_reads_the_object_id_a_canonical_id_was_minted_from(world):
    for canonical, expected in cases.CLUSTER_SEED_OBJECT_IDS.items():
        with world.connect() as db:
            got = db.execute(
                f"SELECT {memory_sql.cluster_seed_object_id('c')} AS seed "
                "FROM (SELECT ? AS canonical_id) c",
                (canonical,),
            ).fetchone()["seed"]
        assert got == expected, canonical


def test_cluster_seed_object_id_matches_how_kg_merge_mints_canonical_ids(world):
    """与 `kg_merge.seed_or_unique` 的铸造规则对齐:每个类型前缀(概念 `K-`,claim/formula/
    procedure 的 `KL-`/`KF-`/`KP-`)加上退化名的哨兵种子,取回的就是那个对象 id;真名种子
    取回 NULL。前缀清单改了、哨兵改了,这里当场红。"""
    from app.services.kg_merge import seed_or_unique
    from app.services import knowledge_lifecycle

    source = open(knowledge_lifecycle.__file__, encoding="utf-8").read()
    prefixes = set(re.findall(r'id_prefix="(K[A-Z]?-)"', source)) | set(
        re.findall(r'\(seed_\w+, "(K[A-Z]?-)"\)', source)
    )
    assert prefixes == {"K-", "KL-", "KF-", "KP-"}, prefixes
    for prefix in sorted(prefixes):
        for object_id, name in (("ko-3f2a9c", ""), ("ko-3f2a9c", "real name")):
            canonical = f"{prefix}{seed_or_unique(name, object_id)}"
            with world.connect() as db:
                got = db.execute(
                    f"SELECT {memory_sql.cluster_seed_object_id('c')} AS seed "
                    "FROM (SELECT ? AS canonical_id) c",
                    (canonical,),
                ).fetchone()["seed"]
            assert got == (object_id if not name else None), canonical


def test_memory_derived_in_notebook_agrees_on_same_notebook_rows_and_stops_at_the_notebook(world):
    """合法数据(行与来源同笔记本)上与 `memory_derived_object` 逐行同义;来源在别的笔记本
    时不算(那条只用来把外层的笔记本条件传进内层)。"""
    got = _ids(
        world,
        "SELECT o.id FROM knowledge_objects o "
        f"WHERE {memory_sql.memory_derived_in_notebook('o')}",
    )
    assert got == cases.MEMORY_DERIVED_OBJECTS
    with world.connect() as db:
        row = db.execute(
            f"SELECT {memory_sql.memory_derived_in_notebook('x')} AS here, "
            f"{memory_sql.memory_derived_object('x')} AS anywhere "
            "FROM (SELECT ? AS source_id, ? AS notebook_id) x",
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
        got = {f"{notebook}/{row[0]}/{row[1]}" for row in rows}
        assert got == {key for key in member_clusters if key.startswith(notebook + "/")}
    assert memory_sql.memory_member_cluster_keys().count("?") == 1


def test_memory_cluster_docstring_holds_seed_arm_only_minted_real_names_left_to_the_dirty_rule(
    world,
):
    """`memory_cluster` docstring 的两句话:canonical 臂只认按对象 id 铸的种子(``K-~ko-…``、
    ``Kx-~ko-…``);真名种子(哪怕长得像对象 id)在这里认不出来——Memory 被删后由拷贝的「脏源库
    不带簇」规则覆盖(见 test_notebook_share_copy / test_copy_memory_exclusion_pg 的
    test_a_cluster_seeded_by_a_since_deleted_memory)。

    已知缺口(登记给 E4-2,本分支不改):成簇**干净**(dirty=0)、真名种子、Memory 已不是成员的
    簇——例如 ``K-alice-private-plan``,唯一成员是共享对象,簇名与描述取自已删的 Memory——两条
    规则都认不出,拷贝会带出它的名字。删除路径今天到不了这个形态:``delete_source`` 在拆除
    事务里必标脏,``remove_memory_sources`` 与它共用拆除事务、同样标脏;只有「重建进行中删了 Memory、
    重建收尾 ``finish_rebuild_state`` 无条件写 dirty=0」才会留下它(重建的新一代若读到删除前
    的成员,名字随之发布,删除的脏标又被清掉)。"""
    doc = memory_sql.memory_cluster.__doc__
    assert "K-~ko-" in doc and "Kx-~ko-" in doc and "_source_clustering_current" in doc
    # 清除与这里共用同一条铸造规则,另认桥接 id;不再指向已删除的第二份规则。
    assert "purge_memory_review_rows_on" in doc and "purge_bridge_canonical_ids" in doc
    assert "minted_canonical_ids" not in doc
    seeded = _cluster_keys(world, memory_sql.memory_seed_cluster("c"))
    assert seeded == cases.MEMORY_SEED_CLUSTERS
    assert f"{cases.NOTEBOOK}/K-ko-mem-alice/7" not in _cluster_keys(
        world, memory_sql.memory_cluster("c")
    )


# --------------------------------------------------------------- 嵌进更大的查询
def test_fragment_embedded_in_a_larger_query_agrees_with_the_standalone_result(world):
    """参数夹在别的参数中间、与别的表 join,结果仍与独立执行一致。"""
    for viewer, expected in cases.READABLE_SOURCES.items():
        got = _ids(
            world,
            "SELECT s.id FROM sources s JOIN notebooks n ON n.id = s.notebook_id "
            "WHERE n.id IN (?, ?) AND "
            f"{memory_sql.memory_source_readable('s')} "
            "AND s.status = ? AND s.id IN (SELECT source_id FROM knowledge_objects "
            "WHERE notebook_id IN (?, ?))",
            (cases.NOTEBOOK, cases.NOTEBOOK2, viewer, "uploaded",
             cases.NOTEBOOK, cases.NOTEBOOK2),
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
            "WHERE o.status = ? AND "
            f"{memory_sql.foreign_memory_object_excluded('o')} AND o.object_type IN (?, ?)",
            ("approved", viewer, "concept", "claim"),
        )
        assert got == expected, viewer


# ------------------------------------------------------ 两个后端与既有常量不漂移
_PUBLIC_FRAGMENTS = (
    "memory_source_readable",
    "foreign_memory_object_excluded",
    "foreign_memory_relation_excluded",
    "memory_derived_object",
    "memory_derived_relation",
    "no_memory_member_cluster",
    "memory_derived_in_notebook",
    "memory_cluster",
    "no_memory_cluster",
    "memory_seed_cluster",
    "cluster_seed_object_id",
)

#: 簇片段里 `cluster_seed_object_id` 的铸造形状字面量(`kg_merge.seed_or_unique`)。
_SEED_SHAPE_LITERALS = {"K-~ko-", "K", "-~ko-"}


def _public_names(module) -> set[str]:
    return {
        name
        for name, member in vars(module).items()
        if not name.startswith("_")
        and getattr(member, "__module__", module.__name__) == module.__name__
        and (callable(member) or name.isupper())
    }


def test_both_backends_declare_the_same_fragments_with_identical_text():
    """双后端同修(仿 `test_access_sql_contract.py` 的镜像守卫):public 符号集合相等,
    每个片段把 `%s` 换成 `?` 后逐字相同 —— 一侧漏改而 PG 泳道又没跑,就是两种部署
    对「谁能读 Memory」给出不同答案,而这种分叉在单后端测试里看不见。"""
    assert _public_names(memory_sql) == _public_names(pg_memory_sql)
    assert memory_sql.MEMORY_SOURCE_TYPE == pg_memory_sql.MEMORY_SOURCE_TYPE
    for name in _PUBLIC_FRAGMENTS:
        for alias in ("x1", "Outer_2"):
            pg_text = getattr(pg_memory_sql, name)(alias)
            assert "?" not in pg_text
            assert pg_text.replace("%s", "?") == getattr(memory_sql, name)(alias), name
    # One documented difference: SQLite's unary ``+`` index hint (no sqlite_stat1 there,
    # see the fragment's docstring); everything else is byte-identical.
    sqlite_keys = memory_sql.memory_member_cluster_keys()
    assert sqlite_keys.count("+mc.notebook_id") == 1
    assert (
        pg_memory_sql.memory_member_cluster_keys().replace("%s", "?")
        == sqlite_keys.replace("+mc.notebook_id", "mc.notebook_id")
    )
    assert pg_memory_sql.memory_source_type_predicate() == memory_sql.memory_source_type_predicate()
    assert pg_memory_sql.memory_source_type_predicate("t.k") == memory_sql.memory_source_type_predicate("t.k")


def test_memory_type_literal_is_single_sourced_and_matches_source_store():
    """`'memory'` 只在 `MEMORY_SOURCE_TYPE` 出现一次:每个片段里所有带引号的字面量都是它;
    且它渲染出的未限定谓词与两个 `source_store` 各自的常量相同 —— 在 `source_store`
    改为 import 本模块之前,任何一边改了 Memory 来源的类型判据,这里当场红。"""
    quoted = re.compile(r"'([^']*)'")
    for module in (memory_sql, pg_memory_sql):
        for name in _PUBLIC_FRAGMENTS:
            text = getattr(module, name)("x1")
            literals = set(quoted.findall(text))
            if name == "cluster_seed_object_id":
                assert literals == _SEED_SHAPE_LITERALS, (module, name)
            elif name in {"memory_cluster", "no_memory_cluster", "memory_seed_cluster"}:
                assert literals == {module.MEMORY_SOURCE_TYPE} | _SEED_SHAPE_LITERALS
            else:
                assert literals == {module.MEMORY_SOURCE_TYPE}, (module, name)
    assert sqlite_source_store.MEMORY_SOURCE_TYPE_PREDICATE == memory_sql.memory_source_type_predicate()
    assert pg_source_store.MEMORY_SOURCE_TYPE_PREDICATE == pg_memory_sql.memory_source_type_predicate()


# ------------------------------------------------------ query_store 旧常量归一
class _RecordingDatabase:
    """Stands in for either backend's database: records each statement and
    answers no rows, so a store method's SQL text can be checked offline."""

    def __init__(self):
        self.statements: list = []

    def connect(self):
        recorder = self

        class _Rows:
            def fetchall(self):
                return []

        class _Connection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                recorder.statements.append((sql, tuple(params)))
                return _Rows()

        return _Connection()


def test_source_stores_decide_memory_readability_with_the_shared_fragment():
    """PR-A (#806 r1): ``hidden_source_ids`` — the read the KG viewer rule
    derives "another member's Memory" from — renders
    ``memory_source_readable('s')`` on both backends instead of a hand-written
    copy, and the Memory type predicate is the fragment's rendering."""
    for store_module, fragments in (
        (sqlite_source_store, memory_sql), (pg_source_store, pg_memory_sql),
    ):
        database = _RecordingDatabase()
        store = store_module.SourceStore(database, now=lambda: NOW)
        assert store.hidden_source_ids("nb", "u-alice") == []
        (sql, params), = database.statements
        assert fragments.memory_source_readable("s") in sql, (store_module, sql)
        assert "memory_items m " not in sql, sql
        assert params == ("nb", "u-alice")
        assert store_module.MEMORY_SOURCE_TYPE_PREDICATE == (
            fragments.memory_source_type_predicate())


@pytest.mark.parametrize("viewer", list(cases.READABLE_SOURCES))
def test_hidden_source_ids_follow_the_readability_matrix(world, viewer):
    """The same golden matrix as the fragment itself: a viewer's hidden
    sources are the notebook's Knowhow projections plus exactly the Memory
    sources ``memory_source_readable`` admits for that viewer (orphans and
    Memory without an owner row for nobody)."""
    store = sqlite_source_store.SourceStore(world, now=lambda: NOW)
    hidden_types = {s[0] for s in cases.SOURCES if s[1] in ("memory", "knowhow")}
    assert set(store.hidden_source_ids(cases.NOTEBOOK, viewer)) == (
        hidden_types & cases.READABLE_SOURCES[viewer])


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
