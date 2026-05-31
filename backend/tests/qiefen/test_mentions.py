from app.services.qiefen.models import KnowledgeObjectQ
from app.services.qiefen.mentions import derive_mentions, build_canonicalization


def test_mentions_from_name_fields_anchored_to_evidence():
    objs = [
        KnowledgeObjectQ(id="O1", type="ArticleMethod", payload={"name": "Engram"},
                         local_evidence_atom_ids=["A1"]),
        KnowledgeObjectQ(id="O2", type="ArticleClaim", payload={"statement": "prose only"},
                         local_evidence_atom_ids=["A2"]),  # no name field -> no mention
        KnowledgeObjectQ(id="O3", type="Concept", payload={"term": "analog signal"},
                         local_evidence_atom_ids=[]),       # no evidence -> skipped
    ]
    ms = derive_mentions(objs)
    assert len(ms) == 1
    assert ms[0].text == "Engram"
    assert ms[0].atom_id == "A1"
    assert ms[0].canonical_key == "engram"
    assert ms[0].type == "ArticleMethod"


def test_canonicalization_groups_aliases():
    objs = [
        KnowledgeObjectQ(id="O1", type="ArticleMethod", payload={"name": "Engram"},
                         local_evidence_atom_ids=["A1"]),
        KnowledgeObjectQ(id="O2", type="ArchitectureComponent", payload={"name": "engram"},
                         local_evidence_atom_ids=["A3"]),
    ]
    canon = build_canonicalization(derive_mentions(objs))
    eng = next(c for c in canon if c["canonical"] == "engram")
    assert set(eng["aliases"]) == {"Engram", "engram"}
