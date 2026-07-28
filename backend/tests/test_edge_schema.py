from __future__ import annotations

from contextlib import contextmanager
from app.services.kg.edge_schema import (
    EDGE_SPECS,
    EDGE_TYPE_ORDER,
    canonical_edge_key,
    is_queryable_edge_pair,
    is_valid_edge_pair,
    render_edge_prompt_rules,
)
from app.services.graph_retrieval import GraphRetrievalService
from pathlib import Path


def test_registry_and_prompt_cover_the_same_edge_vocabulary():
    prompt = render_edge_prompt_rules()
    assert set(EDGE_TYPE_ORDER) == set(EDGE_SPECS)
    assert all(f"- {edge_type} (" in prompt for edge_type in EDGE_SPECS)


def test_exact_endpoint_matrix_examples():
    assert is_valid_edge_pair("supports", "Concept", "Claim")
    assert is_valid_edge_pair("derived_from", "Claim", "Formula")
    assert is_valid_edge_pair("depends_on", "Formula", "Procedure")
    assert is_valid_edge_pair("precedes", "Formula", "Formula")
    assert not is_valid_edge_pair("supports", "Procedure", "Claim")
    assert not is_valid_edge_pair("precedes", "Formula", "Claim")
    assert not is_valid_edge_pair("made_up", "Claim", "Concept")


def test_only_symmetric_edges_canonicalize_endpoint_order():
    assert canonical_edge_key("contrasts_with", "z", "a") == (
        "contrasts_with", "a", "z"
    )
    assert canonical_edge_key("supports", "z", "a") == ("supports", "z", "a")


def test_query_consumers_preserve_known_edges_to_schema_extension_types():
    assert not is_valid_edge_pair("derived_from", "Formula", "voltage")
    assert is_queryable_edge_pair("derived_from", "Formula", "voltage")
    assert not is_queryable_edge_pair("supports", "Procedure", "Claim")
    assert not is_queryable_edge_pair("unknown_edge", "Formula", "voltage")


def test_consumers_do_not_redeclare_a_second_complete_edge_registry():
    kg_dir = Path(__file__).resolve().parents[1] / "app" / "services" / "kg"
    for filename in ("extract.py", "edge_trust.py", "graph_reason.py"):
        source = (kg_dir / filename).read_text(encoding="utf-8")
        assert "TYPE_CONSTRAINTS" not in source
        assert "VALID_EDGE_TYPES = frozenset" not in source


def test_ask_relation_context_filters_historical_illegal_core_pairs():
    class _Store:
        @staticmethod
        def in_network_relation_rows(_db, _notebook_id, _ids):
            return [
                {
                    "source_object_id": "a", "target_object_id": "b",
                    "edge_type": "supports", "source_type": "concept",
                    "target_type": "claim",
                },
                {
                    "source_object_id": "c", "target_object_id": "b",
                    "edge_type": "supports", "source_type": "procedure",
                    "target_type": "claim",
                },
            ]

    @contextmanager
    def _connect():
        yield object()

    service = object.__new__(GraphRetrievalService)
    service.knowledge = _Store()
    service._connect = _connect
    rows = service.in_network_relations(["nb"], ["a", "b", "c"])
    assert [(row["source_object_id"], row["target_object_id"]) for row in rows] == [
        ("a", "b")
    ]
