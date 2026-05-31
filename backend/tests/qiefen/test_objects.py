import json

from app.services.qiefen.models import ContextPackage
from app.services.qiefen.objects import extract_objects


class FakeClient:
    configured = True

    def __init__(self, payload):
        self._payload = payload
        self.last_prompt = ""

    def chat_json(self, messages, schema_hint):
        self.last_prompt = messages[-1]["content"]
        return json.dumps(self._payload)


def _pkg():
    return ContextPackage(id="PKG-1", profile="article_research", chunk_id="C1",
                          section_path="Abstract", document_title="Engram",
                          atoms=[{"atom_id": "A1", "atom_type": "claim_sentence"},
                                 {"atom_id": "A2", "atom_type": "method_sentence"}])


def test_objects_parsed_and_evidence_filtered_to_package():
    fake = FakeClient({"objects": [
        {"type": "ArticleClaim",
         "payload": {"statement": "conditional memory complements MoE"},
         "local_evidence_atom_ids": ["A1", "A99"]},   # A99 hallucinated -> dropped
        {"type": "NotAType", "payload": {"x": "y"},
         "local_evidence_atom_ids": ["A2"]},          # bad type -> dropped
    ]})
    objs = extract_objects(fake, _pkg(), "article_research",
                           atom_text={"A1": "...", "A2": "..."})
    assert len(objs) == 1
    o = objs[0]
    assert o.type == "ArticleClaim"
    assert o.local_evidence_atom_ids == ["A1"]        # A99 filtered out
    assert o.home_package == "PKG-1"
    assert o.section_path == "Abstract"
    assert o.payload == {"statement": "conditional memory complements MoE"}
    assert "A1" in fake.last_prompt and "A2" in fake.last_prompt


def test_payload_pruned_to_declared_fields():
    fake = FakeClient({"objects": [
        {"type": "ArticleClaim",
         "payload": {"statement": "x", "bogus_field": "drop me"},
         "local_evidence_atom_ids": ["A1"]},
    ]})
    objs = extract_objects(fake, _pkg(), "article_research")
    assert objs[0].payload == {"statement": "x"}      # bogus_field pruned


def test_bad_json_degrades_to_empty():
    class Boom:
        configured = True

        def chat_json(self, m, s):
            return "not json{"

    assert extract_objects(Boom(), _pkg(), "article_research") == []
