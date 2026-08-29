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
    CITATION_IMAGE_CAPTION_CHARS,
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
    """记录每次批量调用，让「只查一次」成为主动断言而不是假设。

    `image_asset_rows` 是本特性专用的**窄**读取（`SourceStorePort`）：两个谓词
    (id 集合 + ``element_type='image'``)都在 SQL 里，只回 ``(id, metadata)``、
    不带 ``text``。这个 fake 照着 SQL 的形状过滤，所以「非图片元素不进结果」在
    单测层是**建模**；真 SQL 确实这么过滤由 `test_image_asset_rows_*` 那两条
    store 级用例证明。

    `evidence_elements`（宽读）在这里**直接报错**：附图路径复用它就是把每条被引
    chunk 的全部元素正文拖过网（40 节 MinerU PDF 实测 2750 KiB、最终 0 张图），
    这是本文件要钉死的性能契约，退回去必须报红而不是悄悄变慢。
    """

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
    # 任何顺手带了这个键的元素（例如未来某种附件形态）被当成配图渲染。前半条
    # 现在下推在 SQL 里（fake 照 SQL 形状建模），真 SQL 见 store 级用例。
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


def test_image_description_stands_in_when_there_is_no_alt_caption():
    # markdown 的 `> **图片描述**` 引用块：没有 alt 的图正是靠它进的检索，命中
    # 之后附图旁边只有它能当说明。这不是回退到元素 text（那是占位定位串），而是
    # 用户为这张图写下的描述。
    sources = _SpySources({
        "el-img": _row("image", {"asset_id": "asset-2", "description": "三级流水线示意"}),
    })
    images = _service(sources).citation_images_for(["el-img"])

    assert images["el-img"].caption == "三级流水线示意"


def test_an_alt_caption_wins_over_the_description():
    sources = _SpySources({
        "el-img": _row("image", {"asset_id": "asset-2", "caption": "图 1",
                                 "description": "长长的描述正文"}),
    })
    images = _service(sources).citation_images_for(["el-img"])

    assert images["el-img"].caption == "图 1"


def test_a_runaway_description_is_truncated_to_the_same_named_cap():
    long_description = "描" * (CITATION_IMAGE_CAPTION_CHARS + 50)
    sources = _SpySources({
        "el-img": _row("image", {"asset_id": "asset-1", "description": long_description}),
    })

    images = _service(sources).citation_images_for(["el-img"])

    assert len(images["el-img"].caption) == CITATION_IMAGE_CAPTION_CHARS


def test_a_runaway_caption_is_truncated_to_the_named_cap():
    # `caption` 是本 payload 唯一的自由文本；兄弟字段各有上界（Citation
    # .quoted_span 200 / 枚举行 summary 300），少一个上界就够让一份图注畸长的
    # 文档把响应撑大。具名常量而不是裸切片（「数值上限与截断」红线）。
    long_caption = "图" * (CITATION_IMAGE_CAPTION_CHARS + 50)
    sources = _SpySources({"el-img": _image_row("asset-1", long_caption)})

    images = _service(sources).citation_images_for(["el-img"])

    assert len(images["el-img"].caption) == CITATION_IMAGE_CAPTION_CHARS
    assert images["el-img"].caption == long_caption[:CITATION_IMAGE_CAPTION_CHARS]


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


def _spy_on_image_asset_rows(repo, monkeypatch) -> list:
    """包住**真实** SourceStore.image_asset_rows，观察生产接线本身。

    钉的是附图这一路的调用次数：把锚点与引用拆成两次 `attach_citation_images`
    会让一次回答拿到两份 `CITATION_IMAGES_PER_ANSWER` 预算，那个上限就不再是
    上限——这个 spy 是唯一能让那种拆分报红的东西。
    """
    calls: list[list[str]] = []
    original = repo._runtime.source_store.image_asset_rows

    def _spy(element_ids):
        ids = list(element_ids)
        calls.append(ids)
        return original(ids)

    monkeypatch.setattr(repo._runtime.source_store, "image_asset_rows", _spy)
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


def test_image_asset_rows_filters_to_images_in_sql_and_never_selects_text(repo):
    """store 级：混合 id 请求只回 image 行，且行形状里根本没有 ``text``。

    单测层的 fake 是照这个形状**建模**的；这条用例是真 SQL 的证明。两点都是
    性能契约而不是整洁性：``element_type`` 过滤留在 Python 里意味着每条被引
    chunk 的全部元素**正文**都要过一遍网络（实测 2750 KiB / 次），而这条路径
    从头到尾没有一个消费者读 ``text``。
    """
    _notebook_id, source_id = _seed_source_with_a_captioned_figure(repo)
    paragraph_id = f"el-{source_id}-0001"
    image_id = f"el-{source_id}-0002"

    rows = repo._runtime.source_store.image_asset_rows(
        [paragraph_id, image_id, "el-absent", "", image_id]
    )

    assert [element_id for element_id, _metadata in rows] == [image_id]
    assert json.loads(rows[0][1])["asset_id"] == ASSET_ID
    # 行形状：恰好 (id, metadata) 两项——多一列就是又把正文拖回来了。
    assert len(rows[0]) == 2
    # 宽读同一批 id 会返回段落行（并带 text），这正是本方法要避开的那个形状。
    wide = repo._runtime.source_store.evidence_elements([paragraph_id, image_id])
    assert set(wide) == {paragraph_id, image_id}
    assert "text" in wide[image_id]


def test_image_asset_rows_returns_nothing_for_an_all_falsy_request(repo):
    _notebook_id, _source_id = _seed_source_with_a_captioned_figure(repo)
    assert repo._runtime.source_store.image_asset_rows(["", ""]) == []


def test_ask_chunk_citation_carries_the_section_figure(repo, monkeypatch):
    """chunk 模式（默认模式）的引用回退列表：无 LLM 的确定性路径，anchors 恒空，
    每个精选 chunk 一条引用——引用必须带出该 chunk 里那张配图。"""
    notebook_id, source_id = _seed_source_with_a_captioned_figure(repo)
    calls = _spy_on_image_asset_rows(repo, monkeypatch)

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
    # 批量口径：附图恰好一次，与引用条数无关。这条路径 anchors 恒空，所以它抓
    # 不到「锚点腿与引用腿拆成两次调用」那种变异（空锚点那次会 early-return）
    # ——那条由下面 grounded 用例的同名断言钉住。
    assert len(calls) == 1, calls


def test_ask_chunk_answer_survives_an_image_store_read_failure(repo, monkeypatch):
    """评审 F1：附图是已生成回答上的最后一步装饰性富化，一次 DB 抖动不得废掉
    整条已经算完的回答——镜像 `test_report_reference_images.py` 的同型用例
    `test_assemble_completes_even_when_the_image_store_read_fails`（同一套
    fail-open 惯例，`attach_citation_images` 内部 try/except 见
    `evidence_context.py`）。"""
    notebook_id, _source_id = _seed_source_with_a_captioned_figure(repo)

    def _boom(element_ids):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(repo._runtime.source_store, "image_asset_rows", _boom)

    response = repo.ask_chunk(notebook_id, AskRequest(question=UNIQUE_TERM))

    # 回答没有因为附图这一步炸了而丢失。
    assert response.citations, "ask_chunk 未产出引用"
    assert all(c.images == [] for c in response.citations)


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
    calls = _spy_on_image_asset_rows(repo, monkeypatch)

    response = repo.ask_chunk(notebook_id, AskRequest(question=UNIQUE_TERM))

    assert response.answer, (response.conclusion, response.model_errors)
    chunk_anchors = [a for a in response.anchors if a.object_type == "chunk"]
    assert chunk_anchors, (response.answer, response.anchors)
    with_images = [a for a in chunk_anchors if a.images]
    assert with_images, [(a.object_id, a.element_id) for a in chunk_anchors]
    assert with_images[0].images[0].asset_id == ASSET_ID
    assert with_images[0].images[0].element_id == f"el-{source_id}-0002"
    assert with_images[0].model_dump(mode="json")["images"][0]["caption"] == FIGURE_CAPTION
    # 单次调用口径,在**锚点与引用都非空**的形态上钉：把 ask_chunk 那一处
    # `attach_citation_images` 拆成「先锚点、后引用」两次会让一次回答拿到两份
    # `CITATION_IMAGES_PER_ANSWER` 预算——只有这里能让那个变异报红。
    assert response.citations, "grounded 路径仍应产出引用回退列表"
    assert len(calls) == 1, calls


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


def _seed_kg_concept(repo, notebook_id: str, source_id: str) -> None:
    """一个指向该来源正文元素的 concept 知识对象。

    只为让 reasoning 装配点的**引用腿**非空：reasoning 的 `citations` 只由 KG
    命中（`citations_from`）/ 记忆 / element 锚点 / 集合行产生，纯 chunk 命中不
    产生引用。没有它，「锚点腿与引用腿拆成两次调用」的变异在这个装配点抓不到
    ——空的那次会 early-return，调用数仍是 1。轻量 raw-SQL 造数，镜像
    test_knowhow_citation.py 的同款做法，不经完整 KG 抽取管线。
    """
    now = "2026-01-01T00:00:00"
    evidence = json.dumps([{
        "source_id": source_id, "source_title": "时序手册",
        "element_id": f"el-{source_id}-0001", "element_type": "paragraph",
        "location_label": "p1", "quoted_span": f"{UNIQUE_TERM} 收敛",
        "confidence": 1.0,
    }], ensure_ascii=False)
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ko-fig", notebook_id, "concept", "approved", "",
             json.dumps({"name": f"{UNIQUE_TERM} 时序收敛"}, ensure_ascii=False),
             evidence, source_id, now, now),
        )
    # 造完行还得给它向量，否则 ANN 检索根本看不到它（raw-SQL 插入不经抽取管线的
    # embed 步）。
    repo._embed_objects_batch(notebook_id, [{
        "_oid": "ko-fig", "payload": {"name": f"{UNIQUE_TERM} 时序收敛"},
    }])


def test_reasoning_chunk_anchor_carries_the_section_figure(repo, monkeypatch):
    """reasoning 模式的 chunk 锚点同样富化——它与 chunk 模式是两个独立的装配点，
    共用同一个 `anchor_image_targets` 规则（chunk 锚点按 object_id 反查）。"""
    notebook_id, source_id = _seed_source_with_a_captioned_figure(repo)
    _seed_kg_concept(repo, notebook_id, source_id)
    client = _ReasoningLLM()
    for workload in ("reasoning_agent", "evidence_refine", "ask_answer"):
        bind_chat_client(repo, workload, client)
    calls = _spy_on_image_asset_rows(repo, monkeypatch)

    response = repo.ask(
        notebook_id, AskRequest(question=UNIQUE_TERM, mode="reasoning"),
    )

    chunk_anchors = [a for a in response.anchors if a.object_type == "chunk"]
    assert chunk_anchors, (response.answer, response.anchors)
    with_images = [a for a in chunk_anchors if a.images]
    assert with_images, [(a.object_id, a.element_id) for a in chunk_anchors]
    assert with_images[0].images[0].asset_id == ASSET_ID
    assert with_images[0].images[0].element_id == f"el-{source_id}-0002"
    # 单次调用口径（reasoning 装配点自己的那份）：这里锚点与引用都非空，拆成两次
    # 调用即预算翻倍，必须报红。
    assert response.citations, "reasoning 路径仍应产出引用回退列表"
    assert len(calls) == 1, calls

# graph 模式（PPR 分支 + src_chunks/mix 分支，后者镜像见 test_knowhow_citation.py）
# 曾各有一个装配点钉子测试，已随该 ask 模式退役一并删除。现存的两个装配点
# （ask_chunk、ask_reasoning，测试见上文）覆盖了退役后仍然存在的全部生产站点。
