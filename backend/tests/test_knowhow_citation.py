"""Task 12（引用跳转，后端富化）：`EvidenceContextService.citations_from` 命中
knowhow 格子的引用时，批量按 element_id 查 `source_elements.metadata.knowhow`，
填充 `Citation.knowhow = {table_id, row_id}`；非 knowhow 引用该字段保持 None
（且从 JSON 输出中整体缺席，与既有 `memory_id`/`provenance` 的 `exclude_if`
风格一致）。批量查询无论命中多少条引用都只发生一次——这是运行效率的硬约束
（见仓库 memory「运行效率是一等约束」），不能退化成逐条引用各查一次。

本文件是纯单元测试（假 sources/notebooks/knowledge 协作者，无 DB/HTTP），
镜像 `test_evidence_context_service.py` 的既有测试风格，但补充了一个
「记录每次批量调用参数」的 spy fake（`_SpySources`），专门用来断言「只查一次」
这一条 TDD 要求——这是既有 `test_evidence_context_service.py` 里那个静默返回
`{}` 的 `_Sources` fake做不到的。
"""
from __future__ import annotations

import json

from app.core.config import Settings
from app.models.schemas import Evidence
from app.services.evidence_context import EvidenceContextService
from app.services.retrieval import RetrievedKnowledge


class _Notebooks:
    def tier_map(self, notebook_ids):
        return {}

    def participant_notebook_ids(self, active_notebook_id):
        return [active_notebook_id]


class _Knowledge:
    def cluster_map(self, notebook_id):
        return {}

    def node_context(self, notebook_id, object_id):
        return {}

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_count(self, notebook_id, source_id, edge_type, target_id):
        return 0


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
        [_hit(_evidence("el-cell"))], {"el-cell"}, "KG evidence",
    )
    assert len(citations) == 1
    assert citations[0].knowhow is not None
    assert citations[0].knowhow.table_id == "tbl-1"
    assert citations[0].knowhow.row_id == "row-1"


def test_non_knowhow_citation_has_no_knowhow_field_and_is_excluded_from_json():
    sources = _SpySources({"el-plain": _element_row({})})
    citations = _service(sources).citations_from(
        [_hit(_evidence("el-plain"))], {"el-plain"}, "KG evidence",
    )
    assert citations[0].knowhow is None
    assert "knowhow" not in citations[0].model_dump(mode="json")


def test_element_missing_from_batch_result_has_no_knowhow_field():
    # Element absent from the batch result entirely (e.g. deleted between
    # retrieval and citation-building) must resolve to None, never KeyError.
    sources = _SpySources({})
    citations = _service(sources).citations_from(
        [_hit(_evidence("el-missing"))], {"el-missing"}, "KG evidence",
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
    citations = _service(sources).citations_from([hit], {"el-a", "el-b"}, "KG evidence")

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
    citations = _service(sources).citations_from(hits, {"el-cell", "el-plain"}, "KG evidence")

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
        [_hit(_evidence(""))], set(), "KG evidence",
    )
    assert citations[0].knowhow is None
    assert sources.calls == []
