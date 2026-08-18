"""T6（深度报告引用卡附图）：报告 `references`（`report_engine._assemble` 全局
重编号后的 dict 列表）复用 T1 的共享 enrichment（`EvidenceContextService.
citation_images_for`），经新增的 `attach_reference_images` 就地填上
``"images": [{element_id, asset_id, caption}, ...]``。

三条设计前提（同 `test_citation_images.py` §0/§2，本文件不重复背景，只钉报告
侧特有的三点）：

1. **报告的 references 是 dict，不是 Citation/AnswerAnchor 模型**——批量装配
   因此以每条 reference 的 ``key``（"k1"、"k2"…）为候选映射的键，而不是
   `attach_citation_images` 那种"以 target 对象本身为键"的写法。
2. **chunk 引用的候选需要 chunk 的完整 element_ids**——`chunk_context` 只在
   恰好单元素的 chunk 上填 ``element_id``，多元素 chunk（"一段正文 + 一张
   配图"）被跳过；`_draft_section` 必须把该 chunk 的完整 `element_ids` 补写进
   `chunk_map` 的 ctx，`_assemble` 才有候选可用。
3. **一份报告只发一次批量点查**——不管报告有多少节、多少条引用。
"""
from __future__ import annotations

import json

import pytest
from tests.model_testkit import RecordingModelProvider, bind_chat_client


# ---------------------------------------------------------------------------
# 单元层：假 sources，无 DB / 无 HTTP / 无网络。镜像 test_citation_images.py
# 的 _SpySources 约定（同一份 SQL 形状：只回 (id, metadata)，evidence_elements
# 宽读直接报错，钉死"附图路径不得走宽读"这条性能契约）。
# ---------------------------------------------------------------------------


class _SpySources:
    def __init__(self, elements: dict[str, dict]) -> None:
        self._elements = elements
        self.calls: list[list[str]] = []

    def evidence_elements(self, element_ids):
        raise AssertionError(
            "附图路径不得走宽读 evidence_elements()，必须用窄的 image_asset_rows()"
        )

    def image_asset_rows(self, element_ids):
        ids = list(element_ids)
        self.calls.append(ids)
        return [
            (eid, self._elements[eid]["metadata"])
            for eid in ids
            if eid in self._elements
            and self._elements[eid].get("element_type") == "image"
        ]

    def source_metadata(self, source_ids):
        return {}


class _Notebooks:
    def tier_map(self, notebook_ids):
        return {}

    def participant_notebook_ids(self, active_notebook_id):
        return [active_notebook_id]


class _Knowledge:
    def cluster_fold(self, notebook_id, object_ids):
        return {}

    def node_context(self, notebook_id, object_id):
        return {}

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_counts(self, notebook_id, triples):
        return {triple: 0 for triple in triples}


def _service(sources):
    from app.core.config import Settings
    from app.services.evidence_context import EvidenceContextService

    return EvidenceContextService(
        notebooks=_Notebooks(), sources=sources, knowledge=_Knowledge(),
        settings=Settings(),
    )


def _row(element_type: str, metadata: dict) -> dict:
    return {
        "element_type": element_type,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


def _image_row(asset_id: str, caption: str = "图 1") -> dict:
    return _row("image", {"asset_id": asset_id, "caption": caption})


def _reference(key: str, **kwargs) -> dict:
    base = {
        "key": key, "object_id": "obj", "object_type": "chunk",
        "label": "Doc · §1", "source_title": "Doc", "location_label": "§1",
    }
    base.update(kwargs)
    return base


# --- attach_reference_images：候选规则 --------------------------------------


def test_reference_with_a_single_candidate_id_gets_its_image():
    sources = _SpySources({"el-img": _image_row("asset-1", "时序收敛示意图")})
    reference = _reference("k1")

    _service(sources).attach_reference_images([reference], {"k1": ["el-img"]})

    assert reference["images"] == [
        {"element_id": "el-img", "asset_id": "asset-1", "caption": "时序收敛示意图"}
    ]
    assert sources.calls == [["el-img"]]


def test_chunk_reference_finds_the_figure_through_the_chunks_element_ids():
    """本特性要救的主场景：一段正文 + 一张配图组成的多元素 chunk。`element_id`
    单独看是空的（chunk_context 只在单元素时才填），必须由候选映射带出该 chunk
    的完整 element_ids。"""
    sources = _SpySources({
        "el-text": _row("paragraph", {}),
        "el-fig": _image_row("asset-7", "图 2 时钟树"),
    })
    reference = _reference("k1", element_id="")

    _service(sources).attach_reference_images(
        [reference], {"k1": ["el-text", "el-fig"]}
    )

    assert [image["asset_id"] for image in reference["images"]] == ["asset-7"]


def test_reference_without_a_candidate_entry_is_left_untouched():
    sources = _SpySources({"el-img": _image_row("asset-1")})
    reference = _reference("k1")

    _service(sources).attach_reference_images([reference], {})

    assert "images" not in reference
    assert sources.calls == []


def test_no_store_call_when_nothing_could_carry_an_image():
    sources = _SpySources({})
    reference = _reference("k1")

    _service(sources).attach_reference_images([reference], {"k1": []})
    _service(sources).attach_reference_images([], {"k1": ["el-img"]})

    assert sources.calls == []


def test_non_image_and_missing_ids_resolve_to_no_images_without_raising():
    sources = _SpySources({"el-para": _row("paragraph", {})})
    reference = _reference("k1")

    _service(sources).attach_reference_images(
        [reference], {"k1": ["el-para", "el-absent"]}
    )

    assert "images" not in reference


# --- 批量口径：一份报告一次批量点查 ------------------------------------------


def test_a_whole_report_resolves_in_exactly_one_store_call():
    sources = _SpySources({
        "el-a1": _image_row("asset-a"),
        "el-b1": _row("paragraph", {}),
        "el-b2": _image_row("asset-b"),
    })
    references = [_reference("k1"), _reference("k2")]

    _service(sources).attach_reference_images(
        references, {"k1": ["el-a1"], "k2": ["el-b1", "el-b2"]}
    )

    assert [image["asset_id"] for image in references[0]["images"]] == ["asset-a"]
    assert [image["asset_id"] for image in references[1]["images"]] == ["asset-b"]
    # 两条引用、三个候选 id → 恰好一次读取。
    assert len(sources.calls) == 1
    assert sorted(sources.calls[0]) == ["el-a1", "el-b1", "el-b2"]


# --- 上限截断 ----------------------------------------------------------------


def test_per_reference_cap_truncates_deterministically_by_ascending_element_id():
    from app.services.evidence_context import CITATION_IMAGES_PER_ANCHOR

    ids = [f"el-{index:04d}" for index in range(1, 7)]
    sources = _SpySources({eid: _image_row(f"asset-{eid}") for eid in ids})
    reference = _reference("k1")

    _service(sources).attach_reference_images(
        [reference], {"k1": list(reversed(ids))}
    )

    assert len(reference["images"]) == CITATION_IMAGES_PER_ANCHOR
    assert [image["element_id"] for image in reference["images"]] == \
        ids[:CITATION_IMAGES_PER_ANCHOR]


def test_per_report_cap_is_consumed_in_reference_key_order_not_dict_insertion_order():
    """预算按 `references` 列表自身的 "k1, k2, …" 顺序消费，不按 candidate_ids
    字典的插入序——两者若不一致（这里刻意把 k2 的候选先插入字典），仍必须是
    k1 先拿到图、预算耗尽后 k2 才留空。"""
    from app.services.evidence_context import (
        CITATION_IMAGES_PER_ANCHOR, CITATION_IMAGES_PER_REPORT,
    )

    per_reference = CITATION_IMAGES_PER_ANCHOR
    target_count = (CITATION_IMAGES_PER_REPORT // per_reference) + 2
    assert per_reference * target_count > CITATION_IMAGES_PER_REPORT

    elements: dict[str, dict] = {}
    candidate_ids: dict[str, list[str]] = {}
    references = []
    # 倒序构造字典插入序（k{N} 先插入，k1 最后插入），证明消费顺序看的是
    # references 列表而不是这个字典的迭代序。
    for group in reversed(range(target_count)):
        key = f"k{group + 1}"
        ids = [f"el-{group}-{index}" for index in range(per_reference)]
        for eid in ids:
            elements[eid] = _image_row(f"asset-{eid}")
        candidate_ids[key] = ids
    for group in range(target_count):
        references.append(_reference(f"k{group + 1}"))
    sources = _SpySources(elements)

    _service(sources).attach_reference_images(references, candidate_ids)

    total = sum(len(reference.get("images") or []) for reference in references)
    assert total == CITATION_IMAGES_PER_REPORT
    full_count = CITATION_IMAGES_PER_REPORT // per_reference
    for index, reference in enumerate(references):
        if index < full_count:
            assert len(reference["images"]) == per_reference, index
        else:
            assert "images" not in reference or reference["images"] == [], index


def test_reference_image_caps_exceed_the_single_answer_budget():
    """报告横跨多个章节，参考文献条数天然多于单次 Ask 回答的锚点数——复用
    `CITATION_IMAGES_PER_ANSWER` 会让排在后面章节的引用永远分不到图。"""
    from app.services.evidence_context import (
        CITATION_IMAGES_PER_ANSWER, CITATION_IMAGES_PER_REPORT,
    )

    assert CITATION_IMAGES_PER_REPORT > CITATION_IMAGES_PER_ANSWER


# ---------------------------------------------------------------------------
# 集成层：真实 SQLite,驱动 report_engine._draft_section / _assemble,证明生产
# 装配点真的接上了。
# ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL",
               "REWRITE_LLM_API_KEY", "REWRITE_LLM_BASE_URL", "REWRITE_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    provider = RecordingModelProvider()
    repository = SQLiteRepository(Settings(_env_file=None), model_provider=provider)
    repository.recording_model_provider = provider
    return repository


def _mk_nb(repo):
    from app.models.schemas import NotebookCreate
    return repo.create_notebook(NotebookCreate(name="t", purpose="p", primary_domain="d"))


def _mk_engine(repo, llm):
    from app.services.report_engine import ReportEngine
    for workload_id in (
        "report_outline", "report_sufficiency", "report_section",
        "report_summary", "query_rewrite", "reasoning_agent", "ask_answer",
    ):
        bind_chat_client(repo, workload_id, llm)
    return ReportEngine.from_repository(repo, repo.settings)


def _seed_source_with_a_figure(repo, notebook_id: str) -> tuple[str, str, str]:
    """一段正文 + 一张带图注的配图，各自单独一行 source_elements（不实际跑
    分块管线——`_draft_section` 只需要 `chunk_map` 的 ctx 带 element_ids，
    真实取图走的是 `image_asset_rows()` 的窄读，不依赖 chunk 表本身）。"""
    source_id = "src-fig-1"
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, "时序手册", "document", "timing.md",
             "/tmp/timing.md", 0, "h", "", "", "extracted", now, now),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"el-{source_id}-text", source_id, "paragraph", "p1",
             "收敛需要在综合阶段预留时序裕量。", "{}", now),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"el-{source_id}-fig", source_id, "image", "Markdown image 1",
             "图 1 时钟树收敛示意",
             json.dumps({"asset_id": "asset-fig-1", "caption": "图 1 时钟树收敛示意"},
                        ensure_ascii=False), now),
        )
    return source_id, f"el-{source_id}-text", f"el-{source_id}-fig"


def _spy_on_image_asset_rows(repo, monkeypatch) -> list:
    calls: list[list[str]] = []
    original = repo._runtime.source_store.image_asset_rows

    def _spy(element_ids):
        ids = list(element_ids)
        calls.append(ids)
        return original(ids)

    monkeypatch.setattr(repo._runtime.source_store, "image_asset_rows", _spy)
    return calls


def test_draft_section_carries_the_chunks_full_element_ids_into_chunk_map(repo):
    """T6 wiring 1/2:`_draft_section` 必须把 chunk 的完整 element_ids 补写进
    `chunk_map`（`chunk_context` 单看 element_id 会因多元素 chunk 而漏图）。"""
    from app.services.reasoning_retrieval import ReasoningResult
    from app.services.retrieval import RetrievedChunk

    class _ChunkLLM:
        configured = True
        model = "m"

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"markdown": "## A\n正文附近有配图 [k1]。",
                               "grounded": True})

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _ChunkLLM())
    source_id, text_eid, fig_eid = _seed_source_with_a_figure(repo, nb.id)
    result = ReasoningResult(chunks=[RetrievedChunk(
        chunk_id="chunk-1", source_id=source_id, source_title="时序手册",
        section_path="§1", text="收敛需要在综合阶段预留时序裕量。",
        element_ids=[text_eid, fig_eid], relevance=0.9,
    )])
    out = eng._draft_section(
        nb.id, {"title": "A", "scope": "sa", "sub_queries": ["qa"]}, "q", result,
    )
    assert out["id_map"]["k1"]["object_id"] == "chunk-1"
    # element_id 单看仍是空的(多元素 chunk),但补写的 element_ids 带出完整候选。
    assert out["id_map"]["k1"]["element_id"] == ""
    assert sorted(out["id_map"]["k1"]["element_ids"]) == sorted([text_eid, fig_eid])


def test_assemble_attaches_images_to_a_chunk_reference_via_one_batch_read(
    repo, monkeypatch,
):
    """T6 wiring 2/2（端到端）：_draft_section → _assemble 的完整链路，附图
    只发一次 image_asset_rows 调用（与本次报告引用条数无关）。"""
    from app.services.reasoning_retrieval import ReasoningResult
    from app.services.retrieval import RetrievedChunk

    class _ChunkLLM:
        configured = True
        model = "m"

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({"markdown": "## A\n正文附近有配图 [k1]。",
                               "grounded": True})

    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _ChunkLLM())
    source_id, text_eid, fig_eid = _seed_source_with_a_figure(repo, nb.id)
    calls = _spy_on_image_asset_rows(repo, monkeypatch)
    result = ReasoningResult(chunks=[RetrievedChunk(
        chunk_id="chunk-1", source_id=source_id, source_title="时序手册",
        section_path="§1", text="收敛需要在综合阶段预留时序裕量。",
        element_ids=[text_eid, fig_eid], relevance=0.9,
    )])
    section = eng._draft_section(
        nb.id, {"title": "A", "scope": "sa", "sub_queries": ["qa"]}, "q", result,
    )
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(
        nb.id, rid, "q", [{"title": "A", "scope": "sa"}], [section],
    )

    assert len(references) == 1
    assert references[0]["images"] == [
        {"element_id": fig_eid, "asset_id": "asset-fig-1", "caption": "图 1 时钟树收敛示意"}
    ]
    # 批量口径:恰好一次读取。
    assert len(calls) == 1, calls


class _SummaryOnlyLLM:
    """只服务 `_assemble` 自己发的执行摘要调用；这条测试不经过 `_draft_section`
    （sections 手工构造），所以不需要 report_section/report_outline 分支。"""
    configured = True

    def chat_json(self, messages, schema_hint, **kw):
        return json.dumps({"summary": "总结"})


def test_assemble_without_any_image_evidence_makes_no_store_call(repo, monkeypatch):
    """没有任何候选 element id 时(纯文字证据),附图路径必须整体不发起查询——
    与 T1 的『no-op 是零成本』红线同一条。"""
    nb = _mk_nb(repo)
    eng = _mk_engine(repo, _SummaryOnlyLLM())
    calls = _spy_on_image_asset_rows(repo, monkeypatch)
    outline = [{"title": "A", "scope": "sa", "sub_queries": ["qa"]}]
    sections = [{
        "title": "A", "scope": "sa", "markdown": "## A\nbody [k1]", "grounded": True,
        "id_map": {"k1": {"object_id": "c1", "object_type": "chunk", "name": "BGR",
                          "source_title": "Razavi", "location_label": "§11",
                          "tier": "base"}},
        "attempted": [],
    }]
    rid = repo.create_report(nb.id, "q")
    md, gaps, references = eng._assemble(nb.id, rid, "q", outline, sections)
    assert "images" not in references[0]
    assert calls == []


# --- 持久化往返:重开报告仍能看到 images ------------------------------------


def test_persisted_report_references_round_trip_their_images(repo):
    nb = _mk_nb(repo)
    rid = repo.create_report(nb.id, "q")
    repo.update_report(
        nb.id, rid, status="done", content_md="# 报告\n\n正文 [k1]",
        references=[{
            "key": "k1", "label": "时序手册", "source_title": "时序手册",
            "location_label": "§1", "snippet": "……",
            "images": [
                {"element_id": "el-fig", "asset_id": "asset-fig-1", "caption": "图 1"}
            ],
        }],
    )
    detail = repo.get_report(nb.id, rid)
    assert detail["references"][0]["images"] == [
        {"element_id": "el-fig", "asset_id": "asset-fig-1", "caption": "图 1"}
    ]
