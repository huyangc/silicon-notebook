"""D0-4 -- 对等模式下每条引用都保留真实笔记本归属。

`domain.citation_origin.foreign_notebook_id(origin, active)` 把等于 active 的归属
归零,因为前端那张库名映射**含当前笔记本**,回显 active 会给用户自己的笔记挂一个
多余的「来自「当前笔记本自己的名字」」徽章。这条规则需要「当前库」这个概念,而对等
模式没有:名义 active 是 `ParticipantOverride.notebook_ids[0]`,用户并没有单独选它,
而界面必须说清每条引用来自所选的哪一个库——**包括那一个**。

所以归一口径统一经 `source_scope.citation_active_id`:逐库天花板在场时它返回 `""`,
`foreign_notebook_id(x, "")` 对任何非空 x 原样放行。本文件按四条产出路径各钉一条
(chunk / KG / 元素 / 文档概述),每条都**含名义 active 自己的引用**,并各带一条
「不装天花板时与父提交逐值相等」的对照臂。另外两个归一点(follow-chain 渲染、
电子表格回执)同规则,一并钉住,免得留下未覆盖的产出面。

生产上今天不可达(没有任何地方构造 `notebook_source_ceilings`),入口是
`source_scope_context(active, None, None, notebook_source_ceilings={...})`。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.sources import PaginatedSourceElements, SourceElement
from app.services.collection_enumeration import SourceItem
from app.services.document_source_overview import prepare_source_overview
from app.services.evidence_context import EvidenceContextService
from app.services.kg.follow_chain import (
    compose_two_hop_paths, render_follow_chain_context,
)
from app.services.kg.graph_reason import render_subgraph_context
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge
from app.services.source_scope import source_scope_context
from app.services.spreadsheet_analysis import SpreadsheetAnalysisService


ACTIVE = "nb-active"
PEER = "nb-peer"
_SELECTED = (ACTIVE, PEER)


def peer_scope():
    """PR-D1 冻结的安装形状:只提交第三个维度,每个参与库各一份冻结来源清单。

    名义 active **也**在里面——少了它,``filter_retrieval_items`` 对它会落到
    ``ceiling_active=False → return allowed`` 而完全不设防。
    """
    return source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={
            ACTIVE: {"s-active"}, PEER: {"s-peer"},
        },
    )


# ---------------------------------------------------------------------------
# evidence_context 的替身(形状抄 test_evidence_context_service.py)
# ---------------------------------------------------------------------------

class _Notebooks:
    def tier_map(self, notebook_ids):
        tiers = {ACTIVE: "personal", PEER: "base"}
        return {nid: tiers.get(nid, "personal") for nid in notebook_ids}

    def participant_notebook_ids(self, active_notebook_id):
        return [ACTIVE, PEER]


class _Sources:
    def __init__(self, metadata=None, elements=None):
        self.metadata = metadata or {}
        self.elements = elements or {}

    def evidence_elements(self, element_ids):
        return {eid: self.elements[eid]
                for eid in element_ids if eid in self.elements}

    def source_metadata(self, source_ids):
        return {sid: self.metadata[sid]
                for sid in source_ids if sid in self.metadata}


class _Knowledge:
    def cluster_map(self, notebook_id):
        return {}

    def cluster_fold(self, notebook_id, object_ids):
        return {}

    def node_context(self, notebook_id, object_id):
        return {"occurrences": [], "definition": "定义", "steps": None}

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_counts(self, notebook_id, triples):
        return {triple: 1 for triple in triples}


_SOURCE_METADATA = {
    "s-active": {"title": "active.pdf", "file_name": "active.pdf",
                 "notebook_id": ACTIVE},
    "s-peer": {"title": "peer.pdf", "file_name": "peer.pdf",
               "notebook_id": PEER},
}
_ELEMENTS = {
    "e-active": {"id": "e-active", "source_id": "s-active",
                 "element_type": "paragraph", "location_label": "p.1",
                 "text": "本库原文", "metadata": "{}"},
    "e-peer": {"id": "e-peer", "source_id": "s-peer",
               "element_type": "paragraph", "location_label": "p.2",
               "text": "参考库原文", "metadata": "{}"},
}


def _service() -> EvidenceContextService:
    return EvidenceContextService(
        notebooks=_Notebooks(),
        sources=_Sources(_SOURCE_METADATA, _ELEMENTS),
        knowledge=_Knowledge(),
        settings=Settings(),
    )


def _chunks():
    """两条命中,**两条都打了真实 id**——这是 ``_peer_leg`` 之后联邦通道的产出。"""
    return [
        RetrievedChunk(
            chunk_id="c-active", source_id="s-active", source_title="active.pdf",
            section_path="1", text="本库段落", relevance=0.9,
            element_ids=("e-active",), notebook_id=ACTIVE,
        ),
        RetrievedChunk(
            chunk_id="c-peer", source_id="s-peer", source_title="peer.pdf",
            section_path="2", text="参考库段落", relevance=0.8,
            element_ids=("e-peer",), notebook_id=PEER,
        ),
    ]


# ---------------------------------------------------------------------------
# 路径 1:chunk 引用与 chunk 段 id_map
# ---------------------------------------------------------------------------

def test_chunk_citations_keep_the_nominal_active_notebook_id():
    service = _service()

    with peer_scope():
        pairs = service.chunk_citations(_chunks(), notebook_id=ACTIVE)
        _block, evidence = service.chunk_context(_chunks(), notebook_id=ACTIVE)

    assert [citation.notebook_id for citation, _ids in pairs] == [ACTIVE, PEER]
    assert [entry["notebook_id"] for entry in evidence.values()] == [ACTIVE, PEER]
    # tier 查表口径不动:它问的是「这条 chunk 住在哪个库」,打标之后那条回退分支
    # 只是变得不可达,而不是变错。
    assert [citation.tier for citation, _ids in pairs] == ["personal", "base"]


def test_chunk_citations_are_value_identical_without_a_ceiling():
    service = _service()

    pairs = service.chunk_citations(_chunks(), notebook_id=ACTIVE)
    _block, evidence = service.chunk_context(_chunks(), notebook_id=ACTIVE)

    assert [citation.notebook_id for citation, _ids in pairs] == ["", PEER]
    assert [entry["notebook_id"] for entry in evidence.values()] == ["", PEER]


# ---------------------------------------------------------------------------
# 路径 2:KG(子图渲染的 id_map,以及 knowledge_context 的徽章口径)
# ---------------------------------------------------------------------------

def _subgraph():
    return [
        ({"object_id": "o-active", "object_type": "concept", "name": "本库概念",
          "notebook_id": ACTIVE, "tier": "personal"}, None, None),
        ({"object_id": "o-peer", "object_type": "concept", "name": "参考库概念",
          "notebook_id": PEER, "tier": "base"}, None, None),
    ]


def test_subgraph_anchors_keep_the_nominal_active_notebook_id():
    with peer_scope():
        _ctx, id_map = render_subgraph_context(
            _subgraph(), active_notebook_id=ACTIVE,
        )

    assert [entry["notebook_id"] for entry in id_map.values()] == [ACTIVE, PEER]


def test_subgraph_anchors_are_value_identical_without_a_ceiling():
    _ctx, id_map = render_subgraph_context(
        _subgraph(), active_notebook_id=ACTIVE,
    )

    assert [entry["notebook_id"] for entry in id_map.values()] == ["", PEER]


def test_knowledge_context_anchors_keep_the_nominal_active_notebook_id():
    hits = [
        RetrievedKnowledge(
            object_id="o-active", object_type="concept",
            payload={"name": "本库概念"}, tier="personal",
            notebook_id=ACTIVE, evidence=[],
        ),
        RetrievedKnowledge(
            object_id="o-peer", object_type="concept",
            payload={"name": "参考库概念"}, tier="base",
            notebook_id=PEER, evidence=[],
        ),
    ]
    service = _service()

    with peer_scope():
        _block, evidence = service.knowledge_context(ACTIVE, hits)
    scoped = [entry["notebook_id"] for entry in evidence.values()]

    _block, evidence = service.knowledge_context(ACTIVE, hits)
    plain = [entry["notebook_id"] for entry in evidence.values()]

    assert scoped == [ACTIVE, PEER]
    assert plain == ["", PEER]


# ---------------------------------------------------------------------------
# 路径 3:元素 / 集合行引用(collection_item_citations)
# ---------------------------------------------------------------------------

def _collection_items():
    from app.services.collection_enumeration import KgObjectItem

    return [
        # 文档行(走 _is_document_row 分支)+ KG 行(走元素水合分支),两个库各一条。
        SourceItem("s-active", "active.pdf", "文档", "", ACTIVE, "personal"),
        SourceItem("s-peer", "peer.pdf", "文档", "", PEER, "base"),
        KgObjectItem(
            object_id="o-active", object_type="claim", name="本库论断",
            section_path="", notebook_id=ACTIVE, tier="personal",
            evidence_element_ids=("e-active",),
        ),
        KgObjectItem(
            object_id="o-peer", object_type="claim", name="参考库论断",
            section_path="", notebook_id=PEER, tier="base",
            evidence_element_ids=("e-peer",),
        ),
    ]


def test_collection_item_citations_keep_the_nominal_active_notebook_id():
    """鉴权口径不变、只有显示归属变:同一个函数里 ``active_notebook_id`` 仍然
    在喂挂载谓词与 ``expected_notebook_id``,变的只是第二个实参。"""
    service = _service()

    with peer_scope():
        citations = service.collection_item_citations(
            _collection_items(), active_notebook_id=ACTIVE,
        )

    assert {key: citation.notebook_id for key, citation in citations.items()} == {
        "s-active": ACTIVE, "s-peer": PEER,
        "o-active": ACTIVE, "o-peer": PEER,
    }


def test_collection_item_citations_are_value_identical_without_a_ceiling():
    service = _service()

    citations = service.collection_item_citations(
        _collection_items(), active_notebook_id=ACTIVE,
    )

    assert {key: citation.notebook_id for key, citation in citations.items()} == {
        "s-active": "", "s-peer": PEER, "o-active": "", "o-peer": PEER,
    }


# ---------------------------------------------------------------------------
# 路径 4:文档概述(_try_document_overview 在对等模式保留并开启)
# ---------------------------------------------------------------------------

class _SourcePages:
    def __init__(self, source_id: str):
        self.source_id = source_id
        self.elements = [SourceElement(
            id=f"{source_id}-e0", source_id=source_id,
            element_type="paragraph", location_label="章节 0", text="原文",
            metadata={"section_path": "章节 0"},
        )]

    def source_elements_page(self, source_id, offset=0, limit=1):
        return PaginatedSourceElements(
            items=self.elements[offset:offset + limit],
            total_count=len(self.elements), offset=offset, limit=limit,
        )


def _overview(notebook_id: str):
    item = SourceItem(
        f"s-{notebook_id}", f"{notebook_id}.pdf", "文档", "", notebook_id,
        "personal",
    )
    return prepare_source_overview(
        _SourcePages(item.source_id), item, 1000, 10,
        active_notebook_id=ACTIVE,
    )


def test_document_overview_keeps_the_nominal_active_notebook_id():
    with peer_scope():
        own = _overview(ACTIVE)
        borrowed = _overview(PEER)

    assert [c.notebook_id for c in own.citations] == [ACTIVE]
    assert [e["notebook_id"] for e in own.id_map.values()] == [ACTIVE]
    assert [c.notebook_id for c in borrowed.citations] == [PEER]
    assert [e["notebook_id"] for e in borrowed.id_map.values()] == [PEER]


def test_document_overview_is_value_identical_without_a_ceiling():
    own = _overview(ACTIVE)
    borrowed = _overview(PEER)

    assert [c.notebook_id for c in own.citations] == [""]
    assert [e["notebook_id"] for e in own.id_map.values()] == [""]
    assert [c.notebook_id for c in borrowed.citations] == [PEER]


# ---------------------------------------------------------------------------
# 其余两个归一点:follow-chain 渲染 / 电子表格回执
# ---------------------------------------------------------------------------

def _chain(notebook_id: str):
    nodes = {
        oid: {"id": oid, "object_type": "formula", "status": "approved",
              "payload": {"name": oid}}
        for oid in ("A", "B", "C")
    }
    relations = [
        {"id": rid, "source_object_id": source, "target_object_id": target,
         "edge_type": "derived_from", "review_status": "verified",
         "tier": "personal", "notebook_id": notebook_id,
         "evidence": [{"quote": f"{source} to {target}", "source_id": "src-1",
                       "element_id": f"el-{rid}", "location_label": "p.1"}]}
        for rid, source, target in (("r1", "A", "B"), ("r2", "B", "C"))
    ]
    return compose_two_hop_paths(nodes, relations, "A")[0]


@pytest.mark.parametrize("notebook_id", [ACTIVE, PEER])
def test_follow_chain_anchors_keep_their_notebook_id_in_peer_mode(notebook_id):
    with peer_scope():
        _ctx, id_map = render_follow_chain_context(
            [_chain(notebook_id)], active_notebook_id=ACTIVE,
        )

    assert {entry["notebook_id"] for entry in id_map.values()} == {notebook_id}


def test_follow_chain_anchors_are_value_identical_without_a_ceiling():
    _ctx, id_map = render_follow_chain_context(
        [_chain(ACTIVE)], active_notebook_id=ACTIVE,
    )
    assert {entry["notebook_id"] for entry in id_map.values()} == {""}


def _workbook_citation(notebook_id: str):
    return SpreadsheetAnalysisService._citation(
        {"notebook_id": notebook_id, "source_id": f"s-{notebook_id}",
         "source_title": "表", "source_file_name": "表.xlsx"},
        {"name": "Sheet1", "range": "A1:B2"},
        [{"__element_id": "e-1"}],
        active_notebook_id=ACTIVE,
        notebook_tiers={ACTIVE: "personal", PEER: "base"},
    )


def test_workbook_receipts_keep_their_notebook_id_in_peer_mode():
    with peer_scope():
        assert _workbook_citation(ACTIVE).notebook_id == ACTIVE
        assert _workbook_citation(PEER).notebook_id == PEER


def test_workbook_receipts_are_value_identical_without_a_ceiling():
    assert _workbook_citation(ACTIVE).notebook_id == ""
    assert _workbook_citation(PEER).notebook_id == PEER
