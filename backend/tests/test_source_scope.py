from app.models.common import Evidence
from app.models.ask import AskRequest
from app.models.source_scope import SourceScope
from app.models.notebooks import NotebookSummary
from app.api.ask_routes import (
    _validate_reasoning_scope_preflight,
    _validate_source_scope,
)
from types import SimpleNamespace
from fastapi import HTTPException
import pytest
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge
from app.services.kg.follow_chain import ChainHop, FollowChainResult, InferredChain
from app.services.retrieval_service import RetrievalService
from app.services.source_scope import (
    filter_retrieval_items,
    scoped_allowed_source_ids,
    scoped_conversation_history,
    source_allowed,
    source_scope_context,
)


def _knowledge(source_id: str, *, notebook_id: str = "nb") -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id=f"ko-{source_id}",
        object_type="claim",
        payload={"name": source_id},
        evidence=[Evidence(
            source_id=source_id,
            source_title=source_id,
            element_id=f"el-{source_id}",
            element_type="paragraph",
            location_label="p1",
            quoted_span="evidence",
            confidence=1.0,
        )],
        notebook_id=notebook_id,
    )


def test_omitted_or_default_exclude_scope_preserves_historical_behavior():
    chunks = [
        RetrievedChunk("c1", "s1", "one", "", "one"),
        RetrievedChunk("c2", "s2", "two", "", "two"),
    ]
    assert filter_retrieval_items("nb", "chunk", chunks) == chunks
    with source_scope_context("nb", SourceScope(mode="exclude", source_ids=[])):
        assert filter_retrieval_items("nb", "chunk", chunks) == chunks


def test_include_scope_filters_active_chunks_and_kg_evidence_but_keeps_base():
    chunks = [
        RetrievedChunk("c1", "s1", "one", "", "one"),
        RetrievedChunk("c2", "s2", "two", "", "two"),
        RetrievedChunk("cb", "base-source", "base", "", "base", notebook_id="base"),
    ]
    knowledge = [_knowledge("s1"), _knowledge("s2"), _knowledge("base-source", notebook_id="base")]
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert [row.chunk_id for row in filter_retrieval_items("nb", "chunk", chunks)] == [
            "c1", "cb"
        ]
        assert [row.object_id for row in filter_retrieval_items(
            "nb", "knowledge", knowledge
        )] == ["ko-s1", "ko-base-source"]


def test_empty_include_scope_removes_all_active_sources_but_not_mounted_base():
    with source_scope_context("nb", SourceScope(mode="include", source_ids=[])):
        assert source_allowed("nb", "s1") is False
        assert source_allowed("base", "base-source") is True


def test_restricted_scope_drops_prior_conversation_history():
    assert scoped_conversation_history("prior answer") == "prior answer"
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        assert scoped_conversation_history("prior answer from s2") == ""


def test_scoped_dict_nodes_keep_only_selected_evidence():
    nodes = [
        {
            "object_id": "k1",
            "notebook_id": "nb",
            "evidence": [{"source_id": "s1"}, {"source_id": "s2"}],
        },
        {
            "object_id": "k2",
            "notebook_id": "nb",
            "evidence": [{"source_id": "s2"}],
        },
    ]
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        filtered = filter_retrieval_items("nb", "knowledge", nodes)
    assert [row["object_id"] for row in filtered] == ["k1"]
    assert filtered[0]["evidence"] == [{"source_id": "s1"}]


def test_follow_chain_replaces_hop_evidence_with_scoped_copy():
    def hop(relation_id: str, source: str, target: str, evidence):
        return ChainHop(
            relation_id=relation_id,
            notebook_id="nb",
            tier="personal",
            source_object_id=source,
            target_object_id=target,
            edge_type="precedes",
            source_name=source,
            target_name=target,
            evidence=evidence,
        )

    first = hop("r1", "a", "b", [
        {"source_id": "s2", "quoted_span": "must not leak"},
        {"source_id": "s1", "quoted_span": "selected"},
    ])
    second = hop("r2", "b", "c", [
        {"source_id": "s1", "quoted_span": "selected second"},
    ])
    chain = InferredChain(
        source_object_id="a",
        via_object_id="b",
        target_object_id="c",
        source_name="a",
        via_name="b",
        target_name="c",
        inferred_edge_type="precedes",
        hops=(first, second),
    )
    nodes = [
        {"object_id": oid, "notebook_id": "nb", "evidence": [{"source_id": "s1"}]}
        for oid in ("a", "b", "c")
    ]

    class _Graph:
        def follow_chain(self, *_args, **_kwargs):
            return FollowChainResult(inferences=[chain], nodes=nodes)

    retrieval = RetrievalService(
        candidates=object(), graph=_Graph(), community_queries=lambda: []
    )
    with source_scope_context("nb", SourceScope(mode="include", source_ids=["s1"])):
        result = retrieval.follow_chain("nb", "a")

    assert len(result.inferences) == 1
    assert result.inferences[0].hops[0].evidence == [
        {"source_id": "s1", "quoted_span": "selected"}
    ]


class _ScopeRepo:
    def __init__(self, visible: list[str], count: int):
        self.visible = visible
        self.count = count

    def visible_source_ids(self, _notebook_id, source_ids):
        return [source_id for source_id in source_ids if source_id in self.visible]

    def visible_source_count(self, _notebook_id):
        return self.count

    def all_visible_source_ids(self, _notebook_id):
        return list(self.visible)


def _notebook(*, bases=None):
    return NotebookSummary(
        id="nb", name="n", purpose="", primary_domain="", status="ready",
        counts={}, created_label="", base_notebooks=bases or [],
    )


def test_empty_local_scope_requires_a_mounted_base():
    with pytest.raises(HTTPException) as exc:
        _validate_source_scope(
            _ScopeRepo([], 0), _notebook(),
            SourceScope(mode="include", source_ids=[]),
        )
    assert exc.value.status_code == 409


def test_scope_rejects_cross_notebook_source_ids():
    with pytest.raises(HTTPException) as exc:
        _validate_source_scope(
            _ScopeRepo(["s1"], 1), _notebook(),
            SourceScope(mode="include", source_ids=["foreign"]),
        )
    assert exc.value.status_code == 422


def test_exclusion_scope_is_frozen_to_an_explicit_allow_list():
    resolved = _validate_source_scope(
        _ScopeRepo(["s1", "s2", "s3"], 3),
        _notebook(),
        SourceScope(mode="exclude", source_ids=["s2"]),
    )
    assert resolved == SourceScope(mode="include", source_ids=["s1", "s3"])


def test_checkbox_ceiling_intersects_model_allow_list_and_leaves_base_alone():
    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=["s1", "s2"])
    ):
        assert scoped_allowed_source_ids("nb") == ("s1", "s2")
        assert scoped_allowed_source_ids("nb", ["s2", "s3"]) == ("s2",)
        assert scoped_allowed_source_ids("base", ["b1"]) == ("b1",)


def test_reasoning_preflight_uses_the_submitted_checkbox_ceiling():
    class _Repo:
        def validate_reasoning_submission(self, notebook_id, _payload):
            if not source_allowed(notebook_id, "s2"):
                raise ValueError("指定来源超出当前勾选范围")

    payload = AskRequest(
        question="q",
        mode="reasoning",
        source_scope=SourceScope(mode="include", source_ids=["s1"]),
    )
    with pytest.raises(HTTPException, match="当前勾选范围") as exc:
        _validate_reasoning_scope_preflight(
            _Repo(), "nb", SimpleNamespace(id="reasoning"), payload
        )
    assert exc.value.status_code == 409


def test_scoped_chunk_overlay_keeps_base_seeds_without_whole_graph_io():
    from app.services.retrieval_candidates import CandidateRetrievalService

    base_hit = _knowledge("base-source", notebook_id="base")
    base_hit.tier = "base"

    class _Candidates:
        _MIX_NODE_SEEDS = 8

        def federated_retrieve(self, *_args, **_kwargs):
            return [base_hit]

        def _federated_graph_is_large(self, *_args, **_kwargs):
            raise AssertionError("scoped direct seeds must not inspect whole graph")

        def federated_retrieve_relations(self, *_args, **_kwargs):
            raise AssertionError("scoped overlay must not retrieve relations")

    with source_scope_context(
        "nb", SourceScope(mode="include", source_ids=[])
    ):
        block, id_map, hits, supports = (
            CandidateRetrievalService._chunk_kg_overlay(
                _Candidates(), "nb", "question", "", 1000
            )
        )

    assert hits == [base_hit]
    assert supports == {}
    assert id_map["k1001"]["notebook_id"] == "base"
    assert "base-source" in block
