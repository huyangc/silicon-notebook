"""T1（检索结果带图，后端引用附图）：绑定证据里带图注的图片元素，经共享的
`EvidenceContextService.attach_citation_images` 富化成 `Citation.images` /
`AnswerAnchor.images`（`CitationImage = {element_id, asset_id, caption}`）。

三条设计前提决定了这些用例的形状（设计文档 §0/§2）：

1. **模型不看图**：这是纯响应装配层的增强——检索行为、引用文本、锚点解析逐字
   不变。所以本文件从不断言分数/顺序，只断言「多出来的那个字段」。
2. **图注是图片进检索的唯一入口**：`build_chunks` 只让**有图注**的 image 元素
   进 chunk（`chunking.py` 的 `_SKIP_TYPES and not caption`），它的 element id
   因此躺在该 chunk 的 `element_ids` 里——这正是本特性的取图路径。
3. **字段必须同时活在锚点上**：reasoning 的权威显示路径是 `[k]` 锚点，只加在
   Citation 上就只覆盖回退列表。

批量口径与 `test_knowhow_citation.py` 逐字同一条：不管一次回答带出多少张图，
`evidence_elements()` 只发生**一次**——运行效率是一等约束。本文件沿用那份文件的
`_SpySources` / `_spy_on_evidence_elements` 约定（前者纯单测记参数，后者包住
**真实** store 观察生产接线），而不是发明第三种。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.ask import AnswerAnchor, Citation, CitationImage
from app.models.schemas import AskRequest, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.evidence_context import (
    CITATION_IMAGES_PER_ANCHOR,
    CITATION_IMAGES_PER_ANSWER,
    EvidenceContextService,
    anchor_image_targets,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import (
    RecordingModelProvider, bind_all_embedding_clients, bind_chat_client,
)


# ---------------------------------------------------------------------------
# 单元层：假 sources/notebooks/knowledge，无 DB / 无 HTTP / 无网络。
# ---------------------------------------------------------------------------


class _Notebooks:
    def tier_map(self, notebook_ids):
        return {}

    def participant_notebook_ids(self, active_notebook_id):
        return [active_notebook_id]


class _Knowledge:
    def cluster_map(self, notebook_id):
        return {}

    def cluster_fold(self, notebook_id, object_ids):
        return {}

    def node_context(self, notebook_id, object_id):
        return {}

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_count(self, notebook_id, source_id, edge_type, target_id):
        return 0

    def relation_support_counts(self, notebook_id, triples):
        return {triple: 0 for triple in triples}


class _SpySources:
    """记录每次批量调用，让「只查一次」成为主动断言而不是假设。"""

    def __init__(self, elements: dict[str, dict]) -> None:
        self._elements = elements
        self.calls: list[list[str]] = []

    def evidence_elements(self, element_ids):
        ids = list(element_ids)
        self.calls.append(ids)
        return {eid: self._elements[eid] for eid in ids if eid in self._elements}

    def source_metadata(self, source_ids):
        return {}


def _service(sources) -> EvidenceContextService:
    return EvidenceContextService(
        notebooks=_Notebooks(), sources=sources, knowledge=_Knowledge(),
        settings=Settings(),
    )


def _row(element_type: str, metadata: dict) -> dict:
    """一行 `evidence_elements()` 结果。两个后端返回的 `metadata` 都是 JSON
    **字符串**（PostgreSQL 侧经 `_metadata_compat` 归一），所以 fake 也存字符串。"""
    return {
        "element_type": element_type,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


def _image_row(asset_id: str, caption: str = "图 1") -> dict:
    return _row("image", {"asset_id": asset_id, "caption": caption})


def _citation(element_id: str = "", **kwargs) -> Citation:
    return Citation(
        label="Doc · §1", source_id="src-1", element_id=element_id,
        location_label="§1", quoted_span="span", **kwargs,
    )


def _anchor(object_type: str, object_id: str, element_id: str = "") -> AnswerAnchor:
    return AnswerAnchor(
        key="k1", object_id=object_id, object_type=object_type, label="l",
        element_id=element_id,
    )


# --- citation_images_for：准入判据 -----------------------------------------


def test_captioned_image_with_asset_becomes_an_attached_image():
    sources = _SpySources({"el-img": _image_row("asset-9", "时序收敛示意图")})
    images = _service(sources).citation_images_for(["el-img"])

    assert set(images) == {"el-img"}
    assert images["el-img"] == CitationImage(
        element_id="el-img", asset_id="asset-9", caption="时序收敛示意图",
    )


def test_non_image_element_is_not_attached_even_when_metadata_carries_an_asset_id():
    # 判据是 element_type == 'image' **且** asset_id 非空；只看 asset_id 会让
    # 任何顺手带了这个键的元素（例如未来某种附件形态）被当成配图渲染。
    sources = _SpySources({
        "el-para": _row("paragraph", {"asset_id": "asset-9", "caption": "图 1"}),
    })
    assert _service(sources).citation_images_for(["el-para"]) == {}


def test_image_without_a_persisted_asset_is_not_attached():
    # 未落资产的图（远端 http src / 落盘失败）取不到图，只能留图注文本。
    sources = _SpySources({
        "el-remote": _row("image", {"src": "https://example.com/a.png", "caption": "图 1"}),
    })
    assert _service(sources).citation_images_for(["el-remote"]) == {}


def test_image_without_a_caption_still_attaches_but_with_an_empty_caption():
    # caption 只取 metadata.caption，刻意不回退元素 text——无图注时 text 是
    # 「Markdown 图 3」这类占位定位串，把它当图注渲染是在编造说明文字。
    sources = _SpySources({"el-img": _row("image", {"asset_id": "asset-2"})})
    images = _service(sources).citation_images_for(["el-img"])

    assert images["el-img"].asset_id == "asset-2"
    assert images["el-img"].caption == ""


def test_malformed_metadata_and_missing_rows_resolve_to_nothing_without_raising():
    sources = _SpySources({
        "el-bad": {"element_type": "image", "metadata": "{not json"},
        "el-list": {"element_type": "image", "metadata": "[1, 2]"},
        "el-none": {"element_type": "image", "metadata": None},
        "el-numeric": _row("image", {"asset_id": 12345}),
    })
    images = _service(sources).citation_images_for(
        ["el-bad", "el-list", "el-none", "el-numeric", "el-absent"]
    )
    assert images == {}


def test_citation_images_for_dedupes_and_skips_the_store_call_when_every_id_is_falsy():
    sources = _SpySources({})
    assert _service(sources).citation_images_for(["", ""]) == {}
    assert sources.calls == []


# --- attach_citation_images：候选规则 ---------------------------------------


def test_chunk_anchor_finds_the_figure_through_the_chunks_element_ids():
    """本特性要救的主场景：一段正文 + 一张配图组成的多元素 chunk。

    `chunk_context` 只在 chunk 恰好单元素时才填 `anchor.element_id`，所以这里
    锚点自身的 element_id 是空的——只看它的话一张图都出不来，必须按 object_id
    反查 chunk 的整个 `element_ids`。
    """
    sources = _SpySources({
        "el-0001": _row("paragraph", {}),
        "el-0002": _image_row("asset-7", "图 2 时钟树"),
    })
    anchor = _anchor("chunk", "chunk-1")
    assert anchor.element_id == ""

    service = _service(sources)
    service.attach_citation_images(
        anchor_image_targets([anchor], {"chunk-1": ["el-0001", "el-0002"]})
    )

    assert [image.asset_id for image in anchor.images] == ["asset-7"]
    assert anchor.images[0].caption == "图 2 时钟树"


def test_element_anchor_uses_its_own_element_id_with_no_extra_candidates():
    sources = _SpySources({"el-img": _image_row("asset-3")})
    anchor = _anchor("element", "el-img", element_id="el-img")

    _service(sources).attach_citation_images(anchor_image_targets([anchor], {}))

    assert [image.element_id for image in anchor.images] == ["el-img"]


def test_kg_anchor_uses_its_grounding_evidence_element_id():
    # KG 锚点的 element_id 由 knowledge_context 从 occurrences[0] 填好，与
    # element 锚点走同一条「自身 element_id」规则，无需额外候选。
    sources = _SpySources({"el-fig": _image_row("asset-5")})
    anchor = _anchor("claim", "ko-1", element_id="el-fig")

    _service(sources).attach_citation_images(anchor_image_targets([anchor], {}))

    assert [image.asset_id for image in anchor.images] == ["asset-5"]


def test_anchor_for_a_chunk_that_is_not_in_the_map_simply_gets_no_images():
    sources = _SpySources({"el-img": _image_row("asset-1")})
    anchor = _anchor("chunk", "chunk-missing")

    _service(sources).attach_citation_images(anchor_image_targets([anchor], {}))

    assert anchor.images == []
    assert sources.calls == []


def test_memory_citation_never_gets_images():
    # Memory 引用的 element_id 指向记忆自己的投影行，不是文档证据。
    sources = _SpySources({"el-img": _image_row("asset-1")})
    memory = _citation("el-img", memory_id="mem-1")
    ordinary = _citation("el-img")

    _service(sources).attach_citation_images([(memory, ()), (ordinary, ())])

    assert memory.images == []
    assert ordinary.images and ordinary.images[0].asset_id == "asset-1"
    # 被跳过的目标连它的候选 id 都不该进批量查询。
    assert sources.calls == [["el-img"]]


# --- 批量口径 ---------------------------------------------------------------


def test_a_whole_answer_resolves_in_exactly_one_store_call():
    sources = _SpySources({
        "el-a1": _image_row("asset-a"),
        "el-b1": _row("paragraph", {}),
        "el-b2": _image_row("asset-b"),
    })
    anchors = [_anchor("chunk", "chunk-a"), _anchor("chunk", "chunk-b")]
    citation = _citation("el-a1")

    _service(sources).attach_citation_images(
        anchor_image_targets(
            anchors, {"chunk-a": ["el-a1"], "chunk-b": ["el-b1", "el-b2"]}
        ) + [(citation, ["el-a1"])]
    )

    assert [image.asset_id for image in anchors[0].images] == ["asset-a"]
    assert [image.asset_id for image in anchors[1].images] == ["asset-b"]
    assert [image.asset_id for image in citation.images] == ["asset-a"]
    # 三个目标、四个候选 id（含一个跨目标重复）→ 恰好一次读取、id 已去重。
    assert len(sources.calls) == 1
    assert sorted(sources.calls[0]) == ["el-a1", "el-b1", "el-b2"]


def test_no_store_call_at_all_when_nothing_could_carry_an_image():
    sources = _SpySources({})
    anchors = [_anchor("chunk", "chunk-a")]

    _service(sources).attach_citation_images(
        anchor_image_targets(anchors, {"chunk-a": []})
    )
    _service(sources).attach_citation_images([])

    assert sources.calls == []


# --- 上限截断 ---------------------------------------------------------------


def test_per_anchor_cap_truncates_deterministically_by_ascending_element_id():
    ids = [f"el-{index:04d}" for index in range(1, 7)]
    sources = _SpySources({eid: _image_row(f"asset-{eid}") for eid in ids})
    # 乱序传入：截断必须按 element id 升序，不按调用方给的顺序,否则同一个问题
    # 两次问会带出不同的图。
    anchor = _anchor("chunk", "chunk-1")

    _service(sources).attach_citation_images(
        anchor_image_targets([anchor], {"chunk-1": list(reversed(ids))})
    )

    assert len(anchor.images) == CITATION_IMAGES_PER_ANCHOR
    assert [image.element_id for image in anchor.images] == ids[:CITATION_IMAGES_PER_ANCHOR]


def test_per_answer_cap_bounds_the_whole_response_across_anchors_and_citations():
    # 每目标 3 张 × 6 个目标 = 18 张候选，回答级上限把它压到 12。
    per_target = CITATION_IMAGES_PER_ANCHOR
    targets = 6
    assert per_target * targets > CITATION_IMAGES_PER_ANSWER
    elements: dict[str, dict] = {}
    chunk_map: dict[str, list[str]] = {}
    anchors: list[AnswerAnchor] = []
    for group in range(targets):
        ids = [f"el-{group}-{index}" for index in range(per_target)]
        for eid in ids:
            elements[eid] = _image_row(f"asset-{eid}")
        chunk_map[f"chunk-{group}"] = ids
        anchors.append(_anchor("chunk", f"chunk-{group}"))
    sources = _SpySources(elements)

    _service(sources).attach_citation_images(anchor_image_targets(anchors, chunk_map))

    total = sum(len(anchor.images) for anchor in anchors)
    assert total == CITATION_IMAGES_PER_ANSWER
    # 预算按传入顺序消费，耗尽后的目标干脆留空（不是各拿半张）。
    assert all(len(anchor.images) == per_target for anchor in anchors[:4])
    assert all(anchor.images == [] for anchor in anchors[4:])


# --- 响应形状 / 旧 payload 兼容 ---------------------------------------------


def test_empty_images_are_absent_from_json_and_old_payloads_load_without_migration():
    """`exclude_if` 惯例：空列表整体从 JSON 缺席，所以绝大多数（无图）引用的
    payload 一个字节都不多带；反过来，**旧持久化 payload** 里根本没有这个键，
    重开会话时必须自然回退成空列表而不是校验失败——零 migration 的全部依据。"""
    citation = _citation("el-1")
    anchor = _anchor("chunk", "chunk-1")
    assert "images" not in citation.model_dump(mode="json")
    assert "images" not in anchor.model_dump(mode="json")

    legacy_citation = {
        "label": "Doc · §1", "source_id": "src-1", "element_id": "el-1",
        "location_label": "§1", "quoted_span": "span", "tier": "personal",
    }
    legacy_anchor = {
        "key": "k1", "object_id": "chunk-1", "object_type": "chunk", "label": "l",
    }
    assert Citation(**legacy_citation).images == []
    assert AnswerAnchor(**legacy_anchor).images == []

    citation.images = [CitationImage(element_id="el-1", asset_id="a1", caption="图")]
    assert citation.model_dump(mode="json")["images"] == [
        {"element_id": "el-1", "asset_id": "a1", "caption": "图"}
    ]


# ---------------------------------------------------------------------------
# 集成层：真实 SQLite + 真实分块，驱动 ask_chunk / ask_reasoning，证明生产装配点
# 真的接上了（上面的单测只证明助手本身正确）。镜像 test_chunk_embed.py 的
# `_seed_source_with_elements` + `_chunk_and_embed_source` 约定与
# test_knowhow_citation.py 的 `_spy_on_evidence_elements`。
# ---------------------------------------------------------------------------

UNIQUE_TERM = "TSIMG9001"
FIGURE_CAPTION = f"图 2 {UNIQUE_TERM} 时钟树收敛示意"
ASSET_ID = "asset-fig-2"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # 清空开发者本机可能导出的真实 key：离线、零网络。
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repository = SQLiteRepository(Settings(), model_provider=RecordingModelProvider())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return repository


def _seed_source_with_a_captioned_figure(repo) -> tuple[str, str]:
    """一段正文 + 一张带图注的配图，落进**同一个** chunk。

    这是真实 markdown/MinerU 摄取的形状：`build_chunks` 只跳过无图注的 image，
    带图注的图以图注进检索、element id 进该 chunk 的 `element_ids`。这里用真实
    分块而不是手写 chunks 行，正是为了让「图注命中 → 该 chunk → 它的配图」这条
    链路整条被覆盖。
    """
    notebook = repo.create_notebook(NotebookCreate(name="nb"))
    source_id = "src-fig"
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,parse_status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook.id, "时序手册", "document", "timing.md",
             "/tmp/timing.md", 0, "h", "", "", "extracted", now, now),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"el-{source_id}-0001", source_id, "paragraph", "p1",
             f"{UNIQUE_TERM} 收敛需要在综合阶段预留时序裕量。", "{}", now),
        )
        db.execute(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"el-{source_id}-0002", source_id, "image", "Markdown image 2",
             FIGURE_CAPTION,
             json.dumps({"asset_id": ASSET_ID, "caption": FIGURE_CAPTION},
                        ensure_ascii=False), now),
        )
    repo._chunk_and_embed_source(source_id)
    return notebook.id, source_id


def _spy_on_evidence_elements(repo, monkeypatch) -> list:
    """包住**真实** SourceStore.evidence_elements，观察生产接线本身。"""
    calls: list[list[str]] = []
    original = repo._runtime.source_store.evidence_elements

    def _spy(element_ids):
        ids = list(element_ids)
        calls.append(ids)
        return original(ids)

    monkeypatch.setattr(repo._runtime.source_store, "evidence_elements", _spy)
    return calls


def test_the_captioned_figure_really_lands_in_the_chunks_element_ids(repo):
    """本特性取图路径的前提事实（不是本次改动引入的，但整条链靠它成立）。"""
    _notebook_id, source_id = _seed_source_with_a_captioned_figure(repo)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT element_ids FROM chunks WHERE source_id=?", (source_id,)
        ).fetchall()
    element_ids = {eid for row in rows for eid in json.loads(row["element_ids"])}
    assert f"el-{source_id}-0002" in element_ids


def test_ask_chunk_citation_carries_the_section_figure(repo, monkeypatch):
    """chunk 模式（默认模式）的引用回退列表：无 LLM 的确定性路径，anchors 恒空，
    每个精选 chunk 一条引用——引用必须带出该 chunk 里那张配图。"""
    notebook_id, source_id = _seed_source_with_a_captioned_figure(repo)
    calls = _spy_on_evidence_elements(repo, monkeypatch)

    response = repo.ask_chunk(notebook_id, AskRequest(question=UNIQUE_TERM))

    assert response.citations, "ask_chunk 未产出引用"
    with_images = [c for c in response.citations if c.images]
    assert with_images, [(c.element_id, c.images) for c in response.citations]
    image = with_images[0].images[0]
    assert image.asset_id == ASSET_ID
    assert image.caption == FIGURE_CAPTION
    assert image.element_id == f"el-{source_id}-0002"
    # JSON 形状（前端真正收到的东西）。
    assert with_images[0].model_dump(mode="json")["images"][0]["asset_id"] == ASSET_ID
    # 批量口径：knowhow 定位一次 + 附图一次，与引用/锚点数量无关。
    assert len(calls) == 2, calls


class _ChunkAnswerLLM:
    """grounded 主路径：读上下文块，把每条含 UNIQUE_TERM 的 k 行都 `[k]` 标出来,
    让 chunk 锚点真的解析出来（前端 buildAnswerReferences 是 anchor 优先的全有
    全无，锚点这条腿才是主路径）。纯字符串解析，不触网。"""

    configured = True
    model = "fake-chunk-answer-llm"

    def chat_json(self, messages, schema_hint, **kwargs):
        text = messages[0]["content"]
        keys = []
        for line in text.splitlines():
            head, sep, _rest = line.partition(":")
            if sep and head.startswith("k") and head[1:].isdigit() and UNIQUE_TERM in line:
                keys.append(head)
        markers = "".join(f"[{key}]" for key in keys)
        return json.dumps(
            {"answer": f"时序收敛见 {markers}。", "grounded": True}, ensure_ascii=False,
        )


def test_grounded_chunk_answer_puts_the_figure_on_the_chunk_anchor(repo, monkeypatch):
    """锚点腿：答案带 `[k]` 时前端整体走 anchor 分支，citation.images 被遮蔽——
    附图必须出现在 chunk 型 **锚点** 上，否则最主流的问答形态里一张图都不出现。
    这正是「字段必须同时加在 AnswerAnchor 上」那条设计裁决的钉子测试。"""
    notebook_id, source_id = _seed_source_with_a_captioned_figure(repo)
    bind_chat_client(repo, "ask_answer", _ChunkAnswerLLM())

    response = repo.ask_chunk(notebook_id, AskRequest(question=UNIQUE_TERM))

    assert response.answer, (response.conclusion, response.model_errors)
    chunk_anchors = [a for a in response.anchors if a.object_type == "chunk"]
    assert chunk_anchors, (response.answer, response.anchors)
    with_images = [a for a in chunk_anchors if a.images]
    assert with_images, [(a.object_id, a.element_id) for a in chunk_anchors]
    assert with_images[0].images[0].asset_id == ASSET_ID
    assert with_images[0].images[0].element_id == f"el-{source_id}-0002"
    assert with_images[0].model_dump(mode="json")["images"][0]["caption"] == FIGURE_CAPTION


class _ReasoningLLM:
    """reasoning 三段（规划 / 反思 / 合成）的最小 stub，镜像
    test_reasoning_ask.py 的 `_SeqLLM`。合成半复用 `_ChunkAnswerLLM` 的
    「把含 UNIQUE_TERM 的 k 行全部标出来」策略，好让 chunk 锚点真的绑定。"""

    configured = True
    model = "fake-reasoning-llm"

    def __init__(self) -> None:
        self._answer = _ChunkAnswerLLM()

    def chat_json(self, messages, schema_hint, **kwargs):
        if "sub_queries" in (schema_hint or ""):
            return json.dumps({"sub_queries": [{"query": UNIQUE_TERM}]})
        if "next_action" in (schema_hint or ""):
            return json.dumps({"next_action": "answer", "sufficient": True})
        return self._answer.chat_json(messages, schema_hint, **kwargs)


def test_reasoning_chunk_anchor_carries_the_section_figure(repo, monkeypatch):
    """reasoning 模式的 chunk 锚点同样富化——它与 chunk 模式是两个独立的装配点，
    共用同一个 `anchor_image_targets` 规则（chunk 锚点按 object_id 反查）。"""
    notebook_id, source_id = _seed_source_with_a_captioned_figure(repo)
    client = _ReasoningLLM()
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload, client)

    response = repo.ask(
        notebook_id, AskRequest(question=UNIQUE_TERM, mode="reasoning"),
    )

    chunk_anchors = [a for a in response.anchors if a.object_type == "chunk"]
    assert chunk_anchors, (response.answer, response.anchors)
    with_images = [a for a in chunk_anchors if a.images]
    assert with_images, [(a.object_id, a.element_id) for a in chunk_anchors]
    assert with_images[0].images[0].asset_id == ASSET_ID
    assert with_images[0].images[0].element_id == f"el-{source_id}-0002"
