"""reasoning 原文段引用卡复用装配期的三份批量读取。

``_draft_reasoning_response`` 的原文段腿(``_reasoning_chunk_cards``)给进了合成
prompt 的那几段发卡。卡片要的 tier / 标题+原始上传名 / knowhow 定位,装配合成
上下文的 ``chunk_context`` 对同一批段落已经各读过一次;这里钉住「沿 baseline
capture 带出来的 ``CitationLookups`` 复用它们」这件事的三条合同:

  ① **字节一致**:复用路径产出的卡与旧路径(``lookups=None``,三个读取整池各
     发一次)逐字节相等——同一次调用里拿旧路径当 oracle,再加一轮「整条回答
     关掉复用重跑」的对照;覆盖无图 / 有图 / 联邦多库 / 带 Knowhow / 按节合成;
  ② **只补缺的那部分**:备忘里没有的键(多元素段落的首个元素、装配没渲染的
     段落、另一节的段落)只对缺的那几个键补读,绝不拿别的批次的值顶替;
  ③ **调用次数下降**:这条腿对三个读取方法的调用次数严格少于旧路径;KG-only
     run 这条腿零调用。

检索侧整体替身(``run_stage``)与 ``test_reasoning_chunk_citations`` 同款;store
层用确定性替身数据(``_Data``)换掉 tier / 来源元数据 / 元素行 / 附图行四个读取,
好让每一个被复用的值都与「读错批」时的值不同。
"""
from __future__ import annotations

import json
import re

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedChunk, RetrievedKnowledge
from app.models.schemas import AskRequest, NotebookCreate
from app.services.ask_service import AskService
from app.services.embedding import FakeEmbedder
from app.services.evidence_context import CitationLookups, EvidenceContextService
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import RecordingModelProvider, bind_chat_client
from tests.model_testkit import bind_all_embedding_clients


BASE_NOTEBOOK = "nb-base-library"


@pytest.fixture(autouse=True)
def _synthetic_elements_are_not_dangling(monkeypatch):
    """Synthetic element ids have no ``source_elements`` row; J2's liveness pass
    would rightly drop those cards, and this file is about how cards are minted."""
    monkeypatch.setattr(AskService, "_drop_dangling_references", lambda self, response: None)


@pytest.fixture
def arepo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(repo, FakeEmbedder(dim=16))
    return repo


class _CiteFirstKeyLLM:
    """计划 / 反思定死;作答引用它在自己那份 prompt 里看到的第一个 ``[k]``。

    读 prompt 而不是写死键号:按节合成每节的号段不同,写死的键在第二节会落空。
    """

    configured = True
    model = "fake-answer-llm"

    def chat_json(self, messages, schema_hint, **kwargs):
        content = messages[-1]["content"]
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "增益"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        if '"relevant"' in schema_hint:
            return json.dumps({"relevant": []})
        keys = re.findall(r"(?m)^(k\d+):", content)
        cite = f"[{keys[0]}]" if keys else ""
        return json.dumps({"answer": f"增益基本稳定{cite}。", "grounded": True})


def _bind(repo):
    client = _CiteFirstKeyLLM()
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload, client)
    return client


def _seed(repo):
    """一个知识对象只为让「无图无集合」的早退闸不生效(``top_hits`` 由替身给)。"""
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(notebook.id, None, [
        {"local_id": "C1", "object_type": "claim",
         "payload": {"name": "增益概述", "section_path": "1"}, "evidence": []},
    ], [])
    return notebook


def _chunk(index, *, elements=2, notebook_id="", text=None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"chunk-{index}",
        source_id=f"src-{index}",
        # 与 ``_Data`` 里的标题刻意不同:卡上出现这个回退值就说明标题那份读取丢了。
        source_title=f"检索标题-{index}",
        section_path=f"§{index} 增益",
        text=text or f"第 {index} 段:源极负反馈让增益稳定。",
        element_ids=[f"el-{index}-{n}" for n in range(elements)],
        score=1.0,
        relevance=1.0 - index * 0.05,
        notebook_id=notebook_id,
    )


class _Data:
    """四个 store 读取的确定性替身数据(按键独立,与真实 store 同一形状)。"""

    def __init__(self):
        self.tiers: dict[str, str] = {BASE_NOTEBOOK: "base"}
        self.sources: dict[str, dict] = {}
        self.elements: dict[str, dict] = {}
        self.images: dict[str, str] = {}

    def source(self, index, *, title=None, file_name=None, notebook_id=""):
        self.sources[f"src-{index}"] = {
            "title": title or f"手册-{index}",
            "file_name": file_name if file_name is not None else f"manual-{index}.pdf",
            "notebook_id": notebook_id,
            "source_type": "pdf",
        }

    def knowhow(self, element_id, *, table="tbl-1", row=None):
        self.elements[element_id] = {"metadata": json.dumps(
            {"knowhow": {"table_id": table, "row_id": row or f"row-{element_id}"}})}

    def image(self, element_id):
        self.images[element_id] = json.dumps(
            {"asset_id": f"asset-{element_id}", "caption": f"图 {element_id}"})


def _install(monkeypatch, repo, data: _Data):
    """把 ``data`` 装到真实 evidence context 的四个读取上,并记下三方法的调用。

    返回 ``(evidence_context, reads)``;``reads`` 是 ``(方法名, 去重后的键)``
    的调用序列。
    """
    ec = repo._runtime.ask_service().evidence_context
    assert isinstance(ec, EvidenceContextService)
    real_tier_map = ec.notebooks.tier_map

    def _tier_map(ids):
        ids = list(ids)
        found = real_tier_map([i for i in ids if i not in data.tiers])
        found.update({i: data.tiers[i] for i in ids if i in data.tiers})
        return found

    monkeypatch.setattr(ec.notebooks, "tier_map", _tier_map)
    monkeypatch.setattr(ec, "source_metadata", lambda ids: {
        i: dict(data.sources[i]) for i in ids if i in data.sources})
    monkeypatch.setattr(ec.sources, "evidence_elements", lambda ids: {
        i: dict(data.elements[i]) for i in ids if i in data.elements})
    monkeypatch.setattr(ec.sources, "image_asset_rows", lambda ids: [
        (i, data.images[i]) for i in ids if i in data.images])

    reads: list[tuple[str, list[str]]] = []
    for name in ("tier_map", "citation_source_info", "knowhow_refs_for"):
        real = getattr(ec, name)

        def _counted(ids, *args, _real=real, _name=name, **kwargs):
            ids = list(ids)
            reads.append((_name, sorted(set(i for i in ids if i))))
            return _real(ids, *args, **kwargs)

        monkeypatch.setattr(ec, name, _counted)
    return ec, reads


def _dump(pairs):
    return [(citation.model_dump_json(), tuple(ids)) for citation, ids in pairs]


def _spy_card_leg(monkeypatch, ec, reads):
    """包住 ``chunk_citations``:每次复用调用之后,同一批入参再走一遍旧路径
    (``lookups=None``)当 oracle。返回每次调用的记录。"""
    real = ec.chunk_citations
    calls: list[dict] = []

    def _spy(chunks, *, notebook_id, anchors=None, lookups=None):
        chunks = list(chunks)
        mark = len(reads)
        pairs = real(chunks, notebook_id=notebook_id, anchors=anchors, lookups=lookups)
        leg_reads = reads[mark:]
        mark = len(reads)
        oracle = real(chunks, notebook_id=notebook_id, anchors=anchors)
        calls.append({
            "lookups": lookups, "got": _dump(pairs), "want": _dump(oracle),
            "reads": leg_reads, "oracle_reads": reads[mark:],
            "chunk_ids": [chunk.chunk_id for chunk in chunks],
        })
        return pairs

    monkeypatch.setattr(ec, "chunk_citations", _spy)
    return calls


def _patch_retrieval(monkeypatch, *, chunks=(), top_hits=(), outline=()):
    from app.application.ask_reasoning import ReasoningEvidenceSnapshot
    from app.services.reasoning_retrieval import ReasoningRetriever

    def _run_stage(self, stage, runtime):
        return ReasoningEvidenceSnapshot(
            top_hits=tuple(top_hits), elements=(), trace=(), chunks=tuple(chunks),
            chains=(), attempted=(), enumerations=(), collection_map_text="",
            outline=tuple(outline), outline_evidence=(), baseline_manifest=None,
        )

    monkeypatch.setattr(ReasoningRetriever, "run_stage", _run_stage)


def _ask(repo, notebook, *, effort=None):
    request = {"question": "增益", "mode": "reasoning"}
    if effort:
        request["retrieval_effort"] = effort
    return repo.ask(notebook.id, AskRequest(**request))


def _payload(response) -> str:
    """回答里由这条腿与附图共同决定的那部分 wire payload。"""
    return response.model_dump_json(include={
        "answer", "conclusion", "grounded", "evidence_level", "anchors", "citations",
    })


def _without_reuse(monkeypatch):
    """旧路径:原文段腿拿不到装配期备忘,三个读取整池各发一次。"""
    real = AskService._reasoning_chunk_cards

    def _cards(self, chunks, baseline, notebook_id):
        stripped = {k: v for k, v in baseline.items() if k != "citation_lookups"}
        return real(self, chunks, stripped, notebook_id)

    monkeypatch.setattr(AskService, "_reasoning_chunk_cards", _cards)


def _section(section_id, title, *keys):
    from app.services.reasoning_retrieval import OutlineSection

    return OutlineSection(id=section_id, title=title, evidence_keys=list(keys))


# --------------------------------------------------------------------------
# 场景:每一个都让「复用的值」与「读错批 / 没读时的回退值」可区分
# --------------------------------------------------------------------------


def _scenario_plain(data, notebook):
    """无图、单库、全是多元素段落:装配期没有 knowhow 读取(只问单元素 chunk),
    所以卡片那一侧对 knowhow 是「部分命中」——只补读首个元素;tier 与来源全命中。
    其中一个多元素段落的首个元素恰好带 knowhow 标记,只有补读才拿得到它。"""
    for index in (1, 2, 3):
        data.source(index)
    data.knowhow("el-2-0")
    return [_chunk(1), _chunk(2), _chunk(3)], {}


def _scenario_images(data, notebook):
    """有图:被引段落的第二个元素是图。附图读取不在复用范围内,但锚点与卡片共享
    同一次附图预算,payload 必须照旧。"""
    for index in (1, 2):
        data.source(index)
        data.image(f"el-{index}-1")
    return [_chunk(1), _chunk(2)], {}


def _scenario_federated(data, notebook):
    """联邦多库:一段来自挂载的参考库(tier=base、原样带出 notebook_id),一段来自
    本库但打着本库自己的 id(必须归一成空),一段不带归属(回退本次 ask 的库)。"""
    data.source(1, notebook_id=BASE_NOTEBOOK)
    data.source(2, notebook_id=notebook.id)
    data.source(3, notebook_id=notebook.id, file_name="")
    return [
        _chunk(1, notebook_id=BASE_NOTEBOOK),
        _chunk(2, notebook_id=notebook.id),
        _chunk(3),
    ], {}


def _scenario_knowhow(data, notebook):
    """带 Knowhow:单元素格子段落的 knowhow 由装配期读过,卡片侧复用;首元素也带
    标记的那个多元素段落装配期不问(只问单元素 chunk),卡片侧只为它补读一次。"""
    for index in (1, 2, 3):
        data.source(index)
    data.knowhow("el-1-0", row="r-1")
    data.knowhow("el-2-0", row="r-2")
    data.knowhow("el-3-0", row="r-3")
    return [_chunk(1, elements=1), _chunk(2, elements=1), _chunk(3, elements=3)], {}


def _scenario_sectioned(data, notebook):
    """按节合成:两节各绑不同的段落,共用本 run 的一份备忘,供原文段腿复用。
    第一节的单元素格子段落由装配期读过 knowhow;其余三段都是多元素段落,装配期
    不问——所以 knowhow 只补读这三段的首元素。"""
    for index in (1, 2, 3, 4):
        data.source(index, title=f"节手册-{index}")
    data.knowhow("el-1-0", row="r-1")
    data.knowhow("el-2-0", row="r-2")
    data.knowhow("el-4-0", row="r-4")
    chunks = [_chunk(1, elements=1), _chunk(2), _chunk(3), _chunk(4)]
    outline = [
        _section("s1", "第一节", "chunk-1", "chunk-2"),
        _section("s2", "第二节", "chunk-3", "chunk-4"),
    ]
    return chunks, {"outline": outline, "effort": "exhaustive"}


SCENARIOS = {
    "plain": _scenario_plain,
    "images": _scenario_images,
    "federated": _scenario_federated,
    "knowhow": _scenario_knowhow,
    "sectioned": _scenario_sectioned,
}


def _run(arepo, monkeypatch, scenario):
    notebook = _seed(arepo)
    _bind(arepo)
    data = _Data()
    chunks, extra = SCENARIOS[scenario](data, notebook)
    ec, reads = _install(monkeypatch, arepo, data)
    _patch_retrieval(monkeypatch, chunks=chunks, outline=extra.get("outline", ()))
    return notebook, ec, reads, extra


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_reused_cards_are_byte_identical_to_the_fresh_read_oracle(
    arepo, monkeypatch, scenario
):
    notebook, ec, reads, extra = _run(arepo, monkeypatch, scenario)
    calls = _spy_card_leg(monkeypatch, ec, reads)

    response = _ask(arepo, notebook, effort=extra.get("effort"))

    assert len(calls) == 1, calls
    (call,) = calls
    assert isinstance(call["lookups"], CitationLookups), (
        "原文段腿必须拿到装配期备忘")
    assert call["got"] == call["want"]
    # 卡片真的用上了替身数据(否则「一致」可能只是两边都回退成了同一个空值)。
    cards = [json.loads(dumped) for dumped, _ids in call["got"]]
    assert cards and all(card["label"].startswith(("手册-", "节手册-")) for card in cards), cards
    # 调用次数严格下降:tier 与来源两份读取总是全命中(装配期按整池读过)。
    names = [name for name, _ids in call["reads"]]
    assert "tier_map" not in names and "citation_source_info" not in names, call["reads"]
    assert len(call["reads"]) < len(call["oracle_reads"]) == 3, (
        call["reads"], call["oracle_reads"])
    if scenario == "sectioned":
        detail = [step.detail for step in response.reasoning_trace
                  if step.step_type == "synthesis"][-1]
        assert detail["outline_sections"] == 2, detail
        assert detail["outline_fallback"] is False


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_the_whole_answer_payload_matches_a_run_without_reuse(
    arepo, monkeypatch, scenario
):
    """整条回答的对照:同一个库、同一份检索终态,先关掉复用跑一遍(旧路径),再
    照常跑一遍,两次回答里引用卡 / 锚点 / tier / knowhow / 附图逐字节相等。"""
    notebook, ec, reads, extra = _run(arepo, monkeypatch, scenario)
    with monkeypatch.context() as patch:
        _without_reuse(patch)
        old = _payload(_ask(arepo, notebook, effort=extra.get("effort")))
    new_response = _ask(arepo, notebook, effort=extra.get("effort"))

    assert _payload(new_response) == old
    citations = new_response.citations
    if scenario == "federated":
        by_source = {c.source_id: c for c in citations}
        assert by_source["src-1"].tier == "base"
        assert by_source["src-1"].notebook_id == BASE_NOTEBOOK
        assert by_source["src-2"].notebook_id == ""
        assert by_source["src-3"].source_file_name == ""
    if scenario in {"knowhow", "plain", "sectioned"}:
        assert any(c.knowhow is not None for c in citations), citations
    if scenario == "images":
        assert any(anchor.images for anchor in new_response.anchors), (
            new_response.anchors)


@pytest.mark.parametrize("scenario, expected", [
    # 装配期没有 knowhow 读取 → 只补读进了 prompt 的首元素,tier / 来源零读取。
    ("plain", [("knowhow_refs_for", ["el-1-0", "el-2-0", "el-3-0"])]),
    # 两个单元素格子装配期读过 → 只补那个多元素段落的首元素。
    ("knowhow", [("knowhow_refs_for", ["el-3-0"])]),
    # 第一节的装配读过单元素的 el-1-0 → 只补三个多元素段落的首元素。
    ("sectioned", [("knowhow_refs_for", ["el-2-0", "el-3-0", "el-4-0"])]),
])
def test_a_partial_hit_reads_exactly_the_missing_keys(
    arepo, monkeypatch, scenario, expected
):
    notebook, ec, reads, extra = _run(arepo, monkeypatch, scenario)
    calls = _spy_card_leg(monkeypatch, ec, reads)

    _ask(arepo, notebook, effort=extra.get("effort"))

    (call,) = calls
    assert call["reads"] == expected
    assert call["got"] == call["want"]


def test_a_kg_only_run_never_reaches_the_card_leg(arepo, monkeypatch):
    """KG-only:没有一段原文进 prompt,原文段腿零调用、零读取。"""
    notebook = _seed(arepo)
    _bind(arepo)
    ec, reads = _install(monkeypatch, arepo, _Data())
    calls = _spy_card_leg(monkeypatch, ec, reads)
    _patch_retrieval(monkeypatch, top_hits=[RetrievedKnowledge(
        object_id="ko-1", object_type="claim", payload={"name": "增益概述"},
        relevance=0.9, score=0.9,
    )])

    response = _ask(arepo, notebook)

    assert response.answer, (response.conclusion, response.model_errors)
    assert calls == []


def test_a_second_run_never_sees_the_first_runs_values(arepo, monkeypatch):
    """备忘属于一次装配:两轮之间来源改了名、knowhow 标记换了元素,第二轮的卡必须
    是第二轮读到的值(拿上一轮的备忘就是拿错批)。用「plain」形态:首元素的
    knowhow 只由卡片那一侧补读、装配期从不问——跨轮共享的备忘在这里会原样把上一
    轮的值递给第二轮,而不会被第二轮的装配读取覆盖掉。"""
    notebook, ec, reads, _extra = _run(arepo, monkeypatch, "plain")
    _spy_card_leg(monkeypatch, ec, reads)
    first = _ask(arepo, notebook)
    assert [c.knowhow.row_id if c.knowhow else None for c in first.citations] == [
        None, "row-el-2-0", None]

    data = _Data()
    for index in (1, 2, 3):
        data.source(index, title=f"改名-{index}")
    data.knowhow("el-1-0", row="r-1-new")
    _install(monkeypatch, arepo, data)
    calls = _spy_card_leg(monkeypatch, ec, reads)
    second = _ask(arepo, notebook)

    assert [c.label.split(" · ")[0] for c in second.citations] == [
        "改名-1", "改名-2", "改名-3"]
    assert [c.knowhow.row_id if c.knowhow else None for c in second.citations] == [
        "r-1-new", None, None]
    (call,) = calls
    assert call["got"] == call["want"]


def test_the_retrieval_only_entry_point_keeps_the_memo_less_reads(
    arepo, monkeypatch
):
    """走真实的仅检索入口(``repo.ask(..., output="evidence")`` → ``_draft_reasoning_
    evidence``):这条路径没有引用卡腿,装配不带备忘,knowhow 读取只问单元素段落,
    与接这个特性之前逐字相同。往那个调用点加 ``citation_lookups=`` 这条就红。"""
    notebook = _seed(arepo)
    _bind(arepo)
    data = _Data()
    for index in (1, 2):
        data.source(index)
    ec, reads = _install(monkeypatch, arepo, data)
    _patch_retrieval(monkeypatch, chunks=[_chunk(1, elements=1), _chunk(2)])
    seen: list = []
    real = AskService._assemble_reasoning_context

    def _spy(self, *args, **kwargs):
        context = real(self, *args, **kwargs)
        seen.append((kwargs.get("citation_lookups"), context.baseline))
        return context

    monkeypatch.setattr(AskService, "_assemble_reasoning_context", _spy)

    evidence = arepo.ask(
        notebook.id, AskRequest(question="增益", mode="reasoning"),
        submitted_via="mcp", output="evidence")

    assert evidence.items, evidence
    assert len(seen) == 1, seen
    lookups, baseline = seen[0]
    assert lookups is None and baseline["citation_lookups"] is None
    assert [ids for name, ids in reads if name == "knowhow_refs_for"] == [["el-1-0"]]


def _old_path(monkeypatch):
    """master 的读取:装配不带备忘(``chunk_context`` 不传 sink),原文段腿也拿不到
    备忘(``chunk_citations(lookups=None)``,三个读取整池各一次)。"""
    real = AskService._assemble_reasoning_context

    def _assemble(self, *args, **kwargs):
        kwargs["citation_lookups"] = None
        return real(self, *args, **kwargs)

    monkeypatch.setattr(AskService, "_assemble_reasoning_context", _assemble)


def _read_totals(reads) -> dict[str, tuple[int, int]]:
    """``{方法: (调用次数, 问到的键总数)}``。store 侧按键去重后取行,所以键数
    就是 store 返回的行数上界(命中多少行由数据决定,两条路径同一份数据)。"""
    totals: dict[str, tuple[int, int]] = {}
    for name, ids in reads:
        calls, rows = totals.get(name, (0, 0))
        totals[name] = (calls + 1, rows + len(ids))
    return totals


def _exact_sections(data, notebook):
    """评审抓到的形状:四节各绑一段单元素段落,外加两段多元素与一段单元素的
    **精确命中**——未绑定任何节,按设计每一节都注入(``reasoning_exact_reserve``)。
    共享段落在各节的装配里反复出现,任何一次重复读取都会让行数超过旧路径。"""
    from dataclasses import replace

    for index in range(1, 8):
        data.source(index)
    data.knowhow("el-5-0")
    chunks = [_chunk(index, elements=1) for index in (1, 2, 3, 4)]
    chunks += [replace(_chunk(index, elements=3), exact_lookup=True) for index in (5, 6)]
    chunks += [replace(_chunk(7, elements=1), exact_lookup=True)]
    outline = [_section(f"s{index}", f"第{index}节", f"chunk-{index}")
               for index in (1, 2, 3, 4)]
    return chunks, {"outline": outline, "effort": "exhaustive"}


class _FailSectionLLM(_CiteFirstKeyLLM):
    """第 2 节两次都吐空答案 → 按节合成整体回退到单次合成。"""

    def chat_json(self, messages, schema_hint, **kwargs):
        if "第2节" in messages[-1]["content"] and "sub_queries" not in schema_hint \
                and "next_action" not in schema_hint:
            return json.dumps({"answer": "", "grounded": False})
        return super().chat_json(messages, schema_hint, **kwargs)


def _track_chunk_path(monkeypatch, ec, reads) -> list:
    """只收 ``chunk_context`` / ``chunk_citations`` 内部发生的读取(KG / element
    腿不经备忘,两条路径一样多,不在这条合同里)。"""
    inside: list = []
    for name in ("chunk_context", "chunk_citations"):
        real = getattr(ec, name)

        def _wrapped(*args, _real=real, **kwargs):
            mark = len(reads)
            result = _real(*args, **kwargs)
            inside.extend(reads[mark:])
            return result

        monkeypatch.setattr(ec, name, _wrapped)
    return inside


def _keys_read_twice(chunk_path_reads) -> dict:
    seen: dict = {}
    for name, ids in chunk_path_reads:
        for key in ids:
            seen[(name, key)] = seen.get((name, key), 0) + 1
    return {key: count for key, count in seen.items() if count > 1}


def _measure(arepo, monkeypatch, shape, *, old, fail_second=False, chunk_path=None):
    notebook = _seed(arepo)
    client = _FailSectionLLM() if fail_second else _CiteFirstKeyLLM()
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(arepo, workload, client)
    data = _Data()
    chunks, extra = shape(data, notebook)
    ec, reads = _install(monkeypatch, arepo, data)
    tracked = _track_chunk_path(monkeypatch, ec, reads)
    _patch_retrieval(monkeypatch, chunks=chunks, outline=extra.get("outline", ()))
    with monkeypatch.context() as patch:
        if old:
            _old_path(patch)
        response = _ask(arepo, notebook, effort=extra.get("effort"))
    if chunk_path is not None:
        chunk_path.extend(tracked)
    return response, _read_totals(reads)


SHAPES = {
    **{name: (lambda data, nb, _f=fn: _f(data, nb)) for name, fn in SCENARIOS.items()},
    "exact-sections": _exact_sections,
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_no_read_asks_for_more_rows_than_the_old_path(arepo, monkeypatch, shape):
    """逐个读取方法比**行数**(问到的键总数)与调用次数:新路径每一项都不多于
    旧路径,且总行数严格更少;回答 payload 逐字节相等。"""
    old_response, old = _measure(arepo, monkeypatch, SHAPES[shape], old=True)
    chunk_path: list = []
    new_response, new = _measure(
        arepo, monkeypatch, SHAPES[shape], old=False, chunk_path=chunk_path)

    assert _payload(new_response) == _payload(old_response)
    # 原文段那条通路上,每个键一个 run 至多读一次(各节、重试、引用卡共用一份备忘)。
    assert _keys_read_twice(chunk_path) == {}, chunk_path
    for name in ("tier_map", "citation_source_info", "knowhow_refs_for"):
        old_calls, old_rows = old.get(name, (0, 0))
        new_calls, new_rows = new.get(name, (0, 0))
        assert new_rows <= old_rows, (name, new, old)
        assert new_calls <= old_calls, (name, new, old)
    assert sum(rows for _c, rows in new.values()) < sum(
        rows for _c, rows in old.values()), (new, old)
    if shape == "exact-sections":
        detail = [step.detail for step in new_response.reasoning_trace
                  if step.step_type == "synthesis"][-1]
        assert detail["outline_sections"] == 4 and not detail["outline_fallback"]
        # 精确块确实注入了每一节(否则这个形状测不到共享段落的重复读取)。
        assert {c.source_id for c in new_response.citations} >= {"src-5", "src-6"}


def test_the_fallback_reuses_what_the_sections_already_read(arepo, monkeypatch):
    """按节合成失败、回退单次合成:回退那次装配拿的是本 run 同一份备忘,各节已读
    过的键不再读;payload 与旧路径逐字节相等,行数严格更少。"""
    old_response, old = _measure(
        arepo, monkeypatch, _exact_sections, old=True, fail_second=True)
    chunk_path: list = []
    new_response, new = _measure(
        arepo, monkeypatch, _exact_sections, old=False, fail_second=True,
        chunk_path=chunk_path)

    detail = [step.detail for step in new_response.reasoning_trace
              if step.step_type == "synthesis"][-1]
    assert detail["outline_fallback"] is True, detail
    assert _payload(new_response) == _payload(old_response)
    assert _keys_read_twice(chunk_path) == {}, chunk_path
    for name in ("tier_map", "citation_source_info", "knowhow_refs_for"):
        assert new.get(name, (0, 0))[1] <= old.get(name, (0, 0))[1], (name, new, old)
    assert sum(r for _c, r in new.values()) < sum(r for _c, r in old.values())


# --------------------------------------------------------------------------
# 服务层单元:``chunk_context(lookup_sink=)`` 与 ``chunk_citations(lookups=)``
# --------------------------------------------------------------------------


class _Notebooks:
    def __init__(self, reads):
        self.reads = reads

    def tier_map(self, notebook_ids):
        ids = list(notebook_ids)
        self.reads.append(("tier_map", sorted(ids)))
        tiers = {"active": "personal", "base": "base"}
        return {i: tiers[i] for i in ids if i in tiers}


class _Sources:
    def __init__(self, reads, metadata, elements):
        self.reads, self.metadata, self.elements = reads, metadata, elements

    def evidence_elements(self, element_ids):
        ids = list(element_ids)
        self.reads.append(("evidence_elements", sorted(ids)))
        return {i: self.elements[i] for i in ids if i in self.elements}

    def source_metadata(self, source_ids):
        ids = list(source_ids)
        self.reads.append(("source_metadata", sorted(ids)))
        return {i: self.metadata[i] for i in ids if i in self.metadata}


def _knowhow_row(row):
    return {"metadata": json.dumps({"knowhow": {"table_id": "t", "row_id": row}})}


def _unit_service():
    reads: list = []
    metadata = {
        "s1": {"title": "Paper A", "file_name": "a.pdf", "notebook_id": "active"},
        "s2": {"title": "Paper B", "file_name": "", "notebook_id": "base"},
        # s3 has no row at all: the miss must be remembered, not re-read.
    }
    elements = {"c1-el0": _knowhow_row("r1"), "c3-el0": _knowhow_row("r3")}
    service = EvidenceContextService(
        notebooks=_Notebooks(reads), sources=_Sources(reads, metadata, elements),
        knowledge=None, settings=Settings(),
    )
    return service, reads


def _unit_chunk(chunk_id, source_id, *, notebook_id="active", elements=2):
    return RetrievedChunk(
        chunk_id=chunk_id, source_id=source_id, source_title=f"fallback {chunk_id}",
        section_path="1.1", text=f"{chunk_id} text", relevance=0.9,
        notebook_id=notebook_id,
        element_ids=[f"{chunk_id}-el{n}" for n in range(elements)],
    )


def _unit_chunks():
    return [
        _unit_chunk("c1", "s1", elements=1),
        _unit_chunk("c2", "s2", notebook_id="base"),
        _unit_chunk("c3", "s3", notebook_id=""),
    ]


def test_the_sink_leaves_the_context_and_the_first_reads_unchanged():
    """新备忘上的第一次装配:读取与不传时逐字相同(同样的键、同样的次数),上下文
    逐字节相等;同一份备忘上的第二次装配(另一节 / 重试)一个键都不再读。"""
    service, reads = _unit_service()
    plain = service.chunk_context(_unit_chunks(), notebook_id="active")
    plain_reads = list(reads)
    reads.clear()
    memo = CitationLookups()
    sunk = service.chunk_context(_unit_chunks(), notebook_id="active", lookup_sink=memo)

    assert sunk == plain
    assert reads == plain_reads
    assert plain_reads[-1] == ("evidence_elements", ["c1-el0"])
    # 每一个问过的键都记下,没命中的记 None(是「问过」而不是「没见过」)。
    assert set(memo.tiers) == {"active", "base"}
    assert memo.source_info["s3"] is None
    assert set(memo.knowhow) == {"c1-el0"}

    reads.clear()
    again = service.chunk_context(_unit_chunks(), notebook_id="active", lookup_sink=memo)
    assert again == plain
    assert reads == []


def test_a_full_hit_reads_nothing_and_equals_the_fresh_read():
    service, reads = _unit_service()
    chunks = [_unit_chunk("c1", "s1", elements=1),
              _unit_chunk("c3", "s3", notebook_id="", elements=1)]
    memo = CitationLookups()
    service.chunk_context(chunks, notebook_id="active", lookup_sink=memo)
    reads.clear()

    reused = service.chunk_citations(chunks, notebook_id="active", lookups=memo)

    assert reads == []
    assert _dump(reused) == _dump(service.chunk_citations(chunks, notebook_id="active"))
    assert reused[1][0].knowhow is not None


def test_a_partial_hit_reads_only_the_keys_the_assembly_never_asked():
    """装配只渲染了前两段(第三段被预算挤掉,没问过它的任何键),卡片却要三段:
    只对没问过的键补读——第二段(多元素)与第三段的首元素、第三段的来源;tier 的
    "" 归属回退到本次 ask 的库,已命中。"""
    service, reads = _unit_service()
    memo = CitationLookups()
    service.chunk_context(_unit_chunks()[:2], notebook_id="active", lookup_sink=memo)
    reads.clear()

    reused = service.chunk_citations(_unit_chunks(), notebook_id="active", lookups=memo)

    assert reads == [("evidence_elements", ["c2-el0", "c3-el0"]),
                     ("source_metadata", ["s3"])]
    assert _dump(reused) == _dump(service.chunk_citations(
        _unit_chunks(), notebook_id="active"))
    assert reused[2][0].knowhow is not None, "补读的 knowhow 必须落到卡上"


def test_a_memo_from_another_batch_never_replaces_a_key_it_did_not_ask():
    """另一批段落的备忘(别的来源、别的元素)对这一批全部不命中:三个读取各补一次,
    值来自这一批自己的读取。"""
    service, reads = _unit_service()
    memo = CitationLookups()
    service.chunk_context(
        [_unit_chunk("other", "s-other", notebook_id="base")],
        notebook_id="active", lookup_sink=memo)
    reads.clear()

    chunks = _unit_chunks()[:1]
    reused = service.chunk_citations(chunks, notebook_id="active", lookups=memo)

    assert sorted(name for name, _ids in reads) == [
        "evidence_elements", "source_metadata", "tier_map"]
    assert _dump(reused) == _dump(service.chunk_citations(chunks, notebook_id="active"))


def test_merge_keeps_every_section_and_never_overwrites_an_answer():
    first, second = CitationLookups(), CitationLookups()
    first.tiers.update({"active": "personal"})
    first.source_info.update({"s1": None})
    second.tiers.update({"active": "SHOULD-NOT-WIN", "base": "base"})
    second.knowhow.update({"e1": None})

    first.merge(second)

    assert first.tiers == {"active": "personal", "base": "base"}
    assert first.source_info == {"s1": None}
    assert first.knowhow == {"e1": None}
