import json

import yaml
from app.services.qiefen.pipeline import run
from app.services.qiefen.emit import to_yaml


def test_pipeline_abstract_atoms_satisfy_span_invariant(source_text):
    src = source_text("engram_paper_mineru.md")
    doc = run(src, source_file="engram_paper_mineru.md",
              profile="article_research", line_range=[9, 11],
              source_id="engram", title="Engram", scope="Abstract only")
    assert doc.evidence_atoms, "expected atoms from the abstract"
    for a in doc.evidence_atoms:
        s = a.source_span
        assert src[s.char_start:s.char_end] == a.raw_text
    # every atom is in exactly one chunk
    in_chunks = [aid for c in doc.semantic_chunks for aid in c.atom_ids]
    assert sorted(in_chunks) == sorted(a.id for a in doc.evidence_atoms)
    # emit is valid YAML with gold key order
    parsed = yaml.safe_load(to_yaml(doc))
    assert list(parsed.keys())[1] == "source_meta"
    assert parsed["evidence_atoms"][0]["raw_text"]
    # no client -> deterministic only: no objects/relations
    assert doc.objects == [] and doc.relations == []


class _FakeClient:
    """Returns one ArticleClaim citing the first atom of whatever package it sees."""
    configured = True

    def chat_json(self, messages, schema_hint):
        prompt = messages[-1]["content"]
        if '"objects"' in schema_hint:
            import re
            m = re.search(r"\[(A-[\w-]+)\|", prompt)  # first atom id in the prompt
            aid = m.group(1) if m else None
            if not aid:
                return json.dumps({"objects": []})
            return json.dumps({"objects": [
                {"type": "ArticleClaim", "payload": {"statement": "a core claim"},
                 "local_evidence_atom_ids": [aid]}]})
        return json.dumps({"relations": []})


def test_pipeline_llm_path_populates_objects_and_expected(source_text):
    src = source_text("engram_paper_mineru.md")
    doc = run(src, source_file="engram_paper_mineru.md",
              profile="article_research", line_range=[9, 11],
              source_id="engram", title="Engram", client=_FakeClient())
    assert doc.objects, "LLM path should produce objects"
    assert all(o.type == "ArticleClaim" for o in doc.objects)
    # the home package's expected_objects references the produced object id
    homed = {o.home_package for o in doc.objects}
    pkgs = {p.id: p for p in doc.context_packages}
    for pid in homed:
        assert pkgs[pid].expected_objects
        assert all(oid in {o.id for o in doc.objects} for oid in pkgs[pid].expected_objects)
