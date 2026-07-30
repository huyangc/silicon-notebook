"""逐步推理接入类型化集合枚举工具(PR-2 T5)。

覆盖响应契约与合成集成的接入面:``AskResponse.result_sets`` 的 kind 判别 union
(新旧混合反序列化)、``enumeration_prompt_block`` 的覆盖措辞 + previewed 回填 +
预算三层夹截断、``typed_collection_results`` 的 outcome→wire 映射,以及端到端
(reflect 产出枚举 → result_sets 含 TypedCollectionResult 且合成 prompt 携带
枚举块 → completeness_unavailable 按确定性规则条件化)。执行器/接入面本身的
合同分别在 ``test_collection_enumeration`` / ``test_reasoning_enumeration_tools``
——这里只测 T5 新增的响应契约与合成拼接。
"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from app.core.ask_retrieval_policy import (
    EXPLICIT_PARTIAL_OVERFLOW,
    ask_retrieval_limits,
)
from app.core.config import Settings
from app.models.ask import (
    AskIntentConfirmation,
    AskResponse,
    QueryIntentContract,
    StructuredKnowhowResult,
    StructuredResultCoverage,
    TypedCollectionCoverage,
    TypedCollectionItem,
    TypedCollectionResult,
)
from app.models.schemas import AskRequest, NotebookCreate
from app.services.collection_enumeration import (
    MAX_EVIDENCE_REFS,
    ElementItem,
    EnumerationCoverage,
    KgObjectItem,
    SourceItem,
)
from app.services.collection_enumeration_answer import (
    COLLECTION_MAP_BLOCK_MAX_CHARS,
    apply_synthesis_preview_counts,
    collection_map_block,
    enumeration_prompt_block,
    enumeration_sub_budget,
    typed_collection_results,
)
from app.services.embedding import FakeEmbedder
from app.services.reasoning_retrieval import CollectionEnumerationOutcome
from app.services.sqlite_repository import SQLiteRepository
from app.services.structured_retrieval import (
    StructuredEnumeration,
    structured_prompt_block,
)
from tests.model_testkit import bind_all_embedding_clients, bind_chat_client

NOW = "2026-07-28T00:00:00+08:00"


# --------------------------------------------------------------------- 构件

def _coverage(**overrides) -> EnumerationCoverage:
    base = dict(
        returned=0, returned_total=0, scanned=0, total=None, has_more=False,
        complete=False, truncated_reason="", overflow_semantics="",
    )
    base.update(overrides)
    return EnumerationCoverage(**base)


def _element_item(**overrides) -> ElementItem:
    base = dict(
        element_id="el-1", source_id="s1", source_title="论文一",
        element_type="formula", location_label="p1", text="公式1",
        asset_id="", notebook_id="nb", tier="personal",
    )
    base.update(overrides)
    return ElementItem(**base)


def _source_item(**overrides) -> SourceItem:
    base = dict(
        source_id="s1", source_title="论文一", doc_type_label="学术论文",
        summary="第一篇的摘要", notebook_id="nb", tier="personal",
    )
    base.update(overrides)
    return SourceItem(**base)


def _kg_item(**overrides) -> KgObjectItem:
    base = dict(
        object_id="k-1", object_type="concept", name="版图设计要点",
        section_path="1", notebook_id="nb", tier="personal",
        evidence_element_ids=(),
    )
    base.update(overrides)
    return KgObjectItem(**base)


# 五档共用的结构化载荷上限,也是 wire 载荷闸的真源;映射测试统一用它,
# 免得每个用例各拍一个数、真上限改了却没人红。
PAYLOAD_LIMIT = ask_retrieval_limits("standard").structured_payload_chars


def _outcome(**overrides) -> CollectionEnumerationOutcome:
    base = dict(collection="elements", kind="formula", source_id="",
                items=[_element_item()], coverage=_coverage())
    base.update(overrides)
    return CollectionEnumerationOutcome(**base)


# ------------------------------------------------------- typed_collection_results

def test_element_outcome_maps_onto_typed_collection_result():
    outcome = _outcome(
        items=[_element_item(element_id="el-1", asset_id="a1")],
        coverage=_coverage(returned=3, returned_total=3, scanned=3, total=3,
                           complete=True),
    )
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    assert result.kind == "collection"
    assert result.collection == "elements"
    assert result.element_kind == "formula"
    assert result.object_type == ""
    assert result.source_id == ""
    assert [item.item_id for item in result.items] == ["el-1"]
    assert result.items[0].asset_id == "a1"
    assert result.items[0].source_title == "论文一"
    assert result.coverage.returned_total == 3
    assert result.coverage.total == 3
    assert result.coverage.complete is True
    # 未经 apply_synthesis_preview_counts 之前:默认值表示"未尝试合成预览"。
    assert result.synthesis_rows == 0
    assert result.synthesis_complete is None


def test_kg_object_outcome_maps_object_type_not_element_kind():
    outcome = _outcome(
        collection="kg_objects", kind="concept", source_id="",
        items=[_kg_item(evidence_element_ids=("el-1", "el-2"))],
        coverage=_coverage(returned_total=1, total=None, complete=True),
    )
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    assert result.collection == "kg_objects"
    assert result.object_type == "concept"
    assert result.element_kind == ""
    assert result.items[0].name == "版图设计要点"
    assert result.items[0].section_path == "1"
    assert list(result.items[0].evidence_element_ids) == ["el-1", "el-2"]
    # total=None 必须原样带出,不能被 Optional[int] 之外的机制悄悄归零。
    assert result.coverage.total is None


def test_sources_outcome_maps_onto_the_element_arm_fields():
    """来源清单复用元素臂的字段(不给 wire union 加第三组近义字段):
    source_title=显示名、location_label=文档类型界面词、text=摘要摘录;
    element_kind/object_type 两个都必须为空——它没有子类型。
    """
    outcome = _outcome(
        collection="sources", kind="", source_id="",
        items=[_source_item()],
        coverage=_coverage(returned=1, returned_total=1, scanned=1, total=1,
                           complete=True),
    )
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    assert result.collection == "sources"
    assert result.element_kind == "" and result.object_type == ""
    item = result.items[0]
    assert item.item_id == "s1" and item.source_id == "s1"
    assert item.source_title == "论文一"
    assert item.location_label == "学术论文"
    assert item.text == "第一篇的摘要"
    # 文档不是元素:element_type 留空,前端按 collection 分派。
    assert item.element_type == ""
    assert item.asset_id == "" and item.name == "" and item.section_path == ""


def test_sources_kind_never_leaks_into_object_type():
    """映射必须**按 collection 逐个判**,不能写成「不是 elements 就当 KG 对象」。

    今天来源清单的 kind 恒为空串,所以那个简写**碰巧**也得出空 object_type ——
    这条测试因此故意喂一个带 kind 的来源清单 outcome(防御形状,run() 不会产出),
    好让「不是 elements 就当 KG」这个变异真的红:那个字段是前端用来选标签表的,
    非空就会让一份文档清单去查知识对象那张表。
    """
    outcome = _outcome(collection="sources", kind="would-be-stamped",
                       items=[_source_item()],
                       coverage=_coverage(returned_total=1, complete=True))
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    assert result.object_type == ""
    assert result.element_kind == ""


def test_source_scoped_outcome_carries_source_id():
    outcome = _outcome(source_id="s1")
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    assert result.source_id == "s1"


def test_evidence_element_ids_truncated_to_max_evidence_refs_even_if_executor_widens():
    outcome = _outcome(
        collection="kg_objects", kind="concept",
        items=[_kg_item(evidence_element_ids=("a", "b", "c", "d", "e"))],
        coverage=_coverage(returned_total=1, complete=True),
    )
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    assert len(result.items[0].evidence_element_ids) == MAX_EVIDENCE_REFS


def test_evidence_element_ids_bounded_to_three_at_the_model_layer():
    with pytest.raises(Exception):
        TypedCollectionItem(item_id="k1", notebook_id="nb", tier="personal",
                            evidence_element_ids=["a", "b", "c", "d"])


def test_max_evidence_refs_parity_between_executor_and_wire_model():
    """两侧各留反向注释指向对方(collection_enumeration.MAX_EVIDENCE_REFS 与
    TypedCollectionItem.evidence_element_ids 的 max_length);这条测试是唯一
    钉住两者不漂移的地方——两侧都不导入对方(models 不许 import services),
    所以只能在这里用字面量交叉断言。"""
    assert MAX_EVIDENCE_REFS == 3
    TypedCollectionItem(item_id="x", evidence_element_ids=["a", "b", "c"])
    with pytest.raises(Exception):
        TypedCollectionItem(
            item_id="x", evidence_element_ids=["a", "b", "c", "d"][:MAX_EVIDENCE_REFS + 1]
        )


# --------------------------------------------------- wire 载荷精确闸

def _wire_json(results) -> str:
    """结果集真正下发/持久化时的那串 JSON。"""
    return json.dumps(
        [item.model_dump() for item in results],
        ensure_ascii=False, separators=(",", ":"),
    )


def test_wire_measure_matches_the_transport_serializer():
    """闸用 ``model_dump_json`` 量,响应用 ``json.dumps`` 序列化——两者必须逐
    字符等长,否则「精确闸」只是换了个近似。含 CJK/希腊字母,钉住不转义。"""
    item = _typed_item_of(_element_item(text="公式 α β γ"))
    assert item.model_dump_json() == json.dumps(
        item.model_dump(), ensure_ascii=False, separators=(",", ":")
    )
    kg = _typed_item_of(_kg_item(name="共模抑制比 CMRR"))
    assert kg.model_dump_json() == json.dumps(
        kg.model_dump(), ensure_ascii=False, separators=(",", ":")
    )


def _typed_item_of(raw):
    from app.services.collection_enumeration_answer import _typed_item
    return _typed_item(raw)


def test_wire_ceiling_bounds_the_serialized_payload_and_degrades_coverage():
    """执行器的池量的是紧凑 dataclass,而 wire 形状带着联合体另一臂的全部默认
    字段——只按前者收,下发的 JSON 就会越轨。这里按 wire 收,并诚实降级。"""
    items = [
        _element_item(element_id=f"el-{index:03d}", text="公" * 40)
        for index in range(20)
    ]
    outcome = _outcome(
        items=items,
        coverage=_coverage(returned=20, returned_total=20, scanned=20, total=20,
                           complete=True),
    )
    limit = 2_000
    results = typed_collection_results([outcome], payload_chars=limit)

    assert len(_wire_json(results)) <= limit
    [result] = results
    assert 0 < len(result.items) < 20
    assert [item.item_id for item in result.items] == [
        f"el-{index:03d}" for index in range(len(result.items))
    ]                                       # 收的是前缀,不是抽样
    assert result.coverage.complete is False
    assert result.coverage.truncated_reason == "payload"
    assert result.coverage.overflow_semantics == EXPLICIT_PARTIAL_OVERFLOW
    assert result.coverage.returned_total == len(result.items)
    assert result.coverage.total == 20      # 分母是集合规模,不跟着裁

    # 变异钉子:改回按执行器 dataclass 记账会多收若干条,而那份 wire 越轨。
    dataclass_chars = sum(
        len(json.dumps(asdict(item), ensure_ascii=False,
                       separators=(",", ":"), default=str))
        for item in items[:len(result.items) + 1]
    )
    assert dataclass_chars < limit          # 紧凑记账还以为放得下
    over = typed_collection_results(
        [_outcome(items=items[:len(result.items) + 1],
                  coverage=_coverage(returned_total=len(result.items) + 1))],
        payload_chars=10**9,
    )
    assert len(_wire_json(over)) > limit     # 实际下发已经越轨


def test_wire_ceiling_is_a_run_pool_and_degrades_only_the_row_it_cut():
    """池是整轮共享的:先来的集合完整交付,被闸停的只有真正装不下的那一份。"""
    first = _outcome(
        kind="formula",
        items=[_element_item(element_id=f"a-{i}", text="公" * 40) for i in range(5)],
        coverage=_coverage(returned=5, returned_total=5, scanned=5, total=5,
                           complete=True),
    )
    second = _outcome(
        kind="table",
        items=[_element_item(element_id=f"b-{i}", text="表" * 40) for i in range(20)],
        coverage=_coverage(returned=20, returned_total=20, scanned=20, total=20,
                           complete=True),
    )
    results = typed_collection_results([first, second], payload_chars=2_500)

    assert len(_wire_json(results)) <= 2_500
    assert len(results[0].items) == 5
    assert results[0].coverage.complete is True
    assert results[0].coverage.truncated_reason == ""
    assert 0 <= len(results[1].items) < 20
    assert results[1].coverage.complete is False
    assert results[1].coverage.truncated_reason == "payload"


def test_delivered_outcomes_make_the_preview_agree_with_the_card():
    """预览必须按结果卡真正拿到的那份渲染:多出来的行 + 仍写着 complete 的
    头部,等于 prompt 和卡片对同一份清单说两套话。"""
    from app.services.collection_enumeration_answer import delivered_outcomes

    items = [
        _element_item(element_id=f"el-{index:03d}", text="公" * 40)
        for index in range(20)
    ]
    outcome = _outcome(
        items=items,
        coverage=_coverage(returned=20, returned_total=20, scanned=20, total=20,
                           complete=True),
    )
    results = typed_collection_results([outcome], payload_chars=2_000)
    [view] = delivered_outcomes([outcome], results)

    delivered = len(results[0].items)
    assert len(view.items) == delivered
    assert view.coverage.complete is False
    assert view.coverage.truncated_reason == "payload"
    assert outcome.items == items and outcome.coverage.complete is True  # 不改原件

    preview = enumeration_prompt_block(
        [view], inline_rows=100, budget_chars=100_000
    )
    assert f"listed {delivered}/20, partial: payload limit" in preview.text
    item_lines = [line for line in preview.text.splitlines() if line.startswith("k5")]
    assert len(item_lines) == delivered


def test_delivered_outcomes_passes_untrimmed_outcomes_straight_through():
    outcome = _outcome(coverage=_coverage(returned_total=1, total=1, complete=True))
    from app.services.collection_enumeration_answer import delivered_outcomes

    results = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    [view] = delivered_outcomes([outcome], results)
    assert view is outcome


# ---------------------------------------------------- apply_synthesis_preview_counts

def test_apply_synthesis_preview_counts_marks_complete_when_shown_equals_listed():
    outcome = _outcome(coverage=_coverage(returned_total=3, total=3, complete=True))
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    apply_synthesis_preview_counts([result], [3])
    assert result.synthesis_rows == 3
    assert result.synthesis_complete is True


def test_apply_synthesis_preview_counts_marks_incomplete_when_shown_is_partial():
    outcome = _outcome(coverage=_coverage(returned_total=300, total=300, complete=True))
    [result] = typed_collection_results([outcome], payload_chars=PAYLOAD_LIMIT)
    apply_synthesis_preview_counts([result], [100])
    assert result.synthesis_rows == 100
    assert result.synthesis_complete is False


# --------------------------------------------------------- enumeration_sub_budget

def test_sub_budget_reserves_knowhow_length_and_its_joiner():
    # chunk_context_chars=1000, knowhow block already 500 chars: available
    # should be 1000 - 500 - 2(joiner) = 498, further halved to 500 by the
    # third layer -> min(498, 500) = 498.
    assert enumeration_sub_budget(
        chunk_context_chars=1000, structured_block_len=500
    ) == 498


def test_sub_budget_omits_joiner_when_no_knowhow_block():
    assert enumeration_sub_budget(
        chunk_context_chars=1000, structured_block_len=0
    ) == 500  # half-cap binds here, not the (absent) knowhow reservation


def test_sub_budget_never_lets_combined_length_exceed_chunk_context_chars():
    """The +2 overflow this guards against: a Knowhow block and an
    enumeration block each sized exactly to their own ceiling must still fit
    together with the "\\n\\n" joiner between them."""
    chunk_context_chars = 1000
    structured_block = "x" * 700
    budget = enumeration_sub_budget(
        chunk_context_chars=chunk_context_chars,
        structured_block_len=len(structured_block),
    )
    enum_block = "y" * budget
    combined = f"{structured_block}\n\n{enum_block}"
    assert len(combined) <= chunk_context_chars


def test_sub_budget_caps_at_half_even_with_no_knowhow_consumption():
    assert enumeration_sub_budget(
        chunk_context_chars=30_000, structured_block_len=0
    ) == 15_000


def test_mixed_knowhow_and_enumeration_block_keeps_the_enum_header_and_stays_bounded():
    """混合场景:同一轮既有 Knowhow 结构化预览又有类型化集合枚举——按 ask_service
    的真实拼接顺序(先建 knowhow 的 structured_block,再用
    enumeration_sub_budget 算枚举子预算,最后拼接)重放,断言枚举块的块头仍然
    保住,且两块合计不超过 chunk_context_chars。"""
    limits = ask_retrieval_limits("standard")
    batch = StructuredEnumeration(
        result_sets=[StructuredKnowhowResult(
            table_id="t1", title="方法表",
            coverage=StructuredResultCoverage(
                total_rows=3, scanned_rows=3, returned_rows=3, complete=True,
            ),
        )],
        known_total_rows=3, scanned_rows=3, returned_rows=3, complete=True,
    )
    structured_block = structured_prompt_block(
        batch, inline_rows=limits.inline_answer_rows,
        cell_excerpt_chars=limits.cell_excerpt_chars,
        budget_chars=limits.chunk_context_chars,
    )
    assert structured_block  # sanity: a modest, realistic knowhow block

    outcome = _outcome(
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    enum_budget = enumeration_sub_budget(
        chunk_context_chars=limits.chunk_context_chars,
        structured_block_len=len(structured_block),
    )
    preview = enumeration_prompt_block(
        [outcome], inline_rows=limits.inline_answer_rows, budget_chars=enum_budget)

    assert "[Enumeration: formula elements" in preview.text
    combined = f"{structured_block}\n\n{preview.text}"
    assert len(combined) <= limits.chunk_context_chars


# ---------------------------------------------------------- collection_map_block

def test_collection_map_block_is_empty_in_empty_out():
    """没接枚举工具 / 地图没建出来时注入零字节,合成上下文逐字回到接入前。"""
    assert collection_map_block("") == ""
    assert collection_map_block("   ") == ""
    assert collection_map_block(None) == ""


def test_collection_map_block_endorses_a_markerless_count():
    from app.services.collection_catalog import (
        CollectionMap, ElementKindCount, render_collection_map,
    )

    text = render_collection_map(CollectionMap(
        notebook_ids=("nb",),
        elements=(ElementKindCount(kind="formula", count=812, sources=40),),
        kg_objects=(("concept", 5),),
        knowhow_tables=0,
        sources=40,
    ))
    block = collection_map_block(text)
    assert text in block
    # 三件必须说的事:服务端算的、可无 [k] 引用、数的是「存在多少」而非「检索到多少」。
    assert "WITHOUT a [k] marker" in block
    assert "EXIST in scope" in block
    assert "computed by the server" in block


def test_collection_map_block_stays_bounded():
    """块的上界是**推导**出来的(固定表头 + 硬上限 600 的地图行),不是拍的数。

    表头变长或地图上限抬高都必须是显式改动:这段字符串搭每一次答案合成的车。
    """
    from app.services.collection_catalog import COLLECTION_MAP_MAX_CHARS

    assert COLLECTION_MAP_BLOCK_MAX_CHARS <= 800
    # 上界是**强制**的,不是「假设上游守规矩」:传一份三倍长的地图进来也裁到位。
    assert len(
        collection_map_block("X" * (COLLECTION_MAP_MAX_CHARS * 3))
    ) == COLLECTION_MAP_BLOCK_MAX_CHARS
    assert len(
        collection_map_block("X" * COLLECTION_MAP_MAX_CHARS)
    ) == COLLECTION_MAP_BLOCK_MAX_CHARS


# ------------------------------------------------------- enumeration_prompt_block

def test_empty_outcomes_returns_empty_preview():
    preview = enumeration_prompt_block([], inline_rows=100, budget_chars=10_000)
    assert preview.text == ""
    assert preview.shown_rows == []


def test_complete_header_reports_previewed_count():
    items = [_element_item(element_id=f"el-{i}") for i in range(3)]
    outcome = _outcome(
        items=items,
        coverage=_coverage(returned=3, returned_total=3, scanned=3, total=3,
                           complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "[Enumeration: formula elements, listed 3/3, complete, previewed 3]" in preview.text
    assert "k5001: [enumerated-source-element] 论文一 · p1: 公式1" in preview.text
    assert preview.shown_rows == [3]
    assert list(preview.evidence_by_id) == ["k5001", "k5002", "k5003"]


def test_sources_preview_renders_a_document_roster():
    """来源清单的预览行 = 标题 · 类型: 摘要,块头用「documents」这个名词。

    标题必须**在最前**:模型接下来要按标题 add_subquery 逐篇深挖,标题就是那个
    句柄。空 kind 不得在块头留下一个前导空格(那读起来像被截断的字段)。
    """
    outcome = _outcome(
        collection="sources", kind="",
        items=[_source_item(), _source_item(
            source_id="s2", source_title="教材二", doc_type_label="教材 / 课本",
            summary="")],
        coverage=_coverage(returned=2, returned_total=2, scanned=2, total=2,
                           complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100,
                                       budget_chars=10_000)
    assert "[Enumeration: documents, listed 2/2, complete, previewed 2]" in preview.text
    assert "[Enumeration:  documents" not in preview.text     # 无前导空格
    # 行形态跟随 #402 的可引用格式:`kN: [tag] …`。tag 与兄弟臂同惯例——元素行是
    # 某个来源的元素、KG 行是某类对象,而这一行**就是**一个来源。
    assert "k5001: [enumerated-source] 论文一 · 学术论文: 第一篇的摘要" in preview.text
    # 没有摘要的文档仍然占一行——「这篇存在」正是这份清单要说的事。
    assert "k5002: [enumerated-source] 教材二 · 教材 / 课本" in preview.text
    assert preview.shown_rows == [2]


def test_sources_preview_row_survives_a_missing_type_and_title():
    outcome = _outcome(
        collection="sources", kind="",
        items=[_source_item(source_title="", doc_type_label="", summary="")],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=10,
                                       budget_chars=10_000)
    # 退到 item_id 而不是渲染一行只有 tag 的空行(此处仍是本次提交的形态;
    # 后续提交把它换成中性占位「未命名来源」)。
    assert "k5001: [enumerated-source] s1" in preview.text


def test_partial_header_reports_budget_reason_and_previewed():
    outcome = _outcome(
        coverage=_coverage(returned=200, returned_total=200, scanned=200,
                           total=900, complete=False, truncated_reason="budget"))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert (
        "[Enumeration: formula elements, listed 200/900, "
        "partial: run budget, previewed 1]"
    ) in preview.text
    assert preview.shown_rows == [1]


def test_payload_reason_gets_its_own_label():
    outcome = _outcome(
        coverage=_coverage(returned=1, returned_total=1, scanned=1, total=50,
                           complete=False, truncated_reason="payload"))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "partial: payload limit" in preview.text


def test_denominator_unknown_never_renders_as_slash_zero():
    outcome = _outcome(
        collection="kg_objects", kind="concept",
        items=[_kg_item()],
        coverage=_coverage(returned=51, returned_total=51, scanned=51,
                           total=None, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "/0" not in preview.text
    assert "/None" not in preview.text


def test_denominator_unknown_complete_still_states_complete():
    """卡片徽章「已全部列出」不能与 prompt 措辞「分母未知」自相矛盾——complete
    必须显式出现,不能被"分母未知"这条分支吞掉(规则 11 的强制披露仍要求模型
    知道这份清单其实已经列全了)。"""
    outcome = _outcome(
        collection="kg_objects", kind="concept",
        items=[_kg_item()],
        coverage=_coverage(returned=51, returned_total=51, scanned=51,
                           total=None, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "listed 51, complete, denominator unknown" in preview.text


def test_denominator_unknown_partial_keeps_the_truncation_reason():
    outcome = _outcome(
        collection="kg_objects", kind="concept",
        items=[_kg_item()],
        coverage=_coverage(returned=10, returned_total=10, scanned=10,
                           total=None, complete=False, truncated_reason="budget"))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "partial: run budget, denominator unknown" in preview.text
    assert "/0" not in preview.text


def test_concurrent_change_is_interrupted_not_generic_partial():
    outcome = _outcome(
        coverage=_coverage(returned=40, returned_total=40, scanned=40,
                           total=None, complete=False,
                           truncated_reason="concurrent_change"))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert (
        "[Enumeration: formula elements, listed 40, "
        "INTERRUPTED by concurrent change — completeness unknown, previewed 1]"
    ) in preview.text
    assert "partial" not in preview.text


def test_source_scoped_outcome_states_single_source_scope():
    outcome = _outcome(
        source_id="s1", items=[_element_item(source_title="论文一")],
        coverage=_coverage(returned=1, returned_total=1, scanned=1, total=1,
                           complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "[scope: single source 论文一]" in preview.text


def test_kg_object_item_line_has_a_real_key_but_no_bogus_numeric_marker():
    outcome = _outcome(
        collection="kg_objects", kind="concept",
        items=[_kg_item(section_path="")],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "k5001: [enumerated-concept] 版图设计要点 · —" in preview.text
    assert preview.evidence_by_id["k5001"]["object_id"] == "k-1"


def test_item_line_never_contains_a_bare_bracketed_number():
    """一个常见的 section_path 就是纯数字("1"、"2.3");条目行如果照旧把它包在
    `[...]` 里,前端引用正则会把 `- [1] name` 渲染成可点的假引用 chip。"""
    outcome = _outcome(
        collection="kg_objects", kind="concept",
        items=[_kg_item(section_path="1")],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "[1]" not in preview.text
    assert "k5001: [enumerated-concept] 版图设计要点 · 1" in preview.text


def test_element_item_line_uses_dot_and_colon_not_brackets():
    outcome = _outcome(items=[_element_item(source_title="论文一", location_label="p3",
                                             text="公式3")])
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    assert "k5001: [enumerated-source-element] 论文一 · p3: 公式3" in preview.text
    assert "[论文一" not in preview.text


def test_multiline_element_text_collapses_onto_one_preview_line():
    outcome = _outcome(items=[_element_item(
        text="第一行\n第二行\n\n第三行   有多余空白")])
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    item_lines = [line for line in preview.text.splitlines() if line.startswith("k5")]
    assert len(item_lines) == 1
    assert "第一行 第二行 第三行 有多余空白" in item_lines[0]


def test_element_text_clamped_to_prompt_line_excerpt_chars():
    long_text = "字" * 500
    outcome = _outcome(items=[_element_item(text=long_text)])
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=100_000)
    item_line = next(l for l in preview.text.splitlines() if l.startswith("k5"))
    # 200 是 prompt 行摘录常量;传输/结果卡仍由执行器的 1000 字符摘录负责,
    # 这里只钉 prompt 行本身足够短。
    assert len(item_line) < 260


def test_inline_rows_cap_is_shared_across_outcomes_and_reports_omitted_count():
    many_items = [_element_item(element_id=f"el-{i}") for i in range(5)]
    outcome = _outcome(
        items=many_items,
        coverage=_coverage(returned=5, returned_total=5, scanned=5, total=5,
                           complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=2, budget_chars=10_000)
    item_lines = [line for line in preview.text.splitlines() if line.startswith("k5")]
    assert len(item_lines) == 2       # capped at inline_rows, not the full 5
    assert "(+3 more rows in the result card)" in preview.text
    assert preview.shown_rows == [2]


def _sized_outcome(kind, prefix, count):
    return _outcome(
        kind=kind,
        items=[_element_item(element_id=f"{prefix}-{i}") for i in range(count)],
        coverage=_coverage(returned=count, returned_total=count, scanned=count,
                           total=count, complete=True),
    )


def test_inline_rows_are_split_so_a_big_list_cannot_starve_a_later_one():
    """先到先得会让第一份清单吃光共享额度,后面的集合 previewed 全是 0——混合
    问题于是只按第一张卡作答。两遍分配:先保底,再把余量按序贪心分完。"""
    preview = enumeration_prompt_block(
        [_sized_outcome("formula", "f", 120), _sized_outcome("table", "t", 30)],
        inline_rows=100, budget_chars=100_000,
    )
    assert preview.shown_rows == [70, 30]
    assert sum(preview.shown_rows) == 100
    assert all(shown > 0 for shown in preview.shown_rows)


def test_single_outcome_still_takes_the_whole_allowance():
    """单清单口径逐字不变:保底=全额,余量无处可分。"""
    preview = enumeration_prompt_block(
        [_sized_outcome("formula", "f", 300)],
        inline_rows=100, budget_chars=100_000,
    )
    assert preview.shown_rows == [100]


def test_small_first_list_donates_its_unused_quota_to_the_next():
    """保底不是硬分配:装不满的那份把余量让出去,不浪费在一个只有 3 条的集合上。"""
    preview = enumeration_prompt_block(
        [_sized_outcome("formula", "f", 3), _sized_outcome("table", "t", 300)],
        inline_rows=100, budget_chars=100_000,
    )
    assert preview.shown_rows == [3, 97]


def test_more_outcomes_than_rows_degrades_without_over_committing():
    """额度比集合还少:按序发到发完为止,总数绝不超过 inline_rows。"""
    outcomes = [_sized_outcome("formula", f"o{i}", 5) for i in range(5)]
    preview = enumeration_prompt_block(
        outcomes, inline_rows=2, budget_chars=100_000
    )
    assert sum(preview.shown_rows) == 2
    assert preview.shown_rows == [1, 1, 0, 0, 0]


def test_budget_chars_never_exceeded_even_with_many_rows():
    many_items = [_element_item(element_id=f"el-{i}") for i in range(50)]
    outcome = _outcome(
        items=many_items,
        coverage=_coverage(returned=50, returned_total=50, scanned=50, total=50,
                           complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=80)
    assert len(preview.text) <= 80


def test_header_survives_when_budget_only_fits_the_header():
    """预算再小也先放块头:够放块头(previewed 0)但放不下任何条目时,块头本身
    仍必须出现,而不是把整块一起省略。"""
    outcome = _outcome(
        items=[_element_item(text="很长很长很长很长很长很长很长很长的一段话" * 5)],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    # 用 shown=0 时的块头长度(不含条目行)作为探针,而不是按行号切——指令行与
    # 块头之间那道 "\n\n" 分隔本身会在 splitlines() 里多出一个空行,按下标取
    # 极易错位(已实测踩过一次)。直接用零行版本重新构造整段前缀,量出精确长度。
    zero_row_preview = enumeration_prompt_block(
        [outcome], inline_rows=0, budget_chars=10_000
    )
    assert "previewed 0" in zero_row_preview.text
    bare_prefix_len = len(zero_row_preview.text)
    preview = enumeration_prompt_block(
        [outcome], inline_rows=100, budget_chars=bare_prefix_len + 10
    )
    assert "previewed 0" in preview.text
    assert preview.shown_rows == [0]


def test_whole_block_empty_when_budget_cannot_even_fit_one_header():
    outcome = _outcome(
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=1)
    assert preview.text == ""
    assert preview.shown_rows == [0]


def test_multiple_outcomes_each_get_their_own_header_in_order():
    formula_outcome = _outcome(kind="formula")
    kg_outcome = _outcome(
        collection="kg_objects", kind="concept", items=[_kg_item()],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    preview = enumeration_prompt_block(
        [formula_outcome, kg_outcome], inline_rows=100, budget_chars=10_000)
    formula_idx = preview.text.index("[Enumeration: formula elements")
    kg_idx = preview.text.index("[Enumeration: concept objects")
    assert formula_idx < kg_idx
    assert preview.shown_rows == [1, 1]


def test_instruction_line_present_and_explains_previewed_vs_listed():
    outcome = _outcome(
        coverage=_coverage(returned=3, returned_total=3, total=3, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=100, budget_chars=10_000)
    # Content unique to the ONE-TIME instruction sentence, not to any
    # per-outcome coverage header (both mention "previewed"/"listed", so
    # checking for those words alone would pass even without this line —
    # "result card" only appears in the instruction sentence).
    first_line = preview.text.splitlines()[0]
    assert "result card" in first_line
    assert "previewed" in first_line and "listed" in first_line
    # And it appears exactly once, ahead of every outcome header.
    assert preview.text.count("result card, not this preview") == 1
    header_idx = preview.text.index("[Enumeration: formula elements")
    instruction_idx = preview.text.index("result card, not this preview")
    assert instruction_idx < header_idx


def test_document_roster_carries_adaptive_granularity_guidance():
    """自适应粒度指导(design doc §6.2):目录在场时才出现,且教的是「少则逐篇、
    多则按主题归纳」,不给任何数值阈值。

    它落在这里而不是 answer_prompt:只有文档目录的正确答案形状随规模变化,而
    answer_prompt 那一条会让产品里每一次合成都付这段字符。
    """
    outcome = _outcome(
        collection="sources", kind="",
        items=[_source_item()],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    preview = enumeration_prompt_block([outcome], inline_rows=10,
                                       budget_chars=10_000)

    assert "Document roster guidance" in preview.text
    assert "document by document" in preview.text
    assert "organized by THEME" in preview.text
    # 判断交给模型:任何写死的「超过 N 篇就归纳」都是换了名字的词法路由。
    for numeral in ("more than 1", "more than 5", "more than 10", "more than 20"):
        assert numeral not in preview.text
    # 排在目录块之前(它讲的是紧跟其后的那一块)。
    assert preview.text.index("Document roster guidance") < preview.text.index(
        "[Enumeration: documents")


def test_granularity_guidance_is_absent_without_a_document_roster():
    """没有目录就不出现:元素/知识对象清单的答案形状不随规模变化,这段字符对
    它们是纯开销。"""
    elements = _outcome(
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    objects = _outcome(
        collection="kg_objects", kind="concept",
        items=[_kg_item()],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))

    for outcome in (elements, objects):
        preview = enumeration_prompt_block([outcome], inline_rows=10,
                                           budget_chars=10_000)
        assert "Document roster guidance" not in preview.text, outcome.collection


def test_granularity_guidance_never_costs_the_roster_its_rows():
    """预算紧张时它自己被丢掉,而不是挤掉目录本身——没有目录的粒度提示毫无用处,
    反过来则不然。"""
    outcome = _outcome(
        collection="sources", kind="",
        items=[_source_item()],
        coverage=_coverage(returned=1, returned_total=1, total=1, complete=True))
    # 只够块头 + 一行,放不下两段固定说明。
    preview = enumeration_prompt_block([outcome], inline_rows=10,
                                       budget_chars=140)

    assert "Document roster guidance" not in preview.text
    assert "[Enumeration: documents" in preview.text
    assert len(preview.text) <= 140


# --------------------------------------------------------- result_sets union

def _knowhow_result(**overrides) -> dict:
    base = dict(
        kind="knowhow", table_id="t1", title="T", columns=[], rows=[],
        coverage=dict(total_rows=5, scanned_rows=5, returned_rows=5,
                      complete=True, truncated_reason="", overflow_semantics=""),
    )
    base.update(overrides)
    return base


def _collection_result(**overrides) -> dict:
    base = dict(
        kind="collection", collection="elements", element_kind="formula",
        object_type="", source_id="", items=[],
        coverage=dict(returned_total=3, total=3, complete=True,
                      truncated_reason="", overflow_semantics=""),
    )
    base.update(overrides)
    return base


def test_pure_knowhow_result_sets_round_trip():
    resp = AskResponse(conclusion="x", result_sets=[_knowhow_result()])
    assert isinstance(resp.result_sets[0], StructuredKnowhowResult)
    blob = json.loads(resp.model_dump_json())
    restored = AskResponse(**blob)
    assert isinstance(restored.result_sets[0], StructuredKnowhowResult)
    assert restored.result_sets[0].table_id == "t1"


def test_mixed_knowhow_and_collection_result_sets_round_trip():
    resp = AskResponse(
        conclusion="x",
        result_sets=[_knowhow_result(), _collection_result()],
    )
    assert [row.kind for row in resp.result_sets] == ["knowhow", "collection"]
    blob = json.loads(resp.model_dump_json())
    restored = AskResponse(**blob)
    assert isinstance(restored.result_sets[0], StructuredKnowhowResult)
    assert isinstance(restored.result_sets[1], TypedCollectionResult)
    assert restored.result_sets[1].collection == "elements"
    assert restored.result_sets[1].coverage.total == 3


def test_legacy_result_set_dict_missing_kind_defaults_to_knowhow():
    """旧持久化数据的防御性后备:``kind`` 字段自 result_sets 引入之日起就有默认值
    (#371),所以真实缺口未被观察到过,但一条手搓/历史 payload 若真的缺了这把
    key,仍必须落回 knowhow(union 存在之前唯一的形状),而不是判别失败。"""
    legacy = _knowhow_result()
    del legacy["kind"]
    resp = AskResponse(conclusion="x", result_sets=[legacy])
    assert isinstance(resp.result_sets[0], StructuredKnowhowResult)
    assert resp.result_sets[0].table_id == "t1"


def test_legacy_mixed_with_explicit_collection_kind_still_routes_correctly():
    legacy = _knowhow_result()
    del legacy["kind"]
    resp = AskResponse(
        conclusion="x", result_sets=[legacy, _collection_result()],
    )
    assert isinstance(resp.result_sets[0], StructuredKnowhowResult)
    assert isinstance(resp.result_sets[1], TypedCollectionResult)


def test_unknown_kind_fails_loudly_instead_of_silently_defaulting():
    """before-validator 只兜"缺失/假值"的 kind——一个显式但未知的 kind 值必须
    fail-loud(discriminator 查表失败报 ValidationError),不能被悄悄当成
    knowhow 处理:那会把一份陌生形状的数据错误地解析成物理行批次。这份 payload
    刻意带全 StructuredKnowhowResult 的必填字段(table_id/title/coverage)——
    如果 before-validator 曾经把未知 kind 悄悄改写成 "knowhow"(而不只是兜
    缺失值),这份数据反而会构造成功,测试就该抓住"构造成功"这个假阴性,而
    不只是抓"抛没抛异常"(缺字段本身就会抛,那不能证明 kind 校验起了作用)。
    """
    with pytest.raises(Exception):
        AskResponse(conclusion="x", result_sets=[{
            "kind": "mystery", "table_id": "t1", "title": "T",
            "coverage": {"total_rows": 0, "scanned_rows": 0, "returned_rows": 0,
                         "complete": True, "truncated_reason": "",
                         "overflow_semantics": ""},
        }])


def test_typed_collection_coverage_is_not_reused_from_structured_result_coverage():
    # 独立模型的存在性断言:total=None 是 TypedCollectionCoverage 的合法态,
    # StructuredResultCoverage 的 total_rows 却默认 0——混用会让"分母未知"
    # 与"总行数为 0"无法区分。
    coverage = TypedCollectionCoverage(returned_total=5, total=None, complete=True)
    assert coverage.total is None
    with pytest.raises(Exception):
        StructuredResultCoverage(total_rows=None)  # type: ignore[arg-type]


# --------------------------------------------------------- 端到端(ask_reasoning)

class _SeqLLM:
    """plan 固定;reflect 按序列返回;evidence_refine 返回空;answer 固定并录制
    发给它的 prompt 正文(供断言枚举块的存在与相对位置)。"""

    configured = True

    def __init__(self, plan, reflects, answer):
        self._plan = plan
        self._reflects = list(reflects)
        self._answer = answer
        self.answer_prompts: list[str] = []

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            return json.dumps(self._plan)
        if "next_action" in schema_hint:
            return json.dumps(
                self._reflects.pop(0) if self._reflects
                else {"next_action": "answer", "sufficient": True}
            )
        if '"relevant"' in schema_hint:
            return json.dumps({"relevant": []})
        self.answer_prompts.append(content)
        return json.dumps(self._answer)


@pytest.fixture
def arepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 与 test_reasoning_enumeration_tools 的 repo 同款隔离:本机 .env 里的真实
    # 推理端点会让这些 run 打真实网络。
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    instance = SQLiteRepository(Settings())
    bind_all_embedding_clients(instance, FakeEmbedder(dim=16))
    instance.settings.graph_ppr_enabled = False
    return instance


def _bind_reasoning(repo, client):
    bind_chat_client(repo, "reasoning_agent", client)
    bind_chat_client(repo, "evidence_refine", client)
    bind_chat_client(repo, "ask_answer", client)


def _seed(repo, *, formulas=3, with_kg=True):
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    if with_kg:
        repo.store_kg(notebook.id, None, [
            {"local_id": "C1", "object_type": "claim",
             "payload": {"name": "版图设计要点", "section_path": "1"}, "evidence": []},
        ], [])
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("s1", notebook.id, "论文一", "pdf", "extracted", "extracted", NOW, NOW),
        )
        for index in range(1, formulas + 1):
            db.execute(
                "INSERT INTO source_elements (id,source_id,element_type,"
                "location_label,text,metadata,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"el-{index:03d}", "s1", "formula", f"p{index}",
                 f"公式 {index}", "{}", NOW),
            )
    repo.collection_catalog.invalidate()
    return notebook


def _enumerate_action(kind="formula", **extra):
    request = {"kind": kind}
    request.update(extra)
    return {"next_action": "enumerate_elements", "enumerate": request,
            "reason": "列出公式"}


def test_enumerate_action_populates_result_sets_and_reaches_synthesis(arepo):
    nb = _seed(arepo, formulas=3)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "公式清单如下 [k5001].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    collection_rows = [row for row in resp.result_sets if row.kind == "collection"]
    assert len(collection_rows) == 1
    row = collection_rows[0]
    assert row.collection == "elements" and row.element_kind == "formula"
    assert row.coverage.complete is True
    assert row.coverage.returned_total == 3 and row.coverage.total == 3
    assert len(row.items) == 3
    # previewed 全部 3 条(未截断)→ synthesis 也认为完整。
    assert row.synthesis_rows == 3
    assert row.synthesis_complete is True
    assert all(item.citation for item in row.items)
    assert row.items[0].citation.source_id == "s1"
    assert row.items[0].citation.element_id == "el-001"

    assert llm.answer_prompts, "answer synthesis must have run"
    prompt = llm.answer_prompts[-1]
    assert "[Enumeration: formula elements, listed 3/3, complete, previewed 3]" in prompt
    # 装配位在 source 分区最前:枚举块必须先于本轮唯一的 KG 证据(claim 名称)
    # 出现在喂给合成模型的证据块里,而不是被塞进 KG 分区之后。
    enum_idx = prompt.index("[Enumeration: formula elements")
    kg_idx = prompt.index("版图设计要点")
    assert enum_idx < kg_idx


def test_per_document_analysis_gets_a_roster_then_deepens_by_title(arepo):
    """PR-2.5 的目标形态:「当前notebook的文章分析」→ 先枚举来源拿目录,再按
    标题逐篇深挖。

    钉三件事:(a) 来源清单进 result_sets(卡片形态正确、覆盖率完整);
    (b) 合成 prompt 里真的带着那份目录(标题 + 类型 + 摘要),否则「逐篇」只是
    trace 上的说法;(c) 按标题发出的子查询照常执行(既有 add_subquery 机制,
    零新代码),即先有目录、后有深挖。
    """
    nb = _seed(arepo, formulas=1)
    with arepo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,doc_type,summary,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("s2", nb.id, "论文二", "pdf", "extracted", "extracted",
             "academic_paper", "第二篇讲版图匹配", NOW, NOW),
        )
        db.execute(
            "UPDATE sources SET doc_type=?, summary=? WHERE id=?",
            ("academic_paper", "第一篇讲失配", "s1"),
        )
    arepo.collection_catalog.invalidate()
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "文章分析"}]},
        reflects=[
            # 来源清单是 enumerate 动作的参数值,不是第 11 个动作(§6.2 用户拍板)。
            {"next_action": "enumerate_elements",
             "enumerate": {"collection": "sources"}, "reason": "先拿目录"},
            {"next_action": "add_subquery",
             "new_sub_query": {"query": "论文二 版图匹配", "reason": "逐篇深挖"}},
            {"next_action": "answer", "sufficient": True},
        ],
        answer={"answer": "逐篇分析如下。", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(
        nb.id, AskRequest(question="当前notebook的文章分析", mode="reasoning")
    )

    rows = [row for row in resp.result_sets if row.kind == "collection"]
    assert len(rows) == 1
    row = rows[0]
    assert row.collection == "sources"
    assert row.element_kind == "" and row.object_type == ""
    assert [item.source_title for item in row.items] == ["论文一", "论文二"]
    assert [item.location_label for item in row.items] == ["学术论文", "学术论文"]
    assert [item.text for item in row.items] == ["第一篇讲失配", "第二篇讲版图匹配"]
    assert row.coverage.complete is True and row.coverage.returned_total == 2

    prompt = llm.answer_prompts[-1]
    assert "[Enumeration: documents, listed 2/2, complete, previewed 2]" in prompt
    assert "[enumerated-source] 论文一 · 学术论文: 第一篇讲失配" in prompt
    assert "[enumerated-source] 论文二 · 学术论文: 第二篇讲版图匹配" in prompt
    # 每行都拿到 k5001+ 命名空间里的引用键(#402):目录行因此是可被答案引用的证据,
    # 而不是一段无法归因的散文。
    assert "k5001: [enumerated-source] 论文一" in prompt
    # 目录先于按标题的深挖:轨迹顺序就是「枚举 → 补充子查询」。
    kinds = [step.step_type for step in (resp.reasoning_trace or [])]
    assert kinds.index("enumerate") < kinds.index("retrieve", kinds.index("enumerate"))


def test_prompt_block_failure_clears_the_result_cards_too_and_does_not_crash_ask(
    arepo, monkeypatch
):
    """映射与 enumeration_prompt_block 必须共用同一个 try:块渲染炸了,不能只
    清空块、留下一份看起来完整却没有 synthesis 支持的结果卡(此前的分离 try
    会让这一半状态发生,评审实测复现过"块裸抛穿整轮 Ask")。这里让
    enumeration_prompt_block 本身抛错,断言(a)Ask 请求整体仍然成功返回、
    (b)result_sets 里完全没有 collection 行——两者必须同时发生,不能只满足
    其中一个。"""
    import app.services.collection_enumeration_answer as cea

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(cea, "enumeration_prompt_block", _boom)
    nb = _seed(arepo, formulas=3)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "答案 [k1].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    assert resp.answer_id  # Ask 整体没有因为块渲染异常而崩溃
    assert not any(row.kind == "collection" for row in resp.result_sets)


def test_enumerate_only_run_with_no_other_evidence_still_reaches_synthesis(arepo):
    """无 top_hits/chunks/elements/memory 时,仅有枚举结果也必须真的触发合成
    (``or enumerations`` 那条守卫),不能落回"该 notebook 尚无匹配知识"的
    确定性兜底文案。``has_kg`` 门要求笔记本里存在 KG(否则整轮在
    ReasoningRetriever 跑之前就早退,连枚举动作都不会发生),但只要有一个 KG
    对象存在,FakeEmbedder 的余弦相似度就从不真正为零——plan 子查询显式限定
    ``types=["formula"]``(笔记本里只有一个 "claim")才能让检索**按类型硬性
    过滤**到真正的零命中,而不是"不相关但仍打分"。"""
    nb = _seed(arepo, formulas=2, with_kg=True)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点", "types": ["formula"]}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "仅根据清单作答 [k5001].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    assert not resp.related_knowledge  # top_hits 确实是空的,不是巧合
    assert llm.answer_prompts, "synthesis must actually run"
    assert resp.answer == "仅根据清单作答 [k5001]."
    assert resp.llm_mode != "deterministic"
    assert any(row.kind == "collection" for row in resp.result_sets)
    assert [anchor.key for anchor in resp.anchors] == ["k5001"]
    assert resp.anchors[0].source_id == "s1"
    assert resp.anchors[0].element_id == "el-001"
    assert resp.evidence_level == "grounded"


def test_enumeration_answer_without_bound_row_does_not_show_unrelated_fallback_citations(
    arepo,
):
    nb = _seed(arepo, formulas=2, with_kg=True)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点", "types": ["formula"]}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "清单里有两项。", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    assert resp.anchors == []
    assert resp.citations == []
    assert resp.evidence_level == "inferred"


# ------------------------------------------------- 无图笔记本也够得到枚举工具

def test_notebook_without_a_kg_still_reaches_the_enumeration_tools(arepo):
    """codex 第 1 轮 P1-1:自动 KG 抽取默认关,「解析了来源但没建图」是常态。

    那条「本笔记本尚未构建知识图谱」的早退跑在 ReasoningRetriever 之前,会把
    这类库整个挡在枚举工具外面——而公式/表格/图片/代码块清单恰恰不需要图。
    放行后必须:枚举动作真的执行、产出 result_sets,而 kg_required 仍然如实
    为 True(前端的建图提示不能因此消失)。
    """
    nb = _seed(arepo, formulas=3, with_kg=False)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "已列出公式 [k5001].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    collection_rows = [row for row in resp.result_sets if row.kind == "collection"]
    assert collection_rows, resp.result_sets
    assert collection_rows[0].coverage.returned_total == 3
    assert collection_rows[0].coverage.complete is True
    assert llm.answer_prompts, "放行后必须真的跑到合成,而不是另一种早退"
    assert resp.llm_mode != "deterministic"
    # 旗标语义不变:无图且无可用参考库仍是 True。
    assert resp.kg_required is True
    assert "本笔记本尚未构建知识图谱" not in resp.conclusion


def test_notebook_without_a_kg_and_without_collections_keeps_the_early_return(arepo):
    """反向:既没有图、作用域里也没有任何可列出的集合时,早退原样保留——
    进循环只会让每个工具都返回空,那句明确的「请先构建知识图谱」才是对的。"""
    nb = _seed(arepo, formulas=0, with_kg=False)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "不该跑到这里。", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    assert resp.kg_required is True
    assert resp.llm_mode == "deterministic"
    assert "本笔记本尚未构建知识图谱" in resp.conclusion
    assert not llm.answer_prompts


def test_kill_switch_restores_the_no_kg_early_return(arepo):
    """接线判据与 run 内的总闸必须是同一个:kill switch 关掉枚举工具后,放行
    条件也必须随之失效,否则会放一整轮什么工具都没有的空循环进来。"""
    arepo.settings.reasoning_enum_tools_enabled = False
    nb = _seed(arepo, formulas=3, with_kg=False)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "不该跑到这里。", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    assert resp.llm_mode == "deterministic"
    assert "本笔记本尚未构建知识图谱" in resp.conclusion


def _completeness_required_payload(
    question: str, *, result_scope: str = "complete", **contract_fields
) -> AskRequest:
    contract = QueryIntentContract(
        objective=question, resolved_question=question,
        completeness_required=True, result_scope=result_scope,
        **contract_fields,
    )
    return AskRequest(
        question=question, mode="reasoning",
        intent=AskIntentConfirmation(contract=contract, resolved_question=question),
    )


def test_completeness_unavailable_suppressed_for_nonaggregate_nonempty_card(arepo):
    """规则①非 aggregate + 非空清单卡 → 抑制。notebook 无 knowhow 表 →
    completeness_required 直接落 completeness_unavailable=True
    (is_knowhow_enumeration_query 对空表恒 False)。本轮 reflect 确实枚举出了
    非空元素清单,免责声明必须让位给 coverage 徽章。"""
    nb = _seed(arepo, formulas=2)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "已列出公式 [k1].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(
        nb.id, _completeness_required_payload("库里有哪些公式", result_scope="complete")
    )

    assert any(row.kind == "collection" for row in resp.result_sets)
    assert "当前精确完整枚举支持" not in resp.conclusion
    assert "当前精确完整枚举支持" not in resp.answer


def test_completeness_unavailable_kept_for_aggregate_scope_even_with_a_card(arepo):
    """规则②aggregate(去重/种类计数,如"库里有多少种公式")永不抑制——枚举
    工具只能精确统计物理条目数,证明不了模型自己归并去重后的种类数,红线要求
    这类问题必须回退到相关性检索并保留警告,哪怕模型顺手枚举出了一张非空卡。"""
    nb = _seed(arepo, formulas=2)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "共两种公式 [k1].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(
        nb.id, _completeness_required_payload("库里有多少种公式", result_scope="aggregate")
    )

    assert any(row.kind == "collection" for row in resp.result_sets)
    assert "当前精确完整枚举支持" in resp.conclusion


def test_completeness_unavailable_kept_when_card_is_empty(arepo):
    """规则③0 条清单不能替这道题背书——枚举了一个空集合(returned_total==0)
    时,警告仍必须保留,不能被"有卡就抑制"误伤。"""
    nb = _seed(arepo, formulas=0)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(kind="table"),
                  {"next_action": "answer", "sufficient": True}],
        answer={"answer": "没有找到表格。", "grounded": False},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(
        nb.id, _completeness_required_payload("库里有哪些表格", result_scope="complete")
    )

    collection_rows = [row for row in resp.result_sets if row.kind == "collection"]
    # 空集合仍然算一次枚举(0 条也是诚实的清单结果),只是不足以背书完整性。
    assert collection_rows and collection_rows[0].coverage.returned_total == 0
    assert "当前精确完整枚举支持" in resp.conclusion


@pytest.mark.parametrize("field", ["constraints", "excluded_topics", "assumptions"])
def test_completeness_unavailable_kept_when_the_request_carries_a_predicate(
    arepo, field
):
    """规则④意图合同带谓词(约束/排除项/前提)时永不抑制。

    codex 第 1 轮 P1-2:条件筛选/分组/去重类的完整请求,只要模型顺手枚举出
    **任何**一张非空卡就消音,而那张卡的 coverage 只证明「某个物理集合被完整
    走了一遍」,证明不了它就是用户要的那个子集。方向定为宁可多警告。
    """
    nb = _seed(arepo, formulas=2)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "已列出公式 [k1].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, _completeness_required_payload(
        "库里 2023 年之后的公式有哪些", **{field: ["2023 年之后"]}
    ))

    collection_rows = [row for row in resp.result_sets if row.kind == "collection"]
    assert collection_rows and collection_rows[0].coverage.complete is True
    assert "当前精确完整枚举支持" in resp.conclusion


def test_completeness_unavailable_kept_when_every_card_is_partial(arepo):
    """规则⑤部分清单不背书完整性:卡自己的 partial 徽章只说明「这张卡没列完」,
    承担不了「你要的那种请求本产品还不支持完整枚举」这句披露。

    overview 档的每轮条目上限是 100 条;101 条公式必然截断成 partial。"""
    nb = _seed(arepo, formulas=101)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "已列出部分公式 [k1].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    payload = _completeness_required_payload("库里有哪些公式")
    payload.retrieval_effort = "overview"
    resp = arepo.ask(nb.id, payload)

    collection_rows = [row for row in resp.result_sets if row.kind == "collection"]
    assert collection_rows, resp.result_sets
    assert collection_rows[0].coverage.returned_total == 100
    assert collection_rows[0].coverage.complete is False
    assert "当前精确完整枚举支持" in resp.conclusion


def test_completeness_unavailable_shown_without_enumeration(arepo):
    """同样的 completeness_required=True 笔记本,但本轮 reflect 从未调用枚举
    动作(直接 answer)——没有清单结果卡背书,免责声明必须出现,且措辞提到
    新增的元素/知识对象清单能力(不再说成「仅支持 Knowhow」)。"""
    nb = _seed(arepo, formulas=2)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[{"next_action": "answer", "sufficient": True}],
        answer={"answer": "简单回答 [k1].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, _completeness_required_payload("库里有哪些公式"))

    assert not any(row.kind == "collection" for row in resp.result_sets)
    assert "当前精确完整枚举支持 Knowhow 整表物理行清单与直接行计数" in resp.conclusion
    assert "元素清单" in resp.conclusion and "知识对象清单" in resp.conclusion


def test_enumeration_block_shares_chunk_budget_with_knowhow_and_stays_bounded(arepo):
    """枚举块子预算三层夹的端到端确认:即便本轮同时产生了 knowhow 结构化预览,
    合成 prompt 里枚举块仍至少保住块头,且不会把 chunk_context_chars 撑爆。"""
    nb = _seed(arepo, formulas=5)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "已列出 [k1].", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    assert llm.answer_prompts
    prompt = llm.answer_prompts[-1]
    assert "[Enumeration: formula elements" in prompt
    limits = ask_retrieval_limits("standard")
    # 粗略上界:枚举块本身不可能超过它自己的子预算上限(chunk_context_chars 的
    # 一半),真正的总量守卫在下面的单元测试(test_budget_chars_never_exceeded)
    # 已经钉过,这里只确认端到端接线没有把它撑爆到荒谬的量级。
    assert len(prompt) < limits.chunk_context_chars * 4


# ------------------------------------------- wire 载荷闸的端到端(卡片 ⇄ prompt)

def test_wire_payload_ceiling_bites_end_to_end_and_prompt_agrees_with_the_card(
    arepo, monkeypatch
):
    """执行器按紧凑 dataclass 记账判 complete,而真正下发的 wire 形状更宽——
    此时结果卡必须降级为 payload 部分,合成 prompt 的块头必须跟着降级,两者
    报同一个数。把 structured_payload_chars 压到一个「dataclass 放得下、wire
    放不下」的窄值来复现;窄值同时喂给执行器,所以这里不是人为只掐一侧。"""
    from dataclasses import replace as _replace
    import app.services.ask_service as ask_service_module

    real = ask_retrieval_limits("standard")
    narrow = _replace(real, structured_payload_chars=1_000)
    monkeypatch.setattr(
        ask_service_module, "ask_retrieval_limits", lambda _effort: narrow
    )

    nb = _seed(arepo, formulas=4)
    llm = _SeqLLM(
        plan={"sub_queries": [{"query": "版图设计要点"}]},
        reflects=[_enumerate_action(), {"next_action": "answer", "sufficient": True}],
        answer={"answer": "部分清单。", "grounded": True},
    )
    _bind_reasoning(arepo, llm)

    resp = arepo.ask(nb.id, AskRequest(question="库里有哪些公式", mode="reasoning"))

    [enum_step] = [s for s in resp.reasoning_trace if s.step_type == "enumerate"]
    # 前置条件(而非结论):执行器自己认为列全了——被闸停的确实是 wire 这一侧。
    assert enum_step.detail["complete"] is True
    assert enum_step.detail["returned_total"] == 4

    [row] = [item for item in resp.result_sets if item.kind == "collection"]
    delivered = len(row.items)
    assert 0 < delivered < 4
    assert row.coverage.complete is False
    assert row.coverage.truncated_reason == "payload"
    assert row.coverage.overflow_semantics == EXPLICIT_PARTIAL_OVERFLOW
    assert row.coverage.returned_total == delivered
    assert len(_wire_json([row])) <= narrow.structured_payload_chars

    prompt = llm.answer_prompts[-1]
    assert f"listed {delivered}/4, partial: payload limit" in prompt
    assert "listed 4/4, complete" not in prompt
    # prompt 里的清单行数不得多于卡片真正携带的条数。
    item_lines = [
        line for line in prompt.splitlines()
        if line.startswith("k5") and "论文一 · " in line
    ]
    assert len(item_lines) == delivered
