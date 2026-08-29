"""Task 12（引用跳转，后端富化）：`EvidenceContextService.citations_from` 命中
knowhow 格子的引用时，批量按 element_id 查 `source_elements.metadata.knowhow`，
填充 `Citation.knowhow = {table_id, row_id}`；非 knowhow 引用该字段保持 None
（且从 JSON 输出中整体缺席，与既有 `memory_id`/`provenance` 的 `exclude_if`
风格一致）。批量查询无论命中多少条引用都只发生一次——这是运行效率的硬约束
（见仓库 memory「运行效率是一等约束」），不能退化成逐条引用各查一次。

Task 12b（引用跳转扩面，本文件的第二个阶段）：T12 的评审裁定「引用跳转」按钮
在默认 chunk 模式下结构性地从不出现——`citations_from` 只被 reasoning 模式
调用，chunk 模式的两处内联 `Citation(...)` 构造点（ask_service.py 的
mix/plain chunk 分支；graph 模式当时另有 PPR/mix 两处，已随该 ask 模式退役
一并删除）此前从未查过 knowhow。本文件扩面覆盖两条腿：
  1. Citation 侧：新抽出的 `EvidenceContextService.knowhow_refs_for()`——
     `citations_from` 与四个内联构造点共用的批量查询，仍是「不管命中多少条
     引用只查一次」。
  2. Anchor 侧：`knowledge_context`/`parse_anchors` 从 KO payload 的
     `table_id`/`rows` 算出 `AnswerAnchor.knowhow`——这是 reasoning 模式
     `[k]` 标记命中时的主路径（`buildAnswerReferences` 优先展示 anchor，
     citation 只是没命中标记时的回退列表）。合并行规则（controller 决策）：
     只有 `len(rows) == 1` 才有唯一无歧义的行，多行合并的 KO 留 None。

本文件前半段是纯单元测试（假 sources/notebooks/knowledge 协作者，无 DB/HTTP），
镜像 `test_evidence_context_service.py` 的既有测试风格，但补充了一个
「记录每次批量调用参数」的 spy fake（`_SpySources`），专门用来断言「只查一次」
这一条 TDD 要求——这是既有 `test_evidence_context_service.py` 里那个静默返回
`{}` 的 `_Sources` fake做不到的。后半段（T12b 新增）补真实 SQLite 集成
测试，证明 ask_chunk 这条此前完全没测过 knowhow 富化的路径真的接上了——
镜像 `test_knowhow_projection.py`（chunk 侧：repo/embedder/projector直接建表
投影，不经 HTTP/轮询）的既有约定。graph 侧的镜像集成测试（曾用
`test_graph_src_chunks.py` 同款 raw-SQL 造数手法）已随该 ask 模式退役
一并删除。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, Evidence, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.evidence_context import EvidenceContextService
from app.services.knowhow.projection import KnowhowProjector
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository
from tests.model_testkit import bind_all_embedding_clients
from tests.model_testkit import bind_chat_client


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
    """Records every batch call so tests can assert exactly ONE round trip
    backs however many citations resolve to knowhow cells."""

    def __init__(self, elements: dict[str, dict]) -> None:
        self._elements = elements
        self.calls: list[list[str]] = []

    def evidence_elements(self, element_ids):
        ids = list(element_ids)
        self.calls.append(ids)
        return {eid: self._elements[eid] for eid in ids if eid in self._elements}

    def source_metadata(self, source_ids):
        return {}


def _element_row(metadata: dict) -> dict:
    return {"metadata": json.dumps(metadata, ensure_ascii=False)}


def _service(sources) -> EvidenceContextService:
    return EvidenceContextService(
        notebooks=_Notebooks(), sources=sources, knowledge=_Knowledge(), settings=Settings(),
    )


def _evidence(element_id: str, source_id: str = "src-1") -> Evidence:
    return Evidence(
        source_id=source_id, source_title="Doc", element_id=element_id,
        element_type="knowhow_cell", location_label="loc", quoted_span="span",
        confidence=1.0,
    )


def _hit(*evidences: Evidence, tier: str = "personal") -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id="o1", object_type="claim", payload={}, evidence=list(evidences), tier=tier,
    )


def test_knowhow_cell_citation_carries_table_and_row_locator():
    sources = _SpySources({
        "el-cell": _element_row({
            "knowhow": {"table_id": "tbl-1", "row_id": "row-1", "column_id": "c1"},
        }),
    })
    citations = _service(sources).citations_from(
        [_hit(_evidence("el-cell"))], {"el-cell"}, "KG evidence", notebook_id="nb-1",
    )
    assert len(citations) == 1
    assert citations[0].knowhow is not None
    assert citations[0].knowhow.table_id == "tbl-1"
    assert citations[0].knowhow.row_id == "row-1"


def test_non_knowhow_citation_has_no_knowhow_field_and_is_excluded_from_json():
    sources = _SpySources({"el-plain": _element_row({})})
    citations = _service(sources).citations_from(
        [_hit(_evidence("el-plain"))], {"el-plain"}, "KG evidence", notebook_id="nb-1",
    )
    assert citations[0].knowhow is None
    assert "knowhow" not in citations[0].model_dump(mode="json")


def test_element_missing_from_batch_result_has_no_knowhow_field():
    # Element absent from the batch result entirely (e.g. deleted between
    # retrieval and citation-building) must resolve to None, never KeyError.
    sources = _SpySources({})
    citations = _service(sources).citations_from(
        [_hit(_evidence("el-missing"))], {"el-missing"}, "KG evidence", notebook_id="nb-1",
    )
    assert citations[0].knowhow is None


def test_citations_from_batches_every_lookup_into_one_store_call():
    sources = _SpySources({
        "el-a": _element_row({"knowhow": {"table_id": "t1", "row_id": "r1"}}),
        "el-b": _element_row({}),
    })
    # el-a repeated on purpose: the same element can legitimately back two
    # separate evidence entries (e.g. two hits citing the same cell).
    hit = _hit(_evidence("el-a"), _evidence("el-b"), _evidence("el-a"))
    citations = _service(sources).citations_from(
        [hit], {"el-a", "el-b"}, "KG evidence", notebook_id="nb-1")

    assert len(citations) == 3
    assert len(sources.calls) == 1
    assert sorted(sources.calls[0]) == ["el-a", "el-b"]


def test_mixed_hit_list_only_flags_the_knowhow_ones():
    sources = _SpySources({
        "el-cell": _element_row({"knowhow": {"table_id": "tbl-9", "row_id": "row-9"}}),
        "el-plain": _element_row({}),
    })
    hits = [
        _hit(_evidence("el-cell"), tier="base"),
        _hit(_evidence("el-plain"), tier="personal"),
    ]
    citations = _service(sources).citations_from(
        hits, {"el-cell", "el-plain"}, "KG evidence", notebook_id="nb-1")

    by_element = {citation.element_id: citation for citation in citations}
    assert by_element["el-cell"].knowhow is not None
    assert by_element["el-cell"].knowhow.table_id == "tbl-9"
    assert by_element["el-cell"].knowhow.row_id == "row-9"
    assert by_element["el-plain"].knowhow is None
    assert len(sources.calls) == 1


def test_citations_from_skips_the_store_call_entirely_when_no_element_ids_present():
    # Memory-style evidence with an empty element_id must never trigger a
    # metadata lookup at all (matches the repo's "no incidental calls" bar).
    sources = _SpySources({})
    citations = _service(sources).citations_from(
        [_hit(_evidence(""))], set(), "KG evidence", notebook_id="nb-1",
    )
    assert citations[0].knowhow is None
    assert sources.calls == []


# ---------------------------------------------------------------------------
# Task 12b — Citation side: the new shared `knowhow_refs_for` batching helper,
# tested directly (not just indirectly through `citations_from`) since it is
# now also called from the four ask_service.py inline Citation(...) sites.
# ---------------------------------------------------------------------------


def test_knowhow_refs_for_batches_every_lookup_into_one_store_call():
    sources = _SpySources({
        "el-a": _element_row({"knowhow": {"table_id": "t1", "row_id": "r1"}}),
        "el-b": _element_row({}),
    })
    # "el-a" repeated + a blank id thrown in on purpose: this mirrors how the
    # ask_service.py call sites build the input (one `element_ids[0] if
    # element_ids else ""` per chunk, no pre-filtering by the caller).
    refs = _service(sources).knowhow_refs_for(["el-a", "el-b", "el-a", ""])

    assert set(refs) == {"el-a"}
    assert refs["el-a"].table_id == "t1"
    assert refs["el-a"].row_id == "r1"
    assert len(sources.calls) == 1
    assert sorted(sources.calls[0]) == ["el-a", "el-b"]


def test_knowhow_refs_for_returns_empty_dict_and_skips_the_store_call_when_every_id_is_falsy():
    sources = _SpySources({})
    refs = _service(sources).knowhow_refs_for(["", ""])
    assert refs == {}
    assert sources.calls == []


def test_knowhow_refs_for_omits_unresolved_ids_from_the_returned_mapping():
    # An id present in the batch call but absent from the store's result (or
    # resolving to a non-knowhow element) must simply be missing from the
    # returned dict — callers read it back with `.get(eid)`, never `[eid]`.
    sources = _SpySources({"el-plain": _element_row({})})
    refs = _service(sources).knowhow_refs_for(["el-plain", "el-missing"])
    assert refs == {}
    assert sources.calls == [["el-plain", "el-missing"]]


# ---------------------------------------------------------------------------
# Task 12b — Anchor side: `knowledge_context`/`parse_anchors` populate
# `AnswerAnchor.knowhow` straight from the KO payload's `table_id`/`rows`
# (projection.py's `_ko_object_row`, §④ payload shape) — zero extra queries,
# same "already in memory" pattern as `hit.payload.get("name", "")`. This is
# the reasoning-mode [k]-marker path, which `buildAnswerReferences` prefers
# over the citation fallback list whenever the answer actually cites a [k].
# ---------------------------------------------------------------------------


def _knowledge_service() -> EvidenceContextService:
    # sources/evidence_elements is never consulted by knowledge_context/
    # parse_anchors (the KO payload is already in memory) — a Sources fake
    # that would blow up if called makes that invariant an active check,
    # not just an assumption.
    class _UnusedSources:
        def evidence_elements(self, element_ids):
            raise AssertionError("knowledge_context must not query evidence_elements")

        def source_metadata(self, source_ids):
            raise AssertionError("knowledge_context must not query source_metadata")

    return EvidenceContextService(
        notebooks=_Notebooks(), sources=_UnusedSources(), knowledge=_Knowledge(),
        settings=Settings(),
    )


def _knowhow_hit(object_id: str, *, table_id: str, rows: list[str], object_type="修复方法") -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id=object_id, object_type=object_type,
        payload={
            "name": "增大去耦电容", "text": "增大去耦电容", "table_id": table_id,
            "rows": rows, "column_id": "col-fix", "column_name": object_type,
        },
        evidence=[], tier="personal",
    )


def test_anchor_carries_knowhow_when_ko_payload_has_exactly_one_row():
    hit = _knowhow_hit("ko-1", table_id="tbl-1", rows=["row-1"])
    service = _knowledge_service()
    _block, evidence_by_id = service.knowledge_context("nb-1", [hit])

    assert evidence_by_id["k1"]["knowhow"] is not None
    anchors = service.parse_anchors("结论见 [k1]。", evidence_by_id)

    assert len(anchors) == 1
    assert anchors[0].knowhow is not None
    assert anchors[0].knowhow.table_id == "tbl-1"
    assert anchors[0].knowhow.row_id == "row-1"
    assert anchors[0].model_dump(mode="json")["knowhow"] == {"table_id": "tbl-1", "row_id": "row-1"}


def test_anchor_has_no_knowhow_when_ko_payload_rows_are_merged_across_multiple_rows():
    # Merged-rows rule (controller decision): a KO whose cell value is shared
    # by 2+ rows (design doc §④ "同列同值跨行归并") has no single unambiguous
    # row to jump to — leave knowhow None rather than guessing the first one.
    hit = _knowhow_hit("ko-2", table_id="tbl-1", rows=["row-1", "row-3"])
    service = _knowledge_service()
    _block, evidence_by_id = service.knowledge_context("nb-1", [hit])
    anchors = service.parse_anchors("结论见 [k1]。", evidence_by_id)

    assert len(anchors) == 1
    assert anchors[0].knowhow is None
    assert "knowhow" not in anchors[0].model_dump(mode="json")


def test_anchor_has_no_knowhow_for_an_ordinary_non_knowhow_kg_object():
    # An everyday KG concept/claim payload (no table_id/rows at all) must
    # resolve to None just as safely as the merged-row case — never a
    # KeyError/AttributeError from a payload that was never knowhow-shaped.
    hit = RetrievedKnowledge(
        object_id="ko-3", object_type="concept", payload={"name": "Cascode"},
        evidence=[], tier="personal",
    )
    service = _knowledge_service()
    _block, evidence_by_id = service.knowledge_context("nb-1", [hit])
    anchors = service.parse_anchors("结论见 [k1]。", evidence_by_id)

    assert anchors[0].knowhow is None


def test_anchor_has_no_knowhow_when_rows_key_is_missing_or_not_a_list():
    # Defensive coverage for the two other falsy/malformed shapes the "rows"
    # key could legally take (absent entirely, or present but not a list) —
    # both must resolve to None, never raise.
    for bad_payload in (
        {"name": "x", "table_id": "tbl-1"},               # rows absent
        {"name": "x", "table_id": "tbl-1", "rows": "row-1"},  # rows not a list
        {"name": "x", "table_id": "tbl-1", "rows": []},    # rows empty
    ):
        hit = RetrievedKnowledge(
            object_id="ko-x", object_type="attribute", payload=bad_payload,
            evidence=[], tier="personal",
        )
        service = _knowledge_service()
        _block, evidence_by_id = service.knowledge_context("nb-1", [hit])
        assert evidence_by_id["k1"]["knowhow"] is None, bad_payload


# ---------------------------------------------------------------------------
# Task 12b — end-to-end: real SQLite, driving ask_chunk directly, proving the
# two production Citation(...) sites actually wire the new batching helper in
# (the unit tests above only prove the helper itself is correct in
# isolation). Mirrors test_knowhow_projection.py's repo/embedder/projector
# fixture convention for the chunk-mode table (no HTTP, no background-job
# polling — `project_table` runs synchronously when called directly).
# The graph-mode mirror (two more sites, raw-SQL seeded like
# test_graph_src_chunks.py) was retired along with that ask mode.
# ---------------------------------------------------------------------------

TABLE_TITLE = "时序修复表"
UNIQUE_TERM = "TSFIX7788"
COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "修复方法", "role": "procedure"},
    {"name": "依赖工具", "role": "entity"},
]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    # Mirrors test_knowhow_retrieval.py's `client` fixture: blank any real LLM
    # keys a developer's shell might export, so ask_chunk's no-LLM
    # deterministic fallback path is dependable everywhere, not just in CI.
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    assert r.configured("knowhow_embedding")
    return r


@pytest.fixture
def projector(repo) -> KnowhowProjector:
    rt = repo._runtime
    return KnowhowProjector(
        settings=repo.settings,
        database=rt.database,
        knowhow=rt.knowhow_store,
        sources=rt.source_store,
        chunks=rt.chunk_store,
        knowledge=rt.knowledge,
        embedding=rt.source_embedding,
        note_model_error=rt.models.note_model_error,
        invalidate_unified_cache=rt.kg_mutations.invalidate_unified_cache,
        mark_unified_dirty=rt.kg_mutations.mark_unified_kg_dirty,
        new_id=rt.seams.new_id,
        now=rt.seams.now,
    )


def _spy_on_evidence_elements(repo, monkeypatch) -> list:
    """Wraps the REAL SourceStore.evidence_elements with a call-count spy,
    installed on the app's own repo instance so ask_chunk's actual
    production wiring is what gets observed (not a fake substituted in)."""
    calls: list[list[str]] = []
    original = repo._runtime.source_store.evidence_elements

    def _spy(element_ids):
        ids = list(element_ids)
        calls.append(ids)
        return original(ids)

    monkeypatch.setattr(repo._runtime.source_store, "evidence_elements", _spy)
    return calls


def _seed_projected_table(repo, projector) -> dict:
    """Create + project the shared 3-row fixture table. UNIQUE_TERM
    deliberately appears in TWO different rows' 修复方法 cell — so one ask
    hits two distinct element_ids/citations/anchors, proving the batch
    lookups cover N>1 hits in a single store round trip each."""
    notebook_id = repo.create_notebook(NotebookCreate(name="kh")).id
    store = repo._runtime.knowhow_store
    table_id = store.create_knowhow_table(notebook_id, TABLE_TITLE, "", COLUMNS)
    cols = {
        c["name"]: c["id"]
        for c in store.get_knowhow_table(table_id)["columns"]
    }
    row_a = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "过冲问题", cols["修复方法"]: f"增大去耦电容 {UNIQUE_TERM}",
        cols["依赖工具"]: "示波器",
    })
    store.add_knowhow_row(table_id, {
        cols["违例类型"]: "欠冲问题", cols["修复方法"]: "调整走线拓扑降低寄生电感",
        cols["依赖工具"]: "万用表",
    })
    row_c = store.add_knowhow_row(table_id, {
        cols["违例类型"]: "抖动问题", cols["修复方法"]: f"更换低噪声时钟源芯片 {UNIQUE_TERM}",
        cols["依赖工具"]: "示波器",
    })
    projector.project_table(table_id)
    return {
        "notebook_id": notebook_id, "table_id": table_id,
        "row_a": row_a, "row_c": row_c,
    }


def test_ask_chunk_citations_carry_knowhow_and_the_batch_query_still_fires_once(
    repo, projector, monkeypatch,
):
    """T12b 主场景：chunk 模式（默认模式，brief 明确指出此前从未富化过）对一张
    projected knowhow 表提问，返回的 citations 应带上 .knowhow；且不管这次
    命中几条引用，knowhow 定位只应触发一次 evidence_elements() 批量读取。
    本测试无 LLM（deterministic 回退，anchors 恒空）——这是 citation 回退
    列表这条腿；grounded 主路径（anchor 腿）见下一个测试。"""
    seeded = _seed_projected_table(repo, projector)
    calls = _spy_on_evidence_elements(repo, monkeypatch)

    resp = repo.ask_chunk(seeded["notebook_id"], AskRequest(question=UNIQUE_TERM))

    assert resp.citations, "ask_chunk 未产出引用"
    # Every citation in this fixture comes from a knowhow-projected cell (the
    # notebook has no other source), so every one of them should resolve.
    assert all(c.knowhow is not None for c in resp.citations), [
        (c.element_id, c.knowhow) for c in resp.citations
    ]
    term_citations = [c for c in resp.citations if UNIQUE_TERM in c.quoted_span]
    assert len(term_citations) >= 2, [c.quoted_span for c in resp.citations]
    assert {c.knowhow.row_id for c in term_citations} == {seeded["row_a"], seeded["row_c"]}
    assert all(c.knowhow.table_id == seeded["table_id"] for c in term_citations)
    # 无 LLM → 答案合成不跑 → chunk_context（anchor 侧批量）不触发，只剩
    # citation 侧的一次批量。这个数必须与引用条数无关——它钉的是「批量」本身，
    # 本 fixture 命中两条以上引用正是为了让退化成逐条时报红。（T1 的附图走窄
    # 读 `image_asset_rows`，不经这个 spy；它自己的调用计数在
    # test_citation_images.py。）
    assert len(calls) == 1, calls


class _ChunkAnswerLLM:
    """Grounded-path answer stub for ask_chunk: reads the context block and
    cites EVERY k-line that contains UNIQUE_TERM with a [k] marker — so
    anchors RESOLVE, and (per buildAnswerReferences' anchor-first
    all-or-nothing precedence) the citation fallback list is shadowed in the
    UI. String-parsing only (no re import — keeps this file's import block,
    and the surface manifest's exact-line registration of line 45, frozen)."""
    configured = True
    model = "fake-chunk-answer-llm"

    def chat_json(self, messages, schema_hint, **kw):
        text = messages[0]["content"]
        keys = []
        for line in text.splitlines():
            head, sep, _rest = line.partition(":")
            if sep and head.startswith("k") and head[1:].isdigit() and UNIQUE_TERM in line:
                keys.append(head)
        markers = "".join(f"[{k}]" for k in keys)
        return json.dumps(
            {"answer": f"修复方法见 {markers}。", "grounded": True},
            ensure_ascii=False,
        )


def test_grounded_chunk_answer_puts_knowhow_on_the_chunk_anchor(
    repo, projector, monkeypatch,
):
    """T12b 评审修复的钉子测试（评审原话 "the one that would have caught
    this"）：grounded 主路径——LLM 已配置且答案按 answer_prompt 的要求带
    [k] 标记时，前端 buildAnswerReferences 是 anchor 优先的全有全无（引用
    列表整体走 anchor 分支，citation.knowhow 被遮蔽），所以 knowhow 定位
    必须出现在 chunk 型 ANCHOR 上，否则「在表格中查看」按钮在最主流的问答
    形态里永远不出现。此前的 e2e 只测了无 LLM 的回退路径（anchors 恒空），
    正好错过这条腿。"""
    seeded = _seed_projected_table(repo, projector)
    bind_chat_client(repo, "ask_answer", _ChunkAnswerLLM())
    calls = _spy_on_evidence_elements(repo, monkeypatch)

    resp = repo.ask_chunk(seeded["notebook_id"], AskRequest(question=UNIQUE_TERM))

    assert resp.answer, (resp.conclusion, resp.model_errors)  # 合成真的跑了
    chunk_anchors = [a for a in resp.anchors if a.object_type == "chunk"]
    assert chunk_anchors, (resp.answer, resp.anchors)
    knowhow_anchors = [a for a in chunk_anchors if a.knowhow is not None]
    # 两个含 UNIQUE_TERM 的格子各自的 chunk 锚点都带上了各自行的定位。
    assert {a.knowhow.row_id for a in knowhow_anchors} == {seeded["row_a"], seeded["row_c"]}
    assert all(a.knowhow.table_id == seeded["table_id"] for a in knowhow_anchors)
    # JSON 形状：命中的锚点带 {table_id,row_id}（exclude_if 只隐藏 None）。
    dumped = knowhow_anchors[0].model_dump(mode="json")
    assert dumped["knowhow"]["table_id"] == seeded["table_id"]
    # citations 仍然照旧富化（回退腿保持绿），只是 UI 上被 anchor 分支遮蔽。
    assert resp.citations and all(c.knowhow is not None for c in resp.citations)
    # 批量口径：anchor 侧（chunk_context）一次 + citation 侧一次 = 恰好 2 次
    # store 读取，与锚点/引用数量无关。（T1 附图走窄读 `image_asset_rows`，
    # 不经这个 spy。）
    assert len(calls) == 2, calls
