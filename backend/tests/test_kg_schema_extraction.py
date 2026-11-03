"""Effective definitions survive window extraction, review and record assembly."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.domain.extraction_profiles import OBJECT_SCHEMAS, ObjectSchema
from app.domain.knowledge_contracts import SCHEMA_FIELDS_PAYLOAD_KEY
from app.services import kg_ingest
from app.services.kg.extract import extract_window
from app.services.kg.parsing import parse_elements


class _Client:
    configured = True
    settings = SimpleNamespace(kg_extract_max_tokens=1024)

    def __init__(self, *replies):
        self.replies = iter(replies)
        self.calls = []
        self.cacheable = []

    def chat_json(self, messages, hint, **kwargs):
        reply = json.dumps(next(self.replies))
        self.calls.append((messages, hint))
        self.cacheable.append(kwargs["response_validator"](reply))
        return reply


def _schema():
    return ObjectSchema(
        type="failure_case", plural="failure_cases",
        fields=["title", "root_cause", "symptoms"], primary="title",
        description="Failure cases and their causes", list_fields=["symptoms"],
    )


def _node(title="Clock failure", **payload):
    return {"type": "failure_case", "name": "Envelope is not the primary", "ev": 0,
            "payload": {"title": title, "root_cause": "jitter",
                        "symptoms": ["timing errors"], **payload}}


def test_configured_types_survive_glean_refine_edges_and_record_assembly():
    text = "Clock failure and data failure cause timing errors due to jitter."
    elements = parse_elements(text, "doc.md")
    client = _Client(
        {"nodes": [_node(), {"type": "Concept", "name": "jitter", "ev": 0}],
         "edges": [{"type": "depends_on", "source": 0, "target": 1, "ev": 0}]},
        {"nodes": [_node("Data failure")], "edges": []},
        {"drop": []},
    )
    schemas = {"concept": OBJECT_SCHEMAS["concept"], "failure_case": _schema()}
    nodes, edges = extract_window(
        client, elements, "Failures", "academic", refine=True,
        gleaning_rounds=1, object_schemas=schemas,
    )
    assert [node.name for node in nodes] == ["Clock failure", "jitter", "Data failure"]
    assert [edge.type for edge in edges] == ["depends_on"]
    assert client.cacheable == [True, True, True]
    assert client.calls[0][1] == client.calls[1][1]
    graph = kg_ingest.KnowledgeGraph(doc_id="doc.md", doc_type="academic", nodes=nodes, edges=edges)
    # The graph also crosses JSON in batch/staging paths.
    restored = kg_ingest.KnowledgeGraph.model_validate(graph.to_dict())
    objects, relations = kg_ingest.build_records(
        restored, "source", "Doc",
        [SimpleNamespace(id="element", text=text, element_type="paragraph", location_label="p1")],
    )
    assert objects[0]["payload"]["title"] == "Clock failure"
    assert objects[0]["payload"]["name"] == "Clock failure"
    assert objects[0]["payload"]["symptoms"] == ["timing errors"]
    assert len(relations) == 1


@pytest.mark.parametrize("bad_node", [
    _node(symptoms="not a list"),
    _node(root_cause={"unexpected": "object"}),
    {**_node(), "type": "unregistered_type"},
    {**_node("Unsupported failure"), "ev": 99},
    {**_node(), "payload": {"root_cause": "jitter"}},
])
def test_bad_configured_nodes_are_neither_persisted_nor_cached(bad_node):
    client = _Client({"nodes": [bad_node], "edges": []})
    nodes, edges = extract_window(
        client, parse_elements("Clock failure due to jitter.", "doc.md"), "", "academic",
        object_schemas={"failure_case": _schema()},
    )
    assert nodes == edges == []
    assert client.cacheable == [False]


def test_no_active_types_never_calls_the_model():
    client = _Client()
    nodes, edges = extract_window(
        client, parse_elements("Clock failure.", "doc.md"), "", "academic", object_schemas={},
    )
    assert nodes == edges == []
    assert client.calls == []


def test_gleaning_keeps_same_name_with_different_declared_values():
    client = _Client(
        {"nodes": [_node()], "edges": []},
        {"nodes": [_node(root_cause="voltage droop")], "edges": []},
    )
    nodes, _ = extract_window(
        client, parse_elements("Clock failure due to jitter or voltage droop.", "doc.md"),
        "", "academic", gleaning_rounds=1, object_schemas={"failure_case": _schema()},
    )
    assert {node.payload["root_cause"] for node in nodes} == {"jitter", "voltage droop"}


@pytest.mark.parametrize("fields,primary,list_fields,declared", [
    (["name"], "name", ["name"], {"name": ["Clock failure", "Clock fault"]}),
    (["name", "root_cause"], "root_cause", [], {"name": "Clock failure", "root_cause": "jitter"}),
    (["name", "section_path"], "name", ["section_path"], {"name": "Clock failure", "section_path": ["Failures"]}),
    (["name", "steps"], "name", ["steps"], {"name": "Clock failure", "steps": ["check clock"]}),
    (["name", "validity_scope"], "name", [], {"name": "Clock failure", "validity_scope": "low frequency"}),
])
def test_envelope_collisions_keep_declared_values_visible_and_searchable(
    fields, primary, list_fields, declared,
):
    from app.repositories.postgres.query_store import QueryStore as PostgresQueryStore
    from app.repositories.sqlite.query_store import QueryStore as SQLiteQueryStore
    from app.services.knowledge_governance import payload_join
    from app.services.response_drafting import knowledge_record
    from app.services.retrieval import _payload_text

    schema = ObjectSchema(
        type="failure_case", plural="failure_cases", fields=fields, primary=primary,
        list_fields=list_fields, description="Failure details",
    )
    text = "Clock failure also called Clock fault is caused by jitter."
    client = _Client({"nodes": [{"type": "failure_case", "ev": 0, "payload": declared}], "edges": []})
    nodes, edges = extract_window(
        client, parse_elements(text, "doc.md"), "Section 1", "academic",
        object_schemas={"failure_case": schema},
    )
    graph = kg_ingest.KnowledgeGraph(doc_id="doc.md", doc_type="academic", nodes=nodes, edges=edges)
    objects, _ = kg_ingest.build_records(
        graph, "source", "Doc",
        [SimpleNamespace(id="element", text=text, element_type="paragraph", location_label="p1")],
    )
    payload = objects[0]["payload"]
    assert isinstance(payload["name"], str)
    assert payload["section_path"] == "Section 1"
    assert "steps" not in payload
    assert "validity_scope" not in payload
    restored = {**payload, **payload.get(SCHEMA_FIELDS_PAYLOAD_KEY, {})}
    assert {key: restored[key] for key in fields} == declared
    record = knowledge_record("failure_case", {"id": "object", **objects[0]}, schema)
    shown = {field.key: field.value for field in record.fields}
    for key, value in declared.items():
        assert shown[key] == (", ".join(value) if isinstance(value, list) else value)
    for render in (payload_join, _payload_text, SQLiteQueryStore._payload_join, PostgresQueryStore._payload_join):
        assert "Clock failure" in render(payload)
        assert SCHEMA_FIELDS_PAYLOAD_KEY not in render(payload)
        if "section_path" in declared:
            assert "Failures" in render(payload)


def test_same_name_with_different_configured_fields_keeps_both_records():
    client = _Client({"nodes": [
        {"type": "Concept", "name": "Clock", "ev": 0,
         "payload": {"name": "Clock", "condition": "fast"}},
        {"type": "Concept", "name": "Clock", "ev": 0,
         "payload": {"name": "Clock", "condition": "slow"}},
    ], "edges": []})
    schema = ObjectSchema(
        type="concept", plural="concepts", fields=["name", "condition"],
        primary="name", description="Clock operating conditions",
    )
    graph = kg_ingest.extract_graph(
        client, "Clock operates in fast and slow conditions.", "doc.md", "academic",
        object_schemas={"concept": schema},
    )
    assert {node.payload["condition"] for node in graph.nodes} == {"fast", "slow"}
