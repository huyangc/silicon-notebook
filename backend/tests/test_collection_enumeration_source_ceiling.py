"""类型化集合枚举认来源天花板(PR-B:B2 地图 / B3 执行器 / B4 引用)。

用户裁决(2026-09-29 Q2):花名册、计数、引用卡处处只看勾选的来源,收窄时工具
继续可用。架构裁决:权限与来源范围只在检索/读取层审核——枚举执行器与
``collection_item_citations`` 就是这一层,合成把到手的一切当作可读。

本文件全部跑在**真 SQLite 仓库**上(真 store 的 ``allowed_source_ids`` /
``supported_by_source_ids`` 下推 + 真计数 + 真指纹),覆盖:

* 三类集合(elements / kg_objects / sources)在 include 天花板下的分母
  (``total``)、``complete`` 与续跑游标都与过滤后的集合一致;
* 指纹:天花板外的上传不触发 ``concurrent_change``,天花板内的重解析仍触发;
* 截断前先按天花板过滤:前三条证据都在天花板外、第四条在内,仍能引到界内元素;
* 显式点名天花板外的 ``source_id`` → 与「不在参与库」同一句 ``ValueError``
  (无法借拒绝探测未勾选文档是否存在),推理循环里的跳过步骤与 fail_closed 同形;
* 冻结之后上传的来源既不列出、也不计数、也不能被点名;
* 全局问答:Knowhow 投影对象的引用不再让整份答案以 ``out_of_ceiling`` 作废;
* 无天花板时逐字节不变(页查询不带新关键字、计数走旧口径、证据引用原样)。

⚠ 依赖 store 侧(PR-B·B1)的两个关键字参数与 ``repository_runtime`` 给
``CollectionCatalogService`` 接上 ``knowledge=seats.knowledge``;
``test_catalog_is_composed_with_the_knowledge_store`` 钉住后者。
"""
from __future__ import annotations

import json
from contextlib import contextmanager

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services import collection_catalog
from app.services.collection_enumeration import (
    MAX_EVIDENCE_REFS,
    SELECTED_SOURCES_SCOPE_SUFFIX,
    TRUNCATED_CONCURRENT_CHANGE,
    EnumerationBudget,
    KgObjectItem,
    SourceItem,
)
from app.services.embedding import FakeEmbedder
from app.services.global_citation_check import judge_reference
from app.services.retrieval_participants import (
    ParticipantOverride,
    participant_override,
)
from app.services.retrieval_run import retrieval_run
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients
from tests.test_reasoning_enumeration_tools import (
    _SeqLLM,
    _enumerate_action,
    _retriever,
    _seed as _reasoning_seed,
    _skips,
)


NOW = "2026-07-28T00:00:00+08:00"
LATER = "2026-07-29T00:00:00+08:00"
_ACTOR = "user-local"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    try:
        yield instance
    finally:
        instance.close()


def _budget(**overrides):
    base = dict(
        page_size=25, max_rows=1_000, max_pages=50, max_payload_chars=256_000
    )
    base.update(overrides)
    return EnumerationBudget(**base)


def _src(repo, notebook_id, source_id, *, formulas=0, tables=0,
         source_type="markdown", created_at=NOW):
    """一个来源 + ``formulas`` 个公式 + ``tables`` 个表格元素。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, f"标题-{source_id}", source_type,
             "extracted", "extracted", created_at, NOW),
        )
        index = 0
        for element_type, count in (("formula", formulas), ("table", tables)):
            for _ in range(count):
                index += 1
                db.execute(
                    "INSERT INTO source_elements (id,source_id,element_type,"
                    "location_label,text,metadata,created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (f"el-{source_id}-{index:03d}", source_id, element_type,
                     f"p{index}", f"body {source_id} {index}", "{}", NOW),
                )
    repo.collection_catalog.invalidate()


def _element(repo, source_id, element_id, element_type="knowhow_cell"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,"
            "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
            (element_id, source_id, element_type, "行 1", f"正文 {element_id}",
             "{}", NOW),
        )


def _kg(repo, notebook_id, object_id, evidence, *, object_type="concept",
        owner_source_id="", section_path=None):
    """一个 KG 对象;``evidence`` 是 ``[(source_id, element_id), ...]``。

    反向索引(``knowledge_object_sources``)与 evidence JSON 一起写:store 的
    天花板谓词在「已认证」时读前者、否则读后者,两支都必须看到同一份支撑。
    ``section_path`` 给了才写进 payload(其余用例的 payload 逐字不变)。
    """
    payload = {"name": object_id}
    if section_path is not None:
        payload["section_path"] = section_path
    evidence_json = json.dumps([
        {"source_id": source_id, "source_title": "t", "element_id": element_id,
         "element_type": "formula", "location_label": "p1",
         "quoted_span": "q", "confidence": 1.0}
        for source_id, element_id in evidence
    ])
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,"
            "payload,evidence,status,owner,last_reviewed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (object_id, notebook_id, owner_source_id, object_type,
             json.dumps(payload), evidence_json, "approved", "", "",
             NOW, NOW),
        )
        repo._runtime.knowledge.replace_object_sources(
            db, object_id, notebook_id, evidence_json
        )
    repo._runtime.queries.invalidate_knowledge_counts(notebook_id)
    repo.collection_catalog.invalidate()


def _ticked(notebook_id, source_ids, *, narrowed=True):
    """浏览器形状的本地天花板:冻结成 include 清单。"""
    return source_scope_context(notebook_id, {
        "mode": "include", "source_ids": list(source_ids), "narrowed": narrowed,
    })


@contextmanager
def _peer_run(notebook_ids, ceilings):
    """全局问答的安装形状:参与集覆盖 + 逐库冻结可见来源 + subjectless。"""
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with participant_override(ParticipantOverride(
            notebook_ids=tuple(notebook_ids), tiers={},
            attested_actor_id=_ACTOR,
        )):
            with source_scope_context(
                notebook_ids[0], None, None,
                notebook_source_ceilings=ceilings, subjectless=True,
            ):
                yield


def _library(repo):
    """sA、sC 勾选;sB 未勾选。KG:oA(sA)、oB(sB)、oMix(前三条证据在 sB、
    第四条在 sA)、oNone(没有证据)。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    nb = notebook.id
    _src(repo, nb, "sA", formulas=2, tables=1)
    _src(repo, nb, "sB", formulas=3)
    _src(repo, nb, "sC", formulas=1)
    _kg(repo, nb, "oA", [("sA", "el-sA-001")])
    _kg(repo, nb, "oB", [("sB", "el-sB-001")])
    _kg(repo, nb, "oMix", [
        ("sB", "el-sB-001"), ("sB", "el-sB-002"), ("sB", "el-sB-003"),
        ("sA", "el-sA-002"),
    ])
    _kg(repo, nb, "oNone", [])
    return nb


def _walk_all(call, cursor_of=lambda result: result.cursor):
    """按游标一直续跑到底,返回 (全部条目, 最后一次 coverage, 调用次数)。"""
    items, cursor, calls = [], None, 0
    while True:
        result = call(cursor)
        calls += 1
        items.extend(result.items)
        cursor = cursor_of(result)
        if cursor is None:
            return items, result.coverage, calls


# ------------------------------------------------------------------ 组装

def test_catalog_is_composed_with_the_knowledge_store(repo):
    """天花板下的 KG 计数要 ``count_knowledge(supported_by_source_ids=...)``,
    它在 KnowledgeStorePort 上;组装层必须把同一个 store 递给地图。"""
    assert repo.collection_catalog._knowledge is repo._runtime.knowledge


# ------------------------------------------------------------ elements

def test_elements_under_an_include_ceiling_count_list_and_page_consistently(repo):
    nb = _library(repo)
    with _ticked(nb, ["sA", "sC"]):
        collection_map = repo.collection_catalog.collection_map(nb)
        assert collection_map.element_count("formula") == 3      # 2 + 1,不含 sB
        assert collection_map.element_count("table") == 1
        formula = next(e for e in collection_map.elements if e.kind == "formula")
        assert formula.sources == 2

        one_shot = repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget())
        assert {item.source_id for item in one_shot.items} == {"sA", "sC"}
        assert one_shot.coverage.total == 3
        assert one_shot.coverage.returned_total == 3
        assert one_shot.coverage.complete is True

        items, coverage, calls = _walk_all(
            lambda cursor: repo.collection_enumeration.enumerate_elements(
                nb, "formula", budget=_budget(page_size=1, max_rows=1),
                cursor=cursor))
    assert calls == 3
    assert [item.element_id for item in items] == [
        item.element_id for item in one_shot.items]
    assert coverage.total == 3
    assert coverage.returned_total == 3
    assert coverage.complete is True


def test_element_counts_agree_on_both_sides_of_the_smaller_half(repo):
    """L2 减去被排除的一侧 与 直接累加保留的一侧 必须给出同一个数。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    nb = notebook.id
    for index in range(6):
        _src(repo, nb, f"s{index}", formulas=index + 1)
    catalog = repo.collection_catalog
    # 排除 1 个(减法支):总 21 − s5 的 6 = 15;source 数 5。
    with _ticked(nb, [f"s{index}" for index in range(5)]):
        subtract = catalog.collection_map(nb)
        with repo._connect() as db:
            plan = catalog.scope_element_plan(db, (nb,), "formula")
    # 只保留 1 个(直加支):s2 的 3。
    with _ticked(nb, ["s2"]):
        direct = catalog.collection_map(nb)
    assert (subtract.element_count("formula"), subtract.elements[0].sources) == (15, 5)
    assert plan.total == 15
    assert (direct.element_count("formula"), direct.elements[0].sources) == (3, 1)
    # 不装天花板:还是整库的数,且未被上面两次运行写坏。
    assert catalog.collection_map(nb).element_count("formula") == 21


# ------------------------------------------------------------- sources

def test_sources_roster_under_an_include_ceiling(repo):
    nb = _library(repo)
    with _ticked(nb, ["sA", "sC"]):
        assert repo.collection_catalog.collection_map(nb).sources == 2
        items, coverage, calls = _walk_all(
            lambda cursor: repo.collection_enumeration.enumerate_sources(
                nb, budget=_budget(page_size=1, max_rows=1), cursor=cursor))
    assert calls == 2
    assert [item.source_id for item in items] == ["sA", "sC"]
    assert coverage.total == 2
    assert coverage.returned_total == 2
    assert coverage.complete is True


# ------------------------------------------------------------ KG objects

def test_kg_objects_under_an_include_ceiling(repo):
    """只列有界内证据的对象;无证据的 oNone 与只有界外证据的 oB 不列也不计。"""
    nb = _library(repo)
    with _ticked(nb, ["sA", "sC"]):
        counts = dict(repo.collection_catalog.collection_map(nb).kg_objects)
        assert counts["concept"] == 2
        one_shot = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
        items, coverage, calls = _walk_all(
            lambda cursor: repo.collection_enumeration.enumerate_kg_objects(
                nb, "concept", budget=_budget(page_size=1, max_rows=1),
                cursor=cursor))
    assert sorted(item.object_id for item in one_shot.items) == ["oA", "oMix"]
    assert one_shot.coverage.total == 2
    assert one_shot.coverage.complete is True
    assert calls == 2
    assert [item.object_id for item in items] == [
        item.object_id for item in one_shot.items]
    assert (coverage.total, coverage.returned_total, coverage.complete) == (
        2, 2, True)


def test_the_ceiling_is_pushed_below_the_page_limit(repo):
    """天花板必须下推到页查询的 LIMIT 之下:键序前面堆着 8 个界外对象、最后一个
    才在界内。只在 Python 里事后过滤的话,``max_rows=1`` 的超扫额度(×4 = 4 行)
    会在碰到界内对象之前耗尽,报一个假的 ``budget`` 部分结果。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    nb = notebook.id
    _src(repo, nb, "sIn", formulas=1)
    _src(repo, nb, "sOut", formulas=1)
    for index in range(8):
        _kg(repo, nb, f"o{index}-out", [("sOut", "el-sOut-001")])
    _kg(repo, nb, "o9-in", [("sIn", "el-sIn-001")])
    with _ticked(nb, ["sIn"]):
        listed = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget(max_rows=1))
    assert [item.object_id for item in listed.items] == ["o9-in"]
    assert listed.coverage.complete is True
    assert listed.coverage.scanned == 1


def test_a_store_that_ignores_the_ceiling_cannot_leak_rows(repo, monkeypatch):
    """逐行复核是兜底:store 若无视 ``allowed_source_ids``,界外与无证据的对象也
    绝不进清单——只会表现为分母对不上(``concurrent_change``),不会表现为泄漏。"""
    nb = _library(repo)
    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows

    def ignoring(db, notebook_id, object_type, after, limit, **_kwargs):
        return original(db, notebook_id, object_type, after, limit)

    monkeypatch.setattr(store, "knowledge_object_page_rows", ignoring)
    with _ticked(nb, ["sA", "sC"]):
        listed = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
    assert sorted(item.object_id for item in listed.items) == ["oA", "oMix"]


def test_evidence_refs_are_filtered_before_truncation(repo):
    """oMix 的前三条证据都在 sB(界外),第四条在 sA:先截断再过滤会留下三个
    不可引用的 id,先过滤才能把 sA 的元素交给引用步骤。"""
    nb = _library(repo)
    with _ticked(nb, ["sA", "sC"]):
        listed = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
        mix = next(item for item in listed.items if item.object_id == "oMix")
        assert mix.evidence_element_ids == ("el-sA-002",)
        citations = repo._runtime.ask_service().evidence_context \
            .collection_item_citations(listed.items, active_notebook_id=nb)
    assert citations["oMix"].element_id == "el-sA-002"
    assert citations["oMix"].source_id == "sA"


def test_without_a_ceiling_everything_is_byte_identical(repo, monkeypatch):
    """不装天花板:页查询不带新关键字、计数走旧的 ``knowledge_type_count_rows``、
    无证据对象照列、证据引用按原顺序截到上限。"""
    nb = _library(repo)
    store = repo._runtime.knowledge
    original_page = store.knowledge_object_page_rows
    page_kwargs: list = []

    def spy_page(*args, **kwargs):
        page_kwargs.append(dict(kwargs))
        return original_page(*args, **kwargs)

    def no_count(*_args, **_kwargs):
        raise AssertionError("no-ceiling builds must not call count_knowledge")

    monkeypatch.setattr(store, "knowledge_object_page_rows", spy_page)
    monkeypatch.setattr(store, "count_knowledge", no_count)
    collection_map = repo.collection_catalog.collection_map(nb)
    listed = repo.collection_enumeration.enumerate_kg_objects(
        nb, "concept", budget=_budget())
    elements = repo.collection_enumeration.enumerate_elements(
        nb, "formula", budget=_budget())

    assert dict(collection_map.kg_objects)["concept"] == 4
    assert collection_map.element_count("formula") == 6
    assert sorted(item.object_id for item in listed.items) == [
        "oA", "oB", "oMix", "oNone"]
    assert page_kwargs and all(kwargs == {} for kwargs in page_kwargs)
    mix = next(item for item in listed.items if item.object_id == "oMix")
    assert mix.evidence_element_ids == (
        "el-sB-001", "el-sB-002", "el-sB-003")[:MAX_EVIDENCE_REFS]
    assert elements.coverage.total == 6 and elements.coverage.complete is True
    assert list(repo.collection_catalog._kg_counts) == [(nb, "")]


def test_kg_count_memo_is_keyed_by_the_ceiling(repo):
    """同一 seq 下两份不同的天花板是两个不同的数,不能互相命中。"""
    nb = _library(repo)
    catalog = repo.collection_catalog
    with _ticked(nb, ["sA"]):
        assert dict(catalog.collection_map(nb).kg_objects)["concept"] == 2
    with _ticked(nb, ["sB"]):
        assert dict(catalog.collection_map(nb).kg_objects)["concept"] == 2
    with _ticked(nb, ["sC"]):
        assert dict(catalog.collection_map(nb).kg_objects)["concept"] == 0
    assert dict(catalog.collection_map(nb).kg_objects)["concept"] == 4
    keys = list(catalog._kg_counts)
    assert len(keys) == 4 and (nb, "") in keys
    assert len({digest for _nb, digest in keys}) == 4


def test_kg_count_memo_stays_bounded_across_ceilings(repo, monkeypatch):
    monkeypatch.setattr(collection_catalog, "_MAX_CACHED_NOTEBOOKS", 2)
    nb = _library(repo)
    for ticked in (["sA"], ["sB"], ["sC"]):
        with _ticked(nb, ticked):
            repo.collection_catalog.collection_map(nb)
    assert len(repo.collection_catalog._kg_counts) == 2


def test_a_deny_all_ceiling_lists_and_counts_nothing(repo):
    """``()`` 是显式拒绝,不是「没有天花板」——连 L3 的 memo 键都不能与不装
    天花板的那一格相撞(先把那一格焐热,再看拒绝全部的运行读到什么)。"""
    nb = _library(repo)
    assert dict(repo.collection_catalog.collection_map(nb).kg_objects)[
        "concept"] == 4
    with source_scope_context(nb, None, None, notebook_source_ceilings={nb: []}):
        collection_map = repo.collection_catalog.collection_map(nb)
        listed = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
        elements = repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget())
    assert dict(collection_map.kg_objects)["concept"] == 0
    assert collection_map.element_count("formula") == 0
    assert collection_map.sources == 0
    assert listed.items == () and listed.coverage.complete is True
    assert elements.items == () and elements.coverage.complete is True


def test_legacy_exclude_scope_is_materialized_not_ignored(repo):
    """旧的 exclude 形状(直调或早期持久化的报告范围):``scoped_allowed_source_ids``
    对它返回 None,但它确实在约束——必须按活的来源清单物化,而不是当成没有天花板。"""
    nb = _library(repo)
    with source_scope_context(nb, {"mode": "exclude", "source_ids": ["sB"]}):
        collection_map = repo.collection_catalog.collection_map(nb)
        listed = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
    assert collection_map.element_count("formula") == 3
    assert dict(collection_map.kg_objects)["concept"] == 2
    assert sorted(item.object_id for item in listed.items) == ["oA", "oMix"]
    assert listed.coverage.complete is True


# ----------------------------------------------------------- 指纹

def _hook_first_page(monkeypatch, store, name, side_effect):
    original = getattr(store, name)
    state = {"pages": 0}

    def hook(*args, **kwargs):
        rows = original(*args, **kwargs)
        state["pages"] += 1
        if state["pages"] == 1:
            side_effect()
        return rows

    monkeypatch.setattr(store, name, hook)


def test_an_out_of_ceiling_upload_mid_walk_is_not_a_concurrent_change(
    repo, monkeypatch,
):
    nb = _library(repo)
    _hook_first_page(
        monkeypatch, repo._runtime.source_store, "element_page_rows",
        lambda: _src(repo, nb, "sLate", formulas=4),
    )
    with _ticked(nb, ["sA", "sC"]):
        result = repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget(page_size=1))
    assert result.coverage.complete is True
    assert result.coverage.returned_total == result.coverage.total == 3
    assert "sLate" not in {item.source_id for item in result.items}


def test_without_a_binding_ceiling_an_upload_mid_walk_stays_outside(
    repo, monkeypatch,
):
    """天花板不生效(全选、首次读集合时未漂移,``ceiling_binds=False``)时读取走快
    路径,但每次读取都拿手里的来源行对冻结天花板做读后核验(codex #817 r1):清单
    进行中上传的来源由收尾读取发现,记下漂移、这次读取改为绑定——清单仍是冻结集合
    的完整清单,之后新开的清单也不含它。"""
    nb = _library(repo)
    _hook_first_page(
        monkeypatch, repo._runtime.source_store, "element_page_rows",
        lambda: _src(repo, nb, "sLate", formulas=4),
    )
    with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
        with _all_ticked(repo, nb):
            result = repo.collection_enumeration.enumerate_elements(
                nb, "formula", budget=_budget(page_size=1), ceiling_binds=False)
            fresh = repo.collection_enumeration.enumerate_elements(
                nb, "formula", budget=_budget(), ceiling_binds=False)
    assert result.coverage.complete is True
    assert result.coverage.returned_total == result.coverage.total == 6
    assert "sLate" not in {item.source_id for item in result.items}
    assert "sLate" not in {item.source_id for item in fresh.items}
    assert fresh.coverage.complete is True and fresh.coverage.total == 6


def test_an_in_ceiling_reparse_mid_walk_is_still_a_concurrent_change(
    repo, monkeypatch,
):
    nb = _library(repo)

    def reparse():
        with repo._write() as db:
            db.execute("UPDATE sources SET updated_at=? WHERE id='sC'", (LATER,))

    _hook_first_page(
        monkeypatch, repo._runtime.source_store, "element_page_rows", reparse)
    with _ticked(nb, ["sA", "sC"]):
        result = repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget(page_size=1))
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE


def test_an_out_of_ceiling_reparse_mid_walk_does_not_trip_the_roster(
    repo, monkeypatch,
):
    nb = _library(repo)

    def reparse_unticked():
        with repo._write() as db:
            db.execute("UPDATE sources SET updated_at=? WHERE id='sB'", (LATER,))

    _hook_first_page(
        monkeypatch, repo._runtime.source_store, "source_listing_rows",
        reparse_unticked)
    with _ticked(nb, ["sA", "sC"]):
        result = repo.collection_enumeration.enumerate_sources(
            nb, budget=_budget())
    assert result.coverage.complete is True
    assert [item.source_id for item in result.items] == ["sA", "sC"]


def test_a_resumed_chain_ignores_an_out_of_ceiling_upload_between_calls(repo):
    nb = _library(repo)
    with _ticked(nb, ["sA", "sC"]):
        first = repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget(page_size=1, max_rows=1))
        _src(repo, nb, "sLate", formulas=2)
        rest, coverage, _calls = _walk_all(
            lambda cursor: repo.collection_enumeration.enumerate_elements(
                nb, "formula", budget=_budget(page_size=1, max_rows=1),
                cursor=cursor or first.cursor))
    assert first.cursor is not None
    assert coverage.complete is True
    assert coverage.returned_total == 3


# ------------------------------------------------ 显式 source_id / 冻结后上传

def _refusal(repo, notebook_id, source_id):
    with pytest.raises(ValueError) as caught:
        repo.collection_enumeration.enumerate_elements(
            notebook_id, "formula", source_id=source_id, budget=_budget())
    return str(caught.value)


def test_explicit_out_of_ceiling_source_is_refused_like_a_non_member(
    repo, monkeypatch,
):
    """E-4:以前只核对库成员身份,点名一个未勾选文档的 id 就能列出并引用它的
    元素。现在按该行所属库的天花板拒绝,且与「根本不存在」是**同一句**——拒绝
    本身不能拿来探测一篇未勾选的文档在不在。被拒的来源一次页查询都不发。"""
    nb = _library(repo)
    pages: list = []
    original = repo._runtime.source_store.element_page_rows
    monkeypatch.setattr(
        repo._runtime.source_store, "element_page_rows",
        lambda *args, **kwargs: pages.append(args) or original(*args, **kwargs),
    )
    with _ticked(nb, ["sA", "sC"]):
        unticked = _refusal(repo, nb, "sB")
        missing = _refusal(repo, nb, "no-such-source")
        allowed = repo.collection_enumeration.enumerate_elements(
            nb, "formula", source_id="sA", budget=_budget())
    assert unticked == "source is not in scope: 'sB'"
    assert missing == "source is not in scope: 'no-such-source'"
    assert all(args[1] != "sB" for args in pages)
    assert allowed.coverage.complete is True and len(allowed.items) == 2


@pytest.fixture
def rrepo(tmp_path, monkeypatch):
    """推理循环用的仓库:与 ``test_reasoning_enumeration_tools`` 同款隔离(本机
    .env 的真实推理端点不得被打到)。"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    try:
        yield instance
    finally:
        instance.close()


def test_the_reflect_loop_sees_an_unticked_id_exactly_as_a_missing_one(rrepo):
    """推理循环读到的跳过步骤,对「冻结之外的来源」与「不存在的来源」逐字相同
    (只有报错里回显的 id 不同)——循环照常继续,清单里什么都没多出来。"""
    notebook = _reasoning_seed(rrepo, formulas=2)
    _src(rrepo, notebook.id, "s2", formulas=2)        # 冻结之后才有的来源
    skips = {}
    for source_id in ("s2", "no-such-source"):
        llm = _SeqLLM([_enumerate_action(source_id=source_id),
                       {"next_action": "answer", "sufficient": True}])
        retriever, limits = _retriever(rrepo, llm)
        with _ticked(notebook.id, ["s1"], narrowed=False):
            result = retriever.run(notebook.id, "哪些公式", "", limits=limits)
        assert result.enumerations == []
        skips[source_id] = _skips(result)["enumeration_rejected"]
    unticked, missing = skips["s2"], skips["no-such-source"]
    assert unticked.summary == missing.summary
    assert unticked.detail["error"].replace("'s2'", "'X'") == (
        missing.detail["error"].replace("'no-such-source'", "'X'"))
    assert {k: v for k, v in unticked.detail.items() if k != "error"} == {
        k: v for k, v in missing.detail.items() if k != "error"}


def test_a_fail_closed_run_raises_on_an_unticked_id(rrepo):
    notebook = _reasoning_seed(rrepo, formulas=2)
    _src(rrepo, notebook.id, "s2", formulas=2)
    llm = _SeqLLM([_enumerate_action(source_id="s2")])
    retriever, limits = _retriever(rrepo, llm, fail_closed=True)
    with _ticked(notebook.id, ["s1"], narrowed=False):
        with pytest.raises(ValueError, match="source is not in scope: 's2'"):
            retriever.run(notebook.id, "哪些公式", "", limits=limits)


def test_a_source_uploaded_after_the_freeze_is_neither_listed_nor_named(repo):
    """浏览器默认「全选」冻结成 include 清单(narrowed=False);冻结之后上传的
    sNew 不在清单里:不计数、不列出、点名也拒绝(E-3)。"""
    nb = _library(repo)
    with _ticked(nb, ["sA", "sB", "sC"], narrowed=False):
        _src(repo, nb, "sNew", formulas=5)
        _kg(repo, nb, "oNew", [("sNew", "el-sNew-001")])
        collection_map = repo.collection_catalog.collection_map(nb)
        roster = repo.collection_enumeration.enumerate_sources(
            nb, budget=_budget())
        elements = repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget())
        named = _refusal(repo, nb, "sNew")
        kg = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
    assert collection_map.sources == 3
    assert collection_map.element_count("formula") == 6
    assert "sNew" not in {item.source_id for item in roster.items}
    assert roster.coverage.complete is True
    assert "sNew" not in {item.source_id for item in elements.items}
    assert elements.coverage.complete is True
    assert named == "source is not in scope: 'sNew'"
    assert "oNew" not in {item.object_id for item in kg.items}


def test_per_library_freeze_hides_a_later_upload_from_roster_and_map(repo):
    """E-3 回归钉:逐库冻结(全局问答的形状)之后上传的来源,既不进
    ``enumerate_sources``,也不进集合地图的 ``sources:``。"""
    first, second = _two_libraries(repo)
    with _peer_run([first, second], {first: ["x1", "x2"], second: ["y1", "y2"]}):
        _src(repo, second, "yLate", formulas=2)
        collection_map = repo.collection_catalog.collection_map(first)
        roster = repo.collection_enumeration.enumerate_sources(
            first, budget=_budget())
    assert collection_map.sources == 4
    assert collection_map.element_count("formula") == 10
    assert [item.source_id for item in roster.items] == ["x1", "x2", "y1", "y2"]
    assert roster.coverage.complete is True


def test_title_resolution_cannot_reach_an_unticked_source(repo):
    nb = _library(repo)
    with _ticked(nb, ["sA", "sC"]):
        found, matches, truncated = repo.collection_enumeration \
            .resolve_source_title(nb, "formula", "标题-sB")
    assert (found, matches, truncated) == ("", 0, False)


# ------------------------------------------------------ 对等(全局)模式

def _two_libraries(repo):
    first = repo.create_notebook(NotebookCreate(name="甲")).id
    second = repo.create_notebook(NotebookCreate(name="乙")).id
    _src(repo, first, "x1", formulas=1)
    _src(repo, first, "x2", formulas=2)
    _src(repo, second, "y1", formulas=3)
    _src(repo, second, "y2", formulas=4)
    _kg(repo, first, "ox", [("x2", "el-x2-001")])
    _kg(repo, second, "oy", [("y1", "el-y1-001")])
    return first, second


def test_peer_mode_judges_each_row_against_its_own_library(repo):
    first, second = _two_libraries(repo)
    ceilings = {first: ["x1"], second: ["y1"]}
    with _peer_run([first, second], ceilings):
        collection_map = repo.collection_catalog.collection_map(first)
        elements = repo.collection_enumeration.enumerate_elements(
            first, "formula", budget=_budget())
        kg = repo.collection_enumeration.enumerate_kg_objects(
            first, "concept", budget=_budget())
        roster = repo.collection_enumeration.enumerate_sources(
            first, budget=_budget())
        refused = _refusal(repo, first, "y2")
    assert collection_map.element_count("formula") == 4           # x1 1 + y1 3
    assert dict(collection_map.kg_objects)["concept"] == 1        # 只有 oy
    assert {item.source_id for item in elements.items} == {"x1", "y1"}
    assert elements.coverage.complete is True
    assert [item.object_id for item in kg.items] == ["oy"]
    assert kg.coverage.total == 1 and kg.coverage.complete is True
    assert [item.source_id for item in roster.items] == ["x1", "y1"]
    assert refused == "source is not in scope: 'y2'"


def test_peer_mode_has_no_current_notebook(repo):
    """E-5(a):对等模式没有「当前笔记本」——名义 active 只是第一个被选中的库。
    ``local_only`` 被忽略(否则只会列出一个随意的库并称之为当前笔记本),地图也
    不再渲染 ``(current notebook: N)``;单库运行逐字不变。"""
    first, second = _two_libraries(repo)
    ceilings = {first: ["x1", "x2"], second: ["y1", "y2"]}
    single = collection_catalog.render_collection_map(
        repo.collection_catalog.collection_map(first))
    with _peer_run([first, second], ceilings):
        peer_map = repo.collection_catalog.collection_map(first)
        peer_text = collection_catalog.render_collection_map(peer_map)
        local = repo.collection_enumeration.enumerate_sources(
            first, budget=_budget(), local_only=True)
    assert single.endswith("sources: 2 (current notebook: 2)")
    assert peer_text.endswith("sources: 4")
    assert "current notebook" not in peer_text
    assert [item.source_id for item in local.items] == ["x1", "x2", "y1", "y2"]
    assert local.coverage.complete is True
    # 渲染按调用时的运行形状判断:同一份地图在单库上下文里仍带括号。
    assert collection_catalog.render_collection_map(peer_map).endswith(
        f"sources: 4 (current notebook: {peer_map.active_sources})")


def test_global_ask_knowhow_object_citation_no_longer_voids_the_answer(repo):
    """线上 bug 回归:全局问答的冻结天花板 = 每个参与库的可见来源,Knowhow 投影
    源是隐藏来源,不在天花板里。投影出来的 KG 对象以前被引到投影元素上,终态
    复核判 ``out_of_ceiling``,整份答案作废。

    现在:同时有可见证据的对象引到可见元素;只有投影证据的对象不出引用卡——
    两种情况下终态复核都不作废。挂载进名义 active 的参考库(对等模式里的另一个
    参与库)同样覆盖。
    """
    anchor = repo.create_notebook(NotebookCreate(name="甲")).id
    peer = repo.create_notebook(NotebookCreate(name="乙")).id
    repo.mark_notebook_base(peer)
    repo.replace_notebook_bases(anchor, [peer], _ACTOR)
    items = []
    for notebook_id, prefix in ((anchor, "a"), (peer, "p")):
        visible = f"{prefix}-doc"
        projection = f"{prefix}-knowhow"
        _src(repo, notebook_id, visible, formulas=1)
        _src(repo, notebook_id, projection, source_type="knowhow")
        _element(repo, projection, f"el-{projection}-001")
        items.append(KgObjectItem(
            object_id=f"{prefix}-mixed", object_type="concept", name="n",
            section_path="", notebook_id=notebook_id, tier="personal",
            evidence_element_ids=(f"el-{projection}-001", f"el-{visible}-001"),
        ))
        items.append(KgObjectItem(
            object_id=f"{prefix}-projection-only", object_type="concept",
            name="n", section_path="", notebook_id=notebook_id,
            tier="personal", evidence_element_ids=(f"el-{projection}-001",),
        ))
    sources = repo._runtime.source_store
    ceilings = {
        anchor: sources.all_visible_source_ids(anchor),
        peer: sources.all_visible_source_ids(peer),
    }
    assert ceilings == {anchor: ["a-doc"], peer: ["p-doc"]}
    evidence_context = repo._runtime.ask_service().evidence_context
    with _peer_run([anchor, peer], ceilings):
        citations = evidence_context.collection_item_citations(
            items, active_notebook_id=anchor)
    # The terminal check (``global_citation_check.judge_reference``, the
    # per-reference verdict that replaced whole-answer voiding) passes every
    # card: each one names an in-ceiling source, so none is ``out_of_ceiling``.
    for citation in citations.values():
        ceiling = set(ceilings[citation.notebook_id])
        snapshot = (citation.source_id, "fingerprint")
        assert judge_reference(
            citation, evidence={citation.element_id: snapshot},
            current={citation.element_id: snapshot}, siblings=(),
            ceiling=ceiling, live_sources=ceiling,
        ) is None, citation
    assert set(citations) == {"a-mixed", "p-mixed"}
    assert citations["a-mixed"].element_id == "el-a-doc-001"
    assert citations["p-mixed"].element_id == "el-p-doc-001"
    assert citations["p-mixed"].notebook_id == peer


def test_citations_drop_document_and_element_rows_outside_the_ceiling(repo):
    nb = _library(repo)
    rows = [
        SourceItem(source_id="sA", source_title="甲", doc_type_label="",
                   summary="", notebook_id=nb, tier="personal"),
        SourceItem(source_id="sB", source_title="乙", doc_type_label="",
                   summary="", notebook_id=nb, tier="personal"),
    ]
    listed = repo.collection_enumeration.enumerate_elements(
        nb, "formula", budget=_budget())         # 无天花板:三个来源都列
    evidence_context = repo._runtime.ask_service().evidence_context
    with _ticked(nb, ["sA", "sC"]):
        documents = evidence_context.collection_item_citations(
            rows, active_notebook_id=nb)
        elements = evidence_context.collection_item_citations(
            listed.items, active_notebook_id=nb)
    assert set(documents) == {"sA"}
    assert {citation.source_id for citation in elements.values()} == {"sA", "sC"}
    assert "el-sB-001" not in elements


# ----------------------------------------------------------- Knowhow 表计数

def _knowhow_library(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb")).id
    base = repo.create_notebook(NotebookCreate(name="base")).id
    repo.mark_notebook_base(base)
    columns = [{"name": "Topic", "role": "anchor"}]
    repo.create_knowhow_table(notebook, "T1", "", columns)
    repo.create_knowhow_table(base, "T2", "", columns)
    repo.replace_notebook_bases(notebook, [base], _ACTOR)
    return notebook, base


def test_knowhow_table_count_is_what_the_executor_can_reach(repo):
    """E-5:Knowhow 全量枚举只读当前库的表,且在收窄与对等模式下不跑——地图
    只数它够得着的表。挂载库的表不再出现在地图里(文档化的行为变化)。"""
    notebook, base = _knowhow_library(repo)
    catalog = repo.collection_catalog
    assert catalog.collection_map(notebook).knowhow_tables == 1
    with _ticked(notebook, [], narrowed=True):
        assert catalog.collection_map(notebook).knowhow_tables == 0
    with _ticked(notebook, [], narrowed=False):
        assert catalog.collection_map(notebook).knowhow_tables == 1
    with _peer_run([notebook, base], {notebook: [], base: []}):
        assert catalog.collection_map(notebook).knowhow_tables == 0
    assert collection_catalog.knowhow_enumeration_reachable() is True


# --------------------------------------------------- 私有 Memory 证据引用

def _memory_source(repo, notebook_id, source_id, element_id):
    """一个 Memory 合成源(``source_type='memory'``)与它的一个元素。"""
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, "别人的记忆", "memory", "extracted",
             "extracted", NOW, NOW),
        )
    _element(repo, source_id, element_id, element_type="formula")
    repo.collection_catalog.invalidate()


@pytest.mark.parametrize("scope", ["none", "default", "narrowed"])
def test_a_memory_evidence_element_never_reaches_the_citation(repo, scope):
    """可见来源拥有的对象合并进了一条 Memory 证据(排在最前):无论有没有天花板,
    Memory 元素都不进 ``evidence_element_ids``、也就到不了
    ``collection_item_citations``——「清单里永远不含私有 Memory」覆盖引用会摘录
    的正文,不只是行本身。今天仍可能没有天花板(``none``);默认冻结(``default``,
    全选、未收窄)与真收窄(``narrowed``)同样成立。"""
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _src(repo, nb, "sV", formulas=1)
    _memory_source(repo, nb, "sMem", "el-sMem-001")
    _kg(repo, nb, "oMerged", [("sMem", "el-sMem-001"), ("sV", "el-sV-001")],
        owner_source_id="sV")
    contexts = {
        "none": lambda: source_scope_context(nb, None),
        "default": lambda: _ticked(nb, ["sV"], narrowed=False),
        "narrowed": lambda: _ticked(nb, ["sV"], narrowed=True),
    }
    evidence_context = repo._runtime.ask_service().evidence_context
    with contexts[scope]():
        listed = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
        citations = evidence_context.collection_item_citations(
            listed.items, active_notebook_id=nb)
    merged = next(item for item in listed.items if item.object_id == "oMerged")
    assert merged.evidence_element_ids == ("el-sV-001",)
    assert citations["oMerged"].element_id == "el-sV-001"
    assert all(c.source_id != "sMem" for c in citations.values())


# ------------------------------------------------ 目录概览的「仅勾选的来源」

def _overview_repo(tmp_path):
    return SQLiteRepository(Settings(
        _env_file=None, database_url=f"sqlite:///{tmp_path / 'o.db'}",
        storage_dir=str(tmp_path / "os"), model_services_config="",
        llm_log_enabled=False, event_log_enabled=False,
        document_overview_max_elements=3,
    ))


def test_the_catalog_overview_card_discloses_a_narrowed_scope(tmp_path):
    """chunk 引擎的目录概览与 reasoning 的清单用同一个判据
    (``reasoning_retrieval.unsafe_scope_restricted``)盖 ``source_scoped``:收窄时
    结果卡带 ``source_scoped: true``,未收窄时这个键根本不出现(字节不变)。"""
    from app.models.source_scope import SourceScope
    from tests.test_document_overview import AnswerClient, ask, seed
    from tests.model_testkit import bind_chat_client

    overview_repo = _overview_repo(tmp_path)
    try:
        nb = overview_repo.create_notebook(NotebookCreate(name="资料")).id
        seed(overview_repo, nb, "a", "手册", "可见摘要")
        seed(overview_repo, nb, "b", "未选文档", "不应出现")
        bind_chat_client(overview_repo, "ask_answer", AnswerClient())
        enumeration = overview_repo.collection_enumeration
        original = enumeration.enumerate_sources
        forwarded: list = []

        def spy(*args, **kwargs):
            forwarded.append(kwargs.get("ceiling_binds", "absent"))
            return original(*args, **kwargs)

        enumeration.enumerate_sources = spy
        narrowed = ask(
            overview_repo, nb, "这个库中的文档分别介绍了什么",
            source_scope=SourceScope(mode="include", source_ids=["a"]),
        )
        ticked = ask(
            overview_repo, nb, "这个库中的文档分别介绍了什么",
            source_scope=SourceScope(
                mode="include", source_ids=["a", "b"], narrowed=False),
        )
        whole = ask(overview_repo, nb, "这个库中的文档分别介绍了什么")
    finally:
        overview_repo.close()
    assert narrowed.result_sets[0].source_scoped is True
    assert narrowed.result_sets[0].model_dump()["source_scoped"] is True
    assert [item.source_id for item in narrowed.result_sets[0].items] == ["a"]
    assert "source_scoped" not in whole.result_sets[0].model_dump()
    assert whole.result_sets[0].coverage.total == 2
    # 全选、未收窄、未漂移:与不带范围逐字相同,且判词原样转给了执行器
    # (不转就会落到执行器的安全默认值「施加天花板」)。
    assert ticked.result_sets[0].model_dump() == whole.result_sets[0].model_dump()
    assert forwarded == [True, False, False]


# ------------------------------------------------ section_path 只取自可读的属主

def _owned_breadcrumb_library(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _src(repo, nb, "s1", formulas=1)
    _src(repo, nb, "s2", formulas=1)
    # X 属于 s2(面包屑是 s2 的标题),s1 只贡献了后来合并进来的证据。
    _kg(repo, nb, "oX", [("s1", "el-s1-001"), ("s2", "el-s2-001")],
        owner_source_id="s2", section_path="3.2 竞品价格")
    _kg(repo, nb, "oY", [("s1", "el-s1-001")],
        owner_source_id="s1", section_path="1.1 概述")
    return nb


def test_section_path_comes_only_from_a_readable_owner(repo):
    """天花板 {s1}:X 因 s1 的证据被列出,但它的 ``section_path`` 是 s2(未勾选)
    的章节标题,不得随清单出去;名字保留。Y 的属主在天花板内,面包屑照常。"""
    nb = _owned_breadcrumb_library(repo)
    with _ticked(nb, ["s1"]):
        scoped = repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget())
    unscoped = repo.collection_enumeration.enumerate_kg_objects(
        nb, "concept", budget=_budget())
    by_id = {item.object_id: item for item in scoped.items}
    assert by_id["oX"].section_path == "" and by_id["oX"].name == "oX"
    assert by_id["oY"].section_path == "1.1 概述"
    assert {item.object_id: item.section_path for item in unscoped.items} == {
        "oX": "3.2 竞品价格", "oY": "1.1 概述"}


# ------------------------------------------ 游标身份含天花板摘要(三类集合)

def test_a_cursor_cut_under_one_ceiling_is_refused_under_another(repo):
    """同一批来源、第 1 页与第 2 页之间换了勾选:续跑被拒(``concurrent_change``,
    与作用域变动同一条路),新天花板下重新开一份清单是完整的;天花板不变则原样
    续上;不装天花板时游标的天花板字段为空(与今天逐值相同)。"""
    nb = _library(repo)
    small = dict(page_size=1, max_rows=1)
    calls = {
        "elements": lambda cursor: repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget(**small), cursor=cursor),
        "kg": lambda cursor: repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget(**small), cursor=cursor),
        "sources": lambda cursor: repo.collection_enumeration.enumerate_sources(
            nb, budget=_budget(**small), cursor=cursor),
    }
    for name, call in calls.items():
        with _ticked(nb, ["sA", "sC"]):
            first = call(None)
        assert first.cursor is not None and first.cursor.scope_ceiling, name
        with _ticked(nb, ["sA", "sB", "sC"]):
            moved = call(first.cursor)
            fresh, fresh_coverage, _ = _walk_all(call)
        assert moved.items == () and moved.cursor is None, name
        assert moved.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE
        assert fresh_coverage.complete is True, name
        with _ticked(nb, ["sA", "sC"]):
            rest, coverage, _ = _walk_all(
                lambda cursor: call(cursor or first.cursor))
        assert coverage.complete is True, name
        unscoped = call(None)
        assert unscoped.cursor is not None and unscoped.cursor.scope_ceiling == ""


def test_a_ceiling_change_that_moves_no_signal_still_moves_every_cursor(repo):
    """换一份只多了隐藏来源(没有信号行、也不动图 seq)的天花板:三类集合的续跑
    都必须被拒。KG 游标的身份原本只有 seq 向量,不拒就会悄悄跳过新天花板在游标
    之前放进来的对象;元素与来源清单的指纹只哈希在天花板内的信号行,同样看不见
    这种变化——身份里的天花板摘要是唯一能看见它的东西。"""
    nb = _library(repo)
    _src(repo, nb, "sA2", formulas=2)
    budget = dict(page_size=1, max_rows=1)
    calls = {
        "elements": lambda cursor: repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget(**budget), cursor=cursor),
        "kg": lambda cursor: repo.collection_enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget(**budget), cursor=cursor),
        "sources": lambda cursor: repo.collection_enumeration.enumerate_sources(
            nb, budget=_budget(**budget), cursor=cursor),
    }
    base = {"mode": "include", "source_ids": ["sA", "sA2"], "narrowed": True}
    for name, call in calls.items():
        with source_scope_context(nb, base):
            first = call(None)
        with source_scope_context(
            nb, {**base, "hidden_source_ids": ["memory-of-mine"]}
        ):
            moved = call(first.cursor)
        assert first.cursor is not None, name
        assert moved.coverage.truncated_reason == TRUNCATED_CONCURRENT_CHANGE, name
        assert moved.cursor is None, name


# --------------------------------- 地图的 Knowhow 表数与执行器的闸是同一个判据

def _knowhow_notebook(repo):
    notebook = repo.create_notebook(NotebookCreate(name="nb")).id
    _src(repo, notebook, "s1", formulas=1)
    repo.create_knowhow_table(
        notebook, "T1", "", [{"name": "Topic", "role": "anchor"}])
    return notebook


def _frozen_all_selected(repo, notebook_id, visible):
    """浏览器「全选」形状的冻结:可见来源 + 本人的隐藏来源,narrowed=False。"""
    owner = repo.current_user().id
    hidden = repo._runtime.source_store.hidden_source_ids(notebook_id, owner)
    return source_scope_context(notebook_id, {
        "mode": "include", "source_ids": list(visible), "narrowed": False,
        "hidden_source_ids": list(hidden), "owner_id": owner,
    })


@pytest.mark.parametrize("drifted", [False, True])
def test_map_knowhow_count_follows_the_executor_gate_under_drift(rrepo, drifted):
    """冻结之后又出现一个可见来源 = 漂移:确定性 Knowhow 全量枚举让路
    (``AskService._knowhow_completeness_in_scope`` 为假),地图也必须报 0 张表;
    冻结完好时两边都照常(1 张)。两边读的是同一个按 run 记忆的判定。"""
    from app.services.reasoning_retrieval import knowhow_completeness_reachable

    notebook = _knowhow_notebook(rrepo)
    llm = _SeqLLM([{"next_action": "answer", "sufficient": True}])
    retriever, limits = _retriever(rrepo, llm)
    ask_service = rrepo._runtime.ask_service()
    with _frozen_all_selected(rrepo, notebook, ["s1"]):
        if drifted:
            _src(rrepo, notebook, "s-late", formulas=1)
        with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
            gate = ask_service._knowhow_completeness_in_scope(notebook)
            assert knowhow_completeness_reachable(
                retriever.retrieval, notebook) is gate
            result = retriever.run(notebook, "哪些公式", "", limits=limits)
    assert gate is (not drifted)
    expected = 0 if drifted else 1
    assert f"knowhow tables: {expected} |" in result.collection_map_text


# --------------------------------------- 对等模式:引用只看本 run 冻结的参与集

def _global_libraries(repo):
    """A 锚点;B 挂在 A 上且被选中;C 被选中但没挂在 A 上;D 挂在 A 上却没被选中。"""
    ids = {}
    for name in ("A", "B", "C", "D"):
        ids[name] = repo.create_notebook(NotebookCreate(name=name)).id
        _src(repo, ids[name], f"s{name}", formulas=1)
    for name in ("B", "D"):
        repo.mark_notebook_base(ids[name])
    repo.replace_notebook_bases(ids["A"], [ids["B"], ids["D"]], _ACTOR)
    return ids


def test_peer_citations_follow_the_frozen_participant_set(repo):
    ids = _global_libraries(repo)
    selected = [ids["A"], ids["B"], ids["C"]]
    ceilings = {ids[n]: [f"s{n}"] for n in ("A", "B", "C")}
    evidence_context = repo._runtime.ask_service().evidence_context
    stray = SourceItem(source_id="sD", source_title="D", doc_type_label="",
                       summary="", notebook_id=ids["D"], tier="personal")
    with _peer_run(selected, ceilings):
        roster = repo.collection_enumeration.enumerate_sources(
            ids["A"], budget=_budget())
        elements = repo.collection_enumeration.enumerate_elements(
            ids["A"], "formula", budget=_budget())
        documents = evidence_context.collection_item_citations(
            list(roster.items) + [stray], active_notebook_id=ids["A"])
        element_cites = evidence_context.collection_item_citations(
            elements.items, active_notebook_id=ids["A"])
    assert [item.source_id for item in roster.items] == ["sA", "sB", "sC"]
    assert set(documents) == {"sA", "sB", "sC"}           # C 有引用,D 没有
    assert {c.notebook_id for c in documents.values()} == set(selected)
    assert {c.source_id for c in element_cites.values()} == {"sA", "sB", "sC"}
    # 单库对照:A 的挂载表(B、D)照旧可引,C 不在挂载表里。
    single = evidence_context.collection_item_citations(
        [SourceItem(source_id=f"s{n}", source_title=n, doc_type_label="",
                    summary="", notebook_id=ids[n], tier="personal")
         for n in ("A", "B", "C", "D")],
        active_notebook_id=ids["A"])
    assert set(single) == {"sA", "sB", "sD"}


# ---------------------------- 全选未漂移(ceiling_binds=False)与无范围逐字相同

def _all_ticked(repo, notebook_id):
    """浏览器「全选」的冻结形状:可见来源全在、未收窄。"""
    visible = repo._runtime.source_store.all_visible_source_ids(notebook_id)
    return _ticked(notebook_id, visible, narrowed=False)


def _snapshot(repo, nb, **kwargs):
    enumeration = repo.collection_enumeration
    small = _budget(page_size=1, max_rows=1)
    return {
        "map": collection_catalog.render_collection_map(
            repo.collection_catalog.collection_map(nb, **kwargs)),
        "claims": enumeration.enumerate_kg_objects(
            nb, "claim", budget=_budget(), **kwargs),
        "concepts": enumeration.enumerate_kg_objects(
            nb, "concept", budget=_budget(), **kwargs),
        "formulas": enumeration.enumerate_elements(
            nb, "formula", budget=_budget(), **kwargs),
        "sources": enumeration.enumerate_sources(nb, budget=_budget(), **kwargs),
        "cursors": (
            enumeration.enumerate_elements(
                nb, "formula", budget=small, **kwargs).cursor,
            enumeration.enumerate_kg_objects(
                nb, "concept", budget=small, **kwargs).cursor,
            enumeration.enumerate_sources(nb, budget=small, **kwargs).cursor,
        ),
    }


def test_an_all_ticked_unnarrowed_run_is_byte_identical_to_an_unscoped_one(
    repo, monkeypatch,
):
    """产品决定(2026-09-29):浏览器全选也会冻结一份 include 清单(narrowed=False)。
    这样的运行只要没漂移(调用方的 ``ceiling_binds`` 为假),枚举与地图必须与
    引入天花板之前逐字相同:KG 语句里没有天花板参数,``count_knowledge`` 不被
    调用,没有证据的对象照样被列出、被计数(地图 ``claim 1``),游标也逐值相同
    (天花板摘要为空)。"""
    nb = _library(repo)
    _kg(repo, nb, "cNone", [], object_type="claim")
    store = repo._runtime.knowledge
    original = store.knowledge_object_page_rows
    page_kwargs: list = []

    def spy(*args, **kwargs):
        page_kwargs.append(dict(kwargs))
        return original(*args, **kwargs)

    unscoped = _snapshot(repo, nb)
    monkeypatch.setattr(store, "knowledge_object_page_rows", spy)
    monkeypatch.setattr(store, "count_knowledge", lambda *a, **k: (
        _ for _ in ()).throw(AssertionError("count_knowledge under no ceiling")))
    with _all_ticked(repo, nb):
        repo.collection_catalog.invalidate()
        ticked = _snapshot(repo, nb, ceiling_binds=False)
    assert "claim 1" in ticked["map"]
    assert ticked["map"] == unscoped["map"]
    assert [i.object_id for i in ticked["claims"].items] == ["cNone"]
    for name in ("claims", "concepts", "formulas", "sources"):
        assert ticked[name].items == unscoped[name].items, name
        assert ticked[name].coverage == unscoped[name].coverage, name
    assert ticked["cursors"] == unscoped["cursors"]
    assert all(cursor.scope_ceiling == "" for cursor in ticked["cursors"])
    assert page_kwargs and all(kwargs == {} for kwargs in page_kwargs)


def test_an_all_ticked_reasoning_run_matches_an_unscoped_one(rrepo):
    """推理循环一侧的 A/B:浏览器全选冻结(可见 + 本人隐藏,未收窄、未漂移)与
    不带范围的 run 看到的是同一份东西——地图文本(``claim 1``)、清单(无证据的
    对象在内)、枚举步骤的摘要与 detail、回喂 reflect 的 prompt 都逐字相同。"""
    notebook = _reasoning_seed(rrepo, formulas=2)
    _kg(rrepo, notebook.id, "cNone", [], object_type="claim")

    def run(scoped):
        llm = _SeqLLM([
            {"next_action": "enumerate_kg_objects",
             "enumerate": {"object_type": "claim"}},
            {"next_action": "answer", "sufficient": True},
        ])
        retriever, limits = _retriever(rrepo, llm)
        rrepo.collection_catalog.invalidate()
        with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
            if scoped:
                visible = rrepo._runtime.source_store.all_visible_source_ids(
                    notebook.id)
                with _frozen_all_selected(rrepo, notebook.id, visible):
                    result = retriever.run(notebook.id, "有哪些论断", "",
                                           limits=limits)
            else:
                result = retriever.run(notebook.id, "有哪些论断", "",
                                       limits=limits)
        return result, llm

    unscoped, unscoped_llm = run(False)
    ticked, ticked_llm = run(True)
    assert "claim 2" in ticked.collection_map_text  # store_kg 的 C1 + cNone
    assert ticked.collection_map_text == unscoped.collection_map_text
    assert [o.items for o in ticked.enumerations] == [
        o.items for o in unscoped.enumerations]
    assert "cNone" in {i.object_id for i in ticked.enumerations[0].items}

    def enumerate_steps(result):
        return [(s.summary, s.detail) for s in result.trace
                if s.step_type == "enumerate"]

    assert enumerate_steps(ticked) == enumerate_steps(unscoped)

    # 回喂 reflect 的集合地图与枚举账目逐行相同(清单拿到的是同一批条目,no_progress
    # 因此不会翻转)。首轮候选那一段不比:首轮 KG 检索臂在冻结的全选清单下本来就
    # 按清单下推(与本 PR 之前一样),无证据对象不会成为首轮候选——那不是枚举层。
    def enumeration_lines(prompts):
        return [
            [line for line in prompt.splitlines()
             if "Collections in scope" in line or "枚举" in line]
            for prompt in prompts
        ]

    ticked_lines = enumeration_lines(ticked_llm.reflect_prompts)
    assert ticked_lines == enumeration_lines(unscoped_llm.reflect_prompts)
    assert len(ticked_lines) >= 2 and any(
        "枚举" in line for line in ticked_lines[-1])


def test_the_safe_default_still_binds_the_all_ticked_freeze(repo):
    """没传 ``ceiling_binds`` 的调用方拿到的是安全默认值:照样施加天花板
    (宁可少列,也不漏);无证据对象因此不在清单里。"""
    nb = _library(repo)
    _kg(repo, nb, "cNone", [], object_type="claim")
    with _all_ticked(repo, nb):
        bound = repo.collection_enumeration.enumerate_kg_objects(
            nb, "claim", budget=_budget())
        cursor = repo.collection_enumeration.enumerate_elements(
            nb, "formula", budget=_budget(page_size=1, max_rows=1)).cursor
    assert bound.items == ()
    assert cursor.scope_ceiling != ""


def test_a_global_run_binds_whatever_ceiling_binds_says(repo):
    """对等(全局)运行里逐库天花板恒生效,``ceiling_binds=False`` 不能关掉它。"""
    first, second = _two_libraries(repo)
    with _peer_run([first, second], {first: ["x1"], second: ["y1"]}):
        kg = repo.collection_enumeration.enumerate_kg_objects(
            first, "concept", budget=_budget(), ceiling_binds=False)
        elements = repo.collection_enumeration.enumerate_elements(
            first, "formula", budget=_budget(), ceiling_binds=False)
        collection_map = repo.collection_catalog.collection_map(
            first, ceiling_binds=False)
    assert [item.object_id for item in kg.items] == ["oy"]
    assert {item.source_id for item in elements.items} == {"x1", "y1"}
    assert collection_map.element_count("formula") == 4


def test_a_per_library_freeze_binds_whatever_ceiling_binds_says(repo):
    """单库(非全局)运行里某库带着自己的逐库冻结时,``ceiling_binds=False`` 同样
    关不掉它:判词只描述当前库的勾选与漂移,证明不了那份冻结仍等于该库的来源
    (与知识重读判词 ``source_scope.ceiling_binds`` 的同一条分支)。"""
    first, _second = _two_libraries(repo)
    with source_scope_context(
        first, None, None, notebook_source_ceilings={first: ["x1"]},
    ):
        elements = repo.collection_enumeration.enumerate_elements(
            first, "formula", budget=_budget(), ceiling_binds=False)
        roster = repo.collection_enumeration.enumerate_sources(
            first, budget=_budget(), ceiling_binds=False)
        collection_map = repo.collection_catalog.collection_map(
            first, ceiling_binds=False)
    assert {item.source_id for item in elements.items} == {"x1"}
    assert [item.source_id for item in roster.items] == ["x1"]
    assert collection_map.element_count("formula") == 1


def test_an_excluded_library_is_denied_whatever_ceiling_binds_says(repo):
    """库维度排除的库:判词为假时天花板也是「全部拒绝」,不是「没有天花板」。"""
    first, second = _two_libraries(repo)
    with source_scope_context(
        first,
        {"mode": "include", "source_ids": ["x1", "x2"], "narrowed": False},
        {"mode": "include", "notebook_ids": [], "narrowed": True},
    ):
        ceiling = repo.collection_catalog.source_ceiling(
            None, second, ceiling_binds=False)
        own = repo.collection_catalog.source_ceiling(
            None, first, ceiling_binds=False)
    assert ceiling is not None and not ceiling.members
    assert own is None


# ----------------- 读后核验:判词算出之后新增的来源永远在冻结天花板之外(#817 r1)

def _drifted(notebook_id):
    from app.services.source_scope import (
        collection_ceiling_drifted,
        current_source_scope,
    )

    return collection_ceiling_drifted(current_source_scope(), notebook_id)


def test_an_upload_after_the_verdict_is_never_counted_listed_or_complete(repo):
    """codex #817 r1 的复现:全选 run 的首次集合读取把判词记成「不约束」,之后上传
    一份来源(带元素与它自己的知识对象)。之后的每次集合读取都不许数它、列它,
    ``complete`` 与分母都按冻结集合;第一个看见它的读取把漂移记在 run 上。"""
    nb = _library(repo)
    with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
        with _all_ticked(repo, nb):
            before = repo.collection_catalog.collection_map(nb, ceiling_binds=False)
            assert not _drifted(nb)
            _src(repo, nb, "sLate", formulas=4)
            _kg(repo, nb, "oLate", [("sLate", "el-sLate-001")],
                owner_source_id="sLate")
            elements = repo.collection_enumeration.enumerate_elements(
                nb, "formula", budget=_budget(), ceiling_binds=False)
            assert _drifted(nb)
            roster = repo.collection_enumeration.enumerate_sources(
                nb, budget=_budget(), ceiling_binds=False)
            kg = repo.collection_enumeration.enumerate_kg_objects(
                nb, "concept", budget=_budget(), ceiling_binds=False)
            after = repo.collection_catalog.collection_map(nb, ceiling_binds=False)
    assert "sLate" not in {item.source_id for item in elements.items}
    assert elements.coverage.complete is True
    assert elements.coverage.total == before.element_count("formula") == 6
    assert "sLate" not in {item.source_id for item in roster.items}
    assert roster.coverage.complete is True and roster.coverage.total == 3
    assert "oLate" not in {item.object_id for item in kg.items}
    assert kg.coverage.complete is True
    assert after.element_count("formula") == 6 and after.sources == 3


def test_a_kg_read_alone_detects_the_upload(repo):
    """KG 读取手里没有来源行:入口先做一次快路径确认(一次 signal 读),所以没有
    任何元素 / 来源读取在先时,新来源的对象也不会被列出或计入分母。"""
    nb = _library(repo)
    with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
        with _all_ticked(repo, nb):
            _src(repo, nb, "sLate", formulas=1)
            _kg(repo, nb, "oLate", [("sLate", "el-sLate-001")],
                owner_source_id="sLate")
            kg = repo.collection_enumeration.enumerate_kg_objects(
                nb, "concept", budget=_budget(), ceiling_binds=False)
            assert _drifted(nb)
    assert "oLate" not in {item.object_id for item in kg.items}
    assert kg.coverage.complete is True
    assert kg.coverage.total == len(kg.items)


def test_a_kg_row_owned_outside_the_freeze_flips_the_read_to_bound(repo):
    """行级读后核验:来源行里看不见、属主却在冻结天花板之外的对象(这里是属主
    来源已不在来源表里的孤儿对象——与「确认之后才落库的新对象」同形)。快路径
    返回它的那次读取记下漂移、改为绑定重读,清单里没有它。"""
    nb = _library(repo)
    _kg(repo, nb, "oGhost", [("sGhost", "el-sGhost-001")],
        owner_source_id="sGhost")
    with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
        with _all_ticked(repo, nb):
            kg = repo.collection_enumeration.enumerate_kg_objects(
                nb, "concept", budget=_budget(), ceiling_binds=False)
            assert _drifted(nb)
    assert "oGhost" not in {item.object_id for item in kg.items}
    assert sorted(item.object_id for item in kg.items) == ["oA", "oB", "oMix"]


def test_a_49k_ceiling_binds_once_across_pages_and_counts(repo, monkeypatch):
    """codex #817 r1 P2:执行器与目录把 run 的那一个 frozenset(``members``)交给
    store,store 按对象身份缓存绑定形态——4.9 万 id 的天花板在整条清单的每一页和
    每个计数里只序列化一次(传排序后的 tuple 时每次调用都会重新归一、排序、序列化)。"""
    from app.repositories.sqlite import source_ceiling as store_ceiling

    nb = _library(repo)
    bound = []
    original = store_ceiling.bind_ids

    def counting(ids, **kwargs):
        values = list(ids)
        if len(values) > 40_000:
            bound.append(len(values))
        return original(values, **kwargs)

    monkeypatch.setattr(store_ceiling, "bind_ids", counting)
    monkeypatch.setattr(store_ceiling, "_cache", type(store_ceiling._cache)())
    ticks = ["sA", "sC"] + [f"absent-{index:05d}" for index in range(49_000)]
    with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
        with _ticked(nb, ticks):
            counts = dict(repo.collection_catalog.collection_map(nb).kg_objects)
            items, coverage, calls = _walk_all(
                lambda cursor: repo.collection_enumeration.enumerate_kg_objects(
                    nb, "concept", budget=_budget(page_size=1, max_rows=1),
                    cursor=cursor))
    assert counts["concept"] == 2
    assert calls == 2 and coverage.complete is True
    assert sorted(item.object_id for item in items) == ["oA", "oMix"]
    assert bound == [49_002]


def test_the_listing_that_saw_the_upload_is_disclosed_as_source_scoped(
    rrepo, monkeypatch,
):
    """推理一侧:清单进行中上传的来源由那次读取发现,清单改为绑定并如实带上
    「(仅勾选的来源)」(读后读取的漂移,``_read_drifted``)。"""
    notebook = _reasoning_seed(rrepo, formulas=2)
    _hook_first_page(
        monkeypatch, rrepo._runtime.source_store, "element_page_rows",
        lambda: _src(rrepo, notebook.id, "sLate", formulas=3),
    )
    llm = _SeqLLM([
        {"next_action": "enumerate_elements",
         "enumerate": {"kind": "formula"}},
        {"next_action": "answer", "sufficient": True},
    ])
    retriever, limits = _retriever(rrepo, llm)
    with retrieval_run(run_kind="ask_reasoning", actor_id=_ACTOR):
        visible = rrepo._runtime.source_store.all_visible_source_ids(notebook.id)
        with _frozen_all_selected(rrepo, notebook.id, visible):
            result = retriever.run(notebook.id, "有哪些公式", "", limits=limits)
    [outcome] = result.enumerations
    assert "sLate" not in {item.source_id for item in outcome.items}
    assert outcome.coverage.complete is True
    assert outcome.source_scoped is True
    [step] = [s for s in result.trace if s.step_type == "enumerate"]
    assert step.summary.endswith(SELECTED_SOURCES_SCOPE_SUFFIX)
    assert step.detail["source_scoped"] is True


# ------------------------------------------------- 成本:L4 与 scope 无关、零语句

def _wide_library(repo, count=60):
    nb = repo.create_notebook(NotebookCreate(name="wide")).id
    for index in range(count):
        _src(repo, nb, f"w{index:03d}", formulas=1)
    return nb


def test_a_warm_narrowed_map_build_issues_no_per_source_statement(
    repo, monkeypatch,
):
    """中等规模收窄(一半来源勾选)下,热的地图构建不发任何按来源计数语句:计数取自
    已记忆、与 scope 无关的 L4 计划,不重算 L1、也不扫穿它。

    L1 的上限被压到比勾选集合还小——这正是 4.9 万来源的库里 2.45 万勾选时的形状:
    按来源重算的写法在这里每次热构建都要重新发语句(并把别的库挤出 L1),而 L4
    的写法一条都不发。"""
    monkeypatch.setattr(collection_catalog, "_MAX_CACHED_SOURCES", 8)
    nb = _wide_library(repo)
    ticked = [f"w{index:03d}" for index in range(0, 60, 2)]
    store = repo._runtime.source_store
    original = store.element_type_count_rows
    calls: list = []

    def spy(db, source_ids, kinds):
        calls.append(len(list(source_ids)))
        return original(db, source_ids, kinds)

    monkeypatch.setattr(store, "element_type_count_rows", spy)
    with _ticked(nb, ticked):
        cold = repo.collection_catalog.collection_map(nb)
        calls.clear()
        warm = repo.collection_catalog.collection_map(nb)
    assert calls == []
    assert warm.element_count("formula") == cold.element_count("formula") == 30
    assert warm.elements[0].sources == 30


def test_the_element_plan_memo_is_scope_independent(repo):
    """L4 记的是整库计划,天花板在记忆之后才过滤:先在收窄下建一次计划,随后
    不带天花板的计划必须仍是整库的——在记忆之前就过滤会让它只剩勾选的那半。"""
    nb = _wide_library(repo, count=10)
    catalog = repo.collection_catalog
    with _ticked(nb, ["w000", "w001"]):
        with repo._connect() as db:
            narrowed = catalog.scope_element_plan(db, (nb,), "formula")
    with repo._connect() as db:
        whole = catalog.scope_element_plan(db, (nb,), "formula")
    assert narrowed.total == 2
    assert whole.total == 10
    assert len(catalog._plan_sources[(nb, "formula")][1]) == 10


# ------------------------------------------ 天花板对象:run 上记忆、与 scope 同集合

def _ceiling_members(repo, notebook_id, **kwargs):
    with repo._connect() as db:
        ceiling = repo.collection_catalog.source_ceiling(db, notebook_id, **kwargs)
    return None if ceiling is None else {m for m in ceiling.members if m}


def test_source_ceiling_members_equal_scoped_allowed_source_ids(repo):
    """``source_ceiling`` 不再调用(每次都排序的)``scoped_allowed_source_ids``,
    直接用 scope 自己的 frozenset;两者对每种形状必须是同一个集合。"""
    from app.services.source_scope import scoped_allowed_source_ids

    first, second = _two_libraries(repo)
    shapes = [
        lambda: _ticked(first, ["x1"]),
        lambda: source_scope_context(first, {
            "mode": "include", "source_ids": ["x1"], "narrowed": True,
            "hidden_source_ids": ["hidden-1"]}),
        lambda: source_scope_context(
            first, None, None, notebook_source_ceilings={first: ["x2"]}),
        lambda: source_scope_context(
            first, None, None, notebook_source_ceilings={first: []}),
        lambda: source_scope_context(
            first, None, {"mode": "include", "notebook_ids": []}),
    ]
    for shape in shapes:
        with shape():
            for notebook_id in (first, second):
                allowed = scoped_allowed_source_ids(notebook_id)
                members = _ceiling_members(repo, notebook_id)
                assert members == (None if allowed is None else set(allowed))


def test_the_ceiling_lives_on_the_run_not_in_the_process(repo):
    """同一次检索运行里重复取同一库的天花板拿到同一个对象(只建一次);两次运行
    即便 scope 相等也各建各的——进程级缓存会把对象跨运行共享(并把大集合钉在
    内存里)。成员集就是 scope 自己的 frozenset,不复制。"""
    nb = _library(repo)
    scope_dict = {"mode": "include", "source_ids": ["sA", "sC"], "narrowed": True}
    seen = []
    for _ in range(2):
        with retrieval_run(run_kind="ask_chunk", actor_id=_ACTOR):
            with source_scope_context(nb, scope_dict):
                with repo._connect() as db:
                    once = repo.collection_catalog.source_ceiling(db, nb)
                    again = repo.collection_catalog.source_ceiling(db, nb)
                from app.services.source_scope import current_source_scope
                assert once.members is current_source_scope().source_ids
        assert once is again
        seen.append(once)
    assert seen[0] is not seen[1]
    assert not hasattr(repo.collection_catalog, "_ceilings")
