import json

from app.services.qiefen.models import KnowledgeObjectQ
from app.services.qiefen.relations import extract_relations, build_relation_prompt


class FakeClient:
    configured = True

    def __init__(self, payload):
        self._payload = payload
        self.last_prompt = ""

    def chat_json(self, messages, schema_hint):
        self.last_prompt = messages[-1]["content"]
        return json.dumps(self._payload)


def _objs():
    return [
        KnowledgeObjectQ(id="OBJ-1", type="ArticleMethod", payload={"name": "Engram"},
                         local_evidence_atom_ids=["A1"]),
        KnowledgeObjectQ(id="OBJ-2", type="ArticleClaim", payload={"statement": "x"},
                         local_evidence_atom_ids=["A2"]),
    ]


def test_relations_filtered_by_endpoints_and_type():
    fake = FakeClient({"relations": [
        {"relation_type": "method_addresses_problem", "source_object_id": "OBJ-1",
         "target_object_id": "OBJ-2", "evidence_atom_ids": ["A1", "AX"]},  # AX dropped
        {"relation_type": "method_addresses_problem", "source_object_id": "OBJ-1",
         "target_object_id": "OBJ-99", "evidence_atom_ids": []},           # bad endpoint
        {"relation_type": "not_a_relation", "source_object_id": "OBJ-1",
         "target_object_id": "OBJ-2", "evidence_atom_ids": []},            # bad type
    ]})
    rels = extract_relations(fake, _objs(), "article_research")
    assert len(rels) == 1
    r = rels[0]
    assert r.relation_type == "method_addresses_problem"
    assert (r.source_object_id, r.target_object_id) == ("OBJ-1", "OBJ-2")
    assert r.evidence_atom_ids == ["A1"]          # AX filtered (not in any object evidence)
    assert "OBJ-1" in fake.last_prompt and "Engram" in fake.last_prompt


def test_self_loops_and_too_few_objects_yield_nothing():
    fake = FakeClient({"relations": [
        {"relation_type": "method_addresses_problem", "source_object_id": "OBJ-1",
         "target_object_id": "OBJ-1", "evidence_atom_ids": []},
    ]})
    assert extract_relations(fake, _objs(), "article_research") == []
    assert extract_relations(fake, _objs()[:1], "article_research") == []  # <2 objects
