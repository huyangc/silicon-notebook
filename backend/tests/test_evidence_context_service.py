from __future__ import annotations

from app.core.config import Settings
from app.models.schemas import Evidence
from app.services.evidence_context import EvidenceContextService
from app.services.retrieval import RetrievedChunk, RetrievedKnowledge


class _Notebooks:
    def tier_map(self, notebook_ids):
        tiers = {"active": "personal", "base": "base"}
        return {notebook_id: tiers[notebook_id] for notebook_id in notebook_ids if notebook_id in tiers}

    def participant_notebook_ids(self, active_notebook_id):
        return [active_notebook_id, "base"]


class _Sources:
    def evidence_elements(self, element_ids):
        return {}

    def source_metadata(self, source_ids):
        return {}


class _Knowledge:
    def cluster_map(self, notebook_id):
        return {}

    def node_context(self, notebook_id, object_id):
        return {
            "occurrences": [{
                "element_text": "source excerpt",
                "source_title": "Source A",
                "section_path": "§1",
            }],
            "definition": "stable definition",
            "steps": None,
        }

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_count(self, notebook_id, source_id, edge_type, target_id):
        return 1


def _service():
    return EvidenceContextService(
        notebooks=_Notebooks(),
        sources=_Sources(),
        knowledge=_Knowledge(),
        settings=Settings(),
    )


def test_evidence_context_chunk_golden_matches_master():
    chunks = [
        RetrievedChunk(
            chunk_id="c-active", source_id="s1", source_title="Paper A",
            section_path="1.1", text="active text", relevance=0.9,
            notebook_id="active",
        ),
        RetrievedChunk(
            chunk_id="c-base", source_id="s2", source_title="Paper B",
            section_path="2.2", text="base text", relevance=0.8,
            notebook_id="base",
        ),
    ]
    block, evidence = _service().chunk_context(chunks, notebook_id="active")
    assert block == "k1: active text\nk2: base text"
    assert list(evidence) == ["k1", "k2"]
    assert evidence["k1"] == {
        "object_id": "c-active", "object_type": "chunk", "name": "1.1",
        "definition": None, "snippet": "active text", "source_title": "Paper A",
        "location_label": "1.1", "tier": "personal",
    }


def test_evidence_context_knowledge_golden_matches_master():
    hit = RetrievedKnowledge(
        object_id="o1", object_type="concept", payload={"name": "Cascode"},
        evidence=[], tier="base", notebook_id="base",
    )
    block, evidence = _service().knowledge_context("active", [hit])
    assert block == "k1: [concept][base] Cascode — def: stable definition"
    assert evidence["k1"] == {
        "object_id": "o1", "object_type": "concept", "name": "Cascode",
        "definition": "stable definition", "snippet": "source excerpt",
        "source_title": "Source A", "location_label": "§1", "tier": "base",
    }


def test_evidence_context_numeric_group_anchors_match_master():
    evidence = {
        "k1": {"object_id": "o1", "object_type": "claim", "name": "A", "tier": "base"},
        "k2": {"object_id": "o2", "object_type": "claim", "name": "B", "tier": "personal"},
    }
    service = _service()
    anchors = service.parse_anchors("supported [k1, k2]; duplicate [k1]", evidence)
    assert [(anchor.key, anchor.object_id, anchor.tier) for anchor in anchors] == [
        ("k1", "o1", "base"), ("k2", "o2", "personal")
    ]
    assert service.parse_anchors("mixed [k1, k999]", evidence) == []


def test_evidence_context_preserves_tier_and_source_metadata():
    hit = RetrievedKnowledge(
        object_id="o1", object_type="claim", payload={"name": "Claim"},
        evidence=[Evidence(
            source_id="s1", source_title="Source title", element_id="e1",
            element_type="text", location_label="p. 4", quoted_span="quoted",
            confidence=1.0,
        )], tier="base",
    )
    citations = _service().citations_from([hit], {"e1"}, "KG evidence")
    assert len(citations) == 1
    assert citations[0].tier == "base"
    assert citations[0].source_id == "s1"
    assert citations[0].location_label == "p. 4"
