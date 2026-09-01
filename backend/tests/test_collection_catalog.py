"""集合地图(collection catalog)—— 类型化集合的低成本计数视图。

覆盖:白名单过滤、per-source 计数与来源计数、两层缓存(notebook 指纹 / per-source
变更信号)、LRU 有界、地图文本形态与硬截断、作用域(挂载 base 计入、未挂载不越权)、
KG/knowhow 计数,以及「索引不可绕」——真实执行的 SQL 必须走
idx_source_elements_source_type。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import collection_catalog
from app.services.collection_catalog import (
    COLLECTION_MAP_MAX_CHARS,
    ENUMERABLE_ELEMENT_KINDS,
    CollectionMap,
    ElementKindCount,
    render_collection_map,
)
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


NOW = "2026-07-28T00:00:00+08:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    return instance


def _add_source(repo, notebook_id, source_id, kinds, *, updated_at=NOW):
    """一个 source + 每个 (element_type, n) 对应 n 个元素。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, "t", "markdown", "extracted", "extracted",
             NOW, updated_at),
        )
        index = 0
        for element_type, count in kinds:
            for _ in range(count):
                index += 1
                db.execute(
                    "INSERT INTO source_elements (id,source_id,element_type,"
                    "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                    (f"el-{source_id}-{index}", source_id, element_type, "p1",
                     "body", "{}", NOW),
                )


def _touch_source(repo, source_id, updated_at):
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET updated_at=? WHERE id=?", (updated_at, source_id)
        )


def _add_kg_object(repo, notebook_id, object_id, object_type, status="approved"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,"
            "evidence,status,owner,last_reviewed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, notebook_id, "", object_type, json.dumps({"name": object_id}),
             "[]", status, "", "", NOW, NOW),
        )
    # 裸 INSERT 不 bump kg_mutation_seq,而 KG 计数两侧都是 seq 门控的
    # (store 的 #245 memo + 本模块的 L3)——测试自己捅了库,就得自己敲这两个
    # 安全阀,与两处 docstring 的约定一致。
    repo._runtime.queries.invalidate_knowledge_counts(notebook_id)
    repo.collection_catalog.invalidate()


def _bump_kg_seq(repo, notebook_id):
    """真实写路径唯一推进 kg_mutation_seq 的地方(mark_dirty)。"""
    with repo._write() as db:
        repo._runtime.unified_kg.mark_dirty(db, notebook_id, NOW)


def _catalog(repo):
    return repo.collection_catalog


# ------------------------------------------------------------------ 白名单

def test_only_whitelisted_kinds_are_counted(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [
        ("formula", 3),
        ("table", 1),
        ("image", 2),
        ("code_block", 4),
        # 以下都不是可枚举 kind:正文体量最大,knowhow_cell 是投影产物。
        ("paragraph", 50),
        ("heading", 7),
        ("page_text", 9),
        ("knowhow_cell", 5),
    ])
    result = _catalog(repo).collection_map(notebook.id)
    assert {item.kind: item.count for item in result.elements} == {
        "formula": 3, "table": 1, "image": 2, "code_block": 4,
    }
    # 形态稳定:四个 kind 恒在,且顺序固定。
    assert tuple(item.kind for item in result.elements) == ENUMERABLE_ELEMENT_KINDS


def test_per_source_counts_and_source_tally(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 3), ("table", 2)])
    _add_source(repo, notebook.id, "s2", [("formula", 9)])
    _add_source(repo, notebook.id, "s3", [("paragraph", 4)])   # 无可枚举元素
    result = _catalog(repo).collection_map(notebook.id)
    counts = {item.kind: (item.count, item.sources) for item in result.elements}
    assert counts["formula"] == (12, 2)
    assert counts["table"] == (2, 1)
    assert counts["image"] == (0, 0)


def test_empty_notebook_is_all_zero(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    result = _catalog(repo).collection_map(notebook.id)
    assert all(item.count == 0 and item.sources == 0 for item in result.elements)
    assert all(count == 0 for _type, count in result.kg_objects)
    assert result.knowhow_tables == 0


# -------------------------------------------------------------------- 缓存

def _count_element_queries(repo, monkeypatch):
    """记录 element_type_count_rows 每次被问到的 source_id 列表。"""
    store = repo._runtime.source_store
    original = store.element_type_count_rows
    calls: list[list[str]] = []

    def spy(db, source_ids, element_types):
        ids = list(source_ids)
        calls.append(ids)
        return original(db, ids, element_types)

    monkeypatch.setattr(store, "element_type_count_rows", spy)
    return calls


def test_second_build_issues_no_element_query(repo, monkeypatch):
    """notebook 指纹未变 → 第二次构建零元素查询(连 per-source 缓存都不必查)。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 3)])
    catalog = _catalog(repo)
    first = catalog.collection_map(notebook.id)
    calls = _count_element_queries(repo, monkeypatch)
    second = catalog.collection_map(notebook.id)
    assert calls == []
    assert second.elements == first.elements


def test_only_the_changed_source_is_requeried(repo, monkeypatch):
    """指纹变了但只有一个源的变更信号变 → per-source 缓存兜住其余源,
    元素查询只问那一个源。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 3)])
    _add_source(repo, notebook.id, "s2", [("formula", 1)])
    catalog = _catalog(repo)
    catalog.collection_map(notebook.id)
    calls = _count_element_queries(repo, monkeypatch)
    _touch_source(repo, "s2", "2026-07-28T09:00:00+08:00")
    catalog.collection_map(notebook.id)
    assert calls == [["s2"]]


def _replace_elements(repo, source_id, count, *, clear_chunked_at, touch_updated_at):
    """复刻 reparse 的写事务形状:换代 elements + 调用方选择的信号动作。"""
    with repo._write() as db:
        db.execute("DELETE FROM source_elements WHERE source_id=?", (source_id,))
        for index in range(count):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,location_label,"
                "text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-{source_id}-new-{index}", source_id, "formula", "p1",
                 "body", "{}", NOW),
            )
        if clear_chunked_at:
            db.execute(
                "UPDATE sources SET chunked_at=NULL WHERE id=?", (source_id,)
            )
        if touch_updated_at:
            db.execute(
                "UPDATE sources SET updated_at=? WHERE id=?",
                ("2026-07-28T10:00:00+08:00", source_id),
            )


def test_reparse_clearing_chunked_at_invalidates_the_count(repo):
    """已分块来源被 reparse:``replace_elements`` 与 ``clear_chunked_at`` 同一写
    事务落地,``updated_at`` 尚未动——计数必须当场重算。这正是变更信号里
    chunked_at 的意义:元素换代与信号翻转原子同步,不留「旧计数看起来新鲜」的窗口。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 1)])
    with repo._write() as db:                      # 该来源已成功分块过
        db.execute("UPDATE sources SET chunked_at=? WHERE id=?", (NOW, "s1"))
    catalog = _catalog(repo)
    assert catalog.collection_map(notebook.id).element_count("formula") == 1

    _replace_elements(repo, "s1", 4, clear_chunked_at=True, touch_updated_at=False)
    assert catalog.collection_map(notebook.id).element_count("formula") == 4


def test_status_transition_invalidates_the_count(repo):
    """第二条信号来源:任何 ``set_status`` 都动 updated_at。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 1)])
    catalog = _catalog(repo)
    assert catalog.collection_map(notebook.id).element_count("formula") == 1

    _replace_elements(repo, "s1", 6, clear_chunked_at=False, touch_updated_at=True)
    assert catalog.collection_map(notebook.id).element_count("formula") == 6


def test_replace_elements_alone_invalidates_the_count(repo):
    """真正的元素写入口 ``replace_elements`` 自己就翻转信号(codex 第 3 轮 P1)。

    以前首次解析的来源 ``chunked_at`` 本就是 NULL、清空是空操作,信号要等到
    下一次 ``set_status`` 才动——那是一次 LLM 调用之后,期间地图把这个来源数
    成 0。现在 ``replace_elements`` 在**同一个写事务**里把 ``updated_at`` 推到
    新元素所带的时刻,窗口不再是「已登记的低报」,而是不存在。

    这里刻意**不**清 chunked_at、**不**改状态:两条旧路径都被排除后,还能收敛
    就只可能是 replace_elements 自己做的。
    """
    from app.repositories.ports import SourceElementWrite

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 1)])
    catalog = _catalog(repo)
    assert catalog.collection_map(notebook.id).element_count("formula") == 1

    store = repo._runtime.source_store
    with repo._connect() as db:
        before = {row[0]: row[1]
                  for row in store.source_change_signal_rows(db, notebook.id)}
    later = "2026-07-29T10:00:00.123456+08:00"
    with repo._write() as db:
        store.replace_elements(
            db, "s1",
            [
                SourceElementWrite(
                    id=f"el-fresh-{index}", element_type="formula",
                    location_label="p1", text="E", metadata={},
                )
                for index in range(5)
            ],
            created_at=later,
        )
    with repo._connect() as db:
        after = {row[0]: row[1]
                 for row in store.source_change_signal_rows(db, notebook.id)}
    assert after["s1"] != before["s1"], "信号必须随元素换代当场翻转"
    assert catalog.collection_map(notebook.id).element_count("formula") == 5


def test_deleted_source_drops_out_of_the_scope_sum(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 2)])
    _add_source(repo, notebook.id, "s2", [("formula", 5)])
    catalog = _catalog(repo)
    assert catalog.collection_map(notebook.id).element_count("formula") == 7
    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE id=?", ("s2",))
    assert catalog.collection_map(notebook.id).element_count("formula") == 2


def test_notebook_memo_alone_serves_a_wiped_source_cache(repo, monkeypatch):
    """L2 必须自己就能兜住:清空 L1 之后指纹仍命中 → 零元素查询。
    没有这一层,几万源的挂载库一旦 L1 被淘汰就会整库重扫。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(3):
        _add_source(repo, notebook.id, f"s{index}", [("formula", 2)])
    catalog = _catalog(repo)
    first = catalog.collection_map(notebook.id)
    catalog._source_counts.clear()          # L1 全丢,只剩 L2 指纹
    calls = _count_element_queries(repo, monkeypatch)
    second = catalog.collection_map(notebook.id)
    assert calls == []
    assert second.elements == first.elements


def test_element_counts_span_every_query_batch(repo):
    """批处理守卫:源清单跨批边界时,最后一批里的源也必须被计入。
    「只跑第一批」的变异会让 tail 源丢失 → 红。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-head", [("formula", 3)])
    _add_source(repo, notebook.id, "s-tail", [("formula", 5)])
    store = repo._runtime.source_store
    # 首尾各放一个真实来源,中间用不存在的 id 填充到跨批(SQLite 批宽 =
    # IN_CHUNK - len(kinds) = 896);断言真的跨了批,批宽改了这里会先红。
    padded = ["s-head"] + [f"pad-{index}" for index in range(1200)] + ["s-tail"]
    assert len(padded) > store.IN_CHUNK
    with repo._connect() as db:
        rows = store.element_type_count_rows(db, padded, ENUMERABLE_ELEMENT_KINDS)
    assert sorted(rows) == [
        ("s-head", "formula", 3),
        ("s-tail", "formula", 5),
    ]


def test_source_count_cache_is_lru_bounded(repo, monkeypatch):
    monkeypatch.setattr(collection_catalog, "_MAX_CACHED_SOURCES", 2)
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(5):
        _add_source(repo, notebook.id, f"s{index}", [("formula", 1)])
    catalog = _catalog(repo)
    assert catalog.collection_map(notebook.id).element_count("formula") == 5
    assert len(catalog._source_counts) == 2


def test_notebook_memo_is_lru_bounded(repo, monkeypatch):
    monkeypatch.setattr(collection_catalog, "_MAX_CACHED_NOTEBOOKS", 1)
    catalog = _catalog(repo)
    first = repo.create_notebook(NotebookCreate(name="a"))
    second = repo.create_notebook(NotebookCreate(name="b"))
    catalog.collection_map(first.id)
    catalog.collection_map(second.id)
    assert list(catalog._notebook_counts) == [second.id]


def test_source_cache_evicts_least_recently_used_not_arbitrary(repo, monkeypatch):
    """淘汰顺序必须是 LRU:最近被命中过的条目要活下来。没有 move_to_end,
    OrderedDict 退化成 FIFO,活跃源会被自己的热度反过来淘汰掉。"""
    monkeypatch.setattr(collection_catalog, "_MAX_CACHED_SOURCES", 2)
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    _add_source(repo, notebook.id, "s-old", [("formula", 1)])
    _add_source(repo, notebook.id, "s-mid", [("formula", 1)])
    catalog = _catalog(repo)
    catalog.collection_map(notebook.id)                 # 装入 s-old, s-mid
    assert list(catalog._source_counts) == ["s-old", "s-mid"]

    # 再建一次(指纹未变会短路 L2),故直接走 L1 读取路径重新命中 s-old,
    # 把它顶到最新端。
    with repo._connect() as db:
        signals = repo._runtime.source_store.source_change_signal_rows(db, notebook.id)
        catalog._per_source_counts(db, [item for item in signals if item[0] == "s-old"])
    assert list(catalog._source_counts) == ["s-mid", "s-old"]

    _add_source(repo, other.id, "s-new", [("formula", 1)])
    catalog.collection_map(other.id)                    # 触发淘汰
    assert list(catalog._source_counts) == ["s-old", "s-new"]   # s-mid 最久未用


def test_failed_element_query_caches_nothing(repo, monkeypatch):
    """单源查询失败不得留下坏值:异常上抛,两层缓存都保持空,
    修复后重算得到正确计数。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 3)])
    catalog = _catalog(repo)
    store = repo._runtime.source_store
    original = store.element_type_count_rows
    monkeypatch.setattr(
        store,
        "element_type_count_rows",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(RuntimeError):
        catalog.collection_map(notebook.id)
    assert catalog._source_counts == {}
    assert catalog._notebook_counts == {}
    monkeypatch.setattr(store, "element_type_count_rows", original)
    assert catalog.collection_map(notebook.id).element_count("formula") == 3


def test_out_of_whitelist_kind_from_the_store_fails_loudly(repo, monkeypatch):
    """端口违约(返回白名单外的 kind)必须响亮失败,不得被静默吞掉:
    容错地忽略它会让「白名单只在 SQL 里」这条守卫彻底失效——SQL 里的
    过滤被删掉时计数反而照旧正确,没有任何测试会红。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 1)])
    monkeypatch.setattr(
        repo._runtime.source_store,
        "element_type_count_rows",
        lambda *a, **k: [("s1", "heading", 7)],
    )
    with pytest.raises(KeyError):
        _catalog(repo).collection_map(notebook.id)


# -------------------------------------------------------------------- 作用域

def test_mounted_base_counts_into_scope(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    _add_source(repo, notebook.id, "s1", [("formula", 2)])
    _add_source(repo, base.id, "b1", [("formula", 5), ("image", 1)])
    catalog = _catalog(repo)
    assert catalog.collection_map(notebook.id).element_count("formula") == 2

    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    mounted = catalog.collection_map(notebook.id)
    assert set(mounted.notebook_ids) == {notebook.id, base.id}
    assert mounted.element_count("formula") == 7
    assert mounted.element_count("image") == 1


def test_unmounted_library_never_leaks_into_scope(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    repo.mark_notebook_base(other.id)
    _add_source(repo, other.id, "o1", [("formula", 11)])
    result = _catalog(repo).collection_map(notebook.id)
    assert result.notebook_ids == (notebook.id,)
    assert result.element_count("formula") == 0


def test_downgraded_cross_owner_base_falls_out_of_scope(repo):
    """挂载边不是授权凭证:别人的公共库被降级后,``mount_sql`` 的有效性谓词
    当场判它失效,地图必须跟着掉下去——不能继续许诺检索已经够不着的集合。
    作用域走的就是 Ask 联邦那套参与集解析,所以 notebook_ids 必须逐字等于它。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    other = repo.create_user("a00900101", "pw")   # created_by 有 FK REFERENCES users(id)
    with repo._write() as db:
        db.execute(
            "UPDATE notebooks SET created_by=? WHERE id=?", (other.id, base.id)
        )
    repo.mark_notebook_base(base.id)
    _add_source(repo, base.id, "b1", [("formula", 4)])
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    catalog = _catalog(repo)

    mounted = catalog.collection_map(notebook.id)
    assert mounted.element_count("formula") == 4
    with repo._connect() as db:
        assert list(mounted.notebook_ids) == (
            repo._runtime.notebook_store.participant_ids(db, notebook.id)
        )

    repo.set_notebook_personal(base.id)            # 降级 → 跨 owner 边即刻失效
    downgraded = catalog.collection_map(notebook.id)
    assert downgraded.notebook_ids == (notebook.id,)
    assert downgraded.element_count("formula") == 0


# ------------------------------------------------------------- KG / knowhow

def test_kg_object_counts_sum_over_scope_and_skip_deprecated(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    _add_kg_object(repo, notebook.id, "o1", "concept")
    _add_kg_object(repo, notebook.id, "o2", "claim")
    _add_kg_object(repo, base.id, "o3", "concept")
    _add_kg_object(repo, notebook.id, "o4", "concept", status="deprecated")
    # 非核心四类不进地图。
    _add_kg_object(repo, notebook.id, "o5", "rule")
    counts = dict(_catalog(repo).collection_map(notebook.id).kg_objects)
    assert counts == {"concept": 2, "claim": 1, "formula": 0, "procedure": 0}


def _count_kg_queries(repo, monkeypatch):
    queries = repo._runtime.queries
    original = queries.knowledge_type_count_rows
    calls: list[str] = []

    def spy(db, notebook_id, statuses):
        calls.append(notebook_id)
        return original(db, notebook_id, statuses)

    monkeypatch.setattr(queries, "knowledge_type_count_rows", spy)
    return calls


def test_kg_counts_are_memoized_on_the_mutation_seq(repo, monkeypatch):
    """``knowledge_type_count_rows`` 曾在 PG 侧是实时 GROUP BY(如今两后端都有
    store 级 seq memo),但本模块的计数缓存仍独立成立:它缓存的是 collection_map
    的**组装结果**,不依赖底层 store 是否命中自己的 memo。第二次构建应零 KG 查询。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_kg_object(repo, notebook.id, "o1", "concept")
    catalog = _catalog(repo)
    first = catalog.collection_map(notebook.id)
    calls = _count_kg_queries(repo, monkeypatch)
    second = catalog.collection_map(notebook.id)
    assert calls == []
    assert second.kg_objects == first.kg_objects


def test_kg_memo_recomputes_when_the_mutation_seq_moves(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_kg_object(repo, notebook.id, "o1", "concept")
    catalog = _catalog(repo)
    assert dict(catalog.collection_map(notebook.id).kg_objects)["concept"] == 1

    # KG 写路径推进 seq 而**完全不动 sources 行**——正因如此 KG 计数不能
    # 挂在 L2 的 sources 指纹上。
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,"
            "evidence,status,owner,last_reviewed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("o2", notebook.id, "", "concept", json.dumps({"name": "o2"}), "[]",
             "approved", "", "", NOW, NOW),
        )
    repo._runtime.queries.invalidate_knowledge_counts(notebook.id)   # store 层安全阀
    _bump_kg_seq(repo, notebook.id)
    assert dict(catalog.collection_map(notebook.id).kg_objects)["concept"] == 2


def test_kg_memo_is_lru_bounded(repo, monkeypatch):
    monkeypatch.setattr(collection_catalog, "_MAX_CACHED_NOTEBOOKS", 1)
    catalog = _catalog(repo)
    first = repo.create_notebook(NotebookCreate(name="a"))
    second = repo.create_notebook(NotebookCreate(name="b"))
    catalog.collection_map(first.id)
    catalog.collection_map(second.id)
    assert list(catalog._kg_counts) == [second.id]


def test_kg_counts_use_epoch_not_just_raw_seq_after_a_delete_and_reingest(repo):
    """R1 (P2-2, post-review, batch-3-W1 PR-2): ``delete_notebook_kg`` resets
    ``unified_kg_state.kg_mutation_seq`` back to 0 in place (design doc Sec 3
    Option C) rather than deleting the row, so a notebook that is cleared and
    then reingested can climb the RAW seq counter back up to the exact value
    it held before the delete. If ``_notebook_kg_counts``'s L3 memo key were
    still a bare seq int, the post-delete-and-reingest read would silently
    serve the stale PRE-delete counts the moment the raw counters realign —
    the same aliasing hazard closed everywhere else in this PR.
    ``kg_reset_epoch`` (bumped only by the delete) is what still tells the
    two states apart, so the memo key is ``(kg_reset_epoch, kg_mutation_seq)``.

    变异锚点:把 ``_notebook_kg_counts`` 的
    ``version = (int(row[3]), int(row[0]))`` 改回裸 ``version = int(row[0])``
    (P2-2 之前的形态),本条必须报红——预热的旧计数(2 个 concept)被当命中直接
    返回,而不是重算出 delete-and-reingest 之后的真实计数(1 个 concept)。
    """
    notebook = repo.create_notebook(NotebookCreate(name="alias-guard"))
    _add_kg_object(repo, notebook.id, "o1", "concept")
    _add_kg_object(repo, notebook.id, "o2", "concept")
    _bump_kg_seq(repo, notebook.id)
    catalog = _catalog(repo)
    before = dict(catalog.collection_map(notebook.id).kg_objects)
    assert before["concept"] == 2

    with repo._connect() as db:
        raw_seq_before_delete = int(
            repo._runtime.unified_kg.graph_seq_row(db, notebook.id)[0]
        )

    repo.delete_notebook_kg(notebook.id)

    # Reingest exactly ONE surviving object (bare INSERT — deliberately NOT
    # going through ``_add_kg_object``'s manual ``.invalidate()`` call, since
    # that would sidestep the very seq-keyed memo this test exercises).
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,"
            "payload,evidence,status,owner,last_reviewed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("o3", notebook.id, "", "concept", json.dumps({"name": "o3"}),
             "[]", "approved", "", "", NOW, NOW),
        )
    repo._runtime.queries.invalidate_knowledge_counts(notebook.id)  # store 层安全阀
    # Force the raw seq counter back to the EXACT pre-delete value — the
    # aliasing scenario design doc Sec 3 Option C exists to close. Only
    # kg_reset_epoch (already bumped by the delete above) still distinguishes
    # this post-delete-and-reingest state from the pre-delete one.
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=? WHERE notebook_id=?",
            (raw_seq_before_delete, notebook.id),
        )

    after = dict(catalog.collection_map(notebook.id).kg_objects)
    assert after["concept"] == 1, (
        "post-delete-and-reingest count must reflect the ONE surviving "
        "object, not the stale pre-delete cache the raw seq alone would "
        f"alias onto (got {after!r})"
    )


def test_knowhow_table_count_covers_scope(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    columns = [{"name": "Topic", "role": "anchor"}]
    repo.create_knowhow_table(notebook.id, "T1", "", columns)
    repo.create_knowhow_table(base.id, "T2", "", columns)
    catalog = _catalog(repo)
    assert catalog.collection_map(notebook.id).knowhow_tables == 1
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    assert catalog.collection_map(notebook.id).knowhow_tables == 2


# -------------------------------------------------------------------- 指纹

def test_fingerprint_ignores_row_order(repo):
    """指纹必须与行序无关:``sources`` 查询没有 ORDER BY,SQLite/PostgreSQL
    都可以换返回顺序(甚至同一库不同计划就会变),不排序会让指纹随机漂,
    L2 永远 miss、每次建图都全库重扫。"""
    pairs = [("s1", "sig-a"), ("s2", "sig-b"), ("s3", "sig-c")]
    baseline = collection_catalog.signal_fingerprint(pairs)
    assert collection_catalog.signal_fingerprint(list(reversed(pairs))) == baseline
    assert collection_catalog.signal_fingerprint([pairs[1], pairs[2], pairs[0]]) == baseline
    # 内容真的变了则必须变。
    assert collection_catalog.signal_fingerprint(
        [("s1", "sig-a"), ("s2", "sig-b"), ("s3", "sig-CHANGED")]
    ) != baseline
    # 分隔符不可伪造:拼接歧义会让不同集合撞同一指纹。
    assert collection_catalog.signal_fingerprint([("s1", "sig-a")]) != (
        collection_catalog.signal_fingerprint([("s1|sig-a", "")])
    )


def test_created_at_key_does_not_change_the_fingerprint():
    """来源清单让信号行多带一个 ``created_at`` 排序键,指纹**不得**因此变化。

    两件事:
    * 值不进摘要——创建时间对活着的源永不改变,把它哈希进去只会加宽 token;而
      指纹变化的含义是「整库重数」,一个缓存键不该为一个影响不到被缓存内容的
      理由而移动;
    * 与加字段之前**逐字节相同**——不然这次改动会让所有已部署库的 L1/L2 全量
      失效一次(表现为上线后第一次建图把每个库都重扫一遍)。
    """
    pairs = [("s1", "sig-a"), ("s2", "sig-b")]
    triples = [("s1", "sig-a", "2026-07-02"), ("s2", "sig-b", "2026-07-01")]
    assert collection_catalog.signal_fingerprint(triples) == (
        collection_catalog.signal_fingerprint(pairs)
    )
    # 同一批源、不同创建时间 ⇒ 指纹相同(排序键不是内容)。
    assert collection_catalog.signal_fingerprint(
        [("s1", "sig-a", "1999-01-01"), ("s2", "sig-b", "2030-12-31")]
    ) == collection_catalog.signal_fingerprint(triples)
    # 信号本身变了仍必须变(别把「忽略第三个字段」写成「只看第一个字段」)。
    assert collection_catalog.signal_fingerprint(
        [("s1", "sig-CHANGED", "2026-07-02"), ("s2", "sig-b", "2026-07-01")]
    ) != collection_catalog.signal_fingerprint(triples)


# ------------------------------------------------------------------ 地图文本

def _map(elements, kg, knowhow, sources=0):
    return CollectionMap(
        notebook_ids=("nb",),
        elements=tuple(
            ElementKindCount(kind=kind, count=count, sources=sources_count)
            for kind, count, sources_count in elements
        ),
        kg_objects=tuple(kg),
        knowhow_tables=knowhow,
        sources=sources,
    )


def test_render_matches_the_documented_shape():
    """逐字对齐 spec §2.2 给出的地图形态,包括「单来源不写 (1 sources)」——
    非零计数无后缀本身就唯一地表示该 kind 只在一个来源里。"""
    text = render_collection_map(_map(
        [("formula", 12, 3), ("table", 5, 2), ("image", 0, 0), ("code_block", 7, 1)],
        [("concept", 1234), ("claim", 567), ("formula", 89), ("procedure", 45)],
        2,
        7,
    ))
    assert text == (
        "[Collections in scope] elements: formula 12 (3 sources), "
        "table 5 (2 sources), image 0, code_block 7 | "
        "KG objects: concept 1234, claim 567, formula 89, procedure 45 | "
        "knowhow tables: 2 | sources: 7"
    )
    assert len(text) <= COLLECTION_MAP_MAX_CHARS


def test_render_zero_shape_is_stable():
    text = render_collection_map(_map(
        [(kind, 0, 0) for kind in ENUMERABLE_ELEMENT_KINDS],
        [("concept", 0), ("claim", 0), ("formula", 0), ("procedure", 0)],
        0,
        0,
    ))
    assert text == (
        "[Collections in scope] elements: formula 0, table 0, image 0, code_block 0 | "
        "KG objects: concept 0, claim 0, formula 0, procedure 0 | "
        "knowhow tables: 0 | sources: 0"
    )


def test_render_is_hard_capped():
    huge = 10 ** 60
    text = render_collection_map(_map(
        [(kind, huge, huge) for kind in ENUMERABLE_ELEMENT_KINDS],
        [(name, huge) for name in ("concept", "claim", "formula", "procedure")],
        huge,
        huge,
    ))
    assert len(text) == COLLECTION_MAP_MAX_CHARS
    assert text.endswith("…")


def test_render_carries_no_user_content(repo):
    notebook = repo.create_notebook(NotebookCreate(name="机密项目"))
    _add_source(repo, notebook.id, "s1", [("formula", 1)])
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET title=? WHERE id=?", ("绝密来源标题", "s1")
        )
        db.execute(
            "UPDATE source_elements SET text=? WHERE source_id=?",
            ("绝密正文", "s1"),
        )
    text = _catalog(repo).collection_map_text(notebook.id)
    assert "绝密" not in text and "机密" not in text
    assert text.startswith("[Collections in scope] elements: formula 1,")


# --------------------------------------------------------- 索引不可绕(EXPLAIN)

def test_element_count_query_is_served_by_the_typed_index(repo):
    """真实执行的元素计数 SQL 必须走 idx_source_elements_source_type。

    捕获的是**实际执行**的语句(sqlite3 trace callback 给的是参数展开后的
    SQL),不是测试里另抄一份查询文本——所以查询形状一旦漂到全表扫,这条会红。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 2), ("paragraph", 3)])
    with repo._connect() as db:
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            repo._runtime.source_store.element_type_count_rows(
                db, ["s1"], ENUMERABLE_ELEMENT_KINDS
            )
        finally:
            db.set_trace_callback(None)
        executed = [item for item in statements if "source_elements" in item]
        assert len(executed) == 1
        plan = " ".join(
            str(row[3])
            for row in db.execute("EXPLAIN QUERY PLAN " + executed[0]).fetchall()
        )
    # 逐字钉住覆盖索引与两段键:少了 element_type 段(白名单被去掉、或索引退回
    # 单列 source_id)就不是这条计划,守卫当场红。
    assert (
        "USING COVERING INDEX idx_source_elements_source_type "
        "(source_id=? AND element_type=?)"
    ) in plan, plan
    assert "SCAN source_elements" not in plan, plan


def test_source_signal_query_is_index_seeked(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s1", [("formula", 1)])
    with repo._connect() as db:
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            repo._runtime.source_store.source_change_signal_rows(db, notebook.id)
        finally:
            db.set_trace_callback(None)
        executed = [item for item in statements if "FROM sources" in item]
        assert len(executed) == 1
        plan = " ".join(
            str(row[3])
            for row in db.execute("EXPLAIN QUERY PLAN " + executed[0]).fetchall()
        )
    # 具体选中哪条 notebook_id 前缀索引不是契约(SQLite 可换);「不是全表扫」才是。
    assert "SEARCH sources USING INDEX idx_sources" in plan, plan
    assert "SCAN sources" not in plan, plan
