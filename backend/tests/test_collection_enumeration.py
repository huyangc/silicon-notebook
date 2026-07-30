"""集合枚举执行器(collection enumeration)—— 类型化集合的「清单层」。

覆盖:全集对照(跨源/跨批/跨 notebook,含挂载 base 与 Memory 派生源)、游标续跑
与一次跑到底等价、三种预算触顶形态与「恰好取尽不误报部分」、并发变更判定、
地图计数与枚举 returned 的逐字一致(元素 + KG 两侧)、coverage 语义、取消,
以及两条索引形状守卫与「白名单单一真源」的源码级守卫。
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

from app.core.ask_retrieval_policy import (
    ASK_RETRIEVAL_LIMITS,
    EXPLICIT_PARTIAL_OVERFLOW,
)
from app.models.schemas import NotebookCreate
from app.core.config import Settings
from app.services import collection_catalog, collection_enumeration
from app.services.cancellation import AskCancelled
from app.services.collection_catalog import (
    ENUMERABLE_ELEMENT_KINDS,
    ENUMERABLE_KG_OBJECT_TYPES,
)
from app.services.collection_enumeration import (
    DEFAULT_EXCERPT_CHARS,
    MAX_EVIDENCE_REFS,
    TRUNCATED_BUDGET,
    TRUNCATED_CONCURRENT_CHANGE,
    TRUNCATED_PAYLOAD,
    EnumerationBudget,
)
from app.services.embedding import FakeEmbedder
from app.services.kg.edge_schema import NODE_TYPES
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients


NOW = "2026-07-28T00:00:00+08:00"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    return instance


def _budget(**overrides):
    base = dict(
        page_size=25, max_rows=1_000, max_pages=50, max_payload_chars=256_000
    )
    base.update(overrides)
    return EnumerationBudget(**base)


def _add_source(
    repo, notebook_id, source_id, kinds, *, title="", source_type="markdown"
):
    """一个 source + 每个 (element_type, n) 对应 n 个元素。

    id 用 3 位零填充,让 ``(created_at, id)`` 的字典序与阅读序一致(不填充
    的话 "el-10" 会排在 "el-2" 前面)。**登记**:超过 999 个同源元素时这个
    对齐就失效,断言里的期望序要按字典序写——遍历顺序本身仍然是确定的,
    只是不再"好看"。真实数据的 element id 是随机的,本来就没有阅读序。
    """
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, title or source_id, source_type, "extracted",
             "extracted", NOW, NOW),
        )
        index = 0
        for element_type, count in kinds:
            for _ in range(count):
                index += 1
                db.execute(
                    "INSERT INTO source_elements (id,source_id,element_type,"
                    "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                    (f"el-{source_id}-{index:03d}", source_id, element_type,
                     f"p{index}", f"body {source_id} {index}", "{}", NOW),
                )


def _add_kg_object(repo, notebook_id, object_id, object_type, *,
                   status="approved", evidence=(), name="", source_id=""):
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,"
            "evidence,status,owner,last_reviewed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, notebook_id, source_id, object_type,
             json.dumps({"name": name or object_id, "section_path": "第一章>1.1"}),
             json.dumps([
                 {"source_id": "s", "source_title": "t", "element_id": element_id,
                  "element_type": "formula", "location_label": "p1",
                  "quoted_span": "q", "confidence": 1.0}
                 for element_id in evidence
             ]),
             status, "", "", NOW, NOW),
        )
    # 裸 INSERT 不 bump kg_mutation_seq;两侧计数都是 seq 门控的,测试自己捅库
    # 就得自己敲安全阀(与 collection_catalog 的两处 docstring 约定一致)。
    repo._runtime.queries.invalidate_knowledge_counts(notebook_id)
    repo.collection_catalog.invalidate()


def _bump_kg_seq(repo, notebook_id):
    """真实写路径唯一推进 kg_mutation_seq 的地方(mark_dirty)。"""
    with repo._write() as db:
        repo._runtime.unified_kg.mark_dirty(db, notebook_id, NOW)


def _enum(repo):
    return repo.collection_enumeration


def _element_ids(result):
    return [item.element_id for item in result.items]


def _sql_element_ids(repo, notebook_ids, kind):
    """全集对照:直接 SQL 取整个作用域的该 kind 元素,按枚举承诺的遍历序排。"""
    out = []
    with repo._connect() as db:
        for notebook_id in notebook_ids:
            source_ids = [
                row["id"] for row in db.execute(
                    "SELECT id FROM sources WHERE notebook_id=? ORDER BY id",
                    (notebook_id,),
                ).fetchall()
            ]
            for source_id in source_ids:
                out.extend(row["id"] for row in db.execute(
                    "SELECT id FROM source_elements WHERE source_id=? AND "
                    "element_type=? ORDER BY created_at, id",
                    (source_id, kind),
                ).fetchall())
    return out


# --------------------------------------------------------------- 全集对照

@pytest.mark.parametrize("page_size", [25, 2])
def test_full_scope_enumeration_equals_direct_sql(repo, page_size):
    """跨源、跨批、跨 notebook(含挂载 base)的完整枚举 == 直接 SQL 全集,
    顺序逐字相同。页大小 2 的这一档同时钉住自动翻页:「只跑第一页」的变异
    在这里就会红。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    _add_source(repo, notebook.id, "s-a", [("formula", 3), ("table", 2)])
    _add_source(repo, notebook.id, "s-b", [("paragraph", 9)])       # 无公式
    _add_source(repo, notebook.id, "s-c", [("formula", 2)])
    _add_source(repo, base.id, "b-a", [("formula", 4), ("image", 1)])

    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=page_size)
    )
    assert _element_ids(result) == _sql_element_ids(
        repo, [notebook.id, base.id], "formula"
    )
    assert result.coverage.returned == 9
    assert result.coverage.total == 9
    assert result.coverage.complete is True
    assert result.coverage.has_more is False
    assert result.coverage.truncated_reason == ""
    assert result.coverage.overflow_semantics == ""
    assert result.cursor is None
    # 作用域标注跟着来源走,tier 与参与集解析一致。
    assert {item.notebook_id for item in result.items} == {notebook.id, base.id}
    assert {item.tier for item in result.items} == {"personal", "base"}


def test_unmounted_library_never_leaks_into_enumeration(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    repo.mark_notebook_base(other.id)
    _add_source(repo, other.id, "o-a", [("formula", 5)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    assert result.items == ()
    assert result.coverage.complete is True and result.coverage.total == 0


def test_downgraded_cross_owner_base_falls_out_of_scope(repo):
    """挂载边不是授权凭证:公共库被降级后 ``mount_sql`` 判它失效,清单必须
    立刻不再列出它的元素——与地图同一套参与集解析。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    other = repo.create_user("a00900101", "pw")
    with repo._write() as db:
        db.execute("UPDATE notebooks SET created_by=? WHERE id=?", (other.id, base.id))
    repo.mark_notebook_base(base.id)
    _add_source(repo, base.id, "b-a", [("formula", 4)])
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    assert _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    ).coverage.returned == 4

    repo.set_notebook_personal(base.id)
    after = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    assert after.items == () and after.coverage.total == 0


def test_enumeration_spans_many_pages_within_one_call(repo):
    """一次调用在预算内自动翻页:页大小 2、40 个公式,一次拿全。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 40)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=2)
    )
    assert result.coverage.returned == 40
    assert result.coverage.complete is True
    assert _element_ids(result) == _sql_element_ids(repo, [notebook.id], "formula")


def test_extra_pages_counts_only_round_trips_past_the_first_page(repo):
    """``extra_pages`` = 非首页往返数,与 ``max_pages`` 约束的是同一件事。

    调用方(逐步推理)用它给 run 级页预算据实计费,所以它的语义必须钉死:每个源
    的**第一页免费**(那不是「额外」往返,是看见这个源的唯一办法),第 2 页起才计。
    没有它,调用方只能按「扫过行数 ÷ 页大小」折算上界,把一次实际发生的额外往返
    记成两次。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 3)])

    one_page = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=25)
    )
    assert one_page.coverage.returned == 3
    assert one_page.extra_pages == 0        # 单页取尽:零额外往返

    paged = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=1)
    )
    assert paged.coverage.returned == 3
    assert paged.extra_pages == 2           # 第 1 页免费,第 2、3 页各计一次

    # 每个源各自的第一页都免费:两个源各一条公式 → 一次额外往返都没有,
    # 尽管扫过的行数与上面「翻了两页」的场景相同。
    _add_source(repo, notebook.id, "s-b", [("formula", 1)])
    _add_source(repo, notebook.id, "s-c", [("formula", 1)])
    spread = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=1), source_id="s-b"
    )
    assert spread.coverage.returned == 1 and spread.extra_pages == 0


def test_kg_object_enumeration_reports_extra_pages(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(3):
        _add_kg_object(repo, notebook.id, f"ko-{index}", "concept")

    assert _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=25)
    ).extra_pages == 0
    assert _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=1)
    ).extra_pages == 2


def test_sources_without_the_kind_are_never_queried(repo, monkeypatch):
    """效率契约:只有地图计数 >0 的源才会被翻页。几万源的挂载库里,
    没有公式的那些源必须一条查询都不发。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 2)])
    for index in range(5):
        _add_source(repo, notebook.id, f"s-prose-{index}", [("paragraph", 3)])
    store = repo._runtime.source_store
    original = store.element_page_rows
    visited: list[str] = []

    def spy(db, source_id, element_type, after, limit):
        visited.append(source_id)
        return original(db, source_id, element_type, after, limit)

    monkeypatch.setattr(store, "element_page_rows", spy)
    _enum(repo).enumerate_elements(notebook.id, "formula", budget=_budget())
    assert set(visited) == {"s-a"}


def test_source_labels_are_fetched_in_one_batch(repo, monkeypatch):
    """标题查询按窗口批量,不是每源一次 —— 20 个源的一轮枚举只发一次。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(20):
        _add_source(repo, notebook.id, f"s-{index:03d}", [("formula", 1)])
    store = repo._runtime.source_store
    original = store.source_display_rows
    batches: list[int] = []

    def spy(db, source_ids):
        ids = list(source_ids)
        batches.append(len(ids))
        return original(db, ids)

    monkeypatch.setattr(store, "source_display_rows", spy)
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=100)
    )
    assert result.coverage.returned == 20
    assert batches == [20]
    assert all(item.source_title for item in result.items)


def test_plan_sources_are_memoized_on_the_signal_fingerprint(repo, monkeypatch):
    """L4:计划(非零源列表)按与 L2 相同的指纹 memo。没有它,大库上
    每一次枚举——包括每一次**续跑**——都要重算整库 per-source 计数,
    因为工作集根本装不进 L1。"""
    monkeypatch.setattr(collection_catalog, "_MAX_CACHED_SOURCES", 2)
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(6):
        _add_source(repo, notebook.id, f"s-{index:03d}", [("formula", 1)])
    first = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=2)
    )
    store = repo._runtime.source_store
    original = store.element_type_count_rows
    calls: list[int] = []

    def spy(db, source_ids, element_types):
        calls.append(len(list(source_ids)))
        return original(db, source_ids, element_types)

    monkeypatch.setattr(store, "element_type_count_rows", spy)
    resumed = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(), cursor=first.cursor
    )
    assert resumed.coverage.complete is True
    assert calls == []          # 续跑零计数查询

    # 信号一变,memo 必须失效重算。
    _add_source(repo, notebook.id, "s-late", [("formula", 1)])
    _enum(repo).enumerate_elements(notebook.id, "formula", budget=_budget())
    assert calls and sum(calls) > 0


# ------------------------------------------------------------------ 游标续跑

def test_cursor_resume_equals_one_shot(repo):
    """分三次带游标跑完 == 一次跑到底,顺序与内容逐字相同,且不重不漏。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    _add_source(repo, notebook.id, "s-a", [("formula", 4)])
    _add_source(repo, notebook.id, "s-b", [("formula", 3)])
    _add_source(repo, base.id, "b-a", [("formula", 5)])

    one_shot = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    collected: list[str] = []
    cursor = None
    for _round in range(10):
        page = _enum(repo).enumerate_elements(
            notebook.id, "formula", budget=_budget(max_rows=5), cursor=cursor
        )
        collected.extend(_element_ids(page))
        cursor = page.cursor
        if page.coverage.complete:
            break
        assert cursor is not None
    assert collected == _element_ids(one_shot)
    assert len(collected) == 12


def test_resume_after_a_source_appears_before_the_cursor_is_not_complete(repo):
    """P0 repro:两次动作之间隔着一次 LLM 反思,秒级窗口里新增了一个**排在
    游标之前**的源。位置游标永远走不回去,若不带开场作用域身份,续跑会把
    8/11 报成 complete=True——一个自信的错误答案。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 4)])
    _add_source(repo, notebook.id, "s-c", [("formula", 4)])
    first = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=5)
    )
    assert first.coverage.returned == 5 and first.cursor is not None

    _add_source(repo, notebook.id, "s-b", [("formula", 3)])   # 插在游标之前

    resumed = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(), cursor=first.cursor
    )
    assert resumed.coverage.complete is False
    assert resumed.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE
    assert resumed.items == ()          # 绝不静默从头重跑
    assert resumed.cursor is None


def test_kg_resume_after_a_walked_past_notebook_changes_is_not_complete(repo):
    """P0 repro(KG 侧):游标停在挂载 base 上时,active 库新增对象会被
    ``notebook_ids[index:]`` 整个跳过。开场 seq 向量把它抓住。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    for index in range(2):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    for index in range(4):
        _add_kg_object(repo, base.id, f"b{index}", "concept")

    first = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(max_rows=3)
    )
    assert first.cursor is not None and first.cursor.notebook_id == base.id

    _add_kg_object(repo, notebook.id, "o-late", "concept")     # 已走过的库
    _bump_kg_seq(repo, notebook.id)

    resumed = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(), cursor=first.cursor
    )
    assert resumed.coverage.complete is False
    assert resumed.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE
    assert resumed.items == ()
    assert resumed.cursor is None


def test_resume_chain_denominator_survives_resumption(repo):
    """续跑链末端要拿**累计** returned 对 total:只用本次 returned 比较,
    每一次续跑都会被误判成并发变更;完全不比较,则链条漏掉的行无人发现。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 5)])
    first = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=2)
    )
    second = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=2), cursor=first.cursor
    )
    third = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(), cursor=second.cursor
    )
    assert [page.coverage.returned for page in (first, second, third)] == [2, 2, 1]
    assert [page.coverage.returned_total for page in (first, second, third)] == [
        2, 4, 5,
    ]
    assert third.coverage.complete is True and third.coverage.total == 5


def test_cursor_from_another_kind_is_refused(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 4), ("table", 4)])
    formula_page = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=2)
    )
    with pytest.raises(ValueError):
        _enum(repo).enumerate_elements(
            notebook.id, "table", budget=_budget(), cursor=formula_page.cursor
        )


def test_resume_after_the_cursor_source_vanishes_is_reported(repo):
    """游标指向的源已经不在计划里 → 不能悄悄从头再走一遍,必须显式
    concurrent_change。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 4)])
    _add_source(repo, notebook.id, "s-b", [("formula", 4)])
    first = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=2)
    )
    assert first.cursor is not None
    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE id=?", ("s-a",))
    resumed = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(), cursor=first.cursor
    )
    assert resumed.items == ()
    assert resumed.coverage.complete is False
    assert resumed.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


# -------------------------------------------------- 收尾复检:参与库集合本身

def _hook_element_page(repo, monkeypatch, on_first_page):
    """在元素翻页的第一页之后触发一次副作用(模拟跨页期间的并发改动)。"""
    store = repo._runtime.source_store
    original = store.element_page_rows
    state = {"pages": 0}

    def hook(db, source_id, element_type, after, limit):
        rows = original(db, source_id, element_type, after, limit)
        state["pages"] += 1
        if state["pages"] == 1:
            on_first_page()
        return rows

    monkeypatch.setattr(store, "element_page_rows", hook)


def _hook_kg_page(repo, monkeypatch, on_first_page):
    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows
    state = {"pages": 0}

    def hook(db, notebook_id, object_type, after, limit):
        rows = original(db, notebook_id, object_type, after, limit)
        state["pages"] += 1
        if state["pages"] == 1:
            on_first_page()
        return rows

    monkeypatch.setattr(store, "knowledge_object_page_rows", hook)


def test_mounting_a_base_mid_walk_is_not_complete(repo, monkeypatch):
    """跨页期间挂上一个新的参考库 → 参与库集合变了,不能报 complete。

    codex 第 2 轮 P1:旧的收尾复检只对**开场抓到的那份 id 列表**重算指纹,
    所以「多了一个库」这件事它根本看不见。这里刻意挂一个**空**库——空库不
    贡献任何来源信号,指纹按旧列表算是逐字不变的,唯一能抓到它的就是收尾时
    重新解析出的参与库集合。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 6)])
    late = repo.create_notebook(NotebookCreate(name="late-base"))
    repo.mark_notebook_base(late.id)

    def mount_it():
        repo.replace_notebook_bases(notebook.id, [late.id], "user-local")
        repo.collection_catalog.invalidate()

    _hook_element_page(repo, monkeypatch, mount_it)
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=2)
    )
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


def test_unmounting_a_base_mid_walk_is_not_complete(repo, monkeypatch):
    """反向:跨页期间卸载参考库同样是参与集变化。

    按旧列表复检也看不见——``scope_signal_fingerprint`` 按显式 id 查询,
    不复核挂载是否仍然有效,被卸掉的库的来源信号原样还在。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    _add_source(repo, notebook.id, "s-a", [("formula", 6)])
    _add_source(repo, base.id, "s-b", [("formula", 2)])

    def unmount_it():
        repo.replace_notebook_bases(notebook.id, [], "user-local")
        repo.collection_catalog.invalidate()

    _hook_element_page(repo, monkeypatch, unmount_it)
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=2)
    )
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


def test_kg_mounting_a_base_mid_walk_is_not_complete(repo, monkeypatch):
    """知识对象侧同款:seq 向量按旧列表算同样看不见新挂载的库。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(4):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    late = repo.create_notebook(NotebookCreate(name="late-base"))
    repo.mark_notebook_base(late.id)

    def mount_it():
        repo.replace_notebook_bases(notebook.id, [late.id], "user-local")
        repo.collection_catalog.invalidate()

    _hook_kg_page(repo, monkeypatch, mount_it)
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=2)
    )
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


def test_kg_unmounting_a_base_mid_walk_is_not_complete(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    for index in range(4):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    _add_kg_object(repo, base.id, "b0", "concept")

    def unmount_it():
        repo.replace_notebook_bases(notebook.id, [], "user-local")
        repo.collection_catalog.invalidate()

    _hook_kg_page(repo, monkeypatch, unmount_it)
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=2)
    )
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


def test_an_undisturbed_scope_still_reports_complete(repo, monkeypatch):
    """反向对照:同样的 hook 但什么都不改,收尾复检必须照常放行 complete。

    没有这一条,上面四条只要把 scope_stable 写死成 False 就能全绿。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    _add_source(repo, notebook.id, "s-a", [("formula", 6)])
    _add_source(repo, base.id, "s-b", [("formula", 2)])
    _add_kg_object(repo, notebook.id, "o0", "concept")
    _add_kg_object(repo, base.id, "b0", "concept")

    _hook_element_page(repo, monkeypatch, lambda: None)
    _hook_kg_page(repo, monkeypatch, lambda: None)
    elements = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=2)
    )
    objects = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=2)
    )
    assert elements.coverage.returned == 8 and elements.coverage.complete is True
    assert objects.coverage.returned == 2 and objects.coverage.complete is True


# ------------------------------------------------------------------ 预算触顶

def test_row_ceiling_reports_budget(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 10)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_rows=3, page_size=10)
    )
    assert result.coverage.returned == 3
    assert result.coverage.total == 10
    assert result.coverage.has_more is True
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_BUDGET
    assert result.coverage.overflow_semantics == EXPLICIT_PARTIAL_OVERFLOW
    assert result.cursor is not None


def test_page_ceiling_counts_only_extra_round_trips(repo):
    """页预算只计「同一源第 2 页及之后」——首页不计。10 条 / 页大小 2 /
    页预算 2:首页免费 + 2 次额外往返 = 6 条后触顶。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 10)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula",
        budget=_budget(page_size=2, max_pages=2, max_rows=1_000),
    )
    assert result.coverage.returned == 6
    assert result.coverage.has_more is True
    assert result.coverage.truncated_reason == TRUNCATED_BUDGET


def _count_query_charges(monkeypatch) -> dict:
    """记录 ``_Walk.charge_query`` 的真实调用次数。

    只断言「实际查询数 ≤ 上界」是不够的:把计费点整个删掉,上界照样成立、
    守卫照样绿,而不变式已经不再被强制。拿它与页查询次数逐一对齐,才是在守
    「每次往返都过了计数器」。
    """
    state = {"count": 0}
    original = collection_enumeration._Walk.charge_query

    def counted(self):
        state["count"] += 1
        return original(self)

    monkeypatch.setattr(
        collection_enumeration._Walk, "charge_query", counted
    )
    return state


def test_round_trip_count_stays_within_the_budget_derived_bound(repo, monkeypatch):
    """一次动作的页查询数有硬上界 `max_rows + max_pages`,并在代码里被强制。

    推导(codex 第 3 轮 P2):同源第 2 页起都计入 `max_pages`;首页免费,但一个源
    只因为地图数出过行才进计划,访问它就会产行,而行受 `max_rows` 约束。这条
    用例走「宽而薄」形态(100 源 × 1 公式)的最坏情况——每个源恰好 1 次查询。

    400 个零计数源刻意排在**前面**(计划按 source_id 排序):它们一旦进入计划就
    会在行预算被消耗之前先烧掉 400 次查询,计数器当场超界报错——这正是让「零
    计数源不访问」这条前提可被证伪的形状。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(400):
        _add_source(repo, notebook.id, f"s-a-miss-{index:03d}", [("paragraph", 1)])
    for index in range(100):
        _add_source(repo, notebook.id, f"s-z-hit-{index:03d}", [("formula", 1)])

    store = repo._runtime.source_store
    original = store.element_page_rows
    calls: list[str] = []

    def spy(db, source_id, element_type, after, limit):
        calls.append(source_id)
        return original(db, source_id, element_type, after, limit)

    monkeypatch.setattr(store, "element_page_rows", spy)
    charges = _count_query_charges(monkeypatch)
    budget = _budget(page_size=25, max_rows=100, max_pages=4)
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=budget
    )

    assert result.coverage.returned == 100
    assert result.coverage.complete is True          # 宽而薄仍然可达完整
    assert len(calls) <= budget.max_rows + budget.max_pages, len(calls)
    assert not any("miss" in source_id for source_id in calls), (
        "零计数源不该被访问——它们不在计划里")
    # 上界必须是**强制**的,不能只是恰好成立:每一次页查询都要过计数器。
    assert charges["count"] == len(calls), (charges["count"], len(calls))


def test_kg_page_queries_are_charged_against_the_ceiling_too(repo, monkeypatch):
    """知识对象侧的每一次页查询(含状态过滤的补页)同样过计数器。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(12):
        _add_kg_object(
            repo, notebook.id, f"d{index:03d}", "concept", status="deprecated"
        )
    for index in range(6):
        _add_kg_object(repo, notebook.id, f"z{index}", "concept")

    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows
    calls: list[int] = []

    def spy(db, notebook_id, object_type, after, limit):
        calls.append(limit)
        return original(db, notebook_id, object_type, after, limit)

    monkeypatch.setattr(store, "knowledge_object_page_rows", spy)
    charges = _count_query_charges(monkeypatch)
    budget = _budget(page_size=4, max_rows=6, max_pages=4)
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=budget
    )

    assert result.coverage.returned == 6 and result.coverage.complete is True
    assert len(calls) > 1, "过扫描确实发生了,否则这条守不到补页"
    assert charges["count"] == len(calls), (charges["count"], len(calls))


def test_query_ceiling_is_enforced_not_merely_documented(repo):
    """上界是**强制**的:计数器越界立刻抛 ``EnumerationInvariantError``。

    直接对账本做单元断言,因为让真实遍历越界的唯一办法是先破坏它所依赖的
    前提(上面那条用例负责),而「越界会怎样」本身必须自己钉住。
    """
    walk = collection_enumeration._Walk(_budget())
    walk.bound_queries(2)
    walk.charge_query()
    walk.charge_query()
    with pytest.raises(collection_enumeration.EnumerationInvariantError):
        walk.charge_query()


def test_page_budget_does_not_starve_a_wide_thin_corpus(repo):
    """真实语料形态:公式散在很多源、每源一条。首页若计入页预算,
    enum_pages_per_run(最低档 2)会在行预算用掉 4% 时就先见底,
    complete 永不可达。这条钉住「页预算=额外往返」。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(60):
        _add_source(repo, notebook.id, f"s-{index:03d}", [("formula", 1)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula",
        budget=_budget(page_size=25, max_rows=100, max_pages=4),
    )
    assert result.coverage.returned == 60
    assert result.coverage.complete is True
    assert result.coverage.truncated_reason == ""


def test_page_budget_also_frees_the_first_page_per_notebook_for_kg(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    _add_kg_object(repo, notebook.id, "o0", "concept")
    _add_kg_object(repo, base.id, "b0", "concept")
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=25, max_pages=1)
    )
    assert result.coverage.returned == 2 and result.coverage.complete is True


def test_payload_ceiling_reports_payload(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 10)])
    full = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    two_items = sum(
        collection_enumeration._payload_chars(item) for item in full.items[:2]
    )
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula",
        budget=_budget(page_size=10, max_payload_chars=two_items),
    )
    assert result.coverage.returned == 2
    # payload 在页中途叫停:读过的物理行多于产出的条目,coverage 如实分开记。
    assert result.coverage.scanned == 10
    assert result.coverage.has_more is True
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_PAYLOAD


def test_partial_always_carries_a_cursor_except_on_concurrent_change(repo):
    """合同:``complete=False`` ⟹ cursor 非 None,唯一例外是
    ``concurrent_change``。否则 T4 无法区分「可续跑」与「不可续」——两者
    在响应里长得一模一样。首条就撑爆 payload 是最刁的形态:一条都没产出,
    游标必须指到该行**之前**,续跑给更大预算就能取到它。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 3)])
    with repo._write() as db:
        db.execute(
            "UPDATE source_elements SET text=? WHERE id=?",
            ("x" * 4_000, "el-s-a-001"),
        )
    tiny = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(max_payload_chars=200)
    )
    assert tiny.items == ()
    assert tiny.coverage.complete is False
    assert tiny.coverage.truncated_reason == TRUNCATED_PAYLOAD
    assert tiny.cursor is not None                 # 可续,不是死路
    assert tiny.cursor.created_at is None          # 指向该源的起点

    resumed = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(), cursor=tiny.cursor
    )
    assert [item.element_id for item in resumed.items] == [
        "el-s-a-001", "el-s-a-002", "el-s-a-003",
    ]
    assert resumed.coverage.complete is True


@pytest.mark.parametrize("kind", ENUMERABLE_ELEMENT_KINDS)
def test_every_partial_shape_hands_back_a_usable_cursor(repo, kind):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [(kind, 6)])
    for budget in (
        _budget(max_rows=2),
        _budget(page_size=2, max_pages=1),
        _budget(max_payload_chars=400, page_size=6),
    ):
        result = _enum(repo).enumerate_elements(notebook.id, kind, budget=budget)
        assert result.coverage.complete is False, budget
        assert result.coverage.truncated_reason != TRUNCATED_CONCURRENT_CHANGE
        assert result.cursor is not None, budget


@pytest.mark.parametrize(
    "field", ["page_size", "max_rows", "max_pages", "max_payload_chars"]
)
def test_non_positive_budget_is_a_caller_bug(field):
    """预算参数 ≤0 直接 ValueError:用一个「空的部分结果」应付它,与真实
    截断在响应里无法区分。"""
    with pytest.raises(ValueError):
        _budget(**{field: 0})


@pytest.mark.parametrize("page_size,max_rows", [(4, 12), (12, 12), (3, 12), (12, 100)])
def test_exact_boundaries_never_report_a_false_partial(repo, page_size, max_rows):
    """恰好取尽的四种边界:页大小整除总数、行上限恰等于总数、两者同时。
    多取一行的前瞻就是为了这条——没有它,「刚好读完」会被当成「预算耗尽」,
    把一个完整答案降级成含糊的部分答案。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 12)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula",
        budget=_budget(page_size=page_size, max_rows=max_rows),
    )
    assert result.coverage.returned == 12
    assert result.coverage.complete is True
    assert result.coverage.has_more is False
    assert result.coverage.truncated_reason == ""
    assert result.cursor is None


def test_exact_boundary_across_sources_is_complete(repo):
    """跨源的整除边界:两个源各 6 条、页大小 6、行上限 12。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 6)])
    _add_source(repo, notebook.id, "s-b", [("formula", 6)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=6, max_rows=12)
    )
    assert result.coverage.returned == 12 and result.coverage.complete is True


# ------------------------------------------------------------------ 并发变更

def test_source_changed_between_pages_is_not_complete(repo, monkeypatch):
    """两页之间某个源被改动(变更信号翻转)→ 游标虽然走完,也不得声称完整。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 4)])
    _add_source(repo, notebook.id, "s-b", [("formula", 4)])
    store = repo._runtime.source_store
    original = store.element_page_rows
    state = {"pages": 0}

    def hook(db, source_id, element_type, after, limit):
        rows = original(db, source_id, element_type, after, limit)
        state["pages"] += 1
        if state["pages"] == 1:
            with repo._write() as write_db:
                write_db.execute(
                    "UPDATE sources SET updated_at=? WHERE id=?",
                    ("2026-07-29T00:00:00+08:00", "s-b"),
                )
        return rows

    monkeypatch.setattr(store, "element_page_rows", hook)
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=2)
    )
    assert result.coverage.returned == 8
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE
    assert result.coverage.overflow_semantics == EXPLICIT_PARTIAL_OVERFLOW


def test_plan_promising_more_rows_than_exist_is_not_complete(repo):
    """分母校验:计划说 3 条、实际只走到 1 条,而信号一动没动(所以指纹稳、
    游标也走完了)。这是缓存被"提拔"成完整性断言之后唯一还站得住的兜底——
    列表与分母不一致时宁可报 concurrent_change,也不能说"全部"。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 3)])
    assert repo.collection_catalog.collection_map(
        notebook.id
    ).element_count("formula") == 3          # 计数按当前信号入缓存
    with repo._write() as db:                # 行没了,信号却纹丝不动
        db.execute(
            "DELETE FROM source_elements WHERE id IN (?,?)",
            ("el-s-a-002", "el-s-a-003"),
        )
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    assert result.coverage.returned == 1
    assert result.coverage.total == 3
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


def test_new_source_between_pages_is_not_complete(repo, monkeypatch):
    """作用域里多出一个源同样是变更:清单没看到它,就不能自称全部。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 4)])
    store = repo._runtime.source_store
    original = store.element_page_rows
    state = {"pages": 0}

    def hook(db, source_id, element_type, after, limit):
        rows = original(db, source_id, element_type, after, limit)
        state["pages"] += 1
        if state["pages"] == 1:
            _add_source(repo, notebook.id, "s-late", [("formula", 3)])
        return rows

    monkeypatch.setattr(store, "element_page_rows", hook)
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(page_size=2)
    )
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


# -------------------------------------------------------- 地图 / 枚举 一致性

def test_map_count_equals_enumerated_total_excluding_memory_sources(repo):
    """硬约束:枚举的物理源集合与地图计数的源集合逐字一致,而**两侧都不含**
    Memory 派生合成源(Knowhow 投影源仍在内)。两侧口径一旦分叉,界面就会出现
    「地图报 12、清单只列 8」这种没人能解释的假部分。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 3)])
    _add_source(repo, notebook.id, "s-mem", [("formula", 2)], source_type="memory")
    _add_source(repo, notebook.id, "s-kh", [("formula", 1)], source_type="knowhow")

    mapped = repo.collection_catalog.collection_map(notebook.id)
    listed = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    assert mapped.element_count("formula") == 4
    assert listed.coverage.returned == 4
    assert listed.coverage.total == mapped.element_count("formula")
    assert listed.coverage.complete is True
    assert {item.source_id for item in listed.items} == {"s-a", "s-kh"}


# ------------------------------------------------ 私有 Memory 永不进清单

def _shared_notebook_with_private_memory(repo):
    """一个共享笔记本:owner A 的确认 Memory 派生出隐藏合成源(带公式/表格
    元素)+ 从该源抽出的知识对象;另有一份普通来源与普通知识对象。

    枚举执行器**不接收任何调用者身份**——它只拿 active_notebook_id。这正是
    排除必须无条件的原因:成员 B 的枚举与 owner A 的枚举是同一次调用,想按
    调用者过滤连过滤的输入都没有。所以断言写成「清单里根本没有它」,而不是
    「B 看不到、A 看得到」。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-doc", [("formula", 2), ("table", 1)])
    _add_source(
        repo, notebook.id, "s-mem",
        [("formula", 3), ("table", 2), ("image", 1), ("code_block", 1)],
        title="我的私人记忆", source_type="memory",
    )
    _add_kg_object(repo, notebook.id, "o-doc-1", "concept", source_id="s-doc")
    _add_kg_object(repo, notebook.id, "o-doc-2", "concept", source_id="s-doc")
    _add_kg_object(repo, notebook.id, "o-mem-1", "concept", source_id="s-mem")
    _add_kg_object(repo, notebook.id, "o-mem-2", "concept", source_id="s-mem")
    _add_kg_object(repo, notebook.id, "o-mem-3", "claim", source_id="s-mem")
    return notebook


@pytest.mark.parametrize(
    "kind, expected", [("formula", 2), ("table", 1), ("image", 0), ("code_block", 0)]
)
def test_memory_derived_elements_are_never_enumerable(repo, kind, expected):
    """Memory 派生合成源的元素既不进地图计数,也不进清单。

    共享笔记本里任何成员都能发起枚举,而枚举没有 owner 过滤——留着 Memory
    就等于把别人的确认记忆按公式/表格/图片/代码块逐条读出来。
    """
    notebook = _shared_notebook_with_private_memory(repo)
    mapped = repo.collection_catalog.collection_map(notebook.id)
    listed = _enum(repo).enumerate_elements(notebook.id, kind, budget=_budget())

    assert mapped.element_count(kind) == expected
    assert listed.coverage.returned_total == expected
    assert listed.coverage.total == expected
    # 完整性仍然成立:分子分母同口径,排除不会把「已列全」变成假部分。
    assert listed.coverage.complete is True
    assert all(item.source_id == "s-doc" for item in listed.items)


def test_memory_derived_kg_objects_are_never_enumerable(repo):
    """Memory 派生知识对象同样两侧同谓词地消失:计数减掉、行过滤掉。

    只做其中一半是最坏的形态——只过滤行会让 returned(2) 与 total(4) 对不上,
    coverage 判成 concurrent_change,一张本该完整的清单变成永远的假部分。
    """
    notebook = _shared_notebook_with_private_memory(repo)
    mapped = dict(repo.collection_catalog.collection_map(notebook.id).kg_objects)
    listed = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget()
    )

    assert mapped["concept"] == 2
    assert mapped["claim"] == 0
    assert [item.object_id for item in listed.items] == ["o-doc-1", "o-doc-2"]
    assert listed.coverage.returned_total == 2
    assert listed.coverage.total == 2
    assert listed.coverage.complete is True
    assert listed.coverage.truncated_reason == ""

    claims = _enum(repo).enumerate_kg_objects(
        notebook.id, "claim", budget=_budget()
    )
    assert claims.items == () and claims.coverage.complete is True


def test_memory_exclusion_does_not_touch_the_board_count(repo):
    """刻意的口径分叉:看板计数(notebook_catalog 那条口径)不受影响。

    枚举问的是「清单能列出多少」,看板问的是「这个库里有多少知识」。给两者
    同一个数,必然有一个是错的。
    """
    notebook = _shared_notebook_with_private_memory(repo)
    with repo._connect() as db:
        board = {
            row["object_type"]: int(row["c"])
            for row in repo._runtime.queries.knowledge_type_count_rows(
                db, notebook.id, USABLE_STATUSES
            )
        }
    assert board["concept"] == 4 and board["claim"] == 1


def test_memory_source_cannot_be_reached_by_name_or_by_id(repo):
    """两条「显式点名单一来源」的入口都必须挡住 Memory 合成源。

    标题解析走地图的 plan(Memory 已不在里面),显式 source_id 则是刻意允许
    走 plan 之外的那条路——那正是它必须自己再挡一次的原因。
    """
    notebook = _shared_notebook_with_private_memory(repo)
    assert _enum(repo).resolve_source_title(
        notebook.id, "formula", "我的私人记忆"
    ) == ("", 0, False)
    with pytest.raises(ValueError):
        _enum(repo).enumerate_elements(
            notebook.id, "formula", source_id="s-mem", budget=_budget()
        )


def test_memory_source_ids_complement_the_signal_rows(repo):
    """两条谓词是同一条的正反面:并集=全部物理源,交集=空。

    元素侧在 SQL 里排除、KG 侧靠 id 集合过滤,它们只有互为补集才等价。
    """
    notebook = _shared_notebook_with_private_memory(repo)
    store = repo._runtime.source_store
    with repo._connect() as db:
        signalled = {sid for sid, _signal in store.source_change_signal_rows(
            db, notebook.id)}
        memory = set(store.memory_source_ids(db, notebook.id))
        everything = {
            row["id"] for row in db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (notebook.id,)
            ).fetchall()
        }
    assert memory == {"s-mem"}
    assert signalled & memory == set()
    assert signalled | memory == everything


@pytest.mark.parametrize("kind", ENUMERABLE_ELEMENT_KINDS)
def test_every_whitelisted_kind_agrees_with_the_map(repo, kind):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [
        ("formula", 3), ("table", 2), ("image", 1), ("code_block", 4),
        ("paragraph", 7), ("knowhow_cell", 5),
    ])
    mapped = repo.collection_catalog.collection_map(notebook.id)
    listed = _enum(repo).enumerate_elements(notebook.id, kind, budget=_budget())
    assert listed.coverage.returned == mapped.element_count(kind)
    assert listed.coverage.complete is True
    assert {item.element_type for item in listed.items} == {kind}


# ------------------------------------------------------------------ 单源过滤

def test_single_source_scope(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 3)])
    _add_source(repo, notebook.id, "s-b", [("formula", 4)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", source_id="s-b", budget=_budget()
    )
    assert {item.source_id for item in result.items} == {"s-b"}
    assert result.coverage.returned == 4 and result.coverage.total == 4
    assert result.coverage.complete is True


def test_in_scope_source_without_the_kind_is_a_complete_empty_answer(repo):
    """显式点名的源直接查。地图没有它的计数时分母是「未知」而非 0——
    「不在计划⇒零」的推断正是下一条测试里那个 bug 的来源。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("paragraph", 3)])
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", source_id="s-a", budget=_budget()
    )
    assert result.items == ()
    assert result.coverage.total is None
    assert result.coverage.complete is True


def test_freshly_replaced_elements_are_enumerable_immediately(repo):
    """走真正的写入口 ``replace_elements`` 落新元素后,**不需要**任何后续状态
    写入,整个作用域的枚举当场就能看到它们(codex 第 3 轮 P1)。

    这条是原「首解析窗口」用例的反面:那时元素已落库而信号未动,地图把该源
    数成 0、它因此不进遍历计划、枚举列不出来,只能靠显式点名或等下一次
    set_status。现在信号与元素换代同事务翻转,窗口不存在。
    """
    from app.repositories.ports import SourceElementWrite

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("paragraph", 1)])
    # 先把「该源 0 条公式」按当前信号缓存住。
    assert repo.collection_catalog.collection_map(
        notebook.id
    ).element_count("formula") == 0

    store = repo._runtime.source_store
    with repo._write() as db:
        store.replace_elements(
            db, "s-a",
            [
                SourceElementWrite(
                    id=f"el-fresh-{index}", element_type="formula",
                    location_label="p1", text="E", metadata={},
                )
                for index in range(3)
            ],
            created_at="2026-07-29T10:00:00.123456+08:00",
        )

    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    assert [item.element_id for item in result.items] == [
        "el-fresh-0", "el-fresh-1", "el-fresh-2",
    ]
    assert result.coverage.total == 3
    assert result.coverage.complete is True


def test_explicit_source_is_queried_not_inferred_from_a_stale_zero(repo):
    """绕过元素写入口的直写(测试夹具、离线脚本)仍可能让 per-source 计数陈旧:
    信号一个字节都没动,地图仍报 0。显式点名该源时必须真的分页查询它,
    不能因为「不在计划里」就回一个自信的空清单。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("paragraph", 1)])
    # 先建一次地图,把「该源 0 条公式」按当前信号缓存住。
    assert repo.collection_catalog.collection_map(
        notebook.id
    ).element_count("formula") == 0
    # 元素落库,信号一个字节都没动 —— 缓存现在是陈旧的。
    with repo._write() as db:
        for index in range(3):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"el-late-{index}", "s-a", "formula", "p1", "E", "{}", NOW),
            )
    assert repo.collection_catalog.collection_map(
        notebook.id
    ).element_count("formula") == 0          # 地图仍然只看得见旧计数

    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", source_id="s-a", budget=_budget()
    )
    assert [item.element_id for item in result.items] == [
        "el-late-0", "el-late-1", "el-late-2",
    ]
    assert result.coverage.total is None      # 分母未知,不是 0
    assert result.coverage.complete is True


def test_explicit_source_scope_check_costs_one_lookup(repo, monkeypatch):
    """作用域判定是一次主键读,不是把每个参与库的来源清单全量拉一遍。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 2)])
    store = repo._runtime.source_store
    original = store.source_change_signal_rows
    calls: list[str] = []

    def spy(db, notebook_id):
        calls.append(notebook_id)
        return original(db, notebook_id)

    monkeypatch.setattr(store, "source_change_signal_rows", spy)
    _enum(repo).enumerate_elements(
        notebook.id, "formula", source_id="s-a", budget=_budget()
    )
    # 计划一次 + 收尾指纹一次;单源路径不得再为「它在不在作用域里」多拉一轮。
    assert calls == [notebook.id, notebook.id]


def test_out_of_scope_source_raises(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    _add_source(repo, other.id, "o-a", [("formula", 3)])
    with pytest.raises(ValueError):
        _enum(repo).enumerate_elements(
            notebook.id, "formula", source_id="o-a", budget=_budget()
        )


def test_unknown_kind_raises(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    with pytest.raises(ValueError):
        _enum(repo).enumerate_elements(
            notebook.id, "paragraph", budget=_budget()
        )


# -------------------------------------------------------------------- item

def test_item_text_is_excerpt_truncated_and_labelled(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 1)], title="来源甲")
    with repo._write() as db:
        db.execute(
            "UPDATE source_elements SET text=? WHERE source_id=?",
            ("x" * 5_000, "s-a"),
        )
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget(excerpt_chars=100)
    )
    item = result.items[0]
    assert len(item.text) == 100
    assert item.source_title == "来源甲"
    assert item.location_label == "p1"
    assert item.element_type == "formula"


def test_image_items_carry_a_bounded_asset_reference(repo):
    """spec §2.6 决策:图片带 ``asset_id``(短引用),表格**不带**
    ``table_html``(无界、截断即碎)。表格卡用 text 摘录渲染 + 跳转来源。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("image", 1), ("table", 1)])
    with repo._write() as db:
        db.execute(
            "UPDATE source_elements SET metadata=? WHERE element_type=?",
            (json.dumps({"asset_id": "asset-42", "mime": "image/png"}), "image"),
        )
        db.execute(
            "UPDATE source_elements SET metadata=? WHERE element_type=?",
            (json.dumps({"table_html": "<table>" + "x" * 50_000 + "</table>"}),
             "table"),
        )
    image = _enum(repo).enumerate_elements(notebook.id, "image", budget=_budget())
    table = _enum(repo).enumerate_elements(notebook.id, "table", budget=_budget())
    assert image.items[0].asset_id == "asset-42"
    assert table.items[0].asset_id == ""
    # 表格的巨大 metadata 既不进 item,也不进 payload 账目。
    assert "table_html" not in json.dumps(
        [item.__dict__ for item in table.items], ensure_ascii=False
    )
    assert collection_enumeration._payload_chars(table.items[0]) < 1_000


def test_grounded_paper_title_labels_the_source(repo):
    """引用标题口径与 Ask/报告一致:接地判定为论文且标题非空时优先显示论文标题。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 1)], title="upload-1.pdf")
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_paper_meta (source_id,notebook_id,is_paper,paper_title,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?)",
            ("s-a", notebook.id, 1, "A Study of Things", NOW, NOW),
        )
    result = _enum(repo).enumerate_elements(
        notebook.id, "formula", budget=_budget()
    )
    assert result.items[0].source_title == "A Study of Things"


# ---------------------------------------------------------------- KG 对象

@pytest.mark.parametrize("page_size", [25, 2])
def test_kg_enumeration_matches_the_map_and_skips_deprecated(repo, page_size):
    """硬约束:status 谓词与 L3 计数用的是同一份 USABLE_STATUSES。
    否则会出现「地图报 89、清单列 85」。页大小 2 的这一档让同一条断言
    也覆盖跨页与跨 notebook 的自动翻页。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    for index in range(3):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    _add_kg_object(repo, base.id, "b0", "concept")
    _add_kg_object(repo, notebook.id, "d0", "concept", status="deprecated")
    _add_kg_object(repo, notebook.id, "c0", "claim")

    mapped = dict(repo.collection_catalog.collection_map(notebook.id).kg_objects)
    listed = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=page_size)
    )
    assert mapped["concept"] == 4
    assert listed.coverage.returned == 4
    assert listed.coverage.total == 4
    assert listed.coverage.complete is True
    assert {item.object_id for item in listed.items} == {"o0", "o1", "o2", "b0"}
    assert {item.tier for item in listed.items} == {"personal", "base"}
    assert all(item.object_type == "concept" for item in listed.items)


def test_kg_items_carry_bounded_evidence_refs(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_kg_object(
        repo, notebook.id, "o0", "formula", name="欧姆定律",
        evidence=[f"el-{index}" for index in range(10)],
    )
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "formula", budget=_budget()
    )
    item = result.items[0]
    assert item.name == "欧姆定律"
    assert item.section_path == "第一章>1.1"
    assert item.evidence_element_ids == ("el-0", "el-1", "el-2")
    assert len(item.evidence_element_ids) == MAX_EVIDENCE_REFS


def test_kg_cursor_resume_equals_one_shot(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(notebook.id, [base.id], "user-local")
    for index in range(5):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    for index in range(4):
        _add_kg_object(repo, base.id, f"b{index}", "concept")

    one_shot = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget()
    )
    collected: list[str] = []
    cursor = None
    for _round in range(10):
        page = _enum(repo).enumerate_kg_objects(
            notebook.id, "concept", budget=_budget(max_rows=2), cursor=cursor
        )
        collected.extend(item.object_id for item in page.items)
        cursor = page.cursor
        if page.coverage.complete:
            break
    assert collected == [item.object_id for item in one_shot.items]
    assert len(collected) == 9


def test_kg_budget_and_boundaries(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(6):
        _add_kg_object(repo, notebook.id, f"o{index}", "procedure")
    truncated = _enum(repo).enumerate_kg_objects(
        notebook.id, "procedure", budget=_budget(max_rows=4, page_size=2)
    )
    assert truncated.coverage.returned == 4
    assert truncated.coverage.has_more is True
    assert truncated.coverage.truncated_reason == TRUNCATED_BUDGET
    exact = _enum(repo).enumerate_kg_objects(
        notebook.id, "procedure", budget=_budget(max_rows=6, page_size=3)
    )
    assert exact.coverage.returned == 6 and exact.coverage.complete is True


def test_kg_mutation_between_pages_is_not_complete(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(4):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows
    state = {"pages": 0}

    def hook(db, notebook_id, object_type, after, limit):
        rows = original(db, notebook_id, object_type, after, limit)
        state["pages"] += 1
        if state["pages"] == 1:
            with repo._write() as write_db:
                repo._runtime.unified_kg.mark_dirty(write_db, notebook.id, NOW)
        return rows

    monkeypatch.setattr(store, "knowledge_object_page_rows", hook)
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(page_size=2)
    )
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


def test_kg_total_is_omitted_when_the_map_cannot_answer(repo, monkeypatch):
    """total 的两种形态。KG 侧的计数只是分母(翻页来自 keyset),取不到时
    降级成 total=None 并照常返回诚实清单——不是静默:coverage 里看得见。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(3):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    with_total = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget()
    )
    assert with_total.coverage.total == 3

    monkeypatch.setattr(
        repo.collection_catalog,
        "scope_kg_type_counts",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    without_total = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget()
    )
    assert without_total.coverage.total is None
    assert without_total.coverage.returned == 3
    assert without_total.coverage.complete is True


def test_unknown_object_type_raises(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    with pytest.raises(ValueError):
        _enum(repo).enumerate_kg_objects(notebook.id, "rule", budget=_budget())


# -------------------------------------------------------------------- 取消

def test_cancel_between_pages(repo, monkeypatch):
    import threading

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 20)])
    cancel = threading.Event()
    store = repo._runtime.source_store
    original = store.element_page_rows

    def hook(db, source_id, element_type, after, limit):
        cancel.set()
        return original(db, source_id, element_type, after, limit)

    monkeypatch.setattr(store, "element_page_rows", hook)
    with pytest.raises(AskCancelled):
        _enum(repo).enumerate_elements(
            notebook.id, "formula", budget=_budget(page_size=2),
            cancel_event=cancel,
        )


def test_cancel_between_kg_pages(repo, monkeypatch):
    import threading

    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(10):
        _add_kg_object(repo, notebook.id, f"o{index}", "claim")
    cancel = threading.Event()
    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows

    def hook(db, notebook_id, object_type, after, limit):
        cancel.set()
        return original(db, notebook_id, object_type, after, limit)

    monkeypatch.setattr(store, "knowledge_object_page_rows", hook)
    with pytest.raises(AskCancelled):
        _enum(repo).enumerate_kg_objects(
            notebook.id, "claim", budget=_budget(page_size=2), cancel_event=cancel
        )


# ------------------------------------------------------------------- 契约

def test_excerpt_default_tracks_every_effort_profile():
    """本模块刻意不 import 档位表,但默认摘录宽度必须与五档的
    ``cell_excerpt_chars`` 一致——分叉了就在这里响亮失败,而不是悄悄按
    另一个宽度截断。"""
    assert {
        limits.cell_excerpt_chars for limits in ASK_RETRIEVAL_LIMITS.values()
    } == {DEFAULT_EXCERPT_CHARS}


def test_overflow_semantics_constant_is_reused_verbatim():
    assert EXPLICIT_PARTIAL_OVERFLOW == "explicit_partial"


def test_enumerable_kg_types_match_the_edge_contract():
    """可枚举 KG 类型集合必须等于边契约的核心四类端点(真源
    ``kg/edge_schema.py``);多一类少一类都会让地图/清单与图谱脱节。"""
    assert set(ENUMERABLE_KG_OBJECT_TYPES) == set(NODE_TYPES)


def test_kg_status_predicate_is_the_counting_predicate(repo, monkeypatch):
    """可用状态的判据必须**就是** USABLE_STATUSES 本身,而不是一份等值副本。

    谓词从 SQL 挪到服务层(页查询要保持 O(limit),见 ``_usable_kg_page``)之后,
    「同一份」不能再靠 spy 传参的对象身份来证明。改为行为反证:把执行器所在
    模块的那个名字换成一个更窄的集合,清单必须立刻随之改变——只有真的在读
    这个对象才做得到,一份 import 期就固化的副本不会跟着变。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_kg_object(repo, notebook.id, "o0", "concept", status="approved")
    _add_kg_object(repo, notebook.id, "o1", "concept", status="reviewed")

    # 同一个对象,不是等值副本。
    assert collection_enumeration.USABLE_STATUSES is USABLE_STATUSES
    both = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget()
    )
    assert {item.object_id for item in both.items} == {"o0", "o1"}

    monkeypatch.setattr(collection_enumeration, "USABLE_STATUSES", ("reviewed",))
    narrowed = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget()
    )
    assert {item.object_id for item in narrowed.items} == {"o1"}


def test_kg_page_query_carries_no_status_predicate(repo):
    """页查询必须只按 keyset 取原始行:status 一旦回到 SQL,一个 deprecated
    占比高的库就会让「一页」重新变成无界残余过滤(codex 第 1 轮 P2-5)。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_kg_object(repo, notebook.id, "o0", "concept")
    store = repo._runtime.knowledge
    with repo._connect() as db:
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            store.knowledge_object_page_rows(db, notebook.id, "concept", None, 2)
        finally:
            db.set_trace_callback(None)
    executed = [item for item in statements if "FROM knowledge_objects" in item]
    assert executed, statements
    for statement in executed:
        head, _, where = statement.partition("WHERE")
        # 列仍要带回来——过滤搬到了服务层,不是取消了。
        assert "status" in head, statement
        # 谓词不得回到 SQL。
        assert "status" not in where, statement


def test_kg_enumeration_stays_bounded_when_most_objects_are_deprecated(
    repo, monkeypatch
):
    """deprecated 高占比库:查询次数与读到的原始行都必须有界,并如实报部分。

    这是 P2-5 的替代方案(不加 status 索引)的护栏:执行器在服务层过滤,
    只允许 ``max_rows × _KG_RAW_SCAN_FACTOR`` 行的原始过扫描,触顶即
    ``truncated_reason="budget"`` 的诚实 partial,绝不静默把清单截短成「全部」。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(200):
        _add_kg_object(
            repo, notebook.id, f"d{index:03d}", "concept", status="deprecated"
        )
    _add_kg_object(repo, notebook.id, "zz-usable", "concept")

    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows
    calls: list[int] = []

    def spy(db, notebook_id, object_type, after, limit):
        calls.append(limit)
        return original(db, notebook_id, object_type, after, limit)

    monkeypatch.setattr(store, "knowledge_object_page_rows", spy)
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(max_rows=10, page_size=10)
    )

    cap = 10 * collection_enumeration._KG_RAW_SCAN_FACTOR
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_BUDGET
    assert result.coverage.overflow_semantics == EXPLICIT_PARTIAL_OVERFLOW
    # 原始行**严格**有界(codex #395 第 5 轮):fetch 在查询前夹到剩余额度,
    # 文档声明的上限一行都不能越——不再容忍「最后一批的粒度」。
    assert result.coverage.scanned <= cap, (result.coverage.scanned, cap, calls)
    assert result.coverage.scanned >= cap
    # 查询次数有界:上限 / 每次至少 1 行。若过扫描上限失效,这一条会随 200 行
    # 全扫而爆掉。
    assert len(calls) <= cap, calls
    # 而且必须能续跑(complete=false ⟹ 游标非空)。
    assert result.cursor is not None


def test_kg_raw_scan_ceiling_clamps_the_fetch_before_the_query(
    repo, monkeypatch
):
    """越界批次被构造出来时,fetch 必须在查询前被夹到剩余额度(codex #395 R5)。

    3 个可用对象让首批收下 3 行,后续 fetch 缩为 7——累计 38 行后旧实现会再发
    一个 limit=7 的查询读到 45/40(越过声明上限),且若那批凑满页还会当成功
    退出。修复后:最后一批 limit 被夹为 2,scanned 恰好等于上限,诚实报 budget。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(3):
        _add_kg_object(repo, notebook.id, f"a-usable-{index}", "concept")
    for index in range(60):
        _add_kg_object(
            repo, notebook.id, f"d{index:03d}", "concept", status="deprecated"
        )

    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows
    calls: list[tuple[int, int]] = []          # (请求 limit, 实际返回行数)
    running = 0

    def spy(db, notebook_id, object_type, after, limit):
        rows = original(db, notebook_id, object_type, after, limit)
        nonlocal running
        running += len(rows)
        calls.append((limit, len(rows)))
        return rows

    monkeypatch.setattr(store, "knowledge_object_page_rows", spy)
    result = _enum(repo).enumerate_kg_objects(
        notebook.id, "concept", budget=_budget(max_rows=10, page_size=10)
    )

    cap = 10 * collection_enumeration._KG_RAW_SCAN_FACTOR
    assert result.coverage.truncated_reason == TRUNCATED_BUDGET
    # 每一步累计都不越界——不是「总量恰好没超」的巧合,而是逐批钳制。
    totals = []
    acc = 0
    for _limit, returned in calls:
        acc += returned
        totals.append(acc)
    assert all(total <= cap for total in totals), (totals, cap)
    assert result.coverage.scanned == cap, (result.coverage.scanned, calls)
    # 最后一批的 limit 就是被夹后的剩余额度(2),不是未夹的 want 余量(7)。
    assert calls[-1][0] == cap - totals[-2], calls


def test_kg_resume_after_the_overscan_ceiling_makes_progress(repo):
    """触顶后的续跑必须真的往前走:游标越过已扫过的 deprecated 前缀。

    否则每一次续跑都重扫同一段、再次触顶,链条永远推进不了。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(60):
        _add_kg_object(
            repo, notebook.id, f"d{index:03d}", "concept", status="deprecated"
        )
    _add_kg_object(repo, notebook.id, "zz-usable", "concept")

    collected: list[str] = []
    cursor = None
    rounds = 0
    for _round in range(12):
        rounds += 1
        page = _enum(repo).enumerate_kg_objects(
            notebook.id, "concept",
            budget=_budget(max_rows=4, page_size=4), cursor=cursor,
        )
        collected.extend(item.object_id for item in page.items)
        cursor = page.cursor
        if page.coverage.complete or cursor is None:
            break
    assert collected == ["zz-usable"]
    assert rounds < 12, "续跑没有推进:每次都重扫同一段 deprecated 前缀"


# ------------------------------------------------- 白名单单一真源(源码守卫)

def _literal_string_sequences(path: pathlib.Path):
    """文件里所有「全为字符串字面量」的元组/列表/集合,连同其赋值目标名。

    用 AST 而不是正则:正则要么漏(换行、单双引号、顺序不同),要么误伤
    文档与注释里的同名词。这里只看真正的字面量序列节点。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assigned: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
            name = next(
                (t.id for t in targets if isinstance(t, ast.Name)), ""
            )
            if value is not None and name:
                assigned[id(value)] = name
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            continue
        values = [
            element.value for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if not values or len(values) != len(node.elts):
            continue
        # The node itself travels, not its line number: identity here is
        # (file, assigned name, value set) — the position is only ever used to
        # print a locator in the failure message.
        yield node, frozenset(values), assigned.get(id(node), "")


@pytest.mark.architecture_contract
def test_enumerable_element_kinds_have_exactly_one_literal_definition():
    """可枚举元素 kind 白名单只能有一处字面量:``collection_catalog`` 的
    定义点。执行器与(将来的)reflect 校验一律 import。

    再抄一份的后果不是风格问题——两份副本会各自漂移,地图按一份计数、
    清单按另一份枚举,而 coverage 仍然报 complete。

    刻意**只**守元素 kind:KG 的四类核心节点是知识图谱自己的类型集合
    (真源 ``kg/edge_schema.py`` 的 ``NODE_TYPES``),全仓已有二十余处正当
    出现,对它做同款集合等值守卫只会制造噪声。两者的关联由上面
    ``test_enumerable_kg_types_match_the_edge_contract`` 钉住。
    """
    whitelist = frozenset(ENUMERABLE_ELEMENT_KINDS)
    scanned = 0
    offenders = []
    for root in ("backend/app", "backend/tests", "scripts"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            scanned += 1
            for node, values, name in _literal_string_sequences(path):
                if values != whitelist:
                    continue
                relative = path.relative_to(REPO_ROOT).as_posix()
                if (
                    relative == "backend/app/services/collection_catalog.py"
                    and name == "ENUMERABLE_ELEMENT_KINDS"
                ):
                    continue        # 唯一定义点
                offenders.append(f"{relative}:{node.lineno} ({name or 'inline'})")
    assert scanned > 300, scanned      # 扫描面塌了要先红,别静默放行
    assert offenders == [], (
        "可枚举元素 kind 白名单出现了第二份字面量副本,必须改为 import "
        "collection_catalog.ENUMERABLE_ELEMENT_KINDS: " + ", ".join(offenders)
    )


# ------------------------------------------------------- 索引不可绕(EXPLAIN)

def _plan_for(repo, run, needle):
    with repo._connect() as db:
        statements: list[str] = []
        db.set_trace_callback(statements.append)
        try:
            run(db)
        finally:
            db.set_trace_callback(None)
        executed = [item for item in statements if needle in item]
        assert executed, statements
        return [
            " ".join(
                str(row[3])
                for row in db.execute("EXPLAIN QUERY PLAN " + statement).fetchall()
            )
            for statement in executed
        ]


def test_element_page_query_rides_the_typed_index(repo):
    """元素翻页的**实际执行**语句必须走 idx_source_elements_source_type,
    首页与续页都是索引 range,不得退回扫描或临时排序。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    _add_source(repo, notebook.id, "s-a", [("formula", 4), ("paragraph", 6)])
    store = repo._runtime.source_store

    def run(db):
        first = store.element_page_rows(db, "s-a", "formula", None, 2)
        store.element_page_rows(
            db, "s-a", "formula",
            (first[-1]["created_at"], first[-1]["id"]), 2,
        )

    plans = _plan_for(repo, run, "FROM source_elements")
    assert len(plans) == 2
    assert (
        "SEARCH source_elements USING INDEX idx_source_elements_source_type "
        "(source_id=? AND element_type=?)"
    ) in plans[0], plans[0]
    # 续页的键必须真的进索引:行值比较退化成 OR 展开就会掉出这条计划。
    assert (
        "SEARCH source_elements USING INDEX idx_source_elements_source_type "
        "(source_id=? AND element_type=? AND (created_at,id)>(?,?))"
    ) in plans[1], plans[1]
    for plan in plans:
        assert "SCAN source_elements" not in plan, plan
        assert "TEMP B-TREE" not in plan, plan


def test_kg_object_page_query_rides_the_typed_index(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(4):
        _add_kg_object(repo, notebook.id, f"o{index}", "concept")
    store = repo._runtime.knowledge

    def run(db):
        first = store.knowledge_object_page_rows(
            db, notebook.id, "concept", None, 2
        )
        store.knowledge_object_page_rows(
            db, notebook.id, "concept",
            (first[-1]["created_at"], first[-1]["id"]), 2,
        )

    plans = _plan_for(repo, run, "FROM knowledge_objects")
    assert len(plans) == 2
    assert (
        "SEARCH knowledge_objects USING INDEX idx_knowledge_objects_nb_type_created "
        "(notebook_id=? AND object_type=?)"
    ) in plans[0], plans[0]
    assert (
        "SEARCH knowledge_objects USING INDEX idx_knowledge_objects_nb_type_created "
        "(notebook_id=? AND object_type=? AND (created_at,id)>(?,?))"
    ) in plans[1], plans[1]
    for plan in plans:
        assert "SCAN knowledge_objects" not in plan, plan
        assert "TEMP B-TREE" not in plan, plan
